"""Setup before the executor: read at the base, run in the copy, reported (TASK-001273).

The daemon's side is in ``tests/client/test_agent_setup.py``; here are the
pieces: ``setup`` read at the base and not the branch, the command as a
process (exit code, time limit, environment, what the log shows) and the
reason a person reads when it fails.
"""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.checks import FAILED, PASSED, REDACTED, TIMED_OUT, CheckResult
from control_plane_agent.conventions import RUNNER_CONFIG_INVALID
from control_plane_agent.runner_config import RunnerConfig
from control_plane_agent.setup_command import (
    MAX_ERROR_LINE_CHARS,
    SetupPlan,
    error_line,
    run_setup,
    setup_at_base,
    setup_failure,
)
from control_plane_agent.workspace import Conventions, Workspace, WorkspaceBlocked


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(path: Path, base: str = "", **fields: Any) -> Workspace:
    path.mkdir(parents=True, exist_ok=True)
    return Workspace(
        key="TASK-1", branch="task/TASK-1", path=path, base_commit=base, reused=False, **fields
    )


@pytest.fixture
def setup_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_agent.setup"), "disabled", False)
    caplog.set_level(logging.INFO, logger="control_plane_agent.setup")
    return caplog


def _repo(path: Path, runner_yaml: str | None) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return _git(path, "rev-parse", "HEAD")


# --- setup at the base --------------------------------------------------------


def test_setup_is_read_at_the_base_not_the_branch(tmp_path: Path) -> None:
    copy_path = tmp_path / "copy"
    base = _repo(copy_path, "version: 1\nsetup: make install\n")
    (copy_path / ".agents" / "runner.yaml").write_text("version: 1\nsetup: 'true'\n")
    _git(copy_path, "commit", "-qam", "the task edits its setup")
    head = _git(copy_path, "rev-parse", "HEAD")
    plan = setup_at_base(_workspace(copy_path, head, conventions_base=base))
    assert plan == SetupPlan(revision=base, run="make install")


def test_the_base_revision_wins_over_the_conventions_base(tmp_path: Path) -> None:
    copy_path = tmp_path / "copy"
    first = _repo(copy_path, "version: 1\nsetup: echo old\n")
    (copy_path / ".agents" / "runner.yaml").write_text("version: 1\nsetup: echo new\n")
    _git(copy_path, "commit", "-qam", "newer base")
    newer = _git(copy_path, "rev-parse", "HEAD")
    plan = setup_at_base(_workspace(copy_path, newer, base_revision=first, conventions_base=newer))
    assert plan == SetupPlan(revision=first, run="echo old")


def test_no_runner_yaml_is_no_setup(tmp_path: Path) -> None:
    base = _repo(tmp_path / "copy", None)
    assert setup_at_base(_workspace(tmp_path / "copy", base, conventions_base=base)) is None


def test_runner_yaml_without_setup_is_no_setup(tmp_path: Path) -> None:
    base = _repo(tmp_path / "copy", "version: 1\n")
    assert setup_at_base(_workspace(tmp_path / "copy", base, conventions_base=base)) is None


def test_an_unknown_base_runs_no_setup_rather_than_the_branchs(
    tmp_path: Path, setup_log: pytest.LogCaptureFixture
) -> None:
    # The head of a branch taken over without the record of its base would
    # let the task pick its own setup (FR-009): nothing is run instead.
    copy_path = tmp_path / "copy"
    _repo(copy_path, "version: 1\nsetup: make install\n")
    head = _git(copy_path, "rev-parse", "HEAD")
    assert setup_at_base(_workspace(copy_path, head)) is None
    assert "setup is not run" in setup_log.text


@pytest.mark.parametrize(
    "runner_yaml, path",
    [
        ("version: 1\nsetup: ''\n", "$.setup"),
        ("version: 1\nsetup: [make, install]\n", "$.setup"),
        ("version: 1\nsetup: 7\n", "$.setup"),
        ("version: 1\nsetup: null\n", "$.setup"),
    ],
)
def test_an_invalid_setup_blocks(tmp_path: Path, runner_yaml: str, path: str) -> None:
    base = _repo(tmp_path / "copy", runner_yaml)
    with pytest.raises(WorkspaceBlocked) as caught:
        setup_at_base(_workspace(tmp_path / "copy", base, conventions_base=base))
    assert caught.value.code == RUNNER_CONFIG_INVALID
    assert path in caught.value.reason


def test_a_catalog_run_uses_the_conventions_it_was_prepared_by(tmp_path: Path) -> None:
    config = RunnerConfig(version=1, setup="uv sync --frozen")
    ws = _workspace(tmp_path / "copy", conventions=Conventions(revision="abc", config=config))
    assert setup_at_base(ws) == SetupPlan("abc", "uv sync --frozen")  # no git read
    bare = _workspace(tmp_path / "copy2", conventions=Conventions(revision="def"))
    assert setup_at_base(bare) is None


# --- running it -------------------------------------------------------------


