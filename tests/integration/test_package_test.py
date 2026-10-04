"""``POST /packages:test`` at work (CP-ADR-0074 §10; process-packages P013).

The package goes as its files; the core checks every object, runs the tests
in its sandbox and reports coverage — and writes nothing: every table of the
database is the same, row for row, after the run as before it.
"""

import contextlib
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import false, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.commands import package_test
from control_plane.infrastructure.db.models import Role
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap
from tests.integration.test_process_definitions import _catalog

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
# The sample's exit guard holds before anyone decided (null != ''): the review
# stage would close at once. The package under test waits for the decision.
PROCESS = (
    (FIXTURES / "sample.process.yaml")
    .read_text(encoding="utf-8")
    .replace("exit: data.decision != ''", "exit: has(data.decision)")
)
CALENDAR = (FIXTURES / "ru-2024.calendar.yaml").read_text(encoding="utf-8")
API_VERSION = yaml.safe_load(CALENDAR)["apiVersion"]  # the catalog format of the fixtures
MANIFEST = (
    f"apiVersion: {API_VERSION}\nkind: Package\nkey: sample\n"
    "spec: {version: 1.0.0, displayName: Sample}\n"
)


def scenario(summary: Any = "N-1: yes") -> dict[str, Any]:
    return {
        "process": "sample",
        "name": "a small amount is reviewed and reported",
        "given": {"clock": "2024-03-01T09:00:00Z", "principals": {"lead": ["alice"]}},
        "mocks": {
            "recall": [
                {"step": "history", "output": {"nodes": [{"kind": "case", "key": "sample:0"}]}}
            ],
            "skills": {"text.summarize@1": [{"output": {"summary": summary}}]},
        },
        "steps": [
            {
                "emit": {
                    "observation": "sample.opened",
                    "payload": {"number": "N-1", "amount": 10, "deadline": "2024-03-15T09:00:00Z"},
                }
            },
            {
                "expect": {
                    "stages": {"review": "open", "report": "not_started"},
                    "tasks": [
                        {"step": "decide", "assignee": "alice", "due": "2024-03-13T09:00:00Z"}
                    ],
                    "memory": {"recalled": ["history"]},
                    "data": {"level": 1},
                }
            },
            {"complete": {"step": "decide", "by": "alice", "output": {"decision": "yes"}}},
            {
                "expect": {
                    "status": "completed",
                    "outcome": "done",
                    "data": {"summary": "N-1: yes"},
                    "noSideEffects": True,
                }
            },
        ],
    }


def package(*extra: tuple[str, str], process: str = PROCESS, test: Any = None) -> dict[str, Any]:
    files = [
        ("package.yaml", MANIFEST),
        ("processes/sample.yaml", process),
        ("tests/review.test.yaml", yaml.safe_dump(test or scenario(), allow_unicode=True)),
        *extra,
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


def snapshot(engine: Engine) -> dict[str, str]:
    """Every table of the database, row for row, as one digest per table."""
    with engine.connect() as conn:
        tables = conn.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).all()
        return {
            table: conn.scalar(
                text(
                    f"SELECT md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), ''))"
                    f' FROM "{table}" t'
                )
            )
            for table in tables
        }


