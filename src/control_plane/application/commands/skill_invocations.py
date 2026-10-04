"""Skill invocation: one durable object, one path for every caller (ADR-0056 §2).

The server decides and stores; it never executes. ``invoke_skill`` checks the
version, the inputs, the caller's rights and the side-effect rule, and leaves a
``pending`` row. An executor (the ``skill`` adapter of the agent daemon, M2.2)
takes it under a lease with a fencing token exactly like a task claim, runs the
implementation and reports ``complete`` or ``fail``. The output is validated
again here: what the executor says is evidence, not the verdict.

Lease expiry is handled lazily by ``claim_skill_invocation`` (so correctness
does not depend on the worker) and eagerly by the worker sweep.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    tenant_resource,
)
from control_plane.application.commands._artifact_content import artifact_event_fields
from control_plane.application.commands._child_ceiling import (
    ceiling_of_run,
    enforce_run_ceiling,
)
from control_plane.application.commands.eligibility import parse_skill_ref
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.skill_outputs import record_task_outputs
from control_plane.application.commands.task_types import task_type_of
from control_plane.application.common import clamp_ttl, new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principal_key_share
from control_plane.application.queries.approval_gates import (
    pending_gate_approvals,
    require_open_gates,
)
from control_plane.application.queries.tool_policy import (
    decide_for_skill,
    resolve_effective_tool_policy,
)
from control_plane.application.visibility import task_condition, task_visible
from control_plane.config import Settings
from control_plane.domain import process_engine
from control_plane.domain.canonical import canonical_bytes
from control_plane.domain.enums import (
    INVOCABLE_SKILL_PROTOCOLS,
    ApprovalStatus,
    Permission,
    RunStatus,
    SessionStatus,
    SkillIdempotency,
    SkillInvocationRequester,
    SkillInvocationStatus,
    SkillProtocol,
    SkillSideEffects,
    SkillStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.project import guard_json_document
from control_plane.domain.skill_contract import schema_errors
from control_plane.domain.tool_discovery import REASON_CHILD_GRANT
from control_plane.domain.work_item import TERMINAL_CATEGORIES
from control_plane.infrastructure.content_store import ContentStore
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    ProcessInstance,
    Run,
    RunChildHandle,
    Session,
    Skill,
    SkillInvocation,
    Task,
)

LIVE_STATUSES = (SkillInvocationStatus.PENDING, SkillInvocationStatus.RUNNING)
MAX_IDEMPOTENCY_KEY = 200
MAX_PAYLOAD_BYTES = 256 * 1024
LEASE_MARGIN_SECONDS = 30
#: A heartbeat keeps a slow attempt alive, but not forever: the lease never
#: reaches past attempt start + timeoutSeconds * this factor + the margin.
LEASE_TIMEOUT_FACTOR = 2
#: How much of an output rejected by the contract is kept as evidence.
MAX_REJECTED_OUTPUT_BYTES = 64 * 1024
_SWEEP_BATCH = 100
#: How many candidates one ``:claim`` looks at before answering "nothing".
#: Candidates whose basis is gone are cancelled on the way, so the scan also
#: drains stale calls in bounded steps.
_CLAIM_SCAN = 20
#: Who started a cancellation (``error.details.initiator`` and the
#: ``skill.invocation_cancelled`` payload): a principal's request, or core on
#: its own (a basis found gone, an outcome that stopped waiting).
CANCELLED_BY_PRINCIPAL = "principal"
CANCELLED_BY_SYSTEM = "system"


@dataclass(frozen=True)
class InvocationResult:
    invocation: SkillInvocation
    skill: Skill
    created: bool


# --- resolution ---------------------------------------------------------------


async def resolve_skill_ref(session: AsyncSession, ctx: AuthContext, ref: str) -> Skill:
    """``name@version`` pins that version; ``name`` resolves as in ADR-0021.

    For a bare name: the newest ``active`` version, falling back to the newest
    ``deprecated`` one. A pinned version is returned whatever its status — the
    caller decides what a disabled version means (``skill_not_invocable``).
    """
    try:
        skill_id = uuid.UUID(ref)
    except ValueError:
        skill_id = None
    if skill_id is not None:
        skill = await session.scalar(
            select(Skill).where(Skill.id == skill_id, Skill.tenant_id == ctx.tenant_id)
        )
    else:
        try:
            name, version = parse_skill_ref(ref)
        except ValidationError as exc:
            raise NotFoundError("Skill not found", details={"skill": ref}) from exc
        base = select(Skill).where(Skill.tenant_id == ctx.tenant_id, Skill.name == name)
        if version is not None:
            skill = await session.scalar(base.where(Skill.version == version))
        else:
            skill = None
            for status in (SkillStatus.ACTIVE, SkillStatus.DEPRECATED):
                skill = await session.scalar(
                    base.where(Skill.status == status)
                    .order_by(Skill.created_at.desc(), Skill.id.desc())
                    .limit(1)
                )
                if skill is not None:
                    break
    if skill is None:
        raise NotFoundError("Skill not found", details={"skill": ref})
    return skill


def _require_invocable(skill: Skill) -> dict[str, Any]:
    reason: str | None = None
    if skill.status == SkillStatus.DISABLED:
        reason = "disabled"
    elif skill.contract is None:
        reason = "no_contract"
    elif skill.protocol not in INVOCABLE_SKILL_PROTOCOLS:
        reason = "protocol_not_invocable"
    if reason is not None:
        raise ConflictError(
            "skill_not_invocable",
            "This skill version cannot be invoked by the core",
            details={
                "skill": f"{skill.name}@{skill.version}",
                "skillId": str(skill.id),
                "reason": reason,
                "protocol": skill.protocol,
                "status": skill.status,
            },
        )
    assert skill.contract is not None
    return skill.contract


# --- invoke -------------------------------------------------------------------


def _task_resource(ctx: AuthContext, task: Task | None) -> ResourceRef:
    """Where a task-bound call is authorized: the task's workspace.

    Runner bindings are often workspace-scoped, so a tenant-level question
    would deny them what they hold — or, in policy mode, grant a tenant-wide
    right a narrower binding does not carry.
    """
    if task is not None and task.workspace_id is not None:
        return ResourceRef("workspace", str(task.workspace_id))
    return tenant_resource(ctx)


async def _check_required_permissions(
    session: AsyncSession,
    ctx: AuthContext,
    contract: dict[str, Any],
    *,
    task: Task | None,
    run: Run | None,
) -> None:
    """The caller must itself hold every right the skill declares (§4).

    Held means both: granted to the credential on the task's workspace, and —
    for a call made under a child run — inside that run's ceiling (HRS-7).
    A child holding a stronger API key must not borrow it through a skill.
    """
    resource = _task_resource(ctx, task)
    missing: list[str] = []
    for name in contract.get("requiredPermissions", []):
        try:
            await authorize(ctx, Permission(name), resource=resource)
        except AuthorizationError:
            missing.append(name)
    if missing:
        raise AuthorizationError(
            "The caller lacks permissions this skill requires",
            code="skill_permission_denied",
            details={"missing": missing, "resource": resource.key},
        )
    if run is not None:
        for name in contract.get("requiredPermissions", []):
            await enforce_run_ceiling(session, ctx, run=run, permission=Permission(name))


async def _check_run_tool_policy(
    session: AsyncSession, ctx: AuthContext, skill: Skill, run: Run
) -> None:
    """A call under a run passes the same gate as a run action (HRS-3).

    The effective tool policy — assignment, project governance and the skill
    grant of a child handle — is decided by ``decide_visibility`` alone; the
    harness capability is the client's own statement and is not enforced.
    """
    policy = await resolve_effective_tool_policy(session, ctx, run_id=run.id)
    decision = await decide_for_skill(session, ctx, skill, policy)
    if decision.authorized:
        return
    ref = f"{skill.name}@{skill.version}"
    if decision.reason == REASON_CHILD_GRANT:
        raise AuthorizationError(
            "Skill exceeds the skill grant of this child run",
            code="child_grant_exceeded",
            details={"skill": ref, "runId": str(run.id)},
        )
    raise AuthorizationError(
        "Skill is not authorized for this run",
        code="tool_not_authorized",
        details={"skill": ref, "runId": str(run.id), "reason": decision.reason},
    )


async def _running_bounded_run(session: AsyncSession, ctx: AuthContext) -> uuid.UUID | None:
    """A running run of the caller bounded by a child handle, if any."""
    run_id: uuid.UUID | None = await session.scalar(
        select(Run.id)
        .join(RunChildHandle, RunChildHandle.child_run_id == Run.id)
        .where(
            Run.tenant_id == ctx.tenant_id,
            Run.principal_id == ctx.principal_id,
            Run.status == RunStatus.RUNNING,
            RunChildHandle.tenant_id == ctx.tenant_id,
        )
        .limit(1)
    )
    return run_id


async def _require_bound_run(
    session: AsyncSession, ctx: AuthContext, *, named: Run | None = None
) -> None:
    """A principal executing a child run cannot step outside its ceiling.

    The ceiling lives on the run, and a call without ``runId`` names no run.
    If the caller currently runs a run bounded by a child handle, such a call
    would carry the full rights of the API key — the escalation HRS-7 forbids.
    So it has to name the run it acts for. For the same reason a named run
    must itself be bounded then: naming one's own root run would lift the
    ceiling just the same.
    """
    if named is not None and await ceiling_of_run(session, ctx.tenant_id, named.id) is not None:
        return
    bounded = await _running_bounded_run(session, ctx)
    if bounded is not None:
        raise AuthorizationError(
            "The caller executes a bounded child run and must name it as runId",
            code="run_id_required",
            details={"runId": str(bounded)},
        )


async def _execution_basis(
    session: AsyncSession, skill: Skill, *, task: Task | None, run: Run | None
) -> dict[str, Any] | None:
    """The run executes a task whose type declares THIS skill version (§3).

    Only a call made under that run qualifies: the run is the Work, and its
    claim is what "the task went through its own lifecycle" means. One such
    call per run — ``uq_skill_invocations_run_execution``.
    """
    if run is None or task is None:
        return None
    execution = (await task_type_of(session, task)).execution
    if (
        execution is None
        or execution.get("skill") != skill.name
        or execution.get("version") != skill.version
    ):
        return None
    return {
        "kind": "execution",
        "taskTypeId": str(task.type_id),
        "taskId": str(task.id),
        "runId": str(run.id),
    }


async def _require_open_gates(session: AsyncSession, ctx: AuthContext, task: Task) -> None:
    """An execution basis holds only while no gate approval holds the task."""
    await require_open_gates(session, ctx, task.id)


def _require_open_task(task: Task) -> None:
    if task.system_status_category in TERMINAL_CATEGORIES:
        raise ConflictError(
            "task_terminal",
            "An external_write skill cannot act for a closed Work item",
            details={"taskId": str(task.id), "status": task.status},
        )


async def _side_effect_basis(
    session: AsyncSession,
    ctx: AuthContext,
    skill: Skill,
    *,
    task: Task | None,
    run: Run | None,
    approval_id: uuid.UUID | None,
    process: tuple[ProcessInstance, str] | None = None,
) -> dict[str, Any] | None:
    """ADR-0056 §4: ``external_write`` needs a basis, otherwise 403.

    Three bases are recognized. A gate approval on the same, still open Work
    item that has been decided positively: single-use per skill version — one
    approval, one external action — which the partial unique index
    ``uq_skill_invocations_skill_approval`` guarantees under concurrency. And
    the task type's ``execution`` (M2.2): the call is made under the running
    run of a task whose type declares this very skill version, the task is
    open and no gate approval is pending on it. That basis is recorded for
    every side-effect level — it also makes the call the one call of its run.
    A delegation with the skill in scope is not available (delegations carry
    no scope yet); see the ADR-0056 amendment. And the process instance whose
    step makes the call (amendment TASK-001197): the published version of
    the process names this very skill version in its step, as a task type
    names its ``execution``; only the engine passes ``process`` — the instance
    and the activity of the step — no route does. The basis holds while that
    activity is open, not while the instance lives (review of TASK-001197).
    """
    execution = await _execution_basis(session, skill, task=task, run=run)
    if skill.side_effects != SkillSideEffects.EXTERNAL_WRITE:
        return execution
    if approval_id is None and execution is not None:
        assert task is not None
        _require_open_task(task)
        await _require_open_gates(session, ctx, task)
        return execution
    if approval_id is None and task is None and process is not None:
        instance, activity_id = process
        return {
            "kind": "process",
            "instanceId": str(instance.id),
            "activityId": activity_id,
            "definitionKey": instance.definition_key,
            "definitionVersion": instance.definition_version,
        }
    denied = AuthorizationError(
        "external_write skill needs an approved gate on this Work item",
        code="skill_side_effect_not_authorized",
        details={
            "skill": f"{skill.name}@{skill.version}",
            "sideEffects": skill.side_effects,
            "approvalId": str(approval_id) if approval_id else None,
        },
    )
    if approval_id is None or task is None:
        raise denied
    _require_open_task(task)
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
    )
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or not approval.gate
        or approval.task_id != task.id
    ):
        raise denied
    await _require_unused_approval(session, skill, approval.id)
    return {
        "kind": "approval",
        "approvalId": str(approval.id),
        "decidedBy": str(approval.decision_by_principal_id)
        if approval.decision_by_principal_id
        else None,
    }


async def _require_unused_approval(
    session: AsyncSession, skill: Skill, approval_id: uuid.UUID
) -> None:
    used = await session.scalar(
        select(SkillInvocation.id).where(
            SkillInvocation.skill_id == skill.id,
            SkillInvocation.authorization_basis["kind"].astext == "approval",
            SkillInvocation.authorization_basis["approvalId"].astext == str(approval_id),
        )
    )
    if used is not None:
        _approval_used(skill, approval_id)


def _approval_used(skill: Skill, approval_id: uuid.UUID) -> NoReturn:
    raise ConflictError(
        "approval_already_used",
        "This approval has already authorized a call of this skill version",
        details={"skill": f"{skill.name}@{skill.version}", "approvalId": str(approval_id)},
    )


async def invoke_skill(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    skill_ref: str,
    inputs: dict[str, Any],
    idempotency_key: str | None = None,
    task_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
    requested_by: tuple[SkillInvocationRequester, str] | None = None,
    process: tuple[ProcessInstance, str] | None = None,
) -> InvocationResult:
    """Queue a call; ``requested_by`` names a requester other than the caller.

    Core's own requesters (the verification stage, CP-ADR-0067) call with the
    authority of a principal but on behalf of something else: the call is
    recorded as requested by that ``(kind, ref)``, checked exactly the same.
    ``process`` — the instance whose step makes the call and the id of the
    step's activity: the basis of an ``external_write`` (:func:`_side_effect_basis`).
    """
    await authorize(ctx, Permission.SKILLS_INVOKE)
    skill = await resolve_skill_ref(session, ctx, skill_ref)

    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY:
            raise ValidationError(
                "invalid_idempotency_key",
                f"idempotencyKey must be 1..{MAX_IDEMPOTENCY_KEY} characters",
            )

    # A repeat returns the call it repeats — before any other check, so that a
    # retry after a lost response never becomes a second external action, and
    # a version disabled in the meantime still answers for the call it took.
    if idempotency_key is not None:
        existing = await _existing_by_key(session, skill, idempotency_key)
        if existing is not None:
            return InvocationResult(_same_call(ctx, existing, inputs), skill, created=False)

    contract = _require_invocable(skill)
    if contract["idempotency"] == SkillIdempotency.REQUIRED and idempotency_key is None:
        raise BadRequestError(
            "idempotency_key_required",
            "This skill requires an idempotencyKey from the caller",
            details={"skill": f"{skill.name}@{skill.version}"},
        )

    guard_json_document(inputs, label="inputs", max_bytes=MAX_PAYLOAD_BYTES)
    errors = schema_errors(contract["inputs"], inputs)
    if errors:
        raise BadRequestError(
            "invalid_skill_inputs",
            "inputs do not match the skill's input schema",
            details={"skill": f"{skill.name}@{skill.version}", "errors": errors},
        )
    # preconditions: the contract accepts only an empty list until M1.3, so
    # there is nothing to evaluate yet (skill_contract._conditions).

    task: Task | None = None
    run: Run | None = None
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        if run is None or not await task_visible(session, ctx, run.task_id):
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        if run.principal_id != ctx.principal_id:
            raise AuthorizationError(
                "Run belongs to another principal",
                code="run_owner_mismatch",
                details={"runId": str(run_id)},
            )
        # Only a live run carries authority: a finished one is no longer
        # bounded by anything the caller still executes.
        if run.status != RunStatus.RUNNING:
            raise ConflictError(
                "run_not_active",
                "Run is not running",
                details={"runId": str(run.id), "status": run.status},
            )
        await _require_bound_run(session, ctx, named=run)
        await enforce_run_ceiling(session, ctx, run=run, permission=Permission.SKILLS_INVOKE)
    else:
        await _require_bound_run(session, ctx)
    if task_ref is not None:
        task = await resolve_task(session, ctx, task_ref)
        if run is not None and run.task_id != task.id:
            raise ValidationError(
                "invocation_mismatch",
                "run does not belong to the given task",
                details={"runId": str(run.id), "taskId": str(task.id)},
            )
    elif run is not None:
        task = await session.get(Task, run.task_id)
    if task is not None:
        # The call writes a skill_result into the task and may cite its gate:
        # that is a write to the Work item, authorized where the item lives.
        await authorize(ctx, Permission.TASKS_WRITE, resource=_task_resource(ctx, task))
        if run is not None:
            await enforce_run_ceiling(session, ctx, run=run, permission=Permission.TASKS_WRITE)

    await _check_required_permissions(session, ctx, contract, task=task, run=run)
    if run is not None:
        await _check_run_tool_policy(session, ctx, skill, run)

    basis = await _side_effect_basis(
        session, ctx, skill, task=task, run=run, approval_id=approval_id, process=process
    )

    if run is not None:
        requester_kind, requester_ref = SkillInvocationRequester.RUN, str(run.id)
    elif requested_by is not None:
        requester_kind, requester_ref = requested_by
    else:
        requester_kind, requester_ref = SkillInvocationRequester.PRINCIPAL, str(ctx.principal_id)

    now = utcnow()
    invocation_id = new_uuid()
    values: dict[str, Any] = {
        "id": invocation_id,
        "tenant_id": ctx.tenant_id,
        "skill_id": skill.id,
        "inputs": inputs,
        "requested_by_kind": requester_kind,
        "requested_by_ref": requester_ref,
        "authority_principal_id": ctx.principal_id,
        "authorization_basis": basis,
        "idempotency_key": idempotency_key,
        "status": SkillInvocationStatus.PENDING,
        "attempt": 0,
        "max_attempts": contract["retryPolicy"]["maxAttempts"],
        "available_at": now,
        "fencing_token": 0,
        "task_id": task.id if task is not None else None,
        "run_id": run.id if run is not None else None,
        "created_at": now,
        "updated_at": now,
    }
    # ON CONFLICT without a target covers every unique rule: two concurrent
    # first calls with one key produce one row, two concurrent calls citing
    # one approval spend it once, and a run makes one execution call.
    inserted = await session.scalar(
        insert(SkillInvocation)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(SkillInvocation.id)
    )
    if inserted is None:
        if idempotency_key is not None:
            existing = await _existing_by_key(session, skill, idempotency_key)
            if existing is not None:
                return InvocationResult(_same_call(ctx, existing, inputs), skill, created=False)
        assert basis is not None
        if basis["kind"] == "execution":
            raise ConflictError(
                "execution_already_invoked",
                "This run has already made its execution call",
                details={"runId": basis["runId"], "skill": f"{skill.name}@{skill.version}"},
            )
        assert approval_id is not None
        _approval_used(skill, approval_id)

    invocation = await session.get(SkillInvocation, invocation_id)
    assert invocation is not None
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.invocation_requested",
        entity_type="skill_invocation",
        entity_id=invocation.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "skillId": str(skill.id),
            "skill": skill.name,
            "version": skill.version,
            "sideEffects": skill.side_effects,
            "riskLevel": skill.risk_level,
            "requestedBy": {
                "kind": invocation.requested_by_kind,
                "ref": invocation.requested_by_ref,
            },
            "taskId": str(invocation.task_id) if invocation.task_id else None,
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "authorizationBasis": basis,
        },
    )
    return InvocationResult(invocation, skill, created=True)


async def _existing_by_key(
    session: AsyncSession, skill: Skill, idempotency_key: str
) -> SkillInvocation | None:
    existing: SkillInvocation | None = await session.scalar(
        select(SkillInvocation).where(
            SkillInvocation.skill_id == skill.id,
            SkillInvocation.idempotency_key == idempotency_key,
        )
    )
    return existing


def _same_call(
    ctx: AuthContext, existing: SkillInvocation, inputs: dict[str, Any]
) -> SkillInvocation:
    """A key names ONE call: other inputs or another authority is a reuse."""
    if existing.authority_principal_id != ctx.principal_id or existing.inputs != inputs:
        raise ConflictError(
            "idempotency_key_reuse",
            "This idempotencyKey already names a different invocation",
            details={"invocationId": str(existing.id)}
            if existing.authority_principal_id == ctx.principal_id
            else {},
        )
    return existing


# --- read ---------------------------------------------------------------------


async def _invocation_visible(
    session: AsyncSession, ctx: AuthContext, invocation: SkillInvocation
) -> bool:
    return invocation.task_id is None or await task_visible(session, ctx, invocation.task_id)


async def get_skill_invocation(
    session: AsyncSession, ctx: AuthContext, invocation_id: uuid.UUID
) -> tuple[SkillInvocation, Skill]:
    """Visible to its authority, its executor and any holder of skills.execute.

    Anyone else gets the same 404 as for a missing id: an invocation's inputs
    and output belong to whoever acted, not to the whole tenant.
    """
    await authorize(ctx, Permission.SKILLS_INVOKE, Permission.SKILLS_EXECUTE)
    row = (
        await session.execute(
            select(SkillInvocation, Skill)
            .join(Skill, Skill.id == SkillInvocation.skill_id)
            .where(
                SkillInvocation.id == invocation_id,
                SkillInvocation.tenant_id == ctx.tenant_id,
            )
        )
    ).first()
    if row is None:
        raise NotFoundError(
            "Skill invocation not found", details={"invocationId": str(invocation_id)}
        )
    invocation, skill = row
    # A call on invisible work is a missing call; one without work is an
    # object of the tenant (CP-ADR-0082 §4).
    if not (
        ctx.principal_id in (invocation.authority_principal_id, invocation.executor_principal_id)
        or ctx.has(Permission.SKILLS_EXECUTE)
    ) or not await _invocation_visible(session, ctx, invocation):
        raise NotFoundError(
            "Skill invocation not found", details={"invocationId": str(invocation_id)}
        )
    return invocation, skill


# --- executor: claim ----------------------------------------------------------


async def _require_executor_session(
    session: AsyncSession, ctx: AuthContext, session_id: uuid.UUID
) -> Session:
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
    if work_session.status != SessionStatus.ACTIVE or work_session.expires_at <= utcnow():
        raise ConflictError(
            "session_not_active",
            "Session is not active",
            details={"sessionId": str(session_id), "status": work_session.status},
        )
    return work_session


async def expire_leases(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str | None,
) -> int:
    """Return running rows with a dead lease to ``pending`` (or fail them).

    An expired lease consumed its attempt: the executor may have acted before
    it died. With attempts left the row goes back to the queue, otherwise it
    fails with ``lease_expired`` (retryable — a caller may try again with a
    new call).
    """
    now = utcnow()
    stmt = (
        select(SkillInvocation)
        .where(
            SkillInvocation.status == SkillInvocationStatus.RUNNING,
            SkillInvocation.lease_expires_at <= now,
        )
        .order_by(SkillInvocation.lease_expires_at)
        .limit(_SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        stmt = stmt.where(SkillInvocation.tenant_id == tenant_id)
    rows = (await session.scalars(stmt)).all()
    for invocation in rows:
        error = {
            "code": "lease_expired",
            "retryable": True,
            "message": "The executor's lease expired before a result was reported",
        }
        await _retry_or_fail(
            session,
            invocation,
            error=error,
            actor_id=actor_id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_run_id=trace_run_id,
        )
    return len(rows)


async def _claim_obstacle(
    session: AsyncSession, invocation: SkillInvocation, skill: Skill
) -> tuple[str, str] | None:
    """Does what allowed this call at ``:invoke`` still hold at ``:claim``?

    A pending call may wait — for backoff, for a free executor — while the
    world moves: the version gets disabled, the Work item closes, the run that
    made the execution call ends, the approval is withdrawn. Handing it out
    then would perform an external action on a basis that no longer exists
    (review of M2.1). Returns ``("cancel", reason)`` when the basis is gone
    for good, ``("hold", reason)`` when it is only suspended (a pending gate
    approval), ``None`` when the call may run.

    A lost basis is named before a disabled version: both cancel, but only
    the former tells a waiting approval outcome that nothing is to be
    reported (CP-ADR-0061 §10). A disabled version still cancels a call whose
    basis is merely on hold.
    """
    obstacle = await _basis_obstacle(session, invocation, skill)
    if obstacle is not None and obstacle[0] == "cancel":
        return obstacle
    if skill.status == SkillStatus.DISABLED:
        return "cancel", "skill_disabled"
    return obstacle


async def _basis_obstacle(
    session: AsyncSession, invocation: SkillInvocation, skill: Skill, *, fresh: bool = False
) -> tuple[str, str] | None:
    """``_claim_obstacle`` minus the state of the version: the basis alone.

    ``fresh`` reads the task, approval and run from the database rather than
    from the session, for a caller that checks twice in one transaction.
    """
    basis = invocation.authorization_basis
    if basis is None:
        return None
    task = (
        await session.get(Task, invocation.task_id, populate_existing=fresh)
        if invocation.task_id
        else None
    )
    if task is not None and task.system_status_category in TERMINAL_CATEGORIES:
        # A closed Work item is not a basis for an external action.
        return "cancel", "task_terminal"
    if basis.get("kind") == "approval":
        approval = await session.get(
            Approval, uuid.UUID(basis["approvalId"]), populate_existing=fresh
        )
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            return "cancel", "approval_withdrawn"
    elif basis.get("kind") == "execution":
        run = (
            await session.get(Run, invocation.run_id, populate_existing=fresh)
            if invocation.run_id
            else None
        )
        if run is None or run.status != RunStatus.RUNNING:
            return "cancel", "run_not_active"
        if (
            task is not None
            and skill.side_effects == SkillSideEffects.EXTERNAL_WRITE
            and await pending_gate_approvals(session, invocation.tenant_id, task.id)
        ):
            return "hold", "approval_required"
    elif basis.get("kind") == "process":
        # The basis is the step, not the instance: once its activity closes (a
        # timeout, a retry, a failed instance) the call is no longer awaited and
        # a retry has queued its own. A suspended instance holds its calls.
        instance = await session.get(
            ProcessInstance, uuid.UUID(basis["instanceId"]), populate_existing=fresh
        )
        if instance is None or instance.status == process_engine.CANCELLED:
            return "cancel", "process_cancelled"
        activities = (instance.state or {}).get("activities") or {}
        if basis.get("activityId") not in activities:
            return "cancel", "process_step_closed"
        if instance.status == process_engine.SUSPENDED:
            return "hold", "process_suspended"
    return None


async def lost_basis(
    session: AsyncSession, invocation: SkillInvocation, *, reasons: frozenset[str]
) -> str | None:
    """Which of ``reasons`` took the basis of this call away, if any.

    The version is not looked at: a call whose basis is gone is
    ``basis_revoked`` for that reason whatever became of the version, so a
    disabled skill does not hide a closed Work item. A read only — the caller
    decides whether to lock the call and revoke it (``revoke_lost_basis``);
    what it reads is read afresh, so a check under that lock is a real one.
    """
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    obstacle = await _basis_obstacle(session, invocation, skill, fresh=True)
    if obstacle is None or obstacle[0] != "cancel" or obstacle[1] not in reasons:
        return None
    return obstacle[1]


async def revoke_lost_basis(
    session: AsyncSession,
    ctx: AuthContext,
    invocation: SkillInvocation,
    *,
    reasons: frozenset[str],
) -> str | None:
    """Cancel a ``pending`` call whose basis is gone, without waiting for a claim.

    The same basis check as at ``:claim`` (``_claim_obstacle``, but the basis
    before the version — ``lost_basis``) and the same cancellation
    (``basis_revoked``, the reason in ``message``), for a caller that waits on
    the call and must not wait for an executor that may never come to find
    out. Only an obstacle in ``reasons`` counts. The caller holds the row lock
    of the pending call. The cancellation is the system's (``initiator``),
    ``ctx`` is only whose authority it is recorded under. Returns the reason,
    or ``None`` when the call was left alone.
    """
    reason = await lost_basis(session, invocation, reasons=reasons)
    if reason is None:
        return None
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    await _finish_cancelled(
        session,
        invocation,
        skill,
        reason=reason,
        code="basis_revoked",
        initiator=CANCELLED_BY_SYSTEM,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
    )
    return reason


async def withdraw_principal_invocations(
    session: AsyncSession, ctx: AuthContext, *, principal_id: uuid.UUID, reason: str
) -> int:
    """Stop the live calls of a principal that leaves (CP-ADR-0077).

    A call made on its authority loses its basis: cancelled (``basis_revoked``)
    whether it waits or runs, so no executor performs it later. A lease it holds
    as executor on someone else's call goes back the way an expired one does
    (``_retry_or_fail``): the attempt is spent, the call retries if it may.
    The caller already holds the task, claim and run locks it needs; call rows
    are locked last. Returns how many calls were touched.
    """
    rows = (
        await session.scalars(
            select(SkillInvocation)
            .where(
                SkillInvocation.tenant_id == ctx.tenant_id,
                SkillInvocation.status.in_(LIVE_STATUSES),
                or_(
                    SkillInvocation.authority_principal_id == principal_id,
                    and_(
                        SkillInvocation.status == SkillInvocationStatus.RUNNING,
                        SkillInvocation.executor_principal_id == principal_id,
                    ),
                ),
            )
            .order_by(SkillInvocation.id)
            .with_for_update()
        )
    ).all()
    for invocation in rows:
        skill = await session.get(Skill, invocation.skill_id)
        assert skill is not None
        if invocation.authority_principal_id == principal_id:
            await _finish_cancelled(
                session,
                invocation,
                skill,
                reason=reason,
                code="basis_revoked",
                initiator=CANCELLED_BY_SYSTEM,
                actor_id=ctx.principal_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                trace_run_id=ctx.trace_run_id,
            )
        else:
            await _retry_or_fail(
                session,
                invocation,
                error={
                    "code": reason,
                    "retryable": True,
                    "message": "The executor holding the lease was disabled",
                },
                actor_id=ctx.principal_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                trace_run_id=ctx.trace_run_id,
                skill=skill,
            )
    return len(rows)


_ORIGIN_RE = re.compile(r"^https?://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:[0-9]{1,5})?$")
_STDIO_RE = re.compile(r"^stdio:[A-Za-z0-9_.-]{1,100}$")


def _declared_origins(items: list[str], *, field: str, stdio: bool = False) -> list[str]:
    """Executor-declared ``scheme://host[:port]`` (and ``stdio:<name>`` for mcp)."""
    declared: list[str] = []
    for item in items:
        if stdio and _STDIO_RE.match(item):
            declared.append(item)
            continue
        origin = item.lower().rstrip("/")
        if _ORIGIN_RE.match(origin) is None:
            raise ValidationError(
                "invalid_executor_endpoint",
                "Executor endpoints are origins: scheme://host[:port]"
                + (" or stdio:<name>" if stdio else ""),
                details={"field": field, "value": item[:200]},
            )
        declared.append(origin)
    return declared


