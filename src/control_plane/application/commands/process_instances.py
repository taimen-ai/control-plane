"""Process instances: the engine's steps and their intents in one transaction (CP-ADR-0074 §3-§8).

An instance is a row of ``process_instances`` pinned to the version it
started on; the engine (:mod:`control_plane.domain.process_engine`) owns its
``state``. Every input goes through :func:`take`:

1. the input is taken once — ``(instance, source_ref)`` of the journal is
   unique, a redelivered event or a timer seen twice writes nothing;
2. ``step(definition, state, input)`` gives the next state, the decisions and
   the intents;
3. the intents are executed by the ordinary commands of the core — tasks,
   approvals, skill calls, events, timer rows — with the authority of the
   process's identity agent (CP-ADR-0074 §14), each in a savepoint;
4. the journal entry (input, decisions, intents with what became of them) and
   the new state are written in the same transaction: all of it or nothing;
5. the waiting steps the step opened and closed are ``process.step_entered``
   and ``process.step_exited`` of the core journal, in the same transaction
   (:mod:`control_plane.domain.process_steps`, CP-ADR-0074 §13 amendment).

A command that refuses an intent opening work (a task, approvals, a skill
call, a nested process) is the engine's next input ``intent_failed``: the
process's ``try`` catches it, otherwise the instance fails. A refusal to
close work (cancel a task a person has claimed) is only recorded: the engine
has already stopped waiting for it.

Where the facts come back from: the worker reads the journal with its own
cursor ``processes`` (:func:`process_tenant_events`) and routes each event
to instances — ``refs`` of the instance for its tasks, approvals, skill calls
and nested instances, ``start``/``correlate`` of the latest published
version of each process for the rest; timers are the rows of
``process_timers`` whose ``due_at`` has come (:func:`fire_timer`). Tasks of a
step carry the external reference ``process/<instance>/<element>``
(ADR-0047) and the origin ``{kind: process}``; a goal is never set on them —
the goal of a process is derived from the core (TAI-ADR-0055).

Memory (CP-ADR-0076): a ``remember`` is an observation of the core
(``observation.recorded``) the process's identity records in the step's
transaction — it reaches memory by the ordinary delivery, so it needs no
memory to be up. A ``recall`` is a row of ``process_recalls`` written in the
step's transaction and executed by the worker after it (:func:`run_recall`):
memory is asked outside any transaction, and its answer — or the refusal of a
request memory rejects — is the instance's ``recall`` input, recorded whole
in the journal. A memory that does not answer is asked again until the step's
own timeout timer ends the wait. The context of a step's task is the step's
profile with its anchors computed (``tasks.context_profile``), compiled at
claim like the profile of a task type (CP-ADR-0064).

The journal alone replays an instance (:func:`replay_instance`): the answers
of memory are read from it, memory is never asked.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import (
    AuthContext,
    authorize,
    visible_objects,
)
from control_plane.application.commands.agent_assignees import agent_principal
from control_plane.application.commands.approval_outcomes import (
    authority_snapshot,
    require_active_credential,
)
from control_plane.application.commands.approvals import cancel_approval, request_approval
from control_plane.application.commands.catalog_retirements import (
    PROCESS,
    retired_keys,
    share_keys,
    share_keys_now,
)
from control_plane.application.commands.eligibility import RequirementSpec
from control_plane.application.commands.observations import record_observation
from control_plane.application.commands.process_definitions import (
    PROCESSES_CONSUMER,
    load_catalog,
    process_scope,
)
from control_plane.application.commands.rule_evaluations import agent_authority
from control_plane.application.commands.skill_invocations import invoke_skill
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import create_task, update_task
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.graph import entity_key
from control_plane.application.event_cursor import EventPosition
from control_plane.application.events import record_event
from control_plane.application.locking import lock_process_identities
from control_plane.application.queries.events import JournalEvent, fetch_events_after
from control_plane.application.queries.package_settings import (
    History,
    history,
    object_scope,
    snapshot,
)
from control_plane.application.queries.recall import (
    ProcessRecallFailed,
    fetch_process_recall,
    graph_scope,
    process_recall_call,
)
from control_plane.config import Settings
from control_plane.domain import process_engine as engine
from control_plane.domain import process_replay, process_sla, process_steps
from control_plane.domain.calendar import Calendar
from control_plane.domain.enums import ApprovalStatus, Permission, SkillInvocationStatus
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DependencyUnavailableError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.process_definition import references
from control_plane.domain.settings_refs import NONE, SETTINGS, SettingsScope
from control_plane.domain.work_item import WorkItemStatusCategory
from control_plane.infrastructure.context_provider import GraphProvider
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import (
    Approval,
    CalendarVersion,
    EventConsumerCursor,
    ExternalReference,
    Principal,
    ProcessDefinition,
    ProcessInstance,
    ProcessInstanceEvent,
    ProcessRecall,
    ProcessTimer,
    Role,
    SkillInvocation,
    Task,
)

logger = logging.getLogger(__name__)

# The correlation of everything an instance writes: ``process:<instance id>``.
CORRELATION_PREFIX = "process:"
# The external reference of a step's task: ``process/<instance>/<element>``.
EXTERNAL_SYSTEM = "control-plane"
EXTERNAL_TYPE = "process_step"
# Follow-up inputs (refused intents) one input may cause, a bound against a
# loop of retries that are refused at once every time.
MAX_FOLLOW_UPS = 20
# Checked definitions kept compiled, by version id (versions never change).
_DEFINITION_CACHE_SIZE = 128
_definitions: dict[tuple[uuid.UUID, str | None, int | None], engine.Definition] = {}
# Whether a version names ``settings`` anywhere, by version id: one that does not reads none.
_mentions: dict[uuid.UUID, bool] = {}
_calendars: dict[uuid.UUID, Calendar] = {}

# Intents that open work the engine then waits for: their refusal is an input.
_OPENING = ("create_task", "request_approvals", "invoke_skill", "start_child")
# Step kinds of activities, for the view of open elements.
_ACTIVITY_STEP = {
    "task": "human",
    "agent": "call",
    "approval": "approve",
    "skill": "call",
    "child": "call",
    "recall": "recall",
    "listen": "listen",
    "wait": "wait",
    "retro_skill": "call",
    "retro_review": "human",
}
_CHILD_ENDS = {
    "process.completed": "completed",
    "process.failed": "failed",
    "process.cancelled": "cancelled",
}
_APPROVAL_OUTCOMES = {
    "approval.approved": "approved",
    "approval.rejected": "rejected",
    "approval.cancelled": "cancelled",
}
_SKILL_ENDS = (
    "skill.invocation_succeeded",
    "skill.invocation_failed",
    "skill.invocation_cancelled",
)


# --- definitions, calendars, identity ------------------------------------------------


async def settings_scope(session: AsyncSession, row: ProcessDefinition) -> SettingsScope:
    """The settings ``settings`` of a version is typed by: the active revision of its package.

    A version that does not name ``settings`` anywhere reads none: no lookup.
    """
    mentions = _mentions.get(row.id)
    if mentions is None:
        mentions = SETTINGS in json.dumps(row.spec)
        if len(_mentions) >= _DEFINITION_CACHE_SIZE:
            _mentions.pop(next(iter(_mentions)))
        _mentions[row.id] = mentions
    if not mentions:
        return NONE
    return await object_scope(session, row.tenant_id, PROCESS, row.key)


async def definition_of(
    session: AsyncSession, row: ProcessDefinition, scope: SettingsScope | None = None
) -> engine.Definition:
    """The version compiled for the engine; a version is checked once per worker.

    ``scope`` — the revision of the settings schema ``settings`` is typed by
    (CP-ADR-0081 §6): a step the one it reads, a replay the one a record
    names; ``None`` — the active one. One compilation per version and revision.
    """
    if scope is None:
        scope = await settings_scope(session, row)
    cache_key = (row.id, scope.package, scope.revision)
    cached = _definitions.get(cache_key)
    if cached is not None:
        return cached
    catalog = await load_catalog(
        session, row.tenant_id, row.key, row.spec, None, retired=False, settings=scope
    )
    # A calendar that dropped its working hours after publication fails the
    # due it counts (process.sla_failed, CP-ADR-0078 §3), not the version.
    catalog = replace(catalog, calendars_with_hours=None)
    try:
        definition = engine.Definition.build(
            row.key, row.spec, catalog, engine_revision=row.engine_revision
        )
    except engine.DefinitionError as exc:
        # Published versions pass the check; one that no longer does lost a
        # skill, a task type or its agent from the catalog after publication.
        raise ConflictError(
            "process_definition_unusable",
            f"Process {row.key}@{row.version} no longer passes the check: {exc}",
            details={
                "process": f"{row.key}@{row.version}",
                "problems": [p.out() for p in exc.problems],
            },
        ) from exc
    except engine.EngineError as exc:
        # A revision this code does not know: the version was published by a
        # newer release, and the code was rolled back since.
        raise ConflictError(
            "process_definition_unusable",
            f"Process {row.key}@{row.version} cannot run: {exc}",
            details={
                "process": f"{row.key}@{row.version}",
                "problems": [],
                "engineRevision": row.engine_revision,
            },
        ) from exc
    if len(_definitions) >= _DEFINITION_CACHE_SIZE:
        _definitions.pop(next(iter(_definitions)))
    _definitions[cache_key] = definition
    return definition


async def calendars_named(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    spec: Mapping[str, Any],
    *,
    own: Mapping[str, Calendar] | None = None,
) -> tuple[dict[str, Calendar], dict[str, int]]:
    """The calendars a process spec names: their latest versions, and the numbers of those.

    ``own`` stand for their keys instead of the latest versions and have no
    number: calendars not published yet, as the apply of a package plan will
    publish them.
    """
    own = own or {}
    keys = set(references(spec).calendars)
    calendars, versions = await latest_calendar_versions(session, tenant_id, keys - set(own))
    calendars.update({key: calendar for key, calendar in own.items() if key in keys})
    return calendars, versions


async def latest_calendar_versions(
    session: AsyncSession, tenant_id: uuid.UUID, keys: set[str]
) -> tuple[dict[str, Calendar], dict[str, int]]:
    """The latest published versions of the calendars ``keys``, and their numbers."""
    if not keys:
        return {}, {}
    latest = (
        select(CalendarVersion.key, func.max(CalendarVersion.version).label("version"))
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key.in_(sorted(keys)))
        .group_by(CalendarVersion.key)
        .subquery()
    )
    rows = await session.scalars(
        select(CalendarVersion)
        .join(
            latest,
            (CalendarVersion.key == latest.c.key) & (CalendarVersion.version == latest.c.version),
        )
        .where(CalendarVersion.tenant_id == tenant_id)
    )
    calendars: dict[str, Calendar] = {}
    versions: dict[str, int] = {}
    for version in rows:
        calendar = _calendars.get(version.id)
        if calendar is None:
            calendar = Calendar.from_spec(version.spec)
            _calendars[version.id] = calendar
        calendars[version.key] = calendar
        versions[version.key] = version.version
    return calendars, versions


@dataclass
class _Acting:
    """The process's authority for this step, or why it has none (CP-ADR-0074 §14)."""

    ctx: AuthContext | None
    refusal: DomainError | None


