"""Working context: authoritative operational state + durable memory.

The two halves are explicitly distinct and never mixed:

* ``operational`` — authoritative Control Plane state, read in this request's
  transaction (the same data the harness bootstrap returns, focused on the
  requested task/run). It is the truth; nothing recalled from memory may
  override it.
* ``memory`` — a ContextPack built by the external Context Memory Engine:
  eventually-consistent accumulated knowledge (previous findings, decisions,
  related facts) with provenance and a trace id. May be absent: with the
  provider disabled or unreachable the response degrades to operational-only
  (HTTP 200 + ``memoryStatus``), never a 5xx.

Scope authorization happens HERE, before any provider call: the caller can
focus the request (task/run/workspace) but only on entities that resolve
inside its own tenant; the Memory Service never sees an unauthorized scope
and cannot expand anyone's authority.

The operational snapshot is passed to the provider as ``ephemeral_context``:
the Memory contract compiles it into the pack's ``current`` section without
persisting it — durable ingestion happens only through the event journal via
the Context Adapter (never on this synchronous path).
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import observability
from control_plane.application.authorization import AuthContext, authorize, visible_objects
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.task_inputs import resolve_task_inputs
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.application.context.assertions import validate_anchors
from control_plane.application.context.graph import graph_scope_of, workspace_read
from control_plane.application.event_cursor import EventPosition, encode_position
from control_plane.application.queries.events import current_position
from control_plane.application.queries.harness import get_harness_context
from control_plane.application.queries.instructions import instructions_for_task
from control_plane.application.queries.projects import (
    effective_config_for,
    get_tenant_project,
    parent_project_id,
    project_scope_workspace_ids,
)
from control_plane.application.queries.task_context import (
    prepare_task_context,
    task_profile,
    unavailable,
)
from control_plane.application.visibility import task_visible
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.context_provider import (
    ContextProvider,
    ContextProviderError,
    tenant_namespace,
)
from control_plane.infrastructure.db.models import (
    Artifact,
    Event,
    EventConsumerCursor,
    ProjectTemplate,
    Run,
    Task,
    Workspace,
)
from control_plane.worker.context_adapter import CONSUMER_NAME

logger = logging.getLogger(__name__)

_ARTIFACTS_LIMIT = 20
_LAG_CAP = 1000
# Same bound as ContextQueryRequest.query: the retrieval query is a search
# string, not a document; the head of a long description carries the intent.
_QUERY_MAX_CHARS = 2000
# Statuses on which a multi-namespace read is retried with the tenant namespace
# alone (CP-ADR-0059 p.4).
_MULTI_NAMESPACE_REJECTIONS = (400, 403, 422)


def focus_query(title: str, description: str | None) -> str:
    """Retrieval query of a focused task: its title plus its description.

    The title alone is too thin to recall what the task is about (TAI-ADR-0042
    p.6); the description is where the entities and terms of the task live.
    """
    text = title.strip()
    body = (description or "").strip()
    if body:
        text = f"{text}\n\n{body}" if text else body
    return text[:_QUERY_MAX_CHARS]


async def _task_focus(session: AsyncSession, ctx: AuthContext, task_ref: str) -> dict[str, Any]:
    task = await resolve_task(session, ctx, task_ref)
    artifacts = []
    if ctx.has(Permission.ARTIFACTS_READ):  # same gate as GET /artifacts
        artifacts = list(
            (
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.tenant_id == ctx.tenant_id, Artifact.task_id == task.id)
                    .order_by(Artifact.created_at.desc())
                    .limit(_ARTIFACTS_LIMIT)
                )
            ).all()
        )
    return {
        "task": {
            "id": str(task.id),
            "publicId": task.public_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "priority": task.priority,
            "workspaceId": str(task.workspace_id) if task.workspace_id else None,
            "version": task.version,
            "activeClaimId": str(task.active_claim_id) if task.active_claim_id else None,
        },
        "artifacts": [
            {
                "id": str(a.id),
                "type": a.type,
                "name": a.name,
                "uri": a.uri,
                "runId": str(a.run_id) if a.run_id else None,
                "createdAt": a.created_at.isoformat(),
            }
            for a in artifacts
        ],
        # CP-ADR-0072 §8: the same inputs as in GET /runs/{id}/context.
        "inputs": await resolve_task_inputs(session, ctx, task),
    }


async def _memory_lag(
    session: AsyncSession, ctx: AuthContext
) -> tuple[str | None, int | None, dict[str, Any] | None]:
    """(adapter cursor, per-tenant lag in events (capped), ingest health).

    The ingest block surfaces a parked adapter (poison/permanent failure):
    without it a wedged pipeline would masquerade as ordinary lag while
    ``memoryStatus`` stays "ok" (the synchronous recall path still works)."""
    row = await session.get(EventConsumerCursor, (CONSUMER_NAME, ctx.tenant_id))
    if row is None:
        return None, None, None
    position = EventPosition(tx_id=row.tx_id, sequence=row.sequence)
    lag = await session.scalar(
        select(func.count()).select_from(
            select(Event.sequence)
            .where(
                Event.tenant_id == ctx.tenant_id,
                (Event.tx_id > position.tx_id)
                | ((Event.tx_id == position.tx_id) & (Event.sequence > position.sequence)),
            )
            .limit(_LAG_CAP)
            .subquery()
        )
    )
    meta = row.metadata_ or {}
    # Parked state is now a typed column of THIS tenant's row: a poison event
    # in another tenant no longer shows up here (ADR-0036).
    ingest = {
        "status": "parked" if row.parked_at is not None else "ok",
        "lastError": row.parked_reason or meta.get("last_error"),
        "lastDeliveryAt": meta.get("last_delivery_at"),
        "parkedEventId": str(row.parked_event_id) if row.parked_event_id else None,
        "nextAttemptAt": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
    }
    return encode_position(position), int(lag or 0), ingest


async def prepare_working_context(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    provider: ContextProvider | None,
    *,
    query: str = "",
    entity_anchors: list[str] | None = None,
    task_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    include_subprojects: bool = False,
    max_tokens: int | None = None,
    include_memory: bool = True,
    strategy: str | None = None,
    as_of: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Transactional half: authoritative reads + scope authorization.

    Returns ``(response, memory_call | None)``. The provider is NOT called
    here — the caller invokes :func:`fetch_memory` AFTER the database
    transaction is closed, so a slow/down provider can never hold a pooled
    connection hostage (memory outage must not throttle coordination).
    """
    requested_anchors = validate_anchors(entity_anchors)

    # Focused reads are permission-gated exactly like their standalone
    # endpoints: the working-context door must not bypass the v0.2 model.
    if task_ref is not None or run_id is not None:
        await authorize(ctx, Permission.TASKS_READ)

    # ---- authoritative operational state (this transaction) -----------------
    operational = await get_harness_context(session, ctx)
    scopes: list[str] = [f"principal:{ctx.principal_id}"]
    anchors: list[str] = [f"principal:{ctx.principal_id}"]
    subject: dict[str, str] = {"type": "principal", "id": str(ctx.principal_id)}
    focus_query_text = ""
    root_workspace_id: uuid.UUID | None = None
    # The focus workspace and its ancestors: what a local-mode read of the
    # workspace namespace may see (siblings of the focus stay out).
    workspace_scopes: list[str] = []

    task_id: uuid.UUID | None = None
    task: Task | None = None
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        # A run of invisible work is a missing run, not its missing task
        # (CP-ADR-0082 §3.7).
        if run is None or not await task_visible(session, ctx, run.task_id):
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        scopes.append(f"run:{run.id}")
        anchors.append(f"run:{run.id}")
        if task_ref is None:
            task_ref = str(run.task_id)

    if task_ref is not None:
        focus = await _task_focus(session, ctx, task_ref)
        operational["focus"] = focus
        task_id = uuid.UUID(focus["task"]["id"])
        task = await session.get(Task, task_id)
        focus_query_text = focus_query(focus["task"]["title"], focus["task"]["description"])
        subject = {"type": "task", "id": str(task_id)}
        scopes.append(f"task:{task_id}")
        anchors.append(f"task:{task_id}")
        task_workspace = focus["task"]["workspaceId"]
        if task_workspace and workspace_id is None:
            workspace_id = uuid.UUID(task_workspace)

    if project_id is not None:
        # Project focus is resolved and authorized server-side: a foreign
        # project is a 404 before any provider call, and the scopes handed to
        # Memory are computed here, never taken from the client (ADR-0035).
        await authorize(ctx, Permission.PROJECTS_READ)
        project = await get_tenant_project(session, ctx, project_id)
        if not ctx.sees_workspace(project.workspace_id):
            # The project's own 404, not its workspace's (CP-ADR-0082 §3.7).
            raise NotFoundError("Project not found", details={"projectId": str(project_id)})
        template = await session.get(ProjectTemplate, project.template_id)
        effective = await effective_config_for(session, ctx.tenant_id, project)
        parent_id = await parent_project_id(session, ctx.tenant_id, project)
        scope_ids = await project_scope_workspace_ids(
            session, ctx.tenant_id, project, include_subprojects=include_subprojects
        )
        operational["project"] = {
            "id": str(project.id),
            "workspaceId": str(project.workspace_id),
            "parentProjectId": str(parent_id) if parent_id else None,
            "templateKey": template.key if template else None,
            "templateVersion": template.version if template else None,
            "statusKey": project.status_key,
            "systemStatusCategory": project.system_status_category,
            "status": project.status,
            "ownerPrincipalId": (
                str(project.owner_principal_id) if project.owner_principal_id else None
            ),
            "startDate": project.start_date.isoformat() if project.start_date else None,
            "targetDate": project.target_date.isoformat() if project.target_date else None,
            "version": project.version,
            "workspaceScope": [str(w) for w in scope_ids],
            "includeSubprojects": include_subprojects,
            # Config is secret-free by construction (write-time guard), so it
            # is safe to surface; it never reaches durable memory.
            "effectiveConfig": effective.config,
            "configProvenance": effective.provenance,
        }
        subject = {"type": "project", "id": str(project.id)}
        scopes.append(f"project:{project.id}")
        anchors.append(f"project:{project.id}")
        if workspace_id is None:
            workspace_id = project.workspace_id

    if workspace_id is not None:
        # Resolved inside the caller's tenant only — a foreign workspace is a
        # 404 BEFORE any provider interaction (crafted requests cannot leak).
        workspace = await session.scalar(
            select(Workspace).where(
                Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id
            )
        )
        # One invisible to the caller answers alike (CP-ADR-0082 §3.7).
        if workspace is None or not ctx.sees_workspace(workspace.id):
            raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
        scopes.append(f"workspace:{workspace.id}")
        anchors.append(f"workspace:{workspace.id}")
        # Ancestors give retrieval visibility into enclosing context, without
        # exposing sibling subtrees.
        ancestors = await workspace_ancestor_ids(session, ctx.tenant_id, workspace.id)
        for ancestor in ancestors:
            scope = f"workspace:{ancestor}"
            if scope not in scopes and len(scopes) < 10:
                scopes.append(scope)
        # Nearest first, root last: the root names the workspace namespace
        # that holds the knowledge of this subtree (TAI-ADR-0031 p.4).
        root_workspace_id = ancestors[-1] if ancestors else workspace.id
        workspace_scopes = [f"workspace:{w}" for w in ancestors]

    current = await current_position(session, ctx.tenant_id)
    memory_cursor, memory_lag, memory_ingest = await _memory_lag(session, ctx)

    response: dict[str, Any] = {
        "operational": operational,
        "memory": None,
        "memoryStatus": "disabled",
        "memoryTraceId": None,
        "freshness": {
            "currentCursor": encode_position(current),
            "memoryCursor": memory_cursor,
            "memoryLagEvents": memory_lag,
            "memoryLagCapped": memory_lag is not None and memory_lag >= _LAG_CAP,
            "memoryIngest": memory_ingest,
        },
        "warnings": [],
    }
    if task is not None:
        # CP-ADR-0066: how to do this task — the same block as the run context,
        # so a runner's adapter and a person's harness read one set of layers.
        response["instructions"] = await instructions_for_task(session, ctx.tenant_id, task)

    # CP-ADR-0064: a task whose type version declares a context profile also
    # gets its typed context pack. A type without one keeps the response as it
    # was — no ``taskContext`` key at all.
    profile = await task_profile(session, task) if task is not None else None

    if provider is None or not include_memory:
        if include_memory and provider is None:
            response["warnings"].append("context provider is not configured")
        if profile is not None:
            response["taskContext"] = unavailable("disabled", profile)
        return response, None

    # Durable memory derives from the journal; reading it requires the same
    # permission as reading the journal itself. Degrade, don't 403 — the
    # operational half is the caller's own data and stays available.
    if not ctx.has(Permission.EVENTS_READ):
        response["memoryStatus"] = "forbidden"
        response["warnings"].append("memory recall requires the events.read permission")
        if profile is not None:
            response["taskContext"] = unavailable("forbidden", profile)
        return response, None

    budget = settings.context_default_max_tokens
    if max_tokens is not None:
        if max_tokens <= 0:
            raise ValidationError("invalid_context_request", "maxTokens must be positive")
        budget = min(max_tokens, settings.context_max_tokens_limit)

    ephemeral: dict[str, Any] = {
        "activeClaims": operational["activeClaims"],
        "activeRuns": operational["activeRuns"],
        "suspendedRuns": operational["suspendedRuns"],
        "pendingApprovals": operational["pendingApprovals"],
    }
    if "focus" in operational:
        ephemeral["task"] = operational["focus"]["task"]
        ephemeral["artifacts"] = operational["focus"]["artifacts"]
    if "project" in operational:
        # Only the identity/status slice goes to Memory as ephemeral context;
        # config and provenance stay on the Control Plane side.
        ephemeral["project"] = {
            key: operational["project"][key]
            for key in ("id", "workspaceId", "statusKey", "systemStatusCategory", "templateKey")
        }

    namespace = tenant_namespace(settings, ctx.tenant_id)
    memory_call: dict[str, Any] = {
        "namespace": namespace,
        "namespaces": [namespace],
        "request": {
            "query": query or focus_query_text or "current work context",
            "subject": subject,
            "scopes": scopes[:10],
            # Search hints only; EVENTS_READ authorizes the tenant journal.
            "anchors": list(dict.fromkeys(requested_anchors + anchors))[:10],
            "ephemeral_context": ephemeral,
            "max_tokens": budget,
        },
    }
    if strategy:
        memory_call["request"]["strategy"] = strategy
    if as_of is not None:
        # Point-in-time recall is opt-in: without it Memory answers "now".
        memory_call["request"]["as_of"] = as_of.isoformat()
    # Policy mode (CP-ADR-0055, MEM-ADR-019): the Control Plane reads memory on
    # behalf of the principal and hands Memory the visibility it computed from
    # the PDP — namespaces of readable memory_namespace objects and workspace
    # scopes. Memory never widens this set; without it an on-behalf read is denied.
    visible = await memory_visibility(ctx, settings)
    if visible is not None:
        memory_call["request"]["allowedNamespaces"] = visible[0]
        memory_call["request"]["allowedScopes"] = visible[1]
    if root_workspace_id is not None:
        # The workspace namespace is read only where the principal may read
        # it: in policy mode the PDP decides (a namespace outside the visible
        # set is not asked for at all). In local mode events.read over the
        # tenant, checked above, admits the namespace, but the namespace holds
        # the whole top-level subtree: the read is narrowed to the focus
        # workspace and its ancestors, so a task in one subtree does not
        # recall what was written about its siblings. The tenant namespace
        # holds journal observations tagged with workspace:<id> of every
        # subtree, so the narrowing covers it too, including the tenant-only
        # fallback, which resends this request.
        ws_namespace, narrowed = workspace_read(
            settings, ctx, visible, root_workspace_id, workspace_scopes, principal_scopes(ctx)
        )
        if ws_namespace is not None:
            memory_call["namespaces"].append(ws_namespace)
        if narrowed is not None:
            memory_call["request"]["allowedScopes"] = narrowed
    if profile is not None and task is not None:
        # Same namespaces and visibility as the recall above: the pack is
        # compiled from what this caller may read, nothing more.
        memory_call["taskContext"] = await prepare_task_context(
            session, ctx, task, profile, graph_scope_of(memory_call)
        )
    return response, memory_call


