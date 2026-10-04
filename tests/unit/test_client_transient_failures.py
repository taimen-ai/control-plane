"""A restarting core is not a verdict (TASK-001138).

Behind the proxy a restart of the core answers 502/503/504 for a few seconds.
Such an answer says nothing about a lease or a command: the SDK repeats what
a repeat cannot turn into a second command (a GET, a request with an
Idempotency-Key) within its ``retry_window``, and ``HeartbeatRunner`` treats it
as a transport failure — retried after a short pause, terminal only once the
core has been unreachable for its outage budget since the last good beat. An
answer of the core itself (409, 404, 403) stays terminal at once.
"""

import asyncio
import time
from collections.abc import Callable

import httpx
import pytest

import control_plane_client.client as client_module
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    HeartbeatRunner,
    IamCredential,
    IamCredentialError,
    IdempotencyConflictError,
    NotFoundError,
    PermissionDeniedError,
    StaleClaimError,
    TransportError,
    is_transient,
)

pytestmark = pytest.mark.asyncio

Handler = Callable[[httpx.Request], httpx.Response]

SESSION_BEAT = "/api/v1/sessions/s-1:heartbeat"
CLAIM_BEAT = "/api/v1/claims/c-1:heartbeat"


def bad_gateway(status: int = 502) -> httpx.Response:
    # What nginx says while its upstream is down: HTML, no error envelope.
    return httpx.Response(status, text="<html><h1>502 Bad Gateway</h1></html>")


def envelope(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}})


class Script:
    """Answers each path from its own queue; an empty queue answers 200."""

    def __init__(self, answers: dict[str, list[httpx.Response | Exception]]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.answers.get(request.url.path) or []
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    def seen(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def client_for(script: Script, *, retry_window: float = 1.0) -> ControlPlaneClient:
    return ControlPlaneClient(
        "http://cp.test",
        "cp_key",
        transport=httpx.MockTransport(script),
        retry_window=retry_window,
    )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pauses of 10, 20, 40, 40... ms: the window arithmetic stays the same.
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF", 0.01)
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF_MAX", 0.04)


# -- is_transient ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransportError("ConnectError: refused"), True),
        (ControlPlaneError("http_error", "Unexpected server error", status=502), True),
        (ControlPlaneError("http_error", "Unexpected server error", status=503), True),
        (ControlPlaneError("http_error", "Unexpected server error", status=504), True),
        # A proxy that forwards an envelope-less 500 of a dying worker.
        (ControlPlaneError("http_error", "Unexpected server error", status=500), True),
        # A gateway that wraps its 503 in an envelope of its own.
        (ControlPlaneError("upstream_unavailable", "down", status=503), True),
        # The core itself answered: a verdict, not a blip.
        (ControlPlaneError("internal_error", "boom", status=500), False),
        (StaleClaimError("stale_claim", "fenced", status=409), False),
        (ControlPlaneError("lease_expired", "gone", status=409), False),
        (NotFoundError("not_found", "no claim", status=404), False),
        (PermissionDeniedError("permission_denied", "no", status=403), False),
        (ControlPlaneError("http_error", "Unexpected server error", status=400), False),
        (ControlPlaneError("http_error", "no status", status=0), False),
        (ValueError("not ours"), False),
        (TimeoutError(), False),
        # The IAM exchange before a command: a restarting IAM is a blip...
        (IamCredentialError("iam_exchange_failed", "IAM answered 502", status=502), True),
        (IamCredentialError("iam_exchange_failed", "IAM answered 503", status=503), True),
        (IamCredentialError("iam_exchange_failed", "IAM answered 504", status=504), True),
        (IamCredentialError("iam_unreachable", "IAM is unreachable: ConnectError"), True),
        # ...and its answer about the token is a verdict.
        (IamCredentialError("iam_invalid_token", "rejected", status=401), False),
        (IamCredentialError("iam_audience_not_allowed", "not allowed", status=403), False),
        (IamCredentialError("iam_exchange_failed", "IAM answered 400", status=400), False),
        (IamCredentialError("iam_exchange_failed", "IAM answered 500", status=500), False),
        (IamCredentialError("iam_exchange_malformed", "malformed"), False),
        (IamCredentialError("iam_not_authenticated", "no token"), False),
    ],
)
async def test_is_transient(exc: BaseException, expected: bool) -> None:
    assert is_transient(exc) is expected


