"""The CEL expression profile ``cp/1`` (CP-ADR-0075).

Every expression of the process language is CEL (Common Expression Language)
evaluated in one profile: the environment (variables and their types), the
functions on top of the standard, what is forbidden and how much an
evaluation may cost. The implementation is ``cel-expr-python`` — Google's
bindings to cel-cpp — with the pieces the bindings do not offer built here:

- **Types from JSON Schema.** The variables ``data``, ``event``, ``step``,
  ``task``, ``stage`` and ``instance`` are protobuf messages generated from
  JSON Schema (:func:`environment`), so the cel-cpp checker refuses a field an
  object does not declare, and does so when the expression is compiled.
  Scalars are protobuf wrappers: an absent or null value reads ``null``, as in
  JSON, and is an error once used. ``format: date-time`` is a timestamp,
  ``format: duration`` a duration; an object without ``properties`` is
  ``map(string, dyn)``; what cannot be typed is ``dyn``.
- **Functions.** ``cal.addWorkdays``, ``cal.isWorkday``,
  ``cal.workdaysBetween``, ``cal.addWorkingTime``, ``cal.workingTimeBetween``
  over ``domain/calendar.py`` (CP-ADR-0078 §2); the ``strings``,
  ``optional`` and ``bindings`` extensions of cel-cpp; the list functions
  ``slice``, ``flatten``, ``sort``, ``distinct`` (cel-cpp does not bind its
  ``lists`` extension to Python; ``reverse`` is the strings one). There is no ``now()``: time enters
  only as ``event.time`` and ``instance.clock``.
- **ISO 8601 durations.** ``duration("P3D")`` is accepted: a literal in that
  form is rewritten to the CEL form before compilation (cel-cpp does not let
  the standard overload be replaced).
- **The checked tree.** ``Expression.serialize()`` returns the checked
  expression (``cel.expr.CheckedExpr``); it is decoded here to list the fields
  an expression reads, to bound its nesting and to estimate its cost.
- **Cost.** Before an evaluation its cost is bounded from the checked tree
  and the actual sizes of the inputs (steps, sizes of strings and lists,
  iterations of comprehensions); above the limit the evaluation does not run
  and fails with ``expression_cost_exceeded``. The bound is deterministic: the
  same expression on the same inputs costs the same in a live run, a test and
  a replay.

The translation of the four earlier syntaxes into CEL is at the end of the
module (:func:`translate_condition`, :func:`translate_path`,
:func:`translate_execution_input`, :func:`translate_context_source`,
:func:`translate_when`, :func:`legacy_expressions`).

Pure functions over plain values; no I/O.
"""

import contextvars
import difflib
import hashlib
import json
import math
import re
import struct
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cel_expr_python import cel
from cel_expr_python.ext import ext_bindings, ext_optional, ext_strings
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    duration_pb2,
    json_format,
    message_factory,
    struct_pb2,
    timestamp_pb2,
    wrappers_pb2,
)
from google.protobuf.message import Message

from control_plane.domain.calendar import Calendar, CalendarError
from control_plane.domain.errors import ValidationError

PROFILE = "cp/1"
DEFAULT_COST_LIMIT = 10_000
MAX_EXPRESSION_LENGTH = 4000
# Depth of the checked tree and of nested comprehensions (macros such as
# ``all``/``map``; ``cel.bind`` does not iterate and is not counted).
MAX_NESTING = 32
MAX_COMPREHENSION_NESTING = 3

EXPRESSION_SYNTAX_ERROR = "expression_syntax_error"
EXPRESSION_TYPE_ERROR = "expression_type_error"
EXPRESSION_TOO_COMPLEX = "expression_too_complex"
EXPRESSION_ERROR = "expression_error"
EXPRESSION_COST_EXCEEDED = "expression_cost_exceeded"

VARIABLES = ("data", "event", "step", "task", "stage", "instance")


