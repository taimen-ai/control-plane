"""Replay of a candidate version on real journals: ``POST /process-definitions/{key}:replay``.

CP-ADR-0074 §10, process-packages P014. The body is a candidate ``spec`` of
the process ``key``. The core checks it as a publication would (the latest
version below the candidate keeps its element ids), takes the journals of the
instances asked for — ``instanceIds``, or the latest ``limit`` instances of
the current version in the workspaces where the caller reads processes —
feeds their recorded inputs to the candidate with the same
:func:`process_engine.step` a live instance runs, and compares the decisions
and intents with the recorded ones (:mod:`control_plane.domain.process_replay`).

The candidate runs under the number and the engine revision of each
instance's version (:func:`process_replay.as_version`): only the behaviour of
the spec differs, not the number in the events nor the revision the journal
was recorded under. Every instance reports its first divergence, if any: the
journal entry where the paths part, the element, what was recorded and what
the candidate decides. Memory is never asked — the answers of ``recall`` are
inputs of the journal — and nothing is written: the transaction is ``READ
ONLY``.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext, authorize, visible_objects
from control_plane.application.commands.process_definitions import (
    load_catalog,
    previous_version,
    resolve_process_definition,
)
from control_plane.application.commands.process_instances import (
    get_instance,
    journal_calendars,
    journal_records,
    journal_settings,
)
from control_plane.domain import process_engine as engine
from control_plane.domain import process_replay
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ConflictError, NotFoundError
from control_plane.domain.process_definition import (
    Problem,
    SpecError,
    check_process,
    definition_hash,
    normalized_spec,
)
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import ProcessDefinition, ProcessInstance


@dataclass(frozen=True)
class InstanceReplay:
    instance: ProcessInstance
    events: int
    divergence: process_replay.Divergence | None

    def out(self) -> dict[str, Any]:
        return {
            "instanceId": str(self.instance.id),
            "instanceKey": self.instance.instance_key,
            "version": self.instance.definition_version,
            "events": self.events,
            "divergences": [self.divergence.out()] if self.divergence is not None else [],
        }


@dataclass
class ReplayReport:
    """``ProcessReplayOut``."""

    key: str
    candidate_hash: str
    problems: list[Problem] = field(default_factory=list)
    instances: list[InstanceReplay] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "candidateHash": self.candidate_hash,
            "replayed": len(self.instances),
            "diverged": sum(1 for item in self.instances if item.divergence is not None),
            "problems": [problem.out() for problem in self.problems],
            "instances": [item.out() for item in self.instances],
        }


async def chosen_instances(
    db: AsyncSession,
    ctx: AuthContext,
    key: str,
    version: int,
    instance_ids: list[uuid.UUID] | None,
    limit: int,
) -> list[ProcessInstance]:
    """The instances asked for, or the latest ``limit`` of ``version`` the caller may read."""
    if instance_ids is not None:
        found = []
        for instance_id in dict.fromkeys(instance_ids):
            instance = await get_instance(db, ctx, instance_id)
            if instance.definition_key != key:
                raise NotFoundError(
                    f"Process instance {instance_id} is not an instance of process {key!r}",
                    details={"instanceId": str(instance_id), "process": key},
                )
            found.append(instance)
        return found
    stmt = select(ProcessInstance).where(
        ProcessInstance.tenant_id == ctx.tenant_id,
        ProcessInstance.definition_key == key,
        ProcessInstance.definition_version == version,
    )
    workspaces = await visible_objects(ctx, Permission.PROCESSES_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                ProcessInstance.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                ProcessInstance.workspace_id.is_(None),
            )
        )
    rows = await db.scalars(
        stmt.order_by(ProcessInstance.started_at.desc(), ProcessInstance.id.desc()).limit(limit)
    )
    return list(rows.all())


async def engine_revision_of(
    db: AsyncSession, instance: ProcessInstance, revisions: dict[uuid.UUID, int]
) -> int:
    """The engine revision of the version ``instance`` is pinned to; ``revisions`` caches them."""
    revision = revisions.get(instance.definition_id)
    if revision is None:
        revision = await db.scalar(
            select(ProcessDefinition.engine_revision).where(
                ProcessDefinition.id == instance.definition_id
            )
        )
        if revision is None:
            raise ConflictError(
                "process_definition_unusable",
                f"Process instance {instance.id} is pinned to a version that is not published",
                details={
                    "process": f"{instance.definition_key}@{instance.definition_version}",
                    "instanceId": str(instance.id),
                },
            )
        revisions[instance.definition_id] = revision
    return revision


async def replay_one(
    db: AsyncSession,
    definition: engine.Definition,
    instance: ProcessInstance,
    revisions: dict[uuid.UUID, int],
) -> InstanceReplay:
    """Replay the journal of ``instance`` on ``definition`` under the instance's version."""
    entries = await journal_records(db, instance.id)
    calendars = await journal_calendars(db, instance.tenant_id, entries)
    # The values each record saw; the candidate keeps the types it was checked with.
    settings = await journal_settings(db, instance.tenant_id, definition, entries)
    revision = await engine_revision_of(db, instance, revisions)
    candidate = process_replay.as_version(definition, instance.definition_version, revision)
    result = process_replay.replay(
        candidate, entries, calendars, stop=True, settings=settings.values
    )
    last = int(entries[-1]["seq"]) if entries else 0
    divergence = process_replay.first_divergence(result, instance.state, last)
    return InstanceReplay(instance, result.steps, divergence)


async def replay_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    *,
    key: str,
    spec: dict[str, Any],
    instance_ids: list[uuid.UUID] | None,
    limit: int,
) -> ReplayReport:
    """Replay the journals of real instances of ``key`` on the candidate ``spec``."""
    await authorize(ctx, Permission.PACKAGES_TEST)
    if "@" in key:
        # The candidate replaces the current version; key@version is not a key.
        raise NotFoundError("Process definition not found", details={"process": key})
    async with transaction(session_factory) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        current = (await resolve_process_definition(db, ctx, key)).row
        try:
            body = normalized_spec(spec)
        except SpecError as exc:
            report = ReplayReport(key, definition_hash(spec))
            report.problems.append(Problem("invalid_document", "error", exc.path, exc.message))
            return report
        report = ReplayReport(key, definition_hash(body))
        previous = await previous_version(db, ctx.tenant_id, key, body.get("version"))
        catalog = await load_catalog(db, ctx.tenant_id, key, body, previous)
        checked = check_process(key, body, catalog)
        report.problems.extend(checked.problems)
        if checked.errors:
            return report
        definition = engine.Definition.build(key, body, catalog)
        revisions: dict[uuid.UUID, int] = {}
        for instance in await chosen_instances(db, ctx, key, current.version, instance_ids, limit):
            report.instances.append(await replay_one(db, definition, instance, revisions))
    return report
