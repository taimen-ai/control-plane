"""Approval commands: minimal human/agent-in-the-loop governance primitive.

One approval record = one decision. The decision is atomic: the row is locked
``FOR UPDATE`` and only a ``pending`` approval can transition, so two
concurrent deciders produce exactly one terminal outcome.

Deciding requires BOTH: the ``approvals.decide`` API permission AND
organizational eligibility (being the assigned principal, or holding the
required role in the approval's workspace scope) — and not being one of the
approval's excluded principals (separation of duties, CP-ADR-0074 §7), which
is refused here, on the one decision path every entry point goes through.
The same holds for cancelling such an approval, and for an agent acting under
a delegation from an excluded principal.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.approval_outcomes import (
    OUTCOME_PENDING,
    decision_authority,
    declared_actions,
)
from control_plane.application.commands.approval_preconditions import require_preconditions
from control_plane.application.commands.delegations import find_delegation_from_any
from control_plane.application.commands.org import get_tenant_role
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.tasks import resolve_task_for_update
from control_plane.application.commands.verification import wake_on_decision
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principals_key_share
from control_plane.application.queries.approval_gates import require_open_gates
from control_plane.application.queries.org import role_assignment_scope
from control_plane.application.visibility import approval_visible, artifact_visible
from control_plane.domain.enums import ApprovalStatus, Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.event_catalog import PAYLOAD_TEXT_LIMIT
from control_plane.domain.redaction import redact_secret_material
from control_plane.domain.work_item import TERMINAL_CATEGORIES
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    Principal,
    PrincipalRole,
    Task,
)


def event_comment(comment: str | None) -> str | None:
    """A comment as it may travel in an event (CP-ADR-0068): credential-shaped
    material redacted, cut to the payload text limit."""
    if comment is None:
        return None
    text = redact_secret_material(comment)
    if len(text) > PAYLOAD_TEXT_LIMIT:
        text = text[: PAYLOAD_TEXT_LIMIT - 1] + "\u2026"
    return text


async def request_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str | None = None,
    artifact_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    required_role_id: uuid.UUID | None = None,
    assigned_principal_id: uuid.UUID | None = None,
    comment: str = "",
    gate: bool = False,
    excluded_principals: Sequence[uuid.UUID] = (),
) -> Approval:
    await authorize(ctx, Permission.APPROVALS_MANAGE)
    if (required_role_id is None) == (assigned_principal_id is None):
        raise ValidationError(
            "invalid_approval",
            "Exactly one of requiredRoleId or assignedPrincipalId must be set",
        )
    excluded = list(dict.fromkeys(excluded_principals))
    if assigned_principal_id is not None and assigned_principal_id in excluded:
        raise ValidationError(
            "invalid_approval",
            "The assigned principal is excluded from deciding: nobody could decide",
            details={"assignedPrincipalId": str(assigned_principal_id)},
        )
    if gate and task_ref is None:
        raise ValidationError(
            "invalid_approval",
            "A gate approval must reference the task it gates",
        )

    task_id: uuid.UUID | None = None
    task: Task | None = None
    # The assignee the approval will reference, before the task (rule 3 of
    # ``application/locking.py``, CP-ADR-0077 §3): ``:disable`` of the
    # assignee holds it and may wait for this task. For a gate the task is
    # locked; without one it is only read, but the insert's foreign-key check
    # on the task and on the assignee must not be left to the order of the
    # referential triggers. The requester is the caller, locked by the write
    # flow (rule 1).
    await lock_principals_key_share(session, ctx.tenant_id, [assigned_principal_id])
    if task_ref is not None:
        # A gate must serialize with the commands it gates: taking the task
        # row lock (the first row lock after the principals; no session, claim
        # or run is locked here) means an in-flight :complete either finishes
        # before the gate exists or blocks until it does — no gate can attach
        # to a task that is concurrently becoming terminal.
        task = (
            await resolve_task_for_update(session, ctx, task_ref)
            if gate
            else (await resolve_task(session, ctx, task_ref))
        )
        task_id = task.id
        # A gate on a terminal task is inert (done/cancelled cannot be
        # claimed or completed) and only muddies discovery/audit — reject it.
        if gate and task.system_status_category in TERMINAL_CATEGORIES:
            raise ValidationError(
                "invalid_approval",
                f"Cannot gate a task in status '{task.status}'",
                details={"taskId": str(task_id), "status": task.status},
            )
    if artifact_id is not None:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
        )
        if artifact is None or not await artifact_visible(session, ctx, artifact):
            raise NotFoundError("Artifact not found", details={"artifactId": str(artifact_id)})
    if workspace_id is not None:
        await get_tenant_workspace(session, ctx, workspace_id)
    if task is not None:
        workspace_id = await _task_approval_workspace(session, ctx, task, workspace_id)
    if required_role_id is not None:
        await get_tenant_role(session, ctx, required_role_id)
    if assigned_principal_id is not None:
        await get_tenant_principal(session, ctx, assigned_principal_id)
    if excluded:
        known = set(
            await session.scalars(
                select(Principal.id).where(
                    Principal.id.in_(excluded), Principal.tenant_id == ctx.tenant_id
                )
            )
        )
        missing = [principal_id for principal_id in excluded if principal_id not in known]
        if missing:
            raise NotFoundError(
                "Excluded principal not found", details={"principalId": str(missing[0])}
            )

    now = utcnow()
    approval = Approval(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        task_id=task_id,
        artifact_id=artifact_id,
        requested_by_principal_id=ctx.principal_id,
        status=ApprovalStatus.PENDING,
        gate=gate,
        required_role_id=required_role_id,
        assigned_principal_id=assigned_principal_id,
        comment=comment,
        version=1,
        excluded_principals=[str(principal_id) for principal_id in excluded],
        created_at=now,
        updated_at=now,
    )
    session.add(approval)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.requested",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task_id) if task_id else None,
            "artifactId": str(artifact_id) if artifact_id else None,
            "requiredRoleId": str(required_role_id) if required_role_id else None,
            "assignedPrincipalId": str(assigned_principal_id) if assigned_principal_id else None,
            "gate": gate,
            # v2 (CP-ADR-0068): enough to tell a person what to decide without a read.
            # The approval's own workspace: the one its eligibility is checked in.
            "workspaceId": str(workspace_id) if workspace_id else None,
            "taskPublicId": task.public_id if task is not None else None,
            "taskTitle": task.title if task is not None else None,
            "requestedBy": str(ctx.principal_id),
            "comment": event_comment(comment),
            # v3 (CP-ADR-0074 §7): whose decision the core refuses.
            "excludedPrincipals": approval.excluded_principals,
        },
    )
    return approval


async def _task_approval_workspace(
    session: AsyncSession, ctx: AuthContext, task: Task, workspace_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Workspace of an approval about ``task`` (CP-ADR-0068).

    Without an explicit one the approval lives in the task's workspace, so a
    role granted there is enough to decide. An explicit one may only widen the
    scope: the task's workspace itself or one of its ancestors.
    """
    if workspace_id is None or workspace_id == task.workspace_id:
        return task.workspace_id
    ancestors = (
        await workspace_ancestor_ids(session, ctx.tenant_id, task.workspace_id)
        if task.workspace_id is not None
        else []
    )
    if workspace_id not in ancestors:
        raise ValidationError(
            "invalid_approval",
            "The approval's workspace must be the task's workspace or one of its ancestors",
            details={
                "taskId": str(task.id),
                "taskWorkspaceId": str(task.workspace_id) if task.workspace_id else None,
                "workspaceId": str(workspace_id),
            },
        )
    return workspace_id


