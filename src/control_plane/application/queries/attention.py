"""What needs the calling principal now: ``GET /me/attention`` (CP-ADR-0071).

The list is computed on every read from the core's own entities — approvals,
tasks, runs — by a fixed set of rules; nothing about it is stored except the
principal's feedback on an item. Each rule is named ``ruleKey@version``: a
change of what a rule selects or how it scores is a new version, so feedback
given to one version is never mixed with the next.

Rules (CP-ADR-0071 §2):

* ``approval.decide`` — a pending non-gate approval the principal may decide:
  assigned to it, or requiring a role it holds in the approval's scope.
* ``approval.review`` — the same for a gate approval: the work is waiting on
  a check ("on review") and cannot move until it is decided.
* ``approval.undecidable`` — a pending approval nobody may decide: every
  holder of its required role in scope is excluded from deciding it
  (separation of duties, CP-ADR-0074 §7). Raised to the owner of the process
  whose step asked for it, else to whoever requested it.
* ``task.due_not_started`` — the principal's task (assignee, else owner) is
  due within 48 hours or overdue and nobody has started it: no active claim,
  no run ever.
* ``task.blocked`` — a task the principal owns or is assigned to sits in a
  status of the ``blocked`` category.
* ``task.delegated_failing`` — work the principal handed to someone else
  (owner, else creator; assignee is another principal) failed its last three
  runs or more in a row.

Items are addressed by ``itemKey = <ruleKey>:<entityId>``: stable for as long
as the rule keeps raising the same object, independent of the rule version.

"Mine" is decided by the rule itself (addressee, owner, assignee, creator,
role holder) — exactly the relations the PDP model derives read access from
(authz/catalog.yaml), so the list shows no object the principal could not
read. Rules run independently, each in its own savepoint: a rule that fails,
or whose read permission the credential lacks, is reported in ``degraded``
and the others still answer. Only domain-neutral fields are read — category,
dates, addressing, run status — never a package's statuses or fields.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, cast, exists, func, or_, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import observability
from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.common import utcnow
from control_plane.application.queries.org import role_assignment_scope
from control_plane.application.visibility import approval_condition, workspace_condition
from control_plane.domain.enums import ApprovalStatus, Permission, RunStatus
from control_plane.domain.errors import (
    AuthorizationError,
    DependencyUnavailableError,
    NotFoundError,
)
from control_plane.domain.work_item import WorkItemStatusCategory
from control_plane.infrastructure.db.models import (
    Approval,
    AttentionFeedback,
    PrincipalRole,
    ProcessInstance,
    Run,
    Task,
)

logger = logging.getLogger("control_plane.attention")

# How far ahead a due date counts as "soon" (plan, "Research" p.4).
DUE_SOON = timedelta(hours=48)
# Failed runs in a row after which delegated work needs its owner.
FAILED_RUNS_THRESHOLD = 3
# Rows one rule contributes at most; more is reported as ``truncated``.
RULE_LIMIT = 100
# Approvals nobody may decide that ``approval.undecidable`` reads per call.
UNDECIDABLE_SCAN = 500

KIND_DECISION = "decision"
KIND_REVIEW = "review"
KIND_UNDECIDABLE = "undecidable"
KIND_DEADLINE = "deadline"
KIND_BLOCKED = "blocked"
KIND_DELEGATED_FAILURE = "delegated_failure"

DEGRADED_PERMISSION = "permission_missing"
DEGRADED_POLICY = "policy_unavailable"
DEGRADED_FAILED = "rule_failed"
DEGRADED_TRUNCATED = "truncated"

_OPEN_CATEGORIES = (WorkItemStatusCategory.BACKLOG.value, WorkItemStatusCategory.ACTIVE.value)
_PRIORITY_BONUS = {"critical": 10, "high": 5, "medium": 0, "low": -5}


@dataclass(frozen=True)
class AttentionItem:
    rule_key: str
    rule_version: int
    kind: str
    reason_code: str
    entity_type: str
    entity_id: uuid.UUID
    score: int
    title: str
    workspace_id: uuid.UUID | None
    # Since when the object needs the principal (request, due date, status change).
    since: datetime
    task_id: uuid.UUID | None = None
    task_public_id: str | None = None
    due_date: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def item_key(self) -> str:
        return item_key(self.rule_key, self.entity_id)

    @property
    def rule(self) -> str:
        return f"{self.rule_key}@{self.rule_version}"


@dataclass(frozen=True)
class Degraded:
    rule: str
    reason_code: str
    message: str


@dataclass(frozen=True)
class Scope:
    """Who asks and, optionally, the workspaces — or the one object — the
    answer is limited to."""

    ctx: AuthContext
    now: datetime
    workspaces: list[uuid.UUID] | None = None
    entity_id: uuid.UUID | None = None


RuleFetch = Callable[[AsyncSession, Scope, "AttentionRule", int], Awaitable[list[AttentionItem]]]


@dataclass(frozen=True)
class AttentionRule:
    key: str
    version: int
    permission: Permission
    fetch: RuleFetch

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.version}"


@dataclass(frozen=True)
class AttentionList:
    items: list[AttentionItem]
    degraded: list[Degraded]
    generated_at: datetime
    feedback: dict[str, AttentionFeedback]


def item_key(rule_key: str, entity_id: uuid.UUID) -> str:
    return f"{rule_key}:{entity_id}"


def parse_item_key(key: str) -> tuple[str, uuid.UUID] | None:
    """``(ruleKey, entityId)`` of a well-formed key, else ``None``."""
    rule_key, sep, raw_id = key.rpartition(":")
    if not sep or not rule_key:
        return None
    try:
        return rule_key, uuid.UUID(raw_id)
    except ValueError:
        return None


def _clamp(score: float) -> int:
    return max(0, min(100, round(score)))


def approval_score(*, gate: bool, waiting: timedelta, priority: str | None) -> int:
    """A check blocks the work behind it, so it outranks a plain decision;
    each day of waiting adds a point, up to ten."""
    base = 80 if gate else 70
    days = min(10, max(0, int(waiting.total_seconds() // 86400)))
    return _clamp(base + days + _PRIORITY_BONUS.get(priority or "", 0))


def undecidable_score(*, waiting: timedelta) -> int:
    return _clamp(75 + min(waiting.days, 10))


def due_score(*, left: timedelta, priority: str) -> int:
    """Overdue is 90; due within the window grows from 60 to 80 as it nears."""
    base = 90.0 if left <= timedelta(0) else 60 + 20 * (1 - min(left, DUE_SOON) / DUE_SOON)
    return _clamp(base + _PRIORITY_BONUS.get(priority, 0))


def blocked_score(*, priority: str) -> int:
    return _clamp(55 + _PRIORITY_BONUS.get(priority, 0))


def failing_score(*, failures: int, priority: str) -> int:
    extra = min(15, 5 * max(0, failures - FAILED_RUNS_THRESHOLD))
    return _clamp(65 + extra + _PRIORITY_BONUS.get(priority, 0))


def _task_scope(scope: Scope) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        Task.tenant_id == scope.ctx.tenant_id,
        # Work of the caller's visible workspaces only (CP-ADR-0082 §4).
        workspace_condition(scope.ctx, Task.workspace_id),
    ]
    if scope.workspaces is not None:
        conditions.append(Task.workspace_id.in_(scope.workspaces))
    if scope.entity_id is not None:
        conditions.append(Task.id == scope.entity_id)
    return conditions


# --- approvals ---------------------------------------------------------------


async def _eligible_approvals(session: AsyncSession, scope: Scope) -> ColumnElement[bool]:
    """Approvals the principal may decide: its own, or its role's in scope.

    A role held in workspace W counts for approvals in W's subtree; a
    tenant-wide one counts everywhere — the rule of decision eligibility
    (``approvals._require_decision_eligibility``) read the other way round.
    """
    from control_plane.application.commands.workspaces import workspace_subtree_ids

    me = scope.ctx.principal_id
    held = (
        await session.execute(
            select(PrincipalRole.role_id, PrincipalRole.workspace_id).where(
                PrincipalRole.tenant_id == scope.ctx.tenant_id,
                PrincipalRole.principal_id == me,
            )
        )
    ).all()
    conditions: list[ColumnElement[bool]] = [Approval.assigned_principal_id == me]
    tenant_wide = [role_id for role_id, workspace_id in held if workspace_id is None]
    if tenant_wide:
        conditions.append(Approval.required_role_id.in_(tenant_wide))
    for role_id, workspace_id in held:
        if workspace_id is None:
            continue
        subtree = await workspace_subtree_ids(session, scope.ctx.tenant_id, workspace_id)
        conditions.append(
            and_(Approval.required_role_id == role_id, Approval.workspace_id.in_(subtree))
        )
    # An excluded principal may not decide at all (CP-ADR-0074 §7).
    return and_(or_(*conditions), ~Approval.excluded_principals.contains([str(me)]))


def _approval_fetch(*, gate: bool) -> RuleFetch:
    kind = KIND_REVIEW if gate else KIND_DECISION

    async def fetch(
        session: AsyncSession, scope: Scope, rule: AttentionRule, limit: int
    ) -> list[AttentionItem]:
        me = scope.ctx.principal_id
        workspace = func.coalesce(Approval.workspace_id, Task.workspace_id)
        stmt = (
            select(Approval, Task)
            .outerjoin(Task, Task.id == Approval.task_id)
            .where(
                Approval.tenant_id == scope.ctx.tenant_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.gate.is_(gate),
                await _eligible_approvals(session, scope),
                approval_condition(scope.ctx),
            )
            .order_by(Approval.created_at, Approval.id)
            .limit(limit)
        )
        if scope.workspaces is not None:
            stmt = stmt.where(workspace.in_(scope.workspaces))
        if scope.entity_id is not None:
            stmt = stmt.where(Approval.id == scope.entity_id)
        items = []
        for approval, task in (await session.execute(stmt)).all():
            assigned = approval.assigned_principal_id == me
            items.append(
                AttentionItem(
                    rule_key=rule.key,
                    rule_version=rule.version,
                    kind=kind,
                    reason_code=f"{kind}_{'assigned' if assigned else 'role'}",
                    entity_type="approval",
                    entity_id=approval.id,
                    score=approval_score(
                        gate=gate,
                        waiting=scope.now - approval.created_at,
                        priority=task.priority if task is not None else None,
                    ),
                    title=task.title if task is not None else approval.comment[:200],
                    workspace_id=approval.workspace_id
                    or (task.workspace_id if task is not None else None),
                    since=approval.created_at,
                    task_id=approval.task_id,
                    task_public_id=task.public_id if task is not None else None,
                    due_date=task.due_date if task is not None else None,
                    details={
                        "requestedBy": str(approval.requested_by_principal_id),
                        "requiredRoleId": str(approval.required_role_id)
                        if approval.required_role_id
                        else None,
                        "comment": approval.comment,
                    },
                )
            )
        return items

    return fetch


async def _undecidable_filter(
    session: AsyncSession, tenant_id: uuid.UUID, candidates: Sequence[ColumnElement[bool]]
) -> ColumnElement[bool] | None:
    """Approvals among ``candidates`` whose required role no principal that is
    not excluded holds in the approval's scope; ``None`` when there is none.

    One condition per workspace the candidates are in: role assignments count
    in a workspace through its ancestors (``role_assignment_scope``).
    """
    workspaces = (
        await session.scalars(select(Approval.workspace_id).where(*candidates).distinct())
    ).all()
    conditions: list[ColumnElement[bool]] = []
    for workspace_id in workspaces:
        holder = exists().where(
            PrincipalRole.tenant_id == Approval.tenant_id,
            PrincipalRole.role_id == Approval.required_role_id,
            ~Approval.excluded_principals.has_key(cast(PrincipalRole.principal_id, Text)),
            await role_assignment_scope(session, tenant_id, workspace_id),
        )
        here = (
            Approval.workspace_id.is_(None)
            if workspace_id is None
            else Approval.workspace_id == workspace_id
        )
        conditions.append(and_(here, ~holder))
    return or_(*conditions) if conditions else None


async def _is_addressee(
    session: AsyncSession, scope: Scope, address: Mapping[str, Any] | None
) -> bool:
    """Whether the caller is an addressee of a process (CP-ADR-0078 §3): the
    principal itself, or a holder of the role in the instance's workspace."""
    if not address:
        return False
    me = scope.ctx.principal_id
    if address.get("principalId"):
        return str(me) == str(address["principalId"])
    if not address.get("roleId"):
        return False
    workspace = address.get("workspaceId")
    scope_filter = await role_assignment_scope(
        session, scope.ctx.tenant_id, uuid.UUID(workspace) if workspace else None
    )
    held = await session.scalar(
        select(PrincipalRole.id).where(
            PrincipalRole.tenant_id == scope.ctx.tenant_id,
            PrincipalRole.principal_id == me,
            PrincipalRole.role_id == uuid.UUID(address["roleId"]),
            scope_filter,
        )
    )
    return held is not None


