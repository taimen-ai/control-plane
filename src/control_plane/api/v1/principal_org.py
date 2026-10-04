"""Principal organization assignments: roles, capabilities, skills."""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    CapabilityAssignRequest,
    CapabilityOut,
    RoleAssignRequest,
    RoleOut,
    ScopeRevokeRequest,
    SkillAssignRequest,
    SkillOut,
    dump,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.commands import org as commands
from control_plane.application.queries import org as queries
from control_plane.application.queries.package_links import attach_package, attach_packages
from control_plane.infrastructure.db.models import (
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
)

router = APIRouter(tags=["principal-organization"])


def _assignment_body(
    assignment: PrincipalRole | PrincipalCapability | PrincipalSkill,
    embedded_key: str,
    embedded: dict[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": str(assignment.id),
        "principalId": str(assignment.principal_id),
        "createdAt": assignment.created_at.isoformat(),
        embedded_key: embedded,
    }
    if isinstance(assignment, PrincipalRole):
        body["workspaceId"] = str(assignment.workspace_id) if assignment.workspace_id else None
    else:
        body["metadata"] = assignment.metadata_json
    return body


# --- roles --------------------------------------------------------------------


# visibility: tenant — principals and their grants are objects of the tenant
@router.post("/principals/{principal_id}/roles", status_code=201, responses=ERROR_RESPONSES)
async def assign_role(
    principal_id: uuid.UUID,
    payload: RoleAssignRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        assignment = await commands.assign_role(
            db,
            ctx,
            principal_id=principal_id,
            role_id=payload.role_id,
            workspace_id=payload.workspace_id,
        )
        role = await commands.get_tenant_role(db, ctx, payload.role_id)
        body = await attach_package(
            db, ctx.tenant_id, "Role", dump(RoleOut, role), key_field="slug"
        )
        return 201, _assignment_body(assignment, "role", body)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.get("/principals/{principal_id}/roles", responses=ERROR_RESPONSES)
async def list_roles(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    rows = await queries.list_principal_roles(db, ctx, principal_id)
    bodies = [dump(RoleOut, role) for _, role in rows]
    await attach_packages(db, ctx.tenant_id, "Role", bodies, key_field="slug")
    return JSONResponse(
        {
            "items": [
                _assignment_body(assignment, "role", body)
                for (assignment, _), body in zip(rows, bodies, strict=True)
            ]
        }
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.post(
    "/principals/{principal_id}/roles/{role_id}:revoke",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def revoke_role(
    principal_id: uuid.UUID,
    role_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ScopeRevokeRequest | None = None,
) -> Response:
    workspace_id = payload.workspace_id if payload else None

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.revoke_role(
            db, ctx, principal_id=principal_id, role_id=role_id, workspace_id=workspace_id
        )
        return 204, {}

    return as_no_content(
        await execute_write(
            request,
            ctx,
            settings,
            session_factory,
            canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
            executor=executor,
        )
    )


# --- capabilities -------------------------------------------------------------


# visibility: tenant — principals and their grants are objects of the tenant
@router.post("/principals/{principal_id}/capabilities", status_code=201, responses=ERROR_RESPONSES)
async def assign_capability(
    principal_id: uuid.UUID,
    payload: CapabilityAssignRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        assignment = await commands.assign_capability(
            db,
            ctx,
            principal_id=principal_id,
            capability_id=payload.capability_id,
            metadata=payload.metadata,
        )
        capability = await commands.get_tenant_capability(db, ctx, payload.capability_id)
        body = await attach_package(
            db, ctx.tenant_id, "Capability", dump(CapabilityOut, capability), key_field="name"
        )
        return 201, _assignment_body(assignment, "capability", body)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.get("/principals/{principal_id}/capabilities", responses=ERROR_RESPONSES)
async def list_capabilities(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    rows = await queries.list_principal_capabilities(db, ctx, principal_id)
    bodies = [dump(CapabilityOut, capability) for _, capability in rows]
    await attach_packages(db, ctx.tenant_id, "Capability", bodies, key_field="name")
    return JSONResponse(
        {
            "items": [
                _assignment_body(assignment, "capability", body)
                for (assignment, _), body in zip(rows, bodies, strict=True)
            ]
        }
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.post(
    "/principals/{principal_id}/capabilities/{capability_id}:revoke",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def revoke_capability(
    principal_id: uuid.UUID,
    capability_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.revoke_capability(
            db, ctx, principal_id=principal_id, capability_id=capability_id
        )
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )


# --- skills -------------------------------------------------------------------


# visibility: tenant — principals and their grants are objects of the tenant
@router.post("/principals/{principal_id}/skills", status_code=201, responses=ERROR_RESPONSES)
async def assign_skill(
    principal_id: uuid.UUID,
    payload: SkillAssignRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        assignment = await commands.assign_skill(
            db,
            ctx,
            principal_id=principal_id,
            skill_id=payload.skill_id,
            metadata=payload.metadata,
        )
        skill = await commands.get_tenant_skill(db, ctx, payload.skill_id)
        body = await attach_package(
            db, ctx.tenant_id, "Skill", dump(SkillOut, skill), key_field="name"
        )
        return 201, _assignment_body(assignment, "skill", body)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.get("/principals/{principal_id}/skills", responses=ERROR_RESPONSES)
async def list_skills(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    rows = await queries.list_principal_skills(db, ctx, principal_id)
    bodies = [dump(SkillOut, skill) for _, skill in rows]
    await attach_packages(db, ctx.tenant_id, "Skill", bodies, key_field="name")
    return JSONResponse(
        {
            "items": [
                _assignment_body(assignment, "skill", body)
                for (assignment, _), body in zip(rows, bodies, strict=True)
            ]
        }
    )


# visibility: tenant — principals and their grants are objects of the tenant
@router.post(
    "/principals/{principal_id}/skills/{skill_id}:revoke",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def revoke_skill(
    principal_id: uuid.UUID,
    skill_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.revoke_skill(db, ctx, principal_id=principal_id, skill_id=skill_id)
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )
