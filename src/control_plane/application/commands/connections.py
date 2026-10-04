"""Connections: the tenant's accounts of external systems (CP-ADR-0079 §3, §4).

A connection is created ``pending`` against the latest ``active`` version of
its type and keeps that version until a person moves it. Two accounts of one
type are two connections with different keys; a key is never reused, because
a connection is never deleted — a revoked one connects again under its key.
Nothing here touches the secret store: the material, ``:authorize``, the key
of a connection and ``:revoke`` live in ``connection_access``. An agent reads
the connections its current revision names (``/agents/me/connections``, §8),
and no other.

The status moves the connector reports (``PUT /connections/{key}/status``) are
narrow on purpose: ``active`` on ``active`` only records that access was
checked, ``expired`` on ``active`` is the one transition, and access comes back
only through a new authorization. The connector is an agent of the registry
whose current revision names the connection (``spec.connections``, §8).

Rights: ``connections.read`` to read, ``connections.manage`` to create and
edit, ``connections.status.write`` to report. Another tenant's connection is
``404``, as a missing one.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.agents import active_agent_of_principal, revision_of
from control_plane.application.common import (
    make_created_cursor,
    new_uuid,
    parse_created_cursor,
    utcnow,
)
from control_plane.application.events import record_event
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.domain.connection_type import secret_refusal, settings_errors
from control_plane.domain.enums import (
    AgentStatus,
    ConnectionStatus,
    ConnectionTypeStatus,
    Permission,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.project import (
    MAX_CUSTOM_FIELD_BYTES,
    guard_json_document,
    secret_findings,
)
from control_plane.domain.redaction import redact_secret_material
from control_plane.infrastructure.db.models import Agent, AgentRevision, Connection, ConnectionType

MAX_STATUS_MESSAGE = 500


@dataclass(frozen=True)
class ConnectionView:
    """A connection and, on its card, the keys of the agents that name it."""

    connection: Connection
    agents: list[str]


async def _lock_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize creations of one (tenant, key): the second one sees the first."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:conn:{tenant_id}:{key}", 0)))
    )


def _not_found(key: str) -> NotFoundError:
    return NotFoundError("Connection not found", details={"connection": key})


async def connection_by_key(
    session: AsyncSession, ctx: AuthContext, key: str, *, for_update: bool = False
) -> Connection:
    stmt = select(Connection).where(Connection.tenant_id == ctx.tenant_id, Connection.key == key)
    if for_update:
        stmt = stmt.with_for_update()
    connection: Connection | None = await session.scalar(stmt)
    if connection is None:
        raise _not_found(key)
    return connection


async def _latest_active_type(
    session: AsyncSession, ctx: AuthContext, type_key: str
) -> ConnectionType:
    connection_type: ConnectionType | None = await session.scalar(
        select(ConnectionType)
        .where(
            ConnectionType.tenant_id == ctx.tenant_id,
            ConnectionType.key == type_key,
            ConnectionType.status == ConnectionTypeStatus.ACTIVE,
        )
        .order_by(ConnectionType.version.desc())
        .limit(1)
    )
    if connection_type is None:
        raise ValidationError(
            "unknown_connection_type",
            "The connection type has no active version",
            details={"type": type_key},
        )
    return connection_type


async def usable_type_version(
    session: AsyncSession, ctx: AuthContext, type_key: str, version: int
) -> ConnectionType:
    """A published version of the type that is not ``disabled``."""
    connection_type: ConnectionType | None = await session.scalar(
        select(ConnectionType).where(
            ConnectionType.tenant_id == ctx.tenant_id,
            ConnectionType.key == type_key,
            ConnectionType.version == version,
            ConnectionType.status != ConnectionTypeStatus.DISABLED,
        )
    )
    if connection_type is None:
        raise ValidationError(
            "unknown_connection_type",
            "The connection type has no such version, or it is disabled",
            details={"type": type_key, "typeVersion": version},
        )
    return connection_type


def check_settings(connection_type: ConnectionType, settings: dict[str, Any]) -> None:
    """§4: non-secret settings by the ``settingsSchema`` of the type version.

    The size of the document first, then secret material — credential-shaped
    strings and member names, names like a secret — so no answer of the schema
    check sees a value or a name shaped like a credential. Both refusals carry
    ``details.errors`` with JSON Pointers into ``settings`` (CP-ADR-0079, amendment of 2026-10-03).
    """
    guard_json_document(settings, label="settings", max_bytes=MAX_CUSTOM_FIELD_BYTES)
    found = secret_findings(settings)
    if found:
        raise secret_refusal(found, field="settings")
    errors = settings_errors(connection_type.spec["settingsSchema"], settings)
    if errors:
        raise ValidationError(
            "invalid_connection_settings",
            f"settings do not match the settings schema of the connection type"
            f" ({len(errors)} violations)",
            details={"field": "settings", "errors": errors},
        )


async def create_connection(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_key: str,
    key: str | None,
    display_name: str | None,
    settings: dict[str, Any] | None,
) -> Connection:
    """``POST /connections``: a ``pending`` connection on the latest active type version."""
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    connection_type = await _latest_active_type(session, ctx, type_key)
    effective_settings = settings if settings is not None else {}
    check_settings(connection_type, effective_settings)
    effective_key = key if key is not None else str(connection_type.spec["defaultKey"])
    await _lock_key(session, ctx.tenant_id, effective_key)
    taken = await session.scalar(
        select(Connection.id).where(
            Connection.tenant_id == ctx.tenant_id, Connection.key == effective_key
        )
    )
    if taken is not None:
        raise ConflictError(
            "connection_key_taken",
            "A connection with this key exists; name another key for a second account",
            details={"key": effective_key},
        )
    now = utcnow()
    connection = Connection(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=effective_key,
        type_key=connection_type.key,
        type_version=connection_type.version,
        display_name=display_name if display_name is not None else connection_type.display_name,
        account=None,
        auth=None,
        status=ConnectionStatus.PENDING,
        status_reason=None,
        status_message=None,
        settings=effective_settings,
        secret_ref=None,
        expires_at=None,
        connected_by=None,
        connected_at=None,
        last_checked_at=None,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
        version=1,
    )
    session.add(connection)
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection.created",
        entity_type="connection",
        entity_id=connection.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": connection.key,
            "type": connection.type_key,
            "typeVersion": connection.type_version,
            "status": connection.status,
        },
    )
    return connection


async def agents_naming(session: AsyncSession, connection: Connection) -> list[str]:
    """Keys of the agents whose current revision names the connection (§3)."""
    rows = await session.scalars(
        select(Agent.key)
        .join(
            AgentRevision,
            (AgentRevision.agent_id == Agent.id)
            & (AgentRevision.revision == Agent.current_revision),
        )
        .where(
            Agent.tenant_id == connection.tenant_id,
            Agent.status == AgentStatus.ACTIVE,
            AgentRevision.spec["connections"].contains([connection.key]),
        )
        .order_by(Agent.key)
    )
    return list(rows)


async def get_connection(session: AsyncSession, ctx: AuthContext, key: str) -> ConnectionView:
    """``GET /connections/{key}``: the card, with the agents that use it."""
    await authorize(ctx, Permission.CONNECTIONS_READ)
    connection = await connection_by_key(session, ctx, key)
    return ConnectionView(connection, await agents_naming(session, connection))


async def list_connections(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None,
    cursor: str | None,
    type_key: str | None,
    status: str | None,
) -> Page[Connection]:
    """``GET /connections``: newest first, by ``(created_at, id)``."""
    await authorize(ctx, Permission.CONNECTIONS_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(Connection).where(Connection.tenant_id == ctx.tenant_id)
    if type_key is not None:
        stmt = stmt.where(Connection.type_key == type_key)
    if status is not None:
        stmt = stmt.where(Connection.status == status)
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (Connection.created_at < created_at)
            | ((Connection.created_at == created_at) & (Connection.id < entity_id))
        )
    stmt = stmt.order_by(Connection.created_at.desc(), Connection.id.desc())
    rows = list((await session.scalars(stmt.limit(effective_limit + 1))).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)


async def update_connection(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    expected_version: int,
    changes: dict[str, Any],
) -> Connection:
    """``PATCH /connections/{key}``: ``displayName``, ``settings``, ``typeVersion``.

    ``settings`` are replaced whole and checked by the schema of the type
    version the connection has after the edit, so moving to a version whose
    schema the current settings fail is refused. Values equal to the current
    ones change nothing: no new version, no event.
    """
    await authorize(ctx, Permission.CONNECTIONS_MANAGE)
    connection = await connection_by_key(session, ctx, key, for_update=True)
    if connection.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Connection version does not match If-Match",
            details={"expectedVersion": expected_version, "currentVersion": connection.version},
        )
    type_version = changes.get("typeVersion", connection.type_version)
    settings = changes.get("settings", connection.settings)
    if "typeVersion" in changes or "settings" in changes:
        connection_type = await usable_type_version(session, ctx, connection.type_key, type_version)
        check_settings(connection_type, settings)
    updated = {
        "displayName": changes.get("displayName", connection.display_name),
        "settings": settings,
        "typeVersion": type_version,
    }
    current = {
        "displayName": connection.display_name,
        "settings": connection.settings,
        "typeVersion": connection.type_version,
    }
    changed = [name for name in updated if name in changes and updated[name] != current[name]]
    if changed:
        connection.display_name = updated["displayName"]
        connection.settings = updated["settings"]
        connection.type_version = updated["typeVersion"]
        connection.version += 1
        connection.updated_at = utcnow()
        await session.flush()
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="connection.updated",
            entity_type="connection",
            entity_id=connection.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={"key": connection.key, "version": connection.version, "changes": changed},
        )
    return connection


@dataclass(frozen=True)
class StatusReport:
    status: str
    reason: str | None
    message: str | None
    checked_at: datetime


async def _require_assigned(session: AsyncSession, ctx: AuthContext, key: str) -> None:
    """The caller is an agent of the registry whose current revision names ``key``.

    Checked before the connection is looked up, so a key outside the agent's
    list is ``403`` whether a connection of that key exists or not.
    """
    agent = await active_agent_of_principal(session, ctx.tenant_id, ctx.principal_id)
    revision = await revision_of(session, agent, agent.current_revision) if agent else None
    named = revision.spec.get("connections") if revision is not None else None
    if not isinstance(named, list) or key not in named:
        raise AuthorizationError(
            "The connection is not in the connections of the caller's agent",
            code="connection_not_assigned",
            details={"connection": key},
        )


async def report_connection_status(
    session: AsyncSession, ctx: AuthContext, *, key: str, report: StatusReport
) -> Connection:
    """``PUT /connections/{key}/status``: the connector's report (§3).

    ``active`` on ``active`` moves ``lastCheckedAt`` only; ``expired`` on
    ``active`` is a transition with a reason code and ``connection.status_changed``;
    ``expired`` on ``expired`` is a repeat and moves ``lastCheckedAt`` only.
    Every other pair is ``409 connection_status_conflict``: access comes back
    through a new authorization, not through the connector. A report older than
    ``lastCheckedAt`` is ``409 stale_status_report``.
    """
    await authorize(ctx, Permission.CONNECTIONS_STATUS_WRITE)
    await _require_assigned(session, ctx, key)
    connection = await connection_by_key(session, ctx, key, for_update=True)
    if connection.last_checked_at is not None and report.checked_at < connection.last_checked_at:
        raise ConflictError(
            "stale_status_report",
            "A newer status report is already stored",
            details={"connection": key, "lastCheckedAt": connection.last_checked_at.isoformat()},
        )
    current = connection.status
    if report.status == current and current in (ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED):
        connection.last_checked_at = report.checked_at
        await session.flush()
        return connection
    if not (current == ConnectionStatus.ACTIVE and report.status == ConnectionStatus.EXPIRED):
        raise ConflictError(
            "connection_status_conflict",
            "The connector reports only that an active connection expired;"
            " access comes back through a new authorization",
            details={"connection": key, "from": current, "to": report.status},
        )
    if report.reason is None:
        raise ValidationError(
            "status_reason_required",
            "An expired connection needs a reason code",
            details={"field": "reason"},
        )
    connection.status = ConnectionStatus.EXPIRED
    connection.status_reason = report.reason
    connection.status_message = (
        redact_secret_material(report.message)[:MAX_STATUS_MESSAGE]
        if report.message is not None
        else None
    )
    connection.last_checked_at = report.checked_at
    connection.version += 1
    connection.updated_at = utcnow()
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="connection.status_changed",
        entity_type="connection",
        entity_id=connection.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": connection.key,
            "type": connection.type_key,
            "from": current,
            "to": connection.status,
            "reason": connection.status_reason,
            "connectedBy": str(connection.connected_by) if connection.connected_by else None,
        },
    )
    return connection


# --- what an agent learns of its connections (§8) -------------------------------------


async def _named_by_caller(session: AsyncSession, ctx: AuthContext) -> list[str]:
    """The connections the current revision of the caller's agent names.

    The caller is not an agent of the registry — ``404``, as ``/agents/me``.
    """
    agent = await active_agent_of_principal(session, ctx.tenant_id, ctx.principal_id)
    if agent is None:
        raise NotFoundError("The caller is not a registered agent")
    revision = await revision_of(session, agent, agent.current_revision)
    named = revision.spec.get("connections") if revision is not None else None
    return [key for key in named if isinstance(key, str)] if isinstance(named, list) else []


async def my_connections(session: AsyncSession, ctx: AuthContext) -> list[Connection]:
    """``GET /agents/me/connections``: the connections the caller's revision names that exist."""
    named = await _named_by_caller(session, ctx)
    if not named:
        return []
    rows = await session.scalars(
        select(Connection)
        .where(Connection.tenant_id == ctx.tenant_id, Connection.key.in_(named))
        .order_by(Connection.key)
    )
    return list(rows)


async def my_connection(session: AsyncSession, ctx: AuthContext, key: str) -> Connection:
    """``GET /agents/me/connections/{key}``: a key outside the list is ``404``, as a missing one."""
    if key not in await _named_by_caller(session, ctx):
        raise _not_found(key)
    return await connection_by_key(session, ctx, key)
