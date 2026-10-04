"""Access to a connection: paths in the secret store, the account, the OAuth state.

CP-ADR-0079 §1, §2, §6, §7. Pure functions over plain values; no I/O.

- **Paths.** Where the material of a connection and the OAuth application of
  a type live in OpenBao. ``secretRef`` of a connection is the read path in the
  store's API terms (``oauth2/creds/…`` or ``kv/data/…``): a reference, not a
  value.
- **The account.** It arrives from outside — a parameter of the callback the
  provider (or anyone) sets, or the form of a key — and may go into the host
  the code and the application's secret are sent to. It is at most a DNS name
  long before the type's pattern sees it: the pattern is the package author's,
  and a bounded input bounds what a careless pattern can cost (ReDoS). Put
  into ``{account}`` it is also a host name of the core's form, and a host
  after the substitution is an external DNS name.
- **The state.** 256 random bits, base64url without padding; only its SHA-256
  is kept.
"""

import base64
import hashlib
import re
import secrets
import uuid
from typing import Any
from urllib.parse import quote, urlencode

from control_plane.domain.connection_type import (
    ACCOUNT_PLACEHOLDER,
    external_host_problem,
)
from control_plane.domain.redaction import redact_secret_material

# The longest DNS name: an account is never longer, whatever the type's pattern.
MAX_ACCOUNT = 253
MAX_STATUS_MESSAGE = 500
STATE_BYTES = 32

_ACCOUNT_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


# --- paths (§1) -----------------------------------------------------------------------


def connection_store_name(tenant_id: uuid.UUID, key: str) -> str:
    """The name of the OAuth server and creds of a connection: ``tenants/<t>/connections/<key>``."""
    return f"tenants/{tenant_id}/connections/{key}"


def oauth_server_name(tenant_id: uuid.UUID, key: str, attempt: uuid.UUID) -> str:
    """The OAuth server of one authorization attempt: ``…/connections/<key>/<attempt>``.

    Every attempt writes its own server, so a failed exchange never touches
    the one the creds of the connection refresh through.
    """
    return f"{connection_store_name(tenant_id, key)}/{attempt}"


def oauth_creds_ref(tenant_id: uuid.UUID, key: str) -> str:
    return f"oauth2/creds/{connection_store_name(tenant_id, key)}"


def kv_connection_ref(tenant_id: uuid.UUID, key: str) -> str:
    return f"kv/data/{connection_store_name(tenant_id, key)}"


def agents_store_name(tenant_id: uuid.UUID) -> str:
    """The ``kv`` prefix of the tenant's agents' secrets: ``tenants/<t>/agents`` (§11)."""
    return f"tenants/{tenant_id}/agents"


def agent_secrets_store_name(tenant_id: uuid.UUID, agent_key: str) -> str:
    """The ``kv`` prefix of an agent's secrets by name: ``tenants/<t>/agents/<key>`` (§11)."""
    return f"{agents_store_name(tenant_id)}/{agent_key}"


def agent_secret_store_name(tenant_id: uuid.UUID, agent_key: str, name: str) -> str:
    """The ``kv`` path (under ``kv/data`` and ``kv/metadata``) of one secret of an agent."""
    return f"{agent_secrets_store_name(tenant_id, agent_key)}/{name}"


def agent_secrets_ref(tenant_id: uuid.UUID, agent_key: str) -> str:
    """The read path of an agent's secrets in its policy: ``kv/data/…/agents/<key>/*``."""
    return f"kv/data/{agent_secrets_store_name(tenant_id, agent_key)}/*"


def oauth_app_path(type_key: str) -> str:
    """The ``kv`` path (under ``kv/data`` and ``kv/metadata``) of a type's OAuth application."""
    return f"platform/oauth-apps/{type_key}"


# --- agents' secrets by name (§11) ------------------------------------------------------

MAX_SECRET_VALUE = 65_536
_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def is_secret_name(name: str) -> bool:
    """The form of ``placement.secrets`` (ADR-0073): a name, never a path."""
    return _SECRET_NAME.fullmatch(name) is not None


# --- the account (§2, §6, §7) -----------------------------------------------------------


