"""task verifications: the facts a closed attempt has spent (CP-ADR-0063 Zh7)

Revision ID: d7e3a9c5b2f1
Revises: c4a8e2f6d1b3
Create Date: 2026-09-30

* ``task_verifications.spent_evidence`` — the task's evidence tied to a check
  when the attempt closed (passed, failed or cancelled). A later attempt of the
  same task does not count those facts again: the work handed in anew needs a
  new fact. ``NULL`` for attempts that are open or closed before this column;
  they spend nothing.

Downgrade drops the column: earlier facts count for every attempt again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7e3a9c5b2f1"
down_revision: str | None = "c4a8e2f6d1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_verifications",
        sa.Column("spent_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_verifications", "spent_evidence")
