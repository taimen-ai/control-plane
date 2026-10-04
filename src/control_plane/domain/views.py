"""Screens of a package by description: the kinds ``View`` and ``Component`` (CP-ADR-0080).

TAI-ADR-0066, stage 1. A view says what a screen of a package shows — its
source, its blocks of the closed set of version 1 and the formats of their
values — and never how: the console draws it. The core checks a view when a
package is planned and applied and keeps it with its revision; an unchecked
view is not installed. What is decided here, without I/O:

- **Shape** — ``view.schema.json`` (``$defs.viewSpec``, ``$defs.componentSpec``;
  package-sdk keeps a byte copy). A block is named by its key ``block``
  (TAI-ADR-0066 p.2); a block or a format the set does not know is
  ``unknown_block`` / ``unknown_format``; ``code`` of a view or a component is
  ``component_code_not_supported`` (TAI-ADR-0066 p.7.2: no components with code).
- **Source** — exactly one of ``{process, filter?}``, ``{process, instance:
  param.<name>}``, ``{tasks: {type}}``, ``{knowledge: {kinds}}``; the process
  or task type exists in the package or in the catalog (``unknown_source``).
- **Paths** — ``data.*`` of a process source is declared in the JSON Schema of
  the process data, ``instance.*`` is a field of the instance
  (``undeclared_path``); ``stage``, ``status``, ``slaState`` and ``id`` are the
  stage, the state, the deadline state and the id of an instance. Of a view
  of tasks (stage 6) — ``id``, ``fields.<field of TaskOut>`` and
  ``customFields.*`` declared in the ``fieldSchema`` of the type; of a view of
  knowledge — the fields of a record, ``attributes.*`` and, a column only,
  ``relations.<name>``; the kinds, attributes and relations of the ontology
  of a tree are warnings of the plan (:func:`check_knowledge`).
- **Expressions** — CEL of the profile of processes (CP-ADR-0075) with ``data``
  typed by the process data schema, ``param`` by the ``params`` (a param of a
  component may be typed by a part of a data schema) and ``id``, ``status``,
  ``slaState`` of the instance; the type an expression gives fits the format of
  its value (``format_type_mismatch``). Aggregates ``count/sum/avg/min/max``
  are written only in ``metrics`` and ``chart`` (``aggregate_outside_metrics``),
  and there every value is one (``invalid_aggregate``).
- **References** — ``open.view`` names a view of the package or of a package it
  requires (``unknown_view``), a ``component`` block a component of the package
  (``unknown_component``) and gives it its params (``with``), ``invoke`` a skill
  of the package or of the catalog with an output schema to show its result
  by (``unknown_skill``, ``skill_output_missing``), ``audience.roles`` roles of
  the organization or of the package (``unknown_role``).
- **Strings** — every string a person sees is a key of the package
  dictionaries, present in the dictionary of every declared locale
  (``missing_message``); a label not written is the key
  ``<package>.fields.<path>`` (TAI-ADR-0066 p.1a); ``package.yaml`` declares
  ``locales`` and ``defaultLocale`` (:func:`check_locales`).

The form of a view (:func:`ViewCheck.form`) is what a revision stores and
hashes: its spec with the components inlined (what the query of a view reads,
stage 2), the **display** — the description a console draws (CP-ADR-0080 §9:
keys of columns, titles, filters typed by the data schema; no path and no
expression) with the keys of the dictionaries in it — and the texts of those
keys in every locale. A text changed in a dictionary changes the form: a new
revision, ``view.published``. :func:`present` gives the display in a language.

Pure functions over plain values; no I/O.
"""

import copy
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from control_plane.domain.cel_profile import (
    INSTANCE_SCHEMA,
    Environment,
    ExpressionError,
    environment,
)
from control_plane.domain.package_source import (
    LOCALE_PATTERN,
    MESSAGE_KEY_PATTERN,
    PackageObject,
    ParsedPackage,
)
from control_plane.domain.process_definition import Problem, SkillEntry, pointer
from control_plane.domain.process_engine import CLOSED, RUNNING, SUSPENDED
from control_plane.domain.process_sla import SHOWN as SLA_STATES
from control_plane.domain.settings_refs import (
    NONE,
    SETTINGS,
    SettingsScope,
    compile_expression,
    declared,
    loose_program,
    reads_settings,
    settings_reads,
    type_error,
    unknown_error,
)

VIEW = "View"
COMPONENT = "Component"
SCHEMA_FILE = Path(__file__).with_name("view.schema.json")
# The version of the set of blocks; a view is drawn by a console that knows it.
BLOCK_SET = 1
BLOCKS = (
    "table",
    "board",
    "list",
    "header",
    "fields",
    "timeline",
    "artifacts",
    "related",
    "metrics",
    "chart",
    "steps",
    "invoke",
    "component",
)
FORMATS = (
    "text",
    "number",
    "money",
    "percent",
    "date",
    "datetime",
    "due",
    "duration",
    "principal",
    "status",
    "link",
)
AGGREGATES = ("count", "sum", "avg", "min", "max")
# The groups of the console menu (CP-ADR-0080 §9): a closed list, not keys of the dictionaries.
NAV_GROUPS = ("work", "knowledge", "packages")
DEFAULT_NAV_GROUP = "packages"
# Blocks of many records and of the one record a view shows (``instance``).
COLLECTION_BLOCKS = frozenset({"table", "list", "board"})
INSTANCE_BLOCKS = frozenset({"header", "fields", "timeline", "steps"})
# Blocks only a process has: its journal, its steps and its stages as the columns of a board.
PROCESS_BLOCKS = frozenset({"timeline", "steps", "board"})
AGGREGATE_BLOCKS = frozenset({"metrics", "chart"})
SOURCES = ("process", "tasks", "knowledge")
# The fields of a manifest the screens of a package need.
LOCALES_FIELD = "locales"
DEFAULT_LOCALE_FIELD = "defaultLocale"
# What a form adds to the spec of a view.
FORM_FIELDS = ("display", "messages", "locales", "defaultLocale")
MAX_SCHEMA_PROBLEMS = 20
# A key of the dictionaries in the display: replaced by its text when presented.
TEXT = "$t"
# Of a text that may be missing from the dictionaries: what is shown then.
OTHERWISE = "or"
# Fields of an instance a view reads besides data, instance and stage (``GET /process-instances``).
INSTANCE_FIELDS: Mapping[str, dict[str, Any]] = {
    "id": {"type": "string"},
    "status": {"type": "string"},
    "slaState": {"type": "string"},
}
INSTANCE_STATUSES = (RUNNING, SUSPENDED, *CLOSED)
_TIME_FIELD: Mapping[str, Any] = {"type": "string", "format": "date-time"}
# Fields of a task a view of tasks reads as ``fields.<name>`` (TAI-ADR-0066 stage 6): those of
# ``TaskOut``; ``customFields.<path>`` is typed by the ``fieldSchema`` of the task type.
TASK_FIELDS: Mapping[str, Mapping[str, Any]] = {
    "publicId": {"type": "string"},
    "title": {"type": "string"},
    "description": {"type": "string"},
    "status": {"type": "string"},
    "systemStatusCategory": {"type": "string"},
    "priority": {"type": "string"},
    "ownerId": {"type": "string"},
    "assigneeId": {"type": "string"},
    "workspaceId": {"type": "string"},
    "createdBy": {"type": "string"},
    "startDate": _TIME_FIELD,
    "dueDate": _TIME_FIELD,
    "createdAt": _TIME_FIELD,
    "updatedAt": _TIME_FIELD,
    "completedAt": _TIME_FIELD,
}
TASK_PRIORITIES = ("critical", "high", "medium", "low")
TASK_CATEGORIES = ("backlog", "active", "blocked", "terminal_success", "terminal_cancelled")
# Fields of a record of the knowledge base a view of knowledge reads besides ``attributes.<path>``
# (``KnowledgeEntityOut``); ``relations.<name>`` is a column of a table or a list only.
KNOWLEDGE_FIELDS: Mapping[str, Mapping[str, Any]] = {
    "id": {"type": "string"},
    "kind": {"type": "string"},
    "key": {"type": "string"},
    "title": {"type": "string"},
    "validFrom": {"type": "string"},
    "validTo": {"type": "string"},
}
KNOWLEDGE_DATES = ("validFrom", "validTo")
# A step of a path of the attributes of a record and the name of a relation (ADR-0060 C1).
_ATTRIBUTE_STEP = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,99}$")
RELATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# The param ``source.instance`` names when the view does not declare it.
INSTANCE_PARAM: Mapping[str, Any] = {"type": "uuid", "required": True}
FILTER_TYPES = ("text", "enum", "number", "date")
_COLUMN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")

