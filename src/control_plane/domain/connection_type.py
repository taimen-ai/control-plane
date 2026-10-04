"""The catalog kind ``ConnectionType``: what connecting a system of one kind takes.

CP-ADR-0079 §2. A provider's package publishes it; a version ``(key, version)``
is immutable, its ``status`` moves like a skill's (ADR-0021). What is decided
here, without I/O, is whether a ``spec`` may be published:

- **Form.** The fields of the table of §2; a field the kind does not know is
  ``400 invalid_request``, any other violation ``422 invalid_connection_type``
  with ``details.field``.
- **Secrets.** A property of ``settingsSchema`` named like a secret and any
  string of the spec that carries credential-shaped material are ``422
  secret_material_rejected`` with ``details.errors[{path, match}]`` (the
  amendment of 2026-10-03). The names of the kind's own fields
  (``tokenUrlTemplate``, ``accountField``) are the core's, not the author's,
  and are not checked by name.
- **The host of the exchange address.** ``oauth2.tokenUrlTemplate`` goes to an
  external DNS name only: two or more labels, the last one not numeric, no
  port, no userinfo. A host with ``{account}`` is checked in its literal part
  here; the callback checks the whole host after the substitution with
  :func:`external_host_problem`.

Pure functions over plain values; no I/O.
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jsonschema

from control_plane.domain.errors import BadRequestError, ValidationError
from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.project import secret_findings, validate_json_schema_document
from control_plane.domain.redaction import secret_material

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
AUTH_METHODS = ("oauth2", "token")
AUTH_STYLES = ("in_params", "in_header")
ACCOUNT_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ACCOUNT_PLACEHOLDER = "{account}"
MAX_DISPLAY_NAME = 200
MAX_DESCRIPTION = 2000
MAX_URL = 2000
MAX_SCOPES = 50
MAX_SCOPE = 200
MAX_TITLE = 200
MAX_PATTERN = 500
MAX_SETTINGS_ERRORS = 50

SPEC_FIELDS = frozenset(
    {
        "displayName",
        "description",
        "auth",
        "oauth2",
        "accountField",
        "settingsSchema",
        "defaultKey",
    }
)
OAUTH2_FIELDS = frozenset(
    {"authorizeUrl", "tokenUrlTemplate", "accountParam", "authStyle", "scopes"}
)
ACCOUNT_FIELDS = frozenset({"title", "description", "pattern"})

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class CheckedSpec:
    """A spec that may be published, and the hash two publications compare."""

    spec: dict[str, Any]
    spec_hash: str
    display_name: str
    auth: list[str]


def _invalid(field: str, message: str) -> ValidationError:
    return ValidationError(
        "invalid_connection_type", message, details={"field": f"spec.{field}" if field else "spec"}
    )


def _reject_unknown(value: Any, known: frozenset[str], where: str) -> None:
    if not isinstance(value, dict):
        return
    # A name shaped like a credential is not quoted back.
    extra = sorted(
        "[redacted]" if secret_material(str(name)) else str(name)
        for name in value
        if name not in known
    )
    if extra:
        raise BadRequestError(
            "invalid_request",
            "Request does not match the API contract",
            details={
                "errors": [
                    {"loc": f"body.{where}.{name}", "message": "Extra inputs are not permitted"}
                    for name in extra
                ]
            },
        )


def secret_refusal(found: list[dict[str, str]], *, field: str) -> ValidationError:
    """``422 secret_material_rejected`` over the findings of ``secret_findings``.

    The form of the amendment of 2026-10-03, as in CP-ADR-0081 4.3:
    ``details.errors`` — ``[{path, match}]``, JSON Pointers into the body
    member ``details.field``; no value and no name that carries material.
    """
    return ValidationError(
        "secret_material_rejected",
        f"{field} must not contain credentials; reference a secret store instead",
        details={"field": field, "errors": found},
    )


def spec_secret_findings(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Secret material in a ``spec``: strings and member names everywhere, names
    like a secret in ``settingsSchema`` only (§2: the kind's own field names,
    ``tokenUrlTemplate`` among them, are the core's)."""
    found = secret_findings(spec, names=False)
    schema = spec.get("settingsSchema")
    for item in secret_findings(schema, names=True):
        path = "/settingsSchema" + ("" if item["path"] == "/" else item["path"])
        moved = {"path": path, "match": item["match"]}
        if moved not in found:
            found.append(moved)
    return found


