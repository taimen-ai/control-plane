"""Task types, agents and rules: the catalog kinds a package plan compares beside processes.

CP-ADR-0074 §11, amendment 2026-09-29. ``packages:plan`` compares each object
of these kinds as the fields of its **form** — what the catalog holds of it,
in the names of the package — and ``packages:apply`` publishes the form by the
ordinary command of its kind, under that kind's right. The forms follow what
the package installer (package-sdk) compares, so a key the installer applied plans
``unchanged`` for the same file:

- ``TaskType`` — the fields of ``POST /task-types``; a version is immutable,
  a change is the next version, and every other active version of the key is
  deprecated (``deprecates``), as the installer does. The latest is the
  highest *active* version. A field the installer compares only when the file
  sets it (``lifecycleSchema``, ``contextSchema``, ``instructions``,
  ``completionSchema``, ``artifactSchema``, ``acceptance``, ``executorRoles``)
  takes the latest value when the file leaves it out — in the comparison and
  in the version published (the installer lets the core default them in a new
  version).
- ``Agent`` — the revision body (canonical, CP-ADR-0073 §2) with the desired
  state: ``state`` and ``placement.replicas``. A change of the body is a new
  revision, of the state alone — none (``version`` is the revision). A retired
  key is ``agent_retired``: a package does not bring it back.
- ``WorkRule`` — ``description``, ``trigger``, ``condition``,
  ``interpretation``, ``action`` (normalized), ``identity``, ``status`` and
  ``workspaceId`` of the live rule of the key. The first six are a ``PATCH``
  (a new rule version), ``status`` is ``:enable`` / ``:disable``;
  ``workspaceId`` is what the rule is — another one is
  ``rule_workspace_immutable``.

What an object refers to (skills, task types, agents, the caller's rights to
lend) is checked by the command itself: the plan runs it in a transaction it
rolls back (``package_plan._trial``), the apply for real; an agent's
revision names the package as its source.
"""

import copy
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands import agents as agent_commands
from control_plane.application.commands import task_types as task_type_commands
from control_plane.application.commands import work_rules as rule_commands
from control_plane.application.commands.role_references import normalize_executor_roles
from control_plane.domain.agent_instructions import validate_instructions
from control_plane.domain.enums import AgentStatus, TaskTypeStatus
from control_plane.domain.errors import DomainError
from control_plane.domain.package_plan import PLANNED_KINDS, canonical_hash
from control_plane.domain.package_source import PackageObject
from control_plane.domain.process_definition import Problem
from control_plane.domain.settings_refs import SettingsScope
from control_plane.domain.task_execution import normalize_execution
from control_plane.domain.work_graph import normalize_checks
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.domain.work_rules import (
    RuleStatus,
    check_settings_refs,
    normalize_description,
    normalize_rule_spec,
)
from control_plane.infrastructure.db.models import Agent, AgentRevision, TaskType, WorkRule

# The kinds of this module.
CATALOG_KINDS = ("TaskType", "Agent", "WorkRule")

# The spec of a package object as the route of its kind takes it (its shape
# checked, without the defaults the route fills in), or the findings of its shape.
SpecShape = Callable[[PackageObject], tuple[dict[str, Any] | None, list[Problem]]]

TASK_TYPE_DEFAULTS: dict[str, Any] = {
    "displayName": "",
    "description": "",
    "fieldSchema": {},
    "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
    "execution": None,
    "approvalSchema": {},
    "contextSchema": {},
    "instructions": "",
    "completionSchema": {},
    "artifactSchema": {},
    "acceptance": [],
    "executorRoles": [],
}
# Compared only when the file sets them (the installer's SERVER_DEFAULTED).
TASK_TYPE_KEPT_WHEN_ABSENT = frozenset(
    {
        "lifecycleSchema",
        "contextSchema",
        "instructions",
        "completionSchema",
        "artifactSchema",
        "acceptance",
        "executorRoles",
    }
)
RULE_MUTABLE = ("description", "trigger", "condition", "interpretation", "action", "identity")


@dataclass
class Latest:
    """What the catalog holds of a key: its version, the hash and the form, the row."""

    version: int
    hash: str
    spec: dict[str, Any]
    row: Any
    # Agent: the key is retired. TaskType: every active version, by number.
    retired: bool = False
    active: dict[int, Any] = field(default_factory=dict)


