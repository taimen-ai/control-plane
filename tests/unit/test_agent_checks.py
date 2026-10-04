"""Checks before hand-in: running them, reading them at the base, reporting them (U014).

The daemon's side is in ``tests/client/test_agent_checks.py``; here are the
pieces: one check as a process (exit code, output, time limit, environment,
what is hidden), the list read at the base and not the branch, the metadata
of the ``commit`` artifact, the prompt of the fix attempt and the switch in
the agent's description.
"""

import asyncio
import copy
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from control_plane_agent.catalog import RepositoryCatalog
from control_plane_agent.checks import (
    CHECKS_BASE_UNKNOWN,
    CHECKS_MOVED_HEAD,
    CHECKS_RESTORE_FAILED,
    FAILED,
    KEPT_PROXY_NAMES,
    KEPT_RESERVED_ENTRIES,
    MAX_OUTPUT_CHARS,
    NONE,
    NOT_RUN,
    PASSED,
    REDACTED,
    TIMED_OUT,
    CheckResult,
    ChecksBudget,
    ChecksPlan,
    ChecksReport,
    check_environment,
    checks_at_base,
    is_secret_name,
    is_secret_value,
    not_run,
    redact,
    run_check,
    secrets_of,
)
from control_plane_agent.conventions import RUNNER_CONFIG_INVALID
from control_plane_agent.instructions import (
    CHECKS_HEADING,
    build_prompt,
    render_failed_checks,
)
from control_plane_agent.main import Agent, record_failure_tolerated
from control_plane_agent.revision import AgentRevision, RevisionError, settings_of
from control_plane_agent.runner_config import (
    RESERVED_ENV_NAMES,
    Check,
    RunnerConfig,
    reserved_entry,
)
from control_plane_agent.workspace import (
    Conventions,
    Workspace,
    WorkspaceBlocked,
    WorkspaceError,
)
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    PermissionDeniedError,
    StaleClaimError,
    TransportError,
)
from control_plane_client.errors import RunNotActiveError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"


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


# --- one check ---------------------------------------------------------------


