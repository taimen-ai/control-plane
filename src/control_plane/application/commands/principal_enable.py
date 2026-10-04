"""Enabling a principal: the way back for a person taken out by ``:disable``.

The mirror of ``principal_disable`` with one deliberate asymmetry
(CP-ADR-0077, amendment «Включение»): only the status comes back. The IAM
bindings, delegations, sessions and claims that ``:disable`` closed stay
closed — entry through IAM returns with a new, explicit
``POST /principals/{id}/iam-bindings``, whose permissions the operator states
anew, instead of whatever the person held before they left. What does come
back with the status are the API keys ``:disable`` left unrevoked: a key is
checked against the principal's status on every request. That is why the
escalation rule of every grant applies: enabling a principal hands out again
whatever its live keys hold, so the caller must hold all of it itself — and
enabling a principal that holds ``admin`` is making an admin.

Lock order: the prefix of ``:disable`` — agent records (``FOR SHARE``), then
the principal ``FOR UPDATE`` together with the caller's ``FOR KEY SHARE`` in id
order. Nothing later is locked: bindings and keys are only read, and no
session, task, claim, run or skill call is touched.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.iam_bindings import BINDING_STATUS_REVOKED
from control_plane.application.commands.principal_disable import DISABLEABLE_KINDS, load_target
from control_plane.application.commands.principals import ungranted_permissions
from control_plane.application.common import utcnow
from control_plane.application.events import event_reason, record_event
from control_plane.domain.enums import AgentStatus, Permission, PrincipalStatus
from control_plane.domain.errors import AuthorizationError, ConflictError, ValidationError
from control_plane.infrastructure.db.models import ApiKey, IamPrincipalBinding, Principal

# The kinds ``:disable`` takes out are the kinds ``:enable`` brings back;
# a service is its installer's business either way.
ENABLEABLE_KINDS = DISABLEABLE_KINDS


@dataclass(frozen=True)
class EnabledPrincipal:
    principal: Principal
    changed: bool
    # Live (not revoked, not expired) API keys of the principal: they
    # authenticate again now that it is active.
    live_api_keys: int = 0
    # Identities whose enforcement cache the API drops after commit: every
    # binding of the principal, revoked ones included, so that no answer
    # cached while it was disabled outlives the change of status.
    touched_identities: list[tuple[str, uuid.UUID]] = field(default_factory=list)


@dataclass(frozen=True)
class EnableGate:
    principal: Principal
    # False: already active, the call is a repeat and changes nothing.
    changed: bool
    # Live (not revoked, not expired) API keys of the principal, as the gate
    # counted them: the command reports this number, a repeat included.
    live_api_keys: int = 0
    # Every binding of the principal, revoked ones included; empty on a repeat.
    bindings: Sequence[IamPrincipalBinding] = ()


async def live_api_key_permissions(
    session: AsyncSession, principal: Principal, now: datetime
) -> list[list[str]]:
    """The permissions of each key that would authenticate once it is active."""
    rows = await session.scalars(
        select(ApiKey.permissions)
        .where(
            ApiKey.tenant_id == principal.tenant_id,
            ApiKey.principal_id == principal.id,
            ApiKey.revoked_at.is_(None),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
        .order_by(ApiKey.id)
    )
    return [list(permissions) for permissions in rows]


async def enable_gate(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID, *, lock: bool = False
) -> EnableGate:
    """Everything that decides whether ``ctx`` may enable the principal.

    The command goes through here with ``lock``; ``POST /authz:check`` asks
    the same question without row locks (CP-ADR-0055, amendment of
    2026-09-29). A repeat on an active principal passes: the endpoint answers
    it with ``200``. Nothing later than the principal is locked: bindings and
    keys are only read.
    """
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    # The agent record before the principal, exactly as ``:disable`` and
    # ``:retire`` take them (CP-ADR-0077 §3); a concurrent ``:disable`` of the
    # same principal waits for this one (or this one for it).
    agents, principal = await load_target(session, ctx, principal_id, lock=lock)
    live_keys = await live_api_key_permissions(session, principal, utcnow())
    if principal.status == PrincipalStatus.ACTIVE:
        # Idempotent. It also covers the caller itself: a caller that is not
        # active has already been refused by its own lock.
        return EnableGate(principal=principal, changed=False, live_api_keys=len(live_keys))
    # The registry owns the identity of its agents: a retired agent's
    # principal is its history and comes back only as a new key. A live
    # agent's principal is enabled here — no other call makes it active again
    # (publishing does not touch the status), and its bindings come back
    # through ``PUT /agents/{key}/identity`` (CP-ADR-0073, I5).
    live_agent = next((a for a in agents if a.status != AgentStatus.RETIRED), None)
    if live_agent is None and principal.kind not in ENABLEABLE_KINDS:
        raise ValidationError(
            "principal_kind_not_enableable",
            "Only a human or an agent principal can be enabled",
            details={"kind": principal.kind},
        )
    bindings = (
        await session.scalars(
            select(IamPrincipalBinding)
            .where(
                IamPrincipalBinding.tenant_id == ctx.tenant_id,
                IamPrincipalBinding.principal_id == principal.id,
            )
            .order_by(IamPrincipalBinding.id)
        )
    ).all()
    # What comes back with the status: every live key and every binding that
    # ``:disable`` did not revoke (a paused principal keeps them). The caller
    # hands all of it out again, so it must hold all of it — the rule of
    # issuing a key or a binding, ``admin`` included.
    granted: set[str] = set()
    for permissions in live_keys:
        granted.update(permissions)
    for binding in bindings:
        if binding.status != BINDING_STATUS_REVOKED:
            granted.update(binding.permissions)
    missing = ungranted_permissions(ctx, granted)
    if missing:
        raise AuthorizationError(
            "Cannot enable a principal whose live credentials hold permissions"
            " the caller does not hold",
            code="permission_escalation",
            details={"missing": missing},
        )
    # After the escalation check, as in ``:disable``: the refusal names the agent.
    agent = agents[0] if agents and live_agent is None else None
    if agent is not None:
        raise ConflictError(
            "use_agent_publish",
            "The principal belongs to a registered agent: publish the agent instead",
            details={
                "principalId": str(principal.id),
                "agent": agent.key,
                "agentStatus": agent.status,
            },
        )
    return EnableGate(
        principal=principal, changed=True, live_api_keys=len(live_keys), bindings=bindings
    )


async def enable_principal(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    reason: str | None = None,
) -> EnabledPrincipal:
    gate = await enable_gate(session, ctx, principal_id, lock=True)
    principal = gate.principal
    live_keys = gate.live_api_keys
    if not gate.changed:
        return EnabledPrincipal(principal=principal, changed=False, live_api_keys=live_keys)

    previous_status = principal.status
    principal.status = PrincipalStatus.ACTIVE
    principal.updated_at = utcnow()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="principal.enabled",
        entity_type="principal",
        entity_id=principal.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "kind": principal.kind,
            "previousStatus": previous_status,
            "reason": event_reason(reason) if reason else None,
            "liveApiKeys": live_keys,
        },
    )
    return EnabledPrincipal(
        principal=principal,
        changed=True,
        live_api_keys=live_keys,
        touched_identities=[(b.issuer, b.iam_principal_id) for b in gate.bindings],
    )
