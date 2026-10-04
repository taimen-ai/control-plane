"""Agents' access, ``:revoke`` and ``Agent.spec.connections`` in the contract.

CP-ADR-0079 §8, §9, §10, §13, §17. What is pinned here: the routes and bodies
in OpenAPI, the field of the agent spec, the payload of ``connection.revoked``,
the policy and role the worker writes (and how a role the store answers is
compared), the names it owns, and the events that wake it.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from control_plane.api.v1.schemas import AgentSpec, ConnectionRevokeRequest
from control_plane.application.commands.connection_policies import (
    POLICY_SYNC_CONSUMER,
    triggers_sync,
)
from control_plane.config import Settings
from control_plane.domain.connection_access import (
    agent_policy,
    agent_policy_name,
    agent_role,
    agent_role_name,
    principal_of_policy,
    principal_of_role,
    role_matches,
)
from control_plane.domain.event_catalog import get_event_type
from tests.fake_openbao import policy_allows
from tests.unit.test_connection_type_contract import PATHS, SCHEMAS, _ref, _response

PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
IAM_PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
IAM_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def _role() -> dict[str, Any]:
    return agent_role(
        principal_id=PRINCIPAL, iam_principal_id=IAM_PRINCIPAL, iam_tenant_id=IAM_TENANT
    )


# --- routes and bodies (§17) ------------------------------------------------------------


def test_the_routes_of_revoke_and_of_the_agents_view() -> None:
    revoke = PATHS["/api/v1/connections/{key}:revoke"]
    assert set(revoke) == {"post"}
    # The body is optional: ``{reason?}`` or nothing at all.
    body = revoke["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert [_ref(item) for item in body["anyOf"] if "$ref" in item] == ["ConnectionRevokeRequest"]
    assert revoke["post"]["requestBody"].get("required") is not True
    assert _ref(_response(revoke["post"], "200")) == "ConnectionOut"
    assert "503" in revoke["post"]["responses"]

    mine = PATHS["/api/v1/agents/me/connections"]
    assert set(mine) == {"get"}
    assert _ref(_response(mine["get"], "200")) == "AgentConnectionListOut"
    one = PATHS["/api/v1/agents/me/connections/{key}"]
    assert set(one) == {"get"}
    assert _ref(_response(one["get"], "200")) == "AgentConnectionOut"
    assert "404" in one["get"]["responses"]


def test_the_bodies() -> None:
    assert set(SCHEMAS["ConnectionRevokeRequest"]["properties"]) == {"reason"}
    assert SCHEMAS["AgentConnectionOut"]["required"] == [
        "key",
        "type",
        "typeVersion",
        "account",
        "auth",
        "status",
        "settings",
        "secretRef",
        "expiresAt",
    ]
    connections = SCHEMAS["AgentSpec"]["properties"]["connections"]
    assert connections["maxItems"] == 20
    assert connections["items"]["pattern"] == "^[a-z0-9][a-z0-9-]{0,62}$"
    assert "connections" not in SCHEMAS["AgentSpec"].get("required", [])


@pytest.mark.parametrize("reason", ["x" * 501, 5, ["a"]])
def test_a_revoke_body_is_narrow(reason: Any) -> None:
    with pytest.raises(PydanticValidationError):
        ConnectionRevokeRequest.model_validate({"reason": reason})
    with pytest.raises(PydanticValidationError):
        ConnectionRevokeRequest.model_validate({"other": 1})
    assert ConnectionRevokeRequest.model_validate({}).reason is None


def _spec(**fields: Any) -> dict[str, Any]:
    return {
        "displayName": "Connector",
        "identity": {"kind": "agent", "permissions": ["events.read"]},
        "placement": "none",
        **fields,
    }


def test_the_field_is_absent_from_a_spec_that_does_not_send_it() -> None:
    sent = AgentSpec.model_validate(_spec()).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    assert "connections" not in sent
    named = AgentSpec.model_validate(_spec(connections=["crm", "crm-2"]))
    assert named.connections == ["crm", "crm-2"]


@pytest.mark.parametrize(
    "connections",
    [
        ["crm", "crm"],
        ["Crm"],
        [""],
        ["a" * 64],
        [f"c{index}" for index in range(21)],
        None,
        "crm",
        [{"key": "crm"}],
    ],
)
def test_the_field_takes_up_to_twenty_unique_keys(connections: Any) -> None:
    with pytest.raises(PydanticValidationError):
        AgentSpec.model_validate(_spec(connections=connections))


def test_the_revocation_event_carries_keys_and_statuses_only() -> None:
    entry = get_event_type("connection.revoked")
    assert entry.entity_type == "connection"
    assert entry.current.version == 1
    schema = entry.current.schema
    assert set(schema["properties"]) == {"key", "type", "previousStatus"}
    assert set(schema["required"]) == {"key", "type", "previousStatus"}


def test_the_full_pass_period_is_five_minutes_by_default() -> None:
    assert Settings.model_fields["connections_sync_seconds"].default == 300.0
    assert POLICY_SYNC_CONSUMER == "connections-policy-sync"


# --- the policy and the role (§9) ------------------------------------------------------------


def test_the_names_of_an_agents_policy_and_role() -> None:
    assert agent_policy_name(PRINCIPAL) == f"cp-agent-{PRINCIPAL}"
    assert agent_role_name(PRINCIPAL) == f"agent-{PRINCIPAL}"
    assert principal_of_policy(agent_policy_name(PRINCIPAL)) == PRINCIPAL
    assert principal_of_role(agent_role_name(PRINCIPAL)) == PRINCIPAL


@pytest.mark.parametrize(
    "name",
    [
        "default",
        "root",
        "control-plane",
        "cp-agent-",
        "cp-agent-x",
        f"cp-agent-{str(PRINCIPAL).upper()}",
        f"cp-agent-{PRINCIPAL.hex}",
        f"cp-agent-{PRINCIPAL}-old",
        f"agent-{PRINCIPAL}",
        f"xcp-agent-{PRINCIPAL}",
    ],
)
def test_only_its_own_names_are_the_workers(name: str) -> None:
    assert principal_of_policy(name) is None


@pytest.mark.parametrize(
    "name",
    ["agent-", "agent-x", f"agent-{PRINCIPAL.hex}", f"cp-agent-{PRINCIPAL}", "control-plane"],
)
def test_only_its_own_roles_are_the_workers(name: str) -> None:
    assert principal_of_role(name) is None


def test_a_policy_reads_each_path_once_in_order() -> None:
    first = "oauth2/creds/tenants/t/connections/b"
    second = "kv/data/tenants/t/connections/a"
    policy = agent_policy([first, second, first])
    assert policy == (
        f'path "{second}" {{ capabilities = ["read"] }}\n'
        f'path "{first}" {{ capabilities = ["read"] }}\n'
    )
    assert agent_policy([second, first]) == policy
    # What it gives is read on those paths, and nothing else.
    assert policy_allows(policy, "GET", first)
    assert not policy_allows(policy, "GET", first + "x")
    assert not policy_allows(policy, "DELETE", first)
    assert not policy_allows(policy, "GET", "kv/data/tenants/t/connections/c")


def test_the_role_binds_the_iam_subject_for_five_minutes() -> None:
    assert _role() == {
        "role_type": "jwt",
        "user_claim": "sub",
        "bound_subject": str(IAM_PRINCIPAL),
        "bound_claims_type": "string",
        "bound_claims": {"tenant_id": str(IAM_TENANT), "principal_type": "agent"},
        "bound_audiences": ["openbao"],
        "token_policies": [f"cp-agent-{PRINCIPAL}"],
        "token_no_default_policy": True,
        "token_ttl": 300,
        "token_max_ttl": 300,
    }


def test_a_role_as_the_store_answers_it_matches() -> None:
    stored = {
        **_role(),
        "bound_audiences": ["openbao"],
        "bound_claims": {"principal_type": ["agent"], "tenant_id": str(IAM_TENANT)},
        "token_ttl": "5m",
        "token_max_ttl": 300.0,
        "clock_skew_leeway": 0,
        "token_type": "default",
        "allowed_redirect_uris": [],
    }
    assert role_matches(stored, _role())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bound_subject", str(uuid.uuid4())),
        ("bound_claims", {"tenant_id": str(IAM_TENANT), "principal_type": "service_account"}),
        ("bound_claims", {"tenant_id": str(IAM_TENANT)}),
        ("bound_claims", None),
        ("bound_audiences", ["openbao", "control-plane"]),
        ("bound_audiences", "openbao"),
        ("token_policies", [f"cp-agent-{PRINCIPAL}", "default"]),
        ("token_no_default_policy", False),
        ("token_ttl", 3600),
        ("token_ttl", "1h"),
        ("token_ttl", True),
        ("token_ttl", None),
        ("token_max_ttl", "garbage"),
        ("role_type", "oidc"),
        ("user_claim", "email"),
    ],
)
def test_a_role_that_differs_in_what_the_core_writes_does_not_match(field: str, value: Any) -> None:
    stored = {**_role(), field: value}
    assert not role_matches(stored, _role())
    assert not role_matches({k: v for k, v in _role().items() if k != field}, _role())


@pytest.mark.parametrize(
    ("event_type", "wakes"),
    [
        ("agent.revision_published", True),
        ("agent.retired", True),
        ("agent.secret_set", True),
        ("agent.secret_deleted", True),
        ("iam_binding.created", True),
        ("iam_binding.updated", True),
        ("iam_binding.revoked", True),
        ("principal.disabled", False),
        ("connection.created", True),
        ("connection.authorized", True),
        ("connection.status_changed", True),
        ("connection.revoked", True),
        ("connection.updated", True),
        ("connection_type.published", False),
        ("agent.state_changed", False),
        ("agent.status_changed", False),
        ("task.created", False),
    ],
)
def test_the_events_that_wake_the_worker(event_type: str, wakes: bool) -> None:
    assert triggers_sync(event_type) is wakes
