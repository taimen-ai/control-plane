"""Access to connections: the OAuth application, the OAuth flow, the key (CP-ADR-0079 §5-§7).

The material — the code of the provider, the application's secret, the key of
a connection — passes through the core in transit and lands in the secret
store only. No table, event, log line or answer carries it; the tables keep
the path (``secretRef``), the state as a hash and codes of what went wrong.

- ``PUT /connection-types/{key}/oauth-app`` writes the application of a type
  into ``kv/data/platform/oauth-apps/<type>`` under a check-and-set of the
  version it read: the document remembers the tenant that wrote it, and
  another tenant neither overwrites it nor sends its secret anywhere.
- ``POST /connections/{key}:authorize`` issues a one-time state bound to the
  caller's principal and credential, and answers the provider's address.
- ``GET /connections:callback`` (public: the state is the authentication)
  consumes the state first, in its own transaction, then acts with the
  authority of the credential the state was issued to: the plugin exchanges
  the code, the connection becomes ``active``. A refusal of any step keeps the
  status and says why (``statusReason``, ``connection.authorization_failed``).
- ``PUT /connections/{key}/token`` writes a key into ``kv/data``; over
  ``oauth2`` the creds and the server go first, so a connection never has two
  materials.

A secret store that is not configured or not reachable is ``503
secret_store_unavailable``; nothing falls back to keeping material elsewhere.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.approval_outcomes import (
    authority_snapshot,
    require_active_credential,
)
from control_plane.application.commands.connection_policies import sync_tenant
from control_plane.application.commands.connections import connection_by_key, usable_type_version
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.config import Settings, is_https_url
from control_plane.domain.connection_access import (
    account_problem,
    authorize_url,
    connection_store_name,
    kv_connection_ref,
    new_state,
    oauth_app_path,
    oauth_creds_ref,
    oauth_server_name,
    provider_text,
    state_hash,
    token_url,
)
from control_plane.domain.enums import ConnectionAuth, ConnectionStatus, Permission
from control_plane.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import Connection, ConnectionOAuthState, ConnectionType
from control_plane.infrastructure.secret_store import (
    SecretStore,
    SecretStoreConflict,
    SecretStoreError,
    SecretStoreRejected,
)

logger = logging.getLogger(__name__)

MAX_STATE_LENGTH = 200

# ``statusReason`` codes of a callback that did not connect (§6).
REASON_CONSENT_DENIED = "consent_denied"
REASON_PROVIDER_ERROR = "provider_error"
REASON_INVALID_ACCOUNT = "invalid_account"
REASON_INITIATOR_NOT_AUTHORIZED = "initiator_not_authorized"
REASON_OAUTH_EXCHANGE_FAILED = "oauth_exchange_failed"
REASON_OAUTH_APP_NOT_CONFIGURED = "oauth_app_not_configured"
REASON_AUTH_NOT_SUPPORTED = "auth_not_supported"
REASON_SECRET_STORE_UNAVAILABLE = "secret_store_unavailable"


def secret_store_unavailable(reason: str | None = None) -> DependencyUnavailableError:
    details: dict[str, Any] = {"retryable": True}
    if reason is not None:
        details["reason"] = reason
    return DependencyUnavailableError(
        "The secret store is unavailable; nothing was changed",
        code="secret_store_unavailable",
        details=details,
    )


def require_store(store: SecretStore | None) -> SecretStore:
    if store is None:
        raise secret_store_unavailable("not_configured")
    return store


def _auth_not_supported(connection_type: ConnectionType, auth: str) -> ValidationError:
    return ValidationError(
        "auth_not_supported",
        f"The connection type does not support {auth}",
        details={"type": connection_type.key, "typeVersion": connection_type.version, "auth": auth},
    )


# --- the OAuth application of a type (§5) -------------------------------------------------


@dataclass(frozen=True)
class OAuthAppView:
    """What an answer says of the application: never its secret."""

    type_key: str
    configured: bool
    client_id: str | None
    updated_at: datetime | None


async def _type_versions(
    session: AsyncSession, ctx: AuthContext, type_key: str
) -> list[ConnectionType]:
    rows = list(
        await session.scalars(
            select(ConnectionType)
            .where(ConnectionType.tenant_id == ctx.tenant_id, ConnectionType.key == type_key)
            .order_by(ConnectionType.version.desc())
        )
    )
    if not rows:
        raise NotFoundError("Connection type not found", details={"type": type_key})
    return rows


async def read_oauth_app(
    store: SecretStore, tenant_id: uuid.UUID, type_key: str
) -> dict[str, Any] | None:
    """The application of the type, if the tenant wrote it; another tenant's is none."""
    document = await store.kv_read(oauth_app_path(type_key))
    if document is None or document.data.get("tenant_id") != str(tenant_id):
        return None
    return document.data


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def set_oauth_app(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    *,
    type_key: str,
    client_id: str,
    client_secret: str,
) -> OAuthAppView:
    """``PUT /connection-types/{key}/oauth-app``: the secret goes to the store only.

    The owner check and the write are one operation of the store: the write
    carries ``cas`` = the version that was read, so of two tenants writing
    one key at once the second gets ``409 oauth_app_write_conflict``, and its
    retry reads the first one's document (``oauth_app_owned_by_other_tenant``).
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    versions = await _type_versions(session, ctx, type_key)
    if not any("oauth2" in (row.spec.get("auth") or []) for row in versions):
        raise _auth_not_supported(versions[0], "oauth2")
    secret_store = require_store(store)
    path = oauth_app_path(type_key)
    now = utcnow()
    try:
        current = await secret_store.kv_read(path)
        if current is not None and current.data.get("tenant_id") != str(ctx.tenant_id):
            raise ConflictError(
                "oauth_app_owned_by_other_tenant",
                "The OAuth application of this type key belongs to another tenant",
                details={"type": type_key},
            )
        cas = (
            current.version if current is not None else await secret_store.kv_current_version(path)
        )
        await secret_store.kv_write(
            path,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "tenant_id": str(ctx.tenant_id),
                "updated_by": str(ctx.principal_id),
                "updated_at": now.isoformat(),
            },
            cas=cas,
        )
    except SecretStoreConflict as exc:
        raise ConflictError(
            "oauth_app_write_conflict",
            "The OAuth application was written concurrently; read it again and retry",
            details={"type": type_key, "retryable": True},
        ) from exc
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection_type.oauth_app_set",
        entity_type="connection_type",
        entity_id=versions[0].id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"type": type_key, "created": current is None},
    )
    return OAuthAppView(type_key, True, client_id, now)


async def get_oauth_app(
    session: AsyncSession, ctx: AuthContext, store: SecretStore | None, *, type_key: str
) -> OAuthAppView:
    """``GET /connection-types/{key}/oauth-app``: whether it is set, and its client id."""
    await authorize(ctx, Permission.CONNECTIONS_READ)
    await _type_versions(session, ctx, type_key)
    secret_store = require_store(store)
    try:
        app = await read_oauth_app(secret_store, ctx.tenant_id, type_key)
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    if app is None:
        return OAuthAppView(type_key, False, None, None)
    return OAuthAppView(
        type_key, True, str(app.get("client_id") or ""), _parse_time(app.get("updated_at"))
    )


# --- :authorize (§6) ------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizeResult:
    authorize_url: str
    expires_at: datetime


async def authorize_connection(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    settings: Settings,
    *,
    key: str,
) -> AuthorizeResult:
    """``POST /connections/{key}:authorize``: a one-time state and the provider's address.

    Only the SHA-256 of the state is written, with the caller's principal and
    the snapshot of its credential. Live states of the connection issued
    before are superseded: the last one who started answers for it. The
    status of the connection does not change.
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    connection = await connection_by_key(session, ctx, key, for_update=True)
    connection_type = await usable_type_version(
        session, ctx, connection.type_key, connection.type_version
    )
    spec = connection_type.spec
    if "oauth2" not in (spec.get("auth") or []) or not isinstance(spec.get("oauth2"), dict):
        raise _auth_not_supported(connection_type, "oauth2")
    missing = [
        name
        for name, value in (
            ("CP_OAUTH_REDIRECT_URI", settings.oauth_redirect_uri),
            ("CP_CONNECTIONS_RETURN_URL", settings.connections_return_url),
        )
        if not value
    ]
    if missing:
        raise ConflictError(
            "oauth_not_configured",
            "OAuth is not configured in this installation",
            details={"missing": missing},
        )
    insecure = [
        name
        for name, value in (
            ("CP_OAUTH_REDIRECT_URI", settings.oauth_redirect_uri),
            ("CP_CONNECTIONS_RETURN_URL", settings.connections_return_url),
        )
        if not is_https_url(value)
    ]
    if insecure:
        raise ConflictError(
            "oauth_not_configured",
            "OAuth addresses of this installation are not https",
            details={"insecure": insecure},
        )
    secret_store = require_store(store)
    try:
        app = await read_oauth_app(secret_store, ctx.tenant_id, connection.type_key)
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    if app is None:
        raise ConflictError(
            "oauth_app_not_configured",
            "The OAuth application of the connection type is not set",
            details={"type": connection.type_key},
        )
    now = utcnow()
    await session.execute(
        update(ConnectionOAuthState)
        .where(
            ConnectionOAuthState.connection_id == connection.id,
            ConnectionOAuthState.consumed_at.is_(None),
        )
        .values(consumed_at=now, outcome="superseded")
    )
    state = new_state()
    expires_at = now + timedelta(seconds=settings.oauth_state_ttl_seconds)
    session.add(
        ConnectionOAuthState(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            connection_id=connection.id,
            principal_id=ctx.principal_id,
            authority=authority_snapshot(ctx),
            state_hash=state_hash(state),
            created_at=now,
            expires_at=expires_at,
            consumed_at=None,
            outcome=None,
        )
    )
    await session.flush()
    return AuthorizeResult(
        authorize_url(
            spec,
            client_id=str(app["client_id"]),
            state=state,
            redirect_uri=settings.oauth_redirect_uri,
        ),
        expires_at,
    )


