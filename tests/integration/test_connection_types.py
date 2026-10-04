"""Connection types through the API (CP-ADR-0079 §2, integrations-connections I008).

A provider's package publishes a type under the pair ``(key, version)`` it
names. Publishing the same pair with the same spec again is the same version
(``200``, no event), with another spec ``409``; a type is read by ``key`` (the
latest active version) or ``key@version``; only its status moves, forward,
under ``If-Match``. A spec with material that looks like a secret is ``422``.
"""

import asyncio
import copy
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.helpers import auth, create_agent_with_key, do_bootstrap, make_tenant_directly
from tests.unit.test_connection_type_domain import SPEC, TOKEN_ONLY, spec_with

TYPES = "/api/v1/connection-types"


async def publish(
    client: httpx.AsyncClient,
    key: str,
    *,
    type_key: str = "crm",
    version: int = 1,
    spec: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        TYPES,
        json={"key": type_key, "version": version, "spec": copy.deepcopy(spec or SPEC)},
        headers={**auth(key), **(headers or {})},
    )


async def patch_status(
    client: httpx.AsyncClient, key: str, ref: str, status: str, row_version: int | None
) -> httpx.Response:
    headers = auth(key)
    if row_version is not None:
        headers["If-Match"] = f'"connection-type-{row_version}"'
    return await client.patch(f"{TYPES}/{ref}", json={"status": status}, headers=headers)


async def published_events(client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events", params={"types": "connection_type.published"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_a_version_is_published_once_and_repeated_without_a_change(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    first = await publish(client, admin_key)
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body["key"], body["version"], body["status"], body["rowVersion"]) == (
        "crm",
        1,
        "active",
        1,
    )
    assert body["spec"] == SPEC
    assert body["specHash"].startswith("sha256:")
    assert body["package"] is None

    # A package applied again: the same version, nothing recorded.
    reordered = dict(reversed(list(copy.deepcopy(SPEC).items())))
    again = await publish(client, admin_key, spec=reordered)
    assert again.status_code == 200, again.text
    assert again.json() == body

    other = await publish(client, admin_key, spec=spec_with("displayName", "CRM 2"))
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "connection_type_version_exists"

    events = await published_events(client, admin_key)
    assert len(events) == 1
    assert events[0]["entityType"] == "connection_type"
    assert events[0]["entityId"] == body["id"]
    assert events[0]["payload"] == {"key": "crm", "version": 1, "auth": ["oauth2", "token"]}


async def test_a_field_not_set_reads_back_as_null(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)
    assert created.status_code == 201, created.text
    spec = created.json()["spec"]
    assert spec["oauth2"] is None
    assert spec["description"] is None
    assert spec["accountField"] == {"title": "Workspace", "description": None, "pattern": "[a-z]+"}
    # null is "not set": the response read back is the same version.
    again = await publish(client, admin_key, type_key="tracker", spec=spec)
    assert again.status_code == 200, again.text
    assert again.json()["specHash"] == created.json()["specHash"]


