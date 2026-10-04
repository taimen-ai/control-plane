"""Executor roles of a task type and who may take it in a workspace
(CP-ADR-0048, amendment 2026-10-03: A1-A3, TASK-001307)."""

import asyncio
import copy
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)
from tests.integration.test_agent_registry import _fixture, _link, _publish, coder_spec

TYPE = "coding-task"


async def _type(
    client: httpx.AsyncClient, key: str, type_key: str = TYPE, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": type_key.title(), **extra},
        headers=auth(key),
    )


async def _created_type(
    client: httpx.AsyncClient, key: str, type_key: str = TYPE, **extra: Any
) -> dict[str, Any]:
    response = await _type(client, key, type_key, **extra)
    assert response.status_code == 201, response.text
    return response.json()


async def _executors(
    client: httpx.AsyncClient, key: str, type_id: str, workspace_id: str
) -> httpx.Response:
    return await client.get(
        f"/api/v1/task-types/{type_id}/executors",
        params={"workspaceId": workspace_id},
        headers=auth(key),
    )


async def _by_principal(
    client: httpx.AsyncClient, key: str, type_id: str, workspace_id: str
) -> dict[str, dict[str, Any]]:
    response = await _executors(client, key, type_id, workspace_id)
    assert response.status_code == 200, response.text
    return {e["principalId"]: e for e in response.json()["items"]}


