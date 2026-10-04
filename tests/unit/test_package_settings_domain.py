"""The settings of a package without the database (CP-ADR-0081 §1-§4, §7).

The declaration: the subset of JSON Schema (``title``/``description`` refused,
secret markers, defaults of optional fields, the limits per object and of
nesting), the subset of JSON Forms element by element and by each ban, the
labels in every declared language. The values: every violation with a path
and a keyword and no value, the secret material by path, the references, the
effective values. The plan: two revisions compared against saved values.
"""

import copy
import math
from typing import Any

import pytest
import yaml

from control_plane.application.queries.package_settings import Texts
from control_plane.domain import package_settings as ps
from control_plane.domain.package_source import parse_package
from control_plane.domain.project import secret_findings

KEY = "sample"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["owner"],
    "properties": {
        "limit": {"type": "number", "minimum": 0, "default": 100},
        "days": {"type": "integer", "minimum": 1, "maximum": 20, "default": 2},
        "owner": {"type": "string", "x-ref": "role"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "default": []},
        "window": {
            "type": "object",
            "properties": {"start": {"type": "integer", "default": 9}},
        },
    },
}
LAYOUT: dict[str, Any] = {
    "type": "VerticalLayout",
    "elements": [
        {
            "type": "Group",
            "label": f"{KEY}.settings.groups.main",
            "elements": [
                {"type": "Control", "scope": "#/properties/limit"},
                {"type": "Control", "scope": "#/properties/owner", "label": f"{KEY}.owner"},
            ],
        },
        {
            "type": "HorizontalLayout",
            "elements": [
                {"type": "Control", "scope": "#/properties/days"},
                {"type": "Control", "scope": "#/properties/tags"},
            ],
        },
        {"type": "Label", "text": f"{KEY}.note"},
        {
            "type": "Control",
            "scope": "#/properties/window/properties/start",
            "rule": {
                "effect": "SHOW",
                "condition": {"scope": "#/properties/days", "schema": {"minimum": 3}},
            },
        },
    ],
}


def _messages(schema: dict[str, Any], *extra: str) -> dict[str, str]:
    keys = [f"{KEY}.title", f"{KEY}.settings.groups.main", f"{KEY}.owner", f"{KEY}.note"]
    keys += [ps.field_key(KEY, path) for path, _ in ps.fields(schema)]
    return {k: k.rsplit(".", 1)[-1] for k in [*keys, *extra]}


def _declare(
    settings: Any,
    *,
    messages: dict[str, dict[str, str]] | None = None,
    locales: list[str] | None = None,
) -> ps.Declaration:
    langs = ["en", "ru"] if locales is None else locales
    spec: dict[str, Any] = {"version": "1.0.0", "settings": settings}
    if langs:
        spec |= {"locales": langs, "defaultLocale": langs[0]}
    manifest = {"apiVersion": "x/v1", "kind": "Package", "key": KEY, "spec": spec}
    schema = settings.get("schema") if isinstance(settings, dict) else None
    texts = messages or {
        lang: _messages(schema if isinstance(schema, dict) else {}) for lang in langs
    }
    files = [("package.yaml", yaml.safe_dump(manifest))]
    files += [(f"i18n/{lang}.yaml", yaml.safe_dump(m)) for lang, m in texts.items()]
    return ps.check_declaration(parse_package(files))


def _codes(found: ps.Declaration, severity: str = "error") -> list[tuple[str, str]]:
    return sorted((p.code, p.path) for p in found.problems if p.severity == severity)


