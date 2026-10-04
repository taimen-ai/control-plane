"""Settings of a package: ``PUT /packages/{key}/settings`` and the ``settings`` of a plan.

CP-ADR-0081. **Saving** (:func:`put_settings`, ``packages.settings.manage``). The body is
the saved values whole; the checks go from the cheap to the meaningful and
the first one that fires answers (§4): the package installed and declaring
settings (``404 package_not_installed`` / ``settings_not_declared``), no
credential material in any string, member names included (``422
secret_material_rejected`` with paths only — the check runs before the
schema, so a secret reaches neither the database, nor the journal, nor an echo
of the schema), the schema of the active revision (``422 settings_invalid``,
every violation), the ``x-ref`` strings naming objects the organization has in
use (``422 unknown_ref``), then ``If-Match`` against the version under the
lock of the row (``409 version_conflict``). Values equal to the saved ones
change nothing; others are a new version, a row of history and
``package.settings_changed`` (no values in it) in one transaction.

**Locks.** A ``PUT`` holds the active schema revision ``FOR SHARE`` and then
the row of ``package_settings`` ``FOR UPDATE``; the first saving inserts the
row with ``ON CONFLICT DO NOTHING``, so of two first savings with
``"package-settings-0"`` one passes and the other is ``409``. An apply locks
the same two rows ``FOR UPDATE`` in the same order (:func:`plan_settings`
with ``lock``): a value saved while an apply checks it waits for the apply,
and the apply waits for a saving in progress.

**Plan** (§7, :func:`plan_settings`). ``spec.settings`` of the package
against the active revision: the revision after the apply, the fields added
and removed, the saved values the new schema refuses — each also a finding
``settings_incompatible`` of the plan, so the apply is refused — and whether
the layout changed. The active revision and the version of the values are in
the catalog etag: a value saved between plan and apply is ``409 plan_stale``.
**Apply** (:func:`record_revision`) writes the new revision active and the old
one inactive; the values, their version and history stay as they are.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.catalog_retirements import CALENDAR, retired_keys
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.package_settings import (
    active_revision,
    require_declared,
    saved_row,
    settings_out,
)
from control_plane.domain.enums import Permission, TaskTypeStatus
from control_plane.domain.errors import ConflictError, ValidationError
from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.package_settings import (
    Comparison,
    Declaration,
    Declared,
    Reference,
    changed_paths,
    compare,
    parse_uuid,
    references,
    schema_pointer,
    validate,
)
from control_plane.domain.package_source import PackageObject
from control_plane.domain.process_definition import Problem
from control_plane.domain.project import secret_findings
from control_plane.infrastructure.db.models import (
    CalendarVersion,
    PackageSettings,
    PackageSettingsSchema,
    PackageSettingsVersion,
    Principal,
    Role,
    TaskType,
    Workspace,
)

EVENT = "package.settings_changed"
ENTITY_TYPE = "package"
# The catalog etag of a plan names the settings of its package by this kind.
ETAG_KIND = "PackageSettings"


def package_entity_id(key: str) -> uuid.UUID:
    """The id events about a package carry: a package has no row of its own."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"package:{key}")


# --- references -------------------------------------------------------------------------------


async def unknown_refs(
    db: AsyncSession, tenant_id: uuid.UUID, refs: Sequence[Reference]
) -> list[dict[str, Any]]:
    """``{path, ref}`` of every reference to an object the organization has not, or not in use."""
    known: dict[str, set[str]] = {}
    by_kind: dict[str, set[str]] = {}
    for ref in refs:
        by_kind.setdefault(ref.kind, set()).add(ref.value)
    for kind, values in by_kind.items():
        known[kind] = await _known(db, tenant_id, kind, sorted(values))
    return [{"path": ref.path, "ref": ref.kind} for ref in refs if ref.value not in known[ref.kind]]


