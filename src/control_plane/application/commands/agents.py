"""Agent registry: specs, immutable revisions, desired and observed state (CP-ADR-0073).

An agent is a tenant catalog object with a key. Every applied spec whose
canonical hash differs from the current one becomes the next immutable
revision; re-applying the same spec writes nothing but the desired state.
The desired state (``state``, ``replicas``) lives on the agent row, the
observed state is written by the placement service alone.

Permissions of a spec are checked against the caller who applies it (§5) —
with the very function that guards ``iam-bindings`` — at every application,
so ``:validate`` and ``POST`` answer alike and re-applying an unchanged
package with a narrow credential still fails. Because of that check the core
itself derives the agent's principal, roles and IAM binding from the revision
(§6): the placement service only reports which IAM identity it created.
The same goes for the skills the agent invokes (``skills.invoke``, amendment
2026-09-28): the registry assigns them to its principal and takes back only
what it assigned itself.
"""

import copy
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    VISIBILITY_TENANT,
    AuthContext,
    authorize,
    visible_objects,
)
from control_plane.application.commands._claim_release import release_active_claims_of_holder
from control_plane.application.commands.iam_bindings import (
    BINDING_STATUS_ACTIVE,
    BINDING_STATUS_REVOKED,
    check_agent_binding_escalation,
    check_trusted_issuer,
    identity_taken,
    validate_binding_permissions,
    validate_stored_permissions,
)
from control_plane.application.commands.package_links import link_object
from control_plane.application.common import decode_cursor, encode_cursor, new_uuid, utcnow
from control_plane.application.events import event_reason, record_event
from control_plane.application.locking import lock_caller, lock_caller_and_principal_for_update
from control_plane.application.queries.lists import clamp_limit
from control_plane.domain.canonical import HASH_ALGORITHM, canonical_bytes, canonicalize
from control_plane.domain.enums import (
    AgentState,
    AgentStatus,
    Permission,
    PrincipalKind,
    PrincipalStatus,
    SkillStatus,
    TaskTypeStatus,
    WorkspaceStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.project import reject_secret_material
from control_plane.infrastructure.db.models import (
    Agent,
    AgentObservedStatus,
    AgentRevision,
    Capability,
    IamPrincipalBinding,
    Principal,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    ProjectProfile,
    Role,
    Skill,
    TaskType,
    Workspace,
)

# §2: a spec carries the executor's conventions in ``executor.instructions``,
# so its strings get the bound of that field rather than the general 2000.
AGENT_SPEC_MAX_STRING_CHARS = 65_536
AGENT_SPEC_MAX_BYTES = 256 * 1024

RETIRE_RELEASE_REASON = "agent_retired"

# ``principal_skills.metadata`` of an assignment the registry made: only those
# are taken back when a revision no longer names the skill.
REGISTRY_ASSIGNMENT = {"assignedBy": "agent-registry"}


@dataclass(frozen=True)
class CheckedSpec:
    """A spec that passed every check of §5, ready to be written."""

    key: str
    spec: dict[str, Any]  # canonical, without the desired state
    spec_hash: str
    state: str
    replicas: int
    display_name: str
    identity_kind: str
    permissions: list[str]
    role_ids: list[uuid.UUID]
    capability_ids: list[uuid.UUID]
    skill_ids: list[uuid.UUID]
    workspace_id: uuid.UUID | None
    executor_kind: str | None
    placed: bool


@dataclass(frozen=True)
class CheckResult:
    checked: CheckedSpec
    agent: Agent | None
    current: AgentRevision | None

    @property
    def would_create_revision(self) -> bool:
        return self.current is None or self.current.spec_hash != self.checked.spec_hash

    @property
    def would_change_state(self) -> bool:
        return self.agent is None or (self.agent.state, self.agent.replicas) != (
            self.checked.state,
            self.checked.replicas,
        )


@dataclass(frozen=True)
class AgentView:
    """An agent with the revision a response shows (current or addressed)."""

    agent: Agent
    revision: AgentRevision
    created: bool = False
    # IAM identities whose binding changed: the caller drops their cache
    # entries once the transaction has committed (ADR-0053).
    touched_identities: list[tuple[str, uuid.UUID]] = field(default_factory=list)


# --- lookups ------------------------------------------------------------------


async def _lock_agent_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize publication for one (tenant, key): the first one inserts the row."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:agent:{tenant_id}:{key}", 0)))
    )


async def _agent_by_key(
    session: AsyncSession, ctx: AuthContext, key: str, *, for_update: bool = False
) -> Agent | None:
    stmt = select(Agent).where(Agent.tenant_id == ctx.tenant_id, Agent.key == key)
    if for_update:
        stmt = stmt.with_for_update()
    agent: Agent | None = await session.scalar(stmt)
    return agent


async def require_agent(
    session: AsyncSession, ctx: AuthContext, key: str, *, for_update: bool = False
) -> Agent:
    agent = await _agent_by_key(session, ctx, key, for_update=for_update)
    if agent is None:
        raise NotFoundError("Agent not found", details={"agent": key})
    return agent


async def revision_of(session: AsyncSession, agent: Agent, revision: int) -> AgentRevision | None:
    found: AgentRevision | None = await session.scalar(
        select(AgentRevision).where(
            AgentRevision.agent_id == agent.id, AgentRevision.revision == revision
        )
    )
    return found


async def _current_revision(session: AsyncSession, agent: Agent) -> AgentRevision:
    revision = await revision_of(session, agent, agent.current_revision)
    if revision is None:  # pragma: no cover - the row is written with the agent
        raise NotFoundError("Agent revision not found", details={"agent": agent.key})
    return revision


def retired_conflict(agent: Agent) -> ConflictError:
    return ConflictError(
        "agent_retired",
        "The agent is retired; its key is never reused",
        details={"agent": agent.key, "retiredAt": agent.retired_at.isoformat()}
        if agent.retired_at
        else {"agent": agent.key},
    )


async def resolve_agent(session: AsyncSession, ctx: AuthContext, ref: str) -> AgentView:
    """``key`` (current revision) or ``key@revision``."""
    await authorize(ctx, Permission.AGENTS_READ)
    key, pinned, revision_text = ref.partition("@")
    not_found = NotFoundError("Agent not found", details={"agent": ref})
    agent = await _agent_by_key(session, ctx, key)
    if agent is None:
        raise not_found
    if not pinned:
        return AgentView(agent, await _current_revision(session, agent))
    if not (revision_text.isascii() and revision_text.isdigit()) or len(revision_text) > 9:
        raise not_found
    revision = await revision_of(session, agent, int(revision_text))
    if revision is None:
        raise not_found
    return AgentView(agent, revision)


