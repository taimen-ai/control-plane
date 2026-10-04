"""An in-memory OpenBao for the core's secret store client (CP-ADR-0079 §1).

It answers the API the core uses, in the shapes of OpenBao and of the
``oauthapp`` plugin, and nothing else:

- ``POST auth/jwt/login`` — a known JWT and the core's role give a token;
- the core's policy (``control-plane.hcl``): a path outside it is ``403``;
- ``kv/`` is ``kv-v2`` with ``max_versions=1``: a write replaces the only
  version, ``options.cas`` is checked, ``DELETE kv/metadata`` removes the
  document with its versions, ``LIST kv/metadata`` names the keys of a folder;
- ``oauth2/servers`` keeps a server's config; ``PUT oauth2/creds`` exchanges
  the code at the server's ``token_url`` through :class:`FakeTokenServer`, the
  way the plugin does it, and keeps the tokens;
- ``sys/policies/acl`` and ``auth/jwt/role`` keep the agents' policies and
  roles (``LIST`` is ``GET ?list=true``); an agent logs in with its role and a
  JWT the test registered (``agent_jwt``), the role's bounds are checked, and
  every request of the agent's token is checked against the text of its
  policies *at that moment*, the way OpenBao applies a policy changed after
  the login.

Every request is recorded (method, path, body) so a test sees what the core
sent; ``fail`` makes the next request to a path prefix answer a status.
"""

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

BASE_URL = "http://openbao.test"
IAM_JWT = "iam-service-jwt"