def _remote_filter(
    protocol: SkillProtocol, endpoints: list[str], audiences: list[str]
) -> ColumnElement[bool]:
    """Calls of ``protocol`` whose endpoint and token audience the executor admits.

    An origin admits the endpoint equal to it or under it (``origin/…``);
    ``stdio:<name>`` only itself. The executor checks the parsed URL again
    before it connects — this filter keeps what it would refuse off its queue.
    """
    implementation = Skill.contract["implementation"]
    endpoint = implementation["endpoint"].astext
    audience = implementation["auth"]["audience"].astext
    reaches: list[ColumnElement[bool]] = []
    for item in endpoints:
        if item.startswith("stdio:"):
            reaches.append(endpoint == item)
        else:
            lowered = func.lower(endpoint)
            reaches.append(lowered == item)
            reaches.append(lowered.startswith(item + "/", autoescape=True))
    audience_ok = or_(audience.is_(None), audience.in_(audiences) if audiences else false())
    return and_(Skill.protocol == protocol, or_(*reaches), audience_ok)


async def claim_skill_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    protocols: list[str],
    local_entrypoints: list[str],
    http_origins: list[str] | None = None,
    mcp_endpoints: list[str] | None = None,
    audiences: list[str] | None = None,
    session_id: uuid.UUID | None = None,
    lease_seconds: int | None = None,
    invocation_id: uuid.UUID | None = None,
) -> tuple[SkillInvocation, Skill] | None:
    """Hand the oldest executable ``pending`` call to this executor.

    The executor declares what it can run: protocols, for ``local`` the
    exact entrypoints installed next to it (ADR-0056 §5), for ``http`` and
    ``mcp`` the origins it may reach and the token audiences it issues
    (amendment M2.2, D). It never receives a call it cannot execute; a remote
    protocol declared without endpoints receives nothing. ``FOR UPDATE SKIP
    LOCKED`` lets several executors poll one queue without blocking each
    other. ``invocation_id`` narrows the queue to one call — the executor of
    an execution-typed task takes the call its own run made.

    Every candidate's basis is checked again under its row lock
    (``_claim_obstacle``): a call whose basis is gone is cancelled rather than
    executed, one whose basis is suspended stays pending.
    """
    await authorize(ctx, Permission.SKILLS_EXECUTE)
    unknown = sorted(set(protocols) - set(INVOCABLE_SKILL_PROTOCOLS))
    if unknown:
        raise ValidationError(
            "invalid_protocol",
            "Executors can declare only http, local and mcp",
            details={"unknown": unknown},
        )
    work_session = await _require_executor_session(session, ctx, session_id) if session_id else None

    await expire_leases(
        session,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
    )

    http_declared = _declared_origins(http_origins or [], field="httpOrigins")
    mcp_declared = _declared_origins(mcp_endpoints or [], field="mcpEndpoints", stdio=True)
    protocol_filter: list[ColumnElement[bool]] = []
    if SkillProtocol.LOCAL in protocols and local_entrypoints:
        protocol_filter.append(
            (Skill.protocol == SkillProtocol.LOCAL)
            & Skill.contract["implementation"]["entrypoint"].astext.in_(local_entrypoints)
        )
    if SkillProtocol.HTTP in protocols and http_declared:
        protocol_filter.append(_remote_filter(SkillProtocol.HTTP, http_declared, audiences or []))
    if SkillProtocol.MCP in protocols and mcp_declared:
        protocol_filter.append(_remote_filter(SkillProtocol.MCP, mcp_declared, audiences or []))
    if not protocol_filter:
        return None

    now = utcnow()
    held: list[uuid.UUID] = []
    invocation: SkillInvocation | None = None
    skill: Skill | None = None
    for _ in range(_CLAIM_SCAN):
        stmt = (
            select(SkillInvocation)
            .join(Skill, Skill.id == SkillInvocation.skill_id)
            .where(
                SkillInvocation.tenant_id == ctx.tenant_id,
                SkillInvocation.status == SkillInvocationStatus.PENDING,
                SkillInvocation.available_at <= now,
                or_(*protocol_filter),
                # An executor narrowed to its workspaces takes no call on
                # work outside them (CP-ADR-0082 §4).
                task_condition(ctx, SkillInvocation.task_id, tenant_level=True),
            )
            .order_by(SkillInvocation.available_at, SkillInvocation.created_at, SkillInvocation.id)
            .limit(1)
            .with_for_update(of=SkillInvocation, skip_locked=True)
        )
        if invocation_id is not None:
            stmt = stmt.where(SkillInvocation.id == invocation_id)
        if held:
            stmt = stmt.where(SkillInvocation.id.notin_(held))
        candidate = await session.scalar(stmt)
        if candidate is None:
            return None
        candidate_skill = await session.get(Skill, candidate.skill_id)
        assert candidate_skill is not None
        obstacle = await _claim_obstacle(session, candidate, candidate_skill)
        if obstacle is None:
            invocation, skill = candidate, candidate_skill
            break
        verdict, reason = obstacle
        if verdict == "cancel":
            await _finish_cancelled(
                session,
                candidate,
                candidate_skill,
                reason=reason,
                code="basis_revoked",
                initiator=CANCELLED_BY_SYSTEM,
                actor_id=ctx.principal_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                trace_run_id=ctx.trace_run_id,
            )
        else:
            held.append(candidate.id)
    if invocation is None or skill is None:
        return None
    assert skill.contract is not None

    default_lease = skill.contract["timeoutSeconds"] + LEASE_MARGIN_SECONDS
    ttl = clamp_ttl(
        lease_seconds,
        default=min(
            max(default_lease, settings.claim_ttl_min_seconds), settings.claim_ttl_max_seconds
        ),
        minimum=settings.claim_ttl_min_seconds,
        maximum=settings.claim_ttl_max_seconds,
    )
    invocation.status = SkillInvocationStatus.RUNNING
    invocation.attempt += 1
    invocation.fencing_token += 1
    invocation.executor_principal_id = ctx.principal_id
    invocation.executor_session_id = work_session.id if work_session else None
    invocation.attempt_started_at = now
    invocation.lease_expires_at = min(
        now + timedelta(seconds=ttl), _attempt_deadline(invocation, skill)
    )
    invocation.heartbeat_at = now
    invocation.started_at = invocation.started_at or now
    invocation.updated_at = now

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.invocation_claimed",
        entity_type="skill_invocation",
        entity_id=invocation.id,
        actor_id=ctx.principal_id,
        session_id=work_session.id if work_session else None,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "skillId": str(skill.id),
            "attempt": invocation.attempt,
            "fencingToken": invocation.fencing_token,
            "leaseExpiresAt": invocation.lease_expires_at.isoformat(),
        },
        deliver=False,
    )
    return invocation, skill


