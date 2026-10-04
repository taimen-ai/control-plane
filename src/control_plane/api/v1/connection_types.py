"""Connection type endpoints (CP-ADR-0079 §2, §17).

A provider's package publishes a type: ``POST /connection-types`` with the
pair ``(key, version)`` the package names. ``{ref}`` is ``key`` — the latest
``active`` version — or ``key@version``; a status belongs to one version, so
``PATCH`` takes ``key@version`` only. The OAuth application of a type
(``…/{key}/oauth-app``, §5) lives in the secret store; its secret is written
there in transit and never answered.
"""

from typing import Any, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    DbDep,
    SecretStoreDep,
    SessionFactoryDep,
    SettingsDep,
)
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    SECRET_STORE_ERROR_RESPONSES,
    ConnectionTypeOut,
    ConnectionTypePublishRequest,
    ConnectionTypeUpdateRequest,
    OAuthAppOut,
    OAuthAppSetRequest,
    PageOut,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import connection_access as access
from control_plane.application.commands import connection_types as commands
from control_plane.application.queries.package_links import attach_package, attach_packages
from control_plane.infrastructure.db.models import ConnectionType

router = APIRouter(tags=["connections"])

ETAG_ENTITY = "connection-type"


def connection_type_body(row: ConnectionType) -> dict[str, Any]:
    """``ConnectionTypeOut`` without ``package``: an optional field not set is ``null``."""
    return ConnectionTypeOut.model_validate(
        {
            "id": row.id,
            "key": row.key,
            "version": row.version,
            "status": row.status,
            "spec": row.spec,
            "spec_hash": row.spec_hash,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "row_version": row.row_version,
        }
    ).model_dump(mode="json", by_alias=True)


def _etag(row: ConnectionType) -> dict[str, str]:
    return {"ETag": format_etag(ETAG_ENTITY, row.row_version)}


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.post(
    "/connection-types",
    response_model=ConnectionTypeOut,
    status_code=201,
    responses={
        **ERROR_RESPONSES,
        200: {"model": ConnectionTypeOut, "description": "The same version with the same spec"},
    },
    summary="Publish a version of a connection type; the same spec again changes nothing",
)
async def publish_connection_type(
    payload: ConnectionTypePublishRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        published = await commands.publish_connection_type(
            db, ctx, key=payload.key, version=payload.version, spec=payload.spec
        )
        body = await attach_package(
            db, ctx.tenant_id, commands.KIND, connection_type_body(published.connection_type)
        )
        return (201 if published.created else 200), body

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.get("/connection-types", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_connection_types(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    status: Literal["active", "deprecated", "disabled"] | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    page = await commands.list_connection_types(
        db, ctx, limit=limit, cursor=cursor, key=key, status=status, package=package
    )
    items = [connection_type_body(row) for row in page.items]
    await attach_packages(db, ctx.tenant_id, commands.KIND, items)
    return JSONResponse(page_body(items, page.next_cursor))


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.get("/connection-types/{ref}", response_model=ConnectionTypeOut, responses=ERROR_RESPONSES)
async def get_connection_type(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    """``key`` — the latest ``active`` version; ``key@version`` — that version."""
    row = await commands.resolve_connection_type(db, ctx, ref)
    body = await attach_package(db, ctx.tenant_id, commands.KIND, connection_type_body(row))
    return JSONResponse(body, headers=_etag(row))


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.patch(
    "/connection-types/{ref}",
    response_model=ConnectionTypeOut,
    responses=ERROR_RESPONSES,
    summary="Move the status of one version (key@version) forward",
)
async def update_connection_type(
    ref: str,
    payload: ConnectionTypeUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, ETAG_ENTITY)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        row = await commands.update_connection_type_status(
            db, ctx, ref=ref, expected_row_version=expected_version, status=payload.status
        )
        return 200, await attach_package(
            db, ctx.tenant_id, commands.KIND, connection_type_body(row)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


def _oauth_app_body(view: access.OAuthAppView) -> dict[str, Any]:
    return OAuthAppOut(
        type=view.type_key,
        configured=view.configured,
        client_id=view.client_id,
        updated_at=view.updated_at,
    ).model_dump(mode="json", by_alias=True)


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.put(
    "/connection-types/{key}/oauth-app",
    response_model=OAuthAppOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Set the OAuth application of a type; the secret goes to the secret store only",
)
async def set_connection_type_oauth_app(
    key: str,
    payload: OAuthAppSetRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: SecretStoreDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await access.set_oauth_app(
            db,
            ctx,
            store,
            type_key=key,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
        )
        return 200, _oauth_app_body(view)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        # Without the secret: its hash would rest in the database.
        canonical_body=payload.model_dump_json(exclude={"client_secret"}),
        executor=executor,
    )


# visibility: tenant — connection types are objects of the tenant (CP-ADR-0082 4)
@router.get(
    "/connection-types/{key}/oauth-app",
    response_model=OAuthAppOut,
    responses=SECRET_STORE_ERROR_RESPONSES,
    summary="Whether the OAuth application of a type is set, and its client id",
)
async def get_connection_type_oauth_app(
    key: str, ctx: AuthDep, db: DbDep, store: SecretStoreDep
) -> JSONResponse:
    view = await access.get_oauth_app(db, ctx, store, type_key=key)
    return JSONResponse(_oauth_app_body(view))