def _with(path: str, value: Any, *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """``SCHEMA`` with the field at the dotted ``path`` replaced (``None`` removes it)."""
    out = copy.deepcopy(schema or SCHEMA)
    node = out
    names = path.split(".")
    for name in names[:-1]:
        node = node["properties"][name]
    if value is None:
        del node["properties"][names[-1]]
    else:
        node["properties"][names[-1]] = value
    return out


S = ps.SCHEMA_BASE + "/properties"


# --- the declaration --------------------------------------------------------------------------


def test_the_example_declares_settings_without_findings() -> None:
    found = _declare({"schema": SCHEMA, "uischema": LAYOUT})
    assert found.problems == []
    assert found.declared is not None
    assert found.declared.hash == ps.schema_hash(SCHEMA, LAYOUT)
    assert f"{KEY}.settings.limit.help" in found.messages


def test_a_package_without_settings_declares_nothing() -> None:
    files = [
        (
            "package.yaml",
            yaml.safe_dump(
                {"apiVersion": "x/v1", "kind": "Package", "key": KEY, "spec": {"version": "1"}}
            ),
        )
    ]
    found = ps.check_declaration(parse_package(files))
    assert (found.present, found.declared, found.problems) == (False, None, [])


@pytest.mark.parametrize(
    ("settings", "path"),
    [
        (None, ps.BASE),
        ([], ps.BASE),
        ({}, ps.BASE),
        ({"schema": SCHEMA, "messages": {}}, ps.BASE + "/messages"),
        ({"schema": {"type": "array", "items": {"type": "string"}}}, ps.SCHEMA_BASE + "/type"),
        ({"schema": "object"}, ps.SCHEMA_BASE),
        ({"schema": {"type": "object", "properties": {}}}, ps.SCHEMA_BASE + "/properties"),
    ],
)
def test_a_settings_block_of_another_shape_is_unsupported(settings: Any, path: str) -> None:
    found = _declare(settings)
    assert ("settings_schema_unsupported", path) in _codes(found)
    assert found.declared is None


@pytest.mark.parametrize("keyword", ["title", "description"])
def test_labels_in_the_schema_are_refused(keyword: str) -> None:
    schema = _with("limit", {"type": "number", "default": 1, keyword: "Limit"})
    found = _declare({"schema": schema})
    assert _codes(found) == [("settings_schema_unsupported", f"{S}/limit/{keyword}")]
    assert found.problems[0].hint and "dictionaries" in found.problems[0].hint


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ({"$ref": "#/x"}, "$ref"),
        ({"allOf": []}, "allOf"),
        ({"anyOf": []}, "anyOf"),
        ({"oneOf": []}, "oneOf"),
        ({"not": {}}, "not"),
        ({"if": {}}, "if"),
        ({"const": 1}, "const"),
        ({"multipleOf": 2}, "multipleOf"),
        ({"writeOnly": False}, "writeOnly"),
    ],
)
def test_a_keyword_outside_the_subset_is_unsupported(extra: dict[str, Any], where: str) -> None:
    schema = _with("limit", {"type": "number", "default": 1, **extra})
    found = _declare({"schema": schema})
    assert ("settings_schema_unsupported", f"{S}/limit/{where.replace('/', '~1')}") in _codes(found)


@pytest.mark.parametrize(
    ("field", "where"),
    [
        (
            {
                "type": "object",
                "properties": {"a": {"type": "integer", "default": 1}},
                "additionalProperties": True,
            },
            "additionalProperties",
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "integer", "default": 1}},
                "patternProperties": {},
            },
            "patternProperties",
        ),
        ({"type": "integer", "default": 1, "minLength": 1}, "minLength"),
        ({"type": "string", "default": "", "minimum": 1}, "minimum"),
        ({"type": "string", "default": "", "items": {"type": "string"}}, "items"),
        ({"type": "integer", "default": 1, "x-ref": "role"}, "x-ref"),
        ({"type": "array", "items": {"type": "integer"}, "default": [], "x-ref": "role"}, "x-ref"),
        ({"type": "string", "default": "", "x-ref": "agent"}, "x-ref"),
        ({"type": "string", "default": "", "format": "hostname"}, "format"),
        ({"type": "string", "default": "", "pattern": "("}, "pattern"),
        ({"type": "integer", "default": 1, "enum": []}, "enum"),
        ({"type": "integer", "default": 1, "enum": [1, 1]}, "enum"),
        ({"type": "integer", "default": 1, "enum": ["a"]}, "enum"),
        ({"type": "array", "default": []}, "items"),
        ({"type": "date", "default": ""}, "type"),
        ({"type": "integer", "default": 1, "minimum": "0"}, "minimum"),
        ({"type": "array", "items": {"type": "string"}, "default": [], "minItems": -1}, "minItems"),
    ],
)
def test_a_keyword_where_it_does_not_apply_is_unsupported(
    field: dict[str, Any], where: str
) -> None:
    found = _declare({"schema": _with("limit", field)})
    assert ("settings_schema_unsupported", f"{S}/limit/{where}") in _codes(found)


