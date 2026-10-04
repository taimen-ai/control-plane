"""The skill executor of the agent daemon (ADR-0056 §5) against fakes.

``local`` runs real stub modules (``tests.skill_stubs``), ``http`` a mock
transport, ``mcp`` a real in-process MCP server. The Control Plane is a fake
that records what the executor reports.
"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from control_plane_agent.skills import (
    ENV_LOCAL_ENV,
    ENV_LOCAL_PACKAGES,
    EndpointPolicy,
    HttpProtocol,
    LocalProtocol,
    McpProtocol,
    SkillCall,
    SkillExecutor,
    SkillFailure,
    _guarded_mcp_client,
    _skill_env_reserved,
    discover_local_entrypoints,
    executor_from_environment,
    iam_token_source,
    is_public_address,
    map_inputs,
    origin_of,
    parse_skill_env,
    resolve_path,
)
from control_plane_client import ConflictError, ControlPlaneError

OUTPUTS = {
    "type": "object",
    "properties": {"double": {"type": "integer"}},
    "required": ["double"],
}


class FakeControlPlane:
    """Records reports; heartbeats can be made to say the lease is gone."""

    def __init__(self, *, lease_lost_after: int | None = None) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.lease_lost_after = lease_lost_after

    async def heartbeat_skill_invocation(self, invocation_id: str, **kwargs: Any) -> dict:
        self.heartbeats += 1
        if self.lease_lost_after is not None and self.heartbeats > self.lease_lost_after:
            raise ConflictError("stale_invocation_lease", "lease lost", status=409)
        expires = datetime.now(UTC) + timedelta(seconds=30)
        return {"id": invocation_id, "leaseExpiresAt": expires.isoformat()}

    async def complete_skill_invocation(self, invocation_id: str, **kwargs: Any) -> dict:
        self.completed.append({"id": invocation_id, **kwargs})
        return {"id": invocation_id, "status": "succeeded"}

    async def fail_skill_invocation(self, invocation_id: str, **kwargs: Any) -> dict:
        self.failed.append({"id": invocation_id, **kwargs})
        return {"id": invocation_id, "status": "failed"}


def claimed(
    implementation: dict[str, Any],
    inputs: dict[str, Any] | None = None,
    *,
    timeout: int = 5,
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expires = datetime.now(UTC) + timedelta(seconds=timeout + 30)
    return {
        "invocation": {
            "id": "inv-1",
            "fencingToken": 3,
            "idempotencyKey": "key-1",
            "inputs": inputs if inputs is not None else {"n": 2},
            "leaseExpiresAt": expires.isoformat(),
        },
        "skill": {
            "name": "arith.double",
            "version": "1",
            "contract": {
                "outputs": outputs or OUTPUTS,
                "timeoutSeconds": timeout,
                "implementation": implementation,
            },
        },
    }


def local(entrypoint: str) -> dict[str, Any]:
    return {"protocol": "local", "entrypoint": entrypoint}


def executor(fake: FakeControlPlane, **handlers: Any) -> SkillExecutor:
    return SkillExecutor(fake, handlers, heartbeat_interval=0.05)  # type: ignore[arg-type]


LOCAL_ENTRYPOINTS = [
    "tests.skill_stubs.arith:run",
    "tests.skill_stubs.arith:flaky",
    "tests.skill_stubs.arith:broken",
    "tests.skill_stubs.arith:wrong_shape",
    "tests.skill_stubs.arith:async_run",
    "tests.skill_stubs.arith:mark",
    "tests.skill_stubs.arith:crash",
]


# --- local --------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrypoint", ["tests.skill_stubs.arith:run", "tests.skill_stubs.arith:async_run"]
)
async def test_local_skill_completes_with_its_outputs(entrypoint: str) -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    outcome = await skills.execute_claimed(claimed(local(entrypoint), {"n": 21}), "session-1")
    assert outcome == "succeeded"
    assert fake.completed == [
        {"id": "inv-1", "fencing_token": 3, "output": {"double": 42}, "session_id": "session-1"}
    ]
    assert fake.failed == []


async def test_retryable_exception_is_a_retryable_failure() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    assert await skills.execute_claimed(claimed(local("tests.skill_stubs.arith:flaky")), None) == (
        "failed"
    )
    [failure] = fake.failed
    assert failure["code"] == "upstream_busy"
    assert failure["retryable"] is True
    assert "busy" in failure["message"]


async def test_other_exception_is_not_retryable() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    await skills.execute_claimed(claimed(local("tests.skill_stubs.arith:broken")), None)
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("skill_error", False)
    assert failure["message"] == "ValueError: cannot do that"


async def test_timeout_is_retryable() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    started = asyncio.get_running_loop().time()
    await skills.execute_claimed(
        claimed(local("tests.skill_stubs.arith:run"), {"n": 1, "sleep": 3}, timeout=1), None
    )
    assert asyncio.get_running_loop().time() - started < 2.5
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("timeout", True)
    assert fake.completed == []


async def test_outputs_are_checked_before_they_are_sent() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    await skills.execute_claimed(claimed(local("tests.skill_stubs.arith:wrong_shape")), None)
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("output_contract_violation", False)
    assert failure["details"]["errors"][0]["path"] == "/double"
    assert fake.completed == []


async def test_undeclared_entrypoint_is_never_imported() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(["tests.skill_stubs.arith:run"]))
    await skills.execute_claimed(claimed(local("os:system")), None)
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("entrypoint_not_installed", True)


async def test_lost_lease_drops_the_result() -> None:
    """A heartbeat answered with stale_invocation_lease: nothing is reported."""
    fake = FakeControlPlane(lease_lost_after=1)
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    outcome = await skills.execute_claimed(
        claimed(local("tests.skill_stubs.arith:run"), {"n": 1, "sleep": 0.5}), None
    )
    assert outcome == "lease_lost"
    assert fake.completed == []
    assert fake.failed == []


async def test_expired_lease_without_heartbeat_drops_the_result() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    job = claimed(local("tests.skill_stubs.arith:run"), {"n": 1, "sleep": 0.5})
    job["invocation"]["leaseExpiresAt"] = (datetime.now(UTC) + timedelta(seconds=0.2)).isoformat()

    async def unreachable(*args: Any, **kwargs: Any) -> dict:
        from control_plane_client import TransportError

        raise TransportError("transport_error", "down")

    fake.heartbeat_skill_invocation = unreachable  # type: ignore[method-assign]
    assert await skills.execute_claimed(job, None) == "lease_lost"
    assert fake.completed == [] and fake.failed == []


async def test_a_heartbeat_retrying_past_the_lease_does_not_outlive_it() -> None:
    """The client retries an unreachable core for minutes; the lease is shorter."""
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    job = claimed(local("tests.skill_stubs.arith:run"), {"n": 1, "sleep": 3})
    job["invocation"]["leaseExpiresAt"] = (datetime.now(UTC) + timedelta(seconds=0.3)).isoformat()

    async def retrying(*args: Any, **kwargs: Any) -> dict:
        await asyncio.sleep(60)
        raise AssertionError("not reached")

    fake.heartbeat_skill_invocation = retrying  # type: ignore[method-assign]
    started = time.monotonic()
    assert await skills.execute_claimed(job, None) == "lease_lost"
    assert time.monotonic() - started < 2.0
    assert fake.completed == [] and fake.failed == []


async def test_a_bad_gateway_on_a_skill_heartbeat_keeps_the_lease() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    skills.heartbeat_interval = 0.05
    beats = 0
    renew = fake.heartbeat_skill_invocation

    async def restarting(invocation_id: str, **kwargs: Any) -> dict:
        nonlocal beats
        beats += 1
        if beats == 1:
            raise ControlPlaneError("http_error", "Unexpected server error", status=502)
        return await renew(invocation_id, **kwargs)

    fake.heartbeat_skill_invocation = restarting  # type: ignore[method-assign]
    job = claimed(local("tests.skill_stubs.arith:run"), {"n": 1, "sleep": 0.4})
    assert await skills.execute_claimed(job, None) == "succeeded"
    assert beats >= 2
    assert len(fake.completed) == 1


async def test_timed_out_local_call_is_killed(tmp_path: Path) -> None:
    """A retry of the same invocation must not overlap with the abandoned attempt."""
    marker = tmp_path / "ran"
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    job = claimed(
        local("tests.skill_stubs.arith:mark"),
        {"n": 1, "sleep": 1.5, "marker": str(marker)},
        timeout=1,
    )
    await skills.execute_claimed(job, None)
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("timeout", True)
    await asyncio.sleep(1.5)
    assert not marker.exists()


async def test_thread_isolation_cannot_stop_the_call(tmp_path: Path) -> None:
    """The documented limit of ``isolation=thread``: the call runs on, its result is dropped."""
    marker = tmp_path / "ran"
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS, isolation="thread"))
    job = claimed(
        local("tests.skill_stubs.arith:mark"),
        {"n": 1, "sleep": 1.2, "marker": str(marker)},
        timeout=1,
    )
    await skills.execute_claimed(job, None)
    assert fake.failed[0]["code"] == "timeout"
    await asyncio.sleep(1.0)
    assert marker.exists()
    assert fake.completed == []


async def test_lost_lease_kills_the_local_call(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    fake = FakeControlPlane(lease_lost_after=1)
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    job = claimed(
        local("tests.skill_stubs.arith:mark"), {"n": 1, "sleep": 1, "marker": str(marker)}
    )
    assert await skills.execute_claimed(job, None) == "lease_lost"
    await asyncio.sleep(1.2)
    assert not marker.exists()


async def test_crashed_local_call_is_a_retryable_failure() -> None:
    fake = FakeControlPlane()
    skills = executor(fake, local=LocalProtocol(LOCAL_ENTRYPOINTS))
    await skills.execute_claimed(claimed(local("tests.skill_stubs.arith:crash")), None)
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("skill_crashed", True)


def test_packages_offer_the_entrypoints_their_contracts_declare() -> None:
    found = discover_local_entrypoints(
        ["tests.skill_stubs", "tests.skill_stubs.arith:flaky", "no.such.module:run"]
    )
    assert found == [
        "tests.skill_stubs.amount_match:run",
        "tests.skill_stubs.arith:run",
        "tests.skill_stubs.git_merge:run",
        "tests.skill_stubs.merge:run",
        "tests.skill_stubs.sdk_like:double",
        "tests.skill_stubs.arith:flaky",
    ]


def test_environment_builds_the_executor() -> None:
    assert executor_from_environment(object(), {}) is None  # type: ignore[arg-type]
    built = executor_from_environment(
        object(),  # type: ignore[arg-type]
        {
            "CONTROL_PLANE_SKILLS_LOCAL_PACKAGES": "tests.skill_stubs.arith",
            "CONTROL_PLANE_SKILLS_PROTOCOLS": "local,http mcp",
            "CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS": "https://Skills.test/ http://svc.internal:8080",
            "CONTROL_PLANE_SKILLS_MCP_SERVERS": '{"git": {"command": "git-mcp"}}',
            "CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES": "skill-service",
            "CONTROL_PLANE_SKILLS_PRIVATE_HOSTS": "svc.internal",
        },
    )
    assert built is not None
    assert built.protocols == ["http", "local", "mcp"]
    assert built.local_entrypoints == ["tests.skill_stubs.arith:run"]
    assert built.http_origins == ["http://svc.internal:8080", "https://skills.test"]
    assert built.mcp_endpoints == ["stdio:git"]
    assert built.audiences == ["skill-service"]
    assert built.concurrency == 1
    assert built.can_execute({"contract": {"implementation": http_skill()}})
    assert not built.can_execute(
        {"contract": {"implementation": {**http_skill(), "endpoint": "https://evil.test/x"}}}
    )
    assert not built.can_execute(
        {"contract": {"implementation": http_skill({"audience": "billing"})}}
    )
    assert built.capabilities == [
        "skills.protocol.http",
        "skills.protocol.local",
        "skills.protocol.mcp",
    ]
    assert built.can_execute({"contract": {"implementation": local("tests.skill_stubs.arith:run")}})
    assert not built.can_execute({"contract": {"implementation": local("x.y:z")}})
    with pytest.raises(ValueError):
        executor_from_environment(object(), {"CONTROL_PLANE_SKILLS_PROTOCOLS": "harness"})  # type: ignore[arg-type]


# --- http ---------------------------------------------------------------------


def http_skill(
    auth: dict[str, Any] | None = None, endpoint: str = "https://skills.test/double"
) -> dict[str, Any]:
    return {"protocol": "http", "endpoint": endpoint, "auth": auth}


def resolver(*addresses: str) -> Any:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def policy(**overrides: Any) -> EndpointPolicy:
    values: dict[str, Any] = {
        "origins": frozenset({"https://skills.test", "http://plain.test"}),
        "audiences": frozenset({"skill-service", "svc", "control-plane"}),
        "resolve": resolver("93.184.215.14"),
    }
    return EndpointPolicy(**{**values, **overrides})


async def run_http(
    handler: Any,
    *,
    auth: dict[str, Any] | None = None,
    tokens: Any = None,
    endpoint: str = "https://skills.test/double",
    rules: EndpointPolicy | None = None,
) -> FakeControlPlane:
    fake = FakeControlPlane()
    protocol = HttpProtocol(
        tokens, policy=rules or policy(), transport=httpx.MockTransport(handler)
    )
    await executor(fake, http=protocol).execute_claimed(claimed(http_skill(auth, endpoint)), None)
    return fake


def refuse_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request should be sent, got {request.url}")


async def test_http_posts_the_invocation_with_an_iam_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"double": 4})

    async def tokens(audience: str, scopes: tuple[str, ...] = ()) -> str:
        return f"token-for-{audience}"

    fake = await run_http(handler, auth={"audience": "skill-service"}, tokens=tokens)
    assert fake.completed[0]["output"] == {"double": 4}
    [request] = seen
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer token-for-skill-service"
    assert json.loads(request.content) == {
        "invocationId": "inv-1",
        "idempotencyKey": "key-1",
        "settings": None,
        "inputs": {"n": 2},
    }


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (404, False), (422, False), (500, True), (503, True)]
)
async def test_http_status_decides_retryability(status: int, retryable: bool) -> None:
    fake = await run_http(lambda request: httpx.Response(status, text="nope"))
    [failure] = fake.failed
    assert failure["code"] == f"http_{status}"
    assert failure["retryable"] is retryable
    assert failure["details"] == {"status": status, "body": "nope", "bodyTruncated": False}


async def test_http_error_body_is_an_excerpt_without_headers() -> None:
    body = "secret-" * 1000
    fake = await run_http(
        lambda request: httpx.Response(500, text=body, headers={"Set-Cookie": "session=x"})
    )
    details = fake.failed[0]["details"]
    assert details == {"status": 500, "body": body[:256], "bodyTruncated": True}


async def test_http_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake = await run_http(handler)
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("timeout", True)


async def test_http_redirect_is_not_followed() -> None:
    fake = await run_http(
        lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.test/"})
    )
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("unexpected_status", False)


async def test_http_endpoint_outside_the_allow_list_is_not_called() -> None:
    fake = await run_http(refuse_request, endpoint="http://127.0.0.1:8080/admin")
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("endpoint_not_allowed", True)
    fake = await run_http(refuse_request, endpoint="https://skills.test.evil.test/double")
    assert fake.failed[0]["code"] == "endpoint_not_allowed"
    fake = await run_http(refuse_request, endpoint="https://skills.test@evil.test/double")
    assert fake.failed[0]["code"] == "endpoint_not_allowed"


async def test_http_without_any_allowed_origin_calls_nothing() -> None:
    fake = FakeControlPlane()
    protocol = HttpProtocol(transport=httpx.MockTransport(refuse_request))
    skills = executor(fake, http=protocol)
    assert not skills.can_execute({"contract": {"implementation": http_skill()}})
    await skills.execute_claimed(claimed(http_skill()), None)
    assert fake.failed[0]["code"] == "endpoint_not_allowed"


async def test_http_token_goes_over_https_only() -> None:
    asked: list[str] = []

    async def tokens(audience: str, scopes: tuple[str, ...] = ()) -> str:
        asked.append(audience)
        return "token"

    fake = await run_http(
        refuse_request, auth={"audience": "svc"}, tokens=tokens, endpoint="http://plain.test/x"
    )
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("insecure_endpoint", False)
    assert asked == []

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"double": 4})

    fake = await run_http(handler, tokens=tokens, endpoint="http://plain.test/x")
    assert fake.completed and "Authorization" not in seen[0].headers
    assert asked == []


@pytest.mark.parametrize("audience", ["billing", "control-plane", "iam"])
async def test_http_token_only_for_an_allowed_audience(audience: str) -> None:
    """``control-plane`` and ``iam`` are refused even when a policy lists them."""
    asked: list[str] = []

    async def tokens(requested: str, scopes: tuple[str, ...] = ()) -> str:
        asked.append(requested)
        return "token"

    fake = await run_http(refuse_request, auth={"audience": audience}, tokens=tokens)
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("audience_not_allowed", True)
    assert asked == []


async def test_iam_token_source_withholds_the_daemons_own_audiences() -> None:
    tokens = iam_token_source(
        {"CONTROL_PLANE_IAM_URL": "https://iam.test", "CONTROL_PLANE_IAM_AUDIENCE": "cp-staging"}
    )
    assert tokens is not None
    for audience in ("control-plane", "iam", "cp-staging"):
        assert await tokens(audience) is None


IAM_ENVIRONMENT = {
    "CONTROL_PLANE_IAM_URL": "https://iam.test",
    "CONTROL_PLANE_IAM_TENANT": "tenant-1",
    "CONTROL_PLANE_IAM_SCOPES": "control-plane:read control-plane:write",
    "IAM_CREDENTIAL_MODE": "environment",
    "IAM_PLATFORM_ACCESS_TOKEN": "pat-runner",
}


def iam_exchange(asked: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        name = f"{body['audience']}|{' '.join(body['scopes'])}"
        return httpx.Response(200, json={"accessToken": name, "expiresIn": 600})

    return httpx.MockTransport(handler)


async def test_iam_token_source_asks_for_the_contracts_scopes_only() -> None:
    """A foreign audience gets the contract's scopes, never the core's."""
    asked: list[dict[str, Any]] = []
    tokens = iam_token_source(IAM_ENVIRONMENT, transport=iam_exchange(asked))
    assert tokens is not None
    token = await tokens("notification-service", ("notifications:send",))
    assert token == "notification-service|notifications:send"
    assert await tokens("notification-service", ()) == "notification-service|"
    assert [(body["audience"], body["scopes"]) for body in asked] == [
        ("notification-service", ["notifications:send"]),
        ("notification-service", []),
    ]
    assert not any(scope.startswith("control-plane:") for b in asked for scope in b["scopes"])


async def test_iam_token_source_keeps_one_credential_per_audience_and_scopes() -> None:
    asked: list[dict[str, Any]] = []
    tokens = iam_token_source(IAM_ENVIRONMENT, transport=iam_exchange(asked))
    assert tokens is not None
    send = await tokens("notification-service", ("notifications:send",))
    read = await tokens("notification-service", ("notifications:read",))
    assert send != read
    # Cached per pair; the order of scopes does not make a new credential.
    assert await tokens("notification-service", ("notifications:send",)) == send
    both = await tokens("svc", ("b:x", "a:y"))
    assert await tokens("svc", ("a:y", "b:x")) == both
    assert len(asked) == 3


async def test_http_asks_for_the_scopes_the_contract_names() -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def tokens(audience: str, scopes: tuple[str, ...] = ()) -> str:
        asked.append((audience, scopes))
        return "token"

    auth = {"audience": "svc", "scopes": ["notifications:send"]}
    fake = await run_http(
        lambda r: httpx.Response(200, json={"double": 4}), auth=auth, tokens=tokens
    )
    assert fake.completed
    assert asked == [("svc", ("notifications:send",))]


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "::1", "fd00::1"]
)
async def test_http_host_resolving_to_a_private_address_is_refused(address: str) -> None:
    fake = await run_http(refuse_request, rules=policy(resolve=resolver("93.184.215.14", address)))
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == (
        "endpoint_address_forbidden",
        False,
    )


async def test_http_connects_to_the_address_it_checked() -> None:
    """Explicitly trusted host: private address allowed; the name stays in Host and SNI."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"double": 4})

    rules = policy(resolve=resolver("10.0.0.5"), private_hosts=frozenset({"skills.test"}))
    fake = await run_http(handler, rules=rules)
    assert fake.completed
    [request] = seen
    assert request.url.host == "10.0.0.5"
    assert request.headers["Host"] == "skills.test"
    assert request.extensions["sni_hostname"] == "skills.test"