def changed_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Spec fields that differ, sorted: ``section.field`` inside two objects, else the top field."""
    changed: list[str] = []
    for name in sorted(previous.keys() | current.keys()):
        before, after = previous.get(name), current.get(name)
        if before == after:
            continue
        if isinstance(before, dict) and isinstance(after, dict):
            changed.extend(
                f"{name}.{inner}"
                for inner in sorted(before.keys() | after.keys())
                if before.get(inner) != after.get(inner)
            )
        else:
            changed.append(name)
    return changed


@dataclass(frozen=True)
class RevisionPage:
    """A page of revisions, newest first, each with the spec of the one before it."""

    agent: Agent
    items: list[tuple[AgentRevision, dict[str, Any] | None]]
    next_cursor: str | None


async def list_revisions(
    session: AsyncSession, ctx: AuthContext, *, key: str, limit: int | None, cursor: str | None
) -> RevisionPage:
    """Revisions of ``key`` below the cursor, newest first (amendment 2026-09-29, Г1).

    Numbers are contiguous from 1, so one row past the page is both the
    previous revision of its last item and the sign that a next page exists.
    """
    await authorize(ctx, Permission.AGENTS_READ)
    effective_limit = clamp_limit(limit)
    agent = await require_agent(session, ctx, key)
    stmt = select(AgentRevision).where(AgentRevision.agent_id == agent.id)
    if cursor is not None:
        below = decode_cursor(cursor).get("r")
        if not isinstance(below, int) or isinstance(below, bool) or below < 1:
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
        stmt = stmt.where(AgentRevision.revision < below)
    stmt = stmt.order_by(AgentRevision.revision.desc()).limit(effective_limit + 1)
    rows = list(await session.scalars(stmt))
    page = rows[:effective_limit]
    items = [
        (row, rows[index + 1].spec if index + 1 < len(rows) else None)
        for index, row in enumerate(page)
    ]
    next_cursor = encode_cursor({"r": page[-1].revision}) if len(rows) > effective_limit else None
    return RevisionPage(agent, items, next_cursor)


async def my_agent(session: AsyncSession, ctx: AuthContext) -> AgentView:
    """The agent the caller is (§8); a retired one is returned, not hidden."""
    agent = await session.scalar(
        select(Agent).where(
            Agent.tenant_id == ctx.tenant_id, Agent.principal_id == ctx.principal_id
        )
    )
    if agent is None:
        raise NotFoundError("The caller is not a registered agent")
    return AgentView(agent, await _current_revision(session, agent))


async def active_agent_of_principal(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> Agent | None:
    agent: Agent | None = await session.scalar(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.principal_id == principal_id,
            Agent.status == AgentStatus.ACTIVE,
        )
    )
    return agent


# --- the checks of §5 ---------------------------------------------------------


def _unknown(path: str, value: str, message: str) -> ValidationError:
    return ValidationError("unknown_reference", message, details={"path": path, "value": value})


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def split_desired_state(spec: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
    """The revision body and the desired state it carried (§2, §3).

    ``state`` and ``placement.replicas`` move without a new revision, so they
    are not part of what is hashed. No ``placement`` means placed with the
    defaults (one replica); ``placement: none`` has nothing to run: 0.
    """
    body = copy.deepcopy(spec)
    state = body.pop("state", AgentState.RUNNING.value)
    placement = body.get("placement")
    if placement == "none":
        replicas = 0
    elif isinstance(placement, dict):
        replicas = placement.pop("replicas", 1)
    else:
        replicas = 1
    return body, state, replicas


def spec_hash_of(body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Canonical clone and ``sha256:<hex>`` of a revision body (§2)."""
    canonical = canonicalize(body, path="$.spec", max_string_chars=AGENT_SPEC_MAX_STRING_CHARS)
    raw = canonical_bytes(canonical, max_string_chars=AGENT_SPEC_MAX_STRING_CHARS)
    if len(raw) > AGENT_SPEC_MAX_BYTES:
        raise ValidationError(
            "payload_too_large",
            f"An agent spec must not exceed {AGENT_SPEC_MAX_BYTES} bytes of canonical JSON",
            details={"path": "$.spec", "maxBytes": AGENT_SPEC_MAX_BYTES},
        )
    return canonical, f"{HASH_ALGORITHM}:{hashlib.sha256(raw).hexdigest()}"


