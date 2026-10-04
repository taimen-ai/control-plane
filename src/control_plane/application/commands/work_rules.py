"""Work rule commands: create, change, enable, disable, archive (CP-ADR-0063).

A rule is tenant data that files work on its own. Writing one needs
``rules.write`` on the rule's workspace (the tenant for a tenant-level rule);
what the rule then does, it does with the authority of the credential that
last enabled it — never with more than that principal could do by hand.

Every document is validated by ``domain/work_rules.py`` on write; the names a
rule refers to (a task type key, a pinned skill) must exist when it is
written, and a skill with ``external_write`` side effects is refused: a rule
has no approval to cite as the basis of an external action (ADR-0056 §4).

A rule may instead act as an agent of the registry (``identity: {agent}``,
CP-ADR-0063 amendment 2026-09-27, G1): it is then evaluated with the
authority of that agent's principal. Whoever writes such a rule must hold
every permission the agent's current revision declares — the check a
revision itself passes (CP-ADR-0073 §5) — or ``rules.write`` would lend the
rights of any agent.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.agents import revision_of
from control_plane.application.commands.approval_outcomes import authority_snapshot
from control_plane.application.commands.goals import require_linkable_goal
from control_plane.application.commands.iam_bindings import validate_binding_permissions
from control_plane.application.commands.role_references import (
    is_role_reference,
    require_declared_role,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.package_settings import object_scope
from control_plane.domain.enums import AgentStatus, Permission, SkillSideEffects
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.settings_refs import SettingsScope
from control_plane.domain.work_rules import (
    RuleSpec,
    RuleStatus,
    TriggerKind,
    check_settings_refs,
    normalize_description,
    normalize_rule_key,
    normalize_rule_spec,
    settings_reads,
)
from control_plane.infrastructure.db.models import Agent, Skill, TaskType, WorkRule

_UNSET: Any = object()

# The journal consumer of the rule engine (event_consumer_cursors.name).
RULES_CONSUMER = "work-rules"


def rule_scope(workspace_id: uuid.UUID | None) -> ResourceRef | None:
    """Where ``rules.*`` is decided: the rule's workspace, or the tenant."""
    return ResourceRef("workspace", str(workspace_id)) if workspace_id else None


async def get_tenant_rule(
    session: AsyncSession, ctx: AuthContext, rule_id: uuid.UUID, *, for_update: bool = False
) -> WorkRule:
    stmt = select(WorkRule).where(WorkRule.id == rule_id, WorkRule.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    rule = await session.scalar(stmt)
    # A workspace outside the caller's visibility answers exactly as a missing
    # row, never as a missing workspace (CP-ADR-0082 §3.7).
    if rule is None or (
        rule.workspace_id is not None and not ctx.sees_workspace(rule.workspace_id)
    ):
        raise NotFoundError("Rule not found", details={"ruleId": str(rule_id)})
    return rule


async def rule_write_gate(
    session: AsyncSession, ctx: AuthContext, rule_id: uuid.UUID, *, for_update: bool = False
) -> WorkRule:
    """Who may enable or disable the rule; ``POST /authz:check`` asks the same."""
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=for_update)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    return rule


