"""Harness protocol endpoints (v0.3): bootstrap context and work discovery.

``GET /harness/context`` answers "who am I / where am I / what am I doing /
what can I do / what happened" in one round-trip — the startup and recovery
path of every harness. ``GET /work/available`` is advisory discovery: the
claim remains the only authoritative gate.
"""

import uuid

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, DbDep
from control_plane.api.v1.schemas import ERROR_RESPONSES, PageOut, page_body
from control_plane.api.v1.task_bodies import task_bodies
from control_plane.application.queries import discovery as discovery_queries
from control_plane.application.queries import harness as harness_queries

router = APIRouter(tags=["harness"])


@router.get("/harness/context", responses=ERROR_RESPONSES)
async def get_harness_context(
    ctx: AuthDep,
    db: DbDep,
    session_id: uuid.UUID | None = Query(default=None, alias="sessionId"),
) -> JSONResponse:
    context = await harness_queries.get_harness_context(db, ctx, session_id=session_id)
    return JSONResponse(context)


@router.get(
    "/work/available",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Tasks the calling principal could claim right now (advisory)",
)
async def list_available_work(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    include_descendants: bool = Query(default=False, alias="includeDescendants"),
    project_id: uuid.UUID | None = Query(default=None, alias="projectId"),
    include_subprojects: bool = Query(default=False, alias="includeSubprojects"),
    assignee_id: uuid.UUID | None = Query(default=None, alias="assigneeId"),
    assigned_to_me: bool = Query(default=False, alias="assignedToMe"),
    type_key: list[str] | None = Query(
        default=None,
        alias="typeKey",
        description=(
            "Only tasks of these work item types (repeatable; a key matches every "
            "version of the type). Narrows the queue and nothing else: permissions, "
            "visibility and eligibility are unchanged. Combines with every other "
            "filter and the cursor. A blank key or more than "
            f"{discovery_queries.MAX_TYPE_KEYS} keys is 422 invalid_type_key."
        ),
    ),
) -> JSONResponse:
    # assignedToMe is the honest way for a worker to ask for its own queue: it
    # needs no knowledge of its own Principal id, and it cannot be pointed at
    # someone else's work by a stale configuration value.
    if assigned_to_me:
        assignee_id = ctx.principal_id
    tasks, next_cursor = await discovery_queries.list_available_work(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        workspace_id=workspace_id,
        include_descendants=include_descendants,
        project_id=project_id,
        include_subprojects=include_subprojects,
        assignee_id=assignee_id,
        type_keys=type_key,
    )
    return JSONResponse(page_body(await task_bodies(db, ctx, tasks), next_cursor))
