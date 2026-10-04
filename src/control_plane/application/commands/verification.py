"""The verification stage: acceptance checks run before a task is done (CP-ADR-0067).

A task with acceptance checks is not set done by any of the three completion
paths (``:complete``, a run's ``:succeed``, an approval outcome's
``completeTask``): :func:`open_verification` releases its claim and opens an
**attempt** instead, and the task stays where it was, not claimable
(``verification_pending``). The worker picks up due attempts
(:func:`due_verifications`) and calls :func:`execute_verification`, which runs
the checks in the order they are declared:

* **authority** — the completer's: every call goes through the ordinary
  command with an ``AuthContext`` rebuilt from the credential snapshot taken
  at completion, which must still be active (as an approval outcome runs with
  its decider's);
* **deterministic** — a check with a ``skill`` queues a ``skill_invocation``
  through the path of ``POST /skills/{ref}:invoke``, requested by the attempt
  (``requestedBy.kind = verification``) and bound to the task; it passes when
  the call succeeds and its output equals ``expect``. A call without a result
  after ``skill_timeout`` is cancelled and the check fails ``no_result``;
* **artifact** — a ``deterministic`` check with an ``artifact`` passes at
  once, without waiting, when a head revision (ADR-0020) of an artifact of
  that type on the task itself fits its media types and, if content is
  required, has it stored (CP-ADR-0072 §9); otherwise it fails
  ``artifact_missing``, ``artifact_media_type`` or
  ``artifact_content_missing``. The required outputs of the task's type are
  such checks, run before its acceptance on every completion;
* **evidence** — an ``external_state`` check, and a ``deterministic`` one
  without a skill, passes by evidence of the task tied to it (with ``event``,
  an observation of that kind) and not spent by an earlier attempt; none
  after ``external_timeout`` is ``no_result``;
* **human, llm_judge** — a person's decision on a gate approval of the task:
  the approval whose ``completeTask`` outcome handed the task in counts at
  once, as does a gate approved after the attempt opened; otherwise the
  attempt waits (``waiting_human``) on the open gate, or requests one of the
  check's ``approver`` / ``approverRole`` (else the task's owner, then its
  assignee). Approved passes, rejected fails with the decision's comment;
  the decision wakes the attempt (:func:`wake_on_decision`). An
  ``llm_judge`` is decided by a person the same way — its rubric is shown;
* **outcome** — every check passed: the task moves into its completion
  status with ``task.completed`` and ``task.verified`` in one transaction, a
  ``verification`` artifact, and the work its type declares for after
  completion. The first failed check fails the attempt: the rest is not run,
  the task goes back to its ``releaseStatus`` with a comment on why, and the
  third failure in a row moves it to its first reachable ``blocked`` status
  instead. A cancelled task closes its open attempt ``cancelled``.

Core knows tasks, checks, skills, evidence and approvals here — never what
is checked or what a tenant does with it.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from control_plane.application.authorization import AuthContext
from control_plane.application.commands._artifact_content import (
    CONTENT_STORED,
    artifact_event_fields,
)
from control_plane.application.commands.approval_outcomes import (
    DecisionContext,
    authority_snapshot,
    failure_of,
    read_context,
    require_active_credential,
    task_view,
)
from control_plane.application.commands.artifact_types import latest_artifact_type
from control_plane.application.commands.principals import ensure_core_principal
from control_plane.application.commands.role_references import (
    is_role_reference,
    require_declared_role,
    role_for_task,
)
from control_plane.application.commands.skill_invocations import (
    CANCELLED_BY_SYSTEM,
    LIVE_STATUSES,
    cancel_skill_invocation,
    invoke_skill,
    resolve_skill_ref,
)
from control_plane.application.commands.task_comments import add_comment
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import mark_task_completed
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principal_key_share
from control_plane.application.visibility import with_visibility
from control_plane.domain.approval_outcomes import Path, expressions_in, render
from control_plane.domain.artifact_schema import CONTENT_REQUIRED
from control_plane.domain.artifact_type import media_type_allowed
from control_plane.domain.completion_work import is_met
from control_plane.domain.enums import (
    ApprovalStatus,
    Permission,
    PrincipalKind,
    SkillInvocationRequester,
    SkillInvocationStatus,
    SkillSideEffects,
)
from control_plane.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from control_plane.domain.work_graph import (
    DECISION_KINDS,
    EXTERNAL_WRITE_WITHOUT_DECISION,
    INVALID_ACCEPTANCE_SPEC,
    CheckKind,
    EvidenceKind,
    condition_paths,
    decision_before,
)
from control_plane.domain.work_item import TERMINAL_CATEGORIES, WorkItemStatusCategory
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    Event,
    EventArchive,
    Principal,
    Skill,
    SkillInvocation,
    Task,
    TaskVerification,
)

RUNNING = "running"
WAITING_HUMAN = "waiting_human"
WAITING_EXTERNAL = "waiting_external"
PASSED = "passed"
FAILED = "failed"
CANCELLED = "cancelled"
# A check whose ``when`` does not hold: not run, not a failure (amendment 2026-09-27).
SKIPPED = "skipped"
OPEN_STATUSES = (RUNNING, WAITING_HUMAN, WAITING_EXTERNAL)

TRIGGERS = frozenset({"run", "complete", "approval", "rule"})

# The third failed attempt in a row hands the task to a person.
MAX_CONSECUTIVE_FAILURES = 3
# Type of the artifact a passed attempt leaves on the task.
VERIFICATION_ARTIFACT = "verification"
# Idempotency key of a check's skill call: one per (attempt, check).
INVOCATION_KEY_PREFIX = "verification:"

DEFAULT_CHECK_SECONDS = 15.0
DEFAULT_SKILL_TIMEOUT_SECONDS = 900.0
DEFAULT_EXTERNAL_TIMEOUT_SECONDS = 86400.0

# Why a check failed (``results[].reason``, ``task.verification_failed``).
NO_RESULT = "no_result"
EXPECTATION_NOT_MET = "expectation_not_met"
SKILL_FAILED = "skill_failed"
SKILL_CANCELLED = "skill_cancelled"
APPROVAL_REJECTED = "approval_rejected"
APPROVAL_CANCELLED = "approval_cancelled"
NO_APPROVER = "no_approver"
ARTIFACT_MISSING = "artifact_missing"
ARTIFACT_MEDIA_TYPE = "artifact_media_type"
ARTIFACT_CONTENT_MISSING = "artifact_content_missing"
# A check with ``when`` whose condition does not hold is skipped for this reason.
CONDITION_UNMET = "condition_unmet"
# An external write with no decision passed before it in the same attempt.
NO_DECISION = "no_decision"
# Why an attempt was cancelled.
TASK_CANCELLED = "task_cancelled"
# The reason given to a skill call the stage stops waiting for.
SKILL_TIMEOUT = "verification_skill_timeout"
ATTEMPT_CLOSED = "verification_closed"
# What an approval a check cites is, as evidence.
APPROVAL_EVIDENCE = "approval"
MAX_APPROVAL_COMMENT = 4000


# --- reads -----------------------------------------------------------------------


async def open_attempt(session: AsyncSession, task_id: uuid.UUID) -> TaskVerification | None:
    """The task's open attempt, if one is running or waiting."""
    row: TaskVerification | None = await session.scalar(
        select(TaskVerification)
        .where(TaskVerification.task_id == task_id, TaskVerification.status.in_(OPEN_STATUSES))
        .execution_options(populate_existing=True)
    )
    return row


