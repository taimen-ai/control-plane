"""profile and version of a principal (CP-ADR-0082 §1)

Revision ID: c8a4e2f6b1d3
Revises: b6e1f3a8d4c7
Create Date: 2026-10-03

* ``principals.version`` — optimistic concurrency of ``PATCH
  /principals/{id}`` (``If-Match: "principal-<version>"``); every existing
  row starts at ``1``.
* ``principals.profile`` — display details of the principal in the
  organization (``jobTitle``, ``email``, ``phone``, ``note``); every existing
  row starts empty. The shape is checked by the application.

Downgrade is lossy: the profiles are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8a4e2f6b1d3"
down_revision: str | None = "b6e1f3a8d4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "principals",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "principals",
        sa.Column(
            "profile",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(op.f("ck_principals_version_positive"), "principals", "version >= 1")
    op.create_check_constraint(
        op.f("ck_principals_profile_object"), "principals", "jsonb_typeof(profile) = 'object'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_principals_profile_object"), "principals", type_="check")
    op.drop_constraint(op.f("ck_principals_version_positive"), "principals", type_="check")
    op.drop_column("principals", "profile")
    op.drop_column("principals", "version")
