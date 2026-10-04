"""IAM identity bindings: the management surface over ``iam_principal_bindings``.

A binding is the IAM-era counterpart of an API key: it is where a federated
identity gets its Control Plane permissions (ADR-0053). So the rules that
guard key issuance apply here unchanged — the caller cannot hand out more than
it holds, and only an admin can make another admin. What is new is a rule
about the target: a non-human principal is never given the two rights that
exist to keep a human in the loop, nor narrowed to the workspaces of its
membership: visibility by membership is a human's (CP-ADR-0082 §2.3).
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    VISIBILITIES,
    VISIBILITY_MEMBERS,
    VISIBILITY_TENANT,
    AuthContext,
    authorize,
)
from control_plane.application.commands.principals import (
    get_tenant_principal,
    ungranted_permissions,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import (
    ALL_PERMISSIONS,
    AgentStatus,
    Permission,
    PrincipalKind,
    PrincipalStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import Agent, IamPrincipalBinding, Principal

# Rights that presuppose a human decision. An agent or a service that held
# ``admin`` could rewrite its own binding; one that held ``approvals.decide``
# would approve the very gate meant to stop it.
HUMAN_ONLY_PERMISSIONS = frozenset({Permission.ADMIN.value, Permission.APPROVALS_DECIDE.value})

BINDING_STATUS_ACTIVE = "active"
BINDING_STATUS_REVOKED = "revoked"

# ``visibility`` absent from the request: the mode of the binding is kept.
VISIBILITY_UNSET: Any = object()


@dataclass(frozen=True)
class UpsertedBinding:
    binding: IamPrincipalBinding
    created: bool


def _check_known_permissions(permissions: list[str]) -> None:
    unknown = sorted(set(permissions) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationError(
            "invalid_permissions",
            "Unknown permissions requested",
            details={"unknown": unknown},
        )
    if not permissions:
        raise ValidationError("invalid_permissions", "permissions must not be empty")


def _check_permissions_for_kind(permissions: list[str], principal_kind: str) -> None:
    if principal_kind != PrincipalKind.HUMAN:
        forbidden = sorted(HUMAN_ONLY_PERMISSIONS & set(permissions))
        if forbidden:
            raise ValidationError(
                "permissions_not_allowed_for_kind",
                f"A principal of kind {principal_kind!r} cannot hold human-only permissions",
                details={"kind": principal_kind, "forbidden": forbidden},
            )


def check_visibility_for_kind(visibility: str | None, principal_kind: str) -> None:
    """``members`` narrows a human only; an agent's reach is its package's."""
    if visibility == VISIBILITY_MEMBERS and principal_kind != PrincipalKind.HUMAN:
        raise ValidationError(
            "visibility_requires_human",
            "Only a human can be narrowed to the workspaces of their membership",
            details={"errors": [{"path": "/visibility"}]},
        )


def parse_visibility(value: Any) -> str | None:
    """``visibility`` of the request; ``None`` when absent (the mode is kept).

    ``null``, another string or not a string — ``422 validation_error`` with
    the JSON Pointer of the field and the JSON Schema keyword, without the
    value (CP-ADR-0082 §2.2).
    """
    if value is VISIBILITY_UNSET:
        return None
    if isinstance(value, str) and value in VISIBILITIES:
        return value
    code = "enum" if isinstance(value, str) else "type"
    raise ValidationError(
        "validation_error",
        "Request body does not match the schema",
        details={
            "errors": [
                {"path": "/visibility", "code": code, "message": "must be tenant or members"}
            ]
        },
    )


def check_visibility_escalation(
    ctx: AuthContext,
    *,
    visibility: str | None,
    existing: IamPrincipalBinding | None,
    principal: Principal,
) -> None:
    """A caller in ``members`` mode leaves no binding tenant-wide (CP-ADR-0082 B5).

    The rule of key issuance (CP-ADR-0053): no wider than one's own. Such a
    caller does not set ``tenant`` explicitly, nor by default on a new
    binding, nor by reopening a revoked one, nor by moving an identity onto
    another principal — for itself or anyone. Only a binding that is already
    active, tenant-wide and stays on its principal may have its rights
    changed while ``visibility`` is left out.
    """
    if ctx.visible_workspaces is None:
        return
    if visibility is not None:
        result = visibility
    elif existing is None or principal.kind != PrincipalKind.HUMAN:
        result = VISIBILITY_TENANT
    else:
        result = existing.visibility
        if (
            existing.principal_id == principal.id
            and existing.status == BINDING_STATUS_ACTIVE
            and existing.revoked_at is None
        ):
            # Left as it is: no binding becomes tenant-wide here.
            return
    if result == VISIBILITY_TENANT:
        raise _escalation()


def _escalation() -> AuthorizationError:
    return AuthorizationError(
        "A caller who sees only the workspaces of their membership cannot make"
        " a binding tenant-wide",
        code="visibility_escalation",
        details={"errors": [{"path": "/visibility"}]},
    )


def check_agent_binding_escalation(ctx: AuthContext) -> None:
    """The registry's binding of an agent or service is always ``tenant`` (B4):
    a caller in ``members`` mode does not make one (CP-ADR-0082 B5, V3)."""
    if ctx.visible_workspaces is not None:
        raise _escalation()


def validate_stored_permissions(*, permissions: list[str], principal_kind: str) -> list[str]:
    """The checks of ``validate_binding_permissions`` without the escalation rule.

    For rights the caller does not hand out itself but takes from a record
    checked when it was written — a published agent revision: the catalog
    and the kind rule may have changed since, the caller's own rights are not
    the measure.
    """
    _check_known_permissions(permissions)
    _check_permissions_for_kind(permissions, principal_kind)
    return sorted(set(permissions))


def validate_binding_permissions(
    ctx: AuthContext, *, permissions: list[str], principal_kind: str
) -> list[str]:
    """The same three checks an API key goes through, plus the kind rule."""
    _check_known_permissions(permissions)
    # No privilege escalation: a binding can only grant permissions its creator
    # holds (admin holds everything).
    if not ctx.has(Permission.ADMIN):
        if Permission.ADMIN.value in permissions:
            raise ValidationError(
                "invalid_permissions", "Only an admin can bind an identity as admin"
            )
        missing = ungranted_permissions(ctx, permissions)
        if missing:
            raise AuthorizationError(
                "Cannot grant permissions the calling credential does not hold",
                code="permission_escalation",
                details={"missing": missing},
            )
    _check_permissions_for_kind(permissions, principal_kind)
    return sorted(set(permissions))


def check_trusted_issuer(issuer: str, trusted_issuer: str) -> None:
    """Only the issuer whose tokens the core verifies can be bound.

    Enforcement accepts tokens of ``CP_IAM_ISSUER`` alone, so a binding of any
    other issuer admits nobody. On a registry agent such a call would revoke
    the working binding and leave a useless one: a denial of service, not a
    change of identity. Without a configured issuer (IAM off) there is nothing
    to compare with, and the caller passes the issuer it already trusts.
    """
    if trusted_issuer and issuer != trusted_issuer:
        raise ValidationError(
            "iam_issuer_untrusted",
            "The Control Plane does not accept tokens of this issuer",
            details={"issuer": issuer, "expected": trusted_issuer},
        )


def identity_taken(
    existing: IamPrincipalBinding | None, ctx: AuthContext, key: str
) -> ConflictError:
    """The refusal for an identity another principal holds, same for a racer.

    A concurrent writer that won the unique index gets the answer it would
    have got a moment later, read from the row that beat it.
    """
    if existing is None or existing.tenant_id != ctx.tenant_id:
        return ConflictError(
            "iam_identity_bound_elsewhere",
            "This IAM identity is already bound outside the current tenant",
        )
    return ConflictError(
        "agent_identity_conflict",
        "This IAM identity is already bound to another principal",
        details={"agent": key},
    )


async def _registry_agent_of(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> Agent | None:
    agent: Agent | None = await session.scalar(
        select(Agent).where(
            Agent.tenant_id == ctx.tenant_id,
            Agent.principal_id == principal_id,
            Agent.status != AgentStatus.RETIRED,
        )
    )
    return agent


def _registry_principal_conflict(agent: Agent) -> ConflictError:
    """The bindings of a registry agent's principal are the registry's own.

    Any identity bound here would enter with rights the revision does not set,
    and would survive ``identity:replace`` — the way back to a previous
    service identity with rights wider than the revision. Its own identity is
    no exception, for an admin neither: re-binding it would set rights beside
    the revision, and racing ``identity:replace`` would reopen the binding the
    replacement revokes. So every identity goes through the registry
    (CP-ADR-0073, amendment 2026-09-30, I4).
    """
    return ConflictError(
        "agent_identity_conflict",
        "The identity of a registry agent changes through the registry: "
        f"/agents/{agent.key}/identity (identity:replace for a service)",
        details={"agent": agent.key, "route": f"/agents/{agent.key}/identity"},
    )


async def _check_previous_owner(
    session: AsyncSession, ctx: AuthContext, *, existing_binding: IamPrincipalBinding
) -> None:
    """Moving an identity away from another principal takes it from its owner.

    Two owners are not given up by a plain upsert. An agent of the registry
    (CP-ADR-0073 §6) keeps its identity in ``agents``: moving the binding
    under it would leave the registry granting rights to a binding that is no
    longer its own, so the identity changes through the registry. Any other
    owner loses its entry: only an admin reassigns it — a human's login, and
    no less the identity of a service outside the registry.
    """
    agent_key = await session.scalar(
        select(Agent.key).where(
            Agent.tenant_id == ctx.tenant_id,
            Agent.principal_id == existing_binding.principal_id,
            Agent.iam_issuer == existing_binding.issuer,
            Agent.iam_principal_id == existing_binding.iam_principal_id,
            Agent.status != AgentStatus.RETIRED,
        )
    )
    if agent_key is not None:
        raise ConflictError(
            "agent_identity_conflict",
            "This IAM identity belongs to a registry agent; change it through the registry",
            details={"agent": agent_key},
        )
    if not ctx.has(Permission.ADMIN):
        owner = await session.get(Principal, existing_binding.principal_id)
        raise AuthorizationError(
            "Only an admin can move an identity to another principal",
            code="permission_escalation",
            details={"previousOwnerKind": owner.kind if owner is not None else None},
        )


async def _insert_new(
    session: AsyncSession, binding: IamPrincipalBinding
) -> IamPrincipalBinding | None:
    """Insert a binding of a new identity; the row that beat it, if one did.

    Two upserts of one new identity both find no row to lock, and the second
    insert hits ``uq_iam_bindings_identity`` once the first commits. Under a
    SAVEPOINT the loser keeps its transaction and reads the winner's row
    (locked, as the lookup would have locked it) instead of failing with a 500.
    """
    try:
        async with session.begin_nested():
            session.add(binding)
            await session.flush()
    except IntegrityError:
        winner: IamPrincipalBinding | None = await session.scalar(
            select(IamPrincipalBinding)
            .where(
                IamPrincipalBinding.issuer == binding.issuer,
                IamPrincipalBinding.iam_principal_id == binding.iam_principal_id,
            )
            .with_for_update()
        )
        if winner is None:
            raise  # not the identity index; nothing to answer with
        return winner
    return None


async def upsert_iam_binding(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    issuer: str,
    iam_tenant_id: uuid.UUID,
    iam_principal_id: uuid.UUID,
    permissions: list[str],
    trusted_issuer: str,
    visibility: Any = VISIBILITY_UNSET,
) -> UpsertedBinding:
    """Create or replace the binding of one federated identity.

    The identity ``(issuer, iam_principal_id)`` is the key: if it is already
    bound, the row is repointed and reopened rather than duplicated, so a
    revoked identity can be readmitted with one call. Taking it from another
    principal is checked against that owner first (``_check_previous_owner``);
    the principal of a registry agent takes no identity through here at all
    (``_registry_principal_conflict``). Losing a race to insert the same new
    identity is a 409 too (``_insert_new``).

    ``visibility`` left out keeps the mode of an existing binding and gives a
    new one ``tenant`` (CP-ADR-0082 §2.2): a console changing only the rights
    does not reset it. It is checked after ``principals.write``: a caller
    without the right learns nothing about the body (CP-ADR-0082 B5).
    """
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    visibility = parse_visibility(visibility)
    check_trusted_issuer(issuer, trusted_issuer)
    principal = await get_tenant_principal(session, ctx, principal_id)
    if principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot bind an identity to a non-active principal",
            details={"status": principal.status},
        )
    granted = validate_binding_permissions(
        ctx, permissions=permissions, principal_kind=principal.kind
    )
    check_visibility_for_kind(visibility, principal.kind)
    # Unlocked, and safely so: a principal becomes an agent's only in the
    # transaction that creates it (``PUT /agents/{key}/identity``), and every
    # call on an agent's principal is refused, so nothing here races
    # ``identity:replace`` for the agent's bindings.
    agent = await _registry_agent_of(session, ctx, principal.id)
    if agent is not None:
        raise _registry_principal_conflict(agent)

    now = utcnow()
    existing = await session.scalar(
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.issuer == issuer,
            IamPrincipalBinding.iam_principal_id == iam_principal_id,
        )
        .with_for_update()
    )
    if existing is not None and existing.tenant_id != ctx.tenant_id:
        # The pair is unique across tenants by design; a foreign row is not
        # ours to repoint, and saying so is not a leak — the caller already
        # knows the identity it asked about.
        raise ConflictError(
            "iam_identity_bound_elsewhere",
            "This IAM identity is already bound outside the current tenant",
        )
    previous_owner = existing.principal_id if existing is not None else None
    if existing is not None and existing.principal_id != principal.id:
        await _check_previous_owner(session, ctx, existing_binding=existing)
    check_visibility_escalation(ctx, visibility=visibility, existing=existing, principal=principal)

    if existing is None:
        binding = IamPrincipalBinding(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            principal_id=principal.id,
            issuer=issuer,
            iam_tenant_id=iam_tenant_id,
            iam_principal_id=iam_principal_id,
            permissions=granted,
            status=BINDING_STATUS_ACTIVE,
            visibility=visibility or VISIBILITY_TENANT,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        winner = await _insert_new(session, binding)
        if winner is not None and (
            winner.tenant_id != ctx.tenant_id or winner.principal_id != principal.id
        ):
            # Bound at the same moment elsewhere: the caller is told so rather
            # than taking the identity from a principal it never saw holding it.
            raise ConflictError(
                "iam_identity_bound_elsewhere",
                "This IAM identity was bound to another principal at the same time",
            )
        # The same identity on the same principal: the winner's row is the one
        # this call would have found a moment later, and it is updated below.
        existing = winner
    if existing is None:
        event_type = "iam_binding.created"
    else:
        binding = existing
        binding.principal_id = principal.id
        binding.iam_tenant_id = iam_tenant_id
        binding.permissions = granted
        binding.status = BINDING_STATUS_ACTIVE
        if visibility is not None:
            binding.visibility = visibility
        elif principal.kind != PrincipalKind.HUMAN:
            # An identity moved from a human to an agent or a service does not
            # carry the human's narrowing along.
            binding.visibility = VISIBILITY_TENANT
        binding.revoked_at = None
        binding.updated_at = now
        await session.flush()
        event_type = "iam_binding.updated"

    payload: dict[str, object] = {
        "principalId": str(binding.principal_id),
        "issuer": binding.issuer,
        "iamTenantId": str(binding.iam_tenant_id),
        "iamPrincipalId": str(binding.iam_principal_id),
        "permissions": binding.permissions,
        "visibility": binding.visibility,
    }
    if previous_owner is not None and previous_owner != binding.principal_id:
        payload["previousPrincipalId"] = str(previous_owner)
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="iam_binding",
        entity_id=binding.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )
    return UpsertedBinding(binding=binding, created=existing is None)


async def revoke_iam_binding(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    binding_id: uuid.UUID,
) -> IamPrincipalBinding:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    binding = await session.scalar(
        select(IamPrincipalBinding)
        .where(IamPrincipalBinding.id == binding_id, IamPrincipalBinding.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if binding is None:
        raise NotFoundError("IAM binding not found", details={"bindingId": str(binding_id)})
    if binding.status == BINDING_STATUS_REVOKED:
        return binding  # idempotent

    now = utcnow()
    binding.status = BINDING_STATUS_REVOKED
    binding.revoked_at = now
    binding.updated_at = now
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="iam_binding.revoked",
        entity_type="iam_binding",
        entity_id=binding.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "principalId": str(binding.principal_id),
            "issuer": binding.issuer,
            "iamPrincipalId": str(binding.iam_principal_id),
        },
    )
    return binding