async def _person(
    client: httpx.AsyncClient, key: str, name: str, *, member_of: str | None = None
) -> dict[str, Any]:
    principal, _ = await create_agent_with_key(client, key, name=name, kind="human")
    if member_of is not None:
        response = await client.post(
            f"/api/v1/workspaces/{member_of}/members",
            json={"principalId": principal["id"]},
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
    return principal


async def _agent(
    client: httpx.AsyncClient, key: str, agent: str, spec: dict[str, Any], *, linked: bool = True
) -> str | None:
    published = await _publish(client, key, spec, agent=agent)
    assert published.status_code == 201, published.text
    if not linked:
        return None
    response = await _link(client, key, agent=agent)
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


# --- A1: executorRoles of a version ---------------------------------------------


async def test_executor_roles_are_kept_by_the_version(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    await create_role(client, key, "developer")
    # A role of one workspace is a role of the tenant too.
    await create_role(client, key, "reviewer", workspace_id=ws["id"])

    created = await _created_type(client, key, executorRoles=["developer", "reviewer"])
    assert created["executorRoles"] == ["developer", "reviewer"]
    got = await client.get(f"/api/v1/task-types/{created['id']}", headers=auth(key))
    assert got.json()["executorRoles"] == ["developer", "reviewer"]
    events = await client.get(
        "/api/v1/events", params={"types": "task_type.created"}, headers=auth(key)
    )
    event = next(e for e in events.json()["items"] if e["entityId"] == created["id"])
    assert event["payload"]["executorRoles"] == ["developer", "reviewer"]
    assert event["schemaVersion"] == 3

    # Absent and empty: people are not restricted.
    assert (await _created_type(client, key, "plain"))["executorRoles"] == []
    assert (await _created_type(client, key, "plain", executorRoles=[]))["executorRoles"] == []


async def test_an_unknown_executor_role_is_refused(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_role(client, key, "developer")

    refused = await _type(client, key, executorRoles=["developer", "astronaut"])
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "unknown_role"
    assert error["details"] == {"field": "executorRoles[1]", "role": "astronaut"}
    # Nothing was published.
    listed = await client.get("/api/v1/task-types", params={"key": TYPE}, headers=auth(key))
    assert listed.json()["items"] == []


async def test_a_malformed_executor_roles_list_is_an_invalid_request(
    client: httpx.AsyncClient,
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_role(client, key, "developer")
    for roles in (
        "developer",
        [None],
        [42],
        ["developer", "developer"],
        ["Developer"],
        [""],
        [f"r{i:02d}" for i in range(21)],
    ):
        response = await _type(client, key, executorRoles=roles)
        assert response.status_code == 400, (roles, response.text)
        assert response.json()["error"]["code"] == "invalid_request"
    assert (await _type(client, key, executorRoles=None)).status_code == 400


# --- A2: people ---------------------------------------------------------------------


async def test_people_with_and_without_an_executor_role(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    developer = await create_role(client, key, "developer")
    designer = await create_role(client, key, "designer")
    fits = await _person(client, key, "Fits", member_of=ws["id"])
    other_role = await _person(client, key, "Other role", member_of=ws["id"])
    no_role = await _person(client, key, "No role", member_of=ws["id"])
    outsider = await _person(client, key, "Outsider")
    await assign_role(client, key, fits["id"], developer["id"])
    await assign_role(client, key, other_role["id"], designer["id"])
    # Holds the role, takes no part in the workspace.
    await assign_role(client, key, outsider["id"], developer["id"])

    restricted = await _created_type(client, key, executorRoles=["developer"])
    got = await _by_principal(client, key, restricted["id"], ws["id"])
    assert set(got) == {fits["id"]}
    assert got[fits["id"]] == {
        "principalId": fits["id"],
        "kind": "human",
        "displayName": "Fits",
        "roles": ["developer"],
        "reason": "role",
    }

    # Empty executorRoles: every participant who is a person.
    open_type = await _created_type(client, key, "open-task")
    got = await _by_principal(client, key, open_type["id"], ws["id"])
    assert set(got) == {fits["id"], other_role["id"], no_role["id"]}
    assert {e["reason"] for e in got.values()} == {"any"}
    assert got[no_role["id"]]["roles"] == []


async def test_the_scope_of_an_executor_role(client: httpx.AsyncClient) -> None:
    """The slug resolves as a role:<slug> gate of the workspace would; the assignment
    counts tenant-wide or on the workspace or an ancestor of it."""
    key = (await do_bootstrap(client))["apiKey"]["key"]
    parent = await create_workspace(client, key, "eng")
    child = await create_workspace(client, key, "backend", parent_id=parent["id"])
    sibling = await create_workspace(client, key, "frontend", parent_id=parent["id"])
    tenant_role = await create_role(client, key, "developer")
    child_role = await create_role(client, key, "developer", workspace_id=child["id"])
    sibling_role = await create_role(client, key, "operator", workspace_id=sibling["id"])
    task_type = await _created_type(client, key, executorRoles=["developer", "operator"])

    on_parent = await _person(client, key, "On parent", member_of=child["id"])
    await assign_role(client, key, on_parent["id"], child_role["id"], workspace_id=parent["id"])
    on_child = await _person(client, key, "On child")
    await assign_role(client, key, on_child["id"], child_role["id"], workspace_id=child["id"])
    # The child's own developer role shadows the tenant-wide one there.
    shadowed = await _person(client, key, "Shadowed", member_of=child["id"])
    await assign_role(client, key, shadowed["id"], tenant_role["id"])
    # A role of the sibling workspace does not count in the child.
    elsewhere = await _person(client, key, "Elsewhere", member_of=child["id"])
    await assign_role(client, key, elsewhere["id"], sibling_role["id"], workspace_id=sibling["id"])

    got = await _by_principal(client, key, task_type["id"], child["id"])
    assert set(got) == {on_parent["id"], on_child["id"]}
    assert {e["roles"][0] for e in got.values()} == {"developer"}

    # In the parent the tenant-wide developer counts.
    tenant_wide = await _person(client, key, "Tenant wide", member_of=parent["id"])
    await assign_role(client, key, tenant_wide["id"], tenant_role["id"])
    got = await _by_principal(client, key, task_type["id"], parent["id"])
    assert set(got) == {tenant_wide["id"]}


async def test_an_inactive_person_is_not_offered(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    away = await _person(client, key, "Away", member_of=ws["id"])
    task_type = await _created_type(client, key)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"), {"id": away["id"]}
        )
    assert await _by_principal(client, key, task_type["id"], ws["id"]) == {}


# --- A2: agents and services -------------------------------------------------------


async def test_agents_by_task_types_and_workspace(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    parent = await create_workspace(client, key, "eng")
    child = await create_workspace(client, key, "backend", parent_id=parent["id"])
    other = await create_workspace(client, key, "sales")
    await create_role(client, key, "coder")
    await create_role(client, key, "developer")
    task_type = await _created_type(client, key, executorRoles=["developer"])
    await _created_type(client, key, "design-task")

    def spec(workspace: str | None, task_types: list[str] | None) -> dict[str, Any]:
        document = coder_spec(workspace or child["id"])
        if workspace is None:
            del document["work"]["workspace"]
        if task_types is None:
            del document["work"]["taskTypes"]
        else:
            document["work"]["taskTypes"] = task_types
        return document

    named = await _agent(client, key, "named", spec(child["id"], [TYPE, "design-task"]))
    any_type = await _agent(client, key, "any-type", spec(child["id"], None))
    empty_list = await _agent(client, key, "empty-list", spec(child["id"], []))
    from_parent = await _agent(client, key, "from-parent", spec(parent["id"], [TYPE]))
    tenant_wide = await _agent(client, key, "tenant-wide", spec(None, [TYPE]))
    await _agent(client, key, "other-type", spec(child["id"], ["design-task"]))
    await _agent(client, key, "other-workspace", spec(other["id"], [TYPE]))
    await _agent(client, key, "unlinked", spec(child["id"], [TYPE]), linked=False)

    got = await _by_principal(client, key, task_type["id"], child["id"])
    assert set(got) == {named, any_type, empty_list, from_parent, tenant_wide}
    assert got[named] == {
        "principalId": named,
        "kind": "agent",
        "displayName": "Autonomous coder",
        "roles": [],
        "reason": "agent_task_types",
    }
    assert got[any_type]["reason"] == got[empty_list]["reason"] == "agent_any"
    assert got[from_parent]["reason"] == "agent_task_types"

    # A child's agent does not cover its parent.
    got = await _by_principal(client, key, task_type["id"], parent["id"])
    assert set(got) == {from_parent, tenant_wide}
    # Nor a sibling tree.
    got = await _by_principal(client, key, task_type["id"], other["id"])
    assert set(got) == {tenant_wide, (await _principal_of(client, key, "other-workspace"))}


async def _principal_of(client: httpx.AsyncClient, key: str, agent: str) -> str:
    response = await client.get(f"/api/v1/agents/{agent}", headers=auth(key))
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


async def test_stopped_and_retired_agents_are_not_offered(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    await create_role(client, key, "coder")
    task_type = await _created_type(client, key)
    document = coder_spec(ws["id"])
    document["work"]["taskTypes"] = [TYPE]
    running = await _agent(client, key, "running", document)
    stopped = await _agent(client, key, "stopped", document)
    retired = await _agent(client, key, "retired", document)
    assert set(await _by_principal(client, key, task_type["id"], ws["id"])) == {
        running,
        stopped,
        retired,
    }

    response = await client.patch(
        "/api/v1/agents/stopped/state", json={"state": "stopped"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    response = await client.post(
        "/api/v1/agents/retired:retire", json={"reason": "replaced"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text

    assert set(await _by_principal(client, key, task_type["id"], ws["id"])) == {running}


async def test_services_take_work_only_through_the_registry(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    task_type = await _created_type(client, key)
    bridge = copy.deepcopy(_fixture("process-bridge.yaml")["spec"])
    # A service account without work takes nothing.
    await _agent(client, key, "process-bridge", bridge)
    # The same service describing its work takes the type like an agent.
    worker_spec = {**copy.deepcopy(bridge), "work": {"workspace": ws["id"], "taskTypes": [TYPE]}}
    worker = await _agent(client, key, "service-worker", worker_spec)
    # A service principal outside the registry, even a member, declares no work.
    loose, _ = await create_agent_with_key(client, key, name="Loose service", kind="service")
    response = await client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"principalId": loose["id"]},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text

    got = await _by_principal(client, key, task_type["id"], ws["id"])
    assert set(got) == {worker}
    assert (got[worker]["kind"], got[worker]["reason"]) == ("service", "agent_task_types")


# --- A2: the answer as a whole ------------------------------------------------------


async def test_people_come_first_and_the_answer_is_stable(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    await create_role(client, key, "coder")
    task_type = await _created_type(client, key)
    bob = await _person(client, key, "Bob", member_of=ws["id"])
    alice = await _person(client, key, "Alice", member_of=ws["id"])
    document = coder_spec(ws["id"])
    document["work"]["taskTypes"] = [TYPE]
    agent = await _agent(client, key, "coder", document)

    responses = await asyncio.gather(
        *(_executors(client, key, task_type["id"], ws["id"]) for _ in range(3))
    )
    bodies = [r.json() for r in responses]
    assert all(r.status_code == 200 for r in responses)
    assert bodies[0] == bodies[1] == bodies[2]
    assert [e["principalId"] for e in bodies[0]["items"]] == [alice["id"], bob["id"], agent]


async def test_an_empty_workspace_has_no_executors(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    task_type = await _created_type(client, key)
    response = await _executors(client, key, task_type["id"], ws["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


async def test_executors_permissions_and_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, key, "eng")
    task_type = await _created_type(client, key)

    # The right of participants, no task_types.read and no /roles needed.
    _, no_org = await create_agent_with_key(
        client, key, name="a", permissions=["workspaces.read", "task_types.read"]
    )
    assert (await _executors(client, no_org, task_type["id"], ws["id"])).status_code == 403
    _, no_workspaces = await create_agent_with_key(client, key, name="b", permissions=["org.read"])
    assert (await _executors(client, no_workspaces, task_type["id"], ws["id"])).status_code == 403
    _, reader = await create_agent_with_key(
        client, key, name="c", permissions=["workspaces.read", "principals.read"]
    )
    assert (await _executors(client, reader, task_type["id"], ws["id"])).status_code == 200

    # Another tenant's type or workspace looks like a missing one.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    missing = "00000000-0000-0000-0000-000000000000"
    for type_id, workspace_id in ((task_type["id"], ws["id"]), (missing, ws["id"])):
        response = await _executors(client, key_b, type_id, workspace_id)
        assert response.status_code == 404, response.text
    foreign_ws = await _executors(client, key, task_type["id"], missing)
    assert foreign_ws.status_code == 404

    # workspaceId is required and must be an id.
    url = f"/api/v1/task-types/{task_type['id']}/executors"
    assert (await client.get(url, headers=auth(key))).status_code == 400
    bad = await client.get(url, params={"workspaceId": "eng"}, headers=auth(key))
    assert bad.status_code == 400


# --- A1: a package keeps the roles a person gave the type --------------------------


async def test_a_package_without_executor_roles_keeps_those_of_the_latest_version(
    client: httpx.AsyncClient,
) -> None:
    from tests.integration.test_package_plan import _apply, _errors, _plan, _plan_and_apply
    from tests.integration.test_package_plan_catalog import TYPE as PACKAGE_TYPE
    from tests.integration.test_package_plan_catalog import _package, _type_spec

    key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_role(client, key, "triager")
    package = _package(task_type=_type_spec())
    await _plan_and_apply(client, key, package)
    by_hand = await _type(
        client, key, PACKAGE_TYPE, **{**_type_spec(), "executorRoles": ["triager"]}
    )
    assert by_hand.status_code == 201, by_hand.text

    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    [change] = plan["changes"]
    assert change["action"] == "unchanged"
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    listed = await client.get(
        "/api/v1/task-types", params={"key": PACKAGE_TYPE, "status": "active"}, headers=auth(key)
    )
    [active] = listed.json()["items"]
    assert (active["version"], active["executorRoles"]) == (2, ["triager"])


async def test_a_package_with_malformed_executor_roles_is_refused_with_the_path(
    client: httpx.AsyncClient,
) -> None:
    from tests.integration.test_package_plan import _apply, _plan
    from tests.integration.test_package_plan_catalog import _package, _type_spec

    key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_role(client, key, "triager")
    for roles, path in (
        (["triager", 5], "/spec/executorRoles/1"),
        (["triager", None], "/spec/executorRoles/1"),
        (["triager", "triager"], "/spec/executorRoles"),
        (["Triager"], "/spec/executorRoles/0"),
        ("triager", "/spec/executorRoles"),
        ({"triager": True}, "/spec/executorRoles"),
        (None, "/spec/executorRoles"),
    ):
        package = _package(task_type={**_type_spec(), "executorRoles": roles})
        plan = await _plan(client, key, package)
        errors = [(p["code"], p["path"]) for p in plan["problems"] if p["severity"] == "error"]
        assert errors == [("invalid_task_type", path)], (roles, errors)
        refused = await _apply(client, key, package, plan["planHash"])
        assert refused.status_code == 422, (roles, refused.text)

    # A well-formed list goes into the version as it was sent.
    package = _package(task_type={**_type_spec(), "executorRoles": ["triager"]})
    plan = await _plan(client, key, package)
    assert [p for p in plan["problems"] if p["severity"] == "error"] == []
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
