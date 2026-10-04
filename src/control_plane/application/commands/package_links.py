"""Link catalog objects to the package that installed them: ``POST /packages:record``.

CP-ADR-0074 §11, amendment TASK-000904 (:mod:`control_plane.domain.package_links`).
The installer (package-sdk) applies every kind but the engine's
(``Process``, ``Calendar``) — task types, agents and rules until it moves to
plan and apply — through their own routes and then names, per package, every object it applied — the
unchanged ones too: an upgrade of the package moves their link to its new
version. The core refuses objects it does not hold and the kinds it records
itself (``Process``, ``Calendar`` by ``POST /packages:apply``).

Right: ``packages.plan`` and, per kind named, the right that writes that kind
(a link says who owns an object; it is written by whoever may write it).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.package_links import LINKED_KINDS, RECORDED_KINDS
from control_plane.domain.work_rules import RuleStatus
from control_plane.infrastructure.db.models import (
    Agent,
    ArtifactType,
    Base,
    Capability,
    ConnectionType,
    PackageObject,
    ProjectTemplate,
    Role,
    Skill,
    TaskType,
    WorkRule,
    WorkspaceType,
)


@dataclass(frozen=True)
class _Kind:
    model: type[Base]
    key: InstrumentedAttribute[str]
    right: Permission


_KINDS: dict[str, _Kind] = {
    "ArtifactType": _Kind(ArtifactType, ArtifactType.key, Permission.ARTIFACT_TYPES_MANAGE),
    "TaskType": _Kind(TaskType, TaskType.key, Permission.TASK_TYPES_MANAGE),
    "ProjectTemplate": _Kind(
        ProjectTemplate, ProjectTemplate.key, Permission.PROJECT_TEMPLATES_MANAGE
    ),
    "WorkspaceType": _Kind(WorkspaceType, WorkspaceType.key, Permission.WORKSPACES_MANAGE),
    "Role": _Kind(Role, Role.slug, Permission.ORG_MANAGE),
    "Capability": _Kind(Capability, Capability.name, Permission.ORG_MANAGE),
    "ConnectionType": _Kind(ConnectionType, ConnectionType.key, Permission.CONNECTIONS_MANAGE),
    "Skill": _Kind(Skill, Skill.name, Permission.ORG_MANAGE),
    "WorkRule": _Kind(WorkRule, WorkRule.key, Permission.RULES_WRITE),
    "Agent": _Kind(Agent, Agent.key, Permission.AGENTS_MANAGE),
}
assert tuple(_KINDS) == RECORDED_KINDS


async def link_object(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    kind: str,
    key: str,
    package_key: str,
    package_version: str | None,
    install_hash: str | None,
) -> None:
    """Upsert the link of ``(kind, key)``: the package, its version and installation.

    Only for the kinds the installer records: the rows of a process or a
    calendar carry what the apply published and are written by it. A task
    type, an agent or a rule an apply wrote before forgets what it wanted:
    the installer applied the object through its route after it.
    """
    assert kind in RECORDED_KINDS, kind
    now = utcnow()
    values = {
        "version": None,
        "spec": None,
        "spec_hash": None,
        "package_key": package_key,
        "package_version": package_version,
        "plan_hash": install_hash,
        "applied_by": ctx.principal_id,
        "applied_at": now,
    }
    stmt = insert(PackageObject).values(
        id=new_uuid(), tenant_id=ctx.tenant_id, kind=kind, key=key, **values
    )
    await session.execute(
        stmt.on_conflict_do_update(constraint="uq_package_objects_tenant_kind_key", set_=values)
    )


async def lock_applies(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """One ``packages:apply`` or ``packages:record`` of a tenant at a time.

    Two applies: each plans on the catalog the previous one left. An apply
    and a record: the apply holds the links of its plan ``FOR UPDATE`` and
    inserts the missing ones last, a record writes the same links one by one
    in the order of the catalog kinds; without the lock they would meet in
    another order (``40P01``).
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:packages:{tenant_id}", 0)))
    )


async def _existing(session: AsyncSession, ctx: AuthContext, kind: str, keys: set[str]) -> set[str]:
    spec = _KINDS[kind]
    stmt = select(spec.key).where(spec.key.in_(sorted(keys)))
    stmt = stmt.where(spec.model.tenant_id == ctx.tenant_id)  # type: ignore[attr-defined]
    if kind == "Role":
        stmt = stmt.where(Role.workspace_id.is_(None))
    return set(await session.scalars(stmt.distinct()))


async def _authorize_rules(session: AsyncSession, ctx: AuthContext, keys: set[str]) -> None:
    """``rules.write`` where each live rule of the keys lives (its workspace or the tenant)."""
    scopes = await session.scalars(
        select(WorkRule.workspace_id)
        .where(
            WorkRule.tenant_id == ctx.tenant_id,
            WorkRule.key.in_(sorted(keys)),
            WorkRule.status != RuleStatus.ARCHIVED,
        )
        .distinct()
    )
    for workspace_id in scopes:
        # As work_rules.rule_scope (not imported: work_rules imports agents, which links).
        scope = ResourceRef("workspace", str(workspace_id)) if workspace_id else None
        await authorize(ctx, Permission.RULES_WRITE, resource=scope)


async def record_package(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    package_key: str,
    package_version: str,
    install_hash: str | None,
    objects: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """``POST /packages:record``: link every named object to the package, or none."""
    await authorize(ctx, Permission.PACKAGES_PLAN)
    await lock_applies(session, ctx.tenant_id)
    by_kind: dict[str, set[str]] = {}
    refused: list[dict[str, str]] = []
    for kind, key in objects:
        if kind in RECORDED_KINDS:
            by_kind.setdefault(kind, set()).add(key)
        else:
            refused.append({"kind": kind, "key": key})
    if refused:
        raise ValidationError(
            "invalid_request",
            "Only the kinds the installer applies are recorded: processes and calendars are"
            " linked by POST /packages:apply, other kinds are not in the core's catalog",
            details={"objects": refused, "kinds": list(RECORDED_KINDS)},
        )
    for kind in by_kind:
        if kind == "WorkRule":
            await _authorize_rules(session, ctx, by_kind[kind])
        else:
            await authorize(ctx, _KINDS[kind].right)
    missing: list[dict[str, str]] = []
    for kind, keys in sorted(by_kind.items()):
        found = await _existing(session, ctx, kind, keys)
        missing.extend({"kind": kind, "key": key} for key in sorted(keys - found))
    if missing:
        raise ValidationError(
            "unknown_object",
            "The tenant's catalog has no such object: apply it first, then record it",
            details={"objects": missing},
        )
    recorded = []
    for kind in LINKED_KINDS:
        for key in sorted(by_kind.get(kind, ())):
            await link_object(
                session,
                ctx,
                kind=kind,
                key=key,
                package_key=package_key,
                package_version=package_version,
                install_hash=install_hash,
            )
            recorded.append({"kind": kind, "key": key})
    return {
        "package": {"key": package_key, "version": package_version},
        "installHash": install_hash,
        "recorded": recorded,
    }
