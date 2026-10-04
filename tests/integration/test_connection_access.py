"""Access to connections through the API (CP-ADR-0079 §5-§7, integrations-connections I010).

The secret store is :class:`tests.fake_openbao.FakeOpenBao` behind the real
client, and the provider is its fake token server: the plugin exchanges the
code there. What is checked is what the core sends to the store, what it
keeps in its tables and events, and that no value of the callback or of a
key ever lands outside the store.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.infrastructure.secret_store import SecretStore
from control_plane.worker.main import Worker
from tests.fake_openbao import BASE_URL, IAM_JWT, FakeOpenBao, _Doc
from tests.helpers import auth, do_bootstrap, make_tenant_directly
from tests.integration.test_connection_types import publish
from tests.integration.test_connections import CONNECTIONS, create, events
from tests.unit.test_connection_type_domain import SPEC, TOKEN_ONLY, spec_with

REDIRECT_URI = "https://cp.example/api/v1/connections:callback"
RETURN_URL = "https://console.example/connections"
CALLBACK = "/api/v1/connections:callback"
ACCOUNT = "acme.crm.example"
TOKEN_URL = "https://acme.crm.example/oauth2/access_token"
OTHER_ACCOUNT = "other.crm.example"
OTHER_TOKEN_URL = "https://other.crm.example/oauth2/access_token"
CLIENT_SECRET = "app-secret-" + "Q" * 24


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "oauth_redirect_uri": REDIRECT_URI,
            "connections_return_url": RETURN_URL,
        }
    )


class _IamToken:
    """The core's IAM token source: a fixed JWT the fake store accepts."""

    def __init__(self, bao: FakeOpenBao) -> None:
        self.bao = bao
        self.forgotten = 0

    async def __call__(self) -> str:
        return IAM_JWT

    def forget(self) -> None:
        self.forgotten += 1


@pytest.fixture(autouse=True)
def _live_loggers(app: FastAPI) -> Iterator[None]:
    """The core's loggers as in production: alembic's ``fileConfig`` (the
    migrations of the test database) disables every logger created before
    it, and the log assertions below would then see nothing."""
    disabled = [
        logger
        for name, logger in logging.root.manager.loggerDict.items()
        if name.startswith("control_plane")
        and isinstance(logger, logging.Logger)
        and logger.disabled
    ]
    for logger in disabled:
        logger.disabled = False
    yield
    for logger in disabled:
        logger.disabled = True


@pytest.fixture
def bao(app: FastAPI) -> Iterator[FakeOpenBao]:
    fake = FakeOpenBao()
    iam = _IamToken(fake)
    app.state.secret_store = SecretStore(
        BASE_URL, iam, forget_token=iam.forget, client=fake.client()
    )
    yield fake
    app.state.secret_store = None


# --- helpers -------------------------------------------------------------------------


