"""``POST /views/{key}:query``: the data one block of a view draws (CP-ADR-0080 amendment A).

TAI-ADR-0066 p.4, stage 2, a view of a ``process``: of its instances
(``{process, filter?}``) or of one (``{process, instance: param.<name>}``).
A view of tasks or knowledge (stage 6) is answered by :mod:`.view_records`.
The console names the block by its index in the layout and passes values,
never expressions: the CEL is the package's.

- **Who sees what** — the view as ``GET /views/{key}`` shows it (the roles of
  its audience, the right to read its process; anything else is the 404 of a
  missing view), and of its process the instances in the workspaces where the
  caller holds ``processes.read`` — what ``GET /process-instances`` lists. An
  instance the caller may not read, or of another process, is the 404 of a
  missing one. A view narrows its source, never widens it: ``source.filter``
  holds for every block.
- **What is answered** — the values the block shows by their keys (the keys of
  ``GET /views/{key}``) in the formats of the view, never the raw ``data``: a
  field the view does not show does not leave the core.
- **How fast** — the source filter, the filters and sorts of the request and
  the aggregates are one SQL query where they translate exactly
  (:mod:`.view_sql`); an expression that does not is evaluated record by
  record, with the same result.

The ``related`` block reads Memory: its answer is prepared in the transaction
and read after it (:class:`Related`), as every read of Memory is.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Integer,
    Numeric,
    Text,
    and_,
    any_,
    cast,
    false,
    func,
    literal,
    null,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from control_plane.application.authorization import (
    AuthContext,
    WorkspaceNotVisible,
    permits,
    permits_task,
    visible_objects,
)
from control_plane.application.commands.artifacts import artifact_resource
from control_plane.application.commands.process_definitions import process_scope
from control_plane.application.commands.process_instances import (
    instance_sla,
    journal_entries,
    latest_definition,
    open_elements,
)
from control_plane.application.common import decode_cursor, encode_cursor, utcnow
from control_plane.application.queries import view_sql
from control_plane.application.queries.knowledge_entities import (
    EntitiesQueryCall,
    RelationsInclude,
    prepare_entities_query,
    relations_from,
)
from control_plane.application.queries.package_settings import package_scope, snapshot
from control_plane.application.queries.view_sql import Translator, Untranslatable
from control_plane.application.queries.views import ViewRead, get_view
from control_plane.application.visibility import artifact_condition
from control_plane.config import Settings
from control_plane.domain import view_query as vq
from control_plane.domain.cel_profile import Environment, ExpressionError, Program
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.views import (
    INSTANCE_STATUSES,
    ProcessShape,
    choose_locale,
    source_environment,
    split_aggregate,
)
from control_plane.infrastructure.context_provider import GraphProvider
from control_plane.infrastructure.db.models import (
    Artifact,
    PackageDictionary,
    ProcessDefinition,
    ProcessInstance,
    ProcessInstanceEvent,
    Task,
)

# Instances read per round when an expression is evaluated record by record.
BATCH = 500
# Instances one request may evaluate record by record (CP-ADR-0080 A6): past it
# the view costs more than a block may, and the request is refused.
MAX_EVALUATED = 20_000
# Compiled expressions by their environment: the same view asked again compiles nothing.
_PROGRAMS: dict[tuple[int, str], tuple[Environment, Program]] = {}
_MAX_PROGRAMS = 2048
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FIELDS_PREFIX = "fields"


@dataclass(frozen=True)
class ViewQuery:
    """The body of ``POST /views/{key}:query``."""

    block: int
    params: Mapping[str, Any] | None = None
    filter: Sequence[Mapping[str, Any]] | None = None
    sort: Sequence[Mapping[str, Any]] | None = None
    limit: int | None = None
    cursor: str | None = None
    # A view of knowledge: the workspace whose tree's knowledge it reads (stage 6).
    workspace_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Related:
    """The read of Memory a ``related`` block waits for: done after the transaction."""

    call: EntitiesQueryCall
    kind: str
    key: str


# A read of Memory an answer is computed from after the transaction (a view of knowledge):
# called with the provider, the settings and the trace id of the request.
MemoryRead = Callable[[GraphProvider, Settings, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Answer:
    """The data of the block, or the read of Memory that gives it."""

    body: dict[str, Any] | None
    related: Related | None = None
    memory: MemoryRead | None = None


# --- the query and what it knows ---------------------------------------------------------------


@dataclass
class _Query:
    db: AsyncSession
    ctx: AuthContext
    read: ViewRead
    block: vq.QueryBlock
    params: dict[str, Any]
    locale: str
    default_locale: str
    messages: Mapping[str, Mapping[str, str]]
    process: str
    data_schema: Mapping[str, Any] | None
    stages: tuple[str, ...]
    stage_titles: Mapping[str, str]
    env: Environment
    now: datetime
    # The effective settings of the view's package, read once a query (CP-ADR-0081 §6).
    settings: Mapping[str, Any] | None = None
    _visible: dict[uuid.UUID, bool] = field(default_factory=dict)
    _evaluated: int = 0

    @property
    def form(self) -> Mapping[str, Any]:
        return self.read.revision.spec

    @property
    def package(self) -> str:
        return self.read.revision.package_key or ""

    def evaluating(self, count: int) -> None:
        """Account ``count`` instances read to be evaluated record by record."""
        self._evaluated += count
        if self._evaluated > MAX_EVALUATED:
            raise ConflictError(
                "view_too_costly",
                f"view {self.read.view.key!r} evaluates more than {MAX_EVALUATED} instances"
                " record by record: write its source.filter and values in the part of CEL"
                " the core turns into SQL",
                details={"key": self.read.view.key, "limit": MAX_EVALUATED},
            )

    # -- expressions --

    def compile(self, text: str) -> Program | None:
        """A compiled expression; ``None`` when it no longer compiles against the process."""
        return compiled(self.env, text)

    def compile_strict(self, text: str, what: str) -> Program:
        program = self.compile(text)
        if program is None:
            raise ConflictError(
                "view_stale",
                f"the {what} of view {self.read.view.key!r} no longer compiles against process"
                f" {self.process!r}: apply its package again",
                details={"key": self.read.view.key},
            )
        return program

    def translator(self) -> Translator:
        return Translator(
            self.data_schema, self.stages, self.params, self.now, settings=self.settings or {}
        )

    def values(self, instance: ProcessInstance, *, sla: bool = True) -> "_Record":
        found = vq.instance_values(
            instance_id=instance.id,
            key=instance.instance_key,
            version=instance.definition_version,
            status=instance.status,
            sla_state=instance_sla(instance)[1] if sla else "none",
            data=instance.data,
            state=instance.state,
            started_at=instance.started_at,
            stage_order=self.stages,
            now=self.now,
            param=self.params,
            settings=self.settings,
        )
        return _Record(found, self.env)

    # -- strings --

    def text(self, key: str) -> str | None:
        found = vq.text_of(key, self.messages, self.locale, self.default_locale)
        if found is None:
            found = vq.text_of(
                key, self.form.get("messages") or {}, self.locale, self.default_locale
            )
        return found

    def label(self, name: str, value: Any) -> str:
        """The text of a value of a field: ``<package>.fields.<name>.<value>``, else the value."""
        if value is None:
            return ""
        word = ("true" if value else "false") if isinstance(value, bool) else str(value)
        prefix = f"{self.package}." if self.package else ""
        found = self.text(f"{prefix}{_FIELDS_PREFIX}.{name}.{word}")
        if found is not None:
            return found
        if name == "stage" and word in self.stage_titles:
            return self.stage_titles[word]
        return word

    def how(
        self,
        instance: ProcessInstance | None,
        currency: str | None = None,
        name: str = "status",
    ) -> vq.Shown:
        """What a value of a field ``name`` of ``instance`` is shown with."""
        status = instance.status if instance is not None else "running"
        return vq.Shown(
            label=lambda v: self.label(name, v),
            category=vq.status_category(status),
            currency=currency,
        )

    # -- rights --

    async def visible(self, workspace_id: uuid.UUID | None) -> bool:
        if workspace_id is None:
            return True
        if workspace_id not in self._visible:
            self._visible[workspace_id] = await permits(
                self.ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id)
            )
        return self._visible[workspace_id]


def compiled(env: Environment, text: str) -> Program | None:
    """``text`` compiled in ``env``, once per environment; ``None`` when it does not compile."""
    cached = _PROGRAMS.get((id(env), text))
    if cached is not None and cached[0] is env:
        return cached[1]
    try:
        program = env.compile(text)
    except ExpressionError:
        return None
    if len(_PROGRAMS) >= _MAX_PROGRAMS:
        del _PROGRAMS[next(iter(_PROGRAMS))]
    _PROGRAMS[(id(env), text)] = (env, program)
    return program


class _Record(dict[str, Any]):
    """The values of one instance, laid out for CEL once for every expression read over it."""

    def __init__(self, values: Mapping[str, Any], env: Environment) -> None:
        super().__init__(values)
        self.env = env
        self.built: dict[str, Any] | None = None

    def activation(self) -> dict[str, Any]:
        if self.built is None:
            self.built = self.env.activation(self)
        return self.built


def _run(program: Program, values: Mapping[str, Any]) -> Any:
    activation = values.activation() if isinstance(values, _Record) else None
    return program.evaluate(values, activation=activation).value


async def _dictionaries(
    db: AsyncSession, tenant_id: uuid.UUID, package: str | None
) -> Mapping[str, Mapping[str, str]]:
    """The dictionaries of the package of the view, its latest revision."""
    if not package:
        return {}
    messages = await db.scalar(
        select(PackageDictionary.messages)
        .where(PackageDictionary.tenant_id == tenant_id, PackageDictionary.package_key == package)
        .order_by(PackageDictionary.revision.desc())
        .limit(1)
    )
    return messages or {}


def _stage_titles(spec: Mapping[str, Any]) -> tuple[tuple[str, ...], dict[str, str]]:
    stages = [s for s in spec.get("stages") or () if isinstance(s, Mapping) and "id" in s]
    order = tuple(str(s["id"]) for s in stages)
    return order, {str(s["id"]): str(s.get("displayName") or s["id"]) for s in stages}


async def prepare(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    key: str,
    query: ViewQuery,
    *,
    locale: str | None,
) -> Answer:
    """The data of block ``query.block`` of view ``key`` as the caller may see it."""
    read = await get_view(db, ctx, key)
    revision = read.revision
    if revision.source_kind != "process":
        # Tasks and knowledge (stage 6): the same request and answers over another source.
        from control_plane.application.queries import view_records

        return await view_records.prepare(db, ctx, settings, read, query, locale=locale)
    form = revision.spec
    block = vq.block_of(form, query.block)
    declared = dict(form.get("params") or {})
    given = vq.params(query.params, declared)
    process = str(form["source"]["process"])
    definition = await latest_definition(db, ctx.tenant_id, process)
    spec = definition.spec if definition is not None else {}
    order, titles = _stage_titles(spec)
    data_schema = spec.get("data") if isinstance(spec.get("data"), Mapping) else None
    chosen = choose_locale(form, locale)
    scope = await package_scope(db, ctx.tenant_id, revision.package_key)
    seen = await snapshot(db, ctx.tenant_id, scope)
    q = _Query(
        db=db,
        ctx=ctx,
        read=read,
        block=block,
        params=given,
        locale=chosen,
        default_locale=str(form.get("defaultLocale") or chosen),
        messages=await _dictionaries(db, ctx.tenant_id, revision.package_key),
        process=process,
        data_schema=data_schema,
        stages=order,
        stage_titles=titles,
        env=source_environment(
            "process", ProcessShape(data_schema, order), declared, settings=scope.variable
        ),
        now=utcnow(),
        settings=seen.values if seen is not None else None,
    )
    if query.cursor is not None and block.kind not in vq.PAGED_BLOCKS:
        raise vq.invalid(
            "invalid_cursor", f"block {block.index} ({block.kind}) is not paged", block=block.index
        )
    conditions = vq.conditions(query.filter, vq.filter_fields(block))
    orders = vq.orders(query.sort, block)
    limit = vq.limit_of(query.limit, block)
    if block.kind in ("table", "list"):
        return Answer(await _table(q, conditions, orders, limit, query))
    if block.kind == "board":
        return Answer(await _board(q, conditions, limit))
    if block.kind == "metrics":
        return Answer(await _metrics(q))
    if block.kind == "chart":
        return Answer(await _chart(q, limit))
    instance = await _instance(q)
    if block.kind == "header":
        return Answer(_header(q, instance))
    if block.kind == "fields":
        return Answer({"values": _columns(q, block.spec.get("items") or (), instance)})
    if block.kind == "steps":
        return Answer(await _steps(q, instance))
    if block.kind == "timeline":
        return Answer(await _timeline(q, instance, limit))
    if block.kind == "artifacts":
        return Answer(await _artifacts(q, instance, limit))
    return await _related(q, instance, settings)


async def fetch_related(
    related: Related, provider: GraphProvider, settings: Settings, *, trace_run_id: str = ""
) -> dict[str, Any]:
    """The relations a ``related`` block shows: read from Memory after the transaction."""
    found = await relations_from(
        related.call,
        provider,
        settings,
        kind=related.kind,
        key=related.key,
        trace_run_id=trace_run_id,
    )
    return {
        "items": [
            {
                "relation": r["relation"],
                "kind": r["kind"],
                "key": r["key"],
                "entityTitle": r["title"],
                "direction": r["direction"],
            }
            for r in found
        ]
    }


# --- the set of instances ------------------------------------------------------------------------

Check = Callable[[ProcessInstance], bool]


@dataclass
class _Set:
    """The instances a block reads: SQL conditions, and checks evaluated record by record."""

    where: list[ColumnElement[bool]]
    checks: list[Check]

    def holds(self, instance: ProcessInstance) -> bool:
        return all(check(instance) for check in self.checks)


async def _source(q: _Query, conditions: Sequence[vq.Condition] = ()) -> _Set:
    where: list[ColumnElement[bool]] = [
        ProcessInstance.tenant_id == q.ctx.tenant_id,
        ProcessInstance.definition_key == q.process,
    ]
    workspaces = await visible_objects(q.ctx, Permission.PROCESSES_READ, "workspace")
    if workspaces is not None:
        where.append(
            or_(
                ProcessInstance.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                ProcessInstance.workspace_id.is_(None),
            )
        )
    checks: list[Check] = []
    text = q.form["source"].get("filter")
    if isinstance(text, str):
        program = q.compile_strict(text, "source filter")
        try:
            where.append(q.translator().predicate(program))
        except Untranslatable:
            checks.append(_evaluated(q, program))
    for condition in conditions:
        if condition.field.path == "slaState":
            checks.append(_deadline_state(condition))
        else:
            where.append(_condition(q, condition))
    return _Set(where, checks)


def _deadline_state(condition: vq.Condition) -> Check:
    """A filter by ``slaState``: the state of the deadlines as of now, as an instance shows it."""
    wanted = set(condition.value if condition.op == "in" else (condition.value,))

    def check(instance: ProcessInstance) -> bool:
        return instance_sla(instance)[1] in wanted

    return check


def _evaluated(q: _Query, program: Program) -> Check:
    """The source filter evaluated on one instance: what SQL does not say exactly."""
    sla = "slaState" in program.reads

    def check(instance: ProcessInstance) -> bool:
        return _true(program, q.values(instance, sla=sla))

    return check


def _true(program: Program, values: Mapping[str, Any]) -> bool:
    try:
        return _run(program, values) is True
    except ExpressionError:
        return False


def _value(program: Program | None, values: Mapping[str, Any]) -> Any:
    if program is None:
        return None
    try:
        return _run(program, values)
    except ExpressionError:
        return None


def _json_day(value: ColumnElement[Any]) -> ColumnElement[Any]:
    """The day a JSON string of a date or a date-time starts with; else ``NULL``."""
    day: ColumnElement[Any] = func.substring(
        view_sql.as_text(value), r"^([0-9]{4}-[0-9]{2}-[0-9]{2})"
    )
    return day


def _compare(op: str, value: ColumnElement[Any], given: Any) -> ColumnElement[bool]:
    found: ColumnElement[bool]
    if op == "gte":
        found = value >= given
    elif op == "lte":
        found = value <= given
    else:
        found = value == given
    return found


def _condition(q: _Query, condition: vq.Condition) -> ColumnElement[bool]:
    """A condition of the request in SQL: by the type of the filter, on its path."""
    path, op, kind = condition.field.path, condition.op, condition.field.type
    given = list(condition.value) if op == "in" else [condition.value]
    root, _, rest = path.partition(".")
    if root == "data" and rest:
        return json_condition(tuple(rest.split(".")), kind, op, given)
    if path == "status":
        return ProcessInstance.status.in_([str(v) for v in given])
    if path == "stage":
        found = [q.stages.index(v) for v in given if isinstance(v, str) and v in q.stages]
        return view_sql.current_stage(q.stages).in_(found) if found else false()
    if path in ("id", "instance.id"):
        ids = [i for i in (vq.as_uuid(v) for v in given) if i is not None]
        return ProcessInstance.id.in_(ids) if ids else false()
    if path == "instance.key":
        if op == "prefix":
            pattern = view_sql.like_prefix(str(given[0]))
            return ProcessInstance.instance_key.ilike(pattern, escape="\\")
        return ProcessInstance.instance_key.in_([str(v) for v in given])
    if path == "instance.version":
        version = cast(ProcessInstance.definition_version, Numeric)
        if op in ("eq", "in"):
            return or_(*(version == literal(v, Numeric) for v in given))
        return _compare(op, version, literal(given[0], Numeric))
    if path in ("instance.startedAt", "instance.clock"):
        moment = ProcessInstance.started_at if path == "instance.startedAt" else literal(q.now)
        day = func.to_char(func.timezone("UTC", moment), "YYYY-MM-DD")
        return or_(*(_compare(op, day, v) for v in given))
    raise ValidationError(
        "undeclared_filter", f"{path} is not a field to filter by", details={"field": path}
    )


def json_condition(
    steps: Sequence[str], kind: str, op: str, given: Sequence[Any], column: Any = None
) -> ColumnElement[bool]:
    """A condition of a filter of type ``kind`` on the JSON value at ``steps`` of ``column``."""
    value = view_sql.json_at(steps, column)
    if kind == "date":
        day = _json_day(value)
        return or_(*(_compare(op, day, v) for v in given))
    if op in ("eq", "in"):
        return or_(*(view_sql.contains(steps, _json_scalar(v), column) for v in given))
    if op == "prefix":
        return view_sql.as_text(value).ilike(view_sql.like_prefix(str(given[0])), escape="\\")
    number = view_sql.as_number(value)
    return _compare(op, number, literal(given[0], Numeric))


def _json_scalar(value: Any) -> Any:
    return vq.number(value) if isinstance(value, Decimal) else value


# --- pages of a table and a list -----------------------------------------------------------------


@dataclass(frozen=True)
class _Key:
    expr: Any
    descending: bool
    codec: str


def _keys(q: _Query, orders: Sequence[tuple[vq.DeclaredField, str]]) -> list[_Key]:
    """The keys of the order of a page: each sort field (nulls last), then the newest first."""
    paths: list[tuple[view_sql.PathSql, str]] = []
    for declared, direction in orders:
        try:
            paths.append((view_sql.path_sql(declared.path, q.data_schema, q.stages), direction))
        except Untranslatable:
            continue
    tail = [_Key(ProcessInstance.started_at, True, "time"), _Key(ProcessInstance.id, True, "uuid")]
    return sort_keys(paths, tail)


def sort_keys(paths: Sequence[tuple[view_sql.PathSql, str]], tail: Sequence[_Key]) -> list[_Key]:
    """The keys of ``paths`` in their directions, a missing value last; then ``tail``."""
    keys: list[_Key] = []
    for found, direction in paths:
        if found.codec == "json":
            missing = func.coalesce(func.jsonb_typeof(found.value), "null") == "null"
            value = func.coalesce(found.value, cast(literal("null", Text), JSONB))
        else:
            missing = found.value.is_(None)
            value = func.coalesce(found.value, _placeholder(found.codec))
        keys.append(_Key(missing, False, "bool"))
        keys.append(_Key(value, direction == "desc", found.codec))
    keys.extend(tail)
    return keys


def _placeholder(codec: str) -> ColumnElement[Any]:
    placeholders: dict[str, ColumnElement[Any]] = {
        "text": literal("", Text),
        "int": literal(-1, Integer),
        "time": literal(_EPOCH),
        "uuid": cast(literal(str(uuid.UUID(int=0))), PG_UUID(as_uuid=True)),
    }
    return placeholders[codec]


def _literal(key: _Key, value: Any) -> ColumnElement[Any]:
    """A value of a cursor as a literal of its key's type; a value of another type is refused."""
    codec = key.codec
    if codec == "bool" and isinstance(value, bool):
        return literal(value, Boolean)
    if codec == "json":
        return cast(literal(json.dumps(value), Text), JSONB)
    if codec == "text" and isinstance(value, str):
        return literal(value, Text)
    if codec == "int" and isinstance(value, int) and not isinstance(value, bool):
        return literal(value, Integer)
    if codec == "time" and isinstance(value, str):
        try:
            return literal(datetime.fromisoformat(value))
        except ValueError:
            pass
    if codec == "uuid" and isinstance(value, str) and vq.as_uuid(value) is not None:
        return cast(literal(value), PG_UUID(as_uuid=True))
    raise vq.invalid("invalid_cursor", "Malformed pagination cursor")


