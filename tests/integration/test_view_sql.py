"""The CEL of a view in SQL says what CEL says (CP-ADR-0080 amendment A, ``view_sql``).

Every expression below is translated, and on one set of instances — fields
missing, ``null``, amounts that are no number, strings with ``%`` and ``_``,
letters of either case — the instances its SQL selects are exactly those its
evaluation in Python (the profile ``cp/1``) gives ``true`` for. An expression
the translation does not say exactly is refused (:class:`Untranslatable`)
rather than said approximately.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from control_plane.application.queries.view_sql import Translator, Untranslatable
from control_plane.domain import view_query as vq
from control_plane.domain.cel_profile import ExpressionError
from control_plane.domain.views import ProcessShape, source_environment
from control_plane.infrastructure.db.models import ProcessInstance
from tests.integration.test_process_instances import _setup
from tests.integration.test_view_query import DATA, PROCESS, STAGES, _install, _template, seed

ROWS: list[dict[str, Any]] = [
    {},
    {"kind": "goods", "amount": 5, "urgent": True, "title": "Ab%c"},
    {"kind": "works", "amount": 5.0, "urgent": False, "title": "a_b"},
    {"kind": None, "amount": -1.5, "title": ""},
    {"kind": "draft", "amount": 60, "customer": "b", "price": {"amount": "100.50"}},
    {"customer": "B", "price": {"amount": "abc"}},
    {"customer": "Ä", "price": {"amount": " 7 "}},
    {"customer": "a", "price": {"amount": "1e3", "currency": "RUB"}},
    {"price": {}, "urgent": True},
    {"amount": 0, "title": "Ab"},
]
STAGE_OF = ["work", "work", "archive", None, "work", "archive", "work", None, "work", "archive"]
PARAMS = {"floor": {"type": "number"}, "who": {"type": "string"}}
GIVEN = {"floor": 4.5, "who": "b"}
EXPRESSIONS = [
    'data.kind == "goods"',
    'data.kind != "goods"',
    "data.kind == null",
    "data.kind != null",
    "has(data.kind)",
    "!has(data.kind)",
    "has(data.price)",
    "data.amount > 1.0",
    "data.amount >= 5.0",
    "!(data.amount > 1.0)",
    "data.amount > 1.0 || data.urgent",
    "data.amount > 1.0 && data.urgent == false",
    "data.urgent",
    "!data.urgent",
    "data.urgent == true",
    "decimal(data.price.amount) > 50.0",
    "decimal(data.price.amount) == 100.5",
    "decimal(data.price.amount) < 10.0",
    "!(decimal(data.price.amount) < 10.0)",
    'data.title.startsWith("Ab")',
    'data.title.contains("%")',
    'data.title.contains("_")',
    'data.title.endsWith("b")',
    'data.customer < "b"',
    'data.customer >= "B"',
    'data.kind in ["goods", "works"]',
    "data.amount in [5.0, 60.0]",
    'status == "running"',
    'status != "running"',
    'id != ""',
    'instance.key.startsWith("row-1")',
    "instance.version == 1",
    "stage.work.active",
    "stage.archive.active || stage.work.completed",
    "data.urgent ? data.amount > 0.0 : data.amount < 0.0",
    "data.amount + 1.0 > 6.0",
    "-data.amount < 0.0",
    "data.amount * 2.0 == 10.0",
    "data.amount - 1.0 >= 4.0",
    "param.floor < data.amount",
    "data.customer == param.who",
    "has(param.who) && data.kind == null",
    "data.kind == data.customer",
]
# Said otherwise in SQL than in CEL, or not at all: evaluated record by record instead.
REFUSED = [
    "size(data.kind) > 0",
    'data.title + "x" == "x"',
    "data.amount / 2.0 > 1.0",
    'timestamp("2026-10-01T00:00:00Z") < instance.clock',
    "slaState == 'breached'",
    "data.kind in [data.customer]",
    "int(data.amount) > 1",
    "decimal(data.price.amount) == data.amount",
]


@pytest.fixture
async def instances(client: httpx.AsyncClient, sync_engine: Engine) -> dict[str, Any]:
    s = await _setup(client)
    await _install(client, s["key"], s["admin"])
    template = await _template(client, s["key"])
    rows = [
        {"key": f"row-{n}", "data": data, "stage": STAGE_OF[n], "status": "running"}
        for n, data in enumerate(ROWS)
    ]
    rows[3]["status"] = "suspended"
    ids = seed(sync_engine, template, rows, start=datetime(2026, 9, 1, tzinfo=UTC))
    return {"ids": set(ids), "template": template}


def _environment() -> Any:
    return source_environment("process", ProcessShape(DATA, STAGES), PARAMS)


async def _selected(app: FastAPI, expression: str, ids: set[str]) -> tuple[set[str], set[str]]:
    """The ids SQL selects and the ids the evaluation gives ``true`` for."""
    program = _environment().compile(expression)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    predicate = Translator(DATA, STAGES, GIVEN, now).predicate(program)
    async with app.state.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ProcessInstance).where(
                        ProcessInstance.definition_key == PROCESS,
                        ProcessInstance.id.in_([uuid.UUID(i) for i in ids]),
                    )
                )
            ).all()
        )
        in_sql = {
            str(i)
            for i in await session.scalars(
                select(ProcessInstance.id).where(
                    ProcessInstance.id.in_([r.id for r in rows]), predicate
                )
            )
        }
    evaluated = set()
    for row in rows:
        values = vq.instance_values(
            instance_id=row.id,
            key=row.instance_key,
            version=row.definition_version,
            status=row.status,
            sla_state="none",
            data=row.data,
            state=row.state,
            started_at=row.started_at,
            stage_order=STAGES,
            now=now,
            param=GIVEN,
        )
        try:
            if program.evaluate(values).value is True:
                evaluated.add(str(row.id))
        except ExpressionError:
            pass
    return in_sql, evaluated


@pytest.mark.parametrize("expression", EXPRESSIONS)
async def test_the_sql_of_an_expression_selects_what_cel_evaluates_to_true(
    app: FastAPI, instances: dict[str, Any], expression: str
) -> None:
    in_sql, evaluated = await _selected(app, expression, instances["ids"])
    assert in_sql == evaluated


@pytest.mark.parametrize("expression", REFUSED)
def test_what_sql_would_say_otherwise_is_refused(expression: str) -> None:
    program = _environment().compile(expression)
    with pytest.raises(Untranslatable):
        Translator(DATA, STAGES, GIVEN, datetime.now(UTC)).predicate(program)


async def test_aggregates_in_sql_are_those_of_the_evaluation(
    app: FastAPI, instances: dict[str, Any]
) -> None:
    translator = Translator(DATA, STAGES, GIVEN, datetime.now(UTC))
    env = _environment()
    ids = [uuid.UUID(i) for i in instances["ids"]]
    total = translator.number(env.compile("decimal(data.price.amount)"))
    largest = translator.number(env.compile("data.amount"))
    async with app.state.session_factory() as session:
        found = (
            await session.execute(
                select(func.sum(total), func.max(largest), func.count()).where(
                    ProcessInstance.id.in_(ids)
                )
            )
        ).one()
    # "abc" and the missing amounts are no number: not summed, as an evaluation error.
    assert found[0] == Decimal("100.50") + Decimal("7") + Decimal("1e3")
    assert (found[1], found[2]) == (60, len(ROWS))
