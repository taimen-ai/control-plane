"""The event catalog as a contract (CP-ADR-0068).

The integration package checks every journal event against the catalog after
each test (tests/event_contract.py); these tests pin the catalog itself: its
schemas are valid, versions only add fields, every type the core names in its
source is registered, and the published docs match the code.
"""

import ast
import json
from pathlib import Path

import jsonschema
import pytest

from control_plane.application.queries.events import parse_type_prefixes
from control_plane.domain.errors import ValidationError
from control_plane.domain.event_catalog import (
    PAYLOAD_TEXT_LIMIT,
    UnknownEventType,
    catalog_document,
    current_version,
    event_types,
    get_event_type,
    render_markdown,
)
from control_plane.domain.redaction import redact_secret_material

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "control_plane"


def test_every_schema_is_valid_json_schema() -> None:
    for entry in event_types():
        assert entry.versions, entry.type
        for version in entry.versions:
            jsonschema.Draft202012Validator.check_schema(version.schema)


def test_versions_only_add_fields() -> None:
    for entry in event_types():
        for older, newer in zip(entry.versions, entry.versions[1:], strict=False):
            assert newer.version == older.version + 1
            old_props, new_props = older.schema["properties"], newer.schema["properties"]
            assert set(old_props) <= set(new_props), entry.type
            for name, schema in old_props.items():
                assert new_props[name] == schema, f"{entry.type}.{name} changed its schema"
            assert set(older.schema.get("required", ())) <= set(newer.schema.get("required", ()))
            assert newer.changes, f"{entry.type} v{newer.version} must say what it added"


def test_approval_events_are_at_their_current_versions() -> None:
    for name in ("approval.approved", "approval.rejected", "approval.cancelled"):
        assert current_version(name) == 2
    # v3 adds the principals whose decision the core refuses (CP-ADR-0074 §7).
    assert current_version("approval.requested") == 3
    requested = get_event_type("approval.requested").current.schema
    for field in (
        "workspaceId",
        "taskPublicId",
        "taskTitle",
        "requestedBy",
        "comment",
        "gate",
        "excludedPrincipals",
    ):
        assert field in requested["required"]
    decided = get_event_type("approval.approved").current.schema
    for field in ("decisionBy", "comment", "channel"):
        assert field in decided["required"]
    assert "cancelledBy" in get_event_type("approval.cancelled").current.schema["required"]


def test_unknown_type_is_refused() -> None:
    with pytest.raises(UnknownEventType):
        current_version("nothing.happened")


def _record_event_types(tree: ast.AST) -> set[str]:
    """String constants passed as ``event_type=`` anywhere in the module, plus
    the conditional branches of such expressions."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "event_type":
                continue
            values = [keyword.value]
            while values:
                value = values.pop()
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.IfExp):
                    values += [value.body, value.orelse]
    return found


def test_every_literal_type_in_the_core_is_in_the_catalog() -> None:
    """Static half of the coverage check; the dynamic half is ``record_event``
    itself, which refuses a type the catalog does not know."""
    known = {entry.type for entry in event_types()}
    literal: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        literal |= _record_event_types(ast.parse(path.read_text(encoding="utf-8")))
    assert literal, "the scan found no event types: is record_event still called so?"
    assert literal - known == set()


def test_catalog_is_neutral() -> None:
    """Constitution art. II: the catalog names the core's own entities only."""
    entities = {entry.entity_type for entry in event_types()}
    assert entities <= {
        "agent",
        "api_key",
        "approval",
        "artifact",
        "artifact_type",
        "attention_feedback",
        "calendar",
        "capability",
        "claim",
        "connection",
        "connection_type",
        "delegation",
        "event_consumer",
        "event_journal",
        "goal",
        "iam_binding",
        "knowledge_pack",
        "observation",
        "package",
        "principal",
        "process_definition",
        "process_instance",
        "project",
        "project_template",
        "role",
        "rule",
        "run",
        "session",
        "skill",
        "skill_invocation",
        "task",
        "task_type",
        "tenant",
        "view",
        "workspace",
        "workspace_type",
    }


def test_published_catalog_matches_the_code() -> None:
    docs = ROOT / "docs" / "events"
    assert (docs / "catalog.md").read_text(encoding="utf-8") == render_markdown(), (
        "docs/events/catalog.md is stale: run `make event-catalog`"
    )
    assert json.loads((docs / "catalog.json").read_text(encoding="utf-8")) == json.loads(
        json.dumps(catalog_document())
    ), "docs/events/catalog.json is stale: run `make event-catalog`"


# --- helpers used by the readers and the approval events ---------------------


def test_redaction_replaces_material_and_keeps_prose() -> None:
    text = "rotate the API key; token=" + "A" * 32 + " and ghp_" + "b" * 30
    redacted = redact_secret_material(text)
    assert redacted == "rotate the API key; [redacted] and [redacted]"


def test_event_comment_is_redacted_and_bounded() -> None:
    from control_plane.application.commands.approvals import event_comment

    assert event_comment(None) is None
    assert event_comment("fine") == "fine"
    long = event_comment("x" * (PAYLOAD_TEXT_LIMIT + 50))
    assert long is not None
    assert len(long) == PAYLOAD_TEXT_LIMIT
    assert long.endswith("…")


def test_type_prefixes_parse() -> None:
    assert parse_type_prefixes(None) == ()
    assert parse_type_prefixes(["approval., task.verified", "approval."]) == (
        "approval.",
        "task.verified",
    )
    for bad in (["Approval."], ["approval.*"], ["a b"], [".x"]):
        with pytest.raises(ValidationError):
            parse_type_prefixes(bad)
    with pytest.raises(ValidationError):
        parse_type_prefixes([",".join(f"t{n}." for n in range(21))])
