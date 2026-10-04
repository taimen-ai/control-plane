"""Event journal endpoints: paged reads and a resumable WebSocket stream.

The WebSocket is a wake-up channel, not a source of truth: the server always
reads events from the ``events`` table in ``(tx_id, sequence)`` delivery
order. Clients track the opaque ``cursor`` of the last processed event and
reconnect with ``?after=<cursor>`` to resume; a periodic poll covers lost
NOTIFYs. Legacy v0.3 integer sequences are still accepted as ``after``
values (adapted server-side, see application/event_cursor.py).

Both readers take the same narrowing filters (CP-ADR-0068): ``types`` —
event type prefixes, ``workspaceId`` — the events of a workspace subtree,
``events.read`` checked on that workspace. The page also narrows by
``actorId``, by the period ``occurredFrom`` (inclusive) / ``occurredTo``
(exclusive) and, with ``includeDescendants=false``, to the workspace alone
(CP-ADR-0068 amendment Б1-Б4). A filter never changes the order or the meaning of
a cursor: a filtered reader resumes where it stopped.

The page reads backward too (CP-ADR-0024, amendment 2026-09-29):
``before=<cursor>`` gives the events strictly before it, ``prevCursor`` of a
page is the ``before`` of the preceding one; ``order=desc`` only flips the
items of a page, never which events it holds.

``GET /events:export`` hands out the same filtered journal for a bounded
period as one streamed JSONL or CSV body, for an auditor (CP-ADR-0068,
export amendment): ``events.export`` next to ``events.read``, limits checked and the
export journaled before the first byte.

``GET /event-types`` is the catalog the journal is written by (CP-ADR-0068,
amendment of 2026-10-04): groups, versions and payload schemas of the types,
captions in the language asked. It changes only with a release, so it carries
an ETag and answers ``If-None-Match`` with 304.
"""

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from datetime import datetime
from typing import Literal, cast

from fastapi import APIRouter, Header, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane import observability
from control_plane.api.dependencies import (
    AuthDep,
    DbDep,
    SessionFactoryDep,
    SettingsDep,
    authenticate_websocket,
)
from control_plane.api.etag import none_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    EventOut,
    EventPageOut,
    EventTypeListOut,
    dump,
)
from control_plane.api.v1.views import _locale
from control_plane.application.authorization import AuthContext
from control_plane.application.event_cursor import (
    EventCursor,
    EventPosition,
    decode_cursor,
    encode_position,
)
from control_plane.application.queries import event_export as export_queries
from control_plane.application.queries import event_types as type_queries
from control_plane.application.queries import events as queries
from control_plane.application.queries.events import (
    EventFilter,
    JournalEvent,
    parse_type_prefixes,
)
from control_plane.application.visibility import refresh_visibility
from control_plane.config import Settings
from control_plane.domain.errors import DomainError
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.realtime.hub import RealtimeHub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_WS_BATCH_LIMIT = 200

# The catalog changes only with a release: a client may reuse the body for a
# while and then revalidate it by the ETag; ``private`` — it is behind a right.
_EVENT_TYPES_CACHE_CONTROL = "private, max-age=300"


def _event_body(event: JournalEvent) -> dict[str, object]:
    return dump(EventOut, event, cursor=encode_position(queries.position_of(event)))


