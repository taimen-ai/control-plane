"""Agents' access against a real OpenBao (CP-ADR-0079 §9, §10; I011 acceptance).

Runs when ``CP_TEST_OPENBAO_URL`` and ``CP_TEST_OPENBAO_TOKEN`` are set, as
``test_secret_store_openbao``: the instance should be the image of the
installation (superproject, I006). The test configures it the way bootstrap
does for the core — ``kv/`` as ``kv-v2`` with ``max_versions=1``, the
``jwt`` method, the core's policy with the rights of §1 — with a key it signs
itself in place of the IAM, both for the core and for the agents.

Then the API and the worker do the rest: an agent of the registry names a
connection, the worker writes its policy and role, and the agent, logged in
with a JWT of its IAM subject, reads only the paths of its connections; after
``:revoke`` the first request for material is refused; a repeated pass
changes nothing. I012 adds the agent's secrets by name: the agent reads its
own and not its neighbour's, and after its IAM binding is revoked the token
it already holds reads nothing. A value the names never had (a ``PUT``
whose commit failed) is deleted after ``:retire`` by the store's list. Only
the tenant's sync runs (journal and tenant), not the sweep of orphaned
``cp-agent-*`` policies, which on a shared instance would reach other runs'
policies.
"""

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from control_plane.application.commands import connection_policies
from control_plane.config import Settings
from control_plane.domain.connection_access import agent_policy_name, agent_role_name
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.secret_store import SecretStore
from control_plane.worker.main import Worker
from tests.helpers import auth
from tests.integration.test_connection_policies import (
    AgentIds,
    linked_agent,
    revoke,
    tenant,
    token_connection,
)
from tests.integration.test_secret_store_openbao import (
    AUDIENCE,
    OPENBAO_TOKEN,
    OPENBAO_URL,
    _admin,
    _set_up,
)

pytestmark = pytest.mark.skipif(
    not (OPENBAO_URL and OPENBAO_TOKEN),
    reason="CP_TEST_OPENBAO_URL / CP_TEST_OPENBAO_TOKEN not set (OpenBao required)",
)

ROLE = "cp-test-control-plane-i011"

# The core's policy (CP-ADR-0079 §1, ``control-plane.hcl``) with the rights of §9.
POLICY = """
path "oauth2/servers/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "oauth2/creds/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/data/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/metadata/tenants/*" { capabilities = ["list", "delete"] }
path "kv/data/platform/oauth-apps/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/metadata/platform/oauth-apps/*" { capabilities = ["list", "delete"] }
path "sys/policies/acl" { capabilities = ["list"] }
path "auth/jwt/role" { capabilities = ["list"] }
path "sys/policies/acl/cp-agent-*" { capabilities = ["create", "read", "update", "delete"] }
path "auth/jwt/role/agent-*" { capabilities = ["create", "read", "update", "delete"] }
"""


class Instance:
    def __init__(self, admin: httpx.AsyncClient, key: rsa.RSAPrivateKey) -> None:
        self.admin = admin
        self.key = key

    def jwt(self, claims: dict[str, Any]) -> str:
        now = int(time.time())
        return jwt.encode(
            {"aud": AUDIENCE, "iat": now, "exp": now + 300, **claims}, self.key, algorithm="RS256"
        )

    async def agent_client(self, agent: AgentIds) -> httpx.AsyncClient:
        """The agent logged in with its role and a JWT of its IAM subject."""
        http = httpx.AsyncClient(base_url=f"{OPENBAO_URL}/v1", timeout=10)
        token = self.jwt(
            {
                "sub": agent.iam_principal_id,
                "tenant_id": agent.iam_tenant_id,
                "principal_type": "agent",
            }
        )
        login = await http.post(
            "auth/jwt/login", json={"role": agent_role_name(agent.principal_id), "jwt": token}
        )
        assert login.status_code == 200, login.text
        http.headers["X-Vault-Token"] = login.json()["auth"]["client_token"]
        return http