async def _setup(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    await _catalog(client, key)
    return key


async def test_a_package_is_tested_with_coverage_and_nothing_is_written(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _setup(client)
    before = snapshot(sync_engine)

    response = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )

    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    assert body["status"] == "passed", body["tests"][0]["failures"]
    assert body["checkOnly"] is False
    (test,) = body["tests"]
    assert (test["file"], test["process"], test["status"], test["failures"]) == (
        "tests/review.test.yaml",
        "sample",
        "passed",
        [],
    )
    (coverage,) = body["coverage"]
    assert (coverage["process"], coverage["version"]) == ("sample", 1)
    assert coverage["elements"]["missing"] == []
    assert coverage["decisionRows"] == {"covered": 1, "total": 2, "missing": ["level/1"]}
    assert "history:timeout" in coverage["transitions"]["missing"]
    # Memory is not configured here: the regulations are one warning, not a refusal.
    (warning,) = body["problems"]
    assert (warning["code"], warning["severity"], warning["file"], warning["line"]) == (
        "governed_by_unchecked",
        "warning",
        "processes/sample.yaml",
        7,  # the line of spec:
    )


async def test_check_only_runs_no_test(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    response = await client.post(
        "/api/v1/packages:test?checkOnly=true", json={"package": package()}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["checkOnly"], body["tests"], body["coverage"]) == (
        "passed",
        True,
        [],
        [],
    )


async def test_an_invalid_package_names_the_file_and_line_and_runs_no_test(
    client: httpx.AsyncClient,
) -> None:
    key = await _setup(client)
    broken = PROCESS.replace("taskType: review", "taskType: reveiw")
    response = await client.post(
        "/api/v1/packages:test", json={"package": package(process=broken)}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invalid" and body["tests"] == [] and body["coverage"] == []
    (error,) = [p for p in body["problems"] if p["severity"] == "error"]
    assert (error["code"], error["file"]) == ("unknown_task_type", "processes/sample.yaml")
    assert PROCESS.splitlines()[error["line"] - 1].strip() == "taskType: review"


async def test_a_mock_off_the_skill_schema_fails_the_test(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(test=scenario(summary=42))},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    (failure,) = body["tests"][0]["failures"]
    assert failure["step"] == 2
    assert "text.summarize@1" in failure["message"] and failure["actual"] == {"summary": 42}


async def test_the_package_brings_what_its_process_names(client: httpx.AsyncClient) -> None:
    """Task type, skill, agent and calendar of the package are known before it is applied."""
    key: str = (await do_bootstrap(client))["apiKey"]["key"]

    def document(kind: str, name: str, spec: dict[str, Any]) -> str:
        return str(
            yaml.safe_dump({"apiVersion": API_VERSION, "kind": kind, "key": name, "spec": spec})
        )

    objects = (
        (
            "agents/sample-process.yaml",
            document(
                "Agent",
                "sample-process",
                {
                    "displayName": "S",
                    "identity": {"kind": "service", "permissions": ["tasks.read"]},
                    "placement": "none",
                },
            ),
        ),
        (
            "task-types/review.yaml",
            document(
                "TaskType",
                "review",
                {
                    "displayName": "Review",
                    "fieldSchema": {
                        "type": "object",
                        "properties": {"decision": {"type": "string"}},
                    },
                },
            ),
        ),
        (
            "skills/summarize.yaml",
            document(
                "Skill",
                "text.summarize",
                {
                    "version": "1",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                    },
                },
            ),
        ),
        ("calendars/ru.yaml", CALENDAR),
    )
    alone = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )
    assert alone.json()["status"] == "invalid"
    response = await client.post(
        "/api/v1/packages:test", json={"package": package(*objects)}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed", response.json()


async def test_testing_a_package_needs_its_permission(client: httpx.AsyncClient) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, outsider = await create_agent_with_key(
        client, admin, name="outsider", permissions=["tasks.read"]
    )
    denied = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(outsider)
    )
    assert denied.status_code == 403, denied.text


async def test_a_read_through_raw_sql_is_no_side_effect(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """In a workspace the regulations read its ancestors through ``text(WITH RECURSIVE ...)``."""
    key = await _setup(client)
    workspace = await create_workspace(client, key, "tenders")
    process = PROCESS.replace("  version: 1\n", "  version: 1\n  workspaceId: ${workspace}\n", 1)
    before = snapshot(sync_engine)

    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(process=process), "workspaceId": workspace["id"]},
        headers=auth(key),
    )

    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    assert body["status"] == "passed", body["tests"][0]["failures"]


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda: update(Role).where(false()).values(name="x"), id="orm"),
        pytest.param(lambda: text("DELETE FROM roles WHERE false"), id="text"),
    ],
)
async def test_an_attempt_to_write_is_a_side_effect(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, write: Any
) -> None:
    """A write the run attempts counts, though the READ ONLY transaction refuses it."""
    key = await _setup(client)
    role_slugs = package_test._role_slugs

    async def writing(session: AsyncSession, *args: Any) -> frozenset[str]:
        with contextlib.suppress(DBAPIError):
            async with session.begin_nested():
                await session.execute(write())
        return await role_slugs(session, *args)

    monkeypatch.setattr(package_test, "_role_slugs", writing)
    response = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    (failure,) = body["tests"][0]["failures"]
    assert "wrote outside the sandbox" in failure["message"] and failure["actual"] == 1


