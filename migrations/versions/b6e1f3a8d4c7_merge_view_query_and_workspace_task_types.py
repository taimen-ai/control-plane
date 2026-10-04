"""Merge the view query indexes with the task types of a workspace (TASK-001299).

Revision ID: b6e1f3a8d4c7
Revises: a7d3e9c1f5b2, f4b8d2c6a9e1
Create Date: 2026-10-03

Two parallel lines grew from ``e5c9a3f7b1d4``:

* ``a7d3e9c1f5b2`` — indexes of the data under a view (CP-ADR-0080
  amendment A), TASK-001299;
* ``f4b8d2c6a9e1`` — ``workspaces.task_types`` (CP-ADR-0008 amendment
  2026-10-03 A1), TASK-001306, ``main``.

They touch disjoint tables; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "b6e1f3a8d4c7"
down_revision: tuple[str, str] = ("a7d3e9c1f5b2", "f4b8d2c6a9e1")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
