"""Races of ``POST /principals/{id}/iam-bindings`` (ADR-0053; CP-ADR-0073, I4).

Two upserts of one new identity both find no row to lock and meet on
``uq_iam_bindings_identity``: the loser inserts under a SAVEPOINT and answers
409 ``iam_identity_bound_elsewhere`` (another principal) or updates the
winner's row (the same one), never a 500. An upsert on a registry agent's
principal is refused before it touches a binding, so it cannot reopen the
binding a concurrent ``identity:replace`` revokes (TASK-001127).
"""

import asyncio
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.concurrency.test_agent_identity_replace_races import (
    ISSUER,
    _identity_being_bound,
    _linked_service,
)
from tests.concurrency.test_principal_disable_races import _wait_until_blocked_by
from tests.helpers import auth, do_bootstrap


async def _principal(client: httpx.AsyncClient, admin_key: str, name: str) -> str:
    created = await client.post(
        "/api/v1/principals",
        json={"kind": "service", "displayName": name},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    principal_id: str = created.json()["id"]
    return principal_id


def _body(identity: str) -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": identity,
        "permissions": ["tasks.read"],
    }


def _blocked_by(sync_engine: Engine, holder_pid: int) -> bool:
    with sync_engine.connect() as conn:
        waiting: int = conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ).scalar_one()
    return waiting > 0


def _bindings_of(sync_engine: Engine, identity: str) -> list[tuple[str, str]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT principal_id, status FROM iam_principal_bindings "
                "WHERE iam_principal_id = :i"
            ),
            {"i": identity},
        ).all()
    return [(str(r[0]), r[1]) for r in rows]


