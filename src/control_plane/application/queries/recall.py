"""Pull side of the task context: ``cp_recall`` and pack replay (CP-ADR-0064, TAI-ADR-0042 p.6).

``POST /context/recall`` lets an agent ask the knowledge graph during its work:
from an anchor (an identifier it has in hand) or from a query (identifiers are
extracted from it with the domain packs' ``idPatterns``, exactly as from a
task description), along the relations it names, at a moment it names. It is
the same typed traversal, client and visibility as the task context pack —
the agent sees nothing its principal could not read through ``/context``.

``POST /context-packs/{id}:replay`` sends a recorded pack's request again with
the caller's visibility and says whether the answer still uses the same
entities and facts: how a reviewer sees what the executor saw.

A process's ``recall`` step (CP-ADR-0076 §4) reads through the same client:
its anchors are natural keys the engine computed, its traversal the step's,
its moment the time of the input that reached the step, its visibility the
process identity's. The worker asks outside any transaction
(:func:`fetch_process_recall`) and hands the normalized answer to the
instance's journal.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.knowledge import memory_failure
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.application.context.graph import (
    GraphScope,
    base_scope,
    deadline_after,
    drift,
    entities_of,
    kind_patterns,
    off_loop,
    recall_answer,
    semantic_request,
    typed,
    typed_pack,
    used_of,
    within,
    within_budget,
    workspace_read,
)
from control_plane.application.queries.context import memory_visibility, principal_scopes
from control_plane.application.queries.task_context import get_pack_record, public_record
from control_plane.config import Settings
from control_plane.domain.context_schema import (
    DEFAULT_BUDGET_TOKENS,
    MAX_ANCHORS,
    MAX_CANDIDATE_CHARS,
    MAX_STEP_LIMIT,
    Candidate,
    extract_identifiers,
    traverse_steps,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.models import Task, Workspace


@dataclass
class RecallCall:
    scope: GraphScope
    anchor: str = ""
    kind: str = ""
    query: str = ""
    kinds: list[str] = field(default_factory=list)
    traverse: list[dict[str, Any]] = field(default_factory=list)
    as_of: datetime | None = None
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    # recall.where as the caller gave it: literals, sent to memory as they are.
    where: list[dict[str, Any]] = field(default_factory=list)


def with_where(request: dict[str, Any], where: list[dict[str, Any]]) -> dict[str, Any]:
    """A typed request with ``where`` (MEM-ADR-020, K006) when there are conditions.

    The route and a process's step both send it through here: the core does
    not read the conditions, memory applies them. Without conditions the key
    is absent and the request stays what it was.
    """
    if where:
        request["where"] = where
    return request


def require_graph(provider: object | None) -> GraphProvider:
    if provider is None:
        raise DependencyUnavailableError(
            "Context memory provider is not configured", code="memory_disabled"
        )
    return provider  # type: ignore[return-value]


async def graph_scope(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    workspace_id: uuid.UUID | None,
) -> GraphScope:
    """Namespaces and visibility of a graph read, as ``POST /context`` computes them."""
    scope = base_scope(settings, ctx)
    visible = await memory_visibility(ctx, settings)
    if visible is not None:
        scope.visibility = {"allowedNamespaces": visible[0], "allowedScopes": visible[1]}
    if workspace_id is None:
        return scope
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id)
    )
    # One invisible to the caller answers alike (CP-ADR-0082 §3.7).
    if workspace is None or not ctx.sees_workspace(workspace.id):
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    ancestors = await workspace_ancestor_ids(session, ctx.tenant_id, workspace.id)
    ws_namespace, narrowed = workspace_read(
        settings,
        ctx,
        visible,
        ancestors[-1] if ancestors else workspace.id,
        [f"workspace:{w}" for w in ancestors],
        principal_scopes(ctx),
    )
    if ws_namespace is not None:
        scope.namespaces.append(ws_namespace)
    if narrowed is not None:
        scope.visibility["allowedScopes"] = narrowed
    return scope


async def prepare_recall(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    anchor: str | None,
    kind: str | None,
    query: str | None,
    kinds: list[str],
    relations: list[str],
    direction: str,
    depth: int,
    limit: int,
    as_of: datetime | None,
    task_ref: str | None,
    workspace_id: uuid.UUID | None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    where: list[dict[str, Any]] | None = None,
) -> RecallCall:
    """Authorize and resolve the scope of a recall (transactional half)."""
    # The graph is durable memory: the same right as recall through /context.
    await authorize(ctx, Permission.EVENTS_READ)
    if bool(anchor) == bool(query):
        raise ValidationError("invalid_recall_request", "Give exactly one of anchor or query")
    if task_ref is not None:
        await authorize(ctx, Permission.TASKS_READ)
        task: Task = await resolve_task(session, ctx, task_ref)
        if workspace_id is None:
            workspace_id = task.workspace_id
    return RecallCall(
        scope=await graph_scope(session, ctx, settings, workspace_id),
        anchor=(anchor or "").strip(),
        kind=kind or "",
        query=(query or "").strip(),
        kinds=list(dict.fromkeys(kinds)),
        traverse=[
            {"relation": r, "direction": direction, "depth": depth, "limit": limit}
            for r in dict.fromkeys(relations)
        ],
        as_of=as_of,
        budget_tokens=budget_tokens,
        where=list(where or ()),
    )


async def fetch_recall(
    call: RecallCall,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: resolve the anchors and traverse."""
    deadline = deadline_after(settings)
    trace = trace_run_id or None
    warnings: list[str] = []
    semantic = False
    try:
        if call.anchor:
            anchors = [
                Candidate(value=call.anchor[:MAX_CANDIDATE_CHARS], kind=call.kind, source="anchor")
            ]
        else:
            catalog = await within(
                deadline,
                kind_patterns(
                    provider, call.scope.namespaces, trace_run_id=trace, warnings=warnings
                ),
            )
            found = await off_loop(
                deadline,
                extract_identifiers,
                call.query,
                catalog.patterns,
                call.kinds,
                aliases=catalog.aliases,
            )
            anchors = [
                Candidate(value=value, kind=found_kind, source="query")
                for found_kind, value in found
            ]
            if not anchors:
                # Nothing deterministic in the query: Memory may add semantic
                # hits, which it marks ``evidence: inferred`` (TAI-ADR-0042 p.4).
                anchors = [Candidate(value=call.query[:MAX_CANDIDATE_CHARS], source="query")]
                semantic = True
        request: dict[str, Any] = {
            "anchors": [c.to_request() for c in anchors[:MAX_ANCHORS]],
            "traverse": call.traverse,
            "allow_semantic": semantic,
        }
        if call.as_of is not None:
            request["as_of"] = call.as_of.isoformat()
        pack = await typed(
            provider,
            call.scope,
            with_where(request, call.where),
            deadline=deadline,
            trace_run_id=trace,
        )
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc) from exc
    return {
        "anchors": [c.to_request() for c in anchors[:MAX_ANCHORS]],
        "semantic": semantic,
        "asOf": call.as_of.isoformat() if call.as_of else None,
        "namespaces": list(call.scope.namespaces),
        "warnings": warnings,
        "pack": within_budget(pack, call.budget_tokens),
    }


