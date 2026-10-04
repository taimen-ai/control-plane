"""The entity list of a workspace's knowledge through the core (CP-ADR-0060, K031).

``POST /knowledge/entities:query`` reads Memory's ``entities:query`` in the
namespace of the workspace tree root with the caller's visibility, under the
right to read the workspace's context (``events.read``, as ``/context/recall``).
Memory is ``tests.fake_graph_memory.FakeGraphMemory``: every body it gets is
validated against the pinned ``EntitiesQueryIn``.
"""

from typing import Any

import httpx
import pytest

from control_plane.application.queries import recall
from tests.fake_graph_memory import FakeGraphMemory
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap

PATH = "/api/v1/knowledge/entities:query"
KINDS = ["ui_call", "endpoint", "client_method", "adr"]


@pytest.fixture
def memory(app) -> Any:
    fake = FakeGraphMemory()
    app.state.context_provider = fake
    yield fake
    app.state.context_provider = None


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    reader, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["events.read"]
    )
    return {
        "tenant": boot["tenant"]["id"],
        "admin_key": admin_key,
        "root": root,
        "child": child,
        "reader": reader,
        "reader_key": reader_key,
    }


async def test_listing_needs_the_right_to_read_the_workspace_context(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    _, writer = await create_agent_with_key(
        client, s["admin_key"], name="writer", permissions=["tasks.read", "observations.write"]
    )
    response = await client.post(
        PATH, json={"workspaceId": s["root"]["id"], "kinds": KINDS}, headers=auth(writer)
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["required"] == ["events.read"]
    assert memory.entities_requests == []


async def test_entities_come_from_the_root_namespace_with_the_callers_visibility(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    where = [{"attr": "method", "op": "eq", "value": "POST"}]
    response = await client.post(
        PATH,
        json={
            "workspaceId": s["child"]["id"],
            "kinds": ["endpoint", "endpoint"],
            "where": where,
            "asOf": "2026-09-28T00:00:00Z",
        },
        headers=auth(s["reader_key"]),
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert [(i["kind"], i["key"]) for i in page["items"]] == [("endpoint", "POST /tasks/{}:claim")]
    assert page["nextCursor"] is None
    assert page["asOf"] == "2026-09-28T00:00:00+00:00"

    [body] = memory.entities_requests
    root_ns = f"tenant:{s['tenant']}:ws:{s['root']['id']}"
    # The namespace of the tree root, where a snapshot of the child lands.
    assert body["namespaces"] == [root_ns]
    assert body["kinds"] == ["endpoint"]
    assert body["where"] == where
    assert body["asOf"] == "2026-09-28T00:00:00+00:00"
    assert body["limit"] == 100
    assert "cursor" not in body
    # Local mode narrows to the focus workspace, its ancestors and the caller.
    assert body["allowedScopes"] == [
        f"workspace:{s['child']['id']}",
        f"workspace:{s['root']['id']}",
        f"principal:{s['reader']['id']}",
    ]
    # What another workspace sees stays hidden.
    everything = await client.post(
        PATH,
        json={"workspaceId": s["child"]["id"], "kinds": ["ui_call"]},
        headers=auth(s["reader_key"]),
    )
    keys = [i["key"] for i in everything.json()["items"]]
    assert keys == ["platform-web:src/api/runs.ts:17", "platform-web:src/api/tasks.ts:42"]


async def test_pages_walk_the_whole_list_once(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    request = {"workspaceId": s["root"]["id"], "kinds": KINDS, "limit": 2}
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(10):
        response = await client.post(
            PATH,
            json={**request, **({"cursor": cursor} if cursor else {})},
            headers=auth(s["reader_key"]),
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 2
        seen += [(i["kind"], i["key"]) for i in page["items"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert cursor is None
    visible = sorted(
        (n.kind, n.key)
        for n in memory.nodes.values()
        if n.kind in KINDS and "workspace:other" not in n.scopes
    )
    assert seen == visible
    assert len(seen) > 2
    # Every page asked the same list; the cursor is Memory's, passed as it is.
    first, *rest = memory.entities_requests
    assert "cursor" not in first
    assert all(r["cursor"] and r["kinds"] == first["kinds"] for r in rest)


async def test_the_list_reads_only_what_policy_lets_the_caller_see(
    client: httpx.AsyncClient, memory: FakeGraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await _setup(client)
    root_ns = f"tenant:{s['tenant']}:ws:{s['root']['id']}"
    scopes = [f"workspace:{s['root']['id']}", f"principal:{s['reader']['id']}"]

    async def elsewhere(*_: Any) -> tuple[list[str], list[str]]:
        return [f"tenant:{s['tenant']}"], scopes

    monkeypatch.setattr(recall, "memory_visibility", elsewhere)
    hidden = await client.post(
        PATH, json={"workspaceId": s["root"]["id"], "kinds": KINDS}, headers=auth(s["reader_key"])
    )
    assert hidden.status_code == 403, hidden.text
    assert memory.entities_requests == []

    async def visible(*_: Any) -> tuple[list[str], list[str]]:
        return [f"tenant:{s['tenant']}", root_ns], scopes

    monkeypatch.setattr(recall, "memory_visibility", visible)
    shown = await client.post(
        PATH, json={"workspaceId": s["root"]["id"], "kinds": KINDS}, headers=auth(s["reader_key"])
    )
    assert shown.status_code == 200, shown.text
    [body] = memory.entities_requests
    assert body["namespaces"] == [root_ns]
    # The policy's visibility goes to Memory as computed, not narrowed here.
    assert body["allowedNamespaces"] == [f"tenant:{s['tenant']}", root_ns]
    assert body["allowedScopes"] == scopes


async def test_entity_list_contract_errors(client: httpx.AsyncClient, app) -> None:
    s = await _setup(client)
    headers = auth(s["reader_key"])
    good = {"workspaceId": s["root"]["id"], "kinds": KINDS}
    no_provider = await client.post(PATH, json=good, headers=headers)
    assert no_provider.status_code == 503
    assert no_provider.json()["error"]["code"] == "memory_disabled"

    fake = FakeGraphMemory()
    app.state.context_provider = fake
    try:
        unknown = await client.post(
            PATH,
            json={**good, "workspaceId": "00000000-0000-0000-0000-000000000001"},
            headers=headers,
        )
        assert unknown.status_code == 404, unknown.text
        for bad in (
            {"workspaceId": s["root"]["id"]},
            {**good, "kinds": []},
            {**good, "kinds": ["1kind"]},
            {**good, "kinds": ["k"] * 21},
            {**good, "limit": 0},
            {**good, "limit": 501},
            {**good, "cursor": ""},
            {**good, "asOf": "2026-09-28T00:00:00"},
            {**good, "where": [{"attr": "okpd2", "op": "prefix", "value": ".62"}]},
            {**good, "namespaces": ["tenant:other"]},
            {**good, "allowedScopes": []},
        ):
            refused = await client.post(PATH, json=bad, headers=headers)
            assert refused.status_code == 400, (bad, refused.text)
            assert refused.json()["error"]["code"] == "invalid_request"
        assert fake.entities_requests == []

        foreign = await client.post(PATH, json={**good, "cursor": "not-ours"}, headers=headers)
        assert foreign.status_code == 422, foreign.text
        assert foreign.json()["error"]["code"] == "entities_query_invalid"
        assert foreign.json()["error"]["details"] == {"memoryStatus": 400}

        app.state.context_provider = FakeGraphMemory(fail="entities")
        upstream = await client.post(PATH, json=good, headers=headers)
        assert upstream.status_code == 502, upstream.text
        assert upstream.json()["error"]["code"] == "memory_unavailable"
    finally:
        app.state.context_provider = None


async def test_relations_of_the_entities_with_the_callers_visibility(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    """``include.relations`` (CP-ADR-0060, amendment 2026-10-03): each entity's
    relations and the other end; an end the caller cannot see is not answered."""
    s = await _setup(client)
    response = await client.post(
        PATH,
        json={
            "workspaceId": s["child"]["id"],
            "kinds": ["endpoint"],
            "where": [{"attr": "method", "op": "eq", "value": "POST"}],
            "include": {"relations": ["calls", "calls", "defined_in"], "limit": 2},
        },
        headers=auth(s["reader_key"]),
    )
    assert response.status_code == 200, response.text
    [claim] = response.json()["items"]
    assert claim["relations"] == [
        {
            "relation": "calls",
            "direction": "in",
            "kind": "client_method",
            "key": "control-plane:control_plane_client.client.ControlPlaneClient.claim_task",
            "title": "control-plane:control_plane_client.client.ControlPlaneClient.claim_task",
        },
        {
            "relation": "calls",
            "direction": "in",
            "kind": "ui_call",
            "key": "platform-web:src/api/tasks.ts:42",
            "title": "platform-web:src/api/tasks.ts:42",
        },
    ]
    [body] = memory.typed_requests
    root_ns = f"tenant:{s['tenant']}:ws:{s['root']['id']}"
    # The list's namespace and visibility, the names once each.
    assert body["scope"] == {"namespace": root_ns}
    assert body["allowedScopes"] == memory.entities_requests[0]["allowedScopes"]
    assert [step["relation"] for step in body["traverse"]] == ["calls", "defined_in"]

    # Every relation the namespace's packs declare, the caller's ends only.
    star = await client.post(
        PATH,
        json={
            "workspaceId": s["child"]["id"],
            "kinds": ["endpoint"],
            "include": {"relations": "*", "direction": "in"},
        },
        headers=auth(s["reader_key"]),
    )
    assert star.status_code == 200, star.text
    ends = [r["key"] for item in star.json()["items"] for r in item["relations"]]
    assert ends and "secret-app:src/api.ts:1" not in ends
    assert all(r["direction"] == "in" for item in star.json()["items"] for r in item["relations"])


async def test_source_fields_follow_the_contract(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    response = await client.post(
        PATH, json={"workspaceId": s["root"]["id"], "kinds": ["adr"]}, headers=auth(s["reader_key"])
    )
    assert response.status_code == 200, response.text
    [adr] = response.json()["items"]
    assert adr["validFrom"] == "2026-01-01T00:00:00+00:00"
    assert adr["validTo"] is None
    assert adr["sources"] == [
        {
            "source": "git:control-plane",
            "sourcePath": "",
            "snapshotId": "s1",
            "source_path": "",
            "snapshot_id": "s1",
            "scope": "",
        }
    ]
    # Transitional snake_case fields stay until a later amendment removes them.
    assert adr["valid_from"] == adr["validFrom"]
    assert adr["source"] == "git:control-plane"
    # No relations unless asked for, and no traversal either.
    assert "relations" not in adr
    assert memory.typed_requests == []


async def test_include_contract_errors(client: httpx.AsyncClient, memory: FakeGraphMemory) -> None:
    s = await _setup(client)
    good = {"workspaceId": s["root"]["id"], "kinds": KINDS}
    for include in (
        {},
        {"relations": []},
        {"relations": "all"},
        {"relations": ["Calls"]},
        {"relations": ["calls"], "direction": "sideways"},
        {"relations": ["calls"], "limit": 0},
        {"relations": ["calls"], "limit": 201},
        {"relations": ["calls"], "depth": 2},
    ):
        refused = await client.post(
            PATH, json={**good, "include": include}, headers=auth(s["reader_key"])
        )
        assert refused.status_code == 400, (include, refused.text)
        assert refused.json()["error"]["code"] == "invalid_request"
    too_long = await client.post(
        PATH,
        json={**good, "limit": 101, "include": {"relations": ["calls"]}},
        headers=auth(s["reader_key"]),
    )
    assert too_long.status_code == 400, too_long.text
    assert memory.entities_requests == [] and memory.typed_requests == []

    memory.fail = "typed"
    failed = await client.post(
        PATH, json={**good, "include": {"relations": ["calls"]}}, headers=auth(s["reader_key"])
    )
    assert failed.status_code == 502, failed.text
    assert failed.json()["error"]["code"] == "memory_unavailable"
