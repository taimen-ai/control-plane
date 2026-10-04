"""``PATCH /principals/{id}``: the display name and the profile (CP-ADR-0082 §1).

The order of checks is the contract (§1.4): permission, ``If-Match``, the
principal, credential-shaped material, the shape, the agent registry, the
version. Neither refusal of the body carries the value that was refused.
"""

import json
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, do_bootstrap, make_tenant_directly
from tests.integration.test_agent_registry import _link, _publish, _tenant, coder_spec

# Shaped like a provider token; built at run time so no scanner flags the file.
SECRET = "sk-" + "A1b2C3d4" * 4


async def patch(
    client: httpx.AsyncClient,
    key: str,
    principal_id: str,
    body: Any,
    *,
    version: int | None = 1,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {**auth(key), **(headers or {})}
    if version is not None:
        sent["If-Match"] = f'"principal-{version}"'
    return await client.patch(
        f"/api/v1/principals/{principal_id}",
        content=body if isinstance(body, bytes) else json.dumps(body),
        headers={"Content-Type": "application/json", **sent},
    )


async def principal_events(
    client: httpx.AsyncClient, key: str, principal_id: str
) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events",
        params={"types": "principal.updated", "entityId": principal_id},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def human(client: httpx.AsyncClient, key: str, name: str = "Ann") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/principals", json={"kind": "human", "displayName": name}, headers=auth(key)
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def errors_of(response: httpx.Response, code: str) -> list[dict[str, Any]]:
    error = response.json()["error"]
    assert error["code"] == code, response.text
    found: list[dict[str, Any]] = error["details"]["errors"]
    return found


# --- the change itself -------------------------------------------------------


async def test_new_and_existing_principals_start_with_an_empty_profile_at_version_1(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    assert (ann["profile"], ann["version"]) == ({}, 1)

    read = await client.get(f"/api/v1/principals/{ann['id']}", headers=auth(admin_key))
    assert read.status_code == 200
    assert read.headers["ETag"] == '"principal-1"'
    assert (read.json()["profile"], read.json()["version"]) == ({}, 1)


async def test_name_and_profile_change_with_version_etag_and_event_of_field_names(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    profile = {"jobTitle": "Accountant", "email": "ann@example.com", "phone": "+7 900 000-00-00"}

    response = await patch(
        client, admin_key, ann["id"], {"displayName": "  Ann Smith ", "profile": profile}
    )
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"principal-2"'
    body = response.json()
    assert (body["displayName"], body["profile"], body["version"]) == ("Ann Smith", profile, 2)

    read = await client.get(f"/api/v1/principals/{ann['id']}", headers=auth(admin_key))
    assert read.headers["ETag"] == '"principal-2"'
    assert read.json()["profile"] == profile

    [event] = await principal_events(client, admin_key, ann["id"])
    assert event["payload"] == {
        "principalId": ann["id"],
        "version": 2,
        "changes": ["displayName", "profile.email", "profile.jobTitle", "profile.phone"],
    }
    assert event["schemaVersion"] == 1
    # Names only: no value of the change is in the journal.
    for value in ("Ann Smith", "ann@example.com", "Accountant", "+7 900"):
        assert value not in json.dumps(event)


async def test_profile_is_replaced_whole_and_name_alone_keeps_it(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    first = await patch(
        client, admin_key, ann["id"], {"profile": {"jobTitle": "Clerk", "note": "Part-time"}}
    )
    assert first.status_code == 200, first.text

    # Only the name: the profile stays.
    renamed = await patch(client, admin_key, ann["id"], {"displayName": "Anna"}, version=2)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["profile"] == {"jobTitle": "Clerk", "note": "Part-time"}

    # A profile without "note" drops it.
    replaced = await patch(
        client, admin_key, ann["id"], {"profile": {"jobTitle": "Lead"}}, version=3
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["profile"] == {"jobTitle": "Lead"}

    cleared = await patch(client, admin_key, ann["id"], {"profile": {}}, version=4)
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["profile"], cleared.json()["version"]) == ({}, 5)

    changes = [
        e["payload"]["changes"] for e in await principal_events(client, admin_key, ann["id"])
    ]
    assert sorted(changes) == sorted(
        [
            ["profile.jobTitle", "profile.note"],
            ["displayName"],
            ["profile.jobTitle", "profile.note"],
            ["profile.jobTitle"],
        ]
    )


async def test_a_body_that_changes_nothing_keeps_the_version_and_writes_no_event(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    for body in ({}, {"displayName": "Ann"}, {"profile": {}}):
        response = await patch(client, admin_key, ann["id"], body)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        assert response.headers["ETag"] == '"principal-1"'
    assert await principal_events(client, admin_key, ann["id"]) == []


async def test_a_disabled_human_and_a_plain_agent_are_edited(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    disabled = await client.post(f"/api/v1/principals/{ann['id']}:disable", headers=auth(admin_key))
    assert disabled.status_code == 200, disabled.text
    response = await patch(client, admin_key, ann["id"], {"profile": {"note": "Left in 2026"}})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"

    agent, _ = await create_agent_with_key(client, admin_key)
    response = await patch(client, admin_key, agent["id"], {"displayName": "Script"})
    assert response.status_code == 200, response.text


async def test_a_caller_edits_its_own_profile(client: httpx.AsyncClient) -> None:
    bootstrap = await do_bootstrap(client)
    admin_key = bootstrap["apiKey"]["key"]
    me = bootstrap["adminPrincipal"]
    response = await patch(client, admin_key, me["id"], {"profile": {"jobTitle": "Owner"}})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2


# --- refusals, in the order of §1.4 --------------------------------------------


async def test_without_principals_write_refused_before_if_match(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["principals.read"]
    )
    for version in (1, None):
        response = await patch(client, reader_key, ann["id"], {"displayName": "X"}, version=version)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"


async def test_missing_if_match_is_428_and_a_bad_one_400(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    missing = await patch(client, admin_key, ann["id"], {"displayName": "X"}, version=None)
    assert missing.status_code == 428, missing.text
    assert missing.json()["error"]["code"] == "if_match_required"

    bad = await patch(
        client,
        admin_key,
        ann["id"],
        {"displayName": "X"},
        version=None,
        headers={"If-Match": '"task-1"'},
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "invalid_if_match"

    # The If-Match check comes before the body: a bad body still gets 428.
    assert (
        await patch(client, admin_key, ann["id"], {"displayName": None}, version=None)
    ).status_code == 428


async def test_unknown_and_foreign_principals_are_404(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, other_key = make_tenant_directly(sync_engine, "other")
    foreign = await human(client, other_key, "Bob")

    for principal_id in (str(uuid.uuid4()), foreign["id"]):
        response = await patch(client, admin_key, principal_id, {"displayName": "X"})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["details"] == {"principalId": principal_id}


async def test_version_conflict_names_the_current_version(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    assert (await patch(client, admin_key, ann["id"], {"displayName": "B"})).status_code == 200

    stale = await patch(client, admin_key, ann["id"], {"displayName": "C"}, version=1)
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "version_conflict"
    assert (error["details"]["currentVersion"], error["details"]["expectedVersion"]) == (2, 1)
    read = await client.get(f"/api/v1/principals/{ann['id']}", headers=auth(admin_key))
    assert read.json()["displayName"] == "B"


async def test_secret_material_is_refused_with_paths_and_without_the_value(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    body = {
        "displayName": f"Ann {SECRET}",
        "profile": {"note": f"token: {SECRET}", "jobTitle": "Clerk"},
    }
    response = await patch(client, admin_key, ann["id"], body)
    assert response.status_code == 422, response.text
    errors = errors_of(response, "secret_material_rejected")
    assert [e["path"] for e in errors] == ["/displayName", "/profile/note"]
    assert all(set(e) <= {"path", "match", "field"} for e in errors)
    assert SECRET not in response.text

    # A secret in a member name: the path of the object it stands in.
    response = await patch(client, admin_key, ann["id"], {"profile": {SECRET: "x"}, SECRET: 1})
    assert [e["path"] for e in errors_of(response, "secret_material_rejected")] == [
        "/",
        "/profile",
    ]
    assert SECRET not in response.text

    # Before the shape: a broken other field does not turn it into a schema error.
    response = await patch(
        client, admin_key, ann["id"], {"displayName": None, "profile": {"note": SECRET}}
    )
    assert [e["path"] for e in errors_of(response, "secret_material_rejected")] == ["/profile/note"]

    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT display_name, profile::text, version FROM principals WHERE id = :id"),
            {"id": ann["id"]},
        ).one()
        journal = conn.execute(text("SELECT payload::text FROM events")).scalars().all()
    assert stored == ("Ann", "{}", 1)
    assert not any(SECRET in payload for payload in journal)


async def test_shape_errors_are_json_pointers_all_at_once_without_values(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    body = {
        "displayName": "",
        "profile": {
            "email": "not-an-address",
            "phone": "9" * 51,
            "jobTitle": None,
            "age": 40,
        },
        "metadata": {"x": 1},
    }
    response = await patch(client, admin_key, ann["id"], body)
    assert response.status_code == 422, response.text
    errors = errors_of(response, "validation_error")
    assert {(e["path"], e["code"]) for e in errors} == {
        ("/displayName", "minLength"),
        ("/profile/email", "format"),
        ("/profile/phone", "maxLength"),
        ("/profile/jobTitle", "type"),
        ("/profile/age", "additionalProperties"),
        ("/metadata", "additionalProperties"),
    }
    assert all(set(e) == {"path", "code", "message"} for e in errors)
    assert "not-an-address" not in response.text
    assert "9" * 51 not in response.text


async def test_null_blank_and_non_object_bodies_are_validation_errors_not_400(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    cases: list[tuple[Any, tuple[str, str]]] = [
        ({"displayName": None}, ("/displayName", "type")),
        ({"profile": None}, ("/profile", "type")),
        ({"displayName": "   "}, ("/displayName", "minLength")),
        ({"displayName": "x" * 201}, ("/displayName", "maxLength")),
        ({"displayName": 5}, ("/displayName", "type")),
        ({"profile": ["a"]}, ("/profile", "type")),
        ({"profile": {"email": "a@b.c" + "c" * 250}}, ("/profile/email", "maxLength")),
        ([], ("/", "type")),
        ("text", ("/", "type")),
        (None, ("/", "type")),
        (b"{not json", ("/", "type")),
        (b"", ("/", "type")),
        (b"[" * 100_000, ("/", "type")),
    ]
    for body, expected in cases:
        response = await patch(client, admin_key, ann["id"], body)
        assert response.status_code == 422, (body, response.text)
        errors = errors_of(response, "validation_error")
        assert [(e["path"], e["code"]) for e in errors] == [expected], body


async def test_a_registry_agent_is_refused_and_its_publication_moves_the_version(
    client: httpx.AsyncClient,
) -> None:
    admin_key, workspace = await _tenant(client)
    assert (await _publish(client, admin_key, coder_spec(workspace["id"]))).status_code == 201
    principal_id = (await _link(client, admin_key)).json()["principalId"]

    refused = await patch(client, admin_key, principal_id, {"displayName": "Renamed"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "principal_managed_by_registry"
    assert refused.json()["error"]["details"]["agent"] == "coder"

    # The registry is checked before the version: a stale If-Match still names it.
    stale = await patch(client, admin_key, principal_id, {"displayName": "R"}, version=7)
    assert stale.json()["error"]["code"] == "principal_managed_by_registry"

    republished = await _publish(
        client, admin_key, coder_spec(workspace["id"], displayName="Coder v2")
    )
    assert republished.status_code in (200, 201), republished.text
    read = await client.get(f"/api/v1/principals/{principal_id}", headers=auth(admin_key))
    assert (read.json()["displayName"], read.json()["version"]) == ("Coder v2", 2)


async def test_idempotent_replay_returns_the_saved_answer_and_etag(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ann = await human(client, admin_key)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await patch(client, admin_key, ann["id"], {"displayName": "B"}, headers=key)
    again = await patch(client, admin_key, ann["id"], {"displayName": "B"}, headers=key)
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert again.headers["Idempotency-Replayed"] == "true"
    assert again.headers["ETag"] == '"principal-2"'

    reused = await patch(client, admin_key, ann["id"], {"displayName": "C"}, headers=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "idempotency_key_reused"
    assert len(await principal_events(client, admin_key, ann["id"])) == 1
