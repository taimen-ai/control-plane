"""Replaying an instance from its journal, and memory answers of a package test (CP-ADR-0076 §4).

The engine is a pure function (CP-ADR-0074 §4): the same inputs give the same
decisions and intents. The journal of an instance records every input whole
— a memory answer included (``recall``: ``{activityId, status, result,
reason}``) — with the calendar versions it was computed on. :func:`replay`
feeds the recorded inputs to :func:`process_engine.step` again and lists
where the decisions or the intents differ from the recorded ones. Memory is
never asked: the engine sees memory only through the recorded ``recall``
inputs (SC-011).

The engine revision is the one of the version record the definition was
built from (``process_definitions.engine_revision``, CP-ADR-0074, amendment
2026-09-29): an instance replays under the revision it ran under, and one
migrated since runs under its target version's revision from the migration
on — the entry the replay starts from, followed by the engine's input
``migrated`` that counted its deadlines by that version.

A candidate version replays the journal the same way
(``POST /process-definitions/{key}:replay``, CP-ADR-0074 §10):
:func:`as_version` runs it under the number and the engine revision of the
instance's version, and :func:`first_divergence` names the first journal
entry where the paths part.

A package test has no journal: its ``mocks.recall`` answer the ``recall``
steps (:func:`mock_recall`), checked against the form of a memory answer.

Pure functions over plain values; no I/O.
"""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from control_plane.domain import process_engine as engine
from control_plane.domain.calendar import Calendar
from control_plane.domain.cel_profile import ExpressionError, environment
from control_plane.domain.process_migration import MIGRATION_INPUT

# What memory answers a recall step (CP-ADR-0076 §4): nodes, edges, truncated.
RECALL_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "key"],
                "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "key": {"type": "string", "minLength": 1},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "attributes": {"type": "object"},
                    "anchor": {"type": "boolean"},
                    "inferred": {"type": "boolean"},
                    "validFrom": {"type": "string"},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["relation", "from", "to"],
                "properties": {
                    "relation": {"type": "string", "minLength": 1},
                    "from": {"type": "string", "minLength": 1},
                    "to": {"type": "string", "minLength": 1},
                    "inferred": {"type": "boolean"},
                    "validFrom": {"type": "string"},
                },
            },
        },
        "truncated": {"type": "boolean"},
    },
}
_ANSWER = Draft202012Validator(RECALL_ANSWER_SCHEMA)


class MockError(ValueError):
    """``mocks.recall`` cannot answer: no answer for the step, or not a memory answer."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


# --- replay --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Discrepancy:
    """Where the replayed step differs from the recorded one."""

    seq: int
    field: str
    recorded: Any
    replayed: Any

    def out(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "field": self.field,
            "recorded": self.recorded,
            "replayed": self.replayed,
        }


@dataclass
class Replay:
    steps: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)
    state: dict[str, Any] | None = None


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _intent(recorded: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded intent without what became of it (``executed``)."""
    return {k: v for k, v in recorded.items() if k != "executed"}


