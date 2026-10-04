"""Workspace endpoints: hierarchy, archive, move, members."""

import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PageOut,
    WorkspaceCreateRequest,
    WorkspaceDetailOut,
    WorkspaceMemberOut,
    WorkspaceMemberRequest,
    WorkspaceMoveRequest,
    WorkspaceOut,
    WorkspaceParticipantOut,
    WorkspaceUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.authorization import AuthContext
from control_plane.application.commands import workspaces as commands
from control_plane.application.queries import org as queries
from control_plane.application.queries import projects as project_queries
from control_plane.infrastructure.db.models import Workspace

router = APIRouter(tags=["workspaces"])


def _out(ctx: AuthContext, workspace: Workspace) -> dict[str, object]:
    """A workspace as the caller sees it: the parent of a visible root whose
    parent is not visible is not named (CP-ADR-0082 §3.9)."""
    data = dump(WorkspaceOut, workspace)
    if not ctx.sees_workspace(workspace.parent_id):
        data["parentId"] = None
    return data


async def _detail(db: AsyncSession, ctx: AuthContext, workspace: Workspace) -> dict[str, object]:
    """One workspace with its settings resolved through the tree (CP-ADR-0008 A1)."""
    effective = await commands.effective_task_types(db, ctx.tenant_id, workspace.id)
    data = dump(WorkspaceDetailOut, workspace, effectiveTaskTypes=effective)
    if not ctx.sees_workspace(workspace.parent_id):
        data["parentId"] = None
    return data


@router.post(
    "/workspaces", response_model=WorkspaceDetailOut, status_code=201, responses=ERROR_RESPONSES
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.create_workspace(
            db,
            ctx,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
            type_id=payload.type_id,
            type_key=payload.type_key,
            custom_fields=payload.custom_fields,
        )
        return 201, await _detail(db, ctx, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/workspaces", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_workspaces(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    parent_id: uuid.UUID | None = Query(default=None, alias="parentId"),
    roots_only: bool = Query(default=False, alias="rootsOnly"),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_workspaces(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        parent_id=parent_id,
        roots_only=roots_only,
        status=status,
    )
    return JSONResponse(page_body([_out(ctx, w) for w in page.items], page.next_cursor))


@router.get("/workspaces/tree", responses=ERROR_RESPONSES)
async def get_workspace_tree(
    ctx: AuthDep,
    db: DbDep,
    root_id: uuid.UUID | None = Query(default=None, alias="rootId"),
    depth: int | None = Query(default=None, ge=0, le=64),
    include_archived: bool = Query(default=False, alias="includeArchived"),
    include_projects: bool = Query(default=True, alias="includeProjects"),
) -> JSONResponse:
    """The whole (sub)tree in one query, siblings in stable ``(slug, id)`` order."""
    roots = await project_queries.workspace_tree(
        db,
        ctx,
        root_id=root_id,
        depth=depth,
        include_archived=include_archived,
        include_projects=include_projects,
    )
    return JSONResponse({"roots": roots})


@router.get(
    "/workspaces/{workspace_id}", response_model=WorkspaceDetailOut, responses=ERROR_RESPONSES
)
async def get_workspace(workspace_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    workspace = await queries.get_workspace(db, ctx, workspace_id)
    return JSONResponse(
        await _detail(db, ctx, workspace),
        headers={"ETag": format_etag("workspace", workspace.version)},
    )


@router.patch(
    "/workspaces/{workspace_id}", response_model=WorkspaceDetailOut, responses=ERROR_RESPONSES
)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "workspace")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.update_workspace(
            db,
            ctx,
            workspace_id=workspace_id,
            expected_version=expected_version,
            name=payload.name,
            description=payload.description,
            slug=payload.slug,
            type_id=payload.type_id,
            type_key=payload.type_key,
            custom_fields=payload.custom_fields,
            task_types=(
                payload.task_types if "task_types" in payload.model_fields_set else commands.UNSET
            ),
        )
        return 200, await _detail(db, ctx, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/workspaces/{workspace_id}:archive",
    response_model=WorkspaceDetailOut,
    responses=ERROR_RESPONSES,
)
async def archive_workspace(
    workspace_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.archive_workspace(db, ctx, workspace_id=workspace_id)
        return 200, await _detail(db, ctx, workspace)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


@router.post(
    "/workspaces/{workspace_id}:move",
    response_model=WorkspaceDetailOut,
    responses=ERROR_RESPONSES,
)
async def move_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceMoveRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.move_workspace(
            db, ctx, workspace_id=workspace_id, new_parent_id=payload.new_parent_id
        )
        return 200, await _detail(db, ctx, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def add_member(
    workspace_id: uuid.UUID,
    payload: WorkspaceMemberRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        member = await commands.add_workspace_member(
            db, ctx, workspace_id=workspace_id, principal_id=payload.principal_id
        )
        return 201, dump(WorkspaceMemberOut, member)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/workspaces/{workspace_id}/members", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_members(
    workspace_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_workspace_members(db, ctx, workspace_id, limit=limit, cursor=cursor)
    return JSONResponse(
        page_body([dump(WorkspaceMemberOut, m) for m in page.items], page.next_cursor)
    )


@router.get(
    "/workspaces/{workspace_id}/participants", response_model=PageOut, responses=ERROR_RESPONSES
)
async def list_participants(
    workspace_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_workspace_participants(
        db, ctx, workspace_id, limit=limit, cursor=cursor
    )
    items = [
        WorkspaceParticipantOut.model_validate(p).model_dump(mode="json", by_alias=True)
        for p in page.items
    ]
    return JSONResponse(page_body(items, page.next_cursor))


@router.post(
    "/workspaces/{workspace_id}/members/{principal_id}:remove",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def remove_member(
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.remove_workspace_member(
            db, ctx, workspace_id=workspace_id, principal_id=principal_id
        )
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )
