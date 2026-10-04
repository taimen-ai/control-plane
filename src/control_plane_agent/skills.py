"""Skill executor: the ``skill`` adapter of the agent daemon (ADR-0056 §5).

The daemon that runs Work also runs skill invocations — same process, same
container, same identity. When there is no Work, it claims one ``pending``
invocation it can execute (it declares its protocols and the ``local``
entrypoints installed next to it), keeps the lease alive, calls the
implementation and reports ``:complete`` with the outputs or ``:fail`` with
``{code, retryable, message}``.

This is transport, not domain (TAI-ADR-0041): what a skill means lives in the
skill; what the executor knows is the contract — the protocol, the timeout,
the output schema. The outputs are checked against that schema before they
are sent, so a broken implementation gets a clear ``output_contract_violation``
here rather than only the core's verdict; the core checks them again anyway.

A lease lost while the implementation runs (``stale_invocation_lease``, the
lease deadline passed without a successful heartbeat) means the result is no
longer ours to report: it is dropped, never sent.

``settings`` of every protocol is what the claim handed out: the effective
settings of the skill's package, ``{package, version, schemaRevision,
values}``, or ``null`` for a skill not from a package (CP-ADR-0081 §8); the
``_meta`` of an ``mcp`` call leaves a ``null`` member out.

Protocols:

- ``local`` — ``module:function`` run under the contract's ``timeoutSeconds``:
  by default in a child process that is killed on timeout or a lost lease
  (``CONTROL_PLANE_SKILLS_LOCAL_ISOLATION=thread`` runs it in a worker thread
  of this process instead, which cannot be stopped). An exception with
  ``retryable = True`` is a retryable failure; its ``code`` (if any) names it.
  An entrypoint that carries ``__skill_invoke__(inputs, meta)`` (a skill of
  skill-sdk, TAI-ADR-0045) is called through it: it gets the invocation id,
  idempotency key, timeout and ``settings`` and answers ``{outputs, cost}``.
- ``http`` — ``POST implementation.endpoint`` with ``{invocationId,
  idempotencyKey, settings, inputs}`` and, over ``https`` only, a Bearer token of IAM
  audience ``implementation.auth.audience``. A 2xx body is the outputs
  object (``X-Skill-Cost`` — its cost); 4xx is a non-retryable failure; 5xx, a
  timeout or a transport error is retryable. An error body ``{"error": {code,
  retryable}}`` (skill-sdk) names the code and the retryability itself.
- ``mcp`` — ``tools/call`` of the tool ``implementation.entrypoint`` on the
  server ``implementation.endpoint``: an ``http(s)://`` URL (streamable HTTP)
  or ``stdio:<name>``, a server this executor starts from its own
  configuration (``CONTROL_PLANE_SKILLS_MCP_SERVERS``). The request's
  ``_meta`` carries ``skill/invocationId``, ``skill/idempotencyKey`` and
  ``skill/settings`` — the values ``http`` puts in the body. Structured content —
  or a text block holding one JSON object — is the outputs, ``_meta`` key
  ``skill/cost`` its cost; an ``isError`` result whose one text block is
  ``{"error": {code, retryable}}`` names the failure.

The endpoint and the audience come from a contract a tenant administrator
published; the network and the token are this executor's. So neither is
taken on the contract's word (ADR-0056 amendment M2.2, D): the executor
reaches only the origins it was configured with, resolves the host itself and
refuses non-public addresses unless the host is explicitly trusted with them,
connects to the address it checked, and asks IAM only for the audiences it
was configured with — never the core's own.

Scopes of that token are the contract's ``implementation.auth.scopes``: the
contract names them, the executor asks IAM for them, and IAM cuts the request
by the ``scopeCeiling`` of the executor's PAT — the ceiling, not the contract,
is the limit of what a skill's token can do. The core's own scopes
(``CONTROL_PLANE_IAM_SCOPES``) never go to another audience; a contract
without ``auth.scopes`` gets a token asked for with no scopes.
"""

import asyncio
import contextlib
import importlib
import inspect
import ipaddress
import json
import logging
import multiprocessing
import os
import pkgutil
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
import jsonschema

from control_plane_agent.workspace import redact_local_paths
from control_plane_client import ControlPlaneClient, ControlPlaneError, is_transient

if TYPE_CHECKING:
    from mcp.types import RequestParamsMeta

logger = logging.getLogger("control_plane_agent.skills")

ENV_PROTOCOLS = "CONTROL_PLANE_SKILLS_PROTOCOLS"
ENV_LOCAL_PACKAGES = "CONTROL_PLANE_SKILLS_LOCAL_PACKAGES"
ENV_MCP_SERVERS = "CONTROL_PLANE_SKILLS_MCP_SERVERS"
ENV_HTTP_ORIGINS = "CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS"
ENV_MCP_ORIGINS = "CONTROL_PLANE_SKILLS_MCP_ALLOWED_ORIGINS"
ENV_AUDIENCES = "CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES"
ENV_PRIVATE_HOSTS = "CONTROL_PLANE_SKILLS_PRIVATE_HOSTS"
ENV_LOCAL_ISOLATION = "CONTROL_PLANE_SKILLS_LOCAL_ISOLATION"
ENV_CONCURRENCY = "CONTROL_PLANE_SKILLS_CONCURRENCY"
#: JSON ``{NAME: value}`` — non-secret settings of the skills (a portal's
#: address), set in the environment of each ``local`` call (CP-ADR-0073,
#: amendment 2026-10-01: ``executor.params.env`` of a ``skills`` agent).
ENV_LOCAL_ENV = "CONTROL_PLANE_SKILLS_LOCAL_ENV"

#: ``params.env`` of a ``skills`` executor: names, sizes, what is refused.
SKILL_ENV_MAX_ITEMS = 50
SKILL_ENV_MAX_VALUE = 2000
_SKILL_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
# A secret goes through the node's secret files, never through a description.
_SKILL_ENV_SECRET = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY|API_KEY)$")
# The host's own settings are not the package's to set: with ``thread``
# isolation they land in the daemon's own environment. Refused are whole
# families by prefix (the daemon, its loaders and the tools it runs: git, node,
# uv, pip, XDG dirs) and single names (where it goes and whom it trusts:
# proxies and CA bundles). Compared upper-cased, so ``https_proxy`` is too.
_SKILL_ENV_RESERVED = (
    "CONTROL_PLANE_",
    "IAM_",
    "PYTHON",
    "LD_",
    "DYLD_",
    "GIT_",
    "NODE_",
    "UV_",
    "PIP_",
    "XDG_",
)
_SKILL_ENV_FIXED = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)

