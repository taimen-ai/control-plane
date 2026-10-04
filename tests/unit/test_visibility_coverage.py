"""Every ``/api/v1`` route decides about visibility (CP-ADR-0082 §4, T004).

A route is in exactly one class:

* **seam** — an entry of ``SEAM`` below: ``(method, path)`` to the resolver
  or filter it passes the workspace seam through. An entry of a route that
  does not exist fails, and so does a resolver that does not;
* **tenant** — a comment ``# visibility: tenant — <reason>`` right above the
  route's decorator; a mark without a reason fails.

A route in neither class fails: a new route decides about visibility
explicitly. The registry proves only the classification; the behaviour is
proven by ``tests/integration/test_workspace_visibility.py`` and the
end-to-end ``tests/integration/test_visibility_invoice_payment.py`` (SC-002).
"""

import ast
import importlib
import inspect
import re
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute

from control_plane.api.v1.router import api_v1_router

A = "control_plane.application"
Q = f"{A}.queries"
C = f"{A}.commands"

WS = "WS"

# (method, path) -> the resolver or filter that applies the seam; a tuple when
# the route takes references to several objects and each passes its own.
SEAM: dict[tuple[str, str], str | tuple[str, ...]] = {
    # approvals: the workspace of the approval and of its work
    ("POST", "/api/v1/approvals"): f"{C}.approvals.request_approval",
    ("GET", "/api/v1/approvals"): f"{Q}.execution.list_approvals",
    ("GET", "/api/v1/approvals/{approval_id}"): f"{Q}.execution.get_approval",
    ("POST", "/api/v1/approvals/{approval_id}:approve"): f"{C}.approvals.decision_gate",
    ("POST", "/api/v1/approvals/{approval_id}:reject"): f"{C}.approvals.decision_gate",
    ("POST", "/api/v1/approvals/{approval_id}:cancel"): f"{C}.approvals.cancel_approval",
    ("GET", "/api/v1/approvals/{approval_id}/outcome"): f"{Q}.execution.get_approval",
    ("POST", "/api/v1/approvals/{approval_id}:replay-outcome"): (
        f"{C}.approval_outcomes.replay_outcome"
    ),
    # artifacts: the workspace of the artifact's work, or its own
    ("POST", "/api/v1/artifacts"): f"{C}.artifacts.create_artifact",
    ("GET", "/api/v1/artifacts"): f"{Q}.execution.list_artifacts",
    ("GET", "/api/v1/artifacts/{artifact_id}"): f"{C}.artifacts.get_readable_artifact",
    ("GET", "/api/v1/artifacts/{artifact_id}/content"): f"{C}.artifacts.open_content",
    ("POST", "/api/v1/artifacts/{artifact_id}:purge-content"): f"{C}.artifacts.purge_content",
    # "waiting for you"
    ("GET", "/api/v1/me/attention"): f"{Q}.attention.get_attention",
    ("POST", "/api/v1/me/attention/{item_key}:feedback"): f"{Q}.attention.find_item",
    # the gates of the routes it asks about
    ("POST", "/api/v1/authz:check"): f"{Q}.authz_check.check",
    # runs, claims and what hangs off them: the workspace of their work
    ("POST", "/api/v1/runs/{run_id}/child-handles"): f"{C}.runs._get_tenant_run",
    ("GET", "/api/v1/runs/{run_id}/child-handles"): f"{Q}.child_runs.list_child_handles",
    ("GET", "/api/v1/child-handles/{ref}"): f"{Q}.child_runs.resolve_child_handle",
    ("POST", "/api/v1/child-handles/{handle_id}:revoke"): f"{C}.child_runs.revoke_child_handle",
    ("GET", "/api/v1/claims"): f"{Q}.lists.list_claims",
    ("GET", "/api/v1/claims/{claim_id}"): f"{Q}.lists.get_claim",
    ("POST", "/api/v1/claims/{claim_id}:heartbeat"): f"{C}.claims.heartbeat_claim",
    ("POST", "/api/v1/claims/{claim_id}:release"): f"{C}.claims._get_tenant_claim",
    ("POST", "/api/v1/claims/{claim_id}:reclaim"): f"{C}.claims._get_tenant_claim",
    ("GET", "/api/v1/runs"): f"{Q}.execution.list_runs",
    ("GET", "/api/v1/runs/{run_id}"): f"{Q}.execution.get_run",
    ("POST", "/api/v1/runs/{run_id}/control-messages"): f"{C}.runs._get_tenant_run",
    ("GET", "/api/v1/runs/{run_id}/control-messages"): f"{Q}.execution.get_run",
    ("POST", "/api/v1/runs/{run_id}/control-messages/{message_id}:acknowledge"): (
        f"{C}.runs._get_tenant_run"
    ),
    ("GET", "/api/v1/runs/{run_id}/context"): f"{Q}.execution.get_run_context",
    ("GET", "/api/v1/runs/{run_id}/checkpoints"): f"{Q}.execution.get_run",
    ("POST", "/api/v1/runs/{run_id}/checkpoints"): f"{C}.execution.create_checkpoint",
    ("GET", "/api/v1/runs/{run_id}/actions"): f"{Q}.execution.get_run",
    ("POST", "/api/v1/runs/{run_id}/actions"): f"{C}.execution.record_run_action",
    ("POST", "/api/v1/runs/{run_id}/actions/{action_id}:finish"): (
        f"{C}.execution.finish_run_action"
    ),
    ("POST", "/api/v1/runs/{run_id}:suspend"): f"{C}.runs._get_tenant_run",
    ("POST", "/api/v1/runs/{run_id}:handoff"): f"{C}.runs._get_tenant_run",
    ("POST", "/api/v1/runs/{run_id}:request-cancel"): f"{C}.runs._get_tenant_run",
    ("POST", "/api/v1/runs/{run_id}:succeed"): f"{C}.runs._get_tenant_run",
    ("POST", "/api/v1/runs/{run_id}:fail"): f"{C}.runs._get_tenant_run",
    ("POST", "/api/v1/runs/{run_id}:cancel"): f"{C}.runs._get_tenant_run",
    # context, memory and recorded packs
    ("POST", "/api/v1/context"): f"{Q}.context.prepare_working_context",
    ("POST", "/api/v1/context/recall"): f"{Q}.recall.prepare_recall",
    ("GET", "/api/v1/context-packs/{pack_id}"): f"{Q}.task_context.get_pack_record",
    ("POST", "/api/v1/context-packs/{pack_id}:replay"): f"{Q}.task_context.get_pack_record",
    # the journal: events.workspace_id in the set, or of the tenant
    ("GET", "/api/v1/events"): f"{Q}.events.authorize_event_read",
    ("GET", "/api/v1/events:export"): f"{Q}.events.authorize_event_read",
    (WS, "/api/v1/events/ws"): f"{Q}.events.authorize_event_read",
    # external references: the workspace of the entity they map onto
    # resolve_entity_id dispatches to the loader of the entity type
    ("POST", "/api/v1/external-references"): (
        f"{A}.external_entities._load_project",
        f"{A}.external_entities._load_task",
    ),
    ("GET", "/api/v1/external-references"): f"{Q}.external_references.lookup_by_external_key",
    ("GET", "/api/v1/projects/{project_id}/external-references"): (
        f"{Q}.projects.get_tenant_project"
    ),
    ("POST", "/api/v1/projects/{project_id}/external-references"): (
        f"{A}.external_entities._load_project"
    ),
    # goals
    ("POST", "/api/v1/goals"): (
        f"{C}.workspaces.require_active_workspace",
        f"{C}.goals.verify_evidence",
    ),
    ("GET", "/api/v1/goals"): f"{Q}.goals.list_goals",
    ("GET", "/api/v1/goals/{goal_id}"): f"{C}.goals.get_tenant_goal",
    ("PATCH", "/api/v1/goals/{goal_id}"): f"{C}.goals.get_tenant_goal",
    ("GET", "/api/v1/goals/{goal_id}/work"): f"{Q}.lists.list_tasks",
    # the executor's context and its queue
    ("GET", "/api/v1/harness/context"): f"{Q}.harness.get_harness_context",
    ("GET", "/api/v1/work/available"): f"{Q}.discovery.list_available_work",
    # knowledge: the workspace of the request
    ("POST", "/api/v1/knowledge/snapshots"): f"{C}.knowledge.prepare_snapshot",
    ("POST", "/api/v1/knowledge/snapshots:preview"): f"{C}.knowledge.prepare_snapshot",
    ("POST", "/api/v1/knowledge/documents"): f"{C}.knowledge.prepare_snapshot",
    ("POST", "/api/v1/knowledge/entities:query"): (
        f"{Q}.knowledge_entities.prepare_entities_query"
    ),
    ("GET", "/api/v1/workspaces/{workspace_id}/knowledge-packs"): (
        f"{C}.knowledge.prepare_workspace_packs_read"
    ),
    ("PUT", "/api/v1/workspaces/{workspace_id}/knowledge-packs"): (
        f"{C}.knowledge.prepare_workspace_packs"
    ),
    # the workspace, the work, the run and the superseded observation it names
    ("POST", "/api/v1/observations"): (
        f"{C}.workspaces.get_tenant_workspace",
        f"{C}.relations.resolve_task",
        f"{C}.observations.record_observation",
        f"{Q}.events.recorded_observations",
    ),
    # processes
    ("POST", "/api/v1/process-definitions"): f"{C}.process_definitions._writable",
    ("GET", "/api/v1/process-definitions"): f"{C}.process_definitions.list_process_definitions",
    ("GET", "/api/v1/process-definitions/{ref}"): (
        f"{C}.process_definitions.resolve_process_definition"
    ),
    ("GET", "/api/v1/process-definitions/{key}/versions"): (
        f"{C}.process_definitions.list_process_versions"
    ),
    ("POST", "/api/v1/process-definitions/{key}:replay"): (
        f"{C}.process_definitions.resolve_process_definition"
    ),
    ("POST", "/api/v1/process-definitions/{key}:retire"): f"{C}.process_definitions._writable",
    ("POST", "/api/v1/process-instances"): f"{C}.workspaces.require_active_workspace",
    ("GET", "/api/v1/process-instances"): f"{C}.process_instances.list_instances",
    ("GET", "/api/v1/process-instances/{instance_id}"): f"{C}.process_instances.get_instance",
    ("GET", "/api/v1/process-instances/{instance_id}/journal"): (
        f"{C}.process_instances.get_instance"
    ),
    ("POST", "/api/v1/process-instances/{instance_id}:suspend"): (
        f"{C}.process_instances.get_instance"
    ),
    ("POST", "/api/v1/process-instances/{instance_id}:resume"): (
        f"{C}.process_instances.get_instance"
    ),
    ("POST", "/api/v1/process-instances/{instance_id}:cancel"): (
        f"{C}.process_instances.get_instance"
    ),
    # projects: the workspace of the project
    ("POST", "/api/v1/projects"): f"{C}.workspaces.get_tenant_workspace",
    ("GET", "/api/v1/projects"): f"{Q}.projects.list_projects",
    ("GET", "/api/v1/projects/{project_id}"): f"{Q}.projects.get_tenant_project",
    ("PATCH", "/api/v1/projects/{project_id}"): f"{Q}.projects.get_tenant_project",
    ("POST", "/api/v1/projects/{project_id}:archive"): f"{Q}.projects.get_tenant_project",
    ("POST", "/api/v1/projects/{project_id}:transition"): f"{Q}.projects.get_tenant_project",
    ("GET", "/api/v1/projects/{project_id}/effective-config"): (f"{Q}.projects.get_tenant_project"),
    ("GET", "/api/v1/projects/{project_id}/config-revisions"): (f"{Q}.projects.get_tenant_project"),
    ("POST", "/api/v1/projects/{project_id}/config-revisions"): (
        f"{Q}.projects.get_tenant_project"
    ),
    ("POST", "/api/v1/projects/{project_id}/config-revisions/{revision}:activate"): (
        f"{Q}.projects.get_tenant_project"
    ),
    # rules
    ("POST", "/api/v1/rules"): f"{C}.workspaces.require_active_workspace",
    ("GET", "/api/v1/rules"): f"{Q}.work_rules.list_rules",
    ("GET", "/api/v1/rules/{rule_id}"): f"{C}.work_rules.get_tenant_rule",
    ("PATCH", "/api/v1/rules/{rule_id}"): f"{C}.work_rules.get_tenant_rule",
    ("POST", "/api/v1/rules/{rule_id}:enable"): f"{C}.work_rules.get_tenant_rule",
    ("POST", "/api/v1/rules/{rule_id}:disable"): f"{C}.work_rules.get_tenant_rule",
    ("DELETE", "/api/v1/rules/{rule_id}"): f"{C}.work_rules.get_tenant_rule",
    ("GET", "/api/v1/rules/{rule_id}/evaluations"): f"{C}.work_rules.get_tenant_rule",
    ("GET", "/api/v1/rule-evaluations/{evaluation_id}"): f"{Q}.work_rules.get_rule_evaluation",
    # skill calls: the workspace of their work; one without work is the tenant's
    ("POST", "/api/v1/skills/{skill_ref}:invoke"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/skill-invocations/{invocation_id}"): (
        f"{C}.skill_invocations._invocation_visible"
    ),
    ("POST", "/api/v1/skill-invocations:claim"): f"{C}.skill_invocations.claim_skill_invocation",
    ("POST", "/api/v1/skill-invocations/{invocation_id}:heartbeat"): (
        f"{C}.skill_invocations._invocation_visible"
    ),
    ("POST", "/api/v1/skill-invocations/{invocation_id}:complete"): (
        f"{C}.skill_invocations._invocation_visible"
    ),
    ("POST", "/api/v1/skill-invocations/{invocation_id}:fail"): (
        f"{C}.skill_invocations._invocation_visible"
    ),
    ("POST", "/api/v1/skill-invocations/{invocation_id}:cancel"): (
        f"{C}.skill_invocations._invocation_visible"
    ),
    # work by reference and everything under it
    ("POST", "/api/v1/tasks/{task_ref}/comments"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}/comments"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}/comments/{comment_id}"): f"{C}.relations.resolve_task",
    ("PATCH", "/api/v1/tasks/{task_ref}/comments/{comment_id}"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}/comments/{comment_id}/revisions"): (
        f"{C}.relations.resolve_task"
    ),
    ("GET", "/api/v1/task-types/{type_id}/executors"): f"{C}.workspaces.get_tenant_workspace",
    ("POST", "/api/v1/task-types/{type_id}:migrate-tasks"): (
        f"{C}.task_type_migration.migrate_type_tasks"
    ),
    ("POST", "/api/v1/tasks"): (
        f"{C}.workspaces.require_active_workspace",
        f"{C}.goals.verify_evidence",
    ),
    ("GET", "/api/v1/tasks"): f"{Q}.lists.list_tasks",
    ("GET", "/api/v1/tasks/{task_ref}/claimability"): f"{C}.relations.shown_prerequisites",
    ("GET", "/api/v1/tasks/{task_ref}/transitions"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}/verifications"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}"): f"{Q}.lists.get_task",
    ("PATCH", "/api/v1/tasks/{task_ref}"): (
        f"{C}.tasks.resolve_task_for_update",
        f"{C}.goals.verify_evidence",
    ),
    ("POST", "/api/v1/tasks/{task_ref}:claim"): f"{C}.relations.shown_prerequisites",
    ("POST", "/api/v1/tasks/{task_ref}:complete"): f"{C}.tasks.resolve_task_for_update",
    ("POST", "/api/v1/tasks/{task_ref}:migrate-type"): f"{C}.tasks.resolve_task_for_update",
    ("POST", "/api/v1/tasks/{task_ref}/relations"): f"{C}.relations.resolve_task",
    ("GET", "/api/v1/tasks/{task_ref}/relations"): f"{Q}.execution.list_task_relations",
    ("DELETE", "/api/v1/tasks/{task_ref}/relations/{relation_id}"): (
        f"{C}.relations.remove_relation"
    ),
    ("GET", "/api/v1/tasks/{task_ref}/requirements"): f"{C}.relations.resolve_task",
    ("POST", "/api/v1/tasks/{task_ref}:start-run"): f"{C}.tasks.resolve_task_for_update",
    # package screens: the form is the tenant's, its source data by the set
    ("GET", "/api/v1/views"): f"{Q}.views.list_views",
    ("GET", "/api/v1/views/{view_key}"): f"{Q}.views.get_view",
    ("POST", "/api/v1/views/{view_key}:query"): f"{Q}.view_data.prepare",
    # workspaces and their members
    ("POST", "/api/v1/workspaces"): f"{C}.workspaces.get_tenant_workspace",
    ("GET", "/api/v1/workspaces"): f"{Q}.org.list_workspaces",
    ("GET", "/api/v1/workspaces/tree"): f"{Q}.projects.workspace_tree",
    ("GET", "/api/v1/workspaces/{workspace_id}"): f"{Q}.org.get_workspace",
    ("PATCH", "/api/v1/workspaces/{workspace_id}"): f"{C}.workspaces.get_tenant_workspace",
    ("POST", "/api/v1/workspaces/{workspace_id}:archive"): f"{C}.workspaces.get_tenant_workspace",
    ("POST", "/api/v1/workspaces/{workspace_id}:move"): f"{C}.workspaces.get_tenant_workspace",
    ("POST", "/api/v1/workspaces/{workspace_id}/members"): f"{C}.workspaces.get_tenant_workspace",
    ("GET", "/api/v1/workspaces/{workspace_id}/members"): f"{Q}.org.get_workspace",
    ("GET", "/api/v1/workspaces/{workspace_id}/participants"): f"{Q}.org.get_workspace",
    ("POST", "/api/v1/workspaces/{workspace_id}/members/{principal_id}:remove"): (
        f"{C}.workspaces.get_tenant_workspace"
    ),
}