async def _known(db: AsyncSession, tenant_id: uuid.UUID, kind: str, values: list[str]) -> set[str]:
    if kind in ("role", "principal", "workspace"):
        ids = {value: parse_uuid(value) for value in values}
        wanted = [found for found in ids.values() if found is not None]
        if not wanted:
            return set()
        if kind == "role":
            stmt = select(Role.id).where(Role.tenant_id == tenant_id, Role.id.in_(wanted))
        elif kind == "principal":
            stmt = select(Principal.id).where(
                Principal.tenant_id == tenant_id,
                Principal.id.in_(wanted),
                Principal.status != "disabled",
            )
        else:
            stmt = select(Workspace.id).where(
                Workspace.tenant_id == tenant_id,
                Workspace.id.in_(wanted),
                Workspace.status == "active",
            )
        present = set(await db.scalars(stmt))
        return {value for value, found in ids.items() if found in present}
    if kind == "taskType":
        return set(
            await db.scalars(
                select(TaskType.key).where(
                    TaskType.tenant_id == tenant_id,
                    TaskType.key.in_(values),
                    TaskType.status == TaskTypeStatus.ACTIVE,
                )
            )
        )
    calendars = set(
        await db.scalars(
            select(CalendarVersion.key)
            .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key.in_(values))
            .distinct()
        )
    )
    return calendars - set(await retired_keys(db, tenant_id, CALENDAR, sorted(calendars)))


# --- saving -----------------------------------------------------------------------------------


def _conflict(key: str, expected: int, current: int) -> ConflictError:
    return ConflictError(
        "version_conflict",
        "The settings of the package changed since they were read: If-Match is stale",
        details={"package": key, "expectedVersion": expected, "currentVersion": current},
    )


async def put_settings(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    expected_version: int,
    values: dict[str, Any],
    locale: str | None = None,
) -> dict[str, Any]:
    """``PUT /packages/{key}/settings``: ``PackageSettingsOut`` of the state after it."""
    await authorize(ctx, Permission.PACKAGES_SETTINGS_MANAGE)
    revision = await require_declared(db, ctx.tenant_id, key, lock="share")
    found = secret_findings(values)
    if found:
        raise ValidationError(
            "secret_material_rejected",
            "Settings hold no secrets: a secret belongs to a connection or a named secret"
            " of an agent",
            details={"errors": found},
        )
    errors = validate(values, revision.schema)
    if errors:
        raise ValidationError(
            "settings_invalid",
            f"The values do not match the settings schema ({len(errors)} violations)",
            details={"errors": errors},
        )
    missing = await unknown_refs(db, ctx.tenant_id, references(values, revision.schema))
    if missing:
        raise ValidationError(
            "unknown_ref",
            "A value references an object the organization does not have in use",
            details={"errors": missing},
        )
    row = await saved_row(db, ctx.tenant_id, key, lock=True)
    current = row.version if row is not None else 0
    if current != expected_version:
        raise _conflict(key, expected_version, current)
    before: dict[str, Any] = row.values if row is not None else {}
    if canonical_hash(before) == canonical_hash(values):
        return await settings_out(db, ctx, revision, row, locale)
    now = utcnow()
    if row is None:
        inserted = await db.execute(
            insert(PackageSettings)
            .values(
                tenant_id=ctx.tenant_id,
                package_key=key,
                values=values,
                version=1,
                schema_revision=revision.revision,
                updated_by=ctx.principal_id,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "package_key"])
            .returning(PackageSettings.version)
        )
        if inserted.first() is None:
            # Another first saving committed while this one waited on the key.
            other = await saved_row(db, ctx.tenant_id, key)
            raise _conflict(key, expected_version, other.version if other is not None else 0)
        row = await saved_row(db, ctx.tenant_id, key)
        assert row is not None
    else:
        row.values = values
        row.version += 1
        row.schema_revision = revision.revision
        row.updated_by = ctx.principal_id
        row.updated_at = now
    paths = changed_paths(before, values)
    db.add(
        PackageSettingsVersion(
            tenant_id=ctx.tenant_id,
            package_key=key,
            version=row.version,
            values=values,
            schema_revision=revision.revision,
            changed_paths=paths,
            updated_by=ctx.principal_id,
            updated_at=now,
        )
    )
    await db.flush()
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type=EVENT,
        entity_type=ENTITY_TYPE,
        entity_id=package_entity_id(key),
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "package": key,
            "version": row.version,
            "previousVersion": current,
            "schemaRevision": revision.revision,
            "changedPaths": paths,
            "actorId": str(ctx.principal_id),
        },
    )
    return await settings_out(db, ctx, revision, row, locale)


