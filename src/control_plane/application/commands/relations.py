"""Task relations: a small directed graph over tasks.

Direction semantics live in TaskRelationType. The prerequisite ("needs")
graph is derived from ``depends_on`` (from needs to) and ``blocks`` (to needs
from); adding an edge to it — or to the ``parent`` hierarchy — is cycle-checked
with a recursive CTE under a per-tenant advisory lock, so two concurrent
inserts cannot assemble a cycle together.
"""

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.visibility import task_condition
from control_plane.domain.enums import (
    BLOCKING_RELATION_TYPES,
    Permission,
    TaskRelationType,
)
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.infrastructure.db.models import Task, TaskRelation


async def _lock_task_graph(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:taskgraph:{tenant_id}", 0)))
    )


async def resolve_task(session: AsyncSession, ctx: AuthContext, task_ref: str) -> Task:
    conditions = [Task.tenant_id == ctx.tenant_id]
    try:
        conditions.append(Task.id == uuid.UUID(task_ref))
    except ValueError:
        conditions.append(Task.public_id == task_ref.upper())
    task = await session.scalar(select(Task).where(*conditions))
    # Work of a workspace outside the caller's visibility answers exactly as
    # missing work, for everything under it too (CP-ADR-0082 §3.7).
    if task is None or not ctx.sees_workspace(task.workspace_id):
        raise NotFoundError("Task not found", details={"task": task_ref})
    return task


