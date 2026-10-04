"""The agent registry at work (CP-ADR-0073, declarative-agents D005).

Revisions appear only when the canonical hash changes, the desired state moves
without one, the rights described in a spec never exceed those of whoever
applies it, the core derives the agent's principal and binding itself, a run
names the revision of its own agent, and every change leaves an ``agent.*``
event.
"""

import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands.agents import spec_hash_of, split_desired_state
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_capability,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"
ISSUER = "https://iam.example.test"


def _fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return document


def coder_spec(workspace: str, **overrides: Any) -> dict[str, Any]:
    """The Claude Code example with its placeholders filled for this tenant."""
    spec = copy.deepcopy(_fixture("coder.yaml")["spec"])
    spec["work"]["workspace"] = workspace
    spec["work"]["taskTypes"] = ["task"]
    spec["workingCopy"]["review"]["reviewer"] = "reviewer"
    spec["skills"]["httpOrigins"] = ["https://cp.example.test"]
    spec.update(overrides)
    return spec


def catalog_spec(workspace: str) -> dict[str, Any]:
    """The universal coder example (U001): a catalog of repositories as its working copy."""
    spec = copy.deepcopy(_fixture("universal-coder.yaml")["spec"])
    spec["work"]["workspace"] = workspace
    spec["work"]["taskTypes"] = ["task"]
    return spec


async def _publish(
    client: httpx.AsyncClient, key: str, spec: dict[str, Any], agent: str = "coder"
) -> httpx.Response:
    return await client.post("/api/v1/agents", json={"key": agent, "spec": spec}, headers=auth(key))


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/events", params={"types": event_type}, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _tenant(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """An admin key and a workspace with the ``coder`` role the example names."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    await create_role(client, admin_key, "coder")
    return admin_key, workspace


async def _link(
    client: httpx.AsyncClient, key: str, agent: str = "coder", **identity: Any
) -> httpx.Response:
    body = {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
        **identity,
    }
    return await client.put(f"/api/v1/agents/{agent}/identity", json=body, headers=auth(key))


# --- revisions ----------------------------------------------------------------


async def test_an_agent_with_a_catalog_of_repositories_is_published(
    client: httpx.AsyncClient,
) -> None:
    """U002: the working copy is data; the revision keeps it as sent and hashes it."""
    admin_key, workspace = await _tenant(client)
    spec = catalog_spec(workspace["id"])

    published = await _publish(client, admin_key, spec)
    assert published.status_code == 201, published.text
    revision = published.json()["revision"]
    assert revision["spec"]["workingCopy"] == spec["workingCopy"]

    again = await _publish(client, admin_key, spec)
    assert again.status_code == 200, again.text
    assert again.json()["currentRevision"] == 1

    moved = copy.deepcopy(spec)
    moved["workingCopy"]["repositories"]["fleet"]["baseRef"] = "develop"
    changed = await _publish(client, admin_key, moved)
    assert changed.status_code == 201, changed.text
    assert changed.json()["currentRevision"] == 2
    assert changed.json()["revision"]["specHash"] != revision["specHash"]


