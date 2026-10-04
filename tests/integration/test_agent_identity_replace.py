"""A service agent moves to a new IAM identity (CP-ADR-0073, amendment 2026-09-30).

A service account re-created in IAM is the same service: the agent keeps its
key and principal, the registry follows the new identity, the previous
binding is revoked and the new one gets the rights of the current revision —
so the next revision reshapes the binding that actually enters. Any other
agent still changes identity only by retirement and a new key. A plain
``iam-bindings`` upsert no longer takes an identity from its owner unchecked.
"""

import copy
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, do_bootstrap
from tests.integration.test_agent_registry import (
    ISSUER,
    _events,
    _link,
    _publish,
    _tenant,
    coder_spec,
)
from tests.integration.test_iam_bindings import create_principal

SERVICE_SPEC: dict[str, Any] = {
    "displayName": "Notification service",
    "identity": {"kind": "service", "permissions": ["events.read", "tasks.read"]},
    "placement": "none",
}
KEY = "notification-service"


def identity(**overrides: Any) -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
        **overrides,
    }


async def _replace(
    client: httpx.AsyncClient,
    key: str,
    body: dict[str, Any],
    agent: str = KEY,
    reason: str | None = "service account re-created",
) -> httpx.Response:
    payload = {**body, "reason": reason} if reason is not None else body
    return await client.post(
        f"/api/v1/agents/{agent}/identity:replace", json=payload, headers=auth(key)
    )


async def _service(client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    """An admin key, the principal of a linked service agent and its identity."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    published = await _publish(client, admin_key, SERVICE_SPEC, agent=KEY)
    assert published.status_code == 201, published.text
    first = identity()
    linked = await _link(client, admin_key, agent=KEY, **first)
    assert linked.status_code == 200, linked.text
    return admin_key, linked.json()["principalId"], first


async def _bindings(client: httpx.AsyncClient, key: str, principal_id: str) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/principals/{principal_id}/iam-bindings", headers=auth(key)
    )
    assert response.status_code == 200, response.text
    return {b["iamPrincipalId"]: b for b in response.json()["items"]}


def _registry_identity(sync_engine: Engine, key: str = KEY) -> tuple[str, str, str]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT iam_issuer, iam_tenant_id, iam_principal_id FROM agents WHERE key = :k"),
            {"k": key},
        ).one()
    return row[0], str(row[1]), str(row[2])


# --- the route ------------------------------------------------------------------


async def test_a_service_agent_moves_to_a_new_identity_and_keeps_its_principal(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)
    second = identity()

    replaced = await _replace(client, admin_key, second)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["principalId"] == principal_id
    assert replaced.json()["key"] == KEY
    assert _registry_identity(sync_engine) == (
        ISSUER,
        second["iamTenantId"],
        second["iamPrincipalId"],
    )

    bindings = await _bindings(client, admin_key, principal_id)
    assert bindings[first["iamPrincipalId"]]["status"] == "revoked"
    assert bindings[second["iamPrincipalId"]]["status"] == "active"
    assert bindings[second["iamPrincipalId"]]["permissions"] == ["events.read", "tasks.read"]

    events = await _events(client, admin_key, "agent.identity_replaced")
    assert [e["payload"] for e in events] == [
        {
            "key": KEY,
            "revision": 1,
            "principalId": principal_id,
            "issuer": ISSUER,
            "iamTenantId": second["iamTenantId"],
            "iamPrincipalId": second["iamPrincipalId"],
            "previousIssuer": ISSUER,
            "previousIamTenantId": first["iamTenantId"],
            "previousIamPrincipalId": first["iamPrincipalId"],
            "reason": "service account re-created",
        }
    ]
    assert events[0]["actorId"] is not None
    revoked = await _events(client, admin_key, "iam_binding.revoked")
    assert [e["payload"]["iamPrincipalId"] for e in revoked] == [first["iamPrincipalId"]]
    created = await _events(client, admin_key, "iam_binding.created")
    assert [e["payload"]["iamPrincipalId"] for e in created] == [
        first["iamPrincipalId"],
        second["iamPrincipalId"],
    ]

    # The same identity again changes nothing, with or without an idempotency key.
    again = await _replace(client, admin_key, second, reason="again")
    assert again.status_code == 200, again.text
    assert len(await _events(client, admin_key, "agent.identity_replaced")) == 1
    assert len(await _events(client, admin_key, "iam_binding.revoked")) == 1
    # The link route now knows the new identity as the agent's own.
    assert (await _link(client, admin_key, agent=KEY, **second)).status_code == 200
    conflict = await _link(client, admin_key, agent=KEY, **first)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "agent_identity_conflict"


async def test_the_next_revision_reshapes_the_new_binding_not_the_old_one(
    client: httpx.AsyncClient,
) -> None:
    """The defect behind the amendment: publication reopened the previous binding."""
    admin_key, principal_id, first = await _service(client)
    second = identity()
    assert (await _replace(client, admin_key, second)).status_code == 200

    narrower = copy.deepcopy(SERVICE_SPEC)
    narrower["identity"]["permissions"] = ["tasks.read"]
    published = await _publish(client, admin_key, narrower, agent=KEY)
    assert published.status_code == 201, published.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert bindings[second["iamPrincipalId"]]["permissions"] == ["tasks.read"]
    assert bindings[second["iamPrincipalId"]]["status"] == "active"
    assert bindings[first["iamPrincipalId"]]["status"] == "revoked"


async def test_a_binding_made_beside_the_registry_is_adopted_with_the_revision_rights(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The ADR-0053 workaround of a bootstrap: a new binding, the old one revoked.

    ``iam-bindings`` no longer makes such a binding (I4); one made before is
    written here directly.
    """
    admin_key, principal_id, first = await _service(client)
    second = identity()
    with sync_engine.begin() as conn:
        beside_id = conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, id, :issuer, :iam_tenant, :iam_principal, "
                '\'["events.read", "tasks.read", "tasks.write"]\', \'active\', now(), now() '
                "FROM principals WHERE id = :principal RETURNING id"
            ),
            {
                "issuer": second["issuer"],
                "iam_tenant": second["iamTenantId"],
                "iam_principal": second["iamPrincipalId"],
                "principal": principal_id,
            },
        ).scalar_one()
    old_id = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]["id"]
    revoked = await client.post(f"/api/v1/iam-bindings/{old_id}:revoke", headers=auth(admin_key))
    assert revoked.status_code == 200, revoked.text

    replaced = await _replace(client, admin_key, second)
    assert replaced.status_code == 200, replaced.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert set(bindings) == {first["iamPrincipalId"], second["iamPrincipalId"]}
    assert bindings[second["iamPrincipalId"]]["id"] == str(beside_id)
    # The wider rights of the workaround give way to those of the revision.
    assert bindings[second["iamPrincipalId"]]["permissions"] == ["events.read", "tasks.read"]
    assert bindings[first["iamPrincipalId"]]["status"] == "revoked"
    # The previous binding was already revoked: no second revocation event.
    assert len(await _events(client, admin_key, "iam_binding.revoked")) == 1


