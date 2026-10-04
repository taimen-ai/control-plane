"""Test services of a run through the whole daemon (universal-runner U013).

The daemon reads ``services`` of ``.agents/runner.yaml`` at the task's base,
asks the node's listener for them before the executor starts and hands the
filled ``env`` to the executor process of this run only. The listener is the
fake of ``tests/unit/test_agent_services.py``, held to the pinned OpenAPI of
the fleet; the executor is the real Claude Code adapter over a fake ``claude``
that writes down what it sees; the service is a real TCP socket, so the wait
for the connection is real too.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane_agent.main import Agent
from control_plane_agent.services import (
    FleetServicesClient,
    RunServices,
    RunServicesSource,
    ServicesSettings,
)
from control_plane_agent.supervision import SupervisionSettings
from control_plane_agent.workspace import ExecutionWorkspacePool
from control_plane_claude.adapter import ClaudeCodeAdapter
from control_plane_claude.cli import ClaudeCodeCLI
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap
from tests.unit.test_agent_services import PASSWORD, TOKEN, Clock, FakeListener

VARIABLE = "SVC_DATABASE_URL"
RUNNER_YAML = f"""\
version: 1
services:
  db:
    template: postgres-16
    env:
      {VARIABLE}: "postgresql://{{user}}:{{password}}@{{host}}:{{port}}/{{database}}"
