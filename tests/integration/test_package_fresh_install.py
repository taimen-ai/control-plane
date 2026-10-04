"""A package on a fresh stand, and the tests of a rule the stand already has (TASK-001197).

Found by the claims example (S027):

- the plan checked the task types and rules of a package against the skills
  the stand had, not the skills of the same package: a package that brings
  a skill and uses it in a rule or an outcome did not plan on a fresh stand
  (``unknown_skill``, ``invalid_approval_schema``). The trial of the plan now
  publishes the artifact types, roles and skills of the package the tenant
  lacks first, in the transaction it rolls back; the installer applies them
  (``outside``) before ``packages:apply``, which plans the same;
- a rule test run by the server on a stand where the rule is installed saw
  its input skipped: the default clock of a test is earlier than the moment
  the rule was enabled there. In a test the rule is enabled from the start of
  the test's time.
"""

import copy
import json
from typing import Any

import httpx
import yaml
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key
from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_test import snapshot
from tests.integration.test_package_test_subjects import (
    CLAIM_REOPENED_TEST,
    CLAIM_REVIEW,
    a_test,
    by_file,
    document,
    helpdesk,
    run,
    setup,
)

LETTER = {"displayName": "Claim letter", "mediaTypes": ["text/*"]}
LEAD = {"name": "Claims lead"}
# The installer applies these kinds through their routes (``outside``).
ROUTES = {
    "ArtifactType": ("/api/v1/artifact-types", "key"),
    "Role": ("/api/v1/roles", "slug"),
    "Skill": ("/api/v1/skills", "name"),
}


def _files(*tests: tuple[str, str], skills: bool = True) -> list[dict[str, str]]:
    """The helpdesk package with an artifact type its review hands in and a role of the tenant."""
    review = {**CLAIM_REVIEW, "artifactSchema": {"outputs": [{"key": "letter", "type": "letter"}]}}
    files = [
        f
        for f in helpdesk(*tests)
        if f["path"] != "task-types/claim-review.yaml"
        and (skills or not f["path"].startswith("skills/"))
    ]
    files += [
        {
            "path": "task-types/claim-review.yaml",
            "content": document("TaskType", "claim-review", review),
        },
        {
            "path": "artifact-types/letter.yaml",
            "content": document("ArtifactType", "letter", LETTER),
        },
        {"path": "roles/lead.yaml", "content": document("Role", "claims-lead", LEAD)},
    ]
    return files


def _package(files: list[dict[str, str]]) -> dict[str, Any]:
    return {"files": [f for f in files if not f["path"].startswith("tests/")]}


async def _install_outside(client: httpx.AsyncClient, key: str, package: dict[str, Any]) -> None:
    """What package-sdk applies before ``packages:apply``: the kinds the core does not plan."""
    for item in package["files"]:
        body = yaml.safe_load(item["content"])
        if body["kind"] not in ROUTES:
            continue
        path, identity = ROUTES[body["kind"]]
        response = await client.post(
            path, json={identity: body["key"], **body["spec"]}, headers=auth(key)
        )
        assert response.status_code == 201, response.text


async def _install(client: httpx.AsyncClient, key: str, package: dict[str, Any]) -> None:
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    await _install_outside(client, key, package)
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text


def _codes(plan: dict[str, Any], severity: str) -> set[tuple[str, str | None]]:
    return {(p["code"], p["file"]) for p in plan["problems"] if p["severity"] == severity}


# --- a fresh stand: one plan -----------------------------------------------------------------


async def test_a_package_that_brings_its_skills_plans_and_applies_on_a_fresh_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    package = _package(_files())
    before = snapshot(sync_engine)

    plan = await _plan(client, key, package)

    assert snapshot(sync_engine) == before, "the plan writes nothing"
    assert plan["problems"] == [], json.dumps(plan["problems"], ensure_ascii=False, indent=1)
    assert [(c["kind"], c["key"], c["action"]) for c in plan["changes"]] == [
        ("TaskType", "claim-review", "create"),
        ("TaskType", "refund-approval", "create"),
        ("WorkRule", "claim-reopened", "create"),
    ]
    assert plan["outside"] == [
        {"kind": "ArtifactType", "key": "letter", "appliedBy": "installer"},
        {"kind": "Role", "key": "claims-lead", "appliedBy": "installer"},
        {"kind": "Skill", "key": "claims.classify", "appliedBy": "installer"},
        {"kind": "Skill", "key": "helpdesk.reply", "appliedBy": "installer"},
    ]

    # The installer applies what is outside, then the plan itself: its hash still holds.
    await _install_outside(client, key, package)
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert [(a["kind"], a["key"], a["action"]) for a in applied.json()["applied"]] == [
        ("TaskType", "claim-review", "create"),
        ("TaskType", "refund-approval", "create"),
        ("WorkRule", "claim-reopened", "create"),
    ]
    rules = (await client.get("/api/v1/rules", headers=auth(key))).json()["items"]
    assert [(r["key"], r["status"]) for r in rules] == [("claim-reopened", "enabled")]

    # Once installed, the same package plans unchanged, the skills are the stand's now.
    again = await _plan(client, key, package)
    assert again["problems"] == []
    assert {c["action"] for c in again["changes"]} == {"unchanged"}


