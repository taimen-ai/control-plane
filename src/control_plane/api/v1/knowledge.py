"""Knowledge snapshots, documents and domain packs, proxied to Memory (CP-ADR-0060).

Like ``/context``, every database transaction here closes BEFORE the Memory
Service is called, so a slow memory never holds a pooled connection. Unlike
``/context`` there is no degraded answer: the caller asked for a write into
memory, so a Memory failure is the caller's failure (502).

The company-knowledge amendment of CP-ADR-0060 is implemented: the snapshot
preview and ``expectedState`` (K008), knowledge base documents (K009) and tenant
packs (K010). ``POST /knowledge/entities:query`` (K031) reads the entities of
a workspace's knowledge, with the caller's visibility, and with
``include.relations`` their relations (amendment 2026-10-03); ``GET
/workspaces/{id}/knowledge-packs`` and ``GET /knowledge/packs/{ref}`` read back
what a pack install writes (amendment 2026-09-30).
"""

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ErrorEnvelope,
    KnowledgeDocumentRequest,
    KnowledgeEntitiesInclude,
    KnowledgeEntitiesPageOut,
    KnowledgeEntitiesQueryRequest,
    KnowledgePackOut,
    KnowledgePackRegisterRequest,
    KnowledgeSnapshotPreviewOut,
    KnowledgeSnapshotPreviewRequest,
    KnowledgeSnapshotRequest,
    WorkspaceKnowledgePacksOut,
    WorkspaceKnowledgePacksRequest,
)
from control_plane.application.commands import knowledge as commands
from control_plane.application.queries import knowledge_entities, recall
from control_plane.infrastructure.context_provider import KnowledgeProvider
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["knowledge"])

_MEMORY_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    502: {"model": ErrorEnvelope, "description": "Memory service failed (memory_unavailable)"},
    503: {"model": ErrorEnvelope, "description": "Memory provider not configured"},
}
_SNAPSHOT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_MEMORY_RESPONSES,
    409: {
        "model": ErrorEnvelope,
        "description": "The source's state moved on since the plan, or the snapshot is older "
        "than the applied one (snapshot_stale)",
    },
}


def _provider(request: Request) -> KnowledgeProvider:
    return commands.require_provider(
        cast(KnowledgeProvider | None, getattr(request.app.state, "context_provider", None))
    )


def _trace(request: Request) -> str | None:
    return getattr(request.state, "trace_run_id", "") or None


def _relations(
    include: KnowledgeEntitiesInclude | None,
) -> knowledge_entities.RelationsInclude | None:
    if include is None:
        return None
    names = None if include.relations == "*" else tuple(dict.fromkeys(include.relations))
    return knowledge_entities.RelationsInclude(
        names=names, direction=include.direction, limit=include.limit
    )