def _fact_identity(item: dict[str, Any]) -> str:
    """One fact tied to one check, whatever its note says."""
    return json.dumps({k: v for k, v in item.items() if k != "note"}, sort_keys=True)


def _spend(task: Task, row: TaskVerification) -> None:
    """A closing attempt spends the facts tied to checks the task has now."""
    row.spent_evidence = [
        {k: v for k, v in item.items() if k != "note"}
        for item in task.evidence or []
        if item.get("check") is not None
    ]


async def current_evidence(
    session: AsyncSession, task: Task, row: TaskVerification
) -> list[dict[str, Any]]:
    """The task's evidence the attempt ``row`` may count (CP-ADR-0063 Zh7).

    A fact tied to a check that was on the task when an earlier attempt
    closed is spent: work handed in anew after a failed attempt needs a new
    fact, not the one the rejected work was checked on. Facts without a
    check are not spent — no check reads them.
    """
    documents = (
        await session.scalars(
            select(TaskVerification.spent_evidence).where(
                TaskVerification.task_id == task.id,
                TaskVerification.attempt < row.attempt,
                TaskVerification.spent_evidence.is_not(None),
            )
        )
    ).all()
    spent = {_fact_identity(item) for document in documents for item in document or []}
    return [
        item
        for item in task.evidence or []
        if item.get("check") is None or _fact_identity(item) not in spent
    ]


async def latest_attempts(
    session: AsyncSession, tenant_id: uuid.UUID, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TaskVerification]:
    """The newest attempt of each task, for a whole page in one query."""
    if not task_ids:
        return {}
    rows = (
        await session.scalars(
            select(TaskVerification)
            .where(TaskVerification.tenant_id == tenant_id, TaskVerification.task_id.in_(task_ids))
            .order_by(TaskVerification.task_id, TaskVerification.attempt.desc())
            .distinct(TaskVerification.task_id)
        )
    ).all()
    return {row.task_id: row for row in rows}


async def check_verification_gate(session: AsyncSession, task: Task) -> None:
    """Raise 409 ``verification_pending`` while the task's checks are running.

    The work was handed in; until the attempt closes, nobody takes it again.
    """
    row = await open_attempt(session, task.id)
    if row is not None:
        raise ConflictError(
            "verification_pending",
            "Task is waiting for its acceptance checks",
            details={"taskId": str(task.id), "verificationId": str(row.id), "status": row.status},
        )


def summary(row: TaskVerification | None) -> dict[str, Any] | None:
    """``TaskOut.verification``: the newest attempt, in brief."""
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status,
        "attempt": row.attempt,
        "updatedAt": row.updated_at.isoformat(),
    }


# --- opening ---------------------------------------------------------------------


async def check_acceptance_skills(
    session: AsyncSession,
    ctx: AuthContext,
    checks: list[dict[str, Any]],
    *,
    field: str = "acceptance",
    before: list[dict[str, Any]] | None = None,
) -> None:
    """What ``deterministic`` checks name is registered; an external write follows a decision.

    The grammar (``check_spec``) knows the form only; the registries are
    asked here, when the acceptance is written. A check whose skill is
    ``external_write`` is admitted only after a ``human`` / ``llm_judge``
    check under the same condition (``decision_before``): the decision is
    the basis of the write (ADR-0056 §4; CP-ADR-0067, amendment 2026-09-27,
    B7). ``before`` — the checks that run ahead of ``checks`` in every
    attempt: the task type's, for a task's own acceptance. A check on an
    artifact names an artifact type registered in the tenant (CP-ADR-0072 §9).
    """
    ahead = list(before or [])
    for index, check in enumerate(checks):
        role = (check.get("spec") or {}).get("approverRole")
        if is_role_reference(role):
            # A role of the package by slug (CP-ADR-0061, amendment
            # 2026-10-01): the tenant has it, or nobody could be asked.
            await require_declared_role(
                session, ctx.tenant_id, role, field=f"{field}[{index}].spec.approverRole"
            )
        if check["kind"] != CheckKind.DETERMINISTIC:
            continue
        spec = check.get("spec") or {}
        artifact = spec.get("artifact")
        if artifact is not None:
            path = f"{field}[{index}].spec.artifact.type"
            if await latest_artifact_type(session, ctx.tenant_id, artifact["type"]) is None:
                raise ValidationError(
                    INVALID_ACCEPTANCE_SPEC,
                    f"{path}: artifact type {artifact['type']!r} is not registered",
                    details={"field": path, "kind": check["kind"]},
                )
            continue
        ref = spec.get("skill")
        if not ref:
            continue
        path = f"{field}[{index}].spec.skill"
        try:
            skill = await resolve_skill_ref(session, ctx, ref)
        except NotFoundError as exc:
            raise ValidationError(
                INVALID_ACCEPTANCE_SPEC,
                f"{path}: skill {ref!r} is not registered",
                details={"field": path, "kind": check["kind"]},
            ) from exc
        if skill.side_effects == SkillSideEffects.EXTERNAL_WRITE and not decision_before(
            [*ahead, *checks], len(ahead) + index
        ):
            raise ValidationError(
                INVALID_ACCEPTANCE_SPEC,
                f"{path}: {ref!r} writes to an external system; a check may only after "
                "a human or llm_judge check under the same condition",
                details={
                    "field": path,
                    "kind": check["kind"],
                    "sideEffects": skill.side_effects,
                    "cause": EXTERNAL_WRITE_WITHOUT_DECISION,
                },
            )