def test_x_ref_on_the_items_of_an_array_of_strings_is_supported() -> None:
    field = {"type": "array", "items": {"type": "string", "x-ref": "workspace"}, "default": []}
    assert _declare({"schema": _with("limit", field)}).problems == []


def test_objects_nest_three_levels_deep_counting_the_root() -> None:
    leaf = {"type": "integer", "default": 1}
    two = {"type": "object", "properties": {"leaf": leaf}}
    three = {"type": "object", "properties": {"inner": two}}
    assert _codes(_declare({"schema": _with("limit", three)})) == []
    four = {"type": "object", "properties": {"deeper": three}}
    found = _declare({"schema": _with("limit", four)})
    assert _codes(found) == [
        ("settings_schema_unsupported", f"{S}/limit/properties/deeper/properties/inner")
    ]
    # At the deepest level an array holds scalars only.
    arrays = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    deep = {
        "type": "object",
        "properties": {
            "inner": {"type": "object", "properties": {"list": {**arrays, "default": []}}}
        },
    }
    found = _declare({"schema": _with("limit", deep)})
    assert (
        "settings_schema_unsupported",
        f"{S}/limit/properties/inner/properties/list/items",
    ) in _codes(found)


def test_at_most_one_hundred_properties_on_each_object() -> None:
    many = {f"f{i}": {"type": "integer", "default": i} for i in range(100)}
    nested = {"type": "object", "properties": copy.deepcopy(many)}
    schema = {"type": "object", "properties": {**many, "nested": nested}}
    del schema["properties"]["f99"]  # 100 at the root with nested
    messages = {lang: _messages(schema) for lang in ("en", "ru")}
    assert _codes(_declare({"schema": schema}, messages=messages)) == []
    nested["properties"]["f100"] = {"type": "integer", "default": 0}
    found = _declare({"schema": schema}, messages=messages)
    assert _codes(found) == [("settings_schema_unsupported", f"{S}/nested/properties")]


@pytest.mark.parametrize(
    ("name", "field", "where"),
    [
        ("value", {"type": "string", "default": "", "writeOnly": True}, "/writeOnly"),
        ("value", {"type": "string", "default": "", "format": "password"}, "/format"),
        ("apiKey", {"type": "string", "default": ""}, ""),
        ("clientSecret", {"type": "string", "default": ""}, ""),
        ("accessToken", {"type": "string", "default": ""}, ""),
        ("value", {"type": "string", "default": "ghp_" + "x" * 30}, "/default"),
        ("value", {"type": "string", "default": "a", "enum": ["a", "sk-" + "y" * 20]}, "/enum/1"),
    ],
)
def test_a_secret_marker_is_settings_secret_field(
    name: str, field: dict[str, Any], where: str
) -> None:
    schema = _with(name, field)
    messages = {lang: _messages(schema) for lang in ("en", "ru")}
    found = _declare({"schema": schema}, messages=messages)
    assert _codes(found) == [("settings_secret_field", f"{S}/{name}{where}")]
    assert "x" * 30 not in found.problems[0].message


