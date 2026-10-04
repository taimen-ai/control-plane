"""``GET /views`` in the form agreed with the console on 2026-10-03 (CP-ADR-0080 §9).

The consumer side is ``docs/views.md`` of the console repository. The form is
transcribed here independently of the core's models — :data:`CONSOLE_CONTRACT`
is what the console draws — and checked three ways:

- the views of TAI-ADR-0066 §1 (``tests/fixtures/views/tenders``) are presented
  exactly as the examples below write them;
- a view with every block of the set presents into the contract;
- the openapi models of the routes accept the examples, and the routes are
  the paths the console's BFF opens.

What the core adds to the agreed form, all optional for the console, is named
in the schema with its reason: ``blocks`` and ``locale`` of a view (the
version of the set and the language the strings came in), ``description``,
the ``title`` of a block other than ``chart`` and ``related``, the install
fields of ``package``. Nothing of the raw package form (paths, CEL, the raw
``source``) is in it: ``additionalProperties`` is false everywhere but in
``invoke`` and ``component``, which the console shows as "needs a newer
console" and which go as the package writes them.
"""

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from control_plane.api.v1.router import api_v1_router
from control_plane.domain.views import check_view, present
from tests.unit.test_views_blocks import (
    WITH_SKILLS,
    _tender_context,
    _tender_view,
    _tenders,
)
from tests.unit.test_views_domain import _view

_STR: dict[str, Any] = {"type": "string"}
_FORMAT = {
    "enum": [
        "text",
        "number",
        "money",
        "percent",
        "date",
        "datetime",
        "due",
        "duration",
        "principal",
        "status",
        "link",
    ]
}
# Added by the core: the caption of a block (a key of the dictionaries in text).
_TITLE = _STR


def _object(required: list[str], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": properties,
    }


def _block(name: str | list[str], required: list[str], **properties: Any) -> dict[str, Any]:
    kinds = [name] if isinstance(name, str) else name
    return _object(["block", *required], block={"enum": kinds}, **properties)


_COLUMN = _object(["key", "title"], key=_STR, title=_STR, format=_FORMAT)
_FILTER = _object(
    ["field", "title", "type"],
    field=_STR,
    title=_STR,
    type={"enum": ["text", "enum", "number", "date"]},
    options={
        "type": "array",
        "items": _object(
            ["value", "title"],
            value={"type": ["string", "number", "boolean"]},
            title=_STR,
        ),
    },
)
_SORT = _object(["field", "title"], field=_STR, title=_STR)
_OPEN = _object(["view"], view=_STR)
_BLOCKS = [
    _block("metrics", ["items"], title=_TITLE, items={"type": "array", "items": _COLUMN}),
    _block(
        ["table", "list"],
        ["columns"],
        title=_TITLE,
        columns={"type": "array", "items": _COLUMN},
        filters={"type": "array", "items": _FILTER},
        sort={"type": "array", "items": _SORT},
        open=_OPEN,
    ),
    _block(
        "board",
        ["card"],
        title=_TITLE,
        card=_object(
            ["fields"],
            fields={"type": "array", "items": _object(["key"], key=_STR, format=_FORMAT)},
        ),
        filters={"type": "array", "items": _FILTER},
        open=_OPEN,
    ),
    _block("chart", ["type"], type={"enum": ["bar", "line", "donut"]}, title=_STR, format=_FORMAT),
    _block(
        "fields",
        ["items"],
        title=_TITLE,
        section=_STR,
        items={
            "type": "array",
            "items": _object(["key", "label"], key=_STR, label=_STR, format=_FORMAT),
        },
    ),
    # Its title, status and lead come in the data of the view; actions go as written.
    _block("header", [], actions={}),
    _block(["steps", "timeline", "artifacts"], [], title=_TITLE),
    _block("related", [], title=_STR, relations={"type": "array", "items": _STR}),
    {"type": "object", "required": ["block"], "properties": {"block": {"const": "invoke"}}},
    {
        "type": "object",
        "required": ["block", "component"],
        "properties": {"block": {"const": "component"}, "component": _STR},
    },
]
_SUMMARY_FIELDS: dict[str, Any] = {
    "key": _STR,
    "title": _STR,
    "revision": {"type": "integer", "minimum": 1},
    "hash": _STR,
    "package": {
        "type": ["object", "null"],
        "required": ["key", "version"],
        "properties": {"key": _STR, "version": {"type": ["string", "null"]}},
    },
    "nav": {
        "type": ["object", "null"],
        "required": ["group"],
        "additionalProperties": False,
        "properties": {
            "group": {"enum": ["work", "knowledge", "packages"]},
            "icon": {"type": ["string", "null"]},
            "order": {"type": ["integer", "null"]},
        },
    },
    "source": _object(
        ["kind", "instance"],
        kind={"enum": ["process", "tasks", "knowledge"]},
        process=_STR,
        instance={"type": "boolean"},
    ),
    # Added by the core.
    "blocks": {"const": 1},
    "locale": _STR,
    "description": _STR,
}
_REQUIRED = ["key", "title", "revision", "hash", "source", "blocks"]
VIEW_SUMMARY = _object(_REQUIRED, **_SUMMARY_FIELDS)
VIEW = _object(
    [*_REQUIRED, "layout"],
    **_SUMMARY_FIELDS,
    layout={"type": "array", "items": {"oneOf": _BLOCKS}},
)
VIEW_PAGE = _object(
    ["items", "nextCursor"],
    items={"type": "array", "items": VIEW_SUMMARY},
    nextCursor={"type": ["string", "null"]},
)
CONSOLE_CONTRACT = {"summary": VIEW_SUMMARY, "view": VIEW, "page": VIEW_PAGE}


def assert_console_form(body: dict[str, Any], kind: str = "view") -> None:
    """``body`` is in the form the console draws; the findings, if not."""
    errors = sorted(
        Draft202012Validator(CONSOLE_CONTRACT[kind]).iter_errors(body),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, [(list(e.absolute_path), e.message[:200]) for e in errors]


# --- the examples: the views of the decision, presented -------------------------------------------

BOARD_RU: dict[str, Any] = {
    "title": "Тендеры",
    "nav": {"group": "work", "icon": "tender", "order": 20},
    "source": {"kind": "process", "process": "tender", "instance": False},
    "layout": [
        {
            "block": "metrics",
            "items": [
                {"key": "inProgress", "title": "Тендеров в работе"},
                {"key": "totalPrice", "title": "Сумма НМЦК", "format": "money"},
            ],
        },
        {
            "block": "board",
            "card": {
                "fields": [
                    {"key": "procurement.maxPrice.amount", "format": "money"},
                    {"key": "procurement.deadlines.submissionEnd", "format": "due"},
                ]
            },
            "open": {"view": "tender-card"},
        },
    ],
}
CARD_EN: dict[str, Any] = {
    "title": "Tender",
    "source": {"kind": "process", "process": "tender", "instance": True},
    "layout": [
        {"block": "header", "actions": "steps"},
        {
            "block": "fields",
            "section": "Procurement",
            "items": [
                {"key": "procurement.number", "label": "Number"},
                {"key": "procurement.law", "label": "Law"},
                {
                    "key": "procurement.maxPrice.amount",
                    "label": "Starting price",
                    "format": "money",
                },
            ],
        },
        {"block": "related"},
        {"block": "artifacts"},
        {"block": "timeline"},
    ],
}


def _presented(key: str, locale: str) -> dict[str, Any]:
    package = _tenders()
    checked = check_view(_tender_view(package, key), _tender_context(package))
    assert checked.problems == []
    assert checked.form is not None
    return present(checked.form, locale)


def _as_served(shown: dict[str, Any], key: str, locale: str) -> dict[str, Any]:
    """What ``GET /views/{key}`` adds to the display: identity, revision, package."""
    return {
        "key": key,
        "revision": 1,
        "hash": "0" * 64,
        "blocks": 1,
        "locale": locale,
        "package": {"key": "tenders", "version": "1.0.0"},
        **shown,
    }


def test_the_board_of_the_decision_is_presented_as_the_console_draws_it() -> None:
    shown = _presented("tenders-board", "ru")
    assert shown == BOARD_RU
    assert_console_form(_as_served(shown, "tenders-board", "ru"))


def test_the_card_of_the_decision_is_presented_as_the_console_draws_it() -> None:
    shown = _presented("tender-card", "en-GB")
    assert shown == CARD_EN
    assert_console_form(_as_served(shown, "tender-card", "en"))


def test_the_summary_of_a_list_has_no_layout() -> None:
    summary = _as_served(_presented("tenders-board", "ru"), "tenders-board", "ru")
    del summary["layout"]
    assert_console_form(summary, "summary")
    assert_console_form({"items": [summary], "nextCursor": None}, "page")
    with pytest.raises(AssertionError):
        assert_console_form({**summary, "layout": []}, "summary")


def test_no_path_no_expression_and_no_raw_source_reach_the_console() -> None:
    for key in ("tenders-board", "tender-card"):
        text = repr(_presented(key, "en"))
        for raw in ("data.", "decimal(", "count(", "param.", "status ==", "filter", "slaState"):
            assert raw not in text, (key, raw)


# --- every block of the set -----------------------------------------------------------------------

EVERY_BLOCK_LIST: dict[str, Any] = {
    "title": "t",
    "nav": {"icon": "inbox"},
    "source": {"process": "case", "filter": "data.amount > 1.0"},
    "layout": [
        {
            "block": "table",
            "title": "t",
            "columns": [
                {"label": "l", "field": "data.note"},
                {"field": "data.amount", "format": "money"},
            ],
            "filters": ["data.closed", "stage"],
            "sort": [{"field": "data.amount", "dir": "desc"}],
            "open": {"view": "case-card", "id": "id"},
            "pageSize": 20,
        },
        {"block": "list", "columns": [{"label": "l", "value": "data.note.size()", "key": "size"}]},
        {
            "block": "board",
            "columns": "stages",
            "card": {
                "title": "data.note",
                "badge": "slaState",
                "fields": [{"field": "data.amount"}],
            },
            "filters": ["status"],
            "open": {"view": "case-card"},
        },
        {
            "block": "metrics",
            "items": [{"title": "l", "value": "sum(data.amount)", "format": "number"}],
        },
        {"block": "chart", "chart": "donut", "groupBy": "stage", "value": "count()", "label": "l"},
        {"block": "related", "knowledge": {"kind": "contract", "key": "data.note"}},
        {
            "block": "related",
            "title": "t",
            "knowledge": {"kind": "contract", "key": "data.note"},
            "include": {"relations": ["party"]},
        },
        {"block": "artifacts", "types": ["report"]},
        {
            "block": "invoke",
            "label": "l",
            "skill": "doc.summarize@1",
            "input": {"text": "data.note"},
        },
    ],
}
EVERY_BLOCK_CARD: dict[str, Any] = {
    "title": "t",
    "description": "g",
    "source": {"process": "case", "instance": "param.id"},
    "layout": [
        {"block": "header", "title": "data.note", "status": "stage", "actions": "steps"},
        {
            "block": "fields",
            "title": "t",
            "section": "s",
            "items": [{"label": "l", "field": "data.note"}],
        },
        {"block": "steps"},
        {"block": "timeline", "title": "t"},
    ],
}


def _every_block_context() -> Any:
    names = ["note", "amount", "closed", "stage", "status"]
    texts = {f"sample.fields.{name}": f"T:{name}" for name in names}
    dictionaries = {loc: {**words, **texts} for loc, words in WITH_SKILLS.dictionaries.items()}
    return dataclasses.replace(WITH_SKILLS, dictionaries=dictionaries)


@pytest.mark.parametrize(
    ("spec", "key"), [(EVERY_BLOCK_LIST, "case-list"), (EVERY_BLOCK_CARD, "case-card")]
)
def test_every_block_of_the_set_is_presented_into_the_contract(
    spec: dict[str, Any], key: str
) -> None:
    checked = check_view(_view(spec, key), _every_block_context())
    assert checked.problems == []
    assert checked.form is not None
    for locale in ("en", "ru"):
        assert_console_form(_as_served(present(checked.form, locale), key, locale))


def test_a_presented_table_carries_keys_titles_typed_filters_and_open_only() -> None:
    checked = check_view(_view(EVERY_BLOCK_LIST, "case-list"), _every_block_context())
    assert checked.form is not None
    table, listed, board, metrics, chart, related, included, artifacts, invoke = present(
        checked.form, "ru"
    )["layout"]
    assert table == {
        "block": "table",
        "title": "ru:t",
        "columns": [
            {"key": "note", "title": "ru:l"},
            {"key": "amount", "title": "T:amount", "format": "money"},
        ],
        "filters": [
            {
                "field": "closed",
                "title": "T:closed",
                "type": "enum",
                "options": [{"value": True, "title": "true"}, {"value": False, "title": "false"}],
            },
            {
                "field": "stage",
                "title": "T:stage",
                "type": "enum",
                "options": [
                    {"value": "intake", "title": "intake"},
                    {"value": "done", "title": "done"},
                ],
            },
        ],
        "sort": [{"field": "amount", "title": "T:amount"}],
        "open": {"view": "case-card"},
    }
    assert listed == {"block": "list", "columns": [{"key": "size", "title": "ru:l"}]}
    assert board["card"] == {"fields": [{"key": "amount"}]}
    assert board["filters"][0]["field"] == "status"
    assert metrics == {
        "block": "metrics",
        "items": [{"key": "l", "title": "ru:l", "format": "number"}],
    }
    assert chart == {"block": "chart", "type": "donut", "title": "ru:l"}
    assert related == {"block": "related"}
    assert included == {"block": "related", "title": "ru:t", "relations": ["party"]}
    assert artifacts == {"block": "artifacts"}
    assert invoke == {
        "block": "invoke",
        "label": "ru:l",
        "skill": "doc.summarize@1",
        "input": {"text": "data.note"},
    }


# --- the routes and their models ------------------------------------------------------------------


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def _model(openapi: dict[str, Any], name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"components": openapi["components"], "$ref": f"#/components/schemas/{name}"}
    )


