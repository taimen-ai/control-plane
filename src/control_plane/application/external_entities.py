"""Entity bindings: which internal entities may carry external references.

``external_references`` is generic by construction (``entity_type`` +
``entity_id``, ADR-0034), but the type must never be taken from the client as
free text. The value decides three things at once — which table the reference
resolves into, which permission governs it, and which slice of the tenant's
external-key namespace it occupies — so an unknown type is rejected before any
write instead of being stored and worried about later (ADR-0047).

Adding a work item type here is the whole extension point: a binding is a
loader plus the pair of permissions that already govern that entity.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.visibility import task_condition, workspace_condition
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import ProjectProfile, Task

# An entity reference arrives as text (a UUID, or a public id such as
# ``TASK-000026``). Bound so a pathological value never reaches the database.
MAX_ENTITY_REF_LENGTH = 128

EntityLoader = Callable[[AsyncSession, AuthContext, str], Awaitable[uuid.UUID]]


@dataclass(frozen=True)
class EntityBinding:
    """One entity type that may be the target of an external reference."""

    entity_type: str
    read_permission: Permission
    manage_permission: Permission
    loader: EntityLoader
    # Entity ids of this type the caller sees (CP-ADR-0082 §3.7): a reverse
    # lookup never hands out an entity outside the visible workspaces.
    visible: Callable[[AuthContext, Any], ColumnElement[bool]]


async def _load_project(session: AsyncSession, ctx: AuthContext, entity_ref: str) -> uuid.UUID:
    """Resolve a project by UUID.

    The not-found shape is deliberately the one the project endpoints already
    return, so routing them through the registry does not change their contract.
    """
    missing = NotFoundError("Project not found", details={"projectId": entity_ref})
    try:
        project_id = uuid.UUID(entity_ref)
    except ValueError:
        raise missing from None
    found = (
        await session.execute(
            select(ProjectProfile.id, ProjectProfile.workspace_id).where(
                ProjectProfile.id == project_id,
                ProjectProfile.tenant_id == ctx.tenant_id,
            )
        )
    ).first()
    # A project of an invisible workspace is a missing one (CP-ADR-0082 §3.7).
    if found is None or not ctx.sees_workspace(found.workspace_id):
        raise missing
    entity_id: uuid.UUID = found.id
    return entity_id


async def _load_task(session: AsyncSession, ctx: AuthContext, entity_ref: str) -> uuid.UUID:
    """Resolve a task by UUID or public id — the importer works in public ids."""
    conditions = [Task.tenant_id == ctx.tenant_id]
    try:
        conditions.append(Task.id == uuid.UUID(entity_ref))
    except ValueError:
        conditions.append(Task.public_id == entity_ref.upper())
    found = (await session.execute(select(Task.id, Task.workspace_id).where(*conditions))).first()
    if found is None or not ctx.sees_workspace(found.workspace_id):
        raise NotFoundError("Task not found", details={"task": entity_ref})
    entity_id: uuid.UUID = found.id
    return entity_id


def _visible_projects(ctx: AuthContext, column: Any) -> ColumnElement[bool]:
    condition: ColumnElement[bool] = column.in_(
        select(ProjectProfile.id).where(
            ProjectProfile.tenant_id == ctx.tenant_id,
            workspace_condition(ctx, ProjectProfile.workspace_id),
        )
    )
    return condition


def _visible_tasks(ctx: AuthContext, column: Any) -> ColumnElement[bool]:
    return task_condition(ctx, column)


ENTITY_BINDINGS: dict[str, EntityBinding] = {
    binding.entity_type: binding
    for binding in (
        EntityBinding(
            entity_type="project",
            read_permission=Permission.PROJECTS_READ,
            manage_permission=Permission.PROJECTS_MANAGE,
            loader=_load_project,
            visible=_visible_projects,
        ),
        EntityBinding(
            entity_type="task",
            read_permission=Permission.TASKS_READ,
            manage_permission=Permission.TASKS_WRITE,
            loader=_load_task,
            visible=_visible_tasks,
        ),
    )
}

SUPPORTED_ENTITY_TYPES = tuple(sorted(ENTITY_BINDINGS))


def resolve_entity_binding(entity_type: str) -> EntityBinding:
    """Binding for a client-supplied entity type, or ``422``.

    Fails closed on purpose: an unrecognised type could otherwise occupy an
    external key that nobody is able to resolve, own or clean up.
    """
    binding = ENTITY_BINDINGS.get(entity_type)
    if binding is None:
        raise ValidationError(
            "invalid_entity_type",
            f"Unknown entity type: {entity_type!r}",
            details={"field": "entityType", "supported": list(SUPPORTED_ENTITY_TYPES)},
        )
    return binding


async def resolve_entity_id(
    session: AsyncSession, ctx: AuthContext, binding: EntityBinding, entity_ref: str
) -> uuid.UUID:
    """Resolve a reference to an entity id inside the caller's tenant.

    A foreign-tenant id is indistinguishable from a missing one: both are
    ``404``. Anything else would answer "does this id exist elsewhere?".
    """
    if not entity_ref or len(entity_ref) > MAX_ENTITY_REF_LENGTH:
        raise ValidationError(
            "invalid_entity_reference",
            f"entityId must be 1..{MAX_ENTITY_REF_LENGTH} characters",
            details={"field": "entityId"},
        )
    return await binding.loader(session, ctx, entity_ref)
