"""package_settings_schemas, package_settings, package_settings_versions (CP-ADR-0081 §3)

Revision ID: c4f8a2d6e1b9
Revises: b6e1f3a8d4c7
Create Date: 2026-10-03

Settings of a package (TASK-001315, G004):

* ``package_settings_schemas`` — the settings schema of a package by revision,
  written by ``POST /packages:apply``; at most one active revision a package
  (a partial unique index);
* ``package_settings`` — the saved values, one row a package from its first
  ``PUT /packages/{key}/settings``;
* ``package_settings_versions`` — every saving, insert only.

Downgrade drops the three tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4f8a2d6e1b9"
down_revision: str | None = "b6e1f3a8d4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "package_settings_schemas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("package_version", sa.Text(), nullable=True),
        sa.Column("schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uischema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schema_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("applied_by", sa.UUID(), nullable=False),
        sa.Column("applied_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_package_settings_schemas_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_package_settings_schemas_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["applied_by"],
            ["principals.id"],
            name="fk_package_settings_schemas_applied_by_principals",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_package_settings_schemas"),
        sa.UniqueConstraint(
            "tenant_id",
            "package_key",
            "revision",
            name="uq_package_settings_schemas_package_revision",
        ),
    )
    op.create_index(
        "uq_package_settings_schemas_active",
        "package_settings_schemas",
        ["tenant_id", "package_key"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "package_settings",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_package_settings_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_package_settings_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["principals.id"], name="fk_package_settings_updated_by_principals"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "package_key", name="pk_package_settings"),
    )
    op.create_table(
        "package_settings_versions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("changed_paths", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_package_settings_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_package_settings_versions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["principals.id"],
            name="fk_package_settings_versions_updated_by_principals",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "package_key", "version", name="pk_package_settings_versions"
        ),
    )


def downgrade() -> None:
    op.drop_table("package_settings_versions")
    op.drop_table("package_settings")
    op.drop_index("uq_package_settings_schemas_active", table_name="package_settings_schemas")
    op.drop_table("package_settings_schemas")
