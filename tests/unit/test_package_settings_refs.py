"""``settings`` in processes, rules and views, without the database (CP-ADR-0081 §6).

- the codes of a reference: ``settings_ref_unknown`` (no package, nothing
  declared, an undeclared field) and ``settings_ref_type`` (a declared field
  the place does not take), with the path of the expression; other faults
  keep their codes;
- the engine: a step reads the values of its input, a change between two
  inputs acts on the next one, the values never enter the journal's input;
- the replay: the values of the version a record names, a pair the history
  lacks is a divergence of its own, a journal without pairs replays as before
  under the same engine revision;
- the rules: the reads of a condition, a template and ``forEach``;
- the views: an expression and a column path;
- the sandbox of package tests: ``given.settings`` and the step ``settings``.
"""

import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain import process_replay
from control_plane.domain import process_sandbox as sandbox
from control_plane.domain.cel_profile import ExpressionError, environment
from control_plane.domain.errors import ValidationError
from control_plane.domain.process_engine import Definition, Input
from control_plane.domain.settings_refs import (
    NONE,
    REF_TYPE,
    REF_UNKNOWN,
    SettingsScope,
    compile_expression,
    nullable,
    settings_reads,
)
from control_plane.domain.views import check_view
from control_plane.domain.work_rules import check_settings_refs, normalize_rule_spec
from tests.unit.test_process_engine import CALENDARS, CATALOG, INSTANCE, T0, spec
from tests.unit.test_views_domain import CONTEXT, _col, _spec, _table, _view

PACKAGE = "sample"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["owner"],
    "properties": {
        "limit": {"type": "number", "default": 1000},
        "days": {"type": "integer", "default": 2},
        "owner": {"type": "string", "x-ref": "role"},
        "flag": {"type": "boolean", "default": False},
        "tags": {"type": "array", "items": {"type": "string"}, "default": []},
        "window": {"type": "object", "properties": {"start": {"type": "integer", "default": 9}}},
    },
}
SCOPE = SettingsScope(PACKAGE, SCHEMA, 1)
DECLARES_NONE = SettingsScope(PACKAGE)


def _compile(text: str, scope: SettingsScope = SCOPE) -> Any:
    return compile_expression(scope, lambda s: environment(settings=s), text, path="/x")


# --- the codes of a reference -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "settings.limit > 10.0",
        "settings.days + 1",
        "settings.window.start",
        "settings.tags[0]",
        "size(settings.tags)",
        "has(settings.owner) ? settings.owner : ''",
        "settings.flag || false",
    ],
)
def test_a_declared_field_of_a_fitting_type_compiles(text: str) -> None:
    program = _compile(text)
    assert settings_reads(program.reads)


@pytest.mark.parametrize(
    ("text", "scope", "why"),
    [
        ("settings.limit > 1.0", NONE, "not from a package"),
        ("settings.limit > 1.0", DECLARES_NONE, "declares no settings"),
        ("settings.nothing", SCOPE, "declare no such field"),
        ("settings.window.end", SCOPE, "declare no such field"),
        ("has(settings.nothing)", SCOPE, "declare no such field"),
        ("settings['limit']", NONE, "not from a package"),
    ],
)
def test_an_unknown_reference_is_settings_ref_unknown_with_the_path(
    text: str, scope: SettingsScope, why: str
) -> None:
    with pytest.raises(ExpressionError) as caught:
        _compile(text, scope)
    assert caught.value.code == REF_UNKNOWN
    assert caught.value.path == "/x"
    assert why in caught.value.reason


@pytest.mark.parametrize(
    "text",
    ["settings.limit == 'x'", "settings.owner + 1", "settings.window > 1", "settings.flag + 1"],
)
def test_a_declared_field_the_place_does_not_take_is_settings_ref_type(text: str) -> None:
    with pytest.raises(ExpressionError) as caught:
        _compile(text)
    assert caught.value.code == REF_TYPE


