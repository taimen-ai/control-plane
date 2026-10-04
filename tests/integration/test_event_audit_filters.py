"""Reading the journal for an audit: author, period, one workspace (CP-ADR-0068
amendment Б).

``actorId``, ``occurredFrom`` (inclusive) / ``occurredTo`` (exclusive) and
``includeDescendants`` narrow ``GET /events`` like the earlier filters: the
order and the cursor do not change, forward and backward pages, through the
archive too, and an index serves each of them instead of a scan of the
journal.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.event_cursor import EventPosition
from control_plane.application.queries.events import (
    EventFilter,
    _hot_before_stmt,
    _replay_stmt,
)
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)
from tests.integration.test_event_backward_paging import _drain_outbox_and_confirm


async def _get(client: httpx.AsyncClient, key: str, **params: Any) -> httpx.Response:
    return await client.get("/api/v1/events", params=params, headers=auth(key))


async def _page(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await _get(client, key, **params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _forward(client: httpx.AsyncClient, key: str, size: int, **params: Any) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    while True:
        query = dict(params, limit=size)
        if cursor:
            query["cursor"] = cursor
        page = await _page(client, key, **query)
        ids += [e["id"] for e in page["items"]]
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            return ids


async def _backward(client: httpx.AsyncClient, key: str, size: int, **params: Any) -> list[str]:
    """Walk from ``tail`` back to the start; ids in delivery order."""
    page = await _page(client, key, tail=size, **params)
    pages = [page["items"]]
    while page["prevCursor"]:
        page = await _page(client, key, before=page["prevCursor"], limit=size, **params)
        pages.append(page["items"])
    return [e["id"] for items in reversed(pages) for e in items]


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    """Admin and an agent each create work in ops, ops/child and sales."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    ops = await create_workspace(client, key, "ops")
    child = await create_workspace(client, key, "ops-child", parent_id=ops["id"])
    sales = await create_workspace(client, key, "sales")
    agent, agent_key = await create_agent_with_key(client, key, name="worker")
    for author in (key, agent_key):
        for workspace in (ops, child, sales):
            await create_task(client, author, title="work", workspaceId=workspace["id"])
    return {
        "key": key,
        "adminId": boot["adminPrincipal"]["id"],
        "agentId": agent["id"],
        "agentKey": agent_key,
        "ops": ops["id"],
        "child": child["id"],
        "sales": sales["id"],
        "tenantId": boot["tenant"]["id"],
    }


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- actorId -----------------------------------------------------------------


