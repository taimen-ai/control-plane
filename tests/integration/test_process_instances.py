"""Process instances at work on Postgres (CP-ADR-0074 §3-§8; process-packages P009).

The acceptance of P009: an observation starts an instance, whose human step
files a task of the core; the task's completion moves the instance on; a
change of the data moves the deadline of the next step's timers; the timers
fire, escalate and close the case. The same start event again reaches the
same instance (``process.correlated``), never a second one.

Everything runs through the public API and the worker, as in production:
the instance acts as its identity agent, its facts come back from the
journal through the worker's own cursor, its timers are rows the worker's
timer loop picks up. Observation kinds and task types are neutral
(``sample.*``): the core knows no domain.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    do_bootstrap,
)

AGENT = "sample-process"
ISSUER = "https://iam.example.test"
AGENT_PERMISSIONS = [
    "approvals.manage",
    "events.read",
    "skills.invoke",
    "tasks.read",
    "tasks.write",
]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _case(admin: str) -> dict[str, Any]:
    """A case: a review, then a signature due by a deadline the case may move.

    The signature escalates at its deadline (``notify``) and a second after it
    (``raise``); the raise is caught and closes the case as ``overdue``.
    """
    person = [{"principal": admin}]
    return {
        "version": 1,
        "displayName": "Sample case",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "deadline": {"type": "string", "format": "date-time"},
                "decision": {"type": "string"},
            },
        },
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {
                "number": "string(event.payload.data.number)",
                "deadline": "timestamp(event.payload.data.deadline)",
            },
        },
        "correlate": [
            {
                "on": {"observation": "sample.moved"},
                "key": "event.payload.data.number",
                "set": {"deadline": "timestamp(event.payload.data.deadline)"},
            }
        ],
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": person},
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {
                        "id": "guard",
                        "try": {
                            "do": [
                                {
                                    "id": "sign",
                                    "human": {
                                        "taskType": "review",
                                        "assign": person,
                                        "due": {"at": "data.deadline"},
                                        "escalations": [
                                            {"after": "due", "action": "notify", "to": person},
                                            {
                                                "after": "PT1S",
                                                "action": "raise",
                                                "error": {"type": "overdue"},
                                            },
                                        ],
                                    },
                                }
                            ],
                            "catch": [
                                {
                                    "errors": {"type": "overdue"},
                                    "do": [
                                        {"id": "closed-overdue", "complete": {"outcome": "overdue"}}
                                    ],
                                }
                            ],
                        },
                    },
                    {"id": "done", "complete": {"outcome": "signed"}},
                ],
            }
        ],
    }


# A standing goal (TAI-ADR-0055): no complete; its milestone follows the data.
GOAL: dict[str, Any] = {
    "version": 1,
    "displayName": "Sample goal",
    "identity": {"agent": AGENT},
    "owner": [{"role": "lead"}],
    "data": {"type": "object", "properties": {"open": {"type": "integer"}}},
    "start": {"on": {"observation": "sample.goal"}, "key": "event.payload.data.goal"},
    "correlate": [
        {
            "on": {"observation": "sample.count"},
            "key": "event.payload.data.goal",
            "set": {"open": "int(event.payload.data.open)"},
        }
    ],
    "stages": [
        {
            "id": "watch",
            "milestones": [{"id": "clear", "when": "has(data.open) && data.open == 0"}],
            "steps": [
                {"id": "hold", "listen": {"any": [{"on": {"observation": "sample.never"}}]}},
            ],
        }
    ],
}


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    responses = [
        await client.post(
            "/api/v1/agents",
            json={
                "key": AGENT,
                "spec": {
                    "displayName": "Sample process",
                    "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
                    "placement": "none",
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/task-types",
            json={
                "key": "review",
                "displayName": "Review",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
                "fieldSchema": {"type": "object", "properties": {"decision": {"type": "string"}}},
            },
            headers=auth(key),
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
    principal = await _link_agent(client, key)
    return {"key": key, "admin": boot["adminPrincipal"]["id"], "agent": principal}


async def _link_agent(client: httpx.AsyncClient, admin_key: str) -> str:
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    response = await client.put(
        f"/api/v1/agents/{AGENT}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(fleet_key),
    )
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


async def _publish(client: httpx.AsyncClient, key: str, process: str, spec: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/process-definitions", json={"key": process, "spec": spec}, headers=auth(key)
    )
    assert response.status_code == 201, response.text


async def _observe(client: httpx.AsyncClient, key: str, kind: str, **data: Any) -> None:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": f"{kind} seen", "data": data},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events", params={"types": event_type, "limit": 200}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _instances(client: httpx.AsyncClient, key: str, **params: Any) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/process-instances", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _instance(client: httpx.AsyncClient, key: str, instance_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/process-instances/{instance_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _journal(
    client: httpx.AsyncClient, key: str, instance_id: str, **params: Any
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {**params, "limit": 50, **({"cursor": cursor} if cursor else {})}
        response = await client.get(
            f"/api/v1/process-instances/{instance_id}/journal", params=query, headers=auth(key)
        )
        assert response.status_code == 200, response.text
        page = response.json()
        items += page["items"]
        cursor = page.get("nextCursor")
        if not cursor:
            return items


async def _task(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _complete(
    client: httpx.AsyncClient, key: str, task_id: str, fields: dict[str, Any]
) -> None:
    task = await _task(client, key, task_id)
    patched = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"customFields": fields},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert patched.status_code == 200, patched.text
    done = await client.post(
        f"/api/v1/tasks/{task_id}:complete",
        headers={**auth(key), "If-Match": f'"task-{patched.json()["version"]}"'},
    )
    assert done.status_code == 200, done.text


def _open(instance: dict[str, Any], element: str) -> dict[str, Any]:
    [found] = [e for e in instance["openElements"] if e["id"] == element]
    return found


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _references(sync_engine: Engine, task_id: str) -> list[str]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT external_id FROM external_references"
                " WHERE entity_type = 'task' AND entity_id = :task"
            ),
            {"task": task_id},
        ).all()
    return [row.external_id for row in rows]


# --- the acceptance of P009 --------------------------------------------------------


async def test_start_task_completion_timer_escalation_and_close(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = datetime.now(UTC) + timedelta(days=30)

    # Start: the observation opens the case; its human step is a task of the core.
    await _observe(client, key, "sample.opened", number="S-1", deadline=deadline.isoformat())
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-case")
    assert (instance["instanceKey"], instance["status"], instance["definitionVersion"]) == (
        "S-1",
        "running",
        1,
    )
    assert instance["data"]["number"] == "S-1"
    review = _open(instance, "review")
    assert review["kind"] == "human"
    task = await _task(client, key, review["taskId"])
    assert task["assigneeId"] == s["admin"]
    assert task["goalId"] is None
    assert task["origin"]["kind"] == "process"
    assert task["origin"]["ref"] == f"process/{instance['id']}/review"
    assert _references(sync_engine, review["taskId"]) == [f"process/{instance['id']}/review"]
    [created] = [
        e for e in await _events(client, key, "task.created") if e["entityId"] == task["id"]
    ]
    assert created["actorId"] == s["agent"], "the process acts as its identity agent"
    [started] = await _events(client, key, "process.started")
    assert started["entityId"] == instance["id"]
    assert started["actorId"] == s["agent"]
    assert started["payload"]["triggerType"] == "observation:sample.opened"

    # The start event again: the same instance, correlated.
    await _observe(client, key, "sample.opened", number="S-1", deadline=deadline.isoformat())
    await worker.run_once()
    assert len(await _instances(client, key, definitionKey="sample-case")) == 1
    [correlated] = await _events(client, key, "process.correlated")
    assert correlated["payload"]["instanceKey"] == "S-1"

    # Completion: the task's fields become data, the next step waits with timers.
    await _complete(client, key, review["taskId"], {"decision": "yes"})
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert instance["data"]["decision"] == "yes"
    sign = _open(instance, "sign")
    assert sign["taskId"] is not None
    # The deadline of the step (engine revision 2) and notify at it, raise a second after.
    due, first, second = sorted(
        _time(t["dueAt"]) for t in instance["timers"] if t["element"] == "sign"
    )
    assert abs(first - deadline) < timedelta(seconds=1), "notify at the deadline"
    assert due == first, "the deadline of the step is the due escalations count from"
    assert second - first == timedelta(seconds=1), "raise a second after it"
    assert all(t["state"] == "pending" for t in instance["timers"])

    # A change of the data moves the timers that read it: the deadline is past now.
    moved = datetime.now(UTC) - timedelta(hours=1)
    await _observe(client, key, "sample.moved", number="S-1", deadline=moved.isoformat())
    await worker.run_once()
    rescheduled = await _events(client, key, "process.timer_rescheduled")
    assert len(rescheduled) == 3
    assert {e["payload"]["cause"] for e in rescheduled} == {"data_changed"}
    assert all(e["payload"]["changedFields"] == ["deadline"] for e in rescheduled)
    assert all(
        abs(_time(e["payload"]["dueAt"]) - moved) < timedelta(seconds=2) for e in rescheduled
    )

    # The timers fire: notify at the deadline, raise a second later — caught, closed.
    # The deadline of the step is its own fact, not process.timer_fired.
    await worker.run_once()
    fired = await _events(client, key, "process.timer_fired")
    assert len(fired) == 2
    [breached] = await _events(client, key, "process.sla_breached")
    assert breached["payload"]["element"] == "sign"
    escalated = await _events(client, key, "process.escalated")
    assert [(e["payload"]["level"], e["payload"]["action"]) for e in escalated] == [
        (1, "notify"),
        (2, "raise"),
    ]
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["outcome"]) == ("completed", "overdue")
    assert instance["completedAt"] is not None
    assert instance["openElements"] == []
    assert instance["timers"] == []
    [completed] = await _events(client, key, "process.completed")
    assert completed["payload"]["outcome"] == "overdue"
    # The signature nobody gave is cancelled by the process, not left open.
    assert (await _task(client, key, sign["taskId"]))["systemStatusCategory"] == (
        "terminal_cancelled"
    )

    # The decision journal: every input with its decisions and their reasons.
    journal = await _journal(client, key, instance["id"])
    inputs = [e for e in journal if e["kind"] == "input"]
    assert [e["data"]["input"]["kind"] for e in inputs] == [
        "start",
        "start",
        "task",
        "event",
        "timer",
        "timer",
        "timer",
    ]
    assert [e["seq"] for e in inputs] == [0, 1, 2, 3, 4, 5, 6]
    assert all(e["eventId"] for e in inputs[:4])
    timers = await _journal(client, key, instance["id"], kind="timer")
    assert {e["data"]["decision"] for e in timers} >= {
        "timer_set",
        "timer_rescheduled",
        "timer_fired",
        "escalated",
    }
    errors = await _journal(client, key, instance["id"], kind="error")
    assert any(e["data"]["decision"] == "error_raised" for e in errors)

    # A redelivered batch changes nothing: every input was taken once.
    written = len(await _events(client, key, "process.correlated"))
    assert written == 2, "the repeated start and the moved deadline"
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 WHERE name = :n"),
            {"n": "processes"},
        )
    await worker.run_once()
    assert len(await _journal(client, key, instance["id"], kind="input")) == len(inputs)
    assert len(await _events(client, key, "process.correlated")) == written


async def test_a_task_the_step_waits_for_is_answered_only_by_its_instance(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    for number in ("A", "B"):
        await _observe(client, key, "sample.opened", number=number, deadline=deadline)
    await worker.run_once()
    by_key = {i["instanceKey"]: i for i in await _instances(client, key)}
    assert set(by_key) == {"A", "B"}

    await _complete(client, key, _open(by_key["A"], "review")["taskId"], {"decision": "no"})
    await worker.run_once()
    a = await _instance(client, key, by_key["A"]["id"])
    b = await _instance(client, key, by_key["B"]["id"])
    assert a["data"]["decision"] == "no"
    assert [e["id"] for e in a["openElements"]] == ["sign"]
    assert "decision" not in b["data"]
    assert [e["id"] for e in b["openElements"]] == ["review"]


# --- operator commands ------------------------------------------------------------------


async def test_suspend_freezes_timers_resume_restores_them_cancel_closes_the_work(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    await _observe(client, key, "sample.opened", number="S-2", deadline=deadline)
    await worker.run_once()
    [instance] = await _instances(client, key)
    await _complete(client, key, _open(instance, "review")["taskId"], {"decision": "yes"})
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    sign_task = _open(instance, "sign")["taskId"]
    path = f"/api/v1/process-instances/{instance['id']}"

    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    assert (await client.get(path, headers=auth(reader))).status_code == 200
    denied = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(reader))
    assert denied.status_code == 403, denied.text

    suspended = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(key))
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["status"] == "suspended"
    assert {t["state"] for t in body["timers"]} == {"frozen"}
    assert all(t["dueAt"] is None for t in body["timers"])
    again = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(key))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_process_instance_state"

    resumed = await client.post(f"{path}:resume", json={}, headers=auth(key))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"
    assert {t["state"] for t in resumed.json()["timers"]} == {"pending"}
    assert all(t["dueAt"] for t in resumed.json()["timers"])

    cancelled = await client.post(
        f"{path}:cancel", json={"reason": "withdrawn", "compensate": False}, headers=auth(key)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["timers"] == []
    [event] = await _events(client, key, "process.cancelled")
    assert event["payload"]["reason"] == "withdrawn"
    assert event["actorId"] == s["agent"]
    assert (await _task(client, key, sign_task))["systemStatusCategory"] == "terminal_cancelled"
    journal = await _journal(client, key, instance["id"], kind="input")
    assert [e["data"]["input"]["body"]["action"] for e in journal[-3:]] == [
        "suspend",
        "resume",
        "cancel",
    ]
    assert journal[-1]["actorId"] == s["admin"], "the operator stands behind the command"

    closed = await client.post(f"{path}:resume", json={}, headers=auth(key))
    assert closed.status_code == 409
    missing = await client.get(f"/api/v1/process-instances/{uuid.uuid4()}", headers=auth(key))
    assert missing.status_code == 404


# --- explicit start and a standing goal (TAI-ADR-0055) ------------------------------


async def test_an_instance_started_explicitly_is_one_per_key(client: httpx.AsyncClient) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-goal", GOAL)
    body = {"process": "sample-goal", "key": "goal-1", "data": {"open": 2}}

    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    denied = await client.post("/api/v1/process-instances", json=body, headers=auth(reader))
    assert denied.status_code == 403, denied.text

    created = await client.post("/api/v1/process-instances", json=body, headers=auth(key))
    assert created.status_code == 201, created.text
    instance = created.json()
    assert (instance["instanceKey"], instance["status"], instance["data"]) == (
        "goal-1",
        "running",
        {"open": 2},
    )
    assert instance["stages"] == [{"id": "watch", "state": "active"}]
    [started] = await _events(client, key, "process.started")
    assert started["payload"]["triggerType"] == "command"
    assert started["payload"]["triggerEventId"] is None

    repeated = await client.post("/api/v1/process-instances", json=body, headers=auth(key))
    assert repeated.status_code == 409, repeated.text
    error = repeated.json()["error"]
    assert error["code"] == "process_instance_exists"
    assert error["details"]["instanceId"] == instance["id"]
    assert len(await _instances(client, key)) == 1

    invalid = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-2", "data": {"open": "many"}},
        headers=auth(key),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_process_data"
    unknown = await client.post(
        "/api/v1/process-instances", json={"process": "nothing", "key": "k"}, headers=auth(key)
    )
    assert unknown.status_code == 404


async def test_an_instance_without_deadlines_is_none_and_out_of_the_sla_filter(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0078 §6: the projection has the fields; a process without due has no deadline."""
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-goal", GOAL)
    created = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-sla", "data": {"open": 1}},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    instance = created.json()
    assert (instance["sla"], instance["slaState"]) == (None, "none")
    assert len(await _instances(client, key)) == 1
    assert await _instances(client, key, slaState="breached") == []
    assert await _instances(client, key, slaState="warning") == []
    invalid = await client.get(
        "/api/v1/process-instances", params={"slaState": "paused"}, headers=auth(key)
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_request"


async def test_a_standing_goal_reaches_loses_and_reaches_its_milestone_again(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-goal", GOAL)
    created = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-1", "data": {"open": 2}},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text

    for open_items in (0, 3, 0):
        await _observe(client, key, "sample.count", goal="goal-1", open=open_items)
        await worker.run_once()

    reached = await _events(client, key, "process.milestone_reached")
    lost = await _events(client, key, "process.milestone_lost")
    assert [e["payload"]["milestone"] for e in reached] == ["clear", "clear"]
    assert [(e["payload"]["milestone"], e["payload"]["stage"]) for e in lost] == [
        ("clear", "watch")
    ]
    assert reached[0]["sequence"] < lost[0]["sequence"] < reached[1]["sequence"]
    instance = await _instance(client, key, created.json()["id"])
    assert instance["status"] == "running"
    milestones = await _journal(client, key, instance["id"], kind="milestone")
    assert [e["data"]["decision"] for e in milestones] == [
        "milestone_reached",
        "milestone_lost",
        "milestone_reached",
    ]


# --- approvals ---------------------------------------------------------------------------


def _vote(approvers: list[str], **approve: Any) -> dict[str, Any]:
    return _vote_by([{"principal": p} for p in approvers], **approve)


def _vote_by(
    approvers: list[dict[str, str]], owner: list[dict[str, str]] | None = None, **approve: Any
) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Sample vote",
        "identity": {"agent": AGENT},
        "owner": owner or [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string"},
                "author": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "sample.vote"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "s",
                "steps": [
                    {
                        "id": "vote",
                        "approve": {
                            "approvers": approvers,
                            **approve,
                        },
                        "output": {"as": {"verdict": "step.result.outcome"}},
                    },
                    {"id": "done", "complete": {"outcome": "decided"}},
                ],
            }
        ],
    }


