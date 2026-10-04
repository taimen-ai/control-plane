"""Connections through the API (CP-ADR-0079 §3, §4, integrations-connections I009).

A connection is created ``pending`` on the latest active version of its type;
two accounts of one type are two connections with different keys. A person
edits the display name, the settings (by the type's ``settingsSchema``) and the
type version under ``If-Match``; the connector — an agent of the registry
whose current revision names the connection — reports that access still works
or has expired. No route here touches the secret store.
"""

import asyncio
import copy
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.helpers import auth, create_agent_with_key, do_bootstrap, make_tenant_directly
from tests.integration.test_agent_registry import _link, _publish, _tenant, coder_spec
from tests.integration.test_connection_types import patch_status, publish
from tests.unit.test_connection_type_domain import SPEC, spec_with

CONNECTIONS = "/api/v1/connections"


async def create(
    client: httpx.AsyncClient,
    api_key: str,
    /,
    headers: dict[str, str] | None = None,
    **body: Any,
) -> httpx.Response:
    return await client.post(
        CONNECTIONS, json={"type": "crm", **body}, headers={**auth(api_key), **(headers or {})}
    )


async def patch(
    client: httpx.AsyncClient, key: str, connection: str, version: int | None, **body: Any
) -> httpx.Response:
    headers = auth(key)
    if version is not None:
        headers["If-Match"] = f'"connection-{version}"'
    return await client.patch(f"{CONNECTIONS}/{connection}", json=body, headers=headers)


async def report(
    client: httpx.AsyncClient, key: str, connection: str, **body: Any
) -> httpx.Response:
    return await client.put(f"{CONNECTIONS}/{connection}/status", json=body, headers=auth(key))


async def events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/events", params={"types": event_type}, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _activate(sync_engine: Engine, connection_id: str, connected_by: str) -> None:
    """What I010 does on a successful authorization, without the secret store."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE connections SET status = 'active', auth = 'token', account = 'acme',"
                " secret_ref = 'kv/data/tenants/t/connections/crm', connected_by = :by,"
                " connected_at = now() WHERE id = :id"
            ),
            {"id": connection_id, "by": connected_by},
        )


def _name_connections(sync_engine: Engine, agent_key: str, connections: list[str]) -> None:
    """A new current revision of the agent whose spec names ``connections``.

    ``Agent.spec.connections`` is accepted by the API with I011; until then the
    revision is written the way the registry writes one.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_revisions (id, tenant_id, agent_id, revision, spec, spec_hash,"
                " source_kind, created_by, created_at)"
                " SELECT :id, r.tenant_id, r.agent_id, r.revision + 1,"
                " r.spec || jsonb_build_object('connections', CAST(:names AS jsonb)),"
                " 'sha256:' || repeat('0', 64), 'manual', r.created_by, now()"
                " FROM agent_revisions r JOIN agents a"
                " ON a.id = r.agent_id AND a.current_revision = r.revision WHERE a.key = :key"
            ),
            {"id": str(uuid.uuid4()), "names": json.dumps(connections), "key": agent_key},
        )
        conn.execute(
            text("UPDATE agents SET current_revision = current_revision + 1 WHERE key = :key"),
            {"key": agent_key},
        )


# --- creation -------------------------------------------------------------------


