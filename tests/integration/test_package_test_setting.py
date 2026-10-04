"""The setting of rule and task type tests (CP-ADR-0074, amendment 2026-09-30, TASK-001188).

What S023 could not write as a test of a package: an install variable naming
a principal, a role or a workspace of the stand; a task that needs a
workspace; an artifact whose content an output check looks for; a rule that
runs on a schedule, and a rule that reads the task an event is about. Each
now has a setting of its own, and a setting the core refuses is a red test
that says why. Whatever a test set up, the database is the same afterwards.
"""

import json
import re
import time
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from control_plane.application.authorization import Authorizer, configure_authorizer
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from tests.helpers import auth, create_workspace, do_bootstrap
from tests.integration.test_approval_outcomes import DenyingPolicy
from tests.integration.test_package_test import PROCESS, package, snapshot
from tests.integration.test_package_test_subjects import (
    CLAIM_REVIEW,
    CLASSIFY,
    _iam_caller,
    _test_as,
    a_test,
    by_file,
    document,
    restore_authorizer,
    run,
    setup,
)

__all__ = ["restore_authorizer"]

MANIFEST = document(
    "Package",
    "claims",
    {
        "version": "1.0.0",
        "displayName": "Claims",
        "variables": {
            "REVIEWER": {"description": "Who reviews a claim", "kind": "principal"},
            "SIGNERS": {"description": "Who signs a claim off", "kind": "role"},
            "DESK": {"description": "Where claims are handled", "kind": "workspace"},
        },
    },
)
ASSIGN_RULE = {
    "workspaceId": "${DESK}",
    "trigger": {"kind": "observation", "type": "claim.opened"},
    "action": {
        "kind": "ensure_work",
        "taskType": "claim-review",
        "dedupKeyTemplate": "claim:{{payload.data.id}}",
        "fields": {"title": "Review {{payload.data.id}}", "assignee": "${REVIEWER}"},
    },
}
EXPIRY_RULE = {
    "trigger": {"kind": "schedule", "type": "interval", "everySeconds": 86400},
    "interpretation": {
        "skill": "claims.classify@1",
        "inputs": {"text": "{{trigger.scheduledAt}}"},
    },
    "action": {
        "kind": "ensure_work",
        "taskType": "claim-review",
        "dedupKeyTemplate": "expiry:{{trigger.scheduledAt}}",
        "fields": {
            "title": "Expiry {{trigger.scheduledAt}}",
            "customFields": {"category": "{{skill.output.category}}"},
        },
    },
}
FOLLOW_UP_RULE = {
    "trigger": {"kind": "event", "type": "task.completed"},
    "condition": {"eq": [{"var": "task.typeKey"}, "claim-review"]},
    "action": {
        "kind": "ensure_work",
        "taskType": "claim-review",
        "dedupKeyTemplate": "after:{{task.id}}",
        "fields": {"title": "Follow up {{task.title}}", "relations": {"spawnedBy": "{{task.id}}"}},
    },
}
SIGN_OFF = {
    "displayName": "Sign-off",
    "acceptance": [
        {
            "key": "signed",
            "kind": "human",
            "description": "Signed off by a signer",
            "spec": {"approverRole": "${SIGNERS}"},
        },
        {
            "key": "reviewed",
            "kind": "human",
            "description": "Reviewed by the reviewer",
            "spec": {"approver": "${REVIEWER}"},
        },
    ],
}
ESCALATION = {
    "displayName": "Escalation",
    "approvalSchema": {
        "gates": {
            "default": {
                "outcomes": {
                    "approved": [
                        {
                            "ensureWork": {
                                "type": "claim-review",
                                "key": "escalated:$.task.publicId",
                                "title": "Escalated $.task.publicId",
                                "workspace": "$.task.workspaceId!",
                            }
                        }
                    ]
                }
            }
        }
    },
}
REPORT = {
    "displayName": "Report",
    "mediaTypes": ["application/pdf"],
    "metadataSchema": {"type": "object"},
}
REPORTED = {
    "displayName": "Reported",
    "artifactSchema": {"outputs": [{"key": "report", "type": "claim-report", "required": True}]},
}


def claims(*tests: tuple[str, str]) -> list[dict[str, str]]:
    files = [
        ("package.yaml", MANIFEST),
        ("skills/classify.yaml", document("Skill", "claims.classify", CLASSIFY)),
        ("artifact-types/report.yaml", document("ArtifactType", "claim-report", REPORT)),
        ("task-types/claim-review.yaml", document("TaskType", "claim-review", CLAIM_REVIEW)),
        ("task-types/sign-off.yaml", document("TaskType", "sign-off", SIGN_OFF)),
        ("task-types/escalation.yaml", document("TaskType", "escalation", ESCALATION)),
        ("task-types/reported.yaml", document("TaskType", "reported", REPORTED)),
        ("rules/assign.yaml", document("WorkRule", "claim-assign", ASSIGN_RULE)),
        ("rules/expiry.yaml", document("WorkRule", "claim-expiry", EXPIRY_RULE)),
        ("rules/follow-up.yaml", document("WorkRule", "claim-follow-up", FOLLOW_UP_RULE)),
        *tests,
    ]
    return [{"path": path, "content": content} for path, content in files]


def rule_test(name: str, rule: str, given: dict[str, Any], expect: dict[str, Any]) -> Any:
    return a_test(
        name,
        {
            "subject": "rule",
            "rule": rule,
            "name": name,
            "given": given,
            "steps": [{"expect": expect}],
        },
    )


def type_test(name: str, task_type: str, given: dict[str, Any], steps: list[Any]) -> Any:
    return a_test(
        name,
        {
            "subject": "taskType",
            "taskType": task_type,
            "name": name,
            "given": given,
            "steps": steps,
        },
    )


