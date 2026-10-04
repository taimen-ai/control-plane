"""Checks before hand-in (universal-runner U014, FR-010, FR-025).

A repository names its checks in ``checks`` of ``.agents/runner.yaml``
(``runner_config.py``): the targets its CI calls, not copies of their
commands. With checks switched on for the agent (``workingCopy.checks`` of
its description, ``CONTROL_PLANE_AGENT_CHECKS=1`` in the env mode) the daemon
runs them after the executor has worked and before it commits:

- the list is read at the base the task branch was cut from, never from the
  branch (FR-009) — before the executor starts, so a file that cannot be used
  stops the run before any work is done (``runner_config_invalid``), and so
  does a copy whose base is unknown (:data:`CHECKS_BASE_UNKNOWN`);
- each check is ``sh -c '<run>'`` in the root of the working copy, one after
  another in the order of the file, every one of them whatever the previous
  gave; the environment is the daemon's without the reserved names
  (``runner_config.reserved_entry``, in any case) but those a program needs
  to run at all (:data:`KEPT_RESERVED_ENTRIES`), without anything named
  as a secret (:func:`is_secret_name`) and without anything whose value
  carries one (:func:`is_secret_value`: ``DATABASE_URL`` with a password, a
  connection string with ``Password=``), plus the ``env`` of the run's
  services; the values of known secrets (:func:`secrets_of`) and the
  ``user:password@`` of any URL are masked in what a check prints — best
  effort: a secret split by a line break or encoded is not recognised;
- a check that moves HEAD (a commit, a checkout) blocks the run
  (:data:`CHECKS_MOVED_HEAD`): what would be committed is no longer the
  executor's work on its branch;
- once the shell of a check is gone, its whole process group goes too: a
  background process a check left behind neither holds the result nor lives on;
- all checks of a hand-in share one budget (:data:`DEFAULT_BUDGET_SECONDS`):
  a check it cuts short is ``timed_out``, one it leaves no time for is
  ``not_run``, and with the budget spent the executor gets no attempt to fix;
- a check that fails gives the executor one attempt to fix it, with the tail
  of what the failed checks printed (``failedChecks`` of the task, rendered by
  ``instructions.render_failed_checks``); then all checks run again, and the
  work is handed in whatever they give;
- what the checks leave in the copy is not the executor's work: the daemon
  puts the copy back to what it was before the checks
  (``Workspace.snapshot``/``Workspace.restore``), so only the executor's
  changes are committed;
- the result goes into ``metadata.checks`` of the ``commit`` artifact and the
  run's output (:meth:`ChecksReport.metadata`) — status, time and exit code of
  each check, never its output: output is the executor's to read, not durable
  state.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from control_plane_agent.conventions import read_runner_config
from control_plane_agent.runner_config import SHELL, Check, reserved_entry, run_environment
from control_plane_agent.workspace import Workspace, WorkspaceBlocked, redact_local_paths

#: The action each check is recorded as: a running check is a running action,
#: so the watchdog of the run waits for it (``supervision.py``).
CHECK_ACTION = "checks.run"
#: How long one check may take before it is stopped and counted failed.
DEFAULT_TIMEOUT_SECONDS = 1800.0
#: How long all checks of one hand-in may take together, both rounds.
DEFAULT_BUDGET_SECONDS = 3600.0
#: How long the output of a check is still read once its shell has exited.
DRAIN_SECONDS = 5.0
#: How much of the end of a check's output the executor gets back.
MAX_OUTPUT_CHARS = 20_000
#: Reserved names (``runner_config.RESERVED_ENV_NAMES``) a check still gets
#: from the daemon: without them no program runs, and no request leaves a host
#: behind a proxy (:data:`KEPT_PROXY_NAMES` of ``*_PROXY``). Every other
#: reserved name stays with the daemon — the
#: checks run code the executor wrote, and the credentials and identity of
#: the daemon, the executor and the forge (``CLAUDE_*``, ``GH_*``, ``GIT_*``,
#: ``SSH_*``...) are not its.
KEPT_RESERVED_ENTRIES = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)
#: The names of ``*_PROXY`` a check gets, in any case: the ones tools read.
#: Not the whole suffix — ``GITHUB_TOKEN_PROXY`` is a token, not a proxy.
KEPT_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
#: How a secret is written in a check's output.
REDACTED = "***"
# A value shorter than this is no secret worth hiding, and replacing it
# ("1", "true") would garble the output.
_MIN_REDACTED_CHARS = 6
# A variable of the daemon named so holds a secret; one that names where a
# secret lies (``*_FILE``, ``*_DIR``) does not, and masking its path would
# garble every path under it in the output.
# ``AUTH`` is not ``AUTHOR`` (``GIT_AUTHOR_NAME``) but is ``AUTHORIZATION``,
# ``*_ASKPASS`` is the path of a program, and ``PWD`` of a shell is the
# current directory, not a password (``MYSQL_PWD``).
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSW(OR)?D|PASSPHRASE|(?<!ASK)PASS$|_PASS_|PWD|_PAT$|AUTH(?!OR(_|$))|DSN"
    r"|KEY|CREDENTIAL|COOKIE|SESSION"
)
_NOT_SECRET_NAMES = frozenset({"PWD", "OLDPWD"})
_NOT_SECRET_SUFFIXES = ("_FILE", "_PATH", "_DIR", "_SOCK", "_URL_FILE")
# The userinfo of a URL (``scheme://user:password@host``), whatever variable
# or line carries it: ``https_proxy``, ``DATABASE_URL`` or a traceback.
_USERINFO = re.compile(r"(?<=://)([^\s/?#@:]*)(?::([^\s/?#@]*))?@")
# A key of a connection string (``Server=db;User Id=u;Password=<pw>``, Azure
# ``AccountKey=``, ``SharedAccessKey=``; ``Pwd=`` of ODBC) that holds a secret
# when something follows its ``=``. Only at the start or after ``;``:
# ``SharedAccessKeyName=`` is a name, ``--password=`` an argument.
_CONNECTION_SECRET = re.compile(
    r"(?:^|;)\s*(?:PASSWORD|PWD|ACCOUNTKEY|SHAREDACCESSKEY)\s*=\s*[^;\s]", re.IGNORECASE
)

PASSED = "passed"
FAILED = "failed"
TIMED_OUT = "timed_out"
#: A check the budget of the hand-in left no time for.
NOT_RUN = "not_run"
#: ``failure_reason`` of a run whose copy has no known base to read checks at.
CHECKS_BASE_UNKNOWN = "checks_base_unknown"
#: ``failure_reason`` of a run whose checks moved HEAD of the copy.
CHECKS_MOVED_HEAD = "checks_moved_head"
#: ``failure_reason`` of a run whose copy could not be put back after its checks.
CHECKS_RESTORE_FAILED = "checks_restore_failed"
#: ``status`` of a report when ``runner.yaml`` declares no checks.
NONE = "none"


@dataclass(frozen=True)
class ChecksPlan:
    """The checks of a run: those of ``runner.yaml`` at ``revision``."""

    revision: str
    checks: tuple[Check, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    name: str
    run: str
    status: str
    exit_code: int | None
    duration_seconds: float
    output: str = field(default="", repr=False)

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            # A command of the repository may name a path of this host.
            "run": redact_local_paths(self.run),
            "status": self.status,
            "exitCode": self.exit_code,
            "durationSeconds": round(self.duration_seconds, 3),
        }

    def feedback(self) -> dict[str, Any]:
        """What the executor is told of a failed check (``failedChecks``)."""
        return {**self.metadata(), "run": self.run, "output": self.output}


@dataclass(frozen=True)
class ChecksReport:
    """The checks of one hand-in; ``first`` — the results the fix attempt was given."""

    revision: str
    results: tuple[CheckResult, ...]
    first: tuple[CheckResult, ...] | None = None

    @property
    def status(self) -> str:
        if not self.results:
            return NONE
        return PASSED if all(r.passed for r in self.results) else FAILED

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def metadata(self) -> dict[str, Any]:
        rounds = [self.results] if self.first is None else [self.first, self.results]
        data: dict[str, Any] = {
            "status": self.status,
            "revision": self.revision,
            "fixAttempted": self.first is not None,
            "durationSeconds": round(sum(r.duration_seconds for rs in rounds for r in rs), 3),
            "results": [r.metadata() for r in self.results],
        }
        if self.first is not None:
            data["firstResults"] = [r.metadata() for r in self.first]
        return data

    def summary(self) -> str:
        """One line for a person: which checks failed and how."""
        return ", ".join(f"{r.name} ({_how(r)})" for r in self.failed)


def _how(result: CheckResult) -> str:
    if result.status == TIMED_OUT:
        return "timed out"
    if result.status == NOT_RUN:
        return "not run"
    return f"exit {result.exit_code}"


class ChecksBudget:
    """The time all checks of one hand-in may take together, both rounds.

    Only the checks spend it: the executor's attempt to fix is watched by the
    run's supervision, not by this budget.
    """

    def __init__(self, seconds: float = DEFAULT_BUDGET_SECONDS) -> None:
        self.seconds = seconds
        self.remaining = seconds

    @property
    def spent(self) -> bool:
        return self.remaining <= 0

    def spend(self, seconds: float) -> None:
        self.remaining -= seconds


def checks_at_base(workspace: Workspace) -> ChecksPlan:
    """``checks`` of ``runner.yaml`` at the base the task branch was cut from.

    A catalog run has already read the file there (``workspace.conventions``);
    the one-repository form reads it now, at the base the pool found for the
    copy (``Workspace.conventions_base``). Never at ``base_commit``: that is
    the head of the task branch when the copy is taken over, and a change of
    ``runner.yaml`` there takes effect only once reviewed and merged (FR-009).
    No known base — ``WorkspaceBlocked`` (:data:`CHECKS_BASE_UNKNOWN`). No
    file, or no ``checks`` — an empty plan. A file that cannot be used is
    ``WorkspaceBlocked`` (``runner_config_invalid``).
    """
    conventions = workspace.conventions
    if conventions is not None:
        config = conventions.config
        revision = conventions.revision
    else:
        revision = workspace.base_revision or workspace.conventions_base
        if not revision:
            raise WorkspaceBlocked(
                CHECKS_BASE_UNKNOWN,
                f"the base {workspace.branch} was cut from is unknown, and the checks "
                "are read at the base, never from the task branch; "
                "record the base or recreate the branch",
            )
        config = read_runner_config(workspace.path, revision)
    return ChecksPlan(revision=revision, checks=config.checks if config is not None else ())


def check_environment(
    env: Mapping[str, str] | None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The daemon's environment without the reserved names and secrets, plus the run's."""
    values = os.environ if base is None else base
    kept = {k: v for k, v in values.items() if _kept(k, v)}
    return {**kept, **run_environment(env)}


