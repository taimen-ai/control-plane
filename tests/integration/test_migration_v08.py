"""v0.7 -> v0.8 -> v0.7 work item type migration matrix (ADR-0048).

The claim the migration makes is narrow and testable: existing tasks keep
their statuses, keep being claimable, and gain a type and a category that mean
the same thing the old six-value enumeration meant. Downgrade is lossy, and
the test pins exactly HOW it is lossy rather than pretending it is not.
"""

import uuid
from collections.abc import Iterator

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)
from tests.integration.test_skill_invocations_m21 import claim, invoke, published

V07_HEAD = "f5b91c3e7a24"
V08_TYPES = "c8a51d70b394"
# Second revision of the v0.8 line: custom fields and planned dates (ADR-0049).
V08_FIELDS = "a1c7e94b2f60"
# The current head of the chain the v0.8 tests upgrade back to: the merge of
# the event-journal filter indexes (CP-ADR-0068, amendment B) with main (a rule
# action stores no empty fields, CP-ADR-0063 amendment Z1, on top of the merge
# of the people-access line, CP-ADR-0082, with main).
V08_HEAD = "b7e3d9a1c4f2"
# The revision right before the agent registry.
BEFORE_AGENT_REGISTRY = "c3f8a2d6e1b7"
# The revision right before attention feedback (CP-ADR-0068 approval workspaces).
BEFORE_ATTENTION_FEEDBACK = "d2f8b4a6e1c3"
# Observation dedup keys (CP-ADR-0057) and the revision right before them.
OBSERVATION_DEDUP = "b3e7d1f9c2a4"
BEFORE_OBSERVATION_DEDUP = "a9c4e2d7f1b3"

pytestmark = pytest.mark.usefixtures("clean_database")


@pytest.fixture
def v08_alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_existing_tasks_keep_their_status_and_gain_a_category(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    backlog = await create_task(client, admin_key, title="Backlog", status="backlog")
    todo = await create_task(client, admin_key, title="Todo")
    blocked_task = await create_task(client, admin_key, title="Blocked")
    await client.patch(
        f"/api/v1/tasks/{blocked_task['id']}",
        json={"status": "blocked"},
        headers={**auth(admin_key), "If-Match": f'"task-{blocked_task["version"]}"'},
    )

    alembic_command.downgrade(v08_alembic_config, V07_HEAD)
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    with sync_engine.connect() as connection:
        rows = dict(
            connection.execute(
                text("SELECT public_id, status || '/' || system_status_category FROM tasks")
            ).all()
        )
        typed = connection.execute(
            text(
                "SELECT count(*) FROM tasks t JOIN task_types tt ON tt.id = t.type_id"
                " WHERE tt.key = 'task' AND tt.version = 1 AND tt.tenant_id = t.tenant_id"
            )
        ).scalar_one()
    assert rows[backlog["publicId"]] == "backlog/backlog"
    assert rows[todo["publicId"]] == "todo/active"
    assert rows[blocked_task["publicId"]] == "blocked/blocked"
    assert typed == 3


async def test_a_blocked_task_survives_the_roundtrip_claimable(
    client: httpx.AsyncClient, v08_alembic_config: Config
) -> None:
    """Pre-v0.8 claimability was "not done, not cancelled" — including blocked."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Blocked")
    await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "blocked"},
        headers={**auth(admin_key), "If-Match": f'"task-{task["version"]}"'},
    )

    alembic_command.downgrade(v08_alembic_config, V07_HEAD)
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    session = await open_session(client, agent_key)
    claimed = await claim_task(client, agent_key, task["id"], session["id"])
    assert claimed.status_code == 200, claimed.text


async def test_downgrade_removes_the_registry_and_collapses_custom_keys(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={
            "key": "question",
            "displayName": "Question",
            "lifecycleSchema": {
                "initialStatus": "review",
                "statuses": [
                    {"key": "review", "category": "active"},
                    {"key": "answered", "category": "terminal_success"},
                ],
                "transitions": [{"from": "review", "to": ["answered"]}],
            },
        },
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    task = await create_task(client, admin_key, title="Why?", typeKey="question")
    assert task["status"] == "review"

    alembic_command.downgrade(v08_alembic_config, V07_HEAD)

    inspector = inspect(sync_engine)
    assert "task_types" not in inspector.get_table_names()
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    assert "type_id" not in task_columns and "system_status_category" not in task_columns
    with sync_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM tasks WHERE public_id = :pid"), {"pid": task["publicId"]}
        ).scalar_one()
        # `review` has no representation in the six-value CHECK: it collapses
        # onto the legacy key of its category. Documented, irreversible.
        assert status == "todo"
        # And the old constraint is back in force.
        with pytest.raises(Exception, match="ck_tasks_status"):
            connection.execute(
                text("UPDATE tasks SET status = 'review' WHERE public_id = :pid"),
                {"pid": task["publicId"]},
            )


async def test_fields_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """The second v0.8 revision adds three columns and takes nothing away."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    dated = await create_task(
        client,
        admin_key,
        title="Dated",
        dueDate="2026-09-01T12:00:00Z",
        customFields={"component": "iam"},
    )

    alembic_command.downgrade(v08_alembic_config, V08_TYPES)

    inspector = inspect(sync_engine)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    assert not ({"custom_fields", "start_date", "due_date"} & columns)
    # The task itself survives: the type registry is untouched by this revision.
    with sync_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM tasks WHERE public_id = :pid"), {"pid": dated["publicId"]}
            ).scalar_one()
            == dated["status"]
        )

    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    with sync_engine.connect() as connection:
        row = connection.execute(
            text("SELECT custom_fields, due_date FROM tasks WHERE public_id = :pid"),
            {"pid": dated["publicId"]},
        ).one()
    # Re-created empty, per the documented (lossy) downgrade.
    assert row[0] == {}
    assert row[1] is None


