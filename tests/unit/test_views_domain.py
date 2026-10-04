"""The check of a view, a component and the dictionaries of a package (CP-ADR-0080).

Pure: every finding of TAI-ADR-0066 p.3 with its path, the edge cases of the
shape (empty, null, wrong type), the form a revision stores and the strings a
reader gets in a language.
"""

import copy
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.package_source import PackageObject, message_syntax, parse_package
from control_plane.domain.views import (
    BLOCKS,
    FORMATS,
    ProcessShape,
    ViewContext,
    check_component,
    check_locales,
    check_view,
    choose_locale,
    present,
    references,
    unused_messages,
    view_schema,
)

DATA = {
    "type": "object",
    "$defs": {"party": {"type": "object", "properties": {"name": {"type": "string"}}}},
    "properties": {
        "amount": {"type": "number"},
        "count": {"type": "integer"},
        "note": {"type": "string"},
        "openedAt": {"type": "string", "format": "date-time"},
        "wait": {"type": "string", "format": "duration"},
        "closed": {"type": "boolean"},
        "party": {"$ref": "#/$defs/party"},
        "extra": {"allOf": [{"properties": {"tag": {"type": "string"}}}]},
        "loose": {"type": "object"},
    },
}
# The parser reads <catalog>/v1: which catalog is the package tool's question.
API_VERSION = "catalog.example/v1"
KEYS = ["t", "l", "s", "g"]
CONTEXT = ViewContext(
    locales=("en", "ru"),
    default_locale="en",
    dictionaries={"en": {k: f"en:{k}" for k in KEYS}, "ru": {k: f"ru:{k}" for k in KEYS}},
    processes={"case": ProcessShape(DATA, ("intake", "done"))},
    task_types=frozenset({"item"}),
    roles=frozenset({"clerk"}),
    views=frozenset({"case-list", "case-card"}),
    package="sample",
)


def _table(*columns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"block": "table", "columns": list(columns), **extra}


def _col(**fields: Any) -> dict[str, Any]:
    return {"label": "l", **fields}


def _spec(*layout: dict[str, Any], **change: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "title": "t",
        "source": {"process": "case"},
        "layout": list(layout) or [_table(_col(field="data.amount"))],
    }
    spec.update(change)
    return spec


def _view(spec: dict[str, Any], key: str = "case-list") -> PackageObject:
    return PackageObject("View", key, spec, f"views/{key}.yaml")


def _codes(spec: dict[str, Any], context: ViewContext = CONTEXT) -> list[tuple[str, str]]:
    return [(p.code, p.path) for p in check_view(_view(spec), context).problems]


# --- the schema -----------------------------------------------------------------------------------


def test_the_schema_is_a_schema_and_names_the_set_of_version_1() -> None:
    schema = view_schema()
    Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    assert tuple(defs["block"]["properties"]["block"]["enum"]) == BLOCKS
    assert tuple(defs["format"]["enum"]) == FORMATS
    assert len(BLOCKS) == 13 and "component" in BLOCKS
    assert len(FORMATS) == 11


def test_a_good_view_has_no_finding_and_a_form() -> None:
    checked = check_view(_view(_spec()), CONTEXT)
    assert checked.problems == []
    assert checked.form is not None
    assert checked.form["blocks"] == 1
    assert checked.form["locales"] == ["en", "ru"] and checked.form["defaultLocale"] == "en"
    assert checked.form["messages"] == {
        "en": {"l": "en:l", "t": "en:t"},
        "ru": {"l": "ru:l", "t": "ru:t"},
    }


def test_the_form_is_the_same_for_the_same_view_whatever_the_order_of_its_keys() -> None:
    spec = _spec()
    reordered = dict(reversed(list(copy.deepcopy(spec).items())))
    one = check_view(_view(spec), CONTEXT).form
    two = check_view(_view(reordered), CONTEXT).form
    assert canonical_hash(one) == canonical_hash(two)