async def test_moving_back_to_a_previous_identity_reopens_its_binding(
    client: httpx.AsyncClient,
) -> None:
    admin_key, principal_id, first = await _service(client)
    second = identity()
    assert (await _replace(client, admin_key, second)).status_code == 200
    back = await _replace(client, admin_key, first)
    assert back.status_code == 200, back.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert bindings[first["iamPrincipalId"]]["status"] == "active"
    assert bindings[second["iamPrincipalId"]]["status"] == "revoked"
    assert len(bindings) == 2


async def test_a_new_iam_tenant_of_the_same_identity_updates_its_binding(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)
    moved = {**first, "iamTenantId": str(uuid.uuid4())}
    replaced = await _replace(client, admin_key, moved)
    assert replaced.status_code == 200, replaced.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert len(bindings) == 1
    assert bindings[first["iamPrincipalId"]]["status"] == "active"
    assert bindings[first["iamPrincipalId"]]["iamTenantId"] == moved["iamTenantId"]
    assert _registry_identity(sync_engine)[1] == moved["iamTenantId"]
    assert await _events(client, admin_key, "iam_binding.revoked") == []


# --- refusals -----------------------------------------------------------------


async def test_an_agent_that_is_not_a_service_keeps_the_old_refusal(
    client: httpx.AsyncClient,
) -> None:
    admin_key, workspace = await _tenant(client)
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    principal_id = (await _link(client, admin_key)).json()["principalId"]

    refused = await _replace(client, admin_key, identity(), agent="coder")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "agent_identity_conflict"
    assert refused.json()["error"]["details"] == {"agent": "coder", "kind": "agent"}
    bindings = await _bindings(client, admin_key, principal_id)
    assert [b["status"] for b in bindings.values()] == ["active"]
    assert await _events(client, admin_key, "agent.identity_replaced") == []


