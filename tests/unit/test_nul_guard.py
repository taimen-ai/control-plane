"""The NUL guard on its own: paths, parsing edge cases and the ASGI contract (CP-ADR-0083)."""

import asyncio
import inspect
import json
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from control_plane.api.errors import BodyTooLargeError
from control_plane.api.nul_guard import (
    NulCharacterGuardMiddleware,
    nul_parameter_paths,
    nul_paths,
    reject_nul_in_parameters,
)
from control_plane.domain.errors import ValidationError

NUL = "\x00"


@pytest.mark.parametrize(
    ("document", "paths"),
    [
        ({}, []),
        ([], []),
        (None, []),
        ("", []),
        (0, []),
        (True, []),
        ({"a": None, "b": [1, 2.5, False]}, []),
        (NUL, ["/"]),
        ([NUL], ["/0"]),
        ({"a": {"b": [None, {"c": f"x{NUL}"}]}}, ["/a/b/1/c"]),
        ({"a~b/c": NUL}, ["/a~0b~1c"]),
        ({f"k{NUL}": "v"}, ["/"]),
        ({"o": {f"k{NUL}": NUL, "ok": NUL}}, ["/o", "/o/ok"]),
        ({"z": NUL, "a": NUL}, ["/z", "/a"]),  # document order
    ],
)
def test_nul_paths(document: Any, paths: list[str]) -> None:
    assert nul_paths(document) == paths


def test_nul_paths_is_bounded() -> None:
    assert len(nul_paths([NUL] * 100)) == 20


def test_nul_paths_deep_nesting_does_not_recurse() -> None:
    document: Any = NUL
    for _ in range(5000):
        document = [document]
    assert nul_paths(document) == ["/" + "/".join(["0"] * 5000)]


class _Recorder:
    def __init__(self) -> None:
        self.called = False
        self.body = b""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _scope(
    method: str = "POST", path: str = "/api/v1/tasks", content_type: str | None = "application/json"
) -> Scope:
    headers = [(b"content-type", content_type.encode())] if content_type else []
    return {"type": "http", "method": method, "path": path, "headers": headers}


def _receive(chunks: list[bytes], *, raise_after: int | None = None) -> Receive:
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    count = 0

    async def receive() -> Message:
        nonlocal count
        if raise_after is not None and count >= raise_after:
            raise BodyTooLargeError()
        count += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    return receive