async def _admin(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    admin = await do_bootstrap(client)
    return admin["apiKey"]["key"], admin


async def _crm(client: httpx.AsyncClient, key: str, spec: dict[str, Any] | None = None) -> None:
    assert (await publish(client, key, spec=spec)).status_code == 201
    assert (await create(client, key)).status_code == 201


async def set_app(
    client: httpx.AsyncClient,
    key: str,
    type_key: str = "crm",
    headers: dict[str, str] | None = None,
    **body: Any,
) -> httpx.Response:
    return await client.put(
        f"/api/v1/connection-types/{type_key}/oauth-app",
        json={"clientId": "app-client", "clientSecret": CLIENT_SECRET, **body},
        headers={**auth(key), **(headers or {})},
    )


async def start(
    client: httpx.AsyncClient,
    key: str,
    connection: str = "crm",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"{CONNECTIONS}/{connection}:authorize", json={}, headers={**auth(key), **(headers or {})}
    )


async def started_state(client: httpx.AsyncClient, key: str, connection: str = "crm") -> str:
    response = await start(client, key, connection)
    assert response.status_code == 200, response.text
    [state] = parse_qs(urlsplit(response.json()["authorizeUrl"]).query)["state"]
    return state


async def callback(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    return await client.get(CALLBACK, params=params)


def outcome(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(RETURN_URL + "?")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    return parse_qs(urlsplit(location).query)


async def card(client: httpx.AsyncClient, key: str, connection: str = "crm") -> dict[str, Any]:
    response = await client.get(f"{CONNECTIONS}/{connection}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def put_token(
    client: httpx.AsyncClient,
    key: str,
    connection: str = "crm",
    headers: dict[str, str] | None = None,
    **body: Any,
) -> httpx.Response:
    return await client.put(
        f"{CONNECTIONS}/{connection}/token",
        json={"account": ACCOUNT, "token": "key-" + "K" * 30, **body},
        headers={**auth(key), **(headers or {})},
    )


async def human(
    client: httpx.AsyncClient, admin_key: str, name: str, permissions: list[str]
) -> tuple[str, str, str]:
    """A human with one API key: (principal id, key id, key)."""
    principal = await client.post(
        "/api/v1/principals",
        json={"kind": "human", "displayName": name},
        headers=auth(admin_key),
    )
    assert principal.status_code == 201, principal.text
    created = await client.post(
        f"/api/v1/principals/{principal.json()['id']}/api-keys",
        json={"permissions": permissions},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    return principal.json()["id"], created.json()["id"], created.json()["key"]


MANAGER = ["connections.manage", "connections.read", "events.read"]


def dump_database(sync_engine: Engine) -> str:
    """Every row of every table as text: the place no value may reach."""
    with sync_engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars()
        parts = []
        for table in list(tables):
            rows = conn.execute(text(f'SELECT t::text FROM "{table}" t')).scalars()
            parts.extend(rows)
        return "\n".join(parts)


def app_writes(bao: FakeOpenBao) -> list[dict[str, Any]]:
    return [
        body
        for method, path, body in bao.requests
        if method == "POST" and path == "kv/data/platform/oauth-apps/crm" and body is not None
    ]


def state_rows(sync_engine: Engine) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        return [
            dict(row._mapping)
            for row in conn.execute(
                text("SELECT * FROM connection_oauth_states ORDER BY created_at")
            )
        ]


# --- the OAuth application of a type (§5) --------------------------------------------------


async def test_the_oauth_app_goes_to_the_store_and_its_secret_is_never_answered(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    assert (await publish(client, admin_key)).status_code == 201

    before = await client.get("/api/v1/connection-types/crm/oauth-app", headers=auth(admin_key))
    assert before.status_code == 200, before.text
    assert before.json() == {
        "type": "crm",
        "configured": False,
        "clientId": None,
        "updatedAt": None,
    }

    written = await set_app(client, admin_key)
    assert written.status_code == 200, written.text
    body = written.json()
    assert (body["type"], body["configured"], body["clientId"]) == ("crm", True, "app-client")
    assert body["updatedAt"] is not None
    assert CLIENT_SECRET not in written.text

    document = bao.kv["platform/oauth-apps/crm"]
    assert document.data["client_secret"] == CLIENT_SECRET
    assert document.data["tenant_id"] == admin["tenant"]["id"]
    assert document.data["updated_by"] == admin["adminPrincipal"]["id"]
    # The first write carries cas = 0: the key had no document.
    assert ("POST", "kv/data/platform/oauth-apps/crm") in [(m, p) for m, p, _ in bao.requests]
    assert app_writes(bao)[0]["options"] == {"cas": 0}

    read = await client.get("/api/v1/connection-types/crm/oauth-app", headers=auth(admin_key))
    assert read.json()["configured"] is True
    assert read.json()["clientId"] == "app-client"
    assert CLIENT_SECRET not in read.text

    replaced = await set_app(client, admin_key, clientSecret="app-secret-2-" + "R" * 20)
    assert replaced.status_code == 200, replaced.text
    # One version in the store: the replacement is over the version it read.
    assert bao.kv["platform/oauth-apps/crm"].version == 2
    assert app_writes(bao)[-1]["options"] == {"cas": 1}

    [first, second] = await events(client, admin_key, "connection_type.oauth_app_set")
    assert first["payload"] == {"type": "crm", "created": True}
    assert second["payload"] == {"type": "crm", "created": False}
    assert first["actorId"] == admin["adminPrincipal"]["id"]


async def test_the_oauth_app_refusals(
    client: httpx.AsyncClient, app: FastAPI, bao: FakeOpenBao
) -> None:
    admin_key, _admin_body = await _admin(client)
    assert (
        await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)
    ).status_code == 201
    assert (await publish(client, admin_key)).status_code == 201

    unsupported = await set_app(client, admin_key, type_key="tracker")
    assert unsupported.status_code == 422, unsupported.text
    assert unsupported.json()["error"]["code"] == "auth_not_supported"

    unknown = await set_app(client, admin_key, type_key="nope")
    assert unknown.status_code == 404, unknown.text

    for body in ({"clientSecret": ""}, {"clientId": ""}, {"clientSecret": None}):
        invalid = await set_app(client, admin_key, **body)
        assert invalid.status_code == 400, (body, invalid.text)
        assert CLIENT_SECRET not in invalid.text

    _principal, _key_id, reader = await human(client, admin_key, "reader", ["connections.read"])
    forbidden = await set_app(client, reader)
    assert forbidden.status_code == 403, forbidden.text

    assert bao.kv == {}

    app.state.secret_store = None
    no_store = await set_app(client, admin_key)
    assert no_store.status_code == 503, no_store.text
    error = no_store.json()["error"]
    assert error["code"] == "secret_store_unavailable"
    assert error["details"]["retryable"] is True
    no_store_read = await client.get(
        "/api/v1/connection-types/crm/oauth-app", headers=auth(admin_key)
    )
    assert no_store_read.status_code == 503
    assert await events(client, admin_key, "connection_type.oauth_app_set") == []


async def test_a_store_that_refuses_the_login_or_is_sealed_is_503_and_changes_nothing(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    assert (await publish(client, admin_key)).status_code == 201

    bao.jwt = "another-audience"
    refused = await set_app(client, admin_key)
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["details"]["reason"] == "login_failed"
    assert CLIENT_SECRET not in refused.text

    bao.jwt = IAM_JWT
    bao.sealed = True
    sealed = await set_app(client, admin_key)
    assert sealed.status_code == 503, sealed.text
    assert sealed.json()["error"]["details"]["reason"] == "sealed"
    assert bao.kv == {}
    assert await events(client, admin_key, "connection_type.oauth_app_set") == []


async def test_another_tenant_neither_overwrites_nor_uses_the_oauth_app(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200

    _other_tenant, other_key = make_tenant_directly(sync_engine, "other")
    await _crm(client, other_key)

    taken = await set_app(client, other_key, clientSecret="other-secret-" + "S" * 20)
    assert taken.status_code == 409, taken.text
    assert taken.json()["error"]["code"] == "oauth_app_owned_by_other_tenant"
    assert bao.kv["platform/oauth-apps/crm"].data["client_secret"] == CLIENT_SECRET

    read = await client.get("/api/v1/connection-types/crm/oauth-app", headers=auth(other_key))
    assert read.json()["configured"] is False

    not_theirs = await start(client, other_key)
    assert not_theirs.status_code == 409, not_theirs.text
    assert not_theirs.json()["error"]["code"] == "oauth_app_not_configured"


async def test_two_tenants_writing_one_oauth_app_at_once_do_not_overwrite_each_other(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    assert (await publish(client, admin_key)).status_code == 201
    path = "platform/oauth-apps/crm"
    other = {"client_id": "x", "client_secret": "y", "tenant_id": str(uuid.uuid4())}

    def other_tenant_writes_first() -> None:
        bao.kv[path] = _Doc(dict(other), 1)

    bao.before_kv_write = other_tenant_writes_first
    lost = await set_app(client, admin_key)
    assert lost.status_code == 409, lost.text
    assert lost.json()["error"]["code"] == "oauth_app_write_conflict"
    assert lost.json()["error"]["details"]["retryable"] is True
    assert bao.kv[path].data == other

    retried = await set_app(client, admin_key)
    assert retried.status_code == 409, retried.text
    assert retried.json()["error"]["code"] == "oauth_app_owned_by_other_tenant"


# --- :authorize (§6) ---------------------------------------------------------------------------


async def test_authorize_issues_a_state_kept_as_a_hash_only(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    await _crm(client, admin_key, spec_with("oauth2.scopes", ["crm", "user"]))
    assert (await set_app(client, admin_key)).status_code == 200

    before = datetime.now(UTC)
    response = await start(client, admin_key)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    url = urlsplit(body["authorizeUrl"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == SPEC["oauth2"]["authorizeUrl"]
    query = parse_qs(url.query)
    assert query["client_id"] == ["app-client"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["scope"] == ["crm user"]
    [state] = query["state"]
    # 256 random bits, base64url without padding.
    assert len(state) == 43 and "=" not in state
    expires = datetime.fromisoformat(body["expiresAt"])
    assert before + timedelta(seconds=590) < expires < before + timedelta(seconds=610)

    [row] = state_rows(sync_engine)
    assert bytes(row["state_hash"]) == hashlib.sha256(state.encode()).digest()
    assert str(row["principal_id"]) == admin["adminPrincipal"]["id"]
    assert row["authority"]["credentialId"] == admin["apiKey"]["id"]
    assert row["consumed_at"] is None and row["outcome"] is None
    assert state not in dump_database(sync_engine)
    assert CLIENT_SECRET not in dump_database(sync_engine)

    # The status does not change, and nothing was written to the store.
    assert (await card(client, admin_key))["status"] == "pending"
    assert bao.paths("POST") == ["auth/jwt/login", "kv/data/platform/oauth-apps/crm"]


async def test_a_later_authorize_supersedes_the_live_state(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200

    first = await started_state(client, admin_key)
    second = await started_state(client, admin_key)
    rows = state_rows(sync_engine)
    assert [row["outcome"] for row in rows] == ["superseded", None]

    code = bao.token_server.issue(TOKEN_URL)
    stale = await callback(client, state=first, code=code, referer=ACCOUNT)
    assert outcome(stale) == {"result": ["invalid_state"]}
    assert bao.token_server.requests == []

    live = await callback(client, state=second, code=code, referer=ACCOUNT)
    assert outcome(live)["result"] == ["active"]


async def test_a_replayed_authorize_answers_no_url_and_issues_no_state(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200

    headers = {"Idempotency-Key": "authorize-crm"}
    first = await start(client, admin_key, headers=headers)
    assert first.status_code == 200, first.text
    replay = await start(client, admin_key, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["authorizeUrl"] is None
    assert replay.json()["expiresAt"] == first.json()["expiresAt"]
    assert len(state_rows(sync_engine)) == 1
    [state] = parse_qs(urlsplit(first.json()["authorizeUrl"]).query)["state"]
    assert state not in dump_database(sync_engine)


async def test_authorize_refusals(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)

    not_configured = await start(client, admin_key)
    assert not_configured.status_code == 409, not_configured.text
    assert not_configured.json()["error"]["code"] == "oauth_app_not_configured"

    assert (await set_app(client, admin_key)).status_code == 200
    assert (await start(client, admin_key, connection="nope")).status_code == 404

    _principal, _key_id, reader = await human(client, admin_key, "reader", ["connections.read"])
    assert (await start(client, reader)).status_code == 403

    app.state.settings = settings.model_copy(
        update={"oauth_redirect_uri": "", "connections_return_url": ""}
    )
    unset = await start(client, admin_key)
    assert unset.status_code == 409, unset.text
    assert unset.json()["error"]["code"] == "oauth_not_configured"
    assert unset.json()["error"]["details"]["missing"] == [
        "CP_OAUTH_REDIRECT_URI",
        "CP_CONNECTIONS_RETURN_URL",
    ]
    # Review of I010: the code and the outcome travel over https or not at all.
    app.state.settings = settings.model_copy(
        update={"oauth_redirect_uri": "http://cp.example/api/v1/connections:callback"}
    )
    insecure = await start(client, admin_key)
    assert insecure.status_code == 409, insecure.text
    assert insecure.json()["error"]["code"] == "oauth_not_configured"
    assert insecure.json()["error"]["details"]["insecure"] == ["CP_OAUTH_REDIRECT_URI"]
    app.state.settings = settings

    assert (
        await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)
    ).status_code == 201
    assert (await create(client, admin_key, type="tracker")).status_code == 201
    token_only = await start(client, admin_key, connection="tracker")
    assert token_only.status_code == 422, token_only.text
    assert token_only.json()["error"]["code"] == "auth_not_supported"

    app.state.secret_store = None
    no_store = await start(client, admin_key)
    assert no_store.status_code == 503, no_store.text
    assert no_store.json()["error"]["code"] == "secret_store_unavailable"


# --- the callback (§6) ----------------------------------------------------------------------------


async def test_the_callback_exchanges_the_code_and_the_connection_becomes_active(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    tenant_id = admin["tenant"]["id"]
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)

    # A provider adds its own parameters; the callback accepts any.
    response = await callback(client, state=state, code=code, referer=ACCOUNT, domain="x")
    assert outcome(response) == {"connection": ["crm"], "result": ["active"]}

    name = f"tenants/{tenant_id}/connections/crm"
    # The server is the attempt's own, and the creds refresh through it.
    [(server_name, server)] = bao.servers.items()
    [state_row] = state_rows(sync_engine)
    assert server_name == f"{name}/{state_row['id']}"
    assert bao.creds[name]["server"] == server_name
    assert server["provider"] == "custom"
    assert server["client_id"] == "app-client"
    assert server["provider_options"] == {
        "auth_code_url": SPEC["oauth2"]["authorizeUrl"],
        "token_url": TOKEN_URL,
        "auth_style": "in_params",
    }
    [exchange] = bao.token_server.requests
    assert exchange == {
        "token_url": TOKEN_URL,
        "client_id": "app-client",
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_url": REDIRECT_URI,
        "auth_style": "in_params",
    }
    assert bao.creds[name]["access_token"].startswith("at-")

    body = await card(client, admin_key)
    assert (body["status"], body["auth"], body["account"]) == ("active", "oauth2", ACCOUNT)
    assert body["secretRef"] == f"oauth2/creds/{name}"
    assert body["connectedBy"] == admin["adminPrincipal"]["id"]
    assert body["connectedAt"] is not None
    assert (body["statusReason"], body["statusMessage"], body["expiresAt"]) == (None, None, None)
    assert body["version"] == 2

    [event] = await events(client, admin_key, "connection.authorized")
    assert event["payload"] == {
        "key": "crm",
        "type": "crm",
        "auth": "oauth2",
        "previousStatus": "pending",
        "connectedBy": admin["adminPrincipal"]["id"],
    }
    assert event["actorId"] == admin["adminPrincipal"]["id"]


async def test_consent_denied_keeps_the_connection_pending_with_the_reason(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)

    response = await callback(
        client, state=state, error="access_denied", error_description="The user said no"
    )
    assert outcome(response) == {
        "connection": ["crm"],
        "result": ["failed"],
        "reason": ["consent_denied"],
    }
    body = await card(client, admin_key)
    assert (body["status"], body["auth"], body["secretRef"]) == ("pending", None, None)
    assert (body["statusReason"], body["statusMessage"]) == ("consent_denied", "The user said no")
    assert not [path for path in bao.paths() if path.startswith("oauth2/")]
    [event] = await events(client, admin_key, "connection.authorization_failed")
    assert event["payload"] == {
        "key": "crm",
        "type": "crm",
        "reason": "consent_denied",
        "initiatedBy": admin["adminPrincipal"]["id"],
    }
    assert await events(client, admin_key, "connection.authorized") == []


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"error": "server_error"}, "provider_error"),
        ({}, "provider_error"),
        ({"code": ""}, "provider_error"),
    ],
)
async def test_a_provider_error_or_no_code_keeps_the_connection_pending(
    client: httpx.AsyncClient, bao: FakeOpenBao, params: dict[str, str], reason: str
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)

    response = await callback(client, state=state, **params)
    assert outcome(response)["reason"] == [reason]
    body = await card(client, admin_key)
    assert (body["status"], body["statusReason"]) == ("pending", reason)
    assert body["statusMessage"]


async def test_a_failed_exchange_keeps_pending_and_the_message_loses_the_code_and_secret(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)
    bao.token_server.refuse_with = "invalid_client"

    response = await callback(client, state=state, code=code, referer=ACCOUNT)
    assert outcome(response)["reason"] == ["oauth_exchange_failed"]
    body = await card(client, admin_key)
    assert (body["status"], body["statusReason"]) == ("pending", "oauth_exchange_failed")
    assert "invalid_client" in body["statusMessage"]
    assert code not in body["statusMessage"]
    assert CLIENT_SECRET not in body["statusMessage"]
    # The attempt's server, with its copy of the application's secret, is gone.
    assert bao.servers == {} and bao.creds == {}


async def _connected(client: httpx.AsyncClient, key: str, bao: FakeOpenBao) -> None:
    state = await started_state(client, key)
    response = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT
    )
    assert outcome(response)["result"] == ["active"]


@pytest.mark.parametrize(
    "failure",
    ["exchange_refused", "exchange_unavailable", "server_unavailable"],
)
async def test_a_failed_reauthorization_keeps_the_refresh_at_the_first_host(
    client: httpx.AsyncClient, bao: FakeOpenBao, failure: str
) -> None:
    # Review of I010: a callback naming account B must not point the server
    # the creds of account A refresh through at B's host, whatever fails next.
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    await _connected(client, admin_key, bao)
    creds = dict(bao.creds[name])
    servers = {server: dict(config) for server, config in bao.servers.items()}
    assert servers[creds["server"]]["provider_options"]["token_url"] == TOKEN_URL

    state = await started_state(client, admin_key)
    code = bao.token_server.issue(OTHER_TOKEN_URL)
    if failure == "exchange_refused":
        code = "code-unknown"
    elif failure == "exchange_unavailable":
        bao.fail("PUT", "oauth2/creds/")
    else:
        bao.fail("PUT", "oauth2/servers/")
    response = await callback(client, state=state, code=code, referer=OTHER_ACCOUNT)
    assert outcome(response)["result"] == ["failed"]

    assert bao.creds[name] == creds
    assert bao.servers == servers
    body = await card(client, admin_key)
    assert (body["status"], body["auth"], body["account"]) == ("active", "oauth2", ACCOUNT)
    assert body["statusReason"] in ("oauth_exchange_failed", "secret_store_unavailable")


async def test_a_reauthorization_with_another_account_drops_the_previous_server(
    client: httpx.AsyncClient, bao: FakeOpenBao, caplog: pytest.LogCaptureFixture
) -> None:
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    await _connected(client, admin_key, bao)
    first = bao.creds[name]["server"]

    state = await started_state(client, admin_key)
    response = await callback(
        client, state=state, code=bao.token_server.issue(OTHER_TOKEN_URL), referer=OTHER_ACCOUNT
    )
    assert outcome(response)["result"] == ["active"]
    second = bao.creds[name]["server"]
    assert second != first
    assert list(bao.servers) == [second]
    assert bao.servers[second]["provider_options"]["token_url"] == OTHER_TOKEN_URL
    assert (await card(client, admin_key))["account"] == OTHER_ACCOUNT

    # A previous server the store would not delete is left and named in the
    # log; the connection goes on through the new one.
    bao.fail("DELETE", "oauth2/servers/")
    caplog.set_level(logging.WARNING)
    await _connected(client, admin_key, bao)
    third = bao.creds[name]["server"]
    assert set(bao.servers) == {second, third}
    [orphaned] = [r for r in caplog.records if r.getMessage() == "connection_oauth_server_orphaned"]
    assert orphaned.server == second  # type: ignore[attr-defined]
    assert (await card(client, admin_key))["account"] == ACCOUNT


async def test_a_key_written_during_an_exchange_waits_for_it(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    # Review of I010: the callback holds the connection through the exchange,
    # so a concurrent ``PUT …/token`` never leaves both a key and creds.
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    writes: list[asyncio.Task[httpx.Response]] = []
    waited: list[bool] = []

    async def meanwhile() -> None:
        writes.append(asyncio.create_task(put_token(client, admin_key)))
        await asyncio.sleep(0.5)
        waited.append(not writes[0].done())

    bao.before_exchange = meanwhile
    response = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT
    )
    assert outcome(response)["result"] == ["active"]
    written = await writes[0]
    assert written.status_code == 200, written.text
    assert waited == [True]
    assert bao.creds == {} and bao.servers == {} and name in bao.kv
    body = await card(client, admin_key)
    assert (body["auth"], body["secretRef"]) == ("token", f"kv/data/{name}")


async def test_a_replayed_or_expired_or_unknown_state_is_invalid_and_writes_nothing(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)
    assert outcome(await callback(client, state=state, code=code, referer=ACCOUNT))["result"] == [
        "active"
    ]
    version = (await card(client, admin_key))["version"]
    exchanges = len(bao.token_server.requests)

    replay = await callback(client, state=state, code=code, referer=ACCOUNT)
    assert outcome(replay) == {"result": ["invalid_state"]}

    expired = await started_state(client, admin_key)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE connection_oauth_states SET expires_at = now() - interval '1 second'"
                " WHERE consumed_at IS NULL"
            )
        )
    late = await callback(client, state=expired, code=code, referer=ACCOUNT)
    assert outcome(late) == {"result": ["invalid_state"]}
    # The expired state is left as it was: nothing is written for it.
    assert state_rows(sync_engine)[-1]["consumed_at"] is None

    for params in ({}, {"state": ""}, {"state": "x" * 201}, {"state": "unknown"}):
        assert outcome(await callback(client, **params)) == {"result": ["invalid_state"]}

    assert (await card(client, admin_key))["version"] == version
    assert len(bao.token_server.requests) == exchanges
    assert await events(client, admin_key, "connection.authorization_failed") == []
    assert len(await events(client, admin_key, "connection.authorized")) == 1


async def test_two_arrivals_of_one_state_at_once_connect_once(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)

    responses = await asyncio.gather(
        *(callback(client, state=state, code=code, referer=ACCOUNT) for _ in range(4))
    )
    results = sorted(outcome(response)["result"][0] for response in responses)
    assert results == ["active", "invalid_state", "invalid_state", "invalid_state"]
    assert len(bao.token_server.requests) == 1
    assert len(await events(client, admin_key, "connection.authorized")) == 1


async def test_the_state_acts_for_its_principal_and_credential_only(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    alice, _alice_key_id, alice_key = await human(client, admin_key, "alice", MANAGER)
    _bob, bob_key_id, bob_key = await human(client, admin_key, "bob", MANAGER)

    # Bob starts, and his key is revoked before the provider returns: the
    # callback does not complete his authorization, whoever's browser brings it.
    bob_state = await started_state(client, bob_key)
    revoked = await client.post(f"/api/v1/api-keys/{bob_key_id}:revoke", headers=auth(admin_key))
    assert revoked.status_code == 200, revoked.text
    code = bao.token_server.issue(TOKEN_URL)
    response = await callback(client, state=bob_state, code=code, referer=ACCOUNT)
    assert outcome(response)["reason"] == ["initiator_not_authorized"]
    assert bao.token_server.requests == []
    body = await card(client, admin_key)
    assert (body["status"], body["statusReason"]) == ("pending", "initiator_not_authorized")

    # Alice's state connects as Alice, not as the caller of the callback.
    alice_state = await started_state(client, alice_key)
    response = await callback(client, state=alice_state, code=code, referer=ACCOUNT)
    assert outcome(response)["result"] == ["active"]
    assert (await card(client, admin_key))["connectedBy"] == alice


async def test_a_state_of_another_tenant_connects_only_its_own_connection(
    client: httpx.AsyncClient, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    other_tenant, other_key = make_tenant_directly(sync_engine, "other")
    await _crm(client, other_key)
    state = await started_state(client, admin_key)

    response = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT
    )
    assert outcome(response)["result"] == ["active"]
    assert (await card(client, other_key))["status"] == "pending"
    assert not [name for name in bao.servers if other_tenant in name]


@pytest.mark.parametrize(
    "account",
    [
        "evil.example",  # not the type's pattern
        "a" * 254 + ".crm.example",  # longer than a DNS name
        "ACME.crm.example",  # not a host name of the core's form
    ],
)
async def test_an_invalid_account_is_refused_before_the_exchange(
    client: httpx.AsyncClient, bao: FakeOpenBao, account: str
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)

    response = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=account
    )
    assert outcome(response)["reason"] == ["invalid_account"]
    body = await card(client, admin_key)
    assert (body["status"], body["statusReason"]) == ("pending", "invalid_account")
    assert account not in body["statusMessage"]
    assert bao.servers == {} and bao.token_server.requests == []


async def test_a_long_account_never_reaches_a_backtracking_pattern(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    # ``(a+)+b`` over 254 letters would not finish; the length is checked first.
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key, spec_with("accountField.pattern", r"(a+)+\.crm\.example"))
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)

    response = await asyncio.wait_for(
        callback(client, state=state, code="c", referer="a" * 300), timeout=10
    )
    assert outcome(response)["reason"] == ["invalid_account"]
    token = await asyncio.wait_for(put_token(client, admin_key, account="a" * 300), timeout=10)
    assert token.status_code == 400, token.text


async def test_without_a_return_url_the_callback_answers_text(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    app.state.settings = settings.model_copy(update={"connections_return_url": ""})

    response = await callback(client, state=state, error="access_denied")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.text == "result=failed reason=consent_denied"
    unknown = await callback(client, state="unknown", code="c0de")
    assert unknown.text == "result=invalid_state"
    # A return page that is not https is no return page: the answer is text.
    app.state.settings = settings.model_copy(
        update={"connections_return_url": "http://console.example/connections"}
    )
    plain = await callback(client, state="unknown", code="c0de")
    assert (plain.status_code, plain.text) == (200, "result=invalid_state")


async def test_oauth_over_a_key_deletes_the_key_with_its_versions(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    assert (await put_token(client, admin_key)).status_code == 200
    assert name in bao.kv

    state = await started_state(client, admin_key)
    bao.fail("DELETE", "kv/metadata/")
    failed = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT
    )
    assert outcome(failed)["reason"] == ["secret_store_unavailable"]
    # The new creds and server were removed again; the connection keeps its key.
    assert name not in bao.creds and bao.servers == {}
    body = await card(client, admin_key)
    assert (body["status"], body["auth"]) == ("active", "token")
    assert body["statusReason"] == "secret_store_unavailable"

    state = await started_state(client, admin_key)
    done = await callback(
        client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT
    )
    assert outcome(done)["result"] == ["active"]
    assert name not in bao.kv
    assert f"kv/metadata/{name}" in bao.paths("DELETE")
    assert f"kv/data/{name}" not in bao.paths("DELETE")
    body = await card(client, admin_key)
    assert (body["auth"], body["secretRef"]) == ("oauth2", f"oauth2/creds/{name}")
    authorized = await events(client, admin_key, "connection.authorized")
    assert [event["payload"]["previousStatus"] for event in authorized] == ["pending", "active"]


# --- the key (§7) ---------------------------------------------------------------------------------


async def test_a_key_goes_to_kv_and_the_connection_becomes_active(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    token = "key-" + "T" * 30
    expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)

    response = await put_token(client, admin_key, token=token, expiresAt=expires.isoformat())
    assert response.status_code == 200, response.text
    assert token not in response.text
    body = response.json()
    assert (body["status"], body["auth"], body["account"]) == ("active", "token", ACCOUNT)
    assert body["secretRef"] == f"kv/data/{name}"
    assert datetime.fromisoformat(body["expiresAt"]) == expires
    assert body["connectedBy"] == admin["adminPrincipal"]["id"]
    assert bao.kv[name].data == {"access_token": token, "expires_at": expires.isoformat()}

    [event] = await events(client, admin_key, "connection.authorized")
    assert event["payload"]["auth"] == "token"
    assert event["payload"]["previousStatus"] == "pending"

    replaced = await put_token(client, admin_key, token="key-2-" + "U" * 30)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["expiresAt"] is None
    assert bao.kv[name].data["access_token"] == "key-2-" + "U" * 30
    assert bao.kv[name].version == 2


async def test_a_key_refusals_change_nothing(
    client: httpx.AsyncClient, app: FastAPI, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    cases: list[tuple[dict[str, Any], int, str | None]] = [
        ({"account": "evil.example"}, 422, "invalid_account"),
        ({"expiresAt": past}, 422, "invalid_expiry"),
        ({"token": ""}, 400, None),
        ({"token": None}, 400, None),
        ({"token": 5}, 400, None),
        ({"account": ""}, 400, None),
        ({"account": "a" * 254}, 400, None),
        ({"expiresAt": "2030-01-01T00:00:00"}, 400, None),  # no offset
        ({"token": "k" * 8193}, 400, None),
    ]
    for body, status, code in cases:
        response = await put_token(client, admin_key, **body)
        assert response.status_code == status, (body, response.text)
        if code is not None:
            assert response.json()["error"]["code"] == code
        assert "K" * 30 not in response.text

    oauth_only = spec_with("auth", ["oauth2"])
    published = await publish(client, admin_key, type_key="oauth-only", spec=oauth_only)
    assert published.status_code == 201, published.text
    assert (await create(client, admin_key, type="oauth-only", key="oauth-only")).status_code == 201
    unsupported = await put_token(client, admin_key, connection="oauth-only")
    assert unsupported.status_code == 422, unsupported.text
    assert unsupported.json()["error"]["code"] == "auth_not_supported"

    _principal, _key_id, reader = await human(client, admin_key, "reader", ["connections.read"])
    assert (await put_token(client, reader)).status_code == 403
    assert (await put_token(client, admin_key, connection="nope")).status_code == 404

    bao.fail("POST", "kv/data/tenants/", 503)
    sealed = await put_token(client, admin_key)
    assert sealed.status_code == 503, sealed.text

    app.state.secret_store = None
    no_store = await put_token(client, admin_key)
    assert no_store.status_code == 503, no_store.text
    assert no_store.json()["error"]["code"] == "secret_store_unavailable"

    assert bao.kv == {}
    body = await card(client, admin_key)
    assert (body["status"], body["version"]) == ("pending", 1)
    assert await events(client, admin_key, "connection.authorized") == []


async def test_a_key_over_oauth_removes_the_creds_and_server_first(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    state = await started_state(client, admin_key)
    await callback(client, state=state, code=bao.token_server.issue(TOKEN_URL), referer=ACCOUNT)
    assert name in bao.creds
    server = bao.creds[name]["server"]

    bao.fail("DELETE", "oauth2/creds/")
    failed = await put_token(client, admin_key)
    assert failed.status_code == 503, failed.text
    # The key written first is removed again: one material at every point.
    assert name not in bao.kv
    assert f"kv/metadata/{name}" in bao.paths("DELETE")
    body = await card(client, admin_key)
    assert (body["auth"], body["secretRef"]) == ("oauth2", f"oauth2/creds/{name}")

    done = await put_token(client, admin_key)
    assert done.status_code == 200, done.text
    assert name not in bao.creds and bao.servers == {}
    assert name in bao.kv
    deletes = bao.paths("DELETE")
    assert deletes.index(f"oauth2/creds/{name}") < deletes.index(f"oauth2/servers/{server}")
    assert (done.json()["auth"], done.json()["secretRef"]) == ("token", f"kv/data/{name}")


async def test_a_replayed_key_write_keeps_the_first_key(
    client: httpx.AsyncClient, bao: FakeOpenBao
) -> None:
    admin_key, admin = await _admin(client)
    name = f"tenants/{admin['tenant']['id']}/connections/crm"
    await _crm(client, admin_key)
    headers = {"Idempotency-Key": "token-crm"}
    first = await put_token(client, admin_key, headers=headers, token="first-" + "A" * 30)
    assert first.status_code == 200, first.text
    replay = await put_token(client, admin_key, headers=headers, token="second-" + "B" * 30)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert bao.kv[name].data["access_token"] == "first-" + "A" * 30
    assert len(await events(client, admin_key, "connection.authorized")) == 1


# --- no value outside the store (§14, SC-006) -----------------------------------------------------


async def test_no_value_of_the_callback_or_of_a_key_reaches_events_logs_or_tables(
    client: httpx.AsyncClient,
    sync_engine: Engine,
    bao: FakeOpenBao,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (
        await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)
    ).status_code == 201
    assert (await create(client, admin_key, type="tracker")).status_code == 201
    headers = {"Idempotency-Key": "app"}
    assert (await set_app(client, admin_key, headers=headers)).status_code == 200

    values: list[str] = [CLIENT_SECRET]
    # A refused exchange: the provider's text echoes the code and the secret.
    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)
    bao.token_server.refuse_with = "invalid_client"
    await callback(client, state=state, code=code, referer=ACCOUNT)
    values += [state, code]
    bao.token_server.refuse_with = None

    state = await started_state(client, admin_key)
    code = bao.token_server.issue(TOKEN_URL)
    await callback(client, state=state, code=code, referer=ACCOUNT, extra="x-" + "E" * 20)
    values += [state, code, "x-" + "E" * 20]
    creds = next(iter(bao.creds.values()))
    values += [creds["access_token"], creds["refresh_token"]]

    key = "tracker-key-" + "Z" * 30
    put = await put_token(
        client,
        admin_key,
        connection="tracker",
        account="acme",
        token=key,
        headers={"Idempotency-Key": "key"},
    )
    assert put.status_code == 200, put.text
    values.append(key)
    await callback(client, state=values[1], code=values[2])  # a replay

    database = dump_database(sync_engine)
    everything = await client.get("/api/v1/events", params={"limit": 200}, headers=auth(admin_key))
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    # The log is not empty: the replay above is a warning of the callback.
    assert "connection_callback_invalid_state" in logged
    for value in values:
        assert value not in database, value
        assert value not in everything.text, value
        assert value not in logged, value
    # The store is where they are.
    assert CLIENT_SECRET in bao.dump() and key in bao.dump()


# --- review tails of I009 -------------------------------------------------------------------------


async def test_a_credential_shaped_settings_member_name_is_refused_and_not_quoted(
    client: httpx.AsyncClient,
) -> None:
    admin_key, _ = await _admin(client)
    strict = spec_with(
        "settingsSchema",
        {
            "type": "object",
            "properties": {"pipelineId": {"type": "integer"}},
            "additionalProperties": False,
        },
    )
    assert (await publish(client, admin_key, spec=strict)).status_code == 201
    name = "ghp_" + "a" * 36

    created = await create(client, admin_key, settings={name: 1})
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "secret_material_rejected"
    assert created.json()["error"]["details"] == {
        "field": "settings",
        "errors": [{"path": "/", "match": "provider_token"}],
    }
    assert name not in created.text

    nested = await create(client, admin_key, settings={"pipelineId": 1, "x": {name: 1}})
    assert nested.status_code == 422, nested.text
    assert nested.json()["error"]["details"]["errors"] == [
        {"path": "/x", "match": "provider_token"}
    ]
    assert name not in nested.text


# --- states past a day (§6) ----------------------------------------------------------------


async def test_the_worker_removes_states_older_than_a_day(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, bao: FakeOpenBao
) -> None:
    admin_key, _ = await _admin(client)
    await _crm(client, admin_key)
    assert (await set_app(client, admin_key)).status_code == 200
    await started_state(client, admin_key)
    await started_state(client, admin_key)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE connection_oauth_states SET created_at = now() - interval '25 hours'"
                " WHERE outcome = 'superseded'"
            )
        )

    worker = Worker(settings)
    try:
        assert (await worker.run_once())["oauth_states_cleaned"] == 1
        assert (await worker.run_once())["oauth_states_cleaned"] == 0
    finally:
        await worker.engine.dispose()
    [live] = state_rows(sync_engine)
    assert live["outcome"] is None