async def _deciders(client: httpx.AsyncClient, key: str) -> list[tuple[str, str]]:
    out = []
    for name in ("first", "second"):
        principal, decider_key = await create_agent_with_key(
            client, key, name=name, permissions=["approvals.decide", "approvals.read"]
        )
        out.append((principal["id"], decider_key))
    return out


async def _start(
    client: httpx.AsyncClient, key: str, process: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/process-instances",
        json={"process": process, "key": "V-1", "data": data},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_the_first_approval_of_quorum_any_decides_and_closes_the_rest(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (first, first_key), (second, _) = await _deciders(client, key)
    await _publish(client, key, "sample-vote", _vote([first, second], quorum="any"))
    instance = await _start(client, key, "sample-vote")
    approval_ids = _open(instance, "vote")["approvalIds"]
    assert len(approval_ids) == 2
    approvals = {}
    for approval_id in approval_ids:
        response = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))
        assert response.status_code == 200, response.text
        approvals[response.json()["assignedPrincipalId"]] = response.json()
    assert set(approvals) == {first, second}
    assert all(a["requestedByPrincipalId"] == s["agent"] for a in approvals.values())

    decided = await client.post(
        f"/api/v1/approvals/{approvals[first]['id']}:approve", json={}, headers=auth(first_key)
    )
    assert decided.status_code == 200, decided.text
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["outcome"]) == ("completed", "decided")
    assert instance["data"]["verdict"] == "approved"
    other = await client.get(f"/api/v1/approvals/{approvals[second]['id']}", headers=auth(key))
    assert other.json()["status"] == "cancelled"
    votes = await _journal(client, key, instance["id"], kind="vote")
    assert [e["data"]["decision"] for e in votes] == ["vote", "approval_decided"]
    assert votes[0]["actorId"] == first


