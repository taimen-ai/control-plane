"""Typed Control Plane client errors.

Domain semantics surface as distinct exception types — a harness reacts to
``StaleClaimError`` structurally, not by parsing message strings. Every error
carries the machine-readable ``code`` from the server envelope.
"""

from typing import Any


class ControlPlaneError(Exception):
    """Base error: transport reached the server and it answered with an error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class TransportError(ControlPlaneError):
    """The request may or may not have been executed (network failure).

    Safe to retry only with the same Idempotency-Key — the client does that
    automatically for idempotent commands.
    """

    def __init__(self, message: str) -> None:
        super().__init__("transport_error", message, status=0)


class AuthenticationError(ControlPlaneError):
    pass


class PermissionDeniedError(ControlPlaneError):
    pass


class NotFoundError(ControlPlaneError):
    pass


class ValidationError(ControlPlaneError):
    pass


class ConflictError(ControlPlaneError):
    pass


class StaleClaimError(ConflictError):
    """Fencing rejected the operation: this process no longer owns the task."""


class ClaimConflictError(ConflictError):
    """The task is already claimed by a live owner."""


class TaskNotReadyError(ConflictError):
    """Blocking dependencies are not completed."""


class ApprovalRequiredError(ConflictError):
    """A pending gate approval holds the task."""


class NotEligibleError(PermissionDeniedError):
    """The principal does not satisfy the task's organizational requirements."""


class SessionExpiredError(ConflictError):
    pass


class VersionConflictError(ConflictError):
    """Optimistic concurrency (If-Match) mismatch."""


class IdempotencyConflictError(ConflictError):
    pass


class BudgetExceededError(ConflictError):
    """The run exhausted its action/duration budget."""


class SkillUnavailableError(ConflictError):
    pass


class RunNotActiveError(ConflictError):
    pass


class CancelledError(ConflictError):
    pass


_CODE_MAP: dict[str, type[ControlPlaneError]] = {
    "stale_claim": StaleClaimError,
    "task_already_claimed": ClaimConflictError,
    "task_claimed": ClaimConflictError,
    "claim_not_expired": ClaimConflictError,
    "claim_conflict": ClaimConflictError,
    "task_not_ready": TaskNotReadyError,
    "approval_required": ApprovalRequiredError,
    "not_eligible": NotEligibleError,
    "session_expired": SessionExpiredError,
    "session_not_active": SessionExpiredError,
    "version_conflict": VersionConflictError,
    "idempotency_key_reused": IdempotencyConflictError,
    "idempotency_in_flight": IdempotencyConflictError,
    "budget_exceeded": BudgetExceededError,
    "skill_unavailable": SkillUnavailableError,
    "run_not_active": RunNotActiveError,
    "task_cancelled": CancelledError,
    "unsupported_protocol_version": ValidationError,
    "invalid_credentials": AuthenticationError,
    "permission_denied": PermissionDeniedError,
    "not_found": NotFoundError,
}

_STATUS_MAP: dict[int, type[ControlPlaneError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    428: ValidationError,
}


def error_from_response(status: int, body: dict[str, Any]) -> ControlPlaneError:
    envelope = body.get("error") or {}
    code = envelope.get("code", "http_error")
    message = envelope.get("message", "Unexpected server error")
    details = envelope.get("details") or {}
    cls = _CODE_MAP.get(code) or _STATUS_MAP.get(status, ControlPlaneError)
    return cls(code, message, status=status, details=details)


#: Answers of a proxy or gateway whose upstream is down or restarting: the
#: Control Plane itself said nothing, so nothing about ownership is known.
UNAVAILABLE_STATUSES = frozenset({502, 503, 504})

#: Codes of a failure to reach a service the command depends on, raised before
#: any answer exists (status 0): the IAM exchange that could not connect.
UNREACHABLE_CODES = frozenset({"iam_unreachable"})


def is_transient(exc: BaseException) -> bool:
    """Is ``exc`` a failure to reach the Control Plane rather than its answer?

    A transport failure, a 502/503/504 of the proxy in front of it, or a 5xx
    without the server's error envelope (``http_error``) — a restart of the
    core looks like this for a few seconds. Such a failure is no verdict on a
    lease or a command and is worth repeating; an answer of the core itself
    (409, 404, 403, a 500 with its own code) is not.

    The IAM exchange that precedes each command with an IAM credential counts
    the same way. Its 502/503/504 (``IamCredentialError.status``) and a failed
    connection to IAM (``iam_unreachable``, status 0) are both a restarting
    IAM: a ``HeartbeatRunner`` must outlive that within its outage budget
    rather than report lost ownership, and treating the refused connection as
    final while the 503 of the same restart is not would make the outcome
    depend on which half of the restart the beat hit. A verdict of IAM — 401,
    403, 400, an empty or malformed exchange, a missing local credential — is
    final.
    """
    if isinstance(exc, TransportError):
        return True
    if not isinstance(exc, ControlPlaneError):
        return False
    if exc.code in UNREACHABLE_CODES:
        return True
    return exc.status in UNAVAILABLE_STATUSES or (exc.code == "http_error" and exc.status >= 500)
