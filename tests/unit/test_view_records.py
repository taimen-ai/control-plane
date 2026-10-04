"""Filters and order of a view of knowledge, computed in the core (CP-ADR-0080, amendment Б2).

Memory's ``where`` has its own meaning of ``prefix`` and comparisons, so the
records of a view of knowledge are filtered and ordered here by amendment A4:
equality of JSON values, a list attribute holds a condition when an element
does, a missing value holds none and goes last whatever the direction.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.application.authorization import AuthContext
from control_plane.application.queries.view_records import (
    _holds,
    _ordered,
    _Row,
    _same,
    knowledge_workspace,
    path_value,
    task_values,
)
from control_plane.domain.errors import ValidationError
from control_plane.domain.view_query import Condition, DeclaredField
from tests.unit.test_authorizer import make_ctx


def _condition(op: str, value: Any, kind: str = "text") -> Condition:
    return Condition(DeclaredField("attributes.x", "attributes.x", kind), op, value)


@pytest.mark.parametrize(
    ("condition", "value", "holds"),
    [
        (_condition("eq", "Moscow"), "Moscow", True),
        (_condition("eq", "Moscow"), "moscow", False),
        (_condition("in", ("a", "b")), "b", True),
        (_condition("in", ("a", "b")), ["c", "b"], True),
        (_condition("in", ("a", "b")), [], False),
        (_condition("prefix", "MOS"), "Moscow", True),
        (_condition("prefix", "%"), "Moscow", False),
        (_condition("prefix", "M"), 7, False),
        (_condition("eq", True, "enum"), True, True),
        (_condition("eq", True, "enum"), 1, False),
        (_condition("eq", 1, "enum"), True, False),
        (_condition("eq", 1, "enum"), 1.0, True),
        (_condition("eq", "x"), None, False),
        (_condition("gte", Decimal("10"), "number"), "10.00", True),
        (_condition("gte", Decimal("10"), "number"), 9.5, False),
        (_condition("lte", Decimal("10"), "number"), "ten", False),
        (_condition("lte", Decimal("10"), "number"), True, False),
        (_condition("eq", Decimal("2"), "number"), 2, True),
        (_condition("gte", "2026-01-01", "date"), "2026-01-01T00:00:00+00:00", True),
        (_condition("lte", "2025-12-31", "date"), "2026-01-01", False),
        (_condition("eq", "2026-01-01", "date"), 20260101, False),
    ],
)
def test_a_condition_on_one_value(condition: Condition, value: Any, holds: bool) -> None:
    assert _holds(condition, value) is holds


def test_equality_of_json_values() -> None:
    assert _same(1, Decimal("1.0"))
    assert not _same(False, 0)
    assert not _same("1", 1)
    assert _same({"a": 1}, {"a": 1})


def _row(key: str, kind: str = "k", **attributes: Any) -> _Row:
    values = {"kind": kind, "key": key, "attributes": attributes}
    return _Row(values, f"{kind}:{key}", key, "running")


def test_the_order_puts_a_missing_value_last_in_either_direction() -> None:
    rows = [_row("a", n=2), _row("b"), _row("c", n=10), _row("d", n="x"), _row("e", n=2)]
    field = DeclaredField("attributes.n", "attributes.n")
    ascending = [r.values["key"] for r in _ordered(rows, [(field, "asc")])]
    descending = [r.values["key"] for r in _ordered(rows, [(field, "desc")])]
    # Numbers before strings; ties by (kind, key); the missing one last.
    assert ascending == ["a", "e", "c", "d", "b"]
    assert descending == ["d", "c", "a", "e", "b"]
    assert [r.values["key"] for r in _ordered(rows, [])] == ["a", "b", "c", "d", "e"]
    assert _ordered([], [(field, "asc")]) == []


def test_ties_of_the_order_go_by_kind_then_key() -> None:
    rows = [_row("2", "b"), _row("1", "b"), _row("9", "a")]
    assert [r.id for r in _ordered(rows, [])] == ["a:9", "b:1", "b:2"]


def test_a_path_walks_the_variables_of_a_record() -> None:
    values = {"attributes": {"a": {"b": 1}, "l": [1]}, "kind": "k"}
    assert path_value("attributes.a.b", values) == 1
    assert path_value("attributes.a.c", values) is None
    assert path_value("attributes.l.0", values) is None
    assert path_value("kind", values) == "k"
    assert path_value("kind.x", values) is None


def test_the_variables_of_a_task() -> None:
    created = datetime(2026, 10, 4, tzinfo=UTC)
    task = SimpleNamespace(
        id="5f0c1e6a-0000-4000-8000-000000000001",
        public_id="TASK-1",
        title="T",
        description="",
        status="todo",
        system_status_category="active",
        priority="low",
        owner_id=None,
        assignee_id=None,
        workspace_id=None,
        created_by="5f0c1e6a-0000-4000-8000-000000000002",
        start_date=None,
        due_date=None,
        created_at=created,
        updated_at=created,
        completed_at=None,
        custom_fields=None,
    )
    values = task_values(task, {"p": 1}, None)  # type: ignore[arg-type]
    assert values["fields"]["createdAt"] == "2026-10-04T00:00:00Z"
    assert values["fields"]["ownerId"] is None and values["fields"]["dueDate"] is None
    assert values["customFields"] == {} and values["settings"] == {}
    assert values["param"] == {"p": 1} and values["id"] == task.id


W_DEPT = "00000000-0000-4000-8000-00000000000d"
W_TEAM = "00000000-0000-4000-8000-00000000000e"
W_ROOT = "00000000-0000-4000-8000-0000000000aa"
W_OTHER_ROOT = "00000000-0000-4000-8000-0000000000bb"


def _member(workspaces: tuple[str, ...], roots: tuple[str, ...]) -> AuthContext:
    return replace(
        make_ctx("events.read"),
        visibility="members",
        visible_workspaces=frozenset(workspaces),
        visible_roots=frozenset(roots),
    )


class _NoDatabase:
    """``members`` mode decides from the context alone: the tenant's roots are not read."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was asked: {name}")


async def test_members_the_tree_of_the_caller_is_any_visible_workspace_of_it() -> None:
    # A member of Dept under the root, which is not visible itself.
    ctx = _member((W_TEAM, W_DEPT), (W_ROOT,))
    chosen = await knowledge_workspace(_NoDatabase(), ctx, None)  # type: ignore[arg-type]
    assert chosen == uuid.UUID(W_DEPT)
    # The same on every call: the lowest id of the set.
    assert await knowledge_workspace(_NoDatabase(), ctx, None) == chosen  # type: ignore[arg-type]


async def test_members_a_named_workspace_is_taken_as_named() -> None:
    ctx = _member((W_DEPT,), (W_ROOT,))
    named = uuid.UUID(W_OTHER_ROOT)
    assert await knowledge_workspace(_NoDatabase(), ctx, named) == named  # type: ignore[arg-type]


async def test_members_of_nothing_read_no_knowledge() -> None:
    ctx = _member((), ())
    assert await knowledge_workspace(_NoDatabase(), ctx, None) is None  # type: ignore[arg-type]


async def test_members_in_two_trees_must_name_the_workspace() -> None:
    ctx = _member((W_DEPT, W_OTHER_ROOT), (W_ROOT, W_OTHER_ROOT))
    with pytest.raises(ValidationError) as refused:
        await knowledge_workspace(_NoDatabase(), ctx, None)  # type: ignore[arg-type]
    assert refused.value.code == "workspace_required"