@pytest.fixture
async def instance(app: FastAPI) -> AsyncIterator[Instance]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    async with httpx.AsyncClient(timeout=10) as admin:
        await _set_up(admin, public_pem)
        policy = await _admin(admin, "PUT", f"sys/policies/acl/{ROLE}", {"policy": POLICY})
        assert policy.status_code in (200, 204), policy.text
        role = await _admin(
            admin,
            "POST",
            f"auth/jwt/role/{ROLE}",
            {
                "role_type": "jwt",
                "bound_audiences": [AUDIENCE],
                "user_claim": "sub",
                "token_policies": [ROLE],
                "token_ttl": "10m",
            },
        )
        assert role.status_code in (200, 204), role.text
        found = Instance(admin, key)

        async def iam_token() -> str:
            return found.jwt({"sub": "control-plane"})

        store = SecretStore(OPENBAO_URL, iam_token, role=ROLE)
        app.state.secret_store = store
        try:
            yield found
        finally:
            app.state.secret_store = None
            await store.aclose()


async def _sync(worker: Worker, tenant_id: uuid.UUID) -> int:
    assert worker.secret_store is not None
    async with transaction(worker.session_factory) as session:
        await connection_policies.ensure_policy_cursor(session, tenant_id)
        stats = await connection_policies.sync_tenant(session, worker.secret_store, tenant_id)
    return stats.changes


async def test_an_agent_reads_only_its_connections_and_not_after_revoke(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, instance: Instance
) -> None:
    admin_key, tenant_id = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    other = await token_connection(client, admin_key, "crm-other")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    worker = Worker(settings, secret_store=app.state.secret_store)
    try:
        assert await _sync(worker, uuid.UUID(tenant_id)) == 2
        # A repeated pass changes nothing.
        assert await _sync(worker, uuid.UUID(tenant_id)) == 0

        http = await instance.agent_client(agent)
        own = await http.get(mine["secretRef"])
        assert own.status_code == 200, own.text
        assert own.json()["data"]["data"]["access_token"].endswith("-crm")
        for ref in (other["secretRef"], "kv/data/platform/oauth-apps/crm", "sys/policies/acl"):
            assert (await http.get(ref)).status_code == 403, ref
        # The role admits only the agent's IAM subject.
        stranger = instance.jwt(
            {"sub": str(uuid.uuid4()), "tenant_id": agent.iam_tenant_id, "principal_type": "agent"}
        )
        async with httpx.AsyncClient(base_url=f"{OPENBAO_URL}/v1", timeout=10) as anonymous:
            refused = await anonymous.post(
                "auth/jwt/login",
                json={"role": agent_role_name(agent.principal_id), "jwt": stranger},
            )
        assert refused.status_code in (400, 403)

        revoked = await revoke(client, admin_key, "crm")
        assert revoked.status_code == 200, revoked.text
        # The first request after the answer, with the token issued before it.
        assert (await http.get(mine["secretRef"])).status_code == 403
        policy = f"sys/policies/acl/{agent_policy_name(agent.principal_id)}"
        assert (await _admin(instance.admin, "GET", policy)).status_code == 404

        # The journal brings a new revision; the worker writes it once.
        republished = await client.post(
            "/api/v1/agents",
            json={
                "key": "connector",
                "spec": {
                    "displayName": "Connector",
                    "identity": {"kind": "agent", "permissions": ["events.read"]},
                    "placement": "none",
                    "connections": ["crm-other"],
                },
            },
            headers=auth(admin_key),
        )
        assert republished.status_code == 201, republished.text
        assert await worker.process_connection_policy_events() > 0
        again = await instance.agent_client(agent)
        assert (await again.get(other["secretRef"])).status_code == 200
        assert await _sync(worker, uuid.UUID(tenant_id)) == 0
    finally:
        await worker.engine.dispose()


