"""Artifact commands: append-oriented work products.

Artifacts are immutable records (create + read only). Revisions are new
artifacts. An artifact is handed in as a reference (``uri``), as small JSON
(``content``) or as bytes in the content store (``contentRef``, CP-ADR-0072):
the database keeps references, checksums and sizes, never blobs.

Bytes arrive in two steps: ``PUT /artifact-contents`` spools the body to disk
and stores it as an upload (``artifact_contents``), then ``POST /artifacts``
references the upload. Objects are shared by content inside a tenant; every
change of who needs an object — an upload, a reference, a purge, a sweep —
happens under one advisory lock per (tenant, sha256), so an object is never
deleted while something is about to point at it.

An artifact whose ``type`` is registered in the tenant is checked against the
latest version of that type — metadata, and media type and size of stored
content — and records it (``type_version``, CP-ADR-0072 §6); any other type
is accepted unchecked, as before the registry.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    WorkspaceNotVisible,
    authorize,
)
from control_plane.application.commands._artifact_content import (
    CONTENT_NONE,
    CONTENT_PURGED,
    CONTENT_STORED,
    artifact_event_fields,
    lock_content,
)
from control_plane.application.commands._child_ceiling import enforce_run_ceiling
from control_plane.application.commands.approvals import event_comment
from control_plane.application.commands.artifact_types import definition_of, latest_artifact_type
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.task_inputs import is_input_of
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_caller
from control_plane.application.visibility import artifact_visible, task_visible
from control_plane.domain.artifact_type import check_artifact_against_type
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DependencyUnavailableError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.content_store import (
    ContentObjectMissing,
    ContentStore,
    ContentStoreUnavailable,
    ContentStream,
    SpooledFile,
    object_key,
)
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import Artifact, ArtifactContent, Run, Task

CONTENT_REF_PREFIX = "cref_"


class ContentUnavailableError(DomainError):
    """The artifact exists but carries no bytes to hand out (404)."""

    http_status = 404


class ContentPurgedError(DomainError):
    """The bytes were there and an administrator removed them (410)."""

    http_status = 410


def content_store_unavailable() -> DependencyUnavailableError:
    return DependencyUnavailableError(
        "Artifact content store is not configured or not reachable",
        code="content_store_unavailable",
    )


def require_store(store: ContentStore | None) -> ContentStore:
    if store is None:
        raise content_store_unavailable()
    return store


def content_ref(upload_id: uuid.UUID) -> str:
    return f"{CONTENT_REF_PREFIX}{upload_id}"


def _parse_content_ref(ref: str) -> uuid.UUID | None:
    if not ref.startswith(CONTENT_REF_PREFIX):
        return None
    try:
        return uuid.UUID(ref[len(CONTENT_REF_PREFIX) :])
    except ValueError:
        return None


def artifact_resource(
    task_id: uuid.UUID | None, workspace_id: uuid.UUID | None
) -> ResourceRef | None:
    """What ``artifacts.*`` is decided on (CP-ADR-0072 §5): the artifact's
    task, else its workspace, else the tenant (``None``)."""
    if task_id is not None:
        return ResourceRef("task", str(task_id))
    if workspace_id is not None:
        return ResourceRef("workspace", str(workspace_id))
    return None


async def _object_needed(
    session: AsyncSession, tenant_id: uuid.UUID, sha256: str, now: datetime
) -> bool:
    """Is the object still referenced by a stored artifact or a live upload?"""
    stored = exists().where(
        Artifact.tenant_id == tenant_id,
        Artifact.sha256 == sha256,
        Artifact.content_state == CONTENT_STORED,
    )
    live_upload = exists().where(
        ArtifactContent.tenant_id == tenant_id,
        ArtifactContent.sha256 == sha256,
        ArtifactContent.expires_at > now,
    )
    return bool(await session.scalar(select(or_(stored, live_upload))))


# --- create ------------------------------------------------------------------


async def _take_upload(session: AsyncSession, ctx: AuthContext, ref: str) -> ArtifactContent:
    """The caller's own live upload behind ``ref``, locked for referencing.

    Unknown, foreign and expired references fail alike: knowing a checksum or
    somebody else's reference must not let the caller point at their bytes.
    """
    not_found = ValidationError(
        "content_ref_not_found",
        "contentRef does not name a live upload of this principal",
        details={"contentRef": ref},
    )
    upload_id = _parse_content_ref(ref)
    if upload_id is None:
        raise not_found
    mine = (
        ArtifactContent.id == upload_id,
        ArtifactContent.tenant_id == ctx.tenant_id,
        ArtifactContent.uploaded_by_principal_id == ctx.principal_id,
    )
    sha256 = await session.scalar(select(ArtifactContent.sha256).where(*mine))
    if sha256 is None:
        raise not_found
    await lock_content(session, ctx.tenant_id, sha256)
    upload = await session.scalar(
        select(ArtifactContent)
        .where(*mine, ArtifactContent.expires_at > utcnow())
        .with_for_update()
    )
    if upload is None:
        raise not_found
    return upload


async def create_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_: str,
    name: str,
    task_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    uri: str | None = None,
    content: dict[str, Any] | None = None,
    content_ref_value: str | None = None,
    metadata: dict[str, Any] | None = None,
    supersedes_artifact_id: uuid.UUID | None = None,
) -> Artifact:
    if not type_.strip():
        raise ValidationError("invalid_type", "type must not be empty")
    if not name.strip():
        raise ValidationError("invalid_name", "name must not be empty")
    if content_ref_value is not None and (content is not None or uri is not None):
        raise ValidationError(
            "invalid_artifact_content",
            "contentRef excludes content and uri",
            details={"fields": ["contentRef", "content" if content is not None else "uri"]},
        )

    if supersedes_artifact_id is not None:
        superseded = await session.scalar(
            select(Artifact).where(
                Artifact.id == supersedes_artifact_id, Artifact.tenant_id == ctx.tenant_id
            )
        )
        if superseded is None or not await artifact_visible(session, ctx, superseded):
            raise NotFoundError(
                "Superseded artifact not found",
                details={"supersedesArtifactId": str(supersedes_artifact_id)},
            )

    task_id: uuid.UUID | None = None
    if task_ref is not None:
        task_id = (await resolve_task(session, ctx, task_ref)).id

    run: Run | None = None
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        if run is None or not await task_visible(session, ctx, run.task_id):
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        if task_id is None:
            task_id = run.task_id
        elif task_id != run.task_id:
            raise ValidationError(
                "artifact_mismatch",
                "run does not belong to the given task",
                details={"runId": str(run_id), "taskId": str(task_id)},
            )

    if workspace_id is not None:
        from control_plane.application.commands.workspaces import get_tenant_workspace

        await get_tenant_workspace(session, ctx, workspace_id)

    # Decided on the artifact's task (CP-ADR-0072 §5), before any state changes.
    await authorize(
        ctx, Permission.ARTIFACTS_WRITE, resource=artifact_resource(task_id, workspace_id)
    )
    if run is not None:
        await enforce_run_ceiling(session, ctx, run=run, permission=Permission.ARTIFACTS_WRITE)

    now = utcnow()
    upload: ArtifactContent | None = None
    if content_ref_value is not None:
        upload = await _take_upload(session, ctx, content_ref_value)

    type_key = type_.strip()
    type_version: int | None = None
    registered = await latest_artifact_type(session, ctx.tenant_id, type_key)
    if registered is not None:
        # Media type and size belong to stored content; a reference or small
        # JSON artifact has neither and is checked on its metadata alone.
        check_artifact_against_type(
            definition_of(registered),
            key=type_key,
            version=registered.version,
            metadata=metadata or {},
            media_type=upload.media_type if upload else None,
            size_bytes=upload.size_bytes if upload else None,
        )
        type_version = registered.version

    if upload is not None and upload.referenced_at is None:
        upload.referenced_at = now

    artifact = Artifact(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        created_by_principal_id=ctx.principal_id,
        type=type_key,
        name=name.strip(),
        uri=uri,
        content=content,
        supersedes_artifact_id=supersedes_artifact_id,
        metadata_json=metadata or {},
        content_state=CONTENT_STORED if upload else CONTENT_NONE,
        size_bytes=upload.size_bytes if upload else None,
        media_type=upload.media_type if upload else None,
        sha256=upload.sha256 if upload else None,
        type_version=type_version,
        created_at=now,
    )
    session.add(artifact)
    await session.flush()

    # The event carries references only — artifact content stays out of the
    # journal (it may be large; it may not belong in the audit stream).
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
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
            "taskId": str(task_id) if task_id else None,
            "runId": str(run_id) if run_id else None,
            "uri": uri,
            "supersedesArtifactId": (
                str(supersedes_artifact_id) if supersedes_artifact_id else None
            ),
            **artifact_event_fields(artifact),
        },
    )
    return artifact


# --- upload ------------------------------------------------------------------


async def authorize_upload(ctx: AuthContext) -> None:
    """Uploading needs ``artifacts.write``; the task is checked when an
    artifact references the upload (CP-ADR-0072 §2)."""
    await authorize(ctx, Permission.ARTIFACTS_WRITE)


async def record_upload(
    session: AsyncSession,
    ctx: AuthContext,
    store: ContentStore,
    *,
    spooled: SpooledFile,
    media_type: str,
    ttl_seconds: int,
) -> ArtifactContent:
    """Store the spooled file (once per content in the tenant) and record the upload."""
    key = object_key(ctx.tenant_id, spooled.sha256)
    # The upload references the caller: its principal before the content lock
    # (rule 1 of ``application/locking.py``, CP-ADR-0077 §3) — this transaction
    # does not go through the write flow, and an artifact created meanwhile may
    # hold that content lock after a task row.
    await lock_caller(session, ctx)
    await lock_content(session, ctx.tenant_id, spooled.sha256)
    try:
        if not await store.exists(key):
            await store.put(key, spooled.path, spooled.size)
    except ContentStoreUnavailable as exc:
        raise content_store_unavailable() from exc
    now = utcnow()
    upload = ArtifactContent(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        uploaded_by_principal_id=ctx.principal_id,
        sha256=spooled.sha256,
        size_bytes=spooled.size,
        media_type=media_type,
        storage_key=key,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        referenced_at=None,
    )
    session.add(upload)
    await session.flush()
    return upload


# --- read --------------------------------------------------------------------


async def _read_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    artifact_id: uuid.UUID,
    for_task_ref: str | None,
) -> tuple[Artifact, Task | None]:
    """The artifact and the receiving task it was granted for, if any.

    ``for_task_ref`` (CP-ADR-0072 §5): the artifact is read as an input of
    that task — allowed with ``tasks.read`` on the receiving task while the
    artifact is one of its resolved inputs. Otherwise — and without it — the
    read is decided on the artifact's task. 404 across tenants.
    """
    artifact = await session.scalar(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
    )
    # An artifact of an invisible workspace or work answers as a missing one,
    # whichever way it is asked for — as an input of visible work too
    # (CP-ADR-0082 §3.7): ``authorize`` on its task does not see workspaces.
    if artifact is None or not await artifact_visible(session, ctx, artifact):
        raise NotFoundError("Artifact not found", details={"artifactId": str(artifact_id)})
    if for_task_ref is not None:
        recipient = await resolve_task(session, ctx, for_task_ref)
        try:
            await authorize(
                ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(recipient.id))
            )
        except AuthorizationError:
            pass
        else:
            if await is_input_of(session, ctx.tenant_id, artifact.id, recipient):
                return artifact, recipient
    try:
        await authorize(
            ctx,
            Permission.ARTIFACTS_READ,
            resource=artifact_resource(artifact.task_id, artifact.workspace_id),
        )
    except WorkspaceNotVisible:
        # The artifact's own 404, not the workspace's (CP-ADR-0082 §3.7).
        raise NotFoundError(
            "Artifact not found", details={"artifactId": str(artifact_id)}
        ) from None
    except AuthorizationError:
        if not await _skill_executor_reads(session, ctx, artifact):
            raise
    return artifact, None


async def _skill_executor_reads(
    session: AsyncSession, ctx: AuthContext, artifact: Artifact
) -> bool:
    """A skill executor reads a task's artifact on the task's workspace.

    CP-ADR-0072, amendment 2026-09-28 (company-knowledge): the executor holds
    ``skills.execute`` but not ``tasks.read``; ``artifacts.read`` granted on
    the workspace of the artifact's task lets its skills read the attached
    file. Nobody without ``skills.execute`` gains a path here.
    """
    if artifact.task_id is None:
        # Decided on its own workspace (or the tenant) already.
        return False
    workspace_id = await session.scalar(
        select(Task.workspace_id).where(
            Task.id == artifact.task_id, Task.tenant_id == ctx.tenant_id
        )
    )
    if workspace_id is None:
        return False
    try:
        await authorize(ctx, Permission.SKILLS_EXECUTE)
        await authorize(
            ctx, Permission.ARTIFACTS_READ, resource=ResourceRef("workspace", str(workspace_id))
        )
    except (AuthorizationError, WorkspaceNotVisible):
        # An invisible workspace is "not allowed" too, not its 404 (CP-ADR-0082 B6).
        return False
    return True


async def get_readable_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    artifact_id: uuid.UUID,
    *,
    for_task_ref: str | None = None,
) -> Artifact:
    """The artifact if the caller may read it (on its task, or as an input)."""
    artifact, _ = await _read_artifact(session, ctx, artifact_id, for_task_ref)
    return artifact


@dataclass
class OpenedContent:
    artifact: Artifact
    stream: ContentStream


async def open_content(
    session: AsyncSession,
    ctx: AuthContext,
    store: ContentStore | None,
    artifact_id: uuid.UUID,
    *,
    for_task_ref: str | None = None,
) -> OpenedContent:
    """Open the bytes of an artifact for the caller and journal the read.

    The object is opened before the event is written: a read the store could
    not serve leaves no ``artifact.content_read``. The caller streams the
    result after the transaction commits.
    """
    artifact, recipient = await _read_artifact(session, ctx, artifact_id, for_task_ref)
    if artifact.content_state == CONTENT_NONE:
        raise ContentUnavailableError(
            "content_not_found",
            "Artifact has no stored content",
            details={"artifactId": str(artifact_id)},
        )
    if artifact.content_state == CONTENT_PURGED:
        raise ContentPurgedError(
            "content_purged",
            "Artifact content was purged",
            details={"artifactId": str(artifact_id)},
        )
    assert artifact.sha256 is not None
    try:
        stream = await require_store(store).open(object_key(ctx.tenant_id, artifact.sha256))
    except (ContentStoreUnavailable, ContentObjectMissing) as exc:
        # A missing object under a stored record is a broken store, not a
        # missing artifact: the caller may retry once it is repaired.
        raise content_store_unavailable() from exc

    try:
        run_id = None
        # Read as an input, the reader works on the receiving task.
        run_task_id = recipient.id if recipient is not None else artifact.task_id
        if run_task_id is not None:
            run_id = await session.scalar(
                select(Run.id)
                .where(
                    Run.tenant_id == ctx.tenant_id,
                    Run.task_id == run_task_id,
                    Run.principal_id == ctx.principal_id,
                    Run.status == "running",
                )
                .order_by(Run.attempt.desc())
                .limit(1)
            )
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="artifact.content_read",
            entity_type="artifact",
            entity_id=artifact.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "artifactId": str(artifact.id),
                "taskId": str(artifact.task_id) if artifact.task_id else None,
                "forTaskId": str(recipient.id) if recipient is not None else None,
                "runId": str(run_id) if run_id else None,
                "sha256": artifact.sha256,
                "sizeBytes": artifact.size_bytes,
            },
        )
    except BaseException:
        await stream.aclose()
        raise
    return OpenedContent(artifact=artifact, stream=stream)


# --- purge -------------------------------------------------------------------


async def purge_content(
    session: AsyncSession,
    ctx: AuthContext,
    store: ContentStore | None,
    artifact_id: uuid.UUID,
    *,
    reason: str,
) -> Artifact:
    """Remove the bytes of an artifact; the record stays (CP-ADR-0072 §10)."""
    await authorize(ctx, Permission.ADMIN)
    artifact = await session.scalar(
        select(Artifact)
        .where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if artifact is None or not await artifact_visible(session, ctx, artifact):
        raise NotFoundError("Artifact not found", details={"artifactId": str(artifact_id)})
    if artifact.content_state == CONTENT_PURGED:
        return artifact
    if artifact.content_state != CONTENT_STORED:
        raise ConflictError(
            "content_not_stored",
            "Artifact has no stored content to purge",
            details={"artifactId": str(artifact_id), "contentState": artifact.content_state},
        )
    assert artifact.sha256 is not None and artifact.size_bytes is not None
    active_store = require_store(store)

    await lock_content(session, ctx.tenant_id, artifact.sha256)
    artifact.content_state = CONTENT_PURGED
    await session.flush()
    object_deleted = not await _object_needed(session, ctx.tenant_id, artifact.sha256, utcnow())
    if object_deleted:
        # Deleted before commit: a store that refuses leaves the record stored.
        try:
            await active_store.delete(object_key(ctx.tenant_id, artifact.sha256))
        except ContentStoreUnavailable as exc:
            raise content_store_unavailable() from exc

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="artifact.content_purged",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "artifactId": str(artifact.id),
            "taskId": str(artifact.task_id) if artifact.task_id else None,
            "sha256": artifact.sha256,
            "sizeBytes": artifact.size_bytes,
            "reason": event_comment(reason),
            "objectDeleted": object_deleted,
        },
    )
    return artifact


# --- sweep (worker) ----------------------------------------------------------


async def sweep_expired_uploads(
    session_factory: async_sessionmaker[AsyncSession],
    store: ContentStore,
    *,
    batch: int = 100,
) -> int:
    """Drop uploads no artifact referenced before they expired, and the
    objects nothing needs any more (CP-ADR-0072 §10). Returns uploads removed.

    Each (tenant, sha256) is settled in its own transaction under the content
    lock; a store that does not answer leaves the rows for the next pass.
    """
    now = utcnow()
    async with transaction(session_factory) as session:
        keys = (
            await session.execute(
                select(ArtifactContent.tenant_id, ArtifactContent.sha256)
                .where(ArtifactContent.expires_at <= now, ArtifactContent.referenced_at.is_(None))
                .group_by(ArtifactContent.tenant_id, ArtifactContent.sha256)
                .limit(batch)
            )
        ).all()

    removed = 0
    for tenant_id, sha256 in keys:
        try:
            async with transaction(session_factory) as session:
                await lock_content(session, tenant_id, sha256)
                now = utcnow()
                result = await session.execute(
                    delete(ArtifactContent)
                    .where(
                        and_(
                            ArtifactContent.tenant_id == tenant_id,
                            ArtifactContent.sha256 == sha256,
                            ArtifactContent.expires_at <= now,
                            ArtifactContent.referenced_at.is_(None),
                        )
                    )
                    .returning(ArtifactContent.id)
                )
                count = len(result.all())
                if not await _object_needed(session, tenant_id, sha256, now):
                    await store.delete(object_key(tenant_id, sha256))
                removed += count
        except ContentStoreUnavailable:
            continue
    return removed
