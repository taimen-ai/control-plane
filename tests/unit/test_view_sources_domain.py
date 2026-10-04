"""The check of a view of tasks and of knowledge (CP-ADR-0080, amendment Б; TAI-ADR-0066 stage 6).

Pure: the paths ``fields.*``/``customFields.*`` of a task and ``attributes.*``
of a record exist in their schemas; the filters are typed by them; the
ontology of a tree (kinds, attributes, relations) is checked by warnings.
"""

import dataclasses
from typing import Any

import pytest

from control_plane.domain.package_source import PackageObject
from control_plane.domain.views import (
    KnowledgeCatalog,
    TaskShape,
    ViewContext,
    check_knowledge,
    check_view,
    knowledge_names,
    present,
    source_environment,
    task_shape,
)
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE

FIELDS = {
    "type": "object",
    "properties": {
        "number": {"type": "string"},
        "amount": {"type": "number"},
        "kind": {"enum": ["goods", "works"]},
        "due": {"type": "string", "format": "date"},
        "urgent": {"type": "boolean"},
        "invoice": {
            "type": "object",
            "properties": {"amount": {"type": "string"}, "currency": {"type": "string"}},
        },
    },
}
ITEM = TaskShape(FIELDS, (("new", "New"), ("paid", "Paid")))
KEYS = ["t", "l"]
CONTEXT = ViewContext(
    locales=("en",),
    default_locale="en",
    dictionaries={"en": {k: k for k in KEYS}},
    task_types=frozenset({"item", "bare"}),
    task_shapes={"item": ITEM, "bare": TaskShape(None)},
    package="sample",
)
TASKS = {"tasks": {"type": "item"}}
KNOWLEDGE = {"knowledge": {"kinds": ["legal_entity", "contract"]}}


def _table(*columns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"block": "table", "columns": list(columns), **extra}


def _col(**fields: Any) -> dict[str, Any]:
    return {"label": "l", **fields}


def _spec(*layout: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return {"title": "t", "source": source, "layout": list(layout)}


def _view(spec: dict[str, Any]) -> PackageObject:
    return PackageObject("View", "queue", spec, "views/queue.yaml")


def _codes(spec: dict[str, Any], context: ViewContext = CONTEXT) -> list[tuple[str, str]]:
    return [(p.code, p.path) for p in check_view(_view(spec), context).problems]


def _texts(*keys: str) -> ViewContext:
    words = {**{k: k for k in KEYS}, **{k: k for k in keys}}
    return dataclasses.replace(CONTEXT, dictionaries={"en": words})


# --- a view of tasks -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "fields.title",
        "fields.status",
        "fields.dueDate",
        "fields.assigneeId",
        "customFields.number",
        "customFields.invoice.amount",
    ],
)
def test_a_path_of_a_task_declared_by_its_type_is_accepted(field: str) -> None:
    assert _codes(_spec(_table(_col(field=field)), source=TASKS)) == []


@pytest.mark.parametrize(
    "field",
    [
        "customFields.nothing",
        "customFields.number.inner",
        "customFields",
        "fields.bogus",
        "fields",
        "title",
        "data.amount",
        "attributes.inn",
        "stage",
    ],
)
def test_a_path_of_a_task_its_type_does_not_declare_is_refused(field: str) -> None:
    found = _codes(_spec(_table(_col(field=field)), source=TASKS))
    assert ("undeclared_path", "/spec/layout/0/columns/0/field") in found


def test_a_type_without_a_field_schema_declares_no_custom_field() -> None:
    found = _codes(_spec(_table(_col(field="customFields.x")), source={"tasks": {"type": "bare"}}))
    assert found == [("undeclared_path", "/spec/layout/0/columns/0/field")]
    assert (
        _codes(_spec(_table(_col(field="fields.title")), source={"tasks": {"type": "bare"}})) == []
    )


def test_filters_sort_and_group_of_tasks_are_declared_paths_too() -> None:
    table = _table(
        _col(field="fields.title"),
        filters=["customFields.kind", "customFields.gone"],
        sort=[{"field": "fields.nope"}],
    )
    chart = {"block": "chart", "chart": "bar", "groupBy": "customFields.lost", "value": "count()"}
    found = _codes(_spec(table, chart, source=TASKS))
    assert ("undeclared_path", "/spec/layout/0/filters/1") in found
    assert ("undeclared_path", "/spec/layout/0/sort/0/field") in found
    assert ("undeclared_path", "/spec/layout/1/groupBy") in found
    assert ("undeclared_path", "/spec/layout/0/filters/0") not in found


