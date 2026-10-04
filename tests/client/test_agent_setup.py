"""Setup before the executor through the whole daemon (TASK-001273, TAI-ADR-0063 §4).

The daemon runs ``setup`` of ``.agents/runner.yaml`` at the task's base in
the copy before the adapter: installed — the adapter works in an installed
copy; failed or timed out — the run fails ``setup_failed`` and the task goes
to a person with the first line of the error; no ``setup`` — the cycle is
what it was. The adapter is a fake; setup is a real shell command.
"""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.blocked import BLOCKED_COMMENT_PREFIX
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.setup_command import SETUP_ACTION, SETUP_FAILED
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap

INSTALLS = "version: 1\nsetup: mkdir -p .venv && echo ok >> .venv/installed\n"


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
    (path / ".gitignore").write_text(".venv/\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


class SeeingAdapter:
    """Records what setup left in the copy when it starts, then does the work."""

    def __init__(self) -> None:
        self.installed: list[str | None] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        marker = workspace.path / ".venv" / "installed"
        self.installed.append(marker.read_text() if marker.exists() else None)
        (workspace.path / "work.txt").write_text("done\n")
        return [ArtifactSpec(type="report", name="summary", content={"summary": "Did it."})]


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="coder", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Change a file")
    return {"admin": admin_key, "agent": agent_key, "task": task}


async def _get(client: httpx.AsyncClient, key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params or None, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _work(
    sdk: Make, key: str, adapter: SeeingAdapter, pool: ExecutionWorkspacePool, **options: Any
) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(
            sdk_client, adapter, poll_interval=0.05, max_cycles=1, workspaces=pool, **options
        )
        await agent.run_forever()


async def _outcome(client: httpx.AsyncClient, s: dict[str, Any]) -> dict[str, Any]:
    task_id = s["task"]["id"]
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task_id))["items"]
    runs = (await _get(client, s["admin"], "/runs", taskId=task_id))["items"]
    run = max(runs, key=lambda r: r["createdAt"])
    return {
        "task": await _get(client, s["admin"], f"/tasks/{task_id}"),
        "run": run,
        "runs": runs,
        "commits": [a for a in artifacts if a["type"] == "commit"],
        "comments": (await _get(client, s["admin"], f"/tasks/{task_id}/comments"))["items"],
        "actions": (await _get(client, s["admin"], f"/runs/{run['id']}/actions"))["items"],
    }


async def _return_to_work(client: httpx.AsyncClient, admin: str, task_id: str) -> None:
    record = await _get(client, admin, f"/tasks/{task_id}")
    patched = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "todo"},
        headers={**auth(admin), "If-Match": f'"task-{record["version"]}"'},
    )
    assert patched.status_code == 200, patched.text


async def test_setup_runs_before_the_executor(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", INSTALLS), tmp_path / "w")
    adapter = SeeingAdapter()

    await _work(sdk, s["agent"], adapter, pool)

    assert adapter.installed == ["ok\n"]  # the executor found the copy installed
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    ran = [a for a in out["actions"] if a["action"] == SETUP_ACTION]
    assert [a["status"] for a in ran] == ["completed"]
    [commit] = out["commits"]
    # What setup installed is ignored by the repository: only the work goes in.
    files = _git(tmp_path / "origin", "ls-tree", "-r", "--name-only", commit["uri"][4:])
    assert ".venv/installed" not in files.split() and "work.txt" in files.split()
    assert out["comments"] == []


async def test_failed_setup_blocks_with_the_first_line_of_the_error(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    runner_yaml = (
        "version: 1\nsetup: \"echo Resolving; echo 'error: lock file is stale' >&2; exit 4\"\n"
    )
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", runner_yaml), tmp_path / "w")
    adapter = SeeingAdapter()

    await _work(sdk, s["agent"], adapter, pool)

    assert adapter.installed == []  # the executor never started
    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == SETUP_FAILED
    reason = out["run"]["output"]["reason"]
    assert "exited 4: error: lock file is stale." in reason
    assert str(tmp_path) not in reason
    [comment] = out["comments"]
    assert comment["body"].startswith(f"{BLOCKED_COMMENT_PREFIX} ({SETUP_FAILED}): setup `")
    assert "error: lock file is stale" in comment["body"].splitlines()[0]
    assert out["commits"] == []
    ran = [a for a in out["actions"] if a["action"] == SETUP_ACTION]
    assert [a["status"] for a in ran] == ["failed"]


async def test_setup_over_its_time_blocks(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    origin = _origin(tmp_path / "origin", "version: 1\nsetup: echo fetching; sleep 30\n")
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = SeeingAdapter()

    await _work(sdk, s["agent"], adapter, pool, setup_timeout=0.5)

    assert adapter.installed == []
    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == SETUP_FAILED
    assert "timed out: fetching." in out["run"]["output"]["reason"]


async def _without_setup(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path, runner_yaml: str | None
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", runner_yaml), tmp_path / "w")
    adapter = SeeingAdapter()

    await _work(sdk, s["agent"], adapter, pool)

    assert adapter.installed == [None]
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    assert not [a for a in out["actions"] if a["action"] == SETUP_ACTION]
    assert len(out["commits"]) == 1
    assert out["comments"] == []


async def test_no_runner_yaml_is_the_cycle_as_before(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    await _without_setup(client, sdk, tmp_path, None)


async def test_runner_yaml_without_setup_is_the_cycle_as_before(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    await _without_setup(client, sdk, tmp_path, "version: 1\nchecks:\n  - {name: x, run: 'true'}\n")


async def test_a_returned_task_runs_setup_again_on_its_copy(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    # Setup failed for the environment, not the base (a tool missing on the
    # node: here a file outside the copy); a person fixes it and returns the
    # task. The next run of the same copy runs setup again.
    s = await _setup(client)
    needed = tmp_path / "tool"
    runner_yaml = (
        f'version: 1\nsetup: "test -f {needed} || {{ echo error: no tool; exit 1; }}; '
        'mkdir -p .venv && echo ok >> .venv/installed"\n'
    )
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", runner_yaml), tmp_path / "w")
    adapter = SeeingAdapter()

    await _work(sdk, s["agent"], adapter, pool)
    out = await _outcome(client, s)
    assert out["run"]["failureReason"] == SETUP_FAILED
    assert str(tmp_path) not in out["run"]["output"]["reason"]  # the command names a path

    needed.write_text("")
    await _return_to_work(client, s["admin"], s["task"]["id"])
    await _work(sdk, s["agent"], adapter, pool)

    assert adapter.installed == ["ok\n"]
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    assert len(out["runs"]) == 2


async def test_a_claim_lost_during_setup_starts_nothing(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    started, go = tmp_path / "started", tmp_path / "go"
    runner_yaml = (
        f'version: 1\nsetup: "touch {started}; while [ ! -f {go} ]; do sleep 0.05; done"\n'
    )
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", runner_yaml), tmp_path / "w")
    adapter = SeeingAdapter()

    async with sdk(s["agent"]) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            heartbeat_interval=0.05,
        )
        running = asyncio.ensure_future(agent.run_forever())
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists()
        record = await _get(client, s["admin"], f"/tasks/{s['task']['id']}")
        await sdk_client.release_claim(str(record["activeClaimId"]), reason="taken away")
        await asyncio.sleep(0.5)  # a few heartbeats meet the released claim
        go.write_text("")
        await asyncio.wait_for(running, 10)

    assert adapter.installed == []
    out = await _outcome(client, s)
    assert (out["run"]["status"], out["run"]["failureReason"]) == ("failed", "lease_lost")
    assert out["task"]["status"] != "done"
