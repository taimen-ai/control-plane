"""The body of ``PATCH /principals/{id}`` and its openapi (CP-ADR-0082 §1, §5).

The route checks its body with ``domain/principal_profile`` and documents it
with the models of ``api/v1/schemas``: the two must describe one shape, and
the openapi of the route is the console's contract (constitution, art. V).
"""

import time
from typing import Any

import pytest
from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router
from control_plane.domain.errors import ValidationError
from control_plane.domain.principal_profile import (
    PROFILE_SCHEMA,
    UPDATE_SCHEMA,
    PrincipalUpdate,
    changed_fields,
    parse_update,
    pointer,
)

SECRET = "ghp_" + "a1B2c3D4e5" * 3


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def _bare(schema: dict[str, Any]) -> dict[str, Any]:
    """A schema without the annotations pydantic adds (title, description)."""
    if not isinstance(schema, dict):
        return schema
    return {
        key: (
            {name: _bare(value) for name, value in schema[key].items()}
            if key == "properties"
            else schema[key]
        )
        for key in schema
        if key not in ("title", "description", "$schema")
    }


def _refusal(body: Any) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        parse_update(body)
    return caught.value


# --- openapi -------------------------------------------------------------------


def test_patch_principal_in_openapi_matches_the_adr_draft() -> None:
    spec = _openapi()
    operation = spec["paths"]["/api/v1/principals/{principal_id}"]["patch"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["If-Match"]["in"] == "header"
    assert params["If-Match"]["required"] is True
    assert params["Idempotency-Key"]["required"] is False

    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert _bare(schema) == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "displayName": {"type": "string", "minLength": 1, "maxLength": 200},
            "profile": {"$ref": "#/components/schemas/PrincipalProfile"},
        },
    }

    responses = operation["responses"]
    assert set(responses) >= {"200", "400", "403", "404", "409", "422", "428"}
    assert "ETag" in responses["200"]["headers"]
    assert responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PrincipalOut"
    }
    assert responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/FieldErrorResponse"
    }
    get = spec["paths"]["/api/v1/principals/{principal_id}"]["get"]
    assert "ETag" in get["responses"]["200"]["headers"]


def test_principal_out_and_profile_components() -> None:
    schemas = _openapi()["components"]["schemas"]
    out = schemas["PrincipalOut"]
    assert {"profile", "version"} <= set(out["required"])
    assert out["properties"]["profile"] == {"$ref": "#/components/schemas/PrincipalProfile"}
    assert _bare(out["properties"]["version"]) == {"type": "integer", "minimum": 1}
    # The documented profile is the one the route checks.
    assert _bare(schemas["PrincipalProfile"]) == PROFILE_SCHEMA

    error = schemas["FieldError"]
    assert error["required"] == ["path"]
    assert set(error["properties"]) == {"path", "code", "message", "field", "match"}
    assert schemas["FieldErrorDetails"]["properties"]["errors"]["minItems"] == 1


def test_the_checked_body_is_the_documented_body() -> None:
    documented = _openapi()["paths"]["/api/v1/principals/{principal_id}"]["patch"]
    schema = _bare(documented["requestBody"]["content"]["application/json"]["schema"])
    checked = _bare(UPDATE_SCHEMA)
    assert checked["properties"]["profile"] == PROFILE_SCHEMA
    assert schema["properties"]["displayName"] == checked["properties"]["displayName"]
    assert schema["additionalProperties"] is checked["additionalProperties"] is False


# --- body ----------------------------------------------------------------------


def test_absent_fields_stay_absent() -> None:
    assert parse_update({}) == PrincipalUpdate(display_name=None, profile=None)
    assert parse_update({"profile": {}}) == PrincipalUpdate(display_name=None, profile={})
    assert parse_update({"displayName": " Ann "}).display_name == "Ann"


def test_every_profile_field_is_accepted() -> None:
    profile = {"jobTitle": "a", "email": "a@b.co", "phone": "1", "note": "n"}
    assert parse_update({"profile": profile}).profile == profile