async def test_a_connection_is_created_pending_with_the_defaults_of_its_type(
    client: httpx.AsyncClient,
) -> None:
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    assert (await publish(client, admin_key)).status_code == 201
    assert (
        await publish(client, admin_key, version=2, spec=spec_with("displayName", "CRM 2"))
    ).status_code == 201

    created = await create(client, admin_key)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["key"] == "crm"
    assert (body["type"], body["typeVersion"], body["displayName"]) == ("crm", 2, "CRM 2")
    assert (body["status"], body["auth"], body["account"], body["secretRef"]) == (
        "pending",
        None,
        None,
        None,
    )
    assert body["settings"] == {}
    for unset in (
        "statusReason",
        "statusMessage",
        "expiresAt",
        "connectedBy",
        "connectedAt",
        "lastCheckedAt",
    ):
        assert body[unset] is None, unset
    assert body["version"] == 1
    assert body["createdBy"] == admin["adminPrincipal"]["id"]
    assert "agents" not in body

    recorded = await events(client, admin_key, "connection.created")
    assert len(recorded) == 1
    assert (recorded[0]["entityType"], recorded[0]["entityId"]) == ("connection", body["id"])
    assert recorded[0]["payload"] == {
        "key": "crm",
        "type": "crm",
        "typeVersion": 2,
        "status": "pending",
    }


async def test_two_accounts_of_one_type_are_two_connections_with_different_keys(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)

    first = await create(client, admin_key)
    assert first.status_code == 201, first.text
    taken = await create(client, admin_key)
    assert taken.status_code == 409, taken.text
    assert taken.json()["error"]["code"] == "connection_key_taken"
    assert taken.json()["error"]["details"] == {"key": "crm"}
    second = await create(
        client,
        admin_key,
        key="crm-sales",
        displayName="CRM of sales",
        settings={"pipelineId": 7},
    )
    assert second.status_code == 201, second.text
    assert (second.json()["key"], second.json()["type"]) == ("crm-sales", "crm")
    assert second.json()["settings"] == {"pipelineId": 7}
    assert second.json()["id"] != first.json()["id"]

    listing = await client.get(CONNECTIONS, headers=auth(admin_key))
    assert [c["key"] for c in listing.json()["items"]] == ["crm-sales", "crm"]
    assert len(await events(client, admin_key, "connection.created")) == 2


