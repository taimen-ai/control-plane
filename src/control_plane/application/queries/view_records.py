"""``POST /views/{key}:query`` of a view of tasks or of knowledge (CP-ADR-0080, amendment Б).

TAI-ADR-0066 p.1, stage 6: the request and the answers of a view of a process
(:mod:`.view_data`, amendment A), over two more sources:

- ``{tasks: {type}}`` — the tasks of a type, read as ``GET /tasks`` lists them
  to the caller (:func:`~control_plane.application.queries.lists.readable_tasks`).
  A record is ``id``, ``fields.<field of TaskOut>`` and ``customFields`` by the
  ``fieldSchema`` of the type. The filters and the order of a page are SQL
  over the columns and ``custom_fields``; the expressions of the view are
  evaluated record by record.
- ``{knowledge: {kinds}}`` — the records of those kinds in the knowledge base
  of the root of the caller's workspace tree, read as
  ``POST /knowledge/entities:query`` reads them: ``events.read`` on the
  workspace, the namespace of its tree's root, the caller's visibility in
  Memory. The transaction only decides where and whether; the records are
  read from Memory after it (:attr:`Answer.memory`), filtered, ordered and
  aggregated here. A column ``relations.<name>`` is read by the typed
  traversal ``include.relations`` answers with, for the rows of the page.

Both evaluate record by record, so both have the ceiling of amendment A6: past
it a block costs more than it may and the request is refused
(``409 view_too_costly``), never answered with part of the records.
"""

import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Text, case, cast, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from control_plane.application.authorization import (
    AuthContext,
    WorkspaceNotVisible,
    authorize,
)
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries import view_sql
from control_plane.application.queries.knowledge_entities import (
    EntitiesQueryCall,
    RelationsInclude,
    fetch_entities,
    prepare_entities_query,
    relations_of_items,
)
from control_plane.application.queries.lists import readable_tasks
from control_plane.application.queries.package_settings import package_scope, snapshot
from control_plane.application.queries.view_data import (
    MAX_EVALUATED,
    Answer,
    Related,
    ViewQuery,
    _after,
    _Aggregate,
    _decode,
    _dictionaries,
    _encoded,
    _Key,
    _Sum,
    _value,
    compiled,
    json_condition,
    signature,
    sort_keys,
)
from control_plane.application.queries.views import ViewRead
from control_plane.config import Settings
from control_plane.domain import view_query as vq
from control_plane.domain.cel_profile import Environment, Program
from control_plane.domain.enums import Permission, TaskTypeStatus
from control_plane.domain.errors import AuthorizationError, ConflictError, ValidationError
from control_plane.domain.views import (
    TASK_PRIORITIES,
    TaskShape,
    choose_locale,
    source_environment,
    split_aggregate,
    task_shape,
)
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.infrastructure.context_provider import GraphProvider
from control_plane.infrastructure.db.models import Task, TaskType, Workspace

# Records of knowledge one request reads from Memory, and how many a page of Memory holds.
MAX_KNOWLEDGE = 5_000
KNOWLEDGE_PAGE = 500
# Tasks read per round when they are evaluated record by record.
BATCH = 500
_FIELDS_PREFIX = "fields"
# The category of a value of ``status`` (CP-ADR-0080 A3: as ``ProcessInstanceOut.status``) by the
# system category of the task it is of.
TASK_CATEGORY: Mapping[str, str] = {
    "backlog": "suspended",
    "active": "running",
    "blocked": "suspended",
    "terminal_success": "completed",
    "terminal_cancelled": "cancelled",
}
# A record of knowledge has no state of its own.
KNOWLEDGE_CATEGORY = "running"
# The columns of a task a path ``fields.<name>`` reads, and how its value is kept.
_TASK_COLUMNS: Mapping[str, tuple[Any, str]] = {
    "publicId": (Task.public_id, "text"),
    "title": (Task.title, "text"),
    "description": (Task.description, "text"),
    "status": (Task.status, "text"),
    "systemStatusCategory": (Task.system_status_category, "text"),
    "priority": (Task.priority, "text"),
    "ownerId": (Task.owner_id, "uuid"),
    "assigneeId": (Task.assignee_id, "uuid"),
    "workspaceId": (Task.workspace_id, "uuid"),
    "createdBy": (Task.created_by, "uuid"),
    "startDate": (Task.start_date, "time"),
    "dueDate": (Task.due_date, "time"),
    "createdAt": (Task.created_at, "time"),
    "updatedAt": (Task.updated_at, "time"),
    "completedAt": (Task.completed_at, "time"),
}


