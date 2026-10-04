"""Task requirements and claim eligibility.

Model (v0.2, deliberately simple): every listed requirement is mandatory
(AND semantics across and within kinds). A principal is eligible when:

- for every required role: an assignment exists whose scope is tenant-wide
  (workspace_id IS NULL) or an ancestor-or-self of the task's workspace
  (a role granted on ``engineering`` applies in ``engineering/platform``);
- for every required capability: the principal has it assigned;
- for every required skill: the principal has a skill with the same NAME
  assigned (any non-disabled registry version) — or, when the requirement
  pins ``name@version`` (v0.3), that exact non-disabled version.

Eligibility is organizational and is checked IN ADDITION to API
authorization (``tasks.claim``), task readiness and concurrency rules.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.application.common import new_uuid, utcnow
from control_plane.domain.enums import RequirementKind, SkillStatus
from control_plane.domain.errors import AuthorizationError, ValidationError
from control_plane.infrastructure.db.models import (
    Capability,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    Role,
    Skill,
    Task,
    TaskRequirement,
)


@dataclass(frozen=True)
class RequirementSpec:
    """Requirement lists as supplied by the API (slugs/names, not ids).

    A skill entry is either ``name`` (any non-disabled version qualifies) or
    ``name@version`` (that exact version is pinned).
    """

    roles: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.roles or self.capabilities or self.skills)


def parse_skill_ref(ref: str) -> tuple[str, str | None]:
    """Split ``name`` / ``name@version`` into (name, version|None)."""
    name, sep, version = ref.partition("@")
    if not sep:
        return ref, None
    if not name or not version:
        raise ValidationError(
            "invalid_requirement",
            f"Malformed skill reference '{ref}' (expected name or name@version)",
        )
    return name, version


async def resolve_role(
    session: AsyncSession,
    ctx: AuthContext,
    slug: str,
    scope_ids: list[uuid.UUID],
) -> Role:
    """Resolve a role slug: nearest workspace scope wins, tenant-global last."""
    candidates = (
        await session.scalars(
            select(Role).where(
                Role.tenant_id == ctx.tenant_id,
                Role.slug == slug,
                Role.workspace_id.is_(None) | Role.workspace_id.in_(scope_ids)
                if scope_ids
                else Role.workspace_id.is_(None),
            )
        )
    ).all()
    if not candidates:
        raise ValidationError(
            "unknown_requirement", f"Role '{slug}' not found in task scope", details={"role": slug}
        )
    by_workspace = {role.workspace_id: role for role in candidates}
    for workspace_id in scope_ids:  # nearest first
        if workspace_id in by_workspace:
            return by_workspace[workspace_id]
    return by_workspace[None]


async def set_task_requirements(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    spec: RequirementSpec,
) -> None:
    """Replace the task's requirement set (task row is already locked)."""
    existing = (
        await session.scalars(select(TaskRequirement).where(TaskRequirement.task_id == task.id))
    ).all()
    for requirement in existing:
        await session.delete(requirement)
    await session.flush()

    scope_ids = (
        await workspace_ancestor_ids(session, ctx.tenant_id, task.workspace_id)
        if task.workspace_id
        else []
    )
    now = utcnow()

    for slug in dict.fromkeys(spec.roles):  # de-dup, keep order
        role = await resolve_role(session, ctx, slug, scope_ids)
        session.add(
            TaskRequirement(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                task_id=task.id,
                kind=RequirementKind.ROLE,
                role_id=role.id,
                created_at=now,
            )
        )
    for name in dict.fromkeys(spec.capabilities):
        capability = await session.scalar(
            select(Capability).where(Capability.tenant_id == ctx.tenant_id, Capability.name == name)
        )
        if capability is None:
            raise ValidationError(
                "unknown_requirement",
                f"Capability '{name}' is not registered",
                details={"capability": name},
            )
        session.add(
            TaskRequirement(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                task_id=task.id,
                kind=RequirementKind.CAPABILITY,
                capability_id=capability.id,
                created_at=now,
            )
        )
    for ref in dict.fromkeys(spec.skills):
        name, version = parse_skill_ref(ref)
        if version is not None:
            skill = await session.scalar(
                select(Skill).where(
                    Skill.tenant_id == ctx.tenant_id,
                    Skill.name == name,
                    Skill.version == version,
                    Skill.status != SkillStatus.DISABLED,
                )
            )
        else:
            # Default resolution: the newest ACTIVE version, falling back to
            # the newest non-disabled one (a deprecated-only skill still works).
            skill = await session.scalar(
                select(Skill)
                .where(
                    Skill.tenant_id == ctx.tenant_id,
                    Skill.name == name,
                    Skill.status == SkillStatus.ACTIVE,
                )
                .order_by(Skill.created_at.desc())
                .limit(1)
            )
            if skill is None:
                skill = await session.scalar(
                    select(Skill)
                    .where(
                        Skill.tenant_id == ctx.tenant_id,
                        Skill.name == name,
                        Skill.status != SkillStatus.DISABLED,
                    )
                    .order_by(Skill.created_at.desc())
                    .limit(1)
                )
        if skill is None:
            raise ValidationError(
                "unknown_requirement",
                f"Skill '{ref}' is not registered",
                details={"skill": ref},
            )
        session.add(
            TaskRequirement(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                task_id=task.id,
                kind=RequirementKind.SKILL,
                skill_id=skill.id,
                skill_exact=version is not None,
                created_at=now,
            )
        )
    await session.flush()


