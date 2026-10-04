"""Task context pack: the knowledge an executor starts from (TAI-ADR-0042 p.5, CP-ADR-0064).

A task whose type version declares a ``context_schema`` gets, next to the
free-text recall of ``POST /context``, a pack compiled by Memory's typed
traversal: the core takes the profile from the type version, extracts anchor
candidates from the task (deterministically, with the ``idPatterns`` of the
domain packs), asks the Context Compiler to resolve them and follow the
declared relations at the declared moment, and records what was asked and
what was used as evidence of the task's work.

The pack is bound to the claim. The first read of the working context by the
holder of a claim compiles it and records it (``task_context_packs``, one row
per claim, linked to the task and the claim; the task document itself is not
written, and anyone may cite a pack in ``evidence`` explicitly); every later read
within the same claim — a restarted executor, the MCP ``cp_get_context`` —
sends the recorded request again instead of compiling a new one, so the
executor sees the same pack for the whole claim. A new claim compiles anew.
Reading a task one does not hold (a reviewer) compiles the pack without
recording it; the recorded one is reproduced by ``:replay``.

The moment is pinned in the request (``as_of``): ``taskCreated`` — the task's
creation, ``now`` — the moment the claim was taken, ``origin`` — the time of
the observation the task came from. Memory keeps closed facts with their
validity, so the same request gives the same entities and facts later.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    permits_task,
)
from control_plane.application.commands.approval_outcomes import (
    spawned_by_of,
    task_view,
    walk_path,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.graph import (
    SEMANTIC_REQUEST,
    GraphScope,
    KindPatterns,
    deadline_after,
    entities_of,
    kind_patterns,
    off_loop,
    semantic_request,
    typed,
    typed_pack,
    used_of,
    within,
    within_budget,
)
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principal_key_share
from control_plane.application.visibility import task_visible
from control_plane.config import Settings
from control_plane.domain.context_schema import (
    AS_OF_NOW,
    AS_OF_ORIGIN,
    DEFAULT_BUDGET_TOKENS,
    MAX_ANCHORS,
    MAX_STEP_LIMIT,
    STEP_SOURCE,
    Candidate,
    ContextSchema,
    anchor_candidates,
    parse_context_schema,
    parse_source,
    step_profile,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import DomainError, NotFoundError
from control_plane.domain.work_graph import EvidenceKind
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import (
    Artifact,
    Event,
    EventArchive,
    Task,
    TaskClaim,
    TaskContextPack,
    TaskType,
)

logger = logging.getLogger(__name__)

RECORDED_EVENT = "task.context_pack_recorded"


@dataclass
class TaskContextCall:
    """Everything the non-transactional half needs, read in the transaction."""

    task_id: uuid.UUID
    task_type_id: uuid.UUID
    claim_id: uuid.UUID | None
    store: bool
    schema: ContextSchema
    sources: dict[str, Any]
    as_of: datetime
    as_of_mode: str
    scope: GraphScope
    replay: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    # The text a process step's context reads by similarity (CP-ADR-0076 §6).
    semantic_text: str | None = None


async def task_profile(session: AsyncSession, task: Task) -> ContextSchema | None:
    """The context profile of the task: its process step's, else its type version's.

    A task of a process step carries the step's ``context`` (CP-ADR-0076 §6),
    which replaces the profile of its type.
    """
    if task.context_profile:
        try:
            return step_profile(task.context_profile)
        except DomainError:  # the engine computed it from a checked version
            logger.warning("task %s carries an unreadable context profile", task.id)
            return None
    task_type = await session.get(TaskType, task.type_id)
    if task_type is None or not task_type.context_schema:
        return None
    try:
        return parse_context_schema(task_type.context_schema)
    except DomainError:  # published documents were validated; a stale grammar is logged
        logger.warning("task type %s carries an unreadable context_schema", task_type.id)
        return None


def unavailable(status: str, schema: ContextSchema | None = None) -> dict[str, Any]:
    """The ``taskContext`` stanza when no pack is compiled."""
    return {
        "status": status,
        "contextPackId": None,
        "budgetTokens": schema.budget_tokens if schema else None,
        "pack": None,
    }


async def _origin_moment(session: AsyncSession, task: Task) -> datetime | None:
    """When the observation the task originated from was seen, if any."""
    for item in (task.origin or {}).get("evidence") or []:
        if item.get("kind") != EvidenceKind.OBSERVATION:
            continue
        observation_id = uuid.UUID(item["observationId"])
        tables: tuple[type[Event] | type[EventArchive], ...] = (Event, EventArchive)
        for table in tables:
            row: Event | EventArchive | None = await session.scalar(
                select(table).where(
                    table.tenant_id == task.tenant_id,
                    table.entity_type == "observation",
                    table.event_type == "observation.recorded",
                    table.entity_id == observation_id,
                )
            )
            if row is None:
                continue
            observed = (row.payload or {}).get("observedAt")
            occurred: datetime = row.occurred_at
            try:
                moment = datetime.fromisoformat(observed) if observed else occurred
            except (TypeError, ValueError):
                return occurred
            # Intake refuses a naive observedAt (ADR-0057); an older one is read as UTC.
            return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return None


async def _sources(
    session: AsyncSession, ctx: AuthContext, task: Task, schema: ContextSchema, warnings: list[str]
) -> dict[str, Any]:
    """The value of every anchor's ``from``, read with the caller's rights.

    The task it spawned from is read only with ``tasks.read`` on it, artifacts
    only with ``artifacts.read`` — a pack must not carry what its reader could
    not have seen. A source the caller cannot read contributes nothing.
    """
    views: dict[str, dict[str, Any]] = {"task": task_view(task), "spawnedBy": {}}
    owners: dict[str, Task | None] = {"task": task, "spawnedBy": None}
    if "spawnedBy" in schema.roots:
        spawned = await spawned_by_of(session, task)
        if spawned is not None:
            # Of an invisible workspace it is not readable either (CP-ADR-0082 §3.7).
            if await permits_task(ctx, Permission.TASKS_READ, task=spawned):
                views["spawnedBy"] = task_view(spawned)
                owners["spawnedBy"] = spawned
            else:
                warnings.append("the task this one was spawned by is not readable")
    artifacts: dict[tuple[str, str], dict[str, Any]] = {}
    wanted = {
        (spec.path.root, spec.path.artifact_type)
        for spec in schema.anchors
        if spec.path.artifact_type is not None
    }
    if wanted and not ctx.has(Permission.ARTIFACTS_READ):
        warnings.append("artifact anchors need the artifacts.read permission")
        wanted = set()
    for root, artifact_type in sorted(wanted):
        owner = owners[root]
        if owner is None:
            continue
        metadata = await session.scalar(
            select(Artifact.metadata_json)
            .where(
                Artifact.tenant_id == task.tenant_id,
                Artifact.task_id == owner.id,
                Artifact.type == artifact_type,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
        artifacts[(root, artifact_type)] = dict(metadata or {})
    sources: dict[str, Any] = {}
    for spec in schema.anchors:
        path = spec.path
        if path.artifact_type is not None:
            metadata = artifacts.get((path.root, path.artifact_type)) or {}
            sources[spec.source] = metadata.get(path.metadata_key or "")
        else:
            sources[spec.source] = walk_path(views[path.root], path)
    return sources


async def prepare_task_context(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    schema: ContextSchema,
    scope: GraphScope,
) -> TaskContextCall:
    """Transactional half: sources, the moment, the claim and a recorded pack."""
    warnings: list[str] = []
    claim = (
        await session.get(TaskClaim, task.active_claim_id)
        if task.active_claim_id is not None
        else None
    )
    if claim is not None and claim.status != "active":
        claim = None
    as_of = task.created_at
    if schema.as_of == AS_OF_NOW:
        as_of = claim.acquired_at if claim is not None else utcnow()
    elif schema.as_of == AS_OF_ORIGIN:
        as_of = await _origin_moment(session, task) or task.created_at
    replay: dict[str, Any] | None = None
    if claim is not None:
        record = await session.scalar(
            select(TaskContextPack).where(
                TaskContextPack.tenant_id == ctx.tenant_id,
                TaskContextPack.claim_id == claim.id,
            )
        )
        if record is not None:
            # The pack was compiled with the holder's rights; any other reader
            # (a reviewer, an operator) gets it with the same redaction as
            # GET /context-packs, never the anchor values themselves.
            replay = await visible_pack_record(session, ctx, record)
            if replay["redactedAnchors"]:
                warnings.append(
                    f"{replay['redactedAnchors']} anchor(s) of the recorded pack come from "
                    "sources this reader cannot read and are withheld"
                )
    return TaskContextCall(
        task_id=task.id,
        task_type_id=task.type_id,
        claim_id=claim.id if claim is not None else None,
        store=claim is not None and claim.holder_id == ctx.principal_id,
        schema=schema,
        sources={} if replay else await _sources(session, ctx, task, schema, warnings),
        as_of=replay["asOfValue"] if replay else as_of,
        as_of_mode=schema.as_of,
        scope=scope,
        replay=replay,
        warnings=warnings,
        semantic_text=task.title if schema.semantic else None,
    )


def pack_record(record: TaskContextPack) -> dict[str, Any]:
    """A recorded pack as the API shows it (``asOfValue`` is internal)."""
    return {
        "id": str(record.id),
        "taskId": str(record.task_id),
        "claimId": str(record.claim_id),
        "taskTypeId": str(record.task_type_id),
        "compiledBy": str(record.compiled_by),
        "asOf": record.as_of.isoformat(),
        "asOfValue": record.as_of,
        "asOfMode": record.as_of_mode,
        "namespaces": list(record.namespaces),
        "request": dict(record.request),
        "anchors": list(record.candidates),
        "used": dict(record.used),
        "unresolved": list(record.unresolved),
        "budgetTokens": record.budget_tokens,
        "traceId": record.trace_id,
        "createdAt": record.created_at.isoformat(),
    }


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "asOfValue"}


def _candidate_doc(candidate: Candidate) -> dict[str, Any]:
    doc: dict[str, Any] = {"value": candidate.value, "source": candidate.source}
    if candidate.kind:
        doc["kind"] = candidate.kind
    if candidate.via:
        doc["via"] = candidate.via
    return doc


async def _anchors(
    call: TaskContextCall,
    provider: GraphProvider,
    *,
    deadline: float,
    trace_run_id: str | None,
) -> list[Candidate]:
    """Anchor candidates of the task, with ``via`` anchors replaced by what they reach.

    ``via: <relation>`` asks Memory for the entities that point at the anchor
    over that relation (a changed file -> what is defined in it) and anchors
    the pack on them instead.
    """
    catalog = KindPatterns()
    if any(spec.textual for spec in call.schema.anchors):
        catalog = await within(
            deadline,
            kind_patterns(
                provider, call.scope.namespaces, trace_run_id=trace_run_id, warnings=call.warnings
            ),
        )
    candidates = await off_loop(
        deadline,
        anchor_candidates,
        call.schema,
        call.sources,
        catalog.patterns,
        aliases=catalog.aliases,
        warnings=call.warnings,
    )
    candidates = [*call.schema.values, *candidates]
    anchors = [c for c in candidates if c.via is None]
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        if candidate.via is not None:
            groups.setdefault((candidate.via, candidate.source), []).append(candidate)
    for (relation, source), group in groups.items():
        reached = await typed(
            provider,
            call.scope,
            {
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
                "as_of": call.as_of.isoformat(),
                "allow_semantic": False,
            },
            deadline=deadline,
            trace_run_id=trace_run_id,
        )
        for entity in entities_of(reached):
            if entity.get("anchor") or not entity.get("natural_key"):
                continue
            anchors.append(
                Candidate(
                    value=str(entity["natural_key"]),
                    kind=str(entity.get("kind") or ""),
                    source=source,
                    via=relation,
                )
            )
    unique: dict[tuple[str, str], Candidate] = {}
    for candidate in anchors:
        unique.setdefault((candidate.kind, candidate.value), candidate)
    return list(unique.values())[:MAX_ANCHORS]


async def fetch_task_context(
    call: TaskContextCall,
    provider: GraphProvider,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: compile (or replay) the pack; never fails the caller."""
    result = unavailable("unavailable", call.schema)
    result.update(
        {
            "claimId": str(call.claim_id) if call.claim_id else None,
            "asOf": call.as_of.isoformat(),
            "asOfMode": call.as_of_mode,
            "replayed": call.replay is not None,
            "recorded": False,
            "anchors": [],
            "warnings": call.warnings,
        }
    )
    deadline = deadline_after(settings)
    trace = trace_run_id or None
    try:
        if call.replay is not None:
            record = call.replay
            result.update(
                contextPackId=record["id"],
                recorded=True,
                anchors=record["anchors"],
                budgetTokens=record["budgetTokens"],
                redactedAnchors=record["redactedAnchors"],
            )
            if not record["request"].get("anchors") and not record["request"].get(SEMANTIC_REQUEST):
                result["status"] = "empty"
                return result
            scope = GraphScope(
                namespace=record["namespaces"][0],
                namespaces=record["namespaces"],
                visibility=call.scope.visibility,
            )
            pack = await typed_pack(
                provider, scope, record["request"], deadline=deadline, trace_run_id=trace
            )
            result.update(
                status="ok",
                pack=within_budget(pack, record["budgetTokens"] or DEFAULT_BUDGET_TOKENS),
            )
            return result
        anchors = await _anchors(call, provider, deadline=deadline, trace_run_id=trace)
        result["anchors"] = [_candidate_doc(c) for c in anchors]
        semantic = (call.semantic_text or "").strip()
        if not anchors and not semantic:
            result["status"] = "empty"
            return result
        request: dict[str, Any] = {
            "anchors": [c.to_request() for c in anchors],
            "traverse": [step.to_request() for step in call.schema.traverse],
            "as_of": call.as_of.isoformat(),
            "allow_semantic": False,
        }
        if semantic:
            # A process step's context: the explicit links of the case first,
            # then what its text finds by similarity, marked (CP-ADR-0076 §6).
            request[SEMANTIC_REQUEST] = semantic_request(semantic, call.as_of.isoformat())
        pack = await typed_pack(
            provider, call.scope, request, deadline=deadline, trace_run_id=trace
        )
        budget = call.schema.budget_tokens or DEFAULT_BUDGET_TOKENS
        result.update(status="ok", pack=within_budget(pack, budget))
    except TimeoutError:
        result["status"] = "timeout"
        return result
    except ContextProviderError as exc:
        logger.warning("task context pack unavailable: %s", exc)
        call.warnings.append(f"task context unavailable: {exc}")
        return result
    except Exception:  # a misbehaving provider must never fail POST /context
        logger.exception("task context pack failed unexpectedly")
        return result
    if call.store and call.claim_id is not None:
        stored = await _record(call, ctx, session_factory, request, result["anchors"], pack)
        if stored is not None:
            pack_id, recorded = stored
            result.update(contextPackId=str(pack_id), recorded=True, replayed=not recorded)
    return result


