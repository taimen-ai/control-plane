"""The agent daemon as skill executor against a real Control Plane (ADR-0056 §3, §5).

The implementations are real modules (``tests.skill_stubs``) run through the
``local`` protocol; everything else — claim, lease, report, the run of an
execution-typed task, the ``skill_result`` artifact — is the real server.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from control_plane_agent.main import Agent, ArtifactSpec, EchoAdapter
from control_plane_agent.skills import LocalProtocol, SkillExecutor
from control_plane_client import ControlPlaneClient
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
)
from tests.skill_stubs import arith, merge

Make = Callable[[str], ControlPlaneClient]

RUNNER = [*ORG_AGENT_PERMISSIONS, "skills.invoke", "skills.execute", "task_types.read"]
ENTRYPOINTS = ["tests.skill_stubs.arith:run", "tests.skill_stubs.merge:run"]


def executor(client: ControlPlaneClient, **kwargs: Any) -> SkillExecutor:
    return SkillExecutor(
        client,
        {"local": LocalProtocol(ENTRYPOINTS)},
        local_entrypoints=ENTRYPOINTS,
        poll_interval=0.05,
        **kwargs,
    )


async def publish(
    client: httpx.AsyncClient, key: str, name: str, contract: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": name,
            "version": "1",
            "sideEffects": extra.pop("side_effects", "none"),
            "riskLevel": "low",
            "contract": contract,
            **extra,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def actors(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    admin = boot["apiKey"]["key"]
    runner, runner_key = await create_agent_with_key(
        client, admin, name="runner", permissions=RUNNER
    )
    _, caller_key = await create_agent_with_key(
        client, admin, name="caller", permissions=["skills.invoke", "tasks.read"]
    )
    return {"admin": admin, "runner": runner_key, "runnerId": runner["id"], "caller": caller_key}


async def test_idle_daemon_executes_a_queued_invocation(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    await publish(client, actors["admin"], "arith.double", arith.CONTRACT)
    async with sdk(actors["caller"]) as caller, sdk(actors["runner"]) as runner:
        created = await caller.invoke_skill("arith.double@1", inputs={"n": 21})
        agent = Agent(
            runner, EchoAdapter(), poll_interval=0.05, max_cycles=1, skills=executor(runner)
        )
        await agent.run_forever()
        done = await caller.get_skill_invocation(created["id"])
    assert done["status"] == "succeeded"
    assert done["output"] == {"double": 42}
    # The lease was held by the daemon's session.
    assert done["executorSessionId"] is not None


async def test_work_with_execution_completes_through_one_invocation(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    admin = actors["admin"]
    skill = await publish(
        client, admin, "repo.merge", merge.CONTRACT, side_effects="external_write"
    )
    await assign_skill(client, admin, actors["runnerId"], skill["id"])
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "merge",
            "displayName": "Merge",
            "execution": {
                "skill": "repo.merge",
                "version": "1",
                "inputs": {"branch": "$.customFields.branch", "into": "$.customFields.target"},
            },
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    task = await create_task(
        client,
        admin,
        title="Merge TASK-1",
        typeKey="merge",
        customFields={"branch": "task/TASK-1", "target": "main"},
    )
    async with sdk(actors["runner"]) as runner:
        agent = Agent(
            runner, EchoAdapter(), poll_interval=0.05, max_cycles=1, skills=executor(runner)
        )
        await agent.run_forever()

    # The skill ran in a child process (local isolation): what it was asked
    # to do is seen in its output, recorded as the skill_result below.
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin))).json()
    assert record["status"] == "done"
    [run] = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin))
    ).json()["items"]
    assert run["status"] == "succeeded"
    assert run["output"]["status"] == "succeeded"

    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{run['output']['skillInvocationId']}", headers=auth(admin)
        )
    ).json()
    assert invocation["authorizationBasis"]["kind"] == "execution"
    assert invocation["runId"] == run["id"]
    assert invocation["idempotencyKey"] == f"execution:{run['id']}"
    artifacts = (
        await client.get("/api/v1/artifacts", params={"taskId": task["id"]}, headers=auth(admin))
    ).json()["items"]
    # The skill_result the core wrote is the whole evidence: no adapter ran.
    assert [a["type"] for a in artifacts] == ["skill_result"]
    assert artifacts[0]["content"]["output"] == {"merged": "task/TASK-1->main"}


async def test_failed_invocation_fails_the_run(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    admin = actors["admin"]
    contract = {
        **arith.CONTRACT,
        "implementation": {"protocol": "local", "entrypoint": "tests.skill_stubs.arith:broken"},
    }
    skill = await publish(client, admin, "arith.broken", contract)
    await assign_skill(client, admin, actors["runnerId"], skill["id"])
    await client.post(
        "/api/v1/task-types",
        json={
            "key": "calc",
            "displayName": "Calc",
            "execution": {"skill": "arith.broken", "version": "1"},
        },
        headers=auth(admin),
    )
    task = await create_task(client, admin, title="Calc", typeKey="calc", customFields={"n": 1})

    async with sdk(actors["runner"]) as runner:
        skills = SkillExecutor(
            runner,
            {"local": LocalProtocol(["tests.skill_stubs.arith:broken"])},
            local_entrypoints=["tests.skill_stubs.arith:broken"],
            poll_interval=0.05,
        )
        await Agent(
            runner, EchoAdapter(), poll_interval=0.05, max_cycles=1, skills=skills
        ).run_forever()

    [run] = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin))
    ).json()["items"]
    assert run["status"] == "failed"
    assert run["failureReason"] == "skill_invocation_failed: skill_error"
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin))).json()
    assert record["status"] != "done"


async def test_daemon_without_the_skill_leaves_execution_work_alone(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    admin = actors["admin"]
    await publish(client, admin, "arith.double", arith.CONTRACT)
    await client.post(
        "/api/v1/task-types",
        json={
            "key": "calc",
            "displayName": "Calc",
            "execution": {"skill": "arith.double", "version": "1"},
        },
        headers=auth(admin),
    )
    skill_work = await create_task(client, admin, title="Calc", typeKey="calc")
    ordinary = await create_task(client, admin, title="Ordinary")

    async with sdk(actors["runner"]) as runner:
        await Agent(runner, EchoAdapter(), poll_interval=0.05, max_cycles=2).run_forever()

    statuses = {
        t["id"]: (await client.get(f"/api/v1/tasks/{t['id']}", headers=auth(admin))).json()[
            "status"
        ]
        for t in (skill_work, ordinary)
    }
    assert statuses == {skill_work["id"]: "todo", ordinary["id"]: "done"}


class BlockingAdapter:
    """Work that lasts until the queued skill invocation is done — or times out."""

    def __init__(self, caller: ControlPlaneClient, invocation_id: str) -> None:
        self.caller = caller
        self.invocation_id = invocation_id
        self.saw: str | None = None

    async def execute(self, task: Any, run: Any, client: Any, workspace: Any = None) -> list:
        for _ in range(100):
            self.saw = (await self.caller.get_skill_invocation(self.invocation_id))["status"]
            if self.saw == "succeeded":
                break
            await asyncio.sleep(0.05)
        return [ArtifactSpec(type="report", name="slow", content={"saw": self.saw})]


async def test_skill_workers_run_beside_long_work(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    """A long Work does not hold the skill queue (CONTROL_PLANE_SKILLS_CONCURRENCY)."""
    await publish(client, actors["admin"], "arith.double", arith.CONTRACT)
    await create_task(client, actors["admin"], title="Long code work")
    async with sdk(actors["caller"]) as caller, sdk(actors["runner"]) as runner:
        created = await caller.invoke_skill("arith.double@1", inputs={"n": 4})
        adapter = BlockingAdapter(caller, created["id"])
        agent = Agent(
            runner,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            skills=executor(runner, concurrency=2),
        )
        await agent.run_forever()
    # The invocation finished while the Work was still running.
    assert adapter.saw == "succeeded"


async def test_lease_lost_during_execution_sends_nothing(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    """The call is cancelled while it runs: the executor is fenced out and
    drops the result instead of reporting it."""
    await publish(client, actors["admin"], "arith.double", arith.CONTRACT)
    async with sdk(actors["caller"]) as caller, sdk(actors["runner"]) as runner:
        created = await caller.invoke_skill("arith.double@1", inputs={"n": 1, "sleep": 1.0})
        skills = executor(runner, heartbeat_interval=0.1)
        claimed = await skills.claim(None)
        assert claimed is not None

        async def cancel_soon() -> None:
            await asyncio.sleep(0.2)
            await caller.cancel_skill_invocation(created["id"], reason="changed my mind")

        outcome, _ = await asyncio.gather(skills.execute_claimed(claimed, None), cancel_soon())
        final = await caller.get_skill_invocation(created["id"])

    assert outcome == "lease_lost"
    assert final["status"] == "cancelled"
    assert final["output"] is None
    assert final["error"]["details"]["wasRunning"] is True


def spy_listings(client: ControlPlaneClient) -> list[dict[str, Any]]:
    """Record the arguments of every ``list_available_work`` the daemon makes."""
    calls: list[dict[str, Any]] = []
    listing = client.list_available_work

    async def recorded(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return await listing(**kwargs)

    client.list_available_work = recorded  # type: ignore[method-assign]
    return calls


async def test_skills_executor_finds_its_work_behind_a_full_page_of_other_work(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    """CP-ADR-0056 Ж2: 60 older, more urgent tasks of another type do not hide its own.

    The executor of kind ``skills`` (no adapter) lists only the types whose
    skill it runs, so its task is on the first page whatever stands above it.
    """
    admin = actors["admin"]
    skill = await publish(
        client, admin, "repo.merge", merge.CONTRACT, side_effects="external_write"
    )
    await assign_skill(client, admin, actors["runnerId"], skill["id"])
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "merge",
            "displayName": "Merge",
            "execution": {
                "skill": "repo.merge",
                "version": "1",
                "inputs": {"branch": "$.customFields.branch", "into": "$.customFields.target"},
            },
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    for n in range(60):
        await create_task(client, admin, title=f"Someone else's {n}", priority="critical")
    task = await create_task(
        client,
        admin,
        title="Merge TASK-1",
        typeKey="merge",
        priority="low",
        customFields={"branch": "task/TASK-1", "target": "main"},
    )

    async with sdk(actors["runner"]) as runner:
        listings = spy_listings(runner)
        await Agent(
            runner, None, poll_interval=0.05, max_cycles=1, skills=executor(runner)
        ).run_forever()

    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin))).json()
    assert record["status"] == "done"
    assert [call["type_keys"] for call in listings] == [["merge"]]


async def test_skills_executor_without_a_type_it_runs_does_not_list_work(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    """No type's skill is runnable here: nothing to look for, the queue is not read."""
    admin = actors["admin"]
    await publish(client, admin, "arith.double", arith.CONTRACT)
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "calc",
            "displayName": "Calc",
            "execution": {"skill": "arith.double", "version": "1"},
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    await create_task(client, admin, title="Calc", typeKey="calc")
    await create_task(client, admin, title="Ordinary")
    merge_only = ["tests.skill_stubs.merge:run"]

    async with sdk(actors["runner"]) as runner:
        listings = spy_listings(runner)
        skills = SkillExecutor(
            runner,
            {"local": LocalProtocol(merge_only)},
            local_entrypoints=merge_only,
            poll_interval=0.05,
        )
        await Agent(runner, None, poll_interval=0.05, max_cycles=1, skills=skills).run_forever()

    assert listings == []


async def test_daemon_pages_past_a_page_it_takes_nothing_from(
    client: httpx.AsyncClient, sdk: Make, actors: dict[str, Any]
) -> None:
    """Another kind keeps its listing; a page its own filter empties is followed by the next."""
    admin = actors["admin"]
    response = await client.post(
        "/api/v1/task-types", json={"key": "chore", "displayName": "Chore"}, headers=auth(admin)
    )
    assert response.status_code == 201, response.text
    for n in range(60):
        await create_task(client, admin, title=f"Someone else's {n}", priority="critical")
    chore = await create_task(client, admin, title="Chore", typeKey="chore", priority="low")

    async with sdk(actors["runner"]) as runner:
        listings = spy_listings(runner)
        await Agent(
            runner,
            EchoAdapter(),
            poll_interval=0.05,
            max_cycles=1,
            task_types=frozenset({"chore"}),
        ).run_forever()

    [run] = (
        await client.get("/api/v1/runs", params={"taskId": chore["id"]}, headers=auth(admin))
    ).json()["items"]
    assert run["status"] == "succeeded"
    assert [call["type_keys"] for call in listings] == [None, None]
    assert listings[0]["cursor"] is None and listings[1]["cursor"] is not None
