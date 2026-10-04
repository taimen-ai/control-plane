"""JSON structured logging with request-id propagation and secret redaction."""

import json
import logging
import re
import sys
import urllib.parse
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from control_plane.domain.redaction import LOG_SENSITIVE_KEYS

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
# ``X-Run-Id`` trace correlator (ADR-0039), logged as ``run_id``. Distinct from
# the execution Run entity, whose identifier is logged as ``runId``/``run``.
trace_run_id_var: ContextVar[str | None] = ContextVar("trace_run_id", default=None)

_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)

# The OAuth callback carries ``code`` and ``state`` in its query string
# (CP-ADR-0079 §14): the access log keeps the path and loses the query.
_REDACTED_QUERY_PATH = re.compile(r"connections:callback(?![a-z0-9_-])")


def redact(value: object, key: str | None = None) -> object:
    """Recursively redact obviously sensitive values in log extras."""
    if key is not None and key.lower() in LOG_SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _normalized_path(path: str) -> str:
    """The path as the redaction compares it: unquoted and lower-cased.

    uvicorn quotes the path it logs, so the colon arrives as ``%3A``.
    """
    return urllib.parse.unquote(path).lower()


def redact_query(path: str) -> str:
    """``path`` with the query string of a redacted path replaced by ``[redacted]``.

    Any path that names the callback loses its query, not only the route
    itself: a variant the router answers otherwise (case, a trailing or
    doubled slash and Starlette's ``307``, ``/./``, ``;x``, ``%20``) is
    logged with its query all the same, and a ``code`` in it is still live.
    """
    base, question, _query = path.partition("?")
    if question and _REDACTED_QUERY_PATH.search(_normalized_path(base)):
        return f"{base}?[redacted]"
    return path


class AccessLogQueryFilter(logging.Filter):
    """Drop the query string of the OAuth callback from ``uvicorn.access`` records.

    uvicorn logs ``(client, method, path with query, http version, status)``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            redacted = redact_query(args[2])
            if redacted != args[2]:
                record.args = (*args[:2], redacted, *args[3:])
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        trace_run_id = trace_run_id_var.get()
        if trace_run_id:
            payload["run_id"] = trace_run_id
        for attr, value in record.__dict__.items():
            if attr not in _STD_ATTRS and not attr.startswith("_"):
                payload[attr] = redact(value, attr)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn loggers propagate into the root JSON handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogQueryFilter) for f in access.filters):
        access.addFilter(AccessLogQueryFilter())
