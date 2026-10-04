"""Visibility of a binding by workspace (CP-ADR-0082 §2-3, T003).

A human whose binding is in ``members`` mode sees the work of the workspaces
of their membership and below; everything else answers exactly as if it did
not exist. ``tenant`` — the default and every existing binding — changes
nothing. The mode is the principal's across all its entries and is read per
request, so a change takes effect on the next request without the cache TTL.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from platform_auth.testing import SigningKey
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.infrastructure.auth.iam import SCOPE_READ, SCOPE_WRITE
from control_plane.main import create_app
from tests.helpers import BOOTSTRAP_TOKEN, auth, create_workspace, do_bootstrap
from tests.integration.test_iam_enforcement import AUDIENCE, ISSUER, enable_iam
from tests.integration.test_process_instances import GOAL, _publish, _setup
from tests.integration.test_work_rules_m13 import DRIFT_RULE

PERMISSIONS = [
    "tasks.read",
    "tasks.write",
    "goals.read",
    "rules.read",
    "processes.read",
    "workspaces.read",
    "events.read",
]


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def iam_settings(migrated_database: str) -> Settings:
    return Settings(
        database_url=migrated_database,
        bootstrap_token=BOOTSTRAP_TOKEN,
        log_level="WARNING",
        session_ttl_seconds=60,
        claim_ttl_seconds=60,
        ws_poll_interval_seconds=0.5,
    )


@pytest.fixture
async def iam_app(iam_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(iam_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def iam_client(iam_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=iam_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


class World:
    """Company -> Dept -> Team, and an unrelated root Other; a human in Dept."""

    def __init__(self, client: httpx.AsyncClient, key: SigningKey, admin: str) -> None:
        self.client = client
        self.signing_key = key
        self.admin = admin
        self.ws: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.human = ""
        self.identity: dict[str, str] = {}

    def token(self, identity: dict[str, str] | None = None) -> str:
        who = identity or self.identity
        return self.signing_key.issue(
            subject=uuid.UUID(who["iamPrincipalId"]),
            tenant_id=uuid.UUID(who["iamTenantId"]),
            scopes=[SCOPE_READ, SCOPE_WRITE],
            ttl_seconds=3600,
        )

    async def bind(
        self,
        principal_id: str | None = None,
        identity: dict[str, str] | None = None,
        **extra: Any,
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/principals/{principal_id or self.human}/iam-bindings",
            json={**(identity or self.identity), "permissions": PERMISSIONS, **extra},
            headers=auth(self.admin),
        )

    async def add_member(self, workspace: str, principal_id: str | None = None) -> None:
        response = await self.client.post(
            f"/api/v1/workspaces/{self.ws[workspace]}/members",
            json={"principalId": principal_id or self.human},
            headers=auth(self.admin),
        )
        assert response.status_code == 201, response.text

    async def get(self, path: str, token: str | None = None, **params: Any) -> httpx.Response:
        return await self.client.get(path, params=params, headers=auth(token or self.token()))


def new_identity() -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
    }


async def make_world(
    app: FastAPI,
    client: httpx.AsyncClient,
    signing_key: SigningKey,
    *,
    admin: str | None = None,
    ttl_seconds: float = 0.0,
) -> World:
    enable_iam(app, signing_key, ttl_seconds=ttl_seconds)
    admin = admin or (await do_bootstrap(client))["apiKey"]["key"]
    world = World(client, signing_key, admin)
    company = await create_workspace(client, admin, "company")
    dept = await create_workspace(client, admin, "dept", parent_id=company["id"])
    team = await create_workspace(client, admin, "team", parent_id=dept["id"])
    other = await create_workspace(client, admin, "other")
    world.ws = {"company": company["id"], "dept": dept["id"], "team": team["id"]}
    world.ws["other"] = other["id"]
    for name in ("company", "dept", "team", "other"):
        created = await client.post(
            "/api/v1/tasks",
            json={"title": f"work of {name}", "workspaceId": world.ws[name]},
            headers=auth(admin),
        )
        assert created.status_code == 201, created.text
        world.tasks[name] = created.json()
    loose = await client.post("/api/v1/tasks", json={"title": "loose"}, headers=auth(admin))
    assert loose.status_code == 201, loose.text
    world.tasks["none"] = loose.json()

    human = await client.post(
        "/api/v1/principals", json={"kind": "human", "displayName": "Ann"}, headers=auth(admin)
    )
    assert human.status_code == 201, human.text
    world.human = human.json()["id"]
    world.identity = new_identity()
    await world.add_member("dept")
    return world


async def listed(world: World, token: str | None = None) -> set[str]:
    response = await world.get("/api/v1/tasks", token, limit=200)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


def ids(world: World, *names: str) -> set[str]:
    return {world.tasks[name]["id"] for name in names}


def same_as_missing(invisible: httpx.Response, missing: httpx.Response, refs: tuple[str, str]):
    """The two 404 bodies are equal once the ids asked for are named alike."""
    assert invisible.status_code == missing.status_code == 404, (invisible.text, missing.text)

    def normalized(response: httpx.Response, ref: str) -> dict[str, Any]:
        error = dict(response.json()["error"])
        error.pop("requestId")
        return {k: str(v).replace(ref, "<ref>") for k, v in error.items()}

    assert normalized(invisible, refs[0]) == normalized(missing, refs[1])


# --- tenant ------------------------------------------------------------------


async def test_a_binding_without_visibility_is_tenant_and_sees_everything(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    bound = await world.bind()
    assert bound.status_code == 201, bound.text
    assert bound.json()["visibility"] == "tenant"

    assert await listed(world) == set().union(*(ids(world, n) for n in world.tasks))
    other = await world.get(f"/api/v1/tasks/{world.tasks['other']['publicId']}")
    assert other.status_code == 200, other.text


# --- members -----------------------------------------------------------------


async def test_members_sees_the_subtree_of_its_membership_only(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    bound = await world.bind(visibility="members")
    assert bound.status_code == 201, bound.text
    assert bound.json()["visibility"] == "members"

    # Dept and its descendant Team; not the parent, not a sibling root, not
    # work outside any workspace.
    assert await listed(world) == ids(world, "dept", "team")
    for name in ("dept", "team"):
        response = await world.get(f"/api/v1/tasks/{world.tasks[name]['id']}")
        assert response.status_code == 200, response.text
    for name in ("company", "other", "none"):
        ref = world.tasks[name]["publicId"]
        response = await world.get(f"/api/v1/tasks/{ref}")
        assert response.status_code == 404, response.text


async def test_membership_in_a_parent_and_its_child_gives_the_subtree_once(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.add_member("team")
    await world.bind(visibility="members")
    assert await listed(world) == ids(world, "dept", "team")


async def test_a_member_of_nothing_sees_no_work(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    removed = await iam_client.post(
        f"/api/v1/workspaces/{world.ws['dept']}/members/{world.human}:remove",
        headers=auth(world.admin),
    )
    assert removed.status_code == 204, removed.text
    await world.bind(visibility="members")
    assert await listed(world) == set()


async def test_invisible_work_answers_exactly_as_missing_work(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    other = world.tasks["other"]
    missing = "TASK-999999"

    for suffix in ("", "/comments", "/transitions", "/relations", "/verifications"):
        same_as_missing(
            await world.get(f"/api/v1/tasks/{other['publicId']}{suffix}"),
            await world.get(f"/api/v1/tasks/{missing}{suffix}"),
            (other["publicId"], missing),
        )
    # A write under the work is refused the same way as a read (FR-007).
    token = world.token()
    for path, body in (
        ("/api/v1/tasks/{}/comments", {"body": "hello"}),
        ("/api/v1/tasks/{}/relations", {"type": "blocks", "toTask": world.tasks["dept"]["id"]}),
    ):
        same_as_missing(
            await iam_client.post(path.format(other["id"]), json=body, headers=auth(token)),
            await iam_client.post(path.format(missing), json=body, headers=auth(token)),
            (other["id"], missing),
        )
    patched = await iam_client.patch(
        f"/api/v1/tasks/{other['id']}",
        json={"title": "mine now"},
        headers={**auth(token), "If-Match": f'"task-{other["version"]}"'},
    )
    assert patched.status_code == 404, patched.text
    unchanged = await iam_client.get(f"/api/v1/tasks/{other['id']}", headers=auth(world.admin))
    assert unchanged.json()["title"] == "work of other"


async def test_own_work_in_another_workspace_is_not_an_exception(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """Strictly the set (CP-ADR-0082 §3.5): no owner/assignee/author exception."""
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind()
    mine = await iam_client.post(
        "/api/v1/tasks",
        json={"title": "mine", "workspaceId": world.ws["other"]},
        headers=auth(world.token()),
    )
    assert mine.status_code == 201, mine.text
    await world.bind(visibility="members")
    assert mine.json()["id"] not in await listed(world)


async def test_a_workspace_outside_the_set_is_a_missing_workspace(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    token = world.token()
    missing = str(uuid.uuid4())

    created = [
        await iam_client.post(
            "/api/v1/tasks", json={"title": "t", "workspaceId": ws}, headers=auth(token)
        )
        for ws in (world.ws["other"], missing)
    ]
    same_as_missing(created[0], created[1], (world.ws["other"], missing))
    assert created[0].json()["error"]["message"] == "Workspace not found"

    inside = await iam_client.post(
        "/api/v1/tasks", json={"title": "t", "workspaceId": world.ws["team"]}, headers=auth(token)
    )
    assert inside.status_code == 201, inside.text


async def test_a_missing_permission_stays_403_whatever_the_visibility(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    bound = await iam_client.post(
        f"/api/v1/principals/{world.human}/iam-bindings",
        json={**world.identity, "permissions": ["tasks.read"], "visibility": "members"},
        headers=auth(world.admin),
    )
    assert bound.status_code == 201, bound.text
    response = await iam_client.post(
        "/api/v1/tasks",
        json={"title": "t", "workspaceId": world.ws["other"]},
        headers=auth(world.token()),
    )
    assert response.status_code == 403, response.text


async def test_goal_and_rule_of_an_invisible_workspace_answer_as_missing(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    goal = await iam_client.post(
        "/api/v1/goals",
        json={"title": "elsewhere", "workspaceId": world.ws["other"]},
        headers=auth(world.admin),
    )
    assert goal.status_code == 201, goal.text
    rule = await iam_client.post(
        "/api/v1/rules",
        json={**DRIFT_RULE, "workspaceId": world.ws["other"]},
        headers=auth(world.admin),
    )
    assert rule.status_code == 201, rule.text
    missing = str(uuid.uuid4())

    for kind, row in (("goals", goal.json()), ("rules", rule.json())):
        same_as_missing(
            await world.get(f"/api/v1/{kind}/{row['id']}"),
            await world.get(f"/api/v1/{kind}/{missing}"),
            (row["id"], missing),
        )
        listing = await world.get(f"/api/v1/{kind}", limit=200)
        assert listing.status_code == 200, listing.text
        assert row["id"] not in {item["id"] for item in listing.json()["items"]}


async def test_a_process_instance_of_an_invisible_workspace_answers_as_missing(
    iam_app: FastAPI,
    iam_client: httpx.AsyncClient,
    signing_key: SigningKey,
    sync_engine: Engine,
) -> None:
    setup = await _setup(iam_client)
    world = await make_world(iam_app, iam_client, signing_key, admin=setup["key"])
    await world.bind(visibility="members")
    await _publish(iam_client, world.admin, "sample-goal", GOAL)
    started = await iam_client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-1", "data": {"open": 2}},
        headers=auth(world.admin),
    )
    assert started.status_code == 201, started.text
    instance = started.json()["id"]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE process_instances SET workspace_id = :ws WHERE id = :id"),
            {"ws": world.ws["other"], "id": instance},
        )
    missing = str(uuid.uuid4())
    same_as_missing(
        await world.get(f"/api/v1/process-instances/{instance}"),
        await world.get(f"/api/v1/process-instances/{missing}"),
        (instance, missing),
    )
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE process_instances SET workspace_id = :ws WHERE id = :id"),
            {"ws": world.ws["team"], "id": instance},
        )
    assert (await world.get(f"/api/v1/process-instances/{instance}")).status_code == 200


# --- the mode is the principal's, per request ----------------------------------


async def test_upsert_changes_rights_and_visibility_for_the_next_request(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """SC-003: the same token, a warm binding cache, the very next request."""
    world = await make_world(iam_app, iam_client, signing_key, ttl_seconds=600.0)
    await world.bind()
    token = world.token()
    assert world.tasks["other"]["id"] in await listed(world, token)

    narrowed = await iam_client.post(
        f"/api/v1/principals/{world.human}/iam-bindings",
        json={**world.identity, "permissions": ["workspaces.read"], "visibility": "members"},
        headers=auth(world.admin),
    )
    assert narrowed.status_code == 200, narrowed.text
    # The new rights apply at once: tasks.read is gone ...
    assert (await world.get("/api/v1/tasks", token)).status_code == 403

    await world.bind()  # rights back; visibility absent — stays members
    assert await listed(world, token) == ids(world, "dept", "team")
    await world.bind(visibility="tenant")
    assert world.tasks["other"]["id"] in await listed(world, token)


async def test_upsert_without_visibility_keeps_the_mode(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    again = await world.bind()
    assert again.status_code == 200, again.text
    assert again.json()["visibility"] == "members"
    listed_bindings = await iam_client.get(
        f"/api/v1/principals/{world.human}/iam-bindings", headers=auth(world.admin)
    )
    assert [b["visibility"] for b in listed_bindings.json()["items"]] == ["members"]


async def test_one_members_binding_narrows_every_entry_of_the_principal(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """Another identity and an API key of the same human are narrowed too,
    and the change reaches them without waiting out the cache TTL."""
    world = await make_world(iam_app, iam_client, signing_key, ttl_seconds=600.0)
    second = new_identity()
    await world.bind()
    await world.bind(identity=second)
    key = await iam_client.post(
        f"/api/v1/principals/{world.human}/api-keys",
        json={"permissions": PERMISSIONS},
        headers=auth(world.admin),
    )
    assert key.status_code == 201, key.text
    api_key = key.json()["key"]
    other_token = world.token(second)
    everything = set().union(*(ids(world, n) for n in world.tasks))
    # Warm the cache of the second identity.
    assert await listed(world, other_token) == everything

    await world.bind(visibility="members")
    narrowed = ids(world, "dept", "team")
    assert await listed(world, other_token) == narrowed
    assert await listed(world, api_key) == narrowed

    # A revoked members binding no longer narrows: only active ones count.
    first = (await world.bind()).json()
    revoked = await iam_client.post(
        f"/api/v1/iam-bindings/{first['id']}:revoke", headers=auth(world.admin)
    )
    assert revoked.status_code == 200, revoked.text
    assert await listed(world, other_token) == everything


async def test_a_change_of_membership_takes_effect_on_the_next_request(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key, ttl_seconds=600.0)
    await world.bind(visibility="members")
    token = world.token()
    assert world.tasks["other"]["id"] not in await listed(world, token)
    await world.add_member("other")
    assert world.tasks["other"]["id"] in await listed(world, token)


# --- the upsert contract -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code"),
    [(None, "type"), ("all", "enum"), ("", "enum"), (1, "type"), (["members"], "type")],
)
async def test_a_wrong_visibility_is_a_field_error(
    iam_app: FastAPI,
    iam_client: httpx.AsyncClient,
    signing_key: SigningKey,
    value: Any,
    code: str,
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    response = await world.bind(visibility=value)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [(e["path"], e["code"]) for e in error["details"]["errors"]] == [("/visibility", code)]
    # Nothing was written.
    listed_bindings = await iam_client.get(
        f"/api/v1/principals/{world.human}/iam-bindings", headers=auth(world.admin)
    )
    assert listed_bindings.json()["items"] == []


@pytest.mark.parametrize("kind", ["agent", "service"])
async def test_members_requires_a_human(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey, kind: str
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    principal = await iam_client.post(
        "/api/v1/principals", json={"kind": kind, "displayName": "bot"}, headers=auth(world.admin)
    )
    bot = principal.json()["id"]
    refused = await world.bind(bot, new_identity(), visibility="members")
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "visibility_requires_human"
    assert error["details"]["errors"] == [{"path": "/visibility"}]
    tenant = await world.bind(bot, new_identity(), visibility="tenant")
    assert tenant.status_code == 201, tenant.text


async def test_an_identity_moved_to_an_agent_drops_the_humans_narrowing(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    agent = await iam_client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": "bot"},
        headers=auth(world.admin),
    )
    moved = await world.bind(agent.json()["id"])
    assert moved.status_code == 200, moved.text
    assert moved.json()["visibility"] == "tenant"


async def test_binding_events_carry_the_visibility(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """FR-009: v2 of iam_binding.created/updated; a change of the mode alone
    is an ``iam_binding.updated`` too."""
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind()
    await world.bind(visibility="members")
    response = await iam_client.get(
        "/api/v1/events",
        params={"types": "iam_binding.created,iam_binding.updated", "limit": 50},
        headers=auth(world.admin),
    )
    assert response.status_code == 200, response.text
    seen = [
        (e["type"], e["schemaVersion"], e["payload"]["visibility"])
        for e in response.json()["items"]
        if e["payload"]["principalId"] == world.human
    ]
    assert seen == [("iam_binding.created", 2, "tenant"), ("iam_binding.updated", 2, "members")]


# --- review of the first attempt (CP-ADR-0082 B5-B9) ---------------------------


async def test_work_is_not_moved_into_an_invisible_workspace(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B7: the target is checked before its status or allowed types can tell."""
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    token = world.token()
    hidden = await create_workspace(iam_client, world.admin, "hidden")
    archived = await iam_client.post(
        f"/api/v1/workspaces/{hidden['id']}:archive", headers=auth(world.admin)
    )
    assert archived.status_code == 200, archived.text
    missing = str(uuid.uuid4())
    task = world.tasks["dept"]

    async def move(target: str) -> httpx.Response:
        return await iam_client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"workspaceId": target},
            headers={**auth(token), "If-Match": f'"task-{task["version"]}"'},
        )

    for target in (world.ws["other"], hidden["id"]):
        same_as_missing(await move(target), await move(missing), (target, missing))
    unchanged = await iam_client.get(f"/api/v1/tasks/{task['id']}", headers=auth(world.admin))
    assert unchanged.json()["workspaceId"] == world.ws["dept"]

    moved = await move(world.ws["team"])
    assert moved.status_code == 200, moved.text
    assert moved.json()["workspaceId"] == world.ws["team"]