async def _undecidable(
    session: AsyncSession, scope: Scope, rule: AttentionRule, limit: int
) -> list[AttentionItem]:
    """Pending approvals whose every eligible decider is excluded (CP-ADR-0074 §7).

    Only an approval for a role can come to this: one assigned to an excluded
    principal is refused when requested. The owner of the process that asked
    for it hears of it (the addressee the step recorded), else whoever started
    the instance; a direct request — its requester. A role granted later makes
    the approval decidable, and the item goes away by itself.

    Whether anyone may decide is one query, not a query per approval: only
    approvals nobody may decide are read, at most ``UNDECIDABLE_SCAN`` of them,
    the oldest first — a state an operator repairs, not a daily one.
    """
    candidates: list[ColumnElement[bool]] = [
        Approval.tenant_id == scope.ctx.tenant_id,
        Approval.status == ApprovalStatus.PENDING,
        Approval.required_role_id.is_not(None),
        func.jsonb_array_length(Approval.excluded_principals) > 0,
        approval_condition(scope.ctx),
    ]
    if scope.workspaces is not None:
        candidates.append(Approval.workspace_id.in_(scope.workspaces))
    if scope.entity_id is not None:
        candidates.append(Approval.id == scope.entity_id)
    nobody = await _undecidable_filter(session, scope.ctx.tenant_id, candidates)
    if nobody is None:
        return []
    approvals = (
        await session.scalars(
            select(Approval)
            .where(*candidates, nobody)
            .order_by(Approval.created_at, Approval.id)
            .limit(UNDECIDABLE_SCAN)
        )
    ).all()
    if not approvals:
        return []
    refs = [f"approval:{approval.id}" for approval in approvals]
    instances: dict[str, ProcessInstance] = {}
    for row in await session.scalars(
        select(ProcessInstance).where(
            ProcessInstance.tenant_id == scope.ctx.tenant_id,
            ProcessInstance.refs.has_any(postgresql.array(refs)),
        )
    ):
        for ref in refs:
            if ref in (row.refs or {}):
                instances[ref] = row
    items: list[AttentionItem] = []
    for approval in approvals:
        ref = f"approval:{approval.id}"
        instance = instances.get(ref)
        if instance is not None:
            refs_of = instance.refs or {}
            activity = (refs_of.get(ref) or {}).get("activity")
            owner = (refs_of.get(f"activity:{activity}") or {}).get("owner")
            reason = "undecidable_process_owner"
            if not owner and instance.started_by is not None:
                # A process without ``spec.owner``: whoever started the instance.
                owner = {"principalId": str(instance.started_by)}
                reason = "undecidable_process_starter"
            if not await _is_addressee(session, scope, owner):
                continue
            # The owner is no relation of the approval (authz/catalog.yaml):
            # the item is shown only to an owner who may read it.
            try:
                await authorize(
                    scope.ctx,
                    Permission.APPROVALS_READ,
                    resource=ResourceRef("approval", str(approval.id)),
                )
            except AuthorizationError:
                continue
        elif approval.requested_by_principal_id == scope.ctx.principal_id:
            reason = "undecidable_requester"
        else:
            continue
        items.append(
            AttentionItem(
                rule_key=rule.key,
                rule_version=rule.version,
                kind=KIND_UNDECIDABLE,
                reason_code=reason,
                entity_type="approval",
                entity_id=approval.id,
                score=undecidable_score(waiting=scope.now - approval.created_at),
                title=approval.comment[:200],
                workspace_id=approval.workspace_id,
                since=approval.created_at,
                task_id=approval.task_id,
                details={
                    "requiredRoleId": str(approval.required_role_id),
                    "excludedPrincipals": list(approval.excluded_principals),
                    "processInstanceId": str(instance.id) if instance is not None else None,
                },
            )
        )
        if len(items) >= limit:
            break
    return items