async def ensure_rule_cursor(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The tenant's journal cursor of the rule engine, created at the present.

    Placed after the newest event below the stable horizon: everything the
    consumer will ever read after it commits later than this point. A rule
    never looks at the past it was not enabled for (``enabled_at``), so
    replaying the tenant's history would only be work that matches nothing.
    """
    await ensure_consumer_cursor(session, tenant_id, RULES_CONSUMER)


async def ensure_consumer_cursor(session: AsyncSession, tenant_id: uuid.UUID, name: str) -> None:
    """A journal cursor ``name`` of the tenant, created at the present (see above)."""
    await session.execute(
        text(
            """
            INSERT INTO event_consumer_cursors
                (name, tenant_id, tx_id, sequence, updated_at, metadata, failure_count)
            SELECT :name, :tenant, COALESCE(MAX(p.tx_id), 0),
                   COALESCE((ARRAY_AGG(p.sequence ORDER BY p.tx_id DESC, p.sequence DESC))[1], 0),
                   now(), '{}'::jsonb, 0
              FROM (
                    (SELECT e.tx_id, e.sequence FROM events e
                      WHERE e.tenant_id = :tenant
                        AND e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint
                      ORDER BY e.tx_id DESC, e.sequence DESC LIMIT 1)
                    UNION ALL
                    (SELECT a.tx_id, a.sequence FROM event_archive a
                      WHERE a.tenant_id = :tenant
                      ORDER BY a.tx_id DESC, a.sequence DESC LIMIT 1)
                   ) p
            ON CONFLICT (name, tenant_id) DO NOTHING
            """
        ),
        {"name": name, "tenant": tenant_id},
    )


async def _check_references(session: AsyncSession, ctx: AuthContext, spec: RuleSpec) -> None:
    """The task types, the skill and the author agent a rule names exist now (typos fail early)."""
    author = spec.trigger.get("agent")
    if author is not None:
        status = await session.scalar(
            select(Agent.status).where(Agent.tenant_id == ctx.tenant_id, Agent.key == author)
        )
        if status != AgentStatus.ACTIVE:
            raise ValidationError(
                "unknown_agent",
                f"No active agent {author!r} in the registry",
                details={"field": "trigger.agent", "agent": author},
            )
    allowed = spec.action.get("taskTypes")
    if allowed is not None:
        active = set(
            (
                await session.scalars(
                    select(TaskType.key).where(
                        TaskType.tenant_id == ctx.tenant_id,
                        TaskType.key.in_(allowed),
                        TaskType.status == "active",
                    )
                )
            ).all()
        )
        for index, key in enumerate(allowed):
            if key not in active:
                raise ValidationError(
                    "unknown_task_type",
                    f"Task type {key!r} has no active version",
                    details={"field": f"action.taskTypes[{index}]", "taskType": key},
                )
    type_key = spec.action.get("taskType")
    if type_key is not None and allowed is None:
        found = await session.scalar(
            select(func.count())
            .select_from(TaskType)
            .where(TaskType.tenant_id == ctx.tenant_id, TaskType.key == type_key)
        )
        if not found:
            raise ValidationError(
                "unknown_task_type",
                f"Task type {type_key!r} is not registered",
                details={"field": "action.taskType", "taskType": type_key},
            )
    approver_role = (spec.action.get("fields") or {}).get("approverRole")
    # A role of the package by slug (CP-ADR-0061, amendment 2026-10-01); a
    # template is only known when it renders, and is checked then.
    if is_role_reference(approver_role) and "{{" not in approver_role:
        await require_declared_role(
            session, ctx.tenant_id, approver_role, field="action.fields.approverRole"
        )
    if spec.interpretation is not None:
        ref = spec.interpretation["skill"]
        name, version = ref.split("@", 1)
        skill = await session.scalar(
            select(Skill).where(
                Skill.tenant_id == ctx.tenant_id, Skill.name == name, Skill.version == version
            )
        )
        if skill is None:
            raise ValidationError(
                "unknown_skill",
                f"Skill {ref!r} is not registered",
                details={"field": "interpretation.skill", "skill": ref},
            )
        if skill.side_effects == SkillSideEffects.EXTERNAL_WRITE:
            raise ValidationError(
                "rule_skill_side_effects",
                "A rule interprets facts; it cannot call an external_write skill "
                "(it has no approval to cite as the basis)",
                details={"field": "interpretation.skill", "skill": ref},
            )


async def check_identity(
    session: AsyncSession, ctx: AuthContext, agent_key: str, *, invalid_code: str = "invalid_rule"
) -> None:
    """The agent a rule (or a process) is to act as exists, and the writer may lend its rights.

    Whether the agent is linked (has a principal yet) is not asked: a package
    applies the agent and its rules together, and the placement service links
    the identity later (CP-ADR-0073 §6). Until then the rule's evaluations
    fail ``credential_inactive``.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.tenant_id == ctx.tenant_id, Agent.key == agent_key)
    )
    if agent is None or agent.status != AgentStatus.ACTIVE:
        raise ValidationError(
            "unknown_agent",
            f"No active agent {agent_key!r} in the registry",
            details={"field": "identity.agent", "agent": agent_key},
        )
    revision = await revision_of(session, agent, agent.current_revision)
    assert revision is not None
    identity = revision.spec.get("identity") or {}
    kind = identity.get("kind", "agent")
    if kind not in ("agent", "service"):
        raise ValidationError(
            invalid_code,
            f"A rule or a process acts as an agent or a service; {agent_key!r} is {kind!r}",
            details={"field": "identity.agent", "agent": agent_key, "kind": kind},
        )
    validate_binding_permissions(
        ctx, permissions=list(identity.get("permissions") or ()), principal_kind=kind
    )


