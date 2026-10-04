"""Tails of the T003 review closed in T004 (CP-ADR-0082 B5, V1-V3).

* the registry's way to a binding — ``identity:replace`` with ``agents.manage``
  — does not let a person in ``members`` mode make a tenant-wide binding (B5);
* revoking the one ``members`` binding of a person does not widen their API
  keys to the whole tenant (V1);
* a rule acts with its person's snapshot within that person's visibility as
  it stands now: it does not file work where the person sees nothing (V2).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap
from tests.integration.test_agent_identity_replace import (
    KEY,
    SERVICE_SPEC,
    _bindings,
    _registry_identity,
    _replace,
)
from tests.integration.test_agent_registry import _link, _publish
from tests.integration.test_iam_enforcement import ISSUER


def identity() -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
    }


async def _person(
    client: httpx.AsyncClient, admin: str, permissions: list[str], *, member_of: str | None
) -> tuple[str, str]:
    principal, key = await create_agent_with_key(
        client, admin, name="ann", permissions=permissions, kind="human"
    )
    if member_of is not None:
        added = await client.post(
            f"/api/v1/workspaces/{member_of}/members",
            json={"principalId": principal["id"]},
            headers=auth(admin),
        )
        assert added.status_code == 201, added.text
    return principal["id"], key


async def _bind(
    client: httpx.AsyncClient, admin: str, principal: str, who: dict[str, str], **extra: Any
) -> dict[str, Any]:
    bound = await client.post(
        f"/api/v1/principals/{principal}/iam-bindings",
        json={**who, "permissions": ["tasks.read"], **extra},
        headers=auth(admin),
    )
    assert bound.status_code in (200, 201), bound.text
    binding: dict[str, Any] = bound.json()
    return binding


async def _listed(client: httpx.AsyncClient, key: str) -> set[str]:
    response = await client.get("/api/v1/tasks", params={"limit": 200}, headers=auth(key))
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


# --- B5 on the registry's path ------------------------------------------------------


async def test_members_does_not_move_a_service_agent_to_a_new_identity(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    published = await _publish(client, admin, SERVICE_SPEC, agent=KEY)
    assert published.status_code == 201, published.text
    first = identity()
    linked = await _link(client, admin, agent=KEY, **first)
    assert linked.status_code == 200, linked.text
    service = linked.json()["principalId"]
    dept = await create_workspace(client, admin, "dept")
    manager = ["agents.manage", "agents.read", "events.read", "tasks.read"]
    person, key = await _person(client, admin, manager, member_of=dept["id"])
    await _bind(client, admin, person, identity(), visibility="members")

    refused = await _replace(client, key, identity())
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert error["code"] == "visibility_escalation"
    assert error["details"] == {"errors": [{"path": "/visibility"}]}
    # Nothing moved: the registry, the bindings of the service.
    assert _registry_identity(sync_engine) == (
        ISSUER,
        first["iamTenantId"],
        first["iamPrincipalId"],
    )
    bindings = await _bindings(client, admin, service)
    assert list(bindings) == [first["iamPrincipalId"]]
    assert bindings[first["iamPrincipalId"]]["status"] == "active"

    # The same person in tenant mode may: the rule is about the visibility.
    tenant_wide = identity()
    await _bind(client, admin, person, tenant_wide, visibility="tenant")
    narrowing = next(
        b
        for b in (
            await client.get(f"/api/v1/principals/{person}/iam-bindings", headers=auth(admin))
        ).json()["items"]
        if b["visibility"] == "members"
    )
    revoked = await client.post(
        f"/api/v1/iam-bindings/{narrowing['id']}:revoke", headers=auth(admin)
    )
    assert revoked.status_code == 200, revoked.text
    replaced = await _replace(client, key, identity())
    assert replaced.status_code == 200, replaced.text


async def test_members_does_not_link_an_agent_to_its_identity(
    client: httpx.AsyncClient,
) -> None:
    """``PUT /agents/{key}/identity`` makes the same tenant-wide binding (V3)."""
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _publish(client, admin, SERVICE_SPEC, agent=KEY)).status_code == 201
    dept = await create_workspace(client, admin, "dept")
    placement = ["agents.status.write", "agents.read", "tasks.read"]
    person, key = await _person(client, admin, placement, member_of=dept["id"])
    await _bind(client, admin, person, identity(), visibility="members")

    refused = await _link(client, key, agent=KEY, **identity())
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert error["code"] == "visibility_escalation"
    assert error["details"] == {"errors": [{"path": "/visibility"}]}
    agent = await client.get(f"/api/v1/agents/{KEY}", headers=auth(admin))
    assert agent.status_code == 200, agent.text
    assert agent.json().get("principalId") is None
    # The administrator in tenant mode links it as before.
    assert (await _link(client, admin, agent=KEY, **identity())).status_code == 200


async def test_a_tenant_mode_caller_still_moves_a_service_agent(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _publish(client, admin, SERVICE_SPEC, agent=KEY)).status_code == 201
    assert (await _link(client, admin, agent=KEY, **identity())).status_code == 200
    replaced = await _replace(client, admin, identity())
    assert replaced.status_code == 200, replaced.text


# --- V1: API keys after the last members binding ---------------------------------------


async def test_revoking_the_only_members_binding_does_not_widen_the_api_keys(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    dept = await create_workspace(client, admin, "dept")
    other = await create_workspace(client, admin, "other")
    tasks = {
        name: (
            await client.post(
                "/api/v1/tasks",
                json={"title": name, "workspaceId": ws["id"]},
                headers=auth(admin),
            )
        ).json()["id"]
        for name, ws in (("dept", dept), ("other", other))
    }
    person, key = await _person(client, admin, ["tasks.read"], member_of=dept["id"])
    assert await _listed(client, key) >= set(tasks.values())  # no binding yet: tenant

    who = identity()
    binding = await _bind(client, admin, person, who, visibility="members")
    assert await _listed(client, key) == {tasks["dept"]}

    revoked = await client.post(f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin))
    assert revoked.status_code == 200, revoked.text
    # The key is the only way in left: it keeps the narrowest visibility.
    assert await _listed(client, key) == {tasks["dept"]}

    # Widening is an explicit decision: the identity reopened as tenant-wide...
    await _bind(client, admin, person, who, visibility="tenant")
    assert await _listed(client, key) >= set(tasks.values())
    # ...and revoked again: no binding of the person narrows any more.
    revoked = await client.post(f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin))
    assert revoked.status_code == 200, revoked.text
    assert await _listed(client, key) >= set(tasks.values())


async def test_an_active_tenant_binding_beside_a_revoked_members_one_is_tenant(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    dept = await create_workspace(client, admin, "dept")
    other = await create_workspace(client, admin, "other")
    theirs = (
        await client.post(
            "/api/v1/tasks", json={"title": "x", "workspaceId": other["id"]}, headers=auth(admin)
        )
    ).json()["id"]
    person, key = await _person(client, admin, ["tasks.read"], member_of=dept["id"])
    narrowing = await _bind(client, admin, person, identity(), visibility="members")
    await _bind(client, admin, person, identity(), visibility="tenant")
    assert theirs not in await _listed(client, key)
    revoked = await client.post(
        f"/api/v1/iam-bindings/{narrowing['id']}:revoke", headers=auth(admin)
    )
    assert revoked.status_code == 200, revoked.text
    # The active bindings decide while there are any: the one left is tenant-wide.
    assert theirs in await _listed(client, key)


# --- V2: a rule of a person in members mode ----------------------------------------------


FOUND = "sample.found"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def test_a_rule_does_not_file_work_where_its_person_sees_nothing(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={"key": "sample-work", "displayName": "W", "lifecycleSchema": SYSTEM_TASK_LIFECYCLE},
        headers=auth(admin),
    )
    assert created.status_code == 201, created.text
    dept = await create_workspace(client, admin, "dept")
    other = await create_workspace(client, admin, "other")
    author = ["rules.read", "rules.write", "tasks.read", "tasks.write", "events.read"]
    person, key = await _person(client, admin, author, member_of=dept["id"])
    await _bind(client, admin, person, identity(), visibility="members")

    # A rule of the tenant: no workspace of its own, the item names where.
    rule = await client.post(
        "/api/v1/rules",
        json={
            "key": "sample-found",
            "trigger": {"kind": "observation", "type": FOUND},
            "action": {
                "kind": "ensure_work",
                "taskType": "sample-work",
                "forEach": "payload.data.items",
                "dedupKeyTemplate": "sample:{{item.id}}",
                "fields": {"title": "Sample {{item.id}}", "workspaceId": "{{item.workspace}}"},
            },
        },
        headers=auth(key),
    )
    assert rule.status_code == 201, rule.text

    async def evaluate(workspace: str) -> dict[str, Any]:
        observed = await client.post(
            "/api/v1/observations",
            json={
                "kind": FOUND,
                "content": "found",
                "data": {"items": [{"id": workspace, "workspace": workspace}]},
            },
            headers=auth(admin),
        )
        assert observed.status_code in (200, 201), observed.text
        await worker.run_once()
        response = await client.get(
            f"/api/v1/rules/{rule.json()['id']}/evaluations", headers=auth(admin)
        )
        evaluation: dict[str, Any] = response.json()["items"][0]
        return evaluation

    outside = await evaluate(other["id"])
    assert outside["status"] == "failed", outside
    assert outside["error"]["code"] == "not_found"
    assert outside["error"]["details"] == {"workspaceId": other["id"]}
    inside = await evaluate(dept["id"])
    assert inside["status"] == "matched", inside