# --- the plan and the apply -------------------------------------------------------------------


@dataclass
class SettingsPlan:
    """The ``settings`` section of a plan: the active revision and the declaration."""

    package_key: str
    before: PackageSettingsSchema | None
    declared: Declared | None
    after: int | None
    comparison: Comparison
    uischema_changed: bool
    # Read while the plan's transaction is open: the plan is shown after its rollback.
    before_revision: int | None = None

    def out(self) -> dict[str, Any]:
        return {
            "schemaRevision": {"before": self.before_revision, "after": self.after},
            "added": self.comparison.added,
            "removed": self.comparison.removed,
            "incompatible": self.comparison.incompatible,
            "uischemaChanged": self.uischema_changed,
        }


async def etag_entry(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None:
    """What the catalog etag knows of the settings of ``key``: revision and version."""
    revision = await active_revision(db, tenant_id, key)
    row = await saved_row(db, tenant_id, key)
    if revision is None and row is None:
        return None
    return {
        "kind": ETAG_KIND,
        "key": key,
        "revision": revision.revision if revision is not None else None,
        "hash": revision.schema_hash if revision is not None else None,
        "version": row.version if row is not None else 0,
    }


async def plan_settings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    manifest: PackageObject | None,
    declaration: Declaration,
    *,
    lock: bool = False,
) -> tuple[SettingsPlan | None, list[Problem]]:
    """The ``settings`` section of the plan of ``manifest``'s package and its findings."""
    if manifest is None:
        return None, []
    key = manifest.key
    before = await active_revision(db, tenant_id, key, lock="update" if lock else None)
    row = await saved_row(db, tenant_id, key, lock=lock)
    if declaration.present and declaration.declared is None:
        return None, []  # the declaration has errors: the plan says them
    declared = declaration.declared
    if declared is None and before is None:
        return None, []
    after: int | None = None
    if declared is not None and (before is None or before.schema_hash != declared.hash):
        latest = await db.scalar(
            select(func.max(PackageSettingsSchema.revision)).where(
                PackageSettingsSchema.tenant_id == tenant_id,
                PackageSettingsSchema.package_key == key,
            )
        )
        after = int(latest or 0) + 1
    comparison = compare(
        before.schema if before is not None else None,
        declared.schema if declared is not None else None,
        row.values if row is not None else {},
    )
    changed = declared is not None and canonical_hash(
        before.uischema if before is not None else None
    ) != canonical_hash(declared.uischema)
    problems = [
        manifest.place(
            Problem(
                "settings_incompatible",
                "error",
                schema_pointer(item["path"]),
                f"the saved value at {item['path']} does not pass {item['code']} of the new"
                " settings schema",
                hint=f"change the value with PUT /api/v1/packages/{key}/settings, then plan again",
            )
        )
        for item in comparison.incompatible
    ]
    section = SettingsPlan(
        key,
        before,
        declared,
        after,
        comparison,
        changed,
        before_revision=before.revision if before is not None else None,
    )
    return section, problems


async def record_revision(
    db: AsyncSession,
    ctx: AuthContext,
    plan: SettingsPlan,
    *,
    package_version: str | None,
    plan_hash: str,
) -> None:
    """The apply of a plan: the new revision active, the one before inactive."""
    if plan.after is None and plan.declared is not None:
        return  # the same schema: the revision stays
    if plan.before is not None:
        plan.before.active = False
        await db.flush()
    if plan.declared is None or plan.after is None:
        return
    db.add(
        PackageSettingsSchema(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            package_key=plan.package_key,
            revision=plan.after,
            package_version=package_version,
            schema=plan.declared.schema,
            uischema=plan.declared.uischema,
            schema_hash=plan.declared.hash,
            active=True,
            plan_hash=plan_hash,
            applied_by=ctx.principal_id,
            applied_at=utcnow(),
        )
    )
    await db.flush()
