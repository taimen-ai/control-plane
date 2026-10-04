"""The bindings of a registry agent belong to the registry (CP-ADR-0073, amendment
2026-09-30, I2 and I4; ADR-0053, amendment 2026-09-30).

Each case is a probe from the review of TASK-001063 and the refusal it now
gets: an identity of an issuer the core does not trust, a second identity on
an agent's principal through ``iam-bindings``, a binding made beside the
registry that outlives ``identity:replace``, an identity of a service
outside the registry moved by a non-admin, and the registry's own identity
re-bound by an admin with rights beside the revision (TASK-001127). The module runs with
``CP_IAM_ISSUER`` configured, as a deployment with IAM does.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from tests.helpers import auth, create_agent_with_key, do_bootstrap
from tests.integration.test_agent_identity_replace import (
    KEY,
    SERVICE_SPEC,
    _bindings,
    _registry_identity,
    _replace,
    _service,
    identity,
)
from tests.integration.test_agent_registry import (
    ISSUER,
    _events,
    _link,
    _publish,
    _tenant,
    coder_spec,
)
from tests.integration.test_iam_bindings import create_principal

UNTRUSTED = "https://evil.example"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"iam_issuer": ISSUER})


async def _upsert(
    client: httpx.AsyncClient,
    key: str,
    principal_id: str,
    body: dict[str, Any],
    permissions: list[str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/principals/{principal_id}/iam-bindings",
        json={**body, "permissions": permissions or ["tasks.read"]},
        headers=auth(key),
    )


async def _operator(client: httpx.AsyncClient, admin_key: str) -> str:
    """A holder of ``principals.write`` without ``admin``."""
    _, key = await create_agent_with_key(
        client,
        admin_key,
        name="operator",
        kind="human",
        permissions=["principals.write", "principals.read", "tasks.read", "events.read"],
    )
    return key


def _bind_beside(sync_engine: Engine, principal_id: str, body: dict[str, Any]) -> None:
    """A binding made beside the registry before ``iam-bindings`` refused it."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "SELECT :id, tenant_id, id, :issuer, :iam_tenant, :iam_principal, "
                '\'["events.read", "tasks.read", "tasks.write"]\', \'active\', now(), now() '
                "FROM principals WHERE id = :principal"
            ),
            {
                "id": str(uuid.uuid4()),
                "issuer": body["issuer"],
                "iam_tenant": body["iamTenantId"],
                "iam_principal": body["iamPrincipalId"],
                "principal": principal_id,
            },
        )


# --- 1. the issuer ------------------------------------------------------------------


async def test_replace_refuses_an_untrusted_issuer_and_keeps_the_service_in(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)

    refused = await _replace(client, admin_key, identity(issuer=UNTRUSTED))
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert refused.json()["error"]["details"] == {"issuer": UNTRUSTED, "expected": ISSUER}
    # The working binding is not revoked for a useless one.
    assert _registry_identity(sync_engine)[2] == first["iamPrincipalId"]
    bindings = await _bindings(client, admin_key, principal_id)
    assert [b["status"] for b in bindings.values()] == ["active"]
    assert await _events(client, admin_key, "agent.identity_replaced") == []
    assert await _events(client, admin_key, "iam_binding.revoked") == []