def passed(body: dict[str, Any]) -> None:
    failing = {f: t["failures"] for f, t in by_file(body).items() if t["status"] != "passed"}
    assert body["status"] == "passed", json.dumps(
        [failing, body["problems"]], ensure_ascii=False, indent=1
    )


def failure(body: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    result = by_file(body)[f"tests/{name}.test.yaml"]
    assert result["failures"], result
    first: dict[str, Any] = result["failures"][0]
    return result["status"], first


# --- 1. variables of kind principal and role ------------------------------------------


async def test_a_principal_variable_names_a_principal_of_the_test(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    admin = boot["adminPrincipal"]["id"]
    # Used once: its last use is not a write of the run.
    assert (await client.get("/api/v1/task-types", headers=auth(key))).status_code == 200
    opened = {"observation": {"kind": "claim.opened", "data": {"id": "C-1"}}}
    before = snapshot(sync_engine)

    body = await run(
        client,
        key,
        claims(
            rule_test(
                "named",
                "claim-assign",
                {**opened, "variables": {"REVIEWER": "alice"}},
                {"result": "matched", "ensureWork": [{"title": "Review C-1", "assignee": "alice"}]},
            ),
            # Without a value: the principal of the test called by the variable.
            rule_test(
                "unnamed",
                "claim-assign",
                opened,
                {"result": "matched", "ensureWork": [{"assignee": "REVIEWER"}]},
            ),
            # An empty value is no value.
            rule_test(
                "empty",
                "claim-assign",
                {**opened, "variables": {"REVIEWER": ""}},
                {"result": "matched", "ensureWork": [{"assignee": "REVIEWER"}]},
            ),
            # A principal of the tenant is not taken: a scenario does not depend on the stand.
            rule_test(
                "of-the-stand",
                "claim-assign",
                {**opened, "variables": {"REVIEWER": admin}},
                {"result": "matched", "ensureWork": [{"assignee": "REVIEWER"}]},
            ),
            # The id of no principal of the tenant is not looked up elsewhere.
            rule_test(
                "unknown-id",
                "claim-assign",
                {**opened, "variables": {"REVIEWER": "00000000-0000-4000-8000-0000000000aa"}},
                {"result": "matched", "ensureWork": [{"assignee": "REVIEWER"}]},
            ),
        ),
    )

    assert snapshot(sync_engine) == before
    passed(body)


async def test_role_and_principal_variables_decide_the_checks_of_a_task_type(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    steps = [
        {"complete": {}},
        {"verify": {"check": "signed", "result": "passed"}},
        {"verify": {"check": "reviewed", "result": "passed"}},
        {"expect": {"status": {"category": "terminal_success"}}},
    ]
    body = await run(
        client,
        key,
        claims(
            type_test(
                "by-slug",
                "sign-off",
                {"variables": {"SIGNERS": "signers"}, "principals": {"signers": ["sam"]}},
                steps,
            ),
            # Without a value: the role whose slug is the variable's name.
            type_test("by-name", "sign-off", {}, steps),
            # The same name in a variable and in given.principals is one principal.
            type_test(
                "one-person",
                "sign-off",
                {
                    "variables": {"SIGNERS": "signers", "REVIEWER": "sam"},
                    "principals": {"signers": ["sam"]},
                },
                steps,
            ),
        ),
    )
    passed(body)
    (coverage,) = [c for c in body["taskTypeCoverage"] if c["taskType"] == "sign-off"]
    assert "acceptance/signed:passed" not in coverage["acceptance"]["missing"]
    assert "acceptance/reviewed:passed" not in coverage["acceptance"]["missing"]


async def test_a_role_variable_does_not_take_the_slug_of_a_role_of_the_package(
    client: httpx.AsyncClient, monkeypatch: Any
) -> None:
    """The package's Role is published by its command; the variable names that role."""
    from control_plane.application.commands import org as org_commands

    key = await setup(client)
    created: list[str] = []
    create_role = org_commands.create_role

    async def watching(*args: Any, **kwargs: Any) -> Any:
        created.append(kwargs["slug"])
        return await create_role(*args, **kwargs)

    monkeypatch.setattr(org_commands, "create_role", watching)
    signers = ("roles/signers.yaml", document("Role", "signers", {"name": "Signers"}))
    steps = [
        {"complete": {}},
        {"verify": {"check": "signed", "result": "passed"}},
        {"verify": {"check": "reviewed", "result": "passed"}},
        {"expect": {"status": {"category": "terminal_success"}}},
    ]
    body = await run(
        client,
        key,
        claims(
            signers,
            # Without a value (the slug is the variable's name) and with the slug as value.
            type_test("by-name", "sign-off", {"principals": {"signers": ["sam"]}}, steps),
            type_test(
                "by-slug",
                "sign-off",
                {"variables": {"SIGNERS": "signers"}, "principals": {"signers": ["sam"]}},
                steps,
            ),
        ),
    )
    passed(body)
    assert created == ["signers", "signers"]


# --- 2. workspace -----------------------------------------------------------------------


async def test_a_task_of_a_test_has_a_workspace(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    test = type_test(
        "escalated",
        "escalation",
        {},
        [
            {"approve": {"decision": "approved"}},
            {
                "expect": {
                    "ensureWork": [{"type": "claim-review", "title": "Escalated TEST-000001"}]
                }
            },
        ],
    )
    before = snapshot(sync_engine)
    # Without a workspace in the request: the test's own, gone with its rollback.
    body = await run(client, key, claims(test))
    assert snapshot(sync_engine) == before
    passed(body)
    # With one: the task is filed there.
    workspace = await create_workspace(client, key, "claims")
    passed(await run(client, key, claims(test), workspaceId=workspace["id"]))


async def test_a_workspace_variable_is_the_workspace_of_the_test(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    workspace = await create_workspace(client, key, "desk")
    opened = {"observation": {"kind": "claim.opened", "data": {"id": "C-2"}}}
    expect = {"result": "matched", "ensureWork": [{"title": "Review C-2"}]}
    # The id of a workspace of the tenant stays; any other value is the test's workspace.
    body = await run(
        client,
        key,
        claims(
            rule_test(
                "stand", "claim-assign", {**opened, "variables": {"DESK": workspace["id"]}}, expect
            ),
            rule_test("own", "claim-assign", {**opened, "variables": {"DESK": "desk"}}, expect),
        ),
    )
    passed(body)


def _variables_seen(monkeypatch: Any) -> list[tuple[dict[str, str], Any]]:
    """The variables of each test as its publication takes them, with the test's workspace."""
    from control_plane.application.commands import package_trials

    seen: list[tuple[dict[str, str], Any]] = []
    publish = package_trials._Run._publish

    async def watching(run: Any) -> None:
        seen.append((dict(run.variables), run.workspace_id))
        await publish(run)

    monkeypatch.setattr(package_trials._Run, "_publish", watching)
    return seen


async def test_a_workspace_variable_keeps_only_a_workspace_the_caller_may_read(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    restore_authorizer: None,
    monkeypatch: Any,
) -> None:
    """As ``workspaceId`` of the request: ``processes.read`` of it, which the PDP decides.

    A workspace the caller may not read is taken as no workspace of the
    tenant at all: the test's own, and the report does not tell them apart.
    """
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    own = await create_workspace(client, key, "own")
    readable = await create_workspace(client, key, "readable")
    hidden = await create_workspace(client, key, "hidden")
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    policy = DenyingPolicy(deny={("processes.read", f"workspace:{hidden['id']}")})
    configure_authorizer(Authorizer(policy, "policy"))
    seen = _variables_seen(monkeypatch)
    opened = {"observation": {"kind": "claim.opened", "data": {"id": "C-3"}}}
    expect = {"result": "matched", "ensureWork": [{"title": "Review C-3"}]}
    unknown = "00000000-0000-4000-8000-0000000000bb"
    before = snapshot(sync_engine)

    bodies = [
        await _test_as(
            settings,
            ctx,
            claims(rule_test("desk", "claim-assign", {**opened, "variables": {"DESK": d}}, expect)),
            own["id"],
        )
        for d in (readable["id"], hidden["id"], unknown)
    ]

    assert snapshot(sync_engine) == before
    for body in bodies:
        passed(body)
    (stays, _), (hidden_seen, test_workspace), (unknown_seen, _) = seen
    assert stays["DESK"] == readable["id"]
    assert hidden_seen["DESK"] == str(test_workspace) != hidden["id"]
    assert unknown_seen["DESK"] != unknown
    assert ("processes.read", f"workspace:{hidden['id']}", str(ctx.iam_principal_id)) in (
        policy.calls
    )
    hidden_body = json.dumps(bodies[1], ensure_ascii=False).replace(hidden["id"], "<id>")
    unknown_body = json.dumps(bodies[2], ensure_ascii=False).replace(unknown, "<id>")
    assert "<id>" not in hidden_body and "<id>" not in unknown_body


async def test_in_local_mode_a_workspace_variable_needs_the_flat_processes_read(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine, monkeypatch: Any
) -> None:
    """``CP_AUTHZ_MODE=local``: without a flat ``processes.read`` no workspace stays.

    Such a caller names no ``workspaceId`` of the request either (403), so the
    variable takes the workspace the test makes for itself.
    """
    import dataclasses

    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    desk = await create_workspace(client, key, "desk")
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    flat = frozenset(p.value for p in Permission) - {Permission.ADMIN.value}
    sighted = dataclasses.replace(ctx, permissions=flat)
    blind = dataclasses.replace(ctx, permissions=flat - {Permission.PROCESSES_READ.value})
    seen = _variables_seen(monkeypatch)
    opened = {"observation": {"kind": "claim.opened", "data": {"id": "C-4"}}}
    expect = {"result": "matched", "ensureWork": [{"title": "Review C-4"}]}
    files = claims(
        rule_test("desk", "claim-assign", {**opened, "variables": {"DESK": desk["id"]}}, expect)
    )
    before = snapshot(sync_engine)

    for caller in (sighted, blind):
        passed(await _test_as(settings, caller, files, None))

    assert snapshot(sync_engine) == before
    (kept, _), (replaced, test_workspace) = seen
    assert kept["DESK"] == desk["id"]
    assert test_workspace is not None
    assert replaced["DESK"] == str(test_workspace) != desk["id"]


# --- 3. content of artifacts --------------------------------------------------------------


async def test_the_content_of_an_artifact_passes_the_check_of_an_output(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    report = {"type": "claim-report", "key": "report", "content": "%PDF-1.7 claim"}
    before = snapshot(sync_engine)
    body = await run(
        client,
        key,
        claims(
            type_test(
                "with-content",
                "reported",
                {"artifacts": [{**report, "mediaType": "application/pdf"}]},
                [{"complete": {}}, {"expect": {"status": {"category": "terminal_success"}}}],
            ),
            # A record without content does not pass it: the task stays open.
            type_test(
                "without-content",
                "reported",
                {"artifacts": [{"type": "claim-report", "key": "report"}]},
                [{"complete": {}}, {"expect": {"status": {"category": "active"}}}],
            ),
        ),
    )
    assert snapshot(sync_engine) == before
    passed(body)


async def test_content_of_a_media_type_the_artifact_type_refuses_is_an_error(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    report = {"type": "claim-report", "content": "plain", "mediaType": "text/plain"}
    body = await run(
        client,
        key,
        claims(type_test("text", "reported", {"artifacts": [report]}, [{"complete": {}}])),
    )
    status, first = failure(body, "text")
    assert status == "error"
    assert first["message"].startswith("given_refused: given.artifacts[0]:"), first


async def test_content_without_its_media_type_is_refused_by_the_schema(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    report = {"type": "claim-report", "content": "%PDF"}
    body = await run(
        client,
        key,
        claims(type_test("bare", "reported", {"artifacts": [report]}, [{"complete": {}}])),
    )
    assert body["status"] == "invalid" and body["tests"] == []
    (problem,) = [p for p in body["problems"] if p["code"] == "invalid_test"]
    assert problem["file"] == "tests/bare.test.yaml"


async def test_content_is_bounded_in_bytes_not_characters(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """1 MiB of UTF-8 (CP-ADR-0074 Z8), whatever ``CP_ARTIFACT_MAX_BYTES`` allows the stand.

    The route's body limit keeps such a package off ``/packages:test`` today;
    the command is called directly, as a bigger limit would let it through.
    """
    from control_plane.application.commands.package_trials import GIVEN_CONTENT_MAX_BYTES

    assert settings.artifact_max_bytes > GIVEN_CONTENT_MAX_BYTES
    boot = await do_bootstrap(client)
    workspace = await create_workspace(client, boot["apiKey"]["key"], "claims")
    ctx = _iam_caller(sync_engine, boot["tenant"]["id"])
    head = "%PDF-1.7 "
    fits = head + "x" * (GIVEN_CONTENT_MAX_BYTES - len(head))
    # Fewer characters than the schema's maxLength, more bytes than the limit.
    wide = head + "\u044f" * (GIVEN_CONTENT_MAX_BYTES // 2)
    assert len(wide) <= 1048576 < len(wide.encode("utf-8"))
    report = {"type": "claim-report", "key": "report", "mediaType": "application/pdf"}
    done = [{"complete": {}}, {"expect": {"status": {"category": "terminal_success"}}}]
    before = snapshot(sync_engine)
    body = await _test_as(
        settings,
        ctx,
        claims(
            type_test("fits", "reported", {"artifacts": [{**report, "content": fits}]}, done),
            type_test("wide", "reported", {"artifacts": [{**report, "content": wide}]}, done),
        ),
        workspace["id"],
    )
    assert snapshot(sync_engine) == before
    assert by_file(body)["tests/fits.test.yaml"]["status"] == "passed", body
    status, first = failure(body, "wide")
    assert status == "error"
    assert first["message"] == (
        f"given_refused: given.artifacts: the content is larger than {GIVEN_CONTENT_MAX_BYTES}"
        " bytes"
    )


async def test_more_artifacts_than_the_schema_allows_are_refused(
    client: httpx.AsyncClient,
) -> None:
    """``maxItems``: a YAML alias names one megabyte many times, the list stays short."""
    key = await setup(client)
    note = {"type": "claim-report", "content": "%PDF", "mediaType": "application/pdf"}
    body = await run(
        client,
        key,
        claims(
            type_test("many", "reported", {"artifacts": [note] * 21}, [{"complete": {}}]),
        ),
    )
    assert body["status"] == "invalid" and body["tests"] == []
    (problem,) = [p for p in body["problems"] if p["code"] == "invalid_test"]
    assert problem["file"] == "tests/many.test.yaml"


async def test_the_deadline_of_a_test_bounds_its_artifacts(
    client: httpx.AsyncClient, monkeypatch: Any
) -> None:
    """Past :data:`TEST_DEADLINE`, no further ``given.artifacts[i]`` is filed."""
    from datetime import timedelta

    from control_plane.application.commands import package_trials

    key = await setup(client)
    filed: list[str] = []
    create = package_trials.create_artifact

    async def counting(*args: Any, **kwargs: Any) -> Any:
        filed.append(kwargs["name"])
        return await create(*args, **kwargs)

    monkeypatch.setattr(package_trials, "create_artifact", counting)
    monkeypatch.setattr(package_trials, "TEST_DEADLINE", timedelta(seconds=-1))
    note = {"type": "claim-report", "content": "%PDF", "mediaType": "application/pdf"}
    body = await run(
        client,
        key,
        claims(type_test("late", "reported", {"artifacts": [note] * 3}, [{"complete": {}}])),
    )
    status, first = failure(body, "late")
    assert status == "error"
    assert first["message"].startswith("test_timeout:"), first
    assert filed == []


NOTE = {"displayName": "Note", "metadataSchema": {"type": "object"}}
NOTED = {
    "displayName": "Noted",
    "artifactSchema": {"outputs": [{"key": "note", "type": "claim-note", "required": True}]},
}


def noted(note: dict[str, Any], *tests: tuple[str, str]) -> list[dict[str, str]]:
    return claims(
        ("artifact-types/note.yaml", document("ArtifactType", "claim-note", note)),
        ("task-types/noted.yaml", document("TaskType", "noted", NOTED)),
        *tests,
    )


async def test_an_artifact_type_without_media_types_takes_any(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """``mediaTypes`` is optional in the catalog: omitted, it is ``["*/*"]`` as in package-sdk."""
    key = await setup(client)
    done = [{"complete": {}}, {"expect": {"status": {"category": "terminal_success"}}}]
    before = snapshot(sync_engine)
    body = await run(
        client,
        key,
        noted(
            NOTE,
            *(
                type_test(
                    name,
                    "noted",
                    {"artifacts": [{"type": "claim-note", "content": "x", "mediaType": media}]},
                    done,
                )
                for name, media in (("text", "text/plain"), ("pdf", "application/pdf"))
            ),
        ),
    )
    assert snapshot(sync_engine) == before
    passed(body)
    assert not [p for p in body["problems"] if p["code"] == "invalid_artifact_type"]


async def test_media_types_given_by_a_package_still_bind(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    note = {**NOTE, "mediaTypes": ["text/markdown"]}
    given = {"artifacts": [{"type": "claim-note", "content": "x", "mediaType": "text/plain"}]}
    body = await run(
        client, key, noted(note, type_test("text", "noted", given, [{"complete": {}}]))
    )
    status, first = failure(body, "text")
    assert status == "error"
    assert first["message"].startswith("given_refused: given.artifacts[0]:"), first


async def test_an_empty_list_of_media_types_is_not_the_default(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    note = {**NOTE, "mediaTypes": []}
    body = await run(client, key, noted(note, type_test("any", "noted", {}, [{"complete": {}}])))
    assert body["status"] != "passed"
    assert [p for p in body["problems"] if p["file"] == "artifact-types/note.yaml"], body


# --- 4. a schedule and a task of the setting ----------------------------------------------


async def test_a_rule_runs_on_its_schedule(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    mocks = {"skills": {"claims.classify@1": [{"output": {"category": "stale", "confidence": 1}}]}}
    body = await run(
        client,
        key,
        claims(
            a_test(
                "slot",
                {
                    "subject": "rule",
                    "rule": "claim-expiry",
                    "name": "a slot at its time",
                    "given": {"schedule": {"at": "2026-02-01T00:00:00Z"}},
                    "mocks": mocks,
                    "steps": [
                        {
                            "expect": {
                                "result": "matched",
                                "invokeSkill": [
                                    {
                                        "skill": "claims.classify@1",
                                        "inputs": {"text": "2026-02-01T00:00:00+00:00"},
                                    }
                                ],
                                "ensureWork": [
                                    {
                                        "title": "Expiry 2026-02-01T00:00:00+00:00",
                                        "customFields": {"category": "stale"},
                                    }
                                ],
                            }
                        }
                    ],
                },
            ),
            a_test(
                "clock",
                {
                    "subject": "rule",
                    "rule": "claim-expiry",
                    "name": "a slot at the clock of the test",
                    "given": {"schedule": {}, "clock": "2026-03-01T09:00:00Z"},
                    "mocks": mocks,
                    "steps": [
                        {
                            "expect": {
                                "result": "matched",
                                "ensureWork": [{"title": "Expiry 2026-03-01T09:00:00+00:00"}],
                            }
                        }
                    ],
                },
            ),
        ),
    )
    passed(body)
    (coverage,) = [c for c in body["ruleCoverage"] if c["rule"] == "claim-expiry"]
    assert coverage["outcomes"]["missing"] == ["not_matched", "interpretation:failed"]


async def test_a_schedule_does_not_fire_a_rule_of_events(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    body = await run(
        client,
        key,
        claims(rule_test("slot", "claim-follow-up", {"schedule": {}}, {"result": "matched"})),
    )
    status, first = failure(body, "slot")
    assert status == "failed"
    assert first["message"].startswith("the input does not fire the rule"), first


async def test_an_event_is_about_the_task_of_the_setting(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    completed = {"type": "task.completed", "payload": {}}
    before = snapshot(sync_engine)
    body = await run(
        client,
        key,
        claims(
            rule_test(
                "follow-up",
                "claim-follow-up",
                {"task": {"type": "claim-review", "title": "Claim T-9"}, "event": completed},
                {
                    "result": "matched",
                    # The task of the setting is not work the rule filed.
                    "ensureWork": [
                        {"title": "Follow up Claim T-9", "relation": {"spawnedBy": "TEST-000001"}}
                    ],
                },
            ),
            rule_test(
                "other-type",
                "claim-follow-up",
                {"task": {"type": "escalation"}, "event": completed},
                {"result": "not_matched", "ensureWork": []},
            ),
        ),
    )
    assert snapshot(sync_engine) == before
    passed(body)


# --- 5. a setting the core refuses ----------------------------------------------------------


async def test_a_refused_setting_is_a_red_test_that_says_why(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    completed = {"type": "task.completed", "payload": {}}
    body = await run(
        client,
        key,
        claims(
            type_test(
                "fields",
                "claim-review",
                {"task": {"customFields": {"ticketId": 7}}},
                [{"complete": {}}],
            ),
            type_test(
                "status", "claim-review", {"task": {"status": "nowhere"}}, [{"complete": {}}]
            ),
            rule_test(
                "type",
                "claim-follow-up",
                {"task": {"type": "no-such-type"}, "event": completed},
                {"result": "matched"},
            ),
        ),
    )
    assert body["status"] == "failed"
    for name, where in (
        ("fields", "given.task"),
        ("status", "given.task.status"),
        ("type", "given.task"),
    ):
        status, first = failure(body, name)
        assert status == "error", first
        assert first["message"].startswith(f"given_refused: {where}: the core refused it:"), first
        assert first["actual"]["code"], first


def test_the_setting_grammar_is_in_the_schema_the_core_holds() -> None:
    from control_plane.domain.package_source import package_test_schema

    defs = package_test_schema()["$defs"]
    rule_given = defs["ruleGiven"]
    assert {"schedule"} in [set(item["required"]) for item in rule_given["oneOf"]]
    assert rule_given["properties"]["task"]["required"] == ["type"]
    artifacts = defs["taskTypeGiven"]["properties"]["artifacts"]
    assert artifacts["maxItems"] == 20
    artifact = artifacts["items"]
    assert artifact["dependentRequired"] == {"content": ["mediaType"], "mediaType": ["content"]}


async def test_concurrent_runs_do_not_wait_for_each_others_workspace(
    client: httpx.AsyncClient,
) -> None:
    """Each test's workspace has a slug of its own: no run holds another on the tree."""
    import asyncio

    key = await setup(client)
    test = type_test(
        "escalated",
        "escalation",
        {},
        [{"approve": {"decision": "approved"}}, {"expect": {"status": {"category": "active"}}}],
    )
    first, second = await asyncio.wait_for(
        asyncio.gather(run(client, key, claims(test)), run(client, key, claims(test))),
        timeout=60,
    )
    passed(first)
    passed(second)


# --- files the loader refuses (TASK-001231) -------------------------------------------------


def _json_test(name: str, data: dict[str, Any]) -> tuple[str, str]:
    """A test file as JSON (which is YAML): ``\\ud800`` stays an escape the loader reads."""
    return (f"tests/{name}.test.yaml", json.dumps(data, ensure_ascii=True))


async def test_a_lone_surrogate_in_a_test_is_invalid_yaml_not_a_500(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """In the content of an artifact (UTF-8 of the upload) and in an event (JSON of Postgres)."""
    key = await setup(client)
    report = {
        "type": "claim-report",
        "key": "report",
        "content": "%PDF \ud800",
        "mediaType": "application/pdf",
    }
    content = _json_test(
        "content",
        {
            "subject": "taskType",
            "taskType": "reported",
            "name": "content",
            "given": {"artifacts": [report]},
            "steps": [{"complete": {}}],
        },
    )
    event = _json_test(
        "event",
        {
            "subject": "rule",
            "rule": "claim-follow-up",
            "name": "event",
            "given": {"event": {"type": "task.completed", "payload": {"note": "\udfff"}}},
            "steps": [{"expect": {"result": "matched"}}],
        },
    )
    before = snapshot(sync_engine)
    for test in (content, event):
        body = await run(client, key, claims(test))
        assert body["status"] == "invalid" and body["tests"] == [], body
        (problem,) = body["problems"]
        assert (problem["code"], problem["file"]) == ("invalid_yaml", test[0])
        assert "lone surrogate" in problem["message"]
    assert snapshot(sync_engine) == before


def _bombs() -> dict[str, str]:
    """Files a few KB long that expand past memory; and a character YAML refuses."""
    rows = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 9):
        rows.append(f"a{level}: &a{level} [" + ", ".join([f"*a{level - 1}"] * 10) + "]")
    return {
        # Eight levels of ten aliases: 10**9 nodes from 600 bytes.
        "rules/bomb.yaml": "\n".join(rows) + "\n",
        # 2000 characters times 300 times 290: 174 million characters from 4 KB.
        "rules/long.yaml": (
            f"s: &s {'x' * 2000}\n"
            f"l: &l [{', '.join(['*s'] * 300)}]\n"
            f"m: [{', '.join(['*l'] * 290)}]\n"
        ),
        # 900 KB five times over.
        "rules/big.yaml": f"s: &s {'y' * 900_000}\nl: [*s, *s, *s, *s, *s]\n",
    }


async def test_a_nul_in_a_package_file_is_refused_at_the_api_boundary(
    client: httpx.AsyncClient,
) -> None:
    """A NUL never reaches the reader of YAML: ``422 validation_error`` (CP-ADR-0083)."""
    key = await setup(client)
    files = [*claims(), {"path": "rules/nul.yaml", "content": "a: \x00\n"}]
    for route in ("packages:test", "packages:test?checkOnly=true", "packages:plan"):
        response = await client.post(
            f"/api/v1/{route}", json={"package": {"files": files}}, headers=auth(key)
        )
        assert response.status_code == 422, (route, response.text)
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["errors"] == [
            {
                "path": f"/package/files/{len(files) - 1}/content",
                "code": "nul_character",
                "message": "Request body contains the NUL character (U+0000) in a string",
            }
        ]


async def test_an_alias_bomb_is_invalid_yaml_on_every_route_that_reads_a_package(
    client: httpx.AsyncClient,
) -> None:
    """Each file is refused before its expansion exists, on every route, never a 500."""
    key = await setup(client)
    for path, content in _bombs().items():
        files = [*claims(), {"path": path, "content": content}]
        for route in ("packages:test", "packages:test?checkOnly=true", "packages:plan"):
            started = time.monotonic()
            response = await client.post(
                f"/api/v1/{route}", json={"package": {"files": files}}, headers=auth(key)
            )
            assert time.monotonic() - started < 10, (path, route)
            assert response.status_code in (200, 422), (path, route, response.text)
            problems = response.json().get("problems") or response.json()["error"]["details"]
            assert any(p["code"] == "invalid_yaml" and p["file"] == path for p in problems), (
                path,
                route,
                response.text,
            )


async def test_a_bomb_spread_over_many_files_is_refused_fast_by_the_budget_of_the_package(
    client: httpx.AsyncClient,
) -> None:
    """300 files of 250 bytes, each under the limit of a file: 22 million nodes together."""
    key = await setup(client)
    rows = ["a0: &a0 [" + ", ".join(["x"] * 9) + "]"]
    for level in range(1, 5):
        rows.append(f"a{level}: &a{level} [" + ", ".join([f"*a{level - 1}"] * 9) + "]")
    text = "\n".join(rows) + "\n"
    assert len(text) < 300
    bombs = [{"path": f"rules/bomb{index:03}.yaml", "content": text} for index in range(300)]
    files = [*claims(), *bombs]
    for route in ("packages:test?checkOnly=true", "packages:plan"):
        started = time.monotonic()
        response = await client.post(
            f"/api/v1/{route}", json={"package": {"files": files}}, headers=auth(key)
        )
        assert time.monotonic() - started < 5, route
        assert response.status_code in (200, 422), (route, response.text)
        problems = response.json().get("problems") or response.json()["error"]["details"]
        refused = {
            p["file"]
            for p in problems
            if p["code"] == "invalid_yaml" and "the files of the package" in p["message"]
        }
        # The first two fit; the budget is spent from the third on, whatever the files are.
        assert "rules/bomb002.yaml" in refused and "rules/bomb299.yaml" in refused, route
        assert not refused & {"rules/bomb000.yaml", "rules/bomb001.yaml"}, route


async def test_a_date_in_an_event_is_a_string_and_a_binary_is_invalid_yaml_not_a_500(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """YAML 1.2 has no timestamps; ``!!binary`` makes bytes, which no JSON holds."""
    key = await setup(client)
    dated = (
        "tests/dated.test.yaml",
        "subject: rule\nrule: claim-follow-up\nname: dated\ngiven:\n"
        "  task: {type: claim-review, title: Claim T-9}\n"
        "  event: {type: task.completed, payload: {due: 2026-09-30, at: 2026-09-30T10:00:00Z}}\n"
        "steps:\n  - expect: {result: matched}\n",
    )
    binary = ("tests/binary.test.yaml", dated[1].replace("due: 2026-09-30", "due: !!binary eA=="))
    before = snapshot(sync_engine)
    passed(await run(client, key, claims(dated)))
    body = await run(client, key, claims(binary))
    assert body["status"] == "invalid" and body["tests"] == [], body
    (problem,) = body["problems"]
    assert (problem["code"], problem["file"], problem["line"]) == ("invalid_yaml", binary[0], 6)
    assert snapshot(sync_engine) == before


# --- numbers of YAML 1.2 (TASK-001247) ------------------------------------------------------

ROUTES = ("packages:test", "packages:test?checkOnly=true", "packages:plan")


def _event_test(name: str, value: str) -> tuple[str, str]:
    """A rule test whose event payload holds ``value`` as written, on line 6."""
    return (
        f"tests/{name}.test.yaml",
        f"subject: rule\nrule: claim-follow-up\nname: {name}\ngiven:\n"
        "  task: {type: claim-review, title: Claim T-9}\n"
        f"  event: {{type: task.completed, payload: {{n: {value}}}}}\n"
        "steps:\n  - expect: {result: matched}\n",
    )


async def _problems(
    client: httpx.AsyncClient, key: str, route: str, files: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """The findings of ``route`` for the package: a report (200) or a refusal (422), no 500."""
    response = await client.post(
        f"/api/v1/{route}", json={"package": {"files": files}}, headers=auth(key)
    )
    assert response.status_code in (200, 422), (route, response.text)
    body = response.json()
    problems: list[dict[str, Any]] = body.get("problems") or body.get("error", {}).get(
        "details", []
    )
    return problems


def _refused_at(problems: list[dict[str, Any]], code: str, file: str) -> dict[str, Any]:
    found = [p for p in problems if p["code"] == code and p["file"] == file]
    assert found, problems
    return found[0]


async def test_a_sexagesimal_number_is_a_string_and_costs_no_cpu(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """``1:59:59:...`` of a megabyte: YAML 1.1 made it an int in 27 s; YAML 1.2 reads a string."""
    import asyncio

    key = await setup(client)
    clock = _event_test("clock", "1" + ":59" * 320_000)
    assert len(clock[1]) > 950_000
    files = claims(clock)
    for route in ROUTES:
        started = time.monotonic()
        problems = await _problems(client, key, route, files)
        assert time.monotonic() - started < 5, route
        assert not [p for p in problems if p["file"] == clock[0]], (route, problems)
    # Four at once take no longer than the bound of one each: none holds the pool.
    started = time.monotonic()
    await asyncio.gather(
        *(_problems(client, key, "packages:test?checkOnly=true", files) for _ in range(4))
    )
    assert time.monotonic() - started < 10
    before = snapshot(sync_engine)
    passed(await run(client, key, files))
    assert snapshot(sync_engine) == before


async def test_an_integer_past_its_digits_is_invalid_yaml_not_a_500(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Decimal and hex past 1000 digits are refused at their line; 1000 digits pass."""
    key = await setup(client)
    before = snapshot(sync_engine)
    for value in ("9" * 5000, "-" + "9" * 1001, "0x" + "f" * 5000, "0o" + "7" * 1001):
        test = _event_test("big", value)
        for route in ROUTES[:2]:
            problems = await _problems(client, key, route, claims(test))
            problem = _refused_at(problems, "invalid_yaml", test[0])
            assert problem["line"] == 6, problem
            assert "more than 1000 digits" in problem["message"], problem
    # At the limit the number is read, matched and written as JSON; sexagesimal
    # of 5000 digits is a string now, and 012 is no octal ten.
    for value in ("9" * 1000, "-" + "9" * 1000, "1" + ":59" * 2500, "012", "0o17", "0x1F"):
        passed(await run(client, key, claims(_event_test("fits", value))))
    assert snapshot(sync_engine) == before


async def test_a_float_json_has_not_is_invalid_yaml_not_a_500(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Neither the regex of ``.nan`` nor the plain ``1e999``: every float is checked as built."""
    key = await setup(client)
    before = snapshot(sync_engine)
    for value in (
        "1.0e+999",
        "-1.0e+999",
        "!!float nan",
        "!!float inf",
        "!!float -inf",
        "!!float 1e999",
        ".nan",
        "1:59:59:59:59:59:59:59:59:59:59:59" + ":59" * 200 + ".5",
    ):
        test = _event_test("nan", value)
        for route in ROUTES[:2]:
            problems = await _problems(client, key, route, claims(test))
            problem = _refused_at(problems, "invalid_yaml", test[0])
            assert problem["line"] == 6, (value, problem)
            assert "JSON values only" in problem["message"], (value, problem)
    assert snapshot(sync_engine) == before


async def test_a_scalar_its_tag_cannot_read_is_invalid_yaml_not_a_500(
    client: httpx.AsyncClient,
) -> None:
    """``!!int abc`` was a ValueError, ``!!bool maybe`` a KeyError of the constructor."""
    key = await setup(client)
    for value in ("!!int abc", "!!int 1:30", "!!int 0b1", "!!float abc", "!!bool maybe"):
        test = _event_test("tag", value)
        for route in ROUTES[:2]:
            problems = await _problems(client, key, route, claims(test))
            assert _refused_at(problems, "invalid_yaml", test[0])["line"] == 6, value


async def test_a_number_json_has_not_under_a_data_ref_is_unresolved_not_a_500(
    client: httpx.AsyncClient,
) -> None:
    """The schema a process names by ``$ref``, read as JSON: ``1e999`` and 5000 digits."""
    key = await setup(client)
    process = re.sub(
        r"  data:\n(?:    .*\n)+", "  data: {$ref: ../schemas/data.json}\n", PROCESS, count=1
    )
    assert "$ref" in process

    def files(schema: str) -> list[dict[str, str]]:
        return package(("schemas/data.json", schema), process=process)["files"]  # type: ignore[no-any-return]

    fits = '{"type": "object", "maxProperties": ' + "9" * 1000 + "}"
    for route in ROUTES:
        problems = await _problems(client, key, route, files(fits))
        assert not [p for p in problems if p["code"] == "unresolved_data_ref"], problems
    for schema, message in (
        ('{"type": "object", "maxProperties": 1e999}', "JSON values only"),
        ('{"type": "object", "minimum": -1e999}', "JSON values only"),
        ('{"type": "object", "maxProperties": ' + "9" * 5000 + "}", "more than 1000 digits"),
    ):
        for route in ROUTES:
            problems = await _problems(client, key, route, files(schema))
            problem = _refused_at(problems, "unresolved_data_ref", "processes/sample.yaml")
            assert message in problem["message"], (route, problem)


# --- a gate addressed to a role of the package (CP-ADR-0061, amendment 2026-10-01) ------

APPROVERS = ("roles/approvers.yaml", document("Role", "approvers", {"name": "Approvers"}))
ROLE_GATED = {
    "displayName": "Role-gated",
    "acceptance": [
        {
            "key": "approved",
            "kind": "human",
            "description": "Approved by an approver",
            "spec": {"approverRole": "role:approvers"},
        }
    ],
    "completionSchema": {
        "onComplete": {
            "actions": [
                {
                    "ensureWork": {
                        "type": "claim-review",
                        "key": "after:$.task.id",
                        "title": "After $.task.publicId",
                        "requestApproval": {"assignee": "role:approvers"},
                    }
                }
            ]
        }
    },
}
GATED = ("task-types/role-gated.yaml", document("TaskType", "role-gated", ROLE_GATED))


async def test_a_type_test_checks_who_may_decide_a_gate_addressed_to_a_role(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    holders = {"principals": {"approvers": ["sam"]}}
    before = snapshot(sync_engine)
    body = await run(
        client,
        key,
        claims(
            APPROVERS,
            GATED,
            type_test(
                "holder-decides",
                "role-gated",
                holders,
                [
                    {"complete": {}},
                    {
                        "approve": {
                            "decision": "approved",
                            "by": "bob",
                            "expectRefused": "not_eligible",
                        }
                    },
                    {"approve": {"decision": "approved", "by": "sam"}},
                    {"expect": {"status": {"category": "terminal_success"}}},
                ],
            ),
        ),
    )
    assert snapshot(sync_engine) == before
    passed(body)
    # The type names the package's role by slug: published after the role, no finding.
    assert [p for p in body["problems"] if p["severity"] == "error"] == []


async def test_a_refusal_expected_but_not_given_is_a_failure(client: httpx.AsyncClient) -> None:
    key = await setup(client)
    holders = {"principals": {"approvers": ["sam"]}}
    body = await run(
        client,
        key,
        claims(
            APPROVERS,
            GATED,
            type_test(
                "taken",
                "role-gated",
                holders,
                [
                    {"complete": {}},
                    {
                        "approve": {
                            "decision": "approved",
                            "by": "sam",
                            "expectRefused": "not_eligible",
                        }
                    },
                ],
            ),
            type_test(
                "other-code",
                "role-gated",
                holders,
                [
                    {"complete": {}},
                    {
                        "approve": {
                            "decision": "approved",
                            "by": "bob",
                            "expectRefused": "separation_of_duties_violation",
                        }
                    },
                ],
            ),
            # No gate is pending: there is nobody to refuse.
            type_test(
                "no-gate",
                "claim-review",
                {},
                [{"approve": {"decision": "approved", "by": "bob", "expectRefused": "x"}}],
            ),
            # A refusal nobody expected ends the test, as before.
            type_test(
                "unexpected",
                "role-gated",
                holders,
                [{"complete": {}}, {"approve": {"decision": "approved", "by": "bob"}}],
            ),
        ),
    )
    assert body["status"] == "failed"
    status, first = failure(body, "taken")
    assert (status, first["step"], first["expected"], first["actual"]) == (
        "failed",
        1,
        "not_eligible",
        None,
    )
    assert "a refusal was expected" in first["message"]
    _, first = failure(body, "other-code")
    assert (first["expected"], first["actual"]) == (
        "separation_of_duties_violation",
        "not_eligible",
    )
    _, first = failure(body, "no-gate")
    assert first["step"] == 0 and "no gate is pending" in first["message"]
    _, first = failure(body, "unexpected")
    assert first["actual"]["code"] == "not_eligible"


async def test_without_a_decider_the_holder_of_the_role_is_given(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    body = await run(
        client,
        key,
        claims(
            APPROVERS,
            GATED,
            type_test(
                "anyone",
                "role-gated",
                {},
                [
                    {"complete": {}},
                    {"approve": {"decision": "approved"}},
                    {"expect": {"status": {"category": "terminal_success"}}},
                ],
            ),
        ),
    )
    passed(body)