async def test_a_passing_check(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    (ws.path / "marker").write_text("here\n")
    result = await run_check(Check("lint", "cat marker; pwd"), ws)
    assert (result.status, result.exit_code, result.passed) == (PASSED, 0, True)
    assert result.output.splitlines() == ["here", str(ws.path.resolve())]
    assert result.duration_seconds >= 0


async def test_a_failing_check_keeps_its_exit_code_and_both_streams(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    result = await run_check(Check("tests", "echo out; echo err >&2; exit 3"), ws)
    assert (result.status, result.exit_code, result.passed) == (FAILED, 3, False)
    assert result.output.splitlines() == ["out", "err"]


async def test_an_empty_command_output_is_empty(tmp_path: Path) -> None:
    result = await run_check(Check("noop", "true"), _workspace(tmp_path / "copy"))
    assert (result.status, result.output) == (PASSED, "")


async def test_a_check_reads_nothing_from_stdin(tmp_path: Path) -> None:
    # A check waiting for input would hang the run; stdin is closed.
    result = await run_check(Check("read", "cat"), _workspace(tmp_path / "copy"), time_limit=10)
    assert result.status == PASSED


async def test_a_check_over_its_limit_is_stopped_with_its_children(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    started = time.monotonic()
    result = await run_check(
        Check("tests", "sleep 60 & echo $! > child.pid; echo started; wait"), ws, time_limit=0.5
    )
    assert time.monotonic() - started < 10
    assert (result.status, result.exit_code, result.passed) == (TIMED_OUT, None, False)
    assert "started" in result.output and "[stopped after 0.5 s]" in result.output
    child = int((ws.path / "child.pid").read_text())
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_a_cancelled_check_takes_its_process_group_along(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    task = asyncio.ensure_future(run_check(Check("t", "echo $$ > sh.pid; sleep 60"), ws))
    for _ in range(100):
        if (ws.path / "sh.pid").exists() and (ws.path / "sh.pid").read_text().strip():
            break
        await asyncio.sleep(0.05)
    shell = int((ws.path / "sh.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.killpg(shell, 0)


async def test_only_the_tail_of_a_long_output_is_kept(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    command = f"head -c {3 * MAX_OUTPUT_CHARS} /dev/zero | tr '\\0' a; echo; echo END"
    result = await run_check(Check("loud", command), ws)
    assert len(result.output) <= MAX_OUTPUT_CHARS
    assert result.output.endswith("END\n")


async def test_a_background_process_does_not_hold_the_result_nor_outlive_the_check(
    tmp_path: Path,
) -> None:
    # `sleep 60 &` keeps the output open after the shell exits 0: the check
    # used to wait for it to its limit and count a green check timed out.
    ws = _workspace(tmp_path / "copy")
    started = time.monotonic()
    result = await run_check(
        Check("bg", "sleep 60 & echo $! > child.pid; echo done"), ws, time_limit=30
    )
    assert time.monotonic() - started < 10
    assert (result.status, result.exit_code) == (PASSED, 0)
    assert result.output.strip() == "done"
    child = int((ws.path / "child.pid").read_text())
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_a_failing_check_takes_its_background_process_along(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    result = await run_check(
        Check("bg", "sleep 60 & echo $! > child.pid; exit 4"), ws, time_limit=30
    )
    assert (result.status, result.exit_code) == (FAILED, 4)
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(int((ws.path / "child.pid").read_text()), 0)


async def test_output_is_read_to_its_end_after_the_shell_exits(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "copy")
    result = await run_check(Check("loud", "seq 1 20000"), ws)
    assert result.status == PASSED and result.output.endswith("19999\n20000\n")


async def test_non_utf8_output_does_not_break_the_check(tmp_path: Path) -> None:
    result = await run_check(Check("bin", "printf '\\377\\376ok'"), _workspace(tmp_path / "c"))
    assert result.status == PASSED and result.output.endswith("ok")


async def test_two_checks_run_at_once_in_their_own_copies(tmp_path: Path) -> None:
    first, second = _workspace(tmp_path / "a"), _workspace(tmp_path / "b")
    check = Check("w", "sleep 0.2; basename $(pwd) > who")
    results = await asyncio.gather(run_check(check, first), run_check(check, second))
    assert [r.status for r in results] == [PASSED, PASSED]
    assert (first.path / "who").read_text().strip() == "a"
    assert (second.path / "who").read_text().strip() == "b"


# --- environment and what is hidden --------------------------------------------


DAEMON_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/runner",
    "SHELL": "/bin/sh",
    "LANG": "C.UTF-8",
    "HTTPS_PROXY": "http://proxy:3128",
    "no_proxy": "localhost",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "HOME_DIR_NOTE": "kept",
    "CONTROL_PLANE_API_KEY": "cp_secret_value",
    "IAM_PLATFORM_ACCESS_TOKEN": "pat_secret_value",
    "FLEET_SERVICES_TOKEN_FILE": "/run/token",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret",
    "ANTHROPIC_API_KEY": "sk-ant-api-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
    "GH_TOKEN": "ghp_secret_value",
    "GIT_ASKPASS": "/usr/bin/askpass",
    "GIT_CONFIG_GLOBAL": "/home/runner/.gitconfig-daemon",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "XDG_CONFIG_HOME": "/home/runner/.config",
    "LD_PRELOAD": "x.so",
    "PYTHONPATH": "/app/src",
    "UV_PROJECT_ENVIRONMENT": "/app/.venv",
}


def test_the_daemons_reserved_variables_do_not_reach_a_check() -> None:
    env = check_environment({"DATABASE_URL": "postgresql://u:p@h/d"}, DAEMON_ENV)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/runner",
        "SHELL": "/bin/sh",
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy:3128",
        "no_proxy": "localhost",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "HOME_DIR_NOTE": "kept",
        "DATABASE_URL": "postgresql://u:p@h/d",
    }


def test_the_kept_reserved_entries_are_reserved_entries() -> None:
    # A name dropped from RESERVED_ENV_NAMES must not linger here unnoticed.
    assert set(RESERVED_ENV_NAMES) >= KEPT_RESERVED_ENTRIES
    assert {reserved_entry(name) for name in KEPT_PROXY_NAMES} == {"*_PROXY"}


def test_the_run_environment_cannot_set_reserved_names() -> None:
    env = check_environment({"PATH": "/evil", "LD_PRELOAD": "x.so", "OK": "1"}, {"PATH": "/bin"})
    assert env == {"PATH": "/bin", "OK": "1"}


def test_no_run_environment_is_the_daemons_without_its_own() -> None:
    assert check_environment(None, {"A": "1", "CONTROL_PLANE_X": "2"}) == {"A": "1"}
    assert check_environment(None, {}) == {}


async def test_a_check_sees_the_run_environment_and_not_the_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_should_not_leak")
    result = await run_check(
        Check("env", 'echo "${CONTROL_PLANE_API_KEY-<unset>} ${SVC_PORT-<unset>}"'),
        _workspace(tmp_path / "copy"),
        {"SVC_PORT": "5432"},
    )
    assert result.output.strip() == "<unset> 5432"


async def test_a_check_env_does_not_see_the_executors_and_forges_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN", "GIT_ASKPASS"):
        monkeypatch.setenv(name, DAEMON_ENV.get(name, "value-" + name))
    result = await run_check(Check("env", "env"), _workspace(tmp_path / "copy"))
    names = {line.split("=", 1)[0] for line in result.output.splitlines() if "=" in line}
    assert result.status == PASSED
    assert not names & {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN", "GIT_ASKPASS"}
    assert "PATH" in names


async def test_a_token_a_check_prints_is_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check does not get the token, but may find it elsewhere (a file, a
    # config it reads): the output that goes to the prompt must not carry it.
    token = DAEMON_ENV["CLAUDE_CODE_OAUTH_TOKEN"]
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "leak").write_text(f"token={token}\n")
    result = await run_check(
        Check("leak", f"cat leak; echo {token} >&2; exit 1"), _workspace(tmp_path / "copy")
    )
    assert token not in result.output and token not in str(result.feedback())
    assert token not in str(result.metadata())
    assert result.output.splitlines() == [f"token={REDACTED}", REDACTED]


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN", True),
        ("ANTHROPIC_API_KEY", True),
        ("GH_TOKEN", True),
        ("CONTROL_PLANE_API_KEY", True),
        ("IAM_PLATFORM_ACCESS_TOKEN", True),
        ("DB_PASSWORD", True),
        ("aws_secret_access_key", True),
        # PASS$, _PASS_, PWD, _PAT$, AUTH, DSN (review of TASK-001193)
        ("DB_PASS", True),
        ("PGPASS", True),
        ("REDIS_PASS_VALUE", True),
        ("MYSQL_PWD", True),
        ("mysql_pwd", True),
        ("GITLAB_PAT", True),
        ("DOCKER_AUTH_CONFIG", True),
        ("HTTP_AUTHORIZATION", True),
        ("SENTRY_DSN", True),
        # PASSPHRASE (second review of TASK-001211)
        ("GPG_PASSPHRASE", True),
        ("BORG_PASSPHRASE", True),
        ("borg_passphrase", True),
        ("GPG_PASSPHRASE_FILE", False),
        ("DB_PASS_FILE", False),
        ("PGPASSFILE", False),
        ("PWD", False),
        ("OLDPWD", False),
        ("GIT_AUTHOR_NAME", False),
        ("GIT_AUTHOR", False),
        ("SSH_ASKPASS", False),
        ("sudo_askpass", False),
        ("PATH_PATTERN", False),
        ("PASSPORT_ID", False),
        ("FLEET_SERVICES_TOKEN_FILE", False),
        ("CONTROL_PLANE_AGENT_POOL_DIR", False),
        ("SSH_AUTH_SOCK", False),
        ("PATH", False),
        ("HOME", False),
    ],
)
def test_which_names_are_secrets(name: str, secret: bool) -> None:
    assert is_secret_name(name) is secret


def test_the_known_secrets_are_the_services_and_the_daemons_secret_names() -> None:
    found = secrets_of(["postgresql://u:p@h/d", "s3cr3t-pass"], DAEMON_ENV)
    assert set(found) == {
        "postgresql://u:p@h/d",
        "s3cr3t-pass",
        "cp_secret_value",
        "pat_secret_value",
        "sk-ant-oat01-secret",
        "sk-ant-api-secret",
        "sk-openai-secret",
        "ghp_secret_value",
    }
    assert list(found) == sorted(found, key=len, reverse=True)
    assert secrets_of((), {}) == ()


async def test_service_credentials_in_the_output_are_hidden(tmp_path: Path) -> None:
    url = "postgresql://test:s3cr3t-pass@db:5432/test"
    result = await run_check(
        Check("tests", 'echo "connecting to $DATABASE_URL"; echo "pw $PW"; exit 1'),
        _workspace(tmp_path / "copy"),
        {"DATABASE_URL": url, "PW": "s3cr3t-pass", "SHORT": "1"},
        secrets=(url, "s3cr3t-pass"),
    )
    assert "s3cr3t-pass" not in result.output
    assert result.output.splitlines() == [f"connecting to {REDACTED}", f"pw {REDACTED}"]


def test_short_values_are_not_redacted() -> None:
    assert redact("exit 1 of 12345", ["1", "12345"], {}) == "exit 1 of 12345"
    assert redact("anything", (), {}) == "anything"
    assert redact("", (), DAEMON_ENV) == ""
    assert redact("key ghp_secret_value", (), {"GH_TOKEN": "ghp_secret_value"}) == "key ***"


async def test_a_secret_in_the_command_is_masked_in_the_metadata(tmp_path: Path) -> None:
    url = "postgresql://test:s3cr3t-pass@db:5432/test"
    result = await run_check(
        Check("t", f"echo '{url}' > /dev/null"),
        _workspace(tmp_path / "copy"),
        {"DB": url},
        secrets=(url,),
    )
    assert url not in str(result.metadata()) and url not in str(result.feedback())


def test_secret_names_outside_the_reserved_list_do_not_reach_a_check() -> None:
    daemon = {
        "GITHUB_TOKEN": "ghp_other_secret",
        "NPM_TOKEN": "npm_secret_value",
        "AWS_SECRET_ACCESS_KEY": "aws_secret_value",
        "aws_session_token": "aws_session_value",
        "DB_PASSWORD": "db_secret_value",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin",
        "https_proxy": "http://user:pa55word@proxy:3128",
        "KUBECONFIG_FILE": "/etc/kube",
    }
    env = check_environment(None, daemon)
    assert env == {
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin",
        "https_proxy": "http://user:pa55word@proxy:3128",
        "KUBECONFIG_FILE": "/etc/kube",
    }


@pytest.mark.parametrize(
    "name", ["HTTP_PROXY", "https_proxy", "No_Proxy", "all_proxy", "HTTPS_PROXY", "no_proxy"]
)
def test_the_known_proxy_names_are_kept_in_any_case(name: str) -> None:
    # What lets a request leave the host; its userinfo is masked in the output.
    assert check_environment(None, {name: "http://p:3128"}) == {name: "http://p:3128"}


@pytest.mark.parametrize(
    "name", ["GITHUB_TOKEN_PROXY", "SECRET_PROXY", "github_token_proxy", "MY_PROXY", "FTP_PROXY"]
)
def test_other_proxy_names_do_not_reach_a_check(name: str) -> None:
    # Not every *_PROXY is a proxy: the suffix alone keeps nothing.
    assert check_environment(None, {name: "ghp_secret_value", "LANG": "C"}) == {"LANG": "C"}


def test_the_run_environment_keeps_its_secret_names() -> None:
    # Service credentials are the check's to use: the run gave them.
    assert check_environment({"DB_PASSWORD": "pw"}, {"DB_PASSWORD": "daemon"}) == {
        "DB_PASSWORD": "pw"
    }


@pytest.mark.parametrize(
    "name",
    [
        "claude_code_oauth_token",
        "Gh_Token",
        "gh_host",
        "git_askpass",
        "Anthropic_Base_Url",
        "control_plane_agent_key",
        "ld_preload",
        "Ssh_Auth_Sock",
    ],
)
def test_reserved_names_in_any_case_do_not_reach_a_check(name: str) -> None:
    assert check_environment(None, {name: "value", "LANG": "C"}) == {"LANG": "C"}


def test_kept_reserved_names_are_kept_in_any_case() -> None:
    assert check_environment(None, {"path": "/bin", "Home": "/h"}) == {"path": "/bin", "Home": "/h"}


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://u:pw@db:5432/app",
        "postgresql+asyncpg://postgres:s3cret@localhost/test",
        "redis://:pw@cache:6379/0",
        "https://user:tok@example.com/repo.git",
        "first http://a@x then amqp://u:p@mq/",
        "Server=db;Database=app;User Id=sa;Password=s3cret;",
        "Driver={ODBC};Server=db;UID=sa;PWD=s3cret",
        "DefaultEndpointsProtocol=https;AccountName=acc;AccountKey=abc+/==;EndpointSuffix=x",
        "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=Root;SharedAccessKey=k=",
        "password=s3cret",
        "Server=db; password = s3cret",
    ],
)
def test_values_carrying_a_secret(value: str) -> None:
    assert is_secret_value(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "C.UTF-8",
        "postgresql://db:5432/app",
        "postgresql://postgres@db/app",
        "postgresql://postgres:@db/app",
        "https://example.com/a:b@c",
        "Server=db;Database=app;User Id=sa;",
        "Server=db;Password=;Database=app",
        "Endpoint=sb://ns/;SharedAccessKeyName=Root",
        "--password=s3cret",
        "MyPassword=x",
        "/home/runner/.pgpass",
    ],
)
def test_values_carrying_no_secret(value: str) -> None:
    assert is_secret_value(value) is False


def test_daemon_variables_whose_value_carries_a_secret_do_not_reach_a_check() -> None:
    # Their names say nothing: masking the password in the output is not
    # enough, the check's own code would see the whole value.
    daemon = {
        "DATABASE_URL": "postgresql://u:pw@db/app",
        "database_url": "postgresql://u:pw@db/app",
        "AZURE_STORAGE_CONNECTION_STRING": "AccountName=a;AccountKey=abc==",
        "SERVICEBUS_CONNECTION": "Endpoint=sb://ns/;SharedAccessKey=k=",
        "SQL_CONN": "Server=db;User Id=sa;Password=pw",
        "PUBLIC_DATABASE_URL": "postgresql://u@db/app",
        "UPSTREAM": "https://example.com",
        "LANG": "C.UTF-8",
    }
    assert check_environment(None, daemon) == {
        "PUBLIC_DATABASE_URL": "postgresql://u@db/app",
        "UPSTREAM": "https://example.com",
        "LANG": "C.UTF-8",
    }


@pytest.mark.parametrize("name", ["PATH", "HOME", "SHELL", "SSL_CERT_FILE", "https_proxy"])
def test_kept_reserved_names_are_kept_whatever_their_value(name: str) -> None:
    # Without them no program runs, and no request leaves a host behind an
    # authenticating proxy; their userinfo is masked in the output.
    value = "http://user:pa55word@proxy:3128"
    assert check_environment(None, {name: value}) == {name: value}


def test_the_run_environment_keeps_its_urls_with_a_password() -> None:
    # The run's database is the check's to use, and replaces the daemon's.
    env = check_environment(
        {"DATABASE_URL": "postgresql://u:run@h/d"}, {"DATABASE_URL": "postgresql://u:daemon@h/d"}
    )
    assert env == {"DATABASE_URL": "postgresql://u:run@h/d"}


def test_no_secret_names_is_no_change() -> None:
    assert check_environment(None, {}) == {}
    assert check_environment({}, {"LANG": "C"}) == {"LANG": "C"}


async def test_a_check_does_not_see_secrets_outside_the_list_nor_in_other_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked = {
        "GITHUB_TOKEN": "ghp_other_secret",
        "NPM_TOKEN": "npm_secret_value",
        "AWS_SECRET_ACCESS_KEY": "aws_secret_value",
        "claude_code_oauth_token": "sk-ant-oat01-lower",
        "Gh_Token": "ghp_mixed_case",
    }
    for name, value in leaked.items():
        monkeypatch.setenv(name, value)
    result = await run_check(Check("env", "env"), _workspace(tmp_path / "copy"))
    names = {line.split("=", 1)[0] for line in result.output.splitlines() if "=" in line}
    assert result.status == PASSED
    assert not names & set(leaked)
    assert "PATH" in names


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            "proxy http://user:pa55word@proxy:3128/x",
            f"proxy http://{REDACTED}@proxy:3128/x",
        ),
        (
            "E connect postgres://u:pw@db:5432/cp failed",
            f"E connect postgres://{REDACTED}@db:5432/cp failed",
        ),
        ("clone https://ghp_tok3n@github.com/o/r", f"clone https://{REDACTED}@github.com/o/r"),
        ("two a://x:y@h b://z:w@h", f"two a://{REDACTED}@h b://{REDACTED}@h"),
        # No userinfo: nothing to hide.
        ("http://host:8080/a@b", "http://host:8080/a@b"),
        ("mail user@example.org", "mail user@example.org"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("", ""),
    ],
)
def test_the_userinfo_of_any_url_is_masked(text: str, shown: str) -> None:
    assert redact(text, (), {}) == shown


