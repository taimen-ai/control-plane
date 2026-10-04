"""The request of ``POST /views/{key}:query`` against the view; values by format (CP-ADR-0080).

Pure functions of ``domain/view_query.py``: the block by its index, the
filters, sorts and params the view declares, the stage of an instance and
the shape of a value of each format — the edges included (empty, ``null``,
wrong types, repeats, values out of range).
"""

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from control_plane.domain import view_query as vq
from control_plane.domain.errors import ValidationError
from control_plane.domain.views import TEXT

FORM: dict[str, Any] = {
    "layout": [
        {
            "block": "table",
            "columns": [{"field": "data.a"}],
            "filters": ["data.kind", "data.amount", "data.day", "data.name", "stage"],
            "sort": [{"field": "data.amount", "dir": "desc"}, {"field": "data.name"}],
            "pageSize": 20,
        },
        {"block": "metrics", "items": [{"title": "t", "value": "count()"}]},
        {"block": "invoke", "label": "x", "skill": "s@1"},
        {"block": "component", "component": "c", "layout": []},
    ],
    "display": {
        "layout": [
            {
                "block": "table",
                "filters": [
                    {
                        "field": "kind",
                        "title": {TEXT: "p.fields.kind"},
                        "type": "enum",
                        "options": [{"value": "a", "title": "a"}, {"value": "b", "title": "b"}],
                    },
                    {"field": "amount", "title": "Amount", "type": "number"},
                    {"field": "day", "title": "Day", "type": "date"},
                    {"field": "name", "title": "Name", "type": "text"},
                    {"field": "stage", "title": "Stage", "type": "enum"},
                ],
            },
            {"block": "metrics"},
            {"block": "invoke"},
            {"block": "component"},
        ]
    },
}


def _table() -> vq.QueryBlock:
    return vq.block_of(FORM, 0)


def _code(call: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        call()
    return caught.value.code


# --- the block ------------------------------------------------------------------------------


@pytest.mark.parametrize("index", [-1, 4, 50])
def test_a_block_out_of_the_layout_is_unknown(index: int) -> None:
    assert _code(lambda: vq.block_of(FORM, index)) == "unknown_block"


@pytest.mark.parametrize("index", [2, 3])
def test_invoke_and_component_draw_no_data_of_their_own(index: int) -> None:
    assert _code(lambda: vq.block_of(FORM, index)) == "block_without_data"


def test_an_empty_form_has_no_block() -> None:
    assert _code(lambda: vq.block_of({}, 0)) == "unknown_block"


def test_the_declared_filters_carry_their_paths_types_and_options() -> None:
    fields = vq.filter_fields(_table())
    assert fields["kind"] == vq.DeclaredField("kind", "data.kind", "enum", ("a", "b"))
    assert fields["stage"].path == "stage"
    assert fields["amount"].type == "number" and fields["amount"].options is None
    assert vq.filter_fields(vq.block_of(FORM, 1)) == {}


# --- filter ---------------------------------------------------------------------------------------


def _conditions(*raw: dict[str, Any]) -> list[vq.Condition]:
    return vq.conditions(list(raw), vq.filter_fields(_table()))


def test_no_filter_is_no_condition() -> None:
    assert vq.conditions(None, {}) == []
    assert vq.conditions([], vq.filter_fields(_table())) == []


def test_values_are_typed_by_their_filter() -> None:
    amount, day, name, kinds = _conditions(
        {"field": "amount", "op": "gte", "value": "100.50"},
        {"field": "day", "op": "lte", "value": "2026-10-31"},
        {"field": "name", "op": "prefix", "value": "Ab"},
        {"field": "kind", "op": "in", "value": ["a", "b"]},
    )
    assert amount.value == Decimal("100.50")
    assert day.value == "2026-10-31"
    assert (name.op, name.value) == ("prefix", "Ab")
    assert kinds.value == ("a", "b")
    [number] = _conditions({"field": "amount", "op": "eq", "value": 3})
    assert number.value == Decimal(3)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"field": "secret", "op": "eq", "value": "x"}, "undeclared_filter"),
        ({"field": "", "op": "eq", "value": "x"}, "undeclared_filter"),
        ({"field": None, "op": "eq", "value": "x"}, "undeclared_filter"),
        ({"field": "kind", "op": "prefix", "value": "a"}, "invalid_filter"),
        ({"field": "kind", "op": "gte", "value": "a"}, "invalid_filter"),
        ({"field": "day", "op": "prefix", "value": "2026"}, "invalid_filter"),
        ({"field": "amount", "op": "gte", "value": True}, "invalid_filter"),
        ({"field": "amount", "op": "gte", "value": "many"}, "invalid_filter"),
        ({"field": "amount", "op": "gte", "value": None}, "invalid_filter"),
        ({"field": "amount", "op": "gte", "value": math.nan}, "invalid_filter"),
        ({"field": "amount", "op": "gte", "value": {"$gt": 1}}, "invalid_filter"),
        ({"field": "day", "op": "gte", "value": "31.10.2026"}, "invalid_filter"),
        ({"field": "day", "op": "gte", "value": "2026-02-30"}, "invalid_filter"),
        ({"field": "day", "op": "gte", "value": 20261031}, "invalid_filter"),
        ({"field": "name", "op": "prefix", "value": 12}, "invalid_filter"),
        ({"field": "name", "op": "eq", "value": "x" * 501}, "invalid_filter"),
        ({"field": "kind", "op": "in", "value": []}, "invalid_filter"),
        ({"field": "kind", "op": "in", "value": "a"}, "invalid_filter"),
        ({"field": "kind", "op": "in", "value": ["a"] * 101}, "invalid_filter"),
        ({"field": "kind", "op": "in", "value": [["a"]]}, "invalid_filter"),
        ({"field": "kind", "op": "eq", "value": ["a"]}, "invalid_filter"),
    ],
)
def test_what_the_filter_does_not_take_is_refused(raw: dict[str, Any], code: str) -> None:
    assert _code(lambda: _conditions(raw)) == code