async def _acting(
    session: AsyncSession, row: ProcessDefinition, instance_id: uuid.UUID, trace_run_id: str
) -> _Acting:
    if row.identity_agent is None:  # pragma: no cover - the check refuses such a version
        return _Acting(None, AuthorizationError("The process has no identity", code="forbidden"))
    try:
        authority = await agent_authority(
            session, row.tenant_id, row.identity_agent, acting="the process"
        )
    except AuthorizationError as exc:
        return _Acting(None, exc)
    correlation = f"{CORRELATION_PREFIX}{instance_id}"
    iam = authority.get("iamPrincipalId")
    ctx = AuthContext(
        tenant_id=row.tenant_id,
        principal_id=uuid.UUID(str(authority["principalId"])),
        principal_kind=str(authority.get("principalKind") or "service"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=correlation,
        correlation_id=correlation,
        trace_run_id=trace_run_id,
        iam_principal_id=uuid.UUID(iam) if iam else None,
    )
    # The process acts as its agent, and a step may start or cancel a child
    # acting as another: every process agent's principal before any task row
    # (``lock_process_identities``, CP-ADR-0077 §3). Only the instance row is
    # held here, which ``principals/{id}:disable`` never takes.
    await lock_process_identities(session, ctx.tenant_id)
    try:
        await require_active_credential(
            session,
            authority=authority_snapshot(ctx),
            principal_id=ctx.principal_id,
            subject="the process acts with",
        )
    except AuthorizationError as exc:
        return _Acting(None, exc)
    return _Acting(ctx, None)


# --- taking an input ---------------------------------------------------------------


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def engine_time(instance: ProcessInstance | None, at: datetime) -> datetime:
    """The time of the next input: never before the instance's clock."""
    at = at.astimezone(UTC)
    if instance is None or not instance.state:
        return at
    clock = _time(instance.state.get("clock"))
    return max(at, clock) if clock is not None else at


def _excluded_principals(body: Mapping[str, Any]) -> list[str]:
    """``excludedPrincipals`` of a step as principal ids, in their canonical text.

    A value that is not a principal id — an empty one (``null``, ``""``) too —
    cannot be enforced by the core, and an exclusion is never dropped silently
    (CP-ADR-0074 §7): the step is refused. Only a step without
    ``separationOfDuties`` gives none.
    """
    if "excludedPrincipals" not in body:
        return []
    listed = body["excludedPrincipals"]
    if not isinstance(listed, list):
        raise ValidationError(
            "invalid_approval", "separationOfDuties must give a list of principal ids"
        )
    excluded: list[str] = []
    for value in listed:
        try:
            principal_id = uuid.UUID(str(value))
        except ValueError:
            raise ValidationError(
                "invalid_approval",
                "separationOfDuties names a value that is not a principal id",
                details={"value": str(value)[:200]},
            ) from None
        if str(principal_id) not in excluded:
            excluded.append(str(principal_id))
    return excluded


def _event_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def take(
    session: AsyncSession,
    instance: ProcessInstance,
    row: ProcessDefinition,
    kind: str,
    body: Mapping[str, Any],
    *,
    at: datetime,
    source_ref: str,
    actor_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    trace_run_id: str = "",
) -> bool:
    """One step of a locked instance; ``False`` when ``source_ref`` was taken before."""
    taken = await session.scalar(
        select(func.count())
        .select_from(ProcessInstanceEvent)
        .where(
            ProcessInstanceEvent.instance_id == instance.id,
            ProcessInstanceEvent.source_ref == source_ref,
        )
    )
    if taken:
        return False
    # The settings are read once a step, in its transaction (CP-ADR-0081 §6).
    scope = await settings_scope(session, row)
    definition = await definition_of(session, row, scope)
    seen = await snapshot(session, row.tenant_id, scope) if definition.reads_settings else None
    acting = await _acting(session, row, instance.id, trace_run_id)
    pending: list[tuple[str, Mapping[str, Any], str]] = [(kind, body, source_ref)]
    follow_ups = 0
    while pending:
        kind, body, ref = pending.pop(0)
        calendars, versions = await calendars_named(session, row.tenant_id, row.spec)
        given = engine.Input(
            kind,
            engine_time(instance, at),
            body,
            str(actor_id) if actor_id else None,
            calendars,
            settings=seen.values if seen is not None else None,
        )
        before = instance.state or None
        state, decisions, intents = engine.step(definition, before, given)
        seq = int(state["seq"])
        record = ProcessInstanceEvent(
            instance_id=instance.id,
            seq=seq,
            tenant_id=instance.tenant_id,
            at=given.at,
            kind=kind,
            source_ref=ref,
            event_id=event_id if ref == source_ref else None,
            actor_id=actor_id if ref == source_ref else None,
            input=given.out(),
            decisions=[decision.out() for decision in decisions],
            intents=[],
            calendars=versions,
            settings_version=seen.version if seen is not None else None,
            settings_schema_revision=seen.revision if seen is not None else None,
            created_at=utcnow(),
        )
        executor = _Executor(session, instance, row, acting, state, given.at, current=record)
        results = await executor.run(intents, input_kind=kind, input_body=body)
        record.intents = [
            {**intent.out(), "executed": result}
            for intent, result in zip(intents, results, strict=True)
        ]
        session.add(record)
        _store(instance, state)
        await session.flush()
        await _record_steps(session, instance, definition, before, record, acting)
        for index, failure in enumerate(executor.failures):
            follow_ups += 1
            if follow_ups > MAX_FOLLOW_UPS:
                logger.warning(
                    "process instance keeps refusing its intents; the rest are dropped",
                    extra={"instance_id": str(instance.id), "source_ref": source_ref},
                )
                break
            pending.append(("intent_failed", failure, f"{ref}/intent_failed/{seq}.{index}"))
    return True


async def _record_steps(
    session: AsyncSession,
    instance: ProcessInstance,
    definition: engine.Definition,
    before: Mapping[str, Any] | None,
    record: ProcessInstanceEvent,
    acting: _Acting,
) -> None:
    """``process.step_entered``/``step_exited`` of one step (CP-ADR-0074 §13, amendment).

    A projection of the journal record just written, not a decision of the
    engine: activities that appeared and disappeared in the step, with the
    tasks, approvals and calls the executed intents opened. The attempt
    counters are the application's (``step_attempts``), and so is the attempt
    each open activity entered with (``refs["activity:<id>"]``). A redelivered input
    never gets here — ``(instance, source_ref)`` was taken — so a step is
    reported once. The events act as the process, except a ``withdrawn``
    exit: its actor is the participant who cancelled the work, the actor of
    the input (``record.actor_id``).
    """
    projection = process_steps.step_events(
        definition,
        instance_id=str(instance.id),
        before=before,
        after=instance.state,
        decisions=record.decisions,
        given=record.input,
        at=record.at,
        attempts=instance.step_attempts or {},
        refs=instance.refs or {},
    )
    if projection.attempts != (instance.step_attempts or {}):
        instance.step_attempts = projection.attempts
    if projection.refs != (instance.refs or {}):
        instance.refs = projection.refs
    workspace = instance.workspace_id
    correlation = f"{CORRELATION_PREFIX}{instance.id}"
    process = acting.ctx.principal_id if acting.ctx else None
    for event in projection.events:
        await record_event(
            session,
            tenant_id=instance.tenant_id,
            event_type=event.type,
            entity_type="process_instance",
            entity_id=instance.id,
            actor_id=record.actor_id if process_steps.withdrawn(event) else process,
            request_id=correlation,
            correlation_id=correlation,
            trace_run_id=acting.ctx.trace_run_id if acting.ctx else None,
            payload={**event.payload, "workspaceId": str(workspace) if workspace else None},
        )


def _store(instance: ProcessInstance, state: dict[str, Any]) -> None:
    now = utcnow()
    instance.state = state
    instance.data = state.get("data") or {}
    instance.status = str(state["status"])
    instance.outcome = state.get("outcome")
    instance.error = state.get("error")
    instance.updated_at = now
    if instance.status in engine.CLOSED and instance.completed_at is None:
        instance.completed_at = now
    # The earliest running deadline and warning, for the slaState filter (CP-ADR-0078 §6):
    # a deadline whose timer is frozen counts no more than a closed instance does.
    due_at = warn_at = None
    if instance.status not in engine.CLOSED:
        due_at, warn_at = process_sla.open_moments(_deadlines(state), state.get("timers") or {})
    instance.sla_due_at = due_at
    instance.sla_warn_at = warn_at


def _deadlines(state: Mapping[str, Any]) -> list[Any]:
    """The deadline records of an instance: of the process and of its open steps."""
    activities = state.get("activities") or {}
    return [state.get("sla"), *(activity.get("sla") for activity in activities.values())]


# --- memory ------------------------------------------------------------------------

# The observation kind of what a ``remember`` step writes.
REMEMBER_KIND = "process.remembered"


def case_kind(spec: Mapping[str, Any]) -> str:
    """The kind of the case node the process projects (``memory.case.kind``)."""
    memory = spec.get("memory")
    case = memory.get("case") if isinstance(memory, dict) else None
    return str((case.get("kind") if isinstance(case, dict) else None) or "case")


def remembered(
    intent: Mapping[str, Any], kind_of_case: str, instance: ProcessInstance
) -> dict[str, Any]:
    """The observation of a ``remember`` intent: its content, data and assertions.

    ``facts`` become properties of the case node; an ``entity`` is a node with
    its ``links`` as facts to the nodes they name (CP-ADR-0076 §5). Keys are
    ``<kind>:<key>``, as memory resolves the anchors of a recall. The data
    names the case, the instance and the step — the reference to the case.
    """
    case = intent.get("case")
    assertions: list[dict[str, Any]] = []
    lines: list[str] = []
    facts = intent.get("facts")
    if isinstance(facts, dict) and facts:
        if case:
            assertions.append(
                {
                    "assert": "entity",
                    "entity": {
                        "key": entity_key(kind_of_case, str(case)),
                        "type": kind_of_case,
                        "properties": dict(facts),
                    },
                }
            )
        lines.append(
            "Facts of the case: " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in facts.items())
        )
    entity = intent.get("entity")
    if isinstance(entity, dict) and entity.get("key"):
        key = entity_key(str(entity["kind"]), str(entity["key"]))
        node: dict[str, Any] = {"key": key, "type": str(entity["kind"])}
        if entity.get("name"):
            node["title"] = str(entity["name"])
        if entity.get("text"):
            node["properties"] = {"text": entity["text"]}
        assertions.append({"assert": "entity", "entity": node})
        for link in entity.get("links") or ():
            if not link.get("key"):
                continue
            target = entity_key(str(link["kind"]), str(link["key"]))
            assertions.append(
                {"assert": "entity", "entity": {"key": target, "type": str(link["kind"])}}
            )
            assertions.append(
                {
                    "assert": "fact",
                    "fact": {"subject": key, "predicate": str(link["rel"]), "object": target},
                }
            )
        lines.append(str(entity.get("text") or entity.get("name") or key))
    data: dict[str, Any] = {
        "processInstanceId": str(instance.id),
        "process": instance.definition_key,
        "element": intent.get("element"),
    }
    if case:
        data["case"] = {"kind": kind_of_case, "key": str(case)}
    if intent.get("evidence"):
        data["evidence"] = list(intent["evidence"])
    return {
        "kind": REMEMBER_KIND,
        "content": "\n".join(lines) or f"Step {intent.get('element')} of {instance.definition_key}",
        "data": data,
        "assertions": assertions,
    }


