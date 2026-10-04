"""Principals, API keys and IAM identity bindings."""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    DbDep,
    SessionFactoryDep,
    SettingsDep,
    get_iam_enforcement,
)
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    ErrorEnvelope,
    FieldErrorResponse,
    IamBindingOut,
    IamBindingUpsertRequest,
    PageOut,
    PrincipalCreateRequest,
    PrincipalDisableRequest,
    PrincipalEnabledOut,
    PrincipalEnableRequest,
    PrincipalOut,
    PrincipalUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import iam_bindings, principal_disable, principal_enable
from control_plane.application.commands import principals as commands
from control_plane.application.queries import lists as queries
from control_plane.domain.enums import Permission

router = APIRouter(tags=["principals"])


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/principals",
    response_model=PrincipalOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def create_principal(
    payload: PrincipalCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        principal = await commands.create_principal(
            db,
            ctx,
            kind=payload.kind,
            display_name=payload.display_name,
            metadata=payload.metadata,
            status=payload.status,
        )
        return 201, dump(PrincipalOut, principal)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.get("/principals", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_principals(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_principals(db, ctx, limit=limit, cursor=cursor, kind=kind)
    return JSONResponse(page_body([dump(PrincipalOut, p) for p in page.items], page.next_cursor))


_ETAG_HEADER = {"ETag": {"schema": {"type": "string"}, "description": '"principal-<version>"'}}


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.get(
    "/principals/{principal_id}",
    response_model=PrincipalOut,
    responses={**ERROR_RESPONSES, 200: {"headers": _ETAG_HEADER}},
)
async def get_principal(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    principal = await queries.get_principal(db, ctx, principal_id)
    return JSONResponse(
        dump(PrincipalOut, principal),
        headers={"ETag": format_etag("principal", principal.version)},
    )


# The route reads its body itself (CP-ADR-0082 §1.4): credential-shaped
# material is refused before the shape is checked, and neither refusal is
# the framework's ``400 invalid_request`` that would echo the input. The
# request body and the If-Match header are documented here instead.
_UPDATE_BODY_SCHEMA = {
    key: value
    for key, value in PrincipalUpdateRequest.model_json_schema(
        by_alias=True, ref_template="#/components/schemas/{model}"
    ).items()
    if key != "$defs"
}
_UPDATE_OPENAPI: dict[str, Any] = {
    "parameters": [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "examples": ['"principal-3"']},
        },
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
        },
    ],
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": _UPDATE_BODY_SCHEMA}},
    },
}


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.patch(
    "/principals/{principal_id}",
    response_model=PrincipalOut,
    responses={
        **ERROR_RESPONSES,
        200: {"headers": _ETAG_HEADER},
        400: {"model": ErrorEnvelope, "description": "invalid_if_match"},
        403: {"model": ErrorEnvelope, "description": "permission_denied (principals.write)"},
        409: {
            "model": ErrorEnvelope,
            "description": "version_conflict | principal_managed_by_registry"
            " | idempotency_key_reused",
        },
        422: {
            "model": FieldErrorResponse,
            "description": "validation_error | secret_material_rejected"
            " (details.errors, no field values)",
        },
        428: {"model": ErrorEnvelope, "description": "if_match_required"},
    },
    summary="Change the display name and profile of a principal",
    openapi_extra=_UPDATE_OPENAPI,
)
async def update_principal(
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    expected_version = parse_if_match(request.headers.get("If-Match"), "principal")
    raw = await request.body()
    try:
        body: Any = json.loads(raw)
    except (ValueError, RecursionError):
        # Not JSON at all (or nested past the parser) is "the body is not an
        # object" (path /) to the checks, without an echo of what was sent.
        body = None

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        principal = await commands.update_principal(
            db, ctx, principal_id=principal_id, expected_version=expected_version, body=body
        )
        return 200, dump(PrincipalOut, principal)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n" + raw.decode("utf-8", "replace"),
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    if response.status_code == 200:
        version = json.loads(bytes(response.body))["version"]
        response.headers["ETag"] = format_etag("principal", version)
    return response


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/principals/{principal_id}:disable",
    response_model=PrincipalOut,
    responses=ERROR_RESPONSES,
    summary="Disable a human or agent: revoke its bindings, close its sessions, free its claims",
)
async def disable_principal(
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: PrincipalDisableRequest | None = None,
) -> JSONResponse:
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await principal_disable.disable_principal(
            db, ctx, principal_id=principal_id, reason=payload.reason if payload else None
        )
        touched.extend(result.touched_identities)
        return 200, dump(PrincipalOut, result.principal)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    for issuer, iam_principal_id in touched:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/principals/{principal_id}:enable",
    response_model=PrincipalEnabledOut,
    responses=ERROR_RESPONSES,
    summary="Enable a disabled human or agent; IAM bindings revoked by :disable stay revoked",
)
async def enable_principal(
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: PrincipalEnableRequest | None = None,
) -> JSONResponse:
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await principal_enable.enable_principal(
            db, ctx, principal_id=principal_id, reason=payload.reason if payload else None
        )
        touched.extend(result.touched_identities)
        return 200, dump(PrincipalOut, result.principal, liveApiKeys=result.live_api_keys)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    # Answers cached while it was disabled (``principal_not_active``, a revoked
    # binding) must not outlive the change: the next request reads the base.
    for issuer, iam_principal_id in touched:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/principals/{principal_id}/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Issue an API key; the full key is returned only in this response",
)
async def create_api_key(
    principal_id: uuid.UUID,
    payload: ApiKeyCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        created = await commands.create_api_key(
            db,
            ctx,
            principal_id=principal_id,
            permissions=payload.permissions,
            expires_at=payload.expires_at,
        )
        return 201, dump(ApiKeyOut, created.api_key, key=created.generated.full_key)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
        # The full key is a one-time secret: never persisted in the
        # idempotency store, so a replay returns the record with key=null.
        sensitive_fields=("key",),
    )


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/api-keys/{api_key_id}:revoke",
    response_model=ApiKeyOut,
    responses=ERROR_RESPONSES,
)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        api_key = await commands.revoke_api_key(db, ctx, api_key_id=api_key_id)
        return 200, dump(ApiKeyOut, api_key)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


