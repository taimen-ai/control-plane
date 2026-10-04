"""Project endpoints: profile CRUD, lifecycle, config revisions, references.

A projection over Workspace + Project Profile (ADR-0031) — deliberately not a
second hierarchy. Any parent/child information in a response is derived from
the workspace tree at read time.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    ConfigRevisionCreateRequest,
    ConfigRevisionOut,
    ExternalReferenceCreateRequest,
    ExternalReferenceOut,
    PageOut,
    ProjectCreateRequest,
    ProjectOut,
    ProjectTemplateCreateRequest,
    ProjectTemplateOut,
    ProjectTransitionRequest,
    ProjectUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands import project_templates as template_commands
from control_plane.application.commands import projects as commands
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries import external_references as external_reference_queries
from control_plane.application.queries import projects as queries
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import (
    attach_package,
    attach_packages,
    in_package,
)
from control_plane.application.visibility import workspace_condition
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import (
    ProjectConfigRevision,
    ProjectProfile,
    ProjectTemplate,
)

router = APIRouter(tags=["projects"])


async def _project_body(
    db: AsyncSession, ctx: AuthContext, project: ProjectProfile
) -> dict[str, Any]:
    """Response shape: stored fields plus everything derived from the tree."""
    return (await _project_bodies(db, ctx, [project]))[0]


async def _project_bodies(
    db: AsyncSession, ctx: AuthContext, projects: list[ProjectProfile]
) -> list[dict[str, Any]]:
    """Batch version: three queries for the whole page, not three per row."""
    if not projects:
        return []
    templates = {
        t.id: t
        for t in (
            await db.scalars(
                select(ProjectTemplate).where(
                    ProjectTemplate.id.in_({p.template_id for p in projects}),
                    ProjectTemplate.tenant_id == ctx.tenant_id,
                )
            )
        ).all()
    }
    revision_ids = {p.active_config_revision_id for p in projects if p.active_config_revision_id}
    revisions = (
        {
            r.id: r.revision
            for r in (
                await db.scalars(
                    select(ProjectConfigRevision).where(
                        ProjectConfigRevision.id.in_(revision_ids),
                        ProjectConfigRevision.tenant_id == ctx.tenant_id,
                    )
                )
            ).all()
        }
        if revision_ids
        else {}
    )
    parents = await queries.parent_project_ids(db, ctx.tenant_id, projects)
    if ctx.visible_workspaces is not None and parents:
        # A parent project of an invisible workspace is not named (CP-ADR-0082 §3.9).
        seen = set(
            await db.scalars(
                select(ProjectProfile.id).where(
                    ProjectProfile.id.in_(set(parents.values())),
                    workspace_condition(ctx, ProjectProfile.workspace_id),
                )
            )
        )
        parents = {child: parent for child, parent in parents.items() if parent in seen}
    bodies = []
    for project in projects:
        template = templates.get(project.template_id)
        parent_id = parents.get(project.id)
        bodies.append(
            dump(
                ProjectOut,
                project,
                parentProjectId=str(parent_id) if parent_id else None,
                templateKey=template.key if template else None,
                templateVersion=template.version if template else None,
                activeConfigRevision=(
                    revisions.get(project.active_config_revision_id)
                    if project.active_config_revision_id
                    else None
                ),
            )
        )
    return bodies


# --- project templates --------------------------------------------------------


# visibility: tenant — project templates are objects of the tenant
@router.post(
    "/project-templates",
    response_model=ProjectTemplateOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create the next immutable version of a project template",
)
async def create_project_template(
    payload: ProjectTemplateCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        template = await template_commands.create_template_version(
            db,
            ctx,
            key=payload.key,
            display_name=payload.display_name,
            description=payload.description,
            field_schema=payload.field_schema,
            lifecycle_schema=payload.lifecycle_schema,
            default_config=payload.default_config,
            default_views=payload.default_views,
            governance_schema=payload.governance_schema,
            memory_defaults=payload.memory_defaults,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "ProjectTemplate", dump(ProjectTemplateOut, template)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — project templates are objects of the tenant
@router.get("/project-templates", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_project_templates(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    await authorize(ctx, Permission.PROJECT_TEMPLATES_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(ProjectTemplate).where(ProjectTemplate.tenant_id == ctx.tenant_id)
    if key is not None:
        stmt = stmt.where(ProjectTemplate.key == key)
    if status is not None:
        stmt = stmt.where(ProjectTemplate.status == status)
    if package is not None:
        stmt = stmt.where(
            in_package("ProjectTemplate", ProjectTemplate.tenant_id, ProjectTemplate.key, package)
        )
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ProjectTemplate.created_at < created_at)
            | ((ProjectTemplate.created_at == created_at) & (ProjectTemplate.id < entity_id))
        )
    stmt = stmt.order_by(ProjectTemplate.created_at.desc(), ProjectTemplate.id.desc()).limit(
        effective_limit + 1
    )
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    items = [dump(ProjectTemplateOut, t) for t in rows]
    await attach_packages(db, ctx.tenant_id, "ProjectTemplate", items)
    return JSONResponse(page_body(items, next_cursor))


# visibility: tenant — project templates are objects of the tenant
@router.get(
    "/project-templates/{template_id}",
    response_model=ProjectTemplateOut,
    responses=ERROR_RESPONSES,
)
async def get_project_template(template_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    await authorize(ctx, Permission.PROJECT_TEMPLATES_READ)
    template = await template_commands.get_tenant_template(db, ctx, template_id)
    body = dump(ProjectTemplateOut, template)
    return JSONResponse(await attach_package(db, ctx.tenant_id, "ProjectTemplate", body))


# visibility: tenant — project templates are objects of the tenant
@router.post(
    "/project-templates/{template_id}:deprecate",
    response_model=ProjectTemplateOut,
    responses=ERROR_RESPONSES,
)
async def deprecate_project_template(
    template_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        template = await template_commands.deprecate_template(db, ctx, template_id=template_id)
        return 200, await attach_package(
            db, ctx.tenant_id, "ProjectTemplate", dump(ProjectTemplateOut, template)
        )

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


# --- projects -----------------------------------------------------------------


@router.post("/projects", response_model=ProjectOut, status_code=201, responses=ERROR_RESPONSES)
async def create_project(
    payload: ProjectCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        project = await commands.create_project(
            db,
            ctx,
            workspace_id=payload.workspace_id,
            workspace_slug=payload.workspace_slug,
            workspace_name=payload.workspace_name,
            parent_workspace_id=payload.parent_workspace_id,
            workspace_type_key=payload.workspace_type_key,
            template_id=payload.template_id,
            template_key=payload.template_key,
            template_version=payload.template_version,
            status_key=payload.status_key,
            owner_principal_id=payload.owner_principal_id,
            start_date=payload.start_date,
            target_date=payload.target_date,
            custom_fields=payload.custom_fields,
            settings=payload.settings,
        )
        return 201, await _project_body(db, ctx, project)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/projects", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_projects(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    status: str | None = Query(default=None),
    status_key: str | None = Query(default=None, alias="statusKey"),
    system_status_category: str | None = Query(default=None, alias="systemStatusCategory"),
    template_key: str | None = Query(default=None, alias="templateKey"),
    external_system: str | None = Query(default=None, alias="externalSystem"),
    external_type: str | None = Query(default=None, alias="externalType"),
    external_id: str | None = Query(default=None, alias="externalId"),
) -> JSONResponse:
    page = await queries.list_projects(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        workspace_id=workspace_id,
        status=status,
        status_key=status_key,
        system_status_category=system_status_category,
        template_key=template_key,
        external_system=external_system,
        external_type=external_type,
        external_id=external_id,
    )
    items = await _project_bodies(db, ctx, list(page.items))
    return JSONResponse(page_body(items, page.next_cursor))


@router.get("/projects/{project_id}", response_model=ProjectOut, responses=ERROR_RESPONSES)
async def get_project(project_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    await authorize(ctx, Permission.PROJECTS_READ)
    project = await queries.get_tenant_project(db, ctx, project_id)
    return JSONResponse(
        await _project_body(db, ctx, project),
        headers={"ETag": format_etag("project", project.version)},
    )


@router.patch("/projects/{project_id}", response_model=ProjectOut, responses=ERROR_RESPONSES)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "project")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        project = await commands.update_project(
            db,
            ctx,
            project_id=project_id,
            expected_version=expected_version,
            owner_principal_id=payload.owner_principal_id,
            clear_owner=payload.clear_owner,
            start_date=payload.start_date,
            target_date=payload.target_date,
            custom_fields=payload.custom_fields,
            settings=payload.settings,
            template_id=payload.template_id,
            template_key=payload.template_key,
            template_version=payload.template_version,
        )
        return 200, await _project_body(db, ctx, project)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post("/projects/{project_id}:archive", response_model=ProjectOut, responses=ERROR_RESPONSES)
async def archive_project(
    project_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        project = await commands.archive_project(db, ctx, project_id=project_id)
        return 200, await _project_body(db, ctx, project)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


@router.post(
    "/projects/{project_id}:transition", response_model=ProjectOut, responses=ERROR_RESPONSES
)
async def transition_project(
    project_id: uuid.UUID,
    payload: ProjectTransitionRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "project")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        project = await commands.transition_project(
            db,
            ctx,
            project_id=project_id,
            expected_version=expected_version,
            status_key=payload.status_key,
            comment=payload.comment,
        )
        return 200, await _project_body(db, ctx, project)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/projects/{project_id}/effective-config", responses=ERROR_RESPONSES)
async def get_effective_config(project_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    project, effective = await queries.effective_config(db, ctx, project_id)
    return JSONResponse(
        {
            "projectId": str(project.id),
            "version": project.version,
            "config": effective.config,
            "provenance": effective.provenance,
        }
    )


@router.get(
    "/projects/{project_id}/config-revisions", response_model=PageOut, responses=ERROR_RESPONSES
)
async def list_config_revisions(
    project_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    await authorize(ctx, Permission.PROJECTS_READ)
    project = await queries.get_tenant_project(db, ctx, project_id)
    effective_limit = clamp_limit(limit)
    stmt = select(ProjectConfigRevision).where(
        ProjectConfigRevision.project_id == project.id,
        ProjectConfigRevision.tenant_id == ctx.tenant_id,
    )
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ProjectConfigRevision.created_at < created_at)
            | (
                (ProjectConfigRevision.created_at == created_at)
                & (ProjectConfigRevision.id < entity_id)
            )
        )
    stmt = stmt.order_by(
        ProjectConfigRevision.created_at.desc(), ProjectConfigRevision.id.desc()
    ).limit(effective_limit + 1)
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return JSONResponse(page_body([dump(ConfigRevisionOut, r) for r in rows], next_cursor))


@router.post(
    "/projects/{project_id}/config-revisions",
    response_model=ConfigRevisionOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def create_config_revision(
    project_id: uuid.UUID,
    payload: ConfigRevisionCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        revision = await commands.create_config_revision(
            db, ctx, project_id=project_id, config=payload.config, comment=payload.comment
        )
        return 201, dump(ConfigRevisionOut, revision)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/projects/{project_id}/config-revisions/{revision}:activate",
    response_model=ProjectOut,
    responses=ERROR_RESPONSES,
)
async def activate_config_revision(
    project_id: uuid.UUID,
    revision: int,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "project")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        project, _ = await commands.activate_config_revision(
            db,
            ctx,
            project_id=project_id,
            revision=revision,
            expected_version=expected_version,
        )
        return 200, await _project_body(db, ctx, project)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\nrevision:{revision}",
        executor=executor,
    )


@router.get(
    "/projects/{project_id}/external-references",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
)
async def list_external_references(
    project_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    # The project scope is the generic lookup with entityType fixed (ADR-0047).
    page = await external_reference_queries.list_entity_external_references(
        db,
        ctx,
        entity_type="project",
        entity_ref=str(project_id),
        limit=limit,
        cursor=cursor,
    )
    return JSONResponse(
        page_body([dump(ExternalReferenceOut, r) for r in page.items], page.next_cursor)
    )


@router.post(
    "/projects/{project_id}/external-references",
    response_model=ExternalReferenceOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def add_external_reference(
    project_id: uuid.UUID,
    payload: ExternalReferenceCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        reference, created = await commands.add_external_reference(
            db,
            ctx,
            project_id=project_id,
            external_system=payload.external_system,
            external_type=payload.external_type,
            external_id=payload.external_id,
            metadata=payload.metadata,
        )
        return (201 if created else 200), dump(ExternalReferenceOut, reference)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