MARK = re.compile(r"^#\s*visibility:\s*tenant\b(?P<rest>.*)$")
REASON = re.compile(r"^\s*—\s*\S")


def _routes(router: Any, prefix: str = "") -> Iterator[tuple[str, str, Callable[..., Any]]]:
    """``(method, path, endpoint)`` of every route under ``router``.

    An included router is reached through the wrapper newer FastAPI keeps it
    in, or directly as its routes were in older versions.
    """
    for route in router.routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _routes(included, prefix + route.include_context.prefix)
        elif isinstance(route, APIRoute):
            for method in sorted(route.methods):
                yield method, prefix + route.path, route.endpoint
        elif isinstance(route, APIWebSocketRoute):
            yield WS, prefix + route.path, route.endpoint


ROUTES = sorted(_routes(api_v1_router), key=lambda r: (r[1], r[0]))


@cache
def _module_tree(path: str) -> tuple[ast.Module, list[str]]:
    source = Path(path).read_text(encoding="utf-8")
    return ast.parse(source), source.splitlines()


def tenant_mark(endpoint: Callable[..., Any]) -> str | None:
    """The comment line right above the route's first decorator, if it marks
    the route ``tenant``; ``None`` without a mark."""
    path = inspect.getsourcefile(endpoint)
    assert path is not None
    tree, lines = _module_tree(path)
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == endpoint.__name__
    )
    first = min(d.lineno for d in node.decorator_list)
    index = first - 2
    while index >= 0 and lines[index].startswith("#"):
        if MARK.match(lines[index]):
            return lines[index]
        index -= 1
    return None


