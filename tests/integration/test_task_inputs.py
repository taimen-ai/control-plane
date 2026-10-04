"""Inputs of a task type: artifactSchema, context, claim refusal (CP-ADR-0072 §7, §8, A005).

A task type version declares which artifacts its tasks take in, by artifact
type and by the relation to the task that produced them. The inputs resolve
to head revisions on the source tasks and reach the executor in both
contexts; a task missing a required one is refused at claim, explained in its
claimability and not offered to a runner.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from control_plane.application.commands import artifacts as artifact_commands
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.content_store import InMemoryContentStore
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)

SPEC_INPUT = {"key": "spec", "type": "design-doc", "from": "spawned_by", "required": True}


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def boot(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin, name="runner", permissions=ORG_AGENT_PERMISSIONS
    )
    for key in ("design-doc", "notes"):
        response = await client.post(
            "/api/v1/artifact-types",
            json={"key": key, "displayName": key, "mediaTypes": ["text/*", "application/pdf"]},
            headers=auth(admin),
        )
        assert response.status_code == 201, response.text
    return {"admin": admin, "agent": agent_key, "agentId": agent["id"]}


async def create_task_type(
    client: httpx.AsyncClient, key: str, artifact_schema: Any, *, type_key: str = "consumer"
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": "Consumer", "artifactSchema": artifact_schema},
        headers=auth(key),
    )


async def consumer_type(client: httpx.AsyncClient, key: str, *inputs: dict[str, Any]) -> None:
    response = await create_task_type(client, key, {"inputs": list(inputs)})
    assert response.status_code == 201, response.text


async def relate(client: httpx.AsyncClient, key: str, from_id: str, to_id: str, kind: str) -> None:
    response = await client.post(
        f"/api/v1/tasks/{from_id}/relations",
        json={"toTask": to_id, "type": kind},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def artifact(
    client: httpx.AsyncClient, key: str, task_id: str, type_: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/artifacts",
        json={"task": task_id, "type": type_, "name": f"{type_}.md", **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def spawned_pair(
    client: httpx.AsyncClient, key: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A source task and a consumer spawned by it."""
    source = await create_task(client, key, title="Design")
    consumer = await create_task(client, key, title="Implement", typeKey="consumer")
    await relate(client, key, consumer["id"], source["id"], "spawned_by")
    return source, consumer


async def claimability(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}/claimability", headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def available_ids(client: httpx.AsyncClient, key: str) -> set[str]:
    response = await client.get("/api/v1/work/available", headers=auth(key))
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def start_run(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    session = await open_session(client, key)
    claim = await claim_task(client, key, task_id, session["id"])
    assert claim.status_code == 200, claim.text
    run = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim.json()["id"], "fencingToken": claim.json()["fencingToken"]},
        headers=auth(key),
    )
    assert run.status_code == 201, run.text
    return run.json()


# --- publication -------------------------------------------------------------


