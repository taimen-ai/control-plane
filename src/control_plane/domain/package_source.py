"""A package as its files: parsed by the core, every finding placed in a file and line.

``PackageSource {files: [{path, content}]}`` (CP-ADR-0074 §10) is the package
directory as its author has it: ``package.yaml``, catalog objects in the
envelope ``{apiVersion, kind, key, spec}`` in any other ``*.yaml``, tests in
``tests/*.test.yaml``, data schemas and other files a process refers to.

- **YAML 1.2.** ``on``, ``off``, ``yes``, ``no`` are strings: the key ``on``
  of a trigger stays ``on`` (TAI-ADR-0054 p.11). package-sdk reads packages
  the same way. Integers are those of the core schema of YAML 1.2: decimal,
  ``0o`` and ``0x``; ``1:30`` (sexagesimal of YAML 1.1) is a string and
  ``012`` is twelve.
- **Places.** Each document keeps a map JSON pointer → line, so a finding of
  the check of a process (``/spec/stages/0/steps/1``) names the line of the
  file; a pointer the file does not have takes the line of its nearest parent.
- **``data: {$ref: <file>}``** of a process is inlined from the package
  (JSON or YAML, a path relative to the process file, never outside the
  package), as package-sdk does before it publishes.
- **Tests** are checked against ``schema/v1/test.schema.json`` of package-sdk;
  the copy the core holds (``package_test.schema.json``) is kept equal to it
  by a contract test. A test has a subject (CP-ADR-0074 Z1): a process (the
  default), a ``WorkRule`` or a ``TaskType`` of the package.

What is found here is a finding like those of the check of a definition
(:class:`Problem`): ``invalid_yaml``, ``invalid_document``, ``unknown_kind``,
``duplicate_object``, ``unresolved_data_ref``, ``invalid_test``,
``unknown_test_process``, ``unknown_test_rule``, ``unknown_test_task_type``;
the warning ``test_field_ignored`` names a field of a rule or task type test
the core does not run.

- **Dictionaries** ``i18n/<locale>.yaml`` (CP-ADR-0080): a flat mapping of
  message keys to texts, not catalog objects; ``invalid_dictionary`` (a path or
  a document that is no dictionary, a second file of a locale) and
  ``invalid_message`` (a key, a text that is no string, unpaired braces).

Pure functions over plain values; no I/O.
"""

import json
import math
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema import Draft202012Validator

from control_plane.domain.process_definition import Problem, pointer

# The version of the catalog format the core reads: ``apiVersion`` is
# ``<catalog>/v1``; which catalog is the package tool's check (package-sdk).
FORMAT_VERSION = "v1"
# Kinds of the catalog schema (package-sdk schema/v1/object.schema.json).
KINDS = (
    "Package",
    "Installation",
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "ConnectionType",
    "Skill",
    "WorkRule",
    "Agent",
    "NotificationRule",
    "Process",
    "Calendar",
    # Screens of a package (TAI-ADR-0066, CP-ADR-0080).
    "View",
    "Component",
)
# Kinds whose key is a path segment of the core's routes.
SLUG_KINDS = ("Process", "Calendar", "View", "Component")
MANIFEST = "package.yaml"
TESTS_DIR = "tests"
TEST_SUFFIXES = (".test.yaml", ".test.yml")
# Directories that hold no catalog objects: schemas a process refers to, the
# layout of the visual editor.
RESOURCE_DIRS = ("schemas", ".layout")
# Dictionaries of the package: ``i18n/<locale>.yaml``, flat key -> text (TAI-ADR-0066 p.1a).
I18N_DIR = "i18n"
LOCALE_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
YAML_SUFFIXES = (".yaml", ".yml")
TEST_SCHEMA_FILE = Path(__file__).with_name("package_test.schema.json")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_TEST_PROBLEMS = 20
# What a test is about (``subject``), the field naming the object, its kind.
SUBJECT_PROCESS = "process"
SUBJECT_RULE = "rule"
SUBJECT_TASK_TYPE = "taskType"
SUBJECTS: dict[str, tuple[str, str]] = {
    SUBJECT_PROCESS: ("process", "Process"),
    SUBJECT_RULE: ("rule", "WorkRule"),
    SUBJECT_TASK_TYPE: ("taskType", "TaskType"),
}
_UNKNOWN_TEST_OBJECT = {
    SUBJECT_PROCESS: ("unknown_test_process", "process", "processes"),
    SUBJECT_RULE: ("unknown_test_rule", "rule", "rules"),
    SUBJECT_TASK_TYPE: ("unknown_test_task_type", "task type", "task types"),
}
# Fields schema v1 takes in a rule or task type test that only a process test runs.
PROCESS_ONLY_FIELDS = (("version",), ("mocks", "agents"), ("mocks", "recall"))

