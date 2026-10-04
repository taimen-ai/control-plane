"""Explicit context observation endpoint: the governed "remember" primitive."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ObservationCreateRequest,
    ObservationRecordedOut,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import observations as commands

router = APIRouter(tags=["observations"])


@router.post(
    "/observations",
    response_model=ObservationRecordedOut,
    status_code=201,
    responses={
        **ERROR_RESPONSES,
        200: {
            "model": ObservationRecordedOut,
            "description": "Deduplicated: (source, dedupKey) matched an existing observation",
        },
    },
)
async def record_observation(
    payload: ObservationCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        recorded = await commands.record_observation(
            db,
            ctx,
            kind=payload.kind,
            content=payload.content,
            data=payload.data,
            assertions=payload.assertions,
            task_ref=payload.task,
            run_id=payload.run_id,
            workspace_id=payload.workspace_id,
            session_id=payload.session_id,
            source=payload.source,
            dedup_key=payload.dedup_key,
            observed_at=payload.observed_at,
            supersedes=payload.supersedes,
            external_ref=(
                payload.external_ref.model_dump() if payload.external_ref is not None else None
            ),
        )
        # A (source, dedupKey) repeated by its author is not a creation: 200
        # with the observation the key first produced (CP-ADR-0057).
        return 200 if recorded.deduplicated else 201, {
            "id": str(recorded.id),
            "eventId": str(recorded.event_id),
            "kind": recorded.kind,
            "recordedAt": recorded.recorded_at.isoformat(),
            "deduplicated": recorded.deduplicated,
        }

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
