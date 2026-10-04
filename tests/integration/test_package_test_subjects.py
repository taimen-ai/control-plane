"""``POST /packages:test`` for rules and task types (CP-ADR-0074 Z1-Z5, package-sdk S020).

A rule or a task type has no model in memory: its test runs the code of the
core — the observation command, the evaluation of the one rule, the decision
of a gate and its outcome, completion, the verification stage — in a
transaction that is rolled back, with the skill calls answered by the test's
mocks. The second domain is the ``invoice-payment`` fixture package: its
rules and task types come with their tests and the core runs them as they
are. Whatever the run did, every table of the database is the same, row for
row, afterwards.
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import Authorizer, configure_authorizer
from control_plane.config import Settings
from tests.helpers import auth, create_agent_with_key, create_task, create_workspace, do_bootstrap
from tests.integration.test_approval_outcomes import DenyingPolicy
from tests.integration.test_package_test import snapshot

PACKAGES = Path(__file__).resolve().parents[1] / "fixtures" / "packages"
# The catalog format of the fixtures.
API_VERSION = yaml.safe_load((PACKAGES / "invoice-payment" / "package.yaml").read_text())[
    "apiVersion"
]


def fixture_files(name: str) -> list[dict[str, str]]:
    root = PACKAGES / name
    return [
        {"path": path.relative_to(root).as_posix(), "content": path.read_text(encoding="utf-8")}
        for path in sorted(root.rglob("*.yaml"))
    ]


def document(kind: str, key: str, spec: dict[str, Any]) -> str:
    body = {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}
    return str(yaml.safe_dump(body, allow_unicode=True, sort_keys=False))


async def setup(client: httpx.AsyncClient) -> str:
    """A tenant and its admin key, already used once (its last use is not a write of the run)."""
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await client.get("/api/v1/task-types", headers=auth(key))).status_code == 200
    return key


async def run(
    client: httpx.AsyncClient, key: str, files: list[dict[str, str]], **body: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/packages:test", json={"package": {"files": files}, **body}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def by_file(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {test["file"]: test for test in body["tests"]}


# --- the second domain -----------------------------------------------------------------


async def test_the_invoice_package_runs_its_rule_and_task_type_tests(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    before = snapshot(sync_engine)

    body = await run(client, key, fixture_files("invoice-payment"))

    after = snapshot(sync_engine)
    # Work was filed in the tests: the tenant's counter of task numbers was not touched.
    assert after["task_counters"] == before["task_counters"]
    assert after == before
    tests = by_file(body)
    failing = {f: t["failures"] for f, t in tests.items() if t["status"] != "passed"}
    assert body["status"] == "passed", json.dumps(
        [failing, body["problems"]], ensure_ascii=False, indent=1
    )
    assert set(tests) == {
        "tests/invoice-received.test.yaml",
        "tests/invoice-disputed.test.yaml",
        "tests/invoice-disputed-internal.test.yaml",
        "tests/payment-approval-approved.test.yaml",
        "tests/payment-approval-rejected.test.yaml",
        "tests/payment-approval-completed.test.yaml",
    }
    received = tests["tests/invoice-received.test.yaml"]
    assert (received["subject"], received["object"], received["process"]) == (
        "rule",
        "invoice-received",
        None,
    )
    rules = {item["rule"]: item for item in body["ruleCoverage"]}
    assert set(rules) == {
        "invoice-disputed",
        "invoice-received",
        "invoice-withdrawn",
        "payment-settled",
    }
    disputed = rules["invoice-disputed"]
    assert disputed["tests"] == 2
    assert disputed["outcomes"]["missing"] == ["interpretation:failed"]
    assert disputed["branches"]["missing"] == ["/condition/and/0:false"]
    assert rules["invoice-withdrawn"]["tests"] == 0
    (approval,) = body["taskTypeCoverage"]
    assert (approval["taskType"], approval["tests"]) == ("payment-approval", 3)
    assert approval["outcomes"]["missing"] == ["default/approved/0/onFailure"]
    assert approval["completion"]["missing"] == []
    assert approval["acceptance"]["missing"] == ["acceptance/decided:failed"]


# --- the examples of the contract (plan R6, CP-ADR-0074 Z1) --------------------------------

CLASSIFY = {
    "version": "1",
    "description": "The category of a text",
    "sideEffects": "none",
    "riskLevel": "low",
    "contract": {
        "inputs": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "outputs": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["category", "confidence"],
        },
        "implementation": {"protocol": "local", "entrypoint": "tests.stubs:classify"},
    },
}
REPLY = {
    "version": "1",
    "description": "Answer a ticket",
    "sideEffects": "none",
    "riskLevel": "low",
    "contract": {
        "inputs": {
            "type": "object",
            "properties": {"ticketId": {"type": "string"}},
            "required": ["ticketId"],
        },
        "outputs": {"type": "object", "properties": {"sent": {"type": "boolean"}}},
        "implementation": {"protocol": "local", "entrypoint": "tests.stubs:reply"},
    },
}
MANIFEST = document("Package", "helpdesk", {"version": "1.0.0", "displayName": "Helpdesk"})
REOPENED_RULE = {
    "description": "A closed claim reopened is reviewed",
    "trigger": {"kind": "observation", "type": "helpdesk.ticket_reopened"},
    "condition": {"exists": "payload.data.claimKey"},
    "interpretation": {"skill": "claims.classify@1", "inputs": {"text": "{{payload.data.text}}"}},
    "action": {
        "kind": "ensure_work",
        "taskType": "claim-review",
        "dedupKeyTemplate": "claim:{{payload.data.claimKey}}",
        "fields": {
            "title": "Review claim {{payload.data.claimKey}}",
            "customFields": {
                "ticketId": "{{payload.data.ticketId}}",
                "category": "{{skill.output.category}}",
            },
        },
    },
}
CLAIM_REVIEW = {
    "displayName": "Claim review",
    "fieldSchema": {
        "type": "object",
        "properties": {"ticketId": {"type": "string"}, "category": {"type": "string"}},
    },
}
REFUND_APPROVAL = {
    "displayName": "Refund approval",
    "fieldSchema": {
        "type": "object",
        "properties": {"amount": {"type": "number"}, "ticketId": {"type": "string"}},
    },
    "approvalSchema": {
        "gates": {
            "default": {
                "outcomes": {
                    "approved": [
                        {
                            "invokeSkill": {
                                "skill": "helpdesk.reply@1",
                                "inputs": {"ticketId": "$.task.customFields.ticketId"},
                                "onSuccess": [{"completeTask": {}}],
                                "onFailure": [{"comment": {"body": "The reply failed"}}],
                            }
                        }
                    ],
                    "rejected": [
                        {
                            "ensureWork": {
                                "type": "claim-review",
                                "key": "refused:$.task.publicId",
                                "title": "Explain the refusal of $.task.publicId",
                                "customFields": {"ticketId": "$.task.customFields.ticketId"},
                            }
                        }
                    ],
                }
            }
        }
    },
}
CLAIM_REOPENED_TEST = """\
# tests/claim-reopened.test.yaml
subject: rule
rule: claim-reopened
name: повторное обращение по закрытой претензии заводит разбор ответственному
given:
  observation:
    kind: helpdesk.ticket_reopened
    data: {ticketId: "T-1", claimKey: "T-1", text: "…"}
