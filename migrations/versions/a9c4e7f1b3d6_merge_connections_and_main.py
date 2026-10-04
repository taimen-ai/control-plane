"""Merge the integrations-connections head with the head of main.

Revision ID: a9c4e7f1b3d6
Revises: b7e3d1f9c4a2, e5c9a3f7b1d4
Create Date: 2026-10-03

Two parallel lines grew from ``b5d1e7a3c9f4``:

* ``c4a8e2f6d1b3`` → ``d7e3a9c5b2f1`` → ``e8b4c2a7f3d9`` → ``a3f7c1e9d5b2`` →
  ``f4d2b8e6a1c3`` → ``b7e3d1f9c4a2`` — connection types, spent verification
  evidence, connections, OAuth states, the author in the observation dedup
  key and the names of agents' secrets (CP-ADR-0079), feature
  ``integrations-connections``;
* ``16a16d12fe3f`` → ``c3e8f1a6d2b4`` → (``b2d7e4a9c1f3``, ``d7a3b9e1c5f2``) →
  ``e5c9a3f7b1d4`` — process observability, catalog retirements, executor
  roles of task types and package views (CP-ADR-0078, CP-ADR-0074 amendment
  Zh1, CP-ADR-0048 amendment 2026-10-03, CP-ADR-0080), ``main``.

The lines touch disjoint tables, except one constraint: each rewrote
``ck_package_objects_kind_known`` with its own new kind — ``ConnectionType``
(``c4a8e2f6d1b3``) and ``View`` (``d7a3b9e1c5f2``) — so after both the
constraint holds whichever line ran last. The merge writes it once more with
both kinds, as the model declares it.

Downgrade keeps the wider constraint: each parent line drops its own kind
when it is downgraded.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a9c4e7f1b3d6"
down_revision: tuple[str, str] = ("b7e3d1f9c4a2", "e5c9a3f7b1d4")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

KINDS = (
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "ConnectionType",
    "Skill",
    "WorkRule",
    "Agent",
    "Process",
    "Calendar",
    "View",
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.create_check_constraint(
        op.f("ck_package_objects_kind_known"),
        "package_objects",
        "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")",
    )


def downgrade() -> None:
    """The wider constraint stays; the parent lines narrow it when downgraded."""