async def test_comments_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """The third v0.8 revision only ADDS two tables — no task is touched.

    Downgrade drops the thread and its audit with it, which is the documented
    (lossy) rollback; what must survive is everything that existed before.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Discussed")
    created = await client.post(
        f"/api/v1/tasks/{task['publicId']}/comments",
        json={"body": "First reply"},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text

    alembic_command.downgrade(v08_alembic_config, V08_FIELDS)

    inspector = inspect(sync_engine)
    tables = set(inspector.get_table_names())
    assert not ({"task_comments", "task_comment_revisions"} & tables)
    with sync_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM tasks WHERE public_id = :pid"), {"pid": task["publicId"]}
            ).scalar_one()
            == task["status"]
        )

    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    with sync_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM task_comments")).scalar_one() == 0
    # The task is still commentable after the round trip.
    again = await client.post(
        f"/api/v1/tasks/{task['publicId']}/comments",
        json={"body": "Reply after the roundtrip"},
        headers=auth(admin_key),
    )
    assert again.status_code == 201, again.text


async def test_observation_dedup_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    body = {"kind": "external_fact", "content": "x", "source": "ci", "dedupKey": "k"}
    first = await client.post("/api/v1/observations", json=body, headers=auth(admin_key))
    assert first.status_code == 201, first.text

    alembic_command.downgrade(v08_alembic_config, BEFORE_OBSERVATION_DEDUP)
    assert "observation_dedup_keys" not in inspect(sync_engine).get_table_names()
    alembic_command.upgrade(v08_alembic_config, OBSERVATION_DEDUP)
    inspector = inspect(sync_engine)
    assert inspector.get_pk_constraint("observation_dedup_keys")["constrained_columns"] == [
        "tenant_id",
        "source",
        "dedup_key",
    ]
    # The running code needs the rest of the chain (later revisions add
    # journal columns); the keys table stays as the roundtrip left it.
    alembic_command.upgrade(v08_alembic_config, "head")
    # Keys are not rebuilt from the journal: a repeat after the lossy
    # roundtrip records a new observation, then dedups again.
    again = await client.post("/api/v1/observations", json=body, headers=auth(admin_key))
    assert again.status_code == 201
    repeat = await client.post("/api/v1/observations", json=body, headers=auth(admin_key))
    assert repeat.status_code == 200
    assert repeat.json()["id"] == again.json()["id"]


# The author joins the observation dedup key (CP-ADR-0057, 2026-10-01).
DEDUP_KEY_AUTHOR = "f4d2b8e6a1c3"
BEFORE_DEDUP_KEY_AUTHOR = "a3f7c1e9d5b2"


def _dedup_rows(sync_engine: Engine) -> list[tuple[str, str]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT dedup_key, CAST(observation_id AS text) FROM observation_dedup_keys")
        ).all()
    return sorted((row[0], row[1]) for row in rows)


async def test_dedup_key_author_revision_roundtrips_shared_keys(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """Downgrade keeps the earliest author of a shared key; upgrade takes the
    author from the journal and drops a key whose event is gone."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, first_key = await create_agent_with_key(client, admin_key, name="first")
    _, second_key = await create_agent_with_key(client, admin_key, name="second")
    body = {"kind": "external_fact", "content": "x", "source": "ci", "dedupKey": "shared"}
    first = await client.post("/api/v1/observations", json=body, headers=auth(first_key))
    second = await client.post("/api/v1/observations", json=body, headers=auth(second_key))
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
    assert len(_dedup_rows(sync_engine)) == 2

    alembic_command.downgrade(v08_alembic_config, BEFORE_DEDUP_KEY_AUTHOR)
    inspector = inspect(sync_engine)
    assert inspector.get_pk_constraint("observation_dedup_keys")["constrained_columns"] == [
        "tenant_id",
        "source",
        "dedup_key",
    ]
    assert _dedup_rows(sync_engine) == [("shared", first.json()["id"])]
    # A key whose journal event is gone has no provable author.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO observation_dedup_keys (tenant_id, source, dedup_key, "
                "observation_id, event_id, kind, recorded_at) "
                "VALUES (:tenant, 'ci', 'orphan', gen_random_uuid(), gen_random_uuid(), "
                "'external_fact', now())"
            ),
            {"tenant": boot["tenant"]["id"]},
        )

    alembic_command.upgrade(v08_alembic_config, DEDUP_KEY_AUTHOR)
    inspector = inspect(sync_engine)
    assert inspector.get_pk_constraint("observation_dedup_keys")["constrained_columns"] == [
        "tenant_id",
        "source",
        "dedup_key",
        "actor_id",
    ]
    assert _dedup_rows(sync_engine) == [("shared", first.json()["id"])]
    alembic_command.upgrade(v08_alembic_config, "head")

    # The kept author still dedups; the dropped one records anew, then dedups.
    repeat = await client.post("/api/v1/observations", json=body, headers=auth(first_key))
    assert (repeat.status_code, repeat.json()["id"]) == (200, first.json()["id"])
    again = await client.post("/api/v1/observations", json=body, headers=auth(second_key))
    assert again.status_code == 201
    twice = await client.post("/api/v1/observations", json=body, headers=auth(second_key))
    assert (twice.status_code, twice.json()["id"]) == (200, again.json()["id"])


