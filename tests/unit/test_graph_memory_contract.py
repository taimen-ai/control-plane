"""The core's graph reads against Memory's pinned contract (CP-ADR-0064).

``tests/fixtures/memory_graph_contract.json`` holds what memory-service
publishes for ``POST /api/memory/context/typed`` (``ContextIn`` from its
``app.openapi()`` plus the body rules of ``TypedContextRequest.from_payload``)
and the answers of its pack registry for the software-delivery pack. The
bodies here are produced by ``HttpContextProvider`` from the same requests
the task context pack and ``cp_recall`` build, captured on the wire.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import regex
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from control_plane.api.v1.schemas import (
    KnowledgeEntitiesPageOut,
    KnowledgeEntitiesQueryRequest,
    MemoryWhereCondition,
)
from control_plane.application.context import graph
from control_plane.application.queries.knowledge_entities import (
    EntitiesQueryCall,
    entities_request,
    fetch_entities,
)
from control_plane.application.queries.recall import (
    RecallCall,
    fetch_process_recall,
    fetch_recall,
    process_recall_call,
)
from control_plane.config import Settings
from control_plane.domain.context_schema import (
    anchor_candidates,
    extract_identifiers,
    parse_context_schema,
)
from control_plane.domain.errors import UpstreamError
from control_plane.domain.errors import ValidationError as DomainValidationError
from control_plane.infrastructure.context_provider.base import ContextProviderError
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from tests.fake_graph_memory import FakeGraphMemory

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_graph_contract.json").read_text()
)
NS = "tenant:t1:ws:w1"


def _provider(handler: Any) -> HttpContextProvider:
    return HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )


async def _capture(call: str, answer: Any = None, **kwargs: Any) -> tuple[httpx.Request, Any]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer if answer is not None else {})

    provider = _provider(handler)
    try:
        result = await getattr(provider, call)(**kwargs)
    finally:
        await provider.aclose()
    [request] = seen
    return request, result


@pytest.mark.anyio
async def test_typed_body_is_memorys_context_in() -> None:
    schema = parse_context_schema(
        {
            "anchors": [{"from": "description", "kinds": ["endpoint", "adr"]}],
            "traverse": [{"relation": "calls", "direction": "in"}],
        }
    )
    assert schema is not None
    patterns = {
        spec["kind"]: tuple(regex.compile(p) for p in spec.get("idPatterns") or [])
        for spec in CONTRACT["packages"]["software-delivery@1"]["kinds"]
    }
    anchors = anchor_candidates(
        schema, {"description": "POST /tasks/{task_id}:claim, CP-ADR-0058"}, patterns
    )
    request, _ = await _capture(
        "typed_context",
        namespace="tenant:t1",
        namespaces=["tenant:t1", NS],
        request={
            "anchors": [a.to_request() for a in anchors],
            "traverse": [s.to_request() for s in schema.traverse],
            "as_of": "2026-09-25T10:00:00+00:00",
            "allow_semantic": False,
            "allowedScopes": ["workspace:w1", "principal:p1"],
        },
    )
    path = "/api/memory/context/typed"
    assert request.method == "POST" and request.url.path == path
    assert CONTRACT["paths"][path]["post"]["requestBody"] == "ContextIn"
    body = json.loads(request.content)
    Draft202012Validator(CONTRACT["schemas"]["ContextIn"]).validate(body)
    Draft202012Validator(CONTRACT["typedRequest"]).validate(body)
    assert body["scope"] == {"namespace": "tenant:t1", "namespaces": ["tenant:t1", NS]}
    # Sent as written, once: Memory normalizes template parameters itself.
    assert body["anchors"][0] == {"kind": "endpoint", "value": "POST /tasks/{task_id}:claim"}
    assert {"kind": "endpoint", "value": "POST /tasks/{}:claim"} not in body["anchors"]
    # One namespace keeps the plain shape.
    single, _ = await _capture(
        "typed_context", namespace=NS, namespaces=[NS], request={"anchors": ["x"]}
    )
    assert json.loads(single.content)["scope"] == {"namespace": NS}


@pytest.mark.anyio
async def test_registry_reads_are_the_published_routes() -> None:
    kinds, answer = await _capture(
        "namespace_kinds", answer=CONTRACT["namespaceKinds"], namespace=NS
    )
    assert kinds.method == "GET" and kinds.content == b""
    assert kinds.url.raw_path.decode() == "/api/memory/namespaces/tenant%3At1%3Aws%3Aw1/kinds"
    assert CONTRACT["paths"]["/api/memory/namespaces/{namespace}/kinds"]["get"]["parameters"] == [
        "namespace"
    ]
    assert answer["catalog"]["packages"] == ["software-delivery@1"]

    package, _ = await _capture(
        "get_package",
        answer=CONTRACT["packages"]["software-delivery@1"],
        name="software-delivery",
        version="1",
    )
    assert package.method == "GET" and package.url.path == "/api/memory/packages/software-delivery"
    assert dict(package.url.params) == {"version": "1"}
    assert set(dict(package.url.params)) <= set(
        CONTRACT["paths"]["/api/memory/packages/{name}"]["get"]["parameters"]
    )


@pytest.mark.anyio
async def test_patterns_come_from_the_enabled_packs_and_are_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/kinds"):
            return httpx.Response(200, json=CONTRACT["namespaceKinds"])
        return httpx.Response(200, json=CONTRACT["packages"]["software-delivery@1"])

    graph._pack_patterns.clear()
    provider = _provider(handler)
    try:
        first = await graph.kind_patterns(provider, [NS])
        second = await graph.kind_patterns(provider, [NS])
    finally:
        await provider.aclose()
    # Only kinds that declare idPatterns can extract anything.
    assert set(first.patterns) == {"source_file", "endpoint", "event", "adr"}
    assert first == second
    # An immutable pack version is fetched once; the namespace setting each time.
    assert calls.count("/api/memory/packages/software-delivery") == 1
    assert calls.count(f"/api/memory/namespaces/{NS}/kinds") == 2


@pytest.mark.anyio
async def test_kind_aliases_of_a_pack_select_the_canonical_patterns() -> None:
    """``kindAliases`` (KindSpec.to_dict of memory-service) name a kind too: a
    profile saying ``route`` extracts with the patterns of ``endpoint``."""
    package = copy.deepcopy(CONTRACT["packages"]["software-delivery@1"])
    for spec in package["kinds"]:
        if spec["kind"] == "endpoint":
            spec["kindAliases"] = ["route"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/kinds"):
            return httpx.Response(200, json=CONTRACT["namespaceKinds"])
        return httpx.Response(200, json=package)

    graph._pack_patterns.clear()
    provider = _provider(handler)
    try:
        catalog = await graph.kind_patterns(provider, [NS])
    finally:
        await provider.aclose()
        graph._pack_patterns.clear()
    assert catalog.aliases == {"route": "endpoint"}
    found = extract_identifiers(
        "Fix POST /tasks/{task_id}:claim", catalog.patterns, ["route"], aliases=catalog.aliases
    )
    assert found == [("endpoint", "POST /tasks/{task_id}:claim")]


@pytest.mark.anyio
async def test_unreadable_catalog_is_a_warning_and_a_bad_pack_is_an_error() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "no"})

    provider = _provider(denied)
    warnings: list[str] = []
    try:
        assert await graph.kind_patterns(provider, [NS], warnings=warnings) == graph.KindPatterns()
    finally:
        await provider.aclose()
    assert warnings == ["kind catalog unavailable for a namespace (403)"]

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, 2]")

    provider = _provider(garbage)
    try:
        with pytest.raises(ContextProviderError) as caught:
            await provider.typed_context(namespace=NS, namespaces=[NS], request={"anchors": ["x"]})
    finally:
        await provider.aclose()
    assert caught.value.retryable is False


def test_pack_is_cut_to_the_budget_in_render_order() -> None:
    pack = {
        "sections": [
            {"kind": "endpoint", "items": [{"natural_key": "E" * 40} for _ in range(3)]},
            {"kind": "adr", "items": [{"natural_key": "A" * 40}]},
        ],
        "facts": [{"subject": "s", "relation": "calls", "object": "o"}],
        "used": {"entities": [1, 2, 3, 4], "facts": ["f"], "snapshots": []},
        "unresolved": [{"value": "x"}],
    }
    # 40 characters and the line overhead: two entities fit in 30 tokens.
    cut = graph.within_budget(pack, 30)
    assert [len(s["items"]) for s in cut["sections"]] == [2]
    assert cut["facts"] == []
    assert cut["omitted"] == {"entities": 2, "facts": 1}
    assert cut["used"] == pack["used"] and cut["unresolved"] == pack["unresolved"]
    whole = graph.within_budget(pack, 4000)
    assert "omitted" not in whole and whole["sections"] == pack["sections"]


@pytest.mark.anyio
async def test_recall_where_goes_to_memory_as_it_is_in_both_typed_calls() -> None:
    # CP-ADR-0076, amendment 2026-09-28 (K011): the explicit read and the read by
    # similarity both carry the computed where, unchanged, as memory's K006 takes it.
    where = [
        {"attr": "okpd2", "op": "prefix", "value": "62.01"},
        {"attr": "validUntil", "op": "gte", "value": "2026-05-04T09:00:00Z"},
        {"attr": "status", "op": "in", "value": ["active", "draft"]},
        {"attr": "blocked", "op": "exists", "value": False},
        {"attr": "okpd2", "op": "exists"},
    ]
    call = process_recall_call(
        {
            "anchors": [{"kind": "company", "key": "7700000000"}],
            "traverse": [{"relation": "offers", "direction": "out"}],
            "query": "software",
            "where": where,
            "asOf": "2026-03-02T09:00:00Z",
        },
        graph.GraphScope(namespace="tenant:t1", namespaces=["tenant:t1", NS]),
    )
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/memory/context/typed"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    provider = _provider(handler)
    settings = Settings(database_url="postgresql+psycopg://x/y")
    try:
        await fetch_process_recall(call, provider, settings)
    finally:
        await provider.aclose()
    explicit, semantic = seen
    assert explicit["allow_semantic"] is False and semantic["allow_semantic"] is True
    for body in (explicit, semantic):
        Draft202012Validator(CONTRACT["typedRequest"]).validate(body)
        assert body["where"] == where
    # A step without where sends none: memory's answer stays unfiltered as before.
    call.where = []
    seen.clear()
    provider = _provider(handler)
    try:
        await fetch_process_recall(call, provider, settings)
    finally:
        await provider.aclose()
    assert all("where" not in body for body in seen)


@pytest.mark.parametrize(
    "condition",
    [
        {"attr": "okpd2", "op": "prefix", "value": ".62"},
        {"attr": "okpd2", "op": "prefix", "value": 62},
        {"attr": "status", "op": "in", "value": "active"},
        {"attr": "status", "op": "in", "value": []},
        {"attr": "validUntil", "op": "gte", "value": "soon"},
        {"attr": "blocked", "op": "exists", "value": "no"},
        {"attr": "a.b", "op": "eq", "value": 1},
        {"attr": "kind", "op": "like", "value": "x"},
        {"attr": "kind", "op": "eq"},
        {"attr": "kind", "op": "eq", "value": None},
        {"attr": "kind", "op": "eq", "value": 1, "negate": True},
    ],
)
def test_the_pinned_where_refuses_what_memory_rejects(condition: dict[str, Any]) -> None:
    body = {"anchors": ["x"], "scope": {"namespace": NS}, "where": [condition]}
    assert list(Draft202012Validator(CONTRACT["typedRequest"]).iter_errors(body))


WHERE_CONDITIONS = [
    {"attr": "okpd2", "op": "prefix", "value": "62.01"},
    {"attr": "validUntil", "op": "gte", "value": "2026-05-04T09:00:00Z"},
    {"attr": "validUntil", "op": "lte", "value": "2026-06-01"},
    {"attr": "price", "op": "lte", "value": 1000.5},
    {"attr": "status", "op": "in", "value": ["active", "draft", 3, True]},
    {"attr": "status", "op": "eq", "value": "active"},
    {"attr": "rank", "op": "eq", "value": 2},
    {"attr": "blocked", "op": "exists", "value": False},
    {"attr": "okpd2", "op": "exists"},
    {"attr": "okpd2", "op": "prefix", "value": ".62"},
    {"attr": "okpd2", "op": "prefix", "value": "62..01"},
    {"attr": "okpd2", "op": "prefix", "value": 62},
    {"attr": "status", "op": "in", "value": "active"},
    {"attr": "status", "op": "in", "value": []},
    {"attr": "status", "op": "in", "value": ["x"] * 101},
    {"attr": "status", "op": "in", "value": [["nested"]]},
    {"attr": "validUntil", "op": "gte", "value": "soon"},
    {"attr": "validUntil", "op": "gte", "value": True},
    {"attr": "blocked", "op": "exists", "value": "no"},
    {"attr": "a.b", "op": "eq", "value": 1},
    {"attr": "kind", "op": "like", "value": "x"},
    {"attr": "kind", "op": "eq"},
    {"attr": "kind", "op": "eq", "value": None},
    {"attr": "kind", "op": "eq", "value": {"a": 1}},
    {"attr": "kind", "op": "eq", "value": 1, "negate": True},
]


@pytest.mark.parametrize("condition", WHERE_CONDITIONS)
def test_the_route_accepts_the_where_memory_accepts(condition: dict[str, Any]) -> None:
    # POST /context/recall (CP-ADR-0064, amendment 2026-09-28): the request model
    # refuses exactly what the pinned typed contract refuses, and what it takes
    # reaches memory unchanged.
    body = {"anchors": ["x"], "scope": {"namespace": NS}, "where": [condition]}
    memory_takes = not list(Draft202012Validator(CONTRACT["typedRequest"]).iter_errors(body))
    try:
        parsed = MemoryWhereCondition.model_validate(condition)
    except ValidationError:
        assert not memory_takes
    else:
        assert memory_takes
        assert parsed.to_memory() == condition


@pytest.mark.anyio
async def test_route_recall_sends_where_as_it_is() -> None:
    where = WHERE_CONDITIONS[:9]
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    settings = Settings(database_url="postgresql+psycopg://x/y")
    for wanted in (where, []):
        provider = _provider(handler)
        call = RecallCall(
            scope=graph.GraphScope(namespace=NS, namespaces=[NS]), anchor="x", where=wanted
        )
        try:
            await fetch_recall(call, provider, settings)
        finally:
            await provider.aclose()
    with_where, without = seen
    Draft202012Validator(CONTRACT["typedRequest"]).validate(with_where)
    assert with_where["where"] == where
    assert "where" not in without


# --- the entity list (CP-ADR-0060, K031) ---------------------------------------------

ENTITIES_PAGE = {
    "items": [
        {
            "kind": "license",
            "key": "license:1",
            "namespace": NS,
            "title": "License No. 1",
            "attributes": {"validUntil": "2026-12-01", "okpd2": ["62.01.11"]},
            "source": "sheet:licenses",
            "scope": "licenses",
            "snapshot_id": "snap-1",
            "source_path": "licenses.xlsx#A2",
            "valid_from": "2026-09-01T00:00:00+00:00",
            "valid_to": None,
            "sources": [
                {
                    "source": "sheet:licenses",
                    "scope": "licenses",
                    "snapshot_id": "snap-1",
                    "source_path": "licenses.xlsx#A2",
                },
                {
                    "source": "registry:fsb",
                    "scope": "licenses",
                    "snapshot_id": "snap-7",
                    "source_path": "https://registry.example/licenses/1",
                },
            ],
        }
    ],
    "nextCursor": "opaque",
    "as_of": "2026-09-28T00:00:00+00:00",
    "namespaces": [NS],
    "stats": {"scanned": 1},
}


def _entities_call(**overrides: Any) -> EntitiesQueryCall:
    values: dict[str, Any] = {
        "scope": graph.GraphScope(
            namespace=NS,
            namespaces=[NS],
            visibility={"allowedNamespaces": [NS], "allowedScopes": ["workspace:w1"]},
        ),
        "kinds": ["license"],
        "limit": 50,
        **overrides,
    }
    return EntitiesQueryCall(**values)


@pytest.mark.anyio
async def test_entities_body_is_memorys_entities_query_in() -> None:
    where = WHERE_CONDITIONS[:9]
    call = _entities_call(
        where=where, as_of=datetime(2026, 9, 28, tzinfo=UTC), cursor="opaque-cursor"
    )
    request, _ = await _capture(
        "query_entities",
        answer=ENTITIES_PAGE,
        namespaces=call.scope.namespaces,
        request=entities_request(call),
    )
    path = "/api/memory/entities:query"
    assert request.method == "POST" and request.url.path == path
    assert CONTRACT["paths"][path]["post"]["requestBody"] == "EntitiesQueryIn"
    body = json.loads(request.content)
    Draft202012Validator(CONTRACT["schemas"]["EntitiesQueryIn"]).validate(body)
    Draft202012Validator(CONTRACT["typedRequest"]["properties"]["where"]).validate(body["where"])
    # Memory reads the namespaces from ``namespaces`` or ``scope``, never both.
    assert "scope" not in body
    assert body == {
        "kinds": ["license"],
        "limit": 50,
        "where": where,
        "asOf": "2026-09-28T00:00:00+00:00",
        "cursor": "opaque-cursor",
        "namespaces": [NS],
        "allowedNamespaces": [NS],
        "allowedScopes": ["workspace:w1"],
    }
    # Unset fields stay off the wire: memory's defaults apply.
    plain, _ = await _capture(
        "query_entities",
        answer=ENTITIES_PAGE,
        namespaces=[NS],
        request=entities_request(_entities_call(scope=graph.GraphScope(NS, [NS]))),
    )
    assert json.loads(plain.content) == {"kinds": ["license"], "limit": 50, "namespaces": [NS]}


def test_entities_request_bounds_match_memory() -> None:
    memory = CONTRACT["schemas"]["EntitiesQueryIn"]["properties"]
    ours = KnowledgeEntitiesQueryRequest.model_json_schema(by_alias=True)["properties"]
    assert ours["kinds"]["items"]["pattern"] == memory["kinds"]["items"]["pattern"]
    for bound in ("minItems", "maxItems"):
        assert ours["kinds"][bound] == memory["kinds"][bound], bound
    assert ours["limit"]["maximum"] == memory["limit"]["maximum"]
    assert ours["limit"]["minimum"] == memory["limit"]["minimum"]
    assert ours["limit"]["default"] == memory["limit"]["default"]
    [memory_where] = [b for b in memory["where"]["anyOf"] if b.get("type") == "array"]
    assert ours["where"]["maxItems"] == memory_where["maxItems"]
    # The request carries no namespace, scope or visibility: the core computes them.
    assert not set(ours) & {"namespaces", "scope", "allowedNamespaces", "allowedScopes"}


@pytest.mark.anyio
async def test_a_page_of_memory_is_the_answer_items_and_cursor() -> None:
    Draft202012Validator(CONTRACT["entitiesQuery"]["EntitiesQueryResult"]).validate(ENTITIES_PAGE)
    settings = Settings(database_url="postgresql+psycopg://x/y")
    provider = _provider(lambda _: httpx.Response(200, json=ENTITIES_PAGE))
    try:
        page = await fetch_entities(
            _entities_call(as_of=datetime(2026, 9, 28, tzinfo=UTC)), provider, settings
        )
    finally:
        await provider.aclose()
    [memory_item] = ENTITIES_PAGE["items"]
    transitional = {
        name: memory_item[name]
        for name in (
            "source",
            "source_path",
            "snapshot_id",
            "valid_from",
            "valid_to",
            "namespace",
            "scope",
        )
    }
    assert page == {
        "items": [
            {
                "kind": "license",
                "key": "license:1",
                "title": "License No. 1",
                "attributes": {"validUntil": "2026-12-01", "okpd2": ["62.01.11"]},
                # The contract (CP-ADR-0060, amendment 2026-10-03).
                "validFrom": "2026-09-01T00:00:00+00:00",
                "validTo": None,
                "sources": [
                    {
                        "source": "sheet:licenses",
                        "sourcePath": "licenses.xlsx#A2",
                        "snapshotId": "snap-1",
                        "source_path": "licenses.xlsx#A2",
                        "snapshot_id": "snap-1",
                        "scope": "licenses",
                    },
                    {
                        "source": "registry:fsb",
                        "sourcePath": "https://registry.example/licenses/1",
                        "snapshotId": "snap-7",
                        "source_path": "https://registry.example/licenses/1",
                        "snapshot_id": "snap-7",
                        "scope": "licenses",
                    },
                ],
                # Memory's fields as they were, for the transition.
                **transitional,
            }
        ],
        "nextCursor": "opaque",
        "asOf": "2026-09-28T00:00:00+00:00",
    }
    # No relations unless include asked for them.
    assert "relations" not in page["items"][0]
    KnowledgeEntitiesPageOut.model_validate(page)


@pytest.mark.anyio
async def test_the_fake_entity_list_answers_by_the_pinned_result() -> None:
    fake = FakeGraphMemory()
    page = await fake.query_entities(
        namespaces=[NS], request={"kinds": ["endpoint", "adr"], "limit": 2}
    )
    Draft202012Validator(CONTRACT["entitiesQuery"]["EntitiesQueryResult"]).validate(page)
    assert [i["kind"] for i in page["items"]] == ["adr", "endpoint"]
    assert page["nextCursor"] is not None


@pytest.mark.anyio
async def test_memory_refusals_of_the_entity_list_map_by_meaning() -> None:
    settings = Settings(database_url="postgresql+psycopg://x/y")
    for status, error, code in (
        (400, DomainValidationError, "entities_query_invalid"),
        (503, UpstreamError, "memory_unavailable"),
        (403, UpstreamError, "memory_unavailable"),
    ):
        provider = _provider(lambda _, s=status: httpx.Response(s, json={"detail": "no"}))
        try:
            with pytest.raises(error) as raised:
                await fetch_entities(_entities_call(), provider, settings)
        finally:
            await provider.aclose()
        assert raised.value.code == code
        assert raised.value.details["memoryStatus"] == status