async def _record(
    call: TaskContextCall,
    ctx: AuthContext,
    session_factory: async_sessionmaker[AsyncSession],
    request: dict[str, Any],
    anchors: list[dict[str, Any]],
    pack: dict[str, Any],
) -> tuple[uuid.UUID, bool] | None:
    """Record the pack of the claim.

    ``(id, True)`` when this call recorded it; ``(id, False)`` when a
    concurrent read of the same claim was first — its record stands; ``None``
    when the claim ended while Memory was compiling (nothing is recorded).

    The record is the pack's link to the task (``task_id``, ``claim_id``): the
    task document is not touched, so its version — the optimistic lock of
    ``PATCH /tasks`` — does not move under an agent that read the context
    between reading the task and updating it, and re-claims add nothing to
    ``evidence``.
    """
    assert call.claim_id is not None
    used = used_of(pack)
    async with transaction(session_factory) as db:
        # The record references the caller: its principal before the task
        # (rule 1 of ``application/locking.py``, CP-ADR-0077 §3) — this
        # transaction does not go through the write flow. A caller disabled in
        # the meantime has lost its claim, and the check below records nothing.
        await lock_principal_key_share(db, ctx.tenant_id, ctx.principal_id)
        # Claim, release and takeover lock the task row; expiry is lazy (the
        # deadline passed). Under the lock the claim this pack was compiled
        # for is still the live one, or nothing is recorded.
        task = await db.scalar(
            select(Task)
            .where(Task.id == call.task_id, Task.tenant_id == ctx.tenant_id)
            .with_for_update()
        )
        claim = await db.get(TaskClaim, call.claim_id)
        if (
            task is None
            or claim is None
            or task.active_claim_id != call.claim_id
            or claim.status != "active"
            or claim.expires_at <= utcnow()
            or claim.holder_id != ctx.principal_id
        ):
            call.warnings.append("the claim ended before the context pack was recorded")
            return None
        pack_id = await db.scalar(
            pg_insert(TaskContextPack)
            .values(
                id=new_uuid(),
                tenant_id=ctx.tenant_id,
                task_id=call.task_id,
                claim_id=call.claim_id,
                task_type_id=call.task_type_id,
                compiled_by=ctx.principal_id,
                as_of=call.as_of,
                as_of_mode=call.as_of_mode,
                namespaces=list(call.scope.namespaces),
                request=request,
                candidates=anchors,
                used=used,
                unresolved=[u for u in pack.get("unresolved") or [] if isinstance(u, dict)],
                budget_tokens=call.schema.budget_tokens,
                trace_id=str(pack.get("trace_id") or "") or None,
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(constraint="uq_task_context_packs_claim")
            .returning(TaskContextPack.id)
        )
        if pack_id is None:
            existing = await db.scalar(
                select(TaskContextPack.id).where(TaskContextPack.claim_id == call.claim_id)
            )
            assert existing is not None
            return existing, False
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type=RECORDED_EVENT,
            entity_type="task",
            entity_id=call.task_id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "publicId": task.public_id,
                "contextPackId": str(pack_id),
                "claimId": str(call.claim_id),
                "asOf": call.as_of.isoformat(),
                "asOfMode": call.as_of_mode,
                "entities": len(used["entities"]),
                "facts": len(used["facts"]),
                "snapshots": len(used["snapshots"]),
            },
        )
    return pack_id, True


