"""Agent registry endpoints (CP-ADR-0073).

An agent is a catalog object with a key; every applied change of its spec is a
new immutable revision (a new one only when the canonical hash differs). The
desired state — running or stopped, how many replicas — lives beside the
revisions, and the observed state is written by the placement service alone.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.principals import forget_binding_cache
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    AgentIdentityLinkRequest,
    AgentIdentityReplaceRequest,
    AgentMeOut,
    AgentOut,
    AgentPageOut,
    AgentPublishRequest,
    AgentRetireRequest,
    AgentRevisionPageOut,
    AgentRevisionSummaryOut,
    AgentStateUpdateRequest,
    AgentStatusOut,
    AgentStatusReport,
    AgentValidationOut,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import agents as commands
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import (
    attach_package,
    attach_packages,
    in_package,
)
from control_plane.application.queries.package_settings import object_settings
from control_plane.domain.enums import AgentStatus, Permission
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import Agent, AgentObservedStatus, AgentRevision

router = APIRouter(tags=["agents"])


def agent_body(agent: Agent, revision: AgentRevision) -> dict[str, Any]:
    return AgentOut.model_validate(
        {
            "id": agent.id,
            "tenant_id": agent.tenant_id,
            "key": agent.key,
            "display_name": agent.display_name,
            "status": agent.status,
            "state": agent.state,
            "replicas": agent.replicas,
            "current_revision": agent.current_revision,
            "revision": {
                "id": revision.id,
                "agent_id": agent.id,
                "agent_key": agent.key,
                "revision": revision.revision,
                "spec": revision.spec,
                "spec_hash": revision.spec_hash,
                "created_by": revision.created_by,
                "created_at": revision.created_at,
            },
            "principal_id": agent.principal_id,
            "workspace_id": agent.workspace_id,
            "retired_at": agent.retired_at,
            "retired_by": agent.retired_by,
            "version": agent.version,
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
        }
    ).model_dump(mode="json", by_alias=True)


def status_body(agent: Agent, row: AgentObservedStatus | None) -> dict[str, Any]:
    if row is None:
        observed: dict[str, Any] = {
            "phase": "unknown",
            "reason": None,
            "observed_revision": None,
            "node": None,
            "instances": None,
            "observed_at": None,
            "reported_by": None,
            "updated_at": None,
        }
    else:
        observed = {
            "phase": row.phase,
            "reason": {"code": row.reason_code, "message": row.reason_message or ""}
            if row.reason_code is not None
            else None,
            "observed_revision": row.observed_revision,
            "node": row.node,
            "instances": {"desired": row.instances_desired, "ready": row.instances_ready},
            "observed_at": row.observed_at,
            "reported_by": row.reported_by,
            "updated_at": row.updated_at,
        }
    return AgentStatusOut.model_validate({"agent_key": agent.key, **observed}).model_dump(
        mode="json", by_alias=True
    )


def _spec_as_sent(payload: AgentPublishRequest) -> dict[str, Any]:
    """The spec without defaults filled in: that is what a revision stores and hashes."""
    return payload.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)


def _forget(request: Request, identities: list[tuple[str, uuid.UUID]]) -> None:
    for issuer, iam_principal_id in identities:
        forget_binding_cache(request, issuer, iam_principal_id)


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.post(
    "/agents",
    response_model=AgentOut,
    status_code=201,
    responses={**ERROR_RESPONSES, 200: {"model": AgentOut, "description": "Spec unchanged"}},
    summary="Publish an agent spec: a new revision only when its canonical hash differs",
)
async def publish_agent(
    payload: AgentPublishRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_MANAGE)
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.publish_agent(
            db,
            ctx,
            key=payload.key,
            spec=_spec_as_sent(payload),
            package=(payload.package.key, payload.package.version) if payload.package else None,
        )
        touched.extend(view.touched_identities)
        return (201 if view.created else 200), await attach_package(
            db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision)
        )

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    _forget(request, touched)
    return response


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.post(
    "/agents:validate",
    response_model=AgentValidationOut,
    responses=ERROR_RESPONSES,
    summary="Run every check of POST /agents without saving anything",
)
async def validate_agent(payload: AgentPublishRequest, ctx: AuthDep, db: DbDep) -> JSONResponse:
    result = await commands.validate_agent(db, ctx, key=payload.key, spec=_spec_as_sent(payload))
    return JSONResponse(
        AgentValidationOut(
            key=payload.key,
            spec_hash=result.checked.spec_hash,
            current_revision=result.agent.current_revision if result.agent else None,
            would_create_revision=result.would_create_revision,
            would_change_state=result.would_change_state,
        ).model_dump(mode="json", by_alias=True)
    )


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.get(
    "/agents",
    response_model=AgentPageOut,
    responses=ERROR_RESPONSES,
    summary="A page of agents; include=status adds the observed state of each",
)
async def list_agents(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: Literal["active", "retired"] | None = Query(default=None),
    state: Literal["running", "stopped"] | None = Query(default=None),
    workspace_id: str | None = Query(default=None, alias="workspaceId"),
    include: Literal["status"] | None = Query(
        default=None,
        description="status: each item carries observedStatus, the body of "
        "GET /agents/{key}/status, read by the same query as the page",
    ),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_READ)
    effective_limit = clamp_limit(limit)
    stmt = (
        select(Agent, AgentRevision, AgentObservedStatus)
        .join(
            AgentRevision,
            (AgentRevision.agent_id == Agent.id)
            & (AgentRevision.revision == Agent.current_revision),
        )
        # One row per agent at most (agent_id is the key): the page stays one query.
        .outerjoin(AgentObservedStatus, AgentObservedStatus.agent_id == Agent.id)
        .where(Agent.tenant_id == ctx.tenant_id)
    )
    if status is not None:
        stmt = stmt.where(Agent.status == status)
    if state is not None:
        stmt = stmt.where(Agent.state == state)
    if workspace_id is not None:
        try:
            stmt = stmt.where(Agent.workspace_id == uuid.UUID(workspace_id))
        except ValueError as exc:
            raise ValidationError(
                "invalid_request",
                "workspaceId must be a UUID",
                details={"workspaceId": workspace_id},
            ) from exc
    if package is not None:
        stmt = stmt.where(in_package("Agent", Agent.tenant_id, Agent.key, package))
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (Agent.created_at < created_at)
            | ((Agent.created_at == created_at) & (Agent.id < entity_id))
        )
    stmt = stmt.order_by(Agent.created_at.desc(), Agent.id.desc()).limit(effective_limit + 1)
    rows = list((await db.execute(stmt)).tuples().all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1][0].created_at, rows[-1][0].id)
    if include == "status":
        items = [{**agent_body(a, r), "observedStatus": status_body(a, o)} for a, r, o in rows]
    else:
        items = [agent_body(a, r) for a, r, _ in rows]
    await attach_packages(db, ctx.tenant_id, "Agent", items)
    return JSONResponse(page_body(items, next_cursor))


# Declared before /agents/{ref}: "me" is not an agent key.
# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.get(
    "/agents/me",
    response_model=AgentMeOut,
    responses=ERROR_RESPONSES,
    summary="The agent the caller is, with its current revision and its package's settings",
)
async def get_my_agent(ctx: AuthDep, db: DbDep) -> JSONResponse:
    # Authentication is the whole check: an executor reads its own spec and
    # the settings of the package that describes it (CP-ADR-0081 §8).
    view = await commands.my_agent(db, ctx)
    body = await attach_package(db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision))
    body["packageSettings"] = await object_settings(db, ctx.tenant_id, "Agent", view.agent.key)
    return JSONResponse(body)


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.get(
    "/agents/{ref}",
    response_model=AgentOut,
    responses=ERROR_RESPONSES,
    summary="An agent by key (current revision) or key@revision",
)
async def get_agent(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    view = await commands.resolve_agent(db, ctx, ref)
    return JSONResponse(
        await attach_package(db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision))
    )


def revision_summary_body(
    agent: Agent, revision: AgentRevision, previous_spec: dict[str, Any] | None
) -> dict[str, Any]:
    package = (
        {"key": revision.source_package_key, "version": revision.source_package_version}
        if revision.source_package_key is not None
        else None
    )
    return AgentRevisionSummaryOut.model_validate(
        {
            "id": revision.id,
            "agent_key": agent.key,
            "revision": revision.revision,
            "spec_hash": revision.spec_hash,
            "created_by": revision.created_by,
            "created_at": revision.created_at,
            "source": {"kind": revision.source_kind, "package": package},
            "active": agent.status == AgentStatus.ACTIVE
            and revision.revision == agent.current_revision,
            "changed_fields": commands.changed_fields(previous_spec, revision.spec)
            if previous_spec is not None
            else None,
        }
    ).model_dump(mode="json", by_alias=True)


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.get(
    "/agents/{key}/revisions",
    response_model=AgentRevisionPageOut,
    responses=ERROR_RESPONSES,
    summary="Revisions of an agent, newest first, without their specs (those are key@revision)",
)
async def list_agent_revisions(
    key: str,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await commands.list_revisions(db, ctx, key=key, limit=limit, cursor=cursor)
    items = [revision_summary_body(page.agent, r, p) for r, p in page.items]
    return JSONResponse(page_body(items, page.next_cursor))


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.patch(
    "/agents/{key}/state",
    response_model=AgentOut,
    responses=ERROR_RESPONSES,
    summary="Change the desired state or replicas; never a new revision",
)
async def update_agent_state(
    key: str,
    payload: AgentStateUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_MANAGE)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.update_agent_state(
            db, ctx, key=key, state=payload.state, replicas=payload.replicas
        )
        return 200, await attach_package(
            db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.post(
    "/agents/{key}:retire",
    response_model=AgentOut,
    responses=ERROR_RESPONSES,
    summary="Retire an agent: stop it, revoke its binding, keep its history",
)
async def retire_agent(
    key: str,
    payload: AgentRetireRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_MANAGE)
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.retire_agent(db, ctx, key=key, reason=payload.reason)
        touched.extend(view.touched_identities)
        return 200, await attach_package(
            db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision)
        )

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    _forget(request, touched)
    return response


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.put(
    "/agents/{key}/identity",
    response_model=AgentOut,
    responses=ERROR_RESPONSES,
    summary="Link the IAM identity of an agent; the core derives principal and binding",
)
async def link_agent_identity(
    key: str,
    payload: AgentIdentityLinkRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_STATUS_WRITE)
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.link_agent_identity(
            db,
            ctx,
            key=key,
            issuer=payload.issuer,
            iam_tenant_id=payload.iam_tenant_id,
            iam_principal_id=payload.iam_principal_id,
            trusted_issuer=settings.iam_issuer,
        )
        touched.extend(view.touched_identities)
        return 200, await attach_package(
            db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision)
        )

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    # A negative answer may already be cached for this identity (the executor
    # tried to enter before it was linked): drop it now, not after the TTL.
    _forget(request, touched)
    return response


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.post(
    "/agents/{key}/identity:replace",
    response_model=AgentOut,
    responses=ERROR_RESPONSES,
    summary="Move a service agent to a new IAM identity; the principal stays the same",
)
async def replace_agent_identity(
    key: str,
    payload: AgentIdentityReplaceRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_MANAGE)
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.replace_agent_identity(
            db,
            ctx,
            key=key,
            issuer=payload.issuer,
            iam_tenant_id=payload.iam_tenant_id,
            iam_principal_id=payload.iam_principal_id,
            reason=payload.reason,
            trusted_issuer=settings.iam_issuer,
        )
        touched.extend(view.touched_identities)
        return 200, await attach_package(
            db, ctx.tenant_id, "Agent", agent_body(view.agent, view.revision)
        )

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
        # Takes the caller with the agent's principal, in id order.
        lock_caller_first=False,
    )
    # The previous identity loses entry and the new one gains it now, not
    # after the cache TTL.
    _forget(request, touched)
    return response


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.get(
    "/agents/{key}/status",
    response_model=AgentStatusOut,
    responses=ERROR_RESPONSES,
    summary="Observed state of an agent",
)
async def get_agent_status(key: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    agent, row = await commands.get_agent_status(db, ctx, key=key)
    return JSONResponse(status_body(agent, row))


# visibility: tenant — an agent of the registry is an object of the tenant (CP-ADR-0082 3.8)
@router.put(
    "/agents/{key}/status",
    response_model=AgentStatusOut,
    responses=ERROR_RESPONSES,
    summary="Report the observed state of an agent (placement service only)",
)
async def report_agent_status(
    key: str,
    payload: AgentStatusReport,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.AGENTS_STATUS_WRITE)
    report = commands.StatusReport(
        phase=payload.phase,
        reason_code=payload.reason.code if payload.reason else None,
        reason_message=payload.reason.message if payload.reason else None,
        observed_revision=payload.observed_revision,
        node=payload.node,
        instances_desired=payload.instances.desired,
        instances_ready=payload.instances.ready,
        observed_at=payload.observed_at,
    )

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        agent, row = await commands.report_agent_status(db, ctx, key=key, report=report)
        return 200, status_body(agent, row)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
