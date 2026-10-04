"""Migration matrix over populated data: v0.3 <-> v0.4 <-> v0.5.

Runs against the test database using the real Alembic scripts. The event
journal is append-only history: no migration may rewrite or lose a row in
either direction, and the v0.5 cursor conversion must be replay-safe (a tenant
must not be rewound to the origin by an upgrade).

Every test restores the head revision in teardown — a half-migrated database
would break every later test in the session, not just this module.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)

V03_HEAD = "b3d47a1c9e05"
V04_HEAD = "2cb05920015d"
V05_HEAD = "1adf50721f1e"
V06_HEAD = "72ef8bc31a06"
CURRENT_HEAD = "b7e3d9a1c4f2"


@pytest.fixture
def alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    # Never leave the schema behind an earlier revision.
    alembic_command.upgrade(config, "head")


def _table_exists(sync_engine: Engine, name: str) -> bool:
    with sync_engine.connect() as conn:
        return bool(conn.execute(text(f"SELECT to_regclass('{name}')")).scalar())


def _journal(sync_engine: Engine) -> list[Any]:
    with sync_engine.connect() as conn:
        return conn.execute(
            text("SELECT id, sequence, tx_id, event_type FROM events ORDER BY sequence")
        ).all()


def _counts(sync_engine: Engine, tables: tuple[str, ...]) -> dict[str, Any]:
    with sync_engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}


async def _populate(client: httpx.AsyncClient) -> dict[str, Any]:
    """A realistic v0.5 tenant: workspace type, template, project, run, events."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    await client.post(
        "/api/v1/workspace-types",
        json={"key": "portfolio", "displayName": "Portfolio", "allowedChildTypes": ["*"]},
        headers=auth(admin_key),
    )
    workspace = await create_workspace(client, admin_key, "acme-portfolio")
    template = (
        await client.post(
            "/api/v1/project-templates",
            json={"key": "delivery", "displayName": "Delivery"},
            headers=auth(admin_key),
        )
    ).json()
    project = (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": workspace["id"], "templateId": template["id"]},
            headers=auth(admin_key),
        )
    ).json()
    await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions",
        json={"config": {"settings": {"tone": "formal"}}},
        headers=auth(admin_key),
    )
    await client.post(
        f"/api/v1/projects/{project['id']}/external-references",
        json={"externalSystem": "legacy", "externalType": "board", "externalId": "B-1"},
        headers=auth(admin_key),
    )

    task = await create_task(
        client, admin_key, title="Survives migration", workspaceId=workspace["id"]
    )
    session = await open_session(
        client,
        agent_key,
        harness={"type": "cli", "protocolVersion": "2", "capabilities": ["resume"]},
    )
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()
    await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "working_state", "data": {"step": 1}},
        headers=auth(agent_key),
    )
    await client.post(
        "/api/v1/observations",
        json={"kind": "finding", "content": "Migration test finding"},
        headers=auth(agent_key),
    )
    return {
        "adminKey": admin_key,
        "agentKey": agent_key,
        "tenantId": boot["tenant"]["id"],
        "workspaceId": workspace["id"],
        "projectId": project["id"],
        "taskId": task["id"],
    }


_CORE_TABLES = (
    "tenants",
    "sessions",
    "tasks",
    "task_claims",
    "runs",
    "run_checkpoints",
    "outbox",
)


async def test_v05_roundtrip_preserves_history(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    """v0.5 -> v0.4 -> v0.5 with data: nothing lost, nothing rewritten."""
    context = await _populate(client)
    before_events = _journal(sync_engine)
    before_counts = _counts(sync_engine, _CORE_TABLES)
    assert _table_exists(sync_engine, "project_profiles")

    alembic_command.downgrade(alembic_config, V04_HEAD)
    assert _journal(sync_engine) == before_events, "downgrade must not touch the journal"
    assert _counts(sync_engine, _CORE_TABLES) == before_counts
    for table in ("project_profiles", "project_templates", "workspace_types", "event_archive"):
        assert not _table_exists(sync_engine, table)
    with sync_engine.connect() as conn:
        # Per-tenant cursors fold back into exactly one global row.
        rows = conn.execute(text("SELECT name, tx_id, sequence FROM event_consumer_cursors")).all()
        assert len(rows) <= 1

    alembic_command.upgrade(alembic_config, V05_HEAD)
    assert _journal(sync_engine) == before_events, "re-upgrade must not touch the journal"
    assert _counts(sync_engine, _CORE_TABLES) == before_counts
    with sync_engine.connect() as conn:
        # Workspaces are backfilled onto the tenant's system type, so the tree
        # is valid again without any manual repair.
        untyped = conn.execute(
            text("SELECT count(*) FROM workspaces WHERE type_id IS NULL")
        ).scalar()
        assert untyped == 0
        system_types = conn.execute(
            text("SELECT count(*) FROM workspace_types WHERE is_system")
        ).scalar()
        assert system_types == 1

    # The journal still replays completely through the new order — under the
    # schema the running code expects (later revisions add journal columns).
    alembic_command.upgrade(alembic_config, CURRENT_HEAD)
    replayed = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(context["agentKey"]))
    ).json()["items"]
    assert {e["id"] for e in replayed} == {str(row[0]) for row in before_events}


async def test_v05_cursor_conversion_is_replay_safe(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    """A confirmed global position becomes the SAME per-tenant position."""
    await _populate(client)
    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()

    alembic_command.downgrade(alembic_config, V04_HEAD)
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM event_consumer_cursors"))
        conn.execute(
            text(
                "INSERT INTO event_consumer_cursors (name, tx_id, sequence, updated_at, metadata)"
                " VALUES ('context-adapter', :tx, :seq, now(), '{}')"
            ),
            {"tx": latest[0], "seq": latest[1]},
        )

    alembic_command.upgrade(alembic_config, V05_HEAD)
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, tx_id, sequence FROM event_consumer_cursors"
                " WHERE name = 'context-adapter'"
            )
        ).all()
    assert rows, "every existing tenant must get a cursor row"
    for _, tx_id, sequence in rows:
        # Not rewound to the origin: the whole journal is NOT re-delivered.
        assert (tx_id, sequence) == (latest[0], latest[1])


async def test_v03_roundtrip_still_works(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    """The v0.4 migration keeps its own guarantees under the v0.5 chain."""
    await _populate(client)
    before = _journal(sync_engine)

    alembic_command.downgrade(alembic_config, V03_HEAD)
    assert _journal(sync_engine) == before
    assert not _table_exists(sync_engine, "event_consumer_cursors")

    alembic_command.upgrade(alembic_config, "head")
    assert _journal(sync_engine) == before
    with sync_engine.connect() as conn:
        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'events'")
            )
        }
    assert {"ix_events_tenant_tx_sequence", "ix_events_tx_sequence"} <= indexes


async def test_head_matches_code(sync_engine: Engine) -> None:
    with sync_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == CURRENT_HEAD


@pytest.mark.raw_journal
async def test_legacy_events_replayable_without_backfill(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Rows written before v0.5 (no trace_run_id) replay unchanged."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id, "
                "correlation_id, request_id, payload, occurred_at) "
                "VALUES (:id, :t, 'legacy.event', 'test', :e, 'c', 'r', '{}', now())"
            ),
            {"id": str(uuid.uuid4()), "t": tenant_id, "e": str(uuid.uuid4())},
        )
    events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()["items"]
    legacy = [e for e in events if e["type"] == "legacy.event"]
    assert legacy and legacy[0]["traceRunId"] is None