async def test_artifact_schema_is_published_with_the_version(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    schema = {
        "inputs": [SPEC_INPUT, {"key": "notes", "type": "notes", "from": "parent"}],
        "outputs": [
            {
                "key": "plan",
                "type": "design-doc",
                "required": True,
                "mediaTypes": ["text/markdown"],
            }
        ],
    }
    created = await create_task_type(client, boot["admin"], schema)
    assert created.status_code == 201, created.text
    assert created.json()["artifactSchema"] == schema

    events = (
        await client.get(
            "/api/v1/events", params={"types": "task_type.created"}, headers=auth(boot["admin"])
        )
    ).json()["items"]
    consumer = next(e for e in events if e["entityId"] == created.json()["id"])
    assert consumer["payload"]["declaresArtifactSchema"] is True
    assert (consumer["payload"]["inputs"], consumer["payload"]["outputs"]) == (2, 1)
    assert consumer["schemaVersion"] == 3

    plain = await create_task_type(client, boot["admin"], {}, type_key="plain")
    assert plain.json()["artifactSchema"] == {}


@pytest.mark.parametrize(
    ("schema", "code", "field"),
    [
        ({"inputs": [{**SPEC_INPUT, "type": "unknown"}]}, "unknown_artifact_type",
         "artifactSchema.inputs[0].type"),
        ({"outputs": [{"key": "x", "type": "nothing"}]}, "unknown_artifact_type",
         "artifactSchema.outputs[0].type"),
        ({"inputs": [{**SPEC_INPUT, "from": "related_to"}]}, "invalid_artifact_schema",
         "artifactSchema.inputs[0].from"),
        ({"inputs": [SPEC_INPUT, SPEC_INPUT]}, "invalid_artifact_schema",
         "artifactSchema.inputs[1].key"),
        ({"inputs": [{**SPEC_INPUT, "key": "Spec"}]}, "invalid_artifact_schema",
         "artifactSchema.inputs[0].key"),
        ({"inputs": [{**SPEC_INPUT, "extra": 1}]}, "invalid_artifact_schema",
         "artifactSchema.inputs[0]"),
        ({"stages": []}, "invalid_artifact_schema", "artifactSchema"),
        ({"outputs": [{"key": "x", "type": "notes", "mediaTypes": ["image/png"]}]},
         "invalid_artifact_schema", "artifactSchema.outputs[0].mediaTypes[0]"),
        ({"outputs": [{"key": "x", "type": "notes", "content": "maybe"}]},
         "invalid_artifact_schema", "artifactSchema.outputs[0].content"),
    ],
)  # fmt: skip
async def test_invalid_artifact_schema_is_refused(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    schema: dict[str, Any],
    code: str,
    field: str,
) -> None:
    response = await create_task_type(client, boot["admin"], schema)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"]["field"] == field


async def test_artifact_schema_is_immutable_even_against_raw_sql(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    created = (await create_task_type(client, boot["admin"], {"inputs": [SPEC_INPUT]})).json()
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET artifact_schema = '{}'::jsonb WHERE id = :id"),
            {"id": created["id"]},
        )


# --- inputs in both contexts -------------------------------------------------


async def test_spawned_by_input_reaches_both_contexts(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await consumer_type(client, boot["admin"], SPEC_INPUT)
    source, consumer = await spawned_pair(client, boot["admin"])
    first = await artifact(client, boot["agent"], source["id"], "design-doc", uri="https://x/1")
    head = await artifact(
        client,
        boot["agent"],
        source["id"],
        "design-doc",
        uri="https://x/2",
        supersedesArtifactId=first["id"],
    )
    # Another type on the source and the same type on an unrelated task: not inputs.
    await artifact(client, boot["agent"], source["id"], "notes")
    other = await create_task(client, boot["admin"], title="Unrelated")
    await artifact(client, boot["agent"], other["id"], "design-doc")

    expected = [
        {
            "key": "spec",
            "type": "design-doc",
            "artifactId": head["id"],
            "name": "design-doc.md",
            "mediaType": None,
            "sizeBytes": None,
            "sha256": None,
            "contentState": "none",
            "uri": "https://x/2",
            "sourceTask": {
                "id": source["id"],
                "publicId": source["publicId"],
                "relation": "spawned_by",
            },
        }
    ]
    # The source is still open: its status does not matter.
    run = await start_run(client, boot["agent"], consumer["id"])
    context = await client.get(f"/api/v1/runs/{run['id']}/context", headers=auth(boot["agent"]))
    assert context.status_code == 200, context.text
    assert context.json()["inputs"] == expected

    focused = await client.post(
        "/api/v1/context", json={"task": consumer["id"]}, headers=auth(boot["agent"])
    )
    assert focused.status_code == 200, focused.text
    assert focused.json()["operational"]["focus"]["inputs"] == expected


async def test_every_source_and_head_is_an_input_and_a_type_without_schema_has_none(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await consumer_type(
        client, boot["admin"], {"key": "prior", "type": "notes", "from": "depends_on"}
    )
    consumer = await create_task(client, boot["admin"], title="Consumer", typeKey="consumer")
    produced = []
    for title in ("A", "B"):
        source = await create_task(client, boot["admin"], title=title)
        await relate(client, boot["admin"], consumer["id"], source["id"], "depends_on")
        produced.append((await artifact(client, boot["agent"], source["id"], "notes"))["id"])
    # Two heads on one source: both.
    produced.append((await artifact(client, boot["agent"], source["id"], "notes"))["id"])

    focused = await client.post(
        "/api/v1/context", json={"task": consumer["id"]}, headers=auth(boot["agent"])
    )
    inputs = focused.json()["operational"]["focus"]["inputs"]
    assert [i["artifactId"] for i in inputs] == produced
    assert {i["sourceTask"]["relation"] for i in inputs} == {"depends_on"}

    plain = await create_task(client, boot["admin"], title="Plain")
    focused = await client.post(
        "/api/v1/context", json={"task": plain["id"]}, headers=auth(boot["agent"])
    )
    assert focused.json()["operational"]["focus"]["inputs"] == []


# --- claim refusal -----------------------------------------------------------


async def test_missing_required_input_refuses_claim_and_hides_the_task(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    optional = {"key": "notes", "type": "notes", "from": "parent"}
    await consumer_type(client, boot["admin"], SPEC_INPUT, optional)
    source, consumer = await spawned_pair(client, boot["admin"])
    missing = [{"key": "spec", "type": "design-doc", "from": "spawned_by"}]

    session = await open_session(client, boot["agent"])
    refused = await claim_task(client, boot["agent"], consumer["id"], session["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "input_missing"
    assert refused.json()["error"]["details"]["missing"] == missing

    diagnosis = await claimability(client, boot["agent"], consumer["id"])
    assert diagnosis["claimable"] is False
    assert {"code": "input_missing", "missing": missing} in diagnosis["reasons"]
    assert consumer["id"] not in await available_ids(client, boot["agent"])
    assert source["id"] in await available_ids(client, boot["agent"])

    # The optional input stays absent; the required one arrives.
    await artifact(client, boot["agent"], source["id"], "design-doc")
    assert (await claimability(client, boot["agent"], consumer["id"]))["claimable"] is True
    assert consumer["id"] in await available_ids(client, boot["agent"])
    claimed = await claim_task(client, boot["agent"], consumer["id"], session["id"])
    assert claimed.status_code == 200, claimed.text


# --- reading an input as the receiving task ----------------------------------


def deny_on_task(monkeypatch: pytest.MonkeyPatch, principal_id: str, task_id: str) -> None:
    """A PDP that refuses ``principal_id`` everything about one task."""
    real: Callable[..., Any] = artifact_commands.authorize

    async def authorize(ctx: Any, *any_of: Any, resource: Any = None, **kwargs: Any) -> None:
        if (
            str(ctx.principal_id) == principal_id
            and resource is not None
            and resource.key == f"task:{task_id}"
        ):
            raise AuthorizationError(details={"resource": resource.key})
        await real(ctx, *any_of, resource=resource, **kwargs)

    monkeypatch.setattr(artifact_commands, "authorize", authorize)


async def test_input_is_read_for_the_receiving_task(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await consumer_type(client, boot["admin"], SPEC_INPUT)
    source, consumer = await spawned_pair(client, boot["admin"])
    upload = await client.put(
        "/api/v1/artifact-contents",
        content=b"# spec",
        headers={**auth(boot["admin"]), "Content-Type": "text/markdown"},
    )
    assert upload.status_code == 201, upload.text
    spec = await artifact(
        client, boot["admin"], source["id"], "design-doc", contentRef=upload.json()["contentRef"]
    )
    note = await artifact(client, boot["admin"], source["id"], "notes")
    run = await start_run(client, boot["agent"], consumer["id"])

    # The executor of the next step has no right on the source task.
    deny_on_task(monkeypatch, boot["agentId"], source["id"])
    url = f"/api/v1/artifacts/{spec['id']}"
    as_input = {"forTask": consumer["publicId"]}
    assert (await client.get(url, headers=auth(boot["agent"]))).status_code == 403
    record = await client.get(url, params=as_input, headers=auth(boot["agent"]))
    assert record.status_code == 200, record.text
    assert record.json()["id"] == spec["id"]
    content = await client.get(f"{url}/content", params=as_input, headers=auth(boot["agent"]))
    assert content.status_code == 200, content.text
    assert content.content == b"# spec"

    # Not an input of that task: the ordinary check on the artifact's task.
    foreign = await client.get(
        f"/api/v1/artifacts/{note['id']}", params=as_input, headers=auth(boot["agent"])
    )
    assert foreign.status_code == 403
    unknown = await client.get(url, params={"forTask": "TASK-999999"}, headers=auth(boot["agent"]))
    assert unknown.status_code == 404

    reads = (await client.get("/api/v1/events?limit=200", headers=auth(boot["admin"]))).json()
    read = next(e for e in reads["items"] if e["type"] == "artifact.content_read")
    assert read["payload"]["taskId"] == source["id"]
    assert read["payload"]["forTaskId"] == consumer["id"]
    assert read["payload"]["runId"] == run["id"]