def test_other_faults_keep_their_codes() -> None:
    with pytest.raises(ExpressionError) as caught:
        _compile("settings.limit >")
    assert caught.value.code == "expression_syntax_error"
    with pytest.raises(ExpressionError) as caught:
        _compile("nothing.amount == 'x' && settings.limit > 1.0")
    # The fault is not the settings': the untyped settings would not pass either.
    assert caught.value.code == "expression_type_error"


def test_an_expression_without_settings_compiles_in_any_scope() -> None:
    for scope in (NONE, DECLARES_NONE, SCOPE):
        assert _compile("1 + 2", scope).reads == ()


def test_every_field_of_the_type_is_nullable() -> None:
    typed = nullable(SCHEMA)
    assert "required" not in typed
    assert "default" not in typed["properties"]["limit"]
    assert typed["properties"]["window"]["properties"]["start"] == {"type": "integer"}
    # An unsaved required field is absent: has() tells it.
    program = _compile("has(settings.owner)")
    assert program.evaluate({"settings": {"limit": 1}}).value is False
    assert program.evaluate({"settings": {"owner": "r"}}).value is True


# --- processes --------------------------------------------------------------------------------

BODY = """
stages:
  - id: main
    steps:
      - id: route
        set:
          note: "data.amount > settings.limit ? 'big' : 'small'"
          count: settings.days + 1
      - id: wait
        human: {taskType: review, assign: [{principal: 7d1c1c5e-2f57-4d3a-8e57-2a4c6b1f0a01}]}
      - id: again
        set: {value: "data.amount > settings.limit ? 'big' : 'small'"}
      - id: end
        complete: {outcome: done}
"""


def _definition(body: str = BODY, scope: SettingsScope = SCOPE) -> Definition:
    return Definition.build(
        "test", pd.normalized_spec(spec(body)), replace(CATALOG, settings=scope)
    )


def _start(settings: dict[str, Any] | None) -> Input:
    event = {
        "id": "e",
        "type": "observation.recorded",
        "time": "2026-03-02T09:00:00Z",
        "observation": "case.opened",
        "payload": {
            "number": "N-1",
            "amount": 1500,
            "deadline": "2026-05-04T09:00:00Z",
            "author": INSTANCE,
        },
    }
    return Input(
        "start", T0, {"instanceId": INSTANCE, "event": event}, None, CALENDARS, settings=settings
    )


def test_the_check_places_each_code_at_its_expression() -> None:
    body = BODY.replace("settings.days + 1", "settings.limit").replace(
        "data.amount > settings.limit ? 'big' : 'small'\"\n          count",
        'settings.nothing"\n          count',
    )
    checked = pd.check_process(
        "test", pd.normalized_spec(spec(body)), replace(CATALOG, settings=SCOPE)
    )
    assert {(p.code, p.path) for p in checked.errors} == {
        (REF_UNKNOWN, "/spec/stages/0/steps/0/set/note"),
        (REF_TYPE, "/spec/stages/0/steps/0/set/count"),
    }


def test_a_process_without_settings_reads_none_and_a_catch_may_name_its_error_settings() -> None:
    plain = """
stages:
  - id: main
    steps:
      - id: guarded
        try:
          do:
            - id: inner
              set: {note: "'x'"}
          catch:
            - as: settings
              do:
                - id: handled
                  set: {note: "settings.type"}
      - id: end
        complete: {outcome: done}
"""
    for scope in (NONE, SCOPE):
        checked = pd.check_process(
            "test", pd.normalized_spec(spec(plain)), replace(CATALOG, settings=scope)
        )
        assert checked.errors == [], checked.errors
        assert checked.reads_settings is False
        assert _definition(plain, scope).reads_settings is False


