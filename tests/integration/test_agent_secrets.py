"""An agent's secrets by name and the agents' access after their binding goes.

CP-ADR-0079 §9, §11, integrations-connections I012. The store is
:class:`tests.fake_openbao.FakeOpenBao` behind the real client, as in
``test_connection_policies``: the value of ``PUT /agents/{key}/secrets/{name}``
lands there and nowhere else; the worker puts the agent's prefix into its
policy, and the agent reads its own secret with its own token and not another
agent's. Revoking or disabling the agent's IAM binding, or disabling its
principal, takes the store's access away with the next sync (review of I011).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands import connection_policies
from control_plane.config import Settings
from control_plane.domain.connection_access import agent_policy_name, agent_role_name
from control_plane.infrastructure.secret_store import SecretStore
from control_plane.worker.main import Worker
from tests.fake_openbao import BASE_URL, FakeOpenBao
from tests.helpers import auth, make_tenant_directly
from tests.integration.test_agent_registry import ISSUER
from tests.integration.test_connection_access import _IamToken, dump_database, human
from tests.integration.test_connection_policies import (
    AGENTS,
    AgentIds,
    Store,
    agent_login,
    agent_spec,
    linked_agent,
    policy_writes,
    publish_agent,
    read,
    revoke,
    tenant,
    token_connection,
)
from tests.integration.test_connections import events

VALUE = "agent-secret-" + "V" * 30


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


def secret_url(agent: str, name: str) -> str:
    return f"{AGENTS}/{agent}/secrets/{name}"


async def put_secret(
    client: httpx.AsyncClient,
    key: str,
    agent: str,
    name: str,
    value: Any = VALUE,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.put(
        secret_url(agent, name), json={"value": value}, headers={**auth(key), **(headers or {})}
    )


def kv_path(tenant_id: str, agent: str, name: str) -> str:
    return f"tenants/{tenant_id}/agents/{agent}/{name}"


def secret_ref(tenant_id: str, agent: str, name: str) -> str:
    return f"kv/data/{kv_path(tenant_id, agent, name)}"


def prefix_rule(tenant_id: str, agent: str) -> str:
    return f'path "kv/data/tenants/{tenant_id}/agents/{agent}/*" {{ capabilities = ["read"] }}\n'


# --- PUT, GET, DELETE (§11) ------------------------------------------------------------


async def test_put_sends_the_value_to_the_store_and_keeps_the_name(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201

    created = await put_secret(client, admin_key, "runner", "gh-token")
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"name", "updatedAt", "updatedBy"}
    assert body["name"] == "gh-token"
    assert VALUE not in created.text
    bao = store.bao
    doc = bao.kv[kv_path(tenant_id, "runner", "gh-token")]
    assert doc.data == {"value": VALUE}

    replaced = await put_secret(client, admin_key, "runner", "gh-token", value="second-" + VALUE)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["updatedAt"] >= body["updatedAt"]
    assert bao.kv[kv_path(tenant_id, "runner", "gh-token")].data == {"value": "second-" + VALUE}
    assert (await put_secret(client, admin_key, "runner", "npm")).status_code == 201

    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert listed.status_code == 200, listed.text
    assert [item["name"] for item in listed.json()["items"]] == ["gh-token", "npm"]
    assert all(set(item) == {"name", "updatedAt", "updatedBy"} for item in listed.json()["items"])
    assert VALUE not in listed.text

    recorded = await events(client, admin_key, "agent.secret_set")
    assert sorted((e["payload"]["name"], e["payload"]["created"]) for e in recorded) == [
        ("gh-token", False),
        ("gh-token", True),
        ("npm", True),
    ]
    assert all(e["entityType"] == "agent" for e in recorded)
    assert all(set(e["payload"]) == {"agentKey", "name", "created"} for e in recorded)


async def test_the_name_and_the_value_have_their_form(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    for name in ("Upper", "-dash", "a" * 64, "dot.name", "under_score", "%2e%2e"):
        refused = await put_secret(client, admin_key, "runner", name)
        assert refused.status_code == 422, (name, refused.text)
        assert refused.json()["error"]["code"] == "invalid_secret_name"
    assert (await put_secret(client, admin_key, "runner", "a" * 63)).status_code == 201
    for value in ("", None, 42, ["x"], "x" * 65_537):
        refused = await put_secret(client, admin_key, "runner", "other", value=value)
        assert refused.status_code == 400, (value, refused.text)
    assert (
        await put_secret(client, admin_key, "runner", "big", value="x" * 65_536)
    ).status_code == (201)
    no_body = await client.put(secret_url("runner", "x"), headers=auth(admin_key))
    assert no_body.status_code == 400
    extra = await client.put(
        secret_url("runner", "x"), json={"value": "v", "name": "y"}, headers=auth(admin_key)
    )
    assert extra.status_code == 400
    # Nothing of the refused requests reached the store.
    assert sorted(path.rsplit("/", 1)[-1] for path in store.bao.kv) == ["a" * 63, "big"]


async def test_unknown_retired_and_foreign_agents(
    client: httpx.AsyncClient, store: Store, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    missing = await put_secret(client, admin_key, "ghost", "token")
    assert missing.status_code == 404
    assert (await client.get(f"{AGENTS}/ghost/secrets", headers=auth(admin_key))).status_code == 404
    assert (
        await client.delete(secret_url("ghost", "token"), headers=auth(admin_key))
    ).status_code == 404

    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    retired = await client.post(
        f"{AGENTS}/runner:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    refused = await put_secret(client, admin_key, "runner", "token")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "agent_retired"

    # Another tenant's agent is not found, as an unknown one.
    other_tenant, other_key = make_tenant_directly(sync_engine, "other")
    assert (await publish_agent(client, other_key, "theirs", agent_spec())).status_code == 201
    assert (await put_secret(client, other_key, "theirs", "token")).status_code == 201
    for response in (
        await put_secret(client, admin_key, "theirs", "token"),
        await client.get(f"{AGENTS}/theirs/secrets", headers=auth(admin_key)),
        await client.delete(secret_url("theirs", "token"), headers=auth(admin_key)),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"
    assert store.bao.writes("kv/") == [
        ("POST", f"kv/data/tenants/{other_tenant}/agents/theirs/token")
    ]


async def test_rights(client: httpx.AsyncClient, store: Store) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    _p, _k, manager = await human(client, admin_key, "Manager", ["agents.manage", "agents.read"])
    _p, _k, reader = await human(client, admin_key, "Reader", ["agents.read"])
    _p, _k, setter = await human(client, admin_key, "Setter", ["agents.secrets.manage"])
    _p, _k, nobody = await human(client, admin_key, "Nobody", ["events.read"])

    # agents.manage does not include the secrets: they are a right of their own.
    for key in (manager, reader, nobody):
        denied = await put_secret(client, key, "runner", "token")
        assert denied.status_code == 403, denied.text
    assert (await put_secret(client, setter, "runner", "token")).status_code == 201
    for key in (manager, reader, nobody):
        denied = await client.delete(secret_url("runner", "token"), headers=auth(key))
        assert denied.status_code == 403
    # The names: agents.read, as fleet-controller has it.
    assert (await client.get(f"{AGENTS}/runner/secrets", headers=auth(reader))).status_code == 200
    assert (await client.get(f"{AGENTS}/runner/secrets", headers=auth(nobody))).status_code == 403
    assert (await client.get(f"{AGENTS}/runner/secrets", headers=auth(setter))).status_code == 403
    assert (
        await client.delete(secret_url("runner", "token"), headers=auth(setter))
    ).status_code == (204)


async def test_without_a_store_nothing_is_recorded(
    client: httpx.AsyncClient, store: Store, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    store.bao.sealed = True
    refused = await put_secret(client, admin_key, "runner", "token")
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "secret_store_unavailable"
    store.bao.sealed = False
    store.bao.fail("POST", "kv/data/tenants/")
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 503
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 0
    assert await events(client, admin_key, "agent.secret_set") == []

    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    store.bao.fail("DELETE", "kv/metadata/tenants/")
    assert (
        await client.delete(secret_url("runner", "token"), headers=auth(admin_key))
    ).status_code == 503
    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert [item["name"] for item in listed.json()["items"]] == ["token"]


async def test_without_a_configured_store_put_is_503_and_get_answers(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    app.state.secret_store = None
    refused = await put_secret(client, admin_key, "runner", "token")
    assert refused.status_code == 503
    assert refused.json()["error"]["details"]["reason"] == "not_configured"
    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert listed.status_code == 200 and listed.json() == {"items": []}


async def test_delete_removes_every_version_and_the_name(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    deleted = await client.delete(secret_url("runner", "token"), headers=auth(admin_key))
    assert deleted.status_code == 204 and deleted.content == b""
    assert ("DELETE", f"kv/metadata/{kv_path(tenant_id, 'runner', 'token')}") in store.bao.writes()
    assert kv_path(tenant_id, "runner", "token") not in store.bao.kv
    [event] = await events(client, admin_key, "agent.secret_deleted")
    assert event["payload"] == {"agentKey": "runner", "name": "token"}
    # Again, and a name that could never be one: not found, nothing asked of the store.
    store.bao.requests.clear()
    for name in ("token", "Not-A-Name"):
        again = await client.delete(secret_url("runner", name), headers=auth(admin_key))
        assert again.status_code == 404, again.text
    assert store.bao.requests == []
    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert listed.json() == {"items": []}


async def test_an_idempotency_key_replays_without_a_second_value(
    client: httpx.AsyncClient, store: Store
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    headers = {"Idempotency-Key": "secret-1"}
    first = await put_secret(client, admin_key, "runner", "token", headers=headers)
    assert first.status_code == 201
    # The fingerprint has no value: another value under the key is the replay,
    # and the store keeps the first.
    second = await put_secret(
        client, admin_key, "runner", "token", value="other-" + VALUE, headers=headers
    )
    assert second.status_code == 201
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first.json()
    assert store.bao.kv[kv_path(tenant_id, "runner", "token")].data == {"value": VALUE}
    assert len(await events(client, admin_key, "agent.secret_set")) == 1


async def test_parallel_puts_of_one_new_name(client: httpx.AsyncClient, store: Store) -> None:
    admin_key, _tenant = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    answers = await asyncio.gather(
        *(put_secret(client, admin_key, "runner", "token", value=f"{VALUE}-{i}") for i in range(4))
    )
    assert sorted(a.status_code for a in answers) == [200, 200, 200, 201]
    recorded = await events(client, admin_key, "agent.secret_set")
    assert sorted(e["payload"]["created"] for e in recorded) == [False, False, False, True]


async def test_no_value_reaches_tables_events_or_logs(
    client: httpx.AsyncClient,
    store: Store,
    worker: Worker,
    sync_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    admin_key, _tenant = await tenant(client)
    await linked_agent(client, admin_key, "runner", [])
    values = [VALUE, "second-" + VALUE, "third-" + VALUE]
    assert (
        await put_secret(client, admin_key, "runner", "token", value=values[0])
    ).status_code == 201
    assert (
        await put_secret(
            client,
            admin_key,
            "runner",
            "token",
            value=values[1],
            headers={"Idempotency-Key": "k"},
        )
    ).status_code == 200
    await put_secret(
        client, admin_key, "runner", "token", value=values[2], headers={"Idempotency-Key": "k"}
    )
    # A refused value (too long) and a refused name carry no value back either.
    long_value = "L" * 65_537
    assert (
        await put_secret(client, admin_key, "runner", "token", value=long_value)
    ).status_code == 400
    assert (await put_secret(client, admin_key, "runner", "BAD", value=values[2])).status_code == (
        422
    )
    await worker.run_once()

    database = dump_database(sync_engine)
    everything = await client.get("/api/v1/events", params={"limit": 200}, headers=auth(admin_key))
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for value in [*values, long_value]:
        assert value not in database, value
        assert value not in everything.text, value
        assert value not in logged, value
    assert values[1] in store.bao.dump()


# --- the agent reads its secrets (§9, §11) --------------------------------------------------


async def test_an_agent_reads_its_secret_and_not_another_agents(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, tenant_id = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "runner", ["crm"])
    neighbour = await linked_agent(client, admin_key, "neighbour", [])
    # The first pass starts the tenant's cursor: what follows comes through the journal.
    assert await worker.sync_connection_policies(force=True) == 2
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    assert (
        await put_secret(client, admin_key, "neighbour", "token", value="theirs-" + VALUE)
    ).status_code == 201

    # The journal brings agent.secret_set: the prefix joins the connection's path.
    assert (await worker.run_once())["connection_policy_events_read"] > 0
    bao = store.bao
    assert bao.policies[agent_policy_name(agent.principal_id)] == "".join(
        sorted(
            [
                prefix_rule(tenant_id, "runner"),
                f'path "{mine["secretRef"]}" {{ capabilities = ["read"] }}\n',
            ]
        )
    )
    # An agent with secrets and no connection has a policy of its own.
    assert bao.policies[agent_policy_name(neighbour.principal_id)] == prefix_rule(
        tenant_id, "neighbour"
    )

    http = await agent_login(bao, agent)
    own = await read(http, secret_ref(tenant_id, "runner", "token"))
    assert own.status_code == 200
    assert own.json()["data"]["data"] == {"value": VALUE}
    assert (await read(http, mine["secretRef"])).status_code == 200
    for ref in (
        secret_ref(tenant_id, "neighbour", "token"),
        secret_ref(str(uuid.uuid4()), "runner", "token"),
        f"kv/data/tenants/{tenant_id}/agents/runner",
        f"kv/data/tenants/{tenant_id}/agents/runner-x/token",
    ):
        assert (await read(http, ref)).status_code == 403, ref
    neighbour_http = await agent_login(bao, neighbour)
    theirs = await read(neighbour_http, secret_ref(tenant_id, "neighbour", "token"))
    assert theirs.status_code == 200
    assert theirs.json()["data"]["data"] == {"value": "theirs-" + VALUE}
    assert (await read(neighbour_http, secret_ref(tenant_id, "runner", "token"))).status_code == 403

    # A repeated pass writes nothing.
    bao.requests.clear()
    assert await worker.sync_connection_policies(force=True) == 0
    assert policy_writes(bao) == []

    # The last secret of the neighbour goes: so do its policy and role.
    deleted = await client.delete(secret_url("neighbour", "token"), headers=auth(admin_key))
    assert deleted.status_code == 204
    await worker.run_once()
    assert agent_policy_name(neighbour.principal_id) not in bao.policies
    assert agent_role_name(neighbour.principal_id) not in bao.roles
    assert (
        await read(neighbour_http, secret_ref(tenant_id, "neighbour", "token"))
    ).status_code == (403)


async def test_a_secret_set_before_the_identity_is_read_after_the_link(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    await worker.run_once()
    assert store.bao.policies == {}
    linked = await client.put(
        f"{AGENTS}/runner/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    await worker.run_once()
    principal = uuid.UUID(linked.json()["principalId"])
    assert store.bao.policies[agent_policy_name(principal)] == prefix_rule(tenant_id, "runner")


async def test_retire_deletes_the_secrets_with_the_access(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, tenant_id = await tenant(client)
    agent = await linked_agent(client, admin_key, "runner", [])
    for name in ("token", "npm"):
        assert (await put_secret(client, admin_key, "runner", name)).status_code == 201
    # Never linked: its secrets go as well.
    assert (await publish_agent(client, admin_key, "loose", agent_spec())).status_code == 201
    assert (await put_secret(client, admin_key, "loose", "token")).status_code == 201
    await worker.run_once()
    bao = store.bao
    assert agent_policy_name(agent.principal_id) in bao.policies

    for key in ("runner", "loose"):
        retired = await client.post(
            f"{AGENTS}/{key}:retire", json={"reason": "replaced"}, headers=auth(admin_key)
        )
        assert retired.status_code == 200, retired.text
    await worker.run_once()
    assert agent_policy_name(agent.principal_id) not in bao.policies
    assert agent_role_name(agent.principal_id) not in bao.roles
    assert not [path for path in bao.kv if path.startswith(f"tenants/{tenant_id}/agents/")]
    for name in ("token", "npm"):
        assert ("DELETE", f"kv/metadata/{kv_path(tenant_id, 'runner', name)}") in bao.writes()
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 0
    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert listed.json() == {"items": []}
    # Nothing left: the next pass asks the store to delete nothing.
    bao.requests.clear()
    await worker.sync_connection_policies(force=True)
    assert bao.writes("kv/") == []


async def test_a_store_failure_keeps_the_retired_names_for_the_next_sync(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    retired = await client.post(
        f"{AGENTS}/runner:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200
    store.bao.fail("DELETE", "kv/metadata/tenants/")
    await worker.sync_connection_policies(force=True)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 1
    await worker.sync_connection_policies(force=True)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 0
    assert kv_path(tenant_id, "runner", "token") not in store.bao.kv


# --- values without a name: the store's list comes first (tail of I012) ----------------------


@pytest.fixture
def policy_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """The worker's logs, wherever the root's handlers went.

    ``configure_logging`` replaces the root's handlers, and alembic's
    ``fileConfig`` disables the loggers that existed before the migrations;
    the record goes to the test's handler alone, once.
    """
    logger = logging.getLogger(connection_policies.__name__)
    disabled, logger.disabled = logger.disabled, False
    propagate, logger.propagate = logger.propagate, False
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.disabled = disabled
        logger.propagate = propagate


def unnamed_warnings(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.__dict__["agent"], record.__dict__["secret_name"])
        for record in caplog.records
        if record.getMessage() == "agent secret without a name deleted"
    ]


async def test_retire_deletes_values_the_names_never_had(
    client: httpx.AsyncClient,
    store: Store,
    worker: Worker,
    sync_engine: Engine,
    policy_logs: pytest.LogCaptureFixture,
) -> None:
    """A ``PUT`` whose commit failed: the value is in the store, the name is not."""
    caplog = policy_logs
    admin_key, tenant_id = await tenant(client)
    tenant_uuid = uuid.UUID(tenant_id)
    await linked_agent(client, admin_key, "runner", [])
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    await linked_agent(client, admin_key, "neighbour", [])
    assert (await put_secret(client, admin_key, "neighbour", "token")).status_code == 201
    await worker.run_once()
    # Past the names: what a rolled-back PUT leaves, a nested path, and a key
    # no agent row has.
    for agent, name in (("runner", "stray"), ("runner", "dir/deep"), ("ghost", "token")):
        await store.client.kv_write(kv_path(tenant_id, agent, name), {"value": VALUE})
    assert (await put_secret(client, admin_key, "neighbour", "other")).status_code == 201

    retired = await client.post(
        f"{AGENTS}/runner:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    # The journal (after agent.retired) is enough; no full pass.
    assert await worker.process_connection_policy_events() > 0
    bao = store.bao
    left = sorted(path for path in bao.kv if path.startswith(f"tenants/{tenant_id}/agents/"))
    assert left == [kv_path(tenant_id, "neighbour", name) for name in ("other", "token")]
    for name in ("token", "stray", "dir/deep"):
        assert ("DELETE", f"kv/metadata/{kv_path(tenant_id, 'runner', name)}") in bao.writes()
    with sync_engine.connect() as conn:
        names = conn.execute(text("SELECT name FROM agent_secret_names ORDER BY name")).all()
    assert [row[0] for row in names] == ["other", "token"]
    # Only the paths the names did not have are reported, and never the value.
    assert sorted(unnamed_warnings(caplog)) == [
        ("ghost", "token"),
        ("runner", "dir/deep"),
        ("runner", "stray"),
    ]
    assert {
        record.__dict__["tenant"]
        for record in caplog.records
        if record.getMessage() == "agent secret without a name deleted"
    } == {str(tenant_uuid)}
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert VALUE not in logged

    # Idempotent: the next passes list the store and delete nothing.
    bao.requests.clear()
    await worker.sync_connection_policies(force=True)
    assert await worker.process_connection_policy_events() == 0
    assert bao.writes("kv/") == []


async def test_a_retired_agent_never_linked_and_without_names(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    """No principal, no name, no connection: the full pass still reaches the tenant."""
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "loose", agent_spec())).status_code == 201
    await store.client.kv_write(kv_path(tenant_id, "loose", "token"), {"value": VALUE})
    retired = await client.post(
        f"{AGENTS}/loose:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    assert await worker.sync_connection_policies(force=True) == 1
    assert kv_path(tenant_id, "loose", "token") not in store.bao.kv


async def test_the_full_pass_deletes_values_an_active_agent_has_no_name_for(
    client: httpx.AsyncClient,
    store: Store,
    worker: Worker,
    sync_engine: Engine,
    policy_logs: pytest.LogCaptureFixture,
) -> None:
    caplog = policy_logs
    admin_key, tenant_id = await tenant(client)
    agent = await linked_agent(client, admin_key, "runner", [])
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    await worker.run_once()
    stray = kv_path(tenant_id, "runner", "stray")
    await store.client.kv_write(stray, {"value": VALUE})
    # An event of the tenant wakes the journal: it leaves an active agent's values alone.
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 200
    assert await worker.process_connection_policy_events() > 0
    assert stray in store.bao.kv

    # A writer of the agent holds its row (a PUT before its commit): the pass waits for the next.
    with sync_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT 1 FROM agents WHERE principal_id = :p FOR UPDATE"),
            {"p": agent.principal_id},
        )
        await worker.sync_connection_policies(force=True)
        assert stray in store.bao.kv

    assert await worker.sync_connection_policies(force=True) == 1
    assert stray not in store.bao.kv
    assert kv_path(tenant_id, "runner", "token") in store.bao.kv
    assert unnamed_warnings(caplog) == [("runner", "stray")]
    assert VALUE not in "\n".join(str(record.__dict__) for record in caplog.records)
    # The agent keeps its access and its name.
    assert agent_policy_name(agent.principal_id) in store.bao.policies
    listed = await client.get(f"{AGENTS}/runner/secrets", headers=auth(admin_key))
    assert [item["name"] for item in listed.json()["items"]] == ["token"]
    assert await worker.sync_connection_policies(force=True) == 0


async def test_a_failed_list_keeps_the_retired_names_for_the_next_sync(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, tenant_id = await tenant(client)
    assert (await publish_agent(client, admin_key, "runner", agent_spec())).status_code == 201
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    retired = await client.post(
        f"{AGENTS}/runner:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200
    store.bao.fail("LIST", f"kv/metadata/tenants/{tenant_id}/agents/runner")
    await worker.sync_connection_policies(force=True)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 1
    assert kv_path(tenant_id, "runner", "token") in store.bao.kv
    await worker.sync_connection_policies(force=True)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_secret_names")).scalar() == 0
    assert kv_path(tenant_id, "runner", "token") not in store.bao.kv


# --- the binding and the principal (review of I011) ---------------------------------------------


async def _binding_id(client: httpx.AsyncClient, admin_key: str, agent: AgentIds) -> str:
    bindings = await client.get(
        f"/api/v1/principals/{agent.principal_id}/iam-bindings", headers=auth(admin_key)
    )
    assert bindings.status_code == 200, bindings.text
    [binding] = bindings.json()["items"]
    return str(binding["id"])


async def test_a_revoked_binding_takes_the_store_access_away(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    admin_key, tenant_id = await tenant(client)
    mine = await token_connection(client, admin_key, "crm")
    agent = await linked_agent(client, admin_key, "runner", ["crm"])
    assert (await put_secret(client, admin_key, "runner", "token")).status_code == 201
    await worker.run_once()
    bao = store.bao
    http = await agent_login(bao, agent)
    assert (await read(http, mine["secretRef"])).status_code == 200
    assert (await read(http, secret_ref(tenant_id, "runner", "token"))).status_code == 200

    binding = await _binding_id(client, admin_key, agent)
    revoked = await client.post(f"/api/v1/iam-bindings/{binding}:revoke", headers=auth(admin_key))
    assert revoked.status_code == 200, revoked.text
    # The journal (iam_binding.revoked) brings it: the token issued before is refused.
    assert (await worker.run_once())["connection_policy_events_read"] > 0
    assert agent_policy_name(agent.principal_id) not in bao.policies
    assert agent_role_name(agent.principal_id) not in bao.roles
    assert (await read(http, mine["secretRef"])).status_code == 403
    assert (await read(http, secret_ref(tenant_id, "runner", "token"))).status_code == 403
    # The full pass agrees.
    bao.requests.clear()
    assert await worker.sync_connection_policies(force=True) == 0

    # Linked again through the registry (iam-bindings refuses a registry agent's
    # identity, CP-ADR-0073 amendment 2026-09-30 I4): the same identity reopens
    # the binding, and iam_binding.updated gives the access back.
    rebound = await client.put(
        f"{AGENTS}/{agent.key}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": agent.iam_tenant_id,
            "iamPrincipalId": agent.iam_principal_id,
        },
        headers=auth(admin_key),
    )
    assert rebound.status_code == 200, rebound.text
    assert (await worker.run_once())["connection_policy_events_read"] > 0
    assert agent_policy_name(agent.principal_id) in bao.policies
    again = await agent_login(bao, agent)
    assert (await read(again, secret_ref(tenant_id, "runner", "token"))).status_code == 200


async def test_a_disabled_binding_or_principal_has_no_access(
    client: httpx.AsyncClient, store: Store, worker: Worker, sync_engine: Engine
) -> None:
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    by_binding = await linked_agent(client, admin_key, "by-binding", ["crm"])
    by_principal = await linked_agent(client, admin_key, "by-principal", ["crm"])
    kept = await linked_agent(client, admin_key, "kept", ["crm"])
    await worker.sync_connection_policies(force=True)
    bao = store.bao
    assert len(bao.policies) == 3

    # The operator's switches (no event): the full pass finds them.
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE iam_principal_bindings SET status = 'disabled' WHERE principal_id = :p"),
            {"p": by_binding.principal_id},
        )
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :p"),
            {"p": by_principal.principal_id},
        )
    assert await worker.sync_connection_policies(force=True) == 4
    assert set(bao.policies) == {agent_policy_name(kept.principal_id)}
    assert set(bao.roles) == {agent_role_name(kept.principal_id)}
    # The principal of an agent is disabled only by :retire (use_agent_retire), and
    # a binding is disabled only by the operator: no event, the full pass is the way.


async def test_revoke_of_a_connection_keeps_a_revoked_agent_out(
    client: httpx.AsyncClient, store: Store, worker: Worker
) -> None:
    """``:revoke`` syncs the agents naming the connection with the same rule."""
    admin_key, _tenant = await tenant(client)
    await token_connection(client, admin_key, "crm")
    await token_connection(client, admin_key, "crm-other")
    agent = await linked_agent(client, admin_key, "runner", ["crm", "crm-other"])
    await worker.sync_connection_policies(force=True)
    binding = await _binding_id(client, admin_key, agent)
    store.bao.sealed = True  # the worker does not see the revocation of the binding
    assert (
        await client.post(f"/api/v1/iam-bindings/{binding}:revoke", headers=auth(admin_key))
    ).status_code == 200
    await worker.run_once()
    store.bao.sealed = False
    revoked = await revoke(client, admin_key, "crm")
    assert revoked.status_code == 200, revoked.text
    assert agent_policy_name(agent.principal_id) not in store.bao.policies
