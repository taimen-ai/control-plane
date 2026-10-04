"""Views of packages: plan, apply, ``GET /views`` (CP-ADR-0080, TAI-ADR-0066 stage 1).

The acceptance of TASK-001298 on two packages that are not the platform's own
domain — a package with a process (``sample-plan``) and ``invoice-payment``
(the fixture of tests/fixtures/packages): the same code installs and serves
the screens of both, the core knows no name of their views, blocks or fields.

- the plan shows ``View`` creates, the apply publishes them (``view.published``);
- the check refuses with a path: a source that is not there, a path the data
  schema does not declare, an expression of the wrong type, a key a dictionary
  lacks, a role nobody has, ``open.view`` outside the package and what it
  requires; an apply of such a package is ``422 invalid_package``;
- a view is seen by a holder of a role of its audience who may read its
  source; anyone else gets the 404 of a missing one;
- the strings come in the language asked, else the package's ``defaultLocale``;
- a changed text is a new revision; a view the package drops is retired
  (``view.retired``) and comes back as it was (``view.published``);
- the blocks are written as TAI-ADR-0066 §2 writes them (``block:``, a skill
  for ``invoke``, a board by ``stages``, a component given its params ``with``);
- the views and the component of TAI-ADR-0066 §1 and §7.1 install as they are;
- ``GET /views`` answers in the form agreed with the console (CP-ADR-0080 §9):
  a summary without ``layout`` in the list, blocks of keys and titles without
  paths and expressions in one view;
- a view of one tenant is not there for another.
"""

import copy
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.catalog_packages import PACKAGES, install_package
from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    make_tenant_directly,
)
from tests.integration.test_package_plan import PROCESS, _apply, _errors, _plan, _spec
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import _events, _setup
from tests.unit.test_views_console_contract import BOARD_RU as TENDERS_BOARD_RU
from tests.unit.test_views_console_contract import assert_console_form

PACKAGE = "sample-plan"
DATA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string"},
        "amount": {"type": "number"},
        "case": {
            "type": "object",
            "properties": {"openedAt": {"type": "string", "format": "date-time"}},
        },
    },
}
EN = {
    "sample.list.title": "Cases",
    "sample.col.decision": "Decision",
    "sample.col.amount": "Amount",
    "sample.col.opened": "Opened {when}",
    "sample.metric.open": "{count, plural, one {# open} other {# open}}",
    "sample.card.title": "Case",
    "sample.card.section": "Details",
    "sample-plan.fields.decision": "Decision",
    "sample-plan.fields.case.openedAt": "Opened",
}
RU = {
    "sample.list.title": "Дела",
    "sample.col.decision": "Решение",
    "sample.col.amount": "Сумма",
    "sample.col.opened": "Открыто {when}",
    "sample.metric.open": "{count, plural, one {# открыто} other {# открыто}}",
    "sample.card.title": "Дело",
    "sample.card.section": "Подробности",
    "sample-plan.fields.decision": "Решение",
    "sample-plan.fields.case.openedAt": "Открыто",
}


def _list_view(**change: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "title": "sample.list.title",
        "nav": {"group": "work", "icon": "inbox", "order": 10},
        "audience": {"roles": ["clerk"]},
        "source": {"process": PROCESS, "filter": 'data.decision != "rejected"'},
        "layout": [
            {
                "block": "table",
                "columns": [
                    {"label": "sample.col.decision", "field": "data.decision", "format": "status"},
                    {"label": "sample.col.amount", "field": "data.amount", "format": "money"},
                ],
                "open": {"view": "sample-card", "id": "id"},
                "filters": ["data.decision"],
                "sort": [{"field": "data.case.openedAt", "dir": "desc"}],
            },
            {
                "block": "metrics",
                "items": [{"title": "sample.metric.open", "value": "count()", "format": "number"}],
            },
            {"block": "component", "component": "opened"},
        ],
    }
    spec.update(change)
    return spec


def _card_view() -> dict[str, Any]:
    return {
        "title": "sample.card.title",
        "audience": {"roles": ["clerk"]},
        "params": {"id": {"type": "uuid", "required": True}},
        "source": {"process": PROCESS, "instance": "param.id"},
        "layout": [
            {"block": "header", "title": "data.decision", "status": "stage", "actions": "steps"},
            {
                "block": "fields",
                "section": "sample.card.section",
                "items": [{"label": "sample.col.amount", "value": "data.amount * 1.0"}],
            },
            {"block": "steps"},
            {"block": "timeline"},
        ],
    }


