"""Work discovery: which tasks could this principal claim right now? (v0.3)

Advisory only. The query re-derives claimability from authoritative state
(status, live claim, readiness, required inputs, approval gate, organizational
eligibility), but the CLAIM remains the authoritative gate: between discovery
and claim the world may change, and claim re-checks every invariant in its own
transaction.
"shown as available" is never a promise that claim will succeed.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.eligibility import explain_claim_eligibility
from control_plane.application.commands.relations import resolve_task, shown_prerequisites
from control_plane.application.commands.task_inputs import missing_required_inputs
from control_plane.application.commands.task_types import task_type_lifecycle, task_type_of
from control_plane.application.commands.verification import OPEN_STATUSES, open_attempt
from control_plane.application.commands.workspaces import workspace_subtree_ids
from control_plane.application.common import (
    decode_cursor,
    encode_cursor,
    utcnow,
)
from control_plane.application.queries import projects as project_queries
from control_plane.application.queries.approval_gates import gate_reason, shown_gate_approvals
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.visibility import workspace_condition
from control_plane.domain.enums import (
    ApprovalStatus,
    ClaimStatus,
    Permission,
    SessionStatus,
)
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.work_item import (
    TERMINAL_CATEGORIES,
    WorkItemStatusCategory,
    transition_targets,
)
from control_plane.infrastructure.db.models import (
    Approval,
    Session,
    Task,
    TaskClaim,
    TaskRelation,
    TaskType,
    TaskVerification,
)

_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Candidates fetched per page before the eligibility post-filter. A page of
# available work may contain FEWER than `limit` items while still carrying a
# nextCursor — clients keep paging until nextCursor is null.
_SCAN_FACTOR = 3

#: How many ``typeKey`` values one listing may carry.
MAX_TYPE_KEYS = 50


def _priority_rank_expr() -> Any:
    return case(_PRIORITY_RANK, value=Task.priority, else_=4)


def _candidate_statement(
    tenant_id: uuid.UUID,
    workspace_ids: list[uuid.UUID] | None,
    excluded_workspace_ids: list[uuid.UUID] | None = None,
) -> Select[tuple[Task]]:
    """Tasks that are structurally claimable (everything except eligibility)."""
    now = utcnow()
    live_claim = (
        select(TaskClaim.id)
        .join(Session, Session.id == TaskClaim.session_id)
        .where(
            TaskClaim.id == Task.active_claim_id,
            TaskClaim.status == ClaimStatus.ACTIVE,
            TaskClaim.expires_at > now,
            Session.status == SessionStatus.ACTIVE,
            Session.expires_at > now,
        )
    )
    # Unmet prerequisite: dependency targets / blockers that are not done.
    prereq_task = aliased(Task)
    prereq_id = case(
        (
            (TaskRelation.relation_type == "depends_on") & (TaskRelation.from_task_id == Task.id),
            TaskRelation.to_task_id,
        ),
        (
            (TaskRelation.relation_type == "blocks") & (TaskRelation.to_task_id == Task.id),
            TaskRelation.from_task_id,
        ),
    )
    prereq = (
        select(TaskRelation.id)
        .join(prereq_task, prereq_task.id == prereq_id)
        .where(
            TaskRelation.tenant_id == tenant_id,
            ((TaskRelation.relation_type == "depends_on") & (TaskRelation.from_task_id == Task.id))
            | ((TaskRelation.relation_type == "blocks") & (TaskRelation.to_task_id == Task.id)),
            prereq_task.system_status_category != WorkItemStatusCategory.TERMINAL_SUCCESS,
        )
    )
    gate = select(Approval.id).where(
        Approval.task_id == Task.id,
        Approval.gate.is_(True),
        Approval.status == ApprovalStatus.PENDING,
    )
    verifying = select(TaskVerification.id).where(
        TaskVerification.task_id == Task.id,
        TaskVerification.status.in_(OPEN_STATUSES),
    )
    stmt = select(Task).where(
        Task.tenant_id == tenant_id,
        Task.system_status_category.notin_(sorted(TERMINAL_CATEGORIES)),
        ~exists(live_claim),
        ~exists(prereq),
        ~exists(gate),
        ~exists(verifying),
    )
    if workspace_ids is not None:
        stmt = stmt.where(Task.workspace_id.in_(workspace_ids))
    if excluded_workspace_ids:
        # An archived project stops offering NEW work; claims and runs already
        # in flight are unaffected — the claim remains the authoritative gate.
        stmt = stmt.where(
            Task.workspace_id.is_(None) | Task.workspace_id.notin_(excluded_workspace_ids)
        )
    return stmt


def _work_cursor(task: Task) -> str:
    return encode_cursor(
        {
            "p": _PRIORITY_RANK.get(task.priority, 4),
            "c": task.created_at.isoformat(),
            "i": str(task.id),
        }
    )


async def list_available_work(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    workspace_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    project_id: uuid.UUID | None = None,
    include_subprojects: bool = False,
    assignee_id: uuid.UUID | None = None,
    type_keys: list[str] | None = None,
) -> tuple[list[Task], str | None]:
    """Tasks the calling principal could claim now (advisory, priority-first).

    Stable ordering: (priority rank, created_at, id) ascending — urgent first,
    oldest first within a priority. Returns (tasks, next_cursor); a page may
    be shorter than `limit` while next_cursor is not None (post-filtering).

    ``assignee_id`` narrows the queue to work addressed to someone. Claimability
    and assignment are different questions — a task may be claimable by anyone
    and still be meant for one worker — so this is a filter and not part of
    eligibility. An autonomous runner uses it to take only what was handed to
    it, rather than whatever happens to be at the top of the queue.

    ``type_keys`` narrows the queue to tasks of these types, any version: an
    executor that runs only some types must not page through everyone else's
    work to find its own (CP-ADR-0056, amendment 2026-10-03).
    """
    await authorize(ctx, Permission.TASKS_READ)
    effective_limit = clamp_limit(limit)
    if type_keys is not None and (
        len(type_keys) > MAX_TYPE_KEYS or any(not key.strip() for key in type_keys)
    ):
        raise ValidationError(
            "invalid_type_key",
            f"typeKey must be non-blank, at most {MAX_TYPE_KEYS} values",
            details={"count": len(type_keys)},
        )

    workspace_ids: list[uuid.UUID] | None = None
    if workspace_id is not None:
        if include_descendants:
            workspace_ids = await workspace_subtree_ids(session, ctx.tenant_id, workspace_id)
            # An invisible workspace answers as a missing one (CP-ADR-0082 §3.7).
            if not workspace_ids or not ctx.sees_workspace(workspace_id):
                raise NotFoundError(
                    "Workspace not found", details={"workspaceId": str(workspace_id)}
                )
        else:
            workspace_ids = [workspace_id]

    if project_id is not None:
        project = await project_queries.get_tenant_project(session, ctx, project_id)
        scope = await project_queries.project_scope_workspace_ids(
            session, ctx.tenant_id, project, include_subprojects=include_subprojects
        )
        workspace_ids = (
            scope if workspace_ids is None else [w for w in workspace_ids if w in set(scope)]
        )

    excluded: list[uuid.UUID] = []
    if await project_queries.has_archived_projects(session, ctx.tenant_id):
        excluded = await project_queries.archived_project_workspace_ids(session, ctx.tenant_id)

    # Work of the caller's visible workspaces only, as GET /tasks (CP-ADR-0082 §4).
    stmt = _candidate_statement(ctx.tenant_id, workspace_ids, excluded).where(
        workspace_condition(ctx, Task.workspace_id)
    )
    if assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    if type_keys is not None:
        stmt = stmt.where(
            Task.type_id.in_(
                select(TaskType.id).where(
                    TaskType.tenant_id == ctx.tenant_id, TaskType.key.in_(sorted(set(type_keys)))
                )
            )
        )
    rank = _priority_rank_expr()
    if cursor is not None:
        data = decode_cursor(cursor)
        try:
            after_rank = int(data["p"])
            after_created = datetime.fromisoformat(data["c"])
            after_id = uuid.UUID(data["i"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
        stmt = stmt.where(
            tuple_(rank, Task.created_at, Task.id) > (after_rank, after_created, after_id)
        )
    stmt = stmt.order_by(rank.asc(), Task.created_at.asc(), Task.id.asc())

    scan_limit = effective_limit * _SCAN_FACTOR
    candidates = list((await session.scalars(stmt.limit(scan_limit + 1))).all())
    more_candidates = len(candidates) > scan_limit
    candidates = candidates[:scan_limit]

    eligible: list[Task] = []
    for task in candidates:
        missing = await explain_claim_eligibility(session, ctx, task, ctx.principal_id)
        # A task without a required input would only be refused at claim
        # (CP-ADR-0072 §8): a runner must not spin on it.
        if missing is None and not await missing_required_inputs(session, ctx.tenant_id, task):
            eligible.append(task)
            if len(eligible) >= effective_limit:
                break

    if len(eligible) >= effective_limit:
        next_cursor = _work_cursor(eligible[-1])
    elif more_candidates:
        next_cursor = _work_cursor(candidates[-1])
    else:
        next_cursor = None
    return eligible, next_cursor


async def explain_task_transitions(
    session: AsyncSession, ctx: AuthContext, task_ref: str
) -> dict[str, Any]:
    """Where this task may move next, per the lifecycle of the type it carries.

    Read-only projection of the SAME rules the update and completion paths
    enforce (ADR-0048): a caller must not have to discover the tenant's status
    vocabulary from a 422. ``route`` says which action walks the edge —
    ``update`` for PATCH, ``complete`` for the ``:complete`` action.
    """
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    task_type = await task_type_of(session, task)
    lifecycle = task_type_lifecycle(task_type)
    return {
        "taskId": str(task.id),
        "publicId": task.public_id,
        "typeId": str(task_type.id),
        "typeKey": task_type.key,
        "typeVersion": task_type.version,
        "status": task.status,
        "systemStatusCategory": task.system_status_category,
        "targets": [
            {
                "status": target.status,
                "displayName": target.display_name,
                "systemStatusCategory": target.category,
                "route": target.route.value,
            }
            for target in transition_targets(lifecycle, task.status)
        ],
    }


async def explain_task_claimability(
    session: AsyncSession, ctx: AuthContext, task_ref: str
) -> dict[str, Any]:
    """Full diagnosis: can the calling principal claim this task, and if not, why.

    Advisory (no locks) — the claim itself remains the authoritative gate.
    """
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    now = utcnow()
    reasons: list[dict[str, Any]] = []

    if task.system_status_category in TERMINAL_CATEGORIES:
        reasons.append(
            {
                "code": "task_not_claimable",
                "status": task.status,
                "systemStatusCategory": task.system_status_category,
            }
        )

    if task.active_claim_id is not None:
        claim = await session.get(TaskClaim, task.active_claim_id)
        if claim is not None and claim.status == ClaimStatus.ACTIVE and claim.expires_at > now:
            holder_session = await session.get(Session, claim.session_id)
            if (
                holder_session is not None
                and holder_session.status == SessionStatus.ACTIVE
                and holder_session.expires_at > now
            ):
                reasons.append(
                    {
                        "code": "task_already_claimed",
                        "claimId": str(claim.id),
                        "holderId": str(claim.holder_id),
                        "expiresAt": claim.expires_at.isoformat(),
                    }
                )

    blocking, hidden = await shown_prerequisites(session, ctx, task.id)
    if blocking or hidden:
        reason: dict[str, Any] = {"code": "task_not_ready", "blockedBy": blocking}
        if hidden:
            # Blockers outside the caller's visibility, counted, not named
            # (CP-ADR-0082 V4).
            reason["hiddenBlockers"] = hidden
        reasons.append(reason)

    missing_inputs = await missing_required_inputs(session, ctx.tenant_id, task)
    if missing_inputs:
        reasons.append({"code": "input_missing", "missing": missing_inputs})

    gates, hidden_gates = await shown_gate_approvals(session, ctx, task.id)
    if gates or hidden_gates:
        reasons.append({"code": "approval_required", **gate_reason(gates, hidden_gates)})

    verification = await open_attempt(session, task.id)
    if verification is not None:
        reasons.append(
            {
                "code": "verification_pending",
                "verificationId": str(verification.id),
                "status": verification.status,
            }
        )

    missing = await explain_claim_eligibility(session, ctx, task, ctx.principal_id)
    if missing is not None:
        reasons.append({"code": "not_eligible", **missing})

    return {
        "taskId": str(task.id),
        "publicId": task.public_id,
        "claimable": not reasons,
        "reasons": reasons,
    }
