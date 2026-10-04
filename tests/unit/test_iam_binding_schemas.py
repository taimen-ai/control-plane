"""Wire shape of the IAM binding contract (ADR-0053): camelCase in, camelCase out."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.api.v1.schemas import (
    BootstrapOut,
    BootstrapRequest,
    IamBindingOut,
    IamBindingUpsertRequest,
    IamIdentitySpec,
)

ISSUER = "https://iam.example/iam"


def test_bootstrap_request_binding_is_optional() -> None:
    plain = BootstrapRequest.model_validate(
        {"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin"}
    )
    assert plain.iam_binding is None

    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    bound = BootstrapRequest.model_validate(
        {
            "tenantSlug": "acme",
            "tenantName": "Acme",
            "adminDisplayName": "Admin",
            "iamBinding": {
                "issuer": ISSUER,
                "iamTenantId": str(iam_tenant),
                "iamPrincipalId": str(iam_principal),
            },
        }
    )
    assert bound.iam_binding == IamIdentitySpec(
        issuer=ISSUER, iam_tenant_id=iam_tenant, iam_principal_id=iam_principal
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"permissions": []},
        {"permissions": None},
        {"issuer": ""},
        {"iamTenantId": "not-a-uuid"},
        {"iamPrincipalId": None},
        {"status": "active"},  # not a caller's field: the server decides it
    ],
)
def test_upsert_request_rejects_malformed_bodies(mutation: dict) -> None:
    body = {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
        "permissions": ["tasks.read"],
        **mutation,
    }
    with pytest.raises(ValidationError):
        IamBindingUpsertRequest.model_validate(body)


def test_upsert_request_reads_camel_case() -> None:
    iam_principal = uuid.uuid4()
    request = IamBindingUpsertRequest.model_validate(
        {
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(iam_principal),
            "permissions": ["tasks.read", "tasks.write"],
        }
    )
    assert request.iam_principal_id == iam_principal
    assert request.permissions == ["tasks.read", "tasks.write"]


def test_binding_out_serializes_camel_case_and_bootstrap_carries_it() -> None:
    now = datetime.now(UTC)
    binding = IamBindingOut(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        issuer=ISSUER,
        iam_tenant_id=uuid.uuid4(),
        iam_principal_id=uuid.uuid4(),
        permissions=["admin"],
        status="active",
        visibility="tenant",
        revoked_at=None,
        last_used_at=None,
        created_at=now,
        updated_at=now,
    )
    wire = binding.model_dump(mode="json", by_alias=True)
    assert set(wire) == {
        "id",
        "tenantId",
        "principalId",
        "issuer",
        "iamTenantId",
        "iamPrincipalId",
        "permissions",
        "status",
        "visibility",
        "revokedAt",
        "lastUsedAt",
        "createdAt",
        "updatedAt",
    }
    assert BootstrapOut.model_fields["iam_binding"].alias == "iamBinding"
    assert BootstrapOut.model_fields["iam_binding"].default is None


def test_bootstrap_request_tenant_id_is_optional() -> None:
    from control_plane.api.v1.schemas import BootstrapRequest

    plain = BootstrapRequest(tenantSlug="acme", tenantName="Acme", adminDisplayName="A")
    assert plain.tenant_id is None
    given = BootstrapRequest(
        tenantSlug="acme",
        tenantName="Acme",
        adminDisplayName="A",
        tenantId="3f2b9a6e-1c4d-4e8f-9a0b-5c6d7e8f9a0b",
    )
    assert str(given.tenant_id) == "3f2b9a6e-1c4d-4e8f-9a0b-5c6d7e8f9a0b"
