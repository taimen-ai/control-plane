"""Project read side: scope resolution from the workspace tree, effective config.

Everything project-shaped is *derived* here rather than stored: the owning
project of a task, the parent project of a project, and the workspace sets a
project covers are all recursive CTEs over ``workspaces`` + ``project_profiles``
(ADR-0035). That is what keeps the workspace tree the single hierarchy.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.application.visibility import workspace_condition
from control_plane.domain.enums import Permission, ProjectStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.project import (
    EffectiveConfig,
    ProjectConfigSources,
    compute_effective_config,
    governance_violations,
    locked_setting_violations,
)
from control_plane.infrastructure.db.models import (
    ExternalReference,
    ProjectConfigRevision,
    ProjectProfile,
    ProjectTemplate,
    Workspace,
)

# Guard against a pathological (or corrupted) tree turning one request into an
# unbounded walk. Deeper hierarchies than this are a modelling problem.
MAX_PROJECT_DEPTH = 64


async def get_tenant_project(
    session: AsyncSession, ctx: AuthContext, project_id: uuid.UUID, *, for_update: bool = False
) -> ProjectProfile:
    stmt = select(ProjectProfile).where(
        ProjectProfile.id == project_id, ProjectProfile.tenant_id == ctx.tenant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    project = await session.scalar(stmt)
    # A project of an invisible workspace answers as a missing one, for
    # everything under it too (CP-ADR-0082 §3.7).
    if project is None or not ctx.sees_workspace(project.workspace_id):
        raise NotFoundError("Project not found", details={"projectId": str(project_id)})
    return project


async def project_for_workspace(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """Owning project of one workspace: nearest ancestor carrying a profile."""
    mapping = await projects_for_workspaces(session, tenant_id, [workspace_id])
    return mapping.get(workspace_id)


async def projects_for_workspaces(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Batch owning-project resolution — one CTE, not one query per workspace.

    The walk climbs from each seed workspace and stops at the first node that
    carries a profile, so a nested project correctly shadows its ancestors.
    """
    unique = list({ws for ws in workspace_ids if ws is not None})
    if not unique:
        return {}
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE up(origin, node, parent, depth) AS (
                SELECT w.id, w.id, w.parent_id, 0
                  FROM workspaces w
                 WHERE w.tenant_id = :tenant AND w.id = ANY(:ids)
                UNION ALL
                SELECT u.origin, w.id, w.parent_id, u.depth + 1
                  FROM up u
                  JOIN workspaces w ON w.id = u.parent
                 WHERE u.depth < :max_depth
                   AND NOT EXISTS (
                       SELECT 1 FROM project_profiles p WHERE p.workspace_id = u.node
                   )
            )
            SELECT DISTINCT ON (up.origin) up.origin, p.id
              FROM up
              JOIN project_profiles p ON p.workspace_id = up.node
             ORDER BY up.origin, up.depth
            """
        ),
        {"tenant": tenant_id, "ids": unique, "max_depth": MAX_PROJECT_DEPTH},
    )
    return {row[0]: row[1] for row in rows}


async def parent_project_id(
    session: AsyncSession, tenant_id: uuid.UUID, project: ProjectProfile
) -> uuid.UUID | None:
    """Derived parent: nearest ancestor workspace with a profile, above ours."""
    workspace = await session.get(Workspace, project.workspace_id)
    if workspace is None or workspace.parent_id is None:
        return None
    return await project_for_workspace(session, tenant_id, workspace.parent_id)


async def parent_project_ids(
    session: AsyncSession, tenant_id: uuid.UUID, projects: Sequence[ProjectProfile]
) -> dict[uuid.UUID, uuid.UUID]:
    """Derived parent for a whole page of projects, in two queries."""
    if not projects:
        return {}
    workspaces = {
        w.id: w
        for w in (
            await session.scalars(
                select(Workspace).where(
                    Workspace.id.in_({p.workspace_id for p in projects}),
                    Workspace.tenant_id == tenant_id,
                )
            )
        ).all()
    }
    parent_workspace_ids = [
        workspaces[p.workspace_id].parent_id
        for p in projects
        if p.workspace_id in workspaces and workspaces[p.workspace_id].parent_id is not None
    ]
    owners = await projects_for_workspaces(
        session, tenant_id, [w for w in parent_workspace_ids if w is not None]
    )
    result: dict[uuid.UUID, uuid.UUID] = {}
    for project in projects:
        workspace = workspaces.get(project.workspace_id)
        if workspace is None or workspace.parent_id is None:
            continue
        owner = owners.get(workspace.parent_id)
        if owner is not None:
            result[project.id] = owner
    return result


async def project_ancestry(
    session: AsyncSession, tenant_id: uuid.UUID, project: ProjectProfile
) -> list[ProjectProfile]:
    """The ancestry chain root-most first, ending with ``project`` itself."""
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE anc(id, parent_id, depth) AS (
                SELECT w.id, w.parent_id, 0
                  FROM workspaces w
                 WHERE w.tenant_id = :tenant AND w.id = :ws
                UNION ALL
                SELECT w.id, w.parent_id, anc.depth + 1
                  FROM workspaces w JOIN anc ON w.id = anc.parent_id
                 WHERE anc.depth < :max_depth
            )
            SELECT p.id, anc.depth
              FROM anc JOIN project_profiles p ON p.workspace_id = anc.id
             WHERE p.tenant_id = :tenant
             ORDER BY anc.depth DESC
            """
        ),
        {"tenant": tenant_id, "ws": project.workspace_id, "max_depth": MAX_PROJECT_DEPTH},
    )
    ordered_ids = [row[0] for row in rows]
    if not ordered_ids:
        return [project]
    found = {
        p.id: p
        for p in (
            await session.scalars(
                select(ProjectProfile).where(
                    ProjectProfile.id.in_(ordered_ids), ProjectProfile.tenant_id == tenant_id
                )
            )
        ).all()
    }
    return [found[pid] for pid in ordered_ids if pid in found]


