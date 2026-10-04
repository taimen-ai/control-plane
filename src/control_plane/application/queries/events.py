"""Event journal reads: complete-prefix replay in ``(tx_id, sequence)`` order.

Delivery contract (v0.4): events are handed out ordered by
``(tx_id, sequence)`` and only below the *stable horizon*
``tx_id < pg_snapshot_xmin(pg_current_snapshot())``. Any still-pending
transaction has ``tx_id >= xmin``, so a cursor that advances along delivered
positions can never step over an event that commits later — committed events
cannot be permanently skipped (see application/event_cursor.py for the full
argument; regression: ``test_event_prefix_is_complete_under_xid_inversion``).

``sequence`` stays as the event identifier / audit field / intra-transaction
order; it is NOT the replay cursor. Legacy integer cursors (v0.3
``after=<sequence>``) are accepted as a :class:`LegacyFloor`: one stretch of
"sequence > floor" filtering in the new order, upgraded to a real position by
the first delivered event. The switch may re-deliver events the old client
already saw (at-least-once). The full no-gap guarantee applies from the
first delivered event ONWARD: events the v0.3 defect had already skipped
for that client sit BELOW its floor and stay out of reach of the legacy
cursor — recovering them needs an explicit full replay (no cursor / a
cursor from the origin), which is the documented one-time migration advice.

The journal page for `/events` always returns a ``nextCursor`` (echoing the
input cursor when nothing new is visible) plus ``hasMore``, so followers can
poll without interpreting cursor internals.

Reading backward (``before=<cursor>``, and ``tail`` which is a backward read
from the horizon) walks the same ``(tx_id, sequence)`` order in reverse, hot
journal first, then the archive, and hands out ``prevCursor`` — the oldest
event of the page — as the ``before`` of the preceding page (CP-ADR-0024,
amendment 2026-09-29). Everything strictly below a delivered position is
already final (point 2 of event_cursor.py), so a backward walk can neither
skip nor repeat an event.
"""

import dataclasses
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Select,
    Text,
    and_,
    cast,
    exists,
    func,
    or_,
    select,
    text,
    tuple_,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_subtree_ids,
)
from control_plane.application.event_cursor import (
    ORIGIN,
    EventCursor,
    EventPosition,
    LegacyFloor,
    decode_cursor,
    encode_cursor,
    encode_position,
)
from control_plane.application.queries.lists import clamp_limit
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    Event,
    EventArchive,
    Run,
    SkillInvocation,
    Task,
)

# A replay page may span the cold archive and the hot journal. Both carry the
# same column names, so response serialization is identical (ADR-0038).
JournalEvent = Event | EventArchive


def _stable_horizon() -> ColumnElement[int]:
    """Oldest still-running transaction id: events above it are not final yet."""
    return cast(cast(func.pg_snapshot_xmin(func.pg_current_snapshot()), Text), BigInteger)


def position_of(event: JournalEvent) -> EventPosition:
    return EventPosition(tx_id=event.tx_id, sequence=event.sequence)


# A type prefix: dotted lowercase words, e.g. ``approval.`` or ``task.verified``.
_TYPE_PREFIX = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]*)*$")
MAX_TYPE_PREFIXES = 20

_Row = TypeVar("_Row", Event, EventArchive)