# --- tasks -------------------------------------------------------------------


def _task_item(
    task: Task,
    *,
    rule: AttentionRule,
    kind: str,
    reason_code: str,
    score: int,
    since: datetime,
    details: dict[str, Any] | None = None,
) -> AttentionItem:
    return AttentionItem(
        rule_key=rule.key,
        rule_version=rule.version,
        kind=kind,
        reason_code=reason_code,
        entity_type="task",
        entity_id=task.id,
        score=score,
        title=task.title,
        workspace_id=task.workspace_id,
        since=since,
        task_id=task.id,
        task_public_id=task.public_id,
        due_date=task.due_date,
        details={"status": task.status, "priority": task.priority, **(details or {})},
    )


async def _due_not_started(
    session: AsyncSession, scope: Scope, rule: AttentionRule, limit: int
) -> list[AttentionItem]:
    started = select(Run.id).where(Run.task_id == Task.id).exists()
    stmt = (
        select(Task)
        .where(
            *_task_scope(scope),
            func.coalesce(Task.assignee_id, Task.owner_id) == scope.ctx.principal_id,
            Task.system_status_category.in_(_OPEN_CATEGORIES),
            Task.due_date.is_not(None),
            Task.due_date <= scope.now + DUE_SOON,
            Task.active_claim_id.is_(None),
            ~started,
        )
        .order_by(Task.due_date, Task.id)
        .limit(limit)
    )
    items = []
    for task in (await session.scalars(stmt)).all():
        assert task.due_date is not None
        left = task.due_date - scope.now
        items.append(
            _task_item(
                task,
                rule=rule,
                kind=KIND_DEADLINE,
                reason_code="overdue" if left <= timedelta(0) else "due_soon",
                score=due_score(left=left, priority=task.priority),
                since=task.created_at,
            )
        )
    return items


