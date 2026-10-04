"""The rule engine: evaluate work rules on journal events and schedules (CP-ADR-0063).

The worker drives three loops, each in its own short transactions:

* **journal** — per tenant, the ``work-rules`` cursor over the event journal
  (the same ``(tx_id, sequence)`` replay order and stable horizon as the
  Context Adapter). A batch is evaluated and the cursor advanced in ONE
  transaction, so a crash re-delivers the batch and the unique
  ``(rule_id, trigger_ref)`` of ``rule_evaluations`` makes the repeat a no-op.
  A rule sees only events recorded after it was enabled, and never events
  caused by rules (correlation ``work-rule:…``, the ``rule`` entity, the life
  of a skill call a rule queued): a rule cannot feed itself. Each evaluation
  runs in a savepoint; once a batch has failed ``max_attempts`` times, an
  evaluation that still breaks is recorded ``failed`` (``rule_internal_error``)
  and the batch goes on, so one broken rule does not stop a tenant;
* **schedule** — enabled schedule rules whose ``next_run_at`` is due;
* **waiting** — evaluations whose interpretation skill call was queued. The
  call goes through ``invoke_skill`` — the path of ``POST /skills/{ref}:invoke``
  and of ``invokeSkill`` in approval outcomes — with the rule's authority, and
  the evaluation resumes once it has ended, without holding the worker: its
  result becomes an artifact and that artifact becomes evidence.

**Authority.** Every evaluation acts as the principal whose credential last
enabled the rule (a snapshot, checked for being still active on every
evaluation — the approval-outcome rule). A rule with ``identity: {agent}``
acts as that agent's principal instead, with its IAM binding as it stands at
the evaluation (CP-ADR-0063 amendment 2026-09-27, G1). Reading the facts needs
``events.read`` where the rule lives; every write goes through the ordinary
command (``create_task``, ``update_task``, ``request_approval``) and its checks.

**Unavailable dependencies.** A decision that could not be obtained
(``DependencyUnavailableError``, the PDP in ``policy`` mode) is not a verdict
on the rule: it is never recorded as ``failed`` but raised, so the batch
rolls back and is retried with backoff, and a waiting evaluation is looked at
again later.

**Outcomes.** ``matched`` (the action ran for at least one item),
``not_matched``, ``failed`` (a domain refusal, an evaluation error, a failed
interpretation — nothing written but the evaluation and its evidence),
``skipped`` (the rule changed or stopped while it waited). Each final outcome
is one ``rule.evaluated`` event; work filed is ``work.derived``, work updated or
cancelled is ``work.reconciled``.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    permits_task,
)
from control_plane.application.commands._artifact_content import artifact_event_fields
from control_plane.application.commands.agent_assignees import is_agent_reference
from control_plane.application.commands.approval_outcomes import (
    authority_snapshot,
    require_active_credential,
)
from control_plane.application.commands.approvals import request_approval
from control_plane.application.commands.eligibility import RequirementSpec
from control_plane.application.commands.goals import get_readable_goal
from control_plane.application.commands.relations import add_relation, resolve_task
from control_plane.application.commands.role_references import is_role_reference, role_for_task
from control_plane.application.commands.runs import request_cancel_run
from control_plane.application.commands.skill_invocations import (
    CANCELLED_BY_SYSTEM,
    LIVE_STATUSES,
    cancel_skill_invocation,
    invoke_skill,
)
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import (
    complete_task,
    create_task,
    live_claim_of,
    update_task,
)
from control_plane.application.commands.verification import (
    current_evidence,
    latest_attempts,
    open_attempt,
)
from control_plane.application.commands.work_rules import (
    RULES_CONSUMER,
    rule_scope,
    schedule_slot,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.event_cursor import EventPosition
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principal_key_share, lock_rule_principals
from control_plane.application.queries.events import JournalEvent, fetch_events_after
from control_plane.application.queries.package_settings import history, object_scope, snapshot
from control_plane.application.visibility import with_visibility
from control_plane.domain.enums import (
    AgentStatus,
    Permission,
    RunStatus,
    SkillInvocationStatus,
    TaskPriority,
    TaskRelationType,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DependencyUnavailableError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_graph import (
    CHECK_KEY_RE,
    RULE_EVIDENCE_CHECK,
    CheckKind,
    origin_summary,
)
from control_plane.domain.work_item import TERMINAL_CATEGORIES, WorkItemStatusCategory
from control_plane.domain.work_rules import (
    BASE_ROOTS,
    CREATING_ACTIONS,
    CUSTOM_FIELDS,
    MAX_DEPENDENCIES,
    MAX_FOR_EACH_ITEMS,
    MAX_TEXT_FIELD_LENGTH,
    MAX_TITLE_LENGTH,
    RELATION_DEPENDS_ON,
    RELATION_SPAWNED_BY,
    RELATIONS,
    ROLE_ASSIGNEE_PREFIX,
    ROOT_GOAL,
    ROOT_ITEM,
    ROOT_PAYLOAD,
    ROOT_SETTINGS,
    ROOT_SKILL,
    ROOT_TASK,
    ROOT_TRIGGER,
    WORKSPACE_FIELD,
    ActionKind,
    ActionTarget,
    ConditionError,
    EvaluationStatus,
    RuleStatus,
    TriggerKind,
    VarPath,
    action_roots,
    author_matches,
    evaluate,
    has_author_filter,
    normalize_dedup_key,
    parse_path,
    render,
    rule_roots,
    trigger_matches,
    walk,
)
from control_plane.infrastructure.db.models import (
    Agent,
    Artifact,
    Event,
    EventArchive,
    EventConsumerCursor,
    IamPrincipalBinding,
    Principal,
    RuleEvaluation,
    RuleWorkItem,
    Run,
    Skill,
    SkillInvocation,
    Task,
    TaskType,
    WorkRule,
)

logger = logging.getLogger(__name__)

# The correlation of everything a rule writes: the journal consumer skips it.
CORRELATION_PREFIX = "work-rule:"
# How often a waiting evaluation looks at its skill call.
DEFAULT_SKILL_CHECK_SECONDS = 15.0
# How long an interpretation call may sit unclaimed before the rule gives up.
DEFAULT_SKILL_WAIT_SECONDS = 24 * 3600.0
SKILL_WAIT_EXPIRED = "rule_skill_wait_expired"
# An evaluation that kept breaking for a reason that is not a domain refusal.
RULE_INTERNAL_ERROR = "rule_internal_error"
# Failed batches of a tenant before a still-breaking evaluation is recorded
# failed instead of holding the whole tenant back.
DEFAULT_MAX_ATTEMPTS = 3
# Evaluations that waited for a skill and then found the rule changed/stopped.
RULE_CHANGED = "rule_changed"
RULE_STOPPED = "rule_stopped"
# A skill result, as artifact type: the same one a task-bound call leaves.
SKILL_RESULT_ARTIFACT = "skill_result"
# The kind of the element of an evaluation's evidence naming the settings it read
# (CP-ADR-0081 §6), and the kind of a rule in ``package_objects``.
SETTINGS_EVIDENCE = "settings"
RULE_KIND = "WorkRule"
# Where core names its own journal entries in an external evidence pointer.
CORE_SYSTEM = "control-plane"
# A closing decision on work under a live claim waits for the claim to end
# (CP-ADR-0063, amendment A4): what it waits for, and how it can end.
WAITING_FOR_CLAIM = "claim"
DEFAULT_CLAIM_WAIT_SECONDS = 24 * 3600.0
TASK_CLAIMED = "task_claimed"
ALREADY_DONE = "already_done"
ALREADY_CLOSED = "already_closed"
VERIFICATION_PENDING = "verification_pending"
CLAIM_NOT_RELEASED = "claim_not_released"
# The check a rule closing work without acceptance verifies it by: the
# evidence the rule has just written (amendment A1).
IMPLICIT_CHECK: dict[str, Any] = {
    "key": RULE_EVIDENCE_CHECK,
    "kind": CheckKind.EXTERNAL_STATE.value,
    "description": "The fact the rule closed the work on",
}
# An evaluation whose every item was refused (amendment 2026-09-27, G5).
WORK_ITEMS_REFUSED = "work_items_refused"
# Refusals of one forEach item: the other items go on (G5).
TASK_TYPE_NOT_ALLOWED = "task_type_not_allowed"
INVALID_RELATIONS = "invalid_relations"
RELATION_TARGET_NOT_FOUND = "relation_target_not_found"
DEPENDENCY_NOT_FOUND = "dependency_not_found"
DEPENDENCY_REFUSED = "dependency_refused"
DEPENDENCY_CYCLE = "dependency_cycle"
# A closing action on the task its observation is bound to (amendment
# integrations-connections, Zh3): why it did not close it.
NO_BOUND_TASK = "no_bound_task"
BOUND_TASK_NOT_FOUND = "bound_task_not_found"
BOUND_TASK_TYPE_NOT_LISTED = "bound_task_type_not_listed"
BOUND_TASK_FORBIDDEN = "bound_task_forbidden"
# A rule stored before its trigger had to name the author (Zh6) closes nothing.
TRIGGER_AUTHOR_UNFILTERED = "trigger_author_unfiltered"
# Such work has no dedup key: its trail names it by this pseudo-key (Zh4).
BOUND_KEY_PREFIX = "task:"


def rule_context(
    rule: WorkRule,
    *,
    trace_run_id: str,
    causation_id: str | None = None,
    authority: dict[str, Any] | None = None,
) -> AuthContext:
    """The authority a rule acts with: the snapshot taken when it was enabled.

    ``authority`` overrides the stored snapshot (the rule's agent, G1).
    """
    if authority is None:
        authority = rule.authority or {}
        assert rule.authority_principal_id is not None
        principal_id = rule.authority_principal_id
    else:
        principal_id = uuid.UUID(str(authority["principalId"]))
    iam = authority.get("iamPrincipalId")
    return AuthContext(
        tenant_id=rule.tenant_id,
        principal_id=principal_id,
        principal_kind=str(authority.get("principalKind") or "human"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=f"{CORRELATION_PREFIX}{rule.id}",
        correlation_id=f"{CORRELATION_PREFIX}{rule.id}",
        causation_id=causation_id,
        trace_run_id=trace_run_id,
        iam_principal_id=uuid.UUID(iam) if iam else None,
    )


async def _agent_authority(session: AsyncSession, rule: WorkRule) -> dict[str, Any]:
    """The snapshot of the rule's agent: its principal and IAM binding, as they stand now.

    Read on every evaluation, so a new revision of the agent (its binding
    brought to it in place) changes what the rule may do from the next one.
    An agent that is gone, retired or not linked yet has no authority to
    lend: ``credential_inactive``, as for a revoked key of an enabler.
    """
    assert rule.identity_agent_key is not None
    return await agent_authority(session, rule.tenant_id, rule.identity_agent_key)


async def agent_authority(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, *, acting: str = "the rule"
) -> dict[str, Any]:
    """The authority snapshot of agent ``key``: its principal and IAM binding now.

    Shared by rules and processes (CP-ADR-0074 §14): whatever acts as an
    agent acts with the binding as it stands at the moment it acts.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.key == key)
    )
    binding = None
    principal = None
    if agent is not None and agent.status == AgentStatus.ACTIVE and agent.principal_id:
        binding = await session.scalar(
            select(IamPrincipalBinding).where(
                IamPrincipalBinding.principal_id == agent.principal_id,
                IamPrincipalBinding.issuer == agent.iam_issuer,
                IamPrincipalBinding.iam_principal_id == agent.iam_principal_id,
            )
        )
        principal = await session.get(Principal, agent.principal_id)
    if agent is None or binding is None or principal is None:
        raise AuthorizationError(
            f"The agent {key!r} {acting} acts as has no active identity",
            code="credential_inactive",
            details={"agent": key},
        )
    return {
        "principalId": str(principal.id),
        "principalKind": principal.kind,
        "credentialId": str(binding.id),
        "permissions": sorted(binding.permissions or ()),
        "iamPrincipalId": str(binding.iam_principal_id),
    }