def _resolve(reference: str) -> Any:
    module, _, name = reference.rpartition(".")
    return getattr(importlib.import_module(module), name, None)


def test_the_router_has_routes() -> None:
    assert len(ROUTES) > 200
    assert (WS, "/api/v1/events/ws") in {(m, p) for m, p, _ in ROUTES}


def test_every_route_is_in_exactly_one_class() -> None:
    unclassified, both = [], []
    for method, path, endpoint in ROUTES:
        seam = (method, path) in SEAM
        tenant = tenant_mark(endpoint) is not None
        if seam and tenant:
            both.append(f"{method} {path}")
        elif not seam and not tenant:
            unclassified.append(f"{method} {path}")
    assert not unclassified, (
        "Routes without a visibility decision: add them to SEAM with the resolver they "
        "pass the workspace seam through, or mark them '# visibility: tenant — <reason>' "
        f"(CP-ADR-0082 §4): {unclassified}"
    )
    assert not both, f"Routes both in SEAM and marked tenant: {both}"


def test_a_tenant_mark_names_its_reason() -> None:
    bare = []
    for method, path, endpoint in ROUTES:
        mark = tenant_mark(endpoint)
        if mark is None:
            continue
        match = MARK.match(mark)
        assert match is not None
        if not REASON.match(match["rest"]):
            bare.append(f"{method} {path}: {mark}")
    assert not bare, f"A tenant mark without a reason: {bare}"


