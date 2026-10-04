"""observation_dedup_keys: the author is part of the key (CP-ADR-0057, 2026-10-01)

Revision ID: f4d2b8e6a1c3
Revises: a3f7c1e9d5b2
Create Date: 2026-10-01

A ``(source, dedupKey)`` pair shared by the whole tenant let any author take
a trusted observer's predictable key in advance and silence its fact. The key
becomes ``(tenant_id, source, dedup_key, actor_id)``: every author dedups its
own repeats, and a rule's author filter (CP-ADR-0063 Zh6) tells the facts
apart.

Upgrade fills ``actor_id`` from the journal event the key produced, live or
archived (ADR-0038). A key whose event is in neither has no provable author
and is dropped: its next report records a new observation, as after the
downgrade-upgrade roundtrip of the table itself.

Downgrade is lossy: of the rows that share ``(tenant_id, source, dedup_key)``
it keeps the earliest (``recorded_at``, then ``event_id``) and drops the rest
of the keys — the observations stay in the journal; only the repeat of a
dropped author resolves to the kept one afterwards.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4d2b8e6a1c3"
down_revision: str | None = "a3f7c1e9d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("observation_dedup_keys", sa.Column("actor_id", sa.UUID(), nullable=True))
    for journal in ("events", "event_archive"):
        op.execute(
            f"""
            UPDATE observation_dedup_keys AS k
               SET actor_id = j.actor_id
              FROM {journal} AS j
             WHERE k.actor_id IS NULL
               AND j.tenant_id = k.tenant_id
               AND j.id = k.event_id
            """
        )
    op.execute("DELETE FROM observation_dedup_keys WHERE actor_id IS NULL")
    op.alter_column("observation_dedup_keys", "actor_id", nullable=False)
    op.drop_constraint("pk_observation_dedup_keys", "observation_dedup_keys", type_="primary")
    op.create_primary_key(
        "pk_observation_dedup_keys",
        "observation_dedup_keys",
        ["tenant_id", "source", "dedup_key", "actor_id"],
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM observation_dedup_keys AS k
         USING (
            SELECT tenant_id, source, dedup_key, actor_id,
                   row_number() OVER (
                       PARTITION BY tenant_id, source, dedup_key
                       ORDER BY recorded_at, event_id
                   ) AS rank
              FROM observation_dedup_keys
         ) AS ranked
         WHERE ranked.rank > 1
           AND k.tenant_id = ranked.tenant_id
           AND k.source = ranked.source
           AND k.dedup_key = ranked.dedup_key
           AND k.actor_id = ranked.actor_id
        """
    )
    op.drop_constraint("pk_observation_dedup_keys", "observation_dedup_keys", type_="primary")
    op.drop_column("observation_dedup_keys", "actor_id")
    op.create_primary_key(
        "pk_observation_dedup_keys", "observation_dedup_keys", ["tenant_id", "source", "dedup_key"]
    )
