"""Merge the integrations-connections head with the head of main once more.

Revision ID: c7f2a4d9e3b1
Revises: a9c4e7f1b3d6, b6e1f3a8d4c7
Create Date: 2026-10-03

Two parallel lines grew from ``e5c9a3f7b1d4``:

* ``a9c4e7f1b3d6`` — the earlier merge of the integrations-connections line
  (CP-ADR-0079) with main; it already writes ``ck_package_objects_kind_known``
  with both ``ConnectionType`` and ``View``, feature ``integrations-connections``;
* ``a7d3e9c1f5b2`` and ``f4b8d2c6a9e1`` → ``b6e1f3a8d4c7`` — indexes of the
  data under a view (CP-ADR-0080 amendment A) and ``workspaces.task_types``
  (CP-ADR-0008 amendment 2026-10-03 A1), ``main``.

The main line touches neither the connection tables nor the kind constraint of
package objects; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "c7f2a4d9e3b1"
down_revision: tuple[str, str] = ("a9c4e7f1b3d6", "b6e1f3a8d4c7")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