PROTOCOLS = ("local", "http", "mcp")
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
#: Server codes meaning "this lease is no longer yours".
LEASE_LOST_CODES = frozenset({"stale_invocation_lease", "not_found", "session_not_active"})
MAX_MESSAGE = 4000
#: How much of an error response body goes into ``error.details`` — enough to
#: tell what the service said, not enough to carry what it holds.
MAX_BODY_EXCERPT = 256
#: Audiences a skill never gets a token for, whatever the configuration says:
#: a token of the core or of IAM is the executor's own authority, not a grant.
RESERVED_AUDIENCES = frozenset({"control-plane", "iam"})
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
#: Where skill-sdk hosts put the cost of a call (TAI-ADR-0045 п.5).
COST_HEADER = "X-Skill-Cost"
MCP_COST_META = "skill/cost"
#: Where the executor puts the invocation of an ``mcp`` call (request ``_meta``).
MCP_INVOCATION_META = "skill/invocationId"
MCP_IDEMPOTENCY_META = "skill/idempotencyKey"
MCP_SETTINGS_META = "skill/settings"
#: The local wire contract of skill-sdk: ``(inputs, meta) -> {outputs, cost}``.
SDK_INVOKE = "__skill_invoke__"
SDK_CONTRACT = "__skill_contract__"


class SkillFailure(Exception):
    """A failed call, as reported to ``:fail``."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": redact_local_paths(self.message)[:MAX_MESSAGE],
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass(frozen=True)
class SkillCall:
    """One attempt of one invocation, as a protocol sees it."""

    invocation_id: str
    idempotency_key: str | None
    inputs: dict[str, Any]
    skill: str
    implementation: dict[str, Any]
    timeout_seconds: float
    #: The settings of the skill's package the claim handed out (CP-ADR-0081 §8):
    #: ``{package, version, schemaRevision, values}``; ``None`` — not from a package.
    settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class SkillOutcome:
    """Outputs together with the cost the implementation reported."""

    output: Any
    cost: dict[str, Any] | None = None


def _cost_of(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and value else None


def _error_envelope(value: Any) -> SkillFailure | None:
    """``{"error": {code, message, retryable, details}}`` of a skill-sdk host."""
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not _CODE_RE.match(code):
        return None
    details = error.get("details")
    return SkillFailure(
        code,
        str(error.get("message") or code),
        retryable=error.get("retryable") is True,
        details=details if isinstance(details, dict) else None,
    )


class ProtocolHandler(Protocol):
    def admits(self, implementation: dict[str, Any]) -> bool: ...

    async def call(self, call: SkillCall) -> Any: ...


#: ``(audience, scopes) -> access token`` for a skill service, or None.
TokenSource = Callable[[str, tuple[str, ...]], Awaitable[str | None]]


# --- local --------------------------------------------------------------------


def _resolve_entrypoint(entrypoint: str) -> Callable[..., Any]:
    module_name, _, attribute = entrypoint.partition(":")
    target = getattr(importlib.import_module(module_name), attribute)
    if not callable(target):
        raise TypeError(f"{entrypoint} is not callable")
    return target  # type: ignore[no-any-return]


def _local_entrypoint_of(contract: Any) -> list[str]:
    if not isinstance(contract, Mapping):
        return []
    implementation = contract.get("implementation") or {}
    if implementation.get("protocol") == "local" and implementation.get("entrypoint"):
        return [str(implementation["entrypoint"])]
    return []


def _local_contract_entrypoints(module: Any) -> list[str]:
    """A module-level ``CONTRACT``, and every skill-sdk skill defined in the module."""
    found = _local_entrypoint_of(getattr(module, "CONTRACT", None))
    for value in list(vars(module).values()):
        # Looked up on the type: an arbitrary object's __getattr__ is not asked.
        if SDK_CONTRACT in getattr(type(value), "__dict__", {}):
            for entrypoint in _local_entrypoint_of(getattr(value, SDK_CONTRACT, None)):
                if entrypoint not in found:
                    found.append(entrypoint)
    return found


def discover_local_entrypoints(items: list[str]) -> list[str]:
    """Entrypoints named by ``CONTROL_PLANE_SKILLS_LOCAL_PACKAGES``.

    ``module:function`` names one entrypoint. A bare module or package name
    offers every module in it that declares a ``CONTRACT`` with a ``local``
    implementation — the convention of skill packages (the selfdev
    integration, for one). Only what imports is declared: announcing an
    entrypoint the executor cannot load would fail every call it receives.
    """
    found: list[str] = []
    for item in items:
        if ":" in item:
            candidates = [item]
        else:
            try:
                module = importlib.import_module(item)
            except Exception as exc:
                logger.warning("skill package %s does not import: %s", item, exc)
                continue
            candidates = _local_contract_entrypoints(module)
            for info in pkgutil.walk_packages(getattr(module, "__path__", []), f"{item}."):
                try:
                    candidates += _local_contract_entrypoints(importlib.import_module(info.name))
                except Exception as exc:
                    logger.warning("skill module %s does not import: %s", info.name, exc)
        for entrypoint in candidates:
            try:
                _resolve_entrypoint(entrypoint)
            except Exception as exc:
                logger.warning("skill entrypoint %s is not loadable: %s", entrypoint, exc)
                continue
            if entrypoint not in found:
                found.append(entrypoint)
    return found


def _failure_of(exc: BaseException) -> SkillFailure:
    """A skill's exception as the failure to report (``code``/``retryable``)."""
    if isinstance(exc, SkillFailure):
        return exc
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    return SkillFailure(
        code if isinstance(code, str) and _CODE_RE.match(code) else "skill_error",
        f"{type(exc).__name__}: {exc}",
        retryable=getattr(exc, "retryable", False) is True,
        details=details if isinstance(details, dict) else None,
    )


def _invoke_local(function: Any, inputs: dict[str, Any], meta: dict[str, Any]) -> SkillOutcome:
    """Synchronous side of a ``local`` call (a worker thread or the child process)."""
    sdk_invoke = getattr(function, SDK_INVOKE, None)
    if callable(sdk_invoke):
        answer = sdk_invoke(inputs, meta)
        if not isinstance(answer, dict) or "outputs" not in answer:
            raise SkillFailure(
                "output_contract_violation", f"{SDK_INVOKE} must answer {{outputs, cost}}"
            )
        return SkillOutcome(answer["outputs"], _cost_of(answer.get("cost")))
    if inspect.iscoroutinefunction(function):
        return SkillOutcome(asyncio.run(function(inputs)))
    return SkillOutcome(function(inputs))


def _settings_of(value: Any) -> dict[str, Any] | None:
    """The claim's ``settings``; a core older than CP-ADR-0081 has none."""
    return value if isinstance(value, dict) else None


def _call_meta(call: SkillCall) -> dict[str, Any]:
    return {
        "invocationId": call.invocation_id,
        "idempotencyKey": call.idempotency_key,
        "timeoutSeconds": call.timeout_seconds,
        "skill": call.skill,
        "settings": call.settings,
    }


