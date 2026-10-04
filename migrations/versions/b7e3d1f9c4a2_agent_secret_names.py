"""agent secret names: the names of agents' secrets by name (CP-ADR-0079 §11, §16)

Revision ID: b7e3d1f9c4a2
Revises: f4d2b8e6a1c3
Create Date: 2026-10-01

* ``agent_secret_names`` — one row per secret ``PUT
  /agents/{key}/secrets/{name}`` set: the agent, the name, who set it first
  and last and when. The value is in the secret store
  (``kv/data/tenants/<t>/agents/<key>/<name>``), never in a table. The name has
  the form of ``placement.secrets`` (CHECK).

Downgrade drops the table: the names are lost, the documents stay in the
store and the operator deletes them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e3d1f9c4a2"
down_revision: str | None = "f4d2b8e6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_secret_names",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9][a-z0-9-]{0,62}$'",
            name=op.f("ck_agent_secret_names_name_format"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_agent_secret_names_agent_id_agents"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_agent_secret_names_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_agent_secret_names_created_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["principals.id"], name="fk_agent_secret_names_updated_by_principals"
        ),
        sa.PrimaryKeyConstraint("agent_id", "name", name="pk_agent_secret_names"),
    )
    op.create_index("ix_agent_secret_names_tenant", "agent_secret_names", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_secret_names_tenant", table_name="agent_secret_names")
    op.drop_table("agent_secret_names")
