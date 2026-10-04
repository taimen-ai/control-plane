"""The CEL profile ``cp/1`` (CP-ADR-0075; process-packages P005).

Types from JSON Schema, errors found when an expression is compiled (with
their position and the path of the expression), ``cal.*`` over the working-day
calendar, the extensions, no current time, the cost limit and the fields an
expression reads.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.domain.calendar import Calendar
from control_plane.domain.cel_profile import (
    DEFAULT_COST_LIMIT,
    EXPRESSION_COST_EXCEEDED,
    EXPRESSION_ERROR,
    EXPRESSION_SYNTAX_ERROR,
    EXPRESSION_TOO_COMPLEX,
    EXPRESSION_TYPE_ERROR,
    MAX_EXPRESSION_LENGTH,
    Environment,
    ExpressionError,
    environment,
    parse_iso_duration,
)
from control_plane.domain.event_catalog import current_version, schema_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
RU = Calendar.from_spec(
    yaml.safe_load((FIXTURES / "ru-2024.calendar.yaml").read_text("utf-8"))["spec"]
)

DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "procurement": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "submissionDeadline": {"type": "string", "format": "date-time"},
                "amount": {"type": "number"},
                "lots": {"type": "integer"},
                "review": {"type": "string", "format": "duration"},
                "okpd": {"type": "array", "items": {"type": "string"}},
                "note": {"type": ["string", "null"]},
                "extra": {"type": "object"},
            },
        },
        "approved": {"type": "boolean"},
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"price": {"type": "number"}}},
        },
    },
}
VALUES: dict[str, Any] = {
    "data": {
        "procurement": {
            "number": "0373100000124000001",
            "submissionDeadline": "2024-05-06T15:00:00+03:00",
            "amount": 1500000.5,
            "lots": 2,
            "review": "P3D",
            "okpd": ["62.01", "62.02"],
            "note": None,
            "extra": {"source": "eis"},
        },
        "approved": True,
        "items": [{"price": 10.0}, {"price": 32.5}],
    },
    "event": {"type": "tender.published", "time": "2024-05-02T09:00:00Z"},
    "instance": {"id": "i-1", "clock": "2024-05-02T09:00:00Z"},
}


def _env(**kwargs: Any) -> Environment:
    return environment(data=DATA, **kwargs)


def _value(expression: str, values: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    env = _env(calendar=kwargs.pop("calendar", None))
    return env.compile(expression).evaluate(values or VALUES, **kwargs).value


def _error(expression: str, env: Environment | None = None, **kwargs: Any) -> ExpressionError:
    with pytest.raises(ExpressionError) as caught:
        (env or _env()).compile(expression, **kwargs)
    return caught.value


# --- types and errors at compile time -------------------------------------------------


def test_a_type_error_is_found_when_compiled_with_its_path_and_position() -> None:
    error = _error(
        "data.procurement.submisionDeadline < event.time",
        path="/spec/stages/0/steps/1/human/due/at",
    )
    assert error.code == EXPRESSION_TYPE_ERROR
    assert (error.line, error.column) == (1, 17)
    assert error.path == "/spec/stages/0/steps/1/human/due/at"
    assert "undefined field 'submisionDeadline'" in error.message
    assert "data.procurement" in error.message
    assert error.finding() == {
        "code": "expression_type_error",
        "severity": "error",
        "path": "/spec/stages/0/steps/1/human/due/at",
        "message": error.message,
        "hint": "data.procurement has submissionDeadline",
    }


def test_operand_types_come_from_the_schema() -> None:
    assert _error('data.procurement.amount + "x"').code == EXPRESSION_TYPE_ERROR
    assert _error("data.procurement.number > 3").code == EXPRESSION_TYPE_ERROR
    error = _error("data.approved &&\n  data.procurement.lots")
    assert (error.code, error.line, error.column) == (EXPRESSION_TYPE_ERROR, 1, 15)
    assert _error("data.items[0].cost").code == EXPRESSION_TYPE_ERROR
    env = _env()
    assert env.compile("data.procurement.submissionDeadline").output_type == "TIMESTAMP"
    assert env.compile("data.procurement.review").output_type == "DURATION"
    assert env.compile("data.procurement.lots + 1").output_type == "INT"
    assert env.compile("data.procurement.okpd").output_type == "LIST<STRING>"


def test_a_syntax_error_has_its_position() -> None:
    error = _error("data.approved &&")
    assert error.code == EXPRESSION_SYNTAX_ERROR
    assert (error.line, error.column) == (1, 17)


def test_an_object_without_properties_is_a_map_of_dyn() -> None:
    assert _value('data.procurement.extra.source == "eis"') is True
    assert _value('data.procurement.extra.?missing.orValue("none")') == "none"


def test_values_read_as_in_json() -> None:
    assert _value("data.procurement.note == null") is True
    assert _value("data.procurement.lots * 2") == 4
    assert _value("data.items.map(i, i.price)") == [10.0, 32.5]
    missing = {"data": {"procurement": {}}}
    assert _value("data.procurement.lots == null", missing) is True
    with pytest.raises(ExpressionError) as caught:
        _value("data.procurement.lots + 1", missing)
    assert caught.value.code == EXPRESSION_ERROR


def test_an_unset_timestamp_is_an_error_unless_tested() -> None:
    missing = {"data": {"procurement": {}}}
    with pytest.raises(ExpressionError) as caught:
        _value('data.procurement.submissionDeadline + duration("P1D")', missing)
    assert caught.value.details["field"] == "data.procurement.submissionDeadline"
    guarded = (
        "has(data.procurement.submissionDeadline) "
        "? data.procurement.submissionDeadline : instance.clock"
    )
    assert _value(guarded, {**missing, "instance": {"clock": "2024-05-02T09:00:00Z"}}) == (
        datetime(2024, 5, 2, 9, tzinfo=UTC)
    )


def test_a_value_that_does_not_match_the_schema_is_an_evaluation_error() -> None:
    with pytest.raises(ExpressionError) as caught:
        _value("data.approved", {"data": {"approved": "yes"}})
    assert caught.value.code == EXPRESSION_ERROR


def test_an_optional_result_is_refused() -> None:
    error = _error("data.procurement.extra.?source")
    assert error.code == EXPRESSION_TYPE_ERROR
    assert "orValue" in error.message


def test_the_event_payload_takes_the_catalog_schema() -> None:
    payload = schema_for("task.verification_failed", current_version("task.verification_failed"))
    env = environment(event_payload=payload)
    assert env.compile("event.payload.blocked").output_type == "BOOL"
    assert _error("event.payload.blokced", env).hint == "event.payload has blocked"
    program = env.compile("event.payload.attempt > 1 && !event.payload.blocked")
    assert program.evaluate({"event": {"payload": {"attempt": 3, "blocked": False}}}).value is True


def test_stages_are_fields() -> None:
    env = environment(stages=["intake", "review"])
    program = env.compile("stage.intake.completed && !stage.review.active")
    assert program.evaluate({"stage": {"intake": {"completed": True}}}).value is True
    assert _error("stage.revew.active", env).hint == "stage has review"
    dashed = environment(stages=["prep-docs"])
    program = dashed.compile('stage["prep-docs"].completed')
    assert program.evaluate({"stage": {"prep-docs": {"completed": True}}}).value is True


# --- deterministic ------------------------------------------------------------------------


def test_there_is_no_current_time() -> None:
    error = _error("now() > event.time")
    assert error.code == EXPRESSION_TYPE_ERROR
    assert error.hint is not None and "event.time" in error.hint


def test_the_same_inputs_give_the_same_value_and_cost() -> None:
    program = _env().compile("data.items.filter(i, i.price > 20.0).size() * 2")
    first, second = program.evaluate(VALUES), program.evaluate(VALUES)
    assert first == second
    assert first.value == 2


# --- functions ---------------------------------------------------------------------------


def test_cal_add_workdays_over_the_calendar() -> None:
    # 2024-05-06 (Monday) + 3 working days: 7, 8 May, then the 9-10 May holidays
    # and the weekend, then Monday 13 May.
    expression = 'cal.addWorkdays(data.procurement.submissionDeadline, 3, "ru")'
    result = _env().compile(expression).evaluate(VALUES, calendars={"ru": RU})
    assert result.value == datetime(2024, 5, 13, 12, tzinfo=UTC)
    assert result.provisional is False


def test_cal_on_a_provisional_year_marks_the_result() -> None:
    values = {"event": {"time": "2024-12-27T09:00:00Z"}}
    result = (
        _env()
        .compile('cal.addWorkdays(event.time, 5, "ru")')
        .evaluate(values, calendars={"ru": RU})
    )
    assert result.provisional is True
    unpublished = {"event": {"time": "2031-03-03T09:00:00Z"}}
    result = (
        _env()
        .compile('cal.isWorkday(event.time, "ru")')
        .evaluate(unpublished, calendars={"ru": RU})
    )
    assert (result.value, result.provisional) == (True, True)


def test_the_calendar_key_is_optional_with_the_process_calendar() -> None:
    assert _error("cal.isWorkday(event.time)").code == EXPRESSION_TYPE_ERROR
    env = _env(calendar="ru")
    count = env.compile(
        "cal.workdaysBetween(event.time, data.procurement.submissionDeadline)"
    ).evaluate(VALUES, calendars={"ru": RU})
    # 2 May (Thu) to 6 May (Mon): 3 and 6 May.
    assert count.value == 2


def test_a_calendar_the_evaluation_lacks_is_an_error() -> None:
    with pytest.raises(ExpressionError) as caught:
        _env().compile('cal.isWorkday(event.time, "kz")').evaluate(VALUES, calendars={"ru": RU})
    assert caught.value.code == EXPRESSION_ERROR
    assert caught.value.details["reason"] == "calendar_missing"


def test_string_and_list_extensions_and_macros() -> None:
    assert _value('data.procurement.okpd.join(", ")') == "62.01, 62.02"
    assert _value('"A,b".lowerAscii().split(",")') == ["a", "b"]
    assert _value('data.procurement.number.startsWith("0373")') is True
    assert _value("[3, 1, 2].sort()") == [1, 2, 3]
    assert _value("[[1], [2, 3]].flatten()") == [1, 2, 3]
    assert _value("[1, 1, 2].distinct()") == [1, 2]
    assert _value("data.procurement.okpd.slice(1, 2)") == ["62.02"]
    assert _value('data.procurement.okpd.exists(c, c.startsWith("62"))') is True
    assert _value("data.items.all(i, i.price > 5.0)") is True
    assert _value("cel.bind(n, data.procurement.lots, n * n)") == 4


AMOUNTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "amount": {"type": "string"},
        "count": {"type": "integer"},
        "rate": {"type": "number"},
    },
}


@pytest.mark.parametrize(
    ("expression", "data", "value"),
    [
        ("decimal(data.amount)", {"amount": "1500000.50"}, 1500000.5),
        ("decimal(data.amount)", {"amount": " -3 "}, -3.0),
        ("decimal(data.amount)", {"amount": "1e3"}, 1000.0),
        ("decimal(data.amount)", {"amount": ".5"}, 0.5),
        ("decimal(data.count)", {"count": 7}, 7.0),
        ("decimal(data.rate)", {"rate": 2.5}, 2.5),
        ("decimal(data.amount) + decimal(data.count)", {"amount": "0.5", "count": 1}, 1.5),
    ],
)
def test_decimal_reads_a_number_in_decimal_notation(
    expression: str, data: dict[str, Any], value: float
) -> None:
    program = environment(data=AMOUNTS).compile(expression)
    assert program.output_type == "DOUBLE"
    assert program.evaluate({"data": data}).value == value


@pytest.mark.parametrize("amount", ["", "abc", "1,5", "1 000", "nan", "inf", "0x10", "1e999"])
def test_decimal_of_what_is_no_number_is_an_evaluation_error(amount: str) -> None:
    program = environment(data=AMOUNTS).compile("decimal(data.amount)")
    with pytest.raises(ExpressionError) as caught:
        program.evaluate({"data": {"amount": amount}})
    assert caught.value.code == EXPRESSION_ERROR


def test_decimal_of_an_absent_value_is_an_evaluation_error() -> None:
    program = environment(data=AMOUNTS).compile("decimal(data.amount)")
    with pytest.raises(ExpressionError) as caught:
        program.evaluate({"data": {}})
    assert caught.value.code == EXPRESSION_ERROR


@pytest.mark.parametrize("expression", ["decimal(true)", "decimal([1])", "decimal()"])
def test_decimal_takes_a_string_or_a_number(expression: str) -> None:
    with pytest.raises(ExpressionError) as caught:
        environment(data=AMOUNTS).compile(expression)
    assert caught.value.code == EXPRESSION_TYPE_ERROR


def test_a_scalar_binding_is_typed() -> None:
    env = environment(bindings={"status": {"type": "string"}, "n": {"type": "integer"}})
    assert env.compile("status").output_type == "STRING"
    assert (
        env.compile('status == "running" && n > 1').evaluate({"status": "running", "n": 2}).value
        is True
    )
    with pytest.raises(ExpressionError) as caught:
        env.compile("status == 1")
    assert caught.value.code == EXPRESSION_TYPE_ERROR
    # A binding without a schema stays dyn, as the translation of earlier syntaxes needs.
    assert environment(bindings={"input": None}).compile("input").output_type == "DYN"


def test_iso_durations() -> None:
    assert _value('duration("P3D") == duration("72h")') is True
    assert _value('duration("PT1H30M") + duration("-P1W")') == -timedelta(days=7, minutes=-90)
    assert _value('data.procurement.review == duration("P3D")') is True
    assert parse_iso_duration("P1M") is None
    error = _error('event.time + duration("P1M")')
    assert (error.code, error.line, error.column) == (EXPRESSION_TYPE_ERROR, 1, 24)


def test_positions_after_a_duration_literal_are_those_of_the_source() -> None:
    error = _error('duration("P3D") + data.procurement.nothing')
    assert (error.line, error.column) == (1, 35)


# --- cost -----------------------------------------------------------------------------------


def test_the_cost_limit_stops_an_evaluation_before_it_runs() -> None:
    env = environment(data={"type": "object", "properties": {"xs": {"type": "array"}}})
    program = env.compile("data.xs.map(a, data.xs.map(b, data.xs.filter(c, a == b && b == c)))")
    small = {"data": {"xs": list(range(5))}}
    assert program.evaluate(small).cost < DEFAULT_COST_LIMIT
    large = {"data": {"xs": list(range(200))}}
    with pytest.raises(ExpressionError) as caught:
        program.evaluate(large)
    assert caught.value.code == EXPRESSION_COST_EXCEEDED
    assert caught.value.details["limit"] == DEFAULT_COST_LIMIT
    assert program.cost(large) > DEFAULT_COST_LIMIT
    with pytest.raises(ExpressionError):
        program.evaluate(small, cost_limit=10)


def test_the_cost_grows_with_strings_that_grow() -> None:
    env = environment(data={"type": "object", "properties": {"s": {"type": "string"}}})
    program = env.compile('data.s.replace("a", data.s).replace("a", data.s)')
    assert program.cost({"data": {"s": "a" * 10}}) < program.cost({"data": {"s": "a" * 1000}})


def test_what_is_visible_at_compile_time_is_refused_early() -> None:
    assert _error("1" + " + 1" * (MAX_EXPRESSION_LENGTH // 4)).code == EXPRESSION_TOO_COMPLEX
    nested = "[1].all(a, [1].all(b, [1].all(c, [1].all(d, true))))"
    assert _error(nested).code == EXPRESSION_TOO_COMPLEX


# --- reads ------------------------------------------------------------------------------


def test_the_fields_an_expression_reads() -> None:
    program = _env().compile(
        "has(data.procurement.note) && data.items.exists(i, i.price > data.procurement.amount)"
        ' && cal.isWorkday(event.time, "ru") && data.procurement.extra["source"] == "eis"'
    )
    assert program.reads == (
        "data.items",
        "data.procurement.amount",
        "data.procurement.extra.source",
        "data.procurement.note",
        "event.time",
    )
    assert program.guarded == frozenset({"data.procurement.note"})
