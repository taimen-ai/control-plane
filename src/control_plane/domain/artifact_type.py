"""Artifact type definitions and the check of an artifact against one (CP-ADR-0072 §6).

An artifact type is catalog data of a tenant: a key, a JSON Schema for the
artifact's ``metadata``, the media types its content may have and a size
ceiling. Core knows nothing else about a type — there is no code per type —
and an artifact whose ``type`` is not registered is accepted as before.
"""

import re
from dataclasses import dataclass
from typing import Any

import jsonschema

from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    guard_json_document,
    validate_against_schema,
    validate_json_schema_document,
)

MAX_METADATA_SCHEMA_BYTES = 16 * 1024
MAX_MEDIA_TYPES = 50

# ``type/subtype``, ``type/*`` or ``*/*``; RFC 6838 restricted-name characters.
_TOKEN = r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
_MEDIA_PATTERN = re.compile(rf"^(?:\*/\*|{_TOKEN}/(?:\*|{_TOKEN}))$")


def _invalid(message: str, field: str, **details: Any) -> ValidationError:
    return ValidationError("invalid_artifact_type", message, details={"field": field, **details})


@dataclass(frozen=True)
class ArtifactTypeDefinition:
    metadata_schema: dict[str, Any]
    media_types: list[str]
    max_bytes: int


def normalize_media_type(value: str) -> str:
    """``Text/Markdown; charset=utf-8`` -> ``text/markdown``."""
    return value.split(";", 1)[0].strip().lower()


def validate_artifact_type_definition(
    *,
    metadata_schema: Any,
    media_types: Any,
    max_bytes: int | None,
    global_max_bytes: int,
) -> ArtifactTypeDefinition:
    """Everything a version fixes, checked once at publication.

    Every failure is ``invalid_artifact_type`` with ``details.field`` — the
    generic JSON Schema codes are folded in, so a package installer has one
    code to react to.
    """
    schema = metadata_schema if metadata_schema is not None else {}
    try:
        guard_json_document(schema, label="metadataSchema", max_bytes=MAX_METADATA_SCHEMA_BYTES)
        validate_json_schema_document(schema, field_name="metadataSchema")
    except ValidationError as exc:
        raise _invalid(
            f"metadataSchema is not usable: {exc.message}",
            "metadataSchema",
            reason=exc.code,
            cause={k: v for k, v in exc.details.items() if k != "field"},
        ) from exc

    if not isinstance(media_types, list) or not media_types:
        raise _invalid("mediaTypes must be a non-empty list", "mediaTypes")
    if len(media_types) > MAX_MEDIA_TYPES:
        raise _invalid(
            f"mediaTypes may list at most {MAX_MEDIA_TYPES} entries",
            "mediaTypes",
            maxItems=MAX_MEDIA_TYPES,
        )
    normalized: list[str] = []
    for index, item in enumerate(media_types):
        value = item.strip().lower() if isinstance(item, str) else None
        if value is None or not is_media_pattern(value):
            raise _invalid(
                "mediaTypes entries are 'type/subtype', 'type/*' or '*/*'",
                f"mediaTypes[{index}]",
                value=str(item)[:200],
            )
        if value not in normalized:
            normalized.append(value)

    limit = global_max_bytes if max_bytes is None else max_bytes
    if limit < 1 or limit > global_max_bytes:
        raise _invalid(
            f"maxBytes must be between 1 and {global_max_bytes}",
            "maxBytes",
            maxBytes=global_max_bytes,
        )
    return ArtifactTypeDefinition(metadata_schema=schema, media_types=normalized, max_bytes=limit)


def is_media_pattern(value: str) -> bool:
    """``type/subtype``, ``type/*`` or ``*/*`` in lower case."""
    return bool(_MEDIA_PATTERN.match(value))


def pattern_covered(patterns: list[str], pattern: str) -> bool:
    """Does every media type ``pattern`` admits also pass ``patterns``?

    What narrowing a type's media types means: ``text/markdown`` narrows
    ``text/*``, ``text/*`` does not narrow ``text/markdown``.
    """
    if pattern == "*/*":
        return "*/*" in patterns
    if pattern.endswith("/*"):
        return "*/*" in patterns or pattern in patterns
    return media_type_allowed(patterns, pattern)


def media_type_allowed(patterns: list[str], media_type: str) -> bool:
    value = normalize_media_type(media_type)
    major = value.split("/", 1)[0]
    return any(p == "*/*" or p == value or p == f"{major}/*" for p in patterns)


def check_artifact_against_type(
    definition: ArtifactTypeDefinition,
    *,
    key: str,
    version: int,
    metadata: dict[str, Any],
    media_type: str | None,
    size_bytes: int | None,
) -> None:
    """An artifact of a registered type: metadata, media type, size.

    ``media_type`` and ``size_bytes`` are those of stored content; an artifact
    without it (a reference or small JSON) is checked on its metadata only.
    """
    validate_against_schema(
        definition.metadata_schema,
        metadata,
        code="invalid_artifact_metadata",
        field_name="metadata",
    )
    ref = {"artifactType": key, "artifactTypeVersion": version}
    if media_type is not None and not media_type_allowed(definition.media_types, media_type):
        raise ValidationError(
            "media_type_not_allowed",
            f"media type {normalize_media_type(media_type)!r} is not allowed for {key!r}",
            details={
                **ref,
                "mediaType": normalize_media_type(media_type),
                "allowed": definition.media_types,
            },
        )
    if size_bytes is not None and size_bytes > definition.max_bytes:
        raise ValidationError(
            "artifact_too_large",
            f"content of {size_bytes} bytes exceeds {definition.max_bytes} for {key!r}",
            details={**ref, "sizeBytes": size_bytes, "maxBytes": definition.max_bytes},
        )


def check_output_value(
    definition: ArtifactTypeDefinition,
    *,
    key: str,
    version: int,
    value: Any,
    media_type: str,
    size_bytes: int,
    narrowed_to: tuple[str, ...] | None = None,
) -> None:
    """A value a skill handed in as a typed output of its task (CP-ADR-0072 amendment).

    The value is the artifact: it is checked against the type's
    ``metadataSchema`` — the only JSON Schema a type has — and its encoding
    against the media types (the type's, then the output's own narrowing)
    and the size ceiling. Formats stay annotations, as for ``metadata``.
    """
    ref = {"artifactType": key, "artifactTypeVersion": version}
    schema = definition.metadata_schema
    if schema:
        validator = jsonschema.Draft202012Validator(schema)
        try:
            found = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
        except Exception as exc:  # an unresolvable $ref is a failed check, not a 500
            errors = [{"path": "/", "message": f"schema could not be evaluated: {exc}"[:300]}]
        else:
            errors = [
                {
                    "path": "/" + "/".join(str(part) for part in error.absolute_path),
                    "message": error.message[:500],
                }
                for error in found
            ][:20]
        if errors:
            raise ValidationError(
                "invalid_output_value",
                f"the value does not match the schema of artifact type {key!r}",
                details={**ref, "errors": errors},
            )
    for allowed in (definition.media_types, list(narrowed_to or ())):
        if allowed and not media_type_allowed(allowed, media_type):
            raise ValidationError(
                "media_type_not_allowed",
                f"media type {media_type!r} is not allowed for {key!r}",
                details={**ref, "mediaType": media_type, "allowed": allowed},
            )
    if size_bytes > definition.max_bytes:
        raise ValidationError(
            "artifact_too_large",
            f"content of {size_bytes} bytes exceeds {definition.max_bytes} for {key!r}",
            details={**ref, "sizeBytes": size_bytes, "maxBytes": definition.max_bytes},
        )
