"""Work rule read side: one rule, a page of rules, evaluations of a rule or by id (CP-ADR-0063).

``rules.read`` is decided on the rule's workspace (the tenant for a
tenant-level rule), like ``goals.read``: in policy mode a listing narrows to
rules in workspaces the caller may read plus tenant-level rules.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize, visible_objects
from control_plane.application.commands.work_rules import get_tenant_rule, rule_scope
from control_plane.application.queries.lists import Page, _paginate, clamp_limit
from control_plane.application.queries.package_links import in_package
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.work_rules import RULE_STATUSES, EvaluationStatus, RuleStatus
from control_plane.infrastructure.db.models import RuleEvaluation, WorkRule

_EVALUATION_STATUSES = frozenset(s.value for s in EvaluationStatus)


async def get_rule(session: AsyncSession, ctx: AuthContext, rule_id: uuid.UUID) -> WorkRule:
    await authorize(ctx, Permission.RULES_READ)
    rule = await get_tenant_rule(session, ctx, rule_id)
    if rule.workspace_id is not None:
        await authorize(ctx, Permission.RULES_READ, resource=rule_scope(rule.workspace_id))
    return rule


async def list_rules(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
    workspace_id: uuid.UUID | None = None,
    key: str | None = None,
    trigger_kind: str | None = None,
    package: str | None = None,
) -> Page[WorkRule]:
    """Newest first; archived rules only when asked for by status."""
    await authorize(ctx, Permission.RULES_READ)
    stmt = select(WorkRule).where(WorkRule.tenant_id == ctx.tenant_id)
    workspaces = await visible_objects(ctx, Permission.RULES_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                WorkRule.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                WorkRule.workspace_id.is_(None),
            )
        )
    if status is not None:
        if status not in RULE_STATUSES:
            raise ValidationError(
                "invalid_status", f"status must be one of {sorted(RULE_STATUSES)}"
            )
        stmt = stmt.where(WorkRule.status == status)
    else:
        stmt = stmt.where(WorkRule.status != RuleStatus.ARCHIVED)
    if workspace_id is not None:
        stmt = stmt.where(WorkRule.workspace_id == workspace_id)
    if key is not None:
        stmt = stmt.where(WorkRule.key == key)
    if trigger_kind is not None:
        stmt = stmt.where(WorkRule.trigger["kind"].astext == trigger_kind)
    if package is not None:
        stmt = stmt.where(in_package("WorkRule", WorkRule.tenant_id, WorkRule.key, package))
    return await _paginate(
        session,
        stmt,
        created_col=WorkRule.created_at,
        id_col=WorkRule.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def list_rule_evaluations(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    rule_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
) -> Page[RuleEvaluation]:
    """The rule's history, newest first — what it looked at and what it did."""
    rule = await get_rule(session, ctx, rule_id)
    stmt = select(RuleEvaluation).where(RuleEvaluation.rule_id == rule.id)
    if status is not None:
        if status not in _EVALUATION_STATUSES:
            raise ValidationError(
                "invalid_status", f"status must be one of {sorted(_EVALUATION_STATUSES)}"
            )
        stmt = stmt.where(RuleEvaluation.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=RuleEvaluation.created_at,
        id_col=RuleEvaluation.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_rule_evaluation(
    session: AsyncSession, ctx: AuthContext, evaluation_id: uuid.UUID
) -> RuleEvaluation:
    """One evaluation by id — the target of ``origin.ref = rule_evaluation:<id>``.

    Decided like the rule itself: ``rules.read`` on the rule's workspace. An
    evaluation of another tenant is not found.
    """
    await authorize(ctx, Permission.RULES_READ)
    row = (
        await session.execute(
            select(RuleEvaluation, WorkRule.workspace_id)
            .join(WorkRule, WorkRule.id == RuleEvaluation.rule_id)
            .where(RuleEvaluation.id == evaluation_id, RuleEvaluation.tenant_id == ctx.tenant_id)
        )
    ).first()
    # Of a rule of an invisible workspace: the evaluation's own 404, not the
    # workspace's (CP-ADR-0082 §3.7).
    if row is None or (row[1] is not None and not ctx.sees_workspace(row[1])):
        raise NotFoundError(
            "Rule evaluation not found", details={"evaluationId": str(evaluation_id)}
        )
    evaluation: RuleEvaluation = row[0]
    workspace_id = row[1]
    if workspace_id is not None:
        await authorize(ctx, Permission.RULES_READ, resource=rule_scope(workspace_id))
    return evaluation