# --- what a block of a view of tasks or knowledge knows ----------------------------------------


@dataclass
class _View:
    read: ViewRead
    block: vq.QueryBlock
    params: dict[str, Any]
    locale: str
    default_locale: str
    messages: Mapping[str, Mapping[str, str]]
    env: Environment
    settings: Mapping[str, Any] | None
    # Of a view of tasks: the statuses of its type, ``key -> displayName``, in their order.
    statuses: Mapping[str, str] = field(default_factory=dict)
    # Of a view of knowledge: the kinds of its source.
    kinds: tuple[str, ...] = ()
    _evaluated: int = 0

    @property
    def form(self) -> Mapping[str, Any]:
        return self.read.revision.spec

    @property
    def package(self) -> str:
        return self.read.revision.package_key or ""

    def evaluating(self, count: int, limit: int | None = None) -> None:
        """Account ``count`` records evaluated record by record; past ``limit`` — refused."""
        limit = MAX_EVALUATED if limit is None else limit
        self._evaluated += count
        if self._evaluated > limit:
            raise ConflictError(
                "view_too_costly",
                f"view {self.read.view.key!r} evaluates more than {limit} records record by"
                " record: narrow its source",
                details={"key": self.read.view.key, "limit": limit},
            )

    def compile(self, text: str) -> Program | None:
        return compiled(self.env, text)

    def compile_strict(self, text: str, what: str) -> Program:
        program = self.compile(text)
        if program is None:
            raise ConflictError(
                "view_stale",
                f"the {what} of view {self.read.view.key!r} no longer compiles against its"
                " source: apply its package again",
                details={"key": self.read.view.key},
            )
        return program

    def text(self, key: str) -> str | None:
        found = vq.text_of(key, self.messages, self.locale, self.default_locale)
        if found is None:
            found = vq.text_of(
                key, self.form.get("messages") or {}, self.locale, self.default_locale
            )
        return found

    def label(self, name: str, value: Any) -> str:
        """``<package>.fields.<name>.<value>``, else the name of a status of the type, else it."""
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        word = ("true" if value else "false") if isinstance(value, bool) else str(value)
        prefix = f"{self.package}." if self.package else ""
        found = self.text(f"{prefix}{_FIELDS_PREFIX}.{name}.{word}")
        if found is not None:
            return found
        if name == "fields.status" and word in self.statuses:
            return self.statuses[word]
        return word

    def display_keys(self, name: str) -> list[str]:
        return [str(c["key"]) for c in self.block.shown.get(name) or ()]


@dataclass
class _Row:
    """One record: its variables for CEL, its id and title, the category of its status."""

    values: dict[str, Any]
    id: str
    title: str
    category: str
    # Of a record of knowledge: the other ends of its relations by relation name.
    relations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def path_value(path: str, values: Mapping[str, Any]) -> Any:
    """The value of a path of the source on one record: its steps through the variables."""
    node: Any = values
    for step in path.split("."):
        node = node.get(step) if isinstance(node, Mapping) else None
    return node


def _reads_of_record(reads: Iterable[str]) -> list[str]:
    """The reads of an expression that are fields of the record, not params or settings."""
    return [r for r in reads if r.split(".", 1)[0] not in ("param", "settings")]


def _money(reads: Sequence[str], values: Mapping[str, Any], raw: Any) -> tuple[Any, str | None]:
    """An amount as the record writes it, and the ``currency`` next to the one field read."""
    fields = _reads_of_record(reads)
    if len(fields) != 1:
        return raw, None
    currency = None
    if fields[0].endswith(".amount"):
        found = path_value(fields[0].rsplit(".", 1)[0] + ".currency", values)
        currency = found if isinstance(found, str) else None
    written = path_value(fields[0], values)
    if (
        isinstance(written, str)
        and not isinstance(raw, bool)
        and isinstance(raw, (int, float))
        and isinstance(vq.amount(written), str)
        and float(written) == raw
    ):
        return written, currency
    return raw, currency