@dataclass
class _Acting:
    """Whose authority an evaluation runs with, or why it cannot run.

    ``refusal`` is set when the rule's agent has no identity to act with; the
    context is then the enabler's, only to record the failed evaluation.
    """

    ctx: AuthContext
    refusal: DomainError | None = None

    async def require_standing(self, session: AsyncSession) -> None:
        if self.refusal is not None:
            raise self.refusal
        await require_active_credential(
            session,
            authority=authority_snapshot(self.ctx),
            principal_id=self.ctx.principal_id,
            subject="the rule acts with",
        )


async def _acting(
    session: AsyncSession, rule: WorkRule, *, trace_run_id: str, causation_id: str | None
) -> _Acting:
    acting = await _acting_unlocked(
        session, rule, trace_run_id=trace_run_id, causation_id=causation_id
    )
    # The rule acts as this principal: it goes before any task row the
    # evaluation touches (rule 1 of ``application/locking.py``, CP-ADR-0077
    # §3). Only the rule's own rows are held here, which
    # ``principals/{id}:disable`` never takes.
    await lock_principal_key_share(session, acting.ctx.tenant_id, acting.ctx.principal_id)
    return acting


async def _acting_unlocked(
    session: AsyncSession, rule: WorkRule, *, trace_run_id: str, causation_id: str | None
) -> _Acting:
    if rule.identity_agent_key is not None:
        try:
            authority = await _agent_authority(session, rule)
        except AuthorizationError as exc:
            ctx = rule_context(rule, trace_run_id=trace_run_id, causation_id=causation_id)
            return _Acting(ctx, exc)
        return _Acting(
            rule_context(
                rule, trace_run_id=trace_run_id, causation_id=causation_id, authority=authority
            )
        )
    # The snapshot of a person acts within their visibility as it stands now,
    # like a request of theirs (CP-ADR-0082 V2): a rule does not widen it.
    return _Acting(
        await with_visibility(
            session, rule_context(rule, trace_run_id=trace_run_id, causation_id=causation_id)
        )
    )


# --- facts ----------------------------------------------------------------------


@dataclass
class Facts:
    """What one evaluation reads; plain values, never ORM objects."""

    trigger: dict[str, Any]
    payload: dict[str, Any]
    # Pointers to the triggering fact (CP-ADR-0062 evidence items).
    evidence: list[dict[str, Any]]
    task_id: uuid.UUID | None = None
    goal: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    skill: dict[str, Any] | None = None
    views: set[str] = field(default_factory=set)
    # The effective settings of the rule's package and the evidence element
    # naming their version (CP-ADR-0081 §6), read once an evaluation.
    settings: dict[str, Any] | None = None
    settings_seen: dict[str, Any] | None = None

    def resolve(self, path: VarPath, item: Any = None) -> Any:
        documents: dict[str, Any] = {
            ROOT_TRIGGER: self.trigger,
            ROOT_PAYLOAD: self.payload,
            ROOT_GOAL: self.goal,
            ROOT_TASK: self.task,
            ROOT_SKILL: self.skill,
            ROOT_ITEM: item,
            ROOT_SETTINGS: self.settings,
        }
        return walk(documents.get(path.root), path.segments)


# A package test sees the facts each expression of a rule is evaluated on
# (``condition``, or ``where`` with its item): its coverage counts the branches
# they reach (CP-ADR-0074 Z3). Unset, as everywhere but in a test.
FactsObserver = Callable[[str, Facts, Any], None]
observe_facts: ContextVar[FactsObserver | None] = ContextVar("rule_facts_observer", default=None)


def _observe(where: str, facts: Facts, item: Any = None) -> None:
    observer = observe_facts.get()
    if observer is not None:
        observer(where, facts, item)


def _event_facts(rule: WorkRule, event: JournalEvent) -> Facts:
    payload = dict(event.payload or {})
    trigger = {
        "kind": rule.trigger["kind"],
        "type": rule.trigger["type"],
        "ref": f"event:{event.id}",
        "eventId": str(event.id),
        "eventType": event.event_type,
        "entityType": event.entity_type,
        "entityId": str(event.entity_id),
        "occurredAt": event.occurred_at.isoformat(),
        # Who the journal says wrote the fact (Zh6): the author cannot spoof it.
        "actorId": str(event.actor_id) if event.actor_id is not None else None,
    }
    if event.event_type == "observation.recorded":
        trigger["observationId"] = str(event.entity_id)
        evidence: list[dict[str, Any]] = [
            {"kind": "observation", "observationId": str(event.entity_id)}
        ]
    else:
        # A core event is not an observation: it is cited by its journal id.
        evidence = [
            {"kind": "external", "externalRef": {"system": CORE_SYSTEM, "id": f"event:{event.id}"}}
        ]
    task_id: uuid.UUID | None = None
    if event.entity_type == "task":
        task_id = event.entity_id
    elif isinstance(payload.get("taskId"), str):
        try:
            task_id = uuid.UUID(payload["taskId"])
        except ValueError:
            task_id = None
    return Facts(trigger=trigger, payload=payload, evidence=evidence, task_id=task_id)


def _schedule_facts(rule: WorkRule, trigger_ref: str, scheduled_at: datetime) -> Facts:
    trigger = {
        "kind": TriggerKind.SCHEDULE.value,
        "type": rule.trigger["type"],
        "ref": trigger_ref,
        "scheduledAt": scheduled_at.isoformat(),
    }
    return Facts(trigger=trigger, payload={}, evidence=[])


def _task_view(
    task: Task,
    verification: dict[str, Any] | None = None,
    task_type: TaskType | None = None,
) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "publicId": task.public_id,
        # The type the task carries, as in TaskOut (amendment 2026-09-27, G4).
        "typeKey": task_type.key if task_type is not None else None,
        "typeVersion": task_type.version if task_type is not None else None,
        "title": task.title,
        "status": task.status,
        "systemStatusCategory": task.system_status_category,
        "priority": task.priority,
        "workspaceId": str(task.workspace_id) if task.workspace_id else None,
        "assigneeId": str(task.assignee_id) if task.assignee_id else None,
        "goalId": str(task.goal_id) if task.goal_id else None,
        "customFields": dict(task.custom_fields or {}),
        # The newest verification attempt, so a condition can tell work being
        # checked from abandoned work (amendment A5).
        "verification": verification,
    }


async def _load_views(
    session: AsyncSession, ctx: AuthContext, rule: WorkRule, facts: Facts
) -> None:
    """Load the goal and task views the rule reads, as the rule's authority.

    Only what the documents name is read, and read with the same rights a
    person would need: the rule's goal under ``goals.read``, the trigger's
    task under ``tasks.read``.
    """
    roots = rule_roots(
        condition=rule.condition, interpretation=rule.interpretation, action=rule.action
    )
    if ROOT_GOAL in roots and rule.goal_id is not None and ROOT_GOAL not in facts.views:
        goal = await get_readable_goal(session, ctx, rule.goal_id)
        facts.goal = {
            "id": str(goal.id),
            "title": goal.title,
            "status": goal.status,
            "workspaceId": str(goal.workspace_id) if goal.workspace_id else None,
        }
    if ROOT_TASK in roots and facts.task_id is not None and ROOT_TASK not in facts.views:
        task = await resolve_task(session, ctx, str(facts.task_id))
        await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(task.id)))
        attempt = (await latest_attempts(session, task.tenant_id, [task.id])).get(task.id)
        facts.task = _task_view(
            task,
            {"status": attempt.status, "attempt": attempt.attempt} if attempt else None,
            await session.get(TaskType, task.type_id),
        )
    if ROOT_SETTINGS in roots and ROOT_SETTINGS not in facts.views:
        await _load_settings(session, rule, facts)
    facts.views.update({ROOT_GOAL, ROOT_TASK, ROOT_SETTINGS})


async def _load_settings(session: AsyncSession, rule: WorkRule, facts: Facts) -> None:
    """The settings of the rule's package (``package_objects`` of kind ``WorkRule``).

    An evaluation resumed after its skill answered reads the version its
    evidence names, from the history: the values it began with.
    """
    seen = facts.settings_seen
    if seen is not None:
        found = await history(
            session,
            rule.tenant_id,
            seen.get("package"),
            [(int(seen["version"]), int(seen["schemaRevision"]))],
        )
        facts.settings = found.values(int(seen["version"]), int(seen["schemaRevision"]))
        return
    scope = await object_scope(session, rule.tenant_id, RULE_KIND, rule.key)
    current = await snapshot(session, rule.tenant_id, scope)
    if current is not None:
        facts.settings = current.values
        facts.settings_seen = current.evidence()


def _settings_evidence(row: RuleEvaluation, facts: Facts) -> None:
    """The version of the settings the evaluation read, an element of its evidence."""
    if facts.settings_seen is None:
        return
    if any(item.get("kind") == SETTINGS_EVIDENCE for item in row.evidence or []):
        return
    row.evidence = [*(row.evidence or []), facts.settings_seen]