def test_expressions_of_tasks_are_typed_by_the_field_schema() -> None:
    good = _table(
        _col(value="customFields.amount * 2.0", format="number"),
        _col(value="decimal(customFields.invoice.amount)", format="money"),
        _col(value="fields.dueDate", format="due"),
    )
    assert _codes(_spec(good, source=TASKS)) == []
    metrics = {
        "block": "metrics",
        "items": [{"title": "l", "value": "sum(customFields.amount)"}],
    }
    assert _codes(_spec(metrics, source=TASKS)) == []
    wrong = _table(_col(value="customFields.missing"))
    assert [c for c, _ in _codes(_spec(wrong, source=TASKS))] == ["expression_type_error"]


@pytest.mark.parametrize(
    ("column", "fits"),
    [
        (_col(field="fields.dueDate", format="date"), True),
        (_col(field="fields.dueDate", format="number"), False),
        (_col(field="customFields.amount", format="money"), True),
        (_col(field="customFields.number", format="percent"), False),
        (_col(field="fields.status", format="status"), True),
    ],
)
def test_a_path_of_a_task_fits_its_format(column: dict[str, Any], fits: bool) -> None:
    found = _codes(_spec(_table(column), source=TASKS))
    assert (found == []) is fits
    if not fits:
        assert found == [("format_type_mismatch", "/spec/layout/0/columns/0/field")]


def test_the_filters_of_tasks_are_typed_by_the_type() -> None:
    paths = [
        "fields.status",
        "fields.priority",
        "fields.systemStatusCategory",
        "fields.dueDate",
        "fields.title",
        "customFields.amount",
        "customFields.kind",
        "customFields.due",
        "customFields.urgent",
        "customFields.number",
    ]
    context = _texts(*(f"sample.fields.{p}" for p in paths))
    checked = check_view(
        _view(_spec(_table(_col(field="fields.title"), filters=paths), source=TASKS)), context
    )
    assert checked.problems == []
    assert checked.form is not None
    shown = present(checked.form, "en")["layout"][0]["filters"]
    types = {f["field"]: (f["type"], [o["value"] for o in f.get("options", [])]) for f in shown}
    assert types == {
        "fields.status": ("enum", ["new", "paid"]),
        "fields.priority": ("enum", ["critical", "high", "medium", "low"]),
        "fields.systemStatusCategory": (
            "enum",
            ["backlog", "active", "blocked", "terminal_success", "terminal_cancelled"],
        ),
        "fields.dueDate": ("date", []),
        "fields.title": ("text", []),
        "customFields.amount": ("number", []),
        "customFields.kind": ("enum", ["goods", "works"]),
        "customFields.due": ("date", []),
        "customFields.urgent": ("enum", [True, False]),
        "customFields.number": ("text", []),
    }
    # The key of the label of a filter is the path as it is.
    assert {f["title"] for f in shown} == {f"sample.fields.{p}" for p in paths}


def test_the_shape_of_a_task_type_reads_its_lifecycle() -> None:
    shape = task_shape(FIELDS, SYSTEM_TASK_LIFECYCLE)
    assert shape.field_schema == FIELDS
    assert [key for key, _ in shape.statuses] == [
        "backlog",
        "todo",
        "in_progress",
        "blocked",
        "done",
        "cancelled",
    ]
    assert task_shape(None, None) == TaskShape(None, ())
    assert task_shape("x", {"statuses": [{"key": 1}, "y", {"key": "a"}]}).statuses == (("a", "a"),)


def test_an_absent_field_of_a_task_reads_null() -> None:
    env = source_environment("tasks", None, {}, task=ITEM)
    values = {"id": "x", "fields": {"ownerId": None}, "customFields": {}, "param": {}}
    assert env.compile("fields.ownerId == null").evaluate(values).value is True
    assert env.compile("customFields.amount").evaluate(values).value is None


