"""Visibility of work by workspace (CP-ADR-0082 §2-3).

The mode is a property of the principal, not of the credential it entered
with: a human is in ``members`` mode when at least one of their active IAM
bindings says so, whichever identity or API key carries the request —
otherwise a second entry would bypass the restriction. A human in ``members``
mode sees the workspaces they are a member of and every descendant of those.

Both are read per request, never from the binding cache: the cache is keyed
by identity, the mode by principal, and a change of membership or of any
binding takes effect with the next request in every process. Whether the
principal is a human is read from the local principal too, not from the
``principal_type`` claim of a token: an identity of a human claiming to be an
agent does not leave the narrowing behind.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from sqlalchemy import ColumnElement, exists, false, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    VISIBILITY_MEMBERS,
    VISIBILITY_TENANT,
    AuthContext,
)
from control_plane.domain.enums import PrincipalKind
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    IamPrincipalBinding,
    Principal,
    Task,
)

# The visible workspaces (down from the membership) and the roots of their
# trees (up from it), in one statement; ``root`` tells the two apart.
_VISIBLE_WORKSPACES = text(
    """
    WITH RECURSIVE membership AS (
        SELECT w.id, w.parent_id FROM workspaces w
        JOIN workspace_members m ON m.workspace_id = w.id
        WHERE m.principal_id = :principal AND m.tenant_id = :tenant AND w.tenant_id = :tenant
    ),
    visible AS (
        SELECT id FROM membership
        UNION
        SELECT w.id FROM workspaces w JOIN visible v ON w.parent_id = v.id
        WHERE w.tenant_id = :tenant
    ),
    above AS (
        SELECT id, parent_id FROM membership
        UNION
        SELECT w.id, w.parent_id FROM workspaces w JOIN above a ON w.id = a.parent_id
        WHERE w.tenant_id = :tenant
    )
    SELECT id, false AS root FROM visible
    UNION ALL
    SELECT id, true AS root FROM above WHERE parent_id IS NULL
    """
)


async def principal_visibility(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> str:
    """``members`` if the principal is a human and any active binding says so.

    A human with no active binding left — their API keys are the only way in
    — keeps the narrowest visibility any of their bindings recorded: revoking
    the one ``members`` binding does not widen the keys to the whole tenant
    (CP-ADR-0082 V1).
    """
    binding = IamPrincipalBinding
    of_principal = (binding.tenant_id == tenant_id, binding.principal_id == principal_id)
    active = (binding.status == "active", binding.revoked_at.is_(None))
    human = exists().where(
        Principal.id == principal_id,
        Principal.tenant_id == tenant_id,
        Principal.kind == PrincipalKind.HUMAN,
    )
    narrowed = await session.scalar(
        select(
            human
            & or_(
                exists().where(*of_principal, *active, binding.visibility == VISIBILITY_MEMBERS),
                ~exists().where(*of_principal, *active)
                & exists().where(*of_principal, binding.visibility == VISIBILITY_MEMBERS),
            )
        )
    )
    return VISIBILITY_MEMBERS if narrowed else VISIBILITY_TENANT


async def member_workspaces(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> tuple[frozenset[str], frozenset[str]]:
    """Workspaces of the principal's membership and all their descendants,
    and the roots of the trees they belong to."""
    rows = await session.execute(
        _VISIBLE_WORKSPACES, {"principal": principal_id, "tenant": tenant_id}
    )
    visible: set[str] = set()
    roots: set[str] = set()
    for workspace_id, root in rows:
        (roots if root else visible).add(str(workspace_id))
    return frozenset(visible), frozenset(roots)


async def with_visibility(session: AsyncSession, ctx: AuthContext) -> AuthContext:
    """The context with the visibility of its principal resolved for this request.

    Agents and services are always ``tenant``: their visibility is set by
    their description in a package (CP-ADR-0073). Their kind is the local
    principal's, read with the mode, never ``ctx.principal_kind``, which an
    IAM token's claim may have set.
    """
    mode = await principal_visibility(session, ctx.tenant_id, ctx.principal_id)
    if mode == VISIBILITY_TENANT:
        return ctx
    workspaces, roots = await member_workspaces(session, ctx.tenant_id, ctx.principal_id)
    return dataclasses.replace(
        ctx, visibility=mode, visible_workspaces=workspaces, visible_roots=roots
    )


async def refresh_visibility(session: AsyncSession, ctx: AuthContext) -> AuthContext:
    """The context with its visibility read again: a long-lived reader (the
    event stream) follows a change of mode or membership like the next
    request would (CP-ADR-0082 §3.3)."""
    base = dataclasses.replace(
        ctx, visibility=VISIBILITY_TENANT, visible_workspaces=None, visible_roots=frozenset()
    )
    return await with_visibility(session, base)


# --- the seam for objects that live in a workspace (CP-ADR-0082 §3.7, T004) ---
#
# Each helper is a no-op in ``tenant`` mode: no extra statement, no extra
# condition. Lists filter by these conditions, resolvers by the predicates,
# and both answer for an invisible object exactly as for a missing one.


def _visible_ids(ctx: AuthContext) -> list[uuid.UUID]:
    assert ctx.visible_workspaces is not None
    return [uuid.UUID(w) for w in ctx.visible_workspaces]


def workspace_condition(
    ctx: AuthContext, column: Any, *, tenant_level: bool = False
) -> ColumnElement[bool]:
    """``column`` (a workspace id) is visible to the caller.

    ``tenant_level``: a row without a workspace is an object of the tenant
    and stays visible (goals, rules, approvals without work); otherwise it is
    not, as work without a workspace (CP-ADR-0082 B2).
    """
    if ctx.visible_workspaces is None:
        return true()
    in_set = column.in_(_visible_ids(ctx)) if ctx.visible_workspaces else false()
    return or_(column.is_(None), in_set) if tenant_level else in_set


def visible_tasks(ctx: AuthContext) -> Any:
    """Ids of the tenant's work in the visible workspaces, as a subquery."""
    return select(Task.id).where(
        Task.tenant_id == ctx.tenant_id, workspace_condition(ctx, Task.workspace_id)
    )


