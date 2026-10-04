"""Executor of approval outcomes declared by a task type (CP-ADR-0061).

A gate approval decided on a task whose type version declares outcomes is
marked ``outcome_status='pending'`` together with the deciding credential and
``outcome_next_attempt_at``. The worker picks up due outcomes
(:func:`due_outcomes`) and calls :func:`execute_outcome`, which runs the
declared actions in order:

* **authority** — every action goes through the ordinary command with an
  ``AuthContext`` rebuilt from the decider's credential snapshot, checked by
  the same authorizer the API uses (for a decision from a channel the
  snapshot carries the binding's rights, see :func:`decision_authority`).
  The credential must still be active, and
  what the actions read from the decision context the decider must be able to
  read. An action the decider may not perform fails the outcome (``forbidden``);
* **idempotency** — ``(approval_id, action_index)`` is the key: an index with
  an ``executed`` row is never run again, so a repeated attempt does nothing
  twice and a replay resumes at the first action that did not execute;
* **claims** — an action that would have to write through somebody's live
  claim does not fail: the outcome is ``deferred`` and retried after the claim
  is released or expires;
* **skills** — ``invokeSkill`` queues a ``skill_invocation`` through the same
  path as ``POST /skills/{ref}:invoke`` (the decided approval is the basis of
  an ``external_write``) and is executed once queued. Its reactions wait
  (``deferred``) until every invocation of the outcome has finished; then
  ``onSuccess`` runs if it succeeded as ``expect`` says, ``onFailure``
  otherwise, and the other branch is recorded as skipped. A call nobody
  claims for ``skill_wait`` is cancelled, which is a failure. A call whose
  basis is gone (``basis_revoked``: the task was closed or the approval
  withdrawn meanwhile) — cancelled at claim, or by the outcome itself on its
  next pass over a call still pending — runs neither branch: both are
  recorded as skipped with that reason;
* **failure** — a refused action's writes roll back (savepoint), the rest of
  the outcome is not attempted, the decision itself stands. The failure is
  recorded (``approval.outcome_failed``) and a work item for the decider is
  filed by core's own service principal; ``POST /approvals/{id}:replay-outcome``
  continues from there. An unexpected error is retried with backoff on the
  approval's own counter and, once attempts are exhausted, fails the outcome
  the same way — it never sits in ``pending`` unnoticed.

Core knows the action vocabulary, never what a tenant uses it for.
"""

import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.agent_assignees import (
    agent_principal,
    is_agent_reference,
)
from control_plane.application.commands.principals import ensure_core_principal
from control_plane.application.commands.relations import add_relation, resolve_task
from control_plane.application.commands.role_references import is_role_reference, role_for_task
from control_plane.application.commands.skill_invocations import (
    CANCELLED_BY_SYSTEM,
    LIVE_STATUSES,
    cancel_skill_invocation,
    invoke_skill,
    lost_basis,
    resolve_skill_ref,
    revoke_lost_basis,
)
from control_plane.application.commands.task_comments import add_comment
from control_plane.application.commands.task_types import task_type_of
from control_plane.application.commands.tasks import complete_task, create_task, update_task
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import (
    lock_principal_key_share,
    lock_principals_key_share,
)
from control_plane.application.visibility import approval_visible, with_visibility
from control_plane.domain.approval_outcomes import (
    COMMENT,
    COMPLETE_TASK,
    CUSTOM_FIELDS,
    DEFAULT_GATE,
    ENSURE_WORK,
    INVOKE_SKILL,
    ON_SUCCESS,
    REQUEST_APPROVAL,
    TRANSITION,
    Action,
    Path,
    expressions_in,
    render,
    schema_actions,
)
from control_plane.domain.enums import (
    ApprovalStatus,
    Permission,
    PrincipalStatus,
    SkillInvocationStatus,
    SkillSideEffects,
    TaskPriority,
    TaskRelationType,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_item import WorkItemStatusCategory
from control_plane.infrastructure.auth.iam import SCOPE_READ, SCOPE_WRITE, narrow_permissions
from control_plane.infrastructure.db.models import (
    ApiKey,
    Approval,
    ApprovalOutcomeAction,
    Artifact,
    Event,
    ExternalReference,
    IamPrincipalBinding,
    Principal,
    Skill,
    SkillInvocation,
    Task,
    TaskRelation,
)

logger = logging.getLogger(__name__)

OUTCOME_PENDING = "pending"
OUTCOME_DEFERRED = "deferred"
OUTCOME_EXECUTED = "executed"
OUTCOME_FAILED = "failed"
# States the worker still has to act on.
OUTCOME_LIVE = (OUTCOME_PENDING, OUTCOME_DEFERRED)

# How long a deferred outcome waits before it looks at the claim again.
DEFAULT_DEFER_SECONDS = 15.0
# How long a queued skill call may sit unclaimed (``pending``) before the
# outcome gives up on it: cancels it and reacts as to a failure.
DEFAULT_SKILL_WAIT_SECONDS = 24 * 3600.0
# Why such a call was cancelled.
SKILL_WAIT_EXPIRED = "approval_outcome_skill_wait_expired"
# A call cancelled at claim because what allowed it is gone (M2.2): the
# gated task was closed or the approval withdrawn — by somebody else, since
# the outcome itself closes nothing before the call ends. Nothing was
# attempted and the decision no longer stands as the basis, so neither
# reaction runs (a failure reaction would file work about a merge nobody
# tried). A disabled skill or an expired wait is not this: the basis stands,
# the call just did not happen, and onFailure is told.
BASIS_REVOKED = "basis_revoked"
BASIS_GONE = frozenset({"task_terminal", "approval_withdrawn"})
# A pending outcome nobody has touched for this long is stuck (the worker is
# down or wedged): its decider may replay it by hand.
STALE_PENDING = timedelta(minutes=10)
# The code of an outcome failed because unexpected errors exhausted retries.
ATTEMPTS_EXHAUSTED = "outcome_attempts_exhausted"

# The origin mark of work created by an outcome, until the Work Graph (M1.1)
# gives work items a first-class origin: an external reference in core's own
# namespace. Its external id is the ensureWork key, which is what makes
# ensureWork an "ensure": the same key never creates a second work item.
ORIGIN_SYSTEM = "control-plane"
ORIGIN_TYPE = "approval-outcome"
FAILURE_KEY_PREFIX = "approval-outcome-failure:"

DECIDED_EVENTS = frozenset({"approval.approved", "approval.rejected"})

# Why an outcome waits: a live claim on the target task, or a skill invocation
# whose end an onFailure reaction depends on.
WAIT_TASK_CLAIMED = "task_claimed"
WAIT_SKILL_PENDING = "skill_pending"


class _Deferred(Exception):
    """Not yet: try the outcome again later (a live claim, a running skill)."""

    def __init__(self, details: dict[str, Any], reason: str = WAIT_TASK_CLAIMED) -> None:
        super().__init__(f"outcome waits: {reason}")
        self.details = details
        self.reason = reason


def outcome_of(approval: Approval) -> str | None:
    if approval.status == ApprovalStatus.APPROVED:
        return "approved"
    if approval.status == ApprovalStatus.REJECTED:
        return "rejected"
    return None


# --- declaration and authority ---------------------------------------------------


async def declared_actions(
    session: AsyncSession, approval: Approval, outcome: str | None = None
) -> tuple[Action, ...]:
    """Actions the approval's task type declares for this decision.

    Only a GATE approval sets an outcome in motion: a plain approval is
    advisory, and a task type cannot know what an arbitrary approval that
    merely mentions its task was about.
    """
    outcome = outcome or outcome_of(approval)
    if outcome is None or not approval.gate or approval.task_id is None:
        return ()
    task = await session.get(Task, approval.task_id)
    if task is None:  # pragma: no cover - forbidden by the foreign key
        return ()
    task_type = await task_type_of(session, task)
    return schema_actions(task_type.approval_schema, DEFAULT_GATE, outcome)


def authority_snapshot(ctx: AuthContext) -> dict[str, Any]:
    """What the deciding credential could do at decision time."""
    return {
        "principalId": str(ctx.principal_id),
        "principalKind": ctx.principal_kind,
        "credentialId": str(ctx.api_key_id),
        "permissions": sorted(ctx.permissions),
        "iamPrincipalId": str(ctx.iam_principal_id) if ctx.iam_principal_id else None,
    }


async def decision_authority(session: AsyncSession, ctx: AuthContext) -> dict[str, Any]:
    """The authority an outcome of THIS decision runs with.

    Usually the deciding credential itself (:func:`authority_snapshot`). A
    decision token from a channel (``control-plane:decide``, CP-ADR-0070)
    can do nothing but decide, so its own permissions would refuse every
    other declared action; by the owner's decision its outcome runs with the
    permissions of the human's binding instead, under the ceiling a web
    session gets (read and write scopes, never ``admin``). The credential
    stays the binding, so revoking it still stops the outcome at every
    attempt. The token itself writes nothing beyond the decision: only the
    actions the task type declares get the wider authority.
    """
    snapshot = authority_snapshot(ctx)
    if ctx.purpose_ref is None or ctx.iam_principal_id is None:
        return snapshot
    binding = await session.get(IamPrincipalBinding, ctx.api_key_id)
    if binding is None or binding.principal_id != ctx.principal_id:
        return snapshot
    snapshot["permissions"] = sorted(
        narrow_permissions(binding.permissions, frozenset({SCOPE_READ, SCOPE_WRITE}))
    )
    snapshot["authoritySource"] = "binding"
    snapshot["channel"] = ctx.channel
    return snapshot


def _decider_context(
    approval: Approval, *, trace_run_id: str, causation_id: str | None
) -> AuthContext:
    authority = approval.decision_authority or {}
    iam = authority.get("iamPrincipalId")
    assert approval.decision_by_principal_id is not None
    return AuthContext(
        tenant_id=approval.tenant_id,
        principal_id=approval.decision_by_principal_id,
        principal_kind=str(authority.get("principalKind") or "human"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=f"approval-outcome:{approval.id}",
        correlation_id=f"approval:{approval.id}",
        causation_id=causation_id,
        trace_run_id=trace_run_id,
        iam_principal_id=uuid.UUID(iam) if iam else None,
    )


async def _check_credential(session: AsyncSession, approval: Approval) -> None:
    """The snapshot is only as good as the credential it was taken from."""
    assert approval.decision_by_principal_id is not None
    await require_active_credential(
        session,
        authority=approval.decision_authority or {},
        principal_id=approval.decision_by_principal_id,
        subject="the approval was decided with",
    )


async def require_active_credential(
    session: AsyncSession,
    *,
    authority: dict[str, Any],
    principal_id: uuid.UUID,
    subject: str,
) -> None:
    """A credential snapshot (:func:`authority_snapshot`) still stands.

    What is checked is core's own state: the API key row (not revoked, not
    expired), or the IAM binding row (``active``, not revoked — the local
    revocation policy of ADR-0053), each still belonging to the principal,
    and the principal being ``active``. A token or PAT revoked only in the
    IAM itself is not seen here: the binding has to be revoked or disabled
    for work done through it to stop (in ``policy`` mode the PDP is still
    asked about every action). Shared by approval outcomes and work rules
    (CP-ADR-0063), which both act later on somebody's standing authority.
    """
    credential_id = uuid.UUID(str(authority["credentialId"]))
    active: bool
    if authority.get("iamPrincipalId"):
        binding = await session.get(IamPrincipalBinding, credential_id, populate_existing=True)
        active = (
            binding is not None
            and binding.principal_id == principal_id
            and binding.status == "active"
            and binding.revoked_at is None
        )
    else:
        key = await session.get(ApiKey, credential_id, populate_existing=True)
        active = (
            key is not None
            and key.principal_id == principal_id
            and key.revoked_at is None
            and (key.expires_at is None or key.expires_at > utcnow())
        )
    if active:
        principal = await session.get(Principal, principal_id, populate_existing=True)
        active = principal is not None and principal.status == PrincipalStatus.ACTIVE
    if not active:
        raise AuthorizationError(
            f"The credential {subject} is no longer active",
            code="credential_inactive",
            details={"credentialId": str(credential_id)},
        )


# --- decision context ($.task, $.approval, $.spawnedBy) --------------------------


def task_view(task: Task | None) -> dict[str, Any]:
    if task is None:
        return {}
    return {
        "id": str(task.id),
        "publicId": task.public_id,
        "title": task.title,
        "description": task.description,
        "assigneeId": str(task.assignee_id) if task.assignee_id else None,
        "workspaceId": str(task.workspace_id) if task.workspace_id else None,
        "status": task.status,
        "priority": task.priority,
        "customFields": dict(task.custom_fields or {}),
    }


def walk_path(view: dict[str, Any], path: Path) -> Any:
    value = view.get(path.field or "")
    if path.key is not None:
        return value.get(path.key) if isinstance(value, dict) else None
    return value


@dataclass
class DecisionContext:
    """Plain snapshots taken before the first action runs.

    Values, not ORM objects: a failed action rolls its savepoint back, which
    may expire loaded rows, and the failure path must still read them.
    """

    approval_id: uuid.UUID | None
    decided_by: uuid.UUID | None
    outcome: str
    approval: dict[str, Any]
    task: dict[str, Any]
    spawned_by: dict[str, Any]
    # (root, artifact type) -> metadata of the newest artifact of that type.
    artifacts: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # What work filed by ``ensureWork`` came from: the ``process`` origin ref
    # (CP-ADR-0062) and the metadata of its origin mark. A decided approval,
    # or a completed task (work after completion, amendment 2026-09-25).
    process_ref: str = ""
    origin_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def task_id(self) -> uuid.UUID:
        return uuid.UUID(self.task["id"])

    @property
    def workspace_id(self) -> uuid.UUID | None:
        value = self.task.get("workspaceId")
        return uuid.UUID(value) if value else None

    def resolve(self, path: Path, invocation: dict[str, Any] | None = None) -> Any:
        if path.root == "approval":
            return walk_path(self.approval, path)
        if path.root == "invocation":
            return walk_path(invocation or {}, path)
        if path.artifact_type is not None:
            metadata = self.artifacts.get((path.root, path.artifact_type)) or {}
            return metadata.get(path.metadata_key or "")
        return walk_path(self.task if path.root == "task" else self.spawned_by, path)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


async def spawned_by_of(session: AsyncSession, task: Task) -> Task | None:
    """The task this one was spawned by (the oldest such edge, if several)."""
    target = await session.scalar(
        select(TaskRelation.to_task_id)
        .where(
            TaskRelation.tenant_id == task.tenant_id,
            TaskRelation.from_task_id == task.id,
            TaskRelation.relation_type == TaskRelationType.SPAWNED_BY,
        )
        .order_by(TaskRelation.created_at, TaskRelation.id)
        .limit(1)
    )
    return await session.get(Task, target) if target is not None else None


async def base_context(session: AsyncSession, approval: Approval, outcome: str) -> DecisionContext:
    """The approval and its own task — what a failure report needs.

    Read with core's eyes and never fed to an action: that is what
    :func:`build_context` is for.
    """
    assert approval.task_id is not None
    task = await session.get(Task, approval.task_id, populate_existing=True)
    assert task is not None
    return DecisionContext(
        approval_id=approval.id,
        decided_by=approval.decision_by_principal_id,
        outcome=outcome,
        approval={
            "id": str(approval.id),
            "comment": approval.comment,
            "decidedBy": (
                str(approval.decision_by_principal_id)
                if approval.decision_by_principal_id
                else None
            ),
            "decidedAt": approval.decision_at.isoformat() if approval.decision_at else None,
            "outcome": outcome,
        },
        task=task_view(task),
        spawned_by={},
        process_ref=f"approval:{approval.id}",
        origin_metadata={"origin": "approval", "approvalId": str(approval.id)},
    )


async def build_context(
    session: AsyncSession,
    ctx: AuthContext,
    approval: Approval,
    outcome: str,
    actions: tuple[Action, ...],
) -> DecisionContext:
    """Everything the actions' expressions reference, read as the decider.

    The decider must be able to read what their outcome copies around: the
    approval's task and the task it was spawned by (``tasks.read`` on that
    task) and artifacts (``artifacts.read``). Otherwise an outcome would carry
    into a new work item what its decider could never have seen.
    """
    context = await base_context(session, approval, outcome)
    await read_context(session, ctx, context, actions)
    return context


async def read_context(
    session: AsyncSession,
    ctx: AuthContext,
    context: DecisionContext,
    actions: tuple[Action, ...],
    extra: tuple[Path, ...] = (),
) -> None:
    """Fill ``spawnedBy`` and artifacts in, as far as the expressions need them.

    Read as ``ctx`` and checked like any read of theirs (see
    :func:`build_context`); ``extra`` — expressions read besides the actions'
    inputs (the ``when`` of work after completion).
    """
    paths = [*extra] + [
        path
        for action in actions
        for text in _strings(action.inputs)
        for path in expressions_in(text, invocation=True)
    ]
    roots = {path.root for path in paths}
    tenant_id = ctx.tenant_id
    task = await session.get(Task, context.task_id)
    assert task is not None
    spawned_by = await spawned_by_of(session, task) if "spawnedBy" in roots else None
    owners = {"task": task, "spawnedBy": spawned_by}
    for root in sorted(roots & {"task", "spawnedBy"}):
        owner = owners[root]
        if owner is not None:
            # Invisible work is missing work to its reader (CP-ADR-0082 §3.7).
            if not ctx.sees_workspace(owner.workspace_id):
                raise NotFoundError("Task not found", details={"taskId": str(owner.id)})
            await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(owner.id)))
    context.spawned_by = task_view(spawned_by)
    wanted = {(path.root, path.artifact_type) for path in paths if path.artifact_type is not None}
    if wanted:
        await authorize(ctx, Permission.ARTIFACTS_READ)
    for root, artifact_type in sorted(wanted):
        owner = owners[root]
        if owner is None:
            continue
        metadata = await session.scalar(
            select(Artifact.metadata_json)
            .where(
                Artifact.tenant_id == tenant_id,
                Artifact.task_id == owner.id,
                Artifact.type == artifact_type,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
        context.artifacts[(root, artifact_type)] = dict(metadata or {})


# --- actions --------------------------------------------------------------------


def _input_error(action: str, name: str, message: str) -> ValidationError:
    return ValidationError(
        "invalid_action_input",
        f"{action}.{name}: {message}",
        details={"action": action, "input": name},
    )


def _required_text(inputs: dict[str, Any], action: str, name: str) -> str:
    value = inputs.get(name)
    if value is None or not str(value).strip():
        raise _input_error(action, name, "resolved to an empty value")
    return str(value)


def _optional_uuid(inputs: dict[str, Any], action: str, name: str) -> uuid.UUID | None:
    value = inputs.get(name)
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise _input_error(action, name, f"{value!r} is not an id") from exc


async def _target_task(
    session: AsyncSession, ctx: AuthContext, ref: Any, context: "DecisionContext"
) -> Task:
    """The task an action names, or the approval's own task by default.

    Loaded fresh: an earlier action of the same outcome may have moved it.
    """
    if ref is None or ref == "":
        ref = context.task_id
    task = await resolve_task(session, ctx, str(ref))
    await session.refresh(task)
    return task


async def _origin(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> Task | None:
    entity_id = await session.scalar(
        select(ExternalReference.entity_id).where(
            ExternalReference.tenant_id == tenant_id,
            ExternalReference.external_system == ORIGIN_SYSTEM,
            ExternalReference.external_type == ORIGIN_TYPE,
            ExternalReference.external_id == key,
            ExternalReference.entity_type == "task",
        )
    )
    return await session.get(Task, entity_id) if entity_id is not None else None


async def _mark_origin(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task: Task,
    key: str,
    metadata: dict[str, Any],
) -> None:
    now = utcnow()
    reference = ExternalReference(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        entity_type="task",
        entity_id=task.id,
        external_system=ORIGIN_SYSTEM,
        external_type=ORIGIN_TYPE,
        external_id=key,
        metadata_json=metadata,
        version=1,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    session.add(reference)
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.external_reference_added",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "externalReferenceId": str(reference.id),
            "externalSystem": ORIGIN_SYSTEM,
            "externalType": ORIGIN_TYPE,
            "externalId": key,
        },
    )


async def _ensure_work(
    session: AsyncSession,
    ctx: AuthContext,
    context: DecisionContext,
    inputs: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    key = _required_text(inputs, ENSURE_WORK, "key")
    existing = await _origin(session, ctx.tenant_id, key)
    if existing is not None:
        # An ensure, not an upsert: the work item found keeps its fields and
        # gets no second approval.
        # The key is tenant-wide and chosen by the schema's author: finding a
        # work item by it must not hand the decider one they cannot read.
        # Work of a workspace outside the decider's visibility is not theirs
        # to read either (CP-ADR-0082 §3.7): ``authorize`` on a task does not
        # see workspaces.
        if not ctx.sees_workspace(existing.workspace_id):
            raise NotFoundError("Task not found", details={"key": key})
        try:
            await authorize(
                ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(existing.id))
            )
        except AuthorizationError as exc:
            raise NotFoundError("Task not found", details={"key": key}) from exc
        return {"taskId": str(existing.id), "publicId": existing.public_id, "created": False}

    priority = inputs.get("priority") or TaskPriority.MEDIUM
    workspace_id = (
        _optional_uuid(inputs, ENSURE_WORK, "workspace")
        if inputs.get("workspace")
        else context.workspace_id
    )
    task = await create_task(
        session,
        ctx,
        title=_required_text(inputs, ENSURE_WORK, "title")[:500],
        description=str(inputs.get("description") or ""),
        priority=str(priority),
        type_key=_required_text(inputs, ENSURE_WORK, "type"),
        # An id, or an agent of the registry by key (CP-ADR-0073, A1).
        assignee_id=(
            str(inputs["assignee"])
            if is_agent_reference(inputs.get("assignee"))
            else _optional_uuid(inputs, ENSURE_WORK, "assignee")
        ),
        assignee_field=f"{ENSURE_WORK}.assignee",
        workspace_id=workspace_id,
        # Checked against the field_schema of the version the work item pins:
        # a misfit fails the action with custom_fields_invalid.
        custom_fields=_custom_fields(inputs),
        # Filed by core as a step of the decided approval (or of the
        # completed task), not by the principal typing it in (CP-ADR-0062).
        origin={"kind": "process", "ref": context.process_ref},
    )
    relation = inputs.get("relation") or {}
    for relation_type, target in relation.items():
        if target is None or target == "":
            raise _input_error(ENSURE_WORK, f"relation.{relation_type}", "resolved to nothing")
        await add_relation(
            session,
            ctx,
            from_task_ref=str(task.id),
            to_task_ref=str(target),
            relation_type=relation_type,
        )
    await _mark_origin(
        session, ctx, task=task, key=key, metadata={**context.origin_metadata, "actionIndex": index}
    )
    result: dict[str, Any] = {
        "taskId": str(task.id),
        "publicId": task.public_id,
        "created": True,
        "relations": {k: str(v) for k, v in relation.items()},
    }
    if task.custom_fields:
        # Names only: values may be anything the type holds, the evidence is
        # read more widely than the task.
        result["customFields"] = sorted(task.custom_fields)
    gate = inputs.get(REQUEST_APPROVAL)
    if gate:
        result["approvalId"] = await _request_gate(session, ctx, task, gate)
    return result


def _custom_fields(inputs: dict[str, Any]) -> dict[str, Any] | None:
    """Rendered ``customFields``; a value that resolved to nothing is left out.

    Left out, not blanked: a field the schema requires is then reported
    missing (``custom_fields_invalid``) instead of being filed empty; use
    ``!`` to fail on the expression itself (``unresolved_expression``).
    """
    fields = inputs.get(CUSTOM_FIELDS) or {}
    if not isinstance(fields, dict):  # pragma: no cover - the schema refuses it
        raise _input_error(ENSURE_WORK, CUSTOM_FIELDS, "must be an object")
    kept = {name: value for name, value in fields.items() if value is not None and value != ""}
    return kept or None


async def _request_gate(session: AsyncSession, ctx: AuthContext, task: Task, spec: Any) -> str:
    """``requestApproval``: a gate approval on the work item just filed.

    Same command as ``POST /approvals`` with ``gate: true``, with the authority
    of whoever files the work (``approvals.manage``). Only ever on a work item
    this action created, in its savepoint: the ensure cannot open a second
    decision on work it found.
    """
    from control_plane.application.commands.approvals import request_approval

    if not isinstance(spec, dict):  # pragma: no cover - the schema refuses it
        raise _input_error(ENSURE_WORK, REQUEST_APPROVAL, "must be an object")
    field_name = f"{ENSURE_WORK}.{REQUEST_APPROVAL}"
    role_id: uuid.UUID | None = None
    assignee: uuid.UUID | None = None
    if is_role_reference(spec.get("assignee")):
        # A role of the package by slug (CP-ADR-0061, amendment 2026-10-01),
        # seen from the workspace of the work item just filed.
        role_id = await role_for_task(
            session,
            ctx,
            str(spec["assignee"]),
            workspace_id=task.workspace_id,
            field=f"{field_name}.assignee",
        )
    else:
        assignee = _optional_uuid({"assignee": spec.get("assignee")}, field_name, "assignee")
        if assignee is None:
            raise _input_error(field_name, "assignee", "resolved to nothing")
    approval = await request_approval(
        session,
        ctx,
        task_ref=str(task.id),
        assigned_principal_id=assignee,
        required_role_id=role_id,
        comment=str(spec.get("comment") or ""),
        gate=True,
    )
    return str(approval.id)


@contextmanager
def _defer_on_claim() -> Iterator[None]:
    """A live claim on the target is not a failure — it is "not yet".

    The typical case is a human who claimed the review, runs it and decides
    the gate from inside that run: completing the task under their own live
    claim and running run would be refused (``task_claimed``). The outcome
    waits instead and resumes once the claim is released or expires; if the
    holder completes the task themselves meanwhile, ``completeTask`` finds it
    completed and reports ``alreadyCompleted``.
    """
    try:
        yield
    except ConflictError as exc:
        if exc.code == "task_claimed":
            raise _Deferred(exc.details) from exc
        raise


async def _complete(
    session: AsyncSession, ctx: AuthContext, context: DecisionContext, inputs: dict[str, Any]
) -> dict[str, Any]:
    task = await _target_task(session, ctx, inputs.get("task"), context)
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_SUCCESS:
        return {"taskId": str(task.id), "publicId": task.public_id, "alreadyCompleted": True}
    with _defer_on_claim():
        done = await complete_task(
            session,
            ctx,
            task_ref=str(task.id),
            expected_version=task.version,
            # A task with acceptance checks is verified first (CP-ADR-0067):
            # the attempt records this decision as what completed it.
            trigger="approval" if context.approval_id else "complete",
            trigger_ref=str(context.approval_id) if context.approval_id else None,
        )
    return {"taskId": str(done.id), "publicId": done.public_id, "status": done.status}


async def _comment(
    session: AsyncSession, ctx: AuthContext, context: DecisionContext, inputs: dict[str, Any]
) -> dict[str, Any]:
    task = await _target_task(session, ctx, inputs.get("task"), context)
    comment = await add_comment(
        session, ctx, task_ref=str(task.id), body=_required_text(inputs, COMMENT, "body")
    )
    return {"taskId": str(task.id), "commentId": str(comment.id)}


async def _transition(
    session: AsyncSession, ctx: AuthContext, context: DecisionContext, inputs: dict[str, Any]
) -> dict[str, Any]:
    task = await _target_task(session, ctx, inputs.get("task"), context)
    status = _required_text(inputs, TRANSITION, "status")
    if task.status == status:
        return {"taskId": str(task.id), "status": status, "alreadyInStatus": True}
    with _defer_on_claim():
        moved = await update_task(
            session, ctx, task_ref=str(task.id), expected_version=task.version, status=status
        )
    return {"taskId": str(moved.id), "status": moved.status}


def _approval_id(context: DecisionContext) -> uuid.UUID:
    """The decided approval: only an approval's outcome invokes skills."""
    assert context.approval_id is not None
    return context.approval_id


def _invocation_key(approval_id: uuid.UUID, index: int) -> str:
    """The idempotency key of an outcome's skill call: one per (approval, action)."""
    return f"approval-outcome:{approval_id}:{index}"


async def _invoke(
    session: AsyncSession,
    ctx: AuthContext,
    context: DecisionContext,
    inputs: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    """Queue the skill call exactly like ``POST /skills/{ref}:invoke`` would.

    Same command, same checks: ``skills.invoke`` and the skill's own required
    permissions of the decider, ``tasks.write`` on the gated task, and for an
    ``external_write`` skill the decided approval as its basis (single use per
    skill version). The action is done once the call is queued; how the call
    ends is the invocation's own events and the reactions
    (``onSuccess``/``onFailure``).
    """
    skill_inputs = inputs.get("inputs") or {}
    if not isinstance(skill_inputs, dict):  # pragma: no cover - the schema refuses it
        raise _input_error(INVOKE_SKILL, "inputs", "must be an object")
    ref = _required_text(inputs, INVOKE_SKILL, "skill")
    if "@" not in ref:
        # Publication refuses an unpinned reference to an external_write
        # skill, but a newer version may have become one since.
        skill = await resolve_skill_ref(session, ctx, ref)
        if skill.side_effects == SkillSideEffects.EXTERNAL_WRITE:
            raise _input_error(
                INVOKE_SKILL,
                "skill",
                f"{ref!r} resolves to the external_write {skill.name}@{skill.version}; "
                "an outcome calls such a skill only by name@version",
            )
    queued = await invoke_skill(
        session,
        ctx,
        skill_ref=ref,
        inputs=skill_inputs,
        idempotency_key=_invocation_key(_approval_id(context), index),
        task_ref=str(context.task_id),
        approval_id=_approval_id(context),
    )
    invocation, skill = queued.invocation, queued.skill
    return {
        "invocationId": str(invocation.id),
        "skill": f"{skill.name}@{skill.version}",
        "status": invocation.status,
        "created": queued.created,
        "authorizationBasis": invocation.authorization_basis,
    }


def _invocation_view(invocation: SkillInvocation, skill: Skill) -> dict[str, Any]:
    error = invocation.error or {}
    return {
        "id": str(invocation.id),
        "skill": f"{skill.name}@{skill.version}",
        "status": invocation.status,
        "output": dict(invocation.output or {}),
        "error": {"code": error.get("code"), "message": error.get("message")},
    }


def _basis_revoked(invocation: SkillInvocation) -> str | None:
    """Why the call was cancelled for a lost basis, if it was."""
    error = invocation.error or {}
    if (
        invocation.status == SkillInvocationStatus.CANCELLED
        and error.get("code") == BASIS_REVOKED
        and error.get("message") in BASIS_GONE
    ):
        return str(error["message"])
    return None


def _as_expected(invocation: SkillInvocation, expect: dict[str, Any]) -> bool:
    """Succeeded, and every ``expect`` field of the output equals its literal.

    Strict JSON equality: ``true`` does not match ``1``.
    """
    if invocation.status != SkillInvocationStatus.SUCCEEDED:
        return False
    output = invocation.output or {}
    return all(
        key in output and type(output[key]) is type(value) and output[key] == value
        for key, value in expect.items()
    )


def _wait_expired(invocation: SkillInvocation, skill_wait: timedelta) -> bool:
    """``skill_wait`` has passed since the call last moved."""
    moved = max(invocation.updated_at, invocation.available_at)
    return moved + skill_wait <= utcnow()


async def _due_to_cancel(
    session: AsyncSession, invocation: SkillInvocation, skill_wait: timedelta
) -> bool:
    """Would this pass cancel the pending call? A read, without its lock."""
    if _wait_expired(invocation, skill_wait):
        return True
    return await lost_basis(session, invocation, reasons=BASIS_GONE) is not None


async def _finished_invocation(
    session: AsyncSession,
    ctx: AuthContext,
    invocation_id: uuid.UUID,
    skill_wait: timedelta,
) -> tuple[SkillInvocation, Skill]:
    """The invocation once it has ended; :class:`_Deferred` while it has not.

    A call still ``pending`` — nobody has claimed it — has its basis checked
    on every pass, as a claim would: gone (``BASIS_GONE``) cancels it with
    ``basis_revoked``, so the outcome does not sit ``deferred`` until an
    executor turns up and no reaction runs for a call nobody attempted.
    The basis is checked before the state of the version: a disabled skill
    does not turn a closed review into a failed merge. With the basis
    standing, such a call is cancelled ``skill_wait`` after it last moved with
    the decider's authority (whose call it is): an outcome must not wait
    forever for an executor that is not there. That cancellation is what the
    reactions then see, as a failure. A ``running`` call is bounded by its
    lease and attempt deadline instead. Both cancellations are the system's
    (``initiator: system``), not the decider's.
    """
    invocation = await session.get(SkillInvocation, invocation_id, populate_existing=True)
    assert invocation is not None  # pragma: no cover - rows are never deleted
    if invocation.status == SkillInvocationStatus.PENDING and await _due_to_cancel(
        session, invocation, skill_wait
    ):
        # Locked only now, and only while still pending: a pass with nothing
        # to do leaves the row free for a claim. A claim racing this either
        # wins (running, left alone) or waits for the cancellation; under the
        # lock the call and its obstacle are checked again.
        locked = await session.scalar(
            select(SkillInvocation)
            .where(
                SkillInvocation.id == invocation.id,
                SkillInvocation.status == SkillInvocationStatus.PENDING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is not None:
            async with session.begin_nested():
                revoked = await revoke_lost_basis(session, ctx, locked, reasons=BASIS_GONE)
                if revoked is None and _wait_expired(locked, skill_wait):
                    await cancel_skill_invocation(
                        session,
                        ctx,
                        invocation_id=locked.id,
                        reason=SKILL_WAIT_EXPIRED,
                        initiator=CANCELLED_BY_SYSTEM,
                    )
        await session.refresh(invocation)
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    if invocation.status in LIVE_STATUSES:
        raise _Deferred(
            {"invocationId": str(invocation.id), "status": invocation.status},
            reason=WAIT_SKILL_PENDING,
        )
    return invocation, skill


async def _outcome_invocations(
    session: AsyncSession,
    ctx: AuthContext,
    actions: tuple[Action, ...],
    rows: dict[int, ApprovalOutcomeAction],
    skill_wait: timedelta,
) -> dict[int, tuple[SkillInvocation, Skill]]:
    """Every invocation the outcome queued, each finished — or :class:`_Deferred`.

    Reactions start only once ALL of them have ended: a reaction may close
    the gated task, and a call still queued for that task would then be
    cancelled at claim (``task_terminal``).
    """
    finished: dict[int, tuple[SkillInvocation, Skill]] = {}
    for index, action in enumerate(actions):
        if action.name != INVOKE_SKILL or action.reacts_to is not None:
            continue
        row = rows[index]  # executed: reactions follow every main action
        invocation_id = uuid.UUID(str(row.result["invocationId"]))
        finished[index] = await _finished_invocation(session, ctx, invocation_id, skill_wait)
    return finished


async def run_action(
    session: AsyncSession,
    ctx: AuthContext,
    context: DecisionContext,
    action: Action,
    index: int,
    invocation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inputs = render(action.inputs, lambda path: context.resolve(path, invocation))
    if action.name == ENSURE_WORK:
        return await _ensure_work(session, ctx, context, inputs, index)
    if action.name == COMPLETE_TASK:
        return await _complete(session, ctx, context, inputs)
    if action.name == COMMENT:
        return await _comment(session, ctx, context, inputs)
    if action.name == TRANSITION:
        return await _transition(session, ctx, context, inputs)
    if action.name == INVOKE_SKILL:
        return await _invoke(session, ctx, context, inputs, index)
    raise ValidationError(  # pragma: no cover - the schema parser rejects it first
        "invalid_approval_schema", f"Unknown action {action.name!r}"
    )


def failure_of(exc: DomainError) -> dict[str, Any]:
    # An authorization refusal is reported as `forbidden` whatever its local
    # code: the contract (TAI-ADR-0041 p.4) is "the decider had no right".
    code = "forbidden" if isinstance(exc, AuthorizationError) else exc.code
    return {"code": code, "cause": exc.code, "message": exc.message, "details": exc.details}


# --- the executor --------------------------------------------------------------


async def _referenced_principals(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    context: DecisionContext,
    actions: Sequence[Action],
    rows: dict[int, ApprovalOutcomeAction],
) -> list[uuid.UUID]:
    """The principals the outcome's remaining actions will reference.

    ``ensureWork.assignee`` (an id or an agent of the registry) and
    ``ensureWork.requestApproval.assignee``, rendered as the action will render
    them. An action that reacts to a skill call is rendered without the call's
    result; an input that does not render or does not name a principal is left
    to the action itself, which refuses it the same way.
    """
    found: list[uuid.UUID] = []
    for index, action in enumerate(actions):
        done = rows.get(index)
        if (done is not None and done.status == OUTCOME_EXECUTED) or action.name != ENSURE_WORK:
            continue
        try:
            inputs = render(action.inputs, lambda path: context.resolve(path, None))
        except DomainError as exc:
            # The action itself refuses the same way when it runs.
            logger.debug(
                "outcome action inputs do not render for the pre-lock",
                extra={"action_index": index, "error_code": exc.code},
            )
            continue
        gate = inputs.get(REQUEST_APPROVAL)
        gate_assignee = gate.get("assignee") if isinstance(gate, dict) else None
        for value in (inputs.get("assignee"), gate_assignee):
            if is_agent_reference(value):
                try:
                    found.append(
                        await agent_principal(session, tenant_id, str(value), field="assignee")
                    )
                except DomainError as exc:
                    logger.debug(
                        "outcome assignee names no agent for the pre-lock",
                        extra={"action_index": index, "error_code": exc.code},
                    )
                    continue
            elif value:
                try:
                    found.append(uuid.UUID(str(value)))
                except ValueError:
                    continue
    return found


async def _lock_approval(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    *,
    skip_locked: bool = False,
) -> Approval | None:
    approval: Approval | None = await session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == tenant_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    return approval


async def _action_rows(
    session: AsyncSession, approval_id: uuid.UUID
) -> dict[int, ApprovalOutcomeAction]:
    rows = (
        await session.scalars(
            select(ApprovalOutcomeAction)
            .where(ApprovalOutcomeAction.approval_id == approval_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {row.action_index: row for row in rows}


def _record_row(
    session: AsyncSession,
    rows: dict[int, ApprovalOutcomeAction],
    approval: Approval,
    index: int,
    action: Action,
    *,
    status: str,
    result: dict[str, Any],
    error: dict[str, Any] | None,
) -> ApprovalOutcomeAction:
    now = utcnow()
    row = rows.get(index)
    if row is None:
        row = ApprovalOutcomeAction(
            id=new_uuid(),
            tenant_id=approval.tenant_id,
            approval_id=approval.id,
            action_index=index,
            action=action.name,
            status=status,
            attempts=1,
            result=result,
            error=error,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        rows[index] = row
    else:
        row.status = status
        row.attempts += 1
        row.result = result
        row.error = error
        row.updated_at = now
    return row


def _evidence(rows: dict[int, ApprovalOutcomeAction]) -> list[dict[str, Any]]:
    return [
        {
            "index": row.action_index,
            "action": row.action,
            "status": row.status,
            "attempts": row.attempts,
            "result": row.result,
            **({"error": row.error} if row.error else {}),
        }
        for row in sorted(rows.values(), key=lambda r: r.action_index)
    ]


def _first_open(actions: tuple[Action, ...], rows: dict[int, ApprovalOutcomeAction]) -> int | None:
    """Index of the first action without an ``executed`` row, if any."""
    for index in range(len(actions)):
        row = rows.get(index)
        if row is None or row.status != OUTCOME_EXECUTED:
            return index
    return None


async def _decision_event_id(session: AsyncSession, approval: Approval) -> str | None:
    """The ``approval.approved|rejected`` event: the cause of every outcome event."""
    event_id = await session.scalar(
        select(Event.id)
        .where(
            Event.tenant_id == approval.tenant_id,
            Event.entity_type == "approval",
            Event.entity_id == approval.id,
            Event.event_type.in_(DECIDED_EVENTS),
        )
        .order_by(Event.sequence.desc())
        .limit(1)
    )
    return str(event_id) if event_id is not None else None


async def due_outcomes(session: AsyncSession, *, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """``(tenant_id, approval_id)`` of live outcomes whose next attempt is due."""
    rows = await session.execute(
        select(Approval.tenant_id, Approval.id)
        .where(
            Approval.outcome_status.in_(OUTCOME_LIVE),
            Approval.outcome_next_attempt_at <= utcnow(),
        )
        .order_by(Approval.outcome_next_attempt_at)
        .limit(limit)
    )
    return [(tenant_id, approval_id) for tenant_id, approval_id in rows.all()]


async def execute_outcome(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    trace_run_id: str = "",
    causation_id: str | None = None,
    skip_locked: bool = False,
    defer_seconds: float = DEFAULT_DEFER_SECONDS,
    skill_wait_seconds: float = DEFAULT_SKILL_WAIT_SECONDS,
) -> Approval | None:
    """Run a live outcome to completion, to its first failure or to a claim.

    A no-op for an approval with nothing live — the property that makes a
    repeated attempt harmless. ``skip_locked`` lets several workers scan side
    by side: an approval another transaction holds is simply not ours now.
    An unexpected (non-domain) error propagates; the caller counts it with
    :func:`record_attempt_failure`.
    """
    approval = await _lock_approval(session, tenant_id, approval_id, skip_locked=skip_locked)
    if approval is None or approval.outcome_status not in OUTCOME_LIVE:
        return approval
    outcome = outcome_of(approval)
    assert outcome is not None
    actions = await declared_actions(session, approval, outcome)
    causation_id = causation_id or await _decision_event_id(session, approval)
    # The decider acts within their visibility as it stands now, like a
    # request of theirs (CP-ADR-0082 V2).
    ctx = await with_visibility(
        session, _decider_context(approval, trace_run_id=trace_run_id, causation_id=causation_id)
    )
    # The outcome acts as the decider: its principal before any task row
    # (rule 1 of ``application/locking.py``, CP-ADR-0077 §3). Only the approval
    # row is held so far, and ``principals/{id}:disable`` never takes it.
    await lock_principal_key_share(session, tenant_id, ctx.principal_id)
    rows = await _action_rows(session, approval.id)
    first_open = _first_open(actions, rows)
    context: DecisionContext | None = None

    if first_open is not None:
        # Credential and read rights are checked on every attempt, replay
        # included: a failure here is attributed to the next action to run.
        try:
            await _check_credential(session, approval)
            context = await build_context(session, ctx, approval, outcome, actions)
        except DomainError as exc:
            return await _fail(
                session,
                ctx,
                approval,
                await base_context(session, approval, outcome),
                rows,
                index=first_open,
                action=actions[first_open],
                error=failure_of(exc),
            )
    context = context or await base_context(session, approval, outcome)
    # All actions share this transaction, and the task an earlier one locks
    # (``completeTask``) stays locked until the commit: the principals the
    # later ones will reference go first (rule 3, CP-ADR-0077 §3).
    await lock_principals_key_share(
        session, tenant_id, await _referenced_principals(session, tenant_id, context, actions, rows)
    )
    invocations: dict[int, tuple[SkillInvocation, Skill]] | None = None
    skill_wait = timedelta(seconds=skill_wait_seconds)

    for index, action in enumerate(actions):
        done = rows.get(index)
        if done is not None and done.status == OUTCOME_EXECUTED:
            continue
        try:
            invocation: dict[str, Any] | None = None
            result: dict[str, Any] | None = None
            if action.reacts_to is not None:
                if invocations is None:
                    invocations = await _outcome_invocations(
                        session, ctx, actions, rows, skill_wait
                    )
                called, skill = invocations[action.reacts_to]
                expect = actions[action.reacts_to].inputs.get("expect") or {}
                succeeded = _as_expected(called, expect)
                revoked = _basis_revoked(called)
                if revoked is not None:
                    # Neither branch: nothing was attempted (see BASIS_REVOKED).
                    result = {
                        "skipped": True,
                        "reason": BASIS_REVOKED,
                        "cause": revoked,
                        "invocationId": str(called.id),
                    }
                elif succeeded != (action.when == ON_SUCCESS):
                    # The other branch: the invocation did not end this way.
                    result = {"skipped": True, "invocationId": str(called.id)}
                else:
                    invocation = _invocation_view(called, skill)
            if result is None:
                async with session.begin_nested():
                    result = await run_action(session, ctx, context, action, index, invocation)
                if invocation is not None:
                    result = {**result, "invocationId": invocation["id"]}
        except _Deferred as deferred:
            return await _defer(
                session, ctx, approval, context, index, action, deferred, defer_seconds
            )
        except DomainError as exc:
            return await _fail(
                session,
                ctx,
                approval,
                context,
                rows,
                index=index,
                action=action,
                error=failure_of(exc),
            )
        _record_row(
            session, rows, approval, index, action, status="executed", result=result, error=None
        )
        # Outside the next action's savepoint: its rollback must not take
        # this row with it.
        await session.flush()

    approval.outcome_status = OUTCOME_EXECUTED
    approval.outcome_next_attempt_at = None
    approval.updated_at = utcnow()
    await _record_outcome_event(
        session,
        ctx,
        approval,
        "approval.outcome_executed",
        {"taskId": str(context.task_id), "outcome": outcome, "actions": _evidence(rows)},
    )
    return approval


async def _defer(
    session: AsyncSession,
    ctx: AuthContext,
    approval: Approval,
    context: DecisionContext,
    index: int,
    action: Action,
    deferred: _Deferred,
    defer_seconds: float,
) -> Approval:
    """Park the outcome until the claim on its target is gone (or the skill
    invocation a reaction depends on has finished).

    Nothing is recorded for the action (it did not run) and nothing is
    counted as an attempt; the event is written once, when the outcome
    first starts waiting, not on every re-check.
    """
    await session.refresh(approval)
    was_waiting = approval.outcome_status == OUTCOME_DEFERRED
    approval.outcome_status = OUTCOME_DEFERRED
    approval.outcome_next_attempt_at = utcnow() + timedelta(seconds=defer_seconds)
    approval.updated_at = utcnow()
    if not was_waiting:
        await _record_outcome_event(
            session,
            ctx,
            approval,
            "approval.outcome_deferred",
            {
                "taskId": str(context.task_id),
                "outcome": context.outcome,
                "waitingAction": {"index": index, "action": action.name},
                "reason": deferred.reason,
                "details": deferred.details,
            },
        )
    await session.flush()
    return approval


async def _fail(
    session: AsyncSession,
    ctx: AuthContext,
    approval: Approval,
    context: DecisionContext,
    rows: dict[int, ApprovalOutcomeAction],
    *,
    index: int,
    action: Action,
    error: dict[str, Any],
) -> Approval:
    """Record the failed action, fail the outcome, file work on it, tell the journal."""
    await session.refresh(approval)
    _record_row(session, rows, approval, index, action, status="failed", result={}, error=error)
    approval.outcome_status = OUTCOME_FAILED
    approval.outcome_next_attempt_at = None
    approval.updated_at = utcnow()
    failure_work = await _ensure_failure_work(
        session, ctx, context, index=index, action=action, error=error
    )
    await _record_outcome_event(
        session,
        ctx,
        approval,
        "approval.outcome_failed",
        {
            "taskId": str(context.task_id),
            "outcome": context.outcome,
            "failedAction": {"index": index, "action": action.name, **error},
            "actions": _evidence(rows),
            "failureWorkTaskId": failure_work,
        },
    )
    return approval


async def record_attempt_failure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    error: str,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
    trace_run_id: str = "",
) -> Approval | None:
    """Count an attempt that died of an unexpected error (in its own transaction).

    Retried with backoff like an outbox record; once ``max_attempts`` is
    reached the outcome fails for good — with evidence, a work item and an
    ``approval.outcome_failed`` event — and becomes replayable, instead of
    staying ``pending`` with nobody told.
    """
    approval = await _lock_approval(session, tenant_id, approval_id)
    if approval is None or approval.outcome_status not in OUTCOME_LIVE:
        return approval
    approval.outcome_attempts += 1
    approval.outcome_last_error = error[:2000]
    approval.updated_at = utcnow()
    if approval.outcome_attempts < max_attempts:
        backoff = min(
            backoff_base_seconds * (2 ** (approval.outcome_attempts - 1)), backoff_max_seconds
        )
        approval.outcome_next_attempt_at = utcnow() + timedelta(seconds=backoff)
        await session.flush()
        return approval

    outcome = outcome_of(approval)
    assert outcome is not None
    actions = await declared_actions(session, approval, outcome)
    rows = await _action_rows(session, approval.id)
    index = _first_open(actions, rows)
    assert index is not None  # a live outcome always has an action left
    ctx = _decider_context(
        approval,
        trace_run_id=trace_run_id,
        causation_id=await _decision_event_id(session, approval),
    )
    # The failure work is assigned to the decider (rule 3 of
    # ``application/locking.py``): its principal before any task row.
    await lock_principal_key_share(session, approval.tenant_id, ctx.principal_id)
    return await _fail(
        session,
        ctx,
        approval,
        await base_context(session, approval, outcome),
        rows,
        index=index,
        action=actions[index],
        error={
            "code": ATTEMPTS_EXHAUSTED,
            "cause": "internal_error",
            "message": approval.outcome_last_error or "",
            "details": {"attempts": approval.outcome_attempts},
        },
    )


async def _record_outcome_event(
    session: AsyncSession,
    ctx: AuthContext,
    approval: Approval,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await session.flush()
    await record_event(
        session,
        tenant_id=approval.tenant_id,
        event_type=event_type,
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )


async def _ensure_failure_work(
    session: AsyncSession,
    decider: AuthContext,
    context: DecisionContext,
    *,
    index: int,
    action: Action,
    error: dict[str, Any],
) -> str | None:
    """A work item for the decider to sort the failure out (one per approval).

    Filed by core's own service principal, not by the decider: the typical
    failure is precisely that the decider may not write, and the point of
    this item is that a failed outcome never goes unnoticed. The authority is
    narrowed to ``tasks.write`` and carries no IAM subject, so a policy
    decision point is not asked to vouch for something the decider did not do.
    The decider is the assignee.
    """
    core = await ensure_core_principal(session, decider.tenant_id)
    ctx = AuthContext(
        tenant_id=decider.tenant_id,
        principal_id=core.id,
        principal_kind=core.kind,
        # No credential exists for core; its principal id stands in.
        api_key_id=core.id,
        permissions=frozenset({Permission.TASKS_WRITE.value}),
        request_id=decider.request_id,
        correlation_id=decider.correlation_id,
        causation_id=decider.causation_id,
        trace_run_id=decider.trace_run_id,
    )
    approval_id = _approval_id(context)
    public_id = context.task["publicId"]
    key = f"{FAILURE_KEY_PREFIX}{approval_id}"
    summary = (
        f"Action #{index} ({action.name}) of the '{context.outcome}' outcome of approval "
        f"{approval_id} on {public_id} failed: {error['code']}: {error['message']}"
    )
    try:
        async with session.begin_nested():
            existing = await _origin(session, ctx.tenant_id, key)
            if existing is not None:
                await add_comment(session, ctx, task_ref=str(existing.id), body=summary)
                return str(existing.id)
            task = await create_task(
                session,
                ctx,
                title=f"Approval outcome failed on {public_id}: {action.name}"[:500],
                description=(
                    f"{summary}\n\nThe decision itself stands; the remaining actions of the "
                    "outcome were not executed. Fix the cause (rights, lifecycle, missing type) "
                    f"and resume with POST /api/v1/approvals/{approval_id}:replay-outcome."
                ),
                assignee_id=context.decided_by,
                workspace_id=context.workspace_id,
            )
            await add_relation(
                session,
                ctx,
                from_task_ref=str(task.id),
                to_task_ref=str(context.task_id),
                relation_type=TaskRelationType.RELATED_TO,
            )
            await _mark_origin(
                session,
                ctx,
                task=task,
                key=key,
                metadata={
                    "origin": "approval",
                    "approvalId": str(approval_id),
                    "actionIndex": None,
                },
            )
            return str(task.id)
    except DomainError:
        # The failure is still recorded on the approval and in the journal;
        # a missing follow-up item must not turn into a retry loop.
        return None


# --- replay and read model ------------------------------------------------------


def replayable(approval: Approval) -> bool:
    """Failed, or pending and stuck: retried in vain, or untouched for too long."""
    if approval.outcome_status == OUTCOME_FAILED:
        return True
    if approval.outcome_status != OUTCOME_PENDING:
        return False
    if approval.outcome_attempts > 0:
        return True
    due = approval.outcome_next_attempt_at
    return due is None or due <= utcnow() - STALE_PENDING


async def replay_outcome(
    session: AsyncSession, ctx: AuthContext, *, approval_id: uuid.UUID
) -> Approval:
    """Resume a failed (or stuck pending) outcome at its first open action.

    The decider may replay (and their CURRENT credential becomes the authority
    — the usual fix for ``forbidden`` is granting the right); an admin may
    replay on the decider's behalf with the authority recorded at decision,
    which is still checked to be active.
    """
    await authorize(ctx, Permission.APPROVALS_DECIDE)
    approval = await _lock_approval(session, ctx.tenant_id, approval_id)
    if approval is None or not await approval_visible(session, ctx, approval):
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    if not replayable(approval):
        raise ConflictError(
            "outcome_not_replayable",
            "Only a failed approval outcome, or a pending one that is stuck, can be replayed",
            details={"approvalId": str(approval.id), "outcomeStatus": approval.outcome_status},
        )
    if approval.decision_by_principal_id == ctx.principal_id:
        approval.decision_authority = authority_snapshot(ctx)
    elif not ctx.has(Permission.ADMIN):
        raise AuthorizationError(
            "Only the decider or an admin may replay an approval outcome",
            code="not_eligible",
            details={"approvalId": str(approval.id)},
        )
    approval.outcome_status = OUTCOME_PENDING
    approval.outcome_attempts = 0
    approval.outcome_last_error = None
    approval.outcome_next_attempt_at = utcnow()
    approval.updated_at = utcnow()
    await session.flush()
    replayed = await execute_outcome(
        session,
        tenant_id=ctx.tenant_id,
        approval_id=approval.id,
        trace_run_id=ctx.trace_run_id,
        causation_id=ctx.request_id,
    )
    assert replayed is not None
    return replayed


@dataclass(frozen=True)
class OutcomeView:
    approval: Approval
    outcome: str | None
    actions: list[dict[str, Any]]


async def outcome_view(session: AsyncSession, approval: Approval) -> OutcomeView:
    """Declared actions of the decision, each with what happened to it."""
    outcome = outcome_of(approval)
    declared = await declared_actions(session, approval, outcome) if outcome else ()
    rows = await _action_rows(session, approval.id)
    actions: list[dict[str, Any]] = []
    for index, action in enumerate(declared):
        row = rows.get(index)
        actions.append(
            {
                "index": index,
                "action": action.name,
                "status": row.status if row else "not_executed",
                "attempts": row.attempts if row else 0,
                "result": row.result if row else {},
                "error": row.error if row else None,
                "reactsTo": action.reacts_to,
                "when": action.when,
            }
        )
    return OutcomeView(approval=approval, outcome=outcome, actions=actions)