Locate = Callable[[str], int | None]


# --- YAML 1.2 with places -----------------------------------------------------------------


# Digits of an integer literal: far above any number of a package, far below
# what makes int() quadratic or JSON unable to write it (4300 decimal digits).
MAX_INT_DIGITS = 1000
INT_TOO_LONG_MESSAGE = f"an integer has more than {MAX_INT_DIGITS} digits"
# The booleans and integers of the core schema of YAML 1.2.
_BOOLS = {
    word: word.lower() == "true" for word in ("true", "True", "TRUE", "false", "False", "FALSE")
}
_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")


def _shown(value: str) -> str:
    """A scalar as a message names it: a long one cut."""
    return value if len(value) <= 40 else f"{value[:40]}..."


def _parse_int(text: str) -> int:
    """An integer of the core schema of YAML 1.2 (those of JSON are among them).

    Raises :class:`ValueError` for another form (``1:30``, ``1_000``, ``0b1``)
    and for more than :data:`MAX_INT_DIGITS` digits, before ``int()`` is paid.
    """
    if not _INT.match(text):
        raise ValueError(f"{_shown(text)!r} is not an integer of YAML 1.2")
    base = {"0o": 8, "0x": 16}.get(text[:2], 10)
    digits = text.lstrip("+-") if base == 10 else text[2:]
    if len(digits) > MAX_INT_DIGITS:
        raise ValueError(INT_TOO_LONG_MESSAGE)
    return -int(digits, base) if text.startswith("-") else int(digits, base)


def _unreadable(node: yaml.Node, message: str) -> yaml.constructor.ConstructorError:
    """A scalar its tag cannot read, at its line."""
    return yaml.constructor.ConstructorError(None, None, message, node.start_mark)


@cache
def yaml12_loader() -> type[yaml.SafeLoader]:
    """SafeLoader with the booleans and integers of YAML 1.2 only.

    Booleans are ``true`` and ``false``; integers are decimal, ``0o`` and
    ``0x``, at most :data:`MAX_INT_DIGITS` digits: sexagesimal ``1:59:59`` of
    YAML 1.1 is a string (a million characters of it cost ``int()`` half a
    minute), and so are ``0b1`` and ``1_000``; ``012`` is twelve. A float JSON
    has not (``.nan``, ``1e999``, ``!!float inf``) is refused. YAML 1.2 has no
    timestamps either: ``2026-09-30`` is a string, as JSON (the stored spec, a
    date input of a decision table) holds a date. A scalar its tag cannot read
    (``!!int x``, ``!!bool yes``) is an error at its line.
    """

    class Loader(yaml.SafeLoader):
        def construct_yaml_bool(self, node: yaml.ScalarNode) -> bool:
            value = self.construct_scalar(node)
            if value not in _BOOLS:
                raise _unreadable(node, f"{_shown(value)!r} is not a boolean of YAML 1.2")
            return _BOOLS[value]

        def construct_yaml_int(self, node: yaml.ScalarNode) -> int:
            try:
                return _parse_int(self.construct_scalar(node))
            except ValueError as exc:
                raise _unreadable(node, str(exc)) from None

        def construct_yaml_float(self, node: yaml.ScalarNode) -> float:
            try:
                value = super().construct_yaml_float(node)
            except (ValueError, IndexError):
                # float() of a word, or PyYAML on an empty !!float.
                raise _unreadable(node, f"{_shown(node.value)!r} is not a float") from None
            except OverflowError:
                # Sexagesimal of YAML 1.1 past the floats: 60 ** n as an int.
                value = math.inf
            if not math.isfinite(value):
                raise _unreadable(node, NOT_JSON_MESSAGE.format(_shown(node.value)))
            return value

    Loader.add_constructor("tag:yaml.org,2002:bool", Loader.construct_yaml_bool)
    Loader.add_constructor("tag:yaml.org,2002:int", Loader.construct_yaml_int)
    Loader.add_constructor("tag:yaml.org,2002:float", Loader.construct_yaml_float)
    dropped = (
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:timestamp",
    )
    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag not in dropped]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(f"^(?:{'|'.join(_BOOLS)})$"), list("tTfF")
    )
    Loader.add_implicit_resolver("tag:yaml.org,2002:int", _INT, list("-+0123456789"))
    return Loader