def _encoded(codec: str, value: Any) -> Any:
    if codec == "time" and isinstance(value, datetime):
        return value.isoformat()
    if codec == "uuid":
        return str(value)
    return value


def _after(keys: Sequence[_Key], values: Sequence[Any]) -> ColumnElement[bool]:
    """Rows after ``values`` in the order of ``keys``: a key of either direction."""
    literals = [_literal(k, v) for k, v in zip(keys, values, strict=True)]
    clauses = []
    for index, key in enumerate(keys):
        parts = [keys[j].expr == literals[j] for j in range(index)]
        parts.append(key.expr < literals[index] if key.descending else key.expr > literals[index])
        clauses.append(and_(*parts))
    return or_(*clauses)


def _signature(q: _Query, query: ViewQuery, orders: Sequence[tuple[vq.DeclaredField, str]]) -> str:
    return signature(q.read, q.block, q.params, query, orders)


def signature(
    read: ViewRead,
    block: vq.QueryBlock,
    params: Mapping[str, Any],
    query: ViewQuery,
    orders: Sequence[tuple[vq.DeclaredField, str]],
) -> str:
    """What a cursor belongs to: the revision of the view, the block, the filters, the order."""
    body = {
        "h": read.revision.hash,
        "b": block.index,
        "p": params,
        "f": [dict(f) for f in query.filter or ()],
        "s": [[f.path, d] for f, d in orders],
    }
    canonical = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def _decode(cursor: str, signature: str, keys: Sequence[_Key]) -> list[Any]:
    decoded = decode_cursor(cursor)
    values = decoded.get("k")
    if decoded.get("q") != signature or not isinstance(values, list) or len(values) != len(keys):
        raise vq.invalid(
            "invalid_cursor",
            "The cursor belongs to another query of the view: ask again without it",
        )
    for key, value in zip(keys, values, strict=True):
        _literal(key, value)
    return values


