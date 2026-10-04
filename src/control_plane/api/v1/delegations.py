"""Delegations endpoints."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    DelegationCreateRequest,
    DelegationOut,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import delegations as commands
from control_plane.application.queries import lists as queries

router = APIRouter(tags=["delegations"])


# visibility: tenant — a delegation links two principals and has no workspace (CP-ADR-0082 4)
@router.post(
    "/delegations",
    response_model=DelegationOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def create_delegation(
    payload: DelegationCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        delegation = await commands.create_delegation(
            db,
            ctx,
            human_principal_id=payload.human_principal_id,
            agent_principal_id=payload.agent_principal_id,
            permissions=payload.permissions,
            starts_at=payload.starts_at,
            expires_at=payload.expires_at,
        )
        return 201, dump(DelegationOut, delegation)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — a delegation links two principals and has no workspace (CP-ADR-0082 4)
@router.get("/delegations", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_delegations(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_delegations(db, ctx, limit=limit, cursor=cursor)
    return JSONResponse(page_body([dump(DelegationOut, d) for d in page.items], page.next_cursor))


# visibility: tenant — a delegation links two principals and has no workspace (CP-ADR-0082 4)
@router.post(
    "/delegations/{delegation_id}:revoke",
    response_model=DelegationOut,
    responses=ERROR_RESPONSES,
)
async def revoke_delegation(
    delegation_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        delegation = await commands.revoke_delegation(db, ctx, delegation_id=delegation_id)
        return 200, dump(DelegationOut, delegation)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
