"""The expressions and paths of a view in SQL over ``process_instances`` (CP-ADR-0080 amendment A).

The data under a view is read where it lies: the source filter of the view,
the filters and the sort a console asks for and the aggregates of ``metrics``
and ``chart`` become conditions, keys and aggregates of one query, so a page
of 50 of 10⁴ instances is not 10⁴ evaluations. What is translated is the part
of CEL (profile ``cp/1``, CP-ADR-0075) whose meaning SQL keeps exactly:

- **reads** — ``data.<path>`` where every step is a typed field of the data
  schema (:func:`typed_leaf`: a string, a number, a boolean), ``status``,
  ``id``, ``instance.{id,key,version,startedAt,clock}``,
  ``stage.<id>.{active,completed}``, ``param.<name>`` (a constant of the
  request) and literals;
- **operators** — ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``&&``,
  ``||``, ``!``, ``in`` a list of literals, ``? :``, ``+``, ``-``, ``*``,
  ``has()``, ``decimal()``, ``startsWith``, ``endsWith``, ``contains``.

CEL and SQL agree on a missing value as they are written here: a missing
field is ``null`` in CEL and SQL ``NULL``; an error of CEL (``null > 1``,
``decimal("x")``) is SQL ``NULL``, which ``&&``, ``||`` and ``!`` carry as CEL
carries an error, and which a filter does not match. Equality is two-valued in
both: ``data.x == "a"`` is ``data @> {"x": "a"}`` — what the GIN index of
``data`` answers. Anything else raises :class:`Untranslatable`, and the
caller evaluates that expression in Python, record by record, with the same
result — slower, not different.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Integer,
    Numeric,
    Text,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    not_,
    null,
    or_,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql.elements import ColumnElement

from control_plane.domain.cel_profile import Node, Program, typed_leaf
from control_plane.infrastructure.db.models import ProcessInstance

# The regular expression of decimal notation ``decimal()`` reads (cel_profile._DECIMAL).
DECIMAL_SQL = r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$"
INSTANCE_COLUMNS = ("id", "key", "version", "startedAt", "clock")
_LIKE_SPECIAL = re.compile(r"([\\%_])")


class Untranslatable(Exception):
    """An expression SQL does not say exactly: it is evaluated record by record."""


@dataclass(frozen=True)
class Sql:
    """A translated expression.

    ``kind`` — ``json`` (a value of ``data``, ``leaf`` its JSON type),
    ``text``, ``number``, ``bool``, ``time`` or ``null``. ``nullable`` — SQL
    ``NULL`` may be a CEL ``null`` (a missing field); ``erring`` — it may be a
    CEL error. ``const`` — the value of a literal or a param; ``path`` — the
    steps of a read of ``data``.
    """

    expr: Any
    kind: str
    leaf: str | None = None
    nullable: bool = False
    erring: bool = False
    const: Any = None
    is_const: bool = False
    path: tuple[str, ...] | None = None


def json_at(segments: Sequence[str], column: Any = None) -> ColumnElement[Any]:
    """``data #> '{a,b}'``: the JSON value at a path of the data of an instance.

    ``column`` — another JSON column read the same way (``tasks.custom_fields``).
    """
    source = ProcessInstance.data if column is None else column
    found: ColumnElement[Any] = source.op("#>", return_type=JSONB)(
        literal(list(segments), ARRAY(Text))
    )
    return found


def _scalar_text(value: ColumnElement[Any]) -> ColumnElement[Any]:
    return value.op("#>>", return_type=Text)(literal([], ARRAY(Text)))


def _typeof(value: ColumnElement[Any]) -> ColumnElement[Any]:
    return func.jsonb_typeof(value)


def as_text(value: ColumnElement[Any]) -> ColumnElement[Any]:
    """A JSON string as text; any other JSON value is ``NULL``."""
    return case((_typeof(value) == "string", _scalar_text(value)), else_=null())


def as_number(value: ColumnElement[Any]) -> ColumnElement[Any]:
    """A JSON number as ``numeric``; any other JSON value is ``NULL``."""
    return case((_typeof(value) == "number", cast(_scalar_text(value), Numeric)), else_=null())


def as_bool(value: ColumnElement[Any]) -> ColumnElement[Any]:
    return case((_typeof(value) == "boolean", cast(_scalar_text(value), Boolean)), else_=null())


def decimal_of_text(value: ColumnElement[Any]) -> ColumnElement[Any]:
    """``decimal()`` of a text: its number when it is decimal notation, else ``NULL``."""
    trimmed = func.btrim(value)
    return case((trimmed.op("~")(DECIMAL_SQL), cast(trimmed, Numeric)), else_=null())


def contains(path: Sequence[str], value: Any, column: Any = None) -> ColumnElement[bool]:
    """``data @> {"a": {"b": value}}``: the value at ``path`` is ``value`` (the GIN index)."""
    nested: Any = value
    for segment in reversed(path):
        nested = {segment: nested}
    source = ProcessInstance.data if column is None else column
    found: ColumnElement[bool] = source.op("@>", return_type=Boolean)(
        cast(literal(json.dumps(nested), Text), JSONB)
    )
    return found


def like_prefix(value: str) -> str:
    return _LIKE_SPECIAL.sub(r"\\\1", value) + "%"


def _like(value: str, *, start: bool, end: bool) -> str:
    escaped = _LIKE_SPECIAL.sub(r"\\\1", value)
    return f"{'' if start else '%'}{escaped}{'' if end else '%'}"


def stage_state(stage: str) -> ColumnElement[Any]:
    return ProcessInstance.state.op("#>>", return_type=Text)(
        literal(["stages", stage, "state"], ARRAY(Text))
    )


def current_stage(order: Sequence[str]) -> ColumnElement[Any]:
    """The stage of an instance in SQL (``view_query.current_stage``): its index in ``order``."""
    if not order:
        return cast(null(), Integer)
    whens: list[tuple[ColumnElement[bool], Any]] = [
        (stage_state(sid) == "active", index) for index, sid in enumerate(order)
    ]
    whens += [
        (stage_state(sid) == "completed", index) for index, sid in reversed(list(enumerate(order)))
    ]
    return case(*whens, else_=cast(null(), Integer))


@dataclass
class Translator:
    """CEL of one view over the instances of its process, in SQL."""

    data_schema: Mapping[str, Any] | None
    stage_order: Sequence[str]
    params: Mapping[str, Any]
    now: datetime
    # The data paths a translation read: what a currency is looked up next to.
    reads: list[tuple[str, ...]] = field(default_factory=list)
    # The effective settings of the view's package: constants of the query, as params are.
    settings: Mapping[str, Any] = field(default_factory=dict)

    # -- entry points --

    def predicate(self, program: Program) -> ColumnElement[bool]:
        """A filter: true where CEL gives ``true``; ``false`` for anything else, errors too."""
        return func.coalesce(self.boolean(self.node(program.checked.root)), false())

    def number(self, program: Program) -> Any:
        """A number, ``NULL`` where CEL gives none (an error, a missing value)."""
        return self.numeric(self.node(program.checked.root))

    # -- coercions --

    def boolean(self, sql: Sql) -> Any:
        if sql.kind == "bool":
            return sql.expr
        if sql.kind == "json" and sql.leaf == "boolean":
            return as_bool(sql.expr)
        raise Untranslatable(f"{sql.kind} is no condition")

    def numeric(self, sql: Sql) -> Any:
        if sql.kind == "number":
            return sql.expr
        if sql.kind == "json" and sql.leaf in ("number", "integer"):
            return as_number(sql.expr)
        raise Untranslatable(f"{sql.kind} is no number")

    def text(self, sql: Sql) -> Any:
        if sql.kind == "text":
            return sql.expr
        if sql.kind == "json" and sql.leaf == "string":
            return as_text(sql.expr)
        raise Untranslatable(f"{sql.kind} is no string")

    def _plain(self, sql: Sql) -> str:
        """The kind a value compares as: a JSON value as its schema types it."""
        if sql.kind != "json":
            return sql.kind
        return {"string": "text", "number": "number", "integer": "number", "boolean": "bool"}.get(
            sql.leaf or "", "json"
        )

    def _as(self, kind: str, sql: Sql) -> Any:
        if kind == "text":
            return self.text(sql)
        if kind == "number":
            return self.numeric(sql)
        if kind == "bool":
            return self.boolean(sql)
        if kind == "time" and sql.kind == "time":
            return sql.expr
        raise Untranslatable(f"{sql.kind} does not compare as {kind}")

    # -- nodes --

    def node(self, node: Node) -> Sql:
        if node.kind == "const":
            return self.constant(node.value)
        if node.kind == "ident":
            return self.ident(str(node.value))
        if node.kind == "select":
            if node.test_only:
                return self.has(node)
            return self.read(node)
        if node.kind == "call":
            return self.call(node)
        raise Untranslatable(f"{node.kind} is not translated")

    def constant(self, value: Any) -> Sql:
        if value is None:
            return Sql(null(), "null", nullable=True, is_const=True)
        if isinstance(value, bool):
            return Sql(literal(value, Boolean), "bool", const=value, is_const=True)
        if isinstance(value, (int, float)):
            if isinstance(value, float) and value != value:  # NaN
                raise Untranslatable("NaN")
            return Sql(literal(Decimal(str(value)), Numeric), "number", const=value, is_const=True)
        if isinstance(value, str):
            return Sql(literal(value, Text), "text", const=value, is_const=True)
        raise Untranslatable(f"a literal of {type(value).__name__}")

    def ident(self, name: str) -> Sql:
        if name == "status":
            return Sql(ProcessInstance.status, "text")
        if name == "id":
            return Sql(cast(ProcessInstance.id, Text), "text")
        raise Untranslatable(f"{name} as a whole")

    def _chain(self, node: Node) -> tuple[str, list[str]]:
        """``(variable, steps)`` of a read of fixed fields; anything else is untranslatable."""
        steps: list[str] = []
        current: Node | None = node
        while current is not None:
            if current.kind == "select" and current.target is not None and not current.test_only:
                steps.append(str(current.value))
                current = current.target
            elif (
                current.kind == "call"
                and current.function == "_[_]"
                and len(current.args) == 2
                and current.args[1].kind == "const"
                and isinstance(current.args[1].value, str)
            ):
                steps.append(current.args[1].value)
                current = current.args[0]
            elif current.kind == "ident":
                return str(current.value), steps[::-1]
            else:
                break
        raise Untranslatable("not a read of fixed fields")

    def read(self, node: Node) -> Sql:
        variable, steps = self._chain(node)
        if variable == "data":
            leaf = typed_leaf(self.data_schema, steps)
            if leaf is None or leaf == "object":
                raise Untranslatable(f"data.{'.'.join(steps)} is not a typed scalar")
            self.reads.append(tuple(steps))
            return Sql(json_at(steps), "json", leaf=leaf, nullable=True, path=tuple(steps))
        if variable == "instance" and len(steps) == 1 and steps[0] in INSTANCE_COLUMNS:
            name = steps[0]
            if name == "id":
                return Sql(cast(ProcessInstance.id, Text), "text")
            if name == "key":
                return Sql(ProcessInstance.instance_key, "text")
            if name == "version":
                return Sql(cast(ProcessInstance.definition_version, Numeric), "number")
            if name == "startedAt":
                return Sql(ProcessInstance.started_at, "time")
            return Sql(literal(self.now), "time")
        if (
            variable == "stage"
            and len(steps) == 2
            and steps[0] in self.stage_order
            and steps[1] in ("active", "completed")
        ):
            return Sql(func.coalesce(stage_state(steps[0]) == steps[1], false()), "bool")
        if variable in ("param", "settings"):
            value: Any = self.params if variable == "param" else self.settings
            for step in steps:
                value = value.get(step) if isinstance(value, Mapping) else None
            if isinstance(value, (Mapping, list)):
                raise Untranslatable(f"a {variable} that is no scalar")
            return self.constant(value)
        raise Untranslatable(f"{variable}.{'.'.join(steps)}")

    def has(self, node: Node) -> Sql:
        if node.target is None:
            raise Untranslatable("has() of nothing")
        variable, steps = self._chain(node.target)
        steps = [*steps, str(node.value)]
        if variable == "data":
            if typed_leaf(self.data_schema, steps) is None:
                raise Untranslatable("has() of an untyped field")
            present = func.coalesce(_typeof(json_at(steps)) != "null", false())
            return Sql(present, "bool")
        if variable in ("param", "settings"):
            value: Any = self.params if variable == "param" else self.settings
            for step in steps:
                value = value.get(step) if isinstance(value, Mapping) else None
            return self.constant(value is not None)
        raise Untranslatable(f"has() of {variable}")

    def call(self, node: Node) -> Sql:
        name, args = node.function, node.args
        if name in ("_&&_", "_||_") and len(args) == 2:
            left, right = (self.boolean(self.node(a)) for a in args)
            joined = and_(left, right) if name == "_&&_" else or_(left, right)
            return Sql(joined, "bool", erring=True)
        if name == "!_" and len(args) == 1:
            return Sql(not_(self.boolean(self.node(args[0]))), "bool", erring=True)
        if name in ("_==_", "_!=_") and len(args) == 2:
            equal = self.equal(self.node(args[0]), self.node(args[1]))
            if name == "_==_":
                return equal
            return Sql(not_(equal.expr), "bool", erring=equal.erring)
        if name in ("_<_", "_<=_", "_>_", "_>=_") and len(args) == 2:
            return self.compare(name, self.node(args[0]), self.node(args[1]))
        if name == "@in" and len(args) == 2:
            return self.within(self.node(args[0]), args[1])
        if name == "decimal" and len(args) == 1 and node.target is None:
            return self.decimal(self.node(args[0]))
        if name in ("startsWith", "endsWith", "contains") and node.target and len(args) == 1:
            return self.match(name, self.node(node.target), self.node(args[0]))
        if name in ("_+_", "_-_", "_*_") and len(args) == 2:
            left, right = (self.numeric(self.node(a)) for a in args)
            operator = {"_+_": left + right, "_-_": left - right, "_*_": left * right}[name]
            return Sql(operator, "number", erring=True)
        if name == "-_" and len(args) == 1:
            return Sql(-self.numeric(self.node(args[0])), "number", erring=True)
        if name == "_?_:_" and len(args) == 3:
            return self.choose(*(self.node(a) for a in args))
        raise Untranslatable(f"function {name}")

    def equal(self, left: Sql, right: Sql) -> Sql:
        """``==``: two-valued, as CEL's equality; an error of an operand stays one."""
        if left.is_const and not right.is_const:
            left, right = right, left
        if left.kind == "json" and right.is_const and left.path is not None:
            if right.kind == "null":
                missing = func.coalesce(_typeof(left.expr), "null") == "null"
                return Sql(missing, "bool")
            if self._plain(left) != right.kind:
                raise Untranslatable("a comparison of different types")
            return Sql(contains(left.path, right.const), "bool")
        if right.kind == "null":
            if left.erring:
                raise Untranslatable("null against a value that may be an error")
            return Sql(left.expr.is_(None), "bool")
        kind = self._plain(left)
        if kind != self._plain(right) or kind == "json":
            raise Untranslatable("a comparison of different types")
        a, b = self._as(kind, left), self._as(kind, right)
        if not (left.erring or right.erring):
            return Sql(a.is_not_distinct_from(b), "bool")
        if left.nullable or right.nullable:
            raise Untranslatable("an error beside a null")
        return Sql(a == b, "bool", erring=True)

    def compare(self, name: str, left: Sql, right: Sql) -> Sql:
        kind = self._plain(left)
        if kind == "null" or kind != self._plain(right) or kind not in ("text", "number", "time"):
            raise Untranslatable("an order of different types")
        a, b = self._as(kind, left), self._as(kind, right)
        if kind == "text":
            # CEL orders strings by code point: the byte order of UTF-8.
            a, b = a.collate("C"), b.collate("C")
        operator = {"_<_": a < b, "_<=_": a <= b, "_>_": a > b, "_>=_": a >= b}[name]
        return Sql(operator, "bool", erring=True)

    def within(self, value: Sql, items: Node) -> Sql:
        if items.kind != "list" or not items.args or any(a.kind != "const" for a in items.args):
            raise Untranslatable("in a list that is not of literals")
        options = [self.equal(value, self.constant(a.value)) for a in items.args]
        return Sql(or_(*(o.expr for o in options)), "bool", erring=any(o.erring for o in options))

    def decimal(self, value: Sql) -> Sql:
        if value.kind == "number":
            return Sql(value.expr, "number", erring=value.erring)
        if value.kind == "text":
            return Sql(decimal_of_text(value.expr), "number", erring=True)
        if value.kind == "json" and value.leaf in ("number", "integer"):
            return Sql(as_number(value.expr), "number", erring=True)
        if value.kind == "json" and value.leaf == "string":
            return Sql(decimal_of_text(as_text(value.expr)), "number", erring=True)
        raise Untranslatable(f"decimal() of {value.kind}")

    def match(self, name: str, target: Sql, needle: Sql) -> Sql:
        if not (needle.is_const and isinstance(needle.const, str)):
            raise Untranslatable(f"{name} of something not a literal")
        pattern = _like(needle.const, start=name == "startsWith", end=name == "endsWith")
        return Sql(self.text(target).like(pattern, escape="\\"), "bool", erring=True)

    def choose(self, condition: Sql, yes: Sql, no: Sql) -> Sql:
        kind = self._plain(yes)
        if kind != self._plain(no) or kind not in ("text", "number", "bool"):
            raise Untranslatable("a choice of different types")
        test = self.boolean(condition)
        chosen = case((test, self._as(kind, yes)), (not_(test), self._as(kind, no)))
        return Sql(chosen, kind, erring=True, nullable=yes.nullable or no.nullable)


