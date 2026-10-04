"""Explicit context observations: the governed "remember this" primitive.

A harness (human or agent — same path, no principal.kind privileges) records
an intentional, externalized piece of knowledge: a finding, a decision, a
constraint. The record is a REGULAR domain event in the append-only journal
(``observation.recorded``), which makes it:

* authoritative and replayable — a Memory Service rebuild re-derives it from
  the Control Plane journal, it never lives only in external memory;
* provenance-safe — tenant/principal/session come from the authenticated
  context, the client cannot spoof another actor;
* transactional — recorded under the same one-command-one-transaction rule
  as every other write.

What it is NOT: a dump of hidden reasoning. Only explicitly submitted
content is accepted; harness integrations must never auto-record chains of
thought, raw prompts or terminal history.

External observations (CP-ADR-0057) additionally name their ``source`` system,
the time the fact was seen there (``observedAt``), the observed object
(``externalRef``) and an earlier observation they replace (``supersedes``).
A ``(source, dedupKey)`` pair is unique per tenant and author: repeating it
returns the observation it first produced instead of appending a new event;
the same pair from another author is that author's own observation.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.relations import resolve_task
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.assertions import validate_assertions
from control_plane.application.events import record_event
from control_plane.application.queries.events import recorded_observations
from control_plane.application.visibility import task_visible
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import (
    ObservationDedupKey,
    Run,
    Session,
)

# Same shape the Memory Service enforces for observation kinds.
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

# Recommended baseline kinds (not a closed ontology; any _KIND_RE kind works).
BASELINE_KINDS = frozenset(
    {
        "finding",
        "decision",
        "constraint",
        "note",
        "summary",
        "result",
        "preference",
        "external_fact",
    }
)

_MAX_CONTENT_CHARS = 65_536
_MAX_DATA_KEYS = 100

# Lower-case only: the source is half of the dedup identity, and "GitHub" vs
# "github" must not silently split one object into two observation streams.
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")


@dataclass(frozen=True)
class RecordedObservation:
    id: uuid.UUID
    event_id: uuid.UUID
    kind: str
    recorded_at: datetime
    # True when (source, dedupKey) resolved to an earlier observation.
    deduplicated: bool = False


async def record_observation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    kind: str,
    content: str,
    data: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
    task_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    source: str | None = None,
    dedup_key: str | None = None,
    observed_at: datetime | None = None,
    supersedes: uuid.UUID | None = None,
    external_ref: dict[str, Any] | None = None,
) -> RecordedObservation:
    await authorize(ctx, Permission.OBSERVATIONS_WRITE)

    if not _KIND_RE.match(kind):
        raise ValidationError(
            "observation_invalid",
            "kind must match ^[a-z0-9][a-z0-9._-]{0,127}$",
            details={"baselineKinds": sorted(BASELINE_KINDS)},
        )
    if not content.strip():
        raise ValidationError("observation_invalid", "content must not be empty")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValidationError(
            "observation_invalid",
            f"content exceeds {_MAX_CONTENT_CHARS} characters",
            details={"limit": _MAX_CONTENT_CHARS},
        )
    if data is not None and len(data) > _MAX_DATA_KEYS:
        raise ValidationError("observation_invalid", "structured data has too many keys")

    if source is not None and not _SOURCE_RE.match(source):
        raise ValidationError(
            "observation_invalid", "source must match ^[a-z0-9][a-z0-9._:/-]{0,127}$"
        )
    if dedup_key is not None and not dedup_key.strip():
        raise ValidationError("observation_invalid", "dedupKey must not be blank")
    # A dedup key or an external object only mean something relative to the
    # system they came from.
    if source is None and (dedup_key is not None or external_ref is not None):
        raise ValidationError(
            "observation_invalid", "source is required with dedupKey or externalRef"
        )

    validated_assertions = validate_assertions(assertions)

    now = utcnow()
    payload: dict[str, Any] = {
        "kind": kind,
        "content": content,
        "observedAt": (observed_at or now).isoformat(),
    }
    if source is not None:
        payload["source"] = source
    if dedup_key is not None:
        payload["dedupKey"] = dedup_key
    if external_ref is not None:
        payload["externalRef"] = {k: v for k, v in external_ref.items() if v is not None}
    if validated_assertions:
        payload["assertions"] = validated_assertions
    if data:
        payload["data"] = data

    # Scope references are resolved inside the caller's tenant — a foreign or
    # unknown id is a 404, so cross-tenant attachment is impossible.
    task_id: uuid.UUID | None = None
    if task_ref is not None:
        task_id = (await resolve_task(session, ctx, task_ref)).id
        payload["taskId"] = str(task_id)
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        # A run of invisible work answers as a missing one (CP-ADR-0082 V6).
        if run is None or not await task_visible(session, ctx, run.task_id):
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        payload["runId"] = str(run.id)
        if task_id is None:
            task_id = run.task_id
            payload["taskId"] = str(run.task_id)
    if task_id is not None:
        # Binding a fact to a task speaks about that task: a rule may close
        # the task on it (CP-ADR-0063 Zh6), so the author must be allowed to
        # read the task — the decision of the PDP, as for reading it.
        await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(task_id)))
    if workspace_id is not None:
        from control_plane.application.commands.workspaces import get_tenant_workspace

        await get_tenant_workspace(session, ctx, workspace_id)
        payload["workspaceId"] = str(workspace_id)
    if session_id is not None:
        owned = await session.scalar(
            select(Session).where(
                Session.id == session_id,
                Session.tenant_id == ctx.tenant_id,
                Session.principal_id == ctx.principal_id,
            )
        )
        if owned is None:
            raise NotFoundError("Session not found", details={"sessionId": str(session_id)})
    if supersedes is not None:
        # Same rule as the scope references: unknown, foreign or invisible
        # (the journal would not hand it to the caller) is a 404.
        if not await recorded_observations(session, ctx, {supersedes}):
            raise NotFoundError(
                "Superseded observation not found", details={"supersedes": str(supersedes)}
            )
        payload["supersedes"] = str(supersedes)

    observation_id = uuid.uuid4()
    event_id = new_uuid()
    if source is not None and dedup_key is not None:
        # Claim the key before appending: a concurrent twin blocks on the
        # uncommitted row and then sees the conflict, so exactly one event
        # is written per (tenant, source, dedupKey, author). The author is
        # part of the key, so nobody can take another author's key in advance
        # (CP-ADR-0057 amendment 2026-10-01).
        claimed = await session.scalar(
            insert(ObservationDedupKey)
            .values(
                tenant_id=ctx.tenant_id,
                source=source,
                dedup_key=dedup_key,
                actor_id=ctx.principal_id,
                observation_id=observation_id,
                event_id=event_id,
                kind=kind,
                recorded_at=now,
            )
            .on_conflict_do_nothing()
            .returning(ObservationDedupKey.observation_id)
        )
        if claimed is None:
            existing = await session.scalar(
                select(ObservationDedupKey).where(
                    ObservationDedupKey.tenant_id == ctx.tenant_id,
                    ObservationDedupKey.source == source,
                    ObservationDedupKey.dedup_key == dedup_key,
                    ObservationDedupKey.actor_id == ctx.principal_id,
                )
            )
            assert existing is not None  # the conflicting row is committed
            return RecordedObservation(
                id=existing.observation_id,
                event_id=existing.event_id,
                kind=existing.kind,
                recorded_at=existing.recorded_at,
                deduplicated=True,
            )

    # The observation IS the event: append-only, replayable, provenance from
    # the authenticated context (actor cannot be spoofed by the client).
    event = await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="observation.recorded",
        entity_type="observation",
        entity_id=observation_id,
        actor_id=ctx.principal_id,
        session_id=session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload=payload,
        event_id=event_id,
        occurred_at=now,
    )
    return RecordedObservation(
        id=observation_id, event_id=event.id, kind=kind, recorded_at=event.occurred_at
    )