def _task_evidence(items: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The evidence of an evaluation a task cites: the facts, not the settings read."""
    return [item for item in items or [] if item.get("kind") != SETTINGS_EVIDENCE]


# --- recording ----------------------------------------------------------------


def _pointer(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("kind") == SETTINGS_EVIDENCE:
        return {k: item.get(k) for k in ("kind", "package", "version", "schemaRevision")}
    pointer: dict[str, Any] = origin_summary({"kind": "rule", "evidence": [item]})["evidence"][0]
    return pointer


async def _finish(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    status: EvaluationStatus,
    *,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    now = utcnow()
    row.status = status
    if result is not None:
        row.result = {**(row.result or {}), **result}
    row.error = error
    row.next_check_at = None
    row.updated_at = now
    work = [
        {
            k: w[k]
            for k in (
                "dedupKey",
                "target",
                "taskId",
                "created",
                "skipped",
                "failed",
                "reason",
                "refused",
            )
            if k in w
        }
        for w in (row.result or {}).get("work", [])
    ]
    await record_event(
        session,
        tenant_id=rule.tenant_id,
        event_type="rule.evaluated",
        entity_type="rule",
        entity_id=rule.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "ruleId": str(rule.id),
            "ruleKey": rule.key,
            "ruleVersion": row.rule_version,
            "evaluationId": str(row.id),
            "triggerRef": row.trigger_ref,
            "trigger": {"kind": rule.trigger.get("kind"), "type": rule.trigger.get("type")},
            "result": status.value,
            "conditionMatched": (row.result or {}).get("conditionMatched"),
            "evidence": [_pointer(item) for item in row.evidence or []],
            "skillInvocationId": str(row.skill_invocation_id) if row.skill_invocation_id else None,
            "work": work,
            "error": {"code": error.get("code")} if error else None,
        },
    )


def _domain_failure(exc: DomainError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, "details": exc.details}


def _condition_failure(exc: ConditionError, where: str) -> dict[str, Any]:
    return {"code": "rule_condition_error", "message": f"{where}: {exc}", "details": {}}


# --- actions --------------------------------------------------------------------


def _text_field(fields: dict[str, Any], name: str, limit: int) -> str | None:
    if name not in fields:
        return None
    value = fields[name]
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value))[:limit]


def _uuid_field(fields: dict[str, Any], name: str) -> uuid.UUID | None:
    value = fields.get(name)
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValidationError(
            "invalid_rule_field",
            f"action.fields.{name} rendered to {str(value)[:100]!r}, which is not an id",
            details={"field": f"action.fields.{name}"},
        ) from None


def _custom_fields(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Rendered ``fields.customFields``; a value that resolved to nothing is left out.

    Left out, not blanked, as in an approval outcome's ``ensureWork``: a field
    the schema requires is then reported missing (``custom_fields_invalid``)
    instead of being filed empty.
    """
    rendered = fields.get(CUSTOM_FIELDS) or {}
    kept = {name: value for name, value in rendered.items() if value is not None and value != ""}
    return kept or None


def _assignment(fields: dict[str, Any]) -> tuple[uuid.UUID | str | None, RequirementSpec | None]:
    """``fields.assignee`` as ``create_task`` takes it: an id, an agent, or a role.

    ``role:<slug>`` leaves the work unassigned and requires the role, which
    is looked up in the workspace of the work and its ancestors: any holder
    may take it, as with a role in a process's assignment chain.
    """
    value = fields.get("assignee")
    if isinstance(value, str) and value.startswith(ROLE_ASSIGNEE_PREFIX):
        slug = value.removeprefix(ROLE_ASSIGNEE_PREFIX).strip()
        if not slug:
            raise ValidationError(
                "invalid_rule_field",
                "action.fields.assignee rendered to a role without a slug",
                details={"field": "action.fields.assignee"},
            )
        return None, RequirementSpec(roles=[slug])
    # An id, or an agent of the registry by key (CP-ADR-0073, A1).
    if is_agent_reference(value):
        return str(value), None
    return _uuid_field(fields, "assignee"), None


async def _approver_role(
    session: AsyncSession, ctx: AuthContext, fields: dict[str, Any], task: Task
) -> uuid.UUID | None:
    """``fields.approverRole``: a role id, or ``role:<slug>`` seen from the work's workspace."""
    value = fields.get("approverRole")
    if is_role_reference(value):
        return await role_for_task(
            session,
            ctx,
            str(value),
            workspace_id=task.workspace_id,
            field="action.fields.approverRole",
        )
    return _uuid_field(fields, "approverRole")


async def _lock_keys(session: AsyncSession, ctx: AuthContext, dedup_keys: set[str]) -> None:
    """Serialize work on these keys per (tenant, key) for the transaction.

    Two evaluations cannot both find nothing and both file. An evaluation
    takes its keys in one order, so two evaluations over the same keys
    cannot deadlock on them. A batch holds the keys of all its evaluations
    until commit; a cycle across them is broken by the database and the
    losing side is retried (CP-ADR-0063 §5).
    """
    for dedup_key in sorted(dedup_keys):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"rule-work:{ctx.tenant_id}:{dedup_key}"},
        )


async def _open_work(
    session: AsyncSession, ctx: AuthContext, dedup_key: str, *, for_update: bool
) -> Task | None:
    """The open work item rules keep for the key, if any (the key is locked).

    The key is tenant-wide, like an ``ensureWork`` key of an approval outcome:
    a reconciling rule finds what another rule filed. Work the rule's
    authority may not read is not handed to it: the same ``404`` as for a
    missing task. A task the rule is about to change is locked, so a
    concurrent edit waits for the rule instead of turning its expected
    version stale.
    """
    task_id = await session.scalar(
        select(RuleWorkItem.task_id)
        .where(RuleWorkItem.tenant_id == ctx.tenant_id, RuleWorkItem.dedup_key == dedup_key)
        .order_by(RuleWorkItem.created_at.desc(), RuleWorkItem.id.desc())
        .limit(1)
    )
    if task_id is None:
        return None
    task = await session.get(Task, task_id, populate_existing=True, with_for_update=for_update)
    if task is None or task.system_status_category in TERMINAL_CATEGORIES:
        return None
    # Work outside the visibility of the rule's person is not theirs to find
    # by a key either (CP-ADR-0082 §3.7).
    if not await permits_task(ctx, Permission.TASKS_READ, task=task):
        raise NotFoundError("Task not found", details={"dedupKey": dedup_key})
    return task


async def _work_event(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    task: Task,
    payload: dict[str, Any],
    *,
    event_type: str,
) -> None:
    await record_event(
        session,
        tenant_id=rule.tenant_id,
        event_type=event_type,
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "ruleId": str(rule.id),
            "ruleKey": rule.key,
            "ruleVersion": row.rule_version,
            "evaluationId": str(row.id),
            "taskId": str(task.id),
            "publicId": task.public_id,
            "evidence": [_pointer(item) for item in row.evidence or []],
            **payload,
        },
    )


@dataclass
class _Planned:
    """The action for one item, its templates filled."""

    dedup_key: str
    fields: dict[str, Any]
    # A creating action's acceptance for the work it files.
    acceptance: Any = None
    # The check ``complete_work`` ties its evidence to.
    check: str | None = None
    # The task type a creating action files the work as.
    task_type: str | None = None
    # ``fields.relations`` rendered: a task ref, dedup keys (G3).
    spawned_by: str | None = None
    depends_on: list[str] = field(default_factory=list)
    # Where each ``dependsOn`` key resolved: this evaluation's item, or a task.
    dependency_items: list[int] = field(default_factory=list)
    dependency_tasks: list[uuid.UUID] = field(default_factory=list)
    spawned_by_task: uuid.UUID | None = None
    # ``(code, detail)`` of a refusal of this item alone (G5).
    refused: tuple[str, str] | None = None


def _dependency_keys(value: Any) -> list[str] | None:
    """Rendered ``dependsOn``: a flat list of keys; ``None`` if it is not one.

    Each template may render to a key or to a list of keys (an exact
    placeholder keeps a list); nothing (null, "", []) means no dependency.
    """
    keys: list[str] = []
    for entry in value if isinstance(value, list) else [value]:
        for key in entry if isinstance(entry, list) else [entry]:
            if key is None or key == "":
                continue
            if not isinstance(key, str):
                return None
            normalized = key.strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys if len(keys) <= MAX_DEPENDENCIES else None


def _render_check(
    action: dict[str, Any], resolve: Callable[[VarPath], Any], roots: frozenset[str]
) -> str | None:
    """The check ``complete_work`` ties its evidence to; ``None`` for other actions."""
    if action["kind"] != ActionKind.COMPLETE_WORK:
        return None
    rendered = render(action["check"], resolve, roots=roots) if "check" in action else None
    check = RULE_EVIDENCE_CHECK if rendered is None else str(rendered)
    if not CHECK_KEY_RE.match(check):
        raise ValidationError(
            "invalid_rule_field",
            f"action.check rendered to {check[:100]!r}, which is not a check key",
            details={"field": "action.check"},
        )
    return check


def _render_item(rule: WorkRule, facts: Facts, item: Any) -> _Planned:
    """The dedup key, the fields and the rest of the action for one item."""
    action = rule.action
    roots = action_roots(
        interpreted=rule.interpretation is not None, for_each=action.get("forEach") is not None
    )

    def resolve(path: VarPath) -> Any:
        return facts.resolve(path, item)

    dedup_key = normalize_dedup_key(render(action["dedupKeyTemplate"], resolve, roots=roots))
    acceptance = (
        render(action["acceptance"], resolve, roots=roots) if "acceptance" in action else None
    )
    check = _render_check(action, resolve, roots)
    fields = render(action.get("fields") or {}, resolve, roots=roots)
    planned = _Planned(dedup_key=dedup_key, fields=fields, acceptance=acceptance, check=check)
    if action["kind"] in CREATING_ACTIONS:
        task_type = render(action["taskType"], resolve, roots=roots)
        planned.task_type = task_type if isinstance(task_type, str) else None
        allowed = action.get("taskTypes")
        if allowed is not None and planned.task_type not in allowed:
            planned.refused = (
                TASK_TYPE_NOT_ALLOWED,
                f"action.taskType rendered to {str(task_type)[:100]!r}, not one of {allowed}",
            )
    relations = fields.pop(RELATIONS, None)
    if relations is not None and planned.refused is None:
        spawned_by = relations.get(RELATION_SPAWNED_BY)
        if spawned_by is not None and spawned_by != "":
            if isinstance(spawned_by, str | int | float) and not isinstance(spawned_by, bool):
                planned.spawned_by = str(spawned_by)
            else:
                planned.refused = (INVALID_RELATIONS, "relations.spawnedBy is not a task id")
        keys = _dependency_keys(relations.get(RELATION_DEPENDS_ON))
        if keys is None:
            planned.refused = planned.refused or (
                INVALID_RELATIONS,
                f"relations.dependsOn must render to at most {MAX_DEPENDENCIES} dedup keys",
            )
        else:
            planned.depends_on = keys
    return planned


