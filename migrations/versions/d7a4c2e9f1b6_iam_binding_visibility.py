"""IAM binding: visibility of work by workspace (CP-ADR-0082 §2).

``iam_principal_bindings.visibility`` — ``tenant`` (every existing row and
the default: permissions act on the whole tenant, the behaviour before) or
``members`` (a human sees the workspaces they are a member of and their
descendants). The mode is a property of the principal across all its active
bindings: one ``members`` binding narrows every entry of that principal.

Operational notes: the column is added with a constant default, so no row is
rewritten and the revision is safe to apply while the API is serving.
Downgrade drops the column: every binding returns to tenant-wide visibility.

Revision ID: d7a4c2e9f1b6
Revises: c8a4e2f6b1d3
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a4c2e9f1b6"
down_revision: str | None = "c8a4e2f6b1d3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "iam_principal_bindings"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("visibility", sa.Text(), nullable=False, server_default=sa.text("'tenant'")),
    )
    op.create_check_constraint(
        op.f("ck_iam_principal_bindings_visibility"),
        _TABLE,
        "visibility IN ('tenant', 'members')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_iam_principal_bindings_visibility"), _TABLE, type_="check")
    op.drop_column(_TABLE, "visibility")
