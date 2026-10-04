"""Closing the task an observation is bound to (CP-ADR-0063, amendment integrations-connections).

What is under test (I013):

*Zh1/Zh3 — the fact names the work.* ``complete_work`` / ``cancel_work`` with
``target: task`` close the task of the triggering observation
(``payload.taskId``) — filed by a process, by another rule or by a person —
without a dedup key shared with whoever filed it. Completing goes through the
verification stage; the evidence of the closing is the observation.

*Zh3 — refusals.* A type outside ``taskTypes`` and an observation bound to no
task are skipped; a task the rule cannot see is ``bound_task_not_found``, one
it may not write (its workspace) ``bound_task_forbidden``.

*Idempotency (the review of I024).* A second observation of the same task —
another fact, another dedup key at the source — does not close it again:
done or closed work is skipped (``already_done`` / ``already_closed``) with no
evidence written and no new attempt; while the attempt is open, the repeated
fact is not added to it.

*Zh3 — under a claim.* The decision waits for the claim like a closing on a
key does, and the checks are made again when it applies.

*Zh4 — the trail.* The work item, ``rule.evaluated`` and ``work.reconciled``
carry ``target: task`` and the pseudo-key ``task:<id>``; nothing is written
to ``rule_work_items``.

Observation kinds and task types are neutral (``sample.*``): core knows no domain.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.commands.observations import record_observation
from control_plane.config import Settings
from control_plane.domain.errors import AuthorizationError
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)

CLOSED = "sample.closed"
REVIEW = "sample-review"
OTHER = "sample-other"
AGENT = "sample-process"
RULE_AGENT = "sample-rules"
ISSUER = "https://iam.example.test"
AGENT_PERMISSIONS = [
    "approvals.manage",
    "events.read",
    "skills.invoke",
    "tasks.read",
    "tasks.write",
]
RULE_PERMISSIONS = ["events.read", "tasks.read", "tasks.write"]
RUNNER_PERMISSIONS = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]


def _closing(kind: str = "complete_work", *, actor: str, **action: Any) -> dict[str, Any]:
    return {
        "key": f"sample-{kind.replace('_', '-')}",
        # Zh6: a rule closing the task a fact names says whose facts it trusts.
        "trigger": {"kind": "observation", "type": CLOSED, "actorId": actor},
        "action": {"kind": kind, "target": "task", "taskTypes": [REVIEW], **action},
    }


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def _admin(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    for type_key, extra in (
        (REVIEW, {}),
        (OTHER, {}),
        # Two facts to wait for: its attempt stays open after the rule's one.
        (
            "sample-signed",
            {
                "acceptance": [
                    {"key": "closed", "kind": "external_state", "description": "Closed"},
                    {"key": "signed", "kind": "external_state", "description": "Signed"},
                ]
            },
        ),
    ):
        response = await client.post(
            "/api/v1/task-types",
            json={
                "key": type_key,
                "displayName": type_key,
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
                **extra,
            },
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
    return {"key": key, "admin": boot["adminPrincipal"]["id"], "tenant": boot["tenant"]["id"]}


async def _rule(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/v1/rules", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    rule: dict[str, Any] = response.json()
    return rule


async def _observe(
    client: httpx.AsyncClient, key: str, task: str | None, kind: str = CLOSED, **data: Any
) -> str:
    response = await client.post(
        "/api/v1/observations",
        json={
            "kind": kind,
            "content": f"{kind} seen",
            "data": data,
            **({"task": task} if task else {}),
        },
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _evaluations(client: httpx.AsyncClient, key: str, rule_id: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/rules/{rule_id}/evaluations", params={"limit": 100}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


async def _verifications(client: httpx.AsyncClient, key: str, ref: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _events(sync_engine: Engine, event_type: str, entity_id: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM events WHERE event_type = :type "
                "AND entity_id = CAST(:entity AS uuid) ORDER BY tx_id, sequence"
            ),
            {"type": event_type, "entity": entity_id},
        ).all()
    return [dict(row.payload) for row in rows]


def _count(sync_engine: Engine, sql: str) -> int:
    with sync_engine.connect() as conn:
        count: int = conn.execute(text(sql)).scalar_one()
    return count


def _make_waiting_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE rule_evaluations SET next_check_at = now() WHERE status = 'waiting'")
        )


async def _link_agent(client: httpx.AsyncClient, admin_key: str, agent: str) -> str:
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    response = await client.put(
        f"/api/v1/agents/{agent}/identity",
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


async def _publish_agent(
    client: httpx.AsyncClient, key: str, agent: str, permissions: list[str]
) -> None:
    response = await client.post(
        "/api/v1/agents",
        json={
            "key": agent,
            "spec": {
                "displayName": agent,
                "identity": {"kind": "service", "permissions": permissions},
                "placement": "none",
            },
        },
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


# --- the fact names the work --------------------------------------------------------


async def test_an_observation_closes_the_task_a_process_filed(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The acceptance of I013: a process's task, done and verified on the fact."""
    s = await _admin(client)
    key = s["key"]
    await _publish_agent(client, key, AGENT, AGENT_PERMISSIONS)
    await _link_agent(client, key, AGENT)
    published = await client.post(
        "/api/v1/process-definitions",
        json={
            "key": "sample-review",
            "spec": {
                "version": 1,
                "displayName": "Sample review",
                "identity": {"agent": AGENT},
                "owner": [{"role": "lead"}],
                "data": {"type": "object", "properties": {"number": {"type": "string"}}},
                "start": {
                    "on": {"observation": "sample.opened"},
                    "key": "event.payload.data.number",
                },
                "stages": [
                    {
                        "id": "work",
                        "steps": [
                            {
                                "id": "review",
                                "human": {
                                    "taskType": REVIEW,
                                    "assign": [{"principal": s["admin"]}],
                                },
                            },
                            {"id": "done", "complete": {"outcome": "reviewed"}},
                        ],
                    }
                ],
            },
        },
        headers=auth(key),
    )
    assert published.status_code == 201, published.text
    closing = await _rule(client, key, _closing(actor=s["admin"]))

    await _observe(client, key, None, kind="sample.opened", number="S-1")
    await worker.run_once()
    response = await client.get(
        "/api/v1/process-instances", params={"definitionKey": "sample-review"}, headers=auth(key)
    )
    [instance] = response.json()["items"]
    [review] = [e for e in instance["openElements"] if e["id"] == "review"]
    task_id = review["taskId"]
    assert (await _task(client, key, task_id))["origin"]["kind"] == "process"

    closed = await _observe(client, key, task_id, number="S-1")
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert (task["status"], task["systemStatusCategory"]) == ("done", "terminal_success")
    # The evidence of the closing is the observation (Art. VII).
    assert task["evidence"] == [
        {"kind": "observation", "observationId": closed, "check": "rule-evidence"}
    ]
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched", evaluation
    [work] = evaluation["result"]["work"]
    assert (work["target"], work["dedupKey"], work["taskId"], work["check"]) == (
        "task",
        f"task:{task_id}",
        task_id,
        "rule-evidence",
    )
    [attempt] = await _verifications(client, key, task_id)
    assert (attempt["status"], attempt["trigger"], attempt["triggerRef"]) == (
        "passed",
        "rule",
        f"rule_evaluation:{evaluation['id']}",
    )
    assert [(r["key"], r["status"]) for r in attempt["results"]] == [("rule-evidence", "passed")]
    # Zh4: the trail names the target and the pseudo-key; no key is recorded.
    [reconciled] = _events(sync_engine, "work.reconciled", task_id)
    assert (reconciled["target"], reconciled["dedupKey"], reconciled["action"]) == (
        "task",
        f"task:{task_id}",
        "complete_work",
    )
    [evaluated] = _events(sync_engine, "rule.evaluated", closing["id"])
    assert evaluated["work"] == [
        {"dedupKey": f"task:{task_id}", "target": "task", "taskId": task_id}
    ]
    assert _count(sync_engine, "SELECT count(*) FROM rule_work_items") == 0

    # The process goes on from the task it filed.
    await worker.run_once()
    response = await client.get(f"/api/v1/process-instances/{instance['id']}", headers=auth(key))
    assert response.json()["status"] == "completed", response.json()


