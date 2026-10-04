"""``GET /event-types`` end to end (CP-ADR-0068, amendment of 2026-10-04)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.helpers import auth, create_agent_with_key, do_bootstrap

pytestmark = pytest.mark.usefixtures("clean_database")

URL = "/api/v1/event-types"
CATALOG = Path(__file__).resolve().parents[2] / "docs" / "events" / "catalog.json"
ADDED = ("type", "group", "labelKey", "supportedVersions")


async def _admin(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    return key


async def test_the_catalog_as_published(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    response = await client.get(URL, headers=auth(key))
    assert response.status_code == 200, response.text
    published: dict[str, Any] = json.loads(CATALOG.read_text("utf-8"))["types"]
    body = response.json()
    assert body["locale"] == "en"
    assert [item["type"] for item in body["items"]] == sorted(published)
    for item in body["items"]:
        assert {k: v for k, v in item.items() if k not in ADDED} == published[item["type"]]
    assert response.headers["etag"].startswith('"event-types-')
    assert response.headers["cache-control"] == "private, max-age=300"


@pytest.mark.parametrize("locale", ["ru", "ru-RU", "EN", "pt-BR"])
async def test_a_locale_without_core_strings_gets_english(
    client: httpx.AsyncClient, locale: str
) -> None:
    key = await _admin(client)
    english = await client.get(URL, headers=auth(key))
    asked = await client.get(URL, params={"locale": locale}, headers=auth(key))
    assert asked.status_code == 200, asked.text
    assert asked.json() == english.json()
    assert asked.headers["etag"] == english.headers["etag"]
    approval = next(i for i in asked.json()["items"] if i["type"] == "approval.requested")
    assert approval["labelKey"] == "event.approval.requested"
    assert approval["group"] == "approval"


async def test_if_none_match_is_304_and_a_stale_tag_is_200(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    etag = (await client.get(URL, headers=auth(key))).headers["etag"]

    cached = await client.get(URL, headers={**auth(key), "If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.headers["etag"] == etag
    assert cached.content == b""

    weak = await client.get(URL, headers={**auth(key), "If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304

    stale = await client.get(URL, headers={**auth(key), "If-None-Match": '"event-types-0"'})
    assert stale.status_code == 200
    assert stale.json()["items"]


async def test_events_read_is_required(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    _, key = await create_agent_with_key(client, admin, permissions=["tasks.read"])
    etag = (await client.get(URL, headers=auth(admin))).headers["etag"]

    denied = await client.get(URL, headers=auth(key))
    assert denied.status_code == 403, denied.text
    # A cached tag does not let a caller without the right past the check.
    revalidated = await client.get(URL, headers={**auth(key), "If-None-Match": etag})
    assert revalidated.status_code == 403

    anonymous = await client.get(URL)
    assert anonymous.status_code == 401


async def test_bad_query(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    unknown = await client.get(URL, params={"group": "approval"}, headers=auth(key))
    assert unknown.status_code == 400, unknown.text
    too_long = await client.get(URL, params={"locale": "x" * 36}, headers=auth(key))
    assert too_long.status_code == 422, too_long.text
    assert too_long.json()["error"]["code"] == "invalid_locale"


async def test_parallel_reads_agree(client: httpx.AsyncClient) -> None:
    key = await _admin(client)
    responses = await asyncio.gather(*(client.get(URL, headers=auth(key)) for _ in range(5)))
    assert {r.status_code for r in responses} == {200}
    assert len({r.headers["etag"] for r in responses}) == 1
    assert len({r.content for r in responses}) == 1
