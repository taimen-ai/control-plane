"""Step events over the package tests of ``invoice-payment`` and ``tenders`` (P007).

Every test of both packages runs through ``POST /packages:test`` — the same
engine a live instance runs — and every step the sandbox takes is projected
by :func:`process_steps.step_events`, the function ``take()`` records the
core's step events with. Each waiting step must come out as exactly one
``step_entered``/``step_exited`` pair by ``activityId`` (an entry alone for a
step the test leaves open), no step more than two events per input; an
entry names the task or approvals its step opened.

This is the sandbox of ``packages:test``, not ``take()`` on a stored
instance: the packages need skills that ``packages:apply`` does not register
and ``recall`` against a memory service. The refs the core's executor writes
are rebuilt from the sandbox's tasks and approvals; ``take()`` itself with
``record_event`` is covered on the live core by ``test_process_step_events``.

The packages live in the superproject (``packages/``). The test runs where
control-plane is checked out inside it, or where ``CP_SUPERPROJECT`` names a
checkout of it; elsewhere it is skipped.
"""

import copy
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane.domain import process_sandbox, process_steps
from tests.helpers import auth, do_bootstrap
from tests.package_sdk import UMBRELLA

# The superproject by the layout control-plane lies in (TAI-ADR-0064), unless named.
SUPERPROJECT = Path(os.environ.get("CP_SUPERPROJECT") or UMBRELLA)
PACKAGES = SUPERPROJECT / "packages"
# What the two packages require: its objects are added to the package under test.
REQUIRED = ("platform-calendars", "notify", "process-knowledge")
UNDER_TEST = ("invoice-payment", "tenders")

pytestmark = pytest.mark.skipif(
    not all((PACKAGES / name / "package.yaml").is_file() for name in (*REQUIRED, *UNDER_TEST)),
    reason="the superproject's packages are not checked out (set CP_SUPERPROJECT)",
)


def _files(name: str, *, with_required: bool = False) -> dict[str, Any]:
    """The package as its files; ``with_required`` adds the objects of what it requires.

    Skills are registered by the installer, not by ``packages:apply``: the
    objects of the required packages go into the package under test, where
    ``packages:test`` knows them as the package's own.
    """
    files = []
    for source in (*(REQUIRED if with_required else ()), name):
        root = PACKAGES / source
        for path in sorted(root.rglob("*.yaml")):
            relative = path.relative_to(root).as_posix()
            if source != name and (relative == "package.yaml" or relative.startswith("tests/")):
                continue
            files.append({"path": relative, "content": path.read_text("utf-8")})
    return {"files": files}


@pytest.mark.parametrize("name", UNDER_TEST)
async def test_every_waiting_step_of_the_package_tests_is_one_pair(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]

    # (test, instance, activityId) -> the step events of it; a sandbox's seed names its test.
    seen: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    entries: list[tuple[process_sandbox.Sandbox, dict[str, Any]]] = []
    # Per instance, what take() keeps in its row: attempt counters and refs.
    kept: dict[tuple[str, str], tuple[dict[str, int], dict[str, Any]]] = {}
    take = process_sandbox.Sandbox.take

    def refs_of(sandbox: process_sandbox.Sandbox, instance_id: str) -> dict[str, Any]:
        """The refs the core's executor writes for the work the sandbox opened."""
        refs = {}
        for kind, items in (("task", sandbox.tasks), ("approval", sandbox.approvals)):
            for item in items:
                if item.instance == instance_id:
                    refs[f"{kind}:{item.id}"] = {"activity": item.activity, "element": item.element}
        return refs

    def projected(
        sandbox: process_sandbox.Sandbox,
        instance: Any,
        kind: str,
        body: Any,
        actor: str | None,
    ) -> None:
        before = copy.deepcopy(instance.state)
        decisions_before = len(sandbox.decisions)
        take(sandbox, instance, kind, body, actor)
        attempts, refs = kept.get((sandbox.seed, instance.id), ({}, {}))
        projection = process_steps.step_events(
            instance.definition,
            instance_id=instance.id,
            before=before,
            after=instance.state,
            decisions=[d.out() for _, d in sandbox.decisions[decisions_before:]],
            given={"kind": kind, "body": body},
            at=sandbox.clock,
            attempts=attempts,
            refs={**refs, **refs_of(sandbox, instance.id)},
        )
        kept[(sandbox.seed, instance.id)] = (projection.attempts, projection.refs)
        for event in projection.events:
            seen[(sandbox.seed, instance.id, event.activity_id)].append(event.type)
            if event.type == process_steps.STEP_ENTERED:
                entries.append((sandbox, event.payload))

    monkeypatch.setattr(process_sandbox.Sandbox, "take", projected)
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": _files(name, with_required=True)},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    failed = [(t["file"], t["failures"]) for t in body["tests"] if t["status"] != "passed"]
    assert body["status"] == "passed", (body["problems"], failed)
    assert len(body["tests"]) >= 10

    assert seen, "the package's tests open waiting steps"
    for activity, types in seen.items():
        assert types in (
            [process_steps.STEP_ENTERED, process_steps.STEP_EXITED],
            [process_steps.STEP_ENTERED],
        ), (activity, types)
    pairs = sum(1 for types in seen.values() if len(types) == 2)
    assert pairs > len(body["tests"]), "most waiting steps close within their test"

    # An entry names the work its step opened, as the core's refs route it back.
    kinds = {payload["stepKind"] for _, payload in entries}
    assert {"human", "approve"} <= kinds, kinds
    for sandbox, payload in entries:
        mine = (payload["instanceId"], payload["activityId"])
        tasks = [t.id for t in sandbox.tasks if (t.instance, t.activity) == mine]
        approvals = [a.id for a in sandbox.approvals if (a.instance, a.activity) == mine]
        if payload["stepKind"] == "human":
            assert payload["taskId"] == tasks[-1], payload
        if payload["stepKind"] == "approve":
            assert payload["approvalIds"] and set(payload["approvalIds"]) <= set(approvals)