def test_every_seam_entry_names_an_existing_route() -> None:
    known = {(method, path) for method, path, _ in ROUTES}
    stale = sorted(f"{m} {p}" for m, p in SEAM if (m, p) not in known)
    assert not stale, f"SEAM entries of routes that do not exist: {stale}"


def _references(entry: str | tuple[str, ...]) -> tuple[str, ...]:
    return (entry,) if isinstance(entry, str) else entry


def test_every_seam_entry_names_an_existing_resolver() -> None:
    missing = sorted(
        f"{m} {p} -> {ref}"
        for (m, p), entry in SEAM.items()
        for ref in _references(entry)
        if not callable(_resolve(ref))
    )
    assert not missing, f"SEAM entries naming no function: {missing}"


# What the seam is made of (``application/visibility.py``, ``permits_task``,
# the caller's set on ``AuthContext``): a function whose body names one of
# these, or a function that does, applies the seam.
SEAM_PRIMITIVES = frozenset(
    {
        "visible_workspaces",
        "visible_roots",
        "sees_workspace",
        "check_workspace_visible",
        "permits_task",
        "workspace_condition",
        "task_condition",
        "approval_condition",
        "artifact_condition",
        "visible_tasks",
        "task_visible",
        "approval_visible",
        "artifact_visible",
        "visible_tuple",
    }
)


