"""The check of a process definition (CP-ADR-0074 §1, §2, CP-ADR-0075, CP-ADR-0076).

A process is published as ``{key, spec}``, where ``spec`` is exactly the
``spec`` of a package file of kind ``Process`` (``$defs.processSpec`` of the
superproject catalog schema). Publishing runs two stages:

1. **Shape** — the catalog schema of the kind. The core holds the part of the
   catalog schema the kind needs (``process_spec.schema.json`` beside this
   module, the ``$defs`` reachable from ``processSpec``); a contract test
   keeps it equal to the pinned copy of the superproject's schema.
2. **Language** — what a schema cannot say:

   - every CEL expression compiles and type-checks in the profile
     (:mod:`control_plane.domain.cel_profile`) with the types of its place:
     ``data`` from ``spec.data``, ``event`` from the trigger's payload in the
     event catalog, ``step.result`` from the skill's output schema, the form
     of a human step or the outputs of a decision table, ``task`` from the
     task type — including the expressions of ``memory``, ``recall`` and
     ``context``; guards must be ``bool``, keys and assignees strings,
     deadlines times or durations;
   - writes into the data (``start.set``, ``correlate[].set``, ``set``,
     ``output.as``, ``export.as``) name declared fields and give values of
     their type; the inputs of a skill are its declared inputs, the required
     ones present;
   - element ids are unique, and stable against the previous version: an id
     does not change its kind, a removed element is named by a migration map;
   - reachability: no step after an unconditional ``complete`` or ``raise``;
     a stage whose entry can never become true is unreachable, a stage whose
     exit can never become true is a dead end; data fields that are read but
     never written;
   - references: decision tables, task types, skills, agents, the calendar,
     artifact types of the memory projection, compensated steps;
   - decision tables (:mod:`control_plane.domain.decision_table`);
   - ``governedBy``, the identity (required) and the owner (a warning without
     it).

Every finding has one shape, :class:`Problem` — ``{code, severity, path,
file, line, message, hint}``: ``path`` is a JSON pointer into the object
(``/spec/stages/0/steps/1/output/as/decision``); ``file`` and ``line`` are
filled when the object came from a package file and its caller can locate a
pointer in it (CP-ADR-0074 §10).

The catalog the check refers to (skills, task types, agents, calendars, the
versions already published) is given as plain data (:class:`Catalog`); the
application layer loads it. Pure functions over plain values; no I/O.
"""

import copy
import difflib
import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache, partial
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from control_plane.domain import cel_profile, decision_table
from control_plane.domain.cel_profile import Environment, ExpressionError, Program
from control_plane.domain.event_catalog import current_version, get_event_type, schema_for
from control_plane.domain.settings_refs import (
    NONE,
    SETTINGS,
    SettingsScope,
    compile_expression,
    loose_program,
    reads_settings,
    type_error,
)

JsonSchema = Mapping[str, Any]

CATALOG_KIND = "Process"
EXPRESSION_PROFILE = cel_profile.PROFILE
SCHEMA_FILE = Path(__file__).with_name("process_spec.schema.json")
DEFAULT_RETROSPECTIVE_SKILL = "process.retrospective@1"
# How deep a spec may nest and how long one string may be before hashing.
MAX_SPEC_DEPTH = 64
MAX_SPEC_STRING = 20_000
MAX_SHAPE_PROBLEMS = 50


# --- findings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """One finding: the same shape from every route and MCP tool (``ProcessProblemOut``)."""

    code: str
    severity: str
    path: str
    message: str
    hint: str | None = None
    file: str | None = None
    line: int | None = None

    @property
    def error(self) -> bool:
        return self.severity == "error"

    def out(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "hint": self.hint,
        }


def pointer(*parts: str | int) -> str:
    """A JSON pointer (RFC 6901) from its parts."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


# --- the catalog the definition refers to ----------------------------------------------


@dataclass(frozen=True)
class SkillEntry:
    """A registered skill version: its schemas and whether it can be called."""

    input_schema: JsonSchema | None
    output_schema: JsonSchema | None
    status: str = "active"


def package_skill(spec: Mapping[str, Any]) -> SkillEntry:
    """The entry of a ``Skill`` of a package: its contract, else its plain schemas."""
    contract = spec.get("contract")
    if isinstance(contract, dict):
        return SkillEntry(contract.get("inputs"), contract.get("outputs"))
    return SkillEntry(spec.get("inputSchema"), spec.get("outputSchema"))


@dataclass(frozen=True)
class Catalog:
    """What the tenant's catalog holds of the definition's references.

    ``skills`` by ``name@version``; ``task_types`` — the ``fieldSchema`` of the
    latest version by key; ``agents`` — keys of active agents; ``calendars``,
    ``artifact_types`` and ``processes`` — keys that exist (``None``: not
    checked); ``calendars_with_hours`` — keys whose latest version declares
    ``workingHours`` (``None``: not checked). ``previous`` — the latest
    published spec of this key, whose element ids the new version keeps
    stable; ``versions`` — published specs of the key by version, for the
    ``from`` side of migration maps.
    ``retired_calendars`` and ``retired_processes`` — named keys out of use
    (CP-ADR-0074, amendment Zh3, Zh2): a retired calendar is an error, a
    call of a retired process a warning.
    """

    skills: Mapping[str, SkillEntry] = field(default_factory=dict)
    task_types: Mapping[str, JsonSchema | None] = field(default_factory=dict)
    agents: frozenset[str] = frozenset()
    calendars: frozenset[str] = frozenset()
    calendars_with_hours: frozenset[str] | None = None
    artifact_types: frozenset[str] | None = None
    processes: frozenset[str] | None = None
    previous: Mapping[str, Any] | None = None
    versions: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    retired_calendars: frozenset[str] = frozenset()
    retired_processes: frozenset[str] = frozenset()
    # The settings of the process's package (CP-ADR-0081 §6): the type of ``settings``.
    settings: SettingsScope = NONE


@dataclass(frozen=True)
class References:
    """Keys a spec names, for the caller to load a :class:`Catalog`."""

    skills: frozenset[str]
    task_types: frozenset[str]
    agents: frozenset[str]
    calendars: frozenset[str]
    artifact_types: frozenset[str]
    processes: frozenset[str]
    migration_versions: frozenset[int]


# --- the schema of the kind -----------------------------------------------------------------


def process_schema_from_catalog(catalog_schema: Mapping[str, Any]) -> dict[str, Any]:
    """``$defs.processSpec`` of the catalog schema with every ``$defs`` entry it reaches.

    This is what ``process_spec.schema.json`` holds; a contract test rebuilds
    it from the pinned catalog schema and compares.
    """
    definitions = catalog_schema["$defs"]
    needed: set[str] = set()
    pending = ["processSpec"]
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(_refs(definitions[name]))
    return {
        "$schema": catalog_schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$comment": "Generated from $defs.processSpec of the package-sdk catalog schema"
        " (schema/v1/object.schema.json): process_schema_from_catalog().",
        "$ref": "#/$defs/processSpec",
        "$defs": {name: definitions[name] for name in sorted(needed)},
    }


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            yield ref.split("/")[2]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


@cache
def _shape_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


# --- normalization and the hash ----------------------------------------------------------


class SpecError(ValueError):
    """A spec that cannot be stored or hashed (not a JSON document of bounded size)."""

    def __init__(self, message: str, path: str) -> None:
        super().__init__(message)
        self.message = message
        self.path = path


def normalized_spec(spec: Any) -> dict[str, Any]:
    """The stored form of a spec: NFC strings, integral numbers as integers.

    ``1.0`` and ``1`` are one number in JSON; a fractional number stays as is
    (``json`` writes the shortest representation that reads back the same).
    Key order does not matter: the hash sorts keys.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object", "/spec")
    normalized = _normalize(spec, "/spec", 1)
    assert isinstance(normalized, dict)
    return normalized


