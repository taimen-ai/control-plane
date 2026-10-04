"""The checks a ``ConnectionType`` spec passes before it is published (CP-ADR-0079 §2)."""

import copy
from typing import Any

import pytest

from control_plane.domain.connection_type import check_connection_type_spec, external_host_problem
from control_plane.domain.errors import BadRequestError, DomainError, ValidationError

OAUTH2: dict[str, Any] = {
    "authorizeUrl": "https://www.crm.example/oauth",
    "tokenUrlTemplate": "https://{account}/oauth2/access_token",
    "accountParam": "referer",
    "authStyle": "in_params",
    "scopes": [],
}
SPEC: dict[str, Any] = {
    "displayName": "CRM",
    "description": "The CRM of the first wave",
    "auth": ["oauth2", "token"],
    "oauth2": OAUTH2,
    "accountField": {
        "title": "Portal",
        "description": "The address of the portal",
        "pattern": r"[a-z0-9-]+\.crm\.example",
    },
    "settingsSchema": {
        "type": "object",
        "properties": {"pipelineId": {"type": "integer", "default": 1}},
    },
    "defaultKey": "crm",
}
TOKEN_ONLY: dict[str, Any] = {
    "displayName": "Tracker",
    "auth": ["token"],
    "accountField": {"title": "Workspace", "pattern": "[a-z]+"},
    "settingsSchema": {"type": "object"},
    "defaultKey": "tracker",
}


def spec_with(path: str, value: Any, base: dict[str, Any] = SPEC) -> dict[str, Any]:
    """A copy of ``base`` with ``path`` (dotted) set to ``value``; ``...`` removes it."""
    spec = copy.deepcopy(base)
    *parents, name = path.split(".")
    node = spec
    for parent in parents:
        node = node[parent]
    if value is ...:
        del node[name]
    else:
        node[name] = value
    return spec


def refusal(spec: dict[str, Any]) -> DomainError:
    with pytest.raises(DomainError) as caught:
        check_connection_type_spec(spec)
    return caught.value


def test_a_full_spec_and_a_token_only_spec_pass() -> None:
    checked = check_connection_type_spec(copy.deepcopy(SPEC))
    assert checked.display_name == "CRM"
    assert checked.auth == ["oauth2", "token"]
    assert checked.spec_hash.startswith("sha256:")
    assert check_connection_type_spec(copy.deepcopy(TOKEN_ONLY)).auth == ["token"]


def test_the_hash_is_of_the_canonical_json() -> None:
    reordered = dict(reversed(list(copy.deepcopy(SPEC).items())))
    assert check_connection_type_spec(reordered).spec_hash == (
        check_connection_type_spec(copy.deepcopy(SPEC)).spec_hash
    )
    other = spec_with("displayName", "CRM 2")
    assert check_connection_type_spec(other).spec_hash != (
        check_connection_type_spec(copy.deepcopy(SPEC)).spec_hash
    )