async def test_the_check_of_a_package_sees_the_calendar_of_a_due(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0078 §1 (P009): the refusals of publication, before the package is applied."""
    key = await _setup(client)
    process = PROCESS.replace(
        'due: {at: "cal.addWorkdays(data.deadline, -2)"}', "due: {workhours: 8}"
    )
    assert process != PROCESS
    here = "/spec/stages/0/steps/2/human/due/workhours"

    async def errors(*extra: tuple[str, str], text: str = process) -> list[tuple[str, str]]:
        response = await client.post(
            "/api/v1/packages:test?checkOnly=true",
            json={"package": package(*extra, process=text)},
            headers=auth(key),
        )
        assert response.status_code == 200, response.text
        return [
            (p["code"], p["path"]) for p in response.json()["problems"] if p["severity"] == "error"
        ]

    # The calendar ru of the catalog, and the one the package brings, have no hours.
    assert await errors() == [("sla_calendar_without_hours", here)]
    assert await errors(("calendars/ru.yaml", CALENDAR)) == [("sla_calendar_without_hours", here)]
    assert await errors(text=process.replace("  calendar: ru\n", "")) == [
        ("sla_calendar_missing", here)
    ]
    # The package's version of ru declares working hours: the one the process will see.
    with_hours = (FIXTURES / "ru-2025-2027.calendar.yaml").read_text(encoding="utf-8")
    assert await errors(("calendars/ru.yaml", with_hours)) == []


async def test_expect_sla_counts_by_the_calendar_the_package_brings(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0078 §7 (P015): the deadline of a test is the live one, by the package's calendar.

    The catalog's ``ru`` has no working hours; the package brings the ``ru``
    that has them. Eight working hours from Wednesday 2025-04-30 15:00 MSK —
    a short day before the holidays of 1-2 May and the weekend — end on
    Monday 05-05 at 15:00 MSK, warned two working hours before.
    """
    key = await _setup(client)
    process = PROCESS.replace(
        'due: {at: "cal.addWorkdays(data.deadline, -2)"}',
        "due: {workhours: 8, warnBefore: {workhours: 2}}",
    )
    assert process != PROCESS
    test = scenario()
    test["given"]["clock"] = "2025-04-30T12:00:00Z"
    opened, _, decide, done = test["steps"]
    test["steps"] = [
        opened,
        {"expect": {"sla": {"decide": "ok"}, "events": ["process.step_entered"]}},
        {"advance": "P3D"},
        {"expect": {"sla": {"decide": "ok"}}},
        {"advance": "PT46H"},
        {"expect": {"sla": {"decide": "warning"}, "events": ["process.sla_warning"]}},
        {"advance": "PT2H"},
        {"expect": {"sla": {"decide": "breached"}, "events": ["process.sla_breached"]}},
        decide,
        {**done, "expect": {**done["expect"], "events": ["process.step_exited"]}},
    ]
    with_hours = (FIXTURES / "ru-2025-2027.calendar.yaml").read_text(encoding="utf-8")
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(("calendars/ru.yaml", with_hours), process=process, test=test)},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "passed", body

    # One hour short of the warning the state is still ok: the failure names the step.
    test["steps"][4] = {"advance": "PT45H"}
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(("calendars/ru.yaml", with_hours), process=process, test=test)},
        headers=auth(key),
    )
    failures = response.json()["tests"][0]["failures"]
    assert [(f["step"], f["expected"], f["actual"]) for f in failures] == [
        (5, "warning", "ok"),
        (5, "process.sla_warning", []),
        (7, "breached", "warning"),
        (7, "process.sla_breached", ["process.sla_warning"]),
    ]
    assert failures[0]["message"].startswith("SLA of step 'decide' at 2025-05-05T09:00:00Z")


