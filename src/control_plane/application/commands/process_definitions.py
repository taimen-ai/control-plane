"""Process definitions: immutable versions, checked before they are written (CP-ADR-0074 §1, §2).

A process is tenant data with a key; ``spec.version`` is the version being
published. The pair ``(key, version)`` never changes: the same version with
the same hash again writes nothing, the same version with other content — or a
version not above the latest — is ``409 process_version_conflict``. A new
version passes the check of :mod:`control_plane.domain.process_definition`
against the tenant's catalog (skills, task types, agents, calendars, the
versions already published); any error refuses it with ``422
invalid_process`` and every finding in ``details.problems``, warnings are kept
with the version. Each new version leaves ``process.definition_published``.
A new version runs under the latest engine revision
(:data:`process_engine.ENGINE_REVISION`); versions published before keep the
one they were published with (CP-ADR-0074, amendment 2026-09-29).

A process of a workspace (``spec.workspaceId``) is written and read under the
permissions on that workspace, one without it on the tenant.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    WorkspaceNotVisible,
    authorize,
    visible_objects,
)
from control_plane.application.commands.catalog_retirements import (
    CALENDAR,
    PROCESS,
    is_retired,
    lock_key,
    restore_key,
    retire_key,
    retired_keys,
    retirement,
    retirements,
    share_keys,
)
from control_plane.application.commands.work_rules import check_identity, ensure_consumer_cursor
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import event_reason, record_event
from control_plane.application.queries.package_links import in_package
from control_plane.application.queries.package_settings import object_scope
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.process_definition import (
    EXPRESSION_PROFILE,
    Catalog,
    Problem,
    SkillEntry,
    SpecError,
    check_process,
    definition_hash,
    normalized_spec,
    references,
)
from control_plane.domain.process_engine import ENGINE_REVISION
from control_plane.domain.settings_refs import SettingsScope
from control_plane.infrastructure.db.models import (
    Agent,
    ArtifactType,
    CalendarVersion,
    CatalogRetirement,
    ProcessDefinition,
    ProcessInstance,
    Skill,
    TaskType,
)

# The journal consumer of the process engine (event_consumer_cursors.name).
PROCESSES_CONSUMER = "processes"
# Instances that still run on their version (process_engine.RUNNING, SUSPENDED).
OPEN_STATUSES = ("running", "suspended")


@dataclass(frozen=True)
class ProcessDefinitionView:
    """A process version with the latest version of its key."""

    row: ProcessDefinition
    latest_version: int
    created: bool = False
    retirement: CatalogRetirement | None = None


def process_scope(workspace_id: uuid.UUID | None) -> ResourceRef | None:
    """Where ``processes.*`` is decided: the process's workspace, or the tenant."""
    return ResourceRef("workspace", str(workspace_id)) if workspace_id else None


def invalid_process(problems: list[Problem]) -> ValidationError:
    errors = [p for p in problems if p.error]
    first = errors[0] if errors else problems[0]
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return ValidationError(
        "invalid_process",
        f"The process definition is invalid: {first.message} at {first.path}{more}",
        details={"problems": [p.out() for p in problems]},
    )


def _workspace_of(spec: dict[str, Any]) -> uuid.UUID | None:
    value = spec.get("workspaceId")
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None  # the check reports it


