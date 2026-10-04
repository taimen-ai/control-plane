"""The form of field errors of connections (CP-ADR-0079, the amendment of 2026-10-03).

``secret_findings`` is the one search behind ``secret_material_rejected`` with
``details.errors`` (connections, connection types, package settings of
CP-ADR-0081 4.3); ``settings_errors`` is ``invalid_connection_settings``. Both
answer JSON Pointers and never the value.
"""

import copy
import json
from typing import Any

import pytest

from control_plane.domain.connection_type import (
    MAX_SETTINGS_ERRORS,
    secret_refusal,
    settings_errors,
    spec_secret_findings,
)
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import secret_findings

TOKEN = "ghp_" + "a1B2" * 9
BEARER = "Bearer " + "x" * 24


@pytest.mark.parametrize("value", [{}, [], None, 1, 1.5, True, "", "plain text", {"a": None}])
def test_nothing_to_find_is_an_empty_list(value: Any) -> None:
    assert secret_findings(value) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TOKEN, [{"path": "/", "match": "provider_token"}]),
        ({"note": TOKEN}, [{"path": "/note", "match": "provider_token"}]),
        (
            {"stages": [{"label": BEARER}]},
            [{"path": "/stages/0/label", "match": "bearer_token"}],
        ),
        # A name is reported by the object it stands in: ``/`` is the root.
        ({TOKEN: 1}, [{"path": "/", "match": "provider_token"}]),
        ({"x": {TOKEN: 1}}, [{"path": "/x", "match": "provider_token"}]),
        ({"password": "x"}, [{"path": "/", "match": "secret_name"}]),
        ({"nested": {"apiKey": "x"}}, [{"path": "/nested", "match": "secret_name"}]),
        # RFC 6901: ``~`` and ``/`` in a name are escaped.
        ({"a/b": {"c~d": TOKEN}}, [{"path": "/a~1b/c~0d", "match": "provider_token"}]),
    ],
)
def test_a_finding_is_a_json_pointer_and_a_kind(value: Any, expected: list[Any]) -> None:
    found = secret_findings(value)
    assert found == expected
    assert TOKEN not in json.dumps(found)
    assert "x" * 24 not in json.dumps(found)


def test_every_finding_is_reported_and_nothing_under_a_refused_name() -> None:
    value = {"a": TOKEN, "password": {"b": TOKEN}, "c": [1, BEARER], TOKEN: {"d": TOKEN}}
    assert secret_findings(value) == [
        {"path": "/a", "match": "provider_token"},
        {"path": "/", "match": "secret_name"},
        {"path": "/c/1", "match": "bearer_token"},
        {"path": "/", "match": "provider_token"},
    ]


def test_secret_ref_and_words_about_secrets_pass() -> None:
    assert secret_findings({"secretRef": "vault://x", "note": "the token rotates"}) == []


def test_names_like_a_secret_can_be_left_to_the_caller() -> None:
    assert secret_findings({"tokenUrlTemplate": "https://x.example"}, names=False) == []
    # Material in a name is refused either way.
    assert secret_findings({TOKEN: 1}, names=False) == [{"path": "/", "match": "provider_token"}]


def test_the_search_does_not_change_its_input_and_repeats() -> None:
    value = {"a": [TOKEN, {"password": 1}]}
    before = copy.deepcopy(value)
    assert secret_findings(value) == secret_findings(value)
    assert value == before


def test_the_refusal_carries_the_findings_and_names_the_body_member() -> None:
    error = secret_refusal(secret_findings({"note": TOKEN}), field="settings")
    assert isinstance(error, ValidationError)
    assert error.code == "secret_material_rejected"
    assert error.details == {
        "field": "settings",
        "errors": [{"path": "/note", "match": "provider_token"}],
    }
    assert TOKEN not in f"{error.message} {error.details}"


def test_a_spec_checks_names_in_its_settings_schema_only() -> None:
    spec = {
        "oauth2": {"tokenUrlTemplate": "https://x.example/token"},
        "accountField": {"title": "t"},
        "settingsSchema": {"type": "object", "properties": {"password": {"type": "string"}}},
    }
    assert spec_secret_findings(spec) == [
        {"path": "/settingsSchema/properties", "match": "secret_name"}
    ]
    # Material in a name of the schema is found by both passes and reported once.
    spec["settingsSchema"] = {"type": "object", "properties": {TOKEN: {}}}
    assert spec_secret_findings(spec) == [
        {"path": "/settingsSchema/properties", "match": "provider_token"}
    ]
    assert spec_secret_findings({}) == []


SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pipelineId"],
    "properties": {
        "pipelineId": {"type": "integer", "minimum": 1},
        "stage": {"enum": ["won", "lost"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nested": {
            "type": "object",
            "properties": {"a/b": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


def test_settings_by_the_schema_pass() -> None:
    assert settings_errors(SCHEMA, {"pipelineId": 2, "stage": "won", "tags": ["x"]}) == []
    assert settings_errors({"type": "object"}, {}) == []


def test_every_violation_is_a_pointer_a_keyword_and_no_value() -> None:
    secret_word = "quite-unique-value-41"
    errors = settings_errors(
        SCHEMA,
        {
            "stage": secret_word,
            "tags": ["ok", 7],
            "nested": {"a/b": 3, "extra": secret_word},
            "unknown": secret_word,
        },
    )
    extra = "is not a field of the schema"
    assert [(e["path"], e["code"], e["message"]) for e in errors] == [
        ("/nested/a~1b", "type", "must be of type string"),
        ("/nested/extra", "additionalProperties", extra),
        ("/pipelineId", "required", "is required"),
        ("/stage", "enum", "must be one of the values the schema allows"),
        ("/tags/1", "type", "must be of type string"),
        ("/unknown", "additionalProperties", extra),
    ]
    assert all(set(e) == {"path", "code", "message"} for e in errors)
    assert secret_word not in json.dumps(errors)


@pytest.mark.parametrize(
    ("value", "code", "message"),
    [
        (0, "minimum", "does not satisfy minimum 1"),
        ("seven", "type", "must be of type integer"),
        (None, "type", "must be of type integer"),
        (True, "type", "must be of type integer"),
    ],
)
def test_a_scalar_violation(value: Any, code: str, message: str) -> None:
    errors = settings_errors(SCHEMA, {"pipelineId": value})
    assert errors == [{"path": "/pipelineId", "code": code, "message": message}]


def test_a_violation_of_the_root_is_the_root_pointer() -> None:
    schema = {"type": "object", "minProperties": 1}
    assert settings_errors(schema, {}) == [
        {"path": "/", "code": "minProperties", "message": "does not satisfy minProperties 1"}
    ]
    schema = {"type": "object", "not": {"required": ["x"]}}
    assert settings_errors(schema, {"x": 1}) == [
        {"path": "/", "code": "not", "message": "does not satisfy not"}
    ]


def test_the_list_is_bounded() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    settings = {f"f{index:03}": "x" for index in range(MAX_SETTINGS_ERRORS + 10)}
    errors = settings_errors(schema, settings)
    assert len(errors) == MAX_SETTINGS_ERRORS
    assert errors[0]["path"] == "/f000"


def test_a_schema_that_cannot_be_evaluated_is_a_validation_failure() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValidationError) as caught:
        settings_errors(schema, {"x": 1})
    assert caught.value.code == "invalid_json_schema"
