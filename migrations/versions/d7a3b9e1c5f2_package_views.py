"""views, view_revisions, package_dictionaries: screens of a package (CP-ADR-0080)

Revision ID: d7a3b9e1c5f2
Revises: c3e8f1a6d2b4
Create Date: 2026-10-03

* ``views`` — one row per ``(tenant, key)`` of a view: its latest revision and
  whether it is in use (``active`` / ``retired``).
* ``view_revisions`` — the immutable revisions of a view: the checked form
  (spec with components inlined, texts by locale) and its hash.
* ``package_dictionaries`` — the dictionaries of a package
  (``i18n/<locale>.yaml``), a revision per change.
* ``package_objects`` links the kind ``View`` too, with what the apply wanted.

Downgrade drops the tables and the links of views.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a3b9e1c5f2"
down_revision: str | None = "c3e8f1a6d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'ArtifactType', 'TaskType', 'ProjectTemplate', 'WorkspaceType', 'Role', "
    "'Capability', 'Skill', 'WorkRule', 'Agent', 'Process', 'Calendar'"
)
_PLANNED = (
    " OR (plan_hash IS NOT NULL AND version IS NOT NULL"
    " AND spec IS NOT NULL AND spec_hash IS NOT NULL)"
)


def _package_objects(kinds: str, planned: str) -> None:
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.drop_constraint(op.f("ck_package_objects_planned_spec"), "package_objects", type_="check")
    op.create_check_constraint(
        op.f("ck_package_objects_kind_known"), "package_objects", f"kind IN ({kinds})"
    )
    op.create_check_constraint(
        op.f("ck_package_objects_planned_spec"),
        "package_objects",
        f"kind NOT IN ({planned}){_PLANNED}",
    )


def upgrade() -> None:
    op.create_table(
        "views",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_by", sa.UUID(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'retired')", name=op.f("ck_views_status_known")),
        sa.CheckConstraint("current_revision >= 1", name=op.f("ck_views_revision_positive")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_views_tenant_id_tenants"),
        sa.ForeignKeyConstraint(
            ["retired_by"], ["principals.id"], name="fk_views_retired_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_views"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_views_tenant_key"),
    )
    op.create_table(
        "view_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("view_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("audience_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("package_key", sa.Text(), nullable=True),
        sa.Column("package_version", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_view_revisions_revision_positive")),
        sa.CheckConstraint(
            "source_kind IN ('process', 'tasks', 'knowledge')",
            name=op.f("ck_view_revisions_source_known"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_view_revisions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(["view_id"], ["views.id"], name="fk_view_revisions_view_id_views"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_view_revisions_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_view_revisions"),
        sa.UniqueConstraint("view_id", "revision", name="uq_view_revisions_view_revision"),
    )
    op.create_table(
        "package_dictionaries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("package_version", sa.Text(), nullable=True),
        sa.Column("locales", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_locale", sa.Text(), nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_package_dictionaries_revision_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_package_dictionaries_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_package_dictionaries_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_package_dictionaries"),
        sa.UniqueConstraint(
            "tenant_id", "package_key", "revision", name="uq_package_dictionaries_package_revision"
        ),
    )
    _package_objects(f"{_KINDS}, 'View'", "'Process', 'Calendar', 'View'")


def downgrade() -> None:
    op.execute("DELETE FROM package_objects WHERE kind = 'View'")
    _package_objects(_KINDS, "'Process', 'Calendar'")
    op.drop_table("package_dictionaries")
    op.drop_table("view_revisions")
    op.drop_table("views")