def _settings_message(error: Any) -> str:
    """What a schema violation is about, without the value that broke it."""
    keyword = str(error.validator)
    bound = error.validator_value
    if keyword == "type":
        kinds = bound if isinstance(bound, list) else [bound]
        return "must be of type " + " or ".join(str(kind) for kind in kinds)
    if keyword == "enum":
        return "must be one of the values the schema allows"
    if keyword == "const":
        return "must be the value the schema fixes"
    if isinstance(bound, bool) or not isinstance(bound, int | float | str):
        return f"does not satisfy {keyword}"
    return f"does not satisfy {keyword} {bound}"


def _extra_members(error: Any) -> list[str]:
    """The members of an object ``additionalProperties: false`` refuses."""
    schema = error.schema if isinstance(error.schema, dict) else {}
    declared = schema.get("properties") or {}
    patterns = list((schema.get("patternProperties") or {}).keys())
    return [
        str(name)
        for name in error.instance
        if name not in declared and not any(re.search(pattern, name) for pattern in patterns)
    ]


def settings_errors(schema: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, str]]:
    """``settings`` against a ``settingsSchema``: ``[{path, code, message}]``.

    The form of the amendment of 2026-10-03, as in CP-ADR-0081 4.3:
    ``path`` is the JSON Pointer of the field in ``settings`` (of the member
    itself for a missing required member and for one the schema does not
    declare), ``code`` the JSON Schema keyword, ``message`` an explanation
    that quotes the schema, never the value. Every violation, up to
    ``MAX_SETTINGS_ERRORS``, ordered by path. A schema that cannot be
    evaluated raises ``invalid_json_schema``.
    """
    validator = jsonschema.Draft202012Validator(schema)
    try:
        found = list(validator.iter_errors(settings))
    except Exception as exc:
        # A stored schema whose $ref cannot be resolved is a validation
        # failure, not a 500.
        raise ValidationError(
            "invalid_json_schema",
            "The settings schema of the connection type could not be evaluated",
            details={"field": "settings", "message": f"{type(exc).__name__}: {exc}"[:300]},
        ) from exc
    errors: list[dict[str, str]] = []
    for error in found:
        path = "".join(_pointer_part(part) for part in error.absolute_path)
        keyword = str(error.validator)
        if keyword == "required" and isinstance(error.instance, dict):
            missing = [str(name) for name in error.validator_value if name not in error.instance]
            errors.extend(
                {"path": path + _pointer_part(name), "code": keyword, "message": "is required"}
                for name in missing
            )
        elif (
            keyword == "additionalProperties"
            and error.validator_value is False
            and isinstance(error.instance, dict)
            and (extra := _extra_members(error))
        ):
            errors.extend(
                {
                    "path": path + _pointer_part(name),
                    "code": keyword,
                    "message": "is not a field of the schema",
                }
                for name in extra
            )
        else:
            errors.append(
                {"path": path or "/", "code": keyword, "message": _settings_message(error)}
            )
    errors.sort(key=lambda item: item["path"])
    return errors[:MAX_SETTINGS_ERRORS]


def _pointer_part(part: str | int) -> str:
    return "/" + str(part).replace("~", "~0").replace("/", "~1")


def _text(
    spec: dict[str, Any], name: str, *, field: str, max_length: int, min_length: int = 1
) -> str | None:
    value = spec.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not min_length <= len(value) <= max_length:
        raise _invalid(field, f"{field} is a string of {min_length}..{max_length} characters")
    return value


def _required_text(spec: dict[str, Any], name: str, *, field: str, max_length: int) -> str:
    value = _text(spec, name, field=field, max_length=max_length)
    if value is None:
        raise _invalid(field, f"{field} is required")
    return value


def external_host_problem(host: str) -> str | None:
    """Why ``host`` is not an external DNS name (§2), or ``None``.

    Two or more labels of ``[a-z0-9-]`` without ``-`` at the edges, the last
    one not numeric: every IPv4 literal a client parses (``127.1``,
    ``2130706433``, ``0x7f.1``) ends in a numeric label, IPv6 in ``[...]``
    fails the label, single-label names are the installation's own network.
    """
    labels = host.split(".")
    if len(labels) < 2:
        return "the host is a name of two or more labels"
    if not all(_LABEL.fullmatch(label) for label in labels):
        return "every label of the host is [a-z0-9-]{1,63} without '-' at its edges"
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        return "the last label of the host is not numeric: an IP address is not an external name"
    return None


