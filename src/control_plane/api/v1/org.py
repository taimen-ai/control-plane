"""Organization registries: roles, capabilities, skills."""

import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    CapabilityCreateRequest,
    CapabilityOut,
    PageOut,
    RoleCreateRequest,
    RoleHolderOut,
    RoleOut,
    RoleUpdateRequest,
    SkillOut,
    SkillRegisterRequest,
    SkillUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import org as commands
from control_plane.application.queries import org as queries
from control_plane.application.queries.package_links import attach_package, attach_packages

router = APIRouter(tags=["organization"])


# --- roles --------------------------------------------------------------------


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.post("/roles", response_model=RoleOut, status_code=201, responses=ERROR_RESPONSES)
async def create_role(
    payload: RoleCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        role = await commands.create_role(
            db,
            ctx,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            workspace_id=payload.workspace_id,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "Role", dump(RoleOut, role), key_field="slug"
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/roles", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_roles(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    page = await queries.list_roles(
        db, ctx, limit=limit, cursor=cursor, workspace_id=workspace_id, package=package
    )
    items = [dump(RoleOut, r) for r in page.items]
    await attach_packages(db, ctx.tenant_id, "Role", items, key_field="slug")
    return JSONResponse(page_body(items, page.next_cursor))


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/roles/{role_id}", response_model=RoleOut, responses=ERROR_RESPONSES)
async def get_role(role_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    role = await queries.get_role(db, ctx, role_id)
    body = await attach_package(db, ctx.tenant_id, "Role", dump(RoleOut, role), key_field="slug")
    return JSONResponse(body, headers={"ETag": format_etag("role", role.version)})


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/roles/{role_id}/principals", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_role_holders(
    role_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
) -> JSONResponse:
    page = await queries.list_role_holders(
        db, ctx, role_id, workspace_id=workspace_id, limit=limit, cursor=cursor
    )
    return JSONResponse(page_body([dump(RoleHolderOut, p) for p in page.items], page.next_cursor))


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.patch("/roles/{role_id}", response_model=RoleOut, responses=ERROR_RESPONSES)
async def update_role(
    role_id: uuid.UUID,
    payload: RoleUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "role")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        role = await commands.update_role(
            db,
            ctx,
            role_id=role_id,
            expected_version=expected_version,
            name=payload.name,
            description=payload.description,
        )
        return 200, await attach_package(
            db, ctx.tenant_id, "Role", dump(RoleOut, role), key_field="slug"
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# --- capabilities -------------------------------------------------------------


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.post(
    "/capabilities", response_model=CapabilityOut, status_code=201, responses=ERROR_RESPONSES
)
async def create_capability(
    payload: CapabilityCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        capability = await commands.create_capability(
            db, ctx, name=payload.name, description=payload.description
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "Capability", dump(CapabilityOut, capability), key_field="name"
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/capabilities", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_capabilities(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    page = await queries.list_capabilities(db, ctx, limit=limit, cursor=cursor, package=package)
    items = [dump(CapabilityOut, c) for c in page.items]
    await attach_packages(db, ctx.tenant_id, "Capability", items, key_field="name")
    return JSONResponse(page_body(items, page.next_cursor))


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get(
    "/capabilities/{capability_id}", response_model=CapabilityOut, responses=ERROR_RESPONSES
)
async def get_capability(capability_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    capability = await queries.get_capability(db, ctx, capability_id)
    body = dump(CapabilityOut, capability)
    return JSONResponse(
        await attach_package(db, ctx.tenant_id, "Capability", body, key_field="name")
    )


# --- skills -------------------------------------------------------------------


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.post("/skills", response_model=SkillOut, status_code=201, responses=ERROR_RESPONSES)
async def register_skill(
    payload: SkillRegisterRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        skill = await commands.register_skill(
            db,
            ctx,
            name=payload.name,
            version=payload.version,
            description=payload.description,
            protocol=payload.protocol,
            config=payload.config,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            side_effects=payload.side_effects,
            risk_level=payload.risk_level,
            contract=payload.contract,
        )
        return 201, await attach_package(
            db, ctx.tenant_id, "Skill", dump(SkillOut, skill), key_field="name"
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/skills", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_skills(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    page = await queries.list_skills(
        db, ctx, limit=limit, cursor=cursor, name=name, status=status, package=package
    )
    items = [dump(SkillOut, s) for s in page.items]
    await attach_packages(db, ctx.tenant_id, "Skill", items, key_field="name")
    return JSONResponse(page_body(items, page.next_cursor))


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.get("/skills/{skill_ref}", response_model=SkillOut, responses=ERROR_RESPONSES)
async def get_skill(skill_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    """By id, ``name@version`` or ``name`` (resolution of ADR-0021); the body
    carries the full contract v1 when the version has one (ADR-0056). The
    legacy ``config`` is returned empty to callers without ``org.read``."""
    skill, config_visible = await queries.get_skill_by_ref(db, ctx, skill_ref)
    body = await attach_package(db, ctx.tenant_id, "Skill", dump(SkillOut, skill), key_field="name")
    if not config_visible:
        body["config"] = {}
    return JSONResponse(body, headers={"ETag": format_etag("skill", skill.row_version)})


# visibility: tenant — roles, capabilities and skills are objects of the tenant
@router.patch("/skills/{skill_id}", response_model=SkillOut, responses=ERROR_RESPONSES)
async def update_skill(
    skill_id: uuid.UUID,
    payload: SkillUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "skill")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        skill = await commands.update_skill(
            db,
            ctx,
            skill_id=skill_id,
            expected_row_version=expected_version,
            description=payload.description,
            config=payload.config,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            status=payload.status,
            endpoint=payload.endpoint,
        )
        return 200, await attach_package(
            db, ctx.tenant_id, "Skill", dump(SkillOut, skill), key_field="name"
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