async def _resolve_relations(
    session: AsyncSession, ctx: AuthContext, planned: list[_Planned]
) -> None:
    """Resolve ``spawnedBy`` and ``dependsOn`` of every item, refusing what cannot be (G3, G5).

    A key names an item of this evaluation first, then the newest work any
    rule of the tenant filed under it (closed work too: a dependency that is
    done just does not hold the work). An item depending on a refused item
    is refused; items that depend on each other in a circle are all refused.
    """
    by_key: dict[str, int] = {}
    for index, item in enumerate(planned):
        by_key.setdefault(item.dedup_key, index)
    for item in planned:
        if item.refused is not None:
            continue
        if item.spawned_by is not None:
            try:
                target = await resolve_task(session, ctx, item.spawned_by)
                await authorize(
                    ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(target.id))
                )
            except (NotFoundError, AuthorizationError):
                item.refused = (
                    RELATION_TARGET_NOT_FOUND,
                    f"relations.spawnedBy {item.spawned_by[:100]!r} names no visible task",
                )
                continue
            item.spawned_by_task = target.id
        for key in item.depends_on:
            if key in by_key:
                item.dependency_items.append(by_key[key])
                continue
            task_id = await session.scalar(
                select(RuleWorkItem.task_id)
                .where(RuleWorkItem.tenant_id == ctx.tenant_id, RuleWorkItem.dedup_key == key)
                .order_by(RuleWorkItem.created_at.desc(), RuleWorkItem.id.desc())
                .limit(1)
            )
            dependency = await session.get(Task, task_id) if task_id is not None else None
            visible = dependency is not None and await permits_task(
                ctx, Permission.TASKS_READ, task=dependency
            )
            if not visible:
                item.refused = (DEPENDENCY_NOT_FOUND, f"no work is known by the key {key[:100]!r}")
                break
            assert task_id is not None
            item.dependency_tasks.append(task_id)

    # Items on a cycle among this evaluation's items (a self-reference too).
    for index in _on_cycles([item.dependency_items for item in planned]):
        if planned[index].refused is None:
            planned[index].refused = (
                DEPENDENCY_CYCLE,
                "the items of this evaluation depend on each other in a circle",
            )
    changed = True
    while changed:
        changed = False
        for item in planned:
            if item.refused is not None:
                continue
            refused = [i for i in item.dependency_items if planned[i].refused is not None]
            if refused:
                item.refused = (
                    DEPENDENCY_REFUSED,
                    f"depends on {planned[refused[0]].dedup_key[:100]!r}, which was refused",
                )
                changed = True


def _on_cycles(edges: list[list[int]]) -> set[int]:
    """Nodes of a directed graph (adjacency lists) that lie on a cycle."""
    on_cycle: set[int] = set()
    for start in range(len(edges)):
        # Reachable from start's successors; start is on a cycle iff it is reachable.
        seen: set[int] = set()
        stack = list(edges[start])
        while stack:
            node = stack.pop()
            if node == start:
                on_cycle.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges[node])
    return on_cycle


async def _link_filed(
    session: AsyncSession, ctx: AuthContext, planned: list[_Planned], work: list[dict[str, Any]]
) -> None:
    """Relations of the work this evaluation filed, once all of it exists (G3).

    Work found by its key gets none: ``ensure``, not upsert, so evaluating
    the same document again adds neither work nor relations.
    """
    for item, outcome in zip(planned, work, strict=True):
        if not outcome.get("created"):
            continue
        task_ref = outcome["taskId"]
        targets: list[tuple[str, str]] = []
        if item.spawned_by_task is not None:
            targets.append((str(item.spawned_by_task), TaskRelationType.SPAWNED_BY.value))
        depends = [work[i]["taskId"] for i in item.dependency_items]
        depends += [str(task_id) for task_id in item.dependency_tasks]
        for target in dict.fromkeys(depends):
            if target != task_ref:
                targets.append((target, TaskRelationType.DEPENDS_ON.value))
        for target, relation_type in targets:
            await add_relation(
                session,
                ctx,
                from_task_ref=task_ref,
                to_task_ref=target,
                relation_type=relation_type,
            )


async def _apply(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    planned: _Planned,
) -> dict[str, Any]:
    """Run the rule's action for one item; returns what happened to the work."""
    action = rule.action
    kind = action["kind"]
    dedup_key, fields = planned.dedup_key, planned.fields
    outcome: dict[str, Any] = {"dedupKey": dedup_key, "action": kind}
    current = await _open_work(session, ctx, dedup_key, for_update=kind not in CREATING_ACTIONS)

    if kind in CREATING_ACTIONS:
        if current is not None:
            return {
                **outcome,
                "taskId": str(current.id),
                "publicId": current.public_id,
                "created": False,
            }
        title = _text_field(fields, "title", MAX_TITLE_LENGTH) or ""
        assignee, requirements = _assignment(fields)
        try:
            task = await create_task(
                session,
                ctx,
                title=title,
                description=_text_field(fields, "description", MAX_TEXT_FIELD_LENGTH) or "",
                priority=_text_field(fields, "priority", 32) or TaskPriority.MEDIUM,
                type_key=planned.task_type,
                assignee_id=assignee,
                assignee_field="action.fields.assignee",
                # The work's own workspace (a template), else the rule's; the
                # rule's identity is authorized to file work there, as anyone's.
                workspace_id=_uuid_field(fields, WORKSPACE_FIELD) or rule.workspace_id,
                # Checked against the fieldSchema of the type here: a misfit fails
                # the evaluation with custom_fields_invalid, and no work is filed.
                custom_fields=_custom_fields(fields),
                requirements=requirements,
                goal_id=rule.goal_id,
                origin={
                    "kind": "rule",
                    "ruleId": str(rule.id),
                    "ref": f"rule_evaluation:{row.id}",
                    "evidence": _task_evidence(row.evidence),
                },
                acceptance=planned.acceptance,
            )
        except ValidationError as exc:
            if requirements is None or exc.code != "unknown_requirement":
                raise
            raise ValidationError(
                "unknown_role",
                f"action.fields.assignee: no role {requirements.roles[0]!r}"
                " in the workspace of the work or above it",
                details={"field": "action.fields.assignee", "role": requirements.roles[0][:100]},
            ) from exc
        session.add(
            RuleWorkItem(
                id=new_uuid(),
                tenant_id=rule.tenant_id,
                rule_id=rule.id,
                dedup_key=dedup_key,
                task_id=task.id,
                evaluation_id=row.id,
                created_at=utcnow(),
            )
        )
        await session.flush()
        extra: dict[str, Any] = {}
        if kind == ActionKind.REQUEST_DECISION:
            # A gate, like every other decision core files on a task
            # (ensureWork.requestApproval, the human check): the work waits
            # for it, and the decision runs the outcomes the task's type
            # declares (CP-ADR-0061, CP-ADR-0063 amendment 2026-09-25).
            approval = await request_approval(
                session,
                ctx,
                task_ref=str(task.id),
                assigned_principal_id=_uuid_field(fields, "approver"),
                required_role_id=await _approver_role(session, ctx, fields, task),
                comment=f"Rule {rule.key}: {title}"[:MAX_TEXT_FIELD_LENGTH],
                gate=True,
            )
            extra["approvalId"] = str(approval.id)
        await _work_event(
            session,
            ctx,
            rule,
            row,
            task,
            {"action": kind, "dedupKey": dedup_key, "created": True, **extra},
            event_type="work.derived",
        )
        return {
            **outcome,
            "taskId": str(task.id),
            "publicId": task.public_id,
            "created": True,
            **extra,
        }

    if current is None:
        return {**outcome, "skipped": True, "reason": "no_open_work"}
    if kind == ActionKind.UPDATE_WORK:
        return {**outcome, **await _update(session, ctx, rule, row, current, dedup_key, fields)}
    claim = await live_claim_of(session, current)
    if claim is not None:
        # Somebody is working on it: ask them to stop, decide once they have.
        waiting = await _await_release(session, ctx, rule, current, claim.id, dedup_key)
        return {**outcome, **waiting, **({"check": planned.check} if planned.check else {})}
    return {
        **outcome,
        **await _close(session, ctx, rule, row, current, dedup_key, planned.check),
    }


async def _update(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    current: Task,
    dedup_key: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """``update_work``: new fields and the evaluation's facts on the open work."""
    changes: dict[str, Any] = {}
    for name, limit in (("title", MAX_TITLE_LENGTH), ("description", MAX_TEXT_FIELD_LENGTH)):
        value = _text_field(fields, name, limit)
        if value is not None:
            changes[name] = value
    if "priority" in fields:
        changes["priority"] = _text_field(fields, "priority", 32)
    known = {
        (e["kind"], e.get("observationId"), e.get("artifactId"), str(e.get("externalRef")))
        for e in current.evidence or []
        if "check" not in e
    }
    added = [
        e
        for e in _task_evidence(row.evidence)
        if (e["kind"], e.get("observationId"), e.get("artifactId"), str(e.get("externalRef")))
        not in known
    ]
    if added:
        changes["evidence"] = [*(current.evidence or []), *added]
    if not changes:
        return {
            "taskId": str(current.id),
            "publicId": current.public_id,
            "skipped": True,
            "reason": "nothing_to_change",
        }
    try:
        async with session.begin_nested():
            updated = await update_task(
                session, ctx, task_ref=str(current.id), expected_version=current.version, **changes
            )
    except ConflictError as exc:
        # The fields of work in progress are its executor's: a rule does not
        # write them through a live claim (amendment A4 keeps this).
        if exc.code != TASK_CLAIMED:
            raise
        return {
            "taskId": str(current.id),
            "publicId": current.public_id,
            "skipped": True,
            "reason": TASK_CLAIMED,
        }
    await _work_event(
        session,
        ctx,
        rule,
        row,
        updated,
        {"action": ActionKind.UPDATE_WORK.value, "dedupKey": dedup_key, "changes": sorted(changes)},
        event_type="work.reconciled",
    )
    return {"taskId": str(updated.id), "publicId": updated.public_id, "changes": sorted(changes)}


def _evidence_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["kind"],
        item.get("observationId"),
        item.get("artifactId"),
        item.get("contextPackId"),
        str(item.get("externalRef")),
        item.get("check"),
    )