# The CEL types (``Program.output_type``) a format shows; ``DYN`` fits every format.
_NUMBERS = frozenset({"INT", "UINT", "DOUBLE"})
_TIMES = frozenset({"TIMESTAMP", "STRING"})
FORMAT_TYPES: Mapping[str, frozenset[str]] = {
    "text": frozenset({"STRING"}),
    "number": _NUMBERS,
    "money": _NUMBERS | {"STRING"},
    "percent": _NUMBERS,
    "date": _TIMES,
    "datetime": _TIMES,
    "due": _TIMES,
    "duration": frozenset({"DURATION", "STRING"}),
    "principal": frozenset({"STRING"}),
    "status": frozenset({"STRING"}),
    "link": frozenset({"STRING"}),
}
_AGGREGATE = re.compile(r"^\s*(count|sum|avg|min|max)\s*\((.*)\)\s*$", re.DOTALL)
# A call of an aggregate anywhere in an expression; a method (``x.max(``) is not one.
_AGGREGATE_CALL = re.compile(r"(?<![\w.])(count|sum|avg|min|max)\s*\(")
# A string literal of CEL: quoted, triple-quoted, raw and bytes; what it holds is no call.
_STRING_LITERAL = re.compile(
    r"(?<![\w.])(?:[rR][bB]?|[bB][rR]?)?"
    r'(?:"""(?:\\.|[^\\])*?"""'
    r"|'''(?:\\.|[^\\])*?'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*')",
    re.DOTALL,
)
_PARAM_TYPES: Mapping[str, dict[str, Any]] = {
    "string": {"type": ["string", "null"]},
    "uuid": {"type": ["string", "null"]},
    "date": {"type": ["string", "null"]},
    "datetime": {"type": ["string", "null"], "format": "date-time"},
    "integer": {"type": ["integer", "null"]},
    "number": {"type": ["number", "null"]},
    "boolean": {"type": ["boolean", "null"]},
}
# The CEL types an expression given to a param of each type may have.
_PARAM_CEL_TYPES: Mapping[str, frozenset[str]] = {
    "string": frozenset({"STRING"}),
    "uuid": frozenset({"STRING"}),
    "date": frozenset({"STRING", "TIMESTAMP"}),
    "datetime": frozenset({"STRING", "TIMESTAMP"}),
    "integer": frozenset({"INT", "UINT"}),
    "number": _NUMBERS,
    "boolean": frozenset({"BOOL"}),
}
# The same for a param typed by a JSON Schema, by its JSON type.
_SCHEMA_CEL_TYPES: Mapping[str, frozenset[str]] = {
    "object": frozenset({"MESSAGE", "MAP"}),
    "array": frozenset({"LIST"}),
    "string": frozenset({"STRING", "TIMESTAMP", "DURATION"}),
    "integer": frozenset({"INT", "UINT"}),
    "number": _NUMBERS,
    "boolean": frozenset({"BOOL"}),
}


def _calls_aggregate(text: str) -> bool:
    """Whether ``text`` calls an aggregate; a name in a string literal is no call."""
    return _AGGREGATE_CALL.search(_STRING_LITERAL.sub('""', text)) is not None


def split_aggregate(text: str) -> tuple[str, str] | None:
    """``(name, argument)`` of a value of ``metrics`` or ``chart``: ``sum(x)`` is ``(sum, x)``."""
    match = _AGGREGATE.match(text)
    return (match.group(1), match.group(2).strip()) if match else None


@cache
def view_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return schema


@cache
def _validator(definition: str) -> Draft202012Validator:
    schema = view_schema()
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})


# --- what a check needs to know --------------------------------------------------------------


@dataclass(frozen=True)
class ProcessShape:
    """What a view reads of a process: the JSON Schema of its data and its stage ids."""

    data: Mapping[str, Any] | None
    stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskShape:
    """What a view reads of a task type: the ``fieldSchema`` of its custom fields and its statuses.

    ``statuses`` — ``(key, displayName)`` of the statuses of its lifecycle, in their order.
    """

    field_schema: Mapping[str, Any] | None
    statuses: tuple[tuple[str, str], ...] = ()


def task_shape(field_schema: Any, lifecycle: Any) -> TaskShape:
    """The shape of a task type from its ``fieldSchema`` and ``lifecycleSchema``."""
    statuses = tuple(
        (str(s["key"]), str(s.get("displayName") or s["key"]))
        for s in (lifecycle.get("statuses") if isinstance(lifecycle, Mapping) else None) or ()
        if isinstance(s, Mapping) and isinstance(s.get("key"), str)
    )
    return TaskShape(field_schema if isinstance(field_schema, Mapping) else None, statuses)


@dataclass(frozen=True)
class ViewContext:
    """The world a view is checked against: the package and the catalog around it.

    ``package`` — the key of the package: the prefix of the keys of labels not
    written (``<package>.fields.<path>``); ``processes``, ``task_types``,
    ``roles``, ``skills`` (by ``name@version``) — of the package and of the
    tenant; ``views`` — the views ``open.view`` may name: the package's own and
    those of the packages it requires; ``components`` — the components of the
    package that passed their own check; ``settings`` — the settings the
    package declares.
    """

    locales: tuple[str, ...] = ()
    default_locale: str | None = None
    dictionaries: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    processes: Mapping[str, ProcessShape] = field(default_factory=dict)
    task_types: frozenset[str] = frozenset()
    # The shapes of the task types a view of tasks names (TAI-ADR-0066 stage 6).
    task_shapes: Mapping[str, TaskShape] = field(default_factory=dict)
    roles: frozenset[str] = frozenset()
    skills: Mapping[str, SkillEntry] = field(default_factory=dict)
    views: frozenset[str] = frozenset()
    components: Mapping[str, PackageObject] = field(default_factory=dict)
    component_keys: frozenset[str] = frozenset()
    package: str = ""
    # What ``settings`` of the expressions is: the schema the package declares (CP-ADR-0081 §6).
    settings: SettingsScope = NONE


@dataclass(frozen=True)
class References:
    """What the views of a package name outside themselves: the catalog to read."""

    processes: frozenset[str]
    task_types: frozenset[str]
    roles: frozenset[str]
    views: frozenset[str]
    skills: frozenset[str] = frozenset()


def references(package: ParsedPackage) -> References:
    processes: set[str] = set()
    task_types: set[str] = set()
    roles: set[str] = set()
    views: set[str] = set()
    skills: set[str] = set()
    for obj in package.of_kind(VIEW) + package.of_kind(COMPONENT):
        spec = obj.spec
        source = spec.get("source")
        if isinstance(source, Mapping):
            if isinstance(source.get("process"), str):
                processes.add(source["process"])
            tasks = source.get("tasks")
            if isinstance(tasks, Mapping) and isinstance(tasks.get("type"), str):
                task_types.add(tasks["type"])
        audience = spec.get("audience")
        if isinstance(audience, Mapping) and isinstance(audience.get("roles"), list):
            roles.update(r for r in audience["roles"] if isinstance(r, str))
        for node in _nodes(spec.get("layout")):
            target = node.get("open")
            if isinstance(target, Mapping) and isinstance(target.get("view"), str):
                views.add(target["view"])
            if node.get("block") == "invoke" and isinstance(node.get("skill"), str):
                skills.add(node["skill"])
    return References(
        frozenset(processes),
        frozenset(task_types),
        frozenset(roles),
        frozenset(views),
        frozenset(skills),
    )