async def test_a_sequential_step_asks_the_next_approver_after_each_vote(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (first, first_key), (second, second_key) = await _deciders(client, key)
    await _publish(
        client, key, "sample-vote", _vote([first, second], quorum="all", mode="sequential")
    )
    instance = await _start(client, key, "sample-vote")
    [approval_id] = _open(instance, "vote")["approvalIds"]
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:approve", json={}, headers=auth(first_key)
    )
    assert response.status_code == 200, response.text
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    assert instance["status"] == "running"
    ids = _open(instance, "vote")["approvalIds"]
    assert len(ids) == 2
    [next_id] = [i for i in ids if i != approval_id]
    response = await client.post(
        f"/api/v1/approvals/{next_id}:approve", json={}, headers=auth(second_key)
    )
    assert response.status_code == 200, response.text
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def _role_holders(client: httpx.AsyncClient, key: str, *holders: str) -> str:
    role = await create_role(client, key, "approver")
    for holder in holders:
        await assign_role(client, key, holder, role["id"])
    role_id: str = role["id"]
    return role_id


async def _decide(
    client: httpx.AsyncClient, decider_key: str, approval_id: str, action: str = "approve"
) -> httpx.Response:
    return await client.post(
        f"/api/v1/approvals/{approval_id}:{action}", json={}, headers=auth(decider_key)
    )


