"""v0.6 -> integrated v0.7 -> v0.6 migration matrix.

The v0.7 merge head combined Effective Harness Manifest and Durable Active
Turn Control, which were developed as parallel additive revisions from v0.6.
The manifests were dropped again with CP-ADR-0073 (declarative-agents, D007).
Durable Child Run Handle extends the same line, so the roundtrip is always run
against the current head rather than a frozen intermediate revision: the
server code expects the schema of the head it ships with.
"""

from collections.abc import Iterator

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

V06_HEAD = "72ef8bc31a06"
CURRENT_HEAD = "b7e3d9a1c4f2"
# The revision right before the harness manifests were dropped (D007).
BEFORE_MANIFEST_DROP = "d7e2a9c4f1b8"
MANIFEST_TABLES = {"run_harness_manifests", "run_manifest_ephemerals"}
CHILD_TABLES = {"run_child_handles", "run_child_results"}

pytestmark = pytest.mark.usefixtures("clean_database")


@pytest.fixture
def v07_alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


def _manifest_objects(engine: Engine) -> tuple[set[str], int]:
    with engine.connect() as connection:
        functions = connection.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'reject_manifest_mutation'")
        ).scalar_one()
    return MANIFEST_TABLES & set(inspect(engine).get_table_names()), functions


def test_harness_manifests_are_dropped_and_restored_empty(
    sync_engine: Engine, v07_alembic_config: Config
) -> None:
    """CP-ADR-0073 supersedes CP-ADR-0043: the head carries no manifest tables."""
    assert _manifest_objects(sync_engine) == (set(), 0)

    alembic_command.downgrade(v07_alembic_config, BEFORE_MANIFEST_DROP)
    assert _manifest_objects(sync_engine) == (MANIFEST_TABLES, 1)
    with sync_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM run_harness_manifests")).scalar_one() == 0
        )

    alembic_command.upgrade(v07_alembic_config, "head")
    with sync_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == CURRENT_HEAD
    assert _manifest_objects(sync_engine) == (set(), 0)


def test_active_turn_control_migration_roundtrip(
    sync_engine: Engine, v07_alembic_config: Config
) -> None:
    inspector = inspect(sync_engine)
    assert "run_control_messages" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("run_control_messages")} >= {
        "run_id",
        "seq",
        "operation",
        "status",
        "causal_position",
        "idempotency_key",
        "version",
        "resolved_at",
    }
    assert {index["name"] for index in inspector.get_indexes("run_control_messages")} >= {
        "ix_run_control_messages_run",
        "ix_run_control_messages_tenant_run",
        "ix_run_control_messages_accepted",
    }

    alembic_command.downgrade(v07_alembic_config, V06_HEAD)
    tables = set(inspect(sync_engine).get_table_names())
    assert "run_control_messages" not in tables
    assert not (MANIFEST_TABLES & tables)
    assert not (CHILD_TABLES & tables)

    alembic_command.upgrade(v07_alembic_config, "head")
    with sync_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == CURRENT_HEAD
    tables = set(inspect(sync_engine).get_table_names())
    assert "run_control_messages" in tables
    assert not (MANIFEST_TABLES & tables)
    assert tables >= CHILD_TABLES
