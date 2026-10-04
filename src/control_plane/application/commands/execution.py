"""Run checkpoints and the run action audit trail (v0.3).

Checkpoints are append-only durable execution metadata for restart/resume —
explicit operational state only, never hidden LLM reasoning or chat history.

Run actions are a lightweight execution audit (tool invocation started /
completed / failed) kept OUT of the domain event journal (ADR-0019): they
are telemetry-grade records at a different volume and trust level. Payload
inputs/outputs are never stored — only references and small metadata.

Both are written only by the run's legitimate live owner: the claim gate is
re-validated under the task row lock, so a zombie process cannot pollute the
audit trail of a taken-over task.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import observability
from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._child_ceiling import enforce_run_ceiling
from control_plane.application.commands.tasks import enforce_claim_gate
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_session_key_share
from control_plane.application.queries.tool_policy import (
    resolve_effective_tool_policy,
    resolve_tool_decision,
)
from control_plane.application.visibility import task_visible
from control_plane.domain.enums import (
    Permission,
    RunActionStatus,
    RunStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.tool_discovery import REASON_CHILD_GRANT
from control_plane.infrastructure.db.models import (
    Run,
    RunAction,
    RunCheckpoint,
    RunControlMessage,
    Skill,
    Task,
)

logger = logging.getLogger(__name__)


async def _locked_owned_running_run(
    session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID
) -> tuple[Task, Run]:
    """Lock task then run; require RUNNING status, ownership and a live claim.

    The run's session goes before the task (rule 2 of
    ``application/locking.py``, CP-ADR-0077 §3): the checkpoint or action
    written next references the run's session and principal, and
    ``principals/{id}:disable`` of whoever the session acts for holds the
    session before the task. The caller's principal is already locked by the
    write flow (rule 1).
    """
    run_probe = await session.scalar(
        select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
    )
    # A run of invisible work is a missing run (CP-ADR-0082 §3.7).
    if run_probe is None or not await task_visible(session, ctx, run_probe.task_id):
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    await lock_session_key_share(session, ctx.tenant_id, run_probe.session_id)
    task = await session.scalar(select(Task).where(Task.id == run_probe.task_id).with_for_update())
    run = await session.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or run is None:  # pragma: no cover - FK guarantees existence
        raise NotFoundError("Run not found", details={"runId": str(run_id)})

    if run.status != RunStatus.RUNNING:
        raise ConflictError(
            "run_not_active",
            "Run is not running",
            details={"runId": str(run.id), "status": run.status},
        )
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
    return task, run


def _check_duration_budget(run: Run) -> None:
    if run.max_duration_seconds is not None:
        deadline = run.started_at + timedelta(seconds=run.max_duration_seconds)
        if utcnow() >= deadline:
            raise ConflictError(
                "budget_exceeded",
                "Run exceeded its duration budget",
                details={
                    "runId": str(run.id),
                    "maxDurationSeconds": run.max_duration_seconds,
                    "startedAt": run.started_at.isoformat(),
                },
            )


# --- checkpoints --------------------------------------------------------------


async def create_checkpoint(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    kind: str,
    data: dict[str, Any] | None = None,
) -> RunCheckpoint:
    await authorize(ctx, Permission.TASKS_CLAIM)
    if not kind.strip():
        raise ValidationError("invalid_checkpoint", "kind must not be empty")
    task, run = await _locked_owned_running_run(session, ctx, run_id)
    await enforce_run_ceiling(session, ctx, run=run, permission=Permission.TASKS_CLAIM)
    _check_duration_budget(run)

    # seq is allocated under the run row lock -> gap-free per-run ordering.
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
        kind=kind.strip(),
        data=data or {},
        created_at=utcnow(),
    )
    session.add(checkpoint)
    await session.flush()

    # Event carries references only — checkpoint data stays out of the journal.
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
    return checkpoint


# --- run actions --------------------------------------------------------------


async def _authorize_tool_invocation(
    session: AsyncSession, ctx: AuthContext, run: Run, skill_ref: str
) -> tuple[Skill, bool]:
    """Re-derive the effective tool policy at execution time (HRS-3).

    Discovery is advisory. Between the moment a schema entered the prompt and
    the moment the action is reported, an assignment can be revoked, a version
    disabled or project governance tightened — so the decision is taken again
    here, from authoritative state, inside the same transaction and under the
    same locks as the audit record itself.

    Returns the skill and whether the harness declared it could run this
    protocol. The capability answer is recorded, never enforced: it is the
    client's own statement about itself, and a client must not be able to widen
    or narrow its authorization by what it declares.
    """
    policy = await resolve_effective_tool_policy(session, ctx, run_id=run.id)
    try:
        skill, _, decision = await resolve_tool_decision(session, ctx, skill_ref, policy)
    except NotFoundError as exc:
        # Unresolvable and unauthorized are one answer to the caller. The
        # refusal is NOT a domain event: nothing committed, and an event
        # written in a transaction that is about to roll back would either
        # vanish or force a second connection just to record a non-fact. It
        # surfaces where rejected writes already surface — a low-cardinality
        # metric and a server-side log line (the same shape as the fencing
        # rejection counter).
        observability.inc("tool_invocation_denied_total")
        logger.warning(
            "tool invocation denied",
            extra={
                "runId": str(run.id),
                "taskId": str(run.task_id),
                "principalId": str(ctx.principal_id),
                "tool": skill_ref[:200],
            },
        )
        raise AuthorizationError(
            "Tool is not authorized for this run",
            code="tool_not_authorized",
            details={"tool": skill_ref},
        ) from exc
    if not decision.authorized:
        observability.inc("tool_invocation_denied_total")
        logger.warning(
            "tool invocation denied",
            extra={
                "runId": str(run.id),
                "taskId": str(run.task_id),
                "principalId": str(ctx.principal_id),
                "tool": skill_ref[:200],
            },
        )
        if decision.reason == REASON_CHILD_GRANT:
            raise AuthorizationError(
                "Tool exceeds the skill grant of this child run",
                code="child_grant_exceeded",
                details={"tool": skill_ref},
            )
        raise AuthorizationError(
            "Tool is not authorized for this run",
            code="tool_not_authorized",
            details={"tool": skill_ref},
        )
    return skill, decision.capable


async def record_run_action(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    action: str,
    status: str = RunActionStatus.COMPLETED,
    skill_ref: str | None = None,
    external_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunAction:
    """Append one action record; enforces the run's action/duration budget."""
    await authorize(ctx, Permission.TASKS_CLAIM)
    if not action.strip():
        raise ValidationError("invalid_action", "action must not be empty")
    if status not in set(RunActionStatus):
        raise ValidationError("invalid_status", f"Unknown action status: {status}")
    task, run = await _locked_owned_running_run(session, ctx, run_id)
    await enforce_run_ceiling(session, ctx, run=run, permission=Permission.TASKS_CLAIM)
    _check_duration_budget(run)
    applied_cancel = await session.scalar(
        select(RunControlMessage.id)
        .where(
            RunControlMessage.run_id == run.id,
            RunControlMessage.operation == "request_cancel",
            RunControlMessage.status == "applied",
        )
        .limit(1)
    )
    if applied_cancel is not None:
        raise ConflictError(
            "run_cancel_requested",
            "Run acknowledged cooperative cancellation and cannot start new actions",
            details={"runId": str(run.id), "controlMessageId": str(applied_cancel)},
        )

    skill = None
    action_metadata = dict(metadata or {})
    if skill_ref is not None:
        # The child-handle ceiling (HRS-7) travels inside the effective tool
        # policy, so this one gate covers it too — no second enforcement point.
        skill, capable = await _authorize_tool_invocation(session, ctx, run, skill_ref)
        if not capable:
            action_metadata["capabilityMismatch"] = skill.protocol

    last_seq = (
        await session.scalar(select(func.max(RunAction.seq)).where(RunAction.run_id == run.id))
    ) or 0
    if run.max_actions is not None and last_seq >= run.max_actions:
        raise ConflictError(
            "budget_exceeded",
            "Run exceeded its action budget",
            details={"runId": str(run.id), "maxActions": run.max_actions},
        )

    now = utcnow()
    record = RunAction(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        task_id=task.id,
        principal_id=ctx.principal_id,
        session_id=run.session_id,
        skill_id=skill.id if skill else None,
        seq=last_seq + 1,
        action=action.strip(),
        status=status,
        external_reference=external_reference,
        metadata_json=action_metadata,
        started_at=now,
        finished_at=now if status != RunActionStatus.STARTED else None,
        created_at=now,
    )
    session.add(record)
    await session.flush()
    return record


