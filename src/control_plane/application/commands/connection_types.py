"""Connection type versions: publish, move a status, resolve, list (CP-ADR-0079 §2).

Versions are the skill's (ADR-0021): the publisher names ``(key, version)``,
the pair never changes afterwards. Publishing a pair again with the same spec
— by the hash of its canonical JSON — returns the version as it is and records
nothing, so a package is applied twice without an error; another spec under
the same pair is ``409 connection_type_version_exists``. Only ``status``
moves, forward, under ``If-Match``. A type is never deleted.

Rights: ``connections.read`` to read, ``connections.manage`` to publish and to
move a status. Another tenant's type is ``404``, as a missing one.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import (
    make_created_cursor,
    new_uuid,
    parse_created_cursor,
    utcnow,
)
from control_plane.application.events import record_event
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.application.queries.package_links import in_package
from control_plane.domain.connection_type import check_connection_type_spec
from control_plane.domain.enums import ConnectionTypeStatus, Permission
from control_plane.domain.errors import BadRequestError, ConflictError, NotFoundError
from control_plane.infrastructure.db.models import ConnectionType

KIND = "ConnectionType"

# Forward-only status moves of a published version (mirrors the trigger).
_STATUS_MOVES = {
    ConnectionTypeStatus.ACTIVE: frozenset(
        {ConnectionTypeStatus.DEPRECATED, ConnectionTypeStatus.DISABLED}
    ),
    ConnectionTypeStatus.DEPRECATED: frozenset({ConnectionTypeStatus.DISABLED}),
    ConnectionTypeStatus.DISABLED: frozenset(),
}


@dataclass(frozen=True)
class Published:
    connection_type: ConnectionType
    created: bool


async def _lock_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize publications of one (tenant, key): a repeat sees the first."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:ct:{tenant_id}:{key}", 0)))
    )


async def publish_connection_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    version: int,
    spec: dict[str, object],
) -> Published:
    """``POST /connection-types``: a new version, or the same one again."""
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    checked = check_connection_type_spec(spec)
    await _lock_key(session, ctx.tenant_id, key)
    existing = await session.scalar(
        select(ConnectionType).where(
            ConnectionType.tenant_id == ctx.tenant_id,
            ConnectionType.key == key,
            ConnectionType.version == version,
        )
    )
    if existing is not None:
        if existing.spec_hash == checked.spec_hash:
            return Published(existing, created=False)
        raise ConflictError(
            "connection_type_version_exists",
            "This version of the connection type is published with another spec;"
            " publish a new version",
            details={"key": key, "version": version, "specHash": existing.spec_hash},
        )
    connection_type = ConnectionType(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        version=version,
        status=ConnectionTypeStatus.ACTIVE,
        display_name=checked.display_name,
        spec=checked.spec,
        spec_hash=checked.spec_hash,
        created_by=ctx.principal_id,
        created_at=utcnow(),
        row_version=1,
    )
    session.add(connection_type)
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection_type.published",
        entity_type="connection_type",
        entity_id=connection_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": key, "version": version, "auth": checked.auth},
    )
    return Published(connection_type, created=True)


def _parse_ref(ref: str) -> tuple[str, int | None] | None:
    """``key`` or ``key@version``; ``None`` for a version that is not a number."""
    key, pinned, version_text = ref.partition("@")
    if not pinned:
        return key, None
    if not (version_text.isascii() and version_text.isdigit()) or len(version_text) > 9:
        return None
    return key, int(version_text)


async def resolve_connection_type(
    session: AsyncSession, ctx: AuthContext, ref: str
) -> ConnectionType:
    """``GET /connection-types/{ref}``: ``key`` — the latest ``active`` version,
    ``key@version`` — that version, whatever its status."""
    await authorize(ctx, Permission.CONNECTIONS_READ)
    not_found = NotFoundError("Connection type not found", details={"connectionType": ref})
    parsed = _parse_ref(ref)
    if parsed is None:
        raise not_found
    key, version = parsed
    stmt = select(ConnectionType).where(
        ConnectionType.tenant_id == ctx.tenant_id, ConnectionType.key == key
    )
    if version is None:
        stmt = stmt.where(ConnectionType.status == ConnectionTypeStatus.ACTIVE)
        stmt = stmt.order_by(ConnectionType.version.desc()).limit(1)
    else:
        stmt = stmt.where(ConnectionType.version == version)
    connection_type = await session.scalar(stmt)
    if connection_type is None:
        raise not_found
    return connection_type


async def update_connection_type_status(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    ref: str,
    expected_row_version: int,
    status: str,
) -> ConnectionType:
    """``PATCH /connection-types/{key}@{version}``: move the status forward.

    The same status again is a no-op (the installer applies a deprecation
    twice): the version is returned as it is, ``row_version`` stays.
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    parsed = _parse_ref(ref)
    if parsed is not None and parsed[1] is None:
        raise BadRequestError(
            "invalid_request",
            "A status belongs to one version: address it as key@version",
            details={"connectionType": ref},
        )
    not_found = NotFoundError("Connection type not found", details={"connectionType": ref})
    if parsed is None:
        raise not_found
    key, version = parsed
    connection_type = await session.scalar(
        select(ConnectionType)
        .where(
            ConnectionType.tenant_id == ctx.tenant_id,
            ConnectionType.key == key,
            ConnectionType.version == version,
        )
        .with_for_update()
    )
    if connection_type is None:
        raise not_found
    if connection_type.row_version != expected_row_version:
        raise ConflictError(
            "version_conflict",
            "Connection type version does not match If-Match",
            details={
                "expectedVersion": expected_row_version,
                "currentVersion": connection_type.row_version,
            },
        )
    if status == connection_type.status:
        return connection_type
    if status not in _STATUS_MOVES[ConnectionTypeStatus(connection_type.status)]:
        raise ConflictError(
            "invalid_status_transition",
            "Connection type status may only move active -> deprecated -> disabled",
            details={"from": connection_type.status, "to": status},
        )
    connection_type.status = status
    connection_type.row_version += 1
    await session.flush()
    return connection_type


async def list_connection_types(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None,
    cursor: str | None,
    key: str | None,
    status: str | None,
    package: str | None,
) -> Page[ConnectionType]:
    """``GET /connection-types``: newest first, by ``(created_at, id)``."""
    await authorize(ctx, Permission.CONNECTIONS_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(ConnectionType).where(ConnectionType.tenant_id == ctx.tenant_id)
    if key is not None:
        stmt = stmt.where(ConnectionType.key == key)
    if status is not None:
        stmt = stmt.where(ConnectionType.status == status)
    if package is not None:
        stmt = stmt.where(in_package(KIND, ConnectionType.tenant_id, ConnectionType.key, package))
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ConnectionType.created_at < created_at)
            | ((ConnectionType.created_at == created_at) & (ConnectionType.id < entity_id))
        )
    stmt = stmt.order_by(ConnectionType.created_at.desc(), ConnectionType.id.desc())
    rows = list((await session.scalars(stmt.limit(effective_limit + 1))).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)
