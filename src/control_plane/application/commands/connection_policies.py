"""Agents' access in the secret store: the ``connections-policy-sync`` worker (CP-ADR-0079 §9).

Only the core knows which agent may read which connection, so the core writes
the store's side of it: for every agent of the registry with an identity of
kind ``agent``, a CP principal and an IAM identity, that has something to
read, the ACL policy ``cp-agent-<principalId>`` (``read`` on the ``secretRef``
of each ``active`` connection its current revision names) and the ``jwt``
role ``agent-<principalId>`` bound to its IAM subject. An agent with nothing
to read — retired, no identity, a service account, no active connection — has
neither, and what it had is deleted. The agent's secrets by name (§11) add the
prefix ``kv/data/tenants/<t>/agents/<key>/*`` once it has one. Access needs an
agent that may log in to the core: its principal ``active`` and its IAM
binding ``active``; revoking or disabling either takes the store's access with
the next sync. The secrets of a retired agent are deleted from the store by
the store's own list, then their names; the full pass also deletes a value an
active agent has no name for.

Idempotent by construction: the current policy and role are read and written
only when they differ, so a second pass over unchanged records writes
nothing. Every sync of a tenant takes the tenant's advisory lock for its
transaction: ``:revoke`` narrows the policies under the same lock, so a pass
that read the connection as ``active`` never writes after the revocation.

When: a journal cursor of its own (``connections-policy-sync``) per tenant,
over the events that change who may read what; and a full pass every
``CP_CONNECTIONS_SYNC_SECONDS``, which also expires keys past ``expiresAt``
and deletes policies and roles of principals no agent row knows. A failure of
the store holds the tenant's cursor back with backoff; it never moves past
events it could not apply.
"""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.commands.iam_bindings import BINDING_STATUS_ACTIVE
from control_plane.application.commands.work_rules import ensure_consumer_cursor
from control_plane.application.common import utcnow
from control_plane.application.event_cursor import EventPosition
from control_plane.application.events import record_event
from control_plane.application.queries.events import fetch_events_after
from control_plane.domain.connection_access import (
    agent_policy,
    agent_policy_name,
    agent_role,
    agent_role_name,
    agent_secrets_ref,
    agent_secrets_store_name,
    agents_store_name,
    is_policy_path,
    principal_of_policy,
    principal_of_role,
    role_matches,
)
from control_plane.domain.enums import (
    AgentStatus,
    ConnectionAuth,
    ConnectionStatus,
    PrincipalKind,
    PrincipalStatus,
)
from control_plane.infrastructure.db.models import (
    Agent,
    AgentRevision,
    AgentSecretName,
    Connection,
    EventConsumerCursor,
    IamPrincipalBinding,
    Principal,
)
from control_plane.infrastructure.secret_store import SecretStore

logger = logging.getLogger(__name__)

# The journal consumer of this worker (event_consumer_cursors.name).
POLICY_SYNC_CONSUMER = "connections-policy-sync"

# The events after which the access of a tenant's agents may differ (§9).
_TRIGGERS = frozenset(
    {
        "agent.revision_published",
        "agent.retired",
        "agent.secret_set",
        "agent.secret_deleted",
        "iam_binding.created",
        "iam_binding.updated",
        "iam_binding.revoked",
    }
)

# ``statusReason`` of a key the worker found past its ``expiresAt`` (§7).
REASON_TOKEN_EXPIRED = "token_expired"


def triggers_sync(event_type: str) -> bool:
    return event_type in _TRIGGERS or event_type.startswith("connection.")


@dataclass(frozen=True)
class AgentAccess:
    """What one agent may read, and who it is in the IAM."""

    principal_id: uuid.UUID
    iam_principal_id: uuid.UUID
    iam_tenant_id: uuid.UUID
    read_paths: tuple[str, ...]


