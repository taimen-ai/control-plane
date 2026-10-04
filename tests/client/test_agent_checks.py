"""Checks before hand-in through the whole daemon (universal-runner U014).

With ``checks`` on, the daemon runs the ``checks`` of ``.agents/runner.yaml``
at the task's base after the adapter; a failed one gives the adapter one more
turn with its output, and the result goes into ``metadata.checks`` of the
``commit`` artifact — green, fixed or still red. Off, the cycle is what it
was. The adapter is a fake that writes files in its copy; the checks are real
shell commands in that copy.
"""

import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.blocked import CHECKPOINT_KIND as BLOCKED_KIND
from control_plane_agent.checks import CHECK_ACTION
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap

RUNNER_YAML = """\
version: 1
checks:
  - {name: lint, run: "test -f work.txt"}
  - {name: tests, run: "echo FAILED test_$((6*7)) >&2; test -f fixed.txt"}
"""
COMMIT_FIELDS = {"branch", "commit", "workspaceKey", "published"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin(path: Path, runner_yaml: str | None = RUNNER_YAML) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


class FixingAdapter:
    """Does the work on the first turn; on a turn with ``failedChecks`` maybe fixes it."""

    def __init__(self, *, fixes: bool = False, work: bool = True, block_on_fix: bool = False):
        self.fixes = fixes
        self.work = work
        self.block_on_fix = block_on_fix
        self.turns: list[dict[str, Any]] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        self.turns.append(task)
        if "failedChecks" not in task:
            if self.work:
                (workspace.path / "work.txt").write_text("done\n")
            return [ArtifactSpec(type="report", name="summary", content={"summary": "Did it."})]
        if self.block_on_fix:
            await client.create_checkpoint(
                str(run["id"]), kind=BLOCKED_KIND, data={"reason": "cannot fix the suite"}
            )
        if self.fixes:
            (workspace.path / "fixed.txt").write_text("fixed\n")
        return [ArtifactSpec(type="report", name="fix summary", content={"summary": "Tried."})]


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
    sdk: Make,
    key: str,
    adapter: FixingAdapter,
    pool: ExecutionWorkspacePool,
    *,
    checks: bool,
    **options: Any,
) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=1,
            workspaces=pool,
            checks=checks,
            **options,
        )
        await agent.run_forever()


async def _outcome(client: httpx.AsyncClient, s: dict[str, Any]) -> dict[str, Any]:
    task_id = s["task"]["id"]
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task_id))["items"]
    [run] = (await _get(client, s["admin"], "/runs", taskId=task_id))["items"]
    return {
        "task": await _get(client, s["admin"], f"/tasks/{task_id}"),
        "run": run,
        "commits": [a for a in artifacts if a["type"] == "commit"],
        "reports": [a for a in artifacts if a["type"] == "report"],
        "comments": (await _get(client, s["admin"], f"/tasks/{task_id}/comments"))["items"],
        "actions": (await _get(client, s["admin"], f"/runs/{run['id']}/actions"))["items"],
    }


