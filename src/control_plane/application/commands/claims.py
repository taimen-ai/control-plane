"""Atomic task claims with lease + fencing tokens.

The claim operation is the critical section of the whole service. It runs in
one transaction, under ``SELECT ... FOR UPDATE`` on the task row, and is
backed by a partial unique index (one active claim per task) so the invariant
holds even if application code regresses.

A claim can also reclaim an expired predecessor atomically — correctness does
not depend on the cleanup worker running.
"""

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import observability
from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands._claim_release import (
    apply_claim_status,
    release_claim_on_locked_task,
)
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import resolve_task_for_update
from control_plane.application.common import clamp_ttl, new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_caller
from control_plane.application.visibility import task_visible
from control_plane.config import Settings
from control_plane.domain.enums import ClaimStatus, Permission, SessionStatus
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_item import TERMINAL_CATEGORIES
from control_plane.infrastructure.db.models import Session, Task, TaskClaim


async def _require_live_own_session(
    session: AsyncSession, ctx: AuthContext, session_id: uuid.UUID
) -> Session:
    """Validate and SHARE-lock the caller's session.

    The share lock is taken BEFORE any task lock (global order: session ->
    task -> claim, same as close_session), so a concurrent session close
    cannot slip between validation and claim creation and leave an active
    claim attached to a closed session.

    The caller's principal goes before the session (``lock_caller``, rule 1
    of ``application/locking.py``): the claim inserted later references it as
    its holder, and ``principals/{id}:disable`` locks the principal before the
    session (CP-ADR-0077 §3). The HTTP write flow has already taken it; taken
    again here it costs nothing and keeps the command safe for any other
    caller. It also re-reads the status: a principal disabled while the request
    was in flight takes no claim.
    """
    await lock_caller(session, ctx)
    work_session = await session.scalar(
        select(Session)
        .where(Session.id == session_id, Session.tenant_id == ctx.tenant_id)
        .with_for_update(read=True)
    )
    if work_session is None:
        raise NotFoundError("Session not found", details={"sessionId": str(session_id)})
    if work_session.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Session belongs to another principal",
            code="session_owner_mismatch",
            details={"sessionId": str(session_id)},
        )
    if work_session.status != SessionStatus.ACTIVE:
        raise ConflictError(
            "session_not_active",
            "Session is not active",
            details={"sessionId": str(session_id), "status": work_session.status},
        )
    if work_session.expires_at <= utcnow():
        raise ConflictError(
            "session_expired",
            "Session lease has expired",
            details={"sessionId": str(session_id)},
        )
    return work_session


async def _claim_locked_task(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    task: Task,
    work_session: Session,
    *,
    ttl_seconds: int | None,
    intent: str,
) -> TaskClaim:
    """Steps 3-11 of the claim algorithm; the task row is already locked."""
    from control_plane.application.commands.approvals import check_approval_gate
    from control_plane.application.commands.eligibility import check_claim_eligibility
    from control_plane.application.commands.relations import check_task_readiness
    from control_plane.application.commands.task_inputs import check_required_inputs
    from control_plane.application.commands.verification import check_verification_gate

    now = utcnow()

    # By CATEGORY, never by key (ADR-0048): a tenant may call its terminal
    # status anything, and pre-v0.8 "not done and not cancelled" is exactly
    # "category is not terminal" under the system type's mapping. Note that
    # `blocked` stays claimable, as it was before v0.8.
    if task.system_status_category in TERMINAL_CATEGORIES:
        raise ValidationError(
            "task_not_claimable",
            f"Task in status '{task.status}' cannot be claimed",
            details={
                "taskId": str(task.id),
                "status": task.status,
                "systemStatusCategory": task.system_status_category,
            },
        )

    # Organizational eligibility (requirements), dependency readiness, required
    # inputs and the v0.3 approval gate are checked inside the claiming transaction, under
    # the task row lock.
    await check_claim_eligibility(session, ctx, task, work_session.principal_id)
    await check_task_readiness(session, ctx, task)
    # Inputs the type declares required are there (CP-ADR-0072 §8).
    await check_required_inputs(session, ctx, task)
    await check_approval_gate(session, ctx, task.id)
    # Handed in and under verification (CP-ADR-0067): not work to take.
    await check_verification_gate(session, task)

    if task.active_claim_id is not None:
        # populate_existing: if this claim row was probed earlier in the same
        # transaction (reclaim path), the FOR UPDATE re-select must refresh
        # the cached instance — decisions below rely on the locked row state.
        current = await session.scalar(
            select(TaskClaim)
            .where(TaskClaim.id == task.active_claim_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is not None and current.status == ClaimStatus.ACTIVE:
            holder_alive = current.expires_at > now
            if holder_alive:
                holder_session = await session.get(Session, current.session_id)
                holder_alive = (
                    holder_session is not None
                    and holder_session.status == SessionStatus.ACTIVE
                    and holder_session.expires_at > now
                )
                reap_reason = "session_inactive"
            else:
                reap_reason = "expired"
            if holder_alive:
                raise ConflictError(
                    "task_already_claimed",
                    "Task already has an active claim",
                    details={
                        "taskId": str(task.id),
                        "claimId": str(current.id),
                        "expiresAt": current.expires_at.isoformat(),
                    },
                )
            # Expired (or held by a dead session): reap it right here,
            # atomically with the new claim.
            release_claim_on_locked_task(
                task, current, reason=reap_reason, new_status=ClaimStatus.STALE
            )
            await record_event(
                session,
                tenant_id=ctx.tenant_id,
                event_type="claim.expired",
                entity_type="claim",
                entity_id=current.id,
                actor_id=ctx.principal_id,
                session_id=current.session_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                trace_run_id=ctx.trace_run_id,
                payload={"taskId": str(task.id), "reason": reap_reason},
            )
        else:
            task.active_claim_id = None  # dangling pointer to a released/stale claim

    ttl = clamp_ttl(
        ttl_seconds,
        default=settings.claim_ttl_seconds,
        minimum=settings.claim_ttl_min_seconds,
        maximum=settings.claim_ttl_max_seconds,
    )

    task.claim_epoch += 1
    claim = TaskClaim(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        task_id=task.id,
        session_id=work_session.id,
        holder_id=work_session.principal_id,
        status=ClaimStatus.ACTIVE,
        fencing_token=task.claim_epoch,
        intent=intent,
        acquired_at=now,
        heartbeat_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(claim)
    await session.flush()

    task.active_claim_id = claim.id
    apply_claim_status(task, await lifecycle_of(session, task))
    task.version += 1
    task.updated_at = now

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.claimed",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        session_id=work_session.id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "claimId": str(claim.id),
            "sessionId": str(work_session.id),
            "holderId": str(claim.holder_id),
            "fencingToken": claim.fencing_token,
            "expiresAt": claim.expires_at.isoformat(),
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
            "version": task.version,
        },
    )
    return claim


async def claim_task(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    task_ref: str,
    session_id: uuid.UUID,
    ttl_seconds: int | None = None,
    intent: str = "",
) -> TaskClaim:
    await authorize(ctx, Permission.TASKS_CLAIM)
    # Lock order: session (shared) before task — see _require_live_own_session.
    work_session = await _require_live_own_session(session, ctx, session_id)
    task = await resolve_task_for_update(session, ctx, task_ref)
    await authorize(ctx, Permission.TASKS_CLAIM, resource=ResourceRef("task", str(task.id)))
    return await _claim_locked_task(
        session, ctx, settings, task, work_session, ttl_seconds=ttl_seconds, intent=intent
    )


async def _get_tenant_claim(
    session: AsyncSession, ctx: AuthContext, claim_id: uuid.UUID
) -> TaskClaim:
    claim = await session.scalar(
        select(TaskClaim).where(TaskClaim.id == claim_id, TaskClaim.tenant_id == ctx.tenant_id)
    )
    # A claim on invisible work is a missing claim (CP-ADR-0082 §3.7).
    if claim is None or not await task_visible(session, ctx, claim.task_id):
        raise NotFoundError("Claim not found", details={"claimId": str(claim_id)})
    return claim


async def heartbeat_claim(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    claim_id: uuid.UUID,
    ttl_seconds: int | None = None,
) -> TaskClaim:
    await authorize(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)
    claim = await session.scalar(
        select(TaskClaim)
        .where(TaskClaim.id == claim_id, TaskClaim.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if claim is None or not await task_visible(session, ctx, claim.task_id):
        raise NotFoundError("Claim not found", details={"claimId": str(claim_id)})
    if claim.holder_id != ctx.principal_id and not ctx.has(Permission.CLAIMS_MANAGE):
        raise AuthorizationError("Claim is held by another principal", code="claim_holder_mismatch")
    if claim.status != ClaimStatus.ACTIVE:
        raise ConflictError(
            "claim_not_active",
            "Claim is not active",
            details={"claimId": str(claim_id), "status": claim.status},
        )
    now = utcnow()
    if claim.expires_at <= now:
        raise ConflictError(
            "claim_expired",
            "Claim lease has expired",
            details={"claimId": str(claim_id), "expiresAt": claim.expires_at.isoformat()},
        )
    # The holder's session must itself still be alive.
    holder_session = await session.get(Session, claim.session_id)
    if (
        holder_session is None
        or holder_session.status != SessionStatus.ACTIVE
        or holder_session.expires_at <= now
    ):
        raise ConflictError(
            "session_not_active",
            "The claim's session is no longer alive",
            details={"sessionId": str(claim.session_id)},
        )

    ttl = clamp_ttl(
        ttl_seconds,
        default=settings.claim_ttl_seconds,
        minimum=settings.claim_ttl_min_seconds,
        maximum=settings.claim_ttl_max_seconds,
    )
    claim.heartbeat_at = now
    claim.expires_at = now + timedelta(seconds=ttl)
    return claim


async def release_claim(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    claim_id: uuid.UUID,
    reason: str = "released",
) -> TaskClaim:
    await authorize(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)
    claim_probe = await _get_tenant_claim(session, ctx, claim_id)
    if claim_probe.holder_id != ctx.principal_id and not ctx.has(Permission.CLAIMS_MANAGE):
        raise AuthorizationError("Claim is held by another principal", code="claim_holder_mismatch")

    # Lock ordering: task first, then claim; re-check status afterwards.
    # populate_existing is essential: the probe above put this claim into the
    # identity map, and without it the FOR UPDATE re-select would return the
    # cached pre-lock attributes, making the re-check inert.
    task = await session.scalar(
        select(Task).where(Task.id == claim_probe.task_id).with_for_update()
    )
    claim = await session.scalar(
        select(TaskClaim)
        .where(TaskClaim.id == claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or claim is None:
        raise NotFoundError("Claim not found", details={"claimId": str(claim_id)})
    if claim.status != ClaimStatus.ACTIVE:
        return claim  # idempotent: already released or stale

    release_claim_on_locked_task(
        task, claim, reason=reason, lifecycle=await lifecycle_of(session, task)
    )
    task.version += 1
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="claim.released",
        entity_type="claim",
        entity_id=claim.id,
        actor_id=ctx.principal_id,
        session_id=claim.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": reason, "taskStatus": task.status},
    )
    return claim


async def reclaim_claim(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    claim_id: uuid.UUID,
    session_id: uuid.UUID,
    ttl_seconds: int | None = None,
    intent: str = "",
) -> TaskClaim:
    """Take over an expired claim: mark it stale and claim the task atomically."""
    observability.inc("claim_takeovers_total")
    await authorize(ctx, Permission.TASKS_CLAIM)
    claim_probe = await _get_tenant_claim(session, ctx, claim_id)

    # Lock order: session (shared) -> task -> claim.
    work_session = await _require_live_own_session(session, ctx, session_id)
    task = await session.scalar(
        select(Task).where(Task.id == claim_probe.task_id).with_for_update()
    )
    if task is None:
        raise NotFoundError("Task not found", details={"taskId": str(claim_probe.task_id)})
    # populate_existing: the probe cached this claim; the expiry decision must
    # be made on the row state actually protected by the lock (a concurrent
    # heartbeat may have extended the lease between probe and lock).
    old_claim = await session.scalar(
        select(TaskClaim)
        .where(TaskClaim.id == claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old_claim is None:
        raise NotFoundError("Claim not found", details={"claimId": str(claim_id)})

    now = utcnow()
    if old_claim.status == ClaimStatus.ACTIVE and old_claim.expires_at > now:
        holder_session = await session.get(Session, old_claim.session_id)
        holder_alive = (
            holder_session is not None
            and holder_session.status == SessionStatus.ACTIVE
            and holder_session.expires_at > now
        )
        if holder_alive:
            raise ConflictError(
                "claim_not_expired",
                "Claim is still live and cannot be reclaimed",
                details={
                    "claimId": str(claim_id),
                    "expiresAt": old_claim.expires_at.isoformat(),
                },
            )

    # _claim_locked_task reaps the dead active claim (if it is still the
    # task's active claim) and issues the next fencing token.
    return await _claim_locked_task(
        session, ctx, settings, task, work_session, ttl_seconds=ttl_seconds, intent=intent
    )