async def open_verification(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    *,
    session_id: uuid.UUID | None,
    trigger: str,
    trigger_ref: str | None,
    checks: list[dict[str, Any]] | None = None,
) -> Task:
    """Open an attempt on a locked task whose completion was just requested.

    The caller has released the claim and holds the task row lock, which
    serializes this with every other completion of the task; the partial
    unique index is the net under it. The task keeps its status: only a
    passed attempt moves it into its completion status. ``checks`` default to
    the task's acceptance; a rule closing work without acceptance passes its
    implicit check instead.
    """
    assert trigger in TRIGGERS
    now = utcnow()
    last = await session.scalar(
        select(func.max(TaskVerification.attempt)).where(TaskVerification.task_id == task.id)
    )
    row = TaskVerification(
        id=new_uuid(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        attempt=(last or 0) + 1,
        status=RUNNING,
        trigger=trigger,
        trigger_ref=trigger_ref,
        authority_principal_id=ctx.principal_id,
        authority=authority_snapshot(ctx),
        correlation_id=ctx.correlation_id,
        checks=list(checks if checks is not None else task.acceptance),
        results=[],
        cursor=0,
        skill_invocation_id=None,
        approval_id=None,
        next_check_at=now,
        started_at=now,
        finished_at=None,
        updated_at=now,
    )
    session.add(row)
    task.updated_at = now
    task.version += 1
    await session.flush()
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verification_started",
        payload={"trigger": trigger, "triggerRef": trigger_ref},
        session_id=session_id,
    )
    return task


# --- cancelling ------------------------------------------------------------------


async def cancel_open_attempt(session: AsyncSession, ctx: AuthContext, task: Task) -> None:
    """Close the open attempt of a (locked) task that was just cancelled.

    Its live skill call is cancelled too: nothing it could report would be
    used. A cancelled attempt is never counted as a success or a failure.
    """
    row = await open_attempt(session, task.id)
    if row is None:
        return
    locked = await _lock_row(session, row.id)
    if locked is None or locked.status not in OPEN_STATUSES:
        return
    # The call is the completer's: stopped with their authority, whoever
    # cancelled the task.
    await _stop_call(session, _authority_context(locked, ctx.trace_run_id), locked, ATTEMPT_CLOSED)
    now = utcnow()
    _spend(task, locked)
    locked.status = CANCELLED
    locked.next_check_at = None
    locked.finished_at = now
    locked.updated_at = now
    locked.results = [
        *locked.results,
        *(
            [_result(locked.checks[locked.cursor], CANCELLED, reason=TASK_CANCELLED)]
            if locked.cursor < len(locked.checks)
            else []
        ),
    ]
    await session.flush()
    await _withdraw_request(session, ctx, task, locked)


# --- the worker's pass -----------------------------------------------------------


