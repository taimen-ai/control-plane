"""Data under a view: what ``POST /views/{key}:query`` reads and answers (CP-ADR-0080 amendment A).

TAI-ADR-0066 p.4, stage 2: a console asks the core for exactly what one block
of a view draws — the values of its columns, cards and aggregates — not for
the raw records. What is decided here, without I/O:

- **The request against the view** — the block is one of the layout by its
  index; ``filter`` and ``sort`` name only the fields the block declared
  filterable and sortable (the ``field`` of the display, the path without
  ``data.``), with an operator of the closed list the type of the filter
  takes; ``params`` are the params of the view by their types. Anything else
  is ``422`` with a code the console shows as the error of the block.
- **The values of an instance** an expression of the view reads: ``data``,
  ``stage``, ``instance``, ``param``, ``id``, ``status`` and ``slaState``
  (CP-ADR-0080 §1), laid out as the engine lays them out.
- **The stage of an instance** — its active stage, else its last completed
  one, in the order of the stages of the process; the status category the
  console colors by, the status as ``ProcessInstanceOut.status`` has it.
- **Values by format** — ``money`` is ``{amount, currency}``, times are ISO
  8601, ``status`` is ``{title, category}``, ``link`` is ``{href, title}``;
  no value is ``null``.

Pure functions over plain values; no I/O.
"""

import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import Message

from control_plane.domain.cel_profile import parse_iso_duration
from control_plane.domain.errors import ValidationError

