"""Supervising an executor while it works: cancellation and the no-progress watchdog.

The daemon hands the executor — the Claude Code, Codex or OpenCode adapter, or
any other — one task and waits. Two things can make that wait pointless, and
both are the daemon's business, not the executor's:

* **cancellation** — somebody (a person, a rule whose premise is gone,
  CP-ADR-0063 amendment A4) asked the run to stop: ``POST
  /runs/{id}:request-cancel`` sets ``cancelRequestedAt``. The supervisor reads
  the run every ``poll_seconds`` (at most 30 s) and stops the executor;
  :func:`settle_stopped` then acknowledges the pending control messages,
  cancels the run once and releases the claim;
* **no progress** — the run has recorded no action for ``stall_warn_seconds``:
  a ``stall`` checkpoint names the last action; after ``stall_stop_seconds``
  the executor is stopped and the run fails ``no_progress``, which returns the
  task to the queue. While the last action is still ``started`` (a long test
  suite, a build) it counts as alive: the ``stall`` checkpoint still comes at
  ``stall_warn_seconds`` and says the action is running, but the stop waits
  for ``action_max_seconds`` instead. Finishing that action is progress;
* **drain** — the daemon itself is stopping (``SIGTERM`` from whoever placed
  it, a new revision of its agent) and the run in flight has had the drain
  time of the agent's placement (``placement.drainSeconds``, CP-ADR-0073) to
  finish. It is stopped like a cancelled one and the run is cancelled
  ``drained``, so the task goes back to the queue for the next instance
  rather than waiting for the lease to expire.

Stopping is one mechanism for every adapter: the executor runs as an asyncio
task and is cancelled. An adapter that drives a process must end it when it
is cancelled (the Claude Code and Codex CLIs kill theirs; OpenCode is asked to
abort its session). The watchdog looks at the run's journal of actions only —
never at what the task is about.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from control_plane_client import ControlPlaneClient, ControlPlaneError

logger = logging.getLogger("control_plane_agent.supervision")

#: Why the executor was stopped.
CANCEL_REQUESTED = "cancel_requested"
NO_PROGRESS = "no_progress"
RUN_ENDED = "run_ended"
DRAINED = "drained"
#: The checkpoint the watchdog leaves when a run goes quiet.
STALL_CHECKPOINT = "stall"
#: A cancellation must be noticed within this, whatever is configured.
MAX_POLL_SECONDS = 30.0
ACTIONS_PAGE = 100


@dataclass(frozen=True)
class SupervisionSettings:
    poll_seconds: float = 15.0
    # 0 disables the step.
    stall_warn_seconds: float = 600.0
    stall_stop_seconds: float = 1800.0
    # How long an unfinished last action keeps the run alive; never less than
    # stall_stop_seconds.
    action_max_seconds: float = 3600.0

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "SupervisionSettings":
        env = os.environ if env is None else env
        return cls(
            poll_seconds=float(env.get("CONTROL_PLANE_AGENT_CONTROL_POLL_SECONDS", "15")),
            stall_warn_seconds=float(env.get("CONTROL_PLANE_AGENT_STALL_WARN_SECONDS", "600")),
            stall_stop_seconds=float(env.get("CONTROL_PLANE_AGENT_STALL_STOP_SECONDS", "1800")),
            action_max_seconds=float(env.get("CONTROL_PLANE_AGENT_ACTION_MAX_SECONDS", "3600")),
        )

    @property
    def interval(self) -> float:
        return max(0.01, min(self.poll_seconds, MAX_POLL_SECONDS))


class ExecutionStopped(Exception):
    """The executor was stopped by the supervisor; ``reason`` says why."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class RunSupervisor:
    """Runs one executor coroutine and stops it when the run says so."""

    client: ControlPlaneClient
    run_id: str
    settings: SupervisionSettings = field(default_factory=SupervisionSettings)
    clock: Callable[[], float] = time.monotonic
    # When the daemon is draining: the ``clock`` value after which the executor
    # is stopped. Read on every look, since a drain starts while the run goes.
    drain_deadline: Callable[[], float | None] = lambda: None
    _cursor: str | None = field(default=None, init=False)
    _last_seq: int = field(default=0, init=False)
    _last_action: dict[str, Any] | None = field(default=None, init=False)
    _progress_at: float = field(default=0.0, init=False)
    _warned: bool = field(default=False, init=False)

    async def run[T](self, work: Coroutine[Any, Any, T]) -> T:
        """Await ``work``; raise :class:`ExecutionStopped` if it had to be stopped."""
        task: asyncio.Task[T] = asyncio.ensure_future(work)
        self._progress_at = self.clock()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.settings.interval)
                if done:
                    return task.result()
                stop = await self._look()
                if stop is not None:
                    await _cancel(task)
                    raise stop
        except BaseException:
            # The daemon itself is going down: take the executor with it.
            if not task.done():
                await _cancel(task)
            raise

    async def _look(self) -> ExecutionStopped | None:
        deadline = self.drain_deadline()
        if deadline is not None and self.clock() >= deadline:
            return ExecutionStopped(DRAINED)
        try:
            run = await self.client.get_run(self.run_id)
        except ControlPlaneError as exc:
            # Not knowing is not a reason to stop work; ask again next time.
            logger.info("run %s not readable: %s", self.run_id, exc)
            return None
        if run.get("cancelRequestedAt"):
            return ExecutionStopped(
                CANCEL_REQUESTED, {"cancelRequestedAt": run["cancelRequestedAt"]}
            )
        if run.get("status") != "running":
            return ExecutionStopped(RUN_ENDED, {"status": run.get("status")})
        return await self._watch()

    async def _watch(self) -> ExecutionStopped | None:
        warn, stop = self.settings.stall_warn_seconds, self.settings.stall_stop_seconds
        if warn <= 0 and stop <= 0:
            return None
        try:
            progressed = await self._new_actions()
        except ControlPlaneError as exc:
            logger.info("actions of run %s not readable: %s", self.run_id, exc)
            return None
        now = self.clock()
        if progressed:
            self._progress_at = now
            self._warned = False
            return None
        idle = now - self._progress_at
        last = self._last_action
        detail: dict[str, Any] = {"idleSeconds": int(idle), "lastAction": last}
        running = last is not None and last["status"] == "started"
        if last is not None and running:
            # Idle is counted from when the action was seen, not from its
            # startedAt: the server's clock is not ours.
            detail["actionRunning"] = True
            detail["note"] = f"action still running: {last['action']} since {last['startedAt']}"
            if stop > 0:
                # Never sooner than a finished action would be stopped.
                stop = max(stop, self.settings.action_max_seconds)
        if stop > 0 and idle >= stop:
            if warn > 0 and not self._warned:
                # One late look (a busy host, a machine that slept) can cross
                # both thresholds at once: the stop still leaves the ``stall``
                # checkpoint that names the last action, as it would have
                # had the looks come on time.
                await self._warn(idle, running, detail)
            return ExecutionStopped(NO_PROGRESS, detail)
        if warn > 0 and idle >= warn and not self._warned:
            await self._warn(idle, running, detail)
        return None

    async def _warn(self, idle: float, running: bool, detail: dict[str, Any]) -> None:
        """Leave the one ``stall`` checkpoint of this quiet spell."""
        self._warned = True
        logger.warning(
            "run %s recorded no action for %ds%s",
            self.run_id,
            int(idle),
            " (the last one is still running)" if running else "",
        )
        with contextlib.suppress(ControlPlaneError):
            await self.client.create_checkpoint(self.run_id, kind=STALL_CHECKPOINT, data=detail)

    async def _new_actions(self) -> bool:
        """Has the run recorded an action, or finished its last one, since the last look?

        Follows the ``seq`` cursor: each look re-reads at most the last page,
        which always holds the last action, so its finish is seen too.
        """
        before = self._last_seq
        before_status = self._last_action["status"] if self._last_action else None
        while True:
            page = await self.client.list_run_actions(
                self.run_id, limit=ACTIONS_PAGE, cursor=self._cursor
            )
            for item in page.get("items", []):
                if int(item["seq"]) >= self._last_seq:
                    self._last_seq = int(item["seq"])
                    self._last_action = {
                        "seq": item["seq"],
                        "action": item.get("action"),
                        "status": item.get("status"),
                        "startedAt": item.get("startedAt"),
                    }
            following = page.get("nextCursor")
            if not following:
                if self._last_seq > before:
                    return True
                return self._last_action is not None and (
                    self._last_action["status"] != before_status
                )
            self._cursor = following


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The executor broke while being stopped; it is stopped either way.
        logger.exception("executor failed while being stopped")


