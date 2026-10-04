"""``role:<slug>`` has one parser: publication and the opening of a gate agree.

The acceptance check's pattern (``work_graph``) and ``role_slug`` of the gate
read the reference through ``role_reference_slug``; nothing around the slug is
trimmed by one and refused by the other (CP-ADR-0061, amendment 2026-10-01).
"""

from typing import Any

import pytest

from control_plane.application.commands.role_references import role_slug
from control_plane.domain.errors import ValidationError
from control_plane.domain.work_graph import role_reference_slug

MALFORMED = [
    "role: x",
    "role:x ",
    " role:x",
    "role:x\n",
    "role:\tx",
    "role:",
    "role:X",
    "role:-x",
    "role:a b",
    "role:a_b",
    "role:" + "a" * 64,
    "x",
    "",
    "ROLE:x",
]


@pytest.mark.parametrize("reference", ["role:x", "role:purchase-approver", "role:" + "a" * 63])
def test_a_well_formed_reference_names_its_slug(reference: str) -> None:
    slug = reference.removeprefix("role:")
    assert role_reference_slug(reference) == slug
    assert role_slug(reference, field="assignee") == slug


@pytest.mark.parametrize("reference", MALFORMED)
def test_a_malformed_reference_names_no_role_at_either_moment(reference: str) -> None:
    assert role_reference_slug(reference) is None
    with pytest.raises(ValidationError) as caught:
        role_slug(reference, field="assignee")
    assert caught.value.code == "unknown_role"
    assert caught.value.details == {
        "field": "assignee",
        "role": reference.removeprefix("role:")[:100],
    }


@pytest.mark.parametrize("value", [None, 5, ["role:x"], {"role": "x"}, b"role:x"])
def test_not_a_string_is_not_a_reference(value: Any) -> None:
    assert role_reference_slug(value) is None
