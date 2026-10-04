"""``POST /views/{key}:query`` in the form agreed with the console (CP-ADR-0080 amendment A).

The consumer side is ``docs/views.md`` of the console repository, section
``POST /api/v1/views/{key}:query`` (console commit 54f7b58, the comment of the
owner on TASK-001299). The form is transcribed here independently of the
core's models — :data:`QUERY_CONTRACT`, a JSON Schema per block,
``additionalProperties`` false everywhere — and checked three ways:

- the examples of the agreed form (:data:`EXAMPLES`) are in it;
- the openapi models of the route accept the examples and the request bodies
  the console sends;
- the answers of the route (``tests/integration/test_view_query.py``) are in
  it, block by block (:func:`assert_query_form`).
"""

from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from control_plane.api.v1.router import api_v1_router

_STR: dict[str, Any] = {"type": "string"}
_CATEGORY = {"enum": ["running", "suspended", "completed", "failed", "cancelled"]}


def _object(required: list[str], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": properties,
    }


_STATUS = _object(["title", "category"], title=_STR, category=_CATEGORY)
# A value by its format: money, status and link are objects; times, principals and texts
# strings; numbers numbers; no value null.
_VALUE = {
    "anyOf": [
        {"type": ["string", "number", "boolean", "null"]},
        _object(
            ["amount", "currency"],
            amount={"type": ["string", "number"]},
            currency={"type": ["string", "null"]},
        ),
        _STATUS,
        _object(["href", "title"], href=_STR, title=_STR),
    ]
}
_VALUES = {"type": "object", "additionalProperties": _VALUE}
_ROW = _object(["id", "title", "values"], id=_STR, title=_STR, values=_VALUES)
_CARD = _object(
    ["id", "title", "values"],
    id=_STR,
    title=_STR,
    subtitle=_STR,
    values=_VALUES,
    badge=_STATUS,
)


def _items(item: dict[str, Any]) -> dict[str, Any]:
    return _object(["items"], items={"type": "array", "items": item})


QUERY_CONTRACT: dict[str, dict[str, Any]] = {
    "metrics": _object(["values"], values={"type": "array", "items": _VALUE}),
    "table": _object(
        ["items", "nextCursor"],
        items={"type": "array", "items": _ROW},
        nextCursor={"type": ["string", "null"]},
    ),
    "board": _object(
        ["columns"],
        columns={
            "type": "array",
            "items": _object(
                ["key", "title", "items"],
                key=_STR,
                title=_STR,
                items={"type": "array", "items": _CARD},
            ),
        },
    ),
    "chart": _object(
        ["points"],
        points={"type": "array", "items": _object(["label", "value"], label=_STR, value=_VALUE)},
    ),
    "header": _object(["title", "status"], title=_STR, status=_STATUS, lead=_STR),
    "fields": _object(["values"], values=_VALUES),
    "steps": _items(
        _object(
            ["key", "title"],
            key=_STR,
            title=_STR,
            task=_object(["ref", "title"], ref=_STR, title=_STR),
            assignee=_STR,
            since=_STR,
        )
    ),
    "timeline": _items(_object(["at", "title"], at=_STR, title=_STR, actor=_STR)),
    "artifacts": _items(
        _object(["id", "name", "type", "createdAt"], id=_STR, name=_STR, type=_STR, createdAt=_STR)
    ),
    "related": _items(
        _object(
            ["relation", "kind", "key", "entityTitle", "direction"],
            relation=_STR,
            title=_STR,
            kind=_STR,
            key=_STR,
            entityTitle=_STR,
            direction={"enum": ["in", "out"]},
        )
    ),
}
QUERY_CONTRACT["list"] = QUERY_CONTRACT["table"]


