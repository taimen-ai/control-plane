"""Amounts of working units of a deadline given by an expression (CP-ADR-0081 Г1-Г4).

``due.workdays``/``due.workhours`` and those of ``warnBefore`` take a number or
``{expr: <cel>}``. The expression is computed once, when the step is entered,
with the values of the process (``settings`` included); it must give a
non-negative integer, otherwise the deadline is ``process.sla_failed`` with an
expression error. The recipe keeps the number it gave: the warning, a pause
and a new calendar version count it as they count a number, and the journal
records it (``timer_set.computed``) so that a replay compares it too. The plan
checks the type of the expression and its references to ``settings``.
"""

import copy
import json
from dataclasses import replace
from datetime import datetime
from typing import Any

import jsonschema
import pytest

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain import process_replay
from control_plane.domain import process_sandbox as sandbox
from control_plane.domain import process_sla as sla
from control_plane.domain.calendar import Calendar
from control_plane.domain.process_engine import Definition, Input
from control_plane.domain.settings_refs import NONE, REF_TYPE, REF_UNKNOWN, SettingsScope
from tests.unit.test_process_engine import CATALOG, INSTANCE, T0, spec
from tests.unit.test_process_sla import WITH_HOURS, HoursRun, _fire, _step
from tests.unit.test_process_sla_pause import ENTERED, PAUSED, RESUMED, RU
from tests.unit.test_process_sla_schema import _with_due

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reviewDueWorkdays": {"type": "integer", "default": 2},
        "warnHours": {"type": "integer", "default": 4},
        "limit": {"type": "number", "default": 1.5},
        "label": {"type": "string", "default": "x"},
    },
}
SCOPE = SettingsScope("sample", SCHEMA, 1)
SETTINGS = {"reviewDueWorkdays": 2, "warnHours": 4}
DUE = "{workdays: {expr: settings.reviewDueWorkdays}, warnBefore: {workhours: 4}}"
AT = "/spec/stages/0/steps/0/human/due"


class SettingsRun(HoursRun):
    """A run of a definition that reads the settings of its package, given with every input."""

    calendars: dict[str, Calendar] = WITH_HOURS

    def __init__(self, body: str, settings: dict[str, Any] | None = None) -> None:
        # Not Run's own build: that one knows no settings.
        self.definition = _definition(body)
        self.state = None
        self.clock = T0
        self.records = []
        self.events_made = 0
        self.settings = dict(SETTINGS if settings is None else settings)

    def feed(
        self,
        kind: str,
        body: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
        actor: str | None = None,
    ) -> tuple[list[pe.Decision], list[pe.Intent]]:
        self.clock = at or self.clock
        given = Input(kind, self.clock, body or {}, actor, self.calendars, settings=self.settings)
        state, decisions, intents = pe.step(self.definition, self.state, given)
        self.state = json.loads(json.dumps(state, sort_keys=True))
        self.records.append((given, decisions, intents))
        return decisions, intents


def _definition(body: str, scope: SettingsScope = SCOPE) -> Definition:
    return Definition.build(
        "test", pd.normalized_spec(spec(body)), replace(CATALOG, settings=scope)
    )


def _due(due: str) -> str:
    return _step(f"          due: {due}")


def _codes(due: str, scope: SettingsScope = SCOPE) -> set[tuple[str, str]]:
    checked = pd.check_process(
        "test", pd.normalized_spec(spec(_due(due))), replace(CATALOG, settings=scope)
    )
    return {(p.code, p.path) for p in checked.errors}


# --- recipes ------------------------------------------------------------------------------


def test_an_expression_amount_is_a_recipe_that_names_its_expression() -> None:
    value = {
        "workdays": {"expr": "settings.reviewDueWorkdays"},
        "warnBefore": {"workhours": {"expr": "2"}},
    }
    due = sla.due_recipe(value, AT, "ru")
    assert due == {"kind": "workdays", "expr": AT + "/workdays/expr", "calendar": "ru"}
    assert sla.warn_recipe(value, due, "ru", AT) == {
        "kind": "before",
        "due": due,
        "span": {"kind": "workhours", "expr": AT + "/warnBefore/workhours/expr", "calendar": "ru"},
    }
    done = sla.computed(due, 3)
    assert done == {"kind": "workdays", "n": 3, "calendar": "ru", "from": AT + "/workdays/expr"}
    assert sla.computed_amounts(
        {
            "kind": "before",
            "due": done,
            "span": sla.computed({"kind": "workhours", "expr": "/w", "calendar": "ru"}, 4),
        }
    ) == {AT + "/workdays/expr": 3, "/w": 4}
    # A number is a recipe as before, and records nothing computed.
    assert sla.computed_amounts(sla.due_recipe({"workdays": 2}, AT, "ru")) == {}


