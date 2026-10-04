"""``role:<slug>`` as the addressee of a gate (CP-ADR-0061, amendment 2026-10-01).

A package addresses the gate its task type, acceptance check or rule opens to
the role it declares itself: the reference is checked when the type or the
rule is published (``unknown_role``) and resolved to the role's id, seen from
the task's workspace, when the gate is opened. Whoever decides must hold it.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from control_plane.worker.main import Worker
from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
)
from tests.integration.test_approval_outcomes import _create_type, _task, _tasks_of_type
from tests.integration.test_completion_work import (
    RUNNER_PERMISSIONS,
    _commit,
    _completion_rows,
    _run_to_success,
)
from tests.integration.test_verification_m16 import (
    HUMAN,
    _approval,
    _complete,
    _verifications,
    worker,
)
from tests.integration.test_work_rules_m13 import DRIFT_RULE

__all__ = ["worker"]

DECIDER = ["approvals.read", "approvals.decide"]


def _completion(assignee: str) -> dict[str, Any]:
    return {
        "onComplete": {
            "actions": [
                {
                    "ensureWork": {
                        "type": "purchase-review",
                        "key": "review:$.task.id",
                        "title": "Review $.task.publicId",
                        "requestApproval": {"assignee": assignee, "comment": "Approve it"},
                    }
                }
            ]
        }
    }


async def _setup(client: httpx.AsyncClient, *, role: bool = True) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    found = await create_role(client, admin_key, "purchase-approver") if role else None
    _, runner_key = await create_agent_with_key(
        client, admin_key, name="runner", permissions=RUNNER_PERMISSIONS
    )
    holder, holder_key = await create_agent_with_key(
        client, admin_key, name="holder", permissions=DECIDER
    )
    _, other_key = await create_agent_with_key(client, admin_key, name="other", permissions=DECIDER)
    review = await _create_type(client, admin_key, "purchase-review")
    assert review.status_code == 201, review.text
    return {
        "key": admin_key,
        "role": found,
        "runner_key": runner_key,
        "holder": holder,
        "holder_key": holder_key,
        "other_key": other_key,
    }


async def _approvals_of(client: httpx.AsyncClient, key: str, task_id: str) -> list[Any]:
    response = await client.get(f"/api/v1/approvals?taskId={task_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[Any] = response.json()["items"]
    return items


# --- publication ----------------------------------------------------------------------


@pytest.mark.parametrize("document", ["approvalSchema", "completionSchema"])
async def test_a_gate_addressed_to_an_unknown_role_is_refused_at_publication(
    client: httpx.AsyncClient, document: str
) -> None:
    s = await _setup(client, role=False)
    schema = _completion("role:purchase-approver")
    if document == "approvalSchema":
        schema = {
            "gates": {
                "default": {"outcomes": {"approved": schema["onComplete"]["actions"]}},
            }
        }
    response = await _create_type(client, s["key"], "purchase-request", **{document: schema})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unknown_role"
    assert error["details"]["role"] == "purchase-approver"
    assert error["details"]["field"].endswith("ensureWork.requestApproval.assignee")


@pytest.mark.parametrize(
    "reference",
    [
        "role:",
        "role:Purchase",
        "role:a b",
        "role:-x",
        # Read as the acceptance check's pattern reads it: nothing is trimmed.
        "role: purchase-approver",
        "role:purchase-approver ",
        "role:purchase-approver\n",
        "role:\tpurchase-approver",
    ],
)
async def test_a_malformed_role_reference_names_no_role(
    client: httpx.AsyncClient, reference: str
) -> None:
    s = await _setup(client)
    response = await _create_type(
        client, s["key"], "purchase-request", completionSchema=_completion(reference)
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_role"


async def test_a_declared_role_and_a_template_are_published(client: httpx.AsyncClient) -> None:
    s = await _setup(client)
    declared = await _create_type(
        client,
        s["key"],
        "purchase-request",
        completionSchema=_completion("role:purchase-approver"),
    )
    assert declared.status_code == 201, declared.text
    # A template is only known when it renders: checked when the action runs.
    templated = await _create_type(
        client,
        s["key"],
        "purchase-order",
        completionSchema=_completion("role:$.task.customFields.approver"),
    )
    assert templated.status_code == 201, templated.text


async def test_a_role_in_a_workspace_is_enough_to_publish(client: httpx.AsyncClient) -> None:
    s = await _setup(client, role=False)
    finance = await create_workspace(client, s["key"], "finance")
    await create_role(client, s["key"], "purchase-approver", workspace_id=finance["id"])
    response = await _create_type(
        client,
        s["key"],
        "purchase-request",
        completionSchema=_completion("role:purchase-approver"),
    )
    assert response.status_code == 201, response.text


# --- the gate ---------------------------------------------------------------------------


async def test_the_gate_asks_the_role_and_only_its_holder_decides(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    created = await _create_type(
        client, key, "purchase-request", completionSchema=_completion("role:purchase-approver")
    )
    assert created.status_code == 201, created.text
    await assign_role(client, key, s["holder"]["id"], s["role"]["id"])
    request = await create_task(client, key, title="Buy chairs", typeKey="purchase-request")
    await _commit(client, key, request["id"])

    await _run_to_success(client, s["runner_key"], request["id"])

    (review,) = _tasks_of_type(sync_engine, "purchase-review")
    (approval,) = await _approvals_of(client, key, str(review.id))
    assert (approval["gate"], approval["requiredRoleId"], approval["assignedPrincipalId"]) == (
        True,
        s["role"]["id"],
        None,
    )
    refused = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", json={}, headers=auth(s["other_key"])
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_eligible"
    decided = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", json={}, headers=auth(s["holder_key"])
    )
    assert decided.status_code == 200, decided.text


async def test_the_role_of_the_tasks_workspace_wins_over_the_tenant_wide_one(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    finance = await create_workspace(client, key, "finance")
    invoices = await create_workspace(client, key, "invoices", parent_id=finance["id"])
    local = await create_role(client, key, "purchase-approver", workspace_id=finance["id"])
    created = await _create_type(
        client, key, "purchase-request", completionSchema=_completion("role:purchase-approver")
    )
    assert created.status_code == 201, created.text
    request = await create_task(
        client, key, title="Buy desks", typeKey="purchase-request", workspaceId=invoices["id"]
    )
    await _commit(client, key, request["id"])

    await _run_to_success(client, s["runner_key"], request["id"])

    (review,) = _tasks_of_type(sync_engine, "purchase-review")
    (approval,) = await _approvals_of(client, key, str(review.id))
    assert approval["requiredRoleId"] == local["id"] != s["role"]["id"]


async def test_a_template_that_renders_to_no_role_fails_the_work_not_the_completion(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    created = await _create_type(
        client,
        key,
        "purchase-request",
        completionSchema=_completion("role:$.task.customFields.approver"),
        fieldSchema={"type": "object", "properties": {"approver": {"type": "string"}}},
    )
    assert created.status_code == 201, created.text
    request = await create_task(
        client,
        key,
        title="Buy lamps",
        typeKey="purchase-request",
        customFields={"approver": "nobody-here"},
    )
    await _commit(client, key, request["id"])

    await _run_to_success(client, s["runner_key"], request["id"])

    assert (await _task(client, key, request["id"]))["systemStatusCategory"] == "terminal_success"
    # The action rolled back with its work item: nothing filed, nothing to decide.
    assert _tasks_of_type(sync_engine, "purchase-review") == []
    (row,) = _completion_rows(sync_engine)
    assert row.status == "failed"
    assert "unknown_role" in str(row.error)


async def test_a_template_that_renders_to_a_padded_slug_names_no_role(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    # The role exists; " purchase-approver" is not its slug when the gate is opened.
    s = await _setup(client)
    key = s["key"]
    created = await _create_type(
        client,
        key,
        "purchase-request",
        completionSchema=_completion("role:$.task.customFields.approver"),
        fieldSchema={"type": "object", "properties": {"approver": {"type": "string"}}},
    )
    assert created.status_code == 201, created.text
    request = await create_task(
        client,
        key,
        title="Buy lamps",
        typeKey="purchase-request",
        customFields={"approver": " purchase-approver"},
    )
    await _commit(client, key, request["id"])

    await _run_to_success(client, s["runner_key"], request["id"])

    assert _tasks_of_type(sync_engine, "purchase-review") == []
    (row,) = _completion_rows(sync_engine)
    assert row.status == "failed"
    assert "unknown_role" in str(row.error)


# --- acceptance checks and rules --------------------------------------------------------


async def test_an_acceptance_check_names_a_role_by_slug(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    unknown = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Check",
            "acceptance": [{**HUMAN, "spec": {"approverRole": "role:purchase-approver"}}],
        },
        headers=auth(key),
    )
    assert unknown.status_code == 422, unknown.text
    error = unknown.json()["error"]
    assert (error["code"], error["details"]["field"]) == (
        "unknown_role",
        "acceptance[0].spec.approverRole",
    )
    malformed = await client.post(
        "/api/v1/tasks",
        json={"title": "Check", "acceptance": [{**HUMAN, "spec": {"approverRole": "approvers"}}]},
        headers=auth(key),
    )
    assert malformed.status_code == 422, malformed.text
    assert malformed.json()["error"]["code"] == "invalid_acceptance_spec"

    role = await create_role(client, key, "purchase-approver")
    task = await create_task(
        client,
        key,
        title="Check",
        acceptance=[{**HUMAN, "spec": {"approverRole": "role:purchase-approver"}}],
    )
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    approval = await _approval(client, key, attempt["approvalId"])
    assert (approval["requiredRoleId"], approval["assignedPrincipalId"]) == (role["id"], None)


async def test_a_type_acceptance_check_with_an_unknown_role_is_not_published(
    client: httpx.AsyncClient,
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _create_type(
        client,
        key,
        "purchase-request",
        acceptance=[{**HUMAN, "spec": {"approverRole": "role:purchase-approver"}}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_role"


def _decision_rule(approver_role: str) -> dict[str, Any]:
    return {
        **DRIFT_RULE,
        "action": {
            **DRIFT_RULE["action"],
            "kind": "request_decision",
            "fields": {**DRIFT_RULE["action"]["fields"], "approverRole": approver_role},
        },
    }


async def test_a_rule_asks_a_role_by_slug(client: httpx.AsyncClient, worker: Worker) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    unknown = await client.post(
        "/api/v1/rules", json=_decision_rule("role:purchase-approver"), headers=auth(key)
    )
    assert unknown.status_code == 422, unknown.text
    error = unknown.json()["error"]
    assert (error["code"], error["details"]["field"]) == (
        "unknown_role",
        "action.fields.approverRole",
    )

    role = await create_role(client, key, "purchase-approver")
    rule = await client.post(
        "/api/v1/rules", json=_decision_rule("role:purchase-approver"), headers=auth(key)
    )
    assert rule.status_code == 201, rule.text
    observed = await client.post(
        "/api/v1/observations",
        json={"kind": "drift.seen", "content": "seen", "data": {"id": "a", "severity": "high"}},
        headers=auth(key),
    )
    assert observed.status_code in (200, 201), observed.text
    await worker.run_once()

    evaluations = (
        await client.get(f"/api/v1/rules/{rule.json()['id']}/evaluations", headers=auth(key))
    ).json()["items"]
    approval_id = evaluations[0]["result"]["work"][0]["approvalId"]
    approval = (await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))).json()
    assert (approval["requiredRoleId"], approval["assignedPrincipalId"]) == (role["id"], None)