@pytest.mark.parametrize("email", ["a@b", "@b.co", "a@", "a b@c.de", "a@@b.co", "", "a@b.co\n"])
def test_a_malformed_email_is_a_format_error(email: str) -> None:
    errors = _refusal({"profile": {"email": email}}).details["errors"]
    assert errors[0]["path"] == "/profile/email"
    assert errors[0]["code"] in ("format", "minLength")


@pytest.mark.parametrize("email", ["a@b.co", "a.b@c.d.e", "x@-.-", "a+b@c.co"])
def test_a_well_formed_email_is_accepted(email: str) -> None:
    assert parse_update({"profile": {"email": email}}).profile == {"email": email}


@pytest.mark.parametrize("length", [254, 200_000])
def test_a_long_email_without_a_match_is_checked_in_linear_time(length: int) -> None:
    # "a@" and dots up to a final blank once took quadratic time to refuse:
    # 9.6 s for 40 000 dots, inside the route and under its row lock.
    email = "a@" + "." * (length - 3) + " "
    started = time.perf_counter()
    errors = _refusal({"profile": {"email": email}}).details["errors"]
    elapsed = time.perf_counter() - started
    assert [e["path"] for e in errors] == ["/profile/email"]
    assert errors[0]["code"] == ("format" if length <= 254 else "maxLength")
    assert elapsed < 0.05


def test_secret_material_is_found_in_values_member_names_and_nested_arrays() -> None:
    error = _refusal({"profile": {"note": [f"x {SECRET}"]}, "displayName": SECRET})
    assert error.code == "secret_material_rejected"
    assert error.details == {
        "errors": [
            {"path": "/displayName", "match": "provider_token"},
            {"path": "/profile/note/0", "match": "provider_token"},
        ]
    }
    assert SECRET not in error.message
    named = _refusal({"profile": {SECRET: "x"}})
    assert named.details == {"errors": [{"path": "/profile", "match": "provider_token"}]}
    assert _refusal(SECRET).details == {"errors": [{"path": "/", "match": "provider_token"}]}


def test_mentioning_a_credential_is_not_material() -> None:
    assert parse_update({"profile": {"note": "Rotates the API key on Fridays"}}).profile


def test_secret_is_checked_before_the_shape() -> None:
    assert _refusal({"displayName": None, "x": SECRET}).code == "secret_material_rejected"


def test_shape_errors_carry_no_value() -> None:
    value = "q" * 2001
    error = _refusal({"profile": {"note": value, "nickname": "Q"}})
    assert error.code == "validation_error"
    assert value not in str(error.details) and "Q" not in str(error.details)
    assert error.details["errors"] == [
        {
            "path": "/profile/nickname",
            "code": "additionalProperties",
            "message": "is not a known field",
        },
        {"path": "/profile/note", "code": "maxLength", "message": "is too long"},
    ]


def test_pointer_escapes_member_names() -> None:
    assert pointer(()) == "/"
    assert pointer(("profile", "a/b~c")) == "/profile/a~1b~0c"


# --- changes -------------------------------------------------------------------


def test_changes_are_field_names_name_first_then_profile_sorted() -> None:
    current = {"jobTitle": "Clerk", "note": "n"}
    update = PrincipalUpdate(display_name="B", profile={"note": "m", "email": "a@b.co"})
    assert changed_fields(display_name="A", profile=current, update=update) == [
        "displayName",
        "profile.email",
        "profile.jobTitle",
        "profile.note",
    ]


def test_same_values_are_no_change() -> None:
    update = PrincipalUpdate(display_name="A", profile={"note": "n"})
    assert changed_fields(display_name="A", profile={"note": "n"}, update=update) == []
    assert changed_fields(display_name="A", profile={}, update=PrincipalUpdate(None, None)) == []


def test_a_deeply_nested_body_is_a_shape_error_not_a_crash() -> None:
    body: Any = "x"
    for _ in range(5000):
        body = [body]
    error = _refusal({"profile": body})
    assert error.code == "validation_error"
    assert error.details["errors"] == [
        {"path": "/profile", "code": "type", "message": "has the wrong type"}
    ]