@dataclass
class SyncStats:
    """What a sync wrote to the store; all zeros when it was already in step."""

    policies_written: int = 0
    roles_written: int = 0
    policies_deleted: int = 0
    roles_deleted: int = 0
    # Agents' secrets deleted from the store or the names (§11): of retired
    # agents, and values without a name.
    secrets_deleted: int = 0

    def add(self, other: "SyncStats") -> None:
        self.policies_written += other.policies_written
        self.roles_written += other.roles_written
        self.policies_deleted += other.policies_deleted
        self.roles_deleted += other.roles_deleted
        self.secrets_deleted += other.secrets_deleted

    @property
    def changes(self) -> int:
        return (
            self.policies_written
            + self.roles_written
            + self.policies_deleted
            + self.roles_deleted
            + self.secrets_deleted
        )


@dataclass
class _Desired:
    access: dict[uuid.UUID, AgentAccess] = field(default_factory=dict)
    # Every principal of an agent row in scope: the ones without access lose it.
    known: set[uuid.UUID] = field(default_factory=set)

    @property
    def absent(self) -> set[uuid.UUID]:
        return self.known - set(self.access)


async def lock_tenant_policies(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Serialize the syncs of one tenant's policies, for the transaction."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:conn-policy:{tenant_id}", 0)))
    )


async def _desired(
    session: AsyncSession, tenant_id: uuid.UUID, *, naming: str | None = None
) -> _Desired:
    """The access the records give the tenant's agents (all, or those naming ``naming``).

    An agent gets access only while it may log in to the core: its principal
    is ``active`` and so is its IAM binding — the row of ``(iam_issuer,
    iam_principal_id)`` bound to that principal. A binding ``revoked`` or
    ``disabled``, or a disabled principal, takes the store's access away with
    the core's.
    """
    stmt = (
        select(
            Agent, AgentRevision.spec, Principal.kind, Principal.status, IamPrincipalBinding.status
        )
        .join(
            AgentRevision,
            (AgentRevision.agent_id == Agent.id)
            & (AgentRevision.revision == Agent.current_revision),
        )
        .join(Principal, Principal.id == Agent.principal_id)
        .outerjoin(
            IamPrincipalBinding,
            (IamPrincipalBinding.issuer == Agent.iam_issuer)
            & (IamPrincipalBinding.iam_principal_id == Agent.iam_principal_id)
            & (IamPrincipalBinding.principal_id == Agent.principal_id)
            & (IamPrincipalBinding.tenant_id == Agent.tenant_id),
        )
        .where(Agent.tenant_id == tenant_id, Agent.principal_id.is_not(None))
    )
    if naming is not None:
        stmt = stmt.where(AgentRevision.spec["connections"].contains([naming]))
    rows = (await session.execute(stmt)).tuples().all()
    active = {
        key: ref
        for key, ref in (
            await session.execute(
                select(Connection.key, Connection.secret_ref).where(
                    Connection.tenant_id == tenant_id,
                    Connection.status == ConnectionStatus.ACTIVE,
                    Connection.secret_ref.is_not(None),
                )
            )
        ).tuples()
        if ref is not None
    }
    with_secrets = set(
        await session.scalars(
            select(AgentSecretName.agent_id)
            .where(AgentSecretName.tenant_id == tenant_id)
            .distinct()
        )
    )
    desired = _Desired()
    for agent, spec, kind, principal_status, binding_status in rows:
        assert agent.principal_id is not None
        desired.known.add(agent.principal_id)
        if (
            agent.status != AgentStatus.ACTIVE
            or kind != PrincipalKind.AGENT
            or principal_status != PrincipalStatus.ACTIVE
            or binding_status != BINDING_STATUS_ACTIVE
            or agent.iam_principal_id is None
            or agent.iam_tenant_id is None
        ):
            continue
        named = spec.get("connections") if isinstance(spec, dict) else None
        paths = (
            {active[key] for key in named if isinstance(key, str) and key in active}
            if isinstance(named, list)
            else set()
        )
        if agent.id in with_secrets:
            paths.add(agent_secrets_ref(tenant_id, agent.key))
        for path in sorted(paths):
            if not is_policy_path(path):
                # Keys are checked long before; a record that slipped past
                # costs its own path, not the tenant's sync.
                logger.warning(
                    "connection policy path skipped",
                    extra={"tenant": str(tenant_id), "agent": str(agent.id)},
                )
                paths.discard(path)
        if paths:
            desired.access[agent.principal_id] = AgentAccess(
                principal_id=agent.principal_id,
                iam_principal_id=agent.iam_principal_id,
                iam_tenant_id=agent.iam_tenant_id,
                read_paths=tuple(sorted(paths)),
            )
    return desired