mocks:
  skills:
    claims.classify@1:
      - output: {category: complaint, confidence: 0.91}
steps:
  - expect:
      result: matched
      ensureWork:
        - type: claim-review
          customFields: {ticketId: "T-1", category: complaint}
"""
REFUND_APPROVED_TEST = """\
# tests/refund-approved.test.yaml
subject: taskType
taskType: refund-approval
name: одобренный возврат вызывает скилл выплаты и закрывает задачу
given:
  task: {customFields: {amount: 72000, ticketId: "T-1"}}
mocks:
  skills:
    helpdesk.reply@1:
      - output: {sent: true}
steps:
  - approve: {gate: default, decision: approved}
  - expect:
      invokeSkill: [{skill: helpdesk.reply@1, inputs: {ticketId: "T-1"}}]
      status: {category: terminal_success}
"""


def helpdesk(*tests: tuple[str, str], rule: dict[str, Any] | None = None) -> list[dict[str, str]]:
    files = [
        ("package.yaml", MANIFEST),
        ("skills/classify.yaml", document("Skill", "claims.classify", CLASSIFY)),
        ("skills/reply.yaml", document("Skill", "helpdesk.reply", REPLY)),
        ("task-types/claim-review.yaml", document("TaskType", "claim-review", CLAIM_REVIEW)),
        (
            "task-types/refund-approval.yaml",
            document("TaskType", "refund-approval", REFUND_APPROVAL),
        ),
        (
            "rules/claim-reopened.yaml",
            document("WorkRule", "claim-reopened", rule or REOPENED_RULE),
        ),
        *tests,
    ]
    return [{"path": path, "content": content} for path, content in files]


def a_test(name: str, data: dict[str, Any]) -> tuple[str, str]:
    return (f"tests/{name}.test.yaml", yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


async def test_the_examples_of_the_contract_pass_the_schema_and_the_core(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    before = snapshot(sync_engine)

    body = await run(
        client,
        key,
        helpdesk(
            ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST),
            ("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST),
        ),
    )

    assert snapshot(sync_engine) == before
    assert body["status"] == "passed", json.dumps(body, ensure_ascii=False, indent=1)
    assert [(t["subject"], t["object"]) for t in body["tests"]] == [
        ("rule", "claim-reopened"),
        ("taskType", "refund-approval"),
    ]
    (rule,) = body["ruleCoverage"]
    assert rule["outcomes"] == {
        "covered": 2,
        "total": 4,
        "missing": ["not_matched", "interpretation:failed"],
    }
    assert rule["branches"] == {"covered": 1, "total": 2, "missing": ["/condition:false"]}
    (refund,) = body["taskTypeCoverage"]
    assert refund["outcomes"]["missing"] == [
        "default/approved/0/onFailure",
        "default/rejected",
    ]


async def test_a_rule_fired_by_a_journal_event(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    rule = {
        "trigger": {"kind": "event", "type": "task.completed"},
        "condition": {"eq": [{"var": "payload.status"}, "done"]},
        "action": {
            "kind": "ensure_work",
            "taskType": "claim-review",
            "dedupKeyTemplate": "after:{{payload.publicId}}",
            "fields": {"title": "Follow up {{payload.publicId}}"},
        },
    }
    done = {"type": "task.completed", "payload": {"publicId": "X-1", "status": "done"}}
    other = {"type": "task.completed", "payload": {"publicId": "X-2", "status": "closed"}}
    body = await run(
        client,
        key,
        helpdesk(
            a_test(
                "done",
                {
                    "subject": "rule",
                    "rule": "claim-reopened",
                    "name": "a completed task is followed up",
                    "given": {"event": done},
                    "steps": [
                        {
                            "expect": {
                                "result": "matched",
                                "ensureWork": [{"type": "claim-review", "title": "Follow up X-1"}],
                            }
                        }
                    ],
                },
            ),
            a_test(
                "other",
                {
                    "subject": "rule",
                    "rule": "claim-reopened",
                    "name": "another status files nothing",
                    "given": {"event": other},
                    "steps": [{"expect": {"result": "not_matched", "ensureWork": []}}],
                    "coverage": {"minimum": 100},
                },
            ),
            rule=rule,
        ),
    )
    tests = by_file(body)
    assert tests["tests/done.test.yaml"]["status"] == "passed", tests
    other_result = tests["tests/other.test.yaml"]
    # Both expectations hold; the one branch this test alone reaches is half of them.
    assert other_result["status"] == "failed"
    (failure,) = other_result["failures"]
    assert failure["expected"] == 100 and failure["actual"]["missing"] == ["/condition:true"]
    (coverage,) = body["ruleCoverage"]
    assert coverage["branches"]["missing"] == []


async def test_a_mock_off_the_skill_schema_fails_the_rule_test(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    test = yaml.safe_load(CLAIM_REOPENED_TEST)
    test["mocks"]["skills"]["claims.classify@1"] = [{"output": {"category": 7}}]
    body = await run(client, key, helpdesk(a_test("claim", test)))
    assert body["status"] == "failed"
    (result,) = body["tests"]
    (failure,) = result["failures"]
    assert "claims.classify@1" in failure["message"] and failure["actual"] == {"category": 7}


async def test_a_failed_interpretation_files_nothing(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    test = yaml.safe_load(CLAIM_REOPENED_TEST)
    test["mocks"]["skills"]["claims.classify@1"] = [{"error": {"type": "model_unavailable"}}]
    test["steps"] = [{"expect": {"result": "failed", "ensureWork": [], "noSideEffects": True}}]
    body = await run(client, key, helpdesk(a_test("claim", test)))
    assert body["status"] == "passed", body["tests"]
    (rule,) = body["ruleCoverage"]
    assert "interpretation:failed" not in rule["outcomes"]["missing"]


async def test_a_refused_decision_fails_the_test_with_the_code_of_the_refusal(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    guarded = copy_of(REFUND_APPROVAL)
    guarded["approvalSchema"]["gates"]["default"]["preconditions"] = {
        "approved": [
            {
                "observation": {"kind": "helpdesk.refund_budget"},
                "reason": "no refund budget for $.task.publicId",
            }
        ]
    }
    files = [
        f
        for f in helpdesk(("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST))
        if f["path"] != "task-types/refund-approval.yaml"
    ]
    files.append(
        {
            "path": "task-types/refund-approval.yaml",
            "content": document("TaskType", "refund-approval", guarded),
        }
    )
    body = await run(client, key, files)
    assert body["status"] == "failed"
    (result,) = body["tests"]
    (failure,) = result["failures"]
    assert failure["step"] == 0 and failure["actual"]["code"] == "approval_precondition_failed"
    (refund,) = body["taskTypeCoverage"]
    assert refund["preconditions"] == {
        "covered": 1,
        "total": 2,
        "missing": ["default/preconditions/approved/0:held"],
    }


async def test_a_rejection_files_its_work(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    test = {
        "subject": "taskType",
        "taskType": "refund-approval",
        "name": "a refusal is explained",
        "given": {"task": {"customFields": {"ticketId": "T-2"}}},
        "steps": [
            {"approve": {"decision": "rejected", "by": "lead"}},
            {
                "expect": {
                    "ensureWork": [{"type": "claim-review", "customFields": {"ticketId": "T-2"}}],
                    "invokeSkill": [],
                    "status": {"key": "todo"},
                }
            },
            {"expect": {"ensureWork": [], "invokeSkill": []}},
        ],
    }
    body = await run(client, key, helpdesk(a_test("refused", test)))
    assert body["status"] == "passed", body["tests"]


async def test_fields_only_a_process_runs_are_a_warning(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    test = yaml.safe_load(CLAIM_REOPENED_TEST)
    test["version"] = 2
    test["mocks"]["recall"] = [{"output": {"nodes": []}}]
    body = await run(client, key, helpdesk(a_test("claim", test)))
    assert body["status"] == "passed"
    ignored = [p for p in body["problems"] if p["code"] == "test_field_ignored"]
    assert {(p["severity"], p["path"], p["file"]) for p in ignored} == {
        ("warning", "/version", "tests/claim.test.yaml"),
        ("warning", "/mocks/recall", "tests/claim.test.yaml"),
    }


async def test_an_unknown_rule_or_task_type_is_a_finding(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    rule = yaml.safe_load(CLAIM_REOPENED_TEST) | {"rule": "claim-closed"}
    task_type = yaml.safe_load(REFUND_APPROVED_TEST) | {"taskType": "refund"}
    body = await run(client, key, helpdesk(a_test("rule", rule), a_test("type", task_type)))
    assert body["status"] == "invalid" and body["tests"] == []
    codes = {(p["code"], p["path"]) for p in body["problems"] if p["severity"] == "error"}
    assert codes == {("unknown_test_rule", "/rule"), ("unknown_test_task_type", "/taskType")}


async def test_check_only_checks_the_rules_and_types_and_runs_nothing(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    broken = copy_of(REOPENED_RULE)
    broken["condition"] = {"like": ["a", "b"]}
    response = await client.post(
        "/api/v1/packages:test?checkOnly=true",
        json={
            "package": {
                "files": helpdesk(
                    ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST), rule=broken
                )
            }
        },
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invalid" and body["tests"] == []
    (error,) = [p for p in body["problems"] if p["severity"] == "error"]
    assert (error["code"], error["file"]) == ("invalid_rule_condition", "rules/claim-reopened.yaml")


async def test_without_the_rights_of_a_kind_the_tests_err_and_process_tests_run(
    client: httpx.AsyncClient,
) -> None:
    from tests.integration.test_package_test import PROCESS, _catalog, scenario

    admin = await setup(client)
    await _catalog(client, admin)
    _, author = await create_agent_with_key(
        client,
        admin,
        name="author",
        permissions=["packages.test", "task_types.manage", "org.manage", "skills.invoke"],
    )
    files = [
        *helpdesk(
            ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST),
            ("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST),
        ),
        {"path": "processes/sample.yaml", "content": PROCESS},
        {"path": "tests/review.test.yaml", "content": yaml.safe_dump(scenario())},
    ]
    body = await run(client, author, files)
    tests = by_file(body)
    assert tests["tests/review.test.yaml"]["status"] == "passed"
    for name in ("claim-reopened", "refund-approved"):
        result = tests[f"tests/{name}.test.yaml"]
        assert result["status"] == "error"
        assert result["failures"][0]["message"].startswith("permission_required")
    (warning,) = [p for p in body["problems"] if p["code"] == "permission_required"]
    assert (warning["severity"], warning["file"]) == ("warning", "rules/claim-reopened.yaml")
    assert "rules.write" in (warning["hint"] or "")
    assert body["status"] == "failed"


async def test_an_outgoing_call_is_an_error_of_the_test(
    client: httpx.AsyncClient, monkeypatch: Any
) -> None:
    from control_plane import sandbox
    from control_plane.application.commands import rule_evaluations

    load_views = rule_evaluations._load_views

    async def reaching(*args: Any) -> None:
        sandbox.refuse_outgoing("memory")
        await load_views(*args)

    monkeypatch.setattr(rule_evaluations, "_load_views", reaching)
    key = await setup(client)
    body = await run(client, key, helpdesk(("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST)))
    (result,) = body["tests"]
    assert result["status"] == "error"
    assert result["failures"][0]["message"].startswith("sandbox_outgoing_call")


async def test_a_key_held_on_the_stand_is_a_lock_timeout(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    tenant = boot["tenant"]["id"]
    with sync_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"work-rule:{tenant}:claim-reopened"},
        )
        body = await run(
            client, key, helpdesk(("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST))
        )
    (result,) = body["tests"]
    assert result["status"] == "error"
    assert result["failures"][0]["message"].startswith("lock_timeout")


async def test_a_rule_test_runs_in_the_workspace_of_the_request(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    workspace = await create_workspace(client, key, "finance")
    before = snapshot(sync_engine)
    body = await run(client, key, fixture_files("invoice-payment"), workspaceId=workspace["id"])
    assert snapshot(sync_engine) == before
    assert body["status"] == "passed", body["tests"]


def copy_of(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json.dumps(value))
    return result


# --- one transaction per test, the PDP and the worker's filters (TASK-001066) ------------


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


def _delivered(sync_engine: Engine, event_type: str, entity_id: str) -> bool:
    """Is the event below the horizon every reader of the journal applies (``event_cursor``)?"""
    with sync_engine.connect() as conn:
        return bool(
            conn.scalar(
                text(
                    "SELECT e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint"
                    " FROM events e WHERE e.event_type = :type AND e.entity_id = :id"
                ),
                {"type": event_type, "id": entity_id},
            )
        )


async def test_an_event_committed_during_a_test_is_delivered_when_that_test_ends(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: Any
) -> None:
    """A test holds the horizon of the journal for itself only, never for the whole run."""
    from control_plane.application.commands import package_trials

    key = await setup(client)
    publish = package_trials._Run._publish
    seen: list[bool] = []
    committed: dict[str, Any] = {}

    async def watching(run: Any) -> None:
        if not committed:
            # The first test holds its transaction open: an event of the stand commits.
            committed.update(await create_workspace(client, key, "stand"))
        seen.append(_delivered(sync_engine, "workspace.created", committed["id"]))
        await publish(run)

    monkeypatch.setattr(package_trials._Run, "_publish", watching)
    body = await run(
        client,
        key,
        helpdesk(
            ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST),
            ("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST),
            ("tests/claim-again.test.yaml", CLAIM_REOPENED_TEST),
        ),
    )
    assert body["status"] == "passed", body["tests"]
    # Held back while the first test runs; delivered as soon as it is rolled back.
    assert seen == [False, True, True]


async def test_too_many_rule_and_task_type_tests_are_refused(
    client: httpx.AsyncClient, monkeypatch: Any
) -> None:
    from control_plane.application.commands import package_trials

    monkeypatch.setattr(package_trials, "MAX_SUBJECT_TESTS", 1)
    key = await setup(client)
    body = await run(
        client,
        key,
        helpdesk(
            ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST),
            ("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST),
        ),
    )
    assert body["status"] == "invalid" and body["tests"] == []
    (error,) = [p for p in body["problems"] if p["severity"] == "error"]
    assert error["code"] == "too_many_tests"
    narrowed = await run(
        client,
        key,
        helpdesk(
            ("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST),
            ("tests/refund-approved.test.yaml", REFUND_APPROVED_TEST),
        ),
        tests=["tests/claim-reopened.test.yaml"],
    )
    assert narrowed["status"] == "passed", narrowed


# A rule that reads the task of its trigger and names it in the work it files.
FOLLOW_UP_RULE = {
    "workspaceId": "${WORKSPACE}",
    "trigger": {"kind": "event", "type": "task.completed"},
    "condition": {"exists": "task.title"},
    "action": {
        "kind": "ensure_work",
        "taskType": "claim-review",
        "dedupKeyTemplate": "after:{{payload.taskId}}",
        "fields": {"title": "Follow up {{task.title}}"},
    },
}


def _follow_up_test(payload: dict[str, Any]) -> tuple[str, str]:
    return a_test(
        "follow-up",
        {
            "subject": "rule",
            "rule": "claim-reopened",
            "name": "a completed task is followed up",
            "given": {"event": {"type": "task.completed", "payload": payload}},
            "steps": [{"expect": {"result": "matched", "ensureWork": [{"type": "claim-review"}]}}],
        },
    )


def _iam_caller(sync_engine: Engine, tenant_id: str) -> Any:
    """The admin of the tenant as it calls through IAM: a binding and its subject."""
    from control_plane.application.authorization import AuthContext

    iam = uuid.uuid4()
    with sync_engine.begin() as conn:
        principal_id, permissions = conn.execute(
            text(
                "SELECT principal_id, permissions FROM api_keys WHERE tenant_id = :t"
                " ORDER BY created_at LIMIT 1"
            ),
            {"t": tenant_id},
        ).one()
        binding_id = conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :t, :p, 'https://iam.test', :t, :iam, "
                "CAST(:perms AS jsonb), 'active', now(), now()) RETURNING id"
            ),
            {"t": tenant_id, "p": principal_id, "iam": iam, "perms": json.dumps(permissions)},
        ).scalar_one()
    return AuthContext(
        tenant_id=uuid.UUID(tenant_id),
        principal_id=principal_id,
        principal_kind="human",
        api_key_id=binding_id,
        permissions=frozenset(permissions),
        request_id="package-test-policy",
        correlation_id="package-test-policy",
        iam_principal_id=iam,
    )


async def _test_as(
    settings: Settings, ctx: Any, files: list[dict[str, str]], workspace_id: str | None
) -> dict[str, Any]:
    from control_plane.api.v1.packages import SHAPES, SUPPORTING, check_calendar
    from control_plane.application.commands.package_test import run_package_tests
    from control_plane.infrastructure.db.engine import build_engine, build_session_factory

    engine = build_engine(settings)
    try:
        report = await run_package_tests(
            build_session_factory(engine),
            ctx,
            settings,
            None,
            files=[(f["path"], f["content"]) for f in files],
            tests=None,
            workspace_id=uuid.UUID(workspace_id) if workspace_id else None,
            check_only=False,
            check_calendar=check_calendar,
            shapes=SHAPES,
            supporting=SUPPORTING,
        )
    finally:
        await engine.dispose()
    return report.out()


async def test_in_policy_mode_a_test_reads_no_task_its_caller_could_not(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, restore_authorizer: None
) -> None:
    """CP_AUTHZ_MODE=policy: the PDP answers the caller's read of the task of the event.

    The caller's flat rights read every task of the tenant; the PDP says it
    may not read the task of the other workspace. The local check alone
    would put that task's title into the work the test reports. The setting
    is refused before the rule runs: a rule acting as an agent of the test
    would read it with the local check (Z8).
    """
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    foreign = await create_workspace(client, key, "foreign")
    secret = await create_task(client, key, title="Secret merger", workspaceId=foreign["id"])
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny={("tasks.read", f"task:{secret['id']}")})
    configure_authorizer(Authorizer(policy, "policy"))
    before = snapshot(sync_engine)

    body = await _test_as(
        settings,
        ctx,
        helpdesk(_follow_up_test({"taskId": secret["id"]}), rule=FOLLOW_UP_RULE),
        own["id"],
    )

    assert snapshot(sync_engine) == before
    (result,) = body["tests"]
    assert result["status"] == "error"
    (refused,) = result["failures"]
    assert refused["message"].startswith(
        "given_refused: given.event.payload.taskId: the core refused it: permission_denied"
    ), refused
    assert "Secret merger" not in json.dumps(body, ensure_ascii=False)
    assert ("tasks.read", f"task:{secret['id']}", str(ctx.iam_principal_id)) in policy.calls


async def test_in_policy_mode_an_unknown_task_of_an_event_is_refused_as_an_unreadable_one(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, restore_authorizer: None
) -> None:
    """The caller's read is asked of the id: a refusal says nothing of whether the task exists."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    secret = await create_task(client, key, title="Secret merger", workspaceId=own["id"])
    unknown = str(uuid.uuid4())
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny={("tasks.read", f"task:{t}") for t in (secret["id"], unknown)})
    configure_authorizer(Authorizer(policy, "policy"))

    messages = []
    for task_id in (secret["id"], unknown):
        body = await _test_as(
            settings,
            ctx,
            helpdesk(_follow_up_test({"taskId": task_id}), rule=FOLLOW_UP_RULE),
            own["id"],
        )
        (result,) = body["tests"]
        assert result["status"] == "error", result
        (refused,) = result["failures"]
        messages.append(refused["message"].replace(task_id, "<id>"))
    assert messages[0] == messages[1]


