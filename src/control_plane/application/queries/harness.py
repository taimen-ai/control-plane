"""Harness bootstrap context: "who am I, where am I, what am I doing?" (v0.3)

One efficient read that lets a (re)starting harness recover its coordination
state without a dozen sequential requests. Everything here is ABOUT THE
CALLING PRINCIPAL ITSELF, so no extra permission beyond authentication is
required (ADR-0016): a principal may always see its own profile, sessions,
claims, runs and the approvals addressed to it.

The data is authoritative-at-read-time; nothing is cached server-side.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.application.common import utcnow
from control_plane.application.event_cursor import encode_position
from control_plane.application.queries.events import current_position
from control_plane.application.visibility import (
    approval_condition,
    task_condition,
    workspace_condition,
)
from control_plane.domain.enums import (
    HARNESS_PROTOCOL_NAME,
    SUPPORTED_HARNESS_PROTOCOL_VERSIONS,
    ApprovalStatus,
    ClaimStatus,
    RunStatus,
    SessionStatus,
    SkillStatus,
)
from control_plane.domain.errors import NotFoundError
from control_plane.domain.tool_discovery import normalize_harness_protocols
from control_plane.infrastructure.db.models import (
    Approval,
    Capability,
    Principal,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    Role,
    Run,
    Session,
    Skill,
    Task,
    TaskClaim,
    Tenant,
)

_SUSPENDED_RUNS_LIMIT = 20
_PENDING_APPROVALS_LIMIT = 50


async def current_event_cursor(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Opaque start-of-following cursor for the tenant.

    The greatest stable ``(tx_id, sequence)`` position (the journal readers'
    rule verbatim): replaying after it yields every event that was not yet
    stable at bootstrap. Pending transactions all sort after it
    (``tx_id >= xmin``), so nothing that later commits can be skipped — the
    v0.3 sequence-cursor gap is closed (see application/event_cursor.py).
    """
    return encode_position(await current_position(session, tenant_id))


async def resolve_executable_skills(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    harness_capabilities: list[str] | None,
) -> list[dict[str, Any]]:
    """Skills assigned to the principal that this harness could execute.

    Deterministic resolution: assigned ∧ not disabled ∧ (harness declared
    support for the skill's protocol, or declared no skill protocols at all —
    a legacy/protocol-agnostic session sees everything and filters locally).
    The Control Plane never executes skills; it reports availability.
    """
    rows = (
        await session.execute(
            select(Skill)
            .join(PrincipalSkill, PrincipalSkill.skill_id == Skill.id)
            .where(
                PrincipalSkill.tenant_id == ctx.tenant_id,
                PrincipalSkill.principal_id == ctx.principal_id,
                Skill.status != SkillStatus.DISABLED,
            )
            .order_by(Skill.name, Skill.created_at.desc())
        )
    ).scalars()

    declared = normalize_harness_protocols(harness_capabilities)
    executable: list[dict[str, Any]] = []
    for skill in rows:
        supported = declared is None or skill.protocol in declared
        executable.append(
            {
                "id": str(skill.id),
                "name": skill.name,
                "version": skill.version,
                "protocol": skill.protocol,
                "status": skill.status,
                "executable": supported,
            }
        )
    return executable


def _claim_summary(claim: TaskClaim, task: Task | None) -> dict[str, Any]:
    return {
        "id": str(claim.id),
        "taskId": str(claim.task_id),
        "taskPublicId": task.public_id if task else None,
        "taskTitle": task.title if task else None,
        "taskStatus": task.status if task else None,
        "sessionId": str(claim.session_id),
        "fencingToken": claim.fencing_token,
        "expiresAt": claim.expires_at.isoformat(),
    }


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "taskId": str(run.task_id),
        "claimId": str(run.claim_id),
        "sessionId": str(run.session_id),
        "status": run.status,
        "attempt": run.attempt,
        "fencingToken": run.fencing_token,
        "startedAt": run.started_at.isoformat(),
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "cancelRequestedAt": (
            run.cancel_requested_at.isoformat() if run.cancel_requested_at else None
        ),
    }


