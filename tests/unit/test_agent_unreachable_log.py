"""The daemon names which service it cannot reach (TASK-001202).

The IAM exchange precedes every command of an IAM identity, and its outage
is transient (``is_transient``) just as a restart of the core is. The cycle
backs off on both, but the log line tells the operator which one to look at.
"""

import logging
from typing import cast

import pytest

from control_plane_agent.main import Agent
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    IamCredentialError,
    TransportError,
)

pytestmark = pytest.mark.asyncio


async def _one_failed_cycle(
    exc: ControlPlaneError, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> list[str]:
    agent = Agent(cast(ControlPlaneClient, object()), None, poll_interval=0.01, max_cycles=1)

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def current(*, after_work: bool) -> bool:
        return True

    async def failing() -> bool:
        raise exc

    monkeypatch.setattr(agent, "recover", nothing)
    monkeypatch.setattr(agent, "_open_session", nothing)
    monkeypatch.setattr(agent, "_revision_is_current", current)
    monkeypatch.setattr(agent, "run_once", failing)
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_agent"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="control_plane_agent"):
        await agent.run_forever()
    return [r.getMessage() for r in caplog.records if r.name == "control_plane_agent"]


@pytest.mark.parametrize(
    "exc",
    [
        IamCredentialError("iam_unreachable", "IAM is unreachable: ConnectError"),
        IamCredentialError("iam_exchange_failed", "IAM answered 503 to the exchange", status=503),
    ],
)
async def test_an_unreachable_iam_is_not_logged_as_the_core(
    exc: IamCredentialError, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    messages = await _one_failed_cycle(exc, monkeypatch, caplog)
    assert messages == [f"IAM unreachable, backing off: {exc}"]


@pytest.mark.parametrize(
    "exc",
    [
        TransportError("ConnectError: refused"),
        ControlPlaneError("http_error", "Unexpected server error", status=502),
    ],
)
async def test_an_unreachable_core_is_logged_as_the_core(
    exc: ControlPlaneError, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    messages = await _one_failed_cycle(exc, monkeypatch, caplog)
    assert messages == [f"core unreachable, backing off: {exc}"]


async def test_a_verdict_of_iam_is_a_cycle_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exc = IamCredentialError("iam_invalid_token", "IAM rejected the token", status=401)
    messages = await _one_failed_cycle(exc, monkeypatch, caplog)
    assert messages == [f"cycle error: {exc}"]