async def _blocked(
    session: AsyncSession, scope: Scope, rule: AttentionRule, limit: int
) -> list[AttentionItem]:
    me = scope.ctx.principal_id
    stmt = (
        select(Task)
        .where(
            *_task_scope(scope),
            or_(Task.assignee_id == me, Task.owner_id == me),
            Task.system_status_category == WorkItemStatusCategory.BLOCKED.value,
        )
        .order_by(Task.updated_at, Task.id)
        .limit(limit)
    )
    return [
        _task_item(
            task,
            rule=rule,
            kind=KIND_BLOCKED,
            reason_code="task_blocked",
            score=blocked_score(priority=task.priority),
            since=task.updated_at,
        )
        for task in (await session.scalars(stmt)).all()
    ]


def trailing_failures(statuses: Iterable[str]) -> int:
    """Failed runs at the end of a task's attempts (oldest first).

    A running attempt neither counts nor resets; a succeeded, cancelled or
    suspended one ends the streak.
    """
    count = 0
    for status in statuses:
        if status == RunStatus.FAILED:
            count += 1
        elif status != RunStatus.RUNNING:
            count = 0
    return count


async def _delegated_failing(
    session: AsyncSession, scope: Scope, rule: AttentionRule, limit: int
) -> list[AttentionItem]:
    me = scope.ctx.principal_id
    delegator = func.coalesce(Task.owner_id, Task.created_by)
    failed_runs = (
        select(func.count())
        .where(Run.task_id == Task.id, Run.status == RunStatus.FAILED)
        .scalar_subquery()
    )
    # Candidates by the cheap bound (enough failures in total); the streak is
    # checked on their runs below.
    candidates = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    *_task_scope(scope),
                    delegator == me,
                    Task.assignee_id.is_not(None),
                    Task.assignee_id != me,
                    Task.system_status_category.in_(_OPEN_CATEGORIES),
                    failed_runs >= FAILED_RUNS_THRESHOLD,
                )
                .order_by(Task.updated_at, Task.id)
            )
        ).all()
    )
    if not candidates:
        return []
    runs: dict[uuid.UUID, list[Run]] = {task.id: [] for task in candidates}
    for run in (
        await session.scalars(
            select(Run).where(Run.task_id.in_(list(runs))).order_by(Run.task_id, Run.attempt)
        )
    ).all():
        runs[run.task_id].append(run)
    items = []
    for task in candidates:
        attempts = runs[task.id]
        failures = trailing_failures(run.status for run in attempts)
        if failures < FAILED_RUNS_THRESHOLD:
            continue
        last_failed = next(run for run in reversed(attempts) if run.status == RunStatus.FAILED)
        items.append(
            _task_item(
                task,
                rule=rule,
                kind=KIND_DELEGATED_FAILURE,
                reason_code="runs_failed",
                score=failing_score(failures=failures, priority=task.priority),
                since=last_failed.finished_at or last_failed.started_at,
                details={
                    "assigneeId": str(task.assignee_id),
                    "failedRuns": failures,
                    "lastRunId": str(last_failed.id),
                    "lastFailureReason": last_failed.failure_reason,
                },
            )
        )
        if len(items) >= limit:
            break
    return items


