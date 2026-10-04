"""API v1 router aggregation."""

from fastapi import APIRouter, Depends

from control_plane.api.nul_guard import reject_nul_in_parameters
from control_plane.api.strict_query import reject_unknown_query_params
from control_plane.api.v1 import (
    agents,
    approvals,
    artifact_contents,
    artifact_types,
    artifacts,
    attention,
    authz,
    bootstrap,
    calendars,
    child_handles,
    claims,
    connection_types,
    connections,
    context,
    delegations,
    events,
    external_references,
    goals,
    harness,
    knowledge,
    observations,
    operations,
    org,
    package_settings,
    packages,
    principal_org,
    principals,
    processes,
    projects,
    rules,
    runs,
    sessions,
    skill_invocations,
    task_comments,
    task_types,
    tasks,
    tools,
    views,
    workspace_types,
    workspaces,
)

# An unknown query parameter is a 400, never a silently dropped filter
# (see control_plane.api.strict_query and docs/api.md); a known one with NUL
# is a 422 before it reaches the database (CP-ADR-0083).
api_v1_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(reject_unknown_query_params), Depends(reject_nul_in_parameters)],
)
api_v1_router.include_router(bootstrap.router)
api_v1_router.include_router(principals.router)
api_v1_router.include_router(principal_org.router)
api_v1_router.include_router(delegations.router)
api_v1_router.include_router(sessions.router)
api_v1_router.include_router(harness.router)
api_v1_router.include_router(tools.router)
api_v1_router.include_router(workspace_types.router)
api_v1_router.include_router(workspaces.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(external_references.router)
api_v1_router.include_router(org.router)
api_v1_router.include_router(skill_invocations.router)
api_v1_router.include_router(task_types.router)
api_v1_router.include_router(tasks.router)
api_v1_router.include_router(goals.router)
api_v1_router.include_router(rules.router)
api_v1_router.include_router(processes.router)
api_v1_router.include_router(calendars.router)
api_v1_router.include_router(packages.router)
api_v1_router.include_router(package_settings.router)
api_v1_router.include_router(views.router)
api_v1_router.include_router(task_comments.router)
api_v1_router.include_router(claims.router)
api_v1_router.include_router(runs.router)
api_v1_router.include_router(child_handles.router)
api_v1_router.include_router(artifact_types.router)
api_v1_router.include_router(artifact_contents.router)
api_v1_router.include_router(artifacts.router)
api_v1_router.include_router(agents.router)
api_v1_router.include_router(connection_types.router)
api_v1_router.include_router(connections.router)
api_v1_router.include_router(approvals.router)
api_v1_router.include_router(attention.router)
api_v1_router.include_router(authz.router)
api_v1_router.include_router(events.router)
api_v1_router.include_router(observations.router)
api_v1_router.include_router(context.router)
api_v1_router.include_router(knowledge.router)
api_v1_router.include_router(operations.router)