# --- executor: heartbeat / complete / fail ------------------------------------


def _attempt_deadline(invocation: SkillInvocation, skill: Skill) -> datetime:
    """The furthest a lease of the current attempt may reach.

    A row claimed before ``attempt_started_at`` existed falls back to the start
    of its first attempt (the migration backfills the same value).
    """
    assert skill.contract is not None
    started = invocation.attempt_started_at or invocation.started_at or invocation.updated_at
    return started + timedelta(
        seconds=skill.contract["timeoutSeconds"] * LEASE_TIMEOUT_FACTOR + LEASE_MARGIN_SECONDS
    )


async def _locked_own_lease(
    session: AsyncSession,
    ctx: AuthContext,
    invocation_id: uuid.UUID,
    fencing_token: int,
    session_id: uuid.UUID | None,
    *,
    lock_authority: bool = False,
) -> tuple[SkillInvocation, Skill]:
    """Lock the row and prove the caller still holds the current lease.

    Any mismatch — another executor, an older fencing token, a lease already
    expired, a finished call, or another executor session than the one that
    claimed — is ``409 stale_invocation_lease``: the caller must stop, whatever
    it computed is no longer authoritative. A lease taken under a session is
    held by that session, not by every process sharing the API key.

    Lock order: the executor session (``FOR SHARE``) before the call row, as in
    ``claim_skill_invocation``. ``principals/{id}:disable`` locks sessions
    ``FOR UPDATE`` and call rows last (CP-ADR-0077 §3); taking the call row
    first here would deadlock a heartbeat against the disable of its executor.

    ``lock_authority`` (``:complete``) puts the call's authority principal
    (``FOR KEY SHARE``) in front of both: the result artifact it inserts is
    authored by that principal and points at the call's task and run, so its
    foreign-key checks would otherwise take those rows last, after the call
    row, while ``:disable`` of the authority holds them and waits for the call.
    ``authority_principal_id`` never changes, so it is read without a lock.
    """
    await authorize(ctx, Permission.SKILLS_EXECUTE)
    if lock_authority:
        authority_id = await session.scalar(
            select(SkillInvocation.authority_principal_id).where(
                SkillInvocation.id == invocation_id,
                SkillInvocation.tenant_id == ctx.tenant_id,
            )
        )
        if authority_id is not None:
            await lock_principal_key_share(session, ctx.tenant_id, authority_id)
    if session_id is not None:
        # Only the lock, and only on the caller's own session: whether it may
        # hold this lease is checked below, after the lease itself, so a stale
        # lease stays 409 as before.
        await session.execute(
            select(Session.id)
            .where(
                Session.id == session_id,
                Session.tenant_id == ctx.tenant_id,
                Session.principal_id == ctx.principal_id,
            )
            .with_for_update(read=True)
        )
    invocation = await session.scalar(
        select(SkillInvocation)
        .where(
            SkillInvocation.id == invocation_id,
            SkillInvocation.tenant_id == ctx.tenant_id,
        )
        .with_for_update()
    )
    if invocation is None or not await _invocation_visible(session, ctx, invocation):
        raise NotFoundError(
            "Skill invocation not found", details={"invocationId": str(invocation_id)}
        )
    if (
        invocation.status != SkillInvocationStatus.RUNNING
        or invocation.executor_principal_id != ctx.principal_id
        or invocation.fencing_token != fencing_token
        or invocation.lease_expires_at is None
        or invocation.lease_expires_at <= utcnow()
        or (
            invocation.executor_session_id is not None
            and invocation.executor_session_id != session_id
        )
    ):
        raise ConflictError(
            "stale_invocation_lease",
            "This executor no longer holds the lease on the invocation",
            details={
                "invocationId": str(invocation.id),
                "status": invocation.status,
                "fencingToken": fencing_token,
                "currentFencingToken": invocation.fencing_token,
            },
        )
    if invocation.executor_session_id is not None:
        await _require_executor_session(session, ctx, invocation.executor_session_id)
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    return invocation, skill


