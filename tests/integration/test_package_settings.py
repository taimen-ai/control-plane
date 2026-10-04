"""Settings of packages: plan and apply, the routes, rights and the event (CP-ADR-0081, G004).

- a package declares settings in ``package.yaml``; the plan shows the
  ``settings`` section, the apply writes revision 1, ``GET /package-settings``
  lists the package with ``version: 0``;
- ``GET /packages/{key}/settings`` puts the strings of the dictionaries in the
  schema and the layout, in the language asked, its base language or the
  default one; ``values`` and ``effective``, ``canManage``, the ETag;
- ``PUT`` saves a new version with history and ``package.settings_changed``
  (no values); the same values or ``{}`` at version 0 change nothing; each
  refusal of §4 by its code and in its order; a secret is stored nowhere;
- two ``PUT`` with one version: one passes, the other is ``409``;
- the history pages by ``limit`` and ``cursor``;
- ``packages.settings.read`` and ``packages.settings.manage`` are separate;
- a new revision of the schema: added, removed and incompatible fields in the
  plan, the apply refused until the value is fixed, the version of the values
  untouched; a value saved between plan and apply is ``plan_stale``; a package
  that stops declaring settings answers ``404 settings_not_declared``.
"""

import asyncio
import copy
import logging
import uuid
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_role,
    create_workspace,
    do_bootstrap,
)
from tests.integration.test_package_plan import _apply, _errors, _plan
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import _events

PACKAGE = "sample-settings"
SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["owner"],
    "properties": {
        "limit": {"type": "number", "minimum": 0, "default": 1000},
        "days": {"type": "integer", "minimum": 1, "maximum": 20, "default": 2},
        "owner": {"type": "string", "x-ref": "role"},
        "note": {"type": "string", "maxLength": 200, "default": ""},
        "window": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "minimum": 0, "maximum": 23, "default": 9},
                "end": {"type": "integer", "minimum": 0, "maximum": 23, "default": 18},
            },
        },
    },
}
UISCHEMA: dict[str, Any] = {
    "type": "VerticalLayout",
    "elements": [
        {
            "type": "Group",
            "label": f"{PACKAGE}.settings.groups.main",
            "elements": [
                {"type": "Control", "scope": "#/properties/limit"},
                {"type": "Control", "scope": "#/properties/owner"},
            ],
        },
        {"type": "Control", "scope": "#/properties/days"},
        {"type": "Control", "scope": "#/properties/note"},
        {"type": "Control", "scope": "#/properties/window/properties/start"},
        {"type": "Control", "scope": "#/properties/window/properties/end"},
    ],
}


def _messages(lang: str, key: str = PACKAGE) -> dict[str, str]:
    words = {
        "en": ["Sample", "Limit", "Above it a second signer", "Days", "Owner", "Note"],
        "ru": ["Образец", "Предел", "Выше него второй подписант", "Дни", "Владелец", "Заметка"],
    }[lang]
    return {
        f"{key}.title": words[0],
        f"{key}.settings.limit": words[1],
        f"{key}.settings.limit.help": words[2],
        f"{key}.settings.days": words[3],
        f"{key}.settings.owner": words[4],
        f"{key}.settings.note": words[5],
        f"{key}.settings.window": f"{words[3]} *",
        f"{key}.settings.window.start": "start",
        f"{key}.settings.window.end": "end",
        f"{key}.settings.groups.main": f"{words[0]}!",
    }