class SpyMemory:
    """Records what the core asks of Memory; answers with an empty pack."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        self.requests.append({"namespaces": namespaces or [namespace], **request})
        return {"query": "", "sections": [], "sources": [], "trace_id": "ctx-spy"}

    async def typed_context(
        self,
        *,
        namespace: str,
        namespaces: list[str],
        request: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        self.requests.append({"namespaces": namespaces, **request})
        return {"entities": [], "facts": []}

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


async def test_members_does_not_read_memory_of_another_subtree(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B8: with ``workspaceId`` of another subtree — the 404 of a missing
    workspace before Memory is asked; without it — Memory is told the visible
    set, in local mode too."""
    world = await make_world(iam_app, iam_client, signing_key)
    memory = SpyMemory()
    iam_app.state.context_provider = memory
    await world.bind(visibility="members")
    token = world.token()
    missing = str(uuid.uuid4())

    for path, body in (
        ("/api/v1/context", {"query": "q"}),
        ("/api/v1/context/recall", {"anchor": "CP-0019"}),
    ):
        asked = [
            await iam_client.post(path, json={**body, "workspaceId": ws}, headers=auth(token))
            for ws in (world.ws["other"], missing)
        ]
        same_as_missing(asked[0], asked[1], (world.ws["other"], missing))
    assert memory.requests == []

    for path, body in (
        ("/api/v1/context", {"query": "q"}),
        ("/api/v1/context/recall", {"anchor": "CP-0019"}),
        ("/api/v1/context", {"query": "q", "workspaceId": world.ws["team"]}),
    ):
        response = await iam_client.post(path, json=body, headers=auth(token))
        assert response.status_code == 200, response.text
    assert len(memory.requests) == 3
    prefix = iam_app.state.settings.context_namespace_prefix
    tenant = memory.requests[0]["namespaces"][0]
    company_ns = f"{tenant}:ws:{world.ws['company']}"
    for request in memory.requests:
        scopes = set(request["allowedScopes"])
        assert {f"workspace:{world.ws['dept']}", f"workspace:{world.ws['team']}"} <= scopes
        assert not {f"workspace:{world.ws[name]}" for name in ("company", "other")} & scopes, scopes
        assert f"principal:{world.human}" in scopes
        assert tenant.startswith(prefix)
        assert set(request["allowedNamespaces"]) == {tenant, company_ns}
        assert f"{tenant}:ws:{world.ws['other']}" not in request["namespaces"]
    # The focus in a visible workspace reads its tree's namespace.
    assert company_ns in memory.requests[2]["namespaces"]


