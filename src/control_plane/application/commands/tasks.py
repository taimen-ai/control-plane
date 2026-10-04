"""Task commands: create, update, complete.

Mutations are protected by three independent mechanisms:

1. Optimistic concurrency — the caller states the expected task version
   (HTTP ``If-Match``); a mismatch is a ``409 version_conflict``.
2. Claim gate — while a task holds a live claim, mutations must present the
   matching ``claimId`` + ``fencingToken``; anything else is rejected, so a
   woken-up old session can never write over a newer claim.
3. Row locks — every mutation locks the task row (``SELECT ... FOR UPDATE``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import sandbox
from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    check_workspace_visible,
)
from control_plane.application.commands._claim_release import release_claim_on_locked_task
from control_plane.application.commands.agent_assignees import agent_principal, resolve_assignee
from control_plane.application.commands.eligibility import (
    RequirementSpec,
    set_task_requirements,
)
from control_plane.application.commands.goals import (
    get_tenant_goal,
    goal_serves_workspace,
    require_linkable_goal,
    resolve_origin,
    verify_evidence,
)
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.commands.task_inputs import artifact_schema_of
from control_plane.application.commands.task_types import (
    lifecycle_of,
    resolve_task_type,
    task_type_lifecycle,
    task_type_of,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_claim_session, lock_principals_key_share
from control_plane.domain.enums import (
    ClaimStatus,
    Permission,
    RunStatus,
    SessionStatus,
    TaskPriority,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_graph import (
    attempt_checks,
    check_evidence_against_acceptance,
    normalize_checks,
    normalize_evidence,
    origin_summary,
    output_checks,
)
from control_plane.domain.work_item import (
    TransitionRoute,
    WorkItemLifecycle,
    WorkItemStatusCategory,
    normalize_planned_date,
    transition_route,
    validate_planned_dates,
    validate_task_custom_fields,
)
from control_plane.infrastructure.db.models import (
    Run,
    Session,
    Task,
    TaskClaim,
    TaskCounter,
    TaskType,
)

_UNSET: Any = object()


def _journal_value(field_name: str, value: Any) -> Any:
    """One changed field as the event journal may carry it.

    ``custom_fields`` collapses to a flag: the journal is read far more widely
    than the task, and a tenant's own fields are exactly the place where
    business data ends up (ADR-0015).
    """
    if field_name == "custom_fields":
        return True
    # Work graph documents (CP-ADR-0062) travel as counts: checks carry specs
    # and evidence carries notes, both of which stay with the task.
    if field_name in ("acceptance", "evidence"):
        return len(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _status_not_in_lifecycle(lifecycle: WorkItemLifecycle, status_key: str) -> ValidationError:
    return ValidationError(
        "status_not_in_lifecycle",
        f"Status {status_key!r} is not declared by the task type's lifecycle",
        details={"statusKey": status_key, "known": sorted(lifecycle.statuses)},
    )


async def next_public_id(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Transactionally allocate the next per-tenant task number (no collisions).

    The upsert takes a row lock on the tenant's counter, serializing task
    creation per tenant; the unique constraint on (tenant_id, public_id) is
    the safety net. Inside a package test the number comes from the test and
    the counter is not touched (CP-ADR-0074 Z2): its row lock would stop the
    tenant's task creation until the test rolls back.
    """
    source = sandbox.public_id_source()
    if source is not None:
        return source()
    stmt = (
        pg_insert(TaskCounter)
        .values(tenant_id=tenant_id, last_value=1)
        .on_conflict_do_update(
            index_elements=[TaskCounter.tenant_id],
            set_={"last_value": TaskCounter.last_value + 1},
        )
        .returning(TaskCounter.last_value)
    )
    value = (await session.execute(stmt)).scalar_one()
    return f"TASK-{value:06d}"


async def resolve_task_for_update(session: AsyncSession, ctx: AuthContext, task_ref: str) -> Task:
    """Find a task by UUID or public id within the actor's tenant and lock it."""
    conditions = [Task.tenant_id == ctx.tenant_id]
    try:
        conditions.append(Task.id == uuid.UUID(task_ref))
    except ValueError:
        conditions.append(Task.public_id == task_ref.upper())
    task = await session.scalar(select(Task).where(*conditions).with_for_update())
    # Work of a workspace outside the caller's visibility answers exactly as
    # missing work, for everything under it too (CP-ADR-0082 §3.7).
    if task is None or not ctx.sees_workspace(task.workspace_id):
        raise NotFoundError("Task not found", details={"task": task_ref})
    return task