class ExpressionError(ValidationError):
    """An expression that does not compile or could not be evaluated.

    ``line`` and ``column`` (1-based) point into the expression; ``path`` is
    where the expression sits in its document (a JSON pointer), given by the
    caller. :meth:`finding` is the shape of a publication check finding.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        expression: str = "",
        line: int | None = None,
        column: int | None = None,
        path: str | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        position = f" at {line}:{column}" if line is not None else ""
        super().__init__(
            code,
            f"{message}{position}",
            details={
                **(details or {}),
                **({"path": path} if path is not None else {}),
                **({"line": line, "column": column} if line is not None else {}),
                **({"hint": hint} if hint else {}),
            },
        )
        self.reason = message
        self.expression = expression
        self.line = line
        self.column = column
        self.path = path
        self.hint = hint

    def at(self, path: str) -> "ExpressionError":
        """The same error, placed at ``path`` in its document."""
        return ExpressionError(
            self.code,
            self.reason,
            expression=self.expression,
            line=self.line,
            column=self.column,
            path=path,
            hint=self.hint,
        )

    def finding(self) -> dict[str, Any]:
        found: dict[str, Any] = {"code": self.code, "severity": "error", "message": self.message}
        if self.path is not None:
            found["path"] = self.path
        if self.hint:
            found["hint"] = self.hint
        return found


# --- JSON Schema -> protobuf messages ----------------------------------------------

JsonSchema = Mapping[str, Any]
_FIELD = descriptor_pb2.FieldDescriptorProto
_WELL_KNOWN = (timestamp_pb2, duration_pb2, wrappers_pb2, struct_pb2)
_TIMESTAMP = ".google.protobuf.Timestamp"
_DURATION = ".google.protobuf.Duration"
_VALUE = ".google.protobuf.Value"
_STRUCT = ".google.protobuf.Struct"
_WRAPPERS = {
    "string": ".google.protobuf.StringValue",
    "integer": ".google.protobuf.Int64Value",
    "number": ".google.protobuf.DoubleValue",
    "boolean": ".google.protobuf.BoolValue",
}
_REPEATED_SCALARS = {
    "string": _FIELD.TYPE_STRING,
    "integer": _FIELD.TYPE_INT64,
    "number": _FIELD.TYPE_DOUBLE,
    "boolean": _FIELD.TYPE_BOOL,
}
# A property becomes a message field only under a name CEL can select.
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CEL_RESERVED = frozenset(
    {
        "as",
        "break",
        "const",
        "continue",
        "else",
        "false",
        "for",
        "function",
        "if",
        "import",
        "in",
        "let",
        "loop",
        "package",
        "namespace",
        "null",
        "return",
        "true",
        "var",
        "void",
        "while",
    }
)


@dataclass(frozen=True)
class _Shape:
    """How a JSON value of one schema is laid out in its protobuf field."""

    # "message", "map", "timestamp", "duration", "wrapper", "scalar", "value", "struct"
    kind: str
    message: str | None = None
    fields: Mapping[str, "_Shape"] = field(default_factory=dict)
    repeated: bool = False
    element: "_Shape | None" = None


def _single_type(schema: JsonSchema) -> str | None:
    """The one JSON type of a schema, ``null`` allowed beside it."""
    raw = schema.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        types = [t for t in raw if t != "null"]
        return types[0] if len(types) == 1 else None
    if "enum" in schema:
        values = [v for v in schema["enum"] if v is not None]
        kinds = {type(v) for v in values}
        if kinds == {str}:
            return "string"
    if "properties" in schema:
        return "object"
    return None


def _typed_object(schema: JsonSchema) -> bool:
    properties = schema.get("properties")
    return (
        isinstance(properties, Mapping)
        and bool(properties)
        and all(
            isinstance(name, str) and _FIELD_NAME.match(name) and name not in _CEL_RESERVED
            for name in properties
        )
    )


def typed_leaf(schema: JsonSchema | None, segments: Sequence[str]) -> str | None:
    """The JSON type a path of ``schema`` is read as when every step is a typed field.

    ``string`` (not a timestamp or a duration), ``number``, ``integer``,
    ``boolean`` or ``object`` (a message); ``None`` when a step is no field of
    a message (a map, a list, an untyped value) — a path the environment types
    otherwise than its JSON value, as :class:`_Builder` lays it out.
    """
    node: Any = schema or {}
    for segment in segments:
        if not (isinstance(node, Mapping) and _single_type(node) == "object"):
            return None
        if not _typed_object(node) or segment not in node["properties"]:
            return None
        node = node["properties"][segment]
    if not isinstance(node, Mapping):
        return None
    kind = _single_type(node)
    if kind == "string" and node.get("format") in ("date-time", "duration"):
        return None
    if kind == "object":
        return "object" if _typed_object(node) else None
    return kind if kind in ("string", "number", "integer", "boolean") else None


def _map_of_messages(schema: JsonSchema) -> JsonSchema | None:
    extra = schema.get("additionalProperties")
    if "properties" not in schema and isinstance(extra, Mapping) and _typed_object(extra):
        return extra
    return None


class _Builder:
    """Protobuf messages for the variables of one environment."""

    def __init__(self, package: str) -> None:
        self.package = package
        self.file = descriptor_pb2.FileDescriptorProto(
            name=f"{package.replace('.', '/')}.proto",
            package=package,
            syntax="proto3",
            dependency=[m.DESCRIPTOR.name for m in _WELL_KNOWN],
        )

    def message(self, name: str, schema: JsonSchema) -> _Shape:
        proto = self.file.message_type.add(name=name)
        return self._fill(proto, f".{self.package}.{name}", schema)

    def _fill(self, proto: Any, full_name: str, schema: JsonSchema) -> _Shape:
        fields: dict[str, _Shape] = {}
        required = set(schema.get("required") or ())
        for number, (name, sub) in enumerate(schema["properties"].items(), start=1):
            fields[name] = self._field(proto, full_name, name, number, sub, name in required)
        return _Shape("message", message=full_name.lstrip("."), fields=fields)

    def _nested(self, proto: Any, full_name: str, name: str, schema: JsonSchema) -> _Shape:
        nested = proto.nested_type.add(name=name)
        return self._fill(nested, f"{full_name}.{name}", schema)

    def _field(
        self, proto: Any, full_name: str, name: str, number: int, schema: Any, required: bool
    ) -> _Shape:
        schema = schema if isinstance(schema, Mapping) else {}
        kind = _single_type(schema)
        added = proto.field.add(
            name=name, number=number, json_name=name, label=_FIELD.LABEL_OPTIONAL
        )
        nullable = isinstance(schema.get("type"), list) and "null" in schema["type"]
        if required and not nullable and kind in _REPEATED_SCALARS:
            # Always present: a plain field, its zero value when unset.
            added.type = _REPEATED_SCALARS[kind]
            return _Shape("scalar")
        if kind == "array":
            added.label = _FIELD.LABEL_REPEATED
            items = schema.get("items") if isinstance(schema.get("items"), Mapping) else {}
            item_kind = _single_type(items)
            element = self._element(proto, full_name, name, items, item_kind, added)
            return _Shape(
                element.kind,
                message=element.message,
                fields=element.fields,
                repeated=True,
                element=element,
            )
        return self._element(proto, full_name, name, schema, kind, added)

    def _element(
        self,
        proto: Any,
        full_name: str,
        name: str,
        schema: JsonSchema,
        kind: str | None,
        added: Any,
    ) -> _Shape:
        repeated = added.label == _FIELD.LABEL_REPEATED
        fmt = schema.get("format")
        if kind == "string" and fmt == "date-time":
            added.type, added.type_name = _FIELD.TYPE_MESSAGE, _TIMESTAMP
            return _Shape("timestamp")
        if kind == "string" and fmt == "duration":
            added.type, added.type_name = _FIELD.TYPE_MESSAGE, _DURATION
            return _Shape("duration")
        if kind in _WRAPPERS:
            if repeated:
                added.type = _REPEATED_SCALARS[kind]
            else:
                added.type, added.type_name = _FIELD.TYPE_MESSAGE, _WRAPPERS[kind]
            return _Shape("wrapper")
        if kind == "object" and _typed_object(schema):
            nested = f"T_{name}"
            added.type, added.type_name = _FIELD.TYPE_MESSAGE, f"{full_name}.{nested}"
            return self._nested(proto, full_name, nested, schema)
        values = _map_of_messages(schema) if kind == "object" and not repeated else None
        if values is not None:
            entry_name = f"{name[0].upper()}{name[1:]}Entry"
            entry = proto.nested_type.add(name=entry_name)
            entry.options.map_entry = True
            entry.field.add(
                name="key", number=1, type=_FIELD.TYPE_STRING, label=_FIELD.LABEL_OPTIONAL
            )
            value = entry.field.add(
                name="value", number=2, type=_FIELD.TYPE_MESSAGE, label=_FIELD.LABEL_OPTIONAL
            )
            nested = f"T_{name}"
            value.type_name = f"{full_name}.{nested}"
            element = self._nested(proto, full_name, nested, values)
            added.label = _FIELD.LABEL_REPEATED
            added.type, added.type_name = _FIELD.TYPE_MESSAGE, f"{full_name}.{entry_name}"
            return _Shape("map", element=element)
        if kind == "object":
            added.type, added.type_name = _FIELD.TYPE_MESSAGE, _STRUCT
            return _Shape("struct")
        added.type, added.type_name = _FIELD.TYPE_MESSAGE, _VALUE
        return _Shape("value")


# --- the builtin variables ------------------------------------------------------

_STR = {"type": ["string", "null"]}
_INT = {"type": ["integer", "null"]}
_TIME = {"type": ["string", "null"], "format": "date-time"}
_ANY: JsonSchema = {}

ARTIFACT_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "id": _STR,
        "type": _STR,
        "name": _STR,
        "metadata": {"type": "object"},
    },
}


def task_schema(custom_fields: JsonSchema | None = None) -> JsonSchema:
    """``task``: the fields of ``TaskOut`` an expression may read (CP-ADR-0075 §2).

    ``artifacts`` is the newest artifact of each type on the task, by type key
    (``task.artifacts["commit"].metadata.sha``).
    """
    return {
        "type": "object",
        "properties": {
            "id": _STR,
            "publicId": _STR,
            "typeKey": _STR,
            "typeVersion": _INT,
            "title": _STR,
            "description": _STR,
            "status": _STR,
            "systemStatusCategory": _STR,
            "priority": _STR,
            "ownerId": _STR,
            "assigneeId": _STR,
            "workspaceId": _STR,
            "goalId": _STR,
            "customFields": custom_fields or {"type": "object"},
            "startDate": _TIME,
            "dueDate": _TIME,
            "createdAt": _TIME,
            "completedAt": _TIME,
            "artifacts": {"type": "object", "additionalProperties": ARTIFACT_SCHEMA},
            "verification": {"type": "object"},
        },
    }


def event_schema(payload: JsonSchema | None = None) -> JsonSchema:
    return {
        "type": "object",
        "properties": {
            "id": _STR,
            "type": _STR,
            "time": _TIME,
            "entityType": _STR,
            "entityId": _STR,
            "actorId": _STR,
            "correlationId": _STR,
            # A payload is always an object: without a schema, map(string, dyn).
            "payload": payload or {"type": "object"},
        },
    }


def step_schema(result: JsonSchema | None = None) -> JsonSchema:
    return {
        "type": "object",
        "properties": {
            "id": _STR,
            "skill": _STR,
            "status": _STR,
            "result": result or {"type": "object"},
            "error": {"type": "object", "properties": {"code": _STR, "message": _STR}},
        },
    }


INSTANCE_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "id": _STR,
        "key": _STR,
        "version": _INT,
        "startedAt": _TIME,
        "clock": _TIME,
    },
}
_STAGE_STATE: JsonSchema = {
    "type": "object",
    "properties": {"completed": {"type": "boolean"}, "active": {"type": "boolean"}},
    "required": ["completed", "active"],
}


def stage_schema(stages: Sequence[str]) -> JsonSchema:
    """``stage.<id>.completed`` / ``.active``; ``stage["<id>"]`` for ids CEL cannot select."""
    if stages and all(_FIELD_NAME.match(s) and s not in _CEL_RESERVED for s in stages):
        return {"type": "object", "properties": {s: _STAGE_STATE for s in stages}}
    return {"type": "object", "additionalProperties": _STAGE_STATE}


# --- functions --------------------------------------------------------------------


@dataclass
class _Scope:
    """What one evaluation's functions may see, and what they left behind."""

    calendars: Mapping[str, Calendar]
    default_calendar: str | None
    provisional: bool = False
    failure: ExpressionError | None = None


_SCOPE: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar("cel_scope", default=None)


def _calendar(key: str | None) -> Calendar:
    scope = _SCOPE.get()
    if scope is None:
        raise RuntimeError("cal.* outside an evaluation")
    name = key if key is not None else scope.default_calendar
    if name is None or name not in scope.calendars:
        raise _fail(
            ExpressionError(
                EXPRESSION_ERROR,
                f"calendar {name!r} is not available to this evaluation",
                details={"reason": "calendar_missing", "calendar": name},
            )
        )
    return scope.calendars[name]


