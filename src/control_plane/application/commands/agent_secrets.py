"""An agent's secrets by name (CP-ADR-0079 §11).

The node of fleet takes the names of ``placement.secrets`` from the secret
store. ``PUT /agents/{key}/secrets/{name}`` passes the value through the core
in transit into ``kv/data/tenants/<t>/agents/<key>/<name>`` (one version: the
engine keeps ``max_versions = 1``, §1); ``agent_secret_names`` keeps the name,
who set it and when — never the value, not even as a hash. ``GET`` answers the
names only. ``DELETE`` deletes the document with every version (``DELETE
kv/metadata/…``) and the name.

The agent reads its secrets with its own store token: the worker
``connections-policy-sync`` puts ``kv/data/tenants/<t>/agents/<key>/*`` into
its policy after ``agent.secret_set`` (§9), and deletes the documents and the
names of a retired agent.

The agent's row is locked ``FOR UPDATE`` for a write: two writes of one agent
take turns, and ``:retire`` either waits for one or is seen by it. A store that
is not configured or not reachable is ``503 secret_store_unavailable`` with
nothing changed.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.agents import require_agent, retired_conflict
from control_plane.application.commands.connection_access import (
    require_store,
    secret_store_unavailable,
)
from control_plane.application.common import utcnow
from control_plane.application.events import record_event
from control_plane.domain.connection_access import agent_secret_store_name, is_secret_name
from control_plane.domain.enums import AgentStatus, Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import Agent, AgentSecretName
from control_plane.infrastructure.secret_store import SecretStore, SecretStoreError


@dataclass(frozen=True)
class SecretSet:
    row: AgentSecretName
    created: bool


def _invalid_name() -> ValidationError:
    return ValidationError(
        "invalid_secret_name",
        "A secret name is ^[a-z0-9][a-z0-9-]{0,62}$, the form of placement.secrets",
        details={"field": "name"},
    )


async def _writable_agent(session: AsyncSession, ctx: AuthContext, key: str) -> Agent:
    agent = await require_agent(session, ctx, key, for_update=True)
    if agent.status == AgentStatus.RETIRED:
        # The worker deletes a retired agent's secrets; nobody writes them.
        raise retired_conflict(agent)
    return agent


async def set_agent_secret(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    *,
    key: str,
    name: str,
    value: str,
) -> SecretSet:
    """``PUT /agents/{key}/secrets/{name}``: the value to the store, the name to the records.

    The name is written (and flushed) first and the value last, so a refusal
    of the store rolls the name back; replacing a value moves ``updatedAt``
    and ``updatedBy`` only.
    """
    await authorize(ctx, Permission.AGENTS_SECRETS_MANAGE)
    if not is_secret_name(name):
        raise _invalid_name()
    agent = await _writable_agent(session, ctx, key)
    secret_store = require_store(store)
    now = utcnow()
    row = await session.get(AgentSecretName, (agent.id, name))
    created = row is None
    if row is None:
        row = AgentSecretName(
            agent_id=agent.id,
            name=name,
            tenant_id=ctx.tenant_id,
            created_by=ctx.principal_id,
            created_at=now,
            updated_by=ctx.principal_id,
            updated_at=now,
        )
        session.add(row)
    else:
        row.updated_by = ctx.principal_id
        row.updated_at = now
    await session.flush()
    try:
        await secret_store.kv_write(
            agent_secret_store_name(ctx.tenant_id, agent.key, name), {"value": value}
        )
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="agent.secret_set",
        entity_type="agent",
        entity_id=agent.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"agentKey": agent.key, "name": name, "created": created},
    )
    return SecretSet(row, created)


async def list_agent_secrets(
    session: AsyncSession, ctx: AuthContext, *, key: str
) -> list[AgentSecretName]:
    """``GET /agents/{key}/secrets``: the names, by name; no value, no store."""
    await authorize(ctx, Permission.AGENTS_READ)
    agent = await require_agent(session, ctx, key)
    return list(
        await session.scalars(
            select(AgentSecretName)
            .where(AgentSecretName.agent_id == agent.id)
            .order_by(AgentSecretName.name)
        )
    )


async def delete_agent_secret(
    session: AsyncSession,
    ctx: AuthContext,
    store: SecretStore | None,
    *,
    key: str,
    name: str,
) -> None:
    """``DELETE /agents/{key}/secrets/{name}``: every version in the store, then the name.

    An unknown name — and a name that could never be one — is ``404``.
    """
    await authorize(ctx, Permission.AGENTS_SECRETS_MANAGE)
    agent = await _writable_agent(session, ctx, key)
    row = await session.get(AgentSecretName, (agent.id, name)) if is_secret_name(name) else None
    if row is None:
        raise NotFoundError("Agent secret not found", details={"agent": key, "name": name})
    secret_store = require_store(store)
    try:
        await secret_store.kv_delete_all(agent_secret_store_name(ctx.tenant_id, agent.key, name))
    except SecretStoreError as exc:
        raise secret_store_unavailable(exc.reason) from exc
    await session.delete(row)
    await session.flush()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="agent.secret_deleted",
        entity_type="agent",
        entity_id=agent.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"agentKey": agent.key, "name": name},
    )
