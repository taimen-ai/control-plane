"""The blocks of a view as TAI-ADR-0066 §1, §2 and §7.1 write them (CP-ADR-0080 §1).

The review of TASK-001298 (attempt 1) found the set of blocks drifted from
the accepted decision: the key of a block is ``block:``, ``invoke`` calls a
skill and shows its result by the output schema, ``chart`` is ``bar | line |
donut``, ``artifacts`` names ``types``, ``related`` reads ``knowledge {kind,
key}`` with ``include``, ``board`` lays its columns by ``stages`` with a card
``{title, subtitle, fields, badge}``, a ``component`` takes ``params`` and is
given them ``with``.

The review of attempt 2 found that no example of the decision passed the
check: ``fields`` is ``{section, items}`` and ``header`` ``{title, status,
actions}``; ``metrics.items`` are ``{title, value}``; a label not written is
the key ``<package>.fields.<path>``; ``open`` is ``{view, id}``; a param of a
component is typed by a part of a data schema (``schema: {$ref}``); a process
source reads ``status`` and ``slaState`` of its instances and ``decimal()``.
The views and the component of the decision are installed as they are
(``tests/fixtures/views/tenders``: their spec verbatim, the dictionaries and
the data schema the fixture). Each other case here is a view written by the
decision.
"""

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.domain.package_source import (
    PackageObject,
    ParsedPackage,
    load_file,
    parse_package,
)
from control_plane.domain.process_definition import SkillEntry
from control_plane.domain.views import (
    ProcessShape,
    ViewContext,
    check_component,
    check_locales,
    check_view,
    message_places,
    present,
    references,
    unused_messages,
)
from tests.unit.test_views_domain import API_VERSION, CONTEXT, _col, _files, _spec, _view

SKILL = "doc.summarize@1"
SKILLS = {
    SKILL: SkillEntry(
        {
            "type": "object",
            "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["text"],
        },
        {"type": "object", "properties": {"summary": {"type": "string"}}},
    ),
    "doc.blind@1": SkillEntry({"type": "object", "properties": {"text": {}}}, None),
    "doc.off@1": SkillEntry(None, {"type": "object"}, status="disabled"),
}
WITH_SKILLS = ViewContext(**{**CONTEXT.__dict__, "skills": SKILLS})


def _codes(spec: dict[str, Any], context: ViewContext = WITH_SKILLS) -> list[tuple[str, str]]:
    return [(p.code, p.path) for p in check_view(_view(spec), context).problems]


def _table(*columns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"block": "table", "columns": list(columns), **extra}


# --- the key of a block -----------------------------------------------------------------------


def test_a_block_is_named_by_the_key_block() -> None:
    assert _codes(_spec(_table(_col(field="data.amount")))) == []


def test_the_key_type_of_the_first_attempt_is_refused() -> None:
    found = _codes(_spec({"type": "table", "columns": [_col(field="data.amount")]}))
    assert found and all(code == "invalid_view" for code, _ in found), found


def test_a_block_the_set_does_not_know_is_refused_at_its_key() -> None:
    assert ("unknown_block", "/spec/layout/0/block") in _codes(_spec({"block": "widget"}))
    assert ("unknown_block", "/spec/layout/0/block") in _codes(_spec({"block": None}))


# --- invoke: a skill, its result shown by its output schema -----------------------------------


def _invoke(**fields: Any) -> dict[str, Any]:
    return {"block": "invoke", "label": "l", "skill": SKILL, **fields}


def test_invoke_calls_a_skill_with_its_input() -> None:
    block = _invoke(input={"text": "data.note", "limit": "data.count"})
    assert _codes(_spec(block)) == []


@pytest.mark.parametrize(
    ("block", "found"),
    [
        (
            _invoke(skill="doc.none@1", input={"text": "''"}),
            ("unknown_skill", "/spec/layout/0/skill"),
        ),
        (_invoke(skill="doc.off@1"), ("unknown_skill", "/spec/layout/0/skill")),
        (_invoke(), ("skill_input_missing", "/spec/layout/0/input")),
        (
            _invoke(input={"text": "data.note", "page": "1"}),
            ("unknown_skill_input", "/spec/layout/0/input/page"),
        ),
        (
            _invoke(input={"text": "data.nothing"}),
            ("expression_type_error", "/spec/layout/0/input/text"),
        ),
        (
            _invoke(input={"text": "count()"}),
            ("aggregate_outside_metrics", "/spec/layout/0/input/text"),
        ),
        (
            _invoke(skill="doc.blind@1", input={"text": "data.note"}),
            ("skill_output_missing", "/spec/layout/0/skill"),
        ),
        (_invoke(skill="doc.summarize"), ("invalid_view", "/spec/layout/0/skill")),
    ],
)
def test_invoke_is_refused_with_its_path(block: dict[str, Any], found: tuple[str, str]) -> None:
    assert found in _codes(_spec(block))