async def test_an_agent_reads_its_secret_and_loses_it_with_its_binding(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, instance: Instance
) -> None:
    """I012: the agent's prefix in its policy; a revoked binding takes it away (review of I011)."""
    admin_key, tenant_id = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "runner", ["crm"])
    neighbour = await linked_agent(client, admin_key, "neighbour", [])
    for key, value in (("runner", "mine-" + "S" * 20), ("neighbour", "theirs-" + "S" * 20)):
        put = await client.put(
            f"/api/v1/agents/{key}/secrets/token", json={"value": value}, headers=auth(admin_key)
        )
        assert put.status_code == 201, put.text
        assert value not in put.text
    worker = Worker(settings, secret_store=app.state.secret_store)
    try:
        assert await _sync(worker, uuid.UUID(tenant_id)) == 4
        assert await _sync(worker, uuid.UUID(tenant_id)) == 0

        http = await instance.agent_client(agent)
        own = await http.get(f"kv/data/tenants/{tenant_id}/agents/runner/token")
        assert own.status_code == 200, own.text
        assert own.json()["data"]["data"] == {"value": "mine-" + "S" * 20}
        assert (await http.get(mine["secretRef"])).status_code == 200
        for ref in (
            f"kv/data/tenants/{tenant_id}/agents/neighbour/token",
            f"kv/metadata/tenants/{tenant_id}/agents/runner/token",
        ):
            assert (await http.get(ref)).status_code == 403, ref
        theirs = await instance.agent_client(neighbour)
        assert (
            await theirs.get(f"kv/data/tenants/{tenant_id}/agents/neighbour/token")
        ).status_code == 200
        assert (
            await theirs.get(f"kv/data/tenants/{tenant_id}/agents/runner/token")
        ).status_code == 403

        bindings = await client.get(
            f"/api/v1/principals/{agent.principal_id}/iam-bindings", headers=auth(admin_key)
        )
        [binding] = bindings.json()["items"]
        revoked = await client.post(
            f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key)
        )
        assert revoked.status_code == 200, revoked.text
        assert await worker.process_connection_policy_events() > 0
        # The token issued before the revocation reads nothing any more.
        for ref in (f"kv/data/tenants/{tenant_id}/agents/runner/token", mine["secretRef"]):
            assert (await http.get(ref)).status_code == 403, ref
        policy = f"sys/policies/acl/{agent_policy_name(agent.principal_id)}"
        assert (await _admin(instance.admin, "GET", policy)).status_code == 404
        role = f"auth/jwt/role/{agent_role_name(agent.principal_id)}"
        assert (await _admin(instance.admin, "GET", role)).status_code == 404
        # The neighbour keeps its own.
        assert (
            await theirs.get(f"kv/data/tenants/{tenant_id}/agents/neighbour/token")
        ).status_code == 200
    finally:
        await worker.engine.dispose()


async def test_retire_deletes_a_value_the_names_never_had(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, instance: Instance
) -> None:
    """Tail of I012: the store's list decides what a retired agent leaves, not the names."""
    admin_key, tenant_id = await tenant(client)
    await linked_agent(client, admin_key, "runner", [])
    put = await client.put(
        "/api/v1/agents/runner/secrets/token",
        json={"value": "named-" + "S" * 20},
        headers=auth(admin_key),
    )
    assert put.status_code == 201, put.text
    # A PUT whose commit failed: the value went to the store, the name was rolled back.
    store = app.state.secret_store
    stray = f"tenants/{tenant_id}/agents/runner/stray"
    await store.kv_write(stray, {"value": "unnamed-" + "S" * 20})
    assert await store.kv_list(f"tenants/{tenant_id}/agents/runner") == ["stray", "token"]
    worker = Worker(settings, secret_store=store)
    try:
        await _sync(worker, uuid.UUID(tenant_id))
        retired = await client.post(
            "/api/v1/agents/runner:retire", json={"reason": "replaced"}, headers=auth(admin_key)
        )
        assert retired.status_code == 200, retired.text
        assert await worker.process_connection_policy_events() > 0
        for name in ("stray", "token"):
            path = f"tenants/{tenant_id}/agents/runner/{name}"
            assert (await _admin(instance.admin, "GET", f"kv/data/{path}")).status_code == 404
            assert (await _admin(instance.admin, "GET", f"kv/metadata/{path}")).status_code == 404
        assert await store.kv_list(f"tenants/{tenant_id}/agents") == []
        listed = await client.get("/api/v1/agents/runner/secrets", headers=auth(admin_key))
        assert listed.json() == {"items": []}
        assert await _sync(worker, uuid.UUID(tenant_id)) == 0
    finally:
        await worker.engine.dispose()
