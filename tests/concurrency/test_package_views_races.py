"""Two applies of the same package with views at once (CP-ADR-0080).

Views are written only by ``POST /packages:apply`` under the tenant's apply
lock: of two applies of one plan, one publishes each view once and the other,
planned again behind the lock, finds the catalog moved — ``409 plan_stale`` —
and writes nothing: one revision and one ``view.published`` per view.
"""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_views import _package, _world
from tests.integration.test_process_instances import _events


async def test_two_applies_of_one_plan_publish_each_view_once(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _world(client)
    package = _package(s["admin"])
    plan = await _plan(client, s["key"], package)
    assert _errors(plan) == [], plan["problems"]
    results = await asyncio.gather(
        *(_apply(client, s["key"], package, plan["planHash"]) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    refused = next(r for r in results if r.status_code == 409)
    assert refused.json()["error"]["code"] == "plan_stale"
    with sync_engine.connect() as conn:
        revisions = conn.execute(
            text("SELECT v.key, r.revision FROM views v JOIN view_revisions r ON r.view_id = v.id")
        ).all()
    assert sorted(revisions) == [("sample-card", 1), ("sample-list", 1)]
    published = await _events(client, s["key"], "view.published")
    assert sorted(e["payload"]["key"] for e in published) == ["sample-card", "sample-list"]
