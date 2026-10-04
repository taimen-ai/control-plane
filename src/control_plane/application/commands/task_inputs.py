"""Inputs of a task: artifacts its type declares it takes in (CP-ADR-0072 §7, §8).

An input of a task type names an artifact type and a relation (``depends_on``,
``spawned_by``, ``parent``) of the receiving task to its source — the ``to``
end of a relation going out of the receiving task, direct only. It resolves
to the head revisions (ADR-0020: no artifact supersedes them) of artifacts of
that type on each source task, whatever the source's status. Stored content
is not needed for an input to be present: a purged one is handed out marked
as such.

A task missing a required input cannot be claimed (``409 input_missing``),
is not offered by ``/work/available`` and says why in its claimability. The
check runs right after readiness, like an unfinished dependency.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from control_plane.application.authorization import AuthContext
from control_plane.application.visibility import workspace_condition
from control_plane.domain.artifact_schema import ArtifactInput, ArtifactSchema, schema_of
from control_plane.domain.errors import ConflictError
from control_plane.infrastructure.db.models import Artifact, Task, TaskRelation, TaskType


@dataclass(frozen=True)
class ResolvedInput:
    declared: ArtifactInput
    artifact: Artifact
    source_id: uuid.UUID
    source_public_id: str

    def body(self) -> dict[str, Any]:
        a = self.artifact
        return {
            "key": self.declared.key,
            "type": self.declared.type,
            "artifactId": str(a.id),
            "name": a.name,
            "mediaType": a.media_type,
            "sizeBytes": a.size_bytes,
            "sha256": a.sha256,
            "contentState": a.content_state,
            "uri": a.uri,
            "sourceTask": {
                "id": str(self.source_id),
                "publicId": self.source_public_id,
                "relation": self.declared.from_,
            },
        }


async def artifact_schema_of(session: AsyncSession, task: Task) -> ArtifactSchema:
    """The artifact schema of the type version the task carries."""
    document = await session.scalar(
        select(TaskType.artifact_schema).where(TaskType.id == task.type_id)
    )
    return schema_of(document)


async def _resolve(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    declared: tuple[ArtifactInput, ...],
    *,
    artifact_id: uuid.UUID | None = None,
    visible_to: AuthContext | None = None,
) -> list[ResolvedInput]:
    """Head revisions matching ``declared``, in declaration then creation order."""
    if not declared:
        return []
    pairs = {(i.from_, i.type) for i in declared}
    newer = aliased(Artifact)
    stmt = (
        select(TaskRelation.relation_type, Task.id, Task.public_id, Artifact)
        .join(Task, Task.id == TaskRelation.to_task_id)
        .join(
            Artifact,
            (Artifact.task_id == Task.id) & (Artifact.tenant_id == tenant_id),
        )
        .where(
            TaskRelation.tenant_id == tenant_id,
            TaskRelation.from_task_id == task_id,
            TaskRelation.relation_type.in_({relation for relation, _ in pairs}),
            Artifact.type.in_({type_ for _, type_ in pairs}),
            ~exists().where(
                newer.tenant_id == tenant_id, newer.supersedes_artifact_id == Artifact.id
            ),
        )
        .order_by(Artifact.created_at, Artifact.id)
    )
    if artifact_id is not None:
        stmt = stmt.where(Artifact.id == artifact_id)
    if visible_to is not None:
        # Shown to a caller: a source of an invisible workspace is not named
        # (CP-ADR-0082 §3.7). Claimability reads every input, as before.
        stmt = stmt.where(workspace_condition(visible_to, Task.workspace_id))
    rows = (await session.execute(stmt)).all()
    resolved: list[ResolvedInput] = []
    for entry in declared:
        for relation, source_id, public_id, artifact in rows:
            if relation == entry.from_ and artifact.type == entry.type:
                resolved.append(
                    ResolvedInput(
                        declared=entry,
                        artifact=artifact,
                        source_id=source_id,
                        source_public_id=public_id,
                    )
                )
    return resolved


async def resolve_task_inputs(
    session: AsyncSession, ctx: AuthContext, task: Task
) -> list[dict[str, Any]]:
    """``inputs`` of the run context and of ``operational.focus``; ``[]`` without a schema."""
    schema = await artifact_schema_of(session, task)
    resolved = await _resolve(session, ctx.tenant_id, task.id, schema.inputs, visible_to=ctx)
    return [item.body() for item in resolved]


async def missing_required_inputs(
    session: AsyncSession, tenant_id: uuid.UUID, task: Task
) -> list[dict[str, str]]:
    """Required inputs with no artifact behind them: ``[{key, type, from}]``."""
    required = (await artifact_schema_of(session, task)).required_inputs
    present = {r.declared.key for r in await _resolve(session, tenant_id, task.id, required)}
    return [entry.ref() for entry in required if entry.key not in present]


async def check_required_inputs(session: AsyncSession, ctx: AuthContext, task: Task) -> None:
    """Raise 409 input_missing if a required input of the task's type is absent."""
    missing = await missing_required_inputs(session, ctx.tenant_id, task)
    if missing:
        raise ConflictError(
            "input_missing",
            "Task is missing required inputs declared by its type",
            details={"taskId": str(task.id), "missing": missing},
        )


async def is_input_of(
    session: AsyncSession, tenant_id: uuid.UUID, artifact_id: uuid.UUID, task: Task
) -> bool:
    """Is the artifact, right now, a resolved input of ``task``?"""
    schema = await artifact_schema_of(session, task)
    return bool(await _resolve(session, tenant_id, task.id, schema.inputs, artifact_id=artifact_id))
