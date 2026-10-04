"""Settings of a package for its agent and its skill (CP-ADR-0081 §8, G006).

- ``GET /agents/me`` carries ``packageSettings`` of the package that installed
  the agent: the effective values, read now; an agent of another package sees
  the settings of that one, an agent of a package without settings and an
  agent made by hand see ``null``; ``GET /agents/{key}`` carries none;
- the claim of a skill invocation carries ``settings`` of the skill's
  package, ``null`` for a skill made by hand; a retry of the attempt gets the
  values of its own claim, a replay of the claim the values it answered.
"""

from typing import Any

import httpx

from tests.helpers import auth, create_agent_with_key, create_role
from tests.integration.test_agent_registry import _api_key, _link, _publish, _tenant, coder_spec
from tests.integration.test_package_settings import (
    PACKAGE,
    _put,
    declared,
    install,
    package_files,
)

OTHER = "other-settings"
PLAIN = "plain-pack"
DEFAULTS: dict[str, Any] = {"limit": 1000, "days": 2, "note": "", "window": {"start": 9, "end": 18}}
EXECUTOR_PERMISSIONS = ["skills.execute", "sessions.open"]
CALLER_PERMISSIONS = ["skills.invoke", "tasks.read"]
ENTRYPOINT = "tests.skill_stubs.arith:run"


async def _record(
    client: httpx.AsyncClient, key: str, package: str, objects: list[tuple[str, str]]
) -> None:
    response = await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": package, "version": "1.0.0"},
            "installHash": "sha256:" + "b" * 64,
            "objects": [{"kind": kind, "key": name} for kind, name in objects],
        },
        headers=auth(key),
    )
    assert response.status_code == 200, response.text


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    """Two packages with settings, saved once each; ``PLAIN`` declares none."""
    admin, workspace = await _tenant(client)
    role = (await create_role(client, admin, "approver"))["id"]
    await install(client, admin, package_files(settings=declared()))
    await install(client, admin, package_files(key=OTHER, settings=declared(layout=False)))
    saved = await _put(client, admin, {"owner": role, "limit": 5000}, 0)
    assert saved.status_code == 200, saved.text
    saved = await _put(client, admin, {"owner": role, "days": 7}, 0, package=OTHER)
    assert saved.status_code == 200, saved.text
    return {"admin": admin, "workspace": workspace["id"], "role": role}


async def _agent_key(client: httpx.AsyncClient, world: dict[str, Any], agent: str) -> str:
    published = await _publish(client, world["admin"], coder_spec(world["workspace"]), agent)
    assert published.status_code == 201, published.text
    linked = await _link(client, world["admin"], agent)
    assert linked.status_code == 200, linked.text
    return await _api_key(client, world["admin"], linked.json()["principalId"])