async def check_approval_gate(session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID) -> None:
    """Raise 409 approval_required while a pending gate approval holds the task.

    Called inside the claiming/completing transaction under the task row lock:
    an uncommitted approval decision is invisible here, so the gate opens only
    after the decision has committed.
    """
    await require_open_gates(session, ctx, task_id)


async def _get_locked_pending_approval(
    session: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID
) -> Approval:
    approval = await session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # An invisible approval is a missing one: neither decided nor told apart
    # by its state (CP-ADR-0082 §3.7, FR-007).
    if approval is None or not await approval_visible(session, ctx, approval):
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    if approval.status != ApprovalStatus.PENDING:
        raise ConflictError(
            "approval_already_decided",
            "Approval is no longer pending",
            details={"approvalId": str(approval_id), "status": approval.status},
        )
    return approval


async def _require_not_excluded(
    session: AsyncSession, ctx: AuthContext, approval: Approval
) -> None:
    """Separation of duties (CP-ADR-0074 §7): an excluded principal, or an agent
    an excluded principal delegated to, neither decides nor cancels the approval
    — whoever the request comes through (engine, console, channel, MCP) and
    whatever role is held."""
    if not approval.excluded_principals:
        return
    if str(ctx.principal_id) in approval.excluded_principals:
        raise AuthorizationError(
            "This principal is excluded from deciding the approval (separation of duties)",
            code="separation_of_duties_violation",
            details={"approvalId": str(approval.id)},
        )
    delegation = await find_delegation_from_any(
        session,
        tenant_id=ctx.tenant_id,
        human_principal_ids=[uuid.UUID(p) for p in approval.excluded_principals],
        agent_principal_id=ctx.principal_id,
    )
    if delegation is not None:
        # The agent acts for whoever delegated to it: an excluded principal's
        # agent is excluded too, whichever session it decides through.
        raise AuthorizationError(
            "This agent acts on behalf of a principal excluded from deciding the approval"
            " (separation of duties)",
            code="separation_of_duties_violation",
            details={"approvalId": str(approval.id), "delegationId": str(delegation.id)},
        )


