"""Settings of a package: the declaration, the values, what a reader gets (CP-ADR-0081).

A package declares its settings in ``package.yaml`` (``spec.settings {schema,
uischema?}``); the core keeps the schema by revision, one value per package in
the tenant, and gives the values to the objects of the package. The core knows
no meaning of a setting: no field name, unit or domain is written here
(FR-017, §9).

- **Declaration** (:func:`check_declaration`) — the subset of JSON Schema of
  §1, the closed subset of JSON Forms of §2 and the dictionary keys of the
  labels; findings in the form of the plan (``settings_schema_unsupported``,
  ``settings_default_missing``, ``settings_default_invalid``,
  ``settings_secret_field``, ``settings_uischema_unsupported``,
  ``settings_label_missing``; the warning ``settings_uischema_uncovered``).
- **Values** — ``project.secret_findings`` (§4.3, the one search shared with
  connections: material in any string, the names of members included, before
  the schema), :func:`validate` (§4.4:
  every violation at once, a JSON Pointer and the JSON Schema keyword, never
  the value), :func:`references` (§4.5: the ``x-ref`` strings to look up).
- **Effective values** (:func:`effective`, §3) — the saved values the schema
  still declares over its ``default``: nested objects merge by field, an
  array is replaced whole.
- **Plan** (:func:`compare`, §7) — added, removed and incompatible fields of a
  new revision against the saved values.
- **Reader** (:func:`present`, §4) — the schema and the layout with the
  strings of the dictionaries put in place of their keys.

Pure functions over plain values; no I/O.
"""

import copy
import math
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from control_plane.domain.errors import DomainError
from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.package_source import ParsedPackage
from control_plane.domain.process_definition import Problem, pointer
from control_plane.domain.project import secret_findings, secret_key_name
from control_plane.domain.redaction import secret_material
from control_plane.domain.views import declared_locales

# The field of ``Package.spec`` and where its findings point.
SETTINGS_FIELD = "settings"
BASE = "/spec/settings"
SCHEMA_BASE = f"{BASE}/schema"
UISCHEMA_BASE = f"{BASE}/uischema"
# The keyword of a value that references a platform object, and its kinds (§1).
REF = "x-ref"
REF_KINDS = ("role", "principal", "workspace", "taskType", "calendar")
TYPES = ("object", "string", "integer", "number", "boolean", "array")
SCALARS = ("string", "integer", "number", "boolean")
FORMATS = ("date", "uri", "email", "uuid")
# Objects nest at most this deep, the root counted; properties per object.
MAX_OBJECT_DEPTH = 3
MAX_PROPERTIES = 100
MAX_ENUM = 100
# The layout: nesting of elements and elements in all.
MAX_UI_DEPTH = 5
MAX_UI_ELEMENTS = 200
FIELD_NAME = re.compile(r"^[a-z][A-Za-z0-9_]{0,62}$")
LABEL_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[A-Za-z0-9_-]+)+$")
SCOPE_PREFIX = "#/properties/"
SCOPE_STEP = "/properties/"

# Keywords of a field by its type; ``type``, ``enum`` and ``default`` go with any.
_COMMON = frozenset({"type", "default"})
_BY_TYPE: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "string": frozenset({"enum", "minLength", "maxLength", "pattern", "format", REF}),
    "integer": frozenset({"enum", "minimum", "maximum"}),
    "number": frozenset({"enum", "minimum", "maximum"}),
    "boolean": frozenset({"enum"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
}
_ALL_KEYWORDS = _COMMON.union(*_BY_TYPE.values())
# Labels belong to the dictionaries, not to the schema (§1).
_LABEL_KEYWORDS = ("title", "description")
# The condition of a rule of the layout: the subset of §1 without x-ref and default, and const.
_RULE_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "const",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
    }
)
_RULE_EFFECTS = ("SHOW", "HIDE", "ENABLE", "DISABLE")
_UI_FIELDS: dict[str, frozenset[str]] = {
    "VerticalLayout": frozenset({"type", "elements", "rule"}),
    "HorizontalLayout": frozenset({"type", "elements", "rule"}),
    "Group": frozenset({"type", "label", "elements", "rule"}),
    "Control": frozenset({"type", "scope", "label", "rule"}),
    "Label": frozenset({"type", "text", "rule"}),
}
_UI_ROOTS = ("VerticalLayout", "HorizontalLayout", "Group")
_LAYOUTS = ("VerticalLayout", "HorizontalLayout", "Group")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")

JsonSchema = dict[str, Any]


class SettingsNotFoundError(DomainError):
    """``404`` with a code of its own: ``package_not_installed``, ``settings_not_declared``."""

    http_status = 404


def not_installed(package: str) -> SettingsNotFoundError:
    return SettingsNotFoundError(
        "package_not_installed",
        "The package is not installed in the organization",
        details={"package": package},
    )


def not_declared(package: str) -> SettingsNotFoundError:
    return SettingsNotFoundError(
        "settings_not_declared",
        "The installed revision of the package declares no settings",
        details={"package": package},
    )


# --- the declaration --------------------------------------------------------------------------


@dataclass(frozen=True)
class Declared:
    """A declaration without errors: what an apply keeps as a revision."""

    schema: JsonSchema
    uischema: JsonSchema | None

    @property
    def hash(self) -> str:
        return schema_hash(self.schema, self.uischema)


def schema_hash(schema: Mapping[str, Any], uischema: Mapping[str, Any] | None) -> str:
    """``schema_hash`` of a revision: of the canonical JSON ``{schema, uischema}``."""
    return canonical_hash({"schema": schema, "uischema": uischema})


@dataclass
class Declaration:
    """What the check of ``spec.settings`` found."""

    declared: Declared | None = None
    problems: list[Problem] = field(default_factory=list)
    # The keys of the dictionaries the settings show: not unused (CP-ADR-0080).
    messages: set[str] = field(default_factory=set)
    # Whether ``package.yaml`` has ``spec.settings`` at all.
    present: bool = False


def _unsupported(path: str, message: str, hint: str | None = None) -> Problem:
    return Problem("settings_schema_unsupported", "error", path, message, hint)


def check_declaration(package: ParsedPackage) -> Declaration:
    """``spec.settings`` of ``package.yaml``: the subsets of §1 and §2 and the labels."""
    manifest = package.manifest_object
    out = Declaration()
    if manifest is None or SETTINGS_FIELD not in manifest.spec:
        return out
    out.present = True
    raw = manifest.spec[SETTINGS_FIELD]
    key = manifest.key
    problems: list[Problem] = []
    if not isinstance(raw, Mapping):
        problems.append(_unsupported(BASE, "settings is a mapping {schema, uischema?}"))
    else:
        for name in sorted(set(raw) - {"schema", "uischema"}):
            problems.append(
                _unsupported(pointer("spec", "settings", name), f"settings has no field {name!r}")
            )
        schema = raw.get("schema")
        if schema is None:
            problems.append(_unsupported(BASE, "settings declares its schema"))
        else:
            problems += _SchemaCheck().root(schema)
        uischema = raw.get("uischema")
        schema_ok = not any(p.error for p in problems)
        if uischema is not None and schema_ok:
            assert isinstance(schema, Mapping)
            problems += _UiCheck(key, schema).root(uischema)
        if not any(p.error for p in problems):
            assert isinstance(schema, Mapping)
            declared = Declared(
                copy.deepcopy(dict(schema)),
                copy.deepcopy(dict(uischema)) if isinstance(uischema, Mapping) else None,
            )
            found, used = _labels(package, key, declared)
            problems += found
            out.messages = used
            if not any(p.error for p in found):
                out.declared = declared
    out.problems = [manifest.place(p) for p in problems]
    return out