@pytest.mark.parametrize(
    "action",
    [{"process": "case"}, {"signal": "approve"}],
)
def test_invoke_no_longer_starts_a_process_or_sends_a_signal(action: dict[str, Any]) -> None:
    block = {"block": "invoke", "label": "l", "action": action}
    assert ("invalid_view", "/spec/layout/0") in _codes(_spec(block))


def test_references_name_the_skills_a_view_invokes() -> None:
    spec = _spec(_invoke(input={"text": "data.note"}))
    view = {"apiVersion": API_VERSION, "kind": "View", "key": "w", "spec": spec}
    package = parse_package(_files({}, **{"views/w.yaml": yaml.safe_dump(view)}))
    assert references(package).skills == {SKILL}


# --- chart, artifacts, related ----------------------------------------------------------------


def _chart(kind: str) -> dict[str, Any]:
    return {"block": "chart", "chart": kind, "groupBy": "stage", "value": "count()"}


@pytest.mark.parametrize("kind", ["bar", "line", "donut"])
def test_a_chart_is_bar_line_or_donut(kind: str) -> None:
    assert _codes(_spec(_chart(kind))) == []


@pytest.mark.parametrize("kind", ["pie", "area", None])
def test_another_chart_is_refused(kind: str | None) -> None:
    assert ("invalid_view", "/spec/layout/0/chart") in _codes(_spec(_chart(str(kind))))


def test_artifacts_name_their_types() -> None:
    assert _codes(_spec({"block": "artifacts", "types": ["report", "commit"]})) == []
    old = _codes(_spec({"block": "artifacts", "artifactTypes": ["report"]}))
    assert ("invalid_view", "/spec/layout/0") in old


def _related(**fields: Any) -> dict[str, Any]:
    return {"block": "related", "knowledge": {"kind": "contract", "key": "data.note"}, **fields}


def test_related_reads_a_record_of_knowledge_by_kind_and_key() -> None:
    assert _codes(_spec(_related())) == []
    assert _codes(_spec(_related(include={"relations": ["party", "owned_by"]}))) == []
    every = {"relations": "*", "direction": "in", "limit": 200}
    assert _codes(_spec(_related(include=every))) == []


@pytest.mark.parametrize(
    ("block", "found"),
    [
        ({"block": "related"}, ("invalid_view", "/spec/layout/0")),
        ({"block": "related", "relations": ["x"]}, ("invalid_view", "/spec/layout/0")),
        (
            {"block": "related", "knowledge": {"kind": "contract"}},
            ("invalid_view", "/spec/layout/0/knowledge"),
        ),
        (
            {"block": "related", "knowledge": {"kind": "Has Space", "key": "data.note"}},
            ("invalid_view", "/spec/layout/0/knowledge/kind"),
        ),
        (
            _related(knowledge={"kind": "contract", "key": "data.nothing"}),
            ("expression_type_error", "/spec/layout/0/knowledge/key"),
        ),
        (
            _related(knowledge={"kind": "contract", "key": "data.closed"}),
            ("expression_type_error", "/spec/layout/0/knowledge/key"),
        ),
        (_related(include=["party"]), ("invalid_view", "/spec/layout/0/include")),
        (_related(include={}), ("invalid_view", "/spec/layout/0/include")),
        (_related(include=None), ("invalid_view", "/spec/layout/0/include")),
        (
            _related(include={"relations": []}),
            ("invalid_view", "/spec/layout/0/include/relations"),
        ),
        (
            _related(include={"relations": ["a", "a"]}),
            ("invalid_view", "/spec/layout/0/include/relations"),
        ),
        (
            _related(include={"relations": ["Has-Dash"]}),
            ("invalid_view", "/spec/layout/0/include/relations"),
        ),
        (
            _related(include={"relations": "all"}),
            ("invalid_view", "/spec/layout/0/include/relations"),
        ),
        (
            _related(include={"relations": "*", "direction": "sideways"}),
            ("invalid_view", "/spec/layout/0/include/direction"),
        ),
        (
            _related(include={"relations": "*", "limit": 0}),
            ("invalid_view", "/spec/layout/0/include/limit"),
        ),
    ],
)
def test_related_is_refused_with_its_path(block: dict[str, Any], found: tuple[str, str]) -> None:
    assert found in _codes(_spec(block))


# --- board: columns by stages, a card ---------------------------------------------------------


def _board(**card: Any) -> dict[str, Any]:
    return {
        "block": "board",
        "columns": "stages",
        "card": card or {"title": "data.note"},
    }


