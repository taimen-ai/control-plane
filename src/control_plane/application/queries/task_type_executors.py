"""Who may take a task type in a workspace (CP-ADR-0048, amendment 2026-10-03 A2).

The answer is advice to whoever assigns the work, not a rule: assigning a
person without an executor role of the type stays allowed (A3).

- people — participants of the workspace (``GET /workspaces/{id}/participants``)
  holding one of the type's ``executorRoles`` there; every participant when
  the type names none;
- agents and services — agents of the registry that take work (running, bound
  to an active principal, an ``executor`` or ``work`` in the current revision)
  whose ``spec.work.workspace`` is the workspace or an ancestor of it, or is
  not set, and whose ``spec.work.taskTypes`` names the type or is empty. A
  service principal outside the registry declares no work and is never one.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.task_types import get_tenant_task_type
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.queries.org import (
    authorize_participants_read,
    role_assignment_scope,
    workspace_participants,
)
from control_plane.domain.enums import AgentState, AgentStatus, PrincipalKind, PrincipalStatus
from control_plane.infrastructure.db.models import (
    Agent,
    AgentRevision,
    Principal,
    PrincipalRole,
    Role,
    TaskType,
)

# People first, then agents and services.
KIND_ORDER: dict[str, int] = {
    PrincipalKind.HUMAN: 0,
    PrincipalKind.AGENT: 1,
    PrincipalKind.SERVICE: 2,
}


@dataclass(frozen=True)
class TypeExecutor:
    principal_id: uuid.UUID
    kind: str
    display_name: str
    roles: list[str]
    reason: str


async def list_task_type_executors(
    session: AsyncSession, ctx: AuthContext, type_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[TypeExecutor]:
    # The right of the participants list; the console reads no /roles for this.
    await authorize_participants_read(ctx)
    task_type = await get_tenant_task_type(session, ctx, type_id)
    await get_tenant_workspace(session, ctx, workspace_id)
    # The workspace itself first, then its ancestors up to the root.
    ancestors = await workspace_ancestor_ids(session, ctx.tenant_id, workspace_id)
    found = [
        *await _people(session, ctx.tenant_id, task_type, workspace_id, ancestors),
        *await _agents(session, ctx.tenant_id, task_type, ancestors),
    ]
    return sorted(
        found, key=lambda e: (KIND_ORDER.get(e.kind, 3), e.display_name, str(e.principal_id))
    )


async def _people(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    task_type: TaskType,
    workspace_id: uuid.UUID,
    ancestors: list[uuid.UUID],
) -> list[TypeExecutor]:
    participants = [
        p
        for p in await workspace_participants(
            session, tenant_id, workspace_id, kind=PrincipalKind.HUMAN
        )
        if p.status == PrincipalStatus.ACTIVE
    ]
    wanted: list[str] = list(task_type.executor_roles or [])
    if not wanted:
        return [
            TypeExecutor(
                p.principal_id,
                p.kind,
                p.display_name,
                list(dict.fromkeys(r.slug for r in p.roles)),
                "any",
            )
            for p in participants
        ]
    resolved = await _resolve_roles(session, tenant_id, wanted, ancestors)
    if not participants or not resolved:
        return []
    scope = await role_assignment_scope(session, tenant_id, workspace_id)
    held: dict[uuid.UUID, set[str]] = {}
    rows = await session.execute(
        select(PrincipalRole.principal_id, PrincipalRole.role_id).where(
            PrincipalRole.tenant_id == tenant_id,
            PrincipalRole.principal_id.in_([p.principal_id for p in participants]),
            PrincipalRole.role_id.in_(resolved),
            scope,
        )
    )
    for principal_id, role_id in rows.all():
        held.setdefault(principal_id, set()).add(resolved[role_id])
    return [
        TypeExecutor(
            p.principal_id,
            p.kind,
            p.display_name,
            [slug for slug in wanted if slug in held[p.principal_id]],
            "role",
        )
        for p in participants
        if p.principal_id in held
    ]


async def _resolve_roles(
    session: AsyncSession, tenant_id: uuid.UUID, slugs: list[str], ancestors: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """``{role id: slug}`` of the roles the slugs name in the workspace.

    Like a ``role:<slug>`` gate of a task there: the role of the workspace or
    of its nearest ancestor before a tenant-wide one; a slug with no role in
    this scope names none.
    """
    candidates = (
        await session.scalars(
            select(Role).where(
                Role.tenant_id == tenant_id,
                Role.slug.in_(slugs),
                or_(Role.workspace_id.is_(None), Role.workspace_id.in_(ancestors)),
            )
        )
    ).all()
    depth: dict[uuid.UUID | None, int] = {ws: index for index, ws in enumerate(ancestors)}
    nearest: dict[str, Role] = {}
    # A tenant-wide role (no workspace) ranks after every workspace of the scope.
    for role in sorted(candidates, key=lambda r: depth.get(r.workspace_id, len(ancestors))):
        nearest.setdefault(role.slug, role)
    return {role.id: slug for slug, role in nearest.items()}


async def _agents(
    session: AsyncSession, tenant_id: uuid.UUID, task_type: TaskType, ancestors: list[uuid.UUID]
) -> list[TypeExecutor]:
    rows = await session.execute(
        select(Agent, AgentRevision.spec, Principal)
        .join(
            AgentRevision,
            (AgentRevision.agent_id == Agent.id)
            & (AgentRevision.revision == Agent.current_revision),
        )
        .join(Principal, Principal.id == Agent.principal_id)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.status == AgentStatus.ACTIVE,
            Agent.state == AgentState.RUNNING,
            Principal.status == PrincipalStatus.ACTIVE,
            # An ancestor's agent covers the workspace; none — the whole tenant.
            or_(Agent.workspace_id.is_(None), Agent.workspace_id.in_(ancestors)),
        )
    )
    found: list[TypeExecutor] = []
    for agent, spec, principal in rows.all():
        reason = _agent_reason(spec, task_type.key)
        if reason is not None:
            found.append(TypeExecutor(principal.id, principal.kind, agent.display_name, [], reason))
    return found


def _agent_reason(spec: dict[str, Any], type_key: str) -> str | None:
    """Why the agent of ``spec`` takes the type, or None if it does not."""
    work = spec.get("work")
    if work is None and spec.get("executor") is None:
        # A service account without work (placement: none): it takes nothing.
        return None
    task_types = (work or {}).get("taskTypes") or []
    if not task_types:
        return "agent_any"
    return "agent_task_types" if type_key in task_types else None