async def _call(
    scope: Scope, receive: Receive, exempt: tuple[str, ...] = ()
) -> tuple[_Recorder, int, dict[str, Any] | None]:
    inner = _Recorder()
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await NulCharacterGuardMiddleware(inner, exempt_paths=exempt)(scope, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return inner, sent[0]["status"], (json.loads(body) if body else None)


async def test_rejects_nul_split_across_chunks() -> None:
    inner, status, body = await _call(_scope(), _receive([b'{"title": "a\\u00', b'00"}']))
    assert not inner.called
    assert status == 422
    assert body is not None
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["errors"] == [
        {
            "path": "/title",
            "code": "nul_character",
            "message": "Request body contains the NUL character (U+0000) in a string",
        }
    ]


async def test_clean_chunked_body_is_replayed_whole() -> None:
    inner, status, _ = await _call(_scope(), _receive([b'{"title": ', b'"a"}']))
    assert inner.called
    assert status == 204
    assert inner.body == b'{"title": "a"}'


async def test_joined_body_is_not_kept_while_the_route_runs() -> None:
    # The buffered chunks are replayed; no joined copy of the whole body stays
    # alive in the middleware's frame while the inner app executes.
    whole = b'{"title": "' + b"x" * 4096 + b'"}'
    seen: list[str] = []

    class Inspecting(_Recorder):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            frame = inspect.currentframe()
            while frame is not None:
                if frame.f_code.co_name == "__call__" and "self" in frame.f_locals:
                    owner = frame.f_locals["self"]
                    if isinstance(owner, NulCharacterGuardMiddleware):
                        seen.extend(
                            name for name, value in frame.f_locals.items() if value == whole
                        )
                        seen.append("<checked>")
                frame = frame.f_back
            await super().__call__(scope, receive, send)

    inner = Inspecting()

    async def send(message: Message) -> None:
        pass

    chunks = [whole[:10], whole[10:]]
    await NulCharacterGuardMiddleware(inner)(_scope(), _receive(chunks), send)
    assert inner.body == whole
    assert seen == ["<checked>"]


async def test_body_too_large_while_buffering_is_413() -> None:
    inner, status, body = await _call(_scope(), _receive([b"{", b"}"], raise_after=1))
    assert not inner.called
    assert status == 413
    assert body is not None
    assert body["error"]["code"] == "request_too_large"


async def test_disconnect_while_buffering_is_passed_on() -> None:
    async def receive() -> Message:
        return {"type": "http.disconnect"}

    inner, status, _ = await _call(_scope(), receive)
    assert inner.called
    assert status == 204


@pytest.mark.parametrize(
    "scope",
    [
        _scope(method="GET"),
        _scope(content_type="text/plain"),
        _scope(content_type="multipart/form-data; boundary=x"),
        _scope(path="/api/v1/artifact-contents"),
    ],
)
async def test_not_checked(scope: Scope) -> None:
    inner, status, _ = await _call(
        scope, _receive([b'{"a": "\\u0000"}']), exempt=("/api/v1/artifact-contents",)
    )
    assert inner.called
    assert status == 204
    assert inner.body == b'{"a": "\\u0000"}'


@pytest.mark.parametrize(
    "content_type", [None, "application/json; charset=utf-8", "application/problem+json"]
)
async def test_checked_content_types(content_type: str | None) -> None:
    inner, status, _ = await _call(_scope(content_type=content_type), _receive([b'["\\u0000"]']))
    assert not inner.called
    assert status == 422


@pytest.mark.parametrize(
    "raw",
    [
        b'"\\\\u0000"',  # an escaped backslash, then "u0000": no NUL
        b'{"a": "\\u0000"',  # not JSON
        b"\xff\xfe\\u0000",  # not UTF-8
        b"[" * 100_000 + b"]" * 100_000,  # past the parser's nesting limit
    ],
)
async def test_not_a_nul_string_passes_through(raw: bytes) -> None:
    inner, status, _ = await _call(_scope(), _receive([raw]))
    assert inner.called
    assert status == 204


async def test_utf16_json_with_nul_is_rejected() -> None:
    # json.loads (and so the route) accepts UTF-16; the raw NUL byte gets it parsed.
    raw = json.dumps({"t": NUL}).encode("utf-16")
    _, status, body = await _call(_scope(), _receive([raw]))
    assert status == 422
    assert body is not None
    assert body["error"]["details"]["errors"][0]["path"] == "/t"


async def test_concurrent_requests_are_independent() -> None:
    results = await asyncio.gather(
        *(
            _call(_scope(), _receive([b'{"a": "\\u0000"}' if i % 2 else b'{"a": "b"}']))
            for i in range(10)
        )
    )
    assert [status for _, status, _ in results] == [204, 422] * 5


# -- query and path parameters ---------------------------------------------------


def _connection(query: bytes = b"", path_params: dict[str, Any] | None = None) -> Any:
    from starlette.requests import HTTPConnection

    return HTTPConnection(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/x",
            "query_string": query,
            "headers": [],
            "path_params": path_params or {},
        }
    )


@pytest.mark.parametrize(
    ("query", "path_params", "paths"),
    [
        (b"", {}, []),
        (b"a=&b", {}, []),  # empty values
        (b"a=%5Cu0000", {}, []),  # an escaped backslash is not NUL
        (b"a=%00", {}, ["query.a"]),
        (b"a=x%00y&b=ok&c=%00", {}, ["query.a", "query.c"]),
        (b"a=%00&a=%00&a=ok", {}, ["query.a"]),  # repeated: named once
        (b"a%00=1", {}, ["query"]),  # the name is not echoed
        (b"a%00=1&b%00=2", {}, ["query"]),
        (b"", {"ref": "T\x00"}, ["path.ref"]),
        (b"", {"ref": "ok", "n": 3}, []),  # converted, non-string parameter
        (b"q=%00", {"ref": "\x00"}, ["path.ref", "query.q"]),  # URL order
    ],
)
def test_nul_parameter_paths(query: bytes, path_params: dict[str, Any], paths: list[str]) -> None:
    assert nul_parameter_paths(_connection(query, path_params)) == paths


def test_nul_parameter_paths_is_bounded() -> None:
    query = "&".join(f"p{i}=%00" for i in range(100)).encode()
    assert len(nul_parameter_paths(_connection(query))) == 20


def test_reject_nul_in_parameters_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as caught:
        reject_nul_in_parameters(_connection(b"status=secret%00"))
    assert caught.value.http_status == 422
    assert caught.value.code == "validation_error"
    assert caught.value.details == {
        "errors": [
            {
                "path": "query.status",
                "code": "nul_character",
                "message": "Request parameter contains the NUL character (U+0000)",
            }
        ]
    }
    assert "secret" not in json.dumps(caught.value.details)


def test_reject_nul_in_parameters_passes_clean_and_websocket() -> None:
    reject_nul_in_parameters(_connection(b"status=open"))
    from starlette.requests import HTTPConnection

    websocket = HTTPConnection(
        {"type": "websocket", "path": "/api/v1/events/ws", "query_string": b"c=%00", "headers": []}
    )
    reject_nul_in_parameters(websocket)