def test_address_classification_and_origins() -> None:
    assert is_public_address("203.0.113.7") is False  # TEST-NET-3 is reserved
    assert is_public_address("8.8.8.8") is True
    assert is_public_address("2606:4700::1111") is True
    assert is_public_address("::ffff:127.0.0.1") is False
    assert is_public_address("224.0.0.1") is False
    assert origin_of("HTTPS://Skills.Test:443/a?b") == "https://skills.test"
    assert origin_of("http://[::1]:8080/") == "http://[::1]:8080"
    with pytest.raises(ValueError):
        origin_of("https://user:pw@skills.test/")
    with pytest.raises(ValueError):
        origin_of("file:///etc/passwd")


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES": "svc control-plane"}, "control-plane"),
        ({"CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES": "iam"}, "iam"),
        (
            {
                "CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES": "cp-prod",
                "CONTROL_PLANE_IAM_AUDIENCE": "cp-prod",
            },
            "cp-prod",
        ),
        ({"CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS": "https://skills.test/api"}, "origin"),
        ({"CONTROL_PLANE_SKILLS_CONCURRENCY": "-1"}, "CONCURRENCY"),
    ],
)
def test_environment_refuses_unsafe_configuration(environ: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        executor_from_environment(
            object(),  # type: ignore[arg-type]
            {"CONTROL_PLANE_SKILLS_PROTOCOLS": "http", **environ},
        )


def test_remote_protocol_without_an_allow_list_is_not_run() -> None:
    assert (
        executor_from_environment(object(), {"CONTROL_PLANE_SKILLS_PROTOCOLS": "http mcp"})  # type: ignore[arg-type]
        is None
    )


async def test_http_without_a_token_source_fails_retryably() -> None:
    fake = await run_http(lambda r: httpx.Response(200, json={}), auth={"audience": "svc"})
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == (
        "executor_auth_unavailable",
        True,
    )


# --- mcp ----------------------------------------------------------------------


def mcp_server() -> MCPServer:
    server = MCPServer("skills")

    @server.tool()
    def double(n: int) -> dict[str, int]:
        return {"double": n * 2}

    @server.tool()
    def refuse(n: int) -> dict[str, int]:
        raise ValueError("refused")

    return server


async def run_mcp(tool: str) -> FakeControlPlane:
    fake = FakeControlPlane()
    server = mcp_server()
    protocol = McpProtocol(connect=lambda implementation: server)
    implementation = {"protocol": "mcp", "endpoint": "stdio:skills", "entrypoint": tool}
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    return fake


async def test_mcp_tool_result_becomes_the_outputs() -> None:
    fake = await run_mcp("double")
    assert fake.failed == []
    assert fake.completed[0]["output"] == {"double": 4}


async def test_mcp_tool_error_is_not_retryable() -> None:
    fake = await run_mcp("refuse")
    [failure] = fake.failed
    assert failure["retryable"] is False
    assert failure["code"] in {"tool_error", "mcp_error"}


async def run_remote_mcp(
    endpoint: str, *, auth: dict[str, Any] | None = None, rules: EndpointPolicy | None = None
) -> FakeControlPlane:
    fake = FakeControlPlane()

    def connect(implementation: dict[str, Any]) -> Any:
        raise AssertionError("no connection should be made")

    protocol = McpProtocol(policy=rules or policy(), connect=connect)
    implementation = {"protocol": "mcp", "endpoint": endpoint, "entrypoint": "double", "auth": auth}
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    return fake


async def test_mcp_over_http_obeys_the_same_policy() -> None:
    fake = await run_remote_mcp("https://evil.test/mcp")
    assert fake.failed[0]["code"] == "endpoint_not_allowed"
    fake = await run_remote_mcp("http://plain.test/mcp", auth={"audience": "svc"})
    assert fake.failed[0]["code"] == "insecure_endpoint"
    fake = await run_remote_mcp("https://skills.test/mcp", auth={"audience": "control-plane"})
    assert fake.failed[0]["code"] == "audience_not_allowed"


async def test_mcp_over_http_refuses_a_private_address() -> None:
    fake = FakeControlPlane()
    protocol = McpProtocol(policy=policy(resolve=resolver("127.0.0.1")))
    implementation = {"protocol": "mcp", "endpoint": "https://skills.test/mcp", "entrypoint": "x"}
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    assert fake.failed[0]["code"] == "endpoint_address_forbidden"


def guarded_mcp_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    rules: EndpointPolicy | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """``_guarded_mcp_client`` with a mock transport beneath its guard."""
    import httpx2

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handler))
    return _guarded_mcp_client(rules or policy(), headers or {})