def _identity_key(identity: Any) -> str | None:
    """``{agent: key}`` (or ``None``) as the stored key; the API has checked its form."""
    if identity is None:
        return None
    if not isinstance(identity, dict) or set(identity) != {"agent"}:
        raise ValidationError(
            "invalid_rule", "identity must be {agent: <key>}", details={"field": "identity"}
        )
    key = identity["agent"]
    if not isinstance(key, str) or not key:
        raise ValidationError(
            "invalid_rule", "identity.agent must be an agent key", details={"field": "identity"}
        )
    return key


def _validate_status(status: Any) -> str:
    if status not in (RuleStatus.ENABLED, RuleStatus.DISABLED):
        raise ValidationError(
            "invalid_rule", "status must be 'enabled' or 'disabled'", details={"field": "status"}
        )
    return str(status)


def _schedule_next_run(rule: WorkRule) -> None:
    """A schedule rule runs at the next pass after being enabled, then every N seconds."""
    if rule.status == RuleStatus.ENABLED and rule.trigger.get("kind") == TriggerKind.SCHEDULE:
        rule.next_run_at = rule.next_run_at or utcnow()
    else:
        rule.next_run_at = None


def _take_authority(rule: WorkRule, ctx: AuthContext) -> None:
    rule.authority = authority_snapshot(ctx)
    rule.authority_principal_id = ctx.principal_id


def _summary(rule: WorkRule) -> dict[str, Any]:
    """What the journal says about a rule: shape and references, no templates."""
    return {
        "key": rule.key,
        "version": rule.version,
        "status": rule.status,
        "workspaceId": str(rule.workspace_id) if rule.workspace_id else None,
        "goalId": str(rule.goal_id) if rule.goal_id else None,
        "trigger": {"kind": rule.trigger.get("kind"), "type": rule.trigger.get("type")},
        "skill": (rule.interpretation or {}).get("skill"),
        "action": {"kind": rule.action.get("kind"), "taskType": rule.action.get("taskType")},
    }


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="rule",
        entity_id=rule.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"ruleId": str(rule.id), **payload},
    )


async def lock_rule_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize the creation of a rule with one (tenant, key)."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"work-rule:{tenant_id}:{key}")))
    )


async def _require_free_key(session: AsyncSession, ctx: AuthContext, key: str) -> None:
    # Serialized per (tenant, key): the partial unique index would otherwise
    # turn a concurrent twin into a 500 instead of this 409.
    await lock_rule_key(session, ctx.tenant_id, key)
    taken = await session.scalar(
        select(WorkRule.id).where(
            WorkRule.tenant_id == ctx.tenant_id,
            WorkRule.key == key,
            WorkRule.status != RuleStatus.ARCHIVED,
        )
    )
    if taken is not None:
        raise ConflictError(
            "rule_key_taken",
            "A rule with this key already exists",
            details={"key": key, "ruleId": str(taken)},
        )


