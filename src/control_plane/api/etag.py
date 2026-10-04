"""ETag / If-Match handling for optimistic concurrency.

ETags look like ``"<entity>-<version>"`` (e.g. ``"task-3"``, ``"workspace-2"``).
"""

import re

from control_plane.domain.errors import DomainError


class PreconditionRequiredError(DomainError):
    http_status = 428

    def __init__(self, entity: str = "task") -> None:
        super().__init__(
            "if_match_required",
            f'This operation requires an If-Match header (format: "{entity}-<version>")',
        )


class BadIfMatchError(DomainError):
    http_status = 400

    def __init__(self, header_value: str, entity: str = "task") -> None:
        super().__init__(
            "invalid_if_match",
            f'If-Match must look like "{entity}-<version>"',
            details={"ifMatch": header_value},
        )


def format_etag(entity: str, version: int) -> str:
    return f'"{entity}-{version}"'


def parse_if_match(header_value: str | None, entity: str = "task") -> int:
    if header_value is None or not header_value.strip():
        raise PreconditionRequiredError(entity)
    match = re.match(rf'^\s*(?:W/)?"?{re.escape(entity)}-(\d+)"?\s*$', header_value)
    if match is None:
        raise BadIfMatchError(header_value, entity)
    return int(match.group(1))


def format_task_etag(version: int) -> str:
    return format_etag("task", version)


def none_match(header_value: str | None, tag: str) -> bool:
    """Whether ``If-None-Match`` names ``tag``, quoted or not, weak or not."""
    if not header_value:
        return False
    bare = tag.strip('"')
    candidates = (value.strip().removeprefix("W/").strip('"') for value in header_value.split(","))
    return bare in candidates
