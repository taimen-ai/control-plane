"""The no-progress watchdog whatever the pace of its looks (supervision.py).

The looks come every ``poll_seconds`` only on an idle host. On a busy one (a
CI runner, a laptop that slept) one late look can find the run quiet past
both thresholds at once; the stop must still leave the ``stall`` checkpoint
that names the last action. Time here is a scripted clock, not the wall.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from control_plane_agent.supervision import (
    NO_PROGRESS,
    STALL_CHECKPOINT,
    ExecutionStopped,
    RunSupervisor,
    SupervisionSettings,
)


class QuietRun:
    """A running run that records no action; keeps the checkpoints written to it."""

    def __init__(self) -> None:
        self.checkpoints: list[tuple[str, dict[str, Any]]] = []

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "status": "running", "cancelRequestedAt": None}

    async def list_run_actions(
        self, run_id: str, *, limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        return {"items": [], "nextCursor": None}

    async def create_checkpoint(self, run_id: str, *, kind: str, data: dict[str, Any]) -> None:
        self.checkpoints.append((kind, data))


def _clock(*readings: float) -> Iterator[float]:
    """The supervisor's start, then one reading per look; the last one repeats."""
    yield from readings
    while True:
        yield readings[-1]


async def _work_forever() -> None:
    await asyncio.sleep(3600)


async def _supervise(settings: SupervisionSettings, *readings: float) -> QuietRun:
    run = QuietRun()
    clock = _clock(*readings)
    supervisor = RunSupervisor(
        run,  # type: ignore[arg-type]
        "run-1",
        settings,
        clock=lambda: next(clock),
    )
    with pytest.raises(ExecutionStopped) as stopped:
        await asyncio.wait_for(supervisor.run(_work_forever()), 5)
    assert stopped.value.reason == NO_PROGRESS
    return run


async def test_a_late_look_past_both_thresholds_still_leaves_the_stall_checkpoint() -> None:
    settings = SupervisionSettings(
        poll_seconds=0.01, stall_warn_seconds=0.2, stall_stop_seconds=0.6
    )
    # Started at 0; looked at 0.1 (quiet, under the warning); the next look
    # came at 0.7 — past the warning and the stop alike.
    run = await _supervise(settings, 0.0, 0.1, 0.7)

    assert [kind for kind, _ in run.checkpoints] == [STALL_CHECKPOINT]
    [(_, data)] = run.checkpoints
    assert data["lastAction"] is None
    assert data["idleSeconds"] == 0


async def test_looks_on_time_leave_one_stall_checkpoint_before_the_stop() -> None:
    settings = SupervisionSettings(
        poll_seconds=0.01, stall_warn_seconds=0.2, stall_stop_seconds=0.6
    )
    run = await _supervise(settings, 0.0, 0.1, 0.3, 0.5, 0.7)

    assert [kind for kind, _ in run.checkpoints] == [STALL_CHECKPOINT]


async def test_a_disabled_warning_leaves_no_checkpoint_at_the_stop() -> None:
    settings = SupervisionSettings(poll_seconds=0.01, stall_warn_seconds=0, stall_stop_seconds=0.6)
    run = await _supervise(settings, 0.0, 0.1, 0.7)

    assert run.checkpoints == []
