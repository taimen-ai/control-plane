"""connections: the tenant's accounts of external systems (CP-ADR-0079 §3, §16)

Revision ID: e8b4c2a7f3d9
Revises: d7e3a9c5b2f1
Create Date: 2026-09-30

* ``connections`` — one row per connection, ``(tenant_id, key)`` unique. The
  type is a published version of the tenant's connection type (a foreign key
  on ``(tenant_id, type_key, type_version)``). ``status`` and ``auth`` are
  checked; ``secret_ref`` — the path of the material in the secret store, not
  the material — is set exactly when ``auth`` is. No column holds material.

Downgrade drops the table and every connection with it; material a secret
store keeps stays there for the operator to remove.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8b4c2a7f3d9"
down_revision: str | None = "d7e3a9c5b2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("type_key", sa.Text(), nullable=False),
        sa.Column("type_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("auth", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("connected_by", sa.UUID(), nullable=True),
        sa.Column("connected_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_checked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'expired', 'revoked')",
            name=op.f("ck_connections_status"),
        ),
        sa.CheckConstraint(
            "auth IS NULL OR auth IN ('oauth2', 'token')", name=op.f("ck_connections_auth")
        ),
        sa.CheckConstraint(
            "(secret_ref IS NULL) = (auth IS NULL)",
            name=op.f("ck_connections_secret_ref_with_auth"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(settings) = 'object'", name=op.f("ck_connections_settings_is_object")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_connections_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_connections_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "type_key", "type_version"],
            ["connection_types.tenant_id", "connection_types.key", "connection_types.version"],
            name="fk_connections_type_version",
        ),
        sa.ForeignKeyConstraint(
            ["connected_by"], ["principals.id"], name="fk_connections_connected_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_connections_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_connections_tenant_key"),
    )
    op.create_index(
        "ix_connections_tenant_created", "connections", ["tenant_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_connections_tenant_created", table_name="connections")
    op.drop_table("connections")
