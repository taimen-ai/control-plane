"""``GET /event-types`` against the published catalog (CP-ADR-0068, amendment of 2026-10-04).

The body is ``docs/events/catalog.json`` type by type, plus ``group``,
``labelKey`` and ``supportedVersions``; the captions follow the locale chain
of views; the ETag is the hash of the body.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from control_plane.api.etag import none_match
from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import EventTypeListOut
from control_plane.application.queries.event_types import (
    CORE_LOCALES,
    DEFAULT_LOCALE,
    event_types_view,
    group_of,
)
from control_plane.domain.views import resolve_locale

CATALOG = Path(__file__).resolve().parents[2] / "docs" / "events" / "catalog.json"
ADDED = ("type", "group", "labelKey", "supportedVersions")


def _published() -> dict[str, Any]:
    types: dict[str, Any] = json.loads(CATALOG.read_text("utf-8"))["types"]
    return types


def _body() -> dict[str, Any]:
    return event_types_view(DEFAULT_LOCALE).body


def test_the_answer_is_the_published_catalog() -> None:
    published = _published()
    items = _body()["items"]
    assert [item["type"] for item in items] == sorted(published)
    for item in items:
        assert {k: v for k, v in item.items() if k not in ADDED} == published[item["type"]]


def test_group_label_key_and_versions_of_each_type() -> None:
    for item in _body()["items"]:
        name = item["type"]
        assert item["group"] == name.split(".", 1)[0]
        assert name.startswith(item["group"] + ".")
        assert item["labelKey"] == f"event.{name}"
        assert item["supportedVersions"] == sorted(int(v) for v in item["versions"])
        assert item["currentVersion"] == item["supportedVersions"][-1]
        assert "changes" not in item["versions"]["1"]


def test_group_of_a_type_without_a_dot_is_the_type() -> None:
    assert group_of("approval.requested") == "approval"
    assert group_of("task.completion_work_executed") == "task"
    assert group_of("bare") == "bare"


def test_the_openapi_model_takes_the_body() -> None:
    model = EventTypeListOut.model_validate(_body())
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == _body()


def test_the_route_is_in_openapi_with_its_model_and_parameters() -> None:
    app = FastAPI()
    app.include_router(api_v1_router)
    operation = app.openapi()["paths"]["/api/v1/event-types"]["get"]
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/EventTypeListOut")
    assert "304" in operation["responses"]
    names = {p["name"] for p in operation["parameters"]}
    assert names == {"locale", "If-None-Match"}


# --- locale ------------------------------------------------------------------


def test_the_core_speaks_english_only() -> None:
    assert CORE_LOCALES == ("en",)


@pytest.mark.parametrize("wanted", [None, "", "en", "EN", "en-US", "ru", "ru-RU", "pt-BR", "x"])
def test_every_locale_falls_back_to_english(wanted: str | None) -> None:
    assert resolve_locale(CORE_LOCALES, DEFAULT_LOCALE, wanted) == "en"


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("pt-BR", "pt-BR"),
        ("pt-br", "pt-BR"),
        ("pt-PT", "pt"),
        ("RU", "ru"),
        ("ru-RU", "ru"),
        ("de", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_the_chain_of_views(wanted: str | None, expected: str) -> None:
    assert resolve_locale(["en", "ru", "pt", "pt-BR"], "en", wanted) == expected


def test_captions_in_english_are_the_catalog_descriptions() -> None:
    published = _published()
    body = _body()
    assert body["locale"] == "en"
    for item in body["items"]:
        assert item["description"] == published[item["type"]]["description"]


# --- ETag --------------------------------------------------------------------


def test_the_etag_is_stable_and_the_hash_of_the_body() -> None:
    first = event_types_view(DEFAULT_LOCALE)
    assert first is event_types_view(DEFAULT_LOCALE)
    assert first.etag.startswith('"event-types-') and first.etag.endswith('"')
    event_types_view.cache_clear()
    rebuilt = event_types_view(DEFAULT_LOCALE)
    assert rebuilt is not first
    assert rebuilt.etag == first.etag
    assert rebuilt.body == first.body


@pytest.mark.parametrize(
    ("header", "matches"),
    [
        (None, False),
        ("", False),
        ('"event-types-abc"', True),
        ("event-types-abc", True),
        ('W/"event-types-abc"', True),
        ('"other", "event-types-abc"', True),
        ('"other"', False),
        ('"event-types-ab"', False),
        ("*", False),
    ],
)
def test_if_none_match(header: str | None, matches: bool) -> None:
    assert none_match(header, '"event-types-abc"') is matches
