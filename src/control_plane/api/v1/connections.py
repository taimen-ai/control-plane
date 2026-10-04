"""Connection endpoints (CP-ADR-0079 §3, §17).

A connection is the tenant's account of an external system, created
``pending`` and keyed for good. ``GET /connections/{key}`` adds the agents
whose current revision names it; ``PATCH`` takes ``If-Match``; ``PUT
…/status`` is the connector's report. ``:authorize``, the callback and ``PUT
…/token`` reach the secret store (§6, §7): its material passes through them
in transit and never comes back in an answer. ``:revoke`` deletes the
material and takes the agents' access back before the status says so (§10).
An agent reads what it needs of its own connections under ``/agents/me`` (§8).
An agent's secrets by name (§11) pass the core the same way: ``PUT`` sends the
value to the store, ``GET`` answers the names only.
"""

from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    DbDep,
    SecretStoreDep,
    SessionFactoryDep,
    SettingsDep,
)
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.strict_query import open_query
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    SECRET_STORE_ERROR_RESPONSES,
    AgentConnectionListOut,
    AgentConnectionOut,
    AgentSecretListOut,
    AgentSecretOut,
    AgentSecretSetRequest,
    ConnectionAuthorizeOut,
    ConnectionAuthorizeRequest,
    ConnectionCreateRequest,
    ConnectionOut,
    ConnectionRevokeRequest,
    ConnectionStatusReport,
    ConnectionTokenRequest,
    ConnectionUpdateRequest,
    PageOut,
    page_body,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import agent_secrets
from control_plane.application.commands import connection_access as access
from control_plane.application.commands import connections as commands
from control_plane.config import is_https_url
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import AgentSecretName, Connection

router = APIRouter(tags=["connections"])

ETAG_ENTITY = "connection"


def connection_body(row: Connection, agents: list[str] | None = None) -> dict[str, Any]:
    """``ConnectionOut``; ``agents`` only on the card."""
    body = ConnectionOut.model_validate(
        {
            "id": row.id,
            "key": row.key,
            "type": row.type_key,
            "type_version": row.type_version,
            "display_name": row.display_name,
            "account": row.account,
            "auth": row.auth,
            "status": row.status,
            "status_reason": row.status_reason,
            "status_message": row.status_message,
            "settings": row.settings,
            "secret_ref": row.secret_ref,
            "expires_at": row.expires_at,
            "connected_by": row.connected_by,
            "connected_at": row.connected_at,
            "last_checked_at": row.last_checked_at,
            "agents": agents,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "version": row.version,
        }
    ).model_dump(mode="json", by_alias=True)
    if agents is None:
        del body["agents"]
    return body