async def _attention(client: httpx.AsyncClient, key: str) -> set[tuple[str, str, str]]:
    response = await client.get("/api/v1/me/attention", headers=auth(key))
    assert response.status_code == 200, response.text
    return {
        (item["rule"], item["reasonCode"], item["entity"]["id"])
        for item in response.json()["items"]
    }


async def test_separation_of_duties_refuses_the_excluded_holder_and_takes_another(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """The author holds the role but cannot decide on any path; another holder can
    (CP-ADR-0074 §7, TASK-001230: the step no longer fails ``not_implemented``)."""
    s = await _setup(client)
    key = s["key"]
    (author, author_key), (other, other_key) = await _deciders(client, key)
    await _role_holders(client, key, author, other)
    spec = _vote_by([{"role": "approver"}], quorum="any", separationOfDuties="[data.author]")
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author})
    assert instance["status"] == "running"
    [approval_id] = _open(instance, "vote")["approvalIds"]
    approval = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))
    assert approval.json()["excludedPrincipals"] == [author]
    requests = await _journal(client, key, instance["id"], kind="intent")
    [request] = [e for e in requests if e["data"]["intent"] == "request_approvals"]
    assert request["data"]["executed"] == {"ok": True, "approvalIds": [approval_id]}

    for action in ("approve", "reject"):
        refused = await _decide(client, author_key, approval_id, action)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "separation_of_duties_violation"
    # Neither is the approval on the author's list; it is on the other holder's.
    assert all(entry[2] != approval_id for entry in await _attention(client, author_key))
    assert ("approval.decide@1", "decision_role", approval_id) in await _attention(
        client, other_key
    )
    await worker.run_once()
    assert (await _instance(client, key, instance["id"]))["status"] == "running"

    decided = await _decide(client, other_key, approval_id)
    assert decided.status_code == 200, decided.text
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def test_the_exclusion_holds_for_the_next_approvers_of_a_sequential_step(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (author, author_key), (other, other_key) = await _deciders(client, key)
    await _role_holders(client, key, author, other)
    spec = _vote_by(
        [{"role": "approver"}, {"role": "approver"}],
        quorum="all",
        mode="sequential",
        separationOfDuties="[data.author]",
    )
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author})
    [first_id] = _open(instance, "vote")["approvalIds"]
    assert (await _decide(client, other_key, first_id)).status_code == 200
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    [next_id] = [i for i in _open(instance, "vote")["approvalIds"] if i != first_id]
    approval = await client.get(f"/api/v1/approvals/{next_id}", headers=auth(key))
    assert approval.json()["excludedPrincipals"] == [author]
    refused = await _decide(client, author_key, next_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "separation_of_duties_violation"
    assert (await _decide(client, other_key, next_id)).status_code == 200
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def test_an_approval_nobody_may_decide_is_created_and_raised_to_the_owner(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """The only holder of the role is excluded: no ``failed``; the process owner sees
    ``approval.undecidable`` until someone else holds the role (CP-ADR-0074 §7)."""
    s = await _setup(client)
    key = s["key"]
    (author, author_key), (other, other_key) = await _deciders(client, key)
    owner, owner_key = await create_agent_with_key(
        client, key, name="owner", permissions=["approvals.read", "tasks.read"]
    )
    role_id = await _role_holders(client, key, author)
    spec = _vote_by(
        [{"role": "approver"}],
        owner=[{"principal": owner["id"]}],
        quorum="all",
        separationOfDuties="[data.author]",
    )
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author})
    assert instance["status"] == "running"
    [approval_id] = _open(instance, "vote")["approvalIds"]

    response = await client.get("/api/v1/me/attention", headers=auth(owner_key))
    [item] = [i for i in response.json()["items"] if i["entity"]["id"] == approval_id]
    assert (item["rule"], item["kind"], item["reasonCode"]) == (
        "approval.undecidable@1",
        "undecidable",
        "undecidable_process_owner",
    )
    assert item["details"]["excludedPrincipals"] == [author]
    assert item["details"]["processInstanceId"] == instance["id"]
    assert [a["action"] for a in item["actions"]] == ["open", "openProcess", "feedback"]
    # Nobody else is told: not the excluded holder, not an unrelated principal.
    assert all(entry[2] != approval_id for entry in await _attention(client, author_key))
    assert all(entry[2] != approval_id for entry in await _attention(client, other_key))
    refused = await _decide(client, author_key, approval_id)
    assert refused.status_code == 403, refused.text

    await assign_role(client, key, other, role_id)
    assert all(entry[2] != approval_id for entry in await _attention(client, owner_key))
    assert (await _decide(client, other_key, approval_id)).status_code == 200
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def test_an_excluded_approver_or_a_value_that_is_no_principal_fails_the_step(
    client: httpx.AsyncClient,
) -> None:
    """Not dropped silently (CP-ADR-0074 §7): nobody could decide such a step, and a
    value the core cannot enforce is refused — ``intent_failed``, nothing requested."""
    s = await _setup(client)
    key = s["key"]
    (first, _), (second, _) = await _deciders(client, key)
    cases = {
        "excluded-approver": (
            _vote(
                [second, first],
                quorum="all",
                mode="sequential",
                separationOfDuties=f"['{first}']",
            ),
            [first],
        ),
        "not-a-principal": (
            _vote([first], quorum="all", separationOfDuties="['uploader@example.test']"),
            ["uploader@example.test"],
        ),
        # An agent approver is resolved to its principal before anything is
        # asked for: a later approver of a sequential step too.
        "excluded-agent": (
            _vote_by(
                [{"principal": second}, {"agent": AGENT}],
                quorum="all",
                mode="sequential",
                separationOfDuties=f"['{s['agent']}']",
            ),
            [s["agent"]],
        ),
        # An empty value is no exclusion to drop (review of 2026-09-30): the
        # uploader the data should have named is unknown, nobody may decide.
        "missing-value": (
            _vote([first], quorum="all", separationOfDuties="[data.author]"),
            [None],
        ),
        "empty-value": (
            _vote([first], quorum="all", separationOfDuties="['']"),
            [""],
        ),
    }
    for process, (spec, excluded) in cases.items():
        await _publish(client, key, process, spec)
        instance = await _start(client, key, process)
        assert instance["status"] == "failed", process
        intents = await _journal(client, key, instance["id"], kind="intent")
        [request] = [e for e in intents if e["data"]["intent"] == "request_approvals"]
        assert request["data"]["executed"]["code"] == "invalid_approval", process
        assert request["data"]["excludedPrincipals"] == excluded, process
    failures = await _events(client, key, "process.failed")
    assert len(failures) == len(cases)
    assert {f["payload"]["error"]["type"] for f in failures} == {"intent_failed"}
    approvals = await client.get("/api/v1/approvals", headers=auth(key))
    assert approvals.json()["items"] == []


