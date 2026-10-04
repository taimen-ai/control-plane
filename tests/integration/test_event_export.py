"""Export of the journal for a period (CP-ADR-0068, export amendment).

``GET /events:export`` hands out what ``GET /events`` would page through with
the same filters, as one JSONL or CSV body: same events, same order, read page
by page up to the journal frontier of the moment the export was prepared.
The period is required and bounded, the number of events too, and a refusal
comes before the body. ``events.export`` is a right apart from ``events.read``;
each export leaves ``event_journal.exported`` with its filters, never the data.
"""

import asyncio
import csv
import dataclasses
import io
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from control_plane.application.event_cursor import EventPosition, decode_cursor
from control_plane.application.queries import event_export
from control_plane.application.queries.events import EventFilter
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)
from tests.integration.test_event_backward_paging import _drain_outbox_and_confirm
from tests.integration.test_workspace_visibility import same_as_missing
from tests.integration.test_workspace_visibility_routes import MISSING, make_tree

EXPORT = "/api/v1/events:export"


def _period(hours: int = 1) -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "occurredFrom": (now - timedelta(hours=hours)).isoformat(),
        "occurredTo": (now + timedelta(hours=hours)).isoformat(),
    }


async def _export(
    client: httpx.AsyncClient, key: str, export_format: str = "jsonl", **params: Any
) -> httpx.Response:
    return await client.get(EXPORT, params={"format": export_format, **params}, headers=auth(key))


def _lines(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


def _rows(response: httpx.Response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text, newline="")))


async def _events(client: httpx.AsyncClient, key: str, **params: Any) -> list[dict[str, Any]]:
    """Every event ``GET /events`` hands out for these filters, page after page."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = dict(params, limit=200)
        if cursor:
            query["cursor"] = cursor
        response = await client.get("/api/v1/events", params=query, headers=auth(key))
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["items"]
        cursor = body["nextCursor"]
        if not body["hasMore"]:
            return items


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    """The admin and an agent each create work in ops, ops/child and sales."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    ops = await create_workspace(client, key, "ops")
    child = await create_workspace(client, key, "ops-child", parent_id=ops["id"])
    sales = await create_workspace(client, key, "sales")
    agent, agent_key = await create_agent_with_key(client, key, name="worker")
    tasks = []
    for author in (key, agent_key):
        for workspace in (ops, child, sales):
            tasks.append(
                await create_task(client, author, title="work", workspaceId=workspace["id"])
            )
    return {
        "key": key,
        "adminId": boot["adminPrincipal"]["id"],
        "agentId": agent["id"],
        "ops": ops["id"],
        "child": child["id"],
        "sales": sales["id"],
        "tasks": tasks,
    }


# --- the body ----------------------------------------------------------------------


