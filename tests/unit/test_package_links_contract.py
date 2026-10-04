"""The link of a catalog object to its package in the contract (TASK-000904).

CP-ADR-0074 §11, amendment of 2026-09-29: every list and card of a catalog
kind the core holds carries ``package`` (``PackageLinkOut`` or null), every
such list takes ``?package=<key>``, and ``POST /packages:record`` takes the
kinds the installer applies. The kinds are the same in the domain, the
request schema, the table's check and the catalog format.
"""

import typing
from typing import Any

import pytest
from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import RecordedKind
from control_plane.domain.package_links import ENGINE_KINDS, LINKED_KINDS, RECORDED_KINDS
from control_plane.domain.package_plan import PLANNED_KINDS
from control_plane.domain.package_source import KINDS
from control_plane.infrastructure.db.models import PackageObject


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
PATHS: dict[str, Any] = OPENAPI["paths"]
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]

# kind -> (response body, list route, card route)
CATALOG: dict[str, tuple[str, str, str]] = {
    "ArtifactType": ("ArtifactTypeOut", "/api/v1/artifact-types", "/api/v1/artifact-types/{ref}"),
    "TaskType": ("TaskTypeOut", "/api/v1/task-types", "/api/v1/task-types/{type_id}"),
    "ProjectTemplate": (
        "ProjectTemplateOut",
        "/api/v1/project-templates",
        "/api/v1/project-templates/{template_id}",
    ),
    "WorkspaceType": (
        "WorkspaceTypeOut",
        "/api/v1/workspace-types",
        "/api/v1/workspace-types/{type_id}",
    ),
    "Role": ("RoleOut", "/api/v1/roles", "/api/v1/roles/{role_id}"),
    "Capability": ("CapabilityOut", "/api/v1/capabilities", "/api/v1/capabilities/{capability_id}"),
    "ConnectionType": (
        "ConnectionTypeOut",
        "/api/v1/connection-types",
        "/api/v1/connection-types/{ref}",
    ),
    "Skill": ("SkillOut", "/api/v1/skills", "/api/v1/skills/{skill_ref}"),
    "WorkRule": ("RuleOut", "/api/v1/rules", "/api/v1/rules/{rule_id}"),
    "Agent": ("AgentOut", "/api/v1/agents", "/api/v1/agents/{ref}"),
    "Process": (
        "ProcessDefinitionOut",
        "/api/v1/process-definitions",
        "/api/v1/process-definitions/{ref}",
    ),
    "Calendar": ("CalendarOut", "/api/v1/calendars", "/api/v1/calendars/{ref}"),
    "View": ("ViewOut", "/api/v1/views", "/api/v1/views/{view_key}"),
}


def _ref(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def test_the_kinds_agree_everywhere() -> None:
    assert set(CATALOG) == set(LINKED_KINDS)
    assert set(LINKED_KINDS) <= set(KINDS)
    assert set(PLANNED_KINDS) <= set(LINKED_KINDS)
    assert set(ENGINE_KINDS) <= set(PLANNED_KINDS)
    assert tuple(k for k in LINKED_KINDS if k not in ENGINE_KINDS) == RECORDED_KINDS
    assert typing.get_args(RecordedKind) == RECORDED_KINDS
    check = next(
        str(c.sqltext)
        for c in PackageObject.__table__.constraints
        if getattr(c, "name", None) == "ck_package_objects_kind_known"
    )
    assert all(f"'{kind}'" in check for kind in LINKED_KINDS)
    assert "'NotificationRule'" not in check


@pytest.mark.parametrize("kind", list(CATALOG))
def test_lists_and_cards_carry_the_package_and_lists_filter_by_it(kind: str) -> None:
    body, listing, card = CATALOG[kind]
    field = SCHEMAS[body]["properties"]["package"]
    assert [_ref(item) for item in field["anyOf"] if "$ref" in item] == ["PackageLinkOut"]
    assert {"type": "null"} in field["anyOf"]
    assert "package" not in SCHEMAS[body].get("required", [])
    responses = PATHS[card]["get"]["responses"]
    assert _ref(responses["200"]["content"]["application/json"]["schema"]) == body
    parameters = {(p["name"], p["in"]) for p in PATHS[listing]["get"]["parameters"]}
    assert ("package", "query") in parameters


def test_the_link_names_the_package_its_version_and_installation() -> None:
    link = SCHEMAS["PackageLinkOut"]
    assert set(link["properties"]) == {"key", "version", "installHash", "installedAt"}


def test_the_installer_records_through_one_route() -> None:
    route = PATHS["/api/v1/packages:record"]["post"]
    request = route["requestBody"]["content"]["application/json"]["schema"]
    assert _ref(request) == "PackageRecordRequest"
    assert _ref(route["responses"]["200"]["content"]["application/json"]["schema"]) == (
        "PackageRecordOut"
    )
    body = SCHEMAS["PackageRecordRequest"]
    assert set(body["required"]) == {"package", "objects"}
    kinds = SCHEMAS["PackageRecordedObject"]["properties"]["kind"]["enum"]
    assert tuple(kinds) == RECORDED_KINDS
