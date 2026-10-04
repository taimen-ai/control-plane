"""The entities of a workspace's knowledge, listed through the core (CP-ADR-0060, K031).

``POST /knowledge/entities:query`` lists every entity of the named kinds valid
at a moment whose attributes satisfy ``where`` -- "all licenses valid until the
end of the year" -- page by page, with no anchor to start from. Memory computes
the list (``POST /api/memory/entities:query``, MEM-ADR-020); the core decides
where it reads and what the caller sees there, exactly as for ``cp_recall``:
the namespace is the one a snapshot of the workspace lands in (the root of its
tree), the visibility is the caller's (``graph_scope``). The client names
neither.

As everywhere memory is read, the transaction closes before Memory is called.

Amendment 2026-10-03 (TAI-ADR-0066 p.5): an entity is answered in the core's
contract -- ``validFrom``, ``validTo``, ``sources[{source, sourcePath,
snapshotId}]`` -- next to Memory's snake_case fields, kept for the transition.
``include.relations`` adds the entity's relations and their other ends. Memory's
list has no relations, so they come from its typed traversal
(``POST /api/memory/context/typed``): the entity is the one anchor, each
relation one step of depth 1 from it, with the same namespace, visibility and
``asOf`` as the list. An end the caller may not see is not reached, so its
relation is not answered either.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.knowledge import memory_failure
from control_plane.application.context.graph import (
    GraphScope,
    deadline_after,
    entities_of,
    typed,
    within,
)
from control_plane.application.queries.recall import graph_scope
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError, DependencyUnavailableError
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider


@dataclass
class EntitiesQueryCall:
    scope: GraphScope
    kinds: list[str]
    limit: int
    as_of: datetime | None = None
    cursor: str | None = None
    # Literal conditions as the caller gave them: memory applies them.
    where: list[dict[str, Any]] = field(default_factory=list)
    relations: "RelationsInclude | None" = None


@dataclass(frozen=True)
class RelationsInclude:
    """``include`` of the request: relation names (``None`` -- every relation
    the namespace's packs declare), the direction and the cap per entity."""

    names: tuple[str, ...] | None
    direction: str = "both"
    limit: int = 20


# Memory's typed traversal: at most this many steps in one request
# (``TypedContextRequest.MAX_STEPS``), and the traversals of a page in flight
# at once.
TYPED_MAX_STEPS = 10
RELATION_READS_IN_FLIGHT = 8


async def prepare_entities_query(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    kinds: list[str],
    where: list[dict[str, Any]],
    as_of: datetime | None,
    limit: int,
    cursor: str | None,
    relations: RelationsInclude | None = None,
) -> EntitiesQueryCall:
    """Authorize on the workspace and resolve where memory is read (transactional half)."""
    # The right to read the workspace's context, as recall through /context/recall.
    await authorize(
        ctx, Permission.EVENTS_READ, resource=ResourceRef("workspace", str(workspace_id))
    )
    scope = await graph_scope(session, ctx, settings, workspace_id)
    # graph_scope adds the namespace of the workspace tree root after the
    # tenant's when the caller may read it; the tenant namespace holds no
    # snapshot, so the list reads the root's alone.
    if len(scope.namespaces) < 2:
        raise AuthorizationError(
            "The caller may not read this workspace's knowledge",
            details={"workspaceId": str(workspace_id)},
        )
    namespace = scope.namespaces[-1]
    return EntitiesQueryCall(
        scope=GraphScope(namespace=namespace, namespaces=[namespace], visibility=scope.visibility),
        kinds=list(dict.fromkeys(kinds)),
        limit=limit,
        as_of=as_of,
        cursor=cursor,
        where=list(where),
        relations=relations,
    )


def entities_request(call: EntitiesQueryCall) -> dict[str, Any]:
    """Memory's ``EntitiesQueryIn`` without ``namespaces`` (the provider adds them)."""
    request: dict[str, Any] = {"kinds": call.kinds, "limit": call.limit}
    if call.where:
        request["where"] = call.where
    if call.as_of is not None:
        request["asOf"] = call.as_of.isoformat()
    if call.cursor is not None:
        request["cursor"] = call.cursor
    return {**request, **call.scope.visibility}


async def fetch_entities(
    call: EntitiesQueryCall,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: one page, ``{items, nextCursor, asOf}``."""
    deadline = deadline_after(settings)
    try:
        page = await within(
            deadline,
            provider.query_entities(
                namespaces=call.scope.namespaces,
                request=entities_request(call),
                trace_run_id=trace_run_id or None,
            ),
        )
        items = [entity_out(item) for item in page.get("items") or [] if isinstance(item, dict)]
        if call.relations is not None and items:
            await _add_relations(call, call.relations, provider, items, deadline, trace_run_id)
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        # Memory's 400 is a request it cannot read: a cursor of another list.
        raise memory_failure(exc, invalid={400: "entities_query_invalid"}) from exc
    return {
        "items": items,
        "nextCursor": page.get("nextCursor"),
        "asOf": call.as_of.isoformat() if call.as_of else None,
    }


# Memory's ``EntityItem`` fields answered as they are until a later amendment
# removes them (CP-ADR-0060, amendment 2026-10-03).
TRANSITIONAL_FIELDS = (
    "source",
    "source_path",
    "snapshot_id",
    "valid_from",
    "valid_to",
    "namespace",
    "scope",
)
_TRANSITIONAL_SOURCE_FIELDS = ("source_path", "snapshot_id", "scope")


def _source_out(source: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": str(source.get("source") or ""),
        "sourcePath": str(source.get("source_path") or ""),
        "snapshotId": source.get("snapshot_id") or None,
    }
    for name in _TRANSITIONAL_SOURCE_FIELDS:
        if name in source:
            out[name] = source[name]
    return out


def entity_out(item: dict[str, Any]) -> dict[str, Any]:
    """Memory's ``EntityItem`` in the core's contract (``KnowledgeEntityOut``).

    ``sources`` is Memory's list of merged sources; a Memory that has none
    (before MEM-ADR-022) names its one source at the top level.
    """
    raw_sources = [s for s in item.get("sources") or [] if isinstance(s, dict)]
    if not raw_sources and item.get("source"):
        raw_sources = [
            {name: item[name] for name in ("source", *_TRANSITIONAL_SOURCE_FIELDS) if name in item}
        ]
    attributes = item.get("attributes")
    out: dict[str, Any] = {
        "kind": str(item.get("kind") or ""),
        "key": str(item.get("key") or ""),
        "title": str(item.get("title") or ""),
        "attributes": attributes if isinstance(attributes, dict) else {},
        "validFrom": item.get("valid_from") or None,
        "validTo": item.get("valid_to") or None,
        "sources": [_source_out(source) for source in raw_sources],
    }
    for name in TRANSITIONAL_FIELDS:
        if name in item:
            out[name] = item[name]
    return out


async def _relation_names(
    call: EntitiesQueryCall,
    include: RelationsInclude,
    provider: GraphProvider,
    deadline: float,
    trace_run_id: str,
) -> list[str]:
    if include.names is not None:
        return list(include.names)
    # "*": the relations the packs enabled for the namespace declare.
    body = await within(
        deadline,
        provider.namespace_kinds(namespace=call.scope.namespace, trace_run_id=trace_run_id or None),
    )
    catalog = body.get("catalog") if isinstance(body, dict) else None
    names = catalog.get("relations") if isinstance(catalog, dict) else None
    return [n for n in names or [] if isinstance(n, str) and n]


def relations_request(
    item: dict[str, Any], names: list[str], include: RelationsInclude, as_of: datetime | None
) -> dict[str, Any]:
    """Memory's typed traversal from one entity: ``names`` (at most
    ``TYPED_MAX_STEPS``), one step each, depth 1."""
    request: dict[str, Any] = {
        "anchors": [{"kind": item["kind"], "value": item["key"]}],
        "traverse": [
            {"relation": name, "direction": include.direction, "depth": 1, "limit": include.limit}
            for name in names
        ],
        "allow_semantic": False,
    }
    if as_of is not None:
        request["as_of"] = as_of.isoformat()
    return request


def relations_of(pack: dict[str, Any], key: str, include: RelationsInclude) -> list[dict[str, Any]]:
    """The relations of entity ``key`` in a typed pack, other end visible.

    A fact is answered only when its other end is among the pack's entities:
    Memory leaves out an end the caller may not see, and the core does not
    answer a relation it cannot name the end of either.
    """
    ends = {str(e.get("natural_key") or ""): e for e in entities_of(pack)}
    out: list[dict[str, Any]] = []
    for fact in pack.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        subject, obj = str(fact.get("subject") or ""), str(fact.get("object") or "")
        if subject == key and include.direction in ("out", "both"):
            direction, other = "out", obj
        elif obj == key and include.direction in ("in", "both"):
            direction, other = "in", subject
        else:
            continue
        end = ends.get(other)
        if end is None:
            continue
        out.append(
            {
                "relation": str(fact.get("relation") or ""),
                "direction": direction,
                "kind": str(end.get("kind") or ""),
                "key": other,
                "title": str(end.get("title") or ""),
            }
        )
    return out


async def _add_relations(
    call: EntitiesQueryCall,
    include: RelationsInclude,
    provider: GraphProvider,
    items: list[dict[str, Any]],
    deadline: float,
    trace_run_id: str,
) -> None:
    """``relations`` of every item, at most ``include.limit`` each, in
    ``(relation, direction, kind, key)`` order."""
    names = await _relation_names(call, include, provider, deadline, trace_run_id)
    batches = [names[i : i + TYPED_MAX_STEPS] for i in range(0, len(names), TYPED_MAX_STEPS)]
    gate = asyncio.Semaphore(RELATION_READS_IN_FLIGHT)

    async def read(item: dict[str, Any]) -> None:
        found: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        # An item without a key is no anchor: Memory would refuse the traversal.
        for batch in batches if item["key"].strip() else []:
            async with gate:
                pack = await typed(
                    provider,
                    call.scope,
                    relations_request(item, batch, include, call.as_of),
                    deadline=deadline,
                    trace_run_id=trace_run_id or None,
                )
            for relation in relations_of(pack, item["key"], include):
                ident = (
                    relation["relation"],
                    relation["direction"],
                    relation["kind"],
                    relation["key"],
                )
                found.setdefault(ident, relation)
        item["relations"] = [found[ident] for ident in sorted(found)][: include.limit]

    try:
        async with asyncio.TaskGroup() as group:
            for item in items:
                group.create_task(read(item))
    except* (TimeoutError, ContextProviderError) as failed:
        # The first failure is the page's: the others were cancelled with it.
        raise failed.exceptions[0] from None


async def relations_of_items(
    call: EntitiesQueryCall,
    include: RelationsInclude,
    provider: GraphProvider,
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    trace_run_id: str = "",
) -> None:
    """``relations`` of every item (``{kind, key}``), as ``include.relations`` answers them:
    the column ``relations.<name>`` of a view of knowledge (CP-ADR-0080, amendment Б)."""
    try:
        await _add_relations(call, include, provider, items, deadline_after(settings), trace_run_id)
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc, invalid={400: "entities_query_invalid"}) from exc


async def relations_from(
    call: EntitiesQueryCall,
    provider: GraphProvider,
    settings: Settings,
    *,
    kind: str,
    key: str,
    trace_run_id: str = "",
) -> list[dict[str, Any]]:
    """The relations of one entity named by ``kind`` and ``key`` (the ``related`` block of a
    view, CP-ADR-0080 amendment A), as ``include.relations`` answers them for an item of a list."""
    include = call.relations or RelationsInclude(names=None)
    item: dict[str, Any] = {"kind": kind, "key": key}
    try:
        await _add_relations(
            call, include, provider, [item], deadline_after(settings), trace_run_id
        )
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc, invalid={400: "entities_query_invalid"}) from exc
    relations: list[dict[str, Any]] = item.get("relations") or []
    return relations