def assert_query_form(block: str, body: dict[str, Any]) -> None:
    """``body`` is the answer of a ``block`` in the agreed form; the findings, if not."""
    errors = sorted(
        Draft202012Validator(QUERY_CONTRACT[block]).iter_errors(body),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, [(list(e.absolute_path), e.message[:200]) for e in errors]


# --- the examples of the agreed form -----------------------------------------------------------

MONEY = {"amount": "184500.00", "currency": "RUB"}
EXAMPLES: dict[str, dict[str, Any]] = {
    "metrics": {"values": [12, MONEY]},
    "table": {
        "items": [
            {
                "id": "0b8f6a0e-6a39-4d1e-9a51-2f9c7c3b8d11",
                "title": "0373100000126000001",
                "values": {
                    "procurement.number": "0373100000126000001",
                    "procurement.maxPrice.amount": MONEY,
                    "procurement.deadlines.submissionEnd": "2026-10-20T09:00:00Z",
                    "manager": "6a1f0b9e-5d4c-4f3b-8a2e-1c0d9e8f7a6b",
                    "stage": {"title": "Подача", "category": "running"},
                    "site": {"href": "https://zakupki.example/1", "title": "zakupki"},
                    "comment": None,
                },
            }
        ],
        "nextCursor": "eyJxIjoiYSJ9",
    },
    "board": {
        "columns": [
            {
                "key": "intake",
                "title": "Приём",
                "items": [
                    {
                        "id": "0b8f6a0e-6a39-4d1e-9a51-2f9c7c3b8d11",
                        "title": "Поставка бумаги",
                        "subtitle": "ГБУ «Школа №1»",
                        "values": {"procurement.maxPrice.amount": MONEY},
                        "badge": {"title": "breached", "category": "running"},
                    }
                ],
            },
            {"key": "submission", "title": "Подача", "items": []},
        ]
    },
    "chart": {"points": [{"label": "44-ФЗ", "value": 7}, {"label": "223-ФЗ", "value": 3}]},
    "header": {
        "title": "Поставка бумаги",
        "status": {"title": "Подача", "category": "suspended"},
        "lead": "6a1f0b9e-5d4c-4f3b-8a2e-1c0d9e8f7a6b",
    },
    "fields": {"values": {"procurement.number": "0373100000126000001", "maxPrice": MONEY}},
    "steps": {
        "items": [
            {
                "key": "review",
                "title": "Проверка документации",
                "task": {"ref": "TASK-000042", "title": "Проверить документацию"},
                "assignee": "6a1f0b9e-5d4c-4f3b-8a2e-1c0d9e8f7a6b",
                "since": "2026-10-03T07:00:00Z",
            },
            {"key": "sign", "title": "Подпись"},
        ]
    },
    "timeline": {
        "items": [
            {"at": "2026-10-03T07:00:00Z", "title": "stage submission entered"},
            {
                "at": "2026-10-02T12:00:00Z",
                "title": "task completed",
                "actor": "6a1f0b9e-5d4c-4f3b-8a2e-1c0d9e8f7a6b",
            },
        ]
    },
    "artifacts": {
        "items": [
            {
                "id": "4f0e1c2d-3b4a-5968-7a8b-9c0d1e2f3a4b",
                "name": "Извещение.pdf",
                "type": "procurement-document",
                "createdAt": "2026-10-01T10:00:00Z",
            }
        ]
    },
    "related": {
        "items": [
            {
                "relation": "customer_of",
                "kind": "legal_entity",
                "key": "7701234567",
                "entityTitle": "ГБУ «Школа №1»",
                "direction": "in",
            }
        ]
    },
}
# What the console sends (the comment of 2026-10-03): a card by its id, a filtered page.
REQUESTS = [
    {"block": 0, "params": {"id": "0b8f6a0e-6a39-4d1e-9a51-2f9c7c3b8d11"}},
    {
        "block": 1,
        "filter": [
            {"field": "procurement.law", "op": "eq", "value": "44-FZ"},
            {"field": "customer.shortName", "op": "prefix", "value": "ГБУ"},
            {"field": "procurement.maxPrice.amount", "op": "gte", "value": 100000},
            {"field": "deadline", "op": "lte", "value": "2026-10-31"},
            {"field": "stage", "op": "in", "value": ["intake", "submission"]},
        ],
        "sort": [{"field": "procurement.deadlines.submissionEnd", "dir": "asc"}],
        "limit": 50,
        "cursor": None,
    },
    # A view of knowledge names the workspace whose tree's knowledge base it reads (stage 6).
    {
        "block": 0,
        "filter": [{"field": "kind", "op": "eq", "value": "legal_entity"}],
        "workspaceId": "5c2e8d14-7b3a-4f6e-9d21-8a0c4e6f2b73",
    },
]


@pytest.mark.parametrize("block", sorted(EXAMPLES))
def test_the_examples_of_the_agreed_form_are_in_it(block: str) -> None:
    assert_query_form(block, EXAMPLES[block])


def test_the_raw_data_is_no_value() -> None:
    raw = {"values": {"procurement": {"number": "1", "maxPrice": {"amount": "1"}}}}
    with pytest.raises(AssertionError):
        assert_query_form("fields", raw)


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def _model(openapi: dict[str, Any], ref: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator({"components": openapi["components"], **ref})


def test_the_route_is_the_path_the_console_opens_with_its_models() -> None:
    openapi = _openapi()
    route = openapi["paths"]["/api/v1/views/{view_key}:query"]["post"]
    assert {p["name"] for p in route["parameters"]} == {"view_key", "locale"}
    body = route["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/ViewQueryRequest"}
    answers = route["responses"]["200"]["content"]["application/json"]["schema"]
    named = {item["$ref"].rsplit("/", 1)[1] for item in answers["anyOf"]}
    assert named == {
        "ViewRowsOut",
        "ViewBoardOut",
        "ViewMetricsOut",
        "ViewChartOut",
        "ViewHeaderOut",
        "ViewFieldsOut",
        "ViewStepsOut",
        "ViewTimelineOut",
        "ViewArtifactsOut",
        "ViewRelatedOut",
    }
    assert {"404", "409", "422", "503"} <= set(route["responses"])
    # Views of tasks and knowledge are answered since stage 6 (CP-ADR-0080, amendment Б).
    assert "501" not in route["responses"]


def test_the_models_of_the_route_accept_the_examples_and_the_requests() -> None:
    openapi = _openapi()
    route = openapi["paths"]["/api/v1/views/{view_key}:query"]["post"]
    answer = _model(openapi, route["responses"]["200"]["content"]["application/json"]["schema"])
    for block, body in EXAMPLES.items():
        assert list(answer.iter_errors(body)) == [], block
    request = _model(openapi, {"$ref": "#/components/schemas/ViewQueryRequest"})
    for body in REQUESTS:
        assert list(request.iter_errors(body)) == []
    # An operator outside the closed list, an expression instead of a value: refused.
    assert list(request.iter_errors({"block": 0, "filter": [{"field": "a", "op": "like"}]}))
    assert list(request.iter_errors({"block": 0, "expression": "data.secret != ''"}))
    assert list(request.iter_errors({"block": 0, "workspaceId": 7}))