def _session_summary(work_session: Session) -> dict[str, Any]:
    return {
        "id": str(work_session.id),
        "status": work_session.status,
        "clientName": work_session.client_name,
        "clientVersion": work_session.client_version,
        "harnessType": work_session.harness_type,
        "harnessVersion": work_session.harness_version,
        "controlLevel": work_session.control_level,
        "protocolVersion": work_session.protocol_version,
        "harnessCapabilities": work_session.harness_capabilities,
        "startedAt": work_session.started_at.isoformat(),
        "expiresAt": work_session.expires_at.isoformat(),
    }


async def get_harness_context(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    session_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    # The cursor is taken FIRST, before any state read. Under READ COMMITTED
    # every statement gets its own snapshot: if the cursor were computed
    # last, a transition committed between a state read and the cursor read
    # (e.g. run.suspended) would be reflected in neither the state nor the
    # replay stream — permanently lost. Cursor-first makes the race
    # at-least-once instead: such an event is re-delivered even though the
    # state already reflects it (documented duplicate-tolerant contract).
    event_cursor = await current_event_cursor(session, ctx.tenant_id)
    now = utcnow()
    principal = await session.get(Principal, ctx.principal_id)
    tenant = await session.get(Tenant, ctx.tenant_id)
    if principal is None or tenant is None:  # pragma: no cover - auth guarantees
        raise NotFoundError("Principal not found")

    requested_session: Session | None = None
    if session_id is not None:
        requested_session = await session.scalar(
            select(Session).where(
                Session.id == session_id,
                Session.tenant_id == ctx.tenant_id,
                Session.principal_id == ctx.principal_id,
            )
        )
        if requested_session is None:
            raise NotFoundError("Session not found", details={"sessionId": str(session_id)})

    active_sessions = (
        await session.scalars(
            select(Session)
            .where(
                Session.tenant_id == ctx.tenant_id,
                Session.principal_id == ctx.principal_id,
                Session.status == SessionStatus.ACTIVE,
                Session.expires_at > now,
            )
            .order_by(Session.started_at.desc())
        )
    ).all()

    claims = (
        await session.execute(
            select(TaskClaim, Task)
            .join(Task, Task.id == TaskClaim.task_id)
            .where(
                TaskClaim.tenant_id == ctx.tenant_id,
                TaskClaim.holder_id == ctx.principal_id,
                TaskClaim.status == ClaimStatus.ACTIVE,
                TaskClaim.expires_at > now,
                # Only work of the visible workspaces (CP-ADR-0082 §4).
                workspace_condition(ctx, Task.workspace_id),
            )
            .order_by(TaskClaim.acquired_at.desc())
        )
    ).all()

    active_runs = (
        await session.scalars(
            select(Run)
            .where(
                Run.tenant_id == ctx.tenant_id,
                Run.principal_id == ctx.principal_id,
                Run.status == RunStatus.RUNNING,
                task_condition(ctx, Run.task_id),
            )
            .order_by(Run.started_at.desc())
        )
    ).all()
    suspended_runs = (
        await session.scalars(
            select(Run)
            .where(
                Run.tenant_id == ctx.tenant_id,
                Run.principal_id == ctx.principal_id,
                Run.status == RunStatus.SUSPENDED,
                task_condition(ctx, Run.task_id),
            )
            .order_by(Run.finished_at.desc())
            .limit(_SUSPENDED_RUNS_LIMIT)
        )
    ).all()

    role_rows = (
        await session.execute(
            select(PrincipalRole, Role)
            .join(Role, Role.id == PrincipalRole.role_id)
            .where(
                PrincipalRole.tenant_id == ctx.tenant_id,
                PrincipalRole.principal_id == ctx.principal_id,
            )
            .order_by(PrincipalRole.created_at)
        )
    ).all()
    capability_rows = (
        (
            await session.execute(
                select(Capability)
                .join(
                    PrincipalCapability,
                    PrincipalCapability.capability_id == Capability.id,
                )
                .where(
                    PrincipalCapability.tenant_id == ctx.tenant_id,
                    PrincipalCapability.principal_id == ctx.principal_id,
                )
                .order_by(PrincipalCapability.created_at)
            )
        )
        .scalars()
        .all()
    )

    harness_capabilities = None
    if requested_session is not None:
        harness_capabilities = requested_session.harness_capabilities
    elif active_sessions:
        harness_capabilities = active_sessions[0].harness_capabilities
    skills = await resolve_executable_skills(
        session, ctx, harness_capabilities=harness_capabilities
    )

    # Approvals addressed to me: directly, or through a role I hold IN SCOPE.
    # A role assignment scoped to one workspace must not surface approvals of
    # another subtree — the caller could not decide those anyway.
    my_role_ids = [assignment.role_id for assignment, _ in role_rows]
    approval_filter = Approval.assigned_principal_id == ctx.principal_id
    if my_role_ids:
        approval_filter = approval_filter | Approval.required_role_id.in_(my_role_ids)
    candidates = (
        await session.scalars(
            select(Approval)
            .where(
                Approval.tenant_id == ctx.tenant_id,
                Approval.status == ApprovalStatus.PENDING,
                approval_filter,
                approval_condition(ctx),
            )
            .order_by(Approval.created_at.desc())
            .limit(_PENDING_APPROVALS_LIMIT * 4)
        )
    ).all()

    global_role_ids = {
        assignment.role_id for assignment, _ in role_rows if assignment.workspace_id is None
    }
    scoped_role_ids: dict[uuid.UUID, set[uuid.UUID]] = {}
    for assignment, _ in role_rows:
        if assignment.workspace_id is not None:
            scoped_role_ids.setdefault(assignment.role_id, set()).add(assignment.workspace_id)

    pending_approvals: list[Approval] = []
    for approval in candidates:
        addressed = (
            approval.assigned_principal_id == ctx.principal_id
            or approval.required_role_id in global_role_ids
        )
        if not addressed and (
            approval.required_role_id in scoped_role_ids and approval.workspace_id is not None
        ):
            ancestors = set(
                await workspace_ancestor_ids(session, ctx.tenant_id, approval.workspace_id)
            )
            addressed = bool(ancestors & scoped_role_ids[approval.required_role_id])
        if addressed:
            pending_approvals.append(approval)
            if len(pending_approvals) >= _PENDING_APPROVALS_LIMIT:
                break

    return {
        "protocol": {
            "name": HARNESS_PROTOCOL_NAME,
            "supportedVersions": sorted(SUPPORTED_HARNESS_PROTOCOL_VERSIONS),
        },
        "tenant": {"id": str(tenant.id), "slug": tenant.slug, "name": tenant.name},
        "principal": {
            "id": str(principal.id),
            "kind": principal.kind,
            "displayName": principal.display_name,
            "status": principal.status,
        },
        "session": _session_summary(requested_session) if requested_session else None,
        "activeSessions": [_session_summary(s) for s in active_sessions],
        "activeClaims": [_claim_summary(c, t) for c, t in claims],
        "activeRuns": [_run_summary(r) for r in active_runs],
        "suspendedRuns": [_run_summary(r) for r in suspended_runs],
        "roles": [
            {
                "id": str(role.id),
                "slug": role.slug,
                "name": role.name,
                "workspaceId": str(assignment.workspace_id) if assignment.workspace_id else None,
            }
            for assignment, role in role_rows
        ],
        "capabilities": [
            {"id": str(c.id), "name": c.name, "description": c.description} for c in capability_rows
        ],
        "skills": skills,
        "pendingApprovals": [
            {
                "id": str(a.id),
                "taskId": str(a.task_id) if a.task_id else None,
                "artifactId": str(a.artifact_id) if a.artifact_id else None,
                "requestedBy": str(a.requested_by_principal_id),
                "gate": a.gate,
                "comment": a.comment,
                "createdAt": a.created_at.isoformat(),
            }
            for a in pending_approvals
        ],
        "eventCursor": event_cursor,
        "permissions": sorted(ctx.permissions),
    }
