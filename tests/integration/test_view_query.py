"""The data under a view: ``POST /views/{key}:query`` (CP-ADR-0080 amendment A, TAI-ADR-0066 p.4).

A package that is not the platform's own domain (``deals``) installs a list
view and a card view over its process; its instances are one started through
the API (the template, a ``draft`` the source filter of the list leaves out)
and copies of it written straight into the table with their own data, stages
and workspaces. Checked:

- every block answers in the form agreed with the console
  (:mod:`tests.unit.test_view_query_contract`);
- ``filter`` and ``sort`` name only the fields the block declares (else
  ``422``), with the operators of the type of the filter;
- a page and its cursor; the sort of the request and the package's own;
- aggregates of ``metrics`` and ``chart``, amounts kept as strings of decimal
  notation summed with ``decimal()``;
- the source filter holds for every block; a source filter SQL does not say
  is evaluated record by record with the same result;
- a field of ``data`` the view does not show is in no answer;
- an instance the caller may not read and a view the caller may not see are
  the 404 of a missing one.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.queries import view_data
from control_plane.config import Settings
from control_plane.domain.errors import NotFoundError
from control_plane.infrastructure.db.engine import transaction
from tests.fake_graph_memory import FakeGraphMemory
from tests.helpers import auth, create_agent_with_key, create_workspace
from tests.integration.test_package_plan import _apply, _errors, _plan, _spec
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import _setup
from tests.unit.test_view_query_contract import assert_query_form

PACKAGE = "deals"
PROCESS = "deal"
SECRET = "do-not-leak"
DATA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "customer": {"type": "string"},
        "kind": {"type": "string", "enum": ["goods", "works", "draft"]},
        "urgent": {"type": "boolean"},
        "amount": {"type": "number"},
        "price": {
            "type": "object",
            "properties": {"amount": {"type": "string"}, "currency": {"type": "string"}},
        },
        "deadline": {"type": "string", "format": "date"},
        "secret": {"type": "string"},
        "decision": {"type": "string"},
    },
}
STAGES = ("work", "archive")
TEXTS = {
    "deals.list.title": "Deals",
    "deals.card.title": "Deal",
    "deals.col.customer": "Customer",
    "deals.col.price": "Price",
    "deals.m.count": "Deals",
    "deals.m.total": "Total",
    "deals.m.urgent": "Urgent",
    "deals.m.avg": "Average",
    "deals.m.max": "Largest",
    "deals.chart": "By kind",
    "deals.fields.title": "Title",
    "deals.fields.customer": "Customer",
    "deals.fields.kind": "Kind",
    "deals.fields.amount": "Amount",
    "deals.fields.deadline": "Deadline",
    "deals.fields.urgent": "Urgent",
    "deals.fields.stage": "Stage",
    "deals.fields.status": "Status",
    "deals.fields.price.amount": "Price",
    "deals.fields.stage.work": "In work",
    "deals.fields.status.running": "Running",
}
RU = {**TEXTS, "deals.fields.stage.work": "Работа", "deals.fields.status.running": "Идёт"}

TABLE = {
    "block": "table",
    "columns": [
        {"field": "data.title"},
        {"label": "deals.col.customer", "field": "data.customer"},
        {"value": "decimal(data.price.amount)", "format": "money", "key": "price"},
        {"field": "stage", "format": "status"},
        {"field": "data.deadline", "format": "date"},
    ],
    "filters": [
        "data.kind",
        "data.customer",
        "data.amount",
        "data.deadline",
        "data.urgent",
        "stage",
        "status",
    ],
    "sort": [{"field": "data.amount", "dir": "desc"}, {"field": "data.customer"}],
    "open": {"view": "deal-card", "id": "id"},
    "pageSize": 2,
}
METRICS = {
    "block": "metrics",
    "items": [
        {"title": "deals.m.count", "value": "count()"},
        {"title": "deals.m.total", "value": "sum(decimal(data.price.amount))", "format": "money"},
        {"title": "deals.m.urgent", "value": "count(data.urgent == true)"},
        {"title": "deals.m.avg", "value": "avg(data.amount)", "format": "number"},
        {"title": "deals.m.max", "value": "max(data.amount)"},
    ],
}
CHART = {
    "block": "chart",
    "title": "deals.chart",
    "chart": "bar",
    "groupBy": "data.kind",
    "value": "sum(data.amount)",
}
BOARD = {
    "block": "board",
    "columns": "stages",
    "card": {
        "title": "data.title",
        "subtitle": "data.customer",
        "fields": [{"value": "decimal(data.price.amount)", "format": "money", "key": "price"}],
        "badge": "status",
    },
    "open": {"view": "deal-card", "id": "id"},
    "filters": ["data.kind"],
}
LIST = {"block": "list", "columns": [{"field": "data.title"}]}
# Blocks by their index in the layout of the list view.
B_TABLE, B_METRICS, B_CHART, B_BOARD, B_LIST = range(5)


def _list_view(source_filter: str) -> dict[str, Any]:
    return {
        "title": "deals.list.title",
        "source": {"process": PROCESS, "filter": source_filter},
        "layout": [TABLE, METRICS, CHART, BOARD, LIST],
    }


CARD = {
    "title": "deals.card.title",
    "source": {"process": PROCESS, "instance": "param.id"},
    "layout": [
        {"block": "header", "title": "data.title", "status": "stage"},
        {
            "block": "fields",
            "items": [
                {"label": "deals.col.customer", "field": "data.customer"},
                {
                    "label": "deals.col.price",
                    "value": "decimal(data.price.amount)",
                    "format": "money",
                },
            ],
        },
        {"block": "steps"},
        {"block": "timeline"},
        {"block": "artifacts"},
    ],
}
B_HEADER, B_FIELDS, B_STEPS, B_TIMELINE, B_ARTIFACTS = range(5)
# Translates to SQL / does not (size() is no part of the translated CEL): the same instances.
EXACT = 'data.kind != "draft"'
EVALUATED = 'size(data.kind) > 0 && data.kind != "draft"'


def _doc(kind: str, key: str, spec: dict[str, Any]) -> str:
    return yaml.safe_dump(
        {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec},
        sort_keys=False,
        allow_unicode=True,
    )


def _process(admin: str) -> dict[str, Any]:
    spec = _spec(admin)
    spec["data"] = DATA
    spec["displayName"] = "Deal"
    spec["stages"] = [
        spec["stages"][0],
        {
            "id": "archive",
            "displayName": "Archive",
            "entry": "stage.work.completed",
            "steps": [{"id": "shelve", "complete": {"outcome": "shelved"}}],
        },
    ]
    return spec


def _package(admin: str, views: dict[str, dict[str, Any]]) -> dict[str, Any]:
    head = {"version": "1.0.0", "displayName": "Deals", "locales": ["en", "ru"]}
    head["defaultLocale"] = "en"
    files = [
        ("package.yaml", _doc("Package", PACKAGE, head)),
        ("processes/deal.yaml", _doc("Process", PROCESS, _process(admin))),
        ("i18n/en.yaml", yaml.safe_dump(TEXTS, allow_unicode=True)),
        ("i18n/ru.yaml", yaml.safe_dump(RU, allow_unicode=True)),
    ]
    files += [(f"views/{key}.yaml", _doc("View", key, spec)) for key, spec in views.items()]
    return {"files": [{"path": path, "content": content} for path, content in files]}


RELATED_CARD = {
    "title": "deals.card.title",
    "source": {"process": PROCESS, "instance": "param.id"},
    "layout": [
        {
            "block": "related",
            "knowledge": {"kind": "endpoint", "key": "data.customer"},
            "include": {"relations": ["calls"], "direction": "in", "limit": 2},
        }
    ],
}
VIEWS = {
    "deals": _list_view(EXACT),
    "deals-evaluated": _list_view(EVALUATED),
    "deal-card": CARD,
    "deal-related": RELATED_CARD,
}


async def _install(client: httpx.AsyncClient, key: str, admin: str) -> None:
    package = _package(admin, VIEWS)
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text


async def _template(client: httpx.AsyncClient, key: str) -> str:
    created = await client.post(
        "/api/v1/process-instances",
        json={
            "process": PROCESS,
            "key": "draft-0",
            "data": {"title": "Draft", "kind": "draft", "secret": SECRET},
        },
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


_CLONE = text(
    """
    INSERT INTO process_instances (
        id, tenant_id, workspace_id, definition_id, definition_key, definition_version,
        instance_key, status, outcome, error, data, state, refs, step_attempts,
        sla_due_at, sla_warn_at, parent_instance_id, parent_activity_id, started_by,
        started_at, updated_at, completed_at)
    SELECT :id, tenant_id, :workspace, definition_id, definition_key, definition_version,
        :key, :status, outcome, error, CAST(:data AS jsonb),
        jsonb_set(jsonb_set(state, '{stages}', CAST(:stages AS jsonb)),
                  '{startedAt}', to_jsonb(CAST(:started_text AS text))),
        '{}'::jsonb, step_attempts, NULL, NULL, NULL, NULL, started_by,
        :started, :started, NULL
      FROM process_instances WHERE id = :template
    """
)


def _stages(stage: str | None) -> str:
    states = {sid: {"state": "available"} for sid in STAGES}
    if stage == "archive":
        states["work"] = {"state": "completed"}
    if stage is not None:
        states[stage] = {"state": "active"}
    return json.dumps(states)


def seed(
    sync_engine: Engine,
    template: str,
    rows: list[dict[str, Any]],
    *,
    start: datetime | None = None,
) -> list[str]:
    """Copies of the template instance: their data, stage, status and workspace; newest last."""
    base = start or datetime(2026, 10, 1, tzinfo=UTC)
    ids = []
    with sync_engine.begin() as conn:
        for n, row in enumerate(rows):
            ident = str(uuid.uuid4())
            started = base + timedelta(minutes=n)
            conn.execute(
                _CLONE,
                {
                    "id": ident,
                    "workspace": row.get("workspace"),
                    "key": row.get("key", f"deal-{n}"),
                    "status": row.get("status", "running"),
                    "data": json.dumps({**row.get("data", {}), "secret": SECRET}),
                    "stages": _stages(row.get("stage", "work")),
                    "started": started,
                    "started_text": started.isoformat().replace("+00:00", "Z"),
                    "template": template,
                },
            )
            ids.append(ident)
    return ids


def _deal(n: int, **data: Any) -> dict[str, Any]:
    return {
        "title": f"Deal {n}",
        "customer": f"cust-{n % 3}",
        "kind": "goods" if n % 2 else "works",
        "urgent": n % 3 == 0,
        "amount": float(n * 10),
        "price": {"amount": f"{n * 1000}.50", "currency": "RUB"},
        "deadline": f"2026-11-{n + 1:02d}",
        **data,
    }


async def _query(client: httpx.AsyncClient, key: str, view: str, **body: Any) -> httpx.Response:
    locale = body.pop("locale", None)
    params = {"locale": locale} if locale else {}
    return await client.post(
        f"/api/v1/views/{view}:query", json=body, params=params, headers=auth(key)
    )


async def _ok(client: httpx.AsyncClient, key: str, view: str, **body: Any) -> dict[str, Any]:
    response = await _query(client, key, view, **body)
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    assert SECRET not in response.text
    return out


@pytest.fixture
async def world(client: httpx.AsyncClient, sync_engine: Engine) -> dict[str, Any]:
    s = await _setup(client)
    await _install(client, s["key"], s["admin"])
    s["template"] = await _template(client, s["key"])
    s["ids"] = seed(sync_engine, s["template"], [{"data": _deal(n)} for n in range(1, 7)])
    _, s["reader"] = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["processes.read"], kind="human"
    )
    return s


# --- the blocks and their forms -------------------------------------------------------------------


async def test_a_page_of_a_table_answers_the_values_of_its_columns_by_their_keys(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    page = await _ok(client, world["reader"], "deals", block=B_TABLE, locale="ru")
    assert_query_form("table", page)
    # The package's own order: by amount, the largest first; pageSize 2.
    assert [row["title"] for row in page["items"]] == ["deal-5", "deal-4"]
    first = page["items"][0]
    assert first["id"] == world["ids"][5]
    assert first["values"] == {
        "title": "Deal 6",
        "customer": "cust-0",
        "price": {"amount": "6000.50", "currency": "RUB"},
        "stage": {"title": "Работа", "category": "running"},
        "deadline": "2026-11-07",
    }
    assert page["nextCursor"] is not None


async def test_the_cursor_walks_the_whole_set_once_and_the_draft_is_never_in_it(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    for view in ("deals", "deals-evaluated"):
        seen: list[str] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"block": B_TABLE, "limit": 4}
            if cursor:
                body["cursor"] = cursor
            page = await _ok(client, world["reader"], view, **body)
            seen += [row["title"] for row in page["items"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert seen == [f"deal-{n}" for n in range(5, -1, -1)], view
        assert world["template"] not in json.dumps(seen)


async def test_the_sort_of_the_request_with_missing_values_last(
    client: httpx.AsyncClient, world: dict[str, Any], sync_engine: Engine
) -> None:
    seed(
        sync_engine, world["template"], [{"key": "nobody", "data": {"title": "N", "kind": "goods"}}]
    )
    for view in ("deals", "deals-evaluated"):
        titles = []
        cursor = None
        while True:
            body: dict[str, Any] = {
                "block": B_TABLE,
                "limit": 3,
                "sort": [{"field": "customer", "dir": "asc"}, {"field": "amount", "dir": "asc"}],
            }
            if cursor:
                body["cursor"] = cursor
            page = await _ok(client, world["reader"], view, **body)
            titles += [row["title"] for row in page["items"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
        # cust-0: 3, 6; cust-1: 1, 4; cust-2: 2, 5; no customer last.
        assert titles == ["deal-2", "deal-5", "deal-0", "deal-3", "deal-1", "deal-4", "nobody"]


async def test_filters_by_type_and_operator(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    async def titles(*conditions: dict[str, Any]) -> list[str]:
        out = []
        for view in ("deals", "deals-evaluated"):
            page = await _ok(
                client,
                world["reader"],
                view,
                block=B_TABLE,
                limit=50,
                filter=list(conditions),
                sort=[{"field": "amount", "dir": "asc"}],
            )
            out.append([row["values"]["title"] for row in page["items"]])
        assert out[0] == out[1]
        return out[0]

    assert await titles({"field": "kind", "op": "eq", "value": "goods"}) == [
        "Deal 1",
        "Deal 3",
        "Deal 5",
    ]
    assert await titles({"field": "kind", "op": "in", "value": ["works"]}) == [
        "Deal 2",
        "Deal 4",
        "Deal 6",
    ]
    assert await titles({"field": "urgent", "op": "eq", "value": True}) == ["Deal 3", "Deal 6"]
    assert await titles(
        {"field": "amount", "op": "gte", "value": 20}, {"field": "amount", "op": "lte", "value": 40}
    ) == ["Deal 2", "Deal 3", "Deal 4"]
    assert await titles({"field": "customer", "op": "prefix", "value": "CUST-1"}) == [
        "Deal 1",
        "Deal 4",
    ]
    assert await titles({"field": "deadline", "op": "gte", "value": "2026-11-06"}) == [
        "Deal 5",
        "Deal 6",
    ]
    assert await titles({"field": "deadline", "op": "eq", "value": "2026-11-02"}) == ["Deal 1"]
    assert await titles({"field": "stage", "op": "eq", "value": "work"}) == [
        f"Deal {n}" for n in range(1, 7)
    ]
    assert await titles({"field": "stage", "op": "eq", "value": "archive"}) == []
    assert await titles({"field": "status", "op": "in", "value": ["running"]}) == [
        f"Deal {n}" for n in range(1, 7)
    ]
    # A prefix is a prefix: % and _ are letters of it, not patterns.
    assert await titles({"field": "customer", "op": "prefix", "value": "%"}) == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"filter": [{"field": "secret", "op": "eq", "value": SECRET}]}, "undeclared_filter"),
        ({"filter": [{"field": "title", "op": "eq", "value": "x"}]}, "undeclared_filter"),
        ({"sort": [{"field": "title", "dir": "asc"}]}, "undeclared_sort"),
        ({"filter": [{"field": "kind", "op": "prefix", "value": "g"}]}, "invalid_filter"),
        ({"filter": [{"field": "amount", "op": "gte", "value": "many"}]}, "invalid_filter"),
        ({"filter": [{"field": "deadline", "op": "gte", "value": "11/06/2026"}]}, "invalid_filter"),
        ({"filter": [{"field": "kind", "op": "in", "value": []}]}, "invalid_filter"),
        ({"params": {"nothing": 1}}, "unknown_param"),
        ({"cursor": "not-a-cursor"}, "invalid_cursor"),
    ],
)
async def test_what_the_block_does_not_declare_is_422(
    client: httpx.AsyncClient, world: dict[str, Any], body: dict[str, Any], code: str
) -> None:
    response = await _query(client, world["reader"], "deals", block=B_TABLE, **body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert SECRET not in response.text


async def test_a_block_out_of_the_layout_or_with_no_data_and_a_wrong_operator_are_422(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    missing = await _query(client, world["reader"], "deals", block=9)
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "unknown_block"
    # An operator outside the closed list is no request of the contract at all.
    wrong = await _query(
        client,
        world["reader"],
        "deals",
        block=B_TABLE,
        filter=[{"field": "kind", "op": "like", "value": "g"}],
    )
    assert wrong.status_code == 400, wrong.text
    # A metrics block declares no filter, no sort and no page.
    for body in (
        {"filter": [{"field": "kind", "op": "eq", "value": "goods"}]},
        {"sort": [{"field": "amount"}]},
        {"cursor": "abc"},
    ):
        refused = await _query(client, world["reader"], "deals", block=B_METRICS, **body)
        assert refused.status_code == 422, refused.text


async def test_a_cursor_of_another_order_is_refused(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    page = await _ok(client, world["reader"], "deals", block=B_TABLE)
    other = await _query(
        client,
        world["reader"],
        "deals",
        block=B_TABLE,
        cursor=page["nextCursor"],
        sort=[{"field": "customer"}],
    )
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "invalid_cursor"


async def test_aggregates_of_metrics_and_chart_over_the_filtered_source(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    for view in ("deals", "deals-evaluated"):
        metrics = await _ok(client, world["reader"], view, block=B_METRICS)
        assert_query_form("metrics", metrics)
        total = sum(n * 1000 + 0.5 for n in range(1, 7))
        count, money, urgent, average, largest = metrics["values"]
        assert count == 6, view
        assert money["currency"] == "RUB"
        assert float(money["amount"]) == pytest.approx(total)
        assert (urgent, average, largest) == (2, 35, 60), view
        chart = await _ok(client, world["reader"], view, block=B_CHART)
        assert_query_form("chart", chart)
        assert chart["points"] == [
            {"label": "goods", "value": 90},
            {"label": "works", "value": 120},
        ], view
    # Exactly, in SQL: the decimal notation of the amounts is kept.
    exact = await _ok(client, world["reader"], "deals", block=B_METRICS)
    assert exact["values"][1]["amount"] == "21003.00"


async def test_a_board_puts_each_instance_in_the_column_of_its_stage(
    client: httpx.AsyncClient, world: dict[str, Any], sync_engine: Engine
) -> None:
    seed(
        sync_engine,
        world["template"],
        [{"key": "old", "stage": "archive", "data": _deal(9, title="Old")}],
    )
    for view in ("deals", "deals-evaluated"):
        board = await _ok(client, world["reader"], view, block=B_BOARD, locale="en")
        assert_query_form("board", board)
        work, archive = board["columns"]
        assert (work["key"], work["title"]) == ("work", "In work")
        assert (archive["key"], archive["title"]) == ("archive", "Archive")
        assert [c["title"] for c in work["items"]] == [f"Deal {n}" for n in range(6, 0, -1)]
        [old] = archive["items"]
        assert old["subtitle"] == "cust-0"
        assert old["values"] == {"price": {"amount": "9000.50", "currency": "RUB"}}
        assert old["badge"] == {"title": "Running", "category": "running"}
        filtered = await _ok(
            client,
            world["reader"],
            view,
            block=B_BOARD,
            limit=1,
            filter=[{"field": "kind", "op": "eq", "value": "works"}],
        )
        assert [len(c["items"]) for c in filtered["columns"]] == [1, 0]


async def test_a_card_answers_its_header_fields_steps_timeline_and_artifacts(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    ident = world["ids"][0]
    header = await _ok(
        client, world["reader"], "deal-card", block=B_HEADER, params={"id": ident}, locale="ru"
    )
    assert_query_form("header", header)
    assert header == {"title": "Deal 1", "status": {"title": "Работа", "category": "running"}}
    fields = await _ok(client, world["reader"], "deal-card", block=B_FIELDS, params={"id": ident})
    assert_query_form("fields", fields)
    assert fields["values"] == {
        "customer": "cust-1",
        "price.amount": {"amount": "1000.50", "currency": "RUB"},
    }
    template = world["template"]
    steps = await _ok(client, world["key"], "deal-card", block=B_STEPS, params={"id": template})
    assert_query_form("steps", steps)
    [review] = steps["items"]
    assert review["key"] == "review"
    assert review["task"]["ref"].startswith("TASK-") or review["task"]["ref"]
    assert review["assignee"] == world["admin"]
    timeline = await _ok(
        client, world["reader"], "deal-card", block=B_TIMELINE, params={"id": template}
    )
    assert_query_form("timeline", timeline)
    assert timeline["items"] and all("at" in item for item in timeline["items"])
    artifacts = await _ok(
        client, world["reader"], "deal-card", block=B_ARTIFACTS, params={"id": template}
    )
    assert artifacts == {"items": []}


async def test_a_suspended_instance_is_of_the_category_suspended(
    client: httpx.AsyncClient, world: dict[str, Any], sync_engine: Engine
) -> None:
    [ident] = seed(
        sync_engine,
        world["template"],
        [{"key": "paused", "status": "suspended", "data": _deal(7, title="Paused")}],
    )
    header = await _ok(
        client, world["reader"], "deal-card", block=B_HEADER, params={"id": ident}, locale="ru"
    )
    assert header["status"] == {"title": "Работа", "category": "suspended"}
    board = await _ok(client, world["reader"], "deals", block=B_BOARD, locale="en")
    [paused] = [c for c in board["columns"][0]["items"] if c["title"] == "Paused"]
    assert paused["badge"]["category"] == "suspended"


async def test_a_view_evaluated_past_the_cap_is_refused_and_one_in_sql_is_not(
    client: httpx.AsyncClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seven instances at most are evaluated record by record (six deals and the draft).
    monkeypatch.setattr(view_data, "MAX_EVALUATED", 7)
    for block in (B_TABLE, B_METRICS, B_CHART, B_BOARD):
        await _ok(client, world["reader"], "deals-evaluated", block=block)
    monkeypatch.setattr(view_data, "MAX_EVALUATED", 3)
    for block in (B_TABLE, B_METRICS, B_CHART, B_BOARD):
        refused = await _query(client, world["reader"], "deals-evaluated", block=block)
        assert refused.status_code == 409, (block, refused.text)
        error = refused.json()["error"]
        assert error["code"] == "view_too_costly"
        assert error["details"] == {"key": "deals-evaluated", "limit": 3}
        assert SECRET not in refused.text
        # The same view whose source.filter SQL says evaluates nothing record by record.
        await _ok(client, world["reader"], "deals", block=block)


async def test_an_instance_of_another_process_or_no_instance_is_a_404(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    for ident in (str(uuid.uuid4()), "not-a-uuid"):
        response = await _query(
            client, world["reader"], "deal-card", block=B_HEADER, params={"id": ident}
        )
        assert response.status_code == 404, response.text
    missing = await _query(client, world["reader"], "deal-card", block=B_HEADER)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "missing_param"


async def test_a_view_the_caller_may_not_see_is_a_404_like_a_missing_one(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    _, nobody = await create_agent_with_key(
        client, world["key"], name="nobody", permissions=["tasks.read"], kind="human"
    )
    hidden = await _query(client, nobody, "deals", block=B_TABLE)
    absent = await _query(client, world["reader"], "no-such-view", block=B_TABLE)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"
    assert SECRET not in hidden.text


# --- the caller's workspaces (policy mode) ------------------------------------------------------


@dataclass
class _ProcessesIn:
    """A PDP granting ``processes.read`` on the tenant and on ``readable`` workspaces only."""

    readable: set[str]
    tenant: str

    async def check(  # type: ignore[no-untyped-def]
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):
        allowed = action == "processes.read" and (
            resource.key == f"tenant:{self.tenant}"
            or resource.key in {f"workspace:{w}" for w in self.readable}
        )
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(self, ctx, action, resource_type, **kwargs):  # type: ignore[no-untyped-def]
        objects = sorted(self.readable) if action == "processes.read" else []
        return ObjectPage(objects=objects, cursor=None, model_version="1")


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_instances_of_a_workspace_the_caller_may_not_read_are_not_there(
    client: httpx.AsyncClient,
    app: FastAPI,
    settings: Settings,
    sync_engine: Engine,
    world: dict[str, Any],
    restore_authorizer: None,
) -> None:
    mine = await create_workspace(client, world["key"], "mine")
    theirs = await create_workspace(client, world["key"], "theirs")
    here, there = seed(
        sync_engine,
        world["template"],
        [
            {"key": "mine-1", "workspace": mine["id"], "data": _deal(7, urgent=True)},
            {"key": "theirs-1", "workspace": theirs["id"], "data": _deal(8, urgent=True)},
        ],
    )
    with sync_engine.connect() as conn:
        tenant = str(
            conn.execute(
                text("SELECT tenant_id FROM process_instances WHERE id = :id"),
                {"id": world["template"]},
            ).scalar_one()
        )
    configure_authorizer(Authorizer(_ProcessesIn({mine["id"]}, tenant), "policy"))
    ctx = AuthContext(
        tenant_id=uuid.UUID(tenant),
        principal_id=uuid.UUID(world["admin"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )

    async def ask(view: str, **body: Any) -> dict[str, Any]:
        async with transaction(app.state.session_factory) as db:
            answer = await view_data.prepare(
                db, ctx, settings, view, view_data.ViewQuery(**body), locale=None
            )
        assert answer.body is not None
        return answer.body

    for view in ("deals", "deals-evaluated"):
        page = await ask(view, block=B_TABLE, limit=50)
        titles = {row["title"] for row in page["items"]}
        assert "mine-1" in titles and "theirs-1" not in titles, view
        metrics = await ask(view, block=B_METRICS)
        assert metrics["values"][0] == 7 and metrics["values"][2] == 3, view
        board = await ask(view, block=B_BOARD)
        assert "theirs-1" not in json.dumps(board)
    assert (await ask("deal-card", block=B_HEADER, params={"id": here}))["title"] == "Deal 7"
    # Another workspace's instance and a missing one: the same 404.
    for ident in (there, str(uuid.uuid4())):
        with pytest.raises(NotFoundError) as refused:
            await ask("deal-card", block=B_FIELDS, params={"id": ident})
        assert refused.value.message == "Process instance not found"


# --- related: the relations of a knowledge record, read from Memory -------------------------------


async def test_related_answers_the_relations_of_the_record_the_instance_names(
    client: httpx.AsyncClient, app: FastAPI, world: dict[str, Any], sync_engine: Engine
) -> None:
    workspace = await create_workspace(client, world["key"], "sales")
    endpoint = "POST /tasks/{}:claim"
    [named, outside] = seed(
        sync_engine,
        world["template"],
        [
            {"key": "in-sales", "workspace": workspace["id"], "data": _deal(1, customer=endpoint)},
            {"key": "nowhere", "data": _deal(2, customer=endpoint)},
        ],
    )
    unavailable = await _query(client, world["key"], "deal-related", block=0, params={"id": named})
    assert unavailable.status_code == 503, unavailable.text
    memory = FakeGraphMemory()
    app.state.context_provider = memory
    try:
        related = await _ok(client, world["key"], "deal-related", block=0, params={"id": named})
        assert_query_form("related", related)
        assert related["items"] == [
            {
                "relation": "calls",
                "kind": "client_method",
                "key": "control-plane:control_plane_client.client.ControlPlaneClient.claim_task",
                "entityTitle": (
                    "control-plane:control_plane_client.client.ControlPlaneClient.claim_task"
                ),
                "direction": "in",
            },
            {
                "relation": "calls",
                "kind": "ui_call",
                "key": "platform-web:src/api/tasks.ts:42",
                "entityTitle": "platform-web:src/api/tasks.ts:42",
                "direction": "in",
            },
        ]
        [traversal] = memory.typed_requests
        assert traversal["anchors"] == [{"kind": "endpoint", "value": endpoint}]
        # An instance outside any workspace has no knowledge to start from: nothing asked.
        none = await _ok(client, world["key"], "deal-related", block=0, params={"id": outside})
        assert none == {"items": []}
        assert len(memory.typed_requests) == 1
    finally:
        app.state.context_provider = None