def test_a_board_lays_its_columns_by_the_stages_of_the_process() -> None:
    card = {
        "title": "data.party.name",
        "subtitle": "data.note",
        "fields": [_col(field="data.amount", format="money")],
        "badge": "stage",
    }
    assert _codes(_spec(_board(**card))) == []


@pytest.mark.parametrize(
    ("block", "found"),
    [
        ({**_board(), "columns": "data.note"}, ("invalid_view", "/spec/layout/0/columns")),
        ({"block": "board", "card": {"title": "data.note"}}, ("invalid_view", "/spec/layout/0")),
        ({**_board(), "groupBy": "stage"}, ("invalid_view", "/spec/layout/0")),
        ({**_board(), "card": [_col(field="data.note")]}, ("invalid_view", "/spec/layout/0/card")),
        (_board(subtitle="data.note"), ("invalid_view", "/spec/layout/0/card")),
        (_board(title="data.nothing"), ("undeclared_path", "/spec/layout/0/card/title")),
        (
            _board(title="data.note", badge="data.gone"),
            ("undeclared_path", "/spec/layout/0/card/badge"),
        ),
        (
            _board(title="data.note", fields=[_col(field="data.amount", format="date")]),
            ("format_type_mismatch", "/spec/layout/0/card/fields/0/field"),
        ),
    ],
)
def test_a_board_is_refused_with_its_path(block: dict[str, Any], found: tuple[str, str]) -> None:
    assert found in _codes(_spec(block))


def test_the_stages_belong_to_a_process_source() -> None:
    found = _codes(_spec(_board(), source={"tasks": {"type": "item"}}))
    assert ("block_source_mismatch", "/spec/layout/0") in found


def test_a_label_written_on_a_board_card_is_a_key_though_the_card_shows_none() -> None:
    card = {"title": "data.note", "fields": [{"label": "missing.key", "field": "data.note"}]}
    found = {p.path for p in check_view(_view(_spec(_board(**card))), WITH_SKILLS).problems}
    assert found == {"/spec/layout/0/card/fields/0/label"}
    # Not written: no key is needed, the console shows the value of a card field alone.
    unlabelled = {"title": "data.note", "fields": [{"field": "data.note"}]}
    assert _codes(_spec(_board(**unlabelled))) == []


# --- components: params, given with ------------------------------------------------------------


def _component(spec: dict[str, Any], key: str = "summary") -> PackageObject:
    return PackageObject("Component", key, spec, f"components/{key}.yaml")


COMPONENT = {
    "params": {"limit": {"type": "number", "required": True}, "label": {"type": "string"}},
    "layout": [_table(_col(value="data.amount > param.limit"))],
}


def _with_component(spec: dict[str, Any] = COMPONENT) -> ViewContext:
    component = _component(spec)
    return ViewContext(
        **{
            **WITH_SKILLS.__dict__,
            "components": {"summary": component},
            "component_keys": frozenset({"summary"}),
        }
    )


def _use(**given: str) -> dict[str, Any]:
    return {"block": "component", "component": "summary", "with": given}


def test_a_component_takes_params_and_is_given_them_with() -> None:
    context = _with_component()
    assert check_component(_component(COMPONENT), context) == []
    checked = check_view(_view(_spec(_use(limit="data.amount * 2.0"))), context)
    assert checked.problems == []
    assert checked.form is not None
    assert checked.form["layout"] == [
        {
            "block": "component",
            "component": "summary",
            "with": {"limit": "data.amount * 2.0"},
            "layout": COMPONENT["layout"],
        }
    ]


@pytest.mark.parametrize(
    ("block", "found"),
    [
        (_use(), ("missing_param", "/spec/layout/0/with")),
        (
            {"block": "component", "component": "summary"},
            ("missing_param", "/spec/layout/0/with"),
        ),
        (_use(limit="1.0", page="2"), ("unknown_param", "/spec/layout/0/with/page")),
        (_use(limit="data.note"), ("expression_type_error", "/spec/layout/0/with/limit")),
        (_use(limit="data.nothing"), ("expression_type_error", "/spec/layout/0/with/limit")),
        (
            _use(limit="sum(data.amount)"),
            ("aggregate_outside_metrics", "/spec/layout/0/with/limit"),
        ),
    ],
)
def test_the_params_of_a_component_are_given_as_declared(
    block: dict[str, Any], found: tuple[str, str]
) -> None:
    assert found in _codes(_spec(block), _with_component())


