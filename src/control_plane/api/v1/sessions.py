"""Session endpoints."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.errors import error_body
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PageOut,
    SessionHeartbeatRequest,
    SessionOpenRequest,
    SessionOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import sessions as commands
from control_plane.application.queries import lists as queries

router = APIRouter(tags=["sessions"])


# visibility: tenant — a session is the caller's own
@router.post(
    "/sessions",
    response_model=SessionOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def open_session(
    payload: SessionOpenRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    harness = None
    if payload.harness is not None:
        harness = commands.HarnessSpec(
            harness_type=payload.harness.type,
            harness_version=payload.harness.version,
            protocol_version=payload.harness.protocol_version,
            capabilities=payload.harness.capabilities,
            hostname=payload.harness.hostname,
            environment=payload.harness.environment,
        )

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        work_session = await commands.open_session(
            db,
            ctx,
            settings,
            client_name=payload.client_name,
            client_version=payload.client_version,
            metadata=payload.metadata,
            on_behalf_of_id=payload.on_behalf_of,
            ttl_seconds=payload.ttl_seconds,
            harness=harness,
        )
        return 201, dump(SessionOut, work_session)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — a session is the caller's own
@router.get("/sessions", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_sessions(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_sessions(db, ctx, limit=limit, cursor=cursor, status=status)
    return JSONResponse(page_body([dump(SessionOut, s) for s in page.items], page.next_cursor))


# visibility: tenant — a session is the caller's own
@router.get("/sessions/{session_id}", response_model=SessionOut, responses=ERROR_RESPONSES)
async def get_session(session_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    work_session = await queries.get_session(db, ctx, session_id)
    return JSONResponse(dump(SessionOut, work_session))


# visibility: tenant — a session is the caller's own
@router.post(
    "/sessions/{session_id}:heartbeat",
    response_model=SessionOut,
    responses=ERROR_RESPONSES,
)
async def heartbeat_session(
    session_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: SessionHeartbeatRequest | None = None,
) -> JSONResponse:
    ttl_seconds = payload.ttl_seconds if payload else None

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        work_session = await commands.heartbeat_session(
            db, ctx, settings, session_id=session_id, ttl_seconds=ttl_seconds
        )
        if work_session.status != "active":
            # The command marked the expired session stale; that transition must
            # commit, so the conflict is returned as a value, not an exception.
            return 409, error_body(
                "session_expired",
                "Session lease has expired",
                details={
                    "sessionId": str(session_id),
                    "expiresAt": work_session.expires_at.isoformat(),
                },
            )
        return 200, dump(SessionOut, work_session)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
    )


# visibility: tenant — a session is the caller's own
@router.post(
    "/sessions/{session_id}:close",
    response_model=SessionOut,
    responses=ERROR_RESPONSES,
)
async def close_session(
    session_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        work_session = await commands.close_session(db, ctx, session_id=session_id)
        return 200, dump(SessionOut, work_session)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
