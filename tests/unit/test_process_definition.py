"""The check of a process definition (CP-ADR-0074 §1, §2; process-packages P006).

Negative fixtures (``tests/fixtures/processes/invalid/``) each break one thing
in a small valid process and name, in their ``# expect:`` lines, the finding
the check must give — its code and the JSON pointer into the object. The
check must give those and no other error, so that a fixture pins one class.
"""

import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.domain import process_definition as pd
from control_plane.domain.process_definition import Catalog, SkillEntry
from tests.unit.test_process_contract import PINNED, PROCESS, _yaml12_loader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
INVALID = sorted((FIXTURES / "invalid").glob("*.process.yaml"))
EXPECT = re.compile(r"^# expect: (\S+) (\S+)$", re.MULTILINE)

CATALOG = Catalog(
    skills={
        "text.summarize@1": SkillEntry(
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            {"type": "object", "properties": {"summary": {"type": "string"}}},
        ),
    },
    task_types={"review": {"type": "object", "properties": {"decision": {"type": "string"}}}},
    agents=frozenset({"sample-process"}),
    calendars=frozenset({"ru"}),
    artifact_types=frozenset(),
    processes=frozenset(),
)


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.load(path.read_text("utf-8"), Loader=_yaml12_loader())
    return document


SAMPLE = _load(FIXTURES / "sample.process.yaml")


def _check(
    spec: dict[str, Any], catalog: Catalog = CATALOG, key: str = "sample"
) -> pd.CheckedProcess:
    return pd.check_process(key, pd.normalized_spec(spec), catalog)


def _codes(checked: pd.CheckedProcess) -> set[tuple[str, str]]:
    return {(p.code, p.path) for p in checked.problems}


def _sample(**changes: Any) -> dict[str, Any]:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec.update(changes)
    return spec


def test_the_sample_passes_without_findings() -> None:
    checked = _check(SAMPLE["spec"])
    assert checked.problems == ()
    assert checked.governed_by == ("regulation:sample",)


@pytest.mark.parametrize("path", INVALID, ids=[p.name.split(".")[0] for p in INVALID])
def test_a_negative_fixture_gives_its_code_and_path(path: Path) -> None:
    text = path.read_text("utf-8")
    expected = set(EXPECT.findall(text))
    assert expected, f"{path.name} names no expected finding"
    document = _load(path)
    checked = _check(document["spec"], key=document["key"])
    found = _codes(checked)
    assert expected <= found, [p.out() for p in checked.problems]
    unexpected = [p.out() for p in checked.errors if (p.code, p.path) not in expected]
    assert unexpected == []


def test_every_finding_has_the_one_shape() -> None:
    document = _load(FIXTURES / "invalid" / "unknown_data_field.process.yaml")
    problem = _check(document["spec"]).problems[0].out()
    assert problem == {
        "code": "unknown_data_field",
        "severity": "error",
        "path": "/spec/stages/0/steps/0/output/as/decison",
        "file": None,
        "line": None,
        "message": "data has no field decison",
        "hint": "did you mean decision?",
    }


def _line_index(text: str) -> dict[str, int]:
    """JSON pointer -> 1-based line of the node, as a package check would build it."""
    index: dict[str, int] = {}

    def walk(node: yaml.Node, path: str) -> None:
        index[path] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                walk(value, f"{path}/{pd.pointer(key.value)[1:]}")
        elif isinstance(node, yaml.SequenceNode):
            for position, value in enumerate(node.value):
                walk(value, f"{path}/{position}")

    root = yaml.compose(text, Loader=_yaml12_loader())
    assert root is not None
    walk(root, "")
    return index


def test_a_finding_of_a_package_file_names_the_file_and_line() -> None:
    path = FIXTURES / "invalid" / "unknown_data_field.process.yaml"
    text = path.read_text("utf-8")
    document = _load(path)
    lines = _line_index(text)
    checked = pd.check_process(
        document["key"],
        pd.normalized_spec(document["spec"]),
        CATALOG,
        file="processes/sample.yaml",
        locate=lines.get,
    )
    problem = checked.problems[0]
    assert problem.file == "processes/sample.yaml"
    assert problem.line is not None
    assert "decison:" in text.splitlines()[problem.line - 1]


def test_the_owner_is_kept_and_its_chain_is_checked() -> None:
    assert "owner" in SAMPLE["spec"]
    checked = _check(_sample(owner=[{"agent": "nobody"}, {"expr": "data.number"}]))
    assert _codes(checked) == {("unknown_agent", "/spec/owner/0/agent")}
    checked = _check(_sample(owner=[{"expr": "1 + 1"}]))
    assert _codes(checked) == {("expression_type_error", "/spec/owner/0/expr")}


def test_the_schema_example_passes_once_its_data_declares_what_it_writes() -> None:
    """The superproject's example covers every block: the language check
    types all of them; it only finds the two liberties the example takes."""
    spec = copy.deepcopy(PROCESS["spec"])
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
    assert _codes(_check(spec, catalog, PROCESS["key"])) == {
        ("unknown_data_field", "/spec/stages/1/steps/0/output/as/approversNeeded"),
        ("invalid_migration", "/spec/migrations/0"),
    }
    spec["data"]["properties"]["approversNeeded"] = {"type": "number"}
    del spec["migrations"]  # a map into version 2 belongs to version 2
    checked = _check(spec, catalog, PROCESS["key"])
    assert checked.problems == ()
    kinds = {element.id: (element.kind, element.parent) for element in checked.elements}
    assert kinds["go-no-go"] == ("stage", None)
    assert kinds["decide-participation"] == ("step", "go-no-go")
    assert kinds["no-history"] == ("step", "recall-history")
    assert kinds["approval-level"] == ("decision", None)
    table = next(e for e in checked.elements if e.id == "approval-level").out()
    assert table["governedBy"] == [{"document": "regulation:purchasing", "section": "4.2"}]


def test_event_payloads_are_typed_by_the_event_catalog() -> None:
    start = {"on": {"event": "task.completed"}, "key": "event.entityId"}
    checked = _check(_sample(start={**start, "set": {"number": "event.payload.publicId"}}))
    assert not checked.errors
    checked = _check(_sample(start={**start, "set": {"number": "event.payload.publicIdd"}}))
    assert _codes(checked) >= {("expression_type_error", "/spec/start/set/number")}


def test_a_catch_names_the_error_for_its_handlers_only() -> None:
    step = {
        "id": "guarded",
        "try": {
            "do": [{"id": "risky", "set": {"summary": "'x'"}}],
            "catch": [{"as": "err", "do": [{"id": "note", "set": {"summary": "err.type"}}]}],
        },
    }
    spec = _sample()
    spec["stages"][1]["steps"].insert(0, step)
    assert _check(spec).problems == ()
    step["try"]["do"][0]["set"] = {"summary": "err.type"}
    assert _codes(_check(spec)) == {
        ("expression_type_error", "/spec/stages/1/steps/0/try/do/0/set/summary")
    }
    step["try"]["catch"][0]["as"] = "data"
    assert ("invalid_error_binding", "/spec/stages/1/steps/0/try/catch/0/as") in _codes(
        _check(spec)
    )


def test_a_disabled_skill_is_not_callable() -> None:
    catalog = Catalog(
        **{
            **CATALOG.__dict__,
            "skills": {"text.summarize@1": SkillEntry(None, None, status="disabled")},
        }
    )
    assert ("unknown_skill", "/spec/stages/1/steps/0/call/skill") in _codes(
        _check(SAMPLE["spec"], catalog)
    )


# --- stability of ids against the previous version ---------------------------------


def _next(spec: dict[str, Any]) -> dict[str, Any]:
    return {**copy.deepcopy(spec), "version": spec["version"] + 1}


def test_an_id_does_not_change_its_kind() -> None:
    previous = pd.normalized_spec(SAMPLE["spec"])
    spec = _next(previous)
    spec["stages"][1]["steps"][1]["id"] = "finish"
    spec["stages"][1]["id"] = "summarize"
    spec["stages"][1]["steps"][0]["id"] = "summarize-text"
    catalog = Catalog(**{**CATALOG.__dict__, "previous": previous})
    found = _codes(_check(spec, catalog))
    assert ("element_kind_changed", "/spec/stages/1/id") in found
    assert ("element_removed", "/spec") in found  # step "done" is gone


def test_a_removed_element_named_by_a_migration_map_is_fine() -> None:
    previous = pd.normalized_spec(SAMPLE["spec"])
    spec = _next(previous)
    spec["stages"][1]["steps"][1]["id"] = "finish"
    catalog = Catalog(**{**CATALOG.__dict__, "previous": previous, "versions": {1: previous}})
    removed = [p for p in _check(spec, catalog).problems if p.code == "element_removed"]
    assert len(removed) == 1 and "'done'" in removed[0].message and not removed[0].error

    spec["migrations"] = [{"from": 1, "to": 2, "policy": "migrate", "map": {"done": "finish"}}]
    assert _check(spec, catalog).problems == ()

    spec["migrations"][0]["map"] = {"done": "finnish", "gone": "finish"}
    assert _codes(_check(spec, catalog)) == {
        ("unknown_element", "/spec/migrations/0/map/done"),
        ("unknown_element", "/spec/migrations/0/map/gone"),
    }


# --- normalization and the hash ---------------------------------------------------


def test_the_hash_does_not_depend_on_key_order_or_number_spelling() -> None:
    spec = pd.normalized_spec(SAMPLE["spec"])
    reordered = pd.normalized_spec(dict(reversed(list(copy.deepcopy(SAMPLE["spec"]).items()))))
    assert pd.definition_hash(spec) == pd.definition_hash(reordered)
    assert pd.definition_hash(spec).startswith("sha256:") and len(pd.definition_hash(spec)) == 71
    assert pd.normalized_spec({"version": 1.0, "x": 0.5}) == {"version": 1, "x": 0.5}
    assert pd.normalized_spec({"name": "é"}) == {"name": "é"}
    changed = _sample(displayName="Other")
    assert pd.definition_hash(pd.normalized_spec(changed)) != pd.definition_hash(spec)


@pytest.mark.parametrize(
    "spec",
    [
        {"x": float("nan")},
        {"x": "s" * (pd.MAX_SPEC_STRING + 1)},
        {"x": {"é": 1, "é": 2}},
        {"x": {1, 2}},
        [],
    ],
)
def test_what_cannot_be_hashed_is_refused(spec: Any) -> None:
    with pytest.raises(pd.SpecError):
        pd.normalized_spec(spec)


def test_a_spec_may_nest_deeper_than_a_manifest() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(40):
        node["n"] = {}
        node = node["n"]
    assert pd.normalized_spec(deep) == deep
    for _ in range(30):
        node["n"] = {}
        node = node["n"]
    with pytest.raises(pd.SpecError):
        pd.normalized_spec(deep)


def test_references_name_what_the_catalog_has_to_hold() -> None:
    refs = pd.references(PROCESS["spec"])
    assert refs.skills == {"notify.send@1", "process.retrospective@1"}
    assert refs.task_types == {"go-no-go", "lessons-review"}
    assert refs.agents == {"example-process"}
    assert refs.calendars == {"ru"}
    assert refs.artifact_types == {"notice-document"}
    assert refs.migration_versions == {1}


# --- the schema of the kind is the catalog's ---------------------------------------


def test_the_core_copy_of_the_kind_schema_is_the_catalog_one() -> None:
    catalog = json.loads((PINNED / "object.schema.json").read_text("utf-8"))
    held = json.loads(pd.SCHEMA_FILE.read_text("utf-8"))
    assert held == pd.process_schema_from_catalog(catalog), (
        "regenerate process_spec.schema.json with process_schema_from_catalog()"
    )
    assert "owner" in held["$defs"]["processSpec"]["properties"]


def test_the_workspace_is_an_id_not_an_install_variable() -> None:
    workspace = "0b7f4a52-3c1e-4f6a-9d1c-2f0e6b7a8c9d"
    assert _check(_sample(workspaceId=workspace)).problems == ()
    assert _codes(_check(_sample(workspaceId="nowhere"))) == {
        ("schema_violation", "/spec/workspaceId")
    }


def test_recall_where_values_are_expressions_checked_by_operator() -> None:
    # CP-ADR-0076, amendment 2026-09-28: a value is CEL (a list of CEL for in).
    spec = _sample()
    stage = next(
        index
        for index, stage in enumerate(spec["stages"])
        if any(s["id"] == "history" for s in stage["steps"])
    )
    step = next(s for s in spec["stages"][stage]["steps"] if s["id"] == "history")
    step["recall"]["where"] = [
        {"attr": "okpd2", "op": "prefix", "value": "'62.' + data.number"},
        {"attr": "validUntil", "op": "gte", "value": "data.deadline"},
        {"attr": "status", "op": "in", "value": ["'active'", "data.decision"]},
        {"attr": "region", "op": "in", "value": "['77', '50']"},
        {"attr": "inn", "op": "exists", "value": False},
        {"attr": "rank", "op": "lte", "value": 3},
    ]
    assert _check(spec).problems == ()
    here = f"/spec/stages/{stage}/steps/0/recall/where"
    step["recall"]["where"] = [
        {"attr": "okpd2", "op": "prefix", "value": "data.amount"},
        {"attr": "status", "op": "in", "value": "data.decision"},
        {"attr": "inn", "op": "exists", "value": "data.number"},
        {"attr": "kind", "op": "eq", "value": "data.nope"},
    ]
    assert _codes(_check(spec)) == {
        ("expression_type_error", f"{here}/0/value"),
        ("expression_type_error", f"{here}/1/value"),
        ("expression_type_error", f"{here}/2/value"),
        ("expression_type_error", f"{here}/3/value"),
    }


# --- the calendar of a due in working units (CP-ADR-0078 §1; P009) ---------------------

HOURS = replace(
    CATALOG, calendars=frozenset({"ru", "ru-2024"}), calendars_with_hours=frozenset({"ru"})
)


def _with_due(due: Any, *, calendar: str | None = "ru") -> dict[str, Any]:
    spec = _sample()
    if calendar is None:
        del spec["calendar"]
        # The sample's own deadline reads the process calendar.
        spec["stages"][0]["steps"][2]["human"]["due"] = {
            "at": 'cal.addWorkdays(data.deadline, -2, "ru")'
        }
    spec["stages"][1]["steps"][0]["call"]["due"] = due
    return spec


CALL_DUE = "/spec/stages/1/steps/0/call/due"


@pytest.mark.parametrize(
    "due",
    [
        "P2D",
        {"at": "data.deadline"},
        {"duration": "PT4H", "warnBefore": "PT1H"},
        {"workdays": 2},
        {"workhours": 8},
        {"workhours": 8, "calendar": "ru", "warnBefore": {"workhours": 2}},
        {"workdays": 2, "calendar": "ru-2024", "warnBefore": {"workdays": 1}},
        {"duration": "PT4H", "warnBefore": {"workhours": 1}},
    ],
)
def test_a_due_in_any_form_passes_with_a_calendar_that_counts_it(due: Any) -> None:
    assert _check(_with_due(due), HOURS).problems == ()


def test_working_units_without_a_calendar_are_refused() -> None:
    checked = _check(_with_due({"workhours": 8}, calendar=None), HOURS)
    (problem,) = checked.errors
    assert (problem.code, problem.path) == ("sla_calendar_missing", CALL_DUE + "/workhours")
    assert "calendar" in problem.message and problem.hint is not None
    warn = _with_due({"duration": "PT4H", "warnBefore": {"workdays": 1}}, calendar=None)
    assert _codes(_check(warn, HOURS)) == {
        ("sla_calendar_missing", CALL_DUE + "/warnBefore/workdays")
    }
    # A calendar of the due itself is enough.
    keyed = _with_due({"workdays": 2, "calendar": "ru"}, calendar=None)
    assert _check(keyed, HOURS).problems == ()


def test_workhours_by_a_calendar_without_working_hours_are_refused() -> None:
    checked = _check(_with_due({"workhours": 8, "calendar": "ru-2024"}), HOURS)
    (problem,) = checked.errors
    assert (problem.code, problem.path) == (
        "sla_calendar_without_hours",
        CALL_DUE + "/workhours",
    )
    assert "ru-2024" in problem.message and "workingHours" in (problem.hint or "")
    warn = {"workdays": 2, "calendar": "ru-2024", "warnBefore": {"workhours": 2}}
    assert _codes(_check(_with_due(warn), HOURS)) == {
        ("sla_calendar_without_hours", CALL_DUE + "/warnBefore/workhours")
    }
    by_process = _sample(calendar="ru-2024", due={"workhours": 40})
    assert _codes(_check(by_process, HOURS)) == {
        ("sla_calendar_without_hours", "/spec/due/workhours")
    }


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("human", {"taskType": "review", "assign": [{"role": "lead"}]}),
        ("approve", {"approvers": [{"role": "lead"}], "quorum": "any"}),
        ("recall", {"anchors": [{"case": True}]}),
        ("listen", {"any": [{"on": {"observation": "sample.answered"}}], "timeout": "P1D"}),
    ],
)
def test_every_step_with_a_due_is_checked(kind: str, body: dict[str, Any]) -> None:
    spec = _sample(calendar="ru-2024")
    spec["stages"][1]["steps"].insert(0, {"id": "waiting", kind: {**body, "due": {"workhours": 4}}})
    path = f"/spec/stages/1/steps/0/{kind}/due/workhours"
    assert _codes(_check(spec, HOURS)) == {("sla_calendar_without_hours", path)}