class SourceError(ValueError):
    """A file is not YAML (or JSON); ``line`` is 1-based when known."""

    def __init__(self, message: str, line: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


# Nodes a document may hold with every alias expanded: far above a package file,
# far below what an alias bomb (ten aliases of the level below, level on level)
# makes of a few hundred bytes.
MAX_NODES = 100_000
# Characters of the pointers of those nodes: a long key over many nodes (or
# repeated by an alias) would make gigabytes of pointers from a small file.
MAX_POINTER_CHARS = 20_000_000
# Characters of the scalars of a document with its aliases expanded, as JSON
# (the canonical hash, the stored spec) will write them: a long string an alias
# repeats is the bomb nodes do not count. A file (at most a million characters
# by the API) holds fewer without aliases, so only an expansion reaches it.
MAX_SCALAR_CHARS = 4_000_000
# What all the files of one package may hold together, a file each time a
# ``$ref`` brings it: a bomb spread over many files, each under the limits of a
# file. The largest package of the superproject holds 13.8 thousand
# nodes and 103 thousand characters of strings.
MAX_PACKAGE_NODES = 200_000
MAX_PACKAGE_POINTER_CHARS = 20_000_000
MAX_PACKAGE_SCALAR_CHARS = 4_000_000
# Characters of the files read: parsing costs by the text, comments and blanks
# included, and a file a ``$ref`` names is read once per process naming it.
MAX_PACKAGE_SOURCE_CHARS = 4_000_000
# Nesting of a document; deeper, the parser runs out of stack.
MAX_DEPTH = 100
_SURROGATE = re.compile("[\ud800-\udfff]")
SURROGATE_MESSAGE = "a string holds a lone surrogate (\\ud800-\\udfff): not Unicode text"
TOO_DEEP_MESSAGE = f"the document is nested deeper than {MAX_DEPTH} levels"
TOO_LARGE_MESSAGE = (
    f"the document has more than {MAX_NODES} nodes (or {MAX_POINTER_CHARS} characters"
    " of their pointers) with its aliases expanded"
)
TOO_LONG_MESSAGE = (
    f"the strings of the document have more than {MAX_SCALAR_CHARS} characters"
    " with its aliases expanded"
)
PACKAGE_TOO_LARGE_MESSAGE = (
    f"the files of the package have more than {MAX_PACKAGE_NODES} nodes,"
    f" {MAX_PACKAGE_POINTER_CHARS} characters of their pointers,"
    f" {MAX_PACKAGE_SCALAR_CHARS} characters of strings or {MAX_PACKAGE_SOURCE_CHARS}"
    " characters of text, aliases expanded and a file counted each time a $ref names it:"
    " the package is refused from this file on"
)
NOT_JSON_MESSAGE = "a value is {!r}: the package holds JSON values only"
UNREADABLE_MESSAGE = "a scalar is not a value of its type"
# Tags that make a JSON value; ``!!binary``, ``!!timestamp``, ``!!set``,
# ``!!omap`` and the like make what JSON cannot write.
JSON_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}"
    for name in ("null", "bool", "int", "float", "str", "seq", "map", "merge")
)


@dataclass
class Budget:
    """What one :func:`parse_package` has left to spend over its files, ``$ref`` included."""

    nodes: int = MAX_PACKAGE_NODES
    pointer_chars: int = MAX_PACKAGE_POINTER_CHARS
    scalar_chars: int = MAX_PACKAGE_SCALAR_CHARS
    source_chars: int = MAX_PACKAGE_SOURCE_CHARS

    def read(self, text: str) -> None:
        """Charge a file before it is parsed: its text is the cost of the parser."""
        self.source_chars -= len(text)
        if self.source_chars < 0:
            raise SourceError(PACKAGE_TOO_LARGE_MESSAGE, None)


def _within(spent: int, limit: int, left: int, message: str, line: int | None) -> None:
    """``spent`` of a file against the limit of a file and what the package has left."""
    if spent > limit:
        raise SourceError(message, line)
    if spent > left:
        raise SourceError(PACKAGE_TOO_LARGE_MESSAGE, line)