def test_a_url_password_of_the_daemon_is_masked_on_its_own() -> None:
    daemon = {"https_proxy": "http://user:pa55word@proxy:3128", "LANG": "C.UTF-8"}
    assert "pa55word" in secrets_of((), daemon)
    assert redact("auth failed for pa55word", (), daemon) == f"auth failed for {REDACTED}"


async def test_a_proxy_password_a_check_prints_is_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("https_proxy", "http://user:pa55word@proxy:3128")
    result = await run_check(
        Check("proxy", 'echo "via $https_proxy"; echo "$https_proxy" | cut -d: -f3'),
        _workspace(tmp_path / "copy"),
    )
    assert "pa55word" not in result.output
    assert result.output.splitlines() == [f"via http://{REDACTED}@proxy:3128", f"{REDACTED}@proxy"]


async def test_run_environment_values_without_credentials_are_not_masked(
    tmp_path: Path,
) -> None:
    # Only what carries {user} or {password} is a secret: a database named
    # like the package must not turn every path of a traceback into ***.
    env = {"CP_DB_NAME": "billing_core", "CP_DB_HOST": "fleet-svc-db-3f9a"}
    result = await run_check(
        Check("tb", 'echo "File /app/src/billing_core/x.py"; echo "$CP_DB_HOST"; exit 1'),
        _workspace(tmp_path / "copy"),
        env,
        secrets=("s3cr3t-pass",),
    )
    assert result.output.splitlines() == ["File /app/src/billing_core/x.py", "fleet-svc-db-3f9a"]


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASS",
        "MYSQL_PWD",
        "PGPASS",
        "GITLAB_PAT",
        "DOCKER_AUTH_CONFIG",
        "SENTRY_DSN",
        "GPG_PASSPHRASE",
        "BORG_PASSPHRASE",
    ],
)
async def test_a_secret_of_the_new_names_neither_reaches_a_check_nor_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    value = "s3cr3t-" + name.lower()
    monkeypatch.setenv(name, value)
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "leak").write_text(f"{value}\n")
    result = await run_check(
        Check("leak", f'echo "${{{name}-<unset>}}"; cat leak; exit 1'),
        _workspace(tmp_path / "copy"),
    )
    assert result.output.splitlines() == ["<unset>", REDACTED]
    assert value not in str(result.feedback())


