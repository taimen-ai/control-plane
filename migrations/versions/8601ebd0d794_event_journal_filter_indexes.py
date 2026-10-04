"""event journal: indexes under the author, workspace and period filters

Revision ID: 8601ebd0d794
Revises: e6a2c8f4b1d7
Create Date: 2026-10-03

``GET /events`` narrows by ``actorId``, by the period ``occurredFrom`` /
``occurredTo`` and by one workspace (``includeDescendants=false``),
CP-ADR-0068 amendment Б. On ``events`` and ``event_archive`` alike (a reader
walks both, ADR-0038):

* ``(tenant_id, actor_id, tx_id, sequence)`` and
  ``(tenant_id, workspace_id, tx_id, sequence)`` — an equality on the author
  or the workspace followed by the replay order: the page is an ordered range
  scan in either direction, a rare author is not found by scanning the
  journal;
* ``(tenant_id, occurred_at)`` — a period is a range of this index: a
  quarter's audit reads the quarter, not the journal before or after it.

No rows change. The index builds are NOT ``CONCURRENTLY`` (Alembic runs DDL
in a transaction): on a large journal they hold a SHARE lock and block event
writes for the build — schedule the upgrade in a maintenance window.

Downgrade drops the indexes; the filters keep working by scanning.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8601ebd0d794"
down_revision: str | None = "e6a2c8f4b1d7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEXES = (
    ("tenant_actor_tx_sequence", ["tenant_id", "actor_id", "tx_id", "sequence"]),
    ("tenant_workspace_tx_sequence", ["tenant_id", "workspace_id", "tx_id", "sequence"]),
    ("tenant_occurred_at", ["tenant_id", "occurred_at"]),
)


def upgrade() -> None:
    for table in ("events", "event_archive"):
        for suffix, columns in _INDEXES:
            op.create_index(f"ix_{table}_{suffix}", table, columns)


def downgrade() -> None:
    for table in ("events", "event_archive"):
        for suffix, _columns in _INDEXES:
            op.drop_index(f"ix_{table}_{suffix}", table_name=table)