def test_each_step_reads_the_values_of_its_input() -> None:
    definition = _definition()
    assert definition.reads_settings
    state, _, _ = pe.step(definition, None, _start({"limit": 1000, "days": 2}))
    assert (state["data"]["note"], state["data"]["count"]) == ("big", 3)
    assert state["data"].get("value") is None  # waits on the human step
    # No values in the input the journal keeps.
    assert "settings" not in json.dumps(_start({"limit": 1}).out())


def test_a_definition_that_reads_settings_takes_an_input_without_them_as_empty() -> None:
    state, _, _ = pe.step(_definition(), None, _start(None))
    assert state["status"] == "failed" or state["data"].get("note") is None


# --- the replay -------------------------------------------------------------------------------


def _journal(definition: Definition, versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state, decisions, intents = pe.step(definition, None, _start(versions[0]))
    entries = [
        {
            "seq": state["seq"],
            "input": _start(versions[0]).out(),
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
            "calendars": {},
            "settingsVersion": 0,
            "settingsSchemaRevision": 1,
        }
    ]
    return entries


def test_the_replay_takes_the_values_of_the_recorded_version_not_the_current_ones() -> None:
    definition = _definition()
    entries = _journal(definition, [{"limit": 1000, "days": 2}])
    history = {(0, 1): {"limit": 1000, "days": 2}}
    same = process_replay.replay(
        definition, entries, lambda _: CALENDARS, settings=lambda v, r: history.get((v, r))
    )
    assert same.discrepancies == []
    # The current values in place of the recorded ones decide otherwise.
    current = process_replay.replay(
        definition, entries, lambda _: CALENDARS, settings=lambda v, r: {"limit": 5000, "days": 2}
    )
    assert same.state is not None and current.state is not None
    assert (same.state["data"]["note"], current.state["data"]["note"]) == ("big", "small")
    divergence = process_replay.first_divergence(current, same.state, 0)
    assert divergence is not None and divergence.kind == "data"


def test_a_pair_the_history_lacks_is_a_divergence_of_its_own() -> None:
    definition = _definition()
    entries = _journal(definition, [{"limit": 1000, "days": 2}])
    result = process_replay.replay(
        definition, entries, lambda _: CALENDARS, settings=lambda v, r: None
    )
    [found] = result.discrepancies
    assert (found.field, found.recorded) == ("settings", {"version": 0, "schemaRevision": 1})
    divergence = process_replay.first_divergence(result, None, 0)
    assert divergence is not None
    assert divergence.out() == {
        "journalSeq": 0,
        "kind": "settings",
        "element": None,
        "recorded": {"version": 0, "schemaRevision": 1},
        "replayed": None,
    }


def test_a_journal_without_settings_replays_as_before_under_the_same_engine_revision() -> None:
    assert pe.ENGINE_REVISION == 2
    plain = BODY.replace("settings.limit", "1000.0").replace("settings.days + 1", "1 + 2")
    definition = _definition(plain, NONE)
    state, decisions, intents = pe.step(definition, None, _start(None))
    entries = [
        {
            "seq": state["seq"],
            "input": _start(None).out(),
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
            "calendars": {},
        }
    ]
    result = process_replay.replay(definition, entries, lambda _: CALENDARS)
    assert result.discrepancies == [] and result.state == json.loads(json.dumps(state))


def test_the_journal_entry_of_an_input_names_the_pair_only_when_there_is_one() -> None:
    common: dict[str, Any] = {
        "seq": 1,
        "at": T0,
        "kind": "start",
        "source_ref": "x",
        "actor_id": None,
        "event_id": None,
        "given": {},
        "calendars": {},
        "decisions": [],
        "intents": [],
    }
    [plain] = process_replay.journal_entries(**common)
    assert set(plain["data"]) == {"input", "calendars"}
    [read] = process_replay.journal_entries(
        **common, settings_version=0, settings_schema_revision=2
    )
    assert (read["data"]["settingsVersion"], read["data"]["settingsSchemaRevision"]) == (0, 2)


# --- rules ------------------------------------------------------------------------------------

ACTION: dict[str, Any] = {
    "kind": "ensure_work",
    "taskType": "review",
    "dedupKeyTemplate": "x:{{payload.id}}",
    "fields": {"title": "Above {{settings.limit}}"},
}


def _rule(condition: Any = None, action: dict[str, Any] | None = None) -> Any:
    return normalize_rule_spec(
        trigger={"kind": "observation", "type": "case.flagged"},
        condition=condition,
        interpretation=None,
        action=action or ACTION,
    )


def _refused(rule: Any, scope: SettingsScope = SCOPE) -> tuple[str, str]:
    with pytest.raises(ValidationError) as caught:
        check_settings_refs(rule, scope)
    return caught.value.code, caught.value.details["field"]


def test_a_rule_reads_declared_fields_of_fitting_types() -> None:
    for condition in (
        {"gt": [{"var": "payload.amount"}, {"var": "settings.limit"}]},
        {"eq": [{"var": "settings.flag"}, True]},
        {"in": [{"var": "payload.tag"}, {"var": "settings.tags"}]},
        {"exists": "settings.window"},
        {"eq": [{"var": "settings.tags.0"}, "a"]},
    ):
        check_settings_refs(_rule(condition), SCOPE)
    whole = {**ACTION, "fields": {"title": "t", "customFields": {"w": "{{settings.window}}"}}}
    check_settings_refs(_rule(None, whole), SCOPE)
    check_settings_refs(_rule(None, {**ACTION, "fields": {"title": "plain"}}), NONE)


def test_a_rule_refuses_an_unknown_field_and_a_type_the_place_does_not_take() -> None:
    assert _refused(_rule({"gt": [{"var": "settings.nothing"}, 1]})) == (
        REF_UNKNOWN,
        "condition.gt[0].var",
    )
    assert _refused(_rule({"lt": [{"var": "settings.flag"}, 1]})) == (
        REF_TYPE,
        "condition.lt[0].var",
    )
    assert _refused(_rule({"in": [1, {"var": "settings.limit"}]})) == (
        REF_TYPE,
        "condition.in[1].var",
    )
    text = {**ACTION, "fields": {"title": "At {{settings.window}}"}}
    assert _refused(_rule(None, text)) == (REF_TYPE, "action.fields.title")
    each = {**ACTION, "forEach": "settings.limit"}
    assert _refused(_rule(None, each)) == (REF_TYPE, "action.forEach")
    assert _refused(_rule(None), NONE) == (REF_UNKNOWN, "action.fields.title")
    assert _refused(_rule(None), DECLARES_NONE) == (REF_UNKNOWN, "action.fields.title")


# --- views ------------------------------------------------------------------------------------


def _view_codes(*columns: dict[str, Any], scope: SettingsScope = SCOPE) -> list[tuple[str, str]]:
    context = replace(CONTEXT, settings=scope)
    view = _view(_spec(_table(*columns)))
    return [(p.code, p.path) for p in check_view(view, context).problems]


def test_a_view_reads_the_settings_of_its_package() -> None:
    assert _view_codes(_col(value="data.amount - settings.limit", format="number")) == []
    assert _view_codes(_col(value="data.amount > settings.limit")) == []
    assert _view_codes(_col(field="settings.limit")) == []


def test_a_view_refuses_an_unknown_field_and_a_type_that_does_not_fit() -> None:
    assert _view_codes(_col(value="settings.nothing")) == [
        (REF_UNKNOWN, "/spec/layout/0/columns/0/value")
    ]
    assert _view_codes(_col(field="settings.nothing")) == [
        (REF_UNKNOWN, "/spec/layout/0/columns/0/field")
    ]
    assert _view_codes(_col(value="settings.owner", format="number")) == [
        (REF_TYPE, "/spec/layout/0/columns/0/value")
    ]
    filtered = _spec(source={"process": "case", "filter": "settings.limit"})
    found = check_view(_view(filtered), replace(CONTEXT, settings=SCOPE)).problems
    assert [(p.code, p.path) for p in found] == [(REF_TYPE, "/spec/source/filter")]
    assert _view_codes(_col(value="settings.limit"), scope=NONE) == [
        (REF_UNKNOWN, "/spec/layout/0/columns/0/value")
    ]


# --- the sandbox of package tests -------------------------------------------------------------


def _world(definition: Definition, **extra: Any) -> sandbox.World:
    # Nothing required: a test saves what it changes.
    optional = {k: v for k, v in SCHEMA.items() if k != "required"}
    return sandbox.World(
        definitions={"test": definition},
        task_types={"review": None},
        roles=frozenset({"lead"}),
        calendars=CALENDARS,
        settings_schema=optional,
        **extra,
    )


def _run(world: sandbox.World, given: dict[str, Any], steps: list[dict[str, Any]]) -> Any:
    test = {
        "name": "t",
        "process": "test",
        "given": {"data": {"amount": 1500}, **given},
        "steps": steps,
    }
    return sandbox.run_test(world, "tests/t.test.yaml", test)


def test_the_sandbox_saves_given_settings_and_a_change_in_the_middle_of_a_scenario() -> None:
    world = _world(_definition())
    result = _run(
        world,
        {"settings": {"limit": 5000}},
        [
            {"expect": {"data": {"note": "small", "count": 3}}},
            {"settings": {"limit": 10, "days": 9}},
            {"complete": {"step": "wait"}},
            {"expect": {"data": {"note": "small", "count": 3, "value": "big"}}},
        ],
    )
    assert result.status == "passed", result.failures
    defaults = _run(world, {}, [{"expect": {"data": {"note": "big"}}}])
    assert defaults.status == "passed", defaults.failures


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"limit": "many"}, "settings_invalid"),
        ({"nothing": 1}, "settings_invalid"),
        ({"note": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"}, "secret_material_rejected"),
        ({"owner": "4b1b2c3d-0000-4000-8000-000000000001"}, "unknown_ref"),
        ([], "settings_invalid"),
    ],
)
def test_the_sandbox_checks_saved_values_as_a_saving_does(values: Any, code: str) -> None:
    world = _world(_definition())
    for given, steps in (({"settings": values}, []), ({}, [{"settings": values}])):
        result = _run(world, given, steps)
        assert result.status == "failed"
        [failure] = result.failures
        assert code in failure.message
        assert "ghp_" not in json.dumps(failure.actual, default=str)