RULES: tuple[AttentionRule, ...] = (
    AttentionRule("approval.review", 1, Permission.APPROVALS_READ, _approval_fetch(gate=True)),
    AttentionRule("approval.decide", 1, Permission.APPROVALS_READ, _approval_fetch(gate=False)),
    AttentionRule("approval.undecidable", 1, Permission.APPROVALS_READ, _undecidable),
    AttentionRule("task.due_not_started", 1, Permission.TASKS_READ, _due_not_started),
    AttentionRule("task.blocked", 1, Permission.TASKS_READ, _blocked),
    AttentionRule("task.delegated_failing", 1, Permission.TASKS_READ, _delegated_failing),
)


def rule_by_key(key: str) -> AttentionRule | None:
    return next((rule for rule in RULES if rule.key == key), None)


# --- evaluation --------------------------------------------------------------


async def evaluate(
    session: AsyncSession, scope: Scope, rules: Sequence[AttentionRule]
) -> tuple[list[AttentionItem], list[Degraded]]:
    """Run ``rules`` independently; a failing one degrades, never fails, the list."""
    items: list[AttentionItem] = []
    degraded: list[Degraded] = []
    permitted: dict[Permission, Degraded | None] = {}
    for rule in rules:
        if rule.permission not in permitted:
            permitted[rule.permission] = await _permission_gap(scope.ctx, rule)
        gap = permitted[rule.permission]
        if gap is not None:
            degraded.append(Degraded(rule.ref, gap.reason_code, gap.message))
            continue
        try:
            async with session.begin_nested():
                found = await rule.fetch(session, scope, rule, RULE_LIMIT + 1)
        except Exception:
            logger.exception("attention rule failed", extra={"rule": rule.ref})
            observability.inc("attention_rule_failures_total")
            degraded.append(Degraded(rule.ref, DEGRADED_FAILED, "the rule could not be evaluated"))
            continue
        if len(found) > RULE_LIMIT:
            found = found[:RULE_LIMIT]
            degraded.append(
                Degraded(rule.ref, DEGRADED_TRUNCATED, f"only the first {RULE_LIMIT} items")
            )
        items += found
    return items, degraded