async def _require_decision_eligibility(
    session: AsyncSession, ctx: AuthContext, approval: Approval
) -> None:
    """Organizational check: not excluded, and the assigned principal or a
    holder of the required role."""
    await _require_not_excluded(session, ctx, approval)
    if approval.assigned_principal_id is not None:
        if approval.assigned_principal_id != ctx.principal_id:
            raise AuthorizationError(
                "Approval is assigned to another principal",
                code="not_eligible",
                details={"approvalId": str(approval.id)},
            )
        return

    scope_filter = await role_assignment_scope(session, ctx.tenant_id, approval.workspace_id)
    held = await session.scalar(
        select(PrincipalRole.id).where(
            PrincipalRole.principal_id == ctx.principal_id,
            PrincipalRole.role_id == approval.required_role_id,
            scope_filter,
        )
    )
    if held is None:
        raise AuthorizationError(
            "Deciding requires the approval's required role",
            code="not_eligible",
            details={
                "approvalId": str(approval.id),
                "requiredRoleId": str(approval.required_role_id),
            },
        )


async def decision_gate(
    session: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID, *, for_decision: bool
) -> Approval:
    """Everything that decides whether ``ctx`` may approve or reject.

    The decision itself goes through here with ``for_decision`` (the row locked,
    refused unless pending); ``POST /authz:check`` asks the same question
    without either (CP-ADR-0055, amendment of 2026-09-29).
    """
    target = ResourceRef("approval", str(approval_id))
    if ctx.purpose_ref is not None and ctx.purpose_ref != target.key:
        # Checked before the row is read: a credential bound to one decision
        # learns nothing about any other approval (CP-ADR-0070).
        raise AuthorizationError(
            "This credential decides only the approval it was issued for",
            code="outside_purpose",
        )
    await authorize(ctx, Permission.APPROVALS_DECIDE)
    if for_decision:
        approval = await _get_locked_pending_approval(session, ctx, approval_id)
    else:
        found = await session.scalar(
            select(Approval).where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
        )
        if found is None or not await approval_visible(session, ctx, found):
            raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
        approval = found
    await authorize(ctx, Permission.APPROVALS_DECIDE, resource=target)
    await _require_decision_eligibility(session, ctx, approval)
    return approval