async def _hidden_sources(
    session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID, sources: set[str]
) -> set[str]:
    """Anchor sources of a recorded pack its reader could not have read.

    The pack was compiled with the rights of the claim holder: artifact
    metadata needs ``artifacts.read``, the task it was spawned by ``tasks.read``
    on that task. A source that no longer parses is hidden too.
    """
    hidden: set[str] = set()
    spawned_readable: bool | None = None
    for source in sorted(sources):
        if source == STEP_SOURCE:
            continue  # computed by the process from its data, not read from a source
        try:
            path = parse_source(source, where="source")
        except DomainError:
            hidden.add(source)
            continue
        if path.artifact_type is not None and not ctx.has(Permission.ARTIFACTS_READ):
            hidden.add(source)
            continue
        if path.root == "spawnedBy":
            if spawned_readable is None:
                spawned_readable = await _spawned_readable(session, ctx, task_id)
            if not spawned_readable:
                hidden.add(source)
    return hidden


async def _spawned_readable(session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID) -> bool:
    task = await session.get(Task, task_id)
    spawned = await spawned_by_of(session, task) if task is not None else None
    if spawned is None:
        return True
    return await permits_task(ctx, Permission.TASKS_READ, task=spawned)


def _without(record: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """The record without the anchors (and their echoes) of hidden sources.

    ``anchors`` and ``request.anchors`` are parallel lists (one request anchor
    per candidate); ``unresolved`` and the anchor entities of ``used`` repeat
    anchor values, so a value of a hidden source goes from them as well.
    """
    anchors = record["anchors"]
    request = dict(record["request"])
    sent = list(request.get("anchors") or [])
    keep = [i for i, c in enumerate(anchors) if c.get("source") not in hidden]
    values = {str(c.get("value")) for c in anchors if c.get("source") in hidden}
    request["anchors"] = (
        [sent[i] for i in keep]
        if len(sent) == len(anchors)
        else [a for a in sent if str(a.get("value")) not in values]
    )
    used = dict(record["used"])
    used["entities"] = [
        e for e in used.get("entities") or [] if str(e.get("natural_key")) not in values
    ]
    return {
        **record,
        "request": request,
        "anchors": [anchors[i] for i in keep],
        "used": used,
        "unresolved": [u for u in record["unresolved"] if str(u.get("value")) not in values],
        "redactedAnchors": len(anchors) - len(keep),
    }


async def get_pack_record(
    session: AsyncSession, ctx: AuthContext, pack_id: uuid.UUID
) -> dict[str, Any]:
    """A recorded pack, with what its reader may see of it.

    What the pack used is durable memory (``events.read``, as ``/context``
    and ``cp_recall``), its task needs ``tasks.read``, and anchors taken from
    sources the reader could not have read are withheld (``redactedAnchors``).
    """
    await authorize(ctx, Permission.TASKS_READ)
    await authorize(ctx, Permission.EVENTS_READ)
    record = await session.scalar(
        select(TaskContextPack).where(
            TaskContextPack.id == pack_id, TaskContextPack.tenant_id == ctx.tenant_id
        )
    )
    # A pack of invisible work answers as a missing pack (CP-ADR-0082 §4).
    if record is None or not await task_visible(session, ctx, record.task_id):
        raise NotFoundError("Context pack not found", details={"contextPackId": str(pack_id)})
    await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(record.task_id)))
    return await visible_pack_record(session, ctx, record)


async def visible_pack_record(
    session: AsyncSession, ctx: AuthContext, record: TaskContextPack
) -> dict[str, Any]:
    """The recorded pack without the anchors its reader could not have read."""
    doc = pack_record(record)
    sources = {str(c.get("source")) for c in doc["anchors"] if isinstance(c, dict)}
    return _without(doc, await _hidden_sources(session, ctx, record.task_id, sources))
