"""The local seam of visibility by workspace (CP-ADR-0082 §3.4-3.7)."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from platform_auth import ResourceRef

from control_plane.application.authorization import (
    Authorizer,
    WorkspaceNotVisible,
    authorize,
    configure_authorizer,
    permits,
    visible_objects,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError, NotFoundError
from tests.unit.test_authorizer import FakePolicy, make_ctx

W1, W2, W3 = (str(uuid.uuid4()) for _ in range(3))


def members(*workspaces: str, permissions: tuple[str, ...] = ("tasks.read",)):
    return replace(
        make_ctx(*permissions), visibility="members", visible_workspaces=frozenset(workspaces)
    )


@pytest.fixture(autouse=True)
def _reset_authorizer():
    yield
    configure_authorizer(Authorizer(None, "local"))


@pytest.mark.parametrize("mode", ["local", "shadow"])
async def test_members_narrows_workspaces_in_local_and_shadow(mode: str) -> None:
    configure_authorizer(Authorizer(FakePolicy() if mode != "local" else None, mode))  # type: ignore[arg-type]
    ctx = members(W1, W2)
    assert await visible_objects(ctx, Permission.TASKS_READ, "workspace") == {W1, W2}
    # tenant: no restriction, as before.
    assert await visible_objects(make_ctx("tasks.read"), Permission.TASKS_READ, "workspace") is None


async def test_members_of_nothing_sees_an_empty_set_not_none() -> None:
    assert await visible_objects(members(), Permission.TASKS_READ, "workspace") == set()


async def test_other_resource_types_are_not_narrowed() -> None:
    assert await visible_objects(members(W1), "memory.read", "memory_namespace") is None


async def test_policy_mode_intersects_the_pdp_answer_with_membership() -> None:
    policy = FakePolicy(objects=[W2, W3])
    configure_authorizer(Authorizer(policy, "policy"))
    assert await visible_objects(members(W1, W2), Permission.TASKS_READ, "workspace") == {W2}
    # A legacy credential in policy mode: the PDP is not asked, the set stays.
    legacy = replace(members(W1), iam_principal_id=None)
    assert await visible_objects(legacy, Permission.TASKS_READ, "workspace") == {W1}


async def test_authorize_outside_the_set_is_a_missing_workspace() -> None:
    ctx = members(W1)
    await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("workspace", W1))
    with pytest.raises(WorkspaceNotVisible) as excinfo:
        await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("workspace", W2))
    assert isinstance(excinfo.value, NotFoundError)
    assert (excinfo.value.code, excinfo.value.message, excinfo.value.details) == (
        "not_found",
        "Workspace not found",
        {"workspaceId": W2},
    )
    # Other resources and the tenant are not narrowed by visibility.
    await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", "t1"))
    await authorize(ctx, Permission.TASKS_READ)


async def test_a_missing_permission_is_403_before_visibility() -> None:
    with pytest.raises(AuthorizationError):
        await authorize(members(W1), Permission.TASKS_WRITE, resource=ResourceRef("workspace", W2))


async def test_policy_allow_does_not_lift_visibility() -> None:
    configure_authorizer(Authorizer(FakePolicy(allowed=True), "policy"))
    with pytest.raises(WorkspaceNotVisible):
        await authorize(members(W1), Permission.TASKS_READ, resource=ResourceRef("workspace", W2))


def test_sees_workspace() -> None:
    ctx = members(W1)
    assert ctx.sees_workspace(uuid.UUID(W1))
    assert ctx.sees_workspace(W1)
    assert not ctx.sees_workspace(W2)
    # Work outside any workspace is not in the set.
    assert not ctx.sees_workspace(None)
    tenant = make_ctx("tasks.read")
    assert tenant.sees_workspace(None)
    assert tenant.sees_workspace(W2)


async def test_permits_counts_an_invisible_workspace_as_not_allowed() -> None:
    """CP-ADR-0082 B6: a predicate does not let the workspace's 404 escape."""
    ctx = members(W1)
    assert await permits(ctx, Permission.TASKS_READ, resource=ResourceRef("workspace", W1))
    assert not await permits(ctx, Permission.TASKS_READ, resource=ResourceRef("workspace", W2))
    assert not await permits(ctx, Permission.TASKS_WRITE, resource=ResourceRef("workspace", W1))
    assert await permits(make_ctx("tasks.read"), Permission.TASKS_READ)


def _memory_settings():
    from control_plane.config import Settings

    return Settings(database_url="postgresql+psycopg://x/y", context_namespace_prefix="tenant:")


@pytest.mark.parametrize("mode", ["local", "shadow"])
async def test_memory_visibility_of_members_without_a_policy(mode: str) -> None:
    """CP-ADR-0082 B8: the visible set and the namespaces of its trees' roots."""
    from control_plane.application.queries.context import memory_visibility

    configure_authorizer(Authorizer(FakePolicy() if mode != "local" else None, mode))  # type: ignore[arg-type]
    ctx = replace(members(W1, W2), visible_roots=frozenset({W3}))
    visible = await memory_visibility(ctx, _memory_settings())
    assert visible is not None
    names, scopes = visible
    tenant = str(ctx.tenant_id)
    assert sorted(names) == sorted([f"tenant:{tenant}", f"tenant:{tenant}:ws:{W3}"])
    assert scopes[:2] == [f"workspace:{w}" for w in sorted([W1, W2])]
    assert f"principal:{ctx.principal_id}" in scopes
    # A member of nothing: no workspace at all, the principal's own still.
    alone = members()
    nothing = await memory_visibility(alone, _memory_settings())
    assert nothing is not None
    assert nothing == (
        [f"tenant:{alone.tenant_id}"],
        [f"principal:{alone.principal_id}", f"principal:{alone.iam_principal_id}"],
    )


async def test_memory_visibility_of_members_keeps_only_visible_trees_of_the_policy() -> None:
    from control_plane.application.queries.context import memory_visibility

    policy = FakePolicy(objects=[f"ws-{W3}", f"ws-{W2}", "tenant-t1"])
    configure_authorizer(Authorizer(policy, "policy"))
    ctx = replace(members(W1, W2, W3), visible_roots=frozenset({W3}))
    visible = await memory_visibility(ctx, _memory_settings())
    assert visible is not None
    assert visible[0] == ["tenant:t1", f"tenant:{ctx.tenant_id}:ws:{W3}"]