# The names of agents' secrets (CP-ADR-0079 §11, §16; I012).
AGENT_SECRET_NAMES = "b7e3d1f9c4a2"


async def test_agent_secret_names_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """The revision only adds the table; its CHECK keeps a name a name; downgrade drops it."""
    boot = await do_bootstrap(client)
    alembic_command.downgrade(v08_alembic_config, DEDUP_KEY_AUTHOR)
    assert "agent_secret_names" not in inspect(sync_engine).get_table_names()
    alembic_command.upgrade(v08_alembic_config, AGENT_SECRET_NAMES)
    inspector = inspect(sync_engine)
    assert inspector.get_pk_constraint("agent_secret_names")["constrained_columns"] == [
        "agent_id",
        "name",
    ]
    assert {column["name"] for column in inspector.get_columns("agent_secret_names")} == {
        "agent_id",
        "name",
        "tenant_id",
        "created_by",
        "created_at",
        "updated_by",
        "updated_at",
    }
    insert = text(
        "INSERT INTO agents (id, tenant_id, key, display_name, status, state, replicas, "
        "current_revision, version, created_by, created_at, updated_at) "
        "VALUES (:id, :tenant, 'runner', 'Runner', 'active', 'stopped', 0, 1, 1, :admin, "
        "now(), now())"
    )
    name = text(
        "INSERT INTO agent_secret_names (agent_id, name, tenant_id, created_by, created_at, "
        "updated_by, updated_at) VALUES (:agent, :name, :tenant, :admin, now(), :admin, now())"
    )
    agent_id = uuid.uuid4()
    ids = {"tenant": boot["tenant"]["id"], "admin": boot["adminPrincipal"]["id"]}
    with sync_engine.begin() as conn:
        conn.execute(insert, {"id": agent_id, **ids})
        conn.execute(name, {"agent": agent_id, "name": "gh-token", **ids})
    for bad in ("Upper", "a/b", "-x", "a" * 64):
        with pytest.raises(IntegrityError), sync_engine.begin() as conn:
            conn.execute(name, {"agent": agent_id, "name": bad, **ids})
    alembic_command.downgrade(v08_alembic_config, DEDUP_KEY_AUTHOR)
    assert "agent_secret_names" not in inspect(sync_engine).get_table_names()
    alembic_command.upgrade(v08_alembic_config, "head")