async def test_the_author_of_an_emitted_event_reaches_the_start_of_the_process(
    client: httpx.AsyncClient,
) -> None:
    """``emit.by`` is the event's ``actorId``: a process may read it in ``start.set``."""
    key = await _setup(client)
    process = PROCESS.replace(
        "      summary: {type: string}\n",
        "      summary: {type: string}\n      opener: {type: string}\n",
    ).replace(
        "      number: string(event.payload.number)\n",
        "      number: string(event.payload.number)\n      opener: string(event.actorId)\n",
    )
    assert process.count("opener") == 2

    async def tested(by: Any) -> dict[str, Any]:
        test = scenario()
        if by is not None:
            test["steps"][0]["emit"]["by"] = by
        test["steps"][1]["expect"].update(status="running")
        test["steps"][1]["expect"]["data"]["opener"] = "alice"
        response = await client.post(
            "/api/v1/packages:test",
            json={"package": package(process=process, test=test)},
            headers=auth(key),
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    body = await tested("alice")
    assert body["status"] == "passed", body["tests"]
    # Without ``by`` the event has no actorId, as before: the start fails on it.
    body = await tested(None)
    assert body["status"] == "failed"
    failures = body["tests"][0]["failures"]
    assert ("status of the instance", "running", "failed") in [
        (f["message"], f["expected"], f["actual"]) for f in failures
    ]
    # An empty or non-string author is a finding of the file, and no test runs.
    for wrong in ("", 7):
        body = await tested(wrong)
        assert body["status"] == "invalid" and body["tests"] == [], body
        (error,) = [p for p in body["problems"] if p["severity"] == "error"]
        assert (error["code"], error["file"]) == ("invalid_test", "tests/review.test.yaml")


async def test_a_rule_of_the_package_closes_the_task_its_observation_names(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """CP-ADR-0063 Zh5 (I013): an observation bound to a step task, a rule with target: task."""
    key = await _setup(client)
    test = scenario()
    test["steps"] = [
        test["steps"][0],
        {"emit": {"observation": "sample.closed", "task": "decide", "payload": {"state": "done"}}},
        {
            "expect": {
                "tasks": [{"step": "decide", "status": "completed"}],
                "rules": [{"rule": "sample-closed", "result": "matched", "step": "decide"}],
                "noSideEffects": True,
            }
        },
    ]
    rule = (
        f"apiVersion: {API_VERSION}\nkind: WorkRule\nkey: sample-closed\nspec:\n"
        "  trigger: {kind: observation, type: sample.closed, agent: sample-observer}\n"
        "  action: {kind: complete_work, target: task, taskTypes: [review]}\n"
    )
    before = snapshot(sync_engine)
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(("rules/sample-closed.yaml", rule), test=test)},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    assert body["status"] == "passed", (body["problems"], body["tests"][0]["failures"])

    broken = rule.replace("taskTypes: [review]", "dedupKeyTemplate: k")
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(("rules/sample-closed.yaml", broken), test=test)},
        headers=auth(key),
    )
    body = response.json()
    assert body["status"] == "invalid"
    [problem] = [p for p in body["problems"] if p["file"] == "rules/sample-closed.yaml"]
    assert (problem["code"], problem["path"]) == (
        "invalid_rule_action",
        "/spec/action/dedupKeyTemplate",
    )
    assert body["tests"] == []
