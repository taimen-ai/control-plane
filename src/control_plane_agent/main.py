"""``control-plane-agent`` — minimal autonomous reference harness.

Purpose: protocol symmetry, not autonomous intelligence. The daemon performs
the SAME discover → claim → run → artifact → complete cycle as the Claude
Code human harness, through the same client SDK against the same endpoints —
no special server path exists for agents.

The unit of pluggable behavior is an Adapter:
``execute(task, run, client, workspace)`` does the actual work and returns
artifact specs. The built-in ``echo`` adapter simply records what it saw —
enough for E2E protocol validation.

Execution workspace (ADR-0016 §5): when the agent is configured with a
workspace pool, every task is executed in its own git worktree on branch
``task/<publicId>``, and the resulting commit is registered as an artifact by
reference (``git:<sha>``). Without a pool the adapter gets ``workspace=None``
and the agent behaves exactly as before — the reference harness stays usable
for protocol tests that touch no files.

Inputs (CP-ADR-0072 §8): with a runtime directory configured, the inputs of
a task — artifacts of other tasks its type declares — are downloaded into
``<runtime>/inputs/<key>/<name>`` before the adapter starts, outside the
working copy, and handed to an adapter whose ``execute`` takes ``inputs``
(``inputs.py``). An adapter written before that keeps its four arguments.

Test services (universal-runner U013, ``services.py``): the services the
task's ``.agents/runner.yaml`` declares at its base are asked of the node
before the adapter starts and released when the run ends; their address and
credentials reach the adapter as ``env`` of this run only. No budget in time
sends the task back to the queue (``test_services_unavailable``); a template
the node does not have sends it to a person (``service_template_unavailable``).

Setup (TAI-ADR-0063 §4, ``setup_command.py``): ``setup`` of the base's
``runner.yaml`` runs in the copy after the services are ready and before the
adapter, in the environment of a check with the services' ``env``; a non-zero
exit or its time limit sends the task to a person (``setup_failed``) and the
adapter never starts. No ``setup`` — the cycle is what it was.

Checks before hand-in (universal-runner U014, ``checks.py``): with ``checks``
on (``workingCopy.checks`` of the agent's description), the ``checks`` of the
base's ``runner.yaml`` run after the adapter; a failed one gives the adapter
one more turn with its output, then the checks run again and the result goes
into ``metadata.checks`` of the ``commit`` artifact, failed or not. Off — the
cycle is what it was.

Acceptance (CP-ADR-0067): the daemon hands work in and nothing more — review
and merge are checks the task type declares, run by the core. A task its
verification returned is taken again with the failed attempt in the prompt
(``lastVerification``) and on its branch ``task/<publicId>``; a task waiting
for a person (category ``blocked``) is left alone. An executor that says it
could not do the work (``blocked.py``) fails its run ``executor_blocked`` and
hands the task to a person instead of completing it.

Restart recovery: on startup the agent consults /harness/context; a still-
live claim+run is finished honestly (fail with reason=restart_recovery) so
the task frees up deterministically — a reference policy, not the only one.

Publishing and replicas (universal-runner U007, ``publish.py``): every push
is checked first — the remote must be the configured repository, and the
host's publish hook must allow it — and a refused target stops the task as
``publish_target_rejected``. Uncommitted work of a run that stops ``blocked``
or is closed by restart recovery is saved as a WIP commit on the task branch
and published (``wip: true`` in the checkpoint, no ``commit`` artifact), so
another replica continues from it. A push that failed, or a hook that could
not check now (exit 75), is kept in the replica's unpublished list and
retried at the start of every cycle. Replicas
of one agent share its queue: candidates of one priority are shuffled, the
ones this replica has a copy of first, and a claim lost to another replica
moves on to the next candidate.

Skills (ADR-0056 §3, §5, ``skills.py``): with a skill executor configured the
same daemon runs skill invocations in its own workers, alongside Work
(``CONTROL_PLANE_SKILLS_CONCURRENCY`` at once; 0 — only while it has no Work),
and executes Work whose type declares ``execution = {skill, version}`` through
exactly one invocation instead of the adapter. Work of such a type is never
handed to the adapter: a daemon that cannot run the skill — or cannot read the
type to tell — leaves it for one that can.

Agent mode (CP-ADR-0073 §8, ``revision.py``): a principal bound to an agent
is configured by its agent's current revision (``GET /agents/me``) instead of
the environment, names that revision on every run it starts, and ends with
exit code 75 once the run in flight is over when a newer revision appears. A
stop request drains: the run in flight gets ``placement.drainSeconds`` to
finish and is then stopped and cancelled, like a cancel request. A principal
that is no agent keeps the env mode.
"""

import asyncio
import contextlib
import inspect
import logging
import math
import os
import random
import signal
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from control_plane_agent.blocked import (
    BLOCKED_CATEGORY,
    FAILURE_REASON,
    blocked_reason,
    settle_blocked,
)
from control_plane_agent.catalog import RepositoryBlocked, RepositoryPools, previous_repository
from control_plane_agent.checks import (
    CHECK_ACTION,
    CHECKS_MOVED_HEAD,
    CHECKS_RESTORE_FAILED,
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    CheckResult,
    ChecksBudget,
    ChecksPlan,
    ChecksReport,
    checks_at_base,
    not_run,
    run_check,
)
from control_plane_agent.comments import own_principal_id, with_comments
from control_plane_agent.inputs import (
    LocalInput,
    discard_inputs,
    fetch_inputs,
    task_runtime_dir,
)
from control_plane_agent.publish import (
    ENV_PUBLISH_HOOK,
    PUBLISH_TARGET_REJECTED,
    PublishHook,
    PublishRejected,
    PublishResult,
    Unpublished,
    UnpublishedLedger,
    hook_from_environment,
    publish_branch,
)
from control_plane_agent.revision import (
    ENV_CONFIG_MODE,
    EXIT_MISCONFIGURED,
    EXIT_REVISION_CHANGED,
    AgentRevision,
    RevisionError,
    config_mode,
    my_agent,
    settings_of,
    skills_of,
    skills_params,
    workspace_pool_of,
)
from control_plane_agent.services import (
    RunServices,
    RunServicesSource,
    ServicesBlocked,
    ServicesUnavailable,
)
from control_plane_agent.setup_command import (
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_SETUP_TIMEOUT_SECONDS,
)
from control_plane_agent.setup_command import (
    SETUP_ACTION,
    SETUP_FAILED,
    SetupPlan,
    run_setup,
    setup_at_base,
    setup_failure,
)
from control_plane_agent.skills import SkillExecutor, executor_from_environment
from control_plane_agent.supervision import (
    DRAINED,
    ExecutionStopped,
    RunSupervisor,
    SupervisionSettings,
    settle_stopped,
)
from control_plane_agent.workspace import (
    ARTIFACT_TYPE,
    CHECKPOINT_KIND,
    NEIGHBOUR_MODIFIED,
    NEIGHBOUR_POINTER_REGRESSED,
    ExecutionWorkspacePool,
    Outcome,
    Workspace,
    WorkspaceBlocked,
    WorkspaceBusyError,
    WorkspaceError,
    assert_portable,
    base_branch_of,
    parse_neighbours,
    redact_local_paths,
)
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    HeartbeatRunner,
    IamCredentialError,
    NotEligibleError,
    PermissionDeniedError,
    SessionExpiredError,
    StaleClaimError,
    is_transient,
    resolve_credential,
)

logger = logging.getLogger("control_plane_agent")

#: How many available Work items one cycle looks at: an item this daemon must
#: not take (a skill it cannot run), or one another replica claimed first,
#: should not hide the next one.
WORK_SCAN = 50
#: How many pages of available Work one cycle reads while every item of the
#: page before was one this daemon does not take: its own Work further down
#: the queue must not wait behind more than a page of someone else's.
WORK_PAGES = 5
#: How long the task types an executor of kind ``skills`` takes are kept
#: before they are read again: a type published later is taken after that.
SKILL_TYPES_REFRESH_SECONDS = 60.0
#: Task types read to find those (``GET /task-types``, page of 100).
SKILL_TYPES_PAGES = 20
#: The most ``typeKey`` values the core takes in one listing; past it the
#: daemon lists without the filter and pages instead.
TYPE_KEYS_MAX = 50
#: What the message of a WIP commit carries: the work saved when a run
#: stopped, not a result handed in (FR-022).
WIP_TRAILER = "Control-Plane-WIP: true"
#: How long an orphaned run whose copy is on another replica is left to that
#: replica's own restart recovery (``Agent.orphan_grace``).
ORPHAN_GRACE_SECONDS = 120.0
#: How often an idle daemon asks whether its agent has a newer revision; after
#: a run it asks right away.
REVISION_CHECK_SECONDS = 30.0
#: How long a repeatable call of the core (a read, a command with an
#: Idempotency-Key) is retried while the core is unreachable — a restart
#: behind the proxy answers 502 for a few seconds. As long as the default
#: claim TTL: past it the lease is gone anyway, and the run is failed then.
RETRY_WINDOW_SECONDS = 300.0


class _TypeUnreadable(Exception):
    """The task's type cannot be read, so whether a skill executes it is unknown."""


def _refused_for_task(exc: PermissionDeniedError) -> bool:
    """Whether a refused claim is about that one task rather than the agent.

    The core asks about ``tasks.claim`` twice: at tenant level, then on the
    task itself (a scoped binding may cover some tasks and not others); the
    refusal names the resource it was asked about (``details.resource``).
    ``not_eligible`` — the task's requirements — is about the task as well.
    Anything else (a refusal at tenant level, one naming no resource, a
    session of another principal) stops the cycle: every claim would fail.
    """
    if isinstance(exc, NotEligibleError) or exc.code == "not_eligible":
        return True
    resource = exc.details.get("resource") if isinstance(exc.details, dict) else None
    return isinstance(resource, str) and resource.startswith("task:")


@dataclass(frozen=True)
class ArtifactSpec:
    type: str
    name: str
    uri: str | None = None
    content: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    """The pluggable execution engine of the autonomous harness.

    ``workspace`` is the isolated working copy for this task, or None when the
    agent runs without a pool. An adapter that touches files MUST work inside
    ``workspace.path`` and nowhere else: that is what keeps two concurrent
    tasks from writing into one copy.

    An adapter may also take a keyword ``inputs`` — the task's inputs as local
    files (``inputs.py``); the daemon passes it only to an adapter that does.
    The adapters of Claude Code and Codex also take a keyword ``env`` — the
    environment of this run (``env`` of the run's services in
    ``.agents/runner.yaml``, placeholders filled): it reaches the executor
    process of this run only and is never kept for the next one. The daemon
    passes it only to an adapter that takes it, and only when the run holds
    services (``services.py``).
    """

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]: ...


