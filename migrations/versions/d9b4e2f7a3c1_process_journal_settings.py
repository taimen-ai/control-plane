"""process_instance_events.settings_version and settings_schema_revision (CP-ADR-0081 §6)

Revision ID: d9b4e2f7a3c1
Revises: c4f8a2d6e1b9
Create Date: 2026-10-03

The settings of a package in the journal of a process (TASK-001316, G005): a
record a step of a process that reads ``settings`` wrote names the version of
the saved values (``0`` — nothing saved) and the revision of the settings
schema the step saw; a replay takes the values of that pair from the history.
Both ``null`` for every other record, so the journals written before stay as
they are.

Downgrade drops the two columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9b4e2f7a3c1"
down_revision: str | None = "c4f8a2d6e1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "process_instance_events", sa.Column("settings_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "process_instance_events",
        sa.Column("settings_schema_revision", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_process_instance_events_settings_pair"),
        "process_instance_events",
        "(settings_version IS NULL) = (settings_schema_revision IS NULL)"
        " AND (settings_version IS NULL OR settings_version >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_process_instance_events_settings_pair"), "process_instance_events", type_="check"
    )
    op.drop_column("process_instance_events", "settings_schema_revision")
    op.drop_column("process_instance_events", "settings_version")
