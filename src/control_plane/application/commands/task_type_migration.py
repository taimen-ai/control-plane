"""Moving open tasks to another version of their type (ADR-0048, amendment 2026-09-30).

A task pins the exact type version it was created against, and that stays the
rule: a new version never reaches a task by itself. What changes is that the
pin can be moved deliberately — one task (``POST /tasks/{ref}:migrate-type``)
or every open task of a version (``POST /task-types/{id}:migrate-tasks``) —
to another version of the SAME key.

A migration only happens while nothing is in flight that already read the
old version: no live claim and no running run (it got the old
instructions), no open verification attempt (it runs the old checks), no
gate approval whose outcome is still to run (outcomes are read from the
task's type at decision time, CP-ADR-0061). Everything the target version fixes about a task is
checked before anything is written: its status, ``customFields`` against the
new ``fieldSchema``, and the task's own acceptance against the new type's
checks.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.approval_outcomes import OUTCOME_LIVE
from control_plane.application.commands.task_types import (
    get_tenant_task_type,
    resolve_task_type,
    task_type_lifecycle,
    task_type_of,
)
from control_plane.application.commands.tasks import (
    _own_checks,
    check_expected_version,
    live_claim_of,
    resolve_task_for_update,
)
from control_plane.application.common import utcnow
from control_plane.application.events import record_event
from control_plane.application.visibility import workspace_condition
from control_plane.domain.enums import ApprovalStatus, Permission, RunStatus
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    ValidationError,
)
from control_plane.domain.work_graph import check_evidence_against_acceptance
from control_plane.domain.work_item import (
    TERMINAL_CATEGORIES,
    WorkItemLifecycle,
    migrated_status,
    validate_status_map,
    validate_task_custom_fields,
)
from control_plane.infrastructure.db.models import Approval, Run, Task, TaskType

MIGRATE_TASKS_DEFAULT_LIMIT = 100
MIGRATE_TASKS_MAX_LIMIT = 500


async def _target_version(
    session: AsyncSession, ctx: AuthContext, source: TaskType, version: int | None
) -> TaskType:
    """The version ``version`` of the source's key; without one, its newest active version.

    The default only ever moves forward: when the source is newer than every
    active version (a deprecated newest one), a rollback has to be asked for
    by naming ``version`` explicitly.
    """
    target = await resolve_task_type(session, ctx, type_key=source.key, type_version=version)
    if version is None and target.version < source.version:
        raise ValidationError(
            "invalid_migration_target",
            "The newest active version is older than the source version; "
            "name the version explicitly to roll back",
            details={
                "typeKey": source.key,
                "typeVersion": source.version,
                "newestActiveVersion": target.version,
            },
        )
    return target


async def _check_nothing_in_flight(session: AsyncSession, task: Task) -> None:
    from control_plane.application.commands.verification import check_verification_gate

    if task.system_status_category in TERMINAL_CATEGORIES:
        raise ConflictError(
            "task_terminal",
            "A closed task keeps the type version it was closed under",
            details={"taskId": str(task.id), "status": task.status},
        )
    claim = await live_claim_of(session, task)
    if claim is not None:
        # Even for the holder: its run was started with the old version's
        # instructions and inputs, and would finish under different ones.
        raise ConflictError(
            "task_claimed",
            "Task has an active claim; migrate it after the claim is released",
            details={
                "taskId": str(task.id),
                "claimId": str(claim.id),
                "expiresAt": claim.expires_at.isoformat(),
            },
        )
    # A run can outlive its claim (expired lease, runner still at work): it
    # was started with the old version's instructions all the same.
    running = await session.scalar(
        select(Run.id).where(Run.task_id == task.id, Run.status == RunStatus.RUNNING)
    )
    if running is not None:
        raise ConflictError(
            "run_in_progress",
            "Task has a running run; migrate it after the run has finished",
            details={"taskId": str(task.id), "runId": str(running)},
        )
    await check_verification_gate(session, task)
    waiting = (
        await session.scalars(
            select(Approval.id)
            .where(
                Approval.tenant_id == task.tenant_id,
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                (Approval.status == ApprovalStatus.PENDING)
                | Approval.outcome_status.in_(OUTCOME_LIVE),
            )
            .order_by(Approval.id)
        )
    ).all()
    if waiting:
        raise ConflictError(
            "approval_pending",
            "A gate approval of the task has not run its outcome yet",
            details={"taskId": str(task.id), "approvals": [str(a) for a in waiting]},
        )


async def _migrate_locked(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    target: TaskType,
    target_lifecycle: WorkItemLifecycle,
    status_map: dict[str, str],
    *,
    trigger: str,
) -> bool:
    """Move a locked task to ``target``; ``False`` if it already carries it.

    Every check runs before the first write, so a refused migration leaves the
    task — type, status, version — exactly as it was.
    """
    if task.type_id == target.id:
        return False
    await _check_nothing_in_flight(session, task)
    source = await task_type_of(session, task)
    if source.key != target.key:  # pragma: no cover - callers resolve by the task's key
        raise ValidationError(
            "invalid_migration_target",
            "A task moves between versions of its own type only",
            details={"typeKey": source.key, "targetTypeKey": target.key},
        )
    status = migrated_status(target_lifecycle, task.status, status_map)
    validate_task_custom_fields(target.field_schema, task.custom_fields or {})
    # The task's own checks must not collide with the checks of the target
    # version, and evidence must still point at a check the task will have.
    checks = await _own_checks(session, ctx, target, list(task.acceptance or []))
    check_evidence_against_acceptance(list(task.evidence or []), [*target.acceptance, *checks])

    from_status = task.status
    task.type_id = target.id
    task.status = status
    task.system_status_category = target_lifecycle.category_of(status)
    task.acceptance = checks
    task.version += 1
    task.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.type_migrated",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "typeKey": target.key,
            "fromTypeVersion": source.version,
            "typeVersion": target.version,
            "fromStatus": from_status,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
            "trigger": trigger,
            "version": task.version,
        },
    )
    return True


async def migrate_task_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    expected_version: int,
    type_version: int | None = None,
    status_map: dict[str, Any] | None = None,
) -> Task:
    """Move one open task to another version of its type.

    It needs ``task_types.manage``, not only ``tasks.write`` on the task: a
    migration may drop checks of the type (a version without ``review``), and
    those are a floor the task's author or executor does not lower
    (CP-ADR-0067). Migrating a task to the version it already carries answers the task as it
    is — no new version, no event — so a retried call is harmless.
    """
    await authorize(ctx, Permission.TASK_TYPES_MANAGE)
    await authorize(ctx, Permission.TASKS_WRITE)
    task = await resolve_task_for_update(session, ctx, task_ref)
    await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", str(task.id)))
    check_expected_version(task, expected_version)
    source = await task_type_of(session, task)
    target = await _target_version(session, ctx, source, type_version)
    lifecycle = task_type_lifecycle(target)
    mapping = validate_status_map(task_type_lifecycle(source), lifecycle, status_map)
    await _migrate_locked(session, ctx, task, target, lifecycle, mapping, trigger="task")
    return task


async def migrate_type_tasks(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_id: uuid.UUID,
    to_version: int | None = None,
    status_map: dict[str, Any] | None = None,
    limit: int | None = None,
    cursor: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Move the open tasks of one type version to another version of its key.

    An operator's tool: one page of open tasks (by id, after ``cursor``) per
    call. A task that cannot move is reported in ``skipped`` with the code a
    single migration would answer, and stays where it was; the others move.
    A task the caller may not write is left out of the page altogether, as if
    it did not exist.
    """
    await authorize(ctx, Permission.TASK_TYPES_MANAGE)
    await authorize(ctx, Permission.TASKS_WRITE)
    page_size = MIGRATE_TASKS_DEFAULT_LIMIT if limit is None else limit
    if not 1 <= page_size <= MIGRATE_TASKS_MAX_LIMIT:
        raise ValidationError(
            "invalid_limit",
            f"limit must be between 1 and {MIGRATE_TASKS_MAX_LIMIT}",
            details={"limit": limit},
        )
    source = await get_tenant_task_type(session, ctx, type_id)
    target = await _target_version(session, ctx, source, to_version)
    if target.id == source.id:
        raise ValidationError(
            "invalid_migration_target",
            "The target version is the source version",
            details={"typeKey": source.key, "typeVersion": source.version},
        )
    lifecycle = task_type_lifecycle(target)
    mapping = validate_status_map(task_type_lifecycle(source), lifecycle, status_map)

    stmt = select(Task.id).where(
        Task.tenant_id == ctx.tenant_id,
        Task.type_id == source.id,
        Task.system_status_category.not_in(TERMINAL_CATEGORIES),
        # Work of an invisible workspace is left out like work the caller may
        # not write (CP-ADR-0082 §3.7).
        workspace_condition(ctx, Task.workspace_id),
    )
    if cursor is not None:
        stmt = stmt.where(Task.id > cursor)
    ids = list((await session.scalars(stmt.order_by(Task.id).limit(page_size + 1))).all())
    next_cursor = str(ids[page_size - 1]) if len(ids) > page_size else None

    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task_id in ids[:page_size]:
        try:
            await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", str(task_id)))
        except AuthorizationError:
            continue
        try:
            # One savepoint per task: a refusal leaves the others' moves intact.
            async with session.begin_nested():
                task = await resolve_task_for_update(session, ctx, str(task_id))
                from_status = task.status
                # Read before the lock: by now another call may have moved the
                # task elsewhere, and this page does not move it again.
                moved = task.type_id == source.id and await _migrate_locked(
                    session, ctx, task, target, lifecycle, mapping, trigger="bulk"
                )
        except DomainError as error:
            skipped.append(
                {
                    "taskId": str(task_id),
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            )
            continue
        if moved:
            migrated.append(
                {
                    "taskId": str(task.id),
                    "publicId": task.public_id,
                    "fromStatus": from_status,
                    "status": task.status,
                    "version": task.version,
                }
            )

    return {
        "typeKey": source.key,
        "fromTypeVersion": source.version,
        "typeVersion": target.version,
        "typeId": str(target.id),
        "migrated": migrated,
        "skipped": skipped,
        "nextCursor": next_cursor,
    }