async def test_attention_feedback_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """The revision only ADDS the feedback table; downgrade drops the verdicts."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Kept")

    alembic_command.downgrade(v08_alembic_config, BEFORE_ATTENTION_FEEDBACK)
    assert "attention_feedback" not in inspect(sync_engine).get_table_names()
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    inspector = inspect(sync_engine)
    assert "attention_feedback" in inspector.get_table_names()
    assert {c["name"] for c in inspector.get_unique_constraints("attention_feedback")} == {
        "uq_attention_feedback_item"
    }
    response = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert (await client.get("/api/v1/me/attention", headers=auth(admin_key))).status_code == 200


async def test_agent_registry_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """The revision only ADDS the registry and a nullable run column; downgrade drops them."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Kept")

    alembic_command.downgrade(v08_alembic_config, BEFORE_AGENT_REGISTRY)
    inspector = inspect(sync_engine)
    assert not {"agents", "agent_revisions", "agent_status"} & set(inspector.get_table_names())
    assert "agent_revision_id" not in {c["name"] for c in inspector.get_columns("runs")}
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    inspector = inspect(sync_engine)
    assert {"agents", "agent_revisions", "agent_status"} <= set(inspector.get_table_names())
    assert "agent_revision_id" in {c["name"] for c in inspector.get_columns("runs")}
    with sync_engine.connect() as connection:
        triggers = connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'agent_revisions'::regclass")
        ).scalars()
        assert "agent_revisions_immutable" in set(triggers)
    response = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert (await client.get("/api/v1/agents", headers=auth(admin_key))).status_code == 200


# Separation of duties on an approval (CP-ADR-0074 §7) and the revision right
# before it.
BEFORE_EXCLUDED_PRINCIPALS = "b8e3f1c6d2a9"


async def test_excluded_principals_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    decider, _ = await create_agent_with_key(client, admin_key, name="decider")
    task = await create_task(client, admin_key, title="Kept")
    created = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": decider["id"]},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text

    alembic_command.downgrade(v08_alembic_config, BEFORE_EXCLUDED_PRINCIPALS)
    columns = {c["name"] for c in inspect(sync_engine).get_columns("approvals")}
    assert "excluded_principals" not in columns
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    # An approval from before the revision excludes nobody.
    read = await client.get(f"/api/v1/approvals/{created.json()['id']}", headers=auth(admin_key))
    assert read.status_code == 200, read.text
    assert read.json()["excludedPrincipals"] == []


# The recall queue and the step context profile (CP-ADR-0076 §4, §6) and the
# revision right before them.
BEFORE_PROCESS_RECALLS = "e3b7c1d9a4f2"


async def test_process_recalls_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Kept")

    alembic_command.downgrade(v08_alembic_config, BEFORE_PROCESS_RECALLS)
    assert "process_recalls" not in inspect(sync_engine).get_table_names()
    columns = {c["name"] for c in inspect(sync_engine).get_columns("tasks")}
    assert "context_profile" not in columns
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    # A task from before the revision keeps the profile of its type.
    with sync_engine.connect() as connection:
        profile = connection.execute(
            text("SELECT context_profile FROM tasks WHERE id = :id"), {"id": task["id"]}
        ).scalar_one()
    assert profile is None


