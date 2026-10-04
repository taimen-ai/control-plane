"""How the daemon lists available Work: the ``typeKey`` filter and paging (CP-ADR-0056 Ж2)."""

import asyncio
from typing import Any

import pytest

from control_plane_agent import main as agent_main
from control_plane_agent.main import Agent, EchoAdapter
from control_plane_client import ControlPlaneClient, NotFoundError, PermissionDeniedError

RUNNABLE = "ok.skill"


def execution(skill: str) -> dict[str, Any]:
    return {"skill": skill, "version": "1"}


class FakeSkills:
    """What the daemon asks of a skill executor: can it run this skill?"""

    concurrency = 1
    capabilities = ()

    def __init__(self, unreadable: frozenset[str] = frozenset()) -> None:
        self.unreadable = unreadable
        self.described: list[str] = []

    async def describe(self, ref: str) -> dict[str, Any]:
        self.described.append(ref)
        name = ref.split("@", 1)[0]
        if name in self.unreadable:
            raise NotFoundError("not_found", "no such skill", status=404)
        return {"name": name}

    def can_execute(self, skill: dict[str, Any]) -> bool:
        return bool(skill["name"] == RUNNABLE)


class FakeClient:
    def __init__(
        self,
        types: list[dict[str, Any]],
        *,
        type_page: int = 100,
        pages: list[dict[str, Any]] | None = None,
        types_forbidden: bool = False,
    ) -> None:
        self.types = types
        self.type_page = type_page
        self.pages = pages or [{"items": [], "nextCursor": None}]
        self.types_forbidden = types_forbidden
        self.type_reads = 0
        self.listings: list[dict[str, Any]] = []

    async def list_task_types(self, **params: Any) -> dict[str, Any]:
        self.type_reads += 1
        if self.types_forbidden:
            raise PermissionDeniedError("forbidden", "task_types.read", status=403)
        start = int(params.get("cursor") or 0)
        end = start + self.type_page
        return {
            "items": self.types[start:end],
            "nextCursor": str(end) if end < len(self.types) else None,
        }

    async def list_available_work(self, **kwargs: Any) -> dict[str, Any]:
        self.listings.append(kwargs)
        return self.pages[min(len(self.listings), len(self.pages)) - 1]


def skills_agent(client: FakeClient, skills: FakeSkills | None = None, **kwargs: Any) -> Agent:
    agent = Agent(
        client,  # type: ignore[arg-type]
        None,
        skills=skills or FakeSkills(),  # type: ignore[arg-type]
        **kwargs,
    )
    agent.session_id = "session-1"
    return agent


async def test_skills_executor_lists_the_types_whose_skill_it_runs() -> None:
    client = FakeClient(
        [
            {"key": "doc", "execution": execution(RUNNABLE)},
            {"key": "doc", "execution": execution(RUNNABLE)},  # an older version
            {"key": "other", "execution": execution("other.skill")},
            {"key": "code", "execution": None},
        ]
    )
    assert await skills_agent(client)._listing_type_keys() == {"doc"}


async def test_type_keys_are_read_across_pages_of_task_types() -> None:
    types = [{"key": f"t{n}", "execution": execution(RUNNABLE)} for n in range(5)]
    client = FakeClient(types, type_page=2)
    assert await skills_agent(client)._listing_type_keys() == {f"t{n}" for n in range(5)}
    assert client.type_reads == 3


async def test_an_unreadable_skill_is_not_a_type_taken() -> None:
    client = FakeClient(
        [
            {"key": "doc", "execution": execution(RUNNABLE)},
            {"key": "gone", "execution": execution("gone.skill")},
        ]
    )
    agent = skills_agent(client, FakeSkills(unreadable=frozenset({"gone.skill"})))
    assert await agent._listing_type_keys() == {"doc"}


async def test_work_task_types_narrow_the_type_keys_further() -> None:
    client = FakeClient(
        [
            {"key": "doc", "execution": execution(RUNNABLE)},
            {"key": "memo", "execution": execution(RUNNABLE)},
        ]
    )
    agent = skills_agent(client, task_types=frozenset({"memo", "code"}))
    assert await agent._listing_type_keys() == {"memo"}


