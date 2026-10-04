"""Agents' access in the secret store, ``:revoke`` and ``Agent.spec.connections``.

CP-ADR-0079 §8-§10, integrations-connections I011. The store is
:class:`tests.fake_openbao.FakeOpenBao` behind the real client: the worker
``connections-policy-sync`` writes the policies ``cp-agent-<principal>`` and
the ``jwt`` roles ``agent-<principal>`` there, an agent logs in with its role
and a JWT of its IAM subject, and the fake checks each of the agent's
requests against the text of its policies at that moment — the way OpenBao
applies a policy rewritten after the login.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.connection_access import agent_policy_name, agent_role_name
from control_plane.infrastructure.secret_store import SecretStore
from control_plane.worker.main import Worker
from tests.fake_openbao import BASE_URL, FakeOpenBao
from tests.helpers import auth, do_bootstrap, make_tenant_directly
from tests.integration.test_agent_registry import ISSUER
from tests.integration.test_connection_access import (
    ACCOUNT,
    _IamToken,
    callback,
    human,
    outcome,
    set_app,
    started_state,
)
from tests.integration.test_connection_types import publish
from tests.integration.test_connections import CONNECTIONS, create, events

AGENTS = "/api/v1/agents"
MATERIAL = "key-" + "M" * 30


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "oauth_redirect_uri": "https://cp.example/api/v1/connections:callback",
            "connections_return_url": "https://console.example/connections",
        }
    )


@dataclass
class Store:
    bao: FakeOpenBao
    client: SecretStore


@pytest.fixture
async def store(app: FastAPI) -> AsyncIterator[Store]:
    fake = FakeOpenBao()
    iam = _IamToken(fake)
    client = SecretStore(BASE_URL, iam, forget_token=iam.forget, client=fake.client())
    app.state.secret_store = client
    yield Store(fake, client)
    app.state.secret_store = None


@pytest.fixture
async def worker(settings: Settings, store: Store) -> AsyncIterator[Worker]:
    w = Worker(settings, secret_store=store.client)
    try:
        yield w
    finally:
        await w.engine.dispose()


# --- helpers -------------------------------------------------------------------------


def agent_spec(connections: list[str] | None = None, **overrides: Any) -> dict[str, Any]:
    """An agent that takes no work and runs no process: identity and connections only."""
    spec: dict[str, Any] = {
        "displayName": "Connector",
        "identity": {"kind": "agent", "permissions": ["events.read"]},
        "placement": "none",
    }
    if connections is not None:
        spec["connections"] = connections
    spec.update(overrides)
    return spec


async def publish_agent(
    client: httpx.AsyncClient, key: str, agent: str, spec: dict[str, Any]
) -> httpx.Response:
    return await client.post(AGENTS, json={"key": agent, "spec": spec}, headers=auth(key))


@dataclass(frozen=True)
class AgentIds:
    key: str
    principal_id: uuid.UUID
    iam_principal_id: str
    iam_tenant_id: str


async def linked_agent(
    client: httpx.AsyncClient, admin_key: str, agent: str, connections: list[str]
) -> AgentIds:
    published = await publish_agent(client, admin_key, agent, agent_spec(connections))
    assert published.status_code == 201, published.text
    iam_principal, iam_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    linked = await client.put(
        f"{AGENTS}/{agent}/identity",
        json={"issuer": ISSUER, "iamTenantId": iam_tenant, "iamPrincipalId": iam_principal},
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    return AgentIds(agent, uuid.UUID(linked.json()["principalId"]), iam_principal, iam_tenant)


async def token_connection(client: httpx.AsyncClient, admin_key: str, key: str) -> dict[str, Any]:
    """An ``active`` connection ``key`` of type ``crm`` connected with a key."""
    created = await create(client, admin_key, key=key)
    assert created.status_code == 201, created.text
    put = await client.put(
        f"{CONNECTIONS}/{key}/token",
        json={"account": ACCOUNT, "token": f"{MATERIAL}-{key}"},
        headers=auth(admin_key),
    )
    assert put.status_code == 200, put.text
    body: dict[str, Any] = put.json()
    return body


async def tenant(client: httpx.AsyncClient) -> tuple[str, str]:
    """(admin key, tenant id) with the connection type ``crm``."""
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    assert (await publish(client, admin_key)).status_code == 201
    return admin_key, admin["tenant"]["id"]


async def revoke(
    client: httpx.AsyncClient, key: str, connection: str, **body: Any
) -> httpx.Response:
    return await client.post(f"{CONNECTIONS}/{connection}:revoke", json=body, headers=auth(key))


async def agent_login(bao: FakeOpenBao, agent: AgentIds) -> httpx.AsyncClient:
    """The agent's own client of the store: logged in with its role and IAM JWT."""
    http = bao.client()
    login = await http.post(
        f"{BASE_URL}/v1/auth/jwt/login",
        json={
            "role": agent_role_name(agent.principal_id),
            "jwt": bao.agent_jwt(agent.iam_principal_id, agent.iam_tenant_id),
        },
    )
    assert login.status_code == 200, login.text
    http.headers["X-Vault-Token"] = login.json()["auth"]["client_token"]
    return http