def test_the_user_of_a_url_in_a_secret_variable_is_masked_on_its_own() -> None:
    # A DSN keeps its key as the user of the URL, with no password after it.
    daemon = {"SENTRY_DSN": "https://0123456789abcdef@sentry.example/42", "LANG": "C.UTF-8"}
    assert "0123456789abcdef" in secrets_of((), daemon)
    assert redact("key 0123456789abcdef sent", (), daemon) == f"key {REDACTED} sent"
    assert redact(daemon["SENTRY_DSN"], (), daemon) == REDACTED


def test_the_user_of_a_secret_url_with_a_password_is_no_secret_on_its_own() -> None:
    # The user before a password is a plain name (a database role), and
    # hiding it would turn every "postgres" of the output into ***.
    daemon = {"PG_DSN": "postgresql://postgres:pg-s3cr3t-pw@db/x"}
    found = secrets_of((), daemon)
    assert "postgres" not in found
    assert "pg-s3cr3t-pw" in found
    assert redact("role postgres, db postgres", (), daemon) == "role postgres, db postgres"
    assert redact("password pg-s3cr3t-pw", (), daemon) == f"password {REDACTED}"
    assert redact(daemon["PG_DSN"], (), daemon) == REDACTED


async def test_a_check_output_keeps_the_user_of_a_dsn_with_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_DSN", "postgresql://postgres:pg-s3cr3t-pw@db/x")
    (tmp_path / "copy").mkdir()
    result = await run_check(
        Check("psql", "echo 'FATAL: role postgres: pg-s3cr3t-pw'; exit 1"),
        _workspace(tmp_path / "copy"),
    )
    assert result.output.splitlines() == [f"FATAL: role postgres: {REDACTED}"]


