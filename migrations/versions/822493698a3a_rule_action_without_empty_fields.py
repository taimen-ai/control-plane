"""a rule action stores no empty ``fields`` (CP-ADR-0063, amendment TASK-001373, Z1)

Revision ID: 822493698a3a
Revises: f2c6a8d4e1b9
Create Date: 2026-10-04

The canonical form of ``work_rules.action`` used to carry ``"fields": {}``
for every action without fields — always so for ``cancel_work`` and
``complete_work``, which take none. The form is now what the author writes:
the member is dropped from the rows that have it empty, so a rule read back
compares equal to its package file. Nothing the rule does changes, so the
rule ``version`` stays.

Downgrade puts the empty member back on every action without one, the form
the previous code stored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "822493698a3a"
down_revision: str | None = "f2c6a8d4e1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE work_rules SET action = action - 'fields' WHERE action -> 'fields' = '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE work_rules SET action = action || '{\"fields\": {}}'::jsonb "
        "WHERE NOT action ? 'fields'"
    )
