"""NUL (U+0000) in a JSON body is ``422 validation_error``, not a 500 from PostgreSQL.

CP-ADR-0083: one check at the API boundary, before authentication and the
database; ``details.errors[{path, code: "nul_character"}]`` with JSON Pointers
and never the value.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_task, do_bootstrap

NUL = "\x00"


def _counts(sync_engine: Engine) -> dict[str, int]:
    with sync_engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in ("principals", "tasks", "task_comments", "events")
        }


def _assert_nul_rejected(response: httpx.Response, paths: list[str]) -> None:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [e["path"] for e in error["details"]["errors"]] == paths
    assert {e["code"] for e in error["details"]["errors"]} == {"nul_character"}
    # The value never comes back: neither the NUL nor the text around it.
    assert "\\u0000" not in response.text
    assert "secret-ish" not in response.text


@pytest.fixture
async def admin_key(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    return key


async def test_principal_display_name(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    before = _counts(sync_engine)
    response = await client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": f"secret-ish{NUL}"},
        headers=auth(admin_key),
    )
    _assert_nul_rejected(response, ["/displayName"])
    assert _counts(sync_engine) == before


async def test_principal_profile_patch_raw_body(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    # The route reads the raw body itself; no content type is sent at all.
    principal = (
        await client.post(
            "/api/v1/principals",
            json={"kind": "human", "displayName": "Ann"},
            headers=auth(admin_key),
        )
    ).json()
    before = _counts(sync_engine)
    response = await client.patch(
        f"/api/v1/principals/{principal['id']}",
        content=b'{"profile": {"title": "secret-ish\\u0000"}}',
        headers={**auth(admin_key), "If-Match": '"principal-1"'},
    )
    _assert_nul_rejected(response, ["/profile/title"])
    assert _counts(sync_engine) == before
    stored = (
        await client.get(f"/api/v1/principals/{principal['id']}", headers=auth(admin_key))
    ).json()
    assert stored["version"] == 1


async def test_task_title_and_custom_fields(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    before = _counts(sync_engine)
    response = await client.post(
        "/api/v1/tasks",
        json={
            "title": f"secret-ish{NUL}",
            "customFields": {
                "plain": "ok",
                "nested": {"list": ["ok", f"a{NUL}b"], f"key{NUL}": 1},
                "slash/key": NUL,
            },
        },
        headers=auth(admin_key),
    )
    _assert_nul_rejected(
        response,
        [
            "/title",
            "/customFields/nested",
            "/customFields/nested/list/1",
            "/customFields/slash~1key",
        ],
    )
    assert _counts(sync_engine) == before


async def test_task_comment(client: httpx.AsyncClient, admin_key: str, sync_engine: Engine) -> None:
    task = await create_task(client, admin_key)
    before = _counts(sync_engine)
    response = await client.post(
        f"/api/v1/tasks/{task['id']}/comments",
        json={"body": f"secret-ish{NUL}"},
        headers=auth(admin_key),
    )
    _assert_nul_rejected(response, ["/body"])
    assert _counts(sync_engine) == before


async def test_rejected_before_authentication(client: httpx.AsyncClient) -> None:
    # The body is checked before the credential: nothing reaches the database.
    response = await client.post("/api/v1/tasks", json={"title": NUL})
    _assert_nul_rejected(response, ["/title"])


async def test_escaped_backslash_is_not_nul(client: httpx.AsyncClient, admin_key: str) -> None:
    # "\\u0000" in JSON is a backslash followed by "u0000", a legal string.
    task = await create_task(client, admin_key, title="\\u0000 literally")
    assert task["title"] == "\\u0000 literally"


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"title": "x\\u0000"', "application/json"),  # not JSON: the route decides
        (b"title=x%00", "application/x-www-form-urlencoded"),  # not a JSON body
    ],
)
async def test_non_json_bodies_are_left_to_the_route(
    client: httpx.AsyncClient, admin_key: str, content: bytes, content_type: str
) -> None:
    response = await client.post(
        "/api/v1/tasks",
        content=content,
        headers={**auth(admin_key), "Content-Type": content_type},
    )
    assert response.status_code != 422 or response.json()["error"]["code"] != "validation_error"
    assert response.status_code < 500


async def test_json_suffix_media_type_is_checked(client: httpx.AsyncClient, admin_key: str) -> None:
    response = await client.post(
        "/api/v1/tasks",
        content=b'{"title": "x\\u0000"}',
        headers={**auth(admin_key), "Content-Type": "application/merge-patch+json; charset=utf-8"},
    )
    _assert_nul_rejected(response, ["/title"])


async def test_clean_body_passes_through(client: httpx.AsyncClient, admin_key: str) -> None:
    payload: dict[str, Any] = {"title": "plain", "customFields": {"a": ["b", {"c": "d"}]}}
    response = await client.post("/api/v1/tasks", json=payload, headers=auth(admin_key))
    assert response.status_code == 201, response.text


# -- query string and path parameters (CP-ADR-0083 §7) --------------------------


def _assert_parameter_nul_rejected(response: httpx.Response, paths: list[str]) -> None:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [e["path"] for e in error["details"]["errors"]] == paths
    assert {e["code"] for e in error["details"]["errors"]} == {"nul_character"}
    assert "\\u0000" not in response.text
    assert "secret-ish" not in response.text


async def test_query_parameter(client: httpx.AsyncClient, admin_key: str) -> None:
    response = await client.get("/api/v1/tasks?status=secret-ish%00", headers=auth(admin_key))
    _assert_parameter_nul_rejected(response, ["query.status"])


async def test_every_query_parameter_with_nul_is_named_in_order(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    response = await client.get(
        "/api/v1/tasks?q=a%00&status=ok&systemStatusCategory=%00&priority=b%00",
        headers=auth(admin_key),
    )
    _assert_parameter_nul_rejected(
        response, ["query.q", "query.systemStatusCategory", "query.priority"]
    )


async def test_repeated_query_parameter_is_reported_once(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    response = await client.get("/api/v1/tasks?status=a%00&status=b%00", headers=auth(admin_key))
    _assert_parameter_nul_rejected(response, ["query.status"])


async def test_typed_query_parameter(client: httpx.AsyncClient, admin_key: str) -> None:
    # A NUL is reported as such, not as a failed integer parse.
    response = await client.get("/api/v1/tasks?limit=1%00", headers=auth(admin_key))
    _assert_parameter_nul_rejected(response, ["query.limit"])


async def test_path_parameter(client: httpx.AsyncClient, admin_key: str) -> None:
    response = await client.get("/api/v1/tasks/secret-ish%00", headers=auth(admin_key))
    _assert_parameter_nul_rejected(response, ["path.task_ref"])


async def test_path_parameter_on_write(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    before = _counts(sync_engine)
    response = await client.post(
        "/api/v1/tasks/TASK-1%00/comments", json={"body": "ok"}, headers=auth(admin_key)
    )
    _assert_parameter_nul_rejected(response, ["path.task_ref"])
    assert _counts(sync_engine) == before


async def test_parameter_rejected_before_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/tasks?status=%00")
    _assert_parameter_nul_rejected(response, ["query.status"])


async def test_unknown_parameter_with_nul_stays_unknown(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    # CP-ADR-0058 answers first: the name itself is not understood.
    response = await client.get("/api/v1/tasks?bogus=%00", headers=auth(admin_key))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


async def test_clean_parameters_pass_through(client: httpx.AsyncClient, admin_key: str) -> None:
    task = await create_task(client, admin_key)
    response = await client.get("/api/v1/tasks?status=open&q=%5Cu0000", headers=auth(admin_key))
    assert response.status_code == 200, response.text
    response = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert response.status_code == 200, response.text
