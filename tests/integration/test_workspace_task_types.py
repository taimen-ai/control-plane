"""Task types allowed in a workspace (CP-ADR-0008, amendment 2026-10-03: A1-A3,
TASK-001306)."""

import asyncio
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)

MISSING = "00000000-0000-0000-0000-000000000000"


async def _admin(client: httpx.AsyncClient) -> str:
    return str((await do_bootstrap(client))["apiKey"]["key"])


async def _type(client: httpx.AsyncClient, key: str, type_key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": type_key.title()},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _get(client: httpx.AsyncClient, key: str, workspace_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _patch(
    client: httpx.AsyncClient, key: str, workspace_id: str, body: dict[str, Any]
) -> httpx.Response:
    current = await _get(client, key, workspace_id)
    return await client.patch(
        f"/api/v1/workspaces/{workspace_id}",
        json=body,
        headers={**auth(key), "If-Match": f'"workspace-{current["version"]}"'},
    )


async def _set(
    client: httpx.AsyncClient, key: str, workspace_id: str, task_types: list[str] | None
) -> dict[str, Any]:
    response = await _patch(client, key, workspace_id, {"taskTypes": task_types})
    assert response.status_code == 200, response.text
    return response.json()


async def _type_keys(
    client: httpx.AsyncClient,
    key: str,
    workspace_id: str | None = None,
    params: dict[str, str] | None = None,
) -> list[str]:
    params = dict(params or {})
    if workspace_id is not None:
        params["workspaceId"] = workspace_id
    response = await client.get("/api/v1/task-types", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return sorted(t["key"] for t in response.json()["items"])


async def _post_task(
    client: httpx.AsyncClient, key: str, workspace_id: str | None, type_key: str | None
) -> httpx.Response:
    body: dict[str, Any] = {"title": "Work", "workspaceId": workspace_id}
    if type_key is not None:
        body["typeKey"] = type_key
    return await client.post("/api/v1/tasks", json=body, headers=auth(key))


async def _tree(client: httpx.AsyncClient, key: str) -> tuple[str, str, str]:
    """root -> child -> grandchild, plus the types ``coding`` and ``review``."""
    await _type(client, key, "coding")
    await _type(client, key, "review")
    root = await create_workspace(client, key, "root")
    child = await create_workspace(client, key, "child", parent_id=root["id"])
    grandchild = await create_workspace(client, key, "grandchild", parent_id=child["id"])
    return root["id"], child["id"], grandchild["id"]


async def test_nothing_set_allows_every_type(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    _, child, _ = await _tree(client, key)

    body = await _get(client, key, child)
    assert (body["taskTypes"], body["effectiveTaskTypes"]) == (None, None)
    # The list carries the own setting too.
    listed = (await client.get("/api/v1/workspaces", headers=auth(key))).json()["items"]
    assert {w["taskTypes"] for w in listed} == {None}
    assert await _type_keys(client, key, child) == await _type_keys(client, key)
    for type_key in ("coding", "review", None):
        response = await _post_task(client, key, child, type_key)
        assert response.status_code == 201, response.text


async def test_root_setting_is_inherited_and_a_descendant_overrides_it(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    root, child, grandchild = await _tree(client, key)

    updated = await _set(client, key, root, ["coding"])
    assert (updated["taskTypes"], updated["effectiveTaskTypes"]) == (["coding"], ["coding"])
    for node in (child, grandchild):
        body = await _get(client, key, node)
        assert (body["taskTypes"], body["effectiveTaskTypes"]) == (None, ["coding"])

    # The child widens the root's list for its own subtree: not an upper bound.
    updated = await _set(client, key, child, ["coding", "review"])
    assert updated["effectiveTaskTypes"] == ["coding", "review"]
    body = await _get(client, key, grandchild)
    assert (body["taskTypes"], body["effectiveTaskTypes"]) == (None, ["coding", "review"])
    assert (await _get(client, key, root))["effectiveTaskTypes"] == ["coding"]

    # The nearest setting wins.
    await _set(client, key, grandchild, ["review"])
    assert (await _get(client, key, grandchild))["effectiveTaskTypes"] == ["review"]

    # null drops the own setting: inherited from the nearest ancestor again.
    updated = await _set(client, key, child, None)
    assert (updated["taskTypes"], updated["effectiveTaskTypes"]) == (None, ["coding"])
    assert (await _get(client, key, grandchild))["effectiveTaskTypes"] == ["review"]


async def test_empty_list_allows_none_and_differs_from_null(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, child, grandchild = await _tree(client, key)

    await _set(client, key, child, [])
    body = await _get(client, key, child)
    assert (body["taskTypes"], body["effectiveTaskTypes"]) == ([], [])
    # [] stops the walk: the descendant inherits "none", not "every type".
    assert (await _get(client, key, grandchild))["effectiveTaskTypes"] == []
    assert await _type_keys(client, key, child) == []
    assert await _type_keys(client, key, grandchild) == []
    for node in (child, grandchild):
        response = await _post_task(client, key, node, "coding")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "task_type_not_allowed"
    # The root above stays unrestricted.
    assert (await _post_task(client, key, root, "coding")).status_code == 201

    await _set(client, key, child, None)
    assert (await _get(client, key, grandchild))["effectiveTaskTypes"] is None
    assert (await _post_task(client, key, grandchild, "coding")).status_code == 201


async def test_task_types_filter_by_workspace(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _admin(client)
    root, child, _ = await _tree(client, key)
    await _type(client, key, "coding")  # a second version of the same key

    every = await _type_keys(client, key)
    assert {"coding", "review"} <= set(every) and len(every) > 3
    await _set(client, key, root, ["coding"])
    # Every version of an allowed key, nothing else; inherited by the child.
    assert await _type_keys(client, key, root) == ["coding", "coding"]
    assert await _type_keys(client, key, child) == ["coding", "coding"]
    # Combines with the other filters.
    assert await _type_keys(client, key, child, {"key": "review"}) == []
    # Without the parameter: as before.
    assert await _type_keys(client, key) == every

    response = await client.get(
        "/api/v1/task-types", params={"workspaceId": "not-a-uuid"}, headers=auth(key)
    )
    assert response.status_code == 400, response.text
    response = await client.get(
        "/api/v1/task-types", params={"workspaceId": MISSING}, headers=auth(key)
    )
    assert response.status_code == 404

    # A workspace of another tenant is as absent as a missing one.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    missing = await client.get(
        "/api/v1/task-types", params={"workspaceId": MISSING}, headers=auth(key_b)
    )
    foreign = await client.get(
        "/api/v1/task-types", params={"workspaceId": root}, headers=auth(key_b)
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]


async def test_post_task_of_a_type_not_allowed_is_refused(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, child, _ = await _tree(client, key)
    await _set(client, key, root, ["coding"])

    response = await _post_task(client, key, child, "review")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "task_type_not_allowed"
    assert error["details"] == {"workspaceId": child, "typeKey": "review"}

    # The system type (no type given) is checked like any other.
    response = await _post_task(client, key, child, None)
    assert response.status_code == 422
    assert response.json()["error"]["details"]["typeKey"] == "task"

    assert (await _post_task(client, key, child, "coding")).status_code == 201
    # Work outside any workspace is not restricted.
    assert (await _post_task(client, key, None, "review")).status_code == 201
    # Nothing was written by the refusals.
    tasks = (
        await client.get("/api/v1/tasks", params={"workspaceId": child}, headers=auth(key))
    ).json()["items"]
    assert [t["typeKey"] for t in tasks] == ["coding"]


async def test_existing_work_is_untouched_by_narrowing(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, _, _ = await _tree(client, key)
    task = await create_task(client, key, workspaceId=root, typeKey="review")

    await _set(client, key, root, ["coding"])
    response = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(key))
    assert response.status_code == 200
    assert response.json()["workspaceId"] == root
    # Editing it without moving it is not re-checked.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed", "workspaceId": root},
        headers={**auth(key), "If-Match": f'"task-{response.json()["version"]}"'},
    )
    assert response.status_code == 200, response.text


async def test_moving_work_into_a_workspace_checks_its_type(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, child, grandchild = await _tree(client, key)
    await _set(client, key, child, ["coding"])
    task = await create_task(client, key, workspaceId=root, typeKey="review")

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"workspaceId": grandchild},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "task_type_not_allowed"
    assert error["details"] == {"workspaceId": grandchild, "typeKey": "review"}
    unchanged = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(key))).json()
    assert (unchanged["workspaceId"], unchanged["version"]) == (root, task["version"])

    # Leaving every workspace is not a filing anywhere.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"workspaceId": None},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert response.status_code == 200, response.text

    coding = await create_task(client, key, workspaceId=root, typeKey="coding")
    response = await client.patch(
        f"/api/v1/tasks/{coding['id']}",
        json={"workspaceId": grandchild},
        headers={**auth(key), "If-Match": f'"task-{coding["version"]}"'},
    )
    assert response.status_code == 200, response.text


async def test_moving_a_workspace_changes_what_it_inherits(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, child, grandchild = await _tree(client, key)
    other = await create_workspace(client, key, "other")
    await _set(client, key, root, ["coding"])

    response = await client.post(
        f"/api/v1/workspaces/{child}:move",
        json={"newParentId": other["id"]},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["effectiveTaskTypes"] is None
    assert (await _get(client, key, grandchild))["effectiveTaskTypes"] is None

    created = await create_workspace(client, key, "fresh", parent_id=root)
    assert (created["taskTypes"], created["effectiveTaskTypes"]) == (None, ["coding"])


async def test_patch_validates_the_keys(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    key = await _admin(client)
    root, _, _ = await _tree(client, key)

    response = await _patch(client, key, root, {"taskTypes": ["coding", "no-such-type"]})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_task_type"
    assert error["details"] == {"field": "taskTypes[1]", "taskType": "no-such-type"}
    assert (await _get(client, key, root))["taskTypes"] is None

    for bad in ("coding", [1], ["coding", "coding"], ["Not A Key"], [""], {"a": 1}):
        response = await _patch(client, key, root, {"taskTypes": bad})
        assert response.status_code == 400, (bad, response.text)
    assert (await _get(client, key, root))["version"] == 1

    # A key registered only by another tenant is unknown here.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    await _type(client, key_b, "foreign")
    response = await _patch(client, key, root, {"taskTypes": ["foreign"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_task_type"

    # A deprecated type is still a registered key.
    review = (await client.get("/api/v1/task-types?key=review", headers=auth(key))).json()
    type_id = review["items"][0]["id"]
    response = await client.post(f"/api/v1/task-types/{type_id}:deprecate", headers=auth(key))
    assert response.status_code == 200, response.text
    assert (await _set(client, key, root, ["review"]))["taskTypes"] == ["review"]


async def test_setting_the_same_value_again_is_no_change(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, child, _ = await _tree(client, key)
    first = await _set(client, key, root, ["coding", "review"])

    response = await _patch(client, key, root, {"taskTypes": ["coding", "review"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_update"
    # null on a workspace that sets nothing is no change either.
    response = await _patch(client, key, child, {"taskTypes": None})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_update"
    # Omitting the field leaves the setting alone.
    response = await _patch(client, key, root, {"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert response.json()["taskTypes"] == first["taskTypes"]


async def test_workspace_updated_event_carries_task_types(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    root, _, _ = await _tree(client, key)
    await _set(client, key, root, ["coding"])
    await _set(client, key, root, None)
    await _patch(client, key, root, {"name": "Renamed"})

    events = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    updates = [e for e in events if e["type"] == "workspace.updated"]
    by_version = {e["payload"]["version"]: e for e in updates}
    assert by_version[2]["payload"]["taskTypes"] == ["coding"]
    assert by_version[2]["payload"]["changes"] == {"taskTypes": True}
    assert by_version[2]["schemaVersion"] == 2
    assert "taskTypes" in by_version[3]["payload"]
    assert by_version[3]["payload"]["taskTypes"] is None
    # An update that leaves the setting alone does not carry it.
    assert "taskTypes" not in by_version[4]["payload"]


async def test_parallel_narrowing_and_filing_never_fail_otherwise(
    client: httpx.AsyncClient,
) -> None:
    key = await _admin(client)
    root, _, _ = await _tree(client, key)

    results = await asyncio.gather(
        _patch(client, key, root, {"taskTypes": ["coding"]}),
        *(_post_task(client, key, root, "review") for _ in range(3)),
    )
    patch, *posts = results
    assert patch.status_code == 200, patch.text
    for response in posts:
        assert response.status_code in (201, 422), response.text
        if response.status_code == 422:
            assert response.json()["error"]["code"] == "task_type_not_allowed"
    # Once the narrowing is committed, the refusal is certain.
    response = await _post_task(client, key, root, "review")
    assert response.status_code == 422