async def memory_visibility(
    ctx: AuthContext, settings: Settings
) -> tuple[list[str], list[str]] | None:
    """Namespaces and scopes the principal may read in Memory.

    Policy mode: what the PDP lists. A human in ``members`` mode
    (CP-ADR-0082 §3.4) in any mode: the workspaces of the visible set, and of
    workspace namespaces only those of the trees the set lies in.
    """
    namespaces = await visible_objects(ctx, "memory.read", "memory_namespace")
    if namespaces is None:
        if ctx.visible_workspaces is None:
            return None
        namespaces = {f"tenant-{ctx.tenant_id}", *(f"ws-{root}" for root in ctx.visible_roots)}
    workspaces = await visible_objects(ctx, "memory.read", "workspace") or set()
    prefix = settings.context_namespace_prefix
    tenant = str(ctx.tenant_id)
    names: list[str] = []
    for obj in sorted(namespaces):
        kind, _, ident = obj.partition("-")
        if kind == "ws" and ident:
            if ctx.visible_workspaces is not None and ident not in ctx.visible_roots:
                continue
            names.append(f"{prefix}{tenant}:ws:{ident}")
        elif kind == "principal" and ident:
            names.append(f"{prefix}{tenant}:principal:{ident}")
        elif kind == "tenant" and ident:
            names.append(f"{prefix}{ident}")
    scopes = [f"workspace:{w}" for w in sorted(workspaces)] + principal_scopes(ctx)
    return names, list(dict.fromkeys(scopes))