async def test_nobody_but_an_eligible_decider_cancels_an_approval_with_exclusions(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """A step counts a cancelled approval as one approver fewer: cancelling a
    foreign one is a decision (review of 2026-09-30). The author, whatever
    ``approvals.manage`` it has, cannot void the accounting approval and let the
    director alone pass the case."""
    s = await _setup(client)
    key = s["key"]
    author, author_key = await create_agent_with_key(
        client,
        key,
        name="author",
        permissions=["approvals.decide", "approvals.read", "approvals.manage"],
    )
    (accountant, accountant_key), (director, director_key) = await _deciders(client, key)
    _, bystander_key = await create_agent_with_key(
        client, key, name="bystander", permissions=["approvals.read", "approvals.manage"]
    )
    await _role_holders(client, key, author["id"], accountant)
    spec = _vote_by(
        [{"role": "approver"}, {"principal": director}],
        quorum="all",
        separationOfDuties="[data.author]",
    )
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author["id"]})
    approvals = {}
    for approval_id in _open(instance, "vote")["approvalIds"]:
        approval = (await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))).json()
        approvals["role" if approval["requiredRoleId"] else "director"] = approval_id

    refused = await _decide(client, author_key, approvals["role"], "cancel")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "separation_of_duties_violation"
    # Without the right to decide it, a manager cannot void it either.
    refused = await _decide(client, bystander_key, approvals["role"], "cancel")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] != "separation_of_duties_violation"
    role = await client.get(f"/api/v1/approvals/{approvals['role']}", headers=auth(key))
    assert role.json()["status"] == "pending"

    assert (await _decide(client, director_key, approvals["director"])).status_code == 200
    await worker.run_once()
    assert (await _instance(client, key, instance["id"]))["status"] == "running"
    assert (await _decide(client, accountant_key, approvals["role"])).status_code == 200
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def test_an_agent_acting_for_an_excluded_principal_cannot_decide(
    client: httpx.AsyncClient,
) -> None:
    """The exclusion holds for whoever the excluded principal delegated to: its
    agent holds the role, yet neither decides nor cancels (CP-ADR-0074 §7)."""
    s = await _setup(client)
    key = s["key"]
    author, _ = await create_agent_with_key(client, key, name="author", kind="human")
    helper, helper_key = await create_agent_with_key(
        client,
        key,
        name="helper",
        permissions=["approvals.decide", "approvals.read", "approvals.manage"],
    )
    (other, _), _ = await _deciders(client, key)
    await _role_holders(client, key, author["id"], helper["id"], other)
    response = await client.post(
        "/api/v1/delegations",
        json={
            "humanPrincipalId": author["id"],
            "agentPrincipalId": helper["id"],
            "permissions": ["approvals.decide"],
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    spec = _vote_by([{"role": "approver"}], quorum="any", separationOfDuties="[data.author]")
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author["id"]})
    [approval_id] = _open(instance, "vote")["approvalIds"]

    for action in ("approve", "reject", "cancel"):
        refused = await _decide(client, helper_key, approval_id, action)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "separation_of_duties_violation"
        assert refused.json()["error"]["details"]["delegationId"] == response.json()["id"]
    # Revoked, the delegation no longer ties the agent to the author.
    revoked = await client.post(
        f"/api/v1/delegations/{response.json()['id']}:revoke", headers=auth(key)
    )
    assert revoked.status_code == 200, revoked.text
    assert (await _decide(client, helper_key, approval_id)).status_code == 200


