"""Child run handle reads (HRS-7).

The execution status of a child is *derived* here, on every read, from the
child Task and Run. It is never stored on the handle: two copies of a status
drift, and the copy that drifts is always the one someone trusted.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import decode_cursor, encode_cursor, utcnow
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.visibility import task_condition, task_visible
from control_plane.domain.child_handle import (
    looks_like_token,
    parse_token,
    token_secret_matches,
)
from control_plane.domain.enums import Permission, RunStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.work_item import WorkItemStatusCategory
from control_plane.infrastructure.db.models import Run, RunChildHandle, RunChildResult, Task

#: How many child handles a Run Context carries inline before it says hasMore.
RUN_CONTEXT_CHILD_LIMIT = 50


@dataclass(frozen=True)
class ChildHandleView:
    handle: RunChildHandle
    child_task: Task | None
    child_run: Run | None
    result: RunChildResult | None


@dataclass(frozen=True)
class ChildHandlePage:
    items: list[ChildHandleView]
    next_cursor: str | None
    has_more: bool


def derived_status(view: ChildHandleView) -> str:
    """Status describes the *execution*; revocation and expiry are the handle.

    Once a run exists, its status wins — a revoked handle whose child is still
    running must not report ``revoked``, or an orchestrator would believe the
    work stopped when it did not. ``revokedAt`` and ``expiresAt`` are separate
    fields precisely so the two facts never have to be squeezed into one word.

    Before a run exists, revocation and expiry *are* the whole story: a handle
    in either state can no longer bind a run at all.
    """
    handle = view.handle
    run = view.child_run
    if run is not None:
        return {
            RunStatus.RUNNING: "running",
            RunStatus.SUCCEEDED: "succeeded",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.SUSPENDED: "suspended",
        }[RunStatus(run.status)]
    if handle.revoked_at is not None:
        return "revoked"
    if handle.expires_at <= utcnow():
        return "expired"
    if (
        view.child_task is not None
        and view.child_task.system_status_category == WorkItemStatusCategory.TERMINAL_CANCELLED
    ):
        return "cancelled"
    return "pending"


def child_handle_body(view: ChildHandleView) -> dict[str, Any]:
    handle = view.handle
    granted = handle.granted if isinstance(handle.granted, dict) else {}
    body: dict[str, Any] = {
        "id": str(handle.id),
        "tenantId": str(handle.tenant_id),
        "parentRunId": str(handle.parent_run_id),
        "parentTaskId": str(handle.parent_task_id),
        "childTaskId": str(handle.child_task_id),
        "childTaskPublicId": view.child_task.public_id if view.child_task else None,
        "childRunId": str(handle.child_run_id) if handle.child_run_id else None,
        "correlationId": handle.correlation_id,
        "grant": {
            "permissions": list(granted.get("permissions", [])),
            "capabilities": list(granted.get("capabilities", [])),
            "skills": list(granted.get("skills", [])),
        },
        "cancellationPolicy": handle.cancellation_policy,
        "depth": handle.depth,
        "handleVersion": handle.handle_version,
        "status": derived_status(view),
        "expiresAt": handle.expires_at.isoformat(),
        "revokedAt": handle.revoked_at.isoformat() if handle.revoked_at else None,
        "revokeReason": handle.revoke_reason or None,
        "createdAt": handle.created_at.isoformat(),
        "result": None,
    }
    if view.result is not None:
        body["result"] = {
            "outcome": view.result.outcome,
            "summary": view.result.summary,
            "data": view.result.data,
            "artifactRefs": list(view.result.artifact_refs or []),
            "resultHash": view.result.result_hash,
            "recordedAt": view.result.recorded_at.isoformat(),
        }
    return body


async def _load_views(
    session: AsyncSession, ctx: AuthContext, handles: list[RunChildHandle]
) -> list[ChildHandleView]:
    if not handles:
        return []
    task_ids = {handle.child_task_id for handle in handles}
    run_ids = {handle.child_run_id for handle in handles if handle.child_run_id is not None}
    handle_ids = {handle.id for handle in handles}

    tasks = {
        task.id: task
        for task in (
            await session.scalars(
                select(Task).where(Task.tenant_id == ctx.tenant_id, Task.id.in_(task_ids))
            )
        ).all()
    }
    runs = {
        run.id: run
        for run in (
            await session.scalars(
                select(Run).where(Run.tenant_id == ctx.tenant_id, Run.id.in_(run_ids))
            )
        ).all()
        if run_ids
    }
    results = {
        result.handle_id: result
        for result in (
            await session.scalars(
                select(RunChildResult).where(
                    RunChildResult.tenant_id == ctx.tenant_id,
                    RunChildResult.handle_id.in_(handle_ids),
                )
            )
        ).all()
    }
    return [
        ChildHandleView(
            handle=handle,
            child_task=tasks.get(handle.child_task_id),
            child_run=runs.get(handle.child_run_id) if handle.child_run_id else None,
            result=results.get(handle.id),
        )
        for handle in handles
    ]


async def handle_visible(session: AsyncSession, ctx: AuthContext, handle: RunChildHandle) -> bool:
    """Both ends of a child handle are visible work."""
    return await task_visible(session, ctx, handle.parent_task_id) and await task_visible(
        session, ctx, handle.child_task_id
    )


async def get_child_handle_view(
    session: AsyncSession, ctx: AuthContext, handle: RunChildHandle
) -> ChildHandleView:
    views = await _load_views(session, ctx, [handle])
    return views[0]


async def resolve_child_handle(
    session: AsyncSession, ctx: AuthContext, ref: str
) -> ChildHandleView:
    """Resolve by handle id or by token.

    The token is a locator, not a credential: it makes an id unguessable, and
    the caller still needs ``tasks.read`` in the handle's own tenant. A wrong
    tenant is a 404 rather than a 403 so an id cannot be probed for existence.
    """
    await authorize(ctx, Permission.TASKS_READ)
    secret: str | None = None
    if looks_like_token(ref):
        handle_id, secret = parse_token(ref)
    else:
        try:
            handle_id = uuid.UUID(ref)
        except ValueError as exc:
            raise ValidationError(
                "invalid_child_handle_ref", "Expected a child handle id or a ch1_ token"
            ) from exc

    handle = await session.scalar(
        select(RunChildHandle).where(
            RunChildHandle.id == handle_id,
            RunChildHandle.tenant_id == ctx.tenant_id,
        )
    )
    # A handle between works the caller does not both see is a missing one
    # (CP-ADR-0082 §3.7).
    if handle is None or not await handle_visible(session, ctx, handle):
        raise NotFoundError("Child handle not found", details={"childHandleId": str(handle_id)})
    if secret is not None and not token_secret_matches(secret, handle.secret_hash):
        # Same shape as an unknown id: a valid id with a wrong secret must not
        # be distinguishable from a handle that does not exist.
        raise NotFoundError("Child handle not found", details={"childHandleId": str(handle_id)})
    return await get_child_handle_view(session, ctx, handle)


def _child_cursor(parent_run_id: uuid.UUID, created_at: str, handle_id: uuid.UUID) -> str:
    return "cd1_" + encode_cursor({"r": str(parent_run_id), "t": created_at, "i": str(handle_id)})


def _parse_child_cursor(cursor: str, parent_run_id: uuid.UUID) -> tuple[datetime, uuid.UUID]:
    if not cursor.startswith("cd1_"):
        raise ValidationError("invalid_cursor", "Malformed child handle cursor")
    data = decode_cursor(cursor[4:])
    try:
        cursor_run_id = uuid.UUID(data["r"])
        created_at = datetime.fromisoformat(str(data["t"]))
        handle_id = uuid.UUID(data["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed child handle cursor") from exc
    if cursor_run_id != parent_run_id:
        raise ValidationError("invalid_cursor", "Child handle cursor belongs to another Run")
    return created_at, handle_id


async def list_child_handles(
    session: AsyncSession,
    ctx: AuthContext,
    parent_run_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    active_only: bool = False,
) -> ChildHandlePage:
    from control_plane.application.queries.execution import get_run

    await authorize(ctx, Permission.TASKS_READ)
    await get_run(session, ctx, parent_run_id)
    bounded_limit = clamp_limit(limit)

    statement = select(RunChildHandle).where(
        RunChildHandle.tenant_id == ctx.tenant_id,
        RunChildHandle.parent_run_id == parent_run_id,
        # A child filed in an invisible workspace is not listed (CP-ADR-0082 §3.7).
        task_condition(ctx, RunChildHandle.child_task_id),
    )
    if cursor:
        created_at, handle_id = _parse_child_cursor(cursor, parent_run_id)
        statement = statement.where(
            tuple_(RunChildHandle.created_at, RunChildHandle.id) > (created_at, handle_id)
        )
    if active_only:
        statement = statement.where(
            RunChildHandle.revoked_at.is_(None), RunChildHandle.expires_at > utcnow()
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(RunChildHandle.created_at, RunChildHandle.id).limit(
                    bounded_limit + 1
                )
            )
        ).all()
    )
    has_more = len(rows) > bounded_limit
    page = rows[:bounded_limit]
    next_cursor = (
        _child_cursor(parent_run_id, page[-1].created_at.isoformat(), page[-1].id) if page else None
    )
    return ChildHandlePage(
        items=await _load_views(session, ctx, page),
        next_cursor=next_cursor,
        has_more=has_more,
    )
