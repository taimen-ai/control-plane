"""Merge the people-access head with the head of main.

Revision ID: f2c6a8d4e1b9
Revises: d7a4c2e9f1b6, e6a2c8f4b1d7
Create Date: 2026-10-04

Two parallel lines grew from the common history:

* ``c8a4e2f6b1d3`` → ``d7a4c2e9f1b6`` — the profile and version of a principal
  (``principals.profile``, ``principals.version``) and the visibility of an IAM
  binding (``iam_principal_bindings.visibility``), CP-ADR-0082, feature
  ``people-access``;
* ``e6a2c8f4b1d7`` — the merge of the package-settings line (CP-ADR-0081) with
  main (the integrations-connections line, CP-ADR-0079, view query indexes and
  the task types of a workspace), ``main``.

The lines share no column and no constraint: main only references
``principals.id`` by foreign keys; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "f2c6a8d4e1b9"
down_revision: tuple[str, str] = ("d7a4c2e9f1b6", "e6a2c8f4b1d7")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
