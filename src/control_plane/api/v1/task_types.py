"""Work item type registry endpoints (ADR-0048).

Shaped like ``/project-templates``: a POST creates the NEXT version of a key,
never edits one, and ``:deprecate`` retires a version from resolution without
touching the tasks that already carry it.
"""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    PageOut,
    TaskTypeCreateRequest,
    TaskTypeExecutorOut,
    TaskTypeExecutorsOut,
    TaskTypeMigrateTasksOut,
    TaskTypeMigrateTasksRequest,
    TaskTypeOut,
    dump,
    page_body,
    work_document,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import task_type_migration as migration_commands
from control_plane.application.commands import task_types as commands
from control_plane.application.commands.workspaces import effective_task_types, get_tenant_workspace
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import (
    attach_package,
    attach_packages,
    in_package,
)
from control_plane.application.queries.task_type_executors import list_task_type_executors
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import TaskType

router = APIRouter(tags=["task-types"])


# visibility: tenant — task types are objects of the tenant
@router.post(
    "/task-types",
    response_model=TaskTypeOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create the next immutable version of a work item type",
)
async def create_task_type(
    payload: TaskTypeCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task_type = await commands.create_task_type_version(
            db,
            ctx,
            key=payload.key,
            display_name=payload.display_name,
            description=payload.description,
            field_schema=payload.field_schema,
            lifecycle_schema=payload.lifecycle_schema,
            execution=payload.execution,
            approval_schema=payload.approval_schema,
            context_schema=payload.context_schema,
            instructions=payload.instructions,
            completion_schema=payload.completion_schema,
            artifact_schema=payload.artifact_schema,
            acceptance=work_document(payload.acceptance),
            executor_roles=payload.executor_roles,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "TaskType", dump(TaskTypeOut, task_type)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — task types are objects of the tenant
@router.get("/task-types", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_task_types(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
    workspace_id: uuid.UUID | None = Query(
        default=None,
        alias="workspaceId",
        description="Only the types allowed in this workspace: its effectiveTaskTypes "
        "(CP-ADR-0008, amendment 2026-10-03 A2)",
    ),
) -> JSONResponse:
    await authorize(ctx, Permission.TASK_TYPES_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(TaskType).where(TaskType.tenant_id == ctx.tenant_id)
    if workspace_id is not None:
        # A workspace of another tenant is as absent as a missing one: 404.
        await get_tenant_workspace(db, ctx, workspace_id)
        allowed = await effective_task_types(db, ctx.tenant_id, workspace_id)
        if allowed is not None:
            stmt = stmt.where(TaskType.key.in_(allowed))
    if key is not None:
        stmt = stmt.where(TaskType.key == key)
    if status is not None:
        stmt = stmt.where(TaskType.status == status)
    if package is not None:
        stmt = stmt.where(in_package("TaskType", TaskType.tenant_id, TaskType.key, package))
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (TaskType.created_at < created_at)
            | ((TaskType.created_at == created_at) & (TaskType.id < entity_id))
        )
    stmt = stmt.order_by(TaskType.created_at.desc(), TaskType.id.desc()).limit(effective_limit + 1)
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    items = [dump(TaskTypeOut, t) for t in rows]
    await attach_packages(db, ctx.tenant_id, "TaskType", items)
    return JSONResponse(page_body(items, next_cursor))


# visibility: tenant — task types are objects of the tenant
@router.get("/task-types/{type_id}", response_model=TaskTypeOut, responses=ERROR_RESPONSES)
async def get_task_type(type_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    await authorize(ctx, Permission.TASK_TYPES_READ)
    body = dump(TaskTypeOut, await commands.get_tenant_task_type(db, ctx, type_id))
    return JSONResponse(await attach_package(db, ctx.tenant_id, "TaskType", body))


@router.get(
    "/task-types/{type_id}/executors",
    response_model=TaskTypeExecutorsOut,
    responses=ERROR_RESPONSES,
    summary="Who may take this task type version in a workspace (ADR-0048, 2026-10-03 A2)",
)
async def list_executors(
    type_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    workspace_id: uuid.UUID = Query(alias="workspaceId"),
) -> JSONResponse:
    executors = await list_task_type_executors(db, ctx, type_id, workspace_id)
    items = [
        TaskTypeExecutorOut.model_validate(e).model_dump(mode="json", by_alias=True)
        for e in executors
    ]
    return JSONResponse({"items": items})


# visibility: tenant — task types are objects of the tenant
@router.post(
    "/task-types/{type_id}:deprecate", response_model=TaskTypeOut, responses=ERROR_RESPONSES
)
async def deprecate_task_type(
    type_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task_type = await commands.deprecate_task_type(db, ctx, type_id=type_id)
        return 200, await attach_package(
            db, ctx.tenant_id, "TaskType", dump(TaskTypeOut, task_type)
        )

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


@router.post(
    "/task-types/{type_id}:migrate-tasks",
    response_model=TaskTypeMigrateTasksOut,
    responses=ERROR_RESPONSES,
    summary="Move the open tasks of this version to another version of its key (ADR-0048)",
)
async def migrate_type_tasks(
    type_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: TaskTypeMigrateTasksRequest | None = None,
) -> JSONResponse:
    body = payload or TaskTypeMigrateTasksRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await migration_commands.migrate_type_tasks(
            db,
            ctx,
            type_id=type_id,
            to_version=body.to_version,
            status_map=body.status_map,
            limit=body.limit,
            cursor=body.cursor,
        )
        return 200, result

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )
