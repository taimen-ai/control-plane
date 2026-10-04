"""The core's client of the secret store (OpenBao, CP-ADR-0079 §1).

The core logs in with its own IAM service token: ``ServiceTokenProvider``
(audience ``openbao``) → ``POST auth/jwt/login`` under the core's role
(``CP_SECRET_STORE_ROLE``). The store token lives in process memory until its
lease ends. The role's policy is the superproject's ``control-plane.hcl``; the
client goes no further than it: ``kv/data`` (read, write with ``cas``),
``kv/metadata`` (delete — every version at once), the ``oauth2/`` plugin
(servers and creds) and the ACL policies and ``jwt`` roles of agents
(``cp-agent-*``, ``agent-*``, §9).

Failures are explicit and typed, never a quiet fallback:

- :class:`SecretStoreUnavailable` — not configured, unreachable, sealed, a
  failed login, a refused token or an answer the client does not understand.
  The caller answers ``503 secret_store_unavailable`` and changes nothing;
- :class:`SecretStoreConflict` — a ``cas`` write lost to another writer;
- :class:`SecretStoreRejected` — the store refused a request on its merits
  (the plugin could not exchange a code). ``messages`` are the store's texts:
  a caller that keeps them redacts them first.

No value that passes through here is logged, and no exception carries one:
the store's answer is reduced to its ``errors`` list, and the request body is
never quoted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from platform_auth import ServiceCredentials, ServiceTokenProvider

from control_plane.config import Settings

# The store token is renewed this long before its lease ends.
_LEASE_MARGIN_SECONDS = 30.0


class SecretStoreError(Exception):
    """A failure of the secret store; ``reason`` is a code, never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SecretStoreUnavailable(SecretStoreError):
    """The store cannot serve the request now; retrying later makes sense."""


class SecretStoreConflict(SecretStoreError):
    """A check-and-set write found another version than the one it read."""


class SecretStoreRejected(SecretStoreError):
    """The store refused the request on its merits (e.g. a failed code exchange)."""

    def __init__(self, reason: str, status: int, messages: list[str]) -> None:
        super().__init__(reason)
        self.status = status
        self.messages = messages


@dataclass(frozen=True)
class KvDocument:
    """A ``kv-v2`` document and its version (``metadata.version``)."""

    data: dict[str, Any]
    version: int


TokenSource = Callable[[], Awaitable[str]]


def _errors_of(response: httpx.Response) -> list[str]:
    try:
        body = response.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [str(item) for item in errors]


class SecretStore:
    """HTTP client of OpenBao for the core's role."""

    def __init__(
        self,
        base_url: str,
        token_source: TokenSource,
        *,
        role: str = "control-plane",
        forget_token: Callable[[], None] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_source = token_source
        self._forget_iam_token = forget_token
        self._role = role
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._clock = clock
        self._token = ""
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()
        self._closables: list[Any] = []

    def close_with(self, closable: Any) -> None:
        """Close ``closable`` (e.g. the IAM token provider) together with the store."""
        self._closables.append(closable)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        for closable in self._closables:
            await closable.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # --- login -------------------------------------------------------------------

    async def _login(self) -> str:
        async with self._login_lock:
            if self._token and self._clock() < self._token_expires:
                return self._token
            try:
                jwt = await self._token_source()
            except Exception as exc:
                # The IAM exchange failed; its reason is not retold (it may
                # echo the request with the client secret).
                raise SecretStoreUnavailable("iam_token_unavailable") from exc
            try:
                response = await self._http().post(
                    f"{self._base_url}/v1/auth/jwt/login",
                    json={"role": self._role, "jwt": jwt},
                )
            except httpx.HTTPError as exc:
                raise SecretStoreUnavailable("unreachable") from exc
            if response.status_code != 200:
                if response.status_code in (400, 403) and self._forget_iam_token is not None:
                    # A stale IAM token is the likely cause; the next login
                    # takes a fresh one.
                    self._forget_iam_token()
                raise SecretStoreUnavailable(
                    "sealed" if response.status_code == 503 else "login_failed"
                )
            try:
                auth = response.json()["auth"]
                token = str(auth["client_token"])
                lease = float(auth.get("lease_duration") or 0)
            except (ValueError, KeyError, TypeError) as exc:
                raise SecretStoreUnavailable("unexpected_response") from exc
            if not token:
                raise SecretStoreUnavailable("unexpected_response")
            self._token = token
            self._token_expires = self._clock() + max(lease - _LEASE_MARGIN_SECONDS, 0.0)
            return token

    def _forget(self) -> None:
        self._token = ""
        self._token_expires = 0.0

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        """One call under the core's token; a refused token logs in again once."""
        for attempt in (1, 2):
            token = await self._login()
            try:
                response = await self._http().request(
                    method,
                    f"{self._base_url}/v1/{path}",
                    json=json,
                    headers={"X-Vault-Token": token},
                )
            except httpx.HTTPError as exc:
                raise SecretStoreUnavailable("unreachable") from exc
            if response.status_code == 403 and attempt == 1:
                self._forget()
                continue
            if response.status_code == 403:
                raise SecretStoreUnavailable("permission_denied")
            if response.status_code >= 500:
                raise SecretStoreUnavailable(
                    "sealed" if response.status_code == 503 else "store_error"
                )
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    # --- kv-v2 ---------------------------------------------------------------------

    async def kv_read(self, path: str) -> KvDocument | None:
        """``GET kv/data/<path>``; ``None`` when there is no live document."""
        response = await self._request("GET", f"kv/data/{path}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretStoreUnavailable("unexpected_response")
        try:
            body = response.json()["data"]
            data = body["data"]
            version = int(body["metadata"]["version"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretStoreUnavailable("unexpected_response") from exc
        if not isinstance(data, dict):
            return None
        return KvDocument(data=data, version=version)

    async def kv_current_version(self, path: str) -> int:
        """The version a ``cas`` write compares to: ``0`` when there is no document.

        A document whose latest version was deleted (``DELETE kv/data``, which
        the core never uses) still has a version, and ``cas = 0`` would never
        succeed on it; the store names that version in its ``404``.
        """
        response = await self._request("GET", f"kv/data/{path}")
        if response.status_code not in (200, 404):
            raise SecretStoreUnavailable("unexpected_response")
        try:
            metadata = response.json()["data"]["metadata"]
            return int(metadata["version"])
        except (ValueError, KeyError, TypeError):
            return 0

    async def kv_write(self, path: str, data: dict[str, Any], *, cas: int | None = None) -> int:
        """``POST kv/data/<path>``; with ``cas`` only over that version (0 — none)."""
        body: dict[str, Any] = {"data": data}
        if cas is not None:
            body["options"] = {"cas": cas}
        response = await self._request("POST", f"kv/data/{path}", json=body)
        if response.status_code == 400 and any(
            "check-and-set" in message for message in _errors_of(response)
        ):
            raise SecretStoreConflict("cas_mismatch")
        if response.status_code not in (200, 204):
            raise SecretStoreUnavailable("unexpected_response")
        try:
            return int(response.json()["data"]["version"])
        except (ValueError, KeyError, TypeError):
            return 0

    async def kv_list(self, path: str) -> list[str]:
        """``LIST kv/metadata/<path>/``: the keys under it (a folder ends in ``/``), or ``[]``."""
        return await self._list(f"kv/metadata/{path}/")

    async def kv_delete_all(self, path: str) -> None:
        """``DELETE kv/metadata/<path>``: every version and the metadata; idempotent."""
        response = await self._request("DELETE", f"kv/metadata/{path}")
        if response.status_code not in (200, 204, 404):
            raise SecretStoreUnavailable("unexpected_response")

    # --- oauth2 plugin -------------------------------------------------------------

    async def oauth_put_server(
        self,
        name: str,
        *,
        client_id: str,
        client_secret: str,
        auth_code_url: str,
        token_url: str,
        auth_style: str,
    ) -> None:
        """``PUT oauth2/servers/<name>``: a ``custom`` provider with its exchange address."""
        response = await self._request(
            "PUT",
            f"oauth2/servers/{name}",
            json={
                "provider": "custom",
                "client_id": client_id,
                "client_secret": client_secret,
                "provider_options": {
                    "auth_code_url": auth_code_url,
                    "token_url": token_url,
                    "auth_style": auth_style,
                },
            },
        )
        if response.status_code not in (200, 204):
            raise SecretStoreRejected("server_rejected", response.status_code, _errors_of(response))

    async def oauth_exchange_code(
        self, name: str, *, server: str, code: str, redirect_url: str
    ) -> None:
        """``PUT oauth2/creds/<name>``: the plugin exchanges the code and keeps the tokens."""
        response = await self._request(
            "PUT",
            f"oauth2/creds/{name}",
            json={
                "server": server,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_url": redirect_url,
            },
        )
        if response.status_code not in (200, 204):
            raise SecretStoreRejected("exchange_failed", response.status_code, _errors_of(response))

    async def oauth_delete_creds(self, name: str) -> None:
        response = await self._request("DELETE", f"oauth2/creds/{name}")
        if response.status_code not in (200, 204, 404):
            raise SecretStoreUnavailable("unexpected_response")

    async def oauth_delete_server(self, name: str) -> None:
        response = await self._request("DELETE", f"oauth2/servers/{name}")
        if response.status_code not in (200, 204, 404):
            raise SecretStoreUnavailable("unexpected_response")

    # --- agents' policies and roles (CP-ADR-0079 §9) -----------------------------------

    async def _list(self, path: str) -> list[str]:
        """``LIST <path>`` (as ``GET ?list=true``): the keys, none when the store has none."""
        response = await self._request("GET", f"{path}?list=true")
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise SecretStoreUnavailable("unexpected_response")
        try:
            keys = response.json()["data"]["keys"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretStoreUnavailable("unexpected_response") from exc
        if not isinstance(keys, list):
            raise SecretStoreUnavailable("unexpected_response")
        return [str(key) for key in keys]

    async def _read_data(self, path: str) -> dict[str, Any] | None:
        response = await self._request("GET", path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretStoreUnavailable("unexpected_response")
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretStoreUnavailable("unexpected_response") from exc
        if not isinstance(data, dict):
            raise SecretStoreUnavailable("unexpected_response")
        return data

    async def _write(self, path: str, body: dict[str, Any]) -> None:
        response = await self._request("POST", path, json=body)
        if response.status_code not in (200, 204):
            raise SecretStoreUnavailable("unexpected_response")

    async def _delete(self, path: str) -> None:
        response = await self._request("DELETE", path)
        if response.status_code not in (200, 204, 404):
            raise SecretStoreUnavailable("unexpected_response")

    async def policy_list(self) -> list[str]:
        """Names of the ACL policies (``LIST sys/policies/acl``)."""
        return await self._list("sys/policies/acl")

    async def policy_read(self, name: str) -> str | None:
        """The text of the ACL policy ``name``; ``None`` when there is none."""
        data = await self._read_data(f"sys/policies/acl/{name}")
        if data is None:
            return None
        policy = data.get("policy")
        if not isinstance(policy, str):
            raise SecretStoreUnavailable("unexpected_response")
        return policy

    async def policy_write(self, name: str, policy: str) -> None:
        await self._write(f"sys/policies/acl/{name}", {"policy": policy})

    async def policy_delete(self, name: str) -> None:
        """Idempotent: a missing policy is no error."""
        await self._delete(f"sys/policies/acl/{name}")

    async def jwt_role_list(self) -> list[str]:
        """Names of the roles of the ``jwt`` method (``LIST auth/jwt/role``)."""
        return await self._list("auth/jwt/role")

    async def jwt_role_read(self, name: str) -> dict[str, Any] | None:
        """The role ``name`` of the ``jwt`` method as the store answers it; ``None`` when absent."""
        return await self._read_data(f"auth/jwt/role/{name}")

    async def jwt_role_write(self, name: str, role: dict[str, Any]) -> None:
        await self._write(f"auth/jwt/role/{name}", role)

    async def jwt_role_delete(self, name: str) -> None:
        """Idempotent: a missing role is no error."""
        await self._delete(f"auth/jwt/role/{name}")


def build_secret_store(settings: Settings) -> SecretStore | None:
    """``None`` without ``CP_SECRET_STORE_URL``: the routes that need it answer 503.

    A URL without the core's IAM client fails start-up: the store would be
    configured and never reachable, and that is a mistake to see at once.
    """
    if not settings.secret_store_url:
        return None
    if not settings.iam_client_id or not settings.iam_client_secret:
        raise ValueError("CP_SECRET_STORE_URL requires CP_IAM_CLIENT_ID and CP_IAM_CLIENT_SECRET")
    provider = ServiceTokenProvider(
        settings.iam_base_url,
        ServiceCredentials(
            client_id=settings.iam_client_id,
            client_secret=settings.iam_client_secret,
            audience=settings.secret_store_audience,
            scopes=(),
        ),
        request_timeout_seconds=settings.iam_request_timeout_seconds,
    )
    store = SecretStore(
        settings.secret_store_url,
        provider,
        role=settings.secret_store_role,
        forget_token=provider.forget,
        timeout_seconds=settings.secret_store_timeout_seconds,
    )
    store.close_with(provider)
    return store