@pytest.mark.parametrize(
    ("spec", "code", "path"),
    [
        (
            {"source": {"process": "case"}, "layout": [_table(_col(field="data.amount"))]},
            "invalid_view",
            "/spec",
        ),
        (_spec(title=None), "invalid_view", "/spec/title"),
        (_spec(title=7), "invalid_view", "/spec/title"),
        (_spec(title="has space"), "invalid_view", "/spec/title"),
        (_spec(layout=[]), "invalid_view", "/spec/layout"),
        (_spec(layout=None), "invalid_view", "/spec/layout"),
        (_spec(source={}), "invalid_view", "/spec/source"),
        (_spec(source=None), "invalid_view", "/spec/source"),
        (_spec(extra=1), "invalid_view", "/spec"),
        (_spec(blocks=2), "invalid_view", "/spec/blocks"),
        (_spec(params={"id": {"type": "money"}}), "invalid_view", "/spec/params/id/type"),
        (_spec(audience={"roles": []}), "invalid_view", "/spec/audience/roles"),
        (_spec({"block": "widget"}), "unknown_block", "/spec/layout/0/block"),
        (_spec({"block": None}), "unknown_block", "/spec/layout/0/block"),
        (
            _spec(_table(_col(field="data.amount", format="badge"))),
            "unknown_format",
            "/spec/layout/0/columns/0/format",
        ),
        (_spec(_table(_col(field="data.amount"), widths=[1])), "invalid_view", "/spec/layout/0"),
        (_spec(code="() => null"), "component_code_not_supported", "/spec/code"),
    ],
)
def test_the_shape_is_refused_with_its_path(spec: dict[str, Any], code: str, path: str) -> None:
    assert (code, path) in _codes(spec)


def test_a_view_with_findings_of_its_shape_has_no_form() -> None:
    checked = check_view(_view(_spec({"block": "widget"})), CONTEXT)
    assert checked.form is None


# --- the source -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ({"process": "nowhere"}, ("unknown_source", "/spec/source/process")),
        ({"tasks": {"type": "nowhere"}}, ("unknown_source", "/spec/source/tasks/type")),
        ({"process": "case", "tasks": {"type": "item"}}, ("invalid_source", "/spec/source")),
        ({"filter": "true"}, ("invalid_source", "/spec/source")),
        ({"tasks": {"type": "item"}, "filter": "true"}, ("invalid_source", "/spec/source")),
        (
            {"knowledge": {"kinds": ["doc"]}, "instance": "param.id"},
            ("invalid_source", "/spec/source"),
        ),
        (
            {"process": "case", "filter": "data.note"},
            ("expression_type_error", "/spec/source/filter"),
        ),
        (
            {"process": "case", "filter": "data.nothing == 1"},
            ("expression_type_error", "/spec/source/filter"),
        ),
        (
            {"process": "case", "filter": "data.amount >"},
            ("expression_syntax_error", "/spec/source/filter"),
        ),
        (
            {"process": "case", "filter": "count() > 1"},
            ("aggregate_outside_metrics", "/spec/source/filter"),
        ),
    ],
)
def test_the_source_exists_and_its_filter_compiles(
    source: dict[str, Any], found: tuple[str, str]
) -> None:
    layout = [{"block": "metrics", "items": [{"title": "l", "value": "count()"}]}]
    assert found in _codes(_spec(*layout, source=source))


def test_every_source_of_the_set_is_accepted() -> None:
    metrics = {"block": "metrics", "items": [{"title": "l", "value": "count()"}]}
    for source in (
        {"process": "case", "filter": "data.amount > 10.0 && param.since != ''"},
        {"tasks": {"type": "item"}},
        {"knowledge": {"kinds": ["doc", "adr"]}},
    ):
        assert _codes(_spec(metrics, source=source, params={"since": {"type": "string"}})) == []