async def heartbeat_skill_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    invocation_id: uuid.UUID,
    fencing_token: int,
    lease_seconds: int | None = None,
    session_id: uuid.UUID | None = None,
) -> tuple[SkillInvocation, Skill]:
    invocation, skill = await _locked_own_lease(
        session, ctx, invocation_id, fencing_token, session_id
    )
    assert skill.contract is not None
    ttl = clamp_ttl(
        lease_seconds,
        default=min(
            max(
                skill.contract["timeoutSeconds"] + LEASE_MARGIN_SECONDS,
                settings.claim_ttl_min_seconds,
            ),
            settings.claim_ttl_max_seconds,
        ),
        minimum=settings.claim_ttl_min_seconds,
        maximum=settings.claim_ttl_max_seconds,
    )
    now = utcnow()
    # Capped by the attempt deadline, and never shortened by a heartbeat.
    invocation.lease_expires_at = max(
        invocation.lease_expires_at or now,
        min(now + timedelta(seconds=ttl), _attempt_deadline(invocation, skill)),
    )
    invocation.heartbeat_at = now
    invocation.updated_at = now
    return invocation, skill


async def complete_skill_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    invocation_id: uuid.UUID,
    fencing_token: int,
    output: dict[str, Any],
    cost: dict[str, Any] | None = None,
    session_id: uuid.UUID | None = None,
    store: ContentStore | None = None,
) -> tuple[SkillInvocation, Skill]:
    """Accept a result — after checking it against the contract ourselves.

    The execution call of a task also hands in the task's typed outputs
    (``record_task_outputs``) before the ``skill_result`` that reports them.
    """
    invocation, skill = await _locked_own_lease(
        session, ctx, invocation_id, fencing_token, session_id, lock_authority=True
    )
    assert skill.contract is not None
    guard_json_document(output, label="output", max_bytes=MAX_PAYLOAD_BYTES)
    if cost is not None:
        guard_json_document(cost, label="cost")

    errors = schema_errors(skill.contract["outputs"], output)
    if errors:
        # Not retryable: the same implementation would produce the same shape,
        # and the contract, not the executor, decides what "success" means.
        await _finish_failed(
            session,
            invocation,
            skill,
            error={
                "code": "output_contract_violation",
                "retryable": False,
                "message": "output does not match the skill's output schema",
                "details": {"errors": errors, **_rejected_output(output)},
            },
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
        )
        invocation.cost = cost
        return invocation, skill

    now = utcnow()
    invocation.status = SkillInvocationStatus.SUCCEEDED
    invocation.output = output
    invocation.cost = cost
    invocation.error = None
    invocation.lease_expires_at = None
    invocation.finished_at = now
    invocation.updated_at = now

    outputs = await record_task_outputs(session, ctx, store, invocation, skill, output=output)
    if invocation.task_id is not None:
        invocation.artifact_id = await _record_result_artifact(
            session, ctx, invocation, skill, output=output, outputs=outputs
        )

    await record_event(
        session,
        tenant_id=invocation.tenant_id,
        event_type="skill.invocation_succeeded",
        entity_type="skill_invocation",
        entity_id=invocation.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "skillId": str(skill.id),
            "skill": skill.name,
            "version": skill.version,
            "attempt": invocation.attempt,
            "taskId": str(invocation.task_id) if invocation.task_id else None,
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "artifactId": str(invocation.artifact_id) if invocation.artifact_id else None,
            "cost": cost,
            "outputs": outputs,
        },
    )
    return invocation, skill


