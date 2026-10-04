"""The core's secret store client against a real OpenBao (CP-ADR-0079 §1).

Runs when ``CP_TEST_OPENBAO_URL`` and ``CP_TEST_OPENBAO_TOKEN`` (a token that
may configure the instance: the root token of a test container) are set;
the part of the ``oauthapp`` plugin runs when its mount ``oauth2/`` exists. The instance
should be the image of the installation (superproject, I006) with the version
it pins.

The test sets the instance up the way bootstrap does for the core — ``kv/``
as ``kv-v2`` with ``max_versions=1``, a ``jwt`` role under the paths of the
core's policy — but with a key it signs itself instead of the IAM: what is
checked is the client, not the IAM.
"""

import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.infrastructure.secret_store import (
    SecretStore,
    SecretStoreConflict,
    SecretStoreRejected,
    SecretStoreUnavailable,
)

OPENBAO_URL = os.environ.get("CP_TEST_OPENBAO_URL", "")
OPENBAO_TOKEN = os.environ.get("CP_TEST_OPENBAO_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (OPENBAO_URL and OPENBAO_TOKEN),
    reason="CP_TEST_OPENBAO_URL / CP_TEST_OPENBAO_TOKEN not set (OpenBao required)",
)

ROLE = "cp-test-control-plane"
AUDIENCE = "openbao"

# The paths of the core's policy (CP-ADR-0079 §1, ``control-plane.hcl``).
POLICY = """
path "oauth2/servers/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "oauth2/creds/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/data/tenants/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/metadata/tenants/*" { capabilities = ["list", "delete"] }
path "kv/data/platform/oauth-apps/*" { capabilities = ["create", "read", "update", "delete"] }
path "kv/metadata/platform/oauth-apps/*" { capabilities = ["list", "delete"] }
"""


async def _admin(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.request(
        method, f"{OPENBAO_URL}/v1/{path}", json=body, headers={"X-Vault-Token": OPENBAO_TOKEN}
    )


async def _set_up(client: httpx.AsyncClient, public_pem: str) -> None:
    mounts = (await _admin(client, "GET", "sys/mounts")).json()
    mounts = mounts.get("data", mounts)
    if "kv/" not in mounts:
        created = await _admin(
            client, "POST", "sys/mounts/kv", {"type": "kv", "options": {"version": "2"}}
        )
        assert created.status_code in (200, 204), created.text
    config = await _admin(client, "POST", "kv/config", {"max_versions": 1})
    assert config.status_code in (200, 204), config.text

    auths = (await _admin(client, "GET", "sys/auth")).json()
    auths = auths.get("data", auths)
    if "jwt/" not in auths:
        enabled = await _admin(client, "POST", "sys/auth/jwt", {"type": "jwt"})
        assert enabled.status_code in (200, 204), enabled.text
    configured = await _admin(
        client, "POST", "auth/jwt/config", {"jwt_validation_pubkeys": [public_pem]}
    )
    assert configured.status_code in (200, 204), configured.text
    policy = await _admin(client, "PUT", f"sys/policies/acl/{ROLE}", {"policy": POLICY})
    assert policy.status_code in (200, 204), policy.text
    role = await _admin(
        client,
        "POST",
        f"auth/jwt/role/{ROLE}",
        {
            "role_type": "jwt",
            "bound_audiences": [AUDIENCE],
            "user_claim": "sub",
            "token_policies": [ROLE],
            "token_ttl": "10m",
        },
    )
    assert role.status_code in (200, 204), role.text


@pytest.fixture
async def store() -> AsyncIterator[tuple[SecretStore, httpx.AsyncClient]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    async with httpx.AsyncClient(timeout=10) as admin:
        await _set_up(admin, public_pem)

        async def iam_token() -> str:
            now = int(time.time())
            return jwt.encode(
                {"sub": "control-plane", "aud": AUDIENCE, "iat": now, "exp": now + 300},
                key,
                algorithm="RS256",
            )

        secret_store = SecretStore(OPENBAO_URL, iam_token, role=ROLE)
        try:
            yield secret_store, admin
        finally:
            await secret_store.aclose()


async def test_kv_holds_one_version_under_cas_and_is_deleted_whole(
    store: tuple[SecretStore, httpx.AsyncClient],
) -> None:
    secret_store, admin = store
    path = f"tenants/{uuid.uuid4()}/connections/crm"

    assert await secret_store.kv_read(path) is None
    assert await secret_store.kv_current_version(path) == 0
    assert await secret_store.kv_write(path, {"access_token": "first"}, cas=0) == 1
    with pytest.raises(SecretStoreConflict):
        await secret_store.kv_write(path, {"access_token": "lost"}, cas=0)
    assert await secret_store.kv_write(path, {"access_token": "second"}, cas=1) == 2
    document = await secret_store.kv_read(path)
    assert document is not None
    assert (document.data, document.version) == ({"access_token": "second"}, 2)

    # max_versions=1: the first version is gone, not merely marked deleted.
    old = await _admin(admin, "GET", f"kv/data/{path}?version=1")
    assert old.status_code == 404 or (old.json().get("data") or {}).get("data") is None

    await secret_store.kv_delete_all(path)
    await secret_store.kv_delete_all(path)
    assert await secret_store.kv_read(path) is None
    assert (await _admin(admin, "GET", f"kv/metadata/{path}")).status_code == 404


async def test_a_path_outside_the_policy_is_refused(
    store: tuple[SecretStore, httpx.AsyncClient],
) -> None:
    secret_store, _admin_client = store
    with pytest.raises(SecretStoreUnavailable) as caught:
        await secret_store.kv_read("platform/other/crm")
    assert caught.value.reason == "permission_denied"


async def test_the_oauth2_server_and_a_refused_exchange(
    store: tuple[SecretStore, httpx.AsyncClient],
) -> None:
    secret_store, admin = store
    mounts = (await _admin(admin, "GET", "sys/mounts")).json()
    if "oauth2/" not in mounts.get("data", mounts):
        pytest.skip("no oauth2/ mount (the oauthapp plugin) in this instance")
    name = f"tenants/{uuid.uuid4()}/connections/crm"
    # The server of one authorization attempt (CP-ADR-0079 §1, §6).
    server = f"{name}/{uuid.uuid4()}"

    await secret_store.oauth_put_server(
        server,
        client_id="client",
        client_secret="secret",
        auth_code_url="https://auth.invalid/authorize",
        token_url="https://auth.invalid/token",
        auth_style="in_params",
    )
    # The provider cannot be reached: the store refuses, and the refusal is a
    # typed error of the client whatever status the plugin chose for it.
    with pytest.raises((SecretStoreRejected, SecretStoreUnavailable)) as caught:
        await secret_store.oauth_exchange_code(
            name, server=server, code="c0de", redirect_url="https://cp.invalid/callback"
        )
    assert "c0de" not in str(caught.value)
    await secret_store.oauth_delete_creds(name)
    await secret_store.oauth_delete_server(server)
    await secret_store.oauth_delete_server(server)
