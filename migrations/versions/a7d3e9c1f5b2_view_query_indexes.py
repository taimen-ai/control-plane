"""indexes of the data under a view: instances by data and by process (CP-ADR-0080 amendment A)

Revision ID: a7d3e9c1f5b2
Revises: e5c9a3f7b1d4
Create Date: 2026-10-03

Schema (TAI-ADR-0066 p.4, stage 2, ``POST /views/{key}:query``):

* ``ix_process_instances_data`` — GIN (``jsonb_path_ops``) over
  ``process_instances.data``: an equality of a view's filter on a data field
  is ``data @> {...}``, which it answers.
* ``ix_process_instances_tenant_definition_started`` — the instances of one
  process of a tenant, newest first: the order of a page and its keyset.

No data changes; downgrade drops both indexes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7d3e9c1f5b2"
down_revision: str | None = "e5c9a3f7b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_process_instances_data",
        "process_instances",
        ["data"],
        postgresql_using="gin",
        postgresql_ops={"data": "jsonb_path_ops"},
    )
    op.create_index(
        "ix_process_instances_tenant_definition_started",
        "process_instances",
        ["tenant_id", "definition_key", "started_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_process_instances_tenant_definition_started", table_name="process_instances")
    op.drop_index("ix_process_instances_data", table_name="process_instances")