def _lines(root: yaml.Node, out: dict[str, int], budget: Budget) -> None:
    """Lines by pointer, walking aliases as the document will read them.

    Every node counts each time an alias brings it back, so an alias bomb or a
    node that holds itself stops at :data:`MAX_NODES` (or at the limit of
    pointers, of expanded strings or of depth), and what the file spent,
    refused or not, is taken from ``budget``. A scalar with a lone surrogate
    (``"\\ud800"``) or a tag that makes no JSON value (``!!binary``) is
    refused; each node is searched once, however many aliases bring it back.
    A float JSON has not (``.nan``) is refused as the document is built.
    """
    stack: list[tuple[yaml.Node, str, int]] = [(root, "", 0)]
    nodes = chars = scalar_chars = 0
    searched: set[int] = set()

    def child(node: yaml.Node, path: str, name: str, depth: int) -> None:
        # Charged before the pointer is made: a long one times many is the cost.
        nonlocal chars
        chars += len(path) + len(name)
        line = node.start_mark.line + 1
        _within(chars, MAX_POINTER_CHARS, budget.pointer_chars, TOO_LARGE_MESSAGE, line)
        stack.append((node, path + name, depth))

    try:
        while stack:
            node, path, depth = stack.pop()
            line = node.start_mark.line + 1
            nodes += 1
            _within(nodes, MAX_NODES, budget.nodes, TOO_LARGE_MESSAGE, line)
            if depth > MAX_DEPTH:
                raise SourceError(TOO_DEEP_MESSAGE, line)
            out.setdefault(path, line)
            if id(node) not in searched:
                searched.add(id(node))
                _json_node(node, line)
            if isinstance(node, yaml.ScalarNode):
                scalar_chars += len(node.value)
                _within(scalar_chars, MAX_SCALAR_CHARS, budget.scalar_chars, TOO_LONG_MESSAGE, line)
            elif isinstance(node, yaml.MappingNode):
                for key, value in reversed(node.value):
                    name = pointer(str(key.value)) if isinstance(key, yaml.ScalarNode) else "/?"
                    child(value, path, name, depth + 1)
                    child(key, path, name, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for index in reversed(range(len(node.value))):
                    child(node.value[index], path, f"/{index}", depth + 1)
    finally:
        budget.nodes -= nodes
        budget.pointer_chars -= chars
        budget.scalar_chars -= scalar_chars


def _json_node(node: yaml.Node, line: int) -> None:
    """Refuse a node that makes no JSON value, or a string JSON and the database refuse."""
    if node.tag not in JSON_TAGS:
        raise SourceError(NOT_JSON_MESSAGE.format(node.tag), line)
    if not isinstance(node, yaml.ScalarNode):
        return
    if _SURROGATE.search(node.value):
        raise SourceError(SURROGATE_MESSAGE, line)


def load_yaml(text: str, budget: Budget | None = None) -> tuple[Any, dict[str, int]]:
    """The document and its lines by JSON pointer; an empty file is ``None``.

    Refused as not YAML: more than :data:`MAX_NODES` nodes or
    :data:`MAX_SCALAR_CHARS` characters of strings with aliases expanded (or
    more than ``budget``, the package, has left), nesting deeper than
    :data:`MAX_DEPTH`, a string with a lone surrogate, a value JSON has not
    (``!!binary``, ``.nan``, ``1e999``), an integer longer than
    :data:`MAX_INT_DIGITS` digits, a scalar its tag cannot read (``!!int x``),
    a character YAML does not allow (``\\x00``).
    """
    budget = budget if budget is not None else Budget()
    budget.read(text)
    loader: yaml.SafeLoader | None = None
    try:
        # The reader checks the characters of the text as it is made.
        loader = yaml12_loader()(text)
        node = loader.get_single_node()
        if node is None:
            return None, {}
        lines: dict[str, int] = {}
        _lines(node, lines, budget)
        return loader.construct_document(node), lines
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(str(part) for part in (exc.context, exc.problem) if part)
        raise SourceError(message or "not YAML", mark.line + 1 if mark else None) from None
    except yaml.reader.ReaderError as exc:
        message = f"unacceptable character #x{exc.character:04x}: {exc.reason}"
        raise SourceError(message, text.count("\n", 0, exc.position) + 1) from None
    except yaml.YAMLError as exc:
        raise SourceError(str(exc), None) from None
    except RecursionError:
        # The composer recurses per level: a document nested past the stack.
        raise SourceError(TOO_DEEP_MESSAGE, None) from None
    except SourceError:
        raise
    except ValueError:
        # A constructor of PyYAML the loader does not replace, reading a scalar.
        raise SourceError(UNREADABLE_MESSAGE, None) from None
    finally:
        if loader is not None:
            loader.dispose()


def _json_spend(document: Any, budget: Budget) -> None:
    """The nesting, nodes and strings of a JSON document, held as :func:`_lines` holds YAML.

    Its root is level 0; keys and strings count as strings; what it spent,
    refused or not, is taken from ``budget``.
    """
    nodes = scalar_chars = 0
    stack: list[tuple[Any, int]] = [(document, 0)]
    try:
        while stack:
            value, depth = stack.pop()
            nodes += 1
            _within(nodes, MAX_NODES, budget.nodes, TOO_LARGE_MESSAGE, None)
            if depth > MAX_DEPTH:
                raise SourceError(TOO_DEEP_MESSAGE, None)
            if isinstance(value, dict):
                scalar_chars += sum(len(key) for key in value)
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
            elif isinstance(value, str):
                scalar_chars += len(value)
            _within(scalar_chars, MAX_SCALAR_CHARS, budget.scalar_chars, TOO_LONG_MESSAGE, None)
    finally:
        budget.nodes -= nodes
        budget.scalar_chars -= scalar_chars


def _not_finite(constant: str) -> Any:
    raise SourceError(NOT_JSON_MESSAGE.format(constant), None)


def _json_int(text: str) -> int:
    try:
        return _parse_int(text)
    except ValueError as exc:
        raise SourceError(str(exc), None) from None


def _json_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise SourceError(NOT_JSON_MESSAGE.format(_shown(text)), None)
    return value


def load_file(path: str, text: str, budget: Budget | None = None) -> tuple[Any, dict[str, int]]:
    """A YAML or JSON file of the package (JSON is YAML, but its errors read better).

    JSON has no aliases: its size is that of the file; its nesting, nodes and
    strings are held to what :func:`load_yaml` holds a YAML file to, and
    ``NaN``/``Infinity`` and ``1e999`` (which Python reads, JSON has not) are
    refused, as is an integer longer than :data:`MAX_INT_DIGITS` digits.
    """
    if not path.endswith(".json"):
        return load_yaml(text, budget)
    budget = budget if budget is not None else Budget()
    budget.read(text)
    try:
        document = json.loads(
            text, parse_constant=_not_finite, parse_int=_json_int, parse_float=_json_float
        )
    except SourceError:
        raise
    except json.JSONDecodeError as exc:
        raise SourceError(exc.msg, exc.lineno) from None
    except RecursionError:
        raise SourceError(TOO_DEEP_MESSAGE, None) from None
    except ValueError:
        raise SourceError(UNREADABLE_MESSAGE, None) from None
    _json_spend(document, budget)
    try:
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise SourceError(SURROGATE_MESSAGE, None) from None
    return document, {}


def locator(lines: Mapping[str, int]) -> Locate:
    return lambda path: lines.get(path)


# --- the package -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageObject:
    """A catalog object of the package: its envelope unwrapped."""

    kind: str
    key: str
    spec: dict[str, Any]
    file: str
    lines: Mapping[str, int] = field(default_factory=dict, compare=False)

    @property
    def locate(self) -> Locate:
        return locator(self.lines)

    @property
    def ref(self) -> str:
        """``kind/key``; a skill is ``Skill/<name>@<version>``: its versions are objects."""
        if self.kind == "Skill":
            return f"Skill/{self.key}@{self.spec.get('version')}"
        return f"{self.kind}/{self.key}"

    def place(self, problem: Problem) -> Problem:
        return placed(problem, self.file, self.locate)


@dataclass(frozen=True)
class PackageTestFile:
    """A test of the package (``tests/<name>.test.yaml``), checked against the test schema."""

    file: str
    data: dict[str, Any]
    lines: Mapping[str, int] = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.file)

    @property
    def subject(self) -> str:
        """``process`` (the default), ``rule`` or ``taskType``."""
        return str(self.data.get("subject") or SUBJECT_PROCESS)

    @property
    def object(self) -> str:
        """The key of the process, rule or task type under test."""
        return str(self.data.get(SUBJECTS[self.subject][0]) or "")

    @property
    def process(self) -> str:
        """The key of the process under test; empty for a rule or task type test."""
        return self.object if self.subject == SUBJECT_PROCESS else ""

    def locate(self, path: str) -> int | None:
        return self.lines.get(path)


