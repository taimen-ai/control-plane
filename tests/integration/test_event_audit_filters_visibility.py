"""The audit filters of the journal under workspace visibility (CP-ADR-0068 Б4, CP-ADR-0082).

A person in ``members`` mode, a member of ``dept`` (so of ``dept`` and
``team``), reads ``GET /events`` with ``actorId``, a period and ``workspaceId``
with ``includeDescendants``. Each filter only narrows what visibility already
let through: the intersection, never an event of ``other`` or ``company``,
and an invisible workspace answers exactly as a missing one.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tests.integration.test_workspace_visibility import same_as_missing
from tests.integration.test_workspace_visibility_routes import MISSING, Tree, make_tree


async def _events(tree: Tree, **params: Any) -> list[dict[str, Any]]:
    response = await tree.get("/api/v1/events", limit=200, **params)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _tree(client: httpx.AsyncClient) -> tuple[Tree, str]:
    """The tree of the people-access tests, work in team and company too; the admin's id."""
    tree = await make_tree(client)
    for name in ("team", "company"):
        tree.tasks[name] = await tree.as_admin(
            "/api/v1/tasks", {"title": f"work of {name}", "workspaceId": tree.ws[name]}
        )
    # The admin, who acted everywhere, read through the tenant-wide admin key.
    page = await tree.get(
        "/api/v1/events", tree.admin, entityType="task", entityId=tree.tasks["other"]["id"]
    )
    assert page.status_code == 200, page.text
    actor: str = page.json()["items"][0]["actorId"]
    return tree, actor


def _workspaces(events: list[dict[str, Any]]) -> set[str | None]:
    return {e["workspaceId"] for e in events}


async def test_actor_id_is_intersected_with_the_visible_workspaces(
    client: httpx.AsyncClient,
) -> None:
    tree, admin = await _tree(client)
    # The admin acted in every workspace; the tenant-wide reader sees it all.
    everything = await tree.get("/api/v1/events", tree.admin, actorId=admin, limit=200)
    assert {tree.ws["other"], tree.ws["company"]} <= _workspaces(everything.json()["items"])

    events = await _events(tree, actorId=admin)
    assert events
    assert {e["actorId"] for e in events} == {admin}
    assert {tree.ws["dept"], tree.ws["team"]} <= _workspaces(events)
    assert not _workspaces(events) & {tree.ws["other"], tree.ws["company"]}
    hidden = {tree.tasks[n]["id"] for n in ("other", "company", "none")}
    assert not [e for e in events if e["entityId"] in hidden]
    # The filter is never wider than the unfiltered journal of the same reader.
    unfiltered = {e["id"] for e in await _events(tree)}
    assert {e["id"] for e in events} <= unfiltered


async def test_actor_id_with_a_cursor_stays_within_the_visible_workspaces(
    client: httpx.AsyncClient,
) -> None:
    tree, admin = await _tree(client)
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"actorId": admin, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = await tree.get("/api/v1/events", **params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen += body["items"]
        cursor = body["nextCursor"]
        if not body["hasMore"]:
            break
    assert [e["id"] for e in seen] == [e["id"] for e in await _events(tree, actorId=admin)]
    assert not _workspaces(seen) & {tree.ws["other"], tree.ws["company"]}


async def test_a_period_is_intersected_with_the_visible_workspaces(
    client: httpx.AsyncClient,
) -> None:
    tree, _admin = await _tree(client)
    now = datetime.now(UTC)
    period = {
        "occurredFrom": (now - timedelta(hours=1)).isoformat(),
        "occurredTo": (now + timedelta(hours=1)).isoformat(),
    }
    events = await _events(tree, **period)
    assert tree.ws["dept"] in _workspaces(events)
    assert not _workspaces(events) & {tree.ws["other"], tree.ws["company"]}
    everything = await tree.get("/api/v1/events", tree.admin, limit=200, **period)
    assert tree.ws["other"] in _workspaces(everything.json()["items"])


async def test_include_descendants_on_a_visible_workspace(
    client: httpx.AsyncClient,
) -> None:
    tree, admin = await _tree(client)
    dept, team = tree.ws["dept"], tree.ws["team"]
    for flag, expected in (("true", {dept, team}), (None, {dept, team}), ("false", {dept})):
        params: dict[str, Any] = {"workspaceId": dept, "actorId": admin}
        if flag is not None:
            params["includeDescendants"] = flag
        assert _workspaces(await _events(tree, **params)) == expected, flag


async def test_include_descendants_on_an_invisible_workspace_is_a_missing_one(
    client: httpx.AsyncClient,
) -> None:
    tree, admin = await _tree(client)
    # company is the parent of dept: its subtree holds visible workspaces, but
    # the workspace itself is not visible, so neither form widens anything.
    for invisible in (tree.ws["company"], tree.ws["other"]):
        for flag in ("true", "false"):
            same_as_missing(
                await tree.get(
                    "/api/v1/events",
                    workspaceId=invisible,
                    includeDescendants=flag,
                    actorId=admin,
                ),
                await tree.get(
                    "/api/v1/events", workspaceId=MISSING, includeDescendants=flag, actorId=admin
                ),
                (invisible, MISSING),
            )