async def prepare_replay(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    pack_id: uuid.UUID,
) -> tuple[dict[str, Any], GraphScope]:
    """The recorded pack and the caller's visibility over its namespaces."""
    await authorize(ctx, Permission.EVENTS_READ)
    record = await get_pack_record(session, ctx, pack_id)
    task = await session.get(Task, uuid.UUID(record["taskId"]))
    assert task is not None
    current = await graph_scope(session, ctx, settings, task.workspace_id)
    scope = GraphScope(
        namespace=record["namespaces"][0],
        namespaces=list(record["namespaces"]),
        visibility=current.visibility,
    )
    return record, scope


async def fetch_replay(
    record: dict[str, Any],
    scope: GraphScope,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Send the recorded request again; compare what the answer uses."""
    if not record["request"].get("anchors"):
        # Every anchor was redacted for this reader: nothing to send.
        return _replayed(record, {})
    try:
        pack = await typed_pack(
            provider,
            scope,
            record["request"],
            deadline=deadline_after(settings),
            trace_run_id=trace_run_id or None,
        )
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc) from exc
    return _replayed(record, pack)


def _replayed(record: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any]:
    difference = drift(record["used"], used_of(pack))
    return {
        "contextPack": public_record(record),
        "reproduced": not any(difference.values()),
        "drift": difference,
        "pack": within_budget(pack, record["budgetTokens"] or DEFAULT_BUDGET_TOKENS),
    }


# --- a process's recall step (CP-ADR-0076 §4) -------------------------------------------


class ProcessRecallFailed(Exception):
    """Memory gave no answer: ``reason`` names why, ``retryable`` whether to ask again.

    ``memory_unavailable`` (transport, 5xx, the deadline) and ``memory_disabled``
    (no provider) are asked again until the step's own timeout; a request
    memory rejects (``memory_rejected``) will be rejected again.
    """

    def __init__(self, reason: str, *, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.retryable = retryable


@dataclass
class ProcessRecallCall:
    """A ``recall`` intent ready for memory: what the transactional half resolved."""

    scope: GraphScope
    anchors: list[Candidate]
    traverse: list[dict[str, Any]]
    as_of: str | None
    query: str = ""
    kinds: list[str] = field(default_factory=list)
    limit: int | None = None
    # recall.where as the engine computed it: sent to memory as it is.
    where: list[dict[str, Any]] = field(default_factory=list)


def process_recall_call(intent: dict[str, Any], scope: GraphScope) -> ProcessRecallCall:
    """The call of a ``recall`` intent, as the engine made it (``anchors``, ``traverse``…).

    An anchor is ``{kind, key, via?}`` (``{case: true}`` came with the case's
    kind and key); one whose expression gave no key is not sent.
    """
    anchors = [
        Candidate(
            value=str(anchor["key"])[:MAX_CANDIDATE_CHARS],
            kind=str(anchor.get("kind") or ""),
            source="recall",
            via=str(anchor["via"]) if anchor.get("via") else None,
        )
        for anchor in intent.get("anchors") or ()
        if isinstance(anchor, dict) and anchor.get("key") not in (None, "")
    ]
    return ProcessRecallCall(
        scope=scope,
        anchors=anchors[:MAX_ANCHORS],
        traverse=[
            step.to_request() for step in traverse_steps(intent.get("traverse"), where="recall")
        ],
        as_of=intent.get("asOf"),
        query=str(intent.get("query") or "").strip(),
        kinds=[str(k) for k in intent.get("kinds") or ()],
        limit=intent.get("limit"),
        where=[dict(c) for c in intent.get("where") or ()],
    )


async def _via(
    call: ProcessRecallCall, provider: GraphProvider, *, deadline: float, trace: str | None
) -> list[Candidate]:
    """Anchors with ``via`` replaced by the entities pointing at them over that relation."""
    anchors = [c for c in call.anchors if c.via is None]
    groups: dict[str, list[Candidate]] = {}
    for candidate in call.anchors:
        if candidate.via is not None:
            groups.setdefault(candidate.via, []).append(candidate)
    for relation, group in groups.items():
        request: dict[str, Any] = {
            "anchors": [c.to_request() for c in group],
            "traverse": [
                {
                    "relation": relation,
                    "direction": "in",
                    "depth": 1,
                    "limit": MAX_STEP_LIMIT,
                    "from": "anchors",
                }
            ],
            "allow_semantic": False,
        }
        if call.as_of:
            request["as_of"] = call.as_of
        reached = await typed(provider, call.scope, request, deadline=deadline, trace_run_id=trace)
        anchors += [
            Candidate(value=str(e["natural_key"]), kind=str(e.get("kind") or ""), via=relation)
            for e in entities_of(reached)
            if not e.get("anchor") and e.get("natural_key")
        ]
    unique: dict[tuple[str, str], Candidate] = {}
    for candidate in anchors:
        unique.setdefault((candidate.kind, candidate.value), candidate)
    return list(unique.values())[:MAX_ANCHORS]


async def fetch_process_recall(
    call: ProcessRecallCall,
    provider: GraphProvider | None,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: ``{nodes, edges, truncated}``, or :class:`ProcessRecallFailed`.

    The explicit links of the anchors first; then, with a ``query``, what it
    finds by similarity, marked ``inferred``.
    """
    if provider is None:
        raise ProcessRecallFailed("memory_disabled", retryable=True)
    deadline = deadline_after(settings)
    trace = trace_run_id or None
    try:
        anchors = await _via(call, provider, deadline=deadline, trace=trace)
        explicit: dict[str, Any] = {}
        if anchors:
            request: dict[str, Any] = {
                "anchors": [c.to_request() for c in anchors],
                "traverse": call.traverse,
                "allow_semantic": False,
            }
            if call.as_of:
                request["as_of"] = call.as_of
            explicit = await typed(
                provider,
                call.scope,
                with_where(request, call.where),
                deadline=deadline,
                trace_run_id=trace,
            )
        semantic = None
        if call.query:
            semantic = await typed(
                provider,
                call.scope,
                with_where(semantic_request(call.query, call.as_of), call.where),
                deadline=deadline,
                trace_run_id=trace,
            )
    except TimeoutError:
        raise ProcessRecallFailed(
            "memory_unavailable", retryable=True, detail="no answer in time"
        ) from None
    except ContextProviderError as exc:
        if exc.retryable:
            raise ProcessRecallFailed(
                "memory_unavailable", retryable=True, detail=str(exc)
            ) from exc
        raise ProcessRecallFailed("memory_rejected", retryable=False, detail=str(exc)) from exc
    return recall_answer(explicit, semantic, kinds=call.kinds, limit=call.limit)