def test_a_secret_ref_by_name_is_not_a_secret() -> None:
    schema = _with("secretRef", {"type": "string", "default": ""})
    messages = {lang: _messages(schema) for lang in ("en", "ru")}
    assert _declare({"schema": schema}, messages=messages).problems == []


def test_an_optional_field_needs_a_default_that_passes_it() -> None:
    schema = _with("days", {"type": "integer", "minimum": 1})
    assert _codes(_declare({"schema": schema})) == [("settings_default_missing", f"{S}/days")]
    schema = _with("days", {"type": "integer", "maximum": 5, "default": 7})
    assert _codes(_declare({"schema": schema})) == [
        ("settings_default_invalid", f"{S}/days/default")
    ]
    schema = _with("days", {"type": "integer", "default": "2"})
    assert _codes(_declare({"schema": schema})) == [
        ("settings_default_invalid", f"{S}/days/default")
    ]
    # A required field may go without, an object takes the defaults of its fields.
    required = copy.deepcopy(SCHEMA)
    required["required"].append("days")
    del required["properties"]["days"]["default"]
    assert _declare({"schema": required}).problems == []


def test_required_names_a_property() -> None:
    schema = copy.deepcopy(SCHEMA)
    schema["required"] = ["owner", "nobody"]
    assert _codes(_declare({"schema": schema})) == [
        ("settings_schema_unsupported", ps.SCHEMA_BASE + "/required/1")
    ]


@pytest.mark.parametrize("name", ["Limit", "1st", "with-dash", "a" * 64])
def test_a_field_name_is_camel_case(name: str) -> None:
    found = _declare({"schema": _with(name, {"type": "integer", "default": 1})})
    assert ("settings_schema_unsupported", f"{S}/{name}") in _codes(found)


# --- the layout -------------------------------------------------------------------------------


def _layout(change: Any) -> list[tuple[str, str]]:
    layout = copy.deepcopy(LAYOUT)
    change(layout)
    return _codes(_declare({"schema": SCHEMA, "uischema": layout}))


U = ps.UISCHEMA_BASE


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda u: u.update(type="Control", scope="#/properties/limit"), U + "/type"),
        (
            lambda u: u["elements"].append({"type": "Categorization", "elements": []}),
            U + "/elements/4/type",
        ),
        (
            lambda u: u["elements"].append(
                {"type": "ListWithDetail", "scope": "#/properties/tags"}
            ),
            U + "/elements/4/type",
        ),
        (
            lambda u: u["elements"][1]["elements"][0].update(options={"multi": True}),
            U + "/elements/1/elements/0/options",
        ),
        (lambda u: u.update(i18n="x"), U + "/i18n"),
        (lambda u: u["elements"][0].pop("label"), U + "/elements/0/label"),
        (lambda u: u["elements"][0].update(label="sample.groups.main"), U + "/elements/0/label"),
        (lambda u: u["elements"][0].update(label="Main group"), U + "/elements/0/label"),
        (lambda u: u["elements"][2].update(text="Plain text"), U + "/elements/2/text"),
        (lambda u: u["elements"][1].update(elements=[]), U + "/elements/1/elements"),
        (
            lambda u: u["elements"][1]["elements"][0].update(scope="#/properties/nothing"),
            U + "/elements/1/elements/0/scope",
        ),
        (
            lambda u: u["elements"][1]["elements"][0].update(scope="#"),
            U + "/elements/1/elements/0/scope",
        ),
        (
            lambda u: u["elements"][1]["elements"][1].update(scope="#/properties/tags/items"),
            U + "/elements/1/elements/1/scope",
        ),
        (
            lambda u: u["elements"].append({"type": "Control", "scope": "#/properties/limit"}),
            U + "/elements/4/scope",
        ),
        (lambda u: u["elements"][3]["rule"].update(effect="COLOR"), U + "/elements/3/rule/effect"),
        (
            lambda u: u["elements"][3]["rule"]["condition"].update(type="OR", conditions=[]),
            U + "/elements/3/rule/condition",
        ),
        (
            lambda u: u["elements"][3]["rule"]["condition"].update(schema={"x-ref": "role"}),
            U + "/elements/3/rule/condition/schema",
        ),
        (
            lambda u: u["elements"][3]["rule"]["condition"].update(schema={"default": 1}),
            U + "/elements/3/rule/condition/schema",
        ),
        (
            lambda u: u["elements"][3]["rule"]["condition"].update(scope="#/properties/x"),
            U + "/elements/3/rule/condition/scope",
        ),
        (
            lambda u: u["elements"][3]["rule"]["condition"].update(failWhenUndefined="no"),
            U + "/elements/3/rule/condition/failWhenUndefined",
        ),
        (lambda u: u["elements"][3].update(rule={"effect": "SHOW"}), U + "/elements/3/rule"),
    ],
)
def test_the_layout_outside_its_subset_is_unsupported(change: Any, where: str) -> None:
    assert ("settings_uischema_unsupported", where) in _layout(change)