async def check_rule_settings(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    spec: RuleSpec,
    settings: SettingsScope | None,
) -> None:
    """The reads of ``settings`` against the schema of the rule's package (CP-ADR-0081 §6).

    ``settings`` — given by the apply of a package; ``None`` — the active
    revision of the package that installed ``key``. A rule not from a package
    has no settings: ``settings_ref_unknown``.
    """
    if not settings_reads(spec):
        return
    if settings is None:
        settings = await object_scope(session, tenant_id, "WorkRule", key)
    check_settings_refs(spec, settings)


async def create_rule(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    trigger: dict[str, Any],
    action: dict[str, Any],
    condition: Any = None,
    interpretation: dict[str, Any] | None = None,
    description: str | None = None,
    workspace_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    status: str = RuleStatus.ENABLED,
    identity: dict[str, Any] | None = None,
    settings: SettingsScope | None = None,
) -> WorkRule:
    """A new rule; ``settings`` — the settings of the package that installs it (its apply)."""
    from control_plane.application.commands.workspaces import require_active_workspace

    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(workspace_id))
    rule_key = normalize_rule_key(key)
    text_description = normalize_description(description)
    spec = normalize_rule_spec(
        trigger=trigger, condition=condition, interpretation=interpretation, action=action
    )
    await check_rule_settings(session, ctx.tenant_id, rule_key, spec, settings)
    rule_status = _validate_status(status)
    agent_key = _identity_key(identity)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)
    if goal_id is not None:
        await require_linkable_goal(session, ctx, goal_id, workspace_id=workspace_id)
    await _check_references(session, ctx, spec)
    if agent_key is not None:
        await check_identity(session, ctx, agent_key)
    await _require_free_key(session, ctx, rule_key)

    now = utcnow()
    rule = WorkRule(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        goal_id=goal_id,
        key=rule_key,
        description=text_description,
        version=1,
        status=rule_status,
        trigger=spec.trigger,
        condition=spec.condition,
        interpretation=spec.interpretation,
        action=spec.action,
        authority=None,
        authority_principal_id=None,
        identity_agent_key=agent_key,
        enabled_at=None,
        next_run_at=None,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    if rule_status == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
        rule.enabled_at = now
    _schedule_next_run(rule)
    session.add(rule)
    await session.flush()
    await ensure_rule_cursor(session, ctx.tenant_id)
    await _record(session, ctx, rule, "rule.created", _summary(rule))
    return rule


async def update_rule(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    rule_id: uuid.UUID,
    expected_version: int,
    description: str | Any = _UNSET,
    trigger: dict[str, Any] | Any = _UNSET,
    condition: Any = _UNSET,
    interpretation: dict[str, Any] | Any | None = _UNSET,
    action: dict[str, Any] | Any = _UNSET,
    goal_id: uuid.UUID | Any | None = _UNSET,
    identity: dict[str, Any] | Any | None = _UNSET,
    settings: SettingsScope | None = None,
) -> WorkRule:
    """Change what a rule does; the key and the workspace are what it is.

    A change of an enabled rule moves its authority to the writer: whoever
    decided what the rule does now answers for it. A PATCH that restates the
    current values is not a change (no new version, no event). A rule that
    acts as an agent after the change has the writer checked against that
    agent's rights, whichever field changed: a new action is done with them.
    """
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=True)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    _require_live(rule)
    if rule.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Rule version does not match If-Match",
            details={
                "ruleId": str(rule.id),
                "expectedVersion": expected_version,
                "currentVersion": rule.version,
            },
        )
    provided = [
        v
        for v in (description, trigger, condition, interpretation, action, goal_id, identity)
        if v is not _UNSET
    ]
    if not provided:
        raise ValidationError("empty_update", "No fields to update")

    spec = normalize_rule_spec(
        trigger=rule.trigger if trigger is _UNSET else trigger,
        condition=rule.condition if condition is _UNSET else condition,
        interpretation=rule.interpretation if interpretation is _UNSET else interpretation,
        action=rule.action if action is _UNSET else action,
    )
    await check_rule_settings(session, ctx.tenant_id, rule.key, spec, settings)
    changes: dict[str, Any] = {
        "trigger": spec.trigger,
        "condition": spec.condition,
        "interpretation": spec.interpretation,
        "action": spec.action,
    }
    if description is not _UNSET:
        changes["description"] = normalize_description(description)
    if goal_id is not _UNSET:
        if goal_id is not None:
            await require_linkable_goal(session, ctx, goal_id, workspace_id=rule.workspace_id)
        changes["goal_id"] = goal_id
    if identity is not _UNSET:
        changes["identity_agent_key"] = _identity_key(identity)
    changes = {k: v for k, v in changes.items() if getattr(rule, k) != v}
    if not changes:
        return rule
    await _check_references(session, ctx, spec)
    agent_key = changes.get("identity_agent_key", rule.identity_agent_key)
    if agent_key is not None:
        await check_identity(session, ctx, agent_key)

    for field_name, value in changes.items():
        setattr(rule, field_name, value)
    if "trigger" in changes:
        rule.next_run_at = None
        _schedule_next_run(rule)
    if rule.status == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
    rule.version += 1
    rule.updated_at = utcnow()
    await session.flush()
    await _record(
        session,
        ctx,
        rule,
        "rule.updated",
        {"changes": sorted(_change_name(k) for k in changes), **_summary(rule)},
    )
    return rule


