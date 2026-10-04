"""Skill invocation API (ADR-0056 §2): invoke, read, executor operations."""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    ContentStoreDep,
    DbDep,
    SessionFactoryDep,
    SettingsDep,
)
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    SkillExecutionOut,
    SkillInvocationCancelRequest,
    SkillInvocationClaimRequest,
    SkillInvocationCompleteRequest,
    SkillInvocationFailRequest,
    SkillInvocationHeartbeatRequest,
    SkillInvocationOut,
    SkillInvokeRequest,
    dump,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.commands import skill_invocations as commands
from control_plane.application.queries.package_settings import object_settings
from control_plane.infrastructure.db.models import Skill, SkillInvocation

router = APIRouter(tags=["skills"])


def invocation_body(invocation: SkillInvocation, skill: Skill) -> dict[str, Any]:
    return dump(
        SkillInvocationOut,
        invocation,
        skill={"name": skill.name, "version": skill.version},
        requestedBy={"kind": invocation.requested_by_kind, "ref": invocation.requested_by_ref},
    )


@router.post(
    "/skills/{skill_ref}:invoke",
    response_model=SkillInvocationOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def invoke_skill(
    skill_ref: str,
    payload: SkillInvokeRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Create an invocation; a repeat of ``idempotencyKey`` returns 200 with the
    existing one. The call is executed later by a skill executor — poll
    ``GET /skill-invocations/{id}`` or follow ``skill.invocation_*`` events."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.invoke_skill(
            db,
            ctx,
            skill_ref=skill_ref,
            inputs=payload.inputs,
            idempotency_key=payload.idempotency_key,
            task_ref=payload.task_id,
            run_id=payload.run_id,
            approval_id=payload.approval_id,
        )
        return (201 if result.created else 200), invocation_body(result.invocation, result.skill)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"skill:{skill_ref}\n" + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/skill-invocations/{invocation_id}",
    response_model=SkillInvocationOut,
    responses=ERROR_RESPONSES,
)
async def get_skill_invocation(invocation_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    invocation, skill = await commands.get_skill_invocation(db, ctx, invocation_id)
    return JSONResponse(invocation_body(invocation, skill))


@router.post("/skill-invocations:claim", responses=ERROR_RESPONSES)
async def claim_skill_invocation(
    payload: SkillInvocationClaimRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    """Take one executable pending invocation under a lease; 204 when none.

    The response carries what the executor needs to run the call — name,
    version, side effects and the full contract with its implementation — so
    no second read is needed. The catalog ``config`` is not part of it: it
    stays behind ``org.read`` (ADR-0056 amendment). ``settings`` — the
    effective settings of the skill's package, ``null`` for a skill not
    from a package (CP-ADR-0081 §8). ``invocationId`` narrows the claim to
    one call.
    """

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        claimed = await commands.claim_skill_invocation(
            db,
            ctx,
            settings,
            protocols=payload.protocols,
            local_entrypoints=payload.local_entrypoints,
            http_origins=payload.http_origins,
            mcp_endpoints=payload.mcp_endpoints,
            audiences=payload.audiences,
            session_id=payload.session_id,
            lease_seconds=payload.lease_seconds,
            invocation_id=payload.invocation_id,
        )
        if claimed is None:
            return 204, {}
        invocation, skill = claimed
        return 200, {
            "invocation": invocation_body(invocation, skill),
            "skill": dump(SkillExecutionOut, skill),
            # Read at this claim: a retry of the attempt gets the values of its own.
            "settings": await object_settings(db, ctx.tenant_id, "Skill", skill.name),
        }

    return as_no_content(
        await execute_write(
            request,
            ctx,
            settings,
            session_factory,
            canonical_body=payload.model_dump_json(exclude_unset=True),
            executor=executor,
        )
    )


@router.post(
    "/skill-invocations/{invocation_id}:heartbeat",
    response_model=SkillInvocationOut,
    responses=ERROR_RESPONSES,
)
async def heartbeat_skill_invocation(
    invocation_id: uuid.UUID,
    payload: SkillInvocationHeartbeatRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        invocation, skill = await commands.heartbeat_skill_invocation(
            db,
            ctx,
            settings,
            invocation_id=invocation_id,
            fencing_token=payload.fencing_token,
            lease_seconds=payload.lease_seconds,
            session_id=payload.session_id,
        )
        return 200, invocation_body(invocation, skill)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/skill-invocations/{invocation_id}:complete",
    response_model=SkillInvocationOut,
    responses=ERROR_RESPONSES,
)
async def complete_skill_invocation(
    invocation_id: uuid.UUID,
    payload: SkillInvocationCompleteRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: ContentStoreDep,
) -> JSONResponse:
    """Report a result. The core re-validates ``output``: a violation turns
    the invocation ``failed`` with ``output_contract_violation`` (still 200 —
    the report was accepted, the verdict is in the body). The execution call
    of a task also hands in the task's typed outputs (CP-ADR-0072)."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        invocation, skill = await commands.complete_skill_invocation(
            db,
            ctx,
            invocation_id=invocation_id,
            fencing_token=payload.fencing_token,
            output=payload.output,
            cost=payload.cost,
            session_id=payload.session_id,
            store=store,
        )
        return 200, invocation_body(invocation, skill)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/skill-invocations/{invocation_id}:fail",
    response_model=SkillInvocationOut,
    responses=ERROR_RESPONSES,
)
async def fail_skill_invocation(
    invocation_id: uuid.UUID,
    payload: SkillInvocationFailRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Report a failure; a retryable one returns the call to ``pending`` while
    attempts remain (``retryPolicy``)."""

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        invocation, skill = await commands.fail_skill_invocation(
            db,
            ctx,
            invocation_id=invocation_id,
            fencing_token=payload.fencing_token,
            code=payload.error.code,
            message=payload.error.message,
            retryable=payload.error.retryable,
            details=payload.error.details,
            session_id=payload.session_id,
        )
        return 200, invocation_body(invocation, skill)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/skill-invocations/{invocation_id}:cancel",
    response_model=SkillInvocationOut,
    responses=ERROR_RESPONSES,
)
async def cancel_skill_invocation(
    invocation_id: uuid.UUID,
    payload: SkillInvocationCancelRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Cancel a ``pending`` or ``running`` call (its authority or ``org.manage``).

    A running call's executor loses its lease; it may already have acted —
    ``error.details.wasRunning`` says so. A finished call is ``409
    invocation_terminal``; a cancelled one is returned unchanged.
    """

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        invocation, skill = await commands.cancel_skill_invocation(
            db, ctx, invocation_id=invocation_id, reason=payload.reason
        )
        return 200, invocation_body(invocation, skill)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