def test_a_component_reads_its_own_params_not_those_of_the_view() -> None:
    spec = {
        "params": {"limit": {"type": "number"}},
        "layout": [_table(_col(value="param.since != ''"))],
    }
    context = _with_component(spec)
    found = check_view(_view(_spec(_use(), params={"since": {"type": "string"}})), context).problems
    assert [(p.code, p.file, p.path) for p in found] == [
        ("expression_type_error", "components/summary.yaml", "/spec/layout/0/columns/0/value")
    ]


@pytest.mark.parametrize(
    ("param", "path"),
    [
        ({"type": "money"}, "/spec/params/x/type"),
        ({"schema": "object"}, "/spec/params/x/schema"),
        ({"schema": {}}, "/spec/params/x/schema"),
        ({"schema": {"type": "string"}, "type": "string"}, "/spec/params/x"),
        (None, "/spec/params/x"),
        ({"schema": {"$ref": "#/$defs/x"}}, "/spec/params/x/schema/$ref"),
        ({"schema": {"$ref": "https://example.org/x.json"}}, "/spec/params/x/schema/$ref"),
    ],
)
def test_the_params_of_a_component_are_typed(param: Any, path: str) -> None:
    bad = {"params": {"x": param}, "layout": [_table(_col(field="data.note"))]}
    found = check_component(_component(bad), CONTEXT)
    assert ("invalid_component", path) in {(p.code, p.path) for p in found}


# --- aggregates are calls, not text in a string literal -------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        'data.note == "max(x)"',
        "data.note == 'count()'",
        'data.note == """sum(a)\n"""',
        "data.note == r'avg(\\d)'",
        'data.note.contains("min(") || data.note == "\\"count(\\""',
    ],
)
def test_an_aggregate_name_in_a_string_literal_is_no_aggregate(value: str) -> None:
    assert _codes(_spec(_table(_col(value=value)))) == []


def test_an_aggregate_next_to_a_string_literal_is_still_one() -> None:
    found = _codes(_spec(_table(_col(value='data.note == "x" && count() > 1'))))
    assert ("aggregate_outside_metrics", "/spec/layout/0/columns/0/value") in found


def test_a_string_literal_inside_an_aggregate_is_not_taken_for_a_nested_one() -> None:
    metrics = {
        "block": "metrics",
        "items": [{"title": "l", "value": 'count(data.note == "sum(x)")'}],
    }
    assert _codes(_spec(metrics)) == []


# --- the examples of the decision, as they are -------------------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "views" / "tenders"
STAGES = ("intake", "preparation", "submitted", "result")
TENDER_ROLES = frozenset({"tender-lead", "tender-finance", "general-director"})


def _tenders(**extra: str) -> ParsedPackage:
    files = {
        path.relative_to(FIXTURE).as_posix(): path.read_text(encoding="utf-8")
        for path in FIXTURE.rglob("*.yaml")
    }
    files.update(extra)
    return parse_package(sorted(files.items()))


def _tender_data() -> dict[str, Any]:
    text = (FIXTURE / "schemas" / "tender.yaml").read_text(encoding="utf-8")
    data, _ = load_file("schemas/tender.yaml", text)
    assert isinstance(data, dict)
    return data


def _tender_context(package: ParsedPackage) -> ViewContext:
    components = {obj.key: obj for obj in package.of_kind("Component")}
    return ViewContext(
        locales=("en", "ru"),
        default_locale="en",
        dictionaries={loc: d.messages for loc, d in package.dictionaries.items()},
        processes={"tender": ProcessShape(_tender_data(), STAGES)},
        roles=TENDER_ROLES,
        views=frozenset(obj.key for obj in package.of_kind("View")),
        components=components,
        component_keys=frozenset(components),
        package="tenders",
    )


def _tender_view(package: ParsedPackage, key: str) -> PackageObject:
    return next(obj for obj in package.of_kind("View") if obj.key == key)