async def test_a_revision_appears_only_when_the_hash_changes(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])

    first = await _publish(client, admin_key, spec)
    assert first.status_code == 201, first.text
    agent = first.json()
    assert agent["currentRevision"] == 1
    assert agent["state"] == "running"
    assert agent["replicas"] == 1
    assert agent["workspaceId"] == workspace["id"]
    assert agent["revision"]["specHash"].startswith("sha256:")
    # The desired state is not part of the revision (§2).
    assert "state" not in agent["revision"]["spec"]
    assert "replicas" not in agent["revision"]["spec"]["placement"]

    # The same file again: no revision, no event.
    again = await _publish(client, admin_key, spec)
    assert again.status_code == 200, again.text
    assert again.json()["currentRevision"] == 1

    # Only the desired state differs: still no revision.
    stopped = {**spec, "state": "stopped"}
    stopped["placement"] = {**spec["placement"], "replicas": 3}
    moved = await _publish(client, admin_key, stopped)
    assert moved.status_code == 200, moved.text
    assert (moved.json()["currentRevision"], moved.json()["state"], moved.json()["replicas"]) == (
        1,
        "stopped",
        3,
    )

    changed = coder_spec(workspace["id"], displayName="Autonomous coder v2")
    second = await _publish(client, admin_key, changed)
    assert second.status_code == 201, second.text
    assert second.json()["currentRevision"] == 2
    # A package is the source of truth: its state overrides the manual one (§3).
    assert (second.json()["state"], second.json()["replicas"]) == ("running", 1)

    # Rolling back is a new revision with the hash of the old one.
    rolled_back = await _publish(client, admin_key, spec)
    assert rolled_back.status_code == 201
    assert rolled_back.json()["currentRevision"] == 3
    assert rolled_back.json()["revision"]["specHash"] == agent["revision"]["specHash"]

    published = await _events(client, admin_key, "agent.revision_published")
    assert [e["payload"]["revision"] for e in published] == [1, 2, 3]
    first_event = published[0]
    assert first_event["entityType"] == "agent"
    assert first_event["entityId"] == agent["id"]
    assert first_event["workspaceId"] == workspace["id"]
    assert first_event["actorId"] == agent["revision"]["createdBy"]
    assert first_event["payload"] == {
        "key": "coder",
        "revision": 1,
        "specHash": agent["revision"]["specHash"],
        "previousRevision": None,
        "executorKind": "claude-code",
        "placed": True,
        "permissionsChanged": True,
    }
    assert published[1]["payload"]["previousRevision"] == 1
    assert published[1]["payload"]["permissionsChanged"] is False


