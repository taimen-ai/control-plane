"""Durable Child Run Handle commands (TASK-000007, HRS-7).

A launch creates the child Task, the ``spawned_by`` relation and the handle in
one transaction. Idempotency is a database uniqueness constraint on
``(parent_run_id, correlation_id)`` rather than an HTTP header alone: an
orchestrator that restarts after an ambiguous response gets a new request id
but keeps its own correlation id, and must still not spawn a second child.

The handle records the permission ceiling of the child. Because a grandchild is
narrowed against its parent's *handle* rather than against its API key, the
ceiling can only shrink down the tree.

Lock order (global): task -> claim -> run -> handle.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.runs import (
    _get_tenant_run,
    _lock_task_then_run,
    _require_run_running,
)
from control_plane.application.commands.tasks import create_task, enforce_claim_gate
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principals_key_share
from control_plane.application.queries.child_runs import handle_visible
from control_plane.domain.child_handle import (
    MAX_ARTIFACT_REFS,
    Grant,
    IssuedToken,
    build_result_document,
    child_depth,
    grant_covers,
    grant_from_stored,
    issue_token,
    narrow_grant,
    normalize_grant,
    result_hash,
    root_ceiling,
    validate_cancellation_policy,
    validate_correlation_id,
    validate_expiry_seconds,
)
from control_plane.domain.enums import (
    Permission,
    RunControlOperation,
    RunControlStatus,
    RunStatus,
    TaskRelationType,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import (
    Artifact,
    Capability,
    PrincipalCapability,
    PrincipalSkill,
    Run,
    RunChildHandle,
    RunChildResult,
    RunControlMessage,
    Skill,
    Task,
    TaskRelation,
)


@dataclass(frozen=True)
class LaunchResult:
    handle: RunChildHandle
    child_task: Task
    #: Only ever populated on the transition that created the handle. An
    #: idempotent replay returns ``None``, exactly like replaying an API-key
    #: creation returns no key: a one-time secret is not re-issued.
    token: str | None
    created: bool


async def _principal_capability_names(session: AsyncSession, ctx: AuthContext) -> tuple[str, ...]:
    rows = (
        await session.execute(
            select(Capability.name)
            .join(PrincipalCapability, PrincipalCapability.capability_id == Capability.id)
            .where(
                PrincipalCapability.tenant_id == ctx.tenant_id,
                PrincipalCapability.principal_id == ctx.principal_id,
            )
        )
    ).scalars()
    return tuple(sorted({name for name in rows}))


async def _principal_skill_refs(session: AsyncSession, ctx: AuthContext) -> tuple[str, ...]:
    rows = (
        await session.execute(
            select(Skill.name, Skill.version)
            .join(PrincipalSkill, PrincipalSkill.skill_id == Skill.id)
            .where(
                PrincipalSkill.tenant_id == ctx.tenant_id,
                PrincipalSkill.principal_id == ctx.principal_id,
            )
        )
    ).all()
    return tuple(sorted({f"{name}@{version}" for name, version in rows}))


async def handle_of_run(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> RunChildHandle | None:
    """The handle a run was launched under, if it was launched by a parent."""
    handle: RunChildHandle | None = await session.scalar(
        select(RunChildHandle).where(
            RunChildHandle.tenant_id == tenant_id,
            RunChildHandle.child_run_id == run_id,
        )
    )
    return handle


@dataclass(frozen=True)
class Ceiling:
    grant: Grant
    depth: int
    handle: RunChildHandle | None


async def effective_ceiling(session: AsyncSession, ctx: AuthContext, *, run: Run) -> Ceiling:
    """What ``run`` itself may do, and how deep it sits in the child tree.

    A run launched by a handle is bounded by that handle; a root run is bounded
    by its own principal. Resolving the parent this way — rather than from the
    caller's API key — is what makes the ceiling monotone: a child cannot
    regain a permission its parent gave up merely by holding a stronger key.
    """
    handle = await handle_of_run(session, ctx.tenant_id, run.id)
    if handle is not None:
        return Ceiling(grant=grant_from_stored(handle.granted), depth=handle.depth, handle=handle)
    ceiling = root_ceiling(
        permissions=ctx.permissions,
        capabilities=await _principal_capability_names(session, ctx),
        skills=await _principal_skill_refs(session, ctx),
    )
    return Ceiling(grant=ceiling, depth=0, handle=None)


def require_usable(handle: RunChildHandle) -> None:
    """Reject a handle that was revoked or has outlived its expiry.

    Both are handle-level facts with no other home; the child Task/Run keep
    their own authoritative state and are not touched by either.
    """
    if handle.revoked_at is not None:
        raise ConflictError(
            "child_handle_revoked",
            "Child handle was revoked",
            details={"childHandleId": str(handle.id), "revokedAt": handle.revoked_at.isoformat()},
        )
    if handle.expires_at <= utcnow():
        raise ConflictError(
            "child_handle_expired",
            "Child handle has expired",
            details={"childHandleId": str(handle.id), "expiresAt": handle.expires_at.isoformat()},
        )


async def launch_child_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    parent_run_id: uuid.UUID,
    correlation_id: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    workspace_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    grant: dict[str, object] | None = None,
    cancellation_policy: str | None = None,
    expires_in_seconds: int | None = None,
) -> LaunchResult:
    """Create the child Task, its ``spawned_by`` relation and the handle."""
    await authorize(ctx, Permission.TASKS_CLAIM)
    await authorize(ctx, Permission.TASKS_WRITE)

    correlation = validate_correlation_id(correlation_id)
    policy = validate_cancellation_policy(cancellation_policy)
    expiry_seconds = validate_expiry_seconds(expires_in_seconds)
    requested_grant = normalize_grant(grant)

    # The parent task row lock is taken BEFORE the replay lookup, so competing
    # launches under one parent serialize: the loser blocks, then sees the
    # committed row and replays it. Without that order both would find nothing
    # and race into the unique constraint.
    run_probe = await _get_tenant_run(session, ctx, parent_run_id)
    # The child task and the handle reference the caller (locked by the write
    # flow) and the owner and assignee named here: those go before the parent
    # task (rule 3 of ``application/locking.py``, CP-ADR-0077 §3), and the
    # parent run's session goes first inside ``_lock_task_then_run`` (rule 2).
    await lock_principals_key_share(session, ctx.tenant_id, [owner_id, assignee_id])
    parent_task, parent_run = await _lock_task_then_run(session, ctx, run_probe)

    existing = await session.scalar(
        select(RunChildHandle)
        .where(
            RunChildHandle.parent_run_id == parent_run.id,
            RunChildHandle.correlation_id == correlation,
        )
        .with_for_update()
    )
    if existing is not None:
        # Replay wins over every other check: the caller is recovering from an
        # ambiguous response, and re-validating the run state now would turn a
        # successful launch into a spurious failure.
        replayed_task = await session.scalar(select(Task).where(Task.id == existing.child_task_id))
        if replayed_task is None:  # pragma: no cover - FK guarantees existence
            raise NotFoundError(
                "Child task not found", details={"childTaskId": str(existing.child_task_id)}
            )
        return LaunchResult(handle=existing, child_task=replayed_task, token=None, created=False)

    _require_run_running(parent_run)
    if parent_run.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(parent_run.id)},
        )
    live_claim = await enforce_claim_gate(
        session,
        ctx,
        parent_task,
        claim_id=parent_run.claim_id,
        fencing_token=parent_run.fencing_token,
    )
    if live_claim is None or live_claim.id != parent_run.claim_id:
        raise ConflictError(
            "stale_claim",
            "Launching a child run requires the parent Run's live claim",
            details={"runId": str(parent_run.id), "claimId": str(parent_run.claim_id)},
        )

    parent = await effective_ceiling(session, ctx, run=parent_run)
    if parent.handle is not None:
        # A revoked or expired handle stops being a launch pad: otherwise the
        # subtree below a withdrawn handle could keep growing.
        require_usable(parent.handle)
    granted = narrow_grant(parent.grant, requested_grant)
    depth = child_depth(parent.depth)

    child_task = await create_task(
        session,
        ctx,
        title=title,
        description=description,
        priority=priority,
        owner_id=owner_id,
        assignee_id=assignee_id,
        workspace_id=workspace_id if workspace_id is not None else parent_task.workspace_id,
    )
    now = utcnow()
    relation = TaskRelation(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        from_task_id=child_task.id,
        to_task_id=parent_task.id,
        relation_type=TaskRelationType.SPAWNED_BY,
        created_by_principal_id=ctx.principal_id,
        created_at=now,
    )
    session.add(relation)
    await session.flush()

    handle_id = new_uuid()
    issued: IssuedToken = issue_token(handle_id)
    handle = RunChildHandle(
        id=handle_id,
        tenant_id=ctx.tenant_id,
        parent_run_id=parent_run.id,
        parent_task_id=parent_task.id,
        child_task_id=child_task.id,
        child_run_id=None,
        relation_id=relation.id,
        correlation_id=correlation,
        secret_hash=issued.secret_hash,
        handle_version=1,
        granted=granted.as_dict(),
        cancellation_policy=policy,
        depth=depth,
        created_by_principal_id=ctx.principal_id,
        request_id=ctx.request_id,
        expires_at=now + timedelta(seconds=expiry_seconds),
        revoked_at=None,
        revoked_by_principal_id=None,
        revoke_reason="",
        created_at=now,
    )
    session.add(handle)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.child.launched",
        entity_type="run",
        entity_id=parent_run.id,
        actor_id=ctx.principal_id,
        session_id=parent_run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(parent_task.id),
            "childHandleId": str(handle.id),
            "childTaskId": str(child_task.id),
            "childTaskPublicId": child_task.public_id,
            "correlationId": handle.correlation_id,
            "cancellationPolicy": handle.cancellation_policy,
            "depth": handle.depth,
            "grantSizes": {
                "permissions": len(granted.permissions),
                "capabilities": len(granted.capabilities),
                "skills": len(granted.skills),
            },
        },
    )
    return LaunchResult(handle=handle, child_task=child_task, token=issued.token, created=True)


async def record_child_result(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run: Run,
    outcome: str,
    output: dict[str, Any] | None,
    failure_reason: str | None = None,
) -> RunChildResult | None:
    """Write the bounded terminal result of a child run, once and for good.

    Called from inside the child's terminal transition so there is never a
    moment where the run is finished and the parent has nothing to read. The
    row is append-only and a database trigger rejects edits: a result the
    parent already read cannot be rewritten by a later attempt.

    ``output`` may name ``summary``, ``data`` and ``artifactRefs`` explicitly;
    anything else is treated as data. Oversized content is rejected rather than
    truncated — bulk belongs in an Artifact, with its id in ``artifactRefs``.
    """
    handle = await session.scalar(
        select(RunChildHandle)
        .where(
            RunChildHandle.tenant_id == ctx.tenant_id,
            RunChildHandle.child_run_id == run.id,
        )
        .with_for_update()
    )
    if handle is None:
        return None
    existing = await session.scalar(
        select(RunChildResult).where(RunChildResult.handle_id == handle.id)
    )
    if existing is not None:
        return existing

    payload = dict(output or {})
    summary = payload.pop("summary", None)
    data = payload.pop("data", None)
    artifact_refs = payload.pop("artifactRefs", None)
    if not isinstance(summary, str) or not summary.strip():
        summary = failure_reason or f"Child run {outcome}"
    if not isinstance(data, dict):
        # Anything the child left at the top level is its result data; this
        # keeps a plain `{"checked": 3}` output usable without ceremony.
        data = payload
    if artifact_refs is None:
        artifact_refs = await _run_artifact_refs(session, ctx, run)

    document = build_result_document(
        outcome=outcome,
        summary=summary,
        data=data,
        artifact_refs=artifact_refs,
    )
    result = RunChildResult(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        handle_id=handle.id,
        child_run_id=run.id,
        outcome=document["outcome"],
        summary=document["summary"],
        data=document["data"],
        artifact_refs=document["artifactRefs"],
        result_hash=result_hash(document),
        recorded_at=utcnow(),
    )
    session.add(result)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.child.resolved",
        entity_type="run",
        entity_id=handle.parent_run_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=str(handle.id),
        trace_run_id=ctx.trace_run_id,
        payload={
            "childHandleId": str(handle.id),
            "childRunId": str(run.id),
            "correlationId": handle.correlation_id,
            "outcome": result.outcome,
            "resultHash": result.result_hash,
            "artifactRefs": list(result.artifact_refs),
        },
    )
    return result


async def _run_artifact_refs(session: AsyncSession, ctx: AuthContext, run: Run) -> list[str]:
    """Evidence registered against this run, oldest first.

    Refuses to guess when there is too much: a silently truncated list would
    still hash, and the parent would have no way to tell it read a fragment.
    """
    rows = (
        await session.scalars(
            select(Artifact.id)
            .where(Artifact.tenant_id == ctx.tenant_id, Artifact.run_id == run.id)
            .order_by(Artifact.created_at, Artifact.id)
            .limit(MAX_ARTIFACT_REFS + 1)
        )
    ).all()
    if len(rows) > MAX_ARTIFACT_REFS:
        raise ValidationError(
            "child_result_too_large",
            f"This run has more than {MAX_ARTIFACT_REFS} artifacts; "
            "name the relevant ones in output.artifactRefs",
            details={"field": "artifactRefs", "maxRefs": MAX_ARTIFACT_REFS},
        )
    return [str(row) for row in rows]


async def request_child_cancel(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    handle: RunChildHandle,
    reason: str,
    causation_id: str,
) -> RunControlMessage | None:
    """Ask a running child to stop at its next safe boundary.

    Reuses the Active Turn Control contract (HRS-4) instead of inventing a
    second stop mechanism: the child already knows how to acknowledge a
    ``request_cancel`` at a boundary it considers safe. Idempotent by domain
    key, so a repeated revoke or a repeated parent cancellation adds nothing.
    """
    if handle.child_run_id is None:
        return None
    child_run = await session.scalar(
        select(Run)
        .where(Run.id == handle.child_run_id, Run.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if child_run is None or child_run.status != RunStatus.RUNNING:
        return None

    idempotency_key = f"child-cancel:{handle.id}"
    existing = await session.scalar(
        select(RunControlMessage).where(
            RunControlMessage.run_id == child_run.id,
            RunControlMessage.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    last_seq = (
        await session.scalar(
            select(func.max(RunControlMessage.seq)).where(RunControlMessage.run_id == child_run.id)
        )
    ) or 0
    now = utcnow()
    message = RunControlMessage(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=child_run.id,
        task_id=child_run.task_id,
        seq=last_seq + 1,
        operation=RunControlOperation.REQUEST_CANCEL,
        status=RunControlStatus.ACCEPTED,
        causal_position="server:child_handle",
        directive=None,
        reason=reason,
        safe_boundary=None,
        idempotency_key=idempotency_key,
        requested_by_principal_id=ctx.principal_id,
        acknowledged_by_principal_id=None,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=causation_id,
        version=1,
        accepted_at=now,
        resolved_at=None,
    )
    session.add(message)
    if child_run.cancel_requested_at is None:
        child_run.cancel_requested_at = now
        child_run.cancel_requested_by = ctx.principal_id
    child_run.version += 1
    child_run.updated_at = now
    await session.flush()

    for event_type, payload in (
        (
            "run.control_message.accepted",
            {
                "taskId": str(child_run.task_id),
                "controlMessageId": str(message.id),
                "seq": message.seq,
                "operation": message.operation,
                "status": message.status,
                "causalPosition": message.causal_position,
            },
        ),
        (
            "run.child.cancel_requested",
            {
                "childHandleId": str(handle.id),
                "childRunId": str(child_run.id),
                "controlMessageId": str(message.id),
                "correlationId": handle.correlation_id,
                "reason": reason,
            },
        ),
    ):
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type=event_type,
            entity_type="run",
            entity_id=child_run.id,
            actor_id=ctx.principal_id,
            session_id=child_run.session_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            causation_id=causation_id,
            trace_run_id=ctx.trace_run_id,
            payload=payload,
        )
    return message


async def cascade_cooperative_cancel(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    parent_run_id: uuid.UUID,
    reason: str,
    causation_id: str,
) -> list[RunControlMessage]:
    """Pass an accepted parent stop down to children that opted into it.

    Only ``cascade_cooperative`` handles are followed. ``detach`` exists for
    children meant to outlive their parent's turn — and it deliberately buys no
    immunity from ``force_cancel``, which is a governance stop rather than a
    cooperative signal.
    """
    handles = (
        await session.scalars(
            select(RunChildHandle)
            .where(
                RunChildHandle.tenant_id == ctx.tenant_id,
                RunChildHandle.parent_run_id == parent_run_id,
                RunChildHandle.cancellation_policy == "cascade_cooperative",
                RunChildHandle.child_run_id.is_not(None),
            )
            .order_by(RunChildHandle.created_at, RunChildHandle.id)
        )
    ).all()
    messages: list[RunControlMessage] = []
    for handle in handles:
        message = await request_child_cancel(
            session, ctx, handle=handle, reason=reason, causation_id=causation_id
        )
        if message is not None:
            messages.append(message)
    return messages


async def revoke_child_handle(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    handle_id: uuid.UUID,
    reason: str = "",
    cancel_child: bool = False,
) -> RunChildHandle:
    """Withdraw a handle; optionally ask the child to stop cooperatively.

    Revocation is a statement about the *handle*, not about the child: an
    already running child keeps its own authoritative Task/Run state unless
    ``cancel_child`` is set. Withdrawing the locator without touching a live
    execution would otherwise leave work nobody is watching.
    """
    handle = await session.scalar(
        select(RunChildHandle)
        .where(RunChildHandle.id == handle_id, RunChildHandle.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if handle is None or not await handle_visible(session, ctx, handle):
        raise NotFoundError("Child handle not found", details={"childHandleId": str(handle_id)})

    if not ctx.has(Permission.CLAIMS_MANAGE):
        await authorize(ctx, Permission.TASKS_CLAIM)
        parent_run = await session.scalar(
            select(Run).where(Run.id == handle.parent_run_id, Run.tenant_id == ctx.tenant_id)
        )
        if parent_run is None or parent_run.principal_id != ctx.principal_id:
            raise AuthorizationError(
                "Revoking a child handle requires the parent Run holder or claims.manage",
                code="run_holder_mismatch",
                details={"childHandleId": str(handle.id)},
            )

    if handle.revoked_at is None:
        now = utcnow()
        handle.revoked_at = now
        handle.revoked_by_principal_id = ctx.principal_id
        handle.revoke_reason = reason.strip()
        await session.flush()
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="run.child.revoked",
            entity_type="run",
            entity_id=handle.parent_run_id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "childHandleId": str(handle.id),
                "childTaskId": str(handle.child_task_id),
                "correlationId": handle.correlation_id,
                "reason": handle.revoke_reason,
            },
        )
    if cancel_child:
        await request_child_cancel(
            session,
            ctx,
            handle=handle,
            reason=handle.revoke_reason or "child_handle_revoked",
            causation_id=str(handle.id),
        )
    return handle


async def bind_child_run(
    session: AsyncSession, ctx: AuthContext, *, task: Task, run: Run
) -> RunChildHandle | None:
    """Attach a freshly started run to the handle its Task was launched under.

    Called from ``start_run``: the child is claimed and started by whoever is
    eligible, and only at that moment does the handle learn which run carries
    its ceiling.
    """
    handle = await session.scalar(
        select(RunChildHandle)
        .where(
            RunChildHandle.tenant_id == ctx.tenant_id,
            RunChildHandle.child_task_id == task.id,
        )
        .with_for_update()
    )
    if handle is None:
        return None
    require_usable(handle)
    granted = grant_from_stored(handle.granted)
    if not grant_covers(granted, Permission.TASKS_CLAIM):
        # Refuse at the start rather than at the first write: a run that may
        # not claim is a run that can do nothing, and discovering that halfway
        # through would strand the child holding a live claim.
        raise AuthorizationError(
            "This child handle does not grant tasks.claim",
            code="child_grant_exceeded",
            details={
                "childHandleId": str(handle.id),
                "required": Permission.TASKS_CLAIM.value,
                "granted": list(granted.permissions),
            },
        )
    if handle.child_run_id is not None:
        previous = await session.scalar(select(Run).where(Run.id == handle.child_run_id))
        if previous is not None and previous.status == RunStatus.RUNNING:
            raise ConflictError(
                "child_run_already_bound",
                "This child handle already has a running run",
                details={"childHandleId": str(handle.id), "runId": str(handle.child_run_id)},
            )
        recorded = await session.scalar(
            select(RunChildResult.id).where(RunChildResult.handle_id == handle.id)
        )
        if recorded is not None:
            # The child already reported a terminal result. A later attempt
            # must not become the thing the parent reads: the result is final,
            # and a fresh attempt is a fresh launch with its own correlation id.
            return handle
        # A suspended attempt records no result, so the handle follows the run
        # that continues the work — otherwise no result would ever be written.
        handle.child_run_id = None
    handle.child_run_id = run.id
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.child.started",
        entity_type="run",
        entity_id=handle.parent_run_id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "childHandleId": str(handle.id),
            "childTaskId": str(task.id),
            "childRunId": str(run.id),
            "correlationId": handle.correlation_id,
            "attempt": run.attempt,
        },
    )
    return handle