async def test_actor_filter_returns_only_that_principals_events_in_order(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    page = await _page(client, world["key"], actorId=world["agentId"], limit=200)
    expected = [e["id"] for e in everything if e["actorId"] == world["agentId"]]
    assert expected, "the agent wrote events"
    assert [e["id"] for e in page["items"]] == expected
    assert {e["actorId"] for e in page["items"]} == {world["agentId"]}

    # Nobody acting under that id, or the id of another tenant's principal:
    # an empty page, not an error.
    assert (await _page(client, world["key"], actorId=str(uuid.uuid4())))["items"] == []


async def test_actor_of_another_tenant_reads_nothing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    world = await _world(client)
    _, other_key = make_tenant_directly(sync_engine, "other")
    page = await _page(client, other_key, actorId=world["agentId"])
    assert page["items"] == []


async def test_malformed_actor_is_rejected(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _get(client, key, actorId="not-a-uuid")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


# --- occurredFrom / occurredTo --------------------------------------------------


async def test_period_is_inclusive_from_and_exclusive_to(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    moments = sorted({_moment(e["occurredAt"]) for e in everything})
    assert len(moments) >= 4
    low, high = moments[1], moments[-2]

    page = await _page(
        client,
        world["key"],
        occurredFrom=low.isoformat(),
        occurredTo=high.isoformat(),
        limit=200,
    )
    expected = [e["id"] for e in everything if low <= _moment(e["occurredAt"]) < high]
    assert [e["id"] for e in page["items"]] == expected
    got = {_moment(e["occurredAt"]) for e in page["items"]}
    assert low in got  # the lower bound is included
    assert high not in got  # the upper bound is not

    # One bound alone is a half-line.
    since = await _page(client, world["key"], occurredFrom=high.isoformat(), limit=200)
    assert [e["id"] for e in since["items"]] == [
        e["id"] for e in everything if _moment(e["occurredAt"]) >= high
    ]
    until = await _page(client, world["key"], occurredTo=low.isoformat(), limit=200)
    assert [e["id"] for e in until["items"]] == [
        e["id"] for e in everything if _moment(e["occurredAt"]) < low
    ]


async def test_period_bound_in_another_zone_is_the_same_instant(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    middle = sorted(_moment(e["occurredAt"]) for e in everything)[len(everything) // 2]
    shifted = middle.astimezone(timezone(timedelta(hours=3)))
    in_utc = await _page(client, world["key"], occurredFrom=middle.isoformat(), limit=200)
    in_msk = await _page(client, world["key"], occurredFrom=shifted.isoformat(), limit=200)
    assert in_utc["items"] and in_utc["items"] == in_msk["items"]


async def test_empty_period_is_an_empty_page(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    moment = (await _page(client, world["key"]))["items"][0]["occurredAt"]
    page = await _page(client, world["key"], occurredFrom=moment, occurredTo=moment)
    assert page["items"] == []
    assert page["hasMore"] is False


@pytest.mark.parametrize(
    "params",
    [
        {"occurredFrom": "2026-07-01T00:00:00"},  # no zone
        {"occurredTo": "2026-07-01T00:00:00"},
        {"occurredFrom": "2026-08-01T00:00:00Z", "occurredTo": "2026-07-01T00:00:00Z"},
    ],
)
async def test_bad_period_is_rejected(client: httpx.AsyncClient, params: dict[str, str]) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _get(client, key, **params)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_event_period"


async def test_unparseable_period_is_rejected(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for bad in ("yesterday", "", "2026-13-01T00:00:00Z"):
        response = await _get(client, key, occurredFrom=bad)
        assert response.status_code == 400, (bad, response.text)
        assert response.json()["error"]["code"] == "invalid_request"


# --- includeDescendants -----------------------------------------------------------


async def test_include_descendants_false_reads_the_workspace_alone(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    alone = await _page(
        client, world["key"], workspaceId=world["ops"], includeDescendants="false", limit=200
    )
    assert alone["items"]
    assert {e["workspaceId"] for e in alone["items"]} == {world["ops"]}

    subtree = await _page(
        client, world["key"], workspaceId=world["ops"], includeDescendants="true", limit=200
    )
    assert {e["workspaceId"] for e in subtree["items"]} == {world["ops"], world["child"]}
    # Absent means the subtree, as before the parameter existed (CP-ADR-0068 p.1).
    default = await _page(client, world["key"], workspaceId=world["ops"], limit=200)
    assert default["items"] == subtree["items"]


async def test_include_descendants_without_workspace_is_ignored(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    everything = await _page(client, world["key"], limit=200)
    for flag in ("true", "false"):
        page = await _page(client, world["key"], includeDescendants=flag, limit=200)
        assert page["items"] == everything["items"]


async def test_include_descendants_keeps_the_rights_and_the_404(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    _, no_events = await create_agent_with_key(
        client, world["key"], name="no-events", permissions=["tasks.read"]
    )
    refused = await _get(client, no_events, workspaceId=world["ops"], includeDescendants="false")
    assert refused.status_code == 403
    missing = await _get(
        client, world["key"], workspaceId=str(uuid.uuid4()), includeDescendants="false"
    )
    assert missing.status_code == 404


async def test_new_filters_keep_events_read(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    _, no_events = await create_agent_with_key(
        client, world["key"], name="no-events", permissions=["tasks.read"]
    )
    for params in (
        {"actorId": world["adminId"]},
        {"occurredFrom": "2000-01-01T00:00:00Z"},
    ):
        assert (await _get(client, no_events, **params)).status_code == 403


# --- combinations and cursors ------------------------------------------------------


async def test_filters_combine_as_an_intersection(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    low = sorted(_moment(e["occurredAt"]) for e in everything)[2]
    page = await _page(
        client,
        world["key"],
        actorId=world["agentId"],
        workspaceId=world["ops"],
        includeDescendants="false",
        types="task.",
        occurredFrom=low.isoformat(),
        limit=200,
    )
    expected = [
        e["id"]
        for e in everything
        if e["actorId"] == world["agentId"]
        and e["workspaceId"] == world["ops"]
        and e["type"].startswith("task.")
        and _moment(e["occurredAt"]) >= low
    ]
    assert expected
    assert [e["id"] for e in page["items"]] == expected


@pytest.mark.parametrize("size", [1, 2, 5])
async def test_filtered_walks_forward_and_backward_agree(
    client: httpx.AsyncClient, size: int
) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    moments = sorted(_moment(e["occurredAt"]) for e in everything)
    low, high = moments[2], moments[-1]
    for params in (
        {"actorId": world["agentId"]},
        {"occurredFrom": low.isoformat(), "occurredTo": high.isoformat()},
        {"workspaceId": world["ops"], "includeDescendants": "false"},
        {"actorId": world["adminId"], "occurredFrom": low.isoformat()},
    ):
        whole = [e["id"] for e in (await _page(client, world["key"], limit=200, **params))["items"]]
        assert whole, params
        assert await _forward(client, world["key"], size, **params) == whole, params
        assert await _backward(client, world["key"], size, **params) == whole, params


async def test_newest_first_reading_of_a_period(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    everything = (await _page(client, world["key"], limit=200))["items"]
    low = sorted(_moment(e["occurredAt"]) for e in everything)[3]
    params = {"actorId": world["agentId"], "occurredFrom": low.isoformat()}
    expected = [
        e["id"]
        for e in everything
        if e["actorId"] == world["agentId"] and _moment(e["occurredAt"]) >= low
    ]
    newest = await _page(client, world["key"], tail=2, order="desc", **params)
    assert [e["id"] for e in newest["items"]] == list(reversed(expected))[:2]
    older = await _page(
        client, world["key"], before=newest["prevCursor"], limit=2, order="desc", **params
    )
    assert [e["id"] for e in older["items"]] == list(reversed(expected))[2:4]


async def test_filtered_cursor_moves_past_what_it_skipped(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    first = await _page(client, world["key"], actorId=world["agentId"], limit=200)
    cursor = first["nextCursor"]
    await create_task(client, world["key"], title="admin noise")
    idle = await _page(client, world["key"], actorId=world["agentId"], cursor=cursor)
    assert idle["items"] == []
    assert idle["nextCursor"] != cursor
    later = await create_task(client, world["agentKey"], title="agent again")
    resumed = await _page(client, world["key"], actorId=world["agentId"], cursor=idle["nextCursor"])
    assert [e["entityId"] for e in resumed["items"]] == [later["id"]]


async def test_filters_reach_into_the_archive(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    world = await _world(client)
    _drain_outbox_and_confirm(sync_engine, world["tenantId"])
    archived = await client.post(
        "/api/v1/operations/journal:archive", json={"beforeSeconds": 0}, headers=auth(world["key"])
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] > 0
    await create_task(client, world["agentKey"], title="hot", workspaceId=world["ops"])

    everything = (await _page(client, world["key"], limit=200))["items"]
    low = sorted(_moment(e["occurredAt"]) for e in everything)[2]
    for params in (
        {"actorId": world["agentId"]},
        {"occurredFrom": low.isoformat()},
        {"workspaceId": world["ops"], "includeDescendants": "false"},
    ):
        whole = [e["id"] for e in (await _page(client, world["key"], limit=200, **params))["items"]]
        assert len(whole) > 1, params
        assert await _forward(client, world["key"], 2, **params) == whole, params
        assert await _backward(client, world["key"], 2, **params) == whole, params


# --- plans ---------------------------------------------------------------------------


def _plan(sync_engine: Engine, stmt: Any) -> str:
    with sync_engine.connect() as conn:
        compiled = stmt.compile(dialect=conn.dialect)
        rows = conn.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params).all()
    return "\n".join(row[0] for row in rows)


@pytest.mark.raw_journal
async def test_period_and_actor_pages_use_their_indexes(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A year of journal by one author; a day of it and a rare author are read
    through an index, not by scanning the journal."""
    world = await _world(client)
    rare = (await _page(client, world["key"], actorId=world["agentId"]))["items"]
    assert rare
    start = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
    with sync_engine.begin() as conn:
        # Copies of one real event: the same catalog type and payload, a new
        # id, the admin as the author, spread over a year.
        conn.execute(
            text(
                "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
                " actor_id, correlation_id, request_id, workspace_id, schema_version,"
                " payload, occurred_at)"
                " SELECT gen_random_uuid(), e.tenant_id, e.event_type, e.entity_type,"
                " e.entity_id, :admin, e.correlation_id, e.request_id, e.workspace_id,"
                " e.schema_version, e.payload,"
                " CAST(:start AS timestamptz) + n * interval '30 minutes'"
                " FROM generate_series(1, 20000) AS n,"
                " (SELECT * FROM events WHERE tenant_id = :tenant"
                "  AND event_type = 'task.created' LIMIT 1) AS e"
            ),
            {"admin": world["adminId"], "start": start, "tenant": world["tenantId"]},
        )
        conn.execute(text("ANALYZE events"))

    tenant = uuid.UUID(world["tenantId"])
    day = EventFilter(
        occurred_from=start + timedelta(days=200), occurred_to=start + timedelta(days=201)
    )
    author = EventFilter(actor_id=uuid.UUID(world["agentId"]))
    for filters, index in (
        (day, "ix_events_tenant_occurred_at"),
        (author, "ix_events_tenant_actor_tx_sequence"),
    ):
        backward = _hot_before_stmt(tenant_id=tenant, before=None, limit=51, filters=filters)
        forward = _replay_stmt(
            tenant_id=tenant, start=EventPosition(0, 0), limit=51, filters=filters
        )
        for stmt in (backward, forward):
            plan = _plan(sync_engine, stmt)
            assert index in plan, plan
            assert "Seq Scan on events" not in plan, plan