async def _ordered(
    q: _Query, found: _Set, keys: Sequence[_Key], after: Sequence[Any] | None, wanted: int
) -> tuple[list[tuple[ProcessInstance, list[Any]]], bool]:
    """Up to ``wanted`` instances of the set in the order of ``keys``; whether more follow."""
    labels = [k.expr.label(f"k{i}") for i, k in enumerate(keys)]
    ordering = [k.expr.desc() if k.descending else k.expr.asc() for k in keys]
    base = select(ProcessInstance, *labels).where(*found.where)
    out: list[tuple[ProcessInstance, list[Any]]] = []
    position = list(after) if after is not None else None
    batch = wanted + 1 if not found.checks else BATCH
    while True:
        stmt = base if position is None else base.where(_after(keys, position))
        rows = (await q.db.execute(stmt.order_by(*ordering).limit(batch))).all()
        if found.checks:
            q.evaluating(len(rows))
        for row in rows:
            instance, values = (
                row[0],
                [_encoded(k.codec, v) for k, v in zip(keys, row[1:], strict=True)],
            )
            if found.holds(instance):
                out.append((instance, values))
                if len(out) > wanted:
                    return out[:wanted], True
        if len(rows) < batch or not found.checks:
            return out, False
        last = rows[-1]
        position = [_encoded(k.codec, v) for k, v in zip(keys, last[1:], strict=True)]


