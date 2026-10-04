"""Work item type versions: create, deprecate, resolve (ADR-0048).

A type version is immutable from the moment it is written, exactly like a
project template (ADR-0030): the only mutation the database trigger permits is
``active -> deprecated``. Editing a type therefore always means "create the
next version", and a task keeps pointing at the exact version it was created
against — which is what makes it safe to let a tenant reshape its process
while work is in flight. Moving an open task to another version of its key is
a separate, explicit action (``task_type_migration``, ADR-0048 amendment
2026-09-30).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.role_references import (
    ROLE_REFERENCE_PREFIX,
    is_role_reference,
    normalize_executor_roles,
    require_declared_role,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.agent_instructions import validate_instructions
from control_plane.domain.approval_outcomes import (
    REQUEST_APPROVAL,
    ApprovalSchema,
    parse_approval_schema,
    skill_calls,
)
from control_plane.domain.artifact_schema import (
    ArtifactSchema,
    check_against_artifact_types,
    parse_artifact_schema,
)
from control_plane.domain.completion_work import parse_completion_schema
from control_plane.domain.context_schema import parse_context_schema
from control_plane.domain.enums import (
    Permission,
    SkillSideEffects,
    SkillStatus,
    TaskTypeStatus,
)
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.project import validate_json_schema_document
from control_plane.domain.task_execution import normalize_execution
from control_plane.domain.work_graph import normalize_checks
from control_plane.domain.work_item import (
    SYSTEM_TASK_LIFECYCLE,
    SYSTEM_TASK_TYPE_DISPLAY_NAME,
    SYSTEM_TASK_TYPE_KEY,
    TERMINAL_CATEGORIES,
    WorkItemLifecycle,
    parse_work_item_lifecycle,
)
from control_plane.infrastructure.db.models import ArtifactType, Skill, Task, TaskType


async def _lock_type_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize version allocation for one (tenant, key)."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:tt:{tenant_id}:{key}", 0)))
    )


def task_type_lifecycle(task_type: TaskType) -> WorkItemLifecycle:
    return parse_work_item_lifecycle(task_type.lifecycle_schema)


async def ensure_system_task_type(
    session: AsyncSession, tenant_id: uuid.UUID, created_by: uuid.UUID
) -> TaskType:
    """The per-tenant fallback type, created once at bootstrap/migration."""
    existing = await session.scalar(
        select(TaskType)
        .where(TaskType.tenant_id == tenant_id, TaskType.key == SYSTEM_TASK_TYPE_KEY)
        .order_by(TaskType.version.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    now = utcnow()
    task_type = TaskType(
        id=new_uuid(),
        tenant_id=tenant_id,
        key=SYSTEM_TASK_TYPE_KEY,
        version=1,
        display_name=SYSTEM_TASK_TYPE_DISPLAY_NAME,
        description="System work item type.",
        field_schema={},
        lifecycle_schema=SYSTEM_TASK_LIFECYCLE,
        status=TaskTypeStatus.ACTIVE,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    session.add(task_type)
    await session.flush()
    return task_type


async def get_tenant_task_type(
    session: AsyncSession, ctx: AuthContext, type_id: uuid.UUID
) -> TaskType:
    task_type = await session.scalar(
        select(TaskType).where(TaskType.id == type_id, TaskType.tenant_id == ctx.tenant_id)
    )
    if task_type is None:
        raise NotFoundError("Task type not found", details={"taskTypeId": str(type_id)})
    return task_type


async def resolve_task_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_id: uuid.UUID | None = None,
    type_key: str | None = None,
    type_version: int | None = None,
) -> TaskType:
    """By id, or by key (+optional version; newest active version by default).

    With nothing supplied this is the tenant's system type — the reason a
    pre-v0.8 client that never heard of task types keeps working.
    """
    if type_id is not None:
        return await get_tenant_task_type(session, ctx, type_id)
    key = type_key or SYSTEM_TASK_TYPE_KEY
    stmt = select(TaskType).where(TaskType.tenant_id == ctx.tenant_id, TaskType.key == key)
    if type_version is not None:
        stmt = stmt.where(TaskType.version == type_version)
    else:
        stmt = stmt.where(TaskType.status == TaskTypeStatus.ACTIVE)
    task_type = await session.scalar(stmt.order_by(TaskType.version.desc()).limit(1))
    if task_type is None:
        raise NotFoundError(
            "Task type not found",
            details={"taskTypeKey": key, "taskTypeVersion": type_version},
        )
    return task_type


async def task_type_of(session: AsyncSession, task: Task) -> TaskType:
    """The type a (already loaded) task carries; NOT NULL makes this total."""
    task_type = await session.get(TaskType, task.type_id)
    if task_type is None:  # pragma: no cover - forbidden by the foreign key
        raise NotFoundError("Task type not found", details={"taskTypeId": str(task.type_id)})
    return task_type


async def lifecycle_of(session: AsyncSession, task: Task) -> WorkItemLifecycle:
    return task_type_lifecycle(await task_type_of(session, task))


async def _require_executable_skill(
    session: AsyncSession, ctx: AuthContext, execution: dict[str, Any]
) -> None:
    """``execution`` must name a published, invocable skill version.

    Checked once, at publication, like everything else a type version fixes.
    The skill may be disabled later; its tasks then fail at invocation with
    ``skill_not_invocable`` instead of silently doing nothing.
    """
    skill = await session.scalar(
        select(Skill).where(
            Skill.tenant_id == ctx.tenant_id,
            Skill.name == execution["skill"],
            Skill.version == execution["version"],
        )
    )
    reason: str | None = None
    if skill is None:
        reason = "not_found"
    elif skill.contract is None:
        reason = "no_contract"
    elif skill.status == SkillStatus.DISABLED:
        reason = "disabled"
    if reason is not None:
        raise ValidationError(
            "invalid_task_execution",
            "execution must name an invocable skill version",
            details={
                "field": "execution.skill",
                "skill": f"{execution['skill']}@{execution['version']}",
                "reason": reason,
            },
        )


async def _check_outcome_skills(
    session: AsyncSession, ctx: AuthContext, schema: ApprovalSchema
) -> None:
    """What an outcome's ``invokeSkill`` may call, checked against the registry.

    The grammar cannot see a skill's side effects; the registry can. A skill
    that writes outside (``external_write``) is called only by
    ``name@version`` — the type version is immutable, the skill it writes
    through must be too — and never from the ``rejected`` outcome, whose
    approval is not a basis for an external write (ADR-0056 §4). An unpinned
    name must name a registered skill none of whose versions writes outside;
    a pinned one, a registered version.
    """
    for call in skill_calls(schema):
        base = select(Skill).where(Skill.tenant_id == ctx.tenant_id, Skill.name == call.name)
        if call.version is not None:
            base = base.where(Skill.version == call.version)
        versions = (await session.scalars(base)).all()
        ref = f"{call.name}@{call.version}" if call.version else call.name
        reason: str | None = None
        writes = any(v.side_effects == SkillSideEffects.EXTERNAL_WRITE for v in versions)
        if not versions:
            reason = "not_found"
        elif writes and call.version is None:
            reason = "external_write_not_pinned"
        elif writes and call.outcome == "rejected":
            reason = "external_write_on_rejected"
        if reason is not None:
            raise ValidationError(
                "invalid_approval_schema",
                f"{call.path}: {ref!r} cannot be invoked here ({reason})",
                details={"path": call.path, "skill": ref, "reason": reason},
            )


async def _check_artifact_types(
    session: AsyncSession, ctx: AuthContext, schema: ArtifactSchema
) -> None:
    """The artifact types ``schema`` names exist in the tenant (latest versions)."""
    keys = schema.artifact_types()
    if not keys:
        return
    rows = (
        await session.execute(
            select(ArtifactType.key, ArtifactType.media_types)
            .where(ArtifactType.tenant_id == ctx.tenant_id, ArtifactType.key.in_(keys))
            .order_by(ArtifactType.key, ArtifactType.version.desc())
            .distinct(ArtifactType.key)
        )
    ).all()
    check_against_artifact_types(schema, {key: list(media) for key, media in rows})


def _gate_role_references(document: Any, path: str) -> Iterator[tuple[str, str]]:
    """``(field, role:<slug>)`` of each literal ``requestApproval.assignee`` in ``document``."""
    if isinstance(document, dict):
        gate = document.get(REQUEST_APPROVAL)
        if isinstance(gate, dict):
            assignee = gate.get("assignee")
            # A template is only known when it renders: the action checks it then.
            if is_role_reference(assignee) and "$." not in assignee:
                yield f"{path}.{REQUEST_APPROVAL}.assignee", assignee
        for key, value in document.items():
            yield from _gate_role_references(value, f"{path}.{key}")
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _gate_role_references(value, f"{path}[{index}]")


async def _check_gate_roles(
    session: AsyncSession, ctx: AuthContext, documents: dict[str, dict[str, Any]]
) -> None:
    """A gate addressed to ``role:<slug>`` names a role the tenant has (``unknown_role``)."""
    for name, document in documents.items():
        for field, reference in _gate_role_references(document, name):
            await require_declared_role(session, ctx.tenant_id, reference, field=field)


async def _type_checks(
    session: AsyncSession, ctx: AuthContext, acceptance: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    from control_plane.application.commands.verification import check_acceptance_skills

    checks = normalize_checks(acceptance, field="acceptance")
    await check_acceptance_skills(session, ctx, checks)
    return checks


async def _executor_roles(session: AsyncSession, ctx: AuthContext, slugs: list[str]) -> list[str]:
    """Roles a person needs to take work of the version: each one the tenant has
    (CP-ADR-0048, amendment 2026-10-03 A1), like a ``role:<slug>`` gate."""
    slugs = normalize_executor_roles(slugs)
    for index, slug in enumerate(slugs):
        await require_declared_role(
            session,
            ctx.tenant_id,
            f"{ROLE_REFERENCE_PREFIX}{slug}",
            field=f"executorRoles[{index}]",
        )
    return list(slugs)


@dataclass(frozen=True)
class TypeDocuments:
    """The documents of a type version, checked by their grammar alone."""

    field_schema: dict[str, Any]
    lifecycle_schema: dict[str, Any]
    lifecycle: WorkItemLifecycle
    approval_schema: dict[str, Any]
    outcomes: ApprovalSchema
    context_schema: dict[str, Any]
    instructions: str
    completion_schema: dict[str, Any]
    artifact_schema: dict[str, Any]
    artifact_io: ArtifactSchema


def check_type_documents(
    *,
    field_schema: dict[str, Any] | None = None,
    lifecycle_schema: dict[str, Any] | None = None,
    approval_schema: dict[str, Any] | None = None,
    context_schema: dict[str, Any] | None = None,
    instructions: str | None = None,
    completion_schema: dict[str, Any] | None = None,
    artifact_schema: dict[str, Any] | None = None,
) -> TypeDocuments:
    """What a type version fixes, checked by the pure functions of the domain.

    Everything :func:`create_task_type_version` refuses without asking the
    registries; ``packages:test`` checks a package's types by it too
    (CP-ADR-0074 Z4).
    """
    schema = field_schema or {}
    validate_json_schema_document(schema, field_name="fieldSchema")
    document = lifecycle_schema or SYSTEM_TASK_LIFECYCLE
    # Everything that could make the type unusable is rejected here rather
    # than when a task carrying it tries to move.
    lifecycle = parse_work_item_lifecycle(document)
    # Outcomes are checked against the closed action vocabulary and expression
    # grammar now, so a decided approval never meets a schema it cannot run
    # (CP-ADR-0061).
    outcomes = approval_schema or {}
    categories = lifecycle.lifecycle.categories
    parsed = parse_approval_schema(
        outcomes,
        statuses=frozenset(categories),
        terminal=frozenset(k for k, c in categories.items() if c in TERMINAL_CATEGORIES),
    )
    # The context profile is checked against its grammar and Memory's limits
    # now (CP-ADR-0064); kinds and relations are the domain packs' names and
    # are not known to core.
    profile = context_schema or {}
    parse_context_schema(profile)
    # Executor instructions are checked for size and pasted credentials now
    # (CP-ADR-0066): the version is immutable, a secret in it would stay.
    text = validate_instructions(instructions, field="instructions")
    # Work after completion (CP-ADR-0061, amendment 2026-09-25): the same
    # closed vocabulary, checked now for the same reason as the outcomes.
    after_completion = completion_schema or {}
    parse_completion_schema(after_completion)
    # Inputs and outputs (CP-ADR-0072 §7): the artifact types they name must
    # be registered — which the caller asks the registry; their version is
    # not pinned, an artifact is checked against the latest one.
    handoff = artifact_schema or {}
    io = parse_artifact_schema(handoff)
    return TypeDocuments(
        field_schema=schema,
        lifecycle_schema=document,
        lifecycle=lifecycle,
        approval_schema=outcomes,
        outcomes=parsed,
        context_schema=profile,
        instructions=text,
        completion_schema=after_completion,
        artifact_schema=handoff,
        artifact_io=io,
    )


async def create_task_type_version(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    display_name: str,
    description: str = "",
    field_schema: dict[str, Any] | None = None,
    lifecycle_schema: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    approval_schema: dict[str, Any] | None = None,
    context_schema: dict[str, Any] | None = None,
    instructions: str | None = None,
    completion_schema: dict[str, Any] | None = None,
    artifact_schema: dict[str, Any] | None = None,
    acceptance: list[dict[str, Any]] | None = None,
    executor_roles: list[str] | None = None,
) -> TaskType:
    """Create the next version of ``key`` — never an in-place edit."""
    await authorize(ctx, Permission.TASK_TYPES_MANAGE)
    await _lock_type_key(session, ctx.tenant_id, key)
    normalized_execution = normalize_execution(execution)
    if normalized_execution is not None:
        await _require_executable_skill(session, ctx, normalized_execution)

    checked = check_type_documents(
        field_schema=field_schema,
        lifecycle_schema=lifecycle_schema,
        approval_schema=approval_schema,
        context_schema=context_schema,
        instructions=instructions,
        completion_schema=completion_schema,
        artifact_schema=artifact_schema,
    )
    schema, document, lifecycle = checked.field_schema, checked.lifecycle_schema, checked.lifecycle
    outcomes, profile, text = checked.approval_schema, checked.context_schema, checked.instructions
    after_completion, handoff, io = (
        checked.completion_schema,
        checked.artifact_schema,
        checked.artifact_io,
    )
    await _check_outcome_skills(session, ctx, checked.outcomes)
    await _check_gate_roles(
        session,
        ctx,
        {"approvalSchema": outcomes, "completionSchema": after_completion},
    )
    await _check_artifact_types(session, ctx, io)
    # Default checks of every task of the version (CP-ADR-0067, amendment
    # 2026-09-27): the grammar and registries of a task's acceptance, now —
    # the version is immutable, and its tasks would carry a check nobody can run.
    checks = await _type_checks(session, ctx, acceptance or [])
    roles = await _executor_roles(session, ctx, executor_roles or [])

    current_max = await session.scalar(
        select(func.max(TaskType.version)).where(
            TaskType.tenant_id == ctx.tenant_id, TaskType.key == key
        )
    )
    version = int(current_max or 0) + 1

    now = utcnow()
    task_type = TaskType(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        version=version,
        display_name=display_name,
        description=description,
        field_schema=schema,
        lifecycle_schema=document,
        execution=normalized_execution,
        approval_schema=outcomes,
        context_schema=profile,
        instructions=text,
        completion_schema=after_completion,
        artifact_schema=handoff,
        acceptance=checks,
        executor_roles=roles,
        status=TaskTypeStatus.ACTIVE,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    session.add(task_type)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task_type.created",
        entity_type="task_type",
        entity_id=task_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "version": version,
            "displayName": display_name,
            "initialStatus": lifecycle.initial_status,
            "completionStatus": lifecycle.completion_status,
            "execution": normalized_execution,
            "declaresApprovalOutcomes": bool(outcomes),
            "declaresContextProfile": bool(profile),
            "declaresInstructions": bool(text),
            "declaresCompletionWork": bool(after_completion),
            "declaresArtifactSchema": not io.empty,
            "inputs": len(io.inputs),
            "outputs": len(io.outputs),
            "executorRoles": roles,
        },
    )
    return task_type


async def deprecate_task_type(
    session: AsyncSession, ctx: AuthContext, *, type_id: uuid.UUID
) -> TaskType:
    """Retire a version from resolution; tasks already carrying it are unaffected."""
    await authorize(ctx, Permission.TASK_TYPES_MANAGE)
    task_type = await get_tenant_task_type(session, ctx, type_id)
    if task_type.status == TaskTypeStatus.DEPRECATED:
        return task_type  # idempotent

    if task_type.key == SYSTEM_TASK_TYPE_KEY:
        # The system type is what an unqualified task creation resolves to;
        # deprecating the last active version of it would make `POST /tasks`
        # without a typeKey a 404 for the whole tenant.
        active = await session.scalar(
            select(func.count())
            .select_from(TaskType)
            .where(
                TaskType.tenant_id == ctx.tenant_id,
                TaskType.key == SYSTEM_TASK_TYPE_KEY,
                TaskType.status == TaskTypeStatus.ACTIVE,
            )
        )
        if int(active or 0) <= 1:
            raise ValidationError(
                "system_task_type_required",
                "The tenant must keep one active version of the system task type",
                details={"taskTypeKey": SYSTEM_TASK_TYPE_KEY},
            )

    task_type.status = TaskTypeStatus.DEPRECATED
    task_type.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="task_type.deprecated",
        entity_type="task_type",
        entity_id=task_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": task_type.key, "version": task_type.version},
    )
    return task_type