@pytest.mark.parametrize(
    ("spec", "location"),
    [
        (spec_with("unknown", 1), "body.spec.unknown"),
        (spec_with("oauth2.revokeUrl", "https://a.example"), "body.spec.oauth2.revokeUrl"),
        (spec_with("accountField.placeholder", "x"), "body.spec.accountField.placeholder"),
    ],
)
def test_an_unknown_field_is_invalid_request(spec: dict[str, Any], location: str) -> None:
    error = refusal(spec)
    assert isinstance(error, BadRequestError)
    assert error.code == "invalid_request"
    assert [e["loc"] for e in error.details["errors"]] == [location]


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (spec_with("displayName", ...), "spec.displayName"),
        (spec_with("displayName", ""), "spec.displayName"),
        (spec_with("displayName", None), "spec.displayName"),
        (spec_with("settingsSchema", None), "spec.settingsSchema"),
        (spec_with("displayName", 5), "spec.displayName"),
        (spec_with("displayName", "x" * 201), "spec.displayName"),
        (spec_with("description", "x" * 2001), "spec.description"),
        (spec_with("description", ["x"]), "spec.description"),
        (spec_with("auth", ...), "spec.auth"),
        (spec_with("auth", []), "spec.auth"),
        (spec_with("auth", "token"), "spec.auth"),
        (spec_with("auth", ["token", "token"]), "spec.auth"),
        (spec_with("auth", ["password"]), "spec.auth"),
        (spec_with("auth", [None]), "spec.auth"),
        (spec_with("oauth2", ...), "spec.oauth2"),
        (spec_with("oauth2", None), "spec.oauth2"),
        (spec_with("oauth2", "https://a.example"), "spec.oauth2"),
        (spec_with("oauth2", OAUTH2, TOKEN_ONLY), "spec.oauth2"),
        (spec_with("oauth2.authorizeUrl", ...), "spec.oauth2.authorizeUrl"),
        (
            spec_with("oauth2.authorizeUrl", "http://www.crm.example/oauth"),
            "spec.oauth2.authorizeUrl",
        ),
        (spec_with("oauth2.authorizeUrl", "https:///oauth"), "spec.oauth2.authorizeUrl"),
        (spec_with("oauth2.authorizeUrl", "https://a.example:x/"), "spec.oauth2.authorizeUrl"),
        (spec_with("oauth2.authorizeUrl", "https://a.example/ x"), "spec.oauth2.authorizeUrl"),
        (spec_with("oauth2.tokenUrlTemplate", ...), "spec.oauth2.tokenUrlTemplate"),
        (spec_with("oauth2.accountParam", "re ferer"), "spec.oauth2.accountParam"),
        (spec_with("oauth2.accountParam", ""), "spec.oauth2.accountParam"),
        (spec_with("oauth2.accountParam", "x" * 65), "spec.oauth2.accountParam"),
        (spec_with("oauth2.accountParam", ...), "spec.oauth2.accountParam"),
        (spec_with("oauth2.authStyle", ...), "spec.oauth2.authStyle"),
        (spec_with("oauth2.authStyle", "auto"), "spec.oauth2.authStyle"),
        (spec_with("oauth2.scopes", ...), "spec.oauth2.scopes"),
        (spec_with("oauth2.scopes", "crm"), "spec.oauth2.scopes"),
        (spec_with("oauth2.scopes", ["s"] * 51), "spec.oauth2.scopes"),
        (spec_with("oauth2.scopes", ["crm", ""]), "spec.oauth2.scopes[1]"),
        (spec_with("oauth2.scopes", ["x" * 201]), "spec.oauth2.scopes[0]"),
        (spec_with("accountField", ...), "spec.accountField"),
        (spec_with("accountField", ..., TOKEN_ONLY), "spec.accountField"),
        (spec_with("accountField", "portal"), "spec.accountField"),
        (spec_with("accountField.title", ...), "spec.accountField.title"),
        (spec_with("accountField.pattern", ...), "spec.accountField.pattern"),
        (spec_with("accountField.pattern", ""), "spec.accountField.pattern"),
        (spec_with("accountField.pattern", "a" * 501), "spec.accountField.pattern"),
        (spec_with("accountField.pattern", "([a-z"), "spec.accountField.pattern"),
        (spec_with("settingsSchema", ...), "spec.settingsSchema"),
        (spec_with("settingsSchema", {}), "spec.settingsSchema"),
        (spec_with("settingsSchema", {"type": "string"}), "spec.settingsSchema"),
        (spec_with("settingsSchema", ["object"]), "spec.settingsSchema"),
        (
            spec_with("settingsSchema", {"type": "object", "minProperties": "one"}),
            "spec.settingsSchema",
        ),
        (
            spec_with(
                "settingsSchema", {"type": "object", "$ref": "https://schemas.example/s.json"}
            ),
            "spec.settingsSchema",
        ),
        (
            spec_with("settingsSchema", {"type": "object", "description": "x" * (64 * 1024)}),
            "spec.settingsSchema",
        ),
        (spec_with("defaultKey", ...), "spec.defaultKey"),
        (spec_with("defaultKey", "CRM"), "spec.defaultKey"),
        (spec_with("defaultKey", "-crm"), "spec.defaultKey"),
        (spec_with("defaultKey", "a" * 64), "spec.defaultKey"),
        (spec_with("defaultKey", 1), "spec.defaultKey"),
    ],
)
def test_a_violation_of_the_form_names_the_field(spec: dict[str, Any], field: str) -> None:
    error = refusal(spec)
    assert isinstance(error, ValidationError)
    assert (error.code, error.details["field"]) == ("invalid_connection_type", field)


def test_the_account_field_is_optional_without_token_and_account() -> None:
    spec = spec_with("auth", ["oauth2"])
    spec["oauth2"]["tokenUrlTemplate"] = "https://oauth.crm.example/token"
    del spec["oauth2"]["accountParam"]
    del spec["accountField"]
    check_connection_type_spec(spec)


def test_account_param_is_needed_only_with_the_placeholder() -> None:
    spec = spec_with("oauth2.tokenUrlTemplate", "https://oauth.crm.example/token")
    del spec["oauth2"]["accountParam"]
    check_connection_type_spec(spec)
    # {account} in the path names an account too.
    spec["oauth2"]["tokenUrlTemplate"] = "https://oauth.crm.example/{account}/token"
    assert refusal(spec).details["field"] == "spec.oauth2.accountParam"


@pytest.mark.parametrize(
    "template",
    [
        "https://oauth.crm.example/token",
        "https://oauth.crm.example",
        "https://oauth.crm.example?grant=code",
        "https://{account}/oauth2/access_token",
        "https://{account}.crm.example/token",
        "https://auth.{account}.example/token",
        "https://oauth.crm.example/{account}/token",
        "https://xn--80ak6aa92e.example/token",
        "https://a1.b2",
    ],
)
def test_an_exchange_address_to_an_external_name_passes(template: str) -> None:
    check_connection_type_spec(spec_with("oauth2.tokenUrlTemplate", template))


@pytest.mark.parametrize(
    "template",
    [
        "http://oauth.crm.example/token",
        "oauth.crm.example/token",
        "https://localhost/token",
        "https://openbao/v1/token",
        "https://control-plane/token",
        "https://127.0.0.1/token",
        "https://127.1/token",
        "https://2130706433/token",
        "https://0x7f.1/token",
        "https://10.0.0.1/token",
        "https://crm.0x/token",
        "https://[::1]/token",
        "https://a..b/token",
        "https://.crm.example/token",
        "https://crm.example./token",
        "https://-crm.example/token",
        "https://crm-.example/token",
        "https://crm_x.example/token",
        "https://CRM.example/token",
        "https://oauth.crm.example:8443/token",
        "https://user@oauth.crm.example/token",
        "https://user:pw@oauth.crm.example/token",
        "https://" + "a" * 64 + ".example/token",
        "https:///token",
        "https://x{account}.crm.example/token",
        "https://{account}x.crm.example/token",
        "https://{account}:8443/token",
        "https://{account}.1/token",
        "https://{account}..example/token",
        "https://{tenant}.crm.example/token",
        "https://oauth.crm.example/{path}",
        "https://oauth.crm.example/{account",
        "https://oauth.crm.example/account}",
        "https://oauth.crm.example/{{account}}",
        "https://oauth.crm.example/ token",
    ],
)
def test_an_exchange_address_to_an_inner_host_is_refused(template: str) -> None:
    error = refusal(spec_with("oauth2.tokenUrlTemplate", template))
    assert (error.code, error.details["field"]) == (
        "invalid_connection_type",
        "spec.oauth2.tokenUrlTemplate",
    )


@pytest.mark.parametrize(
    ("host", "passes"),
    [
        ("portal.crm.example", True),
        ("a.b", True),
        ("localhost", False),
        ("openbao", False),
        ("10.0.0.1", False),
        ("127.1", False),
        ("0x7f.1", False),
        ("a..b", False),
        ("", False),
        ("[::1]", False),
        ("portal.crm.example\n", False),  # ``$`` would let a final newline through
        ("portal\n.crm.example", False),
        ("10\n", False),
    ],
)
def test_the_whole_host_rule_after_a_substitution(host: str, passes: bool) -> None:
    assert (external_host_problem(host) is None) is passes


