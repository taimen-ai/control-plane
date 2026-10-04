"""The sandbox of ``POST /packages:test`` with ``CP_IAM_ISSUER`` configured (CP-ADR-0074 Z6).

A rule test publishes the package's agents in its rolled-back transaction and
links an agent without a principal to a temporary identity of the sandbox's
own issuer: nobody presents a token of it. A stand with IAM trusts only its
``CP_IAM_ISSUER``, and the sandbox must pass there too, while the registry's
routes keep refusing every other issuer — the sandbox's one included.
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from control_plane.application.commands.package_trials import _TEST_ISSUER
from control_plane.config import Settings
from tests.helpers import auth, do_bootstrap
from tests.integration.test_agent_identity_replace import (
    KEY,
    SERVICE_SPEC,
    _registry_identity,
    _replace,
    _service,
    identity,
)
from tests.integration.test_agent_registry import ISSUER, _events, _link, _publish
from tests.integration.test_package_test import snapshot
from tests.integration.test_package_test_subjects import (
    CLAIM_REOPENED_TEST,
    REOPENED_RULE,
    document,
    helpdesk,
    run,
    setup,
)

AGENT = "claims-bot"
# What the rule needs: the interpretation is a skill call, the action files work.
RIGHTS = ["events.read", "skills.invoke", "tasks.read", "tasks.write"]
AGENT_SPEC = {
    "displayName": "Claims bot",
    "identity": {"kind": "service", "permissions": RIGHTS},
    "placement": "none",
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"iam_issuer": ISSUER})


def _package(rule: dict[str, Any]) -> list[dict[str, str]]:
    agent = document("Agent", AGENT, AGENT_SPEC)
    files = helpdesk(("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST), rule=rule)
    return [*files, {"path": f"agents/{AGENT}.yaml", "content": agent}]


def test_the_issuer_of_the_sandbox_is_not_the_stand_s() -> None:
    assert _TEST_ISSUER != ISSUER


async def test_a_rule_acting_as_a_package_agent_passes_with_an_iam_issuer(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    before = snapshot(sync_engine)

    body = await run(client, key, _package({**REOPENED_RULE, "identity": {"agent": AGENT}}))

    assert snapshot(sync_engine) == before
    assert body["status"] == "passed", json.dumps(body, ensure_ascii=False, indent=1)
    assert [(t["subject"], t["object"], t["status"]) for t in body["tests"]] == [
        ("rule", "claim-reopened", "passed")
    ]
    assert not [p for p in body["problems"] if p["code"] == "iam_issuer_untrusted"]
    # Nothing of the trial's identity is left on the stand.
    agents = await client.get("/api/v1/agents", headers=auth(key))
    assert agents.status_code == 200, agents.text
    assert agents.json()["items"] == []


# --- the routes of the registry still take CP_IAM_ISSUER ------------------------------


async def test_link_refuses_the_issuer_of_the_sandbox(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    published = await _publish(client, admin_key, SERVICE_SPEC, agent=KEY)
    assert published.status_code == 201, published.text

    refused = await _link(client, admin_key, agent=KEY, issuer=_TEST_ISSUER)

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert refused.json()["error"]["details"] == {"issuer": _TEST_ISSUER, "expected": ISSUER}
    assert await _events(client, admin_key, "principal.created") == []
    linked = await _link(client, admin_key, agent=KEY)
    assert linked.status_code == 200, linked.text


async def test_replace_refuses_the_issuer_of_the_sandbox(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, _, first = await _service(client)

    refused = await _replace(client, admin_key, identity(issuer=_TEST_ISSUER))

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "iam_issuer_untrusted"
    assert _registry_identity(sync_engine)[2] == first["iamPrincipalId"]
    assert await _events(client, admin_key, "agent.identity_replaced") == []