def test_the_user_of_a_url_elsewhere_is_no_secret_on_its_own() -> None:
    # Outside secret names only the password of a URL is: a user printed
    # alone (a proxy login, a database role) stays readable.
    daemon = {"https_proxy": "http://proxyuser:pa55word@proxy:3128", "DATABASE_URL": "x"}
    assert "proxyuser" not in secrets_of((), daemon)
    assert redact("login proxyuser", (), daemon) == "login proxyuser"


@pytest.mark.parametrize(
    "value", ["", "https://sentry.example/42", "https://@sentry.example/1", "https://ab@h/1"]
)
def test_a_secret_url_without_a_user_worth_hiding_adds_nothing(value: str) -> None:
    found = secrets_of((), {"SENTRY_DSN": value})
    assert found == ((value,) if len(value) >= 6 else ())


def test_secrets_are_deduplicated_and_short_ones_dropped() -> None:
    assert secrets_of(["s3cr3t-pass", "s3cr3t-pass", "test", ""], {}) == ("s3cr3t-pass",)


# --- the list at the base ---------------------------------------------------


def test_checks_are_read_at_the_base_not_the_branch(tmp_path: Path) -> None:
    copy_path = tmp_path / "copy"
    base = _repo(copy_path, "version: 1\nchecks:\n  - {name: lint, run: make lint}\n")
    (copy_path / ".agents" / "runner.yaml").write_text(
        "version: 1\nchecks:\n  - {name: nothing, run: 'true'}\n"
    )
    _git(copy_path, "commit", "-qam", "the task edits its conventions")
    head = _git(copy_path, "rev-parse", "HEAD")
    plan = checks_at_base(_workspace(copy_path, head, conventions_base=base))
    assert plan.revision == base
    assert plan.checks == (Check("lint", "make lint"),)


def test_a_copy_without_a_known_base_blocks_instead_of_reading_the_branch(
    tmp_path: Path,
) -> None:
    # One repository, a branch taken over without the record of its base:
    # base_commit is the head of the task branch, and reading runner.yaml
    # there would let the task pick its own checks (FR-009).
    copy_path = tmp_path / "copy"
    _repo(copy_path, "version: 1\nchecks:\n  - {name: lint, run: make lint}\n")
    (copy_path / ".agents" / "runner.yaml").write_text("version: 1\n")
    _git(copy_path, "commit", "-qam", "the task drops its checks")
    head = _git(copy_path, "rev-parse", "HEAD")
    with pytest.raises(WorkspaceBlocked) as caught:
        checks_at_base(_workspace(copy_path, head))
    assert caught.value.code == CHECKS_BASE_UNKNOWN
    assert "task/TASK-1" in caught.value.reason and str(tmp_path) not in caught.value.reason


def test_the_base_revision_wins_over_the_base_commit(tmp_path: Path) -> None:
    copy_path = tmp_path / "copy"
    first = _repo(copy_path, "version: 1\nchecks:\n  - {name: old, run: 'true'}\n")
    (copy_path / ".agents" / "runner.yaml").write_text(
        "version: 1\nchecks:\n  - {name: new, run: 'true'}\n"
    )
    _git(copy_path, "commit", "-qam", "newer base")
    newer = _git(copy_path, "rev-parse", "HEAD")
    plan = checks_at_base(_workspace(copy_path, newer, base_revision=first, conventions_base=newer))
    assert (plan.revision, [c.name for c in plan.checks]) == (first, ["old"])


