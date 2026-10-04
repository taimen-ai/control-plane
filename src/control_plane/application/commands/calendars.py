"""Working-day calendars: immutable versions by canonical hash (CP-ADR-0074 §9).

A calendar is tenant data with a key. Publishing a spec whose canonical hash
differs from the latest version writes the next version and a
``calendar.published`` event; the same spec again writes nothing. Versions are
never changed or removed: a process journal names the version its deadline
was computed on, and replay reads that one.
"""

import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize, permits
from control_plane.application.commands.catalog_retirements import (
    CALENDAR,
    PROCESS,
    is_retired,
    lock_key,
    restore_key,
    retire_key,
    retirement,
    retirements,
)
from control_plane.application.commands.process_definitions import OPEN_STATUSES, process_scope
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import event_reason, record_event
from control_plane.application.queries.package_links import in_package
from control_plane.domain.calendar import Calendar, CalendarError, normalized_spec
from control_plane.domain.canonical import canonicalize, content_hash
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.process_definition import references
from control_plane.infrastructure.db.models import (
    CalendarVersion,
    CatalogRetirement,
    ProcessDefinition,
    ProcessInstance,
)


@dataclass(frozen=True)
class CalendarView:
    """A calendar version with the latest version of its key."""

    row: CalendarVersion
    latest_version: int
    created: bool = False
    retirement: CatalogRetirement | None = None

    @property
    def calendar(self) -> Calendar:
        return Calendar.from_spec(self.row.spec)


def _invalid(error: CalendarError) -> ValidationError:
    return ValidationError(
        "invalid_calendar", error.message, details={"code": error.code, "path": error.path}
    )


def check_calendar_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], str, Calendar]:
    """The stored form of a spec, its hash and the calendar it describes.

    ``spec`` is ``$defs.calendarSpec`` as sent (shape already validated, no
    defaults filled in). Order of years and dates does not make a version.
    """
    body = canonicalize(normalized_spec(spec), path="$.spec")
    try:
        calendar = Calendar.from_spec(body)
    except CalendarError as exc:
        raise _invalid(exc) from exc
    return body, content_hash(body), calendar


