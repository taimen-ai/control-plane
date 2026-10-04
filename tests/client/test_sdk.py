"""Official SDK against a real test Control Plane (ASGI transport)."""

import asyncio
import uuid
from collections.abc import Callable

import httpx
import pytest

from control_plane_client import (
    ClaimConflictError,
    ControlPlaneClient,
    ControlPlaneError,
    StaleClaimError,
    TransportError,
    VersionConflictError,
)
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
)

Make = Callable[[str], ControlPlaneClient]


async def test_full_work_cycle(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key, title="SDK job")

    async with sdk(agent_key) as sdk:
        context = await sdk.get_context()
        assert context["principal"]["kind"] == "agent"

        session = await sdk.open_session(
            client_name="sdk-test",
            harness_type="cli",
            capabilities=["resume", "skills.protocol.local"],
        )
        assert session["protocolVersion"] == "2"

        work = await sdk.list_available_work()
        assert task["id"] in [t["id"] for t in work["items"]]

        claim = await sdk.claim_task(task["id"], session["id"], intent="sdk cycle")
        run = await sdk.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        await sdk.create_checkpoint(run["id"], kind="working_state", data={"step": 1})
        await sdk.record_action(run["id"], action="local.build", external_reference="ok")
        await sdk.create_artifact(
            type="report", name="result", task_ref=task["id"], run_id=run["id"]
        )
        result = await sdk.succeed_run(run["id"])
        assert result["task"]["status"] == "done"
        assert result["run"]["status"] == "succeeded"

        await sdk.close_session(session["id"])


async def test_active_turn_control_sdk(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="SDK controlled job")

    async with sdk(agent_key) as client_sdk:
        session = await client_sdk.open_session(client_name="sdk-control")
        claim = await client_sdk.claim_task(task["id"], session["id"])
        run = await client_sdk.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        created = await client_sdk.create_run_control_message(
            run["id"],
            operation="steer",
            causal_position="turn:1",
            expected_run_version=run["version"],
            directive="Verify the public contract",
        )
        listed = await client_sdk.list_run_control_messages(run["id"])
        assert [item["id"] for item in listed["items"]] == [created["controlMessage"]["id"]]
        applied = await client_sdk.acknowledge_run_control_message(
            run["id"],
            created["controlMessage"]["id"],
            status="applied",
            claim_id=claim["id"],
            fencing_token=claim["fencingToken"],
            expected_run_version=created["runVersion"],
            expected_message_version=1,
            safe_boundary="model:1:cancelled",
        )
        assert applied["controlMessage"]["status"] == "applied"


async def test_typed_errors(client: httpx.AsyncClient, sdk: Make, sync_engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, a_key = await create_agent_with_key(client, admin_key, name="a")
    _, b_key = await create_agent_with_key(client, admin_key, name="b")
    task = await create_task(client, admin_key)

    async with sdk(a_key) as sdk_a, sdk(b_key) as sdk_b:
        session_a = await sdk_a.open_session(client_name="a")
        claim_a = await sdk_a.claim_task(task["id"], session_a["id"])

        session_b = await sdk_b.open_session(client_name="b")
        with pytest.raises(ClaimConflictError) as exc_info:
            await sdk_b.claim_task(task["id"], session_b["id"])
        assert exc_info.value.code == "task_already_claimed"

        # Takeover, then the old holder's run write is a typed StaleClaimError.
        run = await sdk_a.start_run(
            task["id"], claim_id=claim_a["id"], fencing_token=claim_a["fencingToken"]
        )
        backdate_expiry(sync_engine, "task_claims", claim_a["id"])
        await sdk_b.claim_task(task["id"], session_b["id"])
        with pytest.raises(StaleClaimError):
            await sdk_a.succeed_run(run["id"])


class _LoseResponseTransport(httpx.AsyncBaseTransport):
    """Executes the request but 'loses' the FIRST matching response —
    simulating 'request committed, client never heard back'."""

    def __init__(self, inner: httpx.AsyncBaseTransport, lose_path_suffix: str) -> None:
        self.inner = inner
        self.lose_path_suffix = lose_path_suffix
        self.lost = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if not self.lost and request.url.path.endswith(self.lose_path_suffix):
            self.lost = True
            await response.aread()
            raise httpx.ConnectError("simulated response loss", request=request)
        return response


async def test_idempotent_retry_after_lost_response(
    client: httpx.AsyncClient, app, sync_engine
) -> None:
    """The SDK retries with the SAME Idempotency-Key: exactly one run exists."""
    from sqlalchemy import text

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)

    transport = _LoseResponseTransport(httpx.ASGITransport(app=app), ":start-run")
    async with ControlPlaneClient("http://testserver", agent_key, transport=transport) as sdk:
        session = await sdk.open_session(client_name="flaky")
        claim = await sdk.claim_task(task["id"], session["id"])
        run = await sdk.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        assert transport.lost  # the failure really happened
        assert run["status"] == "running"

    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM runs WHERE task_id = :t"), {"t": task["id"]}
        ).scalar()
    assert count == 1


