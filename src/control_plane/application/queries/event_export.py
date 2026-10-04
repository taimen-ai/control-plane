"""Export of the journal for a period, for an auditor (CP-ADR-0068, export amendment).

``GET /events:export`` takes the filters of ``GET /events`` and hands out every
matching event of a bounded period as one JSONL or CSV body. Three rules shape
it:

* **Bounded before the first byte.** The period is required and at most
  ``events_export_max_period_days`` long, the number of matching events at most
  ``events_export_max_events``. Both are checked before the response starts: a
  status cannot change once a streamed body is under way, so an export that is
  too large is refused whole, never cut short.
* **A snapshot of the journal.** The upper bound is the journal frontier taken
  when the export is prepared (:func:`current_position`): every event at or
  below it is final, so the count and the body agree, and events written while
  the body streams are not in it.
* **Streamed page by page.** Events are read in journal order in pages of
  :data:`EXPORT_PAGE_SIZE`, each in a session of its own, through the same
  archive-aware reader as ``GET /events``: memory holds one page, and no
  transaction stays open for as long as a slow client downloads.

The right is ``events.export`` next to ``events.read`` (CP-ADR-0068 п.2): read
says what the caller may see, export that they may take it away in bulk. The
export is journaled as ``event_journal.exported`` — the filters and the count,
never the events — and committed before the body starts.
"""

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, func, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.event_cursor import EventPosition, encode_position
from control_plane.application.events import record_event
from control_plane.application.queries.events import (
    EventFilter,
    JournalEvent,
    authorize_event_read,
    current_position,
    fetch_events_after,
    journal_floors,
    position_of,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import Event, EventArchive

ExportFormat = Literal["jsonl", "csv"]

EXPORT_PAGE_SIZE = 500

# CSV columns: the envelope flattened, payload last as a JSON string.
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("occurredAt", "occurredAt"),
    ("type", "type"),
    ("schemaVersion", "schemaVersion"),
    ("actorId", "actorId"),
    ("entityType", "entityType"),
    ("entityId", "entityId"),
    ("workspaceId", "workspaceId"),
    ("payload", "payload"),
)