async def fail_skill_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    invocation_id: uuid.UUID,
    fencing_token: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | None = None,
    session_id: uuid.UUID | None = None,
) -> tuple[SkillInvocation, Skill]:
    invocation, skill = await _locked_own_lease(
        session, ctx, invocation_id, fencing_token, session_id
    )
    if details is not None:
        guard_json_document(details, label="error.details")
    error: dict[str, Any] = {"code": code, "retryable": retryable, "message": message}
    if details:
        error["details"] = details
    await _retry_or_fail(
        session,
        invocation,
        error=error,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        skill=skill,
    )
    return invocation, skill


async def cancel_skill_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    invocation_id: uuid.UUID,
    reason: str = "cancelled",
    initiator: str = CANCELLED_BY_PRINCIPAL,
) -> tuple[SkillInvocation, Skill]:
    """Stop a call that has not finished (``pending`` or ``running``).

    Allowed to the call's authority (who acts through it) and to a tenant
    administrator (``org.manage``); anybody else gets the same 404 as for a
    missing id. A running call's executor is fenced out: its lease is no
    longer valid, so its ``:complete``/``:fail`` get ``stale_invocation_lease``
    — but it may already have acted, which the event says (``wasRunning``).
    Cancelling a cancelled call returns it unchanged; a finished one is 409.
    ``initiator`` is ``system`` when core cancels on its own (an outcome that
    stopped waiting) rather than on the request of ``ctx``'s principal.
    """
    await authorize(ctx, Permission.SKILLS_INVOKE, Permission.ORG_MANAGE)
    invocation = await session.scalar(
        select(SkillInvocation)
        .where(
            SkillInvocation.id == invocation_id,
            SkillInvocation.tenant_id == ctx.tenant_id,
        )
        .with_for_update()
    )
    if (
        invocation is None
        or not (
            invocation.authority_principal_id == ctx.principal_id or ctx.has(Permission.ORG_MANAGE)
        )
        or not await _invocation_visible(session, ctx, invocation)
    ):
        raise NotFoundError(
            "Skill invocation not found", details={"invocationId": str(invocation_id)}
        )
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    if invocation.status == SkillInvocationStatus.CANCELLED:
        return invocation, skill
    if invocation.status not in LIVE_STATUSES:
        raise ConflictError(
            "invocation_terminal",
            "The invocation has already finished",
            details={"invocationId": str(invocation.id), "status": invocation.status},
        )
    await _finish_cancelled(
        session,
        invocation,
        skill,
        reason=reason,
        code="cancelled",
        initiator=initiator,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
    )
    return invocation, skill