async def test_upsert_losing_the_unique_index_to_another_principal_is_a_conflict(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The lookup finds nothing, the insert waits on the winner and hits the index."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    winner = await _principal(client, admin_key, "winner")
    loser = await _principal(client, admin_key, "loser")
    contested = str(uuid.uuid4())

    with _identity_being_bound(sync_engine, winner, contested) as (holder, conn):
        request = asyncio.create_task(
            client.post(
                f"/api/v1/principals/{loser}/iam-bindings",
                json=_body(contested),
                headers=auth(admin_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await asyncio.wait_for(request, 30)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "iam_identity_bound_elsewhere"
    assert _bindings_of(sync_engine, contested) == [(winner, "active")]
    with sync_engine.connect() as conn:
        created = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'iam_binding.created'")
        ).scalar_one()
    assert created == 0  # the winner was inserted beside the API


async def test_upsert_losing_the_unique_index_on_the_same_principal_updates_the_winner(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The same identity on the same principal is what a moment later finds: an update."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principal = await _principal(client, admin_key, "service")
    contested = str(uuid.uuid4())

    with _identity_being_bound(sync_engine, principal, contested) as (holder, conn):
        request = asyncio.create_task(
            client.post(
                f"/api/v1/principals/{principal}/iam-bindings",
                json={**_body(contested), "permissions": ["events.read", "tasks.read"]},
                headers=auth(admin_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await asyncio.wait_for(request, 30)

    assert response.status_code == 200, response.text
    assert response.json()["permissions"] == ["events.read", "tasks.read"]
    assert _bindings_of(sync_engine, contested) == [(principal, "active")]
    with sync_engine.connect() as conn:
        events = conn.execute(
            text("SELECT event_type FROM events WHERE event_type LIKE 'iam_binding.%'")
        ).all()
    assert [e[0] for e in events] == ["iam_binding.updated"]


async def test_parallel_upserts_of_one_identity_onto_two_principals(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principals = [await _principal(client, admin_key, name) for name in ("a", "b")]
    contested = str(uuid.uuid4())

    responses = await asyncio.wait_for(
        asyncio.gather(
            *(
                client.post(
                    f"/api/v1/principals/{p}/iam-bindings",
                    json=_body(contested),
                    headers=auth(admin_key),
                )
                for p in principals
            )
        ),
        30,
    )
    codes = sorted(r.status_code for r in responses)
    # Serialized by the row lock, the second one moves the identity (admin may);
    # interleaved, it loses the insert and gets the 409 — never a 500.
    assert codes in ([200, 201], [201, 409]), [r.text for r in responses]
    if codes == [201, 409]:
        loser = next(r for r in responses if r.status_code == 409)
        assert loser.json()["error"]["code"] == "iam_identity_bound_elsewhere"
    assert len(_bindings_of(sync_engine, contested)) == 1


async def test_upsert_does_not_reopen_what_a_concurrent_replace_revokes(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The race of TASK-001120's review: the upsert read the agent unlocked.

    A replacement holds the service's binding and is about to revoke it. Before,
    an admin upsert of that identity passed the registry check, waited on the
    row and reopened it after the replacement committed: two active bindings.
    Now it is refused without waiting on the row.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principal = await _linked_service(client, admin_key, "notifier")
    with sync_engine.connect() as conn:
        old = str(
            conn.execute(
                text("SELECT iam_principal_id FROM agents WHERE key = 'notifier'")
            ).scalar_one()
        )
    new = str(uuid.uuid4())

    with sync_engine.connect() as conn:
        # The replacement's locks: the agent row, then the principal's bindings.
        conn.execute(text("SELECT 1 FROM agents WHERE key = 'notifier' FOR UPDATE"))
        conn.execute(
            text("SELECT 1 FROM iam_principal_bindings WHERE principal_id = :p FOR UPDATE"),
            {"p": principal},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        request = asyncio.create_task(
            client.post(
                f"/api/v1/principals/{principal}/iam-bindings",
                json={**_body(old), "permissions": ["principals.write", "tasks.read"]},
                headers=auth(admin_key),
            )
        )
        for _ in range(100):
            if request.done() or _blocked_by(sync_engine, holder):
                break
            await asyncio.sleep(0.05)
        # What the replacement does before it commits.
        conn.execute(
            text(
                "UPDATE iam_principal_bindings SET status = 'revoked', revoked_at = now() "
                "WHERE principal_id = :p"
            ),
            {"p": principal},
        )
        conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, id, :issuer, gen_random_uuid(), :i, "
                "'[\"tasks.read\"]', 'active', now(), now() FROM principals WHERE id = :p"
            ),
            {"issuer": ISSUER, "i": new, "p": principal},
        )
        conn.execute(
            text("UPDATE agents SET iam_principal_id = :i WHERE key = 'notifier'"), {"i": new}
        )
        conn.commit()
    response = await asyncio.wait_for(request, 30)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "agent_identity_conflict"
    with sync_engine.connect() as conn:
        active = conn.execute(
            text(
                "SELECT iam_principal_id FROM iam_principal_bindings "
                "WHERE principal_id = :p AND status = 'active'"
            ),
            {"p": principal},
        ).all()
    assert [str(r[0]) for r in active] == [new]


async def test_a_concurrent_upsert_of_rights_alone_never_resets_the_visibility(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """CP-ADR-0082 §2.2: an upsert without ``visibility`` keeps the mode it
    finds under the row lock, whichever of two concurrent upserts goes first."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/principals", json={"kind": "human", "displayName": "Ann"}, headers=auth(admin_key)
    )
    human = created.json()["id"]
    identity = str(uuid.uuid4())
    body = _body(identity)
    first = await client.post(
        f"/api/v1/principals/{human}/iam-bindings", json=body, headers=auth(admin_key)
    )
    assert first.status_code == 201, first.text

    for _ in range(5):
        narrowed, rights = await asyncio.gather(
            client.post(
                f"/api/v1/principals/{human}/iam-bindings",
                json={**body, "visibility": "members"},
                headers=auth(admin_key),
            ),
            client.post(
                f"/api/v1/principals/{human}/iam-bindings",
                json={**body, "permissions": ["tasks.read", "tasks.write"]},
                headers=auth(admin_key),
            ),
        )
        assert (narrowed.status_code, rights.status_code) == (200, 200)
        with sync_engine.connect() as conn:
            stored = conn.execute(
                text("SELECT visibility FROM iam_principal_bindings WHERE iam_principal_id = :i"),
                {"i": identity},
            ).scalar_one()
        assert stored == "members"