async def _table(
    q: _Query,
    conditions: Sequence[vq.Condition],
    orders: Sequence[tuple[vq.DeclaredField, str]],
    limit: int,
    query: ViewQuery,
) -> dict[str, Any]:
    found = await _source(q, conditions)
    keys = _keys(q, orders)
    signature = _signature(q, query, orders)
    after = _decode(query.cursor, signature, keys) if query.cursor is not None else None
    rows, more = await _ordered(q, found, keys, after, limit)
    columns = q.block.spec.get("columns") or ()
    target = q.block.spec.get("open") or {}
    opener = q.compile(target["id"]) if isinstance(target.get("id"), str) else None
    items = []
    for instance, _ in rows:
        values = q.values(instance)
        items.append(
            {
                "id": _row_id(opener, values, instance),
                "title": instance.instance_key,
                "values": _columns(q, columns, instance, values),
            }
        )
    next_cursor = encode_cursor({"q": signature, "k": rows[-1][1]}) if more and rows else None
    return {"items": items, "nextCursor": next_cursor}


def _row_id(opener: Program | None, values: Mapping[str, Any], instance: ProcessInstance) -> str:
    """What ``open.view`` opens: ``open.id`` of the package computed, else the instance id."""
    if opener is not None:
        found = _value(opener, values)
        if isinstance(found, str):
            return found
    return str(instance.id)