async def _lock_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize publication for one (tenant, key): versions are compared in order."""
    await lock_key(session, tenant_id, PROCESS, key)


async def _latest(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> ProcessDefinition | None:
    row: ProcessDefinition | None = await session.scalar(
        select(ProcessDefinition)
        .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key)
        .order_by(ProcessDefinition.version.desc())
        .limit(1)
    )
    return row


async def _version(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, version: int
) -> ProcessDefinition | None:
    row: ProcessDefinition | None = await session.scalar(
        select(ProcessDefinition).where(
            ProcessDefinition.tenant_id == tenant_id,
            ProcessDefinition.key == key,
            ProcessDefinition.version == version,
        )
    )
    return row


async def engine_revision_for(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, spec: dict[str, Any]
) -> int:
    """The engine revision ``spec`` runs under once :func:`publish_process_definition` takes it.

    A spec equal to the published ``key@version`` is not published again: the
    publication returns that version, and it runs under the revision of its
    record. Any other spec becomes a new version under the latest revision.
    ``spec`` is in the stored form (:func:`normalized_spec`).
    """
    version = spec.get("version")
    if isinstance(version, int):
        row = await _version(session, tenant_id, key, version)
        if row is not None and row.definition_hash == definition_hash(spec):
            return row.engine_revision
    return ENGINE_REVISION


async def previous_version(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, version: Any
) -> ProcessDefinition | None:
    """The latest published version below ``version``: the one whose element ids it keeps."""
    stmt = select(ProcessDefinition).where(
        ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key
    )
    if isinstance(version, int):
        stmt = stmt.where(ProcessDefinition.version < version)
    row: ProcessDefinition | None = await session.scalar(
        stmt.order_by(ProcessDefinition.version.desc()).limit(1)
    )
    return row


async def load_catalog(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    spec: dict[str, Any],
    previous: ProcessDefinition | None,
    *,
    history_key: str | None = None,
    retired: bool = True,
    settings: SettingsScope | None = None,
) -> Catalog:
    """What the tenant's catalog holds of the keys ``spec`` names.

    ``history_key`` — the key whose versions the ``from`` side of the
    migration maps names: the old key of a process a package renames.
    ``retired=False`` — a version already published is compiled to run: the
    keys it names that were retired since do not stop its open instances.
    ``settings`` — the type of ``settings`` (CP-ADR-0081 §6); ``None``: the
    active revision of the package that installed ``key`` (``package_objects``).
    """
    refs = references(spec)
    skills: dict[str, SkillEntry] = {}
    pairs = [tuple(ref.split("@", 1)) for ref in refs.skills if "@" in ref]
    if pairs:
        for skill in await session.scalars(
            select(Skill).where(
                Skill.tenant_id == tenant_id, tuple_(Skill.name, Skill.version).in_(pairs)
            )
        ):
            skills[f"{skill.name}@{skill.version}"] = SkillEntry(
                skill.input_schema, skill.output_schema, skill.status
            )
    task_types: dict[str, Any] = {}
    if refs.task_types:
        latest = (
            select(TaskType.key, func.max(TaskType.version).label("version"))
            .where(TaskType.tenant_id == tenant_id, TaskType.key.in_(refs.task_types))
            .group_by(TaskType.key)
            .subquery()
        )
        for task_type in await session.scalars(
            select(TaskType)
            .join(latest, (TaskType.key == latest.c.key) & (TaskType.version == latest.c.version))
            .where(TaskType.tenant_id == tenant_id)
        ):
            task_types[task_type.key] = task_type.field_schema or None
    agents = await _keys(
        session,
        select(Agent.key).where(
            Agent.tenant_id == tenant_id, Agent.key.in_(refs.agents), Agent.status == "active"
        ),
        refs.agents,
    )
    calendars = await _keys(
        session,
        select(CalendarVersion.key).where(
            CalendarVersion.tenant_id == tenant_id, CalendarVersion.key.in_(refs.calendars)
        ),
        refs.calendars,
    )
    calendars_with_hours = await _calendars_with_hours(session, tenant_id, refs.calendars)
    artifact_types = await _keys(
        session,
        select(ArtifactType.key).where(
            ArtifactType.tenant_id == tenant_id, ArtifactType.key.in_(refs.artifact_types)
        ),
        refs.artifact_types,
    )
    processes = await _keys(
        session,
        select(ProcessDefinition.key).where(
            ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key.in_(refs.processes)
        ),
        refs.processes,
    )
    versions: dict[int, dict[str, Any]] = {}
    if refs.migration_versions:
        for row in await session.scalars(
            select(ProcessDefinition).where(
                ProcessDefinition.tenant_id == tenant_id,
                ProcessDefinition.key == (history_key or key),
                ProcessDefinition.version.in_(refs.migration_versions),
            )
        ):
            versions[row.version] = row.spec
    retired_calendars: frozenset[str] = frozenset()
    retired_processes: frozenset[str] = frozenset()
    if retired:
        retired_calendars = await retired_keys(session, tenant_id, CALENDAR, refs.calendars)
        retired_processes = await retired_keys(session, tenant_id, PROCESS, refs.processes)
    return Catalog(
        skills=skills,
        task_types=task_types,
        agents=agents,
        calendars=calendars,
        calendars_with_hours=calendars_with_hours,
        artifact_types=artifact_types,
        processes=processes,
        previous=previous.spec if previous is not None else None,
        versions=versions,
        retired_calendars=retired_calendars,
        retired_processes=retired_processes - {key},
        settings=(
            settings
            if settings is not None
            else await object_scope(session, tenant_id, PROCESS, key)
        ),
    )


async def _keys(session: AsyncSession, stmt: Select[Any], wanted: frozenset[str]) -> frozenset[str]:
    if not wanted:
        return frozenset()
    return frozenset((await session.scalars(stmt.distinct())).all())


async def _calendars_with_hours(
    session: AsyncSession, tenant_id: uuid.UUID, wanted: frozenset[str]
) -> frozenset[str]:
    """Keys of ``wanted`` whose latest version declares ``workingHours`` (CP-ADR-0078 §2)."""
    if not wanted:
        return frozenset()
    latest = (
        select(CalendarVersion.key, func.max(CalendarVersion.version).label("version"))
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key.in_(wanted))
        .group_by(CalendarVersion.key)
        .subquery()
    )
    stmt = (
        select(CalendarVersion.key)
        .join(
            latest,
            (CalendarVersion.key == latest.c.key) & (CalendarVersion.version == latest.c.version),
        )
        .where(
            CalendarVersion.tenant_id == tenant_id,
            CalendarVersion.spec.has_key("workingHours"),
        )
    )
    return frozenset((await session.scalars(stmt)).all())


def _normalized(spec: Any) -> dict[str, Any]:
    try:
        return normalized_spec(spec)
    except SpecError as exc:
        raise invalid_process(
            [Problem("invalid_document", "error", exc.path, exc.message)]
        ) from exc


async def publish_process_definition(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    spec: dict[str, Any],
    renamed_from: str | None = None,
    settings: SettingsScope | None = None,
) -> ProcessDefinitionView:
    """Publish ``spec.version`` of ``key``: immutable, checked, with its hash.

    ``renamed_from`` — a package renames that key to this one (CP-ADR-0074
    §11): its latest version keeps the element ids of the first version of
    ``key`` and its versions are the ``from`` side of the migrations.
    ``settings`` — the settings the apply of a package types ``settings`` by
    (the revision it records); ``None``: those of the package of ``key``.
    """
    from control_plane.application.commands.workspaces import require_active_workspace

    await authorize(ctx, Permission.PROCESSES_WRITE)
    body = _normalized(spec)
    body_hash = definition_hash(body)
    workspace_id = _workspace_of(body)
    if workspace_id is not None:
        await authorize(ctx, Permission.PROCESSES_WRITE, resource=process_scope(workspace_id))
    version = body.get("version")

    await _lock_key(session, ctx.tenant_id, key)
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is not None and isinstance(version, int):
        if latest.workspace_id is not None and latest.workspace_id != workspace_id:
            await _writable(ctx, latest)
        same = latest if latest.version == version else None
        if same is None and version <= latest.version:
            same = await _version(session, ctx.tenant_id, key, version)
        if same is not None and same.definition_hash == body_hash:
            return ProcessDefinitionView(same, latest.version)
        if version <= latest.version:
            raise ConflictError(
                "process_version_conflict",
                (
                    f"Version {version} of process {key!r} is published with other content"
                    if same is not None
                    else f"Version {version} is not above the latest version {latest.version}"
                ),
                details={
                    "key": key,
                    "version": version,
                    "latestVersion": latest.version,
                    **({"definitionHash": same.definition_hash} if same is not None else {}),
                },
            )

    previous = latest
    if latest is None and renamed_from is not None:
        previous = await _latest(session, ctx.tenant_id, renamed_from)
    # A calendar being retired is not named meanwhile (calendar_in_use, Zh3).
    await share_keys(session, ctx.tenant_id, CALENDAR, references(body).calendars)
    catalog = await load_catalog(
        session,
        ctx.tenant_id,
        key,
        body,
        previous,
        history_key=renamed_from if latest is None else None,
        settings=settings,
    )
    checked = check_process(key, body, catalog)
    if checked.errors:
        raise invalid_process(list(checked.problems))
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)
    agent_key = body["identity"]["agent"]
    await check_identity(session, ctx, agent_key, invalid_code="invalid_process")

    row = ProcessDefinition(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        key=key,
        version=version,
        display_name=body["displayName"],
        definition_hash=body_hash,
        identity_agent=agent_key,
        expression_profile=EXPRESSION_PROFILE,
        spec=body,
        governed_by=list(checked.governed_by),
        warnings=[p.out() for p in checked.warnings],
        engine_revision=ENGINE_REVISION,
        created_by=ctx.principal_id,
        created_at=utcnow(),
    )
    session.add(row)
    # A new version brings a retired key back into use (Zh1).
    await restore_key(session, ctx.tenant_id, PROCESS, key)
    await session.flush()
    # The engine reads the journal from the first published process on.
    await ensure_consumer_cursor(session, ctx.tenant_id, PROCESSES_CONSUMER)
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="process.definition_published",
        entity_type="process_definition",
        entity_id=row.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "version": version,
            "definitionHash": body_hash,
            "previousVersion": latest.version if latest is not None else None,
            "workspaceId": str(workspace_id) if workspace_id else None,
            "identityAgent": agent_key,
            "displayName": body["displayName"],
            "governedBy": [dict(item) for item in body.get("governedBy") or ()],
            "elements": [element.out() for element in checked.elements],
        },
    )
    return ProcessDefinitionView(row, row.version, created=True)


async def _readable(ctx: AuthContext, row: ProcessDefinition, not_found: NotFoundError) -> None:
    """A definition of an invisible workspace answers as a missing one (CP-ADR-0082 §3.7)."""
    if row.workspace_id is not None:
        try:
            await authorize(
                ctx, Permission.PROCESSES_READ, resource=process_scope(row.workspace_id)
            )
        except WorkspaceNotVisible:
            raise not_found from None


async def _writable(ctx: AuthContext, row: ProcessDefinition) -> None:
    """``processes.write`` on the definition's workspace; an invisible one is its 404."""
    try:
        await authorize(ctx, Permission.PROCESSES_WRITE, resource=process_scope(row.workspace_id))
    except WorkspaceNotVisible:
        raise NotFoundError("Process definition not found", details={"process": row.key}) from None


