"""External reference read side: forward lookup by entity, reverse by key.

Both directions stay inside the caller's tenant by construction (ADR-0034) and
are authorized by the entity type they touch rather than by the project that
happens to own it (ADR-0047).
"""

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.external_entities import (
    ENTITY_BINDINGS,
    resolve_entity_binding,
    resolve_entity_id,
)
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import ExternalReference


async def list_entity_external_references(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    entity_type: str,
    entity_ref: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[ExternalReference]:
    """Forward lookup: every external key mapped onto one internal entity."""
    binding = resolve_entity_binding(entity_type)
    await authorize(ctx, binding.read_permission)
    entity_id = await resolve_entity_id(session, ctx, binding, entity_ref)
    return await _page(
        session,
        select(ExternalReference).where(
            ExternalReference.tenant_id == ctx.tenant_id,
            ExternalReference.entity_type == binding.entity_type,
            ExternalReference.entity_id == entity_id,
        ),
        limit=limit,
        cursor=cursor,
    )


async def lookup_by_external_key(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    external_system: str,
    external_type: str | None,
    external_id: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[ExternalReference]:
    """Reverse lookup: which internal entity an external key points at.

    A hit is filtered by the read permission of the entity type it resolves to,
    and a caller without that permission gets an empty page rather than a
    ``403``. Distinguishing "no such key" from "not yours" would answer, for any
    external identifier, whether it exists in this tenant — an enumeration
    oracle over someone else's ticket ids (the single-answer rule of ADR-0045).
    """
    readable = [
        entity_type
        for entity_type, binding in ENTITY_BINDINGS.items()
        if ctx.has(binding.read_permission)
    ]
    if not readable:
        return Page(items=[], next_cursor=None)

    stmt = select(ExternalReference).where(
        ExternalReference.tenant_id == ctx.tenant_id,
        ExternalReference.external_system == external_system,
        ExternalReference.external_id == external_id,
        ExternalReference.entity_type.in_(readable),
    )
    if external_type is not None:
        stmt = stmt.where(ExternalReference.external_type == external_type)
    if ctx.visible_workspaces is not None:
        # An entity outside the caller's visibility is not found by its key
        # either: the same empty page as for an unknown key (CP-ADR-0082 §4).
        stmt = stmt.where(
            or_(
                *(
                    (ExternalReference.entity_type == entity_type)
                    & ENTITY_BINDINGS[entity_type].visible(ctx, ExternalReference.entity_id)
                    for entity_type in readable
                )
            )
        )
    return await _page(session, stmt, limit=limit, cursor=cursor)


def validate_lookup_arguments(
    *,
    entity_type: str | None,
    entity_id: str | None,
    external_system: str | None,
    external_type: str | None,
    external_id: str | None,
) -> None:
    """Reject ambiguous and half-specified lookups before touching the database.

    A half-specified reverse lookup would silently degrade into "return
    everything", which is exactly the failure the project filter already
    guards against.
    """
    forward = entity_type is not None or entity_id is not None
    reverse = external_system is not None or external_type is not None or external_id is not None
    if forward and reverse:
        raise ValidationError(
            "invalid_external_lookup",
            "Filter by entity or by external key, not both",
            details={"conflicting": ["entityType", "externalSystem"]},
        )
    if not forward and not reverse:
        raise ValidationError(
            "invalid_external_lookup",
            "Provide either entityType + entityId, or externalSystem + externalId",
            details={"required": ["entityType", "entityId"]},
        )
    if forward and (entity_type is None or entity_id is None):
        raise ValidationError(
            "invalid_external_lookup",
            "entityType and entityId are required together",
            details={"required": ["entityType", "entityId"]},
        )
    if reverse and (external_system is None or external_id is None):
        raise ValidationError(
            "invalid_external_lookup",
            "externalSystem and externalId are required together",
            details={"required": ["externalSystem", "externalId"]},
        )


async def _page(
    session: AsyncSession,
    stmt: Select[tuple[ExternalReference]],
    *,
    limit: int | None,
    cursor: str | None,
) -> Page[ExternalReference]:
    effective_limit = clamp_limit(limit)
    if cursor is not None:
        created_at, reference_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (ExternalReference.created_at < created_at)
            | ((ExternalReference.created_at == created_at) & (ExternalReference.id < reference_id))
        )
    stmt = stmt.order_by(ExternalReference.created_at.desc(), ExternalReference.id.desc()).limit(
        effective_limit + 1
    )
    rows = list((await session.scalars(stmt)).all())
    next_cursor: str | None = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)
