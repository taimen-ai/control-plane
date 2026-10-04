"""task types allowed in a workspace (CP-ADR-0008, amendment 2026-10-03 A1)

Revision ID: f4b8d2c6a9e1
Revises: e5c9a3f7b1d4
Create Date: 2026-10-03

* ``workspaces.task_types`` — ``[<task type key>]``, the task types work of
  this workspace may have. ``NULL`` (every existing row) — inherited from the
  nearest ancestor that sets it; set nowhere — every type is allowed, the
  behaviour before. ``[]`` — explicitly none. Keys are checked by the
  application on ``PATCH /workspaces/{id}``.

Downgrade is lossy: the configured lists are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b8d2c6a9e1"
down_revision: str | None = "e5c9a3f7b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("task_types", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_workspaces_task_types_array"),
        "workspaces",
        "task_types IS NULL OR jsonb_typeof(task_types) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workspaces_task_types_array"), "workspaces", type_="check")
    op.drop_column("workspaces", "task_types")