def account_problem(spec: dict[str, Any], account: str) -> str | None:
    """Why ``account`` cannot be the account of a connection of this type, or ``None``.

    The length comes first, so the pattern of the type never runs on an
    unbounded string.
    """
    if not account or len(account) > MAX_ACCOUNT:
        return f"the account is 1..{MAX_ACCOUNT} characters"
    account_field = spec.get("accountField")
    if (
        isinstance(account_field, dict)
        and isinstance(account_field.get("pattern"), str)
        and re.fullmatch(account_field["pattern"], account) is None
    ):
        return "the account does not match the pattern of the connection type"
    template = _token_url_template(spec)
    if template is not None and ACCOUNT_PLACEHOLDER in template:
        if not _ACCOUNT_HOST.fullmatch(account):
            return "the account is a host name: [a-z0-9.-], a letter or a digit at its edges"
        if ACCOUNT_PLACEHOLDER in _authority(template):
            problem = external_host_problem(_authority(token_url(spec, account)))
            if problem is not None:
                return problem
    return None


def _token_url_template(spec: dict[str, Any]) -> str | None:
    oauth2 = spec.get("oauth2")
    if not isinstance(oauth2, dict):
        return None
    template = oauth2.get("tokenUrlTemplate")
    return template if isinstance(template, str) else None


def _authority(url: str) -> str:
    rest = url[len("https://") :] if url.startswith("https://") else url
    return re.split(r"[/?#]", rest, maxsplit=1)[0]


def token_url(spec: dict[str, Any], account: str | None) -> str:
    """The exchange address of the type with the account in ``{account}``."""
    template = _token_url_template(spec)
    if template is None:
        raise ValueError("the connection type has no oauth2")
    if ACCOUNT_PLACEHOLDER in template:
        if account is None:
            raise ValueError("the exchange address names {account}")
        return template.replace(ACCOUNT_PLACEHOLDER, account)
    return template


# --- the state (§6) ---------------------------------------------------------------------