async def _permission_gap(ctx: AuthContext, rule: AttentionRule) -> Degraded | None:
    try:
        await authorize(ctx, rule.permission)
    except AuthorizationError:
        return Degraded(
            rule.ref, DEGRADED_PERMISSION, f"the credential lacks {rule.permission.value}"
        )
    except DependencyUnavailableError:
        return Degraded(rule.ref, DEGRADED_POLICY, "the policy decision is unavailable")
    return None


async def _workspace_scope(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID, include_descendants: bool
) -> list[uuid.UUID]:
    from control_plane.application.commands.workspaces import workspace_subtree_ids

    subtree = await workspace_subtree_ids(session, ctx.tenant_id, workspace_id)
    # An invisible workspace answers as a missing one (CP-ADR-0082 §3.6).
    if not subtree or not ctx.sees_workspace(workspace_id):
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    return subtree if include_descendants else [workspace_id]


async def get_attention(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID | None = None,
    include_descendants: bool = False,
) -> AttentionList:
    """The calling principal's attention list, highest score first."""
    # The list is made of tasks and approvals: a credential that may read
    # neither has no list; one that may read one of them gets its half and
    # a ``degraded`` entry for the other.
    await authorize(ctx, Permission.TASKS_READ, Permission.APPROVALS_READ)
    now = utcnow()
    workspaces = (
        await _workspace_scope(session, ctx, workspace_id, include_descendants)
        if workspace_id is not None
        else None
    )
    items, degraded = await evaluate(session, Scope(ctx, now, workspaces), RULES)
    items.sort(key=lambda item: (-item.score, item.since, item.item_key))
    return AttentionList(
        items=items,
        degraded=degraded,
        generated_at=now,
        feedback=await _feedback_for(session, ctx, [item.item_key for item in items]),
    )