async def test_follow_events_catches_up(client: httpx.AsyncClient, sdk: Make) -> None:
    import asyncio

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    async with sdk(agent_key) as sdk:
        cursor = (await sdk.get_context())["eventCursor"]
        await create_task(client, admin_key, title="one")
        await create_task(client, admin_key, title="two")

        stop = asyncio.Event()
        seen: list[str] = []
        async for event in sdk.follow_events(cursor=cursor, poll_interval=0.1, stop=stop):
            seen.append(event["type"])
            if len(seen) == 2:
                stop.set()
        assert seen == ["task.created", "task.created"]


async def test_heartbeat_runner_survives_transport_blip_dies_on_domain_error(
    client: httpx.AsyncClient, sdk: Make, sync_engine
) -> None:
    """A network blip must not be read as lost ownership; an expired lease must."""
    from control_plane_client import HeartbeatRunner, TransportError

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    async with sdk(agent_key) as sdk:
        session = await sdk.open_session(client_name="hb")
        runner = HeartbeatRunner(sdk, session_id=session["id"], interval_seconds=0.01)

        calls = {"n": 0}
        real = sdk.heartbeat_session
        recovered = asyncio.Event()

        async def flaky(session_id: str, **kwargs: object) -> dict[str, object]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransportError("simulated blip")
            result = await real(session_id, **kwargs)  # type: ignore[arg-type]
            if calls["n"] >= 3:
                recovered.set()
            return result

        sdk.heartbeat_session = flaky  # type: ignore[method-assign]
        runner.start()
        await asyncio.wait_for(recovered.wait(), 5)
        assert runner.error is None  # one blip is not lost ownership
        assert runner.alive

        # A domain failure (session gone) stops the runner immediately.
        sdk.heartbeat_session = real  # type: ignore[method-assign]
        backdate_expiry(sync_engine, "sessions", session["id"])
        await asyncio.sleep(0.05)
        await runner.stop()
        assert runner.error is not None
        assert runner.error.code in ("session_expired", "session_not_active")


async def test_transport_error_is_typed(client: httpx.AsyncClient) -> None:
    sdk = ControlPlaneClient("http://127.0.0.1:1", "cp_x_y", timeout=0.2)
    with pytest.raises(TransportError):
        await sdk.get_context()
    await sdk.aclose()


async def test_operator_task_and_relation_methods(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]

    async with sdk(admin_key) as operator:
        root = await operator.create_task(title="Root operator intent", priority="high")
        child = await operator.create_task(title="Child step", parent_task=root["id"])
        page = await operator.list_tasks(status="todo", assignee_id=boot["adminPrincipal"]["id"])
        assert page["items"] == []

        updated = await operator.update_task(
            child["id"], expected_version=child["version"], description="Confirmed detail"
        )
        assert updated["version"] == child["version"] + 1
        with pytest.raises(VersionConflictError):
            await operator.update_task(
                child["id"], expected_version=child["version"], priority="low"
            )

        relation = await operator.add_task_relation(
            root["id"], to_task=child["id"], relation_type="related_to"
        )
        await operator.remove_task_relation(root["id"], relation["id"])


async def test_iam_binding_methods(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, _ = await create_agent_with_key(client, admin_key, permissions=["tasks.read"])
    iam_tenant, iam_principal = str(uuid.uuid4()), str(uuid.uuid4())

    async with sdk(admin_key) as operator:
        created = await operator.upsert_iam_binding(
            agent["id"],
            issuer="https://iam.test",
            iam_tenant_id=iam_tenant,
            iam_principal_id=iam_principal,
            permissions=["tasks.read", "tasks.write"],
        )
        assert created["status"] == "active"
        assert created["permissions"] == ["tasks.read", "tasks.write"]

        repointed = await operator.upsert_iam_binding(
            agent["id"],
            issuer="https://iam.test",
            iam_tenant_id=iam_tenant,
            iam_principal_id=iam_principal,
            permissions=["tasks.read"],
        )
        assert repointed["id"] == created["id"]

        listed = await operator.list_iam_bindings(agent["id"])
        assert [b["id"] for b in listed["items"]] == [created["id"]]

        revoked = await operator.revoke_iam_binding(created["id"])
        assert revoked["status"] == "revoked"
        assert revoked["revokedAt"] is not None


async def test_handoff_retry_after_lost_response_creates_one_checkpoint(
    client: httpx.AsyncClient, app, sync_engine
) -> None:
    from sqlalchemy import text

    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    task = await create_task(client, key, title="Ambiguous handoff")
    transport = _LoseResponseTransport(httpx.ASGITransport(app=app), ":handoff")
    async with ControlPlaneClient("http://testserver", key, transport=transport) as operator:
        session = await operator.open_session(client_name="codex", harness_type="codex")
        claim = await operator.claim_task(task["id"], session["id"])
        run = await operator.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        handoff = await operator.prepare_handoff(
            run["id"], summary="Continue after the lost response", evidence_refs=["git:abc123"]
        )
        assert transport.lost
        assert handoff["run"]["status"] == "suspended"

    with sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM run_checkpoints WHERE run_id = :run"),
            {"run": run["id"]},
        ).scalar_one()
    assert count == 1


# --- consumer-facing extensions (ADR-0030: vertical packages use this client) -----


def _capturing(captured: list[dict], response: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
                "body": request.content.decode() if request.content else "",
            }
        )
        return httpx.Response(200, json=response or {"id": str(uuid.uuid4())})

    return httpx.MockTransport(handler)


async def test_consumer_names_itself_before_the_sdk_token() -> None:
    captured: list[dict] = []
    async with ControlPlaneClient(
        "http://cp.test", "cp_key", transport=_capturing(captured), user_agent="bidops-runner/0.1"
    ) as sdk:
        await sdk.get_task("TASK-000001")
    agent = captured[0]["headers"]["user-agent"]
    assert agent.startswith("bidops-runner/0.1 control-plane-client/")


async def test_a_caller_chosen_idempotency_key_is_sent_verbatim() -> None:
    """A key derived from the caller's own state makes a replay after a crash
    the same business command; the generated key covers only in-process retries."""
    captured: list[dict] = []
    sdk = ControlPlaneClient("http://cp.test", "cp_key", transport=_capturing(captured))
    async with sdk:
        await sdk.create_task(title="t", idempotency_key="task-1-attempt-1")
        await sdk.succeed_run("r1", idempotency_key="r1-succeed")
        await sdk.fail_run("r1", failure_reason="x", idempotency_key="r1-fail")
        await sdk.create_artifact(type="a", name="n", idempotency_key="art-1")
        await sdk.request_approval(required_role_id="role", idempotency_key="appr-1")
        await sdk.create_task(title="generated")
    keys = [entry["headers"]["idempotency-key"] for entry in captured]
    assert keys[:5] == ["task-1-attempt-1", "r1-succeed", "r1-fail", "art-1", "appr-1"]
    assert keys[5] and keys[5] not in keys[:5]


async def test_workspace_id_goes_into_approval_and_artifact_bodies() -> None:
    import json

    captured: list[dict] = []
    sdk = ControlPlaneClient("http://cp.test", "cp_key", transport=_capturing(captured))
    async with sdk:
        await sdk.request_approval(workspace_id="ws-1", assigned_principal_id="p-1", gate=True)
        await sdk.create_artifact(type="doc", name="n", workspace_id="ws-1")
        await sdk.create_artifact(type="doc", name="n", task_ref="TASK-000002")
    approval, with_ws, without_ws = (json.loads(entry["body"]) for entry in captured)
    assert approval == {
        "comment": "",
        "gate": True,
        "workspaceId": "ws-1",
        "assignedPrincipalId": "p-1",
    }
    assert with_ws["workspaceId"] == "ws-1"
    assert "workspaceId" not in without_ws


async def test_directory_and_artifact_reads_use_documented_paths() -> None:
    captured: list[dict] = []
    page = {"items": [], "nextCursor": None}
    async with ControlPlaneClient(
        "http://cp.test", "cp_key", transport=_capturing(captured, page)
    ) as sdk:
        await sdk.get_artifact("art-1")
        await sdk.list_roles(workspace_id="ws-1", limit=50)
        await sdk.list_principals(kind="agent", cursor="c1")
        await sdk.list_principals()
    assert [(e["method"], e["path"], e["params"]) for e in captured] == [
        ("GET", "/api/v1/artifacts/art-1", {}),
        ("GET", "/api/v1/roles", {"limit": "50", "workspaceId": "ws-1"}),
        ("GET", "/api/v1/principals", {"cursor": "c1", "kind": "agent"}),
        ("GET", "/api/v1/principals", {}),
    ]


async def test_knowledge_snapshot_document_and_packs(
    client: httpx.AsyncClient, sdk: Make, app
) -> None:
    from control_plane_client.errors import ConflictError, NotFoundError
    from tests.helpers import FakeKnowledge, create_workspace
    from tests.helpers import knowledge_snapshot as snapshot

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    app.state.settings.knowledge_pack_admins = [boot["adminPrincipal"]["id"]]
    try:
        async with sdk(agent_key) as agent:
            answer = await agent.submit_knowledge_snapshot(
                workspace_id=root["id"], snapshot=snapshot()
            )
            assert answer["duplicate"] is False
            plan = await agent.preview_knowledge_snapshot(
                workspace_id=root["id"], snapshot=snapshot(snapshotId="snap-2")
            )
            assert plan["dryRun"] is True
            applied = await agent.submit_knowledge_snapshot(
                workspace_id=root["id"],
                snapshot=snapshot(snapshotId="snap-2"),
                expected_state=plan["stateToken"],
            )
            assert applied["stateToken"] != plan["stateToken"]
            with pytest.raises(ConflictError):
                await agent.submit_knowledge_snapshot(
                    workspace_id=root["id"],
                    snapshot=snapshot(snapshotId="snap-3"),
                    expected_state=plan["stateToken"],
                )
            stored = await agent.submit_knowledge_document(
                workspace_id=root["id"],
                document={
                    "naturalKey": "document:license-1",
                    "title": "License",
                    "chunks": [{"text": "License No. 1"}],
                    "links": [{"kind": "credential", "key": "license-1", "rel": "evidenced_by"}],
                },
            )
            assert stored["natural_key"] == "document:license-1"
        async with sdk(admin_key) as admin:
            registered = await admin.register_knowledge_pack({"name": "selfdev", "version": 1})
            assert registered["status"] == "created"
            packs = await admin.set_workspace_knowledge_packs(
                root["id"], packs=["selfdev@1"], strict=True
            )
            assert packs["packages"] == ["selfdev@1"]
            current = await admin.get_workspace_knowledge_packs(root["id"])
            assert (current["packs"], current["strict"]) == (["selfdev@1"], True)
            pack = await admin.get_knowledge_pack("selfdev@1")
            assert (pack["name"], pack["version"]) == ("selfdev", "1")
            with pytest.raises(NotFoundError):
                await admin.get_knowledge_pack("selfdev@2")
        app.state.context_provider = FakeKnowledge(fail_status=409)
        async with sdk(agent_key) as agent:
            with pytest.raises(ConflictError):
                await agent.submit_knowledge_snapshot(workspace_id=root["id"], snapshot=snapshot())
    finally:
        app.state.context_provider = None
        app.state.settings.knowledge_pack_admins = []
    assert [kind for kind, _ in fake.calls] == [
        "reconcile",
        "reconcile",
        "reconcile",
        "reconcile",
        "document",
        "package",
        "kinds",
        "read_kinds",
        "read_package",
        "read_package",
    ]
    assert fake.calls[1][1]["dry_run"] is True
    assert fake.calls[2][1]["expected_state"] == "st1-1"
    assert fake.calls[6][1]["strict"] is True


async def test_knowledge_entities_page_by_page(client: httpx.AsyncClient, sdk: Make, app) -> None:
    from tests.fake_graph_memory import FakeGraphMemory
    from tests.helpers import create_workspace

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeGraphMemory()
    app.state.context_provider = fake
    keys: list[str] = []
    relations: list[str] = []
    try:
        async with sdk(agent_key) as agent:
            cursor = None
            while True:
                page = await agent.query_knowledge_entities(
                    workspace_id=root["id"],
                    kinds=["endpoint", "adr"],
                    where=[{"attr": "method", "op": "exists", "value": False}],
                    as_of="2026-09-28T00:00:00+00:00",
                    limit=1,
                    cursor=cursor,
                    include={"relations": ["governs"], "direction": "out"},
                )
                keys += [item["key"] for item in page["items"]]
                relations += [r["key"] for item in page["items"] for r in item["relations"]]
                cursor = page["nextCursor"]
                if cursor is None:
                    break
    finally:
        app.state.context_provider = None
    assert keys == ["CP-0019", "GET /runs/{}/checkpoints"]
    assert relations == ["control-plane:src/control_plane/api/v1/claims.py"]
    assert [r.get("cursor") for r in fake.entities_requests] == [None, "adr|CP-0019"]
    assert fake.entities_requests[0]["asOf"] == "2026-09-28T00:00:00+00:00"


async def test_goal_methods_and_work_graph_task_fields(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    """Goals and the task's goal/origin/acceptance/evidence (CP-ADR-0062)."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    check = {"key": "green", "kind": "deterministic", "description": "The suite passes"}
    async with sdk(admin_key) as api:
        goal = await api.create_goal(title="Stays green", criteria=[check])
        assert goal["createdFrom"]["kind"] == "human"
        subgoal = await api.create_goal(title="Unit layer", parent_goal_id=goal["id"])

        task = await api.create_task(
            title="Fix the flake",
            goal_id=subgoal["id"],
            origin={
                "kind": "external",
                "ref": "tracker:FLAKE-7",
                "evidence": [{"kind": "external", "externalRef": {"system": "ci", "id": "run-9"}}],
            },
            acceptance=[check],
        )
        assert task["goalId"] == subgoal["id"]
        assert task["origin"]["ref"] == "tracker:FLAKE-7"

        updated = await api.update_task(
            task["id"],
            expected_version=task["version"],
            evidence=[
                {
                    "kind": "external",
                    "externalRef": {"system": "ci", "id": "run-10"},
                    "check": "green",
                }
            ],
        )
        assert updated["evidence"][0]["check"] == "green"
        assert (await api.list_tasks(goal_id=subgoal["id"]))["items"][0]["id"] == task["id"]
        found = await api.list_tasks(q=task["publicId"].lower())
        assert [t["id"] for t in found["items"]] == [task["id"]]

        assert (await api.list_goal_work(goal["id"]))["items"] == []
        tree = await api.list_goal_work(goal["id"], include_subgoals=True)
        assert [t["id"] for t in tree["items"]] == [task["id"]]

        listed = await api.list_goals(parent_goal_id=goal["id"])
        assert [g["id"] for g in listed["items"]] == [subgoal["id"]]
        achieved = await api.update_goal(
            subgoal["id"], expected_version=subgoal["version"], status="achieved"
        )
        assert achieved["closedAt"] is not None
        with pytest.raises(VersionConflictError):
            await api.update_goal(subgoal["id"], expected_version=1, title="stale")
        with pytest.raises(TypeError):
            await api.update_goal(subgoal["id"], expected_version=2, created_from={})
        assert (await api.get_goal(subgoal["id"]))["status"] == "achieved"

        unlinked = await api.update_task(
            task["id"], expected_version=updated["version"], goal_id=None
        )
        assert unlinked["goalId"] is None


async def test_rule_methods(client: httpx.AsyncClient, sdk: Make) -> None:
    """Work rules through the SDK (CP-ADR-0063)."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    async with sdk(admin_key) as api:
        rule = await api.create_rule(
            key="drift",
            trigger={"kind": "observation", "type": "drift.seen"},
            condition={"eq": [{"var": "payload.data.severity"}, "high"]},
            action={
                "kind": "ensure_work",
                "taskType": "task",
                "dedupKeyTemplate": "drift:{{payload.data.id}}",
                "fields": {"title": "Drift {{payload.data.id}}"},
            },
        )
        assert rule["status"] == "enabled"
        with pytest.raises(ControlPlaneError) as exc:
            await api.create_rule(
                key="broken",
                trigger={"kind": "observation", "type": "drift.seen"},
                condition={"python": "True"},
                action=rule["action"],
            )
        assert exc.value.code == "invalid_rule_condition"

        changed = await api.update_rule(rule["id"], expected_version=1, condition=None)
        assert (changed["version"], changed["condition"]) == (2, True)
        with pytest.raises(TypeError):
            await api.update_rule(rule["id"], expected_version=2, key="renamed")
        assert (await api.disable_rule(rule["id"]))["status"] == "disabled"
        assert (await api.enable_rule(rule["id"]))["status"] == "enabled"
        assert [r["id"] for r in (await api.list_rules(key="drift"))["items"]] == [rule["id"]]
        assert (await api.get_rule(rule["id"]))["version"] == 2
        assert (await api.list_rule_evaluations(rule["id"]))["items"] == []
        assert await api.archive_rule(rule["id"]) == {}
        assert (await api.list_rules())["items"] == []


async def test_filtered_events_and_role_holders(client: httpx.AsyncClient, sdk: Make) -> None:
    """The SDK speaks the subscription filters and the addressee list (CP-ADR-0068)."""
    from tests.helpers import assign_role, create_role, create_workspace

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ops = await create_workspace(client, admin_key, "ops")
    sales = await create_workspace(client, admin_key, "sales")
    role = await create_role(client, admin_key, "approver")
    holder, _ = await create_agent_with_key(client, admin_key, name="holder")
    await assign_role(client, admin_key, holder["id"], role["id"], workspace_id=ops["id"])
    await create_task(client, admin_key, title="ops", workspaceId=ops["id"])
    await create_task(client, admin_key, title="sales", workspaceId=sales["id"])

    async with sdk(admin_key) as admin:
        page = await admin.list_events(types=["task.", "approval."], workspace_id=ops["id"])
        assert [(e["type"], e["payload"]["title"]) for e in page["items"]] == [
            ("task.created", "ops")
        ]
        assert page["items"][0]["schemaVersion"] == 1
        holders = await admin.list_role_holders(role["id"], workspace_id=ops["id"])
        assert [p["id"] for p in holders["items"]] == [holder["id"]]
        assert (await admin.list_role_holders(role["id"], workspace_id=sales["id"]))["items"] == []
        participants = await admin.list_workspace_participants(ops["id"], limit=10)
        assert [(p["principalId"], p["member"]) for p in participants["items"]] == [
            (holder["id"], False)
        ]
        assert [r["roleId"] for r in participants["items"][0]["roles"]] == [role["id"]]
        assert (await admin.list_workspace_participants(sales["id"]))["items"] == []


async def test_task_type_migration_methods(client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    async with sdk(admin_key) as operator:
        v1 = await operator.create_task_type(key="flow", display_name="Flow")
        first = await operator.create_task(title="One", type_key="flow")
        second = await operator.create_task(title="Two", type_key="flow")
        await operator.create_task_type(key="flow", display_name="Flow")

        moved = await operator.migrate_task_type(first["id"], version=first["version"])
        assert (moved["typeVersion"], moved["version"]) == (2, first["version"] + 1)

        page = await operator.migrate_type_tasks(v1["id"], to_version=2, limit=10)
        assert [m["taskId"] for m in page["migrated"]] == [second["id"]]
        assert page["nextCursor"] is None
