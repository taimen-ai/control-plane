"""Reject unknown query parameters instead of silently ignoring them.

FastAPI drops query parameters a route does not declare. For a narrowing
parameter that turns a refusal into a success: the client believes it filtered
the page (``assignedToMe``, a project filter), while a server that does not
know the parameter returns the full selection. That is the 2026-08-14 incident:
the worker's client already filtered its queue, the deployed server did not,
and the agent claimed an unrelated epic.

The check covers the whole ``/api/v1`` rather than a list of "narrowing"
parameters: such a list is maintained by hand, and a new parameter forgotten in
it would reproduce the same incident. Allowed names come from the parameters
the route itself declares (its dependencies included), so a new ``Query`` is
accepted without separate registration. An unknown parameter yields
``400 invalid_request`` naming it in ``details.errors[].loc`` as
``query.<name>``.

One route is exempt by design, and says so with :func:`open_query`: the OAuth
callback (CP-ADR-0079 §6). Its parameters are set by the provider, not by a
client of this API — the account parameter is the connection type's, and a
provider may add its own — and a ``400`` there would quote the query (the
authorization code) back and lose the provider's answer.
"""

from collections.abc import Callable
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.requests import HTTPConnection

UNKNOWN_QUERY_PARAMETER_MESSAGE = (
    "Unknown query parameter: this server does not support it and will not ignore it"
)


# Computed once per route and kept on the route itself (APIRoute is unhashable).
_CACHE_ATTR = "_cp_declared_query_names"
_OPEN_QUERY_ATTR = "_cp_open_query"


def open_query[E: Callable[..., Any]](endpoint: E) -> E:
    """Mark an endpoint whose query parameters are set by a third party."""
    setattr(endpoint, _OPEN_QUERY_ATTR, True)
    return endpoint


def _query_aliases(dependant: Dependant) -> set[str]:
    aliases = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        aliases |= _query_aliases(sub)
    return aliases


def _declared_query_names(route: APIRoute) -> frozenset[str]:
    names: frozenset[str] | None = getattr(route, _CACHE_ATTR, None)
    if names is None:
        names = frozenset(_query_aliases(route.dependant))
        setattr(route, _CACHE_ATTR, names)
    return names


def reject_unknown_query_params(connection: HTTPConnection) -> None:
    # The WebSocket event stream answers with close codes, not the error
    # envelope, and its only parameter is a cursor that does not narrow output.
    if connection.scope["type"] != "http":
        return
    route = connection.scope.get("route")
    if not isinstance(route, APIRoute) or getattr(route.endpoint, _OPEN_QUERY_ATTR, False):
        return
    allowed = _declared_query_names(route)
    unknown = sorted({name for name in connection.query_params if name not in allowed})
    if unknown:
        raise RequestValidationError(
            [
                {
                    "type": "unknown_query_parameter",
                    "loc": ("query", name),
                    "msg": UNKNOWN_QUERY_PARAMETER_MESSAGE,
                    "input": connection.query_params.get(name),
                }
                for name in unknown
            ]
        )