def principal_scopes(ctx: AuthContext) -> list[str]:
    scopes = [f"principal:{ctx.principal_id}"]
    if ctx.iam_principal_id is not None:
        scopes.append(f"principal:{ctx.iam_principal_id}")
    return scopes


async def fetch_memory(
    response: dict[str, Any],
    memory_call: dict[str, Any],
    provider: ContextProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> None:
    """Non-transactional half: durable memory (degrades, never fails).

    Runs strictly OUTSIDE any database transaction/session."""
    observability.inc("context_requests_total")
    started = time.monotonic()
    # One deadline for the whole read: the tenant-only fallback gets what the
    # rejected multi-namespace call left, not a fresh timeout of its own.
    deadline = started + settings.context_timeout_seconds + 1.0
    namespaces = list(memory_call.get("namespaces") or [memory_call["namespace"]])
    try:
        try:
            pack = await _build_pack(
                provider, memory_call["request"], memory_call, namespaces, deadline, trace_run_id
            )
        except ContextProviderError as exc:
            # A Memory that does not (yet) read several namespaces in one call
            # rejects the request as invalid, and one that does may still
            # refuse the workspace namespace to this caller (403). Losing the
            # workspace namespace is a degradation; losing the whole pack over
            # it would be an outage. The retry is the same request, local
            # allowedScopes included: only the workspace namespace is dropped.
            if len(namespaces) < 2 or exc.status not in _MULTI_NAMESPACE_REJECTIONS:
                raise
            response["warnings"].append(
                "context provider rejected the multi-namespace read; tenant namespace only"
            )
            pack = await _build_pack(
                provider,
                memory_call["request"],
                memory_call,
                [memory_call["namespace"]],
                deadline,
                trace_run_id,
            )
        response["memory"] = pack
        response["memoryStatus"] = "ok"
        response["memoryTraceId"] = pack.get("trace_id")
    except TimeoutError:
        response["memoryStatus"] = "timeout"
        response["warnings"].append("context provider timed out; operational context only")
        observability.inc("context_degraded_total")
        observability.inc("context_provider_failures_total")
    except ContextProviderError as exc:
        response["memoryStatus"] = "unavailable"
        response["warnings"].append(f"context provider unavailable: {exc}")
        logger.warning("context provider failed", exc_info=exc)
        observability.inc("context_degraded_total")
        observability.inc("context_provider_failures_total")
    except Exception as exc:  # a misbehaving provider must never 500 /context
        response["memoryStatus"] = "unavailable"
        response["warnings"].append(f"context provider error: {type(exc).__name__}")
        logger.exception("context provider raised unexpectedly")
        observability.inc("context_degraded_total")
        observability.inc("context_provider_failures_total")
    finally:
        observability.inc("context_request_duration_seconds_sum", time.monotonic() - started)
        observability.inc("context_request_duration_seconds_count")


async def _build_pack(
    provider: ContextProvider,
    request: dict[str, Any],
    memory_call: dict[str, Any],
    namespaces: list[str],
    deadline: float,
    trace_run_id: str,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(
        provider.build_context(
            namespace=memory_call["namespace"],
            namespaces=namespaces,
            request=request,
            trace_run_id=trace_run_id or None,
        ),
        timeout=remaining,
    )