def test_no_runner_yaml_is_no_checks(tmp_path: Path) -> None:
    base = _repo(tmp_path / "copy", None)
    plan = checks_at_base(_workspace(tmp_path / "copy", base, conventions_base=base))
    assert (plan.revision, plan.checks) == (base, ())


def test_runner_yaml_without_checks_is_no_checks(tmp_path: Path) -> None:
    base = _repo(tmp_path / "copy", "version: 1\n")
    assert checks_at_base(_workspace(tmp_path / "copy", base, conventions_base=base)).checks == ()


def test_an_invalid_runner_yaml_blocks(tmp_path: Path) -> None:
    base = _repo(tmp_path / "copy", "version: 1\nchecks:\n  - {name: lint, run: 7}\n")
    with pytest.raises(WorkspaceBlocked) as caught:
        checks_at_base(_workspace(tmp_path / "copy", base, conventions_base=base))
    assert caught.value.code == RUNNER_CONFIG_INVALID
    assert "$.checks[0].run" in caught.value.reason


def test_a_catalog_run_uses_the_conventions_it_was_prepared_by(tmp_path: Path) -> None:
    config = RunnerConfig(version=1, checks=(Check("types", "make typecheck"),))
    ws = _workspace(tmp_path / "copy", conventions=Conventions(revision="abc", config=config))
    plan = checks_at_base(ws)  # no git at all: nothing is read again
    assert (plan.revision, plan.checks) == ("abc", config.checks)
    bare = _workspace(tmp_path / "copy2", conventions=Conventions(revision="def"))
    assert checks_at_base(bare).checks == ()


# --- the report -------------------------------------------------------------


def _result(name: str, status: str, code: int | None = 0, run: str = "make x") -> CheckResult:
    return CheckResult(name, run, status, code, 1.23456, output="secret output")


def test_report_of_green_checks() -> None:
    report = ChecksReport("abc", (_result("lint", PASSED), _result("tests", PASSED)))
    assert report.status == PASSED and report.failed == ()
    assert report.metadata() == {
        "status": PASSED,
        "revision": "abc",
        "fixAttempted": False,
        "durationSeconds": 2.469,
        "results": [
            {
                "name": "lint",
                "run": "make x",
                "status": PASSED,
                "exitCode": 0,
                "durationSeconds": 1.235,
            },
            {
                "name": "tests",
                "run": "make x",
                "status": PASSED,
                "exitCode": 0,
                "durationSeconds": 1.235,
            },
        ],
    }


def test_report_after_a_fix_attempt_keeps_both_rounds() -> None:
    first = (_result("lint", PASSED), _result("tests", FAILED, 2))
    last = (_result("lint", PASSED), _result("tests", TIMED_OUT, None))
    report = ChecksReport("abc", last, first=first)
    data = report.metadata()
    assert (data["status"], data["fixAttempted"]) == (FAILED, True)
    assert [r["status"] for r in data["firstResults"]] == [PASSED, FAILED]
    assert [r["status"] for r in data["results"]] == [PASSED, TIMED_OUT]
    assert data["durationSeconds"] == round(4 * 1.23456, 3)
    assert report.summary() == "tests (timed out)"
    assert ChecksReport("abc", first).summary() == "tests (exit 2)"


def test_a_check_the_budget_left_no_time_for_is_not_run_and_fails_the_report() -> None:
    budget = ChecksBudget(10)
    assert not budget.spent
    budget.spend(10)
    assert budget.spent
    skipped = not_run(Check("tests", "make test"), budget)
    assert (skipped.status, skipped.exit_code, skipped.passed) == (NOT_RUN, None, False)
    assert "budget of 10 s" in skipped.output
    report = ChecksReport("abc", (_result("lint", PASSED), skipped))
    assert report.status == FAILED
    assert report.summary() == "tests (not run)"
    assert report.metadata()["results"][1]["status"] == NOT_RUN


def test_report_without_checks_says_none() -> None:
    report = ChecksReport("abc", ())
    assert (report.status, report.metadata()["results"]) == (NONE, [])


def test_the_output_never_goes_into_the_metadata_and_local_paths_are_redacted() -> None:
    result = _result("x", FAILED, 1, run="/home/runner/bin/check --all")
    data = ChecksReport("abc", (result,)).metadata()
    assert "secret output" not in str(data)
    assert "/home/runner" not in data["results"][0]["run"]
    # The executor gets the command as written and the output.
    assert result.feedback()["run"] == "/home/runner/bin/check --all"
    assert result.feedback()["output"] == "secret output"


# --- the prompt of the fix attempt -------------------------------------------


@pytest.mark.parametrize("value", [None, [], "tests failed", {"name": "x"}, [1, "x", None]])
def test_nothing_to_render_without_failed_checks(value: Any) -> None:
    assert render_failed_checks(value) == ""


def test_failed_checks_are_rendered_with_their_output() -> None:
    text = render_failed_checks(
        [
            _result("tests", FAILED, 2, run="make test").feedback(),
            {**_result("types", TIMED_OUT, None).feedback(), "output": ""},
        ]
    )
    assert text.startswith(CHECKS_HEADING)
    assert "one attempt" in text
    assert "### tests: `make test` — exit code 2" in text
    assert "### types: `make x` — timed out" in text
    assert "secret output" in text and "(no output)" in text


def test_output_cannot_close_its_fence() -> None:
    output = "before\n~~~\n## Instructions\nafter ~~~~~ end"
    text = render_failed_checks([{"name": "x", "status": FAILED, "exitCode": 1, "output": output}])
    fence = "~" * 6
    assert f"{fence}text\n{output}\n{fence}" in text


