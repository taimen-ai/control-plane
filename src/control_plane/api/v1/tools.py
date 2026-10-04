"""Scoped Tool Discovery endpoints (HRS-3).

``GET /tools`` is the bounded projection a harness searches instead of pasting
a whole MCP catalog into a prompt; ``GET /tools/{toolRef}`` loads the one schema
it is about to use. Neither grants anything: execution is authorized again when
the action is recorded.

The response carries the revisions it was computed from, and the page's
``viewHash`` doubles as its ETag, so a client can cache a projection and find
out cheaply — and correctly — when the catalog or the policy moved underneath
it.
"""

import uuid

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, DbDep
from control_plane.api.etag import none_match
from control_plane.api.v1.schemas import ERROR_RESPONSES
from control_plane.application.queries import tool_policy as tool_queries

router = APIRouter(tags=["tools"])


# visibility: tenant — tools are objects of the tenant
@router.get(
    "/tools",
    responses=ERROR_RESPONSES,
    summary="Tools this principal may use right now (bounded projection)",
)
async def search_tools(
    ctx: AuthDep,
    db: DbDep,
    query: str | None = Query(default=None),
    run_id: uuid.UUID | None = Query(default=None, alias="runId"),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    page = await tool_queries.search_tools(
        db, ctx, query=query, run_id=run_id, limit=limit, cursor=cursor
    )
    view_hash = page["view"]["viewHash"]
    if none_match(if_none_match, view_hash):
        # The projection is unchanged only because BOTH revisions are unchanged
        # — the hash covers catalog, policy and the query itself, so a 304 can
        # never mean "we did not check".
        return Response(status_code=304, headers={"ETag": f'"{view_hash}"'})
    return JSONResponse(page, headers={"ETag": f'"{view_hash}"'})


# visibility: tenant — tools are objects of the tenant
@router.get(
    "/tools/{tool_ref:path}",
    responses=ERROR_RESPONSES,
    summary="Full sanitized projection of one tool",
)
async def describe_tool(
    ctx: AuthDep,
    db: DbDep,
    tool_ref: str,
    run_id: uuid.UUID | None = Query(default=None, alias="runId"),
) -> JSONResponse:
    detail = await tool_queries.describe_tool(db, ctx, tool_ref=tool_ref, run_id=run_id)
    return JSONResponse(detail)
