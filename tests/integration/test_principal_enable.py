"""``POST /principals/{id}:enable`` (CP-ADR-0077, amendment «Включение»).

The way back for a principal taken out by ``:disable``: the status returns,
the bindings ``:disable`` revoked do not — IAM entry comes back with a new,
explicit binding. A second call changes nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from platform_auth.testing import SigningKey
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.infrastructure.auth.iam import SCOPE_READ, SCOPE_WRITE
from tests.helpers import auth, create_agent_with_key, do_bootstrap
from tests.integration.test_agent_registry import _link, _publish, _tenant, coder_spec
from tests.integration.test_iam_bindings import create_principal, upsert
from tests.integration.test_iam_enforcement import ISSUER, enable_iam
from tests.integration.test_principal_disable import disable, events_of


async def enable(
    client: httpx.AsyncClient, key: str, principal_id: str, **body: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/principals/{principal_id}:enable",
        json=body or None,
        headers=auth(key),
    )


async def status_of(client: httpx.AsyncClient, key: str, principal_id: str) -> str:
    response = await client.get(f"/api/v1/principals/{principal_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    status: str = response.json()["status"]
    return status


async def test_enabled_human_logs_in_again_through_a_new_binding(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Acceptance: disabled → ``:enable`` → new binding → the IAM token is admitted."""
    signing_key = SigningKey.generate()
    # A long TTL: every answer below must come from the base, not the cache.
    enable_iam(app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    identity = {
        "issuer": ISSUER,
        "iamTenantId": str(iam_tenant),
        "iamPrincipalId": str(iam_principal),
    }
    bound = await upsert(client, admin_key, human["id"], permissions=["tasks.read"], **identity)
    assert bound.status_code == 201, bound.text
    token = signing_key.issue(
        subject=iam_principal,
        tenant_id=iam_tenant,
        scopes=[SCOPE_READ, SCOPE_WRITE],
        ttl_seconds=3600,
    )
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    assert (await disable(client, admin_key, human["id"])).status_code == 200
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 401
    # Re-adding the same person is refused while the principal is disabled.
    refused = await upsert(client, admin_key, human["id"], permissions=["tasks.read"], **identity)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "principal_not_active"

    enabled = await enable(client, admin_key, human["id"], reason="back from leave")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "active"
    assert enabled.json()["liveApiKeys"] == 0

    # The binding revoked by :disable is not restored: no entry yet.
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 401
    listed = await client.get(
        f"/api/v1/principals/{human['id']}/iam-bindings", headers=auth(admin_key)
    )
    assert [b["status"] for b in listed.json()["items"]] == ["revoked"]

    # An explicit new binding — with the permissions stated anew — lets it in.
    rebound = await upsert(
        client, admin_key, human["id"], permissions=["tasks.read", "tasks.write"], **identity
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["status"] == "active"
    admitted = await client.get("/api/v1/tasks", headers=auth(token))
    assert admitted.status_code == 200, admitted.text

    # A second identity bound after the enable is admitted too.
    other_tenant, other_principal = uuid.uuid4(), uuid.uuid4()
    other = await upsert(
        client,
        admin_key,
        human["id"],
        permissions=["tasks.read"],
        issuer=ISSUER,
        iamTenantId=str(other_tenant),
        iamPrincipalId=str(other_principal),
    )
    assert other.status_code == 201, other.text
    other_token = signing_key.issue(
        subject=other_principal, tenant_id=other_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
    )
    assert (await client.get("/api/v1/tasks", headers=auth(other_token))).status_code == 200

    event = (await events_of(client, admin_key, "principal", human["id"]))[-1]
    assert event["type"] == "principal.enabled"
    assert event["payload"] == {
        "kind": "human",
        "previousStatus": "disabled",
        "reason": "back from leave",
        "liveApiKeys": 0,
    }


async def test_an_enabled_agent_enters_again_with_its_unrevoked_key(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    assert (await disable(client, admin_key, agent["id"])).status_code == 200
    denied = await client.get("/api/v1/tasks", headers=auth(agent_key))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "principal_not_active"

    response = await enable(client, admin_key, agent["id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert response.json()["liveApiKeys"] == 1

    assert (await client.get("/api/v1/tasks", headers=auth(agent_key))).status_code == 200
    event = (await events_of(client, admin_key, "principal", agent["id"]))[-1]
    assert event["payload"] == {
        "kind": "agent",
        "previousStatus": "disabled",
        "reason": None,
        "liveApiKeys": 1,
    }


async def test_a_paused_principal_is_enabled(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/principals",
        json={"kind": "human", "displayName": "paused", "status": "paused"},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text

    response = await enable(client, admin_key, created.json()["id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    event = (await events_of(client, admin_key, "principal", created.json()["id"]))[-1]
    assert event["payload"]["previousStatus"] == "paused"


async def test_repeated_enable_is_200_and_changes_nothing(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")

    # Already active: 200, no event.
    untouched = await enable(client, admin_key, human["id"])
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["status"] == "active"
    assert [e["type"] for e in await events_of(client, admin_key, "principal", human["id"])] == [
        "principal.created"
    ]

    assert (await disable(client, admin_key, human["id"])).status_code == 200
    first = await enable(client, admin_key, human["id"])
    assert first.status_code == 200, first.text
    events_after_first = await events_of(client, admin_key, "principal", human["id"])

    again = await enable(client, admin_key, human["id"], reason="once more")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await events_of(client, admin_key, "principal", human["id"]) == events_after_first
    assert [e["type"] for e in events_after_first] == [
        "principal.created",
        "principal.disabled",
        "principal.enabled",
    ]


async def test_a_replay_under_the_same_idempotency_key_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")
    assert (await disable(client, admin_key, human["id"])).status_code == 200
    headers = {**auth(admin_key), "Idempotency-Key": "enable-human-1"}
    path = f"/api/v1/principals/{human['id']}:enable"

    first = await client.post(path, json={"reason": "back"}, headers=headers)
    assert first.status_code == 200, first.text
    events = await events_of(client, admin_key, "principal", human["id"])

    replay = await client.post(path, json={"reason": "back"}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("idempotency-replayed") == "true"
    assert replay.json() == first.json()
    assert await events_of(client, admin_key, "principal", human["id"]) == events

    reused = await client.post(path, json={"reason": "other"}, headers=headers)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "idempotency_key_reused"
    assert await events_of(client, admin_key, "principal", human["id"]) == events


async def test_only_an_admin_enables_an_admin(client: httpx.AsyncClient) -> None:
    """An unrevoked admin key authenticates again after the enable: that makes an admin."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["principals.read", "principals.write"]
    )
    admin_by_key, _ = await create_agent_with_key(
        client, admin_key, name="second admin", kind="human", permissions=["admin"]
    )
    # A binding with admin is revoked by :disable and not restored: no escalation.
    admin_by_binding = await create_principal(client, admin_key, kind="human")
    bound = await upsert(
        client,
        admin_key,
        admin_by_binding["id"],
        permissions=["admin"],
        issuer=ISSUER,
        iamTenantId=str(uuid.uuid4()),
        iamPrincipalId=str(uuid.uuid4()),
    )
    assert bound.status_code == 201, bound.text
    plain = await create_principal(client, admin_key, kind="human")
    for target in (admin_by_key, admin_by_binding, plain):
        assert (await disable(client, admin_key, target["id"])).status_code == 200

    refused = await enable(client, manager_key, admin_by_key["id"])
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_escalation"
    assert refused.json()["error"]["details"]["missing"] == ["admin"]
    assert await status_of(client, admin_key, admin_by_key["id"]) == "disabled"

    for target in (admin_by_binding, plain):
        allowed = await enable(client, manager_key, target["id"])
        assert allowed.status_code == 200, allowed.text

    by_admin = await enable(client, admin_key, admin_by_key["id"])
    assert by_admin.status_code == 200, by_admin.text


async def issue_key(
    client: httpx.AsyncClient,
    admin_key: str,
    principal_id: str,
    permissions: list[str],
    **body: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": permissions, **body},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def test_enable_hands_back_no_key_right_the_caller_lacks(
    client: httpx.AsyncClient,
) -> None:
    """The live keys come back with the status: the caller must hold all they hold.

    The rule of issuing a key or a binding (``permission_escalation`` with
    ``details.missing``), not only the admin special case.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    manage = ["principals.read", "principals.write"]
    _, narrow_key = await create_agent_with_key(
        client, admin_key, name="narrow", permissions=[*manage, "tasks.read"]
    )
    _, wide_key = await create_agent_with_key(
        client,
        admin_key,
        name="wide",
        permissions=[*manage, "tasks.read", "tasks.write", "events.read"],
    )
    target, _ = await create_agent_with_key(
        client, admin_key, name="target", permissions=["tasks.read", "tasks.write"]
    )
    await issue_key(client, admin_key, target["id"], ["tasks.read", "events.read"])
    # Neither a revoked nor an expired key authenticates again: not counted.
    revoked = await issue_key(client, admin_key, target["id"], ["approvals.decide"])
    revoke = await client.post(f"/api/v1/api-keys/{revoked['id']}:revoke", headers=auth(admin_key))
    assert revoke.status_code == 200, revoke.text
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await issue_key(client, admin_key, target["id"], ["projects.read"], expiresAt=past)
    assert (await disable(client, admin_key, target["id"])).status_code == 200

    refused = await enable(client, narrow_key, target["id"])
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert error["code"] == "permission_escalation"
    assert error["details"] == {"missing": ["events.read", "tasks.write"]}
    assert await status_of(client, admin_key, target["id"]) == "disabled"

    allowed = await enable(client, wide_key, target["id"])
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "active"
    assert allowed.json()["liveApiKeys"] == 2
    event = (await events_of(client, admin_key, "principal", target["id"]))[-1]
    assert event["payload"]["liveApiKeys"] == 2


async def test_a_paused_principal_hands_back_its_live_binding(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A paused principal keeps its bindings, so enabling it hands them back.

    Their permissions join the escalation check next to the live keys: a
    caller without them is refused with ``details.missing``.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    manage = ["principals.read", "principals.write"]
    _, narrow_key = await create_agent_with_key(
        client, admin_key, name="narrow", permissions=[*manage, "tasks.read"]
    )
    _, wide_key = await create_agent_with_key(
        client, admin_key, name="wide", permissions=[*manage, "tasks.read", "tasks.write"]
    )
    human = await create_principal(client, admin_key, kind="human")
    bound = await upsert(
        client,
        admin_key,
        human["id"],
        permissions=["tasks.read", "tasks.write"],
        issuer=ISSUER,
        iamTenantId=str(uuid.uuid4()),
        iamPrincipalId=str(uuid.uuid4()),
    )
    assert bound.status_code == 201, bound.text
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'paused' WHERE id = :id"), {"id": human["id"]}
        )

    refused = await enable(client, narrow_key, human["id"])
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert error["code"] == "permission_escalation"
    assert error["details"] == {"missing": ["tasks.write"]}
    assert await status_of(client, admin_key, human["id"]) == "paused"

    allowed = await enable(client, wide_key, human["id"])
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "active"


async def test_enable_drops_the_refusal_its_process_cached(
    app: FastAPI, client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The enable's own cache reset is what lets a still-bound identity in.

    A paused principal keeps its binding (``:disable`` is what revokes them).
    Its token is refused ``principal_not_active``, and that refusal is cached
    for the whole staleness window: setting the status behind the API's back
    changes nothing. ``:enable`` drops it — the next request reads the base.
    """
    signing_key = SigningKey.generate()
    enable_iam(app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    bound = await upsert(
        client,
        admin_key,
        human["id"],
        permissions=["tasks.read"],
        issuer=ISSUER,
        iamTenantId=str(iam_tenant),
        iamPrincipalId=str(iam_principal),
    )
    assert bound.status_code == 201, bound.text
    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
    )

    def set_status(status: str) -> None:
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE principals SET status = :status WHERE id = :id"),
                {"status": status, "id": human["id"]},
            )

    set_status("paused")
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 401
    # The refusal is cached: the base alone does not let it in.
    set_status("active")
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 401
    set_status("paused")

    enabled = await enable(client, admin_key, human["id"])
    assert enabled.status_code == 200, enabled.text
    admitted = await client.get("/api/v1/tasks", headers=auth(token))
    assert admitted.status_code == 200, admitted.text


async def test_refusals(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    service = await client.post(
        "/api/v1/principals",
        json={"kind": "service", "displayName": "svc", "status": "disabled"},
        headers=auth(admin_key),
    )
    assert service.status_code == 201, service.text
    human = await create_principal(client, admin_key, kind="human")
    assert (await disable(client, admin_key, human["id"])).status_code == 200
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["principals.read"]
    )

    as_service = await enable(client, admin_key, service.json()["id"])
    assert as_service.status_code == 422
    assert as_service.json()["error"]["code"] == "principal_kind_not_enableable"

    no_right = await enable(client, reader_key, human["id"])
    assert no_right.status_code == 403

    missing = await enable(client, admin_key, str(uuid.uuid4()))
    assert missing.status_code == 404

    assert await status_of(client, admin_key, human["id"]) == "disabled"


async def test_a_retired_agent_is_not_enabled(client: httpx.AsyncClient) -> None:
    """The registry owns its agents' identity; a retired one returns as a new key."""
    admin_key, workspace = await _tenant(client)
    assert (await _publish(client, admin_key, coder_spec(workspace["id"]))).status_code == 201
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    retired = await client.post(
        "/api/v1/agents/coder:retire", json={"reason": "done"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    assert await status_of(client, admin_key, principal_id) == "disabled"

    refused = await enable(client, admin_key, principal_id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "use_agent_publish"
    assert refused.json()["error"]["details"]["agent"] == "coder"
    assert refused.json()["error"]["details"]["agentStatus"] == "retired"
    assert await status_of(client, admin_key, principal_id) == "disabled"


def _take_out_beside_the_registry(sync_engine: Engine, principal_id: str) -> None:
    """What only SQL does to a live agent: ``:disable`` refuses it (``use_agent_retire``)."""
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"), {"id": principal_id}
        )
        conn.execute(
            text(
                "UPDATE iam_principal_bindings SET status = 'revoked', revoked_at = now() "
                "WHERE principal_id = :id"
            ),
            {"id": principal_id},
        )


async def _binding_status(client: httpx.AsyncClient, key: str, principal_id: str) -> str:
    response = await client.get(
        f"/api/v1/principals/{principal_id}/iam-bindings", headers=auth(key)
    )
    assert response.status_code == 200, response.text
    (binding,) = response.json()["items"]
    status: str = binding["status"]
    return status


async def test_a_live_agent_comes_back_with_enable_and_its_link(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The way out of ``principal_not_active`` for a live agent (CP-ADR-0073, I5).

    Publishing does not touch the principal's status and ``PUT …/identity``
    refuses a non-active one, so ``:enable`` brings the status back; the
    binding stays revoked until the same identity is linked again.
    """
    admin_key, workspace = await _tenant(client)
    assert (await _publish(client, admin_key, coder_spec(workspace["id"]))).status_code == 201
    first = {"iamTenantId": str(uuid.uuid4()), "iamPrincipalId": str(uuid.uuid4())}
    principal_id = (await _link(client, admin_key, **first)).json()["principalId"]
    _take_out_beside_the_registry(sync_engine, principal_id)

    stuck = await _link(client, admin_key, **first)
    assert stuck.status_code == 422, stuck.text
    assert stuck.json()["error"]["code"] == "principal_not_active"

    enabled = await enable(client, admin_key, principal_id, reason="disabled beside the registry")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "active"
    assert await _binding_status(client, admin_key, principal_id) == "revoked"
    (event,) = [
        e
        for e in await events_of(client, admin_key, "principal", principal_id)
        if e["type"] == "principal.enabled"
    ]
    assert event["payload"]["previousStatus"] == "disabled"

    again = await enable(client, admin_key, principal_id)
    assert again.status_code == 200, again.text

    relinked = await _link(client, admin_key, **first)
    assert relinked.status_code == 200, relinked.text
    assert await _binding_status(client, admin_key, principal_id) == "active"


async def test_a_live_service_agent_is_enabled_too(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A registry service agent is the registry's, not its installer's: ``service`` passes."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = {
        "displayName": "Notification service",
        "identity": {"kind": "service", "permissions": ["events.read", "tasks.read"]},
        "placement": "none",
    }
    published = await _publish(client, admin_key, spec, agent="notification-service")
    assert published.status_code == 201, published.text
    linked = await _link(client, admin_key, agent="notification-service")
    principal_id = linked.json()["principalId"]
    _take_out_beside_the_registry(sync_engine, principal_id)

    enabled = await enable(client, admin_key, principal_id)
    assert enabled.status_code == 200, enabled.text
    assert await status_of(client, admin_key, principal_id) == "active"


async def test_a_live_agent_principal_is_enabled_only_with_its_live_rights(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The escalation rule stays: a live binding left by SQL comes back with the status."""
    admin_key, workspace = await _tenant(client)
    assert (await _publish(client, admin_key, coder_spec(workspace["id"]))).status_code == 201
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'paused' WHERE id = :id"), {"id": principal_id}
        )
    _, operator_key = await create_agent_with_key(
        client, admin_key, name="operator", kind="human", permissions=["principals.write"]
    )

    refused = await enable(client, operator_key, principal_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_escalation"
    assert await status_of(client, admin_key, principal_id) == "paused"