def replay(
    definition: engine.Definition,
    entries: Sequence[Mapping[str, Any]],
    calendars: Callable[[Mapping[str, int]], Mapping[str, Calendar]],
    *,
    stop: bool = False,
    settings: Callable[[int, int], Mapping[str, Any] | None] | None = None,
    definitions: Mapping[int, engine.Definition] | None = None,
) -> Replay:
    """Take the recorded inputs again, in ``seq`` order, and compare every step.

    ``entries`` are journal records ``{seq, input: {kind, at, body, actorId},
    decisions, intents, calendars}``; ``calendars`` gives the calendar objects
    of the versions an entry names. An input the engine refuses is a
    discrepancy of that step (``input``), and the replay stops there: the
    state after it is unknown. ``stop`` ends the replay after the first step
    that differs: past it the paths have parted and every later step is
    compared against another history.

    A migration (``input.kind: migrate``, CP-ADR-0074 §11) moved the
    instance to another version with the state it records: the replay starts
    from the last one, since the entries before it ran on another version.
    The input ``migrated`` after it — the deadlines counted by the new
    version — is the first step replayed: an input of the engine like any
    other, recorded with the calendar versions it counted by.

    A record that names the settings of the package (``settingsVersion``,
    ``settingsSchemaRevision``, CP-ADR-0081 §6) is taken with the effective
    values of that pair — ``settings`` gives them from the history — by
    ``definition`` typed with that revision of the schema (``definitions``;
    a candidate not in it keeps its own types), not with the current ones. A pair
    the history lacks is a discrepancy of its own (``settings``) and the
    replay stops there: substituting other values would replay another case.
    """
    result = Replay()
    state: dict[str, Any] | None = None
    ordered = sorted(entries, key=lambda e: int(e["seq"]))
    start = 0
    for index, entry in enumerate(ordered):
        if entry["input"].get("kind") == MIGRATION_INPUT:
            state = copy.deepcopy(dict((entry["input"].get("body") or {}).get("state") or {}))
            start = index + 1
    for entry in ordered[start:]:
        seq = int(entry["seq"])
        recorded = entry["input"]
        here = definition
        values: Mapping[str, Any] | None = None
        version, revision = entry.get("settingsVersion"), entry.get("settingsSchemaRevision")
        if version is not None and revision is not None:
            values = settings(int(version), int(revision)) if settings is not None else None
            if definition.reads_settings:
                here = (definitions or {}).get(int(revision), definition)
            if values is None:
                pair = {"version": int(version), "schemaRevision": int(revision)}
                result.discrepancies.append(Discrepancy(seq, "settings", pair, None))
                result.steps += 1
                return result
        given = engine.Input(
            str(recorded["kind"]),
            _time(str(recorded["at"])),
            dict(recorded.get("body") or {}),
            recorded.get("actorId"),
            calendars(dict(entry.get("calendars") or {})),
            settings=values,
        )
        try:
            state, decisions, intents = engine.step(here, state, given)
        except engine.EngineError as exc:
            result.discrepancies.append(Discrepancy(seq, "input", recorded, str(exc)))
            result.steps += 1
            return result
        replayed = {
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
        }
        expected = {
            "decisions": list(entry.get("decisions") or ()),
            "intents": [_intent(i) for i in entry.get("intents") or ()],
        }
        for name in ("decisions", "intents"):
            if replayed[name] != expected[name]:
                result.discrepancies.append(Discrepancy(seq, name, expected[name], replayed[name]))
        result.steps += 1
        if stop and result.discrepancies:
            break
    result.state = state
    return result


# --- a candidate version against real journals -------------------------------------------


def as_version(
    definition: engine.Definition, version: int, engine_revision: int
) -> engine.Definition:
    """The candidate under the number and the engine revision of the version an instance ran.

    The number is not behaviour, yet the engine writes it into the state and
    into every ``process.*`` event: a candidate under its own number would
    differ from the journal on every step it emits. The revision is
    behaviour, but not the candidate's: the instance keeps its revision until
    a migration moves it, so the candidate is compared under the semantics
    its journal was recorded with. The check and the compiled programs stay
    those of the candidate.
    """
    if definition.version != version:
        definition = replace(definition, spec={**definition.spec, "version": version})
    if definition.engine_revision != engine_revision:
        definition = replace(definition, engine_revision=engine_revision)
    return definition


@dataclass(frozen=True)
class Divergence:
    """The first entry of an instance journal where the candidate decides otherwise.

    ``kind`` — ``decision`` or ``intent`` (the first one that differs, with its
    element), ``input`` (the candidate refused a recorded input), ``settings``
    (the version or the schema revision of the settings a record names is not
    in the database, ``recorded: {version, schemaRevision}``), or, when
    every step matched, what of the final state differs: ``data``, ``timer``,
    ``state``.
    """

    seq: int
    kind: str
    element: str | None
    recorded: Any
    replayed: Any

    def out(self) -> dict[str, Any]:
        return {
            "journalSeq": self.seq,
            "kind": self.kind,
            "element": self.element,
            "recorded": self.recorded,
            "replayed": self.replayed,
        }


def _element(*items: Any) -> str | None:
    for item in items:
        if isinstance(item, Mapping) and item.get("element") is not None:
            return str(item["element"])
    return None


