"""The settings routes in the form agreed with the console on 2026-10-03 (CP-ADR-0081 §4).

The consumer side is ``docs/settings.md`` of the console repository; its
examples are those of CP-ADR-0081 §4, transcribed here independently of the
core's models: :data:`EXAMPLES` is what the console draws, :data:`CONTRACT`
the shape it relies on. Checked here:

- every example fits the contract, and the openapi models of the routes take
  the examples as they are (``schema`` by that name, not a Python alias);
- the routes are where the console's BFF opens them, with their methods;
- the error bodies of a saving carry ``details.errors`` in the agreed form.

``tests/integration/test_package_settings_neutrality.py`` checks real answers
of the core against :data:`CONTRACT` and the example of ``GET …/settings``.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    PackageSettingsListOut,
    PackageSettingsOut,
    PackageSettingsPutRequest,
    PackageSettingsVersionPageOut,
)

_STR: dict[str, Any] = {"type": "string"}
_STR_N: dict[str, Any] = {"type": ["string", "null"]}
_INT: dict[str, Any] = {"type": "integer", "minimum": 0}
_OBJ: dict[str, Any] = {"type": "object"}


def _object(required: list[str], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


_SUMMARY = _object(
    ["package", "title", "packageVersion", "version", "updatedBy", "updatedAt"],
    package=_STR,
    title=_STR,
    packageVersion=_STR_N,
    version=_INT,
    updatedBy=_STR_N,
    updatedAt=_STR_N,
)
_SETTINGS = _object(
    [
        "package",
        "title",
        "packageVersion",
        "schema",
        "uischema",
        "values",
        "effective",
        "version",
        "schemaHash",
        "updatedBy",
        "updatedAt",
        "canManage",
    ],
    package=_STR,
    title=_STR,
    packageVersion=_STR_N,
    schema=_OBJ,
    uischema={"type": ["object", "null"]},
    values=_OBJ,
    effective=_OBJ,
    version=_INT,
    schemaHash={"type": "string", "pattern": "^sha256:"},
    updatedBy=_STR_N,
    updatedAt=_STR_N,
    canManage={"type": "boolean"},
)
_VERSION = _object(
    ["version", "values", "changedPaths", "updatedBy", "updatedAt"],
    version={"type": "integer", "minimum": 1},
    values=_OBJ,
    changedPaths={"type": "array", "items": {"type": "string", "pattern": "^/"}},
    updatedBy=_STR,
    updatedAt=_STR,
)


def _errors(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message", "details", "requestId"],
                "properties": {
                    "details": {
                        "type": "object",
                        "required": ["errors"],
                        "properties": {"errors": {"type": "array", "minItems": 1, "items": item}},
                    }
                },
            }
        },
    }


# What the console draws: the routes and the bodies it reads.
CONTRACT: dict[str, dict[str, Any]] = {
    "list": _object(["items"], items={"type": "array", "items": _SUMMARY}),
    "settings": _SETTINGS,
    "versions": _object(
        ["items", "nextCursor"], items={"type": "array", "items": _VERSION}, nextCursor=_STR_N
    ),
    # Paths only: no value of a refused string; field and match only beside path.
    "secret_material_rejected": _errors(
        {
            "type": "object",
            "required": ["path"],
            "properties": {"path": _STR, "field": _STR, "match": _STR},
            "additionalProperties": False,
        }
    ),
    "settings_invalid": _errors(_object(["path", "code"], path=_STR, code=_STR, message=_STR)),
    "unknown_ref": _errors(
        _object(
            ["path", "ref"],
            path=_STR,
            ref={"enum": ["role", "principal", "workspace", "taskType", "calendar"]},
        )
    ),
    "version_conflict": {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "properties": {
                    "details": {
                        "type": "object",
                        "required": ["currentVersion"],
                        "properties": {"currentVersion": _INT},
                    }
                },
            }
        },
    },
}

ROLE = "5b2e0c1a-7d4f-4e2b-9a61-3c8f0d5e7b21"
ACTOR = "8f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
# The examples of CP-ADR-0081 §4, as the console's docs/settings.md writes them.
EXAMPLES: dict[str, Any] = {
    "list": {
        "items": [
            {
                "package": "invoice-payment",
                "title": "Оплата счетов",
                "packageVersion": "1.3.0",
                "version": 3,
                "updatedBy": ACTOR,
                "updatedAt": "2026-10-03T09:00:00Z",
            }
        ]
    },
    "settings": {
        "package": "invoice-payment",
        "title": "Оплата счетов",
        "packageVersion": "1.3.0",
        "schema": {
            "type": "object",
            "required": ["approverRole"],
            "properties": {
                "approvalThreshold": {
                    "type": "number",
                    "minimum": 0,
                    "default": 100000,
                    "title": "Порог согласования",
                    "description": "Сумма, выше которой нужен второй подписант",
                },
                "reviewDueWorkdays": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 2,
                    "title": "Срок проверки, рабочих дней",
                },
                "approverRole": {"type": "string", "x-ref": "role", "title": "Роль согласующего"},
            },
        },
        "uischema": {
            "type": "VerticalLayout",
            "elements": [
                {
                    "type": "Group",
                    "label": "Согласование",
                    "elements": [
                        {"type": "Control", "scope": "#/properties/approvalThreshold"},
                        {"type": "Control", "scope": "#/properties/approverRole"},
                    ],
                },
                {"type": "Control", "scope": "#/properties/reviewDueWorkdays"},
            ],
        },
        "values": {"approvalThreshold": 150000, "approverRole": ROLE},
        "effective": {
            "approvalThreshold": 150000,
            "reviewDueWorkdays": 2,
            "approverRole": ROLE,
        },
        "version": 3,
        "schemaHash": "sha256:" + "0" * 64,
        "updatedBy": ACTOR,
        "updatedAt": "2026-10-03T09:00:00Z",
        "canManage": True,
    },
    "versions": {
        "items": [
            {
                "version": 3,
                "values": {"approvalThreshold": 150000, "approverRole": ROLE},
                "changedPaths": ["/approvalThreshold"],
                "updatedBy": ACTOR,
                "updatedAt": "2026-10-03T09:00:00Z",
            }
        ],
        "nextCursor": None,
    },
    "put": {"values": {"approvalThreshold": 150000, "approverRole": ROLE}},
}
ROUTES = {
    ("/api/v1/package-settings", "get"),
    ("/api/v1/packages/{key}/settings", "get"),
    ("/api/v1/packages/{key}/settings", "put"),
    ("/api/v1/packages/{key}/settings/versions", "get"),
}


def check(name: str, body: Any) -> None:
    """``body`` fits the contract ``name``; the message lists every place it does not."""
    errors = sorted(
        Draft202012Validator(CONTRACT[name]).iter_errors(body), key=lambda e: list(e.path)
    )
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


@pytest.mark.parametrize("name", ["list", "settings", "versions"])
def test_every_example_fits_the_contract(name: str) -> None:
    check(name, EXAMPLES[name])


def test_the_openapi_models_take_the_examples_as_they_are() -> None:
    for model, name in (
        (PackageSettingsListOut, "list"),
        (PackageSettingsOut, "settings"),
        (PackageSettingsVersionPageOut, "versions"),
    ):
        dumped = model.model_validate(EXAMPLES[name]).model_dump(mode="json", by_alias=True)
        check(name, dumped)
    settings = PackageSettingsOut.model_validate(EXAMPLES["settings"])
    assert settings.model_dump(by_alias=True)["schema"] == EXAMPLES["settings"]["schema"]
    assert (
        PackageSettingsPutRequest.model_validate(EXAMPLES["put"]).values
        == (EXAMPLES["put"]["values"])
    )


def test_the_routes_are_where_the_console_opens_them() -> None:
    app = FastAPI()
    app.include_router(api_v1_router)
    spec = app.openapi()
    found = {(path, method) for path, item in spec["paths"].items() for method in item}
    assert found >= ROUTES
    settings = spec["paths"]["/api/v1/packages/{key}/settings"]
    put_codes = set(settings["put"]["responses"])
    assert put_codes >= {"200", "404", "409", "422", "428"}
    request = spec["components"]["schemas"]["PackageSettingsPutRequest"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == {"values"}
    out = spec["components"]["schemas"]["PackageSettingsOut"]
    assert set(out["properties"]) == set(_SETTINGS["properties"])


@pytest.mark.parametrize(
    ("name", "errors"),
    [
        ("secret_material_rejected", [{"path": "/note", "match": "provider_token"}]),
        ("settings_invalid", [{"path": "/approvalThreshold", "code": "minimum", "message": "x"}]),
        ("unknown_ref", [{"path": "/approverRole", "ref": "role"}]),
    ],
)
def test_the_error_bodies_carry_details_errors(name: str, errors: list[dict[str, Any]]) -> None:
    body = {
        "error": {"code": name, "message": "m", "details": {"errors": errors}, "requestId": "r"}
    }
    check(name, body)
