"""Run commands: concrete execution attempts under a claim.

Semantics:

    Task  = business intent
    Claim = temporary exclusive ownership (lease + fencing token)
    Run   = one execution attempt, pinned to the claim epoch it started under

Fencing: starting a run and writing a *final task result* (``:succeed``)
re-validate the claim gate under the task row lock — the run's recorded
``fencing_token`` must still equal the task's ``claim_epoch`` and the claim
must still be live. A zombie run (its claim was taken over) is rejected on
``:succeed`` and is superseded (failed) when the new claim starts its own run.

``:fail``/``:cancel`` only finalize the run record itself (they never touch
the task), so they are allowed for the run's principal even after the lease
was lost — an honest failure report is always recordable.

Lock order: session -> task -> claim -> run.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import observability
from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._child_ceiling import enforce_run_ceiling
from control_plane.application.commands._claim_release import release_claim_on_locked_task
from control_plane.application.commands.agents import check_run_agent_revision
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import (
    enforce_claim_gate,
    finish_locked_task,
    get_running_run_locked,
    resolve_task_for_update,
    supersede_run,
    verify_task_completable,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.event_cursor import EventPosition, encode_position
from control_plane.application.events import record_event
from control_plane.application.locking import (
    lock_caller,
    lock_claim_session,
    lock_session_key_share,
)
from control_plane.application.queries.instructions import instructions_for_task
from control_plane.application.visibility import task_visible
from control_plane.domain.agent_instructions import instruction_refs
from control_plane.domain.enums import (
    Permission,
    RunControlOperation,
    RunControlStatus,
    RunStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.redaction import reject_unsafe_durable_payload
from control_plane.domain.work_item import TERMINAL_CATEGORIES
from control_plane.infrastructure.db.models import (
    Run,
    RunCheckpoint,
    RunControlMessage,
    Task,
    TaskClaim,
)


@dataclass(frozen=True)
class HandoffResult:
    run: Run
    task: Task
    checkpoint: RunCheckpoint
    event_cursor: str


def _validate_handoff_data(value: Any) -> None:
    """Keep secrets, transcripts and machine-local paths out of handoff state.

    The prohibition list itself lives in ``domain/redaction.py`` so that every
    durable harness payload is checked against one list rather than against
    copies of it.
    """
    reject_unsafe_durable_payload(
        value, code="unsafe_handoff_payload", subject="Handoff checkpoint"
    )


async def start_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    claim_id: uuid.UUID,
    fencing_token: int,
    input_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    max_duration_seconds: int | None = None,
    max_actions: int | None = None,
    agent_revision_id: uuid.UUID | None = None,
) -> Run:
    await authorize(ctx, Permission.TASKS_CLAIM)
    if max_duration_seconds is not None and max_duration_seconds <= 0:
        raise ValidationError("invalid_budget", "maxDurationSeconds must be positive")
    if max_actions is not None and max_actions <= 0:
        raise ValidationError("invalid_budget", "maxActions must be positive")
    # A registered agent runs by a revision of its own spec, anyone else by
    # none (CP-ADR-0073 §7); checked before any lock is taken.
    agent_revision_id = await check_run_agent_revision(session, ctx, agent_revision_id)
    # The run references the caller (the claim's holder) and the claim's
    # session. Both go before the task, as ``principals/{id}:disable`` takes
    # them (CP-ADR-0077 §3, ``application/locking.py``): the caller by rule 1,
    # the session by rule 2 — a session opened on behalf of a human is closed
    # by ``:disable`` of that human, whom the caller's lock does not cover. The
    # claim's session never changes, so it is read without a lock; a claim id
    # that is not the task's live claim is refused by the gate below.
    await lock_caller(session, ctx)
    await lock_claim_session(session, ctx.tenant_id, claim_id)
    task = await resolve_task_for_update(session, ctx, task_ref)

    if task.system_status_category in TERMINAL_CATEGORIES:
        raise ValidationError(
            "task_not_runnable",
            f"Task in status '{task.status}' cannot start a run",
            details={
                "taskId": str(task.id),
                "status": task.status,
                "systemStatusCategory": task.system_status_category,
            },
        )

    # The caller must present its live claim credentials; the gate raises on
    # every fencing violation and returns the verified live claim.
    live_claim = await enforce_claim_gate(
        session, ctx, task, claim_id=claim_id, fencing_token=fencing_token
    )
    if live_claim is None:
        raise ConflictError(
            "stale_claim",
            "Starting a run requires a live claim on the task",
            details={"taskId": str(task.id), "claimId": str(claim_id)},
        )

    existing = await get_running_run_locked(session, task)
    if existing is not None:
        if existing.claim_id == live_claim.id:
            raise ConflictError(
                "run_already_active",
                "This claim already has a running run",
                details={"runId": str(existing.id)},
            )
        # A zombie run left by a previous epoch: supersede it, mirroring how
        # claim takeover reaps the previous claim.
        await supersede_run(session, ctx, task, existing)

    attempt = (
        await session.scalar(select(func.count()).select_from(Run).where(Run.task_id == task.id))
        or 0
    ) + 1

    # The instructions the executor is handed for this run, pinned by hash and
    # layer versions (CP-ADR-0066): the text may move on (a new project config
    # revision), the record of what this run was started under does not.
    refs = instruction_refs(await instructions_for_task(session, ctx.tenant_id, task))

    now = utcnow()
    run = Run(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        task_id=task.id,
        claim_id=live_claim.id,
        principal_id=ctx.principal_id,
        session_id=live_claim.session_id,
        fencing_token=live_claim.fencing_token,
        attempt=attempt,
        status=RunStatus.RUNNING,
        started_at=now,
        input=input_data,
        max_duration_seconds=max_duration_seconds,
        max_actions=max_actions,
        metadata_json=metadata or {},
        instructions_hash=refs["hash"],
        instructions_refs=refs,
        agent_revision_id=agent_revision_id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.started",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "claimId": str(live_claim.id),
            "attempt": attempt,
            "fencingToken": run.fencing_token,
            "instructionsHash": refs["hash"],
            "instructionsRefs": refs["layers"],
            "agentRevisionId": str(agent_revision_id) if agent_revision_id else None,
        },
    )

    # A task launched by a parent run learns its run here: the child is claimed
    # and started by whoever is eligible, and only now does the handle know
    # which run carries its ceiling (HRS-7).
    from control_plane.application.commands.child_runs import bind_child_run

    await bind_child_run(session, ctx, task=task, run=run)
    return run


async def _get_tenant_run(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> Run:
    run = await session.scalar(select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id))
    # A run of invisible work is a missing run, for every command over it
    # (CP-ADR-0082 §3.7, FR-007).
    if run is None or not await task_visible(session, ctx, run.task_id):
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    return run


async def _lock_task_then_run(
    session: AsyncSession, ctx: AuthContext, run_probe: Run
) -> tuple[Task, Run]:
    """Re-acquire task then run under locks (global lock order), fresh state.

    The run's session goes first (rule 2 of ``application/locking.py``): what
    the caller writes next may reference it, and ``principals/{id}:disable``
    of the principal the session acts for holds the session before the task.
    """
    await lock_session_key_share(session, run_probe.tenant_id, run_probe.session_id)
    task = await session.scalar(select(Task).where(Task.id == run_probe.task_id).with_for_update())
    run = await session.scalar(
        select(Run)
        .where(Run.id == run_probe.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or run is None:  # pragma: no cover - FK guarantees existence
        raise NotFoundError("Run not found", details={"runId": str(run_probe.id)})
    return task, run


def _require_run_running(run: Run) -> None:
    if run.status != RunStatus.RUNNING:
        raise ConflictError(
            "run_not_active",
            "Run is not running",
            details={"runId": str(run.id), "status": run.status},
        )


async def succeed_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    output: dict[str, Any] | None = None,
    complete_task: bool = True,
) -> tuple[Run, Task]:
    """Finish a run successfully; by default atomically completes the task.

    The whole transition (run succeeded + claim released + task done) is one
    transaction, so there is no window where the run succeeded but the task
    completion could be lost or raced by another claim.
    """
    await authorize(ctx, Permission.TASKS_CLAIM)
    run_probe = await _get_tenant_run(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)

    _require_run_running(run)
    if run.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )
    await enforce_run_ceiling(session, ctx, run=run, permission=Permission.TASKS_CLAIM)

    # Fencing: the run's claim must still be the task's live claim and the
    # run's recorded token must still equal the claim epoch. A takeover makes
    # both checks fail -> 409 stale_claim, and the zombie writes nothing.
    live_claim = None
    if complete_task:
        live_claim = await verify_task_completable(
            session, ctx, task, claim_id=run.claim_id, fencing_token=run.fencing_token
        )
    else:
        live_claim = await enforce_claim_gate(
            session, ctx, task, claim_id=run.claim_id, fencing_token=run.fencing_token
        )
    if live_claim is None or live_claim.id != run.claim_id:
        raise ConflictError(
            "stale_claim",
            "The run's claim is no longer live",
            details={"runId": str(run.id), "claimId": str(run.claim_id)},
        )

    now = utcnow()
    run.status = RunStatus.SUCCEEDED
    run.finished_at = now
    run.output = output
    run.updated_at = now
    run.version += 1

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.succeeded",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "attempt": run.attempt,
            "taskCompleted": complete_task,
        },
    )

    # A child reports its bounded terminal result inside the same transition
    # (HRS-7): there is never a moment where the run is finished and the parent
    # has nothing to read.
    from control_plane.application.commands.child_runs import record_child_result

    await record_child_result(session, ctx, run=run, outcome="succeeded", output=output)

    if complete_task:
        await finish_locked_task(
            session, ctx, task, live_claim, trigger="run", trigger_ref=str(run.id)
        )
    return run, task


async def fail_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    failure_reason: str = "failed",
    output: dict[str, Any] | None = None,
) -> Run:
    """Record a run failure. Touches only the run: no fencing required, so an
    agent that lost its lease can still honestly record the failure."""
    await authorize(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)
    run_probe = await _get_tenant_run(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)

    if run.principal_id != ctx.principal_id and not ctx.has(Permission.CLAIMS_MANAGE):
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )
    _require_run_running(run)

    now = utcnow()
    run.status = RunStatus.FAILED
    run.failure_reason = failure_reason
    run.output = output
    run.finished_at = now
    run.updated_at = now
    run.version += 1

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.failed",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": failure_reason, "attempt": run.attempt},
    )

    from control_plane.application.commands.child_runs import record_child_result

    await record_child_result(
        session,
        ctx,
        run=run,
        outcome="failed",
        output=output,
        failure_reason=failure_reason,
    )
    return run


async def suspend_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    reason: str = "waiting_approval",
    waiting_for_approval_id: uuid.UUID | None = None,
) -> tuple[Run, Task]:
    """Pause execution: run -> suspended (terminal) and the claim is released.

    ADR-0018 waiting semantics: the exclusive lease is NOT parked for the
    (possibly long) wait — the run archives its state via checkpoints, the
    claim is freed, and continuation is a fresh claim + a fresh run. Only the
    legitimate live owner may suspend (zombies get 409 stale_claim).
    """
    await authorize(ctx, Permission.TASKS_CLAIM)
    run_probe = await _get_tenant_run(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)

    _require_run_running(run)
    if run.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )
    live_claim = await enforce_claim_gate(
        session, ctx, task, claim_id=run.claim_id, fencing_token=run.fencing_token
    )
    if live_claim is None or live_claim.id != run.claim_id:
        raise ConflictError(
            "stale_claim",
            "The run's claim is no longer live",
            details={"runId": str(run.id), "claimId": str(run.claim_id)},
        )

    now = utcnow()
    run.status = RunStatus.SUSPENDED
    run.failure_reason = None
    run.finished_at = now
    run.updated_at = now
    run.version += 1
    if waiting_for_approval_id is not None:
        run.metadata_json = {
            **run.metadata_json,
            "waitingForApprovalId": str(waiting_for_approval_id),
        }

    release_claim_on_locked_task(
        task, live_claim, reason=reason, lifecycle=await lifecycle_of(session, task)
    )
    task.version += 1

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.suspended",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "reason": reason,
            "attempt": run.attempt,
            "waitingForApprovalId": (
                str(waiting_for_approval_id) if waiting_for_approval_id else None
            ),
        },
    )
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="claim.released",
        entity_type="claim",
        entity_id=live_claim.id,
        actor_id=ctx.principal_id,
        session_id=live_claim.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": reason, "taskStatus": task.status},
    )
    return run, task


async def prepare_handoff(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    reason: str,
    checkpoint_kind: str,
    checkpoint_data: dict[str, Any],
) -> HandoffResult:
    """Atomically checkpoint, suspend and release a live run for handoff.

    Continuation always uses a new claim and a new run. The command relies on
    the HTTP idempotency write flow for ambiguous-response replay; the saved
    response includes the checkpoint and cursor produced by this transaction.
    """
    await authorize(ctx, Permission.TASKS_CLAIM)
    if reason != "human_harness_handoff" or checkpoint_kind != "handoff":
        raise ValidationError(
            "invalid_handoff",
            "Human harness handoff requires reason=human_harness_handoff and kind=handoff",
        )
    _validate_handoff_data(checkpoint_data)

    run_probe = await _get_tenant_run(session, ctx, run_id)
    # Global mutation order: session -> task -> claim -> run (CP-ADR-0077 §3).
    # The task lock serializes claim takeover; explicit locks make the handoff
    # transaction auditable; the run's session goes first because the
    # checkpoint written below references its principal and ``:disable`` of
    # whoever the session acts for holds the session before the task.
    await lock_session_key_share(session, ctx.tenant_id, run_probe.session_id)
    task = await session.scalar(select(Task).where(Task.id == run_probe.task_id).with_for_update())
    claim = await session.scalar(
        select(TaskClaim)
        .where(TaskClaim.id == run_probe.claim_id, TaskClaim.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    run = await session.scalar(
        select(Run)
        .where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or claim is None or run is None:  # pragma: no cover - FKs
        raise NotFoundError("Run not found", details={"runId": str(run_id)})

    _require_run_running(run)
    if run.principal_id != ctx.principal_id or claim.holder_id != ctx.principal_id:
        raise AuthorizationError(
            "Run or claim belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id), "claimId": str(claim.id)},
        )
    live_claim = await enforce_claim_gate(
        session, ctx, task, claim_id=run.claim_id, fencing_token=run.fencing_token
    )
    if live_claim is None or live_claim.id != claim.id:
        raise ConflictError(
            "stale_claim",
            "The run's claim is no longer live",
            details={"runId": str(run.id), "claimId": str(run.claim_id)},
        )

    last_seq = (
        await session.scalar(
            select(func.max(RunCheckpoint.seq)).where(RunCheckpoint.run_id == run.id)
        )
    ) or 0
    checkpoint = RunCheckpoint(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        task_id=task.id,
        created_by_principal_id=ctx.principal_id,
        seq=last_seq + 1,
        kind=checkpoint_kind,
        data=checkpoint_data,
        created_at=utcnow(),
    )
    session.add(checkpoint)
    await session.flush()

    now = utcnow()
    run.status = RunStatus.SUSPENDED
    run.finished_at = now
    run.updated_at = now
    run.version += 1
    run.metadata_json = {
        **run.metadata_json,
        "suspendReason": reason,
        "handoffCheckpointId": str(checkpoint.id),
    }
    release_claim_on_locked_task(
        task, claim, reason=reason, lifecycle=await lifecycle_of(session, task)
    )
    task.version += 1

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.checkpointed",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "checkpointId": str(checkpoint.id),
            "seq": checkpoint.seq,
            "kind": checkpoint.kind,
        },
    )
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.suspended",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": reason, "attempt": run.attempt},
    )
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
    handoff_event = await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.handoff_prepared",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "claimId": str(claim.id),
            "checkpointId": str(checkpoint.id),
            "fencingToken": run.fencing_token,
            "reason": reason,
        },
    )
    return HandoffResult(
        run=run,
        task=task,
        checkpoint=checkpoint,
        event_cursor=encode_position(
            EventPosition(tx_id=handoff_event.tx_id, sequence=handoff_event.sequence)
        ),
    )


# The gates below are what ``POST /authz:check`` asks too (CP-ADR-0055,
# amendment of 2026-09-29): a change of who may cancel is made here, once.


async def request_cancel_gate(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> Run:
    await authorize(ctx, Permission.TASKS_WRITE, Permission.CLAIMS_MANAGE)
    return await _get_tenant_run(session, ctx, run_id)


async def cancel_gate(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> Run:
    await authorize(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)
    return await _get_tenant_run(session, ctx, run_id)


def require_cancel_holder(ctx: AuthContext, run: Run) -> None:
    if run.principal_id != ctx.principal_id and not ctx.has(Permission.CLAIMS_MANAGE):
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )


async def request_cancel_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    reason: str = "",
) -> Run:
    """Cooperative cancellation signal (any principal with tasks.write).

    Marks the run cancel-requested and emits ``run.cancel_requested``; the
    harness observes the event and stops, then finalizes via :cancel/:fail.
    ``cancel requested`` != ``execution stopped``: the authoritative stop is
    the terminal run transition, guarded by locks and fencing as usual.
    """
    run_probe = await request_cancel_gate(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)
    _require_run_running(run)

    if run.cancel_requested_at is not None:
        return run  # idempotent: signal already raised

    now = utcnow()
    run.cancel_requested_at = now
    run.cancel_requested_by = ctx.principal_id
    run.updated_at = now
    run.version += 1

    last_seq = (
        await session.scalar(
            select(func.max(RunControlMessage.seq)).where(RunControlMessage.run_id == run.id)
        )
    ) or 0
    control_message = RunControlMessage(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        task_id=task.id,
        seq=last_seq + 1,
        operation=RunControlOperation.REQUEST_CANCEL,
        status=RunControlStatus.ACCEPTED,
        causal_position="legacy:request-cancel",
        directive=None,
        reason=reason.strip(),
        safe_boundary=None,
        idempotency_key="legacy:request-cancel",
        requested_by_principal_id=ctx.principal_id,
        acknowledged_by_principal_id=None,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        version=1,
        accepted_at=now,
        resolved_at=None,
    )
    session.add(control_message)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.control_message.accepted",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "controlMessageId": str(control_message.id),
            "seq": control_message.seq,
            "operation": control_message.operation,
            "status": control_message.status,
            "causalPosition": control_message.causal_position,
        },
    )

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.cancel_requested",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": reason, "attempt": run.attempt},
    )
    return run


async def cancel_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    reason: str = "cancelled",
) -> Run:
    run_probe = await cancel_gate(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)
    require_cancel_holder(ctx, run)
    _require_run_running(run)

    now = utcnow()
    run.status = RunStatus.CANCELLED
    run.failure_reason = reason
    run.finished_at = now
    run.updated_at = now
    run.version += 1

    observability.inc("run_cancellations_total")
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.cancelled",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"taskId": str(task.id), "reason": reason, "attempt": run.attempt},
    )

    from control_plane.application.commands.child_runs import record_child_result

    await record_child_result(
        session, ctx, run=run, outcome="cancelled", output=None, failure_reason=reason
    )
    return run
