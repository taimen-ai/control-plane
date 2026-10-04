"""``GET /package-settings``, ``GET /packages/{key}/settings`` and its history (CP-ADR-0081 §4).

The settings of a package are read under ``packages.settings.read``; the
answer says whether the caller may change them too (``canManage``,
``packages.settings.manage``). A package is installed when the tenant has an
object of it (``package_objects``) or a revision of its settings schema;
otherwise ``404 package_not_installed``. Installed without an active
revision — ``404 settings_not_declared``.

The schema and the layout come with the strings of the package dictionaries
in place of their keys, in the language asked (``?locale=``), its base
language, or the package's ``defaultLocale``; a key no dictionary has stays
as it is.

An agent and a skill read the settings of their own package without a
permission of their own (§8, :func:`object_settings`): ``GET /agents/me`` and
the claim of a skill invocation already hand them their description.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries.package_links import package_links
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError, ValidationError
from control_plane.domain.package_settings import (
    effective,
    lookup,
    not_declared,
    not_installed,
    present,
    prune,
    title_key,
)
from control_plane.domain.settings_refs import NONE, SettingsScope
from control_plane.domain.view_query import rfc3339
from control_plane.domain.views import choose_locale
from control_plane.infrastructure.db.models import (
    PackageDictionary,
    PackageSettings,
    PackageSettingsSchema,
    PackageSettingsVersion,
)
from control_plane.infrastructure.db.models import PackageObject as PackageRecord

ETAG_ENTITY = "package-settings"


# --- rows -------------------------------------------------------------------------------------


async def installed(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> bool:
    """An object of the package or a revision of its settings schema is in the tenant."""
    found = await db.scalar(
        select(
            or_(
                exists().where(
                    PackageRecord.tenant_id == tenant_id, PackageRecord.package_key == key
                ),
                exists().where(
                    PackageSettingsSchema.tenant_id == tenant_id,
                    PackageSettingsSchema.package_key == key,
                ),
            )
        )
    )
    return bool(found)


async def active_revision(
    db: AsyncSession, tenant_id: uuid.UUID, key: str, *, lock: str | None = None
) -> PackageSettingsSchema | None:
    """The active schema revision of ``key``; ``lock`` — ``share`` or ``update`` of its row."""
    stmt = select(PackageSettingsSchema).where(
        PackageSettingsSchema.tenant_id == tenant_id,
        PackageSettingsSchema.package_key == key,
        PackageSettingsSchema.active.is_(True),
    )
    if lock == "share":
        stmt = stmt.with_for_update(read=True)
    elif lock == "update":
        stmt = stmt.with_for_update()
    found: PackageSettingsSchema | None = await db.scalar(stmt)
    return found


async def saved_row(
    db: AsyncSession, tenant_id: uuid.UUID, key: str, *, lock: bool = False
) -> PackageSettings | None:
    stmt = select(PackageSettings).where(
        PackageSettings.tenant_id == tenant_id, PackageSettings.package_key == key
    )
    if lock:
        stmt = stmt.with_for_update()
    found: PackageSettings | None = await db.scalar(stmt)
    return found


async def require_declared(
    db: AsyncSession, tenant_id: uuid.UUID, key: str, *, lock: str | None = None
) -> PackageSettingsSchema:
    """The active revision of ``key``, or the ``404`` of §4."""
    revision = await active_revision(db, tenant_id, key, lock=lock)
    if revision is not None:
        return revision
    if not await installed(db, tenant_id, key):
        raise not_installed(key)
    raise not_declared(key)


@dataclass(frozen=True)
class Texts:
    """The dictionaries of a package: ``{locale: {key: text}}``, its languages and default."""

    messages: Mapping[str, Mapping[str, str]]
    locales: tuple[str, ...]
    default: str | None

    def lookup(self, wanted: str | None) -> Any:
        """Strings of ``wanted`` as views choose its locale (CP-ADR-0080 §5), then the default."""
        form = {"locales": list(self.locales), "defaultLocale": self.default}
        chain = [choose_locale(form, wanted), self.default or ""]
        return lookup(self.messages, [locale for locale in dict.fromkeys(chain) if locale])


async def texts(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> Texts:
    """The latest revision of the dictionaries of ``key``; none — no strings."""
    row = await db.scalar(
        select(PackageDictionary)
        .where(PackageDictionary.tenant_id == tenant_id, PackageDictionary.package_key == key)
        .order_by(PackageDictionary.revision.desc())
        .limit(1)
    )
    if row is None:
        return Texts({}, (), None)
    return Texts(row.messages, tuple(row.locales), row.default_locale)


async def can_manage(ctx: AuthContext) -> bool:
    try:
        await authorize(ctx, Permission.PACKAGES_SETTINGS_MANAGE)
    except AuthorizationError:
        return False
    return True


async def object_settings(
    db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str
) -> dict[str, Any] | None:
    """§8: ``{package, version, schemaRevision, values}`` of the package that installed
    ``key`` of ``kind``, read now; ``None`` — an object made by hand or a package
    without an active revision. Only that package: no other one is ever read.
    """
    link = (await package_links(db, tenant_id, kind, [key])).get(key)
    if link is None:
        return None
    revision = await active_revision(db, tenant_id, link.key)
    if revision is None:
        return None
    row = await saved_row(db, tenant_id, link.key)
    return {
        "package": link.key,
        "version": row.version if row is not None else 0,
        "schemaRevision": revision.revision,
        "values": effective(row.values if row is not None else {}, revision.schema),
    }


# --- the answers ------------------------------------------------------------------------------


def _who(row: PackageSettings | PackageSettingsVersion | None) -> dict[str, Any]:
    return {
        "updatedBy": str(row.updated_by) if row is not None else None,
        "updatedAt": rfc3339(row.updated_at) if row is not None else None,
    }


async def settings_out(
    db: AsyncSession,
    ctx: AuthContext,
    revision: PackageSettingsSchema,
    row: PackageSettings | None,
    locale: str | None,
) -> dict[str, Any]:
    """``PackageSettingsOut`` of the active ``revision`` and the saved ``row``."""
    key = revision.package_key
    found = await texts(db, ctx.tenant_id, key)
    text = found.lookup(locale)
    schema, uischema = present(key, revision.schema, revision.uischema, text)
    stored = row.values if row is not None else {}
    return {
        "package": key,
        "title": text(title_key(key)) or title_key(key),
        "packageVersion": revision.package_version,
        "schema": schema,
        "uischema": uischema,
        "values": prune(stored, revision.schema),
        "effective": effective(stored, revision.schema),
        "version": row.version if row is not None else 0,
        "schemaHash": revision.schema_hash,
        **_who(row),
        "canManage": await can_manage(ctx),
    }


async def get_settings(
    db: AsyncSession, ctx: AuthContext, key: str, locale: str | None
) -> dict[str, Any]:
    """``GET /packages/{key}/settings``."""
    await authorize(ctx, Permission.PACKAGES_SETTINGS_READ)
    revision = await require_declared(db, ctx.tenant_id, key)
    row = await saved_row(db, ctx.tenant_id, key)
    return await settings_out(db, ctx, revision, row, locale)


async def list_settings(
    db: AsyncSession, ctx: AuthContext, locale: str | None
) -> list[dict[str, Any]]:
    """``GET /package-settings``: the packages with an active revision, by key."""
    await authorize(ctx, Permission.PACKAGES_SETTINGS_READ)
    rows = await db.execute(
        select(PackageSettingsSchema, PackageSettings)
        .outerjoin(
            PackageSettings,
            (PackageSettings.tenant_id == PackageSettingsSchema.tenant_id)
            & (PackageSettings.package_key == PackageSettingsSchema.package_key),
        )
        .where(
            PackageSettingsSchema.tenant_id == ctx.tenant_id,
            PackageSettingsSchema.active.is_(True),
        )
        .order_by(PackageSettingsSchema.package_key)
    )
    items = []
    for revision, row in rows.tuples():
        key = revision.package_key
        text = (await texts(db, ctx.tenant_id, key)).lookup(locale)
        items.append(
            {
                "package": key,
                "title": text(title_key(key)) or title_key(key),
                "packageVersion": revision.package_version,
                "version": row.version if row is not None else 0,
                **_who(row),
            }
        )
    return items


async def list_versions(
    db: AsyncSession, ctx: AuthContext, key: str, *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """``GET /packages/{key}/settings/versions``: newest first, the cursor by version."""
    await authorize(ctx, Permission.PACKAGES_SETTINGS_READ)
    await require_declared(db, ctx.tenant_id, key)
    stmt = select(PackageSettingsVersion).where(
        PackageSettingsVersion.tenant_id == ctx.tenant_id,
        PackageSettingsVersion.package_key == key,
    )
    if cursor is not None:
        before = decode_cursor(cursor).get("v")
        if not isinstance(before, int) or isinstance(before, bool):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
        stmt = stmt.where(PackageSettingsVersion.version < before)
    rows = list(
        await db.scalars(stmt.order_by(PackageSettingsVersion.version.desc()).limit(limit + 1))
    )
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "version": row.version,
            "values": row.values,
            "changedPaths": list(row.changed_paths),
            **_who(row),
        }
        for row in rows
    ]
    next_cursor = encode_cursor({"v": rows[-1].version}) if more and rows else None
    return items, next_cursor


# --- the settings objects of a package read (CP-ADR-0081 §6) ----------------------------------


async def object_package(db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> str | None:
    """The package of the catalog object ``kind``/``key`` (``package_objects``); ``None`` — none."""
    found: str | None = await db.scalar(
        select(PackageRecord.package_key).where(
            PackageRecord.tenant_id == tenant_id,
            PackageRecord.kind == kind,
            PackageRecord.key == key,
        )
    )
    return found


async def package_scope(
    db: AsyncSession, tenant_id: uuid.UUID, package: str | None
) -> SettingsScope:
    """The type of ``settings`` of an object of ``package``: its active schema revision."""
    if package is None:
        return NONE
    revision = await active_revision(db, tenant_id, package)
    if revision is None:
        return SettingsScope(package)
    return SettingsScope(package, revision.schema, revision.revision)


async def object_scope(
    db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str
) -> SettingsScope:
    """:func:`package_scope` of the package of the object ``kind``/``key``."""
    return await package_scope(db, tenant_id, await object_package(db, tenant_id, kind, key))


@dataclass(frozen=True)
class Snapshot:
    """The settings one evaluation sees: the scope, the version of the values and the values."""

    scope: SettingsScope
    version: int
    values: dict[str, Any]

    @property
    def revision(self) -> int | None:
        return self.scope.revision

    def evidence(self) -> dict[str, Any]:
        """The element of the evidence of an evaluation that read them (§6)."""
        return {
            "kind": "settings",
            "package": self.scope.package,
            "version": self.version,
            "schemaRevision": self.scope.revision,
        }


async def snapshot(db: AsyncSession, tenant_id: uuid.UUID, scope: SettingsScope) -> Snapshot | None:
    """The effective values of the saved version over the defaults of ``scope``'s revision.

    ``None`` when the scope has no settings (no package, nothing declared).
    """
    if scope.package is None or scope.schema is None:
        return None
    row = await saved_row(db, tenant_id, scope.package)
    stored = row.values if row is not None else {}
    return Snapshot(scope, row.version if row is not None else 0, effective(stored, scope.schema))


@dataclass(frozen=True)
class History:
    """Saved versions and schema revisions of one package, as a replay asks them."""

    package: str | None
    versions: Mapping[int, Mapping[str, Any]]
    schemas: Mapping[int, Mapping[str, Any]]

    def scope(self, revision: int) -> SettingsScope | None:
        schema = self.schemas.get(revision)
        return SettingsScope(self.package, schema, revision) if schema is not None else None

    def values(self, version: int, revision: int) -> dict[str, Any] | None:
        """The effective values of the pair; ``None`` when either is not in the database."""
        schema = self.schemas.get(revision)
        stored = {} if version == 0 else self.versions.get(version)
        if schema is None or stored is None:
            return None
        return effective(stored, schema)


async def history(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    package: str | None,
    pairs: Iterable[tuple[int, int]],
) -> History:
    """The versions and revisions the pairs ``(version, revision)`` name, of ``package``."""
    wanted = list(pairs)
    if package is None or not wanted:
        return History(package, {}, {})
    numbers = sorted({version for version, _ in wanted if version > 0})
    revisions = sorted({revision for _, revision in wanted})
    versions: dict[int, Mapping[str, Any]] = {}
    if numbers:
        rows = await db.scalars(
            select(PackageSettingsVersion).where(
                PackageSettingsVersion.tenant_id == tenant_id,
                PackageSettingsVersion.package_key == package,
                PackageSettingsVersion.version.in_(numbers),
            )
        )
        versions = {row.version: row.values for row in rows}
    found = await db.scalars(
        select(PackageSettingsSchema).where(
            PackageSettingsSchema.tenant_id == tenant_id,
            PackageSettingsSchema.package_key == package,
            PackageSettingsSchema.revision.in_(revisions),
        )
    )
    return History(package, versions, {row.revision: row.schema for row in found})
