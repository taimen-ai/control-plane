"""Typed outputs of a task executed by a skill (CP-ADR-0072, amendment 2026-10-01).

A task type executed by a skill declares outputs in ``artifactSchema``; when
the run's execution call succeeds, the field of the skill's output named like
an output becomes an artifact of the declared type on the task — stored
``application/json`` content, authored by the skill executor — and reaches a
task spawned by it as an input. A neutral package: drafting notes.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from control_plane.config import Settings
from control_plane.infrastructure.content_store import InMemoryContentStore
from control_plane.worker.main import Worker
from tests.helpers import assign_skill, auth, create_agent_with_key, create_task, do_bootstrap
from tests.integration.test_child_run_handle import claim_and_run
from tests.integration.test_skill_invocations_m21 import claim, invoke, published
from tests.integration.test_task_inputs import relate, start_run

RUNNER = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "skills.invoke",
    "skills.execute",
    "artifacts.read",
    "artifacts.write",
]
EXECUTOR = ["skills.execute", "sessions.open"]
DRAFT_SCHEMA = {
    "type": "object",
    "required": ["title", "body"],
    "properties": {"title": {"type": "string", "minLength": 1}, "body": {"type": "string"}},
}
SKILL_OUTPUTS = {
    "type": "object",
    "required": ["draft", "reason"],
    "properties": {
        "draft": {"type": ["object", "null"]},
        "reason": {"type": ["string", "null"]},
    },
}
DRAFT = {"title": "Quarterly note", "body": "Всё по плану."}
DRAFT_OUTPUT = {"key": "draft", "type": "note-draft"}


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


@pytest.fixture
async def boot(client: httpx.AsyncClient, store: InMemoryContentStore) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    runner, runner_key = await create_agent_with_key(
        client, admin, name="runner", permissions=RUNNER
    )
    executor, executor_key = await create_agent_with_key(
        client, admin, name="executor", permissions=EXECUTOR
    )
    response = await client.post(
        "/api/v1/artifact-types",
        json={
            "key": "note-draft",
            "displayName": "Note draft",
            "mediaTypes": ["application/json", "text/markdown"],
            "metadataSchema": DRAFT_SCHEMA,
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    skill = await published(
        client, admin, "notes.draft", version="1", outputs=SKILL_OUTPUTS, idempotency="natural"
    )
    await assign_skill(client, admin, runner["id"], skill["id"])
    review = await client.post(
        "/api/v1/task-types",
        json={
            "key": "draft-review",
            "displayName": "Review the draft",
            "artifactSchema": {
                "inputs": [{"key": "draft", "type": "note-draft", "from": "spawned_by"}]
            },
        },
        headers=auth(admin),
    )
    assert review.status_code == 201, review.text
    return {
        "admin": admin,
        "runner": runner_key,
        "executor": executor_key,
        "executorId": executor["id"],
    }


async def drafting_type(
    client: httpx.AsyncClient, boot: dict[str, Any], *outputs: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "drafting",
            "displayName": "Draft a note",
            "execution": {"skill": "notes.draft", "version": "1"},
            "artifactSchema": {"outputs": list(outputs or [DRAFT_OUTPUT])},
        },
        headers=auth(boot["admin"]),
    )
    assert response.status_code == 201, response.text


async def execute(
    client: httpx.AsyncClient, boot: dict[str, Any], output: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A drafting task, its run's execution call completed with ``output``."""
    task = await create_task(
        client, boot["admin"], title="Draft", typeKey="drafting", customFields={"query": "q"}
    )
    _, run = await claim_and_run(client, boot["runner"], task["id"])
    created = await invoke(
        client,
        boot["runner"],
        "notes.draft@1",
        {"query": "q"},
        runId=run["id"],
        idempotencyKey=f"execution:{run['id']}",
    )
    assert created.status_code == 201, created.text
    assert created.json()["authorizationBasis"]["kind"] == "execution"
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": output},
        headers=auth(boot["executor"]),
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"
    return task, run, done.json()


async def get_json(client: httpx.AsyncClient, key: str, url: str, **params: Any) -> Any:
    response = await client.get(url, params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def outputs_of(client: httpx.AsyncClient, key: str, invocation: dict[str, Any]) -> Any:
    result = await get_json(client, key, f"/api/v1/artifacts/{invocation['artifactId']}")
    assert result["type"] == "skill_result"
    return result["metadata"]["outputs"]


async def drafts_of(client: httpx.AsyncClient, key: str, task: dict[str, Any]) -> list[Any]:
    listed = await get_json(client, key, "/api/v1/artifacts", taskId=task["id"])
    return [a for a in listed["items"] if a["type"] == "note-draft"]


async def review_of(
    client: httpx.AsyncClient, boot: dict[str, Any], task: dict[str, Any]
) -> tuple[dict[str, Any], list[Any]]:
    """A review task spawned by ``task`` and the inputs it is given."""
    review = await create_task(client, boot["admin"], title="Review", typeKey="draft-review")
    await relate(client, boot["admin"], review["id"], task["id"], "spawned_by")
    focused = await client.post(
        "/api/v1/context", json={"task": review["id"]}, headers=auth(boot["runner"])
    )
    assert focused.status_code == 200, focused.text
    return review, focused.json()["operational"]["focus"]["inputs"]


async def events_of(client: httpx.AsyncClient, key: str, event_type: str) -> list[Any]:
    body = await get_json(client, key, "/api/v1/events", limit=200)
    return [e for e in body["items"] if e["type"] == event_type]


# --- the value becomes the typed output, and the next task's input ---------------


async def test_a_skill_output_becomes_the_typed_artifact_and_the_next_input(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    await drafting_type(client, boot)
    task, run, invocation = await execute(client, boot, {"draft": DRAFT, "reason": None})

    [draft] = await drafts_of(client, boot["admin"], task)
    assert draft["name"] == "draft.json"
    assert draft["runId"] == run["id"]
    assert draft["createdByPrincipalId"] == boot["executorId"]
    assert (draft["contentState"], draft["mediaType"]) == ("stored", "application/json")
    assert draft["typeVersion"] == 1
    assert draft["metadata"]["output"] == "draft"
    assert draft["metadata"]["invocationId"] == invocation["id"]
    assert await outputs_of(client, boot["admin"], invocation) == [
        {"key": "draft", "type": "note-draft", "status": "created", "artifactId": draft["id"]}
    ]
    # The skill_result is still there, as before.
    result = await get_json(client, boot["admin"], f"/api/v1/artifacts/{invocation['artifactId']}")
    assert result["content"]["output"]["draft"] == DRAFT

    content = await client.get(
        f"/api/v1/artifacts/{draft['id']}/content", headers=auth(boot["admin"])
    )
    assert content.status_code == 200, content.text
    assert json.loads(content.content) == DRAFT

    review, inputs = await review_of(client, boot, task)
    assert [(i["key"], i["artifactId"], i["contentState"]) for i in inputs] == [
        ("draft", draft["id"], "stored")
    ]
    assert inputs[0]["sourceTask"]["relation"] == "spawned_by"
    # The reviewer reads it as an input of its own task.
    await start_run(client, boot["runner"], review["id"])
    as_input = await client.get(
        f"/api/v1/artifacts/{draft['id']}/content",
        params={"forTask": review["publicId"]},
        headers=auth(boot["runner"]),
    )
    assert as_input.status_code == 200, as_input.text
    assert json.loads(as_input.content) == DRAFT

    [succeeded] = await events_of(client, boot["admin"], "skill.invocation_succeeded")
    assert succeeded["schemaVersion"] == 2
    assert succeeded["payload"]["outputs"][0]["status"] == "created"
    created = [
        e
        for e in await events_of(client, boot["admin"], "artifact.created")
        if e["entityId"] == draft["id"]
    ]
    assert created[0]["payload"]["skillInvocationId"] == invocation["id"]
    assert "Quarterly" not in json.dumps(created[0]["payload"])


async def test_a_null_value_makes_no_artifact_and_the_next_task_has_no_input(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await drafting_type(client, boot)
    task, _, invocation = await execute(client, boot, {"draft": None, "reason": "refused"})

    assert await drafts_of(client, boot["admin"], task) == []
    assert await outputs_of(client, boot["admin"], invocation) == [
        {"key": "draft", "type": "note-draft", "status": "absent"}
    ]
    review, inputs = await review_of(client, boot, task)
    assert inputs == []
    claimable = await get_json(client, boot["runner"], f"/api/v1/tasks/{review['id']}/claimability")
    assert claimable["claimable"] is True, claimable


async def test_an_absent_field_is_like_null(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    # The output is declared, the skill has no such field at all.
    await drafting_type(client, boot, DRAFT_OUTPUT, {"key": "summary", "type": "note-draft"})
    task, _, invocation = await execute(client, boot, {"draft": DRAFT, "reason": None})
    assert [o["status"] for o in await outputs_of(client, boot["admin"], invocation)] == [
        "created",
        "absent",
    ]
    assert len(await drafts_of(client, boot["admin"], task)) == 1


# --- a value the type refuses ----------------------------------------------------


async def test_a_value_failing_the_type_rejects_the_output_and_keeps_the_result(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await drafting_type(client, boot)
    bad = {"title": "", "pages": 3}
    task, _, invocation = await execute(client, boot, {"draft": bad, "reason": None})

    assert await drafts_of(client, boot["admin"], task) == []
    [rejected] = await outputs_of(client, boot["admin"], invocation)
    assert (rejected["key"], rejected["status"]) == ("draft", "rejected")
    assert rejected["reason"]["code"] == "invalid_output_value"
    paths = {e["path"] for e in rejected["reason"]["details"]["errors"]}
    assert paths == {"/", "/title"}  # body missing, title empty
    assert rejected["reason"]["details"]["artifactType"] == "note-draft"
    # The call itself succeeded: its result is kept.
    result = await get_json(client, boot["admin"], f"/api/v1/artifacts/{invocation['artifactId']}")
    assert result["content"]["output"]["draft"] == bad


async def test_an_output_narrowed_away_from_json_is_rejected(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await drafting_type(client, boot, {**DRAFT_OUTPUT, "mediaTypes": ["text/markdown"]})
    task, _, invocation = await execute(client, boot, {"draft": DRAFT, "reason": None})
    [rejected] = await outputs_of(client, boot["admin"], invocation)
    assert rejected["reason"]["code"] == "media_type_not_allowed"
    assert rejected["reason"]["details"]["allowed"] == ["text/markdown"]
    assert await drafts_of(client, boot["admin"], task) == []


async def test_an_unreachable_store_rejects_the_output(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    await drafting_type(client, boot)
    store.available = False
    task, _, invocation = await execute(client, boot, {"draft": DRAFT, "reason": None})
    [rejected] = await outputs_of(client, boot["admin"], invocation)
    assert rejected["reason"]["code"] == "content_store_unavailable"
    assert await drafts_of(client, boot["admin"], task) == []


# --- revisions, required outputs, other calls ---------------------------------------


async def test_a_new_hand_in_supersedes_the_head_of_the_type(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await drafting_type(client, boot)
    task = await create_task(
        client, boot["admin"], title="Draft", typeKey="drafting", customFields={"query": "q"}
    )
    earlier = await client.post(
        "/api/v1/artifacts",
        json={"task": task["id"], "type": "note-draft", "name": "manual", "metadata": DRAFT},
        headers=auth(boot["admin"]),
    )
    assert earlier.status_code == 201, earlier.text
    _, run = await claim_and_run(client, boot["runner"], task["id"])
    await invoke(
        client,
        boot["runner"],
        "notes.draft@1",
        {"query": "q"},
        runId=run["id"],
        idempotencyKey=f"execution:{run['id']}",
    )
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"draft": DRAFT, "reason": None}},
        headers=auth(boot["executor"]),
    )
    assert done.status_code == 200, done.text

    _, inputs = await review_of(client, boot, task)
    assert len(inputs) == 1
    draft = await get_json(client, boot["admin"], f"/api/v1/artifacts/{inputs[0]['artifactId']}")
    assert draft["supersedesArtifactId"] == earlier.json()["id"]


async def test_a_required_output_passes_verification_when_the_skill_hands_it_in(
    client: httpx.AsyncClient, boot: dict[str, Any], worker: Worker
) -> None:
    await drafting_type(client, boot, {**DRAFT_OUTPUT, "required": True})
    task, run, invocation = await execute(client, boot, {"draft": DRAFT, "reason": None})
    succeeded = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"completeTask": True, "output": {"skillInvocationId": invocation["id"]}},
        headers=auth(boot["runner"]),
    )
    assert succeeded.status_code == 200, succeeded.text
    await worker.run_once()
    done = await get_json(client, boot["admin"], f"/api/v1/tasks/{task['id']}")
    assert done["systemStatusCategory"] == "terminal_success", done["verification"]


async def test_a_required_output_left_null_is_missing_and_fails_verification(
    client: httpx.AsyncClient, boot: dict[str, Any], worker: Worker
) -> None:
    await drafting_type(client, boot, {**DRAFT_OUTPUT, "required": True})
    task, run, invocation = await execute(client, boot, {"draft": None, "reason": "refused"})
    assert await outputs_of(client, boot["admin"], invocation) == [
        {"key": "draft", "type": "note-draft", "status": "missing"}
    ]
    succeeded = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"completeTask": True},
        headers=auth(boot["runner"]),
    )
    assert succeeded.status_code == 200, succeeded.text
    await worker.run_once()
    attempts = await get_json(client, boot["admin"], f"/api/v1/tasks/{task['id']}/verifications")
    assert attempts["items"][0]["results"][0]["reason"] == "artifact_missing"


async def test_a_call_that_is_not_the_execution_call_hands_in_no_outputs(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await drafting_type(client, boot)
    task = await create_task(
        client, boot["admin"], title="Draft", typeKey="drafting", customFields={"query": "q"}
    )
    # Invoked for the task, outside of its run: evidence only.
    created = await invoke(
        client, boot["runner"], "notes.draft@1", {"query": "q"}, taskId=task["id"]
    )
    assert created.status_code == 201, created.text
    assert created.json()["authorizationBasis"] is None
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"draft": DRAFT, "reason": None}},
        headers=auth(boot["executor"]),
    )
    assert done.status_code == 200, done.text
    assert await outputs_of(client, boot["admin"], done.json()) == []
    assert await drafts_of(client, boot["admin"], task) == []