async def test_mcp_client_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx2.Response(307, headers={"Location": "https://elsewhere.test/mcp"})

    client = guarded_mcp_client(monkeypatch, handler, headers={"Authorization": "Bearer t"})
    assert client.follow_redirects is False
    async with client:
        response = await client.post("https://skills.test/mcp", json={})
    assert response.status_code == 307
    [request] = seen
    assert request.headers["Host"] == "skills.test"


async def test_mcp_over_http_does_not_follow_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the SDK: every request goes to the checked endpoint, none to the Location."""
    import httpx2

    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx2.Response(307, headers={"Location": "https://elsewhere.test/mcp"})

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handler))
    fake = FakeControlPlane()
    implementation = {"protocol": "mcp", "endpoint": "https://skills.test/mcp", "entrypoint": "x"}
    # The Location is an allowed origin too: only not following keeps the request home.
    origins = frozenset({"https://skills.test", "https://elsewhere.test"})
    protocol = McpProtocol(policy=policy(origins=origins))
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    assert fake.failed and not fake.completed
    assert seen
    assert {(r.url.host, r.headers["Host"]) for r in seen} == {("93.184.215.14", "skills.test")}


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://evil.test/mcp", "endpoint_not_allowed"),
        ("https://skills.test@evil.test/mcp", "endpoint_not_allowed"),
        ("http://127.0.0.1:8080/admin", "endpoint_not_allowed"),
        ("https://skills.test.evil.test/mcp", "endpoint_not_allowed"),
    ],
)
async def test_mcp_client_refuses_a_forbidden_origin(
    monkeypatch: pytest.MonkeyPatch, url: str, code: str
) -> None:
    client = guarded_mcp_client(monkeypatch, refuse_request)
    async with client:
        with pytest.raises(SkillFailure) as raised:
            await client.post(url, json={})
    assert raised.value.code == code


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fd00::1"])
async def test_mcp_client_refuses_a_private_address(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    rules = policy(resolve=resolver("93.184.215.14", address))
    client = guarded_mcp_client(monkeypatch, refuse_request, rules)
    async with client:
        with pytest.raises(SkillFailure) as raised:
            await client.post("https://skills.test/mcp", json={})
    assert (raised.value.code, raised.value.retryable) == ("endpoint_address_forbidden", False)


async def test_mcp_client_connects_to_the_address_it_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx2.Response(200, json={})

    trusted = frozenset({"skills.test", "plain.test"})
    rules = policy(resolve=resolver("10.0.0.5"), private_hosts=trusted)
    client = guarded_mcp_client(monkeypatch, handler, rules)
    async with client:
        await client.post("https://skills.test/mcp", json={})
        await client.post("http://plain.test/mcp", json={})
    https, http = seen
    assert (https.url.host, https.headers["Host"]) == ("10.0.0.5", "skills.test")
    assert https.extensions["sni_hostname"] == "skills.test"
    # SNI is a TLS matter: plain http gets none.
    assert (http.url.host, http.headers["Host"]) == ("10.0.0.5", "plain.test")
    assert "sni_hostname" not in http.extensions


async def test_mcp_client_ignores_proxies_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx2.Response(200, json={})

    client = guarded_mcp_client(monkeypatch, handler)
    assert client.trust_env is False
    async with client:
        await client.post("https://skills.test/mcp", json={})
    # Reached the guarded transport, not a proxy mounted from the environment.
    assert len(seen) == 1


def test_mcp_declares_its_origins_and_stdio_servers() -> None:
    protocol = McpProtocol({"git": {"command": "git-mcp"}}, policy=policy())
    assert protocol.endpoints == ["http://plain.test", "https://skills.test", "stdio:git"]
    assert protocol.admits({"endpoint": "stdio:git"})
    assert not protocol.admits({"endpoint": "stdio:other"})
    assert not protocol.admits({"endpoint": "https://evil.test/mcp"})


async def test_mcp_unknown_stdio_server_is_retryable() -> None:
    fake = FakeControlPlane()
    implementation = {"protocol": "mcp", "endpoint": "stdio:absent", "entrypoint": "double"}
    await executor(fake, mcp=McpProtocol({})).execute_claimed(claimed(implementation), None)
    assert (fake.failed[0]["code"], fake.failed[0]["retryable"]) == ("mcp_server_unknown", True)


# --- inputs of execution-typed Work ---------------------------------------------


TASK = {
    "id": "t-1",
    "publicId": "TASK-1",
    "customFields": {"branch": "task/TASK-1", "targets": [{"ref": "main"}], "empty": None},
}


def test_input_paths() -> None:
    assert resolve_path(TASK, "$.customFields.targets[0].ref") == "main"
    assert resolve_path(TASK, "$.publicId") == "TASK-1"
    assert resolve_path(TASK, "$.customFields.empty") is None
    with pytest.raises(ValueError):
        resolve_path(TASK, "customFields")


def test_inputs_mapping() -> None:
    assert map_inputs(None, TASK) == TASK["customFields"]
    assert map_inputs("$.customFields.targets[0]", TASK) == {"ref": "main"}
    assert map_inputs(
        {
            "branch": "$.customFields.branch",
            "into": "$.customFields.targets[0].ref",
            "missing": "$.customFields.nothing",
            "task": "$.publicId",
        },
        TASK,
    ) == {"branch": "task/TASK-1", "into": "main", "task": "TASK-1"}


# --- skill-sdk wire contract (TAI-ADR-0045) -----------------------------------------

SDK_OUTPUTS = {
    "type": "object",
    "properties": {"double": {"type": "integer"}},
    "required": ["double"],
}


def test_sdk_skills_are_discovered_by_their_contract() -> None:
    assert discover_local_entrypoints(["tests.skill_stubs.sdk_like"]) == [
        "tests.skill_stubs.sdk_like:double"
    ]


@pytest.mark.parametrize("isolation", ["process", "thread"])
async def test_sdk_local_skill_gets_the_invocation_and_reports_its_cost(isolation: str) -> None:
    fake = FakeControlPlane()
    protocol = LocalProtocol(["tests.skill_stubs.sdk_like:double"], isolation=isolation)
    skills = executor(fake, local=protocol)
    outcome = await skills.execute_claimed(
        claimed(local("tests.skill_stubs.sdk_like:double"), {"n": 3}, outputs=SDK_OUTPUTS), "s-1"
    )
    assert outcome == "succeeded"
    [completed] = fake.completed
    assert completed["output"]["double"] == 6
    assert completed["output"]["seen"] == {
        "invocationId": "inv-1",
        "idempotencyKey": "key-1",
        "timeoutSeconds": 5.0,
        "skill": "arith.double@1",
        "settings": None,
    }
    assert completed["cost"] == {"units": {"ops": 1.0}}


async def test_http_cost_header_reaches_the_report() -> None:
    fake = await run_http(
        lambda request: httpx.Response(
            200, json={"double": 4}, headers={"X-Skill-Cost": '{"units":{"pages":2}}'}
        )
    )
    assert fake.completed[0]["cost"] == {"units": {"pages": 2}}


@pytest.mark.parametrize(("status", "retryable"), [(422, False), (503, True), (500, False)])
async def test_http_error_envelope_names_code_and_retryability(
    status: int, retryable: bool
) -> None:
    body = {"error": {"code": "git_unavailable", "message": "remote down", "retryable": retryable}}
    fake = await run_http(lambda request: httpx.Response(status, json=body))
    [failure] = fake.failed
    assert failure["code"] == "git_unavailable"
    assert failure["retryable"] is retryable
    assert failure["details"]["status"] == status


def sdk_like_mcp_server() -> Any:
    from mcp import types
    from mcp.server.lowlevel import Server

    async def call_tool(_ctx: Any, params: Any) -> Any:
        if params.name == "busy":
            error = {"error": {"code": "upstream_busy", "message": "later", "retryable": True}}
            return types.CallToolResult(
                content=[types.TextContent(text=json.dumps(error))], is_error=True
            )
        output = {"double": params.arguments["n"] * 2}
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(output))],
            structured_content=output,
            meta={"skill/cost": {"units": {"ops": 1}}},
        )

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        return types.ListToolsResult(
            tools=[types.Tool(name=name, input_schema=schema) for name in ("double", "busy")]
        )

    return Server("sdk-like", on_list_tools=list_tools, on_call_tool=call_tool)


async def run_sdk_like_mcp(tool: str) -> FakeControlPlane:
    fake = FakeControlPlane()
    server = sdk_like_mcp_server()
    protocol = McpProtocol(connect=lambda implementation: server)
    implementation = {"protocol": "mcp", "endpoint": "stdio:skills", "entrypoint": tool}
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    return fake


async def test_mcp_cost_meta_reaches_the_report() -> None:
    fake = await run_sdk_like_mcp("double")
    assert fake.completed[0]["output"] == {"double": 4}
    assert fake.completed[0]["cost"] == {"units": {"ops": 1}}


async def test_mcp_error_envelope_names_code_and_retryability() -> None:
    fake = await run_sdk_like_mcp("busy")
    [failure] = fake.failed
    assert (failure["code"], failure["retryable"]) == ("upstream_busy", True)


def idempotent_mcp_server(effects: list[dict[str, Any]]) -> Any:
    """A skill that does its effect once per ``skill/idempotencyKey``."""
    from mcp import types
    from mcp.server.lowlevel import Server

    done: dict[str, dict[str, Any]] = {}

    async def call_tool(_ctx: Any, params: Any) -> Any:
        meta = dict(params.meta or {})
        key = meta["skill/idempotencyKey"]
        if key not in done:
            effects.append(meta)
            done[key] = {"double": params.arguments["n"] * 2}
        output = done[key]
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(output))], structured_content=output
        )

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        return types.ListToolsResult(tools=[types.Tool(name="double", input_schema=schema)])

    return Server("idempotent", on_list_tools=list_tools, on_call_tool=call_tool)


async def test_mcp_call_carries_invocation_and_idempotency_key_in_meta() -> None:
    effects: list[dict[str, Any]] = []
    server = idempotent_mcp_server(effects)
    protocol = McpProtocol(connect=lambda implementation: server)
    implementation = {"protocol": "mcp", "endpoint": "stdio:skills", "entrypoint": "double"}
    fake = FakeControlPlane()
    await executor(fake, mcp=protocol).execute_claimed(claimed(implementation), None)
    assert fake.completed[0]["output"] == {"double": 4}
    [meta] = effects
    assert meta["skill/invocationId"] == "inv-1"
    assert meta["skill/idempotencyKey"] == "key-1"
    # The MCP client leaves a null member out of _meta: absent means null.
    assert meta.get("skill/settings") is None


async def test_mcp_retry_with_the_same_key_does_not_repeat_the_effect() -> None:
    effects: list[dict[str, Any]] = []
    server = idempotent_mcp_server(effects)
    protocol = McpProtocol(connect=lambda implementation: server)
    implementation = {"protocol": "mcp", "endpoint": "stdio:skills", "entrypoint": "double"}
    fake = FakeControlPlane()
    skills = executor(fake, mcp=protocol)
    for _ in range(2):
        await skills.execute_claimed(claimed(implementation), None)
    assert [c["output"] for c in fake.completed] == [{"double": 4}, {"double": 4}]
    assert len(effects) == 1


# --- settings of the skills (CP-ADR-0073, amendment 2026-10-01) ---------------------------

SETTINGS_ENTRYPOINT = "tests.skill_stubs.settings:run"


def _settings_call(name: str) -> SkillCall:
    return SkillCall(
        invocation_id="inv-settings",
        idempotency_key=None,
        inputs={"name": name},
        skill="settings.read@1",
        implementation={"protocol": "local", "entrypoint": SETTINGS_ENTRYPOINT},
        timeout_seconds=30,
    )


async def test_a_local_skill_reads_its_settings_in_its_own_process() -> None:
    local = LocalProtocol(
        [SETTINGS_ENTRYPOINT], environment={"PORTAL_URL": "https://portal.example.test"}
    )
    outcome = await local.call(_settings_call("PORTAL_URL"))
    assert outcome.output == {"value": "https://portal.example.test"}
    # The daemon's own environment is not changed by a child's settings.
    assert "PORTAL_URL" not in os.environ


async def test_a_local_skill_in_a_thread_reads_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORTAL_TIMEOUT", raising=False)
    local = LocalProtocol(
        [SETTINGS_ENTRYPOINT], isolation="thread", environment={"PORTAL_TIMEOUT": "30"}
    )
    try:
        outcome = await local.call(_settings_call("PORTAL_TIMEOUT"))
    finally:
        os.environ.pop("PORTAL_TIMEOUT", None)
    assert outcome.output == {"value": "30"}


def test_the_settings_reach_the_local_protocol_from_the_environment() -> None:
    environ = {
        ENV_LOCAL_PACKAGES: SETTINGS_ENTRYPOINT,
        ENV_LOCAL_ENV: json.dumps({"PORTAL_URL": "https://portal.example.test"}),
    }
    skills = executor_from_environment(FakeControlPlane(), environ)  # type: ignore[arg-type]
    assert skills is not None
    local = skills.handlers["local"]
    assert isinstance(local, LocalProtocol)
    assert local.environment == {"PORTAL_URL": "https://portal.example.test"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not json", "must be a JSON object"),
        ("[]", "must be an object"),
        ('{"PORTAL_PASSWORD": "x"}', "a secret"),
        ('{"PATH": "/tmp"}', "belongs to the host"),
        ('{"IAM_CLIENT_ID": "x"}', "belongs to the host"),
        ('{"portal_url": "x"}', "not a variable name"),
        ('{"PORTAL_URL": 5}', "must be a string"),
        (json.dumps({"PORTAL_URL": "x" * 2001}), "must be a string"),
    ],
)
def test_bad_settings_are_a_configuration_error(value: str, message: str) -> None:
    environ = {ENV_LOCAL_PACKAGES: SETTINGS_ENTRYPOINT, ENV_LOCAL_ENV: value}
    with pytest.raises(ValueError, match=message):
        executor_from_environment(FakeControlPlane(), environ)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        # Where the host goes and whom it trusts.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        # Families of the host's loaders and tools, by prefix.
        "XDG_CONFIG_HOME",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_GLOBAL",
        "NODE_OPTIONS",
        "NODE_EXTRA_CA_CERTS",
        "DYLD_INSERT_LIBRARIES",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        # The families refused before.
        "CONTROL_PLANE_URL",
        "IAM_CLIENT_ID",
        "PYTHONPATH",
        "LD_PRELOAD",
        "PATH",
        "TMPDIR",
    ],
)
def test_a_setting_of_the_host_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match=f"env.{name} belongs to the host"):
        parse_skill_env({name: "x"}, where="env")


@pytest.mark.parametrize(
    "name", ["http_proxy", "https_proxy", "Https_Proxy", "all_proxy", "no_proxy", "node_options"]
)
def test_a_lower_case_host_setting_is_refused_too(name: str) -> None:
    with pytest.raises(ValueError):
        parse_skill_env({name: "x"}, where="env")


def test_the_reserved_check_ignores_case() -> None:
    # The name pattern already refuses lower case; the reserved check does not lean on it.
    assert _skill_env_reserved("https_proxy")
    assert _skill_env_reserved("Git_Dir")
    assert not _skill_env_reserved("portal_url")


@pytest.mark.parametrize(
    "name",
    [
        "PORTAL_URL",
        "PORTAL_BASE_URL",
        "API_BASE_URL",
        "CRM_URL",
        "FETCH_LIMIT",
        # A prefix is a prefix: the family name inside or at the end is fine.
        "PORTAL_GIT_URL",
        "MY_NODE_URL",
        "PROXY_URL",
        "PIPELINE_URL",
        "UVA_URL",
        "PATH_PREFIX",
    ],
)
def test_a_setting_of_the_package_passes(name: str) -> None:
    assert parse_skill_env({name: "https://portal.example.test"}, where="env") == {
        name: "https://portal.example.test"
    }


def test_no_settings_and_empty_settings_are_none() -> None:
    assert parse_skill_env(None, where="env") == {}
    assert parse_skill_env({}, where="env") == {}


# --- settings of the skill's package (CP-ADR-0081 §8) ---------------------------

SETTINGS = {
    "package": "sample",
    "version": 3,
    "schemaRevision": 2,
    "values": {"limit": 1000, "window": {"start": 9}},
}


def with_settings(claim: dict[str, Any], settings: Any) -> dict[str, Any]:
    claim["settings"] = settings
    return claim


@pytest.mark.parametrize("isolation", ["process", "thread"])
async def test_sdk_local_skill_gets_the_settings_of_its_package(isolation: str) -> None:
    fake = FakeControlPlane()
    protocol = LocalProtocol(["tests.skill_stubs.sdk_like:double"], isolation=isolation)
    claim = claimed(local("tests.skill_stubs.sdk_like:double"), {"n": 3}, outputs=SDK_OUTPUTS)
    await executor(fake, local=protocol).execute_claimed(with_settings(claim, SETTINGS), "s-1")
    [completed] = fake.completed
    assert completed["output"]["seen"]["settings"] == SETTINGS


async def test_http_body_carries_the_settings_of_the_package() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"double": 4})

    fake = FakeControlPlane()
    protocol = HttpProtocol(None, policy=policy(), transport=httpx.MockTransport(handler))
    claim = with_settings(claimed(http_skill(None, "https://skills.test/double")), SETTINGS)
    await executor(fake, http=protocol).execute_claimed(claim, None)
    assert fake.completed[0]["output"] == {"double": 4}
    assert json.loads(seen[0].content)["settings"] == SETTINGS


async def test_mcp_meta_carries_the_settings_of_the_package() -> None:
    effects: list[dict[str, Any]] = []
    protocol = McpProtocol(connect=lambda implementation: idempotent_mcp_server(effects))
    implementation = {"protocol": "mcp", "endpoint": "stdio:skills", "entrypoint": "double"}
    fake = FakeControlPlane()
    claim = with_settings(claimed(implementation), SETTINGS)
    await executor(fake, mcp=protocol).execute_claimed(claim, None)
    [meta] = effects
    assert meta["skill/settings"] == SETTINGS


@pytest.mark.parametrize("settings", [None, "values", ["a"], 3, True])
async def test_a_claim_without_settings_or_with_a_wrong_type_gives_none(settings: Any) -> None:
    fake = FakeControlPlane()
    protocol = LocalProtocol(["tests.skill_stubs.sdk_like:double"], isolation="thread")
    claim = claimed(local("tests.skill_stubs.sdk_like:double"), {"n": 3}, outputs=SDK_OUTPUTS)
    await executor(fake, local=protocol).execute_claimed(with_settings(claim, settings), "s-1")
    assert fake.completed[0]["output"]["seen"]["settings"] is None


async def test_a_claim_of_a_core_without_settings_gives_none() -> None:
    fake = FakeControlPlane()
    protocol = LocalProtocol(["tests.skill_stubs.sdk_like:double"], isolation="thread")
    claim = claimed(local("tests.skill_stubs.sdk_like:double"), {"n": 3}, outputs=SDK_OUTPUTS)
    assert "settings" not in claim
    await executor(fake, local=protocol).execute_claimed(claim, "s-1")
    assert fake.completed[0]["output"]["seen"]["settings"] is None