# The blocks whose data the core computes; invoke and component draw none (CP-ADR-0080 §9).
QUERIED_BLOCKS = frozenset(
    {
        "table",
        "list",
        "board",
        "metrics",
        "chart",
        "header",
        "fields",
        "steps",
        "timeline",
        "artifacts",
        "related",
    }
)
# Blocks answered page by page: the only ones a cursor and a sort are for.
PAGED_BLOCKS = frozenset({"table", "list"})
OPS = ("eq", "in", "gte", "lte", "prefix")
# The operators a filter of each type takes: what the console sends for it.
FILTER_OPS: Mapping[str, tuple[str, ...]] = {
    "enum": ("eq", "in"),
    "text": ("eq", "in", "prefix"),
    "number": ("eq", "in", "gte", "lte"),
    "date": ("eq", "gte", "lte"),
}
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_IN_VALUES = 100
MAX_TEXT_VALUE = 500
# The category of an instance's status the console colors a status by.
STATUS_CATEGORY: Mapping[str, str] = {
    "running": "running",
    "suspended": "suspended",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def invalid(code: str, message: str, **details: Any) -> ValidationError:
    return ValidationError(code, message, details=details or None)


# --- the block and what it declares --------------------------------------------------------


@dataclass(frozen=True)
class QueryBlock:
    """A block of a view's form: as the package wrote it (``spec``) and as drawn (``shown``)."""

    index: int
    kind: str
    spec: Mapping[str, Any]
    shown: Mapping[str, Any]


def block_of(form: Mapping[str, Any], index: int) -> QueryBlock:
    """The block ``index`` of the layout; one without data of its own is ``422``."""
    layout = form.get("layout") or []
    shown_layout = (form.get("display") or {}).get("layout") or []
    if not 0 <= index < len(layout) or index >= len(shown_layout):
        raise invalid(
            "unknown_block",
            f"the view has no block {index}: its layout has {len(layout)}",
            block=index,
        )
    spec, shown = layout[index], shown_layout[index]
    kind = str(spec.get("block"))
    if kind not in QUERIED_BLOCKS:
        raise invalid(
            "block_without_data",
            f"block {index} ({kind}) has no data of its own to query",
            block=index,
        )
    return QueryBlock(index, kind, spec, shown)


def field_name(path: str) -> str:
    """The name a display gives a path of a process source: ``data.a.b`` is ``a.b``."""
    return path.removeprefix("data.")


@dataclass(frozen=True)
class DeclaredField:
    """A field a block declared filterable or sortable: its name, its path and its type."""

    name: str
    path: str
    type: str = "text"
    options: tuple[Any, ...] | None = None


def filter_fields(block: QueryBlock) -> dict[str, DeclaredField]:
    """The filters of ``block`` by their names, typed as the display types them."""
    paths = list(block.spec.get("filters") or ())
    shown = list(block.shown.get("filters") or ())
    out: dict[str, DeclaredField] = {}
    for path, drawn in zip(paths, shown, strict=False):
        options = drawn.get("options")
        out[str(drawn["field"])] = DeclaredField(
            name=str(drawn["field"]),
            path=str(path),
            type=str(drawn.get("type") or "text"),
            options=tuple(o["value"] for o in options) if isinstance(options, list) else None,
        )
    return out


def sort_fields(block: QueryBlock) -> dict[str, DeclaredField]:
    """The sortable fields of ``block`` by their names, with the direction the package wrote."""
    out: dict[str, DeclaredField] = {}
    for order in block.spec.get("sort") or ():
        path = str(order["field"])
        out[field_name(path)] = DeclaredField(name=field_name(path), path=path)
    return out


def default_sort(block: QueryBlock) -> list[tuple[DeclaredField, str]]:
    """The order the package wrote for the block: applied when the request names none."""
    return [
        (
            DeclaredField(name=field_name(str(o["field"])), path=str(o["field"])),
            str(o.get("dir") or "asc"),
        )
        for o in block.spec.get("sort") or ()
    ]


# --- the request ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """One condition of ``filter``: a declared field, an operator and its value(s)."""

    field: DeclaredField
    op: str
    value: Any


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _typed(field: DeclaredField, op: str, value: Any, where: str) -> Any:
    """``value`` checked against the type of the filter: a number, a day, a text."""
    if not _scalar(value):
        raise invalid(
            "invalid_filter",
            f"{where}: the value of {field.name} is a string, a number or a boolean",
            field=field.name,
        )
    if field.type == "number":
        if isinstance(value, bool):
            raise invalid(
                "invalid_filter", f"{where}: {field.name} takes a number", field=field.name
            )
        if isinstance(value, str):
            if not _DECIMAL.match(value.strip()):
                raise invalid(
                    "invalid_filter", f"{where}: {field.name} takes a number", field=field.name
                )
            return Decimal(value.strip())
        return Decimal(str(value))
    if field.type == "date":
        if not (isinstance(value, str) and _DAY.match(value)):
            raise invalid(
                "invalid_filter",
                f"{where}: {field.name} takes a day YYYY-MM-DD",
                field=field.name,
            )
        try:
            date.fromisoformat(value)
        except ValueError:
            raise invalid(
                "invalid_filter", f"{where}: {value!r} is no day", field=field.name
            ) from None
        return value
    if field.type == "text" and op == "prefix" and not isinstance(value, str):
        raise invalid("invalid_filter", f"{where}: a prefix is a string", field=field.name)
    if isinstance(value, str) and len(value) > MAX_TEXT_VALUE:
        raise invalid(
            "invalid_filter",
            f"{where}: a value is at most {MAX_TEXT_VALUE} characters",
            field=field.name,
        )
    return value


def conditions(
    raw: Sequence[Mapping[str, Any]] | None, declared: Mapping[str, DeclaredField]
) -> list[Condition]:
    """``filter`` of the request against the filters of the block.

    A field the block did not declare is ``undeclared_filter``; an operator
    its type does not take, a value of the wrong type — ``invalid_filter``.
    """
    out: list[Condition] = []
    for index, item in enumerate(raw or ()):
        where = f"filter[{index}]"
        name, op, value = str(item.get("field")), str(item.get("op")), item.get("value")
        field = declared.get(name)
        if field is None:
            raise invalid(
                "undeclared_filter",
                f"{where}: the block declares no filter {name!r}",
                field=name[:200],
                declared=sorted(declared),
            )
        if op not in FILTER_OPS.get(field.type, OPS):
            raise invalid(
                "invalid_filter",
                f"{where}: a {field.type} filter takes {', '.join(FILTER_OPS[field.type])}",
                field=name,
            )
        if op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise invalid(
                    "invalid_filter",
                    f"{where}: in takes a list of 1 to {MAX_IN_VALUES} values",
                    field=name,
                )
            typed: Any = tuple(_typed(field, op, v, where) for v in value)
        else:
            typed = _typed(field, op, value, where)
        out.append(Condition(field, op, typed))
    return out


def orders(
    raw: Sequence[Mapping[str, Any]] | None, block: QueryBlock
) -> list[tuple[DeclaredField, str]]:
    """``sort`` of the request against the sortable fields; none — the package's own order."""
    if not raw:
        return default_sort(block)
    if block.kind not in PAGED_BLOCKS:
        raise invalid(
            "undeclared_sort",
            f"block {block.index} ({block.kind}) is not sorted",
            field=str(raw[0].get("field"))[:200],
        )
    declared = sort_fields(block)
    out: list[tuple[DeclaredField, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        name = str(item.get("field"))
        field = declared.get(name)
        if field is None:
            raise invalid(
                "undeclared_sort",
                f"sort[{index}]: the block declares no sort by {name!r}",
                field=name[:200],
                declared=sorted(declared),
            )
        if name not in seen:
            seen.add(name)
            out.append((field, str(item.get("dir") or "asc")))
    return out


_PARAM_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "uuid": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "date": lambda v: isinstance(v, str) and _DAY.match(v) is not None,
    "datetime": lambda v: isinstance(v, str) and _datetime(v) is not None,
}


def _datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def params(raw: Mapping[str, Any] | None, declared: Mapping[str, Any]) -> dict[str, Any]:
    """``params`` of the request against the params of the view and their types.

    A param the view does not declare is ``unknown_param``, a required one not
    given ``missing_param``, a value of another type ``invalid_param``. A
    ``uuid`` that is no UUID is left as it is: it names no record, and the
    record it does not name is answered as a missing one.
    """
    given = dict(raw or {})
    for name in sorted(set(given) - set(declared)):
        raise invalid(
            "unknown_param",
            f"the view declares no param {name!r}",
            param=name[:200],
            declared=sorted(declared),
        )
    out: dict[str, Any] = {}
    for name, spec in sorted(declared.items()):
        value = given.get(name)
        if value is None:
            if spec.get("required"):
                raise invalid("missing_param", f"param {name} is required", param=name)
            continue
        kind = str(spec.get("type") or "string")
        check = _PARAM_CHECKS.get(kind)
        if check is not None and not check(value):
            raise invalid("invalid_param", f"param {name} is a {kind}", param=name)
        out[name] = value
    return out


def as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def limit_of(limit: int | None, block: QueryBlock) -> int:
    """The page size: asked, else the block's ``pageSize``, else the default; at most the cap."""
    wanted = limit if limit is not None else block.spec.get("pageSize") or DEFAULT_LIMIT
    return max(1, min(int(wanted), MAX_LIMIT))


# --- an instance as the expressions of a view read it ----------------------------------------


def current_stage(stages: Mapping[str, Any] | None, order: Sequence[str]) -> str | None:
    """The stage an instance is at: the first active one, else the last completed one.

    ``order`` — the stage ids of the process in their order (its latest
    version); a stage the process no longer has is not one a view shows.
    """
    states = {
        str(sid): str((record or {}).get("state") or "")
        for sid, record in (stages or {}).items()
        if isinstance(record, Mapping)
    }
    known = [s for s in order if s in states]
    for sid in known:
        if states[sid] == "active":
            return sid
    for sid in reversed(known):
        if states[sid] == "completed":
            return sid
    return None


def stage_values(stages: Mapping[str, Any] | None, order: Sequence[str]) -> dict[str, Any]:
    """``stage.<id>.{active,completed}`` of an instance, as the engine gives them."""
    records = stages or {}
    out: dict[str, Any] = {}
    for sid in order:
        state = str((records.get(sid) or {}).get("state") or "")
        out[sid] = {"active": state == "active", "completed": state == "completed"}
    return out


def instance_values(
    *,
    instance_id: uuid.UUID,
    key: str,
    version: int,
    status: str,
    sla_state: str,
    data: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    started_at: datetime,
    stage_order: Sequence[str],
    now: datetime,
    param: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The variables of an expression of a view over one instance (CP-ADR-0080 §1).

    ``settings`` — the effective settings of the view's package (CP-ADR-0081 §6).
    """
    recorded = (state or {}).get("startedAt")
    return {
        "data": dict(data or {}),
        "stage": stage_values((state or {}).get("stages"), stage_order),
        "instance": {
            "id": str(instance_id),
            "key": key,
            "version": version,
            "startedAt": recorded or rfc3339(started_at),
            "clock": rfc3339(now),
        },
        "param": dict(param),
        "id": str(instance_id),
        "status": status,
        "slaState": sla_state,
        "settings": dict(settings or {}),
    }


def status_category(status: str) -> str:
    return STATUS_CATEGORY.get(status, "running")


# --- values as the console shows them --------------------------------------------------------


def rfc3339(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def iso_duration(value: timedelta) -> str:
    seconds = value.total_seconds()
    sign = "-" if seconds < 0 else ""
    text = f"{abs(seconds):f}".rstrip("0").rstrip(".")
    return f"{sign}PT{text}S"


def plain(value: Any) -> Any:
    """A value of an evaluation as JSON: times in ISO 8601, messages as objects."""
    if isinstance(value, Message):
        return json_format.MessageToDict(value, preserving_proto_field_name=True)
    if isinstance(value, datetime):
        return rfc3339(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return iso_duration(value)
    if isinstance(value, Decimal):
        return number(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return None
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def number(value: Decimal) -> int | float:
    """A decimal as a JSON number: an integer when it is one."""
    if value == value.to_integral_value() and abs(value) < Decimal(2**53):
        return int(value)
    return float(value)


def amount(value: Any) -> str | int | float | None:
    """The amount of ``money``: a string of decimal notation as it is, a number as it is."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        return value.strip() if _DECIMAL.match(value.strip()) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _time(value: Any, *, day: bool) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date().isoformat() if day else rfc3339(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


@dataclass(frozen=True)
class Shown:
    """What a value of a format needs beyond itself."""

    # The text of a value of a status (``<package>.fields.<name>.<value>``, else the value).
    label: Callable[[Any], str]
    # The category of the status of the record the value is of.
    category: str
    # The currency of an amount: the ``currency`` next to the field it was read from.
    currency: str | None = None


def shown(format: str | None, value: Any, how: Shown) -> Any:
    """``value`` as the console shows a value of ``format`` (CP-ADR-0080 amendment A)."""
    if value is None:
        return None
    if format == "money":
        found = amount(value)
        return None if found is None else {"amount": found, "currency": how.currency}
    if format in ("date", "datetime", "due"):
        return _time(value, day=format == "date")
    if format == "duration":
        if isinstance(value, timedelta):
            return iso_duration(value)
        return value if isinstance(value, str) and parse_iso_duration(value) else plain(value)
    if format == "principal":
        return str(value)
    if format == "status":
        return {"title": how.label(value), "category": how.category}
    if format == "link":
        text = str(value)
        return {"href": text, "title": text}
    return plain(value)


def text_of(
    key: str, messages: Mapping[str, Mapping[str, str]], locale: str, default: str
) -> str | None:
    """The text of a key of the dictionaries in ``locale``, else the default locale's."""
    for chosen in (locale, default):
        found = (messages.get(chosen) or {}).get(key)
        if isinstance(found, str):
            return found
    return None