async def project_scope_workspace_ids(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project: ProjectProfile,
    *,
    include_subprojects: bool = False,
) -> list[uuid.UUID]:
    """Workspaces covered by a project.

    ``include_subprojects=False`` (exact scope) walks down from the project
    workspace and stops before any workspace that starts its own project;
    ``True`` returns the whole subtree.
    """
    if include_subprojects:
        sql = """
            WITH RECURSIVE subtree(id, depth) AS (
                SELECT w.id, 0 FROM workspaces w
                 WHERE w.id = :root AND w.tenant_id = :tenant
                UNION ALL
                SELECT c.id, s.depth + 1
                  FROM workspaces c JOIN subtree s ON c.parent_id = s.id
                 WHERE s.depth < :max_depth
            )
            SELECT id FROM subtree
        """
    else:
        sql = """
            WITH RECURSIVE scope(id, depth) AS (
                SELECT w.id, 0 FROM workspaces w
                 WHERE w.id = :root AND w.tenant_id = :tenant
                UNION ALL
                SELECT c.id, s.depth + 1
                  FROM workspaces c JOIN scope s ON c.parent_id = s.id
                 WHERE s.depth < :max_depth
                   AND NOT EXISTS (
                       SELECT 1 FROM project_profiles p WHERE p.workspace_id = c.id
                   )
            )
            SELECT id FROM scope
        """
    rows = await session.execute(
        text(sql),
        {"root": project.workspace_id, "tenant": tenant_id, "max_depth": MAX_PROJECT_DEPTH},
    )
    return [row[0] for row in rows]


