"""Relations and source fields of the entity list (CP-ADR-0060, amendment 2026-10-03).

``include.relations`` of ``POST /knowledge/entities:query`` is read through
Memory's typed traversal, one entity per traversal. Memory is
``tests.fake_graph_memory.FakeGraphMemory``: every typed body is validated
against the pinned ``ContextIn`` and ``typedRequest``, every entity page
against the pinned ``EntitiesQueryIn``; its answers are checked here against
the pinned ``typedRelations`` fields the core reads.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    KNOWLEDGE_RELATION_NAME,
    KnowledgeEntitiesPageOut,
    KnowledgeEntitiesQueryRequest,
)
from control_plane.application.context.graph import GraphScope
from control_plane.application.queries.knowledge_entities import (
    EntitiesQueryCall,
    RelationsInclude,
    entity_out,
    fetch_entities,
    relations_of,
)
from control_plane.config import Settings
from control_plane.domain.errors import DependencyUnavailableError, UpstreamError
from tests.fake_graph_memory import Edge, FakeGraphMemory, Node

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_graph_contract.json").read_text()
)
NS = "tenant:t1:ws:w1"
CLAIM = "POST /tasks/{}:claim"
SETTINGS = Settings(database_url="postgresql+psycopg://x/y")
# Local mode visibility: the secret ui_call (workspace:other) is hidden.
VISIBILITY = {"allowedScopes": ["workspace:w1", "principal:p1"]}


def _call(include: RelationsInclude | None, **overrides: Any) -> EntitiesQueryCall:
    values: dict[str, Any] = {
        "scope": GraphScope(namespace=NS, namespaces=[NS], visibility=VISIBILITY),
        "kinds": ["endpoint"],
        "limit": 100,
        "relations": include,
        **overrides,
    }
    return EntitiesQueryCall(**values)


async def _page(
    fake: FakeGraphMemory, include: RelationsInclude | None, **overrides: Any
) -> dict[str, Any]:
    return await fetch_entities(_call(include, **overrides), fake, SETTINGS)


def _relations(page: dict[str, Any], key: str) -> list[tuple[str, str, str, str]]:
    [item] = [i for i in page["items"] if i["key"] == key]
    return [(r["relation"], r["direction"], r["kind"], r["key"]) for r in item["relations"]]


CALLERS = [
    (
        "calls",
        "in",
        "client_method",
        "control-plane:control_plane_client.client.ControlPlaneClient.claim_task",
    ),
    ("calls", "in", "ui_call", "platform-web:src/api/tasks.ts:42"),
]
DEFINED = [("defined_in", "out", "source_file", "control-plane:src/control_plane/api/v1/claims.py")]


# --- include.relations ---------------------------------------------------------------


@pytest.mark.anyio
async def test_relations_in_out_and_both_with_the_other_end() -> None:
    names = ("calls", "defined_in")
    fake = FakeGraphMemory()
    both = await _page(fake, RelationsInclude(names=names))
    assert _relations(both, CLAIM) == CALLERS + DEFINED
    [claim] = [i for i in both["items"] if i["key"] == CLAIM]
    assert claim["relations"][-1] == {
        "relation": "defined_in",
        "direction": "out",
        "kind": "source_file",
        "key": "control-plane:src/control_plane/api/v1/claims.py",
        # The fake titles an untitled node with its key, as Memory does.
        "title": "control-plane:src/control_plane/api/v1/claims.py",
    }
    out = await _page(FakeGraphMemory(), RelationsInclude(names=names, direction="out"))
    assert _relations(out, CLAIM) == DEFINED
    into = await _page(FakeGraphMemory(), RelationsInclude(names=names, direction="in"))
    assert _relations(into, CLAIM) == CALLERS
    # An entity without such relations answers an empty list, not a missing field.
    assert _relations(both, "GET /runs/{}/checkpoints") == [
        ("calls", "in", "ui_call", "platform-web:src/api/runs.ts:17")
    ]
    KnowledgeEntitiesPageOut.model_validate(both)


@pytest.mark.anyio
async def test_one_typed_traversal_per_entity_from_it_by_the_pinned_contract() -> None:
    fake = FakeGraphMemory()
    as_of = datetime(2026, 9, 28, tzinfo=UTC)
    await _page(
        fake, RelationsInclude(names=("calls", "defined_in"), direction="in", limit=5), as_of=as_of
    )
    # FakeGraphMemory validated each body against ContextIn and typedRequest.
    bodies = sorted(fake.typed_requests, key=lambda b: b["anchors"][0]["value"])
    assert [b["anchors"] for b in bodies] == [
        [{"kind": "endpoint", "value": "GET /runs/{}/checkpoints"}],
        [{"kind": "endpoint", "value": CLAIM}],
    ]
    for body in bodies:
        assert body["traverse"] == [
            {"relation": "calls", "direction": "in", "depth": 1, "limit": 5},
            {"relation": "defined_in", "direction": "in", "depth": 1, "limit": 5},
        ]
        # The list's namespace, visibility and moment; no similarity search.
        assert body["scope"] == {"namespace": NS}
        assert body["allowedScopes"] == VISIBILITY["allowedScopes"]
        assert body["as_of"] == "2026-09-28T00:00:00+00:00"
        assert body["allow_semantic"] is False


@pytest.mark.anyio
async def test_a_relation_to_an_end_the_caller_cannot_see_is_not_answered() -> None:
    fake = FakeGraphMemory()
    page = await _page(fake, RelationsInclude(names=("calls",), direction="in"))
    keys = [key for *_, key in _relations(page, CLAIM)]
    assert "secret-app:src/api.ts:1" not in keys
    # Without the narrowing the same edge is there: the hiding is the visibility's.
    open_page = await _page(
        FakeGraphMemory(),
        RelationsInclude(names=("calls",), direction="in"),
        scope=GraphScope(namespace=NS, namespaces=[NS]),
    )
    assert "secret-app:src/api.ts:1" in [key for *_, key in _relations(open_page, CLAIM)]


def test_a_fact_whose_end_is_not_in_the_pack_is_dropped() -> None:
    pack = {
        "sections": [
            {"kind": "endpoint", "items": [{"natural_key": CLAIM, "kind": "endpoint"}]},
            {"kind": "adr", "items": [{"natural_key": "CP-0019", "kind": "adr", "title": "C"}]},
        ],
        "facts": [
            {"fact_id": "a", "relation": "governs", "subject": "CP-0019", "object": CLAIM},
            {"fact_id": "b", "relation": "calls", "subject": "hidden", "object": CLAIM},
            # Not a relation of this entity at all.
            {"fact_id": "c", "relation": "governs", "subject": "CP-0019", "object": "x"},
            "not a fact",
        ],
    }
    assert relations_of(pack, CLAIM, RelationsInclude(names=None)) == [
        {"relation": "governs", "direction": "in", "kind": "adr", "key": "CP-0019", "title": "C"}
    ]
    assert relations_of(pack, CLAIM, RelationsInclude(names=None, direction="out")) == []
    assert relations_of({}, CLAIM, RelationsInclude(names=None)) == []


@pytest.mark.anyio
async def test_limit_caps_the_relations_of_each_entity() -> None:
    fake = FakeGraphMemory()
    for i in range(5):
        key = f"platform-web:src/api/extra{i}.ts:1"
        fake.nodes[key] = Node(key, "ui_call")
        fake.edges.append(Edge(key, "calls", CLAIM, fact_id=f"f-extra-{i}"))
    page = await _page(fake, RelationsInclude(names=("calls", "defined_in"), limit=3))
    claim = _relations(page, CLAIM)
    # Sorted, then cut: the cap counts all relations of the entity together.
    assert len(claim) == 3
    assert claim == sorted(claim)
    # Each entity has its own cap: a busy neighbour takes nothing from it.
    assert len(_relations(page, "GET /runs/{}/checkpoints")) == 1
    # Memory's step limit is the same cap.
    assert {s["limit"] for b in fake.typed_requests for s in b["traverse"]} == {3}


@pytest.mark.anyio
async def test_star_reads_the_relations_of_the_namespace_catalog() -> None:
    fake = FakeGraphMemory()
    page = await _page(fake, RelationsInclude(names=None))
    assert fake.kind_requests == [NS]
    relations = CONTRACT["namespaceKinds"]["catalog"]["relations"]
    for body in fake.typed_requests:
        assert [s["relation"] for s in body["traverse"]] == relations
    assert _relations(page, CLAIM) == CALLERS + DEFINED


@pytest.mark.anyio
async def test_more_relations_than_a_traversal_takes_are_read_in_batches() -> None:
    fake = FakeGraphMemory()
    names = (*(f"rel_{i:02d}" for i in range(19)), "defined_in")
    page = await _page(fake, RelationsInclude(names=names, direction="out"), kinds=["endpoint"])
    per_entity: dict[str, list[int]] = {}
    for body in fake.typed_requests:
        per_entity.setdefault(body["anchors"][0]["value"], []).append(len(body["traverse"]))
    assert per_entity == {CLAIM: [10, 10], "GET /runs/{}/checkpoints": [10, 10]}
    assert _relations(page, CLAIM) == DEFINED


@pytest.mark.anyio
async def test_no_relation_names_in_the_catalog_or_no_entities_mean_no_traversal() -> None:
    fake = FakeGraphMemory()

    async def no_catalog(**_: Any) -> dict[str, Any]:
        return {"settings": {"packages": None}, "catalog": {"packages": []}}

    fake.namespace_kinds = no_catalog  # type: ignore[method-assign]
    page = await _page(fake, RelationsInclude(names=None))
    assert fake.typed_requests == []
    assert all(item["relations"] == [] for item in page["items"])

    empty = await _page(FakeGraphMemory(), RelationsInclude(names=("calls",)), kinds=["table"])
    assert empty["items"] == []


@pytest.mark.anyio
async def test_relation_reads_of_a_page_run_at_most_eight_at_once() -> None:
    fake = FakeGraphMemory()
    for i in range(30):
        fake.nodes[f"GET /x/{i}"] = Node(f"GET /x/{i}", "endpoint")
    typed = fake.typed_context
    running, peak = 0, 0

    async def slow(**kwargs: Any) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        try:
            return await typed(**kwargs)
        finally:
            running -= 1

    fake.typed_context = slow  # type: ignore[method-assign]
    page = await _page(fake, RelationsInclude(names=("calls",)))
    assert len(page["items"]) == 32
    assert len(fake.typed_requests) == 32
    assert 1 < peak <= 8


@pytest.mark.anyio
async def test_a_failed_relation_read_fails_the_page() -> None:
    with pytest.raises(UpstreamError) as raised:
        await _page(FakeGraphMemory(fail="typed"), RelationsInclude(names=("calls",)))
    assert raised.value.code == "memory_unavailable"
    with pytest.raises(UpstreamError):
        await _page(FakeGraphMemory(fail="kinds"), RelationsInclude(names=None))

    fake = FakeGraphMemory()

    async def hangs(**_: Any) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    fake.typed_context = hangs  # type: ignore[method-assign]
    hurried = Settings(database_url="postgresql+psycopg://x/y", context_timeout_seconds=0.05)
    with pytest.raises(DependencyUnavailableError) as late:
        await fetch_entities(_call(RelationsInclude(names=("calls",))), fake, hurried)
    assert late.value.code == "memory_timeout"


@pytest.mark.anyio
async def test_the_fake_typed_answer_carries_the_pinned_fields() -> None:
    fake = FakeGraphMemory()
    pack = await fake.typed_context(
        namespace=NS,
        namespaces=[NS],
        request={
            "anchors": [{"kind": "endpoint", "value": CLAIM}],
            "traverse": [{"relation": "calls", "direction": "both", "depth": 1, "limit": 20}],
        },
    )
    fact = Draft202012Validator(CONTRACT["typedRelations"]["fact"])
    item = Draft202012Validator(CONTRACT["typedRelations"]["sectionItem"])
    assert pack["facts"]
    for f in pack["facts"]:
        fact.validate(f)
    for section in pack["sections"]:
        for i in section["items"]:
            item.validate(i)


# --- the request ---------------------------------------------------------------------


def _request(include: Any, **fields: Any) -> KnowledgeEntitiesQueryRequest:
    return KnowledgeEntitiesQueryRequest.model_validate(
        {"workspaceId": "00000000-0000-0000-0000-000000000001", "kinds": ["x"], **fields}
        | ({"include": include} if include is not ... else {})
    )


def test_include_defaults_and_bounds() -> None:
    assert _request(...).include is None
    # null is the same as no include.
    assert _request(None).include is None
    include = _request({"relations": ["calls"]}).include
    assert include is not None
    assert (include.relations, include.direction, include.limit) == (["calls"], "both", 20)
    assert _request({"relations": "*", "direction": "in", "limit": 200}).include is not None
    assert _request({"relations": ["calls"]}, limit=100).limit == 100
    for bad in (
        {},
        {"relations": []},
        {"relations": "all"},
        {"relations": "calls"},
        {"relations": ["Calls"]},
        {"relations": [""]},
        {"relations": [1]},
        {"relations": None},
        {"relations": [f"r{i}" for i in range(21)]},
        {"relations": ["calls"], "direction": "up"},
        {"relations": ["calls"], "limit": 0},
        {"relations": ["calls"], "limit": 201},
        {"relations": ["calls"], "limit": "many"},
        {"relations": ["calls"], "depth": 2},
    ):
        with pytest.raises(ValidationError):
            _request(bad)
    # A page of relations is at most 100 entities: one traversal each.
    with pytest.raises(ValidationError):
        _request({"relations": ["calls"]}, limit=101)
    assert _request(..., limit=500).limit == 500


def test_relation_names_and_cap_follow_memorys_bounds() -> None:
    # RelationSpecIn.relation of Memory's pack schema.
    assert CONTRACT["typedRelations"]["relationName"]["pattern"] == KNOWLEDGE_RELATION_NAME
    typed_limit = CONTRACT["typedRequest"]["properties"]["traverse"]["items"]["properties"]["limit"]
    ours = KnowledgeEntitiesQueryRequest.model_json_schema(by_alias=True)["$defs"][
        "KnowledgeEntitiesInclude"
    ]["properties"]["limit"]
    assert ours["maximum"] == typed_limit["maximum"]
    assert ours["minimum"] == typed_limit["minimum"]


# --- source fields -------------------------------------------------------------------


def test_source_fields_of_an_entity_without_merged_sources() -> None:
    # A Memory before MEM-ADR-022 names one source at the top level.
    item = {
        "kind": "license",
        "key": "l1",
        "namespace": NS,
        "title": "L",
        "attributes": {},
        "source": "sheet:licenses",
        "scope": "s",
        "snapshot_id": "snap-1",
        "source_path": "licenses.xlsx#A2",
        "valid_from": "2026-09-01T00:00:00+00:00",
    }
    out = entity_out(item)
    assert out["sources"] == [
        {
            "source": "sheet:licenses",
            "sourcePath": "licenses.xlsx#A2",
            "snapshotId": "snap-1",
            "source_path": "licenses.xlsx#A2",
            "snapshot_id": "snap-1",
            "scope": "s",
        }
    ]
    assert out["validFrom"] == "2026-09-01T00:00:00+00:00"
    assert out["validTo"] is None
    # Transitional fields are passed only when Memory sent them.
    assert "valid_to" not in out


def test_source_fields_of_a_sparse_or_malformed_item() -> None:
    out = entity_out(
        {"kind": "k", "key": "a", "attributes": None, "sources": [None, "x", {"source": "s"}]}
    )
    assert out == {
        "kind": "k",
        "key": "a",
        "title": "",
        "attributes": {},
        "validFrom": None,
        "validTo": None,
        "sources": [{"source": "s", "sourcePath": "", "snapshotId": None}],
    }
    assert entity_out({"kind": "k", "key": "a", "sources": None})["sources"] == []
    assert entity_out({"kind": "k", "key": "a", "valid_from": ""})["validFrom"] is None
    KnowledgeEntitiesPageOut.model_validate({"items": [out], "nextCursor": None})


def test_openapi_names_the_contract_and_marks_the_transition() -> None:
    app = FastAPI()
    app.include_router(api_v1_router)
    schemas = app.openapi()["components"]["schemas"]
    entity = schemas["KnowledgeEntityOut"]["properties"]
    for name in ("validFrom", "validTo", "sources", "relations"):
        assert name in entity, name
        assert not entity[name].get("deprecated"), name
    for name in (
        "source",
        "source_path",
        "snapshot_id",
        "valid_from",
        "valid_to",
        "namespace",
        "scope",
    ):
        assert entity[name]["deprecated"] is True, name
    source = schemas["KnowledgeEntitySourceOut"]["properties"]
    assert {"source", "sourcePath", "snapshotId"} <= set(source)
    assert all(source[n]["deprecated"] for n in ("source_path", "snapshot_id", "scope"))
    relation = schemas["KnowledgeEntityRelationOut"]
    assert set(relation["properties"]) == {"relation", "direction", "kind", "key", "title"}
    assert relation["properties"]["direction"]["enum"] == ["out", "in"]
    request = schemas["KnowledgeEntitiesQueryRequest"]["properties"]
    assert "include" in request
    assert set(schemas["KnowledgeEntitiesInclude"]["properties"]) == {
        "relations",
        "direction",
        "limit",
    }


@pytest.mark.anyio
async def test_an_item_without_a_key_is_not_traversed_from() -> None:
    fake = FakeGraphMemory()
    listed = fake.query_entities

    async def blank_key(**kwargs: Any) -> dict[str, Any]:
        page = await listed(**kwargs)
        page["items"][0]["key"] = " "
        return page

    fake.query_entities = blank_key  # type: ignore[method-assign]
    page = await _page(fake, RelationsInclude(names=("calls",)))
    assert page["items"][0]["relations"] == []
    assert [b["anchors"][0]["value"] for b in fake.typed_requests] == [CLAIM]