@pytest.mark.parametrize(
    "name", ["password", "apiToken", "clientSecret", "client_secret", "Authorization", "privateKey"]
)
def test_a_settings_property_named_like_a_secret_is_refused(name: str) -> None:
    schema = {"type": "object", "properties": {"nested": {"properties": {name: {}}}}}
    error = refusal(spec_with("settingsSchema", schema))
    assert error.code == "secret_material_rejected"
    assert error.details == {
        "field": "spec",
        "errors": [
            {"path": "/settingsSchema/properties/nested/properties", "match": "secret_name"}
        ],
    }


def test_the_kinds_own_field_names_are_not_checked_by_name() -> None:
    # tokenUrlTemplate names "token", accountField is the core's schema: both pass.
    check_connection_type_spec(copy.deepcopy(SPEC))
    # secretRef stays allowed in settings, as everywhere.
    check_connection_type_spec(
        spec_with("settingsSchema", {"type": "object", "properties": {"secretRef": {}}})
    )


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        (
            "oauth2.authorizeUrl",
            "https://www.crm.example/oauth?client_secret=abcdefghijklmnop1234",
            "/oauth2/authorizeUrl",
        ),
        ("description", "Use sk-abcdefghijklmnopqrstuvwx to connect", "/description"),
        ("displayName", "ghp_abcdefghijklmnopqrstuvwxyz0123", "/displayName"),
        (
            "settingsSchema",
            {"type": "object", "properties": {"region": {"default": "AKIAABCDEFGHIJKLMNOP"}}},
            "/settingsSchema/properties/region/default",
        ),
        (
            "settingsSchema",
            {"type": "object", "examples": [{"x": "Bearer abcdefghijklmnopqrstuvwxyz"}]},
            "/settingsSchema/examples/0/x",
        ),
        ("oauth2.scopes", ["crm", "token=abcdefghijklmnopqrst"], "/oauth2/scopes/1"),
    ],
)
def test_material_in_any_string_of_the_spec_is_refused(path: str, value: Any, field: str) -> None:
    error = refusal(spec_with(path, value))
    assert error.code == "secret_material_rejected"
    assert error.details["field"] == "spec"
    assert [item["path"] for item in error.details["errors"]] == [field]
    # The value never comes back in the refusal.
    assert str(value) not in f"{error.message} {error.details}"


def test_mentioning_a_token_is_not_material() -> None:
    check_connection_type_spec(spec_with("description", "Paste the API token of the portal"))


def test_nan_is_not_plain_json() -> None:
    schema = {"type": "object", "properties": {"rate": {"default": float("nan")}}}
    assert refusal(spec_with("settingsSchema", schema)).code == "invalid_connection_type"


def test_null_is_not_set_and_publishes_the_same_version() -> None:
    explicit = copy.deepcopy(TOKEN_ONLY)
    explicit["description"] = None
    explicit["oauth2"] = None
    explicit["accountField"]["description"] = None
    checked = check_connection_type_spec(explicit)
    assert checked.spec == TOKEN_ONLY
    assert checked.spec_hash == check_connection_type_spec(copy.deepcopy(TOKEN_ONLY)).spec_hash
    # The author's settings schema keeps its nulls.
    schema = {"type": "object", "properties": {"x": {"default": None}}}
    assert (
        check_connection_type_spec(spec_with("settingsSchema", schema)).spec["settingsSchema"]
        == schema
    )


def test_a_credential_shaped_member_name_is_refused_and_not_quoted() -> None:
    name = "ghp_" + "a" * 36
    schema = {"type": "object", "properties": {name: {"type": "string"}}}
    error = refusal(spec_with("settingsSchema", schema))
    assert isinstance(error, ValidationError)
    assert error.code == "secret_material_rejected"
    assert error.details == {
        "field": "spec",
        "errors": [{"path": "/settingsSchema/properties", "match": "provider_token"}],
    }
    assert name not in str(error) and name not in str(error.details)

    unknown = refusal(spec_with(name, 1))
    assert name not in str(unknown) and name not in str(unknown.details)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("defaultKey", "crm\n"),
        ("oauth2.accountParam", "referer\n"),
        ("oauth2.tokenUrlTemplate", "https://oauth.crm.example\n/token"),
    ],
)
def test_a_final_newline_is_not_part_of_a_form(path: str, value: str) -> None:
    error = refusal(spec_with(path, value))
    assert error.code in ("invalid_connection_type", "invalid_request")