"""
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 1,
    "result": "Looked at the database.",
    "session_id": "s",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin(path: Path, runner_yaml: str | None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _claude(tmp_path: Path) -> tuple[ClaudeCodeAdapter, Path]:
    """The real adapter over a ``claude`` that appends what it sees of the variable."""
    seen = tmp_path / "seen.txt"
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "${{{VARIABLE}-<unset>}}" >> "{seen}"\n'
        "cat > /dev/null\n"
        f"cat <<'JSON'\n{json.dumps(RESULT)}\nJSON\n"
    )
    script.chmod(0o755)
    cli = ClaudeCodeCLI(binary=str(script), log_dir=tmp_path / "logs")
    return ClaudeCodeAdapter(cli), seen


@pytest.fixture
async def database() -> AsyncIterator[int]:
    """A socket that accepts connections, standing in for the service."""
    server = await asyncio.start_server(lambda r, w: w.close(), "localhost", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]


def _services(tmp_path: Path, port: int, **listener: Any) -> tuple[RunServicesSource, FakeListener]:
    clock = Clock()
    token_file = tmp_path / "services-token"
    token_file.write_text(TOKEN)
    fake = FakeListener(
        clock, token_file=token_file, ready_host="localhost", ready_port=port, **listener
    )
    settings = ServicesSettings(
        url="http://fleet-services:8050", token_file=token_file, agent_key="coder", replica=1
    )
    client = FleetServicesClient(
        settings, transport=fake.transport(), sleep=clock.sleep, clock=clock
    )
    return RunServicesSource(client), fake


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="coder", permissions=RUNNER_PERMISSIONS
    )
    return {"admin": admin_key, "agent": agent_key}


async def _get(client: httpx.AsyncClient, key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params or None, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _work(
    sdk: Make,
    key: str,
    adapter: Any,
    pool: ExecutionWorkspacePool,
    services: RunServicesSource,
    cycles: int = 2,
) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=cycles,
            workspaces=pool,
            services=services,
        )
        await agent.run_forever()


async def _durable_text(client: httpx.AsyncClient, admin: str, task_id: str) -> str:
    """Everything the run left in the core: runs, checkpoints, artifacts, comments."""
    runs = (await _get(client, admin, "/runs", taskId=task_id))["items"]
    parts: list[Any] = [runs]
    for run in runs:
        parts.append(await _get(client, admin, f"/runs/{run['id']}/checkpoints"))
    parts.append(await _get(client, admin, "/artifacts", taskId=task_id))
    parts.append(await _get(client, admin, f"/tasks/{task_id}/comments"))
    return json.dumps(parts)


async def test_service_address_reaches_the_executor_of_this_run_only(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["pending", "ready"])
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    await _work(sdk, s["agent"], adapter, pool, services)

    url = f"postgresql://test:{PASSWORD}@localhost:{database}/test"
    assert seen.read_text().splitlines() == [url]
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["status"] == "done"
    # The services were let go when the run ended, and only then.
    [request_id] = fake.requests
    assert fake.signals[-1] == ("release", request_id)
    # Neither the password nor the token is anywhere durable.
    durable = await _durable_text(client, s["admin"], task["id"])
    assert PASSWORD not in durable and TOKEN not in durable

    # The next run of a repository without services sees nothing of it.
    other = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "plain", None), tmp_path / "w2")
    await create_task(client, s["admin"], title="Needs nothing")
    await _work(sdk, s["agent"], adapter, other, services)
    assert seen.read_text().splitlines() == [url, "<unset>"]
    assert len(fake.bodies) == 1


async def test_timeout_sends_the_task_back_to_the_queue(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database)  # pending until the wait runs out
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    await _work(sdk, s["agent"], adapter, pool, services, cycles=1)

    assert not seen.exists()  # the executor never started
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]  # the request was withdrawn
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "test_services_unavailable")
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["activeClaimId"] is None
    assert record["systemStatusCategory"] != "blocked"
    # Back in the queue: the next cycle takes it again, and this time it works.
    fake.polls = ["ready"]
    await _work(sdk, s["agent"], adapter, pool, services)
    assert seen.read_text().count("localhost") == 1
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"


@pytest.mark.parametrize("reason", ["service_template_unavailable", "service_quota_exceeded"])
async def test_unknown_template_sends_the_task_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int, reason: str
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, _ = _services(tmp_path, database, polls=[f"rejected:{reason}"])
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    await _work(sdk, s["agent"], adapter, pool, services)

    assert not seen.exists()
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert (record["status"], record["systemStatusCategory"]) == ("blocked", "blocked")
    assert record["activeClaimId"] is None
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", reason)
    assert "postgres-16" in run["output"]["reason"]
    [comment] = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
    assert reason in comment["body"]
    assert str(tmp_path) not in comment["body"] + run["output"]["reason"]


async def test_runner_without_a_listener_sends_the_task_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    await _work(sdk, s["agent"], adapter, pool, RunServicesSource(None))

    assert not seen.exists()
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["systemStatusCategory"] == "blocked"
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert run["failureReason"] == "test_services_not_offered"


async def test_host_mode_runs_without_asking(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Uses the host's database")

    await _work(sdk, s["agent"], adapter, pool, RunServicesSource(None, host=True))

    assert seen.read_text().splitlines() == ["<unset>"]
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"


# -- the wait for the services is supervised ------------------------------------------

FAST = SupervisionSettings(poll_seconds=0.05, stall_warn_seconds=0.01, stall_stop_seconds=0.02)


def _hanging(source: RunServicesSource, fake: FakeListener) -> tuple[asyncio.Event, asyncio.Event]:
    """Long polls that hold until ``go`` is set; ``waiting`` is set by the first."""
    waiting, go = asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            waiting.set()
            await go.wait()
        return fake.handle(request)

    assert source.client is not None
    source.client._transport = httpx.MockTransport(handle)
    return waiting, go


async def test_a_cancel_request_stops_the_wait_for_the_services(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["ready"])
    waiting, _ = _hanging(services, fake)
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="No longer needed")

    async with sdk(s["agent"]) as sdk_client:
        # The watchdog would stop a run this quiet at once; it is off for the wait.
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            services=services,
            supervision=FAST,
        )
        running = asyncio.ensure_future(agent.run_forever())
        await asyncio.wait_for(waiting.wait(), 10)
        [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
        asked = await client.post(
            f"/api/v1/runs/{run['id']}:request-cancel",
            json={"reason": "the premise is gone"},
            headers=auth(s["admin"]),
        )
        assert asked.status_code == 200, asked.text
        await asyncio.wait_for(running, 10)

    assert not seen.exists()  # the executor never started
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]  # the request was withdrawn
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("cancelled", "cancel_requested")
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert (record["activeClaimId"], record["status"]) == (None, "todo")
    # The copy was given back.
    pool.release(pool.acquire(task["publicId"]), "failed")


async def test_a_drain_stops_the_wait_for_the_services(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["ready"])
    waiting, _ = _hanging(services, fake)
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Long")

    async with sdk(s["agent"]) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=5,
            workspaces=pool,
            services=services,
            supervision=FAST,
            drain_seconds=0,
        )
        running = asyncio.ensure_future(agent.run_forever())
        await asyncio.wait_for(waiting.wait(), 10)
        agent.request_stop()
        await asyncio.wait_for(running, 10)

    assert not seen.exists()
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("cancelled", "drained")
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert (record["activeClaimId"], record["status"]) == (None, "todo")


async def test_a_drain_that_ran_out_as_the_services_came_stops_before_the_executor(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    """Ready between two looks of the supervisor: the drain is checked once more."""
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["ready"])
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Long")
    slow = SupervisionSettings(poll_seconds=30, stall_warn_seconds=0, stall_stop_seconds=0)

    async with sdk(s["agent"]) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            services=services,
            supervision=slow,
            drain_seconds=0,
        )
        assert services.client is not None
        handle = fake.handle

        def stopping(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                agent.request_stop()
            return handle(request)

        services.client._transport = httpx.MockTransport(stopping)
        await asyncio.wait_for(agent.run_forever(), 10)

    assert not seen.exists()
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("cancelled", "drained")


async def test_a_claim_lost_during_the_wait_starts_nothing(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["ready"])
    waiting, go = _hanging(services, fake)
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    async with sdk(s["agent"]) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            services=services,
            heartbeat_interval=0.05,
        )
        running = asyncio.ensure_future(agent.run_forever())
        await asyncio.wait_for(waiting.wait(), 10)
        record = await _get(client, s["admin"], f"/tasks/{task['id']}")
        await sdk_client.release_claim(str(record["activeClaimId"]), reason="taken away")
        await asyncio.sleep(0.5)  # a few heartbeats meet the released claim
        go.set()
        await asyncio.wait_for(running, 10)

    assert not seen.exists()
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]  # held, then let go unused
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    # Stopped on the lease, before anything wrote under the lost claim.
    assert (run["status"], run["failureReason"]) == ("failed", "lease_lost")
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] != "done"


class _BrokenRelease:
    """Services whose release breaks: the copy must be given back all the same."""

    async def open(self, workspace: Any, *, takes_env: bool) -> RunServices:
        async def release() -> None:
            raise RuntimeError("release broke")

        return RunServices(env={VARIABLE: "x"}, request_id="r", _release=release)


async def test_a_release_that_breaks_still_gives_the_copy_back(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", RUNNER_YAML), tmp_path / "w")
    adapter, _ = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    async with sdk(s["agent"]) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            services=_BrokenRelease(),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="release broke"):
            await agent.run_once()

    # Not held by the run that broke: the pool hands the copy out again.
    pool.release(pool.acquire(task["publicId"]), "failed")


async def test_setup_sees_the_services_and_their_password_stays_out_of_durable_state(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, database: int
) -> None:
    # Setup runs after the services are ready, in the environment of the run
    # (TASK-001273); a failing one that prints the password leaves it nowhere.
    s = await _setup(client)
    saw = tmp_path / "setup-saw.txt"
    runner_yaml = RUNNER_YAML + (
        f'setup: \'echo "${VARIABLE}" > {saw}; echo "error: cannot migrate ${VARIABLE}"; exit 1\'\n'
    )
    pool = ExecutionWorkspacePool(_origin(tmp_path / "forge" / "repo", runner_yaml), tmp_path / "w")
    services, fake = _services(tmp_path, database, polls=["ready"])
    adapter, seen = _claude(tmp_path)
    task = await create_task(client, s["admin"], title="Needs a database")

    await _work(sdk, s["agent"], adapter, pool, services, cycles=1)

    url = f"postgresql://test:{PASSWORD}@localhost:{database}/test"
    assert saw.read_text().splitlines() == [url]
    assert not seen.exists()  # the executor never started
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert run["failureReason"] == "setup_failed"
    # The value of the variable carries the password: all of it is hidden.
    assert "exited 1: error: cannot migrate ***." in run["output"]["reason"]
    [request_id] = fake.requests
    assert fake.signals[-1] == ("release", request_id)
    durable = await _durable_text(client, s["admin"], task["id"])
    assert PASSWORD not in durable and TOKEN not in durable
    assert str(tmp_path) not in durable