async def test_in_policy_mode_the_id_of_a_task_event_is_asked_as_its_task_id(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, restore_authorizer: None
) -> None:
    """``id`` of a ``task.*`` event is the task the rule reads, as ``taskId`` is."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    secret = await create_task(client, key, title="Secret merger", workspaceId=own["id"])
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny={("tasks.read", f"task:{secret['id']}")})
    configure_authorizer(Authorizer(policy, "policy"))

    body = await _test_as(
        settings,
        ctx,
        helpdesk(_follow_up_test({"id": secret["id"]}), rule=FOLLOW_UP_RULE),
        own["id"],
    )

    (result,) = body["tests"]
    assert result["status"] == "error"
    (refused,) = result["failures"]
    assert refused["message"].startswith("given_refused: given.event.payload.id:"), refused
    assert "Secret merger" not in json.dumps(body, ensure_ascii=False)


async def test_in_policy_mode_the_pdp_never_hears_of_the_rows_of_the_test(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    restore_authorizer: None,
    monkeypatch: Any,
) -> None:
    from sqlalchemy import select

    from control_plane.application.commands import package_trials
    from control_plane.infrastructure.db.models import Task

    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    mine = await create_task(client, key, title="Merger", workspaceId=own["id"])
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny=set())
    configure_authorizer(Authorizer(policy, "policy"))
    expect = package_trials._Run._expect
    filed: list[str] = []

    async def capturing(run: Any, index: int, spec: Any) -> Any:
        rows = await run.db.scalars(select(Task.id).where(Task.public_id.like("TEST-%")))
        filed.extend(str(task_id) for task_id in rows.all())
        assert set(filed) <= run.trial.written
        return await expect(run, index, spec)

    monkeypatch.setattr(package_trials._Run, "_expect", capturing)
    body = await _test_as(
        settings,
        ctx,
        helpdesk(_follow_up_test({"taskId": mine["id"]}), rule=FOLLOW_UP_RULE),
        own["id"],
    )

    assert body["status"] == "passed", body["tests"]
    assert filed
    asked = {resource for _, resource, _ in policy.calls}
    assert f"task:{mine['id']}" in asked
    assert not {f"task:{task_id}" for task_id in filed} & asked


async def test_an_event_of_another_workspace_does_not_reach_the_rule(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, restore_authorizer: None
) -> None:
    """The worker skips an event naming another workspace: so does the test, and says so."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    foreign = await create_workspace(client, key, "foreign")
    secret = await create_task(client, key, title="Secret merger", workspaceId=foreign["id"])
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny=set())
    configure_authorizer(Authorizer(policy, "policy"))

    body = await _test_as(
        settings,
        ctx,
        helpdesk(
            _follow_up_test({"taskId": secret["id"], "workspaceId": foreign["id"]}),
            rule=FOLLOW_UP_RULE,
        ),
        own["id"],
    )

    (result,) = body["tests"]
    assert result["status"] == "failed"
    evaluated, work = result["failures"]
    assert evaluated["message"].startswith("the rule is not evaluated: it names workspace")
    assert evaluated["actual"]["result"] is None
    assert work["actual"] == []
    assert "Secret merger" not in json.dumps(body, ensure_ascii=False)
    (warning,) = [p for p in body["problems"] if p["code"] == "input_not_delivered"]
    assert (warning["severity"], warning["file"], warning["path"]) == (
        "warning",
        "tests/follow-up.test.yaml",
        "/given/event",
    )
    assert foreign["id"] in warning["message"]


