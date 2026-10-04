"""Work item comment commands: say something, and correct what you said.

A comment is coordination (ADR-0015, ADR-0050): it never carries authority, it
never replaces an Artifact, and it never becomes a place to park transcripts or
raw prompts. Two rules make the thread trustworthy rather than merely
convenient:

* the author is taken from the authenticated context, never from the body, so
  a human and an agent are told apart by construction;
* an edit writes the superseded text to ``task_comment_revisions`` BEFORE the
  new text lands, so what was actually said at the time survives the edit.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._child_ceiling import enforce_run_ceiling
from control_plane.application.commands.relations import resolve_task
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.visibility import artifact_visible, task_visible
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.work_item import validate_comment_body
from control_plane.infrastructure.db.models import (
    Artifact,
    Run,
    Task,
    TaskComment,
    TaskCommentRevision,
)


async def add_comment(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    body: str,
    run_id: uuid.UUID | None = None,
    artifact_id: uuid.UUID | None = None,
) -> TaskComment:
    """Append one reply to a work item's thread.

    A terminal task still accepts comments on purpose: a retro note, a reason
    for cancelling or a pointer to the follow-up all arrive after the work is
    closed, and refusing them would push that record into the description.
    """
    await authorize(ctx, Permission.TASKS_WRITE)
    task = await resolve_task(session, ctx, task_ref)
    text = validate_comment_body(body)
    await _check_provenance(session, ctx, task=task, run_id=run_id, artifact_id=artifact_id)

    now = utcnow()
    comment = TaskComment(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        task_id=task.id,
        author_principal_id=ctx.principal_id,
        body=text,
        run_id=run_id,
        artifact_id=artifact_id,
        version=1,
        created_at=now,
        updated_at=now,
        edited_at=None,
    )
    session.add(comment)
    await session.flush()

    await _record(session, ctx, comment=comment, action="added")
    return comment


async def edit_comment(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    comment_id: uuid.UUID,
    body: str,
    expected_version: int,
) -> TaskComment:
    """Replace the text of one's own comment, keeping the previous version.

    Only the author may edit. An administrator is not an exception: rewriting
    someone else's words under their name is impersonation, and no permission
    should be able to buy it. Removing a comment outright is not offered at all
    (ADR-0050).
    """
    await authorize(ctx, Permission.TASKS_WRITE)
    task = await resolve_task(session, ctx, task_ref)
    comment = await _locked_comment(session, ctx, task_id=task.id, comment_id=comment_id)

    if comment.author_principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Only the author of a comment may edit it",
            code="not_comment_author",
            details={"commentId": str(comment_id)},
        )
    if comment.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "The comment has changed since it was read",
            details={"expectedVersion": expected_version, "actualVersion": comment.version},
        )

    text = validate_comment_body(body)
    if text == comment.body:
        # Nothing was said differently: no revision, no version bump, no event.
        # An idempotent retry must not manufacture edit history.
        return comment

    now = utcnow()
    session.add(
        TaskCommentRevision(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            comment_id=comment.id,
            task_id=comment.task_id,
            version=comment.version,
            body=comment.body,
            author_principal_id=comment.author_principal_id,
            created_at=comment.edited_at or comment.created_at,
            superseded_at=now,
            superseded_by=ctx.principal_id,
        )
    )
    # Flush the revision first: if the append-only history cannot be written,
    # the edit must not happen either.
    await session.flush()

    comment.body = text
    comment.version += 1
    comment.updated_at = now
    comment.edited_at = now
    await session.flush()

    await _record(session, ctx, comment=comment, action="edited")
    return comment


async def _locked_comment(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_id: uuid.UUID,
    comment_id: uuid.UUID,
) -> TaskComment:
    """The comment, locked for update, scoped to tenant AND to its task.

    Addressing a comment through a task it does not belong to is a 404, not a
    silent success on the right row: the path is part of the identity.
    """
    comment = await session.scalar(
        select(TaskComment)
        .where(
            TaskComment.id == comment_id,
            TaskComment.tenant_id == ctx.tenant_id,
            TaskComment.task_id == task_id,
        )
        .with_for_update()
    )
    if comment is None:
        raise NotFoundError("Comment not found", details={"commentId": str(comment_id)})
    return comment


async def _check_provenance(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task: Task,
    run_id: uuid.UUID | None,
    artifact_id: uuid.UUID | None,
) -> None:
    """A run or artifact named by a comment must belong to the same task.

    Otherwise the link would read as provenance while pointing at unrelated
    work — and, across a tenant boundary, would leak the existence of ids that
    are not the caller's to see.
    """
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        # Of invisible work, a run is a missing one, not a mismatch that tells
        # it exists (CP-ADR-0082 §3.7).
        if run is None or not await task_visible(session, ctx, run.task_id):
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        await enforce_run_ceiling(session, ctx, run=run, permission=Permission.TASKS_WRITE)
        if run.task_id != task.id:
            raise ValidationError(
                "comment_mismatch",
                "run does not belong to the commented task",
                details={"runId": str(run_id), "taskId": str(task.id)},
            )

    if artifact_id is not None:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
        )
        if artifact is None or not await artifact_visible(session, ctx, artifact):
            raise NotFoundError("Artifact not found", details={"artifactId": str(artifact_id)})
        if artifact.task_id != task.id:
            raise ValidationError(
                "comment_mismatch",
                "artifact does not belong to the commented task",
                details={"artifactId": str(artifact_id), "taskId": str(task.id)},
            )


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    comment: TaskComment,
    action: str,
) -> None:
    """Emit ``task.comment_<action>`` on the TASK's stream, without the body.

    Two deliberate choices. The stream is the task's, because a follower of a
    work item wants its discussion in the same place as its status changes. The
    payload carries references and a length, never the text: the journal is
    replicated further than the row it describes, and the body is exactly the
    part that should not travel (ADR-0015).
    """
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=f"task.comment_{action}",
        entity_type="task",
        entity_id=comment.task_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "commentId": str(comment.id),
            "authorPrincipalId": str(comment.author_principal_id),
            "version": comment.version,
            "bodyLength": len(comment.body),
            "runId": str(comment.run_id) if comment.run_id else None,
            "artifactId": str(comment.artifact_id) if comment.artifact_id else None,
        },
    )
