"""A closing rule reads back as its file: a repeated plan changes nothing (TASK-001373).

CP-ADR-0063, amendment Z1. On staging the plan of package-sdk showed
``will change action`` for the two closing rules of the selfdev package
(``oss-check-resolved``, ``submodule-lag-resolved``) after every apply: the
installer without the core's code compares the file's ``action`` with
``GET /rules/{id}``, and the core stored ``"fields": {}`` the file never has.
The rules below have the shape of those two: ``complete_work`` per item, a
dedup key, no fields.
"""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import Engine, text

from tests.helpers import auth, do_bootstrap
from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_test import API_VERSION

# The revision before the action stopped carrying empty fields.
BEFORE_FORM = "f2c6a8d4e1b9"

TRIGGER = {"kind": "observation", "type": "repo.commit_observed"}
CONDITION = {"eq": [{"var": "payload.data.repo"}, "sample-superproject"]}
# Keys and actions as the files of the package carry them.
RULES = {
    "sample-check-resolved": {
        "kind": "complete_work",
        "forEach": "payload.data.current",
        "dedupKeyTemplate": "sample-check:{{item.component}}",
    },
    "sample-lag-resolved": {
        "kind": "complete_work",
        "forEach": "payload.data.current",
        "dedupKeyTemplate": "sample-lag:{{item.path}}",
    },
}


def _spec(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "The work is done outside",
        "trigger": TRIGGER,
        "condition": CONDITION,
        "action": action,
    }


def _package() -> dict[str, Any]:
    manifest = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample-closing",
        "spec": {"version": "1.0.0", "displayName": "Sample closing rules"},
    }
    files = [("package.yaml", yaml.safe_dump(manifest))]
    for key, action in RULES.items():
        document = {
            "apiVersion": API_VERSION,
            "kind": "WorkRule",
            "key": key,
            "spec": _spec(action),
        }
        files.append((f"rules/{key}.yaml", yaml.safe_dump(document, sort_keys=False)))
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _admin(client: httpx.AsyncClient) -> str:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    return key


async def _rules(client: httpx.AsyncClient, key: str) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/v1/rules", headers=auth(key))
    assert response.status_code == 200, response.text
    return {r["key"]: r for r in response.json()["items"] if r["status"] != "archived"}


def _canonical(value: Any) -> str:
    # How package-sdk compares a field of a rule (model.canonical).
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _installer_changes(file_action: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """The fields package-sdk plans to change when it cannot import the core's code."""
    return ["action"] if _canonical(file_action) != _canonical(live["action"]) else []


async def test_a_repeated_plan_after_the_apply_changes_no_closing_rule(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    package = _package()
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    assert {(c["key"], c["action"]) for c in plan["changes"]} == {
        (rule, "create") for rule in RULES
    }
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text

    for _ in range(2):
        again = await _plan(client, key, package)
        assert _errors(again) == [], again["problems"]
        assert sorted((c["key"], c["action"], c["fields"]) for c in again["changes"]) == [
            (rule, "unchanged", []) for rule in sorted(RULES)
        ]
        repeated = await _apply(client, key, package, again["planHash"])
        assert repeated.status_code == 200, repeated.text
        assert {a["key"]: a["version"] for a in repeated.json()["applied"]} == {
            rule: 1 for rule in RULES
        }

    live = await _rules(client, key)
    for rule, action in RULES.items():
        # What the installer compares: the file's action and the rule read back.
        assert live[rule]["action"] == action
        assert _installer_changes(action, live[rule]) == []


async def test_the_installer_path_converges_after_one_apply(client: httpx.AsyncClient) -> None:
    """package-sdk applies by the ordinary routes and plans by ``GET /rules`` (no core code)."""
    key = await _admin(client)
    for rule, action in RULES.items():
        response = await client.post(
            "/api/v1/rules", json={"key": rule, **_spec(action)}, headers=auth(key)
        )
        assert response.status_code == 201, response.text

    live = await _rules(client, key)
    assert {rule: _installer_changes(action, live[rule]) for rule, action in RULES.items()} == {
        rule: [] for rule in RULES
    }
    # A patch of the same action is no change: the version stays.
    for rule, action in RULES.items():
        response = await client.patch(
            f"/api/v1/rules/{live[rule]['id']}",
            json={"action": action},
            headers={**auth(key), "If-Match": f'"rule-{live[rule]["version"]}"'},
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        assert response.json()["action"] == action
    # The core's own plan of the file agrees.
    plan = await _plan(client, key, _package())
    assert [c["action"] for c in plan["changes"]] == ["unchanged"] * len(RULES)


async def test_explicit_empty_fields_are_stored_as_no_fields(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    [(rule, action)] = list(RULES.items())[:1]
    response = await client.post(
        "/api/v1/rules",
        json={"key": rule, **_spec({**action, "fields": {}})},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    assert response.json()["action"] == action
    # Fields that name something are kept as written.
    response = await client.post(
        "/api/v1/rules",
        json={
            "key": "sample-update",
            **_spec(
                {
                    "kind": "update_work",
                    "dedupKeyTemplate": "sample:{{payload.data.repo}}",
                    "fields": {"priority": "high"},
                }
            ),
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    assert response.json()["action"]["fields"] == {"priority": "high"}


# --- the migration ---------------------------------------------------------------------------


@pytest.fixture
def alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_the_migration_drops_empty_fields_and_goes_back(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    key = await _admin(client)
    [(closing, action)] = list(RULES.items())[:1]
    created = await client.post(
        "/api/v1/rules", json={"key": closing, **_spec(action)}, headers=auth(key)
    )
    assert created.status_code == 201, created.text
    filed = {
        "kind": "update_work",
        "dedupKeyTemplate": "sample:{{payload.data.repo}}",
        "fields": {"priority": "high"},
    }
    created = await client.post(
        "/api/v1/rules", json={"key": "sample-update", **_spec(filed)}, headers=auth(key)
    )
    assert created.status_code == 201, created.text

    alembic_command.downgrade(alembic_config, BEFORE_FORM)
    with sync_engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT key, action FROM work_rules")).tuples().all())
    # Down: the form the previous code stored.
    assert rows == {closing: {**action, "fields": {}}, "sample-update": filed}

    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE work_rules SET version = 3 WHERE key = :key"), {"key": closing})
    alembic_command.upgrade(alembic_config, "head")
    with sync_engine.connect() as conn:
        rows = {
            r.key: (r.action, r.version)
            for r in conn.execute(text("SELECT key, action, version FROM work_rules"))
        }
    # Up: the empty member is gone, what the rule does and its version stay.
    assert rows == {closing: (action, 3), "sample-update": (filed, 1)}
    live = await _rules(client, key)
    assert _installer_changes(action, live[closing]) == []