# --- a view of knowledge -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["id", "kind", "key", "title", "validFrom", "validTo", "attributes.inn", "attributes.a.b"],
)
def test_a_path_of_a_record_of_knowledge_is_accepted(field: str) -> None:
    assert _codes(_spec(_table(_col(field=field)), source=KNOWLEDGE)) == []


@pytest.mark.parametrize(
    "field", ["attributes", "attributes.", "data.inn", "fields.title", "relations.Bad", "relations"]
)
def test_a_path_that_is_no_path_of_a_record_is_refused(field: str) -> None:
    found = _codes(_spec(_table(_col(field=field)), source=KNOWLEDGE))
    # A path not of the form of a path at all is refused by the schema already.
    assert {code for code, path in found if path == "/spec/layout/0/columns/0/field"} & {
        "undeclared_path",
        "invalid_view",
    }


def test_the_relations_of_a_record_are_a_column_only() -> None:
    table = _table(
        _col(field="relations.customer_of"),
        filters=["relations.customer_of"],
        sort=[{"field": "relations.customer_of"}],
    )
    chart = {"block": "chart", "chart": "bar", "groupBy": "relations.x", "value": "count()"}
    found = _codes(_spec(table, chart, source=KNOWLEDGE))
    assert ("undeclared_path", "/spec/layout/0/columns/0/field") not in found
    assert ("undeclared_path", "/spec/layout/0/filters/0") in found
    assert ("undeclared_path", "/spec/layout/0/sort/0/field") in found
    assert ("undeclared_path", "/spec/layout/1/groupBy") in found
    # Nor are they a variable of an expression.
    value = _table(_col(value="relations.customer_of"))
    found = _codes(_spec(value, source=KNOWLEDGE))
    assert [path for _, path in found] == ["/spec/layout/0/columns/0/value"]
    assert found[0][0].startswith("expression_")


def test_a_field_of_a_record_fits_its_format() -> None:
    assert _codes(_spec(_table(_col(field="validFrom", format="date")), source=KNOWLEDGE)) == []
    assert _codes(_spec(_table(_col(field="title", format="number")), source=KNOWLEDGE)) == [
        ("format_type_mismatch", "/spec/layout/0/columns/0/field")
    ]
    assert _codes(_spec(_table(_col(field="relations.x", format="percent")), source=KNOWLEDGE)) == [
        ("format_type_mismatch", "/spec/layout/0/columns/0/field")
    ]


def test_the_filters_of_knowledge_are_its_kinds_dates_and_text() -> None:
    paths = ["kind", "validFrom", "validTo", "attributes.inn", "title"]
    context = _texts(*(f"sample.fields.{p}" for p in paths))
    checked = check_view(
        _view(_spec(_table(_col(field="title"), filters=paths), source=KNOWLEDGE)), context
    )
    assert checked.problems == []
    assert checked.form is not None
    shown = present(checked.form, "en")["layout"][0]["filters"]
    assert [(f["field"], f["type"]) for f in shown] == [
        ("kind", "enum"),
        ("validFrom", "date"),
        ("validTo", "date"),
        ("attributes.inn", "text"),
        ("title", "text"),
    ]
    assert [o["value"] for o in shown[0]["options"]] == ["legal_entity", "contract"]


def test_an_expression_of_knowledge_reads_attributes_dynamically() -> None:
    env = source_environment("knowledge", None, {})
    values = {
        "id": "k:1",
        "kind": "k",
        "key": "1",
        "title": "",
        "validFrom": None,
        "validTo": None,
        "attributes": {"inn": "77"},
    }
    assert env.compile("attributes.inn").evaluate(values).value == "77"
    assert env.compile("validFrom == null").evaluate(values).value is True
    metrics = {"block": "metrics", "items": [{"title": "l", "value": "count(has(attributes.inn))"}]}
    assert _codes(_spec(metrics, source=KNOWLEDGE)) == []


# --- the ontology of the tree: warnings of the plan ----------------------------------------------

CATALOG = KnowledgeCatalog(
    kinds={
        "legal_entity": {
            "type": "object",
            "properties": {"inn": {"type": "string"}, "address": {"type": "object"}},
        },
        "contract": {"type": "object", "properties": {"amount": {"type": "number"}}},
        "loose": None,
    },
    aliases={"company": "legal_entity"},
    relations=frozenset({"customer_of", "signed_by"}),
)


