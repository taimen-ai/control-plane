"""The process engine: one pure step function (CP-ADR-0074 §4-§8, CP-ADR-0076 §4-§8).

``step(definition, state, input) -> (state', decisions, intents)``

- **No I/O.** No database, no HTTP, no clock, no randomness. The engine's time
  is the time of its input (``Input.at``): the journal event's time or the
  ``due_at`` of a fired timer. Ids the engine makes (activities, timers,
  recalls) are UUIDv5 of ``(instance, seq, index)``; the same inputs give the
  same ids, decisions and intents — in a live run, a package test and a replay.
- **State** is a plain JSON object (it is stored as ``jsonb``): the instance's
  data, its stages and milestones, the threads that execute blocks with their
  stacks, the activities they wait on, the timers, the completed steps that
  can be compensated. Nothing in it depends on key order.
- **Decisions** are the records of the instance journal: what the engine
  decided and why (which guard, which table rule, which input).
- **Intents** are what has to happen outside the state — tasks, approvals,
  skill calls, recall, remember, timers, ``process.*`` events. The engine does
  not know how they are carried out; the application layer executes them in
  the transaction that stores ``state'`` (CP-ADR-0074 §6).

Two levels (TAI-ADR-0054 R1):

- **the case** — stages with ``entry``/``exit`` guards, milestones, timers of
  a stage or of the process, discretionary work an operator adds;
- **blocks** — ``do``, ``fork`` (``all``/``compete``), ``listen`` (the first
  of several events, with a timeout), ``try``/``catch``/``retry``, ``wait``,
  ``call`` (skill, agent, nested process), ``set``, ``raise``,
  ``compensate``, ``decide``, ``recall``, ``remember``, ``human``,
  ``approve``, ``suspend``, ``resume``, ``complete``.

A running block is a *thread*: a stack of frames (a sequence at a position,
an open ``try``, a ``fork`` waiting for its branches, a running compensation).
A step that needs the world — a task, a vote, a skill, an event, time — opens
an *activity* and the thread waits on it; the input that answers the activity
continues the thread. Ending a scope (a stage's exit, a ``compete`` branch
that lost, ``complete``, a cancel, an unhandled error) ends its threads and
cancels what their activities opened: the cancellation scope.

Errors are RFC 7807 objects ``{type, status, detail}`` (:class:`ProcessError`).
One raised in a thread goes up its stack to the nearest ``try`` that retries
or catches it, then to the ``fork`` that started the thread; with no handler
the instance fails (``process.failed``). An error of a guard or of the memory
projection has no thread: a guard fails the instance, a projection error is
recorded and the projection field left out.

Pure functions over plain values; no I/O.
"""

import contextlib
import copy
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import Message
from jsonschema import Draft202012Validator

from control_plane.domain import decision_table
from control_plane.domain import process_sla as sla
from control_plane.domain.calendar import Calendar
from control_plane.domain.cel_profile import (
    DEFAULT_COST_LIMIT,
    ExpressionError,
    Program,
    parse_iso_duration,
)
from control_plane.domain.process_definition import (
    DEFAULT_RETROSPECTIVE_SKILL,
    Catalog,
    Problem,
    check_process,
    pointer,
    step_kind,
)
from control_plane.domain.settings_refs import NONE, SettingsScope

# Revisions of the engine's semantics (CP-ADR-0074, amendment 2026-09-29): a
# version runs under the revision it was published with, and an instance moves
# to another one only by a migration. ``1`` — versions published before SLA
# deadlines; ``2`` — every version published since: SLA deadlines of steps
# and of the process (timers ``sla``/``sla_warning``, ``process.sla_*``).
ENGINE_REVISIONS = (1, 2)
ENGINE_REVISION = ENGINE_REVISIONS[-1]
# The first revision with SLA deadlines (CP-ADR-0078 §3).
SLA_REVISION = 2
# An escalation level fired; its ``to`` targets are text, resolved into
# addressees by the application when it records the event.
ESCALATED = "process.escalated"

# Ids of the engine: UUIDv5 in this namespace of "<instance>:<seq>:<index>".
ID_NAMESPACE = uuid.UUID("5d0c7a8e-3f5b-5a4e-9c61-0e7c1f4b2a90")

# A recall without its own timeout does not wait forever (spec, edge cases).
DEFAULT_RECALL_TIMEOUT = timedelta(minutes=10)
# Frames the engine may execute in one step: a bound, not a budget to plan by.
MAX_ACTIONS = 10_000
# Leaf paths of a data change listed in changedFields.
MAX_CHANGED_FIELDS = 200
# Entities of the case the retrospective skill takes (process.retrospective@1).
RETROSPECTIVE_ENTITIES = 200

RUNNING = "running"
SUSPENDED = "suspended"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
CLOSED = (COMPLETED, FAILED, CANCELLED)

INPUT_KINDS = (
    "start",
    "event",
    "task",
    "approval",
    "skill",
    "child",
    "recall",
    "timer",
    "intent_failed",
    "calendar",
    "command",
    "migrated",
)
COMMANDS = ("suspend", "resume", "cancel", "start_discretionary")
# The trigger type of an instance started without an event (``POST
# /process-instances`` or a parent's ``call``): its start input names the key.
COMMAND_TRIGGER = "command"

# Inputs that answer an activity: while the instance is suspended they wait.
_ACTIVITY_INPUTS = ("task", "approval", "skill", "child", "recall", "intent_failed")
_WAITING_STEPS = ("human", "approve", "call", "recall", "listen", "wait")
_RESULT_STEPS = ("human", "approve", "call", "decide", "recall", "listen")
# Step kind -> the kinds of its activities that wait with the step's due.
_DUE_ACTIVITIES: Mapping[str, tuple[str, ...]] = {
    "human": ("task",),
    "approve": ("approval",),
    "call": ("skill", "agent", "child"),
    "recall": ("recall",),
    "listen": ("listen",),
}


# --- values in and out ----------------------------------------------------------------