# The core's policy (``deploy/openbao/policies/control-plane.hcl``, CP-ADR-0079 §1).
_POLICY: tuple[tuple[str, frozenset[str]], ...] = (
    (r"oauth2/servers/tenants/.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
    (r"oauth2/creds/tenants/.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
    (r"kv/data/tenants/.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
    (r"kv/metadata/tenants/.+", frozenset({"LIST", "DELETE"})),
    (r"kv/data/platform/oauth-apps/.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
    (r"kv/metadata/platform/oauth-apps/.+", frozenset({"LIST", "DELETE"})),
    (r"sys/policies/acl", frozenset({"LIST"})),
    (r"sys/policies/acl/cp-agent-.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
    (r"auth/jwt/role", frozenset({"LIST"})),
    (r"auth/jwt/role/agent-.+", frozenset({"GET", "PUT", "POST", "DELETE"})),
)

# Fields OpenBao answers on a role besides those written, with its defaults.
_ROLE_DEFAULTS: dict[str, Any] = {
    "allowed_redirect_uris": [],
    "bound_claims_type": "string",
    "claim_mappings": None,
    "clock_skew_leeway": 0,
    "expiration_leeway": 0,
    "groups_claim": "",
    "not_before_leeway": 0,
    "token_bound_cidrs": [],
    "token_explicit_max_ttl": 0,
    "token_no_default_policy": False,
    "token_num_uses": 0,
    "token_period": 0,
    "token_type": "default",
    "verbose_oidc_logging": False,
}

_POLICY_RULE = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}')
_CAPABILITY_OF = {
    "GET": "read",
    "LIST": "list",
    "DELETE": "delete",
    "PUT": "update",
    "POST": "update",
}


def policy_allows(policy: str, method: str, path: str) -> bool:
    """Whether the HCL ``policy`` gives ``method`` on ``path`` (exact or ``…*`` prefix)."""
    wanted = _CAPABILITY_OF.get(method)
    for rule_path, capabilities in _POLICY_RULE.findall(policy):
        granted = {item.strip().strip('"') for item in capabilities.split(",")}
        matches = path.startswith(rule_path[:-1]) if rule_path.endswith("*") else path == rule_path
        if matches and wanted in granted:
            return True
    return False


@dataclass
class FakeTokenServer:
    """A provider's token endpoint: a code it issued is exchanged once.

    ``issue`` returns a code for a token URL. ``requests`` keeps what the
    plugin sent (URL, form, the client's credentials) for the assertions.
    """

    codes: dict[str, str] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    refuse_with: str | None = None

    def issue(self, token_url: str) -> str:
        code = "code-" + secrets.token_urlsafe(16)
        self.codes[code] = token_url
        return code

    def exchange(
        self,
        token_url: str,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_url: str,
        auth_style: str,
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "token_url": token_url,
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_url": redirect_url,
                "auth_style": auth_style,
            }
        )
        if self.refuse_with is not None:
            # Providers echo the request in their errors; the core must not keep it.
            raise ValueError(f"{self.refuse_with}: code {code} client_secret={client_secret}")
        if self.codes.pop(code, None) != token_url:
            raise ValueError(f"invalid_grant: code {code} is unknown or used")
        return {
            "access_token": "at-" + secrets.token_urlsafe(16),
            "refresh_token": "rt-" + secrets.token_urlsafe(16),
            "expires_in": 3600,
        }


@dataclass
class _Doc:
    data: dict[str, Any]
    version: int


class FakeOpenBao:
    def __init__(self, token_server: FakeTokenServer | None = None) -> None:
        self.token_server = token_server or FakeTokenServer()
        self.kv: dict[str, _Doc] = {}
        self.servers: dict[str, dict[str, Any]] = {}
        self.creds: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.tokens: set[str] = set()
        self.policies: dict[str, str] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        # JWT -> its claims, for agents' logins; token -> its policies.
        self.agent_jwts: dict[str, dict[str, Any]] = {}
        self.agent_tokens: dict[str, list[str]] = {}
        self.logins = 0
        self.sealed = False
        self.jwt = IAM_JWT
        self.role = "control-plane"
        self._failures: list[tuple[str, str, int]] = []
        # Called before a kv write is applied: a test races another writer here.
        self.before_kv_write: Any = None
        # Awaited before a code exchange: a test runs another request meanwhile.
        self.before_exchange: Any = None

    # --- test controls -------------------------------------------------------------

    def fail(self, method: str, prefix: str, status: int = 500) -> None:
        """The next ``method`` to a path starting with ``prefix`` answers ``status``."""
        self._failures.append((method, prefix, status))

    def revoke_tokens(self) -> None:
        self.tokens.clear()

    def agent_jwt(
        self,
        subject: str,
        tenant_id: str,
        *,
        principal_type: str = "agent",
        audience: str = "openbao",
    ) -> str:
        """A JWT of the IAM for an agent, as the fake accepts it at ``auth/jwt/login``."""
        jwt = "agent-jwt-" + secrets.token_urlsafe(12)
        self.agent_jwts[jwt] = {
            "sub": subject,
            "tenant_id": tenant_id,
            "principal_type": principal_type,
            "aud": [audience],
        }
        return jwt

    def writes(self, *prefixes: str) -> list[tuple[str, str]]:
        """Requests that change something (not ``GET`` or ``LIST``), under ``prefixes`` if any."""
        return [
            (method, path)
            for method, path, _ in self.requests
            if method not in ("GET", "LIST") and (not prefixes or path.startswith(prefixes))
        ]

    def paths(self, method: str | None = None) -> list[str]:
        return [path for m, path, _ in self.requests if method is None or m == method]

    def dump(self) -> str:
        """Everything the store keeps, as text: where a secret is expected to be."""
        return json.dumps(
            {
                "kv": {path: doc.data for path, doc in self.kv.items()},
                "servers": self.servers,
                "creds": self.creds,
            }
        )

    # --- transport ---------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path.removeprefix("/v1/")
        method = "LIST" if request.method == "GET" and "list=true" in url.query else request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        for index, (failing, prefix, status) in enumerate(self._failures):
            if failing == method and path.startswith(prefix):
                del self._failures[index]
                return _errors(status, "injected failure")
        if self.sealed:
            return _errors(503, "Vault is sealed")
        if path == "auth/jwt/login":
            return self._login(body or {})
        token = request.headers.get("X-Vault-Token")
        if token in self.agent_tokens:
            return self._agent_request(self.agent_tokens[token], method, path)
        if token not in self.tokens:
            return _errors(403, "permission denied")
        if not any(
            re.fullmatch(pattern, path) and method in methods for pattern, methods in _POLICY
        ):
            return _errors(403, "permission denied")
        if path.startswith("sys/policies/acl"):
            return self._policy(method, path.removeprefix("sys/policies/acl").lstrip("/"), body)
        if path.startswith("auth/jwt/role"):
            return self._role(method, path.removeprefix("auth/jwt/role").lstrip("/"), body)
        if path.startswith("kv/data/"):
            return self._kv_data(request.method, path.removeprefix("kv/data/"), body)
        if path.startswith("kv/metadata/") and method == "LIST":
            return self._kv_list(path.removeprefix("kv/metadata/"))
        if path.startswith("kv/metadata/"):
            self.kv.pop(path.removeprefix("kv/metadata/"), None)
            return httpx.Response(204)
        if path.startswith("oauth2/servers/"):
            return self._server(request.method, path.removeprefix("oauth2/servers/"), body)
        if path.startswith("oauth2/creds/"):
            if request.method in ("PUT", "POST") and self.before_exchange is not None:
                hook, self.before_exchange = self.before_exchange, None
                await hook()
            return self._creds(request.method, path.removeprefix("oauth2/creds/"), body)
        return _errors(404, "no handler for route")

    def _agent_request(self, policies: list[str], method: str, path: str) -> httpx.Response:
        """A request of an agent's token: its policies as they are now decide."""
        if not any(policy_allows(self.policies.get(name, ""), method, path) for name in policies):
            return _errors(403, "permission denied")
        if method == "GET" and path.startswith("kv/data/"):
            return self._kv_data("GET", path.removeprefix("kv/data/"), None)
        if method == "GET" and path.startswith("oauth2/creds/"):
            creds = self.creds.get(path.removeprefix("oauth2/creds/"))
            if creds is None:
                return _errors(404)
            return httpx.Response(
                200, json={"data": {"access_token": creds["access_token"], "expire_time": ""}}
            )
        return _errors(405, "unsupported operation")

    def _policy(self, method: str, name: str, body: dict[str, Any] | None) -> httpx.Response:
        if method == "LIST":
            return httpx.Response(
                200, json={"data": {"keys": ["default", *sorted(self.policies), "root"]}}
            )
        if method == "GET":
            if name not in self.policies:
                return _errors(404)
            text = self.policies[name]
            return httpx.Response(200, json={"name": name, "data": {"name": name, "policy": text}})
        if method in ("PUT", "POST"):
            assert body is not None and isinstance(body.get("policy"), str)
            self.policies[name] = body["policy"]
            return httpx.Response(204)
        if method == "DELETE":
            self.policies.pop(name, None)
            return httpx.Response(204)
        return _errors(405, "unsupported operation")

    def _role(self, method: str, name: str, body: dict[str, Any] | None) -> httpx.Response:
        if method == "LIST":
            if not self.roles:
                return _errors(404)
            return httpx.Response(200, json={"data": {"keys": sorted(self.roles)}})
        if method == "GET":
            if name not in self.roles:
                return _errors(404)
            return httpx.Response(200, json={"data": {**_ROLE_DEFAULTS, **self.roles[name]}})
        if method in ("PUT", "POST"):
            assert body is not None
            # An update keeps the fields it does not name, as the jwt method does.
            self.roles[name] = {**self.roles.get(name, {}), **body}
            return httpx.Response(204)
        if method == "DELETE":
            self.roles.pop(name, None)
            return httpx.Response(204)
        return _errors(405, "unsupported operation")

    def _agent_login(self, role_name: str, jwt: str) -> httpx.Response:
        role = self.roles.get(role_name)
        claims = self.agent_jwts.get(jwt)
        if role is None:
            return _errors(400, f'role "{role_name}" could not be found')
        if claims is None:
            return _errors(400, "error validating token: invalid signature")
        if not set(claims["aud"]) & set(role.get("bound_audiences") or []):
            return _errors(400, "error validating token: invalid audience (aud) claim")
        if role.get("bound_subject") and claims["sub"] != role["bound_subject"]:
            return _errors(400, "sub claim does not match bound subject")
        for claim, value in (role.get("bound_claims") or {}).items():
            if str(claims.get(claim)) != str(value):
                return _errors(400, f"claim {claim!r} does not match any associated bound claim")
        token = "s." + secrets.token_urlsafe(12)
        policies = list(role.get("token_policies") or [])
        if not role.get("token_no_default_policy"):
            policies.append("default")
        self.agent_tokens[token] = policies
        return httpx.Response(
            200,
            json={
                "auth": {
                    "client_token": token,
                    "policies": policies,
                    "lease_duration": role.get("token_ttl", 0),
                    "renewable": True,
                }
            },
        )

    def _login(self, body: dict[str, Any]) -> httpx.Response:
        role = str(body.get("role"))
        if role != self.role:
            return self._agent_login(role, str(body.get("jwt")))
        if body.get("jwt") != self.jwt:
            return _errors(400, "error validating token: invalid audience (aud) claim")
        self.logins += 1
        token = "s." + secrets.token_urlsafe(12)
        self.tokens.add(token)
        return httpx.Response(
            200,
            json={"auth": {"client_token": token, "lease_duration": 3600, "renewable": True}},
        )

    def _kv_data(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        doc = self.kv.get(path)
        if method == "GET":
            if doc is None:
                return _errors(404)
            return httpx.Response(
                200,
                json={"data": {"data": doc.data, "metadata": {"version": doc.version}}},
            )
        if method in ("POST", "PUT"):
            assert body is not None
            if self.before_kv_write is not None:
                hook, self.before_kv_write = self.before_kv_write, None
                hook()
                doc = self.kv.get(path)
            current = doc.version if doc is not None else 0
            cas = (body.get("options") or {}).get("cas")
            if cas is not None and cas != current:
                return _errors(
                    400,
                    "check-and-set parameter did not match the current version",
                )
            self.kv[path] = _Doc(dict(body["data"]), current + 1)
            return httpx.Response(200, json={"data": {"version": current + 1}})
        return _errors(405, "unsupported operation")

    def _kv_list(self, folder: str) -> httpx.Response:
        """The keys right under ``folder``, a subfolder with ``/``; none — ``404``."""
        prefix = folder.rstrip("/") + "/"
        keys = set()
        for path in self.kv:
            if path.startswith(prefix):
                head, slash, _ = path.removeprefix(prefix).partition("/")
                keys.add(head + slash)
        if not keys:
            return _errors(404)
        return httpx.Response(200, json={"data": {"keys": sorted(keys)}})

    def _server(self, method: str, name: str, body: dict[str, Any] | None) -> httpx.Response:
        if method == "DELETE":
            self.servers.pop(name, None)
            return httpx.Response(204)
        if method in ("PUT", "POST"):
            assert body is not None
            options = body.get("provider_options") or {}
            if body.get("provider") != "custom" or not options.get("token_url"):
                return _errors(400, "provider options are invalid")
            self.servers[name] = body
            return httpx.Response(204)
        return _errors(405, "unsupported operation")

    def _creds(self, method: str, name: str, body: dict[str, Any] | None) -> httpx.Response:
        if method == "DELETE":
            self.creds.pop(name, None)
            return httpx.Response(204)
        if method in ("PUT", "POST"):
            assert body is not None
            server = self.servers.get(str(body.get("server")))
            if server is None:
                return _errors(400, "server not found")
            options = server["provider_options"]
            try:
                tokens = self.token_server.exchange(
                    options["token_url"],
                    client_id=server["client_id"],
                    client_secret=server["client_secret"],
                    code=str(body.get("code")),
                    redirect_url=str(body.get("redirect_url")),
                    auth_style=options.get("auth_style", ""),
                )
            except ValueError as exc:
                return _errors(400, f"exchange failed: {exc}")
            self.creds[name] = {"server": body["server"], **tokens}
            return httpx.Response(204)
        return _errors(405, "unsupported operation")


def _errors(status: int, *messages: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": list(messages)})