def _first_different(recorded: Sequence[Any], replayed: Sequence[Any]) -> tuple[Any, Any]:
    for index in range(max(len(recorded), len(replayed))):
        mine = recorded[index] if index < len(recorded) else None
        theirs = replayed[index] if index < len(replayed) else None
        if mine != theirs:
            return mine, theirs
    return None, None


_SINGULAR = {"decisions": "decision", "intents": "intent"}


def first_divergence(
    result: Replay, stored: Mapping[str, Any] | None, last_seq: int
) -> Divergence | None:
    """Where the replay parted from the journal first, or ``None`` when it never did.

    ``stored`` is the instance's state as the core keeps it: a replay that
    matched every step is still compared with it — a ``set`` that computes
    another value decides nothing of its own, only the data shows it.
    """
    if result.discrepancies:
        seq = min(d.seq for d in result.discrepancies)
        first = next(d for d in result.discrepancies if d.seq == seq)
        if first.field in ("input", "settings"):
            return Divergence(seq, first.field, None, first.recorded, first.replayed)
        mine, theirs = _first_different(first.recorded, first.replayed)
        return Divergence(seq, _SINGULAR[first.field], _element(mine, theirs), mine, theirs)
    stored = dict(stored or {}) or None
    if result.state == stored:
        return None
    before, after = stored or {}, result.state or {}
    for key, kind in (("data", "data"), ("timers", "timer")):
        if before.get(key) != after.get(key):
            return Divergence(last_seq, kind, None, before.get(key), after.get(key))
    return Divergence(last_seq, "state", None, stored, result.state)


# --- mocks.recall ----------------------------------------------------------------------------


def _matches(when: str | None, request: Mapping[str, Any]) -> bool:
    if not when:
        return True
    try:
        program = environment(bindings={"input": None}).compile(when, path="/mocks/recall/when")
        return program.evaluate({"input": dict(request)}).value is True
    except ExpressionError as exc:
        raise MockError("invalid_mock", f"when of a recall mock: {exc.message}") from exc