@router.get("/events", response_model=EventPageOut, responses=ERROR_RESPONSES)
async def list_events(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    after: int | None = Query(default=None, ge=0),
    before: str | None = Query(default=None),
    order: Literal["asc", "desc"] = Query(default="asc"),
    tail: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None, alias="entityType"),
    entity_id: uuid.UUID | None = Query(default=None, alias="entityId"),
    types: list[str] | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    include_descendants: bool | None = Query(
        default=None,
        alias="includeDescendants",
        description=(
            "With workspaceId: true (or absent) - the workspace and its descendants,"
            " false - the workspace alone. Ignored without workspaceId."
        ),
    ),
    actor_id: uuid.UUID | None = Query(
        default=None,
        alias="actorId",
        description="Only the events whose actor is this principal (the event's actorId).",
    ),
    occurred_from: datetime | None = Query(
        default=None,
        alias="occurredFrom",
        description="ISO 8601 date-time with a zone; occurredAt >= occurredFrom (inclusive).",
    ),
    occurred_to: datetime | None = Query(
        default=None,
        alias="occurredTo",
        description="ISO 8601 date-time with a zone; occurredAt < occurredTo (exclusive).",
    ),
) -> JSONResponse:
    page = await queries.list_events(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        after=after,
        before=before,
        tail=tail,
        entity_type=entity_type,
        entity_id=entity_id,
        types=parse_type_prefixes(types),
        workspace_id=workspace_id,
        include_descendants=include_descendants,
        actor_id=actor_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    observability.inc("event_replay_requests_total")
    observability.inc("event_replay_events_total", len(page.events))
    items = [_event_body(e) for e in page.events]
    if order == "desc":
        items.reverse()
    return JSONResponse(
        {
            "items": items,
            "nextCursor": page.next_cursor,
            "prevCursor": page.prev_cursor,
            "hasMore": page.has_more,
        }
    )


@router.get(
    "/events:export",
    response_class=StreamingResponse,
    responses={
        **ERROR_RESPONSES,
        200: {
            "description": "The events of the period in journal order: JSONL - one event"
            " body of GET /events per line; CSV - a header row, then id, occurredAt, type,"
            " schemaVersion, actorId, entityType, entityId, workspaceId and payload as a"
            " JSON string. X-Event-Count names how many events the body holds.",
            "content": {
                "application/x-ndjson": {"schema": {"type": "string"}},
                "text/csv": {"schema": {"type": "string"}},
            },
        },
    },
    summary="Export the journal of a bounded period for an audit, streamed as JSONL or CSV",
)
async def export_events(
    ctx: AuthDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    export_format: Literal["jsonl", "csv"] = Query(alias="format"),
    entity_type: str | None = Query(default=None, alias="entityType"),
    entity_id: uuid.UUID | None = Query(default=None, alias="entityId"),
    types: list[str] | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    include_descendants: bool | None = Query(
        default=None,
        alias="includeDescendants",
        description="As in GET /events: true (or absent) - the subtree, false - the workspace"
        " alone.",
    ),
    actor_id: uuid.UUID | None = Query(default=None, alias="actorId"),
    occurred_from: datetime | None = Query(
        default=None,
        alias="occurredFrom",
        description="Required; ISO 8601 date-time with a zone, inclusive.",
    ),
    occurred_to: datetime | None = Query(
        default=None,
        alias="occurredTo",
        description="Required; ISO 8601 date-time with a zone, exclusive. The period is at"
        " most events_export_max_period_days long (92 by default).",
    ),
) -> StreamingResponse:
    # The checks and the audit event commit before the first byte leaves.
    async with transaction(session_factory) as session:
        export = await export_queries.prepare_export(
            session,
            ctx,
            export_format=export_format,
            max_period_days=settings.events_export_max_period_days,
            max_events=settings.events_export_max_events,
            entity_type=entity_type,
            entity_id=entity_id,
            types=parse_type_prefixes(types),
            workspace_id=workspace_id,
            include_descendants=include_descendants,
            actor_id=actor_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
    observability.inc("event_export_requests_total")
    name = (
        f"events-{export.occurred_from.strftime('%Y%m%dT%H%M%S')}"
        f"-{export.occurred_to.strftime('%Y%m%dT%H%M%S')}.{export_format}"
    )
    body = export_queries.render(
        export_queries.export_events(session_factory, export), export_format, _event_body
    )
    return StreamingResponse(
        body,
        media_type=export_queries.MEDIA_TYPES[export_format],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Event-Count": str(export.count),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# visibility: tenant — the catalog of event types describes the platform, not workspace data
@router.get(
    "/event-types",
    response_model=EventTypeListOut,
    responses={**ERROR_RESPONSES, 304: {"description": "If-None-Match names the current ETag"}},
    summary="The event catalog: groups, versions and payload schemas of the types, captions"
    " in the language asked",
)
async def list_event_types(
    ctx: AuthDep,
    locale: str | None = Query(
        default=None,
        description="Language of the captions (en, ru, pt-BR): itself, its base language, else"
        " en; the core has English captions only, a console uses labelKey for its own",
    ),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    found = await type_queries.list_event_types(ctx, _locale(locale))
    headers = {"ETag": found.etag, "Cache-Control": _EVENT_TYPES_CACHE_CONTROL}
    if none_match(if_none_match, found.etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(found.body, headers=headers)


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client frames (acks/keepalives) until the client disconnects."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


# authz: public — аутентификация в обработчике (authenticate_websocket) + events.read
@router.websocket("/events/ws")
async def events_ws(
    websocket: WebSocket,
    after: str = Query(default="0"),
    types: list[str] | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
) -> None:
    settings = cast(Settings, websocket.app.state.settings)
    session_factory = cast(async_sessionmaker[AsyncSession], websocket.app.state.session_factory)
    hub = cast(RealtimeHub, websocket.app.state.realtime_hub)

    ctx: AuthContext | None = None
    filters = EventFilter()
    close_code: int | None = None
    start: EventCursor = EventPosition(0, 0)
    try:
        start = decode_cursor(after)
        prefixes = parse_type_prefixes(types)
        ctx = await authenticate_websocket(websocket, settings, session_factory)
        async with session_factory() as db:
            filters = await queries.authorize_event_read(
                db, ctx, workspace_id=workspace_id, types=prefixes
            )
    except DomainError as exc:
        if exc.http_status == 401:
            close_code = 4401
        elif exc.http_status == 403:
            close_code = 4403
        elif exc.http_status == 404:
            close_code = 4404  # the workspace of the filter does not exist
        elif exc.http_status == 503:
            # No decision was reached rather than access refused: retrying is
            # meaningful for the client.
            close_code = 4503
        else:
            close_code = 4400  # malformed/unsupported cursor or filter

    # Close codes only reach the client after the handshake completes, so
    # accept first even on auth failure, then close with 4401/4403.
    await websocket.accept()
    if close_code is not None or ctx is None:
        await websocket.close(code=close_code or 4401)
        return

    reader = asyncio.create_task(_drain_client(websocket))
    waiter: asyncio.Task[None] | None = None
    try:
        while not reader.done():
            # Drain everything new before going back to sleep.
            while True:
                async with session_factory() as db:
                    # The subscription follows a change of the reader's
                    # visibility as the next request would (CP-ADR-0082 §3.3).
                    ctx = await refresh_visibility(db, ctx)
                    filters = dataclasses.replace(
                        filters, visible_workspaces=queries.visible_tuple(ctx)
                    )
                    frontier = (
                        await queries.current_position(db, ctx.tenant_id)
                        if filters.narrows
                        else None
                    )
                    events = await queries.fetch_events_after(
                        db,
                        tenant_id=ctx.tenant_id,
                        start=start,
                        limit=_WS_BATCH_LIMIT,
                        filters=filters,
                    )
                for event in events:
                    await websocket.send_json(_event_body(event))
                    start = queries.position_of(event)
                if len(events) < _WS_BATCH_LIMIT:
                    start = queries.past_filtered_out(start, frontier)
                    break

            waiter = asyncio.create_task(
                hub.wait(ctx.tenant_id, timeout_seconds=settings.ws_poll_interval_seconds)
            )
            done, _ = await asyncio.wait({waiter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                break
    except (WebSocketDisconnect, RuntimeError):
        pass  # client went away mid-send
    except Exception:
        logger.exception("events websocket failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        for task in (reader, waiter):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
