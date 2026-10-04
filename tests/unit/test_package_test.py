"""Package tests in the core: files, the sandbox, coverage (CP-ADR-0074 §10; process-packages P013).

The sandbox runs the engine a live instance runs; here it runs without a
database — the catalog is given as a :class:`World`, as the application
layer builds it from the read-only transaction.
"""

import ast
import copy
import dataclasses
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from control_plane.application.commands.package_test import sandbox_rules, sql_writes
from control_plane.application.commands.process_instances import remembered
from control_plane.application.context.graph import entity_key
from control_plane.domain import package_source
from control_plane.domain import process_definition as pd
from control_plane.domain import process_sandbox as sb
from control_plane.domain.package_source import (
    MAX_DEPTH,
    MAX_INT_DIGITS,
    MAX_NODES,
    MAX_PACKAGE_NODES,
    MAX_PACKAGE_SOURCE_CHARS,
    MAX_SCALAR_CHARS,
    TEST_SCHEMA_FILE,
    Budget,
    SourceError,
    load_file,
    load_yaml,
    package_test_schema,
    parse_package,
    select_tests,
)
from control_plane.domain.process_definition import Catalog, SkillEntry
from control_plane.domain.process_engine import Definition
from tests.unit.test_process_contract import (
    PACKAGE_TEST,
    PINNED,
    PROCESS,
    retrospective_contract,
)
from tests.unit.test_process_engine import AUTHOR, CALENDARS, CATALOG, INSTANCE, OTHER, spec
from tests.unit.test_process_engine import Run as EngineRun

SANDBOX = Path(sb.__file__)
API_VERSION = PROCESS["apiVersion"]  # the catalog format of the fixtures


def world(body: str, **extra: Any) -> sb.World:
    definition = Definition.build("test", pd.normalized_spec(spec(body)), CATALOG)
    return sb.World(
        definitions={"test": definition},
        skills=CATALOG.skills,
        task_types=CATALOG.task_types,
        agents=CATALOG.agents,
        roles=frozenset({"lead"}),
        calendars=CALENDARS,
        **extra,
    )


def run(body: str, steps: list[dict[str, Any]], **test: Any) -> sb.TestResult:
    return sb.run_test(
        world(body), "tests/t.test.yaml", {"process": "test", "name": "t", "steps": steps, **test}
    )


def opened(**payload: Any) -> dict[str, Any]:
    body = {"number": "N-1", "amount": 100, "deadline": "2026-05-04T09:00:00Z", "author": AUTHOR}
    return {"emit": {"observation": "case.opened", "payload": {**body, **payload}}}


# --- the files of a package ----------------------------------------------------------------


def test_the_core_holds_the_superproject_test_schema() -> None:
    assert TEST_SCHEMA_FILE.read_bytes() == (PINNED / "test.schema.json").read_bytes()


def test_yaml_is_read_as_yaml_1_2_with_lines_by_pointer() -> None:
    document, lines = load_yaml("a:\n  on: {observation: x}\n  flag: yes\n  list:\n    - true\n")
    assert document == {"a": {"on": {"observation": "x"}, "flag": "yes", "list": [True]}}
    assert lines["/a/on/observation"] == 2 and lines["/a/list/0"] == 5


def _alias_bomb(depth: int, fan: int = 10) -> str:
    """Each level lists the one below ``fan`` times: fan**depth scalars from a few hundred bytes."""
    rows = [f"a0: &a0 [{', '.join(['x'] * fan)}]"]
    for level in range(1, depth + 1):
        below = ", ".join([f"*a{level - 1}"] * fan)
        rows.append(f"a{level}: &a{level} [{below}]")
    return "\n".join(rows) + "\n"


def _refused(text: str) -> SourceError:
    try:
        load_yaml(text)
    except SourceError as exc:
        return exc
    raise AssertionError("the document was read")


def test_an_alias_bomb_is_refused_fast_as_not_yaml() -> None:
    for depth in (6, 8, 12):
        text = _alias_bomb(depth)
        assert len(text) < 1024
        started = time.monotonic()
        error = _refused(text)
        assert time.monotonic() - started < 1
        assert f"more than {MAX_NODES} nodes" in error.message
    # A node holding itself is an endless nesting, not a RecursionError.
    assert "nested deeper" in _refused("a: &a [*a]\n").message
    package = parse_package([("roles/bomb.yaml", _alias_bomb(8))])
    [problem] = package.problems
    assert (problem.code, problem.file) == ("invalid_yaml", "roles/bomb.yaml")


def test_a_long_key_over_many_nodes_is_refused_before_its_pointers_are_made() -> None:
    # 50k items under a key of 500k characters: 25 billion characters of pointers.
    text = "? " + "k" * 500_000 + "\n: [" + "1, " * 50_000 + "1]\n"
    started = time.monotonic()
    error = _refused(text)
    assert time.monotonic() - started < 5
    assert (error.line, "characters of their pointers" in error.message) == (2, True)


def test_a_long_string_repeated_by_aliases_is_refused_fast() -> None:
    # 2000 characters, 300 aliases to them, 290 aliases to those: 87,000
    # nodes (under the node limit) and 174 million characters from 4 KB.
    text = (
        f"s: &s {'x' * 2000}\nl: &l [{', '.join(['*s'] * 300)}]\nm: [{', '.join(['*l'] * 290)}]\n"
    )
    assert len(text) < 5000
    started = time.monotonic()
    error = _refused(text)
    assert time.monotonic() - started < 1
    assert (error.line, f"more than {MAX_SCALAR_CHARS} characters" in error.message) == (1, True)
    # A string of 900 KB (a file near the API limit) by an alias five times over.
    big = "y" * 900_000
    started = time.monotonic()
    error = _refused(f"s: &s {big}\nl: [*s, *s, *s, *s, *s]\n")
    assert time.monotonic() - started < 1
    assert "characters" in error.message
    # Three times over it stays under the limit: the aliases are read.
    document, _ = load_yaml(f"s: &s {big}\nl: [*s, *s, *s]\n")
    assert document["l"] == [big] * 3
    # A file at the limit of the API without aliases is always under it.
    document, _ = load_yaml(f"s: {'z' * 999_990}\n")
    assert len(document["s"]) == 999_990


def test_a_string_is_searched_for_surrogates_once_however_many_aliases(
    monkeypatch: Any,
) -> None:
    searched: list[str] = []
    pattern = package_source._SURROGATE

    class Counting:
        def search(self, value: str) -> Any:
            searched.append(value)
            return pattern.search(value)

    monkeypatch.setattr(package_source, "_SURROGATE", Counting())
    text = f"s: &s {'x' * 40}\nl: &l [{', '.join(['*s'] * 300)}]\nm: [{', '.join(['*l'] * 20)}]\n"
    document, _ = load_yaml(text)
    assert len(document["m"]) == 20
    # The keys s, l, m and the one string: 6,000 expansions, four searches.
    assert sorted(searched) == sorted(["s", "l", "m", "x" * 40])
    # A surrogate behind an alias is found in the one node it is.
    error = _refused('s: &s "a\\ud800"\nl: [*s, *s]\n')
    assert ("lone surrogate" in error.message, error.line) == (True, 1)


def test_a_character_yaml_does_not_allow_is_not_yaml_with_its_line() -> None:
    for text, line in (("a: \x00", 1), ("a: 1\nb: x\x07\n", 2), ("\ufffe", 1)):
        error = _refused(text)
        assert ("unacceptable character" in error.message, error.line) == (True, line)
    package = parse_package([("roles/nul.yaml", "a: \x00\n")])
    [problem] = package.problems
    assert (problem.code, problem.file, problem.line) == ("invalid_yaml", "roles/nul.yaml", 1)