class _SchemaCheck:
    """The subset of JSON Schema of §1, with the secret markers and the defaults."""

    def __init__(self) -> None:
        self.problems: list[Problem] = []

    def root(self, schema: Any) -> list[Problem]:
        if not isinstance(schema, Mapping):
            self.problems.append(_unsupported(SCHEMA_BASE, "the schema is a mapping"))
            return self.problems
        if schema.get("type") != "object":
            self.problems.append(
                _unsupported(SCHEMA_BASE + "/type", "the root of the schema is type: object")
            )
            return self.problems
        self._field(schema, SCHEMA_BASE, depth=1, field=False, required=True)
        return self.problems

    def _field(
        self, node: Any, where: str, *, depth: int, field: bool, required: bool, name: str = ""
    ) -> None:
        """One node of the schema: ``depth`` — objects from the root, it included."""
        if not isinstance(node, Mapping):
            self.problems.append(_unsupported(where, "a field of the schema is a mapping"))
            return
        if self._secret(node, where):
            return
        before = len(self.problems)
        for keyword in node:
            if keyword in _LABEL_KEYWORDS:
                self.problems.append(
                    _unsupported(
                        f"{where}/{keyword}",
                        f"{keyword} is not written in the schema",
                        hint="labels are keys <package>.settings.<path>[.help] of the"
                        " package dictionaries",
                    )
                )
            elif keyword not in _ALL_KEYWORDS:
                self.problems.append(
                    _unsupported(
                        pointer(*_parts(where), keyword),
                        f"{keyword} is outside the subset of the settings schema",
                    )
                )
        kind = node.get("type")
        if kind not in TYPES:
            self.problems.append(
                _unsupported(f"{where}/type", "type is one of " + ", ".join(TYPES))
            )
            return
        allowed = _COMMON | _BY_TYPE[kind]
        for keyword in sorted(set(node) & _ALL_KEYWORDS - allowed):
            message = (
                f"{REF} is only on a string (or the items of an array of strings)"
                if keyword == REF
                else f"{keyword} does not apply to type {kind}"
            )
            self.problems.append(_unsupported(f"{where}/{keyword}", message))
        if kind == "object":
            if depth > MAX_OBJECT_DEPTH:
                self.problems.append(
                    _unsupported(where, f"objects nest at most {MAX_OBJECT_DEPTH} levels deep")
                )
                return
            self._object(node, where, depth)
        elif kind == "array":
            self._array(node, where, depth)
        else:
            self._scalar(node, kind, where)
        if "default" in node:
            # A default is checked against a field without findings only.
            errors = (
                validate(node["default"], _without_default(node))
                if len(self.problems) == before
                else []
            )
            for error in errors:
                self.problems.append(
                    Problem(
                        "settings_default_invalid",
                        "error",
                        f"{where}/default",
                        f"default does not match the field: {error['message']}",
                    )
                )
        elif field and not required and kind != "object":
            self.problems.append(
                Problem(
                    "settings_default_missing",
                    "error",
                    where,
                    f"the optional field {name} has no default",
                    hint="give it a default or list it in required",
                )
            )

    def _secret(self, node: Mapping[str, Any], where: str) -> bool:
        found = False
        if node.get("writeOnly") is True:
            self._secret_field(f"{where}/writeOnly", "writeOnly marks a secret")
            found = True
        if node.get("format") == "password":
            self._secret_field(f"{where}/format", "format: password marks a secret")
            found = True
        if "default" in node and secret_findings(node["default"]):
            self._secret_field(f"{where}/default", "the default carries credential material")
            found = True
        enum = node.get("enum")
        if isinstance(enum, list):
            for index, item in enumerate(enum):
                if isinstance(item, str) and secret_material(item):
                    self._secret_field(
                        f"{where}/enum/{index}", "a value of enum carries credential material"
                    )
                    found = True
        return found

    def _secret_field(self, where: str, message: str) -> None:
        self.problems.append(
            Problem(
                "settings_secret_field",
                "error",
                where,
                f"{message}: settings hold no secrets",
                hint="a secret belongs to a connection or a named secret of an agent",
            )
        )

    def _object(self, node: Mapping[str, Any], where: str, depth: int) -> None:
        if node.get("additionalProperties", False) is not False:
            self.problems.append(
                _unsupported(
                    f"{where}/additionalProperties",
                    "additionalProperties is false on every object",
                )
            )
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            self.problems.append(
                _unsupported(f"{where}/properties", "an object declares its properties")
            )
            return
        if len(properties) > MAX_PROPERTIES:
            self.problems.append(
                _unsupported(
                    f"{where}/properties", f"an object has at most {MAX_PROPERTIES} properties"
                )
            )
            return
        required = node.get("required", [])
        if (
            not isinstance(required, list)
            or not all(isinstance(n, str) for n in required)
            or len(set(required)) != len(required)
        ):
            self.problems.append(
                _unsupported(f"{where}/required", "required is a list of distinct field names")
            )
            required = []
        for index, name in enumerate(required):
            if name not in properties:
                self.problems.append(
                    _unsupported(
                        f"{where}/required/{index}", f"required names {name}, not a property"
                    )
                )
        for name, child in properties.items():
            at = pointer(*_parts(where), "properties", name)
            if not isinstance(name, str) or not FIELD_NAME.match(name):
                self.problems.append(_unsupported(at, f"a field name matches {FIELD_NAME.pattern}"))
                continue
            if secret_key_name(name):
                self._secret_field(at, f"the name {name} marks a secret")
                continue
            self._field(
                child, at, depth=depth + 1, field=True, required=name in required, name=name
            )

    def _array(self, node: Mapping[str, Any], where: str, depth: int) -> None:
        for keyword in ("minItems", "maxItems"):
            if keyword in node and not _count(node[keyword]):
                self.problems.append(
                    _unsupported(f"{where}/{keyword}", f"{keyword} is an integer >= 0")
                )
        items = node.get("items")
        if items is None:
            self.problems.append(_unsupported(f"{where}/items", "an array declares its items"))
            return
        if (
            isinstance(items, Mapping)
            and depth > MAX_OBJECT_DEPTH
            and items.get("type") not in SCALARS
        ):
            self.problems.append(
                _unsupported(
                    f"{where}/items",
                    "at the deepest level the items of an array are scalars",
                )
            )
            return
        self._field(items, f"{where}/items", depth=depth + 1, field=False, required=True)

    def _scalar(self, node: Mapping[str, Any], kind: str, where: str) -> None:
        if "enum" in node:
            enum = node["enum"]
            if (
                not isinstance(enum, list)
                or not 1 <= len(enum) <= MAX_ENUM
                or any(_type_error(item, kind) for item in enum)
                or len({canonical_hash(item) for item in enum}) != len(enum)
            ):
                self.problems.append(
                    _unsupported(
                        f"{where}/enum",
                        f"enum is a list of 1..{MAX_ENUM} distinct values of type {kind}",
                    )
                )
        for keyword in ("minimum", "maximum"):
            if keyword in node and not _number(node[keyword]):
                self.problems.append(_unsupported(f"{where}/{keyword}", f"{keyword} is a number"))
        for keyword in ("minLength", "maxLength"):
            if keyword in node and not _count(node[keyword]):
                self.problems.append(
                    _unsupported(f"{where}/{keyword}", f"{keyword} is an integer >= 0")
                )
        if "pattern" in node:
            pattern = node["pattern"]
            try:
                ok = isinstance(pattern, str) and bool(pattern) and re.compile(pattern) is not None
            except re.error:
                ok = False
            if not ok:
                self.problems.append(
                    _unsupported(f"{where}/pattern", "pattern is a regular expression")
                )
        if "format" in node and node["format"] not in FORMATS:
            self.problems.append(
                _unsupported(f"{where}/format", "format is one of " + ", ".join(FORMATS))
            )
        if REF in node and node[REF] not in REF_KINDS:
            self.problems.append(
                _unsupported(f"{where}/{REF}", f"{REF} is one of " + ", ".join(REF_KINDS))
            )