async def _feedback_for(
    session: AsyncSession, ctx: AuthContext, keys: list[str]
) -> dict[str, AttentionFeedback]:
    if not keys:
        return {}
    rows = await session.scalars(
        select(AttentionFeedback).where(
            AttentionFeedback.tenant_id == ctx.tenant_id,
            AttentionFeedback.principal_id == ctx.principal_id,
            AttentionFeedback.item_key.in_(keys),
        )
    )
    return {row.item_key: row for row in rows.all()}


async def find_item(session: AsyncSession, ctx: AuthContext, key: str) -> AttentionItem:
    """The item ``key`` as the principal's list shows it right now.

    Only the rule named in the key is evaluated, and only for the object it
    names. A key that is malformed,
    names no rule, or is not on the principal's list is 404 alike — the
    answer does not tell whether the object exists for someone else. A rule
    that could not be evaluated is 503: the item may well be there.
    """
    parsed = parse_item_key(key)
    rule = rule_by_key(parsed[0]) if parsed is not None else None
    if parsed is None or rule is None:
        raise NotFoundError("Attention item not found", details={"itemKey": key})
    scope = Scope(ctx, utcnow(), entity_id=parsed[1])
    items, degraded = await evaluate(session, scope, [rule])
    found = next((item for item in items if item.entity_id == parsed[1]), None)
    if found is not None:
        return found
    if degraded:
        entry = degraded[0]
        if entry.reason_code == DEGRADED_PERMISSION:
            raise AuthorizationError(details={"required": [rule.permission.value]})
        raise DependencyUnavailableError(
            "The rule of this item could not be evaluated",
            code="attention_rule_unavailable",
            details={"itemKey": key, "rule": rule.ref, "reasonCode": entry.reason_code},
        )
    raise NotFoundError("Attention item not found", details={"itemKey": key})