def _etag(row: Connection) -> dict[str, str]:
    return {"ETag": format_etag(ETAG_ENTITY, row.version)}


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.post(
    "/connections",
    response_model=ConnectionOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create a connection; it waits for authorization",
)
async def create_connection(
    payload: ConnectionCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await commands.create_connection(
            db,
            ctx,
            type_key=payload.type,
            key=payload.key,
            display_name=payload.display_name,
            settings=payload.settings,
        )
        return 201, connection_body(row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.get("/connections", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_connections(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    type: str | None = Query(default=None),
    status: Literal["pending", "active", "expired", "revoked"] | None = Query(default=None),
) -> JSONResponse:
    page = await commands.list_connections(
        db, ctx, limit=limit, cursor=cursor, type_key=type, status=status
    )
    return JSONResponse(page_body([connection_body(row) for row in page.items], page.next_cursor))


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.get("/connections/{key}", response_model=ConnectionOut, responses=ERROR_RESPONSES)
async def get_connection(key: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    view = await commands.get_connection(db, ctx, key)
    return JSONResponse(
        connection_body(view.connection, view.agents), headers=_etag(view.connection)
    )


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.patch(
    "/connections/{key}",
    response_model=ConnectionOut,
    responses=ERROR_RESPONSES,
    summary="Change the display name, settings or type version of a connection",
)
async def update_connection(
    key: str,
    payload: ConnectionUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, ETAG_ENTITY)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await commands.update_connection(
            db, ctx, key=key, expected_version=expected_version, changes=payload.changes()
        )
        return 200, connection_body(row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.put(
    "/connections/{key}/status",
    response_model=ConnectionOut,
    responses=ERROR_RESPONSES,
    summary="Report that access still works or has expired (the connector only)",
)
async def report_connection_status(
    key: str,
    payload: ConnectionStatusReport,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.CONNECTIONS_STATUS_WRITE)
    report = commands.StatusReport(
        status=payload.status,
        reason=payload.reason,
        message=payload.message,
        checked_at=payload.checked_at,
    )

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await commands.report_connection_status(db, ctx, key=key, report=report)
        return 200, connection_body(row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# --- access: OAuth and the key (CP-ADR-0079 §6, §7) -------------------------------------

# The callback's answer and the authorize URL carry a state or a code: nothing
# caches them, and the console page does not pass the code on in ``Referer``.
_NO_STORE = {"Cache-Control": "no-store"}
_CALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.post(
    "/connections/{key}:authorize",
    response_model=ConnectionAuthorizeOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Start OAuth: a one-time state and the provider's address for consent",
)
async def authorize_connection(
    key: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
    payload: ConnectionAuthorizeRequest | None = None,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await access.authorize_connection(db, ctx, store, settings, key=key)
        body = ConnectionAuthorizeOut(
            authorize_url=result.authorize_url, expires_at=result.expires_at
        ).model_dump(mode="json", by_alias=True)
        return 200, body

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body="{}",
        executor=executor,
        # The URL carries the state: a replay answers null, the state is
        # kept hashed only.
        sensitive_fields=("authorizeUrl",),
    )
    response.headers.update(_NO_STORE)
    return response


def _with_query(url: str, params: dict[str, str]) -> str:
    base, _hash, _fragment = url.partition("#")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(params)}"


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
# authz: public — одноразовый state
@router.get(
    "/connections:callback",
    status_code=303,
    response_class=RedirectResponse,
    summary="The provider returns the browser here; the one-time state authenticates it",
    responses={
        303: {
            "description": "To CP_CONNECTIONS_RETURN_URL with connection, result and reason",
            "headers": {
                "Location": {"schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string"}},
                "Referrer-Policy": {"schema": {"type": "string"}},
            },
        },
        200: {
            "description": "CP_CONNECTIONS_RETURN_URL is empty: result and reason as text",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
    },
    openapi_extra={
        "parameters": [
            {"name": name, "in": "query", "required": False, "schema": {"type": "string"}}
            for name in ("state", "code", "error", "error_description")
        ]
        + [
            {
                "name": "<accountParam>",
                "in": "query",
                "required": False,
                "description": "The parameter oauth2.accountParam of the connection type names",
                "schema": {"type": "string"},
            }
        ]
    },
)
@open_query
async def connection_oauth_callback(
    request: Request,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
) -> Response:
    """CP-ADR-0079 §6: the state is consumed first; the answer names no value of the query."""
    outcome = await access.handle_callback(
        session_factory,
        store,
        settings,
        params=request.query_params,
        request_id=request.state.request_id,
    )
    return_url = settings.connections_return_url
    if not is_https_url(return_url):
        text = f"result={outcome.result}"
        if outcome.reason is not None:
            text += f" reason={outcome.reason}"
        return PlainTextResponse(text, headers=_CALLBACK_HEADERS)
    query: dict[str, str] = {}
    if outcome.connection_key is not None:
        query["connection"] = outcome.connection_key
    query["result"] = outcome.result
    if outcome.reason is not None:
        query["reason"] = outcome.reason
    return RedirectResponse(
        _with_query(return_url, query), status_code=303, headers=_CALLBACK_HEADERS
    )


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.put(
    "/connections/{key}/token",
    response_model=ConnectionOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Connect with a key: the key goes to the secret store, the connection is active",
)
async def set_connection_token(
    key: str,
    payload: ConnectionTokenRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await access.set_connection_token(
            db,
            ctx,
            store,
            key=key,
            account=payload.account,
            token=payload.token,
            expires_at=payload.expires_at,
        )
        return 200, connection_body(row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        # The fingerprint of the request is taken without the key: a hash of
        # a secret in the database could be matched against a dictionary.
        canonical_body=payload.model_dump_json(exclude={"token"}, exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — a connection is an object of the tenant (CP-ADR-0082 4)
@router.post(
    "/connections/{key}:revoke",
    response_model=ConnectionOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Revoke: the material and the agents' access go, the connection is revoked",
)
async def revoke_connection(
    key: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
    payload: ConnectionRevokeRequest | None = None,
) -> JSONResponse:
    body = payload if payload is not None else ConnectionRevokeRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await access.revoke_connection(db, ctx, store, key=key, reason=body.reason)
        return 200, connection_body(row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# --- what an agent learns of its connections (CP-ADR-0079 §8) ------------------------------


def agent_connection_body(row: Connection) -> dict[str, Any]:
    return AgentConnectionOut.model_validate(
        {
            "key": row.key,
            "type": row.type_key,
            "type_version": row.type_version,
            "account": row.account,
            "auth": row.auth,
            "status": row.status,
            "settings": row.settings,
            "secret_ref": row.secret_ref,
            "expires_at": row.expires_at,
        }
    ).model_dump(mode="json", by_alias=True)


# visibility: tenant — the connections of a registry agent, a tenant object (CP-ADR-0082 3.8)
@router.get(
    "/agents/me/connections",
    response_model=AgentConnectionListOut,
    responses=ERROR_RESPONSES,
    summary="The connections the caller's agent names, without material",
)
async def list_my_connections(ctx: AuthDep, db: DbDep) -> JSONResponse:
    # Authentication is the whole check, as for /agents/me: an agent reads
    # what its own revision already names.
    rows = await commands.my_connections(db, ctx)
    return JSONResponse({"items": [agent_connection_body(row) for row in rows]})


# visibility: tenant — the connections of a registry agent, a tenant object (CP-ADR-0082 3.8)
@router.get(
    "/agents/me/connections/{key}",
    response_model=AgentConnectionOut,
    responses=ERROR_RESPONSES,
    summary="One connection the caller's agent names; any other key is not found",
)
async def get_my_connection(key: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    return JSONResponse(agent_connection_body(await commands.my_connection(db, ctx, key)))


# --- an agent's secrets by name (§11) ----------------------------------------------------


def agent_secret_body(row: AgentSecretName) -> dict[str, Any]:
    """``AgentSecretOut``: the name and who set it last, never the value."""
    return AgentSecretOut.model_validate(
        {"name": row.name, "updated_at": row.updated_at, "updated_by": row.updated_by}
    ).model_dump(mode="json", by_alias=True)


# visibility: tenant — the secrets of a registry agent, a tenant object (CP-ADR-0082 3.8)
@router.put(
    "/agents/{key}/secrets/{name}",
    response_model=AgentSecretOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Set an agent's secret by name: the value goes to the secret store",
)
async def set_agent_secret(
    key: str,
    name: str,
    payload: AgentSecretSetRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await agent_secrets.set_agent_secret(
            db, ctx, store, key=key, name=name, value=payload.value
        )
        return (201 if result.created else 200), agent_secret_body(result.row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        # Fingerprinted without the value, as ``PUT …/token``: a hash of a
        # secret in the database could be matched against a dictionary.
        canonical_body=payload.model_dump_json(exclude={"value"}),
        executor=executor,
    )


# visibility: tenant — the secrets of a registry agent, a tenant object (CP-ADR-0082 3.8)
@router.get(
    "/agents/{key}/secrets",
    response_model=AgentSecretListOut,
    responses=ERROR_RESPONSES,
    summary="The names of an agent's secrets, without values",
)
async def list_agent_secrets(key: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    rows = await agent_secrets.list_agent_secrets(db, ctx, key=key)
    return JSONResponse({"items": [agent_secret_body(row) for row in rows]})


# visibility: tenant — the secrets of a registry agent, a tenant object (CP-ADR-0082 3.8)
@router.delete(
    "/agents/{key}/secrets/{name}",
    status_code=204,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Delete an agent's secret with every version from the secret store",
)
async def delete_agent_secret(
    key: str,
    name: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await agent_secrets.delete_agent_secret(db, ctx, store, key=key, name=name)
        return 204, {}

    return as_no_content(
        await execute_write(
            request,
            ctx,
            settings,
            session_factory,
            canonical_body="",
            executor=executor,
        )
    )
