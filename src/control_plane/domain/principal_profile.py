"""The body of ``PATCH /principals/{id}`` and its field errors (CP-ADR-0082 §1).

The route takes the body itself instead of letting the framework parse it:
the order of checks is part of the contract (§1.4) — credential-shaped
material anywhere in the body is refused before the shape is looked at, so a
value never reaches the database, the journal or an echo of a schema error.

Both refusals carry ``details.errors`` — one platform shape of field errors
(CP-ADR-0081 §4.3): ``path`` is a JSON Pointer into the request body; no
element ever contains the value of the field.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from control_plane.domain.errors import ValidationError
from control_plane.domain.redaction import secret_material

PROFILE_FIELDS: tuple[str, ...] = ("jobTitle", "email", "phone", "note")

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "jobTitle": {"type": "string", "minLength": 1, "maxLength": 200},
        "email": {"type": "string", "format": "email", "maxLength": 254},
        "phone": {"type": "string", "minLength": 1, "maxLength": 50},
        "note": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}

UPDATE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "displayName": {"type": "string", "minLength": 1, "maxLength": 200},
        "profile": PROFILE_SCHEMA,
    },
}

_EMAIL_MAX_LENGTH = PROFILE_SCHEMA["properties"]["email"]["maxLength"]
_FORMATS = FormatChecker(formats=())


@_FORMATS.checks("email")
def _is_email(value: object) -> bool:
    """One "@", something on both sides, a dot inside the domain, no whitespace.

    A shape check for a contact field, not a deliverability check. It is a
    linear scan, not a pattern: jsonschema checks ``format`` even past
    ``maxLength``, and a backtracking match on a long value without a match
    takes quadratic time. A value past the limit is left to ``maxLength``.
    """
    if not isinstance(value, str) or len(value) > _EMAIL_MAX_LENGTH:
        return True
    local, at, domain = value.rpartition("@")
    return (
        at == "@"
        and local != ""
        and "@" not in local
        and "." in domain[1:-1]
        and not any(char.isspace() for char in value)
    )


_VALIDATOR = Draft202012Validator(UPDATE_SCHEMA, format_checker=_FORMATS)

# What a keyword means, said without the value that broke it.
_MESSAGES: dict[str, str] = {
    "type": "has the wrong type",
    "minLength": "is too short",
    "maxLength": "is too long",
    "format": "is not a valid email address",
    "additionalProperties": "is not a known field",
}


@dataclass(frozen=True)
class PrincipalUpdate:
    """A body that passed both checks; an absent field is ``None``."""

    display_name: str | None
    profile: dict[str, str] | None


def pointer(parts: Iterator[Any] | list[Any] | tuple[Any, ...]) -> str:
    """A JSON Pointer (RFC 6901) of a path into the body; ``/`` for the root."""
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"


def _secret_errors(body: Any) -> Iterator[dict[str, str]]:
    # Depth first in document order, without recursion: a body nested
    # deeper than the interpreter's stack is still only a body.
    stack: list[tuple[Any, tuple[Any, ...]]] = [(body, ())]
    while stack:
        node, at = stack.pop()
        if isinstance(node, str):
            kind = secret_material(node)
            if kind is not None:
                yield {"path": pointer(at), "match": kind}
        elif isinstance(node, dict):
            children: list[tuple[Any, tuple[Any, ...]]] = []
            for key, value in node.items():
                # A member name has no path of its own: the object it stands in.
                kind = secret_material(str(key))
                if kind is not None:
                    yield {"path": pointer(at), "match": kind}
                children.append((value, (*at, key)))
            stack.extend(reversed(children))
        elif isinstance(node, list):
            stack.extend(reversed([(value, (*at, i)) for i, value in enumerate(node)]))


def reject_secret_material(body: Any) -> None:
    """``422 secret_material_rejected`` for credential-shaped material in any string."""
    errors: list[dict[str, str]] = []
    for error in _secret_errors(body):
        if error not in errors:
            errors.append(error)
    if errors:
        errors.sort(key=lambda e: e["path"])
        raise ValidationError(
            "secret_material_rejected",
            "The request must not contain credentials; reference a secret store instead",
            details={"errors": errors},
        )


def _shape_errors(body: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for error in _VALIDATOR.iter_errors(body):
        keyword = str(error.validator)
        at = tuple(error.absolute_path)
        if keyword == "additionalProperties" and isinstance(error.instance, dict):
            known = error.schema.get("properties", {})
            for name in error.instance:
                if name not in known:
                    errors.append(
                        {
                            "path": pointer((*at, name)),
                            "code": keyword,
                            "message": _MESSAGES[keyword],
                        }
                    )
            continue
        errors.append(
            {
                "path": pointer(at),
                "code": keyword,
                "message": _MESSAGES.get(keyword, "is not valid"),
            }
        )
    return sorted(errors, key=lambda e: (e["path"], e["code"]))


def parse_update(body: Any) -> PrincipalUpdate:
    """The checked body of ``PATCH /principals/{id}``, or its refusal (§1.4 steps 4-5).

    ``displayName`` is trimmed first, as ``POST /principals`` does: a name of
    blanks is an empty name.
    """
    reject_secret_material(body)
    if isinstance(body, dict) and isinstance(body.get("displayName"), str):
        body = {**body, "displayName": body["displayName"].strip()}
    errors = _shape_errors(body)
    if errors:
        raise ValidationError(
            "validation_error",
            "The request body does not match PrincipalUpdateRequest",
            details={"errors": errors},
        )
    assert isinstance(body, dict)
    profile = body.get("profile")
    return PrincipalUpdate(
        display_name=body.get("displayName"),
        profile=dict(profile) if profile is not None else None,
    )


def changed_fields(
    *,
    display_name: str,
    profile: dict[str, Any],
    update: PrincipalUpdate,
) -> list[str]:
    """Names of what ``update`` changes — ``displayName``, ``profile.<field>`` — no values."""
    changes: list[str] = []
    if update.display_name is not None and update.display_name != display_name:
        changes.append("displayName")
    if update.profile is not None:
        for name in sorted(set(profile) | set(update.profile)):
            if profile.get(name) != update.profile.get(name):
                changes.append(f"profile.{name}")
    return changes