async def archived_project_workspace_ids(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[uuid.UUID]:
    """Workspaces inside archived projects (their exact scopes, unioned).

    Discovery uses this to stop offering new work from retired projects. The
    walk stops at nested projects, so an ACTIVE child project inside an
    archived parent keeps handing out work.
    """
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE archived(id, depth) AS (
                SELECT w.id, 0
                  FROM workspaces w
                  JOIN project_profiles p ON p.workspace_id = w.id
                 WHERE w.tenant_id = :tenant AND p.status = 'archived'
                UNION ALL
                SELECT c.id, a.depth + 1
                  FROM workspaces c JOIN archived a ON c.parent_id = a.id
                 WHERE a.depth < :max_depth
                   AND NOT EXISTS (
                       SELECT 1 FROM project_profiles p2 WHERE p2.workspace_id = c.id
                   )
            )
            SELECT DISTINCT id FROM archived
            """
        ),
        {"tenant": tenant_id, "max_depth": MAX_PROJECT_DEPTH},
    )
    return [row[0] for row in rows]


async def has_archived_projects(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(ProjectProfile.id)
            .where(
                ProjectProfile.tenant_id == tenant_id,
                ProjectProfile.status == ProjectStatus.ARCHIVED,
            )
            .limit(1)
        )
    ) is not None


# --- effective config ---------------------------------------------------------


async def _config_sources(
    session: AsyncSession, chain: list[ProjectProfile]
) -> list[ProjectConfigSources]:
    template_ids = {p.template_id for p in chain}
    templates = {
        t.id: t
        for t in (
            await session.scalars(
                select(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
            )
        ).all()
    }
    revision_ids = [p.active_config_revision_id for p in chain if p.active_config_revision_id]
    revisions = (
        {
            r.id: r
            for r in (
                await session.scalars(
                    select(ProjectConfigRevision).where(ProjectConfigRevision.id.in_(revision_ids))
                )
            ).all()
        }
        if revision_ids
        else {}
    )

    sources: list[ProjectConfigSources] = []
    for project in chain:
        template = templates[project.template_id]
        revision = (
            revisions.get(project.active_config_revision_id)
            if project.active_config_revision_id
            else None
        )
        sources.append(
            ProjectConfigSources(
                project_id=str(project.id),
                template_id=str(template.id),
                template_key=template.key,
                template_version=template.version,
                template_default_config=template.default_config or {},
                template_default_views=list(template.default_views or []),
                revision=revision.revision if revision else None,
                revision_config=revision.config if revision else None,
                profile_settings=project.settings or {},
            )
        )
    return sources


async def effective_config_for(
    session: AsyncSession, tenant_id: uuid.UUID, project: ProjectProfile
) -> EffectiveConfig:
    chain = await project_ancestry(session, tenant_id, project)
    return compute_effective_config(await _config_sources(session, chain))


async def effective_config(
    session: AsyncSession, ctx: AuthContext, project_id: uuid.UUID
) -> tuple[ProjectProfile, EffectiveConfig]:
    await authorize(ctx, Permission.PROJECTS_READ)
    project = await get_tenant_project(session, ctx, project_id)
    return project, await effective_config_for(session, ctx.tenant_id, project)


async def ancestor_effective_governance(
    session: AsyncSession, tenant_id: uuid.UUID, project: ProjectProfile
) -> dict[str, Any]:
    """Governance ceiling imposed on ``project`` by everything above it."""
    chain = await project_ancestry(session, tenant_id, project)
    if len(chain) <= 1:
        return {}
    return compute_effective_config(await _config_sources(session, chain)).inherited_governance


async def assert_subtree_governance_valid(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> None:
    """Every project at or below ``workspace_id`` must still fit its ceiling.

    Called inside the workspace-move transaction: a single violation aborts
    the whole move, so a partially valid hierarchy is never committed.
    """
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE subtree(id, depth) AS (
                SELECT w.id, 0 FROM workspaces w
                 WHERE w.id = :root AND w.tenant_id = :tenant
                UNION ALL
                SELECT c.id, s.depth + 1
                  FROM workspaces c JOIN subtree s ON c.parent_id = s.id
                 WHERE s.depth < :max_depth
            )
            SELECT p.id FROM subtree JOIN project_profiles p ON p.workspace_id = subtree.id
             WHERE p.tenant_id = :tenant
            """
        ),
        {"root": workspace_id, "tenant": ctx.tenant_id, "max_depth": MAX_PROJECT_DEPTH},
    )
    project_ids = [row[0] for row in rows]
    if not project_ids:
        return
    projects = (
        await session.scalars(select(ProjectProfile).where(ProjectProfile.id.in_(project_ids)))
    ).all()
    for project in projects:
        chain = await project_ancestry(session, ctx.tenant_id, project)
        if len(chain) <= 1:
            continue
        sources = await _config_sources(session, chain)
        ceiling = compute_effective_config(sources).inherited_governance
        own = dict((sources[-1].revision_config or {}).get("governance") or {})
        violations = governance_violations(own, ceiling)
        if violations:
            raise ValidationError(
                "governance_weakened",
                "The new hierarchy would let a project weaken its ancestor governance",
                details={"projectId": str(project.id), "violations": violations},
            )
        # Locks are part of the same ceiling: a move must not land a project
        # under an ancestor that forbids a setting it already overrides.
        effective = compute_effective_config(sources)
        own_settings = set((sources[-1].revision_config or {}).get("settings") or {}) | set(
            sources[-1].profile_settings or {}
        )
        locked = locked_setting_violations(own_settings, effective.locked_settings)
        if locked:
            raise ValidationError(
                "setting_locked",
                "The new hierarchy would lock settings this project already overrides",
                details={"projectId": str(project.id), "lockedSettings": locked},
            )