async def finish_run_action(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    action_id: uuid.UUID,
    status: str,
    external_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunAction:
    """Complete a previously ``started`` action record.

    Same gate as recording one: only the run's live owner writes. Otherwise a
    zombie whose claim was taken over could keep appending outcomes to the
    audit trail of work it no longer owns. A dangling ``started`` action left
    behind by a lost lease stays unfinished — which is the honest record.
    """
    await authorize(ctx, Permission.TASKS_CLAIM)
    if status not in (RunActionStatus.COMPLETED, RunActionStatus.FAILED):
        raise ValidationError("invalid_status", "Finish status must be completed or failed")
    probe = await session.scalar(
        select(RunAction).where(RunAction.id == action_id, RunAction.tenant_id == ctx.tenant_id)
    )
    if probe is None:
        raise NotFoundError("Run action not found", details={"actionId": str(action_id)})
    # Lock order: task -> run -> action (global discipline), then re-read the
    # action under its own lock so the status check sees the locked row.
    await _locked_owned_running_run(session, ctx, probe.run_id)
    record = await session.scalar(
        select(RunAction)
        .where(RunAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if record is None:  # pragma: no cover - probe proved existence
        raise NotFoundError("Run action not found", details={"actionId": str(action_id)})
    if record.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Action belongs to another principal",
            code="run_holder_mismatch",
            details={"actionId": str(action_id)},
        )
    if record.status != RunActionStatus.STARTED:
        raise ConflictError(
            "action_already_finished",
            "Action is not in started state",
            details={"actionId": str(action_id), "status": record.status},
        )
    record.status = status
    record.finished_at = utcnow()
    if external_reference is not None:
        record.external_reference = external_reference
    if metadata:
        record.metadata_json = {**record.metadata_json, **metadata}
    return record