async def test_setup_runs_in_the_root_of_the_copy(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    result = await run_setup(SetupPlan("abc", "pwd; touch installed"), ws)
    assert (result.status, result.exit_code) == (PASSED, 0)
    assert result.output.strip() == str(ws.path.resolve())
    assert (ws.path / "installed").exists()


async def test_setup_sees_the_run_environment_and_not_the_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_should_not_leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    result = await run_setup(
        SetupPlan("abc", 'echo "${CONTROL_PLANE_API_KEY-u} ${GITHUB_TOKEN-u} ${DB_URL-u}"'),
        _workspace(tmp_path / "copy"),
        {"DB_URL": "postgresql://h:5432/x"},
    )
    assert result.output.strip() == "u u postgresql://h:5432/x"


async def test_a_failing_setup_keeps_its_exit_code(tmp_path: Path) -> None:
    result = await run_setup(
        SetupPlan("abc", "echo resolving; echo 'error: no lock file' >&2; exit 3"),
        _workspace(tmp_path / "copy"),
    )
    assert (result.status, result.exit_code, result.passed) == (FAILED, 3, False)
    assert "error: no lock file" in result.output


async def test_setup_over_its_limit_is_stopped(tmp_path: Path) -> None:
    result = await run_setup(
        SetupPlan("abc", "sleep 30"), _workspace(tmp_path / "copy"), time_limit=0.3
    )
    assert (result.status, result.exit_code) == (TIMED_OUT, None)
    assert result.duration_seconds < 10


async def test_the_log_hides_secrets_and_local_paths(
    tmp_path: Path, setup_log: pytest.LogCaptureFixture
) -> None:
    ws = _workspace(tmp_path / "copy")
    result = await run_setup(
        SetupPlan("abc", 'echo "pw=$DB_PASSWORD at $(pwd)"; exit 1'),
        ws,
        {"DB_PASSWORD": "s3cret-password"},
        secrets=("s3cret-password",),
    )
    assert result.status == FAILED
    assert "setup of TASK-1: failed" in setup_log.text
    assert f"pw={REDACTED}" in setup_log.text
    assert "s3cret-password" not in setup_log.text
    assert str(tmp_path) not in setup_log.text


async def test_two_setups_at_once_in_their_own_copies(tmp_path: Path) -> None:
    first, second = _workspace(tmp_path / "a"), _workspace(tmp_path / "b")
    results = await asyncio.gather(
        run_setup(SetupPlan("abc", "sleep 0.2; touch done"), first),
        run_setup(SetupPlan("abc", "sleep 0.2; touch done"), second),
    )
    assert [r.status for r in results] == [PASSED, PASSED]
    assert (first.path / "done").exists() and (second.path / "done").exists()


async def test_setup_may_run_again_on_the_same_copy(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    plan = SetupPlan("abc", "mkdir -p .venv && echo x >> .venv/runs")
    for _ in range(2):
        assert (await run_setup(plan, ws)).passed
    assert (ws.path / ".venv" / "runs").read_text() == "x\nx\n"


# --- the reason -------------------------------------------------------------


def _failed(output: str, status: str = FAILED, code: int | None = 2) -> CheckResult:
    return CheckResult("setup", "make install", status, code, 1.0, output=output)


def test_the_reason_names_the_command_the_exit_and_the_first_error_line() -> None:
    output = "uv sync --frozen\nerror: lock file out of date\nmake: *** [install] Error 2\n"
    reason = setup_failure(SetupPlan("0123456789abcdef", "make install"), _failed(output))
    assert reason.startswith(
        "setup `make install` of .agents/runner.yaml at 0123456789ab exited 2: "
        "error: lock file out of date."
    )
    assert "\n" not in reason


def test_the_reason_of_a_timeout() -> None:
    output = "installing\n\n[stopped after 1800 s]"
    reason = setup_failure(SetupPlan("abc", "make install"), _failed(output, TIMED_OUT, None))
    assert "timed out: installing." in reason


def test_the_reason_hides_secrets_and_local_paths(tmp_path: Path) -> None:
    output = f"error: cannot reach postgresql://u:s3cret-password@db in {tmp_path}/x\n"
    reason = setup_failure(
        SetupPlan("abc", "make install"), _failed(output), secrets=("s3cret-password",)
    )
    assert "s3cret-password" not in reason
    assert str(tmp_path) not in reason


@pytest.mark.parametrize(
    "output, line",
    [
        ("", "no output"),
        ("\n  \n", "no output"),
        ("[stopped after 5 s]", "no output"),
        ("one\ntwo\n", "two"),
        (
            "Resolved 3 packages\nERROR: no matching distribution\nexit\n",
            "ERROR: no matching distribution",
        ),
        ("npm ERR! code E404\nnpm ERR! 404 Not Found\n", "npm ERR! code E404"),
        ("   error: spaced   \n", "error: spaced"),
    ],
)
def test_the_error_line(output: str, line: str) -> None:
    assert error_line(output) == line


def test_the_error_line_is_bounded() -> None:
    assert len(error_line("error: " + "x" * 5000)) == MAX_ERROR_LINE_CHARS