async def test_another_kind_lists_without_the_filter() -> None:
    client = FakeClient([{"key": "doc", "execution": execution(RUNNABLE)}])
    agent = Agent(client, EchoAdapter(), skills=FakeSkills())  # type: ignore[arg-type]
    assert await agent._listing_type_keys() is None
    assert client.type_reads == 0


async def test_without_task_types_read_the_listing_is_not_filtered() -> None:
    client = FakeClient([], types_forbidden=True)
    assert await skills_agent(client)._listing_type_keys() is None


async def test_more_types_than_one_listing_takes_are_not_filtered() -> None:
    types = [
        {"key": f"t{n}", "execution": execution(RUNNABLE)}
        for n in range(agent_main.TYPE_KEYS_MAX + 1)
    ]
    assert await skills_agent(FakeClient(types))._listing_type_keys() is None


async def test_more_task_types_than_pages_read_are_not_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_main, "SKILL_TYPES_PAGES", 2)
    types = [{"key": f"t{n}", "execution": execution(RUNNABLE)} for n in range(5)]
    assert await skills_agent(FakeClient(types, type_page=2))._listing_type_keys() is None


async def test_type_keys_are_read_again_only_after_the_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(agent_main.time, "monotonic", lambda: now[0])
    client = FakeClient([{"key": "doc", "execution": execution(RUNNABLE)}])
    agent = skills_agent(client)
    assert await agent._listing_type_keys() == {"doc"}
    client.types.append({"key": "memo", "execution": execution(RUNNABLE)})
    now[0] += agent_main.SKILL_TYPES_REFRESH_SECONDS - 1
    assert await agent._listing_type_keys() == {"doc"}
    assert client.type_reads == 1
    now[0] += 1
    assert await agent._listing_type_keys() == {"doc", "memo"}
    assert client.type_reads == 2


async def test_concurrent_reads_agree() -> None:
    client = FakeClient([{"key": "doc", "execution": execution(RUNNABLE)}])
    agent = skills_agent(client)
    first, second = await asyncio.gather(agent._listing_type_keys(), agent._listing_type_keys())
    assert first == second == {"doc"}


async def test_run_once_sends_the_type_keys_sorted() -> None:
    client = FakeClient(
        [
            {"key": "memo", "execution": execution(RUNNABLE)},
            {"key": "doc", "execution": execution(RUNNABLE)},
        ]
    )
    assert await skills_agent(client).run_once() is False
    assert [call["type_keys"] for call in client.listings] == [["doc", "memo"]]


async def test_paging_stops_at_the_cap() -> None:
    """A queue that never ends is not read to the end in one cycle."""
    foreign = {"items": [{"id": "x", "typeKey": "other"}], "nextCursor": "more"}
    client = FakeClient([], pages=[foreign])
    agent = Agent(
        client,  # type: ignore[arg-type]
        EchoAdapter(),
        task_types=frozenset({"mine"}),
    )
    agent.session_id = "session-1"
    assert await agent.run_once() is False
    assert len(client.listings) == agent_main.WORK_PAGES
    assert [call["cursor"] for call in client.listings] == [None] + ["more"] * (
        agent_main.WORK_PAGES - 1
    )


async def test_paging_stops_at_the_last_page() -> None:
    client = FakeClient(
        [],
        pages=[
            {"items": [{"id": "x", "typeKey": "other"}], "nextCursor": "2"},
            {"items": [], "nextCursor": None},
        ],
    )
    agent = Agent(client, EchoAdapter(), task_types=frozenset({"mine"}))  # type: ignore[arg-type]
    agent.session_id = "session-1"
    assert await agent.run_once() is False
    assert [call["cursor"] for call in client.listings] == [None, "2"]


async def test_client_refuses_an_empty_type_filter() -> None:
    """Sent as no parameter, an empty filter would ask for the whole queue."""
    async with ControlPlaneClient("http://testserver", "key") as client:
        with pytest.raises(ValueError, match="type_keys"):
            await client.list_available_work(type_keys=[])