def test_each_element_of_the_subset_and_a_rule_on_any_is_supported() -> None:
    def rules(u: dict[str, Any]) -> None:
        rule = {
            "effect": "DISABLE",
            "condition": {
                "scope": "#/properties/limit",
                "schema": {"const": 5},
                "failWhenUndefined": True,
            },
        }
        for element in (u, u["elements"][0], u["elements"][1], u["elements"][2]):
            element["rule"] = copy.deepcopy(rule)

    assert _layout(rules) == []
    group_root = {
        "type": "Group",
        "label": f"{KEY}.settings.groups.main",
        "elements": copy.deepcopy(LAYOUT["elements"]),
    }
    assert _codes(_declare({"schema": SCHEMA, "uischema": group_root})) == []


def test_the_layout_nests_five_deep_and_holds_two_hundred_elements() -> None:
    def nest(depth: int) -> dict[str, Any]:
        node: dict[str, Any] = {"type": "Control", "scope": "#/properties/limit"}
        for _ in range(depth - 1):
            node = {"type": "VerticalLayout", "elements": [node]}
        return node

    assert ("settings_uischema_unsupported", U) not in _codes(
        _declare({"schema": SCHEMA, "uischema": nest(5)})
    )
    deep = _codes(_declare({"schema": SCHEMA, "uischema": nest(6)}))
    assert any(code == "settings_uischema_unsupported" for code, _ in deep)
    labels = [{"type": "Label", "text": f"{KEY}.note"} for _ in range(200)]
    wide = _codes(
        _declare({"schema": SCHEMA, "uischema": {"type": "VerticalLayout", "elements": labels}})
    )
    assert ("settings_uischema_unsupported", U) in wide


def test_a_property_without_a_control_is_a_warning() -> None:
    layout = copy.deepcopy(LAYOUT)
    layout["elements"].pop(3)
    found = _declare({"schema": SCHEMA, "uischema": layout})
    assert _codes(found) == []
    assert _codes(found, "warning") == [("settings_uischema_uncovered", U)]
    assert found.declared is not None
    assert "window.start" in found.problems[0].message


# --- labels -----------------------------------------------------------------------------------


def test_a_label_missing_in_a_language_names_the_language_and_the_key() -> None:
    messages = {lang: _messages(SCHEMA) for lang in ("en", "ru")}
    del messages["ru"][f"{KEY}.settings.window.start"]
    del messages["en"][f"{KEY}.title"]
    del messages["en"][f"{KEY}.settings.groups.main"]
    found = _declare({"schema": SCHEMA, "uischema": LAYOUT}, messages=messages)
    assert _codes(found) == [
        ("settings_label_missing", ps.BASE),
        ("settings_label_missing", f"{S}/window/properties/start"),
        ("settings_label_missing", U + "/elements/0/label"),
    ]
    by_path = {p.path: p.message for p in found.problems}
    assert by_path[f"{S}/window/properties/start"] == (
        f"{KEY}.settings.window.start is not in the dictionary of ru"
    )
    assert found.declared is None


