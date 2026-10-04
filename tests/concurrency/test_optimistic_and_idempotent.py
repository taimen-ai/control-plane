import asyncio
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap


async def test_concurrent_updates_only_one_wins(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    async def update(title: str) -> httpx.Response:
        return await client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"title": title},
            headers={**auth(admin_key), "If-Match": '"task-1"'},
        )

    responses = await asyncio.gather(*[update(f"Writer {i}") for i in range(5)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4
    for response in responses:
        if response.status_code == 409:
            error = response.json()["error"]
            assert error["code"] == "version_conflict"
            assert error["details"]["currentVersion"] == 2

    current = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert current.json()["version"] == 2


async def test_parallel_identical_idempotent_requests(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())

    async def create() -> httpx.Response:
        return await client.post(
            "/api/v1/tasks",
            json={"title": "Exactly once"},
            headers={**auth(admin_key), "Idempotency-Key": key},
        )

    responses = await asyncio.gather(*[create() for _ in range(2)])
    assert [r.status_code for r in responses] == [201, 201]
    bodies = [r.json() for r in responses]
    assert bodies[0] == bodies[1]  # one logical result

    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() == 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE event_type = 'task.created'")
            ).scalar()
            == 1
        )


async def test_parallel_external_observations_dedup_to_one(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Concurrent reports of one (source, dedupKey) write exactly one event:
    the loser waits on the winner's key row and answers 200 with its id."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    async def report() -> httpx.Response:
        return await client.post(
            "/api/v1/observations",
            json={
                "kind": "external_fact",
                "content": "Alert fired",
                "source": "alertmanager",
                "dedupKey": "alert-17",
            },
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[report() for _ in range(4)])
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1

    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE event_type = 'observation.recorded'")
            ).scalar()
            == 1
        )


async def test_parallel_reports_of_one_key_by_two_actors(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Two authors racing for one (source, dedupKey): each records its own
    event, the author is part of the key (CP-ADR-0057, 2026-10-01)."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, first = await create_agent_with_key(client, admin_key, name="first")
    _, second = await create_agent_with_key(client, admin_key, name="second")

    async def report(key: str) -> httpx.Response:
        return await client.post(
            "/api/v1/observations",
            json={
                "kind": "external_fact",
                "content": "Alert fired",
                "source": "alertmanager",
                "dedupKey": "alert-18",
            },
            headers=auth(key),
        )

    responses = await asyncio.gather(report(first), report(second), report(first), report(second))
    assert sorted(r.status_code for r in responses) == [200, 200, 201, 201]
    assert responses[0].json()["id"] == responses[2].json()["id"]
    assert responses[1].json()["id"] == responses[3].json()["id"]
    assert responses[0].json()["id"] != responses[1].json()["id"]
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE event_type = 'observation.recorded'")
            ).scalar()
            == 2
        )