async def create_task(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    title: str,
    description: str = "",
    priority: str = TaskPriority.MEDIUM,
    status: str | None = None,
    type_id: uuid.UUID | None = None,
    type_key: str | None = None,
    type_version: int | None = None,
    owner_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | str | None = None,
    workspace_id: uuid.UUID | None = None,
    custom_fields: dict[str, Any] | None = None,
    start_date: datetime | None = None,
    due_date: datetime | None = None,
    requirements: RequirementSpec | None = None,
    goal_id: uuid.UUID | None = None,
    origin: dict[str, Any] | None = None,
    acceptance: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    assignee_field: str = "assigneeId",
) -> Task:
    """File a work item.

    ``origin`` is recorded once and never rewritten (CP-ADR-0062); omitted, it
    is derived from the writer's principal kind, never from the content.
    ``assignee_id`` may be an ``agent:<key>`` reference (CP-ADR-0073, A1),
    resolved once the write is authorized; ``assignee_field`` names the field
    it came from in an ``unknown_agent`` refusal.
    """
    from control_plane.application.commands.workspaces import (
        require_active_workspace,
        require_task_type_allowed,
    )

    await authorize(
        ctx,
        Permission.TASKS_WRITE,
        resource=ResourceRef("workspace", str(workspace_id)) if workspace_id else None,
    )
    if not title.strip():
        raise ValidationError("invalid_title", "title must not be empty")
    if priority not in set(TaskPriority):
        raise ValidationError("invalid_priority", f"Unknown priority: {priority}")

    # No type reference resolves to the tenant's system type, which is what
    # keeps a pre-v0.8 client working unchanged (ADR-0048).
    task_type = await resolve_task_type(
        session, ctx, type_id=type_id, type_key=type_key, type_version=type_version
    )
    lifecycle = task_type_lifecycle(task_type)
    status_key = lifecycle.initial_status if status is None else status
    if not lifecycle.declares(status_key):
        raise _status_not_in_lifecycle(lifecycle, status_key)
    category = lifecycle.category_of(status_key)
    if status_key != lifecycle.initial_status and category != WorkItemStatusCategory.BACKLOG:
        # For the system type this is exactly the pre-v0.8 rule ("backlog" or
        # "todo"). Allowing any non-terminal status here would let a client
        # create a task already in progress with no claim behind it — a lie
        # written into authoritative state.
        raise ValidationError(
            "invalid_status",
            "New tasks must start in the type's initial status or in a 'backlog' status",
            details={
                "statusKey": status_key,
                "initialStatus": lifecycle.initial_status,
                "systemStatusCategory": category,
            },
        )
    fields = custom_fields or {}
    # Against the schema of the version this task pins, not of the newest one:
    # the type it carries is the contract it was created under.
    validate_task_custom_fields(task_type.field_schema, fields)
    start_date = normalize_planned_date(start_date)
    due_date = normalize_planned_date(due_date)
    validate_planned_dates(start_date, due_date)

    assignee_id = await resolve_assignee(session, ctx.tenant_id, assignee_id, field=assignee_field)
    # Principals the new task references (rule 3 of ``application/locking.py``,
    # CP-ADR-0077 §3): taken before any row of its own. A caller that already
    # holds another task (an outcome, a rule, a launch) must take them earlier
    # still; this keeps the task's own writes in order.
    await lock_principals_key_share(session, ctx.tenant_id, [owner_id, assignee_id])
    for ref in (owner_id, assignee_id):
        if ref is not None:
            await get_tenant_principal(session, ctx, ref)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)
        await require_task_type_allowed(session, ctx.tenant_id, workspace_id, task_type.key)

    checks = await _own_checks(session, ctx, task_type, acceptance or [])
    gathered = normalize_evidence(evidence or [])
    check_evidence_against_acceptance(gathered, [*task_type.acceptance, *checks])
    origin_doc = await resolve_origin(session, ctx, origin, field="origin")
    await verify_evidence(session, ctx, gathered)
    if goal_id is not None:
        await require_linkable_goal(session, ctx, goal_id, workspace_id=workspace_id)

    now = utcnow()
    task = Task(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        public_id=await next_public_id(session, ctx.tenant_id),
        workspace_id=workspace_id,
        type_id=task_type.id,
        title=title.strip(),
        description=description,
        status=status_key,
        system_status_category=category,
        priority=priority,
        owner_id=owner_id,
        assignee_id=assignee_id,
        custom_fields=fields,
        start_date=start_date,
        due_date=due_date,
        goal_id=goal_id,
        origin=origin_doc,
        acceptance=checks,
        evidence=gathered,
        version=1,
        claim_epoch=0,
        active_claim_id=None,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()

    if requirements is not None and not requirements.is_empty():
        await set_task_requirements(session, ctx, task, requirements)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.created",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "title": task.title,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
            "typeKey": task_type.key,
            "typeVersion": task_type.version,
            "priority": task.priority,
            "workspaceId": str(workspace_id) if workspace_id else None,
            "startDate": start_date.isoformat() if start_date else None,
            "dueDate": due_date.isoformat() if due_date else None,
            # The FACT of custom fields, never their content: the journal is
            # read far more widely than the task itself (ADR-0015).
            "customFields": bool(fields),
            # Work graph (CP-ADR-0062): which goal, and where the work came
            # from — by reference, so a subscriber can follow the chain.
            "goalId": str(goal_id) if goal_id else None,
            "origin": origin_summary(origin_doc),
            "acceptanceChecks": len(checks),
        },
    )
    return task