def _columns(
    v: _View, columns: Sequence[Mapping[str, Any]], keys: Sequence[str], row: _Row
) -> dict[str, Any]:
    """``{key: value}`` of the columns of a row by the keys of the display."""
    out: dict[str, Any] = {}
    for column, key in zip(columns, keys, strict=False):
        format = column.get("format")
        if "field" in column:
            path = str(column["field"])
            reads: Sequence[str] = [path]
            root, _, name = path.partition(".")
            if v.kinds and root == "relations":
                ends = row.relations.get(name) or ()
                titles = [str(e.get("title") or e.get("key") or "") for e in ends]
                raw: Any = ", ".join(t for t in titles if t) or None
            else:
                raw = path_value(path, row.values)
        else:
            program = v.compile(str(column["value"]))
            raw = _value(program, row.values)
            reads = program.reads if program is not None else ()
        currency = None
        if format == "money":
            raw, currency = _money(reads, row.values, raw)
        fields = _reads_of_record(reads)
        name = fields[0] if len(fields) == 1 else "status"
        how = vq.Shown(label=_labeller(v, name), category=row.category, currency=currency)
        out[key] = vq.shown(format, raw, how)
    return out


def _labeller(v: _View, name: str) -> Callable[[Any], str]:
    return lambda value: v.label(name, value)


def _page(v: _View, rows: Sequence[_Row], next_cursor: str | None) -> dict[str, Any]:
    columns = v.block.spec.get("columns") or ()
    keys = v.display_keys("columns")
    target = v.block.spec.get("open") or {}
    opener = v.compile(target["id"]) if isinstance(target.get("id"), str) else None
    items = []
    for row in rows:
        found = _value(opener, row.values) if opener is not None else None
        items.append(
            {
                "id": found if isinstance(found, str) else row.id,
                "title": row.title,
                "values": _columns(v, columns, keys, row),
            }
        )
    return {"items": items, "nextCursor": next_cursor}


# --- aggregates over records evaluated one by one -----------------------------------------------


def _aggregates(v: _View, texts: Sequence[tuple[str, str | None]]) -> list[_Aggregate]:
    out = []
    for text, format in texts:
        parsed = split_aggregate(text)
        if parsed is None:
            raise ConflictError(
                "view_stale",
                f"an aggregate of view {v.read.view.key!r} is not one the core reads:"
                " apply its package again",
                details={"key": v.read.view.key},
            )
        name, inner = parsed
        program = v.compile_strict(inner, "aggregate") if inner else None
        found = _Aggregate(name, program, format)
        if program is not None and format == "money":
            reads = _reads_of_record(program.reads)
            if len(reads) == 1 and reads[0].endswith(".amount"):
                found.currency_path = (*reads[0].split(".")[:-1], "currency")
        out.append(found)
    return out


def _aggregate_shown(aggregate: _Aggregate, value: Any, currency: str | None) -> Any:
    if isinstance(value, Decimal) and aggregate.format != "money":
        value = vq.number(value)
    how = vq.Shown(label=lambda x: str(x), category=KNOWLEDGE_CATEGORY, currency=currency)
    return vq.shown(aggregate.format, value, how)


def _metrics(v: _View, rows: Iterable[_Row]) -> dict[str, Any]:
    items = v.block.spec.get("items") or ()
    sums = [_Sum(a) for a in _aggregates(v, [(str(i["value"]), i.get("format")) for i in items])]
    for row in rows:
        for total in sums:
            total.add(row.values, row.values)
    return {"values": [_aggregate_shown(s.aggregate, s.value(), s.currency()) for s in sums]}


def _group_order(v: _View, path: str) -> Any:
    if path == "fields.status":
        order = list(v.statuses)
        return lambda value: (order.index(value) if value in order else len(order), str(value))
    if path == "fields.priority":
        return lambda value: (
            TASK_PRIORITIES.index(value) if value in TASK_PRIORITIES else 9,
            str(value),
        )
    if path == "kind":
        return lambda value: (v.kinds.index(value) if value in v.kinds else 99, str(value))
    return lambda value: (type(value).__name__, value)


