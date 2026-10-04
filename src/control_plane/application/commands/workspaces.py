"""Workspace commands: hierarchical organizational scopes.

Structural mutations (create/move/slug change) serialize on a per-tenant
advisory lock, which makes the cycle and sibling-uniqueness pre-checks
race-free; the partial unique indexes remain the DB-level backstop.
"""

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.workspace_types import (
    check_child_allowed,
    get_tenant_workspace_type,
    resolve_workspace_type,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission, ProjectStatus, WorkspaceStatus
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.project import validate_against_schema
from control_plane.infrastructure.db.models import (
    ProjectProfile,
    Task,
    TaskType,
    Workspace,
    WorkspaceMember,
    WorkspaceType,
)

# "Not passed" for a nullable setting, where None means "inherit".
UNSET: Any = object()


async def lock_workspace_tree(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Serialize structural workspace mutations within a tenant."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:ws:{tenant_id}", 0)))
    )


async def get_tenant_workspace(
    session: AsyncSession,
    ctx: AuthContext,
    workspace_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Workspace:
    stmt = select(Workspace).where(
        Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    workspace = await session.scalar(stmt)
    # Outside the caller's visibility a workspace answers as a missing one,
    # for every object filed in it by id (CP-ADR-0082 §3.6).
    if workspace is None or not ctx.sees_workspace(workspace.id):
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    return workspace


async def require_active_workspace(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> Workspace:
    workspace = await get_tenant_workspace(session, ctx, workspace_id)
    if workspace.status != WorkspaceStatus.ACTIVE:
        raise ValidationError(
            "workspace_archived",
            "Workspace is archived",
            details={"workspaceId": str(workspace_id)},
        )
    return workspace


async def workspace_ancestor_ids(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[uuid.UUID]:
    """The workspace itself plus all its ancestors, nearest first (root last).

    The explicit depth ORDER BY guarantees the order (callers rely on
    nearest-first for role slug shadowing) instead of leaning on the
    incidental row order of a recursive CTE.
    """
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE anc AS (
                SELECT id, parent_id, 0 AS depth FROM workspaces
                WHERE id = :ws AND tenant_id = :tenant
                UNION ALL
                SELECT w.id, w.parent_id, anc.depth + 1 FROM workspaces w
                JOIN anc ON w.id = anc.parent_id
            )
            SELECT id FROM anc ORDER BY depth
            """
        ),
        {"ws": workspace_id, "tenant": tenant_id},
    )
    return [row[0] for row in rows]


async def effective_task_types(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[str] | None:
    """Task type keys allowed in a workspace (CP-ADR-0008, amendment 2026-10-03 A1).

    The own ``task_types`` or, while it is NULL, that of the nearest ancestor
    which sets it; None when nobody on the path does — every type is allowed.
    ``[]`` set anywhere on the path stops the walk: it allows none.
    """
    ancestors = await workspace_ancestor_ids(session, tenant_id, workspace_id)
    if not ancestors:
        return None
    rows = await session.execute(
        select(Workspace.id, Workspace.task_types).where(
            Workspace.tenant_id == tenant_id, Workspace.id.in_(ancestors)
        )
    )
    own = {row.id: row.task_types for row in rows}
    for node in ancestors:
        value = own.get(node)
        if value is not None:
            return list(value)
    return None


async def require_task_type_allowed(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID, type_key: str
) -> None:
    """Refuse work of a type the workspace does not allow (A3); work already
    there is never re-checked."""
    allowed = await effective_task_types(session, tenant_id, workspace_id)
    if allowed is not None and type_key not in allowed:
        raise ValidationError(
            "task_type_not_allowed",
            f"Task type {type_key!r} is not allowed in this workspace",
            details={"workspaceId": str(workspace_id), "typeKey": type_key},
        )


async def _check_task_type_keys(session: AsyncSession, ctx: AuthContext, keys: list[str]) -> None:
    """Every key names a task type of the tenant: any version, any status."""
    known = set(
        (
            await session.scalars(
                select(TaskType.key)
                .where(TaskType.tenant_id == ctx.tenant_id, TaskType.key.in_(keys))
                .distinct()
            )
        ).all()
    )
    for index, key in enumerate(keys):
        if key not in known:
            raise ValidationError(
                "unknown_task_type",
                f"Task type {key!r} is not registered",
                details={"field": f"taskTypes[{index}]", "taskType": key},
            )


async def workspace_subtree_ids(
    session: AsyncSession, tenant_id: uuid.UUID, root_id: uuid.UUID
) -> list[uuid.UUID]:
    """The workspace itself plus all its descendants (tenant-scoped CTE).

    Powers ``includeDescendants`` filtering (v0.3). Returns [] when the root
    does not exist in the tenant — callers translate that into 404.
    """
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE subtree AS (
                SELECT id FROM workspaces WHERE id = :root AND tenant_id = :tenant
                UNION ALL
                SELECT w.id FROM workspaces w JOIN subtree s ON w.parent_id = s.id
            )
            SELECT id FROM subtree
            """
        ),
        {"root": root_id, "tenant": tenant_id},
    )
    return [row[0] for row in rows]


async def _is_in_subtree(
    session: AsyncSession, tenant_id: uuid.UUID, root_id: uuid.UUID, candidate_id: uuid.UUID
) -> bool:
    """Is ``candidate_id`` inside the subtree rooted at ``root_id`` (inclusive)?"""
    row = await session.execute(
        text(
            """
            WITH RECURSIVE subtree AS (
                SELECT id FROM workspaces WHERE id = :root AND tenant_id = :tenant
                UNION ALL
                SELECT w.id FROM workspaces w JOIN subtree s ON w.parent_id = s.id
            )
            SELECT 1 FROM subtree WHERE id = :candidate LIMIT 1
            """
        ),
        {"root": root_id, "candidate": candidate_id, "tenant": tenant_id},
    )
    return row.first() is not None


async def _check_sibling_slug_free(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    slug: str,
    parent_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    stmt = select(Workspace.id).where(
        Workspace.tenant_id == ctx.tenant_id,
        Workspace.slug == slug,
        Workspace.parent_id.is_(None) if parent_id is None else Workspace.parent_id == parent_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(Workspace.id != exclude_id)
    if await session.scalar(stmt) is not None:
        raise ConflictError(
            "workspace_slug_conflict",
            "A sibling workspace with this slug already exists",
            details={"slug": slug},
        )


async def _check_children_allowed_under(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID, new_type: WorkspaceType
) -> None:
    """Every active child must still be a legal child of the new type."""
    rows = await session.execute(
        select(WorkspaceType)
        .join(Workspace, Workspace.type_id == WorkspaceType.id)
        .where(
            Workspace.parent_id == workspace_id,
            Workspace.tenant_id == ctx.tenant_id,
            Workspace.status == WorkspaceStatus.ACTIVE,
        )
        .distinct()
    )
    for child_type in rows.scalars():
        check_child_allowed(new_type, child_type)


async def _check_type_allowed_under(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    parent_id: uuid.UUID | None,
    child_type: WorkspaceType,
) -> None:
    """Root nodes accept any type; nested ones must satisfy the parent's rule."""
    if parent_id is None:
        return
    parent = await get_tenant_workspace(session, ctx, parent_id)
    parent_type = await get_tenant_workspace_type(session, ctx, parent.type_id)
    check_child_allowed(parent_type, child_type)


async def create_workspace(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    slug: str,
    name: str,
    description: str = "",
    parent_id: uuid.UUID | None = None,
    type_id: uuid.UUID | None = None,
    type_key: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> Workspace:
    await authorize(
        ctx,
        Permission.WORKSPACES_MANAGE,
        resource=ResourceRef("workspace", str(parent_id)) if parent_id else None,
    )
    await lock_workspace_tree(session, ctx.tenant_id)
    if parent_id is not None:
        await require_active_workspace(session, ctx, parent_id)
    await _check_sibling_slug_free(session, ctx, slug=slug, parent_id=parent_id)

    workspace_type = await resolve_workspace_type(session, ctx, type_id=type_id, type_key=type_key)
    if workspace_type.status != WorkspaceStatus.ACTIVE:
        raise ValidationError(
            "workspace_type_archived",
            "Workspace type is archived",
            details={"workspaceTypeId": str(workspace_type.id)},
        )
    await _check_type_allowed_under(session, ctx, parent_id=parent_id, child_type=workspace_type)
    fields = custom_fields or {}
    validate_against_schema(
        workspace_type.field_schema,
        fields,
        code="custom_fields_invalid",
        field_name="customFields",
    )

    now = utcnow()
    workspace = Workspace(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        parent_id=parent_id,
        type_id=workspace_type.id,
        slug=slug,
        name=name,
        description=description,
        custom_fields=fields,
        status=WorkspaceStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(workspace)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.created",
        entity_type="workspace",
        entity_id=workspace.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "slug": slug,
            "name": name,
            "parentId": str(parent_id) if parent_id else None,
            "typeKey": workspace_type.key,
        },
    )
    return workspace


async def update_workspace(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID,
    expected_version: int,
    name: str | None = None,
    description: str | None = None,
    slug: str | None = None,
    type_id: uuid.UUID | None = None,
    type_key: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    task_types: list[str] | Any | None = UNSET,
) -> Workspace:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    structural = slug is not None or type_id is not None or type_key is not None
    # Slug and type changes are structural: take the tree lock BEFORE the row
    # lock so lock ordering matches create/move.
    if structural:
        await lock_workspace_tree(session, ctx.tenant_id)
    workspace = await get_tenant_workspace(session, ctx, workspace_id, for_update=True)
    if workspace.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Workspace version does not match If-Match",
            details={
                "workspaceId": str(workspace_id),
                "expectedVersion": expected_version,
                "currentVersion": workspace.version,
            },
        )

    changes: dict[str, Any] = {}
    if name is not None and name != workspace.name:
        changes["name"] = name
    if description is not None and description != workspace.description:
        changes["description"] = description
    if slug is not None and slug != workspace.slug:
        await _check_sibling_slug_free(
            session, ctx, slug=slug, parent_id=workspace.parent_id, exclude_id=workspace.id
        )
        changes["slug"] = slug

    workspace_type = await get_tenant_workspace_type(session, ctx, workspace.type_id)
    if type_id is not None or type_key is not None:
        new_type = await resolve_workspace_type(session, ctx, type_id=type_id, type_key=type_key)
        if new_type.id != workspace.type_id:
            if new_type.status != WorkspaceStatus.ACTIVE:
                raise ValidationError(
                    "workspace_type_archived",
                    "Workspace type is archived",
                    details={"workspaceTypeId": str(new_type.id)},
                )
            # Retyping must not leave the tree invalid in either direction.
            await _check_type_allowed_under(
                session, ctx, parent_id=workspace.parent_id, child_type=new_type
            )
            await _check_children_allowed_under(session, ctx, workspace.id, new_type)
            workspace_type = new_type
            changes["typeKey"] = new_type.key
            workspace.type_id = new_type.id

    if custom_fields is not None and custom_fields != workspace.custom_fields:
        changes["customFields"] = True
        workspace.custom_fields = custom_fields
    if changes.get("typeKey") and custom_fields is None:
        # A new type means a new schema: the stored values must still fit.
        validate_against_schema(
            workspace_type.field_schema,
            workspace.custom_fields,
            code="custom_fields_invalid",
            field_name="customFields",
        )
    if custom_fields is not None:
        validate_against_schema(
            workspace_type.field_schema,
            custom_fields,
            code="custom_fields_invalid",
            field_name="customFields",
        )
    if task_types is not UNSET:
        if task_types is not None:
            await _check_task_type_keys(session, ctx, task_types)
        if task_types != workspace.task_types:
            changes["taskTypes"] = True
            workspace.task_types = None if task_types is None else list(task_types)
    if not changes:
        raise ValidationError("empty_update", "No fields to update")

    for field_name, value in changes.items():
        if field_name in ("name", "description", "slug"):
            setattr(workspace, field_name, value)
    workspace.version += 1
    workspace.updated_at = utcnow()

    payload: dict[str, Any] = {"changes": changes, "version": workspace.version}
    if "taskTypes" in changes:
        # The new own setting itself: keys are not secret (event v2).
        payload["taskTypes"] = workspace.task_types
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.updated",
        entity_type="workspace",
        entity_id=workspace.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )
    return workspace


async def archive_workspace(
    session: AsyncSession, ctx: AuthContext, *, workspace_id: uuid.UUID
) -> Workspace:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    await lock_workspace_tree(session, ctx.tenant_id)
    workspace = await get_tenant_workspace(session, ctx, workspace_id, for_update=True)
    if workspace.status == WorkspaceStatus.ARCHIVED:
        return workspace  # idempotent

    active_child = await session.scalar(
        select(Workspace.id).where(
            Workspace.parent_id == workspace.id,
            Workspace.status == WorkspaceStatus.ACTIVE,
        )
    )
    if active_child is not None:
        raise ValidationError(
            "workspace_has_active_children",
            "Archive or move child workspaces first",
            details={"workspaceId": str(workspace_id)},
        )

    # A workspace carrying a live project is not a container the operator may
    # silently retire: archive the project explicitly first (ADR-0031).
    active_project = await session.scalar(
        select(ProjectProfile.id).where(
            ProjectProfile.workspace_id == workspace.id,
            ProjectProfile.tenant_id == ctx.tenant_id,
            ProjectProfile.status == ProjectStatus.ACTIVE,
        )
    )
    if active_project is not None:
        raise ValidationError(
            "workspace_has_active_project",
            "Archive the project on this workspace first",
            details={"workspaceId": str(workspace_id), "projectId": str(active_project)},
        )

    workspace.status = WorkspaceStatus.ARCHIVED
    workspace.version += 1
    workspace.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.archived",
        entity_type="workspace",
        entity_id=workspace.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"slug": workspace.slug},
    )
    return workspace