def test_a_condition_repeated_is_kept_as_written() -> None:
    once = {"field": "kind", "op": "eq", "value": "a"}
    assert len(_conditions(once, once)) == 2


# --- sort -----------------------------------------------------------------------------------------


def test_no_sort_is_the_order_the_package_wrote() -> None:
    assert [(f.path, d) for f, d in vq.orders(None, _table())] == [
        ("data.amount", "desc"),
        ("data.name", "asc"),
    ]
    assert vq.orders([], vq.block_of(FORM, 1)) == []


def test_a_sort_names_declared_fields_once_each() -> None:
    found = vq.orders(
        [{"field": "name", "dir": "desc"}, {"field": "amount"}, {"field": "name"}], _table()
    )
    assert [(f.path, d) for f, d in found] == [("data.name", "desc"), ("data.amount", "asc")]


@pytest.mark.parametrize("field", ["kind", "secret", "data.amount", ""])
def test_a_sort_the_block_does_not_declare_is_refused(field: str) -> None:
    assert _code(lambda: vq.orders([{"field": field}], _table())) == "undeclared_sort"


def test_a_block_that_is_no_page_is_not_sorted() -> None:
    metrics = vq.block_of(FORM, 1)
    assert _code(lambda: vq.orders([{"field": "amount"}], metrics)) == "undeclared_sort"


# --- params ---------------------------------------------------------------------------------------

DECLARED = {
    "id": {"type": "uuid", "required": True},
    "n": {"type": "integer"},
    "x": {"type": "number"},
    "on": {"type": "boolean"},
    "day": {"type": "date"},
    "at": {"type": "datetime"},
    "s": {"type": "string"},
}


def test_params_by_their_types() -> None:
    given = {
        "id": "not-a-uuid",
        "n": 3,
        "x": 1.5,
        "on": False,
        "day": "2026-10-03",
        "at": "2026-10-03T10:00:00Z",
        "s": "",
    }
    assert vq.params(given, DECLARED) == given
    # An optional param given null is not given.
    assert vq.params({"id": "a", "n": None}, DECLARED) == {"id": "a"}
    assert vq.params(None, {}) == {}


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ({}, "missing_param"),
        ({"id": None}, "missing_param"),
        ({"id": "a", "other": 1}, "unknown_param"),
        ({"id": 7}, "invalid_param"),
        ({"id": "a", "n": True}, "invalid_param"),
        ({"id": "a", "n": 1.5}, "invalid_param"),
        ({"id": "a", "x": "1"}, "invalid_param"),
        ({"id": "a", "on": "true"}, "invalid_param"),
        ({"id": "a", "day": "03.10.2026"}, "invalid_param"),
        ({"id": "a", "at": "yesterday"}, "invalid_param"),
    ],
)
def test_params_the_view_does_not_take_are_refused(given: dict[str, Any], code: str) -> None:
    assert _code(lambda: vq.params(given, DECLARED)) == code


def test_a_uuid_param_that_is_no_uuid_names_no_record() -> None:
    assert vq.as_uuid("not-a-uuid") is None
    assert vq.as_uuid(None) is None
    ident = uuid.uuid4()
    assert vq.as_uuid(str(ident)) == ident