def _fail(error: ExpressionError) -> ExpressionError:
    scope = _SCOPE.get()
    if scope is not None and scope.failure is None:
        scope.failure = error
    return error


def _mark(provisional: bool) -> None:
    scope = _SCOPE.get()
    if scope is not None and provisional:
        scope.provisional = True


def _calendar_call(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except CalendarError as exc:
        raise _fail(
            ExpressionError(EXPRESSION_ERROR, exc.message, details={"reason": exc.code})
        ) from exc


def _add_workdays(ts: datetime, n: int, key: str | None = None) -> datetime:
    def call() -> datetime:
        answer = _calendar(key).add_workdays_at(ts, n)
        _mark(answer.provisional)
        return answer.value

    result: datetime = _calendar_call(call)
    return result


def _is_workday(ts: datetime, key: str | None = None) -> bool:
    def call() -> bool:
        answer = _calendar(key).is_workday_at(ts)
        _mark(answer.provisional)
        return answer.value

    result: bool = _calendar_call(call)
    return result


def _workdays_between(a: datetime, b: datetime, key: str | None = None) -> int:
    def call() -> int:
        answer = _calendar(key).workdays_between_at(a, b)
        _mark(answer.provisional)
        return answer.value

    result: int = _calendar_call(call)
    return result


def _add_working_time(ts: datetime, amount: timedelta, key: str | None = None) -> datetime:
    def call() -> datetime:
        answer = _calendar(key).add_working_time_at(ts, amount)
        _mark(answer.provisional)
        return answer.value

    result: datetime = _calendar_call(call)
    return result


def _working_time_between(a: datetime, b: datetime, key: str | None = None) -> timedelta:
    def call() -> timedelta:
        answer = _calendar(key).working_time_between_at(a, b)
        _mark(answer.provisional)
        return answer.value

    result: timedelta = _calendar_call(call)
    return result


# A number in decimal notation, as amounts are kept in data: "1500000.00", "-3", "1e3".
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _decimal(value: Any) -> float:
    """``decimal(x)``: the number a string of decimal notation (or a number) holds, a double."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    number = float(text) if _DECIMAL.match(text) else math.nan
    if not math.isfinite(number):
        raise _fail(
            ExpressionError(
                EXPRESSION_ERROR,
                f"decimal: {text[:50]!r} is not a number in decimal notation",
                details={"reason": "invalid_decimal"},
            )
        )
    return number


def _flatten(items: list[Any]) -> list[Any]:
    flat: list[Any] = []
    for item in items:
        flat.extend(item if isinstance(item, list) else [item])
    return flat


def _distinct(items: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for item in items:
        if not any(type(item) is type(s) and item == s for s in seen):
            seen.append(item)
    return seen


def _sort(items: list[Any]) -> list[Any]:
    kinds = {type(item) for item in items}
    if len(kinds) > 1 and not kinds <= {int, float}:
        raise ValueError("sort() needs elements of one comparable type")
    return sorted(items)


def _slice(items: list[Any], start: int, end: int) -> list[Any]:
    if not 0 <= start <= end <= len(items):
        raise ValueError(f"slice({start}, {end}) is outside a list of {len(items)}")
    return items[start:end]


_T, _I, _B, _S = cel.Type.TIMESTAMP, cel.Type.INT, cel.Type.BOOL, cel.Type.STRING
_D, _DOUBLE = cel.Type.DURATION, cel.Type.DOUBLE
_LIST = cel.Type.List(cel.Type.DYN)


def _functions(with_default_calendar: bool) -> list[Any]:
    add = [cel.Overload("cal_add_workdays_key", _T, [_T, _I, _S], impl=_add_workdays)]
    is_day = [cel.Overload("cal_is_workday_key", _B, [_T, _S], impl=_is_workday)]
    between = [cel.Overload("cal_workdays_between_key", _I, [_T, _T, _S], impl=_workdays_between)]
    add_time = [cel.Overload("cal_add_working_time_key", _T, [_T, _D, _S], impl=_add_working_time)]
    time_between = [
        cel.Overload("cal_working_time_between_key", _D, [_T, _T, _S], impl=_working_time_between)
    ]
    if with_default_calendar:
        add.append(cel.Overload("cal_add_workdays", _T, [_T, _I], impl=_add_workdays))
        is_day.append(cel.Overload("cal_is_workday", _B, [_T], impl=_is_workday))
        between.append(cel.Overload("cal_workdays_between", _I, [_T, _T], impl=_workdays_between))
        add_time.append(cel.Overload("cal_add_working_time", _T, [_T, _D], impl=_add_working_time))
        time_between.append(
            cel.Overload("cal_working_time_between", _D, [_T, _T], impl=_working_time_between)
        )
    member = {"is_member": True}
    return [
        cel.FunctionDecl("cal.addWorkdays", add),
        cel.FunctionDecl("cal.isWorkday", is_day),
        cel.FunctionDecl("cal.workdaysBetween", between),
        cel.FunctionDecl("cal.addWorkingTime", add_time),
        cel.FunctionDecl("cal.workingTimeBetween", time_between),
        cel.FunctionDecl(
            "decimal",
            [
                cel.Overload("decimal_string", _DOUBLE, [_S], impl=_decimal),
                cel.Overload("decimal_int", _DOUBLE, [_I], impl=_decimal),
                cel.Overload("decimal_double", _DOUBLE, [_DOUBLE], impl=_decimal),
            ],
        ),
        cel.FunctionDecl(
            "slice", [cel.Overload("list_slice", _LIST, [_LIST, _I, _I], impl=_slice, **member)]
        ),
        cel.FunctionDecl(
            "flatten", [cel.Overload("list_flatten", _LIST, [_LIST], impl=_flatten, **member)]
        ),
        cel.FunctionDecl("sort", [cel.Overload("list_sort", _LIST, [_LIST], impl=_sort, **member)]),
        cel.FunctionDecl(
            "distinct", [cel.Overload("list_distinct", _LIST, [_LIST], impl=_distinct, **member)]
        ),
    ]


# --- ISO 8601 durations -----------------------------------------------------------

_ISO_DURATION = re.compile(
    r"^(?P<sign>-)?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d{1,9})?)S)?)?$"
)
_DURATION_LITERAL = re.compile(
    r"""duration\(\s*(?P<quote>["'])(?P<text>-?P[^"'\\]*)(?P=quote)\s*\)"""
)


def parse_iso_duration(text: str) -> timedelta | None:
    """``P3D``, ``PT4H30M``, ``-P1W``; years and months have no fixed length: ``None``."""
    match = _ISO_DURATION.match(text)
    if match is None or text.rstrip("-") in {"P", "PT"} or text.endswith("T"):
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v and k != "sign"}
    value = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -value if match.group("sign") else value


def _cel_duration(value: timedelta) -> str:
    micro = value // timedelta(microseconds=1)
    seconds, rest = divmod(abs(micro), 1_000_000)
    sign = "-" if micro < 0 else ""
    return f"{sign}{seconds}.{rest:06d}s" if rest else f"{sign}{seconds}s"


@dataclass(frozen=True)
class _Rewrite:
    source: str
    # (offset in the rewritten text, shift to apply to positions past it)
    shifts: tuple[tuple[int, int], ...]

    def original(self, offset: int) -> int:
        shift = 0
        for start, delta in self.shifts:
            if offset >= start:
                shift = delta
        return max(0, offset - shift)


def _rewrite_durations(source: str) -> _Rewrite:
    """ISO 8601 literals of ``duration()`` in the CEL form, positions kept mappable."""
    out: list[str] = []
    shifts: list[tuple[int, int]] = []
    last, delta = 0, 0
    for match in _DURATION_LITERAL.finditer(source):
        text = match.group("text")
        value = parse_iso_duration(text)
        if value is None:
            line, column = _line_column(source, match.start("text"))
            raise ExpressionError(
                EXPRESSION_TYPE_ERROR,
                f"{text!r} is not an ISO 8601 duration of weeks, days, hours, minutes, seconds",
                expression=source,
                line=line,
                column=column,
            )
        out.append(source[last : match.start("text")])
        replacement = _cel_duration(value)
        out.append(replacement)
        delta += len(replacement) - len(text)
        last = match.end("text")
        shifts.append((last + delta, delta))
    out.append(source[last:])
    return _Rewrite("".join(out), tuple(shifts))


def _line_column(source: str, offset: int) -> tuple[int, int]:
    before = source[:offset]
    line = before.count("\n") + 1
    return line, offset - (before.rfind("\n") + 1) + 1


# --- the checked tree (cel.expr.CheckedExpr, decoded from the wire) ----------------------


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _wire(buf: bytes) -> Iterator[tuple[int, Any]]:
    """``(field number, value)`` of one protobuf message: ints, bytes or fixed64."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, kind = key >> 3, key & 7
        value: int | bytes
        if kind == 0:
            value, pos = _varint(buf, pos)
        elif kind == 1:
            value, pos = buf[pos : pos + 8], pos + 8
        elif kind == 2:
            size, pos = _varint(buf, pos)
            value, pos = buf[pos : pos + size], pos + size
        elif kind == 5:
            value, pos = buf[pos : pos + 4], pos + 4
        else:  # groups are not used by cel.expr
            raise ValueError(f"unexpected wire type {kind}")
        yield number, value


def _signed(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


@dataclass
class Node:
    """One node of a checked expression."""

    id: int
    kind: str  # const, ident, select, call, list, struct, comprehension
    value: Any = None  # const: the value; ident: the name; select: the field
    function: str = ""
    test_only: bool = False
    target: "Node | None" = None
    args: list["Node"] = field(default_factory=list)
    entries: list[tuple[Any, "Node"]] = field(default_factory=list)
    iter_var: str = ""
    accu_var: str = ""


def _constant(buf: bytes) -> Any:
    for number, value in _wire(buf):
        if number == 1:
            return None
        if number == 2:
            return bool(value)
        if number in (3, 4):
            return _signed(value) if number == 3 else value
        if number == 5:
            return float(struct.unpack("<d", value)[0])
        if number == 6:
            return value.decode("utf-8")
        if number == 7:
            return bytes(value)
    return None


def _expr(buf: bytes) -> Node:
    node = Node(id=0, kind="const")
    for number, value in _wire(buf):
        if number == 2:
            node.id = value
        elif number == 3:
            node.kind, node.value = "const", _constant(value)
        elif number == 4:
            node.kind = "ident"
            node.value = next((v.decode() for n, v in _wire(value) if n == 1), "")
        elif number == 5:
            node.kind = "select"
            for n, v in _wire(value):
                if n == 1:
                    node.target = _expr(v)
                elif n == 2:
                    node.value = v.decode()
                elif n == 3:
                    node.test_only = bool(v)
        elif number == 6:
            node.kind = "call"
            for n, v in _wire(value):
                if n == 1:
                    node.target = _expr(v)
                elif n == 2:
                    node.function = v.decode()
                elif n == 3:
                    node.args.append(_expr(v))
        elif number == 7:
            node.kind = "list"
            node.args = [_expr(v) for n, v in _wire(value) if n == 1]
        elif number == 8:
            node.kind = "struct"
            node.value = next((v.decode() for n, v in _wire(value) if n == 1), "")
            for n, v in _wire(value):
                if n != 2:
                    continue
                key: Any = None
                entry_value = Node(id=0, kind="const")
                for en, ev in _wire(v):
                    if en == 2:
                        key = ev.decode()
                    elif en == 3:
                        key = _expr(ev)
                    elif en == 4:
                        entry_value = _expr(ev)
                node.entries.append((key, entry_value))
        elif number == 9:
            node.kind = "comprehension"
            parts: dict[int, Any] = {}
            for n, v in _wire(value):
                parts[n] = v
            node.iter_var = parts.get(1, b"").decode()
            node.accu_var = parts.get(3, b"").decode()
            # args: range, init, condition, step, result
            node.args = [_expr(parts.get(i, b"")) for i in (2, 4, 5, 6, 7)]
    return node


@dataclass(frozen=True)
class _Checked:
    root: Node
    positions: Mapping[int, int]


def _checked(serialized: bytes) -> _Checked:
    # google.protobuf.Any{type_url=1, value=2} around cel.expr.CheckedExpr.
    body = next(v for n, v in _wire(serialized) if n == 2)
    root: Node | None = None
    positions: dict[int, int] = {}
    for number, value in _wire(body):
        if number == 4:
            root = _expr(value)
        elif number == 5:
            for n, v in _wire(value):
                if n == 4:
                    entry = dict(_wire(v))
                    positions[_signed(entry.get(1, 0))] = entry.get(2, 0)
    if root is None:
        raise ValueError("the checked expression has no tree")
    return _Checked(root, positions)


# --- reads ------------------------------------------------------------------------

_INDEX = "_[_]"
_OPT_SELECT = "_?._"
_OPT_INDEX = "_[?_]"


def _path_of(node: Node, bound: frozenset[str]) -> tuple[str, list[str], bool] | None:
    """``(variable, segments, guarded)`` when ``node`` reads a fixed path of a variable."""
    segments: list[str] = []
    guarded = False
    current = node
    while True:
        if current.kind == "select" and current.target is not None:
            segments.append(str(current.value))
            guarded = guarded or current.test_only
            current = current.target
        elif (
            current.kind == "call"
            and current.function in (_INDEX, _OPT_SELECT, _OPT_INDEX)
            and len(current.args) == 2
            and current.args[1].kind == "const"
            and isinstance(current.args[1].value, (str, int))
            and not isinstance(current.args[1].value, bool)
        ):
            key = current.args[1].value
            segments.append(f"[{key}]" if isinstance(key, int) else str(key))
            guarded = guarded or current.function != _INDEX
            current = current.args[0]
        elif current.kind == "ident" and current.value not in bound:
            return str(current.value), segments[::-1], guarded
        else:
            return None


def _walk_reads(node: Node, bound: frozenset[str], found: dict[str, bool]) -> None:
    path = _path_of(node, bound)
    if path is not None:
        variable, segments, guarded = path
        text = _join(variable, segments)
        found[text] = found.get(text, False) or guarded
        return
    children: list[tuple[Node, frozenset[str]]] = []
    if node.kind == "comprehension":
        rng, init, cond, step, result = node.args
        inner = bound | {node.iter_var, node.accu_var}
        children = [(rng, bound), (init, bound), (cond, inner), (step, inner), (result, inner)]
    else:
        if node.target is not None:
            children.append((node.target, bound))
        children.extend((arg, bound) for arg in node.args)
        for key, value in node.entries:
            if isinstance(key, Node):
                children.append((key, bound))
            children.append((value, bound))
    for child, scope in children:
        _walk_reads(child, scope, found)


def _join(variable: str, segments: Sequence[str]) -> str:
    text = variable
    for segment in segments:
        text += segment if segment.startswith("[") else f".{segment}"
    return text


# --- cost -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Estimate:
    cost: int
    size: int  # an upper bound of the size of the value (string/list/map; 1 otherwise)
    element: int  # an upper bound of the size of any element of it


def _size(value: Any) -> int:
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return max(1, len(value))
    return 1


def _largest(value: Any) -> int:
    """The largest size of any string, list or map inside ``value``."""
    best = _size(value)
    if isinstance(value, Mapping):
        for item in value.values():
            best = max(best, _largest(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            best = max(best, _largest(item))
    return best


def _lookup(value: Any, segments: Sequence[str]) -> tuple[bool, Any]:
    for segment in segments:
        if segment.startswith("[") and isinstance(value, list):
            index = int(segment[1:-1])
            if not 0 <= index < len(value):
                return False, None
            value = value[index]
        elif isinstance(value, Mapping) and segment in value:
            value = value[segment]
        else:
            return False, None
    return True, value


# Functions whose result may be larger than their receiver.
_GROWING = frozenset({"replace", "join", "format", "flatten", "_+_", "string"})


class _Coster:
    def __init__(self, activation: Mapping[str, Any]) -> None:
        self.activation = activation
        self.bound = max([1, *(_largest(v) for v in activation.values())])

    def estimate(self, node: Node, scope: Mapping[str, _Estimate]) -> _Estimate:
        if node.kind == "const":
            return _Estimate(0, _size(node.value), 1)
        path = _path_of(node, frozenset(scope))
        if path is not None:
            variable, segments, _ = path
            found, value = _lookup(self.activation.get(variable), segments)
            steps = len(segments) + 1
            if found:
                elements = [
                    _size(v)
                    for v in (
                        value.values()
                        if isinstance(value, Mapping)
                        else value
                        if isinstance(value, list)
                        else []
                    )
                ]
                return _Estimate(steps, _size(value), max([1, *elements]))
            return _Estimate(steps, self.bound, self.bound)
        if node.kind == "ident":
            known = scope.get(str(node.value))
            return known if known is not None else _Estimate(1, self.bound, self.bound)
        if node.kind == "select" and node.target is not None:
            base = self.estimate(node.target, scope)
            return _Estimate(base.cost + 1, base.element, self.bound)
        if node.kind == "list":
            parts = [self.estimate(arg, scope) for arg in node.args]
            return _Estimate(
                1 + sum(p.cost for p in parts),
                max(1, len(parts)),
                max([1, *(p.size for p in parts)]),
            )
        if node.kind == "struct":
            parts = [self.estimate(value, scope) for _, value in node.entries]
            keys = [self.estimate(k, scope) for k, _ in node.entries if isinstance(k, Node)]
            return _Estimate(
                1 + sum(p.cost for p in [*parts, *keys]),
                max(1, len(parts)),
                max([1, *(p.size for p in parts)]),
            )
        if node.kind == "comprehension":
            return self._comprehension(node, scope)
        return self._call(node, scope)

    def _comprehension(self, node: Node, scope: Mapping[str, _Estimate]) -> _Estimate:
        rng, init, cond, step, result = node.args
        over = self.estimate(rng, scope)
        start = self.estimate(init, scope)
        iterations = over.size if not (rng.kind == "list" and not rng.args) else 0
        element = _Estimate(0, over.element, self.bound)
        first = {
            **scope,
            node.iter_var: element,
            node.accu_var: _Estimate(0, start.size, start.element),
        }
        once = self.estimate(step, first)
        grows = max(0, once.size - start.size)
        accu = _Estimate(0, start.size + iterations * grows, max(start.element, once.element))
        inner = {**scope, node.iter_var: element, node.accu_var: accu}
        body = self.estimate(cond, inner).cost + self.estimate(step, inner).cost
        done = self.estimate(result, {**scope, node.accu_var: accu})
        cost = over.cost + start.cost + iterations * max(1, body) + done.cost
        return _Estimate(cost, max(done.size, 1), done.element)

    def _call(self, node: Node, scope: Mapping[str, _Estimate]) -> _Estimate:
        parts = [
            self.estimate(arg, scope)
            for arg in ([node.target] if node.target is not None else []) + node.args
        ]
        total = sum(p.size for p in parts)
        cost = 1 + sum(p.cost for p in parts) + math.ceil(total / 10)
        name = node.function
        if name in _GROWING:
            if name == "replace" and len(parts) >= 3:
                size = parts[0].size * (parts[2].size + 1)
            elif name == "join" and parts:
                sep = parts[1].size if len(parts) > 1 else 0
                size = parts[0].size * (parts[0].element + sep)
            elif name == "flatten" and parts:
                size = parts[0].size * parts[0].element
            elif name == "string":
                size = max(32, total)
            else:
                size = total
            element = max([1, *(p.element for p in parts)])
            return _Estimate(cost, max(1, size), element)
        if name in ("split",) and parts:
            return _Estimate(cost, parts[0].size + 1, parts[0].size)
        if parts and name in (
            "slice",
            "sort",
            "distinct",
            "reverse",
            "lowerAscii",
            "upperAscii",
            "trim",
            "substring",
            "charAt",
            "quote",
            "orValue",
            "value",
            "_?_:_",
            "dyn",
        ):
            biggest = max(parts, key=lambda p: p.size)
            return _Estimate(
                cost, biggest.size * (2 if name == "quote" else 1) + 2, biggest.element
            )
        if name in (_INDEX, _OPT_INDEX, _OPT_SELECT) and parts:
            return _Estimate(cost, parts[0].element, self.bound)
        return _Estimate(cost, 1, 1)


def _depth(node: Node, comprehensions: int = 0) -> tuple[int, int]:
    """``(tree depth, comprehension nesting)``; ``cel.bind`` does not iterate."""
    children: list[Node] = list(node.args)
    if node.target is not None:
        children.append(node.target)
    for key, value in node.entries:
        children.extend([value, *([key] if isinstance(key, Node) else [])])
    nested = comprehensions
    if node.kind == "comprehension":
        rng = node.args[0]
        if not (rng.kind == "list" and not rng.args):
            nested += 1
    deepest, most = 0, nested
    for child in children:
        d, c = _depth(child, nested)
        deepest, most = max(deepest, d), max(most, c)
    return deepest + 1, most


# --- environment and programs ------------------------------------------------------------

Variable = JsonSchema | None


@dataclass(frozen=True)
class Environment:
    """Variables and functions an expression may use, with their types.

    Build it with :func:`environment`; it is immutable and may be shared.
    """

    env: Any
    pool: Any
    shapes: Mapping[str, _Shape]
    classes: Mapping[str, Any]
    default_calendar: str | None

    def compile(self, expression: str, *, path: str | None = None) -> "Program":
        """Parse and type-check ``expression``; :class:`ExpressionError` with its position."""
        try:
            return self._compile(expression)
        except ExpressionError as exc:
            raise exc.at(path) if path is not None else exc from None

    def _compile(self, expression: str) -> "Program":
        if not isinstance(expression, str) or not expression.strip():
            raise ExpressionError(
                EXPRESSION_SYNTAX_ERROR, "an expression must be a non-empty string"
            )
        if len(expression) > MAX_EXPRESSION_LENGTH:
            raise ExpressionError(
                EXPRESSION_TOO_COMPLEX,
                f"the expression is longer than {MAX_EXPRESSION_LENGTH} characters",
                expression=expression[:200],
            )
        rewrite = _rewrite_durations(expression)
        try:
            compiled = self.env.compile(rewrite.source)
        except RuntimeError as exc:
            raise self._compile_error(expression, rewrite, str(exc)) from None
        checked = _checked(compiled.serialize())
        depth, nesting = _depth(checked.root)
        if depth > MAX_NESTING or nesting > MAX_COMPREHENSION_NESTING:
            raise ExpressionError(
                EXPRESSION_TOO_COMPLEX,
                f"the expression nests deeper than {MAX_NESTING} or has more than "
                f"{MAX_COMPREHENSION_NESTING} nested comprehensions",
                expression=expression,
            )
        found: dict[str, bool] = {}
        _walk_reads(checked.root, frozenset(), found)
        output = compiled.return_type().name()
        if output.startswith("optional_type") or output.startswith("OPTIONAL"):
            raise ExpressionError(
                EXPRESSION_TYPE_ERROR,
                "the expression yields an optional value: end it with .orValue(...)",
                expression=expression,
            )
        return Program(
            environment=self,
            expression=expression,
            compiled=compiled,
            checked=checked,
            reads=tuple(sorted(found)),
            guarded=frozenset(path for path, guarded in found.items() if guarded),
            output_type=output,
        )

    def _compile_error(self, expression: str, rewrite: _Rewrite, text: str) -> ExpressionError:
        match = re.search(r"<input>:(\d+):(\d+): (.*?)(?:\n|$)", text)
        if match is None:
            return ExpressionError(EXPRESSION_TYPE_ERROR, text.strip(), expression=expression)
        line, column, message = int(match.group(1)), int(match.group(2)), match.group(3)
        offset = _offset(rewrite.source, line, column)
        line, column = _line_column(expression, rewrite.original(offset))
        syntax = message.startswith("Syntax error")
        code = EXPRESSION_SYNTAX_ERROR if syntax else EXPRESSION_TYPE_ERROR
        return ExpressionError(
            code,
            _STRUCT_NAME.sub(lambda m: f"'{_cel_name(m.group(1))}'", message.rstrip()),
            expression=expression,
            line=line,
            column=column,
            hint=self._hint(message),
        )

    def _hint(self, message: str) -> str | None:
        if "undeclared reference to 'now'" in message:
            return "there is no current time: use event.time or instance.clock"
        match = re.search(r"undefined field '([^']+)' not found in struct '([^']+)'", message)
        if match is None:
            return None
        wrong, message_name = match.groups()
        try:
            descriptor = self.pool.FindMessageTypeByName(message_name)
        except KeyError:
            return None
        names = [f.name for f in descriptor.fields]
        close = difflib.get_close_matches(wrong, names, n=1)
        owner = _cel_name(message_name)
        if close:
            return f"{owner} has {close[0]}"
        return f"{owner} has {', '.join(sorted(names))}" if names else None

    def activation(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Plain JSON-like values of the variables as the messages CEL reads."""
        out: dict[str, Any] = {}
        for name, shape in self.shapes.items():
            raw = values.get(name)
            if shape.kind != "message":
                out[name] = _json_ready({} if raw is None and shape.kind == "struct" else raw)
                continue
            try:
                out[name] = _to_proto(raw, shape, self.classes)
            except (json_format.ParseError, TypeError, ValueError) as exc:
                raise ExpressionError(
                    EXPRESSION_ERROR,
                    f"{name} does not match its schema: {exc}",
                    details={"variable": name},
                ) from None
        return out


_STRUCT_NAME = re.compile(r"'(cp\.cel\.e[0-9a-f]+\.[A-Za-z0-9_.]+)'")


def _cel_name(struct: str) -> str:
    parts = struct.split(".")
    root = next((p for p in parts if p.startswith("V_")), None)
    names = [p[2:] for p in parts[parts.index(root) + 1 :] if p.startswith("T_")] if root else []
    return ".".join([root[2:] if root else struct, *names])


def _offset(source: str, line: int, column: int) -> int:
    lines = source.split("\n")
    return sum(len(text) + 1 for text in lines[: line - 1]) + column - 1


@dataclass(frozen=True)
class Result:
    """The value of one evaluation.

    ``provisional`` — a ``cal.*`` call looked at a provisional or unpublished
    year; the timer or step whose expression gave the value carries the mark.
    ``cost`` — the bound the limit was checked against.
    """

    value: Any
    provisional: bool
    cost: int


@dataclass(frozen=True)
class Program:
    """A compiled, type-checked expression."""

    environment: Environment
    expression: str
    compiled: Any
    checked: _Checked
    # Fields the expression reads: ``data.case.submittedAt``.
    reads: tuple[str, ...]
    # Of those, the ones it tests for presence (``has()``, ``.?``).
    guarded: frozenset[str]
    output_type: str

    def cost(self, values: Mapping[str, Any]) -> int:
        """The cost bound of evaluating on ``values`` (plain JSON-like variables)."""
        return _Coster(values).estimate(self.checked.root, {}).cost

    def evaluate(
        self,
        values: Mapping[str, Any],
        *,
        calendars: Mapping[str, Calendar] | None = None,
        cost_limit: int = DEFAULT_COST_LIMIT,
        activation: Mapping[str, Any] | None = None,
    ) -> Result:
        """Evaluate on plain JSON-like ``values`` of the variables.

        ``calendars`` — the calendar versions recorded for this evaluation, by
        key. :class:`ExpressionError` with ``expression_cost_exceeded`` before
        anything runs when the cost bound is above ``cost_limit``, with
        ``expression_error`` when the evaluation fails. ``activation`` —
        :meth:`Environment.activation` of the same ``values`` in the environment
        of this program, built once for several programs over one record.
        """
        cost = self.cost(values)
        if cost > cost_limit:
            raise ExpressionError(
                EXPRESSION_COST_EXCEEDED,
                f"the evaluation may cost {cost}, more than the limit {cost_limit}",
                expression=self.expression,
                details={"cost": cost, "limit": cost_limit},
            )
        self._require_times(values)
        if activation is None:
            activation = self.environment.activation(values)
        scope = _Scope(calendars or {}, self.environment.default_calendar)
        token = _SCOPE.set(scope)
        try:
            outcome = self.compiled.eval(data=activation)
        finally:
            _SCOPE.reset(token)
        if outcome.type() == cel.Type.ERROR:
            if scope.failure is not None:
                raise scope.failure
            raise ExpressionError(
                EXPRESSION_ERROR, str(outcome.value()), expression=self.expression
            )
        return Result(outcome.plain_value(), scope.provisional, cost)

    def _require_times(self, values: Mapping[str, Any]) -> None:
        """An unset timestamp or duration reads as the epoch or zero in protobuf.

        A deadline must not silently become 1970: a read of such a field the
        expression does not test with ``has()`` or ``.?`` must find it set.
        """
        for path in self.reads:
            if path in self.guarded:
                continue
            variable, *rest = path.split(".")
            shape: _Shape | None = self.environment.shapes.get(variable)
            walked: list[str] = []
            for segment in rest:
                if shape is None or segment.startswith("["):
                    shape = None
                    break
                shape = shape.fields.get(segment) if shape.kind == "message" else None
                walked.append(segment)
            if shape is None or shape.repeated or shape.kind not in ("timestamp", "duration"):
                continue
            found, value = _lookup(values.get(variable), walked)
            if not found or value is None:
                raise ExpressionError(
                    EXPRESSION_ERROR,
                    f"{path} is not set; test it with has({path}) first",
                    expression=self.expression,
                    details={"field": path},
                )


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return _rfc3339(value)
    if isinstance(value, timedelta):
        return _cel_duration(value)
    if isinstance(value, Mapping):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("a timestamp must carry its UTC offset")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _for_shape(value: Any, shape: _Shape) -> Any:
    """A JSON-like value laid out for ``json_format.ParseDict`` on ``shape``."""
    if value is None:
        return None
    if shape.repeated:
        element = shape.element or _Shape(shape.kind)
        return [_for_shape(v, element) for v in value] if isinstance(value, list) else value
    if shape.kind == "timestamp":
        return _rfc3339(value) if isinstance(value, datetime) else value
    if shape.kind == "duration":
        if isinstance(value, timedelta):
            return _cel_duration(value)
        if isinstance(value, str) and value.lstrip("-").startswith("P"):
            parsed = parse_iso_duration(value)
            if parsed is None:
                raise ValueError(f"{value!r} is not an ISO 8601 duration")
            return _cel_duration(parsed)
        return value
    if shape.kind == "message" and isinstance(value, Mapping):
        return {
            name: _for_shape(value[name], sub)
            for name, sub in shape.fields.items()
            if name in value and value[name] is not None
        }
    if shape.kind == "map" and isinstance(value, Mapping) and shape.element is not None:
        return {str(k): _for_shape(v, shape.element) for k, v in value.items() if v is not None}
    return _json_ready(value)


def _to_proto(value: Any, shape: _Shape, classes: Mapping[str, Any]) -> Message:
    cls = classes[shape.message or ""]
    message: Message = cls()
    if value is None:
        return message
    if not isinstance(value, Mapping):
        raise TypeError(f"expected an object, got {type(value).__name__}")
    json_format.ParseDict(_for_shape(value, shape), message, ignore_unknown_fields=True)
    return message


# Marker for a binding typed as a task (``spawnedBy``).
TASK: JsonSchema = {"$comment": "task"}

# Built environments by the hash of their schemas; the oldest goes first.
_ENVIRONMENTS: dict[str, Environment] = {}
_SCALAR_BINDINGS: Mapping[str, Any] = {
    "string": cel.Type.STRING,
    "integer": cel.Type.INT,
    "number": cel.Type.DOUBLE,
    "boolean": cel.Type.BOOL,
}
_MAX_ENVIRONMENTS = 256


def environment(
    *,
    data: Variable = None,
    event_payload: Variable = None,
    step_result: Variable = None,
    custom_fields: Variable = None,
    stages: Sequence[str] = (),
    calendar: str | None = None,
    bindings: Mapping[str, Variable] | None = None,
    settings: Variable = None,
) -> Environment:
    """The ``cp/1`` environment for one place of a process definition.

    ``data`` is the JSON Schema of ``spec.data``; ``event_payload`` the schema
    of the entry event's payload (event catalog or observation kind);
    ``step_result`` the output schema of the step's skill; ``custom_fields``
    the ``fieldSchema`` of the task's type; ``stages`` the stage ids;
    ``calendar`` the process's ``spec.calendar``, which makes ``calendarKey``
    of ``cal.*`` optional. ``bindings`` adds variables beyond the profile's —
    the translation of earlier syntaxes names them (``None``: ``dyn``,
    :data:`TASK` for a task-typed one). ``settings`` — the type of the
    variable ``settings`` of an object of a package (CP-ADR-0081 §6, built by
    :mod:`control_plane.domain.settings_refs`); ``None``: no such variable.
    Environments are cached by content.
    """
    schemas: dict[str, JsonSchema] = {
        "data": data or {"type": "object"},
        "event": event_schema(event_payload),
        "step": step_schema(step_result),
        "task": task_schema(custom_fields),
        "stage": stage_schema(tuple(stages)),
        "instance": INSTANCE_SCHEMA,
    }
    if settings is not None:
        schemas["settings"] = settings
    for name, schema in (bindings or {}).items():
        if name in schemas or not _FIELD_NAME.match(name):
            raise ValueError(f"binding {name!r} clashes with the profile or is not a name")
        schemas[name] = task_schema() if schema is TASK else (schema or _ANY)
    key = hashlib.sha256(
        json.dumps([schemas, calendar], sort_keys=True, default=str).encode()
    ).hexdigest()
    cached = _ENVIRONMENTS.get(key)
    if cached is not None:
        return cached
    package = f"cp.cel.e{key[:16]}"
    builder = _Builder(package)
    shapes: dict[str, _Shape] = {}
    variables: dict[str, Any] = {}
    for name, schema in schemas.items():
        if _single_type(schema) == "object" and _typed_object(schema):
            shapes[name] = builder.message(f"V_{name}", schema)
            variables[name] = f"{package}.V_{name}"
        elif _single_type(schema) == "object":
            shapes[name] = _Shape("struct")
            variables[name] = cel.Type.Map(cel.Type.STRING, cel.Type.DYN)
        elif name not in VARIABLES and _single_type(schema) in _SCALAR_BINDINGS:
            # A scalar binding (a view's ``status`` of an instance) is typed: never null.
            shapes[name] = _Shape("value")
            variables[name] = _SCALAR_BINDINGS[str(_single_type(schema))]
        else:
            shapes[name] = _Shape("value")
            variables[name] = cel.Type.DYN
    pool = descriptor_pool.DescriptorPool()
    for module in _WELL_KNOWN:
        pool.AddSerializedFile(module.DESCRIPTOR.serialized_pb)
    pool.Add(builder.file)
    classes = {
        shape.message: message_factory.GetMessageClass(pool.FindMessageTypeByName(shape.message))
        for shape in shapes.values()
        if shape.message
    }
    variables = {
        name: cel.Type(kind) if isinstance(kind, str) else kind for name, kind in variables.items()
    }
    env = cel.NewEnv(
        descriptor_pool=pool,
        variables=variables,
        extensions=[
            ext_strings.ExtStrings(),
            ext_optional.ExtOptional(),
            ext_bindings.ExtBindings(),
        ],
        functions=_functions(with_default_calendar=calendar is not None),
    )
    built = Environment(env, pool, shapes, classes, calendar)
    if len(_ENVIRONMENTS) >= _MAX_ENVIRONMENTS:
        del _ENVIRONMENTS[next(iter(_ENVIRONMENTS))]
    _ENVIRONMENTS[key] = built
    return built


# --- the earlier syntaxes in CEL (CP-ADR-0075 §7) ------------------------------------------

# Variables beyond the profile's that a translation may need, and what the
# caller has to bind to them (the translation names them explicitly).
BINDINGS: Mapping[str, str] = {
    "trigger": "the trigger document of a work rule (kind, type, ref, eventId, ...)",
    "goal": "the goal of a work rule",
    "item": "the element of a work rule's forEach",
    "skill": "the interpreting skill's invocation, beyond its output (step.result)",
    "spawnedBy": "the task this one was spawned by (the oldest spawned_by relation)",
    "approval": "the decided approval (id, comment, decidedBy, decidedAt, outcome)",
    "observation": "the observation a gate precondition reads",
}
APPROVAL_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "id": _STR,
        "comment": _STR,
        "decidedBy": _STR,
        "decidedAt": _STR,
        "outcome": _STR,
    },
}
_BINDING_TYPES: Mapping[str, Variable] = {
    "spawnedBy": TASK,
    "approval": APPROVAL_SCHEMA,
}