# -- _request retries ------------------------------------------------------------


async def test_a_read_outlives_a_restart_of_the_core() -> None:
    script = Script({"/api/v1/runs/r-1": [bad_gateway(502), bad_gateway(503), bad_gateway(504)]})
    async with client_for(script) as client:
        run = await client.get_run("r-1")
    assert run["ok"] is True
    assert len(script.seen("/api/v1/runs/r-1")) == 4


async def test_an_idempotent_command_is_repeated_with_the_same_key() -> None:
    path = "/api/v1/runs/r-1/actions/a-1:finish"
    script = Script({path: [bad_gateway(), bad_gateway()]})
    async with client_for(script) as client:
        await client.finish_action("r-1", "a-1", status="completed")
    sent = script.seen(path)
    assert len(sent) == 3
    keys = {r.headers["Idempotency-Key"] for r in sent}
    assert len(keys) == 1 and keys != {""}


async def test_fail_run_is_repeated_after_a_bad_gateway() -> None:
    """Otherwise the run hangs ``running`` until its claim expires."""
    path = "/api/v1/runs/r-1:fail"
    script = Script({path: [bad_gateway()]})
    async with client_for(script) as client:
        await client.fail_run("r-1", failure_reason="boom")
    assert len(script.seen(path)) == 2


async def test_a_transport_failure_of_a_read_is_repeated() -> None:
    path = "/api/v1/runs/r-1"
    script = Script({path: [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]})
    async with client_for(script) as client:
        await client.get_run("r-1")
    assert len(script.seen(path)) == 3


async def test_a_command_without_a_key_is_not_repeated() -> None:
    """A heartbeat carries no Idempotency-Key: its caller decides."""
    script = Script({SESSION_BEAT: [bad_gateway()]})
    async with client_for(script) as client:
        with pytest.raises(ControlPlaneError) as caught:
            await client.heartbeat_session("s-1")
    assert caught.value.status == 502
    assert caught.value.code == "http_error"
    assert len(script.seen(SESSION_BEAT)) == 1


async def test_a_command_without_a_key_is_not_repeated_after_a_transport_failure() -> None:
    script = Script({SESSION_BEAT: [httpx.ConnectError("refused")]})
    async with client_for(script) as client:
        with pytest.raises(TransportError) as caught:
            await client.heartbeat_session("s-1")
    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert len(script.seen(SESSION_BEAT)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        envelope(409, "stale_claim"),
        envelope(404, "not_found"),
        envelope(403, "permission_denied"),
        envelope(500, "internal_error"),
        envelope(422, "validation_error"),
    ],
)
async def test_an_answer_of_the_core_is_not_repeated(answer: httpx.Response) -> None:
    path = "/api/v1/runs/r-1:fail"
    script = Script({path: [answer]})
    async with client_for(script) as client:
        with pytest.raises(ControlPlaneError):
            await client.fail_run("r-1", failure_reason="boom")
    assert len(script.seen(path)) == 1


async def test_the_window_bounds_the_retries() -> None:
    # 10 + 20 + 40 ms fit into 75 ms, the next 40 ms do not: four attempts.
    path = "/api/v1/runs/r-1"
    script = Script({path: [bad_gateway() for _ in range(10)]})
    async with client_for(script, retry_window=0.075) as client:
        with pytest.raises(ControlPlaneError) as caught:
            await client.get_run("r-1")
    assert caught.value.status == 502
    assert len(script.seen(path)) == 4


async def test_a_zero_window_is_one_attempt() -> None:
    path = "/api/v1/runs/r-1"
    script = Script({path: [bad_gateway()]})
    async with client_for(script, retry_window=0) as client:
        with pytest.raises(ControlPlaneError):
            await client.get_run("r-1")
    assert len(script.seen(path)) == 1