async def _own_checks(
    session: AsyncSession, ctx: AuthContext, task_type: TaskType, acceptance: Any
) -> list[dict[str, Any]]:
    """A task's own acceptance, checked against the checks of its type version.

    The type's checks run ahead of the task's in every attempt (CP-ADR-0067,
    amendment 2026-09-27, B5): a task may add checks, never replace one of
    its type's — a key the type already declares is ``invalid_acceptance``.
    An external write of the task may rest on a decision the type declares.
    """
    from control_plane.application.commands.verification import check_acceptance_skills

    checks = normalize_checks(acceptance, field="acceptance")
    declared = {check["key"] for check in task_type.acceptance}
    for index, check in enumerate(checks):
        if check["key"] in declared:
            raise ValidationError(
                "invalid_acceptance",
                f"acceptance[{index}].key {check['key']!r} is a check of the task type; "
                "a task adds checks, it does not replace its type's",
                details={"field": f"acceptance[{index}].key", "key": check["key"]},
            )
    await check_acceptance_skills(session, ctx, checks, before=list(task_type.acceptance))
    return checks


def check_expected_version(task: Task, expected_version: int) -> None:
    if task.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Task version does not match If-Match",
            details={
                "taskId": str(task.id),
                "expectedVersion": expected_version,
                "currentVersion": task.version,
            },
        )


async def live_claim_of(session: AsyncSession, task: Task) -> TaskClaim | None:
    """The task's claim if it still protects the task, else ``None``.

    A claim whose session is closed/stale/expired no longer protects the
    task: its holder cannot legitimately write, and blocking everyone else
    for the rest of the claim TTL would freeze the task.
    """
    if task.active_claim_id is None:
        return None
    now = utcnow()
    # populate_existing: never trust a pre-lock identity-map copy here.
    claim = await session.get(TaskClaim, task.active_claim_id, populate_existing=True)
    if claim is None or claim.status != ClaimStatus.ACTIVE or claim.expires_at <= now:
        return None
    holder_session = await session.get(Session, claim.session_id)
    if (
        holder_session is None
        or holder_session.status != SessionStatus.ACTIVE
        or holder_session.expires_at <= now
    ):
        return None
    return claim


