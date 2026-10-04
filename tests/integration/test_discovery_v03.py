"""v0.3 work discovery: /work/available, claimability diagnosis, subtree filters."""

import httpx
import pytest

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_role,
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)

pytestmark = pytest.mark.usefixtures("clean_database")


async def _available_ids(client: httpx.AsyncClient, key: str, **params: object) -> list[str]:
    response = await client.get("/api/v1/work/available", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [t["id"] for t in body["items"]]
    cursor = body["nextCursor"]
    while cursor is not None:
        response = await client.get(
            "/api/v1/work/available", params={**params, "cursor": cursor}, headers=auth(key)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(t["id"] for t in body["items"])
        cursor = body["nextCursor"]
    return ids


async def test_eligible_ready_task_visible(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Open work")
    assert task["id"] in await _available_ids(client, agent_key)


async def test_ineligible_task_hidden(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    role = await create_role(client, admin_key, "sre")
    gated = await create_task(
        client, admin_key, title="Needs role", requirements={"roles": ["sre"]}
    )
    plain = await create_task(client, admin_key, title="Anyone")

    ids = await _available_ids(client, agent_key)
    assert plain["id"] in ids
    assert gated["id"] not in ids

    await assign_role(client, admin_key, agent["id"], role["id"])
    assert gated["id"] in await _available_ids(client, agent_key)


async def test_blocked_task_hidden_until_prerequisite_done(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    first = await create_task(client, admin_key, title="First")
    second = await create_task(client, admin_key, title="Second")
    response = await client.post(
        f"/api/v1/tasks/{second['id']}/relations",
        json={"toTask": first["id"], "type": "depends_on"},
        headers=auth(admin_key),
    )
    assert response.status_code == 201

    ids = await _available_ids(client, agent_key)
    assert first["id"] in ids
    assert second["id"] not in ids

    # Complete the prerequisite -> dependent becomes available.
    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{first['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    version = (await client.get(f"/api/v1/tasks/{first['id']}", headers=auth(agent_key))).json()[
        "version"
    ]
    response = await client.post(
        f"/api/v1/tasks/{first['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": f'"task-{version}"'},
    )
    assert response.status_code == 200
    assert second["id"] in await _available_ids(client, agent_key)


async def test_claimed_task_hidden_until_lease_dies(client: httpx.AsyncClient, sync_engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, other_key = await create_agent_with_key(client, admin_key, name="other")
    task = await create_task(client, admin_key, title="Contended")

    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    assert task["id"] not in await _available_ids(client, other_key)

    # Once the claim lease expires, the task is discoverable again.
    backdate_expiry(sync_engine, "task_claims", claim["id"])
    assert task["id"] in await _available_ids(client, other_key)


async def test_gate_approval_hides_task(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Gated")

    response = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    approval = response.json()
    assert approval["gate"] is True
    assert task["id"] not in await _available_ids(client, agent_key)

    # Decision (either way) opens the gate.
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin_key)
    )
    assert response.status_code == 200
    assert task["id"] in await _available_ids(client, agent_key)


async def test_workspace_subtree_filtering(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    eng = await create_workspace(client, admin_key, "engineering")
    backend = await create_workspace(client, admin_key, "backend", parent_id=eng["id"])
    sales = await create_workspace(client, admin_key, "sales")

    t_eng = await create_task(client, admin_key, title="Eng", workspaceId=eng["id"])
    t_backend = await create_task(client, admin_key, title="Back", workspaceId=backend["id"])
    t_sales = await create_task(client, admin_key, title="Sales", workspaceId=sales["id"])
    t_none = await create_task(client, admin_key, title="Homeless")

    direct = await _available_ids(client, agent_key, workspaceId=eng["id"])
    assert t_eng["id"] in direct and t_backend["id"] not in direct

    subtree = await _available_ids(
        client, agent_key, workspaceId=eng["id"], includeDescendants="true"
    )
    assert t_eng["id"] in subtree and t_backend["id"] in subtree
    assert t_sales["id"] not in subtree and t_none["id"] not in subtree

    # Same contract on the plain task list.
    response = await client.get(
        "/api/v1/tasks",
        params={"workspaceId": eng["id"], "includeDescendants": "true"},
        headers=auth(agent_key),
    )
    listed = [t["id"] for t in response.json()["items"]]
    assert t_backend["id"] in listed and t_sales["id"] not in listed


async def test_priority_ordering_and_pagination(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    low = await create_task(client, admin_key, title="low", priority="low")
    critical = await create_task(client, admin_key, title="crit", priority="critical")
    medium = await create_task(client, admin_key, title="med", priority="medium")

    response = await client.get(
        "/api/v1/work/available", params={"limit": 1}, headers=auth(agent_key)
    )
    first_page = response.json()
    assert [t["id"] for t in first_page["items"]] == [critical["id"]]
    assert first_page["nextCursor"] is not None

    rest = await _available_ids(client, agent_key)
    assert rest == [critical["id"], medium["id"], low["id"]]


async def test_discovery_tenant_isolation(client: httpx.AsyncClient, sync_engine) -> None:
    boot = await do_bootstrap(client)
    task = await create_task(client, boot["apiKey"]["key"], title="Private")
    _, other_key = make_tenant_directly(sync_engine, "rival")
    assert task["id"] not in await _available_ids(client, other_key)


async def test_claimability_diagnosis(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    await create_role(client, admin_key, "approver")
    blocker = await create_task(client, admin_key, title="Blocker")
    task = await create_task(
        client, admin_key, title="Diagnosed", requirements={"roles": ["approver"]}
    )
    await client.post(
        f"/api/v1/tasks/{task['id']}/relations",
        json={"toTask": blocker["id"], "type": "depends_on"},
        headers=auth(admin_key),
    )
    await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
        headers=auth(admin_key),
    )

    response = await client.get(f"/api/v1/tasks/{task['id']}/claimability", headers=auth(agent_key))
    assert response.status_code == 200
    diagnosis = response.json()
    assert diagnosis["claimable"] is False
    codes = {r["code"] for r in diagnosis["reasons"]}
    assert codes == {"task_not_ready", "approval_required", "not_eligible"}

    plain = await create_task(client, admin_key, title="Free")
    diagnosis = (
        await client.get(f"/api/v1/tasks/{plain['id']}/claimability", headers=auth(agent_key))
    ).json()
    assert diagnosis["claimable"] is True
    assert diagnosis["reasons"] == []


# --- work addressed to someone (a runner takes only what it was given) --------


async def test_assigned_to_me_narrows_the_queue_to_this_principal(
    client: httpx.AsyncClient,
) -> None:
    """A worker must be able to ask for its own queue, not the whole board.

    "Claimable by me" and "meant for me" are different questions: an autonomous
    runner that answers the first takes whatever is on top, which is how it ends
    up doing work nobody handed it.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    mine, my_key = await create_agent_with_key(client, admin_key, name="mine")
    theirs, _ = await create_agent_with_key(client, admin_key, name="theirs")

    for_me = await create_task(client, admin_key, title="For me", assigneeId=mine["id"])
    for_them = await create_task(client, admin_key, title="For them", assigneeId=theirs["id"])
    unassigned = await create_task(client, admin_key, title="For anyone")

    # Without the filter the queue is the whole board, as before.
    everything = await _available_ids(client, my_key)
    assert {for_me["id"], for_them["id"], unassigned["id"]} <= set(everything)

    # With it, exactly what was addressed to this principal — an unassigned
    # task is NOT mine: nobody decided it was.
    assert await _available_ids(client, my_key, assignedToMe="true") == [for_me["id"]]


async def test_assigned_to_me_cannot_be_pointed_at_another_principal(
    client: httpx.AsyncClient,
) -> None:
    """The flag resolves to the caller, so a stale config cannot redirect it."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    mine, my_key = await create_agent_with_key(client, admin_key, name="mine")
    theirs, _ = await create_agent_with_key(client, admin_key, name="theirs")

    for_me = await create_task(client, admin_key, title="For me", assigneeId=mine["id"])
    await create_task(client, admin_key, title="For them", assigneeId=theirs["id"])

    ids = await _available_ids(client, my_key, assignedToMe="true", assigneeId=theirs["id"])
    assert ids == [for_me["id"]]


# --- an unknown narrowing parameter is refused, not dropped (2026-08-14) ------


async def test_misspelled_narrowing_parameter_does_not_return_the_whole_queue(
    client: httpx.AsyncClient,
) -> None:
    """A filter the server did not understand must fail, never look like success.

    On 2026-08-14 a client sent ``assignedToMe`` to a server that did not know
    it yet; the parameter was dropped and the worker got the whole board. What
    is pinned here is the outcome — no page of tasks — and which parameter the
    error points at, not the wording of the message.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    mine, my_key = await create_agent_with_key(client, admin_key, name="mine")
    theirs, _ = await create_agent_with_key(client, admin_key, name="theirs")
    await create_task(client, admin_key, title="For me", assigneeId=mine["id"])
    await create_task(client, admin_key, title="For them", assigneeId=theirs["id"])

    response = await client.get(
        "/api/v1/work/available",
        params={"assignedToMee": "true", "limit": 50},
        headers=auth(my_key),
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert "items" not in body
    assert body["error"]["code"] == "invalid_request"
    assert [e["loc"] for e in body["error"]["details"]["errors"]] == ["query.assignedToMee"]


async def test_unknown_parameter_is_refused_on_every_listing_not_only_the_queue(
    client: httpx.AsyncClient,
) -> None:
    """The rule is API-wide: a hand-kept list of "narrowing" endpoints would rot."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await create_task(client, admin_key, title="Somebody's task")

    response = await client.get(
        "/api/v1/tasks", params={"projectID": "whatever"}, headers=auth(admin_key)
    )

    assert response.status_code == 400, response.text
    assert "items" not in response.json()
    locs = [e["loc"] for e in response.json()["error"]["details"]["errors"]]
    assert locs == ["query.projectID"]


async def _publish_type(client: httpx.AsyncClient, key: str, type_key: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": type_key.title()},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_type_key_narrows_the_queue_across_pages_and_versions(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0056 Ж1: the filter is applied before the page, so the cursor walks only it."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    await _publish_type(client, admin_key, "chore")
    await _publish_type(client, admin_key, "errand")
    old_chore = await create_task(client, admin_key, title="chore v1", typeKey="chore")
    for n in range(3):
        await create_task(client, admin_key, title=f"other {n}", priority="critical")
    errand = await create_task(client, admin_key, title="errand", typeKey="errand")
    second = await _publish_type(client, admin_key, "chore")
    assert second["version"] == 2
    new_chore = await create_task(client, admin_key, title="chore v2", typeKey="chore")

    response = await client.get(
        "/api/v1/work/available", params={"typeKey": "chore", "limit": 1}, headers=auth(agent_key)
    )
    assert response.status_code == 200, response.text
    first = response.json()
    assert [t["id"] for t in first["items"]] == [old_chore["id"]]
    assert first["nextCursor"] is not None
    # Every version of the key, in the queue order, and nothing else.
    assert await _available_ids(client, agent_key, typeKey="chore", limit=1) == [
        old_chore["id"],
        new_chore["id"],
    ]
    # Repeated: either type.
    assert await _available_ids(client, agent_key, typeKey=["chore", "errand"], limit=1) == [
        old_chore["id"],
        errand["id"],
        new_chore["id"],
    ]
    # A key no type carries narrows to nothing, not to everything.
    assert await _available_ids(client, agent_key, typeKey="nonexistent") == []


async def test_type_key_combines_with_workspace_and_assignee(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    await _publish_type(client, admin_key, "chore")
    eng = await create_workspace(client, admin_key, "engineering")
    backend = await create_workspace(client, admin_key, "backend", parent_id=eng["id"])
    inside = await create_task(
        client, admin_key, title="in", typeKey="chore", workspaceId=backend["id"]
    )
    mine = await create_task(
        client,
        admin_key,
        title="mine",
        typeKey="chore",
        workspaceId=backend["id"],
        assigneeId=agent["id"],
    )
    await create_task(client, admin_key, title="outside", typeKey="chore")
    await create_task(client, admin_key, title="other type", workspaceId=backend["id"])

    subtree = await _available_ids(
        client, agent_key, typeKey="chore", workspaceId=eng["id"], includeDescendants="true"
    )
    assert subtree == [inside["id"], mine["id"]]
    assert await _available_ids(
        client,
        agent_key,
        typeKey="chore",
        workspaceId=eng["id"],
        includeDescendants="true",
        assignedToMe="true",
    ) == [mine["id"]]


async def test_type_key_does_not_widen_eligibility(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    await _publish_type(client, admin_key, "chore")
    await create_role(client, admin_key, "sre")
    gated = await create_task(
        client, admin_key, title="Needs role", typeKey="chore", requirements={"roles": ["sre"]}
    )
    plain = await create_task(client, admin_key, title="Anyone", typeKey="chore")
    assert await _available_ids(client, agent_key, typeKey="chore") == [plain["id"]]
    assert gated["id"] not in await _available_ids(client, agent_key)


@pytest.mark.parametrize(
    "type_keys",
    [
        pytest.param([""], id="blank"),
        pytest.param(["  "], id="spaces"),
        pytest.param([f"t{n}" for n in range(51)], id="too-many"),
    ],
)
async def test_invalid_type_key_is_refused_not_ignored(
    client: httpx.AsyncClient, type_keys: list[str]
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await create_task(client, admin_key, title="Somebody's task")
    response = await client.get(
        "/api/v1/work/available", params={"typeKey": type_keys}, headers=auth(admin_key)
    )
    assert response.status_code == 422, response.text
    assert "items" not in response.json()
    assert response.json()["error"]["code"] == "invalid_type_key"