def _authority(url: str) -> str:
    """The authority of an ``https://`` address: up to the first ``/``, ``?`` or ``#``."""
    rest = url[len("https://") :]
    return re.split(r"[/?#]", rest, maxsplit=1)[0]


def _check_authorize_url(url: str) -> None:
    field = "oauth2.authorizeUrl"
    if any(ch.isspace() for ch in url) or not url.startswith("https://"):
        raise _invalid(field, f"{field} is an absolute https URL")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError as exc:
        raise _invalid(field, f"{field} is an absolute https URL") from exc
    if not parts.hostname:
        raise _invalid(field, f"{field} is an absolute https URL")


def _check_token_url_template(template: str) -> bool:
    """The template's checks at publication; ``True`` when it names ``{account}``."""
    field = "oauth2.tokenUrlTemplate"
    if any(ch.isspace() for ch in template) or not template.startswith("https://"):
        raise _invalid(field, f"{field} is an https URL")
    stray = _PLACEHOLDER.sub(lambda m: "" if m.group(0) == ACCOUNT_PLACEHOLDER else "{", template)
    if "{" in stray or "}" in stray:
        raise _invalid(field, f"{field} has one placeholder only: {ACCOUNT_PLACEHOLDER}")
    host = _authority(template)
    if "@" in host:
        raise _invalid(field, f"{field} carries no userinfo in its host")
    if ":" in host:
        raise _invalid(field, f"{field} carries no port in its host")
    if ACCOUNT_PLACEHOLDER not in host:
        problem = external_host_problem(host)
        if problem is not None:
            raise _invalid(field, f"{field}: {problem}")
    else:
        # The placeholder takes whole labels; the literal ones are checked
        # now, the host after the substitution on the callback.
        labels = host.split(".")
        for label in labels:
            if label != ACCOUNT_PLACEHOLDER and not _LABEL.fullmatch(label):
                raise _invalid(
                    field,
                    f"{field}: {ACCOUNT_PLACEHOLDER} takes whole labels of the host, every"
                    " other label is [a-z0-9-]{1,63} without '-' at its edges",
                )
        if labels[-1] != ACCOUNT_PLACEHOLDER and _NUMERIC_LABEL.fullmatch(labels[-1]):
            raise _invalid(field, f"{field}: the last label of the host is not numeric")
    return ACCOUNT_PLACEHOLDER in template


def _check_oauth2(oauth2: Any) -> bool:
    """``spec.oauth2``; ``True`` when its exchange address names ``{account}``."""
    if not isinstance(oauth2, dict):
        raise _invalid("oauth2", "oauth2 is an object")
    authorize_url = _required_text(
        oauth2, "authorizeUrl", field="oauth2.authorizeUrl", max_length=MAX_URL
    )
    _check_authorize_url(authorize_url)
    template = _required_text(
        oauth2, "tokenUrlTemplate", field="oauth2.tokenUrlTemplate", max_length=MAX_URL
    )
    names_account = _check_token_url_template(template)
    account_param = oauth2.get("accountParam")
    if account_param is not None and not (
        isinstance(account_param, str) and ACCOUNT_PARAM_PATTERN.fullmatch(account_param)
    ):
        raise _invalid("oauth2.accountParam", "oauth2.accountParam matches ^[A-Za-z0-9_-]{1,64}$")
    if names_account and account_param is None:
        raise _invalid(
            "oauth2.accountParam",
            f"oauth2.accountParam is required: tokenUrlTemplate names {ACCOUNT_PLACEHOLDER}",
        )
    if oauth2.get("authStyle") not in AUTH_STYLES:
        raise _invalid("oauth2.authStyle", f"oauth2.authStyle is one of {', '.join(AUTH_STYLES)}")
    scopes = oauth2.get("scopes")
    if not isinstance(scopes, list) or len(scopes) > MAX_SCOPES:
        raise _invalid("oauth2.scopes", f"oauth2.scopes is a list of at most {MAX_SCOPES} scopes")
    for index, scope in enumerate(scopes):
        if not isinstance(scope, str) or not 1 <= len(scope) <= MAX_SCOPE:
            raise _invalid(
                f"oauth2.scopes[{index}]", f"a scope is a string of 1..{MAX_SCOPE} characters"
            )
    return names_account