async def _lock_calendar_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize publication for one (tenant, key): versions are numbered in order."""
    await lock_key(session, tenant_id, CALENDAR, key)


async def _latest(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> CalendarVersion | None:
    row: CalendarVersion | None = await session.scalar(
        select(CalendarVersion)
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key == key)
        .order_by(CalendarVersion.version.desc())
        .limit(1)
    )
    return row


async def publish_calendar(
    session: AsyncSession, ctx: AuthContext, *, key: str, spec: dict[str, Any]
) -> CalendarView:
    """A new version only when the canonical hash differs from the latest one."""
    await authorize(ctx, Permission.CALENDARS_WRITE)
    body, calendar_hash, calendar = check_calendar_spec(spec)
    await _lock_calendar_key(session, ctx.tenant_id, key)
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is not None and latest.calendar_hash == calendar_hash:
        return CalendarView(latest, latest.version)

    number = 1 if latest is None else latest.version + 1
    row = CalendarVersion(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        version=number,
        calendar_hash=calendar_hash,
        spec=body,
        created_by=ctx.principal_id,
        created_at=utcnow(),
    )
    session.add(row)
    # A new version brings a retired key back into use (Zh1).
    await restore_key(session, ctx.tenant_id, CALENDAR, key)
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="calendar.published",
        entity_type="calendar",
        entity_id=row.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "version": number,
            "calendarHash": calendar_hash,
            "previousVersion": latest.version if latest is not None else None,
            "years": sorted(calendar.years),
            "provisionalYears": calendar.provisional_years,
        },
    )
    return CalendarView(row, number, created=True)


async def load_calendar(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, version: int | None = None
) -> CalendarView | None:
    """The latest version of a key, or the pinned one; ``None`` when there is none.

    No permission check: the engine reads the calendars of its tenant.
    """
    latest = await _latest(session, tenant_id, key)
    if latest is None:
        return None
    if version is None or version == latest.version:
        return CalendarView(latest, latest.version)
    row: CalendarVersion | None = await session.scalar(
        select(CalendarVersion).where(
            CalendarVersion.tenant_id == tenant_id,
            CalendarVersion.key == key,
            CalendarVersion.version == version,
        )
    )
    return None if row is None else CalendarView(row, latest.version)


async def resolve_calendar(session: AsyncSession, ctx: AuthContext, ref: str) -> CalendarView:
    """``key`` (latest version) or ``key@version``; reading needs authentication alone."""
    key, pinned, version_text = ref.partition("@")
    not_found = NotFoundError("Calendar not found", details={"calendar": ref})
    version: int | None = None
    if pinned:
        if not (version_text.isascii() and version_text.isdigit()) or len(version_text) > 9:
            raise not_found
        version = int(version_text)
    view = await load_calendar(session, ctx.tenant_id, key, version)
    if view is None:
        raise not_found
    return replace(view, retirement=await retirement(session, ctx.tenant_id, CALENDAR, key))


async def list_calendars(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int,
    after_key: str | None,
    package: str | None = None,
    status: str | None = None,
) -> tuple[list[CalendarView], str | None]:
    """The latest version of every key, by key; the last key of a full page continues it.

    ``status`` — only keys in use (``active``) or only retired ones (Zh1).
    """
    latest = (
        select(CalendarVersion.key, func.max(CalendarVersion.version).label("version"))
        .where(CalendarVersion.tenant_id == ctx.tenant_id)
        .group_by(CalendarVersion.key)
        .subquery()
    )
    stmt = (
        select(CalendarVersion)
        .join(
            latest,
            (CalendarVersion.key == latest.c.key) & (CalendarVersion.version == latest.c.version),
        )
        .where(CalendarVersion.tenant_id == ctx.tenant_id)
    )
    if package is not None:
        stmt = stmt.where(
            in_package("Calendar", CalendarVersion.tenant_id, CalendarVersion.key, package)
        )
    if status is not None:
        retired = is_retired(CALENDAR, CalendarVersion.tenant_id, CalendarVersion.key)
        stmt = stmt.where(retired if status == "retired" else ~retired)
    if after_key is not None:
        stmt = stmt.where(CalendarVersion.key > after_key)
    rows = list((await session.scalars(stmt.order_by(CalendarVersion.key).limit(limit + 1))).all())
    next_key = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_key = rows[-1].key
    marks = await retirements(session, ctx.tenant_id, CALENDAR, [row.key for row in rows])
    return [CalendarView(row, row.version, retirement=marks.get(row.key)) for row in rows], next_key


# --- retirement (CP-ADR-0074, amendment Zh3) ------------------------------------------------

# How many processes ``calendar_in_use`` names; ``total`` counts them all.
IN_USE_LIMIT = 50


@dataclass(frozen=True)
class CalendarRetirementView:
    key: str
    retirement: CatalogRetirement


async def processes_needing(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> list[tuple[ProcessDefinition, int]]:
    """Process versions that still need the calendar, with their open instances.

    The latest version of every process in use (it starts new instances) and
    the version of every open instance of any process, a retired one too (its
    timers and ``cal.*`` are still computed). A version needs the calendar
    when the key is in ``references(spec).calendars`` — the selection a new
    calendar version moves timers by (CP-ADR-0074 §9).
    """
    latest = (
        select(ProcessDefinition.key, func.max(ProcessDefinition.version).label("version"))
        .where(ProcessDefinition.tenant_id == tenant_id)
        .group_by(ProcessDefinition.key)
        .subquery()
    )
    heads = await session.scalars(
        select(ProcessDefinition)
        .join(
            latest,
            (ProcessDefinition.key == latest.c.key)
            & (ProcessDefinition.version == latest.c.version),
        )
        .where(
            ProcessDefinition.tenant_id == tenant_id,
            ~is_retired(PROCESS, ProcessDefinition.tenant_id, ProcessDefinition.key),
        )
    )
    found: dict[uuid.UUID, tuple[ProcessDefinition, int]] = {
        row.id: (row, 0) for row in heads if key in references(row.spec).calendars
    }
    live = await session.execute(
        select(ProcessDefinition, func.count(ProcessInstance.id))
        .join(ProcessInstance, ProcessInstance.definition_id == ProcessDefinition.id)
        .where(
            ProcessInstance.tenant_id == tenant_id,
            ProcessInstance.status.in_(OPEN_STATUSES),
        )
        .group_by(ProcessDefinition.id)
    )
    for row, count in live.all():
        if key in references(row.spec).calendars:
            found[row.id] = (row, int(count))
    return sorted(found.values(), key=lambda item: (item[0].key, item[0].version))


async def _readable_processes(
    ctx: AuthContext, rows: list[tuple[ProcessDefinition, int]]
) -> list[tuple[ProcessDefinition, int]]:
    """Those the caller may read: the right to a calendar is not one to other processes."""
    allowed: dict[uuid.UUID | None, bool] = {}
    out = []
    for row, count in rows:
        if row.workspace_id not in allowed:
            allowed[row.workspace_id] = await permits(
                ctx, Permission.PROCESSES_READ, resource=process_scope(row.workspace_id)
            )
        if allowed[row.workspace_id]:
            out.append((row, count))
    return out


async def retire_calendar(
    session: AsyncSession, ctx: AuthContext, *, key: str, reason: str, dry_run: bool = False
) -> CalendarRetirementView:
    """Every version of the key is retired, unless a process still needs it.

    A key already retired answers with its first retirement and writes nothing;
    ``dry_run`` makes the same checks and the same answer without writing.
    """
    await authorize(ctx, Permission.CALENDARS_WRITE)
    await _lock_calendar_key(session, ctx.tenant_id, key)
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is None:
        raise NotFoundError("Calendar not found", details={"calendar": key})
    existing = await retirement(session, ctx.tenant_id, CALENDAR, key)
    if existing is not None:
        return CalendarRetirementView(key, existing)
    needing = await processes_needing(session, ctx.tenant_id, key)
    if needing:
        shown = await _readable_processes(ctx, needing)
        raise ConflictError(
            "calendar_in_use",
            f"Calendar {key!r} is still needed by {len(needing)} process version(s)",
            details={
                "calendar": key,
                "processes": [
                    {"key": row.key, "version": row.version, "openInstances": count}
                    for row, count in shown[:IN_USE_LIMIT]
                ],
                "total": len(needing),
            },
        )
    text = event_reason(reason)
    now = utcnow()
    if dry_run:
        planned = CatalogRetirement(
            tenant_id=ctx.tenant_id,
            kind=CALENDAR,
            key=key,
            retired_at=now,
            retired_by=ctx.principal_id,
            reason=text,
        )
        return CalendarRetirementView(key, planned)
    row = await retire_key(
        session, ctx.tenant_id, CALENDAR, key, by=ctx.principal_id, reason=text, at=now
    )
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="calendar.retired",
        entity_type="calendar",
        entity_id=latest.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": key, "latestVersion": latest.version, "reason": text},
    )
    return CalendarRetirementView(key, row)
