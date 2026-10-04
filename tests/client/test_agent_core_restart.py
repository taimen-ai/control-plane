"""The daemon outlives a restart of the core (TASK-001138).

On staging a rollout of the core leaves the proxy answering 502 for seven to
ten seconds. Two runs were lost to it on 2026-09-30: one through a heartbeat
(``lease_lost``), one through the adapter's ``finish_action`` (``cycle
error: http_error``). Here the real core sits behind a transport that answers
502 to chosen calls the way the proxy does, in the middle of a run.
"""

import asyncio
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import control_plane_agent.main as agent_module
import control_plane_client.client as client_module
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.supervision import SupervisionSettings
from control_plane_agent.workspace import Workspace
from control_plane_client import ControlPlaneClient, HeartbeatRunner
from tests.client.test_agent import RUNNER_PERMISSIONS
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap


class RestartingCore(httpx.AsyncBaseTransport):
    """The app behind a proxy that answers 502 to the next N calls of a route."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.outages: list[tuple[str, re.Pattern[str], int]] = []
        self.bad_gateways: list[str] = []

    def down_for(self, method: str, path: str, times: int) -> None:
        self.outages.append((method, re.compile(path), times))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for index, (method, path, times) in enumerate(self.outages):
            if times and request.method == method and path.search(request.url.path):
                self.outages[index] = (method, path, times - 1)
                self.bad_gateways.append(f"{request.method} {request.url.path}")
                return httpx.Response(502, text="<html><h1>502 Bad Gateway</h1></html>")
        return await self._inner.handle_async_request(request)


class NarratingAdapter:
    """Works a moment, bookkeeping its step as a run action like the real adapters."""

    def __init__(self, *, fail: bool = False, seconds: float = 0.3) -> None:
        self.fail = fail
        self.seconds = seconds

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        action = await client.record_action(run["id"], action="turn", status="started")
        await asyncio.sleep(self.seconds)
        await client.finish_action(run["id"], str(action["id"]), status="completed")
        if self.fail:
            raise RuntimeError("the executor gave up")
        return [ArtifactSpec(type="report", name="summary", content={"done": True})]


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF", 0.01)
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF_MAX", 0.05)


#: The daemon's patience for its leases here: the same as the client's
#: ``retry_window`` below. In production it is three intervals, 180 s against a
#: rollout of seven to ten. Three of the test's 50 ms intervals are 150 ms —
#: about what a few in-process calls to the core take on a busy CI runner, so
#: one 502 met by a beat that came late ended the lease (``lease_lost``).
LEASE_PATIENCE = 5.0


class PatientHeartbeats(HeartbeatRunner):
    """The daemon's heartbeats at the production ratio of patience to outage."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("outage_budget_seconds", LEASE_PATIENCE)
        super().__init__(*args, **kwargs)


async def _setup(client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Work through a rollout")
    return admin_key, agent_key, task


async def _runs(client: httpx.AsyncClient, key: str, task_id: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/runs", params={"taskId": task_id}, headers=auth(key))
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _agent(
    core: RestartingCore, agent_key: str, adapter: NarratingAdapter
) -> Callable[[], ControlPlaneClient]:
    def make() -> ControlPlaneClient:
        return ControlPlaneClient("http://testserver", agent_key, transport=core, retry_window=5.0)

    return make


async def test_a_restart_in_the_middle_of_a_run_does_not_fail_it(
    client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Beats every 50 ms meet the outages below; the budget stays the
    # daemon's, not a multiple of the compressed interval. A 502 taken for an
    # answer of the core still ends the lease at once.
    monkeypatch.setattr(agent_module, "HeartbeatRunner", PatientHeartbeats)
    admin_key, agent_key, task = await _setup(client)
    core = RestartingCore(app)
    # The calls of the 10:45 log, and the heartbeat of the 09:49 one.
    core.down_for("POST", r"/actions/[^/]+:finish$", 2)
    core.down_for("POST", r"/claims/[^/]+:heartbeat$", 1)
    core.down_for("POST", r"/sessions/[^/]+:heartbeat$", 1)
    core.down_for("GET", r"/runs/[^/]+$", 2)
    core.down_for("GET", r"/runs/[^/]+/checkpoints$", 1)
    core.down_for("POST", r"/artifacts$", 1)
    core.down_for("POST", r"/runs/[^/]+:succeed$", 1)
    adapter = NarratingAdapter()

    async with _agent(core, agent_key, adapter)() as sdk:
        agent = Agent(
            sdk,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            heartbeat_interval=0.05,
            # The supervisor reads the run while the adapter works.
            supervision=SupervisionSettings(
                poll_seconds=0.05, stall_warn_seconds=0, stall_stop_seconds=0
            ),
        )
        await agent.run_forever()

    [run] = await _runs(client, admin_key, task["id"])
    assert run["status"] == "succeeded", run.get("failureReason")
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["status"] == "done"
    assert record["activeClaimId"] is None
    # Every outage was actually hit, so each of these paths was exercised.
    assert all(times == 0 for _, _, times in core.outages), core.outages
    actions = (
        await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(admin_key))
    ).json()["items"]
    assert [(a["action"], a["status"]) for a in actions] == [("turn", "completed")]


async def test_fail_run_is_delivered_through_a_restart(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Otherwise the run hangs ``running`` until its claim expires."""
    admin_key, agent_key, task = await _setup(client)
    core = RestartingCore(app)
    core.down_for("POST", r"/runs/[^/]+:fail$", 2)
    adapter = NarratingAdapter(fail=True, seconds=0.05)

    async with _agent(core, agent_key, adapter)() as sdk:
        agent = Agent(sdk, adapter, poll_interval=0.05, max_cycles=1)
        # The daemon fails the run, then lets an exception of the adapter out.
        with pytest.raises(RuntimeError):
            await agent.run_forever()

    [run] = await _runs(client, admin_key, task["id"])
    assert run["status"] == "failed"
    assert run["failureReason"] == "RuntimeError: the executor gave up"
    assert core.bad_gateways.count(f"POST /api/v1/runs/{run['id']}:fail") == 2
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["activeClaimId"] is None


async def test_a_core_down_longer_than_the_window_still_ends_the_run_honestly(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The window bounds the patience: past it the run is failed, not published."""
    admin_key, agent_key, task = await _setup(client)
    core = RestartingCore(app)
    core.down_for("POST", r"/runs/[^/]+:succeed$", 1000)
    adapter = NarratingAdapter(seconds=0.05)

    async with ControlPlaneClient(
        "http://testserver", agent_key, transport=core, retry_window=0.2
    ) as sdk:
        agent = Agent(sdk, adapter, poll_interval=0.05, max_cycles=1)
        await agent.run_forever()

    [run] = await _runs(client, admin_key, task["id"])
    assert run["status"] == "failed"
    assert "502" in run["failureReason"] or "http_error" in run["failureReason"]