@dataclass(frozen=True)
class Dictionary:
    """``i18n/<locale>.yaml``: the texts of one language by key."""

    locale: str
    file: str
    messages: dict[str, str]
    lines: Mapping[str, int] = field(default_factory=dict, compare=False)

    def locate(self, path: str) -> int | None:
        return self.lines.get(path)


@dataclass
class ParsedPackage:
    manifest: dict[str, Any] | None = None
    # The Package object of package.yaml: its key and places (renames).
    manifest_object: PackageObject | None = None
    objects: list[PackageObject] = field(default_factory=list)
    tests: list[PackageTestFile] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # The dictionaries by locale, as the files are named.
    dictionaries: dict[str, Dictionary] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[PackageObject]:
        return [obj for obj in self.objects if obj.kind == kind]

    def process(self, key: str) -> PackageObject | None:
        return next((obj for obj in self.of_kind("Process") if obj.key == key), None)


def placed(problem: Problem, file: str | None, locate: Locate | None) -> Problem:
    """``problem`` in ``file`` at the line of its path, or of the nearest parent the file has."""
    line = problem.line
    if line is None and locate is not None:
        path = problem.path
        while True:
            line = locate(path)
            if line is not None or not path:
                break
            path = path.rsplit("/", 1)[0]
    return Problem(
        problem.code, problem.severity, problem.path, problem.message, problem.hint, file, line
    )


def _error(
    code: str, path: str, message: str, file: str, line: int | None, hint: str | None = None
) -> Problem:
    return Problem(code, "error", path, message, hint, file, line)


