"""Official async Control Plane client (control-harness/2).

Encapsulates what every harness would otherwise reimplement: auth headers,
idempotency keys with transport-retry, ETag/If-Match, event cursors with
catch-up iteration, lease heartbeats and typed domain errors. Domain
semantics stay visible — a stale claim is ``StaleClaimError``, not a generic
exception.
"""

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from control_plane_client.credentials import CredentialProvider, StaticCredential
from control_plane_client.errors import (
    ControlPlaneError,
    TransportError,
    error_from_response,
    is_transient,
)

PROTOCOL_VERSION = "2"
_RETRY_BACKOFF = 0.5
_RETRY_BACKOFF_MAX = 5.0
#: How long, in pauses between attempts, a repeatable request is retried by
#: default: 0.5 + 1.0 s, three attempts. A daemon that must outlive a restart
#: of the core asks for more (``retry_window``).
DEFAULT_RETRY_WINDOW = 1.5
# Artifact content travels in chunks of this size, both ways: neither an upload
# nor a download is ever held in memory whole (CP-ADR-0072 §2, §5).
_CONTENT_CHUNK = 1024 * 1024

Json = dict[str, Any]
_UNSET: Any = object()


def _idempotency(key: str | None) -> dict[str, str] | None:
    """Header for a caller-chosen Idempotency-Key.

    A caller that derives the key from its own state (task id + attempt) makes
    a replay after a crash the same business command; without one the client
    generates a key per call, which protects only the in-process retry.
    """
    return {"Idempotency-Key": key} if key else None


def _package_version() -> str:
    try:
        return importlib_metadata.version("control-plane-client")
    except importlib_metadata.PackageNotFoundError:  # pragma: no cover - source-only use
        return "0+unknown"


