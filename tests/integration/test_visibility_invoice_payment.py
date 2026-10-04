"""SC-002 end to end, on the ``invoice-payment`` package (CP-ADR-0082 §4, T004).

The package — data installed through the public API, not a fixture of this
feature (constitution art. II) — files invoice work in ``finance`` and asks
the finance director to decide. Ann holds the director's role tenant-wide,
but takes part only in ``sales`` and her binding is in ``members`` mode.
She gets her own work, approval and artifact in ``sales`` — and not one
object of ``finance``: not in a list, not by reference, not in the journal,
not in "waiting for you". Deciding the finance approval answers ``404`` as
for one that does not exist, and the director outside ``members`` mode closes
the invoice as before.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import assign_role, auth, create_agent_with_key, create_workspace
from tests.integration.test_iam_enforcement import ISSUER
from tests.integration.test_invoice_payment_package import (
    PACKAGE,
    SETTLED,
    _approvals,
    _decide,
    _observe,
    _pay,
    _receive,
    _reconcile_amount,
    _setup,
    _task,
)
from tests.integration.test_workspace_visibility import same_as_missing

ANN_PERMISSIONS = [
    "tasks.read",
    "approvals.read",
    "approvals.decide",
    "artifacts.read",
    "events.read",
    "workspaces.read",
]
MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


async def test_members_gets_no_object_of_another_workspace(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    admin = s["key"]

    # --- finance: the package's own flow, up to the director's decision ------------
    invoice = await _receive(client, worker, sync_engine, s, "INV-7")
    run_id = await _pay(client, s["runner_key"], invoice, "1250.00")
    await _reconcile_amount(client, worker, sync_engine, s["host_key"])
    settled = await _observe(client, admin, SETTLED, s["workspace"], invoice="INV-7")
    await worker.run_once()
    [director_approval] = _approvals(sync_engine, invoice)
    pending = str(director_approval.id)
    report = await _ok(
        await client.post(
            "/api/v1/artifacts",
            json={"type": "note", "name": "INV-7 check", "task": invoice, "content": {}},
            headers=auth(admin),
        )
    )
    finance_task = await _task(client, admin, invoice)
    # An observation of the payment run: the journal event has no workspace of
    # its own, its payload names the run and the work (CP-ADR-0082 V7).
    on_run = (
        await _ok(
            await client.post(
                "/api/v1/observations",
                json={"kind": "finding", "content": "INV-7 paid twice?", "runId": run_id},
                headers=auth(admin),
            )
        )
    )["id"]

    # --- sales: Ann's own workspace and her own work of the same package ------------
    sales = await create_workspace(client, admin, "sales")
    own = await _ok(
        await client.post(
            "/api/v1/tasks",
            json={
                "title": "Pay invoice S-1",
                "typeKey": PACKAGE,
                "workspaceId": sales["id"],
                "customFields": {
                    "invoice": "S-1",
                    "supplier": "Contoso",
                    "invoiceAmount": "10.00",
                },
            },
            headers=auth(admin),
        )
    )
    own_approval = await _ok(
        await client.post(
            "/api/v1/approvals",
            json={"task": own["id"], "requiredRoleId": s["role"], "gate": True},
            headers=auth(admin),
        )
    )
    own_artifact = await _ok(
        await client.post(
            "/api/v1/artifacts",
            json={"type": "note", "name": "S-1 check", "task": own["id"], "content": {}},
            headers=auth(admin),
        )
    )

    ann, key = await create_agent_with_key(
        client, admin, name="ann", permissions=ANN_PERMISSIONS, kind="human"
    )
    # The director's role tenant-wide: without visibility both approvals are hers.
    await assign_role(client, admin, ann["id"], s["role"])
    await _ok(
        await client.post(
            f"/api/v1/workspaces/{sales['id']}/members",
            json={"principalId": ann["id"]},
            headers=auth(admin),
        )
    )
    attention = await _ok(await client.get("/api/v1/me/attention", headers=auth(key)))
    assert pending in {item["entity"]["id"] for item in attention["items"]}
    await _ok(
        await client.post(
            f"/api/v1/principals/{ann['id']}/iam-bindings",
            json={
                "issuer": ISSUER,
                "iamTenantId": str(uuid.uuid4()),
                "iamPrincipalId": str(uuid.uuid4()),
                "permissions": ANN_PERMISSIONS,
                "visibility": "members",
            },
            headers=auth(admin),
        )
    )

    async def get(path: str, **params: Any) -> httpx.Response:
        return await client.get(path, params=params, headers=auth(key))

    finance_ids = {invoice, pending, run_id, report["id"], s["workspace"], settled, on_run}

    # Lists: her own objects, none of finance.
    tasks = await _ok(await get("/api/v1/tasks", limit=200))
    assert {t["id"] for t in tasks["items"]} == {own["id"]}
    approvals = await _ok(await get("/api/v1/approvals", limit=200))
    assert {a["id"] for a in approvals["items"]} == {own_approval["id"]}
    artifacts = await _ok(await get("/api/v1/artifacts", limit=200))
    assert {a["id"] for a in artifacts["items"]} == {own_artifact["id"]}
    assert (await _ok(await get("/api/v1/runs", limit=200)))["items"] == []
    workspaces = await _ok(await get("/api/v1/workspaces", limit=200))
    assert {w["id"] for w in workspaces["items"]} == {sales["id"]}

    # By reference: exactly as missing.
    for path, hidden in (
        (f"/api/v1/tasks/{finance_task['publicId']}", finance_task["publicId"]),
        (f"/api/v1/tasks/{invoice}/verifications", invoice),
        (f"/api/v1/approvals/{pending}", pending),
        (f"/api/v1/artifacts/{report['id']}", report["id"]),
        (f"/api/v1/runs/{run_id}", run_id),
        (f"/api/v1/workspaces/{s['workspace']}", s["workspace"]),
    ):
        missing = "TASK-999999" if hidden.startswith("TASK-") else MISSING
        same_as_missing(
            await get(path), await get(path.replace(hidden, missing)), (hidden, missing)
        )

    # The journal: nothing of finance, by workspace, by entity or in a payload —
    # the observations of finance included.
    journal = await _ok(await get("/api/v1/events", limit=200))
    assert journal["items"], "her own workspace has events"
    for event in journal["items"]:
        assert event["workspaceId"] != s["workspace"], event
        assert event["entityId"] not in finance_ids, event
        dumped = json.dumps(event)
        assert not [i for i in finance_ids if i in dumped], event
    for observation in (settled, on_run):
        assert (await _ok(await get("/api/v1/events", entityId=observation)))["items"] == []
    assert (await _ok(await get("/api/v1/events", entityId=invoice)))["items"] == []

    # "Waiting for you": her own approval, not the finance one.
    attention = await _ok(await get("/api/v1/me/attention"))
    entities = {item["entity"]["id"] for item in attention["items"]}
    assert own_approval["id"] in entities
    assert not entities & finance_ids

    # Deciding the finance approval: 404, as for one that does not exist.
    same_as_missing(
        await client.post(f"/api/v1/approvals/{pending}:approve", json={}, headers=auth(key)),
        await client.post(f"/api/v1/approvals/{MISSING}:approve", json={}, headers=auth(key)),
        (pending, MISSING),
    )
    [still] = _approvals(sync_engine, invoice)
    assert still.status == "pending"

    # The director outside members mode decides as before; the invoice closes.
    await _decide(client, s["director_key"], pending, "approve")
    await worker.run_once()
    done = await _task(client, admin, invoice)
    assert done["systemStatusCategory"] == "terminal_success"