def _rejected_output(output: dict[str, Any]) -> dict[str, Any]:
    """Keep what the contract rejected — it is the evidence of the verdict.

    Bounded: an oversized output is recorded by size only, so a violation
    cannot turn the error column into a second, unchecked output store.
    """
    size = len(canonical_bytes(output))
    if size > MAX_REJECTED_OUTPUT_BYTES:
        return {"rejectedOutputTruncated": True, "rejectedOutputBytes": size}
    return {"rejectedOutput": output}


# --- transitions --------------------------------------------------------------


async def _retry_or_fail(
    session: AsyncSession,
    invocation: SkillInvocation,
    *,
    error: dict[str, Any],
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str | None,
    skill: Skill | None = None,
) -> None:
    """A retryable failure with attempts left goes back to the queue."""
    skill = skill or await session.get(Skill, invocation.skill_id)
    assert skill is not None and skill.contract is not None
    now = utcnow()
    if error.get("retryable") and invocation.attempt < invocation.max_attempts:
        backoff = skill.contract["retryPolicy"]["backoffSeconds"]
        invocation.status = SkillInvocationStatus.PENDING
        invocation.error = error
        invocation.available_at = now + timedelta(seconds=backoff)
        invocation.lease_expires_at = None
        invocation.updated_at = now
        await record_event(
            session,
            tenant_id=invocation.tenant_id,
            event_type="skill.invocation_retry_scheduled",
            entity_type="skill_invocation",
            entity_id=invocation.id,
            actor_id=actor_id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_run_id=trace_run_id,
            payload={
                "skillId": str(skill.id),
                "attempt": invocation.attempt,
                "maxAttempts": invocation.max_attempts,
                "availableAt": invocation.available_at.isoformat(),
                "error": {"code": error["code"], "retryable": True},
            },
        )
        return
    await _finish_failed(
        session,
        invocation,
        skill,
        error=error,
        actor_id=actor_id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
    )