async def test_null_optional_fields_take_the_defaults(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    created = await create(client, admin_key, key=None, displayName=None, settings=None)
    assert created.status_code == 201, created.text
    assert (created.json()["key"], created.json()["displayName"]) == ("crm", "CRM")
    assert created.json()["settings"] == {}


async def test_a_type_without_an_active_version_takes_no_connection(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    unknown = await create(client, admin_key)
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "unknown_connection_type"

    await publish(client, admin_key)
    assert (await patch_status(client, admin_key, "crm@1", "deprecated", 1)).status_code == 200
    deprecated = await create(client, admin_key)
    assert deprecated.status_code == 422, deprecated.text
    assert deprecated.json()["error"]["code"] == "unknown_connection_type"


async def test_settings_follow_the_schema_of_the_type_and_carry_no_secret(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)

    wrong = await create(client, admin_key, settings={"pipelineId": "seven"})
    assert wrong.status_code == 422, wrong.text
    error = wrong.json()["error"]
    assert error["code"] == "invalid_connection_settings"
    assert error["details"] == {
        "field": "settings",
        "errors": [{"path": "/pipelineId", "code": "type", "message": "must be of type integer"}],
    }
    assert "seven" not in wrong.text

    # A name like a secret: the path of the object it stands in (CP-ADR-0079,
    # the amendment of 2026-10-03), the form CP-ADR-0081 4.3 has.
    for secret, path in (
        ({"password": "x"}, "/"),
        ({"nested": {"apiKey": "x"}}, "/nested"),
        ({"clientSecret": "x"}, "/"),
    ):
        refused = await create(client, admin_key, settings=secret)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "secret_material_rejected"
        assert error["details"] == {
            "field": "settings",
            "errors": [{"path": path, "match": "secret_name"}],
        }

    # Credential-shaped strings are refused by value, under any name and at any
    # depth; the refusal names the path and the kind, never the value.
    material = "ghp_" + "a1B2" * 9
    for secret, path in (
        ({"note": material}, "/note"),
        ({"stages": [{"label": f"Bearer {'x' * 24}"}]}, "/stages/0/label"),
        ({"pipelineId": 1, "comment": f"api_key={'k' * 20}"}, "/comment"),
    ):
        refused = await create(client, admin_key, settings=secret)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "secret_material_rejected"
        assert [e["path"] for e in refused.json()["error"]["details"]["errors"]] == [path]
        assert material not in refused.text
        assert "x" * 24 not in refused.text
        assert "k" * 20 not in refused.text
    assert (await create(client, admin_key, settings=["x"])).status_code == 400
    assert (await create(client, admin_key, key="Not A Key")).status_code == 400
    assert (await create(client, admin_key, displayName="")).status_code == 400
    assert (await create(client, admin_key, account="acme")).status_code == 400
    assert (await client.post(CONNECTIONS, json={}, headers=auth(admin_key))).status_code == 400
    assert await events(client, admin_key, "connection.created") == []

    # Ordinary words about secrets are not material.
    plain = await create(client, admin_key, key="crm-plain", settings={"note": "token rotates"})
    assert plain.status_code == 201, plain.text


async def test_parallel_creations_of_one_key_make_one_connection(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    responses = await asyncio.gather(*(create(client, admin_key) for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    assert len(await events(client, admin_key, "connection.created")) == 1


async def test_an_idempotency_key_replays_the_creation(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    headers = {"Idempotency-Key": "create-crm"}
    first = await create(client, admin_key, headers=headers)
    replay = await create(client, admin_key, headers=headers)
    assert (first.status_code, replay.status_code) == (201, 201)
    assert replay.json() == first.json()
    assert len(await events(client, admin_key, "connection.created")) == 1


# --- reading --------------------------------------------------------------------


async def test_the_list_filters_by_type_and_status_and_pages(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    await publish(client, admin_key)
    await publish(client, admin_key, type_key="tracker", spec=spec_with("defaultKey", "tracker"))
    crm = (await create(client, admin_key)).json()
    await create(client, admin_key, type="tracker")
    await create(client, admin_key, key="crm-2")
    _activate(sync_engine, crm["id"], admin["adminPrincipal"]["id"])

    def keys(response: httpx.Response) -> list[str]:
        assert response.status_code == 200, response.text
        return [item["key"] for item in response.json()["items"]]

    listing = await client.get(CONNECTIONS, params={"type": "crm"}, headers=auth(admin_key))
    assert keys(listing) == ["crm-2", "crm"]
    active = await client.get(CONNECTIONS, params={"status": "active"}, headers=auth(admin_key))
    assert keys(active) == ["crm"]
    assert all("agents" not in item for item in active.json()["items"])
    none = await client.get(CONNECTIONS, params={"type": "nothing"}, headers=auth(admin_key))
    assert keys(none) == []
    bad = await client.get(CONNECTIONS, params={"status": "gone"}, headers=auth(admin_key))
    assert bad.status_code == 400

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await client.get(CONNECTIONS, params=params, headers=auth(admin_key))
        seen += keys(page)
        cursor = page.json()["nextCursor"]
        if cursor is None:
            break
    assert seen == ["crm-2", "tracker", "crm"]


async def test_the_card_carries_the_agents_and_an_etag(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, workspace = await _tenant(client)
    await publish(client, admin_key)
    await create(client, admin_key)
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    await _publish(client, admin_key, coder_spec(workspace["id"]), agent="reviewer")
    await _publish(client, admin_key, coder_spec(workspace["id"]), agent="retired")

    card = await client.get(f"{CONNECTIONS}/crm", headers=auth(admin_key))
    assert card.status_code == 200, card.text
    assert card.json()["agents"] == []
    assert card.headers["ETag"] == '"connection-1"'

    for agent in ("reviewer", "coder", "retired"):
        _name_connections(sync_engine, agent, ["crm", "other"])
    _name_connections(sync_engine, "reviewer", ["other"])  # no longer in the current revision
    retired = await client.post(
        "/api/v1/agents/retired:retire", json={"reason": "gone"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    card = await client.get(f"{CONNECTIONS}/crm", headers=auth(admin_key))
    assert card.json()["agents"] == ["coder"]

    missing = await client.get(f"{CONNECTIONS}/nothing", headers=auth(admin_key))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_a_connection_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    await create(client, admin_key)
    _, other_key = make_tenant_directly(sync_engine, "other")

    assert (await client.get(f"{CONNECTIONS}/crm", headers=auth(other_key))).status_code == 404
    assert (await client.get(CONNECTIONS, headers=auth(other_key))).json()["items"] == []
    assert (await patch(client, other_key, "crm", 1, displayName="x")).status_code == 404
    # The other tenant has no type of the key: a type is the tenant's own.
    assert (await create(client, other_key)).json()["error"]["code"] == "unknown_connection_type"
    # The same key in another tenant is another connection.
    await publish(client, other_key)
    assert (await create(client, other_key)).status_code == 201


# --- editing --------------------------------------------------------------------


async def test_a_person_edits_name_settings_and_type_version_under_if_match(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    created = (await create(client, admin_key, settings={"pipelineId": 1})).json()

    assert (await patch(client, admin_key, "crm", None, displayName="x")).status_code == 428
    stale = await patch(client, admin_key, "crm", 7, displayName="x")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    renamed = await patch(client, admin_key, "crm", 1, displayName="CRM of sales")
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["displayName"], renamed.json()["version"]) == ("CRM of sales", 2)
    assert renamed.json()["updatedAt"] > created["updatedAt"]
    assert "agents" not in renamed.json()

    # The same values again change nothing: no version, no event.
    same = await patch(
        client, admin_key, "crm", 2, displayName="CRM of sales", settings={"pipelineId": 1}
    )
    assert same.status_code == 200, same.text
    assert same.json()["version"] == 2

    replaced = await patch(client, admin_key, "crm", 2, settings={})
    assert replaced.status_code == 200, replaced.text
    assert (replaced.json()["settings"], replaced.json()["version"]) == ({}, 3)

    invalid = await patch(client, admin_key, "crm", 3, settings={"pipelineId": "x"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_connection_settings"
    secret = await patch(client, admin_key, "crm", 3, settings={"token": "x"})
    assert secret.json()["error"]["code"] == "secret_material_rejected"
    assert secret.json()["error"]["details"]["errors"] == [{"path": "/", "match": "secret_name"}]
    material = await patch(client, admin_key, "crm", 3, settings={"note": "sk-" + "z" * 20})
    assert material.status_code == 422, material.text
    assert material.json()["error"]["code"] == "secret_material_rejected"
    assert material.json()["error"]["details"]["errors"] == [
        {"path": "/note", "match": "provider_token"}
    ]
    assert "z" * 20 not in material.text

    recorded = await events(client, admin_key, "connection.updated")
    assert [e["payload"] for e in recorded] == [
        {"key": "crm", "version": 2, "changes": ["displayName"]},
        {"key": "crm", "version": 3, "changes": ["settings"]},
    ]
    assert all(e["entityId"] == created["id"] for e in recorded)

    for body in ({}, {"displayName": None}, {"typeVersion": None}, {"status": "active"}):
        response = await patch(client, admin_key, "crm", 3, **body)
        assert response.status_code == 400, body
    assert (await patch(client, admin_key, "nothing", 1, displayName="x")).status_code == 404


async def test_field_errors_of_settings_are_json_pointers_without_values(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0079, the amendment of 2026-10-03: every violation at once, no value."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    strict = spec_with(
        "settingsSchema",
        {
            "type": "object",
            "required": ["pipelineId"],
            "properties": {
                "pipelineId": {"type": "integer"},
                "a/b": {"enum": ["won", "lost"]},
            },
            "additionalProperties": False,
        },
    )
    assert (await publish(client, admin_key, spec=strict)).status_code == 201
    value = "quite-unique-value-41"
    wrong = await create(client, admin_key, settings={"a/b": value, "extra": value})
    assert wrong.status_code == 422, wrong.text
    error = wrong.json()["error"]
    assert error["code"] == "invalid_connection_settings"
    assert [(e["path"], e["code"]) for e in error["details"]["errors"]] == [
        ("/a~1b", "enum"),
        ("/extra", "additionalProperties"),
        ("/pipelineId", "required"),
    ]
    assert value not in wrong.text

    # Secrets are found before the schema, all of them, and none comes back.
    material = "ghp_" + "c3D4" * 9
    refused = await create(
        client,
        admin_key,
        settings={"pipelineId": "x", "note": material, "list": [material], "apiToken": 1},
    )
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "secret_material_rejected"
    assert error["details"]["errors"] == [
        {"path": "/note", "match": "provider_token"},
        {"path": "/list/0", "match": "provider_token"},
        {"path": "/", "match": "secret_name"},
    ]
    assert material not in refused.text
    listed = await client.get(CONNECTIONS, headers=auth(admin_key))
    assert listed.json()["items"] == []


async def test_the_type_version_moves_only_to_a_usable_version_of_the_same_type(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    await create(client, admin_key)
    strict = copy.deepcopy(SPEC)
    strict["settingsSchema"] = {
        "type": "object",
        "required": ["stage"],
        "properties": {"stage": {"type": "string"}},
    }
    await publish(client, admin_key, version=2, spec=strict)
    await publish(client, admin_key, version=3)
    assert (await patch_status(client, admin_key, "crm@3", "disabled", 1)).status_code == 200
    await publish(client, admin_key, type_key="tracker", version=4)

    # The settings are checked by the schema of the version after the edit.
    refused = await patch(client, admin_key, "crm", 1, typeVersion=2)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_connection_settings"
    assert refused.json()["error"]["details"]["errors"] == [
        {"path": "/stage", "code": "required", "message": "is required"}
    ]
    moved = await patch(client, admin_key, "crm", 1, typeVersion=2, settings={"stage": "won"})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["typeVersion"], moved.json()["version"]) == (2, 2)
    recorded = await events(client, admin_key, "connection.updated")
    assert recorded[0]["payload"]["changes"] == ["settings", "typeVersion"]

    for version in (3, 4, 9):  # disabled, another type's, missing
        response = await patch(client, admin_key, "crm", 2, typeVersion=version)
        assert response.status_code == 422, version
        assert response.json()["error"]["code"] == "unknown_connection_type"

    # Back to a deprecated version is allowed: it is not disabled.
    assert (await patch_status(client, admin_key, "crm@1", "deprecated", 1)).status_code == 200
    back = await patch(client, admin_key, "crm", 2, typeVersion=1, settings={})
    assert back.status_code == 200, back.text


async def test_a_connection_on_a_disabled_version_edits_only_its_name(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0079 §3: settings are checked by a usable version of the type.

    A connection whose version was disabled keeps working and can be renamed,
    but a ``settings`` edit without a move to a usable version is refused: the
    schema of a disabled version is not a contract to write against.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    await create(client, admin_key)
    await publish(client, admin_key, version=2)
    assert (await patch_status(client, admin_key, "crm@1", "disabled", 1)).status_code == 200

    settings_only = await patch(client, admin_key, "crm", 1, settings={"pipelineId": 2})
    assert settings_only.status_code == 422, settings_only.text
    assert settings_only.json()["error"]["code"] == "unknown_connection_type"
    assert settings_only.json()["error"]["details"] == {"type": "crm", "typeVersion": 1}

    renamed = await patch(client, admin_key, "crm", 1, displayName="Old CRM")
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["typeVersion"], renamed.json()["version"]) == (1, 2)

    moved = await patch(client, admin_key, "crm", 2, typeVersion=2, settings={"pipelineId": 2})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["typeVersion"], moved.json()["settings"]) == (2, {"pipelineId": 2})


# --- rights ---------------------------------------------------------------------


async def test_the_resource_has_rights_of_its_own(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    await create(client, admin_key)
    _, stranger = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read", "agents.manage"]
    )
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["connections.read"]
    )
    _, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["connections.manage"]
    )
    _, connector = await create_agent_with_key(
        client, admin_key, name="connector", permissions=["connections.status.write"]
    )

    def denied(response: httpx.Response) -> bool:
        return (
            response.status_code == 403 and response.json()["error"]["code"] == "permission_denied"
        )

    for key in (stranger, connector):
        assert denied(await client.get(CONNECTIONS, headers=auth(key)))
        assert denied(await client.get(f"{CONNECTIONS}/crm", headers=auth(key)))
    assert (await client.get(f"{CONNECTIONS}/crm", headers=auth(reader))).status_code == 200
    for key in (stranger, reader, connector):
        assert denied(await create(client, key, key="crm-2"))
        assert denied(await patch(client, key, "crm", 1, displayName="x"))
    assert (await create(client, manager, key="crm-2")).status_code == 201
    assert (await patch(client, manager, "crm", 1, displayName="x")).status_code == 200

    checked = {"status": "active", "checkedAt": "2026-09-30T10:00:00Z"}
    # connections.manage does not include the connector's right.
    for key in (stranger, reader, manager):
        assert denied(await report(client, key, "crm", **checked))


# --- the connector's report -----------------------------------------------------


async def _connector(
    client: httpx.AsyncClient, sync_engine: Engine, connections: list[str]
) -> tuple[str, dict[str, Any], str]:
    """(admin key, the connection ``crm``, connector key): agent ``coder`` names ``connections``."""
    admin_key, workspace = await _tenant(client)
    await publish(client, admin_key)
    created = await create(client, admin_key)
    assert created.status_code == 201, created.text
    await _publish(client, admin_key, coder_spec(workspace["id"]))
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    _name_connections(sync_engine, "coder", connections)
    response = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": ["connections.status.write"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return admin_key, created.json(), response.json()["key"]


async def test_the_connector_reports_that_access_expired(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, created, connector = await _connector(client, sync_engine, ["crm"])
    admin_id = created["createdBy"]

    pending = await report(
        client, connector, "crm", status="active", checkedAt="2026-09-30T10:00:00Z"
    )
    assert pending.status_code == 409, pending.text
    assert pending.json()["error"]["code"] == "connection_status_conflict"

    _activate(sync_engine, created["id"], admin_id)
    checked = await report(
        client, connector, "crm", status="active", checkedAt="2026-09-30T10:00:00Z"
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert (body["status"], body["version"]) == ("active", 1)
    assert body["lastCheckedAt"] == "2026-09-30T10:00:00Z"
    assert body["updatedAt"] == created["updatedAt"]

    stale = await report(
        client,
        connector,
        "crm",
        status="expired",
        reason="refresh_rejected",
        checkedAt="2026-09-30T09:59:59Z",
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_status_report"

    no_reason = await report(
        client, connector, "crm", status="expired", checkedAt="2026-09-30T11:00:00Z"
    )
    assert no_reason.status_code == 422
    assert no_reason.json()["error"]["code"] == "status_reason_required"

    material = "sk-" + "a" * 24
    expired = await report(
        client,
        connector,
        "crm",
        status="expired",
        reason="refresh_rejected",
        message=f"invalid_grant for {material}",
        checkedAt="2026-09-30T11:00:00Z",
    )
    assert expired.status_code == 200, expired.text
    body = expired.json()
    assert (body["status"], body["statusReason"], body["version"]) == (
        "expired",
        "refresh_rejected",
        2,
    )
    assert body["statusMessage"] == "invalid_grant for [redacted]"
    assert material not in expired.text

    recorded = await events(client, admin_key, "connection.status_changed")
    assert len(recorded) == 1
    assert recorded[0]["entityId"] == created["id"]
    assert recorded[0]["payload"] == {
        "key": "crm",
        "type": "crm",
        "from": "active",
        "to": "expired",
        "reason": "refresh_rejected",
        "connectedBy": admin_id,
    }
    assert material not in str(recorded)

    # A repeat moves lastCheckedAt only; access comes back only by a new authorization.
    again = await report(
        client,
        connector,
        "crm",
        status="expired",
        reason="refresh_rejected",
        checkedAt="2026-09-30T12:00:00Z",
    )
    assert again.status_code == 200, again.text
    assert (again.json()["version"], again.json()["lastCheckedAt"]) == (2, "2026-09-30T12:00:00Z")
    back = await report(client, connector, "crm", status="active", checkedAt="2026-09-30T13:00:00Z")
    assert back.status_code == 409
    assert back.json()["error"]["code"] == "connection_status_conflict"
    assert len(await events(client, admin_key, "connection.status_changed")) == 1


async def test_a_revoked_connection_takes_no_report(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    _admin_key, created, connector = await _connector(client, sync_engine, ["crm"])
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE connections SET status = 'revoked' WHERE id = :id"), {"id": created["id"]}
        )
    for status in ("active", "expired"):
        response = await report(
            client, connector, "crm", status=status, reason="gone", checkedAt="2026-09-30T10:00:00Z"
        )
        assert response.status_code == 409, status
        assert response.json()["error"]["code"] == "connection_status_conflict"


async def test_only_an_agent_that_names_the_connection_reports_it(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, _created, connector = await _connector(client, sync_engine, ["crm-other"])
    body = {"status": "active", "checkedAt": "2026-09-30T10:00:00Z"}

    # A key outside the list: 403 whether it exists or not.
    for key in ("crm", "nothing"):
        response = await report(client, connector, key, **body)
        assert response.status_code == 403, key
        assert response.json()["error"]["code"] == "connection_not_assigned"
    # Named but missing: 404.
    missing = await report(client, connector, "crm-other", **body)
    assert missing.status_code == 404
    # A principal that is not a registry agent — even the administrator.
    _, plain = await create_agent_with_key(
        client, admin_key, name="plain", permissions=["connections.status.write"]
    )
    for key in (plain, admin_key):
        response = await report(client, key, "crm", **body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "connection_not_assigned"


# --- storage --------------------------------------------------------------------


async def test_the_table_keeps_its_invariants_against_raw_sql(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await publish(client, admin_key)
    created = (await create(client, admin_key)).json()

    for statement in (
        "UPDATE connections SET secret_ref = 'kv/data/x' WHERE id = :id",
        "UPDATE connections SET auth = 'token' WHERE id = :id",
        "UPDATE connections SET auth = 'basic', secret_ref = 'x' WHERE id = :id",
        "UPDATE connections SET status = 'lost' WHERE id = :id",
        "UPDATE connections SET settings = '[]'::jsonb WHERE id = :id",
        "UPDATE connections SET type_version = 9 WHERE id = :id",
        "UPDATE connections SET version = 0 WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), sync_engine.begin() as conn:
            conn.execute(text(statement), {"id": created["id"]})


async def test_parallel_reports_of_one_expiry_record_one_transition(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, created, connector = await _connector(client, sync_engine, ["crm"])
    _activate(sync_engine, created["id"], created["createdBy"])
    responses = await asyncio.gather(
        *(
            report(
                client,
                connector,
                "crm",
                status="expired",
                reason="refresh_rejected",
                checkedAt="2026-09-30T10:00:00Z",
            )
            for _ in range(4)
        )
    )
    assert [r.status_code for r in responses] == [200] * 4
    assert {r.json()["version"] for r in responses} == {2}
    assert len(await events(client, admin_key, "connection.status_changed")) == 1