def _with_facts(
    existing: list[dict[str, Any]] | None, facts: list[dict[str, Any]], check: str | None
) -> list[dict[str, Any]] | None:
    """The task's evidence plus the evaluation's facts it lacks; ``None`` if none.

    A closing decision is recorded on the work itself (amendment A3): a task
    answers "closed on which fact" without the journal. ``complete_work``
    ties its facts to ``check``.
    """
    known = {_evidence_identity(item) for item in existing or []}
    added: list[dict[str, Any]] = []
    for fact in facts:
        item = {**fact, "check": check} if check else dict(fact)
        if _evidence_identity(item) not in known:
            known.add(_evidence_identity(item))
            added.append(item)
    return [*(existing or []), *added] if added else None


async def _close(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    current: Task,
    dedup_key: str,
    check: str | None,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """``cancel_work`` / ``complete_work`` on open work nobody holds.

    Cancelling moves the task to its first reachable cancelled status.
    Completing goes through the verification stage (CP-ADR-0067): the facts
    are written as evidence tied to ``check`` and the task is completed like
    any other (``trigger = rule``); a task without acceptance is verified by
    the implicit ``external_state`` check those facts pass. Work already
    handed in keeps its open attempt, which reads the new evidence.
    """
    kind = rule.action["kind"]
    changes: dict[str, Any] = {}
    evidence = _with_facts(current.evidence, _task_evidence(row.evidence), check)
    if evidence is not None:
        changes["evidence"] = evidence
    extra: dict[str, Any] = {}
    if kind == ActionKind.CANCEL_WORK:
        lifecycle = await lifecycle_of(session, current)
        targets = [
            status
            for status in lifecycle.targets_from(current.status)
            if lifecycle.category_of(status) == WorkItemStatusCategory.TERMINAL_CANCELLED
        ]
        if not targets:
            raise ValidationError(
                "rule_cannot_cancel",
                f"The lifecycle of {current.public_id} has no cancelled status reachable "
                f"from {current.status!r}",
                details={"taskId": str(current.id), "status": current.status},
            )
        changes["status"] = targets[0]
        updated = await update_task(
            session, ctx, task_ref=str(current.id), expected_version=current.version, **changes
        )
    else:
        updated = current
        if evidence is not None:
            updated = await update_task(
                session,
                ctx,
                task_ref=str(current.id),
                expected_version=current.version,
                evidence=evidence,
            )
        attempt = await open_attempt(session, updated.id)
        if attempt is None:
            updated = await complete_task(
                session,
                ctx,
                task_ref=str(updated.id),
                expected_version=updated.version,
                trigger="rule",
                trigger_ref=f"rule_evaluation:{row.id}",
                implicit_checks=[] if updated.acceptance else [dict(IMPLICIT_CHECK)],
            )
            attempt = await open_attempt(session, updated.id)
            changes["completion"] = True
        assert attempt is not None
        extra = {"verificationId": str(attempt.id), "check": check}
    await _work_event(
        session,
        ctx,
        rule,
        row,
        updated,
        {
            "action": kind,
            "dedupKey": dedup_key,
            "changes": sorted(changes),
            **({"target": target} if target else {}),
            **extra,
        },
        event_type="work.reconciled",
    )
    return {
        "taskId": str(updated.id),
        "publicId": updated.public_id,
        "changes": sorted(changes),
        **extra,
    }


async def _await_release(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    task: Task,
    claim_id: uuid.UUID,
    dedup_key: str,
) -> dict[str, Any]:
    """Ask the run working under a live claim to stop; the decision waits.

    The request goes through ``request_cancel_run`` with the rule's
    authority, in this very pass (SC-006). A claim without a run (a
    person's harness) has nobody to ask: the decision waits all the same.
    A request the rule may not make is recorded, not raised: the decision
    still waits for the executor to finish on its own.
    """
    waiting: dict[str, Any] = {
        "taskId": str(task.id),
        "publicId": task.public_id,
        "waiting": True,
        "reason": TASK_CLAIMED,
        "claimId": str(claim_id),
    }
    run: Run | None = await session.scalar(
        select(Run)
        .where(Run.task_id == task.id, Run.status == RunStatus.RUNNING)
        .order_by(Run.started_at.desc())
        .limit(1)
    )
    if run is None:
        return waiting
    waiting["runId"] = str(run.id)
    if run.cancel_requested_at is not None:
        waiting["cancelRequested"] = True
        return waiting
    try:
        async with session.begin_nested():
            await request_cancel_run(
                session,
                ctx,
                run_id=run.id,
                reason=f"rule {rule.key}: {rule.action['kind']} {dedup_key}"[:500],
            )
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        waiting["cancelRequested"] = False
        waiting["cancelError"] = exc.code
        return waiting
    waiting["cancelRequested"] = True
    return waiting


# --- the task an observation is bound to (amendment integrations-connections, Zh) ---


def _bound_key(task_id: uuid.UUID | str) -> str:
    return f"{BOUND_KEY_PREFIX}{task_id}"


async def _bound_task(
    session: AsyncSession, ctx: AuthContext, rule: WorkRule, task_id: uuid.UUID
) -> tuple[Task, dict[str, Any] | None]:
    """Checks 2-5 of Zh3 on the bound task, locked for the decision.

    Returns the task and, when the decision ends here, why it is skipped: a
    type outside ``taskTypes`` (the fact is not this rule's), work already
    done or closed (a repeated fact gives neither a second attempt nor a
    second copy of the evidence). A task that is missing, of another tenant
    or unreadable to the rule is ``bound_task_not_found`` — what cannot be
    seen is not told apart from what is not there; a task the rule's identity
    may not write (its workspace) is ``bound_task_forbidden``.
    """
    task: Task | None = await session.scalar(
        select(Task)
        .where(Task.id == task_id, Task.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is not None:
        try:
            await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(task.id)))
        except AuthorizationError:
            task = None
    if task is None:
        raise DomainError(
            BOUND_TASK_NOT_FOUND,
            "The task the observation is bound to is not found",
            details={"taskId": str(task_id)},
        )
    task_type = await session.get(TaskType, task.type_id) if task.type_id is not None else None
    type_key = task_type.key if task_type is not None else None
    if type_key not in rule.action["taskTypes"]:
        return task, {"skipped": True, "reason": BOUND_TASK_TYPE_NOT_LISTED, "typeKey": type_key}
    try:
        await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", str(task.id)))
    except AuthorizationError as exc:
        raise DomainError(
            BOUND_TASK_FORBIDDEN,
            f"The rule may not write {task.public_id}, the task the observation is bound to",
            details={
                "taskId": str(task.id),
                "workspaceId": str(task.workspace_id) if task.workspace_id else None,
                "permission": Permission.TASKS_WRITE.value,
            },
        ) from exc
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_SUCCESS:
        return task, {"skipped": True, "reason": ALREADY_DONE}
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_CANCELLED:
        return task, {"skipped": True, "reason": ALREADY_CLOSED}
    return task, None


async def _fact_on_record(session: AsyncSession, task: Task, check: str | None) -> bool:
    """Is the task being verified with a fact of ``check`` for this attempt?

    Then another fact of the same closing (a second observation of the same
    task) adds nothing to the open attempt: the decision is skipped
    (``verification_pending``). A task handed in by its executor without the
    fact still gets it, and its attempt is woken by it. A fact an earlier
    attempt has spent (the work was handed in anew after it failed) is not
    this attempt's: the new closing is written (Zh7).
    """
    if check is None:
        return False
    attempt = await open_attempt(session, task.id)
    if attempt is None:
        return False
    return any(
        item.get("check") == check for item in await current_evidence(session, task, attempt)
    )


def _bound_skip(item: dict[str, Any]) -> tuple[EvaluationStatus, dict[str, Any]]:
    details = {k: item[k] for k in ("taskId", "typeKey") if k in item}
    return EvaluationStatus.SKIPPED, {
        "skipped": {"reason": item["reason"], **details},
        "work": [item],
    }


async def _act_on_bound_task(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    facts: Facts,
) -> tuple[EvaluationStatus, dict[str, Any]]:
    """``cancel_work`` / ``complete_work`` with ``target: task`` (Zh3).

    The work is the task of the triggering observation (``payload.taskId``),
    whoever filed it. The row of the task serializes the decision: there is
    no dedup key to lock, and none is written to ``rule_work_items`` (Zh4).
    Past the checks it is closed as ``target: dedup`` closes work: through
    the verification stage, or after the claim on it has ended.
    """
    kind = rule.action["kind"]
    if not has_author_filter(rule.trigger):
        return EvaluationStatus.SKIPPED, {
            "skipped": {"reason": TRIGGER_AUTHOR_UNFILTERED},
            "work": [],
        }
    if facts.task_id is None:
        return EvaluationStatus.SKIPPED, {"skipped": {"reason": NO_BOUND_TASK}, "work": []}
    dedup_key = _bound_key(facts.task_id)
    outcome: dict[str, Any] = {
        "dedupKey": dedup_key,
        "target": ActionTarget.TASK.value,
        "action": kind,
        "taskId": str(facts.task_id),
    }
    task, skipped = await _bound_task(session, ctx, rule, facts.task_id)
    outcome["publicId"] = task.public_id
    check = _render_check(
        rule.action,
        facts.resolve,
        action_roots(interpreted=rule.interpretation is not None, for_each=False),
    )
    if skipped is None and await _fact_on_record(session, task, check):
        skipped = {"skipped": True, "reason": VERIFICATION_PENDING}
    if skipped is not None:
        return _bound_skip({**outcome, **skipped})
    claim = await live_claim_of(session, task)
    if claim is not None:
        waiting = await _await_release(session, ctx, rule, task, claim.id, dedup_key)
        return EvaluationStatus.WAITING, {
            "work": [{**outcome, **waiting, **({"check": check} if check else {})}],
            "waitingFor": WAITING_FOR_CLAIM,
            "waitingSince": utcnow().isoformat(),
        }
    closed = await _close(
        session, ctx, rule, row, task, dedup_key, check, target=ActionTarget.TASK.value
    )
    return EvaluationStatus.MATCHED, {"work": [{**outcome, **closed}]}


async def _act(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    facts: Facts,
) -> tuple[EvaluationStatus, dict[str, Any]]:
    """The action over every selected item (one item without ``forEach``)."""
    action = rule.action
    if action.get("target") == ActionTarget.TASK:
        return await _act_on_bound_task(session, ctx, rule, row, facts)
    items: list[Any]
    if action.get("forEach") is not None:
        outer = action_roots(interpreted=rule.interpretation is not None, for_each=False)
        path = parse_path(action["forEach"], roots=outer, where="action.forEach", code="x")
        value = facts.resolve(path)
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValidationError(
                "rule_for_each_not_list",
                f"action.forEach {action['forEach']!r} is not a list",
                details={"forEach": action["forEach"]},
            )
        if len(value) > MAX_FOR_EACH_ITEMS:
            raise ValidationError(
                "rule_for_each_too_many",
                f"action.forEach yields {len(value)} items, more than {MAX_FOR_EACH_ITEMS}",
                details={"forEach": action["forEach"], "maxItems": MAX_FOR_EACH_ITEMS},
            )
        roots = action_roots(interpreted=rule.interpretation is not None, for_each=True)
        where = action.get("where")

        def selected(entry: Any) -> bool:
            if where is None:
                return True
            _observe("where", facts, entry)
            return bool(evaluate(where, lambda path: facts.resolve(path, entry), roots=roots))

        try:
            items = [entry for entry in value if selected(entry)]
        except ConditionError as exc:
            raise _WhereError(exc) from exc
        selection = {"items": len(value), "selected": len(items)}
    else:
        items = [None]
        selection = {}
    if not items:
        return EvaluationStatus.NOT_MATCHED, {**selection, "work": []}
    planned = [_render_item(rule, facts, entry) for entry in items]
    await _lock_keys(session, ctx, {item.dedup_key for item in planned})
    if action["kind"] == ActionKind.ENSURE_WORK:
        await _resolve_relations(session, ctx, planned)
    work = [
        await _apply(session, ctx, rule, row, item)
        if item.refused is None
        else {"dedupKey": item.dedup_key, "refused": item.refused[0], "detail": item.refused[1]}
        for item in planned
    ]
    await _link_filed(session, ctx, planned, work)
    row.created_task_ids = [w["taskId"] for w in work if w.get("created")]
    if all(w.get("refused") for w in work):
        return EvaluationStatus.FAILED, {**selection, "work": work}
    if any(w.get("waiting") for w in work):
        # A closing decision on work under a live claim (amendment A4).
        return EvaluationStatus.WAITING, {
            **selection,
            "work": work,
            "waitingFor": WAITING_FOR_CLAIM,
            "waitingSince": utcnow().isoformat(),
        }
    return EvaluationStatus.MATCHED, {**selection, "work": work}


class _WhereError(Exception):
    def __init__(self, cause: ConditionError) -> None:
        super().__init__(str(cause))
        self.cause = cause


async def _run_action(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    facts: Facts,
    *,
    check_seconds: float = DEFAULT_SKILL_CHECK_SECONDS,
) -> None:
    """Run the action in a savepoint and finish the evaluation — or wait for a claim."""
    try:
        async with session.begin_nested():
            status, result = await _act(session, ctx, rule, row, facts)
    except _WhereError as exc:
        row.created_task_ids = []
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error=_condition_failure(exc.cause, "action.where"),
        )
        return
    except ConditionError as exc:
        row.created_task_ids = []
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error=_condition_failure(exc, "action"),
        )
        return
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        # Everything the action wrote is rolled back; the facts stay recorded.
        row.created_task_ids = []
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return
    if status == EvaluationStatus.WAITING:
        _keep_waiting(row, result, check_seconds)
        return
    if status == EvaluationStatus.FAILED:
        # Every item was refused on its own (G5); nothing was written.
        refused = [w["refused"] for w in result["work"]]
        await _finish(
            session,
            ctx,
            rule,
            row,
            status,
            result=result,
            error={
                "code": WORK_ITEMS_REFUSED,
                "message": f"all {len(refused)} items were refused",
                "details": {"refused": sorted(set(refused))},
            },
        )
        return
    await _finish(session, ctx, rule, row, status, result=result)