def package_files(
    *,
    key: str = PACKAGE,
    version: str = "1.0.0",
    settings: dict[str, Any] | None = None,
    messages: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A package of a manifest and dictionaries: what settings need, nothing else."""
    spec: dict[str, Any] = {
        "version": version,
        "displayName": "Sample settings",
        "locales": ["en", "ru"],
        "defaultLocale": "en",
    }
    if settings is not None:
        spec["settings"] = settings
    manifest = {"apiVersion": API_VERSION, "kind": "Package", "key": key, "spec": spec}
    dictionaries = messages or {"en": _messages("en", key), "ru": _messages("ru", key)}
    files = [("package.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))]
    files += [
        (f"i18n/{lang}.yaml", yaml.safe_dump(texts, allow_unicode=True))
        for lang, texts in dictionaries.items()
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


def declared(schema: dict[str, Any] | None = None, *, layout: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": copy.deepcopy(schema or SCHEMA)}
    if layout:
        out["uischema"] = copy.deepcopy(UISCHEMA)
    return out


async def install(client: httpx.AsyncClient, key: str, package: dict[str, Any]) -> dict[str, Any]:
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    return plan


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    role = await create_role(client, key, "approver")
    return {"key": key, "admin": boot["adminPrincipal"]["id"], "role": role["id"]}


def _url(package: str = PACKAGE, tail: str = "") -> str:
    return f"/api/v1/packages/{package}/settings{tail}"


async def _put(
    client: httpx.AsyncClient,
    key: str,
    values: Any,
    version: int | None,
    *,
    package: str = PACKAGE,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = auth(key)
    if version is not None:
        headers["If-Match"] = f'"package-settings-{version}"'
    return await client.put(_url(package), json=body or {"values": values}, headers=headers)


def _error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    return error


# --- install and read -------------------------------------------------------------------------


async def test_an_applied_declaration_is_listed_and_read_in_a_language(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    plan = await install(client, s["key"], package_files(settings=declared()))
    assert plan["settings"] == {
        "schemaRevision": {"before": None, "after": 1},
        "added": [
            {"path": "/days", "default": 2},
            {"path": "/limit", "default": 1000},
            {"path": "/note", "default": ""},
            {"path": "/owner"},
            {"path": "/window"},
        ],
        "removed": [],
        "incompatible": [],
        "uischemaChanged": True,
    }
    listed = await client.get("/api/v1/package-settings", headers=auth(s["key"]))
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "items": [
            {
                "package": PACKAGE,
                "title": "Sample",
                "packageVersion": "1.0.0",
                "version": 0,
                "updatedBy": None,
                "updatedAt": None,
            }
        ]
    }
    read = await client.get(_url(), params={"locale": "ru-RU"}, headers=auth(s["key"]))
    assert read.status_code == 200, read.text
    assert read.headers["ETag"] == '"package-settings-0"'
    body = read.json()
    assert body["title"] == "Образец"
    limit = body["schema"]["properties"]["limit"]
    assert (limit["title"], limit["description"]) == ("Предел", "Выше него второй подписант")
    assert "description" not in body["schema"]["properties"]["days"]
    assert body["schema"]["properties"]["window"]["properties"]["start"]["title"] == "start"
    assert body["uischema"]["elements"][0]["label"] == "Образец!"
    assert body["values"] == {}
    assert body["effective"] == {
        "limit": 1000,
        "days": 2,
        "note": "",
        "window": {"start": 9, "end": 18},
    }
    assert (body["version"], body["updatedBy"], body["canManage"]) == (0, None, True)
    assert body["schemaHash"].startswith("sha256:")
    # A language the package lacks: the default one.
    english = await client.get(_url(), params={"locale": "pt-BR"}, headers=auth(s["key"]))
    assert english.json()["title"] == "Sample"
    assert (await client.get(_url(), headers=auth(s["key"]))).json()["title"] == "Sample"


async def test_a_package_without_a_layout_answers_uischema_null(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared(layout=False)))
    body = (await client.get(_url(), headers=auth(s["key"]))).json()
    assert body["uischema"] is None


async def test_an_unknown_query_parameter_and_package_are_refused(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    unknown = await client.get(_url(), params={"lang": "ru"}, headers=auth(s["key"]))
    assert unknown.status_code == 400, unknown.text
    _error(await client.get(_url("nothing"), headers=auth(s["key"])), 404, "package_not_installed")
    _error(
        await client.get(_url("nothing", "/versions"), headers=auth(s["key"])),
        404,
        "package_not_installed",
    )
    _error(await _put(client, s["key"], {}, 0, package="nothing"), 404, "package_not_installed")


# --- saving -----------------------------------------------------------------------------------


async def test_a_saving_is_a_version_with_history_and_an_event_without_values(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    values = {"limit": 1500, "owner": s["role"], "window": {"end": 20}}
    saved = await _put(client, s["key"], values, 0)
    assert saved.status_code == 200, saved.text
    assert saved.headers["ETag"] == '"package-settings-1"'
    body = saved.json()
    assert body["values"] == values
    assert body["effective"] == {
        "limit": 1500,
        "days": 2,
        "note": "",
        "owner": s["role"],
        "window": {"start": 9, "end": 20},
    }
    assert (body["version"], body["updatedBy"]) == (1, s["admin"])
    # Times end with ``Z``, as in the other answers of the core.
    assert body["updatedAt"].endswith("Z") and "+00:00" not in body["updatedAt"]
    second = await _put(client, s["key"], {"owner": s["role"], "days": 5}, 1)
    assert second.status_code == 200, second.text
    history = await client.get(_url(tail="/versions"), headers=auth(s["key"]))
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert [(i["version"], i["changedPaths"]) for i in items] == [
        (2, ["/days", "/limit", "/window"]),
        (1, ["/limit", "/owner", "/window"]),
    ]
    assert items[1]["values"] == values
    assert all(i["updatedAt"].endswith("Z") for i in items)
    events = await _events(client, s["key"], "package.settings_changed")
    payloads = sorted((e["payload"] for e in events), key=lambda p: p["version"])
    assert payloads == [
        {
            "package": PACKAGE,
            "version": 1,
            "previousVersion": 0,
            "schemaRevision": 1,
            "changedPaths": ["/limit", "/owner", "/window"],
            "actorId": s["admin"],
        },
        {
            "package": PACKAGE,
            "version": 2,
            "previousVersion": 1,
            "schemaRevision": 1,
            "changedPaths": ["/days", "/limit", "/window"],
            "actorId": s["admin"],
        },
    ]
    assert all(e["entityType"] == "package" for e in events)
    listed = (await client.get("/api/v1/package-settings", headers=auth(s["key"]))).json()
    assert (listed["items"][0]["version"], listed["items"][0]["updatedBy"]) == (2, s["admin"])
    assert listed["items"][0]["updatedAt"].endswith("Z")


async def test_the_same_values_and_an_empty_body_at_version_0_change_nothing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _world(client)
    # Nothing is required: an empty body passes the schema (the checks come first, §4).
    optional = copy.deepcopy(SCHEMA)
    del optional["required"], optional["properties"]["owner"]
    await install(client, s["key"], package_files(settings=declared(optional, layout=False)))
    empty = await _put(client, s["key"], {}, 0)
    assert empty.status_code == 200, empty.text
    assert (empty.json()["version"], empty.headers["ETag"]) == (0, '"package-settings-0"')
    values = {"limit": 7}
    assert (await _put(client, s["key"], values, 0)).status_code == 200
    again = await _put(client, s["key"], values, 1)
    assert again.status_code == 200, again.text
    assert again.json()["version"] == 1
    # Repeated with the same If-Match: the state as it is, not a conflict of its own.
    assert (await _put(client, s["key"], values, 1)).json()["version"] == 1
    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM package_settings_versions")).scalar()
    assert rows == 1
    assert len(await _events(client, s["key"], "package.settings_changed")) == 1
    # An empty body over saved values is a new version: every field back to its default.
    cleared = await _put(client, s["key"], {}, 1)
    assert cleared.status_code == 200
    assert (cleared.json()["version"], cleared.json()["values"]) == (2, {})


async def test_each_refusal_answers_by_its_code_in_its_order(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    _error(await _put(client, s["key"], {}, None), 428, "if_match_required")
    bad = await client.put(
        _url(), json={"values": {}}, headers={**auth(s["key"]), "If-Match": '"task-1"'}
    )
    _error(bad, 400, "invalid_if_match")
    # A secret before the schema: an unknown field carrying one is still a secret.
    secret = _error(
        await _put(client, s["key"], {"limit": "x", "other": SECRET}, 0),
        422,
        "secret_material_rejected",
    )
    assert secret["details"]["errors"] == [{"path": "/other", "match": "provider_token"}]
    invalid = _error(
        await _put(client, s["key"], {"limit": -1, "days": 2.5, "extra": 1, "window": []}, 0),
        422,
        "settings_invalid",
    )
    assert sorted((e["path"], e["code"]) for e in invalid["details"]["errors"]) == [
        ("/days", "type"),
        ("/extra", "additionalProperties"),
        ("/limit", "minimum"),
        ("/owner", "required"),
        ("/window", "type"),
    ]
    assert all("-1" not in (e.get("message") or "") for e in invalid["details"]["errors"])
    ref = _error(await _put(client, s["key"], {"owner": str(uuid.uuid4())}, 0), 422, "unknown_ref")
    assert ref["details"]["errors"] == [{"path": "/owner", "ref": "role"}]
    not_uuid = _error(await _put(client, s["key"], {"owner": "approver"}, 0), 422, "unknown_ref")
    assert not_uuid["details"]["errors"] == [{"path": "/owner", "ref": "role"}]
    # A valid body with a stale version: the version is the last check.
    stale = _error(await _put(client, s["key"], {"owner": s["role"]}, 3), 409, "version_conflict")
    assert stale["details"]["currentVersion"] == 0
    # A body of other members than values: 400, a credential-shaped name not quoted.
    extra = await _put(client, s["key"], None, 0, body={"values": {}, SECRET: 1})
    assert extra.status_code == 400, extra.text
    assert SECRET not in extra.text
    for body in ({"values": [1]}, {"values": None}, {"values": "x"}, {}):
        not_object = await _put(client, s["key"], None, 0, body=body)
        assert not_object.status_code == 400, (body, not_object.text)


async def test_a_secret_is_stored_nowhere_and_answered_without_its_value(
    client: httpx.AsyncClient, sync_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    cases: list[tuple[dict[str, Any], list[str]]] = [
        ({"owner": s["role"], "note": SECRET}, ["/note"]),
        ({"owner": s["role"], "note": f"Bearer {SECRET}"}, ["/note"]),
        ({"owner": s["role"], SECRET: "x"}, ["/"]),
        ({"owner": s["role"], "window": {SECRET: 1}}, ["/window"]),
        ({"owner": s["role"], "apiToken": "plain"}, ["/"]),
    ]
    for values, paths in cases:
        response = await _put(client, s["key"], values, 0)
        error = _error(response, 422, "secret_material_rejected")
        assert [e["path"] for e in error["details"]["errors"]] == paths
        assert SECRET not in response.text
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM package_settings)"
                " + (SELECT count(*) FROM package_settings_versions)"
            )
        ).scalar()
        journal = conn.execute(text("SELECT payload::text FROM events")).scalars().all()
        outbox = conn.execute(text("SELECT payload::text FROM outbox")).scalars().all()
    assert stored == 0
    assert not any(SECRET in row for row in [*journal, *outbox])
    assert SECRET not in caplog.text
    assert not any(SECRET in repr(vars(record)) for record in caplog.records)


async def test_each_kind_of_reference_is_looked_up_in_the_organization(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    workspace = await create_workspace(client, s["key"], "finance")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            name: {"type": "string", "x-ref": kind, "default": ""}
            for name, kind in (
                ("role", "role"),
                ("person", "principal"),
                ("place", "workspace"),
                ("work", "taskType"),
                ("days", "calendar"),
            )
        }
        | {"roles": {"type": "array", "items": {"type": "string", "x-ref": "role"}, "default": []}},
    }
    messages = {
        lang: {f"{PACKAGE}.title": "T"}
        | {
            f"{PACKAGE}.settings.{n}": n
            for n in ("role", "person", "place", "work", "days", "roles")
        }
        for lang in ("en", "ru")
    }
    await install(client, s["key"], package_files(settings={"schema": schema}, messages=messages))
    missing = {
        "role": str(uuid.uuid4()),
        "person": str(uuid.uuid4()),
        "place": str(uuid.uuid4()),
        "work": "no-such-type",
        "days": "no-such-calendar",
        "roles": [s["role"], str(uuid.uuid4())],
    }
    error = _error(await _put(client, s["key"], missing, 0), 422, "unknown_ref")
    assert error["details"]["errors"] == [
        {"path": "/role", "ref": "role"},
        {"path": "/person", "ref": "principal"},
        {"path": "/place", "ref": "workspace"},
        {"path": "/work", "ref": "taskType"},
        {"path": "/days", "ref": "calendar"},
        {"path": "/roles/1", "ref": "role"},
    ]
    known = {
        "role": s["role"],
        "person": s["admin"],
        "place": workspace["id"],
        "roles": [s["role"]],
    }
    saved = await _put(client, s["key"], known, 0)
    assert saved.status_code == 200, saved.text


async def test_two_puts_with_one_version_one_passes_and_the_other_is_409(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    for version, limits in ((0, (1, 2)), (1, (3, 4))):
        results = await asyncio.gather(
            *(_put(client, s["key"], {"owner": s["role"], "limit": n}, version) for n in limits)
        )
        assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
        refused = next(r for r in results if r.status_code == 409)
        assert refused.json()["error"]["details"]["currentVersion"] == version + 1
    with sync_engine.connect() as conn:
        versions = (
            conn.execute(text("SELECT version FROM package_settings_versions ORDER BY version"))
            .scalars()
            .all()
        )
    assert versions == [1, 2]


async def test_the_history_pages_by_limit_and_cursor(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    for version in range(5):
        response = await _put(client, s["key"], {"owner": s["role"], "days": version + 1}, version)
        assert response.status_code == 200, response.text
    seen: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await client.get(_url(tail="/versions"), params=params, headers=auth(s["key"]))
        assert page.status_code == 200, page.text
        seen += [item["version"] for item in page.json()["items"]]
        cursor = page.json()["nextCursor"]
        if cursor is None:
            break
    assert seen == [5, 4, 3, 2, 1]
    for limit in (0, 101):
        wrong = await client.get(
            _url(tail="/versions"), params={"limit": limit}, headers=auth(s["key"])
        )
        _error(wrong, 422, "invalid_limit")
    garbled = await client.get(
        _url(tail="/versions"), params={"cursor": "not-a-cursor"}, headers=auth(s["key"])
    )
    _error(garbled, 422, "invalid_cursor")


async def test_reading_and_managing_are_separate_rights(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    _, reader = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["packages.settings.read"]
    )
    _, manager = await create_agent_with_key(
        client, s["key"], name="manager", permissions=["packages.settings.manage"]
    )
    _, planner = await create_agent_with_key(
        client, s["key"], name="planner", permissions=["packages.plan"]
    )
    read = await client.get(_url(), headers=auth(reader))
    assert read.status_code == 200, read.text
    assert read.json()["canManage"] is False
    assert (await client.get(_url(tail="/versions"), headers=auth(reader))).status_code == 200
    _error(await _put(client, reader, {"owner": s["role"]}, 0), 403, "permission_denied")
    assert (await _put(client, manager, {"owner": s["role"]}, 0)).status_code == 200
    _error(await client.get(_url(), headers=auth(manager)), 403, "permission_denied")
    for route in ("/api/v1/package-settings", _url(), _url(tail="/versions")):
        _error(await client.get(route, headers=auth(planner)), 403, "permission_denied")
    _error(await _put(client, planner, {"owner": s["role"]}, 1), 403, "permission_denied")


# --- a new revision of the schema -------------------------------------------------------------


async def test_a_new_schema_shows_added_removed_and_incompatible_fields(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    assert (
        await _put(client, s["key"], {"owner": s["role"], "limit": 5000, "note": "x"}, 0)
    ).status_code == 200
    schema = copy.deepcopy(SCHEMA)
    del schema["properties"]["note"]  # removed, it had a saved value
    del schema["properties"]["days"]  # removed, never saved
    schema["properties"]["limit"]["maximum"] = 2000  # the saved 5000 no longer passes
    schema["properties"]["count"] = {"type": "integer", "default": 3}  # added
    messages = {lang: _messages(lang) | {f"{PACKAGE}.settings.count": "N"} for lang in ("en", "ru")}
    layout = copy.deepcopy(UISCHEMA)
    layout["elements"] = [
        e
        for e in layout["elements"]
        if e.get("scope") not in ("#/properties/days", "#/properties/note")
    ] + [{"type": "Control", "scope": "#/properties/count"}]
    second = package_files(
        version="1.1.0", settings={"schema": schema, "uischema": layout}, messages=messages
    )
    plan = await _plan(client, s["key"], second)
    assert plan["settings"] == {
        "schemaRevision": {"before": 1, "after": 2},
        "added": [{"path": "/count", "default": 3}],
        "removed": [{"path": "/days", "saved": False}, {"path": "/note", "saved": True}],
        "incompatible": [{"path": "/limit", "code": "maximum"}],
        "uischemaChanged": True,
    }
    found = [p for p in plan["problems"] if p["code"] == "settings_incompatible"]
    assert [(p["severity"], p["path"], p["file"]) for p in found] == [
        ("error", "/spec/settings/schema/properties/limit", "package.yaml")
    ]
    assert "5000" not in found[0]["message"]
    refused = await _apply(client, s["key"], second, plan["planHash"])
    _error(refused, 422, "invalid_package")
    # The administrator fixes the value; the plan built again applies.
    fixed = await _put(client, s["key"], {"owner": s["role"], "limit": 1500, "note": "x"}, 1)
    assert fixed.status_code == 200, fixed.text
    plan = await _plan(client, s["key"], second)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, s["key"], second, plan["planHash"])
    assert applied.status_code == 200, applied.text
    body = (await client.get(_url(), headers=auth(s["key"]))).json()
    # The apply moves neither the version nor the history; the removed field is not shown.
    assert (body["version"], body["packageVersion"]) == (2, "1.1.0")
    assert body["values"] == {"owner": s["role"], "limit": 1500}
    assert body["effective"]["count"] == 3
    assert "note" not in body["effective"]
    assert len(await _events(client, s["key"], "package.settings_changed")) == 2
    # The same files again: the revision stays, the section says so.
    again = await _plan(client, s["key"], second)
    assert again["settings"]["schemaRevision"] == {"before": 2, "after": None}
    assert again["settings"]["uischemaChanged"] is False


async def test_a_value_saved_between_plan_and_apply_is_plan_stale(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    second = package_files(version="1.0.1", settings=declared(layout=False))
    plan = await _plan(client, s["key"], second)
    assert (await _put(client, s["key"], {"owner": s["role"]}, 0)).status_code == 200
    _error(await _apply(client, s["key"], second, plan["planHash"]), 409, "plan_stale")


async def test_a_package_that_stops_declaring_settings_keeps_its_history(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    assert (await _put(client, s["key"], {"owner": s["role"]}, 0)).status_code == 200
    plain = package_files(
        version="2.0.0", messages={"en": {f"{PACKAGE}.title": "S"}, "ru": {f"{PACKAGE}.title": "S"}}
    )
    plan = await install(client, s["key"], plain)
    assert plan["settings"]["schemaRevision"] == {"before": 1, "after": None}
    assert [r["path"] for r in plan["settings"]["removed"]] == [
        "/days",
        "/limit",
        "/note",
        "/owner",
        "/window",
    ]
    for response in (
        await client.get(_url(), headers=auth(s["key"])),
        await client.get(_url(tail="/versions"), headers=auth(s["key"])),
        await _put(client, s["key"], {"owner": s["role"]}, 1),
    ):
        _error(response, 404, "settings_not_declared")
    listed = (await client.get("/api/v1/package-settings", headers=auth(s["key"]))).json()
    assert listed == {"items": []}
    with sync_engine.connect() as conn:
        kept = conn.execute(text("SELECT count(*) FROM package_settings_versions")).scalar()
    assert kept == 1
    # Declared again: a new revision, the values saved before still apply.
    plan = await install(client, s["key"], package_files(version="3.0.0", settings=declared()))
    assert plan["settings"]["schemaRevision"] == {"before": None, "after": 2}
    body = (await client.get(_url(), headers=auth(s["key"]))).json()
    assert (body["version"], body["values"]) == (1, {"owner": s["role"]})


async def test_an_invalid_declaration_is_a_finding_and_writes_no_revision(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["limit"]["title"] = "Limit"
    del schema["properties"]["days"]["default"]
    package = package_files(settings={"schema": schema})
    plan = await _plan(client, s["key"], package)
    codes = sorted({(p["code"], p["path"]) for p in plan["problems"] if p["severity"] == "error"})
    assert codes == [
        ("settings_default_missing", "/spec/settings/schema/properties/days"),
        ("settings_schema_unsupported", "/spec/settings/schema/properties/limit/title"),
    ]
    assert plan["settings"] is None
    _error(await _apply(client, s["key"], package, plan["planHash"]), 422, "invalid_package")
    _error(await client.get(_url(), headers=auth(s["key"])), 404, "package_not_installed")


async def test_a_plan_without_settings_has_no_section(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    plan = await _plan(
        client,
        s["key"],
        package_files(messages={"en": {f"{PACKAGE}.title": "S"}, "ru": {f"{PACKAGE}.title": "S"}}),
    )
    assert plan["settings"] is None


async def test_a_put_repeated_with_its_idempotency_key_is_replayed(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install(client, s["key"], package_files(settings=declared()))
    headers = {**auth(s["key"]), "If-Match": '"package-settings-0"', "Idempotency-Key": "k-1"}
    body = {"values": {"owner": s["role"]}}
    first = await client.put(_url(), json=body, headers=headers)
    again = await client.put(_url(), json=body, headers=headers)
    assert (first.status_code, again.status_code) == (200, 200), again.text
    assert again.headers.get("Idempotency-Replayed") == "true"
    assert again.headers["ETag"] == first.headers["ETag"] == '"package-settings-1"'
    assert again.json() == first.json()
    assert len(await _events(client, s["key"], "package.settings_changed")) == 1
    other = await client.put(
        _url(), json={"values": {"owner": s["role"], "days": 3}}, headers=headers
    )
    assert other.status_code == 409, other.text