def _skill_env_reserved(name: str) -> bool:
    upper = name.upper()
    return upper in _SKILL_ENV_FIXED or upper.startswith(_SKILL_ENV_RESERVED)


def parse_skill_env(value: Any, *, where: str) -> dict[str, str]:
    """``{NAME: value}`` of non-secret settings for the skills; ``ValueError`` names the misfit.

    The names are upper-case environment variables; one that looks like a
    credential (``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``, ``*_API_KEY``…) or
    belongs to the host (``CONTROL_PLANE_*``, ``GIT_*``, ``PATH``, a proxy or a
    CA bundle…) is refused.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object of NAME: value")
    if len(value) > SKILL_ENV_MAX_ITEMS:
        raise ValueError(f"{where} allows at most {SKILL_ENV_MAX_ITEMS} variables")
    out: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not _SKILL_ENV_NAME.fullmatch(name):
            raise ValueError(f"{where}: {str(name)[:100]!r} is not a variable name (A-Z, 0-9, _)")
        if _SKILL_ENV_SECRET.search(name):
            raise ValueError(
                f"{where}.{name}: a secret is not a parameter; give it as a secret of the node"
            )
        if _skill_env_reserved(name):
            raise ValueError(f"{where}.{name} belongs to the host")
        if not isinstance(item, str) or len(item) > SKILL_ENV_MAX_VALUE:
            raise ValueError(f"{where}.{name} must be a string of at most {SKILL_ENV_MAX_VALUE}")
        out[name] = item
    return out


def _run_in_child(
    entrypoint: str,
    inputs: dict[str, Any],
    connection: Any,
    meta: dict[str, Any] | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    """Child-process side of an isolated ``local`` call: one result, then exit."""
    try:
        # The skills' settings: the child of a forkserver does not see what
        # the daemon's environment got after the server started.
        os.environ.update(environment or {})
        function = _resolve_entrypoint(entrypoint)
        outcome = _invoke_local(function, inputs, meta or {})
        message: tuple[str, Any] = ("ok", (outcome.output, outcome.cost))
    except BaseException as exc:
        failure = _failure_of(exc)
        message = ("error", (failure.code, failure.message, failure.retryable, failure.details))
    try:
        connection.send(message)
    except Exception as exc:
        # An unpicklable result (not JSON-shaped anyway) must not hang the parent.
        connection.send(("error", ("output_contract_violation", str(exc)[:500], False, None)))
    finally:
        connection.close()


def _process_context() -> Any:
    # forkserver: never fork the multi-threaded daemon itself, and pay the
    # interpreter start once rather than per call.
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")


class LocalProtocol:
    """``module:function``; only the declared entrypoints.

    ``isolation="process"`` (the default) runs each call in a child process
    and kills it when the call is abandoned — on timeout, on a lost lease, on
    cancellation — so a retry of the same invocation never overlaps with its
    predecessor. ``"thread"`` runs it in a worker thread of this process: a
    thread cannot be killed, so an abandoned call keeps running and only its
    result is dropped (ADR-0056 amendment M2.2, D.6).
    """

    POLL_SECONDS = 0.02

    def __init__(
        self,
        entrypoints: list[str],
        *,
        isolation: str = "process",
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if isolation not in ("process", "thread"):
            raise ValueError(f"unknown local isolation {isolation!r}")
        self.entrypoints = list(entrypoints)
        self.isolation = isolation
        # Settings of the skills (ENV_LOCAL_ENV): in the child's environment,
        # or in this process's for ``thread``, which has no other.
        self.environment = dict(environment or {})
        if isolation == "thread":
            os.environ.update(self.environment)

    def admits(self, implementation: dict[str, Any]) -> bool:
        return implementation.get("entrypoint") in self.entrypoints

    async def call(self, call: SkillCall) -> Any:
        entrypoint = call.implementation.get("entrypoint") or ""
        if entrypoint not in self.entrypoints:
            raise SkillFailure(
                "entrypoint_not_installed",
                f"{entrypoint} is not installed on this executor",
                retryable=True,
            )
        meta = _call_meta(call)
        if self.isolation == "process":
            return await self._call_in_process(entrypoint, call.inputs, meta)
        function = _resolve_entrypoint(entrypoint)
        try:
            if inspect.iscoroutinefunction(function) and not callable(
                getattr(function, SDK_INVOKE, None)
            ):
                return SkillOutcome(await function(call.inputs))
            # A thread cannot be killed: on timeout the call is abandoned, not
            # stopped, and whatever it returns later is dropped.
            return await asyncio.to_thread(_invoke_local, function, call.inputs, meta)
        except SkillFailure:
            raise
        except Exception as exc:
            raise _failure_of(exc) from exc

    async def _call_in_process(
        self, entrypoint: str, inputs: dict[str, Any], meta: dict[str, Any]
    ) -> Any:
        context = _process_context()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_run_in_child,
            args=(entrypoint, inputs, sender, meta, self.environment),
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            # Polled rather than awaited in a thread, so that cancellation
            # (timeout, lost lease) lands here and the finally kills the child.
            while not receiver.poll():
                if not process.is_alive() and not receiver.poll():
                    raise SkillFailure(
                        "skill_crashed",
                        f"{entrypoint} exited with code {process.exitcode} without a result",
                        retryable=True,
                    )
                await asyncio.sleep(self.POLL_SECONDS)
            try:
                kind, payload = receiver.recv()
            except EOFError as exc:
                raise SkillFailure(
                    "skill_crashed", f"{entrypoint} exited without a result", retryable=True
                ) from exc
        finally:
            if process.is_alive():
                process.kill()
            process.join(5)
            receiver.close()
        if kind == "ok":
            output, cost = payload
            return SkillOutcome(output, cost)
        code, message, retryable, details = payload
        raise SkillFailure(code, message, retryable=retryable, details=details)


# --- remote endpoints (http, mcp) ------------------------------------------------


_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an http(s) URL: lower-case, default port omitted.

    ``ValueError`` for anything else, including a URL with credentials in it.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise ValueError(f"not a URL: {url!r}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"not an http(s) URL: {url!r}")
    if parsed.userinfo:
        raise ValueError("a URL with credentials is not an origin")
    host = parsed.host.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port if parsed.port not in (None, _DEFAULT_PORTS[parsed.scheme]) else None
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


def parse_origins(items: list[str], *, variable: str) -> list[str]:
    """Configured origins; a path, a query or credentials are a configuration error."""
    origins: list[str] = []
    for item in items:
        origin = origin_of(item)
        parsed = httpx.URL(item)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError(f"{variable}: {item!r} is not an origin (scheme://host[:port])")
        if origin not in origins:
            origins.append(origin)
    return origins


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: str) -> bool:
    """False for loopback, private, link-local, reserved, multicast and the like."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class EndpointPolicy:
    """Where a remote protocol may connect and which tokens it may carry.

    ``origins`` — the only ``scheme://host[:port]`` it reaches; empty means
    the protocol reaches nothing over the network. ``private_hosts`` — hosts
    (names or literal addresses) trusted to resolve to non-public addresses;
    any other host resolving to one is refused, whatever the allow-list says.
    ``audiences`` — IAM audiences a token may be asked for.
    """

    origins: frozenset[str] = frozenset()
    audiences: frozenset[str] = frozenset()
    private_hosts: frozenset[str] = frozenset()
    resolve: Resolver = resolve_host

    def admits_endpoint(self, endpoint: str) -> bool:
        try:
            return origin_of(endpoint) in self.origins
        except ValueError:
            return False

    def admits_audience(self, audience: Any) -> bool:
        return not audience or (
            str(audience) in self.audiences and str(audience) not in RESERVED_AUDIENCES
        )

    def admits(self, implementation: dict[str, Any]) -> bool:
        return self.admits_endpoint(str(implementation.get("endpoint") or "")) and (
            self.admits_audience((implementation.get("auth") or {}).get("audience"))
        )

    def check_endpoint(self, endpoint: str) -> None:
        if not self.admits_endpoint(endpoint):
            # Another executor may be configured for it: retryable.
            raise SkillFailure(
                "endpoint_not_allowed",
                "the endpoint's origin is not in this executor's allow-list",
                retryable=True,
            )

    async def address_for(self, host: str, port: int) -> str:
        """The checked address to connect to for ``host``.

        Every address the name resolves to must be public, unless the host is
        trusted with private ones: a name on the allow-list that resolves to
        loopback or to the metadata service is refused all the same.
        """
        host = host.lower()
        if _ip_literal(host):
            addresses = [host]
        else:
            try:
                addresses = await self.resolve(host, port)
            except OSError as exc:
                raise SkillFailure(
                    "transport_error", f"{host} does not resolve: {exc}", retryable=True
                ) from exc
            if not addresses:
                raise SkillFailure("transport_error", f"{host} does not resolve", retryable=True)
        if host not in self.private_hosts and not all(map(is_public_address, addresses)):
            raise SkillFailure(
                "endpoint_address_forbidden",
                f"{host} resolves to a non-public address",
                retryable=False,
            )
        return addresses[0]