async def _resolve_roles(
    session: AsyncSession, ctx: AuthContext, slugs: list[str]
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for index, slug in enumerate(slugs):
        role_id = await session.scalar(
            select(Role.id).where(
                Role.tenant_id == ctx.tenant_id, Role.slug == slug, Role.workspace_id.is_(None)
            )
        )
        if role_id is None:
            raise _unknown(f"spec.identity.roles[{index}]", slug, "Unknown tenant-wide role")
        ids.append(role_id)
    return sorted(set(ids))


async def _resolve_capabilities(
    session: AsyncSession, ctx: AuthContext, names: list[str]
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for index, name in enumerate(names):
        capability_id = await session.scalar(
            select(Capability.id).where(
                Capability.tenant_id == ctx.tenant_id, Capability.name == name
            )
        )
        if capability_id is None:
            raise _unknown(f"spec.identity.capabilities[{index}]", name, "Unknown capability")
        ids.append(capability_id)
    return sorted(set(ids))


async def _resolve_invoked_skills(
    session: AsyncSession, ctx: AuthContext, refs: list[str], *, strict: bool = True
) -> list[uuid.UUID]:
    """``skills.invoke``: pinned versions of the tenant, not disabled.

    ``strict=False`` is for linking an identity to a revision checked earlier:
    a version disabled since then is left out rather than failing the link.
    """
    ids: list[uuid.UUID] = []
    for index, ref in enumerate(refs):
        name, _, version = ref.partition("@")
        skill = await session.scalar(
            select(Skill).where(
                Skill.tenant_id == ctx.tenant_id, Skill.name == name, Skill.version == version
            )
        )
        if skill is None or skill.status == SkillStatus.DISABLED:
            if not strict:
                continue
            if skill is None:
                raise _unknown(f"spec.skills.invoke[{index}]", ref, "Unknown skill version")
            raise ValidationError(
                "skill_disabled",
                "A disabled skill version cannot be assigned",
                details={"path": f"spec.skills.invoke[{index}]", "value": ref},
            )
        ids.append(skill.id)
    return sorted(set(ids))


def _invoked_refs(spec: dict[str, Any]) -> list[str]:
    skills = spec.get("skills") or {}
    return list(skills.get("invoke", []))


async def _check_executions(
    session: AsyncSession, ctx: AuthContext, work: dict[str, Any], invoked: set[str]
) -> None:
    """A type the agent takes that a skill executes needs that skill in ``skills.invoke``.

    The run of such a task is one ``POST /skills/{ref}:invoke`` under the
    agent's principal; without the assignment it fails with
    ``tool_not_authorized`` on every attempt. Every active version counts: a
    task keeps the version it was created with.
    """
    for index, type_key in enumerate(work.get("taskTypes", [])):
        executions = (
            await session.scalars(
                select(TaskType.execution)
                .where(
                    TaskType.tenant_id == ctx.tenant_id,
                    TaskType.key == type_key,
                    TaskType.status == TaskTypeStatus.ACTIVE,
                    TaskType.execution.is_not(None),
                )
                .order_by(TaskType.version)
            )
        ).all()
        for execution in executions:
            if not execution:
                continue
            ref = f"{execution['skill']}@{execution['version']}"
            if ref not in invoked:
                raise ValidationError(
                    "execution_skill_not_invoked",
                    "The agent takes a task type executed by a skill it does not invoke",
                    details={
                        "path": f"spec.work.taskTypes[{index}]",
                        "taskType": type_key,
                        "skill": ref,
                        "expected": "spec.skills.invoke",
                    },
                )


async def _resolve_workspace(
    session: AsyncSession, ctx: AuthContext, ref: str, *, path: str
) -> Workspace:
    """A workspace by id or slug, active and visible to the caller.

    A slug is unique only among siblings; one that names several workspaces
    is refused rather than guessed. An invisible workspace is reported exactly
    like a missing one.
    """
    stmt = select(Workspace).where(Workspace.tenant_id == ctx.tenant_id)
    workspace_id = _as_uuid(ref)
    stmt = stmt.where(Workspace.id == workspace_id if workspace_id else Workspace.slug == ref)
    found = list((await session.scalars(stmt.limit(2))).all())
    if len(found) != 1 or found[0].status != WorkspaceStatus.ACTIVE:
        raise _unknown(path, ref, "Unknown, ambiguous or archived workspace")
    visible = await visible_objects(ctx, Permission.TASKS_READ, "workspace")
    if visible is not None and str(found[0].id) not in visible:
        raise _unknown(path, ref, "Unknown, ambiguous or archived workspace")
    return found[0]


async def _check_work(
    session: AsyncSession, ctx: AuthContext, work: dict[str, Any]
) -> uuid.UUID | None:
    workspace_ref = work.get("workspace")
    workspace = (
        await _resolve_workspace(session, ctx, workspace_ref, path="spec.work.workspace")
        if workspace_ref is not None
        else None
    )
    project_ref = work.get("project")
    if project_ref is not None:
        project_id = _as_uuid(project_ref)
        condition: ColumnElement[bool]
        if project_id is not None:
            condition = (ProjectProfile.id == project_id) | (
                ProjectProfile.workspace_id == project_id
            )
        else:
            project_workspace = await _resolve_workspace(
                session, ctx, project_ref, path="spec.work.project"
            )
            condition = ProjectProfile.workspace_id == project_workspace.id
        project = await session.scalar(
            select(ProjectProfile.id).where(ProjectProfile.tenant_id == ctx.tenant_id, condition)
        )
        if project is None:
            raise _unknown("spec.work.project", project_ref, "Unknown project")
    for index, type_key in enumerate(work.get("taskTypes", [])):
        exists = await session.scalar(
            select(TaskType.id)
            .where(
                TaskType.tenant_id == ctx.tenant_id,
                TaskType.key == type_key,
                TaskType.status == TaskTypeStatus.ACTIVE,
            )
            .limit(1)
        )
        if exists is None:
            raise _unknown(f"spec.work.taskTypes[{index}]", type_key, "Unknown task type")
    return workspace.id if workspace is not None else None


async def check_agent_spec(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    spec: dict[str, Any],
    lock: bool = False,
) -> CheckResult:
    """Every check of ``POST /agents`` in its order (§5); writes nothing.

    ``spec`` is the spec as sent (shape already validated), without defaults
    filled in: that is what is hashed.
    """
    agent = await _agent_by_key(session, ctx, key, for_update=lock)
    if agent is not None and agent.status == AgentStatus.RETIRED:
        raise retired_conflict(agent)

    identity = spec["identity"]
    identity_kind = identity.get("kind", "agent")
    principal: Principal | None = None
    if agent is not None and agent.principal_id is not None:
        principal = await session.get(Principal, agent.principal_id)
        if principal is not None and principal.kind != identity_kind:
            # The principal of a linked agent is its history; another kind is
            # another identity, i.e. retirement and a new key (§6).
            raise ConflictError(
                "agent_identity_conflict",
                "The identity kind of a linked agent cannot change",
                details={"agent": key, "kind": principal.kind, "requested": identity_kind},
            )

    permissions = validate_binding_permissions(
        ctx, permissions=list(identity["permissions"]), principal_kind=identity_kind
    )

    roles = list(identity.get("roles", []))
    capabilities = list(identity.get("capabilities", []))
    invoked = _invoked_refs(spec)
    if (roles or capabilities or invoked) and not ctx.has(Permission.ORG_MANAGE):
        raise AuthorizationError(
            "Assigning roles, capabilities or skills requires org.manage",
            code="permission_escalation",
            details={"missing": [Permission.ORG_MANAGE.value]},
        )
    if spec.get("connections") and not ctx.has(Permission.CONNECTIONS_MANAGE):
        # The list gives the agent the material of those connections
        # (CP-ADR-0079 §8): its presence is checked, not the difference with
        # the current revision, so ``:validate`` and a re-applied package
        # answer alike.
        raise AuthorizationError(
            "Naming connections in an agent spec requires connections.manage",
            code="permission_escalation",
            details={"missing": [Permission.CONNECTIONS_MANAGE.value], "path": "spec.connections"},
        )
    role_ids = await _resolve_roles(session, ctx, roles)
    capability_ids = await _resolve_capabilities(session, ctx, capabilities)
    skill_ids = await _resolve_invoked_skills(session, ctx, invoked)
    if invoked and Permission.SKILLS_INVOKE.value not in permissions:
        raise ValidationError(
            "skills_invoke_not_permitted",
            "An agent that invokes skills needs the skills.invoke permission",
            details={"path": "spec.identity.permissions", "missing": ["skills.invoke"]},
        )

    work = spec.get("work")
    workspace_id = await _check_work(session, ctx, work) if work is not None else None
    if work is not None:
        await _check_executions(session, ctx, work, set(invoked))

    executor = spec.get("executor")
    if executor is not None:
        reject_secret_material(executor.get("params", {}), label="spec.executor.params")
    if spec.get("workingCopy") is not None:
        reject_secret_material(spec["workingCopy"], label="spec.workingCopy")
    placement = spec.get("placement")
    if isinstance(placement, dict):
        # ``placement.secrets`` is a list of node secret NAMES: the one allowed
        # reference to a secret, deliberately outside this check.
        reject_secret_material(placement.get("resources", {}), label="spec.placement.resources")

    body, state, replicas = split_desired_state(spec)
    canonical, spec_hash = spec_hash_of(body)

    current = await _current_revision(session, agent) if agent is not None else None
    return CheckResult(
        checked=CheckedSpec(
            key=key,
            spec=canonical,
            spec_hash=spec_hash,
            state=state,
            replicas=replicas,
            display_name=spec["displayName"],
            identity_kind=identity_kind,
            permissions=permissions,
            role_ids=role_ids,
            capability_ids=capability_ids,
            skill_ids=skill_ids,
            workspace_id=workspace_id,
            executor_kind=executor["kind"] if executor is not None else None,
            placed=placement != "none",
        ),
        agent=agent,
        current=current,
    )


def _identity_signature(spec: dict[str, Any]) -> tuple[Any, ...]:
    identity = spec.get("identity", {})
    return (
        identity.get("kind", "agent"),
        sorted(set(identity.get("permissions", []))),
        sorted(set(identity.get("roles", []))),
        sorted(set(identity.get("capabilities", []))),
        sorted(set(_invoked_refs(spec))),
    )


# --- publication --------------------------------------------------------------


async def validate_agent(
    session: AsyncSession, ctx: AuthContext, *, key: str, spec: dict[str, Any]
) -> CheckResult:
    await authorize(ctx, Permission.AGENTS_MANAGE)
    return await check_agent_spec(session, ctx, key=key, spec=spec)


async def publish_agent(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    spec: dict[str, Any],
    package: tuple[str, str] | None = None,
    link: bool = True,
) -> AgentView:
    """Apply a spec: a new revision only when its hash differs (§2), state always (§3).

    ``package`` — ``(key, version)`` the installer named: the source of a new
    revision and the link of the agent to its package (``package_objects``);
    without it the revision is a manual edit and the link stays as it was.
    ``link=False`` — ``POST /packages:apply`` writes the link itself, with the
    spec it applied.
    """
    await authorize(ctx, Permission.AGENTS_MANAGE)
    await _lock_agent_key(session, ctx.tenant_id, key)
    result = await check_agent_spec(session, ctx, key=key, spec=spec, lock=True)
    checked, agent, current = result.checked, result.agent, result.current

    now = utcnow()
    previous_state: tuple[str | None, int | None] = (None, None)
    touched: list[tuple[str, uuid.UUID]] = []
    if agent is None:
        agent = Agent(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            key=key,
            display_name=checked.display_name,
            status=AgentStatus.ACTIVE,
            state=checked.state,
            replicas=checked.replicas,
            current_revision=1,
            workspace_id=checked.workspace_id,
            version=1,
            created_by=ctx.principal_id,
            created_at=now,
            updated_at=now,
        )
        session.add(agent)
        await session.flush()
    else:
        previous_state = (agent.state, agent.replicas)

    revision = current
    if result.would_create_revision:
        number = 1 if current is None else current.revision + 1
        revision = AgentRevision(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            agent_id=agent.id,
            revision=number,
            spec=checked.spec,
            spec_hash=checked.spec_hash,
            source_kind="package" if package is not None else "manual",
            source_package_key=package[0] if package is not None else None,
            source_package_version=package[1] if package is not None else None,
            created_by=ctx.principal_id,
            created_at=now,
        )
        session.add(revision)
        await session.flush()
        permissions_changed = current is None or _identity_signature(
            current.spec
        ) != _identity_signature(checked.spec)
        if current is not None:
            agent.current_revision = number
            agent.display_name = checked.display_name
            agent.workspace_id = checked.workspace_id
            if agent.principal_id is not None:
                touched = await _apply_identity(session, ctx, agent, checked)
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="agent.revision_published",
            entity_type="agent",
            entity_id=agent.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "key": key,
                "revision": number,
                "specHash": checked.spec_hash,
                "previousRevision": current.revision if current is not None else None,
                "executorKind": checked.executor_kind,
                "placed": checked.placed,
                "permissionsChanged": permissions_changed,
            },
        )
    assert revision is not None

    if previous_state != (checked.state, checked.replicas):
        agent.state = checked.state
        agent.replicas = checked.replicas
        await _state_changed(session, ctx, agent, previous_state)

    if current is not None and (
        result.would_create_revision or previous_state != (agent.state, agent.replicas)
    ):
        agent.version += 1
        agent.updated_at = now
    if package is not None and link:
        # The agent is the package's object, whether this apply made a revision or not.
        await link_object(
            session,
            ctx,
            kind="Agent",
            key=key,
            package_key=package[0],
            package_version=package[1],
            install_hash=None,
        )
    return AgentView(
        agent, revision, created=result.would_create_revision, touched_identities=touched
    )