def _parts(where: str) -> list[str]:
    """The unescaped segments of a JSON pointer."""
    return [p.replace("~1", "/").replace("~0", "~") for p in where.split("/")[1:]]


def _without_default(node: Mapping[str, Any]) -> JsonSchema:
    return {k: v for k, v in node.items() if k != "default"}


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --- the layout -------------------------------------------------------------------------------


def _ui_unsupported(path: str, message: str) -> Problem:
    return Problem("settings_uischema_unsupported", "error", path, message)


class _UiCheck:
    """The closed subset of JSON Forms of §2."""

    def __init__(self, package: str, schema: Mapping[str, Any]) -> None:
        self.package = package
        self.schema = schema
        self.problems: list[Problem] = []
        self.elements = 0
        self.controls: dict[str, str] = {}

    def root(self, node: Any) -> list[Problem]:
        if not isinstance(node, Mapping) or node.get("type") not in _UI_ROOTS:
            self.problems.append(
                _ui_unsupported(
                    UISCHEMA_BASE + "/type", "the root of uischema is " + ", ".join(_UI_ROOTS)
                )
            )
            return self.problems
        self._element(node, UISCHEMA_BASE, 1)
        if not any(p.error for p in self.problems):
            covered = set(self.controls)
            for path in _leaves(self.schema):
                if not any(path == c or path.startswith(c + ".") for c in covered):
                    self.problems.append(
                        Problem(
                            "settings_uischema_uncovered",
                            "warning",
                            UISCHEMA_BASE,
                            f"no Control edits {path}: its value applies but the form cannot"
                            " change it",
                        )
                    )
        return self.problems

    def _element(self, node: Any, where: str, depth: int) -> None:
        self.elements += 1
        if self.elements == MAX_UI_ELEMENTS + 1:
            self.problems.append(
                _ui_unsupported(UISCHEMA_BASE, f"uischema has at most {MAX_UI_ELEMENTS} elements")
            )
        if depth > MAX_UI_DEPTH:
            self.problems.append(
                _ui_unsupported(where, f"elements nest at most {MAX_UI_DEPTH} levels deep")
            )
            return
        if not isinstance(node, Mapping):
            self.problems.append(_ui_unsupported(where, "an element is a mapping"))
            return
        kind = node.get("type")
        if kind not in _UI_FIELDS:
            self.problems.append(
                _ui_unsupported(f"{where}/type", "type is one of " + ", ".join(_UI_FIELDS))
            )
            return
        for name in sorted(set(node) - _UI_FIELDS[kind]):
            self.problems.append(
                _ui_unsupported(pointer(*_parts(where), name), f"a {kind} has no {name}")
            )
        if "rule" in node:
            self._rule(node["rule"], f"{where}/rule")
        if kind == "Group":
            label = node.get("label")
            prefix = f"{self.package}.settings.groups."
            if not self._key(label, f"{where}/label") or not str(label).startswith(prefix):
                self.problems.append(
                    _ui_unsupported(f"{where}/label", f"the label of a Group is a key {prefix}<id>")
                )
        if kind == "Control":
            self._control(node, where)
        if kind == "Label" and not self._key(node.get("text"), f"{where}/text"):
            self.problems.append(_ui_unsupported(f"{where}/text", "text is a dictionary key"))
        if kind in _LAYOUTS:
            elements = node.get("elements")
            if not isinstance(elements, list) or not elements:
                self.problems.append(
                    _ui_unsupported(f"{where}/elements", "elements is a non-empty list")
                )
                return
            for index, child in enumerate(elements):
                self._element(child, f"{where}/elements/{index}", depth + 1)

    def _key(self, value: Any, where: str) -> bool:
        return isinstance(value, str) and len(value) <= 200 and bool(LABEL_KEY.match(value))

    def _control(self, node: Mapping[str, Any], where: str) -> None:
        if "label" in node and not self._key(node["label"], f"{where}/label"):
            self.problems.append(_ui_unsupported(f"{where}/label", "label is a dictionary key"))
        path = self._scope(node.get("scope"), f"{where}/scope")
        if path is None:
            return
        if path in self.controls:
            self.problems.append(
                _ui_unsupported(
                    f"{where}/scope", f"{path} already has a Control at {self.controls[path]}"
                )
            )
            return
        self.controls[path] = where

    def _scope(self, scope: Any, where: str) -> str | None:
        """The dotted path of the property ``scope`` points at, or ``None`` with a finding."""
        path = scope_path(scope)
        if path is None or field_at(self.schema, path) is None:
            self.problems.append(
                _ui_unsupported(
                    where, "scope points at a property of the schema: #/properties/<field>"
                )
            )
            return None
        return path

    def _rule(self, rule: Any, where: str) -> None:
        if not isinstance(rule, Mapping) or set(rule) != {"effect", "condition"}:
            self.problems.append(_ui_unsupported(where, "a rule is {effect, condition}"))
            return
        if rule["effect"] not in _RULE_EFFECTS:
            self.problems.append(
                _ui_unsupported(f"{where}/effect", "effect is one of " + ", ".join(_RULE_EFFECTS))
            )
        condition = rule["condition"]
        at = f"{where}/condition"
        if (
            not isinstance(condition, Mapping)
            or not {"scope", "schema"} <= set(condition)
            or not set(condition) <= {"scope", "schema", "failWhenUndefined"}
        ):
            self.problems.append(
                _ui_unsupported(
                    at,
                    "a condition is {scope, schema, failWhenUndefined?}: OR, AND and LEAF"
                    " are outside the subset",
                )
            )
            return
        self._scope(condition["scope"], f"{at}/scope")
        schema = condition["schema"]
        if (
            not isinstance(schema, Mapping)
            or not schema
            or not set(schema) <= _RULE_SCHEMA_KEYWORDS
        ):
            self.problems.append(
                _ui_unsupported(
                    f"{at}/schema",
                    "the schema of a condition uses " + ", ".join(sorted(_RULE_SCHEMA_KEYWORDS)),
                )
            )
        if "failWhenUndefined" in condition and not isinstance(
            condition["failWhenUndefined"], bool
        ):
            self.problems.append(
                _ui_unsupported(f"{at}/failWhenUndefined", "failWhenUndefined is a boolean")
            )


