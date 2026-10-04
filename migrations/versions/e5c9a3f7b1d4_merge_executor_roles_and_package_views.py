"""Merge the executor roles of task types with the package views (TASK-001307).

Revision ID: e5c9a3f7b1d4
Revises: b2d7e4a9c1f3, d7a3b9e1c5f2
Create Date: 2026-10-03

Two parallel lines grew from ``c3e8f1a6d2b4``:

* ``b2d7e4a9c1f3`` — ``task_types.executor_roles`` and the immutability
  trigger of task types (CP-ADR-0048 amendment 2026-10-03), TASK-001307;
* ``d7a3b9e1c5f2`` — views, view revisions and package dictionaries
  (CP-ADR-0080), TASK-001298, ``main``.

They touch disjoint tables and columns; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "e5c9a3f7b1d4"
down_revision: tuple[str, str] = ("b2d7e4a9c1f3", "d7a3b9e1c5f2")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