async def test_a_skill_neither_on_the_stand_nor_in_the_package_is_still_a_finding(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    plan = await _plan(client, key, _package(_files(skills=False)))
    assert _codes(plan, "error") == {
        ("invalid_approval_schema", "task-types/refund-approval.yaml"),
        ("unknown_skill", "rules/claim-reopened.yaml"),
    }


async def test_an_apply_before_the_installer_applied_the_skills_is_refused_and_writes_nothing(
    client: httpx.AsyncClient,
) -> None:
    """The plan assumes the supporting objects of the package; the apply does not publish them."""
    key = await setup(client)
    package = _package(_files())
    plan = await _plan(client, key, package)
    assert _errors(plan) == []

    applied = await _apply(client, key, package, plan["planHash"])

    # The command of the first object refuses: claim-review hands in a letter.
    assert applied.status_code == 422, applied.text
    assert applied.json()["error"]["code"] == "unknown_artifact_type"
    types = (await client.get("/api/v1/task-types", headers=auth(key))).json()["items"]
    assert {t["key"] for t in types}.isdisjoint({"claim-review", "refund-approval"})


async def test_a_supporting_object_of_an_invalid_shape_is_a_warning_and_its_users_fail(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    files = _files()
    broken = yaml.safe_load(
        next(f["content"] for f in files if f["path"] == "skills/classify.yaml")
    )
    broken["spec"]["riskLevels"] = "low"  # not a field of POST /skills
    for item in files:
        if item["path"] == "skills/classify.yaml":
            item["content"] = yaml.safe_dump(broken, sort_keys=False)

    plan = await _plan(client, key, _package(files))

    assert ("invalid_skill", "skills/classify.yaml") in _codes(plan, "warning")
    assert _codes(plan, "error") == {("unknown_skill", "rules/claim-reopened.yaml")}


async def test_without_the_right_of_a_supporting_kind_only_its_users_go_unchecked(
    client: httpx.AsyncClient,
) -> None:
    """Review of TASK-001197: a supporting object the caller may not publish stopped the trial."""
    admin = await setup(client)
    _, key = await create_agent_with_key(
        client,
        admin,
        name="planner",
        permissions=[
            "packages.plan",
            "task_types.manage",
            "rules.write",
            "artifact_types.manage",
        ],
    )
    plan = await _plan(client, key, _package(_files()))
    # Roles and skills are org.manage: the objects naming a skill are not tried, nothing
    # false is found; claim-review names only the artifact type the trial published.
    assert _codes(plan, "warning") == {
        ("permission_required", "roles/lead.yaml"),
        ("permission_required", "skills/classify.yaml"),
        ("permission_required", "skills/reply.yaml"),
        ("permission_required", "task-types/refund-approval.yaml"),
        ("permission_required", "rules/claim-reopened.yaml"),
    }
    assert _errors(plan) == []


async def test_a_type_the_trial_could_try_is_still_checked_when_a_supporting_right_is_missing(
    client: httpx.AsyncClient,
) -> None:
    """The objects that name no gap are tried: a broken one is still an error."""
    admin = await setup(client)
    _, key = await create_agent_with_key(
        client,
        admin,
        name="planner",
        permissions=["packages.plan", "task_types.manage", "rules.write", "artifact_types.manage"],
    )
    files = _files()
    review = yaml.safe_load(
        next(f["content"] for f in files if f["path"] == "task-types/claim-review.yaml")
    )
    review["spec"]["artifactSchema"]["outputs"][0]["type"] = "no-such-type"
    for item in files:
        if item["path"] == "task-types/claim-review.yaml":
            item["content"] = yaml.safe_dump(review, sort_keys=False)

    plan = await _plan(client, key, _package(files))

    assert ("unknown_artifact_type", "task-types/claim-review.yaml") in _codes(plan, "error")


# --- a rule test on a stand that has the rule ---------------------------------------------


async def test_a_rule_test_passes_on_a_stand_where_the_rule_is_installed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await setup(client)
    files = _files(("tests/claim-reopened.test.yaml", CLAIM_REOPENED_TEST))
    await _install(client, key, _package(files))
    before = snapshot(sync_engine)

    body = await run(client, key, files)

    assert snapshot(sync_engine) == before, "the rule of the stand is as it was"
    assert body["status"] == "passed", json.dumps(body, ensure_ascii=False, indent=1)
    assert [p["code"] for p in body["problems"]] == []


async def test_a_rule_test_with_a_clock_after_the_enabling_still_passes(
    client: httpx.AsyncClient,
) -> None:
    key = await setup(client)
    installed = _files()
    await _install(client, key, _package(installed))
    later = copy.deepcopy(yaml.safe_load(CLAIM_REOPENED_TEST))
    later["given"]["clock"] = "2099-01-01T00:00:00Z"
    earlier = copy.deepcopy(later)
    earlier["given"]["clock"] = "2020-01-01T00:00:00Z"

    body = await run(client, key, _files(a_test("later", later), a_test("earlier", earlier)))

    tests = by_file(body)
    assert tests["tests/later.test.yaml"]["status"] == "passed", tests
    assert tests["tests/earlier.test.yaml"]["status"] == "passed", tests