def _kept(name: str, value: str) -> bool:
    # Upper case whatever the parser matches exactly: a service env never
    # writes gh_token, but the daemon may have it, and a tool may read it.
    entry = reserved_entry(name.upper())
    if entry is not None:
        return entry in KEPT_RESERVED_ENTRIES or name.upper() in KEPT_PROXY_NAMES
    # Masked in the output is not enough: a check runs the executor's code,
    # which can read a token and send it anywhere. A name says nothing of
    # DATABASE_URL, so the value is looked at too.
    return not is_secret_name(name) and not is_secret_value(value)


def is_secret_name(name: str) -> bool:
    """Whether a variable of the daemon named ``name`` holds a secret."""
    upper = name.upper()
    if upper in _NOT_SECRET_NAMES or upper.endswith(_NOT_SECRET_SUFFIXES):
        return False
    return bool(_SECRET_NAME.search(upper))


def is_secret_value(value: str) -> bool:
    """Whether ``value`` carries a secret, whatever the variable is named.

    A URL with a password (``scheme://user:password@``, not ``user@`` nor
    ``user:@``) or a connection string with ``Password=``, ``Pwd=``,
    ``AccountKey=`` or ``SharedAccessKey=`` that is not empty.
    """
    return bool(_url_passwords(value)) or bool(_CONNECTION_SECRET.search(value))