def scope_path(scope: Any) -> str | None:
    """``#/properties/a/properties/b`` as ``a.b``; ``None`` for another form."""
    if not isinstance(scope, str) or not scope.startswith(SCOPE_PREFIX):
        return None
    names = scope[len(SCOPE_PREFIX) :].split(SCOPE_STEP)
    if not all(FIELD_NAME.match(name) for name in names):
        return None
    return ".".join(names)


def field_at(schema: Mapping[str, Any], path: str) -> Mapping[str, Any] | None:
    """The field of ``schema`` at the dotted ``path``."""
    node: Any = schema
    for name in path.split("."):
        properties = node.get("properties") if isinstance(node, Mapping) else None
        if not isinstance(properties, Mapping) or name not in properties:
            return None
        node = properties[name]
    return node if isinstance(node, Mapping) else None


def fields(schema: Mapping[str, Any], base: str = "") -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every property of ``schema``, nested ones included, with its dotted path."""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return
    for name, node in properties.items():
        path = f"{base}.{name}" if base else str(name)
        if not isinstance(node, Mapping):
            continue
        yield path, node
        if node.get("type") == "object":
            yield from fields(node, path)


def _leaves(schema: Mapping[str, Any]) -> list[str]:
    return [path for path, node in fields(schema) if node.get("type") != "object"]


# --- labels -----------------------------------------------------------------------------------


def title_key(package: str) -> str:
    return f"{package}.title"


def field_key(package: str, path: str) -> str:
    return f"{package}.settings.{path}"


def help_key(package: str, path: str) -> str:
    return f"{field_key(package, path)}.help"


def _ui_keys(node: Any, where: str) -> Iterator[tuple[str, str]]:
    """``(where, key)`` of every label and text of the layout."""
    if not isinstance(node, Mapping):
        return
    for name in ("label", "text"):
        if isinstance(node.get(name), str):
            yield f"{where}/{name}", node[name]
    for index, child in enumerate(node.get("elements") or ()):
        yield from _ui_keys(child, f"{where}/elements/{index}")


def _labels(package: ParsedPackage, key: str, declared: Declared) -> tuple[list[Problem], set[str]]:
    """``settings_label_missing`` for every key a declared language lacks; the keys shown."""
    locales, _ = declared_locales(package)
    required: list[tuple[str, str]] = [(BASE, title_key(key))]
    for path, _node in fields(declared.schema):
        where = SCHEMA_BASE + pointer(
            *[p for name in path.split(".") for p in ("properties", name)]
        )
        required.append((where, field_key(key, path)))
    if declared.uischema is not None:
        required += list(_ui_keys(declared.uischema, UISCHEMA_BASE))
    used = {k for _, k in required}
    for path, _node in fields(declared.schema):
        used.add(help_key(key, path))
    problems: list[Problem] = []
    if not locales:
        problems.append(
            Problem(
                "settings_label_missing",
                "error",
                BASE,
                "the package declares settings: package.yaml declares locales and their"
                " dictionaries hold the labels",
                hint="locales: [en, ru], defaultLocale: en, i18n/<locale>.yaml",
            )
        )
        return problems, used
    for where, message_key in required:
        for locale in locales:
            dictionary = package.dictionaries.get(locale)
            if dictionary is None or message_key not in dictionary.messages:
                problems.append(
                    Problem(
                        "settings_label_missing",
                        "error",
                        where,
                        f"{message_key} is not in the dictionary of {locale}",
                        hint=f"i18n/{locale}.yaml",
                    )
                )
    return problems, used


# --- values -----------------------------------------------------------------------------------


def _type_error(value: Any, kind: str) -> bool:
    if kind == "string":
        return not isinstance(value, str)
    if kind == "boolean":
        return not isinstance(value, bool)
    if kind == "integer":
        if isinstance(value, bool):
            return True
        if isinstance(value, float):
            return not (math.isfinite(value) and value.is_integer())
        return not isinstance(value, int)
    if kind == "number":
        return not _number(value)
    if kind == "array":
        return not isinstance(value, list)
    if kind == "object":
        return not isinstance(value, dict)
    return True


def _format_error(value: str, kind: str) -> bool:
    if kind == "date":
        if not _DATE.match(value):
            return True
        try:
            date.fromisoformat(value)
        except ValueError:
            return True
        return False
    if kind == "uuid":
        return not _UUID.match(value)
    if kind == "email":
        return not _EMAIL.match(value)
    if kind == "uri":
        if any(ch.isspace() for ch in value):
            return True
        try:
            parts = urlsplit(value)
        except ValueError:
            return True
        return not (_URI_SCHEME.match(parts.scheme) and (parts.netloc or parts.path))
    return False


def _error(path: str, code: str, message: str) -> dict[str, Any]:
    return {"path": path, "code": code, "message": message}


def validate(value: Any, schema: Mapping[str, Any], path: str = "") -> list[dict[str, Any]]:
    """Every violation of ``schema`` by ``value``: ``{path, code, message}``, no value in them.

    ``path`` is the JSON Pointer of the field (``""`` — the root), ``code``
    the JSON Schema keyword. A member the schema does not declare is
    ``additionalProperties`` on every object (§1).
    """
    errors: list[dict[str, Any]] = []
    _validate(value, schema, path, errors)
    return errors


def _validate(value: Any, node: Mapping[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    kind = node.get("type")
    if not isinstance(kind, str) or _type_error(value, kind):
        errors.append(_error(path, "type", f"must be of type {kind}"))
        return
    if "enum" in node and not any(_equal(value, item) for item in node["enum"]):
        errors.append(_error(path, "enum", "must be one of the values of enum"))
    if kind in ("integer", "number"):
        if "minimum" in node and value < node["minimum"]:
            errors.append(_error(path, "minimum", f"must be >= {node['minimum']}"))
        if "maximum" in node and value > node["maximum"]:
            errors.append(_error(path, "maximum", f"must be <= {node['maximum']}"))
    elif kind == "string":
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(_error(path, "minLength", f"must be at least {node['minLength']} long"))
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(_error(path, "maxLength", f"must be at most {node['maxLength']} long"))
        if "pattern" in node and re.search(node["pattern"], value) is None:
            errors.append(_error(path, "pattern", "must match the pattern of the field"))
        if "format" in node and _format_error(value, node["format"]):
            errors.append(_error(path, "format", f"must be a {node['format']}"))
    elif kind == "array":
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(_error(path, "minItems", f"must have at least {node['minItems']} items"))
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(_error(path, "maxItems", f"must have at most {node['maxItems']} items"))
        items = node.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(item, items, f"{path}/{index}", errors)
    elif kind == "object":
        properties = node.get("properties") or {}
        for name in node.get("required") or ():
            if name not in value:
                errors.append(_error(pointer_join(path, name), "required", "is required"))
        for name, item in value.items():
            child = properties.get(name)
            if not isinstance(child, Mapping):
                errors.append(
                    _error(
                        pointer_join(path, name),
                        "additionalProperties",
                        "is not a field of the settings",
                    )
                )
                continue
            _validate(item, child, pointer_join(path, name), errors)


def pointer_join(path: str, name: str | int) -> str:
    return path + pointer(name)


def _equal(left: Any, right: Any) -> bool:
    """Equality of JSON values: ``true`` is not ``1``."""
    return canonical_hash(left) == canonical_hash(right)


@dataclass(frozen=True)
class Reference:
    """A string of the values that names a platform object (``x-ref``)."""

    path: str
    kind: str
    value: str


def references(values: Any, schema: Mapping[str, Any]) -> list[Reference]:
    """The ``x-ref`` strings of ``values`` that pass ``schema`` (§4.5)."""
    out: list[Reference] = []
    _references(values, schema, "", out)
    return out


def _references(value: Any, node: Mapping[str, Any], path: str, out: list[Reference]) -> None:
    kind = node.get("type")
    if kind == "string" and isinstance(value, str) and node.get(REF) in REF_KINDS:
        out.append(Reference(path, str(node[REF]), value))
    elif kind == "array" and isinstance(value, list) and isinstance(node.get("items"), Mapping):
        for index, item in enumerate(value):
            _references(item, node["items"], pointer_join(path, index), out)
    elif kind == "object" and isinstance(value, dict):
        properties = node.get("properties") or {}
        for name, item in value.items():
            if isinstance(properties.get(name), Mapping):
                _references(item, properties[name], pointer_join(path, name), out)


def prune(values: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    """The saved values the schema declares: members it has not are dropped, by field."""
    if not isinstance(values, Mapping):
        return {}
    properties = schema.get("properties") or {}
    out: dict[str, Any] = {}
    for name, value in values.items():
        node = properties.get(name)
        if not isinstance(node, Mapping):
            continue
        if node.get("type") == "object" and isinstance(value, Mapping):
            out[name] = prune(value, node)
        else:
            out[name] = copy.deepcopy(value)
    return out


def defaults(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The ``default`` of every field; an object without one — the defaults of its fields."""
    out: dict[str, Any] = {}
    for name, node in (schema.get("properties") or {}).items():
        if not isinstance(node, Mapping):
            continue
        nested = defaults(node) if node.get("type") == "object" else {}
        if "default" in node:
            value = copy.deepcopy(node["default"])
            out[name] = overlay(nested, value) if isinstance(value, dict) else value
        elif nested:
            out[name] = nested
    return out