@dataclass(frozen=True)
class EventFilter:
    """What a reader narrows the journal to; empty = everything.

    ``types`` are prefixes of the event type (``approval.`` covers every
    approval event, ``task.verified`` that type); ``workspace_ids`` is the
    subtree a ``workspaceId`` filter resolved to (CP-ADR-0068), or the
    workspace alone with ``includeDescendants=false``. ``actor_id`` is the
    principal who acted, ``occurred_from``/``occurred_to`` a half-open period
    of ``occurred_at`` (CP-ADR-0068 amendment Б1-Б4). Filters narrow the ordered
    replay scan, they never change the order or the cursor.
    """

    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    types: tuple[str, ...] = ()
    workspace_ids: tuple[uuid.UUID, ...] | None = None
    actor_id: uuid.UUID | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    # The caller's visible workspaces in ``members`` mode (CP-ADR-0082 §4):
    # events of other workspaces are not read, those of the tenant are.
    visible_workspaces: tuple[uuid.UUID, ...] | None = None

    @property
    def narrows(self) -> bool:
        return self != EventFilter()

    def apply(self, stmt: Select[tuple[_Row]], model: type[_Row]) -> Select[tuple[_Row]]:
        if self.entity_type is not None:
            stmt = stmt.where(model.entity_type == self.entity_type)
        if self.entity_id is not None:
            stmt = stmt.where(model.entity_id == self.entity_id)
        if self.types:
            stmt = stmt.where(
                or_(*(model.event_type.startswith(p, autoescape=True) for p in self.types))
            )
        if self.workspace_ids is not None:
            if len(self.workspace_ids) == 1:
                # An equality keeps the (tenant, workspace, tx_id, sequence)
                # index usable as an ordered scan.
                stmt = stmt.where(model.workspace_id == self.workspace_ids[0])
            else:
                stmt = stmt.where(model.workspace_id.in_(self.workspace_ids))
        if self.actor_id is not None:
            stmt = stmt.where(model.actor_id == self.actor_id)
        if self.occurred_from is not None:
            stmt = stmt.where(model.occurred_at >= self.occurred_from)
        if self.occurred_to is not None:
            stmt = stmt.where(model.occurred_at < self.occurred_to)
        if self.visible_workspaces is not None:
            stmt = stmt.where(
                or_(
                    model.workspace_id.in_(self.visible_workspaces),
                    and_(
                        model.workspace_id.is_(None),
                        ~_about_work(model),
                        _names_visible(model, self.visible_workspaces),
                    ),
                )
            )
        return stmt


def _about_work(model: type[Event] | type[EventArchive]) -> ColumnElement[bool]:
    """An event without a workspace that is about work all the same: work
    without a workspace is not visible in ``members`` mode (CP-ADR-0082 B2),
    and neither is what hangs off it."""
    hanging = [
        (model.entity_type == name)
        & exists().where(table.id == model.entity_id, table.task_id.is_not(None))
        for name, table in (
            ("approval", Approval),
            ("artifact", Artifact),
            ("skill_invocation", SkillInvocation),
        )
    ]
    return or_(model.entity_type.in_(("task", "run", "claim")), *hanging)


def _names_visible(
    model: type[Event] | type[EventArchive], visible: tuple[uuid.UUID, ...]
) -> ColumnElement[bool]:
    """An event without a workspace names only visible work and workspaces.

    Such an event is not about an entity with a workspace (an observation,
    attention feedback, an event of a package trial), but its payload may
    still point at work: ``taskId``, ``runId``, ``workspaceId``, or
    ``entityType``/``entityId`` of attention feedback. Each one present must
    be visible (CP-ADR-0082 V7). Compared as text: a payload is JSON, and a
    malformed id must hide the event, not fail the read.
    """
    tasks = select(Task.id).where(Task.workspace_id.in_(visible))
    task_ids = select(cast(Task.id, Text)).where(Task.workspace_id.in_(visible))
    run_ids = select(cast(Run.id, Text)).where(Run.task_id.in_(tasks))
    approval_ids = select(cast(Approval.id, Text)).where(
        or_(Approval.workspace_id.is_(None), Approval.workspace_id.in_(visible)),
        or_(Approval.task_id.is_(None), Approval.task_id.in_(tasks)),
    )
    workspace_ids = [str(w) for w in visible]

    def absent_or(key: str, allowed: Any) -> ColumnElement[bool]:
        # A missing key and a JSON null both read as SQL NULL.
        ref = model.payload[key].astext
        return or_(ref.is_(None), ref.in_(allowed))

    entity_type = model.payload["entityType"].astext
    entity_ref = model.payload["entityId"].astext
    return and_(
        absent_or("taskId", task_ids),
        absent_or("runId", run_ids),
        absent_or("workspaceId", workspace_ids),
        or_(
            entity_type.is_(None),
            entity_type.not_in(("task", "approval")),
            and_(entity_type == "task", entity_ref.in_(task_ids)),
            and_(entity_type == "approval", entity_ref.in_(approval_ids)),
        ),
    )


