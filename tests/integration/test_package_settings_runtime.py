"""``settings`` in processes, rules and views of a package (CP-ADR-0081 §6, G005).

- a process reads the settings of its package: a value saved by ``PUT`` acts
  on the next computation without a plan, an instance already past the step
  keeps what it decided; every record of its journal names the version and
  the schema revision it saw (``settingsVersion``, ``settingsSchemaRevision``);
- the replay of a case after the value changed decides as the case did: the
  values come from the history by the recorded version;
- a rule reads a setting in its condition and its templates; the version is
  an element of the evidence of its evaluation;
- an object without a package refuses ``settings`` at publication, a plan
  finds an undeclared field and a field of a type the place does not take,
  with the path of the expression;
- a package test saves settings in ``given`` and in the middle of a scenario.
"""

import copy
from typing import Any

import httpx
import yaml
from sqlalchemy.engine import Engine

from control_plane.worker.main import Worker
from tests.helpers import auth
from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_test import API_VERSION, snapshot
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _instance,
    _journal,
    _open,
    _setup,
    worker,
)

__all__ = ["worker"]

PACKAGE = "settings-runtime"
PROCESS = "settings-case"
RULE = "settings-flag"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {"type": "number", "minimum": 0, "default": 1000},
        "label": {"type": "string", "maxLength": 50, "default": "std"},
    },
}
MESSAGES = {
    f"{PACKAGE}.title": "Settings runtime",
    f"{PACKAGE}.settings.limit": "Limit",
    f"{PACKAGE}.settings.label": "Label",
}


def process_spec(
    admin: str, *, route: str = "data.amount > settings.limit ? 'big' : 'small'"
) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Settings case",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "route": {"type": "string"},
                "again": {"type": "string"},
                "decision": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "settings.opened"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {"id": "route", "set": {"route": route}},
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": [{"principal": admin}]},
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {
                        "id": "recheck",
                        "set": {"again": "data.amount > settings.limit ? 'big' : 'small'"},
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }


RULE_SPEC: dict[str, Any] = {
    "description": "An amount above the limit of the package is looked at",
    "trigger": {"kind": "observation", "type": "settings.flagged"},
    "condition": {"gt": [{"var": "payload.data.amount"}, {"var": "settings.limit"}]},
    "action": {
        "kind": "ensure_work",
        "taskType": "settings-review",
        "dedupKeyTemplate": "flag:{{payload.data.number}}",
        "fields": {
            "title": "Above {{settings.limit}} ({{settings.label}}): {{payload.data.number}}",
            "customFields": {"limit": "{{settings.limit}}"},
        },
    },
}
REVIEW_TYPE: dict[str, Any] = {
    "displayName": "Settings review",
    "fieldSchema": {"type": "object", "properties": {"limit": {"type": "number"}}},
}


def _document(kind: str, key: str, spec: dict[str, Any]) -> str:
    body = {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}
    return str(yaml.safe_dump(body, allow_unicode=True, sort_keys=False))