# Engine revision, step attempts and SLA deadlines (CP-ADR-0074 amendment
# 2026-09-29, CP-ADR-0078) and the revision right before them.
BEFORE_PROCESS_OBSERVABILITY = "e6b3d8f1a2c9"


async def test_process_observability_revision_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    admin = (await do_bootstrap(client))["adminPrincipal"]["id"]
    alembic_command.downgrade(v08_alembic_config, BEFORE_PROCESS_OBSERVABILITY)
    inspector = inspect(sync_engine)
    assert "engine_revision" not in {
        c["name"] for c in inspector.get_columns("process_definitions")
    }
    assert "remaining_unit" not in {c["name"] for c in inspector.get_columns("process_timers")}
    instance_columns = {c["name"] for c in inspector.get_columns("process_instances")}
    assert not {"step_attempts", "sla_due_at", "sla_warn_at"} & instance_columns
    assert "ix_process_instances_sla_due" not in {
        i["name"] for i in inspector.get_indexes("process_instances")
    }
    # A version, an instance and a frozen timer written before the revision.
    with sync_engine.begin() as connection:
        ids = connection.execute(
            text(
                "WITH d AS ("
                " INSERT INTO process_definitions (id, tenant_id, key, version, display_name,"
                "  definition_hash, expression_profile, spec, governed_by, warnings,"
                "  created_by, created_at)"
                " SELECT gen_random_uuid(), tenant_id, 'kept', 1, 'Kept', 'sha256:0', 'cel',"
                "  '{}', '[]', '[]', id, now() FROM principals WHERE id = :admin"
                " RETURNING id, tenant_id),"
                " i AS ("
                " INSERT INTO process_instances (id, tenant_id, definition_id, definition_key,"
                "  definition_version, instance_key, status, data, state, refs, started_at,"
                "  updated_at)"
                " SELECT gen_random_uuid(), tenant_id, id, 'kept', 1, 'K-1', 'running',"
                "  '{}', '{}', '{}', now(), now() FROM d"
                " RETURNING id, tenant_id, definition_id)"
                " INSERT INTO process_timers (id, tenant_id, instance_id, element, timer_kind,"
                "  state, remaining_seconds, reads, provisional, created_at, updated_at)"
                " SELECT gen_random_uuid(), tenant_id, id, 'hold', 'timeout', 'frozen', 60,"
                "  '[]', false, now(), now() FROM i"
                " RETURNING instance_id, (SELECT definition_id FROM i)"
            ),
            {"admin": admin},
        ).one()
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    # Existing rows read the defaults: the old semantics, wall time, no attempts
    # and no deadline.
    with sync_engine.connect() as connection:
        kept = connection.execute(
            text(
                "SELECT d.engine_revision, t.remaining_unit, t.remaining_seconds,"
                " i.step_attempts, i.sla_due_at, i.sla_warn_at"
                " FROM process_instances i"
                " JOIN process_definitions d ON d.id = i.definition_id"
                " JOIN process_timers t ON t.instance_id = i.id"
                " WHERE i.id = :instance AND d.id = :definition"
            ),
            {"instance": ids[0], "definition": ids[1]},
        ).one()
    assert tuple(kept) == (1, "wall", 60, {}, None, None)

    inspector = inspect(sync_engine)
    columns = {
        (table, c["name"]): c
        for table in ("process_definitions", "process_timers", "process_instances")
        for c in inspector.get_columns(table)
    }
    assert not columns["process_definitions", "engine_revision"]["nullable"]
    assert columns["process_definitions", "engine_revision"]["default"] == "1"
    assert columns["process_timers", "remaining_unit"]["default"] == "'wall'::text"
    assert columns["process_instances", "step_attempts"]["default"] == "'{}'::jsonb"
    assert columns["process_instances", "sla_due_at"]["nullable"]
    assert columns["process_instances", "sla_warn_at"]["nullable"]
    (index,) = [
        i
        for i in inspector.get_indexes("process_instances")
        if i["name"] == "ix_process_instances_sla_due"
    ]
    assert index["column_names"] == ["tenant_id", "sla_due_at"]
    # Over the rows with a running deadline, whatever the status (CP-ADR-0078 §6).
    assert index["dialect_options"]["postgresql_where"] == "(sla_due_at IS NOT NULL)"


