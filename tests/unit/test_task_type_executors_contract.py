"""The contract of executor roles and ``GET /task-types/{id}/executors`` in OpenAPI
(CP-ADR-0048, amendment 2026-10-03: A1, A2)."""

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import TaskTypeCreateRequest
from control_plane.application.queries.task_type_executors import _agent_reason


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]


def test_a_task_type_version_carries_executor_roles() -> None:
    request = SCHEMAS["TaskTypeCreateRequest"]["properties"]["executorRoles"]
    assert request["type"] == "array"
    assert request["maxItems"] == 20
    assert request["items"]["pattern"] == "^[a-z0-9][a-z0-9-]*$"
    assert "executorRoles" not in SCHEMAS["TaskTypeCreateRequest"].get("required", [])
    assert SCHEMAS["TaskTypeOut"]["properties"]["executorRoles"]["type"] == "array"


def test_the_executors_route_is_published() -> None:
    operation = OPENAPI["paths"]["/api/v1/task-types/{type_id}/executors"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["workspaceId"]["required"] is True
    assert params["workspaceId"]["in"] == "query"
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["$ref"].endswith("/TaskTypeExecutorsOut")
    item = SCHEMAS["TaskTypeExecutorOut"]
    assert set(item["properties"]) == {"principalId", "kind", "displayName", "roles", "reason"}
    assert set(item["properties"]["reason"]["enum"]) == {
        "role",
        "any",
        "agent_task_types",
        "agent_any",
    }


@pytest.mark.parametrize(
    "roles",
    [
        "developer",
        [None],
        [1],
        ["developer", "developer"],
        ["Developer"],
        ["d"],
        [f"r{i:02d}" for i in range(21)],
    ],
)
def test_a_malformed_list_is_refused_by_the_model(roles: Any) -> None:
    with pytest.raises(ValidationError):
        TaskTypeCreateRequest.model_validate(
            {"key": "sample", "displayName": "Sample", "executorRoles": roles}
        )


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ({"executor": {"kind": "claude-code"}}, "agent_any"),
        ({"work": {}}, "agent_any"),
        ({"work": {"taskTypes": []}}, "agent_any"),
        ({"work": {"taskTypes": None}}, "agent_any"),
        ({"work": {"taskTypes": ["coding-task"]}}, "agent_task_types"),
        ({"work": {"taskTypes": ["design-task"]}}, None),
        # A service account with nothing to place and no work takes nothing.
        ({"placement": "none"}, None),
        ({}, None),
    ],
)
def test_why_an_agent_takes_a_type(spec: dict[str, Any], reason: str | None) -> None:
    assert _agent_reason(spec, "coding-task") == reason