def _warnings(spec: dict[str, Any], catalog: KnowledgeCatalog = CATALOG) -> list[tuple[str, str]]:
    found = check_knowledge(_view(spec), spec, catalog)
    assert all(p.severity == "warning" and p.file == "views/queue.yaml" for p in found)
    return [(p.code, p.path) for p in found]


def test_a_view_of_the_ontology_has_no_warning() -> None:
    table = _table(
        _col(field="attributes.inn"),
        _col(value="attributes.amount * 2"),
        _col(field="relations.signed_by"),
        filters=["attributes.inn", "kind"],
        sort=[{"field": "attributes.amount"}],
    )
    metrics = {"block": "metrics", "items": [{"title": "l", "value": "sum(attributes.amount)"}]}
    assert _warnings(_spec(table, metrics, source=KNOWLEDGE)) == []
    # A synonym of a kind names it.
    aliased = {"knowledge": {"kinds": ["company"]}}
    assert _warnings(_spec(_table(_col(field="attributes.inn")), source=aliased)) == []


def test_kinds_attributes_and_relations_the_ontology_lacks_are_warned() -> None:
    table = _table(
        _col(field="attributes.nowhere"),
        _col(value="attributes.inn + attributes.ghost"),
        _col(field="relations.knows"),
        filters=["attributes.lost"],
    )
    chart = {"block": "chart", "chart": "bar", "groupBy": "attributes.gone", "value": "count()"}
    source = {"knowledge": {"kinds": ["legal_entity", "planet"]}}
    assert _warnings(_spec(table, chart, source=source)) == [
        ("unknown_kind", "/spec/source/knowledge/kinds/1"),
        ("undeclared_path", "/spec/layout/0/columns/0/field"),
        ("undeclared_path", "/spec/layout/0/columns/1/value"),
        ("unknown_relation", "/spec/layout/0/columns/2/field"),
        ("undeclared_path", "/spec/layout/0/filters/0"),
        ("undeclared_path", "/spec/layout/1/groupBy"),
    ]


def test_an_attribute_of_any_kind_of_the_source_is_declared() -> None:
    table = _table(_col(field="attributes.amount"), _col(field="attributes.address.city"))
    # amount is of contract; address is an object without properties of legal_entity.
    found = _warnings(_spec(table, source=KNOWLEDGE))
    assert found == [("undeclared_path", "/spec/layout/0/columns/1/field")]


def test_a_kind_without_a_schema_of_attributes_takes_any() -> None:
    source = {"knowledge": {"kinds": ["loose", "legal_entity"]}}
    assert _warnings(_spec(_table(_col(field="attributes.anything")), source=source)) == []


def test_a_related_block_of_any_view_is_checked_against_the_ontology() -> None:
    related = {
        "block": "related",
        "knowledge": {"kind": "planet", "key": "param.id"},
        "include": {"relations": ["customer_of", "orbits"]},
    }
    spec = _spec(related, source=TASKS)
    assert knowledge_names(spec)
    assert _warnings(spec) == [
        ("unknown_kind", "/spec/layout/0/knowledge/kind"),
        ("unknown_relation", "/spec/layout/0/include/relations/1"),
    ]
    star = {**related, "knowledge": {"kind": "company", "key": "param.id"}, "include": {}}
    assert _warnings(_spec(star, source=TASKS)) == []


def test_the_blocks_of_an_inlined_component_are_checked_too() -> None:
    inner = _table(_col(field="attributes.ghost"))
    component = {"block": "component", "component": "c", "layout": [inner]}
    assert _warnings(_spec(component, source=KNOWLEDGE)) == [
        ("undeclared_path", "/spec/layout/0/layout/0/columns/0/field")
    ]


def test_views_that_name_no_knowledge_are_not_read() -> None:
    assert not knowledge_names(_spec(_table(_col(field="fields.title")), source=TASKS))
    assert not knowledge_names({"source": {}, "layout": None})
    assert knowledge_names(_spec(source=KNOWLEDGE))
    assert _warnings({"source": KNOWLEDGE, "layout": []}, KnowledgeCatalog(kinds={})) == [
        ("unknown_kind", "/spec/source/knowledge/kinds/0"),
        ("unknown_kind", "/spec/source/knowledge/kinds/1"),
    ]