MEDIA_TYPES: dict[str, str] = {
    "jsonl": "application/x-ndjson; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}


@dataclass(frozen=True)
class EventExport:
    """A prepared export: what to read, between which positions, how many."""

    tenant_id: uuid.UUID
    filters: EventFilter
    start: EventPosition
    until: EventPosition
    count: int
    occurred_from: datetime
    occurred_to: datetime


def check_export_period(
    occurred_from: datetime | None, occurred_to: datetime | None, max_days: int
) -> tuple[datetime, datetime]:
    """Both bounds are given and the period is at most ``max_days`` long."""
    if occurred_from is None or occurred_to is None:
        missing = [
            name
            for name, value in (("occurredFrom", occurred_from), ("occurredTo", occurred_to))
            if value is None
        ]
        raise ValidationError(
            "export_period_required",
            "An export is for a period: give both 'occurredFrom' and 'occurredTo'",
            details={"missing": missing, "maxPeriodDays": max_days},
        )
    # Zones and the direction are checked by check_event_period, which runs
    # first; only the length is left.
    if occurred_to - occurred_from > timedelta(days=max_days):
        raise ValidationError(
            "export_period_too_long",
            f"An export covers at most {max_days} days; split the period",
            details={
                "occurredFrom": occurred_from.isoformat(),
                "occurredTo": occurred_to.isoformat(),
                "maxPeriodDays": max_days,
            },
        )
    return occurred_from, occurred_to


def _counted(
    model: type[Event] | type[EventArchive],
    *,
    tenant_id: uuid.UUID,
    until: EventPosition,
    filters: EventFilter,
    cap: int,
) -> Select[Any]:
    stmt: Select[Any] = select(model).where(
        model.tenant_id == tenant_id,
        tuple_(model.tx_id, model.sequence) <= (until.tx_id, until.sequence),
    )
    return filters.apply(stmt, model).with_only_columns(model.id).limit(cap)


async def count_events_until(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    until: EventPosition,
    filters: EventFilter,
    cap: int,
) -> int:
    """Matching events at or below ``until`` in both tables, counted up to ``cap``.

    One statement reads both tables in one snapshot, so a concurrent archive
    run cannot count a moved event twice or not at all. Each side stops at
    ``cap`` rows: the answer is exact up to ``cap`` and "more" above it.
    """
    both = union_all(
        _counted(Event, tenant_id=tenant_id, until=until, filters=filters, cap=cap),
        _counted(EventArchive, tenant_id=tenant_id, until=until, filters=filters, cap=cap),
    ).subquery()
    total = await session.scalar(select(func.count()).select_from(both))
    return int(total or 0)


async def prepare_export(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    export_format: ExportFormat,
    max_period_days: int,
    max_events: int,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    types: tuple[str, ...] = (),
    workspace_id: uuid.UUID | None = None,
    include_descendants: bool | None = None,
    actor_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> EventExport:
    """Check the right and the limits, fix the snapshot and journal the export.

    Runs in the caller's write transaction: the ``event_journal.exported``
    event commits with it, before the body is streamed.
    """
    resource = ResourceRef("workspace", str(workspace_id)) if workspace_id is not None else None
    await authorize(ctx, Permission.EVENTS_EXPORT, resource=resource)
    filters = await authorize_event_read(
        session,
        ctx,
        workspace_id=workspace_id,
        entity_type=entity_type,
        entity_id=entity_id,
        types=types,
        include_descendants=include_descendants,
        actor_id=actor_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    period_from, period_to = check_export_period(occurred_from, occurred_to, max_period_days)

    _journal_floor, archive_floor = await journal_floors(session, ctx.tenant_id)
    until = await current_position(session, ctx.tenant_id)
    count = await count_events_until(
        session, tenant_id=ctx.tenant_id, until=until, filters=filters, cap=max_events + 1
    )
    if count > max_events:
        raise ValidationError(
            "export_too_large",
            f"The export would hold more than {max_events} events; narrow the period or the"
            " filters",
            details={"maxEvents": max_events},
        )

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="event_journal.exported",
        entity_type="event_journal",
        entity_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "format": export_format,
            "types": list(types),
            "entityType": entity_type,
            "entityId": str(entity_id) if entity_id is not None else None,
            "actorId": str(actor_id) if actor_id is not None else None,
            "occurredFrom": period_from.isoformat(),
            "occurredTo": period_to.isoformat(),
            "workspaceId": str(workspace_id) if workspace_id is not None else None,
            "includeDescendants": include_descendants,
            "events": count,
            "throughCursor": encode_position(until),
        },
    )
    return EventExport(
        tenant_id=ctx.tenant_id,
        filters=filters,
        start=archive_floor,
        until=until,
        count=count,
        occurred_from=period_from,
        occurred_to=period_to,
    )


async def export_events(
    session_factory: async_sessionmaker[AsyncSession],
    export: EventExport,
    *,
    page_size: int | None = None,
) -> AsyncIterator[JournalEvent]:
    """The events of a prepared export in journal order, one page in memory.

    Stops at the snapshot bound and after ``export.count`` events: retention
    may prune what was counted, and an entity moved to another workspace may
    hide or reveal events, so the body can be shorter but never longer.
    """
    page_size = page_size or EXPORT_PAGE_SIZE
    position = export.start
    bound = (export.until.tx_id, export.until.sequence)
    delivered = 0
    while delivered < export.count:
        async with session_factory() as session:
            page = await fetch_events_after(
                session,
                tenant_id=export.tenant_id,
                start=position,
                limit=page_size,
                filters=export.filters,
            )
        for event in page:
            if (event.tx_id, event.sequence) > bound:
                return
            yield event
            delivered += 1
            if delivered >= export.count:
                return
        if len(page) < page_size:
            return
        position = position_of(page[-1])


def jsonl_line(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"


def _csv_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    # RFC 4180: CRLF line ends, fields with separators or quotes quoted.
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue()


def csv_header() -> str:
    return _csv_text([[column for column, _ in CSV_COLUMNS]])


# OWASP CSV injection: a cell starting with one of these is read by a
# spreadsheet as a formula, so it gets a leading apostrophe.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(text: str) -> str:
    return "'" + text if text.startswith(CSV_FORMULA_PREFIXES) else text


def csv_line(body: dict[str, Any]) -> str:
    row: list[str] = []
    for _, key in CSV_COLUMNS:
        value = body.get(key)
        if key == "payload":
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = "" if value is None else str(value)
        row.append(csv_cell(text))
    return _csv_text([row])


async def render(
    events: AsyncIterator[JournalEvent],
    export_format: ExportFormat,
    body_of: Callable[[JournalEvent], dict[str, Any]],
) -> AsyncIterator[str]:
    """The body of an export, a page of lines at a time."""
    line = jsonl_line if export_format == "jsonl" else csv_line
    if export_format == "csv":
        yield csv_header()
    chunk: list[str] = []
    async for event in events:
        chunk.append(line(body_of(event)))
        if len(chunk) >= EXPORT_PAGE_SIZE:
            yield "".join(chunk)
            chunk = []
    if chunk:
        yield "".join(chunk)