def parse_type_prefixes(values: list[str] | None) -> tuple[str, ...]:
    """``types`` query values (repeated and/or comma-separated) -> prefixes."""
    prefixes: list[str] = []
    for value in values or ():
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if not _TYPE_PREFIX.match(item):
                raise ValidationError(
                    "invalid_event_type_filter",
                    "An event type prefix is dotted lowercase words, e.g. 'approval.'",
                    details={"types": item},
                )
            if item not in prefixes:
                prefixes.append(item)
    if len(prefixes) > MAX_TYPE_PREFIXES:
        raise ValidationError(
            "invalid_event_type_filter",
            f"At most {MAX_TYPE_PREFIXES} event type prefixes per request",
            details={"count": len(prefixes)},
        )
    return tuple(prefixes)


def check_event_period(occurred_from: datetime | None, occurred_to: datetime | None) -> None:
    """``occurredFrom``/``occurredTo`` carry a zone and do not run backwards.

    A naive value would be read in the session's timezone — an audit period
    must not depend on server settings, so it is refused, not guessed.
    """
    for name, value in (("occurredFrom", occurred_from), ("occurredTo", occurred_to)):
        if value is not None and value.utcoffset() is None:
            raise ValidationError(
                "invalid_event_period",
                f"'{name}' is an ISO 8601 date-time with a zone, e.g. 2026-07-01T00:00:00Z",
                details={name: value.isoformat()},
            )
    if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:
        raise ValidationError(
            "invalid_event_period",
            "'occurredFrom' must not be after 'occurredTo'",
            details={
                "occurredFrom": occurred_from.isoformat(),
                "occurredTo": occurred_to.isoformat(),
            },
        )


def past_filtered_out(position: EventCursor, frontier: EventPosition | None) -> EventCursor:
    """Move a filtered reader's cursor over the events its filter skipped.

    ``frontier`` is :func:`current_position` taken BEFORE the page query: every
    event at or below it was already stable, so the (later) query has seen it
    and found nothing matching. Without this a reader of a rare type would
    rescan the same unmatched tail on every poll. A legacy floor is left alone.
    """
    if frontier is None or not isinstance(position, EventPosition):
        return position
    if (frontier.tx_id, frontier.sequence) > (position.tx_id, position.sequence):
        return frontier
    return position