def test_an_instance_is_a_param_written_or_implied() -> None:
    header = {"block": "header", "title": "data.note"}
    source = {"process": "case", "instance": "param.id"}
    assert _codes(_spec(header, source=source, params={"id": {"type": "uuid"}})) == []
    # TAI-ADR-0066 p.1 writes a card without params: the id of the instance is implied.
    implied = check_view(_view(_spec(header, source=source)), CONTEXT)
    assert implied.problems == []
    assert implied.form is not None
    assert implied.form["params"] == {"id": {"type": "uuid", "required": True}}
    with_filter = {**source, "filter": "true"}
    found = _codes(_spec(header, source=with_filter, params={"id": {"type": "uuid"}}))
    assert ("invalid_source", "/spec/source/filter") in found


@pytest.mark.parametrize("kind", ["integer", "boolean", "date"])
def test_the_param_of_an_instance_is_an_id(kind: str) -> None:
    header = {"block": "header", "title": "data.note"}
    source = {"process": "case", "instance": "param.id"}
    found = _codes(_spec(header, source=source, params={"id": {"type": kind}}))
    assert ("invalid_source", "/spec/params/id/type") in found


# --- paths and expressions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "data.amount",
        "data.party.name",
        "data.extra.tag",
        "data.loose",
        "instance.startedAt",
        "stage",
        "status",
        "slaState",
        "id",
    ],
)
def test_a_declared_path_is_accepted(field: str) -> None:
    assert _codes(_spec(_table(_col(field=field)))) == []


@pytest.mark.parametrize(
    "field",
    [
        "data.nothing",
        "data.party.age",
        "data.loose.inner",
        "data.amount.x",
        "instance.bogus",
        "event.time",
        "data",
    ],
)
def test_a_path_the_data_schema_does_not_declare_is_refused(field: str) -> None:
    assert ("undeclared_path", "/spec/layout/0/columns/0/field") in _codes(
        _spec(_table(_col(field=field)))
    )


def test_paths_of_filters_sort_and_group_are_declared_too() -> None:
    table = _table(
        _col(field="data.amount"),
        filters=["data.note", "data.nothing"],
        sort=[{"field": "data.missing", "dir": "asc"}],
    )
    chart = {"block": "chart", "chart": "bar", "groupBy": "data.gone", "value": "count()"}
    found = _codes(_spec(table, chart))
    assert ("undeclared_path", "/spec/layout/0/filters/1") in found
    assert ("undeclared_path", "/spec/layout/0/sort/0/field") in found
    assert ("undeclared_path", "/spec/layout/1/groupBy") in found
    assert ("undeclared_path", "/spec/layout/0/filters/0") not in found


# The paths of tasks and knowledge (stage 6): tests/unit/test_view_sources_domain.py.


@pytest.mark.parametrize(
    ("column", "fits"),
    [
        (_col(field="data.amount", format="money"), True),
        (_col(field="data.amount", format="number"), True),
        (_col(field="data.count", format="percent"), True),
        (_col(field="data.note", format="money"), True),
        (_col(field="data.openedAt", format="due"), True),
        (_col(field="data.openedAt", format="date"), True),
        (_col(field="data.wait", format="duration"), True),
        (_col(field="data.note", format="principal"), True),
        (_col(field="stage", format="status"), True),
        (_col(field="data.loose", format="text"), False),
        (_col(value="data.amount * 2.0", format="number"), True),
        (_col(field="data.amount", format="date"), False),
        (_col(field="data.note", format="number"), False),
        (_col(field="data.closed", format="text"), False),
        (_col(field="data.openedAt", format="duration"), False),
        (_col(value="data.amount > 1.0", format="money"), False),
        (_col(field="stage", format="number"), False),
    ],
)
def test_the_type_of_a_value_fits_its_format(column: dict[str, Any], fits: bool) -> None:
    found = [code for code, _ in _codes(_spec(_table(column)))]
    assert ("format_type_mismatch" not in found) is fits, found


def test_a_column_has_exactly_one_of_field_or_value() -> None:
    assert ("invalid_column", "/spec/layout/0/columns/0") in _codes(
        _spec(_table(_col(field="data.note", value="data.note")))
    )
    assert ("invalid_column", "/spec/layout/0/columns/0") in _codes(_spec(_table(_col())))