def _normalize(value: Any, path: str, depth: int) -> Any:
    if depth > MAX_SPEC_DEPTH:
        raise SpecError(f"the spec nests deeper than {MAX_SPEC_DEPTH} levels", path)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpecError("numbers must be finite", path)
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        if len(value) > MAX_SPEC_STRING:
            raise SpecError(f"a string is longer than {MAX_SPEC_STRING} characters", path)
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw, item in value.items():
            if not isinstance(raw, str):
                raise SpecError("object keys must be strings", path)
            key = unicodedata.normalize("NFC", raw)
            if key in out:
                raise SpecError(f"keys collide after Unicode normalization: {key!r}", path)
            out[key] = _normalize(item, path + pointer(key), depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_normalize(item, path + pointer(i), depth + 1) for i, item in enumerate(value)]
    raise SpecError(f"{type(value).__name__} is not a JSON value", path)


def definition_hash(spec: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a normalized spec (sorted keys, no spaces)."""
    body = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# --- the result of a check -------------------------------------------------------------


@dataclass(frozen=True)
class Element:
    """A stage, step, milestone or decision table: what the memory projection is built from.

    ``parent`` is the nearest published element around it: a step inside a
    timer or a branch belongs to the stage or step that holds them.
    """

    id: str
    kind: str
    parent: str | None
    display_name: str | None
    governed_by: tuple[Mapping[str, Any], ...]
    path: str

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "parent": self.parent,
            "displayName": self.display_name,
            "governedBy": [dict(item) for item in self.governed_by],
        }


@dataclass(frozen=True)
class CheckedProcess:
    problems: tuple[Problem, ...]
    elements: tuple[Element, ...]
    governed_by: tuple[str, ...]
    # What the engine runs (process_engine): every compiled expression by its
    # JSON pointer, typed as the check typed it, and the parsed decision tables.
    programs: Mapping[str, Program] = field(default_factory=dict, compare=False)
    tables: Mapping[str, decision_table.Table] = field(default_factory=dict, compare=False)
    # Some expression reads ``settings``: the journal records the version it saw.
    reads_settings: bool = False

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.error]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if not p.error]


Locate = Callable[[str], int | None]


# --- regulations -----------------------------------------------------------------------

UNKNOWN_DOCUMENT = "governed_by_unknown_document"
GOVERNED_BY_UNCHECKED = "governed_by_unchecked"


def governed_references(spec: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(document, JSON pointer)`` of every ``governedBy`` item of a spec, in order.

    The process, its stages, steps, milestones, decision tables and their
    rules — wherever the catalog schema allows ``governedBy``. The data schema
    is not a place of the process and is not walked.
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = path + pointer(key)
                if path == "/spec" and key == "data":
                    continue
                if key == "governedBy" and isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, Mapping) and isinstance(item.get("document"), str):
                            found.append((item["document"], f"{here}/{index}/document"))
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}/{index}")

    walk(spec, "/spec")
    return found


def unknown_document_problems(spec: Mapping[str, Any], unknown: Sequence[str]) -> list[Problem]:
    """A warning at every reference to a document the knowledge base does not hold.

    The check of a package in the core resolves the documents through memory
    (CP-ADR-0076 §7); the reference does not refuse the process — the
    regulation may be loaded later — but ``regulation-drift`` cannot see it.
    """
    missing = set(unknown)
    return [
        Problem(
            UNKNOWN_DOCUMENT,
            "warning",
            path,
            f"{document!r} is not a document of the knowledge base",
            hint="load the regulation into the knowledge base or correct its natural key",
        )
        for document, path in governed_references(spec)
        if document in missing
    ]


# --- references ------------------------------------------------------------------------


def references(spec: Mapping[str, Any]) -> References:
    """The catalog keys a spec names (the shape need not be valid).

    The walk looks at every object, the data schema included: a key it picks
    up by mistake costs a lookup that finds nothing, never a finding.
    """
    skills: set[str] = set()
    task_types: set[str] = set()
    agents: set[str] = set()
    processes: set[str] = set()
    for node in _dicts(spec):
        call = node.get("call")
        if isinstance(call, dict):
            _add_str(skills, call.get("skill"))
            _add_str(agents, call.get("agent"))
            _add_str(processes, call.get("process"))
        for kind in ("human", "approve"):
            step = node.get(kind)
            if isinstance(step, dict):
                _add_str(task_types, step.get("taskType"))
        _add_str(agents, node.get("agent") if "agent" in node and len(node) == 1 else None)
    identity = spec.get("identity")
    if isinstance(identity, dict):
        _add_str(agents, identity.get("agent"))
    retrospective = spec.get("retrospective")
    if isinstance(retrospective, dict):
        _add_str(task_types, retrospective.get("taskType"))
        skills.add(str(retrospective.get("skill") or DEFAULT_RETROSPECTIVE_SKILL))
    calendars: set[str] = set()
    _add_str(calendars, spec.get("calendar"))
    for node in _dicts(spec):
        due = node.get("due")
        if isinstance(due, dict):
            _add_str(calendars, due.get("calendar"))
    artifacts: set[str] = set()
    memory = spec.get("memory")
    if isinstance(memory, dict) and isinstance(memory.get("documents"), dict):
        for item in memory["documents"].get("artifacts") or ():
            _add_str(artifacts, item)
    versions: set[int] = set()
    for migration in spec.get("migrations") or ():
        if isinstance(migration, dict) and isinstance(migration.get("from"), int):
            versions.add(migration["from"])
    return References(
        frozenset(skills),
        frozenset(task_types),
        frozenset(agents),
        frozenset(calendars),
        frozenset(artifacts),
        frozenset(processes),
        frozenset(versions),
    )


def _add_str(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value:
        target.add(value)


def _dicts(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


# --- the check ---------------------------------------------------------------------------


def check_process(
    key: str,
    spec: Mapping[str, Any],
    catalog: Catalog,
    *,
    file: str | None = None,
    locate: Locate | None = None,
) -> CheckedProcess:
    """Check a normalized spec: its shape by the catalog schema, then the language.

    ``file`` and ``locate`` (a JSON pointer → its line in the file) place the
    findings of an object that came from a package file.
    """
    checker = _Checker(key, spec, catalog)
    checker.run()
    problems = sorted(checker.problems, key=lambda p: (not p.error, p.path, p.code))
    if file is not None or locate is not None:
        problems = [_placed(p, file, locate) for p in problems]
    return CheckedProcess(
        tuple(problems),
        tuple(checker.elements),
        tuple(sorted(checker.documents)),
        programs={read.path: read.program for read in checker.reads},
        tables=dict(checker.built_tables),
        reads_settings=any(read.reads_settings for read in checker.reads),
    )


def _placed(problem: Problem, file: str | None, locate: Locate | None) -> Problem:
    line = None
    if locate is not None:
        path = problem.path
        while line is None:
            line = locate(path)
            if not path:
                break
            path = path.rsplit("/", 1)[0]
    return Problem(
        problem.code, problem.severity, problem.path, problem.message, problem.hint, file, line
    )


def shape_problems(spec: Mapping[str, Any]) -> list[Problem]:
    """Findings of the catalog schema of the kind (``schema_violation``), deepest cause first.

    ``spec.workspaceId`` is checked apart: in the catalog it is an install
    variable ``${…}``, the installer substitutes it, and the core receives the
    workspace id.
    """
    problems: list[Problem] = []
    body = dict(spec)
    workspace = body.pop("workspaceId", None)
    if workspace is not None:
        problems.extend(_workspace_problems(workspace))
    errors = sorted(_shape_validator().iter_errors(body), key=lambda e: list(e.absolute_path))
    for error in errors[:MAX_SHAPE_PROBLEMS]:
        cause = _deepest(error)
        problems.append(
            Problem(
                "schema_violation",
                "error",
                pointer("spec", *cause.absolute_path),
                cause.message if len(cause.message) <= 500 else cause.message[:497] + "...",
                hint=_shape_hint(cause),
            )
        )
    return problems


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    if not error.context:
        return error
    best = jsonschema.exceptions.best_match(error.context)
    return _deepest(best) if len(best.absolute_path) >= len(error.absolute_path) else error


def _shape_hint(error: jsonschema.ValidationError) -> str | None:
    if error.validator == "additionalProperties" and isinstance(error.schema, dict):
        known = sorted((error.schema.get("properties") or {}).keys())
        return f"allowed: {', '.join(known)}" if known else None
    if error.validator == "oneOf":
        return "exactly one of the alternatives must match"
    return None


_INSTALL_VARIABLE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


def _workspace_problems(workspace: Any) -> list[Problem]:
    path = "/spec/workspaceId"
    if isinstance(workspace, str) and _INSTALL_VARIABLE.match(workspace):
        return [
            Problem(
                "unresolved_install_variable",
                "error",
                path,
                f"{workspace} is an install variable the core does not substitute",
                hint="the package installer replaces ${…} with the workspace id",
            )
        ]
    try:
        uuid.UUID(str(workspace))
    except ValueError:
        return [Problem("schema_violation", "error", path, "workspaceId must be a workspace id")]
    return []


# Types of the result of a step (step.result), by the kind of step.
RECALL_RESULT: JsonSchema = {
    "type": "object",
    "properties": {
        "nodes": {"type": "array"},
        "edges": {"type": "array"},
        "truncated": {"type": "boolean"},
    },
}
_TABLE_OUTPUT_SCHEMAS: Mapping[str, JsonSchema] = {
    "string": {"type": "string"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string"},
    "duration": {"type": "string", "format": "duration"},
    "object": {"type": "object"},
    "array": {"type": "array"},
}
_ERROR_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "status": {"type": ["integer", "null"]},
        "detail": {"type": ["string", "null"]},
    },
}

# What an expression must give, by place.
BOOL = "bool"
STRING = "string"
KEY = "key"
TIME = "time"
LIST = "list"
COUNT = "count"
_EXPECTED: Mapping[str, tuple[str, ...]] = {
    BOOL: ("BOOL",),
    STRING: ("STRING",),
    KEY: ("STRING", "INT", "UINT"),
    TIME: ("TIMESTAMP", "DURATION"),
    LIST: ("LIST",),
    COUNT: ("INT", "UINT"),
}
_EXPECTED_WORDS = {
    BOOL: "bool",
    STRING: "string",
    KEY: "string or int",
    TIME: "timestamp or duration",
    LIST: "list",
    COUNT: "int",
}

# What a ``celMap`` writes into: (unknown key, type mismatch, owner in the message).
_SKILL_INPUT = ("unknown_skill_input", "skill_input_type_mismatch", "the skill input")
_MAP_TARGETS = {
    "data": ("unknown_data_field", "data_type_mismatch", "data"),
    "input": _SKILL_INPUT,
    # human.customFields (CP-ADR-0074 §7, amendment 2026-10-01).
    "customFields": (
        "unknown_custom_field",
        "custom_field_type_mismatch",
        "the fieldSchema of the task type",
    ),
}


@dataclass(frozen=True)
class _Place:
    """What the variables of an expression are at its place in the definition."""

    event_payload: JsonSchema | None = None
    step_result: JsonSchema | None = None
    custom_fields: JsonSchema | None = None
    errors: tuple[str, ...] = ()  # names bound by enclosing catch clauses
    compensated: JsonSchema | None = None  # the step an enclosing onCompensate compensates

    def but(self, **changes: Any) -> "_Place":
        return _Place(**{**self.__dict__, **changes})


@dataclass
class _Read:
    path: str
    program: Program
    # A catch clause names its error ``settings``: what the program reads is the error.
    shadowed: bool = False

    @property
    def reads_settings(self) -> bool:
        return not self.shadowed and reads_settings(self.program)


_TERMINAL_STEPS = ("complete", "raise")
# The units of a due counted by a calendar (CP-ADR-0078 §1).
_WORKING_UNITS = ("workdays", "workhours")
_STEP_KINDS = (
    "human",
    "approve",
    "call",
    "decide",
    "recall",
    "remember",
    "listen",
    "wait",
    "set",
    "raise",
    "compensate",
    "fork",
    "try",
    "do",
    "suspend",
    "resume",
    "complete",
)


def step_kind(step: Mapping[str, Any]) -> str:
    return next((kind for kind in _STEP_KINDS if kind in step), "step")


class _Checker:
    def __init__(self, key: str, spec: Mapping[str, Any], catalog: Catalog) -> None:
        self.key = key
        self.spec = spec
        self.catalog = catalog
        self.problems: list[Problem] = []
        self.elements: list[Element] = []
        self.documents: set[str] = set()
        self.ids: dict[str, tuple[str, str]] = {}  # id -> (kind, path)
        self.hidden: dict[str, str | None] = {}  # unpublished id -> its published parent
        self.reads: list[_Read] = []
        self.built_tables: dict[str, decision_table.Table] = {}
        self.writes: set[str] = set()
        self.guards: list[tuple[str, str, Program]] = []  # (role, path, program)
        self.compensable: set[str] = set()
        self.compensations: list[tuple[str, Any]] = []
        self.decides: list[tuple[str, Mapping[str, Any]]] = []
        self.tables: dict[str, Mapping[str, Any]] = {}
        self.stage_ids: list[str] = []
        self.milestone_ids: list[str] = []
        self.data: JsonSchema = {"type": "object"}

    # --- plumbing --------------------------------------------------------------------

    def problem(
        self, code: str, path: str, message: str, *, hint: str | None = None, warning: bool = False
    ) -> None:
        self.problems.append(
            Problem(code, "warning" if warning else "error", path, message, hint=hint)
        )

    def env(self, place: _Place, settings: JsonSchema | None = None) -> Environment | None:
        bindings: dict[str, JsonSchema | None] = {"milestone": self._milestone_schema()}
        if place.compensated is not None:
            bindings["compensated"] = place.compensated
        for name in place.errors:
            bindings[name] = _ERROR_SCHEMA
        try:
            return cel_profile.environment(
                data=self.data,
                event_payload=place.event_payload,
                step_result=place.step_result,
                custom_fields=place.custom_fields,
                stages=tuple(self.stage_ids),
                calendar=self.spec.get("calendar"),
                bindings=bindings,
                # A catch clause that names its error ``settings`` hides the settings.
                settings=None if SETTINGS in place.errors else settings,
            )
        except (ValueError, TypeError, KeyError) as exc:
            self.problem(
                "invalid_data_schema",
                "/spec/data",
                f"the types of the expressions cannot be built from the schemas: {exc}",
            )
            return None

    def _milestone_schema(self) -> JsonSchema:
        return {"type": "object", "additionalProperties": {"type": "boolean"}}

    def expr(
        self, path: str, text: Any, place: _Place, expect: str | None = None
    ) -> Program | None:
        if not isinstance(text, str):
            return None
        environment = self.env(place)
        if environment is None:
            return None
        shadowed = SETTINGS in place.errors
        try:
            if shadowed:
                program = environment.compile(text, path=path)
            else:
                program = compile_expression(
                    self.catalog.settings,
                    lambda settings: self.env(place, settings),
                    text,
                    path=path,
                )
        except ExpressionError as exc:
            self.problems.append(Problem(exc.code, "error", path, exc.message, hint=exc.hint))
            return None
        self.reads.append(_Read(path, program, shadowed))
        if expect is not None and not _gives(program.output_type, expect):
            if self._settings_fault(place, text, path, lambda out: _gives(out, expect)):
                return program
            self.problem(
                "expression_type_error",
                path,
                f"the expression gives {_type_word(program.output_type)},"
                f" {_EXPECTED_WORDS[expect]} is expected here",
            )
        return program

    def _settings_fault(
        self, place: _Place, text: str, path: str, fits: Callable[[str], bool]
    ) -> bool:
        """Whether the type of a read of ``settings`` is why the expression misfits its place.

        So it is when the expression with ``settings`` untyped would fit:
        ``settings_ref_type`` is then the finding (CP-ADR-0081 §6).
        """
        if self.catalog.settings.schema is None or SETTINGS in place.errors:
            return False
        loose = loose_program(lambda settings: self.env(place, settings), text, path=path)
        if loose is None or not reads_settings(loose) or not fits(loose.output_type):
            return False
        error = type_error(loose.reads, path=path)
        self.problems.append(Problem(error.code, "error", path, error.message))
        return True

    def expr_map(
        self,
        path: str,
        mapping: Any,
        place: _Place,
        *,
        target: JsonSchema | None,
        what: str,
    ) -> None:
        """A ``celMap``: keys are paths in ``target`` (the data, a skill input,
        the fields of a task type), values CEL."""
        if not isinstance(mapping, dict):
            return
        for name, text in mapping.items():
            here = f"{path}{pointer(name)}"
            program = self.expr(here, text, place)
            if target is None:
                continue
            if what == "data":
                self.writes.add(name)
            resolved = _resolve(target, name.split("."))
            if isinstance(resolved, _Unknown):
                code, _, owner = _MAP_TARGETS.get(what, _SKILL_INPUT)
                prefix = ".".join(name.split(".")[: resolved.depth])
                self.problem(
                    code,
                    here,
                    f"{owner}{'.' + prefix if prefix else ''} has no field {resolved.name}",
                    hint=_did_you_mean(resolved.name, resolved.known),
                )
                continue
            if (
                program is not None
                and resolved is not None
                and not _fits(program.output_type, resolved)
            ):
                if isinstance(text, str) and self._settings_fault(
                    place, text, here, partial(_fits, schema=resolved)
                ):
                    continue
                code = _MAP_TARGETS.get(what, _SKILL_INPUT)[1]
                self.problem(
                    code,
                    here,
                    f"{name} is {_schema_word(resolved)},"
                    f" the expression gives {_type_word(program.output_type)}",
                )

    def element(
        self,
        element_id: Any,
        kind: str,
        path: str,
        parent: str | None,
        node: Mapping[str, Any],
        *,
        published: bool = True,
    ) -> None:
        if not isinstance(element_id, str):
            return
        seen = self.ids.get(element_id)
        if seen is not None:
            self.problem(
                "duplicate_element_id",
                path + "/id",
                f"element id {element_id!r} is already used by a {seen[0]}",
                hint=f"ids are unique in a process; the other one is at {seen[1]}",
            )
            return
        self.ids[element_id] = (kind, path)
        # A timer or a branch is not published: what it holds is published
        # under the nearest published element around it (or the process).
        while parent is not None and parent in self.hidden:
            parent = self.hidden[parent]
        if not published:
            self.hidden[element_id] = parent
            return
        governed = tuple(item for item in node.get("governedBy") or () if isinstance(item, dict))
        self.elements.append(
            Element(element_id, kind, parent, node.get("displayName"), governed, path)
        )

    # --- the walk --------------------------------------------------------------------

    def run(self) -> None:
        self.problems.extend(shape_problems(self.spec))
        if self.problems:
            return
        spec = self.spec
        self.data = self._data_schema()
        self.stage_ids = [s["id"] for s in spec["stages"]]
        self.milestone_ids = [m["id"] for s in spec["stages"] for m in s.get("milestones") or ()]
        self.tables = {t["id"]: t for t in spec.get("decisions") or ()}
        self._identity_and_owner()
        self._references_of_the_process()
        base = _Place()

        start = spec["start"]
        start_place = base.but(event_payload=self._trigger(start["on"], "/spec/start/on", base))
        self.expr("/spec/start/key", start["key"], start_place, KEY)
        self.expr_map(
            "/spec/start/set", start.get("set"), start_place, target=self.data, what="data"
        )
        for index, item in enumerate(spec.get("correlate") or ()):
            path = pointer("spec", "correlate", index)
            place = base.but(event_payload=self._trigger(item["on"], path + "/on", base))
            self.expr(path + "/key", item["key"], place, KEY)
            self.expr_map(path + "/set", item.get("set"), place, target=self.data, what="data")
            self.blocks(path + "/do", item.get("do"), place, None)
        for index, item in enumerate(spec.get("onEvent") or ()):
            path = pointer("spec", "onEvent", index)
            place = base.but(event_payload=self._trigger(item["on"], path + "/on", base))
            self.blocks(path + "/do", item["do"], place, None)
        self.sla_due("/spec/due", spec.get("due"), base)
        self.timers("/spec/timers", spec.get("timers"), base, None)
        for index, stage in enumerate(spec["stages"]):
            self.stage(pointer("spec", "stages", index), stage, base)
        for index, table in enumerate(spec.get("decisions") or ()):
            self.table(pointer("spec", "decisions", index), table, base)
        self.memory(base)
        self.retrospective(base)
        self.governed("/spec/governedBy", spec.get("governedBy"))
        self._decide_references()
        self._compensations()
        self._stability()
        self._reachability()

    def _data_schema(self) -> JsonSchema:
        data = self.spec["data"]
        try:
            Draft202012Validator.check_schema(data)
        except jsonschema.SchemaError as exc:
            self.problem(
                "invalid_data_schema",
                pointer("spec", "data", *exc.path),
                f"spec.data is not a JSON Schema: {exc.message}",
            )
            return {"type": "object"}
        for path, ref in _external_refs(data, "/spec/data"):
            self.problem(
                "unresolved_data_ref",
                path,
                f"$ref {ref!r} points outside the schema",
                hint="the package tool inlines {$ref: <file>} before publishing",
            )
        if data.get("type") != "object":
            self.problem(
                "invalid_data_schema",
                "/spec/data",
                "the data of an instance is an object: spec.data needs type: object",
            )
            return {"type": "object"}
        schema: JsonSchema = data
        return schema

    def _identity_and_owner(self) -> None:
        identity = self.spec.get("identity")
        if identity is None:
            self.problem(
                "process_identity_required",
                "/spec/identity",
                "a process acts as an agent of the registry: spec.identity is required",
                hint="identity: {agent: <key of an agent of kind service or agent>}",
            )
        elif identity["agent"] not in self.catalog.agents:
            self.problem(
                "unknown_agent",
                "/spec/identity/agent",
                f"no active agent {identity['agent']!r} in the registry",
            )
        owner = self.spec.get("owner")
        if owner is None:
            self.problem(
                "process_owner_missing",
                "/spec/owner",
                "the process has no owner: tasks about the process itself"
                " (regulation drift, failed instances) have nobody to go to",
                hint="owner: [{role: <slug>}] or [{principal: <id>}]",
                warning=True,
            )
        else:
            self.assign_chain("/spec/owner", owner, _Place())

    def _references_of_the_process(self) -> None:
        calendar = self.spec.get("calendar")
        if calendar is not None and calendar not in self.catalog.calendars:
            self.problem(
                "unknown_calendar",
                "/spec/calendar",
                f"no calendar {calendar!r} is published",
                hint="publish the calendar first (POST /calendars) or install its package",
            )
        elif calendar is not None and calendar in self.catalog.retired_calendars:
            self.problem(
                "calendar_retired",
                "/spec/calendar",
                f"calendar {calendar!r} is retired: a new version may not use it again",
                hint="name a calendar in use, or publish a new version of this one first",
            )

    def _trigger(self, trigger: Mapping[str, Any], path: str, place: _Place) -> JsonSchema | None:
        payload: JsonSchema | None = None
        event_type = trigger.get("event")
        if event_type is not None:
            try:
                get_event_type(event_type)
            except ValueError:
                self.problem(
                    "unknown_event_type",
                    path + "/event",
                    f"{event_type!r} is not in the event catalog",
                    hint="observation: <type> for facts of connectors",
                )
            else:
                payload = schema_for(event_type, current_version(event_type))
        self.expr(path + "/where", trigger.get("where"), place.but(event_payload=payload), BOOL)
        return payload

    def stage(self, path: str, stage: Mapping[str, Any], base: _Place) -> None:
        stage_id = stage["id"]
        self.element(stage_id, "stage", path, None, stage)
        self.governed(path + "/governedBy", stage.get("governedBy"))
        entry = self.expr(path + "/entry", stage.get("entry"), base, BOOL)
        if entry is not None:
            self.guards.append(("entry", path + "/entry", entry))
        exit_ = self.expr(path + "/exit", stage.get("exit"), base, BOOL)
        if exit_ is not None:
            self.guards.append(("exit", path + "/exit", exit_))
        for index, milestone in enumerate(stage.get("milestones") or ()):
            here = f"{path}/milestones/{index}"
            self.element(milestone["id"], "milestone", here, stage_id, milestone)
            program = self.expr(here + "/when", milestone["when"], base, BOOL)
            if program is not None:
                self.guards.append(("milestone", here + "/when", program))
        self.timers(path + "/timers", stage.get("timers"), base, stage_id)
        self.blocks(path + "/steps", stage["steps"], base, stage_id)
        self.blocks(path + "/discretionary", stage.get("discretionary"), base, stage_id)

    def timers(self, path: str, timers: Any, place: _Place, parent: str | None) -> None:
        for index, timer in enumerate(timers or ()):
            here = f"{path}/{index}"
            self.element(timer["id"], "timer", here, parent, timer, published=False)
            self.due(here + "/at", timer["at"], place)
            self.blocks(here + "/do", timer["do"], place, timer["id"])

    def due(self, path: str, value: Any, place: _Place) -> None:
        if isinstance(value, dict):
            self.expr(path + "/at", value.get("at"), place, TIME)

    def sla_due(self, path: str, value: Any, place: _Place) -> None:
        """A ``processDue`` (CP-ADR-0078 §1): working units need a calendar that has them."""
        self.due(path, value, place)
        if not isinstance(value, dict):
            return
        spans = [(path, value)]
        if isinstance(value.get("warnBefore"), dict):
            spans.append((path + "/warnBefore", value["warnBefore"]))
        units = [
            (f"{here}/{unit}", unit)
            for here, span in spans
            for unit in _WORKING_UNITS
            if unit in span
        ]
        # An amount may be an expression: an integer computed at the step's entry
        # (CP-ADR-0081, amendment 2026-10-03 G1); settings.* are checked as anywhere else.
        for here, span in spans:
            for unit in _WORKING_UNITS:
                amount = span.get(unit)
                if isinstance(amount, dict):
                    self.expr(f"{here}/{unit}/expr", amount.get("expr"), place, COUNT)
        if not units:
            return
        key = value.get("calendar")
        if key is None:
            key = self.spec.get("calendar")
            if key is None:
                self.problem(
                    "sla_calendar_missing",
                    units[0][0],
                    f"{units[0][1]} are counted by a calendar, and the process has none",
                    hint=f"set spec.calendar or {path}/calendar",
                )
                return
        elif key not in self.catalog.calendars:
            self.problem(
                "unknown_calendar",
                path + "/calendar",
                f"no calendar {key!r} is published",
                hint="publish the calendar first (POST /calendars) or install its package",
            )
            return
        hours = self.catalog.calendars_with_hours
        if key not in self.catalog.calendars or hours is None or key in hours:
            return
        for here, unit in units:
            if unit == "workhours":
                self.problem(
                    "sla_calendar_without_hours",
                    here,
                    f"workhours are counted by the working hours of a calendar,"
                    f" and calendar {key!r} declares none",
                    hint="declare workingHours in the calendar, or count the due in workdays",
                )
                return

    def blocks(self, path: str, steps: Any, place: _Place, parent: str | None) -> None:
        ended_by: str | None = None
        for index, step in enumerate(steps or ()):
            here = f"{path}/{index}"
            if ended_by is not None:
                self.problem(
                    "unreachable_step",
                    here,
                    f"step {step['id']!r} comes after {ended_by}, which always ends the flow",
                    hint="guard the earlier step with when, or move this one before it",
                )
            self.step(here, step, place, parent)
            kind = step_kind(step)
            if ended_by is None and kind in _TERMINAL_STEPS and step.get("when") is None:
                ended_by = f"{kind} step {step['id']!r}"

    def step(self, path: str, step: Mapping[str, Any], place: _Place, parent: str | None) -> None:
        step_id = step["id"]
        kind = step_kind(step)
        self.element(step_id, "step", path, parent, step)
        self.governed(path + "/governedBy", step.get("governedBy"))
        self.expr(path + "/when", step.get("when"), place, BOOL)
        if isinstance(step.get("input"), dict):
            self.expr(path + "/input/from", step["input"].get("from"), place)
        body = step[kind]
        here = f"{path}/{kind}"
        result: JsonSchema | None = None
        custom_fields: JsonSchema | None = None
        if kind == "human":
            custom_fields = self.task_type(here + "/taskType", body["taskType"])
            result = self.form(here + "/form", body.get("form")) or custom_fields
            self.expr(here + "/title", body.get("title"), place, STRING)
            # The fields filled from the case: checked against the type's fieldSchema.
            self.expr_map(
                here + "/customFields",
                body.get("customFields"),
                place,
                target=custom_fields,
                what="customFields",
            )
            self.assign_chain(here + "/assign", body["assign"], place)
            self.sla_due(here + "/due", body.get("due"), place)
            self.escalations(here + "/escalations", body.get("escalations"), place)
            self.context(here + "/context", body.get("context"), place)
        elif kind == "approve":
            if body.get("taskType") is not None:
                custom_fields = self.task_type(here + "/taskType", body["taskType"])
            self.assign_chain(here + "/approvers", body["approvers"], place)
            self.expr(here + "/separationOfDuties", body.get("separationOfDuties"), place, LIST)
            self.sla_due(here + "/due", body.get("due"), place)
            self.escalations(here + "/escalations", body.get("escalations"), place)
            self.context(here + "/context", body.get("context"), place)
        elif kind == "call":
            result = self.call(here, body, step, place)
        elif kind == "decide":
            self.decides.append((here, body))
            result = self._decide_result(body)
            self.expr_map(here + "/input", body.get("input"), place, target=None, what="table")
        elif kind == "recall":
            result = RECALL_RESULT
            self.sla_due(here + "/due", body.get("due"), place)
            self.anchors(here + "/anchors", body["anchors"], place)
            self.expr(here + "/query", body.get("query"), place, STRING)
            self.where(here + "/where", body.get("where"), place)
            self.blocks(here + "/onTimeout", body.get("onTimeout"), place, step_id)
        elif kind == "remember":
            self.remember(here, body, place)
        elif kind == "listen":
            for index, option in enumerate(body["any"]):
                option_path = f"{here}/any/{index}"
                payload = self._trigger(option["on"], option_path + "/on", place)
                self.blocks(
                    option_path + "/do", option.get("do"), place.but(event_payload=payload), step_id
                )
            self.due(here + "/timeout", body.get("timeout"), place)
            self.sla_due(here + "/due", body.get("due"), place)
            self.blocks(here + "/onTimeout", body.get("onTimeout"), place, step_id)
        elif kind == "wait":
            self.due(here, body, place)
        elif kind == "set":
            self.expr_map(here, body, place, target=self.data, what="data")
        elif kind == "raise":
            self.expr(here + "/detail", body.get("detail"), place, STRING)
        elif kind == "compensate":
            self.compensations.append((here, body))
        elif kind == "fork":
            for index, branch in enumerate(body["branches"]):
                branch_path = f"{here}/branches/{index}"
                self.element(branch["id"], "branch", branch_path, step_id, branch, published=False)
                self.blocks(branch_path + "/do", branch["do"], place, branch["id"])
        elif kind == "try":
            self.blocks(here + "/do", body["do"], place, step_id)
            for index, clause in enumerate(body.get("catch") or ()):
                clause_path = f"{here}/catch/{index}"
                inner = place
                name = clause.get("as")
                if name is not None:
                    if (
                        name in cel_profile.VARIABLES
                        or name == "milestone"
                        or (name == "compensated" and place.compensated is not None)
                    ):
                        self.problem(
                            "invalid_error_binding",
                            clause_path + "/as",
                            f"{name!r} is a variable of the profile; name the error otherwise",
                        )
                    else:
                        inner = place.but(errors=(*place.errors, name))
                self.blocks(clause_path + "/do", clause["do"], inner, step_id)
        elif kind == "do":
            self.blocks(here, body, place, step_id)
        elif kind in ("suspend", "resume"):
            self.expr(here + "/reason", body.get("reason"), place, STRING)
        after = place.but(step_result=result, custom_fields=custom_fields)
        for side in ("output", "export"):
            if isinstance(step.get(side), dict):
                self.expr_map(
                    f"{path}/{side}/as", step[side].get("as"), after, target=self.data, what="data"
                )
        if step.get("onCompensate"):
            # step is the current step of the block; compensated — the step compensated.
            self.compensable.add(step_id)
            self.blocks(
                path + "/onCompensate",
                step["onCompensate"],
                place.but(compensated=cel_profile.step_schema(result)),
                step_id,
            )

    def call(
        self, path: str, body: Mapping[str, Any], step: Mapping[str, Any], place: _Place
    ) -> JsonSchema | None:
        self.due(path + "/timeout", body.get("timeout"), place)
        self.sla_due(path + "/due", body.get("due"), place)
        self.context(path + "/context", body.get("context"), place)
        skill_ref = body.get("skill")
        if skill_ref is None:
            agent = body.get("agent")
            if agent is not None and agent not in self.catalog.agents:
                self.problem("unknown_agent", path + "/agent", f"no active agent {agent!r}")
            process = body.get("process")
            if (
                process is not None
                and process != self.key
                and self.catalog.processes is not None
                and process not in self.catalog.processes
            ):
                self.problem(
                    "unknown_process",
                    path + "/process",
                    f"no process {process!r} is published yet",
                    hint="publish it before a version that calls it is started",
                    warning=True,
                )
            elif process is not None and process in self.catalog.retired_processes:
                self.problem(
                    "process_retired",
                    path + "/process",
                    f"process {process!r} is retired: calling it fails at run time",
                    hint="call a process in use, or catch process_retired with try",
                    warning=True,
                )
            self.expr_map(path + "/input", body.get("input"), place, target=None, what="input")
            return None
        skill = self.catalog.skills.get(skill_ref)
        if skill is None or skill.status == "disabled":
            state = "is disabled" if skill is not None else "is not registered"
            self.problem("unknown_skill", path + "/skill", f"skill {skill_ref!r} {state}")
            self.expr_map(path + "/input", body.get("input"), place, target=None, what="input")
            return None
        target = skill.input_schema if _object_schema(skill.input_schema) else None
        self.expr_map(path + "/input", body.get("input"), place, target=target, what="input")
        given = {name.split(".")[0] for name in body.get("input") or {}}
        from_input = isinstance(step.get("input"), dict) and step["input"].get("from")
        if target is not None and not from_input:
            missing = [n for n in target.get("required") or () if n not in given]
            if missing:
                self.problem(
                    "skill_input_missing",
                    path + "/input",
                    f"skill {skill_ref!r} requires {', '.join(missing)}",
                )
        return skill.output_schema if _object_schema(skill.output_schema) else None

    def task_type(self, path: str, key: str) -> JsonSchema | None:
        if key not in self.catalog.task_types:
            self.problem("unknown_task_type", path, f"task type {key!r} is not registered")
            return None
        schema = self.catalog.task_types[key]
        return schema if _object_schema(schema) else None

    def form(self, path: str, form: Any) -> JsonSchema | None:
        if not isinstance(form, dict):
            return None
        schema = form["schema"]
        try:
            Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            self.problem(
                "invalid_form_schema",
                path + "/schema" + pointer(*exc.path),
                f"the form is not a JSON Schema: {exc.message}",
            )
            return None
        return schema if _object_schema(schema) else None

    def assign_chain(self, path: str, chain: Any, place: _Place) -> None:
        for index, assignee in enumerate(chain or ()):
            here = f"{path}/{index}"
            agent = assignee.get("agent")
            if agent is not None and agent not in self.catalog.agents:
                self.problem("unknown_agent", here + "/agent", f"no active agent {agent!r}")
            self.expr(here + "/expr", assignee.get("expr"), place, STRING)

    def escalations(self, path: str, escalations: Any, place: _Place) -> None:
        for index, escalation in enumerate(escalations or ()):
            here = f"{path}/{index}"
            self.due(here + "/after", escalation["after"], place)
            action = escalation["action"]
            if action == "reassign" and not escalation.get("to"):
                self.problem("invalid_escalation", here, "reassign needs to: <assign chain>")
            if action == "raise" and not escalation.get("error"):
                self.problem("invalid_escalation", here, "raise needs error: {type}")
            self.assign_chain(here + "/to", escalation.get("to"), place)
            if isinstance(escalation.get("error"), dict):
                self.expr(here + "/error/detail", escalation["error"].get("detail"), place, STRING)

    def context(self, path: str, context: Any, place: _Place) -> None:
        if isinstance(context, dict):
            self.anchors(path + "/anchors", context["anchors"], place)

    def anchors(self, path: str, anchors: Any, place: _Place) -> None:
        for index, anchor in enumerate(anchors or ()):
            here = f"{path}/{index}"
            if anchor.get("case") is True and self.spec.get("memory") is None:
                self.problem(
                    "memory_case_undeclared",
                    here + "/case",
                    "an anchor on the case node needs spec.memory: the process projects no case",
                )
            self.expr(here + "/key", anchor.get("key"), place, KEY)

    def where(self, path: str, conditions: Any, place: _Place) -> None:
        """``recall.where``: each value is CEL (a list of CEL for ``in``); literals stay."""
        for index, condition in enumerate(conditions or ()):
            here = f"{path}/{index}/value"
            value = condition.get("value")
            if isinstance(value, list):
                for item, text in enumerate(value):
                    self.expr(f"{here}/{item}", text, place)
                continue
            op = condition["op"]
            expect = {"in": LIST, "prefix": STRING, "exists": BOOL}.get(op)
            self.expr(here, value, place, expect)

    def remember(self, path: str, body: Mapping[str, Any], place: _Place) -> None:
        entity = body.get("entity")
        if isinstance(entity, dict):
            self.expr(path + "/entity/key", entity["key"], place, KEY)
            self.expr(path + "/entity/name", entity.get("name"), place, STRING)
            self.expr(path + "/entity/text", entity.get("text"), place, STRING)
            for index, link in enumerate(entity.get("links") or ()):
                self.expr(f"{path}/entity/links/{index}/key", link["key"], place, KEY)
            if self.spec.get("memory") is None:
                self.problem(
                    "memory_case_undeclared",
                    path,
                    "remember writes about the case: spec.memory declares none",
                    warning=True,
                )
        self.expr_map(path + "/facts", body.get("facts"), place, target=None, what="facts")

    def memory(self, place: _Place) -> None:
        memory = self.spec.get("memory")
        if not isinstance(memory, dict):
            return
        case = memory["case"]
        self.expr("/spec/memory/case/key", case["key"], place, KEY)
        self.expr("/spec/memory/case/title", case.get("title"), place, STRING)
        self.expr_map("/spec/memory/facts", memory.get("facts"), place, target=None, what="facts")
        for index, entity in enumerate(memory.get("entities") or ()):
            here = f"/spec/memory/entities/{index}"
            self.expr(here + "/key", entity["key"], place, LIST if entity.get("many") else KEY)
            self.expr(here + "/name", entity.get("name"), place)
            self.expr(here + "/when", entity.get("when"), place, BOOL)
        documents = memory.get("documents") or {}
        known = self.catalog.artifact_types
        for index, artifact in enumerate(documents.get("artifacts") or ()):
            if known is not None and artifact not in known:
                self.problem(
                    "unknown_artifact_type",
                    f"/spec/memory/documents/artifacts/{index}",
                    f"artifact type {artifact!r} is not registered",
                    warning=True,
                )

    def retrospective(self, place: _Place) -> None:
        retrospective = self.spec.get("retrospective")
        if not isinstance(retrospective, dict):
            return
        path = "/spec/retrospective"
        self.task_type(path + "/taskType", retrospective["taskType"])
        self.assign_chain(path + "/assign", retrospective["assign"], place)
        self.expr(path + "/when", retrospective.get("when"), place, BOOL)
        skill_ref = retrospective.get("skill")
        if (skill_ref or DEFAULT_RETROSPECTIVE_SKILL) not in self.catalog.skills:
            self.problem(
                "unknown_skill",
                path + "/skill",
                f"skill {skill_ref or DEFAULT_RETROSPECTIVE_SKILL!r} is not registered",
                hint=None if skill_ref else "the package process-knowledge provides it",
                warning=skill_ref is None,
            )

    def governed(self, path: str, items: Any) -> None:
        seen: set[tuple[str, str | None]] = set()
        for index, item in enumerate(items or ()):
            here = f"{path}/{index}"
            document = item["document"]
            if not document.strip() or document != document.strip():
                self.problem(
                    "invalid_governed_by",
                    here + "/document",
                    "the document is a natural key of the knowledge base, without outer spaces",
                )
                continue
            reference = (document, item.get("section"))
            if reference in seen:
                self.problem(
                    "duplicate_governed_by",
                    here,
                    f"{document}{' §' + reference[1] if reference[1] else ''} is listed twice",
                    warning=True,
                )
            seen.add(reference)
            self.documents.add(document)

    # --- decision tables ----------------------------------------------------------------

    def table(self, path: str, table: Mapping[str, Any], place: _Place) -> None:
        self.element(table["id"], "decision", path, None, table)
        self.governed(path + "/governedBy", table.get("governedBy"))
        types: list[str] = []
        for index, item in enumerate(table["inputs"]):
            program = self.expr(f"{path}/inputs/{index}/expr", item["expr"], place)
            types.append(item.get("type") or _table_type(program, table, item["id"]))
        for index, rule in enumerate(table["rules"]):
            self.governed(f"{path}/rules/{index}/governedBy", rule.get("governedBy"))
        parsed, findings = decision_table.build(table, types)
        if not any(f.severity == "error" for f in findings):
            self.built_tables[table["id"]] = parsed
            findings.extend(decision_table.check(parsed))
        for found in findings:
            here = path
            if found.rule is not None:
                here = f"{path}/rules/{found.rule}"
                if found.input is not None:
                    here += "/when" + pointer(found.input)
                elif found.output is not None:
                    here += "/then"
                    if found.code != "table_output_missing":
                        here += pointer(found.output)
            self.problem(
                found.code, here, found.message, hint=found.hint, warning=found.severity != "error"
            )

    def _decide_result(self, body: Mapping[str, Any]) -> JsonSchema | None:
        table = self.tables.get(body["table"])
        if table is None:
            return None
        outputs = {
            item["id"]: _TABLE_OUTPUT_SCHEMAS.get(item.get("type") or "", {})
            for item in table["outputs"]
        }
        if table["hitPolicy"] == "collect":
            # A collect table gives every matching rule's outputs, in order.
            return {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": outputs},
                    }
                },
            }
        return {"type": "object", "properties": outputs}

    def _decide_references(self) -> None:
        used: set[str] = set()
        for path, body in self.decides:
            table = self.tables.get(body["table"])
            if table is None:
                self.problem(
                    "unknown_decision_table",
                    path + "/table",
                    f"the process has no decision table {body['table']!r}",
                    hint=_did_you_mean(body["table"], list(self.tables)),
                )
                continue
            used.add(table["id"])
            inputs = [item["id"] for item in table["inputs"]]
            for name in body.get("input") or {}:
                if name not in inputs:
                    self.problem(
                        "unknown_table_input",
                        f"{path}/input{pointer(name)}",
                        f"table {table['id']!r} has no input {name!r}",
                        hint=_did_you_mean(name, inputs),
                    )
        for index, table in enumerate(self.spec.get("decisions") or ()):
            if table["id"] not in used:
                self.problem(
                    "unused_decision_table",
                    pointer("spec", "decisions", index),
                    f"no decide step uses table {table['id']!r}",
                    warning=True,
                )

    # --- compensation, stability, reachability ---------------------------------------------

    def _compensations(self) -> None:
        for path, body in self.compensations:
            if body == "all":
                if not self.compensable:
                    self.problem(
                        "nothing_to_compensate",
                        path,
                        "no step of the process declares onCompensate",
                        warning=True,
                    )
                continue
            for index, target in enumerate(body):
                if target not in self.compensable:
                    self.problem(
                        "unknown_compensation_target",
                        f"{path}/{index}",
                        f"{target!r} is not a step with onCompensate",
                        hint=_did_you_mean(target, sorted(self.compensable)),
                    )

    def _stability(self) -> None:
        version = self.spec["version"]
        migrations = list(self.spec.get("migrations") or ())
        for index, migration in enumerate(migrations):
            path = pointer("spec", "migrations", index)
            if migration["from"] >= migration["to"] or migration["to"] > version:
                self.problem(
                    "invalid_migration",
                    path,
                    f"a migration goes from an older version to a newer one, at most {version}",
                )
                continue
            older = self.catalog.versions.get(migration["from"])
            for source, target in (migration.get("map") or {}).items():
                if migration["to"] == version and target not in self.ids:
                    self.problem(
                        "unknown_element",
                        f"{path}/map{pointer(source)}",
                        f"version {version} has no element {target!r}",
                        hint=_did_you_mean(target, list(self.ids)),
                    )
                if older is not None and source not in element_kinds(older):
                    self.problem(
                        "unknown_element",
                        f"{path}/map{pointer(source)}",
                        f"version {migration['from']} has no element {source!r}",
                    )
        previous = self.catalog.previous
        if previous is None:
            return
        before = element_kinds(previous)
        mapped = {
            source
            for migration in migrations
            if migration.get("from") == previous.get("version")
            for source in (migration.get("map") or {})
        }
        for element_id, kind in sorted(before.items()):
            now = self.ids.get(element_id)
            if now is not None and now[0] != kind:
                self.problem(
                    "element_kind_changed",
                    now[1] + "/id",
                    f"{element_id!r} was a {kind} in version {previous.get('version')},"
                    f" now it is a {now[0]}",
                    hint="ids are stable: journals, layouts and the memory graph refer to them;"
                    " give the new element a new id",
                )
            elif now is None and element_id not in mapped:
                self.problem(
                    "element_removed",
                    "/spec",
                    f"{kind} {element_id!r} of version {previous.get('version')} is gone",
                    hint="instances on it stay on the old version; to move them, add"
                    f" migrations: [{{from: {previous.get('version')}, to: {version},"
                    " policy: migrate, map: {…}}]",
                    warning=True,
                )

    def _written(self, read: str) -> bool:
        field_path = read[len("data.") :]
        return any(
            field_path == written
            or field_path.startswith(written + ".")
            or written.startswith(field_path + ".")
            for written in self.writes
        )

    def _reachability(self) -> None:
        guarded_reads = {(r.path, p) for r in self.reads for p in r.program.guarded}
        reported: set[tuple[str, str]] = set()
        for read in self.reads:
            for name in read.program.reads:
                if not name.startswith("data.") or self._written(name):
                    continue
                if (read.path, name) in guarded_reads or (read.path, name) in reported:
                    continue
                reported.add((read.path, name))
                self.problem(
                    "unwritten_data_field",
                    read.path,
                    f"{name} is read but no set, start.set, correlate.set or output.as writes it",
                    warning=True,
                )
        stuck: dict[str, str] = {}
        for role, path, program in self.guards:
            if self._never_true(program):
                stuck[path] = role
                if role == "exit":
                    self.problem(
                        "dead_end",
                        path,
                        "the exit guard can never become true: the stage never ends",
                        hint="it reads only data nothing writes, or is false",
                    )
                elif role == "milestone":
                    self.problem(
                        "unreachable_milestone",
                        path,
                        "the milestone can never be reached",
                        warning=True,
                    )
        entries = {
            stage["id"]: pointer("spec", "stages", index, "entry")
            for index, stage in enumerate(self.spec["stages"])
        }
        programs = {where: guard for role, where, guard in self.guards if role == "entry"}
        reachable: set[str] = set()
        changed = True
        while changed:
            changed = False
            for stage_id, path in entries.items():
                if stage_id in reachable:
                    continue
                entry = programs.get(path)
                if (entry is None and path not in stuck) or (
                    entry is not None and self._may_enter(stage_id, entry, reachable)
                ):
                    reachable.add(stage_id)
                    changed = True
        for stage_id, path in entries.items():
            if stage_id not in reachable:
                self.problem(
                    "unreachable_stage",
                    path,
                    f"stage {stage_id!r} can never be entered",
                    hint="its entry waits for stages that cannot complete, or for data"
                    " nothing writes",
                )

    def _never_true(self, program: Program) -> bool:
        if not program.reads:
            try:
                return program.evaluate({}).value is False
            except ExpressionError:
                return False
        return all(name.startswith("data.") and not self._written(name) for name in program.reads)

    def _may_enter(self, stage_id: str, program: Program, reachable: set[str]) -> bool:
        if self._never_true(program):
            return False
        for name in program.reads:
            if name.startswith("stage."):
                other = name.split(".")[1]
                if other != stage_id and other in reachable:
                    return True
            elif not name.startswith("data.") or self._written(name):
                return True
        return False


# --- helpers ---------------------------------------------------------------------------


def _external_refs(node: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            yield path + "/$ref", ref
        for key, value in node.items():
            yield from _external_refs(value, path + pointer(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _external_refs(value, f"{path}/{index}")


def element_kinds(spec: Mapping[str, Any]) -> dict[str, str]:
    """Element ids of a spec and their kinds (stage, step, milestone, timer, branch, table)."""
    found: dict[str, str] = {}

    def steps(items: Any) -> None:
        for step in items or ():
            if not isinstance(step, dict) or "id" not in step:
                continue
            found.setdefault(step["id"], "step")
            kind = step_kind(step)
            body = step.get(kind)
            steps(step.get("onCompensate"))
            if kind in ("do",):
                steps(body)
            elif isinstance(body, dict):
                steps(body.get("do"))
                steps(body.get("onTimeout"))
                for option in body.get("any") or ():
                    steps(option.get("do"))
                for branch in body.get("branches") or ():
                    found.setdefault(branch.get("id"), "branch")
                    steps(branch.get("do"))
                for clause in body.get("catch") or ():
                    steps(clause.get("do"))

    def timers(items: Any) -> None:
        for timer in items or ():
            found.setdefault(timer.get("id"), "timer")
            steps(timer.get("do"))

    for stage in spec.get("stages") or ():
        found.setdefault(stage.get("id"), "stage")
        for milestone in stage.get("milestones") or ():
            found.setdefault(milestone.get("id"), "milestone")
        timers(stage.get("timers"))
        steps(stage.get("steps"))
        steps(stage.get("discretionary"))
    timers(spec.get("timers"))
    for item in (*(spec.get("correlate") or ()), *(spec.get("onEvent") or ())):
        steps(item.get("do"))
    for table in spec.get("decisions") or ():
        found.setdefault(table.get("id"), "decision")
    found.pop(None, None)  # type: ignore[call-overload]
    return found


def _object_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "object"


@dataclass(frozen=True)
class _Unknown:
    depth: int
    name: str
    known: tuple[str, ...]


def _resolve(schema: JsonSchema, segments: Sequence[str]) -> JsonSchema | _Unknown | None:
    """The schema of a path in ``schema``; ``None`` where the schema says nothing definite."""
    current: JsonSchema = schema
    for depth, name in enumerate(segments):
        if _types(current) - {"object", "null"} or "properties" not in current:
            if isinstance(current.get("additionalProperties"), dict):
                current = current["additionalProperties"]
                continue
            return None
        properties = current.get("properties") or {}
        if name not in properties:
            return _Unknown(depth, name, tuple(properties))
        current = properties[name]
    return current


def _types(schema: JsonSchema) -> set[str]:
    kind = schema.get("type")
    if isinstance(kind, str):
        return {kind}
    if isinstance(kind, list):
        return {k for k in kind if isinstance(k, str)}
    return set()


# CEL types of a value by the JSON Schema types they fit.
_FITS: Mapping[str, tuple[str, ...]] = {
    "STRING": ("string",),
    "BYTES": ("string",),
    "INT": ("integer", "number"),
    "UINT": ("integer", "number"),
    "DOUBLE": ("number",),
    "BOOL": ("boolean",),
    "TIMESTAMP": ("string",),
    "DURATION": ("string",),
    "NULL": ("null",),
}


def _fits(output_type: str, schema: JsonSchema) -> bool:
    types = _types(schema)
    if not types or output_type in ("DYN", "ERROR") or output_type.startswith("DYN"):
        return True
    if output_type == "NULL":
        return "null" in types
    if output_type.startswith("LIST"):
        return "array" in types
    if output_type.startswith("MAP") or "." in output_type:
        return "object" in types
    fits = _FITS.get(output_type)
    return fits is None or bool(types & set(fits))


def _gives(output_type: str, expect: str) -> bool:
    if output_type.startswith("DYN") or output_type in ("DYN", "ERROR"):
        return True
    return any(
        output_type == kind or output_type.startswith(kind + "<") for kind in _EXPECTED[expect]
    )


def _type_word(output_type: str) -> str:
    if "." in output_type and not output_type.startswith(("MAP", "LIST")):
        return "an object"
    return output_type.lower()


def _schema_word(schema: JsonSchema) -> str:
    types = sorted(_types(schema))
    return " or ".join(types) if types else "untyped"


def _table_type(program: Program | None, table: Mapping[str, Any], input_id: str) -> str:
    """The type of an input without ``type``: from its expression, else from its cells."""
    if program is not None:
        derived = {
            "STRING": "string",
            "INT": "number",
            "UINT": "number",
            "DOUBLE": "number",
            "BOOL": "boolean",
            "TIMESTAMP": "timestamp",
        }.get(program.output_type)
        if derived is not None:
            return derived
    cells = [rule["when"].get(input_id) for rule in table["rules"] if rule.get("when")]
    cells = [c for c in cells if c is not None and c != "-"]
    if cells and all(isinstance(c, bool) for c in cells):
        return "boolean"
    if cells and all(isinstance(c, int | float) and not isinstance(c, bool) for c in cells):
        return "number"
    return "string"


def _did_you_mean(name: str, known: Sequence[str]) -> str | None:
    close = difflib.get_close_matches(name, list(known), n=1)
    return f"did you mean {close[0]}?" if close else None


# --- a package object ----------------------------------------------------------------------


def split_document(document: Mapping[str, Any]) -> tuple[Any, Any]:
    """``(key, spec)`` of a catalog object of kind ``Process``; the package checked its envelope."""
    if document.get("kind") != CATALOG_KIND:
        raise SpecError(f"not a catalog object of kind {CATALOG_KIND}", "/kind")
    return document.get("key"), copy.deepcopy(document.get("spec"))