async def _state_changed(
    session: AsyncSession,
    ctx: AuthContext,
    agent: Agent,
    previous: tuple[str | None, int | None],
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="agent.state_changed",
        entity_type="agent",
        entity_id=agent.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": agent.key,
            "state": agent.state,
            "replicas": agent.replicas,
            "previousState": previous[0],
            "previousReplicas": previous[1],
        },
    )


async def state_gate(
    session: AsyncSession, ctx: AuthContext, key: str, *, for_update: bool = False
) -> Agent:
    """Who may change state and replicas; ``POST /authz:check`` asks the same."""
    await authorize(ctx, Permission.AGENTS_MANAGE)
    return await require_agent(session, ctx, key, for_update=for_update)


async def update_agent_state(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    state: str | None,
    replicas: int | None,
) -> AgentView:
    """Change only the desired state (§3); an unchanged request writes nothing."""
    agent = await state_gate(session, ctx, key, for_update=True)
    if agent.status == AgentStatus.RETIRED:
        raise retired_conflict(agent)
    previous = (agent.state, agent.replicas)
    desired = (state or agent.state, agent.replicas if replicas is None else replicas)
    if desired != previous:
        agent.state, agent.replicas = desired
        agent.version += 1
        agent.updated_at = utcnow()
        await _state_changed(session, ctx, agent, previous)
    return AgentView(agent, await _current_revision(session, agent))