async def _pin(policy: EndpointPolicy, request: Any) -> None:
    """Check the request's origin and point it at the address checked for it.

    The connection goes to that address, not to a second resolution of the
    name (which could answer differently); the ``Host`` header and, for
    https, SNI and certificate verification keep the name.
    """
    url = request.url
    policy.check_endpoint(str(url))
    host = url.host
    address = await policy.address_for(host, url.port or _DEFAULT_PORTS[url.scheme])
    if address != host.lower():
        if url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = url.copy_with(host=address)


class GuardedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that sends only what ``EndpointPolicy`` allows."""

    def __init__(
        self, policy: EndpointPolicy, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.policy = policy
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await _pin(self.policy, request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _auth_headers(
    token_source: TokenSource | None,
    policy: EndpointPolicy,
    implementation: dict[str, Any],
) -> dict[str, str]:
    """``Authorization`` for the implementation's audience, when it names one.

    A token goes over https only and only for an allowed audience; a contract
    that asks for one anyway fails instead of being called without it.
    """
    auth = implementation.get("auth") or {}
    audience = auth.get("audience")
    if not audience:
        return {}
    endpoint = str(implementation.get("endpoint") or "")
    if not endpoint.startswith("https://"):
        raise SkillFailure(
            "insecure_endpoint",
            "the contract asks for a token, which is sent over https only",
            retryable=False,
        )
    if not policy.admits_audience(audience):
        raise SkillFailure(
            "audience_not_allowed",
            f"this executor does not issue tokens for audience {audience}",
            retryable=True,
        )
    scopes = tuple(str(scope) for scope in auth.get("scopes") or ())
    token = await token_source(str(audience), scopes) if token_source else None
    if not token:
        # Not the skill's fault: another executor with an IAM identity may
        # run it, so the attempt is retryable.
        raise SkillFailure(
            "executor_auth_unavailable",
            f"this executor cannot obtain a token for audience {audience}",
            retryable=True,
        )
    return {"Authorization": f"Bearer {token}"}


# --- http ---------------------------------------------------------------------


class HttpProtocol:
    """``POST endpoint`` with the invocation; the 2xx body is the outputs.

    ``transport`` is the one beneath the guard (tests pass a mock); every
    request goes through ``GuardedTransport`` whatever is given.
    """

    def __init__(
        self,
        token_source: TokenSource | None = None,
        *,
        policy: EndpointPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_source = token_source
        self.policy = policy or EndpointPolicy()
        self._transport = transport

    def admits(self, implementation: dict[str, Any]) -> bool:
        return self.policy.admits(implementation)

    async def call(self, call: SkillCall) -> Any:
        endpoint = str(call.implementation.get("endpoint") or "")
        self.policy.check_endpoint(endpoint)
        headers = await _auth_headers(self._token_source, self.policy, call.implementation)
        body = {
            "invocationId": call.invocation_id,
            "idempotencyKey": call.idempotency_key,
            "settings": call.settings,
            "inputs": call.inputs,
        }
        # No redirects: following one would carry the token to another host.
        # No proxies from the environment: they would bypass the guard.
        async with httpx.AsyncClient(
            transport=GuardedTransport(self.policy, self._transport),
            timeout=call.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            try:
                response = await http.post(endpoint, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise SkillFailure("timeout", f"{endpoint} timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise SkillFailure(
                    "transport_error", f"{type(exc).__name__}: {exc}", retryable=True
                ) from exc
        status = response.status_code
        if 200 <= status < 300:
            try:
                output = response.json()
            except ValueError as exc:
                raise SkillFailure(
                    "output_contract_violation", "the 2xx response is not JSON"
                ) from exc
            cost = None
            with contextlib.suppress(ValueError):
                cost = _cost_of(json.loads(response.headers.get(COST_HEADER) or "null"))
            return SkillOutcome(output, cost)
        # An excerpt of the body and no headers: error details are readable by
        # whoever reads the invocation, the service's response is not theirs.
        text = response.text
        details = {
            "status": status,
            "body": redact_local_paths(text[:MAX_BODY_EXCERPT]),
            "bodyTruncated": len(text) > MAX_BODY_EXCERPT,
        }
        message = f"{endpoint} answered {status}"
        envelope = None
        with contextlib.suppress(ValueError):
            envelope = _error_envelope(response.json())
        if envelope is not None and status >= 400:
            # The service's own code and retryability; the excerpt stays ours.
            raise SkillFailure(
                envelope.code,
                f"{message}: {envelope.message}",
                retryable=envelope.retryable,
                details=details,
            )
        if 400 <= status < 500:
            raise SkillFailure(f"http_{status}", message, retryable=False, details=details)
        if status >= 500:
            raise SkillFailure(f"http_{status}", message, retryable=True, details=details)
        raise SkillFailure("unexpected_status", message, retryable=False, details=details)


# --- mcp ----------------------------------------------------------------------


def _texts(result: Any) -> list[str]:
    return [str(getattr(block, "text", "")) for block in result.content if block.type == "text"]


def _mcp_outputs(result: Any) -> Any:
    """Structured content if the server sent it, else one JSON text block."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = _texts(result)
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            pass
    raise SkillFailure(
        "output_contract_violation",
        "the tool returned neither structured content nor one JSON text block",
    )


