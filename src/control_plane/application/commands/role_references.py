"""``role:<slug>`` as the addressee of a gate (CP-ADR-0061, amendment 2026-10-01).

A gate a task type or a rule opens may be addressed to a role by its slug
instead of by the id of one installation's role, so a package addresses it
to the role it declares itself: ``requestApproval.assignee`` of the type's
``approvalSchema``/``completionSchema``, ``approverRole`` of its acceptance
checks and of a rule's ``request_decision``.

Two moments, like ``agent:<key>`` (CP-ADR-0073 A1):

- **publication** — a literal reference must name a role the tenant has (in
  any workspace): a type or a rule is not published with a gate nobody could
  ever be asked to decide (``422 unknown_role``, ``details: {field, role}``);
  a template is only known when it renders and is checked then;
- **when the gate is opened** — the slug is resolved in the scope of the task
  the gate holds: the role of its workspace or the nearest ancestor before a
  tenant-wide one, as a role requirement of a task is. The approval stores
  the role's id (``requiredRoleId``); no role there is ``unknown_role`` too.
"""

import re
import uuid
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.eligibility import resolve_role
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.domain.errors import ValidationError
from control_plane.domain.work_graph import role_reference_slug
from control_plane.infrastructure.db.models import Role

ROLE_REFERENCE_PREFIX = "role:"


def is_role_reference(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and value.startswith(ROLE_REFERENCE_PREFIX)


def _unknown_role(field: str, slug: str) -> ValidationError:
    return ValidationError(
        "unknown_role",
        f"{field}: no role {slug[:100]!r} to address the decision to",
        details={"field": field, "role": slug[:100]},
    )


# The slug of a role as the API takes it (``RoleCreateRequest.slug``).
_EXECUTOR_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
MAX_EXECUTOR_ROLES = 20


def normalize_executor_roles(items: Any, *, field: str = "executorRoles") -> list[str]:
    """``executorRoles`` of a task type (CP-ADR-0048, amendment 2026-10-03 A1): a list
    of unique role slugs, at most ``MAX_EXECUTOR_ROLES``; anything else is
    ``422 invalid_executor_roles`` with the path of the offending item."""
    if not isinstance(items, list):
        raise ValidationError(
            "invalid_executor_roles", f"{field} must be a list", details={"field": field}
        )
    if len(items) > MAX_EXECUTOR_ROLES:
        raise ValidationError(
            "invalid_executor_roles",
            f"{field} exceeds {MAX_EXECUTOR_ROLES} roles",
            details={"field": field, "maxItems": MAX_EXECUTOR_ROLES},
        )
    seen: set[str] = set()
    for index, item in enumerate(items):
        path = f"{field}[{index}]"
        if not isinstance(item, str) or not _EXECUTOR_ROLE_RE.fullmatch(item):
            raise ValidationError(
                "invalid_executor_roles",
                f"{path} must be a role slug",
                details={"field": path},
            )
        if item in seen:
            raise ValidationError(
                "invalid_executor_roles",
                f"{path}: role {item!r} is listed twice",
                details={"field": path},
            )
        seen.add(item)
    return list(items)


def role_slug(reference: str, *, field: str) -> str:
    """The slug of ``role:<slug>``; a malformed one names no role.

    Read as ``work_graph`` reads it at publication, nothing trimmed:
    ``role: buyer`` is not ``role:buyer``.
    """
    slug = role_reference_slug(reference)
    if slug is None:
        raise _unknown_role(field, reference.removeprefix(ROLE_REFERENCE_PREFIX))
    return slug


async def require_declared_role(
    session: AsyncSession, tenant_id: uuid.UUID, reference: str, *, field: str
) -> None:
    """Publication: the tenant has a role with the slug, in some workspace or tenant-wide."""
    slug = role_slug(reference, field=field)
    found = await session.scalar(
        select(Role.id).where(Role.tenant_id == tenant_id, Role.slug == slug).limit(1)
    )
    if found is None:
        raise _unknown_role(field, slug)


async def role_for_task(
    session: AsyncSession,
    ctx: AuthContext,
    reference: str,
    *,
    workspace_id: uuid.UUID | None,
    field: str,
) -> uuid.UUID:
    """The id of the role ``reference`` names, seen from the task's workspace."""
    slug = role_slug(reference, field=field)
    scope = (
        await workspace_ancestor_ids(session, ctx.tenant_id, workspace_id)
        if workspace_id is not None
        else []
    )
    try:
        role = await resolve_role(session, ctx, slug, scope)
    except ValidationError as exc:
        raise _unknown_role(field, slug) from exc
    return role.id