# --- identity (§6) ------------------------------------------------------------


async def _record_binding_event(
    session: AsyncSession, ctx: AuthContext, event_type: str, binding: IamPrincipalBinding
) -> None:
    payload: dict[str, Any] = {
        "principalId": str(binding.principal_id),
        "issuer": binding.issuer,
        "iamPrincipalId": str(binding.iam_principal_id),
    }
    if event_type != "iam_binding.revoked":
        payload["iamTenantId"] = str(binding.iam_tenant_id)
        payload["permissions"] = binding.permissions
        # An agent's binding is always tenant-wide (CP-ADR-0082 §2.3); the
        # default is applied on flush, which may not have happened yet.
        payload["visibility"] = binding.visibility or VISIBILITY_TENANT
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="iam_binding",
        entity_id=binding.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )


async def _principal_event(
    session: AsyncSession,
    ctx: AuthContext,
    principal_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
    )


async def _apply_identity(
    session: AsyncSession, ctx: AuthContext, agent: Agent, checked: CheckedSpec
) -> list[tuple[str, uuid.UUID]]:
    """Bring the linked principal, its roles, capabilities, skills and binding to ``checked``.

    Runs in the transaction that publishes the revision, so there is no
    window where the revision is new and the rights are old. The authority is
    the one who applied the revision — already checked against it (§5) — not
    whoever triggered this write, which is why the org commands (each checking
    its own permission on the caller) are not reused here.
    """
    assert agent.principal_id is not None
    principal_id = agent.principal_id
    principal = await session.get(Principal, principal_id)
    if principal is not None and principal.display_name != checked.display_name:
        principal.display_name = checked.display_name
        # Any writer of the name moves the version (CP-ADR-0082 §1.3).
        principal.version += 1
        principal.updated_at = utcnow()

    held_roles = {
        row.role_id: row
        for row in (
            await session.scalars(
                select(PrincipalRole).where(
                    PrincipalRole.principal_id == principal_id,
                    PrincipalRole.workspace_id.is_(None),
                )
            )
        ).all()
    }
    for role_id in sorted(set(checked.role_ids) - set(held_roles)):
        session.add(
            PrincipalRole(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                principal_id=principal_id,
                role_id=role_id,
                workspace_id=None,
                created_at=utcnow(),
            )
        )
        await _principal_event(
            session,
            ctx,
            principal_id,
            "role.assigned",
            {"roleId": str(role_id), "workspaceId": None},
        )
    for role_id in sorted(set(held_roles) - set(checked.role_ids)):
        await session.delete(held_roles[role_id])
        await _principal_event(
            session,
            ctx,
            principal_id,
            "role.revoked",
            {"roleId": str(role_id), "workspaceId": None},
        )

    held_capabilities = {
        row.capability_id: row
        for row in (
            await session.scalars(
                select(PrincipalCapability).where(PrincipalCapability.principal_id == principal_id)
            )
        ).all()
    }
    for capability_id in sorted(set(checked.capability_ids) - set(held_capabilities)):
        session.add(
            PrincipalCapability(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                principal_id=principal_id,
                capability_id=capability_id,
                metadata_json={},
                created_at=utcnow(),
            )
        )
        await _principal_event(
            session,
            ctx,
            principal_id,
            "capability.assigned",
            {"capabilityId": str(capability_id)},
        )
    for capability_id in sorted(set(held_capabilities) - set(checked.capability_ids)):
        await session.delete(held_capabilities[capability_id])
        await _principal_event(
            session,
            ctx,
            principal_id,
            "capability.revoked",
            {"capabilityId": str(capability_id)},
        )

    await _apply_invoked_skills(session, ctx, principal_id, checked.skill_ids)

    touched: list[tuple[str, uuid.UUID]] = []
    binding = await session.scalar(
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.issuer == agent.iam_issuer,
            IamPrincipalBinding.iam_principal_id == agent.iam_principal_id,
            IamPrincipalBinding.principal_id == principal_id,
        )
        .with_for_update()
    )
    if binding is not None and (
        binding.status != BINDING_STATUS_ACTIVE or binding.permissions != checked.permissions
    ):
        binding.permissions = checked.permissions
        binding.status = BINDING_STATUS_ACTIVE
        binding.revoked_at = None
        binding.updated_at = utcnow()
        await session.flush()
        await _record_binding_event(session, ctx, "iam_binding.updated", binding)
        touched.append((binding.issuer, binding.iam_principal_id))
    await session.flush()
    return touched


async def _apply_invoked_skills(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID, skill_ids: list[uuid.UUID]
) -> None:
    """``principal_skills`` to ``skills.invoke``: missing ones assigned, the
    registry's own extra ones revoked. An assignment made by hand
    (``POST /principals/{id}/skills``) is neither marked nor taken back."""
    held = {
        row.skill_id: row
        for row in (
            await session.scalars(
                select(PrincipalSkill).where(PrincipalSkill.principal_id == principal_id)
            )
        ).all()
    }
    for skill_id in sorted(set(skill_ids) - set(held)):
        session.add(
            PrincipalSkill(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                principal_id=principal_id,
                skill_id=skill_id,
                metadata_json=dict(REGISTRY_ASSIGNMENT),
                created_at=utcnow(),
            )
        )
        await _principal_event(
            session, ctx, principal_id, "skill.assigned", {"skillId": str(skill_id)}
        )
    for skill_id in sorted(set(held) - set(skill_ids)):
        row = held[skill_id]
        if (row.metadata_json or {}).get("assignedBy") != REGISTRY_ASSIGNMENT["assignedBy"]:
            continue
        await session.delete(row)
        await _principal_event(
            session, ctx, principal_id, "skill.revoked", {"skillId": str(skill_id)}
        )