# --- executing intents ---------------------------------------------------------------


def _refused(exc: DomainError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "detail": exc.message[:500]}


class _Executor:
    """Executes the intents of one step with the process's authority."""

    def __init__(
        self,
        session: AsyncSession,
        instance: ProcessInstance,
        row: ProcessDefinition,
        acting: _Acting,
        state: Mapping[str, Any],
        at: datetime,
        *,
        current: ProcessInstanceEvent | None = None,
    ) -> None:
        self.session = session
        self.instance = instance
        self.row = row
        self.acting = acting
        self.state = state
        self.at = at
        self.current = current  # the journal record of this step, not stored yet
        self.refs: dict[str, Any] = dict(instance.refs or {})
        self.failures: list[dict[str, Any]] = []

    @property
    def ctx(self) -> AuthContext:
        if self.acting.ctx is None:
            assert self.acting.refusal is not None
            raise self.acting.refusal
        return self.acting.ctx

    async def run(
        self,
        intents: Sequence[engine.Intent],
        *,
        input_kind: str,
        input_body: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for intent in intents:
            handler = getattr(self, f"do_{intent.kind}", None)
            if handler is None:  # pragma: no cover - every intent kind has a handler
                results.append({"ok": False, "code": "unknown_intent"})
                continue
            try:
                async with self.session.begin_nested():
                    result = await handler(dict(intent.body))
            except DependencyUnavailableError:
                raise
            except DomainError as exc:
                result = _refused(exc)
                if intent.kind in _OPENING:
                    self.failures.append(
                        {
                            "activityId": intent.body.get("activityId"),
                            "intent": intent.kind,
                            "code": exc.code,
                            "status": exc.http_status,
                            "detail": exc.message[:500],
                        }
                    )
                else:
                    logger.warning(
                        "process intent refused",
                        extra={
                            "instance_id": str(self.instance.id),
                            "intent": intent.kind,
                            "error_code": exc.code,
                        },
                    )
            results.append(result)
        if input_kind == "timer":
            await self.timer_taken(str(input_body.get("timerId") or ""))
        if input_kind == "approval":
            await self.next_approver(str(input_body.get("activityId") or ""))
        self.instance.refs = self.refs
        return results

    # --- helpers ---------------------------------------------------------------------

    @property
    def correlation(self) -> str:
        return f"{CORRELATION_PREFIX}{self.instance.id}"

    def activity_refs(self, activity_id: str, prefix: str) -> list[uuid.UUID]:
        return [
            uuid.UUID(ref.split(":", 1)[1])
            for ref, target in sorted(self.refs.items())
            if ref.startswith(prefix) and target.get("activity") == activity_id
        ]

    async def assignee(self, assign: Sequence[Mapping[str, Any]]) -> tuple[Any, list[str]]:
        """The first resolvable link of an assignment chain: a principal, an agent or a role.

        A chain with no resolvable link keeps its first one, so that the
        command refuses it with its own code (``unknown_agent``…).
        """
        for item in assign or ():
            try:
                return await self.link(item)
            except (DomainError, ValueError):
                continue
        if assign:
            principal = assign[0].get("principal")
            if principal:
                return principal, []
            if assign[0].get("agent"):
                return f"agent:{assign[0]['agent']}", []
            return None, [str(assign[0].get("role"))]
        return None, []

    async def link(self, item: Mapping[str, Any]) -> tuple[Any, list[str]]:
        await self.resolve(item, field="assign")
        if item.get("principal"):
            return uuid.UUID(str(item["principal"])), []
        if item.get("agent"):
            return f"agent:{item['agent']}", []
        return None, [str(item["role"])]

    async def resolve(
        self, item: Mapping[str, Any], *, field: str
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """A candidate of a chain as ``(principal, role)`` ids; raises when it names none.

        A principal of this tenant, the principal of an active agent, or a role
        of the instance's workspace (a tenant-wide one without it).
        """
        if item.get("principal"):
            principal_id = uuid.UUID(str(item["principal"]))
            found = await self.session.get(Principal, principal_id)
            if found is None or found.tenant_id != self.instance.tenant_id:
                raise ValueError("unknown principal")
            return principal_id, None
        if item.get("agent"):
            reference = f"agent:{item['agent']}"
            return await agent_principal(
                self.session, self.instance.tenant_id, reference, field=field
            ), None
        return None, await self.role_id(str(item.get("role")))

    async def role_id(self, slug: str) -> uuid.UUID:
        candidates = (
            await self.session.scalars(
                select(Role).where(
                    Role.tenant_id == self.instance.tenant_id,
                    Role.slug == slug,
                    or_(
                        Role.workspace_id.is_(None),
                        Role.workspace_id == self.instance.workspace_id,
                    ),
                )
            )
        ).all()
        if not candidates:
            raise ValidationError(
                "unknown_role", f"Role {slug!r} does not exist", details={"role": slug}
            )
        # The role of the instance's workspace before a tenant-wide one.
        return sorted(candidates, key=lambda r: r.workspace_id is None)[0].id

    # --- events and timers -----------------------------------------------------------

    async def do_emit_event(self, body: dict[str, Any]) -> dict[str, Any]:
        # The workspace is the instance's, not a decision of the engine: it is
        # added here and stays out of the journal the engine replays.
        workspace = self.instance.workspace_id
        payload = {
            **(body.get("payload") or {}),
            "workspaceId": str(workspace) if workspace else None,
        }
        if str(body["type"]).startswith(process_sla.SLA_EVENT_PREFIX):
            payload["attempt"] = process_steps.sla_attempt(
                payload, refs=self.refs, attempts=self.instance.step_attempts or {}
            )
        if body["type"] == engine.ESCALATED:
            payload.update(await self.escalation_addressees(payload.get("to")))
        addressees = body.get("addressees")
        if isinstance(addressees, Mapping):
            payload["owner"] = await self.addressee(addressees.get("owner") or ())
            if "assignee" in addressees:
                payload["assignee"] = await self.step_assignee(
                    payload.get("activityId"), addressees.get("assignee") or ()
                )
        await record_event(
            self.session,
            tenant_id=self.instance.tenant_id,
            event_type=str(body["type"]),
            entity_type="process_instance",
            entity_id=self.instance.id,
            actor_id=self.acting.ctx.principal_id if self.acting.ctx else None,
            request_id=self.correlation,
            correlation_id=self.correlation,
            trace_run_id=self.acting.ctx.trace_run_id if self.acting.ctx else None,
            payload=payload,
        )
        return {"ok": True}

    def address(
        self, *, principal_id: uuid.UUID | None = None, role_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """An addressee in the form of notification rules (CP-ADR-0078 §3).

        The workspace is the instance's: a workspace process's own, or the one
        a tenant process was started in.
        """
        workspace = self.instance.workspace_id
        return {
            "principalId": str(principal_id) if principal_id else None,
            "roleId": str(role_id) if role_id else None,
            "workspaceId": str(workspace) if workspace else None,
        }

    async def addressee(self, chain: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """The first resolvable candidate of a chain; ``None`` when none resolves.

        An unknown role, agent or principal is not an error of the event: its
        candidate is skipped, and a chain of such ones leaves the field empty.
        """
        for item in chain:
            try:
                principal_id, role_id = await self.resolve(item, field="addressee")
            except (DomainError, ValueError):
                continue
            return self.address(principal_id=principal_id, role_id=role_id)
        return None

    async def escalation_addressees(self, targets: Any) -> dict[str, Any]:
        """``addressees`` of ``process.escalated``: one per ``to`` target, in its order.

        A target is resolved on its own, as a candidate of a chain is: a
        principal of this tenant, the principal of an active agent, a role of
        the instance's workspace. One that does not resolve is ``null`` there,
        and ``unresolved`` says why; the event is recorded all the same
        (CP-ADR-0078, amendment 2026-09-30).
        """
        addressees: list[dict[str, Any] | None] = []
        unresolved: list[dict[str, Any]] = []
        for index, target in enumerate(targets if isinstance(targets, list) else ()):
            text = str(target) if target is not None else ""
            try:
                principal_id, role_id = await self.resolve(
                    engine.assignee_of_text(text), field="to"
                )
            except DomainError as exc:
                reason = exc.code
            except ValueError:
                reason = "unknown_principal"
            else:
                addressees.append(self.address(principal_id=principal_id, role_id=role_id))
                continue
            addressees.append(None)
            unresolved.append({"index": index, "target": text[:200], "reason": reason})
        return {"addressees": addressees, "unresolved": unresolved}

    async def step_assignee(
        self, activity_id: Any, chain: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        """Who a step waits for now: the assignee of its open task, else its chain, else
        its pending approver.

        The task may have been reassigned past the process, or its agent
        retired since: the chain is resolved only when the task has no
        assignee (a task for a role).
        """
        task = await self.open_task(str(activity_id)) if activity_id else None
        if task is not None and task.assignee_id is not None:
            return self.address(principal_id=task.assignee_id)
        return await self.addressee(chain) or await self.approver(activity_id)

    async def approver(self, activity_id: Any) -> dict[str, Any] | None:
        """Who a pending approval of an ``approve`` step waits for: its assignee."""
        if not activity_id:
            return None
        ids = self.activity_refs(str(activity_id), "approval:")
        if not ids:
            return None
        pending = await self.session.scalar(
            select(Approval)
            .where(Approval.id.in_(ids), Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.created_at, Approval.id)
            .limit(1)
        )
        if pending is None:
            return None
        return self.address(
            principal_id=pending.assigned_principal_id, role_id=pending.required_role_id
        )

    async def do_set_timer(self, body: dict[str, Any]) -> dict[str, Any]:
        timer_id = uuid.UUID(str(body["timerId"]))
        now = utcnow()
        timer = await self.session.get(ProcessTimer, timer_id)
        if timer is None:
            timer = ProcessTimer(
                id=timer_id,
                tenant_id=self.instance.tenant_id,
                instance_id=self.instance.id,
                created_at=now,
                fired_at=None,
            )
            self.session.add(timer)
        timer.element = str(body["element"])
        timer.timer_kind = str(body["timerKind"])
        timer.due_at = _time(body.get("dueAt"))
        timer.state = str(body["state"])
        timer.remaining_seconds = body.get("remainingSeconds")
        timer.remaining_unit = str(body.get("remainingUnit") or "wall")
        timer.reads = list(body.get("reads") or ())
        timer.provisional = bool(body.get("provisional"))
        timer.updated_at = now
        return {"ok": True}

    async def do_cancel_timer(self, body: dict[str, Any]) -> dict[str, Any]:
        timer = await self.session.get(ProcessTimer, uuid.UUID(str(body["timerId"])))
        if timer is not None and timer.state in ("pending", "frozen"):
            timer.state = "cancelled"
            timer.updated_at = utcnow()
        return {"ok": True}

    async def timer_taken(self, timer_id: str) -> None:
        """A timer the engine took does not fire again, whatever it decided."""
        try:
            timer = await self.session.get(ProcessTimer, uuid.UUID(timer_id))
        except ValueError:
            return
        if timer is not None and timer.state in ("pending", "frozen"):
            live = (self.state.get("timers") or {}).get(timer_id)
            if live is not None and live.get("state") == "frozen":
                return
            timer.state = "fired"
            timer.fired_at = utcnow()
            timer.updated_at = timer.fired_at

    # --- tasks -----------------------------------------------------------------------

    def description(self, body: Mapping[str, Any]) -> str:
        lines = [
            f"Step `{body.get('element')}` of process `{self.instance.definition_key}`"
            f" (instance `{self.instance.instance_key}`).",
        ]
        if body.get("input"):
            lines += ["", "Input:", "```json", json.dumps(body["input"], indent=2), "```"]
        return "\n".join(lines)

    async def do_create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        assignee, roles = await self.assignee(body.get("assign") or ())
        ref = str(body["externalRef"])
        task = await create_task(
            self.session,
            self.ctx,
            title=str(body.get("title") or body.get("element"))[:500],
            description=self.description(body),
            type_key=body.get("taskType"),
            assignee_id=assignee,
            workspace_id=self.instance.workspace_id,
            due_date=_time(body.get("due")),
            requirements=RequirementSpec(roles=roles) if roles else None,
            origin={"kind": "process", "ref": ref},
            assignee_field="assign",
            # Filled from the case (CP-ADR-0074 §7, amendment 2026-10-01) and
            # checked against the type's fieldSchema: a misfit is
            # custom_fields_invalid, the intent fails.
            custom_fields=dict(body.get("customFields") or {}) or None,
        )
        if body.get("context"):
            # The step's profile replaces the type's (CP-ADR-0076 §6).
            task.context_profile = dict(body["context"])
        await self.link_task(task, ref, str(body["activityId"]))
        self.refs[f"task:{task.id}"] = {
            "activity": body["activityId"],
            "element": body.get("element"),
        }
        return {"ok": True, "taskId": str(task.id)}

    async def link_task(self, task: Task, ref: str, activity_id: str) -> None:
        """``process/<instance>/<element>`` names the element's latest task (ADR-0047)."""
        now = utcnow()
        metadata = {"instanceId": str(self.instance.id), "activityId": activity_id}
        reference = await self.session.scalar(
            select(ExternalReference)
            .where(
                ExternalReference.tenant_id == self.instance.tenant_id,
                ExternalReference.external_system == EXTERNAL_SYSTEM,
                ExternalReference.external_type == EXTERNAL_TYPE,
                ExternalReference.external_id == ref,
            )
            .with_for_update()
        )
        if reference is None:
            reference = ExternalReference(
                id=new_uuid(),
                tenant_id=self.instance.tenant_id,
                entity_type="task",
                entity_id=task.id,
                external_system=EXTERNAL_SYSTEM,
                external_type=EXTERNAL_TYPE,
                external_id=ref,
                metadata_json=metadata,
                version=1,
                created_by=self.ctx.principal_id,
                created_at=now,
                updated_at=now,
            )
            self.session.add(reference)
        else:
            # The step ran again (a repeated stage, a retry): the reference
            # moves to the new task; the old one stays in the instance journal.
            reference.entity_id = task.id
            reference.metadata_json = metadata
            reference.version += 1
            reference.updated_at = now
        await self.session.flush()
        await record_event(
            self.session,
            tenant_id=self.instance.tenant_id,
            event_type="task.external_reference_added",
            entity_type="task",
            entity_id=task.id,
            actor_id=self.ctx.principal_id,
            request_id=self.correlation,
            correlation_id=self.correlation,
            trace_run_id=self.ctx.trace_run_id,
            payload={
                "externalReferenceId": str(reference.id),
                "externalSystem": EXTERNAL_SYSTEM,
                "externalType": EXTERNAL_TYPE,
                "externalId": ref,
            },
        )

    async def open_task(self, activity_id: str) -> Task | None:
        for task_id in self.activity_refs(activity_id, "task:"):
            task = await self.session.get(Task, task_id, populate_existing=True)
            if task is not None and task.completed_at is None:
                return task
        return None

    async def do_cancel_task(self, body: dict[str, Any]) -> dict[str, Any]:
        task = await self.open_task(str(body["activityId"]))
        if task is None or task.system_status_category in (
            WorkItemStatusCategory.TERMINAL_SUCCESS,
            WorkItemStatusCategory.TERMINAL_CANCELLED,
        ):
            return {"ok": True, "taskId": None}
        lifecycle = await lifecycle_of(self.session, task)
        target = next(
            (
                status
                for status in lifecycle.targets_from(task.status)
                if lifecycle.category_of(status) == WorkItemStatusCategory.TERMINAL_CANCELLED
            ),
            None,
        )
        if target is None:
            raise ConflictError(
                "task_not_cancellable",
                f"The lifecycle of the task has no cancelled status from {task.status!r}",
                details={"taskId": str(task.id)},
            )
        await update_task(
            self.session,
            self.ctx,
            task_ref=str(task.id),
            expected_version=task.version,
            status=target,
        )
        return {"ok": True, "taskId": str(task.id)}

    async def do_reassign_task(self, body: dict[str, Any]) -> dict[str, Any]:
        task = await self.open_task(str(body["activityId"]))
        if task is None:
            return {"ok": True, "taskId": None}
        assignee, roles = await self.assignee(body.get("assign") or ())
        await update_task(
            self.session,
            self.ctx,
            task_ref=str(task.id),
            expected_version=task.version,
            assignee_id=assignee,
            requirements=RequirementSpec(roles=roles) if roles else None,
        )
        return {"ok": True, "taskId": str(task.id)}

    async def do_update_task_due(self, body: dict[str, Any]) -> dict[str, Any]:
        """The due of a step's open task, counted again by a migration (CP-ADR-0074 §11)."""
        task = await self.open_task(str(body["activityId"]))
        if task is None:
            return {"ok": True, "taskId": None}
        due = _time(body.get("due"))
        if task.due_date != due:
            await update_task(
                self.session,
                self.ctx,
                task_ref=str(task.id),
                expected_version=task.version,
                due_date=due,
            )
        return {"ok": True, "taskId": str(task.id)}

    # --- approvals -------------------------------------------------------------------

    async def do_request_approvals(self, body: dict[str, Any]) -> dict[str, Any]:
        approvers = list(body.get("approvers") or ())
        if not approvers:
            raise ValidationError("invalid_approval", "The step names no approvers")
        excluded = _excluded_principals(body)
        excluded_ids = {uuid.UUID(p) for p in excluded}
        for approver in approvers if excluded_ids else ():
            # Checked for every approver before any approval is asked for: a
            # sequential step would otherwise fail at a later vote.
            try:
                principal_id = await self.approver_principal(approver)
            except ValueError:
                continue  # not a principal id: refused when its approval is asked for
            if principal_id in excluded_ids:
                raise ValidationError(
                    "invalid_approval",
                    "An approver of the step is excluded from deciding: nobody could decide",
                    details={
                        key: str(approver[key]) for key in ("principal", "agent") if key in approver
                    },
                )
        activity_id = str(body["activityId"])
        sequential = body.get("mode") == "sequential"
        first, rest = (approvers[:1], approvers[1:]) if sequential else (approvers, [])
        record: dict[str, Any] = {
            "element": body.get("element"),
            "pending": rest,
            "total": len(approvers),
            "cancelled": 0,
        }
        if excluded:
            # The exclusion holds for every approver of the step, the next ones
            # of a sequential step too; the owner is who hears when nobody may
            # decide (CP-ADR-0074 §7).
            record["excluded"] = excluded
            record["owner"] = await self.owner()
        self.refs[f"activity:{activity_id}"] = record
        ids = [await self.request_one(activity_id, body.get("element"), a) for a in first]
        return {"ok": True, "approvalIds": ids}

    async def owner(self) -> dict[str, Any] | None:
        """The process owner as an addressee: the first resolvable candidate of ``spec.owner``."""
        definition = await definition_of(self.session, self.row)
        return await self.addressee(engine.owner_chain(definition, self.state, self.at))

    async def approver_principal(self, approver: Mapping[str, Any]) -> uuid.UUID | None:
        """The principal an approver names (``principal`` or ``agent:<key>``); none for a role."""
        if approver.get("role"):
            return None
        if approver.get("agent"):
            return await agent_principal(
                self.session,
                self.instance.tenant_id,
                f"agent:{approver['agent']}",
                field="approvers",
            )
        return uuid.UUID(str(approver.get("principal")))

    async def request_one(self, activity_id: str, element: Any, approver: Mapping[str, Any]) -> str:
        role_id = None
        if approver.get("role"):
            role_id = await self.role_id(str(approver["role"]))
        principal_id = await self.approver_principal(approver)
        record = self.refs.get(f"activity:{activity_id}") or {}
        approval = await request_approval(
            self.session,
            self.ctx,
            workspace_id=self.instance.workspace_id,
            required_role_id=role_id,
            assigned_principal_id=principal_id,
            comment=(
                f"Step {element} of process {self.instance.definition_key}"
                f" (instance {self.instance.instance_key})"
            ),
            excluded_principals=[uuid.UUID(p) for p in record.get("excluded") or ()],
        )
        self.refs[f"approval:{approval.id}"] = {"activity": activity_id, "element": element}
        return str(approval.id)

    async def next_approver(self, activity_id: str) -> None:
        """A sequential step asks the next approver once a vote left it open."""
        record = self.refs.get(f"activity:{activity_id}")
        if not record or not record.get("pending"):
            return
        if activity_id not in (self.state.get("activities") or {}):
            return
        if self.state.get("status") != engine.RUNNING:
            return
        approver, *rest = record["pending"]
        self.refs[f"activity:{activity_id}"] = {**record, "pending": rest}
        try:
            async with self.session.begin_nested():
                await self.request_one(activity_id, record.get("element"), approver)
        except DomainError as exc:
            self.failures.append(
                {
                    "activityId": activity_id,
                    "intent": "request_approvals",
                    "code": exc.code,
                    "status": exc.http_status,
                    "detail": exc.message[:500],
                }
            )

    async def do_close_approvals(self, body: dict[str, Any]) -> dict[str, Any]:
        activity_id = str(body["activityId"])
        record = self.refs.get(f"activity:{activity_id}")
        if record:
            self.refs[f"activity:{activity_id}"] = {**record, "pending": []}
        closed = []
        for approval_id in self.activity_refs(activity_id, "approval:"):
            approval = await self.session.get(Approval, approval_id)
            if approval is None or approval.status != ApprovalStatus.PENDING:
                continue
            await cancel_approval(
                self.session, self.ctx, approval_id=approval_id, comment=body.get("reason")
            )
            closed.append(str(approval_id))
        return {"ok": True, "cancelled": closed}

    # --- skills, memory, nested processes --------------------------------------------

    async def journal(self) -> list[dict[str, Any]]:
        """The latest journal entries of the instance, this step's decisions included.

        A record holds one entry at least, so the latest records hold every
        entry a skill gets (:data:`process_replay.SKILL_JOURNAL_LIMIT`).
        """
        latest = (
            await self.session.scalars(
                select(ProcessInstanceEvent)
                .where(ProcessInstanceEvent.instance_id == self.instance.id)
                .order_by(ProcessInstanceEvent.seq.desc())
                .limit(process_replay.SKILL_JOURNAL_LIMIT)
            )
        ).all()
        rows = [*reversed(latest)]
        if self.current is not None:
            rows.append(self.current)
        return [entry for row in rows for entry in journal_entries(row)]

    async def do_invoke_skill(self, body: dict[str, Any]) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        if "journal" in (body.get("attach") or ()):
            entries = await self.journal()
        result = await invoke_skill(
            self.session,
            self.ctx,
            skill_ref=str(body["skill"]),
            inputs=process_replay.with_attachments(body, lambda: entries),
            idempotency_key=f"{CORRELATION_PREFIX}{self.instance.id}:{body['activityId']}",
            process=(self.instance, str(body["activityId"])),
        )
        invocation = result.invocation
        self.refs[f"skill:{invocation.id}"] = {
            "activity": body["activityId"],
            "element": body.get("element"),
        }
        return {"ok": True, "invocationId": str(invocation.id)}

    async def do_recall(self, body: dict[str, Any]) -> dict[str, Any]:
        """Queue the recall; the worker asks memory after this transaction (:func:`run_recall`)."""
        recall_id = uuid.UUID(str(body["recallId"]))
        if await self.session.get(ProcessRecall, recall_id) is None:
            now = utcnow()
            self.session.add(
                ProcessRecall(
                    id=recall_id,
                    tenant_id=self.instance.tenant_id,
                    instance_id=self.instance.id,
                    element=str(body.get("element")),
                    request=body,
                    state="pending",
                    attempts=0,
                    next_attempt_at=now,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                    answered_at=None,
                )
            )
        return {"ok": True, "recallId": str(recall_id)}

    async def do_remember(self, body: dict[str, Any]) -> dict[str, Any]:
        """An observation of the core with the process's authority (CP-ADR-0076 §5)."""
        written = remembered(body, case_kind(self.row.spec), self.instance)
        observation = await record_observation(
            self.session,
            self.ctx,
            kind=written["kind"],
            content=written["content"],
            data=written["data"],
            assertions=written["assertions"],
            workspace_id=self.instance.workspace_id,
            source=str(body["source"]),
            dedup_key=str(body["dedupKey"]),
            observed_at=self.at,
            external_ref={"system": EXTERNAL_SYSTEM, "id": f"process/{self.instance.id}"},
        )
        return {
            "ok": True,
            "observationId": str(observation.id),
            "deduplicated": observation.deduplicated,
        }

    async def do_start_child(self, body: dict[str, Any]) -> dict[str, Any]:
        process = str(body["process"])
        # A retired process starts no child either: the parent gets intent_failed.
        # The key is shared without waiting: a batch of the worker holds the
        # keys of the children it started until it commits, and an apply
        # holding this key would wait for one of them (catalog_key_busy).
        # The version is read under the key, not before it.
        await share_keys_now(self.session, self.instance.tenant_id, PROCESS, [process])
        row = await latest_definition(self.session, self.instance.tenant_id, process)
        if row is None:
            raise NotFoundError("Process definition not found", details={"process": process})
        if await retired_processes(self.session, self.instance.tenant_id, [row.key]):
            raise process_retired(row.key)
        activity_id = str(body["activityId"])
        child = await start_instance(
            self.session,
            row,
            key=f"{self.instance.id}/{activity_id}",
            data=dict(body.get("input") or {}),
            workspace_id=self.instance.workspace_id if row.workspace_id is None else None,
            actor_id=self.ctx.principal_id,
            parent=(self.instance.id, activity_id),
            trace_run_id=self.ctx.trace_run_id,
        )
        self.refs[f"child:{child.id}"] = {"activity": activity_id, "element": body.get("element")}
        return {"ok": True, "instanceId": str(child.id)}

    async def do_cancel_child(self, body: dict[str, Any]) -> dict[str, Any]:
        for child_id in self.activity_refs(str(body["activityId"]), "child:"):
            child = await self.session.scalar(
                select(ProcessInstance).where(ProcessInstance.id == child_id).with_for_update()
            )
            if child is None or child.status in engine.CLOSED:
                continue
            row = await self.session.get(ProcessDefinition, child.definition_id)
            assert row is not None
            await take(
                self.session,
                child,
                row,
                "command",
                {"action": "cancel", "reason": str(body.get("reason") or "parent")},
                at=utcnow(),
                source_ref=f"parent:{self.instance.id}:{body['activityId']}",
                actor_id=self.ctx.principal_id,
                trace_run_id=self.ctx.trace_run_id,
            )
        return {"ok": True}

    async def do_complete(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


# --- starting ----------------------------------------------------------------------


async def latest_definition(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> ProcessDefinition | None:
    row: ProcessDefinition | None = await session.scalar(
        select(ProcessDefinition)
        .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key)
        .order_by(ProcessDefinition.version.desc())
        .limit(1)
    )
    return row


async def _lock_instance_key(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, instance_key: str
) -> None:
    """Serialize starts of one (process, key): one instance per key (FR-013)."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"cp:process-instance:{tenant_id}:{key}:{instance_key}", 0)
            )
        )
    )


async def instance_by_key(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, instance_key: str
) -> ProcessInstance | None:
    row: ProcessInstance | None = await session.scalar(
        select(ProcessInstance)
        .where(
            ProcessInstance.tenant_id == tenant_id,
            ProcessInstance.definition_key == key,
            ProcessInstance.instance_key == instance_key,
        )
        .with_for_update()
    )
    return row


def _new_instance(
    row: ProcessDefinition,
    instance_key: str,
    workspace_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    parent: tuple[uuid.UUID, str] | None,
) -> ProcessInstance:
    now = utcnow()
    return ProcessInstance(
        id=new_uuid(),
        tenant_id=row.tenant_id,
        workspace_id=row.workspace_id or workspace_id,
        definition_id=row.id,
        definition_key=row.key,
        definition_version=row.version,
        instance_key=instance_key,
        status=engine.RUNNING,
        outcome=None,
        error=None,
        data={},
        state={},
        refs={},
        parent_instance_id=parent[0] if parent else None,
        parent_activity_id=parent[1] if parent else None,
        started_by=actor_id,
        started_at=now,
        updated_at=now,
        completed_at=None,
    )


async def start_instance(
    session: AsyncSession,
    row: ProcessDefinition,
    *,
    key: str,
    data: dict[str, Any],
    workspace_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    parent: tuple[uuid.UUID, str] | None = None,
    trace_run_id: str = "",
) -> ProcessInstance:
    """Start an instance without a trigger event: its key and data are given.

    A key that already has an instance is ``409 process_instance_exists``
    with the id of that instance — never a second instance.
    """
    await _lock_instance_key(session, row.tenant_id, row.key, key)
    existing = await instance_by_key(session, row.tenant_id, row.key, key)
    if existing is not None:
        raise ConflictError(
            "process_instance_exists",
            f"Process {row.key!r} already has an instance with key {key!r}",
            details={"instanceId": str(existing.id), "status": existing.status},
        )
    errors = sorted(
        Draft202012Validator(row.spec.get("data") or {}).iter_errors(data),
        key=lambda e: list(e.path),
    )
    if errors:
        raise ValidationError(
            "invalid_process_data",
            "data does not match the data schema of the process",
            details={
                "errors": [
                    {"path": "/" + "/".join(map(str, e.path)), "message": e.message}
                    for e in errors[:20]
                ]
            },
        )
    instance = _new_instance(row, key, workspace_id, actor_id, parent)
    session.add(instance)
    await session.flush()
    await take(
        session,
        instance,
        row,
        "start",
        {"instanceId": str(instance.id), "key": key, "data": data},
        at=utcnow(),
        source_ref="start",
        actor_id=actor_id,
        trace_run_id=trace_run_id,
    )
    return instance


async def start_process_instance(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    process: str,
    key: str,
    data: dict[str, Any] | None,
    workspace_id: uuid.UUID | None,
) -> ProcessInstance:
    """``POST /process-instances``: an instance started by an operator (TAI-ADR-0055)."""
    await authorize(ctx, Permission.PROCESSES_OPERATE)
    # The principals the step locks anyway come first, as in a journal batch
    # (CP-ADR-0077 §3, rules 1 and 3). An apply holds principals only
    # FOR KEY SHARE, so this is not what keeps the start out of a cycle with it.
    await lock_process_identities(session, ctx.tenant_id)
    # A retirement in flight has counted the open instances: this one waits for it.
    await share_keys(session, ctx.tenant_id, PROCESS, [process])
    # Read under the key: a start that waited for an apply or a publication
    # runs on the version it committed, not on the one seen before the wait.
    row = await latest_definition(session, ctx.tenant_id, process)
    if row is None:
        raise NotFoundError("Process definition not found", details={"process": process})
    if await retired_processes(session, ctx.tenant_id, [process]):
        raise process_retired(process)
    if row.workspace_id is not None:
        if workspace_id is not None and workspace_id != row.workspace_id:
            raise ValidationError(
                "invalid_workspace",
                "The process lives in another workspace: its instances live there too",
                details={"workspaceId": str(row.workspace_id)},
            )
        workspace_id = row.workspace_id
    if workspace_id is not None:
        from control_plane.application.commands.workspaces import require_active_workspace

        await authorize(ctx, Permission.PROCESSES_OPERATE, resource=process_scope(workspace_id))
        await require_active_workspace(session, ctx, workspace_id)
    return await start_instance(
        session,
        row,
        key=key,
        data=data or {},
        workspace_id=workspace_id,
        actor_id=ctx.principal_id,
        trace_run_id=ctx.trace_run_id,
    )


# --- operator commands -------------------------------------------------------------

_COMMAND_FROM = {
    "suspend": ("running",),
    "resume": ("suspended",),
    "cancel": ("running", "suspended"),
}


async def get_instance(
    session: AsyncSession,
    ctx: AuthContext,
    instance_id: uuid.UUID,
    permission: Permission = Permission.PROCESSES_READ,
    *,
    for_update: bool = False,
) -> ProcessInstance:
    await authorize(ctx, permission)
    stmt = select(ProcessInstance).where(
        ProcessInstance.id == instance_id, ProcessInstance.tenant_id == ctx.tenant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    instance: ProcessInstance | None = await session.scalar(stmt)
    # A workspace outside the caller's visibility answers exactly as a missing
    # row, never as a missing workspace (CP-ADR-0082 §3.7).
    if instance is None or (
        instance.workspace_id is not None and not ctx.sees_workspace(instance.workspace_id)
    ):
        raise NotFoundError("Process instance not found", details={"instanceId": str(instance_id)})
    if instance.workspace_id is not None:
        await authorize(ctx, permission, resource=process_scope(instance.workspace_id))
    return instance


async def command_instance(
    session: AsyncSession,
    ctx: AuthContext,
    instance_id: uuid.UUID,
    *,
    action: str,
    reason: str | None,
    compensate: bool = True,
) -> ProcessInstance:
    """``:suspend``, ``:resume``, ``:cancel`` of an operator: an input of the engine."""
    instance = await get_instance(
        session, ctx, instance_id, Permission.PROCESSES_OPERATE, for_update=True
    )
    if instance.status not in _COMMAND_FROM[action]:
        raise ConflictError(
            "invalid_process_instance_state",
            f"Cannot {action} an instance that is {instance.status}",
            details={"instanceId": str(instance.id), "status": instance.status},
        )
    if action == "cancel" and (instance.state or {}).get("closing") is not None:
        raise ConflictError(
            "invalid_process_instance_state",
            "The instance is already being cancelled",
            details={"instanceId": str(instance.id), "status": instance.status},
        )
    row = await session.get(ProcessDefinition, instance.definition_id)
    assert row is not None
    body: dict[str, Any] = {"action": action, "reason": reason or ""}
    if action == "cancel":
        body["compensate"] = compensate
    await take(
        session,
        instance,
        row,
        "command",
        body,
        at=utcnow(),
        source_ref=f"command:{new_uuid()}",
        actor_id=ctx.principal_id,
        trace_run_id=ctx.trace_run_id,
    )
    return instance


# --- reading -----------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceView:
    instance: ProcessInstance
    timers: list[ProcessTimer]


async def instance_view(session: AsyncSession, instance: ProcessInstance) -> InstanceView:
    timers = (
        await session.scalars(
            select(ProcessTimer)
            .where(
                ProcessTimer.instance_id == instance.id,
                ProcessTimer.state.in_(("pending", "frozen")),
            )
            .order_by(ProcessTimer.created_at, ProcessTimer.id)
        )
    ).all()
    return InstanceView(instance, list(timers))


def _sla_clock(instance: ProcessInstance) -> datetime:
    """The moment deadlines are read at.

    A closed instance is read at its close, so a step done in time stays in
    time (CP-ADR-0078 §6).
    """
    if instance.status in engine.CLOSED and instance.completed_at is not None:
        return instance.completed_at
    return utcnow()


def instance_sla(instance: ProcessInstance) -> tuple[dict[str, Any] | None, str]:
    """``sla`` and ``slaState`` of an instance: the worst of the process and its open steps."""
    now = _sla_clock(instance)
    state = instance.state or {}
    timers = state.get("timers") or {}
    due, process, _ = process_sla.shown(state.get("sla"), now=now, timers=timers)
    steps = [
        process_sla.shown(record, now=now, timers=timers)[1] for record in _deadlines(state)[1:]
    ]
    return due, process_sla.worst([process, *steps])


def open_elements(instance: ProcessInstance) -> list[dict[str, Any]]:
    """The activities the instance waits on, with the tasks and approvals they opened.

    Each carries its attempt and its deadline as of now (CP-ADR-0078 §6).
    """
    refs = instance.refs or {}
    now = _sla_clock(instance)
    state = instance.state or {}
    timers = state.get("timers") or {}
    out = []
    activities = state.get("activities") or {}
    for activity in sorted(activities.values(), key=lambda a: a["n"]):
        linked: dict[str, list[str]] = {"task": [], "approval": []}
        for ref, target in refs.items():
            kind, _, ident = ref.partition(":")
            if kind in linked and target.get("activity") == activity["id"]:
                linked[kind].append(ident)
        tasks = sorted(linked["task"])
        entered = refs.get(f"{process_steps.ACTIVITY_REF}{activity['id']}")
        attempt = entered.get("attempt") if isinstance(entered, Mapping) else None
        due, sla_state, overdue = process_sla.shown(activity.get("sla"), now=now, timers=timers)
        out.append(
            {
                "id": activity.get("element") or activity["id"],
                "kind": _ACTIVITY_STEP.get(activity["kind"], activity["kind"]),
                "since": activity.get("openedAt"),
                "taskId": tasks[-1] if tasks else None,
                "approvalIds": sorted(linked["approval"]),
                "attempt": int(attempt) if attempt is not None else None,
                "due": due,
                "slaState": sla_state,
                "overdueSeconds": overdue,
            }
        )
    return out


async def list_instances(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
    definition_key: str | None = None,
    instance_key: str | None = None,
    status: str | None = None,
    workspace_id: uuid.UUID | None = None,
    sla_state: str | None = None,
) -> tuple[list[ProcessInstance], tuple[datetime, uuid.UUID] | None]:
    """Instances the caller may read, newest first."""
    await authorize(ctx, Permission.PROCESSES_READ)
    stmt = select(ProcessInstance).where(ProcessInstance.tenant_id == ctx.tenant_id)
    workspaces = await visible_objects(ctx, Permission.PROCESSES_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                ProcessInstance.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                ProcessInstance.workspace_id.is_(None),
            )
        )
    if definition_key is not None:
        stmt = stmt.where(ProcessInstance.definition_key == definition_key)
    if instance_key is not None:
        stmt = stmt.where(ProcessInstance.instance_key == instance_key)
    if status is not None:
        stmt = stmt.where(ProcessInstance.status == status)
    if workspace_id is not None:
        stmt = stmt.where(ProcessInstance.workspace_id == workspace_id)
    if sla_state is not None:
        # The columns _store keeps (CP-ADR-0078 §6): null once closed and for a
        # deadline whose clock stands. Both predicates imply sla_due_at IS NOT NULL,
        # the partial index ix_process_instances_sla_due.
        now = utcnow()
        if sla_state == "breached":
            stmt = stmt.where(ProcessInstance.sla_due_at <= now)
        else:
            stmt = stmt.where(ProcessInstance.sla_due_at > now, ProcessInstance.sla_warn_at <= now)
    if after is not None:
        stmt = stmt.where(tuple_(ProcessInstance.started_at, ProcessInstance.id) < after)
    rows = list(
        (
            await session.scalars(
                stmt.order_by(ProcessInstance.started_at.desc(), ProcessInstance.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    following = None
    if len(rows) > limit:
        rows = rows[:limit]
        following = (rows[-1].started_at, rows[-1].id)
    return rows, following


def journal_entries(row: ProcessInstanceEvent) -> list[dict[str, Any]]:
    """One journal row as entries: the input, each decision, each intent."""
    return process_replay.journal_entries(
        seq=row.seq,
        at=row.at,
        kind=row.kind,
        source_ref=row.source_ref,
        actor_id=row.actor_id,
        event_id=row.event_id,
        given=row.input,
        calendars=row.calendars,
        decisions=row.decisions,
        intents=row.intents,
        settings_version=row.settings_version,
        settings_schema_revision=row.settings_schema_revision,
    )


async def instance_journal(
    session: AsyncSession,
    ctx: AuthContext,
    instance_id: uuid.UUID,
    *,
    limit: int,
    after: tuple[int, int] | None,
    kind: str | None = None,
) -> tuple[list[dict[str, Any]], tuple[int, int] | None]:
    """The decision journal, oldest first: ``(seq, index)`` positions entries of a step."""
    instance = await get_instance(session, ctx, instance_id)
    stmt = select(ProcessInstanceEvent).where(ProcessInstanceEvent.instance_id == instance.id)
    if after is not None:
        stmt = stmt.where(ProcessInstanceEvent.seq >= after[0])
    out: list[dict[str, Any]] = []
    following: tuple[int, int] | None = None
    batch = 200
    offset = 0
    while True:
        rows = (
            await session.scalars(
                stmt.order_by(ProcessInstanceEvent.seq).offset(offset).limit(batch)
            )
        ).all()
        for row in rows:
            for index, entry in enumerate(journal_entries(row)):
                if after is not None and (row.seq, index) <= after:
                    continue
                if kind is not None and entry["kind"] != kind:
                    continue
                if len(out) == limit:
                    return out, following
                out.append(entry)
                following = (row.seq, index)
        if len(rows) < batch:
            return out, None
        offset += batch


# --- the worker: journal events ------------------------------------------------------


async def due_process_tenants(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Tenants whose ``processes`` cursor has events to read and no backoff pending."""
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
        {"name": PROCESSES_CONSUMER, "limit": limit},
    )
    return [row[0] for row in rows]


def event_document(event: JournalEvent) -> dict[str, Any]:
    """A journal event as the engine reads it; an observation adds its kind and source."""
    payload = dict(event.payload or {})
    document: dict[str, Any] = {
        "id": str(event.id),
        "type": event.event_type,
        "time": event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "entityType": event.entity_type,
        "entityId": str(event.entity_id),
        "actorId": str(event.actor_id) if event.actor_id else None,
        "correlationId": event.correlation_id,
        "payload": payload,
    }
    if event.event_type == "observation.recorded":
        document["observation"] = payload.get("kind")
        if payload.get("source") is not None:
            document["source"] = payload.get("source")
    return document


@dataclass
class _Process:
    row: ProcessDefinition
    definition: engine.Definition
    since: datetime
    retired: bool = False


async def retired_processes(
    session: AsyncSession, tenant_id: uuid.UUID, keys: Iterable[str] | None = None
) -> frozenset[str]:
    """Retired keys (CP-ADR-0074 §11, Zh1): no new instances, open ones go on."""
    return await retired_keys(session, tenant_id, PROCESS, keys)


def process_retired(process: str) -> ConflictError:
    return ConflictError(
        "process_retired",
        f"Process {process!r} is retired: it starts no new instances",
        details={"process": process},
    )


async def _published(session: AsyncSession, tenant_id: uuid.UUID) -> list[_Process]:
    """The latest version of every process, with the time its first version appeared."""
    latest = (
        select(
            ProcessDefinition.key,
            func.max(ProcessDefinition.version).label("version"),
            func.min(ProcessDefinition.created_at).label("since"),
        )
        .where(ProcessDefinition.tenant_id == tenant_id)
        .group_by(ProcessDefinition.key)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ProcessDefinition, latest.c.since)
            .join(
                latest,
                (ProcessDefinition.key == latest.c.key)
                & (ProcessDefinition.version == latest.c.version),
            )
            .where(ProcessDefinition.tenant_id == tenant_id)
            .order_by(ProcessDefinition.key)
        )
    ).all()
    retired = await retired_processes(session, tenant_id)
    out = []
    for row, since in rows:
        try:
            definition = await definition_of(session, row)
        except DomainError:
            logger.warning(
                "process definition cannot run", extra={"process": f"{row.key}@{row.version}"}
            )
            continue
        out.append(_Process(row, definition, since, row.key in retired))
    return out


async def _routing_settings(session: AsyncSession, process: _Process) -> dict[str, Any] | None:
    """The settings a ``where`` or a key of a trigger reads, when the process reads them."""
    if not process.definition.reads_settings:
        return None
    seen = await snapshot(session, process.row.tenant_id, process.definition.settings)
    return seen.values if seen is not None else {}


def _own(instance: ProcessInstance, event: JournalEvent) -> bool:
    """An event the instance wrote itself never comes back to it as a trigger."""
    return event.correlation_id == f"{CORRELATION_PREFIX}{instance.id}"


def _outside_workspace(row: ProcessDefinition, event: JournalEvent) -> bool:
    if row.workspace_id is None:
        return False
    named = (event.payload or {}).get("workspaceId")
    return isinstance(named, str) and named != str(row.workspace_id)


async def _routed_by_refs(session: AsyncSession, tenant_id: uuid.UUID, ref: str) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(ProcessInstance.id).where(
            ProcessInstance.tenant_id == tenant_id,
            ProcessInstance.refs.has_key(ref),
        )
    )
    return list(rows.all())


async def _locked(session: AsyncSession, instance_id: uuid.UUID) -> ProcessInstance | None:
    row: ProcessInstance | None = await session.scalar(
        select(ProcessInstance)
        .where(ProcessInstance.id == instance_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row


async def _answer(
    session: AsyncSession, event: JournalEvent
) -> tuple[str, str, dict[str, Any]] | None:
    """The engine input an event answers with, as ``(ref, kind, body)`` without activityId."""
    kind = event.event_type
    if kind in ("task.completed", "task.updated"):
        task = await session.get(Task, event.entity_id, populate_existing=True)
        if task is None:
            return None
        if kind == "task.completed" or (
            task.system_status_category == WorkItemStatusCategory.TERMINAL_SUCCESS
            and (event.payload or {}).get("status") is not None
        ):
            status = "completed"
        elif task.system_status_category == WorkItemStatusCategory.TERMINAL_CANCELLED and (
            event.payload or {}
        ).get("status"):
            status = "cancelled"
        else:
            return None
        return (
            f"task:{task.id}",
            "task",
            {
                "status": status,
                "task": {
                    "id": str(task.id),
                    "publicId": task.public_id,
                    "status": task.status,
                    "customFields": dict(task.custom_fields or {}),
                    "assigneeId": str(task.assignee_id) if task.assignee_id else None,
                },
            },
        )
    if kind in _APPROVAL_OUTCOMES:
        approval = await session.get(Approval, event.entity_id, populate_existing=True)
        if approval is None:
            return None
        principal = approval.decision_by_principal_id or approval.assigned_principal_id
        return (
            f"approval:{approval.id}",
            "approval",
            {
                "approvalId": str(approval.id),
                "outcome": _APPROVAL_OUTCOMES[kind],
                "principal": str(principal) if principal else None,
            },
        )
    if kind in _SKILL_ENDS:
        invocation = await session.get(SkillInvocation, event.entity_id, populate_existing=True)
        if invocation is None:
            return None
        if invocation.status == SkillInvocationStatus.SUCCEEDED:
            body: dict[str, Any] = {"status": "succeeded", "output": invocation.output or {}}
        elif invocation.status in (SkillInvocationStatus.FAILED, SkillInvocationStatus.CANCELLED):
            error = dict(invocation.error or {})
            body = {
                "status": "failed",
                "error": {
                    "code": error.get("code") or f"skill_{invocation.status}",
                    "message": error.get("message"),
                },
            }
        else:
            return None  # a failed attempt that will be retried
        return f"skill:{invocation.id}", "skill", body
    if kind in _CHILD_ENDS and event.entity_type == "process_instance":
        child = await session.get(ProcessInstance, event.entity_id, populate_existing=True)
        if child is None:
            return None
        return (
            f"child:{child.id}",
            "child",
            {
                "status": _CHILD_ENDS[kind],
                "outcome": child.outcome,
                "data": dict(child.data or {}),
                "error": child.error,
            },
        )
    return None


def _ref_of(event: JournalEvent) -> str | None:
    """The ``refs`` key an event may answer: its entity, if it is of a kind instances open."""
    kind = event.event_type
    if kind in ("task.completed", "task.updated"):
        return f"task:{event.entity_id}"
    if kind in _APPROVAL_OUTCOMES:
        return f"approval:{event.entity_id}"
    if kind in _SKILL_ENDS:
        return f"skill:{event.entity_id}"
    if kind in _CHILD_ENDS and event.entity_type == "process_instance":
        return f"child:{event.entity_id}"
    return None


async def _deliver_answer(session: AsyncSession, event: JournalEvent, *, trace_run_id: str) -> int:
    ref = _ref_of(event)
    if ref is None:
        return 0
    routed = await _routed_by_refs(session, event.tenant_id, ref)
    if not routed:
        return 0  # nobody waits for this entity: the common case, one indexed lookup
    answer = await _answer(session, event)
    if answer is None:
        return 0
    _, kind, body = answer
    taken = 0
    for instance_id in routed:
        instance = await _locked(session, instance_id)
        if instance is None:
            continue
        target = (instance.refs or {}).get(ref) or {}
        activity_id = target.get("activity")
        given = {**body, "activityId": activity_id}
        if kind == "approval":
            record = (instance.refs or {}).get(f"activity:{activity_id}") or {}
            if body["outcome"] == "cancelled":
                if event.correlation_id == f"{CORRELATION_PREFIX}{instance.id}":
                    continue  # the instance closed it itself
                record = {**record, "cancelled": int(record.get("cancelled") or 0) + 1}
                instance.refs = {**instance.refs, f"activity:{activity_id}": record}
            given["total"] = int(record.get("total") or 1) - int(record.get("cancelled") or 0)
        elif kind == "task" and body["status"] == "cancelled":
            if event.correlation_id == f"{CORRELATION_PREFIX}{instance.id}":
                continue
        row = await session.get(ProcessDefinition, instance.definition_id)
        assert row is not None
        taken += await take(
            session,
            instance,
            row,
            kind,
            given,
            at=event.occurred_at,
            source_ref=f"event:{event.id}",
            actor_id=event.actor_id,
            event_id=event.id,
            trace_run_id=trace_run_id,
        )
    return taken


async def _deliver_calendar(
    session: AsyncSession, event: JournalEvent, processes: list[_Process], *, trace_run_id: str
) -> int:
    """A new calendar version moves the timers of the running instances that use it."""
    key = (event.payload or {}).get("key")
    keys = [p.row.key for p in processes if key in references(p.row.spec).calendars]
    if not keys:
        return 0
    ids = (
        await session.scalars(
            select(ProcessInstance.id).where(
                ProcessInstance.tenant_id == event.tenant_id,
                ProcessInstance.definition_key.in_(keys),
                ProcessInstance.status.in_((engine.RUNNING, engine.SUSPENDED)),
            )
        )
    ).all()
    taken = 0
    for instance_id in ids:
        instance = await _locked(session, instance_id)
        if instance is None or instance.status in engine.CLOSED:
            continue
        row = await session.get(ProcessDefinition, instance.definition_id)
        assert row is not None
        if key not in references(row.spec).calendars:
            continue
        taken += await take(
            session,
            instance,
            row,
            "calendar",
            {"key": key},
            at=event.occurred_at,
            source_ref=f"event:{event.id}",
            event_id=event.id,
            trace_run_id=trace_run_id,
        )
    return taken


async def _deliver_triggers(
    session: AsyncSession, event: JournalEvent, processes: list[_Process], *, trace_run_id: str
) -> int:
    """``start`` and ``correlate`` of every process: a new instance, or an input of one."""
    document = event_document(event)
    taken = 0
    for process in processes:
        if event.occurred_at < process.since or _outside_workspace(process.row, event):
            continue
        settings = await _routing_settings(session, process)
        started = engine.start_key(process.definition, document, settings)
        keys = engine.correlation_keys(process.definition, document, settings)
        # A retired process starts nothing; the start event of an existing key
        # still reaches its instance, like a correlation.
        if (
            started is not None
            and process.retired
            and await instance_by_key(session, event.tenant_id, process.row.key, started) is None
        ):
            started = None
        if started is None and not keys:
            continue
        if started is not None:
            await _lock_instance_key(session, event.tenant_id, process.row.key, started)
            instance = await instance_by_key(session, event.tenant_id, process.row.key, started)
            if instance is not None and _own(instance, event):
                continue
            if instance is None:
                instance = _new_instance(process.row, started, None, event.actor_id, parent=None)
                session.add(instance)
                await session.flush()
            row = await session.get(ProcessDefinition, instance.definition_id)
            assert row is not None
            taken += await take(
                session,
                instance,
                row,
                "start",
                {"instanceId": str(instance.id), "event": document},
                at=event.occurred_at,
                source_ref=f"event:{event.id}",
                actor_id=event.actor_id,
                event_id=event.id,
                trace_run_id=trace_run_id,
            )
        for key in keys:
            if key == started:
                continue  # the start input already correlated it
            instance = await instance_by_key(session, event.tenant_id, process.row.key, key)
            if instance is None or instance.status in engine.CLOSED or _own(instance, event):
                continue
            row = await session.get(ProcessDefinition, instance.definition_id)
            assert row is not None
            taken += await take(
                session,
                instance,
                row,
                "event",
                {"event": document},
                at=event.occurred_at,
                source_ref=f"event:{event.id}",
                actor_id=event.actor_id,
                event_id=event.id,
                trace_run_id=trace_run_id,
            )
    return taken


async def process_tenant_events(
    session: AsyncSession, *, tenant_id: uuid.UUID, batch_size: int, trace_run_id: str
) -> int:
    """One batch of a tenant's journal into its instances, and the cursor after it.

    The cursor row is locked (``SKIP LOCKED``): two workers never feed one
    tenant side by side. Every event goes through its own savepoint; a
    domain refusal (a definition that no longer runs) is logged and passed,
    anything else rolls the batch back and the tenant is retried with backoff
    (``catalog_key_busy`` — soon and without it, :func:`defer_tenant`).
    """
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == PROCESSES_CONSUMER,
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
    # One transaction feeds many instances, and a task an earlier input locks
    # stays locked until the commit: every principal the processes may act as
    # or name, and the actors of the batch's events (an instance an event
    # starts records its actor as ``started_by``), first.
    await lock_process_identities(session, tenant_id, [event.actor_id for event in events])
    processes = await _published(session, tenant_id)
    taken = 0
    for event in events:
        try:
            async with session.begin_nested():
                taken += await _deliver_answer(session, event, trace_run_id=trace_run_id)
                if event.event_type == "calendar.published":
                    taken += await _deliver_calendar(
                        session, event, processes, trace_run_id=trace_run_id
                    )
                taken += await _deliver_triggers(
                    session, event, processes, trace_run_id=trace_run_id
                )
        except DependencyUnavailableError:
            raise
        except DomainError as exc:
            logger.warning(
                "process input refused",
                extra={"event_id": str(event.id), "error_code": exc.code, "tenant": str(tenant_id)},
            )
    last = events[-1]
    cursor.tx_id = last.tx_id
    cursor.sequence = last.sequence
    cursor.updated_at = utcnow()
    cursor.failure_count = 0
    cursor.next_attempt_at = None
    cursor.parked_at = None
    cursor.parked_reason = None
    metadata = dict(cursor.metadata_ or {})
    metadata["inputs_total"] = int(metadata.get("inputs_total", 0)) + taken
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
    """Hold the tenant's cursor back with a growing delay; the batch is read again."""
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == PROCESSES_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if cursor is None:  # pragma: no cover - created with the first process
        return
    cursor.failure_count += 1
    cursor.parked_at = utcnow()
    cursor.parked_reason = error[:2000]
    delay = min(backoff_base_seconds * (2 ** (cursor.failure_count - 1)), backoff_max_seconds)
    cursor.next_attempt_at = utcnow() + timedelta(seconds=delay)
    cursor.updated_at = utcnow()


async def defer_tenant(session: AsyncSession, *, tenant_id: uuid.UUID, seconds: float) -> None:
    """Read the tenant's batch again shortly: a key it needs is held for a while.

    Not a failure: the delay does not grow and ``failure_count`` and
    ``parked_*`` stay as they are (``catalog_key_busy``, CP-ADR-0074, amendment
    2026-09-29).
    """
    cursor: EventConsumerCursor | None = await session.scalar(
        select(EventConsumerCursor)
        .where(
            EventConsumerCursor.name == PROCESSES_CONSUMER,
            EventConsumerCursor.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if cursor is None:  # pragma: no cover - created with the first process
        return
    cursor.next_attempt_at = utcnow() + timedelta(seconds=seconds)
    cursor.updated_at = utcnow()


# --- the worker: timers --------------------------------------------------------------


async def due_timers(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Pending timers whose moment has come, the earliest first."""
    rows = await session.scalars(
        select(ProcessTimer.id)
        .where(ProcessTimer.state == "pending", ProcessTimer.due_at <= utcnow())
        .order_by(ProcessTimer.due_at, ProcessTimer.id)
        .limit(limit)
    )
    return list(rows.all())


async def fire_timer(session: AsyncSession, timer_id: uuid.UUID, *, trace_run_id: str) -> bool:
    """The input ``timer`` of one due timer; ``False`` when it is not due or its instance is busy.

    The instance is locked first (``SKIP LOCKED``), as every step locks it,
    then the timer is read again: a step that moved or cancelled it in the
    meantime wins.
    """
    instance_id = await session.scalar(
        select(ProcessTimer.instance_id).where(ProcessTimer.id == timer_id)
    )
    if instance_id is None:
        return False
    instance: ProcessInstance | None = await session.scalar(
        select(ProcessInstance)
        .where(ProcessInstance.id == instance_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if instance is None:
        return False
    timer = await session.get(ProcessTimer, timer_id, populate_existing=True)
    now = utcnow()
    if timer is None or timer.state != "pending" or timer.due_at is None or timer.due_at > now:
        return False
    row = await session.get(ProcessDefinition, instance.definition_id)
    assert row is not None
    return await take(
        session,
        instance,
        row,
        "timer",
        # When the core noticed the timer: a breached deadline says it (CP-ADR-0078 §3).
        {"timerId": str(timer.id), "detectedAt": now.isoformat().replace("+00:00", "Z")},
        at=timer.due_at,
        source_ref=f"timer:{timer.id}",
        trace_run_id=trace_run_id,
    )


# --- the worker: recalls (CP-ADR-0076 §4) ----------------------------------------------


async def due_recalls(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Pending recalls whose next attempt has come, the oldest first."""
    rows = await session.scalars(
        select(ProcessRecall.id)
        .where(ProcessRecall.state == "pending", ProcessRecall.next_attempt_at <= utcnow())
        .order_by(ProcessRecall.next_attempt_at, ProcessRecall.id)
        .limit(limit)
    )
    return list(rows.all())


def _recall_lease(settings: Settings) -> timedelta:
    """How long an attempt holds a recall: past the deadline of one memory read."""
    return timedelta(seconds=3 * settings.context_timeout_seconds + 5)


def _recall_backoff(settings: Settings, attempts: int) -> timedelta:
    seconds = settings.outbox_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, settings.outbox_backoff_max_seconds))


async def begin_recall(
    session: AsyncSession,
    recall_id: uuid.UUID,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> Any:
    """Transactional half of one attempt: the call to make, or ``None``.

    The row is taken ``SKIP LOCKED`` and leased (``next_attempt_at`` moves past
    the attempt), so a crashed attempt is retried and two workers do not ask
    twice. A step that no longer waits for the recall — its timeout fired, it
    was cancelled, the instance closed — closes the row instead. Namespaces
    and visibility are the process identity's (CP-ADR-0076 §1).
    """
    row = await session.scalar(
        select(ProcessRecall)
        .where(
            ProcessRecall.id == recall_id,
            ProcessRecall.state == "pending",
            ProcessRecall.next_attempt_at <= utcnow(),
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    now = utcnow()
    row.updated_at = now
    instance = await session.get(ProcessInstance, row.instance_id)
    activities = ((instance.state if instance else None) or {}).get("activities") or {}
    if instance is None or instance.status in engine.CLOSED or str(row.id) not in activities:
        row.state = "closed"
        return None
    row.attempts += 1
    definition = await session.get(ProcessDefinition, instance.definition_id)
    assert definition is not None
    acting = await _acting(session, definition, instance.id, trace_run_id)
    try:
        if acting.ctx is None:
            assert acting.refusal is not None
            raise acting.refusal
        # Recall reads durable memory: the same right as POST /context/recall.
        await authorize(acting.ctx, Permission.EVENTS_READ)
        scope = await graph_scope(session, acting.ctx, settings, instance.workspace_id)
    except DomainError as exc:
        # The identity may get its right back before the step times out.
        row.last_error = f"{exc.code}: {exc.message}"[:500]
        row.next_attempt_at = now + _recall_backoff(settings, row.attempts)
        return None
    row.next_attempt_at = now + _recall_lease(settings)
    return process_recall_call(dict(row.request), scope)


async def finish_recall(
    session: AsyncSession,
    recall_id: uuid.UUID,
    outcome: dict[str, Any] | ProcessRecallFailed,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> bool:
    """Transactional half after memory: the instance's ``recall`` input, or a later attempt.

    A memory that did not answer is asked again with backoff (the step's own
    timeout ends the wait); a request memory rejected is the step's timeout
    now, with the reason. An answer that comes after the step stopped waiting
    is recorded in the journal all the same: the engine ignores it as stale.
    """
    row = await session.scalar(
        select(ProcessRecall).where(ProcessRecall.id == recall_id).with_for_update()
    )
    if row is None or row.state != "pending":
        return False
    now = utcnow()
    row.updated_at = now
    if isinstance(outcome, ProcessRecallFailed) and outcome.retryable:
        row.last_error = str(outcome)[:500]
        row.next_attempt_at = now + _recall_backoff(settings, row.attempts)
        return False
    instance = await _locked(session, row.instance_id)
    if instance is None:
        row.state = "closed"
        return False
    body: dict[str, Any] = {"activityId": str(row.id)}
    if isinstance(outcome, ProcessRecallFailed):
        row.last_error = str(outcome)[:500]
        body.update(status="timed_out", reason=outcome.reason)
    else:
        body.update(status="completed", result=outcome)
    definition = await session.get(ProcessDefinition, instance.definition_id)
    assert definition is not None
    taken = await take(
        session,
        instance,
        definition,
        "recall",
        body,
        at=now,
        source_ref=f"recall:{row.id}",
        trace_run_id=trace_run_id,
    )
    row.state = "answered"
    row.answered_at = now
    return taken


async def run_recall(
    session_factory: async_sessionmaker[AsyncSession],
    provider: GraphProvider | None,
    settings: Settings,
    recall_id: uuid.UUID,
    *,
    trace_run_id: str = "",
) -> bool:
    """One attempt of one recall: resolve, ask memory outside any transaction, answer."""
    async with transaction(session_factory) as session:
        call = await begin_recall(session, recall_id, settings, trace_run_id=trace_run_id)
    if call is None:
        return False
    outcome: dict[str, Any] | ProcessRecallFailed
    try:
        outcome = await fetch_process_recall(call, provider, settings, trace_run_id=trace_run_id)
    except ProcessRecallFailed as exc:
        outcome = exc
    async with transaction(session_factory) as session:
        return await finish_recall(session, recall_id, outcome, settings, trace_run_id=trace_run_id)


# --- replay (CP-ADR-0076 §4, SC-011) ---------------------------------------------------


async def journal_records(session: AsyncSession, instance_id: uuid.UUID) -> list[dict[str, Any]]:
    """The journal of an instance as :func:`process_replay.replay` reads it."""
    rows = await session.scalars(
        select(ProcessInstanceEvent)
        .where(ProcessInstanceEvent.instance_id == instance_id)
        .order_by(ProcessInstanceEvent.seq)
    )
    return [
        {
            "seq": row.seq,
            "input": row.input,
            "decisions": row.decisions,
            "intents": row.intents,
            "calendars": row.calendars,
            "settingsVersion": row.settings_version,
            "settingsSchemaRevision": row.settings_schema_revision,
        }
        for row in rows
    ]


async def journal_calendars(
    session: AsyncSession, tenant_id: uuid.UUID, entries: Sequence[Mapping[str, Any]]
) -> Callable[[Mapping[str, int]], dict[str, Calendar]]:
    """The calendar versions the entries of a journal were computed on, as a replay asks them."""
    wanted = {(k, int(v)) for e in entries for k, v in (e["calendars"] or {}).items()}
    versions: dict[tuple[str, int], Calendar] = {}
    if wanted:
        found = await session.scalars(
            select(CalendarVersion).where(
                CalendarVersion.tenant_id == tenant_id,
                tuple_(CalendarVersion.key, CalendarVersion.version).in_(sorted(wanted)),
            )
        )
        versions = {(c.key, c.version): Calendar.from_spec(c.spec) for c in found}

    def calendars(named: Mapping[str, int]) -> dict[str, Calendar]:
        return {k: versions[(k, int(v))] for k, v in named.items() if (k, int(v)) in versions}

    return calendars


def settings_pairs(entries: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    """The pairs ``(version, schema revision)`` the records of a journal name."""
    return [
        (int(e["settingsVersion"]), int(e["settingsSchemaRevision"]))
        for e in entries
        if e.get("settingsVersion") is not None and e.get("settingsSchemaRevision") is not None
    ]


async def journal_settings(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    definition: engine.Definition,
    entries: Sequence[Mapping[str, Any]],
) -> History:
    """The saved versions and schema revisions the records of a journal name (CP-ADR-0081 §6).

    The package is the one of the version run: ``settings`` of its records are its settings.
    """
    return await history(session, tenant_id, definition.settings.package, settings_pairs(entries))


async def revisions_of(
    entries: Sequence[Mapping[str, Any]],
    typed: Callable[[int], Awaitable[engine.Definition | None]],
) -> dict[int, engine.Definition]:
    """The definition typed by each schema revision the records name, as a replay asks them."""
    out: dict[int, engine.Definition] = {}
    for _, revision in settings_pairs(entries):
        if revision not in out:
            found = await typed(revision)
            if found is not None:
                out[revision] = found
    return out


async def replay_instance(
    session: AsyncSession, instance: ProcessInstance
) -> process_replay.Replay:
    """Replay an instance on its own version from its journal alone.

    Memory is not asked: the answers of its recalls are inputs of the journal.
    The replayed state is compared with the stored one as a last discrepancy
    (``field: state``).
    """
    row = await session.get(ProcessDefinition, instance.definition_id)
    assert row is not None
    definition = await definition_of(session, row)
    entries = await journal_records(session, instance.id)
    calendars = await journal_calendars(session, instance.tenant_id, entries)
    settings = await journal_settings(session, instance.tenant_id, definition, entries)

    async def typed(revision: int) -> engine.Definition | None:
        scope = settings.scope(revision)
        if scope is None:
            return None
        try:
            return await definition_of(session, row, scope)
        except ConflictError:
            return None  # the version does not pass with that revision: its own types

    result = process_replay.replay(
        definition,
        entries,
        calendars,
        settings=settings.values,
        definitions=await revisions_of(entries, typed),
    )
    if not result.discrepancies and result.state != (instance.state or None):
        result.discrepancies.append(
            process_replay.Discrepancy(len(entries), "state", instance.state, result.state)
        )
    return result