def _guarded_mcp_client(policy: EndpointPolicy, headers: dict[str, str]) -> Any:
    """The httpx2 client the MCP SDK talks through, behind the same guard.

    Unlike ``create_mcp_http_client`` it does not follow redirects (the token
    would go along) and ignores proxy settings of the environment.
    """
    import httpx2

    class _Guarded(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx2.AsyncHTTPTransport()

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            await _pin(policy, request)
            return await self.inner.handle_async_request(request)

        async def aclose(self) -> None:
            await self.inner.aclose()

    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=False,
        trust_env=False,
        transport=_Guarded(),
    )


@contextlib.asynccontextmanager
async def _streamable(endpoint: str, http_client: Any) -> Any:
    """Streamable HTTP over ``http_client``, which the SDK leaves to its owner to close."""
    from mcp.client.streamable_http import streamable_http_client

    async with http_client, streamable_http_client(endpoint, http_client=http_client) as streams:
        yield streams


class McpProtocol:
    """``tools/call`` on an MCP server named by the implementation.

    ``servers`` maps ``stdio:<name>`` endpoints to ``{command, args, env}``;
    ``policy`` limits ``http(s)://`` endpoints as for ``http``. ``connect``
    overrides how an implementation becomes a client target (a URL, a
    transport or an in-process server) — tests pass a real in-process server
    through it.
    """

    def __init__(
        self,
        servers: dict[str, dict[str, Any]] | None = None,
        *,
        token_source: TokenSource | None = None,
        policy: EndpointPolicy | None = None,
        connect: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.servers = servers or {}
        self._token_source = token_source
        self.policy = policy or EndpointPolicy()
        self._connect = connect

    @property
    def endpoints(self) -> list[str]:
        """What this executor reaches: allowed origins and its stdio servers."""
        return sorted(self.policy.origins) + [f"stdio:{name}" for name in sorted(self.servers)]

    def admits(self, implementation: dict[str, Any]) -> bool:
        endpoint = str(implementation.get("endpoint") or "")
        if endpoint.startswith("stdio:"):
            return endpoint.removeprefix("stdio:") in self.servers
        return self.policy.admits(implementation)

    async def _target(self, implementation: dict[str, Any]) -> Any:
        endpoint = str(implementation.get("endpoint") or "")
        if endpoint.startswith(("http://", "https://")):
            self.policy.check_endpoint(endpoint)
            headers = await _auth_headers(self._token_source, self.policy, implementation)
            if self._connect is not None:
                return self._connect(implementation)
            # The address is checked up front for a clear failure; the guarded
            # transport checks it again for every request it actually sends.
            url = httpx.URL(endpoint)
            await self.policy.address_for(url.host, url.port or _DEFAULT_PORTS[url.scheme])
            return _streamable(endpoint, _guarded_mcp_client(self.policy, headers))
        if self._connect is not None:
            return self._connect(implementation)
        if endpoint.startswith("stdio:"):
            server = self.servers.get(endpoint.removeprefix("stdio:"))
            if server is None:
                raise SkillFailure(
                    "mcp_server_unknown",
                    f"this executor has no MCP server {endpoint}",
                    retryable=True,
                )
            from mcp.client.stdio import StdioServerParameters, stdio_client

            return stdio_client(StdioServerParameters(**server))
        raise SkillFailure(
            "mcp_endpoint_invalid",
            "mcp endpoint must be an http(s) URL or stdio:<name>",
        )

    async def call(self, call: SkillCall) -> Any:
        from mcp.client import Client
        from mcp.shared.exceptions import MCPError

        tool = str(call.implementation.get("entrypoint") or "")
        target = await self._target(call.implementation)
        try:
            async with Client(target) as client:
                result = await client.call_tool(
                    tool,
                    call.inputs,
                    read_timeout_seconds=call.timeout_seconds,
                    meta=cast(
                        "RequestParamsMeta",
                        {
                            MCP_INVOCATION_META: call.invocation_id,
                            MCP_IDEMPOTENCY_META: call.idempotency_key,
                            MCP_SETTINGS_META: call.settings,
                        },
                    ),
                )
        except MCPError as exc:
            # A protocol error (unknown tool, invalid params) repeats on retry.
            raise SkillFailure("mcp_error", f"{tool}: {exc}", retryable=False) from exc
        except (OSError, httpx.HTTPError) as exc:
            raise SkillFailure(
                "transport_error", f"{type(exc).__name__}: {exc}", retryable=True
            ) from exc
        if result.is_error:
            texts = _texts(result)
            envelope = None
            if len(texts) == 1:
                with contextlib.suppress(ValueError):
                    envelope = _error_envelope(json.loads(texts[0]))
            if envelope is not None:
                raise envelope
            raise SkillFailure("tool_error", f"{tool}: {' '.join(texts)}", retryable=False)
        meta = getattr(result, "meta", None)
        cost = _cost_of(meta.get(MCP_COST_META)) if isinstance(meta, dict) else None
        return SkillOutcome(_mcp_outputs(result), cost)


# --- executor -----------------------------------------------------------------


def output_errors(schema: dict[str, Any], output: Any) -> list[dict[str, str]]:
    """The same check the core makes at ``:complete`` (JSON Schema 2020-12)."""
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    try:
        found = sorted(validator.iter_errors(output), key=lambda e: list(e.absolute_path))
    except Exception as exc:
        return [{"path": "/", "message": f"schema could not be evaluated: {exc}"[:300]}]
    return [
        {
            "path": "/" + "/".join(str(part) for part in error.absolute_path),
            "message": error.message[:500],
        }
        for error in found
    ][:20]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


@dataclass
class _Lease:
    invocation_id: str
    fencing_token: int
    expires_at: datetime | None
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = ""


class SkillExecutor:
    """Claims, runs and reports skill invocations for one daemon."""

    def __init__(
        self,
        client: ControlPlaneClient,
        handlers: dict[str, ProtocolHandler],
        *,
        local_entrypoints: list[str] | None = None,
        heartbeat_interval: float = 30.0,
        poll_interval: float = 1.0,
        concurrency: int = 0,
    ) -> None:
        unknown = sorted(set(handlers) - set(PROTOCOLS))
        if unknown:
            raise ValueError(f"unknown skill protocols: {unknown}")
        self.client = client
        self.handlers = dict(handlers)
        self.local_entrypoints = list(local_entrypoints or [])
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        # Invocations taken from the queue at once by the daemon's own skill
        # workers; 0 — only when the daemon has no Work, one at a time.
        self.concurrency = concurrency
        self._skills: dict[str, dict[str, Any]] = {}

    @property
    def protocols(self) -> list[str]:
        return sorted(self.handlers)

    @property
    def capabilities(self) -> list[str]:
        """Session capabilities announcing what this executor runs (ADR-0021)."""
        return [f"skills.protocol.{name}" for name in self.protocols]

    @property
    def http_origins(self) -> list[str]:
        handler = self.handlers.get("http")
        return sorted(handler.policy.origins) if isinstance(handler, HttpProtocol) else []

    @property
    def mcp_endpoints(self) -> list[str]:
        handler = self.handlers.get("mcp")
        return handler.endpoints if isinstance(handler, McpProtocol) else []

    @property
    def audiences(self) -> list[str]:
        found: set[str] = set()
        for handler in self.handlers.values():
            if isinstance(handler, HttpProtocol | McpProtocol):
                found |= handler.policy.audiences - RESERVED_AUDIENCES
        return sorted(found)

    def can_execute(self, skill: dict[str, Any]) -> bool:
        implementation = (skill.get("contract") or {}).get("implementation") or {}
        handler = self.handlers.get(str(implementation.get("protocol")))
        if handler is None:
            return False
        if implementation.get("protocol") == "local":
            return implementation.get("entrypoint") in self.local_entrypoints
        return handler.admits(implementation)

    async def describe(self, ref: str) -> dict[str, Any]:
        """A pinned skill version never changes, so it is read once."""
        if ref not in self._skills:
            self._skills[ref] = await self.client.describe_skill(ref)
        return self._skills[ref]

    # -- claim → execute → report ----------------------------------------------

    async def claim(
        self, session_id: str | None, *, invocation_id: str | None = None
    ) -> dict[str, Any] | None:
        # The core hands out only what this executor admits: the same
        # allow-lists its protocols enforce again before they connect.
        return await self.client.claim_skill_invocation(
            protocols=self.protocols,
            local_entrypoints=self.local_entrypoints,
            http_origins=self.http_origins,
            mcp_endpoints=self.mcp_endpoints,
            audiences=self.audiences,
            session_id=session_id,
            invocation_id=invocation_id,
        )

    async def run_once(self, session_id: str | None) -> bool:
        """Take one invocation from the queue and see it through. True if any."""
        claimed = await self.claim(session_id)
        if claimed is None:
            return False
        await self.execute_claimed(claimed, session_id)
        return True

    async def execute_claimed(self, claimed: dict[str, Any], session_id: str | None) -> str:
        """Run a claimed invocation and report it; returns what happened.

        ``succeeded``/``failed`` — reported (the core's verdict may still
        differ); ``lease_lost`` — nothing was reported.
        """
        invocation, skill = claimed["invocation"], claimed["skill"]
        contract = skill.get("contract") or {}
        lease = _Lease(
            invocation_id=str(invocation["id"]),
            fencing_token=int(invocation["fencingToken"]),
            expires_at=_parse_time(invocation.get("leaseExpiresAt")),
        )
        ref = f"{skill.get('name')}@{skill.get('version')}"
        call = SkillCall(
            invocation_id=lease.invocation_id,
            idempotency_key=invocation.get("idempotencyKey"),
            inputs=dict(invocation.get("inputs") or {}),
            skill=ref,
            implementation=dict(contract.get("implementation") or {}),
            timeout_seconds=float(contract.get("timeoutSeconds") or 60),
            settings=_settings_of(claimed.get("settings")),
        )
        heartbeat = asyncio.create_task(self._keep_lease(lease, session_id))
        work = asyncio.create_task(self._attempt(call, contract))
        lost = asyncio.create_task(lease.lost.wait())
        try:
            await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
            if lease.lost.is_set():
                work.cancel()
                logger.warning("lease on %s lost (%s); result dropped", ref, lease.reason)
                return "lease_lost"
            outcome = work.result()
        finally:
            for task in (heartbeat, lost, work):
                task.cancel()
            await asyncio.gather(heartbeat, lost, work, return_exceptions=True)
        if not self._lease_alive(lease):
            logger.warning("lease on %s expired during the call; result dropped", ref)
            return "lease_lost"
        return await self._report(lease, session_id, ref, outcome)

    async def _attempt(self, call: SkillCall, contract: dict[str, Any]) -> Any:
        """The SkillOutcome (outputs and cost) or the SkillFailure to report."""
        handler = self.handlers.get(str(call.implementation.get("protocol")))
        if handler is None:
            return SkillFailure(
                "protocol_not_supported", "this executor does not run the protocol", retryable=True
            )
        try:
            output = await asyncio.wait_for(handler.call(call), call.timeout_seconds)
        except TimeoutError:
            return SkillFailure(
                "timeout",
                f"{call.skill} did not finish within {call.timeout_seconds:g}s",
                retryable=True,
            )
        except SkillFailure as failure:
            return failure
        except Exception as exc:
            return SkillFailure("executor_error", f"{type(exc).__name__}: {exc}", retryable=True)
        cost = None
        if isinstance(output, SkillOutcome):
            output, cost = output.output, output.cost
        if not isinstance(output, dict):
            return SkillFailure(
                "output_contract_violation",
                f"outputs must be a JSON object, got {type(output).__name__}",
            )
        errors = output_errors(contract.get("outputs") or {}, output)
        if errors:
            return SkillFailure(
                "output_contract_violation",
                "outputs do not match the skill's output schema",
                details={"errors": errors},
            )
        return SkillOutcome(output, cost)

    async def _report(self, lease: _Lease, session_id: str | None, ref: str, outcome: Any) -> str:
        try:
            if isinstance(outcome, SkillFailure):
                error = outcome.as_error()
                await self.client.fail_skill_invocation(
                    lease.invocation_id,
                    fencing_token=lease.fencing_token,
                    code=error["code"],
                    message=error["message"],
                    retryable=error["retryable"],
                    details=error["details"],
                    session_id=session_id,
                )
                logger.info("skill %s failed: %s", ref, outcome.code)
                return "failed"
            extra: dict[str, Any] = {"cost": outcome.cost} if outcome.cost else {}
            await self.client.complete_skill_invocation(
                lease.invocation_id,
                fencing_token=lease.fencing_token,
                output=outcome.output,
                session_id=session_id,
                **extra,
            )
            logger.info("skill %s succeeded", ref)
            return "succeeded"
        except ControlPlaneError as exc:
            if is_transient(exc) or exc.code not in LEASE_LOST_CODES:
                raise
            logger.warning("lease on %s lost before the report (%s)", ref, exc.code)
            return "lease_lost"

    def _lease_alive(self, lease: _Lease) -> bool:
        if lease.lost.is_set():
            return False
        return lease.expires_at is None or datetime.now(UTC) < lease.expires_at

    async def _keep_lease(self, lease: _Lease, session_id: str | None) -> None:
        """Heartbeat until cancelled; set ``lost`` once the lease is not ours.

        A transport failure or a 502/503/504 of a restarting core is not a
        verdict — the lease may still be valid — but the deadline is: past
        ``leaseExpiresAt`` without a successful heartbeat the lease is treated
        as gone. The call itself is bounded by that deadline: the client keeps
        retrying an unreachable core for its whole retry window, which may be
        longer than what is left of the lease.
        """
        while True:
            interval = self.heartbeat_interval
            if lease.expires_at is not None:
                remaining = (lease.expires_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    lease.reason = "lease_expired"
                    lease.lost.set()
                    return
                interval = max(0.05, min(interval, remaining / 3))
            await asyncio.sleep(interval)
            deadline: float | None = None
            if lease.expires_at is not None:
                deadline = max(0.0, (lease.expires_at - datetime.now(UTC)).total_seconds())
            try:
                beat = await asyncio.wait_for(
                    self.client.heartbeat_skill_invocation(
                        lease.invocation_id,
                        fencing_token=lease.fencing_token,
                        session_id=session_id,
                    ),
                    deadline,
                )
            except TimeoutError:
                logger.info("heartbeat of %s outlived the lease", lease.invocation_id)
                continue
            except ControlPlaneError as exc:
                if is_transient(exc):
                    logger.info("heartbeat of %s failed: %s", lease.invocation_id, exc)
                    continue
                if exc.code in LEASE_LOST_CODES:
                    lease.reason = exc.code
                    lease.lost.set()
                    return
                logger.info("heartbeat of %s rejected: %s", lease.invocation_id, exc.code)
                continue
            except Exception as exc:
                # Never let the heartbeat die quietly: the deadline check above
                # must keep running for as long as the call does.
                logger.warning("heartbeat of %s errored: %s", lease.invocation_id, exc)
                continue
            lease.expires_at = _parse_time(beat.get("leaseExpiresAt")) or lease.expires_at

    # -- Work executed by a skill (ADR-0056 §3) ----------------------------------

    async def execute_work(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        execution: dict[str, Any],
        session_id: str | None,
        *,
        alive: Callable[[], bool] = lambda: True,
    ) -> dict[str, Any]:
        """Make the run's one invocation and see it to a terminal state.

        The call is keyed by the run: a retry after a lost response is the same
        call, not a second one. The daemon claims that very call; if another
        executor got it first, the daemon waits for its result. Waiting is
        bounded by the contract (attempts * (timeout + backoff) plus lease
        margins) and by ``alive`` — the task claim this run holds. A call that
        outlives the bound is cancelled, so it cannot act for a failed run.
        """
        ref = f"{execution['skill']}@{execution['version']}"
        skill = await self.describe(ref)
        contract = skill.get("contract") or {}
        invocation = await self.client.invoke_skill(
            ref,
            inputs=map_inputs(execution.get("inputs"), task),
            idempotency_key=f"execution:{run['id']}",
            task_id=str(task["id"]),
            run_id=str(run["id"]),
        )
        retry = contract.get("retryPolicy") or {}
        attempts = int(retry.get("maxAttempts") or 1)
        per_attempt = float(contract.get("timeoutSeconds") or 60) * 2 + 30
        budget = attempts * (per_attempt + float(retry.get("backoffSeconds") or 0)) + 30
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while invocation.get("status") not in TERMINAL:
            if not alive() or loop.time() > deadline:
                with contextlib.suppress(ControlPlaneError):
                    invocation = await self.client.cancel_skill_invocation(
                        str(invocation["id"]),
                        reason="run_ended" if not alive() else "execution_deadline",
                    )
                return invocation
            if invocation.get("status") == "pending" and self.can_execute(skill):
                claimed = await self.claim(session_id, invocation_id=str(invocation["id"]))
                if claimed is not None:
                    await self.execute_claimed(claimed, session_id)
            invocation = await self.client.get_skill_invocation(str(invocation["id"]))
            if invocation.get("status") not in TERMINAL:
                await asyncio.sleep(self.poll_interval)
        return invocation


# --- inputs of an execution-typed task -------------------------------------------

_PATH_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d{1,6})\]")
_MISSING = object()