async def _insert_binding(
    session: AsyncSession, ctx: AuthContext, binding: IamPrincipalBinding, key: str
) -> None:
    """Insert a new binding; losing a race for its identity is a 409.

    Two calls may give one new identity to two principals at once (two
    agents, or an agent and an ``iam-bindings`` upsert): the lookup of both
    finds nothing and the second insert hits ``uq_iam_bindings_identity``.
    Under a SAVEPOINT the loser reads the winner's row and answers as it would
    have a moment later, instead of a 500.
    """
    try:
        async with session.begin_nested():
            session.add(binding)
            await session.flush()
    except IntegrityError as exc:
        winner = await session.scalar(
            select(IamPrincipalBinding).where(
                IamPrincipalBinding.issuer == binding.issuer,
                IamPrincipalBinding.iam_principal_id == binding.iam_principal_id,
            )
        )
        raise identity_taken(winner, ctx, key) from exc


async def link_agent_identity(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    issuer: str,
    iam_tenant_id: uuid.UUID,
    iam_principal_id: uuid.UUID,
    trusted_issuer: str,
) -> AgentView:
    """Derive the agent's principal, roles, skills and binding from its current revision (§6).

    The caller is the placement service: it created the IAM identity and says
    which one it is. Relinking the same identity changes nothing; another
    identity for a linked agent is a conflict — a new identity is a new key.
    Only the issuer the core trusts is linked (``check_trusted_issuer``).
    """
    await authorize(ctx, Permission.AGENTS_STATUS_WRITE)
    # Linking makes or reopens a tenant-wide binding (CP-ADR-0082 B5, V3).
    check_agent_binding_escalation(ctx)
    check_trusted_issuer(issuer, trusted_issuer)
    agent = await require_agent(session, ctx, key, for_update=True)
    if agent.status == AgentStatus.RETIRED:
        raise retired_conflict(agent)
    revision = await _current_revision(session, agent)
    requested = (issuer, iam_tenant_id, iam_principal_id)
    if agent.principal_id is not None:
        if (agent.iam_issuer, agent.iam_tenant_id, agent.iam_principal_id) == requested:
            touched = await _reopen_identity(session, ctx, agent, revision)
            return AgentView(agent, revision, touched_identities=touched)
        raise ConflictError(
            "agent_identity_conflict",
            "The agent is linked to another IAM identity; retire it and publish a new key",
            details={"agent": key},
        )

    existing = await session.scalar(
        select(IamPrincipalBinding).where(
            IamPrincipalBinding.issuer == issuer,
            IamPrincipalBinding.iam_principal_id == iam_principal_id,
        )
    )
    if existing is not None:
        raise identity_taken(existing, ctx, key)

    spec = revision.spec
    identity = spec["identity"]
    identity_kind = identity.get("kind", "agent")
    now = utcnow()
    principal = Principal(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        kind=identity_kind,
        display_name=agent.display_name,
        status=PrincipalStatus.ACTIVE,
        metadata_json={"agentKey": key},
        created_at=now,
        updated_at=now,
    )
    session.add(principal)
    await session.flush()
    await _principal_event(
        session,
        ctx,
        principal.id,
        "principal.created",
        {"kind": principal.kind, "displayName": principal.display_name},
    )

    binding = IamPrincipalBinding(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=principal.id,
        issuer=issuer,
        iam_tenant_id=iam_tenant_id,
        iam_principal_id=iam_principal_id,
        permissions=sorted(set(identity["permissions"])),
        status=BINDING_STATUS_ACTIVE,
        revoked_at=None,
        last_used_at=None,
        created_at=now,
        updated_at=now,
    )
    await _insert_binding(session, ctx, binding, key)
    await _record_binding_event(session, ctx, "iam_binding.created", binding)

    agent.principal_id = principal.id
    agent.iam_issuer = issuer
    agent.iam_tenant_id = iam_tenant_id
    agent.iam_principal_id = iam_principal_id
    agent.version += 1
    agent.updated_at = now
    await session.flush()

    await _apply_identity(
        session, ctx, agent, await _revision_identity(session, ctx, agent, revision)
    )
    return AgentView(agent, revision, touched_identities=[(issuer, iam_principal_id)])


async def _revision_identity(
    session: AsyncSession, ctx: AuthContext, agent: Agent, revision: AgentRevision
) -> CheckedSpec:
    """The identity part of the current revision, in the shape ``_apply_identity`` takes.

    The rights are checked again against the catalog and the kind rule: the
    revision was checked when it was published, and a permission may have
    left the catalog since. The escalation rule is not applied — the caller
    hands out no rights of its own, only those of a revision already checked
    against whoever applied it (§5).
    """
    spec = revision.spec
    identity = spec["identity"]
    identity_kind = identity.get("kind", "agent")
    permissions = validate_stored_permissions(
        permissions=list(identity["permissions"]), principal_kind=identity_kind
    )
    return CheckedSpec(
        key=agent.key,
        spec=spec,
        spec_hash=revision.spec_hash,
        state=agent.state,
        replicas=agent.replicas,
        display_name=agent.display_name,
        identity_kind=identity_kind,
        permissions=permissions,
        role_ids=await _resolve_roles(session, ctx, list(identity.get("roles", []))),
        capability_ids=await _resolve_capabilities(
            session, ctx, list(identity.get("capabilities", []))
        ),
        skill_ids=await _resolve_invoked_skills(session, ctx, _invoked_refs(spec), strict=False),
        workspace_id=agent.workspace_id,
        executor_kind=None,
        placed=False,
    )


async def _reopen_identity(
    session: AsyncSession, ctx: AuthContext, agent: Agent, revision: AgentRevision
) -> list[tuple[str, uuid.UUID]]:
    """The same identity linked again: its binding reopened if it was shut (§6).

    Since ``iam-bindings`` refuses the registry's own identity (I4), this is
    the way back for a binding revoked beside the registry: it comes back
    with the rights of the current revision, the principal's roles,
    capabilities and skills brought to it as by a publish. An active binding
    changes nothing — the repeat stays idempotent.
    """
    assert agent.principal_id is not None
    binding = await session.scalar(
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.issuer == agent.iam_issuer,
            IamPrincipalBinding.iam_principal_id == agent.iam_principal_id,
            IamPrincipalBinding.principal_id == agent.principal_id,
        )
        .with_for_update()
    )
    if binding is None or binding.status == BINDING_STATUS_ACTIVE:
        return []
    principal = await session.get(Principal, agent.principal_id)
    if principal is None or principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot reopen the binding of a non-active principal",
            details={"status": principal.status if principal is not None else None},
        )
    return await _apply_identity(
        session, ctx, agent, await _revision_identity(session, ctx, agent, revision)
    )


# --- identity replacement (amendment 2026-09-30) ------------------------------


