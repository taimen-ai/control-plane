"""The sandbox of a package test: the engine run with its intents kept in memory (CP-ADR-0074 §10).

A test (``tests/<name>.test.yaml``, package-sdk ``schema/v1/test.schema.json``)
drives instances of a package's process through :func:`process_engine.step` —
the same function a live instance runs (FR-025) — and the sandbox executes
what the engine asks for:

- **tasks, approvals, timers** are objects of the sandbox; a test step
  ``complete`` finishes a task, ``approve`` decides an approval (the core's
  refusals included: ``separation_of_duties_violation``, ``not_eligible``);
- **skills, agents, recall** are answered by the test's ``mocks``; the output
  of a skill mock is checked against the output schema of the skill from the
  catalog, a recall mock against the form of a memory answer — a mock that
  does not fit fails the test. A call without a mock stays unanswered, as a
  skill that has not answered yet;
- **remember** and ``process.*`` events are recorded for ``expect``: the
  engine's (``process.sla_*`` among them, with the attempt the core counts)
  and the step events ``process.step_*``, projected from every step by
  :func:`process_steps.step_events` as ``take()`` projects them, with the
  refs of the tasks, approvals and nested instances the sandbox opened;
- **deadlines** are the engine's: the timers ``sla``/``sla_warning`` fire on
  ``advance`` by the calendars of the world (the package's over the
  catalog's); ``expect.sla`` reads them as the projection of an instance
  does (:func:`process_sla.shown`) at the test's clock (CP-ADR-0078 §7);
- **nested processes** (``call: {process}``) run in the same sandbox;
- **time** is virtual: ``advance: P3D`` fires the timers that fall due in
  order, each at its own moment;
- **rules that close the task an observation is bound to** (``WorkRule``
  with ``target: task``, CP-ADR-0063 amendment Zh5): ``emit: {observation,
  task: <step>}`` binds the observation to the newest task of that step, as
  the ``task`` of an observation binds it in the core; each such rule of the
  package whose trigger matches is evaluated — its condition, ``taskTypes``,
  the status of the task — and completes or cancels the task, which the
  process then sees as it would see its executor finish it. The sandbox has
  no verification stage, identities or claims: a completed task is done at
  once. ``expect: {rules: [...]}`` reads the decisions since the last expect;
- **the trial run on a stand** (``given.fromInstance``, P014) starts from a
  copy of a live instance's state, read by the caller into ``World.live``:
  the test goes on from where the instance is, nothing of it is written back.

The sandbox has no clients: no database, no HTTP, no memory, no content
store. ``World.writes`` is the caller's count of writes that left the
sandbox — ``expect: {noSideEffects: true}`` holds while it is zero.

Coverage (FR-026) is computed from the decisions of every run: elements
(stages, steps, milestones, timers), transitions (guards, branches, outcomes
of a wait), rows of decision tables and error handlers — with the list of
what no test reached.

Pure functions over plain values; no I/O.
"""

import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator

from control_plane.domain import process_engine as engine
from control_plane.domain import process_replay, process_sla, process_steps
from control_plane.domain.calendar import Calendar
from control_plane.domain.cel_profile import ExpressionError, environment, parse_iso_duration
from control_plane.domain.package_settings import (
    effective,
    references,
    validate,
)
from control_plane.domain.process_definition import SkillEntry, step_kind
from control_plane.domain.project import secret_findings
from control_plane.domain.work_rules import (
    BASE_ROOTS,
    ActionKind,
    ConditionError,
    VarPath,
    evaluate,
    trigger_matches,
    walk,
)

JsonSchema = Mapping[str, Any]

# The virtual time of a test without ``given.clock``: fixed, so a test gives
# the same answer on every run.
DEFAULT_CLOCK = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
# Inputs one test may feed the engine: a bound, not a budget to plan by.
MAX_INPUTS = 5_000
SANDBOX_NAMESPACE = uuid.UUID("0f5b8a52-7d3e-5c1a-9b4f-6e2d8c7a1b30")
_OPENING = ("create_task", "request_approvals", "invoke_skill", "start_child")
_CHILD_ENDS = {
    engine.COMPLETED: "completed",
    engine.FAILED: "failed",
    engine.CANCELLED: "cancelled",
}


@dataclass(frozen=True)
class LiveInstance:
    """A live instance a trial run starts from (``given.fromInstance``), read before the run.

    ``state`` — the engine state as the core keeps it; ``approvals`` — its
    pending approvals ``{activity, element, approver, excluded}`` (the
    approver as the sandbox names one: a principal, ``role:<slug>``);
    ``pending`` — approvers of a sequential step not asked yet, by activity;
    ``totals`` — how many approvers each approval activity has;
    ``attempts`` — its ``step_attempts``, ``entered`` — the attempt each open
    activity entered with, so the step events of the copy count on from them.
    """

    id: str
    process: str
    state: Mapping[str, Any]
    approvals: tuple[Mapping[str, Any], ...] = ()
    pending: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    totals: Mapping[str, int] = field(default_factory=dict)
    attempts: Mapping[str, int] = field(default_factory=dict)
    entered: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    """What a test runs against: the processes and the catalog, read before the run.

    ``definitions`` — checked processes by key: the package's and the
    catalog's latest versions for nested calls; ``skills`` by
    ``name@version``; ``task_types`` — the ``fieldSchema`` by key; ``agents``
    and ``roles`` — keys that exist; ``calendars`` by key; ``live`` — the
    instances the tests start from (``given.fromInstance``) by id; ``rules``
    — the package's rules that close the task of an observation.

    ``settings_schema`` — the settings schema the files of the package under
    test declare (``None``: none declared, CP-ADR-0081 §6); ``known_refs`` —
    the ``(x-ref kind, value)`` of the test values the organization has in
    use; ``other_settings`` — the effective settings of the catalog's
    processes the package calls, by key.
    """

    definitions: Mapping[str, engine.Definition]
    skills: Mapping[str, SkillEntry] = field(default_factory=dict)
    task_types: Mapping[str, JsonSchema | None] = field(default_factory=dict)
    agents: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    calendars: Mapping[str, Calendar] = field(default_factory=dict)
    live: Mapping[str, LiveInstance] = field(default_factory=dict)
    writes: Callable[[], int] = lambda: 0
    settings_schema: JsonSchema | None = None
    known_refs: frozenset[tuple[str, str]] = frozenset()
    other_settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Rules of the package the sandbox evaluates (``target: task``), normalized
    # ``{trigger, condition, interpretation, action}`` by key.
    rules: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure:
    step: int
    message: str
    expected: Any = None
    actual: Any = None

    def out(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class TestResult:
    file: str
    name: str
    process: str
    status: str
    duration_ms: int
    failures: list[Failure]
    # (process key, decision) of every input of every instance: the coverage.
    decisions: list[tuple[str, engine.Decision]] = field(default_factory=list)

    __test__ = False  # not a pytest class

    def out(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "name": self.name,
            "process": self.process,
            "status": self.status,
            "durationMs": self.duration_ms,
            "failures": [failure.out() for failure in self.failures],
        }


class _Abort(Exception):
    """The test cannot go on: a step could not be done."""

    def __init__(self, message: str, expected: Any = None, actual: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual


class _Refused(Exception):
    """A command of the core refuses an intent: the engine's next input is ``intent_failed``."""

    def __init__(self, code: str, status: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail


# --- objects of the sandbox --------------------------------------------------------------


@dataclass
class _Instance:
    id: str
    definition: engine.Definition
    state: dict[str, Any] | None = None
    parent: tuple[str, str] | None = None
    # What take() keeps in the instance's row: attempt counters and refs.
    attempts: dict[str, int] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)
    # The virtual time it closed at: its deadlines are read at that moment.
    closed_at: datetime | None = None


@dataclass
class _Task:
    id: str
    instance: str
    activity: str
    element: str
    task_type: str | None
    assignee: str | None
    due: str | None
    status: str = "open"
    # Filled from the case by the step (human.customFields).
    custom_fields: dict[str, Any] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "step": self.element,
            "status": self.status,
            "assignee": self.assignee,
            "due": self.due,
        }
        if self.custom_fields:
            out["customFields"] = self.custom_fields
        return out


@dataclass
class _Approval:
    id: str
    instance: str
    activity: str
    element: str
    approver: str
    excluded: tuple[str, ...]
    status: str = "pending"


# A task of the sandbox as a rule's ``task`` view reads it: (status, category).
_TASK_STATES = {
    "open": ("todo", "active"),
    "completed": ("done", "terminal_success"),
    "cancelled": ("cancelled", "terminal_cancelled"),
}


def _link(item: Mapping[str, Any]) -> str:
    if item.get("principal"):
        return str(item["principal"])
    if item.get("agent"):
        return f"agent:{item['agent']}"
    return f"role:{item.get('role')}"


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _same_time(expected: Any, actual: Any) -> bool:
    if expected == actual:
        return True
    try:
        return _time(str(expected)) == _time(str(actual))
    except ValueError:
        return False


def _same(expected: Any, actual: Any) -> bool:
    """Equal as JSON values: ``1`` and ``1.0`` are one number, ``true`` is not a number."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return float(expected) == float(actual)
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(
            _same(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _same(a, b) for a, b in zip(expected, actual, strict=True)
        )
    return bool(expected == actual)


def _contains(expected: Any, actual: Any) -> bool:
    """``expected`` is a part of ``actual``: every key it names, with the same value."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _contains(v, actual[k]) for k, v in expected.items()
        )
    return _same(expected, actual)