@cache
def _seam_functions() -> frozenset[str]:
    """Names of the application's functions whose body reaches the seam.

    A fixed point over the bodies of every function in ``application``: a
    function is in when it names a primitive or a function already in. By
    name, not by import: two functions of one name count as one, which can
    only let a resolver in, never keep one out wrongly — the integration
    tests prove the behaviour, this proves the body is not seam-free.
    """
    root = Path(importlib.import_module(A).__file__ or "").parent
    names: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        tree, _ = _module_tree(str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                used = names.setdefault(node.name, set())
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Name):
                        used.add(inner.id)
                    elif isinstance(inner, ast.Attribute):
                        used.add(inner.attr)
    seam = set(SEAM_PRIMITIVES)
    while True:
        grown = {name for name, used in names.items() if used & seam} - seam
        if not grown:
            return frozenset(seam)
        seam |= grown


def test_every_seam_resolver_reaches_the_seam_in_its_body() -> None:
    """A resolver in ``SEAM`` that never asks about visibility is a wrong entry:
    the registry would classify the route and the route would leak (the T004
    review found ``POST /observations`` so)."""
    seam = _seam_functions()
    blind = sorted(
        f"{m} {p} -> {ref}"
        for (m, p), entry in SEAM.items()
        for ref in _references(entry)
        if ref.rpartition(".")[2] not in seam
    )
    assert not blind, f"SEAM resolvers whose body never reaches the seam: {blind}"


def test_the_body_check_tells_a_blind_function_from_a_seeing_one() -> None:
    seam = _seam_functions()
    assert {"task_visible", "resolve_task", "record_observation", "verify_evidence"} <= seam
    # Pure helpers of the domain and of the API never ask about visibility.
    assert "utcnow" not in seam
    assert "normalize_evidence" not in seam


def test_the_mark_is_read_as_the_registry_reads_it() -> None:
    """The parser itself: a mark with a reason, one without, and a foreign comment."""
    assert MARK.match("# visibility: tenant — principals are tenant objects")
    rest = MARK.match("# visibility: tenant")
    assert rest is not None and not REASON.match(rest["rest"])
    blank = MARK.match("# visibility: tenant — ")
    assert blank is not None and not REASON.match(blank["rest"])
    assert MARK.match("# authz: public — a reason") is None
