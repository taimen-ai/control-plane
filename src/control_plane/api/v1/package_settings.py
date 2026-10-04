"""Settings of packages: ``GET /package-settings``, ``GET|PUT /packages/{key}/settings`` and
``GET /packages/{key}/settings/versions`` (CP-ADR-0081 §4).

The contract was agreed with the console on 2026-10-03 (its ``docs/settings.md``).
Optimistic concurrency as for rules and workspaces: ``GET`` returns
``ETag: "package-settings-<version>"`` and ``PUT`` requires ``If-Match``
(``"package-settings-0"`` before the first saving). The checks of a saving —
:mod:`control_plane.application.commands.package_settings`.
"""

import json
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ErrorEnvelope,
    PackageSettingsListOut,
    PackageSettingsOut,
    PackageSettingsPutRequest,
    PackageSettingsVersionPageOut,
    page_body,
)
from control_plane.api.v1.views import LOCALE_DESCRIPTION, _locale
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import package_settings as commands
from control_plane.application.queries import package_settings as queries
from control_plane.application.queries.lists import clamp_limit
from control_plane.domain.errors import BadRequestError, ValidationError
from control_plane.domain.redaction import REDACTED, secret_material

router = APIRouter(tags=["package-settings"])

ENTITY = queries.ETAG_ENTITY
# Versions of the history a page holds at most (CP-ADR-0081 §4); 50 by default.
MAX_VERSIONS = 100

_PUT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    404: {
        "model": ErrorEnvelope,
        "description": "package_not_installed, or settings_not_declared: the installed"
        " revision declares no settings",
    },
    409: {
        "model": ErrorEnvelope,
        "description": "version_conflict: If-Match is stale, details.currentVersion",
    },
    422: {
        "model": ErrorEnvelope,
        "description": "secret_material_rejected (details.errors[{path}], no value),"
        " settings_invalid (details.errors[{path, code, message?}]), unknown_ref"
        " (details.errors[{path, ref}])",
    },
    428: {"model": ErrorEnvelope, "description": "if_match_required"},
}
_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    404: _PUT_RESPONSES[404],
}


def _with_etag(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, headers={"ETag": format_etag(ENTITY, body["version"])})


# visibility: tenant — the settings of a package are shared by the tenant (CP-ADR-0082 4)
@router.get(
    "/package-settings",
    response_model=PackageSettingsListOut,
    responses=ERROR_RESPONSES,
    summary="The packages whose installed revision declares settings",
)
async def list_package_settings(
    ctx: AuthDep,
    db: DbDep,
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
) -> JSONResponse:
    items = await queries.list_settings(db, ctx, _locale(locale))
    return JSONResponse({"items": items})


# visibility: tenant — the settings of a package are shared by the tenant (CP-ADR-0082 4)
@router.get(
    "/packages/{key}/settings",
    response_model=PackageSettingsOut,
    responses=_READ_RESPONSES,
    summary="The settings of a package: schema and layout with their strings, saved and"
    " effective values",
)
async def get_package_settings(
    key: str,
    ctx: AuthDep,
    db: DbDep,
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
) -> JSONResponse:
    return _with_etag(await queries.get_settings(db, ctx, key, _locale(locale)))


def _reject_extra(payload: PackageSettingsPutRequest) -> None:
    """Members of the body other than ``values``; a name shaped like a credential is not quoted."""
    extra = sorted(
        REDACTED if secret_material(name) else name for name in (payload.model_extra or {})
    )
    if extra:
        raise BadRequestError(
            "invalid_request",
            "Request does not match the API contract",
            details={
                "errors": [
                    {"loc": f"body.{name}", "message": "Extra inputs are not permitted"}
                    for name in extra
                ]
            },
        )


# visibility: tenant — the settings of a package are shared by the tenant (CP-ADR-0082 4)
@router.put(
    "/packages/{key}/settings",
    response_model=PackageSettingsOut,
    responses=_PUT_RESPONSES,
    summary="Save the settings of a package whole: a new version, or the state as it is"
    " when nothing changes",
)
async def put_package_settings(
    key: str,
    payload: PackageSettingsPutRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, ENTITY)
    _reject_extra(payload)
    wanted = _locale(locale)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, Any]]:
        body = await commands.put_settings(
            db,
            ctx,
            key=key,
            expected_version=expected_version,
            values=payload.values,
            locale=wanted,
        )
        return 200, body

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + json.dumps(payload.values, sort_keys=True, separators=(",", ":")),
        executor=executor,
    )
    if response.status_code == 200:
        response.headers["ETag"] = format_etag(ENTITY, _version(response))
    return response


def _version(response: JSONResponse) -> int:
    body = json.loads(bytes(response.body))
    return int(body["version"])


# visibility: tenant — the settings of a package are shared by the tenant (CP-ADR-0082 4)
@router.get(
    "/packages/{key}/settings/versions",
    response_model=PackageSettingsVersionPageOut,
    responses=_READ_RESPONSES,
    summary="The history of the settings of a package, newest first",
)
async def list_package_settings_versions(
    key: str,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    if limit is not None and not 1 <= limit <= MAX_VERSIONS:
        raise ValidationError("invalid_limit", f"limit must be between 1 and {MAX_VERSIONS}")
    items, next_cursor = await queries.list_versions(
        db, ctx, key, limit=clamp_limit(limit), cursor=cursor
    )
    return JSONResponse(page_body(items, next_cursor))