def package_files(
    admin: str,
    *,
    process: dict[str, Any] | None = None,
    rule: dict[str, Any] | None = None,
    tests: list[tuple[str, str]] = (),  # type: ignore[assignment]
    settings: bool = True,
    schema: dict[str, Any] | None = None,
    version: str = "1.0.0",
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": version,
        "displayName": "Settings runtime",
        "locales": ["en"],
        "defaultLocale": "en",
    }
    if settings:
        spec["settings"] = {"schema": copy.deepcopy(schema or SCHEMA)}
    files = [
        ("package.yaml", _document("Package", PACKAGE, spec)),
        ("i18n/en.yaml", yaml.safe_dump(MESSAGES)),
        ("processes/case.yaml", _document("Process", PROCESS, process or process_spec(admin))),
        ("task-types/review.yaml", _document("TaskType", "settings-review", REVIEW_TYPE)),
        ("rules/flag.yaml", _document("WorkRule", RULE, rule or RULE_SPEC)),
        *tests,
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _install(client: httpx.AsyncClient, key: str, package: dict[str, Any]) -> None:
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text


async def _save(client: httpx.AsyncClient, key: str, values: dict[str, Any], version: int) -> None:
    response = await client.put(
        f"/api/v1/packages/{PACKAGE}/settings",
        json={"values": values},
        headers={**auth(key), "If-Match": f'"package-settings-{version}"'},
    )
    assert response.status_code == 200, response.text


async def _start(client: httpx.AsyncClient, key: str, number: str, amount: float) -> str:
    response = await client.post(
        "/api/v1/process-instances",
        json={"process": PROCESS, "key": number, "data": {"amount": amount}},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    instance_id: str = response.json()["id"]
    return instance_id


def _seen(journal: list[dict[str, Any]]) -> list[tuple[int, int | None, int | None]]:
    """``(seq, settingsVersion, settingsSchemaRevision)`` of every input of a journal."""
    return [
        (e["seq"], e["data"].get("settingsVersion"), e["data"].get("settingsSchemaRevision"))
        for e in journal
        if e["kind"] == "input"
    ]


# --- processes --------------------------------------------------------------------------------


async def test_a_saved_value_acts_on_the_next_step_and_the_replay_keeps_the_old_decision(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _install(client, key, package_files(admin))

    first = await _start(client, key, "A", 1500)
    instance = await _instance(client, key, first)
    assert instance["data"]["route"] == "big"  # 1500 above the default 1000
    assert _seen(await _journal(client, key, first)) == [(0, 0, 1)]

    # Saved: the next computation reads it, with no plan and no new version of the process.
    await _save(client, key, {"limit": 2000}, 0)
    second = await _start(client, key, "B", 1500)
    assert (await _instance(client, key, second))["data"]["route"] == "small"

    # The first case goes on: its step after the saving reads the new value,
    # the step it took before keeps what it decided.
    await _complete(client, key, _open(instance, "review")["taskId"], {"decision": "ok"})
    await worker.run_once()
    done = await _instance(client, key, first)
    assert done["status"] == "completed", done
    assert (done["data"]["route"], done["data"]["again"]) == ("big", "small")
    seen = _seen(await _journal(client, key, first))
    assert seen[0] == (0, 0, 1) and seen[-1][1:] == (1, 1)

    # The replay of both cases: the values each step saw, from the history.
    response = await client.post(
        f"/api/v1/process-definitions/{PROCESS}:replay",
        json={"spec": process_spec(admin)},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert (report["replayed"], report["diverged"]) == (2, 0), report

    # A new revision of the schema with another default: the first step of the
    # first case saw the default of revision 1, and its replay still does.
    newer = copy.deepcopy(SCHEMA)
    newer["properties"]["limit"]["default"] = 3000
    await _install(client, key, package_files(admin, schema=newer, version="1.1.0"))
    response = await client.post(
        f"/api/v1/process-definitions/{PROCESS}:replay",
        json={"spec": process_spec(admin)},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    assert (response.json()["replayed"], response.json()["diverged"]) == (2, 0), response.json()
    third = await _start(client, key, "C", 2500)
    assert _seen(await _journal(client, key, third)) == [(0, 1, 2)]

    # A candidate that reads the setting otherwise decides otherwise on the first case.
    other = process_spec(admin, route="data.amount > settings.limit * 2.0 ? 'big' : 'small'")
    response = await client.post(
        f"/api/v1/process-definitions/{PROCESS}:replay",
        json={"spec": other, "instanceIds": [first]},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    [diverged] = response.json()["instances"]
    assert [d["kind"] for d in diverged["divergences"]] == ["data"], diverged


async def test_a_process_without_settings_keeps_its_journal_as_it_was(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    plain = process_spec(admin, route="data.amount > 1000.0 ? 'big' : 'small'")
    plain["stages"][0]["steps"][2]["set"] = {"again": "'same'"}
    await _install(client, key, package_files(admin, process=plain))
    started = await _start(client, key, "A", 1500)
    journal = await _journal(client, key, started)
    assert _seen(journal) == [(0, None, None)]
    assert all("settingsVersion" not in e["data"] for e in journal)
    definition = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert definition.json()["engineRevision"] == 2


async def test_an_object_without_a_package_refuses_settings_at_publication(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    published = await client.post(
        "/api/v1/process-definitions",
        json={"key": "hand-made", "spec": process_spec(admin)},
        headers=auth(key),
    )
    assert published.status_code == 422, published.text
    error = published.json()["error"]
    assert error["code"] == "invalid_process"
    found = {(p["code"], p["path"]) for p in error["details"]["problems"]}
    assert found == {
        ("settings_ref_unknown", "/spec/stages/0/steps/0/set/route"),
        ("settings_ref_unknown", "/spec/stages/0/steps/2/set/again"),
    }
    assert all(
        "not from a package" in p["message"]
        for p in error["details"]["problems"]
        if p["code"] == "settings_ref_unknown"
    )

    review = await client.post(
        "/api/v1/task-types",
        json={"key": "settings-review", **REVIEW_TYPE},
        headers=auth(key),
    )
    assert review.status_code == 201, review.text
    rule = await client.post(
        "/api/v1/rules", json={"key": "hand-made", **RULE_SPEC}, headers=auth(key)
    )
    assert rule.status_code == 422, rule.text
    assert rule.json()["error"]["code"] == "settings_ref_unknown"
    assert rule.json()["error"]["details"]["field"] == "condition.gt[1].var"


async def test_the_plan_finds_an_undeclared_field_and_a_type_that_does_not_fit(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    process = process_spec(admin, route="settings.missing")
    process["stages"][0]["steps"][2]["set"] = {"again": "settings.limit"}  # a number as a string
    rule = copy.deepcopy(RULE_SPEC)
    rule["condition"] = {"in": [{"var": "payload.data.amount"}, {"var": "settings.limit"}]}
    plan = await _plan(client, key, package_files(admin, process=process, rule=rule))
    found = {
        (p["code"], p["file"], p["path"])
        for p in plan["problems"]
        if p["code"].startswith("settings_ref")
    }
    assert found == {
        ("settings_ref_unknown", "processes/case.yaml", "/spec/stages/0/steps/0/set/route"),
        ("settings_ref_type", "processes/case.yaml", "/spec/stages/0/steps/2/set/again"),
        ("settings_ref_type", "rules/flag.yaml", "/spec/condition/in/1/var"),
    }
    # A package that declares no settings: every read is unknown.
    bare = await _plan(client, key, package_files(admin, settings=False))
    assert {p["code"] for p in bare["problems"] if p["severity"] == "error"} == {
        "settings_ref_unknown"
    }


# --- rules ------------------------------------------------------------------------------------


async def _observe(client: httpx.AsyncClient, key: str, number: str, amount: float) -> None:
    response = await client.post(
        "/api/v1/observations",
        json={
            "kind": "settings.flagged",
            "content": "flagged",
            "data": {"number": number, "amount": amount},
        },
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


async def _evaluations(client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    rules = await client.get("/api/v1/rules", params={"key": RULE}, headers=auth(key))
    assert rules.status_code == 200, rules.text
    [rule] = [r for r in rules.json()["items"] if r["key"] == RULE]
    response = await client.get(f"/api/v1/rules/{rule['id']}/evaluations", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_a_rule_reads_a_setting_and_its_evaluation_names_the_version(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _install(client, key, package_files(admin))

    await _observe(client, key, "A", 1500)
    await worker.run_once()
    tasks = await client.get("/api/v1/tasks", params={"limit": 50}, headers=auth(key))
    [filed] = [t for t in tasks.json()["items"] if t["title"].startswith("Above")]
    assert filed["title"] == "Above 1000 (std): A"
    assert filed["customFields"] == {"limit": 1000}
    # The task cites the fact, not the settings read.
    assert all(e["kind"] != "settings" for e in filed.get("evidence") or [])

    await _save(client, key, {"limit": 2000, "label": "high"}, 0)
    await _observe(client, key, "B", 1500)
    await worker.run_once()
    evaluations = await _evaluations(client, key)
    pairs = sorted(
        (e["result"].get("conditionMatched"), item["version"])
        for e in evaluations
        for item in e["evidence"]
        if item["kind"] == "settings"
    )
    assert pairs == [(False, 1), (True, 0)]
    assert all(
        item
        == {"kind": "settings", "package": PACKAGE, "version": item["version"], "schemaRevision": 1}
        for e in evaluations
        for item in e["evidence"]
        if item["kind"] == "settings"
    )


# --- the sandbox of package tests -------------------------------------------------------------


def _scenario(name: str, given: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[str, str]:
    data = {"name": name, "process": PROCESS, "given": given, "steps": steps}
    return (f"tests/{name}.test.yaml", yaml.safe_dump(data, sort_keys=False))


async def test_a_package_test_saves_settings_in_given_and_in_the_middle_of_a_scenario(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    changed = _scenario(
        "changed",
        {"data": {"amount": 1500}, "settings": {"limit": 5000}},
        [
            {"expect": {"data": {"route": "small"}}},
            {"settings": {"limit": 10}},
            {"complete": {"step": "review", "output": {"decision": "ok"}}},
            {"expect": {"data": {"route": "small", "again": "big"}, "status": "completed"}},
        ],
    )
    defaults = _scenario(
        "defaults",
        {"data": {"amount": 1500}},
        [{"expect": {"data": {"route": "big"}}}],
    )
    invalid = _scenario(
        "invalid",
        {"data": {"amount": 1500}},
        [{"settings": {"limit": -1}}],
    )
    before = snapshot(sync_engine)
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package_files(admin, tests=[changed, defaults, invalid])},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    results = {t["name"]: t for t in body["tests"]}
    assert results["changed"]["status"] == "passed", results["changed"]
    assert results["defaults"]["status"] == "passed", results["defaults"]
    assert results["invalid"]["status"] == "failed"
    [failure] = results["invalid"]["failures"]
    assert failure["message"].startswith("steps[0].settings: settings_invalid")
    assert failure["actual"] == [
        {"path": "/limit", "code": "minimum", "message": failure["actual"][0]["message"]}
    ]