async def explain_claim_eligibility(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    principal_id: uuid.UUID,
) -> dict[str, list[str]] | None:
    """Return the missing requirements, or None when the principal is eligible.

    Shared by the authoritative claim gate (which raises) and advisory work
    discovery (which filters/annotates).
    """
    requirements = (
        await session.scalars(select(TaskRequirement).where(TaskRequirement.task_id == task.id))
    ).all()
    if not requirements:
        return None

    required_role_ids = [r.role_id for r in requirements if r.role_id is not None]
    required_capability_ids = [r.capability_id for r in requirements if r.capability_id is not None]
    exact_skill_ids = [r.skill_id for r in requirements if r.skill_id is not None and r.skill_exact]
    named_skill_ids = [
        r.skill_id for r in requirements if r.skill_id is not None and not r.skill_exact
    ]

    missing_roles: list[str] = []
    missing_capabilities: list[str] = []
    missing_skills: list[str] = []

    if required_role_ids:
        scope_ids = (
            await workspace_ancestor_ids(session, ctx.tenant_id, task.workspace_id)
            if task.workspace_id
            else []
        )
        scope_filter: ColumnElement[bool] = PrincipalRole.workspace_id.is_(None)
        if scope_ids:
            scope_filter = scope_filter | PrincipalRole.workspace_id.in_(scope_ids)
        held = set(
            (
                await session.scalars(
                    select(PrincipalRole.role_id).where(
                        PrincipalRole.principal_id == principal_id,
                        PrincipalRole.role_id.in_(required_role_ids),
                        scope_filter,
                    )
                )
            ).all()
        )
        for role_id in required_role_ids:
            if role_id not in held:
                slug = await session.scalar(select(Role.slug).where(Role.id == role_id))
                missing_roles.append(slug or str(role_id))

    if required_capability_ids:
        held = set(
            (
                await session.scalars(
                    select(PrincipalCapability.capability_id).where(
                        PrincipalCapability.principal_id == principal_id,
                        PrincipalCapability.capability_id.in_(required_capability_ids),
                    )
                )
            ).all()
        )
        for capability_id in required_capability_ids:
            if capability_id not in held:
                name = await session.scalar(
                    select(Capability.name).where(Capability.id == capability_id)
                )
                missing_capabilities.append(name or str(capability_id))

    if named_skill_ids:
        required_names: dict[uuid.UUID, str] = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(Skill.id, Skill.name).where(Skill.id.in_(named_skill_ids))
                )
            ).all()
        }
        # By-name skill eligibility: any assigned, non-disabled version
        # satisfies it (a disabled version is unusable everywhere else in the
        # model, so it cannot satisfy a requirement either).
        held_names = set(
            (
                await session.scalars(
                    select(Skill.name)
                    .select_from(PrincipalSkill)
                    .join(Skill, Skill.id == PrincipalSkill.skill_id)
                    .where(
                        PrincipalSkill.principal_id == principal_id,
                        Skill.name.in_(list(required_names.values())),
                        Skill.status != SkillStatus.DISABLED,
                    )
                )
            ).all()
        )
        for skill_id in named_skill_ids:
            name = required_names.get(skill_id, str(skill_id))
            if name not in held_names:
                missing_skills.append(name)

    if exact_skill_ids:
        # Pinned skill eligibility: that exact assigned version, not disabled.
        held_exact = set(
            (
                await session.scalars(
                    select(PrincipalSkill.skill_id)
                    .join(Skill, Skill.id == PrincipalSkill.skill_id)
                    .where(
                        PrincipalSkill.principal_id == principal_id,
                        PrincipalSkill.skill_id.in_(exact_skill_ids),
                        Skill.status != SkillStatus.DISABLED,
                    )
                )
            ).all()
        )
        for skill_id in exact_skill_ids:
            if skill_id not in held_exact:
                row = (
                    await session.execute(
                        select(Skill.name, Skill.version).where(Skill.id == skill_id)
                    )
                ).first()
                missing_skills.append(f"{row[0]}@{row[1]}" if row else str(skill_id))

    if missing_roles or missing_capabilities or missing_skills:
        return {
            "missingRoles": missing_roles,
            "missingCapabilities": missing_capabilities,
            "missingSkills": missing_skills,
        }
    return None


async def check_claim_eligibility(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    principal_id: uuid.UUID,
) -> None:
    """Raise 403 not_eligible unless the principal satisfies ALL requirements."""
    missing = await explain_claim_eligibility(session, ctx, task, principal_id)
    if missing is not None:
        raise AuthorizationError(
            "Principal does not satisfy the task's requirements",
            code="not_eligible",
            details={"taskId": str(task.id), **missing},
        )