# --- the callback (§6) ------------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackOutcome:
    """How a callback ended: ``active``, ``failed`` or ``invalid_state``."""

    result: str
    connection_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _LiveState:
    id: uuid.UUID
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    principal_id: uuid.UUID
    authority: dict[str, Any]


class _Refused(Exception):
    """A step after the state was consumed refused: the status stays, the reason is kept."""

    def __init__(self, reason: str, message: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


async def _consume_state(
    session_factory: async_sessionmaker[AsyncSession], state: str | None
) -> _LiveState | None:
    """Step 1-2: the state is consumed first, in its own transaction.

    A second arrival of the same state finds it consumed even if everything
    after this fails: the provider's code is one-time too.
    """
    if not state or len(state) > MAX_STATE_LENGTH:
        return None
    now = utcnow()
    async with transaction(session_factory) as session:
        row: ConnectionOAuthState | None = await session.scalar(
            select(ConnectionOAuthState)
            .where(ConnectionOAuthState.state_hash == state_hash(state))
            .with_for_update()
        )
        if row is None or row.consumed_at is not None or row.expires_at <= now:
            return None
        row.consumed_at = now
        row.outcome = "consumed"
        return _LiveState(
            id=row.id,
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            principal_id=row.principal_id,
            authority=dict(row.authority),
        )


def _initiator_context(live: _LiveState, request_id: str) -> AuthContext:
    """The authority of the credential the state was issued to (as ``_decider_context``)."""
    authority = live.authority
    iam = authority.get("iamPrincipalId")
    return AuthContext(
        tenant_id=live.tenant_id,
        principal_id=live.principal_id,
        principal_kind=str(authority.get("principalKind") or "human"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=request_id,
        correlation_id=f"connection-oauth:{live.id}",
        iam_principal_id=uuid.UUID(str(iam)) if iam else None,
    )


async def _check_initiator(session: AsyncSession, live: _LiveState, ctx: AuthContext) -> None:
    """Step 3: the credential still stands and still may manage connections."""
    try:
        await require_active_credential(
            session,
            authority=live.authority,
            principal_id=live.principal_id,
            subject="that started the authorization",
        )
        await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    except DomainError as exc:
        raise _Refused(
            REASON_INITIATOR_NOT_AUTHORIZED,
            "The credential that started the authorization is no longer authorized to"
            " manage connections",
        ) from exc


def _provider_answer(params: Mapping[str, str]) -> str:
    """Step 4: the code, or the refusal the provider sent instead."""
    error = params.get("error")
    code = params.get("code")
    if error or not code:
        reason = REASON_CONSENT_DENIED if error == "access_denied" else REASON_PROVIDER_ERROR
        message = provider_text(params.get("error_description") or error) or (
            "The provider returned no authorization code"
        )
        raise _Refused(reason, message)
    return code


def _account_of(spec: dict[str, Any], params: Mapping[str, str]) -> str | None:
    """Step 5: the account from ``oauth2.accountParam``, by the checks of §2.

    The refusal never quotes the account: it came from the query, not from
    the provider's answer the core trusts.
    """
    oauth2 = spec["oauth2"]
    account_param = oauth2.get("accountParam")
    account = params.get(account_param) if isinstance(account_param, str) else None
    if account is None:
        if "{account}" in str(oauth2.get("tokenUrlTemplate")):
            raise _Refused(
                REASON_INVALID_ACCOUNT, "The provider did not name the account of the connection"
            )
        return None
    problem = account_problem(spec, account)
    if problem is not None:
        raise _Refused(REASON_INVALID_ACCOUNT, f"The account is not valid: {problem}")
    return account


async def _forget_server(store: SecretStore, server: str, connection_key: str) -> None:
    """Delete an OAuth server no creds go through; a failure leaves it, and says so."""
    try:
        await store.oauth_delete_server(server)
    except SecretStoreError:
        logger.warning(
            "connection_oauth_server_orphaned",
            extra={
                "connection": connection_key,
                "server": server,
                "error_code": "secret_store_unavailable",
            },
        )


async def _exchange(
    store: SecretStore | None,
    settings: Settings,
    live: _LiveState,
    connection: Connection,
    spec: dict[str, Any],
    *,
    code: str,
    account: str | None,
) -> str:
    """Step 6: the OAuth server and the code exchange in ``oauth2/``, done by the plugin.

    The server is the attempt's own (``…/connections/<key>/<state id>``): the
    one the creds of the connection refresh through is not touched until the
    exchange succeeded, so a callback naming another account never points the
    current refresh token and the application's secret at another host. A
    failed attempt deletes its server. After a successful exchange the server
    of the previous authorization is deleted; from ``token`` to ``oauth2``
    the key of the connection is deleted with every version, and if that
    fails, the new creds and server are removed again and the connection
    stays ``token``. Answers the name of the new server.
    """
    if store is None:
        raise _Refused(REASON_SECRET_STORE_UNAVAILABLE, "The secret store is not configured")
    name = connection_store_name(live.tenant_id, connection.key)
    server = oauth_server_name(live.tenant_id, connection.key, live.id)
    client_secret = ""
    written = False
    try:
        app = await read_oauth_app(store, live.tenant_id, connection.type_key)
        if app is None:
            raise _Refused(
                REASON_OAUTH_APP_NOT_CONFIGURED,
                "The OAuth application of the connection type is not set",
            )
        client_secret = str(app.get("client_secret") or "")
        oauth2 = spec["oauth2"]
        written = True
        await store.oauth_put_server(
            server,
            client_id=str(app.get("client_id") or ""),
            client_secret=client_secret,
            auth_code_url=str(oauth2["authorizeUrl"]),
            token_url=token_url(spec, account),
            auth_style=str(oauth2["authStyle"]),
        )
        await store.oauth_exchange_code(
            name, server=server, code=code, redirect_url=settings.oauth_redirect_uri
        )
    except SecretStoreRejected as exc:
        if written:
            await _forget_server(store, server, connection.key)
        raise _Refused(
            REASON_OAUTH_EXCHANGE_FAILED,
            provider_text("; ".join(exc.messages), withhold=(code, client_secret))
            or "The authorization code was not exchanged",
        ) from exc
    except SecretStoreError as exc:
        if written:
            await _forget_server(store, server, connection.key)
        raise _Refused(
            REASON_SECRET_STORE_UNAVAILABLE, f"The secret store is unavailable ({exc.reason})"
        ) from exc
    if connection.auth == ConnectionAuth.TOKEN:
        try:
            await store.kv_delete_all(name)
        except SecretStoreError as exc:
            try:
                await store.oauth_delete_creds(name)
            except SecretStoreError:
                logger.warning(
                    "connection_oauth_rollback_failed",
                    extra={"connection": connection.key, "error_code": "secret_store_unavailable"},
                )
            await _forget_server(store, server, connection.key)
            raise _Refused(
                REASON_SECRET_STORE_UNAVAILABLE,
                "The key of the connection could not be removed; start the authorization again",
            ) from exc
    elif connection.oauth_server is not None and connection.oauth_server != server:
        await _forget_server(store, connection.oauth_server, connection.key)
    return server


async def _record(
    session: AsyncSession,
    live: _LiveState,
    connection: Connection,
    request_id: str,
    *,
    refused: _Refused | None,
    account: str | None,
    server: str | None,
) -> CallbackOutcome:
    """Steps 7-8: the connection and its event, in the transaction that holds its lock."""
    state_row = await session.get(ConnectionOAuthState, live.id)
    assert state_row is not None
    now = utcnow()
    common: dict[str, Any] = {
        "tenant_id": live.tenant_id,
        "entity_type": "connection",
        "entity_id": connection.id,
        "actor_id": live.principal_id,
        "request_id": request_id,
        "correlation_id": f"connection-oauth:{live.id}",
    }
    if refused is None:
        previous = connection.status
        connection.status = ConnectionStatus.ACTIVE
        connection.auth = ConnectionAuth.OAUTH2
        connection.account = account
        connection.secret_ref = oauth_creds_ref(live.tenant_id, connection.key)
        connection.oauth_server = server
        connection.expires_at = None
        connection.connected_by = live.principal_id
        connection.connected_at = now
        connection.status_reason = None
        connection.status_message = None
        connection.version += 1
        connection.updated_at = now
        state_row.outcome = "authorized"
        await session.flush()
        await record_event(
            session,
            event_type="connection.authorized",
            payload={
                "key": connection.key,
                "type": connection.type_key,
                "auth": ConnectionAuth.OAUTH2.value,
                "previousStatus": previous,
                "connectedBy": str(live.principal_id),
            },
            **common,
        )
        return CallbackOutcome("active", connection.key)
    connection.status_reason = refused.reason
    connection.status_message = refused.message
    connection.version += 1
    connection.updated_at = now
    state_row.outcome = "failed"
    await session.flush()
    await record_event(
        session,
        event_type="connection.authorization_failed",
        payload={
            "key": connection.key,
            "type": connection.type_key,
            "reason": refused.reason,
            "initiatedBy": str(live.principal_id),
        },
        **common,
    )
    return CallbackOutcome("failed", connection.key, refused.reason)


async def _attempt(
    session: AsyncSession,
    store: SecretStore | None,
    settings: Settings,
    live: _LiveState,
    connection: Connection,
    params: Mapping[str, str],
    request_id: str,
) -> tuple[str | None, str]:
    """Steps 3-6 over the locked connection: the account and the new server."""
    connection_type = await session.scalar(
        select(ConnectionType).where(
            ConnectionType.tenant_id == connection.tenant_id,
            ConnectionType.key == connection.type_key,
            ConnectionType.version == connection.type_version,
        )
    )
    assert connection_type is not None  # a foreign key of the connection
    spec = dict(connection_type.spec)
    try:
        ctx = _initiator_context(live, request_id)
    except (KeyError, ValueError) as exc:
        raise _Refused(
            REASON_INITIATOR_NOT_AUTHORIZED,
            "The authorization carries no credential of its initiator",
        ) from exc
    await _check_initiator(session, live, ctx)
    code = _provider_answer(params)
    if "oauth2" not in (spec.get("auth") or []) or not isinstance(spec.get("oauth2"), dict):
        raise _Refused(REASON_AUTH_NOT_SUPPORTED, "The connection type does not support oauth2")
    account = _account_of(spec, params)
    server = await _exchange(store, settings, live, connection, spec, code=code, account=account)
    return account, server


async def handle_callback(
    session_factory: async_sessionmaker[AsyncSession],
    store: SecretStore | None,
    settings: Settings,
    *,
    params: Mapping[str, str],
    request_id: str,
) -> CallbackOutcome:
    """``GET /connections:callback``: the provider's answer, applied to the state's connection.

    Nothing of ``params`` is logged or kept but codes: an unknown, consumed or
    expired state writes nothing and names no connection. After the state is
    consumed, the connection stays locked (``FOR UPDATE``) through the
    exchange until its record: ``PUT …/token`` and another callback of the
    connection wait, so the ``auth`` the exchange acted on is the one the
    record replaces, and a connection never ends with two materials.
    """
    live = await _consume_state(session_factory, params.get("state"))
    if live is None:
        logger.warning("connection_callback_invalid_state")
        return CallbackOutcome("invalid_state")
    async with transaction(session_factory) as session:
        connection = await session.get(
            Connection, live.connection_id, with_for_update=True, populate_existing=True
        )
        assert connection is not None  # a state references its connection
        account: str | None = None
        server: str | None = None
        refused: _Refused | None = None
        try:
            account, server = await _attempt(
                session, store, settings, live, connection, params, request_id
            )
        except _Refused as exc:
            refused = exc
        return await _record(
            session,
            live,
            connection,
            request_id,
            refused=refused,
            account=account,
            server=server,
        )


# --- the key of a connection (§7) ---------------------------------------------------------------


async def set_connection_token(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    *,
    key: str,
    account: str,
    token: str,
    expires_at: datetime | None,
) -> Connection:
    """``PUT /connections/{key}/token``: the key goes to ``kv/data`` in transit.

    Over ``oauth2`` the order keeps one material at every point of failure:
    the key is written, then the creds and the server are deleted (a failure
    there removes the key again), then the account names the new path.
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    connection = await connection_by_key(session, ctx, key, for_update=True)
    connection_type = await usable_type_version(
        session, ctx, connection.type_key, connection.type_version
    )
    spec = connection_type.spec
    if "token" not in (spec.get("auth") or []):
        raise _auth_not_supported(connection_type, "token")
    problem = account_problem(spec, account)
    if problem is not None:
        raise ValidationError(
            "invalid_account",
            f"The account is not valid: {problem}",
            details={"field": "account"},
        )
    now = utcnow()
    if expires_at is not None and expires_at <= now:
        raise ValidationError(
            "invalid_expiry", "expiresAt is in the future", details={"field": "expiresAt"}
        )
    secret_store = require_store(store)
    name = connection_store_name(ctx.tenant_id, connection.key)
    try:
        await secret_store.kv_write(
            name,
            {
                "access_token": token,
                "expires_at": expires_at.isoformat() if expires_at is not None else None,
            },
        )
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    if connection.auth == ConnectionAuth.OAUTH2:
        try:
            await secret_store.oauth_delete_creds(name)
            if connection.oauth_server is not None:
                await secret_store.oauth_delete_server(connection.oauth_server)
        except SecretStoreError as exc:
            try:
                await secret_store.kv_delete_all(name)
            except SecretStoreError:
                logger.warning(
                    "connection_token_rollback_failed",
                    extra={"connection": connection.key, "error_code": "secret_store_unavailable"},
                )
            raise secret_store_unavailable(exc.reason) from exc
    previous = connection.status
    connection.status = ConnectionStatus.ACTIVE
    connection.auth = ConnectionAuth.TOKEN
    connection.account = account
    connection.secret_ref = kv_connection_ref(ctx.tenant_id, connection.key)
    connection.oauth_server = None
    connection.expires_at = expires_at
    connection.connected_by = ctx.principal_id
    connection.connected_at = now
    connection.status_reason = None
    connection.status_message = None
    connection.version += 1
    connection.updated_at = now
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection.authorized",
        entity_type="connection",
        entity_id=connection.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": connection.key,
            "type": connection.type_key,
            "auth": ConnectionAuth.TOKEN.value,
            "previousStatus": previous,
            "connectedBy": str(ctx.principal_id),
        },
    )
    return connection


# --- :revoke (§10) ------------------------------------------------------------------------------


async def revoke_connection(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    *,
    key: str,
    reason: str | None,
) -> Connection:
    """``POST /connections/{key}:revoke``: the material and the agents' access go first.

    Under the connection's lock: the creds and the OAuth server, or the key
    with every version, are deleted; then the policies of the agents that
    name the connection are brought to the records in which it is already
    ``revoked`` (the change is flushed, not committed, so a failure of the
    store rolls it back). Only then does the transaction commit the status,
    the live states are put out and ``connection.revoked`` is recorded. A
    failure of the store is ``503`` with the status unchanged; whatever was
    deleted stays deleted, and a repeat finishes the work. Revoking a revoked
    connection is ``200`` and records nothing.
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    connection = await connection_by_key(session, ctx, key, for_update=True)
    if connection.status == ConnectionStatus.REVOKED:
        return connection
    # Without a store there is no material and no policy to take back, so a
    # connection that never had material is revoked in the records alone.
    secret_store = store if store is None and connection.auth is None else require_store(store)
    name = connection_store_name(ctx.tenant_id, connection.key)
    previous = connection.status
    now = utcnow()
    try:
        if connection.auth is not None:
            assert secret_store is not None
            if connection.auth == ConnectionAuth.OAUTH2:
                await secret_store.oauth_delete_creds(name)
                if connection.oauth_server is not None:
                    await secret_store.oauth_delete_server(connection.oauth_server)
            else:
                await secret_store.kv_delete_all(name)
        connection.status = ConnectionStatus.REVOKED
        connection.auth = None
        connection.secret_ref = None
        connection.oauth_server = None
        connection.expires_at = None
        connection.status_reason = None
        connection.status_message = (
            provider_text(reason) if reason is not None and reason.strip() else None
        )
        connection.version += 1
        connection.updated_at = now
        await session.flush()
        if secret_store is not None:
            await sync_tenant(session, secret_store, ctx.tenant_id, naming=connection.key)
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    await session.execute(
        update(ConnectionOAuthState)
        .where(
            ConnectionOAuthState.connection_id == connection.id,
            ConnectionOAuthState.consumed_at.is_(None),
        )
        .values(consumed_at=now, outcome="superseded")
    )
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection.revoked",
        entity_type="connection",
        entity_id=connection.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": connection.key, "type": connection.type_key, "previousStatus": previous},
    )
    return connection