async def test_without_an_owner_the_starter_hears_that_nobody_may_decide(
    client: httpx.AsyncClient,
) -> None:
    """``spec.owner`` resolves to nobody (no ``lead`` role): the signal goes to
    whoever started the instance instead of to no one (review of 2026-09-30)."""
    s = await _setup(client)
    key = s["key"]
    (author, _), (_, other_key) = await _deciders(client, key)
    await _role_holders(client, key, author)
    spec = _vote_by([{"role": "approver"}], quorum="all", separationOfDuties="[data.author]")
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote", {"author": author})
    [approval_id] = _open(instance, "vote")["approvalIds"]
    assert ("approval.undecidable@1", "undecidable_process_starter", approval_id) in (
        await _attention(client, key)
    )
    assert all(entry[2] != approval_id for entry in await _attention(client, other_key))


# --- human.customFields: the task filled from the case (amendment 2026-10-01) -------------

PURCHASE_FIELDS = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string"},
        "amount": {"type": "integer", "maximum": 1000},
        "note": {"type": "string"},
    },
    "required": ["supplier", "amount"],
}


def _purchase(admin: str) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Sample purchase",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string"},
                "amount": {"type": "integer"},
                "note": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "sample.purchase"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "s",
                "steps": [
                    {
                        "id": "check",
                        "human": {
                            "taskType": "purchase",
                            "assign": [{"principal": admin}],
                            "customFields": {
                                "supplier": "data.supplier",
                                "amount": "data.amount",
                            },
                        },
                        "output": {
                            "as": {"note": "step.result.note", "amount": "step.result.amount"}
                        },
                    },
                    {"id": "done", "complete": {"outcome": "checked"}},
                ],
            }
        ],
    }