def _at_path(data: Any, path: str) -> tuple[bool, Any]:
    """The value at ``a.b.c`` or ``/a/b/c``: ``(found, value)``."""
    parts = (
        [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]
        if path.startswith("/")
        else path.split(".")
    )
    node = data
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def _schema_errors(schema: JsonSchema | None, value: Any) -> list[str]:
    if not schema:
        return []
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    return [f"/{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]]


def _when(expression: str | None, value: Mapping[str, Any]) -> bool:
    """A mock's ``when``: CEL over the call's input (``input``)."""
    if not expression:
        return True
    try:
        program = environment(bindings={"input": None}).compile(expression, path="/mocks/when")
        return program.evaluate({"input": dict(value)}).value is True
    except ExpressionError as exc:
        raise _Abort(f"when of a mock: {exc.message}", expected=expression) from exc


# --- the sandbox --------------------------------------------------------------------------


class Sandbox:
    """One test: its instances, their tasks, approvals and calls, its virtual clock."""

    def __init__(self, world: World, test: Mapping[str, Any], *, seed: str) -> None:
        self.world = world
        self.test = test
        self.seed = seed
        given = test.get("given") or {}
        self.given = given
        self.principals: dict[str, list[str]] = {
            role: [str(p) for p in principals]
            for role, principals in (given.get("principals") or {}).items()
        }
        self.clock = _time(given["clock"]) if given.get("clock") else DEFAULT_CLOCK
        self.mocks = test.get("mocks") or {}
        self.calendars = dict(world.calendars)
        self.process = world.definitions[str(test["process"])]
        if given.get("calendar"):
            chosen = world.calendars.get(str(given["calendar"]))
            if chosen is None:
                raise _Abort(f"given.calendar: no calendar {given['calendar']!r}")
            own = self.process.spec.get("calendar")
            if own:
                self.calendars[str(own)] = chosen
        self.instances: dict[str, _Instance] = {}
        self.roots: list[str] = []
        self.tasks: list[_Task] = []
        self.approvals: list[_Approval] = []
        self.sequential: dict[str, list[dict[str, Any]]] = {}
        self.approvers: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self.seen_events = 0
        self.remembered: list[dict[str, Any]] = []
        self.rule_decisions: list[dict[str, Any]] = []
        self.seen_rules = 0
        self.decisions: list[tuple[str, engine.Decision]] = []
        self.journals: dict[str, list[dict[str, Any]]] = {}
        self.queue: deque[tuple[str, str, dict[str, Any], str | None]] = deque()
        self.made = 0
        self.inputs = 0
        self.used: dict[tuple[str, str], int] = {}
        # The settings of the package as the sandbox saves them: version 0 — nothing saved.
        self.settings_version = 0
        self.saved_settings: dict[str, Any] = {}
        self.settings_history: dict[int, dict[str, Any]] = {}
        if given.get("settings") is not None:
            self.save_settings(given["settings"], "given.settings")

    # --- ids and inputs ---------------------------------------------------------------

    def new_id(self, kind: str) -> str:
        self.made += 1
        return str(uuid.uuid5(SANDBOX_NAMESPACE, f"{self.seed}:{kind}:{self.made}"))

    def feed(
        self, instance_id: str, kind: str, body: Mapping[str, Any], actor: str | None = None
    ) -> None:
        self.queue.append((instance_id, kind, dict(body), actor))
        self.drain()

    def drain(self) -> None:
        while self.queue:
            instance_id, kind, body, actor = self.queue.popleft()
            self.inputs += 1
            if self.inputs > MAX_INPUTS:
                raise _Abort(f"the test fed the engine more than {MAX_INPUTS} inputs")
            self.take(self.instances[instance_id], kind, body, actor)

    # --- settings of the package (CP-ADR-0081 §6) ----------------------------------------

    def save_settings(self, values: Any, where: str) -> None:
        """A saving of the settings as ``PUT`` checks it: a new version in the temporary history."""
        schema = self.world.settings_schema
        if schema is None:
            raise _Abort(
                f"{where}: settings_not_declared — the package declares no settings",
                expected="spec.settings in package.yaml",
            )
        if not isinstance(values, Mapping):
            raise _Abort(f"{where}: settings_invalid — the values are an object", actual=values)
        found = secret_findings(values)
        if found:
            raise _Abort(
                f"{where}: secret_material_rejected — settings hold no secrets", actual=found
            )
        errors = validate(values, schema)
        if errors:
            raise _Abort(
                f"{where}: settings_invalid — the values do not match the settings schema",
                actual=errors,
            )
        missing = [
            {"path": ref.path, "ref": ref.kind}
            for ref in references(values, schema)
            if (ref.kind, ref.value) not in self.world.known_refs
        ]
        if missing:
            raise _Abort(
                f"{where}: unknown_ref — a value references an object the organization"
                " does not have in use",
                actual=missing,
            )
        if json.dumps(values, sort_keys=True) == json.dumps(self.saved_settings, sort_keys=True):
            return  # the same values: no new version
        self.settings_version += 1
        self.saved_settings = json.loads(json.dumps(values))
        self.settings_history[self.settings_version] = self.saved_settings

    def settings_of(
        self, definition: engine.Definition
    ) -> tuple[Mapping[str, Any] | None, int | None]:
        """The effective settings a step of ``definition`` reads, and their version."""
        if not definition.reads_settings:
            return None, None
        if definition.key in self.world.other_settings:
            return self.world.other_settings[definition.key], None
        schema = self.world.settings_schema or {}
        return effective(self.saved_settings, schema), self.settings_version

    def take(
        self, instance: _Instance, kind: str, body: Mapping[str, Any], actor: str | None
    ) -> None:
        settings, version = self.settings_of(instance.definition)
        given = engine.Input(kind, self.clock, body, actor, self.calendars, settings=settings)
        before = instance.state
        state, decisions, intents = engine.step(instance.definition, before, given)
        # The state goes through JSON as a live one goes through jsonb.
        instance.state = json.loads(json.dumps(state))
        if instance.state["status"] in engine.CLOSED and instance.closed_at is None:
            instance.closed_at = self.clock
        self.decisions.extend((instance.definition.key, d) for d in decisions)
        # The journal as the core keeps it: a skill of this step reads its decisions.
        event = body.get("event")
        entries = process_replay.journal_entries(
            seq=int(state["seq"]),
            at=self.clock,
            kind=kind,
            source_ref=f"sandbox:{self.inputs}",
            actor_id=actor,
            event_id=event.get("id") if isinstance(event, Mapping) else None,
            given=given.out(),
            calendars={},
            decisions=[d.out() for d in decisions],
            intents=[i.out() for i in intents],
            settings_version=version,
            settings_schema_revision=None,
        )
        journal = self.journals.setdefault(instance.id, [])
        journal.extend(entries[: len(entries) - len(intents)])
        for intent in intents:
            try:
                getattr(self, f"do_{intent.kind}")(instance, dict(intent.body))
            except _Refused as refused:
                if intent.kind not in _OPENING:
                    continue
                self.queue.append(
                    (
                        instance.id,
                        "intent_failed",
                        {
                            "activityId": intent.body.get("activityId"),
                            "intent": intent.kind,
                            "code": refused.code,
                            "status": refused.status,
                            "detail": refused.detail,
                        },
                        None,
                    )
                )
        journal.extend(entries[len(entries) - len(intents) :])
        self.project_steps(instance, before, given, decisions)
        if kind == "approval":
            self.next_approver(instance, str(body.get("activityId") or ""))

    def project_steps(
        self,
        instance: _Instance,
        before: Mapping[str, Any] | None,
        given: engine.Input,
        decisions: Sequence[engine.Decision],
    ) -> None:
        """The step events of the step just taken, as ``take()`` records them (§13)."""
        projection = process_steps.step_events(
            instance.definition,
            instance_id=instance.id,
            before=before,
            after=instance.state or {},
            decisions=[d.out() for d in decisions],
            given=given.out(),
            at=self.clock,
            attempts=instance.attempts,
            refs=instance.refs,
        )
        instance.attempts = projection.attempts
        instance.refs = projection.refs
        self.events.extend(
            {"type": event.type, "instance": instance.id, "payload": event.payload}
            for event in projection.events
        )

    # --- intents ------------------------------------------------------------------------

    def do_emit_event(self, instance: _Instance, body: dict[str, Any]) -> None:
        payload = dict(body.get("payload") or {})
        if str(body["type"]).startswith(process_sla.SLA_EVENT_PREFIX):
            payload["attempt"] = process_steps.sla_attempt(
                payload, refs=instance.refs, attempts=instance.attempts
            )
        self.events.append({"type": body["type"], "instance": instance.id, "payload": payload})

    def do_set_timer(self, instance: _Instance, body: dict[str, Any]) -> None:
        return None  # timers live in the instance's state; advance reads them there

    do_cancel_timer = do_set_timer

    def resolve(self, assign: Sequence[Mapping[str, Any]], field_name: str) -> str:
        """The first link of an assignment chain the sandbox knows, as the core picks it."""
        for item in assign or ():
            if item.get("principal"):
                return str(item["principal"])
            if item.get("agent") and item["agent"] in self.world.agents:
                return f"agent:{item['agent']}"
            if item.get("role") and self.known_role(str(item["role"])):
                return f"role:{item['role']}"
        if assign:
            first = assign[0]
            if first.get("agent"):
                raise _Refused(
                    "unknown_agent", 422, f"{field_name}: no active agent {first['agent']!r}"
                )
            raise _Refused("unknown_role", 422, f"{field_name}: no role {first.get('role')!r}")
        raise _Refused("invalid_assignment", 422, f"{field_name}: nobody is named")

    def known_role(self, role: str) -> bool:
        return role in self.world.roles or role in self.principals

    def do_create_task(self, instance: _Instance, body: dict[str, Any]) -> None:
        assignee = self.resolve(body.get("assign") or (), "assign")
        prefill = dict(body.get("customFields") or {})
        if prefill and body.get("taskType") is not None:
            # As the core's create_task: the fields against the type's fieldSchema.
            errors = _schema_errors(self.world.task_types.get(body["taskType"]), prefill)
            if errors:
                raise _Refused(
                    "custom_fields_invalid",
                    422,
                    f"customFields of task type {body['taskType']}: {'; '.join(errors)}",
                )
        task = _Task(
            id=self.new_id("task"),
            instance=instance.id,
            activity=str(body["activityId"]),
            element=str(body.get("element")),
            task_type=body.get("taskType"),
            assignee=assignee,
            due=body.get("due"),
            custom_fields=prefill,
        )
        self.tasks.append(task)
        instance.refs[f"task:{task.id}"] = {"activity": task.activity, "element": task.element}
        if assignee.startswith("agent:"):
            answers = (self.mocks.get("agents") or {}).get(assignee[len("agent:") :])
            if answers:
                self.answer_agent(task, answers, dict(body.get("input") or {}))

    def answer_agent(self, task: _Task, answers: Sequence[Mapping[str, Any]], given: Any) -> None:
        mock = self.pick(("agent", task.assignee or ""), answers, task.element, given)
        if mock is None or mock.get("timeout") is True:
            return
        if mock.get("error") is not None:
            task.status = "cancelled"
            self.queue.append(
                (task.instance, "task", {"activityId": task.activity, "status": "cancelled"}, None)
            )
            return
        output = mock.get("output")
        if not isinstance(output, dict):
            raise _Abort(
                f"the mock of {task.assignee} gives no object: an agent's result is an object",
                actual=output,
            )
        self.finish_task(task, output, task.assignee)

    def finish_task(self, task: _Task, output: dict[str, Any], by: str | None) -> None:
        task.status = "completed"
        record = {
            "id": task.id,
            "status": "done",
            # The task keeps what the step filled in, as a task of the core does.
            "customFields": {**task.custom_fields, **output},
            "assigneeId": by if by is not None else task.assignee,
        }
        self.queue.append(
            (
                task.instance,
                "task",
                {"activityId": task.activity, "status": "completed", "task": record},
                by,
            )
        )

    def open_task(self, activity: str) -> _Task | None:
        return next((t for t in self.tasks if t.activity == activity and t.status == "open"), None)

    def do_cancel_task(self, instance: _Instance, body: dict[str, Any]) -> None:
        task = self.open_task(str(body["activityId"]))
        if task is not None:
            task.status = "cancelled"

    def do_reassign_task(self, instance: _Instance, body: dict[str, Any]) -> None:
        task = self.open_task(str(body["activityId"]))
        if task is not None:
            task.assignee = self.resolve(body.get("assign") or (), "assign")

    def do_request_approvals(self, instance: _Instance, body: dict[str, Any]) -> None:
        approvers = list(body.get("approvers") or ())
        if not approvers:
            raise _Refused("invalid_approval", 422, "the step names no approvers")
        for approver in approvers:
            self.resolve([approver], "approvers")
        listed = body.get("excludedPrincipals", [])
        if not isinstance(listed, list) or any(p in (None, "") for p in listed):
            # As the core: an empty value is refused, never an exclusion dropped
            # (CP-ADR-0074 §7).
            raise _Refused(
                "invalid_approval", 422, "separationOfDuties names a value that is not a principal"
            )
        excluded = tuple(str(p) for p in listed)
        if any(approver.get("principal") and _link(approver) in excluded for approver in approvers):
            # As the core: an excluded approver could never decide (CP-ADR-0074 §7).
            raise _Refused("invalid_approval", 422, "an approver of the step is excluded")
        activity = str(body["activityId"])
        self.approvers[activity] = len(approvers)
        first = approvers[:1] if body.get("mode") == "sequential" else approvers
        self.sequential[activity] = approvers[len(first) :]
        for approver in first:
            self.request_one(instance, activity, str(body.get("element")), approver, excluded)

    def request_one(
        self,
        instance: _Instance,
        activity: str,
        element: str,
        approver: Mapping[str, Any],
        excluded: tuple[str, ...],
    ) -> None:
        approval = _Approval(
            self.new_id("approval"), instance.id, activity, element, _link(approver), excluded
        )
        self.approvals.append(approval)
        instance.refs[f"approval:{approval.id}"] = {"activity": activity, "element": element}

    def next_approver(self, instance: _Instance, activity: str) -> None:
        pending = self.sequential.get(activity)
        state = instance.state or {}
        if not pending or activity not in (state.get("activities") or {}):
            return
        if state.get("status") != engine.RUNNING:
            return
        approver, *rest = pending
        self.sequential[activity] = rest
        first = next(a for a in self.approvals if a.activity == activity)
        self.request_one(instance, activity, first.element, approver, first.excluded)

    def do_close_approvals(self, instance: _Instance, body: dict[str, Any]) -> None:
        activity = str(body["activityId"])
        self.sequential[activity] = []
        for approval in self.approvals:
            if approval.activity == activity and approval.status == "pending":
                approval.status = "cancelled"

    def do_invoke_skill(self, instance: _Instance, body: dict[str, Any]) -> None:
        ref = str(body["skill"])
        skill = self.world.skills.get(ref)
        if skill is None or skill.status != "active":
            raise _Refused("unknown_skill", 422, f"no active skill {ref}")
        given = process_replay.with_attachments(body, lambda: self.journals.get(instance.id, []))
        errors = _schema_errors(skill.input_schema, given)
        if errors:
            raise _Refused("invalid_skill_inputs", 422, "; ".join(errors))
        answers = (self.mocks.get("skills") or {}).get(ref)
        if not answers:
            return  # no mock: the call stays unanswered
        mock = self.pick(("skill", ref), answers, str(body.get("element")), given)
        if mock is None or mock.get("timeout") is True:
            return
        answer: dict[str, Any] = {"activityId": body["activityId"]}
        if mock.get("error") is not None:
            error = mock["error"]
            answer.update(
                status="failed",
                error={
                    "code": error.get("type"),
                    "status": error.get("status"),
                    "message": error.get("detail"),
                },
            )
        else:
            output = mock.get("output")
            problems = _schema_errors(skill.output_schema, output)
            if problems:
                raise _Abort(
                    f"the mock of skill {ref} does not match the skill's output schema: "
                    + "; ".join(problems),
                    expected=skill.output_schema,
                    actual=output,
                )
            answer.update(status="succeeded", output=output)
        self.queue.append((instance.id, "skill", answer, None))

    def pick(
        self,
        name: tuple[str, str],
        answers: Sequence[Mapping[str, Any]],
        element: str,
        given: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """The answer of a call: the next one of its step in order, or the first whose when holds.

        Answers are taken in the order of calls; once they are used up the last
        one answers again.
        """
        fitting = [
            (index, mock)
            for index, mock in enumerate(answers)
            if mock.get("step") in (None, element) and _when(mock.get("when"), given)
        ]
        if not fitting:
            return None
        used = self.used.get(name, 0)
        self.used[name] = used + 1
        return fitting[min(used, len(fitting) - 1)][1]

    def do_recall(self, instance: _Instance, body: dict[str, Any]) -> None:
        try:
            answer = process_replay.mock_recall(self.mocks.get("recall") or (), body)
        except process_replay.MockError as exc:
            if exc.code == "mock_missing":
                return  # memory does not answer: the step's timeout decides
            raise _Abort(str(exc), actual=exc.details) from exc
        self.queue.append((instance.id, "recall", answer, None))

    def do_remember(self, instance: _Instance, body: dict[str, Any]) -> None:
        self.remembered.append(body)

    def do_start_child(self, instance: _Instance, body: dict[str, Any]) -> None:
        key = str(body["process"])
        definition = self.world.definitions.get(key)
        if definition is None:
            raise _Refused("not_found", 404, f"no process {key}")
        data = dict(body.get("input") or {})
        errors = _schema_errors(definition.spec.get("data"), data)
        if errors:
            raise _Refused("invalid_process_data", 422, "; ".join(errors))
        activity = str(body["activityId"])
        child = _Instance(self.new_id("instance"), definition, None, (instance.id, activity))
        self.instances[child.id] = child
        instance.refs[f"child:{child.id}"] = {"activity": activity, "element": body.get("element")}
        self.queue.append(
            (
                child.id,
                "start",
                {"instanceId": child.id, "key": f"{instance.id}/{activity}", "data": data},
                None,
            )
        )

    def do_cancel_child(self, instance: _Instance, body: dict[str, Any]) -> None:
        activity = str(body["activityId"])
        for child in self.instances.values():
            state = child.state or {}
            if child.parent == (instance.id, activity) and state.get("status") not in engine.CLOSED:
                self.queue.append(
                    (
                        child.id,
                        "command",
                        {"action": "cancel", "reason": str(body.get("reason") or "parent")},
                        None,
                    )
                )

    def do_complete(self, instance: _Instance, body: dict[str, Any]) -> None:
        if instance.parent is None:
            return
        parent, activity = instance.parent
        state = instance.state or {}
        self.queue.append(
            (
                parent,
                "child",
                {
                    "activityId": activity,
                    "status": _CHILD_ENDS[str(body["status"])],
                    "outcome": body.get("outcome"),
                    "data": dict(state.get("data") or {}),
                    "error": state.get("error"),
                },
                None,
            )
        )

    # --- test steps ---------------------------------------------------------------------

    def start_given(self) -> None:
        given = self.given
        if given.get("fromInstance"):
            self.start_from(str(given["fromInstance"]))
            return
        if given.get("stage"):
            raise _Abort("given.stage: the engine starts an instance at its start; not supported")
        if given.get("data") is None:
            return
        data = dict(given["data"])
        errors = _schema_errors(self.process.spec.get("data"), data)
        if errors:
            raise _Abort("given.data does not match the data schema of the process", actual=errors)
        root = self.new_root()
        self.feed(root.id, "start", {"instanceId": root.id, "key": "test", "data": data})

    def start_from(self, instance_id: str) -> None:
        """The trial run: the test goes on from a copy of a live instance's state.

        The copy runs on the package's version of its process. Its open tasks
        and pending approvals become the sandbox's; a skill call, a recall or
        a nested process it waits on stays unanswered, as a call without a
        mock. The clock starts at the instance's last input unless
        ``given.clock`` is set.
        """
        given = self.given
        if given.get("data") is not None or given.get("stage"):
            raise _Abort("given.fromInstance starts from the instance: no given.data or stage")
        live = self.world.live.get(instance_id)
        if live is None:
            raise _Abort(f"given.fromInstance: no instance {instance_id}")
        if live.process != self.process.key:
            raise _Abort(
                f"given.fromInstance: the instance is of process {live.process!r}",
                expected=self.process.key,
                actual=live.process,
            )
        state: dict[str, Any] = json.loads(json.dumps(live.state))
        instance = _Instance(live.id, self.process, state)
        instance.attempts = {str(k): int(v) for k, v in live.attempts.items()}
        instance.refs = {
            f"{process_steps.ACTIVITY_REF}{k}": {"attempt": int(v)} for k, v in live.entered.items()
        }
        self.instances[instance.id] = instance
        self.roots.append(instance.id)
        if not given.get("clock") and state.get("clock"):
            self.clock = _time(str(state["clock"]))
        activities = sorted((state.get("activities") or {}).values(), key=lambda a: a["n"])
        for activity in activities:
            if activity["kind"] not in ("task", "agent"):
                continue
            element = str(activity.get("element"))
            entry = self.process.steps.get(element)
            human = (entry.node.get("human") if entry is not None else None) or {}
            try:
                assignee: str | None = self.resolve(activity.get("assign") or (), "assign")
            except _Refused:
                assignee = None
            task = _Task(
                id=self.new_id("task"),
                instance=instance.id,
                activity=str(activity["id"]),
                element=element,
                task_type=human.get("taskType"),
                assignee=assignee,
                due=activity.get("due"),
            )
            self.tasks.append(task)
            instance.refs[f"task:{task.id}"] = {"activity": task.activity, "element": element}
        for approval in live.approvals:
            copied = _Approval(
                self.new_id("approval"),
                instance.id,
                str(approval["activity"]),
                str(approval["element"]),
                str(approval["approver"]),
                tuple(str(p) for p in approval.get("excluded") or ()),
            )
            self.approvals.append(copied)
            instance.refs[f"approval:{copied.id}"] = {
                "activity": copied.activity,
                "element": copied.element,
            }
        self.approvers.update({str(k): int(v) for k, v in live.totals.items()})
        self.sequential.update({str(k): [dict(a) for a in v] for k, v in live.pending.items()})

    def new_root(self) -> _Instance:
        instance = _Instance(self.new_id("instance"), self.process)
        self.instances[instance.id] = instance
        self.roots.append(instance.id)
        return instance

    def emit(self, spec: Mapping[str, Any]) -> None:
        # ``by`` is the author of the event, as the journal's ``actor_id``: the
        # core feeds it to the instances as the actor of their input too
        actor = str(spec["by"]) if spec.get("by") is not None else None
        event: dict[str, Any] = {
            "id": self.new_id("event"),
            "time": _rfc3339(self.clock),
            "entityType": None,
            "entityId": None,
            "actorId": actor,
            "correlationId": None,
            "payload": dict(spec.get("payload") or {}),
        }
        if spec.get("observation") is not None:
            event["type"] = "observation.recorded"
            event["observation"] = spec["observation"]
            if spec.get("source") is not None:
                event["source"] = spec["source"]
            if spec.get("task") is not None:
                event["payload"]["taskId"] = self.task_of(str(spec["task"])).id
        else:
            if spec.get("task") is not None:
                raise _Abort("emit.task binds an observation to a task; an event has its own")
            event["type"] = spec["event"]
        started = engine.start_key(self.process, event, self.settings_of(self.process)[0])
        if started is not None:
            existing = next(
                (
                    self.instances[i]
                    for i in self.roots
                    if (self.instances[i].state or {}).get("key") == started
                ),
                None,
            )
            target = existing or self.new_root()
            self.queue.append(
                (target.id, "start", {"instanceId": target.id, "event": event}, actor)
            )
        for instance in list(self.instances.values()):
            state = instance.state
            if not state or state["status"] in engine.CLOSED:
                continue
            if instance.definition is self.process and state["key"] == started:
                continue  # the start input already correlated it
            reads = self.settings_of(instance.definition)[0]
            if state["key"] in engine.correlation_keys(instance.definition, event, reads):
                self.queue.append((instance.id, "event", {"event": event}, actor))
        self.drain()
        if event.get("observation") is not None:
            for key in sorted(self.world.rules):
                self.apply_rule(key, self.world.rules[key], event)
            self.drain()

    # --- rules that close the task of an observation (CP-ADR-0063 Zh5) --------------

    def task_of(self, element: str) -> _Task:
        tasks = [t for t in self.tasks if t.element == element]
        if not tasks:
            raise _Abort(f"emit.task: step {element!r} has no task")
        return tasks[-1]

    def apply_rule(self, key: str, rule: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        """One rule on one observation, as the core decides it (Zh3), identities aside."""
        payload = {**event["payload"], "kind": event["observation"]}
        if event.get("source") is not None:
            payload["source"] = event["source"]
        if not trigger_matches(rule["trigger"], "observation.recorded", payload):
            return
        if rule.get("interpretation") is not None:
            raise _Abort(
                f"rule {key!r} interprets its facts with a skill: "
                "the sandbox does not run the interpretation of a rule"
            )
        action = rule["action"]
        decision: dict[str, Any] = {"rule": key, "action": action["kind"]}
        self.rule_decisions.append(decision)
        task = next((t for t in self.tasks if t.id == payload.get("taskId")), None)
        view = None
        if task is not None:
            decision["step"] = task.element
            status, category = _TASK_STATES[task.status]
            view = {
                "id": task.id,
                "typeKey": task.task_type,
                "status": status,
                "systemStatusCategory": category,
                "assigneeId": task.assignee,
            }
        documents = {
            "trigger": {"kind": "observation", "type": event["observation"], "ref": event["id"]},
            "payload": payload,
            "goal": None,
            "task": view,
            # The effective settings of the package under test (CP-ADR-0081 §6).
            "settings": effective(self.saved_settings, self.world.settings_schema or {}),
        }

        def resolve(path: VarPath) -> Any:
            return walk(documents.get(path.root), path.segments)

        try:
            matched = evaluate(rule.get("condition", True), resolve, roots=BASE_ROOTS)
        except ConditionError as exc:
            decision.update(result="failed", reason="rule_condition_error", detail=str(exc))
            return
        if not matched:
            decision["result"] = "not_matched"
            return
        if payload.get("taskId") is None:
            decision.update(result="skipped", reason="no_bound_task")
            return
        if task is None:
            decision.update(result="failed", reason="bound_task_not_found")
            return
        if task.task_type not in (action.get("taskTypes") or ()):
            decision.update(result="skipped", reason="bound_task_type_not_listed")
            return
        if task.status != "open":
            reason = "already_done" if task.status == "completed" else "already_closed"
            decision.update(result="skipped", reason=reason)
            return
        decision["result"] = "matched"
        if action["kind"] == ActionKind.COMPLETE_WORK:
            self.finish_task(task, {}, None)
        else:
            task.status = "cancelled"
            self.queue.append(
                (task.instance, "task", {"activityId": task.activity, "status": "cancelled"}, None)
            )

    def pending_timers(self) -> list[tuple[datetime, int, str, dict[str, Any]]]:
        found = []
        for instance in self.instances.values():
            state = instance.state or {}
            if state.get("status") in engine.CLOSED:
                continue
            for timer in (state.get("timers") or {}).values():
                if timer["state"] == "pending" and timer.get("dueAt"):
                    found.append((_time(timer["dueAt"]), int(timer["n"]), instance.id, timer))
        return sorted(found, key=lambda t: (t[0], t[2], t[1]))

    def advance(self, text: str) -> None:
        if text.startswith("until:"):
            name = text[len("until:") :]
            matching = [t for t in self.pending_timers() if name in (t[3]["id"], t[3]["element"])]
            if not matching:
                raise _Abort(f"no pending timer {name!r}", actual=self.timer_view())
            target = matching[0][0]
        else:
            delta = parse_iso_duration(text)
            if delta is None or delta < timedelta(0):
                raise _Abort(f"advance: {text!r} is not a duration of fixed length (P3D, PT4H)")
            target = self.clock + delta
        while True:
            due = [t for t in self.pending_timers() if t[0] <= target]
            if not due:
                break
            moment, _, instance_id, timer = due[0]
            self.clock = max(self.clock, moment)
            self.feed(instance_id, "timer", {"timerId": timer["id"]})
        self.clock = max(self.clock, target)

    def eligible(self, holder: str | None, by: str) -> bool:
        if holder is None or holder == by:
            return True
        if holder.startswith("role:"):
            role = holder[len("role:") :]
            return role not in self.principals or by in self.principals[role]
        return False

    def complete(self, spec: Mapping[str, Any]) -> None:
        element = str(spec["step"])
        tasks = [t for t in self.tasks if t.element == element and t.status == "open"]
        if not tasks:
            raise _Abort(
                f"step {element!r} has no open task",
                actual=[t.out() for t in self.tasks if t.element == element],
            )
        task = tasks[-1]
        by = spec.get("by")
        if by is not None and not self.eligible(task.assignee, str(by)):
            raise _Abort(f"{by} may not complete the task of {element!r}", actual=task.out())
        if spec.get("cancel") is True:
            task.status = "cancelled"
            self.feed(task.instance, "task", {"activityId": task.activity, "status": "cancelled"})
            return
        # What the person enters goes over what the step filled in.
        output = {**task.custom_fields, **dict(spec.get("output") or {})}
        if task.task_type is not None:
            errors = _schema_errors(self.world.task_types.get(task.task_type), output)
            if errors:
                raise _Abort(
                    f"the output does not match the field schema of task type {task.task_type}",
                    expected=self.world.task_types.get(task.task_type),
                    actual=errors,
                )
        self.finish_task(task, output, str(by) if by is not None else None)
        self.drain()

    def approve(self, spec: Mapping[str, Any]) -> str | None:
        """Decide an approval; the code of the core's refusal, or ``None`` when it was taken."""
        element, by = str(spec["step"]), str(spec["by"])
        pending = [a for a in self.approvals if a.element == element and a.status == "pending"]
        if not pending:
            raise _Abort(f"step {element!r} has no pending approval")
        if by in pending[0].excluded:
            return "separation_of_duties_violation"
        mine = [a for a in pending if a.approver == by or self.holds(by, a.approver)]
        if not mine:
            return "not_eligible"
        approval = mine[0]
        outcome = "approved" if spec["decision"] == "approve" else "rejected"
        approval.status = outcome
        self.feed(
            approval.instance,
            "approval",
            {
                "activityId": approval.activity,
                "approvalId": approval.id,
                "outcome": outcome,
                "principal": by,
                "total": self.approvers.get(approval.activity),
            },
            by,
        )
        return None

    def holds(self, by: str, approver: str) -> bool:
        return approver.startswith("role:") and by in self.principals.get(
            approver[len("role:") :], ()
        )

    # --- expect -------------------------------------------------------------------------

    def root(self) -> dict[str, Any]:
        if not self.roots:
            raise _Abort("no instance has started")
        return self.instances[self.roots[0]].state or {}

    def timer_view(self) -> list[dict[str, Any]]:
        return [
            {"id": t[3]["element"], "at": t[3]["dueAt"], "provisional": t[3]["provisional"]}
            for t in self.pending_timers()
        ]

    def expect(self, index: int, spec: Mapping[str, Any]) -> list[Failure]:
        failures: list[Failure] = []

        def fail(message: str, expected: Any, actual: Any) -> None:
            failures.append(Failure(index, message, expected, actual))

        state = self.root() if set(spec) - {"events", "noSideEffects", "memory", "rules"} else None
        if state is not None:
            self.expect_state(state, spec, fail)
        for key, wanted in (spec.get("sla") or {}).items():
            self.expect_sla(str(key), str(wanted), fail)
        if "events" in spec:
            emitted = [e["type"] for e in self.events[self.seen_events :]]
            left = list(emitted)
            for wanted in spec["events"]:
                if wanted in left:
                    left.remove(wanted)
                else:
                    fail(f"no event {wanted} since the last expect", wanted, emitted)
        self.seen_events = len(self.events)
        if "rules" in spec:
            decided = self.rule_decisions[self.seen_rules :]
            for wanted in spec["rules"]:
                if not any(_contains(wanted, decision) for decision in decided):
                    fail("no decision of a rule like this since the last expect", wanted, decided)
        self.seen_rules = len(self.rule_decisions)
        memory = spec.get("memory") or {}
        if "recalled" in memory:
            recalled = sorted(
                {d.element for _, d in self.decisions if d.kind == "recall_completed" and d.element}
            )
            for step in memory["recalled"]:
                if step not in recalled:
                    fail(f"step {step!r} got no answer of memory", step, recalled)
        for wanted in memory.get("remembered") or ():
            if not any(_contains(wanted, written) for written in self.remembered):
                fail("nothing like this was remembered", wanted, self.remembered)
        if spec.get("noSideEffects") is True:
            writes = self.world.writes()
            if writes:
                fail("the run wrote outside the sandbox", 0, writes)
        return failures

    def expect_state(
        self,
        state: Mapping[str, Any],
        spec: Mapping[str, Any],
        fail: Callable[[str, Any, Any], None],
    ) -> None:
        closed = state.get("status") in engine.CLOSED
        if "status" in spec and spec["status"] != state.get("status"):
            fail("status of the instance", spec["status"], state.get("status"))
        if "outcome" in spec and spec["outcome"] != state.get("outcome"):
            fail("outcome of the instance", spec["outcome"], state.get("outcome"))
        if "error" in spec:
            error = (state.get("error") or {}).get("type")
            if spec["error"] != error:
                fail("error of the instance", spec["error"], state.get("error"))
        for stage, wanted in (spec.get("stages") or {}).items():
            record = (state.get("stages") or {}).get(stage)
            if record is None:
                fail(f"the process has no stage {stage!r}", wanted, None)
                continue
            actual = {
                "active": "open",
                "completed": "completed",
                "available": "skipped" if closed else "not_started",
            }.get(record["state"], record["state"])
            if actual != wanted:
                fail(f"stage {stage!r}", wanted, actual)
        milestones = state.get("milestones") or {}
        for milestone in spec.get("milestones") or ():
            if not milestones.get(milestone):
                fail(f"milestone {milestone!r} is not reached", True, milestones.get(milestone))
        for wanted in spec.get("tasks") or ():
            if not any(self.task_matches(wanted, task) for task in self.tasks):
                fail(
                    f"no task of step {wanted.get('step')!r} like this",
                    wanted,
                    [t.out() for t in self.tasks if t.element == wanted.get("step")],
                )
        timers = self.timer_view()
        pending = self.pending_timers()
        for wanted in spec.get("timers") or ():
            found = [
                t[3] for t in pending if wanted.get("id") in (None, t[3]["id"], t[3]["element"])
            ]
            if not any(
                ("at" not in wanted or _same_time(wanted["at"], timer["dueAt"]))
                and ("provisional" not in wanted or wanted["provisional"] == timer["provisional"])
                for timer in found
            ):
                fail(f"no pending timer {wanted.get('id')!r} like this", wanted, timers)
        for path, wanted in (spec.get("data") or {}).items():
            exists, actual = _at_path(state.get("data") or {}, path)
            if not exists:
                fail(f"data has no {path}", wanted, None)
            elif not _same(wanted, actual):
                fail(f"data {path}", wanted, actual)

    def expect_sla(self, key: str, wanted: str, fail: Callable[[str, Any, Any], None]) -> None:
        """The state of a deadline of the test's instance as its projection shows it (§6).

        ``process`` is the process's deadline (``spec.due``), any other key the
        deadline of the open attempt of that step — the latest, when the
        element holds several. A closed instance is read at its close.
        """
        instance = self.instances[self.roots[0]]
        state = instance.state or {}
        timers = state.get("timers") or {}
        now = instance.closed_at or self.clock
        if key == process_sla.PROCESS:
            what, found = "the process", state.get("sla")
        else:
            what = f"step {key!r}"
            if key not in instance.definition.steps:
                fail(f"SLA: the process has no step {key!r}", wanted, None)
                return
            open_ = [a for a in (state.get("activities") or {}).values() if a["element"] == key]
            if not open_:
                fail(f"SLA of {what}: the step has no open attempt", wanted, None)
                return
            found = max(open_, key=lambda a: a["n"]).get("sla")
        due, actual, _ = process_sla.shown(found, now=now, timers=timers)
        if actual == wanted:
            return
        moments = ""
        if due is not None and due["dueAt"] is not None:
            moments = f" (due {due['dueAt']}, warning {due['warnAt'] or '-'})"
        fail(
            f"SLA of {what} at {_rfc3339(now)}{moments}: {actual}, expected {wanted}",
            wanted,
            actual,
        )

    def task_matches(self, wanted: Mapping[str, Any], task: _Task) -> bool:
        if task.element != wanted.get("step"):
            return False
        if "status" in wanted and wanted["status"] != task.status:
            return False
        if "assignee" in wanted:
            who = str(wanted["assignee"])
            if task.assignee != who and not (
                task.assignee is not None and self.holds(who, task.assignee)
            ):
                return False
        if "due" in wanted and not _same_time(wanted["due"], task.due):
            return False
        # Only the fields named are compared.
        fields = wanted.get("customFields") or {}
        return all(
            name in task.custom_fields and _same(value, task.custom_fields[name])
            for name, value in fields.items()
        )


def run_test(world: World, file: str, test: Mapping[str, Any]) -> TestResult:
    """Run one test in a fresh sandbox; nothing of it outlives the call."""
    started = time.monotonic()
    failures: list[Failure] = []
    status = "passed"
    steps = list(test.get("steps") or ())
    index = -1
    sandbox: Sandbox | None = None
    try:
        sandbox = Sandbox(world, test, seed=f"{file}:{test.get('name')}")
        sandbox.start_given()
        for index, step in enumerate(steps):
            failures.extend(_run_step(sandbox, index, step))
        minimum = (test.get("coverage") or {}).get("minimum")
        if minimum is not None:
            coverage = process_coverage(
                sandbox.process, [d for k, d in sandbox.decisions if k == sandbox.process.key]
            )
            percent = coverage.elements.percent
            if percent < float(minimum):
                failures.append(
                    Failure(
                        max(len(steps) - 1, 0),
                        f"the test covers {percent:.1f}% of the elements of the process",
                        minimum,
                        {"percent": round(percent, 1), "missing": coverage.elements.missing},
                    )
                )
    except _Abort as abort:
        failures.append(Failure(max(index, 0), abort.message, abort.expected, abort.actual))
    except (engine.EngineError, engine.DefinitionError) as exc:
        status = "error"
        failures.append(Failure(max(index, 0), f"the engine refused an input: {exc}"))
    if failures and status == "passed":
        status = "failed"
    return TestResult(
        file=file,
        name=str(test.get("name") or file),
        process=str(test.get("process")),
        status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        failures=failures,
        decisions=list(sandbox.decisions) if sandbox is not None else [],
    )


def _run_step(sandbox: Sandbox, index: int, step: Mapping[str, Any]) -> list[Failure]:
    if "settings" in step:
        sandbox.save_settings(step["settings"], f"steps[{index}].settings")
    elif "emit" in step:
        sandbox.emit(step["emit"])
    elif "advance" in step:
        sandbox.advance(str(step["advance"]))
    elif "complete" in step:
        sandbox.complete(step["complete"])
    elif "approve" in step:
        spec = step["approve"]
        refused = sandbox.approve(spec)
        wanted = spec.get("expectRefused")
        if refused != wanted:
            message = (
                f"the core refused the decision of {spec['by']}: {refused}"
                if refused
                else f"the decision of {spec['by']} was taken, a refusal was expected"
            )
            if refused and not wanted:
                raise _Abort(message, expected=wanted, actual=refused)
            return [Failure(index, message, wanted, refused)]
    elif "expect" in step:
        return sandbox.expect(index, step["expect"])
    return []


# --- coverage ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Counter:
    covered: int
    total: int
    missing: list[str]

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total

    def out(self) -> dict[str, Any]:
        return {"covered": self.covered, "total": self.total, "missing": self.missing}


@dataclass(frozen=True)
class Coverage:
    process: str
    version: int
    elements: Counter
    transitions: Counter
    decision_rows: Counter
    handlers: Counter

    def out(self) -> dict[str, Any]:
        return {
            "process": self.process,
            "version": self.version,
            "elements": self.elements.out(),
            "transitions": self.transitions.out(),
            "decisionRows": self.decision_rows.out(),
            "handlers": self.handlers.out(),
        }


def _counter(total: Iterable[str], reached: set[str]) -> Counter:
    names = list(dict.fromkeys(total))
    missing = [name for name in names if name not in reached]
    return Counter(len(names) - len(missing), len(names), missing)


def _block_steps(definition: engine.Definition, block: str) -> list[str]:
    _, items = definition.blocks.get(block, ("", ()))
    return [str(item["id"]) for item in items]


def process_coverage(
    definition: engine.Definition, decisions: Sequence[engine.Decision]
) -> Coverage:
    """What the decisions of a process's runs reached, against everything it declares.

    - **elements** — stages (entered), steps (executed; a ``do`` or ``try``
      when a step inside ran), milestones (reached), timers of a stage or of
      the process (fired);
    - **transitions** — ``<stage>:entry`` and ``<stage>:exit`` (a guard held),
      ``<step>:when`` and ``<step>:skip``, ``<step>:any/<i>`` and
      ``<step>:timeout`` of a ``listen``, ``<step>:answered`` and
      ``<step>:timeout`` of a ``recall``, ``<step>:approved`` and
      ``<step>:rejected``, ``<branch>`` of a ``fork``, ``correlate/<i>``,
      ``onEvent/<i>``;
    - **decision rows** — ``<table>/<row>``, a row that answered;
    - **handlers** — ``<step>/catch/<i>``, ``<step>/retry``,
      ``<step>/onTimeout``, ``<step>/onCompensate``, ``<step>/escalations/<i>``,
      ``<step>/onDue``.
    """
    spec = definition.spec
    by_element: dict[str, set[str]] = {}
    for decision in decisions:
        if decision.element is not None:
            by_element.setdefault(decision.element, set()).add(decision.kind)

    def did(element: str, *kinds: str) -> bool:
        return bool(by_element.get(element, set()) & set(kinds))

    def matching(kind: str, element: str) -> list[Mapping[str, Any]]:
        return [d.detail for d in decisions if d.kind == kind and d.element == element]

    ran: set[str] = {
        sid
        for sid, kinds in by_element.items()
        if sid in definition.steps and kinds - {"step_skipped"}
    }
    # A container ran when a step inside it did: ``do`` and ``try`` leave no decision of their own.
    changed = True
    while changed:
        changed = False
        for sid, entry in definition.steps.items():
            if sid in ran or step_kind(entry.node) not in ("do", "try"):
                continue
            inner = [
                step
                for name in definition.blocks
                if name.startswith(f"step:{sid}/")
                for step in _block_steps(definition, name)
            ]
            if any(step in ran for step in inner):
                ran.add(sid)
                changed = True

    elements: list[str] = []
    reached: set[str] = set()
    transitions: list[str] = []
    passed: set[str] = set()
    handlers: list[str] = []
    handled: set[str] = set()
    rows: list[str] = []
    answered: set[str] = set()

    for stage in spec["stages"]:
        sid = stage["id"]
        elements.append(sid)
        if did(sid, "stage_entered"):
            reached.add(sid)
        if stage.get("entry") is not None:
            transitions.append(f"{sid}:entry")
            if did(sid, "stage_entered"):
                passed.add(f"{sid}:entry")
        if stage.get("exit") is not None:
            transitions.append(f"{sid}:exit")
            if any(d.get("cause") == "exit" for d in matching("stage_exited", sid)):
                passed.add(f"{sid}:exit")
        for milestone in stage.get("milestones") or ():
            elements.append(milestone["id"])
            if did(milestone["id"], "milestone_reached"):
                reached.add(milestone["id"])
    for sid in definition.steps:
        elements.append(sid)
        if sid in ran:
            reached.add(sid)
    for tid in definition.timers:
        elements.append(tid)
        if did(tid, "timer_fired"):
            reached.add(tid)

    for index, _ in enumerate(spec.get("correlate") or ()):
        name = f"correlate/{index}"
        transitions.append(name)
        if did(f"correlate:{index}", "correlated"):
            passed.add(name)
    for index, _ in enumerate(spec.get("onEvent") or ()):
        name = f"onEvent/{index}"
        transitions.append(name)
        if did(f"onEvent:{index}", "event_matched"):
            passed.add(name)

    for sid, entry in definition.steps.items():
        node = entry.node
        kind = step_kind(node)
        body = node[kind]
        if node.get("when") is not None:
            transitions += [f"{sid}:when", f"{sid}:skip"]
            if sid in ran:
                passed.add(f"{sid}:when")
            if did(sid, "step_skipped"):
                passed.add(f"{sid}:skip")
        if kind == "listen":
            for index, _ in enumerate(body["any"]):
                transitions.append(f"{sid}:any/{index}")
                if any(d.get("option") == index for d in matching("listen_matched", sid)):
                    passed.add(f"{sid}:any/{index}")
            if body.get("timeout") is not None:
                transitions.append(f"{sid}:timeout")
                if did(sid, "step_timed_out"):
                    passed.add(f"{sid}:timeout")
        elif kind == "recall":
            transitions += [f"{sid}:answered", f"{sid}:timeout"]
            if did(sid, "recall_completed"):
                passed.add(f"{sid}:answered")
            if did(sid, "recall_timed_out"):
                passed.add(f"{sid}:timeout")
        elif kind == "approve":
            outcomes = {d.get("outcome") for d in matching("approval_decided", sid)}
            for outcome in ("approved", "rejected"):
                transitions.append(f"{sid}:{outcome}")
                if outcome in outcomes:
                    passed.add(f"{sid}:{outcome}")
            if body.get("onDue") in ("approve", "reject"):
                handlers.append(f"{sid}/onDue")
                if any(d.get("cause") == "due" for d in matching("approval_decided", sid)):
                    handled.add(f"{sid}/onDue")
        elif kind == "fork":
            for branch in body["branches"]:
                transitions.append(str(branch["id"]))
                if any(s in ran for s in _block_steps(definition, f"branch:{branch['id']}")):
                    passed.add(str(branch["id"]))
        elif kind == "try":
            for index, _ in enumerate(body.get("catch") or ()):
                handlers.append(f"{sid}/catch/{index}")
                if any(d.get("clause") == index for d in matching("error_caught", sid)):
                    handled.add(f"{sid}/catch/{index}")
            if body.get("retry"):
                handlers.append(f"{sid}/retry")
                if did(sid, "retry_scheduled"):
                    handled.add(f"{sid}/retry")
        if kind in ("listen", "recall") and body.get("onTimeout"):
            handlers.append(f"{sid}/onTimeout")
            if did(sid, "step_timed_out"):
                handled.add(f"{sid}/onTimeout")
        if node.get("onCompensate"):
            handlers.append(f"{sid}/onCompensate")
            if any(
                sid in (d.detail.get("steps") or ())
                for d in decisions
                if d.kind == "compensation_started"
            ):
                handled.add(f"{sid}/onCompensate")
        for index, _ in enumerate(body.get("escalations") or () if isinstance(body, dict) else ()):
            handlers.append(f"{sid}/escalations/{index}")
            if any(d.get("level") == index + 1 for d in matching("escalated", sid)):
                handled.add(f"{sid}/escalations/{index}")

    for table in spec.get("decisions") or ():
        for index, _ in enumerate(table.get("rules") or ()):
            rows.append(f"{table['id']}/{index}")
    for decision in decisions:
        if decision.kind == "table_decided":
            for rule in decision.detail.get("rules") or ():
                answered.add(f"{decision.detail.get('table')}/{rule}")

    return Coverage(
        process=definition.key,
        version=definition.version,
        elements=_counter(elements, reached),
        transitions=_counter(transitions, passed),
        decision_rows=_counter(rows, answered),
        handlers=_counter(handlers, handled),
    )


def package_coverage(
    definitions: Iterable[engine.Definition], results: Sequence[TestResult]
) -> list[Coverage]:
    """The coverage of each process by all tests together."""
    out = []
    for definition in definitions:
        decisions = [
            decision
            for result in results
            for key, decision in result.decisions
            if key == definition.key
        ]
        out.append(process_coverage(definition, decisions))
    return out