async def enforce_claim_gate(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    *,
    claim_id: uuid.UUID | None,
    fencing_token: int | None,
) -> TaskClaim | None:
    """Reject mutations that conflict with the task's live claim.

    Returns the presented live claim when the caller is its legitimate holder,
    else None (no live claim involved). Raises on every fencing violation.
    """
    active_claim = await live_claim_of(session, task)
    if active_claim is None:
        if claim_id is not None:
            raise ConflictError(
                "stale_claim",
                "Presented claim is no longer live",
                details={"claimId": str(claim_id)},
            )
        return None

    if claim_id is None:
        raise ConflictError(
            "task_claimed",
            "Task has an active claim; present claimId and fencingToken",
            details={
                "taskId": str(task.id),
                "claimId": str(active_claim.id),
                "expiresAt": active_claim.expires_at.isoformat(),
            },
        )
    if claim_id != active_claim.id or fencing_token is None:
        raise ConflictError(
            "stale_claim",
            "Presented claim is not the task's active claim",
            details={"claimId": str(claim_id)},
        )
    if fencing_token != active_claim.fencing_token or fencing_token != task.claim_epoch:
        raise ConflictError(
            "stale_claim",
            "Fencing token does not match the current claim epoch",
            details={
                "claimId": str(claim_id),
                "presentedFencingToken": fencing_token,
                "currentClaimEpoch": task.claim_epoch,
            },
        )

    if active_claim.holder_id != ctx.principal_id:
        raise AuthorizationError(
            "Claim is held by another principal",
            code="claim_holder_mismatch",
            details={"claimId": str(active_claim.id)},
        )
    return active_claim


async def _plan_transition(session: AsyncSession, task: Task, target: str) -> dict[str, Any]:
    """Validate a status change against the task type's lifecycle.

    Raises before anything is mutated, so a refused transition leaves both the
    status and the task version untouched — that is the acceptance criterion,
    not an implementation detail.
    """
    lifecycle = await lifecycle_of(session, task)
    if not lifecycle.declares(target):
        raise _status_not_in_lifecycle(lifecycle, target)
    if transition_route(lifecycle, target) is TransitionRoute.COMPLETE:
        # Completion is a separate action because it does more than write a
        # status: it releases the claim, settles a running run and stamps
        # completed_at. The same routing rule feeds the read-only projection
        # (GET /tasks/{ref}/transitions), so what a reader is told and what a
        # writer is allowed cannot drift apart.
        raise ValidationError(
            "invalid_status",
            "Use the :complete action to finish a task",
            details={"statusKey": target},
        )
    if target == task.status:
        raise ValidationError(
            "invalid_transition", "Task is already in this status", details={"statusKey": target}
        )
    if not lifecycle.allows(task.status, target):
        raise ValidationError(
            "invalid_transition",
            f"Transition {task.status!r} -> {target!r} is not declared",
            details={
                "from": task.status,
                "to": target,
                "allowed": lifecycle.targets_from(task.status),
            },
        )
    return {"status": target, "system_status_category": lifecycle.category_of(target)}