async def _grant(store: SecretStore, access: AgentAccess) -> SyncStats:
    """The policy first (the role names it), each written only when it differs."""
    stats = SyncStats()
    policy_name = agent_policy_name(access.principal_id)
    policy = agent_policy(list(access.read_paths))
    if await store.policy_read(policy_name) != policy:
        await store.policy_write(policy_name, policy)
        stats.policies_written += 1
    role_name = agent_role_name(access.principal_id)
    role = agent_role(
        principal_id=access.principal_id,
        iam_principal_id=access.iam_principal_id,
        iam_tenant_id=access.iam_tenant_id,
    )
    stored = await store.jwt_role_read(role_name)
    if stored is None or not role_matches(stored, role):
        await store.jwt_role_write(role_name, role)
        stats.roles_written += 1
    return stats


async def _withdraw(store: SecretStore, principal_id: uuid.UUID) -> SyncStats:
    """The role first: nobody logs in for a policy that is about to go."""
    stats = SyncStats()
    role_name = agent_role_name(principal_id)
    if await store.jwt_role_read(role_name) is not None:
        await store.jwt_role_delete(role_name)
        stats.roles_deleted += 1
    policy_name = agent_policy_name(principal_id)
    if await store.policy_read(policy_name) is not None:
        await store.policy_delete(policy_name)
        stats.policies_deleted += 1
    return stats


async def _apply(store: SecretStore, desired: _Desired) -> SyncStats:
    stats = SyncStats()
    for principal_id in sorted(desired.access):
        stats.add(await _grant(store, desired.access[principal_id]))
    for principal_id in sorted(desired.absent):
        stats.add(await _withdraw(store, principal_id))
    return stats


async def sync_tenant(
    session: AsyncSession,
    store: SecretStore,
    tenant_id: uuid.UUID,
    *,
    naming: str | None = None,
    sweep: bool = False,
) -> SyncStats:
    """Bring the store to the records for the tenant's agents (or those naming ``naming``).

    ``sweep`` — the full pass: the values of active agents with no name in
    the records are deleted as well (§11).

    Raises :class:`~control_plane.infrastructure.secret_store.SecretStoreError`
    when the store fails; what was written before stays, and a repeat finishes
    it.
    """
    await lock_tenant_policies(session, tenant_id)
    stats = await _apply(store, await _desired(session, tenant_id, naming=naming))
    if naming is None:
        stats.secrets_deleted += await _delete_agent_secrets(session, store, tenant_id, sweep=sweep)
    return stats


async def _stored_names(store: SecretStore, prefix: str) -> list[str]:
    """Every document under ``prefix`` in the store, as a path relative to it."""
    names: list[str] = []
    for key in await store.kv_list(prefix):
        if key.endswith("/"):
            folder = key.removesuffix("/")
            names += [f"{key}{name}" for name in await _stored_names(store, f"{prefix}/{folder}")]
        else:
            names.append(key)
    return sorted(names)


async def _secret_names(session: AsyncSession, agent_id: uuid.UUID) -> set[str]:
    return set(
        await session.scalars(
            select(AgentSecretName.name).where(AgentSecretName.agent_id == agent_id)
        )
    )