def _keep_waiting(row: RuleEvaluation, result: dict[str, Any], check_seconds: float) -> None:
    now = utcnow()
    row.result = {**(row.result or {}), **result}
    row.status = EvaluationStatus.WAITING
    row.next_check_at = now + timedelta(seconds=check_seconds)
    row.updated_at = now


# --- evaluation -------------------------------------------------------------------


async def _insert_evaluation(
    session: AsyncSession,
    rule: WorkRule,
    facts: Facts,
    *,
    trigger_ref: str,
    trigger_event_id: uuid.UUID | None,
) -> RuleEvaluation | None:
    """Claim the trigger for the rule; ``None`` if it was already evaluated."""
    now = utcnow()
    evaluation_id = new_uuid()
    inserted = await session.scalar(
        insert(RuleEvaluation)
        .values(
            id=evaluation_id,
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            rule_version=rule.version,
            trigger_ref=trigger_ref,
            trigger_event_id=trigger_event_id,
            status=EvaluationStatus.FAILED,
            result={"trigger": {k: facts.trigger[k] for k in ("kind", "type", "ref")}},
            evidence=list(facts.evidence),
            created_task_ids=[],
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_rule_evaluations_trigger")
        .returning(RuleEvaluation.id)
    )
    if inserted is None:
        return None
    row = await session.get(RuleEvaluation, evaluation_id)
    assert row is not None
    return row


async def evaluate_trigger(
    session: AsyncSession,
    rule: WorkRule,
    facts: Facts,
    *,
    trigger_ref: str,
    trigger_event_id: uuid.UUID | None,
    trace_run_id: str,
    skill_check_seconds: float = DEFAULT_SKILL_CHECK_SECONDS,
) -> RuleEvaluation | None:
    """Evaluate one rule on one trigger; ``None`` if it was already evaluated."""
    row = await _insert_evaluation(
        session, rule, facts, trigger_ref=trigger_ref, trigger_event_id=trigger_event_id
    )
    if row is None:
        return None
    acting = await _acting(
        session,
        rule,
        trace_run_id=trace_run_id,
        causation_id=str(trigger_event_id) if trigger_event_id else None,
    )
    ctx = acting.ctx
    try:
        await acting.require_standing(session)
        await authorize(ctx, Permission.EVENTS_READ, resource=rule_scope(rule.workspace_id))
        await _load_views(session, ctx, rule, facts)
        _settings_evidence(row, facts)
        _observe("condition", facts)
        matched = evaluate(rule.condition, facts.resolve, roots=BASE_ROOTS)
    except ConditionError as exc:
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error=_condition_failure(exc, "condition"),
        )
        return row
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return row
    row.result = {**row.result, "conditionMatched": matched}
    if not matched:
        await _finish(session, ctx, rule, row, EvaluationStatus.NOT_MATCHED)
        return row

    if rule.interpretation is not None:
        interpretation = rule.interpretation
        try:
            async with session.begin_nested():
                inputs = render(interpretation.get("inputs") or {}, facts.resolve, roots=BASE_ROOTS)
                queued = await invoke_skill(
                    session,
                    ctx,
                    skill_ref=interpretation["skill"],
                    inputs=inputs,
                    idempotency_key=f"{CORRELATION_PREFIX}{row.id}",
                )
        except DependencyUnavailableError:
            raise
        except DomainError as exc:
            await _finish(
                session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc)
            )
            return row
        row.skill_invocation_id = queued.invocation.id
        row.status = EvaluationStatus.WAITING
        row.next_check_at = utcnow() + timedelta(seconds=skill_check_seconds)
        row.updated_at = utcnow()
        return row

    await _run_action(session, ctx, rule, row, facts, check_seconds=skill_check_seconds)
    return row


async def record_internal_failure(
    session: AsyncSession,
    rule: WorkRule,
    facts: Facts,
    *,
    trigger_ref: str,
    trigger_event_id: uuid.UUID | None,
    trace_run_id: str,
    error: str,
) -> RuleEvaluation | None:
    """Close a trigger whose evaluation keeps breaking as ``failed``.

    Whatever the broken attempt wrote was rolled back; what stays is the
    evaluation, its facts and the reason, so the rule's history shows it.
    """
    row = await _insert_evaluation(
        session, rule, facts, trigger_ref=trigger_ref, trigger_event_id=trigger_event_id
    )
    if row is None:
        return None
    acting = await _acting(
        session,
        rule,
        trace_run_id=trace_run_id,
        causation_id=str(trigger_event_id) if trigger_event_id else None,
    )
    await _finish(
        session,
        acting.ctx,
        rule,
        row,
        EvaluationStatus.FAILED,
        error={"code": RULE_INTERNAL_ERROR, "message": error[:2000], "details": {}},
    )
    return row


async def _load_trigger_event(
    session: AsyncSession, tenant_id: uuid.UUID, event_id: uuid.UUID
) -> JournalEvent | None:
    for table in (Event, EventArchive):
        found: JournalEvent | None = await session.scalar(
            select(table).where(table.tenant_id == tenant_id, table.id == event_id)
        )
        if found is not None:
            return found
    return None


async def _record_skill_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    invocation: SkillInvocation,
    skill: Skill,
) -> Artifact:
    """The skill's result as a fact of its own: the evidence the rule cites.

    Not bound to a task (the work does not exist yet, and may never); it
    lives in the rule's workspace and is authored by the rule's authority.
    """
    artifact = Artifact(
        id=new_uuid(),
        tenant_id=rule.tenant_id,
        workspace_id=rule.workspace_id,
        task_id=None,
        run_id=None,
        created_by_principal_id=ctx.principal_id,
        type=SKILL_RESULT_ARTIFACT,
        name=f"{skill.name}@{skill.version}",
        uri=None,
        content={
            "invocationId": str(invocation.id),
            "skill": skill.name,
            "version": skill.version,
            "attempt": invocation.attempt,
            "output": dict(invocation.output or {}),
        },
        supersedes_artifact_id=None,
        metadata_json={
            "skillId": str(skill.id),
            "invocationId": str(invocation.id),
            "ruleId": str(rule.id),
            "ruleVersion": row.rule_version,
            "evaluationId": str(row.id),
        },
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()
    await record_event(
        session,
        tenant_id=rule.tenant_id,
        event_type="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "type": artifact.type,
            "name": artifact.name,
            "taskId": None,
            "runId": None,
            "uri": None,
            "supersedesArtifactId": None,
            "skillInvocationId": str(invocation.id),
            "ruleEvaluationId": str(row.id),
            **artifact_event_fields(artifact),
        },
    )
    return artifact


