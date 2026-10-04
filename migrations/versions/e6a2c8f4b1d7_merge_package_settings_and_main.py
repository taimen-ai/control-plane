"""Merge the package-settings head with the head of main.

Revision ID: e6a2c8f4b1d7
Revises: d9b4e2f7a3c1, c7f2a4d9e3b1
Create Date: 2026-10-04

Two parallel lines grew from ``b6e1f3a8d4c7``:

* ``c4f8a2d6e1b9`` → ``d9b4e2f7a3c1`` — the settings of packages
  (``package_settings_schemas``, ``package_settings``,
  ``package_settings_versions``) and the settings version in the journal of a
  process (``process_instance_events.settings_version`` and
  ``settings_schema_revision``), CP-ADR-0081, feature ``package-settings``;
* ``c7f2a4d9e3b1`` — the merge of the integrations-connections line
  (CP-ADR-0079: connections, connection types, OAuth states, names of agents'
  secrets, the author of an observation dedup key) with main, ``main``.

The lines share no table and no constraint; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "e6a2c8f4b1d7"
down_revision: tuple[str, str] = ("d9b4e2f7a3c1", "c7f2a4d9e3b1")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