async def settle_stopped(
    client: ControlPlaneClient,
    *,
    run_id: str,
    claim_id: str,
    fencing_token: int,
    stop: ExecutionStopped,
) -> None:
    """Close a run whose executor was stopped, once, and let the task go.

    Cancellation: the accepted control messages are acknowledged at the
    executor's stop (the cancel request ``applied``, anything queued behind
    it ``superseded``), then the run is cancelled. No progress: the run fails
    ``no_progress``. Drain: the run is cancelled ``drained``. Either way the
    claim is released, so the task is free for whatever comes next — the
    rule's decision or another executor.
    """
    if stop.reason == CANCEL_REQUESTED:
        await _acknowledge_controls(client, run_id, claim_id, fencing_token)
        with contextlib.suppress(ControlPlaneError):
            await client.cancel_run(run_id, reason=CANCEL_REQUESTED)
    elif stop.reason == NO_PROGRESS:
        with contextlib.suppress(ControlPlaneError):
            await client.fail_run(run_id, failure_reason=NO_PROGRESS)
    elif stop.reason == DRAINED:
        with contextlib.suppress(ControlPlaneError):
            await client.cancel_run(run_id, reason=DRAINED)
    with contextlib.suppress(ControlPlaneError):
        await client.release_claim(claim_id, reason=stop.reason)


async def _acknowledge_controls(
    client: ControlPlaneClient, run_id: str, claim_id: str, fencing_token: int
) -> None:
    try:
        messages = (await client.list_run_control_messages(run_id)).get("items", [])
    except ControlPlaneError as exc:
        logger.info("control messages of run %s not readable: %s", run_id, exc)
        return
    pending = sorted(
        (m for m in messages if m.get("status") == "accepted"), key=lambda m: int(m["seq"])
    )
    for message in pending:
        applied = message.get("operation") in ("request_cancel", "force_cancel")
        try:
            run = await client.get_run(run_id)
            await client.acknowledge_run_control_message(
                run_id,
                str(message["id"]),
                status="applied" if applied else "superseded",
                claim_id=claim_id,
                fencing_token=fencing_token,
                expected_run_version=int(run["version"]),
                expected_message_version=int(message["version"]),
                safe_boundary="executor stopped" if applied else None,
                reason="executor stopped on cancel request",
            )
        except ControlPlaneError as exc:
            # The cancellation itself still goes through; the message stays
            # visible as accepted.
            logger.info("control message %s not acknowledged: %s", message.get("id"), exc)
            return
