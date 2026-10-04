"""Operator endpoints: adapter diagnostics/redrive and journal retention.

Split from the rest of the API on purpose: these actions need their own
``operations.*`` permissions rather than riding on a general write scope.
"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    AdapterRebuildRequest,
    AdapterRedriveRequest,
    JournalArchiveRequest,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import operations as commands

router = APIRouter(tags=["operations"], prefix="/operations")


# visibility: tenant — operations on the journal and the memory adapter of the whole tenant
@router.get("/context-adapter", responses=ERROR_RESPONSES)
async def context_adapter_status(ctx: AuthDep, db: DbDep) -> JSONResponse:
    return JSONResponse(await commands.adapter_diagnostics(db, ctx))


# visibility: tenant — operations on the journal and the memory adapter of the whole tenant
@router.post("/context-adapter/{tenant_id}:redrive", responses=ERROR_RESPONSES)
async def redrive_context_adapter(
    tenant_id: uuid.UUID,
    payload: AdapterRedriveRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Un-park the tenant and retry the same position. Never skips an event."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.redrive_adapter(db, ctx, tenant_id=tenant_id, reason=payload.reason)
        return 200, await commands.adapter_diagnostics(db, ctx)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — operations on the journal and the memory adapter of the whole tenant
@router.post("/context-adapter/{tenant_id}:rebuild", responses=ERROR_RESPONSES)
async def rebuild_context_adapter(
    tenant_id: uuid.UUID,
    payload: AdapterRebuildRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Rewind the tenant cursor so memory is rebuilt from the journal."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.rebuild_adapter(
            db, ctx, tenant_id=tenant_id, cursor=payload.cursor, reason=payload.reason
        )
        return 200, await commands.adapter_diagnostics(db, ctx)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — operations on the journal and the memory adapter of the whole tenant
@router.post("/journal:archive", responses=ERROR_RESPONSES)
async def archive_journal(
    payload: JournalArchiveRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.archive_journal(
            db,
            ctx,
            settings,
            before_seconds=payload.before_seconds,
            max_events=payload.max_events,
        )
        return 200, result

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — operations on the journal and the memory adapter of the whole tenant
@router.post("/journal:prune", responses=ERROR_RESPONSES)
async def prune_journal(
    payload: JournalArchiveRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Delete archived events for good — the only lossy retention action."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.prune_journal(
            db,
            ctx,
            settings,
            before_seconds=payload.before_seconds,
            max_events=payload.max_events,
        )
        return 200, result

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