async def test_tenant_mode_memory_reads_are_unchanged(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    world = await make_world(iam_app, iam_client, signing_key)
    memory = SpyMemory()
    iam_app.state.context_provider = memory
    await world.bind()
    response = await iam_client.post(
        "/api/v1/context",
        json={"query": "q", "workspaceId": world.ws["other"]},
        headers=auth(world.token()),
    )
    assert response.status_code == 200, response.text
    [request] = memory.requests
    assert "allowedNamespaces" not in request
    assert f"workspace:{world.ws['other']}" in request["allowedScopes"]


async def test_members_cannot_make_a_binding_tenant_wide(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B5: no wider than one's own — not for oneself, not for another, not by default."""
    world = await make_world(iam_app, iam_client, signing_key)
    writer = [*PERMISSIONS, "principals.read", "principals.write"]
    await world.bind(visibility="members", permissions=writer)
    token = world.token()
    peer = await iam_client.post(
        "/api/v1/principals",
        json={"kind": "human", "displayName": "Bob"},
        headers=auth(world.admin),
    )
    bob = peer.json()["id"]
    bob_identity = new_identity()
    agent = await iam_client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": "bot"},
        headers=auth(world.admin),
    )

    async def bind(principal: str, identity: dict[str, str], **extra: Any) -> httpx.Response:
        return await iam_client.post(
            f"/api/v1/principals/{principal}/iam-bindings",
            json={**identity, "permissions": PERMISSIONS, **extra},
            headers=auth(token),
        )

    refusals = [
        # Oneself, explicitly; a second identity of oneself by default.
        await bind(world.human, world.identity, visibility="tenant", permissions=writer),
        await bind(world.human, new_identity()),
        # Another human, explicitly and by default; an agent (tenant only).
        await bind(bob, bob_identity, visibility="tenant"),
        await bind(bob, bob_identity),
        await bind(agent.json()["id"], new_identity()),
    ]
    for refused in refusals:
        assert refused.status_code == 403, refused.text
        error = refused.json()["error"]
        assert error["code"] == "visibility_escalation"
        assert error["details"]["errors"] == [{"path": "/visibility"}]
    bindings = await iam_client.get(
        f"/api/v1/principals/{bob}/iam-bindings", headers=auth(world.admin)
    )
    assert bindings.json()["items"] == []
    # The caller is still narrowed: nothing it tried took effect.
    assert await listed(world, token) == ids(world, "dept", "team")

    # In members mode, for another: allowed.
    narrowed = await bind(bob, bob_identity, visibility="members")
    assert narrowed.status_code == 201, narrowed.text


async def test_members_may_change_the_rights_of_an_active_tenant_binding(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B5: left out, ``visibility`` widens nothing of a binding already tenant-wide —
    unless it is reopened or moved."""
    world = await make_world(iam_app, iam_client, signing_key)
    writer = [*PERMISSIONS, "principals.read", "principals.write"]
    await world.bind(visibility="members", permissions=writer)
    token = world.token()
    peer = await iam_client.post(
        "/api/v1/principals",
        json={"kind": "human", "displayName": "Bob"},
        headers=auth(world.admin),
    )
    bob = peer.json()["id"]
    bob_identity = new_identity()
    first = await world.bind(bob, bob_identity)
    assert first.json()["visibility"] == "tenant"

    def body(**extra: Any) -> dict[str, Any]:
        return {**bob_identity, "permissions": ["tasks.read"], **extra}

    changed = await iam_client.post(
        f"/api/v1/principals/{bob}/iam-bindings", json=body(), headers=auth(token)
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["visibility"] == "tenant"

    revoked = await iam_client.post(
        f"/api/v1/iam-bindings/{first.json()['id']}:revoke", headers=auth(world.admin)
    )
    assert revoked.status_code == 200, revoked.text
    reopened = await iam_client.post(
        f"/api/v1/principals/{bob}/iam-bindings", json=body(), headers=auth(token)
    )
    assert reopened.status_code == 403, reopened.text
    assert reopened.json()["error"]["code"] == "visibility_escalation"


async def test_principals_write_is_checked_before_the_visibility_value(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B5: without the right — 403 whatever the body says."""
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind()
    for value in ("all", None, 1):
        response = await iam_client.post(
            f"/api/v1/principals/{world.human}/iam-bindings",
            json={**new_identity(), "permissions": ["tasks.read"], "visibility": value},
            headers=auth(world.token()),
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] != "validation_error"


async def test_a_human_claiming_to_be_an_agent_is_still_narrowed(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B9: the kind is the local principal's, not the token's claim."""
    world = await make_world(iam_app, iam_client, signing_key)
    await world.bind(visibility="members")
    token = signing_key.issue(
        subject=uuid.UUID(world.identity["iamPrincipalId"]),
        tenant_id=uuid.UUID(world.identity["iamTenantId"]),
        scopes=[SCOPE_READ, SCOPE_WRITE],
        principal_type="agent",
        issuer=ISSUER,
        audience=AUDIENCE,
        ttl_seconds=3600,
    )
    assert await listed(world, token) == ids(world, "dept", "team")
    hidden = await world.get(f"/api/v1/tasks/{world.tasks['other']['publicId']}", token)
    assert hidden.status_code == 404, hidden.text


async def test_a_process_definition_of_an_invisible_workspace_is_its_own_404(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """B6: retiring or publishing over a definition of another workspace
    answers as for a missing definition, never naming the workspace."""
    setup = await _setup(iam_client)
    world = await make_world(iam_app, iam_client, signing_key, admin=setup["key"])
    await world.bind(
        visibility="members", permissions=[*PERMISSIONS, "processes.write", "processes.read"]
    )
    token = world.token()
    await _publish(iam_client, world.admin, "elsewhere", {**GOAL, "workspaceId": world.ws["other"]})

    async def retire(key: str) -> httpx.Response:
        return await iam_client.post(
            f"/api/v1/process-definitions/{key}:retire",
            params={"dryRun": "true"},
            json={"reason": "gone"},
            headers=auth(token),
        )

    same_as_missing(await retire("elsewhere"), await retire("nowhere"), ("elsewhere", "nowhere"))
    published = await iam_client.post(
        "/api/v1/process-definitions",
        json={"key": "elsewhere", "spec": {**GOAL, "version": 2, "workspaceId": world.ws["team"]}},
        headers=auth(token),
    )
    assert published.status_code == 404, published.text
    error = published.json()["error"]
    assert error["message"] == "Process definition not found"
    assert world.ws["other"] not in published.text