async def test_a_human_step_fills_the_task_from_the_case(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={"key": "purchase", "displayName": "Purchase", "fieldSchema": PURCHASE_FIELDS},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    await _publish(client, key, "sample-purchase", _purchase(s["admin"]))

    instance = await _start(client, key, "sample-purchase", {"supplier": "Acme", "amount": 300})
    assert instance["status"] == "running", instance
    check = _open(instance, "check")
    task = await _task(client, key, check["taskId"])
    # The person does not retype what the case already knows.
    assert task["customFields"] == {"supplier": "Acme", "amount": 300}

    # What the person enters is added to what the step filled in.
    await _complete(client, key, task["id"], {**task["customFields"], "note": "fine"})
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert instance["status"] == "completed"
    assert (instance["data"]["note"], instance["data"]["amount"]) == ("fine", 300)


async def test_fields_the_type_refuses_fail_the_step_and_file_nothing(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key = s["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={"key": "purchase", "displayName": "Purchase", "fieldSchema": PURCHASE_FIELDS},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    # Over the maximum of the type, and a required field the case does not know:
    # a null leaves the field out, and the type requires it.
    cases = {
        "over-maximum": {"supplier": "Acme", "amount": 5000},
        "unknown-supplier": {"amount": 10},
    }
    for process, data in cases.items():
        await _publish(client, key, process, _purchase(s["admin"]))
        instance = await _start(client, key, process, data)
        assert instance["status"] == "failed", data
        intents = await _journal(client, key, instance["id"], kind="intent")
        [create] = [e for e in intents if e["data"]["intent"] == "create_task"]
        assert create["data"]["executed"]["code"] == "custom_fields_invalid", create
    tasks = await client.get("/api/v1/tasks", params={"typeKey": "purchase"}, headers=auth(key))
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["items"] == []