async def read(http: httpx.AsyncClient, ref: str) -> httpx.Response:
    return await http.get(f"{BASE_URL}/v1/{ref}")


def policy_writes(bao: FakeOpenBao) -> list[tuple[str, str]]:
    return bao.writes("sys/policies/acl", "auth/jwt/role")


# --- Agent.spec.connections (§8) -----------------------------------------------------


async def test_naming_connections_needs_connections_manage_of_who_applies(
    client: httpx.AsyncClient,
) -> None:
    admin_key, _tenant = await tenant(client)
    _id, _key_id, narrow = await human(
        client, admin_key, "Installer", ["agents.manage", "agents.read", "events.read"]
    )
    for path in (AGENTS, f"{AGENTS}:validate"):
        denied = await client.post(
            path, json={"key": "connector", "spec": agent_spec(["crm"])}, headers=auth(narrow)
        )
        assert denied.status_code == 403, denied.text
        error = denied.json()["error"]
        assert error["code"] == "permission_escalation"
        assert error["details"] == {"missing": ["connections.manage"], "path": "spec.connections"}
    # An empty list gives nothing and needs nothing.
    empty = await publish_agent(client, narrow, "connector", agent_spec([]))
    assert empty.status_code == 201, empty.text
    # Whoever may manage connections names them; the field is in the revision.
    named = await publish_agent(client, admin_key, "connector", agent_spec(["crm", "tracker"]))
    assert named.status_code == 201, named.text
    assert named.json()["revision"]["spec"]["connections"] == ["crm", "tracker"]
    # Re-applying the same list is checked again (the presence, not the difference).
    again = await publish_agent(client, narrow, "connector", agent_spec(["crm", "tracker"]))
    assert again.status_code == 403


async def test_a_spec_without_the_field_hashes_as_before(client: httpx.AsyncClient) -> None:
    admin_key, _tenant = await tenant(client)
    first = await publish_agent(client, admin_key, "connector", agent_spec())
    assert first.status_code == 201, first.text
    assert "connections" not in first.json()["revision"]["spec"]
    with_empty = await publish_agent(client, admin_key, "connector", agent_spec([]))
    assert with_empty.json()["currentRevision"] == 2
    assert with_empty.json()["revision"]["specHash"] != first.json()["revision"]["specHash"]


@pytest.mark.parametrize(
    ("connections", "location"),
    [
        (["crm", "crm"], "connections"),
        (["CRM"], "connections"),
        (["-crm"], "connections"),
        (["a" * 64], "connections"),
        ([f"c{index}" for index in range(21)], "connections"),
        (None, "connections"),
        ("crm", "connections"),
        ([1], "connections"),
    ],
)
async def test_the_list_is_up_to_twenty_unique_keys(
    client: httpx.AsyncClient, connections: Any, location: str
) -> None:
    admin_key, _tenant = await tenant(client)
    spec = agent_spec()
    spec["connections"] = connections
    response = await publish_agent(client, admin_key, "connector", spec)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert location in response.text


