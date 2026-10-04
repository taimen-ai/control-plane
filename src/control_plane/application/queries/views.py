"""``GET /views`` and ``GET /views/{key}``: the views a caller may see, in a language (CP-ADR-0080).

There is no right of its own to read a view: a view describes a screen over
its source, and narrows what the source shows, never widens it. The caller
sees a view in use when

- **audience** — the view names no roles, or the caller holds one of them:
  a role of the organization (``principal_roles``), tenant-wide or in any
  workspace;
- **source** — the caller may read it: ``processes.read`` (on the workspace
  of a process bound to one), ``tasks.read`` for tasks, ``events.read`` for
  knowledge, as the routes of the source decide them.

A view the caller may not see, a retired one and a missing one are the same
``404``. The strings come in the language asked for (``?locale=``), its base
language, or the package's ``defaultLocale``.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, permits
from control_plane.application.commands.process_definitions import process_scope
from control_plane.application.commands.views import ACTIVE
from control_plane.application.queries.package_links import in_package
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError
from control_plane.domain.views import BLOCK_SET, choose_locale, present
from control_plane.infrastructure.db.models import (
    PrincipalRole,
    ProcessDefinition,
    Role,
    View,
    ViewRevision,
)

VIEW = "View"
# Views read per round of a list: the visible ones are taken from each round.
_BATCH = 200
_SOURCE_RIGHTS = {
    "process": Permission.PROCESSES_READ,
    "tasks": Permission.TASKS_READ,
    "knowledge": Permission.EVENTS_READ,
}


@dataclass(frozen=True)
class ViewRead:
    """A view in use with its current revision."""

    view: View
    revision: ViewRevision

    def out(self, locale: str | None, *, layout: bool = True) -> dict[str, Any]:
        """The view as a console draws it (CP-ADR-0080 §9): a summary, with ``layout`` the whole."""
        form = self.revision.spec
        chosen = choose_locale(form, locale)
        shown = present(form, chosen)
        body: dict[str, Any] = {
            "key": self.view.key,
            "title": shown["title"],
            "revision": self.revision.revision,
            "hash": self.revision.hash,
            "blocks": form.get("blocks", BLOCK_SET),
            "locale": chosen,
            "nav": shown.get("nav"),
            "source": shown["source"],
        }
        if "description" in shown:
            body["description"] = shown["description"]
        if layout:
            body["layout"] = shown["layout"]
        return body


class _Visibility:
    """What the caller may see, asked once per request: roles and the rights of sources."""

    def __init__(self, db: AsyncSession, ctx: AuthContext) -> None:
        self.db = db
        self.ctx = ctx
        self._roles: set[str] | None = None
        self._rights: dict[tuple[str, str | None], bool] = {}

    async def roles(self) -> set[str]:
        if self._roles is None:
            self._roles = set(
                await self.db.scalars(
                    select(Role.slug)
                    .join(PrincipalRole, PrincipalRole.role_id == Role.id)
                    .where(
                        PrincipalRole.tenant_id == self.ctx.tenant_id,
                        PrincipalRole.principal_id == self.ctx.principal_id,
                    )
                )
            )
        return self._roles

    async def _may(self, permission: Permission, workspace_id: uuid.UUID | None = None) -> bool:
        cached = (permission.value, str(workspace_id) if workspace_id else None)
        if cached not in self._rights:
            self._rights[cached] = await permits(
                self.ctx, permission, resource=process_scope(workspace_id)
            )
        return self._rights[cached]

    async def source(self, revision: ViewRevision) -> bool:
        permission = _SOURCE_RIGHTS[revision.source_kind]
        if not await self._may(permission):
            return False
        if revision.source_kind != "process" or revision.source_key is None:
            return True
        workspace = await self.db.scalar(
            select(ProcessDefinition.workspace_id)
            .where(
                ProcessDefinition.tenant_id == self.ctx.tenant_id,
                ProcessDefinition.key == revision.source_key,
            )
            .order_by(ProcessDefinition.version.desc())
            .limit(1)
        )
        return workspace is None or await self._may(permission, workspace)

    async def sees(self, revision: ViewRevision) -> bool:
        audience = revision.audience_roles
        if audience and not (set(audience) & await self.roles()):
            return False
        return await self.source(revision)


def _current() -> Any:
    return select(View, ViewRevision).join(
        ViewRevision,
        (ViewRevision.view_id == View.id) & (ViewRevision.revision == View.current_revision),
    )


async def list_views(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int,
    after_key: str | None,
    package: str | None = None,
) -> tuple[list[ViewRead], str | None]:
    """The views in use the caller sees, by key; the key to continue after, if more."""
    visibility = _Visibility(db, ctx)
    found: list[ViewRead] = []
    cursor = after_key
    while len(found) <= limit:
        stmt = _current().where(View.tenant_id == ctx.tenant_id, View.status == ACTIVE)
        if cursor is not None:
            stmt = stmt.where(View.key > cursor)
        if package is not None:
            stmt = stmt.where(in_package(VIEW, View.tenant_id, View.key, package))
        rows = list((await db.execute(stmt.order_by(View.key).limit(_BATCH))).tuples())
        for view, revision in rows:
            if await visibility.sees(revision):
                found.append(ViewRead(view, revision))
                if len(found) > limit:
                    break
        if len(rows) < _BATCH:
            break
        cursor = rows[-1][0].key
    if len(found) > limit:
        return found[:limit], found[limit - 1].view.key
    return found, None


async def get_view(db: AsyncSession, ctx: AuthContext, key: str) -> ViewRead:
    """One view the caller sees; invisible, retired and missing are the same 404."""
    row = (
        await db.execute(
            _current().where(
                View.tenant_id == ctx.tenant_id, View.key == key, View.status == ACTIVE
            )
        )
    ).first()
    if row is None or not await _Visibility(db, ctx).sees(row[1]):
        raise NotFoundError("View not found", details={"key": key[:200]})
    return ViewRead(row[0], row[1])