# --- what the catalog holds ------------------------------------------------------------------


def task_type_form(row: TaskType) -> dict[str, Any]:
    return {
        "displayName": row.display_name,
        "description": row.description or "",
        "fieldSchema": row.field_schema or {},
        "lifecycleSchema": row.lifecycle_schema or SYSTEM_TASK_LIFECYCLE,
        "execution": row.execution,
        "approvalSchema": row.approval_schema or {},
        "contextSchema": row.context_schema or {},
        "instructions": row.instructions or "",
        "completionSchema": row.completion_schema or {},
        "artifactSchema": row.artifact_schema or {},
        "acceptance": row.acceptance or [],
        "executorRoles": row.executor_roles or [],
    }


def agent_form(body: Mapping[str, Any], state: str, replicas: int) -> dict[str, Any]:
    """A revision body with the desired state, as a spec carries it."""
    form = copy.deepcopy(dict(body))
    form["state"] = state
    if isinstance(form.get("placement"), dict):
        form["placement"]["replicas"] = replicas
    return form


def rule_form(row: WorkRule) -> dict[str, Any]:
    return {
        "description": row.description or "",
        "trigger": row.trigger,
        "condition": row.condition,
        "interpretation": row.interpretation,
        "action": row.action,
        "identity": {"agent": row.identity_agent_key} if row.identity_agent_key else None,
        "status": row.status,
        "workspaceId": str(row.workspace_id) if row.workspace_id else None,
    }


async def lock_keys(db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]]) -> None:
    """Hold every task type, agent and rule of ``pairs`` until the transaction ends.

    The key locks the commands of each kind take themselves, and the rows the
    other commands of the kind lock: a publication racing the apply either
    lands before its plan (and the hash is stale) or waits for the commit.

    The order is one for every caller — the kinds in ``PLANNED_KINDS``, the
    keys sorted within a kind — the order the apply and the trial of a plan
    run the commands in, so neither holds a key the other waits behind.

    The rows of task types and rules are ``FOR NO KEY UPDATE``: a task or a
    rule evaluation inserted under a foreign key takes ``FOR KEY SHARE`` on
    them, and the process engine inserts tasks while it holds the instance an
    apply locks next. An agent row is no foreign key target of such an insert.
    """
    order = {kind: n for n, kind in enumerate(PLANNED_KINDS)}
    for kind, key in sorted(pairs, key=lambda pair: (order.get(pair[0], len(order)), pair[1])):
        if kind == "TaskType":
            await task_type_commands._lock_type_key(db, tenant_id, key)
            await db.execute(
                select(TaskType.id)
                .where(TaskType.tenant_id == tenant_id, TaskType.key == key)
                .with_for_update(key_share=True)
            )
        elif kind == "Agent":
            await agent_commands._lock_agent_key(db, tenant_id, key)
            await db.execute(
                select(Agent.id)
                .where(Agent.tenant_id == tenant_id, Agent.key == key)
                .with_for_update()
            )
        elif kind == "WorkRule":
            await rule_commands.lock_rule_key(db, tenant_id, key)
            await db.execute(
                select(WorkRule.id)
                .where(
                    WorkRule.tenant_id == tenant_id,
                    WorkRule.key == key,
                    WorkRule.status != RuleStatus.ARCHIVED,
                )
                .with_for_update(key_share=True)
            )