@pytest.mark.parametrize(
    ("value", "unit", "amount"),
    [(0, "workdays", 0), (3, "workdays", 3), (3.0, "workhours", 3), (1000, "workdays", 1000)],
)
def test_an_amount_is_a_non_negative_integer(value: Any, unit: str, amount: int) -> None:
    assert sla.amount_of(value, unit, "/x") == amount


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (None, "workdays"),
        (True, "workdays"),
        ("3", "workdays"),
        (1.5, "workhours"),
        (-1, "workdays"),
        (1001, "workdays"),
        (10001, "workhours"),
        ([], "workdays"),
    ],
    ids=[
        "null",
        "bool",
        "string",
        "fraction",
        "negative",
        "too-many-days",
        "too-many-hours",
        "list",
    ],
)
def test_anything_else_is_an_expression_error(value: Any, unit: str) -> None:
    with pytest.raises(sla.DeadlineError) as raised:
        sla.amount_of(value, unit, "/x/workdays/expr")
    assert raised.value.code == "expression_error"
    assert raised.value.message.startswith("/x/workdays/expr: ")


# --- the schema ---------------------------------------------------------------------------

EXPR_FORMS: list[Any] = [
    {"workdays": {"expr": "settings.reviewDueWorkdays"}},
    {"workhours": {"expr": "settings.warnHours"}, "calendar": "ru"},
    {"workdays": {"expr": "settings.reviewDueWorkdays"}, "warnBefore": {"workhours": 4}},
    {"workdays": 5, "warnBefore": {"workdays": {"expr": "1"}}},
    {"workhours": 8, "warnBefore": {"workhours": {"expr": "settings.warnHours"}}},
]
BAD_EXPR_FORMS: list[Any] = [
    {"workdays": {}},
    {"workdays": {"expr": ""}},
    {"workdays": {"expr": 3}},
    {"workdays": {"expr": "1", "unit": "d"}},
    {"workdays": "settings.reviewDueWorkdays"},
    {"workdays": {"at": "1"}},
    {"workdays": 2, "warnBefore": {"workhours": {"expr": None}}},
    {"workdays": 2, "warnBefore": {"expr": "1"}},
]


@pytest.mark.parametrize("due", EXPR_FORMS)
def test_the_schema_takes_an_expression_amount_wherever_a_deadline_stands(due: Any) -> None:
    validator = jsonschema.Draft202012Validator(json.loads(pd.SCHEMA_FILE.read_text("utf-8")))
    for place, body in _with_due(due):
        assert validator.is_valid(body), place


@pytest.mark.parametrize("due", BAD_EXPR_FORMS)
def test_the_schema_refuses_a_malformed_expression_amount(due: Any) -> None:
    validator = jsonschema.Draft202012Validator(json.loads(pd.SCHEMA_FILE.read_text("utf-8")))
    for place, body in _with_due(due):
        assert not validator.is_valid(body), place


# --- the plan -----------------------------------------------------------------------------


def test_the_plan_takes_integer_expressions_of_settings_and_data() -> None:
    assert _codes(DUE) == set()
    assert _codes("{workhours: {expr: 'settings.warnHours * 2'}}") == set()
    assert _codes("{workdays: 3, warnBefore: {workdays: {expr: 'data.count'}}}") == set()
    assert _definition(_due(DUE)).reads_settings


@pytest.mark.parametrize(
    ("due", "code", "where"),
    [
        ("{workdays: {expr: \"'two'\"}}", "expression_type_error", "/workdays/expr"),
        ("{workdays: {expr: 'data.amount'}}", "expression_type_error", "/workdays/expr"),
        ("{workdays: {expr: 'data.deadline'}}", "expression_type_error", "/workdays/expr"),
        (
            "{workdays: 2, warnBefore: {workhours: {expr: 'true'}}}",
            "expression_type_error",
            "/warnBefore/workhours/expr",
        ),
        ("{workdays: {expr: 'settings.limit'}}", REF_TYPE, "/workdays/expr"),
        ("{workdays: {expr: 'settings.label'}}", REF_TYPE, "/workdays/expr"),
        ("{workdays: {expr: 'settings.nothing'}}", REF_UNKNOWN, "/workdays/expr"),
        ("{workhours: {expr: 'data.count +'}}", "expression_syntax_error", "/workhours/expr"),
    ],
    ids=[
        "string",
        "double",
        "timestamp",
        "bool-warning",
        "settings-number",
        "settings-string",
        "settings-unknown",
        "syntax",
    ],
)
def test_the_plan_refuses_an_expression_of_the_wrong_type_or_reference(
    due: str, code: str, where: str
) -> None:
    assert _codes(due) == {(code, AT + where)}


def test_a_process_of_no_package_may_not_read_settings_in_a_deadline() -> None:
    assert _codes(DUE, NONE) == {(REF_UNKNOWN, AT + "/workdays/expr")}