async def test_the_default_window_keeps_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that ask for nothing keep what they had: 0.5 + 1.0 s, three attempts."""
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF", 0.5)
    monkeypatch.setattr(client_module, "_RETRY_BACKOFF_MAX", 5.0)
    slept: list[float] = []

    async def no_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    path = "/api/v1/runs/r-1"
    script = Script({path: [bad_gateway() for _ in range(5)]})
    client = ControlPlaneClient("http://cp.test", "cp_key", transport=httpx.MockTransport(script))
    with pytest.raises(ControlPlaneError):
        await client.get_run("r-1")
    await client.aclose()
    assert len(script.seen(path)) == 3
    assert slept == [0.5, 1.0]


async def test_in_flight_after_a_gateway_timeout_is_waited_out() -> None:
    """The first attempt reached the core and is still running there."""
    path = "/api/v1/runs/r-1:succeed"
    script = Script({path: [bad_gateway(504), envelope(409, "idempotency_in_flight")]})
    async with client_for(script) as client:
        await client.succeed_run("r-1")
    assert len(script.seen(path)) == 3


async def test_in_flight_on_the_first_attempt_is_a_conflict() -> None:
    """Without an earlier failure, another caller holds the key: not ours to wait for."""
    path = "/api/v1/runs/r-1:succeed"
    script = Script({path: [envelope(409, "idempotency_in_flight")]})
    async with client_for(script) as client:
        with pytest.raises(IdempotencyConflictError):
            await client.succeed_run("r-1")
    assert len(script.seen(path)) == 1


async def test_a_download_is_read_again_after_a_bad_gateway() -> None:
    path = "/api/v1/artifacts/a-1/content"
    script = Script({path: [bad_gateway(), httpx.ConnectError("refused")]})
    async with client_for(script) as client:
        response = await client._send("GET", "/artifacts/a-1/content")
    assert response.status_code == 200
    assert len(script.seen(path)) == 3


async def test_an_upload_is_not_repeated() -> None:
    path = "/api/v1/artifacts/content"
    script = Script({path: [bad_gateway()]})
    async with client_for(script) as client:
        with pytest.raises(ControlPlaneError):
            await client._send("POST", "/artifacts/content", content=lambda: b"x")
    assert len(script.seen(path)) == 1


# -- HeartbeatRunner -------------------------------------------------------------


async def wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in 2 s")


def runner_for(script: Script, **kwargs: float) -> tuple[ControlPlaneClient, HeartbeatRunner]:
    client = client_for(script)
    params: dict[str, float] = {
        "interval_seconds": 0.01,
        "retry_seconds": 0.01,
        "outage_budget_seconds": 1.0,
        **kwargs,
    }
    runner = HeartbeatRunner(client, session_id="s-1", claim_id="c-1", **params)  # type: ignore[arg-type]
    return client, runner


@pytest.mark.parametrize("path", [SESSION_BEAT, CLAIM_BEAT])
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_bad_gateway_on_a_heartbeat_does_not_end_the_lease(path: str, status: int) -> None:
    script = Script({path: [bad_gateway(status)]})
    client, runner = runner_for(script)
    runner.start()
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 3)
    assert runner.error is None
    assert runner.alive
    assert runner.transport_failures == 0
    await runner.stop()
    await client.aclose()


async def test_failures_that_are_not_consecutive_never_add_up() -> None:
    # Ticks: session 502, session 502, ok, claim 502, claim 502, ok...
    script = Script(
        {
            SESSION_BEAT: [bad_gateway(), bad_gateway()],
            CLAIM_BEAT: [httpx.Response(200, json={}), bad_gateway(), bad_gateway()],
        }
    )
    client, runner = runner_for(script)
    runner.start()
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 5)
    assert runner.error is None
    await runner.stop()
    await client.aclose()


async def test_three_failures_at_the_default_pace_do_not_end_the_lease() -> None:
    """The defaults scaled down (60 s interval, 10 s retry, three beats' patience).

    Three failures in a row are 20 s of an unreachable core — a wifi drop, a
    recreated proxy, a long rollout — well inside the 180 s the lease used to
    tolerate; counting them used to end the lease right there.
    """
    failures: list[httpx.Response | Exception] = [
        bad_gateway(),
        httpx.ConnectError("refused"),
        bad_gateway(503),
    ]
    script = Script({SESSION_BEAT: failures})
    client = client_for(script)
    runner = HeartbeatRunner(
        client,
        session_id="s-1",
        claim_id="c-1",
        interval_seconds=0.12,
        retry_seconds=0.02,
    )
    assert runner.outage_budget == pytest.approx(0.36)
    runner.start()
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 1)
    assert runner.error is None
    assert runner.alive
    assert runner.transport_failures == 0
    assert len(script.seen(SESSION_BEAT)) == 4
    await runner.stop()
    await client.aclose()


async def test_the_default_budget_is_three_intervals() -> None:
    client = client_for(Script({}))
    runner = HeartbeatRunner(client, session_id="s-1")
    assert runner.outage_budget == 180.0
    assert runner.retry_seconds < runner.outage_budget
    explicit = HeartbeatRunner(client, session_id="s-1", outage_budget_seconds=240.0)
    assert explicit.outage_budget == 240.0
    await client.aclose()


async def test_failures_within_the_budget_do_not_end_the_lease() -> None:
    failures: list[httpx.Response | Exception] = []
    for _ in range(5):
        failures += [bad_gateway(), httpx.ConnectError("refused"), bad_gateway(504)]
    script = Script({CLAIM_BEAT: failures})
    client, runner = runner_for(script, outage_budget_seconds=5.0)
    runner.start()
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 17)
    assert runner.error is None
    assert runner.alive
    assert runner.transport_failures == 0
    await runner.stop()
    await client.aclose()


@pytest.mark.parametrize("path", [SESSION_BEAT, CLAIM_BEAT])
@pytest.mark.parametrize("failure", ["bad_gateway", "transport"])
async def test_failures_past_the_budget_end_the_lease(path: str, failure: str) -> None:
    def answer() -> httpx.Response | Exception:
        return bad_gateway() if failure == "bad_gateway" else httpx.ConnectError("refused")

    script = Script({path: [answer() for _ in range(200)]})
    client, runner = runner_for(script, outage_budget_seconds=0.08)
    started = time.monotonic()
    runner.start()
    await wait_for(lambda: runner.error is not None)
    assert time.monotonic() - started >= 0.08
    assert runner.error is not None
    if failure == "bad_gateway":
        assert runner.error.status == 502
    else:
        assert isinstance(runner.error, TransportError)
    assert runner.transport_failures >= 2
    assert not runner.alive
    await runner.stop()
    await client.aclose()


class ClockedScript(Script):
    """Every request moves a fake clock ten seconds forward."""

    now = 0.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.now += 10.0
        return super().__call__(request)


async def test_the_budget_runs_from_the_last_good_beat_not_the_count() -> None:
    # t=10 session ok, t=20 claim ok: the last good beat. Then session 502s
    # at t=30, 40, ... — 100 s later, at t=120, the tenth one ends the lease.
    script = ClockedScript({SESSION_BEAT: [httpx.Response(200, json={})] + [bad_gateway()] * 50})
    client, runner = runner_for(script, outage_budget_seconds=100.0)
    runner._clock = lambda: script.now
    runner.start()
    await wait_for(lambda: runner.error is not None)
    assert runner.transport_failures == 10
    assert len(script.seen(SESSION_BEAT)) == 11
    assert len(script.seen(CLAIM_BEAT)) == 1
    await runner.stop()
    await client.aclose()


async def test_a_retry_waits_a_short_pause_not_a_whole_interval() -> None:
    """Retries a minute apart would spend the lease before the core is back."""
    script = Script({SESSION_BEAT: [bad_gateway() for _ in range(50)]})
    client, runner = runner_for(
        script, interval_seconds=0.3, retry_seconds=0.01, outage_budget_seconds=0.33
    )
    started = time.monotonic()
    runner.start()
    await wait_for(lambda: runner.error is not None)
    # One interval before the first beat, then short pauses — not intervals.
    assert time.monotonic() - started < 0.6
    assert runner.transport_failures >= 2
    await runner.stop()
    await client.aclose()


@pytest.mark.parametrize(
    "answer",
    [
        envelope(409, "stale_claim"),
        envelope(409, "lease_expired"),
        envelope(409, "session_expired"),
        envelope(404, "not_found"),
        envelope(403, "permission_denied"),
    ],
)
async def test_an_answer_of_the_core_ends_the_lease_at_once(answer: httpx.Response) -> None:
    script = Script({CLAIM_BEAT: [answer]})
    client, runner = runner_for(script)
    runner.start()
    await wait_for(lambda: runner.error is not None)
    assert runner.error is not None
    assert runner.error.status == answer.status_code
    assert runner.transport_failures == 0
    assert len(script.seen(CLAIM_BEAT)) == 1
    await runner.stop()
    await client.aclose()


async def test_a_domain_answer_after_a_bad_gateway_is_still_terminal() -> None:
    script = Script({CLAIM_BEAT: [bad_gateway(), envelope(409, "stale_claim")]})
    client, runner = runner_for(script)
    runner.start()
    await wait_for(lambda: runner.error is not None)
    assert isinstance(runner.error, StaleClaimError)
    assert len(script.seen(CLAIM_BEAT)) == 2
    await runner.stop()
    await client.aclose()


async def test_restart_clears_the_failures() -> None:
    script = Script({SESSION_BEAT: [bad_gateway() for _ in range(3)]})
    client, runner = runner_for(script, outage_budget_seconds=0.02)
    runner.start()
    await wait_for(lambda: runner.error is not None)
    await runner.stop()
    runner.start()
    assert runner.error is None
    assert runner.transport_failures == 0
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 2)
    assert runner.alive
    await runner.stop()
    await client.aclose()


# -- HeartbeatRunner behind an IAM credential ------------------------------------


def iam_answers(answers: list[httpx.Response | Exception]) -> IamCredential:
    """An IAM that answers the exchanges from ``answers``, then a token.

    The token lives one second against a 30 s refresh margin: every beat
    exchanges anew, as a beat after a long run does.
    """
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Response(200, json={"accessToken": "at", "expiresIn": 1})

    return IamCredential(
        "http://iam.test",
        "tenant",
        platform_access_token="iam_pat_x_y",
        transport=httpx.MockTransport(handler),
    )


def iam_runner_for(
    credential: IamCredential, script: Script, *, outage_budget_seconds: float = 1.0
) -> tuple[ControlPlaneClient, HeartbeatRunner]:
    client = ControlPlaneClient(
        "http://cp.test", credential, transport=httpx.MockTransport(script), retry_window=1.0
    )
    runner = HeartbeatRunner(
        client,
        session_id="s-1",
        claim_id="c-1",
        interval_seconds=0.01,
        retry_seconds=0.01,
        outage_budget_seconds=outage_budget_seconds,
    )
    return client, runner


async def test_a_restart_of_iam_does_not_end_the_lease() -> None:
    """Refused connections and a 503 of the same restart: both are a blip."""
    credential = iam_answers(
        [
            httpx.ConnectError("refused"),
            httpx.Response(503, text="unavailable"),
            httpx.ConnectError("refused"),
            httpx.Response(502, text="bad gateway"),
        ]
    )
    script = Script({})
    client, runner = iam_runner_for(credential, script)
    runner.start()
    await wait_for(lambda: len(script.seen(CLAIM_BEAT)) >= 2)
    assert runner.error is None
    assert runner.alive
    await runner.stop()
    await client.aclose()


async def test_an_iam_unreachable_past_the_budget_ends_the_lease() -> None:
    credential = iam_answers([httpx.ConnectError("refused")] * 1000)
    script = Script({})
    client, runner = iam_runner_for(credential, script, outage_budget_seconds=0.1)
    runner.start()
    await wait_for(lambda: not runner.alive)
    assert isinstance(runner.error, IamCredentialError)
    assert runner.error.code == "iam_unreachable"
    assert runner.transport_failures >= 2
    assert script.seen(SESSION_BEAT) == []
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "iam_invalid_token"), (403, "iam_audience_not_allowed"), (400, "iam_exchange_failed")],
)
async def test_a_verdict_of_iam_ends_the_lease_at_once(status: int, code: str) -> None:
    credential = iam_answers([httpx.Response(status, json={})])
    script = Script({})
    client, runner = iam_runner_for(credential, script, outage_budget_seconds=60.0)
    runner.start()
    await wait_for(lambda: not runner.alive)
    assert isinstance(runner.error, IamCredentialError)
    assert runner.error.code == code
    assert runner.transport_failures == 0
    await client.aclose()