async def latest_of(db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> Latest | None:
    if kind == "TaskType":
        rows = list(
            await db.scalars(
                select(TaskType)
                .where(
                    TaskType.tenant_id == tenant_id,
                    TaskType.key == key,
                    TaskType.status == TaskTypeStatus.ACTIVE,
                )
                .order_by(TaskType.version)
            )
        )
        if not rows:
            return None
        form = task_type_form(rows[-1])
        return Latest(
            rows[-1].version,
            canonical_hash(form),
            form,
            rows[-1],
            active={row.version: row for row in rows},
        )
    if kind == "Agent":
        agent = await db.scalar(select(Agent).where(Agent.tenant_id == tenant_id, Agent.key == key))
        if agent is None:
            return None
        revision = await db.scalar(
            select(AgentRevision).where(
                AgentRevision.agent_id == agent.id,
                AgentRevision.revision == agent.current_revision,
            )
        )
        assert revision is not None  # an agent always has its current revision
        form = agent_form(revision.spec, agent.state, agent.replicas)
        return Latest(
            agent.current_revision,
            canonical_hash(form),
            form,
            agent,
            retired=agent.status == AgentStatus.RETIRED,
        )
    rule = await db.scalar(
        select(WorkRule).where(
            WorkRule.tenant_id == tenant_id,
            WorkRule.key == key,
            WorkRule.status != RuleStatus.ARCHIVED,
        )
    )
    if rule is None:
        return None
    form = rule_form(rule)
    return Latest(rule.version, canonical_hash(form), form, rule)


# --- what the package wants ------------------------------------------------------------------


def wanted_form(
    kind: str,
    sent: Mapping[str, Any],
    latest: Latest | None,
    settings: SettingsScope | None = None,
) -> dict[str, Any]:
    """The form of what the package sends; a document the command would refuse raises.

    ``settings`` — the settings the package declares: the reads of ``settings``
    of a rule are checked against them (CP-ADR-0081 §6); ``None`` — not checked.
    """
    if kind == "TaskType":
        form: dict[str, Any] = {}
        for name, default in TASK_TYPE_DEFAULTS.items():
            if name in sent:
                form[name] = copy.deepcopy(sent[name])
            elif name in TASK_TYPE_KEPT_WHEN_ABSENT and latest is not None:
                form[name] = copy.deepcopy(latest.spec[name])
            else:
                form[name] = copy.deepcopy(default)
        if "execution" in sent:
            form["execution"] = normalize_execution(form["execution"])
        if "instructions" in sent:
            form["instructions"] = validate_instructions(form["instructions"], field="instructions")
        if "acceptance" in sent:
            form["acceptance"] = normalize_checks(form["acceptance"], field="acceptance")
        if "executorRoles" in sent:
            form["executorRoles"] = normalize_executor_roles(form["executorRoles"])
        return form
    if kind == "Agent":
        body, state, replicas = agent_commands.split_desired_state(dict(sent))
        canonical, _ = agent_commands.spec_hash_of(body)
        return agent_form(canonical, state, replicas)
    spec = normalize_rule_spec(
        trigger=sent.get("trigger"),
        condition=sent.get("condition"),
        interpretation=sent.get("interpretation"),
        action=sent.get("action"),
    )
    if settings is not None:
        check_settings_refs(spec, settings)
    return {
        "description": normalize_description(sent.get("description", "")),
        "trigger": spec.trigger,
        "condition": spec.condition,
        "interpretation": spec.interpretation,
        "action": spec.action,
        "identity": copy.deepcopy(sent.get("identity")),
        "status": sent.get("status", RuleStatus.ENABLED),
        "workspaceId": sent.get("workspaceId"),
    }


def _agent_body(form: Mapping[str, Any]) -> dict[str, Any]:
    return agent_commands.split_desired_state(dict(form))[0]


async def planned_version(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    key: str,
    latest: Latest | None,
    published: Mapping[str, Any],
    action: str,
) -> int:
    """The version (the revision of an agent) the object has after the apply."""
    if latest is not None and action == "unchanged":
        return latest.version
    if kind == "TaskType":
        highest = await db.scalar(
            select(func.max(TaskType.version)).where(
                TaskType.tenant_id == tenant_id, TaskType.key == key
            )
        )
        return int(highest or 0) + 1
    if latest is None:
        return 1
    if kind == "Agent":
        changed = _agent_body(published) != _agent_body(latest.spec)
    else:
        changed = any(published.get(name) != latest.spec.get(name) for name in RULE_MUTABLE)
    return latest.version + 1 if changed else latest.version


def deprecated_versions(latest: Latest | None, action: str) -> list[int]:
    """The active versions of a task type the apply deprecates: all but the one it keeps."""
    if latest is None:
        return []
    keep = latest.version if action == "unchanged" else None
    return [version for version in sorted(latest.active) if version != keep]


def static_problems(
    kind: str, key: str, latest: Latest | None, published: Mapping[str, Any]
) -> list[Problem]:
    """What the plan refuses without asking the command."""
    if kind == "Agent" and latest is not None and latest.retired:
        return [
            Problem(
                "agent_retired",
                "error",
                "",
                f"agent {key!r} is retired: a package does not bring it back",
                hint="give the agent another key",
            )
        ]
    if kind == "WorkRule" and latest is not None:
        before, after = latest.spec.get("workspaceId"), published.get("workspaceId")
        if before != after:
            return [
                Problem(
                    "rule_workspace_immutable",
                    "error",
                    "/spec/workspaceId",
                    f"the workspace of rule {key!r} is what it is ({before or 'the tenant'},"
                    f" the package says {after or 'the tenant'})",
                    hint="retire the rule in the installation and apply it again",
                )
            ]
    return []


def finding(exc: DomainError, severity: str = "error") -> Problem:
    """A refusal of a command as a finding of the object it was about."""
    details = exc.details or {}
    where = details.get("path") or details.get("field")
    path = ""
    if isinstance(where, str) and where:
        where = where.removeprefix("$.").removeprefix("spec.").removeprefix("spec")
        path = "/spec" + "".join(
            "/" + part for part in where.replace("[", ".").replace("]", "").split(".") if part
        )
    return Problem(exc.code, severity, path, exc.message)


# --- publication -----------------------------------------------------------------------------


@dataclass
class Published:
    version: int
    # IAM identities whose binding changed (an agent): the route drops their
    # cache entries after the commit (ADR-0053).
    touched: list[tuple[str, uuid.UUID]] = field(default_factory=list)


async def publish(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    kind: str,
    key: str,
    spec: dict[str, Any],
    latest: Latest | None,
    action: str,
    deprecates: list[int],
    package: tuple[str, str] | None,
    settings: SettingsScope | None = None,
) -> Published:
    """Apply one object by the command of its kind; every command checks its own right.

    ``settings`` — the settings of the package a rule's reads are checked
    against: those the apply records, not the active revision before it.
    """
    if kind == "TaskType":
        version = latest.version if latest is not None else 0
        if action != "unchanged":
            row = await task_type_commands.create_task_type_version(
                db,
                ctx,
                key=key,
                display_name=spec["displayName"],
                description=spec["description"],
                field_schema=spec["fieldSchema"],
                lifecycle_schema=spec["lifecycleSchema"],
                execution=spec["execution"],
                approval_schema=spec["approvalSchema"],
                context_schema=spec["contextSchema"],
                instructions=spec["instructions"],
                completion_schema=spec["completionSchema"],
                artifact_schema=spec["artifactSchema"],
                acceptance=spec["acceptance"],
                executor_roles=spec["executorRoles"],
            )
            version = row.version
        for number in deprecates:
            assert latest is not None
            await task_type_commands.deprecate_task_type(db, ctx, type_id=latest.active[number].id)
        return Published(version)
    if kind == "Agent":
        view = await agent_commands.publish_agent(
            db, ctx, key=key, spec=copy.deepcopy(spec), package=package, link=False
        )
        return Published(view.revision.revision, list(view.touched_identities))
    identity = spec.get("identity")
    if latest is None:
        workspace = spec.get("workspaceId")
        rule = await rule_commands.create_rule(
            db,
            ctx,
            key=key,
            trigger=spec["trigger"],
            action=spec["action"],
            condition=spec["condition"],
            interpretation=spec["interpretation"],
            description=spec["description"],
            workspace_id=uuid.UUID(workspace) if workspace else None,
            status=spec["status"],
            identity=identity,
            settings=settings,
        )
        return Published(rule.version)
    rule = latest.row
    changed = {
        name: spec.get(name) for name in RULE_MUTABLE if spec.get(name) != latest.spec.get(name)
    }
    if changed:
        rule = await rule_commands.update_rule(
            db, ctx, rule_id=rule.id, expected_version=rule.version, settings=settings, **changed
        )
    if spec["status"] != rule.status:
        rule = await rule_commands.set_rule_status(db, ctx, rule_id=rule.id, status=spec["status"])
    return Published(rule.version)