def test_a_json_file_nested_too_deep_is_refused_like_yaml() -> None:
    for lists in (MAX_DEPTH + 2, 5000):
        try:
            load_file("schemas/data.json", "[" * lists + "]" * lists)
        except SourceError as exc:
            assert "nested deeper" in exc.message
        else:
            raise AssertionError("the file was read")
    deepest = '{"a": ' * MAX_DEPTH + "1" + "}" * MAX_DEPTH
    assert load_file("schemas/data.json", deepest)[0] is not None
    assert load_file("schemas/data.json", "[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1))[0]


def test_a_document_nested_too_deep_is_not_yaml_not_a_recursion_error() -> None:
    # The outermost list is level 0, the innermost of n lists level n - 1.
    for lists in (MAX_DEPTH + 2, 5000):
        assert "nested deeper" in _refused("[" * lists + "]" * lists).message
    document, _ = load_yaml("[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1))
    assert document is not None


def test_a_document_with_a_few_aliases_is_read_with_them_expanded() -> None:
    text = "base: &base {kind: x, n: 1}\nfirst: *base\nsecond: {<<: *base, n: 2}\n"
    document, lines = load_yaml(text)
    assert document["first"] == {"kind": "x", "n": 1}
    assert document["second"] == {"kind": "x", "n": 2}
    assert lines["/first/kind"] == 1 and lines["/second"] == 3
    # Aliases of aliases are read while their expansion stays under the limit.
    document, _ = load_yaml(_alias_bomb(3))
    assert len(document["a3"]) == 10 and document["a3"][0][0][0] == ["x"] * 10


def test_a_lone_surrogate_is_refused_as_not_yaml() -> None:
    for text in (
        'content: "\\ud800"\n',
        'payload: {note: "a\\udfffb"}\n',
        'list:\n  - fine\n  - "\\ud83d"\n',
        '"\\ud800": key\n',
    ):
        error = _refused(text)
        assert "lone surrogate" in error.message
    assert _refused('list:\n  - fine\n  - "\\ud83d"\n').line == 3
    # Characters beyond the BMP are text: written as such or as \U escape.
    document, _ = load_yaml('a: "\U0001f600"\nb: "\\U0001F600"\n')
    assert document == {"a": "\U0001f600", "b": "\U0001f600"}
    # A JSON file of the package (a schema a process refers to) likewise;
    # a surrogate pair there is one character and stays.
    try:
        load_file("schemas/data.json", '{"title": "\\ud800"}')
    except SourceError as exc:
        assert "lone surrogate" in exc.message
    else:
        raise AssertionError("the file was read")
    assert load_file("schemas/data.json", '{"t": "\\ud83d\\ude00"}')[0] == {"t": "\U0001f600"}
    package = parse_package([("tests/bad.test.yaml", 'process: p\nname: "\\ud800"\n')])
    [problem] = package.problems
    assert (problem.code, problem.file, problem.line) == ("invalid_yaml", "tests/bad.test.yaml", 2)


def test_a_bomb_spread_over_many_files_is_refused_by_the_budget_of_the_package() -> None:
    # Nine aliases on four levels: 75 thousand nodes from 250 bytes, under the
    # limit of a file; 300 such files hold 22 million.
    text = _alias_bomb(4, fan=9)
    assert len(text) < 300
    load_yaml(text)
    files = [(f"roles/bomb{index:03}.yaml", text) for index in range(300)]
    started = time.monotonic()
    package = parse_package(files)
    assert time.monotonic() - started < 2
    # The first two fit (and are no catalog objects); from the third on the package is spent.
    codes = [(p.code, p.file) for p in package.problems]
    assert codes[:2] == [
        ("invalid_document", "roles/bomb000.yaml"),
        ("invalid_document", "roles/bomb001.yaml"),
    ]
    refused = package.problems[2:]
    assert [p.file for p in refused] == [path for path, _ in files[2:]]
    assert {p.code for p in refused} == {"invalid_yaml"}
    assert all(f"more than {MAX_PACKAGE_NODES} nodes" in p.message for p in refused)
    # A file refused by the limit of a file spends the budget all the same.
    refusing = [(f"roles/bomb{index:03}.yaml", _alias_bomb(5)) for index in range(300)]
    started = time.monotonic()
    package = parse_package(refusing)
    assert time.monotonic() - started < 2
    assert f"more than {MAX_NODES} nodes" in package.problems[0].message
    assert f"more than {MAX_PACKAGE_NODES} nodes" in package.problems[-1].message
    # Each package starts with a budget of its own.
    assert parse_package(files[:2]).problems[-1].code == "invalid_document"


def test_the_budget_counts_strings_pointers_and_text_over_files_and_refs() -> None:
    budget = Budget()
    load_yaml(f"s: {'x' * 1000}\nl: [1, 2]\n", budget)
    assert budget.nodes == MAX_PACKAGE_NODES - 7
    assert budget.source_chars == MAX_PACKAGE_SOURCE_CHARS - 1014
    # Strings: 4.2 million characters in two files of the package, each under a file's limit.
    budget = Budget()
    load_yaml(f"s: &s {'y' * 900_000}\nl: [*s, *s, *s]\n", budget)
    error = None
    try:
        load_yaml(f"s: &s {'y' * 200_000}\nl: [*s, *s]\n", budget)
    except SourceError as exc:
        error = exc
    assert error is not None and "the files of the package" in error.message
    # Pointers: a long key over many nodes, twice.
    budget = Budget(pointer_chars=1_000)
    load_yaml("? " + "k" * 100 + "\n: [1, 2, 3]\n", budget)
    try:
        load_yaml("? " + "k" * 100 + "\n: [1, 2, 3, 4, 5, 6, 7]\n", budget)
    except SourceError as exc:
        assert "the files of the package" in exc.message
    else:
        raise AssertionError("the document was read")
    # Text: a schema of comments twenty processes name is read each time; once
    # 4 million characters are read, the rest of the package is refused unread.
    body = spec("stages: []")
    schema = "# " + "c" * 400_000 + "\ntype: object\n"
    files = [
        (
            f"processes/p{index:02}.yaml",
            _process_file({**body, "data": {"$ref": "../schemas/s.yaml"}}, f"p{index:02}"),
        )
        for index in range(20)
    ]
    started = time.monotonic()
    package = parse_package([*files, ("schemas/s.yaml", schema)])
    assert time.monotonic() - started < 5
    assert [o.key for o in package.objects] == [f"p{index:02}" for index in range(10)]
    assert [(p.code, p.file) for p in package.problems[:2]] == [
        ("unresolved_data_ref", "processes/p09.yaml"),
        ("invalid_yaml", "processes/p10.yaml"),
    ]
    assert all("the files of the package" in p.message for p in package.problems)
    # A JSON schema a process names spends nodes and strings as YAML does.
    budget = Budget()
    load_file("schemas/a.json", json.dumps({"k": ["abc"] * 10}), budget)
    assert budget.nodes == MAX_PACKAGE_NODES - 12
    try:
        load_file("schemas/a.json", json.dumps(list(range(10))), Budget(nodes=5))
    except SourceError as exc:
        assert "the files of the package" in exc.message
    else:
        raise AssertionError("the file was read")


def test_a_value_json_has_not_is_refused_and_a_date_is_a_string() -> None:
    for text in (
        "a: !!binary aGVsbG8=\n",
        "a: !!timestamp 2026-09-30\n",
        "a: !!set {x: null}\n",
        "a: !!omap [{x: 1}]\n",
        "a: .nan\n",
        "a: [1, -.inf]\n",
        "a: !!float .Inf\n",
    ):
        error = _refused(text)
        assert "JSON values only" in error.message, text
    assert _refused("a: 1\nb: !!binary aGVsbG8=\n").line == 2
    # YAML 1.2: a date and a time are strings, as a date input of a decision table takes them.
    document, _ = load_yaml("day: 2026-09-30\nat: 2026-09-30T10:00:00Z\nn: 1.5\ninf: .infinity\n")
    assert document == {
        "day": "2026-09-30",
        "at": "2026-09-30T10:00:00Z",
        "n": 1.5,
        "inf": ".infinity",
    }
    json.dumps(document, allow_nan=False)
    # The explicit tags of JSON values and a merge key stay.
    document, _ = load_yaml("b: &b {x: 1}\na: !!str 1\nc: {<<: *b}\nd: !!int '2'\n")
    assert document == {"b": {"x": 1}, "a": "1", "c": {"x": 1}, "d": 2}
    for text in ('{"a": NaN}', "[Infinity]", '{"a": -Infinity}'):
        try:
            load_file("schemas/a.json", text)
        except SourceError as exc:
            assert "JSON values only" in exc.message
        else:
            raise AssertionError("the file was read")
    package = parse_package([("tests/bin.test.yaml", "process: p\nname: !!binary aGVsbG8=\n")])
    [problem] = package.problems
    assert (problem.code, problem.file, problem.line) == ("invalid_yaml", "tests/bin.test.yaml", 2)


def test_integers_are_those_of_yaml_1_2_and_sexagesimal_costs_nothing() -> None:
    document, _ = load_yaml(
        "a: 012\nb: 0o17\nc: 0x1F\nd: -12\ne: +3\nf: 0\n"
        "g: 1:30\nh: 0b101\ni: 1_000\nj: -0x1F\nk: !!int 0o17\nl: !!int '-7'\n"
    )
    assert document == {
        "a": 12,
        "b": 15,
        "c": 31,
        "d": -12,
        "e": 3,
        "f": 0,
        "g": "1:30",
        "h": "0b101",
        "i": "1_000",
        "j": "-0x1F",
        "k": 15,
        "l": -7,
    }
    # A megabyte of sexagesimal: YAML 1.1 made it one int in half a minute.
    clock = "1" + ":59" * 330_000
    started = time.monotonic()
    document, _ = load_yaml(f"a: {clock}\n")
    assert time.monotonic() - started < 2
    assert document == {"a": clock}


def test_an_integer_past_its_digits_is_refused_before_int_pays_for_it() -> None:
    assert MAX_INT_DIGITS == 1000
    for fits in ("9" * 1000, "-" + "9" * 1000, "0x" + "f" * 1000, "0o" + "7" * 1000):
        document, _ = load_yaml(f"a: {fits}\n")
        json.dumps(document)
    assert load_file("schemas/a.json", f"[{'9' * 1000}]")[0] == [int("9" * 1000)]
    for text in (
        "a: " + "9" * 1001,
        "a: " + "9" * 5000,
        "a: -" + "9" * 5000,
        "a: 0x" + "f" * 5000,
        "a: 0o" + "7" * 1001,
        "a: !!int " + "1" * 5000,
    ):
        started = time.monotonic()
        error = _refused("x: 1\n" + text + "\n")
        assert time.monotonic() - started < 1
        assert (error.message, error.line) == ("an integer has more than 1000 digits", 2)
    for text in ("[" + "9" * 5000 + "]", '{"a": -' + "9" * 1001 + "}"):
        try:
            load_file("schemas/a.json", text)
        except SourceError as exc:
            assert exc.message == "an integer has more than 1000 digits"
        else:
            raise AssertionError("the file was read")


def test_every_float_is_finite_however_it_is_written() -> None:
    for value in (
        "1.0e+999",
        "-1.0e+999",
        "1" + "0" * 5000 + ".0",
        "!!float nan",
        "!!float inf",
        "!!float -inf",
        "!!float 1e999",
        "!!float .NaN",
        "1" + ":59" * 330_000 + ".5",
    ):
        started = time.monotonic()
        error = _refused(f"x: 1\na: {value}\n")
        assert time.monotonic() - started < 2
        assert "JSON values only" in error.message and error.line == 2, value
        assert len(error.message) < 120  # a long scalar is cut in the message
    for text in ("[1e999]", '{"a": -1e999}', '{"a": 1.5e400}'):
        try:
            load_file("schemas/a.json", text)
        except SourceError as exc:
            assert "JSON values only" in exc.message, text
        else:
            raise AssertionError("the file was read")
    document, _ = load_yaml("a: 1.5\nb: -0.0\nc: 1.7e+308\nd: !!float 2\n")
    assert document == {"a": 1.5, "b": -0.0, "c": 1.7e308, "d": 2.0}
    assert load_file("schemas/a.json", "[1.5, 1e300]")[0] == [1.5, 1e300]


def test_a_scalar_its_tag_cannot_read_is_not_yaml_at_its_line() -> None:
    for value, message in (
        ("!!int abc", "'abc' is not an integer of YAML 1.2"),
        ("!!int 1:30", "'1:30' is not an integer of YAML 1.2"),
        ("!!int ''", "'' is not an integer of YAML 1.2"),
        ("!!float abc", "'abc' is not a float"),
        ("!!float ''", "'' is not a float"),
        ("!!bool maybe", "'maybe' is not a boolean of YAML 1.2"),
        ("!!bool yes", "'yes' is not a boolean of YAML 1.2"),
    ):
        error = _refused(f"x: 1\na: {value}\n")
        assert (error.message, error.line) == (message, 2), value
    document, _ = load_yaml("a: !!bool true\nb: !!bool False\n")
    assert document == {"a": True, "b": False}
    package = parse_package([("tests/n.test.yaml", "process: p\nname: !!int x\n")])
    [problem] = package.problems
    assert (problem.code, problem.file, problem.line) == ("invalid_yaml", "tests/n.test.yaml", 2)


def test_the_packages_of_the_fixtures_read_as_before() -> None:
    """No number of theirs is written in a form YAML 1.1 and 1.2 read apart."""
    root = Path(__file__).resolve().parents[1] / "fixtures"
    files = sorted(root.rglob("*.yaml"))
    assert len(files) > 50
    for path in files:
        text = path.read_text(encoding="utf-8")
        old = yaml.load(text, Loader=_yaml11_but_bool_and_timestamp())
        assert load_yaml(text)[0] == old, path


def _yaml11_but_bool_and_timestamp() -> type[yaml.SafeLoader]:
    """The loader before TASK-001247: integers and floats of YAML 1.1."""

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [
            (tag, rx)
            for tag, rx in resolvers
            if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
        ]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    return Loader


def _process_file(spec_body: dict[str, Any], key: str = "test") -> str:
    document = {"apiVersion": API_VERSION, "kind": "Process", "key": key, "spec": spec_body}
    return str(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def test_a_package_parses_objects_tests_and_places_its_findings() -> None:
    test = {"process": "test", "name": "t", "steps": [{"expect": {"status": "running"}}]}
    package = parse_package(
        [
            ("package.yaml", f"apiVersion: {API_VERSION}\nkind: Package\nkey: p\nspec: {{}}\n"),
            ("processes/test.yaml", _process_file(spec("stages: []"))),
            ("tests/ok.test.yaml", yaml.safe_dump(test)),
            ("tests/bad.test.yaml", "process: test\nname: t\nsteps:\n  - {jump: 1}\n"),
            ("tests/other.test.yaml", yaml.safe_dump({**test, "process": "nothing"})),
            ("broken.yaml", "a: [1,\n"),
            ("odd.yaml", f"apiVersion: {API_VERSION}\nkind: Oddity\nkey: x\nspec: {{}}\n"),
            ("again.yaml", _process_file(spec("stages: []"))),
            ("future.yaml", "apiVersion: example.org/v2\nkind: Role\nkey: r\nspec: {}\n"),
            ("schemas/data.yaml", "type: object\n"),
            ("README.md", "not a catalog object"),
        ]
    )
    assert package.manifest == {}
    assert [o.ref for o in package.objects] == ["Process/test"]
    assert [t.file for t in package.tests] == ["tests/ok.test.yaml", "tests/other.test.yaml"]
    found = {(p.code, p.file, p.line) for p in package.problems}
    assert ("invalid_yaml", "broken.yaml", 2) in found
    assert ("unknown_kind", "odd.yaml", 2) in found
    assert ("invalid_document", "future.yaml", 1) in found
    assert ("duplicate_object", "processes/test.yaml", 3) in found  # again.yaml came first
    assert ("invalid_test", "tests/bad.test.yaml", 4) in found
    assert ("unknown_test_process", "tests/other.test.yaml", 2) in found  # keys sorted


def test_a_data_ref_is_inlined_from_the_package_and_never_from_outside() -> None:
    body = spec("stages: []")
    schema = body["data"]
    package = parse_package(
        [
            (
                "processes/a.yaml",
                _process_file({**body, "data": {"$ref": "../schemas/a.json"}}, "a"),
            ),
            ("processes/b.yaml", _process_file({**body, "data": {"$ref": "../../x.json"}}, "b")),
            ("schemas/a.json", json.dumps(schema)),
        ]
    )
    assert package.process("a") is not None and package.process("a").spec["data"] == schema  # type: ignore[union-attr]
    (problem,) = package.problems
    assert (problem.code, problem.file, problem.path) == (
        "unresolved_data_ref",
        "processes/b.yaml",
        "/spec/data/$ref",
    )


def test_a_data_ref_to_a_file_the_loader_refuses_is_unresolved() -> None:
    body = spec("stages: []")
    for name, content in (
        ("schemas/a.json", '{"title": "\\ud800"}'),
        ("schemas/a.yaml", _alias_bomb(8)),
    ):
        target = {**body, "data": {"$ref": f"../{name}"}}
        package = parse_package([("processes/a.yaml", _process_file(target, "a")), (name, content)])
        (problem,) = package.problems
        assert (problem.code, problem.file) == ("unresolved_data_ref", "processes/a.yaml")


def test_the_request_may_name_tests_and_a_name_that_is_no_test_is_a_finding() -> None:
    test = {"process": "test", "name": "t", "steps": [{"advance": "P1D"}]}
    package = parse_package(
        [
            ("processes/test.yaml", _process_file(spec("stages: []"))),
            ("tests/a.test.yaml", yaml.safe_dump(test)),
            ("tests/b.test.yaml", yaml.safe_dump(test)),
        ]
    )
    chosen, problems = select_tests(package, ["tests/b.test.yaml", "tests/c.test.yaml"])
    assert [t.file for t in chosen] == ["tests/b.test.yaml"]
    assert [(p.code, p.path) for p in problems] == [("unknown_test", "/tests/1")]


# --- the sandbox ---------------------------------------------------------------------------

SKILL = """
stages:
  - id: s
    steps:
      - id: work
        call: {skill: work.do@1, input: {text: "'x'"}}
        output: {as: {value: step.result.value}}
"""


def test_a_skill_mock_that_matches_the_skill_schema_answers_the_call() -> None:
    result = run(
        SKILL,
        [opened(), {"expect": {"status": "completed", "data": {"value": "done"}}}],
        mocks={"skills": {"work.do@1": [{"output": {"value": "done"}}]}},
    )
    assert result.status == "passed", result.failures


def test_a_skill_mock_off_the_skill_schema_fails_the_test() -> None:
    result = run(
        SKILL,
        [opened(), {"expect": {"status": "completed"}}],
        mocks={"skills": {"work.do@1": [{"output": {"value": 5}}]}},
    )
    assert result.status == "failed"
    (failure,) = result.failures
    assert failure.step == 0
    assert "does not match the skill's output schema" in failure.message
    assert failure.actual == {"value": 5}


def test_a_call_without_a_mock_waits_and_a_mock_error_is_the_steps_error() -> None:
    waiting = run(SKILL, [opened(), {"expect": {"status": "running", "stages": {"s": "open"}}}])
    assert waiting.status == "passed", waiting.failures
    failing = run(
        SKILL,
        [opened(), {"expect": {"status": "failed", "error": "busy"}}],
        mocks={"skills": {"work.do@1": [{"error": {"type": "busy", "status": 503}}]}},
    )
    assert failing.status == "passed", failing.failures


def test_a_recall_mock_off_the_form_of_a_memory_answer_fails_the_test() -> None:
    body = """
stages:
  - id: s
    steps:
      - id: history
        recall: {anchors: [{kind: person, key: data.author}], kinds: [case]}
        output: {as: {history: step.result.nodes}}
"""
    good = run(
        body,
        [opened(), {"expect": {"memory": {"recalled": ["history"]}, "status": "completed"}}],
        mocks={
            "recall": [{"step": "history", "output": {"nodes": [{"kind": "case", "key": "c"}]}}]
        },
    )
    assert good.status == "passed", good.failures
    bad = run(body, [opened()], mocks={"recall": [{"output": {"nodes": [{"kind": "case"}]}}]})
    assert bad.status == "failed" and "not a memory answer" in bad.failures[0].message


def test_a_recall_mock_answers_by_the_computed_where() -> None:
    # CP-ADR-0076, amendment 2026-09-28 (K011): the mock's input is the intent,
    # its where computed from the data, so a test answers each filter its own way.
    body = """
stages:
  - id: s
    steps:
      - id: code
        set: {value: "data.amount > 500.0 ? '62.01' : '58.29'"}
      - id: offers
        recall:
          anchors: [{kind: company, key: data.author}]
          where:
            - {attr: okpd2, op: prefix, value: data.value}
            - {attr: validUntil, op: gte, value: data.deadline}
        output: {as: {history: step.result.nodes}}
"""
    software = {"nodes": [{"kind": "product", "key": "p-62"}]}
    books = {"nodes": [{"kind": "product", "key": "p-58"}]}
    mocks = {
        "recall": [
            {
                "step": "offers",
                "when": "input.where.exists(c, c.attr == 'okpd2' && c.value == '62.01')"
                " && input.where.exists(c, c.attr == 'validUntil'"
                " && c.op == 'gte' && c.value == '2026-05-04T09:00:00Z')",
                "output": software,
            },
            {"step": "offers", "when": "input.where[0].value == '58.29'", "output": books},
        ]
    }
    for amount, key in ((1000, "p-62"), (100, "p-58")):
        result = run(
            body,
            [
                opened(amount=amount),
                {
                    "expect": {
                        "memory": {"recalled": ["offers"]},
                        "data": {"history": [{"kind": "product", "key": key}]},
                        "status": "completed",
                    }
                },
            ],
            mocks=mocks,
        )
        assert result.status == "passed", result.failures
    # Another deadline is another filter: no mock answers it, the step waits.
    missed = run(
        body,
        [
            opened(amount=1000, deadline="2026-06-01T00:00:00Z"),
            {"expect": {"memory": {"recalled": []}, "status": "running"}},
        ],
        mocks=mocks,
    )
    assert missed.status == "passed", missed.failures


APPROVE = """
stages:
  - id: s
    steps:
      - id: sign
        approve:
          approvers: [{role: lead}]
          quorum: {atLeast: 1}
          separationOfDuties: "[data.author]"
        output: {as: {decision: step.result.outcome}}
"""


def test_the_core_refuses_an_excluded_or_ineligible_approver() -> None:
    principals = {"lead": [AUTHOR, "bob"]}
    steps = [
        opened(),
        {
            "approve": {
                "step": "sign",
                "by": AUTHOR,
                "decision": "approve",
                "expectRefused": "separation_of_duties_violation",
            }
        },
        {
            "approve": {
                "step": "sign",
                "by": "eve",
                "decision": "approve",
                "expectRefused": "not_eligible",
            }
        },
        {"approve": {"step": "sign", "by": "bob", "decision": "approve"}},
        {"expect": {"status": "completed", "data": {"decision": "approved"}}},
    ]
    result = run(APPROVE, steps, given={"principals": principals})
    assert result.status == "passed", result.failures

    refused = run(
        APPROVE,
        [opened(), {"approve": {"step": "sign", "by": AUTHOR, "decision": "approve"}}],
        given={"principals": principals},
    )
    assert refused.status == "failed"
    assert refused.failures[0].actual == "separation_of_duties_violation"


def test_an_excluded_approver_is_refused_as_the_core_refuses_it() -> None:
    """Nobody could decide: the step fails ``intent_failed`` in the sandbox as on the core."""
    body = APPROVE.replace("[{role: lead}]", f"[{{role: lead}}, {{principal: {AUTHOR}}}]")
    result = run(
        body,
        [opened(), {"expect": {"status": "failed", "error": "intent_failed"}}],
        given={"principals": {"lead": ["bob"]}},
    )
    assert result.status == "passed", result.failures


def test_an_empty_exclusion_fails_the_step_as_on_the_core() -> None:
    """``uploadedBy`` left empty is no exclusion dropped: ``intent_failed`` (CP-ADR-0074 §7)."""
    result = run(
        APPROVE,
        [opened(author=""), {"expect": {"status": "failed", "error": "intent_failed"}}],
        given={"principals": {"lead": ["bob"]}},
    )
    assert result.status == "passed", result.failures


def test_virtual_time_fires_timers_in_order_at_their_own_moment() -> None:
    body = """
stages:
  - id: s
    steps:
      - {id: pause, wait: P2D}
      - {id: note, set: {note: "string(instance.clock)"}}
"""
    steps = [
        opened(),
        {"advance": "P1D"},
        {
            "expect": {
                "status": "running",
                "timers": [{"id": "pause", "at": "2026-03-04T09:00:00Z"}],
            }
        },
        {"advance": "until:pause"},
        {"expect": {"status": "completed", "data": {"note": "2026-03-04T09:00:00Z"}}},
    ]
    result = run(body, steps, given={"clock": "2026-03-02T09:00:00Z"})
    assert result.status == "passed", result.failures


def test_a_human_step_is_completed_by_its_assignee_with_its_field_schema() -> None:
    body = """
stages:
  - id: s
    steps:
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
        output: {as: {decision: step.result.decision}}
"""
    given = {"principals": {"lead": ["alice"]}}
    steps = [
        opened(),
        {"expect": {"tasks": [{"step": "review", "assignee": "alice", "status": "open"}]}},
        {"complete": {"step": "review", "by": "alice", "output": {"decision": "yes"}}},
        {"expect": {"status": "completed", "data": {"decision": "yes"}}},
    ]
    assert run(body, steps, given=given).status == "passed"
    stranger = run(body, [opened(), {"complete": {"step": "review", "by": "mallory"}}], given=given)
    assert stranger.status == "failed" and "may not complete" in stranger.failures[0].message
    wrong = run(
        body, [opened(), {"complete": {"step": "review", "output": {"decision": 1}}}], given=given
    )
    assert wrong.status == "failed" and "field schema" in wrong.failures[0].message


AUTHORED = """
start:
  on: {observation: case.opened}
  key: event.payload.number
  set: {number: string(event.payload.number), author: string(event.actorId)}
correlate:
  - on: {observation: case.changed}
    key: event.payload.number
    set: {note: event.actorId}
stages:
  - id: s
    steps:
      - {id: hold, human: {taskType: review, assign: [{role: lead}]}}
"""


def _emit(observation: str, **extra: Any) -> dict[str, Any]:
    return {"emit": {"observation": observation, "payload": {"number": "N-1"}, **extra}}


def test_the_author_of_an_emitted_event_is_its_actor_id() -> None:
    steps = [
        _emit("case.opened", by="alice"),
        {"expect": {"status": "running", "data": {"author": "alice"}}},
        _emit("case.changed", by="agent:writer"),
        {"expect": {"data": {"author": "alice", "note": "agent:writer"}}},
    ]
    result = run(AUTHORED, steps, given={"principals": {"lead": ["alice"]}})
    assert result.status == "passed", result.failures


def test_the_author_of_an_event_is_the_actor_of_the_inputs_it_feeds() -> None:
    box = sb.Sandbox(world(AUTHORED), {"process": "test", "steps": []}, seed="t")
    box.emit(_emit("case.opened", by="alice")["emit"])
    box.emit(_emit("case.changed", by="bob")["emit"])
    box.emit(_emit("case.changed")["emit"])
    (journal,) = box.journals.values()
    inputs = [e["data"]["input"] for e in journal if e["kind"] == "input"]
    assert [(i["kind"], i["actorId"]) for i in inputs] == [
        ("start", "alice"),
        ("event", "bob"),
        ("event", None),
    ]


def test_an_event_without_an_author_has_no_actor_id_as_before() -> None:
    # As before ``by``: no actorId in the event, and the instance fails on it.
    failed = {"status": "failed", "error": "expression_error"}
    assert run(AUTHORED, [_emit("case.opened"), {"expect": failed}]).status == "passed"
    guarded = AUTHORED.replace(
        "author: string(event.actorId)",
        "author: \"has(event.actorId) ? event.actorId : 'nobody'\"",
    )
    steps = [_emit("case.opened"), {"expect": {"data": {"author": "nobody"}}}]
    assert run(guarded, steps).status == "passed"


def test_the_schema_takes_a_principal_or_an_agent_as_the_author_of_an_emit() -> None:
    validator = Draft202012Validator(package_test_schema())

    def errors(by: Any) -> list[str]:
        step = {"emit": {"observation": "case.opened", "by": by}}
        test = {"name": "t", "process": "p", "steps": [step]}
        return [e.message for e in validator.iter_errors(test)]

    assert errors("alice") == [] and errors("agent:writer") == []
    assert errors("") and errors(None) and errors(7) and errors(["alice"])


def test_an_expectation_that_does_not_hold_names_what_was_expected_and_what_is() -> None:
    result = run(
        "stages: [{id: s, steps: [{id: note, set: {note: \"'x'\"}}]}]",
        [
            opened(),
            {"expect": {"status": "running", "data": {"note": "y"}, "events": ["process.failed"]}},
            {"expect": {"outcome": "completed", "noSideEffects": True}},
        ],
    )
    assert result.status == "failed"
    assert [(f.step, f.expected, f.actual) for f in result.failures] == [
        (1, "running", "completed"),
        (1, "y", "x"),
        (
            1,
            "process.failed",
            [
                "process.started",
                "process.stage_entered",
                "process.stage_exited",
                "process.data_changed",
                "process.completed",
            ],
        ),
    ]


def test_no_side_effects_reads_the_count_of_writes_outside_the_sandbox() -> None:
    writes = [0]
    body = "stages: [{id: s, steps: [{id: n, set: {note: \"'x'\"}}]}]"
    here = dataclasses.replace(world(body), writes=lambda: writes[0])
    test = {
        "process": "test",
        "name": "t",
        "steps": [opened(), {"expect": {"noSideEffects": True}}],
    }
    assert sb.run_test(here, "t", test).status == "passed"
    writes[0] = 2
    failed = sb.run_test(here, "t", test)
    assert failed.status == "failed" and failed.failures[0].actual == 2


def test_raw_sql_is_a_write_by_its_keyword_and_a_read_is_not() -> None:
    reads = [
        "SELECT version_num FROM alembic_version",
        "  -- the ancestors\n  WITH RECURSIVE a AS (SELECT id FROM workspaces) SELECT id FROM a",
        "WITH t AS (SELECT id FROM roles FOR UPDATE) SELECT id FROM t",
        "WITH t AS (SELECT id FROM roles FOR NO KEY UPDATE) SELECT 'delete' FROM t",
        '/* insert */ SELECT deleted_at, "update" FROM tasks',
        "SET TRANSACTION READ ONLY",
        "",
    ]
    writes = [
        "INSERT INTO roles (slug) VALUES ('x')",
        "update roles SET slug = 'x'",
        "/* note */ DELETE FROM roles",
        "MERGE INTO roles USING t ON true WHEN MATCHED THEN DELETE",
        "COPY roles FROM STDIN",
        "TRUNCATE roles",
        "WITH gone AS (DELETE FROM roles RETURNING id) SELECT id FROM gone",
    ]
    assert [sql for sql in reads if sql_writes(sql)] == []
    assert [sql for sql in writes if not sql_writes(sql)] == []


def test_the_example_test_of_the_superproject_passes() -> None:
    example = copy.deepcopy(PROCESS["spec"])
    example["data"]["properties"]["approversNeeded"] = {"type": "number"}
    del example["migrations"]  # a map into version 2 belongs to version 2
    catalog = Catalog(
        skills={
            "notify.send@1": SkillEntry(
                {"type": "object", "properties": {"text": {"type": "string"}}}, None
            ),
            "process.retrospective@1": SkillEntry(None, None),
        },
        task_types={"go-no-go": None, "lessons-review": None},
        agents=frozenset({"example-process"}),
        calendars=frozenset({"ru"}),
        artifact_types=frozenset({"notice-document"}),
        processes=frozenset(),
    )
    definition = Definition.build(PROCESS["key"], pd.normalized_spec(example), catalog)
    here = sb.World(
        definitions={definition.key: definition},
        skills=catalog.skills,
        task_types=catalog.task_types,
        agents=catalog.agents,
        roles=frozenset({"director"}),
        calendars=CALENDARS,
    )
    result = sb.run_test(here, "tests/purchase.test.yaml", PACKAGE_TEST)
    assert result.status == "passed", result.failures


# --- coverage ------------------------------------------------------------------------------

BRANCHES = """
decisions:
  - id: level
    hitPolicy: first
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: "[0..1000)"}, then: {level: 1}}
      - {when: {amount: "-"}, then: {level: 2}}
stages:
  - id: s
    steps:
      - id: pick
        decide: {table: level}
        output: {as: {level: step.result.level}}
      - id: big
        when: data.level == 2.0
        set: {note: "'big'"}
      - id: guarded
        try:
          do:
            - {id: work, call: {skill: work.do@1, input: {text: "'x'"}}}
          catch:
            - errors: {type: busy}
              do: [{id: fallback, set: {note: "'fallback'"}}]
"""


def test_coverage_counts_what_the_tests_reached_and_lists_the_rest() -> None:
    here = world(BRANCHES)
    test = {
        "process": "test",
        "name": "small",
        "mocks": {"skills": {"work.do@1": [{"output": {"value": "v"}}]}},
        "steps": [opened(amount=10), {"expect": {"status": "completed"}}],
    }
    result = sb.run_test(here, "t", test)
    assert result.status == "passed", result.failures
    (coverage,) = sb.package_coverage(here.definitions.values(), [result])
    out = coverage.out()
    assert out["elements"]["missing"] == ["big", "fallback"]
    # correlate/0 comes with the header every process of these tests shares
    assert out["transitions"] == {"covered": 1, "total": 3, "missing": ["correlate/0", "big:when"]}
    assert out["decisionRows"] == {"covered": 1, "total": 2, "missing": ["level/1"]}
    assert out["handlers"] == {"covered": 0, "total": 1, "missing": ["guarded/catch/0"]}

    other = {
        **test,
        "name": "big and failing",
        "mocks": {"skills": {"work.do@1": [{"error": {"type": "busy"}}]}},
        "steps": [opened(amount=5000), {"expect": {"data": {"note": "fallback"}}}],
    }
    second = sb.run_test(here, "t", other)
    assert second.status == "passed", second.failures
    (both,) = sb.package_coverage(here.definitions.values(), [result, second])
    out = both.out()
    assert [out[name]["missing"] for name in ("elements", "decisionRows", "handlers")] == [
        [],
        [],
        [],
    ]
    assert out["transitions"]["missing"] == ["correlate/0"]


def test_a_test_below_its_coverage_minimum_fails() -> None:
    result = run(
        BRANCHES,
        [opened(amount=10)],
        mocks={"skills": {"work.do@1": [{"output": {"value": "v"}}]}},
        coverage={"minimum": 100},
    )
    assert result.status == "failed"
    assert result.failures[0].actual["missing"] == ["big", "fallback"]


# --- nothing leaves the sandbox --------------------------------------------------------------


def test_the_sandbox_has_no_client() -> None:
    """The sandbox imports the domain and plain libraries only: no database, HTTP or memory."""
    tree = ast.parse(SANDBOX.read_text(encoding="utf-8"))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    project = {name for name in imported if name.startswith("control_plane")}
    assert all(name.startswith("control_plane.domain") for name in project), project
    assert not imported & {"httpx", "sqlalchemy", "boto3", "socket", "urllib.request"}


# --- the trial run on a stand (P014) -------------------------------------------------------

REVIEW = """
stages:
  - id: s
    steps:
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
        output: {as: {decision: step.result.decision}}
      - id: sign
        approve:
          approvers: [{role: lead}]
          quorum: {atLeast: 1}
          separationOfDuties: "[data.author]"
        output: {as: {outcome: step.result.outcome}}
"""


def _live(body: str, *, until: str) -> tuple[sb.LiveInstance, Any]:
    """A live instance as the core keeps it, stopped at the open step ``until``."""
    live = EngineRun(body)
    live.clock = live.clock.replace(day=9)
    live.start(amount=700)
    if until == "sign":
        live.complete_task("review", {"decision": "go"})
    assert live.state is not None
    activity = live.activity(until)
    approvals = (
        (
            {
                "activity": activity["id"],
                "element": "sign",
                "approver": "role:lead",
                "excluded": [AUTHOR],
            },
        )
        if until == "sign"
        else ()
    )
    return (
        sb.LiveInstance(
            id=INSTANCE,
            process="test",
            state=live.state,
            approvals=approvals,
            totals={activity["id"]: 1} if until == "sign" else {},
        ),
        live,
    )


def _trial(body: str, live: sb.LiveInstance, steps: list[dict[str, Any]], **given: Any) -> Any:
    here = dataclasses.replace(world(body), live={live.id: live})
    test = {
        "process": "test",
        "name": "trial",
        "given": {"fromInstance": live.id, "principals": {"lead": [AUTHOR, "bob"]}, **given},
        "steps": steps,
    }
    return sb.run_test(here, "tests/trial.test.yaml", test)


def test_a_trial_run_goes_on_from_a_copy_of_a_live_instance() -> None:
    live, _ = _live(REVIEW, until="review")
    before = copy.deepcopy(live.state)
    steps = [
        {"expect": {"status": "running", "data": {"amount": 700}}},
        {"expect": {"tasks": [{"step": "review", "assignee": "role:lead", "status": "open"}]}},
        {"complete": {"step": "review", "by": "bob", "output": {"decision": "go"}}},
        {"expect": {"data": {"decision": "go"}}},
        {"approve": {"step": "sign", "by": "bob", "decision": "approve"}},
        {"expect": {"status": "completed", "data": {"outcome": "approved"}}},
    ]
    result = _trial(REVIEW, live, steps)
    assert result.status == "passed", result.failures
    assert live.state == before, "the live state is copied, never changed"


def test_a_trial_run_starts_its_clock_at_the_instances_last_input() -> None:
    body = REVIEW.replace(
        "      - id: sign\n",
        '      - {id: stamp, set: {note: "string(instance.clock)"}}\n      - id: sign\n',
    )
    live, source = _live(body, until="review")
    assert source.state is not None
    at = source.state["clock"]
    steps = [
        {"complete": {"step": "review", "by": "bob", "output": {"decision": "go"}}},
        {"expect": {"data": {"note": at}}},
    ]
    assert _trial(body, live, steps).status == "passed"
    given = _trial(body, live, steps, clock="2026-04-01T00:00:00Z")
    assert given.status == "failed" and given.failures[0].actual == "2026-04-01T00:00:00Z"


def test_a_trial_run_takes_the_pending_approvals_of_the_instance() -> None:
    live, _ = _live(REVIEW, until="sign")
    steps = [
        {
            "approve": {
                "step": "sign",
                "by": AUTHOR,
                "decision": "approve",
                "expectRefused": "separation_of_duties_violation",
            }
        },
        {"approve": {"step": "sign", "by": "bob", "decision": "reject"}},
        {"expect": {"status": "completed", "data": {"outcome": "rejected"}}},
    ]
    result = _trial(REVIEW, live, steps)
    assert result.status == "passed", result.failures


def test_a_trial_run_needs_an_instance_of_its_process_and_nothing_else_given() -> None:
    live, _ = _live(REVIEW, until="review")
    other = dataclasses.replace(live, process="other")
    wrong = _trial(REVIEW, other, [{"expect": {"status": "running"}}])
    assert wrong.status == "failed" and "of process 'other'" in wrong.failures[0].message
    mixed = _trial(REVIEW, live, [{"expect": {"status": "running"}}], data={"amount": 1})
    assert mixed.status == "failed" and "no given.data" in mixed.failures[0].message
    missing = sb.run_test(
        world(REVIEW),
        "tests/trial.test.yaml",
        {"process": "test", "name": "t", "given": {"fromInstance": INSTANCE}, "steps": []},
    )
    assert missing.status == "failed" and "no instance" in missing.failures[0].message


# --- the retrospective of a case and the next case (SC-013) --------------------------------


LESSONS = """
memory:
  case: {key: "'case:' + data.number", title: "'Case ' + data.number"}
  entities: [{kind: legal_entity, key: data.author, name: "'Customer'", rel: customer}]
retrospective: {taskType: lessons, assign: [{role: lead}], appliesTo: [legal_entity]}
stages:
  - id: s
    steps:
      - id: history
        recall: {anchors: [{kind: legal_entity, key: data.author}], kinds: [lesson]}
        output: {as: {history: step.result.nodes}}
      - {id: finish, complete: {outcome: lost}}
"""


class MemoryStub:
    """Memory as the core writes it and a recall reads it: nodes and facts by entity keys.

    What a ``remember`` intent writes is the core's own observation of it
    (:func:`remembered`); a recall anchored on an entity finds the lessons
    that apply to it (CP-ADR-0076 §6: entity ← ``applies_to`` — lesson).
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, str]] = []

    def write(self, intent: dict[str, Any]) -> None:
        instance = SimpleNamespace(id=INSTANCE, definition_key="test")
        observation = remembered(intent, "case", instance)  # type: ignore[arg-type]
        for assertion in observation["assertions"]:
            if assertion["assert"] == "entity":
                entity = assertion["entity"]
                self.nodes.setdefault(entity["key"], {}).update(entity)
            else:
                self.facts.append(assertion["fact"])

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        anchors = {entity_key(str(a["kind"]), str(a["key"])) for a in request["anchors"]}
        found = [f for f in self.facts if f["predicate"] == "applies_to" and f["object"] in anchors]
        nodes = [
            {
                "kind": self.nodes[f["subject"]]["type"],
                "key": f["subject"],
                "text": self.nodes[f["subject"]]["properties"]["text"],
            }
            for f in found
            if self.nodes[f["subject"]]["type"] in (request.get("kinds") or ["lesson"])
        ]
        edges = [{"relation": "applies_to", "from": f["subject"], "to": f["object"]} for f in found]
        return {"nodes": nodes, "edges": edges}


class SandboxWithMemory(sb.Sandbox):
    """The sandbox whose remember and recall go to one memory, across tests."""

    memory: MemoryStub

    def do_recall(self, instance: Any, body: dict[str, Any]) -> None:
        answer = {"activityId": body["activityId"], "status": "completed"}
        self.queue.append(
            (instance.id, "recall", {**answer, "result": self.memory.answer(body)}, None)
        )

    def do_remember(self, instance: Any, body: dict[str, Any]) -> None:
        super().do_remember(instance, body)
        self.memory.write(body)


def _with_memory(
    here: sb.World, memory: MemoryStub, steps: list[dict[str, Any]], **test: Any
) -> SandboxWithMemory:
    sandbox = SandboxWithMemory(
        here, {"process": "test", "name": "t", "steps": steps, **test}, seed="t"
    )
    sandbox.memory = memory
    sandbox.start_given()
    for index, step in enumerate(steps):
        failures = sb._run_step(sandbox, index, step)
        assert not failures, [f.out() for f in failures]
    return sandbox


def test_a_lesson_of_a_closed_case_is_recalled_by_the_next_case_of_the_customer() -> None:
    contract = retrospective_contract()
    skills = {
        **CATALOG.skills,
        "process.retrospective@1": SkillEntry(contract["inputs"], contract["outputs"]),
    }
    here = dataclasses.replace(world(LESSONS), skills=skills)
    memory = MemoryStub()
    evidence = [{"seq": 1, "eventId": None}]
    proposed = {
        "case": "case:N-1",
        "lessons": [
            {
                "key": "lesson:case:N-1/late",
                "text": "The customer asks for documents late: ask a week earlier",
                "appliesTo": [{"kind": "legal_entity", "key": AUTHOR}],
                "evidence": evidence,
            },
            {
                "key": "lesson:case:N-1/noise",
                "text": "Nothing to learn",
                "appliesTo": [{"kind": "case", "key": "case:N-1"}],
                "evidence": evidence,
            },
        ],
        "dropped": [],
    }
    # The mock answers only an input by the skill's contract: the sandbox refuses others.
    mocks = {"skills": {"process.retrospective@1": [{"output": proposed}]}}
    first = _with_memory(
        here,
        memory,
        [
            opened(number="N-1"),
            {
                "expect": {
                    "status": "completed",
                    "data": {"history": []},
                    "tasks": [{"step": "retrospective", "status": "open"}],
                }
            },
        ],
        given={"principals": {"lead": ["alice"]}},
        mocks=mocks,
    )
    (review,) = [t for t in first.tasks if t.element == "retrospective"]
    lessons = [
        {**lesson, "appliesTo": [{"kind": "legal_entity", "key": AUTHOR}], "decision": decision}
        for lesson, decision in zip(proposed["lessons"], ["confirm", "reject"], strict=True)
    ]
    first.complete({"step": "retrospective", "by": "alice", "output": {"lessons": lessons}})
    assert review.status == "completed"
    (lesson,) = [r["entity"] for r in first.remembered]
    assert lesson["key"] == "lesson:case:N-1/late"
    assert lesson["links"] == [
        {"rel": "learned_from", "kind": "case", "key": "case:N-1"},
        {"rel": "applies_to", "kind": "legal_entity", "key": AUTHOR},
    ]

    second = _with_memory(
        here,
        memory,
        [opened(number="N-2"), {"expect": {"status": "completed"}}],
        given={"principals": {"lead": ["alice"]}},
    )
    (instance,) = second.instances.values()
    assert instance.state is not None
    assert instance.state["data"]["history"] == [
        {
            "kind": "lesson",
            "key": "lesson:lesson:case:N-1/late",
            "text": "The customer asks for documents late: ask a week earlier",
        }
    ]
    other = _with_memory(
        here,
        memory,
        [opened(number="N-3", author=OTHER), {"expect": {"data": {"history": []}}}],
        given={"principals": {"lead": ["alice"]}},
    )
    assert other.remembered == []


PREFILLED = """
stages:
  - id: s
    steps:
      - id: review
        human:
          taskType: review
          assign: [{role: lead}]
          customFields: {decision: "data.number + '-draft'"}
        output: {as: {decision: step.result.decision}}
"""


def test_a_human_step_fills_its_task_from_the_case_and_the_person_may_keep_it() -> None:
    """``human.customFields`` (CP-ADR-0074 §7, amendment 2026-10-01): as on the core."""
    given = {"principals": {"lead": ["alice"]}}
    kept = run(
        PREFILLED,
        [
            opened(),
            {"expect": {"tasks": [{"step": "review", "customFields": {"decision": "N-1-draft"}}]}},
            {"complete": {"step": "review", "by": "alice"}},
            {"expect": {"status": "completed", "data": {"decision": "N-1-draft"}}},
        ],
        given=given,
    )
    assert kept.status == "passed", kept.failures
    changed = run(
        PREFILLED,
        [
            opened(),
            {"complete": {"step": "review", "by": "alice", "output": {"decision": "yes"}}},
            {"expect": {"status": "completed", "data": {"decision": "yes"}}},
        ],
        given=given,
    )
    assert changed.status == "passed", changed.failures
    other = run(
        PREFILLED,
        [opened(), {"expect": {"tasks": [{"step": "review", "customFields": {"decision": "x"}}]}}],
        given=given,
    )
    assert other.status == "failed"
    assert other.failures[0].actual == [
        {
            "step": "review",
            "status": "open",
            "assignee": "role:lead",
            "due": None,
            "customFields": {"decision": "N-1-draft"},
        }
    ]


def test_fields_the_type_refuses_fail_the_step_as_on_the_core() -> None:
    narrow = {"type": "object", "properties": {"decision": {"type": "string", "maxLength": 3}}}
    task_types = {**CATALOG.task_types, "review": narrow}
    catalog = dataclasses.replace(CATALOG, task_types=task_types)
    definition = Definition.build("test", pd.normalized_spec(spec(PREFILLED)), catalog)
    narrow_world = sb.World(
        definitions={"test": definition},
        skills=catalog.skills,
        task_types=task_types,
        agents=catalog.agents,
        roles=frozenset({"lead"}),
        calendars=CALENDARS,
    )
    result = sb.run_test(
        narrow_world,
        "tests/t.test.yaml",
        {
            "process": "test",
            "name": "t",
            "given": {"principals": {"lead": ["alice"]}},
            "steps": [opened(), {"expect": {"status": "failed", "error": "intent_failed"}}],
        },
    )
    assert result.status == "passed", result.failures


# --- rules that close the task an observation is bound to (CP-ADR-0063 Zh5; I013) --------

REVIEWED = """
stages:
  - id: s
    steps:
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
      - id: done
        complete: {outcome: reviewed}
"""


def _closing(kind: str = "complete_work", **extra: Any) -> dict[str, Any]:
    return {
        "trigger": {"kind": "observation", "type": "case.closed", "agent": "observer"},
        "condition": True,
        "interpretation": None,
        "action": {"kind": kind, "target": "task", "taskTypes": ["review"], "fields": {}, **extra},
    }


def closed(task: str | None = "review", **payload: Any) -> dict[str, Any]:
    emit: dict[str, Any] = {"observation": "case.closed", "payload": payload}
    if task is not None:
        emit["task"] = task
    return {"emit": emit}


def run_rules(
    steps: list[dict[str, Any]], rules: dict[str, dict[str, Any]], body: str = REVIEWED
) -> sb.TestResult:
    return sb.run_test(
        world(body, rules=rules),
        "tests/t.test.yaml",
        {"process": "test", "name": "t", "steps": steps},
    )


def test_an_observation_closes_the_task_of_a_step_by_a_rule_of_the_package() -> None:
    steps = [
        opened(),
        {"expect": {"tasks": [{"step": "review", "status": "open"}]}},
        closed(),
        {
            "expect": {
                "status": "completed",
                "outcome": "reviewed",
                "tasks": [{"step": "review", "status": "completed"}],
                "rules": [{"rule": "close-review", "result": "matched", "step": "review"}],
            }
        },
        # Another fact about the same task: done is done.
        closed(),
        {
            "expect": {
                "rules": [{"rule": "close-review", "result": "skipped", "reason": "already_done"}]
            }
        },
    ]
    result = run_rules(steps, {"close-review": _closing()})
    assert result.status == "passed", result.failures


def test_a_rule_cancels_the_bound_task_and_a_closed_task_stays_closed() -> None:
    steps = [
        opened(),
        closed(),
        {"expect": {"tasks": [{"step": "review", "status": "cancelled"}]}},
        closed(),
        {"expect": {"rules": [{"result": "skipped", "reason": "already_closed"}]}},
    ]
    result = run_rules(steps, {"cancel-review": _closing("cancel_work")})
    assert result.status == "passed", result.failures


def test_a_rule_skips_a_type_it_does_not_list_and_an_observation_bound_to_nothing() -> None:
    rule = _closing(taskTypes=["sign-off"])
    steps = [
        opened(),
        closed(),
        closed(task=None),
        {
            "expect": {
                "tasks": [{"step": "review", "status": "open"}],
                "rules": [
                    {"result": "skipped", "reason": "bound_task_type_not_listed"},
                    {"result": "skipped", "reason": "no_bound_task"},
                ],
            }
        },
    ]
    result = run_rules(steps, {"close-review": rule})
    assert result.status == "passed", result.failures


def test_a_rule_condition_reads_the_bound_task_and_the_observation() -> None:
    rule = {
        **_closing(),
        "condition": {
            "and": [
                {"eq": [{"var": "task.typeKey"}, "review"]},
                {"eq": [{"var": "payload.state"}, "won"]},
            ]
        },
    }
    steps = [
        opened(),
        closed(state="lost"),
        {
            "expect": {
                "tasks": [{"step": "review", "status": "open"}],
                "rules": [{"result": "not_matched"}],
            }
        },
        closed(state="won"),
        {"expect": {"tasks": [{"step": "review", "status": "completed"}]}},
    ]
    result = run_rules(steps, {"close-review": rule})
    assert result.status == "passed", result.failures


def test_an_expected_rule_decision_that_was_not_taken_fails_the_test() -> None:
    steps = [
        opened(),
        closed(),
        {"expect": {"rules": [{"rule": "close-review", "result": "matched"}]}},
    ]
    result = run_rules(steps, {"close-review": _closing(taskTypes=["sign-off"])})
    assert result.status == "failed"
    assert "no decision of a rule" in result.failures[0].message


def test_emit_task_needs_an_observation_and_a_step_with_a_task() -> None:
    on_event = run_rules(
        [opened(), {"emit": {"event": "case.touched", "task": "review"}}], {"r": _closing()}
    )
    assert on_event.status == "failed" and "emit.task" in on_event.failures[0].message
    no_task = run_rules([opened(), closed(task="done")], {"r": _closing()})
    assert no_task.status == "failed" and "has no task" in no_task.failures[0].message
    interpreted = run_rules(
        [opened(), closed()],
        {"r": {**_closing(), "interpretation": {"skill": "work.do@1", "inputs": {}}}},
    )
    assert interpreted.status == "failed" and "interpretation" in interpreted.failures[0].message


def test_the_package_rules_with_target_task_are_checked_and_handed_to_the_sandbox() -> None:
    def rule(key: str, action: str) -> tuple[str, str]:
        return (
            f"rules/{key}.yaml",
            f"apiVersion: {API_VERSION}\nkind: WorkRule\nkey: {key}\nspec:\n"
            "  trigger: {kind: observation, type: case.closed, agent: observer}\n"
            f"  action: {action}\n",
        )

    package = parse_package(
        [
            ("package.yaml", f"apiVersion: {API_VERSION}\nkind: Package\nkey: p\nspec: {{}}\n"),
            rule("good", "{kind: complete_work, target: task, taskTypes: [review]}"),
            rule(
                "keyed",
                "{kind: complete_work, target: task, taskTypes: [review], dedupKeyTemplate: k}",
            ),
            rule("filing", "{kind: ensure_work, taskType: review, dedupKeyTemplate: k}"),
        ]
    )
    rules, problems = sandbox_rules(package)
    assert list(rules) == ["good"]
    assert rules["good"]["action"] == {
        "kind": "complete_work",
        "target": "task",
        "taskTypes": ["review"],
    }
    assert [(p.code, p.file, p.path, p.line) for p in problems] == [
        ("invalid_rule_action", "rules/keyed.yaml", "/spec/action/dedupKeyTemplate", 6)
    ]