async def _me(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.get("/api/v1/agents/me", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- GET /agents/me ---------------------------------------------------------------------------


async def test_an_agent_sees_the_settings_of_its_own_package_only(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    keys = {agent: await _agent_key(client, world, agent) for agent in ("coder", "other", "plain")}
    keys["manual"] = await _agent_key(client, world, "manual")
    await _record(client, world["admin"], PACKAGE, [("Agent", "coder")])
    await _record(client, world["admin"], OTHER, [("Agent", "other")])
    await _record(client, world["admin"], PLAIN, [("Agent", "plain")])

    mine = await _me(client, keys["coder"])
    assert mine["package"]["key"] == PACKAGE
    assert mine["packageSettings"] == {
        "package": PACKAGE,
        "version": 1,
        "schemaRevision": 1,
        "values": {**DEFAULTS, "limit": 5000, "owner": world["role"]},
    }

    other = await _me(client, keys["other"])
    assert other["packageSettings"] == {
        "package": OTHER,
        "version": 1,
        "schemaRevision": 1,
        "values": {**DEFAULTS, "days": 7, "owner": world["role"]},
    }

    # A package without settings and an agent made by hand: null, not absent.
    plain = await _me(client, keys["plain"])
    assert plain["package"]["key"] == PLAIN
    assert plain["packageSettings"] is None
    manual = await _me(client, keys["manual"])
    assert manual["package"] is None
    assert manual["packageSettings"] is None

    # Another agent's card does not carry the settings of its package.
    card = await client.get("/api/v1/agents/coder", headers=auth(world["admin"]))
    assert card.status_code == 200, card.text
    assert "packageSettings" not in card.json()


async def test_the_agent_reads_the_values_saved_now(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    key = await _agent_key(client, world, "coder")
    await _record(client, world["admin"], PACKAGE, [("Agent", "coder")])
    assert (await _me(client, key))["packageSettings"]["version"] == 1

    # Back to the defaults: version 2, the agent sees it at once.
    saved = await _put(client, world["admin"], {"owner": world["role"]}, 1)
    assert saved.status_code == 200, saved.text
    settings = (await _me(client, key))["packageSettings"]
    assert settings["version"] == 2
    assert settings["values"] == {**DEFAULTS, "owner": world["role"]}


async def test_a_package_never_saved_gives_its_defaults_at_version_0(
    client: httpx.AsyncClient,
) -> None:
    admin, workspace = await _tenant(client)
    world = {"admin": admin, "workspace": workspace["id"]}
    await install(client, admin, package_files(settings=declared()))
    key = await _agent_key(client, world, "coder")
    await _record(client, admin, PACKAGE, [("Agent", "coder")])
    assert (await _me(client, key))["packageSettings"] == {
        "package": PACKAGE,
        "version": 0,
        "schemaRevision": 1,
        "values": DEFAULTS,
    }


async def test_a_package_that_stops_declaring_settings_gives_null(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    key = await _agent_key(client, world, "coder")
    await _record(client, world["admin"], PACKAGE, [("Agent", "coder")])
    await install(client, world["admin"], package_files(version="1.1.0"))
    assert (await _me(client, key))["packageSettings"] is None


# --- the claim of a skill invocation --------------------------------------------------------


async def _skill(
    client: httpx.AsyncClient, admin: str, name: str, *, attempts: int = 1
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": name,
            "version": "1.0.0",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": {
                "inputs": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "outputs": {"type": "object"},
                "retryPolicy": {"maxAttempts": attempts, "backoffSeconds": 0},
                "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
            },
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _actors(client: httpx.AsyncClient, admin: str) -> tuple[str, str]:
    _, caller = await create_agent_with_key(
        client, admin, name="caller", permissions=CALLER_PERMISSIONS
    )
    _, executor = await create_agent_with_key(
        client, admin, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    return caller, executor


async def _invoke(client: httpx.AsyncClient, caller: str, ref: str) -> str:
    response = await client.post(
        f"/api/v1/skills/{ref}:invoke", json={"inputs": {"n": 2}}, headers=auth(caller)
    )
    assert response.status_code == 201, response.text
    invocation: str = response.json()["id"]
    return invocation


async def _claim(
    client: httpx.AsyncClient, executor: str, invocation: str, *, idempotency: str | None = None
) -> dict[str, Any]:
    headers = auth(executor)
    if idempotency is not None:
        headers["Idempotency-Key"] = idempotency
    response = await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": ["local"],
            "localEntrypoints": [ENTRYPOINT],
            "invocationId": invocation,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_skill_gets_the_settings_of_its_package(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    admin = world["admin"]
    caller, executor = await _actors(client, admin)
    await _skill(client, admin, "sample.check")
    await _skill(client, admin, "other.check")
    await _skill(client, admin, "manual.check")
    await _record(client, admin, PACKAGE, [("Skill", "sample.check")])
    await _record(client, admin, OTHER, [("Skill", "other.check")])

    claimed = await _claim(client, executor, await _invoke(client, caller, "sample.check@1.0.0"))
    assert claimed["skill"]["name"] == "sample.check"
    assert claimed["settings"] == {
        "package": PACKAGE,
        "version": 1,
        "schemaRevision": 1,
        "values": {**DEFAULTS, "limit": 5000, "owner": world["role"]},
    }

    claimed = await _claim(client, executor, await _invoke(client, caller, "other.check@1.0.0"))
    assert claimed["settings"]["package"] == OTHER
    assert claimed["settings"]["values"]["days"] == 7
    assert claimed["settings"]["values"]["limit"] == 1000

    claimed = await _claim(client, executor, await _invoke(client, caller, "manual.check@1.0.0"))
    assert claimed["settings"] is None


async def test_a_retry_of_the_attempt_gets_the_values_of_its_own_claim(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    admin = world["admin"]
    caller, executor = await _actors(client, admin)
    await _skill(client, admin, "sample.check", attempts=2)
    await _record(client, admin, PACKAGE, [("Skill", "sample.check")])
    invocation = await _invoke(client, caller, "sample.check@1.0.0")

    first = await _claim(client, executor, invocation, idempotency="claim-1")
    assert first["settings"]["version"] == 1
    saved = await _put(client, admin, {"owner": world["role"], "limit": 7}, 1)
    assert saved.status_code == 200, saved.text

    # A replay of the claim answers what it answered then.
    replayed = await _claim(client, executor, invocation, idempotency="claim-1")
    assert replayed == first

    failed = await client.post(
        f"/api/v1/skill-invocations/{invocation}:fail",
        json={
            "fencingToken": first["invocation"]["fencingToken"],
            "error": {"code": "upstream_busy", "retryable": True},
        },
        headers=auth(executor),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "pending"

    second = await _claim(client, executor, invocation)
    assert second["invocation"]["attempt"] == 2
    assert second["settings"]["version"] == 2
    assert second["settings"]["values"]["limit"] == 7