def _hashable(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _chart(v: _View, rows: Iterable[_Row], limit: int) -> dict[str, Any]:
    spec = v.block.spec
    path = str(spec["groupBy"])
    aggregate = _aggregates(v, [(str(spec["value"]), spec.get("format"))])[0]
    groups: dict[Any, tuple[Any, _Sum]] = {}
    for row in rows:
        group = vq.plain(path_value(path, row.values))
        if group is None:
            continue
        entry = groups.setdefault(_hashable(group), (group, _Sum(aggregate)))
        entry[1].add(row.values, row.values)
    by = _group_order(v, path)
    ordered = sorted(groups.values(), key=lambda g: by(_hashable(g[0])))[:limit]
    return {
        "points": [
            {
                "label": v.label(path, group),
                "value": _aggregate_shown(aggregate, total.value(), total.currency()),
            }
            for group, total in ordered
        ]
    }


# --- the request against the view ----------------------------------------------------------------


@dataclass(frozen=True)
class _Asked:
    """The request, checked against the block: conditions, order, size."""

    conditions: list[vq.Condition]
    orders: list[tuple[vq.DeclaredField, str]]
    limit: int


def _asked(block: vq.QueryBlock, query: ViewQuery) -> _Asked:
    if query.cursor is not None and block.kind not in vq.PAGED_BLOCKS:
        raise vq.invalid(
            "invalid_cursor", f"block {block.index} ({block.kind}) is not paged", block=block.index
        )
    return _Asked(
        vq.conditions(query.filter, vq.filter_fields(block)),
        vq.orders(query.sort, block),
        vq.limit_of(query.limit, block),
    )


def _mismatch(block: vq.QueryBlock, source: str) -> ValidationError:
    return vq.invalid(
        "block_source_mismatch",
        f"block {block.index} ({block.kind}) shows one instance of a process: the source of the"
        f" view is {source}",
        block=block.index,
    )


async def prepare(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    read: ViewRead,
    query: ViewQuery,
    *,
    locale: str | None,
) -> Answer:
    """The data of block ``query.block`` of ``read`` (tasks or knowledge) as the caller sees it."""
    revision = read.revision
    form = revision.spec
    block = vq.block_of(form, query.block)
    declared = dict(form.get("params") or {})
    given = vq.params(query.params, declared)
    source = revision.source_kind
    shape: TaskShape | None = None
    statuses: dict[str, str] = {}
    if source == "tasks":
        shape = await _task_type(db, ctx.tenant_id, str(form["source"]["tasks"]["type"]))
        statuses = dict(shape.statuses)
    chosen = choose_locale(form, locale)
    scope = await package_scope(db, ctx.tenant_id, revision.package_key)
    seen = await snapshot(db, ctx.tenant_id, scope)
    v = _View(
        read=read,
        block=block,
        params=given,
        locale=chosen,
        default_locale=str(form.get("defaultLocale") or chosen),
        messages=await _dictionaries(db, ctx.tenant_id, revision.package_key),
        env=source_environment(source, None, declared, settings=scope.variable, task=shape),
        settings=seen.values if seen is not None else None,
        statuses=statuses,
        kinds=tuple(str(k) for k in form["source"].get("knowledge", {}).get("kinds") or ())
        if source == "knowledge"
        else (),
    )
    asked = _asked(block, query)
    if block.kind == "related":
        return await _related(db, ctx, settings, v, query.workspace_id)
    if block.kind not in ("table", "list", "metrics", "chart"):
        raise _mismatch(block, source)
    if source == "tasks":
        return Answer(await _tasks(db, ctx, v, asked, query))
    workspace_id = await knowledge_workspace(db, ctx, query.workspace_id)
    if workspace_id is None:
        return Answer(_over_nothing(v, asked))
    # A cursor not of this query is refused before Memory is asked anything.
    after = _knowledge_cursor(v, asked, query)
    call = await prepare_entities_query(
        db,
        ctx,
        settings,
        workspace_id=workspace_id,
        kinds=list(v.kinds),
        where=[],
        as_of=None,
        limit=KNOWLEDGE_PAGE,
        cursor=None,
    )

    async def later(provider: GraphProvider, settings: Settings, trace: str) -> dict[str, Any]:
        return await _knowledge(v, asked, query, after, call, provider, settings, trace)

    return Answer(None, memory=later)


def _over_nothing(v: _View, asked: _Asked) -> dict[str, Any]:
    """The answer of a block over no record at all: the caller has no knowledge base."""
    if v.block.kind == "metrics":
        return _metrics(v, [])
    if v.block.kind == "chart":
        return _chart(v, [], asked.limit)
    return _page(v, [], None)


async def knowledge_workspace(
    db: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The workspace whose tree's root holds the knowledge a view reads.

    The one named (an invisible one is the 404 of a missing one, as
    ``entities:query`` answers it); else the caller's only tree — of the
    visible workspaces in ``members`` mode, of the tenant otherwise. None —
    no knowledge to read; several trees — ``422 workspace_required``.

    In ``members`` mode the root of the tree is often not visible itself
    (a member of a department under the company): any visible workspace of
    the tree stands for it, ``graph_scope`` reads the namespace of its root.
    """
    if workspace_id is not None:
        return workspace_id
    if ctx.visible_workspaces is not None:
        if len(ctx.visible_roots) > 1:
            raise _several_trees()
        visible = sorted(ctx.visible_workspaces)
        return uuid.UUID(visible[0]) if visible else None
    roots = list(
        await db.scalars(
            select(Workspace.id)
            .where(
                Workspace.tenant_id == ctx.tenant_id,
                Workspace.parent_id.is_(None),
                Workspace.status == "active",
            )
            .limit(2)
        )
    )
    if not roots:
        return None
    if len(roots) > 1:
        raise _several_trees()
    return roots[0]


def _several_trees() -> ValidationError:
    return vq.invalid(
        "workspace_required",
        "the caller's workspaces are in more than one tree: name workspaceId, the knowledge"
        " of whose tree the view reads",
    )


async def _related(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    v: _View,
    workspace_id: uuid.UUID | None,
) -> Answer:
    """``related`` of a view of tasks or knowledge: the key is an expression over the params."""
    spec = v.block.spec
    knowledge = spec.get("knowledge") or {}
    values = {"param": dict(v.params), "settings": dict(v.settings or {})}
    key = _value(v.compile(str(knowledge.get("key") or "")), values)
    if not isinstance(key, str) or not key.strip():
        return Answer({"items": []})
    include = spec.get("include") or {}
    names = include.get("relations")
    relations = RelationsInclude(
        names=tuple(names) if isinstance(names, list) else None,
        direction=str(include.get("direction") or "both"),
        limit=int(include.get("limit") or 20),
    )
    try:
        workspace = await knowledge_workspace(db, ctx, workspace_id)
        if workspace is None:
            return Answer({"items": []})
        call = await prepare_entities_query(
            db,
            ctx,
            settings,
            workspace_id=workspace,
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


# --- a view of tasks -----------------------------------------------------------------------------


async def _task_type(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> TaskShape:
    """The shape of the latest active version of a task type (what the view was checked by;
    none active — the latest): what its tasks are typed by now."""
    row = await db.scalar(
        select(TaskType)
        .where(TaskType.tenant_id == tenant_id, TaskType.key == key)
        .order_by((TaskType.status == TaskTypeStatus.ACTIVE).desc(), TaskType.version.desc())
        .limit(1)
    )
    if row is None:
        return TaskShape(None)
    return task_shape(row.field_schema, row.lifecycle_schema or SYSTEM_TASK_LIFECYCLE)


def task_values(
    task: Task, param: Mapping[str, Any], settings: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The variables of an expression of a view of tasks over one task."""

    def moment(value: Any) -> str | None:
        return vq.rfc3339(value) if value is not None else None

    def ident(value: Any) -> str | None:
        return str(value) if value is not None else None

    return {
        "id": str(task.id),
        "fields": {
            "publicId": task.public_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
            "priority": task.priority,
            "ownerId": ident(task.owner_id),
            "assigneeId": ident(task.assignee_id),
            "workspaceId": ident(task.workspace_id),
            "createdBy": ident(task.created_by),
            "startDate": moment(task.start_date),
            "dueDate": moment(task.due_date),
            "createdAt": moment(task.created_at),
            "updatedAt": moment(task.updated_at),
            "completedAt": moment(task.completed_at),
        },
        "customFields": dict(task.custom_fields or {}),
        "param": dict(param),
        "settings": dict(settings or {}),
    }


def _task_row(v: _View, task: Task) -> _Row:
    return _Row(
        values=task_values(task, v.params, v.settings),
        id=str(task.id),
        title=task.title,
        category=TASK_CATEGORY.get(task.system_status_category, "running"),
    )


def _task_condition(condition: vq.Condition) -> ColumnElement[bool]:
    """A condition of the request on a path of a task, in SQL."""
    path, op, kind = condition.field.path, condition.op, condition.field.type
    given = list(condition.value) if op == "in" else [condition.value]
    root, _, rest = path.partition(".")
    if root == "customFields" and rest:
        return json_condition(tuple(rest.split(".")), kind, op, given, Task.custom_fields)
    if path == "id":
        ids = [i for i in (vq.as_uuid(v) for v in given) if i is not None]
        return Task.id.in_(ids) if ids else false()
    if root != "fields" or rest not in _TASK_COLUMNS:
        raise ValidationError(
            "undeclared_filter", f"{path} is not a field to filter by", details={"field": path}
        )
    column, codec = _TASK_COLUMNS[rest]
    if codec == "time":
        day = func.to_char(func.timezone("UTC", column), "YYYY-MM-DD")
        return or_(
            *(day >= v if op == "gte" else day <= v if op == "lte" else day == v for v in given)
        )
    text = cast(column, Text) if codec == "uuid" else column
    if op == "prefix":
        return text.ilike(view_sql.like_prefix(str(given[0])), escape="\\")
    return text.in_([str(v) for v in given])


def _task_order(path: str, statuses: Sequence[str]) -> view_sql.PathSql:
    """A path of a task as a key of an order: statuses and priorities in their own order."""
    root, _, rest = path.partition(".")
    if root == "customFields" and rest:
        steps = tuple(rest.split("."))
        return view_sql.PathSql(view_sql.json_at(steps, Task.custom_fields), "json", steps)
    if path == "id":
        return view_sql.PathSql(Task.id, "uuid")
    if rest == "priority":
        ranks = case(*((Task.priority == p, n) for n, p in enumerate(TASK_PRIORITIES)), else_=9)
        return view_sql.PathSql(ranks, "int")
    if rest == "status" and statuses:
        known = case(*((Task.status == s, n) for n, s in enumerate(statuses)), else_=len(statuses))
        return view_sql.PathSql(known, "int")
    column, codec = _TASK_COLUMNS[rest]
    if codec == "uuid":
        return view_sql.PathSql(cast(column, Text), "text")
    return view_sql.PathSql(column, codec)


async def _task_where(
    ctx: AuthContext, type_key: str, conditions: Sequence[vq.Condition] = ()
) -> list[ColumnElement[bool]]:
    """The tasks of the type the caller may read, as ``GET /tasks`` lists them."""
    await authorize(ctx, Permission.TASKS_READ)
    where: list[ColumnElement[bool]] = [
        Task.tenant_id == ctx.tenant_id,
        Task.type_id.in_(
            select(TaskType.id).where(TaskType.tenant_id == ctx.tenant_id, TaskType.key == type_key)
        ),
    ]
    readable = await readable_tasks(ctx)
    if readable is not None:
        where.append(readable)
    where.extend(_task_condition(c) for c in conditions)
    return where


async def _each_task(
    db: AsyncSession, v: _View, where: Sequence[ColumnElement[bool]]
) -> list[_Row]:
    """Every task of the set, in rounds of :data:`BATCH`, within the ceiling of A6."""
    rows: list[_Row] = []
    after: uuid.UUID | None = None
    while True:
        stmt = select(Task).where(*where)
        if after is not None:
            stmt = stmt.where(Task.id > after)
        found = list((await db.scalars(stmt.order_by(Task.id).limit(BATCH))).all())
        v.evaluating(len(found))
        rows.extend(_task_row(v, task) for task in found)
        if len(found) < BATCH:
            return rows
        after = found[-1].id


async def _tasks(
    db: AsyncSession, ctx: AuthContext, v: _View, asked: _Asked, query: ViewQuery
) -> dict[str, Any]:
    type_key = str(v.form["source"]["tasks"]["type"])
    if v.block.kind in ("metrics", "chart"):
        rows = await _each_task(db, v, await _task_where(ctx, type_key))
        if v.block.kind == "metrics":
            return _metrics(v, rows)
        return _chart(v, rows, asked.limit)
    where = await _task_where(ctx, type_key, asked.conditions)
    paths = [(_task_order(f.path, list(v.statuses)), d) for f, d in asked.orders]
    keys = sort_keys(paths, [_Key(Task.created_at, True, "time"), _Key(Task.id, True, "uuid")])
    marker = signature(v.read, v.block, v.params, query, asked.orders)
    after = _decode(query.cursor, marker, keys) if query.cursor is not None else None
    labels = [k.expr.label(f"k{i}") for i, k in enumerate(keys)]
    ordering = [k.expr.desc() if k.descending else k.expr.asc() for k in keys]
    stmt = select(Task, *labels).where(*where)
    if after is not None:
        stmt = stmt.where(_after(keys, after))
    found = (await db.execute(stmt.order_by(*ordering).limit(asked.limit + 1))).all()
    more = len(found) > asked.limit
    found = found[: asked.limit]
    next_cursor = None
    if more and found:
        last = [_encoded(k.codec, value) for k, value in zip(keys, found[-1][1:], strict=True)]
        next_cursor = encode_cursor({"q": marker, "k": last})
    return _page(v, [_task_row(v, row[0]) for row in found], next_cursor)


# --- a view of knowledge -------------------------------------------------------------------------


def _entity_row(v: _View, item: Mapping[str, Any]) -> _Row:
    kind, key = str(item.get("kind") or ""), str(item.get("key") or "")
    attributes = item.get("attributes")
    title = str(item.get("title") or "")
    values: dict[str, Any] = {
        "id": f"{kind}:{key}",
        "kind": kind,
        "key": key,
        "title": title,
        "validFrom": item.get("validFrom"),
        "validTo": item.get("validTo"),
        "attributes": dict(attributes) if isinstance(attributes, Mapping) else {},
        "param": dict(v.params),
        "settings": dict(v.settings or {}),
    }
    return _Row(values, f"{kind}:{key}", title or key, KNOWLEDGE_CATEGORY)


async def _entities(
    v: _View, call: EntitiesQueryCall, provider: GraphProvider, settings: Settings, trace: str
) -> list[_Row]:
    """Every record of the kinds of the view the caller sees, within :data:`MAX_KNOWLEDGE`."""
    rows: list[_Row] = []
    cursor: str | None = None
    while True:
        call.cursor = cursor
        page = await fetch_entities(call, provider, settings, trace_run_id=trace)
        items = page.get("items") or []
        v.evaluating(len(items), MAX_KNOWLEDGE)
        rows.extend(_entity_row(v, item) for item in items)
        cursor = page.get("nextCursor")
        if not cursor:
            return rows


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str) and vq.amount(value) is not None:
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _holds(condition: vq.Condition, value: Any) -> bool:
    """A condition of the request on one value (a list holds it when an element does)."""
    if isinstance(value, list):
        return any(_holds(condition, element) for element in value)
    op, kind = condition.op, condition.field.type
    given = list(condition.value) if op == "in" else [condition.value]
    if value is None:
        return False
    if kind == "date":
        day = value[:10] if isinstance(value, str) else None
        if day is None:
            return False
        return any(
            day >= g if op == "gte" else day <= g if op == "lte" else day == g for g in given
        )
    if kind == "number":
        number = _decimal(value)
        if number is None:
            return False
        return any(
            number >= g if op == "gte" else number <= g if op == "lte" else number == g
            for g in given
        )
    if op == "prefix":
        return isinstance(value, str) and value.casefold().startswith(str(given[0]).casefold())
    return any(_same(value, g) for g in given)


def _same(value: Any, given: Any) -> bool:
    """Equality of JSON values: a boolean is no number, ``1`` and ``1.0`` are one number."""
    if isinstance(value, bool) or isinstance(given, bool):
        return type(value) is type(given) and value == given
    if isinstance(value, (int, float)) and isinstance(given, (int, float, Decimal)):
        return Decimal(str(value)) == Decimal(str(given))
    return bool(value == given)


def _sort_value(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (2, value)
    if isinstance(value, (int, float, Decimal)):
        return (0, Decimal(str(value)))
    if isinstance(value, str):
        return (1, value)
    return (3, json.dumps(value, sort_keys=True, default=str))


def _ordered(rows: list[_Row], orders: Sequence[tuple[vq.DeclaredField, str]]) -> list[_Row]:
    """Rows in the order of ``orders``, a missing value last; ties by kind and key."""
    found = sorted(rows, key=lambda r: (r.values["kind"], r.values["key"]))
    for declared, direction in reversed(orders):
        present = [r for r in found if path_value(declared.path, r.values) is not None]
        missing = [r for r in found if path_value(declared.path, r.values) is None]
        present.sort(
            key=lambda r: _sort_value(path_value(declared.path, r.values)),
            reverse=direction == "desc",
        )
        found = present + missing
    return found


def _relation_names(block: vq.QueryBlock) -> list[str]:
    return [
        str(c["field"]).partition(".")[2]
        for c in block.spec.get("columns") or ()
        if isinstance(c.get("field"), str) and str(c["field"]).startswith("relations.")
    ]


def _knowledge_cursor(v: _View, asked: _Asked, query: ViewQuery) -> tuple[str, int] | None:
    """The record a page of knowledge goes on after, and its place: from the cursor."""
    if query.cursor is None:
        return None
    decoded = decode_cursor(query.cursor)
    last, offset = decoded.get("k"), decoded.get("o")
    marker = signature(v.read, v.block, v.params, query, asked.orders)
    if decoded.get("q") != marker or not isinstance(last, str) or not isinstance(offset, int):
        raise vq.invalid(
            "invalid_cursor",
            "The cursor belongs to another query of the view: ask again without it",
        )
    return last, offset


async def _knowledge(
    v: _View,
    asked: _Asked,
    query: ViewQuery,
    after: tuple[str, int] | None,
    call: EntitiesQueryCall,
    provider: GraphProvider,
    settings: Settings,
    trace: str,
) -> dict[str, Any]:
    rows = await _entities(v, call, provider, settings, trace)
    if v.block.kind == "metrics":
        return _metrics(v, rows)
    if v.block.kind == "chart":
        return _chart(v, rows, asked.limit)
    for condition in asked.conditions:
        rows = [r for r in rows if _holds(condition, path_value(condition.field.path, r.values))]
    rows = _ordered(rows, asked.orders)
    marker = signature(v.read, v.block, v.params, query, asked.orders)
    start = 0
    if after is not None:
        # After the last record of the page before; gone since — at the same place.
        last, offset = after
        ids = [r.id for r in rows]
        start = ids.index(last) + 1 if last in ids else max(0, min(offset, len(rows)))
    page = rows[start : start + asked.limit]
    more = start + asked.limit < len(rows)
    names = _relation_names(v.block)
    if names and page:
        items = [{"kind": r.values["kind"], "key": r.values["key"]} for r in page]
        include = RelationsInclude(names=tuple(dict.fromkeys(names)), direction="both")
        await relations_of_items(call, include, provider, settings, items, trace_run_id=trace)
        for row, item in zip(page, items, strict=True):
            for name in names:
                row.relations[name] = [
                    r for r in item.get("relations") or () if r["relation"] == name
                ]
    next_cursor = None
    if more and page:
        next_cursor = encode_cursor({"q": marker, "k": page[-1].id, "o": start + len(page)})
    return _page(v, page, next_cursor)