async def test_a_call_without_a_mock_that_leaves_the_rule_waiting_is_a_warning(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    test = yaml.safe_load(CLAIM_REOPENED_TEST)
    del test["mocks"]
    test["steps"] = [{"expect": {"result": "waiting", "ensureWork": []}}]
    body = await run(client, key, helpdesk(a_test("claim", test)))
    assert body["status"] == "passed", body["tests"]
    (warning,) = [p for p in body["problems"] if p["code"] == "unmocked_skill_call"]
    assert (warning["severity"], warning["file"], warning["path"]) == (
        "warning",
        "tests/claim.test.yaml",
        "/mocks/skills",
    )
    assert "claims.classify@1" in warning["message"]
    # A call its mock answers leaves no warning.
    answered = await run(client, key, helpdesk(("tests/claim.test.yaml", CLAIM_REOPENED_TEST)))
    assert not [p for p in answered["problems"] if p["code"] == "unmocked_skill_call"]


async def test_in_members_mode_a_task_of_an_event_outside_the_sight_is_a_missing_task(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """CP-ADR-0082 V6: the local check reads every task with the caller's flat
    rights, but the caller in ``members`` mode does not see the workspace of
    the task. The setting is refused as for a task that does not exist."""
    import dataclasses

    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    foreign = await create_workspace(client, key, "foreign")
    secret = await create_task(client, key, title="Secret merger", workspaceId=foreign["id"])
    mine = await create_task(client, key, title="Own work", workspaceId=own["id"])
    ctx = dataclasses.replace(
        _iam_caller(sync_engine, boot["tenant"]["id"]),
        visibility="members",
        visible_workspaces=frozenset({own["id"]}),
    )

    messages = []
    for task_id in (secret["id"], str(uuid.uuid4())):
        body = await _test_as(
            settings,
            ctx,
            helpdesk(_follow_up_test({"taskId": task_id}), rule=FOLLOW_UP_RULE),
            own["id"],
        )
        (result,) = body["tests"]
        assert result["status"] == "error", result
        (refused,) = result["failures"]
        assert refused["message"].startswith(
            "given_refused: given.event.payload.taskId: the core refused it: not_found"
        ), refused
        assert "Secret merger" not in json.dumps(body, ensure_ascii=False)
        messages.append(refused["message"].replace(task_id, "<id>"))
    assert messages[0] == messages[1]

    body = await _test_as(
        settings,
        ctx,
        helpdesk(_follow_up_test({"taskId": mine["id"]}), rule=FOLLOW_UP_RULE),
        own["id"],
    )
    assert "given_refused" not in json.dumps(body), body