#: Statuses after which a skill invocation no longer changes (ADR-0056 §2).
SKILL_INVOCATION_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class ControlPlaneClient:
    """Async HTTP client for one principal against one Control Plane server."""

    def __init__(
        self,
        server_url: str,
        api_key: str | CredentialProvider,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str | None = None,
        retry_window: float = DEFAULT_RETRY_WINDOW,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.retry_window = retry_window
        # A plain string stays a plain string for every existing caller; an IAM
        # identity arrives as a provider because its token outlives neither the
        # session nor, usually, the command after next.
        self._credential: CredentialProvider = (
            StaticCredential(api_key) if isinstance(api_key, str) else api_key
        )
        # A consumer names itself first (``bidops-runner/0.1``); the SDK token
        # follows so server logs still show which client build spoke.
        sdk_agent = f"control-plane-client/{_package_version()}"
        self._http = httpx.AsyncClient(
            base_url=f"{self.server_url}/api/v1",
            headers={"User-Agent": f"{user_agent} {sdk_agent}" if user_agent else sdk_agent},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ControlPlaneClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- transport core --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Json | None = None,
        params: Json | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = False,
    ) -> Json:
        """One logical command. With ``idempotent=True`` the client attaches a
        generated Idempotency-Key and retries with the SAME key: a retried HTTP
        request is never a second business command.

        Retried are the failures that say nothing about the command
        (:func:`is_transient`: the network, a 502/503/504 of a restarting core)
        and only where a repeat cannot become a second command — a GET, or a
        request with an Idempotency-Key. The pauses between attempts grow and
        stop once they would exceed ``retry_window`` seconds.
        """
        request_headers = dict(headers or {})
        if idempotent and "Idempotency-Key" not in request_headers:
            request_headers["Idempotency-Key"] = uuid.uuid4().hex
        repeatable = method == "GET" or "Idempotency-Key" in request_headers
        waited = 0.0
        attempt = 0
        reauthenticated = False
        while True:
            request_headers["Authorization"] = f"Bearer {await self._credential.token()}"
            cause: Exception | None = None
            try:
                response = await self._http.request(
                    method, path, json=json_body, params=params, headers=request_headers
                )
                if (
                    response.status_code == 401
                    and self._credential.refreshable
                    and not reauthenticated
                ):
                    # The credential expired between commands. Exactly one
                    # retry, with the same Idempotency-Key: a re-sent request
                    # must stay the same business command, and a second 401 is
                    # a real denial rather than something to keep retrying.
                    reauthenticated = True
                    await self._credential.refresh()
                    response = await self._http.request(
                        method,
                        path,
                        json=json_body,
                        params=params,
                        headers={
                            **request_headers,
                            "Authorization": f"Bearer {await self._credential.token()}",
                        },
                    )
            except httpx.HTTPError as exc:
                cause = exc
                error: ControlPlaneError = TransportError(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code < 400:
                    if response.status_code == 204 or not response.content:
                        return {}
                    result: Json = response.json()
                    return result
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                error = error_from_response(response.status_code, body)
            delay = min(_RETRY_BACKOFF * (2**attempt), _RETRY_BACKOFF_MAX)
            # After a failure that may have reached the core, the first attempt
            # can still be in flight there: its key answers "in flight" until
            # it ends, and then the stored result.
            retryable = is_transient(error) or (
                attempt > 0 and error.code == "idempotency_in_flight"
            )
            if not repeatable or not retryable or waited + delay > self.retry_window:
                raise error from cause
            await asyncio.sleep(delay)
            waited += delay
            attempt += 1

    async def _send(
        self,
        method: str,
        path: str,
        *,
        content: Callable[[], Any] | None = None,
        params: Json | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """One raw request whose body or answer is bytes, not JSON.

        ``content`` is a factory: the body is produced anew for the single
        re-send after a refreshed credential, since a streamed file cannot be
        replayed. With ``stream=True`` the answer is returned unread and the
        caller closes it. An error answer raises the typed error, as
        :meth:`_request` does.
        """
        refreshed = False
        waited = 0.0
        attempt = 0
        while True:
            request = self._http.build_request(
                method,
                path,
                content=content() if content is not None else None,
                params=params,
                headers={
                    **(headers or {}),
                    "Authorization": f"Bearer {await self._credential.token()}",
                },
            )
            cause: Exception | None = None
            try:
                response = await self._http.send(request, stream=stream)
            except httpx.HTTPError as exc:
                cause = exc
                error: ControlPlaneError = TransportError(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code == 401 and self._credential.refreshable and not refreshed:
                    refreshed = True
                    await response.aclose()
                    await self._credential.refresh()
                    continue
                if response.status_code < 400:
                    return response
                try:
                    await response.aread()
                    body = response.json()
                except (ValueError, httpx.HTTPError):
                    body = {}
                finally:
                    await response.aclose()
                error = error_from_response(response.status_code, body)
            # A download is read again from the start; an upload is not
            # repeated here — a second one is a new version.
            delay = min(_RETRY_BACKOFF * (2**attempt), _RETRY_BACKOFF_MAX)
            if method != "GET" or not is_transient(error) or waited + delay > self.retry_window:
                raise error from cause
            await asyncio.sleep(delay)
            waited += delay
            attempt += 1

    # -- identity / context ----------------------------------------------------

    async def get_context(self, *, session_id: str | None = None) -> Json:
        params = {"sessionId": session_id} if session_id else None
        return await self._request("GET", "/harness/context", params=params)

    # -- sessions --------------------------------------------------------------

    async def open_session(
        self,
        *,
        client_name: str,
        client_version: str = "",
        harness_type: str | None = None,
        harness_version: str = "",
        capabilities: list[str] | None = None,
        hostname: str | None = None,
        environment: Json | None = None,
        ttl_seconds: int | None = None,
        metadata: Json | None = None,
    ) -> Json:
        body: Json = {"clientName": client_name, "clientVersion": client_version}
        if metadata:
            body["metadata"] = metadata
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        if harness_type is not None:
            body["harness"] = {
                "type": harness_type,
                "version": harness_version,
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": capabilities or [],
                "hostname": hostname,
                "environment": environment or {},
            }
        return await self._request("POST", "/sessions", json_body=body, idempotent=True)

    async def heartbeat_session(self, session_id: str, *, ttl_seconds: int | None = None) -> Json:
        body: Json = {}
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        return await self._request("POST", f"/sessions/{session_id}:heartbeat", json_body=body)

    async def close_session(self, session_id: str) -> Json:
        return await self._request(
            "POST", f"/sessions/{session_id}:close", json_body={}, idempotent=True
        )

    # -- work discovery --------------------------------------------------------

    async def list_available_work(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        workspace_id: str | None = None,
        include_descendants: bool = False,
        project_id: str | None = None,
        include_subprojects: bool = False,
        assigned_to_me: bool = False,
        type_keys: Sequence[str] | None = None,
    ) -> Json:
        """``type_keys`` — only tasks of these types (``typeKey``, repeated).

        An empty ``type_keys`` is refused: sent as no parameter at all, it
        would ask for the whole queue instead of none of it.
        """
        if type_keys is not None and not type_keys:
            raise ValueError("type_keys must not be empty; pass None for every type")
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        if type_keys is not None:
            params["typeKey"] = list(type_keys)
        if workspace_id is not None:
            params["workspaceId"] = workspace_id
        if include_descendants:
            params["includeDescendants"] = "true"
        if project_id is not None:
            params["projectId"] = project_id
        if include_subprojects:
            params["includeSubprojects"] = "true"
        if assigned_to_me:
            params["assignedToMe"] = "true"
        return await self._request("GET", "/work/available", params=params)

    # -- tool discovery (HRS-3) ------------------------------------------------

    async def search_tools(
        self,
        *,
        query: str | None = None,
        run_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        params: Json = {}
        if query:
            params["query"] = query
        if run_id is not None:
            params["runId"] = run_id
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/tools", params=params)

    async def describe_tool(self, tool_ref: str, *, run_id: str | None = None) -> Json:
        params: Json = {"runId": run_id} if run_id is not None else {}
        return await self._request("GET", f"/tools/{tool_ref}", params=params)

    async def get_task(self, task_ref: str) -> Json:
        return await self._request("GET", f"/tasks/{task_ref}")

    async def list_task_verifications(
        self, task_ref: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Json:
        """Attempts of the verification stage, newest first (CP-ADR-0067)."""
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", f"/tasks/{task_ref}/verifications", params=params or None)

    async def get_claimability(self, task_ref: str) -> Json:
        return await self._request("GET", f"/tasks/{task_ref}/claimability")

    async def get_task_transitions(self, task_ref: str) -> Json:
        """Declared next statuses and the action that walks each edge (ADR-0048)."""
        return await self._request("GET", f"/tasks/{task_ref}/transitions")

    async def create_task(
        self,
        *,
        title: str,
        description: str = "",
        priority: str = "medium",
        status: str | None = None,
        type_key: str | None = None,
        type_version: int | None = None,
        workspace_id: str | None = None,
        owner_id: str | None = None,
        assignee_id: str | None = None,
        custom_fields: Json | None = None,
        start_date: str | None = None,
        due_date: str | None = None,
        parent_task: str | None = None,
        goal_id: str | None = None,
        origin: Json | None = None,
        acceptance: list[Json] | None = None,
        evidence: list[Json] | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        """File a Task. ``origin`` ({kind, ref?, ruleId?, evidence[]}) is recorded
        once; omitted, the server derives it (CP-ADR-0062)."""
        body: Json = {
            "title": title,
            "description": description,
            "priority": priority,
        }
        # None means "the initial status declared by the task type" (ADR-0048);
        # sending it would pin a key the type may not declare.
        for name, value in (
            ("status", status),
            ("typeKey", type_key),
            ("typeVersion", type_version),
            ("workspaceId", workspace_id),
            ("ownerId", owner_id),
            ("assigneeId", assignee_id),
            ("customFields", custom_fields),
            ("startDate", start_date),
            ("dueDate", due_date),
            ("parentTask", parent_task),
            ("goalId", goal_id),
            ("origin", origin),
            ("acceptance", acceptance),
            ("evidence", evidence),
        ):
            if value is not None:
                body[name] = value
        return await self._request(
            "POST",
            "/tasks",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def list_tasks(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | None = None,
        system_status_category: str | None = None,
        type_key: str | None = None,
        priority: str | None = None,
        owner_id: str | None = None,
        assignee_id: str | None = None,
        workspace_id: str | None = None,
        include_descendants: bool = False,
        project_id: str | None = None,
        include_subprojects: bool = False,
        start_from: str | None = None,
        start_to: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        sort: str | None = None,
        goal_id: str | None = None,
        q: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (
            ("limit", limit),
            ("cursor", cursor),
            ("status", status),
            ("systemStatusCategory", system_status_category),
            ("typeKey", type_key),
            ("priority", priority),
            ("ownerId", owner_id),
            ("assigneeId", assignee_id),
            ("workspaceId", workspace_id),
            ("projectId", project_id),
            ("startFrom", start_from),
            ("startTo", start_to),
            ("dueFrom", due_from),
            ("dueTo", due_to),
            ("sort", sort),
            ("goalId", goal_id),
            ("q", q),
        ):
            if value is not None:
                params[name] = value
        if include_descendants:
            params["includeDescendants"] = "true"
        if include_subprojects:
            params["includeSubprojects"] = "true"
        return await self._request("GET", "/tasks", params=params or None)

    async def update_task(
        self,
        task_ref: str,
        *,
        expected_version: int,
        title: str | Any = _UNSET,
        description: str | Any = _UNSET,
        priority: str | Any = _UNSET,
        status: str | Any = _UNSET,
        owner_id: str | Any | None = _UNSET,
        assignee_id: str | Any | None = _UNSET,
        workspace_id: str | Any | None = _UNSET,
        custom_fields: Json | Any = _UNSET,
        start_date: str | Any | None = _UNSET,
        due_date: str | Any | None = _UNSET,
        requirements: Json | Any = _UNSET,
        goal_id: str | Any | None = _UNSET,
        acceptance: list[Json] | Any = _UNSET,
        evidence: list[Json] | Any = _UNSET,
        claim_id: str | None = None,
        fencing_token: int | None = None,
    ) -> Json:
        """Optimistic Task update; expected_version is always explicit."""
        body: Json = {}
        for name, value in (
            ("title", title),
            ("description", description),
            ("priority", priority),
            ("status", status),
            ("ownerId", owner_id),
            ("assigneeId", assignee_id),
            ("workspaceId", workspace_id),
            # Explicit None is meaningful for the dates (clear) and rejected
            # for customFields, so both go through the _UNSET gate below.
            ("customFields", custom_fields),
            ("startDate", start_date),
            ("dueDate", due_date),
            ("requirements", requirements),
            # goalId=None unlinks; acceptance/evidence replace the document.
            ("goalId", goal_id),
            ("acceptance", acceptance),
            ("evidence", evidence),
        ):
            if value is not _UNSET:
                body[name] = value
        if claim_id is not None:
            body["claimId"] = claim_id
        if fencing_token is not None:
            body["fencingToken"] = fencing_token
        return await self._request(
            "PATCH",
            f"/tasks/{task_ref}",
            json_body=body,
            headers={"If-Match": f'"task-{expected_version}"'},
            idempotent=True,
        )

    async def add_task_relation(self, from_task: str, *, to_task: str, relation_type: str) -> Json:
        return await self._request(
            "POST",
            f"/tasks/{from_task}/relations",
            json_body={"toTask": to_task, "type": relation_type},
            idempotent=True,
        )

    async def remove_task_relation(self, task_ref: str, relation_id: str) -> Json:
        return await self._request(
            "DELETE",
            f"/tasks/{task_ref}/relations/{relation_id}",
            idempotent=True,
        )

    # -- goals (CP-ADR-0062) ---------------------------------------------------

    async def create_goal(
        self,
        *,
        title: str,
        desired_state: str = "",
        criteria: list[Json] | None = None,
        owner_id: str | None = None,
        workspace_id: str | None = None,
        parent_goal_id: str | None = None,
        created_from: Json | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        body: Json = {"title": title, "desiredState": desired_state}
        for name, value in (
            ("criteria", criteria),
            ("ownerId", owner_id),
            ("workspaceId", workspace_id),
            ("parentGoalId", parent_goal_id),
            ("createdFrom", created_from),
        ):
            if value is not None:
                body[name] = value
        return await self._request(
            "POST",
            "/goals",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def list_goals(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | None = None,
        workspace_id: str | None = None,
        owner_id: str | None = None,
        parent_goal_id: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (
            ("limit", limit),
            ("cursor", cursor),
            ("status", status),
            ("workspaceId", workspace_id),
            ("ownerId", owner_id),
            ("parentGoalId", parent_goal_id),
        ):
            if value is not None:
                params[name] = value
        return await self._request("GET", "/goals", params=params or None)

    async def get_goal(self, goal_id: str) -> Json:
        return await self._request("GET", f"/goals/{goal_id}")

    async def update_goal(self, goal_id: str, *, expected_version: int, **fields: Any) -> Json:
        """Optimistic Goal update. ``fields`` use snake_case names
        (title, desired_state, criteria, owner_id, status, parent_goal_id);
        an explicit None clears owner_id / parent_goal_id."""
        names = {
            "title": "title",
            "desired_state": "desiredState",
            "criteria": "criteria",
            "owner_id": "ownerId",
            "status": "status",
            "parent_goal_id": "parentGoalId",
        }
        unknown = sorted(set(fields) - set(names))
        if unknown:
            raise TypeError(f"update_goal() got unexpected fields: {unknown}")
        return await self._request(
            "PATCH",
            f"/goals/{goal_id}",
            json_body={names[k]: v for k, v in fields.items()},
            headers={"If-Match": f'"goal-{expected_version}"'},
            idempotent=True,
        )

    async def list_goal_work(
        self,
        goal_id: str,
        *,
        include_subgoals: bool = False,
        system_status_category: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (
            ("systemStatusCategory", system_status_category),
            ("limit", limit),
            ("cursor", cursor),
        ):
            if value is not None:
                params[name] = value
        if include_subgoals:
            params["includeSubgoals"] = "true"
        return await self._request("GET", f"/goals/{goal_id}/work", params=params or None)

    # -- work rules (CP-ADR-0063) ----------------------------------------------

    async def create_rule(
        self,
        *,
        key: str,
        trigger: Json,
        action: Json,
        condition: Json | bool | None = None,
        interpretation: Json | None = None,
        description: str = "",
        workspace_id: str | None = None,
        goal_id: str | None = None,
        status: str = "enabled",
        identity: Json | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        """Write a rule; enabled, it acts with this credential's authority, or
        with the agent's of ``identity`` (``{"agent": "<key>"}``)."""
        body: Json = {
            "key": key,
            "trigger": trigger,
            "action": action,
            "description": description,
            "status": status,
        }
        for name, value in (
            ("condition", condition),
            ("interpretation", interpretation),
            ("workspaceId", workspace_id),
            ("goalId", goal_id),
            ("identity", identity),
        ):
            if value is not None:
                body[name] = value
        return await self._request(
            "POST",
            "/rules",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def list_rules(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | None = None,
        workspace_id: str | None = None,
        key: str | None = None,
        trigger_kind: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (
            ("limit", limit),
            ("cursor", cursor),
            ("status", status),
            ("workspaceId", workspace_id),
            ("key", key),
            ("triggerKind", trigger_kind),
        ):
            if value is not None:
                params[name] = value
        return await self._request("GET", "/rules", params=params or None)

    async def get_rule(self, rule_id: str) -> Json:
        return await self._request("GET", f"/rules/{rule_id}")

    async def update_rule(self, rule_id: str, *, expected_version: int, **fields: Any) -> Json:
        """Optimistic rule update. ``fields`` use snake_case names (description,
        trigger, condition, interpretation, action, goal_id, identity); an
        explicit None resets the condition to "always", removes the
        interpretation or the identity, or unlinks the goal."""
        names = {
            "description": "description",
            "trigger": "trigger",
            "condition": "condition",
            "interpretation": "interpretation",
            "action": "action",
            "goal_id": "goalId",
            "identity": "identity",
        }
        unknown = sorted(set(fields) - set(names))
        if unknown:
            raise TypeError(f"update_rule() got unexpected fields: {unknown}")
        return await self._request(
            "PATCH",
            f"/rules/{rule_id}",
            json_body={names[k]: v for k, v in fields.items()},
            headers={"If-Match": f'"rule-{expected_version}"'},
            idempotent=True,
        )

    async def enable_rule(self, rule_id: str) -> Json:
        return await self._request("POST", f"/rules/{rule_id}:enable", idempotent=True)

    async def disable_rule(self, rule_id: str) -> Json:
        return await self._request("POST", f"/rules/{rule_id}:disable", idempotent=True)

    async def archive_rule(self, rule_id: str) -> Json:
        return await self._request("DELETE", f"/rules/{rule_id}", idempotent=True)

    async def list_rule_evaluations(
        self,
        rule_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (("status", status), ("limit", limit), ("cursor", cursor)):
            if value is not None:
                params[name] = value
        return await self._request("GET", f"/rules/{rule_id}/evaluations", params=params or None)

    # -- comments --------------------------------------------------------------

    async def add_task_comment(
        self,
        task_ref: str,
        *,
        body: str,
        run_id: str | None = None,
        artifact_id: str | None = None,
    ) -> Json:
        """Append a reply to a work item's thread; the author is the credential."""
        payload: Json = {"body": body}
        if run_id is not None:
            payload["runId"] = run_id
        if artifact_id is not None:
            payload["artifactId"] = artifact_id
        return await self._request(
            "POST", f"/tasks/{task_ref}/comments", json_body=payload, idempotent=True
        )

    async def list_task_comments(self, task_ref: str, **params: Any) -> Json:
        """A page of the thread, oldest first; the cursor is its own format.

        Every comment carries ``author {kind, displayName}`` (ADR-0050): who
        wrote it is readable with ``tasks.read``, without ``principals.read``.
        """
        return await self._request("GET", f"/tasks/{task_ref}/comments", params=params or None)

    async def get_task_comment(self, task_ref: str, comment_id: str) -> Json:
        return await self._request("GET", f"/tasks/{task_ref}/comments/{comment_id}")

    async def edit_task_comment(
        self, task_ref: str, comment_id: str, *, body: str, expected_version: int
    ) -> Json:
        """Correct one's OWN comment; the superseded text is kept as a revision."""
        return await self._request(
            "PATCH",
            f"/tasks/{task_ref}/comments/{comment_id}",
            json_body={"body": body},
            headers={"If-Match": f'"comment-{expected_version}"'},
            idempotent=True,
        )

    async def list_task_comment_revisions(
        self, task_ref: str, comment_id: str, **params: Any
    ) -> Json:
        return await self._request(
            "GET", f"/tasks/{task_ref}/comments/{comment_id}/revisions", params=params or None
        )

    # -- claims ----------------------------------------------------------------

    async def claim_task(
        self,
        task_ref: str,
        session_id: str,
        *,
        ttl_seconds: int | None = None,
        intent: str = "",
    ) -> Json:
        body: Json = {"sessionId": session_id}
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        if intent:
            body["intent"] = intent
        return await self._request(
            "POST", f"/tasks/{task_ref}:claim", json_body=body, idempotent=True
        )

    async def heartbeat_claim(self, claim_id: str, *, ttl_seconds: int | None = None) -> Json:
        body: Json = {}
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        return await self._request("POST", f"/claims/{claim_id}:heartbeat", json_body=body)

    async def release_claim(self, claim_id: str, *, reason: str = "released") -> Json:
        return await self._request(
            "POST", f"/claims/{claim_id}:release", json_body={"reason": reason}, idempotent=True
        )

    # -- runs ------------------------------------------------------------------

    async def start_run(
        self,
        task_ref: str,
        *,
        claim_id: str,
        fencing_token: int,
        input: Json | None = None,
        metadata: Json | None = None,
        max_duration_seconds: int | None = None,
        max_actions: int | None = None,
        agent_revision_id: str | None = None,
    ) -> Json:
        """Start a run under a live claim.

        ``agent_revision_id`` — the revision of its own agent the caller runs
        by (CP-ADR-0073 §7): required of a principal bound to an agent,
        refused from one that is not.
        """
        body: Json = {"claimId": claim_id, "fencingToken": fencing_token}
        if agent_revision_id is not None:
            body["agentRevisionId"] = agent_revision_id
        if input is not None:
            body["input"] = input
        if metadata:
            body["metadata"] = metadata
        if max_duration_seconds is not None:
            body["maxDurationSeconds"] = max_duration_seconds
        if max_actions is not None:
            body["maxActions"] = max_actions
        return await self._request(
            "POST", f"/tasks/{task_ref}:start-run", json_body=body, idempotent=True
        )

    async def get_run(self, run_id: str) -> Json:
        return await self._request("GET", f"/runs/{run_id}")

    async def get_run_context(self, run_id: str) -> Json:
        return await self._request("GET", f"/runs/{run_id}/context")

    async def succeed_run(
        self,
        run_id: str,
        *,
        output: Json | None = None,
        complete_task: bool = True,
        idempotency_key: str | None = None,
    ) -> Json:
        body: Json = {"completeTask": complete_task}
        if output is not None:
            body["output"] = output
        return await self._request(
            "POST",
            f"/runs/{run_id}:succeed",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def fail_run(
        self,
        run_id: str,
        *,
        failure_reason: str = "failed",
        output: Json | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        body: Json = {"failureReason": failure_reason}
        if output is not None:
            body["output"] = output
        return await self._request(
            "POST",
            f"/runs/{run_id}:fail",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def cancel_run(self, run_id: str, *, reason: str = "cancelled") -> Json:
        return await self._request(
            "POST", f"/runs/{run_id}:cancel", json_body={"reason": reason}, idempotent=True
        )

    async def suspend_run(
        self,
        run_id: str,
        *,
        reason: str = "waiting_approval",
        waiting_for_approval_id: str | None = None,
    ) -> Json:
        body: Json = {"reason": reason}
        if waiting_for_approval_id is not None:
            body["waitingForApprovalId"] = waiting_for_approval_id
        return await self._request(
            "POST", f"/runs/{run_id}:suspend", json_body=body, idempotent=True
        )

    async def prepare_handoff(
        self,
        run_id: str,
        *,
        summary: str,
        next_steps: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        reason: str = "human_harness_handoff",
    ) -> Json:
        return await self._request(
            "POST",
            f"/runs/{run_id}:handoff",
            json_body={
                "reason": reason,
                "checkpoint": {
                    "kind": "handoff",
                    "data": {
                        "summary": summary,
                        "nextSteps": next_steps or [],
                        "evidenceRefs": evidence_refs or [],
                    },
                },
            },
            idempotent=True,
        )

    async def continue_after_handoff(
        self,
        task_ref: str,
        *,
        session_id: str,
        intent: str = "continue_after_handoff",
    ) -> Json:
        """Create a new claim and Run, then return its server context."""
        claim = await self.claim_task(task_ref, session_id, intent=intent)
        run = await self.start_run(
            task_ref,
            claim_id=claim["id"],
            fencing_token=int(claim["fencingToken"]),
        )
        context = await self.get_run_context(run["id"])
        return {"claim": claim, "run": run, "context": context}

    async def request_cancel_run(self, run_id: str, *, reason: str = "") -> Json:
        return await self._request(
            "POST", f"/runs/{run_id}:request-cancel", json_body={"reason": reason}, idempotent=True
        )

    async def create_run_control_message(
        self,
        run_id: str,
        *,
        operation: str,
        causal_position: str,
        expected_run_version: int,
        directive: str | None = None,
        reason: str = "",
    ) -> Json:
        body: Json = {
            "operation": operation,
            "causalPosition": causal_position,
            "expectedRunVersion": expected_run_version,
            "reason": reason,
        }
        if directive is not None:
            body["directive"] = directive
        return await self._request(
            "POST",
            f"/runs/{run_id}/control-messages",
            json_body=body,
            idempotent=True,
        )

    async def list_run_control_messages(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", f"/runs/{run_id}/control-messages", params=params or None)

    async def acknowledge_run_control_message(
        self,
        run_id: str,
        message_id: str,
        *,
        status: str,
        claim_id: str,
        fencing_token: int,
        expected_run_version: int,
        expected_message_version: int,
        safe_boundary: str | None = None,
        reason: str = "",
    ) -> Json:
        body: Json = {
            "status": status,
            "claimId": claim_id,
            "fencingToken": fencing_token,
            "expectedRunVersion": expected_run_version,
            "expectedMessageVersion": expected_message_version,
            "reason": reason,
        }
        if safe_boundary is not None:
            body["safeBoundary"] = safe_boundary
        return await self._request(
            "POST",
            f"/runs/{run_id}/control-messages/{message_id}:acknowledge",
            json_body=body,
            idempotent=True,
        )

    # -- child run handles (HRS-7) ---------------------------------------------

    async def launch_child_run(
        self,
        run_id: str,
        *,
        correlation_id: str,
        title: str,
        description: str = "",
        priority: str = "medium",
        workspace_id: str | None = None,
        owner_id: str | None = None,
        assignee_id: str | None = None,
        grant: Json | None = None,
        cancellation_policy: str | None = None,
        expires_in_seconds: int | None = None,
    ) -> Json:
        """Launch a child run. Repeating a correlation id returns the same child.

        The response carries ``handleToken`` only on the call that created the
        handle; a replay returns ``None`` there. Losing the token costs nothing:
        every operation also accepts the handle id.
        """
        body: Json = {
            "correlationId": correlation_id,
            "title": title,
            "description": description,
            "priority": priority,
        }
        for key, value in (
            ("workspaceId", workspace_id),
            ("ownerId", owner_id),
            ("assigneeId", assignee_id),
            ("grant", grant),
            ("cancellationPolicy", cancellation_policy),
            ("expiresInSeconds", expires_in_seconds),
        ):
            if value is not None:
                body[key] = value
        return await self._request(
            "POST", f"/runs/{run_id}/child-handles", json_body=body, idempotent=True
        )

    async def list_child_handles(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        active: bool | None = None,
    ) -> Json:
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        if active is not None:
            params["active"] = active
        return await self._request("GET", f"/runs/{run_id}/child-handles", params=params or None)

    async def resolve_child_handle(self, ref: str) -> Json:
        """Resolve a child handle by id or ``ch1_`` token."""
        return await self._request("GET", f"/child-handles/{ref}")

    async def revoke_child_handle(
        self, handle_id: str, *, reason: str = "", cancel_child: bool = False
    ) -> Json:
        return await self._request(
            "POST",
            f"/child-handles/{handle_id}:revoke",
            json_body={"reason": reason, "cancelChild": cancel_child},
            idempotent=True,
        )

    # -- checkpoints & actions -------------------------------------------------

    async def create_checkpoint(self, run_id: str, *, kind: str, data: Json | None = None) -> Json:
        return await self._request(
            "POST",
            f"/runs/{run_id}/checkpoints",
            json_body={"kind": kind, "data": data or {}},
            idempotent=True,
        )

    async def list_checkpoints(self, run_id: str) -> Json:
        return await self._request("GET", f"/runs/{run_id}/checkpoints")

    async def list_run_actions(
        self, run_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Json:
        """A run's actions in ``seq`` order; with ``limit``/``cursor`` — one page."""
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", f"/runs/{run_id}/actions", params=params or None)

    async def record_action(
        self,
        run_id: str,
        *,
        action: str,
        status: str = "completed",
        skill: str | None = None,
        external_reference: str | None = None,
        metadata: Json | None = None,
    ) -> Json:
        body: Json = {"action": action, "status": status}
        if skill is not None:
            body["skill"] = skill
        if external_reference is not None:
            body["externalReference"] = external_reference
        if metadata:
            body["metadata"] = metadata
        return await self._request(
            "POST", f"/runs/{run_id}/actions", json_body=body, idempotent=True
        )

    async def finish_action(
        self,
        run_id: str,
        action_id: str,
        *,
        status: str,
        external_reference: str | None = None,
    ) -> Json:
        body: Json = {"status": status}
        if external_reference is not None:
            body["externalReference"] = external_reference
        return await self._request(
            "POST", f"/runs/{run_id}/actions/{action_id}:finish", json_body=body, idempotent=True
        )

    # -- task completion (without runs) ---------------------------------------

    async def complete_task(
        self,
        task_ref: str,
        *,
        version: int,
        claim_id: str | None = None,
        fencing_token: int | None = None,
    ) -> Json:
        body: Json = {}
        if claim_id is not None:
            body["claimId"] = claim_id
        if fencing_token is not None:
            body["fencingToken"] = fencing_token
        return await self._request(
            "POST",
            f"/tasks/{task_ref}:complete",
            json_body=body,
            headers={"If-Match": f'"task-{version}"'},
            idempotent=True,
        )

    async def migrate_task_type(
        self,
        task_ref: str,
        *,
        version: int,
        type_version: int | None = None,
        status_map: dict[str, str] | None = None,
    ) -> Json:
        """Move an open task to another version of its type (ADR-0048).

        Needs ``task_types.manage`` besides ``tasks.write`` on the task.
        """
        body: Json = {}
        if type_version is not None:
            body["typeVersion"] = type_version
        if status_map is not None:
            body["statusMap"] = status_map
        return await self._request(
            "POST",
            f"/tasks/{task_ref}:migrate-type",
            json_body=body,
            headers={"If-Match": f'"task-{version}"'},
            idempotent=True,
        )

    # -- artifacts -------------------------------------------------------------

    async def create_artifact(
        self,
        *,
        type: str,
        name: str,
        task_ref: str | None = None,
        run_id: str | None = None,
        uri: str | None = None,
        content: Json | None = None,
        content_ref: str | None = None,
        metadata: Json | None = None,
        supersedes_artifact_id: str | None = None,
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        """``workspace_id`` is needed only for an artifact bound to neither a
        task nor a run — otherwise the server derives it.

        ``content_ref`` is the answer of :meth:`upload_artifact_content`; it
        excludes ``uri`` and ``content`` (CP-ADR-0072 §1)."""
        body: Json = {"type": type, "name": name}
        if task_ref is not None:
            body["task"] = task_ref
        if run_id is not None:
            body["runId"] = run_id
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        if uri is not None:
            body["uri"] = uri
        if content is not None:
            body["content"] = content
        if content_ref is not None:
            body["contentRef"] = content_ref
        if metadata:
            body["metadata"] = metadata
        if supersedes_artifact_id is not None:
            body["supersedesArtifactId"] = supersedes_artifact_id
        return await self._request(
            "POST",
            "/artifacts",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def get_artifact(self, artifact_id: str) -> Json:
        """One artifact with its ``content``: the journal event names it only."""
        return await self._request("GET", f"/artifacts/{artifact_id}")

    async def list_artifacts(self, **params: Any) -> Json:
        return await self._request("GET", "/artifacts", params=params or None)

    async def upload_artifact_content(
        self, source: bytes | str | os.PathLike[str], *, media_type: str
    ) -> Json:
        """Upload the bytes of a file: ``{contentRef, sizeBytes, mediaType,
        sha256, expiresAt}`` (CP-ADR-0072 §2).

        The ``contentRef`` is then named in :meth:`create_artifact`. A path is
        streamed from disk, never read whole. There is no Idempotency-Key and
        no retry on a transport failure: a repeated upload is a new
        ``contentRef`` for the same object, and one nobody references expires
        on its own, so the caller may simply upload again.
        """
        if isinstance(source, bytes):
            data = source

            def body() -> Any:
                return data

            size = len(data)
        else:
            path = Path(source)
            size = (await asyncio.to_thread(path.stat)).st_size

            def body() -> Any:
                return _file_chunks(path)

        response = await self._send(
            "PUT",
            "/artifact-contents",
            content=body,
            headers={"Content-Type": media_type, "Content-Length": str(size)},
        )
        result: Json = response.json()
        return result

    async def download_artifact_content(
        self,
        artifact_id: str,
        destination: str | os.PathLike[str],
        *,
        for_task: str | None = None,
    ) -> Json:
        """Stream the stored content of an artifact into ``destination``.

        ``for_task`` reads the artifact as an input of that task (CP-ADR-0072
        §5): the executor of the next step may have no right on the task that
        produced it. The file appears only complete — it is written beside the
        destination and renamed into place — and only when its sha256 matches
        the ``ETag`` the server sent. Returns ``{path, sizeBytes, mediaType,
        sha256}``.
        """
        target = Path(destination)
        params = {"forTask": for_task} if for_task else None
        response = await self._send(
            "GET", f"/artifacts/{artifact_id}/content", params=params, stream=True
        )
        partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as sink:
                async for chunk in response.aiter_bytes(_CONTENT_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    await asyncio.to_thread(sink.write, chunk)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            await response.aclose()
        sha256 = digest.hexdigest()
        etag = response.headers.get("etag", "").strip('"')
        if etag.startswith("sha256:") and etag.removeprefix("sha256:") != sha256:
            partial.unlink(missing_ok=True)
            raise ControlPlaneError(
                "content_integrity_failed",
                "Downloaded content does not match its sha256",
                details={"artifactId": artifact_id, "expected": etag, "actual": sha256},
            )
        os.replace(partial, target)
        media_type = response.headers.get("content-type", "application/octet-stream")
        return {"path": str(target), "sizeBytes": size, "mediaType": media_type, "sha256": sha256}

    # -- approvals -------------------------------------------------------------

    async def request_approval(
        self,
        *,
        task_ref: str | None = None,
        artifact_id: str | None = None,
        required_role_id: str | None = None,
        assigned_principal_id: str | None = None,
        comment: str = "",
        gate: bool = False,
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        """Exactly one of ``required_role_id``/``assigned_principal_id`` names
        the decider; ``workspace_id`` is required when there is no task to
        derive it from."""
        body: Json = {"comment": comment, "gate": gate}
        if task_ref is not None:
            body["task"] = task_ref
        if artifact_id is not None:
            body["artifactId"] = artifact_id
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        if required_role_id is not None:
            body["requiredRoleId"] = required_role_id
        if assigned_principal_id is not None:
            body["assignedPrincipalId"] = assigned_principal_id
        return await self._request(
            "POST",
            "/approvals",
            json_body=body,
            headers=_idempotency(idempotency_key),
            idempotent=True,
        )

    async def list_approvals(self, **params: Any) -> Json:
        return await self._request("GET", "/approvals", params=params or None)

    async def approve(self, approval_id: str, *, comment: str | None = None) -> Json:
        body: Json = {"comment": comment} if comment is not None else {}
        return await self._request(
            "POST", f"/approvals/{approval_id}:approve", json_body=body, idempotent=True
        )

    async def reject(self, approval_id: str, *, comment: str | None = None) -> Json:
        body: Json = {"comment": comment} if comment is not None else {}
        return await self._request(
            "POST", f"/approvals/{approval_id}:reject", json_body=body, idempotent=True
        )

    async def get_approval_outcome(self, approval_id: str) -> Json:
        """The declared outcome of a decision and what happened to each action."""
        return await self._request("GET", f"/approvals/{approval_id}/outcome")

    async def replay_approval_outcome(self, approval_id: str) -> Json:
        """Resume a failed (or stuck pending) outcome at its first open action."""
        return await self._request(
            "POST", f"/approvals/{approval_id}:replay-outcome", json_body={}, idempotent=True
        )

    # -- context memory --------------------------------------------------------

    async def get_working_context(
        self,
        *,
        query: str = "",
        task_ref: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        include_subprojects: bool = False,
        max_tokens: int | None = None,
        include_memory: bool = True,
        anchors: list[str] | None = None,
        strategy: str | None = None,
        as_of: str | None = None,
    ) -> Json:
        """Working context: authoritative operational state + (optionally)
        durable memory recalled by the external context provider. Degrades to
        operational-only when memory is unavailable (see ``memoryStatus``)."""
        body: Json = {"query": query, "includeMemory": include_memory}
        if anchors is not None:
            body["anchors"] = anchors
        if strategy is not None:
            body["strategy"] = strategy
        if as_of is not None:
            body["asOf"] = as_of
        if task_ref is not None:
            body["task"] = task_ref
        if run_id is not None:
            body["runId"] = run_id
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        if project_id is not None:
            body["projectId"] = project_id
        if include_subprojects:
            body["includeSubprojects"] = True
        if max_tokens is not None:
            body["maxTokens"] = max_tokens
        return await self._request("POST", "/context", json_body=body)

    async def recall(
        self,
        *,
        anchor: str | None = None,
        query: str | None = None,
        kind: str | None = None,
        kinds: list[str] | None = None,
        relations: list[str] | None = None,
        direction: str | None = None,
        depth: int | None = None,
        limit: int | None = None,
        as_of: str | None = None,
        task_ref: str | None = None,
        workspace_id: str | None = None,
        budget_tokens: int | None = None,
    ) -> Json:
        """Typed recall from the knowledge graph (CP-ADR-0064): from an anchor
        or from identifiers found in a query, along ``relations``, at ``as_of``.
        The server picks the namespaces from the task/workspace, applies the
        caller's visibility and cuts the pack to ``budget_tokens``."""
        body: Json = {}
        for key, value in (
            ("anchor", anchor),
            ("query", query),
            ("kind", kind),
            ("kinds", kinds),
            ("relations", relations),
            ("direction", direction),
            ("depth", depth),
            ("limit", limit),
            ("asOf", as_of),
            ("task", task_ref),
            ("workspaceId", workspace_id),
            ("budgetTokens", budget_tokens),
        ):
            if value is not None:
                body[key] = value
        return await self._request("POST", "/context/recall", json_body=body)

    async def get_context_pack(self, pack_id: str) -> Json:
        """A recorded task context pack (anchors of sources the caller cannot
        read are withheld, ``redactedAnchors``)."""
        return await self._request("GET", f"/context-packs/{pack_id}")

    async def replay_context_pack(self, pack_id: str) -> Json:
        """Send a recorded pack's request again: ``reproduced``, ``drift``, ``pack``."""
        return await self._request("POST", f"/context-packs/{pack_id}:replay", json_body={})

    async def remember(
        self,
        *,
        kind: str,
        content: str,
        data: Json | None = None,
        assertions: list[Json] | None = None,
        task_ref: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        dedup_key: str | None = None,
        observed_at: str | None = None,
        supersedes: str | None = None,
        external_ref: Json | None = None,
    ) -> Json:
        """Record an explicit observation (finding/decision/...) into the
        replayable journal; the context adapter delivers it to durable
        memory. Submit only intentional, externalized knowledge.

        External observations name their ``source`` system (required with
        ``dedup_key``/``external_ref``). A ``(source, dedup_key)`` repeated by
        the same author returns the existing observation with
        ``deduplicated: true``."""
        body: Json = {"kind": kind, "content": content}
        if assertions is not None:
            body["assertions"] = assertions
        if data is not None:
            body["data"] = data
        if task_ref is not None:
            body["task"] = task_ref
        if run_id is not None:
            body["runId"] = run_id
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        if session_id is not None:
            body["sessionId"] = session_id
        if source is not None:
            body["source"] = source
        if dedup_key is not None:
            body["dedupKey"] = dedup_key
        if observed_at is not None:
            body["observedAt"] = observed_at
        if supersedes is not None:
            body["supersedes"] = supersedes
        if external_ref is not None:
            body["externalRef"] = external_ref
        return await self._request("POST", "/observations", json_body=body, idempotent=True)

    # -- knowledge (CP-ADR-0060) -------------------------------------------------

    async def submit_knowledge_snapshot(
        self, *, workspace_id: str, snapshot: Json, expected_state: str | None = None
    ) -> Json:
        """Hand a connector's snapshot (pack, source, scope, snapshotId,
        observedAt, entities, relations) to the core, which reconciles it into
        the namespace of the workspace tree root with the workspace's
        visibility. Returns Memory's answer (counters, ``duplicate``,
        ``stateToken``); a stale snapshot is a ``ConflictError``. With
        ``expected_state`` (the ``stateToken`` of
        :meth:`preview_knowledge_snapshot`) it applies only while the source's
        knowledge is still in the previewed state, otherwise ``ConflictError``
        ``snapshot_stale``: preview again. Safe to retry: Memory recognises a
        repeated ``snapshotId``."""
        body: Json = {**snapshot, "workspaceId": workspace_id}
        if expected_state is not None:
            body["expectedState"] = expected_state
        return await self._request("POST", "/knowledge/snapshots", json_body=body, idempotent=True)

    async def preview_knowledge_snapshot(self, *, workspace_id: str, snapshot: Json) -> Json:
        """What applying ``snapshot`` would change -- ``changes``, counters,
        ``conflicts`` with other sources -- and ``stateToken``, the state the
        plan was built on. Nothing is written."""
        body: Json = {**snapshot, "workspaceId": workspace_id}
        return await self._request(
            "POST", "/knowledge/snapshots:preview", json_body=body, idempotent=True
        )

    async def submit_knowledge_document(self, *, workspace_id: str, document: Json) -> Json:
        """Store a knowledge base document (naturalKey, title, type, chunks,
        links ``{kind, key, rel}``, meta) in the namespace of the workspace
        tree root with the workspace's visibility. The text is already
        extracted and cut into chunks: the core parses no files. Returns
        Memory's answer. Safe to retry: a write with the same ``naturalKey``
        replaces the document's chunks."""
        body: Json = {**document, "workspaceId": workspace_id}
        return await self._request("POST", "/knowledge/documents", json_body=body, idempotent=True)

    async def query_knowledge_entities(
        self,
        *,
        workspace_id: str,
        kinds: list[str],
        where: list[Json] | None = None,
        as_of: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        include: Json | None = None,
    ) -> Json:
        """One page of the entities of ``kinds`` in the workspace's knowledge
        valid at ``as_of`` (now when omitted) whose attributes satisfy every
        ``where`` condition (``{attr, op, value}``, literals). Returns
        ``items`` and ``nextCursor``: pass it back as ``cursor`` with the same
        arguments for the next page; the list ends at ``nextCursor: None``.
        The server reads the namespace of the workspace tree root with the
        caller's visibility. ``include={"relations": [...] | "*", "direction":
        "out" | "in" | "both", "limit": n}`` adds each entity's ``relations``
        (``{relation, direction, kind, key, title}`` of the other end)."""
        body: Json = {"workspaceId": workspace_id, "kinds": kinds}
        for key, value in (
            ("where", where),
            ("asOf", as_of),
            ("limit", limit),
            ("cursor", cursor),
            ("include", include),
        ):
            if value is not None:
                body[key] = value
        return await self._request("POST", "/knowledge/entities:query", json_body=body)

    async def register_knowledge_pack(self, pack: Json) -> Json:
        """Register a domain knowledge pack. A shared pack is for platform
        administrators only (``CP_KNOWLEDGE_PACK_ADMINS`` on the server): the
        registry of shared packs serves all tenants. A manifest with
        ``"scope": "tenant"`` registers a pack of the caller's tenant under
        ``knowledge.packs.manage``; it is enabled as ``tenant:name@version``."""
        return await self._request("POST", "/knowledge/packs", json_body=pack)

    async def set_workspace_knowledge_packs(
        self, workspace_id: str, *, packs: list[str], strict: bool = False
    ) -> Json:
        """Enable ``packs`` (pinned ``name@version`` or ``tenant:name@version``
        references) and
        ``strict`` kind checking for the memory namespace of a root workspace.
        Replaces the previous set."""
        return await self._request(
            "PUT",
            f"/workspaces/{workspace_id}/knowledge-packs",
            json_body={"packs": packs, "strict": strict},
            idempotent=True,
        )

    async def get_workspace_knowledge_packs(self, workspace_id: str) -> Json:
        """The packs enabled for the workspace's tree and ``strict``, in the
        shape :meth:`set_workspace_knowledge_packs` takes them, plus
        ``effective`` (what Memory applies) and ``configured``."""
        return await self._request("GET", f"/workspaces/{workspace_id}/knowledge-packs")

    async def get_knowledge_pack(self, ref: str) -> Json:
        """A registered pack version: ``name@version``, ``name`` for the latest,
        ``tenant:name[@version]`` for a pack of the caller's tenant.
        ``NotFoundError`` when there is none."""
        return await self._request("GET", f"/knowledge/packs/{ref}")

    # -- events ----------------------------------------------------------------

    async def list_events(
        self,
        *,
        cursor: str | None = None,
        after: int | None = None,
        before: str | None = None,
        order: str | None = None,
        limit: int | None = None,
        tail: int | None = None,
        types: Sequence[str] | None = None,
        workspace_id: str | None = None,
        **params: Any,
    ) -> Json:
        """One journal page.

        ``cursor`` is the opaque replay cursor (``eventCursor`` from context,
        or ``nextCursor`` from a previous page) — never construct or compare
        one. ``after`` is the deprecated v0.3 integer sequence, still accepted
        by the server during the compatibility window. ``types`` (type
        prefixes, e.g. ``approval.``) and ``workspace_id`` (a workspace
        subtree) narrow the page (CP-ADR-0068); ``nextCursor`` of a narrowed
        page is resumed with the same filters. ``before`` (a ``prevCursor``
        of a previous page, e.g. of a ``tail`` page) reads the page that
        precedes it; ``order="desc"`` returns the items newest first.
        """
        query: Json = dict(params)
        if types:
            query["types"] = ",".join(types)
        if workspace_id is not None:
            query["workspaceId"] = workspace_id
        if cursor is not None:
            query["cursor"] = cursor
        elif after is not None:
            query["after"] = after
        if before is not None:
            query["before"] = before
        if order is not None:
            query["order"] = order
        if limit is not None:
            query["limit"] = limit
        if tail is not None:
            query["tail"] = tail
        return await self._request("GET", "/events", params=query)

    async def follow_events(
        self,
        *,
        cursor: str | None = None,
        after: int | None = None,
        poll_interval: float = 2.0,
        stop: asyncio.Event | None = None,
    ) -> AsyncIterator[Json]:
        """Yield events strictly after the cursor, forever (or until `stop`).

        Poll-based follower on top of GET /events: reconnect-safe by design —
        the server hands back an opaque ``nextCursor`` with every page and
        guarantees committed events are never permanently skipped. The SDK
        stores the cursor verbatim and never interprets it. (A WebSocket
        variant is a latency optimization, not a correctness feature.)
        """
        position: str | None = cursor
        legacy_after = after
        while stop is None or not stop.is_set():
            page = await self.list_events(cursor=position, after=legacy_after, limit=200)
            position = page["nextCursor"]
            legacy_after = None
            events = page.get("items", [])
            for event in events:
                yield event
            if not events:
                try:
                    if stop is not None:
                        await asyncio.wait_for(stop.wait(), poll_interval)
                        return
                    await asyncio.sleep(poll_interval)
                except TimeoutError:
                    continue

    # -- projects (v0.5) -------------------------------------------------------

    # --- work item types (ADR-0048) -------------------------------------------

    async def list_task_types(self, **params: Any) -> Json:
        return await self._request("GET", "/task-types", params=params or None)

    async def get_task_type(self, type_id: str) -> Json:
        return await self._request("GET", f"/task-types/{type_id}")

    async def create_task_type(
        self,
        *,
        key: str,
        display_name: str,
        description: str = "",
        field_schema: Json | None = None,
        lifecycle_schema: Json | None = None,
        execution: Json | None = None,
        approval_schema: Json | None = None,
        context_schema: Json | None = None,
        instructions: str | None = None,
        completion_schema: Json | None = None,
    ) -> Json:
        """Create the NEXT version of ``key``; the server allocates the number.

        ``execution = {skill, version, inputs}`` makes it a type whose tasks
        are executed by one skill invocation (ADR-0056 §3). ``context_schema``
        is the context profile of its tasks (CP-ADR-0064). ``instructions`` —
        Markdown up to 16 KiB telling the executor how to do a task of this
        version (CP-ADR-0066); changing it means a new version.
        ``completion_schema`` — work core files once a task of this version is
        completed (CP-ADR-0061, amendment 2026-09-25).
        """
        body: Json = {"key": key, "displayName": display_name, "description": description}
        if field_schema is not None:
            body["fieldSchema"] = field_schema
        if lifecycle_schema is not None:
            body["lifecycleSchema"] = lifecycle_schema
        if execution is not None:
            body["execution"] = execution
        if approval_schema is not None:
            body["approvalSchema"] = approval_schema
        if context_schema is not None:
            body["contextSchema"] = context_schema
        if instructions is not None:
            body["instructions"] = instructions
        if completion_schema is not None:
            body["completionSchema"] = completion_schema
        return await self._request("POST", "/task-types", json_body=body, idempotent=True)

    async def deprecate_task_type(self, type_id: str) -> Json:
        return await self._request(
            "POST", f"/task-types/{type_id}:deprecate", json_body={}, idempotent=True
        )

    async def migrate_type_tasks(
        self,
        type_id: str,
        *,
        to_version: int | None = None,
        status_map: dict[str, str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        """Move one page of open tasks of a type version (ADR-0048)."""
        body: Json = {}
        if to_version is not None:
            body["toVersion"] = to_version
        if status_map is not None:
            body["statusMap"] = status_map
        if limit is not None:
            body["limit"] = limit
        if cursor is not None:
            body["cursor"] = cursor
        return await self._request(
            "POST", f"/task-types/{type_id}:migrate-tasks", json_body=body, idempotent=True
        )

    # -- skills (ADR-0056) -----------------------------------------------------

    async def register_skill(
        self,
        *,
        name: str,
        version: str = "1.0.0",
        description: str = "",
        protocol: str | None = None,
        side_effects: str | None = None,
        risk_level: str | None = None,
        contract: Json | None = None,
        config: Json | None = None,
    ) -> Json:
        """Publish a skill version; with ``contract`` it becomes invocable."""
        body: Json = {"name": name, "version": version, "description": description}
        for key, value in (
            ("protocol", protocol),
            ("sideEffects", side_effects),
            ("riskLevel", risk_level),
            ("contract", contract),
            ("config", config),
        ):
            if value is not None:
                body[key] = value
        return await self._request("POST", "/skills", json_body=body, idempotent=True)

    async def describe_skill(self, skill_ref: str) -> Json:
        """One version by id, ``name@version`` or ``name``, with its full contract."""
        return await self._request("GET", f"/skills/{skill_ref}")

    async def invoke_skill(
        self,
        skill_ref: str,
        *,
        inputs: Json | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        approval_id: str | None = None,
    ) -> Json:
        """Create a skill invocation (``pending``); the result arrives later.

        ``idempotency_key`` is the business key of the call — a repeat returns
        the existing invocation. Without it every call is a new invocation.
        """
        body: Json = {"inputs": inputs or {}}
        for key, value in (
            ("idempotencyKey", idempotency_key),
            ("taskId", task_id),
            ("runId", run_id),
            ("approvalId", approval_id),
        ):
            if value is not None:
                body[key] = value
        return await self._request(
            "POST", f"/skills/{skill_ref}:invoke", json_body=body, idempotent=True
        )

    async def get_skill_invocation(self, invocation_id: str) -> Json:
        return await self._request("GET", f"/skill-invocations/{invocation_id}")

    async def wait_skill_invocation(
        self,
        invocation_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.5,
    ) -> Json:
        """Poll until the invocation is terminal or the timeout passes.

        Returns the last observed state either way; the caller checks
        ``status``. Waiting is bounded: skills run asynchronously by design.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.0)
        while True:
            invocation = await self.get_skill_invocation(invocation_id)
            if invocation.get("status") in SKILL_INVOCATION_TERMINAL:
                return invocation
            remaining = deadline - loop.time()
            if remaining <= 0:
                return invocation
            await asyncio.sleep(min(poll_interval_seconds, remaining))

    # executor side

    async def claim_skill_invocation(
        self,
        *,
        protocols: list[str],
        local_entrypoints: list[str] | None = None,
        http_origins: list[str] | None = None,
        mcp_endpoints: list[str] | None = None,
        audiences: list[str] | None = None,
        session_id: str | None = None,
        lease_seconds: int | None = None,
        invocation_id: str | None = None,
    ) -> Json | None:
        """Take one executable pending invocation; ``None`` when the queue is empty.

        The executor declares what it runs: ``local_entrypoints`` for
        ``local``, ``http_origins`` (``scheme://host[:port]``) for ``http``,
        ``mcp_endpoints`` (origins and ``stdio:<name>``) for ``mcp``, and the
        IAM ``audiences`` it issues tokens for. A remote protocol without any
        of its endpoints gets nothing.

        Returns ``{"invocation": ..., "skill": ..., "settings": ...}`` — the
        skill as an executor sees it: name, version and the full contract, no
        catalog ``config``; ``settings`` — the effective settings of the
        skill's package or ``None``. ``invocation_id`` narrows the claim to
        that one call.
        """
        body: Json = {
            "protocols": protocols,
            "localEntrypoints": local_entrypoints or [],
            "httpOrigins": http_origins or [],
            "mcpEndpoints": mcp_endpoints or [],
            "audiences": audiences or [],
        }
        if session_id is not None:
            body["sessionId"] = session_id
        if lease_seconds is not None:
            body["leaseSeconds"] = lease_seconds
        if invocation_id is not None:
            body["invocationId"] = invocation_id
        result = await self._request(
            "POST", "/skill-invocations:claim", json_body=body, idempotent=True
        )
        return result or None

    async def heartbeat_skill_invocation(
        self,
        invocation_id: str,
        *,
        fencing_token: int,
        lease_seconds: int | None = None,
        session_id: str | None = None,
    ) -> Json:
        """Extend the lease; pass the claim's ``session_id`` if it had one."""
        body: Json = {"fencingToken": fencing_token}
        if lease_seconds is not None:
            body["leaseSeconds"] = lease_seconds
        if session_id is not None:
            body["sessionId"] = session_id
        return await self._request(
            "POST",
            f"/skill-invocations/{invocation_id}:heartbeat",
            json_body=body,
            idempotent=True,
        )

    async def complete_skill_invocation(
        self,
        invocation_id: str,
        *,
        fencing_token: int,
        output: Json,
        cost: Json | None = None,
        session_id: str | None = None,
    ) -> Json:
        body: Json = {"fencingToken": fencing_token, "output": output}
        if cost is not None:
            body["cost"] = cost
        if session_id is not None:
            body["sessionId"] = session_id
        return await self._request(
            "POST",
            f"/skill-invocations/{invocation_id}:complete",
            json_body=body,
            idempotent=True,
        )

    async def fail_skill_invocation(
        self,
        invocation_id: str,
        *,
        fencing_token: int,
        code: str,
        message: str = "",
        retryable: bool = False,
        details: Json | None = None,
        session_id: str | None = None,
    ) -> Json:
        error: Json = {"code": code, "message": message, "retryable": retryable}
        if details is not None:
            error["details"] = details
        body: Json = {"fencingToken": fencing_token, "error": error}
        if session_id is not None:
            body["sessionId"] = session_id
        return await self._request(
            "POST",
            f"/skill-invocations/{invocation_id}:fail",
            json_body=body,
            idempotent=True,
        )

    async def cancel_skill_invocation(
        self, invocation_id: str, *, reason: str = "cancelled"
    ) -> Json:
        """Cancel a call that has not finished; its executor is fenced out."""
        return await self._request(
            "POST",
            f"/skill-invocations/{invocation_id}:cancel",
            json_body={"reason": reason},
            idempotent=True,
        )

    async def list_projects(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        workspace_id: str | None = None,
        status: str | None = None,
        template_key: str | None = None,
        external_system: str | None = None,
        external_type: str | None = None,
        external_id: str | None = None,
    ) -> Json:
        params: Json = {}
        for name, value in (
            ("limit", limit),
            ("cursor", cursor),
            ("workspaceId", workspace_id),
            ("status", status),
            ("templateKey", template_key),
            ("externalSystem", external_system),
            ("externalType", external_type),
            ("externalId", external_id),
        ):
            if value is not None:
                params[name] = value
        return await self._request("GET", "/projects", params=params)

    async def get_project(self, project_id: str) -> Json:
        return await self._request("GET", f"/projects/{project_id}")

    async def create_project(
        self,
        *,
        workspace_id: str | None = None,
        workspace_slug: str | None = None,
        workspace_name: str | None = None,
        parent_workspace_id: str | None = None,
        workspace_type_key: str | None = None,
        template_id: str | None = None,
        template_key: str | None = None,
        template_version: int | None = None,
        status_key: str | None = None,
        owner_principal_id: str | None = None,
        custom_fields: Json | None = None,
        settings: Json | None = None,
    ) -> Json:
        body: Json = {}
        for name, value in (
            ("workspaceId", workspace_id),
            ("workspaceSlug", workspace_slug),
            ("workspaceName", workspace_name),
            ("parentWorkspaceId", parent_workspace_id),
            ("workspaceTypeKey", workspace_type_key),
            ("templateId", template_id),
            ("templateKey", template_key),
            ("templateVersion", template_version),
            ("statusKey", status_key),
            ("ownerPrincipalId", owner_principal_id),
            ("customFields", custom_fields),
            ("settings", settings),
        ):
            if value is not None:
                body[name] = value
        return await self._request("POST", "/projects", json_body=body, idempotent=True)

    async def update_project(self, project_id: str, *, version: int, **fields: Any) -> Json:
        """PATCH under the project's ETag; ``version`` comes from the body."""
        return await self._request(
            "PATCH",
            f"/projects/{project_id}",
            json_body=fields,
            headers={"If-Match": f'"project-{version}"'},
            idempotent=True,
        )

    async def archive_project(self, project_id: str) -> Json:
        return await self._request(
            "POST", f"/projects/{project_id}:archive", json_body={}, idempotent=True
        )

    async def transition_project(
        self, project_id: str, *, status_key: str, version: int, comment: str = ""
    ) -> Json:
        return await self._request(
            "POST",
            f"/projects/{project_id}:transition",
            json_body={"statusKey": status_key, "comment": comment},
            headers={"If-Match": f'"project-{version}"'},
            idempotent=True,
        )

    async def get_effective_config(self, project_id: str) -> Json:
        return await self._request("GET", f"/projects/{project_id}/effective-config")

    async def list_config_revisions(self, project_id: str, **params: Any) -> Json:
        return await self._request("GET", f"/projects/{project_id}/config-revisions", params=params)

    async def create_config_revision(
        self, project_id: str, *, config: Json, comment: str = ""
    ) -> Json:
        return await self._request(
            "POST",
            f"/projects/{project_id}/config-revisions",
            json_body={"config": config, "comment": comment},
            idempotent=True,
        )

    async def activate_config_revision(
        self, project_id: str, revision: int, *, version: int
    ) -> Json:
        return await self._request(
            "POST",
            f"/projects/{project_id}/config-revisions/{revision}:activate",
            json_body={},
            headers={"If-Match": f'"project-{version}"'},
            idempotent=True,
        )

    async def list_external_references(self, project_id: str, **params: Any) -> Json:
        return await self._request(
            "GET", f"/projects/{project_id}/external-references", params=params
        )

    async def add_external_reference(
        self,
        project_id: str,
        *,
        external_system: str,
        external_type: str,
        external_id: str,
        metadata: Json | None = None,
    ) -> Json:
        return await self._request(
            "POST",
            f"/projects/{project_id}/external-references",
            json_body={
                "externalSystem": external_system,
                "externalType": external_type,
                "externalId": external_id,
                "metadata": metadata or {},
            },
            idempotent=True,
        )

    # Generic external references (ADR-0047): any entity type, not just projects.

    async def register_external_reference(
        self,
        *,
        entity_type: str,
        entity_id: str,
        external_system: str,
        external_type: str,
        external_id: str,
        metadata: Json | None = None,
    ) -> Json:
        """Map an external identifier onto an internal entity.

        ``entity_id`` is a reference: a UUID, or a public id such as
        ``TASK-000026`` for entity types that have one.
        """
        return await self._request(
            "POST",
            "/external-references",
            json_body={
                "entityType": entity_type,
                "entityId": entity_id,
                "externalSystem": external_system,
                "externalType": external_type,
                "externalId": external_id,
                "metadata": metadata or {},
            },
            idempotent=True,
        )

    async def list_entity_external_references(
        self, *, entity_type: str, entity_id: str, **params: Any
    ) -> Json:
        return await self._request(
            "GET",
            "/external-references",
            params={"entityType": entity_type, "entityId": entity_id, **params},
        )

    async def lookup_external_reference(
        self,
        *,
        external_system: str,
        external_id: str,
        external_type: str | None = None,
        **params: Any,
    ) -> Json:
        """Reverse lookup. An entity the caller cannot read is simply absent."""
        query: dict[str, Any] = {
            "externalSystem": external_system,
            "externalId": external_id,
            **params,
        }
        if external_type is not None:
            query["externalType"] = external_type
        return await self._request("GET", "/external-references", params=query)

    async def list_project_templates(self, **params: Any) -> Json:
        return await self._request("GET", "/project-templates", params=params)

    async def create_project_template(self, *, key: str, display_name: str, **fields: Any) -> Json:
        return await self._request(
            "POST",
            "/project-templates",
            json_body={"key": key, "displayName": display_name, **fields},
            idempotent=True,
        )

    async def get_workspace_tree(
        self,
        *,
        root_id: str | None = None,
        depth: int | None = None,
        include_projects: bool = True,
    ) -> Json:
        params: Json = {"includeProjects": "true" if include_projects else "false"}
        if root_id is not None:
            params["rootId"] = root_id
        if depth is not None:
            params["depth"] = depth
        return await self._request("GET", "/workspaces/tree", params=params)

    # -- organization directory ---------------------------------------------------

    async def list_roles(self, *, workspace_id: str | None = None, **params: Any) -> Json:
        """A page of roles (``RoleOut``), optionally of one workspace."""
        query: Json = dict(params)
        if workspace_id is not None:
            query["workspaceId"] = workspace_id
        return await self._request("GET", "/roles", params=query or None)

    async def list_role_holders(
        self, role_id: str, *, workspace_id: str | None = None, **params: Any
    ) -> Json:
        """A page of principals holding the role in the workspace — the ones
        eligible to decide an approval requiring it there (CP-ADR-0068)."""
        query: Json = dict(params)
        if workspace_id is not None:
            query["workspaceId"] = workspace_id
        return await self._request("GET", f"/roles/{role_id}/principals", params=query or None)

    async def list_workspace_participants(self, workspace_id: str, **params: Any) -> Json:
        """A page of participants of the workspace: explicit members (``member``)
        and holders of its roles (``roles``) — CP-ADR-0010 amendment."""
        return await self._request(
            "GET", f"/workspaces/{workspace_id}/participants", params=params or None
        )

    async def list_principals(self, *, kind: str | None = None, **params: Any) -> Json:
        """A page of principals (``PrincipalOut``); there is no name filter."""
        query: Json = dict(params)
        if kind is not None:
            query["kind"] = kind
        return await self._request("GET", "/principals", params=query or None)

    async def get_principal(self, principal_id: str) -> Json:
        """One principal (``PrincipalOut``); needs ``principals.read``."""
        return await self._request("GET", f"/principals/{principal_id}")

    # -- IAM identity bindings (ADR-0053) --------------------------------------

    async def list_iam_bindings(self, principal_ref: str) -> Json:
        return await self._request("GET", f"/principals/{principal_ref}/iam-bindings")

    async def upsert_iam_binding(
        self,
        principal_ref: str,
        *,
        issuer: str,
        iam_tenant_id: str,
        iam_principal_id: str,
        permissions: list[str],
    ) -> Json:
        """Bind a federated identity to a principal; a repeat repoints it."""
        return await self._request(
            "POST",
            f"/principals/{principal_ref}/iam-bindings",
            json_body={
                "issuer": issuer,
                "iamTenantId": iam_tenant_id,
                "iamPrincipalId": iam_principal_id,
                "permissions": permissions,
            },
            idempotent=True,
        )

    async def revoke_iam_binding(self, binding_id: str) -> Json:
        return await self._request("POST", f"/iam-bindings/{binding_id}:revoke", idempotent=True)

    # -- agents (CP-ADR-0073) --------------------------------------------------

    async def get_my_agent(self) -> Json:
        """The agent the caller is, with its current revision (``AgentMeOut``).

        ``packageSettings`` — the effective settings of the package that
        installed the agent, or ``None``.

        ``NotFoundError`` when the caller's principal is not bound to an agent;
        a retired agent is returned with ``status: retired``.
        """
        return await self._request("GET", "/agents/me")

    async def get_agent(self, ref: str) -> Json:
        """An agent by ``key`` (current revision) or ``key@revision``."""
        return await self._request("GET", f"/agents/{ref}")

    async def list_agents(
        self,
        *,
        status: str | None = None,
        state: str | None = None,
        workspace_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        include: str | None = None,
    ) -> Json:
        """A page of agents; ``include="status"`` adds ``observedStatus`` to each."""
        params: Json = {}
        for key, value in (
            ("status", status),
            ("state", state),
            ("workspaceId", workspace_id),
            ("limit", limit),
            ("cursor", cursor),
            ("include", include),
        ):
            if value is not None:
                params[key] = value
        return await self._request("GET", "/agents", params=params or None)

    async def publish_agent(self, key: str, spec: Json, *, package: Json | None = None) -> Json:
        """Apply a spec: a new revision only when its canonical hash differs.

        ``package`` — ``{"key", "version"}`` of the package being applied: the
        source a new revision records; without it the revision is a manual edit.
        """
        body: Json = {"key": key, "spec": spec}
        if package is not None:
            body["package"] = package
        return await self._request("POST", "/agents", json_body=body, idempotent=True)

    async def list_agent_revisions(
        self, key: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Json:
        """A page of the revisions of ``key``, newest first, without their specs."""
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", f"/agents/{key}/revisions", params=params or None)

    async def validate_agent(self, key: str, spec: Json) -> Json:
        """Every check of :meth:`publish_agent`, nothing saved."""
        return await self._request("POST", "/agents:validate", json_body={"key": key, "spec": spec})

    async def update_agent_state(
        self, key: str, *, state: str | None = None, replicas: int | None = None
    ) -> Json:
        """Change the desired state or replicas; never a new revision."""
        body: Json = {}
        if state is not None:
            body["state"] = state
        if replicas is not None:
            body["replicas"] = replicas
        return await self._request("PATCH", f"/agents/{key}/state", json_body=body, idempotent=True)

    async def retire_agent(self, key: str, *, reason: str) -> Json:
        return await self._request(
            "POST", f"/agents/{key}:retire", json_body={"reason": reason}, idempotent=True
        )

    async def link_agent_identity(
        self, key: str, *, issuer: str, iam_tenant_id: str, iam_principal_id: str
    ) -> Json:
        """The placement service names the agent's IAM identity (§6); idempotent."""
        return await self._request(
            "PUT",
            f"/agents/{key}/identity",
            json_body={
                "issuer": issuer,
                "iamTenantId": iam_tenant_id,
                "iamPrincipalId": iam_principal_id,
            },
            idempotent=True,
        )

    async def replace_agent_identity(
        self,
        key: str,
        *,
        issuer: str,
        iam_tenant_id: str,
        iam_principal_id: str,
        reason: str,
    ) -> Json:
        """Move a service agent to a new IAM identity; the principal stays the same."""
        return await self._request(
            "POST",
            f"/agents/{key}/identity:replace",
            json_body={
                "issuer": issuer,
                "iamTenantId": iam_tenant_id,
                "iamPrincipalId": iam_principal_id,
                "reason": reason,
            },
            idempotent=True,
        )

    async def get_agent_status(self, key: str) -> Json:
        return await self._request("GET", f"/agents/{key}/status")

    async def report_agent_status(
        self,
        key: str,
        *,
        phase: str,
        instances: Json,
        observed_at: str,
        observed_revision: int | None = None,
        node: str | None = None,
        reason: Json | None = None,
    ) -> Json:
        """What actually runs (§4); only the placement service may write it."""
        body: Json = {"phase": phase, "instances": instances, "observedAt": observed_at}
        for name, value in (
            ("observedRevision", observed_revision),
            ("node", node),
            ("reason", reason),
        ):
            if value is not None:
                body[name] = value
        return await self._request("PUT", f"/agents/{key}/status", json_body=body, idempotent=True)

    # -- processes and packages (CP-ADR-0074) -----------------------------------

    async def get_process_definition(self, ref: str) -> Json:
        """A process by ``key`` (latest version) or ``key@version`` (``ProcessDefinitionOut``)."""
        return await self._request("GET", f"/process-definitions/{ref}")

    async def list_process_versions(
        self, key: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Json:
        params: Json = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request(
            "GET", f"/process-definitions/{key}/versions", params=params or None
        )

    async def retire_process_definition(
        self, key: str, reason: str, *, dry_run: bool = False
    ) -> Json:
        """Retire every version of a process: no new instances, open ones run to the end.

        ``ProcessRetireOut`` with the open instances by version; ``dry_run``
        makes the same checks and answer without writing.
        """
        return await self._request(
            "POST",
            f"/process-definitions/{key}:retire",
            json_body={"reason": reason},
            params={"dryRun": "true"} if dry_run else None,
            idempotent=not dry_run,
        )

    async def retire_calendar(self, key: str, reason: str, *, dry_run: bool = False) -> Json:
        """Retire every version of a calendar; ``calendar_in_use`` while a process needs it."""
        return await self._request(
            "POST",
            f"/calendars/{key}:retire",
            json_body={"reason": reason},
            params={"dryRun": "true"} if dry_run else None,
            idempotent=not dry_run,
        )

    async def get_process_instance(self, instance_id: str) -> Json:
        return await self._request("GET", f"/process-instances/{instance_id}")

    async def list_process_journal(
        self,
        instance_id: str,
        *,
        kind: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Json:
        """One page of the decision journal of an instance, oldest first."""
        params: Json = {}
        for name, value in (("kind", kind), ("limit", limit), ("cursor", cursor)):
            if value is not None:
                params[name] = value
        return await self._request(
            "GET", f"/process-instances/{instance_id}/journal", params=params or None
        )

    async def test_package(
        self,
        files: Sequence[Json],
        *,
        tests: Sequence[str] | None = None,
        workspace_id: str | None = None,
        check_only: bool = False,
    ) -> Json:
        """Check a package and run its tests in the core's sandbox; nothing is written.

        ``files`` are ``{path, content}`` of the package; ``check_only`` runs
        the check without any test.
        """
        body: Json = {"package": {"files": list(files)}}
        if tests is not None:
            body["tests"] = list(tests)
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        return await self._request(
            "POST",
            "/packages:test",
            json_body=body,
            params={"checkOnly": "true"} if check_only else None,
        )

    async def plan_package(
        self,
        files: Sequence[Json],
        *,
        workspace_id: str | None = None,
        replay_limit: int | None = None,
        overwrite_console: bool = False,
    ) -> Json:
        """The plan of applying a package with its ``planHash``; nothing is written."""
        body: Json = {"package": {"files": list(files)}, "overwriteConsole": overwrite_console}
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        if replay_limit is not None:
            body["replayLimit"] = replay_limit
        return await self._request("POST", "/packages:plan", json_body=body)

    async def apply_package(
        self,
        files: Sequence[Json],
        *,
        plan_hash: str,
        workspace_id: str | None = None,
        overwrite_console: bool = False,
    ) -> Json:
        """Apply exactly the plan with ``plan_hash``; a changed catalog is ``plan_stale``."""
        body: Json = {
            "package": {"files": list(files)},
            "planHash": plan_hash,
            "overwriteConsole": overwrite_console,
        }
        if workspace_id is not None:
            body["workspaceId"] = workspace_id
        return await self._request("POST", "/packages:apply", json_body=body, idempotent=True)

    # -- operator actions (v0.5) ----------------------------------------------

    async def adapter_status(self) -> Json:
        return await self._request("GET", "/operations/context-adapter")

    async def redrive_adapter(self, tenant_id: str, *, reason: str = "operator_redrive") -> Json:
        return await self._request(
            "POST",
            f"/operations/context-adapter/{tenant_id}:redrive",
            json_body={"reason": reason},
            idempotent=True,
        )

    async def rebuild_adapter(
        self, tenant_id: str, *, cursor: str | None = None, reason: str = "operator_rebuild"
    ) -> Json:
        body: Json = {"reason": reason}
        if cursor is not None:
            body["cursor"] = cursor
        return await self._request(
            "POST",
            f"/operations/context-adapter/{tenant_id}:rebuild",
            json_body=body,
            idempotent=True,
        )


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    """A file as chunks read off the event loop."""
    with path.open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, _CONTENT_CHUNK):
            yield chunk


class HeartbeatRunner:
    """Keeps session (and optionally claim) leases alive in the background.

    A DOMAIN failure (expired lease, lost ownership) is terminal and is NOT
    hidden: it is stored in ``error`` and the loop stops — the harness must
    check ``error``/``alive`` and stop authoritative writes. A TRANSIENT
    failure (:func:`is_transient`: the network, a 502/503/504 of a restarting
    core) is not evidence of lost ownership: it is retried after
    ``retry_seconds`` rather than a whole interval — the lease keeps running
    out meanwhile — and becomes terminal only once ``outage_budget_seconds``
    have passed since the last successful heartbeat (or the start). The budget
    defaults to ``interval_seconds * max_transport_failures``: the patience of
    three missed beats, 180 s with the defaults, whatever the retry pace.
    """

    def __init__(
        self,
        client: ControlPlaneClient,
        *,
        session_id: str,
        claim_id: str | None = None,
        interval_seconds: float = 60.0,
        max_transport_failures: int = 3,
        retry_seconds: float = 10.0,
        outage_budget_seconds: float | None = None,
    ) -> None:
        self._client = client
        self.session_id = session_id
        self.claim_id = claim_id
        self.interval = interval_seconds
        self.retry_seconds = retry_seconds
        self.outage_budget = (
            interval_seconds * max_transport_failures
            if outage_budget_seconds is None
            else outage_budget_seconds
        )
        self.error: ControlPlaneError | None = None
        self.transport_failures = 0
        self._clock: Callable[[], float] = time.monotonic
        self._last_success = 0.0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if self.transport_failures == 0:
                pause = self.interval
            else:
                pause = min(self.retry_seconds, self.interval)
            try:
                await asyncio.wait_for(self._stop.wait(), pause)
                return
            except TimeoutError:
                pass
            try:
                await self._client.heartbeat_session(self.session_id)
                if self.claim_id is not None:
                    await self._client.heartbeat_claim(self.claim_id)
            except ControlPlaneError as exc:
                if not is_transient(exc):
                    self.error = exc
                    return
                self.transport_failures += 1
                if self._clock() - self._last_success >= self.outage_budget:
                    self.error = exc
                    return
            else:
                self.transport_failures = 0
                self._last_success = self._clock()

    def start(self) -> None:
        self._stop.clear()
        self.error = None
        self.transport_failures = 0
        self._last_success = self._clock()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done() and self.error is None


def pretty(data: Json) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
