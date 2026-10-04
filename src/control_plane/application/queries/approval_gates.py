"""Pending gate approvals on a task (ADR-0018).

A read of its own so that the commands which consult a gate (claim, skill
invocation, discovery) do not import the approvals command module, which in
turn imports the outcome executor (CP-ADR-0061).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.visibility import approval_condition
from control_plane.domain.enums import ApprovalStatus
from control_plane.domain.errors import ConflictError
from control_plane.infrastructure.db.models import Approval


async def pending_gate_approvals(
    session: AsyncSession, tenant_id: uuid.UUID, task_id: uuid.UUID
) -> list[dict[str, str]]:
    """Pending gate approvals holding a task (empty list = no gate)."""
    rows = (
        await session.execute(
            select(Approval.id, Approval.requested_by_principal_id).where(
                Approval.tenant_id == tenant_id,
                Approval.task_id == task_id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).all()
    return [{"approvalId": str(row[0]), "requestedBy": str(row[1])} for row in rows]


async def shown_gate_approvals(
    session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID
) -> tuple[list[dict[str, str]], int]:
    """The pending gates of a task as the caller may see them, and how many it may not.

    A gate in a workspace outside the caller's visibility (an ancestor of the
    task's) still holds the task, but is not named (CP-ADR-0082 V4).
    """
    rows = (
        await session.execute(
            select(Approval.id, Approval.requested_by_principal_id, approval_condition(ctx)).where(
                Approval.tenant_id == ctx.tenant_id,
                Approval.task_id == task_id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).all()
    shown = [{"approvalId": str(row[0]), "requestedBy": str(row[1])} for row in rows if row[2]]
    return shown, len(rows) - len(shown)


def gate_reason(pending: list[dict[str, str]], hidden: int) -> dict[str, Any]:
    """``pendingApprovals`` and, when some are not shown, their count."""
    reason: dict[str, Any] = {"pendingApprovals": pending}
    if hidden:
        reason["hiddenApprovals"] = hidden
    return reason


async def require_open_gates(session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID) -> None:
    """Raise 409 approval_required while a pending gate approval holds the task."""
    pending, hidden = await shown_gate_approvals(session, ctx, task_id)
    if pending or hidden:
        raise ConflictError(
            "approval_required",
            "Task is waiting for a pending gate approval",
            details={"taskId": str(task_id), **gate_reason(pending, hidden)},
        )