def _nodes(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every mapping under ``value``."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


# --- the package: locales and dictionaries ---------------------------------------------------


def declared_locales(package: ParsedPackage) -> tuple[tuple[str, ...], str | None]:
    """``locales`` and ``defaultLocale`` of ``package.yaml`` as far as they are well formed."""
    manifest = package.manifest or {}
    raw = manifest.get(LOCALES_FIELD)
    locales = (
        tuple(dict.fromkeys(x for x in raw if isinstance(x, str) and LOCALE_PATTERN.match(x)))
        if isinstance(raw, list)
        else ()
    )
    default = manifest.get(DEFAULT_LOCALE_FIELD)
    return locales, default if isinstance(default, str) and default in locales else None


def check_locales(package: ParsedPackage) -> list[Problem]:
    """``locales`` and ``defaultLocale`` of the manifest against the dictionaries."""
    manifest = package.manifest_object
    screens = bool(package.of_kind(VIEW) or package.of_kind(COMPONENT))
    raw = (manifest.spec if manifest else {}).get(LOCALES_FIELD)
    default = (manifest.spec if manifest else {}).get(DEFAULT_LOCALE_FIELD)
    problems: list[Problem] = []

    def at_manifest(code: str, path: str, message: str, hint: str | None = None) -> None:
        problem = Problem(code, "error", path, message, hint)
        problems.append(manifest.place(problem) if manifest else problem)

    if raw is None and default is None:
        if screens or package.dictionaries:
            at_manifest(
                "locales_required",
                "/spec",
                "the package has screens or dictionaries: package.yaml declares locales"
                " and defaultLocale",
                hint="locales: [en, ru], defaultLocale: en",
            )
        return problems
    if not isinstance(raw, list) or not raw:
        at_manifest("invalid_locales", "/spec/locales", "locales is a non-empty list of locales")
        return problems
    seen: set[str] = set()
    for index, locale in enumerate(raw):
        where = pointer("spec", "locales", index)
        if not isinstance(locale, str) or not LOCALE_PATTERN.match(locale):
            at_manifest("invalid_locales", where, f"{locale!r} is not a locale such as en or pt-BR")
        elif locale in seen:
            at_manifest("invalid_locales", where, f"locale {locale} is declared twice")
        else:
            seen.add(locale)
            if locale not in package.dictionaries:
                at_manifest(
                    "missing_dictionary",
                    where,
                    f"locale {locale} has no dictionary i18n/{locale}.yaml",
                )
    if not isinstance(default, str) or default not in seen:
        at_manifest(
            "invalid_default_locale",
            "/spec/defaultLocale",
            f"defaultLocale {default!r} is not one of the declared locales",
            hint=f"one of {', '.join(sorted(seen))}" if seen else None,
        )
    for locale, dictionary in sorted(package.dictionaries.items()):
        if locale not in seen:
            problems.append(
                Problem(
                    "undeclared_locale",
                    "error",
                    "",
                    f"locale {locale} of {dictionary.file} is not declared in package.yaml",
                    hint="add it to locales or remove the dictionary",
                    file=dictionary.file,
                    line=1,
                )
            )
    return problems


def unused_messages(package: ParsedPackage, used: set[str]) -> list[Problem]:
    """Warnings: keys of a dictionary no view or component of the package shows."""
    problems = []
    for _, dictionary in sorted(package.dictionaries.items()):
        for key in sorted(set(dictionary.messages) - used):
            where = pointer(key)
            problems.append(
                Problem(
                    "unused_message",
                    "warning",
                    where,
                    f"no view or component of the package shows {key}",
                    file=dictionary.file,
                    line=dictionary.locate(where),
                )
            )
    return problems


# --- the check of a view ---------------------------------------------------------------------

# Lists of a block whose items are columns, and the field of an item a person reads.
_COLUMN_LISTS = (("columns", "label"), ("items", "label"))


def message_places(spec: Mapping[str, Any], base: str = "/spec") -> Iterator[tuple[str, str]]:
    """``(pointer, key)`` of every key of the dictionaries ``spec`` writes out.

    Labels not written (``<package>.fields.<path>``) are not here: they depend
    on the source of the view a block is shown in (:class:`_Checker`).
    """
    for name in ("title", "description"):
        if isinstance(spec.get(name), str):
            yield f"{base}/{name}", spec[name]
    layout = spec.get("layout")
    if isinstance(layout, list):
        for index, block in enumerate(layout):
            if isinstance(block, Mapping):
                yield from _block_messages(block, f"{base}/layout/{index}")


def _block_messages(block: Mapping[str, Any], at: str) -> Iterator[tuple[str, str]]:
    kind = block.get("block")
    # The title of a header is a path of the source, not a key.
    names = ("section", "label") if kind == "header" else ("title", "section", "label")
    for name in names:
        if isinstance(block.get(name), str):
            yield f"{at}/{name}", block[name]
    groups: list[tuple[str, Any, str]] = [
        (f"{at}/columns", block.get("columns"), "label"),
        (f"{at}/items", block.get("items"), "title" if kind == "metrics" else "label"),
    ]
    card = block.get("card")
    if isinstance(card, Mapping):
        groups.append((f"{at}/card/fields", card.get("fields"), "label"))
    for where, items, name in groups:
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, Mapping) and isinstance(item.get(name), str):
                yield f"{where}/{index}/{name}", item[name]
    inlined = block.get("layout")
    if kind == "component" and isinstance(inlined, list):
        for index, inner in enumerate(inlined):
            if isinstance(inner, Mapping):
                yield from _block_messages(inner, f"{at}/layout/{index}")


def _shape_problems(spec: Mapping[str, Any], definition: str, noun: str) -> list[Problem]:
    errors = sorted(
        _validator(definition).iter_errors(spec), key=lambda e: [str(p) for p in e.absolute_path]
    )
    problems: list[Problem] = []
    for error in errors[:MAX_SCHEMA_PROBLEMS]:
        cause = _deepest(error)
        parts = list(cause.absolute_path)
        where = pointer("spec", *parts)
        last = parts[-1] if parts else None
        if cause.validator == "enum" and last == "block" and "layout" in parts:
            problems.append(
                Problem(
                    "unknown_block",
                    "error",
                    where,
                    f"block {cause.instance!r} is not in the set of version {BLOCK_SET}",
                    hint=", ".join(BLOCKS),
                )
            )
        elif cause.validator == "enum" and last == "format":
            problems.append(
                Problem(
                    "unknown_format",
                    "error",
                    where,
                    f"format {cause.instance!r} is not in the set of version {BLOCK_SET}",
                    hint=", ".join(FORMATS),
                )
            )
        elif cause.validator == "enum" and parts == ["nav", "group"]:
            problems.append(
                Problem(
                    f"invalid_{noun}",
                    "error",
                    where,
                    f"nav group {cause.instance!r} is not a group of the console menu",
                    hint=", ".join(NAV_GROUPS),
                )
            )
        else:
            problems.append(Problem(f"invalid_{noun}", "error", where, cause.message[:500]))
    return problems


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    if not error.context:
        return error
    best = jsonschema.exceptions.best_match(error.context)
    return _deepest(best) if len(best.absolute_path) >= len(error.absolute_path) else error


def _no_code(obj: PackageObject) -> list[Problem]:
    if "code" not in obj.spec:
        return []
    return [
        Problem(
            "component_code_not_supported",
            "error",
            "/spec/code",
            f"a {obj.kind} is a description: components with code are not supported",
            hint="describe the screen with the blocks of the set (TAI-ADR-0066 p.7.1)",
        )
    ]


def check_component(obj: PackageObject, context: ViewContext) -> list[Problem]:
    """The shape of a component and its strings; its paths and expressions are checked in a view."""
    problems = _no_code(obj)
    spec = {k: v for k, v in obj.spec.items() if k != "code"}
    problems += _shape_problems(spec, "componentSpec", "component")
    if not problems:
        for index, block in enumerate(spec["layout"]):
            if block.get("block") == "component":
                problems.append(
                    Problem(
                        "nested_component",
                        "error",
                        f"/spec/layout/{index}",
                        "a component is not made of components: inline its blocks",
                    )
                )
        for name, param in sorted((spec.get("params") or {}).items()):
            schema = param.get("schema")
            ref = schema.get("$ref") if isinstance(schema, Mapping) else None
            # A file of the package is read by the parser (unresolved_schema_ref when it cannot be).
            if isinstance(ref, str) and (ref.startswith("#") or "://" in ref):
                problems.append(
                    Problem(
                        "invalid_component",
                        "error",
                        f"/spec/params/{name}/schema/$ref",
                        "the schema of a param is written inline or names a schema file of the"
                        " package: <file>#<pointer>",
                    )
                )
        problems += _missing_messages(message_places(spec), context)
    return [obj.place(p) for p in problems]


def _missing_messages(places: Iterator[tuple[str, str]], context: ViewContext) -> list[Problem]:
    problems = []
    for where, key in places:
        missing = _missing_in(key, context)
        if missing:
            problems.append(_missing_message(where, key, missing))
    return problems


def _missing_in(key: str, context: ViewContext) -> list[str]:
    return [loc for loc in context.locales if key not in context.dictionaries.get(loc, {})]


def _missing_message(where: str, key: str, missing: Sequence[str]) -> Problem:
    return Problem(
        "missing_message",
        "error",
        where,
        f"{key} is not in the dictionary of {', '.join(missing)}",
        hint=" ".join(f"i18n/{loc}.yaml" for loc in missing),
    )


def _text(key: str, otherwise: str | None = None) -> dict[str, str]:
    """A key of the dictionaries in the display; ``otherwise`` — shown when no dictionary has it."""
    return {TEXT: key} if otherwise is None else {TEXT: key, OTHERWISE: otherwise}


