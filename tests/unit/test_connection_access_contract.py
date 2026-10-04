"""Access to connections in the contract (CP-ADR-0079 §5, §6, §7, §13, §14, §17).

What is pinned here: the routes and bodies of the OAuth application, the OAuth
flow and the key in OpenAPI, the public mark of the callback and its open
query, the payloads of the three events, the pure functions of the flow
(paths, the account, the state, the provider's address and texts), and the
access log that loses the callback's query however its path is written.
"""

import hashlib
import inspect
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError as PydanticValidationError
from uvicorn.protocols.utils import get_path_with_query_string

from control_plane.api.v1 import connections as connection_routes
from control_plane.api.v1.schemas import ConnectionTokenRequest, OAuthAppSetRequest
from control_plane.config import Settings, is_https_url
from control_plane.domain.connection_access import (
    MAX_ACCOUNT,
    account_problem,
    authorize_url,
    connection_store_name,
    kv_connection_ref,
    new_state,
    oauth_app_path,
    oauth_creds_ref,
    oauth_server_name,
    provider_text,
    state_hash,
    token_url,
)
from control_plane.domain.event_catalog import get_event_type
from control_plane.logging import AccessLogQueryFilter, redact_query
from tests.unit.test_connection_type_contract import PATHS, SCHEMAS, _body, _ref, _response
from tests.unit.test_connection_type_domain import SPEC, TOKEN_ONLY, spec_with

# --- routes and bodies (§17) --------------------------------------------------------------


def test_the_routes_of_access() -> None:
    oauth_app = PATHS["/api/v1/connection-types/{key}/oauth-app"]
    assert set(oauth_app) == {"put", "get"}
    assert _body(oauth_app["put"]) == "OAuthAppSetRequest"
    assert _ref(_response(oauth_app["put"], "200")) == "OAuthAppOut"
    assert {"409", "422", "503"} <= set(oauth_app["put"]["responses"])
    assert _ref(_response(oauth_app["get"], "200")) == "OAuthAppOut"
    assert "503" in oauth_app["get"]["responses"]

    authorize = PATHS["/api/v1/connections/{key}:authorize"]["post"]
    assert _ref(_response(authorize, "200")) == "ConnectionAuthorizeOut"
    assert {"409", "422", "503"} <= set(authorize["responses"])

    callback = PATHS["/api/v1/connections:callback"]
    assert set(callback) == {"get"}
    names = {p["name"] for p in callback["get"]["parameters"]}
    assert {"state", "code", "error", "error_description"} <= names
    assert set(callback["get"]["responses"]["303"]["headers"]) == {
        "Location",
        "Cache-Control",
        "Referrer-Policy",
    }
    assert "security" not in callback["get"] or callback["get"]["security"] == []

    token = PATHS["/api/v1/connections/{key}/token"]["put"]
    assert _body(token) == "ConnectionTokenRequest"
    assert _ref(_response(token, "200")) == "ConnectionOut"
    assert {"422", "503"} <= set(token["responses"])


def test_the_bodies_of_access() -> None:
    app_in = SCHEMAS["OAuthAppSetRequest"]
    assert set(app_in["required"]) == {"clientId", "clientSecret"}
    assert app_in["properties"]["clientSecret"]["writeOnly"] is True
    assert (app_in["properties"]["clientSecret"]["maxLength"]) == 4096
    assert set(SCHEMAS["OAuthAppOut"]["properties"]) == {
        "type",
        "configured",
        "clientId",
        "updatedAt",
    }
    assert "clientSecret" not in SCHEMAS["OAuthAppOut"]["properties"]

    authorize = SCHEMAS["ConnectionAuthorizeOut"]["properties"]
    assert set(authorize) == {"authorizeUrl", "expiresAt"}
    assert {"type": "null"} in authorize["authorizeUrl"]["anyOf"]

    token = SCHEMAS["ConnectionTokenRequest"]
    assert set(token["required"]) == {"account", "token"}
    assert set(token["properties"]) == {"account", "token", "expiresAt"}
    assert token["properties"]["token"]["writeOnly"] is True
    assert token["properties"]["token"]["maxLength"] == 8192


