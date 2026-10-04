"""Workspace type endpoints: the tenant's node-type registry (ADR-0029)."""

import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    PageOut,
    WorkspaceTypeCreateRequest,
    WorkspaceTypeOut,
    WorkspaceTypeUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import workspace_types as commands
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import (
    attach_package,
    attach_packages,
    in_package,
)
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import WorkspaceType

router = APIRouter(tags=["workspace-types"])


# visibility: tenant — workspace types are objects of the tenant
@router.post(
    "/workspace-types",
    response_model=WorkspaceTypeOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def create_workspace_type(
    payload: WorkspaceTypeCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace_type = await commands.create_workspace_type(
            db,
            ctx,
            key=payload.key,
            display_name=payload.display_name,
            description=payload.description,
            field_schema=payload.field_schema,
            allowed_child_types=payload.allowed_child_types,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "WorkspaceType", dump(WorkspaceTypeOut, workspace_type)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — workspace types are objects of the tenant
@router.get("/workspace-types", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_workspace_types(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    await authorize(ctx, Permission.WORKSPACES_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(WorkspaceType).where(WorkspaceType.tenant_id == ctx.tenant_id)
    if status is not None:
        stmt = stmt.where(WorkspaceType.status == status)
    if package is not None:
        stmt = stmt.where(
            in_package("WorkspaceType", WorkspaceType.tenant_id, WorkspaceType.key, package)
        )
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (WorkspaceType.created_at < created_at)
            | ((WorkspaceType.created_at == created_at) & (WorkspaceType.id < entity_id))
        )
    stmt = stmt.order_by(WorkspaceType.created_at.desc(), WorkspaceType.id.desc()).limit(
        effective_limit + 1
    )
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    items = [dump(WorkspaceTypeOut, t) for t in rows]
    await attach_packages(db, ctx.tenant_id, "WorkspaceType", items)
    return JSONResponse(page_body(items, next_cursor))


# visibility: tenant — workspace types are objects of the tenant
@router.get(
    "/workspace-types/{type_id}", response_model=WorkspaceTypeOut, responses=ERROR_RESPONSES
)
async def get_workspace_type(type_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    await authorize(ctx, Permission.WORKSPACES_READ)
    workspace_type = await commands.get_tenant_workspace_type(db, ctx, type_id)
    return JSONResponse(
        await attach_package(
            db, ctx.tenant_id, "WorkspaceType", dump(WorkspaceTypeOut, workspace_type)
        ),
        headers={"ETag": format_etag("workspace_type", workspace_type.version)},
    )


# visibility: tenant — workspace types are objects of the tenant
@router.patch(
    "/workspace-types/{type_id}", response_model=WorkspaceTypeOut, responses=ERROR_RESPONSES
)
async def update_workspace_type(
    type_id: uuid.UUID,
    payload: WorkspaceTypeUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "workspace_type")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace_type = await commands.update_workspace_type(
            db,
            ctx,
            type_id=type_id,
            expected_version=expected_version,
            display_name=payload.display_name,
            description=payload.description,
            field_schema=payload.field_schema,
            allowed_child_types=payload.allowed_child_types,
        )
        return 200, await attach_package(
            db, ctx.tenant_id, "WorkspaceType", dump(WorkspaceTypeOut, workspace_type)
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


# visibility: tenant — workspace types are objects of the tenant
@router.post(
    "/workspace-types/{type_id}:archive",
    response_model=WorkspaceTypeOut,
    responses=ERROR_RESPONSES,
)
async def archive_workspace_type(
    type_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace_type = await commands.archive_workspace_type(db, ctx, type_id=type_id)
        return 200, await attach_package(
            db, ctx.tenant_id, "WorkspaceType", dump(WorkspaceTypeOut, workspace_type)
        )

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
