"""The catalog kind ``ConnectionType`` in the contract (CP-ADR-0079 §2, §12, §13, §17).

What is pinned here: the routes and bodies of ``/connection-types`` in OpenAPI,
the response model against the core's check of a spec (what the check passes
the response reads back, ``null`` for a field not set), the rights in the enum
and ``authz/catalog.yaml``, the payload of ``connection_type.published`` and
the kind in the package format: a package with a connection type is no
``unknown_kind``, the plan leaves it to the installer, who records it.
"""

import copy
import typing
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from control_plane.api.v1.connection_types import ETAG_ENTITY
from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import ConnectionTypeSpec, RecordedKind
from control_plane.domain.connection_type import (
    ACCOUNT_FIELDS,
    OAUTH2_FIELDS,
    SPEC_FIELDS,
    check_connection_type_spec,
)
from control_plane.domain.enums import ConnectionTypeStatus, Permission
from control_plane.domain.event_catalog import get_event_type
from control_plane.domain.package_links import LINKED_KINDS, RECORDED_KINDS
from control_plane.domain.package_plan import INSTALLER, outside
from control_plane.domain.package_source import KINDS, parse_package
from tests.unit.test_connection_type_domain import SPEC, TOKEN_ONLY

ROOT = Path(__file__).resolve().parents[2]


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
PATHS: dict[str, Any] = OPENAPI["paths"]
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]


def _ref(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def _body(route: dict[str, Any]) -> str:
    return _ref(route["requestBody"]["content"]["application/json"]["schema"])


def _response(route: dict[str, Any], status: str) -> dict[str, Any]:
    schema: dict[str, Any] = route["responses"][status]["content"]["application/json"]["schema"]
    return schema


def test_the_routes_of_the_kind() -> None:
    collection = PATHS["/api/v1/connection-types"]
    card = PATHS["/api/v1/connection-types/{ref}"]
    assert set(collection) == {"post", "get"}
    assert set(card) == {"get", "patch"}

    publish = collection["post"]
    assert _body(publish) == "ConnectionTypePublishRequest"
    assert _ref(_response(publish, "201")) == "ConnectionTypeOut"
    assert _ref(_response(publish, "200")) == "ConnectionTypeOut"
    assert {"409", "422"} <= set(publish["responses"])

    listing = {p["name"] for p in collection["get"]["parameters"]}
    assert listing == {"key", "status", "package", "limit", "cursor"}

    assert _ref(_response(card["get"], "200")) == "ConnectionTypeOut"
    patch = card["patch"]
    assert _body(patch) == "ConnectionTypeUpdateRequest"
    assert "if-match" in {p["name"].lower() for p in patch["parameters"]}
    assert _ref(_response(patch, "200")) == "ConnectionTypeOut"


def test_the_bodies_of_the_kind() -> None:
    publish = SCHEMAS["ConnectionTypePublishRequest"]
    assert set(publish["required"]) == {"key", "version", "spec"}
    assert publish["properties"]["key"]["pattern"] == "^[a-z0-9][a-z0-9-]{0,62}$"
    assert publish["properties"]["version"]["minimum"] == 1
    assert publish["properties"]["spec"]["allOf"] == [
        {"$ref": "#/components/schemas/ConnectionTypeSpec"}
    ]
    update = SCHEMAS["ConnectionTypeUpdateRequest"]
    assert update["required"] == ["status"]
    assert update["properties"]["status"]["enum"] == [s.value for s in ConnectionTypeStatus]

    out = SCHEMAS["ConnectionTypeOut"]
    assert set(out["properties"]) == {
        "id",
        "key",
        "version",
        "status",
        "spec",
        "specHash",
        "package",
        "createdBy",
        "createdAt",
        "rowVersion",
    }
    assert _ref(out["properties"]["spec"]) == "ConnectionTypeSpec"


def test_the_spec_schema_is_the_table_of_the_adr() -> None:
    spec = SCHEMAS["ConnectionTypeSpec"]
    assert set(spec["properties"]) == SPEC_FIELDS
    assert set(spec["required"]) == {"displayName", "auth", "settingsSchema", "defaultKey"}
    assert spec["additionalProperties"] is False
    assert spec["properties"]["auth"]["uniqueItems"] is True
    assert spec["properties"]["auth"]["items"]["enum"] == ["oauth2", "token"]

    oauth2 = SCHEMAS["ConnectionTypeOAuth2"]
    assert set(oauth2["properties"]) == OAUTH2_FIELDS
    assert set(oauth2["required"]) == {"authorizeUrl", "tokenUrlTemplate", "authStyle", "scopes"}
    assert oauth2["properties"]["authStyle"]["enum"] == ["in_params", "in_header"]
    assert oauth2["properties"]["scopes"]["maxItems"] == 50

    account = SCHEMAS["ConnectionTypeAccountField"]
    assert set(account["properties"]) == ACCOUNT_FIELDS
    assert set(account["required"]) == {"title", "pattern"}
    assert account["properties"]["pattern"]["maxLength"] == 500


def _set(value: Any) -> Any:
    """``value`` without the members that are ``null``: what a spec had set."""
    if isinstance(value, dict):
        return {name: _set(item) for name, item in value.items() if item is not None}
    return value


def test_what_the_check_passes_the_response_reads_back_with_null_for_unset() -> None:
    for spec in (SPEC, TOKEN_ONLY):
        checked = check_connection_type_spec(copy.deepcopy(spec))
        out = ConnectionTypeSpec.model_validate(checked.spec).model_dump(mode="json", by_alias=True)
        assert _set(out) == spec
        assert set(out) == SPEC_FIELDS
    token_only = ConnectionTypeSpec.model_validate(TOKEN_ONLY).model_dump(by_alias=True)
    assert token_only["oauth2"] is None
    assert token_only["description"] is None
    assert token_only["accountField"]["description"] is None


def test_the_etag_of_a_version() -> None:
    assert ETAG_ENTITY == "connection-type"


def test_the_rights_are_in_the_enum_and_the_catalog() -> None:
    assert Permission.CONNECTIONS_READ.value == "connections.read"
    assert Permission.CONNECTIONS_MANAGE.value == "connections.manage"
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    for name in ("connections.read", "connections.manage"):
        assert catalog["actions"][name] == {"resource": "tenant"}


def test_the_published_event_carries_keys_and_codes_only() -> None:
    entry = get_event_type("connection_type.published")
    assert entry.entity_type == "connection_type"
    schema = entry.current.schema
    assert set(schema["properties"]) == {"key", "version", "auth"}
    assert set(schema["required"]) == {"key", "version", "auth"}


def test_the_kind_is_in_the_package_format_after_capability() -> None:
    for kinds in (KINDS, LINKED_KINDS, RECORDED_KINDS, typing.get_args(RecordedKind)):
        assert kinds[kinds.index("Capability") + 1] == "ConnectionType"


def test_a_package_with_a_connection_type_parses_and_goes_to_the_installer() -> None:
    document = {"apiVersion": "platform.example/v1", "kind": "ConnectionType", "key": "crm"}
    package = parse_package(
        [
            ("package.yaml", "apiVersion: platform.example/v1\nkind: Package\nkey: p\nspec: {}\n"),
            ("connections/crm.yaml", yaml.safe_dump({**document, "spec": SPEC})),
        ]
    )
    assert package.problems == []
    assert [o.ref for o in package.objects] == ["ConnectionType/crm"]
    assert outside(package) == [{"kind": "ConnectionType", "key": "crm", "appliedBy": INSTALLER}]