class DefinitionError(ValueError):
    """The definition does not pass the check: the engine runs checked versions only."""

    def __init__(self, key: str, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        first = self.problems[0] if self.problems else None
        where = f": {first.code} at {first.path}" if first else ""
        super().__init__(f"process {key!r} does not pass the check{where}")


class EngineError(ValueError):
    """An input the engine cannot take: a programming error of the caller."""


@dataclass(frozen=True)
class ProcessError:
    """An error of the process in the form of RFC 7807: ``type``, ``status``, ``detail``."""

    type: str
    status: int | None = None
    detail: str | None = None
    element: str | None = None

    def out(self) -> dict[str, Any]:
        return {"type": self.type, "status": self.status, "detail": self.detail}

    @classmethod
    def of(cls, value: Mapping[str, Any], element: str | None = None) -> "ProcessError":
        return cls(
            str(value.get("type") or "error"), value.get("status"), value.get("detail"), element
        )


@dataclass(frozen=True)
class Input:
    """One input of the engine (CP-ADR-0074 §5).

    ``kind`` and ``body``:

    - ``start`` — ``{instanceId, event}``: the start trigger matched. On an
      existing state it is a repeat of the start event: ``process.correlated``;
    - ``event`` — ``{event}``: a journal event or an observation routed to the
      instance (``correlate``, ``onEvent``, ``listen``);
    - ``task`` — ``{activityId, status: completed|cancelled, task}``;
    - ``approval`` — ``{activityId, approvalId, outcome:
      approved|rejected|cancelled, principal, total}``: ``total`` — how many
      approvals of the activity are open or decided (not cancelled);
    - ``skill`` — ``{activityId, status: succeeded|failed, output, error}``;
    - ``child`` — ``{activityId, status: completed|failed|cancelled, outcome,
      data, error}``: a nested process ended;
    - ``recall`` — ``{activityId, status: completed|timed_out, result,
      reason}``: the answer of memory, recorded whole in the journal;
    - ``timer`` — ``{timerId, detectedAt?}``: ``detectedAt`` — when the core
      noticed the timer (the input's time is its due), the ``detectedAt`` of
      a breached SLA deadline;
    - ``intent_failed`` — ``{activityId?, intent, code, detail}``: a command
      refused an intent (CP-ADR-0074 §6);
    - ``calendar`` — ``{key}``: a new version of a calendar was published;
    - ``command`` — ``{action: suspend|resume|cancel|start_discretionary,
      reason, stage, step}`` of an operator.

    An event document is ``{id, type, time, entityType, entityId, actorId,
    correlationId, payload}``; an observation adds ``observation`` (its kind)
    and ``source``. ``calendars`` — the calendar versions the input is
    evaluated with (the journal records them; a replay passes the same ones).
    ``settings`` — the effective settings of the process's package the input
    is evaluated with (CP-ADR-0081 §6): read once per step by the application
    layer, recorded in the journal by version, the same ones in a replay.
    Neither is part of :meth:`out`.
    """

    kind: str
    at: datetime
    body: Mapping[str, Any] = field(default_factory=dict)
    actor_id: str | None = None
    calendars: Mapping[str, Calendar] = field(default_factory=dict)
    settings: Mapping[str, Any] | None = None

    def out(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": _rfc3339(self.at),
            "body": _jsonable(self.body),
            "actorId": self.actor_id,
        }


@dataclass(frozen=True)
class Decision:
    """A record of the instance journal: what the engine decided and why."""

    kind: str
    element: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        return {"kind": self.kind, "element": self.element, **_jsonable(self.detail)}


@dataclass(frozen=True)
class Intent:
    """What has to happen outside the state; the application layer executes it."""

    kind: str
    body: Mapping[str, Any] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        return {"kind": self.kind, **_jsonable(self.body)}


# --- the definition --------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    id: str
    path: str
    node: Mapping[str, Any]
    stage: str | None


@dataclass(frozen=True)
class _Timer:
    id: str
    path: str
    node: Mapping[str, Any]
    stage: str | None


@dataclass(frozen=True)
class Definition:
    """A checked process version, ready to run: build it with :meth:`build`.

    Expressions are compiled once, typed as the check typed them
    (``programs`` by JSON pointer); blocks are addressed by keys made of
    element ids (``stage:<id>/steps``, ``step:<id>/try``, ``branch:<id>``…),
    so a thread's position names elements, not places in a file.
    ``engine_revision`` is the revision of the semantics the version runs
    under: the one of its record (``process_definitions.engine_revision``),
    the latest for a spec a publication would create as a new version.
    """

    key: str
    spec: Mapping[str, Any]
    programs: Mapping[str, Program]
    tables: Mapping[str, decision_table.Table]
    blocks: Mapping[str, tuple[str, tuple[Mapping[str, Any], ...]]]
    steps: Mapping[str, _Step]
    timers: Mapping[str, _Timer]
    stage_ids: tuple[str, ...]
    cost_limit: int = DEFAULT_COST_LIMIT
    engine_revision: int = ENGINE_REVISION
    # Some expression reads ``settings`` (CP-ADR-0081 §6); the type it was compiled with.
    reads_settings: bool = False
    settings: SettingsScope = NONE

    def __post_init__(self) -> None:
        if self.engine_revision not in ENGINE_REVISIONS:
            raise EngineError(f"unknown engine revision {self.engine_revision!r}")

    @property
    def version(self) -> int:
        return int(self.spec["version"])

    @classmethod
    def build(
        cls,
        key: str,
        spec: Mapping[str, Any],
        catalog: Catalog,
        *,
        cost_limit: int = DEFAULT_COST_LIMIT,
        engine_revision: int = ENGINE_REVISION,
    ) -> "Definition":
        """Check ``spec`` (a normalized, published spec) and compile it for the engine."""
        checked = check_process(key, spec, catalog)
        if checked.errors:
            raise DefinitionError(key, checked.errors)
        blocks: dict[str, tuple[str, tuple[Mapping[str, Any], ...]]] = {}
        steps: dict[str, _Step] = {}
        timers: dict[str, _Timer] = {}

        def block(name: str, path: str, items: Any, stage: str | None) -> None:
            blocks[name] = (path, tuple(items or ()))
            for index, item in enumerate(items or ()):
                walk(f"{path}/{index}", item, stage)

        def walk(path: str, node: Mapping[str, Any], stage: str | None) -> None:
            sid = node["id"]
            steps[sid] = _Step(sid, path, node, stage)
            blocks[f"step:{sid}"] = (path, (node,))
            kind = step_kind(node)
            body = node[kind]
            here = f"{path}/{kind}"
            if node.get("onCompensate"):
                block(
                    f"step:{sid}/onCompensate", path + "/onCompensate", node["onCompensate"], stage
                )
            if kind == "do":
                block(f"step:{sid}/do", here, body, stage)
            elif kind == "try":
                block(f"step:{sid}/try", here + "/do", body["do"], stage)
                for index, clause in enumerate(body.get("catch") or ()):
                    block(
                        f"step:{sid}/catch/{index}", f"{here}/catch/{index}/do", clause["do"], stage
                    )
            elif kind == "fork":
                for index, branch in enumerate(body["branches"]):
                    block(
                        f"branch:{branch['id']}", f"{here}/branches/{index}/do", branch["do"], stage
                    )
            elif kind == "listen":
                for index, option in enumerate(body["any"]):
                    block(
                        f"step:{sid}/any/{index}", f"{here}/any/{index}/do", option.get("do"), stage
                    )
                block(f"step:{sid}/onTimeout", here + "/onTimeout", body.get("onTimeout"), stage)
            elif kind == "recall":
                block(f"step:{sid}/onTimeout", here + "/onTimeout", body.get("onTimeout"), stage)

        def boundary(items: Any, path: str, stage: str | None) -> None:
            for index, timer in enumerate(items or ()):
                here = f"{path}/{index}"
                timers[timer["id"]] = _Timer(timer["id"], here, timer, stage)
                block(f"timer:{timer['id']}", here + "/do", timer["do"], stage)

        for index, item in enumerate(spec.get("correlate") or ()):
            block(
                f"correlate:{index}",
                pointer("spec", "correlate", index, "do"),
                item.get("do"),
                None,
            )
        for index, item in enumerate(spec.get("onEvent") or ()):
            block(f"onEvent:{index}", pointer("spec", "onEvent", index, "do"), item["do"], None)
        boundary(spec.get("timers"), "/spec/timers", None)
        for index, stage in enumerate(spec["stages"]):
            path = pointer("spec", "stages", index)
            block(f"stage:{stage['id']}/steps", path + "/steps", stage["steps"], stage["id"])
            for number, item in enumerate(stage.get("discretionary") or ()):
                walk(f"{path}/discretionary/{number}", item, stage["id"])
            boundary(stage.get("timers"), path + "/timers", stage["id"])
        return cls(
            key=key,
            spec=spec,
            programs=dict(checked.programs),
            tables=dict(checked.tables),
            blocks=blocks,
            steps=steps,
            timers=timers,
            stage_ids=tuple(s["id"] for s in spec["stages"]),
            cost_limit=cost_limit,
            engine_revision=engine_revision,
            reads_settings=checked.reads_settings,
            settings=catalog.settings,
        )

    def stage(self, stage_id: str) -> tuple[str, Mapping[str, Any]]:
        for index, stage in enumerate(self.spec["stages"]):
            if stage["id"] == stage_id:
                return pointer("spec", "stages", index), stage
        raise EngineError(f"the process has no stage {stage_id!r}")


# --- helpers for the application layer --------------------------------------------------


def trigger_matches(
    definition: Definition,
    trigger: Mapping[str, Any],
    path: str,
    event: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> bool:
    """Whether ``event`` is the trigger's event or observation and passes its ``where``.

    ``values`` — the other variables the ``where`` may read (the instance's
    data, the settings of the package).
    """
    if trigger.get("event") is not None:
        if event.get("observation") is not None or event.get("type") != trigger["event"]:
            return False
    elif event.get("observation") != trigger.get("observation"):
        return False
    source = trigger.get("source")
    if source is not None and event.get("source") != source:
        return False
    if trigger.get("where") is None:
        return True
    program = definition.programs[path + "/where"]
    try:
        value = program.evaluate(
            {**(values or {}), "event": _event_var(event)}, cost_limit=definition.cost_limit
        )
    except ExpressionError:
        return False
    return value.value is True


def _settings_values(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"settings": settings} if settings is not None else {}


def start_key(
    definition: Definition, event: Mapping[str, Any], settings: Mapping[str, Any] | None = None
) -> str | None:
    """The instance key ``event`` starts (or reaches), ``None`` when it is not the start trigger.

    ``settings`` — the effective settings of the package, when the process reads them.
    """
    start = definition.spec["start"]
    values = _settings_values(settings)
    if not trigger_matches(definition, start["on"], "/spec/start/on", event, values):
        return None
    return _key_of(definition, "/spec/start/key", event, values)


def correlation_keys(
    definition: Definition, event: Mapping[str, Any], settings: Mapping[str, Any] | None = None
) -> list[str]:
    """Instance keys ``event`` reaches through ``correlate``, in declaration order."""
    keys: list[str] = []
    values = _settings_values(settings)
    for index, item in enumerate(definition.spec.get("correlate") or ()):
        path = pointer("spec", "correlate", index)
        if trigger_matches(definition, item["on"], path + "/on", event, values):
            key = _key_of(definition, path + "/key", event, values)
            if key is not None and key not in keys:
                keys.append(key)
    return keys


def _key_of(
    definition: Definition,
    path: str,
    event: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> str | None:
    try:
        value = definition.programs[path].evaluate(
            {**(values or {}), "event": _event_var(event)}, cost_limit=definition.cost_limit
        )
    except ExpressionError:
        return None
    return _key_text(value.value)


def _key_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value)
    return text or None


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def new_id(instance_id: str, seq: int, index: int) -> str:
    """The id the engine gives to the ``index``-th thing it makes at step ``seq``."""
    return str(uuid.uuid5(ID_NAMESPACE, f"{instance_id}:{seq}:{index}"))


# --- the step ----------------------------------------------------------------------------


def step(
    definition: Definition, state: Mapping[str, Any] | None, input: Input
) -> tuple[dict[str, Any], list[Decision], list[Intent]]:
    """Take one input: the next state, the decisions made and the intents to execute.

    ``state`` is ``None`` before the start (then ``input`` is ``start``). The
    given state is not changed.
    """
    if input.kind not in INPUT_KINDS:
        raise EngineError(f"unknown input kind {input.kind!r}")
    if input.at.tzinfo is None:
        raise EngineError("the time of an input carries its UTC offset")
    engine = _Engine(definition, state, input)
    engine.take()
    return engine.state, engine.decisions, engine.intents


def owner_chain(
    definition: Definition, state: Mapping[str, Any], at: datetime
) -> list[dict[str, Any]]:
    """``spec.owner`` of an instance in ``state``, its expressions computed at ``at``.

    The candidates an SLA event addresses, for the application to resolve
    elsewhere: the owner of an approval nobody may decide (CP-ADR-0074 §7).
    """
    return _Engine(definition, state, Input("event", at)).owner_chain()


class _Raised(Exception):
    def __init__(self, error: ProcessError) -> None:
        super().__init__(error.type)
        self.error = error


class _Engine:
    def __init__(
        self, definition: Definition, state: Mapping[str, Any] | None, input: Input
    ) -> None:
        self.d = definition
        self.input = input
        self.now = input.at.astimezone(UTC)
        self.state: dict[str, Any] = copy.deepcopy(dict(state)) if state is not None else {}
        self.decisions: list[Decision] = []
        self.intents: list[Intent] = []
        self.made = 0
        self.actions = 0
        self.changed: set[str] = set()
        self.changed_by: str | None = None
        self.flipped: set[str] = set()

    # --- plumbing -------------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        return str(self.state["instanceId"])

    @property
    def sla_on(self) -> bool:
        """Whether the version runs with SLA deadlines (CP-ADR-0078 §3)."""
        return self.d.engine_revision >= SLA_REVISION

    def decide(self, kind: str, element: str | None = None, **detail: Any) -> None:
        self.decisions.append(Decision(kind, element, detail))

    def intent(self, kind: str, **body: Any) -> None:
        self.intents.append(Intent(kind, body))

    def emit(
        self,
        event_type: str,
        *,
        addressees: Mapping[str, Any] | None = None,
        **payload: Any,
    ) -> None:
        common = {
            "instanceId": self.instance_id,
            "definitionKey": self.d.key,
            "version": self.state["version"],
            "instanceKey": self.state["key"],
        }
        if addressees is None:
            self.intent("emit_event", type=event_type, payload={**common, **payload})
            return
        # Candidate chains with expressions computed; the application resolves
        # them into the payload's addressees (CP-ADR-0078 §3).
        self.intent(
            "emit_event", type=event_type, payload={**common, **payload}, addressees=addressees
        )

    def make_id(self) -> str:
        made = new_id(self.instance_id, self.state["seq"], self.made)
        self.made += 1
        return made

    def counter(self, name: str) -> int:
        self.state["counters"][name] += 1
        return int(self.state["counters"][name])

    def tick(self) -> None:
        self.actions += 1
        if self.actions > MAX_ACTIONS:
            raise _Raised(
                ProcessError(
                    "step_limit_exceeded",
                    500,
                    f"one input made the engine execute more than {MAX_ACTIONS} steps",
                )
            )

    # --- values of expressions ------------------------------------------------------

    def values(self, thread: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        state = self.state
        stages = {
            sid: {
                "completed": state["stages"][sid]["state"] == COMPLETED,
                "active": state["stages"][sid]["state"] == "active",
            }
            for sid in self.d.stage_ids
        }
        values: dict[str, Any] = {
            "data": state["data"],
            "event": _event_var((thread or {}).get("event") or self.input_event() or {}),
            "step": (thread or {}).get("step") or {},
            "task": (thread or {}).get("task") or state.get("lastTask") or {},
            "stage": stages,
            "instance": {
                "id": state["instanceId"],
                "key": state["key"],
                "version": state["version"],
                "startedAt": state["startedAt"],
                "clock": _rfc3339(self.now),
            },
            "milestone": {m: bool(v) for m, v in state["milestones"].items()},
            **_settings_values(self.settings()),
        }
        for frame in (thread or {}).get("stack") or ():
            values.update(frame.get("bindings") or {})
            if frame.get("stepVar") is not None:
                # A compensation frame of an earlier engine: its step is the compensated one.
                values["compensated"] = frame["stepVar"]
        values.update(extra)
        return values

    def settings(self) -> Mapping[str, Any] | None:
        """The settings of the input, for a definition that reads them."""
        if not self.d.reads_settings:
            return None
        return self.input.settings if self.input.settings is not None else {}

    def matches(self, trigger: Mapping[str, Any], path: str, event: Mapping[str, Any]) -> bool:
        return trigger_matches(self.d, trigger, path, event, self.values())

    def input_event(self) -> Mapping[str, Any] | None:
        event = self.input.body.get("event")
        return event if isinstance(event, Mapping) else None

    def evaluate(self, path: str, values: Mapping[str, Any], element: str | None) -> Any:
        """The value of the expression at ``path``: a native value (datetimes stay datetimes)."""
        value, _ = self.evaluate_marked(path, values, element)
        return value

    def evaluate_marked(
        self, path: str, values: Mapping[str, Any], element: str | None
    ) -> tuple[Any, bool]:
        program = self.d.programs.get(path)
        if program is None:
            raise EngineError(f"no compiled expression at {path}")
        try:
            result = program.evaluate(
                values, calendars=self.input.calendars, cost_limit=self.d.cost_limit
            )
        except ExpressionError as exc:
            raise _Raised(ProcessError(exc.code, 422, f"{path}: {exc.message}", element)) from None
        return _native(result.value), result.provisional

    def evaluate_map(
        self, path: str, mapping: Mapping[str, Any] | None, values: Mapping[str, Any], element: str
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in mapping or {}:
            _put(out, name, _jsonable(self.evaluate(f"{path}{pointer(name)}", values, element)))
        return out

    # --- data -------------------------------------------------------------------------

    def write(
        self, path: str, mapping: Mapping[str, Any] | None, values: dict[str, Any], element: str
    ) -> None:
        """Write a ``celMap`` into the data; every value is computed before any is written."""
        computed = [
            (name, _jsonable(self.evaluate(f"{path}{pointer(name)}", values, element)))
            for name in mapping or {}
        ]
        before = copy.deepcopy(self.state["data"])
        for name, value in computed:
            _put(self.state["data"], name, value)
        changed = _diff(before, self.state["data"])
        if changed:
            self.changed.update(changed)
            self.changed_by = element
            self.decide("data_set", element, fields=sorted(changed))

    # --- taking an input --------------------------------------------------------------

    def take(self) -> None:
        kind = self.input.kind
        if not self.state:
            if kind != "start":
                raise EngineError(f"an instance starts with a start input, not {kind!r}")
            self.start()
            return
        self.state["seq"] += 1
        self.state["clock"] = _rfc3339(self.now)
        if kind == "command":
            self.command()
        elif self.state["status"] in CLOSED:
            if not self.retrospective_input():
                self.decide("ignored", reason="instance_closed", input=kind)
        elif kind == "start":
            self.repeat_start()
        elif kind == "calendar":
            self.recompute_timers(cause="calendar_changed", calendar=self.input.body.get("key"))
            self.refreeze(self.input.body.get("key"))
        elif kind == "migrated":
            self.migrated()
        elif self.state["status"] == SUSPENDED and self.defer():
            pass
        else:
            self.dispatch(kind, self.input.body)
        self.drive()

    def dispatch(self, kind: str, body: Mapping[str, Any]) -> None:
        if kind == "event":
            self.event(body["event"])
        elif kind == "timer":
            self.timer_fired(str(body["timerId"]), body)
        elif kind == "intent_failed":
            self.intent_failed(body)
        else:
            self.answer(kind, body)

    def defer(self) -> bool:
        """While suspended, answers to the work of stages wait for the resume; events run."""
        kind = self.input.kind
        if kind not in _ACTIVITY_INPUTS:
            return False
        activity = self.state["activities"].get(str(self.input.body.get("activityId") or ""))
        if activity is None or not self.thread_paused(activity["thread"]):
            return False
        self.state["deferred"].append(self.input.out())
        self.decide("deferred", activity["element"], reason="suspended", input=kind)
        return True

    # --- start and correlate ----------------------------------------------------------

    def start(self) -> None:
        body = self.input.body
        event = body.get("event") or {}
        instance_id = str(body.get("instanceId") or "")
        if not instance_id:
            raise EngineError("a start input names the instance: body.instanceId")
        # An explicit start (an operator or a parent process) names the key and
        # the initial data itself; there is no trigger event to evaluate.
        explicit = body.get("key") is not None
        key = _key_text(body["key"]) if explicit else start_key(self.d, event, self.settings())
        if key is None:
            raise EngineError("the event is not the start trigger of the process")
        self.state = {
            "instanceId": instance_id,
            "definitionKey": self.d.key,
            "version": self.d.version,
            "key": key,
            "status": RUNNING,
            "outcome": None,
            "error": None,
            "attention": None,
            "closing": None,
            "startedAt": _rfc3339(self.now),
            "clock": _rfc3339(self.now),
            "seq": 0,
            "counters": {"thread": 0, "activity": 0, "timer": 0, "done": 0},
            "data": copy.deepcopy(dict(body.get("data") or {})) if explicit else {},
            "stages": {
                sid: {"state": "available", "enteredAt": None, "runs": 0, "closedSeq": None}
                for sid in self.d.stage_ids
            },
            "milestones": {},
            "threads": {},
            "activities": {},
            "timers": {},
            "done": [],
            "deferred": [],
            "lastTask": None,
            "retrospective": None,
        }
        if explicit:
            event = {"type": COMMAND_TRIGGER}
        else:
            values = self.values(event=_event_var(event))
            try:
                self.write("/spec/start/set", self.d.spec["start"].get("set"), values, "start")
            except _Raised as raised:
                self.decide("started", key=key, trigger=_trigger_ref(event))
                self.fail(raised.error)
                return
        self.changed.clear()  # the start's data is the initial data, not a change
        self.decide("started", key=key, trigger=_trigger_ref(event))
        self.emit(
            "process.started",
            triggerEventId=event.get("id"),
            triggerType=_trigger_type(event),
            memory=self.projection(),
        )
        for timer in self.d.timers.values():
            if timer.stage is None:
                self.boundary_timer(timer)
        if self.sla_on and self.d.spec.get("due") is not None:
            self.state["sla"] = self.deadline(
                sla.PROCESS, sla.PROCESS, "/spec/due", self.d.spec["due"], self.values(), None, None
            )
        self.drive()

    def repeat_start(self) -> None:
        """The start event again for an existing key: it reaches the instance, not a new one."""
        event = self.input.body.get("event") or {}
        if not self.correlate(event):
            self.decide("correlated", "start", trigger=_trigger_ref(event), fields=[])
            self.emit(
                "process.correlated",
                triggerEventId=event.get("id"),
                triggerType=_trigger_type(event),
                changedFields=[],
            )

    def correlate(self, event: Mapping[str, Any]) -> bool:
        matched = False
        for index, item in enumerate(self.d.spec.get("correlate") or ()):
            path = pointer("spec", "correlate", index)
            if not self.matches(item["on"], path + "/on", event):
                continue
            if _key_of(self.d, path + "/key", event) != self.state["key"]:
                continue
            matched = True
            before = set(self.changed)
            self.changed.clear()
            values = self.values(event=_event_var(event))
            try:
                self.write(path + "/set", item.get("set"), values, f"correlate:{index}")
            except _Raised as raised:
                self.fail(raised.error)
                return True
            fields = sorted(self.changed)
            self.changed |= before
            self.decide(
                "correlated", f"correlate:{index}", trigger=_trigger_ref(event), fields=fields
            )
            self.emit(
                "process.correlated",
                triggerEventId=event.get("id"),
                triggerType=_trigger_type(event),
                changedFields=fields,
            )
            if item.get("do"):
                self.spawn(f"correlate:{index}", scope="process", event=event)
        return matched

    def event(self, event: Mapping[str, Any]) -> None:
        matched = self.correlate(event)
        if self.state["status"] in CLOSED:
            return
        for index, item in enumerate(self.d.spec.get("onEvent") or ()):
            path = pointer("spec", "onEvent", index)
            if self.matches(item["on"], path + "/on", event):
                matched = True
                self.decide("event_matched", f"onEvent:{index}", trigger=_trigger_ref(event))
                self.spawn(f"onEvent:{index}", scope="process", event=event)
        for activity in self.sorted_activities("listen"):
            if self.state["status"] == SUSPENDED and self.thread_paused(activity["thread"]):
                self.state["deferred"].append(self.input.out())
                self.decide("deferred", activity["element"], reason="suspended", input="event")
                matched = True
                break
            if self.listen_matched(activity, event):
                matched = True
        if not matched:
            self.decide("ignored", reason="no_match", trigger=_trigger_ref(event))

    # --- threads ----------------------------------------------------------------------

    def spawn(
        self,
        block: str,
        *,
        scope: str,
        parent: str | None = None,
        event: Mapping[str, Any] | None = None,
        frames: Sequence[Mapping[str, Any]] = (),
        step_var: Mapping[str, Any] | None = None,
    ) -> str:
        number = self.counter("thread")
        tid = f"t{number}"
        stack = [dict(frame) for frame in frames]
        if block:
            stack.append({"kind": "seq", "block": block, "index": 0})
        self.state["threads"][tid] = {
            "id": tid,
            "n": number,
            "scope": scope,
            "parent": parent,
            "stack": stack,
            "wait": None,
            "event": dict(event) if event is not None else self.input_event(),
            "step": dict(step_var) if step_var is not None else None,
            "task": None,
        }
        return tid

    def thread_paused(self, tid: str) -> bool:
        """While suspended, the work of stages waits; blocks of events and timers run."""
        thread = self.state["threads"].get(tid)
        if thread is None or self.state["status"] != SUSPENDED:
            return False
        return str(thread["scope"]).startswith("stage:")

    def drive(self) -> None:
        """Run threads and the case until nothing moves; then timers and the data event."""
        try:
            while self.state["status"] not in CLOSED:
                moved = False
                for thread in sorted(self.state["threads"].values(), key=lambda t: t["n"]):
                    if thread["id"] in self.state["threads"] and self.runnable(thread):
                        self.run(thread["id"])
                        moved = True
                if self.state["status"] in CLOSED:
                    break
                if self.case():
                    moved = True
                if not moved:
                    break
        except _Raised as raised:
            self.fail(raised.error)
        self.flush_changes(recompute=self.state["status"] not in (FAILED, CANCELLED))
        if (
            self.state["status"] == RUNNING
            and self.state["closing"] is None
            and not self.state["threads"]
            and all(s["state"] == COMPLETED for s in self.state["stages"].values())
        ):
            self.complete(None, "completed")

    def flush_changes(self, *, recompute: bool) -> None:
        """One ``process.data_changed`` for what the input changed; timers that read it move."""
        if not self.changed:
            return
        fields = sorted(self.changed)[:MAX_CHANGED_FIELDS]
        if recompute:
            self.recompute_timers(cause="data_changed", fields=fields)
        self.emit(
            "process.data_changed",
            changedFields=fields,
            element=self.changed_by,
            memory=self.projection(),
        )
        self.changed.clear()

    def runnable(self, thread: Mapping[str, Any]) -> bool:
        return thread["wait"] is None and not self.thread_paused(thread["id"])

    def run(self, tid: str) -> None:
        while True:
            thread = self.state["threads"].get(tid)
            if thread is None or not self.runnable(thread) or self.state["status"] in CLOSED:
                return
            self.tick()
            stack = thread["stack"]
            if not stack:
                self.finish_thread(tid)
                return
            frame = stack[-1]
            if frame["kind"] != "seq":
                self.close_frame(tid, frame)
                continue
            _, items = self.d.blocks[frame["block"]]
            if frame["index"] >= len(items):
                stack.pop()
                continue
            node = items[frame["index"]]
            frame["index"] += 1
            try:
                self.execute(tid, node)
            except _Raised as raised:
                self.raise_in(tid, raised.error)

    def close_frame(self, tid: str, frame: Mapping[str, Any]) -> None:
        thread = self.state["threads"][tid]
        thread["stack"].pop()
        if frame["kind"] == "compensation":
            self.decide(
                "compensated", frame.get("element"), scope=frame["scope"], steps=frame["steps"]
            )
            self.emit("process.compensated", scope=frame["scope"], steps=frame["steps"])
        elif frame["kind"] == "try" and frame.get("phase") == "catch":
            self.decide("error_handled", frame["step"])

    def finish_thread(self, tid: str) -> None:
        thread = self.state["threads"].pop(tid)
        parent = thread["parent"]
        if parent is not None and parent in self.state["threads"]:
            self.branch_done(parent, thread)
        if thread["scope"] == "closing":
            self.cancelled()

    def end_thread(self, tid: str, reason: str) -> None:
        """End a thread and what it waits on: the cancellation scope."""
        thread = self.state["threads"].pop(tid, None)
        if thread is None:
            return
        for child in sorted(self.state["threads"].values(), key=lambda t: t["n"]):
            if child["parent"] == tid:
                self.end_thread(child["id"], reason)
        if thread["wait"] and thread["wait"] in self.state["activities"]:
            self.cancel_activity(thread["wait"], reason)

    # --- the case ---------------------------------------------------------------------

    def case(self) -> bool:
        """Milestones, stage exits and entries; ``True`` when something changed."""
        if self.state["status"] != RUNNING or self.state["closing"] is not None:
            return False
        moved = False
        for index, stage in enumerate(self.d.spec["stages"]):
            sid = stage["id"]
            path = pointer("spec", "stages", index)
            record = self.state["stages"][sid]
            if record["state"] == "active":
                for number, milestone in enumerate(stage.get("milestones") or ()):
                    mid = milestone["id"]
                    if mid in self.flipped:
                        continue
                    reached = bool(self.state["milestones"].get(mid))
                    holds = self.guard(f"{path}/milestones/{number}/when", mid)
                    if holds == reached:
                        continue
                    # A milestone follows its guard: reached when it holds, lost
                    # when it stops holding (a standing goal is met, then not).
                    # It changes at most once per input, so a guard that reads
                    # its own milestone cannot swing back and forth.
                    self.flipped.add(mid)
                    self.state["milestones"][mid] = holds
                    kind = "milestone_reached" if holds else "milestone_lost"
                    self.decide(kind, mid, stage=sid, when=milestone["when"])
                    self.emit(f"process.{kind}", milestone=mid, stage=sid)
                    moved = True
                live = any(t["scope"] == f"stage:{sid}" for t in self.state["threads"].values())
                if stage.get("exit") is not None:
                    if self.guard(path + "/exit", sid):
                        self.exit_stage(sid, "exit", stage["exit"])
                        moved = True
                elif not live:
                    self.exit_stage(sid, "work_done", None)
                    moved = True
            elif self.may_enter(stage, record):
                if stage.get("entry") is None or self.guard(path + "/entry", sid):
                    self.enter_stage(sid, stage)
                    moved = True
        return moved

    def may_enter(self, stage: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
        if record["state"] == "available":
            return True
        # A repeatable stage enters again on a later input once its entry holds again.
        return (
            record["state"] == COMPLETED
            and bool(stage.get("repeatable"))
            and stage.get("entry") is not None
            and record["closedSeq"] is not None
            and record["closedSeq"] < self.state["seq"]
        )

    def guard(self, path: str, element: str) -> bool:
        value = self.evaluate(path, self.values(), element)
        return value is True

    def enter_stage(self, sid: str, stage: Mapping[str, Any]) -> None:
        record = self.state["stages"][sid]
        record.update(state="active", enteredAt=_rfc3339(self.now), runs=record["runs"] + 1)
        self.decide("stage_entered", sid, entry=stage.get("entry"), run=record["runs"])
        self.emit("process.stage_entered", stage=sid)
        for timer in self.d.timers.values():
            if timer.stage == sid:
                self.boundary_timer(timer)
        self.spawn(f"stage:{sid}/steps", scope=f"stage:{sid}")

    def exit_stage(self, sid: str, cause: str, guard: str | None) -> None:
        self.end_scope(f"stage:{sid}", "stage_exited")
        record = self.state["stages"][sid]
        record.update(state=COMPLETED, closedSeq=self.state["seq"])
        self.decide("stage_exited", sid, cause=cause, exit=guard)
        self.emit("process.stage_exited", stage=sid)

    def end_scope(self, scope: str, reason: str) -> None:
        for thread in sorted(self.state["threads"].values(), key=lambda t: t["n"]):
            if thread["scope"] == scope and thread["parent"] is None:
                self.end_thread(thread["id"], reason)
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            if timer.get("scope") == scope:
                self.cancel_timer(timer["id"])

    # --- steps ------------------------------------------------------------------------

    def execute(self, tid: str, node: Mapping[str, Any]) -> None:
        sid = node["id"]
        entry = self.d.steps[sid]
        thread = self.state["threads"][tid]
        values = self.values(thread)
        if (
            node.get("when") is not None
            and self.evaluate(entry.path + "/when", values, sid) is not True
        ):
            self.decide("step_skipped", sid, when=node["when"])
            return
        kind = step_kind(node)
        handler = getattr(self, f"step_{kind}")
        handler(tid, entry, node[kind], values)

    def completed(
        self, tid: str, entry: _Step, result: Any, *, task: Mapping[str, Any] | None = None
    ) -> None:
        """A step gave its result: ``output``/``export`` into the data, compensation noted."""
        thread = self.state["threads"][tid]
        step_var = {
            "id": entry.id,
            "status": "completed",
            "result": _jsonable(result) if result is not None else {},
        }
        thread["step"] = step_var
        if task is not None:
            thread["task"] = dict(task)
            self.state["lastTask"] = dict(task)
        values = self.values(thread)
        for side in ("output", "export"):
            spec = entry.node.get(side)
            if isinstance(spec, dict) and spec.get("as"):
                self.write(f"{entry.path}/{side}/as", spec["as"], values, entry.id)
        self.decide("step_completed", entry.id)
        self.note_done(entry, step_var)

    def note_done(self, entry: _Step, step_var: Mapping[str, Any]) -> None:
        if entry.node.get("onCompensate"):
            self.state["done"].append(
                {
                    "n": self.counter("done"),
                    "step": entry.id,
                    "result": dict(step_var),
                    "compensated": False,
                }
            )

    def step_set(self, tid: str, entry: _Step, body: Any, values: dict[str, Any]) -> None:
        self.write(entry.path + "/set", body, values, entry.id)
        self.decide("step_completed", entry.id)
        self.note_done(entry, {"id": entry.id, "status": "completed", "result": {}})

    def step_do(self, tid: str, entry: _Step, body: Any, values: dict[str, Any]) -> None:
        self.state["threads"][tid]["stack"].append(
            {"kind": "seq", "block": f"step:{entry.id}/do", "index": 0}
        )

    def step_raise(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        detail = None
        if body.get("detail") is not None:
            detail = str(self.evaluate(entry.path + "/raise/detail", values, entry.id))
        raise _Raised(ProcessError(body["type"], body.get("status"), detail, entry.id))

    def step_complete(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        self.complete(entry.id, body.get("outcome") or "completed")

    def step_suspend(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        reason = ""
        if body.get("reason") is not None:
            reason = str(self.evaluate(entry.path + "/suspend/reason", values, entry.id))
        self.suspend("event", reason, entry.id)

    def step_resume(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        self.resume("event", entry.id)

    def step_decide(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        table_id = body["table"]
        table = self.d.tables[table_id]
        index = next(i for i, t in enumerate(self.d.spec["decisions"]) if t["id"] == table_id)
        spec = self.d.spec["decisions"][index]
        given = body.get("input") or {}
        inputs: dict[str, Any] = {}
        for number, item in enumerate(spec["inputs"]):
            if item["id"] in given:
                path = f"{entry.path}/decide/input{pointer(item['id'])}"
            else:
                path = pointer("spec", "decisions", index, "inputs", number, "expr")
            inputs[item["id"]] = _jsonable(self.evaluate(path, values, entry.id))
        try:
            answer = decision_table.evaluate(table, inputs)
        except decision_table.DecisionError as exc:
            self.decide(
                "table_decided", entry.id, table=table_id, inputs=inputs, rules=[], error=exc.code
            )
            raise _Raised(ProcessError(exc.code, 422, str(exc), entry.id)) from None
        result = {"items": answer.result} if table.hit_policy == "collect" else answer.result
        self.decide(
            "table_decided", entry.id, table=table_id, inputs=inputs, rules=list(answer.rules)
        )
        self.completed(tid, entry, result)

    def step_remember(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        here = entry.path + "/remember"
        what: dict[str, Any] = {}
        if body.get("facts") is not None:
            what["facts"] = self.evaluate_map(here + "/facts", body["facts"], values, entry.id)
        entity = body.get("entity")
        if isinstance(entity, dict):
            written: dict[str, Any] = {
                "kind": entity["kind"],
                "key": _key_text(self.evaluate(here + "/entity/key", values, entry.id)),
            }
            for name in ("name", "text"):
                if entity.get(name) is not None:
                    written[name] = _jsonable(
                        self.evaluate(f"{here}/entity/{name}", values, entry.id)
                    )
            written["links"] = [
                {
                    "rel": link["rel"],
                    "kind": link["kind"],
                    "key": _key_text(
                        self.evaluate(f"{here}/entity/links/{i}/key", values, entry.id)
                    ),
                }
                for i, link in enumerate(entity.get("links") or ())
            ]
            what["entity"] = written
        self.remember(entry.id, what)
        self.decide("step_completed", entry.id)
        self.note_done(entry, {"id": entry.id, "status": "completed", "result": {}})

    def remember(self, element: str, what: Mapping[str, Any]) -> None:
        self.intent(
            "remember",
            element=element,
            source=f"process:{self.d.key}",
            dedupKey=f"process:{self.instance_id}:{element}:{self.state['seq']}:{self.made}",
            case=self.case_key(),
            **what,
        )
        self.made += 1

    def step_compensate(self, tid: str, entry: _Step, body: Any, values: dict[str, Any]) -> None:
        self.compensate(tid, body, entry.id)

    def compensate(self, tid: str, targets: Any, element: str | None) -> list[str]:
        """Push the ``onCompensate`` blocks of completed steps: the last completed runs first."""
        chosen = [
            done
            for done in sorted(self.state["done"], key=lambda d: d["n"])
            if not done["compensated"] and (targets == "all" or done["step"] in targets)
        ]
        steps = [done["step"] for done in reversed(chosen)]
        scope = "all" if targets == "all" else ",".join(targets)
        stack = self.state["threads"][tid]["stack"]
        stack.append({"kind": "compensation", "scope": scope, "steps": steps, "element": element})
        for done in chosen:
            done["compensated"] = True
            stack.append(
                {
                    "kind": "seq",
                    "block": f"step:{done['step']}/onCompensate",
                    "index": 0,
                    # step stays the current step of the block, as in any block (CP-ADR-0074).
                    "bindings": {"compensated": done["result"]},
                }
            )
        self.decide("compensation_started", element, scope=scope, steps=steps)
        return steps

    def step_try(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        stack = self.state["threads"][tid]["stack"]
        stack.append({"kind": "try", "step": entry.id, "attempt": 0, "phase": "do"})
        stack.append({"kind": "seq", "block": f"step:{entry.id}/try", "index": 0})

    def step_fork(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        thread = self.state["threads"][tid]
        children = [
            self.spawn(
                f"branch:{branch['id']}",
                scope=thread["scope"],
                parent=tid,
                event=thread["event"],
            )
            for branch in body["branches"]
        ]
        thread["stack"].append(
            {
                "kind": "fork",
                "step": entry.id,
                "mode": body.get("mode") or "all",
                "children": children,
                "branches": [b["id"] for b in body["branches"]],
                "finished": [],
            }
        )
        thread["wait"] = "fork"
        self.decide(
            "fork_started",
            entry.id,
            mode=body.get("mode") or "all",
            branches=[b["id"] for b in body["branches"]],
        )

    def branch_done(self, parent: str, child: Mapping[str, Any]) -> None:
        thread = self.state["threads"][parent]
        frame = thread["stack"][-1]
        if frame["kind"] != "fork" or child["id"] not in frame["children"]:
            return
        branch = frame["branches"][frame["children"].index(child["id"])]
        frame["finished"].append(branch)
        if frame["mode"] == "compete":
            for other in frame["children"]:
                if other != child["id"]:
                    self.end_thread(other, "branch_lost")
            done = True
        else:
            done = len(frame["finished"]) == len(frame["children"])
        if done:
            thread["stack"].pop()
            thread["wait"] = None
            self.decide(
                "fork_completed", frame["step"], finished=frame["finished"], mode=frame["mode"]
            )
            self.note_done(
                self.d.steps[frame["step"]],
                {"id": frame["step"], "status": "completed", "result": {}},
            )

    # --- waiting steps ----------------------------------------------------------------

    def open_activity(
        self, tid: str, entry: _Step | None, kind: str, **fields: Any
    ) -> dict[str, Any]:
        aid = self.make_id()
        activity = {
            "id": aid,
            "n": self.counter("activity"),
            "kind": kind,
            "thread": tid,
            "element": entry.id if entry is not None else fields.pop("element", None),
            "openedAt": _rfc3339(self.now),
            "timers": [],
            **fields,
        }
        self.state["activities"][aid] = activity
        if tid in self.state["threads"]:
            self.state["threads"][tid]["wait"] = aid
        self.decide("activity_opened", activity["element"], activity=aid, activityKind=kind)
        return activity

    def close_activity(self, aid: str) -> dict[str, Any]:
        activity: dict[str, Any] = self.state["activities"].pop(aid)
        for timer_id in activity["timers"]:
            self.cancel_timer(timer_id)
        thread = self.state["threads"].get(activity["thread"])
        if thread is not None and thread["wait"] == aid:
            thread["wait"] = None
        return activity

    def cancel_activity(self, aid: str, reason: str) -> None:
        if aid not in self.state["activities"]:
            return
        activity = self.close_activity(aid)
        kind = activity["kind"]
        element = activity["element"]
        if kind in ("task", "agent", "retro_review"):
            self.intent("cancel_task", activityId=aid, element=element, reason=reason)
        elif kind == "approval":
            self.intent(
                "close_approvals", activityId=aid, element=element, outcome=None, reason=reason
            )
        elif kind == "child":
            self.intent("cancel_child", activityId=aid, element=element, reason=reason)
        self.decide("activity_cancelled", element, activity=aid, reason=reason)

    def step_wait(self, tid: str, entry: _Step, body: Any, values: dict[str, Any]) -> None:
        activity = self.open_activity(tid, entry, "wait")
        self.add_timer(activity, "wait", _recipe(body, entry.path + "/wait"), values)

    def step_human(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        here = entry.path + "/human"
        title = entry.node.get("displayName") or entry.id
        if body.get("title") is not None:
            title = str(self.evaluate(here + "/title", values, entry.id))
        assign = self.assignees(here + "/assign", body["assign"], values, entry.id)
        due, due_recipe, failed = self.due_of(here + "/due", body.get("due"), values, entry.id)
        prefill: dict[str, Any] = {}
        if body.get("customFields"):
            # The task's fields filled from the case (CP-ADR-0074 §7, amendment
            # 2026-10-01); a value that is null leaves the field to the person.
            computed = self.evaluate_map(
                here + "/customFields", body["customFields"], values, entry.id
            )
            prefill["customFields"] = {k: v for k, v in computed.items() if v is not None}
        activity = self.open_activity(tid, entry, "task", assign=assign, due=due)
        self.intent(
            "create_task",
            activityId=activity["id"],
            element=entry.id,
            taskType=body["taskType"],
            title=title,
            form=body.get("form"),
            assign=assign,
            due=due,
            escalations=self.escalation_plan(here, body.get("escalations"), values, entry.id),
            context=self.context_profile(here + "/context", body.get("context"), values, entry.id),
            input=self.step_input(entry, values),
            externalRef=self.external_ref(entry.id),
            **prefill,
        )
        self.open_sla(activity, here + "/due", body.get("due"), values, failed)
        self.escalation_timers(activity, here, body.get("escalations"), due_recipe, values, failed)

    def step_approve(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        here = entry.path + "/approve"
        approvers = self.assignees(here + "/approvers", body["approvers"], values, entry.id)
        excluded: Any = []
        if body.get("separationOfDuties") is not None:
            # Passed on as computed: an empty value is no exclusion to drop but
            # a refusal of the intent (CP-ADR-0074 §7), the application's to make.
            excluded = self.evaluate(here + "/separationOfDuties", values, entry.id)
        due, due_recipe, failed = self.due_of(here + "/due", body.get("due"), values, entry.id)
        quorum = body.get("quorum") or "all"
        activity = self.open_activity(
            tid,
            entry,
            "approval",
            quorum=quorum,
            mode=body.get("mode") or "parallel",
            early=body.get("earlyDecision", True),
            votes={},
            total=None,
        )
        self.intent(
            "request_approvals",
            activityId=activity["id"],
            element=entry.id,
            taskType=body.get("taskType"),
            approvers=approvers,
            mode=activity["mode"],
            quorum=quorum,
            earlyDecision=activity["early"],
            excludedPrincipals=excluded,
            due=due,
            context=self.context_profile(here + "/context", body.get("context"), values, entry.id),
            externalRef=self.external_ref(entry.id),
        )
        self.open_sla(activity, here + "/due", body.get("due"), values, failed)
        if due_recipe is not None and body.get("onDue") in ("approve", "reject"):
            self.add_timer(activity, "due", due_recipe, values, onDue=body["onDue"])
        self.escalation_timers(activity, here, body.get("escalations"), due_recipe, values, failed)

    def step_call(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        here = entry.path + "/call"
        arguments = self.step_input(entry, values) or {}
        for name, value in self.evaluate_map(
            here + "/input", body.get("input"), values, entry.id
        ).items():
            arguments[name] = value
        if body.get("skill") is not None:
            activity = self.open_activity(tid, entry, "skill", skill=body["skill"])
            self.intent(
                "invoke_skill",
                activityId=activity["id"],
                element=entry.id,
                skill=body["skill"],
                input=arguments,
            )
        elif body.get("agent") is not None:
            assign = [{"agent": body["agent"]}]
            activity = self.open_activity(tid, entry, "agent", assign=assign)
            self.intent(
                "create_task",
                activityId=activity["id"],
                element=entry.id,
                taskType=None,
                title=entry.node.get("displayName") or entry.id,
                form=None,
                assign=assign,
                due=None,
                escalations=[],
                context=self.context_profile(
                    here + "/context", body.get("context"), values, entry.id
                ),
                input=arguments,
                externalRef=self.external_ref(entry.id),
            )
        else:
            activity = self.open_activity(tid, entry, "child", process=body["process"])
            self.intent(
                "start_child",
                activityId=activity["id"],
                element=entry.id,
                process=body["process"],
                input=arguments,
                parentInstanceId=self.instance_id,
            )
        if body.get("timeout") is not None:
            self.add_timer(activity, "timeout", _recipe(body["timeout"], here + "/timeout"), values)
        self.open_sla(activity, here + "/due", body.get("due"), values)

    def step_recall(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        here = entry.path + "/recall"
        anchors = self.anchors(here + "/anchors", body["anchors"], values, entry.id)
        query = None
        if body.get("query") is not None:
            query = str(self.evaluate(here + "/query", values, entry.id))
        activity = self.open_activity(tid, entry, "recall")
        timeout = body.get("timeout")
        self.intent(
            "recall",
            recallId=activity["id"],
            activityId=activity["id"],
            element=entry.id,
            anchors=anchors,
            traverse=list(body.get("traverse") or ()),
            kinds=list(body.get("kinds") or ()),
            query=query,
            **self.where(here + "/where", body.get("where"), values, entry.id),
            limit=body.get("limit"),
            timeout=timeout or _iso(DEFAULT_RECALL_TIMEOUT),
            asOf=_rfc3339(self.now),
        )
        recipe = (
            _recipe(timeout, here + "/timeout")
            if timeout
            else {"kind": "duration", "value": _iso(DEFAULT_RECALL_TIMEOUT)}
        )
        self.add_timer(activity, "timeout", recipe, values)
        self.open_sla(activity, here + "/due", body.get("due"), values)

    def step_listen(
        self, tid: str, entry: _Step, body: Mapping[str, Any], values: dict[str, Any]
    ) -> None:
        activity = self.open_activity(tid, entry, "listen")
        if body.get("timeout") is not None:
            self.add_timer(
                activity,
                "timeout",
                _recipe(body["timeout"], entry.path + "/listen/timeout"),
                values,
            )
        self.open_sla(activity, entry.path + "/listen/due", body.get("due"), values)

    def listen_matched(self, activity: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        entry = self.d.steps[activity["element"]]
        for index, option in enumerate(entry.node["listen"]["any"]):
            path = f"{entry.path}/listen/any/{index}/on"
            if not self.matches(option["on"], path, event):
                continue
            tid = activity["thread"]
            self.close_activity(activity["id"])
            thread = self.state["threads"][tid]
            thread["event"] = dict(event)
            self.decide("listen_matched", entry.id, option=index, trigger=_trigger_ref(event))
            try:
                self.completed(tid, entry, {"option": index, "event": _event_var(event)})
            except _Raised as raised:
                self.raise_in(tid, raised.error)
                return True
            if option.get("do"):
                thread["stack"].append(
                    {"kind": "seq", "block": f"step:{entry.id}/any/{index}", "index": 0}
                )
            return True
        return False

    # --- pieces of steps --------------------------------------------------------------

    def external_ref(self, element: str) -> str:
        return f"process/{self.instance_id}/{element}"

    def step_input(self, entry: _Step, values: dict[str, Any]) -> dict[str, Any] | None:
        spec = entry.node.get("input")
        if not isinstance(spec, dict) or spec.get("from") is None:
            return None
        value = _jsonable(self.evaluate(entry.path + "/input/from", values, entry.id))
        return dict(value) if isinstance(value, dict) else {"value": value}

    def assignees(
        self,
        path: str,
        chain: Sequence[Mapping[str, Any]],
        values: dict[str, Any],
        element: str,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        """The chain with expressions computed: ``agent:<key>``, ``role:<slug>`` or a principal.

        ``start`` is the index of the chain's first item under ``path``.
        """
        resolved: list[dict[str, Any]] = []
        for index, item in enumerate(chain or (), start):
            if item.get("expr") is None:
                resolved.append(dict(item))
                continue
            value = self.evaluate(f"{path}/{index}/expr", values, element)
            if value in (None, ""):
                continue
            text = str(value)
            if text.startswith("agent:"):
                resolved.append({"agent": text[len("agent:") :]})
            elif text.startswith("role:"):
                resolved.append({"role": text[len("role:") :]})
            else:
                resolved.append({"principal": text})
        return resolved

    def owner_chain(self) -> list[dict[str, Any]]:
        """``spec.owner`` with expressions computed; a candidate that fails is skipped.

        An owner is who an SLA event is addressed to, not a step: a failed
        expression leaves its candidate unresolvable, as an unknown role does.
        """
        chain = self.d.spec.get("owner") or ()
        values = self.values()
        resolved: list[dict[str, Any]] = []
        for index, item in enumerate(chain):
            try:
                resolved.extend(self.assignees("/spec/owner", [item], values, "process", index))
            except _Raised:
                continue
        return resolved

    def due_of(
        self, path: str, value: Any, values: dict[str, Any], element: str
    ) -> tuple[str | None, dict[str, Any] | None, ProcessError | None]:
        """The due of a ``human``/``approve`` step: its moment, recipe and why it failed.

        Before SLA deadlines a due that cannot be computed is an error of the
        step; since, it is ``process.sla_failed`` and the step runs without a
        due (CP-ADR-0078 §3).
        """
        if value is None:
            return None, None, None
        recipe = sla.due_recipe(value, path, self.d.spec.get("calendar"))
        try:
            recipe = self.resolve(recipe, values, element)
            due, _, _ = self.compute(recipe, self.now, values, element)
        except _Raised as raised:
            if not self.sla_on:
                raise
            return None, None, raised.error
        return _rfc3339(due), recipe, None

    def escalation_plan(
        self, path: str, escalations: Any, values: dict[str, Any], element: str
    ) -> list[dict[str, Any]]:
        plan = []
        for index, escalation in enumerate(escalations or ()):
            plan.append(
                {
                    "level": index + 1,
                    "after": escalation["after"],
                    "action": escalation["action"],
                    "to": self.assignees(
                        f"{path}/escalations/{index}/to", escalation.get("to"), values, element
                    ),
                }
            )
        return plan

    def escalation_timers(
        self,
        activity: dict[str, Any],
        path: str,
        escalations: Any,
        due_recipe: dict[str, Any] | None,
        values: dict[str, Any],
        failed: ProcessError | None = None,
    ) -> None:
        for index, escalation in enumerate(escalations or ()):
            if failed is not None:
                # Levels count from the due; without it they have no moment.
                self.decide(
                    "escalation_skipped", activity["element"], level=index + 1, reason="due_failed"
                )
                continue
            after = escalation["after"]
            recipe: dict[str, Any] = {
                "kind": "after",
                "due": due_recipe,
                "after": None
                if after == "due"
                else _recipe(after, f"{path}/escalations/{index}/after"),
            }
            self.add_timer(
                activity,
                "escalation",
                recipe,
                values,
                level=index + 1,
                escalation=f"{path}/escalations/{index}",
            )

    def context_profile(
        self, path: str, context: Any, values: dict[str, Any], element: str
    ) -> dict[str, Any] | None:
        """The step's ``context``: anchors computed now; the task's profile replaces its type's."""
        if not isinstance(context, dict):
            return None
        profile = {
            "anchors": self.anchors(path + "/anchors", context["anchors"], values, element),
            "traverse": list(context.get("traverse") or ()),
            "semantic": context.get("semantic", True),
        }
        if context.get("budgetTokens") is not None:
            profile["budgetTokens"] = context["budgetTokens"]
        return profile

    def anchors(
        self, path: str, anchors: Sequence[Mapping[str, Any]], values: dict[str, Any], element: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for index, anchor in enumerate(anchors or ()):
            if anchor.get("case") is True:
                out.append({"case": True, "kind": self.case_kind(), "key": self.case_key()})
                continue
            key = _key_text(self.evaluate(f"{path}/{index}/key", values, element))
            if key is None:
                continue
            found = {"kind": anchor["kind"], "key": key}
            if anchor.get("via") is not None:
                found["via"] = anchor["via"]
            out.append(found)
        return out

    def where(
        self, path: str, conditions: Any, values: dict[str, Any], element: str
    ) -> dict[str, Any]:
        """``recall.where`` with its values computed: ``{where: [...]}``, or nothing without it.

        The core does not read the conditions (CP-ADR-0076, amendment 2026-09-28):
        a CEL value becomes the JSON literal it gives, a list of CEL a list of
        them, a number or a bool stays; memory gets them as they are.
        """
        if not conditions:
            return {}
        out: list[dict[str, Any]] = []
        for index, condition in enumerate(conditions):
            here = f"{path}/{index}/value"
            computed = {"attr": condition["attr"], "op": condition["op"]}
            value = condition.get("value")
            if isinstance(value, list):
                computed["value"] = [
                    _jsonable(self.evaluate(f"{here}/{item}", values, element))
                    for item in range(len(value))
                ]
            elif isinstance(value, str):
                computed["value"] = _jsonable(self.evaluate(here, values, element))
            elif "value" in condition:
                computed["value"] = value
            out.append(computed)
        return {"where": out}

    # --- answers to activities --------------------------------------------------------

    def answer(self, kind: str, body: Mapping[str, Any]) -> None:
        aid = str(body.get("activityId") or "")
        activity = self.state["activities"].get(aid)
        expected = {
            "task": ("task", "agent", "retro_review"),
            "approval": ("approval",),
            "skill": ("skill", "retro_skill"),
            "child": ("child",),
            "recall": ("recall",),
        }[kind]
        if activity is None or activity["kind"] not in expected:
            self.decide("ignored", reason="stale", input=kind, activity=aid or None)
            return
        if activity["kind"].startswith("retro_"):
            self.retrospective_answer(activity, kind, body)
            return
        getattr(self, f"answer_{activity['kind']}")(activity, body)

    def answer_task(self, activity: dict[str, Any], body: Mapping[str, Any]) -> None:
        tid = activity["thread"]
        entry = self.d.steps[activity["element"]]
        task = dict(body.get("task") or {})
        self.close_activity(activity["id"])
        if body.get("status") != "completed":
            self.raise_in(
                tid,
                ProcessError("task_cancelled", 409, "the task of the step was cancelled", entry.id),
            )
            return
        result = dict(task.get("customFields") or {})
        form = entry.node.get("human", {}).get("form") if activity["kind"] == "task" else None
        if isinstance(form, dict):
            problems = sorted(
                Draft202012Validator(form["schema"]).iter_errors(result), key=lambda e: list(e.path)
            )
            if problems:
                detail = "; ".join(
                    f"/{'/'.join(map(str, p.path))}: {p.message}" for p in problems[:5]
                )
                self.raise_in(tid, ProcessError("form_invalid", 422, detail, entry.id))
                return
        self.decide(
            "task_completed",
            entry.id,
            activity=activity["id"],
            taskId=task.get("id"),
            actor=self.input.actor_id,
        )
        self.continue_with(tid, entry, result, task=task)

    answer_agent = answer_task

    def continue_with(
        self, tid: str, entry: _Step, result: Any, *, task: Mapping[str, Any] | None = None
    ) -> None:
        try:
            self.completed(tid, entry, result, task=task)
        except _Raised as raised:
            self.raise_in(tid, raised.error)

    def answer_approval(self, activity: dict[str, Any], body: Mapping[str, Any]) -> None:
        entry = self.d.steps[activity["element"]]
        approval_id = str(body.get("approvalId") or "")
        outcome = body.get("outcome")
        if body.get("total") is not None:
            activity["total"] = int(body["total"])
        if outcome == "cancelled":
            activity["votes"].pop(approval_id, None)
        elif outcome in ("approved", "rejected"):
            activity["votes"][approval_id] = {
                "principal": body.get("principal"),
                "outcome": outcome,
            }
        self.decide(
            "vote",
            entry.id,
            activity=activity["id"],
            approvalId=approval_id,
            outcome=outcome,
            principal=body.get("principal"),
        )
        verdict = _quorum(activity)
        if verdict is not None:
            self.approval_decided(activity, verdict, "quorum")

    def approval_decided(self, activity: dict[str, Any], outcome: str, cause: str) -> None:
        entry = self.d.steps[activity["element"]]
        tid = activity["thread"]
        votes = activity["votes"]
        self.close_activity(activity["id"])
        self.intent(
            "close_approvals",
            activityId=activity["id"],
            element=entry.id,
            outcome=outcome,
            reason=cause,
        )
        approved = sorted(str(v["principal"]) for v in votes.values() if v["outcome"] == "approved")
        rejected = sorted(str(v["principal"]) for v in votes.values() if v["outcome"] == "rejected")
        self.decide(
            "approval_decided",
            entry.id,
            outcome=outcome,
            cause=cause,
            approvedBy=approved,
            rejectedBy=rejected,
        )
        self.continue_with(
            tid, entry, {"outcome": outcome, "approvedBy": approved, "rejectedBy": rejected}
        )

    def answer_skill(self, activity: dict[str, Any], body: Mapping[str, Any]) -> None:
        entry = self.d.steps[activity["element"]]
        tid = activity["thread"]
        self.close_activity(activity["id"])
        if body.get("status") != "succeeded":
            error = body.get("error") or {}
            self.raise_in(
                tid,
                ProcessError(
                    str(error.get("code") or "skill_failed"),
                    error.get("status") or 502,
                    error.get("message"),
                    entry.id,
                ),
            )
            return
        self.decide("skill_succeeded", entry.id, activity=activity["id"])
        self.continue_with(tid, entry, body.get("output") or {})

    def answer_child(self, activity: dict[str, Any], body: Mapping[str, Any]) -> None:
        entry = self.d.steps[activity["element"]]
        tid = activity["thread"]
        self.close_activity(activity["id"])
        if body.get("status") != "completed":
            error = body.get("error") or {}
            self.raise_in(
                tid,
                ProcessError(
                    str(error.get("type") or f"child_{body.get('status') or 'failed'}"),
                    error.get("status") or 502,
                    error.get("detail"),
                    entry.id,
                ),
            )
            return
        self.decide(
            "child_completed", entry.id, activity=activity["id"], outcome=body.get("outcome")
        )
        self.continue_with(
            tid, entry, {"outcome": body.get("outcome"), "data": body.get("data") or {}}
        )

    def answer_recall(self, activity: dict[str, Any], body: Mapping[str, Any]) -> None:
        if body.get("status") != "completed":
            self.recall_timed_out(activity, str(body.get("reason") or "timeout"))
            return
        entry = self.d.steps[activity["element"]]
        tid = activity["thread"]
        self.close_activity(activity["id"])
        result = body.get("result") or {}
        nodes = list(result.get("nodes") or ())
        edges = list(result.get("edges") or ())
        answer = {"nodes": nodes, "edges": edges, "truncated": bool(result.get("truncated"))}
        digest = "sha256:" + hashlib.sha256(_canonical(answer).encode()).hexdigest()
        self.decide("recall_completed", entry.id, recallId=activity["id"], resultHash=digest)
        self.emit(
            "process.recall_completed",
            step=entry.id,
            recallId=activity["id"],
            asOf=activity["openedAt"],
            nodeCount=len(nodes),
            edgeCount=len(edges),
            truncated=answer["truncated"],
            resultHash=digest,
        )
        self.continue_with(tid, entry, answer)

    def recall_timed_out(self, activity: dict[str, Any], reason: str) -> None:
        entry = self.d.steps[activity["element"]]
        self.decide("recall_timed_out", entry.id, recallId=activity["id"], reason=reason)
        self.emit("process.recall_timed_out", step=entry.id, recallId=activity["id"], reason=reason)
        self.timed_out(activity)

    def timed_out(self, activity: dict[str, Any]) -> None:
        """No answer in time: the step's ``onTimeout`` runs, or the flow goes on without result."""
        entry = self.d.steps[activity["element"]]
        tid = activity["thread"]
        self.close_activity(activity["id"])
        thread = self.state["threads"][tid]
        thread["step"] = {"id": entry.id, "status": "timed_out", "result": {}}
        self.decide("step_timed_out", entry.id, activity=activity["id"])
        body = entry.node[step_kind(entry.node)]
        if body.get("onTimeout"):
            thread["stack"].append(
                {"kind": "seq", "block": f"step:{entry.id}/onTimeout", "index": 0}
            )

    def intent_failed(self, body: Mapping[str, Any]) -> None:
        error = ProcessError(
            "intent_failed",
            body.get("status") or 422,
            str(body.get("code") or body.get("detail") or ""),
            None,
        )
        activity = self.state["activities"].get(str(body.get("activityId") or ""))
        if activity is None:
            self.decide("intent_failed", None, intent=body.get("intent"), code=body.get("code"))
            self.fail(error)
            return
        tid = activity["thread"]
        self.close_activity(activity["id"])
        error = ProcessError(error.type, error.status, error.detail, activity["element"])
        self.decide(
            "intent_failed", activity["element"], intent=body.get("intent"), code=body.get("code")
        )
        if activity["kind"].startswith("retro_"):
            self.state["retrospective"] = {"phase": "failed", "error": error.out()}
            return
        self.raise_in(tid, error)

    # --- errors -----------------------------------------------------------------------

    def raise_in(self, tid: str, error: ProcessError) -> None:
        """Take ``error`` up the thread's stack to the nearest handler, then to its fork."""
        thread = self.state["threads"].get(tid)
        if thread is None:
            self.fail(error)
            return
        self.decide("error_raised", error.element, error=error.out(), thread=tid)
        if thread["wait"] and thread["wait"] in self.state["activities"]:
            self.cancel_activity(thread["wait"], "error")
        thread["wait"] = None
        stack = thread["stack"]
        while stack:
            frame = stack[-1]
            if frame["kind"] == "try" and frame["phase"] == "do" and self.handle(tid, frame, error):
                return
            if frame["kind"] == "compensation":
                self.state["attention"] = {
                    "reason": "compensation_failed",
                    "element": error.element,
                    "error": error.out(),
                }
            if frame["kind"] == "fork":
                for child in frame["children"]:
                    self.end_thread(child, "error")
            stack.pop()
        del self.state["threads"][tid]
        parent = thread["parent"]
        if parent is not None and parent in self.state["threads"]:
            parent_thread = self.state["threads"][parent]
            fork = parent_thread["stack"][-1]
            for child in fork["children"]:
                self.end_thread(child, "branch_failed")
            parent_thread["stack"].pop()
            parent_thread["wait"] = None
            self.raise_in(parent, error)
            return
        if thread["scope"] == "closing":
            self.state["attention"] = self.state["attention"] or {
                "reason": "compensation_failed",
                "element": error.element,
                "error": error.out(),
            }
        self.fail(error)

    def handle(self, tid: str, frame: dict[str, Any], error: ProcessError) -> bool:
        entry = self.d.steps[frame["step"]]
        body = entry.node["try"]
        stack = self.state["threads"][tid]["stack"]
        retry = body.get("retry")
        if (
            retry
            and (not retry.get("on") or error.type in retry["on"])
            and frame["attempt"] < retry["limit"]
        ):
            frame["attempt"] += 1
            del stack[stack.index(frame) + 1 :]
            stack.append({"kind": "seq", "block": f"step:{entry.id}/try", "index": 0})
            delay = _retry_delay(retry, frame["attempt"])
            self.decide(
                "retry_scheduled",
                entry.id,
                attempt=frame["attempt"],
                delay=_iso(delay),
                error=error.out(),
            )
            if delay > timedelta(0):
                activity = self.open_activity(tid, entry, "retry")
                self.add_timer(activity, "retry", {"kind": "duration", "value": _iso(delay)}, {})
            return True
        for index, clause in enumerate(body.get("catch") or ()):
            wanted = clause.get("errors") or {}
            if wanted.get("type") is not None and wanted["type"] != error.type:
                continue
            if wanted.get("status") is not None and wanted["status"] != error.status:
                continue
            del stack[stack.index(frame) + 1 :]
            frame["phase"] = "catch"
            bindings = {clause["as"]: error.out()} if clause.get("as") else {}
            stack.append(
                {
                    "kind": "seq",
                    "block": f"step:{entry.id}/catch/{index}",
                    "index": 0,
                    "bindings": bindings,
                }
            )
            self.decide("error_caught", entry.id, clause=index, error=error.out())
            return True
        return False

    def fail(self, error: ProcessError) -> None:
        if self.state["status"] in CLOSED:
            return
        self.terminate_all("failed")
        self.state.update(status=FAILED, error={**error.out(), "element": error.element})
        self.decide("failed", error.element, error=error.out(), attention=self.state["attention"])
        self.emit("process.failed", error=error.out(), element=error.element)
        self.intent("complete", status=FAILED, outcome=None, error=error.out())

    def terminate_all(self, reason: str) -> None:
        for thread in sorted(self.state["threads"].values(), key=lambda t: t["n"]):
            if thread["parent"] is None:
                self.end_thread(thread["id"], reason)
        for activity in sorted(self.state["activities"].values(), key=lambda a: a["n"]):
            if not activity["kind"].startswith("retro_"):
                self.cancel_activity(activity["id"], reason)
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            self.cancel_timer(timer["id"])
        for sid, record in self.state["stages"].items():
            if record["state"] == "active":
                record.update(state=COMPLETED, closedSeq=self.state["seq"])
                self.decide("stage_exited", sid, cause=reason, exit=None)
                self.emit("process.stage_exited", stage=sid)

    # --- closing ----------------------------------------------------------------------

    def complete(self, element: str | None, outcome: str) -> None:
        self.flush_changes(recompute=False)
        self.terminate_all("completed")
        self.state.update(status=COMPLETED, outcome=outcome)
        self.decide("completed", element, outcome=outcome)
        self.emit("process.completed", outcome=outcome, memory=self.projection())
        self.intent("complete", status=COMPLETED, outcome=outcome, error=None)
        self.start_retrospective()

    def cancel(self, reason: str, *, compensate: bool = True) -> None:
        """An operator cancels: open work ends, compensations run in reverse, then ``cancelled``.

        ``compensate=False`` — the operator chose to leave completed steps as they are.
        """
        self.terminate_all("cancelled")
        self.state["closing"] = {"kind": "cancel", "reason": reason, "compensated": False}
        pending = [d for d in self.state["done"] if not d["compensated"]]
        if not pending or not compensate:
            self.cancelled()
            return
        tid = self.spawn("", scope="closing")
        self.compensate(tid, "all", None)
        self.state["closing"]["compensated"] = True

    def cancelled(self) -> None:
        closing = self.state["closing"] or {}
        if self.sla_on:
            # A suspension of the process's deadline ends with the instance.
            self.close_stops()
        self.state.update(status=CANCELLED, outcome=None)
        self.decide(
            "cancelled",
            None,
            reason=closing.get("reason"),
            compensated=closing.get("compensated", False),
        )
        self.emit(
            "process.cancelled",
            reason=closing.get("reason") or "",
            compensated=bool(closing.get("compensated")),
        )
        self.intent("complete", status=CANCELLED, outcome=None, error=None)

    # --- suspend and resume -----------------------------------------------------------

    def suspend(self, cause: str, reason: str, element: str | None) -> None:
        if self.state["status"] != RUNNING:
            self.decide("ignored", element, reason="not_running", command="suspend")
            return
        self.state["status"] = SUSPENDED
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            if timer["state"] != "pending" or self.timer_runs_while_suspended(timer):
                continue
            self.freeze(timer)
            self.intent("set_timer", **self.timer_row(timer))
        if self.sla_on:
            self.open_stops()
        self.decide("suspended", element, cause=cause, reason=reason)
        self.emit("process.suspended", cause=cause, reason=reason)

    def freeze(self, timer: dict[str, Any]) -> None:
        """Stop a pending timer: what is left of it is kept, in its unit (CP-ADR-0078 §4).

        A timer that reads the data keeps nothing and is recomputed on resume,
        as is a deadline from the data (``{at}``). A deadline in working units
        keeps working time (or working days); should its calendar be gone, the
        wall-clock seconds are kept instead.
        """
        due = _parse_time(timer["dueAt"])
        remaining: float | None = (
            None if timer["reads"] else max(0.0, (due - self.now).total_seconds())
        )
        unit = sla.WALL
        if timer["kind"] in sla.SLA_TIMERS:
            with contextlib.suppress(sla.DeadlineError):
                remaining, unit = sla.remainder(
                    timer["recipe"], due, self.now, self.input.calendars
                )
        timer.update(state="frozen", remaining=remaining, frozenFrom=timer["dueAt"], dueAt=None)
        if self.sla_on:
            # Revision 1 keeps the timer as it was before CP-ADR-0078: a
            # replay of its journal compares the state too (FR-031).
            timer.update(remainingUnit=unit, frozenAt=_rfc3339(self.now))

    def thawed(self, timer: Mapping[str, Any]) -> tuple[datetime, bool]:
        """The new moment of a frozen timer resumed now.

        A deadline's warning is counted back from its resumed deadline, as when
        it was set; a timer with nothing kept is recomputed from its recipe.
        With SLA deadlines a timer already due at the freeze keeps its moment:
        it fires right away, and a deadline is reported as declared.
        """
        if self.sla_on and self.due_at_freeze(timer):
            return _parse_time(timer["frozenFrom"]), bool(timer["provisional"])
        if timer["kind"] == sla.SLA_WARNING and timer["remaining"] is not None:
            activity = self.state["activities"].get(timer.get("activity") or "")
            record = self.sla_holder(timer, activity) or {}
            due_timer = self.state["timers"].get(record.get("timer") or "")
            if due_timer is not None and due_timer["state"] == "pending":
                due = _parse_time(due_timer["dueAt"])
                warn, marked = self.before(
                    timer["recipe"]["span"], due, self.timer_values(timer), timer["element"]
                )
                return warn, bool(due_timer["provisional"]) or marked
        if timer["remaining"] is None:
            due, provisional, _ = self.compute(
                timer["recipe"],
                _parse_time(timer["base"]),
                self.timer_values(timer),
                timer["element"],
            )
            return due, provisional
        unit = timer.get("remainingUnit") or sla.WALL
        if unit == sla.WALL:
            return self.now + timedelta(seconds=timer["remaining"]), bool(timer["provisional"])
        try:
            moment, provisional = sla.thaw(
                timer["recipe"], timer["remaining"], unit, self.now, self.input.calendars
            )
        except sla.DeadlineError as exc:
            raise _Raised(ProcessError(exc.code, 422, exc.message, timer["element"])) from None
        return moment.astimezone(UTC), provisional

    @staticmethod
    def due_at_freeze(timer: Mapping[str, Any]) -> bool:
        """Whether a frozen timer's moment had come when it was frozen (not yet taken)."""
        frozen_from, frozen_at = timer.get("frozenFrom"), timer.get("frozenAt")
        if timer["remaining"] is None or not frozen_from or not frozen_at:
            return False
        return _parse_time(str(frozen_from)) <= _parse_time(str(frozen_at))

    def timer_runs_while_suspended(self, timer: Mapping[str, Any]) -> bool:
        activity = self.state["activities"].get(timer.get("activity") or "")
        return activity is not None and not self.thread_paused(activity["thread"])

    def resume(self, cause: str, element: str | None) -> None:
        if self.state["status"] != SUSPENDED:
            self.decide("ignored", element, reason="not_suspended", command="resume")
            return
        self.state["status"] = RUNNING
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            if timer["state"] != "frozen" or timer["id"] not in self.state["timers"]:
                continue
            try:
                due, provisional = self.thawed(timer)
            except _Raised as raised:
                if timer["kind"] in sla.SLA_TIMERS:
                    self.sla_lost(timer, raised.error)
                    continue
                self.fail(raised.error)
                return
            if self.sla_on:
                self.note_pause(timer, due)
            timer.update(
                state="pending", dueAt=_rfc3339(due), remaining=None, provisional=provisional
            )
            if self.sla_on:
                timer.update(remainingUnit=sla.WALL, frozenAt=None)
            self.sync_sla(timer)
            self.intent("set_timer", **self.timer_row(timer))
            self.emit(
                "process.timer_rescheduled",
                timerId=timer["id"],
                element=timer["element"],
                previousDueAt=timer["frozenFrom"],
                dueAt=timer["dueAt"],
                provisional=provisional,
                cause="resumed",
                changedFields=[],
            )
        if self.sla_on:
            self.close_stops()
        self.decide("resumed", element, cause=cause)
        self.emit("process.resumed", cause=cause)
        deferred, self.state["deferred"] = self.state["deferred"], []
        for record in deferred:
            self.decide("replayed", None, input=record["kind"])
            self.dispatch_deferred(record)

    def note_pause(self, timer: dict[str, Any], due: datetime) -> None:
        """Keep how far the resume moved a timer (``paused``), in the unit it was frozen in.

        A recount counts the timer from its base again (a migration, a new
        calendar version) and adds the pauses back (:meth:`paced`), so a past
        suspension is not lost (FR-016). A timer with nothing kept (``{at}``,
        one that reads the data) or already due at the freeze has no pause. A
        warning counted back from its resumed deadline takes the deadline's.

        A deadline already past at the freeze does not move: its record keeps
        the suspension instead (``overdueStops``, :meth:`open_stops`).
        """
        if timer["kind"] == sla.SLA and self.due_at_freeze(timer):
            return
        if not timer["remaining"] or not timer.get("frozenFrom"):
            return
        if timer["kind"] == sla.SLA_WARNING:
            activity = self.state["activities"].get(timer.get("activity") or "")
            record = self.sla_holder(timer, activity) or {}
            due_timer = self.state["timers"].get(record.get("timer") or "")
            if due_timer is not None and due_timer["state"] == "pending":
                if due_timer.get("paused"):
                    timer["paused"] = copy.deepcopy(due_timer["paused"])
                return
        unit = timer.get("remainingUnit") or sla.WALL
        try:
            more = sla.pause(
                timer["recipe"],
                unit,
                _parse_time(timer["frozenFrom"]),
                due,
                self.input.calendars,
            )
        except sla.DeadlineError:
            return
        if more is not None:
            timer["paused"] = sla.add_pause(timer.get("paused") or [], more)

    def open_stops(self) -> None:
        """Start a suspension (``overdueStops``) of every deadline already past that stands.

        The overdue clock of a past deadline stands with its thread, whether
        the worker recorded the breach before the suspension or not (FR-021):
        a record breached, or due by now, of a step whose thread waits (or of
        the process) gets ``{from: now, to: null}``; :meth:`close_stops` ends
        it. ``overdueSeconds`` of the breach, of the step's exit and of the
        projection leave out what of it falls after the deadline.
        """
        holders: list[tuple[dict[str, Any], dict[str, Any] | None]] = [(self.state, None)]
        holders += [(a, a) for a in self.state["activities"].values()]
        for holder, activity in holders:
            if activity is not None and not self.thread_paused(activity["thread"]):
                continue
            record = holder.get("sla")
            if not isinstance(record, dict) or record.get("state") == sla.FAILED:
                continue
            due = record.get("dueAt")
            if not due or (record.get("state") != sla.BREACHED and _parse_time(due) > self.now):
                continue
            stops = record.get("overdueStops") or []
            if any(stop.get("to") is None for stop in stops):
                continue
            record["overdueStops"] = [*stops, {"from": _rfc3339(self.now), "to": None}]

    def close_stops(self) -> None:
        """End the suspensions deadline records keep open (``overdueStops``): resumed or closed.

        The open ones come from :meth:`open_stops` and from a migration that
        finds a deadline breached while its timer is frozen (the timer goes,
        the record keeps the suspension from ``frozenAt``).
        """
        holders = [self.state, *self.state["activities"].values()]
        for holder in holders:
            record = holder.get("sla")
            if not isinstance(record, dict):
                continue
            for stop in record.get("overdueStops") or ():
                if stop.get("to") is None:
                    stop["to"] = _rfc3339(self.now)

    def dispatch_deferred(self, record: Mapping[str, Any]) -> None:
        body = record["body"]
        if record["kind"] == "event":
            for activity in self.sorted_activities("listen"):
                if self.listen_matched(activity, body["event"]):
                    break
            return
        self.dispatch(record["kind"], body)

    def command(self) -> None:
        body = self.input.body
        action = body.get("action")
        if action not in COMMANDS:
            raise EngineError(f"unknown command {action!r}")
        if self.state["status"] in CLOSED:
            self.decide("ignored", reason="instance_closed", command=action)
            return
        reason = str(body.get("reason") or "")
        if action == "suspend":
            self.suspend("operator", reason, None)
        elif action == "resume":
            self.resume("operator", None)
        elif action == "cancel":
            if self.state["closing"] is not None:
                self.decide("ignored", reason="closing", command=action)
                return
            compensate = body.get("compensate") is not False
            self.decide(
                "cancel_requested",
                None,
                reason=reason,
                actor=self.input.actor_id,
                compensate=compensate,
            )
            self.cancel(reason, compensate=compensate)
        else:
            self.start_discretionary(str(body.get("stage") or ""), str(body.get("step") or ""))

    def start_discretionary(self, stage_id: str, step_id: str) -> None:
        _, stage = self.d.stage(stage_id)
        planned = [s["id"] for s in stage.get("discretionary") or ()]
        if step_id not in planned:
            raise EngineError(f"stage {stage_id!r} has no discretionary step {step_id!r}")
        if self.state["stages"][stage_id]["state"] != "active":
            self.decide("ignored", step_id, reason="stage_not_active", stage=stage_id)
            return
        self.decide("discretionary_started", step_id, stage=stage_id, actor=self.input.actor_id)
        self.spawn(f"step:{step_id}", scope=f"stage:{stage_id}")

    # --- timers -----------------------------------------------------------------------

    def boundary_timer(self, timer: _Timer) -> None:
        scope = f"stage:{timer.stage}" if timer.stage else "process"
        base = self.now
        recipe = _recipe(timer.node["at"], timer.path + "/at")
        self.new_timer(
            "boundary",
            timer.id,
            recipe,
            base,
            self.values(),
            scope=scope,
            boundary=timer.id,
        )

    def add_timer(
        self,
        activity: dict[str, Any],
        kind: str,
        recipe: dict[str, Any],
        values: dict[str, Any],
        *,
        base: datetime | None = None,
        **extra: Any,
    ) -> str:
        thread = self.state["threads"].get(activity["thread"])
        scope = thread["scope"] if thread is not None else "process"
        timer_id = self.new_timer(
            kind,
            activity["element"],
            recipe,
            self.now if base is None else base,
            values,
            scope=scope,
            activity=activity["id"],
            **extra,
        )
        activity["timers"].append(timer_id)
        return timer_id

    def new_timer(
        self,
        kind: str,
        element: str,
        recipe: dict[str, Any],
        base: datetime,
        values: dict[str, Any],
        **extra: Any,
    ) -> str:
        due, provisional, reads = self.paced(recipe, base, values, element, extra.get("paused"))
        timer_id = self.make_id()
        timer = {
            "id": timer_id,
            "n": self.counter("timer"),
            "kind": kind,
            "element": element,
            "recipe": recipe,
            "base": _rfc3339(base),
            "reads": sorted(reads),
            "dueAt": _rfc3339(due),
            "state": "pending",
            "remaining": None,
            "provisional": provisional,
            "frozenFrom": None,
            **extra,
        }
        self.state["timers"][timer_id] = timer
        if self.state["status"] == SUSPENDED and not self.timer_runs_while_suspended(timer):
            self.freeze(timer)
        # What the expressions of the deadline gave, so that a replay compares it too.
        amounts = sla.computed_amounts(recipe)
        self.decide(
            "timer_set",
            element,
            timerId=timer_id,
            timerKind=kind,
            dueAt=timer["dueAt"],
            provisional=provisional,
            **({"computed": amounts} if amounts else {}),
        )
        self.intent("set_timer", **self.timer_row(timer))
        return timer_id

    def timer_row(self, timer: Mapping[str, Any]) -> dict[str, Any]:
        row = {
            "timerId": timer["id"],
            "element": timer["element"],
            "timerKind": timer["kind"],
            "dueAt": timer["dueAt"],
            "state": timer["state"],
            "remainingSeconds": timer["remaining"],
            "reads": timer["reads"],
            "provisional": timer["provisional"],
        }
        # Only a remainder in working units names its unit: the rows of every
        # journal before CP-ADR-0078 §4 stay as they were recorded.
        unit = timer.get("remainingUnit") or sla.WALL
        if unit != sla.WALL:
            row["remainingUnit"] = unit
        return row

    def cancel_timer(self, timer_id: str) -> None:
        timer = self.state["timers"].pop(timer_id, None)
        if timer is None:
            return
        self.intent("cancel_timer", timerId=timer_id, element=timer["element"])

    def timer_values(self, timer: Mapping[str, Any]) -> dict[str, Any]:
        activity = self.state["activities"].get(timer.get("activity") or "")
        thread = self.state["threads"].get(activity["thread"]) if activity else None
        return self.values(thread)

    def compute(
        self, recipe: Mapping[str, Any], base: datetime, values: Mapping[str, Any], element: str
    ) -> tuple[datetime, bool, set[str]]:
        """A timer's moment: a duration after ``base``, a CEL moment or duration, or after a due."""
        kind = recipe["kind"]
        if kind == "duration":
            delta = parse_iso_duration(recipe["value"])
            if delta is None:
                raise _Raised(
                    ProcessError(
                        "invalid_duration", 422, f"{recipe['value']!r} has no fixed length", element
                    )
                )
            return base + delta, False, set()
        if kind == "at":
            value, provisional = self.evaluate_marked(recipe["path"], values, element)
            reads = {
                r[len("data.") :]
                for r in self.d.programs[recipe["path"]].reads
                if r.startswith("data.")
            }
            if isinstance(value, datetime):
                return value.astimezone(UTC), provisional, reads
            if isinstance(value, timedelta):
                return base + value, provisional, reads
            raise _Raised(
                ProcessError(
                    "invalid_due", 422, f"{recipe['path']} gives no moment or duration", element
                )
            )
        if kind in sla.WORKING:
            moment, provisional = self.working(recipe, base, element)
            return moment, provisional, set()
        if kind == "before":
            due, provisional, reads = self.compute(recipe["due"], base, values, element)
            moment, more = self.before(recipe["span"], due, values, element)
            return moment, provisional or more, reads
        due, provisional, reads = (
            (base, False, set())
            if recipe["due"] is None
            else self.compute(recipe["due"], base, values, element)
        )
        if recipe["after"] is None:
            return due, provisional, reads
        moment, more, more_reads = self.compute(recipe["after"], due, values, element)
        return moment, provisional or more, reads | more_reads

    def paced(
        self,
        recipe: Mapping[str, Any],
        base: datetime,
        values: Mapping[str, Any],
        element: str,
        pauses: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[datetime, bool, set[str]]:
        """A timer's moment counted from ``base`` again, its past pauses added back.

        A warning threshold is counted back from its deadline moved on by
        them, as the resume counts it; a moment from the data does not move
        with a pause.
        """
        if not pauses:
            return self.compute(recipe, base, values, element)
        if recipe["kind"] == "before":
            due, provisional, reads = self.paced(recipe["due"], base, values, element, pauses)
            moment, more = self.before(recipe["span"], due, values, element)
            return moment, provisional or more, reads
        moment, provisional, reads = self.compute(recipe, base, values, element)
        if reads or recipe["kind"] == "at":
            return moment, provisional, reads
        for pause in pauses:
            try:
                moment, more = sla.extend(moment, pause, self.input.calendars)
            except sla.DeadlineError as exc:
                raise _Raised(ProcessError(exc.code, 422, exc.message, element)) from None
            provisional = provisional or more
        return moment.astimezone(UTC), provisional, reads

    def before(
        self, span: Mapping[str, Any], due: datetime, values: Mapping[str, Any], element: str
    ) -> tuple[datetime, bool]:
        """A warning threshold: ``span`` counted back from ``due``."""
        if span["kind"] in sla.WORKING:
            return self.working(span, due, element, back=True)
        moment, _, _ = self.compute(span, due, values, element)
        return due - (moment - due), False

    def refreeze(self, calendar: Any) -> None:
        """A new calendar version recounts what frozen deadlines in working units keep.

        The deadline is recomputed from its base as a pending one would be, and
        the remainder is measured again from the moment it was frozen. The
        timer stays frozen: the new moment comes on resume
        (``process.timer_rescheduled``, ``cause: resumed``). A timer kept in
        ``wall`` is left alone: what is left of it does not depend on a calendar.
        """
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            if (
                timer["state"] != "frozen"
                or timer["kind"] not in sla.SLA_TIMERS
                or (timer.get("remainingUnit") or sla.WALL) == sla.WALL
                or not timer.get("frozenAt")
                or not self.calls_calendar(timer["recipe"], calendar)
            ):
                continue
            try:
                due, provisional, _ = self.paced(
                    timer["recipe"],
                    _parse_time(timer["base"]),
                    self.timer_values(timer),
                    timer["element"],
                    timer.get("paused"),
                )
                remaining, unit = sla.remainder(
                    timer["recipe"],
                    due,
                    _parse_time(timer["frozenAt"]),
                    self.input.calendars,
                )
            except (_Raised, sla.DeadlineError) as raised:
                error = (
                    raised.error
                    if isinstance(raised, _Raised)
                    else ProcessError(raised.code, 422, raised.message, timer["element"])
                )
                self.decide("timer_kept", timer["element"], timerId=timer["id"], error=error.out())
                continue
            previous = timer["frozenFrom"]
            if (
                _rfc3339(due) == previous
                and remaining == timer["remaining"]
                and unit == timer.get("remainingUnit")
                and provisional == timer["provisional"]
            ):
                continue
            timer.update(
                frozenFrom=_rfc3339(due),
                remaining=remaining,
                remainingUnit=unit,
                provisional=provisional,
            )
            self.sync_sla(timer)
            self.decide(
                "timer_rescheduled",
                timer["element"],
                timerId=timer["id"],
                previousDueAt=previous,
                dueAt=None,
                frozenDueAt=timer["frozenFrom"],
                remainingSeconds=remaining,
                remainingUnit=unit,
                cause="calendar_changed",
            )
            self.intent("set_timer", **self.timer_row(timer))

    def recompute_timers(
        self, *, cause: str, fields: Sequence[str] = (), calendar: Any = None
    ) -> None:
        """Pending timers whose expressions read changed data (or call a changed calendar) move."""
        for timer in sorted(self.state["timers"].values(), key=lambda t: t["n"]):
            if timer["state"] != "pending":
                continue
            if cause == "data_changed" and not _touches(timer["reads"], fields):
                continue
            if cause == "calendar_changed" and not self.calls_calendar(timer["recipe"], calendar):
                continue
            try:
                due, provisional, reads = self.paced(
                    timer["recipe"],
                    _parse_time(timer["base"]),
                    self.timer_values(timer),
                    timer["element"],
                    timer.get("paused"),
                )
            except _Raised as raised:
                self.decide(
                    "timer_kept", timer["element"], timerId=timer["id"], error=raised.error.out()
                )
                continue
            previous = timer["dueAt"]
            timer["reads"] = sorted(reads)
            if _rfc3339(due) == previous and provisional == timer["provisional"]:
                continue
            timer.update(dueAt=_rfc3339(due), provisional=provisional)
            self.sync_sla(timer)
            self.decide(
                "timer_rescheduled",
                timer["element"],
                timerId=timer["id"],
                previousDueAt=previous,
                dueAt=timer["dueAt"],
                cause=cause,
            )
            self.intent("set_timer", **self.timer_row(timer))
            self.emit(
                "process.timer_rescheduled",
                timerId=timer["id"],
                element=timer["element"],
                previousDueAt=previous,
                dueAt=timer["dueAt"],
                provisional=provisional,
                cause=cause,
                changedFields=list(fields),
            )

    def working(
        self, recipe: Mapping[str, Any], base: datetime, element: str, *, back: bool = False
    ) -> tuple[datetime, bool]:
        try:
            moment, provisional = sla.working(recipe, base, self.input.calendars, back=back)
        except sla.DeadlineError as exc:
            raise _Raised(ProcessError(exc.code, 422, exc.message, element)) from None
        return moment.astimezone(UTC), provisional

    def calls_calendar(self, recipe: Mapping[str, Any] | None, key: Any = None) -> bool:
        if recipe is None:
            return False
        known = sla.calls_calendar(recipe, key)
        if known is not None:
            return known
        if recipe["kind"] == "at":
            return "cal." in self.d.programs[recipe["path"]].expression
        if recipe["kind"] == "after":
            return self.calls_calendar(recipe["due"], key) or self.calls_calendar(
                recipe["after"], key
            )
        return False

    def timer_fired(self, timer_id: str, body: Mapping[str, Any]) -> None:
        timer = self.state["timers"].get(timer_id)
        if timer is None or timer["state"] != "pending":
            self.decide("ignored", reason="stale", input="timer", timerId=timer_id)
            return
        activity = self.state["activities"].get(timer.get("activity") or "")
        if activity is not None and self.thread_paused(activity["thread"]):
            self.state["deferred"].append(self.input.out())
            self.decide("deferred", timer["element"], reason="suspended", input="timer")
            return
        del self.state["timers"][timer_id]
        if activity is not None and timer_id in activity["timers"]:
            activity["timers"].remove(timer_id)
        self.decide(
            "timer_fired",
            timer["element"],
            timerId=timer_id,
            timerKind=timer["kind"],
            dueAt=timer["dueAt"],
        )
        kind = timer["kind"]
        if kind in sla.SLA_TIMERS:
            # The fact of a deadline is its own event, not process.timer_fired (SC-010).
            self.sla_fired(timer, activity, body)
            return
        self.emit(
            "process.timer_fired", timerId=timer_id, element=timer["element"], dueAt=timer["dueAt"]
        )
        if kind == "boundary":
            self.boundary_fired(timer)
        elif activity is None:
            return
        elif kind in ("wait", "retry"):
            tid = activity["thread"]
            self.close_activity(activity["id"])
            if kind == "wait":
                self.continue_with(tid, self.d.steps[activity["element"]], None)
        elif kind == "timeout":
            if activity["kind"] == "recall":
                self.recall_timed_out(activity, "timeout")
            elif activity["kind"] == "listen":
                self.timed_out(activity)
            else:
                tid = activity["thread"]
                self.raise_in(
                    tid,
                    ProcessError(
                        "timeout", 408, "the call gave no result in time", activity["element"]
                    ),
                )
        elif kind == "due":
            self.approval_decided(
                activity, "approved" if timer["onDue"] == "approve" else "rejected", "due"
            )
        elif kind == "escalation":
            self.escalate(activity, timer)

    def boundary_fired(self, timer: Mapping[str, Any]) -> None:
        spec = self.d.timers[timer["boundary"]]
        scope = timer["scope"]
        if spec.node.get("interrupting"):
            for thread in sorted(self.state["threads"].values(), key=lambda t: t["n"]):
                inside = thread["scope"] == scope or (
                    scope == "process" and thread["scope"].startswith("stage:")
                )
                if inside and thread["parent"] is None:
                    self.end_thread(thread["id"], "interrupted")
            self.decide("interrupted", spec.id, scope=scope)
        self.spawn(f"timer:{spec.id}", scope=scope)

    def escalate(self, activity: dict[str, Any], timer: Mapping[str, Any]) -> None:
        entry = self.d.steps[activity["element"]]
        escalation = _at_pointer(self.d.spec, timer["escalation"])
        action = escalation["action"]
        values = self.timer_values(timer)
        to = self.assignees(timer["escalation"] + "/to", escalation.get("to"), values, entry.id)
        self.decide("escalated", entry.id, level=timer["level"], action=action, to=to)
        self.emit(
            ESCALATED,
            element=entry.id,
            level=timer["level"],
            action=action,
            taskId=None,
            to=[_assignee_text(a) for a in (to or activity.get("assign") or ())],
        )
        if action == "reassign":
            activity["assign"] = to
            self.intent(
                "reassign_task",
                activityId=activity["id"],
                element=entry.id,
                assign=to,
                level=timer["level"],
            )
        elif action == "raise":
            error = escalation.get("error") or {"type": "escalation"}
            detail = None
            if error.get("detail") is not None:
                detail = str(self.evaluate(timer["escalation"] + "/error/detail", values, entry.id))
            self.raise_in(
                activity["thread"],
                ProcessError(error["type"], error.get("status"), detail, entry.id),
            )

    # --- SLA deadlines (CP-ADR-0078 §3) ------------------------------------------------

    def open_sla(
        self,
        activity: dict[str, Any],
        path: str,
        value: Any,
        values: dict[str, Any],
        failed: ProcessError | None = None,
    ) -> None:
        """The deadline of a waiting step: timers ``sla``/``sla_warning`` of its activity.

        Every activity is an attempt of its own, counted from its opening; the
        timers go with the activity when it closes.
        """
        if not self.sla_on or value is None:
            return
        activity["sla"] = self.deadline(
            sla.STEP, str(activity["element"]), path, value, values, activity, failed
        )

    def deadline(
        self,
        scope: str,
        element: str,
        path: str,
        value: Any,
        values: dict[str, Any],
        activity: dict[str, Any] | None,
        failed: ProcessError | None,
    ) -> dict[str, Any]:
        """Set the timers of a deadline from now; the record of it the state keeps.

        A deadline that cannot be computed (no calendar, an expression error)
        does not stop the instance: it is ``process.sla_failed`` and a record
        marked ``failed``.
        """
        due_recipe: dict[str, Any] = {}
        warn_recipe = None
        due = warn = None
        provisional = False
        if failed is None:
            try:
                due_recipe, warn_recipe = self.resolved(value, path, values, element)
                due, provisional, _ = self.compute(due_recipe, self.now, values, element)
                if warn_recipe is not None:
                    warn, marked, _ = self.compute(warn_recipe, self.now, values, element)
                    provisional = provisional or marked
            except _Raised as raised:
                failed = raised.error
        aid = activity["id"] if activity is not None else None
        named = element if scope == sla.STEP else None
        if failed is not None or due is None:
            error = (failed or ProcessError("invalid_due", 422)).out()
            self.sla_failure(scope, named, aid, error)
            return sla.record(due_at=None, warn_at=None, provisional=False, error=error)
        timers = [(sla.SLA, due_recipe)]
        if warn_recipe is not None:
            timers.append((sla.SLA_WARNING, warn_recipe))
        made: list[str] = []
        for kind, recipe in timers:
            if activity is not None:
                made.append(self.add_timer(activity, kind, recipe, values, sla=scope))
            else:
                made.append(
                    self.new_timer(kind, element, recipe, self.now, values, scope=scope, sla=scope)
                )
        return sla.record(
            due_at=_rfc3339(due),
            warn_at=_rfc3339(warn) if warn is not None else None,
            provisional=provisional,
            timer=made[0],
            warn_timer=made[1] if len(made) > 1 else None,
        )

    def resolved(
        self, value: Any, path: str, values: Mapping[str, Any], element: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The recipes of the deadline at ``path`` and of its warning, expressions computed."""
        calendar = self.d.spec.get("calendar")
        due = self.resolve(sla.due_recipe(value, path, calendar), values, element)
        warn = sla.warn_recipe(value, due, calendar, path)
        return due, self.resolve(warn, values, element) if warn is not None else None

    def resolve(
        self, recipe: dict[str, Any], values: Mapping[str, Any], element: str
    ) -> dict[str, Any]:
        """A recipe with the ``{expr}`` amounts of its working units computed.

        An amount is computed once, from the values of the step's entry, and
        the recipe keeps the number: the warning, a pause and a new calendar
        version count it as they count a number (CP-ADR-0081, amendment 2026-10-03 G1).
        """
        kind = recipe.get("kind")
        if kind == "before":
            return {**recipe, "span": self.resolve(recipe["span"], values, element)}
        if kind not in sla.WORKING or "expr" not in recipe:
            return recipe
        path = str(recipe["expr"])
        raw = self.evaluate(path, values, element)
        try:
            amount = sla.amount_of(raw, str(kind), path)
        except sla.DeadlineError as exc:
            raise _Raised(ProcessError(exc.code, 422, exc.message, element)) from None
        return sla.computed(recipe, amount)

    def sla_failure(
        self, scope: str, element: str | None, aid: str | None, error: Mapping[str, Any]
    ) -> None:
        """``process.sla_failed``: addressed to the owner, no assignee (CP-ADR-0078 §3)."""
        self.decide("sla_failed", element, scope=scope, activity=aid, error=dict(error))
        self.emit(
            "process.sla_failed",
            scope=scope,
            element=element,
            attempt=None,
            activityId=aid,
            error=dict(error),
            owner=None,
            addressees={"owner": self.owner_chain()},
        )

    def sla_lost(self, timer: Mapping[str, Any], error: ProcessError) -> None:
        """A frozen deadline that cannot be counted on resume fails; the instance goes on."""
        activity = self.state["activities"].get(timer.get("activity") or "")
        record = self.sla_holder(timer, activity)
        ids = [timer["id"]]
        if record is not None:
            ids += [record.get("timer"), record.get("warnTimer")]
        for timer_id in dict.fromkeys(i for i in ids if i):
            self.cancel_timer(timer_id)
        out = error.out()
        if record is not None:
            record.update(state=sla.FAILED, timer=None, warnTimer=None, error=out)
        scope = str(timer.get("sla") or sla.STEP)
        aid = activity["id"] if activity is not None and scope == sla.STEP else None
        self.sla_failure(scope, timer["element"] if scope == sla.STEP else None, aid, out)

    def sla_holder(
        self, timer: Mapping[str, Any], activity: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """The record of the deadline a timer ``sla``/``sla_warning`` belongs to."""
        holder = self.state if timer.get("sla") == sla.PROCESS else activity
        found = (holder or {}).get("sla")
        return found if isinstance(found, dict) else None

    def sync_sla(self, timer: Mapping[str, Any]) -> None:
        """A deadline timer moved (data, calendar, resume): its record follows."""
        if timer["kind"] not in sla.SLA_TIMERS:
            return
        activity = self.state["activities"].get(timer.get("activity") or "")
        record = self.sla_holder(timer, activity)
        if record is None:
            return
        field_name = "dueAt" if timer["kind"] == sla.SLA else "warnAt"
        moment = timer["dueAt"] if timer["state"] != "frozen" else timer.get("frozenFrom")
        if moment is not None:
            record[field_name] = moment
        record["provisional"] = bool(timer["provisional"]) or any(
            bool(self.state["timers"].get(other or "", {}).get("provisional"))
            for other in (record.get("timer"), record.get("warnTimer"))
            if other != timer["id"]
        )

    def sla_addressees(self, activity: Mapping[str, Any] | None) -> dict[str, Any]:
        """Candidates of an SLA event's ``owner`` and ``assignee``: the chains, not ids.

        The assignee of a step is its ``assign`` chain as it stands (a
        reassignment replaces it; a ``call`` of an agent has one); a step
        without one (``approve``, ``call`` of a skill…) and the process scope
        have none. The application prefers the actual assignee of the step's
        task and looks further for a step without a chain.
        """
        return {
            "owner": self.owner_chain(),
            "assignee": list((activity or {}).get("assign") or ()),
        }

    def sla_fired(
        self, timer: Mapping[str, Any], activity: dict[str, Any] | None, body: Mapping[str, Any]
    ) -> None:
        """A deadline or its warning threshold passed while its step (process) is open."""
        record = self.sla_holder(timer, activity)
        if record is None:
            self.decide("ignored", reason="stale", input="timer", timerId=timer["id"])
            return
        scope = str(timer.get("sla") or sla.STEP)
        element = timer["element"] if scope == sla.STEP else None
        aid = activity["id"] if activity is not None and scope == sla.STEP else None
        common = {"scope": scope, "element": element, "attempt": None, "activityId": aid}
        if timer["kind"] == sla.SLA_WARNING:
            record["warnTimer"] = None
            if record["state"] != sla.PENDING:
                reason = "sla_" + record["state"]
                self.decide("ignored", element, reason=reason, timerId=timer["id"])
                return
            record["state"] = sla.WARNING
            self.decide(
                "sla_warning",
                element,
                scope=scope,
                activity=aid,
                dueAt=record["dueAt"],
                warnAt=timer["dueAt"],
            )
            self.emit(
                "process.sla_warning",
                **common,
                dueAt=record["dueAt"],
                warnAt=timer["dueAt"],
                provisional=bool(record["provisional"]),
                owner=None,
                assignee=None,
                addressees=self.sla_addressees(activity if scope == sla.STEP else None),
            )
            return
        # The engine's time of a timer is its due; when the core noticed it is the input's.
        detected = self.now
        if body.get("detectedAt"):
            detected = max(detected, _parse_time(str(body["detectedAt"])))
        overdue = sla.overdue_seconds(
            _parse_time(timer["dueAt"]), detected, record.get("overdueStops")
        )
        record.update(state=sla.BREACHED, timer=None, dueAt=timer["dueAt"])
        self.decide(
            "sla_breached",
            element,
            scope=scope,
            activity=aid,
            dueAt=timer["dueAt"],
            detectedAt=_rfc3339(detected),
            overdueSeconds=overdue,
        )
        self.emit(
            "process.sla_breached",
            **common,
            dueAt=timer["dueAt"],
            detectedAt=_rfc3339(detected),
            overdueSeconds=overdue,
            detectedBy="timer",
            provisional=bool(record["provisional"]),
            owner=None,
            assignee=None,
            addressees=self.sla_addressees(activity if scope == sla.STEP else None),
        )

    # --- deadlines of a migrated instance (CP-ADR-0074 §11, amendment 2026-09-29) --------

    def migrated(self) -> None:
        """The deadlines of an instance migrated to this version, counted by it.

        The core takes this input right after the migration's journal entry:
        the state is already the new version's, its deadlines still the old
        one's. Every open waiting step recounts its ``due`` from its
        activity's ``openedAt`` and the process its ``spec.due`` from the
        start (FR-024): a moved timer is ``process.timer_rescheduled``
        (``cause: migrated``), a deadline already past is one
        ``process.sla_breached`` (``detectedBy: migration``), escalation
        levels already past are skipped, not fired (decision B7), and the
        task of a ``human`` step gets the new due (``update_task_due``).
        Before SLA deadlines (revision 1) there is nothing to recount.
        """
        if not self.sla_on:
            self.decide("ignored", reason="no_deadlines", input="migrated")
            return
        for activity in sorted(self.state["activities"].values(), key=lambda a: a["n"]):
            entry = self.d.steps.get(str(activity.get("element")))
            if entry is None:
                continue
            kind = step_kind(entry.node)
            if activity["kind"] not in _DUE_ACTIVITIES.get(kind, ()):
                continue
            here = f"{entry.path}/{kind}"
            body = entry.node[kind]
            base = _parse_time(activity["openedAt"])
            values = self.values(self.state["threads"].get(activity["thread"]))
            pauses = self.deadline_pauses(self.deadline_timers(sla.STEP, activity))
            due, recipe, failed = self.recount(
                sla.STEP, entry.id, here + "/due", body.get("due"), values, activity, base
            )
            if kind in ("human", "approve"):
                self.recount_escalations(activity, here, body, recipe, failed, values, base, pauses)
            if activity["kind"] == "task" and activity.get("due") != due:
                activity["due"] = due
                self.intent("update_task_due", activityId=activity["id"], element=entry.id, due=due)
        self.recount(
            sla.PROCESS,
            sla.PROCESS,
            "/spec/due",
            self.d.spec.get("due"),
            self.values(),
            None,
            _parse_time(self.state["startedAt"]),
        )

    def recount(
        self,
        scope: str,
        element: str,
        path: str,
        value: Any,
        values: dict[str, Any],
        activity: dict[str, Any] | None,
        base: datetime,
    ) -> tuple[str | None, dict[str, Any] | None, ProcessError | None]:
        """One deadline counted by this version from ``base``: its moment, recipe and error.

        The timers of the deadline move (or are set, or cancelled); the
        record in the state is the new deadline's. Whether the deadline has
        passed is judged at the moment its clock stopped when its timer is
        frozen, otherwise now. The pauses the deadline's timer kept from past
        suspensions are added back (:meth:`paced`): a clock that stood still
        does not count against the deadline. ``deadline_migrated`` records
        what changed.
        """
        holder = activity if activity is not None else self.state
        old = holder.get("sla") if isinstance(holder.get("sla"), dict) else None
        timers = self.deadline_timers(scope, activity)
        # Before SLA deadlines the due of a human step was its task's alone.
        previous = old.get("dueAt") if old is not None else (activity or {}).get("due")
        aid = activity["id"] if activity is not None else None
        named = element if scope == sla.STEP else None
        report = {"scope": scope, "activity": aid, "previousDueAt": previous}
        if value is None:
            for timer in timers.values():
                self.drop_timer(timer, activity)
            if old is not None:
                holder.pop("sla")
                self.decide("deadline_migrated", named, **report, dueAt=None, breached=False)
            return None, None, None
        # The deadline keeps the pauses of its timer: a past suspension still counts.
        pauses = self.deadline_pauses(timers)
        paused: dict[str, Any] = {"paused": pauses} if pauses else {}
        try:
            due_recipe, warn_recipe = self.resolved(value, path, values, element)
            due, provisional, _ = self.paced(due_recipe, base, values, element, pauses)
            warn = None
            if warn_recipe is not None:
                warn, marked, _ = self.paced(warn_recipe, base, values, element, pauses)
                provisional = provisional or marked
        except _Raised as raised:
            for timer in timers.values():
                self.drop_timer(timer, activity)
            error = raised.error.out()
            holder["sla"] = sla.record(due_at=None, warn_at=None, provisional=False, error=error)
            if old is None or old.get("state") != sla.FAILED or old.get("error") != error:
                self.sla_failure(scope, named, aid, error)
                self.decide("deadline_migrated", named, **report, dueAt=None, breached=False)
            return None, None, raised.error
        due_timer = timers.get(sla.SLA)
        at = self.stopped_at(due_timer)
        due_at = _rfc3339(due)
        warn_at = _rfc3339(warn) if warn is not None else None
        breached = due <= at
        # The suspensions a past deadline sat through stay with the recounted one.
        stops: dict[str, Any] = (
            {"overdueStops": copy.deepcopy(old["overdueStops"])}
            if old is not None and old.get("overdueStops")
            else {}
        )
        if breached:
            if at < self.now and not any(
                s.get("to") is None for s in stops.get("overdueStops", ())
            ):
                # Past only by the new version, at the freeze: the clock stands with the
                # frozen timer, which goes with the breach, so the record keeps the
                # suspension open from then (a deadline past already has it, open_stops).
                stops["overdueStops"] = [
                    *stops.get("overdueStops", ()),
                    {"from": _rfc3339(at), "to": None},
                ]
            for timer in timers.values():
                self.drop_timer(timer, activity)
            record = sla.record(due_at=due_at, warn_at=warn_at, provisional=provisional)
            record.update(stops, state=sla.BREACHED)
            holder["sla"] = record
            if old is None or old.get("state") != sla.BREACHED:
                overdue = sla.overdue_seconds(due, at, stops.get("overdueStops"))
                self.migration_breach(scope, named, aid, due, overdue, provisional, activity)
        else:
            made = self.place_timer(
                due_timer, sla.SLA, due_recipe, element, values, activity, base, scope, **paused
            )
            warn_timer = timers.get(sla.SLA_WARNING)
            warned = warn is not None and warn <= at
            if warn_recipe is None or warned:
                if warn_timer is not None:
                    self.drop_timer(warn_timer, activity)
                warn_made = None
            else:
                warn_made = self.place_timer(
                    warn_timer,
                    sla.SLA_WARNING,
                    warn_recipe,
                    element,
                    values,
                    activity,
                    base,
                    scope,
                    **paused,
                )
            record = sla.record(
                due_at=due_at,
                warn_at=warn_at,
                provisional=provisional,
                timer=made,
                warn_timer=warn_made,
            )
            record.update(stops)
            if warned:
                # A threshold already past is not reported afterwards, like a past escalation.
                record["state"] = sla.WARNING
            holder["sla"] = record
        if old is None or previous != due_at or (old.get("state") == sla.BREACHED) != breached:
            self.decide("deadline_migrated", named, **report, dueAt=due_at, breached=breached)
        return due_at, due_recipe, None

    def frozen_at(self, timer: Mapping[str, Any]) -> datetime:
        """When a frozen timer was frozen.

        A timer frozen under revision 1 (before CP-ADR-0078, or by a version
        of revision 1 since) has no ``frozenAt``: what it keeps is counted up
        to the freeze, so the moment is ``frozenFrom`` less the remainder;
        with nothing kept it is unknown, and the clock stands now. A timer
        already due at the freeze kept ``0``: the moment comes out as its
        ``frozenFrom``, the earliest it could be, and what is left of a new
        moment after it is overstated by as much (CP-ADR-0078 §4).
        """
        if timer.get("frozenAt"):
            return _parse_time(timer["frozenAt"])
        if timer.get("frozenFrom") and timer.get("remaining") is not None:
            return _parse_time(timer["frozenFrom"]) - timedelta(seconds=timer["remaining"])
        return self.now

    def stopped_at(self, timer: Mapping[str, Any] | None) -> datetime:
        """The moment a timer's clock stands at: when it was frozen (:meth:`frozen_at`), or now."""
        if timer is not None and timer["state"] == "frozen":
            return self.frozen_at(timer)
        return self.now

    def deadline_timers(
        self, scope: str, activity: Mapping[str, Any] | None
    ) -> dict[str, dict[str, Any]]:
        """The live timers ``sla``/``sla_warning`` of a step's (the process's) deadline."""
        if activity is not None:
            found = [self.state["timers"].get(t) for t in activity["timers"]]
        else:
            found = [t for t in self.state["timers"].values() if t.get("sla") == scope]
        return {
            timer["kind"]: timer
            for timer in found
            if timer is not None and timer["kind"] in sla.SLA_TIMERS
        }

    @staticmethod
    def deadline_pauses(timers: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]] | None:
        """The pauses a deadline's timer ``sla`` keeps (:meth:`note_pause`)."""
        due_timer = timers.get(sla.SLA)
        return copy.deepcopy(due_timer.get("paused")) if due_timer is not None else None

    def drop_timer(self, timer: Mapping[str, Any], activity: dict[str, Any] | None) -> None:
        self.cancel_timer(timer["id"])
        if activity is not None and timer["id"] in activity["timers"]:
            activity["timers"].remove(timer["id"])

    def place_timer(
        self,
        timer: dict[str, Any] | None,
        kind: str,
        recipe: dict[str, Any],
        element: str,
        values: dict[str, Any],
        activity: dict[str, Any] | None,
        base: datetime,
        scope: str | None = None,
        **extra: Any,
    ) -> str:
        """A timer of a recounted deadline: the live one moved, or a new one from ``base``."""
        if timer is not None:
            timer.update(extra)
            self.move_timer(timer, recipe, base, values)
            return str(timer["id"])
        if scope is not None:
            extra["sla"] = scope
        if activity is not None:
            return self.add_timer(activity, kind, recipe, values, base=base, **extra)
        return self.new_timer(kind, element, recipe, base, values, scope=scope, **extra)

    def move_timer(
        self, timer: dict[str, Any], recipe: dict[str, Any], base: datetime, values: dict[str, Any]
    ) -> None:
        """A live timer counted by another recipe from ``base``: ``timer_rescheduled``.

        A frozen timer keeps what is left of its new moment from when it was
        frozen; ``process.timer_rescheduled`` comes on resume, as after a new
        calendar version.
        """
        due, provisional, reads = self.paced(
            recipe, base, values, timer["element"], timer.get("paused")
        )
        timer.update(recipe=recipe, base=_rfc3339(base), reads=sorted(reads))
        if timer["state"] == "frozen":
            frozen_at = self.frozen_at(timer)
            remaining: float | None = None if reads else max(0.0, (due - frozen_at).total_seconds())
            unit = sla.WALL
            if timer["kind"] in sla.SLA_TIMERS:
                with contextlib.suppress(sla.DeadlineError):
                    remaining, unit = sla.remainder(recipe, due, frozen_at, self.input.calendars)
            # A timer frozen under revision 1 keeps the moment made out for it.
            timer["frozenAt"] = _rfc3339(frozen_at)
            previous = timer["frozenFrom"]
            if (_rfc3339(due), remaining, unit, provisional) == (
                previous,
                timer["remaining"],
                timer.get("remainingUnit") or sla.WALL,
                timer["provisional"],
            ):
                return
            timer.update(
                frozenFrom=_rfc3339(due),
                remaining=remaining,
                remainingUnit=unit,
                provisional=provisional,
            )
            self.decide(
                "timer_rescheduled",
                timer["element"],
                timerId=timer["id"],
                previousDueAt=previous,
                dueAt=None,
                frozenDueAt=timer["frozenFrom"],
                remainingSeconds=remaining,
                remainingUnit=unit,
                cause="migrated",
            )
            self.intent("set_timer", **self.timer_row(timer))
            return
        previous = timer["dueAt"]
        if _rfc3339(due) == previous and provisional == timer["provisional"]:
            return
        timer.update(dueAt=_rfc3339(due), provisional=provisional)
        self.decide(
            "timer_rescheduled",
            timer["element"],
            timerId=timer["id"],
            previousDueAt=previous,
            dueAt=timer["dueAt"],
            cause="migrated",
        )
        self.intent("set_timer", **self.timer_row(timer))
        self.emit(
            "process.timer_rescheduled",
            timerId=timer["id"],
            element=timer["element"],
            previousDueAt=previous,
            dueAt=timer["dueAt"],
            provisional=provisional,
            cause="migrated",
            changedFields=[],
        )

    def migration_breach(
        self,
        scope: str,
        element: str | None,
        aid: str | None,
        due: datetime,
        overdue: int,
        provisional: bool,
        activity: dict[str, Any] | None,
    ) -> None:
        """A recounted deadline already past: one breach, found by the migration.

        ``overdue`` is counted to the moment the deadline's clock stands at, the
        suspensions it sat through past due left out, as a timer's breach is.
        """
        self.decide(
            "sla_breached",
            element,
            scope=scope,
            activity=aid,
            dueAt=_rfc3339(due),
            detectedAt=_rfc3339(self.now),
            overdueSeconds=overdue,
            detectedBy="migration",
        )
        self.emit(
            "process.sla_breached",
            scope=scope,
            element=element,
            attempt=None,
            activityId=aid,
            dueAt=_rfc3339(due),
            detectedAt=_rfc3339(self.now),
            overdueSeconds=overdue,
            detectedBy="migration",
            provisional=provisional,
            owner=None,
            assignee=None,
            addressees=self.sla_addressees(activity),
        )

    def recount_escalations(
        self,
        activity: dict[str, Any],
        here: str,
        body: Mapping[str, Any],
        due_recipe: dict[str, Any] | None,
        failed: ProcessError | None,
        values: dict[str, Any],
        base: datetime,
        pauses: list[dict[str, Any]] | None = None,
    ) -> None:
        """Escalation levels (and ``onDue``) of a ``human``/``approve`` step, from the new due.

        A level whose moment has passed is skipped, not fired
        (``escalation_skipped``, ``reason: migrated``); a future one moves or
        is set. A level the new version no longer has is cancelled. A level
        that fired on the old version has no timer left, so it is not told
        from one the new version adds: a future moment sets it again. A
        level keeps the pauses of its timer; a new one takes its deadline's
        (``pauses``).
        """
        live = [self.state["timers"].get(t) for t in list(activity["timers"])]
        levels = {t["level"]: t for t in live if t is not None and t["kind"] == "escalation"}
        on_due = next((t for t in live if t is not None and t["kind"] == "due"), None)
        element = str(activity["element"])
        timers: list[tuple[dict[str, Any] | None, str, dict[str, Any], dict[str, Any]]] = []
        if body.get("onDue") in ("approve", "reject") and due_recipe is not None:
            timers.append((on_due, "due", due_recipe, {"onDue": body["onDue"]}))
        elif on_due is not None:
            self.drop_timer(on_due, activity)
        for index, escalation in enumerate(body.get("escalations") or ()):
            level = index + 1
            old = levels.pop(level, None)
            if failed is not None:
                if old is not None:
                    self.drop_timer(old, activity)
                self.decide("escalation_skipped", element, level=level, reason="due_failed")
                continue
            after = escalation["after"]
            recipe: dict[str, Any] = {
                "kind": "after",
                "due": due_recipe,
                "after": None
                if after == "due"
                else _recipe(after, f"{here}/escalations/{index}/after"),
            }
            extra: dict[str, Any] = {"level": level, "escalation": f"{here}/escalations/{index}"}
            timers.append((old, "escalation", recipe, extra))
        for old in levels.values():
            self.drop_timer(old, activity)
        for old, kind, recipe, extra in timers:
            # A live timer keeps its own pauses; a new one takes its deadline's.
            own = old.get("paused") if old is not None else pauses
            if old is None and own:
                extra["paused"] = copy.deepcopy(own)
            try:
                moment, _, _ = self.paced(recipe, base, values, element, own)
            except _Raised as raised:
                if old is not None:
                    self.drop_timer(old, activity)
                self.decide(
                    "escalation_skipped",
                    element,
                    level=extra.get("level"),
                    reason="due_failed",
                    error=raised.error.out(),
                )
                continue
            if moment <= self.stopped_at(old):
                if old is not None:
                    self.drop_timer(old, activity)
                self.decide(
                    "escalation_skipped",
                    element,
                    level=extra.get("level"),
                    reason="migrated",
                    dueAt=_rfc3339(moment),
                    **({"onDue": extra["onDue"]} if "onDue" in extra else {}),
                )
                continue
            self.place_timer(old, kind, recipe, element, values, activity, base, **extra)

    # --- retrospective ----------------------------------------------------------------

    def start_retrospective(self) -> None:
        spec = self.d.spec.get("retrospective")
        if not isinstance(spec, dict):
            return
        if spec.get("when") is not None:
            try:
                if (
                    self.evaluate("/spec/retrospective/when", self.values(), "retrospective")
                    is not True
                ):
                    self.decide("retrospective_skipped", "retrospective", when=spec["when"])
                    return
            except _Raised as raised:
                self.decide("retrospective_skipped", "retrospective", error=raised.error.out())
                return
        projection = self.projection(quiet=True)
        case = (projection or {}).get("case") or {}
        if not case.get("key"):
            # The skill takes the case node, and a lesson learned from no case is found by nothing.
            self.decide("retrospective_skipped", "retrospective", error={"type": "case_unknown"})
            return
        skill = spec.get("skill") or DEFAULT_RETROSPECTIVE_SKILL
        activity = self.open_activity("", None, "retro_skill", element="retrospective", skill=skill)
        self.state["retrospective"] = {"phase": "proposing", "activity": activity["id"]}
        # The input of process.retrospective@1; the executor attaches the journal (CP-ADR-0076 §6).
        self.intent(
            "invoke_skill",
            activityId=activity["id"],
            element="retrospective",
            skill=skill,
            input={
                "case": {
                    "kind": case["kind"],
                    "key": case["key"],
                    "title": _text_or_none(case.get("title")),
                },
                "definitionKey": self.d.key,
                "version": self.state["version"],
                "instanceId": self.instance_id,
                "outcome": self.state["outcome"],
                "data": self.state["data"],
                "entities": [
                    {
                        "kind": entity["kind"],
                        "key": entity["key"],
                        "name": _text_or_none(entity.get("name")),
                        "rel": entity.get("rel"),
                    }
                    for entity in (projection or {}).get("entities") or ()
                ][:RETROSPECTIVE_ENTITIES],
                "appliesToKinds": list(spec.get("appliesTo") or ()),
            },
            attach=["journal"],
        )

    def case_nodes(self) -> dict[str, str]:
        """The kind of each node of the case projection by its key: the case, then its entities."""
        projection = self.projection(quiet=True) or {}
        nodes: dict[str, str] = {}
        for node in [projection.get("case") or {}, *(projection.get("entities") or ())]:
            if node.get("key"):
                nodes.setdefault(str(node["key"]), str(node["kind"]))
        return nodes

    def lesson_targets(self, lesson: Mapping[str, Any], nodes: Mapping[str, str]) -> list[Any]:
        """``appliesTo`` of a lesson as ``{kind, key}``: a bare key is a node of the case."""
        targets: list[Any] = []
        for target in lesson.get("appliesTo") or ():
            if isinstance(target, Mapping):
                if target.get("kind") and target.get("key"):
                    targets.append({"kind": str(target["kind"]), "key": str(target["key"])})
            elif isinstance(target, str) and target in nodes:
                targets.append({"kind": nodes[target], "key": target})
        return targets

    def retrospective_input(self) -> bool:
        if self.input.kind not in ("task", "skill", "intent_failed"):
            return False
        aid = str(self.input.body.get("activityId") or "")
        activity = self.state["activities"].get(aid)
        if activity is None or not activity["kind"].startswith("retro_"):
            return False
        if self.input.kind == "intent_failed":
            self.intent_failed(self.input.body)
        else:
            self.retrospective_answer(activity, self.input.kind, self.input.body)
        return True

    def retrospective_answer(
        self, activity: dict[str, Any], kind: str, body: Mapping[str, Any]
    ) -> None:
        spec = self.d.spec["retrospective"]
        self.close_activity(activity["id"])
        if activity["kind"] == "retro_skill":
            if body.get("status") != "succeeded":
                error = body.get("error") or {}
                self.state["retrospective"] = {
                    "phase": "failed",
                    "error": {"type": error.get("code") or "skill_failed"},
                }
                self.decide(
                    "retrospective_failed",
                    "retrospective",
                    error=self.state["retrospective"]["error"],
                )
                return
            lessons = list((body.get("output") or {}).get("lessons") or ())
            if not lessons:
                self.state["retrospective"] = {"phase": "done", "confirmed": 0}
                self.decide("retrospective_done", "retrospective", proposed=0, confirmed=0)
                return
            nodes = self.case_nodes()
            lessons = [
                {**lesson, "appliesTo": self.lesson_targets(lesson, nodes)}
                if isinstance(lesson, Mapping)
                else lesson
                for lesson in lessons
            ]
            values = self.values()
            assign = self.assignees(
                "/spec/retrospective/assign", spec["assign"], values, "retrospective"
            )
            review = self.open_activity(
                "", None, "retro_review", element="retrospective", proposed=len(lessons)
            )
            self.state["retrospective"] = {"phase": "review", "activity": review["id"]}
            self.decide("retrospective_proposed", "retrospective", proposed=len(lessons))
            self.intent(
                "create_task",
                activityId=review["id"],
                element="retrospective",
                taskType=spec["taskType"],
                title="retrospective",
                form=None,
                assign=assign,
                due=None,
                escalations=[],
                context=None,
                input={"lessons": lessons, "outcome": self.state["outcome"]},
                externalRef=self.external_ref("retrospective"),
            )
            return
        if body.get("status") != "completed":
            self.state["retrospective"] = {"phase": "done", "confirmed": 0}
            self.decide(
                "retrospective_done",
                "retrospective",
                proposed=activity.get("proposed"),
                confirmed=0,
            )
            return
        # Only what the person confirmed or edited becomes knowledge (TAI-ADR-0054 R18, art. IV).
        answered = list(((body.get("task") or {}).get("customFields") or {}).get("lessons") or ())
        confirmed = [
            lesson
            for lesson in answered
            if isinstance(lesson, dict) and lesson.get("decision") in ("confirm", "edit")
        ]
        case = self.case_key()
        nodes = self.case_nodes()
        for number, lesson in enumerate(confirmed):
            links = [{"rel": "learned_from", "kind": self.case_kind(), "key": case}] if case else []
            for target in self.lesson_targets(lesson, nodes):
                link = {"rel": "applies_to", **target}
                if link not in links:
                    links.append(link)
            text = lesson.get("text")
            if lesson.get("decision") == "edit" and lesson.get("editedText"):
                text = lesson["editedText"]
            key = lesson.get("key")
            self.remember(
                "retrospective",
                {
                    "entity": {
                        "kind": "lesson",
                        # The skill's key names the lesson by its case and text: a
                        # repeated retrospective does not make a second node.
                        "key": key
                        if isinstance(key, str) and key
                        else f"lesson:{self.instance_id}:{number + 1}",
                        "text": str(text or ""),
                        "links": links,
                    },
                    "evidence": list(lesson.get("evidence") or ()),
                },
            )
        self.state["retrospective"] = {"phase": "done", "confirmed": len(confirmed)}
        self.decide(
            "retrospective_done", "retrospective", proposed=len(answered), confirmed=len(confirmed)
        )

    # --- memory -----------------------------------------------------------------------

    def case_kind(self) -> str:
        memory = self.d.spec.get("memory") or {}
        return str((memory.get("case") or {}).get("kind") or "case")

    def case_key(self) -> str | None:
        memory = self.d.spec.get("memory")
        if not isinstance(memory, dict):
            return None
        try:
            return _key_text(self.evaluate("/spec/memory/case/key", self.values(), "memory"))
        except _Raised:
            return None

    def projection(self, *, quiet: bool = False) -> dict[str, Any] | None:
        """The case projection the process declares (CP-ADR-0076 §2), computed now.

        ``quiet`` — a read of the projection, not its event: paths that fail
        are not a decision.
        """
        memory = self.d.spec.get("memory")
        if not isinstance(memory, dict):
            return None
        values = self.values()
        failed: list[str] = []

        def value(path: str) -> Any:
            try:
                return _jsonable(self.evaluate(path, values, "memory"))
            except _Raised:
                failed.append(path)
                return None

        case = memory["case"]
        out: dict[str, Any] = {
            "case": {
                "kind": case.get("kind") or "case",
                "key": _key_text(value("/spec/memory/case/key")),
                "title": value("/spec/memory/case/title")
                if case.get("title") is not None
                else None,
            },
            "facts": {
                name: value(f"/spec/memory/facts{pointer(name)}")
                for name in memory.get("facts") or {}
            },
            "entities": [],
            "documents": dict(memory.get("documents") or {}),
        }
        for index, entity in enumerate(memory.get("entities") or ()):
            here = f"/spec/memory/entities/{index}"
            if entity.get("when") is not None and value(here + "/when") is not True:
                continue
            keys = value(here + "/key")
            name = value(here + "/name") if entity.get("name") is not None else None
            for key in keys if entity.get("many") and isinstance(keys, list) else [keys]:
                text = _key_text(key)
                if text is not None:
                    out["entities"].append(
                        {
                            "kind": entity["kind"],
                            "key": text,
                            "name": name,
                            "rel": entity.get("rel"),
                        }
                    )
        if failed and not quiet:
            self.decide("projection_incomplete", "memory", paths=failed)
        return out

    # --- small queries ----------------------------------------------------------------

    def sorted_activities(self, kind: str) -> list[dict[str, Any]]:
        return sorted(
            (a for a in self.state["activities"].values() if a["kind"] == kind),
            key=lambda a: a["n"],
        )


# --- quorum -------------------------------------------------------------------------------


def _quorum(activity: Mapping[str, Any]) -> str | None:
    """``approved``/``rejected`` once the votes decide, ``None`` while they do not.

    ``total`` is the number of approvals the activity has (not cancelled). With
    ``earlyDecision`` the outcome is taken as soon as it cannot change.
    """
    total = activity.get("total")
    if total is None:
        return None
    votes = activity["votes"].values()
    approved = sum(1 for v in votes if v["outcome"] == "approved")
    rejected = sum(1 for v in votes if v["outcome"] == "rejected")
    pending = max(0, total - approved - rejected)
    quorum = activity["quorum"]
    if total <= 0:
        return "rejected"
    if quorum == "all":
        need = total
    elif quorum == "any":
        need = 1
    elif isinstance(quorum, dict) and "atLeast" in quorum:
        need = int(quorum["atLeast"])
    else:
        need = max(1, math.ceil(float(quorum["percent"]) * total / 100 - 1e-9))
    if approved >= need and (activity["early"] or pending == 0):
        return "approved"
    if approved + pending < need and (activity["early"] or pending == 0):
        return "rejected"
    return None


# --- values ---------------------------------------------------------------------------------


_EVENT_FIELDS = (
    "id",
    "type",
    "time",
    "entityType",
    "entityId",
    "actorId",
    "correlationId",
    "payload",
)


def _event_var(event: Mapping[str, Any]) -> dict[str, Any]:
    out = {name: event.get(name) for name in _EVENT_FIELDS if event.get(name) is not None}
    out.setdefault("payload", {})
    return out


def _trigger_ref(event: Mapping[str, Any]) -> dict[str, Any]:
    return {"eventId": event.get("id"), "type": _trigger_type(event)}


def _trigger_type(event: Mapping[str, Any]) -> str:
    observation = event.get("observation")
    return f"observation:{observation}" if observation else str(event.get("type") or "")


def _native(value: Any) -> Any:
    """A CEL result as plain Python: messages become dicts, well-known types their values."""
    if isinstance(value, Message):
        return _message(value)
    if isinstance(value, Mapping):
        return {str(k): _native(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_native(v) for v in value]
    return value


_WRAPPERS = frozenset(
    f"google.protobuf.{name}Value"
    for name in ("Bool", "String", "Bytes", "Int32", "Int64", "UInt32", "UInt64", "Float", "Double")
)


def _message(message: Message) -> Any:
    name = message.DESCRIPTOR.full_name
    if name == "google.protobuf.Timestamp":
        return message.ToDatetime(tzinfo=UTC)  # type: ignore[attr-defined]
    if name == "google.protobuf.Duration":
        return message.ToTimedelta()  # type: ignore[attr-defined]
    if name in _WRAPPERS:
        return message.value  # type: ignore[attr-defined]
    if name in ("google.protobuf.Struct", "google.protobuf.Value", "google.protobuf.ListValue"):
        return json_format.MessageToDict(message)
    # Not ListFields(): it leaves out empty lists and maps and zero plain scalars,
    # and a recall with no nodes must still read ``nodes == []`` (TASK-001134).
    # Only a field that tracks presence and is unset is left out.
    out: dict[str, Any] = {}
    for descriptor in message.DESCRIPTOR.fields:
        value = getattr(message, descriptor.name)
        if descriptor.message_type is not None and descriptor.message_type.GetOptions().map_entry:
            out[descriptor.name] = {str(k): _native(v) for k, v in value.items()}
        elif descriptor.is_repeated:
            out[descriptor.name] = [_native(v) for v in value]
        elif not descriptor.has_presence or message.HasField(descriptor.name):
            out[descriptor.name] = _native(value)
    return out


def _jsonable(value: Any) -> Any:
    """A value as it is stored: RFC 3339 times, CEL durations, integral numbers as integers."""
    if isinstance(value, datetime):
        return _rfc3339(value)
    if isinstance(value, timedelta):
        return _duration_text(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, Message):
        return _jsonable(_native(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _duration_text(value: timedelta) -> str:
    """A duration as CEL writes it: ``259200s``, ``1.5s``."""
    micro = value // timedelta(microseconds=1)
    seconds, rest = divmod(abs(micro), 1_000_000)
    sign = "-" if micro < 0 else ""
    return f"{sign}{seconds}.{rest:06d}s".rstrip("0") if rest else f"{sign}{seconds}s"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _iso(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    return f"PT{seconds}S"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _put(target: dict[str, Any], path: str, value: Any) -> None:
    """Write ``value`` at a dotted path, making the objects on the way."""
    *parents, last = path.split(".")
    node = target
    for name in parents:
        child = node.get(name)
        if not isinstance(child, dict):
            child = {}
            node[name] = child
        node = child
    node[last] = value


def _diff(before: Any, after: Any, prefix: str = "") -> set[str]:
    """Dotted paths of the leaves that differ; objects are compared field by field."""
    if isinstance(before, dict) and isinstance(after, dict):
        changed: set[str] = set()
        for name in set(before) | set(after):
            path = f"{prefix}.{name}" if prefix else name
            changed |= _diff(before.get(name), after.get(name), path)
        return changed
    return set() if before == after else {prefix}


def _touches(reads: Sequence[str], fields: Sequence[str]) -> bool:
    for read in reads:
        read_path = read.split("[")[0]
        for changed in fields:
            if (
                read_path == changed
                or read_path.startswith(changed + ".")
                or changed.startswith(read_path + ".")
            ):
                return True
    return False


def _recipe(value: Any, path: str) -> dict[str, Any]:
    """How a ``durationOrCel`` gives a moment: ``{kind: duration}`` or ``{kind: at, path}``."""
    if isinstance(value, str):
        return {"kind": "duration", "value": value}
    return {"kind": "at", "path": path + "/at"}


def _retry_delay(retry: Mapping[str, Any], attempt: int) -> timedelta:
    delay = parse_iso_duration(retry["delay"]) if retry.get("delay") else timedelta(0)
    delay = delay or timedelta(0)
    if retry.get("backoff") == "exponential":
        delay = delay * (2 ** (attempt - 1))
    if retry.get("maxDelay"):
        ceiling = parse_iso_duration(retry["maxDelay"])
        if ceiling is not None:
            delay = min(delay, ceiling)
    return delay


def _at_pointer(document: Any, path: str) -> Any:
    node = document
    for part in path.lstrip("/").split("/")[1:]:  # the pointer starts at /spec
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _assignee_text(assignee: Mapping[str, Any]) -> str:
    if assignee.get("agent"):
        return f"agent:{assignee['agent']}"
    if assignee.get("role"):
        return f"role:{assignee['role']}"
    return str(assignee.get("principal") or "")


def assignee_of_text(target: str) -> dict[str, str]:
    """The candidate a ``to`` target of ``process.escalated`` names: its text form read back.

    The inverse of the event's text form (``role:<slug>``, ``agent:<key>`` or a
    principal id), so the application resolves the targets from the payload
    and the engine's intents stay as they were (CP-ADR-0078, amendment 2026-09-30).
    """
    for prefix in ("agent", "role"):
        if target.startswith(prefix + ":"):
            return {prefix: target[len(prefix) + 1 :]}
    return {"principal": target}