class EchoAdapter:
    """Trivial adapter: 'does the work' by describing it. For E2E tests."""

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None = None,
    ) -> list[ArtifactSpec]:
        await client.record_action(run["id"], action="echo.observe")
        return [
            ArtifactSpec(
                type="report",
                name=f"echo of {task['publicId']}",
                content={"echo": task["title"], "attempt": run["attempt"]},
            )
        ]


ADAPTERS: dict[str, type] = {"echo": EchoAdapter}

# Adapters that live outside the daemon are resolved by name and imported only
# when asked for: the reference harness must keep running on a host where no
# vendor CLI is installed, and an unconditional import would make Claude Code
# or Codex a hard dependency of protocol tests that never touch them.
EXTERNAL_ADAPTERS = ("claude-code", "codex")


def build_adapter(name: str) -> Adapter:
    """Instantiate an adapter by name. Raises LookupError if it is unknown."""
    factory = ADAPTERS.get(name)
    if factory is not None:
        return factory()  # type: ignore[no-any-return]
    if name == "claude-code":
        from control_plane_claude import adapter_from_environment

        return adapter_from_environment()
    if name == "codex":
        from control_plane_codex import adapter_from_environment as codex_adapter_from_environment

        return codex_adapter_from_environment()
    raise LookupError(name)


def adapter_for_revision(
    revision: AgentRevision, environ: dict[str, str] | None = None
) -> Adapter | None:
    """The executor a revision names, built from its ``params`` and ``instructions``.

    ``skills`` has no adapter: such an agent runs only the skills of its
    ``skills`` section, and ordinary Work is left to others (None); its only
    params are ``env``, the settings of its skills. A kind
    this daemon does not know, or parameters its adapter refuses, is a
    :class:`RevisionError` — the core stores ``executor.params`` without
    reading them, so the adapter is where they are checked.
    """
    kind = revision.executor_kind
    if kind is None:
        raise RevisionError(f"{revision.label} has no executor: there is nothing to run")
    params = revision.executor_params
    try:
        if kind == "claude-code":
            from control_plane_claude import adapter_from_params

            return adapter_from_params(params, instructions=revision.instructions, environ=environ)
        if kind == "codex":
            from control_plane_codex import adapter_from_params as codex_adapter_from_params

            return codex_adapter_from_params(
                params, instructions=revision.instructions, environ=environ
            )
    except ImportError as exc:
        raise RevisionError(f"executor {kind} is not installed on this runner: {exc}") from exc
    except ValueError as exc:
        raise RevisionError(str(exc)) from exc
    if kind == "skills":
        # Its params are the skills' settings (params.env), checked here and
        # handed to the skill host by skills_environ.
        skills_params(revision)
        return None
    if kind in ADAPTERS:
        if params:
            raise RevisionError(f"executor {kind} takes no params, got {sorted(params)}")
        adapter: Adapter = ADAPTERS[kind]()
        return adapter
    raise RevisionError(f"executor kind {kind!r} is not run by this daemon")