async def move_workspace(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID,
    new_parent_id: uuid.UUID | None,
) -> Workspace:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    await lock_workspace_tree(session, ctx.tenant_id)
    workspace = await get_tenant_workspace(session, ctx, workspace_id, for_update=True)

    if new_parent_id == workspace.parent_id:
        return workspace  # no-op move

    if new_parent_id is not None:
        if new_parent_id == workspace.id:
            raise ValidationError("workspace_cycle", "A workspace cannot be its own parent")
        await require_active_workspace(session, ctx, new_parent_id)
        if await _is_in_subtree(session, ctx.tenant_id, workspace.id, new_parent_id):
            raise ValidationError(
                "workspace_cycle",
                "Cannot move a workspace under its own descendant",
                details={"workspaceId": str(workspace_id), "newParentId": str(new_parent_id)},
            )

    await _check_sibling_slug_free(
        session, ctx, slug=workspace.slug, parent_id=new_parent_id, exclude_id=workspace.id
    )
    workspace_type = await get_tenant_workspace_type(session, ctx, workspace.type_id)
    await _check_type_allowed_under(
        session, ctx, parent_id=new_parent_id, child_type=workspace_type
    )

    old_parent_id = workspace.parent_id
    workspace.parent_id = new_parent_id
    workspace.version += 1
    workspace.updated_at = utcnow()
    await session.flush()

    # Moving a subtree re-parents every project inside it. Governance is an
    # upper bound from ancestors, so the new position must still satisfy it —
    # for the moved subtree AND for every project below it. Checked after the
    # parent_id write, inside the same transaction: a violation rolls the whole
    # move back rather than leaving a partially valid hierarchy (ADR-0033).
    from control_plane.application.queries.projects import assert_subtree_governance_valid

    await assert_subtree_governance_valid(session, ctx, workspace.id)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.moved",
        entity_type="workspace",
        entity_id=workspace.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "fromParentId": str(old_parent_id) if old_parent_id else None,
            "toParentId": str(new_parent_id) if new_parent_id else None,
        },
    )
    return workspace


async def add_workspace_member(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> WorkspaceMember:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    from control_plane.application.commands.principals import get_tenant_principal

    await require_active_workspace(session, ctx, workspace_id)
    await get_tenant_principal(session, ctx, principal_id)

    existing = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.principal_id == principal_id,
        )
    )
    if existing is not None:
        return existing  # idempotent

    member = WorkspaceMember(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        created_at=utcnow(),
    )
    session.add(member)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.member_added",
        entity_type="workspace",
        entity_id=workspace_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"principalId": str(principal_id)},
    )
    return member


async def remove_workspace_member(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> None:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    await get_tenant_workspace(session, ctx, workspace_id)
    member = await session.scalar(
        select(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.principal_id == principal_id,
        )
        .with_for_update()
    )
    if member is None:
        return  # idempotent
    await session.delete(member)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace.member_removed",
        entity_type="workspace",
        entity_id=workspace_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"principalId": str(principal_id)},
    )


async def workspace_task_count(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.tenant_id == ctx.tenant_id, Task.workspace_id == workspace_id)
        )
    ) or 0