# --- values of one record -------------------------------------------------------------------------


def path_value(path: str, values: Mapping[str, Any], order: Sequence[str]) -> Any:
    """The value of a path of the source on one instance (``stage`` — its stage)."""
    if path == "stage":
        stages = {
            sid: {"state": "active" if s["active"] else "completed" if s["completed"] else ""}
            for sid, s in values["stage"].items()
        }
        return vq.current_stage(stages, order)
    if path in ("status", "slaState", "id"):
        return values[path]
    root, _, rest = path.partition(".")
    node: Any = values.get(root)
    for step in rest.split(".") if rest else ():
        node = node.get(step) if isinstance(node, Mapping) else None
    return node


def _currency(reads: Sequence[str], data: Mapping[str, Any]) -> str | None:
    """The currency of an amount: ``currency`` next to the one ``…amount`` field it reads."""
    fields = [r for r in reads if r.startswith("data.")]
    if len(fields) != 1 or not fields[0].endswith(".amount"):
        return None
    node: Any = data
    for step in fields[0].removeprefix("data.").split(".")[:-1]:
        node = node.get(step) if isinstance(node, Mapping) else None
    found = node.get("currency") if isinstance(node, Mapping) else None
    return found if isinstance(found, str) else None


def _columns(
    q: _Query,
    columns: Sequence[Mapping[str, Any]],
    instance: ProcessInstance,
    values: Mapping[str, Any] | None = None,
    keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """``{key: value}`` of columns (or ``items`` of fields) by the keys of the display."""
    values = values if values is not None else q.values(instance)
    shown = (
        keys
        if keys is not None
        else _display_keys(q, "columns" if q.block.kind in ("table", "list") else "items")
    )
    out: dict[str, Any] = {}
    for column, key in zip(columns, shown, strict=False):
        format = column.get("format")
        if "field" in column:
            raw = path_value(str(column["field"]), values, q.stages)
            reads: Sequence[str] = [str(column["field"])]
        else:
            program = q.compile(str(column["value"]))
            raw = _value(program, values)
            reads = program.reads if program is not None else ()
        data = instance.data or {}
        currency = None
        if format == "money":
            currency = _currency(reads, data)
            raw = _as_written(raw, reads, data)
        fields = [r for r in reads if not r.startswith("param")]
        name = vq.field_name(fields[0]) if len(fields) == 1 else "status"
        out[key] = vq.shown(format, raw, q.how(instance, currency, name))
    return out


def _as_written(value: Any, reads: Sequence[str], data: Mapping[str, Any]) -> Any:
    """An amount as the data writes it: ``decimal(data.x)`` of ``"184500.00"`` is that string.

    ``decimal()`` gives a double; the console shows the decimal notation the data
    keeps when the value is that of the one field it reads, unchanged.
    """
    fields = [r for r in reads if r.startswith("data.")]
    if len(fields) != 1 or isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    node: Any = data
    for step in fields[0].removeprefix("data.").split("."):
        node = node.get(step) if isinstance(node, Mapping) else None
    written = vq.amount(node) if isinstance(node, str) else None
    if isinstance(written, str) and float(written) == value:
        return written
    return value


def _display_keys(q: _Query, name: str) -> list[str]:
    return [str(c["key"]) for c in q.block.shown.get(name) or ()]


# --- a board: a column per stage ------------------------------------------------------------


async def _board(q: _Query, conditions: Sequence[vq.Condition], limit: int) -> dict[str, Any]:
    found = await _source(q, conditions)
    buckets: dict[str, list[ProcessInstance]] = {sid: [] for sid in q.stages}
    if not found.checks and q.stages:
        stage = view_sql.current_stage(q.stages)
        ranked = (
            select(
                ProcessInstance.id.label("iid"),
                stage.label("stage"),
                func.row_number()
                .over(
                    partition_by=stage,
                    order_by=(ProcessInstance.started_at.desc(), ProcessInstance.id.desc()),
                )
                .label("n"),
            )
            .where(*found.where)
            .subquery()
        )
        stmt = (
            select(ProcessInstance, ranked.c.stage)
            .join(ranked, ranked.c.iid == ProcessInstance.id)
            .where(ranked.c.n <= limit, ranked.c.stage.is_not(None))
            .order_by(ranked.c.stage, ProcessInstance.started_at.desc(), ProcessInstance.id.desc())
        )
        for instance, index in (await q.db.execute(stmt)).tuples():
            buckets[q.stages[index]].append(instance)
    elif q.stages:
        keys = _keys(q, ())
        position: list[Any] | None = None
        while True:
            rows, more = await _ordered(q, _Set(found.where, []), keys, position, BATCH)
            q.evaluating(len(rows))
            for instance, _ in rows:
                if not found.holds(instance):
                    continue
                sid = vq.current_stage((instance.state or {}).get("stages"), q.stages)
                if sid is not None and len(buckets[sid]) < limit:
                    buckets[sid].append(instance)
            if not more or all(len(b) >= limit for b in buckets.values()):
                break
            position = rows[-1][1]
    card = q.block.spec.get("card") or {}
    fields = card.get("fields") or ()
    field_keys = [str(f["key"]) for f in (q.block.shown.get("card") or {}).get("fields") or ()]
    target = q.block.spec.get("open") or {}
    opener = q.compile(target["id"]) if isinstance(target.get("id"), str) else None
    columns = []
    for sid in q.stages:
        items = []
        for instance in buckets[sid]:
            values = q.values(instance)
            item: dict[str, Any] = {
                "id": _row_id(opener, values, instance),
                "title": _text_value(card.get("title"), values, q) or instance.instance_key,
            }
            subtitle = _text_value(card.get("subtitle"), values, q)
            if subtitle is not None:
                item["subtitle"] = subtitle
            item["values"] = _columns(q, fields, instance, values, field_keys)
            badge = card.get("badge")
            if isinstance(badge, str):
                raw = path_value(badge, values, q.stages)
                if raw is not None:
                    item["badge"] = {
                        "title": q.label(vq.field_name(badge), raw),
                        "category": vq.status_category(instance.status),
                    }
            items.append(item)
        columns.append({"key": sid, "title": q.label("stage", sid), "items": items})
    return {"columns": columns}


def _text_value(path: Any, values: Mapping[str, Any], q: _Query) -> str | None:
    if not isinstance(path, str):
        return None
    raw = path_value(path, values, q.stages)
    if raw is None:
        return None
    if path in ("stage", "status", "slaState"):
        return q.label(path, raw)
    shown = vq.plain(raw)
    return shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)