async def _finish_failed(
    session: AsyncSession,
    invocation: SkillInvocation,
    skill: Skill,
    *,
    error: dict[str, Any],
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str | None,
) -> None:
    now = utcnow()
    invocation.status = SkillInvocationStatus.FAILED
    invocation.error = error
    invocation.output = None
    invocation.lease_expires_at = None
    invocation.finished_at = now
    invocation.updated_at = now
    await record_event(
        session,
        tenant_id=invocation.tenant_id,
        event_type="skill.invocation_failed",
        entity_type="skill_invocation",
        entity_id=invocation.id,
        actor_id=actor_id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
        payload={
            "skillId": str(skill.id),
            "skill": skill.name,
            "version": skill.version,
            "attempt": invocation.attempt,
            "maxAttempts": invocation.max_attempts,
            "taskId": str(invocation.task_id) if invocation.task_id else None,
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "error": {
                "code": error["code"],
                "retryable": bool(error.get("retryable")),
                "message": str(error.get("message", ""))[:500],
            },
        },
    )


async def _finish_cancelled(
    session: AsyncSession,
    invocation: SkillInvocation,
    skill: Skill,
    *,
    reason: str,
    code: str,
    initiator: str,
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str | None,
) -> None:
    """``cancelled`` is terminal; ``error`` says who stopped it and why.

    ``cancelledBy`` is the principal whose authority the cancellation is
    recorded under; ``initiator`` says whether that principal asked for it
    (``principal``) or core did it on its own (``system``: a basis found gone,
    an outcome that stopped waiting).
    """
    now = utcnow()
    was_running = invocation.status == SkillInvocationStatus.RUNNING
    invocation.status = SkillInvocationStatus.CANCELLED
    invocation.error = {
        "code": code,
        "retryable": False,
        "message": reason,
        "details": {
            "cancelledBy": str(actor_id) if actor_id else None,
            "initiator": initiator,
            "wasRunning": was_running,
        },
    }
    invocation.output = None
    invocation.lease_expires_at = None
    invocation.finished_at = now
    invocation.updated_at = now
    await record_event(
        session,
        tenant_id=invocation.tenant_id,
        event_type="skill.invocation_cancelled",
        entity_type="skill_invocation",
        entity_id=invocation.id,
        actor_id=actor_id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
        payload={
            "skillId": str(skill.id),
            "skill": skill.name,
            "version": skill.version,
            "attempt": invocation.attempt,
            "taskId": str(invocation.task_id) if invocation.task_id else None,
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "code": code,
            "reason": reason[:500],
            "wasRunning": was_running,
            "cancelledBy": str(actor_id) if actor_id else None,
            "initiator": initiator,
        },
    )