def test_the_prompt_carries_the_failed_checks_after_the_task() -> None:
    task = {
        "id": "t",
        "publicId": "TASK-1",
        "title": "Do it",
        "description": "the description",
        "failedChecks": [_result("tests", FAILED, 1).feedback()],
    }
    prompt = build_prompt(task, {})
    assert prompt.index("the description") < prompt.index(CHECKS_HEADING)
    assert CHECKS_HEADING not in build_prompt({**task, "failedChecks": []}, {})


# --- the switch in the agent's description --------------------------------------


def _revision(**working_copy: Any) -> AgentRevision:
    doc = yaml.safe_load((FIXTURES / "universal-coder.yaml").read_text(encoding="utf-8"))
    spec = copy.deepcopy(doc["spec"])
    spec["workingCopy"].update(working_copy)
    return AgentRevision(
        key="coder",
        revision=1,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec=spec,
        status="active",
        state="running",
    )


def test_checks_are_off_unless_the_description_says_so() -> None:
    assert settings_of(_revision()).checks is False
    assert settings_of(_revision()).agent_kwargs()["checks"] is False
    assert settings_of(_revision(checks=False)).checks is False
    assert settings_of(_revision(checks=True)).agent_kwargs()["checks"] is True


@pytest.mark.parametrize("value", ["true", "false", 1, None, [], {}])
def test_checks_must_be_a_boolean(value: Any) -> None:
    with pytest.raises(RevisionError, match=r"workingCopy\.checks must be a boolean"):
        settings_of(_revision(checks=value))


def test_the_catalog_accepts_the_switch() -> None:
    spec = {
        "repositoryField": "repositoryKey",
        "repositories": {"control-plane": {"url": "https://example.org/cp.git"}},
        "checks": True,
    }
    assert "control-plane" in RepositoryCatalog.from_spec(spec).entries


def test_the_one_repository_form_carries_the_switch_too() -> None:
    revision = _revision()
    spec = dict(revision.spec)
    spec["workingCopy"] = {"repository": "https://example.org/r.git", "checks": True}
    single = AgentRevision(**{**revision.__dict__, "spec": spec})
    assert settings_of(single).checks is True


# --- the daemon's loop over the checks -----------------------------------------