async def update_task(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    expected_version: int,
    claim_id: uuid.UUID | None = None,
    fencing_token: int | None = None,
    title: str | Any = _UNSET,
    description: str | Any = _UNSET,
    priority: str | Any = _UNSET,
    status: str | Any = _UNSET,
    owner_id: uuid.UUID | Any | None = _UNSET,
    assignee_id: uuid.UUID | Any | None = _UNSET,
    workspace_id: uuid.UUID | Any | None = _UNSET,
    custom_fields: dict[str, Any] | Any = _UNSET,
    start_date: datetime | Any | None = _UNSET,
    due_date: datetime | Any | None = _UNSET,
    requirements: RequirementSpec | None = None,
    goal_id: uuid.UUID | Any | None = _UNSET,
    acceptance: list[dict[str, Any]] | Any = _UNSET,
    evidence: list[dict[str, Any]] | Any = _UNSET,
) -> Task:
    await authorize(ctx, Permission.TASKS_WRITE)
    if isinstance(assignee_id, str):
        assignee_id = await agent_principal(session, ctx.tenant_id, assignee_id, field="assigneeId")
    # Principals the task will reference, before the task (rule 3 of
    # ``application/locking.py``, CP-ADR-0077 §3): their foreign-key lock would
    # otherwise come after the task row, which ``:disable`` of one of them may
    # be waiting for while holding the principal.
    await lock_principals_key_share(
        session,
        ctx.tenant_id,
        [v for v in (owner_id, assignee_id) if isinstance(v, uuid.UUID)],
    )
    task = await resolve_task_for_update(session, ctx, task_ref)
    await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", str(task.id)))
    check_expected_version(task, expected_version)
    await enforce_claim_gate(session, ctx, task, claim_id=claim_id, fencing_token=fencing_token)

    changes: dict[str, Any] = {}
    if title is not _UNSET:
        if not str(title).strip():
            raise ValidationError("invalid_title", "title must not be empty")
        changes["title"] = str(title).strip()
    if description is not _UNSET:
        changes["description"] = str(description)
    if priority is not _UNSET:
        if priority not in set(TaskPriority):
            raise ValidationError("invalid_priority", f"Unknown priority: {priority}")
        changes["priority"] = priority
    previous_status = task.status
    if status is not _UNSET:
        changes.update(await _plan_transition(session, task, str(status)))
    for field_name, value in (("owner_id", owner_id), ("assignee_id", assignee_id)):
        if value is not _UNSET:
            if value is not None:
                await get_tenant_principal(session, ctx, value)
            changes[field_name] = value
    if workspace_id is not _UNSET:
        if workspace_id is not None:
            from control_plane.application.commands.workspaces import (
                require_active_workspace,
                require_task_type_allowed,
            )

            # Moving work into a workspace the caller does not see answers as
            # for a missing one, before its status or allowed types can tell
            # otherwise (CP-ADR-0082 §3.7).
            check_workspace_visible(ctx, workspace_id)
            await require_active_workspace(session, ctx, workspace_id)
            if workspace_id != task.workspace_id:
                # Moving work in is filing it there: the target must allow its
                # type (CP-ADR-0008, amendment 2026-10-03 A3).
                moved_type = await task_type_of(session, task)
                await require_task_type_allowed(
                    session, ctx.tenant_id, workspace_id, moved_type.key
                )
        changes["workspace_id"] = workspace_id
    if custom_fields is not _UNSET:
        task_type = await task_type_of(session, task)
        # A whole-document replace, like a project profile: merging would make
        # it impossible to remove a key, and a partial document could not be
        # checked against a schema with "required".
        validate_task_custom_fields(task_type.field_schema, custom_fields)
        changes["custom_fields"] = custom_fields
    if start_date is not _UNSET or due_date is not _UNSET:
        # An update that moves one end of the interval is validated against the
        # OTHER end as stored, not against nothing.
        new_start = (
            normalize_planned_date(start_date) if start_date is not _UNSET else task.start_date
        )
        new_due = normalize_planned_date(due_date) if due_date is not _UNSET else task.due_date
        validate_planned_dates(new_start, new_due)
        if start_date is not _UNSET:
            changes["start_date"] = new_start
        if due_date is not _UNSET:
            changes["due_date"] = new_due
    new_workspace_id = changes.get("workspace_id", task.workspace_id)
    if goal_id is not _UNSET:
        if goal_id is not None:
            await require_linkable_goal(session, ctx, goal_id, workspace_id=new_workspace_id)
        changes["goal_id"] = goal_id
    elif "workspace_id" in changes and task.goal_id is not None:
        # Moving the task must not leave it serving a goal of the workspace it
        # left; the caller relinks (or unlinks) in the same update.
        linked = await get_tenant_goal(session, ctx, task.goal_id)
        if not goal_serves_workspace(linked, new_workspace_id):
            raise ValidationError(
                "goal_workspace_mismatch",
                "The task's goal belongs to another workspace; relink or unlink it",
                details={"goalId": str(task.goal_id)},
            )
    if acceptance is not _UNSET or evidence is not _UNSET:
        # Both are whole-document replaces, like custom_fields. Evidence tied
        # to a check is validated against the acceptance the task will HAVE,
        # so dropping a check that evidence still cites is refused too.
        task_type = await task_type_of(session, task)
        new_acceptance = (
            await _own_checks(session, ctx, task_type, acceptance)
            if acceptance is not _UNSET
            else task.acceptance
        )
        new_evidence = normalize_evidence(evidence) if evidence is not _UNSET else task.evidence
        check_evidence_against_acceptance(new_evidence, [*task_type.acceptance, *new_acceptance])
        if evidence is not _UNSET:
            await verify_evidence(session, ctx, new_evidence)
            changes["evidence"] = new_evidence
        if acceptance is not _UNSET:
            changes["acceptance"] = new_acceptance

    if not changes and requirements is None:
        raise ValidationError("empty_update", "No fields to update")

    for field_name, value in changes.items():
        setattr(task, field_name, value)
    if changes.get("system_status_category") == WorkItemStatusCategory.TERMINAL_CANCELLED:
        # A cancelled task is not verified any further (CP-ADR-0067 §5).
        from control_plane.application.commands.verification import cancel_open_attempt

        await cancel_open_attempt(session, ctx, task)
    elif "evidence" in changes:
        # A fact an attempt waits for may just have arrived (CP-ADR-0067).
        from control_plane.application.commands.verification import wake_on_evidence

        await wake_on_evidence(session, task.id)
    if requirements is not None:
        # Replace the whole requirement set (requirements resolve against the
        # task's NEW workspace if it changed in this same update).
        await set_task_requirements(session, ctx, task, requirements)
        changes["requirements"] = {
            "roles": requirements.roles,
            "capabilities": requirements.capabilities,
            "skills": requirements.skills,
        }
    task.version += 1
    task.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.updated",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "changes": {k: _journal_value(k, v) for k, v in changes.items()},
            # A status change is the one update a subscriber routinely needs to
            # react to, so it is stated at the top level and in both halves of
            # the pair, rather than only as a column name inside `changes`.
            **(
                {
                    "fromStatus": previous_status,
                    "status": task.status,
                    "systemStatusCategory": task.system_status_category,
                }
                if "status" in changes
                else {}
            ),
            "version": task.version,
        },
    )
    return task