def _parse_ref(ref: str) -> tuple[str, int | None]:
    key, pinned, version_text = ref.partition("@")
    if not pinned:
        return key, None
    if not (version_text.isascii() and version_text.isdigit()) or len(version_text) > 9:
        raise NotFoundError("Process definition not found", details={"process": ref})
    return key, int(version_text)


async def resolve_process_definition(
    session: AsyncSession, ctx: AuthContext, ref: str
) -> ProcessDefinitionView:
    """``key`` (latest version) or ``key@version``."""
    await authorize(ctx, Permission.PROCESSES_READ)
    key, version = _parse_ref(ref)
    not_found = NotFoundError("Process definition not found", details={"process": ref})
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is None:
        raise not_found
    row = latest if version is None else await _version(session, ctx.tenant_id, key, version)
    if row is None:
        raise not_found
    await _readable(ctx, row, not_found)
    retired = await retirements(session, ctx.tenant_id, PROCESS, [key])
    return ProcessDefinitionView(row, latest.version, retirement=retired.get(key))


async def list_process_definitions(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int,
    after_key: str | None,
    key: str | None = None,
    workspace_id: uuid.UUID | None = None,
    governed_by: str | None = None,
    package: str | None = None,
    status: str | None = None,
) -> tuple[list[ProcessDefinitionView], str | None]:
    """The latest version of every key the caller may read, by key.

    ``governedBy`` keeps the processes whose latest version names the document;
    ``status`` — only keys in use (``active``) or only retired ones (Zh1).
    """
    await authorize(ctx, Permission.PROCESSES_READ)
    latest = (
        select(ProcessDefinition.key, func.max(ProcessDefinition.version).label("version"))
        .where(ProcessDefinition.tenant_id == ctx.tenant_id)
        .group_by(ProcessDefinition.key)
        .subquery()
    )
    stmt = (
        select(ProcessDefinition)
        .join(
            latest,
            (ProcessDefinition.key == latest.c.key)
            & (ProcessDefinition.version == latest.c.version),
        )
        .where(ProcessDefinition.tenant_id == ctx.tenant_id)
    )
    workspaces = await visible_objects(ctx, Permission.PROCESSES_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                ProcessDefinition.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                ProcessDefinition.workspace_id.is_(None),
            )
        )
    if key is not None:
        stmt = stmt.where(ProcessDefinition.key == key)
    if workspace_id is not None:
        stmt = stmt.where(ProcessDefinition.workspace_id == workspace_id)
    if governed_by is not None:
        stmt = stmt.where(ProcessDefinition.governed_by.contains([governed_by]))
    if package is not None:
        stmt = stmt.where(
            in_package("Process", ProcessDefinition.tenant_id, ProcessDefinition.key, package)
        )
    if status is not None:
        stmt = stmt.where(
            is_retired(PROCESS, ProcessDefinition.tenant_id, ProcessDefinition.key)
            if status == "retired"
            else ~is_retired(PROCESS, ProcessDefinition.tenant_id, ProcessDefinition.key)
        )
    if after_key is not None:
        stmt = stmt.where(ProcessDefinition.key > after_key)
    rows = list(
        (await session.scalars(stmt.order_by(ProcessDefinition.key).limit(limit + 1))).all()
    )
    next_key = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_key = rows[-1].key
    retired = await retirements(session, ctx.tenant_id, PROCESS, [row.key for row in rows])
    views = [
        ProcessDefinitionView(row, row.version, retirement=retired.get(row.key)) for row in rows
    ]
    return views, next_key


