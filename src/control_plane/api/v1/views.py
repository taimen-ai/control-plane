"""Screens of packages by description: ``GET /views``, ``GET /views/{view_key}`` and
``POST /views/{view_key}:query`` (CP-ADR-0080).

A view is installed by ``POST /packages:apply`` (TAI-ADR-0066); here it is
read, its strings in the language of ``?locale=`` (or the package's
``defaultLocale``). Who sees a view — the roles of its audience and the right
to read its source — is :mod:`control_plane.application.queries.views`; the
data one of its blocks draws, :mod:`control_plane.application.queries.view_data`.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    ErrorEnvelope,
    ViewOut,
    ViewQueryOut,
    ViewQueryRequest,
    ViewSummaryPageOut,
    page_body,
)
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries import recall, view_data
from control_plane.application.queries import views as queries
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import attach_package, attach_packages
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["views"])

CATALOG_KIND = "View"
LOCALE_DESCRIPTION = (
    "Language of the strings (en, ru, pt-BR); a language the package lacks — its defaultLocale"
)


def _locale(locale: str | None) -> str | None:
    if locale is not None and not 1 <= len(locale) <= 35:
        raise ValidationError("invalid_locale", "locale is a language tag such as en or pt-BR")
    return locale


@router.get(
    "/views",
    response_model=ViewSummaryPageOut,
    responses=ERROR_RESPONSES,
    summary="The views of packages the caller sees: a role of their audience and the right"
    " to read their source",
)
async def list_views(
    ctx: AuthDep,
    db: DbDep,
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    wanted = _locale(locale)
    after_key: str | None = None
    if cursor is not None:
        after_key = decode_cursor(cursor).get("k")
        if not isinstance(after_key, str):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
    found, next_key = await queries.list_views(
        db, ctx, limit=clamp_limit(limit), after_key=after_key, package=package
    )
    items = [read.out(wanted, layout=False) for read in found]
    await attach_packages(db, ctx.tenant_id, CATALOG_KIND, items)
    next_cursor = encode_cursor({"k": next_key}) if next_key is not None else None
    return JSONResponse(page_body(items, next_cursor))


@router.get(
    "/views/{view_key}",
    response_model=ViewOut,
    responses=ERROR_RESPONSES,
    summary="A view by key, its strings in the language asked; one the caller may not see is 404",
)
async def get_view(
    view_key: str,
    ctx: AuthDep,
    db: DbDep,
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
) -> JSONResponse:
    wanted = _locale(locale)
    body = (await queries.get_view(db, ctx, view_key)).out(wanted)
    return JSONResponse(await attach_package(db, ctx.tenant_id, CATALOG_KIND, body))


_QUERY_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    409: {
        "model": ErrorEnvelope,
        "description": "The view no longer compiles against its source (view_stale), or costs"
        " more records than a block may (view_too_costly)",
    },
    503: {
        "model": ErrorEnvelope,
        "description": "Memory, which a related block and a view of knowledge read, is down",
    },
}


@router.post(
    "/views/{view_key}:query",
    response_model=ViewQueryOut,
    responses=_QUERY_RESPONSES,
    summary="The data one block of a view draws, as the caller may see it; a view or an"
    " instance the caller may not see is 404",
)
async def query_view(
    view_key: str,
    payload: ViewQueryRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    locale: str | None = Query(default=None, description=LOCALE_DESCRIPTION),
) -> JSONResponse:
    """By the block (``table``, ``list``, ``board``, ``metrics``, ``chart``, ``header``,
    ``fields``, ``steps``, ``timeline``, ``artifacts``, ``related``) — its form of
    CP-ADR-0080 amendment A; ``filter`` and ``sort`` name only what the block declares
    (else 422). A view of a process, of the tasks of a type or of the knowledge base of
    the caller's tree (amendment Б)."""
    wanted = _locale(locale)
    query = view_data.ViewQuery(
        block=payload.block,
        params=payload.params,
        filter=[f.model_dump() for f in payload.filter or ()],
        sort=[s.model_dump() for s in payload.sort or ()],
        limit=payload.limit,
        cursor=payload.cursor,
        workspace_id=payload.workspace_id,
    )
    async with transaction(session_factory) as db:
        answer = await view_data.prepare(db, ctx, settings, view_key, query, locale=wanted)
    if answer.related is None and answer.memory is None:
        return JSONResponse(answer.body)
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    trace = getattr(request.state, "trace_run_id", "") or ""
    if answer.memory is not None:
        return JSONResponse(await answer.memory(provider, settings, trace))
    assert answer.related is not None
    return JSONResponse(
        await view_data.fetch_related(answer.related, provider, settings, trace_run_id=trace)
    )
