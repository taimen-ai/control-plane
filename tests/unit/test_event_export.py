"""``GET /events:export`` without a database (CP-ADR-0068, export amendment).

The line formats, the period limit, the right in the enum and the policy
catalog, the audit event in the event catalog and the route in OpenAPI.
"""

import csv
import io
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router
from control_plane.application.queries.event_export import (
    CSV_COLUMNS,
    check_export_period,
    csv_cell,
    csv_header,
    csv_line,
    jsonl_line,
)
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.event_catalog import get_event_type

ROOT = Path(__file__).resolve().parents[2]

EVENT: dict[str, Any] = {
    "id": "0b6f2c1e-8d5f-4e0e-9a3f-1c2d3e4f5a6b",
    "occurredAt": "2026-07-01T10:00:00Z",
    "type": "task.created",
    "schemaVersion": 2,
    "actorId": None,
    "entityType": "task",
    "entityId": "1b6f2c1e-8d5f-4e0e-9a3f-1c2d3e4f5a6b",
    "workspaceId": None,
    "payload": {"title": 'a, "quoted"\nline — ünïcode', "n": 1, "none": None},
    "sequence": 7,
    "cursor": "ec1_x",
}


def _parsed(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def test_csv_header_names_the_flat_columns() -> None:
    assert _parsed(csv_header()) == [[c for c, _ in CSV_COLUMNS]]
    assert [c for c, _ in CSV_COLUMNS] == [
        "id",
        "occurredAt",
        "type",
        "schemaVersion",
        "actorId",
        "entityType",
        "entityId",
        "workspaceId",
        "payload",
    ]


def test_csv_line_is_one_record_with_payload_as_json() -> None:
    line = csv_line(EVENT)
    assert line.endswith("\r\n")
    [row] = _parsed(line)
    assert row[:8] == [
        EVENT["id"],
        "2026-07-01T10:00:00Z",
        "task.created",
        "2",
        "",
        "task",
        EVENT["entityId"],
        "",
    ]
    # Separators, quotes and a line break in the payload stay inside the field.
    assert json.loads(row[8]) == EVENT["payload"]
    assert row[8].startswith("{")


def test_csv_line_of_an_empty_payload() -> None:
    [row] = _parsed(csv_line({**EVENT, "payload": {}}))
    assert row[8] == "{}"


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_a_cell_read_as_a_formula_gets_an_apostrophe(text: str) -> None:
    assert csv_cell(text) == "'" + text


@pytest.mark.parametrize("text", ["", "{}", "task.created", "a=b", "1-2", "'=x"])
def test_a_plain_cell_stays_as_is(text: str) -> None:
    assert csv_cell(text) == text


def test_a_payload_starting_with_a_formula_sign_is_guarded() -> None:
    # A non-object payload serialises without the leading brace.
    [row] = _parsed(csv_line({**EVENT, "payload": -1}))
    assert row[8] == "'-1"
    [row] = _parsed(csv_line({**EVENT, "payload": "=HYPERLINK(1)"}))
    assert row[8] == '"=HYPERLINK(1)"'  # a JSON string starts with a quote


def test_a_payload_object_with_a_formula_inside_stays_json() -> None:
    payload = {"title": '=HYPERLINK("http://x")'}
    [row] = _parsed(csv_line({**EVENT, "payload": payload}))
    assert json.loads(row[8]) == payload


def test_every_scalar_column_is_guarded() -> None:
    body = {key: "=cmd" for _, key in CSV_COLUMNS if key != "payload"}
    [row] = _parsed(csv_line({**body, "payload": {}}))
    assert row[:8] == ["'=cmd"] * 8
    assert row[8] == "{}"


def test_jsonl_line_is_the_body_on_one_line() -> None:
    line = jsonl_line(EVENT)
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == EVENT
    assert "ü" in line  # UTF-8 as is, not escaped


START = datetime(2026, 7, 1, tzinfo=UTC)


def test_period_bounds_are_both_required() -> None:
    for occurred_from, occurred_to, missing in (
        (None, None, ["occurredFrom", "occurredTo"]),
        (START, None, ["occurredTo"]),
        (None, START, ["occurredFrom"]),
    ):
        with pytest.raises(ValidationError) as caught:
            check_export_period(occurred_from, occurred_to, 92)
        assert caught.value.code == "export_period_required"
        assert caught.value.details == {"missing": missing, "maxPeriodDays": 92}


def test_period_up_to_the_limit_passes_and_beyond_it_is_refused() -> None:
    assert check_export_period(START, START, 92) == (START, START)
    end = START + timedelta(days=92)
    assert check_export_period(START, end, 92) == (START, end)
    # The length is measured in time, not in the zone's calendar.
    shifted = end.astimezone(timezone(timedelta(hours=3)))
    assert check_export_period(START, shifted, 92) == (START, shifted)
    with pytest.raises(ValidationError) as caught:
        check_export_period(START, end + timedelta(microseconds=1), 92)
    assert caught.value.code == "export_period_too_long"
    assert caught.value.details["maxPeriodDays"] == 92


def test_the_limits_default_to_a_quarter_and_a_hundred_thousand() -> None:
    fields = Settings.model_fields
    assert fields["events_export_max_period_days"].default == 92
    assert fields["events_export_max_events"].default == 100_000


def test_the_right_is_in_the_enum_and_the_catalog() -> None:
    assert Permission.EVENTS_EXPORT.value == "events.export"
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    assert catalog["actions"]["events.export"] == {"resource": "workspace"}
    assert catalog["actions"]["events.read"] == catalog["actions"]["events.export"]


def test_the_audit_event_carries_filters_not_data() -> None:
    entry = get_event_type("event_journal.exported")
    assert entry.entity_type == "event_journal"
    properties = set(entry.current.schema["properties"])
    assert properties == {
        "format",
        "types",
        "entityType",
        "entityId",
        "actorId",
        "occurredFrom",
        "occurredTo",
        "workspaceId",
        "includeDescendants",
        "events",
        "throughCursor",
    }


def test_the_route_is_in_openapi() -> None:
    app = FastAPI()
    app.include_router(api_v1_router)
    operation = app.openapi()["paths"]["/api/v1/events:export"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert parameters["format"]["required"] is True
    assert parameters["format"]["schema"]["enum"] == ["jsonl", "csv"]
    assert {
        "types",
        "entityType",
        "entityId",
        "actorId",
        "occurredFrom",
        "occurredTo",
        "workspaceId",
        "includeDescendants",
    } <= set(parameters)
    content = operation["responses"]["200"]["content"]
    assert {"application/x-ndjson", "text/csv"} <= set(content)
    assert "422" in operation["responses"]
