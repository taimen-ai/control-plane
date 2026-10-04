"""A skill of a package gets its settings through the daemon (CP-ADR-0081 §8, G006).

The real server hands ``settings`` out with the claim; the daemon puts them in
the ``meta`` of the skill-sdk wire contract (``__skill_invoke__``), which the
stub answers back in its outputs.
"""

from collections.abc import Callable
from typing import Any

import httpx

from control_plane_agent.main import Agent, EchoAdapter
from control_plane_agent.skills import LocalProtocol, SkillExecutor
from control_plane_client import ControlPlaneClient
from tests.client.test_skill_executor import RUNNER
from tests.helpers import auth, create_agent_with_key, create_role, do_bootstrap
from tests.integration.test_package_settings import PACKAGE, _put, declared, install, package_files

Make = Callable[[str], ControlPlaneClient]

ENTRYPOINT = "tests.skill_stubs.sdk_like:double"
CONTRACT: dict[str, Any] = {
    "inputs": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    "outputs": {"type": "object", "required": ["double"]},
    "timeoutSeconds": 5,
    "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
}


async def _skill(client: httpx.AsyncClient, admin: str, name: str) -> None:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": name,
            "version": "1",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": CONTRACT,
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text


async def _run(sdk: Make, caller_key: str, runner_key: str, ref: str) -> dict[str, Any]:
    async with sdk(caller_key) as caller, sdk(runner_key) as runner:
        created = await caller.invoke_skill(ref, inputs={"n": 2})
        skills = SkillExecutor(
            runner,
            {"local": LocalProtocol([ENTRYPOINT])},
            local_entrypoints=[ENTRYPOINT],
            poll_interval=0.05,
        )
        agent = Agent(runner, EchoAdapter(), poll_interval=0.05, max_cycles=1, skills=skills)
        await agent.run_forever()
        done = await caller.get_skill_invocation(created["id"])
    assert done["status"] == "succeeded", done
    seen: dict[str, Any] = done["output"]["seen"]
    return seen


async def test_the_daemon_hands_a_skill_the_settings_of_its_package(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin = boot["apiKey"]["key"]
    role = (await create_role(client, admin, "approver"))["id"]
    await install(client, admin, package_files(settings=declared()))
    saved = await _put(client, admin, {"owner": role, "limit": 5000}, 0)
    assert saved.status_code == 200, saved.text
    await _skill(client, admin, "sample.double")
    await _skill(client, admin, "manual.double")
    recorded = await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": PACKAGE, "version": "1.0.0"},
            "objects": [{"kind": "Skill", "key": "sample.double"}],
        },
        headers=auth(admin),
    )
    assert recorded.status_code == 200, recorded.text
    _, runner_key = await create_agent_with_key(client, admin, name="runner", permissions=RUNNER)
    _, caller_key = await create_agent_with_key(
        client, admin, name="caller", permissions=["skills.invoke", "tasks.read"]
    )

    seen = await _run(sdk, caller_key, runner_key, "sample.double@1")
    assert seen["settings"] == {
        "package": PACKAGE,
        "version": 1,
        "schemaRevision": 1,
        "values": {
            "limit": 5000,
            "days": 2,
            "owner": role,
            "note": "",
            "window": {"start": 9, "end": 18},
        },
    }

    seen = await _run(sdk, caller_key, runner_key, "manual.double@1")
    assert seen["settings"] is None