async def test_revisions_are_addressable_and_immutable(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    await _publish(client, admin_key, spec)
    await _publish(client, admin_key, coder_spec(workspace["id"], displayName="Second"))

    current = (await client.get("/api/v1/agents/coder", headers=auth(admin_key))).json()
    assert current["revision"]["revision"] == 2
    first = await client.get("/api/v1/agents/coder@1", headers=auth(admin_key))
    assert first.status_code == 200
    assert first.json()["revision"]["spec"]["displayName"] == spec["displayName"]
    for missing in ("coder@3", "coder@x", "nobody"):
        response = await client.get(f"/api/v1/agents/{missing}", headers=auth(admin_key))
        assert response.status_code == 404, missing

    # The export of FR-012: the revision plus the desired state is the spec applied.
    exported = copy.deepcopy(first.json()["revision"]["spec"])
    exported["state"] = current["state"]
    exported["placement"]["replicas"] = current["replicas"]
    assert exported == spec

    with sync_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(text("UPDATE agent_revisions SET spec_hash = 'x'"))

    listed = (await client.get("/api/v1/agents", headers=auth(admin_key))).json()
    assert [a["key"] for a in listed["items"]] == ["coder"]
    assert listed["items"][0]["revision"]["revision"] == 2


async def test_validate_answers_like_publish_and_writes_nothing(
    client: httpx.AsyncClient,
) -> None:
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    body = {"key": "coder", "spec": spec}

    dry = await client.post("/api/v1/agents:validate", json=body, headers=auth(admin_key))
    assert dry.status_code == 200, dry.text
    assert dry.json() == {
        "key": "coder",
        "specHash": dry.json()["specHash"],
        "currentRevision": None,
        "wouldCreateRevision": True,
        "wouldChangeState": True,
    }
    assert (await client.get("/api/v1/agents/coder", headers=auth(admin_key))).status_code == 404

    published = (await _publish(client, admin_key, spec)).json()
    assert published["revision"]["specHash"] == dry.json()["specHash"]
    again = await client.post("/api/v1/agents:validate", json=body, headers=auth(admin_key))
    assert again.json()["currentRevision"] == 1
    assert again.json()["wouldCreateRevision"] is False
    assert again.json()["wouldChangeState"] is False

    bad = {"key": "coder", "spec": {**spec, "work": {**spec["work"], "taskTypes": ["nope"]}}}
    for path in ("/api/v1/agents:validate", "/api/v1/agents"):
        response = await client.post(path, json=bad, headers=auth(admin_key))
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "unknown_reference"
        assert response.json()["error"]["details"]["path"] == "spec.work.taskTypes[0]"


async def test_the_executor_image_is_data_of_the_revision(client: httpx.AsyncClient) -> None:
    """CP-ADR-0073 Z1-Z3: ``executor.image`` is stored, hashed and handed out with the spec."""
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    assert "image" not in spec["executor"]

    # Without the field the revision is what it was before the amendment.
    first = (await _publish(client, admin_key, spec)).json()
    assert "image" not in first["revision"]["spec"]["executor"]
    assert first["revision"]["specHash"] == spec_hash_of(split_desired_state(spec)[0])[1]

    image = "ghcr.io/org/coder:1.2@sha256:" + "0123456789abcdef" * 4
    imaged = copy.deepcopy(spec)
    imaged["executor"]["image"] = image
    second = await _publish(client, admin_key, imaged)
    assert second.status_code == 201, second.text
    assert second.json()["currentRevision"] == 2
    assert second.json()["revision"]["spec"]["executor"]["image"] == image
    # The same file again: no revision.
    assert (await _publish(client, admin_key, imaged)).status_code == 200

    # What fleet-controller reads: the list and a revision by its address.
    listed = (await client.get("/api/v1/agents", headers=auth(admin_key))).json()["items"]
    assert [a["revision"]["spec"]["executor"].get("image") for a in listed] == [image]
    old = (await client.get("/api/v1/agents/coder@1", headers=auth(admin_key))).json()
    assert "image" not in old["revision"]["spec"]["executor"]

    history = await client.get("/api/v1/agents/coder/revisions", headers=auth(admin_key))
    assert history.json()["items"][0]["changedFields"] == ["executor.image"]
    published = await _events(client, admin_key, "agent.revision_published")
    assert published[-1]["payload"]["executorKind"] == "claude-code"
    assert "image" not in published[-1]["payload"]

    # What the executor reads.
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    agent_key = await _api_key(client, admin_key, principal_id)
    me = await client.get("/api/v1/agents/me", headers=auth(agent_key))
    assert me.status_code == 200, me.text
    assert me.json()["revision"]["spec"]["executor"]["image"] == image

    # Dropping the image rolls the spec back to the hash of the first revision.
    rolled_back = (await _publish(client, admin_key, spec)).json()
    assert rolled_back["currentRevision"] == 3
    assert rolled_back["revision"]["specHash"] == first["revision"]["specHash"]

    bad = copy.deepcopy(spec)
    bad["executor"]["image"] = "ghcr.io/org/coder"  # neither tag nor digest
    for path in ("/api/v1/agents:validate", "/api/v1/agents"):
        response = await client.post(
            path, json={"key": "coder", "spec": bad}, headers=auth(admin_key)
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        locations = [e["loc"] for e in response.json()["error"]["details"]["errors"]]
        assert locations == ["body.spec.executor.image"]
    assert (await client.get("/api/v1/agents/coder", headers=auth(admin_key))).json()[
        "currentRevision"
    ] == 3


async def test_fractional_cpus_are_refused_by_the_shape_with_the_path_of_the_field(
    client: httpx.AsyncClient,
) -> None:
    """Amendment 2026-10-03 of CP-ADR-0073: not ``non_canonical_value`` of the whole spec."""
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    for cpus in (0.5, 1.5):
        bad = {**spec, "placement": {**spec["placement"], "resources": {"cpus": cpus}}}
        for path in ("/api/v1/agents:validate", "/api/v1/agents"):
            response = await client.post(
                path, json={"key": "coder", "spec": bad}, headers=auth(admin_key)
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "invalid_request"
            [error] = response.json()["error"]["details"]["errors"]
            assert error["loc"] == "body.spec.placement.resources.cpus"
            assert "whole number" in error["message"]
    assert (await client.get("/api/v1/agents/coder", headers=auth(admin_key))).status_code == 404

    # A whole float is the integer it names: the same revision, published once.
    whole = {**spec, "placement": {**spec["placement"], "resources": {"cpus": 2}}}
    first = await _publish(client, admin_key, whole)
    assert first.status_code == 201, first.text
    as_float = {**spec, "placement": {**spec["placement"], "resources": {"cpus": 2.0}}}
    again = await _publish(client, admin_key, as_float)
    assert again.status_code == 200, again.text
    assert again.json()["currentRevision"] == 1
    stored = again.json()["revision"]["spec"]["placement"]["resources"]["cpus"]
    assert stored == 2 and type(stored) is int


async def test_the_scope_ceiling_takes_dotted_iam_scopes(client: httpx.AsyncClient) -> None:
    """The connector's ``iam:identities.link`` (iam-service ADR-0004) is published."""
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    iam = {"audiences": ["iam"], "scopeCeiling": ["iam:people", "iam:identities.link"]}
    spec["identity"] = {**spec["identity"], "iam": iam}

    published = await _publish(client, admin_key, spec)
    assert published.status_code == 201, published.text
    assert published.json()["revision"]["spec"]["identity"]["iam"] == iam

    for scope in ("iam:identities.", "iam:identities..link", "iam.identities.link"):
        bad = {**spec, "identity": {**spec["identity"], "iam": {**iam, "scopeCeiling": [scope]}}}
        response = await _publish(client, admin_key, bad)
        assert response.status_code == 400, response.text
    listed = await client.get("/api/v1/agents/coder/revisions", headers=auth(admin_key))
    assert [item["revision"] for item in listed.json()["items"]] == [1]


async def test_references_and_secrets_are_checked(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])

    by_slug = await _publish(client, admin_key, coder_spec("engineering"))
    assert by_slug.status_code == 201, by_slug.text
    assert by_slug.json()["workspaceId"] == workspace["id"]

    cases: list[tuple[dict[str, Any], int, str]] = [
        ({**spec, "work": {**spec["work"], "workspace": "nowhere"}}, 422, "unknown_reference"),
        ({**spec, "identity": {**spec["identity"], "roles": ["ghost"]}}, 422, "unknown_reference"),
        (
            {**spec, "executor": {**spec["executor"], "params": {"apiKey": "sk-live"}}},
            422,
            "secret_material_rejected",
        ),
        # ``placement.resources`` is closed: a stray key fails the shape first.
        (
            {**spec, "placement": {**spec["placement"], "resources": {"token": "x"}}},
            400,
            "invalid_request",
        ),
        (
            {
                **spec,
                "workingCopy": {
                    **spec["workingCopy"],
                    "neighbours": {"deploy-token": "ghp-live"},
                },
            },
            422,
            "secret_material_rejected",
        ),
        (
            {**spec, "executor": {**spec["executor"], "params": {"temperature": 0.5}}},
            422,
            "non_canonical_value",
        ),
        (
            {**spec, "identity": {**spec["identity"], "permissions": ["approvals.decide"]}},
            422,
            "permissions_not_allowed_for_kind",
        ),
        (
            {**spec, "identity": {**spec["identity"], "permissions": ["no.such"]}},
            422,
            "invalid_permissions",
        ),
    ]
    for bad, status, code in cases:
        response = await _publish(client, admin_key, bad, agent="other")
        assert response.status_code == status, (code, response.text)
        assert response.json()["error"]["code"] == code
    leaked = await _publish(client, admin_key, cases[4][0], agent="other")
    assert leaked.json()["error"]["details"] == {
        "field": "spec.workingCopy",
        "path": "neighbours.deploy-token",
    }

    # The catalog of repositories is searched the same way (TAI-ADR-0063, U002).
    catalog = catalog_spec(workspace["id"])
    catalog["workingCopy"]["repositories"]["fleet"]["accessToken"] = "ghp-live"
    leaked = await _publish(client, admin_key, catalog, agent="catalog")
    assert leaked.status_code == 422, leaked.text
    assert leaked.json()["error"]["code"] == "secret_material_rejected"
    assert leaked.json()["error"]["details"] == {
        "field": "spec.workingCopy",
        "path": "repositories.fleet.accessToken",
    }
    assert "ghp-live" not in leaked.text
    # An object, but not of the shape of any kind: the shape is the schema's, not the core's.
    odd = {**spec, "workingCopy": {"somethingElse": {"deep": [1, "two"]}}}
    assert (await _publish(client, admin_key, odd, agent="odd")).status_code == 201
    for not_an_object in ("https://git.example/org/control-plane.git", [], 1):
        response = await _publish(
            client, admin_key, {**spec, "workingCopy": not_an_object}, agent="odd"
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"

    # Node secret names are the one allowed reference to a secret.
    named = {**spec, "placement": {**spec["placement"], "secrets": ["github-token"]}}
    assert (await _publish(client, admin_key, named, agent="named")).status_code == 201

    # Long executor conventions fit (§2); the general 2000-character bound does not apply.
    long = {**spec, "executor": {**spec["executor"], "instructions": "x" * 60_000}}
    assert (await _publish(client, admin_key, long, agent="long")).status_code == 201


async def test_a_spec_without_placement_is_placed_with_the_defaults(
    client: httpx.AsyncClient,
) -> None:
    """No ``placement`` is a placed agent with one replica; only ``none`` is unplaced (§1)."""
    admin_key, workspace = await _tenant(client)
    spec = coder_spec(workspace["id"])
    del spec["placement"]
    # A placed agent needs no work section either: it is optional (§1).
    del spec["work"]

    published = await _publish(client, admin_key, spec)
    assert published.status_code == 201, published.text
    agent = published.json()
    assert (agent["state"], agent["replicas"], agent["workspaceId"]) == ("running", 1, None)
    assert "placement" not in agent["revision"]["spec"]
    published_events = await _events(client, admin_key, "agent.revision_published")
    assert [event["payload"]["placed"] for event in published_events] == [True]

    # Round trip: the stored spec, sent back, is the same revision.
    again = await _publish(client, admin_key, agent["revision"]["spec"])
    assert again.status_code == 200, again.text
    assert again.json()["currentRevision"] == 1

    # A placed agent still needs its executor, placement or not.
    del spec["executor"]
    refused = await _publish(client, admin_key, spec, agent="headless")
    assert refused.status_code == 400, refused.text

    bridge = _fixture("process-bridge.yaml")["spec"]
    unplaced = await _publish(client, admin_key, bridge, "bridge")
    assert unplaced.status_code == 201, unplaced.text
    assert unplaced.json()["replicas"] == 0
    # identity.iam is data for whoever issues the account: stored and exported as sent.
    exported = (await client.get("/api/v1/agents/bridge", headers=auth(admin_key))).json()
    assert exported["revision"]["spec"]["identity"] == bridge["identity"]


# --- rights (FR-010) ----------------------------------------------------------


async def test_a_spec_cannot_describe_more_rights_than_its_applier_holds(
    client: httpx.AsyncClient,
) -> None:
    admin_key, workspace = await _tenant(client)
    _, narrow_key = await create_agent_with_key(
        client,
        admin_key,
        name="packager",
        kind="service",
        permissions=["agents.manage", "agents.read", "tasks.read", "tasks.write"],
    )
    spec = coder_spec(workspace["id"])
    spec["identity"] = {"kind": "agent", "permissions": ["tasks.read", "tasks.write"]}

    allowed = await _publish(client, narrow_key, spec)
    assert allowed.status_code == 201, allowed.text

    wider = copy.deepcopy(spec)
    wider["identity"]["permissions"] = ["tasks.read", "tasks.write", "principals.write"]
    for path in ("/api/v1/agents:validate", "/api/v1/agents"):
        response = await client.post(
            path, json={"key": "coder", "spec": wider}, headers=auth(narrow_key)
        )
        assert response.status_code == 403, response.text
        error = response.json()["error"]
        assert error["code"] == "permission_escalation"
        assert error["details"]["missing"] == ["principals.write"]

    admin_grant = copy.deepcopy(spec)
    admin_grant["identity"]["permissions"] = ["admin"]
    response = await _publish(client, narrow_key, admin_grant)
    # Not even the shape of a permission (§1): refused before any rights check.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

    # Roles and capabilities are handed out with org.manage, as they are today.
    with_role = copy.deepcopy(spec)
    with_role["identity"]["roles"] = ["coder"]
    response = await _publish(client, narrow_key, with_role)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["missing"] == ["org.manage"]

    # Nothing was written by the refusals.
    agent = (await client.get("/api/v1/agents/coder", headers=auth(admin_key))).json()
    assert agent["currentRevision"] == 1

    # An unchanged spec is checked too: a narrower applier cannot re-apply it.
    admin_spec = copy.deepcopy(wider)
    assert (await _publish(client, admin_key, admin_spec)).status_code == 201
    response = await _publish(client, narrow_key, admin_spec)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_escalation"


# --- desired and observed state -------------------------------------------------


async def test_state_moves_without_a_revision(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    await _publish(client, admin_key, coder_spec(workspace["id"]))

    response = await client.patch(
        "/api/v1/agents/coder/state", json={"state": "stopped"}, headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["currentRevision"]) == ("stopped", 1)
    # Same state again: nothing to journal.
    await client.patch(
        "/api/v1/agents/coder/state", json={"state": "stopped"}, headers=auth(admin_key)
    )
    response = await client.patch(
        "/api/v1/agents/coder/state", json={"replicas": 0}, headers=auth(admin_key)
    )
    assert response.json()["replicas"] == 0

    changes = [e["payload"] for e in await _events(client, admin_key, "agent.state_changed")]
    assert [(c["previousState"], c["state"], c["replicas"]) for c in changes] == [
        (None, "running", 1),
        ("running", "stopped", 1),
        ("stopped", "stopped", 0),
    ]
    assert changes[2] == {
        "key": "coder",
        "state": "stopped",
        "replicas": 0,
        "previousState": "stopped",
        "previousReplicas": 1,
    }


async def test_only_the_placement_service_reports_what_runs(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name="fleet-controller",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )

    unknown = await client.get("/api/v1/agents/coder/status", headers=auth(admin_key))
    assert unknown.json()["phase"] == "unknown"
    assert unknown.json()["instances"] is None

    def report(phase: str, at: str, **extra: Any) -> dict[str, Any]:
        return {
            "phase": phase,
            "instances": {"desired": 1, "ready": 1 if phase == "running" else 0},
            "observedAt": at,
            **extra,
        }

    waiting = report(
        "waiting_for_node",
        "2026-09-27T10:00:00+00:00",
        reason={"code": "no_matching_node", "message": "no node has repos"},
    )
    response = await client.put(
        "/api/v1/agents/coder/status", json=waiting, headers=auth(fleet_key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == {"code": "no_matching_node", "message": "no node has repos"}

    # The same report twenty seconds later: stored, not journaled.
    later = {**waiting, "observedAt": "2026-09-27T10:00:20+00:00"}
    assert (
        await client.put("/api/v1/agents/coder/status", json=later, headers=auth(fleet_key))
    ).status_code == 200
    running = report("running", "2026-09-27T10:01:00+00:00", node="node-a", observedRevision=1)
    assert (
        await client.put("/api/v1/agents/coder/status", json=running, headers=auth(fleet_key))
    ).status_code == 200

    stale = await client.put("/api/v1/agents/coder/status", json=waiting, headers=auth(fleet_key))
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_status_report"

    # A manager of the catalog never writes what runs.
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["agents.manage", "agents.read"]
    )
    denied = await client.put(
        "/api/v1/agents/coder/status", json=running, headers=auth(manager_key)
    )
    assert denied.status_code == 403

    observed = (await client.get("/api/v1/agents/coder/status", headers=auth(admin_key))).json()
    assert (observed["phase"], observed["node"], observed["observedRevision"]) == (
        "running",
        "node-a",
        1,
    )

    changes = [e["payload"] for e in await _events(client, admin_key, "agent.status_changed")]
    assert [(c["phase"], c["previousPhase"]) for c in changes] == [
        ("waiting_for_node", None),
        ("running", "waiting_for_node"),
    ]
    assert changes[0]["reasonCode"] == "no_matching_node"


# --- identity, me, retirement -----------------------------------------------------


async def test_the_core_derives_principal_roles_and_binding(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    await create_capability(client, admin_key, "python")
    spec = coder_spec(workspace["id"])
    spec["identity"]["capabilities"] = ["python"]
    await _publish(client, admin_key, spec)
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name="fleet-controller",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    iam_principal = str(uuid.uuid4())
    identity = {"iamPrincipalId": iam_principal, "iamTenantId": str(uuid.uuid4())}

    linked = await _link(client, fleet_key, **identity)
    assert linked.status_code == 200, linked.text
    principal_id = linked.json()["principalId"]
    assert principal_id is not None

    principal = (
        await client.get(f"/api/v1/principals/{principal_id}", headers=auth(admin_key))
    ).json()
    assert (principal["kind"], principal["displayName"]) == ("agent", spec["displayName"])
    bindings = (
        await client.get(f"/api/v1/principals/{principal_id}/iam-bindings", headers=auth(admin_key))
    ).json()["items"]
    assert [b["iamPrincipalId"] for b in bindings] == [iam_principal]
    assert bindings[0]["permissions"] == sorted(spec["identity"]["permissions"])
    roles = (
        await client.get(f"/api/v1/principals/{principal_id}/roles", headers=auth(admin_key))
    ).json()["items"]
    assert len(roles) == 1

    # The same identity again changes nothing; another one is a conflict.
    assert (await _link(client, fleet_key, **identity)).json()["principalId"] == principal_id
    conflict = await _link(client, fleet_key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "agent_identity_conflict"

    # A new revision with narrower rights reshapes the binding in the same write.
    narrower = copy.deepcopy(spec)
    narrower["identity"]["permissions"] = ["tasks.read"]
    narrower["identity"]["roles"] = []
    response = await _publish(client, admin_key, narrower)
    assert response.status_code == 201, response.text
    bindings = (
        await client.get(f"/api/v1/principals/{principal_id}/iam-bindings", headers=auth(admin_key))
    ).json()["items"]
    assert bindings[0]["permissions"] == ["tasks.read"]
    roles = (
        await client.get(f"/api/v1/principals/{principal_id}/roles", headers=auth(admin_key))
    ).json()["items"]
    assert roles == []
    published = await _events(client, admin_key, "agent.revision_published")
    assert published[-1]["payload"]["permissionsChanged"] is True


async def test_me_and_retirement(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    agent_key = await _api_key(client, admin_key, principal_id)

    me = await client.get("/api/v1/agents/me", headers=auth(agent_key))
    assert me.status_code == 200, me.text
    assert (me.json()["key"], me.json()["status"]) == ("coder", "active")
    assert (await client.get("/api/v1/agents/me", headers=auth(admin_key))).status_code == 404

    session = await open_session(client, agent_key)
    task = await create_task(client, admin_key, workspaceId=workspace["id"])
    claimed = await claim_task(client, agent_key, task["id"], session["id"])
    assert claimed.status_code == 200, claimed.text

    retired = await client.post(
        "/api/v1/agents/coder:retire",
        json={"reason": "replaced by coder-2"},
        headers=auth(admin_key),
    )
    assert retired.status_code == 200, retired.text
    assert (retired.json()["status"], retired.json()["state"]) == ("retired", "stopped")

    principal = (
        await client.get(f"/api/v1/principals/{principal_id}", headers=auth(admin_key))
    ).json()
    assert principal["status"] == "disabled"
    bindings = (
        await client.get(f"/api/v1/principals/{principal_id}/iam-bindings", headers=auth(admin_key))
    ).json()["items"]
    assert {b["status"] for b in bindings} == {"revoked"}
    task_after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert task_after["activeClaimId"] is None

    events = await _events(client, admin_key, "agent.retired")
    assert [e["payload"] for e in events] == [
        {
            "key": "coder",
            "revision": 1,
            "principalId": principal_id,
            "reason": "replaced by coder-2",
            "releasedClaims": 1,
        }
    ]

    # Idempotent, and the key is never reused.
    again = await client.post(
        "/api/v1/agents/coder:retire", json={"reason": "again"}, headers=auth(admin_key)
    )
    assert again.status_code == 200
    assert len(await _events(client, admin_key, "agent.retired")) == 1
    for response in (
        await _publish(client, admin_key, coder_spec(workspace["id"])),
        await client.patch(
            "/api/v1/agents/coder/state", json={"state": "running"}, headers=auth(admin_key)
        ),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "agent_retired"


# --- the revision of a run (§7) -------------------------------------------------


async def _api_key(client: httpx.AsyncClient, admin_key: str, principal_id: str) -> str:
    response = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": ["sessions.open", "tasks.read", "tasks.claim"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    key: str = response.json()["key"]
    return key


async def _claimed(
    client: httpx.AsyncClient, admin_key: str, key: str, workspace_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = await open_session(client, key)
    task = await create_task(client, admin_key, workspaceId=workspace_id)
    claim = (await claim_task(client, key, task["id"], session["id"])).json()
    return task, claim


async def _start(
    client: httpx.AsyncClient, key: str, task: dict[str, Any], claim: dict[str, Any], **extra: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"], **extra},
        headers=auth(key),
    )


async def test_an_agent_runs_by_a_revision_of_its_own(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    first = (await _publish(client, admin_key, coder_spec(workspace["id"]))).json()
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    agent_key = await _api_key(client, admin_key, principal_id)

    other = (await _publish(client, admin_key, coder_spec(workspace["id"]), agent="other")).json()
    second = (
        await _publish(client, admin_key, coder_spec(workspace["id"], displayName="Coder v2"))
    ).json()

    task, claim = await _claimed(client, admin_key, agent_key, workspace["id"])
    missing = await _start(client, agent_key, task, claim)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "agent_revision_required"

    foreign = await _start(client, agent_key, task, claim, agentRevisionId=other["revision"]["id"])
    assert foreign.status_code == 422
    assert foreign.json()["error"]["code"] == "agent_revision_mismatch"

    unknown = await _start(client, agent_key, task, claim, agentRevisionId=str(uuid.uuid4()))
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "agent_revision_mismatch"

    # Its own, even if no longer current: a publication racing a start is not a failure.
    started = await _start(client, agent_key, task, claim, agentRevisionId=first["revision"]["id"])
    assert started.status_code == 201, started.text
    run = started.json()
    assert run["agentRevisionId"] == first["revision"]["id"]
    assert first["revision"]["id"] != second["revision"]["id"]
    fetched = (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(admin_key))).json()
    assert fetched["agentRevisionId"] == first["revision"]["id"]

    event = next(
        e for e in await _events(client, admin_key, "run.started") if e["entityId"] == run["id"]
    )
    assert event["schemaVersion"] == 2
    assert event["payload"]["agentRevisionId"] == first["revision"]["id"]


async def test_a_principal_that_is_not_an_agent_names_no_revision(
    client: httpx.AsyncClient,
) -> None:
    admin_key, workspace = await _tenant(client)
    agent = (await _publish(client, admin_key, coder_spec(workspace["id"]))).json()
    _, runner_key = await create_agent_with_key(client, admin_key, name="runner")

    task, claim = await _claimed(client, admin_key, runner_key, workspace["id"])
    refused = await _start(client, runner_key, task, claim, agentRevisionId=agent["revision"]["id"])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "agent_revision_mismatch"

    started = await _start(client, runner_key, task, claim)
    assert started.status_code == 201, started.text
    assert started.json()["agentRevisionId"] is None


async def test_the_settings_of_a_skills_executor_are_stored_and_shown(
    client: httpx.AsyncClient,
) -> None:
    """``executor.params.env`` of kind ``skills`` (CP-ADR-0073, amendment 2026-10-01).

    Data of the kind: the core stores it as given and shows it with the
    revision, so an operator reads the portal address the skills use from the
    description, not from the node's environment; a credential is still refused.
    """
    key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = copy.deepcopy(_fixture("skills-executor.yaml")["spec"])
    spec.pop("state", None)
    spec["executor"]["params"] = {"env": {"PORTAL_URL": "https://portal.example.test"}}
    published = await _publish(client, key, spec, agent="skills-executor")
    assert published.status_code == 201, published.text

    shown = await client.get("/api/v1/agents/skills-executor", headers=auth(key))
    assert shown.status_code == 200, shown.text
    assert shown.json()["revision"]["spec"]["executor"]["params"] == {
        "env": {"PORTAL_URL": "https://portal.example.test"}
    }

    # A setting named as a credential is refused by the core's scan of the params.
    spec["executor"]["params"] = {"env": {"PORTAL_TOKEN": "x"}}
    refused = await _publish(client, key, spec, agent="skills-executor")
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "secret_material_rejected"
