"""Setup before the executor (TAI-ADR-0063 §4, TASK-001273).

A repository names how its working copy is installed in ``setup`` of
``.agents/runner.yaml`` (``runner_config.py``). The daemon runs it itself,
after the copy, its neighbours and the run's test services are ready and
before the executor starts — the executor no longer has to guess it from
``AGENTS.md``:

- the command is read at the base the task branch was cut from, never from
  the branch (FR-009), like ``checks`` (``checks.checks_at_base``); no known
  base — setup is not run and the executor installs as before (a run with
  checks on stops earlier, ``checks_base_unknown``);
- it runs exactly like a check (``checks.run_check``): ``sh -c '<setup>'`` in
  the root of the copy, the environment of a check — the daemon's without the
  reserved names and secrets, plus the ``env`` of the run's services —, its
  whole process group killed when it ends, stopped after
  :data:`DEFAULT_TIMEOUT_SECONDS`;
- it is an action of the run (:data:`SETUP_ACTION`) while it goes, so the
  watchdog of the run waits for it; what it prints goes to the daemon's log
  with the secrets of the run and the paths of this host hidden, never into
  durable state;
- a non-zero exit or the time limit fails the run :data:`SETUP_FAILED`, and
  the task goes to a person (``blocked.settle_blocked``) with the first line
  of the error: a repeat on the same base would fail the same way;
- it runs on every run, also on a copy it already ran in: installing an
  installed copy is cheap with a lock file, and skipping it on a hash of
  ``runner.yaml`` and the lock files would miss what is not in them (a
  removed ``.venv``, a cache cleaned by the node). ``setup`` must be
  idempotent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from control_plane_agent.checks import TIMED_OUT, CheckResult, redact, run_check
from control_plane_agent.conventions import read_runner_config
from control_plane_agent.runner_config import RUNNER_CONFIG_PATH, Check
from control_plane_agent.workspace import Workspace, redact_local_paths

logger = logging.getLogger("control_plane_agent.setup")

#: The action setup is recorded as: a running action keeps the watchdog waiting.
SETUP_ACTION = "setup.run"
#: ``failure_reason`` of a run whose setup failed or timed out.
SETUP_FAILED = "setup_failed"
#: How long setup may take before it is stopped and counted failed.
DEFAULT_TIMEOUT_SECONDS = 1800.0
#: How much of the end of setup's output goes to the daemon's log.
MAX_LOGGED_CHARS = 4000
#: How long the line of the error in the reason may be.
MAX_ERROR_LINE_CHARS = 300
# A line that says it is an error: ``error: ...`` of uv, ``ERROR:`` of pip,
# ``npm ERR!``, ``make: *** ... Error 2``.
_ERROR_LINE = re.compile(r"\berror\b|\bERR!", re.IGNORECASE)
# The note run_check appends to the output of a command it stopped.
_STOPPED_NOTE = re.compile(r"^\[stopped after .* s\]$")


@dataclass(frozen=True)
class SetupPlan:
    """``setup`` of ``runner.yaml`` at ``revision``."""

    revision: str
    run: str


def setup_at_base(workspace: Workspace) -> SetupPlan | None:
    """``setup`` of ``runner.yaml`` at the base the task branch was cut from.

    The base is the one the checks are read at (``checks.checks_at_base``).
    None — no file, no ``setup``, or no known base to read it at. A file that
    cannot be used is ``WorkspaceBlocked`` (``runner_config_invalid``).
    """
    conventions = workspace.conventions
    if conventions is not None:
        config = conventions.config
        revision = conventions.revision
    else:
        revision = workspace.base_revision or workspace.conventions_base
        if not revision:
            logger.warning(
                "the base %s was cut from is unknown: setup is not run", workspace.branch
            )
            return None
        config = read_runner_config(workspace.path, revision)
    if config is None or config.setup is None:
        return None
    return SetupPlan(revision=revision, run=config.setup)


async def run_setup(
    plan: SetupPlan,
    workspace: Workspace,
    env: Mapping[str, str] | None = None,
    *,
    secrets: Iterable[str] = (),
    time_limit: float = DEFAULT_TIMEOUT_SECONDS,
) -> CheckResult:
    """Run setup in the copy to its end or for ``time_limit`` seconds, and log what it printed."""
    secrets = tuple(secrets)
    result = await run_check(
        Check(name="setup", run=plan.run),
        workspace,
        env,
        secrets=secrets,
        time_limit=time_limit,
    )
    # run_check has hidden the secrets; the paths of this host stay here too.
    output = redact_local_paths(result.output)[-MAX_LOGGED_CHARS:]
    logger.log(
        logging.INFO if result.passed else logging.WARNING,
        "setup of %s: %s in %.1fs (exit %s)\n%s",
        workspace.key,
        result.status,
        result.duration_seconds,
        result.exit_code,
        output,
    )
    return result


def setup_failure(plan: SetupPlan, result: CheckResult, secrets: Iterable[str] = ()) -> str:
    """The reason a person reads: the command, how it ended, the first line of the error."""
    how = "timed out" if result.status == TIMED_OUT else f"exited {result.exit_code}"
    reason = (
        f"setup `{result.run}` of {RUNNER_CONFIG_PATH} at {plan.revision[:12]} {how}: "
        f"{error_line(result.output)}. Fix the setup or the repository, then return the task"
    )
    return redact_local_paths(redact(reason, secrets))


def error_line(output: str) -> str:
    """The first line of ``output`` that says it is an error, else its last line."""
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not _STOPPED_NOTE.match(line.strip())
    ]
    if not lines:
        return "no output"
    line = next((line for line in lines if _ERROR_LINE.search(line)), lines[-1])
    return line[:MAX_ERROR_LINE_CHARS]