def test_settings_need_declared_languages() -> None:
    found = _declare({"schema": SCHEMA}, locales=[], messages={})
    assert _codes(found) == [("settings_label_missing", ps.BASE)]


# --- values -----------------------------------------------------------------------------------


def _errors(values: Any, schema: dict[str, Any] = SCHEMA) -> list[tuple[str, str]]:
    found = ps.validate(values, schema)
    return sorted((e["path"], e["code"]) for e in found)


@pytest.mark.parametrize(
    ("values", "errors"),
    [
        ({"owner": "x"}, []),
        ({}, [("/owner", "required")]),
        (None, [("", "type")]),
        ([], [("", "type")]),
        ({"owner": None}, [("/owner", "type")]),
        ({"owner": "x", "limit": None}, [("/limit", "type")]),
        ({"owner": "x", "limit": True}, [("/limit", "type")]),
        ({"owner": "x", "limit": "5"}, [("/limit", "type")]),
        ({"owner": "x", "limit": math.nan}, [("/limit", "type")]),
        ({"owner": "x", "limit": math.inf}, [("/limit", "type")]),
        ({"owner": "x", "days": 2.0}, []),
        ({"owner": "x", "days": 2.5}, [("/days", "type")]),
        ({"owner": "x", "days": True}, [("/days", "type")]),
        ({"owner": "x", "days": 0}, [("/days", "minimum")]),
        ({"owner": "x", "days": 21}, [("/days", "maximum")]),
        ({"owner": 5}, [("/owner", "type")]),
        ({"owner": "x", "tags": "a"}, [("/tags", "type")]),
        ({"owner": "x", "tags": ["a", 1]}, [("/tags/1", "type")]),
        ({"owner": "x", "tags": ["a", "b", "c", "d"]}, [("/tags", "maxItems")]),
        (
            {"owner": "x", "window": {"start": 1, "end": 2}},
            [("/window/end", "additionalProperties")],
        ),
        ({"owner": "x", "window": None}, [("/window", "type")]),
        ({"owner": "x", "a/b": 1}, [("/a~1b", "additionalProperties")]),
    ],
)
def test_every_violation_is_a_path_and_a_keyword(
    values: Any, errors: list[tuple[str, str]]
) -> None:
    assert _errors(values) == errors


def test_messages_never_carry_the_value() -> None:
    schema = _with("note", {"type": "string", "maxLength": 3, "pattern": "^a", "default": ""})
    found = ps.validate({"owner": "x", "note": "zzzzzz-unique", "limit": -987654}, schema)
    assert {e["code"] for e in found} == {"maxLength", "pattern", "minimum"}
    assert not any("zzzzzz" in e["message"] or "987654" in e["message"] for e in found)


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("date", "2026-10-03", ["2026-13-01", "03.10.2026", "2026-10-3"]),
        ("uuid", "5b2e0c1a-7d4f-4e2b-9a61-3c8f0d5e7b21", ["5b2e", "not-a-uuid"]),
        ("email", "a@example.org", ["a@", "a example.org", "@example.org"]),
        ("uri", "https://example.org/x", ["no scheme", "://x", "http://exa mple"]),
    ],
)
def test_formats_are_checked(kind: str, good: str, bad: list[str]) -> None:
    schema = _with("note", {"type": "string", "format": kind, "default": good})
    assert _errors({"owner": "x", "note": good}, schema) == []
    for value in bad:
        assert _errors({"owner": "x", "note": value}, schema) == [("/note", "format")], value


