"""An agent's secrets by name in the contract (CP-ADR-0079 §11, §12, §13, §17; I012).

Pinned here: the routes and bodies in OpenAPI (the value ``writeOnly``, never
in an answer), the right in the enum and the catalog, the payloads of
``agent.secret_set`` and ``agent.secret_deleted``, the paths in the store, the
form of a name, and the policy's own check of its paths.
"""

import uuid

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from control_plane.api.v1.schemas import AgentSecretSetRequest
from control_plane.domain.connection_access import (
    agent_policy,
    agent_secret_store_name,
    agent_secrets_ref,
    is_policy_path,
    is_secret_name,
    kv_connection_ref,
    oauth_creds_ref,
)
from control_plane.domain.enums import Permission
from control_plane.domain.event_catalog import get_event_type
from tests.fake_openbao import policy_allows
from tests.unit.test_connection_contract import ROOT
from tests.unit.test_connection_type_contract import PATHS, SCHEMAS, _ref, _response

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def test_the_routes_and_their_bodies() -> None:
    listed = PATHS["/api/v1/agents/{key}/secrets"]
    assert set(listed) == {"get"}
    assert _ref(_response(listed["get"], "200")) == "AgentSecretListOut"

    one = PATHS["/api/v1/agents/{key}/secrets/{name}"]
    assert set(one) == {"put", "delete"}
    body = one["put"]["requestBody"]["content"]["application/json"]["schema"]
    assert _ref(body) == "AgentSecretSetRequest"
    assert _ref(_response(one["put"], "200")) == "AgentSecretOut"
    assert "503" in one["put"]["responses"]
    assert "204" in one["delete"]["responses"]
    assert "503" in one["delete"]["responses"]


def test_the_value_is_write_only_and_never_answered() -> None:
    request = SCHEMAS["AgentSecretSetRequest"]
    assert request["required"] == ["value"]
    value = request["properties"]["value"]
    assert value["writeOnly"] is True
    assert (value["minLength"], value["maxLength"]) == (1, 65_536)
    assert set(SCHEMAS["AgentSecretOut"]["properties"]) == {"name", "updatedAt", "updatedBy"}
    assert set(SCHEMAS["AgentSecretListOut"]["properties"]) == {"items"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"value": ""},
        {"value": None},
        {"value": 1},
        {"value": "x" * 65_537},
        {"value": "v", "x": 1},
    ],
)
def test_a_value_is_one_string_of_bounded_length(body: dict[str, object]) -> None:
    with pytest.raises(PydanticValidationError):
        AgentSecretSetRequest.model_validate(body)


def test_the_right_is_in_the_enum_and_the_catalog() -> None:
    assert Permission.AGENTS_SECRETS_MANAGE.value == "agents.secrets.manage"
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    assert catalog["actions"]["agents.secrets.manage"] == {"resource": "tenant"}


@pytest.mark.parametrize(
    ("event_type", "fields"),
    [
        ("agent.secret_set", {"agentKey", "name", "created"}),
        ("agent.secret_deleted", {"agentKey", "name"}),
    ],
)
def test_the_events_carry_the_name_only(event_type: str, fields: set[str]) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == "agent"
    assert entry.current.version == 1
    schema = entry.current.schema
    assert set(schema["properties"]) == fields
    assert set(schema["required"]) == fields


def test_the_paths_in_the_store() -> None:
    assert agent_secret_store_name(TENANT, "runner", "gh-token") == (
        f"tenants/{TENANT}/agents/runner/gh-token"
    )
    ref = agent_secrets_ref(TENANT, "runner")
    assert ref == f"kv/data/tenants/{TENANT}/agents/runner/*"
    policy = agent_policy([ref])
    assert policy_allows(policy, "GET", f"kv/data/tenants/{TENANT}/agents/runner/gh-token")
    for path in (
        f"kv/data/tenants/{TENANT}/agents/runner-x/gh-token",
        f"kv/data/tenants/{TENANT}/agents/other/gh-token",
        f"kv/metadata/tenants/{TENANT}/agents/runner/gh-token",
    ):
        assert not policy_allows(policy, "GET", path), path


@pytest.mark.parametrize("name", ["a", "gh-token", "0", "a" * 63, "npm-2"])
def test_a_secret_name_has_the_form_of_placement_secrets(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "-a", "A", "a" * 64, "a.b", "a_b", "a/b", "..", "a\n", "ä", "a b", "*"],
)
def test_anything_else_is_not_a_name(name: str) -> None:
    assert not is_secret_name(name)


@pytest.mark.parametrize(
    "path",
    [
        kv_connection_ref(TENANT, "crm"),
        oauth_creds_ref(TENANT, "crm-2"),
        agent_secrets_ref(TENANT, "runner"),
    ],
)
def test_the_core_paths_pass_the_policys_own_check(path: str) -> None:
    assert is_policy_path(path)
    assert agent_policy([path]) == f'path "{path}" {{ capabilities = ["read"] }}\n'


@pytest.mark.parametrize(
    "path",
    [
        "",
        "*",
        "kv",
        "kv/*",
        "kv/data/*/x",
        'kv/data/a" { capabilities = ["sudo"] } path "x',
        "kv/data/a\npath",
        "kv/data/a}",
        "kv/data/../sys",
        "kv/data/./a",
        "kv/data//a",
        "kv/data/a/",
        "kv/data/A",
        "kv/data/a+",
        "kv/data/ä",
        "/kv/data/a",
        "kv/data/" + "a" * 600,
    ],
)
def test_a_path_outside_the_form_never_reaches_a_policy(path: str) -> None:
    assert not is_policy_path(path)
    with pytest.raises(ValueError):
        agent_policy([kv_connection_ref(TENANT, "crm"), path])