async def _stop_call(
    session: AsyncSession, ctx: AuthContext, invocation: SkillInvocation, reason: str
) -> None:
    """Cancel a call nobody needs any more; a finished one is left alone."""
    if invocation.status not in LIVE_STATUSES:
        return
    try:
        async with session.begin_nested():
            await cancel_skill_invocation(
                session,
                ctx,
                invocation_id=invocation.id,
                reason=reason,
                initiator=CANCELLED_BY_SYSTEM,
            )
    except DependencyUnavailableError:
        raise
    except DomainError:
        # The rule's authority can no longer cancel it (the executor's lease
        # bounds it anyway); the evaluation still ends.
        return


async def resume_evaluation(
    session: AsyncSession,
    *,
    evaluation_id: uuid.UUID,
    trace_run_id: str,
    skill_wait_seconds: float = DEFAULT_SKILL_WAIT_SECONDS,
    skill_check_seconds: float = DEFAULT_SKILL_CHECK_SECONDS,
    claim_wait_seconds: float = DEFAULT_CLAIM_WAIT_SECONDS,
) -> RuleEvaluation | None:
    """Continue an evaluation that waits for its interpretation call or for a claim."""
    row: RuleEvaluation | None = await session.scalar(
        select(RuleEvaluation)
        .where(
            RuleEvaluation.id == evaluation_id,
            RuleEvaluation.status == EvaluationStatus.WAITING,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    rule = await session.get(WorkRule, row.rule_id, populate_existing=True)
    assert rule is not None
    invocation = (
        await session.get(SkillInvocation, row.skill_invocation_id, populate_existing=True)
        if row.skill_invocation_id is not None
        else None
    )
    acting = await _acting(
        session,
        rule,
        trace_run_id=trace_run_id,
        causation_id=str(row.trigger_event_id) if row.trigger_event_id else None,
    )
    ctx = acting.ctx

    # What decided to ask is gone: the answer would be applied by a rule
    # nobody wrote (a new version) or nobody runs (disabled, archived).
    stopped = None
    if rule.status != RuleStatus.ENABLED:
        stopped = RULE_STOPPED
    elif rule.version != row.rule_version:
        stopped = RULE_CHANGED
    if stopped is not None:
        if invocation is not None:
            await _stop_call(session, ctx, invocation, stopped)
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.SKIPPED,
            result={
                "skipped": {
                    "reason": stopped,
                    "ruleStatus": rule.status,
                    "ruleVersion": rule.version,
                }
            },
        )
        return row

    if (row.result or {}).get("waitingFor") == WAITING_FOR_CLAIM:
        return await _resume_claimed(
            session,
            acting,
            rule,
            row,
            claim_wait_seconds=claim_wait_seconds,
            check_seconds=skill_check_seconds,
        )
    assert invocation is not None
    if invocation.status == SkillInvocationStatus.PENDING:
        moved = max(invocation.updated_at, invocation.available_at)
        if moved + timedelta(seconds=skill_wait_seconds) <= utcnow():
            await _stop_call(session, ctx, invocation, SKILL_WAIT_EXPIRED)
            await session.refresh(invocation)
    if invocation.status in LIVE_STATUSES:
        row.next_check_at = utcnow() + timedelta(seconds=skill_check_seconds)
        row.updated_at = utcnow()
        return row

    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    error = invocation.error or {}
    row.result = {
        **row.result,
        "skill": {
            "invocationId": str(invocation.id),
            "skill": f"{skill.name}@{skill.version}",
            "status": invocation.status,
        },
    }
    try:
        await acting.require_standing(session)
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return row
    if invocation.status != SkillInvocationStatus.SUCCEEDED:
        # No interpretation, no verdict: the rule does not guess.
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error={
                "code": "rule_skill_failed",
                "message": f"{skill.name}@{skill.version} ended {invocation.status}",
                "details": {
                    "invocationId": str(invocation.id),
                    "status": invocation.status,
                    "cause": {"code": error.get("code"), "message": error.get("message")},
                },
            },
        )
        return row

    artifact = await _record_skill_artifact(session, ctx, rule, row, invocation, skill)
    row.evidence = [*(row.evidence or []), {"kind": "artifact", "artifactId": str(artifact.id)}]
    facts = await _resume_facts(session, rule, row)
    if facts is None:
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error={
                "code": "rule_trigger_missing",
                "message": "The trigger event is gone",
                "details": {"eventId": str(row.trigger_event_id)},
            },
        )
        return row
    facts.evidence = _task_evidence(row.evidence)
    facts.settings_seen = next(
        (item for item in row.evidence or [] if item.get("kind") == SETTINGS_EVIDENCE), None
    )
    facts.skill = {
        "invocationId": str(invocation.id),
        "skill": f"{skill.name}@{skill.version}",
        "status": invocation.status,
        "output": dict(invocation.output or {}),
        "artifactId": str(artifact.id),
    }
    try:
        await _load_views(session, ctx, rule, facts)
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return row
    await _run_action(session, ctx, rule, row, facts, check_seconds=skill_check_seconds)
    return row


async def _resume_claimed(
    session: AsyncSession,
    acting: _Acting,
    rule: WorkRule,
    row: RuleEvaluation,
    *,
    claim_wait_seconds: float,
    check_seconds: float,
) -> RuleEvaluation:
    """Apply a closing decision that waited for a claim — once, when it has ended.

    The evaluation row is locked (``SKIP LOCKED``) and leaves ``waiting`` in
    the same transaction that applies the decision, so it is applied at most
    once. Work the executor completed meanwhile is not overwritten
    (``already_done``), nor is work handed in and being verified; closed work
    is left as it is (``already_closed``). A claim that outlives
    ``claim_wait_seconds`` fails the decision (``claim_not_released``).
    """
    ctx = acting.ctx
    try:
        await acting.require_standing(session)
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return row
    result = dict(row.result or {})
    since = datetime.fromisoformat(str(result.get("waitingSince") or row.created_at.isoformat()))
    expired = since + timedelta(seconds=claim_wait_seconds) <= utcnow()
    work = [dict(item) for item in result.get("work", [])]
    try:
        async with session.begin_nested():
            await _lock_keys(
                session,
                ctx,
                {
                    w["dedupKey"]
                    for w in work
                    if w.get("waiting") and w.get("target") != ActionTarget.TASK
                },
            )
            for index, item in enumerate(work):
                if item.get("waiting"):
                    work[index] = await _settle(session, ctx, rule, row, item, expired=expired)
    except DependencyUnavailableError:
        raise
    except DomainError as exc:
        await _finish(session, ctx, rule, row, EvaluationStatus.FAILED, error=_domain_failure(exc))
        return row
    result["work"] = work
    if any(w.get("waiting") for w in work):
        row.result = result
        row.next_check_at = utcnow() + timedelta(seconds=check_seconds)
        row.updated_at = utcnow()
        return row
    result.pop("waitingFor", None)
    result.pop("waitingSince", None)
    row.result = result
    if len(work) == 1 and work[0].get("target") == ActionTarget.TASK and work[0].get("skipped"):
        # The bound task was closed, handed in or changed while the decision
        # waited: skipped, as it would have been without the wait (Zh3).
        status, skipped = _bound_skip(work[0])
        await _finish(session, ctx, rule, row, status, result=skipped)
        return row
    stuck = [w["dedupKey"] for w in work if w.get("reason") == CLAIM_NOT_RELEASED]
    if stuck:
        await _finish(
            session,
            ctx,
            rule,
            row,
            EvaluationStatus.FAILED,
            error={
                "code": CLAIM_NOT_RELEASED,
                "message": f"the claim was not released within {int(claim_wait_seconds)} s",
                "details": {"dedupKeys": stuck},
            },
        )
        return row
    await _finish(session, ctx, rule, row, EvaluationStatus.MATCHED)
    return row


async def _settle(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    row: RuleEvaluation,
    item: dict[str, Any],
    *,
    expired: bool,
) -> dict[str, Any]:
    """One waiting decision: apply it, keep waiting, or say why it is moot."""
    base: dict[str, Any] = {
        k: item[k] for k in ("dedupKey", "target", "action", "taskId", "publicId") if k in item
    }
    target = item.get("target")
    task: Task | None
    if target == ActionTarget.TASK:
        if not has_author_filter(rule.trigger):
            # Waited under a rule saved before the filter became mandatory:
            # it closes nothing now, as it would not have started (Zh6).
            return {**base, "skipped": True, "reason": TRIGGER_AUTHOR_UNFILTERED}
        # The checks are made again: rights and status may have changed meanwhile.
        task, skipped = await _bound_task(session, ctx, rule, uuid.UUID(item["taskId"]))
        if skipped is None and await _fact_on_record(session, task, item.get("check")):
            skipped = {"skipped": True, "reason": VERIFICATION_PENDING}
        if skipped is not None:
            return {**base, **skipped}
    else:
        task = await session.get(
            Task, uuid.UUID(item["taskId"]), populate_existing=True, with_for_update=True
        )
    assert task is not None  # pragma: no cover - tasks are never deleted
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_SUCCESS:
        # The executor finished first: its result stands.
        return {**base, "skipped": True, "reason": ALREADY_DONE}
    if task.system_status_category == WorkItemStatusCategory.TERMINAL_CANCELLED:
        return {**base, "skipped": True, "reason": ALREADY_CLOSED}
    claim = await live_claim_of(session, task)
    if claim is not None:
        if expired:
            return {**base, "failed": True, "reason": CLAIM_NOT_RELEASED}
        # Still (or again) under a claim: a run started since is asked too.
        waiting = await _await_release(session, ctx, rule, task, claim.id, item["dedupKey"])
        return {**base, **waiting, **({"check": item["check"]} if item.get("check") else {})}
    if rule.action["kind"] == ActionKind.CANCEL_WORK and await open_attempt(session, task.id):
        # Handed in before the decision could apply: the executor's result is
        # being verified and is not thrown away by a stale cancellation.
        return {**base, "skipped": True, "reason": VERIFICATION_PENDING}
    closed = await _close(
        session, ctx, rule, row, task, item["dedupKey"], item.get("check"), target=target
    )
    return {**base, **closed, "afterClaim": True}


async def _resume_facts(session: AsyncSession, rule: WorkRule, row: RuleEvaluation) -> Facts | None:
    if row.trigger_event_id is not None:
        event = await _load_trigger_event(session, rule.tenant_id, row.trigger_event_id)
        return _event_facts(rule, event) if event is not None else None
    scheduled = int(row.trigger_ref.split(":", 1)[1])
    return _schedule_facts(
        rule, row.trigger_ref, datetime.fromtimestamp(scheduled, tz=utcnow().tzinfo)
    )


# --- the worker's loops ------------------------------------------------------------


