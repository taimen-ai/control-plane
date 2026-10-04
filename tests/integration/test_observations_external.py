"""External observations (CP-ADR-0057): source, dedupKey, observedAt,
supersedes, externalRef — and dedup of repeats per tenant and author."""

import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, do_bootstrap, make_tenant_directly


async def _agent_key(client: httpx.AsyncClient) -> str:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    return agent_key


async def _post(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/v1/observations", json=body, headers=auth(key))


async def _recorded(client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    events = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e for e in events if e["type"] == "observation.recorded"]


def _external(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "external_fact",
        "content": "Issue 42 was closed",
        "source": "github",
        "dedupKey": "monthu56/repo#42@closed",
        "observedAt": "2026-09-20T08:15:00+00:00",
        "externalRef": {
            "system": "github",
            "id": "monthu56/repo#42",
            "url": "https://example.test/monthu56/repo/issues/42",
        },
    }
    body.update(overrides)
    return body


async def test_repeat_with_same_source_and_dedup_key_returns_existing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _agent_key(client)

    first = await _post(client, key, _external())
    assert first.status_code == 201, first.text
    assert first.json()["deduplicated"] is False

    # A repeat — even with different content — is the same observation.
    repeat = await _post(client, key, _external(content="Issue 42 is closed (re-polled)"))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["deduplicated"] is True
    for field in ("id", "eventId", "kind", "recordedAt"):
        assert repeat.json()[field] == first.json()[field]

    assert len(await _recorded(client, key)) == 1
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM observation_dedup_keys")).scalar() == 1


async def test_dedup_identity_is_source_key_and_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _agent_key(client)
    first = await _post(client, key, _external())
    other_source = await _post(client, key, _external(source="gitlab"))
    other_key = await _post(client, key, _external(dedupKey="monthu56/repo#42@reopened"))
    assert [r.status_code for r in (first, other_source, other_key)] == [201, 201, 201]
    assert len({r.json()["id"] for r in (first, other_source, other_key)}) == 3

    # The same pair in ANOTHER tenant is an unrelated observation.
    _tenant, foreign_key = make_tenant_directly(sync_engine, "other")
    foreign = await _post(client, foreign_key, _external())
    assert foreign.status_code == 201
    assert foreign.json()["id"] != first.json()["id"]


async def _two_agent_keys(client: httpx.AsyncClient) -> tuple[str, str]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, first = await create_agent_with_key(client, admin_key, name="observer")
    _, second = await create_agent_with_key(client, admin_key, name="squatter")
    return first, second


async def test_a_key_taken_by_another_author_does_not_silence_the_observer(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """CP-ADR-0057, amendment 2026-10-01: the author is part of the key.

    A predictable key taken in advance is the taker's own observation; the
    real observer still records its fact, and each author dedups its own
    repeats only.
    """
    observer, squatter = await _two_agent_keys(client)
    taken = await _post(client, squatter, _external(content="squatted"))
    assert taken.status_code == 201, taken.text

    real = await _post(client, observer, _external())
    assert real.status_code == 201, real.text
    assert real.json()["deduplicated"] is False
    assert real.json()["id"] != taken.json()["id"]

    for key, first in ((observer, real), (squatter, taken)):
        repeat = await _post(client, key, _external(content="re-polled"))
        assert (repeat.status_code, repeat.json()["deduplicated"]) == (200, True)
        assert repeat.json()["id"] == first.json()["id"]

    recorded = await _recorded(client, observer)
    assert sorted(e["entityId"] for e in recorded) == sorted(
        [real.json()["id"], taken.json()["id"]]
    )
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM observation_dedup_keys")).scalar() == 2


async def test_no_answer_names_the_key_of_another_author(client: httpx.AsyncClient) -> None:
    """The key of another author is neither a conflict nor a repeat: nothing
    about that author's observation reaches the caller."""
    observer, squatter = await _two_agent_keys(client)
    taken = await _post(client, squatter, _external())
    real = await _post(client, observer, _external())
    assert (taken.status_code, real.status_code) == (201, 201)
    assert taken.json()["id"] not in real.text
    assert taken.json()["eventId"] not in real.text


async def test_event_carries_external_fields(client: httpx.AsyncClient) -> None:
    key = await _agent_key(client)
    previous = await _post(client, key, _external(dedupKey="monthu56/repo#42@open"))
    assert previous.status_code == 201

    body = _external(supersedes=previous.json()["id"])
    created = await _post(client, key, body)
    assert created.status_code == 201, created.text

    event = next(e for e in await _recorded(client, key) if e["entityId"] == created.json()["id"])
    payload = event["payload"]
    assert payload["source"] == "github"
    assert payload["dedupKey"] == body["dedupKey"]
    assert payload["observedAt"] == "2026-09-20T08:15:00+00:00"
    assert payload["supersedes"] == previous.json()["id"]
    assert payload["externalRef"] == body["externalRef"]
    assert event["id"] == created.json()["eventId"]


async def test_supersedes_unknown_or_foreign_observation_is_404(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _agent_key(client)
    unknown = await _post(client, key, _external(supersedes=str(uuid.uuid4())))
    assert unknown.status_code == 404, unknown.text

    _tenant, foreign_key = make_tenant_directly(sync_engine, "other")
    foreign = await _post(client, foreign_key, _external())
    assert foreign.status_code == 201
    spy = await _post(client, key, _external(supersedes=foreign.json()["id"]))
    assert spy.status_code == 404

    # A rejected request claims no dedup key: the corrected retry creates.
    assert (await _post(client, key, _external())).status_code == 201
    assert len(await _recorded(client, key)) == 1


async def test_supersedes_accepts_archived_observation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _agent_key(client)
    previous = await _post(client, key, {"kind": "note", "content": "v1"})
    assert previous.status_code == 201
    previous_id = previous.json()["id"]
    # Retention moved the journal row to the archive (ADR-0038).
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_archive (sequence, tx_id, id, tenant_id, event_type, "
                "entity_type, entity_id, actor_id, session_id, correlation_id, "
                "causation_id, request_id, trace_run_id, iam_actor_id, payload, "
                "occurred_at, archived_at) "
                "SELECT sequence, tx_id, id, tenant_id, event_type, entity_type, "
                "entity_id, actor_id, session_id, correlation_id, causation_id, "
                "request_id, trace_run_id, iam_actor_id, payload, occurred_at, now() "
                "FROM events WHERE entity_id = :id"
            ),
            {"id": previous_id},
        )
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM outbox"))
        conn.execute(text("DELETE FROM events WHERE entity_id = :id"), {"id": previous_id})

    created = await _post(client, key, {"kind": "note", "content": "v2", "supersedes": previous_id})
    assert created.status_code == 201, created.text


async def test_external_fields_are_validated(client: httpx.AsyncClient) -> None:
    key = await _agent_key(client)

    no_source = await _post(client, key, _external(source=None))
    assert no_source.status_code == 422
    assert no_source.json()["error"]["code"] == "observation_invalid"

    ref_without_source = await _post(
        client,
        key,
        {"kind": "note", "content": "x", "externalRef": {"system": "jira", "id": "CP-1"}},
    )
    assert ref_without_source.status_code == 422

    bad_source = await _post(client, key, _external(source="GitHub"))
    assert bad_source.status_code == 422
    assert bad_source.json()["error"]["code"] == "observation_invalid"

    blank_key = await _post(client, key, _external(dedupKey="   "))
    assert blank_key.status_code == 422

    naive_time = await _post(client, key, _external(observedAt="2026-09-20T08:15:00"))
    assert naive_time.status_code == 400
    assert naive_time.json()["error"]["code"] == "invalid_request"

    unknown_ref_field = await _post(
        client,
        key,
        _external(externalRef={"system": "github", "id": "1", "owner": "x"}),
    )
    assert unknown_ref_field.status_code == 400

    assert await _recorded(client, key) == []


async def test_legacy_body_without_new_fields_works_as_before(
    client: httpx.AsyncClient,
) -> None:
    key = await _agent_key(client)
    first = await _post(client, key, {"kind": "note", "content": "same text"})
    second = await _post(client, key, {"kind": "note", "content": "same text"})
    # No dedup without a key: every call is a new observation (ADR-0054).
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["deduplicated"] is False

    recorded = await _recorded(client, key)
    assert len(recorded) == 2
    payload = recorded[0]["payload"]
    # observedAt defaults to the recording time; no external fields appear.
    assert datetime.fromisoformat(payload["observedAt"]) == datetime.fromisoformat(
        recorded[0]["occurredAt"]
    )
    for absent in ("source", "dedupKey", "supersedes", "externalRef"):
        assert absent not in payload