def test_a_known_reference_and_the_same_values_twice_pass() -> None:
    owner = "4b1b2c3d-0000-4000-8000-000000000001"
    world = _world(_definition(), known_refs=frozenset({("role", owner)}))
    result = _run(
        world,
        {"settings": {"owner": owner}},
        [{"settings": {"owner": owner}}, {"expect": {"data": {"note": "big"}}}],
    )
    assert result.status == "passed", result.failures


def test_settings_in_a_test_of_a_package_without_them_stop_the_test() -> None:
    world = replace(_world(_definition()), settings_schema=None)
    result = _run(world, {"settings": {"limit": 1}}, [])
    [failure] = result.failures
    assert "settings_not_declared" in failure.message


def test_the_journal_of_the_sandbox_names_the_version_of_each_step() -> None:
    world = _world(_definition())
    box = sandbox.Sandbox(world, {"process": "test", "given": {"data": {"amount": 1}}}, seed="s")
    box.start_given()
    box.save_settings({"limit": 1}, "steps[0].settings")
    box.save_settings({"limit": 1}, "steps[1].settings")  # the same values: no new version
    assert box.settings_version == 1
    [journal] = box.journals.values()
    versions = {e["data"].get("settingsVersion") for e in journal if e["kind"] == "input"}
    assert versions == {0}
    assert copy.deepcopy(box.settings_history) == {1: {"limit": 1}}