async def authorize_event_read(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    types: tuple[str, ...] = (),
    include_descendants: bool | None = None,
    actor_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> EventFilter:
    """Check ``events.read`` for the requested scope and build the filter.

    Without ``workspace_id`` the question is asked at tenant level, as before
    CP-ADR-0068. With it — on that workspace (a policy decision point honours
    grants on its ancestors), and the reader sees the events of the workspace
    and its descendants only, subtree evaluated at read time;
    ``include_descendants=False`` narrows that to the workspace itself.
    Absent, it means the subtree, as before the parameter existed.
    """
    check_event_period(occurred_from, occurred_to)
    narrowing = EventFilter(
        entity_type=entity_type,
        entity_id=entity_id,
        types=types,
        actor_id=actor_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        visible_workspaces=visible_tuple(ctx),
    )
    if workspace_id is None:
        await authorize(ctx, Permission.EVENTS_READ)
        return narrowing
    await authorize(
        ctx, Permission.EVENTS_READ, resource=ResourceRef("workspace", str(workspace_id))
    )
    await get_tenant_workspace(session, ctx, workspace_id)
    if include_descendants is False:
        scope = [workspace_id]
    else:
        scope = await workspace_subtree_ids(session, ctx.tenant_id, workspace_id)
    return dataclasses.replace(narrowing, workspace_ids=tuple(scope))


async def recorded_observations(
    session: AsyncSession, ctx: AuthContext, observation_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Those of ``observation_ids`` recorded in this tenant and visible to the caller.

    An observation IS its journal event, which retention may have moved to
    the archive (ADR-0038) — both tables are the journal. It is visible when
    the journal would hand its event to the caller (CP-ADR-0082 V7): a
    reference to an invisible observation answers as one to a missing one.
    """
    if not observation_ids:
        return set()
    filters = EventFilter(visible_workspaces=visible_tuple(ctx))

    def recorded(table: type[_Row]) -> Select[tuple[uuid.UUID]]:
        stmt = select(table).where(
            table.tenant_id == ctx.tenant_id,
            table.entity_type == "observation",
            table.event_type == "observation.recorded",
            table.entity_id.in_(observation_ids),
        )
        return filters.apply(stmt, table).with_only_columns(table.entity_id)

    rows = await session.scalars(union(recorded(Event), recorded(EventArchive)))
    return set(rows.all())


def visible_tuple(ctx: AuthContext) -> tuple[uuid.UUID, ...] | None:
    """The filter value of the caller's visibility; ``None`` in ``tenant`` mode."""
    if ctx.visible_workspaces is None:
        return None
    return tuple(uuid.UUID(w) for w in sorted(ctx.visible_workspaces))


async def journal_floors(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[EventPosition, EventPosition]:
    """(journal floor, archive floor) for this tenant — what lives where.

    Deliberately lock-free: taking a row lock here would give every ordinary
    ``/events`` read a transaction id and put it in the way of the DDL/DML an
    archive run performs. The archive/hot boundary is instead made safe by
    re-reading the floor after the page is assembled (see
    :func:`fetch_events_after`) and retrying once if it moved.
    """
    row = (
        await session.execute(
            text(
                "SELECT journal_tx_id, journal_sequence, archive_tx_id, archive_sequence"
                " FROM event_journal_floor WHERE tenant_id = :tenant"
            ),
            {"tenant": tenant_id},
        )
    ).first()
    if row is None:
        return EventPosition(0, 0), EventPosition(0, 0)
    return EventPosition(row[0], row[1]), EventPosition(row[2], row[3])


def _archive_stmt(
    *,
    tenant_id: uuid.UUID,
    start: EventCursor,
    until: EventPosition,
    limit: int,
    filters: EventFilter,
) -> Select[tuple[EventArchive]]:
    stmt = (
        select(EventArchive)
        .where(
            EventArchive.tenant_id == tenant_id,
            tuple_(EventArchive.tx_id, EventArchive.sequence) <= (until.tx_id, until.sequence),
        )
        .order_by(EventArchive.tx_id.asc(), EventArchive.sequence.asc())
        .limit(limit)
    )
    if isinstance(start, EventPosition):
        stmt = stmt.where(
            tuple_(EventArchive.tx_id, EventArchive.sequence) > (start.tx_id, start.sequence)
        )
    else:
        stmt = stmt.where(EventArchive.sequence > start.floor)
    return filters.apply(stmt, EventArchive)


def _replay_stmt(
    *,
    tenant_id: uuid.UUID,
    start: EventCursor,
    limit: int,
    filters: EventFilter,
) -> Select[tuple[Event]]:
    stmt = (
        select(Event)
        .where(Event.tenant_id == tenant_id, Event.tx_id < _stable_horizon())
        .order_by(Event.tx_id.asc(), Event.sequence.asc())
        .limit(limit)
    )
    if isinstance(start, EventPosition):
        stmt = stmt.where(tuple_(Event.tx_id, Event.sequence) > (start.tx_id, start.sequence))
    else:
        stmt = stmt.where(Event.sequence > start.floor)
    return filters.apply(stmt, Event)


async def fetch_events_after(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start: EventCursor,
    limit: int,
    filters: EventFilter | None = None,
) -> list[JournalEvent]:
    """Archive-aware page, safe against a concurrent archive run.

    An archive moves rows from ``events`` to ``event_archive``; a row never
    moves the other way. So the only way to miss one is for it to move
    between the archive read and the hot read — detected by re-reading the
    floor afterwards and redoing the page once.
    """
    filters = filters or EventFilter()
    page = await _fetch_page(
        session, tenant_id=tenant_id, start=start, limit=limit, filters=filters
    )
    journal_floor, _archive_floor = await journal_floors(session, tenant_id)
    if page.floor_moved(journal_floor):
        page = await _fetch_page(
            session, tenant_id=tenant_id, start=start, limit=limit, filters=filters
        )
    return page.events


@dataclass(frozen=True)
class _Page:
    events: list[JournalEvent]
    journal_floor: EventPosition

    def floor_moved(self, current: EventPosition) -> bool:
        return (current.tx_id, current.sequence) != (
            self.journal_floor.tx_id,
            self.journal_floor.sequence,
        )


async def _fetch_page(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start: EventCursor,
    limit: int,
    filters: EventFilter,
) -> _Page:
    """One archive-then-hot page plus the floor it was assembled under."""
    journal_floor, archive_floor = await journal_floors(session, tenant_id)
    events: list[JournalEvent] = []
    unservable = (
        (start.tx_id, start.sequence) < (archive_floor.tx_id, archive_floor.sequence)
        if isinstance(start, EventPosition)
        # A legacy sequence floor gets the same protection: below the pruned
        # sequence it would silently skip events that no longer exist.
        else start.floor < archive_floor.sequence
    )
    if unservable:
        raise ValidationError(
            "cursor_below_journal_floor",
            "The requested cursor is older than the retained journal",
            details={
                "floorCursor": encode_cursor(archive_floor),
                "requestedCursor": encode_cursor(start),
            },
        )

    below_floor = (journal_floor.tx_id, journal_floor.sequence) > (0, 0) and (
        not isinstance(start, EventPosition)
        or (start.tx_id, start.sequence) < (journal_floor.tx_id, journal_floor.sequence)
    )
    if below_floor:
        events.extend(
            (
                await session.scalars(
                    _archive_stmt(
                        tenant_id=tenant_id,
                        start=start,
                        until=journal_floor,
                        limit=limit,
                        filters=filters,
                    )
                )
            ).all()
        )
        if len(events) >= limit:
            return _Page(events=events[:limit], journal_floor=journal_floor)
        start = position_of(events[-1]) if events else start

    stmt = _replay_stmt(
        tenant_id=tenant_id,
        start=start,
        limit=limit - len(events),
        filters=filters,
    )
    events.extend((await session.scalars(stmt)).all())
    return _Page(events=events, journal_floor=journal_floor)


def _archive_before_stmt(
    *,
    tenant_id: uuid.UUID,
    before: EventPosition | None,
    limit: int,
    filters: EventFilter,
) -> Select[tuple[EventArchive]]:
    stmt = (
        select(EventArchive)
        .where(EventArchive.tenant_id == tenant_id)
        .order_by(EventArchive.tx_id.desc(), EventArchive.sequence.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(
            tuple_(EventArchive.tx_id, EventArchive.sequence) < (before.tx_id, before.sequence)
        )
    return filters.apply(stmt, EventArchive)


def _hot_before_stmt(
    *,
    tenant_id: uuid.UUID,
    before: EventPosition | None,
    limit: int,
    filters: EventFilter,
) -> Select[tuple[Event]]:
    stmt = (
        select(Event)
        .where(Event.tenant_id == tenant_id, Event.tx_id < _stable_horizon())
        .order_by(Event.tx_id.desc(), Event.sequence.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(tuple_(Event.tx_id, Event.sequence) < (before.tx_id, before.sequence))
    return filters.apply(stmt, Event)


async def fetch_events_before(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    before: EventPosition | None,
    limit: int,
    filters: EventFilter | None = None,
) -> list[JournalEvent]:
    """Up to ``limit`` stable events strictly below ``before``, NEWEST FIRST.

    ``before=None`` reads back from the stable horizon (the tail). Hot rows
    all sort above the journal floor and archived rows at or below it, so
    "hot first, then archive" keeps the reverse order. A concurrent archive
    run is handled as in :func:`fetch_events_after`: the page is redone once
    if the floor moved while it was assembled.
    """
    filters = filters or EventFilter()
    page = await _fetch_page_before(
        session, tenant_id=tenant_id, before=before, limit=limit, filters=filters
    )
    journal_floor, _archive_floor = await journal_floors(session, tenant_id)
    if page.floor_moved(journal_floor):
        page = await _fetch_page_before(
            session, tenant_id=tenant_id, before=before, limit=limit, filters=filters
        )
    return page.events


async def _fetch_page_before(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    before: EventPosition | None,
    limit: int,
    filters: EventFilter,
) -> _Page:
    journal_floor, archive_floor = await journal_floors(session, tenant_id)
    if (
        before is not None
        and archive_floor > ORIGIN
        and (before.tx_id, before.sequence) <= (archive_floor.tx_id, archive_floor.sequence)
    ):
        # Everything below such a cursor was pruned: an empty page would read
        # as "start of the journal reached", which is not what happened.
        raise ValidationError(
            "cursor_below_journal_floor",
            "The requested cursor is older than the retained journal",
            details={
                "floorCursor": encode_cursor(archive_floor),
                "requestedCursor": encode_cursor(before),
            },
        )
    events: list[JournalEvent] = list(
        (
            await session.scalars(
                _hot_before_stmt(tenant_id=tenant_id, before=before, limit=limit, filters=filters)
            )
        ).all()
    )
    if len(events) < limit and journal_floor > ORIGIN:
        events.extend(
            (
                await session.scalars(
                    _archive_before_stmt(
                        tenant_id=tenant_id,
                        before=before,
                        limit=limit - len(events),
                        filters=filters,
                    )
                )
            ).all()
        )
    return _Page(events=events, journal_floor=journal_floor)


async def current_position(session: AsyncSession, tenant_id: uuid.UUID) -> EventPosition:
    """Highest delivered-or-deliverable position for the tenant right now.

    The greatest ``(tx_id, sequence)`` below the stable horizon. Replaying
    after this position yields exactly the events that were not yet stable at
    the moment it was taken — nothing stable is skipped, because pending
    transactions all sort after it (``tx_id >= xmin``).

    Falls back to the journal floor when the hot table is empty for this
    tenant: after an archive run the frontier is NOT the origin, and handing
    out the origin would make every fresh follower replay the whole archive.
    """
    row = (
        await session.execute(
            select(Event.tx_id, Event.sequence)
            .where(Event.tenant_id == tenant_id, Event.tx_id < _stable_horizon())
            .order_by(Event.tx_id.desc(), Event.sequence.desc())
            .limit(1)
        )
    ).first()
    if row is not None:
        return EventPosition(tx_id=row[0], sequence=row[1])
    journal_floor, _archive_floor = await journal_floors(session, tenant_id)
    return journal_floor


@dataclass(frozen=True)
class EventPage:
    """One journal page, events always in delivery (ascending) order.

    ``prev_cursor`` is the ``before`` of the preceding page. Backward reads
    (``before``, ``tail``) know exactly whether something precedes the page
    and give ``None`` at the start of the journal; a forward page gives the
    cursor of its first event (the page before it may turn out empty) and
    ``None`` when it is empty.
    """

    events: list[JournalEvent]
    next_cursor: str
    has_more: bool
    prev_cursor: str | None = None


def _conflicting_cursors(*names: str) -> ValidationError:
    return ValidationError(
        "conflicting_cursors",
        "A page is read either forward or backward: give one of "
        + ", ".join(f"'{n}'" for n in names),
        details={"parameters": list(names)},
    )


async def list_events(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    after: int | None = None,
    before: str | None = None,
    tail: int | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    types: tuple[str, ...] = (),
    workspace_id: uuid.UUID | None = None,
    include_descendants: bool | None = None,
    actor_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> EventPage:
    backward_to: EventPosition | None = None
    if before is not None:
        given = [
            name
            for name, value in (("cursor", cursor), ("after", after), ("tail", tail))
            if value is not None
        ]
        if given:
            raise _conflicting_cursors("before", *given)
        decoded = decode_cursor(before)
        if not isinstance(decoded, EventPosition):
            raise ValidationError(
                "invalid_cursor",
                "'before' takes an event cursor, not a legacy sequence",
            )
        backward_to = decoded

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
    effective_limit = clamp_limit(limit)

    if backward_to is not None:
        return await _events_before(
            session,
            tenant_id=ctx.tenant_id,
            before=backward_to,
            limit=effective_limit,
            filters=filters,
        )

    if tail is not None:
        return await _tail_events(
            session,
            tenant_id=ctx.tenant_id,
            tail=min(tail, effective_limit),
            filters=filters,
        )

    start: EventCursor
    if cursor is not None:
        start = decode_cursor(cursor)
    elif after is not None:
        start = LegacyFloor(after)
    else:
        # "From the beginning" means the beginning of what still EXISTS: after
        # a prune the origin is unservable, and a fresh reader must not be
        # permanently locked out of the journal.
        _journal_floor, archive_floor = await journal_floors(session, ctx.tenant_id)
        start = archive_floor

    # Taken before the page (see past_filtered_out): a narrowed page that
    # reaches the end of the journal lets the cursor skip what it filtered out.
    frontier = await current_position(session, ctx.tenant_id) if filters.narrows else None
    events = await fetch_events_after(
        session,
        tenant_id=ctx.tenant_id,
        start=start,
        limit=effective_limit + 1,
        filters=filters,
    )
    has_more = len(events) > effective_limit
    events = events[:effective_limit]
    last: EventCursor = position_of(events[-1]) if events else start
    if not has_more:
        last = past_filtered_out(last, frontier)
    next_cursor = encode_cursor(last)
    return EventPage(
        events=events,
        next_cursor=next_cursor,
        has_more=has_more,
        prev_cursor=encode_position(position_of(events[0])) if events else None,
    )


async def _backward_page(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    before: EventPosition | None,
    limit: int,
    filters: EventFilter,
) -> tuple[list[JournalEvent], str | None]:
    """``limit`` events below ``before`` in delivery order, plus ``prevCursor``.

    One extra event is read to tell "the start of the journal" (``None``)
    from "more precede this page" (the cursor of its oldest event).
    """
    newest_first = await fetch_events_before(
        session, tenant_id=tenant_id, before=before, limit=limit + 1, filters=filters
    )
    more_before = len(newest_first) > limit
    events = newest_first[:limit]
    events.reverse()
    prev_cursor = encode_position(position_of(events[0])) if more_before else None
    return events, prev_cursor


async def _events_before(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    before: EventPosition,
    limit: int,
    filters: EventFilter,
) -> EventPage:
    """The page that ends just before ``before``; ``hasMore`` looks backward.

    ``nextCursor`` is the newest event of the page — reading forward from it
    reaches the events the reader already holds. An empty page echoes
    ``before``, the forward analogue of the echo on an empty forward page.
    """
    events, prev_cursor = await _backward_page(
        session, tenant_id=tenant_id, before=before, limit=limit, filters=filters
    )
    newest = position_of(events[-1]) if events else before
    return EventPage(
        events=events,
        next_cursor=encode_position(newest),
        has_more=prev_cursor is not None,
        prev_cursor=prev_cursor,
    )


async def _tail_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tail: int,
    filters: EventFilter,
) -> EventPage:
    """Last ``tail`` stable events in delivery order (for CLI/diagnostics).

    A backward read from the horizon: it reaches into the archive when the
    hot journal holds fewer events, and its ``prevCursor`` starts the walk
    back with ``before``.
    """
    # The fallback cursor is taken BEFORE the page query: under READ
    # COMMITTED each statement gets its own snapshot, and a cursor from a
    # LATER snapshot could sort past an event that stabilized between the
    # two statements while the (empty) page never showed it. Cursor-first
    # errs toward re-delivery, never toward a permanent skip.
    fallback = await current_position(session, tenant_id)
    events, prev_cursor = await _backward_page(
        session, tenant_id=tenant_id, before=None, limit=tail, filters=filters
    )
    next_cursor = encode_cursor(position_of(events[-1]) if events else fallback)
    return EventPage(
        events=events, next_cursor=next_cursor, has_more=False, prev_cursor=prev_cursor
    )