def test_an_expression_amount_still_needs_a_calendar_with_hours_for_workhours() -> None:
    hours = replace(CATALOG, settings=SCOPE, calendars_with_hours=frozenset())
    checked = pd.check_process(
        "test", pd.normalized_spec(spec(_due("{workhours: {expr: 'settings.warnHours'}}"))), hours
    )
    assert {p.code for p in checked.errors} == {"sla_calendar_without_hours"}


# --- the engine ---------------------------------------------------------------------------


def _timers(run: HoursRun) -> tuple[str, str]:
    return run.timer("ask", "sla")["dueAt"], run.timer("ask", "sla_warning")["dueAt"]


def test_a_deadline_from_an_expression_is_the_deadline_of_its_number() -> None:
    run = SettingsRun(_due(DUE))
    run.start()
    number = HoursRun(_due("{workdays: 2, warnBefore: {workhours: 4}}"))
    number.start()
    # Wednesday 12:00 MSK, and four working hours before it Tuesday 17:00 MSK.
    assert _timers(run) == _timers(number) == ("2026-03-04T09:00:00Z", "2026-03-03T14:00:00Z")
    assert run.activity("ask")["sla"]["dueAt"] == "2026-03-04T09:00:00Z"
    [task] = run.intents("create_task")
    assert task["due"] == "2026-03-04T09:00:00Z"
    # The timer keeps the number, and the journal records what the expression gave.
    assert run.timer("ask", "sla")["recipe"] == {
        "kind": "workdays",
        "n": 2,
        "calendar": "ru",
        "from": AT + "/workdays/expr",
    }
    computed = [d.get("computed") for d in run.decisions("timer_set")]
    assert computed == [{AT + "/workdays/expr": 2}, {AT + "/workdays/expr": 2}]
    assert all("computed" not in d for d in number.decisions("timer_set"))


def test_the_warning_of_an_expression_deadline_is_a_sla_warning_then_the_breach() -> None:
    run = SettingsRun(
        _due(
            "{workdays: {expr: settings.reviewDueWorkdays}, "
            "warnBefore: {workhours: {expr: settings.warnHours}}}"
        )
    )
    run.start()
    _fire(run, "sla_warning")
    [warning] = run.events("process.sla_warning")
    assert (warning["dueAt"], warning["warnAt"]) == ("2026-03-04T09:00:00Z", "2026-03-03T14:00:00Z")
    _fire(run, "sla")
    [breached] = run.events("process.sla_breached")
    assert breached["dueAt"] == "2026-03-04T09:00:00Z"
    assert run.status == "running"


def test_another_value_of_the_settings_gives_another_deadline() -> None:
    run = SettingsRun(_due(DUE), {"reviewDueWorkdays": 3})
    run.start()
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-05T09:00:00Z"


def test_zero_workdays_is_a_deadline_at_the_entry() -> None:
    run = SettingsRun(_due("{workdays: {expr: '0'}}"))
    run.start()
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-02T09:00:00Z"


def test_a_pause_moves_an_expression_deadline_as_it_moves_its_number() -> None:
    moved: list[tuple[Any, ...]] = []
    for run in (
        SettingsRun(_due(DUE)),
        HoursRun(_due("{workdays: 2, warnBefore: {workhours: 4}}")),
    ):
        run.calendars = RU
        run.clock = ENTERED
        run.start()
        declared = _timers(run)
        run.clock = PAUSED
        run.command("suspend", reason="supplier sent a new invoice")
        frozen = run.timer("ask", "sla")
        kept = (frozen["remaining"], frozen["remainingUnit"])
        run.clock = RESUMED
        run.command("resume")
        moved.append((declared, kept, _timers(run)))
    assert moved[0] == moved[1]
    declared, kept, resumed = moved[0]
    # The control set of the pause: Fri 10:00, seven working hours kept, Mon 16:00.
    assert declared[0] == "2026-10-09T07:00:00Z"
    assert kept == (7 * 3600.0, "working_seconds")
    assert resumed[0] == "2026-10-12T13:00:00Z"


def test_the_settings_saved_after_the_entry_do_not_move_the_deadline() -> None:
    run = SettingsRun(_due(DUE))
    run.start()
    before = _timers(run)
    run.settings = {"reviewDueWorkdays": 9, "warnHours": 1}
    run.feed("calendar", {"key": "ru"})
    run.command("suspend")
    run.command("resume")
    assert _timers(run) == before


