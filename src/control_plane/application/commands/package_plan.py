"""Package plan and apply by hash: ``POST /packages:plan``, ``POST /packages:apply``.

CP-ADR-0074 §11, process-packages P015, amendment 2026-09-29. The body is
the package as its files, as for ``packages:test``; the core plans every kind
of the catalog it holds — ``TaskType``, ``Agent``, ``Calendar``, ``Process``,
``WorkRule``, ``View`` (:data:`package_plan.PLANNED_KINDS`, in that order; a
view as its checked form, its components inlined — CP-ADR-0080,
:mod:`control_plane.application.commands.views`); the objects
of other kinds are listed in ``outside`` with who applies them (the installer,
the notification service).

**Plan** (:func:`plan_package`, a transaction that is rolled back; memory is
asked after it closes):

- ``changes`` — per object ``create | update | rename | restore | unchanged``
  and the fields it changes, each with its owner: ``console`` when a person changed
  it since the last apply (``package_objects`` keeps what that apply wanted),
  kept unless ``overwriteConsole`` (:func:`package_plan.diff_fields`); a task
  type also names the active versions the apply deprecates (``deprecates``).
  Task types, agents and rules are compared as their form
  (:mod:`control_plane.application.commands.package_catalog`), and the
  command of their kind is run on what the apply would publish, in a
  savepoint of the rolled-back transaction: what it refuses is a finding;
- ``processes`` — per changed process the replay of the new version on the
  latest ``replayLimit`` instances of the current one, and the fate of open
  instances by version: ``pin`` or ``migrate`` by the version's
  ``migrations``, ``unaffected`` without one; ``migrationRequired`` when an
  element they stand on is gone and no migration carries them — a problem
  ``migration_required`` of the plan;
- ``deadlines`` of each changed process — the open instances it migrates
  whose deadlines the engine's step ``migrated`` sets, moves, lifts or finds
  already past by the new version: the step taken on a copy of the migrated
  state, as the apply takes it, nothing written (:func:`_deadlines`, FR-023);
- ``regulationCoverage`` — the sections of each regulation the processes name
  (``governedBy``), as memory holds them, with the elements governed by each
  and the sections no element covers (FR-058);
- ``catalogEtag`` and ``planHash`` (:mod:`control_plane.domain.package_plan`).

**Apply** (:func:`apply_package`) builds the plan again from the same files in
its own transaction, under a lock of the tenant's applies, with the keys of
its task types, agents and rules locked as their commands lock them
(:func:`package_catalog.lock_keys`), then those of its processes and
calendars, and the open instances locked, and
refuses ``409 plan_stale`` when its hash differs from the one shown: the
catalog or the instances changed since. A plan with
``migration_required`` is ``422 migration_required``, any other error ``422
invalid_package``. Then, in one transaction, every object is published by the
ordinary command of its kind, under the right of that kind, in the order of
``PLANNED_KINDS`` (a refusal of a command rolls the whole apply back); open
instances with ``migrate`` move to the new version by the map
(:func:`control_plane.domain.process_migration.migrate_state`) — a journal
entry ``migrate`` with the migrated state and ``process.migrated`` each, then
the engine's step ``migrated`` that counts the instance's deadlines by the new
version (CP-ADR-0074 §11, amendment 2026-09-29); a key renamed away is
retired; ``package_objects`` records what the apply wanted and the package
(key, version, plan hash) of every object of the plan; a retired process or
calendar the package installs as it is (``restore``) is back in use
(CP-ADR-0074, amendment Zh3).
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands import package_catalog, package_links
from control_plane.application.commands import package_settings as settings_commands
from control_plane.application.commands import views as view_commands
from control_plane.application.commands.calendars import check_calendar_spec, publish_calendar
from control_plane.application.commands.catalog_retirements import (
    CALENDAR,
    PROCESS,
    RENAMED_BY,
    lock_keys,
    restore_key,
    retire_key,
    retired_keys,
    share_keys,
)
from control_plane.application.commands.package_catalog import CATALOG_KINDS, Latest, SpecShape
from control_plane.application.commands.package_test import (
    overlay_catalog,
    with_workspace,
    workspace_exists,
)
from control_plane.application.commands.package_trials import (
    SUPPORTING_KINDS,
    SupportingShape,
    publish_supporting,
)
from control_plane.application.commands.process_definitions import (
    engine_revision_for,
    load_catalog,
    process_scope,
    publish_process_definition,
)
from control_plane.application.commands.process_instances import (
    CORRELATION_PREFIX,
    calendars_named,
    definition_of,
    engine_time,
    take,
)
from control_plane.application.commands.process_replays import chosen_instances, replay_one
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.graph import GraphScope
from control_plane.application.events import record_event
from control_plane.application.locking import lock_caller
from control_plane.application.queries.process_regulations import (
    UNKNOWN_SECTION,
    document_sections,
    regulation_scope,
)
from control_plane.config import Settings
from control_plane.domain import process_engine as engine
from control_plane.domain.calendar import Calendar
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.package_plan import (
    PLANNED_KINDS,
    RENAMED_KINDS,
    FieldChange,
    Rename,
    canonical_hash,
    catalog_etag,
    diff_fields,
    outside,
    package_hash,
    plan_hash,
    renames,
)
from control_plane.domain.package_settings import check_declaration
from control_plane.domain.package_source import PackageObject, ParsedPackage, parse_package
from control_plane.domain.process_definition import (
    GOVERNED_BY_UNCHECKED,
    Catalog,
    Problem,
    SpecError,
    check_process,
    definition_hash,
    governed_references,
    normalized_spec,
)
from control_plane.domain.process_definition import references as process_references
from control_plane.domain.process_migration import (
    MIGRATE,
    MIGRATION_INPUT,
    PIN,
    RECOUNT_INPUT,
    MigrationError,
    migrate_state,
    migration_for,
    migration_record,
    recount_body,
    recounts,
    renamer,
    uncovered,
)
from control_plane.domain.settings_refs import NONE, SettingsScope
from control_plane.domain.views import knowledge_names
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.models import (
    CalendarVersion,
    ProcessDefinition,
    ProcessInstance,
    ProcessInstanceEvent,
    ProcessRecall,
    ProcessTimer,
)
from control_plane.infrastructure.db.models import PackageObject as PackageRecord

# The shape of a package object per kind (``Calendar``, ``TaskType``,
# ``Agent``, ``WorkRule``): the spec as the route of its kind takes it.
Shapes = Mapping[str, SpecShape]
# Diverged instances named per process in the plan.
MAX_DIVERGED_IDS = 20
# Deadlines a migration moves listed per process in the plan; ``deadlinesTotal`` counts all.
MAX_PLAN_DEADLINES = 200
_OPEN = (engine.RUNNING, engine.SUSPENDED)
# The actions that publish no version: the object stays at its latest one.
UNPUBLISHED = ("unchanged", "restore")
# A retired key a package installs as it is comes back into use (Zh3).
RESTORED_EVENTS = {"Process": "process.definition_restored", "Calendar": "calendar.restored"}
_ENTITY_TYPES = {"Process": "process_definition", "Calendar": "calendar"}
VIEW = "View"
# Why a view the package no longer brings is retired.
VIEW_GONE = "no longer in package "


# --- the plan --------------------------------------------------------------------------------


@dataclass
class _Planned:
    """One object the plan applies."""

    kind: str
    key: str
    obj: PackageObject
    wanted: dict[str, Any]
    wanted_hash: str
    renamed_from: str | None = None
    latest: Latest | None = None
    # What the last apply wanted of this key, and of the key it is renamed from.
    record: PackageRecord | None = None
    source_record: PackageRecord | None = None
    action: str = "create"
    fields: list[FieldChange] = field(default_factory=list)
    published: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    definition: engine.Definition | None = None
    # TaskType: the active versions the apply deprecates.
    deprecates: list[int] = field(default_factory=list)

    @property
    def source(self) -> str:
        """The key whose instances and versions the object carries on."""
        return self.renamed_from or self.key

    def change(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "action": self.action,
            "renamedFrom": self.renamed_from,
            "fields": [item.out() for item in self.fields],
            "deprecates": self.deprecates,
        }


@dataclass(frozen=True)
class _Moved:
    """An open instance moved to a new version by the map: the journal entry ``migrate``.

    The engine's step ``migrated`` starts from ``state`` at ``at``, the time
    of the entry; the plan takes it on this copy, the apply after writing it.
    """

    state: dict[str, Any]
    at: datetime
    from_version: int

    @property
    def seq(self) -> int:
        return int(self.state["seq"])

    def recount(self, target: engine.Definition) -> dict[str, Any]:
        """The body of the input ``migrated`` that follows the entry."""
        return recount_body(from_version=self.from_version, target=target)


def _move(
    old: engine.Definition,
    new: engine.Definition,
    instance: ProcessInstance,
    mapping: Mapping[str, str],
) -> _Moved:
    """Move ``instance`` from ``old`` to ``new`` by ``mapping``; ``MigrationError`` if it cannot."""
    state = migrate_state(old, new, instance.state, mapping)
    state["seq"] = int(instance.state["seq"]) + 1
    return _Moved(state, engine_time(instance, utcnow()), instance.definition_version)


@dataclass
class _Group:
    """The open instances of one version of a changed process."""

    process: _Planned
    version: int
    row: ProcessDefinition
    instances: list[ProcessInstance]
    fate: str
    migration: Mapping[str, Any] | None
    failures: list[tuple[ProcessInstance, MigrationError]] = field(default_factory=list)
    # The instances moved by the map, by id, when the new version recounts deadlines.
    moved: dict[uuid.UUID, _Moved] = field(default_factory=dict)

    @property
    def mapping(self) -> dict[str, str]:
        return dict((self.migration or {}).get("map") or {})

    def out(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "open": len(self.instances),
            "fate": self.fate,
            "migrationRequired": bool(self.failures),
        }


@dataclass
class PackagePlan:
    """``PackagePlanOut``."""

    package_key: str | None
    package_version: str | None
    package_hash: str
    overwrite: bool
    problems: list[Problem] = field(default_factory=list)
    planned: list[_Planned] = field(default_factory=list)
    outside: list[dict[str, str]] = field(default_factory=list)
    # The artifact types, roles and skills of the package: the installer
    # applies them, the trial publishes those the tenant lacks (TASK-001197).
    supporting: list[PackageObject] = field(default_factory=list)
    groups: list[_Group] = field(default_factory=list)
    effective_renames: list[_Planned] = field(default_factory=list)
    behaviour: dict[str, dict[str, Any]] = field(default_factory=dict)
    deadlines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    etag_entries: list[dict[str, Any]] = field(default_factory=list)
    # The (kind, key) of the catalog objects the plan reads: what the apply and the trial lock.
    lock_pairs: set[tuple[str, str]] = field(default_factory=set)
    # (kind, key) of the objects out of use (catalog_retirements, CP-ADR-0074 Zh1).
    retired: set[tuple[str, str]] = field(default_factory=set)
    created_at: datetime = field(default_factory=utcnow)
    # The package as parsed: its dictionaries are kept by the apply (CP-ADR-0080).
    source: ParsedPackage | None = None
    # The settings of the package against its active revision (CP-ADR-0081 §7).
    settings: settings_commands.SettingsPlan | None = None
    # What ``settings`` of its processes, rules and views is: the declared schema (§6).
    settings_scope: SettingsScope = NONE

    @property
    def catalog_etag(self) -> str:
        return catalog_etag(self.etag_entries)

    @property
    def package(self) -> tuple[str, str] | None:
        """The source an agent's revision records: the package and its version."""
        if self.package_key is None or self.package_version is None:
            return None
        return (self.package_key, self.package_version)

    def changes(self) -> list[dict[str, Any]]:
        return [item.change() for item in self.planned]

    def processes(self) -> list[dict[str, Any]]:
        out = []
        for item in self.planned:
            if item.kind != "Process" or item.action in UNPUBLISHED:
                continue
            out.append(
                {
                    "key": item.key,
                    "fromVersion": item.latest.version if item.latest else None,
                    "toVersion": item.published.get("version"),
                    "behaviour": self.behaviour.get(item.key),
                    "instances": [g.out() for g in self.groups if g.process is item],
                    "deadlines": self.deadlines.get(item.key, [])[:MAX_PLAN_DEADLINES],
                    "deadlinesTotal": len(self.deadlines.get(item.key, [])),
                }
            )
        return out

    @property
    def hash(self) -> str:
        return plan_hash(
            self.package_hash,
            self.catalog_etag,
            self.changes(),
            self.processes(),
            overwrite=self.overwrite,
            settings=self.settings.out() if self.settings is not None else None,
        )

    def out(self) -> dict[str, Any]:
        return {
            "planHash": self.hash,
            "catalogEtag": self.catalog_etag,
            "package": {"key": self.package_key, "version": self.package_version},
            "changes": self.changes(),
            "outside": self.outside,
            "processes": self.processes(),
            "regulationCoverage": self.coverage,
            "settings": self.settings.out() if self.settings is not None else None,
            "problems": [p.out() for p in _sorted(self.problems)],
            "createdAt": self.created_at.isoformat(),
        }