def _doc(kind: str, key: str, spec: dict[str, Any]) -> str:
    return yaml.safe_dump({"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec})


def test_the_views_and_the_component_of_the_decision_install_as_they_are() -> None:
    package = _tenders()
    assert package.problems == []
    assert check_locales(package) == []
    assert sorted(obj.key for obj in package.of_kind("View")) == ["tender-card", "tenders-board"]
    context = _tender_context(package)
    for component in package.of_kind("Component"):
        assert check_component(component, context) == []
    used: set[str] = set()
    for view in package.of_kind("View"):
        checked = check_view(view, context)
        assert checked.problems == [], (view.key, checked.problems)
        assert checked.form is not None
        used |= checked.messages
    for component in package.of_kind("Component"):
        used |= {key for _, key in message_places(component.spec)}
    assert unused_messages(package, used) == []


def test_the_param_of_the_component_is_typed_by_a_part_of_the_data_schema() -> None:
    package = _tenders()
    component = package.of_kind("Component")[0]
    # The parser reads the $ref: the param is typed by data.procurement of the schema.
    assert (
        component.spec["params"]["procurement"]["schema"]
        == (_tender_data()["properties"]["procurement"])
    )


def _summary(given: dict[str, str] | None = None) -> str:
    spec = {
        "title": "tenders.card.title",
        "source": {"process": "tender", "instance": "param.id"},
        "layout": [
            {
                "block": "component",
                "component": "procurement-summary",
                "with": {"procurement": "data.procurement"} if given is None else given,
            }
        ],
    }
    return _doc("View", "tender-summary", spec)


def test_one_card_of_a_lot_reads_param_procurement_customer_short_name() -> None:
    package = _tenders(**{"views/tender-summary.yaml": _summary()})
    checked = check_view(_tender_view(package, "tender-summary"), _tender_context(package))
    assert checked.problems == []
    assert checked.form is not None
    assert present(checked.form, "ru")["layout"] == [
        {
            "block": "component",
            "component": "procurement-summary",
            "layout": [
                {
                    "block": "fields",
                    "items": [
                        {"key": "procurement.customer.shortName", "label": "Заказчик"},
                        {
                            "key": "procurement.maxPrice.amount",
                            "label": "НМЦК",
                            "format": "money",
                        },
                    ],
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("given", "found"),
    [
        (
            {"procurement": "data.procurement.number"},
            ("expression_type_error", "/spec/layout/0/with/procurement"),
        ),
        (
            {"procurement": "data.procurement.nothing"},
            ("expression_type_error", "/spec/layout/0/with/procurement"),
        ),
        (
            {"procurement": "data.procurement", "lot": "1"},
            ("unknown_param", "/spec/layout/0/with/lot"),
        ),
    ],
)
def test_a_param_typed_by_a_schema_is_given_a_value_of_its_type(
    given: dict[str, str], found: tuple[str, str]
) -> None:
    package = _tenders(**{"views/tender-summary.yaml": _summary(given)})
    checked = check_view(_tender_view(package, "tender-summary"), _tender_context(package))
    assert found in {(p.code, p.path) for p in checked.problems}


def test_a_field_the_schema_of_a_param_does_not_declare_is_refused_in_the_component() -> None:
    text = (FIXTURE / "components" / "procurement-summary.yaml").read_text(encoding="utf-8")
    broken = text.replace("param.procurement.customer.shortName", "param.procurement.customer.age")
    package = _tenders(
        **{
            "components/procurement-summary.yaml": broken,
            "views/tender-summary.yaml": _summary(),
        }
    )
    checked = check_view(_tender_view(package, "tender-summary"), _tender_context(package))
    assert [(p.code, p.file, p.path) for p in checked.problems] == [
        (
            "expression_type_error",
            "components/procurement-summary.yaml",
            "/spec/layout/0/items/0/value",
        )
    ]


@pytest.mark.parametrize(
    "ref",
    [
        "../schemas/none.yaml#/properties/procurement",
        "../../outside.yaml",
        "../schemas/tender.yaml#/properties/nothing",
        "../schemas/tender.yaml#/type",
        "../i18n/broken.json",
    ],
)
def test_a_schema_of_a_param_that_leads_nowhere_is_refused_with_its_place(ref: str) -> None:
    text = (FIXTURE / "components" / "procurement-summary.yaml").read_text(encoding="utf-8")
    moved = text.replace("../schemas/tender.yaml#/properties/procurement", ref)
    package = _tenders(**{"components/procurement-summary.yaml": moved})
    found = [(p.code, p.file, p.path) for p in package.problems]
    assert (
        "unresolved_schema_ref",
        "components/procurement-summary.yaml",
        "/spec/params/procurement/schema/$ref",
    ) in found, found


def test_a_whole_schema_file_and_its_defs_are_read_for_a_param() -> None:
    money = {
        "$defs": {"money": {"type": "object", "properties": {"amount": {"type": "string"}}}},
        "type": "object",
        "properties": {"price": {"$ref": "#/$defs/money"}, "lot": {"type": "integer"}},
    }
    spec = {
        "params": {
            "whole": {"schema": {"$ref": "../schemas/lot.yaml"}},
            "price": {"schema": {"$ref": "../schemas/lot.yaml#/properties/price"}},
        },
        "layout": [{"block": "fields", "items": [{"label": "l", "value": "param.whole.lot"}]}],
    }
    package = parse_package(
        [
            ("schemas/lot.yaml", yaml.safe_dump(money)),
            ("components/lot.yaml", _doc("Component", "lot", spec)),
        ]
    )
    assert package.problems == []
    params = package.of_kind("Component")[0].spec["params"]
    assert params["whole"]["schema"] == money
    # The part keeps the $defs of its file: its local $ref still resolves.
    assert params["price"]["schema"] == {"$defs": money["$defs"], "$ref": "#/$defs/money"}


# --- fields and header ------------------------------------------------------------------------


def _card(*layout: dict[str, Any], **change: Any) -> dict[str, Any]:
    return _spec(*layout, source={"process": "case", "instance": "param.id"}, **change)


def test_fields_are_a_section_of_items_and_a_header_its_title_status_and_actions() -> None:
    fields = {
        "block": "fields",
        "section": "s",
        "items": [_col(value="data.note"), _col(field="data.amount", format="money")],
    }
    header = {"block": "header", "title": "data.note", "status": "stage", "actions": "steps"}
    assert _codes(_card(header, fields)) == []
    assert _codes(_card({"block": "header", "title": "data.party.name"})) == []
    assert _codes(_card({"block": "header", "title": "data.note", "status": "slaState"})) == []


@pytest.mark.parametrize(
    ("block", "found"),
    [
        ({"block": "fields"}, ("invalid_view", "/spec/layout/0")),
        (
            {"block": "fields", "fields": [_col(field="data.note")]},
            ("invalid_view", "/spec/layout/0"),
        ),
        ({"block": "fields", "items": []}, ("invalid_view", "/spec/layout/0/items")),
        ({"block": "fields", "items": None}, ("invalid_view", "/spec/layout/0/items")),
        ({"block": "header"}, ("invalid_view", "/spec/layout/0")),
        (
            {"block": "header", "fields": [_col(field="data.note")]},
            ("invalid_view", "/spec/layout/0"),
        ),
        (
            {"block": "header", "title": "data.note", "section": "s"},
            ("invalid_view", "/spec/layout/0"),
        ),
        (
            {"block": "header", "title": "data.note", "actions": "buttons"},
            ("invalid_view", "/spec/layout/0/actions"),
        ),
        (
            {"block": "header", "title": "data.note", "actions": ["steps"]},
            ("invalid_view", "/spec/layout/0/actions"),
        ),
        ({"block": "header", "title": "data.nothing"}, ("undeclared_path", "/spec/layout/0/title")),
        (
            {"block": "header", "title": "data.note", "status": "phase"},
            ("undeclared_path", "/spec/layout/0/status"),
        ),
        (
            {"block": "fields", "items": [_col(value="data.nothing")]},
            ("expression_type_error", "/spec/layout/0/items/0/value"),
        ),
        (
            {"block": "fields", "items": [_col(value="data.amount", format="date")]},
            ("format_type_mismatch", "/spec/layout/0/items/0/value"),
        ),
    ],
)
def test_fields_and_header_are_refused_with_their_path(
    block: dict[str, Any], found: tuple[str, str]
) -> None:
    assert found in _codes(_card(block))


# --- metrics, labels and keys ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "path"),
    [
        ({"label": "l", "value": "count()"}, "/spec/layout/0/items/0"),
        ({"value": "count()"}, "/spec/layout/0/items/0"),
        ({"title": "l"}, "/spec/layout/0/items/0"),
        ({"title": "l", "value": "count()", "key": "1st"}, "/spec/layout/0/items/0/key"),
    ],
)
def test_an_item_of_metrics_is_a_title_and_a_value(item: dict[str, Any], path: str) -> None:
    assert ("invalid_view", path) in _codes(_spec({"block": "metrics", "items": [item]}))


def _with_texts(**texts: str) -> ViewContext:
    dictionaries = {loc: {**words, **texts} for loc, words in WITH_SKILLS.dictionaries.items()}
    return dataclasses.replace(WITH_SKILLS, dictionaries=dictionaries)


def test_a_label_not_written_is_the_key_package_fields_path() -> None:
    table = _table(
        {"field": "data.note"},
        {"key": "noteAmount", "value": "decimal(data.note)", "format": "money"},
    )
    found = check_view(_view(_spec(table)), WITH_SKILLS).problems
    assert [(p.code, p.path) for p in found] == [
        ("missing_message", "/spec/layout/0/columns/0/label"),
        ("missing_message", "/spec/layout/0/columns/1/label"),
    ]
    assert all("sample.fields.note" in p.message for p in found)
    checked = check_view(_view(_spec(table)), _with_texts(**{"sample.fields.note": "Note"}))
    assert checked.problems == []
    assert checked.form is not None
    assert present(checked.form, "en")["layout"][0]["columns"] == [
        {"key": "note", "title": "Note"},
        {"key": "noteAmount", "title": "Note", "format": "money"},
    ]


def test_a_value_of_no_one_field_writes_its_label() -> None:
    table = _table({"value": "data.amount + double(data.count)"}, {"value": "'x'"})
    found = _codes(_spec(table))
    assert ("missing_label", "/spec/layout/0/columns/0") in found
    assert ("missing_label", "/spec/layout/0/columns/1") in found


def test_two_columns_of_one_key_are_refused_and_a_key_written_parts_them() -> None:
    same = _table(_col(field="data.amount"), _col(field="data.amount", format="money"))
    assert ("duplicate_key", "/spec/layout/0/columns/1") in _codes(_spec(same))
    parted = _table(_col(field="data.amount"), _col(key="amountMoney", field="data.amount"))
    checked = check_view(_view(_spec(parted)), WITH_SKILLS)
    assert checked.problems == []
    assert checked.form is not None
    keys = [c["key"] for c in present(checked.form, "en")["layout"][0]["columns"]]
    assert keys == ["amount", "amountMoney"]
    metrics = {
        "block": "metrics",
        "items": [{"title": "l", "value": "count()"}, {"title": "l", "value": "sum(data.amount)"}],
    }
    assert ("duplicate_key", "/spec/layout/0/items/1") in _codes(_spec(metrics))


# --- open, the context of a process, decimal ---------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        {"view": "case-card", "id": "id"},
        {"view": "case-card", "id": "instance.id"},
        {"view": "case-card", "params": {"id": "id"}},
        {"view": "case-card"},
    ],
)
def test_open_names_a_view_and_the_id_of_the_record(target: dict[str, Any]) -> None:
    assert _codes(_spec(_table(_col(field="data.note"), open=target))) == []
    assert _codes(_spec({**_board(), "open": target})) == []


@pytest.mark.parametrize(
    ("target", "found"),
    [
        (
            {"view": "case-card", "id": "data.amount"},
            ("expression_type_error", "/spec/layout/0/open/id"),
        ),
        (
            {"view": "case-card", "id": "count()"},
            ("aggregate_outside_metrics", "/spec/layout/0/open/id"),
        ),
        ({"view": "case-card", "id": ""}, ("invalid_view", "/spec/layout/0/open/id")),
        ({"view": "case-card", "id": None}, ("invalid_view", "/spec/layout/0/open/id")),
        ({"id": "id"}, ("invalid_view", "/spec/layout/0/open")),
    ],
)
def test_open_is_refused_with_its_path(target: dict[str, Any], found: tuple[str, str]) -> None:
    assert found in _codes(_spec(_table(_col(field="data.note"), open=target)))


@pytest.mark.parametrize(
    "filter",
    [
        "status == 'active'",
        "slaState in ['breached', 'warning'] && status != 'completed'",
        "id != '' && stage.intake.completed",
    ],
)
def test_a_process_source_reads_status_and_sla_state_of_its_instances(filter: str) -> None:
    assert _codes(_spec(source={"process": "case", "filter": filter})) == []


@pytest.mark.parametrize(
    ("filter", "code"),
    [
        ("status", "expression_type_error"),
        ("slaState == 1", "expression_type_error"),
        ("state == 'active'", "expression_type_error"),
    ],
)
def test_the_state_of_an_instance_is_a_string(filter: str, code: str) -> None:
    assert (code, "/spec/source/filter") in _codes(
        _spec(source={"process": "case", "filter": filter})
    )


def test_status_and_sla_state_are_shown_as_strings() -> None:
    assert _codes(_spec(_table(_col(field="slaState", format="status")))) == []
    assert _codes(_spec(_table(_col(value="status", format="text")))) == []
    found = _codes(_spec(_table(_col(field="status", format="number"))))
    assert ("format_type_mismatch", "/spec/layout/0/columns/0/field") in found


@pytest.mark.parametrize(
    ("value", "format", "ok"),
    [
        ("decimal(data.note)", "money", True),
        ("decimal(data.count)", "number", True),
        ("decimal(data.amount)", "money", True),
        ("decimal(data.note)", "date", False),
        ("decimal(data.closed)", "money", False),
    ],
)
def test_decimal_reads_an_amount_written_as_a_string(value: str, format: str, ok: bool) -> None:
    found = _codes(_spec(_table(_col(value=value, format=format))))
    assert (found == []) is ok, found


def test_a_sum_of_amounts_written_as_strings_is_a_figure() -> None:
    metrics = {
        "block": "metrics",
        "items": [{"title": "l", "value": "sum(decimal(data.note))", "format": "money"}],
    }
    assert _codes(_spec(metrics)) == []
    bare = copy.deepcopy(metrics)
    bare["items"][0]["value"] = "sum(data.note)"
    assert ("expression_type_error", "/spec/layout/0/items/0/value") in _codes(_spec(bare))


# --- nav: a group of the console menu -----------------------------------------------------------


@pytest.mark.parametrize("group", ["work", "knowledge", "packages"])
def test_nav_names_a_group_of_the_console_menu(group: str) -> None:
    checked = check_view(_view(_spec(nav={"group": group, "icon": "inbox"})), WITH_SKILLS)
    assert checked.problems == []
    assert checked.form is not None
    assert present(checked.form, "en")["nav"] == {"group": group, "icon": "inbox"}


@pytest.mark.parametrize("group", ["sales", "t", "Work", "", None, 1])
def test_another_group_is_refused(group: Any) -> None:
    found = check_view(_view(_spec(nav={"group": group})), WITH_SKILLS).problems
    assert [(p.code, p.path) for p in found] == [("invalid_view", "/spec/nav/group")]


def test_a_nav_without_a_group_is_in_packages_and_a_view_without_nav_has_none() -> None:
    checked = check_view(_view(_spec(nav={"order": 3})), WITH_SKILLS)
    assert checked.form is not None
    assert present(checked.form, "en")["nav"] == {"group": "packages", "order": 3}
    plain = check_view(_view(_spec()), WITH_SKILLS)
    assert plain.form is not None
    assert "nav" not in present(plain.form, "en")


# --- filters: typed by the data schema ----------------------------------------------------------

KINDS_DATA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["goods", "works", None]},
        "amount": {"type": "number"},
        "count": {"type": "integer"},
        "due": {"type": "string", "format": "date"},
        "openedAt": {"type": "string", "format": "date-time"},
        "closed": {"type": ["boolean", "null"]},
        "note": {"type": "string"},
        "party": {"$ref": "#/$defs/party"},
    },
    "$defs": {"party": {"type": "object", "properties": {"inn": {"type": "string"}}}},
}
FILTERED = ("kind", "amount", "count", "due", "openedAt", "closed", "note", "party.inn")


