"""Goal commands and the work-graph checks shared with tasks (CP-ADR-0062).

A Goal is a desired state, not a work item: it has no claim, no run and no
lifecycle beyond ``active -> achieved | abandoned`` (and back to ``active``
when the state it describes stops being true). Work items point at it.

The helpers at the bottom are the part tasks reuse: resolving a goal a task
may serve, and checking that evidence names facts that exist in this tenant.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    WorkspaceNotVisible,
    authorize,
)
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.visibility import artifact_condition, task_condition
from control_plane.domain.enums import Permission, PrincipalKind
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_graph import (
    GoalStatus,
    OriginKind,
    context_pack_targets,
    evidence_targets,
    normalize_checks,
    normalize_desired_state,
    normalize_goal_title,
    normalize_origin,
    origin_summary,
    validate_goal_status,
)
from control_plane.infrastructure.db.models import (
    Artifact,
    Goal,
    TaskContextPack,
)

_UNSET: Any = object()

# A goal hierarchy deeper than this is a modelling mistake, and the walk that
# refuses cycles must terminate whatever the data says.
MAX_GOAL_DEPTH = 32


def default_origin(ctx: AuthContext) -> dict[str, Any]:
    """The origin of an item whose writer did not describe one.

    Taken from who is writing, never guessed from the content: a person's
    credential files ``human`` work, an agent's or a service's files
    ``harness`` work.
    """
    kind = OriginKind.HUMAN if ctx.principal_kind == PrincipalKind.HUMAN else OriginKind.HARNESS
    return {"kind": kind.value, "evidence": []}


async def resolve_origin(
    session: AsyncSession, ctx: AuthContext, value: dict[str, Any] | None, *, field: str
) -> dict[str, Any]:
    origin = default_origin(ctx) if value is None else normalize_origin(value, field=field)
    await verify_evidence(session, ctx, origin["evidence"])
    return origin


async def create_goal(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    title: str,
    desired_state: str = "",
    criteria: list[dict[str, Any]] | None = None,
    owner_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    parent_goal_id: uuid.UUID | None = None,
    created_from: dict[str, Any] | None = None,
) -> Goal:
    from control_plane.application.commands.workspaces import require_active_workspace

    await authorize(ctx, Permission.GOALS_WRITE, resource=goal_scope(workspace_id))
    title_text = normalize_goal_title(title)
    desired = normalize_desired_state(desired_state)
    checks = normalize_checks(criteria or [], field="criteria", typed_spec=False)
    origin = await resolve_origin(session, ctx, created_from, field="createdFrom")
    if owner_id is not None:
        await get_tenant_principal(session, ctx, owner_id)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)
    if parent_goal_id is not None:
        parent = await get_readable_goal(session, ctx, parent_goal_id)
        _check_parent_scope(parent, workspace_id)

    now = utcnow()
    goal = Goal(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        title=title_text,
        desired_state=desired,
        criteria=checks,
        owner_id=owner_id,
        status=GoalStatus.ACTIVE.value,
        created_from=origin,
        parent_goal_id=parent_goal_id,
        version=1,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
        closed_at=None,
    )
    session.add(goal)
    await session.flush()

    await _record(
        session,
        ctx,
        goal,
        "goal.created",
        {
            "title": goal.title,
            "status": goal.status,
            "workspaceId": str(workspace_id) if workspace_id else None,
            "ownerId": str(owner_id) if owner_id else None,
            "parentGoalId": str(parent_goal_id) if parent_goal_id else None,
            # References and counts only: the desired state is prose and the
            # criteria specs are tenant documents (ADR-0015).
            "criteriaCount": len(checks),
            "createdFrom": origin_summary(origin),
        },
    )
    return goal


async def update_goal(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    goal_id: uuid.UUID,
    expected_version: int,
    title: str | Any = _UNSET,
    desired_state: str | Any = _UNSET,
    criteria: list[dict[str, Any]] | Any = _UNSET,
    owner_id: uuid.UUID | Any | None = _UNSET,
    status: str | Any = _UNSET,
    parent_goal_id: uuid.UUID | Any | None = _UNSET,
) -> Goal:
    """Change what a goal says; ``createdFrom`` and the workspace are not editable.

    Only fields whose value actually changes count: a PATCH that restates the
    current values is answered with the goal as it is, without a new version
    and without an event, so a retried write cannot manufacture history.
    """
    await authorize(ctx, Permission.GOALS_WRITE)
    goal = await get_tenant_goal(session, ctx, goal_id, for_update=True)
    await authorize(ctx, Permission.GOALS_WRITE, resource=goal_scope(goal.workspace_id))
    if goal.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Goal version does not match If-Match",
            details={
                "goalId": str(goal.id),
                "expectedVersion": expected_version,
                "currentVersion": goal.version,
            },
        )
    provided = [
        v
        for v in (title, desired_state, criteria, owner_id, status, parent_goal_id)
        if v is not _UNSET
    ]
    if not provided:
        raise ValidationError("empty_update", "No fields to update")

    changes: dict[str, Any] = {}
    if title is not _UNSET:
        changes["title"] = normalize_goal_title(title)
    if desired_state is not _UNSET:
        changes["desired_state"] = normalize_desired_state(desired_state)
    if criteria is not _UNSET:
        changes["criteria"] = normalize_checks(criteria, field="criteria", typed_spec=False)
    if owner_id is not _UNSET:
        if owner_id is not None:
            await get_tenant_principal(session, ctx, owner_id)
        changes["owner_id"] = owner_id
    if status is not _UNSET:
        changes["status"] = validate_goal_status(status)
    if parent_goal_id is not _UNSET:
        if parent_goal_id is not None:
            await _check_parent(session, ctx, goal, parent_goal_id)
        changes["parent_goal_id"] = parent_goal_id
    changes = {k: v for k, v in changes.items() if getattr(goal, k) != v}
    if not changes:
        return goal

    previous_status = goal.status
    now = utcnow()
    for field_name, value in changes.items():
        setattr(goal, field_name, value)
    if "status" in changes:
        # The CHECK pins closed_at to the status; keep the two moving together.
        goal.closed_at = None if goal.status == GoalStatus.ACTIVE else now
    goal.version += 1
    goal.updated_at = now
    await session.flush()

    await _record(
        session,
        ctx,
        goal,
        "goal.updated",
        {
            "changes": {k: _journal_value(k, v) for k, v in changes.items()},
            **(
                {"fromStatus": previous_status, "status": goal.status}
                if "status" in changes
                else {}
            ),
            "version": goal.version,
        },
    )
    return goal


def _journal_value(field_name: str, value: Any) -> Any:
    if field_name == "desired_state":
        return True
    if field_name == "criteria":
        return len(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def _check_parent(
    session: AsyncSession, ctx: AuthContext, goal: Goal, parent_goal_id: uuid.UUID
) -> None:
    """Refuse a parent the caller cannot read, one from another workspace, and
    one that is the goal itself or one of its descendants."""
    if parent_goal_id == goal.id:
        raise ValidationError(
            "goal_cycle", "A goal cannot be its own parent", details={"goalId": str(goal.id)}
        )
    parent = await get_readable_goal(session, ctx, parent_goal_id)
    _check_parent_scope(parent, goal.workspace_id)
    cursor: uuid.UUID | None = parent.id
    depth = 0
    while cursor is not None:
        if cursor == goal.id:
            raise ValidationError(
                "goal_cycle",
                "The new parent is a descendant of this goal",
                details={"goalId": str(goal.id), "parentGoalId": str(parent_goal_id)},
            )
        depth += 1
        if depth > MAX_GOAL_DEPTH:
            raise ValidationError(
                "goal_too_deep",
                f"Goal hierarchy exceeds {MAX_GOAL_DEPTH} levels",
                details={"parentGoalId": str(parent_goal_id)},
            )
        cursor = await session.scalar(
            select(Goal.parent_goal_id).where(Goal.tenant_id == ctx.tenant_id, Goal.id == cursor)
        )


def _check_parent_scope(parent: Goal, workspace_id: uuid.UUID | None) -> None:
    """A parent lives in the child's workspace or at tenant level.

    Only reached for a parent the caller may read, so the refusal tells it
    nothing it could not already see.
    """
    if not goal_serves_workspace(parent, workspace_id):
        raise ValidationError(
            "goal_workspace_mismatch",
            "The parent goal belongs to another workspace",
            details={
                "parentGoalId": str(parent.id),
                "workspaceId": str(workspace_id) if workspace_id else None,
            },
        )


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    goal: Goal,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="goal",
        entity_id=goal.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"goalId": str(goal.id), **payload},
    )


# --- shared with tasks --------------------------------------------------------


async def get_tenant_goal(
    session: AsyncSession, ctx: AuthContext, goal_id: uuid.UUID, *, for_update: bool = False
) -> Goal:
    stmt = select(Goal).where(Goal.id == goal_id, Goal.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    goal = await session.scalar(stmt)
    # A workspace outside the caller's visibility answers exactly as a missing
    # row, never as a missing workspace (CP-ADR-0082 §3.7).
    if goal is None or (
        goal.workspace_id is not None and not ctx.sees_workspace(goal.workspace_id)
    ):
        raise NotFoundError("Goal not found", details={"goalId": str(goal_id)})
    return goal


def goal_scope(workspace_id: uuid.UUID | None) -> ResourceRef | None:
    """The resource ``goals.*`` is decided on: the goal's workspace, or the
    tenant for a tenant-level goal (``authz/catalog.yaml``)."""
    return ResourceRef("workspace", str(workspace_id)) if workspace_id else None


def goal_serves_workspace(goal: Goal, workspace_id: uuid.UUID | None) -> bool:
    """A goal is reachable from its own workspace; a tenant-level goal from any."""
    return goal.workspace_id is None or goal.workspace_id == workspace_id


async def get_readable_goal(session: AsyncSession, ctx: AuthContext, goal_id: uuid.UUID) -> Goal:
    """A goal the caller may read under ``goals.read``, else ``404``.

    Used where a goal is only referenced (a parent, the goal a task serves):
    a goal the caller may not read and one that does not exist answer alike,
    so a writer cannot probe another workspace for ids.
    """
    goal = await get_tenant_goal(session, ctx, goal_id)
    try:
        await authorize(ctx, Permission.GOALS_READ, resource=goal_scope(goal.workspace_id))
    except (AuthorizationError, WorkspaceNotVisible):
        raise NotFoundError("Goal not found", details={"goalId": str(goal_id)}) from None
    return goal


async def require_linkable_goal(
    session: AsyncSession,
    ctx: AuthContext,
    goal_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None,
) -> Goal:
    """The goal a task in ``workspace_id`` may be linked to.

    Readable by the caller and serving the task's workspace — otherwise the
    same ``404`` as a goal that does not exist — and not abandoned. An
    achieved goal still accepts work on purpose: the state it describes can
    stop being true, and the work that restores it belongs to the same goal.
    """
    goal = await get_readable_goal(session, ctx, goal_id)
    if not goal_serves_workspace(goal, workspace_id):
        raise NotFoundError("Goal not found", details={"goalId": str(goal_id)})
    if goal.status == GoalStatus.ABANDONED:
        raise ValidationError(
            "goal_abandoned",
            "Work cannot be linked to an abandoned goal",
            details={"goalId": str(goal_id)},
        )
    return goal


async def verify_evidence(
    session: AsyncSession, ctx: AuthContext, *documents: list[dict[str, Any]]
) -> None:
    """Every observation, artifact and context pack named by evidence exists in this tenant
    and is visible to the caller.

    Unknown, foreign and invisible ids are the same ``404``: evidence is a
    reference, and a reference into another tenant or an invisible workspace
    would both be false and confirm that the id exists there (CP-ADR-0082
    V6). Observations are journal events, possibly archived (ADR-0038), so
    both tables are searched.
    """
    observation_ids, artifact_ids = evidence_targets(*documents)
    if observation_ids:
        from control_plane.application.queries.events import recorded_observations

        seen = await recorded_observations(session, ctx, set(observation_ids))
        missing = sorted(str(i) for i in observation_ids - seen)
        if missing:
            raise NotFoundError(
                "Evidence observation not found", details={"observationIds": missing}
            )
    if artifact_ids:
        found = set(
            (
                await session.scalars(
                    select(Artifact.id).where(
                        Artifact.tenant_id == ctx.tenant_id,
                        Artifact.id.in_(artifact_ids),
                        artifact_condition(ctx),
                    )
                )
            ).all()
        )
        missing = sorted(str(i) for i in artifact_ids - found)
        if missing:
            raise NotFoundError("Evidence artifact not found", details={"artifactIds": missing})
    pack_ids = context_pack_targets(*documents)
    if pack_ids:
        found = set(
            (
                await session.scalars(
                    select(TaskContextPack.id).where(
                        TaskContextPack.tenant_id == ctx.tenant_id,
                        TaskContextPack.id.in_(pack_ids),
                        task_condition(ctx, TaskContextPack.task_id),
                    )
                )
            ).all()
        )
        missing = sorted(str(i) for i in pack_ids - found)
        if missing:
            raise NotFoundError(
                "Evidence context pack not found", details={"contextPackIds": missing}
            )
