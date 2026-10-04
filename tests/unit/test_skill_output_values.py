"""A skill output value checked against an artifact type (CP-ADR-0072, amendment 2026-10-01)."""

from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.application.commands.skill_outputs import encode_value, is_execution_call
from control_plane.domain.artifact_type import ArtifactTypeDefinition, check_output_value
from control_plane.domain.errors import ValidationError

JSON = "application/json"


def definition(schema: dict[str, Any] | None = None, **extra: Any) -> ArtifactTypeDefinition:
    return ArtifactTypeDefinition(
        metadata_schema=schema or {},
        media_types=extra.get("media_types", ["application/*"]),
        max_bytes=extra.get("max_bytes", 1024),
    )


def check(defn: ArtifactTypeDefinition, value: Any, **extra: Any) -> None:
    check_output_value(
        defn,
        key="t",
        version=2,
        value=value,
        media_type=JSON,
        size_bytes=extra.pop("size_bytes", len(encode_value(value))),
        **extra,
    )


@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], "text", 0, False])
def test_an_empty_schema_takes_any_json_value(value: Any) -> None:
    check(definition(), value)


def test_a_value_is_checked_whatever_its_json_type() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    check(definition(schema), [1, 2])
    with pytest.raises(ValidationError) as caught:
        check(definition(schema), {"a": 1})
    assert caught.value.code == "invalid_output_value"
    assert caught.value.details["artifactTypeVersion"] == 2
    assert caught.value.details["errors"][0]["path"] == "/"


def test_formats_stay_annotations_as_for_metadata() -> None:
    check(definition({"type": "string", "format": "uuid"}), "not-a-uuid")


def test_an_unresolvable_schema_fails_the_value_not_the_request() -> None:
    with pytest.raises(ValidationError) as caught:
        check(definition({"$ref": "#/$defs/missing"}), {"a": 1})
    assert caught.value.code == "invalid_output_value"
    assert "could not be evaluated" in caught.value.details["errors"][0]["message"]


def test_media_types_of_the_type_then_of_the_output() -> None:
    with pytest.raises(ValidationError) as caught:
        check(definition(media_types=["text/*"]), {"a": 1})
    assert caught.value.code == "media_type_not_allowed"
    check(definition(), {"a": 1}, narrowed_to=("application/json",))
    with pytest.raises(ValidationError) as caught:
        check(definition(), {"a": 1}, narrowed_to=("application/pdf",))
    assert caught.value.details["allowed"] == ["application/pdf"]


def test_the_size_ceiling_of_the_type() -> None:
    check(definition(max_bytes=7), {"a": 1})  # {"a":1}
    with pytest.raises(ValidationError) as caught:
        check(definition(max_bytes=6), {"a": 1})
    assert caught.value.code == "artifact_too_large"


def test_the_encoding_is_stable_utf8_json() -> None:
    assert encode_value({"b": "ё", "a": [1]}) == '{"a":[1],"b":"ё"}'.encode()


@pytest.mark.parametrize(
    ("basis", "task_id", "expected"),
    [
        ({"kind": "execution"}, "t", True),
        ({"kind": "execution"}, None, False),
        ({"kind": "approval"}, "t", False),
        (None, "t", False),
    ],
)
def test_only_the_execution_call_hands_in_outputs(
    basis: dict[str, Any] | None, task_id: str | None, expected: bool
) -> None:
    invocation: Any = SimpleNamespace(authorization_basis=basis, task_id=task_id)
    assert is_execution_call(invocation) is expected