async def _delete_agent_secrets(
    session: AsyncSession, store: SecretStore, tenant_id: uuid.UUID, *, sweep: bool
) -> int:
    """The secrets the store keeps for nobody: by its list, the names second (§11).

    The store is listed under ``kv/metadata/tenants/<t>/agents/``: every
    document of a retired agent (or of a key no agent row has) is deleted
    with all its versions, whether ``agent_secret_names`` has its name or not
    — a ``PUT`` whose commit failed leaves a value without a name. Then the
    names of retired agents go. With ``sweep`` an active agent loses the
    values it has no name for; its row is locked ``SKIP LOCKED`` first, so a
    ``PUT`` that wrote its value and has not committed its name yet is left
    to the next pass. A document deleted without a name is logged by its
    path, never its value. Runs after the policies, so a retired agent's role
    is already gone. A failure of the store raises and rolls the names back;
    the deletions are idempotent, and the next sync repeats them.
    """
    root = agents_store_name(tenant_id)
    folders = sorted(
        key.removesuffix("/") for key in await store.kv_list(root) if key.endswith("/")
    )
    agents = {
        agent.key: agent
        for agent in await session.scalars(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.key.in_(folders))
        )
    }
    deleted: set[tuple[str, str]] = set()
    for agent_key in folders:
        agent = agents.get(agent_key)
        if agent is not None and agent.status != AgentStatus.RETIRED:
            if not sweep:
                continue
            locked = await session.scalar(
                select(Agent.id)
                .where(Agent.id == agent.id, Agent.status == AgentStatus.ACTIVE)
                .with_for_update(skip_locked=True)
            )
            if locked is None:
                continue
        prefix = agent_secrets_store_name(tenant_id, agent_key)
        stored = await _stored_names(store, prefix)
        named = await _secret_names(session, agent.id) if agent is not None else set()
        retired = agent is None or agent.status == AgentStatus.RETIRED
        for name in stored:
            if not retired and name in named:
                continue
            if name not in named:
                logger.warning(
                    "agent secret without a name deleted",
                    extra={"tenant": str(tenant_id), "agent": agent_key, "secret_name": name},
                )
            await store.kv_delete_all(f"{prefix}/{name}")
            deleted.add((agent_key, name))
    rows = (
        (
            await session.execute(
                select(AgentSecretName, Agent.key)
                .join(Agent, Agent.id == AgentSecretName.agent_id)
                .where(AgentSecretName.tenant_id == tenant_id, Agent.status == AgentStatus.RETIRED)
                .order_by(AgentSecretName.agent_id, AgentSecretName.name)
            )
        )
        .tuples()
        .all()
    )
    for row, agent_key in rows:
        # The store's list said what it keeps; a name without a value goes as well.
        await session.delete(row)
        deleted.add((agent_key, row.name))
    if rows:
        await session.flush()
    return len(deleted)


# --- the journal cursor -------------------------------------------------------------------


async def due_policy_tenants(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Tenants whose cursor has events to read and no backoff pending."""
    rows = await session.execute(
        text(
            """
            SELECT c.tenant_id
              FROM event_consumer_cursors c
             WHERE c.name = :name
               AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= now())
               AND EXISTS (
                   SELECT 1 FROM events e
                    WHERE e.tenant_id = c.tenant_id
                      AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)
                      AND e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint
               )
             ORDER BY c.updated_at ASC
             LIMIT :limit
            """
        ),
        {"name": POLICY_SYNC_CONSUMER, "limit": limit},
    )
    return [row[0] for row in rows]


async def process_tenant_events(
    session: AsyncSession,
    store: SecretStore,
    *,
    tenant_id: uuid.UUID,
    batch_size: int,
) -> int:
    """One batch of the tenant's journal: one sync if any event of it may change access.

    The cursor row is locked (``SKIP LOCKED``) and moves in the same
    transaction, after the store took the sync; a failure of the store raises
    and leaves the cursor where it was.
    """
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == POLICY_SYNC_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if cursor is None:
        return 0
    events = await fetch_events_after(
        session,
        tenant_id=tenant_id,
        start=EventPosition(cursor.tx_id, cursor.sequence),
        limit=batch_size,
    )
    if not events:
        return 0
    if any(triggers_sync(event.event_type) for event in events):
        stats = await sync_tenant(session, store, tenant_id)
        if stats.changes:
            logger.info(
                "connection policies synced",
                extra={"tenant": str(tenant_id), "changes": stats.changes},
            )
    last = events[-1]
    cursor.tx_id = last.tx_id
    cursor.sequence = last.sequence
    cursor.updated_at = utcnow()
    cursor.failure_count = 0
    cursor.next_attempt_at = None
    cursor.parked_at = None
    cursor.parked_reason = None
    cursor.parked_event_id = None
    return len(events)


async def record_tenant_failure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    reason: str,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> None:
    """Hold the tenant's cursor back with a growing delay and a visible reason (a code)."""
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == POLICY_SYNC_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if cursor is None:  # pragma: no cover - the full pass creates it
        return
    cursor.failure_count += 1
    cursor.parked_at = utcnow()
    cursor.parked_reason = reason[:2000]
    delay = min(backoff_base_seconds * (2 ** (cursor.failure_count - 1)), backoff_max_seconds)
    cursor.next_attempt_at = utcnow() + timedelta(seconds=delay)
    cursor.updated_at = utcnow()