def _sorted(problems: Sequence[Problem]) -> list[Problem]:
    return sorted(problems, key=lambda p: (not p.error, p.file or "", p.line or 0, p.path, p.code))


# --- reading the catalog ---------------------------------------------------------------------


async def _latest(db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> Latest | None:
    if kind in CATALOG_KINDS:
        return await package_catalog.latest_of(db, tenant_id, kind, key)
    if kind == VIEW:
        return await view_commands.latest_view(db, tenant_id, key)
    if kind == "Process":
        row = await db.scalar(
            select(ProcessDefinition)
            .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key)
            .order_by(ProcessDefinition.version.desc())
            .limit(1)
        )
        return Latest(row.version, row.definition_hash, row.spec, row) if row else None
    calendar = await db.scalar(
        select(CalendarVersion)
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key == key)
        .order_by(CalendarVersion.version.desc())
        .limit(1)
    )
    if calendar is None:
        return None
    return Latest(calendar.version, calendar.calendar_hash, calendar.spec, calendar)


async def _records(
    db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]], *, lock: bool
) -> dict[tuple[str, str], PackageRecord]:
    if not pairs:
        return {}
    stmt = (
        select(PackageRecord)
        .where(
            PackageRecord.tenant_id == tenant_id,
            tuple_(PackageRecord.kind, PackageRecord.key).in_(sorted(pairs)),
        )
        .order_by(PackageRecord.kind, PackageRecord.key)
    )
    if lock:
        stmt = stmt.with_for_update()
    return {(r.kind, r.key): r for r in await db.scalars(stmt)}