async def test_an_observation_cancels_the_work_another_rule_filed(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The closing rule shares no dedup key with the rule that filed the work."""
    s = await _admin(client)
    key = s["key"]
    await _rule(
        client,
        key,
        {
            "key": "sample-appeared",
            "trigger": {"kind": "observation", "type": "sample.appeared"},
            "action": {
                "kind": "ensure_work",
                "taskType": REVIEW,
                "dedupKeyTemplate": "sample:{{payload.data.id}}",
                "fields": {"title": "Sample {{payload.data.id}}"},
            },
        },
    )
    closing = await _rule(client, key, _closing("cancel_work", actor=s["admin"]))
    await _observe(client, key, None, kind="sample.appeared", id="a")
    await worker.run_once()
    [(task_id, public_id)] = [
        (str(row.id), row.public_id)
        for row in sync_engine.connect().execute(text("SELECT id, public_id FROM tasks")).all()
    ]

    # Bound by the public id, as a connector that keeps it as its external ref.
    closed = await _observe(client, key, public_id)
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert task["systemStatusCategory"] == "terminal_cancelled"
    assert task["evidence"] == [{"kind": "observation", "observationId": closed}]
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched"
    assert evaluation["result"]["work"][0]["changes"] == ["evidence", "status"]
    assert await _verifications(client, key, task_id) == []
    assert _count(sync_engine, "SELECT count(*) FROM rule_work_items") == 1


# --- idempotency: a second fact about the same task ------------------------------------


async def test_a_second_observation_of_done_work_is_skipped(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing(actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    first = await _observe(client, key, task["id"], id="deal-1", dedup="first")
    await worker.run_once()
    done = await _task(client, key, task["id"])
    assert done["status"] == "done"

    # Reconciled, reopened and closed again at the source: another fact.
    await _observe(client, key, task["id"], id="deal-1", dedup="second")
    await worker.run_once()
    latest, _ = await _evaluations(client, key, closing["id"])
    assert latest["status"] == "skipped", latest
    assert latest["error"] is None
    assert latest["result"]["skipped"] == {"reason": "already_done", "taskId": task["id"]}
    [work] = latest["result"]["work"]
    assert (work["skipped"], work["reason"], work["target"]) == (True, "already_done", "task")
    after = await _task(client, key, task["id"])
    assert after["evidence"] == [
        {"kind": "observation", "observationId": first, "check": "rule-evidence"}
    ]
    assert after["version"] == done["version"]
    assert len(await _verifications(client, key, task["id"])) == 1
    assert len(_events(sync_engine, "work.reconciled", task["id"])) == 1


async def test_a_second_observation_of_cancelled_work_is_skipped(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing("cancel_work", actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    await _observe(client, key, task["id"], dedup="first")
    await worker.run_once()
    cancelled = await _task(client, key, task["id"])
    assert cancelled["systemStatusCategory"] == "terminal_cancelled"

    await _observe(client, key, task["id"], dedup="second")
    await worker.run_once()
    latest, _ = await _evaluations(client, key, closing["id"])
    assert latest["status"] == "skipped"
    assert latest["result"]["skipped"]["reason"] == "already_closed"
    assert (await _task(client, key, task["id"]))["version"] == cancelled["version"]


async def test_a_second_observation_adds_no_fact_to_the_open_attempt(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(
        client,
        key,
        _closing(actor=s["admin"], taskTypes=["sample-signed"], check="closed"),
    )
    task = await create_task(client, key, title="Signed", typeKey="sample-signed")
    first = await _observe(client, key, task["id"], dedup="first")
    await worker.run_once()
    [attempt] = await _verifications(client, key, task["id"])
    # Its fact is in; the sign-off is not yet.
    assert attempt["status"] == "waiting_external", attempt["results"]
    handed_in = await _task(client, key, task["id"])

    await _observe(client, key, task["id"], dedup="second")
    await worker.run_once()
    latest, _ = await _evaluations(client, key, closing["id"])
    assert latest["status"] == "skipped", latest
    assert latest["result"]["skipped"]["reason"] == "verification_pending"
    after = await _task(client, key, task["id"])
    assert after["evidence"] == [{"kind": "observation", "observationId": first, "check": "closed"}]
    assert after["version"] == handed_in["version"]
    [same] = await _verifications(client, key, task["id"])
    assert same["id"] == attempt["id"]


async def test_two_facts_in_one_pass_close_the_task_once(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Both evaluations take the task's row in turn: one closes, the other is skipped."""
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing(actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    await _observe(client, key, task["id"], dedup="first")
    await _observe(client, key, task["id"], dedup="second")
    await worker.run_once()
    await worker.run_once()
    statuses = sorted(e["status"] for e in await _evaluations(client, key, closing["id"]))
    assert statuses == ["matched", "skipped"]
    assert (await _task(client, key, task["id"]))["status"] == "done"
    assert len((await _task(client, key, task["id"]))["evidence"]) == 1
    assert len(await _verifications(client, key, task["id"])) == 1
    assert len(_events(sync_engine, "work.reconciled", task["id"])) == 1


# --- refusals ---------------------------------------------------------------------------


async def test_a_type_outside_task_types_is_skipped_and_the_task_left_alone(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing(actor=s["admin"]))
    task = await create_task(client, key, title="Other", typeKey=OTHER)
    await _observe(client, key, task["id"])
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "skipped", evaluation
    assert evaluation["error"] is None
    assert evaluation["result"]["skipped"] == {
        "reason": "bound_task_type_not_listed",
        "taskId": task["id"],
        "typeKey": OTHER,
    }
    after = await _task(client, key, task["id"])
    assert (after["version"], after["evidence"]) == (task["version"], [])


async def test_an_observation_bound_to_no_task_is_skipped(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing(actor=s["admin"]))
    await _observe(client, key, None)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "skipped"
    assert evaluation["result"]["skipped"] == {"reason": "no_bound_task"}
    assert evaluation["result"]["work"] == []


@dataclass
class DenyingPolicy:
    """A PDP that refuses exactly the listed (action, resource) pairs."""

    deny: set[tuple[str, str]]
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):  # type: ignore[no-untyped-def]
        self.calls.append((action, resource.key))
        allowed = (action, resource.key) not in self.deny
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(self, ctx, action, resource_type, **kwargs):  # type: ignore[no-untyped-def]
        return ObjectPage(objects=[], cursor=None, model_version="1")


async def test_a_task_the_rule_may_not_see_or_write_fails_with_a_code(
    client: httpx.AsyncClient, settings: Settings, restore_authorizer: None
) -> None:
    """The rule's identity decides, per task, as for anyone (Zh3 p.2 and p.4).

    The PDP stands for the workspace of the task: the rule's agent may read
    the one task but not write it, and may not even see the other.
    """
    s = await _admin(client)
    key = s["key"]
    await _publish_agent(client, key, RULE_AGENT, RULE_PERMISSIONS)
    await _link_agent(client, key, RULE_AGENT)
    closing = await _rule(
        client, key, {**_closing(actor=s["admin"]), "identity": {"agent": RULE_AGENT}}
    )
    elsewhere = await create_task(client, key, title="Elsewhere", typeKey=REVIEW)
    hidden = await create_task(client, key, title="Hidden", typeKey=REVIEW)
    policy = DenyingPolicy(
        deny={
            ("tasks.write", f"task:{elsewhere['id']}"),
            ("tasks.read", f"task:{hidden['id']}"),
        }
    )
    worker = Worker(settings, authorizer=Authorizer(policy, "policy"))
    try:
        await _observe(client, key, elsewhere["id"])
        await worker.run_once()
        [evaluation] = await _evaluations(client, key, closing["id"])
        assert evaluation["status"] == "failed", evaluation
        assert evaluation["error"]["code"] == "bound_task_forbidden"
        assert evaluation["error"]["details"] == {
            "taskId": elsewhere["id"],
            "workspaceId": None,
            "permission": "tasks.write",
        }

        await _observe(client, key, hidden["id"])
        await worker.run_once()
        latest, _ = await _evaluations(client, key, closing["id"])
        assert latest["status"] == "failed", latest
        # Unseen is not told apart from missing.
        assert latest["error"]["code"] == "bound_task_not_found"
        assert latest["error"]["details"] == {"taskId": hidden["id"]}
        assert "Hidden" not in str(latest)
    finally:
        await worker.aclose()
    for task in (elsewhere, hidden):
        after = await _task(client, key, task["id"])
        assert (after["version"], after["evidence"]) == (task["version"], [])


# --- under a claim ----------------------------------------------------------------------


async def _run_on(
    client: httpx.AsyncClient, admin_key: str, task_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    _, runner_key = await create_agent_with_key(
        client, admin_key, name="runner", permissions=RUNNER_PERMISSIONS
    )
    session = await open_session(client, runner_key)
    claimed = await claim_task(client, runner_key, task_id, session["id"])
    assert claimed.status_code == 200, claimed.text
    claim: dict[str, Any] = claimed.json()
    started = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(runner_key),
    )
    assert started.status_code in (200, 201), started.text
    run: dict[str, Any] = started.json()
    return runner_key, claim, run


async def test_a_claimed_bound_task_is_closed_once_the_claim_ends(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing("cancel_work", actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    runner_key, claim, run = await _run_on(client, key, task["id"])

    closed = await _observe(client, key, task["id"])
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "waiting"
    [work] = evaluation["result"]["work"]
    assert (work["target"], work["waiting"], work["runId"], work["cancelRequested"]) == (
        "task",
        True,
        run["id"],
        True,
    )

    stopped = await client.post(
        f"/api/v1/runs/{run['id']}:cancel", json={"reason": "asked"}, headers=auth(runner_key)
    )
    assert stopped.status_code == 200, stopped.text
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "cancelled"},
        headers=auth(runner_key),
    )
    assert released.status_code == 200, released.text
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched", evaluation
    [work] = evaluation["result"]["work"]
    assert (work["target"], work["afterClaim"], work["changes"]) == (
        "task",
        True,
        ["evidence", "status"],
    )
    after = await _task(client, key, task["id"])
    assert after["systemStatusCategory"] == "terminal_cancelled"
    assert after["evidence"] == [{"kind": "observation", "observationId": closed}]
    [reconciled] = _events(sync_engine, "work.reconciled", task["id"])
    assert reconciled["target"] == "task"


async def test_a_bound_task_the_executor_finished_while_waiting_is_skipped(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing("cancel_work", actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    runner_key, _, run = await _run_on(client, key, task["id"])
    await _observe(client, key, task["id"])
    await worker.run_once()
    assert (await _evaluations(client, key, closing["id"]))[0]["status"] == "waiting"

    finished = await client.post(
        f"/api/v1/runs/{run['id']}:succeed", json={}, headers=auth(runner_key)
    )
    assert finished.status_code == 200, finished.text
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "skipped", evaluation
    assert evaluation["result"]["skipped"] == {"reason": "already_done", "taskId": task["id"]}
    assert "waitingFor" not in evaluation["result"]
    after = await _task(client, key, task["id"])
    assert (after["status"], after["evidence"]) == ("done", [])
    assert _events(sync_engine, "work.reconciled", task["id"]) == []


async def test_a_decision_waiting_under_an_unfiltered_rule_closes_nothing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """A wait begun before Zh6 is settled by the rule as it is now: skipped."""
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing("cancel_work", actor=s["admin"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    runner_key, claim, run = await _run_on(client, key, task["id"])
    await _observe(client, key, task["id"])
    await worker.run_once()
    assert (await _evaluations(client, key, closing["id"]))[0]["status"] == "waiting"

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE work_rules SET trigger = trigger - 'actorId' WHERE id = :id"),
            {"id": closing["id"]},
        )
    stopped = await client.post(
        f"/api/v1/runs/{run['id']}:cancel", json={"reason": "asked"}, headers=auth(runner_key)
    )
    assert stopped.status_code == 200, stopped.text
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "cancelled"},
        headers=auth(runner_key),
    )
    assert released.status_code == 200, released.text
    before = await _task(client, key, task["id"])
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "skipped", evaluation
    assert evaluation["result"]["skipped"] == {
        "reason": "trigger_author_unfiltered",
        "taskId": task["id"],
    }
    after = await _task(client, key, task["id"])
    assert after["systemStatusCategory"] != "terminal_cancelled"
    assert (after["version"], after["evidence"]) == (before["version"], [])
    assert _events(sync_engine, "work.reconciled", task["id"]) == []


# --- writing the rule --------------------------------------------------------------------


async def test_a_rule_names_only_task_types_that_exist(client: httpx.AsyncClient) -> None:
    s = await _admin(client)
    key = s["key"]
    response = await client.post(
        "/api/v1/rules",
        json=_closing(actor=s["admin"], taskTypes=[REVIEW, "sample-missing"]),
        headers=auth(key),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unknown_task_type"
    assert error["details"]["field"] == "action.taskTypes[1]"

    for body, field_path in (
        (
            {**_closing(actor=s["admin"]), "trigger": {"kind": "event", "type": "task.completed"}},
            "action.target",
        ),
        (_closing(actor=s["admin"], dedupKeyTemplate="k"), "action.dedupKeyTemplate"),
        (_closing(actor=s["admin"], taskTypes=None), "action.taskTypes"),
    ):
        response = await client.post("/api/v1/rules", json=body, headers=auth(key))
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert (error["code"], error["details"]["field"]) == ("invalid_rule_action", field_path)

    created = await _rule(client, key, _closing(actor=s["admin"], check="closed-outside"))
    assert created["action"] == {
        "kind": "complete_work",
        "target": "task",
        "taskTypes": [REVIEW],
        "check": "closed-outside",
    }


# --- Zh6: whose facts close the work -------------------------------------------------------

OBSERVER_PERMISSIONS = ["observations.write", "tasks.read"]


async def test_a_fact_of_another_author_does_not_close_the_task(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The review of I013: a forged closing from any harness closes nothing.

    The source is the author's own word; the author is the journal's. The
    rule also reads the author as ``trigger.actorId`` in its condition.
    """
    s = await _admin(client)
    key = s["key"]
    observer, observer_key = await create_agent_with_key(
        client, key, name="observer", permissions=OBSERVER_PERMISSIONS
    )
    _, stranger_key = await create_agent_with_key(
        client, key, name="stranger", permissions=OBSERVER_PERMISSIONS
    )
    closing = await _rule(
        client,
        key,
        {
            **_closing(actor=observer["id"]),
            "condition": {"eq": [{"var": "trigger.actorId"}, {"const": observer["id"]}]},
        },
    )
    task = await create_task(client, key, title="Review", typeKey=REVIEW)

    await _observe(client, stranger_key, task["id"], dedup="forged")
    await worker.run_once()
    assert await _evaluations(client, key, closing["id"]) == []
    after = await _task(client, key, task["id"])
    assert (after["version"], after["evidence"], after["status"]) == (
        task["version"],
        [],
        task["status"],
    )
    assert await _verifications(client, key, task["id"]) == []

    closed = await _observe(client, observer_key, task["id"], dedup="real")
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched", evaluation
    done = await _task(client, key, task["id"])
    assert done["status"] == "done"
    assert done["evidence"] == [
        {"kind": "observation", "observationId": closed, "check": "rule-evidence"}
    ]


async def test_the_agent_filter_takes_the_facts_of_that_agent_only(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _admin(client)
    key = s["key"]
    await _publish_agent(client, key, AGENT, AGENT_PERMISSIONS)
    agent_principal = await _link_agent(client, key, AGENT)
    issued = await client.post(
        f"/api/v1/principals/{agent_principal}/api-keys",
        json={"permissions": OBSERVER_PERMISSIONS},
        headers=auth(key),
    )
    assert issued.status_code == 201, issued.text
    agent_key = issued.json()["key"]
    rule = _closing(actor=s["admin"])
    rule["trigger"] = {"kind": "observation", "type": CLOSED, "agent": AGENT}
    closing = await _rule(client, key, rule)
    assert closing["trigger"] == {"kind": "observation", "type": CLOSED, "agent": AGENT}
    task = await create_task(client, key, title="Review", typeKey=REVIEW)

    # The admin may do anything but is not the agent the rule trusts.
    await _observe(client, key, task["id"], dedup="admin")
    await worker.run_once()
    assert await _evaluations(client, key, closing["id"]) == []

    await _observe(client, agent_key, task["id"], dedup="agent")
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched", evaluation
    assert (await _task(client, key, task["id"]))["status"] == "done"


async def test_a_key_taken_in_advance_does_not_stop_the_trusted_observer(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The review of TASK-001215: a stranger takes the observer's predictable
    (source, dedupKey) first. The observer's fact is still recorded — the
    author is part of the key (CP-ADR-0057, 2026-10-01) — and the rule closes
    the task on the trusted fact only."""
    s = await _admin(client)
    key = s["key"]
    observer, observer_key = await create_agent_with_key(
        client, key, name="observer", permissions=OBSERVER_PERMISSIONS
    )
    _, stranger_key = await create_agent_with_key(
        client, key, name="stranger", permissions=OBSERVER_PERMISSIONS
    )
    closing = await _rule(client, key, _closing(actor=observer["id"]))
    task = await create_task(client, key, title="Review", typeKey=REVIEW)

    async def report(api_key: str) -> httpx.Response:
        return await client.post(
            "/api/v1/observations",
            json={
                "kind": CLOSED,
                "content": "closed",
                "task": task["id"],
                "source": "sample-tracker",
                "dedupKey": f"{task['publicId']}@closed",
            },
            headers=auth(api_key),
        )

    taken = await report(stranger_key)
    assert taken.status_code == 201, taken.text
    await worker.run_once()
    assert await _evaluations(client, key, closing["id"]) == []

    real = await report(observer_key)
    assert real.status_code == 201, real.text
    assert real.json()["deduplicated"] is False
    assert _events(sync_engine, "observation.recorded", real.json()["id"])
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched", evaluation
    done = await _task(client, key, task["id"])
    assert done["status"] == "done"
    assert done["evidence"] == [
        {"kind": "observation", "observationId": real.json()["id"], "check": "rule-evidence"}
    ]

    # The observer's repeat is a repeat: no second event, no second closing.
    repeat = await report(observer_key)
    assert (repeat.status_code, repeat.json()["id"]) == (200, real.json()["id"])
    await worker.run_once()
    assert len(await _evaluations(client, key, closing["id"])) == 1


async def test_a_rule_stored_without_an_author_closes_nothing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """A rule written before Zh6 is skipped with a reason, not trusted."""
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(client, key, _closing(actor=s["admin"]))
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE work_rules SET trigger = trigger - 'actorId' WHERE id = :id"),
            {"id": closing["id"]},
        )
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    await _observe(client, key, task["id"])
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "skipped", evaluation
    assert evaluation["result"]["skipped"] == {"reason": "trigger_author_unfiltered"}
    after = await _task(client, key, task["id"])
    assert (after["version"], after["evidence"]) == (task["version"], [])


async def test_a_bound_rule_must_name_the_author_it_trusts(client: httpx.AsyncClient) -> None:
    s = await _admin(client)
    key = s["key"]
    bare = _closing(actor=s["admin"])
    bare["trigger"] = {"kind": "observation", "type": CLOSED, "source": "crm"}
    for trigger_extra, code, field_path in (
        ({}, "invalid_rule_trigger", "trigger.agent"),
        ({"agent": None, "actorId": None}, "invalid_rule_trigger", "trigger.agent"),
        ({"agent": "Not A Key"}, "invalid_rule_trigger", "trigger.agent"),
        ({"agent": 7}, "invalid_rule_trigger", "trigger.agent"),
        ({"actorId": "not-a-uuid"}, "invalid_rule_trigger", "trigger.actorId"),
        ({"actorId": 1}, "invalid_rule_trigger", "trigger.actorId"),
        ({"agent": "sample-missing"}, "unknown_agent", "trigger.agent"),
    ):
        body = {**bare, "trigger": {**bare["trigger"], **trigger_extra}}
        response = await client.post("/api/v1/rules", json=body, headers=auth(key))
        assert response.status_code == 422, (trigger_extra, response.text)
        error = response.json()["error"]
        assert (error["code"], error["details"]["field"]) == (code, field_path), trigger_extra

    # The id is kept in its canonical form; both filters may be given.
    await _publish_agent(client, key, AGENT, AGENT_PERMISSIONS)
    body = {
        **bare,
        "trigger": {**bare["trigger"], "agent": AGENT, "actorId": s["admin"].upper()},
    }
    created = await _rule(client, key, body)
    assert created["trigger"] == {
        "kind": "observation",
        "type": CLOSED,
        "source": "crm",
        "agent": AGENT,
        "actorId": s["admin"],
    }
    # Patching the filter away is refused like writing the rule without it.
    response = await client.patch(
        f"/api/v1/rules/{created['id']}",
        json={"trigger": {"kind": "observation", "type": CLOSED}},
        headers={**auth(key), "If-Match": f'"rule-{created["version"]}"'},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_rule_trigger"


async def test_an_author_binds_a_fact_only_to_a_task_it_may_read(
    app: FastAPI, client: httpx.AsyncClient, sync_engine: Engine, restore_authorizer: None
) -> None:
    """Zh6 / CP-ADR-0057: binding a fact to a task needs tasks.read on it (the PDP).

    An IAM subject (the PDP decides for it, as for reading the task) may not
    read one task: it binds no fact to it, neither by the task nor by its run.
    """
    s = await _admin(client)
    key = s["key"]
    hidden = await create_task(client, key, title="Hidden", typeKey=REVIEW)
    visible = await create_task(client, key, title="Visible", typeKey=REVIEW)
    _, _, run = await _run_on(client, key, hidden["id"])
    policy = DenyingPolicy(deny={("tasks.read", f"task:{hidden['id']}")})
    configure_authorizer(Authorizer(policy, "policy"))
    ctx = AuthContext(
        tenant_id=uuid.UUID(s["tenant"]),
        principal_id=uuid.UUID(s["admin"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    count = "SELECT count(*) FROM events WHERE entity_type = 'observation'"
    before = _count(sync_engine, count)
    for bound in (
        {"task_ref": hidden["id"]},
        {"task_ref": hidden["publicId"]},
        # Bound through its run: the same task, the same right.
        {"run_id": uuid.UUID(run["id"])},
    ):
        async with app.state.session_factory() as session:
            with pytest.raises(AuthorizationError) as refused:
                await record_observation(session, ctx, kind=CLOSED, content="closed", **bound)
        assert refused.value.details["resource"] == f"task:{hidden['id']}", bound
    assert _count(sync_engine, count) == before

    async with app.state.session_factory() as session:
        recorded = await record_observation(
            session, ctx, kind=CLOSED, content="closed", task_ref=visible["id"]
        )
        # Not bound to a task: nothing to read.
        free = await record_observation(session, ctx, kind=CLOSED, content="free")
        await session.commit()
    assert recorded.id != free.id
    assert ("tasks.read", f"task:{visible['id']}") in policy.calls
    assert _count(sync_engine, count) == before + 2


async def test_a_local_author_without_tasks_read_binds_no_fact(client: httpx.AsyncClient) -> None:
    s = await _admin(client)
    key = s["key"]
    task = await create_task(client, key, title="Review", typeKey=REVIEW)
    _, writer_key = await create_agent_with_key(
        client, key, name="writer", permissions=["observations.write"]
    )
    response = await client.post(
        "/api/v1/observations",
        json={"kind": CLOSED, "content": "closed", "task": task["id"]},
        headers=auth(writer_key),
    )
    assert response.status_code == 403, response.text
    response = await client.post(
        "/api/v1/observations", json={"kind": CLOSED, "content": "free"}, headers=auth(writer_key)
    )
    assert response.status_code == 201, response.text


# --- Zh7: the facts of the current attempt ----------------------------------------------


async def test_work_handed_in_anew_after_a_failed_attempt_needs_a_new_fact(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The review of I013: the old fact neither satisfies nor blocks the new attempt."""
    s = await _admin(client)
    key = s["key"]
    closing = await _rule(
        client, key, _closing(actor=s["admin"], taskTypes=["sample-signed"], check="closed")
    )
    task = await create_task(client, key, title="Signed", typeKey="sample-signed")
    first = await _observe(client, key, task["id"], dedup="first")
    await worker.run_once()
    [attempt] = await _verifications(client, key, task["id"])
    assert attempt["status"] == "waiting_external"

    # The sign-off never comes: the attempt fails and the work goes back.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE task_verifications SET started_at = now() - interval '30 days', "
                "next_check_at = now() WHERE id = :id"
            ),
            {"id": attempt["id"]},
        )
    await worker.run_once()
    [failed] = await _verifications(client, key, task["id"])
    assert failed["status"] == "failed", failed
    returned = await _task(client, key, task["id"])
    assert returned["systemStatusCategory"] != "terminal_success"

    # Handed in anew: the fact the failed attempt was checked on is spent.
    current = await _task(client, key, task["id"])
    handed = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert handed.status_code == 200, handed.text
    await worker.run_once()
    second, _ = await _verifications(client, key, task["id"])
    assert second["attempt"] == 2 and second["status"] == "waiting_external", second
    assert second["results"] == []

    # A new closing at the source is this attempt's fact, not a repeat.
    again = await _observe(client, key, task["id"], dedup="second")
    await worker.run_once()
    latest, _ = await _evaluations(client, key, closing["id"])
    assert latest["status"] == "matched", latest
    await worker.run_once()
    second, _ = await _verifications(client, key, task["id"])
    [closed_check] = second["results"]
    assert closed_check["key"] == "closed" and closed_check["status"] == "passed"
    assert closed_check["evidence"] == [{"kind": "observation", "observationId": again}]
    evidence = (await _task(client, key, task["id"]))["evidence"]
    assert [item["observationId"] for item in evidence] == [first, again]

    # A third fact while this attempt already has one: skipped as before.
    await _observe(client, key, task["id"], dedup="third")
    await worker.run_once()
    latest, *_ = await _evaluations(client, key, closing["id"])
    assert latest["status"] == "skipped"
    assert latest["result"]["skipped"]["reason"] == "verification_pending"
