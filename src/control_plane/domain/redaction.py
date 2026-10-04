"""What must never reach durable state, in one place.

Handoff checkpoints and any future durable payload written by a harness share
the same prohibition list: no credentials, no transcripts or raw prompts, no
chain-of-thought, no machine-local absolute paths. Keeping the
patterns in the domain layer means the list is defined once and every writer is
guarded by the same rule rather than by a copy of it.

The guard is deliberately coarse — it catches an honest mistake loudly at write
time. It is not a scanner and does not pretend to be one.
"""

import re
from typing import Any

from control_plane.domain.errors import ValidationError

SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|password|secret|credential|"
    r"transcript|chat[_-]?history|raw[_-]?prompt|chain[_-]?of[_-]?thought|reasoning)",
    re.IGNORECASE,
)
# Keys whose values the JSON logger replaces, compared whole and lower-cased
# (CP-ADR-0079 §14). ``code`` is an OAuth authorization code: a log extra that
# carries an error code names it ``error_code``. Unlike the pattern above it is
# no substring match — ``reasonCode`` and ``key_hash_prefix`` stay readable.
LOG_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "key_hash",
        "password",
        "token",
        "code",
        "access_token",
        "refresh_token",
        "client_secret",
    }
)
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:^|\s)(?:/(?:Users|home|private|tmp|var|opt|Volumes)/|[A-Za-z]:[\\/])"
)


# Free text is a different problem from a JSON document. In a document the KEY
# names the secret, so a key-based guard is enough; in prose there is no key,
# and people legitimately talk ABOUT credentials ("rotate the API key before
# Friday"). So the patterns below match credential-shaped MATERIAL only:
# a recognizable token format, or an assignment whose value looks like a secret
# rather than like a word. Mentioning a secret stays allowed; pasting one does
# not.
_SECRET_MATERIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Provider-prefixed tokens: the prefix is the format, so a match is not a
    # guess about entropy.
    ("provider_token", re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}")),
    ("provider_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("provider_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("provider_token", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("provider_token", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9+/=_.-]{16,}",
            re.IGNORECASE,
        ),
    ),
)


def secret_material(value: str) -> str | None:
    """The kind of credential-shaped material in ``value``, if any.

    For writers that derive text rather than accept it (anchor values of a
    task context pack): they drop the value instead of refusing a request.
    """
    for kind, pattern in _SECRET_MATERIAL_PATTERNS:
        if pattern.search(value):
            return kind
    return None


REDACTED = "[redacted]"


def redact_secret_material(value: str) -> str:
    """``value`` with every credential-shaped match replaced by ``[redacted]``.

    For text copied into a place that is read far more widely than its source
    (an event payload every consumer sees): the source keeps the text, the copy
    loses the material.
    """
    for _kind, pattern in _SECRET_MATERIAL_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def reject_secret_text(value: str, *, code: str, subject: str) -> None:
    """Raise if free text carries something shaped like an actual credential.

    Coarse on purpose, and deliberately biased the other way from
    ``reject_secret_material``: a false positive here silences a person
    mid-discussion, so the guard refuses material, not vocabulary.
    """
    kind = secret_material(value)
    if kind is not None:
        raise ValidationError(
            code,
            f"{subject} must not contain credentials; reference a secret store instead",
            details={"match": kind},
        )


def reject_unsafe_durable_payload(
    value: Any,
    *,
    code: str,
    subject: str,
    key: str = "",
) -> None:
    """Raise if ``value`` carries a forbidden key or a local absolute path.

    ``code`` and ``subject`` keep each caller's error contract stable: the
    handoff flow reports ``unsafe_handoff_payload`` with its own wording, other
    writers report their own, and all of them check the same list.
    """
    if key and SENSITIVE_KEY_PATTERN.search(key):
        raise ValidationError(code, f"{subject} field '{key}' is not allowed")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            reject_unsafe_durable_payload(
                child_value, code=code, subject=subject, key=str(child_key)
            )
    elif isinstance(value, list):
        for child_value in value:
            reject_unsafe_durable_payload(child_value, code=code, subject=subject, key=key)
    elif isinstance(value, str) and ABSOLUTE_PATH_PATTERN.search(value):
        raise ValidationError(code, f"{subject} must not contain absolute local filesystem paths")