# --- aggregates: metrics and chart ----------------------------------------------------------------


@dataclass
class _Aggregate:
    """One value of ``metrics`` or ``chart``: ``count/sum/avg/min/max`` of an expression."""

    name: str
    program: Program | None
    format: str | None
    # The SQL of the aggregate, when it translates; else it is summed up record by record.
    sql: ColumnElement[Any] | None = None
    currency_sql: tuple[ColumnElement[Any], ColumnElement[Any]] | None = None
    currency_path: tuple[str, ...] | None = None


def _aggregate(q: _Query, text: str, format: str | None, exact: bool) -> _Aggregate:
    parsed = split_aggregate(text)
    if parsed is None:
        raise q_stale(q, "aggregate")
    name, inner = parsed
    program = q.compile_strict(inner, "aggregate") if inner else None
    found = _Aggregate(name, program, format)
    if program is not None:
        reads = [r for r in program.reads if r.startswith("data.")]
        if format == "money" and len(reads) == 1 and reads[0].endswith(".amount"):
            found.currency_path = (*reads[0].removeprefix("data.").split(".")[:-1], "currency")
    if not exact:
        return found
    try:
        translator = q.translator()
        if name == "count":
            condition = translator.predicate(program) if program is not None else None
            found.sql = func.count() if condition is None else func.count().filter(condition)
        else:
            assert program is not None
            number = translator.number(program)
            found.sql = {
                "sum": func.coalesce(func.sum(number), literal(0, Numeric)),
                "avg": func.avg(number),
                "min": func.min(number),
                "max": func.max(number),
            }[name]
    except Untranslatable:
        found.sql = None
    if found.sql is not None and found.currency_path is not None:
        currency = view_sql.as_text(view_sql.json_at(found.currency_path))
        found.currency_sql = (func.min(currency), func.max(currency))
    return found


def q_stale(q: _Query, what: str) -> ConflictError:
    return ConflictError(
        "view_stale",
        f"the {what} of view {q.read.view.key!r} is not one the core reads:"
        " apply its package again",
        details={"key": q.read.view.key},
    )


class _Sum:
    """An aggregate summed up record by record (an expression SQL does not say)."""

    def __init__(self, aggregate: _Aggregate) -> None:
        self.aggregate = aggregate
        self.count = 0
        self.total: Any = 0
        self.best: Any = None
        self.currencies: set[str] = set()

    def add(self, values: Mapping[str, Any], data: Mapping[str, Any]) -> None:
        name, program = self.aggregate.name, self.aggregate.program
        if name == "count":
            if program is None or _true(program, values):
                self.count += 1
            return
        found = _value(program, values)
        if isinstance(found, bool) or found is None:
            return
        if name in ("sum", "avg"):
            if not isinstance(found, (int, float)):
                return
            self.total += found
            self.count += 1
        else:
            try:
                better = self.best is None or (
                    found < self.best if name == "min" else found > self.best
                )
            except TypeError:
                return
            if better:
                self.best = found
        if self.aggregate.currency_path is not None:
            node: Any = data
            for step in self.aggregate.currency_path:
                node = node.get(step) if isinstance(node, Mapping) else None
            if isinstance(node, str):
                self.currencies.add(node)

    def value(self) -> Any:
        if self.aggregate.name == "count":
            return self.count
        if self.aggregate.name == "sum":
            return self.total
        if self.aggregate.name == "avg":
            return self.total / self.count if self.count else None
        return self.best

    def currency(self) -> str | None:
        return next(iter(self.currencies)) if len(self.currencies) == 1 else None


def _aggregate_shown(q: _Query, aggregate: _Aggregate, value: Any, currency: str | None) -> Any:
    if isinstance(value, Decimal) and aggregate.format != "money":
        value = vq.number(value)
    return vq.shown(aggregate.format, value, q.how(None, currency))