@pytest.mark.parametrize(
    ("due", "settings", "says"),
    [
        (DUE, {"reviewDueWorkdays": -1}, "from 0 to 1000, the expression gave -1"),
        (DUE, {"reviewDueWorkdays": 5000}, "from 0 to 1000, the expression gave 5000"),
        ("{workdays: {expr: 'data.count'}}", SETTINGS, "the expression gave NoneType"),
        (
            "{workdays: 2, warnBefore: {workhours: {expr: 'settings.warnHours - 10'}}}",
            SETTINGS,
            "from 0 to 10000, the expression gave -6",
        ),
    ],
    ids=["negative", "too-many", "null", "negative-warning"],
)
def test_an_expression_that_gives_no_amount_is_sla_failed_and_the_instance_goes_on(
    due: str, settings: dict[str, Any], says: str
) -> None:
    run = SettingsRun(_due(due), settings)
    run.start()
    [failed] = run.events("process.sla_failed")
    assert failed["error"]["type"] == "expression_error"
    assert failed["error"]["detail"].endswith(says)
    record = run.activity("ask")["sla"]
    assert (record["state"], record["dueAt"], record["timer"]) == ("failed", None, None)
    assert run.state is not None
    assert not [t for t in run.state["timers"].values() if t["kind"] in sla.SLA_TIMERS]
    assert run.status == "running"


def test_the_process_deadline_takes_an_expression_too() -> None:
    body = "due: {workdays: {expr: settings.reviewDueWorkdays}}\n" + _due("P1D")
    run = SettingsRun(body)
    run.start()
    [timer] = [t for t in run.state["timers"].values() if t["sla"] == sla.PROCESS]
    assert timer["dueAt"] == "2026-03-04T09:00:00Z"
    assert timer["recipe"]["from"] == "/spec/due/workdays/expr"


# --- the replay ---------------------------------------------------------------------------


def _start_input(settings: dict[str, Any]) -> Input:
    event = {
        "id": "e",
        "type": "observation.recorded",
        "time": "2026-03-02T09:00:00Z",
        "observation": "case.opened",
        "payload": {
            "number": "N-1",
            "amount": 1,
            "deadline": "2026-05-04T09:00:00Z",
            "author": INSTANCE,
        },
    }
    return Input(
        "start", T0, {"instanceId": INSTANCE, "event": event}, None, WITH_HOURS, settings=settings
    )


def test_the_replay_compares_the_amount_the_expression_gave() -> None:
    definition = _definition(_due(DUE))
    given = _start_input(SETTINGS)
    state, decisions, intents = pe.step(definition, None, given)
    entries = [
        {
            "seq": state["seq"],
            "input": given.out(),
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
            "calendars": {},
            "settingsVersion": 0,
            "settingsSchemaRevision": 1,
        }
    ]
    same = process_replay.replay(
        definition, entries, lambda _: WITH_HOURS, settings=lambda v, r: copy.deepcopy(SETTINGS)
    )
    assert same.discrepancies == []
    other = process_replay.replay(
        definition, entries, lambda _: WITH_HOURS, settings=lambda v, r: {"reviewDueWorkdays": 5}
    )
    assert other.discrepancies
    divergence = process_replay.first_divergence(other, same.state, 0)
    assert divergence is not None and divergence.kind == "decision"
    assert divergence.recorded["computed"] == {AT + "/workdays/expr": 2}
    assert divergence.replayed["computed"] == {AT + "/workdays/expr": 5}


# --- the sandbox of package tests ---------------------------------------------------------


def _sandbox_run(given: dict[str, Any], steps: list[dict[str, Any]]) -> Any:
    world = sandbox.World(
        definitions={"test": _definition(_due(DUE))},
        task_types={"review": None},
        roles=frozenset({"lead"}),
        calendars=WITH_HOURS,
        settings_schema=SCHEMA,
    )
    test = {
        "name": "t",
        "process": "test",
        "given": {"clock": "2026-03-02T09:00:00Z", "data": {"amount": 1}, **given},
        "steps": steps,
    }
    return sandbox.run_test(world, "tests/t.test.yaml", test)


def test_a_package_test_sees_the_warning_and_the_breach_of_an_expression_deadline() -> None:
    result = _sandbox_run(
        {"settings": {"reviewDueWorkdays": 2}},
        [
            {"expect": {"sla": {"ask": "ok"}}},
            {"advance": "PT29H"},  # Tuesday 17:00 MSK
            {"expect": {"sla": {"ask": "warning"}, "events": ["process.sla_warning"]}},
            # A change of the settings does not move a deadline already counted.
            {"settings": {"reviewDueWorkdays": 9}},
            {"advance": "PT19H"},  # Wednesday 12:00 MSK
            {"expect": {"sla": {"ask": "breached"}}},
        ],
    )
    assert result.status == "passed", result.failures


def test_a_package_test_with_the_default_settings_counts_the_default() -> None:
    passed = _sandbox_run(
        {},
        [
            {"advance": "PT22H"},
            {"expect": {"sla": {"ask": "ok"}}},
            {"advance": "PT26H"},
            {"expect": {"sla": {"ask": "breached"}}},
        ],
    )
    assert passed.status == "passed", passed.failures