async def _record_result_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    invocation: SkillInvocation,
    skill: Skill,
    *,
    output: dict[str, Any],
    outputs: list[dict[str, Any]],
) -> uuid.UUID:
    """Evidence on the Work item: a ``skill_result`` artifact (ADR-0056 §2).

    Authored by the invocation's authority, not the executor: the executor is
    a transport and the result belongs to whoever acted. ``metadata.outputs``
    says what became of the task's typed outputs (empty unless this is the
    task's execution call).
    """
    task = await session.get(Task, invocation.task_id)
    assert task is not None
    artifact = Artifact(
        id=new_uuid(),
        tenant_id=invocation.tenant_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=invocation.run_id,
        created_by_principal_id=invocation.authority_principal_id,
        type="skill_result",
        name=f"{skill.name}@{skill.version}",
        uri=None,
        content={
            "invocationId": str(invocation.id),
            "skill": skill.name,
            "version": skill.version,
            "attempt": invocation.attempt,
            "output": output,
        },
        supersedes_artifact_id=None,
        metadata_json={
            "skillId": str(skill.id),
            "invocationId": str(invocation.id),
            "executorPrincipalId": str(ctx.principal_id),
            "outputs": outputs,
        },
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()
    await record_event(
        session,
        tenant_id=invocation.tenant_id,
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
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "uri": None,
            "supersedesArtifactId": None,
            "skillInvocationId": str(invocation.id),
            **artifact_event_fields(artifact),
        },
    )
    return artifact.id


__all__ = [
    "LIVE_STATUSES",
    "InvocationResult",
    "cancel_skill_invocation",
    "claim_skill_invocation",
    "complete_skill_invocation",
    "expire_leases",
    "fail_skill_invocation",
    "get_skill_invocation",
    "heartbeat_skill_invocation",
    "invoke_skill",
    "resolve_skill_ref",
]