async def test_versions_are_read_by_key_and_by_key_at_version(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    for version, name in ((1, "CRM"), (2, "CRM 2"), (3, "CRM 3")):
        response = await publish(
            client, admin_key, version=version, spec=spec_with("displayName", name)
        )
        assert response.status_code == 201, response.text
    third = (await client.get(f"{TYPES}/crm@3", headers=auth(admin_key))).json()
    moved = await patch_status(client, admin_key, "crm@3", "disabled", third["rowVersion"])
    assert moved.status_code == 200, moved.text

    latest = await client.get(f"{TYPES}/crm", headers=auth(admin_key))
    assert latest.status_code == 200, latest.text
    # The latest ACTIVE version: 3 is disabled.
    assert latest.json()["version"] == 2
    assert latest.headers["ETag"] == '"connection-type-1"'
    pinned = await client.get(f"{TYPES}/crm@3", headers=auth(admin_key))
    assert (pinned.json()["version"], pinned.json()["status"]) == (3, "disabled")
    assert pinned.headers["ETag"] == '"connection-type-2"'

    for missing in ("crm@4", "crm@x", "crm@", "crm@-1", "crm@1234567890", "nothing", "@1"):
        response = await client.get(f"{TYPES}/{missing}", headers=auth(admin_key))
        assert response.status_code == 404, missing
        assert response.json()["error"]["code"] == "not_found"


async def test_a_key_without_an_active_version_has_no_latest(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    assert (await patch_status(client, admin_key, "crm@1", "deprecated", 1)).status_code == 200

    assert (await client.get(f"{TYPES}/crm", headers=auth(admin_key))).status_code == 404
    assert (await client.get(f"{TYPES}/crm@1", headers=auth(admin_key))).status_code == 200


async def test_the_list_filters_by_key_status_and_pages(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key, version=1)
    await publish(client, admin_key, version=2, spec=spec_with("displayName", "CRM 2"))
    await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)
    await patch_status(client, admin_key, "crm@1", "deprecated", 1)

    def refs(page: dict[str, Any]) -> list[str]:
        return [f"{item['key']}@{item['version']}" for item in page["items"]]

    everything = (await client.get(TYPES, headers=auth(admin_key))).json()
    assert refs(everything) == ["tracker@1", "crm@2", "crm@1"]
    assert all(item["package"] is None for item in everything["items"])
    by_key = (await client.get(TYPES, params={"key": "crm"}, headers=auth(admin_key))).json()
    assert refs(by_key) == ["crm@2", "crm@1"]
    deprecated = await client.get(TYPES, params={"status": "deprecated"}, headers=auth(admin_key))
    assert refs(deprecated.json()) == ["crm@1"]
    bad = await client.get(TYPES, params={"status": "gone"}, headers=auth(admin_key))
    assert bad.status_code == 400

    first = (await client.get(TYPES, params={"limit": 2}, headers=auth(admin_key))).json()
    assert refs(first) == ["tracker@1", "crm@2"]
    rest = await client.get(
        TYPES, params={"limit": 2, "cursor": first["nextCursor"]}, headers=auth(admin_key)
    )
    assert refs(rest.json()) == ["crm@1"]
    assert rest.json()["nextCursor"] is None


async def test_the_status_moves_forward_under_if_match(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)

    missing = await client.patch(
        f"{TYPES}/crm@1", json={"status": "deprecated"}, headers=auth(admin_key)
    )
    assert missing.status_code == 428
    malformed = await client.patch(
        f"{TYPES}/crm@1",
        json={"status": "deprecated"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )
    assert malformed.status_code == 400
    stale = await patch_status(client, admin_key, "crm@1", "deprecated", 7)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    deprecated = await patch_status(client, admin_key, "crm@1", "deprecated", 1)
    assert deprecated.status_code == 200, deprecated.text
    assert (deprecated.json()["status"], deprecated.json()["rowVersion"]) == ("deprecated", 2)
    # The same status again changes nothing: the installer may repeat itself.
    repeated = await patch_status(client, admin_key, "crm@1", "deprecated", 2)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["rowVersion"] == 2

    back = await patch_status(client, admin_key, "crm@1", "active", 2)
    assert back.status_code == 409
    assert back.json()["error"]["code"] == "invalid_status_transition"
    disabled = await patch_status(client, admin_key, "crm@1", "disabled", 2)
    assert (disabled.json()["status"], disabled.json()["rowVersion"]) == ("disabled", 3)
    again = await patch_status(client, admin_key, "crm@1", "deprecated", 3)
    assert again.status_code == 409

    # Status moves record no publication.
    assert len(await published_events(client, admin_key)) == 1


async def test_a_status_is_moved_on_one_version_only(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)

    by_key = await patch_status(client, admin_key, "crm", "deprecated", 1)
    assert by_key.status_code == 400
    assert by_key.json()["error"]["code"] == "invalid_request"
    for missing in ("crm@2", "crm@x", "other@1"):
        response = await patch_status(client, admin_key, missing, "deprecated", 1)
        assert response.status_code == 404, missing
    unknown = await client.patch(
        f"{TYPES}/crm@1",
        json={"status": "retired"},
        headers={**auth(admin_key), "If-Match": '"connection-type-1"'},
    )
    assert unknown.status_code == 400
    extra = await client.patch(
        f"{TYPES}/crm@1",
        json={"status": "deprecated", "spec": SPEC},
        headers={**auth(admin_key), "If-Match": '"connection-type-1"'},
    )
    assert extra.status_code == 400


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (spec_with("auth", []), "spec.auth"),
        (spec_with("oauth2", ...), "spec.oauth2"),
        (
            spec_with("oauth2.tokenUrlTemplate", "https://localhost/token"),
            "spec.oauth2.tokenUrlTemplate",
        ),
        (
            spec_with("oauth2.tokenUrlTemplate", "https://10.0.0.1/token"),
            "spec.oauth2.tokenUrlTemplate",
        ),
        (
            spec_with("oauth2.tokenUrlTemplate", "https://x{account}/t"),
            "spec.oauth2.tokenUrlTemplate",
        ),
        (spec_with("accountField.pattern", "([a-z"), "spec.accountField.pattern"),
        (spec_with("settingsSchema", {"type": "string"}), "spec.settingsSchema"),
        (spec_with("defaultKey", "CRM"), "spec.defaultKey"),
    ],
)
async def test_an_invalid_spec_is_invalid_connection_type(
    client: httpx.AsyncClient, spec: dict[str, Any], field: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await publish(client, admin_key, spec=spec)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["details"]["field"]) == ("invalid_connection_type", field)
    assert (await client.get(TYPES, headers=auth(admin_key))).json()["items"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"key": "crm", "version": 1, "spec": spec_with("unknown", True)},
        {"key": "crm", "version": 1, "spec": spec_with("oauth2.pkce", "S256")},
        {"key": "crm", "version": 1, "spec": SPEC, "status": "active"},
        {"key": "CRM", "version": 1, "spec": SPEC},
        {"key": "crm", "version": 0, "spec": SPEC},
        {"key": "crm", "version": None, "spec": SPEC},
        {"key": "crm", "version": 1, "spec": None},
        {"key": "crm", "version": 1, "spec": ["displayName"]},
        {"key": "crm", "version": 1},
    ],
)
async def test_a_body_off_the_contract_is_invalid_request(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.post(TYPES, json=body, headers=auth(admin_key))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("path", "value", "pointer"),
    [
        (
            "oauth2.authorizeUrl",
            "https://www.crm.example/oauth?client_secret=abcdefghijklmnop1234",
            "/oauth2/authorizeUrl",
        ),
        ("description", "Connect with sk-abcdefghijklmnopqrstuvwx", "/description"),
        (
            "settingsSchema",
            {"type": "object", "properties": {"region": {"default": "AKIAABCDEFGHIJKLMNOP"}}},
            "/settingsSchema/properties/region/default",
        ),
        (
            "settingsSchema",
            {"type": "object", "properties": {"clientSecret": {"type": "string"}}},
            "/settingsSchema/properties",
        ),
        (
            "settingsSchema",
            {"type": "object", "properties": {"password": {"type": "string"}}},
            "/settingsSchema/properties",
        ),
    ],
)
async def test_material_like_a_secret_is_refused_and_not_echoed(
    client: httpx.AsyncClient, path: str, value: Any, pointer: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await publish(client, admin_key, spec=spec_with(path, value))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "secret_material_rejected"
    # CP-ADR-0079, the amendment of 2026-10-03: JSON Pointers into ``spec``.
    assert error["details"]["field"] == "spec"
    assert [item["path"] for item in error["details"]["errors"]] == [pointer]
    for material in ("abcdefghijklmnop1234", "sk-abcdefghijklmnopqrstuvwx", "AKIAABCDEFGHIJKLMNOP"):
        assert material not in response.text
    assert (await client.get(TYPES, headers=auth(admin_key))).json()["items"] == []
    assert await published_events(client, admin_key) == []


async def test_the_kind_has_rights_of_its_own(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    _, stranger = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read", "org.manage", "agents.manage"]
    )
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["connections.read"]
    )
    _, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["connections.manage"]
    )

    assert (await client.get(TYPES, headers=auth(stranger))).status_code == 403
    assert (await client.get(f"{TYPES}/crm", headers=auth(stranger))).status_code == 403
    assert (await publish(client, stranger, type_key="other")).status_code == 403
    # Refused before the spec is read: no hint about what is wrong in it.
    refused = await publish(client, stranger, spec=spec_with("auth", []))
    assert refused.status_code == 403

    assert (await client.get(TYPES, headers=auth(reader))).status_code == 200
    assert (await client.get(f"{TYPES}/crm@1", headers=auth(reader))).status_code == 200
    assert (await publish(client, reader, type_key="other")).status_code == 403
    assert (await patch_status(client, reader, "crm@1", "deprecated", 1)).status_code == 403

    assert (await publish(client, manager, type_key="other")).status_code == 201
    assert (await patch_status(client, manager, "crm@1", "deprecated", 1)).status_code == 200