async def list_process_versions(
    session: AsyncSession,
    ctx: AuthContext,
    key: str,
    *,
    limit: int,
    before_version: int | None,
) -> tuple[list[ProcessDefinitionView], int | None]:
    """Versions of a key, newest first; the last version of a full page continues it."""
    await authorize(ctx, Permission.PROCESSES_READ)
    not_found = NotFoundError("Process definition not found", details={"process": key})
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is None:
        raise not_found
    await _readable(ctx, latest, not_found)
    stmt = select(ProcessDefinition).where(
        ProcessDefinition.tenant_id == ctx.tenant_id, ProcessDefinition.key == key
    )
    if before_version is not None:
        stmt = stmt.where(ProcessDefinition.version < before_version)
    rows = list(
        (
            await session.scalars(stmt.order_by(ProcessDefinition.version.desc()).limit(limit + 1))
        ).all()
    )
    next_version = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_version = rows[-1].version
    retired = (await retirements(session, ctx.tenant_id, PROCESS, [key])).get(key)
    views = [ProcessDefinitionView(row, latest.version, retirement=retired) for row in rows]
    return views, next_version


# --- retirement (CP-ADR-0074, amendment Zh2) ------------------------------------------------


@dataclass(frozen=True)
class ProcessRetirementView:
    key: str
    retirement: CatalogRetirement
    by_version: list[tuple[int, int]]

    @property
    def open_instances(self) -> int:
        return sum(count for _, count in self.by_version)


