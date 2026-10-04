"""One-time bootstrap endpoint, guarded by CONTROL_PLANE_BOOTSTRAP_TOKEN."""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ApiKeyOut,
    BootstrapOut,
    BootstrapRequest,
    IamBindingOut,
    PrincipalOut,
    TenantOut,
    dump,
)
from control_plane.application.commands.bootstrap import BootstrapIamBinding, bootstrap
from control_plane.domain.errors import AuthenticationError, AuthorizationError
from control_plane.infrastructure.auth.service import parse_bearer
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["bootstrap"])


# authz: public — защищён bootstrap-токеном (CP_BOOTSTRAP_TOKEN), сравнение ниже
# visibility: tenant — no workspace exists before the tenant does
@router.post(
    "/bootstrap",
    response_model=BootstrapOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create the first tenant, admin principal, admin API key and, optionally, "
    "the admin's IAM binding",
)
async def bootstrap_endpoint(
    payload: BootstrapRequest,
    request: Request,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    if not settings.bootstrap_token:
        raise AuthorizationError(
            "Bootstrap is disabled: CP_BOOTSTRAP_TOKEN is not configured",
            code="bootstrap_disabled",
        )
    presented = parse_bearer(request.headers.get("authorization"))
    if presented is None or not secrets.compare_digest(presented, settings.bootstrap_token):
        raise AuthenticationError("Invalid bootstrap token")

    request_id: str = request.state.request_id
    async with transaction(session_factory) as session:
        result = await bootstrap(
            session,
            tenant_slug=payload.tenant_slug,
            tenant_name=payload.tenant_name,
            tenant_id=payload.tenant_id,
            admin_display_name=payload.admin_display_name,
            request_id=request_id,
            trace_run_id=getattr(request.state, "trace_run_id", ""),
            iam_binding=(
                BootstrapIamBinding(
                    issuer=payload.iam_binding.issuer,
                    iam_tenant_id=payload.iam_binding.iam_tenant_id,
                    iam_principal_id=payload.iam_binding.iam_principal_id,
                )
                if payload.iam_binding is not None
                else None
            ),
        )
        body = {
            "tenant": dump(TenantOut, result.tenant),
            "adminPrincipal": dump(PrincipalOut, result.admin),
            "apiKey": dump(ApiKeyOut, result.api_key, key=result.generated.full_key),
            "iamBinding": (
                dump(IamBindingOut, result.iam_binding) if result.iam_binding is not None else None
            ),
        }
    return JSONResponse(status_code=201, content=body)