async def decide_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    approval_id: uuid.UUID,
    approve: bool,
    comment: str | None = None,
) -> Approval:
    approval = await decision_gate(session, ctx, approval_id, for_decision=True)
    # What the type declares must hold before an approve (TAI-ADR-0041 p.7):
    # refused, the decision is not recorded and the gate stays pending.
    if approve:
        await require_preconditions(session, ctx, approval)

    now = utcnow()
    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    approval.decision_by_principal_id = ctx.principal_id
    approval.decision_at = now
    if comment is not None:
        approval.comment = comment
    # A gate on a task whose type declares outcomes for this decision: the
    # worker executes them after commit, with the authority of THIS credential
    # (CP-ADR-0061), or of its binding for a channel decision (CP-ADR-0070).
    # Otherwise nothing is pending — as before that ADR.
    if await declared_actions(session, approval):
        approval.outcome_status = OUTCOME_PENDING
        approval.decision_authority = await decision_authority(session, ctx)
        approval.outcome_next_attempt_at = now
    elif approval.gate and approval.task_id is not None:
        # A gate of a task may be the basis of an acceptance check's external
        # write, made with the decider's authority (CP-ADR-0067, amendment
        # 2026-09-27, B7): the snapshot is taken now, as for an outcome.
        approval.decision_authority = await decision_authority(session, ctx)
    approval.version += 1
    approval.updated_at = now
    # A verification attempt waiting on this gate looks at it now (CP-ADR-0067).
    await wake_on_decision(session, approval)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.approved" if approve else "approval.rejected",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(approval.task_id) if approval.task_id else None,
            "artifactId": str(approval.artifact_id) if approval.artifact_id else None,
            "outcomeStatus": approval.outcome_status,
            "decisionBy": str(ctx.principal_id),
            "comment": event_comment(comment),
            # The credential's channel of entry (CP-ADR-0070); a direct API
            # call has none.
            "channel": ctx.channel,
        },
    )
    return approval


async def cancel_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    approval_id: uuid.UUID,
    comment: str | None = None,
) -> Approval:
    await authorize(ctx, Permission.APPROVALS_MANAGE)
    approval = await session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval is None or not await approval_visible(session, ctx, approval):
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    if approval.status == ApprovalStatus.CANCELLED:
        return approval  # idempotent
    if approval.status != ApprovalStatus.PENDING:
        raise ConflictError(
            "approval_already_decided",
            "Approval is no longer pending",
            details={"approvalId": str(approval_id), "status": approval.status},
        )
    # A GATE approval is an enforcement primitive (blocks claim/complete):
    # cancelling it opens the gate exactly like a decision. So cancelling a
    # foreign gate requires the FULL authority to decide it — both the
    # approvals.decide permission and organizational eligibility — otherwise
    # the very principal the gate is meant to hold could void it with only
    # approvals.manage. The requester may always cancel its own request.
    #
    # An approval with excluded principals is the same kind of primitive: a
    # process step counts a cancelled approval as one approver fewer
    # (CP-ADR-0074 §7), so a foreign cancel is a decision too, and an
    # excluded principal may not cancel it at all — its own request included
    # (the process that asked for a step's approvals closes them itself).
    if approval.requested_by_principal_id != ctx.principal_id:
        await _require_not_excluded(session, ctx, approval)
        if approval.gate or approval.excluded_principals:
            await authorize(ctx, Permission.APPROVALS_DECIDE)
            await _require_decision_eligibility(session, ctx, approval)
    elif str(ctx.principal_id) in approval.excluded_principals:
        await _require_not_excluded(session, ctx, approval)

    now = utcnow()
    approval.status = ApprovalStatus.CANCELLED
    if comment is not None:
        approval.comment = comment
    approval.version += 1
    approval.updated_at = now
    await wake_on_decision(session, approval)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.cancelled",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(approval.task_id) if approval.task_id else None,
            "cancelledBy": str(ctx.principal_id),
        },
    )
    return approval