async def test_an_agent_reads_only_the_connections_its_revision_names(
    client: httpx.AsyncClient,
) -> None:
    admin_key, _tenant = await tenant(client)
    for key in ("crm", "crm-other"):
        assert (await create(client, admin_key, key=key)).status_code == 201
    agent = await linked_agent(client, admin_key, "connector", ["crm", "crm-missing"])
    response = await client.post(
        f"/api/v1/principals/{agent.principal_id}/api-keys",
        json={"permissions": ["events.read"]},
        headers=auth(admin_key),
    )
    agent_key = response.json()["key"]

    listed = await client.get(f"{AGENTS}/me/connections", headers=auth(agent_key))
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item == {
        "key": "crm",
        "type": "crm",
        "typeVersion": 1,
        "account": None,
        "auth": None,
        "status": "pending",
        "settings": {},
        "secretRef": None,
        "expiresAt": None,
    }
    one = await client.get(f"{AGENTS}/me/connections/crm", headers=auth(agent_key))
    assert one.status_code == 200 and one.json() == item
    # Another connection of the tenant and a missing one answer alike.
    for key in ("crm-other", "crm-missing", "nothing"):
        hidden = await client.get(f"{AGENTS}/me/connections/{key}", headers=auth(agent_key))
        assert hidden.status_code == 404, key
        assert hidden.json()["error"]["details"] == {"connection": key}
    # Who is not an agent of the registry is not found, as at /agents/me.
    for path in ("me/connections", "me/connections/crm"):
        stranger = await client.get(f"{AGENTS}/{path}", headers=auth(admin_key))
        assert stranger.status_code == 404


# --- the worker connections-policy-sync (§9) -----------------------------------------