def overlay(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    """``top`` over ``base``: objects merge by field, anything else is replaced."""
    out = copy.deepcopy(dict(base))
    for name, value in top.items():
        if isinstance(value, Mapping) and isinstance(out.get(name), Mapping):
            out[name] = overlay(out[name], value)
        else:
            out[name] = copy.deepcopy(value)
    return out


def effective(values: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    """§3: the saved values the schema declares over its defaults."""
    return overlay(defaults(schema), prune(values, schema))


def changed_paths(before: Any, after: Any, path: str = "") -> list[str]:
    """JSON Pointers of the members whose saved value differs: added, changed or removed."""
    out: list[str] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for name in sorted(set(before) | set(after)):
            at = pointer_join(path, str(name))
            if name not in before or name not in after:
                out.append(at)
            else:
                out += changed_paths(before[name], after[name], at)
    elif not _equal(before, after):
        out.append(path or "/")
    return out


# --- the plan ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    incompatible: list[dict[str, Any]]


def compare(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None, saved: Any
) -> Comparison:
    """§7: the fields ``after`` adds and removes against ``before``; saved values it refuses."""
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    _diff(
        before or {}, after or {}, "", saved if isinstance(saved, Mapping) else {}, added, removed
    )
    incompatible: list[dict[str, Any]] = []
    if after is not None:
        incompatible = [
            {"path": e["path"], "code": e["code"]}
            for e in validate(prune(saved, after), after)
            if e["code"] != "required"
        ]
    return Comparison(added, removed, incompatible)


def _diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    path: str,
    saved: Mapping[str, Any],
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
) -> None:
    old = before.get("properties") or {}
    new = after.get("properties") or {}
    for name in sorted(set(old) | set(new)):
        at = pointer_join(path, name)
        if name not in old:
            item: dict[str, Any] = {"path": at}
            if isinstance(new[name], Mapping) and "default" in new[name]:
                item["default"] = new[name]["default"]
            added.append(item)
        elif name not in new:
            removed.append({"path": at, "saved": name in saved})
        elif (
            isinstance(old[name], Mapping)
            and isinstance(new[name], Mapping)
            and old[name].get("type") == new[name].get("type") == "object"
        ):
            inner = saved.get(name)
            _diff(
                old[name],
                new[name],
                at,
                inner if isinstance(inner, Mapping) else {},
                added,
                removed,
            )


def schema_pointer(path: str) -> str:
    """Where the field at ``path`` of the values is declared: ``/spec/settings/schema/...``."""
    names = _parts(path) if path not in ("", "/") else []
    parts: list[str] = []
    for name in names:
        parts += ["items"] if name.isdigit() and parts else ["properties", name]
    return SCHEMA_BASE + pointer(*parts)


# --- the reader -------------------------------------------------------------------------------


Lookup = Callable[[str], str | None]


def lookup(messages: Mapping[str, Mapping[str, str]], chain: Sequence[str]) -> Lookup:
    """The text of a key in the first language of ``chain`` that has it."""

    def text(key: str) -> str | None:
        for locale in chain:
            found = (messages.get(locale) or {}).get(key)
            if isinstance(found, str):
                return found
        return None

    return text


def present(
    package: str,
    schema: Mapping[str, Any],
    uischema: Mapping[str, Any] | None,
    text: Lookup,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The schema and the layout with the strings in place of their keys (§4).

    ``title`` of a property — ``<package>.settings.<path>`` (the key itself when
    no dictionary has it), ``description`` — ``<package>.settings.<path>.help``
    when one has it; ``label`` and ``text`` of the layout — the string of their key.
    """
    shown = copy.deepcopy(dict(schema))
    for path, node in fields(shown):
        assert isinstance(node, dict)
        node["title"] = text(field_key(package, path)) or field_key(package, path)
        found = text(help_key(package, path))
        if found is not None:
            node["description"] = found
    layout = copy.deepcopy(dict(uischema)) if uischema is not None else None
    if layout is not None:
        _present_ui(layout, text)
    return shown, layout


def _present_ui(node: Any, text: Lookup) -> None:
    if not isinstance(node, dict):
        return
    for name in ("label", "text"):
        if isinstance(node.get(name), str):
            node[name] = text(node[name]) or node[name]
    for child in node.get("elements") or ():
        _present_ui(child, text)


def parse_uuid(value: str) -> uuid.UUID | None:
    """The id an ``x-ref`` names, or ``None`` when it is no UUID."""
    if not _UUID.match(value):
        return None
    return uuid.UUID(value)
