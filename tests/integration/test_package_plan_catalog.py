"""Plan and apply by hash for task types, agents and rules (CP-ADR-0074 §11, amendment 2026-09-29).

The acceptance of TASK-000903:

- the plan of a package with a task type, an agent and a rule shows three
  additions, the apply writes them, the same plan applied again — ``409
  plan_stale``;
- a key applied the way the installer (package-sdk) applies it (the
  ordinary routes, with the file's spec) plans ``unchanged``: both paths leave
  the same catalog.

Per kind: create, update, unchanged, a stale plan; a field a person changed
is kept unless ``overwriteConsole``; the right of each kind; findings of the
commands; what the core does not plan (``outside``). A package that mixes the
catalog kinds with a process migration that moves deadlines plans both and
writes nothing (TASK-001225).
"""

import copy
from collections.abc import Sequence
from typing import Any

import httpx
import yaml
from sqlalchemy.engine import Engine

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap
from tests.integration.test_package_plan import _apply, _errors, _plan, _plan_and_apply
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import _events, _instance, _journal, _time
from tests.integration.test_process_sla_migration import (
    DUE,
    MIGRATED_AT,
    Still,
    _changed,
    _normal,
    _section,
    _spec,
    _timers,
    _waiting,
    still,
)
from tests.integration.test_process_sla_migration import _package as _deadlines_package

__all__ = ["still"]

TYPE = "sample-triage"
AGENT = "sample-triager"
RULE = "sample-triage-on-appear"
AGENT_PERMISSIONS = ["events.read", "tasks.read", "tasks.write"]


def _type_spec(**change: Any) -> dict[str, Any]:
    return {
        "displayName": "Sample triage",
        "description": "Triage what appeared",
        "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
        "fieldSchema": {"type": "object", "properties": {"number": {"type": "string"}}},
        **change,
    }


def _agent_spec(**change: Any) -> dict[str, Any]:
    return {
        "displayName": "Sample triager",
        "description": "Files triage",
        "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
        "placement": "none",
        **change,
    }


def _rule_spec(**change: Any) -> dict[str, Any]:
    return {
        "description": "Triage each appearance",
        "trigger": {"kind": "observation", "type": "sample.appeared"},
        "action": {
            "kind": "ensure_work",
            "taskType": TYPE,
            "dedupKeyTemplate": "sample:{{payload.data.id}}",
            "fields": {"title": "Triage {{payload.data.id}}"},
        },
        "identity": {"agent": AGENT},
        **change,
    }


def _package(
    *,
    task_type: dict[str, Any] | None = None,
    agent: dict[str, Any] | None = None,
    rule: dict[str, Any] | None = None,
    extra: Sequence[tuple[str, dict[str, Any]]] = (),
    renames: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample-catalog",
        "spec": {"version": "1.0.0", "displayName": "Sample catalog"},
    }
    if renames:
        manifest["spec"]["renames"] = renames
    objects = [
        ("task-types/triage.yaml", "TaskType", TYPE, task_type),
        ("agents/triager.yaml", "Agent", AGENT, agent),
        ("rules/triage.yaml", "WorkRule", RULE, rule),
    ]
    files = [("package.yaml", yaml.safe_dump(manifest))]
    for path, kind, key, spec in objects:
        if spec is not None:
            document = {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}
            files.append((path, yaml.safe_dump(document, sort_keys=False)))
    for path, document in extra:
        files.append((path, yaml.safe_dump(document, sort_keys=False)))
    return {"files": [{"path": path, "content": content} for path, content in files]}


def _full(**changes: dict[str, Any]) -> dict[str, Any]:
    return _package(
        task_type=_type_spec(**changes.get("task_type", {})),
        agent=_agent_spec(**changes.get("agent", {})),
        rule=_rule_spec(**changes.get("rule", {})),
    )


def _changes(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["kind"]: c for c in plan["changes"]}


async def _admin(client: httpx.AsyncClient) -> str:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    return key


async def _get(client: httpx.AsyncClient, api_key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params, headers=auth(api_key))
    assert response.status_code == 200, response.text
    return response.json()