@dataclass
class ViewCheck:
    """The findings of a view and, when it has none, its form."""

    problems: list[Problem]
    form: dict[str, Any] | None
    # The keys of the dictionaries the view shows, its components' included.
    messages: set[str] = field(default_factory=set)


@dataclass
class _Checker:
    obj: PackageObject
    context: ViewContext
    spec: dict[str, Any]
    env: Environment | None = None
    source: str = ""
    process: ProcessShape | None = None
    task: TaskShape | None = None
    kinds: tuple[str, ...] = ()
    instance: bool = False
    problems: list[Problem] = field(default_factory=list)
    # The keys of the dictionaries the display shows: required, and shown only when present.
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    # The fields an expression compiled last reads.
    reads: tuple[str, ...] = ()
    # The params each environment was built with: one of the view, one of each component.
    params_of: dict[int, Mapping[str, Any]] = field(default_factory=dict)

    # -- findings, placed in the file of the object they are about --

    def add(
        self,
        owner: PackageObject,
        code: str,
        where: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        self.problems.append(owner.place(Problem(code, "error", where, message, hint)))

    def say(self, owner: PackageObject, key: str, where: str, *, written: bool) -> dict[str, str]:
        """A key the display shows. One ``written`` in a component is checked with the component."""
        self.required.add(key)
        if not (written and owner is not self.obj):
            missing = _missing_in(key, self.context)
            if missing:
                self.problems.append(owner.place(_missing_message(where, key, missing)))
        return _text(key)

    def maybe(self, key: str, otherwise: str) -> dict[str, str] | str:
        """A text shown when the dictionaries have it: the title of a value of a filter."""
        if not MESSAGE_KEY_PATTERN.match(key):
            return otherwise
        if any(key in self.context.dictionaries.get(loc, {}) for loc in self.context.locales):
            self.optional.add(key)
        return _text(key, otherwise)

    # -- the source --

    def check_source(self) -> None:
        source = self.spec["source"]
        named = [name for name in SOURCES if name in source]
        if len(named) != 1:
            self.add(
                self.obj,
                "invalid_source",
                "/spec/source",
                "the source is exactly one of process, tasks or knowledge",
                hint="{process, filter?} | {process, instance: param.<name>} | {tasks: {type}}"
                " | {knowledge: {kinds}}",
            )
            return
        self.source = named[0]
        if self.source != "process" and ("filter" in source or "instance" in source):
            self.add(
                self.obj,
                "invalid_source",
                "/spec/source",
                "filter and instance belong to a process source",
            )
        params = self.spec.get("params") or {}
        if self.source == "process":
            key = source["process"]
            self.process = self.context.processes.get(key)
            if self.process is None:
                self.add(
                    self.obj,
                    "unknown_source",
                    "/spec/source/process",
                    f"there is no process {key!r} in the package or in the catalog",
                )
            instance = source.get("instance")
            if isinstance(instance, str):
                self.instance = True
                name = instance.removeprefix("param.")
                if name not in params:
                    # The param of an instance is its id: written or not (TAI-ADR-0066 p.1).
                    params = self.spec["params"] = {**params, name: dict(INSTANCE_PARAM)}
                elif params[name]["type"] not in ("uuid", "string"):
                    self.add(
                        self.obj,
                        "invalid_source",
                        f"/spec/params/{name}/type",
                        f"param {name} is the id of the instance shown: uuid or string",
                    )
                if "filter" in source:
                    self.add(
                        self.obj,
                        "invalid_source",
                        "/spec/source/filter",
                        "a view of one instance has no filter",
                    )
        elif self.source == "tasks":
            key = source["tasks"]["type"]
            self.task = self.context.task_shapes.get(key)
            if key not in self.context.task_types:
                self.add(
                    self.obj,
                    "unknown_source",
                    "/spec/source/tasks/type",
                    f"there is no task type {key!r} in the package or in the catalog",
                )
            elif self.task is None:
                self.task = TaskShape(None)
        else:
            self.kinds = tuple(dict.fromkeys(str(k) for k in source["knowledge"]["kinds"]))
        self.env = self._environment(params)
        if self.source == "process" and isinstance(source.get("filter"), str):
            self.expression(self.obj, source["filter"], "/spec/source/filter", wants="BOOL")

    def _environment(self, params: Mapping[str, Any]) -> Environment:
        env = source_environment(
            self.source,
            self.process,
            params,
            settings=self.context.settings.variable,
            task=self.task,
        )
        self.params_of[id(env)] = params
        return env

    def _build(self, env: Environment) -> Callable[[Mapping[str, Any]], Environment]:
        """The environment ``env`` with another type of ``settings``: what tells its faults."""
        params = self.params_of.get(id(env), {})
        return lambda settings: source_environment(
            self.source, self.process, params, settings=settings, task=self.task
        )

    # -- expressions and paths --

    def expression(
        self,
        owner: PackageObject,
        text: str,
        where: str,
        *,
        wants: str | None = None,
        format: str | None = None,
        aggregate: bool = False,
    ) -> str | None:
        """Compile ``text``; the CEL type it gives, or ``None`` with a finding."""
        self.reads = ()
        if self.env is None:
            return None
        if not aggregate and _calls_aggregate(text):
            self.add(
                owner,
                "aggregate_outside_metrics",
                where,
                "count, sum, avg, min and max are written only in metrics and chart",
            )
            return None
        build = self._build(self.env)
        try:
            program = compile_expression(self.context.settings, build, text, path=where)
        except ExpressionError as exc:
            self.add(owner, exc.code, where, exc.message, hint=exc.hint)
            return None
        self.reads = program.reads
        output = _base_type(program.output_type)
        if wants is not None and output not in (wants, "DYN"):
            if self._settings_fault(owner, build, text, where, lambda out: out in (wants, "DYN")):
                return None
            self.add(
                owner,
                "expression_type_error",
                where,
                f"the expression gives {output}, a {wants} is wanted",
            )
            return None
        if format is not None:
            if output != "DYN" and output not in FORMAT_TYPES[format]:
                fits = FORMAT_TYPES[format]
                if self._settings_fault(owner, build, text, where, lambda out: out in fits):
                    return output
            self.fits(owner, output, format, where)
        return output

    def _settings_fault(
        self,
        owner: PackageObject,
        build: Callable[[Mapping[str, Any]], Environment],
        text: str,
        where: str,
        fits: Callable[[str], bool],
    ) -> bool:
        """``settings_ref_type`` when the type of a read of ``settings`` is why it misfits."""
        if self.context.settings.schema is None:
            return False
        loose = loose_program(build, text, path=where)
        if loose is None or not reads_settings(loose):
            return False
        if not fits(_base_type(loose.output_type)) and _base_type(loose.output_type) != "DYN":
            return False
        error = type_error(loose.reads, path=where)
        self.add(owner, error.code, where, error.message)
        return True

    def fits(self, owner: PackageObject, output: str, format: str, where: str) -> None:
        if output == "DYN" or output in FORMAT_TYPES[format]:
            return
        self.add(
            owner,
            "format_type_mismatch",
            where,
            f"format {format} does not show a value of type {output}",
            hint=f"{format} shows {', '.join(sorted(FORMAT_TYPES[format]))}",
        )

    def path(
        self,
        owner: PackageObject,
        text: str,
        where: str,
        *,
        format: str | None = None,
        shown: bool = False,
    ) -> bool:
        """A path of the source: declared where the source declares its records. Whether it is.

        ``shown`` — the path of a column of a table or a list: of a view of
        knowledge, ``relations.<name>`` is one too.
        """
        if self.source == "tasks":
            return self.task_path(owner, text, where, format=format)
        if self.source == "knowledge":
            return self.knowledge_path(owner, text, where, format=format, shown=shown)
        if text == "stage" or text in INSTANCE_FIELDS:
            if format is not None:
                self.fits(owner, "STRING", format, where)
            return True
        root, _, rest = text.partition(".")
        if root == SETTINGS:
            scope = self.context.settings
            if not rest or scope.schema is None or not declared(scope.schema, text):
                error = unknown_error(scope, text, path=where)
                self.add(owner, error.code, where, error.message, hint=error.hint)
                return False
        elif root == "data" and rest:
            if self.process is None:
                return False
            if _schema_at(self.process.data, rest.split(".")) is None:
                self.add(
                    owner,
                    "undeclared_path",
                    where,
                    f"{text} is not declared in the data schema of process"
                    f" {self.spec['source']['process']!r}",
                    hint="declare it in spec.data of the process",
                )
                return False
        elif root == "instance" and rest in INSTANCE_SCHEMA["properties"]:
            pass
        else:
            self.add(
                owner,
                "undeclared_path",
                where,
                f"{text} is no path of a process instance",
                hint="data.<field>, instance.<"
                + "|".join(sorted(INSTANCE_SCHEMA["properties"]))
                + ">, stage, "
                + ", ".join(INSTANCE_FIELDS),
            )
            return False
        if format is not None:
            self.expression(owner, text, where, format=format)
        return True

    def task_path(self, owner: PackageObject, text: str, where: str, *, format: str | None) -> bool:
        """``id``, ``fields.<field of a task>`` or ``customFields.<path of its fieldSchema>``."""
        root, _, rest = text.partition(".")
        if text == "id" or (root == "fields" and rest in TASK_FIELDS):
            pass
        elif root == "customFields" and rest:
            if self.task is None:
                return False
            if _schema_at(self.task.field_schema, rest.split(".")) is None:
                self.add(
                    owner,
                    "undeclared_path",
                    where,
                    f"{text} is not declared in the fieldSchema of task type"
                    f" {self.spec['source']['tasks']['type']!r}",
                    hint="declare it in fieldSchema of the task type",
                )
                return False
        else:
            self.add(
                owner,
                "undeclared_path",
                where,
                f"{text} is no path of a task",
                hint="id, fields.<" + "|".join(TASK_FIELDS) + ">, customFields.<field>",
            )
            return False
        if format is not None:
            self.expression(owner, text, where, format=format)
        return True

    def knowledge_path(
        self, owner: PackageObject, text: str, where: str, *, format: str | None, shown: bool
    ) -> bool:
        """A field of a record, ``attributes.<path>``, or (a column) ``relations.<name>``.

        The attributes of the kinds of an ontology live in Memory: the plan
        checks them against the ontology of the tree it is asked for (§3 of the
        amendment of stage 6), here only their form.
        """
        root, _, rest = text.partition(".")
        if text in KNOWLEDGE_FIELDS:
            if format is not None:
                self.fits(owner, "STRING", format, where)
            return True
        if root == "attributes" and rest and all(_ATTRIBUTE_STEP.match(p) for p in rest.split(".")):
            if format is not None:
                self.expression(owner, text, where, format=format)
            return True
        if root == "relations" and RELATION_NAME.match(rest):
            if not shown:
                self.add(
                    owner,
                    "undeclared_path",
                    where,
                    f"{text}: the relations of a record are a column of a table or a list,"
                    " not a filter, an order or a group",
                )
                return False
            if format is not None:
                self.fits(owner, "STRING", format, where)
            return True
        self.add(
            owner,
            "undeclared_path",
            where,
            f"{text} is no path of a record of the knowledge base",
            hint="attributes.<attribute>, relations.<relation> (a column), "
            + ", ".join(KNOWLEDGE_FIELDS),
        )
        return False

    def name(self, path: str) -> str | None:
        """The name of a path of the source in keys: ``data.a.b`` is ``a.b``."""
        name = path.removeprefix("data.") if self.source == "process" else path
        return name if _COLUMN_KEY.match(name) else None

    def read_name(self) -> str | None:
        """The name of the one field the expression compiled last reads.

        Params aside (``data.amount * param.rate`` is ``amount``); without a field
        of the source, its one param (``param.party.name`` of a component
        is ``party.name``).
        """
        params = [r for r in self.reads if r == "param" or r.startswith("param.")]
        # The settings of the package are constants of the view, as its params are.
        fields = [r for r in self.reads if r not in params and r not in settings_reads([r])]
        if len(fields) == 1:
            return self.name(fields[0])
        if not fields and len(params) == 1 and params[0] != "param":
            name = params[0].removeprefix("param.")
            return name if _COLUMN_KEY.match(name) else None
        return None

    def default_label(self, name: str) -> str:
        prefix = f"{self.context.package}." if self.context.package else ""
        return f"{prefix}fields.{name}"

    # -- blocks --

    def columns(
        self,
        owner: PackageObject,
        items: Sequence[Mapping[str, Any]],
        at: str,
        *,
        title: str | None,
        shown_paths: bool = False,
    ) -> list[dict[str, Any]]:
        """The columns a display shows: ``{key, <title>, format?}``; ``title=None`` shows none."""
        out: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for index, column in enumerate(items):
            where = f"{at}/{index}"
            name = self.column(owner, column, where, shown=shown_paths)
            key = column.get("key") or name or str(index)
            if key in seen:
                self.add(
                    owner,
                    "duplicate_key",
                    where,
                    f"column {index} has the key {key!r} of column {seen[key]}",
                    hint="give one of them its own key",
                )
            seen.setdefault(key, index)
            shown: dict[str, Any] = {"key": key}
            label = column.get("label")
            if isinstance(label, str):
                # A label written is a key of the dictionaries, shown or not (a card shows none).
                text = self.say(owner, label, f"{where}/label", written=True)
                if title is not None:
                    shown[title] = text
            elif title is not None and name is not None:
                shown[title] = self.say(
                    owner, self.default_label(name), f"{where}/label", written=False
                )
            elif title is not None:
                self.add(
                    owner,
                    "missing_label",
                    where,
                    "the value reads no one field to name its label by",
                    hint="write its label: a key of the dictionaries",
                )
            if "format" in column:
                shown["format"] = column["format"]
            out.append(shown)
        return out

    def column(
        self, owner: PackageObject, column: Mapping[str, Any], at: str, *, shown: bool = False
    ) -> str | None:
        """Check a column; the name of the field it shows, if one."""
        # Without a format the console shows the value by its type: nothing to fit.
        format = column.get("format")
        has_field, has_value = "field" in column, "value" in column
        if has_field == has_value:
            self.add(owner, "invalid_column", at, "a column has exactly one of field or value")
            return None
        if has_field:
            ok = self.path(owner, column["field"], f"{at}/field", format=format, shown=shown)
            return self.name(column["field"]) if ok else None
        found = self.expression(owner, column["value"], f"{at}/value", format=format)
        return self.read_name() if found is not None else None

    def open(self, owner: PackageObject, target: Mapping[str, Any], at: str) -> dict[str, Any]:
        view = target["view"]
        if view not in self.context.views:
            self.add(
                owner,
                "unknown_view",
                f"{at}/view",
                f"there is no view {view!r} in the package or in a package it requires",
                hint="list the package of that view in requires of package.yaml",
            )
        if isinstance(target.get("id"), str):
            self.expression(owner, target["id"], f"{at}/id", wants="STRING")
        for name, text in sorted((target.get("params") or {}).items()):
            self.expression(owner, text, f"{at}/params/{name}")
        # The id of the record opened comes as the id of its row in the data of the view.
        return {"view": view}

    def aggregate(self, owner: PackageObject, text: str, at: str, format: str | None) -> None:
        """Check one aggregate of metrics or chart."""
        match = _AGGREGATE.match(text)
        if match is None:
            self.add(
                owner,
                "invalid_aggregate",
                at,
                "a value of metrics and chart is count(), count(<condition>), sum(<number>),"
                " avg(<number>), min(<value>) or max(<value>)",
            )
            return
        name, inner = match.group(1), match.group(2)
        if _calls_aggregate(inner):
            self.add(owner, "invalid_aggregate", at, "an aggregate does not hold another one")
            return
        if name == "count":
            output = "INT"
            if inner.strip():
                self.expression(owner, inner, at, wants="BOOL")
        else:
            found = self.expression(owner, inner, at)
            if found is None:
                return
            allowed = _NUMBERS if name in ("sum", "avg") else _NUMBERS | {"TIMESTAMP", "DURATION"}
            if found != "DYN" and found not in allowed:
                self.add(
                    owner,
                    "expression_type_error",
                    at,
                    f"{name} takes {', '.join(sorted(allowed))}, the expression gives {found}",
                )
                return
            output = "DOUBLE" if name == "avg" and found != "DYN" else found
        if format is not None:
            self.fits(owner, output, format, at)

    def block(self, owner: PackageObject, block: Mapping[str, Any], at: str) -> dict[str, Any]:
        """Check a block; what the display shows of it."""
        kind = block["block"]
        if kind in COLLECTION_BLOCKS and self.instance:
            self.add(
                owner,
                "block_source_mismatch",
                at,
                f"{kind} shows many records: the view shows one instance",
            )
        if kind in INSTANCE_BLOCKS and not self.instance:
            self.add(
                owner,
                "block_source_mismatch",
                at,
                f"{kind} shows one instance: the source has no instance: param.<name>",
            )
        if kind in PROCESS_BLOCKS and self.source != "process":
            self.add(owner, "block_source_mismatch", at, f"{kind} belongs to a process source")
        shown: dict[str, Any] = {"block": kind}
        if kind != "header" and isinstance(block.get("title"), str):
            shown["title"] = self.say(owner, block["title"], f"{at}/title", written=True)
        if kind in ("table", "list"):
            shown["columns"] = self.columns(
                owner, block["columns"], f"{at}/columns", title="title", shown_paths=True
            )
        if kind == "board":
            shown["card"] = self.card(owner, block["card"], f"{at}/card")
        if kind in ("table", "list", "board"):
            filters = [
                self.filter(owner, path, f"{at}/filters/{index}")
                for index, path in enumerate(block.get("filters") or ())
            ]
            if filters:
                shown["filters"] = filters
        if kind in ("table", "list"):
            orders = [
                self.sort(owner, order["field"], f"{at}/sort/{index}/field")
                for index, order in enumerate(block.get("sort") or ())
            ]
            if orders:
                shown["sort"] = orders
        if isinstance(block.get("open"), Mapping):
            shown["open"] = self.open(owner, block["open"], f"{at}/open")
        if kind == "header":
            for name in ("title", "status"):
                if isinstance(block.get(name), str):
                    self.path(owner, block[name], f"{at}/{name}")
            if "actions" in block:
                shown["actions"] = block["actions"]
        if kind == "fields":
            if isinstance(block.get("section"), str):
                shown["section"] = self.say(owner, block["section"], f"{at}/section", written=True)
            shown["items"] = self.columns(owner, block["items"], f"{at}/items", title="label")
        if kind == "metrics":
            shown["items"] = self.metrics(owner, block["items"], f"{at}/items")
        if kind == "chart":
            self.path(owner, block["groupBy"], f"{at}/groupBy")
            self.aggregate(owner, block["value"], f"{at}/value", block.get("format"))
            shown["type"] = block["chart"]
            if "title" not in shown and isinstance(block.get("label"), str):
                shown["title"] = self.say(owner, block["label"], f"{at}/label", written=True)
            if "format" in block:
                shown["format"] = block["format"]
        if kind == "related":
            # The kinds of an ontology live in the memory service: checked with the query (stage 6).
            key = block["knowledge"]["key"]
            self.expression(owner, key, f"{at}/knowledge/key", wants="STRING")
            relations = (block.get("include") or {}).get("relations")
            if isinstance(relations, list):
                shown["relations"] = list(relations)
        if kind == "invoke":
            self.invoke(owner, block, at)
            shown["label"] = self.say(owner, block["label"], f"{at}/label", written=True)
            shown.update({k: copy.deepcopy(block[k]) for k in ("skill", "input") if k in block})
        return shown

    def metrics(
        self, owner: PackageObject, items: Sequence[Mapping[str, Any]], at: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for index, item in enumerate(items):
            where = f"{at}/{index}"
            self.aggregate(owner, item["value"], f"{where}/value", item.get("format"))
            # A figure is named by its title: sample.board.open is open.
            word = item["title"].rsplit(".", 1)[-1]
            key = item.get("key") or (word if _COLUMN_KEY.match(word) else str(index))
            if key in seen:
                self.add(
                    owner,
                    "duplicate_key",
                    where,
                    f"item {index} has the key {key!r} of item {seen[key]}",
                    hint="give one of them its own key",
                )
            seen.setdefault(key, index)
            shown: dict[str, Any] = {
                "key": key,
                "title": self.say(owner, item["title"], f"{where}/title", written=True),
            }
            if "format" in item:
                shown["format"] = item["format"]
            out.append(shown)
        return out

    def card(self, owner: PackageObject, card: Mapping[str, Any], at: str) -> dict[str, Any]:
        for name in ("title", "subtitle", "badge"):
            if isinstance(card.get(name), str):
                self.path(owner, card[name], f"{at}/{name}")
        # The title, subtitle and badge come with each card in the data of the view.
        fields = self.columns(owner, card.get("fields") or (), f"{at}/fields", title=None)
        return {"fields": fields}

    def filter(self, owner: PackageObject, path: str, at: str) -> dict[str, Any]:
        """A field the data of the view is filtered by: its title, its type and its values."""
        ok = self.path(owner, path, at)
        name = self.name(path) or path
        shown: dict[str, Any] = {
            "field": name,
            "title": self.say(owner, self.default_label(name), at, written=False),
        }
        kind, values = self.filter_type(path) if ok else ("text", None)
        shown["type"] = kind
        if values is not None:
            shown["options"] = [
                {
                    "value": v,
                    "title": self.maybe(f"{self.default_label(name)}.{_word(v)}", _word(v)),
                }
                for v in values
            ]
        return shown

    def filter_type(self, path: str) -> tuple[str, list[Any] | None]:
        """``text | enum | number | date`` and the values of an enum, by the data schema."""
        if self.source == "tasks":
            return self.task_filter_type(path)
        if self.source == "knowledge":
            if path == "kind":
                return "enum", list(self.kinds)
            return ("date", None) if path in KNOWLEDGE_DATES else ("text", None)
        if path == "stage":
            return "enum", list(self.process.stages) if self.process else []
        if path == "status":
            return "enum", list(INSTANCE_STATUSES)
        if path == "slaState":
            return "enum", list(SLA_STATES)
        root, _, rest = path.partition(".")
        if root == "instance":
            node: Mapping[str, Any] | None = INSTANCE_SCHEMA["properties"].get(rest)
        elif root == "data" and self.process is not None:
            node = _schema_at(self.process.data, rest.split("."))
        else:
            node = None
        return _filter_type(node or {})

    def task_filter_type(self, path: str) -> tuple[str, list[Any] | None]:
        """Of a view of tasks: the statuses of the type, the categories, the priorities, dates."""
        root, _, rest = path.partition(".")
        if path == "fields.status":
            return "enum", [key for key, _ in self.task.statuses] if self.task else []
        if path == "fields.systemStatusCategory":
            return "enum", list(TASK_CATEGORIES)
        if path == "fields.priority":
            return "enum", list(TASK_PRIORITIES)
        if root == "fields":
            return _filter_type(TASK_FIELDS.get(rest) or {})
        if root == "customFields" and self.task is not None:
            return _filter_type(_schema_at(self.task.field_schema, rest.split(".")) or {})
        return "text", None

    def sort(self, owner: PackageObject, path: str, at: str) -> dict[str, Any]:
        self.path(owner, path, at)
        name = self.name(path) or path
        return {
            "field": name,
            "title": self.say(owner, self.default_label(name), at, written=False),
        }

    def invoke(self, owner: PackageObject, block: Mapping[str, Any], at: str) -> None:
        """A skill called with its input; the console shows its result by the output schema."""
        ref = block["skill"]
        given: Mapping[str, str] = block.get("input") or {}
        for name, text in sorted(given.items()):
            self.expression(owner, text, f"{at}/input/{name}")
        skill = self.context.skills.get(ref)
        if skill is None or skill.status == "disabled":
            state = "is disabled" if skill is not None else "is not in the package or the catalog"
            self.add(owner, "unknown_skill", f"{at}/skill", f"skill {ref!r} {state}")
            return
        if not _object_schema(skill.output_schema):
            self.add(
                owner,
                "skill_output_missing",
                f"{at}/skill",
                f"skill {ref!r} declares no output schema to show its result by",
                hint="declare contract.outputs (or outputSchema) of the skill",
            )
        inputs = skill.input_schema if _object_schema(skill.input_schema) else None
        if inputs is None:
            return
        declared = inputs.get("properties")
        if isinstance(declared, Mapping):
            for name in sorted(set(given) - set(declared)):
                self.add(
                    owner,
                    "unknown_skill_input",
                    f"{at}/input/{name}",
                    f"skill {ref!r} takes no input {name!r}",
                    hint=", ".join(sorted(declared)) or None,
                )
        missing = [n for n in inputs.get("required") or () if n not in given]
        if missing:
            self.add(
                owner,
                "skill_input_missing",
                f"{at}/input",
                f"skill {ref!r} requires {', '.join(missing)}",
            )

    def component(self, block: Mapping[str, Any], component: PackageObject, at: str) -> None:
        """What the view gives the params of ``component``, checked against their types."""
        params: Mapping[str, Any] = component.spec.get("params") or {}
        given: Mapping[str, str] = block.get("with") or {}
        for name, text in sorted(given.items()):
            where = f"{at}/with/{name}"
            if name not in params:
                self.add(
                    self.obj,
                    "unknown_param",
                    where,
                    f"component {component.key!r} declares no param {name!r}",
                    hint=", ".join(sorted(params)) or None,
                )
                self.expression(self.obj, text, where)
                continue
            found = self.expression(self.obj, text, where)
            declared = params[name]
            wanted, noun = _param_cel_types(declared)
            if found is not None and found != "DYN" and wanted and found not in wanted:
                self.add(
                    self.obj,
                    "expression_type_error",
                    where,
                    f"param {name} is {noun}: the expression gives {found}",
                )
        missing = [n for n, p in sorted(params.items()) if p.get("required") and n not in given]
        if missing:
            self.add(
                self.obj,
                "missing_param",
                f"{at}/with",
                f"component {component.key!r} requires {', '.join(missing)}",
            )

    def layout(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The blocks with each component inlined, and their display; every block checked."""
        out: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []
        for index, block in enumerate(self.spec["layout"]):
            at = f"/spec/layout/{index}"
            if block["block"] != "component":
                shown.append(self.block(self.obj, block, at))
                out.append(copy.deepcopy(block))
                continue
            key = block["component"]
            component = self.context.components.get(key)
            if component is None:
                if key not in self.context.component_keys:
                    self.add(
                        self.obj,
                        "unknown_component",
                        f"{at}/component",
                        f"the package has no component {key!r}",
                    )
                continue  # a component with findings of its own: the plan refuses it anyway
            self.component(block, component, at)
            # Inside the component ``param`` is its own params, given by ``with``.
            view_env, self.env = self.env, self._environment(component.spec.get("params") or {})
            inner, inner_shown = [], []
            for number, item in enumerate(component.spec["layout"]):
                inner_shown.append(self.block(component, item, f"/spec/layout/{number}"))
                inner.append(copy.deepcopy(item))
            self.env = view_env
            out.append({**copy.deepcopy(dict(block)), "layout": inner})
            shown.append({"block": "component", "component": key, "layout": inner_shown})
        return out, shown

    def display(self, layout: list[dict[str, Any]]) -> dict[str, Any]:
        """What a console draws: the view without its paths and expressions (CP-ADR-0080 §9)."""
        spec = self.spec
        shown: dict[str, Any] = {
            "title": self.say(self.obj, spec["title"], "/spec/title", written=True),
        }
        if isinstance(spec.get("description"), str):
            shown["description"] = self.say(
                self.obj, spec["description"], "/spec/description", written=True
            )
        nav = spec.get("nav")
        if isinstance(nav, Mapping):
            shown["nav"] = {
                "group": nav.get("group", DEFAULT_NAV_GROUP),
                **{k: nav[k] for k in ("icon", "order") if k in nav},
            }
        source = spec["source"]
        shown["source"] = {
            "kind": self.source,
            **({"process": source["process"]} if self.source == "process" else {}),
            "instance": self.instance,
        }
        shown["layout"] = layout
        return shown


def source_environment(
    source: str,
    process: ProcessShape | None,
    params: Mapping[str, Any],
    *,
    settings: Mapping[str, Any] | None = None,
    task: TaskShape | None = None,
) -> Environment:
    """The CEL environment of a view's expressions: its source and its ``params``.

    The check of a view and the query of its data (``POST /views/{key}:query``)
    compile in the same one. ``settings`` — the type of the variable
    ``settings`` (:attr:`SettingsScope.variable` of the view's package,
    CP-ADR-0081 §6); ``None`` — the view has none. ``task`` — of a view of
    tasks, the shape of its type: ``customFields`` is typed by its
    ``fieldSchema``. A record of knowledge is ``id``, ``kind``, ``key``,
    ``title``, ``validFrom``, ``validTo`` and ``attributes`` — a map: its
    kinds' schemas live in Memory.
    """
    param_schema = {
        "type": "object",
        "properties": {name: _param_schema(declared) for name, declared in sorted(params.items())},
    }
    bindings: dict[str, Any] = {"param": param_schema if params else {"type": "object"}}
    if source == "process":
        # What GET /process-instances filters by is what a view of instances reads.
        bindings.update(INSTANCE_FIELDS)
        return environment(
            data=dict(process.data) if process and process.data else None,
            stages=process.stages if process else (),
            bindings=bindings,
            settings=settings,
        )
    if source == "tasks":
        custom = task.field_schema if task is not None and task.field_schema else None
        bindings.update(
            {
                "id": INSTANCE_FIELDS["id"],
                "fields": {"type": "object", "properties": dict(TASK_FIELDS)},
                "customFields": dict(custom) if custom else {"type": "object"},
            }
        )
        return environment(bindings=bindings, settings=settings)
    if source == "knowledge":
        bindings.update(KNOWLEDGE_FIELDS)
        # A record may have no bounds of validity: a dyn value, null when there is none.
        bindings.update({name: None for name in KNOWLEDGE_DATES})
        bindings["attributes"] = {"type": "object"}
        return environment(bindings=bindings, settings=settings)
    bindings["id"] = INSTANCE_FIELDS["id"]
    return environment(bindings=bindings, settings=settings)


def _param_schema(declared: Mapping[str, Any]) -> dict[str, Any]:
    schema = declared.get("schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    return _PARAM_TYPES[declared["type"]]


def _param_cel_types(declared: Mapping[str, Any]) -> tuple[frozenset[str] | None, str]:
    """The CEL types a value given to a param may have (``None``: any), and how it is named."""
    if "type" in declared:
        return _PARAM_CEL_TYPES[declared["type"]], str(declared["type"])
    kind = _json_type(declared.get("schema") or {})
    return (_SCHEMA_CEL_TYPES.get(kind) if kind else None), f"{kind or 'any'} by its schema"


def _json_type(schema: Mapping[str, Any]) -> str | None:
    raw = schema.get("type")
    if isinstance(raw, list):
        types = [t for t in raw if t != "null"]
        raw = types[0] if len(types) == 1 else None
    if isinstance(raw, str):
        return raw
    return "object" if "properties" in schema else None


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _filter_type(node: Mapping[str, Any]) -> tuple[str, list[Any] | None]:
    enum = node.get("enum")
    if isinstance(enum, list):
        return "enum", [v for v in enum if isinstance(v, (str, int, float, bool))]
    kind = _json_type(node)
    if kind == "boolean":
        return "enum", [True, False]
    if kind in ("integer", "number"):
        return "number", None
    if kind == "string" and node.get("format") in ("date", "date-time"):
        return "date", None
    return "text", None


def _object_schema(schema: Any) -> bool:
    return isinstance(schema, Mapping) and schema.get("type") == "object"


def _base_type(output: str) -> str:
    """``LIST<STRING>`` is a list, a message is an object: what a format compares."""
    if output.startswith("LIST"):
        return "LIST"
    if output.startswith("MAP"):
        return "MAP"
    if "." in output:
        return "MESSAGE"
    return output


def _schema_at(schema: Mapping[str, Any] | None, parts: Sequence[str]) -> dict[str, Any] | None:
    """The JSON Schema ``parts`` (``case.amount``) is declared by in ``schema``, if it is."""
    root = schema or {}
    node: dict[str, Any] = _resolved(root, root)
    for part in parts:
        properties = _properties(root, node)
        if part not in properties or not isinstance(properties[part], Mapping):
            return None
        node = _resolved(root, properties[part])
    return node


def _resolved(root: Mapping[str, Any], node: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    """``node`` with its local ``$ref`` and ``allOf`` merged in: what its type is read from."""
    if depth > 20:
        return dict(node)
    found: dict[str, Any] = {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, Mapping) else None
        if isinstance(target, Mapping):
            found.update(_resolved(root, target, depth + 1))
    for branch in node.get("allOf") or ():
        if isinstance(branch, Mapping):
            found.update(_resolved(root, branch, depth + 1))
    found.update({k: v for k, v in node.items() if k not in ("$ref", "allOf")})
    return found


def _properties(root: Mapping[str, Any], node: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    """The properties ``node`` declares: its own, of a local ``$ref`` and of its combinators."""
    if depth > 20:
        return {}
    found: dict[str, Any] = {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, Mapping) else None
        if isinstance(target, Mapping):
            found.update(_properties(root, target, depth + 1))
    for combinator in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(combinator) or ():
            if isinstance(branch, Mapping):
                found.update(_properties(root, branch, depth + 1))
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        found.update(properties)
    return found


def check_view(obj: PackageObject, context: ViewContext) -> ViewCheck:
    """The findings of a view and, when it has none, the form a revision stores."""
    problems = [obj.place(p) for p in _no_code(obj)]
    spec = {k: v for k, v in obj.spec.items() if k != "code"}
    problems += [obj.place(p) for p in _shape_problems(spec, "viewSpec", "view")]
    if problems:
        return ViewCheck(problems, None)
    checker = _Checker(obj, context, spec)
    checker.check_source()
    layout, shown = checker.layout()
    display = checker.display(shown)
    expanded = {**copy.deepcopy(spec), "blocks": spec.get("blocks", BLOCK_SET), "layout": layout}
    audience = spec.get("audience")
    if isinstance(audience, Mapping):
        for index, role in enumerate(audience["roles"]):
            if role not in context.roles:
                checker.add(
                    obj,
                    "unknown_role",
                    f"/spec/audience/roles/{index}",
                    f"the organization has no role {role!r}, nor does the package",
                    hint="a role of the organization (its slug) or a Role of the package",
                )
    used = checker.required | checker.optional
    if checker.problems:
        return ViewCheck(checker.problems, None, used)
    form = {
        **expanded,
        "display": display,
        "messages": {
            locale: {
                key: context.dictionaries[locale][key]
                for key in sorted(used)
                if key in context.dictionaries.get(locale, {})
            }
            for locale in context.locales
        },
        "locales": list(context.locales),
        "defaultLocale": context.default_locale,
    }
    return ViewCheck([], form, used)


# --- what a reader of a view gets --------------------------------------------------------------


def choose_locale(form: Mapping[str, Any], wanted: str | None) -> str:
    """The locale of a view for ``wanted``: itself, its language, or the default locale."""
    locales: list[str] = list(form.get("locales") or ())
    default = str(form.get("defaultLocale") or (locales[0] if locales else ""))
    return resolve_locale(locales, default, wanted)


def resolve_locale(locales: Sequence[str], default: str, wanted: str | None) -> str:
    """One of ``locales`` for ``wanted``: itself (any case), its base language, or ``default``.

    The chain of CP-ADR-0080 §5, shared by everything the core says in a language.
    """
    if not wanted:
        return default
    if wanted in locales:
        return wanted
    lowered = {locale.lower(): locale for locale in locales}
    if wanted.lower() in lowered:
        return lowered[wanted.lower()]
    language = wanted.split("-", 1)[0].lower()
    return lowered.get(language, default)


def present(form: Mapping[str, Any], locale: str) -> dict[str, Any]:
    """The display of a view with every key of the dictionaries replaced by its text in ``locale``.

    A key the dictionary of ``locale`` lacks (a form stored by an earlier
    check, the title of a value of a filter) takes the text of the default
    locale, then what the display says to show otherwise, then the key itself.
    """
    messages: Mapping[str, Mapping[str, str]] = form.get("messages") or {}
    texts = messages.get(locale) or {}
    fallback = messages.get(str(form.get("defaultLocale") or "")) or {}

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            if TEXT in node and set(node) <= {TEXT, OTHERWISE}:
                key = str(node[TEXT])
                return texts.get(key, fallback.get(key, node.get(OTHERWISE, key)))
            return {name: walk(value) for name, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    shown: dict[str, Any] = walk(form.get("display") or {})
    return shown


# --- the ontology a view of knowledge reads (stage 6) ------------------------------------------

KNOWLEDGE_UNCHECKED = "knowledge_unchecked"


@dataclass(frozen=True)
class KnowledgeCatalog:
    """The kinds and relations of the ontology of a tree, as Memory has it for its namespace.

    ``kinds`` — a kind by name with the JSON Schema of its attributes (``None``:
    the kind declares none, any attribute goes); ``aliases`` — a synonym of a
    kind name to the kind.
    """

    kinds: Mapping[str, Mapping[str, Any] | None]
    aliases: Mapping[str, str] = field(default_factory=dict)
    relations: frozenset[str] = frozenset()

    def kind(self, name: str) -> str | None:
        found = self.aliases.get(name, name)
        return found if found in self.kinds else None


def knowledge_names(form: Mapping[str, Any]) -> bool:
    """Whether a view names the knowledge base: its source, or a ``related`` block."""
    if "knowledge" in (form.get("source") or {}):
        return True
    return any(node.get("block") == "related" for node in _nodes(form.get("layout")))


def _blocks(layout: Any, at: str) -> Iterator[tuple[Mapping[str, Any], str]]:
    """Every block of a layout with its pointer, the blocks of an inlined component too."""
    for index, block in enumerate(layout if isinstance(layout, list) else ()):
        if not isinstance(block, Mapping):
            continue
        where = f"{at}/{index}"
        yield block, where
        if block.get("block") == "component":
            yield from _blocks(block.get("layout"), f"{where}/layout")


def _source_reads(form: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """``(pointer, path)`` of every path of the record a view of knowledge reads."""
    env = source_environment("knowledge", None, form.get("params") or {})

    def reads(text: Any, where: str) -> Iterator[tuple[str, str]]:
        if not isinstance(text, str):
            return
        parsed = split_aggregate(text)
        inner = parsed[1] if parsed is not None else text
        if not inner.strip():
            return
        try:
            program = env.compile(inner)
        except ExpressionError:
            return
        for read in program.reads:
            yield where, read

    for block, at in _blocks(form.get("layout"), "/spec/layout"):
        for index, column in enumerate(block.get("columns") or ()):
            if isinstance(column, Mapping):
                if isinstance(column.get("field"), str):
                    yield f"{at}/columns/{index}/field", column["field"]
                yield from reads(column.get("value"), f"{at}/columns/{index}/value")
        for index, path in enumerate(block.get("filters") or ()):
            yield f"{at}/filters/{index}", str(path)
        for index, order in enumerate(block.get("sort") or ()):
            if isinstance(order, Mapping):
                yield f"{at}/sort/{index}/field", str(order.get("field"))
        if isinstance(block.get("groupBy"), str):
            yield f"{at}/groupBy", block["groupBy"]
        if block.get("block") in ("metrics", "chart"):
            for index, item in enumerate(block.get("items") or ()):
                if isinstance(item, Mapping):
                    yield from reads(item.get("value"), f"{at}/items/{index}/value")
            yield from reads(block.get("value"), f"{at}/value")
        target = block.get("open")
        if isinstance(target, Mapping):
            yield from reads(target.get("id"), f"{at}/open/id")


def check_knowledge(
    obj: PackageObject, form: Mapping[str, Any], catalog: KnowledgeCatalog
) -> list[Problem]:
    """Warnings: kinds, attributes and relations a view names that the ontology lacks.

    Warnings, not errors: an ontology belongs to a tree of workspaces and a
    package to the tenant — the tree the plan is asked for may not be the one
    a person reads the view in (CP-ADR-0080, amendment Б3).
    """
    problems: list[Problem] = []

    def warn(code: str, where: str, message: str, hint: str | None = None) -> None:
        problems.append(obj.place(Problem(code, "warning", where, message, hint)))

    known = ", ".join(sorted(catalog.kinds)[:20]) or None
    source = (form.get("source") or {}).get("knowledge")
    kinds: list[str] = []
    if isinstance(source, Mapping):
        for index, name in enumerate(source.get("kinds") or ()):
            found = catalog.kind(str(name))
            if found is None:
                warn(
                    "unknown_kind",
                    f"/spec/source/knowledge/kinds/{index}",
                    f"the ontology of the tree has no kind {name!r}",
                    hint=known,
                )
            else:
                kinds.append(found)
        schemas = [catalog.kinds[k] for k in kinds]
        for where, path in _source_reads(form):
            root, _, rest = path.partition(".")
            if root == "attributes" and rest and schemas and all(s is not None for s in schemas):
                steps = rest.split(".")
                if all(_schema_at(schema, steps) is None for schema in schemas):
                    warn(
                        "undeclared_path",
                        where,
                        f"{path} is not an attribute of {', '.join(kinds)} in the ontology",
                    )
            elif root == "relations" and rest and rest not in catalog.relations:
                warn(
                    "unknown_relation",
                    where,
                    f"the ontology of the tree has no relation {rest!r}",
                )
    for block, at in _blocks(form.get("layout"), "/spec/layout"):
        if block.get("block") != "related":
            continue
        kind = (block.get("knowledge") or {}).get("kind")
        if isinstance(kind, str) and catalog.kind(kind) is None:
            warn(
                "unknown_kind",
                f"{at}/knowledge/kind",
                f"the ontology of the tree has no kind {kind!r}",
                hint=known,
            )
        names = (block.get("include") or {}).get("relations")
        for index, name in enumerate(names if isinstance(names, list) else ()):
            if name not in catalog.relations:
                warn(
                    "unknown_relation",
                    f"{at}/include/relations/{index}",
                    f"the ontology of the tree has no relation {name!r}",
                )
    return problems