async def due_verifications(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Open attempts whose next look is due, oldest first."""
    rows = await session.scalars(
        select(TaskVerification.id)
        .where(
            TaskVerification.status.in_(OPEN_STATUSES),
            TaskVerification.next_check_at <= utcnow(),
        )
        .order_by(TaskVerification.next_check_at)
        .limit(limit)
    )
    return list(rows.all())


async def postpone_verification(
    session: AsyncSession, *, verification_id: uuid.UUID, seconds: float
) -> None:
    """Look at an attempt whose pass broke unexpectedly again only later."""
    row = await _lock_row(session, verification_id)
    if row is not None and row.status in OPEN_STATUSES:
        row.next_check_at = utcnow() + timedelta(seconds=seconds)
        row.updated_at = utcnow()


@dataclass
class Timing:
    check: timedelta = timedelta(seconds=DEFAULT_CHECK_SECONDS)
    skill_timeout: timedelta = timedelta(seconds=DEFAULT_SKILL_TIMEOUT_SECONDS)
    external_timeout: timedelta = timedelta(seconds=DEFAULT_EXTERNAL_TIMEOUT_SECONDS)


@dataclass
class _Outcome:
    """What one look at a check found: a result, or a reason to wait."""

    status: str  # passed | skipped | failed | running | waiting_human | waiting_external
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None
    next_check_at: datetime | None = None
    details: dict[str, Any] | None = None


async def execute_verification(
    session: AsyncSession,
    *,
    verification_id: uuid.UUID,
    trace_run_id: str = "",
    timing: Timing | None = None,
) -> TaskVerification | None:
    """Run an open attempt as far as it goes now.

    A no-op for an attempt that is no longer open, or whose task another
    transaction holds (``SKIP LOCKED``: several workers may scan side by
    side). Lock order is the codebase's: the task row, then the attempt.
    """
    timing = timing or Timing()
    probe = await session.get(TaskVerification, verification_id, populate_existing=True)
    if probe is None or probe.status not in OPEN_STATUSES:
        return probe
    # The attempt acts as its authority (the completer): the calls, artifacts
    # and approvals it writes reference that principal, so it is locked before
    # the task (rule 1 of ``application/locking.py``, CP-ADR-0077 §3). The
    # authority of an attempt never changes; the probe's value is the one.
    await lock_principal_key_share(session, probe.tenant_id, probe.authority_principal_id)
    task: Task | None = await session.scalar(
        select(Task)
        .where(Task.id == probe.task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        return None
    row = await _lock_row(session, verification_id)
    if row is None or row.status not in OPEN_STATUSES:
        return row
    # The completer acts within their visibility as it stands now (CP-ADR-0082 V2).
    ctx = await with_visibility(session, _authority_context(row, trace_run_id))
    if task.system_status_category in TERMINAL_CATEGORIES:
        # Closed under the attempt by a path that did not cancel it.
        await cancel_open_attempt(session, ctx, task)
        return row

    try:
        await require_active_credential(
            session,
            authority=row.authority,
            principal_id=row.authority_principal_id,
            subject="the task was completed with",
        )
    except DomainError as exc:
        error = failure_of(exc)
        return await _fail(
            session,
            ctx,
            task,
            row,
            _Outcome(FAILED, reason=error["code"], message=error["message"]),
        )

    while row.cursor < len(row.checks):
        check = row.checks[row.cursor]
        outcome = None
        if row.status == RUNNING and row.skill_invocation_id is None:
            # Just arrived at the check: its condition is read now, not when
            # the attempt opened — a check before it may have waited for days.
            outcome = await _condition(session, ctx, task, check)
        if outcome is None:
            outcome = await _look(session, ctx, task, row, check, timing)
        if outcome.status == FAILED:
            return await _fail(session, ctx, task, row, outcome)
        if outcome.status not in (PASSED, SKIPPED):
            row.status = outcome.status
            row.next_check_at = outcome.next_check_at
            row.updated_at = utcnow()
            await session.flush()
            return row
        row.results = [
            *row.results,
            _result(
                check,
                outcome.status,
                evidence=outcome.evidence,
                reason=outcome.reason,
                details=outcome.details,
            ),
        ]
        row.cursor += 1
        row.skill_invocation_id = None
        row.approval_id = None
        row.status = RUNNING
        row.updated_at = utcnow()
        await session.flush()
    return await _pass(session, ctx, task, row)


async def _condition(
    session: AsyncSession, ctx: AuthContext, task: Task, check: dict[str, Any]
) -> _Outcome | None:
    """``skipped`` if the check's ``when`` does not hold; ``None`` to run it.

    Read as the completer, like the check's inputs. What it cannot read
    fails the check: whether it is due cannot even be told.
    """
    conditions = condition_paths(check)
    if not conditions:
        return None
    context = _task_context(task, ctx)
    try:
        async with session.begin_nested():
            await read_context(session, ctx, context, (), extra=conditions)
    except DomainError as exc:
        error = failure_of(exc)
        return _Outcome(FAILED, reason=error["code"], message=error["message"])
    unmet = next((c for c in conditions if not is_met(context.resolve(c))), None)
    if unmet is None:
        return None
    return _Outcome(SKIPPED, reason=CONDITION_UNMET, details={"when": unmet.text})


def _task_context(task: Task, ctx: AuthContext) -> DecisionContext:
    return DecisionContext(
        approval_id=None,
        decided_by=ctx.principal_id,
        outcome="verification",
        approval={},
        task=task_view(task),
        spawned_by={},
    )


async def _look(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    kind = check["kind"]
    spec = check.get("spec") or {}
    if kind == CheckKind.DETERMINISTIC and spec.get("skill"):
        return await _skill_check(session, ctx, task, row, spec, timing)
    if kind == CheckKind.DETERMINISTIC and spec.get("artifact"):
        return await _artifact_check(session, task, spec["artifact"])
    if kind in (CheckKind.DETERMINISTIC, CheckKind.EXTERNAL_STATE):
        return await _evidence_check(session, task, row, check, spec, timing)
    return await _decision_check(session, ctx, task, row, check)


# --- deterministic: a skill call -------------------------------------------------


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


async def _skill_check(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    spec: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    now = utcnow()
    if row.skill_invocation_id is None:
        try:
            async with session.begin_nested():
                caller, approval_id = ctx, None
                skill_row = await resolve_skill_ref(session, ctx, spec["skill"])
                if skill_row.side_effects == SkillSideEffects.EXTERNAL_WRITE:
                    basis = await _decision_basis(session, row, ctx.trace_run_id)
                    if isinstance(basis, _Outcome):
                        return basis
                    caller, approval_id = basis
                inputs = await _render_inputs(session, ctx, task, spec.get("inputs") or {})
                queued = await invoke_skill(
                    session,
                    caller,
                    skill_ref=spec["skill"],
                    inputs=inputs,
                    idempotency_key=f"{INVOCATION_KEY_PREFIX}{row.id}:{row.cursor}",
                    task_ref=str(task.id),
                    approval_id=approval_id,
                    requested_by=(SkillInvocationRequester.VERIFICATION, str(row.id)),
                )
        except DomainError as exc:
            # The completer may not make the call, the skill is gone or its
            # inputs do not fit: the check cannot pass, and says why.
            error = failure_of(exc)
            return _Outcome(FAILED, reason=error["code"], message=error["message"])
        row.skill_invocation_id = queued.invocation.id
        return _Outcome(RUNNING, next_check_at=now + min(timing.check, timing.skill_timeout))

    invocation = await session.get(SkillInvocation, row.skill_invocation_id, populate_existing=True)
    assert invocation is not None  # pragma: no cover - rows are never deleted
    evidence = [{"kind": "skill_invocation", "ref": str(invocation.id)}]
    if invocation.status in LIVE_STATUSES:
        deadline = invocation.created_at + timing.skill_timeout
        if deadline > now:
            return _Outcome(RUNNING, next_check_at=min(now + timing.check, deadline))
        await _stop_call(session, ctx, row, SKILL_TIMEOUT)
        return _Outcome(
            FAILED,
            evidence=evidence,
            reason=NO_RESULT,
            message=f"no result within {int(timing.skill_timeout.total_seconds())} s",
        )
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    name = f"{skill.name}@{skill.version}"
    if invocation.status == SkillInvocationStatus.SUCCEEDED:
        artifact = await _result_artifact(session, task, invocation)
        if artifact is not None:
            evidence.append({"kind": "artifact", "ref": str(artifact)})
        mismatch = _mismatch(invocation.output or {}, spec.get("expect") or {})
        if not mismatch:
            return _Outcome(PASSED, evidence=evidence)
        return _Outcome(
            FAILED,
            evidence=evidence,
            reason=EXPECTATION_NOT_MET,
            message=f"{name}: output does not match expect in {mismatch}",
        )
    error = invocation.error or {}
    return _Outcome(
        FAILED,
        evidence=evidence,
        reason=SKILL_CANCELLED
        if invocation.status == SkillInvocationStatus.CANCELLED
        else SKILL_FAILED,
        message=f"{name} ended {invocation.status}: {error.get('code')}: {error.get('message')}",
    )


async def _decision_basis(
    session: AsyncSession, row: TaskVerification, trace_run_id: str
) -> tuple[AuthContext, uuid.UUID] | _Outcome:
    """Who writes outside for the check, and on which decision (amendment 2026-09-27, B7).

    The gate approval counted for the nearest ``human`` / ``llm_judge`` check
    that passed earlier in this attempt; the call is made with the authority
    of whoever decided it — they allowed the write, not the completer — and
    cites the approval as its basis (ADR-0056 §4). A decision skipped by its
    condition, or none at all, is ``no_decision``.
    """
    approval: Approval | None = None
    for result in reversed(row.results):
        if result["kind"] not in DECISION_KINDS or result["status"] != PASSED:
            continue
        cited = next(
            (e["ref"] for e in result["evidence"] if e.get("kind") == APPROVAL_EVIDENCE), None
        )
        if cited is not None:
            approval = await session.get(Approval, uuid.UUID(cited))
        break
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or approval.decision_by_principal_id is None
        or not approval.decision_authority
    ):
        return _Outcome(
            FAILED,
            reason=NO_DECISION,
            message="the check writes to an external system and no decision of a person "
            "passed before it in this attempt",
        )
    await require_active_credential(
        session,
        authority=approval.decision_authority,
        principal_id=approval.decision_by_principal_id,
        subject="the approval was decided with",
    )
    caller = _snapshot_context(
        approval.decision_authority,
        tenant_id=row.tenant_id,
        principal_id=approval.decision_by_principal_id,
        request_id=f"verification:{row.id}",
        correlation_id=row.correlation_id,
        trace_run_id=trace_run_id,
    )
    return await with_visibility(session, caller), approval.id


async def _render_inputs(
    session: AsyncSession, ctx: AuthContext, task: Task, inputs: dict[str, Any]
) -> dict[str, Any]:
    """The check's inputs, their ``$.task…`` expressions read as the completer."""
    paths: list[Path] = [path for text in _strings(inputs) for path in expressions_in(text)]
    context = _task_context(task, ctx)
    await read_context(session, ctx, context, (), extra=tuple(paths))
    rendered = render(inputs, context.resolve)
    assert isinstance(rendered, dict)
    return rendered


def _mismatch(output: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Output fields that differ from ``expect`` (strict JSON equality)."""
    return sorted(
        key
        for key, value in expect.items()
        if key not in output or type(output[key]) is not type(value) or output[key] != value
    )


async def _result_artifact(
    session: AsyncSession, task: Task, invocation: SkillInvocation
) -> uuid.UUID | None:
    artifact_id: uuid.UUID | None = await session.scalar(
        select(Artifact.id)
        .where(
            Artifact.tenant_id == task.tenant_id,
            Artifact.task_id == task.id,
            Artifact.type == "skill_result",
            Artifact.metadata_json["invocationId"].astext == str(invocation.id),
        )
        .limit(1)
    )
    return artifact_id


async def _stop_call(
    session: AsyncSession, ctx: AuthContext, row: TaskVerification, reason: str
) -> None:
    """Cancel the attempt's live skill call; a finished one is left alone."""
    if row.skill_invocation_id is None:
        return
    invocation = await session.get(SkillInvocation, row.skill_invocation_id)
    if invocation is None or invocation.status not in LIVE_STATUSES:
        return
    try:
        async with session.begin_nested():
            await cancel_skill_invocation(
                session,
                ctx,
                invocation_id=invocation.id,
                reason=reason,
                initiator=CANCELLED_BY_SYSTEM,
            )
    except DomainError:
        # The completer's authority can no longer cancel it (its lease bounds
        # it anyway); the attempt does not wait for its result either way.
        return


# --- deterministic: an artifact of the task -------------------------------------


async def _artifact_check(session: AsyncSession, task: Task, spec: dict[str, Any]) -> _Outcome:
    """A head revision of the artifact type on the task itself, looked at now.

    Records only: the content store is not read, so its outage fails nothing
    here. The reason is that of the first step no head revision gets past:
    none of the type, none of the media types, none with stored content.
    """
    newer = aliased(Artifact)
    heads = (
        await session.scalars(
            select(Artifact)
            .where(
                Artifact.tenant_id == task.tenant_id,
                Artifact.task_id == task.id,
                Artifact.type == spec["type"],
                ~exists().where(
                    newer.tenant_id == task.tenant_id, newer.supersedes_artifact_id == Artifact.id
                ),
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .execution_options(populate_existing=True)
        )
    ).all()
    if not heads:
        return _Outcome(
            FAILED,
            reason=ARTIFACT_MISSING,
            message=f"the task has no artifact of type {spec['type']!r}",
        )
    patterns = [p.strip().lower() for p in spec.get("mediaTypes") or ()]
    if patterns:
        # A reference has no media type: it fits no declared one.
        heads = [a for a in heads if a.media_type and media_type_allowed(patterns, a.media_type)]
        if not heads:
            return _Outcome(
                FAILED,
                reason=ARTIFACT_MEDIA_TYPE,
                message=f"no artifact of type {spec['type']!r} has a media type in {patterns}",
            )
    if spec.get("content", CONTENT_REQUIRED) == CONTENT_REQUIRED:
        heads = [a for a in heads if a.content_state == CONTENT_STORED]
        if not heads:
            return _Outcome(
                FAILED,
                reason=ARTIFACT_CONTENT_MISSING,
                message=f"no artifact of type {spec['type']!r} has its content stored",
            )
    evidence = [{"kind": EvidenceKind.ARTIFACT.value, "ref": str(heads[0].id)}]
    return _Outcome(PASSED, evidence=evidence)


# --- deterministic without a skill, external_state: evidence ---------------------


async def _evidence_check(
    session: AsyncSession,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    spec: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    """Evidence of the task tied to the check — read fresh, it may arrive late.

    Only facts not spent by an earlier attempt count (:func:`current_evidence`).
    """
    tied = [
        item
        for item in await current_evidence(session, task, row)
        if item.get("check") == check["key"]
    ]
    wanted = spec.get("event")
    if wanted:
        tied = [item for item in tied if await _observation_of_kind(session, task, item, wanted)]
    if tied:
        return _Outcome(PASSED, evidence=[_pointer(item) for item in tied])
    now = utcnow()
    deadline = row.started_at + timing.external_timeout
    if deadline > now:
        return _Outcome(WAITING_EXTERNAL, next_check_at=min(now + timing.check, deadline))
    return _Outcome(
        FAILED,
        reason=NO_RESULT,
        message=(
            f"no evidence for {check['key']!r}"
            + (f" of kind {wanted!r}" if wanted else "")
            + f" within {int(timing.external_timeout.total_seconds())} s"
        ),
    )


async def _observation_of_kind(
    session: AsyncSession, task: Task, item: dict[str, Any], kind: str
) -> bool:
    if item.get("kind") != EvidenceKind.OBSERVATION:
        return False
    for table in (Event, EventArchive):
        payload = await session.scalar(
            select(table.payload).where(
                table.tenant_id == task.tenant_id,
                table.entity_type == "observation",
                table.event_type == "observation.recorded",
                table.entity_id == uuid.UUID(item["observationId"]),
            )
        )
        if payload is not None:
            return bool(payload.get("kind") == kind)
    return False


def _pointer(item: dict[str, Any]) -> dict[str, Any]:
    """An evidence item as a result cites it: the pointer, not the note."""
    return {k: v for k, v in item.items() if k not in ("note", "check")}


# --- human, llm_judge: a decision on a gate approval -----------------------------


async def _decision_check(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
) -> _Outcome:
    """A person's decision on the task's gate approval (CP-ADR-0067 §6).

    Nothing to look at on a timer: the attempt waits without one, and the
    decision wakes it (:func:`wake_on_decision`).
    """
    if row.approval_id is None:
        counted = await _counted_approval(session, task, row)
        if counted is not None:
            return _Outcome(PASSED, evidence=[_approval_pointer(counted)])
        waited: Approval | None = await session.scalar(
            select(Approval)
            .where(
                Approval.tenant_id == task.tenant_id,
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at, Approval.id)
            .limit(1)
        )
        if waited is None:
            try:
                async with session.begin_nested():
                    waited = await _request_decision(session, ctx, task, row, check)
            except DomainError as exc:
                error = failure_of(exc)
                return _Outcome(FAILED, reason=error["code"], message=error["message"])
            if waited is None:
                return _Outcome(
                    FAILED,
                    reason=NO_APPROVER,
                    message="the check names no approver and the task's owner and assignee "
                    "are not people",
                )
        row.approval_id = waited.id
        return _Outcome(WAITING_HUMAN)

    approval = await session.get(Approval, row.approval_id, populate_existing=True)
    assert approval is not None  # pragma: no cover - rows are never deleted
    if approval.status == ApprovalStatus.PENDING:
        return _Outcome(WAITING_HUMAN)
    evidence = [_approval_pointer(approval)]
    if approval.status == ApprovalStatus.APPROVED:
        return _Outcome(PASSED, evidence=evidence)
    return _Outcome(
        FAILED,
        evidence=evidence,
        reason=APPROVAL_REJECTED
        if approval.status == ApprovalStatus.REJECTED
        else APPROVAL_CANCELLED,
        message=f"approval {approval.status}: {approval.comment}"
        if approval.comment
        else f"approval {approval.status}",
    )


async def _counted_approval(
    session: AsyncSession, task: Task, row: TaskVerification
) -> Approval | None:
    """An approved gate that already decides the check, if there is one.

    The decision whose ``completeTask`` outcome handed the task in counts for
    every ``human`` check of its attempt. Otherwise a gate of the task
    approved since the attempt opened counts once: each check of the attempt
    needs a decision of its own.
    """
    if row.trigger == "approval" and row.trigger_ref:
        trigger = await session.get(Approval, uuid.UUID(row.trigger_ref))
        if (
            trigger is not None
            and trigger.task_id == task.id
            and trigger.status == ApprovalStatus.APPROVED
        ):
            return trigger
    cited = {
        item["ref"]
        for result in row.results
        for item in result["evidence"]
        if item.get("kind") == APPROVAL_EVIDENCE
    }
    candidates = (
        await session.scalars(
            select(Approval)
            .where(
                Approval.tenant_id == task.tenant_id,
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.APPROVED,
                Approval.decision_at >= row.started_at,
            )
            .order_by(Approval.decision_at, Approval.id)
        )
    ).all()
    return next((a for a in candidates if str(a.id) not in cited), None)


async def _request_decision(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
) -> Approval | None:
    """Ask for the decision: a gate approval of the task, filed by core.

    Filed by core's own principal, narrowed to ``approvals.manage``, like the
    comment on a failed attempt: the typical completer is a runner, which asks
    no one for anything, and core can withdraw its own request when the
    attempt closes. Without ``approver`` / ``approverRole`` it goes to a
    person only (:func:`_person_to_ask`). ``None`` if nobody is there to ask.
    """
    from control_plane.application.commands.approvals import request_approval

    spec = check.get("spec") or {}
    role = spec.get("approverRole")
    principal = spec.get("approver") or (None if role else await _person_to_ask(session, task))
    if role is None and principal is None:
        return None
    lines = [
        f"Acceptance check {check['key']!r} ({check['kind']}) of {task.public_id}, "
        f"verification attempt #{row.attempt}: {check.get('description') or ''}".rstrip(": "),
    ]
    if spec.get("rubric"):
        lines.append(f"Rubric: {spec['rubric']}")
    return await request_approval(
        session,
        await _core_context(session, task, ctx, Permission.APPROVALS_MANAGE),
        task_ref=str(task.id),
        workspace_id=task.workspace_id,
        required_role_id=await _approver_role(session, ctx, task, role),
        assigned_principal_id=uuid.UUID(str(principal)) if principal else None,
        comment="\n".join(lines)[:MAX_APPROVAL_COMMENT],
        gate=True,
    )


async def _approver_role(
    session: AsyncSession, ctx: AuthContext, task: Task, role: Any
) -> uuid.UUID | None:
    """``approverRole``: a role id, or ``role:<slug>`` seen from the task's workspace."""
    if not role:
        return None
    if is_role_reference(role):
        return await role_for_task(
            session, ctx, str(role), workspace_id=task.workspace_id, field="spec.approverRole"
        )
    return uuid.UUID(str(role))


async def _person_to_ask(session: AsyncSession, task: Task) -> uuid.UUID | None:
    """The task's owner, else its assignee — whichever is a person.

    An agent is never asked to accept work, least of all its own: a runner
    that executed the task would otherwise approve its own result. Nobody
    human — ``None``, and the check fails ``no_approver``.
    """
    for principal_id in (task.owner_id, task.assignee_id):
        if principal_id is None:
            continue
        kind = await session.scalar(select(Principal.kind).where(Principal.id == principal_id))
        if kind == PrincipalKind.HUMAN:
            return principal_id
    return None


async def _withdraw_request(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> None:
    """Cancel the gate a closed attempt filed and nobody decided: it would hold the task.

    A gate the attempt only waited on is somebody else's and is left alone.
    """
    from control_plane.application.commands.approvals import cancel_approval

    if row.approval_id is None:
        return
    approval = await session.get(Approval, row.approval_id, populate_existing=True)
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    core = await _core_context(session, task, ctx, Permission.APPROVALS_MANAGE)
    if approval.requested_by_principal_id != core.principal_id:
        return
    try:
        async with session.begin_nested():
            await cancel_approval(session, core, approval_id=approval.id)
    except DomainError:
        # Left pending; whoever it is assigned to can still close it.
        return


def _approval_pointer(approval: Approval) -> dict[str, Any]:
    return {"kind": APPROVAL_EVIDENCE, "ref": str(approval.id)}


async def wake_on_decision(session: AsyncSession, approval: Approval) -> None:
    """A gate of a task was decided or withdrawn: look at its waiting attempt now.

    A plain ``UPDATE``: if the worker holds the attempt, this waits for it and
    then sees the status it left, so a decision is never slept through.
    """
    if not approval.gate or approval.task_id is None:
        return
    await session.execute(
        update(TaskVerification)
        .where(
            TaskVerification.task_id == approval.task_id,
            TaskVerification.status == WAITING_HUMAN,
        )
        .values(next_check_at=utcnow(), updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


async def wake_on_evidence(session: AsyncSession, task_id: uuid.UUID) -> None:
    """The task's evidence changed: an attempt waiting for a fact looks now.

    A rule closing the work, or anybody writing evidence tied to a check,
    does not leave the attempt asleep until its next timed look.
    """
    await session.execute(
        update(TaskVerification)
        .where(TaskVerification.task_id == task_id, TaskVerification.status == WAITING_EXTERNAL)
        .values(next_check_at=utcnow(), updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


# --- closing ---------------------------------------------------------------------


def _result(
    check: dict[str, Any],
    status: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": check["key"],
        "kind": check["kind"],
        **({"source": check["source"]} if "source" in check else {}),
        "status": status,
        "evidence": evidence or [],
        "reason": reason,
        **({"message": message[:2000]} if message else {}),
        **({"details": details} if details else {}),
    }


def _brief(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Results as the journal carries them: no reason text (ADR-0015)."""
    return [
        {"key": r["key"], "kind": r["kind"], "status": r["status"], "evidence": r["evidence"]}
        for r in results
    ]


async def _pass(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> TaskVerification:
    """Every check passed: the task is done — in this transaction, with its events."""
    now = utcnow()
    _spend(task, row)
    row.status = PASSED
    row.next_check_at = None
    row.finished_at = now
    row.updated_at = now
    await session.flush()
    artifact = await _record_artifact(session, ctx, task, row)
    await mark_task_completed(
        session,
        ctx,
        task,
        payload={"verificationId": str(row.id), "attempt": row.attempt},
    )
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verified",
        payload={"results": _brief(row.results), "artifactId": str(artifact.id)},
    )
    return row


async def _fail(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    outcome: _Outcome,
) -> TaskVerification:
    """The first failed check fails the attempt; the task goes back to work."""
    now = utcnow()
    check = row.checks[min(row.cursor, len(row.checks) - 1)]
    row.results = [
        *row.results,
        _result(
            check, FAILED, evidence=outcome.evidence, reason=outcome.reason, message=outcome.message
        ),
    ]
    _spend(task, row)
    row.status = FAILED
    row.next_check_at = None
    row.finished_at = now
    row.updated_at = now
    await session.flush()
    await _withdraw_request(session, ctx, task, row)

    failures = await _consecutive_failures(session, task.id)
    exhausted = failures >= MAX_CONSECUTIVE_FAILURES
    previous = task.status
    await _return_task(session, task, blocked=exhausted)
    task.updated_at = now
    task.version += 1
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verification_failed",
        payload={
            "results": _brief(row.results),
            "failedCheck": check["key"],
            "reason": outcome.reason,
            "consecutiveFailures": failures,
            "blocked": exhausted,
            "fromStatus": previous,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
        },
    )
    await _tell(session, ctx, task, row, check, outcome, failures, exhausted)
    return row


async def _consecutive_failures(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Failed attempts in a row, newest first, up to the first that did not fail.

    A cancelled attempt says nothing about the work and is passed over.
    """
    statuses = (
        await session.scalars(
            select(TaskVerification.status)
            .where(
                TaskVerification.task_id == task_id,
                TaskVerification.status.in_((PASSED, FAILED)),
            )
            .order_by(TaskVerification.attempt.desc())
        )
    ).all()
    count = 0
    for status in statuses:
        if status != FAILED:
            break
        count += 1
    return count


async def _return_task(session: AsyncSession, task: Task, *, blocked: bool) -> None:
    """Back to the executor (``releaseStatus``), or to a person (``blocked``).

    Only along a declared edge: a lifecycle without one leaves the status
    alone, as a claim release does (SPEC §4.4). The open attempt is closed
    by now, so the task is claimable again either way.
    """
    lifecycle = await lifecycle_of(session, task)
    target: str | None
    if blocked:
        target = next(
            (
                status
                for status in lifecycle.targets_from(task.status)
                if lifecycle.category_of(status) == WorkItemStatusCategory.BLOCKED
            ),
            None,
        )
    else:
        target = lifecycle.release_status
    if target is None or target == task.status or not lifecycle.allows(task.status, target):
        return
    task.status = target
    task.system_status_category = lifecycle.category_of(target)


async def _record_artifact(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> Artifact:
    """What the passed attempt established, on the task, by the completer."""
    artifact = Artifact(
        id=new_uuid(),
        tenant_id=task.tenant_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=None,
        created_by_principal_id=row.authority_principal_id,
        type=VERIFICATION_ARTIFACT,
        name=f"{task.public_id} verification #{row.attempt}",
        uri=None,
        content={
            "verificationId": str(row.id),
            "attempt": row.attempt,
            "trigger": row.trigger,
            "triggerRef": row.trigger_ref,
            "results": row.results,
        },
        supersedes_artifact_id=None,
        metadata_json={"verificationId": str(row.id), "attempt": row.attempt},
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()
    await record_event(
        session,
        tenant_id=task.tenant_id,
        event_type="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "type": artifact.type,
            "name": artifact.name,
            "taskId": str(task.id),
            "runId": None,
            "uri": None,
            "supersedesArtifactId": None,
            "verificationId": str(row.id),
            **artifact_event_fields(artifact),
        },
    )
    return artifact


async def _tell(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    outcome: _Outcome,
    failures: int,
    exhausted: bool,
) -> None:
    """A comment on the task with why it failed, by core's own principal.

    By core, narrowed to ``tasks.write``, like the comment on failed work
    after completion: the typical completer is a runner, and the reason must
    reach whoever takes the task next.
    """
    core_ctx = await _core_context(session, task, ctx, Permission.TASKS_WRITE)
    lines = [
        f"Verification attempt #{row.attempt} failed at check {check['key']!r} "
        f"({check['kind']}): {outcome.reason}"
        + (f": {outcome.message}" if outcome.message else ""),
    ]
    for result in row.results:
        lines.append(f"- {result['key']} ({result['kind']}): {result['status']}")
    skipped = [c["key"] for c in row.checks[len(row.results) :]]
    if skipped:
        lines.append(f"Not run: {', '.join(skipped)}.")
    if exhausted:
        lines.append(
            f"{failures} failed attempts in a row: the task waits for a person "
            f"(status {task.status!r})."
        )
    else:
        lines.append(
            f"The task is back in {task.status!r} for another attempt "
            f"({failures} of {MAX_CONSECUTIVE_FAILURES} failed in a row)."
        )
    try:
        async with session.begin_nested():
            await add_comment(session, core_ctx, task_ref=str(task.id), body="\n".join(lines))
    except DomainError:
        # Still recorded on the attempt and in the journal.
        return


# --- shared ----------------------------------------------------------------------


async def _core_context(
    session: AsyncSession, task: Task, ctx: AuthContext, permission: Permission
) -> AuthContext:
    """Core's own principal, narrowed to the one permission it acts with."""
    core = await ensure_core_principal(session, task.tenant_id)
    return AuthContext(
        tenant_id=task.tenant_id,
        principal_id=core.id,
        principal_kind=core.kind,
        # No credential exists for core; its principal id stands in.
        api_key_id=core.id,
        permissions=frozenset({permission.value}),
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
    )


async def _lock_row(session: AsyncSession, verification_id: uuid.UUID) -> TaskVerification | None:
    row: TaskVerification | None = await session.scalar(
        select(TaskVerification)
        .where(TaskVerification.id == verification_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row


def _authority_context(row: TaskVerification, trace_run_id: str) -> AuthContext:
    """The completer, as the credential snapshot taken at completion says."""
    return _snapshot_context(
        row.authority or {},
        tenant_id=row.tenant_id,
        principal_id=row.authority_principal_id,
        request_id=f"verification:{row.id}",
        correlation_id=row.correlation_id,
        trace_run_id=trace_run_id,
    )


def _snapshot_context(
    authority: dict[str, Any],
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    request_id: str,
    correlation_id: str,
    trace_run_id: str,
) -> AuthContext:
    iam = authority.get("iamPrincipalId")
    return AuthContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        principal_kind=str(authority.get("principalKind") or "human"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=request_id,
        correlation_id=correlation_id,
        causation_id=None,
        trace_run_id=trace_run_id,
        iam_principal_id=uuid.UUID(iam) if iam else None,
    )


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    *,
    event_type: str,
    payload: dict[str, Any],
    session_id: uuid.UUID | None = None,
) -> None:
    await record_event(
        session,
        tenant_id=task.tenant_id,
        event_type=event_type,
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        session_id=session_id,
        request_id=ctx.request_id,
        correlation_id=row.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "taskId": str(task.id),
            "verificationId": str(row.id),
            "attempt": row.attempt,
            "trigger": row.trigger,
            "checks": len(row.checks),
            **payload,
        },
    )
