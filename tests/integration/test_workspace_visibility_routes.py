"""Every route of an object that lives in a workspace passes the seam (CP-ADR-0082 §4, T004).

A person in ``members`` mode, a member of ``dept`` (so of ``dept`` and
``team``), reads the tenant through an API key: the mode is the principal's,
whatever the entry (§3.2). Whatever lives in ``other`` — work, approvals,
artifacts, runs, claims, events, projects, the workspace itself — answers to
that person exactly as if it did not exist: missing from lists, ``404`` with
the body of a missing id by reference, refused alike when acted upon (FR-007).
"""

import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.testclient import TestClient

from control_plane.config import Settings
from control_plane.main import create_app
from tests.helpers import (
    BOOTSTRAP_TOKEN,
    auth,
    claim_task,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
    open_session,
)
from tests.integration.test_iam_enforcement import ISSUER
from tests.integration.test_process_instances import GOAL, _publish, _setup
from tests.integration.test_projects_v05 import create_project, create_template
from tests.integration.test_workspace_visibility import same_as_missing

HUMAN_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "events.read",
    "workspaces.read",
    "approvals.read",
    "approvals.decide",
    "approvals.manage",
    "artifacts.read",
    "artifacts.write",
    "projects.read",
    "org.read",
    "processes.read",
    "processes.operate",
]
RUNNER_PERMISSIONS = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]
MISSING = "00000000-0000-4000-8000-000000000000"


def identity() -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
    }


class Tree:
    """Company -> Dept -> Team, an unrelated root Other; Ann is a member of Dept."""

    def __init__(self, client: httpx.AsyncClient, admin: str) -> None:
        self.client = client
        self.admin = admin
        self.ws: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.human = ""
        self.key = ""

    async def get(self, path: str, key: str | None = None, **params: Any) -> httpx.Response:
        return await self.client.get(path, params=params, headers=auth(key or self.key))

    async def post(self, path: str, body: Any = None, key: str | None = None) -> httpx.Response:
        return await self.client.post(
            path, json=body if body is not None else {}, headers=auth(key or self.key)
        )

    async def as_admin(self, path: str, body: Any) -> dict[str, Any]:
        response = await self.client.post(path, json=body, headers=auth(self.admin))
        assert response.status_code in (200, 201), response.text
        created: dict[str, Any] = response.json()
        return created

    async def narrow(self, principal_id: str | None = None) -> None:
        bound = await self.client.post(
            f"/api/v1/principals/{principal_id or self.human}/iam-bindings",
            json={**identity(), "permissions": ["tasks.read"], "visibility": "members"},
            headers=auth(self.admin),
        )
        assert bound.status_code == 201, bound.text

    async def add_member(self, workspace: str, principal_id: str | None = None) -> None:
        response = await self.client.post(
            f"/api/v1/workspaces/{self.ws[workspace]}/members",
            json={"principalId": principal_id or self.human},
            headers=auth(self.admin),
        )
        assert response.status_code == 201, response.text


async def make_tree(
    client: httpx.AsyncClient, *, narrowed: bool = True, admin: str | None = None
) -> Tree:
    admin = admin or (await do_bootstrap(client))["apiKey"]["key"]
    tree = Tree(client, admin)
    company = await create_workspace(client, admin, "company")
    dept = await create_workspace(client, admin, "dept", parent_id=company["id"])
    team = await create_workspace(client, admin, "team", parent_id=dept["id"])
    other = await create_workspace(client, admin, "other")
    tree.ws = {"company": company["id"], "dept": dept["id"], "team": team["id"]}
    tree.ws["other"] = other["id"]
    for name in ("dept", "other"):
        tree.tasks[name] = await tree.as_admin(
            "/api/v1/tasks", {"title": f"work of {name}", "workspaceId": tree.ws[name]}
        )
    tree.tasks["none"] = await tree.as_admin("/api/v1/tasks", {"title": "loose"})
    human, tree.key = await create_agent_with_key(
        client, admin, name="ann", permissions=HUMAN_PERMISSIONS, kind="human"
    )
    tree.human = human["id"]
    await tree.add_member("dept")
    if narrowed:
        await tree.narrow()
    return tree


def ids(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def run_on(tree: Tree, runner_key: str, task_id: str) -> dict[str, Any]:
    """A tenant-wide executor claims the work and starts a run on it."""
    session = await open_session(tree.client, runner_key)
    claimed = await claim_task(tree.client, runner_key, task_id, session["id"])
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    started = await tree.post(
        f"/api/v1/tasks/{task_id}:start-run",
        {"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        key=runner_key,
    )
    assert started.status_code == 201, started.text
    return {"claim": claim, "run": started.json()}


# --- approvals -------------------------------------------------------------------


async def test_approvals_of_an_invisible_workspace_answer_as_missing(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    mine, theirs = [
        await tree.as_admin(
            "/api/v1/approvals",
            {"task": tree.tasks[name]["id"], "assignedPrincipalId": tree.human, "gate": True},
        )
        for name in ("dept", "other")
    ]
    loose = await tree.as_admin(
        "/api/v1/approvals",
        {"task": tree.tasks["none"]["id"], "assignedPrincipalId": tree.human},
    )
    tenant_level = await tree.as_admin(
        "/api/v1/approvals", {"assignedPrincipalId": tree.human, "comment": "no work"}
    )

    listed = ids(await tree.get("/api/v1/approvals", limit=200))
    assert listed == {mine["id"], tenant_level["id"]}
    assert ids(await tree.get("/api/v1/approvals", taskId=tree.tasks["other"]["id"])) == set()

    for hidden in (theirs["id"], loose["id"]):
        for suffix in ("", "/outcome"):
            same_as_missing(
                await tree.get(f"/api/v1/approvals/{hidden}{suffix}"),
                await tree.get(f"/api/v1/approvals/{MISSING}{suffix}"),
                (hidden, MISSING),
            )
        # Deciding, cancelling and replaying an approval of another workspace
        # is refused exactly as for one that does not exist (FR-007).
        for action in (":approve", ":reject", ":cancel", ":replay-outcome"):
            same_as_missing(
                await tree.post(f"/api/v1/approvals/{hidden}{action}"),
                await tree.post(f"/api/v1/approvals/{MISSING}{action}"),
                (hidden, MISSING),
            )
    still = await client.get(f"/api/v1/approvals/{theirs['id']}", headers=auth(tree.admin))
    assert still.json()["status"] == "pending"

    # Its own approval is decided as before.
    decided = await tree.post(f"/api/v1/approvals/{mine['id']}:approve")
    assert decided.status_code == 200, decided.text


async def test_an_approval_does_not_name_an_invisible_artifact(client: httpx.AsyncClient) -> None:
    tree = await make_tree(client)
    artifact = await tree.as_admin(
        "/api/v1/artifacts",
        {"type": "note", "name": "theirs", "task": tree.tasks["other"]["id"], "content": {}},
    )
    same_as_missing(
        await tree.post(
            "/api/v1/approvals", {"assignedPrincipalId": tree.human, "artifactId": artifact["id"]}
        ),
        await tree.post(
            "/api/v1/approvals", {"assignedPrincipalId": tree.human, "artifactId": MISSING}
        ),
        (artifact["id"], MISSING),
    )


# --- artifacts -------------------------------------------------------------------


async def test_artifacts_of_an_invisible_workspace_answer_as_missing(
    client: httpx.AsyncClient,
) -> None:
    """The T003 review case: ``authorize`` on the artifact's TASK did not see workspaces."""
    tree = await make_tree(client)

    def body(name: str, **where: str) -> dict[str, Any]:
        return {"type": "note", "name": name, "content": {"text": name}, **where}

    mine = await tree.as_admin("/api/v1/artifacts", body("mine", task=tree.tasks["dept"]["id"]))
    of_task = await tree.as_admin(
        "/api/v1/artifacts", body("their work", task=tree.tasks["other"]["id"])
    )
    of_workspace = await tree.as_admin(
        "/api/v1/artifacts", body("their space", workspaceId=tree.ws["other"])
    )
    of_loose = await tree.as_admin(
        "/api/v1/artifacts", body("loose work", task=tree.tasks["none"]["id"])
    )

    assert ids(await tree.get("/api/v1/artifacts", limit=200)) == {mine["id"]}
    # A filter by an invisible workspace is the empty page of a missing one.
    hidden_page = await tree.get("/api/v1/artifacts", workspaceId=tree.ws["other"])
    missing_page = await tree.get("/api/v1/artifacts", workspaceId=MISSING)
    assert hidden_page.status_code == missing_page.status_code == 200
    assert hidden_page.json() == missing_page.json() == {"items": [], "nextCursor": None}
    assert ids(await tree.get("/api/v1/artifacts", taskId=tree.tasks["other"]["id"])) == set()

    for hidden in (of_task["id"], of_workspace["id"], of_loose["id"]):
        for suffix in ("", "/content"):
            same_as_missing(
                await tree.get(f"/api/v1/artifacts/{hidden}{suffix}"),
                await tree.get(f"/api/v1/artifacts/{MISSING}{suffix}"),
                (hidden, MISSING),
            )
        # Asked for as an input of visible work, it is still missing.
        same_as_missing(
            await tree.get(f"/api/v1/artifacts/{hidden}", forTask=tree.tasks["dept"]["id"]),
            await tree.get(f"/api/v1/artifacts/{MISSING}", forTask=tree.tasks["dept"]["id"]),
            (hidden, MISSING),
        )
    # Nor is it superseded from a visible workspace.
    same_as_missing(
        await tree.post(
            "/api/v1/artifacts",
            {**body("v2", task=tree.tasks["dept"]["id"]), "supersedesArtifactId": of_task["id"]},
        ),
        await tree.post(
            "/api/v1/artifacts",
            {**body("v2", task=tree.tasks["dept"]["id"]), "supersedesArtifactId": MISSING},
        ),
        (of_task["id"], MISSING),
    )
    assert (await tree.get(f"/api/v1/artifacts/{mine['id']}")).status_code == 200


# --- runs, claims, child handles ---------------------------------------------------


async def test_runs_and_claims_of_invisible_work_answer_as_missing(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    _, runner_key = await create_agent_with_key(
        client, tree.admin, name="runner", permissions=RUNNER_PERMISSIONS
    )
    mine = await run_on(tree, runner_key, tree.tasks["dept"]["id"])
    theirs = await run_on(tree, runner_key, tree.tasks["other"]["id"])
    run_id, claim_id = theirs["run"]["id"], theirs["claim"]["id"]

    assert ids(await tree.get("/api/v1/runs", limit=200)) == {mine["run"]["id"]}
    assert ids(await tree.get("/api/v1/claims", limit=200)) == {mine["claim"]["id"]}
    assert ids(await tree.get("/api/v1/runs", taskId=tree.tasks["other"]["id"])) == set()

    for suffix in (
        "",
        "/context",
        "/checkpoints",
        "/actions",
        "/control-messages",
        "/child-handles",
    ):
        same_as_missing(
            await tree.get(f"/api/v1/runs/{run_id}{suffix}"),
            await tree.get(f"/api/v1/runs/{MISSING}{suffix}"),
            (run_id, MISSING),
        )
    same_as_missing(
        await tree.get(f"/api/v1/claims/{claim_id}"),
        await tree.get(f"/api/v1/claims/{MISSING}"),
        (claim_id, MISSING),
    )
    for action in (":heartbeat", ":release"):
        same_as_missing(
            await tree.post(f"/api/v1/claims/{claim_id}{action}"),
            await tree.post(f"/api/v1/claims/{MISSING}{action}"),
            (claim_id, MISSING),
        )
    for action, payload in ((":request-cancel", {"reason": "mine now"}), (":cancel", {})):
        same_as_missing(
            await tree.post(f"/api/v1/runs/{run_id}{action}", payload),
            await tree.post(f"/api/v1/runs/{MISSING}{action}", payload),
            (run_id, MISSING),
        )
    assert (await tree.get(f"/api/v1/runs/{mine['run']['id']}/context")).status_code == 200


async def test_the_context_of_an_invisible_run_is_a_missing_run(
    client: httpx.AsyncClient,
) -> None:
    """Oracle from the T003 review: "Run not found" against "Task not found"."""
    tree = await make_tree(client)
    _, runner_key = await create_agent_with_key(
        client, tree.admin, name="runner", permissions=RUNNER_PERMISSIONS
    )
    theirs = await run_on(tree, runner_key, tree.tasks["other"]["id"])
    run_id = theirs["run"]["id"]
    same_as_missing(
        await tree.post("/api/v1/context", {"runId": run_id}),
        await tree.post("/api/v1/context", {"runId": MISSING}),
        (run_id, MISSING),
    )


# --- work: relations, prerequisites, oracles ------------------------------------------


async def test_relations_and_prerequisites_do_not_name_invisible_work(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client, narrowed=False)
    mine, theirs = tree.tasks["dept"], tree.tasks["other"]
    visible_blocker = await tree.as_admin(
        "/api/v1/tasks", {"title": "first", "workspaceId": tree.ws["team"]}
    )
    for blocker in (theirs, visible_blocker):
        await tree.as_admin(
            f"/api/v1/tasks/{mine['id']}/relations",
            {"type": "depends_on", "toTask": blocker["id"]},
        )
    await tree.narrow()

    relations = await tree.get(f"/api/v1/tasks/{mine['id']}/relations")
    assert relations.status_code == 200, relations.text
    named = {r["fromTaskId"] for r in relations.json()["items"]} | {
        r["toTaskId"] for r in relations.json()["items"]
    }
    assert theirs["id"] not in named and visible_blocker["id"] in named
    hidden_relation = next(
        r["id"]
        for r in (
            await client.get(f"/api/v1/tasks/{mine['id']}/relations", headers=auth(tree.admin))
        ).json()["items"]
        if r["toTaskId"] == theirs["id"]
    )
    removed = await client.delete(
        f"/api/v1/tasks/{mine['id']}/relations/{hidden_relation}", headers=auth(tree.key)
    )
    assert removed.status_code == 404, removed.text

    claimability = await tree.get(f"/api/v1/tasks/{mine['id']}/claimability")
    assert claimability.status_code == 200, claimability.text
    [reason] = [r for r in claimability.json()["reasons"] if r["code"] == "task_not_ready"]
    assert [b["taskId"] for b in reason["blockedBy"]] == [visible_blocker["id"]]
    assert reason["hiddenBlockers"] == 1
    assert theirs["publicId"] not in claimability.text

    session = await open_session(client, tree.key)
    refused = await claim_task(client, tree.key, mine["id"], session["id"])
    assert refused.status_code == 409, refused.text
    details = refused.json()["error"]["details"]
    assert details["hiddenBlockers"] == 1
    assert theirs["id"] not in refused.text and theirs["publicId"] not in refused.text


async def test_a_listing_by_an_invisible_workspace_is_a_missing_workspace(
    client: httpx.AsyncClient,
) -> None:
    """Oracle from the T003 review: an empty page against 404 for a missing workspace."""
    tree = await make_tree(client)
    for path in ("/api/v1/tasks", "/api/v1/work/available", "/api/v1/me/attention"):
        same_as_missing(
            await tree.get(path, workspaceId=tree.ws["other"], includeDescendants="true"),
            await tree.get(path, workspaceId=MISSING, includeDescendants="true"),
            (tree.ws["other"], MISSING),
        )
    same_as_missing(
        await tree.get("/api/v1/events", workspaceId=tree.ws["other"]),
        await tree.get("/api/v1/events", workspaceId=MISSING),
        (tree.ws["other"], MISSING),
    )


async def test_the_executor_queue_and_context_hold_visible_work_only(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    queue = await tree.get("/api/v1/work/available", limit=200)
    assert queue.status_code == 200, queue.text
    offered = {item["id"] for item in queue.json()["items"]}
    assert tree.tasks["dept"]["id"] in offered
    assert not offered & {tree.tasks["other"]["id"], tree.tasks["none"]["id"]}

    await tree.as_admin(
        "/api/v1/approvals",
        {"task": tree.tasks["other"]["id"], "assignedPrincipalId": tree.human},
    )
    context = await tree.get("/api/v1/harness/context")
    assert context.status_code == 200, context.text
    assert tree.tasks["other"]["id"] not in context.text


# --- events --------------------------------------------------------------------------


async def test_the_journal_holds_events_of_visible_workspaces_and_the_tenant(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    page = await tree.get("/api/v1/events", limit=200)
    assert page.status_code == 200, page.text
    events = page.json()["items"]
    workspaces = {e["workspaceId"] for e in events}
    assert tree.ws["dept"] in workspaces
    assert not workspaces & {tree.ws["other"], tree.ws["company"]}
    # Work without a workspace is not visible, and neither are its events (B2).
    assert not [e for e in events if e["entityId"] == tree.tasks["none"]["id"]]
    # Tenant-level events are.
    assert [e for e in events if e["type"] == "principal.created"]
    # Asked by the entity, the event of another workspace is not there either.
    by_entity = await tree.get(
        "/api/v1/events", entityType="task", entityId=tree.tasks["other"]["id"]
    )
    assert by_entity.json()["items"] == []


def test_the_event_stream_does_not_deliver_another_workspace(settings: Settings) -> None:
    with TestClient(create_app(settings)) as tc:
        admin = tc.post(
            "/api/v1/bootstrap",
            json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "A"},
            headers=auth(BOOTSTRAP_TOKEN),
        ).json()["apiKey"]["key"]
        ops, sales = (
            tc.post(
                "/api/v1/workspaces", json={"slug": slug, "name": slug}, headers=auth(admin)
            ).json()["id"]
            for slug in ("ops", "sales")
        )
        human = tc.post(
            "/api/v1/principals", json={"kind": "human", "displayName": "Ann"}, headers=auth(admin)
        ).json()["id"]
        key = tc.post(
            f"/api/v1/principals/{human}/api-keys",
            json={"permissions": ["events.read"]},
            headers=auth(admin),
        ).json()["key"]
        tc.post(
            f"/api/v1/workspaces/{ops}/members", json={"principalId": human}, headers=auth(admin)
        )
        bound = tc.post(
            f"/api/v1/principals/{human}/iam-bindings",
            json={**identity(), "permissions": ["events.read"], "visibility": "members"},
            headers=auth(admin),
        )
        assert bound.status_code == 201, bound.text
        with tc.websocket_connect("/api/v1/events/ws?types=task.", headers=auth(key)) as ws:
            for title, workspace in (("sales", sales), ("loose", None), ("ops", ops)):
                created = tc.post(
                    "/api/v1/tasks",
                    json={"title": title, "workspaceId": workspace},
                    headers=auth(admin),
                )
                assert created.status_code == 201, created.text
            first = ws.receive_json()
            assert (first["type"], first["payload"]["title"]) == ("task.created", "ops")
            assert first["workspaceId"] == ops


# --- attention ------------------------------------------------------------------------


async def test_waiting_for_you_holds_no_item_of_another_workspace(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    mine, theirs = [
        await tree.as_admin(
            "/api/v1/approvals",
            {"task": tree.tasks[name]["id"], "assignedPrincipalId": tree.human, "gate": True},
        )
        for name in ("dept", "other")
    ]
    attention = await tree.get("/api/v1/me/attention")
    assert attention.status_code == 200, attention.text
    entities = {item["entity"]["id"] for item in attention.json()["items"]}
    assert mine["id"] in entities and theirs["id"] not in entities
    same_as_missing(
        await tree.post(
            f"/api/v1/me/attention/approval.review:{theirs['id']}:feedback", {"verdict": "useful"}
        ),
        await tree.post(
            f"/api/v1/me/attention/approval.review:{MISSING}:feedback", {"verdict": "useful"}
        ),
        (theirs["id"], MISSING),
    )


# --- workspaces, members, projects, external references ------------------------------


async def test_the_tree_and_the_workspaces_name_no_invisible_one(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    listed = await tree.get("/api/v1/workspaces", limit=200)
    assert ids(listed) == {tree.ws["dept"], tree.ws["team"]}
    by_id = {w["id"]: w for w in listed.json()["items"]}
    # The parent of a visible root is not named (§3.9).
    assert by_id[tree.ws["dept"]]["parentId"] is None
    assert by_id[tree.ws["team"]]["parentId"] == tree.ws["dept"]
    assert ids(await tree.get("/api/v1/workspaces", rootsOnly="true")) == {tree.ws["dept"]}

    shown = await tree.get("/api/v1/workspaces/tree")
    assert shown.status_code == 200, shown.text
    [root] = shown.json()["roots"]
    assert (root["id"], root["parentId"]) == (tree.ws["dept"], None)
    assert [child["id"] for child in root["children"]] == [tree.ws["team"]]

    detail = await tree.get(f"/api/v1/workspaces/{tree.ws['dept']}")
    assert detail.status_code == 200 and detail.json()["parentId"] is None
    for suffix in ("", "/members", "/participants"):
        same_as_missing(
            await tree.get(f"/api/v1/workspaces/{tree.ws['other']}{suffix}"),
            await tree.get(f"/api/v1/workspaces/{MISSING}{suffix}"),
            (tree.ws["other"], MISSING),
        )
    same_as_missing(
        await tree.get("/api/v1/workspaces/tree", rootId=tree.ws["company"]),
        await tree.get("/api/v1/workspaces/tree", rootId=MISSING),
        (tree.ws["company"], MISSING),
    )
    # Nothing is filed into it either.
    same_as_missing(
        await tree.post("/api/v1/tasks", {"title": "x", "workspaceId": tree.ws["other"]}),
        await tree.post("/api/v1/tasks", {"title": "x", "workspaceId": MISSING}),
        (tree.ws["other"], MISSING),
    )


async def test_projects_and_their_references_of_an_invisible_workspace(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client, narrowed=False)
    await create_template(client, tree.admin)
    projects = {
        name: await create_project(
            client, tree.admin, workspaceId=tree.ws[name], templateKey="delivery"
        )
        for name in ("dept", "other")
    }
    reference = {"externalSystem": "jira", "externalType": "epic", "externalId": "OPS-1"}
    await tree.as_admin(
        "/api/v1/external-references",
        {"entityType": "project", "entityId": projects["other"]["id"], **reference},
    )
    await tree.narrow()

    assert ids(await tree.get("/api/v1/projects", limit=200)) == {projects["dept"]["id"]}
    hidden = projects["other"]["id"]
    for suffix in ("", "/effective-config", "/config-revisions", "/external-references"):
        same_as_missing(
            await tree.get(f"/api/v1/projects/{hidden}{suffix}"),
            await tree.get(f"/api/v1/projects/{MISSING}{suffix}"),
            (hidden, MISSING),
        )
    found = await tree.get("/api/v1/external-references", externalSystem="jira", externalId="OPS-1")
    unknown = await tree.get(
        "/api/v1/external-references", externalSystem="jira", externalId="OPS-404"
    )
    assert found.status_code == unknown.status_code == 200
    assert found.json()["items"] == unknown.json()["items"] == []


# --- processes ---------------------------------------------------------------------


async def test_process_instance_actions_of_an_invisible_workspace_answer_as_missing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """``:suspend``, ``:resume``, ``:cancel`` and the journal, as the read (T003 review)."""
    setup = await _setup(client)
    tree = await make_tree(client, admin=setup["key"])
    await _publish(client, tree.admin, "sample-goal", GOAL)
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-1", "data": {"open": 2}},
        headers=auth(tree.admin),
    )
    assert started.status_code == 201, started.text
    instance = started.json()["id"]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE process_instances SET workspace_id = :ws WHERE id = :id"),
            {"ws": tree.ws["other"], "id": instance},
        )
    for suffix in ("", "/journal"):
        same_as_missing(
            await tree.get(f"/api/v1/process-instances/{instance}{suffix}"),
            await tree.get(f"/api/v1/process-instances/{MISSING}{suffix}"),
            (instance, MISSING),
        )
    for action in (":suspend", ":resume", ":cancel"):
        same_as_missing(
            await tree.post(f"/api/v1/process-instances/{instance}{action}", {"reason": "x"}),
            await tree.post(f"/api/v1/process-instances/{MISSING}{action}", {"reason": "x"}),
            (instance, MISSING),
        )
    assert instance not in ids(await tree.get("/api/v1/process-instances", limit=200))
    with sync_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM process_instances WHERE id = :id"), {"id": instance}
        ).scalar_one()
    assert status not in ("suspended", "cancelled")


async def test_a_gate_in_an_invisible_ancestor_holds_the_work_without_being_named(
    client: httpx.AsyncClient,
) -> None:
    """The approval's own workspace may be an ancestor of its work's (ADR-0068)."""
    tree = await make_tree(client)
    mine = tree.tasks["dept"]
    gate = await tree.as_admin(
        "/api/v1/approvals",
        {
            "task": mine["id"],
            "workspaceId": tree.ws["company"],
            "assignedPrincipalId": tree.human,
            "gate": True,
        },
    )
    assert ids(await tree.get("/api/v1/approvals", taskId=mine["id"])) == set()
    same_as_missing(
        await tree.get(f"/api/v1/approvals/{gate['id']}"),
        await tree.get(f"/api/v1/approvals/{MISSING}"),
        (gate["id"], MISSING),
    )

    claimability = await tree.get(f"/api/v1/tasks/{mine['id']}/claimability")
    assert claimability.status_code == 200, claimability.text
    [reason] = [r for r in claimability.json()["reasons"] if r["code"] == "approval_required"]
    assert reason == {"code": "approval_required", "pendingApprovals": [], "hiddenApprovals": 1}

    session = await open_session(client, tree.key)
    refused = await claim_task(client, tree.key, mine["id"], session["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "approval_required"
    assert refused.json()["error"]["details"]["hiddenApprovals"] == 1
    assert gate["id"] not in refused.text