# --- lists --------------------------------------------------------------------


async def list_projects(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    workspace_id: uuid.UUID | None = None,
    status: str | None = None,
    status_key: str | None = None,
    system_status_category: str | None = None,
    template_key: str | None = None,
    external_system: str | None = None,
    external_type: str | None = None,
    external_id: str | None = None,
) -> Page[ProjectProfile]:
    await authorize(ctx, Permission.PROJECTS_READ)
    from control_plane.application.common import make_created_cursor, parse_created_cursor

    stmt = select(ProjectProfile).where(
        ProjectProfile.tenant_id == ctx.tenant_id,
        workspace_condition(ctx, ProjectProfile.workspace_id),
    )
    if workspace_id is not None:
        stmt = stmt.where(ProjectProfile.workspace_id == workspace_id)
    if status is not None:
        if status not in set(ProjectStatus):
            raise ValidationError("invalid_status", f"Unknown project status: {status}")
        stmt = stmt.where(ProjectProfile.status == status)
    if status_key is not None:
        stmt = stmt.where(ProjectProfile.status_key == status_key)
    if system_status_category is not None:
        stmt = stmt.where(ProjectProfile.system_status_category == system_status_category)
    if template_key is not None:
        stmt = stmt.join(ProjectTemplate, ProjectTemplate.id == ProjectProfile.template_id).where(
            ProjectTemplate.key == template_key
        )
    if external_id is None and (external_system is not None or external_type is not None):
        # A half-specified lookup would silently return every project.
        raise ValidationError(
            "invalid_external_lookup",
            "externalId is required when filtering by an external reference",
            details={"required": ["externalSystem", "externalId"]},
        )
    if external_id is not None:
        # Lookup by external mapping stays inside the tenant by construction.
        if external_system is None:
            raise ValidationError(
                "invalid_external_lookup",
                "externalSystem is required with externalId",
                details={"required": ["externalSystem", "externalId"]},
            )
        reference = select(ExternalReference.entity_id).where(
            ExternalReference.tenant_id == ctx.tenant_id,
            ExternalReference.entity_type == "project",
            ExternalReference.external_system == external_system,
            ExternalReference.external_id == external_id,
        )
        if external_type is not None:
            reference = reference.where(ExternalReference.external_type == external_type)
        stmt = stmt.where(ProjectProfile.id.in_(reference))

    effective_limit = clamp_limit(limit)
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ProjectProfile.created_at < created_at)
            | ((ProjectProfile.created_at == created_at) & (ProjectProfile.id < entity_id))
        )
    stmt = stmt.order_by(ProjectProfile.created_at.desc(), ProjectProfile.id.desc()).limit(
        effective_limit + 1
    )
    rows = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)


