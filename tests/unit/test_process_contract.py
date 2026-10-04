"""The process-packages contract of the core (CP-ADR-0074, 0075, 0076; P002).

What is pinned here: the routes of processes, calendars and packages and their
bodies in OpenAPI, ``excludedPrincipals`` of an approval, the new permissions
in the enum and ``authz/catalog.yaml``, the ``process.*``, ``calendar.*`` and
``knowledge.changed`` payloads of the event catalog, and the catalog schema of
the superproject (P001) against the core: the pinned copies of
``object.schema.json`` and ``test.schema.json`` are the superproject's (and
the contract of ``process.retrospective@1`` of the package process-knowledge), the
examples of the kinds Process and Calendar and of a package test pass the
schema and the core's request models unchanged.

The schemas belong to package-sdk and are read from it when it is checked out
next to control-plane, from the pinned copies otherwise (``tests/package_sdk.py``);
the examples are read from the superproject when this repository is checked
out inside it, from the pinned copies otherwise.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    ApprovalOut,
    ApprovalRequestRequest,
    CalendarPublishRequest,
    PackageSource,
    PackageTestRequest,
    PlanProcessOut,
    ProcessDefinitionPublishRequest,
    ProcessJournalEntryOut,
    ProcessOpenElementOut,
    ProcessReplayRequest,
)
from control_plane.domain.enums import Permission
from control_plane.domain.event_catalog import event_types, get_event_type
from control_plane.domain.package_plan import PLANNED_KINDS
from tests.package_sdk import PINNED_NAMES, UMBRELLA, live_schema_path, schema_path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PINNED = FIXTURES / "superproject"
EXAMPLES = FIXTURES / "processes"
# The superproject control-plane is a submodule of, by its layout (TAI-ADR-0064):
# services/control-plane, or control-plane at the root in the flat one.
SUPERPROJECT = UMBRELLA
SUPERPROJECT_EXAMPLES = SUPERPROJECT / "tools" / "tests" / "fixtures" / "process"
# The skill of a process's retrospective (package process-knowledge, CP-ADR-0076 §6).
SUPERPROJECT_RETROSPECTIVE = (
    SUPERPROJECT / "packages" / "process-knowledge" / "skills" / "process.retrospective.yaml"
)
# The development superproject has both sources; an open umbrella does not ship them,
# and then the pinned copies are the truth. The schemas are package-sdk's (S007).
INSIDE_SUPERPROJECT = all(
    path.is_file()
    for path in (SUPERPROJECT_EXAMPLES / "purchase.process.yaml", SUPERPROJECT_RETROSPECTIVE)
)


def _yaml12_loader() -> type[yaml.SafeLoader]:
    """SafeLoader with the booleans of YAML 1.2 only (TAI-ADR-0054 p.11).

    PyYAML follows YAML 1.1 and reads ``on``/``off``/``yes``/``no`` as bools:
    the key ``on`` of a trigger would become ``True``. package-sdk reads
    packages the same way."""

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    return Loader


def _read_yaml(name: str) -> Any:
    directory = SUPERPROJECT_EXAMPLES if INSIDE_SUPERPROJECT else EXAMPLES
    return yaml.load((directory / name).read_text("utf-8"), Loader=_yaml12_loader())


def _schema(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text("utf-8"))


CATALOG_SCHEMA = _schema("object.schema.json")
CATALOG = jsonschema.Draft202012Validator(
    CATALOG_SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)
TESTS = jsonschema.Draft202012Validator(_schema("test.schema.json"))


def retrospective_contract() -> dict[str, Any]:
    """``inputs`` and ``outputs`` of ``process.retrospective@1`` as the package declares them."""
    path = (
        SUPERPROJECT_RETROSPECTIVE if INSIDE_SUPERPROJECT else PINNED / "process.retrospective.yaml"
    )
    return dict(yaml.safe_load(path.read_text("utf-8"))["spec"]["contract"])


PROCESS = _read_yaml("purchase.process.yaml")
CALENDAR = _read_yaml("ru.calendar.yaml")
PACKAGE_TEST = _read_yaml("purchase.test.yaml")
# The examples of CP-ADR-0074 Z1 (plan R6 of the superproject).
RULE_TEST: dict[str, Any] = {
    "subject": "rule",
    "rule": "claim-reopened",
    "name": "a reopened claim files its review",
    "given": {
        "observation": {
            "kind": "helpdesk.ticket_reopened",
            "data": {"ticketId": "T-1", "claimKey": "T-1", "text": "..."},
        }
    },
    "mocks": {"skills": {"claims.classify@1": [{"output": {"category": "complaint"}}]}},
    "steps": [
        {
            "expect": {
                "result": "matched",
                "ensureWork": [{"type": "claim-review", "customFields": {"ticketId": "T-1"}}],
            }
        }
    ],
}
TASK_TYPE_TEST: dict[str, Any] = {
    "subject": "taskType",
    "taskType": "refund-approval",
    "name": "an approved refund replies and closes the task",
    "given": {"task": {"customFields": {"amount": 72000, "ticketId": "T-1"}}},
    "steps": [
        {"approve": {"gate": "default", "decision": "approved"}},
        {
            "expect": {
                "invokeSkill": [{"skill": "helpdesk.reply@1", "inputs": {"ticketId": "T-1"}}],
                "status": {"category": "terminal_success"},
            }
        },
    ],
}


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
PATHS: dict[str, Any] = OPENAPI["paths"]
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]


def _ref(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def _body(path: str, method: str) -> str | None:
    body = PATHS[path][method].get("requestBody")
    return _ref(body["content"]["application/json"]["schema"]) if body else None


def _response(path: str, method: str) -> str:
    responses = PATHS[path][method]["responses"]
    ok = next(code for code in ("201", "200") if code in responses)
    return _ref(responses[ok]["content"]["application/json"]["schema"])


def _catalog_errors(document: dict[str, Any]) -> list[str]:
    return [error.message for error in CATALOG.iter_errors(document)]


def _core_accepts(model: type[BaseModel], value: Any) -> bool:
    try:
        model.model_validate(value)
    except ValidationError:
        return False
    return True


# --- OpenAPI -------------------------------------------------------------------

# (path, method, request body, response body)
ROUTES: list[tuple[str, str, str | None, str]] = [
    (
        "/api/v1/process-definitions",
        "post",
        "ProcessDefinitionPublishRequest",
        "ProcessDefinitionOut",
    ),
    ("/api/v1/process-definitions", "get", None, "PageOut"),
    ("/api/v1/process-definitions/{ref}", "get", None, "ProcessDefinitionOut"),
    ("/api/v1/process-definitions/{key}/versions", "get", None, "PageOut"),
    (
        "/api/v1/process-definitions/{key}:replay",
        "post",
        "ProcessReplayRequest",
        "ProcessReplayOut",
    ),
    (
        "/api/v1/process-instances",
        "post",
        "ProcessInstanceStartRequest",
        "ProcessInstanceOut",
    ),
    ("/api/v1/process-instances", "get", None, "PageOut"),
    ("/api/v1/process-instances/{instance_id}", "get", None, "ProcessInstanceOut"),
    ("/api/v1/process-instances/{instance_id}/journal", "get", None, "PageOut"),
    (
        "/api/v1/process-instances/{instance_id}:suspend",
        "post",
        "ProcessSuspendRequest",
        "ProcessInstanceOut",
    ),
    (
        "/api/v1/process-instances/{instance_id}:resume",
        "post",
        "ProcessResumeRequest",
        "ProcessInstanceOut",
    ),
    (
        "/api/v1/process-instances/{instance_id}:cancel",
        "post",
        "ProcessCancelRequest",
        "ProcessInstanceOut",
    ),
    ("/api/v1/calendars", "post", "CalendarPublishRequest", "CalendarOut"),
    ("/api/v1/calendars", "get", None, "PageOut"),
    ("/api/v1/calendars/{ref}", "get", None, "CalendarOut"),
    ("/api/v1/calendars/{key}:retire", "post", "CatalogRetireRequest", "CalendarRetireOut"),
    (
        "/api/v1/process-definitions/{key}:retire",
        "post",
        "CatalogRetireRequest",
        "ProcessRetireOut",
    ),
    ("/api/v1/packages:test", "post", "PackageTestRequest", "PackageTestOut"),
    ("/api/v1/packages:plan", "post", "PackagePlanRequest", "PackagePlanOut"),
    ("/api/v1/packages:apply", "post", "PackageApplyRequest", "PackageApplyOut"),
    ("/api/v1/packages:record", "post", "PackageRecordRequest", "PackageRecordOut"),
]
# Routes whose step has landed: they no longer document a 501.
IMPLEMENTED = {
    "/api/v1/calendars",  # P004
    "/api/v1/calendars/{ref}",
    "/api/v1/process-definitions",  # P006
    "/api/v1/process-definitions/{ref}",
    "/api/v1/process-definitions/{key}/versions",
    "/api/v1/process-instances",  # P009
    "/api/v1/process-instances/{instance_id}",
    "/api/v1/process-instances/{instance_id}/journal",
    "/api/v1/process-instances/{instance_id}:suspend",
    "/api/v1/process-instances/{instance_id}:resume",
    "/api/v1/process-instances/{instance_id}:cancel",
    "/api/v1/packages:plan",  # P015
    "/api/v1/packages:apply",
    "/api/v1/packages:record",  # TASK-000904
    "/api/v1/calendars/{key}:retire",  # S012
    "/api/v1/process-definitions/{key}:retire",
}


@pytest.mark.parametrize(("path", "method", "request_body", "response_body"), ROUTES)
def test_openapi_carries_the_route_with_its_bodies(
    path: str, method: str, request_body: str | None, response_body: str
) -> None:
    assert _body(path, method) == request_body
    assert _response(path, method) == response_body
    pending = path not in IMPLEMENTED
    assert ("501" in PATHS[path][method]["responses"]) is pending, "only a pending route has 501"


@pytest.mark.parametrize(
    "path", ["/api/v1/process-definitions/{key}:retire", "/api/v1/calendars/{key}:retire"]
)
def test_retire_takes_dry_run_as_a_query_parameter(path: str) -> None:
    parameters = PATHS[path]["post"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [("key", "path"), ("dryRun", "query")]


@pytest.mark.parametrize("schema", ["ProcessDefinitionOut", "CalendarOut"])
def test_a_version_shows_the_retirement_of_its_key(schema: str) -> None:
    properties = SCHEMAS[schema]["properties"]
    assert properties["status"]["enum"] == ["active", "retired"]
    assert "retired" in properties


def test_package_test_takes_check_only_as_a_query_parameter() -> None:
    parameters = PATHS["/api/v1/packages:test"]["post"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [("checkOnly", "query")]


def test_a_finding_has_the_same_shape_everywhere() -> None:
    """``{code, path, file, line, message, hint}`` (FR-022) plus severity."""
    problem = SCHEMAS["ProcessProblemOut"]["properties"]
    assert set(problem) == {"code", "severity", "path", "file", "line", "message", "hint"}
    for name in ("ProcessReplayOut", "PackageTestOut", "PackagePlanOut"):
        assert _ref(SCHEMAS[name]["properties"]["problems"]["items"]) == "ProcessProblemOut"


def test_the_plan_names_its_hash_the_catalog_etag_and_every_part_of_fr_027() -> None:
    plan = SCHEMAS["PackagePlanOut"]["properties"]
    assert {"planHash", "catalogEtag", "changes", "processes", "regulationCoverage"} <= set(plan)
    field = SCHEMAS["PlanFieldOut"]["properties"]
    assert field["owner"]["enum"] == ["package", "console"]
    process = SCHEMAS["PlanProcessOut"]["properties"]
    assert {"behaviour", "instances"} <= set(process)
    assert SCHEMAS["PlanInstancesOut"]["properties"]["fate"]["enum"] == [
        "pin",
        "migrate",
        "unaffected",
    ]
    # Every kind of the catalog the core holds is planned (amendment 2026-09-29);
    # the rest of a package is named with who applies it.
    change = SCHEMAS["PlanChangeOut"]["properties"]
    assert change["kind"]["enum"] == list(PLANNED_KINDS)
    assert change["deprecates"]["items"] == {"type": "integer"}
    assert _ref(plan["outside"]["items"]) == "PlanOutsideOut"
    outside = SCHEMAS["PlanOutsideOut"]["properties"]["appliedBy"]
    assert outside["enum"] == ["installer", "notification-service"]
    apply = SCHEMAS["PackageApplyRequest"]
    assert apply["properties"]["planHash"]["pattern"] == "^sha256:[0-9a-f]{64}$"
    assert "planHash" in apply["required"]


def test_an_instance_shows_its_pinned_version_and_journal_entries_their_author() -> None:
    instance = SCHEMAS["ProcessInstanceOut"]["properties"]
    assert {"definitionKey", "definitionVersion", "instanceKey", "data", "timers"} <= set(instance)
    # The journal is a page of these (PageOut), so the model publishes it.
    entry = ProcessJournalEntryOut.model_json_schema(by_alias=True)["properties"]
    assert {"seq", "at", "kind", "element", "reason", "actorId", "eventId"} <= set(entry)
    definition = SCHEMAS["ProcessDefinitionOut"]["properties"]
    assert {
        "version",
        "definitionHash",
        "identityAgent",
        "expressionProfile",
        "engineRevision",
        "owner",
    } <= set(definition)


SLA_STATES = ["ok", "warning", "breached", "paused", "unknown", "none"]


def test_the_projection_carries_the_sla_of_steps_and_of_the_process() -> None:
    """CP-ADR-0078 §6: attempt, due, slaState and overdueSeconds; optional, null by default."""
    element = SCHEMAS["ProcessOpenElementOut"]
    assert {"attempt", "due", "slaState", "overdueSeconds"} <= set(element["properties"])
    instance = SCHEMAS["ProcessInstanceOut"]
    assert {"sla", "slaState"} <= set(instance["properties"])
    for schema in (element, instance):
        options = schema["properties"]["slaState"]["anyOf"]
        assert {"type": "string", "enum": SLA_STATES} in options
    sla = SCHEMAS["ProcessSlaOut"]["properties"]
    assert set(sla) == {"dueAt", "warnAt", "provisional", "remainingSeconds", "remainingUnit"}
    # The unit of a frozen remainder (CP-ADR-0078 §4): workdays are encoded, not seconds.
    unit = {"type": "string", "enum": ["wall", "working_seconds", "workdays"]}
    assert unit in sla["remainingUnit"]["anyOf"]
    assert {"type": "null"} in sla["remainingUnit"]["anyOf"]
    assert _ref(element["properties"]["due"]["anyOf"][0]) == "ProcessSlaOut"
    assert _ref(instance["properties"]["sla"]["anyOf"][0]) == "ProcessSlaOut"
    # The projection built before the SLA fields validates, and reads them as null.
    before = {"id": "a", "kind": "human", "since": "2026-09-29T10:00:00Z", "taskId": None}
    body = ProcessOpenElementOut.model_validate(before | {"approvalIds": []}).model_dump(
        mode="json", by_alias=True
    )
    new = ("attempt", "due", "slaState", "overdueSeconds")
    assert {name: body[name] for name in new} == dict.fromkeys(new)


def test_the_instance_list_filters_by_sla_state_under_processes_read() -> None:
    parameters = PATHS["/api/v1/process-instances"]["get"]["parameters"]
    [sla] = [p for p in parameters if p["name"] == "slaState"]
    assert sla["in"] == "query"
    assert not sla.get("required", False)
    options = sla["schema"]["anyOf"]
    assert {"type": "string", "enum": ["breached", "warning"]} in options


def test_the_plan_lists_the_deadlines_a_migration_moves() -> None:
    """CP-ADR-0074 amendment 2026-09-29, §11 (FR-023)."""
    process = SCHEMAS["PlanProcessOut"]["properties"]
    assert _ref(process["deadlines"]["items"]) == "PlanDeadlineOut"
    deadline = SCHEMAS["PlanDeadlineOut"]["properties"]
    assert set(deadline) == {"instanceId", "element", "previousDueAt", "dueAt", "breached"}
    # Empty until the plan computes them.
    out = PlanProcessOut.model_validate(
        {"key": "p", "fromVersion": 1, "toVersion": 2, "behaviour": None, "instances": []}
    )
    assert out.deadlines == []


def test_an_approval_carries_excluded_principals() -> None:
    request = SCHEMAS["ApprovalRequestRequest"]
    assert "excludedPrincipals" not in request.get("required", [])
    options = request["properties"]["excludedPrincipals"]["anyOf"]
    array = next(option for option in options if option.get("type") == "array")
    assert array["items"] == {"type": "string", "format": "uuid"}
    assert array["maxItems"] == 100
    assert SCHEMAS["ApprovalOut"]["properties"]["excludedPrincipals"]["type"] == "array"
    # An approval that excludes nobody reads as an empty list.
    field = ApprovalOut.model_fields["excluded_principals"]
    assert field.get_default(call_default_factory=True) == []
    assert ApprovalRequestRequest.model_validate({"task": "T"}).excluded_principals is None


# --- permissions -----------------------------------------------------------------

PERMISSIONS = {
    "processes.read": "workspace",
    "processes.write": "workspace",
    "processes.operate": "workspace",
    "packages.test": "tenant",
    "packages.plan": "tenant",
    "calendars.write": "tenant",
}


def test_permissions_are_in_the_enum_and_the_catalog() -> None:
    assert {p.value for p in Permission} >= set(PERMISSIONS)
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    for name, resource in PERMISSIONS.items():
        assert catalog["actions"][name] == {"resource": resource}, name


# --- events ----------------------------------------------------------------------

PROCESS_EVENTS = {
    "process.started",
    "process.correlated",
    "process.data_changed",
    "process.stage_entered",
    "process.stage_exited",
    "process.milestone_reached",
    "process.timer_fired",
    "process.timer_rescheduled",
    "process.escalated",
    "process.suspended",
    "process.resumed",
    "process.compensated",
    "process.recall_completed",
    "process.recall_timed_out",
    "process.migrated",
    "process.completed",
    "process.cancelled",
    "process.failed",
}


def test_process_events_are_in_the_catalog() -> None:
    registered = {entry.type for entry in event_types()}
    assert registered >= PROCESS_EVENTS | {
        "process.definition_published",
        "process.definition_retired",
        "calendar.published",
        "calendar.retired",
        "knowledge.changed",
    }
    for name in PROCESS_EVENTS:
        entry = get_event_type(name)
        assert entry.entity_type == "process_instance", name
        required = set(entry.current.schema["required"])
        assert {"instanceId", "definitionKey", "version", "instanceKey"} <= required, name


def test_the_case_projection_travels_on_the_events_memory_is_built_from() -> None:
    """CP-ADR-0076 §2: the adapter builds the case node from the event alone."""
    for name in ("process.started", "process.data_changed", "process.completed"):
        assert "memory" in get_event_type(name).current.schema["required"], name
    published = get_event_type("process.definition_published").current.schema
    assert {"key", "version", "definitionHash", "elements"} <= set(published["required"])


def test_a_recall_answer_is_announced_not_copied() -> None:
    """CP-ADR-0076 §4: the answer lives in the instance journal; the event
    carries its size and hash, never the recalled nodes."""
    completed = get_event_type("process.recall_completed").current.schema["properties"]
    assert {"step", "recallId", "asOf", "resultHash"} <= set(completed)
    assert not {"nodes", "result", "pack"} & set(completed)
    timed_out = get_event_type("process.recall_timed_out").current.schema["properties"]
    assert {"step", "recallId", "reason"} <= set(timed_out)


def test_knowledge_changed_names_the_changed_keys() -> None:
    schema = get_event_type("knowledge.changed").current.schema
    assert get_event_type("knowledge.changed").entity_type == "workspace"
    assert {"changes", "truncated", "namespace", "source", "snapshotId"} <= set(schema["required"])
    validator = jsonschema.Draft202012Validator(schema["properties"]["changes"])
    assert validator.is_valid([{"kind": "regulation", "key": "regulation:p", "change": "changed"}])
    assert not validator.is_valid([{"kind": "regulation", "key": "x", "change": "renamed"}])


# --- the catalog schema of the superproject (P001) ------------------------------


@pytest.mark.parametrize("name", PINNED_NAMES)
def test_the_pinned_schemas_are_the_package_sdk_ones(name: str) -> None:
    """The catalog and test schemas belong to package-sdk (S003, S007); the core holds copies."""
    assert (PINNED / name).read_bytes() == live_schema_path(name).read_bytes(), name


def test_the_pinned_examples_are_the_superproject_ones() -> None:
    if not INSIDE_SUPERPROJECT:
        pytest.skip("not checked out inside the superproject")
    pinned = (PINNED / "process.retrospective.yaml").read_bytes()
    assert pinned == SUPERPROJECT_RETROSPECTIVE.read_bytes(), "process.retrospective.yaml"
    for name in ("purchase.process.yaml", "purchase.test.yaml", "ru.calendar.yaml"):
        assert (EXAMPLES / name).read_bytes() == (SUPERPROJECT_EXAMPLES / name).read_bytes(), name


def test_a_test_names_its_subject_and_each_subject_its_given_and_steps() -> None:
    schema = _schema("test.schema.json")
    assert schema["properties"]["subject"]["enum"] == ["process", "rule", "taskType"]
    assert schema["required"] == ["name", "steps"]
    wanted = {
        "rule": ("#/$defs/ruleGiven", "#/$defs/ruleStep"),
        "taskType": ("#/$defs/taskTypeGiven", "#/$defs/taskTypeStep"),
    }
    for branch in schema["allOf"][1:]:
        subject = branch["if"]["properties"]["subject"]["const"]
        then = branch["then"]["properties"]
        assert (then["given"]["$ref"], then["steps"]["items"]["$ref"]) == wanted[subject]
    for example in (RULE_TEST, TASK_TYPE_TEST):
        assert TESTS.is_valid(example)
    refused = {**RULE_TEST, "steps": [{"advance": "P1D"}]}
    assert not TESTS.is_valid(refused)


def test_the_test_result_names_its_subject_and_the_package_its_coverage() -> None:
    result = SCHEMAS["PackageTestResultOut"]
    assert {"subject", "object", "process"} <= set(result["required"])
    assert result["properties"]["subject"]["enum"] == ["process", "rule", "taskType"]
    assert {"type": "null"} in result["properties"]["process"]["anyOf"]
    out = SCHEMAS["PackageTestOut"]
    assert {"coverage", "ruleCoverage", "taskTypeCoverage"} <= set(out["required"])
    rule = SCHEMAS["RuleCoverageOut"]["properties"]
    assert set(rule) == {"rule", "tests", "branches", "outcomes"}
    task_type = SCHEMAS["TaskTypeCoverageOut"]["properties"]
    assert set(task_type) == {
        "taskType",
        "version",
        "tests",
        "outcomes",
        "preconditions",
        "completion",
        "acceptance",
    }
    for name in ("branches", "outcomes"):
        assert _ref(rule[name]) == "CoverageCounterOut"


def test_the_catalog_has_the_kinds_and_the_language_the_adrs_describe() -> None:
    kinds = CATALOG_SCHEMA["properties"]["kind"]["enum"]
    assert {"Process", "Calendar"} <= set(kinds)
    defs = CATALOG_SCHEMA["$defs"]
    # ADR references live in ``$comment`` since package-sdk TASK-001236.
    assert "CP-ADR-0075" in defs["cel"]["$comment"]
    process = defs["processSpec"]["properties"]
    assert {
        "identity",
        "owner",
        "calendar",
        "memory",
        "governedBy",
        "retrospective",
        "migrations",
    } <= set(process)
    assert process["owner"] == {
        "$ref": "#/$defs/assignChain",
        "description": process["owner"]["description"],
        "$comment": process["owner"]["$comment"],
    }
    step = defs["processStep"]["properties"]
    assert {"human", "approve", "call", "decide", "recall", "remember", "listen", "wait"} <= set(
        step
    )
    assert "context" in step["human"]["properties"]
    assert "separationOfDuties" in step["approve"]["properties"]
    assert set(defs["memoryProjection"]["properties"]) == {"case", "facts", "entities", "documents"}
    assert defs["stepContext"]["required"] == ["anchors"]
    assert "renames" in defs["packageSpec"]["properties"]


def test_the_examples_pass_the_catalog() -> None:
    assert _catalog_errors(PROCESS) == []
    assert _catalog_errors(CALENDAR) == []
    assert [error.message for error in TESTS.iter_errors(PACKAGE_TEST)] == []
    assert "recall" in PACKAGE_TEST["mocks"]


def test_the_example_process_passes_the_core_unchanged() -> None:
    request = ProcessDefinitionPublishRequest.model_validate(
        {"key": PROCESS["key"], "spec": PROCESS["spec"]}
    )
    assert request.spec == PROCESS["spec"]
    assert ProcessReplayRequest.model_validate({"spec": PROCESS["spec"]}).limit == 50
    for bad in ("Purchase", "", "p" * 64, "-purchase"):
        assert _catalog_errors({**PROCESS, "key": bad}) != [], bad
        assert not _core_accepts(ProcessDefinitionPublishRequest, {"key": bad, "spec": {}}), bad


def _calendar(**changes: Any) -> dict[str, Any]:
    spec = copy.deepcopy(CALENDAR["spec"])
    spec.update(changes)
    return spec


def _year(**changes: Any) -> dict[str, Any]:
    return {"year": 2026, **changes}


def _hours(start: str, end: str) -> dict[str, str]:
    return {"from": start, "to": end}


CALENDARS_BOTH_ACCEPT: list[dict[str, Any]] = [
    _calendar(),
    _calendar(weekend=[7]),
    _calendar(weekend=[5, 6]),
    _calendar(years=[_year(provisional=True)]),
    _calendar(years=[_year(holidays=["2026-01-01"], workdays=["2026-11-01"])]),
    _calendar(years=[_year(shortDays=["2026-12-31"], source="decree")]),
    _calendar(years=[_year(year=2000), _year(year=2100)]),
    # Working hours (CP-ADR-0078 §2).
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")]}),
    _calendar(
        workingHours={
            "intervals": [_hours("09:00", "13:00"), _hours("14:00", "18:00")],
            "weekdays": {"5": [_hours("09:00", "16:45")], "6": []},
            "shortDayReduction": "PT1H",
        }
    ),
    _calendar(workingHours={"intervals": [_hours("00:00", "24:00")]}),
]

CALENDARS_BOTH_REJECT: list[dict[str, Any]] = [
    _calendar(years=[]),
    _calendar(weekend=[0]),
    _calendar(weekend=[8]),
    _calendar(weekend=[6, 6]),
    _calendar(years=[_year(year=1999)]),
    _calendar(years=[_year(year=2101)]),
    _calendar(years=[{"provisional": True}]),
    _calendar(years=[_year(holidays=["2026-01-01", "2026-01-01"])]),
    _calendar(years=[_year(holidays=["tomorrow"])]),
    _calendar(years=[_year(source="s" * 501)]),
    _calendar(years=[_year(unknown=True)]),
    _calendar(timezone="t" * 65),
    _calendar(displayName=""),
    _calendar(unknown=True),
    _calendar(workingHours={}),
    _calendar(workingHours={"intervals": []}),
    _calendar(workingHours={"intervals": [_hours("9:00", "18:00")]}),
    _calendar(workingHours={"intervals": [_hours("09:00", "24:01")]}),
    _calendar(workingHours={"intervals": [_hours("24:00", "24:00")]}),
    _calendar(workingHours={"intervals": [{"from": "09:00"}]}),
    _calendar(workingHours={"intervals": [{**_hours("09:00", "18:00"), "break": "PT1H"}]}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")] * 11}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")], "weekdays": {"0": []}}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")], "weekdays": {"8": []}}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")], "shortDayReduction": "1H"}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")], "shortDayReduction": "PT"}),
    _calendar(workingHours={"intervals": [_hours("09:00", "18:00")], "unknown": True}),
    {key: value for key, value in CALENDAR["spec"].items() if key != "timezone"},
]


@pytest.mark.parametrize("spec", CALENDARS_BOTH_ACCEPT)
def test_a_calendar_the_catalog_accepts_the_core_accepts(spec: dict[str, Any]) -> None:
    assert _catalog_errors({**CALENDAR, "spec": spec}) == []
    request = CalendarPublishRequest.model_validate({"key": "ru", "spec": spec})
    assert request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True) == spec


@pytest.mark.parametrize("spec", CALENDARS_BOTH_REJECT)
def test_a_calendar_the_catalog_rejects_the_core_rejects(spec: dict[str, Any]) -> None:
    assert _catalog_errors({**CALENDAR, "spec": spec}) != []
    assert not _core_accepts(CalendarPublishRequest, {"key": "ru", "spec": spec})


def test_a_package_travels_as_its_files() -> None:
    files = [
        {"path": f"processes/{name}", "content": (EXAMPLES / name).read_text("utf-8")}
        for name in ("purchase.process.yaml", "ru.calendar.yaml")
    ] + [
        {
            "path": "tests/purchase.test.yaml",
            "content": (EXAMPLES / "purchase.test.yaml").read_text("utf-8"),
        }
    ]
    request = PackageTestRequest.model_validate({"package": {"files": files}})
    assert [item.path for item in request.package.files] == [item["path"] for item in files]
    for bad in ("/etc/passwd", "../x.yaml", "a/../b.yaml", "a//b.yaml", "a\\b.yaml", ""):
        assert not _core_accepts(PackageSource, {"files": [{"path": bad, "content": ""}]}), bad
    twice = {"files": [files[0], files[0]]}
    assert not _core_accepts(PackageSource, twice)


def test_the_plan_counts_all_deadlines_it_lists_at_most_a_limit_of() -> None:
    """Review of P017 (TASK-001161): the section is capped, ``deadlinesTotal`` counts all."""
    process = SCHEMAS["PlanProcessOut"]["properties"]
    assert process["deadlinesTotal"]["type"] == "integer"
    out = PlanProcessOut.model_validate(
        {"key": "p", "fromVersion": 1, "toVersion": 2, "behaviour": None, "instances": []}
    )
    assert out.deadlines_total == 0
