"""Concurrent ``PATCH /principals/{id}`` with one version: exactly one wins (CP-ADR-0082 §1.4)."""

import asyncio

import httpx

from tests.helpers import auth, do_bootstrap


async def test_concurrent_profile_edits_of_one_version_only_one_wins(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/principals", json={"kind": "human", "displayName": "Ann"}, headers=auth(admin_key)
    )
    principal_id = created.json()["id"]

    async def update(title: str) -> httpx.Response:
        return await client.patch(
            f"/api/v1/principals/{principal_id}",
            json={"profile": {"jobTitle": title}},
            headers={**auth(admin_key), "If-Match": '"principal-1"'},
        )

    responses = await asyncio.gather(*[update(f"Writer {i}") for i in range(5)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409, 409, 409, 409], [r.text for r in responses]
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] == "version_conflict"
            assert response.json()["error"]["details"]["currentVersion"] == 2

    events = await client.get(
        "/api/v1/events",
        params={"types": "principal.updated", "entityId": principal_id},
        headers=auth(admin_key),
    )
    assert len(events.json()["items"]) == 1