async def _types(client: httpx.AsyncClient, key: str) -> dict[int, dict[str, Any]]:
    page = await _get(client, key, "/task-types", key=TYPE)
    return {item["version"]: item for item in page["items"]}


async def _rule(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    page = await _get(client, key, "/rules", key=RULE)
    [rule] = [r for r in page["items"] if r["status"] != "archived"]
    out: dict[str, Any] = rule
    return out


async def test_a_task_type_an_agent_and_a_rule_are_planned_applied_and_a_stale_plan_is_refused(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    package = _full()
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    assert [(c["kind"], c["key"], c["action"]) for c in plan["changes"]] == [
        ("TaskType", TYPE, "create"),
        ("Agent", AGENT, "create"),
        ("WorkRule", RULE, "create"),
    ]
    fields = {f["path"]: f for f in _changes(plan)["TaskType"]["fields"]}
    assert fields["/spec/displayName"] == {
        "path": "/spec/displayName",
        "before": None,
        "after": "Sample triage",
        "owner": "package",
        "applies": True,
    }
    assert _changes(plan)["TaskType"]["deprecates"] == []
    assert plan["outside"] == []
    # The trial of the commands wrote nothing: the plan did not create the type.
    assert await _types(client, key) == {}

    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == [
        {"kind": "TaskType", "key": TYPE, "action": "create", "version": 1},
        {"kind": "Agent", "key": AGENT, "action": "create", "version": 1},
        {"kind": "WorkRule", "key": RULE, "action": "create", "version": 1},
    ]
    types = await _types(client, key)
    assert (types[1]["displayName"], types[1]["status"]) == ("Sample triage", "active")
    agent = await _get(client, key, f"/agents/{AGENT}")
    assert (agent["currentRevision"], agent["state"]) == (1, "running")
    # The revision names the package it came from, as the installer's does.
    history = await _get(client, key, f"/agents/{AGENT}/revisions")
    assert [i["source"] for i in history["items"]] == [
        {"kind": "package", "package": {"key": "sample-catalog", "version": "1.0.0"}}
    ]
    rule = await _rule(client, key)
    assert (rule["status"], rule["identity"], rule["action"]["taskType"]) == (
        "enabled",
        {"agent": AGENT},
        TYPE,
    )

    # The same plan again: the catalog is not the one it was built on.
    stale = await _apply(client, key, package, plan["planHash"])
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "plan_stale"
    again = await _plan(client, key, package)
    assert [c["action"] for c in again["changes"]] == ["unchanged"] * 3
    assert all(c["fields"] == [] and c["deprecates"] == [] for c in again["changes"])


async def test_each_kind_updates_and_a_change_of_any_kind_makes_the_plan_stale(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())

    changed = _full(
        task_type={"description": "Triage, carefully"},
        agent={"description": "Files triage, carefully"},
        rule={"description": "Triage it", "status": "disabled"},
    )
    plan = await _plan(client, key, changed)
    assert _errors(plan) == [], plan["problems"]
    changes = _changes(plan)
    assert {k: c["action"] for k, c in changes.items()} == {
        "TaskType": "update",
        "Agent": "update",
        "WorkRule": "update",
    }
    assert [f["path"] for f in changes["TaskType"]["fields"]] == ["/spec/description"]
    assert changes["TaskType"]["deprecates"] == [1]
    assert [f["path"] for f in changes["WorkRule"]["fields"]] == [
        "/spec/description",
        "/spec/status",
    ]
    applied = await _apply(client, key, changed, plan["planHash"])
    assert applied.status_code == 200, applied.text
    versions = {a["kind"]: a["version"] for a in applied.json()["applied"]}
    assert versions == {"TaskType": 2, "Agent": 2, "WorkRule": 2}
    types = await _types(client, key)
    assert (types[1]["status"], types[2]["status"]) == ("deprecated", "active")
    assert types[2]["description"] == "Triage, carefully"
    rule = await _rule(client, key)
    assert (rule["status"], rule["description"], rule["version"]) == ("disabled", "Triage it", 2)

    # The desired state of an agent moves without a new revision.
    paused = _full(
        task_type={"description": "Triage, carefully"},
        agent={"description": "Files triage, carefully", "state": "stopped"},
        rule={"description": "Triage it", "status": "disabled"},
    )
    plan = await _plan(client, key, paused)
    changes = _changes(plan)
    assert changes["Agent"]["action"] == "update"
    assert [f["path"] for f in changes["Agent"]["fields"]] == ["/spec/state"]
    applied = await _apply(client, key, paused, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"][1]["version"] == 2
    agent = await _get(client, key, f"/agents/{AGENT}")
    assert (agent["currentRevision"], agent["state"]) == (2, "stopped")

    # Any kind changed by hand between the plan and the apply: plan_stale.
    rule = await _rule(client, key)
    by_hand = [
        ("POST", "/api/v1/task-types", {"key": TYPE, **_type_spec(displayName="By hand")}),
        ("PATCH", f"/api/v1/agents/{AGENT}/state", {"state": "running"}),
        ("POST", f"/api/v1/rules/{rule['id']}:enable", None),
    ]
    for method, path, body in by_hand:
        plan = await _plan(client, key, paused)
        response = await client.request(method, path, json=body, headers=auth(key))
        assert response.status_code in (200, 201), response.text
        stale = await _apply(client, key, paused, plan["planHash"])
        assert stale.status_code == 409, (path, stale.text)
        assert stale.json()["error"]["code"] == "plan_stale"


async def test_a_field_a_person_changed_is_kept_for_every_kind_unless_overwritten(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    # A person publishes the next task type version and renames the rule.
    response = await client.post(
        "/api/v1/task-types",
        json={"key": TYPE, **_type_spec(displayName="Named by hand")},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    rule = await _rule(client, key)
    response = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"description": "Described by hand"},
        headers={**auth(key), "If-Match": f'"rule-{rule["version"]}"'},
    )
    assert response.status_code == 200, response.text

    package = _full(rule={"condition": {"eq": [{"var": "payload.data.kind"}, "sample"]}})
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    changes = _changes(plan)
    # The person's version is kept as it is; the version they left active goes.
    assert changes["TaskType"]["action"] == "unchanged"
    assert changes["TaskType"]["deprecates"] == [1]
    [kept] = changes["TaskType"]["fields"]
    assert (kept["path"], kept["owner"], kept["applies"]) == ("/spec/displayName", "console", False)
    fields = {f["path"]: f for f in changes["WorkRule"]["fields"]}
    assert (fields["/spec/description"]["owner"], fields["/spec/description"]["applies"]) == (
        "console",
        False,
    )
    assert fields["/spec/condition"]["owner"] == "package"
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    types = await _types(client, key)
    assert (types[1]["status"], types[2]["status"]) == ("deprecated", "active")
    assert types[2]["displayName"] == "Named by hand"
    rule = await _rule(client, key)
    assert rule["description"] == "Described by hand"

    plan = await _plan(client, key, package, overwriteConsole=True)
    changes = _changes(plan)
    assert changes["TaskType"]["action"] == "update"
    assert changes["TaskType"]["fields"][0]["applies"] is True
    applied = await _apply(client, key, package, plan["planHash"], overwriteConsole=True)
    assert applied.status_code == 200, applied.text
    types = await _types(client, key)
    assert types[3]["displayName"] == "Sample triage"
    assert types[2]["status"] == "deprecated"
    assert (await _rule(client, key))["description"] == "Triage each appearance"


async def test_what_the_installer_applied_plans_unchanged(client: httpx.AsyncClient) -> None:
    """The installer (package-sdk) sends the file's spec to the ordinary routes."""
    key = await _admin(client)
    installed = [
        ("/api/v1/task-types", {"key": TYPE, **_type_spec()}),
        ("/api/v1/agents", {"key": AGENT, "spec": _agent_spec()}),
        ("/api/v1/rules", {"key": RULE, **_rule_spec()}),
    ]
    for path, body in installed:
        response = await client.post(path, json=body, headers=auth(key))
        assert response.status_code == 201, response.text
    plan = await _plan(client, key, _full())
    assert _errors(plan) == [], plan["problems"]
    assert [(c["action"], c["fields"], c["deprecates"]) for c in plan["changes"]] == [
        ("unchanged", [], [])
    ] * 3
    # A task type the file leaves without instructions keeps the ones it has.
    response = await client.post(
        "/api/v1/task-types",
        json={"key": TYPE, **_type_spec(), "instructions": "Read the sample first"},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    plan = await _plan(client, key, _full())
    [task_type, *_] = plan["changes"]
    assert (task_type["action"], task_type["fields"], task_type["deprecates"]) == (
        "unchanged",
        [],
        [1],
    )
    applied = await _apply(client, key, _full(), plan["planHash"])
    assert applied.status_code == 200, applied.text
    types = await _types(client, key)
    assert (types[1]["status"], types[2]["instructions"]) == (
        "deprecated",
        "Read the sample first",
    )


async def test_the_apply_needs_the_right_of_every_kind_and_writes_all_or_nothing(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    package = _full()
    _, planner = await create_agent_with_key(
        client, key, name="planner", permissions=["packages.plan"]
    )
    plan = await _plan(client, planner, package)
    [warning] = [p for p in plan["problems"] if p["code"] == "permission_required"]
    assert (warning["severity"], warning["file"]) == ("warning", "task-types/triage.yaml")
    refused = await _apply(client, planner, package, plan["planHash"])
    assert refused.status_code == 403, refused.text

    rights = ["packages.plan", "task_types.manage", "task_types.read", *AGENT_PERMISSIONS]
    for name, file in (("types", "agents/triager.yaml"), ("agents", "rules/triage.yaml")):
        granted = rights + (["agents.manage", "agents.read"] if name == "agents" else [])
        _, principal = await create_agent_with_key(client, key, name=name, permissions=granted)
        plan = await _plan(client, principal, package)
        assert _errors(plan) == [], plan["problems"]
        [warning] = [p for p in plan["problems"] if p["code"] == "permission_required"]
        assert warning["file"] == file
        refused = await _apply(client, principal, package, plan["planHash"])
        assert refused.status_code == 403, refused.text
        # All or nothing: the task type the apply wrote first is gone with it.
        assert await _types(client, key) == {}


async def test_the_plan_names_what_a_command_would_refuse_and_what_it_does_not_plan(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    notification = {
        "apiVersion": API_VERSION,
        "kind": "NotificationRule",
        "key": "sample-notify",
        "spec": {"on": {"event": "task.created"}},
    }
    skill = {
        "apiVersion": API_VERSION,
        "kind": "Role",
        "key": "sample-role",
        "spec": {"name": "Sample role"},
    }
    unknown_type = copy.deepcopy(_rule_spec())
    unknown_type["action"]["taskType"] = "sample-missing"
    package = _package(
        rule=unknown_type,
        extra=[("notification-rules/n.yaml", notification), ("roles/r.yaml", skill)],
        renames=[{"kind": "TaskType", "from": "sample-old", "to": TYPE}],
    )
    plan = await _plan(client, key, package)
    assert plan["outside"] == [
        {"kind": "NotificationRule", "key": "sample-notify", "appliedBy": "notification-service"},
        {"kind": "Role", "key": "sample-role", "appliedBy": "installer"},
    ]
    problems = {p["code"]: p for p in plan["problems"]}
    # The rule acts as an agent the package does not bring and files an unknown type.
    [refused] = [p for p in plan["problems"] if p["file"] == "rules/triage.yaml"]
    assert refused["severity"] == "error", refused
    assert (refused["code"], refused["path"]) == ("unknown_task_type", "/spec/action/taskType")
    assert problems["rename_not_planned"]["severity"] == "warning"

    # A shape the route refuses is a finding with its path.
    bad = _package(task_type={"lifecycleSchema": SYSTEM_TASK_LIFECYCLE, "colour": "red"})
    plan = await _plan(client, key, bad)
    shapes = {(p["file"], p["path"]) for p in plan["problems"] if p["code"] == "invalid_task_type"}
    assert shapes == {
        ("task-types/triage.yaml", "/spec/displayName"),
        ("task-types/triage.yaml", "/spec/colour"),
    }
    invalid = await _apply(client, key, bad, plan["planHash"])
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_package"


async def test_fractional_cpus_are_a_finding_of_the_plan_and_of_the_check(
    client: httpx.AsyncClient,
) -> None:
    """Amendment 2026-10-03 of CP-ADR-0073: the field and its path, before the revision hash."""
    key = await _admin(client)
    placed = {
        "executor": {"kind": "skills"},
        "placement": {"resources": {"cpus": 0.5, "memoryMb": 1024}},
    }
    package = _package(agent=_agent_spec(**placed))
    plan = await _plan(client, key, package)
    [problem] = [p for p in plan["problems"] if p["file"] == "agents/triager.yaml"]
    assert (problem["code"], problem["severity"], problem["path"]) == (
        "invalid_agent",
        "error",
        "/spec/placement/resources/cpus",
    )
    assert "whole number" in problem["message"]
    refused = await _apply(client, key, package, plan["planHash"])
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_package"
    page = await _get(client, key, "/agents", limit=10)
    assert [a["key"] for a in page["items"] if a["key"] == AGENT] == []

    checked = await client.post(
        "/api/v1/packages:test?checkOnly=true", json={"package": package}, headers=auth(key)
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "invalid"
    problems = checked.json()["problems"]
    assert [(p["code"], p["path"]) for p in problems if p["file"] == "agents/triager.yaml"] == [
        ("invalid_agent", "/spec/placement/resources/cpus")
    ]

    # Whole CPUs plan and apply.
    placed["placement"]["resources"]["cpus"] = 1
    await _plan_and_apply(client, key, _package(agent=_agent_spec(**placed)))


async def test_a_retired_agent_and_another_workspace_of_a_rule_are_refused(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    response = await client.post(
        f"/api/v1/agents/{AGENT}:retire", json={"reason": "gone"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    workspace = await create_workspace(client, key, "elsewhere")
    moved = _full(rule={"workspaceId": workspace["id"]})
    plan = await _plan(client, key, moved)
    problems = {p["code"]: p for p in plan["problems"] if p["severity"] == "error"}
    assert set(problems) == {"agent_retired", "rule_workspace_immutable"}
    assert problems["agent_retired"]["file"] == "agents/triager.yaml"
    assert problems["rule_workspace_immutable"]["path"] == "/spec/workspaceId"
    refused = await _apply(client, key, moved, plan["planHash"])
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_package"


async def test_a_record_of_the_installer_hands_every_field_back_to_the_package(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0074 E6: the installer overwrote the object through its route."""
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    agent = await _get(client, key, f"/agents/{AGENT}")
    assert (agent["package"]["key"], agent["package"]["version"]) == ("sample-catalog", "1.0.0")
    response = await client.post(
        "/api/v1/task-types",
        json={"key": TYPE, **_type_spec(displayName="Named by hand")},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    [kept] = _changes(await _plan(client, key, _full()))["TaskType"]["fields"]
    assert (kept["owner"], kept["applies"]) == ("console", False)

    response = await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": "sample-catalog", "version": "1.0.1"},
            "objects": [{"kind": "TaskType", "key": TYPE}],
        },
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    change = _changes(await _plan(client, key, _full()))["TaskType"]
    assert change["action"] == "update"
    [field] = change["fields"]
    assert (field["path"], field["owner"], field["applies"]) == (
        "/spec/displayName",
        "package",
        True,
    )


async def test_an_object_of_another_package_is_a_warning_of_the_plan(
    client: httpx.AsyncClient,
) -> None:
    """The apply links every object of its plan to its package: a move is shown."""
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    package = _full()
    manifest = yaml.safe_load(package["files"][0]["content"])
    manifest["key"] = "other-catalog"
    package["files"][0]["content"] = yaml.safe_dump(manifest)

    plan = await _plan(client, key, package)
    assert _errors(plan) == []
    moved = [p for p in plan["problems"] if p["code"] == "package_owner_changed"]
    assert [(p["severity"], p["file"]) for p in moved] == [
        ("warning", "agents/triager.yaml"),
        ("warning", "rules/triage.yaml"),
        ("warning", "task-types/triage.yaml"),
    ]
    assert "sample-catalog" in moved[0]["message"] and "other-catalog" in moved[0]["message"]

    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    agent = await _get(client, key, f"/agents/{AGENT}")
    assert agent["package"]["key"] == "other-catalog"
    again = await _plan(client, key, package)
    assert [p for p in again["problems"] if p["code"] == "package_owner_changed"] == []


async def test_a_gate_addressed_to_a_role_needs_the_role_of_the_package(
    client: httpx.AsyncClient,
) -> None:
    """``role:<slug>`` is resolved when the type is published (CP-ADR-0061, 2026-10-01)."""
    key = await _admin(client)
    gated = _type_spec(
        completionSchema={
            "onComplete": {
                "actions": [
                    {
                        "ensureWork": {
                            "type": TYPE,
                            "key": "after:$.task.id",
                            "title": "After $.task.publicId",
                            "requestApproval": {"assignee": "role:sample-approvers"},
                        }
                    }
                ]
            }
        }
    )
    plan = await _plan(client, key, _package(task_type=gated))
    [refused] = [p for p in plan["problems"] if p["severity"] == "error"]
    assert (refused["code"], refused["file"], refused["path"]) == (
        "unknown_role",
        "task-types/triage.yaml",
        "/spec/completionSchema/onComplete/actions/0/ensureWork/requestApproval/assignee",
    )

    role = {
        "apiVersion": API_VERSION,
        "kind": "Role",
        "key": "sample-approvers",
        "spec": {"name": "Sample approvers"},
    }
    plan = await _plan(client, key, _package(task_type=gated, extra=[("roles/a.yaml", role)]))
    assert [p for p in plan["problems"] if p["severity"] == "error"] == []


async def test_a_plan_of_catalog_kinds_and_a_migration_with_deadlines_writes_nothing(
    client: httpx.AsyncClient, sync_engine: Engine, still: Still
) -> None:
    """The section ``deadlines`` is counted beside the trial of the catalog kinds, all rolled back.

    One package brings a task type and an agent (``shapes``, ``_trial``) and the
    version of a process that migrates its open instances to a shorter due
    (``_deadlines``). The plan lists both; the catalog, the instances, their
    journals and timers are as they were; the apply of that plan counts the
    deadlines the plan listed.
    """
    s, instances = await _waiting(client, still)
    key = s["key"]
    journals = {iid: await _journal(client, key, iid) for iid in instances.values()}
    timers = {iid: _timers(sync_engine, iid) for iid in instances.values()}

    still.at = MIGRATED_AT
    process = _deadlines_package(_spec(s["admin"], version=2, workdays=1, policy="migrate"))
    catalog = _package(task_type=_type_spec(), agent=_agent_spec())
    package = {
        "files": process["files"] + [f for f in catalog["files"] if f["path"] != "package.yaml"]
    }
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    assert {(c["kind"], c["key"], c["action"]) for c in plan["changes"]} >= {
        ("TaskType", TYPE, "create"),
        ("Agent", AGENT, "create"),
    }
    planned = _section(plan)
    assert {(d["instanceId"], d["element"]) for d in planned} == {
        (iid, "review") for iid in instances.values()
    }
    for name, iid in instances.items():
        [deadline] = [d for d in planned if d["instanceId"] == iid]
        assert _time(deadline["dueAt"]) == _time(DUE[name])
        assert deadline["breached"] is (name != "HALF")

    # The transaction of the plan rolled back: nothing of either part is written.
    assert await _types(client, key) == {}
    missing = await client.get(f"/api/v1/agents/{AGENT}", headers=auth(key))
    assert missing.status_code == 404, missing.text
    for iid in instances.values():
        assert (await _instance(client, key, iid))["definitionVersion"] == 1
        assert await _journal(client, key, iid) == journals[iid]
        assert _timers(sync_engine, iid) == timers[iid]
    assert not await _events(client, key, "process.sla_breached")

    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert {(a["kind"], a["action"]) for a in applied.json()["applied"]} >= {
        ("TaskType", "create"),
        ("Agent", "create"),
    }
    assert (await _instance(client, key, instances["HALF"]))["definitionVersion"] == 2
    recorded = [
        d for iid in instances.values() for d in _changed(await _journal(client, key, iid), iid)
    ]
    assert _normal(planned) == _normal(recorded)