async def test_unknown_unlinked_and_retired_agents_are_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    missing = await _replace(client, admin_key, identity(), agent="nobody")
    assert missing.status_code == 404, missing.text

    await _publish(client, admin_key, SERVICE_SPEC, agent=KEY)
    unlinked = await _replace(client, admin_key, identity())
    assert unlinked.status_code == 409, unlinked.text
    assert unlinked.json()["error"]["code"] == "agent_identity_not_linked"

    assert (await _link(client, admin_key, agent=KEY)).status_code == 200
    retired = await client.post(
        f"/api/v1/agents/{KEY}:retire", json={"reason": "gone"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    after = await _replace(client, admin_key, identity())
    assert after.status_code == 409, after.text
    assert after.json()["error"]["code"] == "agent_retired"


async def test_a_disabled_principal_gets_no_new_identity(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, _ = await _service(client)
    # ``:disable`` does not take a service principal (CP-ADR-0077): an operator did.
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"), {"id": principal_id}
        )

    refused = await _replace(client, admin_key, identity())
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "principal_not_active"


async def test_an_identity_of_another_principal_is_not_taken(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)
    other = await create_principal(client, admin_key, kind="service", name="other")
    taken = identity()
    bound = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**taken, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert bound.status_code == 201, bound.text

    refused = await _replace(client, admin_key, taken)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "agent_identity_conflict"
    # Nothing moved: the registry and both bindings are as they were.
    assert _registry_identity(sync_engine)[2] == first["iamPrincipalId"]
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "active"
    assert (await _bindings(client, admin_key, other["id"]))[taken["iamPrincipalId"]][
        "status"
    ] == "active"


async def test_the_route_needs_agents_manage_and_the_rights_it_hands_over(
    client: httpx.AsyncClient,
) -> None:
    admin_key, _, _ = await _service(client)
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name="fleet-controller",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    forbidden = await _replace(client, fleet_key, identity())
    assert forbidden.status_code == 403, forbidden.text

    # agents.manage alone cannot take over an identity holding more than the caller.
    _, manager_key = await create_agent_with_key(
        client,
        admin_key,
        name="catalog-admin",
        kind="human",
        permissions=["agents.manage", "tasks.read"],
    )
    escalation = await _replace(client, manager_key, identity())
    assert escalation.status_code == 403, escalation.text
    assert escalation.json()["error"]["code"] == "permission_escalation"
    assert escalation.json()["error"]["details"]["missing"] == ["events.read"]
    assert await _events(client, admin_key, "agent.identity_replaced") == []


async def test_without_a_configured_issuer_the_service_keeps_its_own(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """No ``CP_IAM_ISSUER`` to compare with: the issuer the agent already has is it."""
    admin_key, _, first = await _service(client)
    refused = await _replace(client, admin_key, identity(issuer="https://evil.example"))
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert refused.json()["error"]["details"]["expected"] == ISSUER
    assert _registry_identity(sync_engine)[2] == first["iamPrincipalId"]


async def test_the_body_is_validated(client: httpx.AsyncClient) -> None:
    admin_key, _, _ = await _service(client)
    for body, reason in (
        (identity(), None),
        (identity(), ""),
        (identity(iamPrincipalId="not-a-uuid"), "r"),
        ({"issuer": ISSUER, "iamTenantId": str(uuid.uuid4())}, "r"),
    ):
        response = await _replace(client, admin_key, body, reason=reason)
        assert response.status_code == 400, (body, reason, response.text)


# --- the owner of a moved binding (ADR-0053, amendment 2026-09-30) --------------


async def test_upsert_does_not_take_the_identity_of_a_registry_agent(
    client: httpx.AsyncClient,
) -> None:
    admin_key, principal_id, first = await _service(client)
    other = await create_principal(client, admin_key, kind="service", name="other")

    refused = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**first, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "agent_identity_conflict"
    assert refused.json()["error"]["details"] == {"agent": KEY}
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "active"

    # Nor is its own identity re-bound there, for an admin neither (I4, TASK-001127).
    same = await client.post(
        f"/api/v1/principals/{principal_id}/iam-bindings",
        json={**first, "permissions": ["events.read", "tasks.read"]},
        headers=auth(admin_key),
    )
    assert same.status_code == 409, same.text
    assert same.json()["error"]["code"] == "agent_identity_conflict"

    # Once the agent moved away, the previous identity is nobody's registry identity.
    assert (await _replace(client, admin_key, identity())).status_code == 200
    moved = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**first, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert moved.status_code == 200, moved.text
    updated = await _events(client, admin_key, "iam_binding.updated")
    assert updated[-1]["payload"]["previousPrincipalId"] == principal_id
    assert updated[-1]["payload"]["principalId"] == other["id"]


async def test_upsert_takes_the_identity_of_a_retired_agent(client: httpx.AsyncClient) -> None:
    admin_key, _, first = await _service(client)
    retired = await client.post(
        f"/api/v1/agents/{KEY}:retire", json={"reason": "gone"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    other = await create_principal(client, admin_key, kind="service", name="other")
    moved = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**first, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert moved.status_code == 200, moved.text


async def test_only_an_admin_moves_the_identity_of_a_human(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    person = await create_principal(client, admin_key, kind="human", name="person")
    other = await create_principal(client, admin_key, kind="agent", name="other")
    login = identity()
    bound = await client.post(
        f"/api/v1/principals/{person['id']}/iam-bindings",
        json={**login, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert bound.status_code == 201, bound.text
    _, operator_key = await create_agent_with_key(
        client,
        admin_key,
        name="operator",
        kind="human",
        permissions=["principals.write", "principals.read", "tasks.read"],
    )

    refused = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**login, "permissions": ["tasks.read"]},
        headers=auth(operator_key),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_escalation"
    assert refused.json()["error"]["details"] == {"previousOwnerKind": "human"}

    # The same person's binding is re-bound by the operator as before.
    same = await client.post(
        f"/api/v1/principals/{person['id']}/iam-bindings",
        json={**login, "permissions": ["tasks.read"]},
        headers=auth(operator_key),
    )
    assert same.status_code == 200, same.text

    moved = await client.post(
        f"/api/v1/principals/{other['id']}/iam-bindings",
        json={**login, "permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    assert moved.status_code == 200, moved.text
