"""Observations and evidence pass the seam (CP-ADR-0082 V6-V7, T004 review).

An observation is a journal event without a workspace of its own: what it
is about is in its payload (``workspaceId``, ``taskId``, ``runId``). A person
in ``members`` mode does not read an observation of another workspace in the
journal, cannot attach one to invisible work, cannot supersede one, and
cannot name one — or an artifact or a context pack of invisible work — as
evidence: each answers as a missing id.
"""

import json
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.testclient import TestClient

from control_plane.config import Settings
from control_plane.main import create_app
from tests.helpers import BOOTSTRAP_TOKEN, auth, create_agent_with_key
from tests.integration.test_workspace_visibility import same_as_missing
from tests.integration.test_workspace_visibility_routes import (
    HUMAN_PERMISSIONS,
    MISSING,
    RUNNER_PERMISSIONS,
    Tree,
    identity,
    make_tree,
    run_on,
)

WRITER_PERMISSIONS = [*HUMAN_PERMISSIONS, "observations.write", "goals.write", "goals.read"]


async def _writer(tree: Tree) -> str:
    """Bob: like Ann a member of ``dept`` in ``members`` mode, who may also
    record observations and write goals."""
    bob, key = await create_agent_with_key(
        tree.client, tree.admin, name="bob", permissions=WRITER_PERMISSIONS, kind="human"
    )
    await tree.add_member("dept", bob["id"])
    await tree.narrow(bob["id"])
    return key


async def _observe(tree: Tree, key: str, **scope: Any) -> httpx.Response:
    body = {"kind": "finding", "content": f"seen {sorted(scope)}", **scope}
    return await tree.client.post("/api/v1/observations", json=body, headers=auth(key))


async def _recorded(tree: Tree, **scope: Any) -> str:
    response = await _observe(tree, tree.admin, **scope)
    assert response.status_code == 201, response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _runs(tree: Tree) -> tuple[dict[str, Any], dict[str, Any]]:
    _, runner_key = await create_agent_with_key(
        tree.client, tree.admin, name="runner", permissions=RUNNER_PERMISSIONS
    )
    return (
        await run_on(tree, runner_key, tree.tasks["dept"]["id"]),
        await run_on(tree, runner_key, tree.tasks["other"]["id"]),
    )


# --- the journal ---------------------------------------------------------------------


async def test_the_journal_holds_no_observation_of_invisible_work(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    mine, theirs = await _runs(tree)
    other_task, loose_task = tree.tasks["other"]["id"], tree.tasks["none"]["id"]
    hidden = {
        await _recorded(tree, workspaceId=tree.ws["other"]),
        await _recorded(tree, task=other_task),
        await _recorded(tree, runId=theirs["run"]["id"]),
        await _recorded(tree, task=loose_task),
        # Visible work, but a run of invisible work: the run is named too.
        await _recorded(tree, task=tree.tasks["dept"]["id"], runId=theirs["run"]["id"]),
        # A visible workspace, but invisible work.
        await _recorded(tree, workspaceId=tree.ws["dept"], task=other_task),
    }
    shown = {
        await _recorded(tree),
        await _recorded(tree, workspaceId=tree.ws["team"]),
        await _recorded(tree, task=tree.tasks["dept"]["id"]),
        await _recorded(tree, runId=mine["run"]["id"]),
    }

    page = await tree.get("/api/v1/events", limit=200)
    assert page.status_code == 200, page.text
    events = page.json()["items"]
    observed = {e["entityId"] for e in events if e["type"] == "observation.recorded"}
    assert observed == shown
    # Nothing in any payload names the invisible work, its run or its workspace.
    secrets = {other_task, loose_task, theirs["run"]["id"], tree.ws["other"], *hidden}
    for event in events:
        dumped = json.dumps(event)
        assert not [s for s in secrets if s in dumped], event
    # Asked for by the entity, an invisible observation is not there either.
    for observation in hidden:
        by_entity = await tree.get("/api/v1/events", entityType="observation", entityId=observation)
        assert by_entity.json()["items"] == []
    # The administrator in tenant mode reads them all, as before.
    everything = await tree.get("/api/v1/events", key=tree.admin, limit=200)
    every = {e["entityId"] for e in everything.json()["items"]}
    assert hidden | shown <= every


async def test_attention_feedback_does_not_name_invisible_work(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Attention feedback carries ``entityType``/``entityId`` and no workspace."""
    tree = await make_tree(client)
    with sync_engine.begin() as db:
        tenant = db.execute(
            text("SELECT tenant_id FROM workspaces WHERE id = :w"), {"w": tree.ws["other"]}
        ).scalar_one()
        for entity in (tree.tasks["other"]["id"], tree.tasks["dept"]["id"]):
            db.execute(
                text(
                    "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
                    " correlation_id, request_id, payload, occurred_at)"
                    " VALUES (:id, :t, 'attention.feedback_recorded', 'attention_feedback',"
                    " :id, 'c', 'r', CAST(:p AS jsonb), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": tenant,
                    "p": json.dumps(
                        {
                            "principalId": tree.human,
                            "itemKey": f"assigned:{entity}",
                            "rule": "assigned@1",
                            "ruleKey": "assigned",
                            "ruleVersion": 1,
                            "kind": "task",
                            "reasonCode": "assigned",
                            "entityType": "task",
                            "entityId": entity,
                            "score": 1,
                            "verdict": "useful",
                            "created": True,
                            "hasComment": False,
                        }
                    ),
                },
            )
    page = await tree.get("/api/v1/events", types="attention.", limit=50)
    named = {e["payload"]["entityId"] for e in page.json()["items"]}
    assert named == {tree.tasks["dept"]["id"]}


def test_the_event_stream_does_not_deliver_an_observation_of_another_workspace(
    settings: Settings,
) -> None:
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
        sales_task = tc.post(
            "/api/v1/tasks", json={"title": "s", "workspaceId": sales}, headers=auth(admin)
        ).json()["id"]
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
        with tc.websocket_connect("/api/v1/events/ws?types=observation.", headers=auth(key)) as ws:
            for content, scope in (
                ("sales", {"workspaceId": sales}),
                ("sales work", {"task": sales_task}),
                ("ops", {"workspaceId": ops}),
            ):
                recorded = tc.post(
                    "/api/v1/observations",
                    json={"kind": "finding", "content": content, **scope},
                    headers=auth(admin),
                )
                assert recorded.status_code == 201, recorded.text
            first = ws.receive_json()
            assert first["payload"]["content"] == "ops"


# --- recording -----------------------------------------------------------------------


async def test_an_observation_is_not_attached_to_an_invisible_run(
    client: httpx.AsyncClient,
) -> None:
    tree = await make_tree(client)
    key = await _writer(tree)
    mine, theirs = await _runs(tree)
    run_id = theirs["run"]["id"]
    same_as_missing(
        await _observe(tree, key, runId=run_id),
        await _observe(tree, key, runId=MISSING),
        (run_id, MISSING),
    )
    assert (await _observe(tree, key, runId=mine["run"]["id"])).status_code == 201
    # Nothing was attached to the invisible run.
    journal = await tree.get("/api/v1/events", key=tree.admin, limit=200)
    attached = [
        e
        for e in journal.json()["items"]
        if e["type"] == "observation.recorded" and e["payload"].get("runId") == run_id
    ]
    assert attached == []


async def test_an_invisible_observation_is_not_superseded(client: httpx.AsyncClient) -> None:
    tree = await make_tree(client)
    key = await _writer(tree)
    theirs = await _recorded(tree, workspaceId=tree.ws["other"])
    loose = await _recorded(tree, task=tree.tasks["none"]["id"])
    mine = await _recorded(tree, workspaceId=tree.ws["dept"])
    for hidden in (theirs, loose):
        same_as_missing(
            await _observe(tree, key, supersedes=hidden),
            await _observe(tree, key, supersedes=MISSING),
            (hidden, MISSING),
        )
    assert (await _observe(tree, key, supersedes=mine)).status_code == 201


# --- evidence ------------------------------------------------------------------------


async def _hidden_pack(tree: Tree, sync_engine: Engine, claim: dict[str, Any]) -> str:
    """A context pack of the claim on invisible work, as the compiler records it."""
    pack_id = str(uuid.uuid4())
    with sync_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO task_context_packs (id, tenant_id, task_id, claim_id, task_type_id,"
                " compiled_by, as_of, as_of_mode, namespaces, request, candidates, used,"
                " unresolved, created_at)"
                " SELECT :id, t.tenant_id, t.id, :claim, t.type_id, c.holder_id, now(),"
                " 'now', '[]', '{}', '[]', '{}', '[]', now()"
                " FROM tasks t JOIN task_claims c ON c.id = :claim WHERE t.id = c.task_id"
            ),
            {"id": pack_id, "claim": claim["id"]},
        )
    return pack_id


async def test_evidence_does_not_name_an_invisible_fact(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    tree = await make_tree(client)
    key = await _writer(tree)
    mine, theirs = await _runs(tree)

    def artifact(**where: str) -> dict[str, Any]:
        return {"type": "note", "name": "n", "content": {"text": "n"}, **where}

    hidden_artifacts = [
        (await tree.as_admin("/api/v1/artifacts", artifact(task=tree.tasks["other"]["id"])))["id"],
        (await tree.as_admin("/api/v1/artifacts", artifact(workspaceId=tree.ws["other"])))["id"],
    ]
    own_artifact = (
        await tree.as_admin("/api/v1/artifacts", artifact(task=tree.tasks["dept"]["id"]))
    )["id"]
    hidden_observation = await _recorded(tree, workspaceId=tree.ws["other"])
    own_observation = await _recorded(tree, workspaceId=tree.ws["dept"])
    hidden_pack = await _hidden_pack(tree, sync_engine, theirs["claim"])
    own_pack = await _hidden_pack(tree, sync_engine, mine["claim"])

    cases = [
        *((("artifact", "artifactId", a), own_artifact) for a in hidden_artifacts),
        (("observation", "observationId", hidden_observation), own_observation),
        (("context_pack", "contextPackId", hidden_pack), own_pack),
    ]
    # Unclaimed visible work to patch: the claimed one would answer task_claimed.
    task = await tree.as_admin(
        "/api/v1/tasks", {"title": "patched", "workspaceId": tree.ws["dept"]}
    )
    for (kind, field, hidden), own in cases:

        def item(ref: str, kind: str = kind, field: str = field) -> list[dict[str, str]]:
            return [{"kind": kind, field: ref}]

        async def attempts(ref: str) -> list[httpx.Response]:
            current = await tree.get(f"/api/v1/tasks/{task['id']}", key=key)
            return [
                await client.post(
                    "/api/v1/tasks",
                    json={"title": "t", "workspaceId": tree.ws["dept"], "evidence": item(ref)},
                    headers=auth(key),
                ),
                await client.post(
                    "/api/v1/tasks",
                    json={
                        "title": "t",
                        "workspaceId": tree.ws["dept"],
                        "origin": {"kind": "human", "evidence": item(ref)},
                    },
                    headers=auth(key),
                ),
                await client.patch(
                    f"/api/v1/tasks/{task['id']}",
                    json={"evidence": item(ref)},
                    headers={**auth(key), "If-Match": f'"task-{current.json()["version"]}"'},
                ),
                await client.post(
                    "/api/v1/goals",
                    json={"title": "g", "createdFrom": {"kind": "human", "evidence": item(ref)}},
                    headers=auth(key),
                ),
            ]

        for invisible, missing in zip(await attempts(hidden), await attempts(MISSING), strict=True):
            same_as_missing(invisible, missing, (hidden, MISSING))
        for allowed in await attempts(own):
            assert allowed.status_code in (200, 201), (kind, allowed.text)