async def _narrowed(q: _Query, found: _Set) -> _Set:
    """The set with its checks done: the ids of the instances that hold them.

    What SQL does not say is evaluated once a record; the aggregates are then
    SQL's again, over those ids.
    """
    if not found.checks:
        return found
    ids: list[uuid.UUID] = []
    after: uuid.UUID | None = None
    while True:
        stmt = select(ProcessInstance).where(*found.where)
        if after is not None:
            stmt = stmt.where(ProcessInstance.id > after)
        rows = list((await q.db.scalars(stmt.order_by(ProcessInstance.id).limit(BATCH))).all())
        q.evaluating(len(rows))
        ids += [instance.id for instance in rows if found.holds(instance)]
        if len(rows) < BATCH:
            break
        after = rows[-1].id
    chosen = literal(ids, ARRAY(PG_UUID(as_uuid=True)))
    return _Set(
        [ProcessInstance.tenant_id == q.ctx.tenant_id, ProcessInstance.id == any_(chosen)], []
    )


async def _metrics(q: _Query) -> dict[str, Any]:
    found = await _narrowed(q, await _source(q))
    exact = not found.checks
    aggregates = [
        _aggregate(q, str(item["value"]), item.get("format"), exact)
        for item in q.block.spec.get("items") or ()
    ]
    out: list[Any] = [None] * len(aggregates)
    in_sql = [i for i, a in enumerate(aggregates) if a.sql is not None]
    if in_sql:
        columns: list[Any] = []
        for i in in_sql:
            columns.append(aggregates[i].sql)
            columns.extend(aggregates[i].currency_sql or (null(), null()))
        row = (await q.db.execute(select(*columns).where(*found.where))).one()
        for n, i in enumerate(in_sql):
            value, low, high = row[3 * n], row[3 * n + 1], row[3 * n + 2]
            currency = low if low is not None and low == high else None
            out[i] = _aggregate_shown(q, aggregates[i], value, currency)
    rest = [i for i, a in enumerate(aggregates) if a.sql is None]
    if rest:
        sums = {i: _Sum(aggregates[i]) for i in rest}

        def visit(values: Mapping[str, Any], data: Mapping[str, Any]) -> None:
            for total in sums.values():
                total.add(values, data)

        await _each(q, found, visit, sla=any(_reads_sla(aggregates[i]) for i in rest))
        for i, total in sums.items():
            out[i] = _aggregate_shown(q, aggregates[i], total.value(), total.currency())
    return {"values": out}


async def _each(
    q: _Query,
    found: _Set,
    visit: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
    *,
    sla: bool,
) -> None:
    """Every instance of the set, record by record, in rounds of :data:`BATCH`.

    ``sla`` — an expression reads ``slaState``: the state of the deadlines of
    each instance is worked out (it is not, otherwise: it costs).
    """
    after: uuid.UUID | None = None
    while True:
        stmt = select(ProcessInstance).where(*found.where)
        if after is not None:
            stmt = stmt.where(ProcessInstance.id > after)
        rows = list((await q.db.scalars(stmt.order_by(ProcessInstance.id).limit(BATCH))).all())
        q.evaluating(len(rows))
        for instance in rows:
            if found.holds(instance):
                visit(q.values(instance, sla=sla), instance.data or {})
        if len(rows) < BATCH:
            return
        after = rows[-1].id


def _reads_sla(aggregate: _Aggregate) -> bool:
    return aggregate.program is not None and "slaState" in aggregate.program.reads


def _group_order(path: str, q: _Query) -> Callable[[Any], Any]:
    if path == "stage":
        return lambda v: q.stages.index(v) if v in q.stages else len(q.stages)
    if path == "status":
        return lambda v: INSTANCE_STATUSES.index(v) if v in INSTANCE_STATUSES else 99
    return lambda v: (type(v).__name__, v)


async def _chart(q: _Query, limit: int) -> dict[str, Any]:
    spec = q.block.spec
    path, format = str(spec["groupBy"]), spec.get("format")
    found = await _source(q)
    if path != "slaState":
        found = await _narrowed(q, found)
    aggregate = _aggregate(q, str(spec["value"]), format, not found.checks and path != "slaState")
    groups: dict[Any, tuple[Any, Any, str | None]] = {}
    group_sql: ColumnElement[Any] | None = None
    if aggregate.sql is not None:
        try:
            group_sql = view_sql.path_sql(path, q.data_schema, q.stages).value
        except Untranslatable:
            group_sql = None
    if group_sql is not None and aggregate.sql is not None:
        columns: list[Any] = [group_sql.label("g"), aggregate.sql.label("v")]
        columns += list(aggregate.currency_sql or (null(), null()))
        stmt = select(*columns).where(*found.where).group_by(group_sql)
        for group, value, low, high in (await q.db.execute(stmt)).tuples():
            if path == "stage":
                group = q.stages[group] if isinstance(group, int) else None
            elif isinstance(group, uuid.UUID):
                group = str(group)
            if group is None:
                continue
            groups[_hashable(group)] = (group, value, low if low == high else None)
    else:
        sums: dict[Any, tuple[Any, _Sum]] = {}

        def bucket(values: Mapping[str, Any], data: Mapping[str, Any]) -> None:
            group = vq.plain(path_value(path, values, q.stages))
            if group is None:
                return
            entry = sums.setdefault(_hashable(group), (group, _Sum(aggregate)))
            entry[1].add(values, data)

        await _each(q, found, bucket, sla=path == "slaState" or _reads_sla(aggregate))
        for marker, (group, total) in sums.items():
            groups[marker] = (group, total.value(), total.currency())
    by = _group_order(path, q)
    ordered = sorted(groups.values(), key=lambda g: by(g[0]))[:limit]
    name = vq.field_name(path)
    return {
        "points": [
            {
                "label": q.label(name, group)
                if not isinstance(group, (dict, list))
                else json.dumps(group),
                "value": _aggregate_shown(q, aggregate, value, currency),
            }
            for group, value, currency in ordered
        ]
    }


def _hashable(value: Any) -> Any:
    return (
        json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else value
    )


# --- one instance: header, fields, steps, timeline, artifacts, related --------------------------


async def _instance(q: _Query) -> ProcessInstance:
    """The instance the view shows: of its process and readable by the caller, else a 404."""
    name = str(q.form["source"]["instance"]).removeprefix("param.")
    raw = q.params.get(name)
    ident = vq.as_uuid(raw)
    instance = None
    if ident is not None:
        instance = await q.db.scalar(
            select(ProcessInstance).where(
                ProcessInstance.id == ident,
                ProcessInstance.tenant_id == q.ctx.tenant_id,
                ProcessInstance.definition_key == q.process,
            )
        )
    if instance is None or not await q.visible(instance.workspace_id):
        raise NotFoundError("Process instance not found", details={"instanceId": str(raw)[:100]})
    return instance