async def open_instances_by_version(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> list[tuple[int, int]]:
    """Open (running, suspended) instances of every workspace of the key, by version."""
    rows = await session.execute(
        select(ProcessInstance.definition_version, func.count())
        .where(
            ProcessInstance.tenant_id == tenant_id,
            ProcessInstance.definition_key == key,
            ProcessInstance.status.in_(OPEN_STATUSES),
        )
        .group_by(ProcessInstance.definition_version)
        .order_by(ProcessInstance.definition_version)
    )
    return [(int(version), int(count)) for version, count in rows.all()]


async def retire_process_definition(
    session: AsyncSession, ctx: AuthContext, *, key: str, reason: str, dry_run: bool = False
) -> ProcessRetirementView:
    """Every version of the key is retired: no new instances, open ones run to the end.

    A key already retired answers with its first retirement and writes nothing;
    ``dry_run`` makes the same checks and the same answer without writing.
    """
    await authorize(ctx, Permission.PROCESSES_WRITE)
    await _lock_key(session, ctx.tenant_id, key)
    latest = await _latest(session, ctx.tenant_id, key)
    if latest is None:
        raise NotFoundError("Process definition not found", details={"process": key})
    await _writable(ctx, latest)
    by_version = await open_instances_by_version(session, ctx.tenant_id, key)
    existing = await retirement(session, ctx.tenant_id, PROCESS, key)
    if existing is not None:
        return ProcessRetirementView(key, existing, by_version)
    text = event_reason(reason)
    if dry_run:
        planned = CatalogRetirement(
            tenant_id=ctx.tenant_id,
            kind=PROCESS,
            key=key,
            retired_at=utcnow(),
            retired_by=ctx.principal_id,
            reason=text,
        )
        return ProcessRetirementView(key, planned, by_version)
    row = await retire_key(
        session, ctx.tenant_id, PROCESS, key, by=ctx.principal_id, reason=text, at=utcnow()
    )
    view = ProcessRetirementView(key, row, by_version)
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="process.definition_retired",
        entity_type="process_definition",
        entity_id=latest.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "latestVersion": latest.version,
            "workspaceId": str(latest.workspace_id) if latest.workspace_id else None,
            "reason": text,
            "openInstances": view.open_instances,
            "byVersion": [
                {"version": version, "openInstances": count} for version, count in by_version
            ],
        },
    )
    return view
