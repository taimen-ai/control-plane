"""Read-side queries: relations, requirements, runs, artifacts, approvals,
checkpoints, run actions and the run execution context (v0.3)."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, WorkspaceNotVisible, authorize
from control_plane.application.commands.artifacts import (
    artifact_resource,
    get_readable_artifact,
)
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.task_inputs import resolve_task_inputs
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries.instructions import instructions_for_task
from control_plane.application.queries.lists import Page, _paginate, clamp_limit
from control_plane.application.visibility import (
    approval_condition,
    approval_visible,
    artifact_condition,
    task_condition,
    task_visible,
)
from control_plane.domain.enums import ApprovalStatus, Permission, RunStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import (
    Agent,
    Approval,
    Artifact,
    Capability,
    Role,
    Run,
    RunAction,
    RunCheckpoint,
    RunControlMessage,
    Session,
    Skill,
    Task,
    TaskClaim,
    TaskRelation,
    TaskRequirement,
    Workspace,
)


@dataclass(frozen=True)
class RunControlPage:
    items: list[RunControlMessage]
    next_cursor: str
    has_more: bool


def _control_cursor(run_id: uuid.UUID, seq: int) -> str:
    return "rc1_" + encode_cursor({"r": str(run_id), "s": seq})


def _parse_control_cursor(cursor: str, run_id: uuid.UUID) -> int:
    if not cursor.startswith("rc1_"):
        raise ValidationError("invalid_cursor", "Malformed Run control cursor")
    data = decode_cursor(cursor[4:])
    try:
        cursor_run_id = uuid.UUID(data["r"])
        seq = int(data["s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed Run control cursor") from exc
    if cursor_run_id != run_id or seq < 0:
        raise ValidationError("invalid_cursor", "Run control cursor belongs to another Run")
    return seq


async def list_run_control_messages(
    session: AsyncSession,
    ctx: AuthContext,
    run_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> RunControlPage:
    await authorize(ctx, Permission.TASKS_READ)
    await get_run(session, ctx, run_id)
    bounded_limit = clamp_limit(limit)
    after_seq = _parse_control_cursor(cursor, run_id) if cursor else 0
    rows = list(
        (
            await session.scalars(
                select(RunControlMessage)
                .where(
                    RunControlMessage.run_id == run_id,
                    RunControlMessage.tenant_id == ctx.tenant_id,
                    RunControlMessage.seq > after_seq,
                )
                .order_by(RunControlMessage.seq)
                .limit(bounded_limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > bounded_limit
    items = rows[:bounded_limit]
    next_seq = items[-1].seq if items else after_seq
    return RunControlPage(
        items=items,
        next_cursor=_control_cursor(run_id, next_seq),
        has_more=has_more,
    )


async def list_task_relations(
    session: AsyncSession, ctx: AuthContext, task_ref: str
) -> list[TaskRelation]:
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    rows = await session.scalars(
        select(TaskRelation)
        .where(
            TaskRelation.tenant_id == ctx.tenant_id,
            (TaskRelation.from_task_id == task.id) | (TaskRelation.to_task_id == task.id),
            # A relation to invisible work is not shown: it would name that
            # work (CP-ADR-0082 §3.7).
            task_condition(ctx, TaskRelation.from_task_id),
            task_condition(ctx, TaskRelation.to_task_id),
        )
        .order_by(TaskRelation.created_at, TaskRelation.id)
    )
    return list(rows.all())


async def get_task_requirements(
    session: AsyncSession, ctx: AuthContext, task_ref: str
) -> dict[str, list[dict[str, str]]]:
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    requirements = (
        await session.scalars(select(TaskRequirement).where(TaskRequirement.task_id == task.id))
    ).all()

    roles: list[dict[str, str]] = []
    capabilities: list[dict[str, str]] = []
    skills: list[dict[str, str]] = []
    for requirement in requirements:
        if requirement.role_id is not None:
            role = await session.get(Role, requirement.role_id)
            if role:
                roles.append({"id": str(role.id), "slug": role.slug})
        elif requirement.capability_id is not None:
            capability = await session.get(Capability, requirement.capability_id)
            if capability:
                capabilities.append({"id": str(capability.id), "name": capability.name})
        elif requirement.skill_id is not None:
            skill = await session.get(Skill, requirement.skill_id)
            if skill:
                skills.append({"id": str(skill.id), "name": skill.name, "version": skill.version})
    return {"roles": roles, "capabilities": capabilities, "skills": skills}


async def list_runs(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    task_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    status: str | None = None,
    principal_id: uuid.UUID | None = None,
    agent_key: str | None = None,
) -> Page[Run]:
    """Runs of the tenant, newest first by ``(started_at, id)``.

    ``agent_key`` narrows to the principal of that registry agent, retired or
    not (CP-ADR-0073, amendment of 2026-09-29): an unknown key or an agent
    without a principal yet has no runs, so the page is empty rather than a
    404 - the listing does not tell a reader of runs which agents exist.
    """
    await authorize(ctx, Permission.TASKS_READ)
    if status is not None and status not in set(RunStatus):
        raise ValidationError("invalid_status", f"Unknown run status: {status}")
    stmt = select(Run).where(Run.tenant_id == ctx.tenant_id, task_condition(ctx, Run.task_id))
    if task_id is not None:
        stmt = stmt.where(Run.task_id == task_id)
    if claim_id is not None:
        stmt = stmt.where(Run.claim_id == claim_id)
    if status is not None:
        stmt = stmt.where(Run.status == status)
    if principal_id is not None:
        stmt = stmt.where(Run.principal_id == principal_id)
    if agent_key is not None:
        agent_principal = await session.scalar(
            select(Agent.principal_id).where(
                Agent.tenant_id == ctx.tenant_id, Agent.key == agent_key
            )
        )
        if agent_principal is None:
            return Page(items=[], next_cursor=None)
        stmt = stmt.where(Run.principal_id == agent_principal)
    return await _paginate(
        session,
        stmt,
        created_col=Run.started_at,
        id_col=Run.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_run(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> Run:
    await authorize(ctx, Permission.TASKS_READ)
    run = await session.scalar(select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id))
    # A run of invisible work answers as a missing run (CP-ADR-0082 §3.7).
    if run is None or not await task_visible(session, ctx, run.task_id):
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    return run


async def list_artifacts(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    type_: str | None = None,
) -> Page[Artifact]:
    # A listing narrowed to one task (or workspace) is decided there, like a
    # single artifact (CP-ADR-0072 §5); an unfiltered one at tenant level.
    try:
        await authorize(
            ctx, Permission.ARTIFACTS_READ, resource=artifact_resource(task_id, workspace_id)
        )
    except WorkspaceNotVisible:
        # The empty page of a workspace that does not exist (CP-ADR-0082 §3.7).
        return Page(items=[], next_cursor=None)
    stmt = select(Artifact).where(Artifact.tenant_id == ctx.tenant_id, artifact_condition(ctx))
    if task_id is not None:
        stmt = stmt.where(Artifact.task_id == task_id)
    if run_id is not None:
        stmt = stmt.where(Artifact.run_id == run_id)
    if workspace_id is not None:
        stmt = stmt.where(Artifact.workspace_id == workspace_id)
    if type_ is not None:
        stmt = stmt.where(Artifact.type == type_)
    return await _paginate(
        session,
        stmt,
        created_col=Artifact.created_at,
        id_col=Artifact.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    artifact_id: uuid.UUID,
    *,
    for_task_ref: str | None = None,
) -> Artifact:
    return await get_readable_artifact(session, ctx, artifact_id, for_task_ref=for_task_ref)


def _seq_cursor(run_id: uuid.UUID, seq: int) -> str:
    return encode_cursor({"r": str(run_id), "s": seq})


def _parse_seq_cursor(cursor: str, run_id: uuid.UUID) -> int:
    data = decode_cursor(cursor)
    try:
        cursor_run_id = uuid.UUID(data["r"])
        seq = int(data["s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    if cursor_run_id != run_id or seq < 0:
        raise ValidationError("invalid_cursor", "Pagination cursor belongs to another Run")
    return seq


async def _paginate_by_seq[T: (RunCheckpoint, RunAction)](
    session: AsyncSession,
    model: type[T],
    run_id: uuid.UUID,
    *,
    limit: int | None,
    cursor: str | None,
) -> Page[T]:
    """Page a run-local, append-only sequence in ``seq`` order.

    Without ``limit`` and ``cursor`` the whole sequence is returned in one
    response, as before these routes accepted paging: the console and the
    harness adapters read it without a cursor and look for the latest entry.
    Paging starts only when the caller asks for it explicitly.

    The cursor is the last returned ``seq``: rows appended after the page was
    read land behind it, so a poller that follows ``nextCursor`` sees each row
    exactly once.
    """
    stmt = select(model).where(model.run_id == run_id).order_by(model.seq.asc())
    if limit is None and not cursor:
        return Page(items=list((await session.scalars(stmt)).all()), next_cursor=None)
    bounded_limit = clamp_limit(limit)
    after_seq = _parse_seq_cursor(cursor, run_id) if cursor else 0
    rows = list(
        (await session.scalars(stmt.where(model.seq > after_seq).limit(bounded_limit + 1))).all()
    )
    items = rows[:bounded_limit]
    next_cursor = _seq_cursor(run_id, items[-1].seq) if len(rows) > bounded_limit else None
    return Page(items=items, next_cursor=next_cursor)


async def list_run_checkpoints(
    session: AsyncSession,
    ctx: AuthContext,
    run_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[RunCheckpoint]:
    await authorize(ctx, Permission.TASKS_READ)
    await get_run(session, ctx, run_id)  # tenant scoping + 404
    return await _paginate_by_seq(session, RunCheckpoint, run_id, limit=limit, cursor=cursor)


async def list_run_actions(
    session: AsyncSession,
    ctx: AuthContext,
    run_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[RunAction]:
    await authorize(ctx, Permission.TASKS_READ)
    await get_run(session, ctx, run_id)
    return await _paginate_by_seq(session, RunAction, run_id, limit=limit, cursor=cursor)


_CONTEXT_ARTIFACTS_LIMIT = 50
_CONTEXT_CHECKPOINTS_LIMIT = 50


async def get_run_context(
    session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID
) -> dict[str, Any]:
    """Operational execution context for a run (v0.3).

    A snapshot of what a harness needs to (re)build its working state: the
    task, workspace, claim, requirements, relations, recent artifacts,
    pending approvals, checkpoints of ALL of the task's runs (continuity
    across suspensions), executable skills and the current event cursor.
    Operational context only — not memory, not chat history.
    """
    from control_plane.application.queries.harness import (
        current_event_cursor,
        resolve_executable_skills,
    )

    await authorize(ctx, Permission.TASKS_READ)
    # Cursor FIRST (before any state read): under READ COMMITTED a transition
    # committed mid-build must land after the returned cursor, not vanish
    # between stale state and a too-new cursor (see get_harness_context).
    event_cursor = await current_event_cursor(session, ctx.tenant_id)
    run = await get_run(session, ctx, run_id)
    task = await session.get(Task, run.task_id)
    if task is None:  # pragma: no cover - FK guarantees existence
        raise NotFoundError("Task not found", details={"taskId": str(run.task_id)})
    workspace = await session.get(Workspace, task.workspace_id) if task.workspace_id else None
    claim = await session.get(TaskClaim, run.claim_id)

    requirements = await get_task_requirements(session, ctx, str(task.id))
    relations = await list_task_relations(session, ctx, str(task.id))

    artifacts = (
        await session.scalars(
            select(Artifact)
            .where(Artifact.task_id == task.id)
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(_CONTEXT_ARTIFACTS_LIMIT)
        )
    ).all()
    pending_approvals = (
        await session.scalars(
            select(Approval)
            .where(
                Approval.task_id == task.id,
                Approval.status == ApprovalStatus.PENDING,
                approval_condition(ctx),
            )
            .order_by(Approval.created_at.desc())
        )
    ).all()
    # Checkpoints of ALL the task's runs, newest first: a fresh run resumes
    # from where any previous (suspended/failed) attempt left off.
    checkpoints = (
        await session.execute(
            select(RunCheckpoint, Run.attempt)
            .join(Run, Run.id == RunCheckpoint.run_id)
            .where(RunCheckpoint.task_id == task.id)
            .order_by(RunCheckpoint.created_at.desc(), RunCheckpoint.id.desc())
            .limit(_CONTEXT_CHECKPOINTS_LIMIT)
        )
    ).all()
    pending_control_messages = (
        await session.scalars(
            select(RunControlMessage)
            .where(
                RunControlMessage.run_id == run.id,
                RunControlMessage.status == "accepted",
            )
            .order_by(RunControlMessage.seq)
            .limit(200)
        )
    ).all()

    # Child handles launched by this run: the reconnect path after a restart
    # (HRS-7). Bounded — an orchestrator with more children than this pages
    # through /runs/{id}/child-handles rather than getting an unbounded body.
    from control_plane.application.queries.child_runs import (
        RUN_CONTEXT_CHILD_LIMIT,
        child_handle_body,
        list_child_handles,
    )

    child_page = await list_child_handles(session, ctx, run.id, limit=RUN_CONTEXT_CHILD_LIMIT)

    instructions = await instructions_for_task(session, ctx.tenant_id, task)
    # CP-ADR-0072 §8: what the task's type declares it takes in, resolved now.
    inputs = await resolve_task_inputs(session, ctx, task)

    run_session = await session.get(Session, run.session_id)
    skills = await resolve_executable_skills(
        session,
        ctx,
        harness_capabilities=run_session.harness_capabilities if run_session else None,
    )

    return {
        "run": {
            "id": str(run.id),
            "taskId": str(run.task_id),
            "claimId": str(run.claim_id),
            "status": run.status,
            "attempt": run.attempt,
            "fencingToken": run.fencing_token,
            "input": run.input,
            "maxDurationSeconds": run.max_duration_seconds,
            "maxActions": run.max_actions,
            "cancelRequestedAt": (
                run.cancel_requested_at.isoformat() if run.cancel_requested_at else None
            ),
            "startedAt": run.started_at.isoformat(),
            "instructionsHash": run.instructions_hash,
            "instructionsRefs": run.instructions_refs,
        },
        # CP-ADR-0066: layers 1-3 as they stand now; the run's own record above
        # says what it was started under (the hashes differ if a layer moved).
        "instructions": instructions,
        "task": {
            "id": str(task.id),
            "publicId": task.public_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "priority": task.priority,
            "workspaceId": str(task.workspace_id) if task.workspace_id else None,
            "version": task.version,
            "claimEpoch": task.claim_epoch,
        },
        "workspace": (
            {"id": str(workspace.id), "slug": workspace.slug, "name": workspace.name}
            if workspace
            else None
        ),
        "claim": (
            {
                "id": str(claim.id),
                "status": claim.status,
                "fencingToken": claim.fencing_token,
                "expiresAt": claim.expires_at.isoformat(),
                "sessionId": str(claim.session_id),
            }
            if claim
            else None
        ),
        "requirements": requirements,
        "relations": [
            {
                "id": str(r.id),
                "fromTaskId": str(r.from_task_id),
                "toTaskId": str(r.to_task_id),
                "type": r.relation_type,
            }
            for r in relations
        ],
        "inputs": inputs,
        "artifacts": [
            {
                "id": str(a.id),
                "type": a.type,
                "name": a.name,
                "uri": a.uri,
                "runId": str(a.run_id) if a.run_id else None,
                "supersedesArtifactId": (
                    str(a.supersedes_artifact_id) if a.supersedes_artifact_id else None
                ),
                "createdAt": a.created_at.isoformat(),
            }
            for a in artifacts
        ],
        "approvals": [
            {
                "id": str(a.id),
                "status": a.status,
                "gate": a.gate,
                "requestedBy": str(a.requested_by_principal_id),
                "comment": a.comment,
                "createdAt": a.created_at.isoformat(),
            }
            for a in pending_approvals
        ],
        "checkpoints": [
            {
                "id": str(cp.id),
                "runId": str(cp.run_id),
                "runAttempt": attempt,
                "seq": cp.seq,
                "kind": cp.kind,
                "data": cp.data,
                "createdAt": cp.created_at.isoformat(),
            }
            for cp, attempt in checkpoints
        ],
        "childHandles": {
            "items": [child_handle_body(view) for view in child_page.items],
            "hasMore": child_page.has_more,
        },
        "pendingControlMessages": [
            {
                "id": str(message.id),
                "runId": str(message.run_id),
                "seq": message.seq,
                "operation": message.operation,
                "status": message.status,
                "causalPosition": message.causal_position,
                "directive": message.directive,
                "reason": message.reason,
                "version": message.version,
                "acceptedAt": message.accepted_at.isoformat(),
            }
            for message in pending_control_messages
        ],
        "availableSkills": skills,
        "eventCursor": event_cursor,
    }


async def list_approvals(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
    task_id: uuid.UUID | None = None,
) -> Page[Approval]:
    await authorize(ctx, Permission.APPROVALS_READ)
    if status is not None and status not in set(ApprovalStatus):
        raise ValidationError("invalid_status", f"Unknown approval status: {status}")
    # Approvals of invisible workspaces and work are not listed (CP-ADR-0082 §4).
    stmt = select(Approval).where(Approval.tenant_id == ctx.tenant_id, approval_condition(ctx))
    if status is not None:
        stmt = stmt.where(Approval.status == status)
    if task_id is not None:
        stmt = stmt.where(Approval.task_id == task_id)
    return await _paginate(
        session,
        stmt,
        created_col=Approval.created_at,
        id_col=Approval.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_approval(session: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID) -> Approval:
    await authorize(ctx, Permission.APPROVALS_READ)
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
    )
    # An invisible approval answers as a missing one (CP-ADR-0082 §3.7).
    if approval is None or not await approval_visible(session, ctx, approval):
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    return approval
