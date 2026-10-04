"""Work derivation rules: documents, the condition language, templates (CP-ADR-0063).

A rule is tenant data. It says *when* to look (``trigger``), *what must hold*
(``condition``, optionally after asking a skill to ``interpret`` the facts)
and *what work that implies* (``action``). Core knows the shape and the
closed vocabulary below — never what a tenant's rule is about: which
observation kinds, event types, skills and task types a rule names is data.

Documents (camelCase, as over the wire)::

    trigger:        {"kind": "observation", "type": "<observation kind>", "source"?: "...",
                     "agent"?: "<agent key>", "actorId"?: "<principal id>"}
                    {"kind": "event", "type": "<journal event type>"}
                    {"kind": "schedule", "type": "interval", "everySeconds": N}
    condition:      an expression (below); omitted means "always"
    interpretation: {"skill": "name@version", "inputs": {... templates ...}}
    action:         {"kind": "ensure_work" | "update_work" | "cancel_work"
                             | "complete_work" | "request_decision",
                     "taskType": "<type key>", "dedupKeyTemplate": "<template>",
                     "taskTypes"?: ["<type key>", ...],
                     "fields": {"title", "description", "priority", "assignee",
                                "approver", "approverRole", "workspaceId",
                                "customFields": {"<field>": "<template>", ...},
                                "relations": {"spawnedBy"?: "<template>",
                                              "dependsOn"?: "<template>" | [...]}},
                     "acceptance"?: [<acceptance checks, templates allowed>],
                     "check"?: "<template of the check key>",
                     "target"?: "dedup" | "task",
                     "forEach"?: "<path to a list>", "where"?: <expression>}

``request_decision`` files the work like ``ensure_work`` and a *gate*
approval on it for ``fields.approver`` or ``fields.approverRole``: the work
cannot be claimed or completed until the decision, and the decision runs the
outcomes the task's type declares (CP-ADR-0061; CP-ADR-0063, amendment
TASK-000444).

``acceptance`` (creating actions only) is the acceptance of the work filed;
``check`` (``complete_work`` only) ties the evidence the rule writes into the
task to one of its checks (CP-ADR-0063, amendment 2026-09-25).

``fields.customFields`` (creating actions only) are the custom fields of the
work filed: only their form is checked here, the rendered values meet the
``fieldSchema`` of the task type when the work is created (CP-ADR-0063,
oss-sync amendment, B1). Work found by the key keeps its fields.

``fields.workspaceId`` (creating actions only) is a template of the
workspace the work is filed in, the rule's own when it is omitted or renders
to nothing; the rule's identity must be allowed to file work there.
``fields.assignee`` names a principal by id, an agent as ``agent:<key>`` or a
role of the target workspace as ``role:<slug>`` — work any holder of the role
may take (CP-ADR-0063, amendment process-packages P012).

``target: task`` (``cancel_work`` / ``complete_work`` on an observation)
closes the task the observation is bound to (``payload.taskId``) instead of
the work found by the dedup key: ``taskTypes`` is then required and bounds
the types the rule may close, and the action has no dedup key, ``forEach``
or ``where`` — the fact names the one work item (CP-ADR-0063, amendment
integrations-connections, Zh1/Zh2). Such a rule must name the author of
the facts it trusts: ``trigger.agent`` and/or ``trigger.actorId`` — the
source an observation names is the author's own claim (amendment Zh6).

``ensure_work`` may pick the task type per item: ``taskType`` is then a
template and ``taskTypes`` lists the keys it may render to. Its
``fields.relations`` link the work it files: ``spawnedBy`` names a task (id
or public id), ``dependsOn`` names dedup keys of other work, resolved among
the items of the same evaluation first, then in the tenant's rule work
(CP-ADR-0063, amendment 2026-09-27, G2/G3).

Expressions are JSON, never code. An expression is a single-key object naming
an operator, or a literal ``true`` / ``false``:

* ``{"and": [e, ...]}``, ``{"or": [e, ...]}``, ``{"not": e}``;
* ``{"eq": [a, b]}``, ``ne``, ``lt``, ``le``, ``gt``, ``ge``;
* ``{"in": [a, b]}`` — ``a`` is an element of the list ``b``;
* ``{"exists": "<path>"}`` — the path resolves to something other than null.

Operands are ``{"var": "<path>"}``, ``{"const": <any JSON>}``, a scalar
literal, or a list of operands. A path is ``root(.segment)*``; the roots a
stage may read are fixed (``trigger``, ``payload``, ``goal``, ``task``; after
interpretation also ``skill``; inside ``forEach`` also ``item``). A missing
value is ``null``. Equality is strict JSON equality (``true`` is not ``1``);
ordering compares two numbers or two strings, is false when either side is
null, and any other pairing is an evaluation error — a rule that compares a
string with a number is broken, and silently answering ``false`` would hide
it.

``fields`` is kept only when it names something: an action without fields
(always so for ``cancel_work`` / ``complete_work``) is stored, returned and
compared without the member (CP-ADR-0063, amendment TASK-001373, Z1).

Templates are strings with ``{{ path }}`` placeholders. A string that is
exactly one placeholder takes the raw value (a list stays a list); any other
string gets each placeholder's text.

Pure functions, no database and no I/O, like ``domain/approval_outcomes.py``.
"""

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document, reject_secret_material
from control_plane.domain.settings_refs import REF_TYPE, REF_UNKNOWN, SettingsScope
from control_plane.domain.work_graph import CHECK_KEY_RE, check_spec, normalize_checks

# --- vocabulary -----------------------------------------------------------------


class RuleStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    # Removed from service for good: kept for its history, never evaluated,
    # its key free for a new rule.
    ARCHIVED = "archived"


class TriggerKind(StrEnum):
    OBSERVATION = "observation"
    EVENT = "event"
    SCHEDULE = "schedule"


class ActionKind(StrEnum):
    ENSURE_WORK = "ensure_work"
    UPDATE_WORK = "update_work"
    CANCEL_WORK = "cancel_work"
    # Close the work as done: through the verification stage (CP-ADR-0067).
    COMPLETE_WORK = "complete_work"
    REQUEST_DECISION = "request_decision"


