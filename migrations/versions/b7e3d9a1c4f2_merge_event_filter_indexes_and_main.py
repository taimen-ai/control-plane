"""Merge the event-journal filter indexes with the head of main.

Revision ID: b7e3d9a1c4f2
Revises: 822493698a3a, 8601ebd0d794
Create Date: 2026-10-04

Two parallel lines grew from ``e6a2c8f4b1d7``:

* ``f2c6a8d4e1b9`` → ``822493698a3a`` — the merge of the people-access line
  (CP-ADR-0082) with main, then a rule action without empty ``fields``
  (CP-ADR-0063, amendment Z1), ``main``;
* ``8601ebd0d794`` — the indexes under the author, workspace and period
  filters of ``GET /events`` (CP-ADR-0068, amendment Б).

The lines share no table: the indexes are on ``events`` and
``event_archive`` only; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "b7e3d9a1c4f2"
down_revision: tuple[str, str] = ("822493698a3a", "8601ebd0d794")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