async def retired_pairs(
    db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """The processes, calendars and views of ``pairs`` that are retired."""
    out: set[tuple[str, str]] = set()
    for kind in RENAMED_KINDS:
        keys = [key for k, key in pairs if k == kind]
        out |= {(kind, key) for key in await retired_keys(db, tenant_id, kind, keys)}
    for kind, key in pairs:
        if kind == VIEW:
            latest = await view_commands.latest_view(db, tenant_id, key)
            if latest is not None and latest.retired:
                out.add((kind, key))
    return out


async def catalog_entries(
    db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """What the catalog etag is computed from, for the objects ``pairs``."""
    records = await _records(db, tenant_id, pairs, lock=False)
    retired = await retired_pairs(db, tenant_id, pairs)
    entries = []
    for kind, key in sorted(pairs):
        latest = await _latest(db, tenant_id, kind, key)
        record = records.get((kind, key))
        entries.append(
            {
                "kind": kind,
                "key": key,
                "version": latest.version if latest else None,
                "hash": latest.hash if latest else None,
                "applied": record.spec_hash if record else None,
                "package": (record.package_key or None) if record else None,
                "retired": (kind, key) in retired or (latest is not None and latest.retired),
            }
        )
    return entries


# --- building the plan -----------------------------------------------------------------------


async def build_plan(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    files: Sequence[tuple[str, str]],
    workspace_id: uuid.UUID | None,
    overwrite: bool,
    shapes: Shapes,
    lock: bool = False,
) -> PackagePlan:
    """The plan without its reports (behaviour, coverage): what the hash covers."""
    # Parsing is CPU work bounded by its budget: off the event loop, which the
    # other requests share.
    package = await asyncio.to_thread(parse_package, files)
    manifest = package.manifest_object
    plan = PackagePlan(
        package_key=manifest.key if manifest else None,
        package_version=str(manifest.spec.get("version")) if manifest else None,
        package_hash=package_hash(files),
        overwrite=overwrite,
        source=package,
    )
    plan.problems.extend(package.problems)
    plan.outside = outside(package)
    plan.supporting = [
        obj
        for kind in SUPPORTING_KINDS
        for obj in sorted(package.of_kind(kind), key=lambda o: o.key)
    ]
    if manifest is None:
        plan.problems.append(
            Problem(
                "package_manifest_missing",
                "error",
                "",
                "the package has no package.yaml: an apply records its objects under the package",
                file="package.yaml",
            )
        )
    listed, found = renames(package)
    plan.problems.extend(found)
    by_target = {(r.kind, r.target): r for r in listed}
    # The settings it declares (CP-ADR-0081 §1, §2): their labels are keys of its dictionaries.
    declaration = check_declaration(package)
    plan.problems.extend(declaration.problems)
    plan.settings_scope = SettingsScope(
        plan.package_key, declaration.declared.schema if declaration.declared else None
    )
    # The screens of the package, checked against it and the catalog (CP-ADR-0080).
    screens = await view_commands.check_package(
        db, ctx.tenant_id, package, shown=declaration.messages, settings=plan.settings_scope
    )
    plan.problems.extend(screens.problems)
    brought = {obj.key for obj in package.of_kind(VIEW)}
    gone = (
        [
            key
            for key in await view_commands.linked_views(db, ctx.tenant_id, plan.package_key)
            if key not in brought
        ]
        if plan.package_key is not None
        else []
    )
    pairs = {(o.kind, o.key) for o in package.objects if o.kind in PLANNED_KINDS}
    pairs |= {(r.kind, r.source) for r in listed}
    pairs |= {(VIEW, key) for key in gone}
    plan.lock_pairs = pairs
    if lock:
        await package_catalog.lock_keys(db, ctx.tenant_id, pairs)
        # Every process and calendar up front, processes first: a publication
        # of a process holds its key and then shares its calendars.
        for kind in (PROCESS, CALENDAR):
            await lock_keys(db, ctx.tenant_id, kind, (k for c, k in pairs if c == kind))
    records = await _records(db, ctx.tenant_id, pairs, lock=lock)
    plan.retired = await retired_pairs(db, ctx.tenant_id, pairs)
    plan.etag_entries = await catalog_entries(db, ctx.tenant_id, pairs)
    # The active revision and the saved values, locked as a PUT locks them.
    plan.settings, found = await settings_commands.plan_settings(
        db, ctx.tenant_id, manifest, declaration, lock=lock
    )
    plan.problems.extend(found)
    if manifest is not None:
        entry = await settings_commands.etag_entry(db, ctx.tenant_id, manifest.key)
        if entry is not None:
            plan.etag_entries.append(entry)

    calendars = frozenset(o.key for o in package.of_kind("Calendar"))
    for kind in PLANNED_KINDS:
        for obj in sorted(package.of_kind(kind), key=lambda o: o.key):
            if kind == "Calendar":
                sent, problems = shapes[kind](obj)
                plan.problems.extend(obj.place(p) for p in problems)
                if sent is None:
                    continue
                body, body_hash, _ = check_calendar_spec(sent)
                item = _Planned(kind, obj.key, obj, body, body_hash)
            elif kind == VIEW:
                checked = screens.checks.get(obj.key)
                if checked is None or checked.form is None:
                    continue
                item = _Planned(kind, obj.key, obj, checked.form, canonical_hash(checked.form))
            elif kind == "Process":
                try:
                    body = normalized_spec(obj.spec)
                except SpecError as exc:
                    plan.problems.append(
                        obj.place(Problem("invalid_document", "error", exc.path, exc.message))
                    )
                    continue
                if workspace_id is not None:
                    body = with_workspace(body, workspace_id)
                item = _Planned(kind, obj.key, obj, body, definition_hash(body))
            else:
                shaped = await _catalog_item(db, ctx, plan, obj, workspace_id, shapes)
                if shaped is None:
                    continue
                item = shaped
            await _place(db, ctx, plan, item, by_target, records, manifest)
            if kind == "Process":
                await _check(db, ctx, plan, item, package, calendars)
            plan.planned.append(item)
            if kind == "Process" and item.action in ("update", "rename"):
                await _instances(db, ctx, plan, item, lock=lock)
    for key in gone:
        assert manifest is not None
        latest = await _latest(db, ctx.tenant_id, VIEW, key)
        assert latest is not None
        plan.planned.append(
            _Planned(
                VIEW,
                key,
                manifest,
                {},
                "",
                latest=latest,
                record=records.get((VIEW, key)),
                action="retire",
                version=latest.version,
            )
        )
    return plan


async def _catalog_item(
    db: AsyncSession,
    ctx: AuthContext,
    plan: PackagePlan,
    obj: PackageObject,
    workspace_id: uuid.UUID | None,
    shapes: Shapes,
) -> _Planned | None:
    """A task type, an agent or a rule as its form, or ``None`` with its findings."""
    raw = obj.spec.get("workspaceId") if obj.kind == "WorkRule" else None
    if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
        if workspace_id is None:
            plan.problems.append(
                obj.place(
                    Problem(
                        "unresolved_install_variable",
                        "error",
                        "/spec/workspaceId",
                        f"{raw} is an install variable: the plan was given no workspaceId",
                        hint="pass workspaceId with the plan and the apply",
                    )
                )
            )
            return None
        obj = replace(obj, spec=with_workspace(obj.spec, workspace_id))
    sent, problems = shapes[obj.kind](obj)
    plan.problems.extend(obj.place(p) for p in problems)
    if sent is None:
        return None
    latest = await _latest(db, ctx.tenant_id, obj.kind, obj.key)
    try:
        form = package_catalog.wanted_form(obj.kind, sent, latest, plan.settings_scope)
    except DomainError as exc:
        plan.problems.append(obj.place(_finding(exc)))
        return None
    return _Planned(obj.kind, obj.key, obj, form, canonical_hash(form))


def _finding(exc: DomainError, severity: str = "error") -> Problem:
    """A refusal of a command as a finding of the object it was about."""
    return package_catalog.finding(exc, severity)


async def _place(
    db: AsyncSession,
    ctx: AuthContext,
    plan: PackagePlan,
    item: _Planned,
    by_target: Mapping[tuple[str, str], Rename],
    records: Mapping[tuple[str, str], PackageRecord],
    manifest: PackageObject | None,
) -> None:
    """Where the object comes from (its key or a key it is renamed from) and what changes."""
    latest = await _latest(db, ctx.tenant_id, item.kind, item.key)
    item.record = records.get((item.kind, item.key))
    owner = item.record.package_key if item.record is not None else None
    if owner and plan.package_key is not None and owner != plan.package_key:
        # The apply links the object to this package: say so, not silently.
        plan.problems.append(
            item.obj.place(
                Problem(
                    "package_owner_changed",
                    "warning",
                    "",
                    f"{item.kind}/{item.key} belongs to package {owner}: the apply moves it"
                    f" to package {plan.package_key}",
                    hint=f"drop {item.kind}/{item.key} from one of the packages"
                    " unless the package was renamed",
                )
            )
        )
    # The link of a retired key is no base: what it wanted is not in use.
    base = item.record if (item.kind, item.key) not in plan.retired else None
    rename = by_target.get((item.kind, item.key))
    if rename is not None:
        source = await _latest(db, ctx.tenant_id, item.kind, rename.source)
        record = records.get((item.kind, rename.source))
        retired = (item.kind, rename.source) in plan.retired
        if source is not None and not retired and latest is None:
            # The object moves with its versions and what its last apply wanted.
            item.renamed_from, item.source_record = rename.source, record
            latest, base = source, record
            plan.effective_renames.append(item)
        elif source is not None and not retired and manifest is not None:
            plan.problems.append(
                manifest.place(
                    Problem(
                        "rename_target_exists",
                        "error",
                        "/spec/renames",
                        f"{item.kind}/{rename.source} and {item.kind}/{item.key} both exist:"
                        " the rename has nothing to move to",
                        hint=f"retire {item.kind}/{rename.source} or drop the rename",
                    )
                )
            )
    item.latest = latest
    item.fields, item.published = diff_fields(
        latest.spec if latest else None,
        item.wanted,
        base.spec if base is not None else None,
        overwrite=plan.overwrite,
    )
    if item.kind == "Calendar":
        # Kept console fields join the package's: the stored form again.
        item.published, _, _ = check_calendar_spec(item.published)
    if latest is None:
        item.action = "create"
    elif item.renamed_from is not None:
        item.action = "rename"
    elif item.published == latest.spec:
        item.action = "unchanged"
    else:
        item.action = "update"
    if item.action == "unchanged" and (item.kind, item.key) in plan.retired:
        # Installed again as it is, a retired process or calendar is back in
        # use: the apply deletes its retirement, no version is published (Zh3).
        item.action = "restore"
    if item.kind in CATALOG_KINDS:
        item.version = await package_catalog.planned_version(
            db, ctx.tenant_id, item.kind, item.key, latest, item.published, item.action
        )
        if item.kind == "TaskType":
            item.deprecates = package_catalog.deprecated_versions(latest, item.action)
        found = package_catalog.static_problems(item.kind, item.key, latest, item.published)
        plan.problems.extend(item.obj.place(p) for p in found)
        return
    if item.kind == VIEW:
        if latest is None:
            item.version = 1
        else:
            item.version = latest.version if item.action in UNPUBLISHED else latest.version + 1
        return
    if item.kind == "Calendar":
        if item.action in UNPUBLISHED and latest is not None:
            item.version = latest.version
        elif latest is None or item.renamed_from is not None:
            item.version = 1
        else:
            item.version = latest.version + 1
        return
    version = item.published.get("version")
    item.version = latest.version if item.action in UNPUBLISHED and latest else version
    if (
        item.action in ("update", "rename")
        and latest is not None
        and isinstance(version, int)
        and version <= latest.version
    ):
        plan.problems.append(
            item.obj.place(
                Problem(
                    "process_version_conflict",
                    "error",
                    "/spec/version",
                    f"version {version} is not above the latest version {latest.version}"
                    f" of {item.source}",
                    hint=f"raise spec.version to {latest.version + 1}",
                )
            )
        )


async def _check(
    db: AsyncSession,
    ctx: AuthContext,
    plan: PackagePlan,
    item: _Planned,
    package: ParsedPackage,
    calendars: frozenset[str],
) -> None:
    """The check of a publication (CP-ADR-0074 §2) of the spec the apply would publish.

    A restored process publishes nothing: of the check only ``calendar_retired``
    applies to it — back in use, it would need a calendar out of use (Zh3).
    """
    if item.action == "unchanged":
        return
    previous = item.latest.row if item.latest is not None else None
    catalog: Catalog = overlay_catalog(
        await load_catalog(
            db,
            ctx.tenant_id,
            item.key,
            item.published,
            previous,
            history_key=item.renamed_from,
            settings=plan.settings_scope,
        ),
        package,
        calendars,
    )
    checked = check_process(
        item.key, item.published, catalog, file=item.obj.file, locate=item.obj.locate
    )
    if item.action == "restore":
        plan.problems.extend(p for p in checked.problems if p.code == "calendar_retired")
        return
    plan.problems.extend(checked.problems)
    if not checked.errors:
        revision = await engine_revision_for(db, ctx.tenant_id, item.key, item.published)
        item.definition = engine.Definition.build(
            item.key, item.published, catalog, engine_revision=revision
        )


async def _instances(
    db: AsyncSession, ctx: AuthContext, plan: PackagePlan, item: _Planned, *, lock: bool
) -> None:
    """The open instances of the key the process carries on, by version, and their fate."""
    stmt = (
        select(ProcessInstance)
        .where(
            ProcessInstance.tenant_id == ctx.tenant_id,
            ProcessInstance.definition_key == item.source,
            ProcessInstance.status.in_(_OPEN),
        )
        .order_by(ProcessInstance.definition_version, ProcessInstance.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    by_version: dict[int, list[ProcessInstance]] = {}
    for instance in await db.scalars(stmt):
        by_version.setdefault(instance.definition_version, []).append(instance)
    for version, instances in sorted(by_version.items()):
        row = await db.scalar(
            select(ProcessDefinition).where(
                ProcessDefinition.tenant_id == ctx.tenant_id,
                ProcessDefinition.key == item.source,
                ProcessDefinition.version == version,
            )
        )
        assert row is not None  # an instance is pinned to a published version
        found = migration_for(item.published, version)
        migration = found[1] if found else None
        policy = migration.get("policy") if migration else None
        fate = PIN if policy == PIN else MIGRATE if migration else "unaffected"
        group = _Group(item, version, row, instances, fate, migration)
        plan.groups.append(group)
        if fate == PIN or item.definition is None:
            continue
        try:
            old = await definition_of(db, row)
        except DomainError as exc:
            error = MigrationError("definition_unusable", exc.message)
            group.failures = [(instance, error) for instance in instances]
        else:
            keep = recounts(item.definition)
            for instance in instances:
                try:
                    if fate == MIGRATE:
                        moved = _move(old, item.definition, instance, group.mapping)
                        if keep:
                            group.moved[instance.id] = moved
                    else:
                        missing = uncovered(old, instance.state, item.definition)
                        if missing:
                            raise MigrationError(
                                "migration_required",
                                f"version {item.definition.version} has no element for"
                                f" {', '.join(missing)}",
                                missing,
                            )
                except MigrationError as exc:
                    group.failures.append((instance, exc))
        if group.failures:
            group.moved.clear()
            plan.problems.append(_migration_required(item, group, found[0] if found else None))


def _migration_required(item: _Planned, group: _Group, index: int | None) -> Problem:
    elements = sorted({e for _, exc in group.failures for e in exc.elements})
    reasons = sorted({exc.message for _, exc in group.failures})
    target = item.published.get("version")
    where = f"/spec/migrations/{index}" if index is not None else "/spec"
    listed = f" on {', '.join(elements)}" if elements else ""
    hint = (
        "map every element the instances stand on to an element of the new version"
        if index is not None
        else f"add migrations: [{{from: {group.version}, to: {target}, policy: pin | migrate,"
        " map: {old element: new element}}]"
    )
    return item.obj.place(
        Problem(
            "migration_required",
            "error",
            where,
            f"{len(group.failures)} open instance(s) of {item.source}@{group.version} stand"
            f"{listed}; version {target} cannot carry them: {reasons[0]}",
            hint=hint,
        )
    )


# --- reports of the plan -----------------------------------------------------------------------


async def _behaviour(db: AsyncSession, ctx: AuthContext, plan: PackagePlan, limit: int) -> None:
    """The replay of each changed process on the latest instances of its current version."""
    revisions: dict[uuid.UUID, int] = {}
    for item in plan.planned:
        if item.definition is None or item.latest is None or item.action == "unchanged":
            continue
        instances = await chosen_instances(db, ctx, item.source, item.latest.version, None, limit)
        # A renamed process replays under the key its instances ran: the key is not behaviour.
        candidate = replace(item.definition, key=item.source)
        diverged: list[str] = []
        for instance in instances:
            result = await replay_one(db, candidate, instance, revisions)
            if result.divergence is not None:
                diverged.append(str(instance.id))
        plan.behaviour[item.key] = {
            "replayed": len(instances),
            "diverged": len(diverged),
            "instanceIds": diverged[:MAX_DIVERGED_IDS],
        }


async def _calendars(
    db: AsyncSession, ctx: AuthContext, plan: PackagePlan, spec: Mapping[str, Any]
) -> dict[str, Calendar]:
    """The calendars a version names as the apply leaves them: the package's ones published."""
    own = {
        item.key: Calendar.from_spec(item.published)
        for item in plan.planned
        if item.kind == "Calendar"
    }
    calendars, _ = await calendars_named(db, ctx.tenant_id, spec, own=own)
    return calendars


async def _deadlines(db: AsyncSession, ctx: AuthContext, plan: PackagePlan) -> None:
    """The deadlines each migrating instance gets from the new version (FR-023).

    The apply moves an instance by the map and then takes the engine's input
    ``migrated`` (:func:`_migrate`); here the same step is taken on the
    migrated copy :func:`_instances` kept (:func:`_move`), and its
    ``deadline_migrated`` decisions — a deadline set, moved, lifted or
    already past — are the section. Nothing is written. A group that cannot
    migrate has no section: the plan says ``migration_required`` instead.
    """
    calendars: dict[str, dict[str, Calendar]] = {}
    for group in plan.groups:
        item, new = group.process, group.process.definition
        if not group.moved or new is None:
            continue
        if item.key not in calendars:
            calendars[item.key] = await _calendars(db, ctx, plan, item.published)
        found = plan.deadlines.setdefault(item.key, [])
        for instance in group.instances:
            moved = group.moved[instance.id]
            given = engine.Input(
                RECOUNT_INPUT,
                moved.at,
                moved.recount(new),
                str(ctx.principal_id),
                calendars[item.key],
            )
            _, decisions, _ = engine.step(new, moved.state, given)
            found.extend(
                {
                    "instanceId": str(instance.id),
                    "element": decision.element,
                    "previousDueAt": decision.detail.get("previousDueAt"),
                    "dueAt": decision.detail.get("dueAt"),
                    "breached": bool(decision.detail.get("breached")),
                }
                for decision in decisions
                if decision.kind == "deadline_migrated"
            )


@dataclass(frozen=True)
class _Reference:
    process: _Planned
    document: str
    section: str | None
    element: str
    path: str


def _references(item: _Planned) -> list[_Reference]:
    """Every ``governedBy`` of a process with the element it governs."""
    spec = item.published if item.action != "unchanged" else item.wanted
    places: list[tuple[str, str]] = []
    for index, stage in enumerate(spec.get("stages") or ()):
        places.append((f"/spec/stages/{index}", str(stage.get("id"))))
    if item.definition is not None:
        places += [(entry.path, sid) for sid, entry in item.definition.steps.items()]
    for index, table in enumerate(spec.get("decisions") or ()):
        places.append((f"/spec/decisions/{index}", str(table.get("id"))))
    for index, stage in enumerate(spec.get("stages") or ()):
        for number, milestone in enumerate(stage.get("milestones") or ()):
            places.append((f"/spec/stages/{index}/milestones/{number}", str(milestone.get("id"))))
    places.sort(key=lambda p: len(p[0]), reverse=True)
    found = []
    for document, path in governed_references(spec):
        at = path[: -len("/document")]
        node: Any = spec
        for part in at.split("/")[2:]:
            node = node[int(part)] if isinstance(node, list) else node.get(part)
        section = node.get("section") if isinstance(node, Mapping) else None
        element = next((element for prefix, element in places if at.startswith(prefix + "/")), None)
        label = f"{item.key}/{element}" if element else item.key
        found.append(
            _Reference(item, document, section if isinstance(section, str) else None, label, at)
        )
    return found


async def _coverage(
    provider: GraphProvider | None,
    scoped: Sequence[tuple[_Planned, GraphScope]],
    settings: Settings,
    trace_run_id: str,
) -> tuple[list[dict[str, Any]], list[Problem]]:
    """Coverage of the sections of each named regulation, and the problems it finds."""
    references = [(ref, scope) for item, scope in scoped for ref in _references(item)]
    if not references:
        return [], []
    sections: dict[str, list[str] | None] = {}
    reason: str | None = None
    if provider is None:
        reason = "memory is not configured"
    else:
        for item, scope in scoped:
            wanted = [r.document for r in _references(item) if r.document not in sections]
            if not wanted:
                continue
            try:
                sections.update(
                    await document_sections(
                        provider, scope, wanted, settings, trace_run_id=trace_run_id
                    )
                )
            except TimeoutError:
                reason = "memory did not answer in time"
                break
            except ContextProviderError:
                reason = "memory failed to answer"
                break
    if reason is not None:
        return [], [
            Problem(
                GOVERNED_BY_UNCHECKED,
                "warning",
                "",
                f"the coverage of the regulations was not computed: {reason}",
                hint="plan again when memory is available",
            )
        ]
    coverage: list[dict[str, Any]] = []
    problems: list[Problem] = []
    for document in dict.fromkeys(r.document for r, _ in references):
        known = sections.get(document)
        covered: dict[str, list[str]] = {}
        for ref, _ in references:
            if ref.document != document or ref.section is None:
                continue
            covered.setdefault(ref.section, [])
            if ref.element not in covered[ref.section]:
                covered[ref.section].append(ref.element)
            if known and ref.section not in known:
                problems.append(
                    ref.process.obj.place(
                        Problem(
                            UNKNOWN_SECTION,
                            "warning",
                            ref.path + "/section",
                            f"{document!r} has no section {ref.section!r} in the knowledge base",
                            hint=f"sections: {', '.join(known[:20])}",
                        )
                    )
                )
        coverage.append(
            {
                "document": document,
                "found": known is not None,
                "covered": {k: covered[k] for k in sorted(covered)},
                "uncovered": [s for s in known or () if s not in covered],
            }
        )
    return coverage, problems


# --- routes' work ------------------------------------------------------------------------------


async def plan_package(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    settings: Settings,
    provider: GraphProvider | None,
    *,
    files: Sequence[tuple[str, str]],
    workspace_id: uuid.UUID | None,
    replay_limit: int,
    overwrite: bool,
    shapes: Shapes,
    supporting: Mapping[str, SupportingShape],
) -> PackagePlan:
    """``POST /packages:plan``: whatever is written to try the commands is rolled back."""
    await authorize(ctx, Permission.PACKAGES_PLAN)
    if workspace_id is not None:
        await authorize(ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id))
    scoped: list[tuple[_Planned, GraphScope]] = []
    namespace: str | None = None
    async with session_factory() as db:
        tx = await db.begin()
        try:
            # Rule 1 of CP-ADR-0077: the trial runs the writing commands.
            await lock_caller(db, ctx)
            if workspace_id is not None and not await workspace_exists(db, ctx, workspace_id):
                raise NotFoundError(
                    "Workspace not found", details={"workspaceId": str(workspace_id)}
                )
            plan = await build_plan(
                db,
                ctx,
                files=files,
                workspace_id=workspace_id,
                overwrite=overwrite,
                shapes=shapes,
            )
            if replay_limit > 0:
                await _behaviour(db, ctx, plan, replay_limit)
            await _deadlines(db, ctx, plan)
            for item in plan.planned:
                spec = item.published if item.action != "unchanged" else item.wanted
                if item.kind == "Process" and governed_references(spec):
                    try:
                        scoped.append((item, await regulation_scope(db, ctx, settings, spec)))
                    except ValueError:
                        continue  # a workspace id that is no UUID: the check has said so
            if any(i.kind == VIEW and knowledge_names(i.wanted) for i in plan.planned):
                namespace = await view_commands.knowledge_namespace(db, ctx, settings, workspace_id)
            await _trial(db, ctx, settings, plan, supporting)
        finally:
            await tx.rollback()
    plan.coverage, found = await _coverage(provider, scoped, settings, ctx.trace_run_id)
    plan.problems.extend(found)
    # The views of knowledge against the ontology of the tree (CP-ADR-0080, amendment Б3).
    plan.problems.extend(
        await view_commands.knowledge_problems(
            provider,
            namespace,
            [(i.obj, i.wanted) for i in plan.planned if i.kind == VIEW],
            settings,
            asked_workspace=workspace_id is not None,
            trace_run_id=ctx.trace_run_id,
        )
    )
    return plan


async def _trial(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    plan: PackagePlan,
    supporting: Mapping[str, SupportingShape],
) -> None:
    """Run the command of each task type, agent and rule the apply would publish.

    Each in its own savepoint, in the order of the apply, so a rule sees the
    task type and the agent the package brings; the transaction around is
    rolled back by the caller. What a command refuses is a finding of the
    object. A right the caller lacks ends the trial with a warning: the apply
    needs the right anyway, and what follows would stumble on the gap. A
    supporting object the caller may not publish only leaves unchecked the
    objects that name its key (review of TASK-001197); the others are tried.

    First the artifact types, roles and skills of the package the tenant
    lacks are published the same way (:func:`package_trials.publish_supporting`):
    the installer applies them before ``packages:apply`` (``outside``), so a
    type, an agent or a rule is checked against the skills and artifact
    types of its own package, not only against those the stand has
    (TASK-001197). Their findings are warnings: the plan does not apply them.

    The keys are locked first, as the apply locks them
    (:func:`package_catalog.lock_keys`): a savepoint released keeps its locks,
    and taken one command at a time they would come in another order than
    the apply's.
    """
    if not any(
        item.kind in CATALOG_KINDS and (item.action != "unchanged" or item.deprecates)
        for item in plan.planned
    ):
        return
    await package_catalog.lock_keys(db, ctx.tenant_id, plan.lock_pairs)
    gaps: dict[str, AuthorizationError] = {}
    for obj in plan.supporting:
        try:
            async with db.begin_nested():
                found = await publish_supporting(db, ctx, settings, supporting, obj)
        except AuthorizationError as exc:
            plan.problems.append(_unchecked(obj, exc))
            gaps[obj.key] = exc
            continue
        except DomainError as exc:
            found = [_finding(exc)]
        plan.problems.extend(obj.place(replace(p, severity="warning")) for p in found)
    for item in plan.planned:
        if item.kind not in CATALOG_KINDS or (item.action == "unchanged" and not item.deprecates):
            continue
        if any(p.error and p.file == item.obj.file for p in plan.problems):
            continue
        gap = _names_gap(item.published, gaps)
        if gap is not None:
            # Its command would refuse a key the trial could not publish, not the object.
            plan.problems.append(_unchecked(item.obj, gap))
            continue
        try:
            async with db.begin_nested():
                await package_catalog.publish(
                    db,
                    ctx,
                    kind=item.kind,
                    key=item.key,
                    spec=item.published,
                    latest=item.latest,
                    action=item.action,
                    deprecates=item.deprecates,
                    package=plan.package,
                    settings=plan.settings_scope,
                )
        except AuthorizationError as exc:
            plan.problems.append(_unchecked(item.obj, exc))
            return
        except DomainError as exc:
            plan.problems.append(item.obj.place(_finding(exc)))


def _names_gap(spec: Any, gaps: Mapping[str, AuthorizationError]) -> AuthorizationError | None:
    """The refusal of a supporting object ``spec`` names: a string leaf is its key or ``key@v``."""
    if not gaps:
        return None
    if isinstance(spec, str):
        return gaps.get(spec) or gaps.get(spec.partition("@")[0])
    values = spec.values() if isinstance(spec, Mapping) else spec if isinstance(spec, list) else ()
    for value in values:
        found = _names_gap(value, gaps)
        if found is not None:
            return found
    return None


def _unchecked(obj: PackageObject, exc: AuthorizationError) -> Problem:
    """The warning of a trial that a right of the caller ended."""
    return obj.place(
        Problem(
            "permission_required",
            "warning",
            "",
            f"{obj.kind}/{obj.key} was not checked: {exc.message}; the apply"
            " needs the right of every kind it changes",
            hint=", ".join((exc.details or {}).get("missing") or ()) or None,
        )
    )


async def apply_package(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    files: Sequence[tuple[str, str]],
    expected_hash: str,
    workspace_id: uuid.UUID | None,
    overwrite: bool,
    shapes: Shapes,
    touched: list[tuple[str, uuid.UUID]] | None = None,
) -> dict[str, Any]:
    """``POST /packages:apply``: exactly the plan with ``expected_hash``, or a refusal.

    ``touched`` collects the IAM identities whose binding an agent's revision
    changed: the route drops their cache entries after the commit.
    """
    await authorize(ctx, Permission.PACKAGES_PLAN)
    if workspace_id is not None:
        await authorize(ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id))
    await package_links.lock_applies(db, ctx.tenant_id)
    plan = await build_plan(
        db,
        ctx,
        files=files,
        workspace_id=workspace_id,
        overwrite=overwrite,
        shapes=shapes,
        lock=True,
    )
    current = plan.hash
    if current != expected_hash:
        raise ConflictError(
            "plan_stale",
            "The catalog or the open instances changed since the plan was built:"
            " build the plan again and apply the new one",
            details={
                "planHash": expected_hash,
                "currentPlanHash": current,
                "catalogEtag": plan.catalog_etag,
            },
        )
    problems = _sorted(plan.problems)
    required = [p for p in problems if p.code == "migration_required"]
    if required:
        raise ValidationError(
            "migration_required",
            f"{required[0].message} (and {len(required) - 1} more)"
            if len(required) > 1
            else required[0].message,
            details={"problems": [p.out() for p in problems]},
        )
    errors = [p for p in problems if p.error]
    if errors:
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise ValidationError(
            "invalid_package",
            f"The package is invalid: {errors[0].message} at {errors[0].path}{more}",
            details={"problems": [p.out() for p in problems]},
        )
    published: dict[str, ProcessDefinition] = {}
    for item in plan.planned:
        if item.kind in CATALOG_KINDS:
            if item.action == "unchanged" and not item.deprecates:
                continue
            done = await package_catalog.publish(
                db,
                ctx,
                kind=item.kind,
                key=item.key,
                spec=item.published,
                latest=item.latest,
                action=item.action,
                deprecates=item.deprecates,
                package=plan.package,
                settings=plan.settings_scope,
            )
            item.version = done.version
            if touched is not None:
                touched.extend(done.touched)
            continue
        if item.kind == VIEW:
            await _apply_view(db, ctx, plan, item)
            continue
        if item.action == "unchanged":
            continue
        if item.action == "restore":
            await _restore(db, ctx, plan, item)
            continue
        if item.kind == "Calendar":
            calendar = await publish_calendar(db, ctx, key=item.key, spec=item.published)
            item.version = calendar.row.version
        else:
            process = await publish_process_definition(
                db,
                ctx,
                key=item.key,
                spec=item.published,
                renamed_from=item.renamed_from,
                settings=plan.settings_scope,
            )
            item.version = process.row.version
            published[item.key] = process.row
    for group in plan.groups:
        if group.fate != MIGRATE:
            continue
        target = published[group.process.key]
        for instance in group.instances:
            await _migrate(db, ctx, instance, group, target, current)
    now = utcnow()
    for item in plan.effective_renames:
        assert item.renamed_from is not None and item.latest is not None
        record = item.source_record or _new_record(
            ctx, plan, item.kind, item.renamed_from, item.latest.spec, current
        )
        db.add(record)
        record.version, record.spec_hash = item.latest.version, item.latest.hash
        record.package_version = plan.package_version
        _stamp(record, ctx, current, now)
        await retire_key(
            db,
            ctx.tenant_id,
            item.kind,
            item.renamed_from,
            by=ctx.principal_id,
            reason=f"{RENAMED_BY}{plan.package_key or ''}",
            at=now,
        )
    for item in plan.planned:
        if item.action == "retire":
            continue  # the link stays: which package the retired view was in
        record = item.record or _new_record(ctx, plan, item.kind, item.key, item.wanted, current)
        db.add(record)
        record.package_key = plan.package_key or record.package_key
        record.package_version = plan.package_version
        record.version = int(item.version or 0)
        record.spec, record.spec_hash = item.wanted, item.wanted_hash
        _stamp(record, ctx, current, now)
    if plan.package_key is not None and plan.source is not None:
        await view_commands.record_dictionaries(
            db, ctx, plan.source, plan.package_key, plan.package_version
        )
    if plan.settings is not None:
        await settings_commands.record_revision(
            db, ctx, plan.settings, package_version=plan.package_version, plan_hash=current
        )
    pairs = {
        (e["kind"], e["key"]) for e in plan.etag_entries if e["kind"] != settings_commands.ETAG_KIND
    }
    await db.flush()
    entries = await catalog_entries(db, ctx.tenant_id, pairs)
    if plan.package_key is not None:
        entry = await settings_commands.etag_entry(db, ctx.tenant_id, plan.package_key)
        if entry is not None:
            entries.append(entry)
    return {
        "planHash": current,
        "catalogEtag": catalog_etag(entries),
        "applied": [
            {"kind": item.kind, "key": item.key, "action": item.action, "version": item.version}
            for item in plan.planned
        ],
    }


async def _apply_view(
    db: AsyncSession, ctx: AuthContext, plan: PackagePlan, item: _Planned
) -> None:
    """Publish, restore or retire a view (CP-ADR-0080); the plan checked it."""
    if item.action == "unchanged":
        return
    if item.action == "retire":
        assert item.latest is not None
        await view_commands.retire_view(
            db,
            ctx,
            latest=item.latest,
            package=plan.package,
            reason=f"{VIEW_GONE}{plan.package_key or ''}",
        )
        return
    if item.action == "restore":
        assert item.latest is not None
        await view_commands.restore_view(db, ctx, latest=item.latest, package=plan.package)
        return
    item.version = await view_commands.publish_view(
        db, ctx, key=item.key, form=item.published, latest=item.latest, package=plan.package
    )


async def _restore(db: AsyncSession, ctx: AuthContext, plan: PackagePlan, item: _Planned) -> None:
    """A retired key the package installs as it is comes back into use (Zh3).

    A process back in use needs its calendars in use: they are shared, as a
    publication shares them, and read again — a retirement of one that
    committed after the plan found the process retired, not needing it.
    """
    assert item.latest is not None
    if item.kind == PROCESS:
        calendars = process_references(item.latest.spec).calendars
        await share_keys(db, ctx.tenant_id, CALENDAR, calendars)
        retired = sorted(await retired_keys(db, ctx.tenant_id, CALENDAR, calendars))
        if retired:
            raise ConflictError(
                "calendar_retired",
                f"Process {item.key!r} is not restored: calendar {retired[0]!r} it needs"
                " was retired meanwhile; publish a new version of the calendar or of the"
                " process, then plan again",
                details={"process": item.key, "calendars": retired},
            )
    await restore_key(db, ctx.tenant_id, item.kind, item.key)
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type=RESTORED_EVENTS[item.kind],
        entity_type=_ENTITY_TYPES[item.kind],
        entity_id=item.latest.row.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": item.key,
            "latestVersion": item.latest.version,
            "packageKey": plan.package_key,
            "packageVersion": plan.package_version,
        },
    )