# --- paths of a view: filters, sorts, groups ----------------------------------------------------


@dataclass(frozen=True)
class PathSql:
    """A path of the source in SQL: its value and, for a key of an order, its codec."""

    value: Any
    # json | text | int | time | uuid: how a cursor writes the value down.
    codec: str
    path: tuple[str, ...] | None = None
    leaf: str | None = None


def path_sql(path: str, data_schema: Mapping[str, Any] | None, order: Sequence[str]) -> PathSql:
    """A path of the source as SQL; ``slaState`` — its nearest running deadline (an order only)."""
    root, _, rest = path.partition(".")
    if root == "data" and rest:
        steps = tuple(rest.split("."))
        return PathSql(json_at(steps), "json", steps, typed_leaf(data_schema, steps))
    if path == "status":
        return PathSql(ProcessInstance.status, "text")
    if path in ("id", "instance.id"):
        return PathSql(ProcessInstance.id, "uuid")
    if path == "instance.key":
        return PathSql(ProcessInstance.instance_key, "text")
    if path == "instance.version":
        return PathSql(ProcessInstance.definition_version, "int")
    if path in ("instance.startedAt", "instance.clock"):
        return PathSql(ProcessInstance.started_at, "time")
    if path == "stage":
        return PathSql(current_stage(order), "int")
    if path == "slaState":
        return PathSql(ProcessInstance.sla_due_at, "time")
    raise Untranslatable(f"path {path}")