# The source of an agent revision (CP-ADR-0073, amendment 2026-09-29) and the
# revision right before it.
BEFORE_AGENT_REVISION_SOURCE = "e3b7d1a9c5f2"


async def test_agent_revision_source_is_additive_and_reversible(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    """Revisions from before the step read as unknown: the immutable rows are not rewritten."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = {
        "displayName": "Bridge",
        "identity": {"kind": "service", "permissions": ["tasks.read"]},
        "placement": "none",
    }
    published = await client.post(
        "/api/v1/agents", json={"key": "bridge", "spec": spec}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text

    alembic_command.downgrade(v08_alembic_config, BEFORE_AGENT_REVISION_SOURCE)
    columns = {c["name"] for c in inspect(sync_engine).get_columns("agent_revisions")}
    assert not {"source_kind", "source_package_key", "source_package_version"} & columns
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)

    history = await client.get("/api/v1/agents/bridge/revisions", headers=auth(admin_key))
    assert history.status_code == 200, history.text
    assert [i["source"] for i in history.json()["items"]] == [{"kind": "unknown", "package": None}]


async def test_head_matches_code(sync_engine: Engine) -> None:
    with sync_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == V08_HEAD


# The attempt start of a skill invocation (ADR-0056 amendment) and the revision
# right before it.
ATTEMPT_STARTED = "c7d3a1f9e2b6"
BEFORE_ATTEMPT_STARTED = "b5e1d9c3a7f2"


async def test_running_invocations_gain_an_attempt_start_on_upgrade(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await published(client, admin_key)
    created = (await invoke(client, admin_key, "repo.search", {"query": "x"})).json()
    lease = (await claim(client, admin_key)).json()["invocation"]

    alembic_command.downgrade(v08_alembic_config, BEFORE_ATTEMPT_STARTED)
    alembic_command.upgrade(v08_alembic_config, ATTEMPT_STARTED)
    with sync_engine.connect() as connection:
        started, attempt_started = connection.execute(
            text("SELECT started_at, attempt_started_at FROM skill_invocations")
        ).one()
    assert started is not None
    assert attempt_started == started

    # A row the backfill missed still gets a bounded lease instead of a 500.
    # The API reads the schema of the code: back to the head first.
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)
    with sync_engine.begin() as connection:
        connection.execute(text("UPDATE skill_invocations SET attempt_started_at = NULL"))
    # The API reads the current schema on every request (the visibility of a
    # binding, CP-ADR-0082): it is called on the head, the row stays as left.
    alembic_command.upgrade(v08_alembic_config, V08_HEAD)
    beat = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:heartbeat",
        json={"fencingToken": lease["fencingToken"], "leaseSeconds": 3600},
        headers=auth(admin_key),
    )
    assert beat.status_code == 200, beat.text


# task_types.execution (ADR-0056 §3) and the revision right before it.
TASK_TYPE_EXECUTION = "e3a9c5d7f1b4"


async def test_task_type_execution_roundtrip(
    client: httpx.AsyncClient, sync_engine: Engine, v08_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await create_task(client, admin_key, title="Before")

    alembic_command.downgrade(v08_alembic_config, ATTEMPT_STARTED)
    with sync_engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'task_types' AND column_name = 'execution'"
            )
        ).all()
    assert columns == []
    alembic_command.upgrade(v08_alembic_config, TASK_TYPE_EXECUTION)

    with sync_engine.connect() as connection:
        executions = connection.execute(text("SELECT execution FROM task_types")).scalars().all()
    assert executions and all(value is None for value in executions)
    # The replaced trigger function still freezes the version — execution included.
    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as connection:
        connection.execute(text("""UPDATE task_types SET execution = '{"skill": "x"}'"""))
    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as connection:
        connection.execute(text("UPDATE task_types SET display_name = 'changed'"))