@pytest.mark.parametrize(
    ("asked", "expected"), [(None, 20), (5, 5), (0, 1), (-3, 1), (500, vq.MAX_LIMIT)]
)
def test_the_page_size(asked: int | None, expected: int) -> None:
    assert vq.limit_of(asked, _table()) == expected
    assert vq.limit_of(None, vq.block_of(FORM, 1)) == vq.DEFAULT_LIMIT


# --- the stage and the status of an instance ------------------------------------------------------

ORDER = ("intake", "work", "archive")


@pytest.mark.parametrize(
    ("stages", "expected"),
    [
        ({"intake": {"state": "completed"}, "work": {"state": "active"}}, "work"),
        ({"intake": {"state": "active"}, "work": {"state": "active"}}, "intake"),
        ({"intake": {"state": "completed"}, "work": {"state": "completed"}}, "work"),
        ({"intake": {"state": "available"}}, None),
        ({"gone": {"state": "active"}}, None),
        ({"intake": "active"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_stage_of_an_instance(stages: Any, expected: str | None) -> None:
    assert vq.current_stage(stages, ORDER) == expected


def test_the_stage_values_cover_every_stage_of_the_process() -> None:
    assert vq.stage_values({"work": {"state": "active"}}, ORDER) == {
        "intake": {"active": False, "completed": False},
        "work": {"active": True, "completed": False},
        "archive": {"active": False, "completed": False},
    }


@pytest.mark.parametrize(
    ("status", "category"),
    [
        ("running", "running"),
        ("suspended", "suspended"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("unknown", "running"),
    ],
)
def test_the_category_of_a_status(status: str, category: str) -> None:
    assert vq.status_category(status) == category


# --- values by format -----------------------------------------------------------------------------

HOW = vq.Shown(label=lambda v: f"<{v}>", category="suspended", currency="RUB")


@pytest.mark.parametrize(
    ("format", "value", "expected"),
    [
        ("money", "184500.00", {"amount": "184500.00", "currency": "RUB"}),
        ("money", Decimal("184500.50"), {"amount": "184500.50", "currency": "RUB"}),
        ("money", 1.5, {"amount": 1.5, "currency": "RUB"}),
        ("money", "abc", None),
        ("money", True, None),
        ("money", math.inf, None),
        ("date", datetime(2026, 10, 3, 23, 0, tzinfo=UTC), "2026-10-03"),
        ("date", "2026-10-03", "2026-10-03"),
        ("datetime", datetime(2026, 10, 3, 7, 0, tzinfo=UTC), "2026-10-03T07:00:00Z"),
        ("due", date(2026, 10, 3), "2026-10-03"),
        ("due", 12, None),
        ("duration", timedelta(hours=1, seconds=1.5), "PT3601.5S"),
        ("duration", "P3D", "P3D"),
        ("principal", uuid.UUID(int=1), str(uuid.UUID(int=1))),
        ("status", "work", {"title": "<work>", "category": "suspended"}),
        (
            "link",
            "https://example.org/a",
            {"href": "https://example.org/a", "title": "https://example.org/a"},
        ),
        ("number", Decimal("2.50"), 2.5),
        ("number", Decimal("2"), 2),
        ("percent", 0.25, 0.25),
        ("text", "x", "x"),
        (None, {"a": datetime(2026, 1, 1, tzinfo=UTC)}, {"a": "2026-01-01T00:00:00Z"}),
        (None, b"bytes", None),
        (None, math.nan, None),
        ("money", None, None),
        ("status", None, None),
    ],
)
def test_a_value_as_its_format_shows_it(format: str | None, value: Any, expected: Any) -> None:
    assert vq.shown(format, value, HOW) == expected


def test_an_amount_with_no_currency_next_to_it() -> None:
    how = vq.Shown(label=str, category="running")
    assert vq.shown("money", "1", how) == {"amount": "1", "currency": None}


def test_the_values_of_an_instance_as_the_engine_lays_them_out() -> None:
    ident = uuid.uuid4()
    started = datetime(2026, 10, 1, tzinfo=UTC)
    values = vq.instance_values(
        instance_id=ident,
        key="k-1",
        version=2,
        status="suspended",
        sla_state="warning",
        data=None,
        state={"stages": {"work": {"state": "active"}}},
        started_at=started,
        stage_order=ORDER,
        now=started + timedelta(days=1),
        param={"id": str(ident)},
    )
    assert values["data"] == {}
    assert values["instance"] == {
        "id": str(ident),
        "key": "k-1",
        "version": 2,
        "startedAt": "2026-10-01T00:00:00Z",
        "clock": "2026-10-02T00:00:00Z",
    }
    assert (values["id"], values["status"], values["slaState"]) == (
        str(ident),
        "suspended",
        "warning",
    )
    assert values["stage"]["work"] == {"active": True, "completed": False}
    assert values["param"] == {"id": str(ident)}
