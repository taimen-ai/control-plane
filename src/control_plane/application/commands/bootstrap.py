"""One-time system bootstrap: first tenant, admin principal, first API key.

Optionally the admin's first IAM binding as well (ADR-0053): on an IAM-only
deployment the API key is only the emergency path, and without a binding the
administrator could not make the first federated call that creates one.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import VISIBILITY_TENANT
from control_plane.application.commands.task_types import ensure_system_task_type
from control_plane.application.commands.workspace_types import ensure_system_workspace_type
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import ALL_PERMISSIONS, Permission, PrincipalKind, PrincipalStatus
from control_plane.domain.errors import ConflictError
from control_plane.infrastructure.auth.api_keys import GeneratedKey, generate_api_key
from control_plane.infrastructure.db.models import ApiKey, IamPrincipalBinding, Principal, Tenant

# Advisory lock key serializing concurrent bootstrap attempts.
_BOOTSTRAP_LOCK_KEY = 0x_C0_57_A1_01


@dataclass(frozen=True)
class BootstrapIamBinding:
    """The federated identity the admin principal will sign in with."""

    issuer: str
    iam_tenant_id: uuid.UUID
    iam_principal_id: uuid.UUID


@dataclass(frozen=True)
class BootstrapResult:
    tenant: Tenant
    admin: Principal
    api_key: ApiKey
    generated: GeneratedKey
    iam_binding: IamPrincipalBinding | None = None


async def bootstrap(
    session: AsyncSession,
    *,
    tenant_slug: str,
    tenant_name: str,
    admin_display_name: str,
    request_id: str,
    tenant_id: uuid.UUID | None = None,
    correlation_id: str = "",
    trace_run_id: str = "",
    iam_binding: BootstrapIamBinding | None = None,
) -> BootstrapResult:
    # Serialize competing bootstrap requests for the lifetime of this transaction.
    await session.execute(select(func.pg_advisory_xact_lock(_BOOTSTRAP_LOCK_KEY)))

    tenant_count = await session.scalar(select(func.count()).select_from(Tenant))
    if tenant_count:
        raise ConflictError("already_bootstrapped", "System is already bootstrapped")

    now = utcnow()
    tenant = Tenant(
        id=tenant_id or new_uuid(),
        slug=tenant_slug,
        name=tenant_name,
        created_at=now,
        updated_at=now,
    )
    admin = Principal(
        id=new_uuid(),
        tenant_id=tenant.id,
        kind=PrincipalKind.HUMAN,
        display_name=admin_display_name,
        status=PrincipalStatus.ACTIVE,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    generated = generate_api_key()
    api_key = ApiKey(
        id=new_uuid(),
        tenant_id=tenant.id,
        principal_id=admin.id,
        key_prefix=generated.prefix,
        key_hash=generated.key_hash,
        permissions=[Permission.ADMIN.value],
        expires_at=None,
        created_at=now,
    )
    # No ORM relationships are defined (by design), so the unit of work cannot
    # infer FK insert order across mappers — flush each dependency explicitly.
    session.add(tenant)
    await session.flush()
    session.add(admin)
    await session.flush()
    session.add(api_key)
    await session.flush()
    binding: IamPrincipalBinding | None = None
    if iam_binding is not None:
        # Every permission by name, not just ``admin``: the scope of the first
        # token is a ceiling (IAM-7), and a PAT issued for read+write would
        # narrow a bare ``admin`` binding down to nothing.
        binding = IamPrincipalBinding(
            id=new_uuid(),
            tenant_id=tenant.id,
            principal_id=admin.id,
            issuer=iam_binding.issuer,
            iam_tenant_id=iam_binding.iam_tenant_id,
            iam_principal_id=iam_binding.iam_principal_id,
            permissions=sorted(ALL_PERMISSIONS),
            status="active",
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
        await session.flush()
    # Every tenant needs the system workspace type before any workspace can be
    # created (ADR-0029); the migration does the same for pre-v0.5 tenants.
    await ensure_system_workspace_type(session, tenant.id)
    # Likewise the system work item type: it is what an unqualified task
    # creation resolves to (ADR-0048).
    await ensure_system_task_type(session, tenant.id, admin.id)
    # Retention state is per tenant (ADR-0038); create it up front so the
    # operator endpoints have something to report from day one.
    await session.execute(
        text(
            "INSERT INTO event_journal_floor (tenant_id, journal_tx_id, journal_sequence,"
            " archive_tx_id, archive_sequence, updated_at)"
            " VALUES (:tenant, 0, 0, 0, 0, now()) ON CONFLICT (tenant_id) DO NOTHING"
        ),
        {"tenant": tenant.id},
    )

    await record_event(
        session,
        tenant_id=tenant.id,
        event_type="tenant.bootstrapped",
        entity_type="tenant",
        entity_id=tenant.id,
        actor_id=admin.id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
        payload={
            "slug": tenant.slug,
            "adminPrincipalId": str(admin.id),
            "apiKeyId": str(api_key.id),
            "apiKeyPrefix": generated.prefix,
            "iamBindingId": str(binding.id) if binding is not None else None,
            "iamPrincipalId": str(binding.iam_principal_id) if binding is not None else None,
        },
    )
    if binding is not None:
        await record_event(
            session,
            tenant_id=tenant.id,
            event_type="iam_binding.created",
            entity_type="iam_binding",
            entity_id=binding.id,
            actor_id=admin.id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_run_id=trace_run_id,
            payload={
                "principalId": str(admin.id),
                "issuer": binding.issuer,
                "iamTenantId": str(binding.iam_tenant_id),
                "iamPrincipalId": str(binding.iam_principal_id),
                "permissions": binding.permissions,
                "visibility": binding.visibility or VISIBILITY_TENANT,
            },
        )
    return BootstrapResult(
        tenant=tenant, admin=admin, api_key=api_key, generated=generated, iam_binding=binding
    )
