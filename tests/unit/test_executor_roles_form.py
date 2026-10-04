"""The form of ``executorRoles`` a package sends (CP-ADR-0048, amendment 2026-10-03 A1)."""

from typing import Any

import pytest

from control_plane.application.commands.package_catalog import wanted_form
from control_plane.application.commands.role_references import (
    MAX_EXECUTOR_ROLES,
    normalize_executor_roles,
)
from control_plane.domain.errors import ValidationError


@pytest.mark.parametrize(
    ("roles", "field"),
    [
        (None, "executorRoles"),
        ("developer", "executorRoles"),
        ({"developer": True}, "executorRoles"),
        (["developer", 5], "executorRoles[1]"),
        (["developer", None], "executorRoles[1]"),
        ([["developer"]], "executorRoles[0]"),
        (["developer", "developer"], "executorRoles[1]"),
        ([""], "executorRoles[0]"),
        (["d"], "executorRoles[0]"),
        (["Developer"], "executorRoles[0]"),
        (["role:developer"], "executorRoles[0]"),
        (["x" * 64], "executorRoles[0]"),
        ([f"role-{i}" for i in range(MAX_EXECUTOR_ROLES + 1)], "executorRoles"),
    ],
)
def test_a_malformed_list_is_refused_with_the_path(roles: Any, field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        wanted_form("TaskType", {"executorRoles": roles}, None)
    assert caught.value.code == "invalid_executor_roles"
    assert caught.value.details["field"] == field


def test_a_well_formed_list_is_kept_in_its_order() -> None:
    assert wanted_form("TaskType", {"executorRoles": ["b-role", "a-role"]}, None)[
        "executorRoles"
    ] == ["b-role", "a-role"]
    assert wanted_form("TaskType", {"executorRoles": []}, None)["executorRoles"] == []
    assert wanted_form("TaskType", {}, None)["executorRoles"] == []


def test_normalizing_does_not_share_the_list() -> None:
    sent = ["developer"]
    kept = normalize_executor_roles(sent)
    sent.append("reviewer")
    assert kept == ["developer"]