def test_a_value_without_a_format_is_shown_by_its_type() -> None:
    assert _codes(_spec(_table(_col(value="data.closed")))) == []


# --- aggregates -----------------------------------------------------------------------------------


def _metrics(*values: str, format: str | None = None) -> dict[str, Any]:
    items = [
        {"key": f"m{i}", "title": "l", "value": v, **({"format": format} if format else {})}
        for i, v in enumerate(values)
    ]
    return {"block": "metrics", "items": items}


def test_aggregates_are_written_in_metrics_and_chart() -> None:
    chart = {"block": "chart", "chart": "bar", "groupBy": "stage", "value": "avg(data.amount)"}
    good = _metrics(
        "count()", "count(data.closed)", "sum(data.amount)", "min(data.openedAt)", "max(data.count)"
    )
    assert _codes(_spec(good, chart)) == []


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("data.amount", "invalid_aggregate"),
        ("sum(count())", "invalid_aggregate"),
        ("median(data.amount)", "invalid_aggregate"),
        ("sum(data.note)", "expression_type_error"),
        ("avg(data.closed)", "expression_type_error"),
        ("count(data.amount)", "expression_type_error"),
        ("sum(data.nothing)", "expression_type_error"),
    ],
)
def test_a_value_of_metrics_is_one_aggregate(value: str, code: str) -> None:
    assert (code, "/spec/layout/0/items/0/value") in _codes(_spec(_metrics(value)))


def test_an_aggregate_fits_the_format_of_its_value() -> None:
    assert _codes(_spec(_metrics("count()", format="number"))) == []
    found = _codes(_spec(_metrics("count()", format="date")))
    assert ("format_type_mismatch", "/spec/layout/0/items/0/value") in found
    found = _codes(_spec(_metrics("max(data.openedAt)", format="money")))
    assert ("format_type_mismatch", "/spec/layout/0/items/0/value") in found


@pytest.mark.parametrize(
    ("layout", "path"),
    [
        (_table(_col(value="sum(data.amount)")), "/spec/layout/0/columns/0/value"),
        (_table(_col(value="max (data.amount)")), "/spec/layout/0/columns/0/value"),
        (
            _table(_col(field="data.note"), open={"view": "case-card", "params": {"n": "count()"}}),
            "/spec/layout/0/open/params/n",
        ),
    ],
)
def test_an_aggregate_outside_metrics_and_chart_is_refused(
    layout: dict[str, Any], path: str
) -> None:
    assert ("aggregate_outside_metrics", path) in _codes(_spec(layout))


def test_a_method_named_like_an_aggregate_is_no_aggregate() -> None:
    assert _codes(_spec(_table(_col(value="data.note.size()")))) == []


# --- references -----------------------------------------------------------------------------------


def test_open_view_names_a_view_of_the_package_or_of_a_required_one() -> None:
    table = _table(
        _col(field="data.note"), open={"view": "case-card", "params": {"id": "instance.id"}}
    )
    assert _codes(_spec(table)) == []
    table["open"]["view"] = "elsewhere"
    assert ("unknown_view", "/spec/layout/0/open/view") in _codes(_spec(table))
    # A record is opened by its block: a column opens nothing of its own.
    column = _col(field="data.note", open={"view": "case-card"})
    assert ("invalid_view", "/spec/layout/0/columns/0") in _codes(_spec(_table(column)))


def test_audience_roles_are_roles_of_the_organization() -> None:
    assert _codes(_spec(audience={"roles": ["clerk"]})) == []
    assert ("unknown_role", "/spec/audience/roles/1") in _codes(
        _spec(audience={"roles": ["clerk", "nobody"]})
    )