def _filtered_context() -> ViewContext:
    names = [*FILTERED, "stage", "status", "slaState", "instance.startedAt"]
    texts = {f"sample.fields.{name}": f"T:{name}" for name in names}
    context = _with_texts(**texts, **{"sample.fields.kind.goods": "Goods"})
    processes = {"case": ProcessShape(KINDS_DATA, ("intake", "done"))}
    return dataclasses.replace(context, processes=processes)


def test_the_type_and_values_of_a_filter_are_read_from_the_data_schema() -> None:
    filters = [f"data.{name}" for name in FILTERED] + [
        "stage",
        "status",
        "slaState",
        "instance.startedAt",
    ]
    table = _table(_col(field="data.note"), filters=filters, sort=[{"field": "data.amount"}])
    checked = check_view(_view(_spec(table)), _filtered_context())
    assert checked.problems == []
    assert checked.form is not None
    shown = present(checked.form, "en")["layout"][0]
    by_field = {f["field"]: f for f in shown["filters"]}
    assert [f["field"] for f in shown["filters"]] == [*FILTERED, *filters[len(FILTERED) :]]
    assert by_field["kind"] == {
        "field": "kind",
        "title": "T:kind",
        "type": "enum",
        # A value the dictionaries do not name is shown as it is.
        "options": [{"value": "goods", "title": "Goods"}, {"value": "works", "title": "works"}],
    }
    assert {name: f["type"] for name, f in by_field.items()} == {
        "kind": "enum",
        "amount": "number",
        "count": "number",
        "due": "date",
        "openedAt": "date",
        "closed": "enum",
        "note": "text",
        "party.inn": "text",
        "stage": "enum",
        "status": "enum",
        "slaState": "enum",
        "instance.startedAt": "date",
    }
    assert [o["value"] for o in by_field["closed"]["options"]] == [True, False]
    assert [o["value"] for o in by_field["stage"]["options"]] == ["intake", "done"]
    assert [o["value"] for o in by_field["status"]["options"]] == [
        "running",
        "suspended",
        "completed",
        "failed",
        "cancelled",
    ]
    assert "breached" in [o["value"] for o in by_field["slaState"]["options"]]
    assert shown["sort"] == [{"field": "amount", "title": "T:amount"}]


def test_the_title_of_a_filter_is_a_key_of_the_dictionaries() -> None:
    table = _table(_col(field="data.note"), filters=["data.amount"], sort=[{"field": "stage"}])
    found = check_view(_view(_spec(table)), WITH_SKILLS).problems
    assert [(p.code, p.path) for p in found] == [
        ("missing_message", "/spec/layout/0/filters/0"),
        ("missing_message", "/spec/layout/0/sort/0/field"),
    ]


# The filters of tasks and knowledge, typed by their schemas (stage 6):
# tests/unit/test_view_sources_domain.py.
