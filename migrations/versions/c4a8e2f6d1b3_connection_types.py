"""connection types: a versioned immutable catalog kind (CP-ADR-0079 §2, §16)

Revision ID: c4a8e2f6d1b3
Revises: b5d1e7a3c9f4
Create Date: 2026-09-30

* ``connection_types`` — one row per published ``(tenant, key, version)``;
  the publisher names the version, as with skills (ADR-0021). A trigger keeps
  a version immutable: only ``status`` moves forward (active -> deprecated ->
  disabled, active -> disabled) together with ``row_version``; DELETE is
  rejected — a type is never removed;
* ``package_objects`` admits the kind ``ConnectionType``: the installer
  records the types a package brought (``POST /packages:record``).

Downgrade drops the table with every published type and forgets the package
links of the kind.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4a8e2f6d1b3"
down_revision: str | None = "b5d1e7a3c9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = (
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "Skill",
    "WorkRule",
    "Agent",
    "Process",
    "Calendar",
)
KINDS = (*KINDS_BEFORE[:6], "ConnectionType", *KINDS_BEFORE[6:])

_CONNECTION_TYPE_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_connection_type_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'connection_types rows are immutable (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.display_name IS DISTINCT FROM OLD.display_name
        OR NEW.spec IS DISTINCT FROM OLD.spec
        OR NEW.spec_hash IS DISTINCT FROM OLD.spec_hash
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connection type version content is immutable; publish a new version';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (
            (OLD.status = 'active' AND NEW.status IN ('deprecated', 'disabled'))
            OR (OLD.status = 'deprecated' AND NEW.status = 'disabled')
        )
    THEN
        RAISE EXCEPTION 'connection type status may only move active -> deprecated -> disabled';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _in(kinds: Sequence[str]) -> str:
    return "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def upgrade() -> None:
    op.create_table(
        "connection_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated', 'disabled')",
            name=op.f("ck_connection_types_status"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_connection_types_version_positive")),
        sa.CheckConstraint(
            "row_version >= 1", name=op.f("ck_connection_types_row_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_connection_types_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_connection_types_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection_types"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_connection_types_key_version"),
    )
    op.create_index(
        "ix_connection_types_tenant_created",
        "connection_types",
        ["tenant_id", "created_at", "id"],
    )
    op.execute(_CONNECTION_TYPE_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER connection_types_immutable BEFORE UPDATE OR DELETE ON connection_types "
        "FOR EACH ROW EXECUTE FUNCTION forbid_connection_type_mutation()"
    )
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.create_check_constraint(op.f("ck_package_objects_kind_known"), "package_objects", _in(KINDS))


def downgrade() -> None:
    op.execute("DELETE FROM package_objects WHERE kind = 'ConnectionType'")
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.create_check_constraint(
        op.f("ck_package_objects_kind_known"), "package_objects", _in(KINDS_BEFORE)
    )
    op.execute("DROP TRIGGER IF EXISTS connection_types_immutable ON connection_types")
    op.execute("DROP FUNCTION IF EXISTS forbid_connection_type_mutation()")
    op.drop_index("ix_connection_types_tenant_created", table_name="connection_types")
    op.drop_table("connection_types")
