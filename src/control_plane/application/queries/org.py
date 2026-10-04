"""Read-side queries for the organization model."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.queries.lists import Page, _paginate, clamp_limit
from control_plane.application.queries.package_links import in_package
from control_plane.application.visibility import workspace_condition
from control_plane.domain.enums import Permission, WorkspaceStatus
from control_plane.domain.errors import AuthorizationError, NotFoundError, ValidationError
from control_plane.infrastructure.db.models import (
    Capability,
    Principal,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    Role,
    Skill,
    Workspace,
    WorkspaceMember,
)


async def list_workspaces(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    parent_id: uuid.UUID | None = None,
    roots_only: bool = False,
    status: str | None = None,
) -> Page[Workspace]:
    await authorize(ctx, Permission.WORKSPACES_READ)
    if status is not None and status not in set(WorkspaceStatus):
        raise ValidationError("invalid_status", f"Unknown workspace status: {status}")
    stmt = select(Workspace).where(
        Workspace.tenant_id == ctx.tenant_id, workspace_condition(ctx, Workspace.id)
    )
    if parent_id is not None:
        stmt = stmt.where(Workspace.parent_id == parent_id)
    elif roots_only:
        # A visible workspace whose parent is not visible is a root to the
        # caller (CP-ADR-0082 §3.9).
        stmt = stmt.where(
            or_(
                Workspace.parent_id.is_(None),
                ~workspace_condition(ctx, Workspace.parent_id),
            )
        )
    if status is not None:
        stmt = stmt.where(Workspace.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=Workspace.created_at,
        id_col=Workspace.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_workspace(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> Workspace:
    await authorize(ctx, Permission.WORKSPACES_READ)
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id)
    )
    # An invisible workspace answers as a missing one (CP-ADR-0082 §3.6).
    if workspace is None or not ctx.sees_workspace(workspace.id):
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    return workspace


async def list_workspace_members(
    session: AsyncSession,
    ctx: AuthContext,
    workspace_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[WorkspaceMember]:
    await authorize(ctx, Permission.WORKSPACES_READ)
    await get_workspace(session, ctx, workspace_id)
    stmt = select(WorkspaceMember).where(
        WorkspaceMember.tenant_id == ctx.tenant_id,
        WorkspaceMember.workspace_id == workspace_id,
    )
    return await _paginate(
        session,
        stmt,
        created_col=WorkspaceMember.created_at,
        id_col=WorkspaceMember.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


@dataclass(frozen=True)
class ParticipantRole:
    """A role assignment that makes a principal a participant of a workspace."""

    role_id: uuid.UUID
    slug: str
    name: str
    role_workspace_id: uuid.UUID | None
    assignment_workspace_id: uuid.UUID | None


@dataclass(frozen=True)
class WorkspaceParticipant:
    principal_id: uuid.UUID
    kind: str
    display_name: str
    status: str
    member: bool
    roles: list[ParticipantRole]


async def list_workspace_participants(
    session: AsyncSession,
    ctx: AuthContext,
    workspace_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[WorkspaceParticipant]:
    """Explicit members of the workspace and holders of its roles (CP-ADR-0010
    amendment): ``member`` tells the two apart, ``roles`` names the assignments.

    A role assignment makes a participant when it counts in the workspace by
    the rule of ``GET /roles/{id}/principals?workspaceId=`` (CP-ADR-0068) and
    either the role belongs to this workspace or the assignment is scoped to it.
    """
    await authorize_participants_read(ctx)
    await get_workspace(session, ctx, workspace_id)
    rule = await _participant_rule(session, ctx.tenant_id, workspace_id)
    stmt = select(Principal).where(Principal.tenant_id == ctx.tenant_id, rule.condition)
    page = await _paginate(
        session,
        stmt,
        created_col=Principal.created_at,
        id_col=Principal.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )
    return Page(
        items=await _describe_participants(session, ctx.tenant_id, rule, page.items),
        next_cursor=page.next_cursor,
    )


async def authorize_participants_read(ctx: AuthContext) -> None:
    """The right to read who takes part in a workspace."""
    await authorize(ctx, Permission.WORKSPACES_READ)
    # Who holds which role is org data, as in GET /roles/{id}/principals.
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)


async def workspace_participants(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, kind: str
) -> list[WorkspaceParticipant]:
    """Every participant of ``kind``, unpaged; the caller has authorized the read."""
    rule = await _participant_rule(session, tenant_id, workspace_id)
    principals = (
        await session.scalars(
            select(Principal)
            .where(Principal.tenant_id == tenant_id, Principal.kind == kind, rule.condition)
            .order_by(Principal.created_at, Principal.id)
        )
    ).all()
    return await _describe_participants(session, tenant_id, rule, principals)


@dataclass(frozen=True)
class _ParticipantRule:
    workspace_id: uuid.UUID
    # Assignments that count in the workspace, and those that make a participant.
    scope: ColumnElement[bool]
    role_here: ColumnElement[bool]
    condition: ColumnElement[bool]


async def _participant_rule(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> _ParticipantRule:
    scope = await role_assignment_scope(session, tenant_id, workspace_id)
    role_here = or_(Role.workspace_id == workspace_id, PrincipalRole.workspace_id == workspace_id)
    is_member = exists().where(
        WorkspaceMember.tenant_id == tenant_id,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.principal_id == Principal.id,
    )
    holds_role = (
        exists()
        .where(
            PrincipalRole.principal_id == Principal.id,
            PrincipalRole.tenant_id == tenant_id,
            scope,
            role_here,
        )
        .where(Role.id == PrincipalRole.role_id)
    )
    return _ParticipantRule(workspace_id, scope, role_here, or_(is_member, holds_role))


async def _describe_participants(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rule: _ParticipantRule,
    principals: Sequence[Principal],
) -> list[WorkspaceParticipant]:
    ids = [p.id for p in principals]
    members: set[uuid.UUID] = set()
    roles: dict[uuid.UUID, list[ParticipantRole]] = {pid: [] for pid in ids}
    if ids:
        members = set(
            (
                await session.scalars(
                    select(WorkspaceMember.principal_id).where(
                        WorkspaceMember.tenant_id == tenant_id,
                        WorkspaceMember.workspace_id == rule.workspace_id,
                        WorkspaceMember.principal_id.in_(ids),
                    )
                )
            ).all()
        )
        rows = await session.execute(
            select(PrincipalRole, Role)
            .join(Role, Role.id == PrincipalRole.role_id)
            .where(
                PrincipalRole.tenant_id == tenant_id,
                PrincipalRole.principal_id.in_(ids),
                rule.scope,
                rule.role_here,
            )
            .order_by(PrincipalRole.created_at, PrincipalRole.id)
        )
        for assignment, role in rows.all():
            roles[assignment.principal_id].append(
                ParticipantRole(
                    role_id=role.id,
                    slug=role.slug,
                    name=role.name,
                    role_workspace_id=role.workspace_id,
                    assignment_workspace_id=assignment.workspace_id,
                )
            )
    return [
        WorkspaceParticipant(
            principal_id=p.id,
            kind=p.kind,
            display_name=p.display_name,
            status=p.status,
            member=p.id in members,
            roles=roles[p.id],
        )
        for p in principals
    ]


async def list_roles(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    workspace_id: uuid.UUID | None = None,
    package: str | None = None,
) -> Page[Role]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Role).where(Role.tenant_id == ctx.tenant_id)
    if workspace_id is not None:
        stmt = stmt.where(Role.workspace_id == workspace_id)
    if package is not None:
        # A package brings roles of the tenant, never of a workspace.
        stmt = stmt.where(
            Role.workspace_id.is_(None), in_package("Role", Role.tenant_id, Role.slug, package)
        )
    return await _paginate(
        session,
        stmt,
        created_col=Role.created_at,
        id_col=Role.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_role(session: AsyncSession, ctx: AuthContext, role_id: uuid.UUID) -> Role:
    await authorize(ctx, Permission.ORG_READ)
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id)
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    return role


async def role_assignment_scope(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> ColumnElement[bool]:
    """Role assignments that count in ``workspace_id``: tenant-wide ones and
    those scoped to the workspace or one of its ancestors. Without a workspace
    only tenant-wide assignments count — the rule of approval eligibility."""
    scope: ColumnElement[bool] = PrincipalRole.workspace_id.is_(None)
    if workspace_id is not None:
        ancestors = await workspace_ancestor_ids(session, tenant_id, workspace_id)
        if ancestors:
            scope = scope | PrincipalRole.workspace_id.in_(ancestors)
    return scope


async def list_role_holders(
    session: AsyncSession,
    ctx: AuthContext,
    role_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[Principal]:
    """Principals who hold the role in ``workspace_id`` (CP-ADR-0068).

    Exactly the principals eligible to decide an approval that requires this
    role in that workspace: an addressee list for whoever tells them about it.
    """
    # Tenant-level actions (authz/catalog.yaml): who holds a role is org data.
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id)
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    if workspace_id is not None:
        await get_tenant_workspace(session, ctx, workspace_id)
    scope = await role_assignment_scope(session, ctx.tenant_id, workspace_id)
    holds = exists().where(
        PrincipalRole.principal_id == Principal.id,
        PrincipalRole.tenant_id == ctx.tenant_id,
        PrincipalRole.role_id == role_id,
        scope,
    )
    stmt = select(Principal).where(Principal.tenant_id == ctx.tenant_id, holds)
    return await _paginate(
        session,
        stmt,
        created_col=Principal.created_at,
        id_col=Principal.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def list_capabilities(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    package: str | None = None,
) -> Page[Capability]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Capability).where(Capability.tenant_id == ctx.tenant_id)
    if package is not None:
        stmt = stmt.where(in_package("Capability", Capability.tenant_id, Capability.name, package))
    return await _paginate(
        session,
        stmt,
        created_col=Capability.created_at,
        id_col=Capability.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_capability(
    session: AsyncSession, ctx: AuthContext, capability_id: uuid.UUID
) -> Capability:
    await authorize(ctx, Permission.ORG_READ)
    capability = await session.scalar(
        select(Capability).where(
            Capability.id == capability_id, Capability.tenant_id == ctx.tenant_id
        )
    )
    if capability is None:
        raise NotFoundError("Capability not found", details={"capabilityId": str(capability_id)})
    return capability


async def list_skills(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    name: str | None = None,
    status: str | None = None,
    package: str | None = None,
) -> Page[Skill]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Skill).where(Skill.tenant_id == ctx.tenant_id)
    if name is not None:
        stmt = stmt.where(Skill.name == name)
    if status is not None:
        stmt = stmt.where(Skill.status == status)
    if package is not None:
        stmt = stmt.where(in_package("Skill", Skill.tenant_id, Skill.name, package))
    return await _paginate(
        session,
        stmt,
        created_col=Skill.created_at,
        id_col=Skill.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_skill_by_ref(session: AsyncSession, ctx: AuthContext, ref: str) -> tuple[Skill, bool]:
    """Read one version by id or reference; readable by whoever may call or
    execute skills too — a caller cannot honour a contract it cannot see.

    The second value says whether the caller may also see the legacy
    ``config`` (endpoints, headers of a catalog entry): that stays behind
    ``org.read``, as it was before skills became invocable.
    """
    from control_plane.application.commands.skill_invocations import resolve_skill_ref

    await authorize(ctx, Permission.ORG_READ, Permission.SKILLS_INVOKE, Permission.SKILLS_EXECUTE)
    skill = await resolve_skill_ref(session, ctx, ref)
    try:
        await authorize(ctx, Permission.ORG_READ)
    except AuthorizationError:
        return skill, False
    return skill, True


async def get_skill(session: AsyncSession, ctx: AuthContext, skill_id: uuid.UUID) -> Skill:
    await authorize(ctx, Permission.ORG_READ)
    skill = await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.tenant_id == ctx.tenant_id)
    )
    if skill is None:
        raise NotFoundError("Skill not found", details={"skillId": str(skill_id)})
    return skill


async def list_principal_roles(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalRole, Role]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalRole, Role)
        .join(Role, Role.id == PrincipalRole.role_id)
        .where(
            PrincipalRole.tenant_id == ctx.tenant_id,
            PrincipalRole.principal_id == principal_id,
        )
        .order_by(PrincipalRole.created_at)
    )
    return [(assignment, role) for assignment, role in rows.all()]


async def list_principal_capabilities(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalCapability, Capability]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalCapability, Capability)
        .join(Capability, Capability.id == PrincipalCapability.capability_id)
        .where(
            PrincipalCapability.tenant_id == ctx.tenant_id,
            PrincipalCapability.principal_id == principal_id,
        )
        .order_by(PrincipalCapability.created_at)
    )
    return [(assignment, capability) for assignment, capability in rows.all()]


async def list_principal_skills(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalSkill, Skill]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalSkill, Skill)
        .join(Skill, Skill.id == PrincipalSkill.skill_id)
        .where(
            PrincipalSkill.tenant_id == ctx.tenant_id,
            PrincipalSkill.principal_id == principal_id,
        )
        .order_by(PrincipalSkill.created_at)
    )
    return [(assignment, skill) for assignment, skill in rows.all()]