def _new_record(
    ctx: AuthContext, plan: PackagePlan, kind: str, key: str, spec: dict[str, Any], current: str
) -> PackageRecord:
    return PackageRecord(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        kind=kind,
        key=key,
        package_key=plan.package_key or "",
        package_version=plan.package_version,
        version=0,
        spec_hash="",
        spec=spec,
        plan_hash=current,
        applied_by=ctx.principal_id,
        applied_at=utcnow(),
    )


def _stamp(record: PackageRecord, ctx: AuthContext, current: str, now: datetime) -> None:
    record.plan_hash = current
    record.applied_by = ctx.principal_id
    record.applied_at = now


async def _migrate(
    db: AsyncSession,
    ctx: AuthContext,
    instance: ProcessInstance,
    group: _Group,
    target: ProcessDefinition,
    current: str,
) -> None:
    """Move one open instance to ``target`` by the group's map: journal entry and event.

    Under a revision with SLA deadlines the engine then takes ``migrated``:
    the deadlines of the open steps and of the process are counted by the
    new version, their timers and the due of the step's task follow.
    """
    old = await definition_of(db, group.row)
    new = await definition_of(db, target)
    mapping = group.mapping
    try:
        moved = _move(old, new, instance, mapping)
    except MigrationError as exc:  # pragma: no cover - the plan was built under the same locks
        raise ValidationError(
            "migration_required", exc.message, details={"instanceId": str(instance.id)}
        ) from exc
    state, seq, at, from_version = moved.state, moved.seq, moved.at, moved.from_version
    body, migrated = migration_record(
        from_key=instance.definition_key,
        from_version=from_version,
        target=new,
        mapping=mapping,
        plan_hash=current,
        state=state,
    )
    given = engine.Input(MIGRATION_INPUT, at, body, str(ctx.principal_id))
    db.add(
        ProcessInstanceEvent(
            instance_id=instance.id,
            seq=seq,
            tenant_id=instance.tenant_id,
            at=at,
            kind=MIGRATION_INPUT,
            source_ref=f"migration:{target.id}",
            event_id=None,
            actor_id=ctx.principal_id,
            input=given.out(),
            decisions=[migrated],
            intents=[],
            calendars={},
            created_at=utcnow(),
        )
    )
    rename = renamer(mapping)
    instance.definition_id = target.id
    instance.definition_key = target.key
    instance.definition_version = target.version
    instance.state = state
    instance.data = state.get("data") or {}
    instance.updated_at = utcnow()
    instance.refs = {
        ref: (
            {**value, "element": rename(value["element"])}
            if isinstance(value, dict) and isinstance(value.get("element"), str)
            else value
        )
        for ref, value in (instance.refs or {}).items()
    }
    # The attempt counters of step events follow the elements they count.
    attempts: dict[str, int] = {}
    for element, count in (instance.step_attempts or {}).items():
        attempts[rename(element)] = max(attempts.get(rename(element), 0), int(count))
    instance.step_attempts = attempts
    for timer in await db.scalars(
        select(ProcessTimer).where(
            ProcessTimer.instance_id == instance.id,
            ProcessTimer.state.in_(("pending", "frozen")),
        )
    ):
        timer.element = rename(timer.element)
    for recall in await db.scalars(
        select(ProcessRecall).where(
            ProcessRecall.instance_id == instance.id, ProcessRecall.state == "pending"
        )
    ):
        recall.element = rename(recall.element)
    await db.flush()
    await record_event(
        db,
        tenant_id=instance.tenant_id,
        event_type="process.migrated",
        entity_type="process_instance",
        entity_id=instance.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=f"{CORRELATION_PREFIX}{instance.id}",
        trace_run_id=ctx.trace_run_id,
        payload={
            "instanceId": str(instance.id),
            "definitionKey": target.key,
            "version": target.version,
            "instanceKey": instance.instance_key,
            "fromVersion": from_version,
            "map": mapping,
            "policy": MIGRATE,
        },
    )
    if recounts(new):
        await take(
            db,
            instance,
            target,
            RECOUNT_INPUT,
            moved.recount(new),
            at=at,
            source_ref=f"migration:{target.id}/{RECOUNT_INPUT}",
            actor_id=ctx.principal_id,
            trace_run_id=ctx.trace_run_id or "",
        )