async def test_an_agent_reads_its_connections_and_nothing_else(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, tenant_id = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    other = await token_connection(client, admin_key, "crm-other")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    neighbour = await linked_agent(client, admin_key, "neighbour", ["crm-other"])

    assert await worker.sync_connection_policies(force=True) == 4
    bao = store.bao
    assert bao.policies[agent_policy_name(agent.principal_id)] == (
        f'path "{mine["secretRef"]}" {{ capabilities = ["read"] }}\n'
    )
    role = bao.roles[agent_role_name(agent.principal_id)]
    assert role == {
        "role_type": "jwt",
        "user_claim": "sub",
        "bound_subject": agent.iam_principal_id,
        "bound_claims_type": "string",
        "bound_claims": {"tenant_id": agent.iam_tenant_id, "principal_type": "agent"},
        "bound_audiences": ["openbao"],
        "token_policies": [agent_policy_name(agent.principal_id)],
        "token_no_default_policy": True,
        "token_ttl": 300,
        "token_max_ttl": 300,
    }

    http = await agent_login(bao, agent)
    own = await read(http, mine["secretRef"])
    assert own.status_code == 200
    assert own.json()["data"]["data"]["access_token"] == f"{MATERIAL}-crm"
    # The neighbour's connection, another tenant's path, the core's paths: refused.
    for ref in (
        other["secretRef"],
        f"kv/data/tenants/{uuid.uuid4()}/connections/crm",
        "kv/data/platform/oauth-apps/crm",
        f"kv/data/tenants/{tenant_id}/agents/connector/secret",
    ):
        assert (await read(http, ref)).status_code == 403, ref
    neighbour_http = await agent_login(bao, neighbour)
    assert (await read(neighbour_http, other["secretRef"])).status_code == 200
    assert (await read(neighbour_http, mine["secretRef"])).status_code == 403

    # The role is bound to the agent's IAM subject, tenant, kind and audience.
    for jwt in (
        bao.agent_jwt(neighbour.iam_principal_id, agent.iam_tenant_id),
        bao.agent_jwt(agent.iam_principal_id, neighbour.iam_tenant_id),
        bao.agent_jwt(agent.iam_principal_id, agent.iam_tenant_id, principal_type="human"),
        bao.agent_jwt(agent.iam_principal_id, agent.iam_tenant_id, audience="control-plane"),
    ):
        refused = await bao.client().post(
            f"{BASE_URL}/v1/auth/jwt/login",
            json={"role": agent_role_name(agent.principal_id), "jwt": jwt},
        )
        assert refused.status_code == 400

    # A repeated pass changes nothing: it reads and does not write.
    bao.requests.clear()
    assert await worker.sync_connection_policies(force=True) == 0
    assert policy_writes(bao) == []
    assert bao.paths("GET")


async def test_only_active_connections_of_linked_agents_get_a_policy(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    active = await token_connection(client, admin_key, "crm")
    assert (await create(client, admin_key, key="crm-pending")).status_code == 201
    expired = await token_connection(client, admin_key, "crm-expired")
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE connections SET status = 'expired' WHERE key = 'crm-expired'"),
        )
    both = await linked_agent(
        client, admin_key, "both", ["crm", "crm-pending", "crm-expired", "crm-missing"]
    )
    nothing = await linked_agent(client, admin_key, "nothing", ["crm-pending", "crm-expired"])
    # Published, never linked: no principal, nothing to bind a role to.
    unlinked = await publish_agent(client, admin_key, "unlinked", agent_spec(["crm"]))
    assert unlinked.status_code == 201
    # A service account names the connection: the agents' role does not admit it.
    service = await publish_agent(
        client,
        admin_key,
        "service",
        agent_spec(["crm"], identity={"kind": "service", "permissions": ["events.read"]}),
    )
    assert service.status_code == 201, service.text
    service_linked = await client.put(
        f"{AGENTS}/service/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(admin_key),
    )
    assert service_linked.status_code == 200, service_linked.text

    await worker.sync_connection_policies(force=True)
    bao = store.bao
    assert set(bao.policies) == {agent_policy_name(both.principal_id)}
    assert set(bao.roles) == {agent_role_name(both.principal_id)}
    assert bao.policies[agent_policy_name(both.principal_id)] == (
        f'path "{active["secretRef"]}" {{ capabilities = ["read"] }}\n'
    )
    assert expired["secretRef"] not in bao.policies[agent_policy_name(both.principal_id)]
    assert agent_policy_name(nothing.principal_id) not in bao.policies


async def test_events_bring_the_policies_to_the_records(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    first = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    bao = store.bao
    policy = agent_policy_name(agent.principal_id)
    assert first["secretRef"] in bao.policies[policy]

    # A new connection becomes active and a revision names it: the journal
    # brings it without waiting for the next full pass.
    second = await token_connection(client, admin_key, "crm-second")
    republished = await publish_agent(
        client, admin_key, "connector", agent_spec(["crm", "crm-second"])
    )
    assert republished.status_code == 201, republished.text
    assert (await worker.run_once())["connection_policy_events_read"] > 0
    assert second["secretRef"] in bao.policies[policy]
    assert first["secretRef"] in bao.policies[policy]

    # A revision that names none: the policy and the role go.
    assert (await publish_agent(client, admin_key, "connector", agent_spec([]))).status_code == 201
    await worker.run_once()
    assert policy not in bao.policies
    assert agent_role_name(agent.principal_id) not in bao.roles

    # Named again, then retired: gone again.
    await publish_agent(client, admin_key, "connector", agent_spec(["crm"]))
    await worker.run_once()
    assert policy in bao.policies
    retired = await client.post(
        f"{AGENTS}/connector:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    await worker.run_once()
    assert policy not in bao.policies
    assert agent_role_name(agent.principal_id) not in bao.roles

    # Nothing else happened: the journal has nothing to apply, nothing is written.
    bao.requests.clear()
    stats = await worker.run_once()
    assert stats["connection_policy_events_read"] == 0
    assert policy_writes(bao) == []


async def test_a_role_changed_in_the_store_is_written_back(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    bao = store.bao
    bao.roles[agent_role_name(agent.principal_id)]["token_ttl"] = 86_400
    bao.roles[agent_role_name(agent.principal_id)]["bound_subject"] = str(uuid.uuid4())
    bao.policies[agent_policy_name(agent.principal_id)] += (
        'path "kv/data/*" { capabilities = ["read"] }\n'
    )
    bao.requests.clear()
    assert await worker.sync_connection_policies(force=True) == 2
    assert bao.roles[agent_role_name(agent.principal_id)]["token_ttl"] == 300
    assert bao.roles[agent_role_name(agent.principal_id)]["bound_subject"] == agent.iam_principal_id
    assert bao.policies[agent_policy_name(agent.principal_id)] == (
        f'path "{mine["secretRef"]}" {{ capabilities = ["read"] }}\n'
    )


async def test_the_full_pass_deletes_policies_no_agent_has(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    bao = store.bao
    orphan = uuid.uuid4()
    bao.policies[agent_policy_name(orphan)] = 'path "kv/data/x" { capabilities = ["read"] }\n'
    bao.roles[agent_role_name(orphan)] = {"token_policies": [agent_policy_name(orphan)]}
    # Names outside the agents' pattern are not the worker's.
    bao.policies["cp-agent-not-a-uuid"] = "#\n"
    bao.policies["control-plane"] = "#\n"
    bao.roles["agent-x"] = {}
    await worker.sync_connection_policies(force=True)
    assert set(bao.policies) == {
        agent_policy_name(agent.principal_id),
        "cp-agent-not-a-uuid",
        "control-plane",
    }
    assert set(bao.roles) == {agent_role_name(agent.principal_id), "agent-x"}


async def test_the_full_pass_is_periodic(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    await linked_agent(client, admin_key, "connector", ["crm"])
    assert (await worker.run_once())["connection_policy_changes"] == 2
    store.bao.policies.clear()
    # Before the period ends no full pass: the journal has nothing new either.
    assert (await worker.run_once())["connection_policy_changes"] == 0
    assert store.bao.policies == {}
    worker._next_policy_pass = 0.0
    assert (await worker.run_once())["connection_policy_changes"] == 1


async def test_an_unavailable_store_holds_the_tenant_back_and_the_worker_goes_on(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    await worker.sync_connection_policies(force=True)
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    store.bao.sealed = True
    stats = await worker.run_once()
    assert stats["connection_policy_events_read"] == 0
    with sync_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT failure_count, parked_reason, next_attempt_at FROM event_consumer_cursors"
                " WHERE name = 'connections-policy-sync'"
            )
        ).one()
    assert row.failure_count == 1
    assert row.parked_reason == "sealed"
    assert row.next_attempt_at is not None
    store.bao.sealed = False
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE event_consumer_cursors SET next_attempt_at = now()"))
    assert (await worker.run_once())["connection_policy_events_read"] > 0
    assert agent_policy_name(agent.principal_id) in store.bao.policies
    with sync_engine.begin() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT failure_count FROM event_consumer_cursors"
                    " WHERE name = 'connections-policy-sync'"
                )
            ).scalar_one()
            == 0
        )


async def test_an_expired_key_leaves_the_policy_with_its_event(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE connections SET expires_at = :at WHERE key = 'crm'"),
            {"at": datetime.now(UTC) - timedelta(seconds=1)},
        )
    first = await worker.run_once()
    assert first["connection_keys_expired"] == 1
    [changed] = await events(client, admin_key, "connection.status_changed")
    assert changed["actorId"] is None
    assert changed["payload"] == {
        "key": "crm",
        "type": "crm",
        "from": "active",
        "to": "expired",
        "reason": "token_expired",
        "connectedBy": mine["connectedBy"],
    }
    await worker.run_once()
    assert agent_policy_name(agent.principal_id) not in store.bao.policies
    assert (await worker.run_once())["connection_keys_expired"] == 0


# --- :revoke (§10) -------------------------------------------------------------------------


async def test_after_revoke_the_next_request_for_material_is_refused(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    other = await token_connection(client, admin_key, "crm-other")
    agent = await linked_agent(client, admin_key, "connector", ["crm", "crm-other"])
    await worker.sync_connection_policies(force=True)
    bao = store.bao
    http = await agent_login(bao, agent)
    assert (await read(http, mine["secretRef"])).status_code == 200

    revoked = await revoke(client, admin_key, "crm", reason="left the company")
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == "revoked"
    assert body["auth"] is None and body["secretRef"] is None and body["expiresAt"] is None
    assert body["statusMessage"] == "left the company"
    assert body["account"] == ACCOUNT
    # The first request after the answer, with a token issued before: refused.
    assert (await read(http, mine["secretRef"])).status_code == 403
    assert (await read(http, other["secretRef"])).status_code == 200
    # Not readable under any version either: the document is gone with its versions.
    assert ("DELETE", f"kv/metadata/{mine['secretRef'].removeprefix('kv/data/')}") in bao.writes()
    assert mine["secretRef"].removeprefix("kv/data/") not in bao.kv
    [event] = await events(client, admin_key, "connection.revoked")
    assert event["payload"] == {"key": "crm", "type": "crm", "previousStatus": "active"}
    # The worker afterwards finds everything in step.
    bao.requests.clear()
    await worker.sync_connection_policies(force=True)
    assert policy_writes(bao) == []

    # Revoking again: 200, nothing recorded, nothing asked of the store.
    bao.requests.clear()
    again = await revoke(client, admin_key, "crm")
    assert again.status_code == 200 and again.json()["version"] == body["version"]
    assert bao.requests == []
    assert len(await events(client, admin_key, "connection.revoked")) == 1


async def test_revoking_the_last_connection_withdraws_the_role(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    assert (await revoke(client, admin_key, "crm")).status_code == 200
    assert agent_policy_name(agent.principal_id) not in store.bao.policies
    assert agent_role_name(agent.principal_id) not in store.bao.roles


async def test_revoke_of_an_oauth_connection_deletes_creds_and_server(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await create(client, admin_key)).status_code == 201
    assert (await set_app(client, admin_key)).status_code == 200
    bao = store.bao
    state = await started_state(client, admin_key)
    token_url = "https://acme.crm.example/oauth2/access_token"
    code = bao.token_server.issue(token_url)
    assert outcome(await callback(client, state=state, code=code, referer=ACCOUNT))["result"] == [
        "active"
    ]
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    [creds] = bao.creds
    [server] = bao.servers
    http = await agent_login(bao, agent)
    assert (await read(http, f"oauth2/creds/{creds}")).status_code == 200
    # A live state of a later attempt is put out by the revocation.
    later = await started_state(client, admin_key)

    assert (await revoke(client, admin_key, "crm")).status_code == 200
    assert bao.creds == {} and bao.servers == {}
    assert ("DELETE", f"oauth2/servers/{server}") in bao.writes()
    assert (await read(http, f"oauth2/creds/{creds}")).status_code == 403
    with sync_engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM connection_oauth_states WHERE consumed_at IS NULL")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT oauth_server FROM connections WHERE key = 'crm'")
            ).scalar_one()
            is None
        )
    late = outcome(await callback(client, state=later, code=bao.token_server.issue(token_url)))
    assert late["result"] == ["invalid_state"]
    # Connected again under the same key: the agent reads again after the sync.
    again = await client.put(
        f"{CONNECTIONS}/crm/token",
        json={"account": ACCOUNT, "token": MATERIAL},
        headers=auth(admin_key),
    )
    assert again.status_code == 200, again.text
    await worker.run_once()
    assert (await read(http, again.json()["secretRef"])).status_code == 200


async def test_a_store_failure_changes_no_status_and_a_repeat_finishes(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, _tenant = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    bao = store.bao
    http = await agent_login(bao, agent)
    # The material, then the role, then the policy fail in turn.
    for method, prefix in (
        ("DELETE", "kv/metadata/"),
        ("DELETE", "auth/jwt/role/"),
        ("GET", "sys/policies/acl/"),
    ):
        bao.fail(method, prefix, 503)
        failed = await revoke(client, admin_key, "crm")
        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["code"] == "secret_store_unavailable"
        assert failed.json()["error"]["details"]["retryable"] is True
        card = await client.get(f"{CONNECTIONS}/crm", headers=auth(admin_key))
        assert card.json()["status"] == "active"
        assert card.json()["secretRef"] == mine["secretRef"]
        assert await events(client, admin_key, "connection.revoked") == []
    # The material went at the first successful step; the policy stayed until now.
    assert (await read(http, mine["secretRef"])).status_code == 404
    done = await revoke(client, admin_key, "crm")
    assert done.status_code == 200, done.text
    assert (await read(http, mine["secretRef"])).status_code == 403
    assert len(await events(client, admin_key, "connection.revoked")) == 1


async def test_revoke_without_a_store(
    client: httpx.AsyncClient, app: FastAPI, store: Store
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    assert (await create(client, admin_key, key="crm-pending")).status_code == 201
    app.state.secret_store = None
    # Material without the store to delete it from: nothing changes.
    unavailable = await revoke(client, admin_key, "crm")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["details"]["reason"] == "not_configured"
    # A connection that never had material is revoked in the records alone.
    pending = await revoke(client, admin_key, "crm-pending")
    assert pending.status_code == 200, pending.text
    [event] = await events(client, admin_key, "connection.revoked")
    assert event["payload"]["previousStatus"] == "pending"


async def test_revoke_has_its_right_and_tenant(
    client: httpx.AsyncClient, store: Store, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    _id, _key_id, reader = await human(client, admin_key, "Reader", ["connections.read"])
    denied = await revoke(client, reader, "crm")
    assert denied.status_code == 403
    assert (await revoke(client, admin_key, "missing")).status_code == 404
    _other_tenant, other_key = make_tenant_directly(sync_engine, "other")
    foreign = await revoke(client, other_key, "crm")
    assert foreign.status_code == 404, foreign.text
    assert store.bao.writes("kv/metadata/") == []
    too_long = await revoke(client, admin_key, "crm", reason="x" * 501)
    assert too_long.status_code == 400


async def test_the_reason_keeps_no_credential(client: httpx.AsyncClient, store: Store) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    leaked = "ghp_" + "A" * 36
    response = await revoke(client, admin_key, "crm", reason=f"leaked {leaked} in a chat")
    assert response.status_code == 200, response.text
    assert leaked not in response.text
    assert leaked not in str(await events(client, admin_key, "connection.revoked"))


async def test_parallel_revocations_record_one_event(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    responses = await asyncio.gather(*(revoke(client, admin_key, "crm") for _ in range(4)))
    assert [r.status_code for r in responses] == [200] * 4
    assert len(await events(client, admin_key, "connection.revoked")) == 1


async def test_an_idempotency_key_replays_the_revocation(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    headers = {**auth(admin_key), "Idempotency-Key": "revoke-1"}
    first = await client.post(f"{CONNECTIONS}/crm:revoke", json={}, headers=headers)
    second = await client.post(f"{CONNECTIONS}/crm:revoke", json={}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    without_body = await client.post(f"{CONNECTIONS}/crm:revoke", headers=auth(admin_key))
    assert without_body.status_code == 200, without_body.text


async def test_the_sync_adds_no_revision(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    """The card of the connection names the agent; the sync publishes nothing."""
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    await linked_agent(client, admin_key, "connector", ["crm"])
    await worker.sync_connection_policies(force=True)
    card = await client.get(f"{CONNECTIONS}/crm", headers=auth(admin_key))
    assert card.json()["agents"] == ["connector"]
    agent = await client.get(f"{AGENTS}/connector", headers=auth(admin_key))
    assert agent.json()["currentRevision"] == 1