@pytest.mark.parametrize(
    ("block", "source"),
    [
        ({"block": "header", "title": "data.note"}, {"process": "case"}),
        ({"block": "fields", "items": [_col(field="data.note")]}, {"process": "case"}),
        ({"block": "steps"}, {"tasks": {"type": "item"}}),
        ({"block": "timeline"}, {"knowledge": {"kinds": ["doc"]}}),
        (_table(_col(field="data.note")), {"process": "case", "instance": "param.id"}),
    ],
)
def test_a_block_fits_its_source(block: dict[str, Any], source: dict[str, Any]) -> None:
    found = _codes(_spec(block, source=source, params={"id": {"type": "uuid"}}))
    assert ("block_source_mismatch", "/spec/layout/0") in found


# --- strings --------------------------------------------------------------------------------------


def test_every_string_is_a_key_of_every_declared_dictionary() -> None:
    context = ViewContext(
        **{**CONTEXT.__dict__, "dictionaries": {"en": {"t": "T", "l": "L"}, "ru": {"t": "T-ru"}}}
    )
    found = check_view(_view(_spec(description="g")), context).problems
    assert {(p.code, p.path) for p in found} == {
        ("missing_message", "/spec/layout/0/columns/0/label"),
        ("missing_message", "/spec/description"),
    }
    by_path = {p.path: p.message for p in found}
    assert by_path["/spec/description"].endswith("en, ru")
    assert by_path["/spec/layout/0/columns/0/label"].endswith("ru")


def test_the_strings_of_every_block_are_keys() -> None:
    blocks = [
        {
            "block": "board",
            "title": "t",
            "columns": "stages",
            "card": {"title": "data.note", "fields": [_col(field="data.note")]},
        },
        {
            "block": "chart",
            "chart": "donut",
            "groupBy": "stage",
            "value": "count()",
            "label": "missing.key",
        },
        {"block": "invoke", "label": "missing.too", "skill": "doc.summarize@1"},
    ]
    found = {(p.code, p.path) for p in check_view(_view(_spec(*blocks)), CONTEXT).problems}
    assert found == {
        ("missing_message", "/spec/layout/1/label"),
        ("missing_message", "/spec/layout/2/label"),
        ("unknown_skill", "/spec/layout/2/skill"),
    }


# --- components -----------------------------------------------------------------------------------


def _component(spec: dict[str, Any], key: str = "summary") -> PackageObject:
    return PackageObject("Component", key, spec, f"components/{key}.yaml")


def test_a_component_is_inlined_and_checked_in_the_view_that_names_it() -> None:
    component = _component({"layout": [_table(_col(field="data.note"))]})
    context = ViewContext(
        **{
            **CONTEXT.__dict__,
            "components": {"summary": component},
            "component_keys": frozenset({"summary"}),
        }
    )
    assert check_component(component, context) == []
    checked = check_view(_view(_spec({"block": "component", "component": "summary"})), context)
    assert checked.problems == []
    assert checked.form is not None
    assert checked.form["layout"] == [
        {"block": "component", "component": "summary", "layout": [_table(_col(field="data.note"))]}
    ]
    # A path the view's source does not declare is a finding of the component's file.
    broken = _component({"layout": [_table(_col(field="data.nothing"))]})
    context = ViewContext(**{**context.__dict__, "components": {"summary": broken}})
    found = check_view(
        _view(_spec({"block": "component", "component": "summary"})), context
    ).problems
    assert [(p.code, p.file, p.path) for p in found] == [
        ("undeclared_path", "components/summary.yaml", "/spec/layout/0/columns/0/field")
    ]


def test_a_component_is_a_description_of_one_level() -> None:
    coded = check_component(
        _component({"layout": [_table(_col(field="data.note"))], "code": "x"}), CONTEXT
    )
    assert ("component_code_not_supported", "/spec/code") in {(p.code, p.path) for p in coded}
    nested = check_component(
        _component({"layout": [{"block": "component", "component": "other"}]}), CONTEXT
    )
    assert [(p.code, p.path) for p in nested] == [("nested_component", "/spec/layout/0")]
    empty = check_component(_component({"layout": []}), CONTEXT)
    assert [p.code for p in empty] == ["invalid_component"]
    assert ("unknown_component", "/spec/layout/0/component") in _codes(
        _spec({"block": "component", "component": "summary"})
    )


