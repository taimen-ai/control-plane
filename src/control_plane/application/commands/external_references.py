"""External reference commands, generic over the entity being mapped.

The storage was always generic (ADR-0034); what was project-shaped was the
command and the permission check. Both now go through the entity binding
registry (ADR-0047), so a project is one case of the same code path rather than
the only one it knows.

The mapping is never a source of truth: Control Plane does not write to the
external system and does not treat its state as authoritative.
"""

import uuid
from typing import Any

from sqlalchemy import literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.external_entities import (
    ENTITY_BINDINGS,
    EntityBinding,
    resolve_entity_binding,
    resolve_entity_id,
)
from control_plane.domain.errors import ConflictError, ValidationError
from control_plane.domain.project import guard_json_document, validate_config_document
from control_plane.infrastructure.db.models import ExternalReference

MAX_EXTERNAL_ID_LENGTH = 512


def _validate_external_key(external_system: str, external_type: str, external_id: str) -> None:
    for name, value in (
        ("externalSystem", external_system),
        ("externalType", external_type),
        ("externalId", external_id),
    ):
        if not value or len(value) > MAX_EXTERNAL_ID_LENGTH:
            raise ValidationError(
                "invalid_external_reference",
                f"{name} must be 1..{MAX_EXTERNAL_ID_LENGTH} characters",
                details={"field": name},
            )


def _validate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = metadata or {}
    # Bound the document BEFORE walking it: the secret scan is recursive.
    guard_json_document(payload, label="metadata")
    validate_config_document({"settings": payload}, field_name="metadata")
    return payload


async def register_external_reference(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    entity_type: str,
    entity_ref: str,
    external_system: str,
    external_type: str,
    external_id: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[ExternalReference, bool]:
    """Map an external identifier onto an internal entity. Returns (row, created).

    Idempotent by external key: re-registering the same mapping with the same
    metadata is a no-op that neither writes a row nor bumps the version.
    """
    binding = resolve_entity_binding(entity_type)
    await authorize(ctx, binding.manage_permission)
    entity_id = await resolve_entity_id(session, ctx, binding, entity_ref)
    _validate_external_key(external_system, external_type, external_id)
    payload = _validate_metadata(metadata)

    existing = await session.scalar(
        select(ExternalReference)
        .where(
            ExternalReference.tenant_id == ctx.tenant_id,
            ExternalReference.external_system == external_system,
            ExternalReference.external_type == external_type,
            ExternalReference.external_id == external_id,
        )
        .with_for_update()
    )
    now = utcnow()
    if existing is not None:
        # Identity is the PAIR: the same uuid under a different entity type is a
        # different entity, not the same one seen from another angle.
        if existing.entity_type != binding.entity_type or existing.entity_id != entity_id:
            # The other entity is named only when the caller sees it
            # (CP-ADR-0082 §4): the conflict itself stays, the key is taken.
            seen = ctx.visible_workspaces is None or await session.scalar(
                select(
                    ENTITY_BINDINGS[existing.entity_type].visible(ctx, literal(existing.entity_id))
                )
            )
            raise ConflictError(
                "external_reference_conflict",
                "This external identifier already maps to a different entity",
                details=(
                    {"entityType": existing.entity_type, "entityId": str(existing.entity_id)}
                    if seen
                    else {}
                ),
            )
        if existing.metadata_json == payload:
            return existing, False
        existing.metadata_json = payload
        existing.version += 1
        existing.updated_at = now
        await _record(
            session,
            ctx,
            binding=binding,
            entity_id=entity_id,
            reference_id=existing.id,
            external_system=external_system,
            external_type=external_type,
            external_id=external_id,
            action="updated",
        )
        return existing, False

    reference = ExternalReference(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        entity_type=binding.entity_type,
        entity_id=entity_id,
        external_system=external_system,
        external_type=external_type,
        external_id=external_id,
        metadata_json=payload,
        version=1,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    try:
        # SAVEPOINT: a losing racer must get a 409, not a dead transaction.
        async with session.begin_nested():
            session.add(reference)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "external_reference_conflict",
            "This external identifier is already mapped",
            details={
                "externalSystem": external_system,
                "externalType": external_type,
                "externalId": external_id,
            },
        ) from exc

    await _record(
        session,
        ctx,
        binding=binding,
        entity_id=entity_id,
        reference_id=reference.id,
        external_system=external_system,
        external_type=external_type,
        external_id=external_id,
        action="added",
    )
    return reference, True


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    binding: EntityBinding,
    entity_id: uuid.UUID,
    reference_id: uuid.UUID,
    external_system: str,
    external_type: str,
    external_id: str,
    action: str,
) -> None:
    """Emit ``<entityType>.external_reference_<action>`` against the target entity.

    Keeping the event on the entity's own stream is what lets an existing
    subscriber keep working: for projects this is byte-for-byte the event type
    it already receives. ``metadata`` is deliberately absent — it is a
    client-shaped document, and the event stream travels further than the row.
    """
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=f"{binding.entity_type}.external_reference_{action}",
        entity_type=binding.entity_type,
        entity_id=entity_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "externalReferenceId": str(reference_id),
            "externalSystem": external_system,
            "externalType": external_type,
            "externalId": external_id,
        },
    )