# --- the full pass ---------------------------------------------------------------------------


async def policy_tenants(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants with an agent or a connection.

    The full pass's scope: an agent without a principal or a secret's name
    may still have a document in the store (a ``PUT`` whose commit failed).
    """
    rows = await session.execute(select(Agent.tenant_id).union(select(Connection.tenant_id)))
    return sorted({row[0] for row in rows})


async def ensure_policy_cursor(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The tenant's cursor, created at the present: the full pass covers the past."""
    await ensure_consumer_cursor(session, tenant_id, POLICY_SYNC_CONSUMER)


async def expire_keys(session: AsyncSession, *, now: datetime, trace_run_id: str) -> int:
    """``active`` keys (``auth = token``) past ``expiresAt`` become ``expired`` (§7)."""
    rows = (
        await session.scalars(
            select(Connection)
            .where(
                Connection.status == ConnectionStatus.ACTIVE,
                Connection.auth == ConnectionAuth.TOKEN,
                Connection.expires_at.is_not(None),
                Connection.expires_at <= now,
            )
            .order_by(Connection.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for connection in rows:
        connection.status = ConnectionStatus.EXPIRED
        connection.status_reason = REASON_TOKEN_EXPIRED
        connection.status_message = None
        connection.version += 1
        connection.updated_at = now
        await session.flush()
        await record_event(
            session,
            tenant_id=connection.tenant_id,
            event_type="connection.status_changed",
            entity_type="connection",
            entity_id=connection.id,
            actor_id=None,
            request_id="worker",
            correlation_id=f"{POLICY_SYNC_CONSUMER}:expiry",
            trace_run_id=trace_run_id,
            payload={
                "key": connection.key,
                "type": connection.type_key,
                "from": ConnectionStatus.ACTIVE.value,
                "to": ConnectionStatus.EXPIRED.value,
                "reason": REASON_TOKEN_EXPIRED,
                "connectedBy": str(connection.connected_by) if connection.connected_by else None,
            },
        )
    return len(rows)


async def orphan_principals(
    session: AsyncSession, store: SecretStore
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Principals with a ``cp-agent-*`` policy or an ``agent-*`` role that no agent row has.

    The agents a row knows are synced by their tenant (and lose what they
    should not have there); these belong to nobody the records know.
    """
    policies = {p for p in map(principal_of_policy, await store.policy_list()) if p is not None}
    roles = {p for p in map(principal_of_role, await store.jwt_role_list()) if p is not None}
    named = policies | roles
    if not named:
        return set(), set()
    known = set(
        await session.scalars(select(Agent.principal_id).where(Agent.principal_id.in_(named)))
    )
    return policies - known, roles - known


async def delete_orphans(
    store: SecretStore, policies: Iterable[uuid.UUID], roles: Iterable[uuid.UUID]
) -> SyncStats:
    stats = SyncStats()
    for principal_id in sorted(roles):
        await store.jwt_role_delete(agent_role_name(principal_id))
        stats.roles_deleted += 1
    for principal_id in sorted(policies):
        await store.policy_delete(agent_policy_name(principal_id))
        stats.policies_deleted += 1
    return stats