# --- presentation --------------------------------------------------------------


def _action(action: str, method: str, href: str) -> dict[str, str]:
    return {"action": action, "method": method, "href": href}


def item_actions(item: AttentionItem) -> list[dict[str, str]]:
    """What the principal can do about the item, as calls of this API."""
    api = "/api/v1"
    actions: list[dict[str, str]] = []
    if item.kind == KIND_UNDECIDABLE:
        # Nobody may decide: the owner opens the approval and its process.
        actions.append(_action("open", "GET", f"{api}/approvals/{item.entity_id}"))
        instance = item.details.get("processInstanceId")
        if instance:
            actions.append(_action("openProcess", "GET", f"{api}/process-instances/{instance}"))
    elif item.entity_type == "approval":
        base = f"{api}/approvals/{item.entity_id}"
        actions += [
            _action("approve", "POST", f"{base}:approve"),
            _action("reject", "POST", f"{base}:reject"),
            _action("open", "GET", base),
        ]
        if item.task_id is not None:
            actions.append(_action("openTask", "GET", f"{api}/tasks/{item.task_id}"))
    else:
        task = f"{api}/tasks/{item.entity_id}"
        actions.append(_action("open", "GET", task))
        if item.kind == KIND_DEADLINE:
            actions.append(_action("claim", "POST", f"{task}:claim"))
        elif item.kind == KIND_BLOCKED:
            actions.append(_action("update", "PATCH", task))
        elif item.kind == KIND_DELEGATED_FAILURE:
            actions += [
                _action("listRuns", "GET", f"{api}/runs?taskId={item.entity_id}"),
                _action("update", "PATCH", task),
            ]
    actions.append(_action("feedback", "POST", f"{api}/me/attention/{item.item_key}:feedback"))
    return actions


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def feedback_body(row: AttentionFeedback) -> dict[str, Any]:
    return {
        "itemKey": row.item_key,
        "rule": f"{row.rule_key}@{row.rule_version}",
        "verdict": row.verdict,
        "comment": row.comment,
        "recordedAt": _iso(row.updated_at),
    }


def item_body(item: AttentionItem, feedback: AttentionFeedback | None) -> dict[str, Any]:
    return {
        "itemKey": item.item_key,
        "kind": item.kind,
        "reasonCode": item.reason_code,
        "rule": item.rule,
        "score": item.score,
        "entity": {"type": item.entity_type, "id": str(item.entity_id)},
        "title": item.title,
        "workspaceId": str(item.workspace_id) if item.workspace_id else None,
        "taskId": str(item.task_id) if item.task_id else None,
        "taskPublicId": item.task_public_id,
        "dueDate": _iso(item.due_date),
        "since": _iso(item.since),
        "details": item.details,
        "actions": item_actions(item),
        "feedback": feedback_body(feedback) if feedback is not None else None,
    }


def attention_body(result: AttentionList) -> dict[str, Any]:
    return {
        "items": [item_body(item, result.feedback.get(item.item_key)) for item in result.items],
        "degraded": [
            {"rule": entry.rule, "reasonCode": entry.reason_code, "message": entry.message}
            for entry in result.degraded
        ],
        "generatedAt": _iso(result.generated_at),
    }