def _caused_by_rules(event: JournalEvent, rule_calls: set[uuid.UUID]) -> bool:
    """Is this event a consequence of what a rule did?

    What a rule writes carries its correlation; the rest of a skill call it
    queued is written by the call's executor under the executor's own
    correlation, so such calls are recognized by their idempotency key.
    """
    return (
        event.entity_type == "rule"
        or (event.correlation_id or "").startswith(CORRELATION_PREFIX)
        or (event.entity_type == "skill_invocation" and event.entity_id in rule_calls)
    )


async def _rule_calls(
    session: AsyncSession, tenant_id: uuid.UUID, events: list[JournalEvent]
) -> set[uuid.UUID]:
    """The skill calls among these events' entities that rules queued."""
    ids = {e.entity_id for e in events if e.entity_type == "skill_invocation"}
    if not ids:
        return set()
    rows = await session.scalars(
        select(SkillInvocation.id).where(
            SkillInvocation.tenant_id == tenant_id,
            SkillInvocation.id.in_(ids),
            SkillInvocation.idempotency_key.startswith(CORRELATION_PREFIX),
        )
    )
    return set(rows.all())


async def _agent_principals(
    session: AsyncSession, tenant_id: uuid.UUID, rules: Sequence[WorkRule]
) -> dict[str, str]:
    """The principals of the active agents the rules' triggers name as authors (Zh6)."""
    keys = {rule.trigger["agent"] for rule in rules if rule.trigger.get("agent")}
    if not keys:
        return {}
    rows = await session.execute(
        select(Agent.key, Agent.principal_id).where(
            Agent.tenant_id == tenant_id,
            Agent.key.in_(keys),
            Agent.status == AgentStatus.ACTIVE,
            Agent.principal_id.is_not(None),
        )
    )
    return {key: str(principal_id) for key, principal_id in rows.all()}


def _outside_workspace(rule: WorkRule, event: JournalEvent) -> bool:
    """An event that names another workspace does not reach a workspace rule.

    Events that name no workspace are facts of the whole tenant and reach
    every rule (CP-ADR-0063 §3).
    """
    if rule.workspace_id is None:
        return False
    named = (event.payload or {}).get("workspaceId")
    return isinstance(named, str) and named != str(rule.workspace_id)


async def due_rule_tenants(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Tenants whose rule cursor has events to read and no backoff pending."""
    rows = await session.execute(
        text(
            """
            SELECT c.tenant_id
              FROM event_consumer_cursors c
             WHERE c.name = :name
               AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= now())
               AND EXISTS (
                   SELECT 1 FROM events e
                    WHERE e.tenant_id = c.tenant_id
                      AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)
                      AND e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint
               )
             ORDER BY c.updated_at ASC
             LIMIT :limit
            """
        ),
        {"name": RULES_CONSUMER, "limit": limit},
    )
    return [row[0] for row in rows]


async def process_tenant_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    batch_size: int,
    trace_run_id: str,
    skill_check_seconds: float = DEFAULT_SKILL_CHECK_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Evaluate one batch of a tenant's journal and advance its cursor.

    The cursor row is locked (``SKIP LOCKED``) for the transaction, so two
    workers never evaluate one tenant's batch side by side; everything the
    batch writes commits together with the new cursor position.

    Each evaluation runs in a savepoint. An unexpected error rolls the whole
    batch back (the worker holds the tenant back with backoff) until the
    batch has failed ``max_attempts`` times; after that the evaluation that
    still breaks is recorded ``failed`` and the rest of the batch goes on.
    An unavailable dependency is never recorded: it always rolls back.
    """
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == RULES_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if cursor is None:
        return 0
    events = await fetch_events_after(
        session,
        tenant_id=tenant_id,
        start=EventPosition(cursor.tx_id, cursor.sequence),
        limit=batch_size,
    )
    if not events:
        return 0
    # Every evaluation of the batch shares this transaction, and the task
    # rows an earlier one locks stay locked until the commit: every principal
    # a rule may act as goes first (``lock_rule_principals``, CP-ADR-0077 §3).
    await lock_rule_principals(session, tenant_id)
    rules = (
        await session.scalars(
            select(WorkRule)
            .where(
                WorkRule.tenant_id == tenant_id,
                WorkRule.status == RuleStatus.ENABLED,
                or_(
                    WorkRule.trigger["kind"].astext == TriggerKind.OBSERVATION.value,
                    WorkRule.trigger["kind"].astext == TriggerKind.EVENT.value,
                ),
            )
            .order_by(WorkRule.created_at, WorkRule.id)
        )
    ).all()
    rule_calls = await _rule_calls(session, tenant_id, events)
    authors = await _agent_principals(session, tenant_id, rules)
    isolate = cursor.failure_count >= max_attempts
    evaluated = 0
    for event in events:
        if _caused_by_rules(event, rule_calls):
            continue
        for rule in rules:
            assert rule.enabled_at is not None
            if event.occurred_at < rule.enabled_at:
                continue
            if not trigger_matches(rule.trigger, event.event_type, event.payload or {}):
                continue
            actor_id = str(event.actor_id) if event.actor_id is not None else None
            if not author_matches(rule.trigger, actor_id, authors):
                continue
            if _outside_workspace(rule, event):
                continue
            trigger_ref = f"event:{event.id}"
            try:
                async with session.begin_nested():
                    done = await evaluate_trigger(
                        session,
                        rule,
                        _event_facts(rule, event),
                        trigger_ref=trigger_ref,
                        trigger_event_id=event.id,
                        trace_run_id=trace_run_id,
                        skill_check_seconds=skill_check_seconds,
                    )
            except DependencyUnavailableError:
                raise
            except Exception as exc:
                if not isolate:
                    raise
                logger.exception(
                    "rule evaluation keeps failing; recorded as failed",
                    extra={"rule_id": str(rule.id), "trigger_ref": trigger_ref},
                )
                done = await record_internal_failure(
                    session,
                    rule,
                    _event_facts(rule, event),
                    trigger_ref=trigger_ref,
                    trigger_event_id=event.id,
                    trace_run_id=trace_run_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            evaluated += done is not None
    last = events[-1]
    cursor.tx_id = last.tx_id
    cursor.sequence = last.sequence
    cursor.updated_at = utcnow()
    cursor.failure_count = 0
    cursor.next_attempt_at = None
    cursor.parked_at = None
    cursor.parked_reason = None
    cursor.parked_event_id = None
    metadata = dict(cursor.metadata_ or {})
    metadata["evaluated_total"] = int(metadata.get("evaluated_total", 0)) + evaluated
    cursor.metadata_ = metadata
    return len(events)


async def record_tenant_failure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    error: str,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> None:
    """Hold one tenant's cursor back with a growing delay and a visible reason.

    The cursor does not move past the failing batch — skipping it would lose
    facts silently; the next attempt re-reads the same batch.
    """
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == RULES_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if cursor is None:  # pragma: no cover - created with the first rule
        return
    cursor.failure_count += 1
    cursor.parked_at = utcnow()
    cursor.parked_reason = error[:2000]
    delay = min(backoff_base_seconds * (2 ** (cursor.failure_count - 1)), backoff_max_seconds)
    cursor.next_attempt_at = utcnow() + timedelta(seconds=delay)
    cursor.updated_at = utcnow()


async def due_schedules(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkRule.id)
        .where(
            WorkRule.status == RuleStatus.ENABLED,
            WorkRule.next_run_at.is_not(None),
            WorkRule.next_run_at <= utcnow(),
        )
        .order_by(WorkRule.next_run_at)
        .limit(limit)
    )
    return list(rows.all())


async def run_schedule(
    session: AsyncSession,
    *,
    rule_id: uuid.UUID,
    trace_run_id: str,
    skill_check_seconds: float = DEFAULT_SKILL_CHECK_SECONDS,
) -> RuleEvaluation | None:
    rule: WorkRule | None = await session.scalar(
        select(WorkRule)
        .where(
            WorkRule.id == rule_id,
            WorkRule.status == RuleStatus.ENABLED,
            and_(WorkRule.next_run_at.is_not(None), WorkRule.next_run_at <= utcnow()),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if rule is None or rule.trigger.get("kind") != TriggerKind.SCHEDULE:
        return None
    assert rule.next_run_at is not None
    scheduled_at = rule.next_run_at
    trigger_ref, upcoming = schedule_slot(rule)
    rule.next_run_at = upcoming
    return await evaluate_trigger(
        session,
        rule,
        _schedule_facts(rule, trigger_ref, scheduled_at),
        trigger_ref=trigger_ref,
        trigger_event_id=None,
        trace_run_id=trace_run_id,
        skill_check_seconds=skill_check_seconds,
    )


async def record_schedule_failure(
    session: AsyncSession, *, rule_id: uuid.UUID, error: str, trace_run_id: str
) -> RuleEvaluation | None:
    """Close a due schedule slot whose run broke, and move to the next slot.

    Without this the slot's ``next_run_at`` rolls back with the failed run
    and the rule is retried on every worker tick. The rule's own interval
    is its backoff: a schedule has no fact to lose, the next slot looks at
    the world afresh.
    """
    rule: WorkRule | None = await session.scalar(
        select(WorkRule)
        .where(
            WorkRule.id == rule_id,
            WorkRule.status == RuleStatus.ENABLED,
            and_(WorkRule.next_run_at.is_not(None), WorkRule.next_run_at <= utcnow()),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if rule is None or rule.trigger.get("kind") != TriggerKind.SCHEDULE:
        return None
    assert rule.next_run_at is not None
    scheduled_at = rule.next_run_at
    trigger_ref, upcoming = schedule_slot(rule)
    rule.next_run_at = upcoming
    return await record_internal_failure(
        session,
        rule,
        _schedule_facts(rule, trigger_ref, scheduled_at),
        trigger_ref=trigger_ref,
        trigger_event_id=None,
        trace_run_id=trace_run_id,
        error=error,
    )


async def due_evaluations(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(RuleEvaluation.id)
        .where(
            RuleEvaluation.status == EvaluationStatus.WAITING,
            RuleEvaluation.next_check_at <= utcnow(),
        )
        .order_by(RuleEvaluation.next_check_at)
        .limit(limit)
    )
    return list(rows.all())


async def postpone_evaluation(
    session: AsyncSession, *, evaluation_id: uuid.UUID, seconds: float
) -> None:
    """After an unexpected error: look again later instead of spinning on it."""
    row = await session.get(RuleEvaluation, evaluation_id, with_for_update=True)
    if row is not None and row.status == EvaluationStatus.WAITING:
        row.next_check_at = utcnow() + timedelta(seconds=seconds)
        row.updated_at = utcnow()
