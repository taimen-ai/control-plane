"""The core's client of the secret store (CP-ADR-0079 §1).

Against :class:`tests.fake_openbao.FakeOpenBao`: the login with the core's IAM
token and role, the store token kept until its lease ends, one new login on a
refused token, ``cas`` writes, and failures that are typed codes and never
carry a value.
"""

import asyncio

import httpx
import pytest

from control_plane.config import Settings
from control_plane.infrastructure.secret_store import (
    SecretStore,
    SecretStoreConflict,
    SecretStoreRejected,
    SecretStoreUnavailable,
    build_secret_store,
)
from tests.fake_openbao import BASE_URL, IAM_JWT, FakeOpenBao

SECRET = "value-" + "V" * 30


class _Iam:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.forgotten = 0
        self.fail = fail

    async def __call__(self) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"exchange failed for client_secret={SECRET}")
        return IAM_JWT

    def forget(self) -> None:
        self.forgotten += 1


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _store(bao: FakeOpenBao, iam: _Iam | None = None, clock: _Clock | None = None) -> SecretStore:
    iam = iam or _Iam()
    return SecretStore(
        BASE_URL,
        iam,
        forget_token=iam.forget,
        client=bao.client(),
        clock=clock or _Clock(),
    )


async def test_the_store_token_is_kept_until_its_lease_ends() -> None:
    bao = FakeOpenBao()
    iam = _Iam()
    clock = _Clock()
    store = _store(bao, iam, clock)

    await store.kv_write("tenants/t/connections/crm", {"access_token": SECRET})
    await store.kv_read("tenants/t/connections/crm")
    assert (bao.logins, iam.calls) == (1, 1)
    login = [body for method, path, body in bao.requests if path == "auth/jwt/login"]
    assert login == [{"role": "control-plane", "jwt": IAM_JWT}]

    clock.now += 3600 - 29  # inside the margin before the lease ends
    await store.kv_read("tenants/t/connections/crm")
    assert bao.logins == 2


async def test_concurrent_calls_log_in_once() -> None:
    bao = FakeOpenBao()
    store = _store(bao)
    await asyncio.gather(*(store.kv_read(f"tenants/t/connections/c{i}") for i in range(10)))
    assert bao.logins == 1


async def test_a_refused_store_token_logs_in_again_once() -> None:
    bao = FakeOpenBao()
    store = _store(bao)
    await store.kv_read("tenants/t/connections/crm")
    bao.revoke_tokens()
    assert await store.kv_read("tenants/t/connections/crm") is None
    assert bao.logins == 2

    # A path outside the core's policy stays refused after the second login.
    with pytest.raises(SecretStoreUnavailable) as caught:
        await store.kv_read("tenants")
    assert caught.value.reason == "permission_denied"


async def test_a_refused_login_forgets_the_iam_token() -> None:
    bao = FakeOpenBao()
    bao.jwt = "another"
    iam = _Iam()
    store = _store(bao, iam)
    with pytest.raises(SecretStoreUnavailable) as caught:
        await store.kv_read("tenants/t/connections/crm")
    assert caught.value.reason == "login_failed"
    assert iam.forgotten == 1


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda bao: setattr(bao, "sealed", True), "sealed"),
        (lambda bao: bao.fail("GET", "kv/data/", 500), "store_error"),
        (lambda bao: bao.fail("POST", "auth/jwt/login", 502), "login_failed"),
    ],
)
async def test_store_failures_are_typed_codes(setup: object, reason: str) -> None:
    bao = FakeOpenBao()
    setup(bao)  # type: ignore[operator]
    store = _store(bao)
    with pytest.raises(SecretStoreUnavailable) as caught:
        await store.kv_read("tenants/t/connections/crm")
    assert caught.value.reason == reason


async def test_an_unreachable_store_and_a_failed_iam_exchange_carry_no_value() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    store = SecretStore(
        BASE_URL, _Iam(), client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(SecretStoreUnavailable) as caught:
        await store.kv_write("tenants/t/connections/crm", {"access_token": SECRET})
    assert caught.value.reason == "unreachable"
    assert SECRET not in str(caught.value)

    failing = _store(FakeOpenBao(), _Iam(fail=True))
    with pytest.raises(SecretStoreUnavailable) as caught:
        await failing.kv_read("tenants/t/connections/crm")
    assert caught.value.reason == "iam_token_unavailable"
    assert SECRET not in str(caught.value)


async def test_kv_keeps_one_version_and_checks_cas() -> None:
    bao = FakeOpenBao()
    store = _store(bao)
    path = "platform/oauth-apps/crm"
    assert await store.kv_current_version(path) == 0
    assert await store.kv_write(path, {"client_secret": "a"}, cas=0) == 1
    document = await store.kv_read(path)
    assert document is not None and (document.data, document.version) == (
        {"client_secret": "a"},
        1,
    )
    with pytest.raises(SecretStoreConflict):
        await store.kv_write(path, {"client_secret": "b"}, cas=0)
    assert await store.kv_write(path, {"client_secret": "b"}, cas=1) == 2

    await store.kv_delete_all(path)
    await store.kv_delete_all(path)  # idempotent
    assert await store.kv_read(path) is None
    assert bao.paths("DELETE") == [f"kv/metadata/{path}", f"kv/metadata/{path}"]


async def test_kv_list_names_the_keys_of_a_folder() -> None:
    bao = FakeOpenBao()
    store = _store(bao)
    assert await store.kv_list("tenants/t/agents") == []
    for path in ("tenants/t/agents/a/x", "tenants/t/agents/a/d/y", "tenants/t/agents/b/x"):
        await store.kv_write(path, {"value": SECRET})
    assert await store.kv_list("tenants/t/agents") == ["a/", "b/"]
    assert await store.kv_list("tenants/t/agents/a") == ["d/", "x"]
    assert bao.paths("LIST")[-1] == "kv/metadata/tenants/t/agents/a/"
    # Listing deletes nothing.
    assert len([path for path in bao.kv if path.startswith("tenants/t/")]) == 3
    bao.fail("LIST", "kv/metadata/tenants/")
    with pytest.raises(SecretStoreUnavailable):
        await store.kv_list("tenants/t/agents")


async def test_a_refused_exchange_keeps_only_the_store_messages() -> None:
    bao = FakeOpenBao()
    store = _store(bao)
    name = "tenants/t/connections/crm"
    with pytest.raises(SecretStoreRejected) as caught:
        await store.oauth_exchange_code(name, server=name, code="c", redirect_url="https://cp/cb")
    assert caught.value.reason == "exchange_failed"
    assert caught.value.status == 400
    assert caught.value.messages == ["server not found"]

    await store.oauth_delete_creds(name)
    await store.oauth_delete_server(name)


def test_the_store_is_built_only_when_configured(settings_base: Settings) -> None:
    assert build_secret_store(settings_base) is None
    with pytest.raises(ValueError, match="CP_IAM_CLIENT_ID"):
        build_secret_store(settings_base.model_copy(update={"secret_store_url": BASE_URL}))
    store = build_secret_store(
        settings_base.model_copy(
            update={
                "secret_store_url": BASE_URL,
                "iam_client_id": "control-plane",
                "iam_client_secret": "s",
            }
        )
    )
    assert isinstance(store, SecretStore)
    asyncio.run(store.aclose())


@pytest.fixture
def settings_base() -> Settings:
    return Settings(database_url="postgresql+psycopg://x@localhost/x", bootstrap_token="t" * 32)