def resolve_path(document: Any, path: str) -> Any:
    """Evaluate ``$.a.b[0]`` — the grammar ``task_types.execution`` allows.

    A missing step yields ``_MISSING``; the caller leaves that input out and
    the contract's input schema decides whether it was required.
    """
    if not path.startswith("$"):
        raise ValueError(f"invalid input path {path!r}")
    position, value = 1, document
    while position < len(path):
        match = _PATH_SEGMENT.match(path, position)
        if match is None:
            raise ValueError(f"invalid input path {path!r}")
        name, index = match.groups()
        if name is not None:
            value = value.get(name, _MISSING) if isinstance(value, dict) else _MISSING
        else:
            number = int(index)
            value = value[number] if isinstance(value, list) and number < len(value) else _MISSING
        if value is _MISSING:
            return _MISSING
        position = match.end()
    return value


def map_inputs(mapping: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Inputs of the run's invocation from the task (``execution.inputs``)."""
    if mapping is None:
        mapping = "$.customFields"
    if isinstance(mapping, str):
        value = resolve_path(task, mapping)
        return dict(value) if isinstance(value, dict) else {}
    inputs: dict[str, Any] = {}
    for name, path in mapping.items():
        value = resolve_path(task, path)
        if value is not _MISSING:
            inputs[name] = value
    return inputs


# --- configuration -----------------------------------------------------------------


def _split(value: str) -> list[str]:
    return [item for item in re.split(r"[\s,]+", value) if item]


def iam_token_source(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenSource | None:
    """Tokens for skill services from the daemon's own IAM identity.

    The same PAT the daemon uses for the Control Plane, exchanged for the
    audience the skill names (``implementation.auth.audience``) with the
    scopes it names (``implementation.auth.scopes``) — never with the core's
    ``CONTROL_PLANE_IAM_SCOPES``. IAM grants no more than the PAT's scope
    ceiling. One credential per (audience, scopes). Without an IAM
    configuration there is no source, and calls needing one fail retryably on
    this executor.
    """
    from control_plane_client.iam import (
        DEFAULT_AUDIENCE,
        ENV_IAM_AUDIENCE,
        ENV_IAM_SCOPES,
        ENV_IAM_URL,
        iam_credential_from_environment,
    )

    values = os.environ if environ is None else environ
    if not values.get(ENV_IAM_URL, "").strip():
        return None
    # The daemon's own audience is its authority over the core: never a skill's.
    withheld = RESERVED_AUDIENCES | {values.get(ENV_IAM_AUDIENCE, "").strip() or DEFAULT_AUDIENCE}
    credentials: dict[tuple[str, tuple[str, ...]], Any] = {}

    async def token(audience: str, scopes: tuple[str, ...] = ()) -> str | None:
        if audience in withheld:
            return None
        key = (audience, tuple(sorted(set(scopes))))
        if key not in credentials:
            scoped = {**values, ENV_IAM_AUDIENCE: audience, ENV_IAM_SCOPES: " ".join(key[1])}
            credentials[key] = iam_credential_from_environment(scoped, transport=transport)
        credential = credentials[key]
        return await credential.token() if credential is not None else None

    return token


def _audiences(values: Mapping[str, str]) -> frozenset[str]:
    """``CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES``; the core's own is never one."""
    from control_plane_client.iam import DEFAULT_AUDIENCE, ENV_IAM_AUDIENCE

    audiences = frozenset(_split(values.get(ENV_AUDIENCES, "")))
    own = values.get(ENV_IAM_AUDIENCE, "").strip() or DEFAULT_AUDIENCE
    reserved = sorted(audiences & (RESERVED_AUDIENCES | {own}))
    if reserved:
        raise ValueError(
            f"{ENV_AUDIENCES}: {reserved} would hand the executor's own authority to a skill"
        )
    return audiences


def executor_from_environment(
    client: ControlPlaneClient,
    environ: Mapping[str, str] | None = None,
    *,
    heartbeat_interval: float = 30.0,
) -> SkillExecutor | None:
    """Build the executor from ``CONTROL_PLANE_SKILLS_*``; None when not asked for.

    ``CONTROL_PLANE_SKILLS_PROTOCOLS`` — which protocols to run (default:
    ``local`` when local packages are given, otherwise none);
    ``CONTROL_PLANE_SKILLS_LOCAL_PACKAGES`` — entrypoints or packages;
    ``CONTROL_PLANE_SKILLS_LOCAL_ISOLATION`` — ``process`` (default) or
    ``thread``;
    ``CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS`` /
    ``CONTROL_PLANE_SKILLS_MCP_ALLOWED_ORIGINS`` — ``scheme://host[:port]``
    the protocol may reach; without any, ``http`` is not run and ``mcp`` runs
    only its ``stdio`` servers;
    ``CONTROL_PLANE_SKILLS_PRIVATE_HOSTS`` — hosts of those origins trusted to
    resolve to non-public addresses (in-cluster services);
    ``CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES`` — IAM audiences a skill may get
    a token of (never ``control-plane``, ``iam`` or the daemon's own);
    ``CONTROL_PLANE_SKILLS_MCP_SERVERS`` — JSON ``{name: {command, args,
    env}}`` for ``stdio:<name>`` endpoints;
    ``CONTROL_PLANE_SKILLS_CONCURRENCY`` — invocations run at once alongside
    Work (default 1; 0 — only while there is no Work);
    ``CONTROL_PLANE_SKILLS_LOCAL_ENV`` — JSON ``{NAME: value}``, non-secret
    settings set in the environment of each ``local`` call.
    """
    values = os.environ if environ is None else environ
    packages = _split(values.get(ENV_LOCAL_PACKAGES, ""))
    protocols = _split(values.get(ENV_PROTOCOLS, "")) or (["local"] if packages else [])
    if not protocols:
        return None
    unknown = sorted(set(protocols) - set(PROTOCOLS))
    if unknown:
        raise ValueError(f"{ENV_PROTOCOLS}: unknown protocols {unknown}")
    try:
        concurrency = int(values.get(ENV_CONCURRENCY, "") or 1)
    except ValueError as exc:
        raise ValueError(f"{ENV_CONCURRENCY} must be an integer") from exc
    if concurrency < 0:
        raise ValueError(f"{ENV_CONCURRENCY} must not be negative")
    audiences = _audiences(values)
    private_hosts = frozenset(
        host.lower().strip("[]") for host in _split(values.get(ENV_PRIVATE_HOSTS, ""))
    )
    tokens = iam_token_source(values)
    handlers: dict[str, ProtocolHandler] = {}
    entrypoints: list[str] = []
    if "local" in protocols:
        entrypoints = discover_local_entrypoints(packages)
        isolation = values.get(ENV_LOCAL_ISOLATION, "").strip() or "process"
        try:
            settings = json.loads(values.get(ENV_LOCAL_ENV, "") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{ENV_LOCAL_ENV} must be a JSON object") from exc
        handlers["local"] = LocalProtocol(
            entrypoints,
            isolation=isolation,
            environment=parse_skill_env(settings, where=ENV_LOCAL_ENV),
        )
    if "http" in protocols:
        origins = parse_origins(_split(values.get(ENV_HTTP_ORIGINS, "")), variable=ENV_HTTP_ORIGINS)
        if origins:
            policy = EndpointPolicy(frozenset(origins), audiences, private_hosts)
            handlers["http"] = HttpProtocol(tokens, policy=policy)
        else:
            logger.warning("skill protocol http is not run: %s is empty", ENV_HTTP_ORIGINS)
    if "mcp" in protocols:
        servers = json.loads(values.get(ENV_MCP_SERVERS, "") or "{}")
        if not isinstance(servers, dict):
            raise ValueError(f"{ENV_MCP_SERVERS} must be a JSON object")
        origins = parse_origins(_split(values.get(ENV_MCP_ORIGINS, "")), variable=ENV_MCP_ORIGINS)
        if origins or servers:
            policy = EndpointPolicy(frozenset(origins), audiences, private_hosts)
            handlers["mcp"] = McpProtocol(servers, token_source=tokens, policy=policy)
        else:
            logger.warning(
                "skill protocol mcp is not run: neither %s nor %s is set",
                ENV_MCP_ORIGINS,
                ENV_MCP_SERVERS,
            )
    if not handlers:
        return None
    executor = SkillExecutor(
        client,
        handlers,
        local_entrypoints=entrypoints,
        heartbeat_interval=heartbeat_interval,
        concurrency=concurrency,
    )
    logger.info(
        "skill executor: protocols %s, local entrypoints %s, http %s, mcp %s, audiences %s",
        executor.protocols,
        entrypoints,
        executor.http_origins,
        executor.mcp_endpoints,
        executor.audiences,
    )
    return executor


__all__ = [
    "EndpointPolicy",
    "GuardedTransport",
    "HttpProtocol",
    "LocalProtocol",
    "McpProtocol",
    "SkillCall",
    "SkillExecutor",
    "SkillFailure",
    "discover_local_entrypoints",
    "executor_from_environment",
    "is_public_address",
    "map_inputs",
    "origin_of",
    "output_errors",
    "parse_skill_env",
    "resolve_path",
]