@router.post("/knowledge/snapshots", responses=_SNAPSHOT_RESPONSES)
async def submit_snapshot(
    payload: KnowledgeSnapshotRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async with transaction(session_factory) as db:
        target = await commands.prepare_snapshot(
            db, ctx, settings, workspace_id=payload.workspace_id
        )
    provider = _provider(request)
    snapshot = payload.snapshot_document()
    answer = await commands.reconcile_snapshot(
        provider,
        target,
        snapshot,
        expected_state=payload.expected_state,
        trace_run_id=_trace(request),
    )
    async with transaction(session_factory) as db:
        await commands.record_snapshot_reconciled(db, ctx, target, snapshot, answer)
    return JSONResponse(answer)


@router.post(
    "/knowledge/snapshots:preview",
    response_model=KnowledgeSnapshotPreviewOut,
    responses=_SNAPSHOT_RESPONSES,
)
async def preview_snapshot(
    payload: KnowledgeSnapshotPreviewRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """What applying the snapshot would change, and the state it was computed
    on (``stateToken``). Nothing is written, no event either."""
    async with transaction(session_factory) as db:
        target = await commands.prepare_snapshot(
            db, ctx, settings, workspace_id=payload.workspace_id
        )
    answer = await commands.preview_snapshot(
        _provider(request), target, payload.snapshot_document(), trace_run_id=_trace(request)
    )
    return JSONResponse(answer)


@router.post("/knowledge/documents", responses=_MEMORY_RESPONSES)
async def submit_document(
    payload: KnowledgeDocumentRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """A knowledge base document outside any case: text the caller already
    cut into chunks, stored where a snapshot of the workspace would land."""
    async with transaction(session_factory) as db:
        target = await commands.prepare_snapshot(
            db, ctx, settings, workspace_id=payload.workspace_id
        )
    document = payload.memory_document()
    answer = await commands.store_document(
        _provider(request), target, document, trace_run_id=_trace(request)
    )
    async with transaction(session_factory) as db:
        await commands.record_document_stored(db, ctx, target, document)
    return JSONResponse(answer)


@router.post(
    "/knowledge/entities:query",
    response_model=KnowledgeEntitiesPageOut,
    responses={
        **_MEMORY_RESPONSES,
        503: {
            "model": ErrorEnvelope,
            "description": "Memory provider not configured or not answering in time",
        },
    },
)
async def query_entities(
    payload: KnowledgeEntitiesQueryRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """The entities of ``kinds`` valid at ``asOf`` that satisfy ``where``, a
    page at a time, from the namespace of the workspace tree root with the
    caller's visibility (the right to read the workspace's context);
    ``include.relations`` adds each entity's relations with a visible end."""
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    async with transaction(session_factory) as db:
        call = await knowledge_entities.prepare_entities_query(
            db,
            ctx,
            settings,
            workspace_id=payload.workspace_id,
            kinds=payload.kinds,
            where=[c.to_memory() for c in payload.where],
            as_of=payload.as_of,
            limit=payload.limit,
            cursor=payload.cursor,
            relations=_relations(payload.include),
        )
    page = await knowledge_entities.fetch_entities(
        call, provider, settings, trace_run_id=_trace(request) or ""
    )
    return JSONResponse(page)


# visibility: tenant — a knowledge pack is an object of the tenant, used per workspace
@router.post("/knowledge/packs", responses=_MEMORY_RESPONSES)
async def register_pack(
    body: KnowledgePackRegisterRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """A shared pack (platform administrators) or, with ``scope: tenant``, a
    pack of the caller's tenant (``knowledge.packs.manage``)."""
    payload = await commands.prepare_pack(ctx, settings, body.model_dump(exclude_unset=True))
    answer = await commands.register_pack(_provider(request), payload, trace_run_id=_trace(request))
    async with transaction(session_factory) as db:
        await commands.record_pack_registered(db, ctx, payload, answer)
    return JSONResponse(answer)


# visibility: tenant — a knowledge pack is an object of the tenant, used per workspace
@router.get(
    "/knowledge/packs/{ref}",
    response_model=KnowledgePackOut,
    responses={**_MEMORY_RESPONSES, 404: {"model": ErrorEnvelope, "description": "No such pack"}},
)
async def get_pack(
    ref: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
) -> JSONResponse:
    """A registered pack version: ``name@version``, ``name`` for the latest,
    ``tenant:name[@version]`` for a pack of the caller's tenant."""
    read = await commands.prepare_pack_read(ctx, settings, ref)
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    return JSONResponse(await commands.read_pack(provider, read, trace_run_id=_trace(request)))


@router.get(
    "/workspaces/{workspace_id}/knowledge-packs",
    response_model=WorkspaceKnowledgePacksOut,
    responses=_MEMORY_RESPONSES,
)
async def get_workspace_packs(
    workspace_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """The packs enabled for the workspace's tree and its strictness."""
    async with transaction(session_factory) as db:
        target = await commands.prepare_workspace_packs_read(
            db, ctx, settings, workspace_id=workspace_id
        )
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    answer = await commands.read_workspace_packs(provider, target, trace_run_id=_trace(request))
    return JSONResponse(answer)


@router.put("/workspaces/{workspace_id}/knowledge-packs", responses=_MEMORY_RESPONSES)
async def set_workspace_packs(
    workspace_id: uuid.UUID,
    payload: WorkspaceKnowledgePacksRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async with transaction(session_factory) as db:
        target = await commands.prepare_workspace_packs(
            db, ctx, settings, workspace_id=workspace_id
        )
    packs = commands.require_pinned_packs(payload.packs)
    answer = await commands.set_workspace_packs(
        _provider(request),
        target,
        packs=packs,
        strict=payload.strict,
        trace_run_id=_trace(request),
    )
    async with transaction(session_factory) as db:
        await commands.record_workspace_packs_set(
            db, ctx, target, packs=packs, strict=payload.strict
        )
    return JSONResponse(answer)
