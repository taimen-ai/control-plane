"""Typed outputs of a task executed by a skill (CP-ADR-0072, amendment 2026-10-01).

A task whose type says ``execution: {skill, version}`` is done by one skill
call. When that call succeeds, every output its type declares in
``artifactSchema.outputs`` is taken from the field of the skill's ``output``
with the same name as the output's ``key``, and becomes an artifact of the
declared type on the task — stored content, ``application/json``, so a
downstream task gets it as an input and the implicit criterion of a required
output (CP-ADR-0067 amendment) passes with the default ``content: required``.

What happens to each output is reported, never raised: the call has already
succeeded and its ``skill_result`` stays whatever becomes of the outputs.

- a field that is absent or ``null`` creates nothing (``absent``; ``missing``
  when the output is required — the implicit criterion then fails the task);
- a value that fails the artifact type (its ``metadataSchema``, media types,
  size) or that cannot be stored is ``rejected`` with the reason.

Core knows keys, artifact types and JSON — never what an output means.
"""

import json
import uuid
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from control_plane.application.authorization import AuthContext
from control_plane.application.commands._artifact_content import (
    CONTENT_STORED,
    artifact_event_fields,
    store_core_content,
)
from control_plane.application.commands.artifact_types import definition_of, latest_artifact_type
from control_plane.application.commands.task_types import task_type_of
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.artifact_schema import ArtifactOutput, schema_of
from control_plane.domain.artifact_type import check_output_value
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.content_store import ContentStore, ContentStoreUnavailable
from control_plane.infrastructure.db.models import Artifact, Skill, SkillInvocation, Task

OUTPUT_MEDIA_TYPE = "application/json"

CREATED = "created"
ABSENT = "absent"
MISSING = "missing"
REJECTED = "rejected"


def is_execution_call(invocation: SkillInvocation) -> bool:
    """The one call a run makes to execute its task (ADR-0056 §3)."""
    basis = invocation.authorization_basis or {}
    return basis.get("kind") == "execution" and invocation.task_id is not None


def encode_value(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


async def _head_of_type(session: AsyncSession, task: Task, type_key: str) -> uuid.UUID | None:
    """The newest head revision of ``type_key`` on the task: a new hand-in replaces it."""
    newer = aliased(Artifact)
    head: uuid.UUID | None = await session.scalar(
        select(Artifact.id)
        .where(
            Artifact.tenant_id == task.tenant_id,
            Artifact.task_id == task.id,
            Artifact.type == type_key,
            ~exists().where(
                newer.tenant_id == task.tenant_id, newer.supersedes_artifact_id == Artifact.id
            ),
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(1)
    )
    return head


def _rejected(declared: ArtifactOutput, code: str, message: str, details: Any) -> dict[str, Any]:
    reason: dict[str, Any] = {"code": code, "message": message[:500]}
    if details:
        reason["details"] = details
    return {"key": declared.key, "type": declared.type, "status": REJECTED, "reason": reason}


async def _record_output(
    session: AsyncSession,
    ctx: AuthContext,
    store: ContentStore | None,
    invocation: SkillInvocation,
    skill: Skill,
    task: Task,
    declared: ArtifactOutput,
    value: Any,
) -> dict[str, Any]:
    data = encode_value(value)
    registered = await latest_artifact_type(session, task.tenant_id, declared.type)
    type_version: int | None = None
    if registered is not None:
        try:
            check_output_value(
                definition_of(registered),
                key=declared.type,
                version=registered.version,
                value=value,
                media_type=OUTPUT_MEDIA_TYPE,
                size_bytes=len(data),
                narrowed_to=declared.media_types,
            )
        except ValidationError as exc:
            return _rejected(declared, exc.code, exc.message, exc.details)
        type_version = registered.version
    if store is None:
        return _rejected(
            declared,
            "content_store_unavailable",
            "Artifact content store is not configured",
            None,
        )
    try:
        stored = await store_core_content(session, store, task.tenant_id, data)
    except ContentStoreUnavailable:
        return _rejected(
            declared,
            "content_store_unavailable",
            "Artifact content store is not reachable",
            None,
        )

    supersedes = await _head_of_type(session, task, declared.type)
    artifact = Artifact(
        id=new_uuid(),
        tenant_id=task.tenant_id,
        workspace_id=None,
        task_id=task.id,
        run_id=invocation.run_id,
        # Handed in by whoever executed the skill, as an agent hands in its own.
        created_by_principal_id=ctx.principal_id,
        type=declared.type,
        name=f"{declared.key}.json",
        uri=None,
        content=None,
        supersedes_artifact_id=supersedes,
        metadata_json={
            "output": declared.key,
            "skill": skill.name,
            "version": skill.version,
            "invocationId": str(invocation.id),
            "authorityPrincipalId": str(invocation.authority_principal_id),
        },
        content_state=CONTENT_STORED,
        size_bytes=stored.size,
        media_type=OUTPUT_MEDIA_TYPE,
        sha256=stored.sha256,
        type_version=type_version,
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()
    await record_event(
        session,
        tenant_id=task.tenant_id,
        event_type="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "type": artifact.type,
            "name": artifact.name,
            "taskId": str(task.id),
            "runId": str(invocation.run_id) if invocation.run_id else None,
            "uri": None,
            "supersedesArtifactId": str(supersedes) if supersedes else None,
            "skillInvocationId": str(invocation.id),
            **artifact_event_fields(artifact),
        },
    )
    return {
        "key": declared.key,
        "type": declared.type,
        "status": CREATED,
        "artifactId": str(artifact.id),
    }


async def record_task_outputs(
    session: AsyncSession,
    ctx: AuthContext,
    store: ContentStore | None,
    invocation: SkillInvocation,
    skill: Skill,
    *,
    output: dict[str, Any],
) -> list[dict[str, Any]]:
    """One entry per declared output, in declaration order; ``[]`` for any
    call that is not its task's execution call or whose type declares none."""
    if not is_execution_call(invocation):
        return []
    task = await session.get(Task, invocation.task_id)
    assert task is not None
    schema = schema_of((await task_type_of(session, task)).artifact_schema)
    results: list[dict[str, Any]] = []
    for declared in schema.outputs:
        value = output.get(declared.key)
        if value is None:
            status = MISSING if declared.required else ABSENT
            results.append({"key": declared.key, "type": declared.type, "status": status})
            continue
        results.append(
            await _record_output(session, ctx, store, invocation, skill, task, declared, value)
        )
    return results