def _check_identity_authority(ctx: AuthContext, spec: dict[str, Any]) -> list[str]:
    """Whoever hands the agent's rights to a new identity must hold them (§5).

    The same rule as publishing the revision: a new IAM identity gets exactly
    the rights of the current revision, so the caller could otherwise take the
    identity of a service holding more than it holds itself.
    """
    identity = spec["identity"]
    permissions = validate_binding_permissions(
        ctx,
        permissions=list(identity["permissions"]),
        principal_kind=identity.get("kind", "agent"),
    )
    assigns = identity.get("roles") or identity.get("capabilities") or _invoked_refs(spec)
    if assigns and not ctx.has(Permission.ORG_MANAGE):
        raise AuthorizationError(
            "Assigning roles, capabilities or skills requires org.manage",
            code="permission_escalation",
            details={"missing": [Permission.ORG_MANAGE.value]},
        )
    return permissions


async def replace_agent_identity(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    issuer: str,
    iam_tenant_id: uuid.UUID,
    iam_principal_id: uuid.UUID,
    reason: str,
    trusted_issuer: str,
) -> AgentView:
    """Move a service agent to a new IAM identity; the principal stays the same.

    Only ``identity.kind: service``: a service account re-created in IAM is
    the same service, while any other agent that changes its identity is a
    new one (§6, retirement and a new key). Every other unrevoked binding of
    the principal is revoked — the registry's previous one and any made
    beside it — and the new identity gets the rights of the current revision,
    all in one transaction. Replacing with the identity already linked
    changes nothing. The issuer is the one the core trusts, or without one
    configured the agent's current issuer.
    """
    await authorize(ctx, Permission.AGENTS_MANAGE)
    agent = await require_agent(session, ctx, key, for_update=True)
    # The principal ``FOR UPDATE`` before its bindings, the order of
    # ``principals/{id}:disable`` and ``:retire`` (CP-ADR-0077).
    if agent.principal_id is not None:
        principal = await lock_caller_and_principal_for_update(session, ctx, agent.principal_id)
    else:
        await lock_caller(session, ctx)
        principal = None
    revision = await _current_revision(session, agent)
    if agent.status == AgentStatus.RETIRED:
        raise retired_conflict(agent)
    if agent.principal_id is None or principal is None:
        raise ConflictError(
            "agent_identity_not_linked",
            "The agent has no IAM identity yet; link it with PUT /agents/{key}/identity",
            details={"agent": key},
        )
    if principal.kind != PrincipalKind.SERVICE:
        raise ConflictError(
            "agent_identity_conflict",
            "Only a service agent changes its IAM identity; retire it and publish a new key",
            details={"agent": key, "kind": principal.kind},
        )
    if principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot bind an identity to a non-active principal",
            details={"status": principal.status},
        )
    check_trusted_issuer(issuer, trusted_issuer or agent.iam_issuer or "")
    permissions = _check_identity_authority(ctx, revision.spec)

    previous = (agent.iam_issuer, agent.iam_tenant_id, agent.iam_principal_id)
    if previous == (issuer, iam_tenant_id, iam_principal_id):
        return AgentView(agent, revision)  # idempotent
    # The new binding is tenant-wide, as every binding of the registry: not
    # one a caller in members mode makes (CP-ADR-0082 B5, V3).
    check_agent_binding_escalation(ctx)

    # In id order: every binding of the principal and the requested one.
    rows = (
        await session.scalars(
            select(IamPrincipalBinding)
            .where(
                or_(
                    IamPrincipalBinding.principal_id == principal.id,
                    and_(
                        IamPrincipalBinding.issuer == issuer,
                        IamPrincipalBinding.iam_principal_id == iam_principal_id,
                    ),
                )
            )
            .order_by(IamPrincipalBinding.id)
            .with_for_update()
        )
    ).all()
    requested = next(
        (r for r in rows if (r.issuer, r.iam_principal_id) == (issuer, iam_principal_id)), None
    )
    if requested is not None and (
        requested.tenant_id != ctx.tenant_id or requested.principal_id != principal.id
    ):
        raise identity_taken(requested, ctx, key)

    now = utcnow()
    touched: list[tuple[str, uuid.UUID]] = []
    # The registry's previous binding and any made beside it: after the
    # replacement the new identity is the only way in as this principal. Only
    # ``iamTenantId`` differs: that row is the requested one, updated below.
    for old in rows:
        if old is requested or old.principal_id != principal.id:
            continue
        if old.status == BINDING_STATUS_REVOKED:
            continue
        old.status = BINDING_STATUS_REVOKED
        old.revoked_at = now
        old.updated_at = now
        await session.flush()
        await _record_binding_event(session, ctx, "iam_binding.revoked", old)
        touched.append((old.issuer, old.iam_principal_id))

    if requested is None:
        binding = IamPrincipalBinding(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            principal_id=principal.id,
            issuer=issuer,
            iam_tenant_id=iam_tenant_id,
            iam_principal_id=iam_principal_id,
            permissions=permissions,
            status=BINDING_STATUS_ACTIVE,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        await _insert_binding(session, ctx, binding, key)
        await _record_binding_event(session, ctx, "iam_binding.created", binding)
    else:
        # A binding of this very principal made beside the registry (the
        # ADR-0053 workaround): adopted, with the rights of the revision.
        binding = requested
        binding.iam_tenant_id = iam_tenant_id
        binding.permissions = permissions
        binding.status = BINDING_STATUS_ACTIVE
        binding.revoked_at = None
        binding.updated_at = now
        await session.flush()
        await _record_binding_event(session, ctx, "iam_binding.updated", binding)
    touched.append((issuer, iam_principal_id))

    agent.iam_issuer = issuer
    agent.iam_tenant_id = iam_tenant_id
    agent.iam_principal_id = iam_principal_id
    agent.version += 1
    agent.updated_at = now
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="agent.identity_replaced",
        entity_type="agent",
        entity_id=agent.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": agent.key,
            "revision": agent.current_revision,
            "principalId": str(principal.id),
            "issuer": issuer,
            "iamTenantId": str(iam_tenant_id),
            "iamPrincipalId": str(iam_principal_id),
            "previousIssuer": previous[0],
            "previousIamTenantId": str(previous[1]) if previous[1] is not None else None,
            "previousIamPrincipalId": str(previous[2]) if previous[2] is not None else None,
            "reason": event_reason(reason),
        },
    )
    await session.flush()
    return AgentView(agent, revision, touched_identities=touched)


# --- retirement (§9) ----------------------------------------------------------