async def test_a_type_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    _, other_key = make_tenant_directly(sync_engine, "other")

    for ref in ("crm", "crm@1"):
        assert (await client.get(f"{TYPES}/{ref}", headers=auth(other_key))).status_code == 404
    assert (await client.get(TYPES, headers=auth(other_key))).json()["items"] == []
    assert (await patch_status(client, other_key, "crm@1", "deprecated", 1)).status_code == 404
    # The other tenant publishes its own crm@1 with another spec.
    other = await publish(client, other_key, spec=spec_with("displayName", "Theirs"))
    assert other.status_code == 201, other.text
    mine = (await client.get(f"{TYPES}/crm@1", headers=auth(admin_key))).json()
    assert mine["spec"]["displayName"] == "CRM"


async def test_the_package_records_the_type_and_the_list_filters_by_it(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    await publish(client, admin_key, type_key="tracker", spec=TOKEN_ONLY)

    recorded = await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": "connections-crm", "version": "1.0.0"},
            "installHash": "sha256:" + "a" * 64,
            "objects": [{"kind": "ConnectionType", "key": "crm"}],
        },
        headers=auth(admin_key),
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["recorded"] == [{"kind": "ConnectionType", "key": "crm"}]

    card = (await client.get(f"{TYPES}/crm", headers=auth(admin_key))).json()
    assert card["package"]["key"] == "connections-crm"
    assert card["package"]["version"] == "1.0.0"
    listed = await client.get(TYPES, params={"package": "connections-crm"}, headers=auth(admin_key))
    assert [item["key"] for item in listed.json()["items"]] == ["crm"]
    tracker = (await client.get(f"{TYPES}/tracker", headers=auth(admin_key))).json()
    assert tracker["package"] is None
    # A version published later is the same object: the link is the key's.
    second = await publish(client, admin_key, version=2, spec=spec_with("displayName", "CRM 2"))
    assert second.json()["package"]["key"] == "connections-crm"

    unknown = await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": "connections-crm", "version": "1.0.0"},
            "objects": [{"kind": "ConnectionType", "key": "nothing"}],
        },
        headers=auth(admin_key),
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_object"