async def test_green_checks_are_in_the_commit_metadata(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    origin = _origin(tmp_path / "origin", RUNNER_YAML.replace("fixed.txt", "work.txt"))
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = FixingAdapter()

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    assert len(adapter.turns) == 1  # green: no attempt to fix
    [commit] = out["commits"]
    metadata = commit["metadata"]
    # The fields a commit always had are there, unchanged.
    assert set(metadata) >= COMMIT_FIELDS
    assert metadata["commit"] == commit["uri"].removeprefix("git:")
    checks = metadata["checks"]
    assert checks["status"] == "passed"
    assert checks["fixAttempted"] is False
    assert "firstResults" not in checks
    assert checks["revision"] == _git(origin, "rev-parse", "main")
    assert [(r["name"], r["status"], r["exitCode"]) for r in checks["results"]] == [
        ("lint", "passed", 0),
        ("tests", "passed", 0),
    ]
    assert all(r["durationSeconds"] >= 0 for r in checks["results"])
    assert out["run"]["output"] == {"checks": checks}
    assert out["comments"] == []
    # Each check was an action of the run while it went.
    ran = [a for a in out["actions"] if a["action"] == CHECK_ACTION]
    assert [a["status"] for a in ran] == ["completed", "completed"]
    # The output of a check is not durable state.
    assert "FAILED test_42" not in str(commit)


async def test_red_check_gets_one_attempt_and_is_handed_in_fixed(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    origin = _origin(tmp_path / "origin")
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = FixingAdapter(fixes=True)

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    assert len(adapter.turns) == 2
    [failed] = adapter.turns[1]["failedChecks"]
    assert (failed["name"], failed["run"], failed["status"]) == (
        "tests",
        "echo FAILED test_$((6*7)) >&2; test -f fixed.txt",
        "failed",
    )
    assert failed["exitCode"] == 1
    assert "FAILED test_42" in failed["output"]  # stderr reaches the executor
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    checks = out["commits"][0]["metadata"]["checks"]
    assert (checks["status"], checks["fixAttempted"]) == ("passed", True)
    assert [r["status"] for r in checks["firstResults"]] == ["passed", "failed"]
    assert [r["status"] for r in checks["results"]] == ["passed", "passed"]
    # The fix is part of the commit handed in, and both reports went out.
    branch = "task/" + s["task"]["publicId"]
    changed = _git(origin, "show", "--name-only", "--format=", branch).splitlines()
    assert {"fixed.txt", "work.txt"} <= set(changed)
    assert {r["name"] for r in out["reports"]} == {"summary", "fix summary"}
    assert out["comments"] == []


async def test_red_after_the_attempt_is_handed_in_with_the_failure_explicit(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin"), tmp_path / "w")
    adapter = FixingAdapter(fixes=False)

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    assert len(adapter.turns) == 2  # one attempt, not more
    out = await _outcome(client, s)
    # Handed in, not failed: the review sees the red check and decides.
    assert out["task"]["status"] == "done"
    assert out["run"]["status"] == "succeeded"
    checks = out["commits"][0]["metadata"]["checks"]
    assert (checks["status"], checks["fixAttempted"]) == ("failed", True)
    assert [(r["name"], r["status"], r["exitCode"]) for r in checks["results"]] == [
        ("lint", "passed", 0),
        ("tests", "failed", 1),
    ]
    assert out["run"]["output"]["checks"]["status"] == "failed"
    [comment] = out["comments"]
    assert "failed after one attempt to fix them: tests (exit 1)" in comment["body"]
    ran = [a for a in out["actions"] if a["action"] == CHECK_ACTION]
    assert [a["status"] for a in ran] == ["completed", "failed", "completed", "failed"]


async def test_checks_off_is_the_cycle_as_before(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin"), tmp_path / "w")
    adapter = FixingAdapter()

    await _work(sdk, s["agent"], adapter, pool, checks=False)

    assert len(adapter.turns) == 1
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    [commit] = out["commits"]
    assert "checks" not in commit["metadata"]
    assert set(commit["metadata"]) >= COMMIT_FIELDS
    assert out["run"]["output"] is None
    assert not [a for a in out["actions"] if a["action"] == CHECK_ACTION]
    assert out["comments"] == []


async def test_no_checks_declared_says_so(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", None), tmp_path / "w")
    adapter = FixingAdapter()

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    assert len(adapter.turns) == 1
    out = await _outcome(client, s)
    checks = out["commits"][0]["metadata"]["checks"]
    assert (checks["status"], checks["results"]) == ("none", [])
    assert out["comments"] == []


async def test_nothing_to_commit_still_reports_the_checks_on_the_run(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin"), tmp_path / "w")
    adapter = FixingAdapter(work=False)

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    out = await _outcome(client, s)
    assert out["commits"] == []
    assert out["run"]["output"]["checks"]["status"] == "failed"
    assert len(out["comments"]) == 1


async def test_invalid_runner_yaml_blocks_before_the_executor_works(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    origin = _origin(tmp_path / "origin", "version: 1\nchecks:\n  - {name: lint}\n")
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = FixingAdapter()

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    assert adapter.turns == []
    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == "runner_config_invalid"
    assert "$.checks[0].run" in out["run"]["output"]["reason"]


async def test_invalid_runner_yaml_with_checks_off_still_blocks_for_setup(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    # With checks off the file is still read, for its setup (TASK-001273): a
    # file that cannot be used tells nothing about what to install.
    s = await _setup(client)
    origin = _origin(tmp_path / "origin", "version: 1\nchecks:\n  - {name: lint}\n")
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = FixingAdapter()

    await _work(sdk, s["agent"], adapter, pool, checks=False)

    assert adapter.turns == []
    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == "runner_config_invalid"


async def test_executor_blocked_on_the_fix_attempt_hands_nothing_in(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin"), tmp_path / "w")
    adapter = FixingAdapter(block_on_fix=True)

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    assert len(adapter.turns) == 2
    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == "executor_blocked"
    assert out["commits"] == []
    # Reports of both turns go out, as for any blocked run.
    assert {r["name"] for r in out["reports"]} == {"summary", "fix summary"}


async def test_a_spent_budget_fails_the_checks_instead_of_waiting(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    # 1800 s per check and two rounds used to be the only bound; now all the
    # checks of a hand-in share one budget, and what it cuts is a failure.
    s = await _setup(client)
    slow = "version: 1\nchecks:\n  - {name: slow, run: 'sleep 30'}\n  - {name: next, run: 'true'}\n"
    pool = ExecutionWorkspacePool(_origin(tmp_path / "origin", slow), tmp_path / "w")
    adapter = FixingAdapter(fixes=True)

    started = time.monotonic()
    await _work(sdk, s["agent"], adapter, pool, checks=True, checks_budget=0.5)

    assert time.monotonic() - started < 20
    # Nothing could tell a fix worked: no attempt to make one.
    assert len(adapter.turns) == 1
    out = await _outcome(client, s)
    assert out["task"]["status"] == "done"
    checks = out["commits"][0]["metadata"]["checks"]
    assert (checks["status"], checks["fixAttempted"]) == ("failed", False)
    assert [(r["name"], r["status"], r["exitCode"]) for r in checks["results"]] == [
        ("slow", "timed_out", None),
        ("next", "not_run", None),
    ]
    [comment] = out["comments"]
    assert "slow (timed out), next (not run)" in comment["body"]
    # A check never started is no action of the run.
    ran = [a for a in out["actions"] if a["action"] == CHECK_ACTION]
    assert [a["status"] for a in ran] == ["failed"]


async def test_what_the_checks_leave_in_the_copy_is_not_committed(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    messy = (
        "version: 1\nchecks:\n"
        "  - {name: report, run: 'mkdir -p reports && echo x > reports/junit.xml'}\n"
        "  - {name: fmt, run: 'echo formatted > work.txt && rm README.md'}\n"
        "  - {name: tests, run: 'test -f fixed.txt'}\n"
    )
    origin = _origin(tmp_path / "origin", messy)
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")
    adapter = FixingAdapter(fixes=True)

    await _work(sdk, s["agent"], adapter, pool, checks=True)

    # The fix attempt starts from the executor's work, not from what the
    # first round of checks left.
    assert len(adapter.turns) == 2
    out = await _outcome(client, s)
    assert out["commits"][0]["metadata"]["checks"]["status"] == "passed"
    branch = "task/" + s["task"]["publicId"]
    changed = _git(origin, "show", "--name-only", "--format=", branch).splitlines()
    assert sorted(changed) == ["fixed.txt", "work.txt"]
    assert _git(origin, "show", f"{branch}:work.txt") == "done"
    assert _git(origin, "show", f"{branch}:README.md") == "x"


async def test_a_check_that_moves_head_blocks_and_hands_nothing_in(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    committing = (
        "version: 1\nchecks:\n"
        "  - {name: sneaky, run: 'git -c user.name=t -c user.email=t@t "
        "commit -q --allow-empty -m by-a-check'}\n"
    )
    origin = _origin(tmp_path / "origin", committing)
    pool = ExecutionWorkspacePool(origin, tmp_path / "w")

    await _work(sdk, s["agent"], FixingAdapter(), pool, checks=True)

    out = await _outcome(client, s)
    assert out["task"]["systemStatusCategory"] == "blocked"
    assert out["run"]["failureReason"] == "checks_moved_head"
    assert out["commits"] == []
    # The daemon committed nothing on top of what the check did.
    branch = "task/" + s["task"]["publicId"]
    assert _git(origin, "log", "--format=%s", branch).splitlines() == ["by-a-check", "base"]