class Agent:
    def __init__(
        self,
        client: ControlPlaneClient,
        adapter: Adapter | None,
        *,
        poll_interval: float = 5.0,
        workspace_id: str | None = None,
        project_id: str | None = None,
        include_subprojects: bool = False,
        heartbeat_interval: float = 60.0,
        max_cycles: int | None = None,
        workspaces: ExecutionWorkspacePool | RepositoryPools | None = None,
        only_assigned: bool = False,
        skills: SkillExecutor | None = None,
        supervision: SupervisionSettings | None = None,
        runtime_dir: Path | None = None,
        task_types: frozenset[str] = frozenset(),
        revision: AgentRevision | None = None,
        drain_seconds: float | None = None,
        publish_hook: PublishHook | None = None,
        rng: random.Random | None = None,
        services: RunServicesSource | None = None,
        clock: Callable[[], float] | None = None,
        orphan_grace: float = ORPHAN_GRACE_SECONDS,
        checks: bool = False,
        checks_budget: float = DEFAULT_BUDGET_SECONDS,
        setup_timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
    ) -> None:
        self.client = client
        # None: ordinary Work is not taken — an agent of kind ``skills`` runs
        # only Work its skills execute, and skill invocations.
        self.adapter = adapter
        # Task type keys taken; empty — any (``work.taskTypes``).
        self.task_types = task_types
        # The revision this process was built from (agent mode), or None (env
        # mode). Named on every run; a newer one ends the process with
        # ``exit_code`` 75 between runs.
        self.revision = revision
        # How long a stop waits for the run in flight before stopping it;
        # None — until it ends (``placement.drainSeconds``).
        self.drain_seconds = drain_seconds
        self._drain_deadline: float | None = None
        self._revision_checked_at = time.monotonic()
        #: What the process should exit with once ``run_forever`` returns.
        self.exit_code = 0
        # Where the inputs of a task are downloaded, one directory per task
        # (CP-ADR-0072 §8). None: nothing is downloaded, and the prompt lists
        # the inputs of the working context without files.
        self.runtime_dir = runtime_dir
        # While the adapter works the daemon watches the run: a cancel request
        # or a run without progress stops the adapter (supervision.py).
        self.supervision = supervision or SupervisionSettings()
        # The skill adapter (ADR-0056 §5): runs invocations when there is no
        # Work, and Work whose type is executed by a skill.
        self.skills = skills
        self._executions: dict[str, dict[str, Any] | None] = {}
        # Keys of the task types whose skill this executor runs (kind
        # ``skills`` only), and the monotonic time they were read.
        self._skill_type_keys: frozenset[str] | None = None
        self._skill_types_read_at = -math.inf
        # This principal's id, read once for the comments of a task
        # (comments.py): its own "blocked" comments are left out of the prompt.
        self._own_principal: str | None = None
        self._unreadable_types: set[str] = set()
        self._session_lock = asyncio.Lock()
        self._skills_stop = asyncio.Event()
        self.poll_interval = poll_interval
        self.workspace_id = workspace_id
        self.project_id = project_id
        self.include_subprojects = include_subprojects
        self.heartbeat_interval = heartbeat_interval
        self.max_cycles = max_cycles
        # Execution workspaces are optional: an adapter that touches no files
        # (protocol tests, echo) needs no working copy at all. A catalog of
        # repositories (``catalog.py``) picks the pool per task.
        self.workspaces = workspaces
        # Asked before every push whether the target may take the branch
        # (``publish.py``); None — only the remote is checked.
        self.publish_hook = publish_hook
        # Branches whose push failed, on the replica's volume beside its copies.
        self.unpublished = UnpublishedLedger(workspaces.root) if workspaces is not None else None
        # Orders candidates of one priority; replicas must not all reach for
        # the same task first.
        self.rng = rng or random.Random()
        # Wall clock of the unpublished list's pauses (they outlive the process).
        self.clock = clock or time.time
        # How long an orphaned run whose copy is not here is left to its own
        # replica before this one closes it; 0 — closed at once.
        self.orphan_grace = orphan_grace
        # run id -> (monotonic deadline, claim id, task id) of those orphaned
        # runs; their tasks are not taken here until they are closed.
        self._foreign_orphans: dict[str, tuple[float, str, str]] = {}
        # Test services of a run (``services.py``): asked of the node before
        # the adapter starts, released when the run ends. None: nothing is
        # asked, whatever ``runner.yaml`` declares.
        self.services = services
        # Run the checks of runner.yaml before hand-in (``checks.py``). Off:
        # the work is handed in as the adapter left it, as before.
        self.checks = checks
        # How long all checks of one hand-in may take together, both rounds.
        self.checks_budget = checks_budget
        # How long ``setup`` of runner.yaml may take before the executor
        # (``setup_command.py``); it runs whenever the base declares it.
        self.setup_timeout = setup_timeout
        # Take only work addressed to this Principal. Off by default, because
        # the reference harness exists to prove the protocol and an unassigned
        # queue is the simplest way to do that — but any runner doing real work
        # wants it on: "claimable by me" and "meant for me" are not the same
        # question, and only the second is a decision somebody made.
        self.only_assigned = only_assigned
        self.session_id: str | None = None
        self._session_heartbeats: HeartbeatRunner | None = None
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Stop taking work; the run in flight gets ``drain_seconds`` to finish."""
        if not self._stop.is_set() and self.drain_seconds is not None:
            self._drain_deadline = time.monotonic() + self.drain_seconds
            logger.info("stopping: the run in flight has %ds to finish", self.drain_seconds)
        self._stop.set()

    def _drain_deadline_of(self) -> float | None:
        return self._drain_deadline

    def _drained(self) -> bool:
        return self._drain_deadline is not None and time.monotonic() >= self._drain_deadline

    async def _revision_is_current(self, *, after_work: bool) -> bool:
        """Between runs: is this process still the one its agent describes?

        A newer revision — exit code 75, so whoever placed the process starts
        it again on the new one. Retired or stopped — exit code 0: nothing is
        to be restarted. A failure to read is not a reason to stop working.
        """
        if self.revision is None:
            return True
        now = time.monotonic()
        if not after_work and now - self._revision_checked_at < REVISION_CHECK_SECONDS:
            return True
        self._revision_checked_at = now
        try:
            current = await my_agent(self.client)
        except ControlPlaneError as exc:
            logger.warning("could not read the agent's revision: %s", exc.code)
            return True
        if current is None:
            logger.warning(
                "%s: the principal is no longer an agent; restarting", self.revision.label
            )
            self.exit_code = EXIT_REVISION_CHANGED
            return False
        if current.retired or current.stopped:
            logger.info(
                "%s is %s; stopping",
                current.label,
                current.status if current.retired else "stopped",
            )
            self.exit_code = 0
            return False
        if current.revision_id != self.revision.revision_id:
            logger.info(
                "agent %s moved from revision %d to %d; restarting on the new one",
                current.key,
                self.revision.revision,
                current.revision,
            )
            self.exit_code = EXIT_REVISION_CHANGED
            return False
        return True

    # -- lifecycle -------------------------------------------------------------

    async def _open_session(self) -> str:
        session = await self.client.open_session(
            client_name="control-plane-agent",
            client_version="0.4.0",
            harness_type="autonomous-agent",
            capabilities=[
                "resume",
                "checkpoints",
                "artifacts.publish",
                *(self.skills.capabilities if self.skills is not None else []),
            ],
            environment={
                "adapter": type(self.adapter).__name__ if self.adapter is not None else "none",
                **({"agent": self.revision.label} if self.revision is not None else {}),
            },
        )
        self.session_id = str(session["id"])
        # Keep the session lease alive across idle polls, not only during a run
        # (an idle daemon whose session expires cannot claim anything).
        if self._session_heartbeats is not None:
            await self._session_heartbeats.stop()
        self._session_heartbeats = HeartbeatRunner(
            self.client, session_id=self.session_id, interval_seconds=self.heartbeat_interval
        )
        self._session_heartbeats.start()
        return self.session_id

    async def _ensure_session(self) -> str:
        """Return a live session, reopening if the current one was lost.

        Serialized: the work cycle and the skill workers share one session,
        and two of them noticing its loss must not open two.
        """
        async with self._session_lock:
            if self.session_id is None:
                return await self._open_session()
            if self._session_heartbeats is not None and self._session_heartbeats.error is not None:
                logger.info("session lease lost; reopening")
                return await self._open_session()
            return self.session_id

    async def recover(self) -> None:
        """Startup recovery: reap ONLY orphaned work of this principal.

        A run/claim is orphaned when its backing session is no longer live —
        i.e. left by a crashed predecessor. Work backed by a still-live session
        may belong to a concurrent sibling instance of the same principal and
        MUST NOT be touched; its lease will expire on its own if it too died.
        """
        context = await self.client.get_context()
        live_sessions = {s["id"] for s in context["activeSessions"]}
        for run in context["activeRuns"]:
            if run["sessionId"] in live_sessions:
                continue
            # Its copy may be here, with work nobody committed: saved and
            # published first, so whoever takes the task continues from it.
            # Its claim died with its session, so the core takes no checkpoint
            # of it any more: the WIP record goes with the failure instead.
            elsewhere, wip = await self._save_orphaned_work(run)
            if elsewhere and self.orphan_grace > 0:
                # Its copy, if any, is on another replica, which may be
                # restarting too: give it the time to save the WIP first.
                self._foreign_orphans[run["id"]] = (
                    time.monotonic() + self.orphan_grace,
                    str(run.get("claimId") or ""),
                    str(run["taskId"]),
                )
                logger.info("orphaned run %s left to its replica for now", run["id"])
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(
                    run["id"],
                    failure_reason="restart_recovery",
                    output={"workspace": wip} if wip is not None else None,
                )
                logger.info("recovered: failed orphaned run %s", run["id"])
        waiting = {claim for _, claim, _ in self._foreign_orphans.values()}
        for claim in context["activeClaims"]:
            if claim["sessionId"] in live_sessions or claim["id"] in waiting:
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.release_claim(claim["id"], reason="restart_recovery")
                logger.info("recovered: released orphaned claim %s", claim["id"])

    async def _skill_worker(self, skills: SkillExecutor) -> None:
        """Take skill invocations one after another until the daemon stops.

        Runs beside the work cycle, so a long Work does not hold the queue;
        an invocation in flight is finished (or its lease lost), not dropped.
        """
        while not self._skills_stop.is_set():
            try:
                worked = await skills.run_once(await self._ensure_session())
            except ControlPlaneError as exc:
                logger.warning("skill worker: %s", exc)
                worked = False
            except Exception:
                logger.exception("skill worker failed; continuing")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._skills_stop.wait(), self.poll_interval)

    async def run_forever(self) -> None:
        await self.recover()
        await self._open_session()
        cycles = 0
        worked = False
        skills = self.skills
        workers = (
            [asyncio.create_task(self._skill_worker(skills)) for _ in range(skills.concurrency)]
            if skills is not None
            else []
        )
        try:
            while not self._stop.is_set():
                if self.max_cycles is not None and cycles >= self.max_cycles:
                    return
                if not await self._revision_is_current(after_work=worked):
                    return
                cycles += 1
                try:
                    worked = await self.run_once()
                except ControlPlaneError as exc:
                    if is_transient(exc):
                        # The IAM exchange precedes every command: its outage
                        # is not the core's, and the operator restarts another
                        # service.
                        unreachable = "IAM" if isinstance(exc, IamCredentialError) else "core"
                        logger.warning("%s unreachable, backing off: %s", unreachable, exc)
                    else:
                        logger.warning("cycle error: %s", exc)
                    worked = False
                if not worked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), self.poll_interval)
        finally:
            self._skills_stop.set()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if self._session_heartbeats is not None:
                await self._session_heartbeats.stop()
            if self.session_id is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.close_session(self.session_id)

    # -- one work cycle --------------------------------------------------------

    async def _execution_of(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """``execution`` of the task's type version, read once per version.

        A principal without ``task_types.read`` cannot tell whether a skill
        executes the task, and handing it to the adapter on a guess would run
        skill work as code: ``_TypeUnreadable``, the task is left alone. Not
        cached, so a right granted later takes effect; logged once per type.
        """
        type_id = str(task.get("typeId") or "")
        if not type_id:
            return None
        if type_id not in self._executions:
            try:
                task_type = await self.client.get_task_type(type_id)
            except PermissionDeniedError as exc:
                if type_id not in self._unreadable_types:
                    self._unreadable_types.add(type_id)
                    logger.warning(
                        "no task_types.read: leaving %s alone, its type %s may be "
                        "executed by a skill",
                        task.get("publicId"),
                        type_id,
                    )
                raise _TypeUnreadable(type_id) from exc
            self._unreadable_types.discard(type_id)
            self._executions[type_id] = task_type.get("execution")
        return self._executions[type_id]

    async def _takes(self, execution: dict[str, Any] | None) -> bool:
        """Ordinary work goes to the adapter; skill work only to a capable executor."""
        if execution is None:
            return self.adapter is not None
        if self.skills is None:
            return False
        try:
            skill = await self.skills.describe(f"{execution['skill']}@{execution['version']}")
        except ControlPlaneError as exc:
            logger.info("skill %s not readable: %s", execution.get("skill"), exc.code)
            return False
        return self.skills.can_execute(skill)

    async def _listing_type_keys(self) -> frozenset[str] | None:
        """The ``typeKey`` filter of the listing; None — no filter.

        Only an executor of kind ``skills`` (no adapter) narrows the listing:
        to the types whose ``execution`` skill it runs — what ``_takes`` would
        decide item by item. Without ``task_types.read``, or with more types
        than one listing takes, it lists as before and pages instead.
        """
        if self.adapter is not None or self.skills is None:
            return None
        now = time.monotonic()
        if now - self._skill_types_read_at < SKILL_TYPES_REFRESH_SECONDS:
            return self._skill_type_keys
        keys = await self._read_skill_type_keys()
        if keys is not None and len(keys) > TYPE_KEYS_MAX:
            logger.warning("%d task types taken: listing every type", len(keys))
            keys = None
        self._skill_type_keys = frozenset(keys) if keys is not None else None
        self._skill_types_read_at = now
        return self._skill_type_keys

    async def _read_skill_type_keys(self) -> set[str] | None:
        """Keys of the task types this skill executor runs; None — unknown."""
        keys: set[str] = set()
        cursor: str | None = None
        try:
            for _ in range(SKILL_TYPES_PAGES):
                params: dict[str, Any] = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                page = await self.client.list_task_types(**params)
                for task_type in page["items"]:
                    key = str(task_type.get("key") or "")
                    if key in keys or (self.task_types and key not in self.task_types):
                        continue
                    if await self._takes(task_type.get("execution")):
                        keys.add(key)
                cursor = page.get("nextCursor")
                if not cursor:
                    return keys
        except PermissionDeniedError as exc:
            logger.warning("task types not readable (%s): listing every type", exc.code)
            return None
        logger.warning("more task types than %d pages: listing every type", SKILL_TYPES_PAGES)
        return None

    async def run_once(self) -> bool:
        """Discover, take ONE task through the full cycle. True if work done.

        Without Work, a daemon whose skill executor has no workers of its own
        (``concurrency = 0``) takes one skill invocation instead.
        """
        session_id = await self._ensure_session()
        await self._publish_pending()
        await self._close_foreign_orphans()
        type_keys = await self._listing_type_keys()
        task: dict[str, Any] | None = None
        execution: dict[str, Any] | None = None
        claim: dict[str, Any] = {}
        cursor: str | None = None
        # An executor of kind ``skills`` with no type it runs has no Work to
        # look for.
        pages = 0 if type_keys is not None and not type_keys else WORK_PAGES
        for _ in range(pages):
            page = await self.client.list_available_work(
                limit=WORK_SCAN,
                cursor=cursor,
                workspace_id=self.workspace_id,
                include_descendants=True,
                project_id=self.project_id,
                include_subprojects=self.include_subprojects,
                assigned_to_me=self.only_assigned,
                type_keys=sorted(type_keys) if type_keys else None,
            )
            for item in self._candidates(page["items"]):
                try:
                    execution = await self._execution_of(item)
                except _TypeUnreadable:
                    continue
                if not await self._takes(execution):
                    continue
                # Autonomous policy: claim automatically (the human harness would ask).
                try:
                    claim = await self.client.claim_task(
                        item["id"], session_id, intent="autonomous-agent auto"
                    )
                except SessionExpiredError:
                    # Our session died between the poll and the claim; drop it so
                    # the next cycle reopens, and treat this cycle as no-op.
                    await self._open_session()
                    return False
                except PermissionDeniedError as exc:
                    if _refused_for_task(exc):
                        # This task only: a scoped binding that does not cover
                        # it, requirements this agent does not meet. The next
                        # candidate may well be allowed.
                        logger.info(
                            "claim of %s forbidden for this task (%s); trying the next",
                            item["publicId"],
                            exc.code,
                        )
                        continue
                    # Not a race: this agent may not claim, and every next
                    # candidate would say the same. The cycle stops here.
                    logger.warning(
                        "claim of %s forbidden (%s); no more claims this cycle",
                        item["publicId"],
                        exc.code,
                    )
                    return False
                except ControlPlaneError as exc:
                    # Another replica of this agent took it since the listing: the
                    # next candidate, not an idle cycle.
                    logger.info("claim lost race for %s: %s", item["publicId"], exc.code)
                    continue
                task = item
                break
            cursor = page.get("nextCursor")
            if task is not None or not cursor:
                break
        if task is None:
            if self.skills is not None and self.skills.concurrency == 0:
                return await self.skills.run_once(session_id)
            return False

        heartbeats = HeartbeatRunner(
            self.client,
            session_id=session_id,
            claim_id=str(claim["id"]),
            interval_seconds=self.heartbeat_interval,
        )
        heartbeats.start()
        workspace: Workspace | None = None
        run_services: RunServices | None = None
        # "suspended" is part of Outcome but never assigned here: today every
        # non-success path ends in fail_run, and the workspace is released the
        # same way ("failed") regardless. It's reserved for graceful
        # degradation (approval gate, budget limit, cancellation) that doesn't
        # exist yet — those will set outcome = "suspended" without changing
        # this release logic.
        outcome: Outcome = "failed"
        try:
            run = await self.client.start_run(
                task["id"],
                claim_id=str(claim["id"]),
                fencing_token=int(claim["fencingToken"]),
                agent_revision_id=self.revision.revision_id if self.revision is not None else None,
            )
            if execution is not None:
                assert self.skills is not None
                return await self._run_skill_work(
                    task, run, execution, session_id, heartbeats, self.skills
                )
            try:
                try:
                    workspace = await self._open_workspace(task, run)
                except (RepositoryBlocked, WorkspaceBlocked) as exc:
                    # No repository to work in (TAI-ADR-0063 §3), a neighbour
                    # an earlier run changed, a runner.yaml of the base that
                    # cannot be used: nothing ran, nothing to publish; a
                    # person fixes the key, the copy or the base.
                    await self._settle_blocked(
                        task, run, claim, exc.reason, failure_reason=exc.code
                    )
                    return True
                except WorkspaceBusyError as exc:
                    # Another process owns this copy. Honest failure for now;
                    # once AR-5 lands this becomes checkpoint → suspend.
                    logger.warning("workspace busy for %s: %s", task["publicId"], exc)
                    with contextlib.suppress(ControlPlaneError):
                        await self.client.fail_run(str(run["id"]), failure_reason="workspace_busy")
                    return False
                try:
                    run_services = await self._open_services(workspace, run)
                except ServicesBlocked as exc:
                    # No template, over the quota, no way to ask: waiting
                    # changes nothing, a person fixes runner.yaml or the node.
                    await self._settle_blocked(
                        task, run, claim, exc.reason, failure_reason=exc.code
                    )
                    return True
                except ServicesUnavailable as exc:
                    # No budget or readiness in time (FR-020): nothing ran;
                    # the task goes back to the queue for a later cycle.
                    await self._requeue(task, run, claim, exc)
                    return False
                if heartbeats.error is not None:
                    # The claim died while the node readied the services: the
                    # server would fence the run, so none of it starts.
                    logger.warning(
                        "lease lost before %s started (%s); aborting",
                        task["publicId"],
                        heartbeats.error.code,
                    )
                    with contextlib.suppress(ControlPlaneError):
                        await self.client.fail_run(str(run["id"]), failure_reason="lease_lost")
                    return False
                if self._drained():
                    # The drain time ran out during the wait: the executor
                    # would be stopped at its first look.
                    raise ExecutionStopped(DRAINED)
                plan: ChecksPlan | None = None
                if self.checks and workspace is not None:
                    try:
                        plan = await asyncio.to_thread(checks_at_base, workspace)
                    except WorkspaceBlocked as exc:
                        # Read before the executor starts: a runner.yaml the
                        # checks cannot come from stops the run before work.
                        await self._settle_blocked(
                            task, run, claim, exc.reason, failure_reason=exc.code
                        )
                        return True
                env = run_services.env if run_services is not None else None
                secrets = run_services.secrets if run_services is not None else ()
                supervisor = RunSupervisor(
                    self.client,
                    str(run["id"]),
                    self.supervision,
                    drain_deadline=self._drain_deadline_of,
                )
                if workspace is not None:
                    try:
                        setup = await asyncio.to_thread(setup_at_base, workspace)
                    except WorkspaceBlocked as exc:
                        await self._settle_blocked(
                            task, run, claim, exc.reason, failure_reason=exc.code
                        )
                        return True
                    if setup is not None:
                        # After the copy, its neighbours and the services, before
                        # the executor (TAI-ADR-0063 §4): a copy that does not
                        # install is not worked in; a person fixes the base.
                        installed = await supervisor.run(
                            self._run_setup(run, workspace, setup, env, secrets)
                        )
                        if not installed.passed:
                            await self._settle_blocked(
                                task,
                                run,
                                claim,
                                setup_failure(setup, installed, secrets),
                                failure_reason=SETUP_FAILED,
                            )
                            return True
                        if heartbeats.error is not None:
                            # The claim died while setup ran: as after the
                            # wait for the services, the executor never starts.
                            logger.warning(
                                "lease lost during setup of %s (%s); aborting",
                                task["publicId"],
                                heartbeats.error.code,
                            )
                            with contextlib.suppress(ControlPlaneError):
                                await self.client.fail_run(
                                    str(run["id"]), failure_reason="lease_lost"
                                )
                            return False
                inputs = await self._fetch_inputs(task, run)
                task = await self._with_comments(await self._with_feedback(task))
                artifacts = await supervisor.run(self._execute(task, run, workspace, inputs, env))
                stopped = await self._after_execution(
                    task, run, claim, workspace, heartbeats, artifacts
                )
                if stopped is not None:
                    return stopped
                report: ChecksReport | None = None
                if plan is not None:
                    assert workspace is not None  # a plan is read from a working copy
                    budget = ChecksBudget(self.checks_budget)
                    try:
                        results = await supervisor.run(
                            self._run_checks(run, workspace, plan, env, budget, secrets)
                        )
                        report = ChecksReport(plan.revision, results)
                        if report.failed and not budget.spent:
                            # One attempt to fix, with what the failed checks
                            # said; then all of them again, and the work goes in
                            # as it is. With the budget spent nothing could tell
                            # the fix worked: the work goes in with the first
                            # results.
                            fix = {**task, "failedChecks": [r.feedback() for r in report.failed]}
                            artifacts = [
                                *artifacts,
                                *await supervisor.run(
                                    self._execute(fix, run, workspace, inputs, env)
                                ),
                            ]
                            stopped = await self._after_execution(
                                task, run, claim, workspace, heartbeats, artifacts
                            )
                            if stopped is not None:
                                return stopped
                            again = await supervisor.run(
                                self._run_checks(run, workspace, plan, env, budget, secrets)
                            )
                            report = ChecksReport(plan.revision, again, first=results)
                    except WorkspaceBlocked as exc:
                        # The checks moved HEAD, or their leftovers could not
                        # be taken away: what a commit would take now is not
                        # the executor's work. Nothing is handed in; the copy
                        # stays for a person.
                        await self._publish(task, run, artifacts)
                        await self._settle_blocked(
                            task, run, claim, exc.reason, failure_reason=exc.code
                        )
                        return True
                if workspace is not None:
                    try:
                        evidence = await self._commit_evidence(task, run, workspace, checks=report)
                    except PublishRejected as rejected:
                        # The branch may not go where it was to go: nothing was
                        # pushed and nothing is handed in; a person fixes the
                        # catalog or the forge, then returns the task.
                        await self._publish(task, run, artifacts)
                        await self._settle_blocked(
                            task,
                            run,
                            claim,
                            f"the branch was not published: {rejected.reason}",
                            failure_reason=PUBLISH_TARGET_REJECTED,
                        )
                        return True
                    artifacts = [*artifacts, *evidence]
                await self._publish(task, run, artifacts)
                if report is not None and report.status == "failed":
                    await self._report_failed_checks(task, run, report)
                await self.client.succeed_run(
                    str(run["id"]),
                    output={"checks": report.metadata()} if report is not None else None,
                )
                outcome = "succeeded"
                if self.runtime_dir is not None:
                    await discard_inputs(task_runtime_dir(self.runtime_dir, task))
                logger.info("completed %s", task["publicId"])
                return True
            except ExecutionStopped as stop:
                # Asked to stop, or stuck: the adapter is stopped; close the run
                # once and let the task go (to the rule's decision, or back to
                # the queue).
                logger.warning("stopped %s: %s", task["publicId"], stop.reason)
                await settle_stopped(
                    self.client,
                    run_id=str(run["id"]),
                    claim_id=str(claim["id"]),
                    fencing_token=int(claim["fencingToken"]),
                    stop=stop,
                )
                return True
            except StaleClaimError:
                # Ownership lost mid-flight: stop writing, report honestly.
                logger.warning("ownership of %s lost; aborting", task["publicId"])
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(str(run["id"]), failure_reason="ownership_lost")
                return False
            except Exception as exc:
                # failure_reason is durable and read in other environments, and
                # this handler catches anything an adapter may raise — including
                # exceptions whose text this package never composed. Redacting
                # here covers what the workspace guard cannot see.
                reason = redact_local_paths(f"{type(exc).__name__}: {exc}")[:500]
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(str(run["id"]), failure_reason=reason)
                raise
        finally:
            try:
                if run_services is not None:
                    # The run is over whatever its outcome: the node counts the
                    # idle time of the services from here.
                    await run_services.close()
            finally:
                try:
                    if workspace is not None:
                        if outcome == "succeeded" and await self._unpublished(workspace):
                            # The copy stays while its branch waits to be pushed
                            # again: the unpublished list only pushes tasks with
                            # a copy here.
                            outcome = "failed"
                        await asyncio.to_thread(
                            self._pool_of(workspace).release, workspace, outcome
                        )
                finally:
                    await heartbeats.stop()

    async def _run_skill_work(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        execution: dict[str, Any],
        session_id: str,
        heartbeats: HeartbeatRunner,
        skills: SkillExecutor,
    ) -> bool:
        """Work executed by a skill (ADR-0056 §3): one invocation, then the run.

        The ``skill_result`` artifact and the task's typed outputs
        (``artifactSchema.outputs``, CP-ADR-0072) are written by the core when
        the call succeeds; the run carries only references. No workspace, no
        adapter: the skill is the whole execution, and what it did
        is decided by its contract, not by this daemon.
        """
        run_id = str(run["id"])
        try:
            invocation = await skills.execute_work(
                task, run, execution, session_id, alive=lambda: heartbeats.error is None
            )
        except StaleClaimError:
            logger.warning("ownership of %s lost; aborting", task["publicId"])
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason="ownership_lost")
            return False
        except ControlPlaneError as exc:
            # The core refused the call (inputs, rights, basis): nothing ran.
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(
                    run_id, failure_reason=f"skill_invocation_rejected: {exc.code}"[:500]
                )
            logger.warning("skill call for %s rejected: %s", task["publicId"], exc.code)
            return False
        except Exception as exc:
            reason = redact_local_paths(f"{type(exc).__name__}: {exc}")[:500]
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason=reason)
            raise
        summary = {
            "skillInvocationId": str(invocation["id"]),
            "skill": f"{execution['skill']}@{execution['version']}",
            "status": invocation.get("status"),
            "artifactId": invocation.get("artifactId"),
        }
        if heartbeats.error is not None:
            logger.warning("lease lost during %s; aborting", task["publicId"])
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason="lease_lost", output=summary)
            return False
        if invocation.get("status") != "succeeded":
            code = (invocation.get("error") or {}).get("code") or invocation.get("status")
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(
                    run_id,
                    failure_reason=f"skill_invocation_{invocation.get('status')}: {code}"[:500],
                    output={**summary, "error": invocation.get("error")},
                )
            logger.info("skill work %s failed: %s", task["publicId"], code)
            return False
        await self.client.succeed_run(run_id, output=summary)
        logger.info("completed %s through %s", task["publicId"], summary["skill"])
        return True

    async def _publish(
        self, task: dict[str, Any], run: dict[str, Any], artifacts: list[ArtifactSpec]
    ) -> None:
        for spec in artifacts:
            # Nothing leaves the host that names this host: an adapter that put
            # a local path or a credential in an artifact is stopped here, not
            # discovered later by a reader.
            assert_portable(
                {
                    "name": spec.name,
                    "uri": spec.uri,
                    "content": spec.content,
                    "metadata": spec.metadata,
                },
                where="artifact",
            )
            await self.client.create_artifact(
                type=spec.type,
                name=spec.name,
                task_ref=task["id"],
                run_id=str(run["id"]),
                uri=spec.uri,
                content=spec.content,
                metadata=spec.metadata,
            )

    async def _settle_blocked(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        claim: dict[str, Any],
        reason: str,
        *,
        failure_reason: str = FAILURE_REASON,
    ) -> None:
        """The executor stopped without doing the work (``blocked.py``).

        Or the daemon did: before the executor started, the task names no
        repository of the catalog, or moved away from a copy that holds work
        (``catalog.py``); after it, the target check refused the branch
        (``publish.py``) — ``failure_reason`` says which. How the task goes
        to a person: :func:`settle_blocked`.
        """
        await settle_blocked(self.client, task, run, claim, reason, failure_reason=failure_reason)

    async def _requeue(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        claim: dict[str, Any],
        exc: ServicesUnavailable,
    ) -> None:
        """No services this time: the run fails, the claim goes, the task waits in the queue."""
        logger.warning("%s: %s", task["publicId"], exc)
        with contextlib.suppress(ControlPlaneError):
            await self.client.fail_run(
                str(run["id"]), failure_reason=exc.code, output={"reason": exc.reason}
            )
        with contextlib.suppress(ControlPlaneError):
            await self.client.release_claim(str(claim["id"]), reason=exc.code)

    async def _open_services(
        self, workspace: Workspace | None, run: dict[str, Any]
    ) -> RunServices | None:
        """The services ``runner.yaml`` of the task's base declares, ready for the run.

        The wait (up to ``waitSeconds`` and a poll more) is supervised like
        the executor: a cancel request, a run ended elsewhere or the end of
        the drain time stop it (:class:`ExecutionStopped`) and withdraw the
        request. The watchdog is off: nothing records actions while the node
        readies the services.
        """
        if self.services is None or workspace is None:
            return None
        assert self.adapter is not None  # only ordinary Work reaches here (_takes)
        supervisor = RunSupervisor(
            self.client,
            str(run["id"]),
            replace(self.supervision, stall_warn_seconds=0, stall_stop_seconds=0),
            drain_deadline=self._drain_deadline_of,
        )
        return await supervisor.run(
            self.services.open(workspace, takes_env=_takes(self.adapter, "env"))
        )

    async def _after_execution(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        claim: dict[str, Any],
        workspace: Workspace | None,
        heartbeats: HeartbeatRunner,
        artifacts: list[ArtifactSpec],
    ) -> bool | None:
        """What stops the hand-in after an adapter turn; None — nothing does.

        Asked after every turn, the fix attempt of the checks included: a
        lease lost, a neighbour changed or an executor that says it is
        blocked end the run the same way whichever turn it was.
        """
        if heartbeats.error is not None:
            # The lease died while the adapter worked: the server would
            # fence us anyway, so stop before writing results.
            logger.warning(
                "lease lost during %s (%s); aborting",
                task["publicId"],
                heartbeats.error.code,
            )
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(str(run["id"]), failure_reason="lease_lost")
            return False
        check = (
            await asyncio.to_thread(workspace.neighbour_check) if workspace is not None else None
        )
        if check is not None and check.changes:
            # Neighbours are read-only (FR-007): the work was done
            # against revisions no checkpoint names, and a change
            # there belongs to a task of that repository. Nothing is
            # committed or published; the copies stay for a person.
            await self._publish(task, run, artifacts)
            changed = {
                **check.changes,
                **{name: r.what for name, r in check.regressions.items()},
            }
            reason = "; ".join(f"neighbour {n} {what}" for n, what in changed.items())
            await self._settle_blocked(
                task,
                run,
                claim,
                f"{reason}. Neighbours are read-only: move the change to a task of "
                "that repository or drop it, then return the task",
                failure_reason=NEIGHBOUR_MODIFIED,
            )
            return True
        if check is not None and check.regressions:
            # The branch would undo a pointer the base moved on (a merge of
            # the base, then ``commit -a`` over the old checkout): nothing is
            # handed in, and the reason tells the next attempt the one
            # checkout that fixes it.
            await self._publish(task, run, artifacts)
            await self._settle_blocked(
                task,
                run,
                claim,
                "; ".join(r.reason() for r in check.regressions.values()),
                failure_reason=NEIGHBOUR_POINTER_REGRESSED,
            )
            return True
        blocked = await blocked_reason(self.client, str(run["id"]))
        if blocked is not None:
            # Stopped, not done: the report goes out, the work so far
            # is saved as WIP for whoever continues — on this replica
            # or another — and nothing is handed in.
            wip = await self._save_wip(workspace, FAILURE_REASON) if workspace is not None else None
            if wip is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.create_checkpoint(
                        str(run["id"]), kind=CHECKPOINT_KIND, data=wip
                    )
            await self._publish(task, run, artifacts)
            await self._settle_blocked(task, run, claim, blocked)
            return True
        return None

    async def _run_checks(
        self,
        run: dict[str, Any],
        workspace: Workspace,
        plan: ChecksPlan,
        env: Mapping[str, str] | None,
        budget: ChecksBudget,
        secrets: Sequence[str] = (),
    ) -> tuple[CheckResult, ...]:
        """Every check of the plan, each an action of the run while it goes.

        A running action is what keeps the watchdog from taking a long test
        suite for a stuck run (``supervision.py``). The checks share
        ``budget``; one it leaves no time for is not run. What they leave in
        the copy is taken away after them: only the executor's work is
        committed. A copy that cannot be put back, or whose HEAD the checks
        moved, is ``WorkspaceBlocked`` (:data:`CHECKS_RESTORE_FAILED`,
        :data:`CHECKS_MOVED_HEAD`); an error of the checks themselves is
        raised as it is, whatever the putting back gives.
        """
        head = await asyncio.to_thread(workspace.head_state)
        snapshot = await asyncio.to_thread(workspace.snapshot)
        try:
            results = await self._each_check(run, workspace, plan, env, budget, secrets)
        except BaseException:
            try:
                await asyncio.to_thread(workspace.restore, snapshot)
            except Exception:
                logger.exception("could not put %s back after its checks", workspace.key)
            raise
        try:
            await asyncio.to_thread(workspace.restore, snapshot)
        except (WorkspaceError, OSError) as exc:
            raise WorkspaceBlocked(
                CHECKS_RESTORE_FAILED,
                f"what the checks left in the copy could not be taken away ({exc}); "
                "a commit would take it for the executor's work",
            ) from exc
        moved = await asyncio.to_thread(workspace.head_state)
        if moved != head:
            raise WorkspaceBlocked(
                CHECKS_MOVED_HEAD,
                f"the checks moved HEAD of the copy from {_where(head)} to {_where(moved)} "
                "(a commit or a checkout in a check); a check must leave the branch "
                "as it found it: fix the check at the base, then return the task",
            )
        return results

    async def _run_setup(
        self,
        run: dict[str, Any],
        workspace: Workspace,
        plan: SetupPlan,
        env: Mapping[str, str] | None,
        secrets: Sequence[str] = (),
    ) -> CheckResult:
        """``setup`` of the base in the copy, an action of the run while it goes."""
        run_id = str(run["id"])
        action: dict[str, Any] | None = None
        try:
            action = await self.client.record_action(
                run_id, action=SETUP_ACTION, status="started", metadata={"revision": plan.revision}
            )
        except ControlPlaneError as exc:
            # As for a check: a core that does not answer for a moment does
            # not stop the run; a lost claim or an ended run does.
            if not record_failure_tolerated(exc):
                raise
            logger.warning("could not record setup: %s", exc)
        result = await run_setup(
            plan, workspace, env, secrets=secrets, time_limit=self.setup_timeout
        )
        if action is not None:
            with contextlib.suppress(ControlPlaneError):
                await self.client.finish_action(
                    run_id, str(action["id"]), status="completed" if result.passed else "failed"
                )
        return result

    async def _each_check(
        self,
        run: dict[str, Any],
        workspace: Workspace,
        plan: ChecksPlan,
        env: Mapping[str, str] | None,
        budget: ChecksBudget,
        secrets: Sequence[str] = (),
    ) -> tuple[CheckResult, ...]:
        run_id = str(run["id"])
        results: list[CheckResult] = []
        for check in plan.checks:
            if budget.spent:
                results.append(not_run(check, budget))
                continue
            action: dict[str, Any] | None = None
            try:
                action = await self.client.record_action(
                    run_id, action=CHECK_ACTION, status="started", metadata={"check": check.name}
                )
            except ControlPlaneError as exc:
                # The work is done by now: a core that does not answer for a
                # moment must not throw it away. Anything else is not ours to
                # swallow (a lost claim, a run no longer active).
                if not record_failure_tolerated(exc):
                    raise
                logger.warning("could not record check %s: %s", check.name, exc)
            result = await run_check(
                check,
                workspace,
                env,
                secrets=secrets,
                time_limit=min(DEFAULT_TIMEOUT_SECONDS, budget.remaining),
            )
            budget.spend(result.duration_seconds)
            logger.info(
                "check %s of %s: %s in %.1fs",
                check.name,
                workspace.key,
                result.status,
                result.duration_seconds,
            )
            if action is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.finish_action(
                        run_id, str(action["id"]), status="completed" if result.passed else "failed"
                    )
            results.append(result)
        return tuple(results)

    async def _report_failed_checks(
        self, task: dict[str, Any], run: dict[str, Any], report: ChecksReport
    ) -> None:
        """Handed in red: said where a reviewer looks, not only in the metadata."""
        logger.warning("%s handed in with failed checks: %s", task["publicId"], report.summary())
        with contextlib.suppress(ControlPlaneError):
            await self.client.add_task_comment(
                str(task["id"]),
                body=(
                    "Checks before hand-in failed"
                    + (" after one attempt to fix them" if report.first is not None else "")
                    + f": {report.summary()}. The work is handed in with the failure recorded "
                    "in metadata.checks of the commit artifact."
                ),
                run_id=str(run["id"]),
            )

    async def _with_feedback(self, task: dict[str, Any]) -> dict[str, Any]:
        """The task with ``lastVerification`` when its newest attempt failed.

        A task returned by its verification (a rejected review, a failed merge)
        is taken again by the same executor; what the reviewer or the check
        said is rendered into the prompt (``instructions.py``). Only the brief
        of the attempt comes with the task, so the attempt itself is read here.
        """
        brief = task.get("verification")
        if not isinstance(brief, dict) or brief.get("status") != "failed":
            return task
        try:
            page = await self.client.list_task_verifications(str(task["id"]), limit=1)
        except ControlPlaneError as exc:
            logger.info("verification of %s not readable: %s", task["publicId"], exc.code)
            return task
        items = page.get("items") or []
        if not items or items[0].get("status") != "failed":
            return task
        return {**task, "lastVerification": items[0]}

    async def _with_comments(self, task: dict[str, Any]) -> dict[str, Any]:
        """The task with its comments for the prompt (``comments.py``)."""
        if self._own_principal is None:
            self._own_principal = await own_principal_id(self.client)
        return await with_comments(self.client, task, own_principal=self._own_principal)

    # -- execution workspace ---------------------------------------------------

    async def _fetch_inputs(
        self, task: dict[str, Any], run: dict[str, Any]
    ) -> list[LocalInput] | None:
        """The task's inputs on disk, or None without a runtime directory."""
        if self.runtime_dir is None:
            return None
        runtime = task_runtime_dir(self.runtime_dir, task)
        return await fetch_inputs(self.client, task, str(run["id"]), runtime)

    def _execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        workspace: Workspace | None,
        inputs: list[LocalInput] | None,
        env: Mapping[str, str] | None = None,
    ) -> Coroutine[Any, Any, list[ArtifactSpec]]:
        assert self.adapter is not None  # only ordinary Work reaches here (_takes)
        extra: dict[str, Any] = {}
        if inputs is not None and _takes(self.adapter, "inputs"):
            extra["inputs"] = inputs
        if env:
            # Only a run holding services has one, and only an adapter that
            # takes it gets services at all (RunServicesSource.open).
            extra["env"] = env
        return self.adapter.execute(task, run, self.client, workspace, **extra)

    def _pool_of(self, workspace: Workspace) -> ExecutionWorkspacePool:
        """The pool that handed out ``workspace``: the one, or its key's in a catalog."""
        assert self.workspaces is not None  # a workspace comes from a pool
        if isinstance(self.workspaces, RepositoryPools):
            return self.workspaces.pool_for(
                self.workspaces.catalog.entries[workspace.repository_key]
            )
        return self.workspaces

    async def _open_workspace(self, task: dict[str, Any], run: dict[str, Any]) -> Workspace | None:
        """Take this task's working copy and record it durably.

        The checkpoint is what makes the copy survive a restart: a later run of
        the same task finds the branch here instead of forking a second copy.
        With a catalog it also carries the repository key, which is how the
        next run tells that the task moved to another repository.
        """
        if self.workspaces is None:
            return None
        if isinstance(self.workspaces, RepositoryPools):
            pools = self.workspaces
            entry = pools.catalog.entry_of(task)
            context = await self.client.get_run_context(str(run["id"]))
            previous = previous_repository(list(context.get("checkpoints") or []), CHECKPOINT_KIND)
            await asyncio.to_thread(pools.settle_change, task["publicId"], previous, entry)
            pool = await asyncio.to_thread(pools.pool_for, entry)
        else:
            pool = self.workspaces
        workspace = await asyncio.to_thread(pool.acquire, task["publicId"], base_branch_of(task))
        await self.client.create_checkpoint(
            str(run["id"]), kind=CHECKPOINT_KIND, data=workspace.checkpoint_data
        )
        logger.info(
            "workspace %s on %s (%s)",
            workspace.key,
            workspace.branch,
            "reused" if workspace.reused else "created",
        )
        return workspace

    async def _commit_evidence(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        workspace: Workspace,
        *,
        checks: ChecksReport | None = None,
    ) -> list[ArtifactSpec]:
        """Turn the working copy into evidence: a commit, referenced not copied.

        The branch is then published, when the pool has a remote, so the work
        can be reviewed where code is normally reviewed. ``checks`` — what the
        checks before hand-in gave, when they ran — goes into the metadata
        beside the fields it always had. Publishing is the
        DAEMON's job and not the agent's for the same reason completion is: this
        process holds the claim and its fencing token, so what it publishes is
        attributable to the run that produced it.
        """
        summary = f"{task['publicId']}: {task['title']}"
        sha = await asyncio.to_thread(workspace.commit, summary)
        if await asyncio.to_thread(_is_wip, workspace):
            # A run that continued saved work and added nothing (a copy taken
            # again starts at the WIP, so ``commit`` may even see no change).
            # WIP is never the result (FR-022): the result is a commit of the
            # task on top of it, which is what review and merge take.
            sha = await asyncio.to_thread(workspace.commit, summary, allow_empty=True)
            logger.info("%s: handing in the work saved before", workspace.key)
        if sha is None:
            logger.info("no changes in %s; nothing to commit", workspace.key)
            return []

        pool = self._pool_of(workspace)
        remote = pool.push_remote
        published = False
        # Where the branch lives and what it is meant to be merged into: the
        # acceptance of the task type hands both to a merge skill (CP-ADR-0067,
        # amendment 2026-09-27), which cannot guess them.
        location: dict[str, str] = {}
        if remote:
            result = await self._push(pool, workspace.branch, workspace.key)
            if result.outcome == "rejected":
                await self.client.create_checkpoint(
                    str(run["id"]),
                    kind=CHECKPOINT_KIND,
                    data={
                        **workspace.checkpoint_data,
                        "head": sha,
                        "published": False,
                        "publishRejected": True,
                    },
                )
                raise PublishRejected(result.reason)
            published = result.published
            logger.info(
                "%s %s", "published" if published else "could not publish", workspace.branch
            )
            repository = await asyncio.to_thread(pool.push_remote_url)
            location["repository"] = repository or ""
            # The branch the copy was cut from: a task's feature branch
            # (``customFields.baseBranch``) is where its work is merged back.
            location["targetBranch"] = workspace.base_branch or await asyncio.to_thread(
                lambda: pool.base_branch
            )

        data = {**workspace.checkpoint_data, "head": sha, "published": published}
        if workspace.conventions is not None:
            # The prose conventions the work was handed in with, beside the
            # base's (FR-011): a reviewer sees whether the task changed them.
            data["agentsMdHead"] = await asyncio.to_thread(workspace.agents_md_at, sha)
        await self.client.create_checkpoint(str(run["id"]), kind=CHECKPOINT_KIND, data=data)
        return [
            ArtifactSpec(
                type=ARTIFACT_TYPE,
                name=f"{workspace.branch}@{sha[:12]}",
                uri=workspace.artifact_uri(sha),
                metadata={
                    "branch": workspace.branch,
                    "commit": sha,
                    "workspaceKey": workspace.key,
                    # A reviewer needs to know whether the branch is actually in
                    # the forge: a commit that exists only on the runner cannot
                    # be reviewed, and silence about that would waste their time.
                    "published": published,
                    **{k: v for k, v in location.items() if v},
                    **({"checks": checks.metadata()} if checks is not None else {}),
                },
            )
        ]

    # -- publishing and replicas ----------------------------------------------

    async def _push(
        self, pool: ExecutionWorkspacePool, branch: str, task_key: str
    ) -> PublishResult:
        """Publish ``branch`` through the target check; a failed push is kept to retry."""
        result = await asyncio.to_thread(publish_branch, pool, branch, self.publish_hook)
        ledger = self.unpublished
        if ledger is None:
            return result
        if result.outcome == "failed":
            head = await asyncio.to_thread(pool.branch_head, branch)
            if head is not None:
                entry = Unpublished(
                    task_key, branch, head, pool.repository_key, reason=result.reason
                )
                await self._record_failure(ledger, entry)
        else:
            await asyncio.to_thread(ledger.discard, pool.repository_key, branch)
        return result

    async def _record_failure(self, ledger: UnpublishedLedger, entry: Unpublished) -> None:
        """Keep a failed push to retry; give it up where a person sees it once exhausted.

        A branch that failed :data:`publish.RETRY_MAX_FAILURES` times, or for
        :data:`publish.RETRY_MAX_AGE_SECONDS`, is off the list (``ledger.add``
        drops it) and the task gets a comment with the last reason: the
        forge keeps refusing, and a person fixes it and returns the task. The
        branch stays in the mirror.
        """
        now = self.clock()
        recorded = await asyncio.to_thread(ledger.add, entry, now=now)
        if not recorded.exhausted(now):
            return
        reason = recorded.reason or "unknown"
        logger.warning(
            "%s: %s not published after %d attempt(s), giving up: %s",
            recorded.task,
            recorded.branch,
            recorded.failures,
            reason,
        )
        try:
            await self.client.add_task_comment(
                recorded.task,
                body=(
                    f"The branch {recorded.branch} was not published after "
                    f"{recorded.failures} attempt(s): {reason}. The runner no longer retries "
                    f"it; commit {recorded.commit[:12]} stays on the runner. Fix the forge "
                    "or the credentials and return the task."
                ),
            )
        except ControlPlaneError as exc:
            logger.warning(
                "%s: could not comment on the given-up branch: %s", recorded.task, exc.code
            )

    async def _publish_pending(self) -> None:
        """Push again what an earlier push could not publish (FR-023).

        At the start of every cycle, before work is looked for; an entry is
        tried once its pause is over (``publish.retry_delay``). The branch
        goes as it is now — a later run may have added to it. The list is on
        a volume an executor can write, so it only names what to push again:
        an entry is pushed only to the task branch of its own task, and only
        when that task has a copy on this replica. A branch that keeps
        failing is given up (:meth:`_record_failure`). An entry
        whose branch or repository is gone, whose target is refused, or whose
        published branch is not an ancestor of the local one (a person
        rewrote it; no push can succeed) is dropped: retrying cannot help it.
        A failure of one entry never stops the cycle.
        """
        ledger = self.unpublished
        if ledger is None:
            return
        now = self.clock()
        for entry in await asyncio.to_thread(ledger.entries):
            if not entry.due(now):
                continue
            try:
                result = await self._publish_again(ledger, entry, now)
            except Exception:
                logger.exception("%s: could not publish %s again", entry.task, entry.branch)
                continue
            if result is None:
                continue
            if result.published:
                logger.info("%s: %s published on retry", entry.task, entry.branch)
            elif result.outcome == "rejected":
                logger.warning("%s: %s may not be published; dropped", entry.task, entry.branch)
            elif result.outcome == "local":
                logger.info("%s: %s is no longer published anywhere", entry.task, entry.branch)
                await asyncio.to_thread(ledger.discard, entry.repository_key, entry.branch)

    async def _publish_again(
        self, ledger: UnpublishedLedger, entry: Unpublished, now: float
    ) -> PublishResult | None:
        """One entry of the unpublished list; None when it was settled without a push."""

        async def drop(why: str) -> None:
            logger.warning(
                "%s: %s %s; dropped from the unpublished list", entry.task, entry.branch, why
            )
            await asyncio.to_thread(ledger.discard, entry.repository_key, entry.branch)

        pool = await asyncio.to_thread(self._pool_named, entry.repository_key)
        if pool is None or not pool.is_task_branch(entry.branch):
            await drop("is not a task branch here")
            return None
        if pool.branch_for(entry.task) != entry.branch:
            await drop("is not the branch of the task")
            return None
        if not await asyncio.to_thread(pool.has_copy, entry.task):
            # The list is on a volume the executor can write: only a task
            # that ran on this replica — its copy is kept while the branch is
            # unpublished — is pushed from here.
            await drop("has no copy on this replica")
            return None
        if await asyncio.to_thread(pool.branch_head, entry.branch) is None:
            await drop("is no longer here")
            return None
        state = await asyncio.to_thread(pool.published_state, entry.branch)
        if state == "unreachable":
            # The forge did not answer: the pause grows, nobody else is asked.
            await self._record_failure(ledger, replace(entry, reason="the forge did not answer"))
            return None
        if state == "same":
            await asyncio.to_thread(ledger.discard, entry.repository_key, entry.branch)
            logger.info("%s: %s is already published", entry.task, entry.branch)
            return None
        if state == "diverged":
            await drop("in the forge is not an ancestor of the local one (rewritten there?)")
            return None
        return await self._push(pool, entry.branch, entry.task)

    async def _unpublished(self, workspace: Workspace) -> bool:
        """Whether the branch of ``workspace`` is on the unpublished list."""
        ledger = self.unpublished
        if ledger is None:
            return False
        key = self._pool_of(workspace).repository_key
        try:
            entries = await asyncio.to_thread(ledger.entries)
        except OSError:
            return False
        return any((e.repository_key, e.branch) == (key, workspace.branch) for e in entries)

    def _pool_named(self, repository_key: str) -> ExecutionWorkspacePool | None:
        """The pool of a catalog key ("" — the one-repository pool), if this host has it."""
        pools = self.workspaces
        if isinstance(pools, RepositoryPools):
            entry = pools.catalog.entries.get(repository_key)
            return pools.existing_pool(entry) if entry is not None else None
        return pools if not repository_key else None

    async def _save_wip(self, workspace: Workspace, why: str) -> dict[str, Any] | None:
        """Commit what the copy holds as WIP and publish it (FR-022); the record, or None.

        The commit is the work so far, not a result: its record says ``wip:
        true`` — the ``execution.workspace`` checkpoint, or the output of a run
        closed by restart recovery — and no ``commit`` artifact is made, so
        neither a review nor a merge takes it. A moved submodule pointer is not
        part of it (:meth:`Workspace.commit_wip`). A copy with nothing
        uncommitted but commits of the executor's own is published without a
        WIP commit, and its record has no ``wip``. None when there is nothing
        to save. Best-effort: the run is being stopped anyway, and what could not
        be saved stays in the copy.
        """
        try:
            sha = await asyncio.to_thread(
                workspace.commit_wip, f"{workspace.key}: work in progress ({why})\n\n{WIP_TRAILER}"
            )
            # Nothing uncommitted: commits the executor made itself are still
            # published — they are the work so far — but they are its own, not
            # a WIP commit, and the record does not say ``wip``.
            head = sha if sha is not None else await asyncio.to_thread(workspace.head)
            if head == workspace.base_commit:
                return None
            pool = self._pool_of(workspace)
            result = await self._push(pool, workspace.branch, workspace.key)
        except Exception as exc:
            logger.warning(
                "%s: could not save the work in progress: %s",
                workspace.key,
                redact_local_paths(f"{type(exc).__name__}: {exc}")[:300],
            )
            return None
        logger.info(
            "%s: work in progress saved as %s (%s)", workspace.key, head[:12], result.outcome
        )
        data: dict[str, Any] = {
            **workspace.checkpoint_data,
            "head": head,
            "published": result.published,
        }
        if sha is not None:
            data["wip"] = True
        if result.outcome == "rejected":
            data["publishRejected"] = True
        return data

    async def _save_orphaned_work(self, run: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """WIP of an orphaned run whose copy is on this replica.

        ``(elsewhere, record)``: ``elsewhere`` — its copy may be on another
        replica (this one has no copy of it, or not its repository), so that
        replica should be the one to close it; ``record`` — the WIP record
        when the copy was here and held work, else None.
        """
        if self.workspaces is None:
            return False, None
        try:
            task = await self.client.get_task(str(run["taskId"]))
            key = str(task["publicId"])
            repository_key = ""
            if isinstance(self.workspaces, RepositoryPools):
                context = await self.client.get_run_context(str(run["id"]))
                checkpoints = list(context.get("checkpoints") or [])
                repository_key = previous_repository(checkpoints, CHECKPOINT_KIND) or ""
                if not repository_key:
                    return False, None  # no copy of it was ever recorded
            pool = await asyncio.to_thread(self._pool_named, repository_key)
            if pool is None:
                return True, None
            workspace = await asyncio.to_thread(pool.reopen, key)
        except (ControlPlaneError, WorkspaceError) as exc:
            logger.warning("could not look for the copy of run %s: %s", run["id"], exc)
            return False, None
        if workspace is None:
            return True, None  # its copy is on another replica, or there is none
        try:
            return False, await self._save_wip(workspace, "restart_recovery")
        finally:
            await asyncio.to_thread(pool.release, workspace, "failed")

    async def _close_foreign_orphans(self) -> None:
        """Close orphaned runs left to their own replica once their pause is over.

        Replicas restarted together each see the others' orphaned runs; the
        one that has the copy saves its WIP, and the others wait
        ``orphan_grace`` seconds for it (SC-007), not taking the task either:
        a claim of a dead session does not hold it, and a new run would
        supersede the orphaned one before its work is saved. A run still
        orphaned after that is closed without a record, and its claim
        released: its replica is gone.
        """
        now = time.monotonic()
        due = {run: claim for run, (at, claim, _) in self._foreign_orphans.items() if at <= now}
        if not due:
            return
        context = await self.client.get_context()
        for run_id in due:
            self._foreign_orphans.pop(run_id, None)
        live_sessions = {s["id"] for s in context["activeSessions"]}
        for run in context["activeRuns"]:
            if run["id"] not in due or run["sessionId"] in live_sessions:
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run["id"], failure_reason="restart_recovery")
                logger.info("recovered: failed orphaned run %s of another replica", run["id"])
        claims = set(due.values())
        for claim in context["activeClaims"]:
            if claim["id"] not in claims or claim["sessionId"] in live_sessions:
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.release_claim(claim["id"], reason="restart_recovery")
                logger.info("recovered: released orphaned claim %s", claim["id"])

    def _candidates(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The listed Work this daemon may take, in the order to try it."""
        allowed = [item for item in items if self._may_take(item)]
        return order_candidates(allowed, own=self._has_copy, rng=self.rng)

    def _may_take(self, item: dict[str, Any]) -> bool:
        if self.task_types and item.get("typeKey") not in self.task_types:
            return False
        if any(item.get("id") == task for _, _, task in self._foreign_orphans.values()):
            # Left to the replica that has its copy (``recover``): taking it
            # now would supersede the run before its WIP is saved.
            return False
        if item.get("systemStatusCategory") == BLOCKED_CATEGORY:
            # Claimable, but waiting for a person (an executor stopped on it,
            # or its verification failed too often): the status says so, and
            # a runner takes it only once a person returns it.
            return False
        # Unassigned work is not taken by a runner that takes its own queue,
        # whatever the listing returned (TAI-ADR-0063, decision 7).
        return not (self.only_assigned and not item.get("assigneeId"))

    def _has_copy(self, item: dict[str, Any]) -> bool:
        """Whether this replica holds a container of the task (work it may resume)."""
        if self.workspaces is None:
            return False
        name = str(item.get("publicId") or "")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return False
        return (self.workspaces.root / name).is_dir()


def order_candidates(
    items: list[dict[str, Any]],
    *,
    own: Callable[[dict[str, Any]], bool],
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Candidates in the order to try them: priority first, then soft preferences.

    The listing is by priority; that order is kept. Within one priority the
    candidates are shuffled, so replicas of one agent do not all reach for
    the same task, and those ``own`` says this replica has a copy of come
    first — a soft preference: it never outranks a higher priority.
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item.get("priority"), []).append(item)
    ordered: list[dict[str, Any]] = []
    for group in groups.values():
        mine = [item for item in group if own(item)]
        others = [item for item in group if not own(item)]
        rng.shuffle(mine)
        rng.shuffle(others)
        ordered += mine + others
    return ordered


def warn_without_publish_hook(workspaces: Any, hook: PublishHook | None) -> bool:
    """Warn when an agent with a catalog starts without the host's publish hook.

    Without it only the push address is checked against the catalog, not
    what the forge says about the repository (a public one, a renamed one);
    a host that forgot the variable should hear of it at start, not after a
    branch went where it should not. True when the warning was given.
    """
    if hook is not None or not isinstance(workspaces, RepositoryPools):
        return False
    logger.warning(
        "%s is not set: branches of the catalog are checked only against its addresses",
        ENV_PUBLISH_HOOK,
    )
    return True


def _is_wip(workspace: Workspace) -> bool:
    """Whether HEAD of the copy is a WIP commit saved by an earlier run."""
    return WIP_TRAILER in workspace.head_message()


def record_failure_tolerated(exc: ControlPlaneError) -> bool:
    """Whether a failed record of a check may be let go: the core may answer later.

    No answer at all (:func:`~control_plane_client.is_transient`), too many
    requests, or a server error; a refusal — a lost claim, a run no longer
    active, a denied permission — is not. Broader than ``is_transient`` on
    purpose: the record is bookkeeping, and the work is done by then.
    """
    return is_transient(exc) or exc.status == 429 or exc.status >= 500


def _where(head: tuple[str, str]) -> str:
    ref, commit = head
    return f"{ref.removeprefix('refs/heads/')}@{commit[:12]}"


def _takes(adapter: Adapter, keyword: str) -> bool:
    """Whether ``adapter.execute`` takes ``keyword`` (``inputs``, ``env``)."""
    try:
        parameters = inspect.signature(adapter.execute).parameters
    except (TypeError, ValueError):  # pragma: no cover - a builtin callable
        return False
    return keyword in parameters


def _runtime_dir_from_env(
    pool: ExecutionWorkspacePool | RepositoryPools | None,
) -> Path:  # pragma: no cover
    """Where inputs go: ``CONTROL_PLANE_AGENT_RUNTIME_DIR``, else beside the copies.

    ``.runtime`` under the pool root cannot be a task's container — a task key
    never starts with a dot — and the pool prunes only directories holding a
    working copy.
    """
    configured = os.environ.get("CONTROL_PLANE_AGENT_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    if pool is not None:
        return pool.root / ".runtime"
    return Path.home() / ".control-plane-agent" / "runtime"


def _workspace_pool_from_env() -> ExecutionWorkspacePool | None:  # pragma: no cover - wiring
    """Build the workspace pool if the runner was given a repository to work in.

    Note the naming: ``CONTROL_PLANE_AGENT_WORKSPACE`` is the Control Plane
    Workspace to take tasks from, an entirely different thing from the local
    working copies configured here.
    """
    origin = os.environ.get("CONTROL_PLANE_AGENT_REPO", "")
    root = os.environ.get("CONTROL_PLANE_AGENT_WORKTREE_ROOT", "")
    if not origin or not root:
        return None
    return ExecutionWorkspacePool(
        origin,
        root,
        base_ref=os.environ.get("CONTROL_PLANE_AGENT_BASE_REF", "HEAD"),
        keep_on_success=os.environ.get("CONTROL_PLANE_AGENT_KEEP_WORKSPACES") == "1",
        max_workspaces=int(os.environ.get("CONTROL_PLANE_AGENT_MAX_WORKSPACES", "8")),
        # Named, not a boolean: a runner may legitimately publish somewhere
        # other than the remote it clones from, and an empty value keeps the
        # work local — the default, since publishing needs a credential.
        push_remote=os.environ.get("CONTROL_PLANE_AGENT_PUSH_REMOTE", ""),
        # Neighbours the copy must build against, and the superproject that
        # says at which revision each of them. Both are configuration of the
        # deployment: which repositories sit on this runner is not something
        # the agent may decide per task.
        repo_dir=os.environ.get("CONTROL_PLANE_AGENT_REPO_DIR", ""),
        neighbours=parse_neighbours(os.environ.get("CONTROL_PLANE_AGENT_NEIGHBOURS", "")),
        superproject=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT") or None,
        superproject_ref=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT_REF", "HEAD"),
        superproject_remote=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT_REMOTE", ""),
    )


def _agent_from_revision(
    client: ControlPlaneClient, revision: AgentRevision
) -> Agent:  # pragma: no cover - wiring
    """Agent mode: everything the revision says, the host's environment for the rest."""
    environ = dict(os.environ)
    adapter = adapter_for_revision(revision, environ)
    skills = skills_of(revision, client, environ)
    if adapter is None and skills is None:
        raise RevisionError(f"{revision.label}: executor skills without a skills section")
    pool = workspace_pool_of(revision, environ)
    try:
        hook = hook_from_environment(environ)
    except ValueError as exc:
        raise RevisionError(f"publish hook: {exc}") from exc
    warn_without_publish_hook(pool, hook)
    try:
        services = RunServicesSource.from_environment(environ, revision.key)
    except ValueError as exc:
        raise RevisionError(f"test services: {exc}") from exc
    return Agent(
        client,
        adapter,
        skills=skills,
        supervision=SupervisionSettings.from_environment(),
        workspaces=pool,
        services=services,
        runtime_dir=_runtime_dir_from_env(pool),
        poll_interval=float(os.environ.get("CONTROL_PLANE_AGENT_POLL", "5")),
        revision=revision,
        publish_hook=hook,
        **settings_of(revision).agent_kwargs(),
    )


def _agent_from_environment(
    client: ControlPlaneClient, adapter: Adapter
) -> Agent:  # pragma: no cover - wiring
    """Env mode: a principal that is no agent, configured as it always was."""
    try:
        skills = executor_from_environment(client)
    except ValueError as exc:
        logger.error("skill executor misconfigured: %s", exc)
        raise SystemExit(EXIT_MISCONFIGURED) from exc
    try:
        services = RunServicesSource.from_environment(os.environ)
    except ValueError as exc:
        logger.error("test services misconfigured: %s", exc)
        raise SystemExit(EXIT_MISCONFIGURED) from exc
    pool = _workspace_pool_from_env()
    try:
        hook = hook_from_environment()
    except ValueError as exc:
        logger.error("publish hook misconfigured: %s", exc)
        raise SystemExit(EXIT_MISCONFIGURED) from exc
    drain = os.environ.get("CONTROL_PLANE_AGENT_DRAIN_SECONDS")
    return Agent(
        client,
        adapter,
        skills=skills,
        supervision=SupervisionSettings.from_environment(),
        workspaces=pool,
        services=services,
        runtime_dir=_runtime_dir_from_env(pool),
        workspace_id=os.environ.get("CONTROL_PLANE_AGENT_WORKSPACE") or None,
        project_id=os.environ.get("CONTROL_PLANE_AGENT_PROJECT") or None,
        include_subprojects=os.environ.get("CONTROL_PLANE_AGENT_SUBPROJECTS") == "1",
        only_assigned=os.environ.get("CONTROL_PLANE_AGENT_ONLY_ASSIGNED") == "1",
        poll_interval=float(os.environ.get("CONTROL_PLANE_AGENT_POLL", "5")),
        drain_seconds=float(drain) if drain else None,
        publish_hook=hook,
        checks=os.environ.get("CONTROL_PLANE_AGENT_CHECKS") == "1",
    )


class RetryWindowError(ValueError):
    """``CONTROL_PLANE_AGENT_RETRY_WINDOW`` is not a number of seconds."""


def retry_window_from_env(environ: Mapping[str, str]) -> float:
    """``CONTROL_PLANE_AGENT_RETRY_WINDOW``, else :data:`RETRY_WINDOW_SECONDS`.

    A finite number of seconds, zero or more (0 — no retries at all).
    """
    raw = environ.get("CONTROL_PLANE_AGENT_RETRY_WINDOW", "").strip()
    if not raw:
        return RETRY_WINDOW_SECONDS
    try:
        window = float(raw)
    except ValueError:
        window = math.nan
    if not math.isfinite(window) or window < 0:
        raise RetryWindowError(
            "CONTROL_PLANE_AGENT_RETRY_WINDOW must be a number of seconds, zero or more "
            f"(got {raw!r}); unset it for the default of {RETRY_WINDOW_SECONDS:g}"
        )
    return window


def _env_adapter() -> Adapter | int:  # pragma: no cover - wiring
    adapter_name = os.environ.get("CONTROL_PLANE_AGENT_ADAPTER", "echo")
    try:
        return build_adapter(adapter_name)
    except LookupError:
        available = sorted({*ADAPTERS, *EXTERNAL_ADAPTERS})
        print(f"unknown adapter '{adapter_name}' (available: {available})")
    except ImportError as exc:
        print(f"adapter '{adapter_name}' is not installed on this runner: {exc}")
    return EXIT_MISCONFIGURED


def main() -> int:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = os.environ.get("CONTROL_PLANE_SERVER", "").rstrip("/")
    if not server:
        print("control-plane-agent requires CONTROL_PLANE_SERVER")
        return EXIT_MISCONFIGURED
    # The same resolution the human harness uses: an IAM identity first, then a
    # legacy API key. Reading CONTROL_PLANE_API_KEY directly would have sent the
    # Platform Access Token itself as a Bearer — a PAT is exchanged for an
    # audience-bound token, never presented — so a runner on an IAM-only server
    # could not authenticate at all.
    credential = resolve_credential(server)
    if credential is None:
        print(
            f"control-plane-agent has no credentials for {server}: set "
            "CONTROL_PLANE_IAM_URL and CONTROL_PLANE_IAM_TENANT with a Platform "
            "Access Token in IAM_PLATFORM_ACCESS_TOKEN, or CONTROL_PLANE_API_KEY "
            "where legacy keys are still enabled"
        )
        return EXIT_MISCONFIGURED
    try:
        mode = config_mode()
    except RevisionError as exc:
        print(exc)
        return EXIT_MISCONFIGURED

    try:
        retry_window = retry_window_from_env(os.environ)
    except RetryWindowError as exc:
        print(exc)
        return EXIT_MISCONFIGURED

    async def _run() -> int:
        async with ControlPlaneClient(server, credential, retry_window=retry_window) as client:
            # A principal bound to an agent must name its revision on every
            # run (CP-ADR-0073 §7), so "auto" is not a convenience: the env
            # mode would fail every start-run of an agent.
            # Not knowing which one it is, the process must not guess: it
            # asks to be started again (75) rather than run in the env mode.
            try:
                revision = await my_agent(client) if mode != "env" else None
            except ControlPlaneError as exc:
                logger.error("could not read this principal's agent: %s", exc.code)
                return EXIT_REVISION_CHANGED
            if revision is None and mode == "revision":
                logger.error("%s=revision, but this principal is not an agent", ENV_CONFIG_MODE)
                return EXIT_MISCONFIGURED
            if revision is not None:
                if revision.retired or revision.stopped:
                    logger.info(
                        "%s is %s; nothing to run",
                        revision.label,
                        revision.status if revision.retired else "stopped",
                    )
                    return 0
                try:
                    agent = _agent_from_revision(client, revision)
                except RevisionError as exc:
                    logger.error("%s cannot be run here: %s", revision.label, exc)
                    return EXIT_MISCONFIGURED
                logger.info("agent mode: %s (%s)", revision.label, revision.spec_hash)
            else:
                adapter = _env_adapter()
                if isinstance(adapter, int):
                    return adapter
                agent = _agent_from_environment(client, adapter)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, agent.request_stop)
            await agent.run_forever()
            return agent.exit_code

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