def test_enum_tells_a_boolean_from_a_number() -> None:
    schema = _with("days", {"type": "integer", "enum": [1, 2], "default": 1})
    assert _errors({"owner": "x", "days": 1}, schema) == []
    assert _errors({"owner": "x", "days": 3}, schema) == [("/days", "enum")]


def test_secret_material_is_found_by_path_and_never_quoted() -> None:
    token = "ghp_" + "Z" * 30
    found = secret_findings(
        {
            "note": f"see {token}",
            token: {"inner": token},
            "list": ["ok", f"Bearer {'q' * 30}"],
            "nested": {"password": "plain", "fine": "word"},
            "secretRef": "vault:abc",
            "number": 5,
        }
    )
    assert found == [
        {"path": "/note", "match": "provider_token"},
        {"path": "/", "match": "provider_token"},
        {"path": "/list/1", "match": "bearer_token"},
        {"path": "/nested", "match": "secret_name"},
    ]
    assert token not in repr(found)
    assert secret_findings({}) == []
    assert secret_findings(None) == []


def test_references_are_the_x_ref_strings_that_pass_the_schema() -> None:
    schema = _with(
        "roles", {"type": "array", "items": {"type": "string", "x-ref": "role"}, "default": []}
    )
    refs = ps.references({"owner": "r1", "roles": ["r2", 3], "limit": 5, "zzz": "r4"}, schema)
    assert [(r.path, r.kind, r.value) for r in refs] == [
        ("/owner", "role", "r1"),
        ("/roles/0", "role", "r2"),
    ]


def test_effective_values_are_the_saved_ones_over_the_defaults() -> None:
    saved = {"limit": 5, "tags": ["a"], "window": {"start": 1}, "gone": 1, "owner": "r"}
    assert ps.effective(saved, SCHEMA) == {
        "limit": 5,
        "days": 2,
        "tags": ["a"],
        "window": {"start": 1},
        "owner": "r",
    }
    assert ps.effective({}, SCHEMA) == {"limit": 100, "days": 2, "tags": [], "window": {"start": 9}}
    assert ps.effective(None, SCHEMA) == ps.effective({}, SCHEMA)
    # An object merges by field; an array is replaced whole.
    schema = _with(
        "window",
        {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "default": 9},
                "end": {"type": "integer", "default": 18},
            },
        },
    )
    assert ps.effective({"window": {"end": 20}, "tags": []}, schema)["window"] == {
        "start": 9,
        "end": 20,
    }
    assert ps.prune({"window": {"end": 1, "x": 2}, "y": 1}, schema) == {"window": {"end": 1}}


def test_changed_paths_name_added_changed_and_removed_members() -> None:
    before = {"a": 1, "b": {"c": 1, "d": 2}, "e": [1], "f": 1}
    after = {"a": 1, "b": {"c": 2, "d": 2}, "e": [1, 2], "g": True}
    assert ps.changed_paths(before, after) == ["/b/c", "/e", "/f", "/g"]
    assert ps.changed_paths({}, {}) == []
    assert ps.changed_paths({"a": 1}, {"a": True}) == ["/a"]


# --- the plan ---------------------------------------------------------------------------------


def test_compare_names_added_removed_and_incompatible_fields() -> None:
    after = _with("days", None)
    after = _with("limit", {"type": "number", "maximum": 10, "default": 1}, schema=after)
    after = _with("extra", {"type": "string", "default": "x"}, schema=after)
    after = _with("must", {"type": "string"}, schema=after)
    after["required"] = ["owner", "must"]
    after["properties"]["window"]["properties"]["end"] = {"type": "integer", "default": 1}
    after["properties"]["tags"] = {"type": "string", "default": ""}
    saved = {"limit": 50, "days": 3, "owner": "r", "tags": ["a"]}
    found = ps.compare(SCHEMA, after, saved)
    assert found.added == [
        {"path": "/extra", "default": "x"},
        {"path": "/must"},
        {"path": "/window/end", "default": 1},
    ]
    assert found.removed == [{"path": "/days", "saved": True}]
    # A required field nobody saved is no incompatibility of a saved value.
    assert found.incompatible == [
        {"path": "/limit", "code": "maximum"},
        {"path": "/tags", "code": "type"},
    ]
    gone = ps.compare(SCHEMA, None, {"owner": "r"})
    assert [r["path"] for r in gone.removed] == ["/days", "/limit", "/owner", "/tags", "/window"]
    assert gone.removed[2] == {"path": "/owner", "saved": True}
    assert (gone.added, gone.incompatible) == ([], [])
    first = ps.compare(None, SCHEMA, {})
    assert {"path": "/owner"} in first.added and first.removed == []