async def complete_task(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    expected_version: int,
    claim_id: uuid.UUID | None = None,
    fencing_token: int | None = None,
    trigger: str = "complete",
    trigger_ref: str | None = None,
    implicit_checks: list[dict[str, Any]] | None = None,
) -> Task:
    await authorize(ctx, Permission.TASKS_WRITE)
    # The claim's session before the task (rule 2 of ``application/locking.py``,
    # CP-ADR-0077 §3): ``:disable`` of the principal a session acts for holds
    # it before the task. The completer itself — referenced by the attempt and
    # the completion work written below — was locked by the write flow (rule 1).
    await lock_claim_session(session, ctx.tenant_id, claim_id)
    task = await resolve_task_for_update(session, ctx, task_ref)
    await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", str(task.id)))
    check_expected_version(task, expected_version)

    live_claim = await verify_task_completable(
        session, ctx, task, claim_id=claim_id, fencing_token=fencing_token
    )

    running_run = await get_running_run_locked(session, task)
    if running_run is not None:
        if live_claim is not None and running_run.claim_id == live_claim.id:
            raise ConflictError(
                "run_in_progress",
                "The task has an active run; finish it via :succeed, :fail or :cancel",
                details={"taskId": str(task.id), "runId": str(running_run.id)},
            )
        # A zombie run from a previous claim epoch: supersede it as part of
        # completion (mirrors claim takeover semantics).
        await supersede_run(session, ctx, task, running_run)

    return await finish_locked_task(
        session,
        ctx,
        task,
        live_claim,
        trigger=trigger,
        trigger_ref=trigger_ref,
        implicit_checks=implicit_checks,
    )


async def verify_task_completable(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    *,
    claim_id: uuid.UUID | None,
    fencing_token: int | None,
) -> TaskClaim | None:
    """Status + claim-gate checks shared by :complete and run :succeed."""
    from control_plane.application.commands.approvals import check_approval_gate

    # By category, never by key: a tenant's "shipped" and "archived" must
    # behave the way "done" and "cancelled" did (ADR-0048).
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_SUCCESS:
        raise ConflictError(
            "task_already_completed",
            "Task is already completed",
            details={"taskId": str(task.id), "status": task.status},
        )
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_CANCELLED:
        raise ValidationError(
            "task_cancelled",
            "Cancelled tasks cannot be completed",
            details={"taskId": str(task.id), "status": task.status},
        )
    # v0.3 approval gate: a pending gate approval blocks completion the same
    # way it blocks claiming (409 approval_required).
    await check_approval_gate(session, ctx, task.id)
    return await enforce_claim_gate(
        session, ctx, task, claim_id=claim_id, fencing_token=fencing_token
    )


async def get_running_run_locked(session: AsyncSession, task: Task) -> Run | None:
    """Lock the task's running run, if any (task row is already locked)."""
    run: Run | None = await session.scalar(
        select(Run)
        .where(Run.task_id == task.id, Run.status == RunStatus.RUNNING)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return run


async def supersede_run(
    session: AsyncSession, ctx: AuthContext, task: Task, run: Run, *, reason: str = "superseded"
) -> None:
    """Fail a (locked) run whose claim is gone: a zombie left behind by a
    previous claim epoch, or a run of a principal that was disabled."""
    now = utcnow()
    run.status = RunStatus.FAILED
    run.failure_reason = reason
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
        payload={"taskId": str(task.id), "reason": reason, "attempt": run.attempt},
    )