def _is_test(path: str) -> bool:
    return path.split("/", 1)[0] == TESTS_DIR and path.endswith(TEST_SUFFIXES)


def _is_dictionary(path: str) -> bool:
    return path.split("/", 1)[0] == I18N_DIR and path.endswith(YAML_SUFFIXES)


def _is_object(path: str) -> bool:
    first = path.split("/", 1)[0]
    return (
        path.endswith(YAML_SUFFIXES)
        and path != MANIFEST
        and first not in RESOURCE_DIRS
        and first not in (TESTS_DIR, I18N_DIR)
    )


def parse_package(files: Iterable[tuple[str, str]]) -> ParsedPackage:
    """The objects and tests of a package from its files ``(path, content)``, with findings."""
    contents = dict(files)
    package = ParsedPackage()
    seen: dict[str, str] = {}
    budget = Budget()
    for path in sorted(contents):
        if not (path == MANIFEST or _is_object(path) or _is_test(path) or _is_dictionary(path)):
            continue
        try:
            document, lines = load_yaml(contents[path], budget)
        except SourceError as exc:
            package.problems.append(_error("invalid_yaml", "", exc.message, path, exc.line))
            continue
        if _is_test(path):
            _add_test(package, path, document, lines)
            continue
        if _is_dictionary(path):
            _add_dictionary(package, path, document, lines)
            continue
        obj = _envelope(package, path, document, lines)
        if obj is None:
            continue
        if path == MANIFEST:
            if obj.kind != "Package":
                package.problems.append(
                    _error(
                        "invalid_document",
                        "/kind",
                        f"{MANIFEST} holds the object of kind Package, not {obj.kind}",
                        path,
                        lines.get("/kind"),
                    )
                )
            else:
                package.manifest = obj.spec
                package.manifest_object = obj
            continue
        if obj.kind == "Process":
            obj = _inline_data(package, obj, contents, budget)
        if obj.kind == "Component":
            obj = _inline_param_schemas(package, obj, contents, budget)
        if obj.ref in seen:
            package.problems.append(
                _error(
                    "duplicate_object",
                    "/key",
                    f"{obj.ref} is also defined in {seen[obj.ref]}",
                    path,
                    lines.get("/key"),
                )
            )
            continue
        seen[obj.ref] = path
        package.objects.append(obj)
    for test in package.tests:
        field_name, kind = SUBJECTS[test.subject]
        known = {obj.key for obj in package.of_kind(kind)}
        if test.object not in known:
            code, noun, plural = _UNKNOWN_TEST_OBJECT[test.subject]
            package.problems.append(
                _error(
                    code,
                    f"/{field_name}",
                    f"the package has no {noun} {test.object!r}",
                    test.file,
                    test.locate(f"/{field_name}"),
                    hint=_known(plural, known),
                )
            )
        if test.subject != SUBJECT_PROCESS:
            package.problems.extend(_ignored_fields(test))
    return package


def _known(plural: str, names: Iterable[str]) -> str | None:
    listed = sorted(names)
    return f"{plural} of the package: {', '.join(listed)}" if listed else None


def _ignored_fields(test: PackageTestFile) -> list[Problem]:
    """``test_field_ignored``: a field of schema v1 a rule or task type test does not run."""
    problems = []
    for parts in PROCESS_ONLY_FIELDS:
        node: Any = test.data
        for part in parts:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            continue
        where = pointer(*parts)
        problems.append(
            Problem(
                "test_field_ignored",
                "warning",
                where,
                f"{'.'.join(parts)} is not run in a test of subject {test.subject}",
                hint="the field takes effect only for subject: process",
                file=test.file,
                line=test.locate(where),
            )
        )
    return problems


def _envelope(
    package: ParsedPackage, path: str, document: Any, lines: dict[str, int]
) -> PackageObject | None:
    def refuse(where: str, message: str, hint: str | None = None) -> None:
        package.problems.append(
            _error("invalid_document", where, message, path, lines.get(where, 1), hint)
        )

    if not isinstance(document, dict):
        refuse("", "a catalog object is a mapping {apiVersion, kind, key, spec}")
        return None
    api_version = document.get("apiVersion")
    if not isinstance(api_version, str) or api_version.rsplit("/", 1)[-1] != FORMAT_VERSION:
        refuse("/apiVersion", f"apiVersion names version {FORMAT_VERSION} of the catalog format")
        return None
    kind, key, spec = document.get("kind"), document.get("key"), document.get("spec")
    if kind not in KINDS:
        package.problems.append(
            _error(
                "unknown_kind",
                "/kind",
                f"unknown kind {kind!r}",
                path,
                lines.get("/kind"),
                hint=f"one of {', '.join(KINDS)}",
            )
        )
        return None
    if not isinstance(key, str) or not key:
        refuse("/key", "key is a non-empty string")
        return None
    if kind in SLUG_KINDS and not KEY_PATTERN.match(key):
        refuse("/key", f"the key of a {kind} matches {KEY_PATTERN.pattern}")
        return None
    if not isinstance(spec, dict):
        refuse("/spec", "spec is a mapping")
        return None
    return PackageObject(str(kind), key, spec, path, lines)