async def test_link_refuses_an_untrusted_issuer(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _publish(client, admin_key, SERVICE_SPEC, agent=KEY)

    for issuer in (UNTRUSTED, ISSUER + "/", ISSUER.upper()):
        refused = await _link(client, admin_key, agent=KEY, issuer=issuer)
        assert refused.status_code == 422, (issuer, refused.text)
        assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert await _events(client, admin_key, "principal.created") == []

    linked = await _link(client, admin_key, agent=KEY)
    assert linked.status_code == 200, linked.text


async def test_upsert_refuses_an_untrusted_issuer(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    other = await create_principal(client, admin_key, kind="service", name="other")

    refused = await _upsert(client, admin_key, other["id"], identity(issuer=UNTRUSTED))
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert await _bindings(client, admin_key, other["id"]) == {}

    created = await _upsert(client, admin_key, other["id"], identity())
    assert created.status_code == 201, created.text


# --- 2. iam-bindings on the principal of a registry agent ---------------------------


async def test_a_second_identity_on_an_agent_principal_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """The probe: ``principals.write`` adds an identity to ``coder`` — was 201."""
    admin_key, workspace = await _tenant(client)
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    operator_key = await _operator(client, admin_key)

    for key in (operator_key, admin_key):
        refused = await _upsert(client, key, principal_id, identity())
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "agent_identity_conflict"
        assert refused.json()["error"]["details"] == {
            "agent": "coder",
            "route": "/agents/coder/identity",
        }
    assert len(await _bindings(client, admin_key, principal_id)) == 1


async def test_the_previous_identity_of_a_service_is_not_reopened_beside_the_registry(
    client: httpx.AsyncClient,
) -> None:
    """The probe: after ``replace`` the old identity comes back with wider rights."""
    admin_key, principal_id, first = await _service(client)
    assert (await _replace(client, admin_key, identity())).status_code == 200
    operator_key = await _operator(client, admin_key)

    for key in (operator_key, admin_key):
        refused = await _upsert(
            client, key, principal_id, first, ["events.read", "tasks.read", "principals.read"]
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "agent_identity_conflict"
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "revoked"
    # Through the registry the way back is open, with the rights of the revision.
    back = await _replace(client, admin_key, first)
    assert back.status_code == 200, back.text
    reopened = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert reopened["status"] == "active"
    assert reopened["permissions"] == ["events.read", "tasks.read"]


async def test_the_registry_identity_itself_is_not_rebound_through_iam_bindings(
    client: httpx.AsyncClient,
) -> None:
    """The probe of TASK-001120: admin re-bound it with ``principals.write`` — was 200.

    The transitional exception for the bootstrap is gone (I4, TASK-001127): the
    registry's own identity is refused like any other, for an admin too, and
    its binding keeps the rights of the revision.
    """
    admin_key, principal_id, first = await _service(client)
    operator_key = await _operator(client, admin_key)
    before = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]

    for key in (operator_key, admin_key):
        refused = await _upsert(
            client, key, principal_id, first, ["events.read", "principals.write", "tasks.read"]
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "agent_identity_conflict"
        route = f"/agents/{KEY}/identity"
        assert refused.json()["error"]["details"] == {"agent": KEY, "route": route}

    after = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert after == before
    assert after["permissions"] == ["events.read", "tasks.read"]
    assert len(await _events(client, admin_key, "iam_binding.updated")) == 0
    # The way that stays: the idempotent link of the same identity, which
    # changes nothing while the binding is active.
    relinked = await _link(client, admin_key, agent=KEY, **first)
    assert relinked.status_code == 200, relinked.text
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]] == after
    assert len(await _events(client, admin_key, "iam_binding.updated")) == 0


async def test_a_revoked_registry_identity_is_not_reopened_by_an_admin(
    client: httpx.AsyncClient,
) -> None:
    """Revoked beside the registry, the binding stays shut: no admin reopens it."""
    admin_key, principal_id, first = await _service(client)
    binding_id = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]["id"]
    revoked = await client.post(
        f"/api/v1/iam-bindings/{binding_id}:revoke", headers=auth(admin_key)
    )
    assert revoked.status_code == 200, revoked.text

    refused = await _upsert(client, admin_key, principal_id, first, ["events.read", "tasks.read"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "agent_identity_conflict"
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "revoked"


async def _revoke_registry_binding(
    client: httpx.AsyncClient, admin_key: str, principal_id: str, body: dict[str, Any]
) -> str:
    binding_id: str = (await _bindings(client, admin_key, principal_id))[body["iamPrincipalId"]][
        "id"
    ]
    revoked = await client.post(
        f"/api/v1/iam-bindings/{binding_id}:revoke", headers=auth(admin_key)
    )
    assert revoked.status_code == 200, revoked.text
    return binding_id


async def test_link_of_the_same_identity_reopens_a_revoked_registry_binding(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The way back after I4 (TASK-001203): ``PUT …/identity`` with the same identity.

    The binding comes back with the rights of the current revision, whatever
    it held when it was shut, and the journal says so as ``iam-bindings``
    would; a repeat changes nothing more.
    """
    admin_key, principal_id, first = await _service(client)
    binding_id = await _revoke_registry_binding(client, admin_key, principal_id, first)
    # Rights beside the revision, left on the shut row: they do not come back.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE iam_principal_bindings SET permissions = "
                '\'["events.read", "principals.write", "tasks.read"]\' WHERE id = :id'
            ),
            {"id": binding_id},
        )

    reopened = await _link(client, admin_key, agent=KEY, **first)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["principalId"] == principal_id

    binding = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert binding["id"] == binding_id
    assert binding["status"] == "active"
    assert binding["revokedAt"] is None
    assert binding["permissions"] == ["events.read", "tasks.read"]
    updated = await _events(client, admin_key, "iam_binding.updated")
    assert len(updated) == 1
    assert updated[0]["entityId"] == binding_id
    assert updated[0]["payload"] == {
        "principalId": principal_id,
        "issuer": first["issuer"],
        "iamTenantId": first["iamTenantId"],
        "iamPrincipalId": first["iamPrincipalId"],
        "permissions": ["events.read", "tasks.read"],
        "visibility": "tenant",
    }
    # Only the binding: no second principal, no second binding.
    assert len(await _events(client, admin_key, "principal.created")) == 1
    assert len(await _bindings(client, admin_key, principal_id)) == 1

    again = await _link(client, admin_key, agent=KEY, **first)
    assert again.status_code == 200, again.text
    assert len(await _events(client, admin_key, "iam_binding.updated")) == 1


async def test_link_does_not_reopen_the_binding_of_a_non_active_principal(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)
    await _revoke_registry_binding(client, admin_key, principal_id, first)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"),
            {"id": principal_id},
        )

    refused = await _link(client, admin_key, agent=KEY, **first)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "principal_not_active"
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "revoked"
    assert await _events(client, admin_key, "iam_binding.updated") == []


async def test_link_of_another_identity_does_not_reopen_the_revoked_one(
    client: httpx.AsyncClient,
) -> None:
    admin_key, principal_id, first = await _service(client)
    await _revoke_registry_binding(client, admin_key, principal_id, first)

    for other in (identity(), {**first, "iamTenantId": str(uuid.uuid4())}):
        refused = await _link(client, admin_key, agent=KEY, **other)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "agent_identity_conflict"
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "revoked"


async def test_the_placement_right_alone_reopens_the_binding(
    client: httpx.AsyncClient,
) -> None:
    """``agents.status.write`` is the whole authority of the way back (I5).

    The fleet-controller holds none of the revision's rights: the escalation
    rule of publishing does not apply, the rights were checked against
    whoever applied the revision.
    """
    admin_key, principal_id, first = await _service(client)
    await _revoke_registry_binding(client, admin_key, principal_id, first)
    _, fleet_key = await create_agent_with_key(
        client, admin_key, name="fleet", kind="service", permissions=["agents.status.write"]
    )

    reopened = await _link(client, fleet_key, agent=KEY, **first)
    assert reopened.status_code == 200, reopened.text
    binding = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert binding["status"] == "active"
    assert binding["permissions"] == ["events.read", "tasks.read"]


async def test_without_the_placement_right_the_binding_stays_revoked(
    client: httpx.AsyncClient,
) -> None:
    """Holding the revision's rights and ``agents.manage`` is not enough."""
    admin_key, principal_id, first = await _service(client)
    await _revoke_registry_binding(client, admin_key, principal_id, first)
    _, manager_key = await create_agent_with_key(
        client,
        admin_key,
        name="manager",
        kind="human",
        permissions=["agents.manage", "agents.read", "events.read", "tasks.read"],
    )

    refused = await _link(client, manager_key, agent=KEY, **first)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_denied"
    assert (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]][
        "status"
    ] == "revoked"
    assert await _events(client, admin_key, "iam_binding.updated") == []


def _republish_beside(sync_engine: Engine, permissions: list[str]) -> None:
    """A current revision whose rights a publish would refuse today.

    Revisions are immutable, so the row is a new one: the catalog or the kind
    rule changed after the revision was checked.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_revisions (id, tenant_id, agent_id, revision, spec, "
                "spec_hash, source_kind, source_package_key, source_package_version, "
                "created_by, created_at) "
                "SELECT :id, r.tenant_id, r.agent_id, r.revision + 1, "
                "jsonb_set(r.spec, '{identity,permissions}', CAST(:permissions AS jsonb)), "
                "r.spec_hash, r.source_kind, r.source_package_key, r.source_package_version, "
                "r.created_by, now() "
                "FROM agent_revisions r JOIN agents a "
                "ON a.id = r.agent_id AND a.current_revision = r.revision WHERE a.key = :key"
            ),
            {"id": str(uuid.uuid4()), "permissions": json.dumps(permissions), "key": KEY},
        )
        conn.execute(
            text("UPDATE agents SET current_revision = current_revision + 1 WHERE key = :key"),
            {"key": KEY},
        )


@pytest.mark.parametrize(
    ("permissions", "code"),
    [
        (["events.read", "no.such.right"], "invalid_permissions"),
        ([], "invalid_permissions"),
        (["events.read", "approvals.decide"], "permissions_not_allowed_for_kind"),
        (["admin"], "permissions_not_allowed_for_kind"),
    ],
)
async def test_the_revision_rights_are_checked_again_on_reopening(
    client: httpx.AsyncClient, sync_engine: Engine, permissions: list[str], code: str
) -> None:
    """The catalog and the kind rule, not the escalation rule (TASK-001227).

    The caller is an admin, so only these two checks can refuse; the binding
    stays revoked and the journal is silent.
    """
    admin_key, principal_id, first = await _service(client)
    await _revoke_registry_binding(client, admin_key, principal_id, first)
    _republish_beside(sync_engine, permissions)

    refused = await _link(client, admin_key, agent=KEY, **first)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == code
    binding = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert binding["status"] == "revoked"
    assert await _events(client, admin_key, "iam_binding.updated") == []


async def test_an_active_binding_is_not_checked_again_on_a_repeat(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A repeat on a live binding changes nothing, so it refuses nothing either."""
    admin_key, principal_id, first = await _service(client)
    _republish_beside(sync_engine, ["events.read", "no.such.right"])

    again = await _link(client, admin_key, agent=KEY, **first)
    assert again.status_code == 200, again.text
    binding = (await _bindings(client, admin_key, principal_id))[first["iamPrincipalId"]]
    assert binding["status"] == "active"
    assert binding["permissions"] == ["events.read", "tasks.read"]


async def test_a_principal_outside_the_registry_still_takes_new_identities(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    operator_key = await _operator(client, admin_key)
    other = await create_principal(client, admin_key, kind="service", name="other")

    first = await _upsert(client, operator_key, other["id"], identity())
    second = await _upsert(client, operator_key, other["id"], identity())
    assert (first.status_code, second.status_code) == (201, 201), second.text


# --- 3. replace leaves one way in -------------------------------------------------


async def test_replace_revokes_every_other_binding_of_the_principal(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, first = await _service(client)
    beside = identity()
    _bind_beside(sync_engine, principal_id, beside)
    new = identity()

    replaced = await _replace(client, admin_key, new)
    assert replaced.status_code == 200, replaced.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert {k: b["status"] for k, b in bindings.items()} == {
        first["iamPrincipalId"]: "revoked",
        beside["iamPrincipalId"]: "revoked",
        new["iamPrincipalId"]: "active",
    }
    revoked = await _events(client, admin_key, "iam_binding.revoked")
    assert sorted(e["payload"]["iamPrincipalId"] for e in revoked) == sorted(
        [first["iamPrincipalId"], beside["iamPrincipalId"]]
    )


async def test_replace_with_a_binding_beside_it_adopts_it_and_revokes_the_rest(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, principal_id, _ = await _service(client)
    adopted, stray = identity(), identity()
    _bind_beside(sync_engine, principal_id, adopted)
    _bind_beside(sync_engine, principal_id, stray)

    replaced = await _replace(client, admin_key, adopted)
    assert replaced.status_code == 200, replaced.text

    bindings = await _bindings(client, admin_key, principal_id)
    assert [k for k, b in bindings.items() if b["status"] == "active"] == [
        adopted["iamPrincipalId"]
    ]
    assert bindings[adopted["iamPrincipalId"]]["permissions"] == ["events.read", "tasks.read"]
    assert len(await _events(client, admin_key, "iam_binding.revoked")) == 2
    # A repeat changes nothing and revokes nothing more.
    again = await _replace(client, admin_key, adopted)
    assert again.status_code == 200, again.text
    assert len(await _events(client, admin_key, "iam_binding.revoked")) == 2


# --- 5. moving an identity needs admin -----------------------------------------------


async def test_only_an_admin_moves_the_identity_of_a_service_outside_the_registry(
    client: httpx.AsyncClient,
) -> None:
    """The probe: a non-admin took the identity of another service — was 200."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    owner = await create_principal(client, admin_key, kind="service", name="owner")
    taker = await create_principal(client, admin_key, kind="agent", name="taker")
    login = identity()
    assert (await _upsert(client, admin_key, owner["id"], login)).status_code == 201
    operator_key = await _operator(client, admin_key)

    refused = await _upsert(client, operator_key, taker["id"], login)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_escalation"
    assert refused.json()["error"]["details"] == {"previousOwnerKind": "service"}
    assert login["iamPrincipalId"] in await _bindings(client, admin_key, owner["id"])

    moved = await _upsert(client, admin_key, taker["id"], login)
    assert moved.status_code == 200, moved.text
    # The same identity on the same principal is not a move: a non-admin re-binds it.
    again = await _upsert(client, operator_key, taker["id"], login)
    assert again.status_code == 200, again.text
