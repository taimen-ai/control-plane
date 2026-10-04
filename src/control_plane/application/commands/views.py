"""Views of a package in the catalog: checked by the plan, written by the apply (CP-ADR-0080).

A view is published only by ``POST /packages:apply`` (TAI-ADR-0066): the
apply holds the tenant's apply lock (``package_links.lock_applies``), so the
rows of ``views`` have no writer to race. What a plan and an apply do with a
view of the package:

- **check** — :func:`view_context` reads what the views name from the package
  and the catalog (processes, task types, roles, skills, the views of the
  packages it requires) and :func:`check_package` runs the pure check of every view and
  component (:mod:`control_plane.domain.views`); an error is a finding of the
  plan, and an apply with one is refused;
- **create / update** — a new revision with the form and its hash,
  ``view.published``; **restore** — a retired view the package brings again as
  it is: back in use, ``view.published`` of its revision;
- **retire** — a view the package installed and no longer brings:
  ``view.retired`` (the console drops what it cached).

The dictionaries of the package are kept with it, a revision per change
(:func:`record_dictionaries`).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.package_catalog import Latest
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.graph import deadline_after, within
from control_plane.application.events import record_event
from control_plane.application.queries.recall import graph_scope
from control_plane.config import Settings
from control_plane.domain.enums import TaskTypeStatus
from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.package_source import PackageObject, ParsedPackage
from control_plane.domain.process_definition import Problem, SkillEntry, package_skill
from control_plane.domain.settings_refs import NONE, SettingsScope
from control_plane.domain.views import (
    COMPONENT,
    KNOWLEDGE_UNCHECKED,
    VIEW,
    KnowledgeCatalog,
    ProcessShape,
    ViewCheck,
    ViewContext,
    check_component,
    check_knowledge,
    check_locales,
    check_view,
    declared_locales,
    knowledge_names,
    message_places,
    references,
    task_shape,
    unused_messages,
)
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.models import (
    PackageDictionary,
    ProcessDefinition,
    Role,
    Skill,
    TaskType,
    View,
    ViewRevision,
)
from control_plane.infrastructure.db.models import (
    PackageObject as PackageRecord,
)

ACTIVE = "active"
RETIRED = "retired"
ENTITY_TYPE = "view"


def _process_shape(spec: Mapping[str, Any]) -> ProcessShape:
    data = spec.get("data")
    stages = tuple(
        str(stage["id"])
        for stage in spec.get("stages") or ()
        if isinstance(stage, Mapping) and isinstance(stage.get("id"), str)
    )
    return ProcessShape(data if isinstance(data, Mapping) else None, stages)


def required_packages(package: ParsedPackage) -> list[str]:
    """The keys ``requires`` of ``package.yaml`` names (a key or ``{package, version}``)."""
    out: list[str] = []
    for item in (package.manifest or {}).get("requires") or ():
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("package"), str):
            out.append(item["package"])
    return out


async def view_context(
    db: AsyncSession, tenant_id: uuid.UUID, package: ParsedPackage
) -> ViewContext:
    """What the views of ``package`` are checked against: the package and the catalog."""
    named = references(package)
    locales, default = declared_locales(package)
    processes = {obj.key: _process_shape(obj.spec) for obj in package.of_kind("Process")}
    wanted = sorted(named.processes - set(processes))
    if wanted:
        latest = (
            select(ProcessDefinition.key, func.max(ProcessDefinition.version).label("version"))
            .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key.in_(wanted))
            .group_by(ProcessDefinition.key)
            .subquery()
        )
        rows = await db.scalars(
            select(ProcessDefinition)
            .join(
                latest,
                (ProcessDefinition.key == latest.c.key)
                & (ProcessDefinition.version == latest.c.version),
            )
            .where(ProcessDefinition.tenant_id == tenant_id)
        )
        processes.update({row.key: _process_shape(row.spec) for row in rows})
    task_shapes = {
        obj.key: task_shape(
            obj.spec.get("fieldSchema"), obj.spec.get("lifecycleSchema") or SYSTEM_TASK_LIFECYCLE
        )
        for obj in package.of_kind("TaskType")
    }
    task_types = set(task_shapes)
    wanted = sorted(named.task_types - task_types)
    if wanted:
        # The latest active version of each: what a task of the type is created by.
        for row in await db.scalars(
            select(TaskType)
            .where(
                TaskType.tenant_id == tenant_id,
                TaskType.key.in_(wanted),
                TaskType.status == TaskTypeStatus.ACTIVE,
            )
            .order_by(TaskType.key, TaskType.version.desc())
        ):
            if row.key not in task_shapes:
                task_types.add(row.key)
                task_shapes[row.key] = task_shape(
                    row.field_schema, row.lifecycle_schema or SYSTEM_TASK_LIFECYCLE
                )
    roles = {obj.key for obj in package.of_kind("Role")}
    wanted = sorted(named.roles - roles)
    if wanted:
        roles |= set(
            await db.scalars(
                select(Role.slug).where(Role.tenant_id == tenant_id, Role.slug.in_(wanted))
            )
        )
    skills: dict[str, SkillEntry] = {
        f"{obj.key}@{obj.spec.get('version')}": package_skill(obj.spec)
        for obj in package.of_kind("Skill")
    }
    pairs = [tuple(ref.split("@", 1)) for ref in sorted(named.skills - set(skills)) if "@" in ref]
    if pairs:
        for skill in await db.scalars(
            select(Skill).where(
                Skill.tenant_id == tenant_id, tuple_(Skill.name, Skill.version).in_(pairs)
            )
        ):
            skills[f"{skill.name}@{skill.version}"] = SkillEntry(
                skill.input_schema, skill.output_schema, skill.status
            )
    views = {obj.key for obj in package.of_kind(VIEW)}
    required = required_packages(package)
    wanted = sorted(named.views - views)
    if wanted and required:
        views |= set(
            await db.scalars(
                select(PackageRecord.key)
                .join(
                    View,
                    (View.tenant_id == PackageRecord.tenant_id) & (View.key == PackageRecord.key),
                )
                .where(
                    PackageRecord.tenant_id == tenant_id,
                    PackageRecord.kind == VIEW,
                    PackageRecord.key.in_(wanted),
                    PackageRecord.package_key.in_(required),
                    View.status == ACTIVE,
                )
            )
        )
    return ViewContext(
        locales=locales,
        default_locale=default,
        dictionaries={loc: d.messages for loc, d in package.dictionaries.items()},
        processes=processes,
        task_types=frozenset(task_types),
        task_shapes={key: task_shapes[key] for key in sorted(named.task_types & task_types)},
        roles=frozenset(roles),
        skills=skills,
        views=frozenset(views),
        component_keys=frozenset(obj.key for obj in package.of_kind(COMPONENT)),
        package=package.manifest_object.key if package.manifest_object else "",
    )


@dataclass
class PackageViews:
    """The findings of the screens of a package and the form of each view without one."""

    problems: list[Problem] = field(default_factory=list)
    checks: dict[str, ViewCheck] = field(default_factory=dict)


async def check_package(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    package: ParsedPackage,
    *,
    shown: frozenset[str] | set[str] = frozenset(),
    settings: SettingsScope = NONE,
) -> PackageViews:
    """Locales, dictionaries, components and views of ``package``, checked.

    ``shown`` — keys of the dictionaries something else of the package shows
    (the labels of its settings, CP-ADR-0081): not unused; ``settings`` — the
    settings the package declares, the type of ``settings`` of its views (§6).
    """
    out = PackageViews(problems=check_locales(package))
    views, components = package.of_kind(VIEW), package.of_kind(COMPONENT)
    if not views and not components and not package.dictionaries:
        return out
    context = replace(await view_context(db, tenant_id, package), settings=settings)
    usable: dict[str, PackageObject] = {}
    for obj in sorted(components, key=lambda o: o.key):
        found = check_component(obj, context)
        out.problems.extend(found)
        if not any(p.error for p in found):
            usable[obj.key] = obj
    context = replace(context, components=usable)
    used: set[str] = set(shown)
    for obj in sorted(views, key=lambda o: o.key):
        checked = check_view(obj, context)
        out.problems.extend(checked.problems)
        out.checks[obj.key] = checked
        used |= checked.messages
    for obj in components:
        used |= {key for _, key in message_places(obj.spec)}
    out.problems.extend(unused_messages(package, used))
    return out


# --- the catalog -----------------------------------------------------------------------------


async def latest_view(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> Latest | None:
    view = await db.scalar(select(View).where(View.tenant_id == tenant_id, View.key == key))
    if view is None:
        return None
    revision = await db.scalar(
        select(ViewRevision).where(
            ViewRevision.view_id == view.id, ViewRevision.revision == view.current_revision
        )
    )
    assert revision is not None  # a view always has its current revision
    return Latest(
        view.current_revision, revision.hash, revision.spec, view, retired=view.status == RETIRED
    )


async def linked_views(db: AsyncSession, tenant_id: uuid.UUID, package_key: str) -> list[str]:
    """The views in use the package ``package_key`` installed."""
    return sorted(
        await db.scalars(
            select(PackageRecord.key)
            .join(
                View, (View.tenant_id == PackageRecord.tenant_id) & (View.key == PackageRecord.key)
            )
            .where(
                PackageRecord.tenant_id == tenant_id,
                PackageRecord.kind == VIEW,
                PackageRecord.package_key == package_key,
                View.status == ACTIVE,
            )
        )
    )


def _source(form: Mapping[str, Any]) -> tuple[str, str | None]:
    source = form["source"]
    if "process" in source:
        return "process", str(source["process"])
    if "tasks" in source:
        return "tasks", str(source["tasks"]["type"])
    return "knowledge", None


async def publish_view(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    form: dict[str, Any],
    latest: Latest | None,
    package: tuple[str, str] | None,
) -> int:
    """A new revision of ``key`` with ``form``; the view is in use. Its revision."""
    now = utcnow()
    view: View | None = latest.row if latest is not None else None
    revision = (latest.version + 1) if latest is not None else 1
    if view is None:
        view = View(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            key=key,
            current_revision=revision,
            status=ACTIVE,
            created_at=now,
            updated_at=now,
        )
        db.add(view)
        await db.flush()
    source_kind, source_key = _source(form)
    audience = form.get("audience")
    form_hash = canonical_hash(form)
    db.add(
        ViewRevision(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            view_id=view.id,
            revision=revision,
            hash=form_hash,
            spec=form,
            source_kind=source_kind,
            source_key=source_key,
            audience_roles=list(audience["roles"]) if isinstance(audience, Mapping) else None,
            package_key=package[0] if package else None,
            package_version=package[1] if package else None,
            created_by=ctx.principal_id,
            created_at=now,
        )
    )
    view.current_revision = revision
    view.status, view.retired_at, view.retired_by = ACTIVE, None, None
    view.updated_at = now
    await db.flush()
    await _published(db, ctx, view, form_hash, latest.version if latest else None, package)
    return revision


async def restore_view(
    db: AsyncSession, ctx: AuthContext, *, latest: Latest, package: tuple[str, str] | None
) -> None:
    """A retired view the package brings again as it is: back in use at its revision."""
    view: View = latest.row
    view.status, view.retired_at, view.retired_by = ACTIVE, None, None
    view.updated_at = utcnow()
    await db.flush()
    await _published(db, ctx, view, latest.hash, latest.version, package)


async def retire_view(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    latest: Latest,
    package: tuple[str, str] | None,
    reason: str,
) -> None:
    view: View = latest.row
    now = utcnow()
    view.status, view.retired_at, view.retired_by = RETIRED, now, ctx.principal_id
    view.updated_at = now
    await db.flush()
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="view.retired",
        entity_type=ENTITY_TYPE,
        entity_id=view.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": view.key,
            "revision": view.current_revision,
            "reason": reason,
            "packageKey": package[0] if package else None,
            "packageVersion": package[1] if package else None,
        },
    )


async def _published(
    db: AsyncSession,
    ctx: AuthContext,
    view: View,
    form_hash: str,
    previous: int | None,
    package: tuple[str, str] | None,
) -> None:
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="view.published",
        entity_type=ENTITY_TYPE,
        entity_id=view.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": view.key,
            "revision": view.current_revision,
            "hash": form_hash,
            "previousRevision": previous,
            "packageKey": package[0] if package else None,
            "packageVersion": package[1] if package else None,
        },
    )


async def record_dictionaries(
    db: AsyncSession,
    ctx: AuthContext,
    package: ParsedPackage,
    package_key: str,
    version: str | None,
) -> int | None:
    """Keep the dictionaries of the package: a new revision when they changed. Its number."""
    locales, default = declared_locales(package)
    if not locales or default is None:
        return None
    body = {
        "locales": list(locales),
        "defaultLocale": default,
        "messages": {
            locale: dict(sorted(package.dictionaries[locale].messages.items()))
            for locale in locales
            if locale in package.dictionaries
        },
    }
    body_hash = canonical_hash(body)
    latest = await db.scalar(
        select(PackageDictionary)
        .where(
            PackageDictionary.tenant_id == ctx.tenant_id,
            PackageDictionary.package_key == package_key,
        )
        .order_by(PackageDictionary.revision.desc())
        .limit(1)
    )
    if latest is not None and latest.hash == body_hash:
        return latest.revision
    revision = (latest.revision + 1) if latest is not None else 1
    db.add(
        PackageDictionary(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            package_key=package_key,
            revision=revision,
            package_version=version,
            locales=body["locales"],
            default_locale=default,
            messages=body["messages"],
            hash=body_hash,
            created_by=ctx.principal_id,
            created_at=utcnow(),
        )
    )
    await db.flush()
    return revision


# --- the ontology of the views of knowledge (stage 6) -----------------------------------------


async def knowledge_namespace(
    db: AsyncSession, ctx: AuthContext, settings: Settings, workspace_id: uuid.UUID | None
) -> str | None:
    """The namespace of the tree whose ontology a plan checks the views of knowledge against.

    The tree of the workspace the plan is asked for; ``None`` — no workspace,
    or the caller may not read the knowledge of its tree (transactional half).
    """
    if workspace_id is None:
        return None
    scope = await graph_scope(db, ctx, settings, workspace_id)
    # The tenant's namespace first, the root of the tree after it when the caller may read it.
    return scope.namespaces[-1] if len(scope.namespaces) > 1 else None


async def knowledge_catalog(
    provider: GraphProvider, namespace: str, settings: Settings, *, trace_run_id: str = ""
) -> KnowledgeCatalog:
    """The kinds (with the schemas of their attributes) and relations Memory has for a namespace.

    A kind is the first pack's that declares it, in the order the namespace
    enables them, as Memory's catalog decides it.
    """
    deadline = deadline_after(settings)
    trace = trace_run_id or None
    body = await within(deadline, provider.namespace_kinds(namespace=namespace, trace_run_id=trace))
    catalog = body.get("catalog") if isinstance(body, Mapping) else None
    catalog = catalog if isinstance(catalog, Mapping) else {}
    names = [k for k in (catalog.get("base_kinds") or []) + (catalog.get("kinds") or []) if k]
    kinds: dict[str, Mapping[str, Any] | None] = {str(k): None for k in names}
    seen: set[str] = set()
    for ref in catalog.get("packages") or []:
        if not isinstance(ref, str) or not ref:
            continue
        name, _, version = ref.partition("@")
        pack = await within(
            deadline,
            provider.get_package(
                name=name, version=version, namespace=namespace, trace_run_id=trace
            ),
        )
        for spec in pack.get("kinds") or []:
            kind = spec.get("kind") if isinstance(spec, Mapping) else None
            if not isinstance(kind, str) or kind in seen or kind not in kinds:
                continue
            seen.add(kind)
            attributes = spec.get("attributes")
            kinds[kind] = attributes if isinstance(attributes, Mapping) else None
    aliases = catalog.get("kindAliases")
    return KnowledgeCatalog(
        kinds=kinds,
        aliases={str(k): str(v) for k, v in aliases.items()}
        if isinstance(aliases, Mapping)
        else {},
        relations=frozenset(str(r) for r in catalog.get("relations") or [] if r),
    )


async def knowledge_problems(
    provider: GraphProvider | None,
    namespace: str | None,
    views: Sequence[tuple[PackageObject, Mapping[str, Any]]],
    settings: Settings,
    *,
    asked_workspace: bool,
    trace_run_id: str = "",
) -> list[Problem]:
    """Warnings of the views (``(object, form)``) that name the knowledge base, against the
    ontology of the tree the plan is asked for (CP-ADR-0080, amendment Б3)."""
    named = [(obj, form) for obj, form in views if knowledge_names(form)]
    if not named:
        return []
    reason: str | None = None
    if not asked_workspace:
        reason = "the plan names no workspaceId whose tree's ontology to check them against"
    elif namespace is None:
        reason = "the caller may not read the knowledge of the workspace's tree"
    elif provider is None:
        reason = "memory is not configured"
    if reason is None:
        assert provider is not None and namespace is not None
        try:
            catalog = await knowledge_catalog(
                provider, namespace, settings, trace_run_id=trace_run_id
            )
        except TimeoutError:
            reason = "memory did not answer in time"
        except ContextProviderError:
            reason = "memory failed to answer"
    if reason is not None:
        return [
            Problem(
                KNOWLEDGE_UNCHECKED,
                "warning",
                "",
                "the kinds, attributes and relations of the views of knowledge were not checked:"
                f" {reason}",
                hint="plan with the workspaceId of the tree the views are read in",
            )
        ]
    problems: list[Problem] = []
    for obj, form in named:
        problems.extend(check_knowledge(obj, form, catalog))
    return problems