def test_the_routes_are_the_paths_the_console_opens_with_their_models() -> None:
    openapi = _openapi()
    paths = openapi["paths"]
    one = paths["/api/v1/views/{view_key}"]["get"]
    many = paths["/api/v1/views"]["get"]
    assert one["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ViewOut"
    }
    assert many["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ViewSummaryPageOut"
    }
    assert "layout" not in openapi["components"]["schemas"]["ViewSummaryOut"]["properties"]
    assert {p["name"] for p in many["parameters"]} >= {"locale", "limit", "cursor", "package"}


def test_the_models_of_the_routes_accept_the_examples() -> None:
    openapi = _openapi()
    view = _model(openapi, "ViewOut")
    page = _model(openapi, "ViewSummaryPageOut")
    for shown, key, locale in (
        (_presented("tenders-board", "ru"), "tenders-board", "ru"),
        (_presented("tender-card", "en"), "tender-card", "en"),
    ):
        body = _as_served(shown, key, locale)
        body["package"] = {
            "key": "tenders",
            "version": "1.0.0",
            "installHash": "0" * 64,
            "installedAt": "2026-10-03T00:00:00Z",
        }
        assert list(view.iter_errors(body)) == []
        summary = {k: v for k, v in body.items() if k != "layout"}
        assert list(page.iter_errors({"items": [summary], "nextCursor": None})) == []
