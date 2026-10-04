"""Artifact type registry endpoints (CP-ADR-0072 §6).

Shaped like ``/task-types``: a POST creates the NEXT version of a key, never
edits one. A type is addressed by its key — ``{key}`` is the latest version,
``{key}@{version}`` an exact one — because that is how an artifact names it.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    ArtifactTypeCreateRequest,
    ArtifactTypeOut,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import artifact_types as commands
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import (
    attach_package,
    attach_packages,
    in_package,
)
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import ArtifactType

router = APIRouter(tags=["artifact-types"])


# visibility: tenant — artifact types are objects of the tenant
@router.post(
    "/artifact-types",
    response_model=ArtifactTypeOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create the next immutable version of an artifact type",
)
async def create_artifact_type(
    payload: ArtifactTypeCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        artifact_type = await commands.create_artifact_type_version(
            db,
            ctx,
            key=payload.key,
            display_name=payload.display_name,
            description=payload.description,
            metadata_schema=payload.metadata_schema,
            media_types=payload.media_types,
            max_bytes=payload.max_bytes,
            global_max_bytes=settings.artifact_max_bytes,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "ArtifactType", dump(ArtifactTypeOut, artifact_type)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — artifact types are objects of the tenant
@router.get("/artifact-types", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_artifact_types(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    await authorize(ctx, Permission.ARTIFACT_TYPES_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(ArtifactType).where(ArtifactType.tenant_id == ctx.tenant_id)
    if key is not None:
        stmt = stmt.where(ArtifactType.key == key)
    if status is not None:
        stmt = stmt.where(ArtifactType.status == status)
    if package is not None:
        stmt = stmt.where(
            in_package("ArtifactType", ArtifactType.tenant_id, ArtifactType.key, package)
        )
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ArtifactType.created_at < created_at)
            | ((ArtifactType.created_at == created_at) & (ArtifactType.id < entity_id))
        )
    stmt = stmt.order_by(ArtifactType.created_at.desc(), ArtifactType.id.desc()).limit(
        effective_limit + 1
    )
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    items = [dump(ArtifactTypeOut, t) for t in rows]
    await attach_packages(db, ctx.tenant_id, "ArtifactType", items)
    return JSONResponse(page_body(items, next_cursor))


# visibility: tenant — artifact types are objects of the tenant
@router.get("/artifact-types/{ref}", response_model=ArtifactTypeOut, responses=ERROR_RESPONSES)
async def get_artifact_type(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    """``key`` — the latest version; ``key@version`` — that version."""
    await authorize(ctx, Permission.ARTIFACT_TYPES_READ)
    body = dump(ArtifactTypeOut, await commands.resolve_artifact_type(db, ctx, ref))
    return JSONResponse(await attach_package(db, ctx.tenant_id, "ArtifactType", body))