async def workspace_tree(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    root_id: uuid.UUID | None = None,
    depth: int | None = None,
    include_archived: bool = False,
    include_projects: bool = True,
) -> list[dict[str, Any]]:
    """Deterministic, N+1-free workspace tree with an optional project projection.

    One recursive CTE walks the tree in a stable order — siblings by
    ``(slug, id)`` — and a single LEFT JOIN attaches the project projection.
    """
    await authorize(ctx, Permission.WORKSPACES_READ)
    max_depth = MAX_PROJECT_DEPTH if depth is None else max(0, min(depth, MAX_PROJECT_DEPTH))
    if root_id is not None:
        exists = await session.scalar(
            select(Workspace.id).where(
                Workspace.id == root_id, Workspace.tenant_id == ctx.tenant_id
            )
        )
        if exists is None or not ctx.sees_workspace(root_id):
            raise NotFoundError("Workspace not found", details={"workspaceId": str(root_id)})

    rows: list[Row[Any]] = list(
        await session.execute(
            text(
                """
                WITH RECURSIVE tree(id, parent_id, depth, path) AS (
                    SELECT w.id, w.parent_id, 0, ARRAY[w.slug || '/' || CAST(w.id AS text)]
                      FROM workspaces w
                     WHERE w.tenant_id = :tenant
                       AND (
                           (NOT :has_root AND w.parent_id IS NULL)
                           OR (:has_root AND w.id = :root)
                           -- members: a visible workspace under an invisible
                           -- parent is a root to the caller (CP-ADR-0082 3.9)
                           OR (NOT :has_root AND :members
                               AND w.parent_id <> ALL(CAST(:visible AS uuid[])))
                       )
                       AND (NOT :members OR w.id = ANY(CAST(:visible AS uuid[])))
                       AND (:include_archived OR w.status = 'active')
                    UNION ALL
                    SELECT c.id, c.parent_id, t.depth + 1,
                           t.path || (c.slug || '/' || CAST(c.id AS text))
                      FROM workspaces c JOIN tree t ON c.parent_id = t.id
                     WHERE t.depth < :max_depth
                       AND (NOT :members OR c.id = ANY(CAST(:visible AS uuid[])))
                       AND (:include_archived OR c.status = 'active')
                )
                SELECT t.id, t.parent_id, t.depth,
                       w.slug, w.name, w.status, w.version, w.type_id, wt.key AS type_key,
                       p.id AS project_id, p.status_key, p.system_status_category,
                       p.status AS project_status, pt.key AS template_key,
                       pt.version AS template_version
                  FROM tree t
                  JOIN workspaces w ON w.id = t.id
                  JOIN workspace_types wt ON wt.id = w.type_id
                  LEFT JOIN project_profiles p ON p.workspace_id = w.id
                  LEFT JOIN project_templates pt ON pt.id = p.template_id
                 ORDER BY t.path
                """
            ),
            {
                "tenant": ctx.tenant_id,
                "has_root": root_id is not None,
                # A NULL uuid parameter is fine: the has_root branch guards it.
                "root": root_id or uuid.UUID(int=0),
                "max_depth": max_depth,
                "include_archived": include_archived,
                "members": ctx.visible_workspaces is not None,
                "visible": [uuid.UUID(w) for w in ctx.visible_workspaces or ()],
            },
        )
    )

    nodes: dict[uuid.UUID, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for row in rows:
        node: dict[str, Any] = {
            "id": str(row.id),
            # The parent of a root the caller sees is not named when it is
            # not visible itself (CP-ADR-0082 3.9).
            "parentId": (
                str(row.parent_id) if row.parent_id and ctx.sees_workspace(row.parent_id) else None
            ),
            "depth": row.depth,
            "slug": row.slug,
            "name": row.name,
            "status": row.status,
            "version": row.version,
            "typeId": str(row.type_id),
            "typeKey": row.type_key,
            "children": [],
        }
        if include_projects:
            node["project"] = (
                {
                    "id": str(row.project_id),
                    "statusKey": row.status_key,
                    "systemStatusCategory": row.system_status_category,
                    "status": row.project_status,
                    "templateKey": row.template_key,
                    "templateVersion": row.template_version,
                }
                if row.project_id
                else None
            )
        nodes[row.id] = node
        parent = nodes.get(row.parent_id) if row.parent_id else None
        if parent is not None and row.depth > 0:
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots
