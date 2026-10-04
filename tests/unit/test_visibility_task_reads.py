"""Reads of a task by id see its workspace (CP-ADR-0082 §3.7, tails of the T003 review).

``authorize(..., resource=ResourceRef("task", id))`` asks about permissions
only: the authorizer reads no rows and does not know the task's workspace.
Every place that reads a task by id for a caller therefore asks
``permits_task`` (or checks the workspace itself), and these places are the
ones the review named: the steps of a package screen, ``spawnedBy`` of a
context pack, the outcome's ``ensureWork`` by key and its ``spawnedBy``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.application.authorization import permits_task
from control_plane.application.commands import approval_outcomes
from control_plane.application.commands.approval_outcomes import DecisionContext
from control_plane.application.queries import task_context, view_data
from control_plane.domain.approval_outcomes import expressions_in
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError
from tests.unit.test_authorizer import make_ctx

SEEN, HIDDEN = uuid.uuid4(), uuid.uuid4()


def members(*permissions: str) -> Any:
    return replace(
        make_ctx(*permissions), visibility="members", visible_workspaces=frozenset({str(SEEN)})
    )


def task(workspace_id: uuid.UUID | None, **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "public_id": f"TASK-{uuid.uuid4().int % 1_000_000:06d}",
        "title": "the work",
        "description": "",
        "assignee_id": uuid.uuid4(),
        "workspace_id": workspace_id,
        "status": "todo",
        "priority": "medium",
        "custom_fields": {},
        "type_id": uuid.uuid4(),
    }
    return SimpleNamespace(**{**fields, **extra})


async def test_permits_task_sees_the_workspace_of_the_task() -> None:
    ctx = members("tasks.read")
    assert await permits_task(ctx, Permission.TASKS_READ, task=task(SEEN))
    assert not await permits_task(ctx, Permission.TASKS_READ, task=task(HIDDEN))
    # Work without a workspace is not visible in members mode (B2).
    assert not await permits_task(ctx, Permission.TASKS_READ, task=task(None))
    # A missing permission is still a no; tenant mode sees every workspace.
    assert not await permits_task(members(), Permission.TASKS_READ, task=task(SEEN))
    assert await permits_task(make_ctx("tasks.read"), Permission.TASKS_READ, task=task(HIDDEN))


# --- view_data._steps ---------------------------------------------------------------


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def get(self, *_: Any) -> None:
        return None

    async def scalars(self, *_: Any) -> list[Any]:
        return self.rows


async def test_a_step_does_not_name_work_of_an_invisible_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine, theirs = task(SEEN), task(HIDDEN)
    opened = [
        {"id": "review", "taskId": str(mine.id)},
        {"id": "pay", "taskId": str(theirs.id)},
    ]
    monkeypatch.setattr(view_data, "open_elements", lambda _instance: opened)
    q: Any = SimpleNamespace(db=_Rows([mine, theirs]), ctx=members("tasks.read"))
    instance: Any = SimpleNamespace(definition_id=uuid.uuid4())

    steps = (await view_data._steps(q, instance))["items"]

    assert steps[0]["task"] == {"ref": mine.public_id, "title": mine.title}
    assert steps[0]["assignee"] == str(mine.assignee_id)
    # The step is shown, its work is not: no reference, title or assignee.
    assert steps[1] == {"key": "pay", "title": "pay"}


# --- task_context: spawnedBy ---------------------------------------------------------


async def test_a_context_pack_does_not_read_spawned_by_of_an_invisible_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = task(SEEN)
    parent = task(HIDDEN, title="the parent's secret")

    async def spawned_by_of(_session: Any, _task: Any) -> Any:
        return parent

    async def get(_model: Any, _id: Any) -> Any:
        return child

    monkeypatch.setattr(task_context, "spawned_by_of", spawned_by_of)
    schema: Any = SimpleNamespace(roots={"task", "spawnedBy"}, anchors=[])
    warnings: list[str] = []
    ctx = members("tasks.read")

    await task_context._sources(SimpleNamespace(), ctx, child, schema, warnings)  # type: ignore[arg-type]
    assert warnings == ["the task this one was spawned by is not readable"]

    session: Any = SimpleNamespace(get=get)
    assert not await task_context._spawned_readable(session, ctx, child.id)
    # Of a visible workspace it is read as before.
    parent.workspace_id = SEEN
    assert await task_context._spawned_readable(session, ctx, child.id)


# --- approval outcomes: ensureWork by key and spawnedBy -------------------------------------


async def test_ensure_work_by_key_does_not_hand_out_work_of_an_invisible_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = task(HIDDEN)

    async def origin(_session: Any, _tenant: Any, _key: str) -> Any:
        return found

    monkeypatch.setattr(approval_outcomes, "_origin", origin)
    context: Any = SimpleNamespace(workspace_id=None)
    ctx = members("tasks.read", "tasks.write")

    with pytest.raises(NotFoundError) as refused:
        await approval_outcomes._ensure_work(None, ctx, context, {"key": "invoice:1"}, 0)  # type: ignore[arg-type]
    # Exactly the answer of a task the decider may not read: no id, no public id.
    assert (refused.value.message, refused.value.details) == (
        "Task not found",
        {"key": "invoice:1"},
    )

    found.workspace_id = SEEN
    answer = await approval_outcomes._ensure_work(None, ctx, context, {"key": "invoice:1"}, 0)  # type: ignore[arg-type]
    assert answer == {"taskId": str(found.id), "publicId": found.public_id, "created": False}


async def test_an_outcome_does_not_read_spawned_by_of_an_invisible_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child, parent = task(SEEN), task(HIDDEN)

    async def spawned_by_of(_session: Any, _task: Any) -> Any:
        return parent

    async def get(_model: Any, _id: Any) -> Any:
        return child

    monkeypatch.setattr(approval_outcomes, "spawned_by_of", spawned_by_of)
    session: Any = SimpleNamespace(get=get)
    context = DecisionContext(
        approval_id=uuid.uuid4(),
        decided_by=uuid.uuid4(),
        outcome="approved",
        approval={},
        task={"id": str(child.id)},
        spawned_by={},
    )
    paths = tuple(expressions_in("$.spawnedBy.publicId", invocation=True))
    assert paths, "the expression names spawnedBy"

    with pytest.raises(NotFoundError):
        await approval_outcomes.read_context(
            session, members("tasks.read"), context, (), extra=paths
        )
    assert context.spawned_by == {}

    parent.workspace_id = SEEN
    await approval_outcomes.read_context(session, members("tasks.read"), context, (), extra=paths)
    assert context.spawned_by["publicId"] == parent.public_id
