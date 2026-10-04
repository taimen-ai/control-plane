"""connection OAuth states: one-time states of :authorize (CP-ADR-0079 §6, §16)

Revision ID: a3f7c1e9d5b2
Revises: e8b4c2a7f3d9
Create Date: 2026-09-30

* ``connection_oauth_states`` — one row per state ``POST
  /connections/{key}:authorize`` issued. Only the SHA-256 of the state is
  kept (``state_hash``, unique, 32 bytes), with the connection, the principal
  that started and the snapshot of its credential (``authority``: credential
  id, permissions, IAM subject — no secret). ``consumed_at`` and ``outcome``
  are set together: the first callback consumes a state, a later
  ``:authorize`` of the connection supersedes it.
* ``connections.oauth_server`` — the name of the OAuth server in the store
  the creds of an ``oauth2`` connection refresh through. Each authorization
  attempt writes a server of its own, and the connection switches to it only
  after a successful exchange; the name is not a secret. Set only with
  ``auth = 'oauth2'``.

Downgrade drops the table and the column; a state not yet used is lost, and its callback
answers ``invalid_state``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3f7c1e9d5b2"
down_revision: str | None = "e8b4c2a7f3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("oauth_server", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_connections_oauth_server_with_oauth2"),
        "connections",
        "oauth_server IS NULL OR auth = 'oauth2'",
    )
    op.create_table(
        "connection_oauth_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("principal_id", sa.UUID(), nullable=False),
        sa.Column("authority", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('consumed', 'authorized', 'failed', 'superseded')",
            name=op.f("ck_connection_oauth_states_outcome"),
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (outcome IS NULL)",
            name=op.f("ck_connection_oauth_states_consumed_with_outcome"),
        ),
        sa.CheckConstraint(
            "octet_length(state_hash) = 32",
            name=op.f("ck_connection_oauth_states_state_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_connection_oauth_states_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_connection_oauth_states_connection_id_connections",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_connection_oauth_states_principal_id_principals",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection_oauth_states"),
        sa.UniqueConstraint("state_hash", name="uq_connection_oauth_states_state_hash"),
    )
    op.create_index(
        "ix_connection_oauth_states_connection",
        "connection_oauth_states",
        ["connection_id", "consumed_at"],
    )
    op.create_index("ix_connection_oauth_states_created", "connection_oauth_states", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_connection_oauth_states_created", table_name="connection_oauth_states")
    op.drop_index("ix_connection_oauth_states_connection", table_name="connection_oauth_states")
    op.drop_table("connection_oauth_states")
    op.drop_constraint(
        op.f("ck_connections_oauth_server_with_oauth2"), "connections", type_="check"
    )
    op.drop_column("connections", "oauth_server")