def secrets_of(
    secrets: Iterable[str] = (), base: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The values a check's output must not show, longest first.

    ``secrets`` — those of the run's services (``RunServices.secrets``: the
    passwords and the values that carry them), the daemon's variables named
    as secrets (:func:`is_secret_name`) — whether or not the check got them:
    a check can read a token from a file as well — with the user of a URL
    they hold when no password follows it (``SENTRY_DSN=https://<key>@host``:
    the key is the user; ``postgres`` of ``postgres:<password>@`` is not), and
    the password of a URL in any variable of the daemon. The rest of the
    run's environment (a host, a port, a database name) is no secret.
    """
    values = os.environ if base is None else base
    secret = [v for k, v in values.items() if is_secret_name(k)]
    found = set(secret)
    found.update(u for v in secret for u in _url_users(v))
    found.update(p for v in values.values() for p in _url_passwords(v))
    found.update(secrets)
    return tuple(sorted((v for v in found if len(v) >= _MIN_REDACTED_CHARS), key=len, reverse=True))


def _url_passwords(value: str) -> list[str]:
    return [m.group(2) for m in _USERINFO.finditer(value) if m.group(2)]


def _url_users(value: str) -> list[str]:
    # Only a user with no password after it is a key (``https://<key>@host``);
    # the user of ``postgresql://postgres:<password>@db`` is a plain name, and
    # hiding it would garble every "postgres" in the output.
    return [m.group(1) for m in _USERINFO.finditer(value) if m.group(1) and not m.group(2)]


def redact(text: str, secrets: Iterable[str] = (), base: Mapping[str, str] | None = None) -> str:
    """``text`` with known secrets (:func:`secrets_of`) and URL userinfo hidden."""
    for value in secrets_of(secrets, base):
        text = text.replace(value, REDACTED)
    return _USERINFO.sub(REDACTED + "@", text)


def not_run(check: Check, budget: ChecksBudget) -> CheckResult:
    """The result of a check the budget of the hand-in left no time for."""
    return CheckResult(
        name=check.name,
        run=check.run,
        status=NOT_RUN,
        exit_code=None,
        duration_seconds=0.0,
        output=f"[not run: the checks budget of {budget.seconds:g} s is spent]",
    )


async def run_check(
    check: Check,
    workspace: Workspace,
    env: Mapping[str, str] | None = None,
    *,
    secrets: Iterable[str] = (),
    time_limit: float = DEFAULT_TIMEOUT_SECONDS,
) -> CheckResult:
    """Run one check to its end or for ``time_limit`` seconds.

    ``secrets`` are masked in what it prints (:func:`redact`).

    However it ends — its shell exits, the limit stops it, the run is
    cancelled — its whole process group is killed: a process left in the
    background dies with the check and does not keep its output open.
    """
    started = time.monotonic()
    # A pipe of its own rather than PIPE: asyncio's wait() also waits for the
    # pipes it made to close, and a process the check left in the background
    # holds them open after the shell has exited.
    read_fd, write_fd = os.pipe()
    try:
        process = await asyncio.create_subprocess_exec(
            *SHELL,
            check.run,
            cwd=workspace.path,
            env=check_environment(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=write_fd,
            stderr=write_fd,
            # Its own process group: `make test` starts pytest, and stopping
            # the shell alone would leave the suite running.
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    stream = asyncio.StreamReader()
    pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stream), os.fdopen(read_fd, "rb", buffering=0)
    )
    tail = bytearray()

    async def read() -> None:
        while chunk := await stream.read(65536):
            tail.extend(chunk)
            # Bytes, not characters, but four bytes per character at most.
            del tail[: max(0, len(tail) - 4 * MAX_OUTPUT_CHARS)]

    status: str | None = None
    reader = asyncio.ensure_future(read())
    try:
        await asyncio.wait_for(process.wait(), time_limit)
    except TimeoutError:
        status = TIMED_OUT
    finally:
        # Always, not only when the shell outlived its limit: `sleep 60 &`
        # outlives the shell that started it and holds its output open.
        _kill(process.pid)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
        # With the group gone the pipe closes; what is still buffered is
        # read, for a short while only.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), DRAIN_SECONDS)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        pipe.close()
    exit_code = process.returncode
    if status is None:
        status = PASSED if exit_code == 0 else FAILED
    secrets = tuple(secrets)
    output = redact(tail.decode("utf-8", errors="replace"), secrets)[-MAX_OUTPUT_CHARS:]
    if status == TIMED_OUT:
        output += f"\n[stopped after {time_limit:g} s]"
    return CheckResult(
        name=check.name,
        run=redact(check.run, secrets),
        status=status,
        exit_code=None if status == TIMED_OUT else exit_code,
        duration_seconds=time.monotonic() - started,
        output=output,
    )


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