def test_a_due_names_a_calendar_that_exists() -> None:
    assert _codes(_check(_with_due({"workdays": 2, "calendar": "kz"}), HOURS)) == {
        ("unknown_calendar", CALL_DUE + "/calendar")
    }
    refs = pd.references(_with_due({"workdays": 2, "calendar": "kz"}))
    assert refs.calendars == {"ru", "kz"}


def test_hours_not_loaded_are_not_checked() -> None:
    # The engine compiles a published version without them: a calendar that
    # lost its hours later fails the due, not the version (CP-ADR-0078 §3).
    spec = _with_due({"workhours": 8, "calendar": "ru-2024"})
    unchecked = replace(HOURS, calendars_with_hours=None)
    assert _check(spec, unchecked).problems == ()


# --- retired keys (CP-ADR-0074, amendment Zh2, Zh3) -------------------------------------


def test_a_retired_calendar_is_an_error() -> None:
    catalog = Catalog(**{**CATALOG.__dict__, "retired_calendars": frozenset({"ru"})})
    checked = _check(SAMPLE["spec"], catalog)
    assert [(p.code, p.path, p.error) for p in checked.problems] == [
        ("calendar_retired", "/spec/calendar", True)
    ]


def test_a_call_of_a_retired_process_is_a_warning() -> None:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec["stages"][1]["steps"][0] = {"id": "summarize", "call": {"process": "child"}}
    catalog = Catalog(
        **{
            **CATALOG.__dict__,
            "processes": frozenset({"child"}),
            "retired_processes": frozenset({"child"}),
        }
    )
    checked = _check(spec, catalog)
    assert [(p.code, p.path, p.error) for p in checked.problems] == [
        ("process_retired", "/spec/stages/1/steps/0/call/process", False)
    ]


# --- human.customFields: the task filled from the case (amendment 2026-10-01) ----------


def _prefilled(custom_fields: Any, task_type: str = "review") -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Prefilled",
        "identity": {"agent": "sample-process"},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "amount": {"type": "integer"},
                "decision": {"type": "string"},
            },
        },
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.number",
            "set": {"number": "'1'"},
        },
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "decide",
                        "human": {
                            "taskType": task_type,
                            "assign": [{"role": "lead"}],
                            "customFields": custom_fields,
                        },
                    }
                ],
            }
        ],
    }


def test_the_fields_a_human_step_fills_are_checked_against_the_type() -> None:
    assert _check(_prefilled({"decision": "data.number + '-draft'"})).errors == []
    found = _codes(_check(_prefilled({"decison": "data.number", "decision": "data.amount"})))
    here = "/spec/stages/0/steps/0/human/customFields"
    assert {
        ("unknown_custom_field", f"{here}/decison"),
        ("custom_field_type_mismatch", f"{here}/decision"),
    } <= found


def test_a_field_expression_is_checked_as_an_expression() -> None:
    found = _codes(_check(_prefilled({"decision": "data.nowhere"})))
    assert any(p.endswith("/human/customFields/decision") for _, p in found), found


def test_a_type_without_an_object_schema_takes_any_field() -> None:
    catalog = replace(CATALOG, task_types={**CATALOG.task_types, "free": {}})
    assert _check(_prefilled({"anything": "data.number"}, "free"), catalog).errors == []


@pytest.mark.parametrize(
    "custom_fields", [{"bad-name": "data.number"}, {"a.b": "data.number"}, {"x": 1}, []]
)
def test_the_shape_of_the_fields_is_the_schemas(custom_fields: Any) -> None:
    found = {code for code, _ in _codes(_check(_prefilled(custom_fields)))}
    assert "schema_violation" in found, found