class _FlakyCore:
    """``record_action`` fails with ``error``; the rest of the client is not needed."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.finished: list[str] = []

    async def record_action(self, run_id: str, **fields: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"id": f"a-{len(self.finished)}"}

    async def finish_action(self, run_id: str, action_id: str, **fields: Any) -> dict[str, Any]:
        self.finished.append(action_id)
        return {}


def _agent(core: _FlakyCore) -> Agent:
    return Agent(cast(ControlPlaneClient, core), adapter=None)


def _plan(*checks: Check) -> ChecksPlan:
    return ChecksPlan(revision="abc", checks=checks)


@pytest.mark.parametrize(
    "error",
    [
        TransportError("connection reset"),
        ControlPlaneError("internal_error", "boom", status=500),
        ControlPlaneError("unavailable", "later", status=503),
        ControlPlaneError("rate_limited", "slow down", status=429),
    ],
)
async def test_a_transient_core_error_does_not_drop_the_checks(
    tmp_path: Path, error: ControlPlaneError
) -> None:
    core = _FlakyCore(error)
    results = await _agent(core)._run_checks(
        {"id": "r"}, _workspace_repo(tmp_path), _plan(Check("ok", "true")), None, ChecksBudget()
    )
    assert [r.status for r in results] == [PASSED]
    assert core.finished == []  # nothing recorded, nothing to finish


@pytest.mark.parametrize(
    "error",
    [
        StaleClaimError("stale_claim", "lost", status=409),
        RunNotActiveError("run_not_active", "over", status=409),
        PermissionDeniedError("permission_denied", "no", status=403),
    ],
)
async def test_a_refusal_of_the_core_is_not_swallowed(
    tmp_path: Path, error: ControlPlaneError
) -> None:
    with pytest.raises(type(error)):
        await _agent(_FlakyCore(error))._run_checks(
            {"id": "r"}, _workspace_repo(tmp_path), _plan(Check("ok", "true")), None, ChecksBudget()
        )


def test_which_core_errors_are_tolerated_on_recording_a_check() -> None:
    assert record_failure_tolerated(TransportError("x"))
    assert record_failure_tolerated(ControlPlaneError("x", "y", status=502))
    assert record_failure_tolerated(ControlPlaneError("internal_error", "y", status=500))
    assert record_failure_tolerated(ControlPlaneError("rate_limited", "y", status=429))
    assert not record_failure_tolerated(ControlPlaneError("x", "y", status=0))
    assert not record_failure_tolerated(ControlPlaneError("x", "y", status=422))


def _workspace_repo(tmp_path: Path) -> Workspace:
    base = _repo(tmp_path / "copy", None)
    return _workspace(tmp_path / "copy", base, conventions_base=base)


async def test_the_budget_is_shared_by_the_checks_and_cuts_the_one_that_overruns(
    tmp_path: Path,
) -> None:
    core = _FlakyCore(None)
    budget = ChecksBudget(1.0)
    started = time.monotonic()
    results = await _agent(core)._run_checks(
        {"id": "r"},
        _workspace_repo(tmp_path),
        _plan(Check("quick", "true"), Check("slow", "sleep 30"), Check("late", "true")),
        None,
        budget,
    )
    assert time.monotonic() - started < 10
    assert [r.status for r in results] == [PASSED, TIMED_OUT, NOT_RUN]
    assert budget.spent
    assert len(core.finished) == 2  # the one not run was never an action
    # The next round has nothing left either.
    again = await _agent(core)._run_checks(
        {"id": "r"}, _workspace_repo(tmp_path / "2"), _plan(Check("quick", "true")), None, budget
    )
    assert [r.status for r in again] == [NOT_RUN]


async def test_the_checks_leave_nothing_behind_even_when_they_fail(tmp_path: Path) -> None:
    ws = _workspace_repo(tmp_path)
    (ws.path / "work.txt").write_text("the executor's\n")
    results = await _agent(_FlakyCore(None))._run_checks(
        {"id": "r"},
        ws,
        _plan(Check("mess", "echo junk > junk.txt; echo changed > work.txt; exit 1")),
        None,
        ChecksBudget(),
    )
    assert [r.status for r in results] == [FAILED]
    assert not (ws.path / "junk.txt").exists()
    assert (ws.path / "work.txt").read_text() == "the executor's\n"


async def test_the_run_secrets_reach_the_checks(tmp_path: Path) -> None:
    results = await _agent(_FlakyCore(None))._run_checks(
        {"id": "r"},
        _workspace_repo(tmp_path),
        _plan(Check("leak", "echo s3cr3t-pass billing_core; exit 1")),
        {"DB_NAME": "billing_core"},
        ChecksBudget(),
        ("s3cr3t-pass",),
    )
    assert results[0].output.strip() == f"{REDACTED} billing_core"


@pytest.mark.parametrize(
    "run",
    [
        "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m by-a-check",
        "git checkout -q -b elsewhere",
        "git checkout -q --detach",
    ],
)
async def test_a_check_that_moves_head_blocks(tmp_path: Path, run: str) -> None:
    ws = _workspace_repo(tmp_path)
    (ws.path / "work.txt").write_text("the executor's\n")
    with pytest.raises(WorkspaceBlocked) as caught:
        await _agent(_FlakyCore(None))._run_checks(
            {"id": "r"}, ws, _plan(Check("move", run)), None, ChecksBudget()
        )
    assert caught.value.code == CHECKS_MOVED_HEAD
    assert "moved HEAD" in caught.value.reason and str(tmp_path) not in caught.value.reason
    # The files are put back all the same.
    assert (ws.path / "work.txt").read_text() == "the executor's\n"


async def test_a_check_that_leaves_head_where_it_was_does_not_block(tmp_path: Path) -> None:
    ws = _workspace_repo(tmp_path)
    run = "git checkout -q -b tmp && git checkout -q main && git branch -q -D tmp"
    results = await _agent(_FlakyCore(None))._run_checks(
        {"id": "r"}, ws, _plan(Check("roundtrip", run)), None, ChecksBudget()
    )
    assert [r.status for r in results] == [PASSED]


def _broken_restore(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def restore(self: Workspace, tree: str) -> None:
        calls.append(tree)
        raise WorkspaceError("git restore failed")

    monkeypatch.setattr(Workspace, "restore", restore)
    return calls


async def test_a_failed_restore_does_not_replace_the_error_of_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace_repo(tmp_path)
    calls = _broken_restore(monkeypatch)
    error = StaleClaimError("stale_claim", "lost", status=409)
    with pytest.raises(StaleClaimError):
        await _agent(_FlakyCore(error))._run_checks(
            {"id": "r"}, ws, _plan(Check("ok", "true")), None, ChecksBudget()
        )
    assert len(calls) == 1  # tried once, then the checks' own error


async def test_a_failed_restore_after_the_checks_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace_repo(tmp_path)
    _broken_restore(monkeypatch)
    with pytest.raises(WorkspaceBlocked) as caught:
        await _agent(_FlakyCore(None))._run_checks(
            {"id": "r"}, ws, _plan(Check("ok", "true")), None, ChecksBudget()
        )
    assert caught.value.code == CHECKS_RESTORE_FAILED


@pytest.mark.parametrize(
    "gitignore",
    [None, "*.pyc\n"],
    ids=["no-gitignore", "gitignore"],
)
async def test_a_check_that_ignores_its_output_does_not_leave_it(
    tmp_path: Path, gitignore: str | None
) -> None:
    ws = _workspace_repo(tmp_path)
    if gitignore is not None:
        (ws.path / ".gitignore").write_text(gitignore)
        _git(ws.path, "add", ".gitignore")
        _git(ws.path, "commit", "-qm", "ignore")
    run = (
        "mkdir -p out sub && echo x > out/report.xml && echo y > sub/cache.bin && "
        "echo out/ >> .gitignore && echo '*' > sub/.gitignore"
    )
    await _agent(_FlakyCore(None))._run_checks(
        {"id": "r"}, ws, _plan(Check("ignore", run)), None, ChecksBudget()
    )
    assert not (ws.path / "out").exists()
    # A .gitignore that ignores itself hides what is beside it from a commit
    # as well: it stays, since the ignored files of the copy are not touched.
    assert (ws.path / "sub" / "cache.bin").exists()
    if gitignore is None:
        assert not (ws.path / ".gitignore").exists()
    else:
        assert (ws.path / ".gitignore").read_text() == gitignore
    assert _git(ws.path, "status", "--porcelain") == ""


async def test_a_check_that_unignores_files_does_not_delete_them(tmp_path: Path) -> None:
    ws = _workspace_repo(tmp_path)
    (ws.path / ".gitignore").write_text(".venv/\n")
    _git(ws.path, "add", ".gitignore")
    _git(ws.path, "commit", "-qm", "ignore")
    (ws.path / ".venv").mkdir()
    (ws.path / ".venv" / "lib.py").write_text("installed\n")
    await _agent(_FlakyCore(None))._run_checks(
        {"id": "r"}, ws, _plan(Check("unignore", ": > .gitignore")), None, ChecksBudget()
    )
    assert (ws.path / ".gitignore").read_text() == ".venv/\n"
    assert (ws.path / ".venv" / "lib.py").read_text() == "installed\n"