async def finish_locked_task(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    live_claim: TaskClaim | None,
    *,
    trigger: str = "complete",
    trigger_ref: str | None = None,
    implicit_checks: list[dict[str, Any]] | None = None,
) -> Task:
    """Complete a locked task: release its claim, set done, record events.

    A task with acceptance checks is not set done here: its claim is released
    and a verification attempt is opened instead (CP-ADR-0067); the stage sets
    it done once the checks pass. ``trigger`` / ``trigger_ref`` say which of
    the completion paths got here (``complete``, ``run``, ``approval``,
    ``rule``). ``implicit_checks`` are run instead when the task has no
    acceptance of its own: a rule closing work verifies it by the evidence it
    wrote (CP-ADR-0063, amendment A1). The required outputs of the task's
    type come first on every path, even with an acceptance of its own: there
    is no way to done without them (CP-ADR-0067, amendment 2026-09-26); the
    checks its type version declares follow, then its own (amendment
    2026-09-27, :func:`attempt_checks`).
    """
    from control_plane.application.commands.verification import (
        open_attempt,
        open_verification,
    )

    checks = attempt_checks(
        output_checks(await artifact_schema_of(session, task)),
        list((await task_type_of(session, task)).acceptance),
        list(task.acceptance or []),
        implicit_checks or [],
    )
    if await open_attempt(session, task.id) is not None:
        # Completed again while its checks run: the attempt already open is
        # the answer, a second one is never opened (FR-011).
        return task
    lifecycle = await lifecycle_of(session, task)
    completion = lifecycle.completion_status
    if not lifecycle.allows(task.status, completion):
        # Unlike the automatic claim/release transitions, completion is an
        # explicit user action, so an undeclared edge is refused rather than
        # skipped: silently finishing along a path the tenant never declared
        # would defeat the point of having a lifecycle.
        raise ValidationError(
            "invalid_transition",
            f"Transition {task.status!r} -> {completion!r} is not declared",
            details={
                "from": task.status,
                "to": completion,
                "allowed": lifecycle.targets_from(task.status),
            },
        )
    await _detach_claim(session, ctx, task, live_claim)
    if checks:
        return await open_verification(
            session,
            ctx,
            task,
            session_id=live_claim.session_id if live_claim else None,
            trigger=trigger,
            trigger_ref=trigger_ref,
            checks=checks,
        )
    await mark_task_completed(
        session, ctx, task, session_id=live_claim.session_id if live_claim else None
    )
    return task


async def _detach_claim(
    session: AsyncSession, ctx: AuthContext, task: Task, live_claim: TaskClaim | None
) -> None:
    """Release the completer's claim, or drop a pointer to a dead one."""
    if live_claim is not None:
        release_claim_on_locked_task(task, live_claim, reason="completed")
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
            payload={"taskId": str(task.id), "reason": "completed"},
        )
    elif task.active_claim_id is not None:
        # Pointer to an expired/dead claim: detach it as part of completion.
        dead = await session.get(TaskClaim, task.active_claim_id)
        if dead is not None and dead.status == ClaimStatus.ACTIVE:
            release_claim_on_locked_task(task, dead, reason="expired", new_status=ClaimStatus.STALE)
        task.active_claim_id = None


async def mark_task_completed(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    *,
    session_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Move a locked task into its completion status and file what follows.

    The one place a task becomes ``terminal_success``: straight from a
    completion when it has no acceptance checks, from a passed verification
    attempt otherwise (``payload`` then adds the attempt to the event).
    """
    now = utcnow()
    lifecycle = await lifecycle_of(session, task)
    completion = lifecycle.completion_status
    task.status = completion
    task.system_status_category = lifecycle.category_of(completion)
    task.completed_at = now
    task.updated_at = now
    task.version += 1

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.completed",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        session_id=session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
            "version": task.version,
            **(payload or {}),
        },
    )
    # What the task's type declares for after completion is filed now, by the
    # completer, whoever that is (CP-ADR-0061, amendment 2026-09-25). It never
    # undoes the completion: a failed action is recorded, not raised.
    from control_plane.application.commands.completion_work import file_completion_work

    await file_completion_work(session, ctx, task)
