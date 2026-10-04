"""Project Model domain rules: lifecycle, schemas, governance, config merge.

Pure functions over plain dicts — no database, no HTTP, no I/O. Everything
here is deterministic and unit-testable in isolation, which is what makes the
effective-config contract (ADR-0032) and the governance lattice (ADR-0033)
checkable by a table of cases rather than by inspection.

Three things live here that the rest of the codebase must not re-derive:

* the five system status categories and the lifecycle graph parsed out of a
  template (``parse_lifecycle``);
* the typed governance vocabulary with its "stricter" partial order — a fixed
  dictionary, deliberately not a policy DSL;
* the layered effective-config computation with provenance.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError

from control_plane.domain.agent_instructions import PROJECT_SETTING, validate_instructions
from control_plane.domain.errors import ValidationError
from control_plane.domain.redaction import secret_material

# --- payload guards -----------------------------------------------------------

# Schema and config documents are attacker-influenced JSON. Validate the SHAPE
# before handing anything to a schema engine: a deeply nested or enormous
# document can burn CPU inside the validator itself.
MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 5_000
MAX_STATUSES = 100
MAX_VIEWS = 50
MAX_CUSTOM_FIELD_BYTES = 64 * 1024


def _count_nodes_and_depth(value: Any, depth: int = 1) -> tuple[int, int]:
    if depth > MAX_JSON_DEPTH:
        raise ValidationError(
            "payload_too_deep",
            f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level limit",
            details={"maxDepth": MAX_JSON_DEPTH},
        )
    if isinstance(value, dict):
        nodes, deepest = 1, depth
        for item in value.values():
            child_nodes, child_depth = _count_nodes_and_depth(item, depth + 1)
            nodes += child_nodes
            deepest = max(deepest, child_depth)
        return nodes, deepest
    if isinstance(value, list):
        nodes, deepest = 1, depth
        for item in value:
            child_nodes, child_depth = _count_nodes_and_depth(item, depth + 1)
            nodes += child_nodes
            deepest = max(deepest, child_depth)
        return nodes, deepest
    return 1, depth


def guard_json_document(value: Any, *, label: str, max_bytes: int = MAX_JSON_BYTES) -> None:
    """Reject pathological JSON before any schema engine sees it."""
    if not isinstance(value, dict):
        raise ValidationError(
            "invalid_document", f"{label} must be a JSON object", details={"field": label}
        )
    encoded = json.dumps(value, separators=(",", ":"), default=str).encode()
    if len(encoded) > max_bytes:
        raise ValidationError(
            "payload_too_large",
            f"{label} exceeds the {max_bytes}-byte limit",
            details={"field": label, "maxBytes": max_bytes, "actualBytes": len(encoded)},
        )
    nodes, _ = _count_nodes_and_depth(value)
    if nodes > MAX_JSON_NODES:
        raise ValidationError(
            "payload_too_large",
            f"{label} exceeds the {MAX_JSON_NODES}-node limit",
            details={"field": label, "maxNodes": MAX_JSON_NODES},
        )


# --- secrets ------------------------------------------------------------------

# Project config, external-reference metadata and Context Packs must never
# carry secret material — only an opaque ``secretRef`` pointing at whatever the
# deployment uses for secrets. This is a coarse guard on obviously-named keys,
# not a scanner: it exists so an honest mistake fails loudly at write time.
_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "credential",
    "authorization",
    "clientsecret",
    "client_secret",
)
_SECRET_KEY_ALLOWED = frozenset({"secretref", "secret_ref"})


def secret_key_name(name: str) -> bool:
    """Whether a member name looks like it carries a secret (``secretRef`` is fine)."""
    compact = name.lower().replace("-", "_").replace("_", "")
    return compact not in _SECRET_KEY_ALLOWED and any(
        hint.replace("_", "") in compact for hint in _SECRET_KEY_HINTS
    )


def reject_secret_material(value: Any, *, label: str, path: str = "") -> None:
    """Raise if a key looks like it carries a secret (``secretRef`` is fine)."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if secret_key_name(key_text):
                raise ValidationError(
                    "secret_material_rejected",
                    "Secrets must not be stored here; use an opaque secretRef instead",
                    details={"field": label, "path": child_path},
                )
            reject_secret_material(item, label=label, path=child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_material(item, label=label, path=f"{path}[{index}]")


def secret_findings(value: Any, *, names: bool = True) -> list[dict[str, str]]:
    """Secret material in a JSON document, ``{path, match}`` each.

    The one search behind ``secret_material_rejected`` with ``details.errors``
    (CP-ADR-0079, the amendment of 2026-10-03):
    the ``settings`` of a connection, the ``spec`` of a connection type and the
    settings values of a package (CP-ADR-0081 4.3). A finding is credential
    material (``secret_material``) in any string, the names of members
    included, and, with ``names``, a member name like a secret
    (``secret_key_name``, ``match = "secret_name"``). ``path`` is the JSON
    Pointer of the string; for a name, of the object it stands in (``/`` is
    the root). The material is never in a finding, and nothing under a refused
    name is looked at, so no path carries it.
    """
    found: list[dict[str, str]] = []
    _secret_findings(value, "", names, found)
    return found


def _pointer_join(path: str, part: str | int) -> str:
    return f"{path}/{str(part).replace('~', '~0').replace('/', '~1')}"


def _secret_findings(value: Any, path: str, names: bool, found: list[dict[str, str]]) -> None:
    if isinstance(value, str):
        kind = secret_material(value)
        if kind is not None:
            found.append({"path": path or "/", "match": kind})
    elif isinstance(value, dict):
        for name, item in value.items():
            kind = secret_material(str(name)) or (
                "secret_name" if names and secret_key_name(str(name)) else None
            )
            if kind is not None:
                found.append({"path": path or "/", "match": kind})
                continue
            _secret_findings(item, _pointer_join(path, str(name)), names, found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _secret_findings(item, _pointer_join(path, index), names, found)


# --- JSON Schema --------------------------------------------------------------


def _reject_remote_refs(node: Any, *, field_name: str, path: str = "") -> None:
    """A tenant-authored schema must not point outside itself.

    ``jsonschema`` resolves an absolute ``$ref`` by FETCHING it, which would
    turn a stored schema into a server-side request forgery primitive and a
    hard dependency on someone else's uptime. Only same-document refs (``#``)
    are allowed.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{key}"
            if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if not isinstance(value, str) or not value.startswith("#"):
                    raise ValidationError(
                        "invalid_json_schema",
                        "Only same-document $ref is allowed in a stored schema",
                        details={"field": field_name, "path": child, "ref": str(value)[:200]},
                    )
            else:
                _reject_remote_refs(value, field_name=field_name, path=child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_remote_refs(item, field_name=field_name, path=f"{path}/{index}")


def validate_json_schema_document(schema: Any, *, field_name: str) -> None:
    """The document must itself be a valid JSON Schema (draft 2020-12)."""
    guard_json_document(schema, label=field_name)
    _reject_remote_refs(schema, field_name=field_name)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValidationError(
            "invalid_json_schema",
            f"{field_name} is not a valid JSON Schema",
            details={
                "field": field_name,
                "path": "/".join(str(part) for part in exc.absolute_path),
                "message": exc.message,
            },
        ) from exc


def validate_against_schema(
    schema: dict[str, Any], instance: dict[str, Any], *, code: str, field_name: str
) -> None:
    """Validate an instance, reporting every error with a stable JSON path."""
    guard_json_document(instance, label=field_name, max_bytes=MAX_CUSTOM_FIELD_BYTES)
    if not schema:
        return
    validator = jsonschema.Draft202012Validator(schema)
    try:
        found = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    except Exception as exc:
        # A stored schema whose $ref cannot be resolved must not turn every
        # later write into a 500 — it is a validation failure, not a bug.
        raise ValidationError(
            "invalid_json_schema",
            "The stored schema could not be evaluated",
            details={"field": field_name, "message": f"{type(exc).__name__}: {exc}"[:300]},
        ) from exc
    errors = [
        {
            "path": "/" + "/".join(str(part) for part in error.absolute_path),
            "message": error.message,
        }
        for error in found
    ][:20]
    if errors:
        raise ValidationError(
            code,
            f"{field_name} does not match the template schema",
            details={"field": field_name, "errors": errors},
        )


# --- lifecycle ----------------------------------------------------------------


class SystemStatusCategory(StrEnum):
    """The only status vocabulary core ever branches on (ADR-0031)."""

    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_CANCELLED = "terminal_cancelled"


TERMINAL_CATEGORIES = frozenset(
    {SystemStatusCategory.TERMINAL_SUCCESS, SystemStatusCategory.TERMINAL_CANCELLED}
)

_STATUS_KEY_MAX = 64


@dataclass(frozen=True)
class Lifecycle:
    initial_status: str
    categories: dict[str, str]
    display_names: dict[str, str]
    transitions: dict[str, frozenset[str]]

    def category_of(self, status_key: str) -> str:
        return self.categories[status_key]

    def allows(self, from_status: str, to_status: str) -> bool:
        return to_status in self.transitions.get(from_status, frozenset())


def _lifecycle_error(message: str, path: str) -> ValidationError:
    return ValidationError(
        "invalid_lifecycle_schema", message, details={"field": "lifecycleSchema", "path": path}
    )


PROJECT_STATUS_CATEGORIES = frozenset(c.value for c in SystemStatusCategory)


def parse_lifecycle(
    schema: Any, *, valid_categories: frozenset[str] = PROJECT_STATUS_CATEGORIES
) -> Lifecycle:
    """Validate and parse a ``lifecycle_schema``.

    ``valid_categories`` is a parameter because work item and project speak
    different category vocabularies (ADR-0031 vs ADR-0048) over the same
    machinery: "a paused project" and "a blocked task" are not the same thing,
    and one merged dictionary would serve both badly.
    """
    guard_json_document(schema, label="lifecycleSchema")
    statuses = schema.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        raise _lifecycle_error("statuses must be a non-empty array", "/statuses")
    if len(statuses) > MAX_STATUSES:
        raise _lifecycle_error(f"at most {MAX_STATUSES} statuses are allowed", "/statuses")

    categories: dict[str, str] = {}
    display_names: dict[str, str] = {}
    for index, entry in enumerate(statuses):
        path = f"/statuses/{index}"
        if not isinstance(entry, dict):
            raise _lifecycle_error("status entry must be an object", path)
        key = entry.get("key")
        if not isinstance(key, str) or not key or len(key) > _STATUS_KEY_MAX:
            raise _lifecycle_error(
                f"status key must be a non-empty string of at most {_STATUS_KEY_MAX} characters",
                f"{path}/key",
            )
        if key in categories:
            raise _lifecycle_error(f"duplicate status key {key!r}", f"{path}/key")
        category = entry.get("category")
        if category not in valid_categories:
            raise _lifecycle_error(
                f"category must be one of {sorted(valid_categories)}", f"{path}/category"
            )
        categories[key] = str(category)
        display_name = entry.get("displayName", key)
        if not isinstance(display_name, str):
            raise _lifecycle_error("displayName must be a string", f"{path}/displayName")
        display_names[key] = display_name

    initial = schema.get("initialStatus")
    if not isinstance(initial, str) or initial not in categories:
        raise _lifecycle_error(
            "initialStatus must name one of the declared statuses", "/initialStatus"
        )

    transitions: dict[str, frozenset[str]] = {}
    raw_transitions = schema.get("transitions", [])
    if not isinstance(raw_transitions, list):
        raise _lifecycle_error("transitions must be an array", "/transitions")
    for index, entry in enumerate(raw_transitions):
        path = f"/transitions/{index}"
        if not isinstance(entry, dict):
            raise _lifecycle_error("transition entry must be an object", path)
        source = entry.get("from")
        if source not in categories:
            raise _lifecycle_error("transition 'from' must name a declared status", f"{path}/from")
        targets = entry.get("to")
        if not isinstance(targets, list):
            raise _lifecycle_error("transition 'to' must be an array", f"{path}/to")
        for position, target in enumerate(targets):
            if target not in categories:
                raise _lifecycle_error(
                    "transition 'to' must name declared statuses", f"{path}/to/{position}"
                )
        if source in transitions:
            raise _lifecycle_error(f"duplicate transition source {source!r}", f"{path}/from")
        transitions[str(source)] = frozenset(str(t) for t in targets)

    return Lifecycle(
        initial_status=initial,
        categories=categories,
        display_names=display_names,
        transitions=transitions,
    )


# --- governance ---------------------------------------------------------------


class GovernanceKind(StrEnum):
    ORDERED_ENUM = "ordered_enum"
    BOOL_STRICT_TRUE = "bool_strict_true"
    SET_SUBSET = "set_subset"
    NUMERIC_CEILING = "numeric_ceiling"


@dataclass(frozen=True)
class GovernanceField:
    kind: GovernanceKind
    # ORDERED_ENUM: strictest first. SET_SUBSET: the full allowed vocabulary.
    values: tuple[str, ...] = ()


# The complete governance vocabulary. A key outside this dictionary is
# rejected — that is the mechanism preventing governance from degenerating
# into a free-form policy language (ADR-0033).
GOVERNANCE_FIELDS: dict[str, GovernanceField] = {
    "maxAutonomyLevel": GovernanceField(
        GovernanceKind.ORDERED_ENUM, ("supervised", "assisted", "autonomous")
    ),
    "requireApprovalForRun": GovernanceField(GovernanceKind.BOOL_STRICT_TRUE),
    "requireApprovalForCompletion": GovernanceField(GovernanceKind.BOOL_STRICT_TRUE),
    "allowedTaskPriorities": GovernanceField(
        GovernanceKind.SET_SUBSET, ("critical", "high", "medium", "low")
    ),
    "allowedSkillProtocols": GovernanceField(
        GovernanceKind.SET_SUBSET, ("mcp", "http", "local", "opencode", "custom")
    ),
    "maxRunDurationSeconds": GovernanceField(GovernanceKind.NUMERIC_CEILING),
    "maxRunActions": GovernanceField(GovernanceKind.NUMERIC_CEILING),
    "maxConcurrentRuns": GovernanceField(GovernanceKind.NUMERIC_CEILING),
    "memoryScopeSharing": GovernanceField(
        GovernanceKind.ORDERED_ENUM, ("none", "project", "ancestors")
    ),
}


def _governance_error(message: str, path: str) -> ValidationError:
    return ValidationError(
        "invalid_governance", message, details={"field": "governance", "path": path}
    )


def validate_governance(governance: Any) -> dict[str, Any]:
    """Type-check one governance record; returns it normalized."""
    if not isinstance(governance, dict):
        raise _governance_error("governance must be an object", "/")
    normalized: dict[str, Any] = {}
    for key, value in governance.items():
        spec = GOVERNANCE_FIELDS.get(str(key))
        if spec is None:
            raise ValidationError(
                "unknown_governance_field",
                f"Unknown governance field {key!r}",
                details={
                    "field": "governance",
                    "path": f"/{key}",
                    "known": sorted(GOVERNANCE_FIELDS),
                },
            )
        path = f"/{key}"
        if spec.kind is GovernanceKind.ORDERED_ENUM:
            if value not in spec.values:
                raise _governance_error(f"must be one of {list(spec.values)}", path)
            normalized[str(key)] = value
        elif spec.kind is GovernanceKind.BOOL_STRICT_TRUE:
            if not isinstance(value, bool):
                raise _governance_error("must be a boolean", path)
            normalized[str(key)] = value
        elif spec.kind is GovernanceKind.SET_SUBSET:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise _governance_error("must be an array of strings", path)
            unknown = sorted(set(value) - set(spec.values))
            if unknown:
                raise _governance_error(f"unknown values {unknown}", path)
            # Stored sorted so equality and comparison are order-independent.
            normalized[str(key)] = sorted(set(value))
        else:  # NUMERIC_CEILING
            if value is None:
                normalized[str(key)] = None
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _governance_error("must be a non-negative integer or null", path)
            else:
                normalized[str(key)] = value
    return normalized


def _is_weaker(key: str, child: Any, ancestor: Any) -> bool:
    spec = GOVERNANCE_FIELDS[key]
    if spec.kind is GovernanceKind.ORDERED_ENUM:
        return spec.values.index(child) > spec.values.index(ancestor)
    if spec.kind is GovernanceKind.BOOL_STRICT_TRUE:
        return bool(ancestor) and not bool(child)
    if spec.kind is GovernanceKind.SET_SUBSET:
        return not set(child).issubset(set(ancestor))
    # NUMERIC_CEILING: None is "unbounded" and therefore the weakest value.
    if ancestor is None:
        return False
    return child is None or int(child) > int(ancestor)


def governance_violations(child: dict[str, Any], ancestor: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field where ``child`` is weaker than ``ancestor``."""
    violations: list[dict[str, Any]] = []
    for key, ancestor_value in ancestor.items():
        if key not in GOVERNANCE_FIELDS or key not in child:
            continue
        if _is_weaker(key, child[key], ancestor_value):
            violations.append(
                {"path": f"/governance/{key}", "ancestor": ancestor_value, "project": child[key]}
            )
    return violations


def meet(key: str, left: Any, right: Any) -> Any:
    """The greatest lower bound of two values of one governance field.

    For sets that is the INTERSECTION, not "whichever looks stricter": two
    incomparable sets have no stricter side, and picking one would widen the
    result relative to the other input.
    """
    spec = GOVERNANCE_FIELDS[key]
    if spec.kind is GovernanceKind.SET_SUBSET:
        return sorted(set(left) & set(right))
    if spec.kind is GovernanceKind.BOOL_STRICT_TRUE:
        return bool(left) or bool(right)
    if spec.kind is GovernanceKind.ORDERED_ENUM:
        return left if spec.values.index(left) <= spec.values.index(right) else right
    # NUMERIC_CEILING: None is unbounded, i.e. the weakest value.
    if left is None:
        return right
    if right is None:
        return left
    return min(int(left), int(right))


def stricter(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Field-wise meet; a field present on only one side is taken as-is."""
    result = dict(base)
    for key, value in overlay.items():
        if key not in GOVERNANCE_FIELDS:
            continue
        result[key] = value if key not in result else meet(key, result[key], value)
    return result


# --- config document ----------------------------------------------------------

CONFIG_SECTIONS = ("settings", "views", "governance", "memory", "inheritance")
INHERIT_ALL = "*"


def validate_config_document(config: Any, *, field_name: str = "config") -> dict[str, Any]:
    """Validate the five-section config document; returns it normalized."""
    guard_json_document(config, label=field_name)
    unknown = sorted(set(config) - set(CONFIG_SECTIONS))
    if unknown:
        raise ValidationError(
            "unknown_config_section",
            f"Unknown config section(s): {unknown}",
            details={"field": field_name, "unknown": unknown, "known": list(CONFIG_SECTIONS)},
        )
    normalized: dict[str, Any] = {}

    settings = config.get("settings", {})
    if not isinstance(settings, dict):
        raise ValidationError(
            "invalid_config", "settings must be an object", details={"path": "/settings"}
        )
    if "governance" in settings:
        # Governance is auditable and versioned by construction: it may only
        # arrive through a config revision, never through the profile's
        # settings overlay (ADR-0033).
        raise ValidationError(
            "governance_not_in_settings",
            "Governance is only settable through a versioned config revision",
            details={"field": field_name, "path": "/settings/governance"},
        )
    reject_secret_material(settings, label=field_name, path="settings")
    if PROJECT_SETTING in settings:
        # The project layer of executor instructions (CP-ADR-0066): prose,
        # bounded, and free of credential-shaped material.
        validate_instructions(
            settings[PROJECT_SETTING], field=f"{field_name}.settings.{PROJECT_SETTING}"
        )
    normalized["settings"] = settings

    views = config.get("views", [])
    if not isinstance(views, list):
        raise ValidationError(
            "invalid_config", "views must be an array", details={"path": "/views"}
        )
    if len(views) > MAX_VIEWS:
        raise ValidationError(
            "invalid_config",
            f"at most {MAX_VIEWS} views are allowed",
            details={"path": "/views", "maxViews": MAX_VIEWS},
        )
    # Views are stored and re-served verbatim, so they get the same guard as
    # settings — a "view" is a perfectly good hiding place for a token.
    reject_secret_material(views, label=field_name, path="views")
    normalized["views"] = views

    normalized["governance"] = validate_governance(config.get("governance", {}))

    memory = config.get("memory", {})
    if not isinstance(memory, dict):
        raise ValidationError(
            "invalid_config", "memory must be an object", details={"path": "/memory"}
        )
    reject_secret_material(memory, label=field_name, path="memory")
    normalized["memory"] = memory

    normalized["inheritance"] = _validate_inheritance(config.get("inheritance", {}))
    return normalized


def _validate_inheritance(inheritance: Any) -> dict[str, Any]:
    if not isinstance(inheritance, dict):
        raise ValidationError(
            "invalid_config", "inheritance must be an object", details={"path": "/inheritance"}
        )
    unknown = sorted(set(inheritance) - {"inheritableSettings", "lockedSettings"})
    if unknown:
        raise ValidationError(
            "invalid_config",
            f"Unknown inheritance key(s): {unknown}",
            details={"path": "/inheritance", "unknown": unknown},
        )
    result: dict[str, Any] = {}
    for key, default in (("inheritableSettings", [INHERIT_ALL]), ("lockedSettings", [])):
        value = inheritance.get(key, default)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValidationError(
                "invalid_config",
                f"{key} must be an array of strings",
                details={"path": f"/inheritance/{key}"},
            )
        result[key] = sorted(set(value))
    return result


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge for objects; arrays and scalars replace wholesale."""
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


# --- effective config ---------------------------------------------------------


@dataclass(frozen=True)
class ProjectConfigSources:
    """Everything one project in the ancestry chain contributes."""

    project_id: str
    template_id: str
    template_key: str
    template_version: int
    template_default_config: dict[str, Any]
    template_default_views: list[Any]
    revision: int | None
    revision_config: dict[str, Any] | None
    profile_settings: dict[str, Any]


@dataclass(frozen=True)
class EffectiveConfig:
    config: dict[str, Any]
    provenance: dict[str, Any]
    locked_settings: frozenset[str] = field(default_factory=frozenset)
    inherited_governance: dict[str, Any] = field(default_factory=dict)


def _origin(source: str, src: ProjectConfigSources) -> dict[str, Any]:
    origin: dict[str, Any] = {"source": source, "projectId": src.project_id}
    if source == "template":
        origin["templateId"] = src.template_id
        origin["templateKey"] = src.template_key
        origin["templateVersion"] = src.template_version
    if source == "revision":
        origin["revision"] = src.revision
    return origin


def _apply_settings_layer(
    settings: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
    overlay: dict[str, Any],
    origin: dict[str, Any],
) -> dict[str, Any]:
    merged = deep_merge(settings, overlay)
    for key in overlay:
        provenance[key] = origin
    return merged


def compute_effective_config(chain: Sequence[ProjectConfigSources]) -> EffectiveConfig:
    """Fold the ancestry chain (root-most first) into one effective config.

    Layer order per project, lowest precedence first (ADR-0032):
    template defaults, inherited ancestor settings, active revision, profile
    overlay. Governance folds with ``stricter`` instead of replacing, so a
    descendant can never end up laxer than an ancestor.
    """
    if not chain:
        raise ValueError("chain must contain at least the project itself")

    inherited_settings: dict[str, Any] = {}
    inherited_settings_origin: dict[str, dict[str, Any]] = {}
    inherited_memory: dict[str, Any] = {}
    inherited_memory_origin: dict[str, dict[str, Any]] = {}
    inherited_views: list[Any] | None = None
    inherited_views_origin: dict[str, Any] | None = None
    inherited_governance: dict[str, Any] = {}
    inherited_governance_origin: dict[str, dict[str, Any]] = {}
    locked: set[str] = set()
    locked_by_ancestors: frozenset[str] = frozenset()
    ancestor_governance: dict[str, Any] = {}

    settings: dict[str, Any] = {}
    settings_origin: dict[str, dict[str, Any]] = {}
    memory: dict[str, Any] = {}
    memory_origin: dict[str, dict[str, Any]] = {}
    views: list[Any] = []
    views_origin: dict[str, Any] = {}
    governance: dict[str, Any] = {}
    governance_origin: dict[str, dict[str, Any]] = {}
    inheritance: dict[str, Any] = {"inheritableSettings": [INHERIT_ALL], "lockedSettings": []}
    layers: list[dict[str, Any]] = []

    for depth, src in enumerate(chain):
        is_target = depth == len(chain) - 1
        if is_target:
            locked_by_ancestors = frozenset(locked)
            ancestor_governance = dict(inherited_governance)

        template_config = src.template_default_config or {}
        template_settings = dict(template_config.get("settings") or {})
        template_memory = dict(template_config.get("memory") or {})
        template_governance = dict(template_config.get("governance") or {})
        template_inheritance = dict(
            template_config.get("inheritance")
            or {"inheritableSettings": [INHERIT_ALL], "lockedSettings": []}
        )

        revision_config = src.revision_config or {}
        revision_settings = dict(revision_config.get("settings") or {})
        revision_memory = dict(revision_config.get("memory") or {})
        revision_governance = dict(revision_config.get("governance") or {})
        revision_views = revision_config.get("views")
        revision_inheritance = revision_config.get("inheritance")

        # -- settings: template -> ancestors -> revision -> profile ------------
        settings = {}
        settings_origin = {}
        template_origin = _origin("template", src)
        settings = _apply_settings_layer(
            settings, settings_origin, template_settings, template_origin
        )
        for key, value in inherited_settings.items():
            settings = deep_merge(settings, {key: value})
            settings_origin[key] = inherited_settings_origin[key]
        settings = _apply_settings_layer(
            settings, settings_origin, revision_settings, _origin("revision", src)
        )
        settings = _apply_settings_layer(
            settings, settings_origin, src.profile_settings or {}, _origin("profile", src)
        )

        # -- memory: same shape as settings ------------------------------------
        memory = {}
        memory_origin = {}
        memory = _apply_settings_layer(memory, memory_origin, template_memory, template_origin)
        for key, value in inherited_memory.items():
            memory = deep_merge(memory, {key: value})
            memory_origin[key] = inherited_memory_origin[key]
        memory = _apply_settings_layer(
            memory, memory_origin, revision_memory, _origin("revision", src)
        )

        # -- views: replaced wholesale by the nearest declaring layer ----------
        views = list(src.template_default_views or [])
        views_origin = template_origin
        if inherited_views and inherited_views_origin is not None:
            views = list(inherited_views)
            views_origin = inherited_views_origin
        if isinstance(revision_views, list):
            views = list(revision_views)
            views_origin = _origin("revision", src)

        # -- governance: folded with `stricter`, never replaced ----------------
        governance = {}
        governance_origin = {}
        for source_name, contribution, origins in (
            ("template", template_governance, None),
            ("ancestor", inherited_governance, inherited_governance_origin),
            ("revision", revision_governance, None),
        ):
            for key, value in contribution.items():
                if key not in GOVERNANCE_FIELDS:
                    continue
                origin = (
                    origins[key]
                    if origins is not None and key in origins
                    else _origin(source_name, src)
                )
                if key not in governance:
                    governance[key] = value
                    governance_origin[key] = origin
                    continue
                folded = meet(key, governance[key], value)
                # Provenance names the layer that actually tightened the field;
                # a no-op contribution leaves the earlier attribution alone.
                if folded != governance[key]:
                    governance_origin[key] = origin
                governance[key] = folded

        # -- inheritance: nearest declaring layer wins for what PROPAGATES,
        # but locks only ever accumulate. A revision that simply does not
        # mention inheritance must not silently unlock what the template
        # locked (the config document always materializes the section, so
        # "not mentioned" and "explicitly empty" look identical on the wire).
        inheritance = dict(template_inheritance)
        if isinstance(revision_inheritance, dict):
            inheritance = dict(revision_inheritance)
        inheritance.setdefault("inheritableSettings", [INHERIT_ALL])
        own_locked = set(template_inheritance.get("lockedSettings") or []) | set(
            (revision_inheritance or {}).get("lockedSettings") or []
        )
        inheritance["lockedSettings"] = sorted(own_locked)
        locked |= own_locked

        layers.append(
            {
                "projectId": src.project_id,
                "templateId": src.template_id,
                "templateKey": src.template_key,
                "templateVersion": src.template_version,
                "revision": src.revision,
                "depth": depth,
            }
        )

        if not is_target:
            allowed = set(inheritance["inheritableSettings"])
            inherit_all = INHERIT_ALL in allowed
            inherited_settings = {
                key: value for key, value in settings.items() if inherit_all or key in allowed
            }
            inherited_settings_origin = {
                key: {**settings_origin[key], "source": "ancestor"} for key in inherited_settings
            }
            inherited_memory = dict(memory)
            inherited_memory_origin = {
                key: {**memory_origin[key], "source": "ancestor"} for key in memory
            }
            inherited_views = list(views)
            inherited_views_origin = {**views_origin, "source": "ancestor"}
            inherited_governance = dict(governance)
            inherited_governance_origin = {
                key: {**origin, "source": "ancestor"} for key, origin in governance_origin.items()
            }

    config = {
        "settings": settings,
        "views": views,
        "governance": governance,
        "memory": memory,
        "inheritance": inheritance,
    }
    provenance = {
        "settings": settings_origin,
        "views": views_origin,
        "governance": governance_origin,
        "memory": memory_origin,
        "layers": layers,
        "lockedSettings": sorted(locked_by_ancestors),
    }
    return EffectiveConfig(
        config=config,
        provenance=provenance,
        locked_settings=locked_by_ancestors,
        inherited_governance=ancestor_governance,
    )


def locked_setting_violations(settings: Iterable[str], locked: frozenset[str]) -> list[str]:
    return sorted(key for key in settings if key in locked)
