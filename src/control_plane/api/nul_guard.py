"""Reject NUL characters in JSON request bodies (CP-ADR-0083).

PostgreSQL stores neither ``U+0000`` in ``text`` nor ``\\u0000`` in ``jsonb``:
a string with NUL that reaches the database fails the statement and the caller
gets ``500 internal_error``. One check at the API boundary covers every route,
nested JSON fields included, instead of a check per route or per schema field:
a JSON body with NUL in any string -- a value or an object key -- is
``422 validation_error`` with ``details.errors[{path, code: "nul_character"}]``
(JSON Pointer into the body, never the value).

The check runs before routing, authentication and the database. A body whose
raw bytes contain neither the ``\\u0000`` escape nor a NUL byte is passed on
without parsing; JSON cannot carry NUL any other way (a raw control character
inside a string is invalid JSON). A body that is not JSON at all is left to the
route, which answers with its own contract error.

Exempt paths take bytes that are not stored in PostgreSQL: the artifact upload
streams any media type, ``application/json`` included, into the content store.

Query and path parameters are checked by :func:`reject_nul_in_parameters`, a
router-level dependency next to the strict query check (CP-ADR-0058): path
parameter names are known only after routing. It answers with the same error,
the path being ``path.<name>`` or ``query.<name>``.
"""

import json
from collections.abc import Iterable
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from control_plane.api.errors import BodyTooLargeError, error_response
from control_plane.domain.errors import ValidationError
from control_plane.domain.principal_profile import pointer

NUL_CHARACTER_CODE = "nul_character"
NUL_CHARACTER_MESSAGE = "Request body contains the NUL character (U+0000) in a string"
NUL_PARAMETER_MESSAGE = "Request parameter contains the NUL character (U+0000)"
_MAX_ERRORS = 20
_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_NUL_MARKERS = (b"\\u0000", b"\x00")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


def _is_json_body(content_type: str | None) -> bool:
    # A missing content type counts: routes that read the raw body parse it as
    # JSON regardless of the header (PATCH /principals/{id}).
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def nul_paths(document: Any, *, limit: int = _MAX_ERRORS) -> list[str]:
    """JSON Pointers (``/`` for the root) to the strings of ``document`` with NUL.

    A member name has no path of its own: a key with NUL is reported at the
    object it stands in, and is not echoed. Iterative, so a deeply nested body
    the parser accepted cannot exhaust the stack here.
    """
    found: list[str] = []
    stack: list[tuple[tuple[str | int, ...], Any]] = [((), document)]
    while stack and len(found) < limit:
        at, value = stack.pop()
        if isinstance(value, str):
            if "\x00" in value:
                found.append(pointer(at))
        elif isinstance(value, dict):
            if any("\x00" in key for key in value):
                found.append(pointer(at))
            stack.extend(
                ((*at, key), item) for key, item in reversed(value.items()) if "\x00" not in key
            )
        elif isinstance(value, list):
            stack.extend(((*at, i), value[i]) for i in reversed(range(len(value))))
    return found


def _replay(messages: Iterable[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay_receive() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay_receive


class NulCharacterGuardMiddleware:
    """Answer ``422 validation_error`` for a JSON body with NUL in a string."""

    def __init__(self, app: ASGIApp, exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in _METHODS_WITH_BODY
            or scope.get("path", "") in self.exempt_paths
            or not _is_json_body(_header(scope, b"content-type"))
        ):
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        chunks: list[bytes] = []
        try:
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
        except BodyTooLargeError:
            # Raised by the size limit outside the exception-handler stack.
            response = error_response(
                413, "request_too_large", "Request body exceeds the size limit"
            )
            await response(scope, receive, send)
            return

        # The joined copy lives only inside _find_nul, not while the route runs.
        paths = _find_nul(chunks)
        del chunks
        if paths:
            response = error_response(
                422,
                "validation_error",
                NUL_CHARACTER_MESSAGE,
                details={
                    "errors": [
                        {"path": path, "code": NUL_CHARACTER_CODE, "message": NUL_CHARACTER_MESSAGE}
                        for path in paths
                    ]
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, _replay(messages, receive), send)


def _find_nul(chunks: list[bytes]) -> list[str]:
    raw = b"".join(chunks)
    if not any(marker in raw for marker in _NUL_MARKERS):
        return []
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        # Not JSON: the route rejects it with its own contract error.
        return []
    return nul_paths(document)


def nul_parameter_paths(connection: HTTPConnection, *, limit: int = _MAX_ERRORS) -> list[str]:
    """``path.<name>`` and ``query.<name>`` of the parameters with NUL, in URL order.

    A repeated query parameter is named once; a name with NUL is reported as
    ``query`` and not echoed, like a member name in the body.
    """
    found: list[str] = []
    for name, value in connection.path_params.items():
        if isinstance(value, str) and "\x00" in value:
            found.append(f"path.{name}")
    for name, value in connection.query_params.multi_items():
        at = "query" if "\x00" in name else f"query.{name}"
        if (at == "query" or "\x00" in value) and at not in found:
            found.append(at)
    return found[:limit]


def reject_nul_in_parameters(connection: HTTPConnection) -> None:
    """Router-level dependency: ``422`` for a path or query parameter with NUL.

    Router dependencies are solved before the route's own ones, so this runs
    before authentication and before any parameter reaches the database.
    """
    # The WebSocket event stream answers with close codes, not the envelope.
    if connection.scope["type"] != "http":
        return
    paths = nul_parameter_paths(connection)
    if paths:
        raise ValidationError(
            "validation_error",
            NUL_PARAMETER_MESSAGE,
            details={
                "errors": [
                    {"path": path, "code": NUL_CHARACTER_CODE, "message": NUL_PARAMETER_MESSAGE}
                    for path in paths
                ]
            },
        )