def legacy_environment(bindings: Sequence[str] = tuple(BINDINGS)) -> Environment:
    """An environment with the translation's bindings: the one to check a translation in."""
    return environment(bindings={name: _BINDING_TYPES.get(name) for name in bindings})


@dataclass(frozen=True)
class Translation:
    expression: str
    # Variables beyond the profile's the expression reads (names of BINDINGS).
    bindings: tuple[str, ...] = ()


class TranslationError(ValueError):
    """An earlier expression that has no CEL counterpart (it is outside its grammar)."""


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def literal(value: Any) -> str:
    """A JSON value as a CEL literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".eEn") else f"{text}.0"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{literal(str(k))}: {literal(v)}" for k, v in value.items()) + "}"
    raise TranslationError(f"{value!r} is not a JSON value")


def _segment(name: str, *, safe: bool) -> str:
    if name.isdigit():
        return f"[?{name}]" if safe else f"[{name}]"
    if _IDENT.match(name) and name not in _CEL_RESERVED:
        return f".?{name}" if safe else f".{name}"
    return f"[?{literal(name)}]" if safe else f"[{literal(name)}]"


def _access(prefix: str, segments: Sequence[str], *, safe: bool = True) -> str:
    """``prefix`` + segments; ``safe``: a missing step reads ``null``, as before."""
    if not segments:
        return prefix
    text = prefix + "".join(_segment(s, safe=safe) for s in segments)
    return f"{text}.orValue(null)" if safe else text


# -- work rule conditions (CP-ADR-0063) and notification conditions ---------------

_TASK_FIELDS = frozenset(task_schema()["properties"])


def _rule_path(text: str) -> tuple[str, str | None]:
    """A work rule path (``payload.data.repo``) in CEL; the binding it needs, if any."""
    root, *segments = text.split(".")
    if root == "payload":
        return _access("event.payload", segments), None
    if root == "event":
        return _access("event", segments), None
    if root == "task":
        if segments and segments[0] in _TASK_FIELDS:
            return _access(f"task.{segments[0]}", segments[1:]), None
        return _access("task", segments), None
    if root == "skill":
        if segments[:1] == ["output"]:
            return _access("step.result", segments[1:]), None
        step = {"status": "status", "skill": "skill", "invocationId": "id"}
        if segments and segments[0] in step:
            return _access(f"step.{step[segments[0]]}", segments[1:]), None
    if root in BINDINGS:
        return _access(root, segments), root
    raise TranslationError(f"{text!r} starts with an unknown root")


def _operand(value: Any, bindings: set[str]) -> tuple[str, bool]:
    """CEL of one operand; whether it may be null."""
    if isinstance(value, Mapping) and set(value) == {"var"}:
        text, binding = _rule_path(str(value["var"]))
        if binding:
            bindings.add(binding)
        return text, True
    if isinstance(value, Mapping) and set(value) == {"const"}:
        return literal(value["const"]), value["const"] is None
    if isinstance(value, list):
        return "[" + ", ".join(_operand(v, bindings)[0] for v in value) + "]", False
    if isinstance(value, Mapping):
        return f"({_condition(value, bindings)})", False
    return literal(value), value is None


_ORDER = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _condition(expression: Any, bindings: set[str]) -> str:
    if isinstance(expression, bool):
        return literal(expression)
    if not isinstance(expression, Mapping) or len(expression) != 1:
        raise TranslationError(f"{expression!r} is not a condition")
    ((operator, args),) = expression.items()
    if operator in ("and", "or"):
        joiner = " && " if operator == "and" else " || "
        parts = [_condition(item, bindings) for item in args]
        return parts[0] if len(parts) == 1 else joiner.join(f"({p})" for p in parts)
    if operator == "not":
        return f"!({_condition(args, bindings)})"
    if operator == "exists":
        text, binding = _rule_path(str(args))
        if binding:
            bindings.add(binding)
        return f"{text} != null"
    left, left_null = _operand(args[0], bindings)
    right, right_null = _operand(args[1], bindings)
    if operator == "eq":
        return f"{left} == {right}"
    if operator == "ne":
        return f"{left} != {right}"
    if operator == "in":
        # null on the right is "not in"; a literal list never is null.
        if isinstance(args[1], list):
            return f"{left} in {right}"
        return f"{right} != null && {left} in {right}"
    if operator in _ORDER:
        # An ordering with a null side is false, as before.
        guards = [
            f"{side} != null" for side, null in ((left, left_null), (right, right_null)) if null
        ]
        return " && ".join([*guards, f"{left} {_ORDER[operator]} {right}"])
    raise TranslationError(f"unknown operator {operator!r}")


def translate_condition(condition: Any) -> Translation:
    """A JSON condition (``{"eq": [{"var": "payload.kind"}, "results"]}``) in CEL.

    Roots: ``payload`` is ``event.payload``, ``skill.output`` is ``step.result``,
    ``task`` is ``task``; ``trigger``, ``goal``, ``item`` and ``observation``
    stay variables of their own (:data:`BINDINGS`). A missing value reads
    ``null`` as before (``.?`` and ``orValue(null)``), so ``ne`` against a
    missing field still holds.
    """
    bindings: set[str] = set()
    text = _condition(condition, bindings)
    return Translation(text, tuple(sorted(bindings)))


def translate_rule_path(path: str) -> Translation:
    """A work rule path (``forEach: payload.data.failedJobs``) in CEL."""
    text, binding = _rule_path(path)
    return Translation(text, (binding,) if binding else ())


# -- ``$.`` expressions of approval outcomes, checks and completions (CP-ADR-0061) ----

_OUTCOME = re.compile(
    r"\$\.(?P<root>[A-Za-z]+)(?P<rest>(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[A-Za-z0-9_.:-]+\])*)"
    r"(?P<required>!)?(?:\|truncate:(?P<truncate>[0-9]+))?"
)


def _outcome_value(path: Any, *, safe: bool) -> tuple[str, str | None]:
    """CEL of one parsed ``$.`` path (``approval_outcomes.Path``)."""
    root = path.root
    if root == "invocation":
        if path.field == "output":
            return _access("step.result", [path.key], safe=safe), None
        if path.field == "error":
            return f"step.error.{path.key}", None
        return f"step.{path.field}", None
    if root == "approval":
        return f"approval.{path.field}", "approval"
    prefix = "task" if root == "task" else "spawnedBy"
    binding = None if root == "task" else "spawnedBy"
    if path.artifact_type is not None:
        return _access(
            f"{prefix}.artifacts", [path.artifact_type, "metadata", path.metadata_key], safe=safe
        ), binding
    if path.field == "customFields":
        return _access(f"{prefix}.customFields", [path.key], safe=safe), binding
    return f"{prefix}.{path.field}", binding


def _parse_outcome(text: str) -> Any:
    from control_plane.domain.approval_outcomes import parse_path

    try:
        return parse_path(text, invocation=True)
    except ValidationError as exc:
        raise TranslationError(exc.message) from None


def _truncated(text: str, limit: int, *, only_strings: bool) -> str:
    check = "type(t) == string && " if only_strings else ""
    return f'cel.bind(t, {text}, {check}t.size() > {limit} ? t.substring(0, {limit - 1}) + "…" : t)'


def translate_path(source: str) -> Translation:
    """A ``$.`` input of an outcome, check or completion in CEL.

    A string that is one expression keeps the raw value; any other string is
    a template: the pieces are concatenated, a missing value is ``""``.
    ``!`` (required) reads the field without ``.?``, so a missing one fails
    the evaluation; ``|truncate:N`` becomes ``substring``. The decision's
    approval and the ``spawned_by`` task are variables of their own
    (``approval``, ``spawnedBy``); ``$.invocation`` is ``step``.
    """
    bindings: set[str] = set()
    whole = _OUTCOME.fullmatch(source)
    if whole is not None:
        path = _parse_outcome(source)
        text, binding = _outcome_value(path, safe=not path.required)
        if binding:
            bindings.add(binding)
        if path.truncate is not None:
            text = _truncated(text, path.truncate, only_strings=True)
        return Translation(text, tuple(sorted(bindings)))
    pieces: list[str] = []
    last = 0
    for match in _OUTCOME.finditer(source):
        if match.start() > last:
            pieces.append(literal(source[last : match.start()]))
        path = _parse_outcome(match.group(0))
        text, binding = _outcome_value(path, safe=not path.required)
        if binding:
            bindings.add(binding)
        piece = (
            f"string({text})"
            if path.required
            else (f'cel.bind(v, {text}, v == null ? "" : string(v))')
        )
        if path.truncate is not None:
            piece = _truncated(piece, path.truncate, only_strings=False)
        pieces.append(piece)
        last = match.end()
    if last < len(source) or not pieces:
        pieces.append(literal(source[last:]))
    return Translation(" + ".join(pieces), tuple(sorted(bindings)))


def translate_when(conditions: Sequence[str]) -> Translation:
    """``when`` of a check or a completion: every path resolved to something.

    "Something" is what it was: not ``null``, not ``""``, not ``false``.
    """
    bindings: set[str] = set()
    parts: list[str] = []
    for source in conditions:
        path = _parse_outcome(source)
        text, binding = _outcome_value(path, safe=True)
        if binding:
            bindings.add(binding)
        parts.append(f'!({text} in [null, "", false])')
    return Translation(" && ".join(parts), tuple(sorted(bindings)))


# -- execution inputs (ADR-0056 §3) and context anchors (CP-ADR-0064) ----------------

_EXECUTION_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d{1,6})\]")


def translate_execution_input(path: str) -> Translation:
    """An ``execution.inputs`` path over the task (``$.customFields.url``) in CEL.

    A missing step reads ``null``; the daemon left such an input out, and the
    contract's input schema decides either way.
    """
    if not path.startswith("$"):
        raise TranslationError(f"{path!r} is not an execution input path")
    segments: list[str] = []
    position = 1
    while position < len(path):
        match = _EXECUTION_SEGMENT.match(path, position)
        if match is None:
            raise TranslationError(f"{path!r} is not an execution input path")
        segments.append(match.group(1) if match.group(1) is not None else match.group(2))
        position = match.end()
    if not segments:
        return Translation("task")
    if segments[0] not in _TASK_FIELDS:
        raise TranslationError(f"{path!r}: the task has no field {segments[0]!r}")
    return Translation(_access(f"task.{segments[0]}", segments[1:]))


def translate_context_source(source: str) -> Translation:
    """An anchor's ``from`` (``description``, ``$.customFields.codes``) in CEL.

    ``description``/``title`` are the text identifiers are extracted from, as
    before; the extraction stays with the anchor.
    """
    from control_plane.domain.context_schema import parse_source

    try:
        path = parse_source(source, where="from")
    except ValidationError as exc:
        raise TranslationError(exc.message) from None
    text, binding = _outcome_value(path, safe=True)
    return Translation(text, (binding,) if binding else ())


# -- where the earlier expressions are ----------------------------------------------------


@dataclass(frozen=True)
class LegacyExpression:
    pointer: str  # JSON pointer into the catalog object: /spec/condition
    syntax: str  # condition, rule_path, path, when, execution_input, context_source
    source: Any


def _escape(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def _outcome_strings(value: Any, pointer: str) -> Iterator[LegacyExpression]:
    if isinstance(value, str):
        if _OUTCOME.search(value):
            yield LegacyExpression(pointer, "path", value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{pointer}/{_escape(key)}"
            if key == "condition":
                yield LegacyExpression(here, "condition", item)
            elif key == "when" and isinstance(item, list):
                yield LegacyExpression(here, "when", item)
            else:
                yield from _outcome_strings(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _outcome_strings(item, f"{pointer}/{index}")


def legacy_expressions(kind: str, spec: Mapping[str, Any]) -> Iterator[LegacyExpression]:
    """Every expression of an earlier syntax in a catalog object's ``spec``.

    ``{{...}}`` text templates are not expressions (CP-ADR-0075, boundaries).
    """
    if kind == "WorkRule":
        if spec.get("condition") is not None:
            yield LegacyExpression("/spec/condition", "condition", spec["condition"])
        action = spec.get("action") or {}
        if action.get("forEach") is not None:
            yield LegacyExpression("/spec/action/forEach", "rule_path", action["forEach"])
        if action.get("where") is not None:
            yield LegacyExpression("/spec/action/where", "condition", action["where"])
    elif kind == "NotificationRule":
        when = (spec.get("on") or {}).get("when")
        if when is not None:
            yield LegacyExpression("/spec/on/when", "condition", when)
    elif kind == "TaskType":
        inputs = (spec.get("execution") or {}).get("inputs")
        if isinstance(inputs, str):
            yield LegacyExpression("/spec/execution/inputs", "execution_input", inputs)
        elif isinstance(inputs, Mapping):
            for name, path in inputs.items():
                yield LegacyExpression(
                    f"/spec/execution/inputs/{_escape(name)}", "execution_input", path
                )
        for section in ("approvalSchema", "completionSchema", "acceptance"):
            if spec.get(section) is not None:
                yield from _outcome_strings(spec[section], f"/spec/{section}")
        anchors = (spec.get("contextSchema") or {}).get("anchors") or []
        for index, anchor in enumerate(anchors):
            if isinstance(anchor, Mapping) and anchor.get("from") is not None:
                yield LegacyExpression(
                    f"/spec/contextSchema/anchors/{index}/from", "context_source", anchor["from"]
                )


_TRANSLATORS: Mapping[str, Callable[[Any], Translation]] = {
    "condition": translate_condition,
    "rule_path": translate_rule_path,
    "path": translate_path,
    "when": translate_when,
    "execution_input": translate_execution_input,
    "context_source": translate_context_source,
}


def translate(expression: LegacyExpression) -> Translation:
    """The CEL of one earlier expression found by :func:`legacy_expressions`."""
    return _TRANSLATORS[expression.syntax](expression.source)