# --- IAM identity bindings (ADR-0053) -----------------------------------------


def forget_binding_cache(request: Request, issuer: str, iam_principal_id: uuid.UUID) -> None:
    """Drop the enforcement cache for one identity once its binding changed.

    Done after the transaction committed, never inside it: a cache dropped for
    a write that then rolls back would just be reloaded with the old row, but
    a cache kept for a write that committed would let a revoked identity in
    for a whole TTL.
    """
    enforcement = get_iam_enforcement(request)
    if enforcement is not None:
        enforcement.bindings.invalidate(issuer, iam_principal_id)


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.get(
    "/principals/{principal_id}/iam-bindings",
    responses=ERROR_RESPONSES,
    summary="Federated identities bound to a principal, revoked ones included",
)
async def list_iam_bindings(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    bindings = await queries.list_iam_bindings(db, ctx, principal_id)
    return JSONResponse({"items": [dump(IamBindingOut, b) for b in bindings]})


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/principals/{principal_id}/iam-bindings",
    response_model=IamBindingOut,
    responses={**ERROR_RESPONSES, 201: {"model": IamBindingOut}},
    summary="Bind an IAM identity to a principal (upsert by issuer + IAM principal)",
)
async def upsert_iam_binding(
    principal_id: uuid.UUID,
    payload: IamBindingUpsertRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await iam_bindings.upsert_iam_binding(
            db,
            ctx,
            principal_id=principal_id,
            issuer=payload.issuer,
            iam_tenant_id=payload.iam_tenant_id,
            iam_principal_id=payload.iam_principal_id,
            permissions=payload.permissions,
            trusted_issuer=settings.iam_issuer,
            # Checked by the command after principals.write (CP-ADR-0082 B5).
            visibility=(
                payload.visibility
                if "visibility" in payload.model_fields_set
                else iam_bindings.VISIBILITY_UNSET
            ),
        )
        return (201 if result.created else 200), dump(IamBindingOut, result.binding)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    forget_binding_cache(request, payload.issuer, payload.iam_principal_id)
    return response


# visibility: tenant — principals, their keys and bindings are objects of the tenant
@router.post(
    "/iam-bindings/{binding_id}:revoke",
    response_model=IamBindingOut,
    responses=ERROR_RESPONSES,
    summary="Close entry for a federated identity without waiting for its token to expire",
)
async def revoke_iam_binding(
    binding_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    identity: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        binding = await iam_bindings.revoke_iam_binding(db, ctx, binding_id=binding_id)
        identity.append((binding.issuer, binding.iam_principal_id))
        return 200, dump(IamBindingOut, binding)

    response = await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
    for issuer, iam_principal_id in identity:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response