def _inline_data(
    package: ParsedPackage, obj: PackageObject, contents: Mapping[str, str], budget: Budget
) -> PackageObject:
    data = obj.spec.get("data")
    if not (isinstance(data, dict) and set(data) == {"$ref"} and isinstance(data["$ref"], str)):
        return obj
    ref = data["$ref"]
    if ref.startswith("#") or "://" in ref:
        return obj  # inside the document or remote: the check of the definition refuses it
    where = "/spec/data/$ref"

    def refuse(message: str) -> PackageObject:
        package.problems.append(
            _error(
                "unresolved_data_ref",
                where,
                message,
                obj.file,
                obj.lines.get("/spec/data/$ref") or obj.lines.get("/spec/data"),
                hint="a path relative to the process file, inside the package",
            )
        )
        return obj

    target = posixpath.normpath(posixpath.join(posixpath.dirname(obj.file), ref))
    if target.startswith("../") or target == ".." or target.startswith("/"):
        return refuse(f"$ref {ref!r} leads outside the package")
    if target not in contents:
        return refuse(f"$ref {ref!r}: the package has no file {target}")
    try:
        schema, _ = load_file(target, contents[target], budget)
    except SourceError as exc:
        return refuse(f"$ref {ref!r}: {target} is not YAML or JSON: {exc.message}")
    if not isinstance(schema, dict):
        return refuse(f"$ref {ref!r}: {target} is not a JSON Schema object")
    return PackageObject(obj.kind, obj.key, {**obj.spec, "data": schema}, obj.file, obj.lines)


def _inline_param_schemas(
    package: ParsedPackage, obj: PackageObject, contents: Mapping[str, str], budget: Budget
) -> PackageObject:
    """``params.<name>.schema: {$ref: <file>#<pointer>}`` of a component, read from the package.

    TAI-ADR-0066 p.7.1: a param of a component is typed by a part of a data
    schema of the package (``../schemas/case.yaml#/properties/party``).
    The part is inlined with the ``$defs`` of its file, so its local ``$ref``
    still resolve; a reference that leads nowhere is ``unresolved_schema_ref``.
    """
    params = obj.spec.get("params")
    if not isinstance(params, dict):
        return obj
    inlined: dict[str, Any] = {}
    for name, param in params.items():
        schema = param.get("schema") if isinstance(param, dict) else None
        ref = schema.get("$ref") if isinstance(schema, dict) and set(schema) == {"$ref"} else None
        if not isinstance(ref, str) or ref.startswith("#") or "://" in ref:
            continue  # inline or remote: the check of the component refuses what it cannot type
        where = f"/spec/params/{name}/schema/$ref"
        resolved = _schema_at(obj.file, ref, contents, budget)
        if isinstance(resolved, str):
            package.problems.append(
                _error(
                    "unresolved_schema_ref",
                    where,
                    resolved,
                    obj.file,
                    obj.lines.get(where) or obj.lines.get(f"/spec/params/{name}"),
                    hint="<file relative to the component>#<JSON pointer>, inside the package",
                )
            )
            continue
        inlined[name] = {**param, "schema": resolved}
    if not inlined:
        return obj
    spec = {**obj.spec, "params": {**params, **inlined}}
    return PackageObject(obj.kind, obj.key, spec, obj.file, obj.lines)


def _schema_at(
    base: str, ref: str, contents: Mapping[str, str], budget: Budget
) -> dict[str, Any] | str:
    """The JSON Schema ``ref`` names from the file ``base``, or why it cannot be read."""
    file, _, fragment = ref.partition("#")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), file))
    if target.startswith("../") or target == ".." or target.startswith("/"):
        return f"$ref {ref!r} leads outside the package"
    if target not in contents:
        return f"$ref {ref!r}: the package has no file {target}"
    try:
        root, _ = load_file(target, contents[target], budget)
    except SourceError as exc:
        return f"$ref {ref!r}: {target} is not YAML or JSON: {exc.message}"
    node: Any = root
    for part in [p for p in fragment.split("/") if p] if fragment else ():
        part = part.replace("~1", "/").replace("~0", "~")
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return f"$ref {ref!r}: {target} has no JSON Schema object at {fragment or '/'}"
    if node is root:
        return dict(node)
    # The local references of the part point into its file: keep what they name.
    kept = {k: root[k] for k in ("$defs", "definitions") if isinstance(root.get(k), dict)}
    return {**kept, **node}


# --- dictionaries ---------------------------------------------------------------------------

MESSAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
MAX_MESSAGE_CHARS = 2000
MAX_DICTIONARY_PROBLEMS = 20


def message_syntax(text: str) -> str | None:
    """What is wrong with the ICU text ``text``, or ``None``.

    The core does not format messages (the console does); it checks that the
    braces of the arguments pair up, quoting by ``'`` as ICU quotes them.
    """
    depth = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char == "{":
            depth += 1
        elif not quoted and char == "}":
            depth -= 1
            if depth < 0:
                return "a '}' closes no argument"
        index += 1
    if depth:
        return "an argument '{' is not closed"
    return None


def _add_dictionary(
    package: ParsedPackage, path: str, document: Any, lines: dict[str, int]
) -> None:
    """``i18n/<locale>.yaml``: a flat mapping of message keys to texts."""
    name = posixpath.basename(path)
    locale = name.rsplit(".", 1)[0]
    if "/" in path[len(I18N_DIR) + 1 :] or not LOCALE_PATTERN.match(locale):
        package.problems.append(
            _error(
                "invalid_dictionary",
                "",
                f"a dictionary is {I18N_DIR}/<locale>.yaml with a locale such as en or pt-BR,"
                f" not {path}",
                path,
                1,
            )
        )
        return
    if locale in package.dictionaries:
        package.problems.append(
            _error(
                "invalid_dictionary",
                "",
                f"locale {locale} also has {package.dictionaries[locale].file}",
                path,
                1,
            )
        )
        return
    if document is None:
        document = {}
    if not isinstance(document, dict):
        package.problems.append(
            _error("invalid_dictionary", "", "a dictionary is a mapping key -> text", path, 1)
        )
        return
    messages: dict[str, str] = {}
    found = 0
    for key, text in document.items():
        where = pointer(str(key))
        problem: str | None = None
        if not isinstance(key, str) or not MESSAGE_KEY_PATTERN.match(key):
            problem = f"key {_shown(str(key))!r} does not match {MESSAGE_KEY_PATTERN.pattern}"
        elif not isinstance(text, str):
            problem = f"the text of {key} is a string, not {type(text).__name__}"
        elif len(text) > MAX_MESSAGE_CHARS:
            problem = f"the text of {key} is longer than {MAX_MESSAGE_CHARS} characters"
        else:
            problem = message_syntax(text)
            if problem is not None:
                problem = f"the text of {key}: {problem}"
        if problem is None:
            messages[key] = str(text)
            continue
        found += 1
        if found <= MAX_DICTIONARY_PROBLEMS:
            package.problems.append(
                _error("invalid_message", where, problem, path, lines.get(where, 1))
            )
    package.dictionaries[locale] = Dictionary(locale, path, messages, lines)


# --- tests ----------------------------------------------------------------------------------


@cache
def package_test_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(TEST_SCHEMA_FILE.read_text(encoding="utf-8"))
    return schema


@cache
def _test_validator() -> Draft202012Validator:
    return Draft202012Validator(
        package_test_schema(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    if not error.context:
        return error
    best = jsonschema.exceptions.best_match(error.context)
    return _deepest(best) if len(best.absolute_path) >= len(error.absolute_path) else error


def _add_test(package: ParsedPackage, path: str, document: Any, lines: dict[str, int]) -> None:
    errors = sorted(_test_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    for error in errors[:MAX_TEST_PROBLEMS]:
        cause = _deepest(error)
        where = pointer(*cause.absolute_path)
        package.problems.append(
            placed(
                Problem("invalid_test", "error", where, cause.message[:500]),
                path,
                locator(lines),
            )
        )
    if errors:
        return
    package.tests.append(PackageTestFile(path, document, lines))


def select_tests(
    package: ParsedPackage, wanted: Sequence[str] | None
) -> tuple[list[PackageTestFile], list[Problem]]:
    """The tests the request names (all by default); a named file that is no test is a finding."""
    if wanted is None:
        return list(package.tests), []
    by_file = {test.file: test for test in package.tests}
    chosen: list[PackageTestFile] = []
    problems: list[Problem] = []
    for index, name in enumerate(dict.fromkeys(wanted)):
        test = by_file.get(name)
        if test is not None:
            chosen.append(test)
            continue
        if not any(problem.file == name for problem in package.problems):
            problems.append(
                Problem(
                    "unknown_test",
                    "error",
                    pointer("tests", index),
                    f"the package has no test file {name}",
                    hint=f"tests are files {TESTS_DIR}/<name>.test.yaml",
                )
            )
    return chosen, problems