async def test_jsonl_is_the_journal_of_the_period_in_order(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    period = _period()
    expected = await _events(client, world["key"], **period)
    assert len(expected) > 10

    response = await _export(client, world["key"], **period)
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    assert response.headers["cache-control"] == "private, no-store"
    exported = _lines(response)
    # The same bodies as GET /events, cursor included; the export's own audit
    # event is written after its snapshot and is not in it.
    assert exported == expected
    assert response.headers["x-event-count"] == str(len(expected))


async def test_csv_has_flat_columns_and_payload_as_json(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    period = _period()
    expected = await _events(client, world["key"], **period)

    response = await _export(client, world["key"], "csv", **period)
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0] == (
        "id,occurredAt,type,schemaVersion,actorId,entityType,entityId,workspaceId,payload"
    )
    rows = _rows(response)
    assert [r["id"] for r in rows] == [e["id"] for e in expected]
    for row, event in zip(rows, expected, strict=True):
        assert row["occurredAt"] == event["occurredAt"]
        assert row["type"] == event["type"]
        assert row["schemaVersion"] == str(event["schemaVersion"])
        assert row["actorId"] == (event["actorId"] or "")
        assert row["entityType"] == event["entityType"]
        assert row["entityId"] == event["entityId"]
        assert row["workspaceId"] == (event["workspaceId"] or "")
        assert json.loads(row["payload"]) == event["payload"]


async def test_an_empty_period_is_an_empty_body(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    moment = datetime.now(UTC).isoformat()
    jsonl = await _export(client, key, occurredFrom=moment, occurredTo=moment)
    assert jsonl.status_code == 200, jsonl.text
    assert jsonl.text == ""
    assert jsonl.headers["x-event-count"] == "0"
    table = await _export(client, key, "csv", occurredFrom=moment, occurredTo=moment)
    assert table.status_code == 200, table.text
    assert table.text.splitlines() == [
        "id,occurredAt,type,schemaVersion,actorId,entityType,entityId,workspaceId,payload"
    ]


async def test_the_body_is_read_page_by_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(client)
    period = _period()
    expected = await _events(client, world["key"], **period)
    monkeypatch.setattr(event_export, "EXPORT_PAGE_SIZE", 3)
    assert _lines(await _export(client, world["key"], **period)) == expected
    rows = _rows(await _export(client, world["key"], "csv", types="task.", **period))
    assert [r["id"] for r in rows] == [
        e["id"] for e in await _events(client, world["key"], types="task.", **period)
    ]


async def test_each_page_has_a_session_of_its_own_and_the_snapshot_bounds_it(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    world = await _world(client)
    events = await _events(client, world["key"])
    bound = decode_cursor(events[-4]["cursor"])
    assert isinstance(bound, EventPosition)
    tenant = uuid.UUID(world["tasks"][0]["tenantId"])
    # Work written after the snapshot is not in the export.
    await create_task(client, world["key"], title="later", workspaceId=world["ops"])

    factory = app.state.session_factory
    opened = 0

    @asynccontextmanager
    async def counting() -> AsyncIterator[Any]:
        nonlocal opened
        opened += 1
        async with factory() as session:
            yield session

    prepared = event_export.EventExport(
        tenant_id=tenant,
        filters=EventFilter(),
        start=EventPosition(0, 0),
        until=bound,
        count=len(events),
        occurred_from=datetime.now(UTC),
        occurred_to=datetime.now(UTC),
    )
    got = [e.id async for e in event_export.export_events(counting, prepared, page_size=4)]  # type: ignore[arg-type]
    assert [str(i) for i in got] == [e["id"] for e in events[:-3]]
    # A page per session: full pages, then the one that crosses the bound.
    assert opened == len(got) // 4 + 1

    # The count bounds it too: retention may only take events away.
    capped = dataclasses.replace(prepared, count=5)
    assert len([e async for e in event_export.export_events(factory, capped, page_size=2)]) == 5


async def test_the_export_spans_the_archive_and_the_hot_journal(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(client)
    _drain_outbox_and_confirm(sync_engine, world["tasks"][0]["tenantId"])
    archived = await client.post(
        "/api/v1/operations/journal:archive", json={"beforeSeconds": 0}, headers=auth(world["key"])
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] > 0
    await create_task(client, world["key"], title="hot", workspaceId=world["ops"])

    period = _period()
    monkeypatch.setattr(event_export, "EXPORT_PAGE_SIZE", 4)
    for params in ({}, {"actorId": world["agentId"]}, {"types": "task."}):
        expected = await _events(client, world["key"], **params, **period)
        response = await _export(client, world["key"], **params, **period)
        assert _lines(response) == expected, params
        assert response.headers["x-event-count"] == str(len(expected))


# --- filters -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        {"types": "task."},
        {"types": "task.created,workspace."},
        {"workspaceId": "ops"},
        {"workspaceId": "ops", "includeDescendants": "false"},
        {"workspaceId": "ops", "includeDescendants": "true"},
        {"actorId": "agentId"},
        {"actorId": "agentId", "workspaceId": "ops", "types": "task."},
        {"entityType": "task", "entityId": "task0"},
    ],
)
async def test_filters_are_those_of_get_events(
    client: httpx.AsyncClient, filters: dict[str, str]
) -> None:
    world = await _world(client)
    values = {**world, "task0": world["tasks"][0]["id"]}
    params = {name: values.get(value, value) for name, value in filters.items()}
    period = _period()
    expected = await _events(client, world["key"], **params, **period)
    assert expected, params
    assert _lines(await _export(client, world["key"], **params, **period)) == expected
    rows = _rows(await _export(client, world["key"], "csv", **params, **period))
    assert [r["id"] for r in rows] == [e["id"] for e in expected]


async def test_the_period_is_inclusive_from_and_exclusive_to(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    events = await _events(client, world["key"])
    moments = sorted({e["occurredAt"] for e in events})
    assert len(moments) > 3
    start, end = moments[1], moments[-2]
    expected = [e["id"] for e in events if start <= e["occurredAt"] < end]
    exported = _lines(await _export(client, world["key"], occurredFrom=start, occurredTo=end))
    assert [e["id"] for e in exported] == expected
    assert all(start <= e["occurredAt"] < end for e in exported)


async def test_another_tenant_exports_none_of_these_events(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    world = await _world(client)
    _, other_key = make_tenant_directly(sync_engine, "other")
    period = _period()
    mine = {e["id"] for e in await _events(client, world["key"], **period)}
    theirs = _lines(await _export(client, other_key, **period))
    assert not mine & {e["id"] for e in theirs}
    assert _lines(await _export(client, other_key, actorId=world["agentId"], **period)) == []


# --- limits ------------------------------------------------------------------------


async def test_a_period_is_required(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    period = _period()
    for params in (
        {},
        {"occurredFrom": period["occurredFrom"]},
        {"occurredTo": period["occurredTo"]},
    ):
        response = await _export(client, key, **params)
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "export_period_required"
        assert error["details"]["maxPeriodDays"] == 92


async def test_a_period_longer_than_the_limit_is_refused(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    start = datetime(2026, 7, 1, tzinfo=UTC)
    # A quarter fits exactly (92 days), a day more does not.
    quarter = await _export(
        client,
        key,
        occurredFrom=start.isoformat(),
        occurredTo=(start + timedelta(days=92)).isoformat(),
    )
    assert quarter.status_code == 200, quarter.text
    longer = await _export(
        client,
        key,
        occurredFrom=start.isoformat(),
        occurredTo=(start + timedelta(days=92, seconds=1)).isoformat(),
    )
    assert longer.status_code == 422, longer.text
    error = longer.json()["error"]
    assert error["code"] == "export_period_too_long"
    assert error["details"]["maxPeriodDays"] == 92


async def test_a_naive_or_backward_period_is_refused(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for params in (
        {"occurredFrom": "2026-07-01T00:00:00", "occurredTo": "2026-07-02T00:00:00Z"},
        {"occurredFrom": "2026-07-02T00:00:00Z", "occurredTo": "2026-07-01T00:00:00Z"},
    ):
        response = await _export(client, key, **params)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_event_period"
    garbage = await _export(client, key, occurredFrom="yesterday", occurredTo="today")
    assert garbage.status_code == 400, garbage.text


async def test_more_events_than_the_limit_is_refused_before_the_body(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    world = await _world(client)
    period = _period()
    count = len(await _events(client, world["key"], types="task.", **period))
    app.state.settings = app.state.settings.model_copy(update={"events_export_max_events": count})
    fits = await _export(client, world["key"], types="task.", **period)
    assert fits.status_code == 200, fits.text
    assert len(_lines(fits)) == count

    app.state.settings = app.state.settings.model_copy(
        update={"events_export_max_events": count - 1}
    )
    response = await _export(client, world["key"], "csv", types="task.", **period)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == "export_too_large"
    assert error["details"] == {"maxEvents": count - 1}
    # A refused export is not journaled as one.
    audits = await _events(client, world["key"], types="event_journal.exported")
    assert len(audits) == 1


async def test_format_is_required_and_known(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    period = _period()
    missing = await client.get(EXPORT, params=period, headers=auth(key))
    assert missing.status_code == 400, missing.text
    unknown = await _export(client, key, "xml", **period)
    assert unknown.status_code == 400, unknown.text
    stray = await _export(client, key, limit=10, **period)
    assert stray.status_code == 400, stray.text
    assert stray.json()["error"]["code"] == "invalid_request"


# --- the right and the audit -------------------------------------------------------


async def test_events_export_is_a_right_apart_from_events_read(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    _, reader = await create_agent_with_key(client, key, name="reader", permissions=["events.read"])
    _, exporter = await create_agent_with_key(
        client, key, name="exporter", permissions=["events.export"]
    )
    _, auditor = await create_agent_with_key(
        client, key, name="auditor", permissions=["events.read", "events.export"]
    )
    period = _period()
    for refused in (reader, exporter):
        response = await _export(client, refused, **period)
        assert response.status_code == 403, response.text
    assert (await _export(client, auditor, **period)).status_code == 200
    anonymous = await client.get(EXPORT, params={"format": "jsonl", **period})
    assert anonymous.status_code == 401, anonymous.text


async def test_an_export_is_journaled_with_its_filters_and_without_data(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    period = _period()
    response = await _export(
        client,
        world["key"],
        "csv",
        types="task.",
        actorId=world["agentId"],
        workspaceId=world["ops"],
        includeDescendants="false",
        **period,
    )
    count = len(_rows(response))
    audits = await _events(client, world["key"], types="event_journal.exported")
    assert len(audits) == 1
    audit = audits[0]
    assert audit["actorId"] == world["adminId"]
    assert audit["entityType"] == "event_journal"
    assert audit["workspaceId"] is None
    assert audit["schemaVersion"] == 1
    payload = audit["payload"]
    assert payload["throughCursor"].startswith("ec1_")
    del payload["throughCursor"]
    assert payload == {
        "format": "csv",
        "types": ["task."],
        "entityType": None,
        "entityId": None,
        "actorId": world["agentId"],
        "occurredFrom": datetime.fromisoformat(period["occurredFrom"]).isoformat(),
        "occurredTo": datetime.fromisoformat(period["occurredTo"]).isoformat(),
        "workspaceId": world["ops"],
        "includeDescendants": False,
        "events": count,
    }


async def test_repeated_and_parallel_exports_hand_out_the_same_body(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    period = _period()
    first, second = await asyncio.gather(
        _export(client, world["key"], types="task.", **period),
        _export(client, world["key"], types="task.", **period),
    )
    third = await _export(client, world["key"], types="task.", **period)
    assert _lines(first) == _lines(second) == _lines(third)
    assert len(await _events(client, world["key"], types="event_journal.exported")) == 3


# --- visibility --------------------------------------------------------------------


async def _auditor(client: httpx.AsyncClient) -> tuple[Any, str]:
    """The people-access tree; an auditor in members mode, a member of dept."""
    tree = await make_tree(client)
    auditor, key = await create_agent_with_key(
        client,
        tree.admin,
        name="auditor",
        permissions=["events.read", "events.export"],
        kind="human",
    )
    await tree.add_member("dept", auditor["id"])
    await tree.narrow(auditor["id"])
    return tree, key


async def test_a_members_auditor_exports_what_they_may_read(client: httpx.AsyncClient) -> None:
    tree, key = await _auditor(client)
    period = _period()
    readable = await _events(client, key, **period)
    exported = _lines(await _export(client, key, **period))
    assert exported == readable
    workspaces = {e["workspaceId"] for e in exported}
    assert tree.ws["dept"] in workspaces
    assert not workspaces & {tree.ws["other"], tree.ws["company"]}
    everything = _lines(await _export(client, tree.admin, **period))
    assert tree.ws["other"] in {e["workspaceId"] for e in everything}


async def test_an_invisible_workspace_exports_as_a_missing_one(client: httpx.AsyncClient) -> None:
    tree, key = await _auditor(client)
    period = _period()
    for invisible in (tree.ws["company"], tree.ws["other"]):
        same_as_missing(
            await _export(client, key, workspaceId=invisible, **period),
            await _export(client, key, workspaceId=MISSING, **period),
            (invisible, MISSING),
        )
