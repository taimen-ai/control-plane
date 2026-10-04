"""The resource ``Connection`` in the contract (CP-ADR-0079 §3, §12, §13, §14, §17).

What is pinned here: the routes and bodies of ``/connections`` in OpenAPI, the
rights in the enum and ``authz/catalog.yaml``, the payloads of the connection
events (keys, statuses and codes only), and the redaction of §14: the keys the
JSON logger hides, ``refresh_token`` in the durable-payload guard and the
query string of the OAuth callback in the access log.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError
from uvicorn.protocols.utils import get_path_with_query_string

from control_plane.api.v1.connections import ETAG_ENTITY
from control_plane.api.v1.schemas import ConnectionStatusReport, ConnectionUpdateRequest
from control_plane.domain.enums import ConnectionAuth, ConnectionStatus, Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.event_catalog import get_event_type
from control_plane.domain.redaction import (
    LOG_SENSITIVE_KEYS,
    SENSITIVE_KEY_PATTERN,
    reject_unsafe_durable_payload,
)
from control_plane.logging import AccessLogQueryFilter, JsonFormatter, redact, redact_query
from tests.unit.test_connection_type_contract import PATHS, SCHEMAS, _body, _ref, _response

ROOT = Path(__file__).resolve().parents[2]


def test_the_routes_of_the_resource() -> None:
    collection = PATHS["/api/v1/connections"]
    card = PATHS["/api/v1/connections/{key}"]
    status = PATHS["/api/v1/connections/{key}/status"]
    assert set(collection) == {"post", "get"}
    assert set(card) == {"get", "patch"}
    assert set(status) == {"put"}

    create = collection["post"]
    assert _body(create) == "ConnectionCreateRequest"
    assert _ref(_response(create, "201")) == "ConnectionOut"
    assert {"409", "422"} <= set(create["responses"])

    listing = {p["name"] for p in collection["get"]["parameters"]}
    assert listing == {"type", "status", "limit", "cursor"}

    assert _ref(_response(card["get"], "200")) == "ConnectionOut"
    patch = card["patch"]
    assert _body(patch) == "ConnectionUpdateRequest"
    assert "if-match" in {p["name"].lower() for p in patch["parameters"]}
    assert _ref(_response(patch, "200")) == "ConnectionOut"

    report = status["put"]
    assert _body(report) == "ConnectionStatusReport"
    assert _ref(_response(report, "200")) == "ConnectionOut"
    assert {"403", "409"} <= set(report["responses"])


def test_the_bodies_of_the_resource() -> None:
    create = SCHEMAS["ConnectionCreateRequest"]
    assert create["required"] == ["type"]
    assert set(create["properties"]) == {"key", "type", "displayName", "settings"}

    update = SCHEMAS["ConnectionUpdateRequest"]
    assert update["minProperties"] == 1
    assert set(update["properties"]) == {"displayName", "settings", "typeVersion"}
    assert "required" not in update

    report = SCHEMAS["ConnectionStatusReport"]
    assert set(report["required"]) == {"status", "checkedAt"}
    assert report["properties"]["status"]["enum"] == ["active", "expired"]

    out = SCHEMAS["ConnectionOut"]
    assert set(out["properties"]) == {
        "id",
        "key",
        "type",
        "typeVersion",
        "displayName",
        "account",
        "auth",
        "status",
        "statusReason",
        "statusMessage",
        "settings",
        "secretRef",
        "expiresAt",
        "connectedBy",
        "connectedAt",
        "lastCheckedAt",
        "agents",
        "createdBy",
        "createdAt",
        "updatedAt",
        "version",
    }
    status_enum = out["properties"]["status"]["enum"]
    assert status_enum == [s.value for s in ConnectionStatus]
    assert {s.value for s in ConnectionAuth} == {"oauth2", "token"}


def test_the_etag_of_a_connection() -> None:
    assert ETAG_ENTITY == "connection"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"displayName": None},
        {"settings": None},
        {"typeVersion": None},
        {"displayName": "CRM", "typeVersion": None},
        {"displayName": ""},
        {"typeVersion": 0},
        {"typeVersion": "two"},
        {"settings": []},
        {"account": "x"},
    ],
)
def test_an_update_names_at_least_one_field_and_no_null(body: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        ConnectionUpdateRequest.model_validate_json(json.dumps(body))


def test_an_update_hands_over_only_what_was_sent() -> None:
    update = ConnectionUpdateRequest.model_validate({"settings": {}, "typeVersion": 2})
    assert update.changes() == {"settings": {}, "typeVersion": 2}


@pytest.mark.parametrize(
    "body",
    [
        {"status": "revoked", "checkedAt": "2026-09-30T10:00:00Z"},
        {"status": "pending", "checkedAt": "2026-09-30T10:00:00Z"},
        {"status": "expired"},
        {"status": "expired", "checkedAt": "2026-09-30T10:00:00"},
        {"status": "expired", "reason": "Refresh rejected", "checkedAt": "2026-09-30T10:00:00Z"},
        {"status": "expired", "message": "x" * 501, "checkedAt": "2026-09-30T10:00:00Z"},
    ],
)
def test_a_status_report_is_narrow(body: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        ConnectionStatusReport.model_validate(body)


def test_the_rights_are_in_the_enum_and_the_catalog() -> None:
    assert Permission.CONNECTIONS_STATUS_WRITE.value == "connections.status.write"
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    for name in ("connections.read", "connections.manage", "connections.status.write"):
        assert catalog["actions"][name] == {"resource": "tenant"}


@pytest.mark.parametrize(
    ("event_type", "fields"),
    [
        ("connection.created", {"key", "type", "typeVersion", "status"}),
        ("connection.updated", {"key", "version", "changes"}),
        ("connection.status_changed", {"key", "type", "from", "to", "reason", "connectedBy"}),
    ],
)
def test_the_events_carry_keys_statuses_and_codes_only(event_type: str, fields: set[str]) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == "connection"
    assert entry.current.version == 1
    schema = entry.current.schema
    assert set(schema["properties"]) == fields
    assert set(schema["required"]) == fields


# --- redaction (§14) --------------------------------------------------------------


def test_the_logger_hides_oauth_material_by_key() -> None:
    assert {"code", "access_token", "refresh_token", "client_secret"} <= LOG_SENSITIVE_KEYS
    extras = {
        "code": "abc",
        "Access_Token": "t",
        "nested": {"refresh_token": "r", "client_secret": "s", "error_code": "not_found"},
        "reasonCode": "refresh_rejected",
    }
    assert redact(extras) == {
        "code": "[REDACTED]",
        "Access_Token": "[REDACTED]",
        "nested": {
            "refresh_token": "[REDACTED]",
            "client_secret": "[REDACTED]",
            "error_code": "not_found",
        },
        "reasonCode": "refresh_rejected",
    }


def test_an_error_code_in_log_extras_stays_readable() -> None:
    record = logging.LogRecord("x", logging.WARNING, "f", 1, "refused", (), None)
    record.error_code = "invalid_transition"
    record.code = "oauth-code-value"
    line = json.loads(JsonFormatter().format(record))
    assert line["error_code"] == "invalid_transition"
    assert line["code"] == "[REDACTED]"


def test_the_durable_payload_guard_refuses_a_refresh_token() -> None:
    for key in ("refresh_token", "refreshToken", "refresh-token"):
        assert SENSITIVE_KEY_PATTERN.search(key)
        with pytest.raises(ValidationError):
            reject_unsafe_durable_payload({key: "x"}, code="unsafe", subject="payload")
    # ``code`` is not a durable-payload key: reason codes stay allowed.
    reject_unsafe_durable_payload(
        {"code": "x", "reasonCode": "y"}, code="unsafe", subject="payload"
    )


@pytest.mark.parametrize(
    ("path", "query", "logged"),
    [
        (
            "/api/v1/connections:callback",
            b"state=s3cr3t&code=c0de",
            "/api/v1/connections%3Acallback?[redacted]",
        ),
        ("/api/v1/connections:callback", b"", "/api/v1/connections%3Acallback"),
        ("/api/v1/connections", b"type=crm", "/api/v1/connections?type=crm"),
        (
            "/api/v1/connections:callbackx",
            b"code=1",
            "/api/v1/connections%3Acallbackx?code=1",
        ),
    ],
)
def test_the_access_log_loses_the_query_of_the_callback(
    path: str, query: bytes, logged: str
) -> None:
    # The path exactly as uvicorn puts it into the access record: quoted, so
    # the colon of the custom method arrives as ``%3A``.
    access_path = get_path_with_query_string(
        {"type": "http", "path": path, "root_path": "", "query_string": query}  # type: ignore[typeddict-item]
    )
    assert redact_query(access_path) == logged
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "f",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", access_path, "1.1", 303),
        None,
    )
    assert AccessLogQueryFilter().filter(record) is True
    assert record.getMessage() == f'127.0.0.1:5000 - "GET {logged} HTTP/1.1" 303'
    assert "c0de" not in record.getMessage()
    assert "s3cr3t" not in record.getMessage()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/connections:callback?state=s3cr3t&code=c0de",
        "/api/v1/connections%3Acallback?state=s3cr3t&code=c0de",
        "/api/v1/connections%3acallback?state=s3cr3t&code=c0de",
    ],
)
def test_the_callback_query_is_redacted_whatever_the_colon_encoding(path: str) -> None:
    redacted = redact_query(path)
    assert redacted.endswith("?[redacted]")
    assert "c0de" not in redacted
    assert "s3cr3t" not in redacted


def test_the_access_filter_leaves_other_records_alone() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, "plain", None, None)
    assert AccessLogQueryFilter().filter(record) is True
    assert record.getMessage() == "plain"