@pytest.mark.parametrize(
    "body",
    [
        {"account": "a", "token": ""},
        {"account": "", "token": "k"},
        {"account": "a" * (MAX_ACCOUNT + 1), "token": "k"},
        {"account": "a", "token": "k" * 8193},
        {"account": "a", "token": None},
        {"account": "a", "token": "k", "expiresAt": "2030-01-01T00:00:00"},
        {"account": "a", "token": "k", "extra": 1},
    ],
)
def test_a_key_body_is_narrow(body: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        ConnectionTokenRequest.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {"clientId": "", "clientSecret": "s"},
        {"clientId": "c", "clientSecret": ""},
        {"clientId": "c" * 501, "clientSecret": "s"},
        {"clientId": "c", "clientSecret": "s" * 4097},
        {"clientId": "c"},
    ],
)
def test_an_oauth_app_body_is_narrow(body: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        OAuthAppSetRequest.model_validate(body)


def test_the_callback_is_marked_public_and_takes_any_query() -> None:
    source = inspect.getsource(connection_routes)
    assert '# authz: public — одноразовый state\n@router.get(\n    "/connections:callback"' in (
        source
    )
    assert getattr(connection_routes.connection_oauth_callback, "_cp_open_query", False) is True


@pytest.mark.parametrize(
    ("event_type", "entity", "fields"),
    [
        (
            "connection.authorized",
            "connection",
            {"key", "type", "auth", "previousStatus", "connectedBy"},
        ),
        ("connection.authorization_failed", "connection", {"key", "type", "reason", "initiatedBy"}),
        ("connection_type.oauth_app_set", "connection_type", {"type", "created"}),
    ],
)
def test_the_access_events_carry_keys_and_codes_only(
    event_type: str, entity: str, fields: set[str]
) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == entity
    assert entry.current.version == 1
    schema = entry.current.schema
    assert set(schema["properties"]) == fields
    assert set(schema["required"]) == fields


# --- paths (§1) -------------------------------------------------------------------------------


def test_the_paths_of_a_connection_and_of_an_application() -> None:
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert connection_store_name(tenant, "crm") == f"tenants/{tenant}/connections/crm"
    assert oauth_creds_ref(tenant, "crm") == f"oauth2/creds/tenants/{tenant}/connections/crm"
    assert kv_connection_ref(tenant, "crm") == f"kv/data/tenants/{tenant}/connections/crm"
    assert oauth_app_path("crm") == "platform/oauth-apps/crm"
    attempt = uuid.UUID("00000000-0000-0000-0000-000000000002")
    assert oauth_server_name(tenant, "crm", attempt) == (
        f"tenants/{tenant}/connections/crm/{attempt}"
    )


# --- the account (§2, §6) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("account", "fits"),
    [
        ("acme.crm.example", True),
        ("a-1.crm.example", True),
        ("", False),
        ("evil.example", False),
        ("ACME.crm.example", False),  # the pattern is case-sensitive
        ("acme.crm.example/", False),
        ("-acme.crm.example", False),
        ("a" * MAX_ACCOUNT, False),
    ],
)
def test_an_account_of_a_host_template(account: str, fits: bool) -> None:
    assert (account_problem(SPEC, account) is None) is fits


def test_an_account_is_bounded_before_the_pattern_runs() -> None:
    # Over a DNS name the pattern is never run: ``(a+)+b`` on this input would
    # backtrack for longer than any test waits.
    spec = spec_with("accountField.pattern", r"(a+)+b")
    started = time.monotonic()
    problem = account_problem(spec, "a" * (MAX_ACCOUNT + 1))
    assert problem is not None and "1..253" in problem
    assert time.monotonic() - started < 1


def test_the_host_of_the_exchange_is_an_external_name() -> None:
    # A pattern the author left wide open does not let the host be anything.
    wide = spec_with("accountField.pattern", r".+")
    assert account_problem(wide, "localhost") is not None
    assert account_problem(wide, "10.0.0.1") is not None
    assert account_problem(wide, "evil.example#") is not None
    assert account_problem(wide, "acme.crm.example") is None
    # Without {account} in the template the account is only the pattern's.
    assert account_problem(TOKEN_ONLY, "acme") is None
    assert account_problem(TOKEN_ONLY, "ACME") is not None


def test_a_final_newline_is_not_part_of_an_account() -> None:
    # The type's pattern lets anything through; the host form still holds.
    anything = spec_with("accountField.pattern", r"[\s\S]+")
    assert account_problem(anything, "acme.crm.example") is None
    assert account_problem(anything, "acme.crm.example\n") is not None
    assert account_problem(anything, "acme\n.crm.example") is not None


def test_the_token_url_names_the_account() -> None:
    assert token_url(SPEC, "acme.crm.example") == "https://acme.crm.example/oauth2/access_token"
    fixed = spec_with("oauth2.tokenUrlTemplate", "https://oauth.crm.example/token")
    assert token_url(fixed, None) == "https://oauth.crm.example/token"
    with pytest.raises(ValueError):
        token_url(SPEC, None)
    with pytest.raises(ValueError):
        token_url(TOKEN_ONLY, "acme")


# --- the state and the provider's address (§6) ----------------------------------------------------


def test_a_state_is_256_random_bits_kept_as_a_hash() -> None:
    states = {new_state() for _ in range(100)}
    assert len(states) == 100
    for state in states:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
    state = next(iter(states))
    assert state_hash(state) == hashlib.sha256(state.encode()).digest()
    assert len(state_hash(state)) == 32


@pytest.mark.parametrize(
    ("base", "scopes", "expected_prefix"),
    [
        ("https://www.crm.example/oauth", [], "https://www.crm.example/oauth?"),
        ("https://www.crm.example/oauth?x=1", ["a", "b"], "https://www.crm.example/oauth?x=1&"),
        ("https://www.crm.example/oauth?", [], "https://www.crm.example/oauth?"),
        ("https://www.crm.example/oauth#frag", [], "https://www.crm.example/oauth?"),
    ],
)
def test_the_authorize_url(base: str, scopes: list[str], expected_prefix: str) -> None:
    spec = spec_with("oauth2.authorizeUrl", base)
    spec["oauth2"]["scopes"] = scopes
    url = authorize_url(spec, client_id="app client", state="s-1", redirect_uri="https://cp/cb?a=b")
    assert url.startswith(expected_prefix)
    assert "#" not in url
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["app client"]
    assert query["state"] == ["s-1"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["https://cp/cb?a=b"]
    assert query.get("scope") == ([" ".join(scopes)] if scopes else None)


def test_a_provider_text_loses_the_values_the_core_passed_through() -> None:
    code = "c0de-" + "x" * 20
    secret = "app-secret-" + "y" * 20
    text = f"invalid_client: code {code} client_secret={secret} ghp_{'a' * 36}"
    kept = provider_text(text, withhold=(code, secret))
    assert kept is not None
    assert code not in kept and secret not in kept and "ghp_" not in kept
    assert kept.startswith("invalid_client")
    assert len(provider_text("x" * 2000) or "") == 500
    assert provider_text(None) is None
    assert provider_text("   ") is None
    assert provider_text("text", withhold=("",)) == "text"


# --- the access log (§14; review of I009) ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/connections:callback/",
        "/api/v1/connections:callback//",
        "/API/V1/Connections:Callback",
        "//api//v1//connections:callback",
        # Variants the router answers otherwise (404, 405, a redirect) are
        # logged with their query all the same (review of I010).
        "/api/v1/./connections:callback",
        "/api/v1/../v1/connections:callback",
        "/api/v1/connections:callback;x",
        "/api/v1/connections:callback ",
    ],
)
def test_the_callback_query_is_redacted_whatever_the_case_or_slashes(path: str) -> None:
    # The path as uvicorn logs it: a trailing slash is answered with a 307
    # redirect by Starlette and still logged with its query.
    access_path = get_path_with_query_string(
        {"type": "http", "path": path, "root_path": "", "query_string": b"state=s3cr3t&code=c0de"}  # type: ignore[typeddict-item]
    )
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "f",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", access_path, "1.1", 307),
        None,
    )
    assert AccessLogQueryFilter().filter(record) is True
    message = record.getMessage()
    assert message.endswith('?[redacted] HTTP/1.1" 307')
    assert "c0de" not in message and "s3cr3t" not in message


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/connections:callbackx?code=1",
        "/api/v1/connections/callback?code=1",
        "/api/v1/connections?type=crm",
    ],
)
def test_other_paths_keep_their_query(path: str) -> None:
    assert redact_query(path) == path


# --- the addresses of the installation (§6; review of I010) ---------------------------------------


@pytest.mark.parametrize(
    ("url", "https"),
    [
        ("https://cp.example/api/v1/connections:callback", True),
        ("https://console.example", True),
        ("", False),
        ("http://cp.example/api/v1/connections:callback", False),
        ("HTTP://cp.example/cb", False),
        ("https://", False),
        ("https:///cb", False),
        ("//cp.example/cb", False),
        ("javascript:alert(1)", False),
        (" https://cp.example/cb", False),
        ("https://[::1/cb", False),
    ],
)
def test_an_https_address(url: str, https: bool) -> None:
    assert is_https_url(url) is https


@pytest.mark.parametrize("name", ["oauth_redirect_uri", "connections_return_url"])
def test_the_oauth_addresses_are_https_or_empty(name: str) -> None:
    assert getattr(Settings(**{name: ""}), name) == ""  # type: ignore[arg-type]
    assert getattr(Settings(**{name: "https://cp.example/x"}), name) == "https://cp.example/x"  # type: ignore[arg-type]
    with pytest.raises(PydanticValidationError):
        Settings(**{name: "http://cp.example/x"})  # type: ignore[arg-type]
