"""Views of tasks and of knowledge: ``POST /views/{key}:query`` (CP-ADR-0080, amendment Б).

TAI-ADR-0066 stage 6. A package that is not the platform's own domain
(``ledger``) installs a task type and two views — one of its tasks, one of
the knowledge base. Tasks are created through the API in two workspaces;
the knowledge base is ``tests.fake_graph_memory.FakeGraphMemory`` with the
records of a pack whose kinds declare their attributes, pinned from
memory-service (``tests/fixtures/memory_kinds_contract.json``); every
request the core makes of it is validated against the pinned contract.
Checked for both sources: the answers are in the form agreed with the
console, filters and sorts are only the declared ones, a page and its
cursor, aggregates, the rights of the source — tasks as ``GET /tasks`` lists
them, records as ``entities:query`` answers them — and the plan's check of
the paths against the schemas.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from platform_auth import ObjectPage, PolicyDecision
from platform_auth.testing import SigningKey
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.queries import lists, view_data
from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.infrastructure.auth.iam import SCOPE_READ, SCOPE_WRITE
from control_plane.infrastructure.db.engine import transaction
from tests.fake_graph_memory import Edge, FakeGraphMemory, Node
from tests.helpers import auth, create_agent_with_key, create_workspace
from tests.integration.test_iam_enforcement import ISSUER, enable_iam
from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import _setup
from tests.integration.test_workspace_visibility import same_as_missing
from tests.unit.test_view_query_contract import assert_query_form

KINDS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_kinds_contract.json").read_text()
)
PACKAGE = "ledger"
TYPE = "invoice"
SECRET = "do-not-leak"
FIELDS = {
    "type": "object",
    "properties": {
        "number": {"type": "string"},
        "amount": {
            "type": "object",
            "properties": {"amount": {"type": "string"}, "currency": {"type": "string"}},
        },
        "total": {"type": "number"},
        "kind": {"type": "string", "enum": ["goods", "works"]},
        "due": {"type": "string", "format": "date"},
        "urgent": {"type": "boolean"},
        "note": {"type": "string"},
    },
}
INVOICES_TABLE = {
    "block": "table",
    "columns": [
        {"label": "ledger.col.number", "field": "customFields.number"},
        {"field": "fields.title"},
        {"value": "decimal(customFields.amount.amount)", "format": "money", "key": "amount"},
        {"field": "fields.status", "format": "status"},
        {"field": "customFields.due", "format": "date"},
    ],
    "filters": [
        "customFields.kind",
        "customFields.number",
        "customFields.total",
        "customFields.due",
        "customFields.urgent",
        "fields.status",
        "fields.priority",
    ],
    "sort": [{"field": "customFields.total", "dir": "desc"}, {"field": "fields.title"}],
    "open": {"view": "invoices", "id": "fields.publicId"},
    "pageSize": 2,
}
INVOICES_METRICS = {
    "block": "metrics",
    "items": [
        {"title": "ledger.m.count", "value": "count()"},
        {
            "title": "ledger.m.amount",
            "value": "sum(decimal(customFields.amount.amount))",
            "format": "money",
        },
        {"title": "ledger.m.urgent", "value": "count(customFields.urgent == true)"},
        {"title": "ledger.m.avg", "value": "avg(customFields.total)", "format": "number"},
        {"title": "ledger.m.max", "value": "max(customFields.total)"},
    ],
}
INVOICES_CHART = {
    "block": "chart",
    "chart": "bar",
    "title": "ledger.chart",
    "groupBy": "fields.status",
    "value": "sum(customFields.total)",
}
INVOICES = {
    "title": "ledger.invoices.title",
    "source": {"tasks": {"type": TYPE}},
    "params": {"inn": {"type": "string"}},
    "layout": [
        INVOICES_TABLE,
        INVOICES_METRICS,
        INVOICES_CHART,
        {"block": "list", "columns": [{"field": "fields.publicId"}]},
        {
            "block": "related",
            "knowledge": {"kind": "legal_entity", "key": "param.inn"},
            "include": {"relations": ["party_to"]},
        },
        {"block": "artifacts"},
    ],
}
I_TABLE, I_METRICS, I_CHART, I_LIST, I_RELATED, I_ARTIFACTS = range(6)
ENTITIES = {
    "title": "ledger.kb.title",
    "nav": {"group": "knowledge"},
    "source": {"knowledge": {"kinds": ["legal_entity", "contract"]}},
    "layout": [
        {
            "block": "table",
            "columns": [
                {"field": "title"},
                {"field": "attributes.inn"},
                {"field": "attributes.city"},
                {"field": "relations.party_to"},
                {"value": "decimal(attributes.amount)", "format": "money", "key": "amount"},
            ],
            "filters": ["kind", "attributes.city", "validFrom"],
            "sort": [{"field": "attributes.revenue", "dir": "desc"}],
            "open": {"view": "entities", "id": "key"},
            "pageSize": 2,
        },
        {
            "block": "metrics",
            "items": [
                {"title": "ledger.m.records", "value": "count()"},
                {"title": "ledger.m.revenue", "value": "sum(attributes.revenue)"},
                {"title": "ledger.m.contracts", "value": 'count(kind == "contract")'},
            ],
        },
        {"block": "chart", "chart": "donut", "groupBy": "kind", "value": "count()"},
    ],
}
K_TABLE, K_METRICS, K_CHART = range(3)
TEXTS = {
    "ledger.invoices.title": "Invoices",
    "ledger.kb.title": "Counterparties",
    "ledger.col.number": "Number",
    "ledger.m.count": "Invoices",
    "ledger.m.amount": "Amount",
    "ledger.m.urgent": "Urgent",
    "ledger.m.avg": "Average",
    "ledger.m.max": "Largest",
    "ledger.m.records": "Records",
    "ledger.m.revenue": "Revenue",
    "ledger.m.contracts": "Contracts",
    "ledger.chart": "By status",
    "ledger.fields.fields.status.todo": "To pay",
    **{
        f"ledger.fields.{path}": path
        for path in (
            "fields.title",
            "fields.status",
            "fields.priority",
            "fields.publicId",
            "customFields.due",
            "customFields.amount.amount",
            "customFields.kind",
            "customFields.number",
            "customFields.total",
            "customFields.urgent",
            "title",
            "kind",
            "validFrom",
            "attributes.inn",
            "attributes.city",
            "attributes.revenue",
            "attributes.amount",
            "relations.party_to",
        )
    },
}


def _doc(kind: str, key: str, spec: dict[str, Any]) -> str:
    return yaml.safe_dump(
        {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec},
        sort_keys=False,
        allow_unicode=True,
    )


def _package(views: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    head = {"version": "1.0.0", "displayName": "Ledger", "locales": ["en"], "defaultLocale": "en"}
    task_type = {
        "displayName": "Invoice",
        "fieldSchema": FIELDS,
        "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
    }
    files = [
        ("package.yaml", _doc("Package", PACKAGE, head)),
        ("task-types/invoice.yaml", _doc("TaskType", TYPE, task_type)),
        ("i18n/en.yaml", yaml.safe_dump(TEXTS, allow_unicode=True)),
    ]
    chosen = views if views is not None else {"invoices": INVOICES, "entities": ENTITIES}
    files += [(f"views/{key}.yaml", _doc("View", key, spec)) for key, spec in chosen.items()]
    return {"files": [{"path": path, "content": content} for path, content in files]}


class KnowledgeMemory(FakeGraphMemory):
    """The fake Memory with the pinned pack of kinds with attributes and records of them."""

    def __init__(self) -> None:
        super().__init__()
        records = [
            Node(
                "7701",
                "legal_entity",
                "Acme",
                attributes={"inn": "7701", "city": "Moscow", "revenue": 300, "secret": SECRET},
            ),
            Node(
                "7702",
                "legal_entity",
                "Birch",
                attributes={"inn": "7702", "city": "Kazan", "revenue": 100},
            ),
            Node("7703", "legal_entity", "Cedar", attributes={"inn": "7703", "city": "moscow"}),
            Node("C-1", "contract", "Supply 1", attributes={"amount": "1500.50"}),
            Node("C-2", "contract", "Supply 2", attributes={"amount": "99.00", "revenue": 200}),
            Node(
                "hidden",
                "legal_entity",
                "Hidden",
                attributes={"inn": "0000", "city": "Moscow"},
                scopes=("workspace:other",),
            ),
        ]
        self.nodes.update({n.key: n for n in records})
        self.edges += [
            Edge("7701", "party_to", "C-1", fact_id="f-1"),
            Edge("7701", "party_to", "C-2", fact_id="f-2"),
            Edge("7702", "party_to", "C-2", fact_id="f-3"),
            Edge("hidden", "party_to", "C-1", fact_id="f-4"),
        ]

    async def namespace_kinds(
        self, *, namespace: str, trace_run_id: str | None = None
    ) -> dict[str, Any]:
        self.kind_requests.append(namespace)
        if ":ws:" not in namespace:
            return {"settings": {"packages": None}, "catalog": {"packages": []}}
        return {**KINDS["namespaceKinds"], "settings": {"namespace": namespace}}

    async def get_package(
        self, *, name: str, version: str = "", namespace: str = "", trace_run_id: str | None = None
    ) -> dict[str, Any]:
        self.package_requests.append((name, version))
        assert f"{name}@{version}" == "deals-kb@1"
        package: dict[str, Any] = KINDS["package"]
        return package


async def _install(client: httpx.AsyncClient, key: str) -> None:
    package = _package()
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text


async def _task(client: httpx.AsyncClient, key: str, **body: Any) -> dict[str, Any]:
    created = await client.post("/api/v1/tasks", json={"typeKey": TYPE, **body}, headers=auth(key))
    assert created.status_code == 201, created.text
    out: dict[str, Any] = created.json()
    return out


def _invoice(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "number": f"INV-{n}",
        "amount": {"amount": f"{n * 1000}.50", "currency": "RUB"},
        "total": float(n * 10),
        "kind": "goods" if n % 2 else "works",
        "due": f"2026-11-{n + 1:02d}",
        "urgent": n % 3 == 0,
        "note": SECRET,
        **extra,
    }


async def _query(client: httpx.AsyncClient, key: str, view: str, **body: Any) -> httpx.Response:
    return await client.post(f"/api/v1/views/{view}:query", json=body, headers=auth(key))


async def _ok(client: httpx.AsyncClient, key: str, view: str, **body: Any) -> dict[str, Any]:
    response = await _query(client, key, view, **body)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    out: dict[str, Any] = response.json()
    return out


@pytest.fixture
async def world(client: httpx.AsyncClient, sync_engine: Engine) -> dict[str, Any]:
    s = await _setup(client)
    await _install(client, s["key"])
    s["sales"] = await create_workspace(client, s["key"], "sales")
    s["ops"] = await create_workspace(client, s["key"], "ops")
    s["tasks"] = []
    for n in range(1, 7):
        workspace = s["sales"] if n <= 4 else s["ops"]
        priority = ("low", "high", "critical")[n % 3]
        s["tasks"].append(
            await _task(
                client,
                s["key"],
                title=f"Invoice {n}",
                priority=priority,
                workspaceId=workspace["id"],
                customFields=_invoice(n),
            )
        )
    # The even ones are in progress: a new task starts in the initial status of its type.
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET status = 'in_progress' WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(t["id"]) for n, t in enumerate(s["tasks"], 1) if n % 2 == 0]},
        )
    # A task of another type is no task of the view.
    other = await client.post(
        "/api/v1/tasks",
        json={"typeKey": "review", "title": "Not an invoice", "workspaceId": s["sales"]["id"]},
        headers=auth(s["key"]),
    )
    assert other.status_code == 201, other.text
    _, s["reader"] = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["tasks.read"], kind="human"
    )
    _, s["knower"] = await create_agent_with_key(
        client, s["key"], name="knower", permissions=["events.read"], kind="human"
    )
    return s


# --- a view of tasks -----------------------------------------------------------------------------


async def test_a_page_of_tasks_answers_the_values_of_its_columns(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    page = await _ok(client, world["reader"], "invoices", block=I_TABLE)
    assert_query_form("table", page)
    # The package's order: total, largest first; two a page.
    [first, second] = page["items"]
    sixth = world["tasks"][5]
    assert first == {
        "id": sixth["publicId"],
        "title": "Invoice 6",
        "values": {
            "customFields.number": "INV-6",
            "fields.title": "Invoice 6",
            "amount": {"amount": "6000.50", "currency": "RUB"},
            "fields.status": {"title": "In progress", "category": "running"},
            "customFields.due": "2026-11-07",
        },
    }
    assert second["values"]["fields.status"] == {"title": "To pay", "category": "running"}
    assert page["nextCursor"]


async def test_the_cursor_walks_every_task_of_the_type_once(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        body: dict[str, Any] = {"block": I_TABLE}
        if cursor:
            body["cursor"] = cursor
        page = await _ok(client, world["reader"], "invoices", **body)
        seen += [row["title"] for row in page["items"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == [f"Invoice {n}" for n in range(6, 0, -1)]
    # A cursor of another order is not this query's.
    other = await _query(
        client,
        world["reader"],
        "invoices",
        block=I_TABLE,
        cursor=(await _ok(client, world["reader"], "invoices", block=I_TABLE))["nextCursor"],
        sort=[{"field": "fields.title"}],
    )
    assert other.status_code == 422 and other.json()["error"]["code"] == "invalid_cursor"


async def test_the_sort_of_the_request_with_missing_values_last(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await _task(client, world["key"], title="Invoice 0", customFields={"number": "INV-0"})
    page = await _ok(
        client,
        world["reader"],
        "invoices",
        block=I_TABLE,
        sort=[{"field": "customFields.total", "dir": "asc"}],
        limit=10,
    )
    assert [row["title"] for row in page["items"]] == [
        *(f"Invoice {n}" for n in range(1, 7)),
        "Invoice 0",
    ]
    by_title = await _ok(
        client,
        world["reader"],
        "invoices",
        block=I_TABLE,
        sort=[{"field": "fields.title", "dir": "desc"}],
        limit=3,
    )
    assert [row["title"] for row in by_title["items"]] == ["Invoice 6", "Invoice 5", "Invoice 4"]


@pytest.mark.parametrize(
    ("condition", "titles"),
    [
        ({"field": "customFields.kind", "op": "eq", "value": "goods"}, [5, 3, 1]),
        ({"field": "customFields.kind", "op": "in", "value": ["works"]}, [6, 4, 2]),
        ({"field": "customFields.number", "op": "prefix", "value": "inv-2"}, [2]),
        ({"field": "customFields.number", "op": "eq", "value": "INV-3"}, [3]),
        ({"field": "customFields.total", "op": "gte", "value": 40}, [6, 5, 4]),
        ({"field": "customFields.total", "op": "lte", "value": "20"}, [2, 1]),
        ({"field": "customFields.due", "op": "lte", "value": "2026-11-03"}, [2, 1]),
        ({"field": "customFields.due", "op": "eq", "value": "2026-11-05"}, [4]),
        ({"field": "customFields.urgent", "op": "eq", "value": True}, [6, 3]),
        ({"field": "fields.status", "op": "eq", "value": "in_progress"}, [6, 4, 2]),
        ({"field": "fields.priority", "op": "in", "value": ["critical", "low"]}, [6, 5, 3, 2]),
    ],
)
async def test_filters_of_tasks_by_type_and_operator(
    client: httpx.AsyncClient,
    world: dict[str, Any],
    condition: dict[str, Any],
    titles: list[int],
) -> None:
    page = await _ok(
        client, world["reader"], "invoices", block=I_TABLE, filter=[condition], limit=10
    )
    assert [row["title"] for row in page["items"]] == [f"Invoice {n}" for n in titles]


async def test_what_a_block_of_tasks_does_not_declare_is_422(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    cases = [
        (
            {"filter": [{"field": "customFields.note", "op": "eq", "value": "x"}]},
            "undeclared_filter",
        ),
        ({"filter": [{"field": "fields.title", "op": "eq", "value": "x"}]}, "undeclared_filter"),
        ({"sort": [{"field": "customFields.kind"}]}, "undeclared_sort"),
        (
            {"filter": [{"field": "customFields.kind", "op": "prefix", "value": "g"}]},
            "invalid_filter",
        ),
        (
            {"filter": [{"field": "customFields.total", "op": "gte", "value": "many"}]},
            "invalid_filter",
        ),
        (
            {"filter": [{"field": "customFields.due", "op": "eq", "value": "11/05"}]},
            "invalid_filter",
        ),
        ({"params": {"nope": 1}}, "unknown_param"),
        ({"params": {"inn": 7701}}, "invalid_param"),
    ]
    for body, code in cases:
        refused = await _query(client, world["reader"], "invoices", block=I_TABLE, **body)
        assert refused.status_code == 422, (body, refused.text)
        assert refused.json()["error"]["code"] == code, body
    # A block that shows one instance of a process has no data over tasks.
    artifacts = await _query(client, world["reader"], "invoices", block=I_ARTIFACTS)
    assert artifacts.status_code == 422
    assert artifacts.json()["error"]["code"] == "block_source_mismatch"
    beyond = await _query(client, world["reader"], "invoices", block=40)
    assert beyond.json()["error"]["code"] == "unknown_block"


async def test_aggregates_of_tasks(client: httpx.AsyncClient, world: dict[str, Any]) -> None:
    metrics = await _ok(client, world["reader"], "invoices", block=I_METRICS)
    assert_query_form("metrics", metrics)
    total = sum(n * 1000 + 0.5 for n in range(1, 7))
    assert metrics["values"] == [
        6,
        {"amount": total, "currency": "RUB"},
        2,
        35,
        60,
    ]
    chart = await _ok(client, world["reader"], "invoices", block=I_CHART)
    assert_query_form("chart", chart)
    # In the order of the statuses of the lifecycle, titled by the dictionary, else displayName.
    assert chart["points"] == [
        {"label": "To pay", "value": 90},
        {"label": "In progress", "value": 120},
    ]
    listed = await _ok(client, world["reader"], "invoices", block=I_LIST, limit=1)
    assert_query_form("list", listed)
    assert listed["items"][0]["values"] == {"fields.publicId": world["tasks"][5]["publicId"]}


async def test_a_view_of_tasks_the_caller_may_not_see_is_a_404(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    _, blind = await create_agent_with_key(
        client, world["key"], name="blind", permissions=["processes.read"], kind="human"
    )
    hidden = await _query(client, blind, "invoices", block=I_TABLE)
    absent = await _query(client, world["reader"], "no-such-view", block=I_TABLE)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"
    assert hidden.json()["error"]["message"] == absent.json()["error"]["message"]


@dataclass
class _TasksIn:
    """A PDP granting ``tasks.read`` on the tenant and on ``readable`` workspaces only."""

    readable: set[str]
    tenant: str

    async def check(  # type: ignore[no-untyped-def]
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):
        allowed = action == "tasks.read" and (
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
        objects = sorted(self.readable) if action == "tasks.read" else []
        return ObjectPage(objects=objects, cursor=None, model_version="1")


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_tasks_are_those_get_tasks_lists_to_the_caller(
    client: httpx.AsyncClient,
    app: FastAPI,
    settings: Settings,
    sync_engine: Engine,
    world: dict[str, Any],
    restore_authorizer: None,
) -> None:
    """Policy mode: the tasks of the readable workspace, and the caller's own elsewhere."""
    person, _ = await create_agent_with_key(
        client, world["key"], name="person", permissions=[], kind="human"
    )
    mine = await _task(
        client,
        world["key"],
        title="Invoice 7",
        workspaceId=world["ops"]["id"],
        assigneeId=person["id"],
        customFields=_invoice(7),
    )
    with sync_engine.connect() as conn:
        tenant = str(
            conn.execute(
                text("SELECT tenant_id FROM tasks WHERE id = :id"), {"id": mine["id"]}
            ).scalar_one()
        )
    configure_authorizer(Authorizer(_TasksIn({world["sales"]["id"]}, tenant), "policy"))
    ctx = AuthContext(
        tenant_id=uuid.UUID(tenant),
        principal_id=uuid.UUID(person["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )

    async def ask(**body: Any) -> dict[str, Any]:
        async with transaction(app.state.session_factory) as db:
            answer = await view_data.prepare(
                db, ctx, settings, "invoices", view_data.ViewQuery(**body), locale=None
            )
        assert answer.body is not None
        return answer.body

    page = await ask(block=I_TABLE, limit=50)
    titles = sorted(row["title"] for row in page["items"])
    assert titles == ["Invoice 1", "Invoice 2", "Invoice 3", "Invoice 4", "Invoice 7"]
    async with transaction(app.state.session_factory) as db:
        listed = await lists.list_tasks(db, ctx, type_key=TYPE, limit=50)
    assert sorted(task.title for task in listed.items) == titles
    metrics = await ask(block=I_METRICS)
    assert metrics["values"][0] == 5
    assert metrics["values"][4] == 70


async def test_aggregates_past_the_ceiling_are_refused(
    client: httpx.AsyncClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from control_plane.application.queries import view_records

    monkeypatch.setattr(view_records, "MAX_EVALUATED", 5)
    monkeypatch.setattr(view_records, "BATCH", 2)
    refused = await _query(client, world["reader"], "invoices", block=I_METRICS)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "view_too_costly"
    assert refused.json()["error"]["details"] == {"key": "invoices", "limit": 5}
    # A page reads only what it shows: no ceiling.
    assert (await _query(client, world["reader"], "invoices", block=I_TABLE)).status_code == 200


# --- a view of knowledge -------------------------------------------------------------------------


@pytest.fixture
async def memory(app: FastAPI) -> AsyncIterator[KnowledgeMemory]:
    fake = KnowledgeMemory()
    app.state.context_provider = fake
    try:
        yield fake
    finally:
        app.state.context_provider = None


async def test_a_page_of_records_with_their_attributes_and_relations(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    page = await _ok(
        client, world["knower"], "entities", block=K_TABLE, workspaceId=world["sales"]["id"]
    )
    assert_query_form("table", page)
    # By revenue, largest first, a missing one last; ties by kind and key.
    assert page["items"] == [
        {
            "id": "7701",
            "title": "Acme",
            "values": {
                "title": "Acme",
                "attributes.inn": "7701",
                "attributes.city": "Moscow",
                "relations.party_to": "Supply 1, Supply 2",
                "amount": None,
            },
        },
        {
            "id": "C-2",
            "title": "Supply 2",
            "values": {
                "title": "Supply 2",
                "attributes.inn": None,
                "attributes.city": None,
                "relations.party_to": "Acme, Birch",
                "amount": {"amount": "99.00", "currency": None},
            },
        },
    ]
    # Every request of the core to Memory is the pinned contract's (the fake validates it).
    assert memory.entities_requests[0]["kinds"] == ["legal_entity", "contract"]
    rest = await _ok(
        client,
        world["knower"],
        "entities",
        block=K_TABLE,
        workspaceId=world["sales"]["id"],
        cursor=page["nextCursor"],
        limit=10,
    )
    assert [row["id"] for row in rest["items"]] == ["7702", "C-1", "7703"]
    assert rest["nextCursor"] is None
    # The hidden record of another workspace's scope is in no answer, nor its relation.
    assert "Hidden" not in json.dumps([page, rest])


async def test_the_records_are_those_entities_query_answers(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    listed = await client.post(
        "/api/v1/knowledge/entities:query",
        json={"workspaceId": world["sales"]["id"], "kinds": ["legal_entity", "contract"]},
        headers=auth(world["knower"]),
    )
    assert listed.status_code == 200, listed.text
    page = await _ok(
        client,
        world["knower"],
        "entities",
        block=K_TABLE,
        workspaceId=world["sales"]["id"],
        limit=50,
    )
    assert sorted(r["id"] for r in page["items"]) == sorted(
        f"{e['key']}" for e in listed.json()["items"]
    )


@pytest.mark.parametrize(
    ("condition", "ids"),
    [
        ({"field": "kind", "op": "eq", "value": "contract"}, ["C-2", "C-1"]),
        ({"field": "attributes.city", "op": "prefix", "value": "MOS"}, ["7701", "7703"]),
        ({"field": "attributes.city", "op": "in", "value": ["Kazan", "Perm"]}, ["7702"]),
        ({"field": "validFrom", "op": "gte", "value": "2026-01-01"}, None),
        ({"field": "validFrom", "op": "lte", "value": "2025-12-31"}, []),
    ],
)
async def test_filters_of_records(
    client: httpx.AsyncClient,
    world: dict[str, Any],
    memory: KnowledgeMemory,
    condition: dict[str, Any],
    ids: list[str] | None,
) -> None:
    page = await _ok(
        client,
        world["knower"],
        "entities",
        block=K_TABLE,
        workspaceId=world["sales"]["id"],
        filter=[condition],
        limit=50,
    )
    found = [row["id"] for row in page["items"]]
    assert found == (ids if ids is not None else ["7701", "C-2", "7702", "C-1", "7703"])


async def test_aggregates_of_records(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    body = {"workspaceId": world["sales"]["id"]}
    metrics = await _ok(client, world["knower"], "entities", block=K_METRICS, **body)
    assert_query_form("metrics", metrics)
    assert metrics["values"] == [5, 600, 2]
    chart = await _ok(client, world["knower"], "entities", block=K_CHART, **body)
    assert_query_form("chart", chart)
    # In the order of the kinds of the source.
    assert chart["points"] == [
        {"label": "legal_entity", "value": 3},
        {"label": "contract", "value": 2},
    ]


async def test_what_a_block_of_knowledge_does_not_declare_is_422(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    body = {"workspaceId": world["sales"]["id"], "block": K_TABLE}
    for extra, code in [
        ({"filter": [{"field": "attributes.inn", "op": "eq", "value": "1"}]}, "undeclared_filter"),
        ({"filter": [{"field": "kind", "op": "prefix", "value": "c"}]}, "invalid_filter"),
        ({"sort": [{"field": "title"}]}, "undeclared_sort"),
        ({"cursor": "bm90LWEtY3Vyc29y"}, "invalid_cursor"),
    ]:
        refused = await _query(client, world["knower"], "entities", **body, **extra)
        assert refused.status_code == 422, (extra, refused.text)
        assert refused.json()["error"]["code"] == code, extra
    # Nothing was read from Memory for a request the core refuses.
    assert memory.entities_requests == []


async def test_the_tree_of_the_caller_and_its_rights(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    # Two trees and none named: which knowledge base is the caller's?
    several = await _query(client, world["knower"], "entities", block=K_TABLE)
    assert several.status_code == 422
    assert several.json()["error"]["code"] == "workspace_required"
    # A workspace that is not there: the 404 of entities:query.
    missing = await _query(
        client, world["knower"], "entities", block=K_TABLE, workspaceId=str(uuid.uuid4())
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    # Without events.read the view is not the caller's to see: the 404 of a missing view.
    hidden = await _query(
        client, world["reader"], "entities", block=K_TABLE, workspaceId=world["sales"]["id"]
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["message"] == "View not found"


async def test_one_tree_is_the_callers_without_naming_it(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _install(client, s["key"])
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE workspaces SET status = 'archived' WHERE parent_id IS NULL"))
    nothing = await _ok(client, s["key"], "entities", block=K_TABLE)
    assert nothing == {"items": [], "nextCursor": None}
    metrics = await _ok(client, s["key"], "entities", block=K_METRICS)
    assert metrics == {"values": [0, 0, 0]}
    only = await create_workspace(client, s["key"], "only")
    unavailable = await _query(client, s["key"], "entities", block=K_TABLE)
    assert unavailable.status_code == 503, unavailable.text
    assert only["id"]


async def test_memory_failing_and_too_many_records(
    client: httpx.AsyncClient,
    world: dict[str, Any],
    memory: KnowledgeMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.application.queries import view_records

    body = {"block": K_METRICS, "workspaceId": world["sales"]["id"]}
    monkeypatch.setattr(view_records, "KNOWLEDGE_PAGE", 2)
    monkeypatch.setattr(view_records, "MAX_KNOWLEDGE", 3)
    refused = await _query(client, world["knower"], "entities", **body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"] == {"key": "entities", "limit": 3}
    monkeypatch.setattr(view_records, "MAX_KNOWLEDGE", 50)
    paged = await _ok(client, world["knower"], "entities", **body)
    assert paged["values"][0] == 5
    # Memory failing: the answer entities:query gives.
    memory.fail = "entities"
    failed = await _query(client, world["knower"], "entities", **body)
    listed = await client.post(
        "/api/v1/knowledge/entities:query",
        json={"workspaceId": world["sales"]["id"], "kinds": ["legal_entity"]},
        headers=auth(world["knower"]),
    )
    assert failed.status_code == listed.status_code == 502, failed.text
    assert failed.json()["error"]["code"] == listed.json()["error"]["code"] == "memory_unavailable"


async def test_related_of_a_view_of_tasks_reads_the_key_from_the_params(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    related = await _ok(
        client,
        world["key"],
        "invoices",
        block=I_RELATED,
        params={"inn": "7701"},
        workspaceId=world["sales"]["id"],
    )
    assert_query_form("related", related)
    assert [(r["relation"], r["key"], r["direction"]) for r in related["items"]] == [
        ("party_to", "C-1", "out"),
        ("party_to", "C-2", "out"),
    ]
    none = await _ok(
        client, world["key"], "invoices", block=I_RELATED, workspaceId=world["sales"]["id"]
    )
    assert none == {"items": []}


# --- members mode: what a member of a subtree sees (CP-ADR-0082) -------------------------------


MEMBER_PERMISSIONS = ["tasks.read", "events.read", "workspaces.read"]


async def _members_tree(client: httpx.AsyncClient) -> dict[str, Any]:
    """Company -> Dept -> Team and an unrelated root Other, an invoice in each; Ann in Dept."""
    s = await _setup(client)
    await _install(client, s["key"])
    ws: dict[str, str] = {}
    for name, parent in (("company", None), ("dept", "company"), ("team", "dept"), ("other", None)):
        created = await create_workspace(
            client, s["key"], name, parent_id=ws[parent] if parent else None
        )
        ws[name] = created["id"]
    for n, name in enumerate(("company", "dept", "team", "other"), 1):
        await _task(
            client,
            s["key"],
            title=f"Invoice of {name}",
            workspaceId=ws[name],
            customFields=_invoice(n),
        )
    ann, s["ann"] = await create_agent_with_key(
        client, s["key"], name="ann", permissions=MEMBER_PERMISSIONS, kind="human"
    )
    member = await client.post(
        f"/api/v1/workspaces/{ws['dept']}/members",
        json={"principalId": ann["id"]},
        headers=auth(s["key"]),
    )
    assert member.status_code == 201, member.text
    s["identity"] = {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
    }
    bound = await client.post(
        f"/api/v1/principals/{ann['id']}/iam-bindings",
        json={**s["identity"], "permissions": MEMBER_PERMISSIONS, "visibility": "members"},
        headers=auth(s["key"]),
    )
    assert bound.status_code in (200, 201), bound.text
    s["ws"] = ws
    s["ann_id"] = ann["id"]
    return s


async def test_members_a_view_of_tasks_shows_the_visible_subtree_only(
    client: httpx.AsyncClient,
) -> None:
    s = await _members_tree(client)
    page = await _ok(client, s["ann"], "invoices", block=I_TABLE, limit=50)
    assert sorted(row["title"] for row in page["items"]) == ["Invoice of dept", "Invoice of team"]
    metrics = await _ok(client, s["ann"], "invoices", block=I_METRICS)
    assert metrics["values"][0] == 2
    # What GET /tasks lists to her, and nothing else.
    listed = await client.get(
        "/api/v1/tasks", params={"typeKey": TYPE, "limit": 50}, headers=auth(s["ann"])
    )
    assert listed.status_code == 200, listed.text
    assert sorted(t["title"] for t in listed.json()["items"]) == sorted(
        row["title"] for row in page["items"]
    )


async def test_members_the_knowledge_of_the_tree_without_naming_a_workspace(
    client: httpx.AsyncClient, memory: KnowledgeMemory
) -> None:
    """A member of Dept, not of the root Company: the knowledge base of Company's tree."""
    s = await _members_tree(client)
    page = await _ok(client, s["ann"], "entities", block=K_TABLE, limit=50)
    assert [row["id"] for row in page["items"]] == ["7701", "C-2", "7702", "C-1", "7703"]
    [request] = memory.entities_requests
    assert len(request["namespaces"]) == 1
    assert request["namespaces"][0].endswith(f":ws:{s['ws']['company']}")
    assert f"workspace:{s['ws']['other']}" not in request["allowedScopes"]
    # The same records entities:query gives her for a workspace of the tree.
    listed = await client.post(
        "/api/v1/knowledge/entities:query",
        json={"workspaceId": s["ws"]["team"], "kinds": ["legal_entity", "contract"]},
        headers=auth(s["ann"]),
    )
    assert listed.status_code == 200, listed.text
    assert sorted(e["key"] for e in listed.json()["items"]) == sorted(
        row["id"] for row in page["items"]
    )
    metrics = await _ok(client, s["ann"], "entities", block=K_METRICS)
    assert metrics["values"] == [5, 600, 2]
    # ``related`` of a view of tasks reads the same tree, not an empty block.
    related = await _ok(client, s["ann"], "invoices", block=I_RELATED, params={"inn": "7701"})
    assert [(r["relation"], r["key"]) for r in related["items"]] == [
        ("party_to", "C-1"),
        ("party_to", "C-2"),
    ]


async def test_members_a_workspace_not_visible_is_a_missing_one(
    client: httpx.AsyncClient, memory: KnowledgeMemory
) -> None:
    s = await _members_tree(client)
    missing = str(uuid.uuid4())
    for block in (K_TABLE, K_METRICS):
        asked = [
            await _query(client, s["ann"], "entities", block=block, workspaceId=ws)
            for ws in (s["ws"]["other"], missing)
        ]
        same_as_missing(asked[0], asked[1], (s["ws"]["other"], missing))
    # The root of her own tree is not hers to name either: she is a member of Dept.
    root = await _query(client, s["ann"], "entities", block=K_TABLE, workspaceId=s["ws"]["company"])
    same_as_missing(
        root,
        await _query(client, s["ann"], "entities", block=K_TABLE, workspaceId=missing),
        (s["ws"]["company"], missing),
    )
    assert memory.entities_requests == []


async def test_members_in_two_trees_must_name_the_workspace(
    client: httpx.AsyncClient, memory: KnowledgeMemory
) -> None:
    s = await _members_tree(client)
    added = await client.post(
        f"/api/v1/workspaces/{s['ws']['other']}/members",
        json={"principalId": s["ann_id"]},
        headers=auth(s["key"]),
    )
    assert added.status_code == 201, added.text
    several = await _query(client, s["ann"], "entities", block=K_TABLE)
    assert several.status_code == 422, several.text
    assert several.json()["error"]["code"] == "workspace_required"
    named = await _ok(client, s["ann"], "entities", block=K_TABLE, workspaceId=s["ws"]["dept"])
    assert named["items"]


@dataclass
class _NoWorkspaceKnowledge:
    """A PDP granting everything but ``events.read`` on a workspace: no knowledge of a tree."""

    async def check(  # type: ignore[no-untyped-def]
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):
        allowed = not (action == "events.read" and resource.key.startswith("workspace:"))
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
        return ObjectPage(objects=[], cursor=None, model_version="1")


async def test_members_without_the_right_to_read_the_knowledge_is_403(
    client: httpx.AsyncClient,
    app: FastAPI,
    memory: KnowledgeMemory,
    restore_authorizer: None,
) -> None:
    """The view is hers (``events.read`` on the tenant), the tree's knowledge is not: the
    403 entities:query answers, and an empty ``related`` block."""
    s = await _members_tree(client)
    signing_key = SigningKey.generate()
    enable_iam(app, signing_key)
    token = signing_key.issue(
        subject=uuid.UUID(s["identity"]["iamPrincipalId"]),
        tenant_id=uuid.UUID(s["identity"]["iamTenantId"]),
        scopes=[SCOPE_READ, SCOPE_WRITE],
        ttl_seconds=3600,
    )
    configure_authorizer(Authorizer(_NoWorkspaceKnowledge(), "policy"))
    refused = await _query(client, token, "entities", block=K_TABLE)
    listed = await client.post(
        "/api/v1/knowledge/entities:query",
        json={"workspaceId": s["ws"]["dept"], "kinds": ["legal_entity", "contract"]},
        headers=auth(token),
    )
    assert refused.status_code == listed.status_code == 403, (refused.text, listed.text)
    assert refused.json()["error"]["code"] == listed.json()["error"]["code"]
    related = await _ok(client, token, "invoices", block=I_RELATED, params={"inn": "7701"})
    assert related == {"items": []}
    assert memory.entities_requests == []


# --- the check of the paths when the package is published --------------------------------------


def _warnings(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """The warnings of a plan but the dictionary keys no view shows."""
    return [
        (p["code"], p["path"])
        for p in plan["problems"]
        if p["severity"] == "warning" and p["code"] != "unused_message"
    ]


async def test_the_plan_refuses_paths_the_task_type_does_not_declare(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    wrong = {
        **INVOICES,
        "layout": [
            {
                "block": "table",
                "columns": [{"field": "customFields.nothing"}, {"field": "fields.bogus"}],
            }
        ],
    }
    plan = await _plan(client, s["key"], _package({"invoices": wrong}))
    found = [
        (p["code"], p["path"], p["file"]) for p in plan["problems"] if p["severity"] == "error"
    ]
    assert ("undeclared_path", "/spec/layout/0/columns/0/field", "views/invoices.yaml") in found
    assert ("undeclared_path", "/spec/layout/0/columns/1/field", "views/invoices.yaml") in found
    assert not any(c["kind"] == "View" for c in plan["changes"])


async def test_the_plan_warns_of_what_the_ontology_of_the_tree_lacks(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    s = await _setup(client)
    workspace = await create_workspace(client, s["key"], "kb")
    lacking = {
        **ENTITIES,
        "source": {"knowledge": {"kinds": ["legal_entity", "planet"]}},
        "layout": [
            {
                "block": "table",
                "columns": [{"field": "attributes.inn"}, {"field": "attributes.orbit"}],
            }
        ],
    }
    texts = {"ledger.fields.attributes.orbit": "Orbit"}
    package = _package({"entities": lacking})
    package["files"][2]["content"] = yaml.safe_dump({**TEXTS, **texts})
    unchecked = await _plan(client, s["key"], package)
    assert _errors(unchecked) == []
    assert _warnings(unchecked) == [("knowledge_unchecked", "")]
    memory = KnowledgeMemory()
    app.state.context_provider = memory
    try:
        plan = await _plan(client, s["key"], package, workspaceId=workspace["id"])
    finally:
        app.state.context_provider = None
    assert _warnings(plan) == [
        ("unknown_kind", "/spec/source/knowledge/kinds/1"),
        ("undeclared_path", "/spec/layout/0/columns/1/field"),
    ]
    assert memory.package_requests == [("deals-kb", "1")]
    # A warning does not keep the view out of the plan: it is installed.
    applied = await _apply(client, s["key"], package, plan["planHash"], workspaceId=workspace["id"])
    assert applied.status_code == 200, applied.text


async def test_the_same_queries_at_once_answer_as_one_after_another(
    client: httpx.AsyncClient, world: dict[str, Any], memory: KnowledgeMemory
) -> None:
    """Read-only, so parallel and repeated queries answer the same; nothing is written."""
    where = {"workspaceId": world["sales"]["id"]}
    asks = [
        ("invoices", {"block": I_TABLE}),
        ("invoices", {"block": I_METRICS}),
        ("entities", {"block": K_TABLE, **where}),
        ("entities", {"block": K_METRICS, **where}),
    ]
    one_by_one = [await _ok(client, world["key"], view, **body) for view, body in asks]
    at_once = await asyncio.gather(
        *(_ok(client, world["key"], view, **body) for view, body in asks * 2)
    )
    assert list(at_once) == one_by_one * 2