def test_schema_pointer_names_the_field_in_the_manifest() -> None:
    assert ps.schema_pointer("/limit") == f"{S}/limit"
    assert ps.schema_pointer("/window/start") == f"{S}/window/properties/start"
    assert ps.schema_pointer("/tags/0") == f"{S}/tags/items"


# --- the reader -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (None, "en"),
        ("ru", "ru"),
        ("RU-ru", "ru"),
        ("pt-BR", "pt-BR"),
        ("pt-PT", "pt"),
        ("de", "en"),
    ],
)
def test_a_string_is_looked_for_in_the_locale_a_view_would_choose_then_the_default(
    wanted: str | None, found: str
) -> None:
    locales = ("en", "ru", "pt", "pt-BR")
    messages = {locale: {"k": locale} for locale in locales}
    messages["en"]["only"] = "en"
    text = Texts(messages, locales, "en").lookup(wanted)
    assert text("k") == found
    # The order of views (CP-ADR-0080 §5): no base language between the chosen and the default.
    assert text("only") == "en"
    assert text("missing") is None


def test_no_dictionaries_look_up_nothing() -> None:
    assert Texts({}, (), None).lookup("ru")("k") is None


def test_present_puts_the_strings_in_place_of_their_keys() -> None:
    messages = {
        "ru": {f"{KEY}.settings.limit": "Предел", f"{KEY}.settings.groups.main": "Главное"},
        "en": {
            f"{KEY}.settings.limit": "Limit",
            f"{KEY}.settings.limit.help": "Help",
            f"{KEY}.owner": "Owner",
        },
    }
    schema, layout = ps.present(KEY, SCHEMA, LAYOUT, ps.lookup(messages, ["ru", "en"]))
    limit = schema["properties"]["limit"]
    assert (limit["title"], limit["description"]) == ("Предел", "Help")
    assert schema["properties"]["days"]["title"] == f"{KEY}.settings.days"
    assert "description" not in schema["properties"]["days"]
    assert schema["properties"]["window"]["properties"]["start"]["title"] == (
        f"{KEY}.settings.window.start"
    )
    assert layout is not None
    assert layout["elements"][0]["label"] == "Главное"
    assert layout["elements"][0]["elements"][1]["label"] == "Owner"
    assert layout["elements"][2]["text"] == f"{KEY}.note"
    assert "title" not in SCHEMA["properties"]["limit"]  # the revision itself is not touched
    assert ps.present(KEY, SCHEMA, None, ps.lookup({}, []))[1] is None


def test_the_properties_of_objects_inside_items_have_no_labels_of_their_own() -> None:
    """CP-ADR-0081 amendment Б: neither required in the dictionaries nor put in the answer."""
    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "default": [],
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "default": ""}},
                },
            }
        },
    }
    assert [path for path, _ in ps.fields(schema)] == ["steps"]
    messages = {"en": {f"{KEY}.settings.steps": "Steps", f"{KEY}.settings.steps.name": "Name"}}
    shown, _ = ps.present(KEY, schema, None, ps.lookup(messages, ["en"]))
    assert shown["properties"]["steps"]["title"] == "Steps"
    assert "title" not in shown["properties"]["steps"]["items"]["properties"]["name"]