async def test_recording_a_type_needs_the_right_that_writes_it(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    _, planner = await create_agent_with_key(
        client, admin_key, name="planner", permissions=["packages.plan"]
    )
    body = {
        "package": {"key": "connections-crm", "version": "1.0.0"},
        "objects": [{"kind": "ConnectionType", "key": "crm"}],
    }
    refused = await client.post("/api/v1/packages:record", json=body, headers=auth(planner))
    assert refused.status_code == 403
    _, installer = await create_agent_with_key(
        client, admin_key, name="installer", permissions=["packages.plan", "connections.manage"]
    )
    recorded = await client.post("/api/v1/packages:record", json=body, headers=auth(installer))
    assert recorded.status_code == 200, recorded.text


async def test_parallel_publications_of_one_pair_make_one_version(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    responses = await asyncio.gather(*(publish(client, admin_key) for _ in range(5)))

    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(await published_events(client, admin_key)) == 1


async def test_an_idempotency_key_replays_the_publication(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    headers = {"Idempotency-Key": "publish-crm-1"}
    first = await publish(client, admin_key, headers=headers)
    replay = await publish(client, admin_key, headers=headers)
    assert (first.status_code, replay.status_code) == (201, 201)
    assert replay.json() == first.json()


async def test_a_version_is_immutable_even_against_raw_sql(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await publish(client, admin_key)).json()

    for statement in (
        "UPDATE connection_types SET spec = '{}'::jsonb WHERE id = :id",
        "UPDATE connection_types SET display_name = 'x' WHERE id = :id",
        "UPDATE connection_types SET version = 2 WHERE id = :id",
        "DELETE FROM connection_types WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), sync_engine.begin() as conn:
            conn.execute(text(statement), {"id": created["id"]})
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE connection_types SET status = 'disabled', row_version = 2 WHERE id = :id"),
            {"id": created["id"]},
        )
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE connection_types SET status = 'active' WHERE id = :id"),
            {"id": created["id"]},
        )