def task_condition(
    ctx: AuthContext, column: Any, *, tenant_level: bool = False
) -> ColumnElement[bool]:
    """``column`` (a task id) names visible work; ``NULL`` per ``tenant_level``."""
    if ctx.visible_workspaces is None:
        return true()
    in_set = column.in_(visible_tasks(ctx))
    return or_(column.is_(None), in_set) if tenant_level else in_set


async def task_visible(session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID | None) -> bool:
    """Is the work ``task_id`` visible? ``None`` is no work: not visible."""
    if ctx.visible_workspaces is None:
        return True
    if task_id is None:
        return False
    workspace_id = await session.scalar(
        select(Task.workspace_id).where(Task.id == task_id, Task.tenant_id == ctx.tenant_id)
    )
    return ctx.sees_workspace(workspace_id)


def approval_condition(ctx: AuthContext) -> ColumnElement[bool]:
    """An approval is seen in its workspace and on its work; without either
    it is an object of the tenant (CP-ADR-0082 §4)."""
    if ctx.visible_workspaces is None:
        return true()
    return workspace_condition(ctx, Approval.workspace_id, tenant_level=True) & task_condition(
        ctx, Approval.task_id, tenant_level=True
    )


async def approval_visible(session: AsyncSession, ctx: AuthContext, approval: Approval) -> bool:
    if ctx.visible_workspaces is None:
        return True
    if approval.workspace_id is not None and not ctx.sees_workspace(approval.workspace_id):
        return False
    return approval.task_id is None or await task_visible(session, ctx, approval.task_id)


def artifact_condition(ctx: AuthContext) -> ColumnElement[bool]:
    """An artifact is seen in the workspace of its work, or in its own; one
    with neither is an object of the tenant."""
    if ctx.visible_workspaces is None:
        return true()
    return workspace_condition(ctx, Artifact.workspace_id, tenant_level=True) & task_condition(
        ctx, Artifact.task_id, tenant_level=True
    )


async def artifact_visible(session: AsyncSession, ctx: AuthContext, artifact: Artifact) -> bool:
    if ctx.visible_workspaces is None:
        return True
    if artifact.workspace_id is not None and not ctx.sees_workspace(artifact.workspace_id):
        return False
    return artifact.task_id is None or await task_visible(session, ctx, artifact.task_id)