def _header(q: _Query, instance: ProcessInstance) -> dict[str, Any]:
    values = q.values(instance)
    spec = q.block.spec
    title = _text_value(spec.get("title"), values, q) or instance.instance_key
    path = spec.get("status")
    if isinstance(path, str):
        raw = path_value(path, values, q.stages)
        status_title = q.label(vq.field_name(path), raw)
    else:
        status_title = q.label("status", instance.status)
    return {
        "title": title,
        "status": {"title": status_title, "category": vq.status_category(instance.status)},
    }


def _step_titles(spec: Mapping[str, Any]) -> dict[str, str]:
    """``displayName`` of every element of a process by its id."""
    found: dict[str, str] = {}
    stack: list[Any] = [spec.get("stages")]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            if isinstance(node.get("id"), str) and isinstance(node.get("displayName"), str):
                found.setdefault(node["id"], node["displayName"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


async def _steps(q: _Query, instance: ProcessInstance) -> dict[str, Any]:
    """The open steps of the instance; the task of each the caller may read."""
    definition = await q.db.get(ProcessDefinition, instance.definition_id)
    titles = _step_titles(definition.spec if definition is not None else {})
    opened = open_elements(instance)
    task_ids = [i for i in (vq.as_uuid(e.get("taskId")) for e in opened if e.get("taskId")) if i]
    tasks: dict[uuid.UUID, Task] = {}
    if task_ids:
        rows = await q.db.scalars(
            select(Task).where(Task.tenant_id == q.ctx.tenant_id, Task.id.in_(task_ids))
        )
        for task in rows:
            # A step's work of an invisible workspace is not named: neither
            # its reference, nor its title, nor its assignee (CP-ADR-0082 §3.7).
            if await permits_task(q.ctx, Permission.TASKS_READ, task=task):
                tasks[task.id] = task
    items = []
    for element in opened:
        key = str(element["id"])
        item: dict[str, Any] = {"key": key, "title": titles.get(key, key)}
        opened_task = tasks.get(vq.as_uuid(element.get("taskId")) or uuid.UUID(int=0))
        if opened_task is not None:
            task = opened_task
            item["task"] = {"ref": task.public_id, "title": task.title}
            if task.assignee_id is not None:
                item["assignee"] = str(task.assignee_id)
        if element.get("since"):
            item["since"] = element["since"]
        items.append(item)
    return {"items": items}


async def _timeline(q: _Query, instance: ProcessInstance, limit: int) -> dict[str, Any]:
    """The journal of the instance, newest first: what happened, when and by whom."""
    rows = await q.db.scalars(
        select(ProcessInstanceEvent)
        .where(ProcessInstanceEvent.instance_id == instance.id)
        .order_by(ProcessInstanceEvent.seq.desc())
        .limit(limit)
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        for entry in reversed(journal_entries(row)):
            if len(items) == limit:
                break
            at = entry.get("at")
            item: dict[str, Any] = {
                "at": vq.rfc3339(at) if isinstance(at, datetime) else at,
                "title": str(entry.get("reason") or entry.get("kind") or ""),
            }
            if entry.get("actorId") is not None:
                item["actor"] = str(entry["actorId"])
            items.append(item)
    return {"items": items}


async def _artifacts(q: _Query, instance: ProcessInstance, limit: int) -> dict[str, Any]:
    """The artifacts of the tasks the instance opened, of the types the block names."""
    task_ids = [
        i
        for i in (
            vq.as_uuid(ref.partition(":")[2])
            for ref in instance.refs or {}
            if ref.startswith("task:")
        )
        if i is not None
    ]
    if not task_ids:
        return {"items": []}
    stmt = select(Artifact).where(
        Artifact.tenant_id == q.ctx.tenant_id,
        Artifact.task_id.in_(task_ids),
        # Artifacts of a step's work in an invisible workspace are not shown
        # (CP-ADR-0082 §3.7): a permission on the task does not see workspaces.
        artifact_condition(q.ctx),
    )
    types = q.block.spec.get("types")
    if isinstance(types, list) and types:
        stmt = stmt.where(Artifact.type.in_([str(t) for t in types]))
    rows = await q.db.scalars(
        stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(limit)
    )
    allowed: dict[uuid.UUID | None, bool] = {}
    items = []
    for artifact in rows:
        if artifact.task_id not in allowed:
            allowed[artifact.task_id] = await permits(
                q.ctx,
                Permission.ARTIFACTS_READ,
                resource=artifact_resource(artifact.task_id, artifact.workspace_id),
            )
        if allowed[artifact.task_id]:
            items.append(
                {
                    "id": str(artifact.id),
                    "name": artifact.name,
                    "type": artifact.type,
                    "createdAt": vq.rfc3339(artifact.created_at),
                }
            )
    return {"items": items}


async def _related(q: _Query, instance: ProcessInstance, settings: Settings) -> Answer:
    """The relations of the knowledge record the block starts from, read from Memory."""
    spec = q.block.spec
    knowledge = spec.get("knowledge") or {}
    program = q.compile(str(knowledge.get("key") or ""))
    key = _value(program, q.values(instance))
    if not isinstance(key, str) or not key.strip() or instance.workspace_id is None:
        return Answer({"items": []})
    include = spec.get("include") or {}
    names = include.get("relations")
    relations = RelationsInclude(
        names=tuple(names) if isinstance(names, list) else None,
        direction=str(include.get("direction") or "both"),
        limit=int(include.get("limit") or 20),
    )
    try:
        call = await prepare_entities_query(
            q.db,
            q.ctx,
            settings,
            workspace_id=instance.workspace_id,
            kinds=[str(knowledge["kind"])],
            where=[],
            as_of=None,
            limit=1,
            cursor=None,
            relations=relations,
        )
    except (AuthorizationError, WorkspaceNotVisible):
        # The knowledge of the workspace is not the caller's to read: the block shows none.
        return Answer({"items": []})
    return Answer(None, Related(call, str(knowledge["kind"]), key))