class EvaluationStatus(StrEnum):
    # A skill was asked to interpret the facts; the evaluation resumes when
    # the call has ended.
    WAITING = "waiting"
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    FAILED = "failed"
    # Nothing was decided: the rule changed or stopped while it waited.
    SKIPPED = "skipped"


RULE_STATUSES = frozenset(s.value for s in RuleStatus)
FINAL_EVALUATION_STATUSES = frozenset(
    {
        EvaluationStatus.MATCHED,
        EvaluationStatus.NOT_MATCHED,
        EvaluationStatus.FAILED,
        EvaluationStatus.SKIPPED,
    }
)

# Actions that create work (and so need a task type and a title).
CREATING_ACTIONS = frozenset({ActionKind.ENSURE_WORK, ActionKind.REQUEST_DECISION})
# Actions that close the work; under a live claim they wait for it to end.
CLOSING_ACTIONS = frozenset({ActionKind.CANCEL_WORK, ActionKind.COMPLETE_WORK})


class ActionTarget(StrEnum):
    """Which work a closing action closes (amendment integrations-connections, Zh1)."""

    # The work rules filed under the rendered dedup key (the default).
    DEDUP = "dedup"
    # The task the triggering observation is bound to.
    TASK = "task"


RULE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
# Same shape as an observation kind (application/commands/observations.py).
_OBSERVATION_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
# Same shape as an agent key of the registry (agent_assignees.py).
_AGENT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_TYPE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Pinned only: a rule's interpretation must not change under it when a newer
# skill version is published.
_PINNED_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,126}@[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

# Rules must not react to what rules did: their own journal is a consequence,
# and a rule on it would feed itself (see also the correlation guard in the
# engine). The life of a skill call is written by its executor, not by the
# rule that queued it, so a rule on it would re-queue itself through its own
# interpretation. ``observation.recorded`` has its own trigger kind.
_RESERVED_EVENT_PREFIXES = ("rule.", "work.", "skill.invocation_")
_OBSERVATION_EVENT = "observation.recorded"

MIN_SCHEDULE_SECONDS = 60
MAX_SCHEDULE_SECONDS = 7 * 24 * 3600
MAX_DESCRIPTION_LENGTH = 2000
MAX_DOCUMENT_BYTES = 16 * 1024
MAX_EXPRESSION_DEPTH = 16
MAX_EXPRESSION_NODES = 256
MAX_OPERATOR_ARGS = 50
MAX_PATH_SEGMENTS = 16
MAX_FOR_EACH_ITEMS = 50
MAX_DEDUP_KEY_LENGTH = 200
MAX_TITLE_LENGTH = 500
MAX_TEXT_FIELD_LENGTH = 10_000

ROOT_TRIGGER = "trigger"
ROOT_PAYLOAD = "payload"
ROOT_GOAL = "goal"
ROOT_TASK = "task"
ROOT_SKILL = "skill"
ROOT_ITEM = "item"
# The effective settings of the rule's package (CP-ADR-0081 §6).
ROOT_SETTINGS = "settings"
# What the condition and the interpretation inputs may read.
BASE_ROOTS = frozenset({ROOT_TRIGGER, ROOT_PAYLOAD, ROOT_GOAL, ROOT_TASK, ROOT_SETTINGS})

_LOGICAL = ("and", "or")
_COMPARISONS = ("eq", "ne", "lt", "le", "gt", "ge")
OPERATORS = frozenset({*_LOGICAL, "not", *_COMPARISONS, "in", "exists"})

CUSTOM_FIELDS = "customFields"
RELATIONS = "relations"
WORKSPACE_FIELD = "workspaceId"
FIELD_KEYS = frozenset(
    {
        "title",
        "description",
        "priority",
        "assignee",
        "approver",
        "approverRole",
        WORKSPACE_FIELD,
        CUSTOM_FIELDS,
        RELATIONS,
    }
)
# ``fields.assignee`` of a role: the work goes to whoever holds it.
ROLE_ASSIGNEE_PREFIX = "role:"
RELATION_SPAWNED_BY = "spawnedBy"
RELATION_DEPENDS_ON = "dependsOn"
MAX_TASK_TYPES = 20
MAX_DEPENDENCIES = 50
# Same bounds as the customFields of an approval outcome's ensureWork
# (domain/approval_outcomes.py).
MAX_CUSTOM_FIELDS = 32
_CUSTOM_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _invalid(code: str, message: str, path: str, **details: Any) -> ValidationError:
    return ValidationError(code, message, details={"field": path, **details})


# --- paths ------------------------------------------------------------------------


@dataclass(frozen=True)
class VarPath:
    root: str
    segments: tuple[str, ...]

    @property
    def text(self) -> str:
        return ".".join((self.root, *self.segments))


def parse_path(value: Any, *, roots: frozenset[str], where: str, code: str) -> VarPath:
    if not isinstance(value, str) or not value:
        raise _invalid(code, f"{where}: a path must be a non-empty string", where)
    parts = value.split(".")
    root, segments = parts[0], tuple(parts[1:])
    if root not in roots:
        raise _invalid(
            code,
            f"{where}: {value!r} starts with an unknown root; allowed here: {sorted(roots)}",
            where,
            allowed=sorted(roots),
        )
    if len(segments) > MAX_PATH_SEGMENTS or not all(_SEGMENT_RE.match(s) for s in segments):
        raise _invalid(code, f"{where}: {value!r} is not a valid path", where)
    return VarPath(root, segments)


def walk(document: Any, segments: tuple[str, ...]) -> Any:
    """Follow segments through objects (by key) and lists (by index)."""
    current = document
    for segment in segments:
        if isinstance(current, Mapping):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


Resolver = Callable[[VarPath], Any]


# --- expressions -------------------------------------------------------------------


class ConditionError(Exception):
    """An expression could not be evaluated on these facts (a broken rule)."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


def validate_expression(
    expression: Any, *, roots: frozenset[str], where: str = "condition"
) -> list[VarPath]:
    """Refuse anything outside the grammar; returns every path it reads."""
    code = "invalid_rule_condition"
    paths: list[VarPath] = []
    nodes = 0

    def operand(value: Any, at: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_EXPRESSION_NODES:
            raise _invalid(code, f"{where} exceeds {MAX_EXPRESSION_NODES} nodes", at)
        if depth > MAX_EXPRESSION_DEPTH:
            raise _invalid(code, f"{where} is nested deeper than {MAX_EXPRESSION_DEPTH}", at)
        if value is None or isinstance(value, (bool, int, float, str)):
            return
        if isinstance(value, list):
            if len(value) > MAX_OPERATOR_ARGS:
                raise _invalid(code, f"{at}: more than {MAX_OPERATOR_ARGS} items", at)
            for index, item in enumerate(value):
                operand(item, f"{at}[{index}]", depth + 1)
            return
        if isinstance(value, dict) and set(value) == {"var"}:
            paths.append(parse_path(value["var"], roots=roots, where=f"{at}.var", code=code))
            return
        if isinstance(value, dict) and set(value) == {"const"}:
            return
        # An operator used as an operand is still an expression (a boolean).
        node(value, at, depth)

    def node(value: Any, at: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_EXPRESSION_NODES:
            raise _invalid(code, f"{where} exceeds {MAX_EXPRESSION_NODES} nodes", at)
        if depth > MAX_EXPRESSION_DEPTH:
            raise _invalid(code, f"{where} is nested deeper than {MAX_EXPRESSION_DEPTH}", at)
        if isinstance(value, bool):
            return
        if not isinstance(value, dict) or len(value) != 1:
            raise _invalid(
                code,
                f"{at}: an expression is true, false or a single-operator object",
                at,
                operators=sorted(OPERATORS),
            )
        ((operator, args),) = value.items()
        if operator not in OPERATORS:
            raise _invalid(
                code, f"{at}: unknown operator {operator!r}", at, operators=sorted(OPERATORS)
            )
        here = f"{at}.{operator}"
        if operator in _LOGICAL:
            if not isinstance(args, list) or not 1 <= len(args) <= MAX_OPERATOR_ARGS:
                raise _invalid(
                    code, f"{here} takes a list of 1..{MAX_OPERATOR_ARGS} expressions", here
                )
            for index, item in enumerate(args):
                node(item, f"{here}[{index}]", depth + 1)
        elif operator == "not":
            node(args, here, depth + 1)
        elif operator == "exists":
            paths.append(parse_path(args, roots=roots, where=here, code=code))
        else:
            if not isinstance(args, list) or len(args) != 2:
                raise _invalid(code, f"{here} takes exactly two operands", here)
            operand(args[0], f"{here}[0]", depth + 1)
            operand(args[1], f"{here}[1]", depth + 1)

    node(expression, where, 0)
    return paths


def _value(operand: Any, resolve: Resolver, roots: frozenset[str]) -> Any:
    if isinstance(operand, dict):
        if set(operand) == {"var"}:
            return resolve(parse_path(operand["var"], roots=roots, where="var", code="x"))
        if set(operand) == {"const"}:
            return operand["const"]
        return evaluate(operand, resolve, roots=roots)
    if isinstance(operand, list):
        return [_value(item, resolve, roots) for item in operand]
    return operand


def _strict_equal(left: Any, right: Any) -> bool:
    """JSON equality without Python's ``True == 1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return set(left) == set(right) and all(_strict_equal(left[k], right[k]) for k in left)
    return bool(left == right)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ordered(operator: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if not (
        (_is_number(left) and _is_number(right))
        or (isinstance(left, str) and isinstance(right, str))
    ):
        raise ConditionError(
            f"{operator} compares {type(left).__name__} with {type(right).__name__}"
        )
    if operator == "lt":
        return bool(left < right)
    if operator == "le":
        return bool(left <= right)
    if operator == "gt":
        return bool(left > right)
    return bool(left >= right)


def evaluate(expression: Any, resolve: Resolver, *, roots: frozenset[str]) -> bool:
    """Evaluate a validated expression; :class:`ConditionError` on a type clash."""
    if isinstance(expression, bool):
        return expression
    ((operator, args),) = expression.items()
    if operator == "and":
        return all(evaluate(item, resolve, roots=roots) for item in args)
    if operator == "or":
        return any(evaluate(item, resolve, roots=roots) for item in args)
    if operator == "not":
        return not evaluate(args, resolve, roots=roots)
    if operator == "exists":
        return resolve(parse_path(args, roots=roots, where="exists", code="x")) is not None
    left = _value(args[0], resolve, roots)
    right = _value(args[1], resolve, roots)
    if operator == "eq":
        return _strict_equal(left, right)
    if operator == "ne":
        return not _strict_equal(left, right)
    if operator == "in":
        if right is None:
            return False
        if not isinstance(right, list):
            raise ConditionError(f"in expects a list on the right, got {type(right).__name__}")
        return any(_strict_equal(left, item) for item in right)
    return _ordered(operator, left, right)


_BOOLS = ("true", "false")


def _branch_nodes(expression: Any, at: str) -> list[tuple[str, Any]]:
    """Every operator node of a validated expression with its JSON pointer, outermost first."""
    if not isinstance(expression, dict) or len(expression) != 1:
        return []
    ((operator, args),) = expression.items()
    nodes = [(at, expression)]
    if operator in _LOGICAL:
        for index, item in enumerate(args):
            nodes.extend(_branch_nodes(item, f"{at}/{operator}/{index}"))
    elif operator == "not":
        nodes.extend(_branch_nodes(args, f"{at}/not"))
    return nodes


def expression_branches(expression: Any, at: str) -> list[str]:
    """The branches of an expression: each operator node true and false (``/condition:true``).

    What the coverage of a rule counts (CP-ADR-0074 Z3): every operand of
    ``and``/``or``, every ``not`` and every comparison has been both.
    """
    return [f"{path}:{value}" for path, _ in _branch_nodes(expression, at) for value in _BOOLS]


def branch_outcomes(
    expression: Any, resolve: Resolver, *, roots: frozenset[str], at: str
) -> set[str]:
    """The branches these facts reach: each node evaluated on its own, not short-circuited.

    A node whose evaluation breaks (a type clash) reaches neither of its branches.
    """
    reached = set()
    for path, node in _branch_nodes(expression, at):
        try:
            value = evaluate(node, resolve, roots=roots)
        except ConditionError:
            continue
        reached.add(f"{path}:{'true' if value else 'false'}")
    return reached


# --- templates ----------------------------------------------------------------------


def template_paths(value: Any, *, roots: frozenset[str], where: str, code: str) -> list[VarPath]:
    """Every placeholder path in a (possibly nested) template document."""
    paths: list[VarPath] = []
    if isinstance(value, str):
        for match in _PLACEHOLDER_RE.finditer(value):
            paths.append(parse_path(match.group(1), roots=roots, where=where, code=code))
    elif isinstance(value, dict):
        for key, item in value.items():
            paths.extend(template_paths(item, roots=roots, where=f"{where}.{key}", code=code))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(template_paths(item, roots=roots, where=f"{where}[{index}]", code=code))
    return paths


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def render(value: Any, resolve: Resolver, *, roots: frozenset[str]) -> Any:
    """Fill a template document; an exact placeholder keeps the raw value."""
    if isinstance(value, str):
        exact = _PLACEHOLDER_RE.fullmatch(value)
        if exact is not None:
            return resolve(parse_path(exact.group(1), roots=roots, where="template", code="x"))
        return _PLACEHOLDER_RE.sub(
            lambda m: _text(
                resolve(parse_path(m.group(1), roots=roots, where="template", code="x"))
            ),
            value,
        )
    if isinstance(value, dict):
        return {key: render(item, resolve, roots=roots) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, resolve, roots=roots) for item in value]
    return value


# --- documents ------------------------------------------------------------------------


def _document(value: Any, *, label: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(code, f"{label} must be an object", label)
    guard_json_document(value, label=label, max_bytes=MAX_DOCUMENT_BYTES)
    reject_secret_material(value, label=label)
    return value


def _unknown_keys(value: dict[str, Any], allowed: set[str], *, label: str, code: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _invalid(code, f"{label} has unknown keys: {unknown}", label, unknown=unknown)


def normalize_rule_key(value: Any) -> str:
    if not isinstance(value, str) or not RULE_KEY_RE.match(value):
        raise _invalid("invalid_rule", f"key must match {RULE_KEY_RE.pattern}", "key")
    return value


def normalize_description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_DESCRIPTION_LENGTH:
        raise _invalid(
            "invalid_rule",
            f"description must be a string of at most {MAX_DESCRIPTION_LENGTH} characters",
            "description",
        )
    return value


def normalize_trigger(value: Any) -> dict[str, Any]:
    code = "invalid_rule_trigger"
    trigger = _document(value, label="trigger", code=code)
    kind = trigger.get("kind")
    if kind == TriggerKind.OBSERVATION:
        _unknown_keys(
            trigger, {"kind", "type", "source", "agent", "actorId"}, label="trigger", code=code
        )
        kind_type = trigger.get("type")
        if not isinstance(kind_type, str) or not _OBSERVATION_KIND_RE.match(kind_type):
            raise _invalid(code, "trigger.type must be an observation kind", "trigger.type")
        result: dict[str, Any] = {"kind": kind, "type": kind_type}
        source = trigger.get("source")
        if source is not None:
            if not isinstance(source, str) or not _SOURCE_RE.match(source):
                raise _invalid(code, "trigger.source must be an observation source", "trigger")
            result["source"] = source
        # The author filter (amendment Zh6): the source is what the author
        # says about itself, the author is who the journal says wrote it.
        agent = trigger.get("agent")
        if agent is not None:
            if not isinstance(agent, str) or not _AGENT_KEY_RE.match(agent):
                raise _invalid(code, "trigger.agent must be an agent key", "trigger.agent")
            result["agent"] = agent
        actor_id = trigger.get("actorId")
        if actor_id is not None:
            try:
                result["actorId"] = str(uuid.UUID(actor_id))
            except (ValueError, TypeError, AttributeError):
                raise _invalid(
                    code, "trigger.actorId must be a principal id", "trigger.actorId"
                ) from None
        return result
    if kind == TriggerKind.EVENT:
        _unknown_keys(trigger, {"kind", "type"}, label="trigger", code=code)
        event_type = trigger.get("type")
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.match(event_type):
            raise _invalid(code, "trigger.type must be a journal event type", "trigger.type")
        if event_type == _OBSERVATION_EVENT:
            raise _invalid(
                code, "observations have their own trigger kind 'observation'", "trigger.type"
            )
        if event_type.startswith(_RESERVED_EVENT_PREFIXES):
            raise _invalid(
                code,
                "a rule cannot be triggered by what rules do "
                "(rule.*, work.* and skill.invocation_* events)",
                "trigger.type",
            )
        return {"kind": kind, "type": event_type}
    if kind == TriggerKind.SCHEDULE:
        _unknown_keys(trigger, {"kind", "type", "everySeconds"}, label="trigger", code=code)
        if trigger.get("type") != "interval":
            raise _invalid(code, "trigger.type of a schedule must be 'interval'", "trigger.type")
        every = trigger.get("everySeconds")
        if (
            not isinstance(every, int)
            or isinstance(every, bool)
            or not MIN_SCHEDULE_SECONDS <= every <= MAX_SCHEDULE_SECONDS
        ):
            raise _invalid(
                code,
                f"trigger.everySeconds must be {MIN_SCHEDULE_SECONDS}..{MAX_SCHEDULE_SECONDS}",
                "trigger.everySeconds",
            )
        return {"kind": kind, "type": "interval", "everySeconds": every}
    raise _invalid(
        code,
        "trigger.kind must be one of ['event', 'observation', 'schedule']",
        "trigger.kind",
    )


def normalize_condition(value: Any) -> Any:
    """``None`` means "always"; stored as ``true``."""
    if value is None:
        return True
    if isinstance(value, dict):
        guard_json_document(value, label="condition", max_bytes=MAX_DOCUMENT_BYTES)
        reject_secret_material(value, label="condition")
    validate_expression(value, roots=BASE_ROOTS)
    return value


def normalize_interpretation(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    code = "invalid_rule_interpretation"
    doc = _document(value, label="interpretation", code=code)
    _unknown_keys(doc, {"skill", "inputs"}, label="interpretation", code=code)
    skill = doc.get("skill")
    if not isinstance(skill, str) or not _PINNED_SKILL_RE.match(skill):
        raise _invalid(
            code,
            "interpretation.skill must be a pinned reference name@version",
            "interpretation.skill",
        )
    inputs = doc.get("inputs", {})
    if not isinstance(inputs, dict):
        raise _invalid(code, "interpretation.inputs must be an object", "interpretation.inputs")
    template_paths(inputs, roots=BASE_ROOTS, where="interpretation.inputs", code=code)
    return {"skill": skill, "inputs": inputs}


def action_roots(*, interpreted: bool, for_each: bool) -> frozenset[str]:
    roots = set(BASE_ROOTS)
    if interpreted:
        roots.add(ROOT_SKILL)
    if for_each:
        roots.add(ROOT_ITEM)
    return frozenset(roots)


def normalize_action(value: Any, *, interpreted: bool) -> dict[str, Any]:
    code = "invalid_rule_action"
    doc = _document(value, label="action", code=code)
    _unknown_keys(
        doc,
        {
            "kind",
            "taskType",
            "dedupKeyTemplate",
            "fields",
            "forEach",
            "where",
            "acceptance",
            "check",
            "taskTypes",
            "target",
        },
        label="action",
        code=code,
    )
    kind = doc.get("kind")
    if kind not in {k.value for k in ActionKind}:
        raise _invalid(
            code,
            f"action.kind must be one of {sorted(k.value for k in ActionKind)}",
            "action.kind",
        )
    result: dict[str, Any] = {"kind": kind}
    target = doc.get("target")
    if target is not None:
        if kind not in CLOSING_ACTIONS:
            raise _invalid(
                code, f"action.target is only used by {sorted(CLOSING_ACTIONS)}", "action.target"
            )
        if target not in {t.value for t in ActionTarget}:
            raise _invalid(
                code,
                f"action.target must be one of {sorted(t.value for t in ActionTarget)}",
                "action.target",
            )
        result["target"] = target
    bound = target == ActionTarget.TASK
    if bound:
        # The fact names the work: there is no key to render and no items.
        for name in ("dedupKeyTemplate", "forEach", "where"):
            if name in doc:
                raise _invalid(
                    code, f"action.{name} is not used with target: task", f"action.{name}"
                )
        if doc.get("taskTypes") is None:
            raise _invalid(
                code,
                "action.taskTypes is required with target: task: the types the rule may close",
                "action.taskTypes",
            )

    for_each = doc.get("forEach")
    outer = action_roots(interpreted=interpreted, for_each=False)
    if for_each is not None:
        parse_path(for_each, roots=outer, where="action.forEach", code=code)
        result["forEach"] = for_each
    roots = action_roots(interpreted=interpreted, for_each=for_each is not None)
    if "where" in doc:
        if for_each is None:
            raise _invalid(code, "action.where filters forEach items; forEach is missing", "action")
        validate_expression(doc["where"], roots=roots, where="action.where")
        result["where"] = doc["where"]

    task_type = doc.get("taskType")
    task_types = doc.get("taskTypes")
    if task_types is not None:
        if kind != ActionKind.ENSURE_WORK and not bound:
            raise _invalid(
                code,
                "action.taskTypes is only used by ensure_work and by target: task",
                "action.taskTypes",
            )
        result["taskTypes"] = _normalize_task_types(task_types)
    if kind in CREATING_ACTIONS:
        if isinstance(task_type, str) and _has_placeholder(task_type):
            if task_types is None:
                raise _invalid(
                    code,
                    "a templated action.taskType needs action.taskTypes, the keys it may name",
                    "action.taskType",
                )
            template_paths(task_type, roots=roots, where="action.taskType", code=code)
        elif not isinstance(task_type, str) or not _TYPE_KEY_RE.match(task_type):
            raise _invalid(code, f"action.taskType is required for {kind}", "action.taskType")
        elif task_types is not None and task_type not in result["taskTypes"]:
            raise _invalid(
                code, "action.taskType is not one of action.taskTypes", "action.taskType"
            )
        result["taskType"] = task_type
    elif task_type is not None:
        raise _invalid(
            code, f"action.taskType is only used by {sorted(CREATING_ACTIONS)}", "action.taskType"
        )

    if not bound:
        template = doc.get("dedupKeyTemplate")
        if not isinstance(template, str) or not template.strip():
            raise _invalid(code, "action.dedupKeyTemplate is required", "action.dedupKeyTemplate")
        if len(template) > MAX_DEDUP_KEY_LENGTH:
            raise _invalid(
                code,
                f"action.dedupKeyTemplate exceeds {MAX_DEDUP_KEY_LENGTH} characters",
                "action.dedupKeyTemplate",
            )
        template_paths(template, roots=roots, where="action.dedupKeyTemplate", code=code)
        result["dedupKeyTemplate"] = template

    fields = doc.get("fields", {})
    if not isinstance(fields, dict):
        raise _invalid(code, "action.fields must be an object", "action.fields")
    _unknown_keys(fields, set(FIELD_KEYS), label="action.fields", code=code)
    for name, template_value in fields.items():
        if name == CUSTOM_FIELDS:
            if kind not in CREATING_ACTIONS:
                raise _invalid(
                    code,
                    f"action.fields.customFields is only used by {sorted(CREATING_ACTIONS)}",
                    "action.fields.customFields",
                )
            _check_custom_fields(template_value, roots=roots)
            continue
        if name == RELATIONS:
            if kind != ActionKind.ENSURE_WORK:
                raise _invalid(
                    code,
                    "action.fields.relations is only used by ensure_work",
                    "action.fields.relations",
                )
            _check_relations(template_value, roots=roots)
            continue
        if name == WORKSPACE_FIELD and kind not in CREATING_ACTIONS:
            raise _invalid(
                code,
                f"action.fields.workspaceId is only used by {sorted(CREATING_ACTIONS)}",
                "action.fields.workspaceId",
            )
        if not isinstance(template_value, str):
            raise _invalid(code, f"action.fields.{name} must be a string", f"action.fields.{name}")
        template_paths(template_value, roots=roots, where=f"action.fields.{name}", code=code)
    if kind in CREATING_ACTIONS and not str(fields.get("title") or "").strip():
        raise _invalid(code, f"action.fields.title is required for {kind}", "action.fields.title")
    if kind == ActionKind.REQUEST_DECISION:
        if ("approver" in fields) == ("approverRole" in fields):
            raise _invalid(
                code,
                "request_decision needs exactly one of fields.approver or fields.approverRole",
                "action.fields",
            )
    elif "approver" in fields or "approverRole" in fields:
        raise _invalid(
            code, "approver/approverRole are only used by request_decision", "action.fields"
        )
    if kind in CLOSING_ACTIONS and fields:
        raise _invalid(code, f"{kind} takes no fields", "action.fields")
    # No fields is no member: the canonical form is what the author writes, so
    # a document read back compares equal to its file (amendment Z1).
    if fields:
        result["fields"] = fields

    if "acceptance" in doc:
        if kind not in CREATING_ACTIONS:
            raise _invalid(
                code,
                f"action.acceptance is only used by {sorted(CREATING_ACTIONS)}",
                "action.acceptance",
            )
        result["acceptance"] = _normalize_acceptance(doc["acceptance"], roots=roots)
    if "check" in doc:
        if kind != ActionKind.COMPLETE_WORK:
            raise _invalid(code, "action.check is only used by complete_work", "action.check")
        check = doc["check"]
        if not isinstance(check, str) or not check:
            raise _invalid(code, "action.check must be a check key or a template", "action.check")
        if not template_paths(check, roots=roots, where="action.check", code=code) and (
            not CHECK_KEY_RE.match(check)
        ):
            raise _invalid(code, f"action.check must match {CHECK_KEY_RE.pattern}", "action.check")
        result["check"] = check
    return result


def _check_custom_fields(value: Any, *, roots: frozenset[str]) -> None:
    """``{field: template}``: the form only, the values meet the type's schema later.

    Which fields a type holds and what fits them is the ``fieldSchema`` of the
    version the work is filed under, known when the action runs; the rendered
    document goes through it in ``create_task`` like any task's.
    """
    code = "invalid_rule_action"
    where = "action.fields.customFields"
    if not isinstance(value, dict) or not value:
        raise _invalid(code, f"{where} must be a non-empty object of field -> template", where)
    if len(value) > MAX_CUSTOM_FIELDS:
        raise _invalid(code, f"{where} allows at most {MAX_CUSTOM_FIELDS} fields", where)
    for name, template_value in value.items():
        if not _CUSTOM_FIELD_NAME_RE.match(name):
            raise _invalid(code, f"{where}: {name!r} is not a field name", f"{where}.{name}")
        if not isinstance(template_value, str):
            raise _invalid(code, f"{where}.{name} must be a string", f"{where}.{name}")
        template_paths(template_value, roots=roots, where=f"{where}.{name}", code=code)


def _normalize_task_types(value: Any) -> list[str]:
    code = "invalid_rule_action"
    where = "action.taskTypes"
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TASK_TYPES:
        raise _invalid(code, f"{where} must list 1..{MAX_TASK_TYPES} task type keys", where)
    for index, key in enumerate(value):
        if not isinstance(key, str) or not _TYPE_KEY_RE.match(key):
            raise _invalid(code, f"{where}[{index}] is not a task type key", f"{where}[{index}]")
    if len(set(value)) != len(value):
        raise _invalid(code, f"{where} lists a key twice", where)
    return list(value)


def _check_relations(value: Any, *, roots: frozenset[str]) -> None:
    """``{spawnedBy?, dependsOn?}`` of templates; what they name is resolved when work is filed."""
    code = "invalid_rule_action"
    where = "action.fields.relations"
    if not isinstance(value, dict) or not value:
        raise _invalid(code, f"{where} must be a non-empty object", where)
    _unknown_keys(value, {RELATION_SPAWNED_BY, RELATION_DEPENDS_ON}, label=where, code=code)
    spawned_by = value.get(RELATION_SPAWNED_BY)
    if RELATION_SPAWNED_BY in value:
        at = f"{where}.{RELATION_SPAWNED_BY}"
        if not isinstance(spawned_by, str) or not spawned_by:
            raise _invalid(code, f"{at} must be a template of a task id", at)
        template_paths(spawned_by, roots=roots, where=at, code=code)
    if RELATION_DEPENDS_ON in value:
        at = f"{where}.{RELATION_DEPENDS_ON}"
        depends_on = value[RELATION_DEPENDS_ON]
        entries = depends_on if isinstance(depends_on, list) else [depends_on]
        if not entries or len(entries) > MAX_DEPENDENCIES:
            raise _invalid(
                code, f"{at} must be a template or a list of 1..{MAX_DEPENDENCIES} templates", at
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry:
                raise _invalid(code, f"{at} must hold templates of dedup keys", at)
            template_paths(entry, roots=roots, where=f"{at}[{index}]", code=code)


def _is_template(value: Any) -> bool:
    """A string that is exactly one placeholder: its value is known only at run time."""
    return isinstance(value, str) and _PLACEHOLDER_RE.fullmatch(value) is not None


def _has_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return False


def _normalize_acceptance(value: Any, *, roots: frozenset[str]) -> Any:
    """The acceptance a creating action gives its work, checked when the rule is written.

    The grammar of a task's acceptance (``normalize_checks``, ``check_spec``)
    applies to everything already known; a value that is exactly a template
    is known only when the rule fires, so only its paths are checked here —
    the rendered list goes through ``normalize_checks`` again in
    ``create_task``. A grammar error is ``invalid_rule_action`` with the
    grammar's own code and path in ``details``.
    """
    code = "invalid_rule_action"
    where = "action.acceptance"
    template_paths(value, roots=roots, where=where, code=code)
    if _is_template(value):
        return value
    if not isinstance(value, list):
        raise _invalid(code, f"{where} must be a list of checks or a template", where)
    probe: list[Any] = []
    typed: list[bool] = []
    for index, item in enumerate(value):
        if _is_template(item):
            # Stands in for a check whose whole document comes from the facts.
            probe.append({"key": f"templated-{index}", "kind": "human", "description": "templated"})
            typed.append(False)
            continue
        if not isinstance(item, dict):
            probe.append(item)
            typed.append(False)
            continue
        stand_in = dict(item)
        if _is_template(item.get("key")):
            stand_in["key"] = f"templated-{index}"
        if _is_template(item.get("kind")):
            stand_in["kind"] = "human"
        if _is_template(item.get("description")):
            stand_in["description"] = "templated"
        if _has_placeholder(item.get("when")):
            # Known only when the rule fires; create_task checks it then.
            stand_in.pop("when")
        probe.append(stand_in)
        typed.append(
            not _is_template(item.get("kind"))
            and item.get("spec") is not None
            and not _has_placeholder(item.get("spec"))
        )
    try:
        checks = normalize_checks(probe, field=where, typed_spec=False, conditions=True)
        for index, check in enumerate(checks):
            if typed[index]:
                check_spec(check["kind"], check["spec"], field=f"{where}[{index}].spec")
    except ValidationError as exc:
        raise ValidationError(
            code,
            exc.message,
            details={**exc.details, "cause": exc.code},
        ) from exc
    return value


@dataclass(frozen=True)
class RuleSpec:
    trigger: dict[str, Any]
    condition: Any
    interpretation: dict[str, Any] | None
    action: dict[str, Any]


def normalize_rule_spec(
    *, trigger: Any, condition: Any, interpretation: Any, action: Any
) -> RuleSpec:
    """Validate the four documents together (their rules cross).

    What the documents name is checked for its form only: whether the task
    type exists is the caller's lookup, and whether ``fields.customFields``
    fit that type's ``fieldSchema`` is decided when the work is filed — the
    type version a rule's work pins is the newest active one at that moment.
    """
    normalized_trigger = normalize_trigger(trigger)
    normalized_condition = normalize_condition(condition)
    normalized_interpretation = normalize_interpretation(interpretation)
    normalized_action = normalize_action(action, interpreted=normalized_interpretation is not None)
    # Work a rule creates must name at least one fact (CP-ADR-0062): a
    # schedule has none of its own, so only a skill's result can be one.
    # Work a rule completes is verified by the facts the rule cites
    # (amendment A1): the same holds for it.
    if (
        normalized_trigger["kind"] == TriggerKind.SCHEDULE
        and normalized_action["kind"] in {*CREATING_ACTIONS, ActionKind.COMPLETE_WORK}
        and normalized_interpretation is None
    ):
        raise _invalid(
            "invalid_rule",
            f"a scheduled rule that runs {normalized_action['kind']} needs an interpretation: "
            "its skill result is the only fact such work can cite",
            "interpretation",
        )
    if (
        normalized_action.get("target") == ActionTarget.TASK
        and normalized_trigger["kind"] != TriggerKind.OBSERVATION
    ):
        # Only an observation is bound to a task; closing the task of a core
        # event is a different decision (amendment Zh2).
        raise _invalid(
            "invalid_rule_action",
            "target: task closes the task an observation is bound to: "
            "the trigger must be an observation",
            "action.target",
        )
    if normalized_action.get("target") == ActionTarget.TASK and not has_author_filter(
        normalized_trigger
    ):
        # Anyone who may record an observation may bind it to a task and name
        # any source: without an author the rule would close work on anybody's
        # word (amendment Zh6).
        raise _invalid(
            "invalid_rule_trigger",
            "target: task needs the author of the facts it trusts: "
            "trigger.agent or trigger.actorId",
            "trigger.agent",
        )
    return RuleSpec(
        trigger=normalized_trigger,
        condition=normalized_condition,
        interpretation=normalized_interpretation,
        action=normalized_action,
    )


_ALL_ROOTS = frozenset({*BASE_ROOTS, ROOT_SKILL, ROOT_ITEM})
_ORDERS = ("lt", "le", "gt", "ge")


def rule_roots(
    *, condition: Any, interpretation: Mapping[str, Any] | None, action: Mapping[str, Any]
) -> frozenset[str]:
    """Every root the (normalized) documents of a rule read."""
    all_roots = _ALL_ROOTS
    code = "invalid_rule"
    paths = validate_expression(condition, roots=all_roots)
    if interpretation is not None:
        paths += template_paths(
            interpretation.get("inputs", {}), roots=all_roots, where="", code=code
        )
    if action.get("forEach") is not None:
        paths.append(parse_path(action["forEach"], roots=all_roots, where="", code=code))
    if "where" in action:
        paths += validate_expression(action["where"], roots=all_roots)
    paths += template_paths(
        action.get("dedupKeyTemplate", ""), roots=all_roots, where="", code=code
    )
    paths += template_paths(action.get("taskType", ""), roots=all_roots, where="", code=code)
    paths += template_paths(action.get("fields", {}), roots=all_roots, where="", code=code)
    paths += template_paths(action.get("acceptance", []), roots=all_roots, where="", code=code)
    paths += template_paths(action.get("check", ""), roots=all_roots, where="", code=code)
    return frozenset(path.root for path in paths)


def normalize_dedup_key(value: Any) -> str:
    """A rendered dedup key: non-empty text, bounded."""
    text = _text(value).strip()
    if not text:
        raise ValidationError(
            "invalid_dedup_key", "action.dedupKeyTemplate rendered to an empty key"
        )
    if len(text) > MAX_DEDUP_KEY_LENGTH:
        raise ValidationError(
            "invalid_dedup_key",
            f"the rendered dedup key exceeds {MAX_DEDUP_KEY_LENGTH} characters",
        )
    return text


def has_author_filter(trigger: Mapping[str, Any]) -> bool:
    """Does the (normalized) trigger name who wrote the facts it takes?"""
    return trigger.get("agent") is not None or trigger.get("actorId") is not None


def author_matches(
    trigger: Mapping[str, Any], actor_id: str | None, agents: Mapping[str, str]
) -> bool:
    """Did the author the journal names write a fact this trigger takes?

    ``agents`` maps agent keys to their principals; an agent without one (not
    linked, retired, unknown) wrote nothing. Both filters, when given, hold.
    """
    expected = trigger.get("actorId")
    if expected is not None and actor_id != expected:
        return False
    agent = trigger.get("agent")
    return agent is None or (actor_id is not None and agents.get(agent) == actor_id)


def trigger_matches(
    trigger: Mapping[str, Any], event_type: str, payload: Mapping[str, Any]
) -> bool:
    """Does a journal event fire a rule with this (normalized) trigger?"""
    kind = trigger.get("kind")
    if kind == TriggerKind.OBSERVATION:
        if event_type != _OBSERVATION_EVENT or payload.get("kind") != trigger.get("type"):
            return False
        return trigger.get("source") is None or payload.get("source") == trigger["source"]
    if kind == TriggerKind.EVENT:
        return event_type == trigger.get("type")
    return False


# --- settings of the package (CP-ADR-0081 §6) -------------------------------------------------

# What a read of ``settings`` is used as, and the JSON types that fit it.
_ORDERED_TYPES = frozenset({"number", "integer", "string"})
_SCALAR_TYPES = frozenset({"number", "integer", "string", "boolean"})
_FITS: Mapping[str, frozenset[str] | None] = {
    "order": _ORDERED_TYPES,  # lt, le, gt, ge
    "equal": _SCALAR_TYPES | {"array"},  # eq, ne
    "member": _SCALAR_TYPES,  # the left operand of in
    "list": frozenset({"array"}),  # the right operand of in, forEach
    "text": _SCALAR_TYPES,  # a placeholder inside text
    "exists": None,  # anything
    "whole": None,  # a template that is exactly one placeholder keeps the raw value
}


@dataclass(frozen=True)
class SettingsRead:
    """A read of ``settings`` in a rule: where it sits, the path and what it is used as."""

    where: str
    path: VarPath
    use: str


def _condition_reads(expression: Any, where: str) -> list[SettingsRead]:
    found: list[SettingsRead] = []

    def operand(value: Any, at: str, use: str) -> None:
        if isinstance(value, dict) and set(value) == {"var"}:
            path = parse_path(value["var"], roots=_ALL_ROOTS, where=at, code="invalid_rule")
            if path.root == ROOT_SETTINGS:
                found.append(SettingsRead(f"{at}.var", path, use))
        elif isinstance(value, dict) and set(value) != {"const"}:
            node(value, at)

    def node(value: Any, at: str) -> None:
        if not isinstance(value, dict) or len(value) != 1:
            return
        ((operator, args),) = value.items()
        here = f"{at}.{operator}"
        if operator in _LOGICAL:
            for index, item in enumerate(args):
                node(item, f"{here}[{index}]")
        elif operator == "not":
            node(args, here)
        elif operator == "exists":
            path = parse_path(args, roots=_ALL_ROOTS, where=here, code="invalid_rule")
            if path.root == ROOT_SETTINGS:
                found.append(SettingsRead(here, path, "exists"))
        else:
            uses = {"in": ("member", "list")}.get(
                operator, ("order", "order") if operator in _ORDERS else ("equal", "equal")
            )
            operand(args[0], f"{here}[0]", uses[0])
            operand(args[1], f"{here}[1]", uses[1])

    node(expression, where)
    return found


def _template_reads(value: Any, where: str) -> list[SettingsRead]:
    found: list[SettingsRead] = []
    if isinstance(value, str):
        use = "whole" if _PLACEHOLDER_RE.fullmatch(value) is not None else "text"
        for match in _PLACEHOLDER_RE.finditer(value):
            path = parse_path(match.group(1), roots=_ALL_ROOTS, where=where, code="invalid_rule")
            if path.root == ROOT_SETTINGS:
                found.append(SettingsRead(where, path, use))
    elif isinstance(value, dict):
        for key, item in value.items():
            found += _template_reads(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _template_reads(item, f"{where}[{index}]")
    return found


def settings_reads(spec: RuleSpec) -> list[SettingsRead]:
    """Every read of ``settings`` of a normalized rule, with the field it sits in."""
    found = _condition_reads(spec.condition, "condition")
    if spec.interpretation is not None:
        found += _template_reads(spec.interpretation.get("inputs", {}), "interpretation.inputs")
    action = spec.action
    for_each = action.get("forEach")
    if for_each is not None:
        path = parse_path(for_each, roots=_ALL_ROOTS, where="action.forEach", code="invalid_rule")
        if path.root == ROOT_SETTINGS:
            found.append(SettingsRead("action.forEach", path, "list"))
    if "where" in action:
        found += _condition_reads(action["where"], "action.where")
    for name in ("taskType", "dedupKeyTemplate", "fields", "acceptance", "check"):
        if name in action:
            found += _template_reads(action[name], f"action.{name}")
    return found


def _settings_type(schema: Mapping[str, Any], segments: tuple[str, ...]) -> str | None:
    """The JSON type of a path of the settings schema; ``None`` when it names no field."""
    node: Any = schema
    for segment in segments:
        if not isinstance(node, Mapping):
            return None
        if node.get("type") == "array" and segment.isdigit():
            node = node.get("items")
            continue
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return None
        node = properties[segment]
    if not isinstance(node, Mapping):
        return None
    kind = node.get("type")
    return kind if isinstance(kind, str) else "object"


def check_settings_refs(spec: RuleSpec, scope: SettingsScope) -> None:
    """``settings_ref_unknown`` / ``settings_ref_type`` of the first read that does not fit.

    The field of the error is the condition or template the read sits in.
    """
    for read in settings_reads(spec):
        named = read.path.text
        if scope.package is None:
            why = "the rule is not from a package: it has no settings"
        elif scope.schema is None:
            why = f"package {scope.package} declares no settings"
        else:
            kind = _settings_type(scope.schema, read.path.segments)
            if kind is None:
                why = f"the settings of package {scope.package} declare no such field"
            else:
                fits = _FITS[read.use]
                if fits is None or kind in fits:
                    continue
                raise _invalid(
                    REF_TYPE,
                    f"{read.where}: {named} is {kind}, which does not fit the place of the read",
                    read.where,
                    settings=named,
                )
        raise _invalid(REF_UNKNOWN, f"{read.where}: {named}: {why}", read.where, settings=named)