# --- the package: locales and dictionaries --------------------------------------------------------


def _files(manifest: dict[str, Any], **files: str) -> list[tuple[str, str]]:
    head = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample",
        "spec": {"version": "1.0.0", "displayName": "S", **manifest},
    }
    view = {"apiVersion": API_VERSION, "kind": "View", "key": "case-list", "spec": _spec()}
    return [
        ("package.yaml", yaml.safe_dump(head)),
        ("views/v.yaml", yaml.safe_dump(view)),
        *files.items(),
    ]


def _locale_codes(manifest: dict[str, Any], **files: str) -> list[tuple[str, str | None, str]]:
    package = parse_package(_files(manifest, **files))
    return [(p.code, p.file, p.path) for p in package.problems + check_locales(package)]


def test_the_dictionaries_are_parsed_and_not_taken_for_objects() -> None:
    package = parse_package(
        _files(
            {"locales": ["en"], "defaultLocale": "en"},
            **{"i18n/en.yaml": "t: Title\nl: 'Label {n}'\n"},
        )
    )
    assert package.problems == []
    assert package.dictionaries["en"].messages == {"t": "Title", "l": "Label {n}"}
    assert check_locales(package) == []


@pytest.mark.parametrize(
    ("manifest", "files", "found"),
    [
        ({}, {}, ("locales_required", "package.yaml", "/spec")),
        (
            {"locales": "en", "defaultLocale": "en"},
            {},
            ("invalid_locales", "package.yaml", "/spec/locales"),
        ),
        (
            {"locales": [], "defaultLocale": "en"},
            {},
            ("invalid_locales", "package.yaml", "/spec/locales"),
        ),
        (
            {"locales": ["en", "en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A"},
            ("invalid_locales", "package.yaml", "/spec/locales/1"),
        ),
        (
            {"locales": ["EN_us"], "defaultLocale": "EN_us"},
            {},
            ("invalid_locales", "package.yaml", "/spec/locales/0"),
        ),
        (
            {"locales": ["en", "ru"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A"},
            ("missing_dictionary", "package.yaml", "/spec/locales/1"),
        ),
        (
            {"locales": ["en"]},
            {"i18n/en.yaml": "a: A"},
            ("invalid_default_locale", "package.yaml", "/spec/defaultLocale"),
        ),
        (
            {"locales": ["en"], "defaultLocale": None},
            {"i18n/en.yaml": "a: A"},
            ("invalid_default_locale", "package.yaml", "/spec/defaultLocale"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "ru"},
            {"i18n/en.yaml": "a: A"},
            ("invalid_default_locale", "package.yaml", "/spec/defaultLocale"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A", "i18n/de.yaml": "a: A"},
            ("undeclared_locale", "i18n/de.yaml", ""),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A", "i18n/en.yml": "a: A"},
            ("invalid_dictionary", "i18n/en.yml", ""),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "- a\n- b\n"},
            ("invalid_dictionary", "i18n/en.yaml", ""),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A", "i18n/x/en.yaml": "a: A"},
            ("invalid_dictionary", "i18n/x/en.yaml", ""),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: A", "i18n/English.yaml": "a: A"},
            ("invalid_dictionary", "i18n/English.yaml", ""),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: 1\n"},
            ("invalid_message", "i18n/en.yaml", "/a"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: null\n"},
            ("invalid_message", "i18n/en.yaml", "/a"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "'a b': A\n"},
            ("invalid_message", "i18n/en.yaml", "/a b"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: 'Hello {name'\n"},
            ("invalid_message", "i18n/en.yaml", "/a"),
        ),
        (
            {"locales": ["en"], "defaultLocale": "en"},
            {"i18n/en.yaml": "a: [x\n"},
            ("invalid_yaml", "i18n/en.yaml", ""),
        ),
    ],
)
def test_locales_and_dictionaries_are_refused_with_their_place(
    manifest: dict[str, Any], files: dict[str, str], found: tuple[str, str, str]
) -> None:
    assert found in _locale_codes(manifest, **files)


def test_an_empty_dictionary_is_a_dictionary_without_keys() -> None:
    package = parse_package(
        _files({"locales": ["en"], "defaultLocale": "en"}, **{"i18n/en.yaml": ""})
    )
    assert package.dictionaries["en"].messages == {}
    assert check_locales(package) == []


def test_a_package_without_screens_needs_no_locales() -> None:
    head = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample",
        "spec": {"version": "1.0.0", "displayName": "S"},
    }
    assert check_locales(parse_package([("package.yaml", yaml.safe_dump(head))])) == []


def test_a_key_no_screen_shows_is_a_warning() -> None:
    package = parse_package(
        _files(
            {"locales": ["en"], "defaultLocale": "en"}, **{"i18n/en.yaml": "t: T\nl: L\nspare: S\n"}
        )
    )
    found = unused_messages(package, {"t", "l"})
    assert [(p.code, p.severity, p.file, p.path) for p in found] == [
        ("unused_message", "warning", "i18n/en.yaml", "/spare")
    ]


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("plain", True),
        ("Hello {name}", True),
        ("{count, plural, one {# item} other {# items}}", True),
        ("Quoted '{' brace", True),
        ("It''s {n}", True),
        ("", True),
        ("{open", False),
        ("close}", False),
        ("{a}}", False),
    ],
)
def test_the_braces_of_a_message_pair_up(text: str, ok: bool) -> None:
    assert (message_syntax(text) is None) is ok


def test_the_kinds_of_screens_are_known_to_the_parser_and_keyed_like_routes() -> None:
    bad = {"apiVersion": API_VERSION, "kind": "View", "key": "Has Space", "spec": _spec()}
    package = parse_package([("views/bad.yaml", yaml.safe_dump(bad))])
    assert [(p.code, p.path) for p in package.problems] == [("invalid_document", "/key")]


# --- what a reader gets ---------------------------------------------------------------------------


def test_the_locale_is_the_one_asked_its_language_or_the_default() -> None:
    form = {"locales": ["en", "pt-BR", "ru"], "defaultLocale": "en"}
    assert choose_locale(form, "ru") == "ru"
    assert choose_locale(form, "RU") == "ru"
    assert choose_locale(form, "ru-RU") == "ru"
    assert choose_locale(form, "pt-br") == "pt-BR"
    assert choose_locale(form, "pt") == "en"
    assert choose_locale(form, "de") == "en"
    assert choose_locale(form, "") == "en"
    assert choose_locale(form, None) == "en"


def test_the_strings_are_substituted_and_a_missing_one_falls_back() -> None:
    checked = check_view(_view(_spec(description="g")), CONTEXT)
    form = copy.deepcopy(checked.form)
    assert form is not None
    del form["messages"]["ru"]["g"]
    del form["messages"]["en"]["l"]
    del form["messages"]["ru"]["l"]
    shown = present(form, "ru")
    assert shown["title"] == "ru:t"
    assert shown["description"] == "en:g"
    assert shown["layout"][0]["columns"][0]["title"] == "l"
    assert "messages" not in shown and "locales" not in shown
    # The stored form is not changed by a reader.
    assert form["title"] == "t"
    assert form["display"]["title"] == {"$t": "t"}


def test_references_name_what_the_check_reads_from_the_catalog() -> None:
    spec = _spec(
        _table(_col(field="data.note"), open={"view": "case-card"}),
        {"block": "invoke", "label": "l", "skill": "doc.summarize@1"},
        audience={"roles": ["clerk"]},
    )
    package = parse_package(
        _files(
            {},
            **{
                "views/w.yaml": yaml.safe_dump(
                    {"apiVersion": API_VERSION, "kind": "View", "key": "w", "spec": spec}
                )
            },
        )
    )
    named = references(package)
    assert named.processes == {"case"}
    assert named.skills == {"doc.summarize@1"}
    assert named.roles == {"clerk"}
    assert named.views == {"case-card"}