def mock_recall(
    mocks: Sequence[Mapping[str, Any]],
    intent: Mapping[str, Any],
    used: dict[int, int] | None = None,
) -> dict[str, Any]:
    """The ``recall`` input a package test's ``mocks.recall`` gives to a recall intent.

    The first answer whose ``step`` is the intent's element (or names no step)
    and whose ``when`` — CEL over the request (``input``: the intent) — holds.
    ``output`` is checked against the form of a memory answer;
    ``timeout: true`` and ``error`` are the step's timeout, the error's
    ``type`` its reason. ``used`` counts how often each answer answered, for
    coverage; answers are not consumed.
    """
    request = {k: v for k, v in intent.items() if k not in ("recallId", "activityId")}
    for index, mock in enumerate(mocks):
        if mock.get("step") not in (None, intent.get("element")):
            continue
        if not _matches(mock.get("when"), request):
            continue
        if used is not None:
            used[index] = used.get(index, 0) + 1
        body: dict[str, Any] = {"activityId": intent.get("activityId")}
        if mock.get("timeout") is True:
            return {**body, "status": "timed_out", "reason": "timeout"}
        if mock.get("error") is not None:
            reason = str((mock["error"] or {}).get("type") or "memory_unavailable")
            return {**body, "status": "timed_out", "reason": reason}
        output = mock.get("output")
        errors = sorted(_ANSWER.iter_errors(output), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            raise MockError(
                "mock_output_invalid",
                f"mocks.recall[{index}].output is not a memory answer: {first.message}",
                path="/" + "/".join(map(str, first.path)),
                mock=index,
            )
        answer = dict(output or {})
        return {
            **body,
            "status": "completed",
            "result": {
                "nodes": list(answer.get("nodes") or ()),
                "edges": list(answer.get("edges") or ()),
                "truncated": bool(answer.get("truncated")),
            },
        }
    raise MockError(
        "mock_missing",
        f"no mocks.recall answer for step {intent.get('element')!r}",
        step=intent.get("element"),
    )


# --- the journal as entries ----------------------------------------------------------------

# Journal entry kinds of ProcessJournalEntryOut, by engine decision.
_JOURNAL_KIND = {
    "stage_entered": "stage",
    "stage_exited": "stage",
    "milestone_reached": "milestone",
    "milestone_lost": "milestone",
    "timer_set": "timer",
    "timer_fired": "timer",
    "timer_rescheduled": "timer",
    "timer_kept": "timer",
    "escalated": "timer",
    "vote": "vote",
    "approval_decided": "vote",
    "recall_completed": "recall",
    "recall_timed_out": "recall",
    "compensation_started": "compensation",
    "compensated": "compensation",
    "migrated": "migration",
    "deadline_migrated": "migration",
    "error_raised": "error",
    "error_caught": "error",
    "error_handled": "error",
    "intent_failed": "error",
    "failed": "error",
    "projection_incomplete": "error",
    "retry_scheduled": "error",
}

# Entries of the journal a skill gets (``attach: [journal]``): the latest ones,
# as many as ``process.retrospective@1`` reads; the input of a skill is bounded.
SKILL_JOURNAL_LIMIT = 400


def journal_kind(decision_kind: str) -> str:
    return _JOURNAL_KIND.get(decision_kind, "transition")


def _reason(entry: Mapping[str, Any]) -> str:
    kind = str(entry.get("kind"))
    detail = entry.get("reason") or entry.get("cause")
    return f"{kind}: {detail}" if isinstance(detail, str) and detail else kind


def journal_entries(
    *,
    seq: int,
    at: Any,
    kind: str,
    source_ref: str,
    actor_id: Any,
    event_id: Any,
    given: Mapping[str, Any],
    calendars: Any,
    decisions: Sequence[Mapping[str, Any]],
    intents: Sequence[Mapping[str, Any]],
    settings_version: int | None = None,
    settings_schema_revision: int | None = None,
) -> list[dict[str, Any]]:
    """A journal record as entries of ProcessJournalEntryOut: input, decisions, intents.

    The input of a step that read the settings of the package names their
    version and schema revision (``settingsVersion``, ``settingsSchemaRevision``).
    """
    base = {"seq": seq, "at": at, "actorId": actor_id, "eventId": event_id}
    data: dict[str, Any] = {"input": given, "calendars": calendars}
    if settings_version is not None:
        data["settingsVersion"] = settings_version
        data["settingsSchemaRevision"] = settings_schema_revision
    entries: list[dict[str, Any]] = [
        {
            **base,
            "kind": "input",
            "element": None,
            "reason": f"{kind}: {source_ref}",
            "data": data,
        }
    ]
    for decision in decisions:
        data = {k: v for k, v in decision.items() if k not in ("kind", "element")}
        entries.append(
            {
                **base,
                "kind": journal_kind(str(decision.get("kind"))),
                "element": decision.get("element"),
                "reason": _reason(decision),
                "data": {"decision": decision.get("kind"), **data},
            }
        )
    for intent in intents:
        data = {k: v for k, v in intent.items() if k != "kind"}
        entries.append(
            {
                **base,
                "kind": "intent",
                "element": intent.get("element"),
                "reason": str(intent.get("kind")),
                "data": {"intent": intent.get("kind"), **data},
            }
        )
    return entries


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def skill_journal(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The journal as a skill reads it (``process.retrospective@1``): the latest entries.

    Without ``data``: the inputs and intents it holds are whole documents, and
    the entry names what was decided; times and ids are strings.
    """
    return [
        {
            "seq": int(entry["seq"]),
            "at": _text(entry.get("at")),
            "kind": str(entry["kind"]),
            "element": entry.get("element"),
            "reason": str(entry.get("reason") or ""),
            "actorId": _text(entry.get("actorId")),
            "eventId": _text(entry.get("eventId")),
        }
        for entry in list(entries)[-SKILL_JOURNAL_LIMIT:]
    ]


def with_attachments(
    body: Mapping[str, Any], journal: Callable[[], Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    """The inputs of an ``invoke_skill`` intent with what it asks attached (``attach``).

    ``journal`` — the instance's journal entries so far, the current step's
    included; read only when asked. The intent in the journal stays without
    them: they are the journal itself.
    """
    inputs = dict(body.get("input") or {})
    if "journal" in (body.get("attach") or ()):
        inputs["journal"] = skill_journal(journal())
    return inputs