COMPONENT = {
    "layout": [
        {
            "block": "list",
            "columns": [
                {"label": "sample.col.opened", "field": "data.case.openedAt", "format": "datetime"}
            ],
        }
    ]
}


def _doc(kind: str, key: str, spec: dict[str, Any]) -> str:
    return yaml.safe_dump(
        {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec},
        sort_keys=False,
        allow_unicode=True,
    )


def _package(
    admin: str,
    *,
    views: dict[str, dict[str, Any]] | None = None,
    en: dict[str, str] | None = None,
    ru: dict[str, str] | None = None,
    manifest: dict[str, Any] | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    process = _spec(admin)
    process["data"] = DATA
    head = {
        "version": "1.0.0",
        "displayName": "Sample plan",
        "locales": ["en", "ru"],
        "defaultLocale": "en",
        **(manifest or {}),
    }
    files = [
        ("package.yaml", _doc("Package", PACKAGE, head)),
        ("processes/sample.yaml", _doc("Process", PROCESS, process)),
        ("components/opened.yaml", _doc("Component", "opened", COMPONENT)),
        ("i18n/en.yaml", yaml.safe_dump(EN if en is None else en, allow_unicode=True)),
        ("i18n/ru.yaml", yaml.safe_dump(RU if ru is None else ru, allow_unicode=True)),
    ]
    chosen = {"sample-list": _list_view(), "sample-card": _card_view()} if views is None else views
    for key, spec in chosen.items():
        files.append((f"views/{key}.yaml", _doc("View", key, spec)))
    files += extra or []
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _install(client: httpx.AsyncClient, key: str, package: dict[str, Any]) -> dict[str, Any]:
    plan = await _plan(client, key, package)
    assert _errors(plan) == [], plan["problems"]
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    out: dict[str, Any] = applied.json()
    return out


async def _person(
    client: httpx.AsyncClient, admin_key: str, name: str, permissions: list[str], roles: list[str]
) -> str:
    principal, key = await create_agent_with_key(
        client, admin_key, name=name, permissions=permissions, kind="human"
    )
    for role_id in roles:
        await assign_role(client, admin_key, principal["id"], role_id)
    return key


async def _views(client: httpx.AsyncClient, key: str, **params: Any) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/views", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _view(client: httpx.AsyncClient, key: str, view: str, **params: Any) -> httpx.Response:
    return await client.get(f"/api/v1/views/{view}", params=params, headers=auth(key))


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    s = await _setup(client)
    clerk = await create_role(client, s["key"], "clerk")
    s["clerk"] = await _person(client, s["key"], "clerk", ["processes.read"], [clerk["id"]])
    s["outsider"] = await _person(client, s["key"], "outsider", ["processes.read"], [])
    s["no-right"] = await _person(client, s["key"], "no-right", ["tasks.read"], [clerk["id"]])
    return s


# --- publication and reading ------------------------------------------------------------------


async def test_a_package_publishes_its_views_and_a_holder_of_the_role_reads_them(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    package = _package(s["admin"])
    plan = await _plan(client, s["key"], package)
    assert _errors(plan) == [], plan["problems"]
    views = [(c["key"], c["action"]) for c in plan["changes"] if c["kind"] == "View"]
    assert views == [("sample-card", "create"), ("sample-list", "create")]
    assert all(o["kind"] != "Component" for o in plan["outside"])
    applied = await _apply(client, s["key"], package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert {"kind": "View", "key": "sample-list", "action": "create", "version": 1} in (
        applied.json()["applied"]
    )

    page = await client.get("/api/v1/views", headers=auth(s["clerk"]))
    assert page.status_code == 200, page.text
    assert_console_form(page.json(), "page")
    listed = page.json()["items"]
    assert [v["key"] for v in listed] == ["sample-card", "sample-list"]
    assert all("layout" not in v for v in listed)
    response = await _view(client, s["clerk"], "sample-list", locale="ru")
    assert response.status_code == 200, response.text
    view = response.json()
    assert_console_form(view)
    assert view["revision"] == 1 and view["hash"].startswith("sha256:")
    assert view["blocks"] == 1
    assert view["package"]["key"] == PACKAGE and view["package"]["version"] == "1.0.0"
    assert view["locale"] == "ru"
    assert view["title"] == "Дела"
    assert view["nav"] == {"group": "work", "icon": "inbox", "order": 10}
    # The raw source of the package (its CEL filter) does not reach the console.
    assert view["source"] == {"kind": "process", "process": PROCESS, "instance": False}
    assert {v["key"]: v["source"]["instance"] for v in listed} == {
        "sample-card": True,
        "sample-list": False,
    }
    table, metrics, component = view["layout"]
    assert table == {
        "block": "table",
        "columns": [
            {"key": "decision", "title": "Решение", "format": "status"},
            {"key": "amount", "title": "Сумма", "format": "money"},
        ],
        "filters": [{"field": "decision", "title": "Решение", "type": "text"}],
        "sort": [{"field": "case.openedAt", "title": "Открыто"}],
        "open": {"view": "sample-card"},
    }
    assert metrics["items"][0]["key"] == "open"
    assert metrics["items"][0]["title"].startswith("{count, plural")
    # The component is inlined, its strings substituted too.
    assert component["component"] == "opened"
    assert component["layout"][0]["columns"] == [
        {"key": "case.openedAt", "title": "Открыто {when}", "format": "datetime"}
    ]
    card = (await _view(client, s["clerk"], "sample-card", locale="en")).json()
    assert_console_form(card)
    assert card["layout"][:2] == [
        {"block": "header", "actions": "steps"},
        {
            "block": "fields",
            "section": "Details",
            "items": [{"key": "amount", "label": "Amount"}],
        },
    ]

    events = await _events(client, s["key"], "view.published")
    published = {e["payload"]["key"]: e["payload"] for e in events}
    assert published["sample-list"]["revision"] == 1
    assert published["sample-list"]["hash"] == view["hash"]
    assert published["sample-list"]["previousRevision"] is None
    assert published["sample-list"]["packageKey"] == PACKAGE


async def test_the_strings_fall_back_to_the_default_locale(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    titles = {}
    for wanted in ("ru", "ru-RU", "RU", "en", "de", None):
        params = {"locale": wanted} if wanted else {}
        response = await _view(client, s["clerk"], "sample-list", **params)
        assert response.status_code == 200, response.text
        titles[wanted] = (response.json()["locale"], response.json()["title"])
    assert titles == {
        "ru": ("ru", "Дела"),
        "ru-RU": ("ru", "Дела"),
        "RU": ("ru", "Дела"),
        "en": ("en", "Cases"),
        "de": ("en", "Cases"),
        None: ("en", "Cases"),
    }
    listed = await _views(client, s["clerk"], locale="de")
    assert {v["title"] for v in listed} == {"Cases", "Case"}


async def test_a_view_is_seen_with_a_role_of_its_audience_and_the_right_to_its_source(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    # The role, not the permission, opens the screen; without the role it is not there.
    assert await _views(client, s["outsider"]) == []
    missing = await _view(client, s["outsider"], "sample-list")
    absent = await _view(client, s["outsider"], "no-such-view")
    assert missing.status_code == absent.status_code == 404
    assert missing.json()["error"]["code"] == absent.json()["error"]["code"]
    # The role without the right to read the source: the view narrows, never widens.
    assert await _views(client, s["no-right"]) == []
    assert (await _view(client, s["no-right"], "sample-list")).status_code == 404
    # The administrator holds every right but not the role.
    assert await _views(client, s["key"]) == []


async def test_a_view_without_an_audience_is_seen_by_whoever_reads_its_source(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    spec = _list_view()
    del spec["audience"]
    await _install(
        client,
        s["key"],
        _package(s["admin"], views={"sample-list": spec, "sample-card": _card_view()}),
    )
    assert [v["key"] for v in await _views(client, s["outsider"])] == ["sample-list"]
    assert await _views(client, s["no-right"]) == []


async def test_the_list_pages_by_key_and_filters_by_package(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    first = await client.get("/api/v1/views", params={"limit": 1}, headers=auth(s["clerk"]))
    assert first.status_code == 200, first.text
    assert [v["key"] for v in first.json()["items"]] == ["sample-card"]
    cursor = first.json()["nextCursor"]
    second = await client.get(
        "/api/v1/views", params={"limit": 1, "cursor": cursor}, headers=auth(s["clerk"])
    )
    assert [v["key"] for v in second.json()["items"]] == ["sample-list"]
    assert second.json()["nextCursor"] is None
    assert len(await _views(client, s["clerk"], package=PACKAGE)) == 2
    assert await _views(client, s["clerk"], package="other") == []
    unknown = await client.get("/api/v1/views", params={"lang": "ru"}, headers=auth(s["clerk"]))
    assert unknown.status_code == 400


# --- revisions, retirement ----------------------------------------------------------------------


async def test_a_changed_text_is_a_new_revision_and_the_same_package_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    again = await _plan(client, s["key"], _package(s["admin"]))
    assert {c["action"] for c in again["changes"] if c["kind"] == "View"} == {"unchanged"}

    changed = _package(s["admin"], ru={**RU, "sample.list.title": "Все дела"})
    plan = await _plan(client, s["key"], changed)
    actions = {c["key"]: c for c in plan["changes"] if c["kind"] == "View"}
    assert actions["sample-list"]["action"] == "update"
    assert [f["path"] for f in actions["sample-list"]["fields"]] == ["/spec/messages"]
    assert actions["sample-card"]["action"] == "unchanged"
    applied = await _apply(client, s["key"], changed, plan["planHash"])
    assert applied.status_code == 200, applied.text

    view = (await _view(client, s["clerk"], "sample-list", locale="ru")).json()
    assert (view["revision"], view["title"]) == (2, "Все дела")
    events = [
        e["payload"]
        for e in await _events(client, s["key"], "view.published")
        if e["payload"]["key"] == "sample-list"
    ]
    assert [(e["revision"], e["previousRevision"]) for e in events] == [(1, None), (2, 1)]


async def test_a_view_the_package_drops_is_retired_and_comes_back_as_it_was(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    before = (await _view(client, s["clerk"], "sample-card")).json()

    dropped = _package(
        s["admin"], views={"sample-list": _list_view(layout=_list_view()["layout"][1:])}
    )
    plan = await _plan(client, s["key"], dropped)
    retire = [c for c in plan["changes"] if c["action"] == "retire"]
    assert [(c["kind"], c["key"]) for c in retire] == [("View", "sample-card")]
    await _install(client, s["key"], dropped)
    assert (await _view(client, s["clerk"], "sample-card")).status_code == 404
    assert [v["key"] for v in await _views(client, s["clerk"])] == ["sample-list"]
    retired = await _events(client, s["key"], "view.retired")
    assert [(e["payload"]["key"], e["payload"]["revision"]) for e in retired] == [
        ("sample-card", 1)
    ]
    assert retired[0]["payload"]["reason"] == f"no longer in package {PACKAGE}"

    plan = await _plan(client, s["key"], _package(s["admin"]))
    actions = {c["key"]: c["action"] for c in plan["changes"] if c["kind"] == "View"}
    assert actions == {"sample-card": "restore", "sample-list": "update"}
    await _install(client, s["key"], _package(s["admin"]))
    after = (await _view(client, s["clerk"], "sample-card")).json()
    assert (after["revision"], after["hash"]) == (before["revision"], before["hash"])
    published = [
        e["payload"]["revision"]
        for e in await _events(client, s["key"], "view.published")
        if e["payload"]["key"] == "sample-card"
    ]
    assert published == [1, 1]


async def test_the_dictionaries_are_kept_with_a_revision_per_change(
    client: httpx.AsyncClient, sync_engine: Any
) -> None:
    from sqlalchemy import text

    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    await _install(client, s["key"], _package(s["admin"]))
    await _install(client, s["key"], _package(s["admin"], en={**EN, "sample.list.title": "All"}))
    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT revision, package_version, locales, default_locale, messages"
                " FROM package_dictionaries WHERE package_key = :key ORDER BY revision"
            ),
            {"key": PACKAGE},
        ).all()
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][2] == ["en", "ru"] and rows[0][3] == "en"
    assert rows[0][4]["ru"] == RU
    assert rows[1][4]["en"]["sample.list.title"] == "All"


# --- the check refuses with a path ----------------------------------------------------------------


def _problem(plan: dict[str, Any], code: str) -> dict[str, Any]:
    found = [p for p in plan["problems"] if p["code"] == code]
    assert found, (code, plan["problems"])
    return found[0]


async def test_the_check_refuses_what_is_not_there_with_a_path(client: httpx.AsyncClient) -> None:
    s = await _world(client)
    cases: list[tuple[dict[str, Any], str, str]] = [
        (_list_view(source={"process": "nowhere"}), "unknown_source", "/spec/source/process"),
        (
            _list_view(source={"tasks": {"type": "nowhere"}}, layout=[_list_view()["layout"][1]]),
            "unknown_source",
            "/spec/source/tasks/type",
        ),
        (
            _list_view(audience={"roles": ["clerk", "nobody"]}),
            "unknown_role",
            "/spec/audience/roles/1",
        ),
        (
            _list_view(source={"process": PROCESS, "filter": "data.amount"}),
            "expression_type_error",
            "/spec/source/filter",
        ),
    ]
    undeclared = _list_view()
    undeclared["layout"][0]["columns"][0]["field"] = "data.nothing"
    cases.append((undeclared, "undeclared_path", "/spec/layout/0/columns/0/field"))
    mismatch = _list_view()
    mismatch["layout"][0]["columns"][1]["format"] = "date"
    cases.append((mismatch, "format_type_mismatch", "/spec/layout/0/columns/1/field"))
    outside = _list_view()
    outside["layout"][0]["columns"][1] = {"label": "sample.col.amount", "value": "sum(data.amount)"}
    cases.append((outside, "aggregate_outside_metrics", "/spec/layout/0/columns/1/value"))
    foreign = _list_view()
    foreign["layout"][0]["open"]["view"] = "somewhere-else"
    cases.append((foreign, "unknown_view", "/spec/layout/0/open/view"))
    block = _list_view()
    block["layout"].append({"block": "widget"})
    cases.append((block, "unknown_block", "/spec/layout/3/block"))
    shown = _list_view()
    shown["layout"][0]["columns"][0]["format"] = "badge"
    cases.append((shown, "unknown_format", "/spec/layout/0/columns/0/format"))
    coded = _list_view()
    coded["code"] = "export default () => null"
    cases.append((coded, "component_code_not_supported", "/spec/code"))
    typed = _list_view()
    typed["layout"][0] = {"type": "table", "columns": typed["layout"][0]["columns"]}
    cases.append((typed, "invalid_view", "/spec/layout/0"))
    skill = _list_view()
    skill["layout"].append(
        {"block": "invoke", "label": "sample.col.decision", "skill": "no.such@1"}
    )
    cases.append((skill, "unknown_skill", "/spec/layout/3/skill"))
    for spec, code, path in cases:
        package = _package(s["admin"], views={"sample-list": spec, "sample-card": _card_view()})
        plan = await _plan(client, s["key"], package)
        problem = _problem(plan, code)
        assert (problem["path"], problem["file"]) == (path, "views/sample-list.yaml"), code
        assert problem["line"] is not None
        assert all(c["key"] != "sample-list" for c in plan["changes"]), code
        refused = await _apply(client, s["key"], package, plan["planHash"])
        assert refused.status_code == 422, code
        assert refused.json()["error"]["code"] == "invalid_package"
    # Nothing was installed by the refusals.
    assert await _views(client, s["clerk"]) == []


async def test_every_string_is_a_key_of_every_declared_dictionary(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    ru = {k: v for k, v in RU.items() if k != "sample.col.amount"}
    plan = await _plan(client, s["key"], _package(s["admin"], ru=ru))
    missing = [p for p in plan["problems"] if p["code"] == "missing_message"]
    assert {(p["file"], p["path"]) for p in missing} == {
        ("views/sample-list.yaml", "/spec/layout/0/columns/1/label"),
        ("views/sample-card.yaml", "/spec/layout/1/items/0/label"),
    }
    assert all("ru" in p["message"] and p["severity"] == "error" for p in missing)

    extra = _package(s["admin"], en={**EN, "sample.unused": "Unused"})
    unused = _problem(await _plan(client, s["key"], extra), "unused_message")
    assert (unused["severity"], unused["file"], unused["path"]) == (
        "warning",
        "i18n/en.yaml",
        "/sample.unused",
    )

    undeclared = _package(s["admin"], extra=[("i18n/de.yaml", yaml.safe_dump(EN))])
    problem = _problem(await _plan(client, s["key"], undeclared), "undeclared_locale")
    assert problem["file"] == "i18n/de.yaml"

    no_dictionary = _package(s["admin"], manifest={"locales": ["en", "ru", "kk"]})
    problem = _problem(await _plan(client, s["key"], no_dictionary), "missing_dictionary")
    assert (problem["file"], problem["path"]) == ("package.yaml", "/spec/locales/2")

    bad_default = _package(s["admin"], manifest={"defaultLocale": "de"})
    problem = _problem(await _plan(client, s["key"], bad_default), "invalid_default_locale")
    assert problem["path"] == "/spec/defaultLocale"

    package = _package(s["admin"])
    files = package["files"]
    files[0]["content"] = _doc(
        "Package", PACKAGE, {"version": "1.0.0", "displayName": "Sample plan"}
    )
    problem = _problem(await _plan(client, s["key"], package), "locales_required")
    assert problem["file"] == "package.yaml"


async def test_open_view_reaches_the_views_of_a_required_package(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    texts = {"other.title": "Other", "sample.col.decision": "Decision"}
    view = {
        "title": "other.title",
        "source": {"process": PROCESS},
        "layout": [
            {
                "block": "table",
                "columns": [{"label": "sample.col.decision", "field": "data.decision"}],
                "open": {"view": "sample-card", "params": {"id": "instance.id"}},
            }
        ],
    }

    def other(requires: list[Any]) -> dict[str, Any]:
        head = {
            "version": "0.1.0",
            "displayName": "Other",
            "locales": ["en"],
            "defaultLocale": "en",
            "requires": requires,
        }
        files = [
            ("package.yaml", _doc("Package", "other", head)),
            ("views/other.yaml", _doc("View", "other-list", view)),
            ("i18n/en.yaml", yaml.safe_dump(texts)),
        ]
        return {"files": [{"path": p, "content": c} for p, c in files]}

    alone = await _plan(client, s["key"], other([]))
    assert _problem(alone, "unknown_view")["path"] == "/spec/layout/0/open/view"
    for requires in ([PACKAGE], [{"package": PACKAGE, "version": "^1.0.0"}]):
        plan = await _plan(client, s["key"], other(requires))
        assert _errors(plan) == [], plan["problems"]


# --- the core knows no domain: a second package of another domain -------------------------------


async def test_a_package_of_another_domain_installs_and_serves_its_screens_the_same_way(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install_package(
        client, s["key"], "invoice-payment", kinds=("ArtifactType", "Role", "Skill", "TaskType")
    )
    role = yaml.safe_load(
        (PACKAGES / "invoice-payment" / "roles" / "finance-director.yaml").read_text("utf-8")
    )
    roles = (await client.get("/api/v1/roles", headers=auth(s["key"]))).json()["items"]
    director = next(r for r in roles if r["slug"] == role["key"])
    payer = await _person(client, s["key"], "payer", ["tasks.read"], [director["id"]])
    texts = {
        "invoice-payment.queue.title": "Invoices to pay",
        "invoice-payment.col.number": "Invoice",
        "invoice-payment.col.amount": "Amount",
        "invoice-payment.metric.count": "To pay",
    }
    queue = {
        "title": "invoice-payment.queue.title",
        "audience": {"roles": [role["key"]]},
        "source": {"tasks": {"type": "invoice-payment"}},
        "layout": [
            {
                "block": "table",
                "columns": [
                    {"label": "invoice-payment.col.number", "field": "customFields.invoice"},
                    {
                        "label": "invoice-payment.col.amount",
                        "field": "customFields.invoiceAmount",
                        "format": "money",
                    },
                ],
            },
            {
                "block": "metrics",
                "items": [{"title": "invoice-payment.metric.count", "value": "count()"}],
            },
        ],
    }
    head = {
        "version": "0.1.0",
        "displayName": "Invoice payment (test fixture)",
        "locales": ["ru", "en"],
        "defaultLocale": "ru",
    }
    files = [
        ("package.yaml", _doc("Package", "invoice-payment", head)),
        ("roles/finance-director.yaml", _doc("Role", role["key"], role["spec"])),
        ("views/queue.yaml", _doc("View", "invoice-queue", queue)),
        ("i18n/en.yaml", yaml.safe_dump(texts)),
        (
            "i18n/ru.yaml",
            yaml.safe_dump(
                {**texts, "invoice-payment.queue.title": "К оплате"}, allow_unicode=True
            ),
        ),
    ]
    package = {"files": [{"path": p, "content": c} for p, c in files]}
    await _install(client, s["key"], package)
    await _install(client, s["key"], _package(s["admin"]))

    seen = await _views(client, payer)
    assert [v["key"] for v in seen] == ["invoice-queue"]
    assert seen[0]["title"] == "К оплате" and seen[0]["locale"] == "ru"
    assert seen[0]["source"] == {"kind": "tasks", "instance": False}
    assert seen[0]["package"]["key"] == "invoice-payment"
    english = (await _view(client, payer, "invoice-queue", locale="en")).json()
    assert english["title"] == "Invoices to pay"
    # The clerk of the other package sees its screens and not these.
    assert [v["key"] for v in await _views(client, s["clerk"])] == ["sample-card", "sample-list"]
    assert (await _view(client, s["clerk"], "invoice-queue")).status_code == 404


async def test_a_spec_is_not_changed_by_reading_it(client: httpx.AsyncClient) -> None:
    """Two readers in two languages get each their own strings from one stored revision."""
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    ru = (await _view(client, s["clerk"], "sample-list", locale="ru")).json()
    en = (await _view(client, s["clerk"], "sample-list", locale="en")).json()
    assert ru["hash"] == en["hash"]
    assert copy.deepcopy(ru["source"]) == en["source"]
    assert (ru["title"], en["title"]) == ("Дела", "Cases")


# --- the blocks of TAI-ADR-0066 §2 --------------------------------------------------------------

BOARD_EN = {
    "sample.board.title": "Board",
    "sample.action.match": "Match amounts",
    "sample.action.classify": "Classify",
    "sample.col.over": "Over {limit}",
}
BOARD_RU = {
    "sample.board.title": "Доска",
    "sample.action.match": "Сверить суммы",
    "sample.action.classify": "Разобрать",
    "sample.col.over": "Больше {limit}",
}
THRESHOLD = {
    "params": {"limit": {"type": "number", "required": True}},
    "layout": [
        {
            "block": "list",
            "columns": [{"label": "sample.col.over", "value": "data.amount > param.limit"}],
        }
    ],
}


def _board_view() -> dict[str, Any]:
    return {
        "title": "sample.board.title",
        "audience": {"roles": ["clerk"]},
        "source": {"process": PROCESS},
        "layout": [
            {
                "block": "board",
                "columns": "stages",
                "card": {
                    "title": "data.decision",
                    "subtitle": "data.case.openedAt",
                    "fields": [
                        {"label": "sample.col.amount", "field": "data.amount", "format": "money"}
                    ],
                    "badge": "stage",
                },
                "open": {"view": "sample-card", "params": {"id": "instance.id"}},
            },
            {
                "block": "chart",
                "chart": "donut",
                "groupBy": "stage",
                "value": "sum(data.amount)",
                "format": "money",
            },
            {
                "block": "related",
                "knowledge": {"kind": "record", "key": "data.decision"},
                "include": {"relations": ["party"], "direction": "out"},
            },
            {
                "block": "invoke",
                "label": "sample.action.classify",
                "skill": "invoice.classify_dispute@1",
                "input": {"text": "data.decision"},
            },
            {
                "block": "invoke",
                "label": "sample.action.match",
                "skill": "sample.match@1",
                "input": {"invoiceAmount": "string(data.amount)", "paymentAmount": "'0'"},
            },
            {"block": "component", "component": "threshold", "with": {"limit": "100.0"}},
        ],
    }


def _skill_doc() -> str:
    """A skill of the package itself: ``invoke`` finds it before the catalog."""
    doc = yaml.safe_load(
        (PACKAGES / "invoice-payment" / "skills" / "invoice.amount_match.yaml").read_text("utf-8")
    )
    return _doc("Skill", "sample.match", doc["spec"])


def _board_package(admin: str, board: dict[str, Any] | None = None) -> dict[str, Any]:
    card = _card_view()
    card["layout"].append({"block": "artifacts", "types": ["report"]})
    return _package(
        admin,
        views={"sample-board": board or _board_view(), "sample-card": card},
        en={**EN, **BOARD_EN},
        ru={**RU, **BOARD_RU},
        extra=[
            ("components/threshold.yaml", _doc("Component", "threshold", THRESHOLD)),
            ("skills/sample.match.yaml", _skill_doc()),
        ],
    )


async def test_a_view_written_by_the_decision_installs_with_its_blocks(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    await install_package(client, s["key"], "invoice-payment", kinds=("Skill",))
    await _install(client, s["key"], _board_package(s["admin"]))

    response = await _view(client, s["clerk"], "sample-board", locale="ru")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["title"] == "Доска"
    board, chart, related, classify, match, component = view["layout"]
    assert [b["block"] for b in view["layout"]] == [
        "board",
        "chart",
        "related",
        "invoke",
        "invoke",
        "component",
    ]
    assert_console_form(view)
    # Columns are the stages; title, subtitle and badge of a card come in the data of the view.
    assert board == {
        "block": "board",
        "card": {"fields": [{"key": "amount", "format": "money"}]},
        "open": {"view": "sample-card"},
    }
    assert chart == {"block": "chart", "type": "donut", "format": "money"}
    assert related == {"block": "related", "relations": ["party"]}
    assert classify == {
        "block": "invoke",
        "label": "Разобрать",
        "skill": "invoice.classify_dispute@1",
        "input": {"text": "data.decision"},
    }
    assert match["label"] == "Сверить суммы"
    assert component == {
        "block": "component",
        "component": "threshold",
        "layout": [{"block": "list", "columns": [{"key": "amount", "title": "Больше {limit}"}]}],
    }
    card = (await _view(client, s["clerk"], "sample-card")).json()
    assert card["layout"][-1] == {"block": "artifacts"}


async def test_the_blocks_of_the_decision_are_refused_with_a_path(
    client: httpx.AsyncClient,
) -> None:
    s = await _world(client)
    # Without the catalog's skill: invoke names a skill nobody has.
    plan = await _plan(client, s["key"], _board_package(s["admin"]))
    problem = _problem(plan, "unknown_skill")
    assert (problem["file"], problem["path"]) == ("views/sample-board.yaml", "/spec/layout/3/skill")
    await install_package(client, s["key"], "invoice-payment", kinds=("Skill",))

    board = _board_view()
    board["layout"][5] = {"block": "component", "component": "threshold"}
    plan = await _plan(client, s["key"], _board_package(s["admin"], board))
    problem = _problem(plan, "missing_param")
    assert (problem["file"], problem["path"]) == ("views/sample-board.yaml", "/spec/layout/5/with")

    board = _board_view()
    board["layout"][4]["input"] = {"invoiceAmount": "'1'"}
    plan = await _plan(client, s["key"], _board_package(s["admin"], board))
    assert _problem(plan, "skill_input_missing")["path"] == "/spec/layout/4/input"

    board = _board_view()
    board["layout"][0]["card"]["badge"] = "data.gone"
    plan = await _plan(client, s["key"], _board_package(s["admin"], board))
    assert _problem(plan, "undeclared_path")["path"] == "/spec/layout/0/card/badge"


# --- the views of TAI-ADR-0066 as they are -----------------------------------------------------

TENDERS = Path(__file__).resolve().parents[1] / "fixtures" / "views" / "tenders"


def _tenders_package(admin: str) -> dict[str, Any]:
    """The package of the decision's examples with a process tender over its data schema."""
    process = _spec(admin)
    process["data"] = {"$ref": "../schemas/tender.yaml"}
    files = [
        (path.relative_to(TENDERS).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(TENDERS.rglob("*.yaml"))
    ]
    files.append(("processes/tender.yaml", _doc("Process", "tender", process)))
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def test_the_views_of_the_decision_install_and_are_served_as_the_console_draws_them(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    roles = [
        await create_role(client, s["key"], slug) for slug in ("tender-lead", "tender-finance")
    ]
    lead = await _person(client, s["key"], "lead", ["processes.read"], [roles[0]["id"]])
    reader = await _person(client, s["key"], "reader", ["processes.read"], [])
    package = _tenders_package(s["admin"])
    # general-director is a role of the organization nobody created yet.
    refused = _problem(await _plan(client, s["key"], package), "unknown_role")
    assert (refused["file"], refused["path"]) == (
        "views/tenders-board.yaml",
        "/spec/audience/roles/2",
    )
    await create_role(client, s["key"], "general-director")
    applied = await _install(client, s["key"], package)
    assert {(a["kind"], a["key"]) for a in applied["applied"] if a["kind"] == "View"} == {
        ("View", "tenders-board"),
        ("View", "tender-card"),
    }

    page = await client.get("/api/v1/views", params={"locale": "ru"}, headers=auth(lead))
    assert page.status_code == 200, page.text
    assert_console_form(page.json(), "page")
    assert [v["key"] for v in page.json()["items"]] == ["tender-card", "tenders-board"]
    board = (await _view(client, lead, "tenders-board", locale="ru")).json()
    assert_console_form(board)
    assert {k: board[k] for k in ("title", "nav", "source", "layout")} == TENDERS_BOARD_RU
    card = (await _view(client, lead, "tender-card", locale="en")).json()
    assert_console_form(card)
    assert card["source"] == {"kind": "process", "process": "tender", "instance": True}
    # The board is for its audience; the card, without one, for whoever reads tenders.
    assert [v["key"] for v in await _views(client, reader)] == ["tender-card"]


# --- tenants ------------------------------------------------------------------------------------


async def test_a_view_of_one_tenant_is_not_there_for_another(
    client: httpx.AsyncClient, sync_engine: Any
) -> None:
    s = await _world(client)
    await _install(client, s["key"], _package(s["admin"]))
    _, rival_key = make_tenant_directly(sync_engine, "rival")
    clerk = await create_role(client, rival_key, "clerk")
    rival = await _person(client, rival_key, "rival-clerk", ["processes.read"], [clerk["id"]])
    # The same role slug and the same right in another tenant open nothing here.
    assert await _views(client, rival) == []
    assert await _views(client, rival, package=PACKAGE) == []
    for key in ("sample-list", "sample-card"):
        response = await _view(client, rival, key)
        assert response.status_code == 404, response.text
    # A cursor of the first tenant pages nothing of it for the second.
    first = await client.get("/api/v1/views", params={"limit": 1}, headers=auth(s["clerk"]))
    cursor = first.json()["nextCursor"]
    paged = await client.get(
        "/api/v1/views", params={"limit": 1, "cursor": cursor}, headers=auth(rival)
    )
    assert paged.status_code == 200 and paged.json()["items"] == []