def _needs_edge(
    relation_type: str, from_task_id: uuid.UUID, to_task_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Normalize a blocking relation into a (dependent, prerequisite) edge."""
    if relation_type == TaskRelationType.DEPENDS_ON:
        return from_task_id, to_task_id
    return to_task_id, from_task_id  # blocks


async def _would_create_cycle(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    relation_type: str,
    from_task_id: uuid.UUID,
    to_task_id: uuid.UUID,
) -> bool:
    """Would adding this edge close a cycle in its graph?

    For an edge X -> Y a cycle appears iff a path Y ->* X already exists.
    """
    if relation_type == TaskRelationType.PARENT:
        # child -> parent hierarchy graph.
        source, target = from_task_id, to_task_id
        edges_sql = """
            SELECT from_task_id AS src, to_task_id AS dst FROM task_relations
            WHERE tenant_id = :tenant AND relation_type = 'parent'
        """
    else:
        # Combined "dependent needs prerequisite" graph.
        dependent, prerequisite = _needs_edge(relation_type, from_task_id, to_task_id)
        source, target = dependent, prerequisite
        edges_sql = """
            SELECT CASE WHEN relation_type = 'depends_on' THEN from_task_id
                        ELSE to_task_id END AS src,
                   CASE WHEN relation_type = 'depends_on' THEN to_task_id
                        ELSE from_task_id END AS dst
            FROM task_relations
            WHERE tenant_id = :tenant AND relation_type IN ('depends_on', 'blocks')
        """
    row = await session.execute(
        text(
            f"""
            WITH RECURSIVE g AS ({edges_sql}),
            reach AS (
                SELECT dst FROM g WHERE src = :start
                UNION
                SELECT g.dst FROM g JOIN reach r ON g.src = r.dst
            )
            SELECT 1 FROM reach WHERE dst = :goal LIMIT 1
            """
        ),
        {"tenant": tenant_id, "start": target, "goal": source},
    )
    return row.first() is not None


async def add_relation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    from_task_ref: str,
    to_task_ref: str,
    relation_type: str,
) -> TaskRelation:
    await authorize(ctx, Permission.TASKS_WRITE)
    if relation_type not in set(TaskRelationType):
        raise ValidationError("invalid_relation_type", f"Unknown relation type: {relation_type}")

    from_task = await resolve_task(session, ctx, from_task_ref)
    to_task = await resolve_task(session, ctx, to_task_ref)
    if from_task.id == to_task.id:
        raise ValidationError("invalid_relation", "A task cannot be related to itself")

    cycle_checked = relation_type in BLOCKING_RELATION_TYPES or (
        relation_type == TaskRelationType.PARENT
    )
    if cycle_checked:
        # Serialize graph-structure changes per tenant so two individually
        # acyclic inserts cannot form a cycle together.
        await _lock_task_graph(session, ctx.tenant_id)

    duplicate = await session.scalar(
        select(TaskRelation.id).where(
            TaskRelation.from_task_id == from_task.id,
            TaskRelation.to_task_id == to_task.id,
            TaskRelation.relation_type == relation_type,
        )
    )
    if duplicate is not None:
        raise ConflictError(
            "relation_exists",
            "This relation already exists",
            details={"relationId": str(duplicate)},
        )

    if cycle_checked and await _would_create_cycle(
        session,
        ctx.tenant_id,
        relation_type=relation_type,
        from_task_id=from_task.id,
        to_task_id=to_task.id,
    ):
        raise ValidationError(
            "dependency_cycle",
            "This relation would create a cycle",
            details={
                "fromTaskId": str(from_task.id),
                "toTaskId": str(to_task.id),
                "type": relation_type,
            },
        )

    relation = TaskRelation(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        from_task_id=from_task.id,
        to_task_id=to_task.id,
        relation_type=relation_type,
        created_by_principal_id=ctx.principal_id,
        created_at=utcnow(),
    )
    session.add(relation)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.relation_added",
        entity_type="task",
        entity_id=from_task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "relationId": str(relation.id),
            "toTaskId": str(to_task.id),
            "type": relation_type,
        },
    )
    return relation


async def remove_relation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    relation_id: uuid.UUID,
) -> None:
    await authorize(ctx, Permission.TASKS_WRITE)
    task = await resolve_task(session, ctx, task_ref)
    relation = await session.scalar(
        select(TaskRelation)
        .where(
            TaskRelation.id == relation_id,
            TaskRelation.tenant_id == ctx.tenant_id,
            (TaskRelation.from_task_id == task.id) | (TaskRelation.to_task_id == task.id),
            # A relation to invisible work is not listed, nor removed (CP-ADR-0082 §3.7).
            task_condition(ctx, TaskRelation.from_task_id),
            task_condition(ctx, TaskRelation.to_task_id),
        )
        .with_for_update()
    )
    if relation is None:
        raise NotFoundError("Relation not found", details={"relationId": str(relation_id)})
    payload = {
        "relationId": str(relation.id),
        "fromTaskId": str(relation.from_task_id),
        "toTaskId": str(relation.to_task_id),
        "type": relation.relation_type,
    }
    await session.delete(relation)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task.relation_removed",
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )


def _blocker(row: Any) -> dict[str, str]:
    return {
        "taskId": str(row[0]),
        "publicId": row[1],
        "status": row[2],
        "systemStatusCategory": row[3],
    }


async def shown_prerequisites(
    session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID
) -> tuple[list[dict[str, str]], int]:
    """The unmet prerequisites as the caller may see them, and how many it may not.

    A prerequisite of an invisible workspace still blocks — readiness is the
    same for everyone — but is not named: neither its id nor its status
    (CP-ADR-0082 V4). Only the count of such blockers tells why the work waits.
    """
    shown: list[dict[str, str]] = []
    hidden = 0
    for row in await _unmet_rows(session, ctx.tenant_id, task_id):
        if ctx.sees_workspace(row[4]):
            shown.append(_blocker(row))
        else:
            hidden += 1
    return shown, hidden


async def _unmet_rows(session: AsyncSession, tenant_id: uuid.UUID, task_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            """
            SELECT t.id, t.public_id, t.status, t.system_status_category, t.workspace_id
            FROM task_relations r
            JOIN tasks t ON t.id = CASE
                WHEN r.relation_type = 'depends_on' AND r.from_task_id = :task
                    THEN r.to_task_id
                WHEN r.relation_type = 'blocks' AND r.to_task_id = :task
                    THEN r.from_task_id
            END
            WHERE r.tenant_id = :tenant
              AND ((r.relation_type = 'depends_on' AND r.from_task_id = :task)
                OR (r.relation_type = 'blocks' AND r.to_task_id = :task))
              AND t.system_status_category != 'terminal_success'
            """
        ),
        {"tenant": tenant_id, "task": task_id},
    )
    return list(rows.all())


async def check_task_readiness(session: AsyncSession, ctx: AuthContext, task: Task) -> None:
    """Raise 409 task_not_ready if blocking prerequisites are incomplete.

    Readiness is computed from authoritative state inside the claiming
    transaction: an uncommitted prerequisite completion is invisible here, so
    a dependent task can never be claimed before the completion has committed.
    """
    blocking, hidden = await shown_prerequisites(session, ctx, task.id)
    if blocking or hidden:
        details: dict[str, Any] = {"taskId": str(task.id), "blockedBy": blocking}
        if hidden:
            details["hiddenBlockers"] = hidden
        raise ConflictError(
            "task_not_ready",
            "Task has incomplete blocking dependencies",
            details=details,
        )