def _check_account_field(account_field: Any) -> None:
    if not isinstance(account_field, dict):
        raise _invalid("accountField", "accountField is an object {title, description?, pattern}")
    _required_text(account_field, "title", field="accountField.title", max_length=MAX_TITLE)
    _text(
        account_field,
        "description",
        field="accountField.description",
        max_length=MAX_DESCRIPTION,
        min_length=0,
    )
    pattern = _required_text(
        account_field, "pattern", field="accountField.pattern", max_length=MAX_PATTERN
    )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _invalid(
            "accountField.pattern", f"accountField.pattern is not a regular expression: {exc}"
        ) from exc


def _check_settings_schema(schema: Any) -> None:
    field = "settingsSchema"
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise _invalid(field, "settingsSchema is a JSON Schema whose root is type: object")
    try:
        validate_json_schema_document(schema, field_name=f"spec.{field}")
    except ValidationError as exc:
        raise ValidationError(
            "invalid_connection_type",
            f"settingsSchema is not a valid JSON Schema: {exc.message}",
            details={**exc.details, "field": f"spec.{field}", "reason": exc.code},
        ) from exc


def _without_nulls(spec: dict[str, Any]) -> dict[str, Any]:
    """``spec`` without ``null`` members in it, ``oauth2`` and ``accountField``.

    ``settingsSchema`` is the author's document and is kept as it is.
    """
    kept = {name: value for name, value in spec.items() if value is not None}
    for name in ("oauth2", "accountField"):
        if isinstance(kept.get(name), dict):
            kept[name] = {key: value for key, value in kept[name].items() if value is not None}
    return kept


def check_connection_type_spec(spec: dict[str, Any]) -> CheckedSpec:
    """Every check of §2 a publication passes, in the order of the ADR's list.

    Unknown fields first (they are the API's shape), then secret material —
    before any message could quote a value — then the form and the rules of
    the fields. A member of the kind's own objects that is ``null`` is not
    set: it is dropped before the spec is kept and hashed, so ``null`` and an
    absent field publish the same version (a response reads both as ``null``).
    """
    spec = _without_nulls(spec)
    _reject_unknown(spec, SPEC_FIELDS, "spec")
    _reject_unknown(spec.get("oauth2"), OAUTH2_FIELDS, "spec.oauth2")
    _reject_unknown(spec.get("accountField"), ACCOUNT_FIELDS, "spec.accountField")

    # Strings (member names included) first: a name shaped like a credential
    # never reaches the path of the name check below.
    found = spec_secret_findings(spec)
    if found:
        raise secret_refusal(found, field="spec")

    display_name = _required_text(
        spec, "displayName", field="displayName", max_length=MAX_DISPLAY_NAME
    )
    _text(spec, "description", field="description", max_length=MAX_DESCRIPTION, min_length=0)
    auth = spec.get("auth")
    if (
        not isinstance(auth, list)
        or not auth
        or any(not isinstance(method, str) or method not in AUTH_METHODS for method in auth)
        or len(set(auth)) != len(auth)
    ):
        raise _invalid("auth", f"auth is a non-empty list without repeats of {AUTH_METHODS}")

    names_account = False
    if "oauth2" in auth:
        if spec.get("oauth2") is None:
            raise _invalid("oauth2", "oauth2 is required: auth names oauth2")
        names_account = _check_oauth2(spec["oauth2"])
    elif spec.get("oauth2") is not None:
        raise _invalid("oauth2", "oauth2 is described only for a type whose auth names oauth2")

    if spec.get("accountField") is not None:
        _check_account_field(spec["accountField"])
    elif "token" in auth or names_account:
        raise _invalid(
            "accountField",
            "accountField is required: auth names token or tokenUrlTemplate names"
            f" {ACCOUNT_PLACEHOLDER}",
        )

    if "settingsSchema" not in spec:
        raise _invalid("settingsSchema", "settingsSchema is required")
    _check_settings_schema(spec["settingsSchema"])

    default_key = spec.get("defaultKey")
    if not isinstance(default_key, str) or not KEY_PATTERN.fullmatch(default_key):
        raise _invalid("defaultKey", f"defaultKey matches {KEY_PATTERN.pattern}")

    try:
        spec_hash = canonical_hash(spec)
    except ValueError as exc:  # NaN or Infinity: not JSON the database keeps
        raise _invalid("", "spec is plain JSON: no NaN or Infinity") from exc
    return CheckedSpec(spec=spec, spec_hash=spec_hash, display_name=display_name, auth=list(auth))