async def retire_agent(
    session: AsyncSession, ctx: AuthContext, *, key: str, reason: str
) -> AgentView:
    """Stop the agent for good: revoke its bindings, disable its principal, free its claims."""
    await authorize(ctx, Permission.AGENTS_MANAGE)
    agent = await require_agent(session, ctx, key, for_update=True)
    # The agent's principal ``FOR UPDATE`` and the caller's ``FOR KEY SHARE``,
    # in id order (the write flow leaves the caller to this command,
    # CP-ADR-0077 §3).
    if agent.principal_id is not None:
        await lock_caller_and_principal_for_update(session, ctx, agent.principal_id)
    else:
        await lock_caller(session, ctx)
    revision = await _current_revision(session, agent)
    if agent.status == AgentStatus.RETIRED:
        return AgentView(agent, revision)  # idempotent

    now = utcnow()
    touched: list[tuple[str, uuid.UUID]] = []
    released: list[uuid.UUID] = []
    if agent.principal_id is not None:
        # Lock order shared with ``principals/{id}:disable`` (CP-ADR-0077):
        # principal, then its bindings, then sessions, task and claim.
        principal = await session.get(Principal, agent.principal_id)  # locked above
        if principal is not None and principal.status != PrincipalStatus.DISABLED:
            principal.status = PrincipalStatus.DISABLED
            principal.updated_at = now
        bindings = (
            await session.scalars(
                select(IamPrincipalBinding)
                .where(
                    IamPrincipalBinding.tenant_id == ctx.tenant_id,
                    IamPrincipalBinding.principal_id == agent.principal_id,
                    IamPrincipalBinding.status != BINDING_STATUS_REVOKED,
                )
                .order_by(IamPrincipalBinding.id)
                .with_for_update()
            )
        ).all()
        for binding in bindings:
            binding.status = BINDING_STATUS_REVOKED
            binding.revoked_at = now
            binding.updated_at = now
            await _record_binding_event(session, ctx, "iam_binding.revoked", binding)
            touched.append((binding.issuer, binding.iam_principal_id))
        released = await release_active_claims_of_holder(
            session,
            tenant_id=ctx.tenant_id,
            holder_id=agent.principal_id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            reason=RETIRE_RELEASE_REASON,
        )

    agent.status = AgentStatus.RETIRED
    agent.state = AgentState.STOPPED
    agent.retired_at = now
    agent.retired_by = ctx.principal_id
    agent.version += 1
    agent.updated_at = now
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="agent.retired",
        entity_type="agent",
        entity_id=agent.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": agent.key,
            "revision": agent.current_revision,
            "principalId": str(agent.principal_id) if agent.principal_id else None,
            "reason": event_reason(reason),
            "releasedClaims": len(released),
        },
    )
    return AgentView(agent, revision, touched_identities=touched)


# --- observed state (§4) ------------------------------------------------------


@dataclass(frozen=True)
class StatusReport:
    phase: str
    reason_code: str | None
    reason_message: str | None
    observed_revision: int | None
    node: str | None
    instances_desired: int
    instances_ready: int
    observed_at: datetime


async def get_agent_status(
    session: AsyncSession, ctx: AuthContext, *, key: str
) -> tuple[Agent, AgentObservedStatus | None]:
    await authorize(ctx, Permission.AGENTS_READ)
    agent = await require_agent(session, ctx, key)
    return agent, await session.get(AgentObservedStatus, agent.id)


async def report_agent_status(
    session: AsyncSession, ctx: AuthContext, *, key: str, report: StatusReport
) -> tuple[Agent, AgentObservedStatus]:
    """Store what the placement service sees; journal only a change that matters."""
    await authorize(ctx, Permission.AGENTS_STATUS_WRITE)
    agent = await require_agent(session, ctx, key, for_update=True)
    if agent.status == AgentStatus.RETIRED:
        raise retired_conflict(agent)
    row = await session.get(AgentObservedStatus, agent.id, with_for_update=True)
    if row is not None and report.observed_at < row.observed_at:
        raise ConflictError(
            "stale_status_report",
            "A newer status report is already stored",
            details={"agent": key, "observedAt": row.observed_at.isoformat()},
        )

    previous = (
        (row.phase, row.reason_code, row.node, row.observed_revision) if row is not None else None
    )
    now = utcnow()
    if row is None:
        row = AgentObservedStatus(agent_id=agent.id, tenant_id=ctx.tenant_id)
        session.add(row)
    row.phase = report.phase
    row.reason_code = report.reason_code
    row.reason_message = report.reason_message
    row.observed_revision = report.observed_revision
    row.node = report.node
    row.instances_desired = report.instances_desired
    row.instances_ready = report.instances_ready
    row.observed_at = report.observed_at
    row.reported_by = ctx.principal_id
    row.updated_at = now
    await session.flush()

    if previous != (row.phase, row.reason_code, row.node, row.observed_revision):
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="agent.status_changed",
            entity_type="agent",
            entity_id=agent.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "key": agent.key,
                "phase": row.phase,
                "previousPhase": previous[0] if previous is not None else None,
                "reasonCode": row.reason_code,
                "node": row.node,
                "observedRevision": row.observed_revision,
                "observedAt": row.observed_at.isoformat(),
            },
        )
    return agent, row


# --- the revision of a run (§7) -----------------------------------------------


async def check_run_agent_revision(
    session: AsyncSession, ctx: AuthContext, agent_revision_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The revision a run is recorded with; the rules of ``start-run`` (§7).

    A principal linked to an active agent names a revision of its own agent —
    not necessarily the current one: a publication racing a start must not
    fail the work. Anyone else names none.
    """
    agent = await active_agent_of_principal(session, ctx.tenant_id, ctx.principal_id)
    if agent is None:
        if agent_revision_id is not None:
            raise ValidationError(
                "agent_revision_mismatch",
                "The caller is not a registered agent and runs by no agent revision",
                details={"agentRevisionId": str(agent_revision_id)},
            )
        return None
    if agent_revision_id is None:
        raise ValidationError(
            "agent_revision_required",
            "A registered agent names the revision it runs by",
            details={"agent": agent.key, "currentRevision": agent.current_revision},
        )
    owner = await session.scalar(
        select(AgentRevision.agent_id).where(
            AgentRevision.id == agent_revision_id, AgentRevision.tenant_id == ctx.tenant_id
        )
    )
    if owner != agent.id:
        raise ValidationError(
            "agent_revision_mismatch",
            "The revision does not belong to the caller's agent",
            details={"agent": agent.key, "agentRevisionId": str(agent_revision_id)},
        )
    return agent_revision_id