def _change_name(name: str) -> str:
    if name == "identity_agent_key":
        return "identity"
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _require_live(rule: WorkRule) -> None:
    if rule.status == RuleStatus.ARCHIVED:
        raise ConflictError(
            "rule_archived",
            "An archived rule cannot be changed or enabled",
            details={"ruleId": str(rule.id)},
        )


async def set_rule_status(
    session: AsyncSession, ctx: AuthContext, *, rule_id: uuid.UUID, status: str
) -> WorkRule:
    """Enable or disable a rule; repeating the current state changes nothing.

    Enabling takes the caller's authority and starts the rule at the present:
    facts recorded while it was disabled are not evaluated after the fact.
    """
    rule = await rule_write_gate(session, ctx, rule_id, for_update=True)
    _require_live(rule)
    target = _validate_status(status)
    if rule.status == target:
        return rule
    now = utcnow()
    rule.status = target
    if target == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
        rule.enabled_at = now
        await ensure_rule_cursor(session, ctx.tenant_id)
    rule.next_run_at = None
    _schedule_next_run(rule)
    rule.updated_at = now
    await session.flush()
    await _record(
        session,
        ctx,
        rule,
        "rule.enabled" if target == RuleStatus.ENABLED else "rule.disabled",
        _summary(rule),
    )
    return rule


async def archive_rule(session: AsyncSession, ctx: AuthContext, *, rule_id: uuid.UUID) -> None:
    """Take a rule out of service for good (``DELETE``); its history stays.

    The work it filed is not touched; evaluations still waiting on a skill
    end as ``skipped``. The key becomes free for a new rule.
    """
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=True)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    if rule.status == RuleStatus.ARCHIVED:
        return
    rule.status = RuleStatus.ARCHIVED
    rule.next_run_at = None
    rule.updated_at = utcnow()
    await session.flush()
    await _record(session, ctx, rule, "rule.archived", _summary(rule))


def schedule_slot(rule: WorkRule) -> tuple[str, Any]:
    """The trigger ref of a due schedule and the rule's next run time."""
    every = timedelta(seconds=int(rule.trigger["everySeconds"]))
    assert rule.next_run_at is not None
    due = rule.next_run_at
    now = utcnow()
    upcoming = due + every
    # A worker that was down does not replay every missed slot: one run now.
    if upcoming <= now:
        upcoming = now + every
    return f"schedule:{int(due.timestamp())}", upcoming