def new_state() -> str:
    """256 random bits, base64url without padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(STATE_BYTES)).rstrip(b"=").decode("ascii")


def state_hash(state: str) -> bytes:
    return hashlib.sha256(state.encode("utf-8")).digest()


def authorize_url(spec: dict[str, Any], *, client_id: str, state: str, redirect_uri: str) -> str:
    """``oauth2.authorizeUrl`` with ``client_id``, ``state``, ``response_type``,
    ``redirect_uri`` and ``scope`` (space-separated, only when there are scopes)."""
    oauth2 = spec["oauth2"]
    params = {
        "client_id": client_id,
        "state": state,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    scopes = oauth2.get("scopes") or []
    if scopes:
        params["scope"] = " ".join(scopes)
    base = str(oauth2["authorizeUrl"])
    base, _hash, _fragment = base.partition("#")
    separator = "&" if "?" in base else "?"
    if base.endswith(("?", "&")):
        separator = ""
    return f"{base}{separator}{urlencode(params, quote_via=quote)}"


# --- texts of the provider (§14) ---------------------------------------------------------


def provider_text(text: str | None, *, withhold: tuple[str, ...] = ()) -> str | None:
    """A text of the provider or the store, fit for ``statusMessage``.

    Credential-shaped material is redacted, the values the core itself passed
    through (the code, the application's secret) are cut out wherever they are
    echoed, and the text is at most 500 characters.
    """
    if text is None:
        return None
    for value in withhold:
        if value:
            text = text.replace(value, "[redacted]")
    text = redact_secret_material(text).strip()
    return text[:MAX_STATUS_MESSAGE] or None


# --- agents' policies and roles in the store (§9) --------------------------------------

AGENT_POLICY_PREFIX = "cp-agent-"
AGENT_ROLE_PREFIX = "agent-"
AGENT_TOKEN_TTL_SECONDS = 300
SECRET_STORE_AUDIENCE = "openbao"


def agent_policy_name(principal_id: uuid.UUID) -> str:
    """``cp-agent-<principalId>``: the ACL policy of an agent (CP principal id)."""
    return f"{AGENT_POLICY_PREFIX}{principal_id}"


def agent_role_name(principal_id: uuid.UUID) -> str:
    """``agent-<principalId>``: the ``jwt`` role an agent logs in with."""
    return f"{AGENT_ROLE_PREFIX}{principal_id}"


def principal_of_policy(name: str) -> uuid.UUID | None:
    """The principal id in ``cp-agent-<id>``; ``None`` for any other name."""
    return _principal_after(name, AGENT_POLICY_PREFIX)


def principal_of_role(name: str) -> uuid.UUID | None:
    """The principal id in ``agent-<id>``; ``None`` for any other name."""
    return _principal_after(name, AGENT_ROLE_PREFIX)


def _principal_after(name: str, prefix: str) -> uuid.UUID | None:
    if not name.startswith(prefix):
        return None
    try:
        principal = uuid.UUID(name[len(prefix) :])
    except ValueError:
        return None
    return principal if str(principal) == name[len(prefix) :] else None


def agent_policy(read_paths: list[str]) -> str:
    """The text of an agent's policy: ``read`` on each path, nothing else.

    Paths are sorted and unique, so one set of paths always gives one text and
    the worker compares texts, not meanings. A path is a ``secretRef`` of an
    active connection or the prefix of the agent's secrets (``…/*``). A path
    outside :func:`is_policy_path` raises ``ValueError``: the callers check
    their keys first, and this is the last line before the store.
    """
    for path in read_paths:
        if not is_policy_path(path):
            raise ValueError("a policy path outside the core's form")
    lines = [f'path "{path}" {{ capabilities = ["read"] }}' for path in sorted(set(read_paths))]
    return "\n".join(lines) + "\n"


# Segments of lower-case letters, digits, ``-``, ``.`` and ``_`` that start with
# a letter or a digit (so no ``.`` or ``..``), and ``/*`` only at the end. The
# keys and ids that make the paths are checked before they get here; this check
# is the policy's own, so no quote, newline, brace or glob ever reaches the
# text of a policy.
_POLICY_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def is_policy_path(path: str) -> bool:
    """Whether ``path`` may stand in an agent's policy (see :func:`agent_policy`)."""
    if not path.isascii() or len(path) > 512:
        return False
    segments = path.split("/")
    if segments[-1] == "*":
        segments = segments[:-1]
    return len(segments) >= 2 and all(_POLICY_SEGMENT.fullmatch(segment) for segment in segments)


def agent_role(
    *, principal_id: uuid.UUID, iam_principal_id: uuid.UUID, iam_tenant_id: uuid.UUID
) -> dict[str, Any]:
    """The ``jwt`` role of an agent: the IAM subject of the agent, tokens of 5 minutes.

    The subject and the tenant are the IAM's: the IAM issues the agent's
    token. The one policy is the agent's own, without ``default``.
    """
    return {
        "role_type": "jwt",
        "user_claim": "sub",
        "bound_subject": str(iam_principal_id),
        "bound_claims_type": "string",
        "bound_claims": {"tenant_id": str(iam_tenant_id), "principal_type": "agent"},
        "bound_audiences": [SECRET_STORE_AUDIENCE],
        "token_policies": [agent_policy_name(principal_id)],
        "token_no_default_policy": True,
        "token_ttl": AGENT_TOKEN_TTL_SECONDS,
        "token_max_ttl": AGENT_TOKEN_TTL_SECONDS,
    }


def role_matches(stored: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Whether the role the store answers already is ``desired``.

    The store answers more fields than the core writes, with its own defaults;
    only the fields the core writes are compared. Lists compare as sets (the
    store may reorder them), TTLs as seconds (the store may answer ``"5m"``).
    """
    for name, value in desired.items():
        current = stored.get(name)
        if isinstance(value, list):
            if not isinstance(current, list) or sorted(map(str, current)) != sorted(value):
                return False
        elif name in ("token_ttl", "token_max_ttl"):
            if _seconds(current) != value:
                return False
        elif isinstance(value, dict):
            if (
                not isinstance(current, dict)
                or {str(k): _claim_value(v) for k, v in current.items()} != value
            ):
                return False
        elif current != value:
            return False
    return True


def _claim_value(value: Any) -> Any:
    # A bound claim may come back as a one-element list of the string written.
    if isinstance(value, list) and len(value) == 1:
        return str(value[0])
    return value


_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        match = _DURATION.fullmatch(value)
        if match and any(match.groups()):
            hours, minutes, seconds = (int(part or 0) for part in match.groups())
            return hours * 3600 + minutes * 60 + seconds
    return None
