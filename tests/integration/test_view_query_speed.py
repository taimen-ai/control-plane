"""How fast the data under a view is: p95 < 150 ms a page of 50 of 10⁴ instances (TAI-ADR-0066 p.4).

10 000 instances of the process of ``deals`` in one tenant, written in one
statement; each query below is asked :data:`ROUNDS` times through the API
(the ASGI app in process, the test database) after one warm-up, and its p95
is printed and held to :data:`BUDGET_MS`. The queries are the pages a console
opens: the package's own order, a sort and filters of the request (an
equality the GIN index of ``data`` answers, a range, a prefix), a page
further by its cursor, and — not bound by the budget of a page, printed for
the report — the aggregates of ``metrics`` over all of them, a board, and the
same page and aggregates of a view whose source filter SQL does not say
(evaluated record by record).

``make test PYTEST_ARGS="tests/integration/test_view_query_speed.py -s"``
prints the table.
"""

import statistics
import time
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key
from tests.integration.test_process_instances import _setup
from tests.integration.test_view_query import B_BOARD, B_METRICS, B_TABLE, _install, _template

INSTANCES = 10_000
ROUNDS = 30
BUDGET_MS = 150.0

_MANY = text(
    """
    INSERT INTO process_instances (
        id, tenant_id, workspace_id, definition_id, definition_key, definition_version,
        instance_key, status, outcome, error, data, state, refs, step_attempts,
        sla_due_at, sla_warn_at, parent_instance_id, parent_activity_id, started_by,
        started_at, updated_at, completed_at)
    SELECT gen_random_uuid(), t.tenant_id, NULL, t.definition_id, t.definition_key,
        t.definition_version, 'deal-' || n, 'running', NULL, NULL,
        jsonb_build_object(
            'title', 'Deal ' || n,
            'customer', 'cust-' || (n % 500),
            'kind', CASE WHEN n % 2 = 0 THEN 'works' ELSE 'goods' END,
            'urgent', n % 7 = 0,
            'amount', n % 1000,
            'price', jsonb_build_object('amount', (n * 10) || '.50', 'currency', 'RUB'),
            'deadline', to_char(DATE '2026-11-01' + (n % 60), 'YYYY-MM-DD'),
            'secret', 'do-not-leak'),
        jsonb_set(t.state, '{stages}', CASE WHEN n % 3 = 0
            THEN '{"work": {"state": "completed"}, "archive": {"state": "active"}}'::jsonb
            ELSE '{"work": {"state": "active"}, "archive": {"state": "available"}}'::jsonb END),
        '{}'::jsonb, t.step_attempts, NULL, NULL, NULL, NULL, t.started_by,
        TIMESTAMPTZ '2026-01-01 00:00:00+00' + n * INTERVAL '1 minute',
        TIMESTAMPTZ '2026-01-01 00:00:00+00' + n * INTERVAL '1 minute', NULL
      FROM process_instances t, generate_series(1, :count) AS n
     WHERE t.id = :template
    """
)


async def _timed(
    client: httpx.AsyncClient,
    key: str,
    body: dict[str, Any],
    view: str = "deals",
    rounds: int = ROUNDS,
) -> list[float]:
    times = []
    for round_ in range(rounds + 1):
        started = time.perf_counter()
        response = await client.post(f"/api/v1/views/{view}:query", json=body, headers=auth(key))
        elapsed = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, response.text
        if round_:  # the first one warms the caches up
            times.append(elapsed)
    return times


def _p95(times: list[float]) -> float:
    return statistics.quantiles(times, n=20, method="inclusive")[18]


async def test_a_page_of_50_of_10000_instances_is_under_150_ms_at_p95(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _install(client, s["key"], s["admin"])
    template = await _template(client, s["key"])
    with sync_engine.begin() as conn:
        conn.execute(_MANY, {"count": INSTANCES, "template": template})
        conn.execute(text("ANALYZE process_instances"))
    _, reader = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["processes.read"], kind="human"
    )
    first = await client.post(
        "/api/v1/views/deals:query", json={"block": B_TABLE, "limit": 50}, headers=auth(reader)
    )
    pages: dict[str, dict[str, Any]] = {
        "table, its own order": {"block": B_TABLE, "limit": 50},
        "table, sort by customer": {
            "block": B_TABLE,
            "limit": 50,
            "sort": [{"field": "customer", "dir": "asc"}, {"field": "amount", "dir": "desc"}],
        },
        "table, kind eq + amount range": {
            "block": B_TABLE,
            "limit": 50,
            "filter": [
                {"field": "kind", "op": "eq", "value": "goods"},
                {"field": "amount", "op": "gte", "value": 100},
                {"field": "amount", "op": "lte", "value": 900},
            ],
        },
        "table, customer prefix": {
            "block": B_TABLE,
            "limit": 50,
            "filter": [{"field": "customer", "op": "prefix", "value": "cust-1"}],
        },
        "table, second page": {
            "block": B_TABLE,
            "limit": 50,
            "cursor": first.json()["nextCursor"],
        },
    }
    report = {
        "metrics, 5 aggregates": {"block": B_METRICS},
        "board, 50 a column": {"block": B_BOARD},
    }
    results = {}
    for name, body in {**pages, **report}.items():
        times = await _timed(client, reader, body)
        results[name] = (_p95(times), statistics.median(times))
    # The source filter evaluated record by record (no SQL for it): what that costs.
    evaluated = {
        "evaluated filter: table page": {"block": B_TABLE, "limit": 50},
        "evaluated filter: metrics": {"block": B_METRICS},
    }
    for name, body in evaluated.items():
        times = await _timed(client, reader, body, view="deals-evaluated", rounds=5)
        results[name] = (_p95(times), statistics.median(times))
    print(f"\n{INSTANCES} instances, {ROUNDS} rounds each: p95 / median, ms")
    for name, (p95, median) in results.items():
        print(f"  {name:32} {p95:7.1f} / {median:7.1f}")
    for name in pages:
        assert results[name][0] < BUDGET_MS, (name, results[name])
