"""SQLAlchemy ORM models — the PostgreSQL source of truth.

Models are plain data carriers: no relationship() attributes, no lazy loading.
Critical invariants live in the database itself (CHECK constraints, partial
unique indexes, append-only trigger on events — see the Alembic migration).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# The origin of a row nobody described: the pre-M1.1 meaning of every task
# (CP-ADR-0062). Mirrors the server default of the migration.
_HUMAN_ORIGIN: dict[str, Any] = {"kind": "human", "evidence": []}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy class-level configuration
        uuid.UUID: UUID(as_uuid=True),
        datetime: TIMESTAMP(timezone=True),
        dict[str, Any]: JSONB(),
        list[str]: JSONB(),
    }


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint("kind IN ('human', 'agent', 'service')", name="kind"),
        CheckConstraint("status IN ('active', 'paused', 'disabled')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("jsonb_typeof(profile) = 'object'", name="profile_object"),
        Index("ix_principals_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    # Display details in the organization, edited by ``PATCH /principals/{id}``
    # (CP-ADR-0082 §1); ``version`` grows on every change of the name or profile.
    profile: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    key_prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    permissions: Mapped[list[str]] = mapped_column(default=list)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class IamPrincipalBinding(Base):
    """External IAM identity -> local Principal and its permissions.

    An IAM token carries no Control Plane permissions and must not, so what this
    identity may do here is decided in this table. The pair
    ``(issuer, iam_principal_id)`` is unique: one upstream identity never gets
    two local Principals.

    ``status``/``revoked_at`` are the local revocation policy: they close entry
    at once instead of waiting out an already issued access token. ``disabled``
    is an operator switch, ``revoked`` a deliberate cut-off through the API;
    the enforcement path treats both alike, and an upsert of the same identity
    reopens the row (ADR-0053).
    """

    __tablename__ = "iam_principal_bindings"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'revoked')", name="status"),
        CheckConstraint("visibility IN ('tenant', 'members')", name="visibility"),
        Index("uq_iam_bindings_identity", "issuer", "iam_principal_id", unique=True),
        Index("ix_iam_bindings_tenant_principal", "tenant_id", "principal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    issuer: Mapped[str] = mapped_column(Text)
    iam_tenant_id: Mapped[uuid.UUID]
    iam_principal_id: Mapped[uuid.UUID]
    permissions: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(Text, default="active")
    # ``tenant`` — permissions act on the whole tenant; ``members`` — a human
    # sees only the workspaces of their membership and below (CP-ADR-0082 §2).
    visibility: Mapped[str] = mapped_column(Text, default="tenant", server_default=text("'tenant'"))
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Delegation(Base):
    __tablename__ = "delegations"
    __table_args__ = (Index("ix_delegations_tenant_agent", "tenant_id", "agent_principal_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    human_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    agent_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    permissions: Mapped[list[str]] = mapped_column(default=list)
    starts_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class Session(Base):
    """Work session lease.

    v0.3: a session doubles as the live harness registration. The harness
    (Claude Code, CLI, agent daemon, ...) has no lifecycle of its own beyond
    the session that carries it, so its identity/protocol data are session
    columns, not a separate aggregate (ADR-0015).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'stale', 'closed')", name="status"),
        CheckConstraint(
            "control_level IN ('managed', 'connected', 'human_operated')",
            name="control_level",
        ),
        Index("ix_sessions_tenant_status", "tenant_id", "status"),
        Index(
            "ix_sessions_active_expires",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_sessions_tenant_created", "tenant_id", "started_at", "id"),
        Index(
            "ix_sessions_principal_active",
            "principal_id",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    on_behalf_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    delegation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delegations.id"))
    status: Mapped[str] = mapped_column(Text, default="active")
    client_name: Mapped[str] = mapped_column(Text)
    client_version: Mapped[str] = mapped_column(Text, default="")
    # Observability only. Derived by the server from authenticated Principal
    # kind and deliberately absent from authorization checks.
    control_level: Mapped[str] = mapped_column(Text, default="connected")
    # v0.3 harness registration (all nullable: pre-v0.3 clients send none of it).
    harness_type: Mapped[str | None] = mapped_column(Text)
    harness_version: Mapped[str | None] = mapped_column(Text)
    protocol_version: Mapped[str | None] = mapped_column(Text)
    harness_capabilities: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    hostname: Mapped[str | None] = mapped_column(Text)
    environment: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    started_at: Mapped[datetime]
    heartbeat_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]


class TaskType(Base):
    """One immutable version of a work item type (ADR-0048).

    Same shape and same guarantee as ``project_templates``: ``(tenant_id, key,
    version)`` is unique and the row never changes after INSERT — a database
    trigger rejects every UPDATE except ``status: active -> deprecated``. That
    is what makes a task's reference to a type version safe: editing a type
    means creating the next version, and live tasks keep the semantics they
    were created under.
    """

    __tablename__ = "task_types"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deprecated')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_task_types_key_version"),
        UniqueConstraint("tenant_id", "id", name="uq_task_types_tenant_id_id"),
        Index("ix_task_types_tenant_key", "tenant_id", "key", "version"),
        Index("ix_task_types_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON Schema for task custom_fields; declared here, applied from WI-3 on.
    field_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # {"initialStatus", "statuses", "transitions", "claimStatus",
    #  "releaseStatus", "completionStatus"}
    lifecycle_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # ADR-0056 §3: {"skill", "version", "inputs"} when a task of this type is
    # executed by one skill invocation; NULL for ordinary work.
    execution: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # What a decided approval on a task of this version sets in motion
    # (CP-ADR-0061): {"gates": {<gate>: {"outcomes": {approved|rejected:
    # [action...]}}}}. Part of the immutable version like the lifecycle; an
    # empty document means "no outcomes" — the behaviour before CP-ADR-0061.
    approval_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Where a task of this version takes its knowledge context from
    # (CP-ADR-0064): {"anchors", "traverse", "asOf", "budgetTokens"}. Part of
    # the immutable version; empty means "no profile".
    context_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # How to execute a task of this version (CP-ADR-0066): Markdown up to
    # 16 KiB, delivered to the executor as the task-type layer of its
    # instructions. Part of the immutable version; empty means "none".
    instructions: Mapped[str] = mapped_column(Text, default="")
    # What core files once a task of this version is completed (CP-ADR-0061,
    # amendment 2026-09-25): {"onComplete": {"when": [...], "actions": [...]}}.
    # Part of the immutable version; empty means "nothing after completion".
    completion_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # What artifacts a task of this version takes in and hands on
    # (CP-ADR-0072 §7): {"inputs": [...], "outputs": [...]}. Part of the
    # immutable version; empty means "no inputs, no outputs".
    artifact_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Checks every task of this version passes before it is done, after the
    # required outputs and before the task's own acceptance (CP-ADR-0067,
    # amendment 2026-09-27): [{key, kind, description, spec?, when?}]. Part
    # of the immutable version; empty means "none", the behaviour before.
    acceptance: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Slugs of the tenant's roles a person needs to take work of this version
    # (CP-ADR-0048, amendment 2026-10-03 A1). Part of the immutable version;
    # empty means "people are not restricted", the behaviour before.
    executor_roles: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Goal(Base):
    """A desired state the tenant wants to be true (CP-ADR-0062).

    Product-neutral: a title, prose for the desired state and a list of
    criteria in the same shape as a task's acceptance checks. Work items point
    at a goal (``tasks.goal_id``); a goal may refine a parent goal. The
    ``created_from`` document says why the goal exists, in the same shape as a
    task's ``origin``, and is never rewritten.
    """

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'achieved', 'abandoned')", name="status"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 500", name="title_length"),
        CheckConstraint("version >= 1", name="version_positive"),
        # "Is this goal still open?" is answerable from one column and cannot
        # drift from the status: closed_at is set exactly when it is closed.
        CheckConstraint("(status = 'active') = (closed_at IS NULL)", name="closed_at_matches"),
        CheckConstraint("parent_goal_id IS DISTINCT FROM id", name="not_own_parent"),
        CheckConstraint("jsonb_typeof(criteria) = 'array'", name="criteria_is_array"),
        CheckConstraint(
            "jsonb_typeof(created_from) = 'object' AND created_from ? 'kind'",
            name="created_from_has_kind",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_goals_tenant_id_id"),
        # A parent goal of another tenant is impossible, not merely unchecked.
        ForeignKeyConstraint(
            ["tenant_id", "parent_goal_id"],
            ["goals.tenant_id", "goals.id"],
            name="fk_goals_parent",
        ),
        Index("ix_goals_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_goals_tenant_status", "tenant_id", "status"),
        Index("ix_goals_parent", "tenant_id", "parent_goal_id"),
        Index("ix_goals_workspace", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    title: Mapped[str] = mapped_column(Text)
    desired_state: Mapped[str] = mapped_column(Text, default="")
    # [{key, kind, description, spec?}] - see domain/work_graph.py.
    criteria: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    status: Mapped[str] = mapped_column(Text, default="active")
    # {kind, ref?, ruleId?, evidence[]} - immutable after INSERT.
    created_from: Mapped[dict[str, Any]] = mapped_column(default=lambda: dict(_HUMAN_ORIGIN))
    parent_goal_id: Mapped[uuid.UUID | None]
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # The status VOCABULARY is no longer known to the database (ADR-0048):
        # it lives in the task type's lifecycle_schema and is enforced by the
        # application under the task's row lock, exactly as for a project
        # profile. What the database still guarantees is the SYSTEM CATEGORY,
        # which is the only thing core branches on.
        CheckConstraint("char_length(status) BETWEEN 1 AND 64", name="status"),
        CheckConstraint(
            "system_status_category IN ('backlog', 'active', 'blocked', "
            "'terminal_success', 'terminal_cancelled')",
            name="system_status_category",
        ),
        CheckConstraint("priority IN ('critical', 'high', 'medium', 'low')", name="priority"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("claim_epoch >= 0", name="claim_epoch_nonnegative"),
        # A planned interval that runs backwards is not a validation nicety:
        # every timeline and every date filter would have to special-case it.
        CheckConstraint(
            "start_date IS NULL OR due_date IS NULL OR start_date <= due_date",
            name="planned_dates_ordered",
        ),
        UniqueConstraint("tenant_id", "public_id", name="uq_tasks_tenant_public_id"),
        # Composite key target for tenant-consistent FKs (e.g. task_relations).
        UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "type_id"],
            ["task_types.tenant_id", "task_types.id"],
            name="fk_tasks_type",
        ),
        # M1.1 (CP-ADR-0062): the goal a work item serves, same tenant only.
        ForeignKeyConstraint(
            ["tenant_id", "goal_id"],
            ["goals.tenant_id", "goals.id"],
            name="fk_tasks_goal",
        ),
        CheckConstraint(
            "jsonb_typeof(origin) = 'object' AND origin ? 'kind'", name="origin_has_kind"
        ),
        CheckConstraint("jsonb_typeof(acceptance) = 'array'", name="acceptance_is_array"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
        Index("ix_tasks_tenant_status", "tenant_id", "status"),
        # Discovery filters by category on every claim-candidate query.
        Index("ix_tasks_tenant_category", "tenant_id", "system_status_category"),
        Index("ix_tasks_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_tasks_workspace", "workspace_id"),
        Index("ix_tasks_type", "type_id"),
        # GET /goals/{id}/work pages a goal's work newest first.
        Index(
            "ix_tasks_tenant_goal",
            "tenant_id",
            "goal_id",
            "created_at",
            "id",
            postgresql_where=text("goal_id IS NOT NULL"),
        ),
        # v0.8 (ADR-0049): owner and date filters. The date indexes are partial
        # — most work items never get a planned date, and a full index would be
        # mostly NULLs that no query can use.
        Index("ix_tasks_tenant_owner", "tenant_id", "owner_id"),
        Index(
            "ix_tasks_tenant_due",
            "tenant_id",
            "due_date",
            "id",
            postgresql_where=text("due_date IS NOT NULL"),
        ),
        Index(
            "ix_tasks_tenant_start",
            "tenant_id",
            "start_date",
            "id",
            postgresql_where=text("start_date IS NOT NULL"),
        ),
        # GET /tasks?q= (CP-ADR-0049, amendment TASK-000866): substring search
        # through ILIKE, one pg_trgm GIN index per searched column.
        *(
            Index(
                f"ix_tasks_{column}_trgm",
                column,
                postgresql_using="gin",
                postgresql_ops={column: "gin_trgm_ops"},
            )
            for column in ("title", "description", "public_id")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    public_id: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    type_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    # User-facing lifecycle key; core only ever branches on the category.
    status: Mapped[str] = mapped_column(Text, default="todo")
    system_status_category: Mapped[str] = mapped_column(Text, default="active")
    priority: Mapped[str] = mapped_column(Text, default="medium")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    # v0.8 (ADR-0049): extensible fields validated against the type's
    # field_schema, and planned dates as typed columns rather than fields
    # inside the document — they need indexes, ordering and a timeline.
    custom_fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
    start_date: Mapped[datetime | None]
    due_date: Mapped[datetime | None]
    # M1.1 work graph (CP-ADR-0062): which goal the item serves, why it
    # exists (immutable), what accepts it and the facts gathered so far.
    goal_id: Mapped[uuid.UUID | None]
    origin: Mapped[dict[str, Any]] = mapped_column(default=lambda: dict(_HUMAN_ORIGIN))
    acceptance: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # The ``context`` of the process step this task is (CP-ADR-0076 §6), its
    # anchors computed: it replaces the profile of the type. NULL — the type's.
    context_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    claim_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    active_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_claims.id", use_alter=True, name="fk_tasks_active_claim")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class TaskClaim(Base):
    __tablename__ = "task_claims"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'released', 'stale')", name="status"),
        # DB-level invariant: at most one active claim per task.
        Index(
            "uq_task_claims_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        UniqueConstraint("task_id", "fencing_token", name="uq_task_claims_task_fencing"),
        Index("ix_task_claims_tenant_created", "tenant_id", "acquired_at", "id"),
        Index("ix_task_claims_session", "session_id"),
        Index(
            "ix_task_claims_active_expires",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    holder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    status: Mapped[str] = mapped_column(Text, default="active")
    fencing_token: Mapped[int] = mapped_column(BigInteger)
    intent: Mapped[str] = mapped_column(Text, default="")
    acquired_at: Mapped[datetime]
    heartbeat_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    released_at: Mapped[datetime | None]
    release_reason: Mapped[str | None] = mapped_column(Text)


class TaskContextPack(Base):
    """What the Context Compiler was asked for a claim, and what it used (CP-ADR-0064).

    A reference, not a copy: the typed request as sent — anchors, traverse and
    the pinned moment — plus the entities, facts and snapshot ids of the
    answer. Sending the same request again reproduces the pack; the task's
    evidence points here. One row per claim; append-only (database trigger).
    """

    __tablename__ = "task_context_packs"
    __table_args__ = (
        CheckConstraint("as_of_mode IN ('taskCreated', 'now', 'origin')", name="as_of_mode"),
        UniqueConstraint("claim_id", name="uq_task_context_packs_claim"),
        Index("ix_task_context_packs_task", "tenant_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_claims.id"))
    task_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_types.id"))
    compiled_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    as_of: Mapped[datetime]
    as_of_mode: Mapped[str] = mapped_column(Text)
    namespaces: Mapped[list[str]] = mapped_column(JSONB)
    # Memory's typed request without the caller's visibility fields.
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # [{kind, value, source, via?}] — where each anchor came from.
    candidates: Mapped[list[Any]] = mapped_column(JSONB)
    # {"entities": [{namespace, natural_key}], "facts": [id], "snapshots": [...]}
    used: Mapped[dict[str, Any]] = mapped_column(JSONB)
    unresolved: Mapped[list[Any]] = mapped_column(JSONB)
    budget_tokens: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class TaskCounter(Base):
    """Per-tenant monotonically increasing counter behind ``tasks.public_id``."""

    __tablename__ = "task_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, default=0)


class Event(Base):
    """Append-only domain event journal.

    UPDATE/DELETE are rejected by a database trigger (see migration 0001).
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("id", name="uq_events_id"),
        Index("ix_events_tenant_sequence", "tenant_id", "sequence"),
        Index("ix_events_entity", "tenant_id", "entity_type", "entity_id"),
        # v0.4 replay order (per-tenant) and the adapter's global scan.
        Index("ix_events_tenant_tx_sequence", "tenant_id", "tx_id", "sequence"),
        Index("ix_events_tx_sequence", "tx_id", "sequence"),
        # Narrowing filters of the journal page (CP-ADR-0068 amendment B, Б4):
        # an author or one workspace in replay order, a period by time.
        Index("ix_events_tenant_actor_tx_sequence", "tenant_id", "actor_id", "tx_id", "sequence"),
        Index(
            "ix_events_tenant_workspace_tx_sequence",
            "tenant_id",
            "workspace_id",
            "tx_id",
            "sequence",
        ),
        Index("ix_events_tenant_occurred_at", "tenant_id", "occurred_at"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # Writer's 64-bit transaction id, filled by the database. Readers only hand
    # out events whose transaction is below the oldest still-running xid
    # (pg_snapshot_xmin), so a sequence gap can never be skipped by a cursor:
    # sequence values are assigned at INSERT time, not commit time.
    tx_id: Mapped[int] = mapped_column(
        BigInteger, server_default=text("pg_current_xact_id()::text::bigint")
    )
    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    event_type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID]
    actor_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    correlation_id: Mapped[str] = mapped_column(Text)
    causation_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text)
    # v0.5 distributed trace id (X-Run-Id header); NOT the execution Run entity
    # (ADR-0039). Nullable: pre-v0.5 events carry none.
    trace_run_id: Mapped[str | None] = mapped_column(Text)
    # IAM identity of the actor (CP-ADR-0055): the subject policy-service knows.
    # Nullable: legacy API keys and pre-P2 events carry none.
    iam_actor_id: Mapped[uuid.UUID | None]
    # Workspace of the event's entity, resolved by the writer (CP-ADR-0068);
    # NULL for tenant-level events and for events written before it.
    workspace_id: Mapped[uuid.UUID | None]
    # Version of the payload schema in domain/event_catalog.py (CP-ADR-0068).
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    occurred_at: Mapped[datetime]


class EventArchive(Base):
    """Cold storage for journal events moved out of ``events`` (v0.5).

    Same shape as :class:`Event` plus ``archived_at``. Replay from a cursor
    below the journal floor reads this table first and continues in the hot
    table, so archiving never breaks audit or replay (ADR-0038).
    """

    __tablename__ = "event_archive"
    __table_args__ = (
        UniqueConstraint("id", name="uq_event_archive_id"),
        Index("ix_event_archive_tenant_tx_sequence", "tenant_id", "tx_id", "sequence"),
        Index("ix_event_archive_entity", "tenant_id", "entity_type", "entity_id"),
        Index(
            "ix_event_archive_tenant_actor_tx_sequence",
            "tenant_id",
            "actor_id",
            "tx_id",
            "sequence",
        ),
        Index(
            "ix_event_archive_tenant_workspace_tx_sequence",
            "tenant_id",
            "workspace_id",
            "tx_id",
            "sequence",
        ),
        Index("ix_event_archive_tenant_occurred_at", "tenant_id", "occurred_at"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tx_id: Mapped[int] = mapped_column(BigInteger)
    id: Mapped[uuid.UUID]
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    event_type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID]
    actor_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    correlation_id: Mapped[str] = mapped_column(Text)
    causation_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text)
    trace_run_id: Mapped[str | None] = mapped_column(Text)
    iam_actor_id: Mapped[uuid.UUID | None]
    workspace_id: Mapped[uuid.UUID | None]
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    occurred_at: Mapped[datetime]
    archived_at: Mapped[datetime]


class EventJournalFloor(Base):
    """What the journal can still serve, PER TENANT (v0.5).

    ``journal_*`` is the highest position moved into ``event_archive`` (the
    hot table holds everything strictly above it); ``archive_*`` is the
    highest position physically deleted. A replay cursor below ``archive_*``
    is unservable and gets a machine-readable error, never a silent skip.

    Keyed by tenant because retention is a tenant-owned operation: one
    tenant's operator must never be able to prune another tenant's history.
    """

    __tablename__ = "event_journal_floor"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    journal_tx_id: Mapped[int] = mapped_column(BigInteger, default=0)
    journal_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    archive_tx_id: Mapped[int] = mapped_column(BigInteger, default=0)
    archive_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class EventConsumerCursor(Base):
    """Durable replay position of a background journal consumer, per tenant.

    The (tx_id, sequence) pair of the last event whose downstream delivery
    was confirmed; advanced only after confirmation (at-least-once). v0.5
    makes the key ``(name, tenant_id)`` so a poison event in one tenant parks
    only that tenant's row (ADR-0036).
    """

    __tablename__ = "event_consumer_cursors"
    __table_args__ = (
        CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
        Index("ix_event_consumer_cursors_name", "name", "updated_at"),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    tx_id: Mapped[int] = mapped_column(BigInteger, default=0)
    sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # Parked state: set on a permanent provider rejection, cleared by the
    # operator redrive action. The cursor itself never moves past a poison
    # event — there is no API that can skip one (ADR-0037).
    parked_at: Mapped[datetime | None]
    parked_reason: Mapped[str | None] = mapped_column(Text)
    parked_event_id: Mapped[uuid.UUID | None]
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)


class OutboxRecord(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "ix_outbox_undelivered",
            "available_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    topic: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    available_at: Mapped[datetime]
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_at: Mapped[datetime | None]
    locked_by: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_expires", "expires_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    request_method: Mapped[str] = mapped_column(Text)
    request_path: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class ObservationDedupKey(Base):
    """Recognises a repeat of an external observation (CP-ADR-0057).

    The observation itself stays a journal event (ADR-0027); this row only
    remembers which observation a ``(source, dedup_key)`` pair of one author
    produced, so a repeated report answers with the existing id instead of a
    new event. The primary key IS the tenant-scoped uniqueness guarantee; the
    author is part of it, so nobody can take another author's key in advance
    (CP-ADR-0057, amendment 2026-10-01).
    """

    __tablename__ = "observation_dedup_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    observation_id: Mapped[uuid.UUID]
    event_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime]


# --- v0.2 Organization Model --------------------------------------------------


class Workspace(Base):
    """Hierarchical organizational scope (adjacency list via parent_id)."""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        CheckConstraint("id != parent_id", name="not_own_parent"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "task_types IS NULL OR jsonb_typeof(task_types) = 'array'", name="task_types_array"
        ),
        # slug is unique among siblings; NULL parent (root level) needs its own
        # partial index because NULLs never collide in a plain UNIQUE.
        Index(
            "uq_workspaces_root_slug",
            "tenant_id",
            "slug",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
        Index(
            "uq_workspaces_sibling_slug",
            "tenant_id",
            "parent_id",
            "slug",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
        UniqueConstraint("tenant_id", "id", name="uq_workspaces_tenant_id_id"),
        Index("ix_workspaces_tenant_parent", "tenant_id", "parent_id"),
        Index("ix_workspaces_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    # v0.5: typed node. Backfilled to the tenant's system default type, so a
    # v0.4 tree keeps working without any manual repair (ADR-0029).
    type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_types.id"))
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    custom_fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Keys of the task types work here may have (CP-ADR-0008, amendment
    # 2026-10-03 A1). NULL inherits from the nearest ancestor that sets it
    # (every type when none does); [] allows none.
    task_types: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class WorkspaceMember(Base):
    """Organizational membership metadata (not an eligibility gate in v0.2)."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "principal_id", name="uq_workspace_members_pair"),
        Index("ix_workspace_members_principal", "principal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class Role(Base):
    """Organizational function (NOT an API authorization permission)."""

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        Index(
            "uq_roles_global_slug",
            "tenant_id",
            "slug",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index(
            "uq_roles_workspace_slug",
            "tenant_id",
            "workspace_id",
            "slug",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index("ix_roles_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PrincipalRole(Base):
    """Role assignment; workspace_id scopes it to a subtree (NULL = tenant-wide)."""

    __tablename__ = "principal_roles"
    __table_args__ = (
        Index(
            "uq_principal_roles_global",
            "principal_id",
            "role_id",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index(
            "uq_principal_roles_scoped",
            "principal_id",
            "role_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index("ix_principal_roles_role", "role_id"),
        Index("ix_principal_roles_principal", "principal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    created_at: Mapped[datetime]


class Capability(Base):
    """Declarative "what can this principal potentially do" metadata."""

    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_capabilities_tenant_name"),
        Index("ix_capabilities_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]


class PrincipalCapability(Base):
    __tablename__ = "principal_capabilities"
    __table_args__ = (
        UniqueConstraint("principal_id", "capability_id", name="uq_principal_capabilities_pair"),
        Index("ix_principal_capabilities_capability", "capability_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    capability_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capabilities.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime]


class Skill(Base):
    """Registry entry for an executable interface/tool.

    A version is immutable once published (ADR-0056 §1, trigger
    ``skills_immutable``): only ``description`` and a forward ``status`` move
    (active -> deprecated -> disabled) are allowed afterwards.

    ``version`` is the skill's own registry version string; the optimistic
    concurrency counter is ``row_version`` (ETag ``"skill-<row_version>"``).
    """

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deprecated', 'disabled')", name="status"),
        CheckConstraint(
            "protocol IN ('mcp', 'http', 'local', 'opencode', 'custom')",
            name="protocol",
        ),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "side_effects IS NULL OR side_effects IN ('none', 'external_read', 'external_write')",
            name="side_effects",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')", name="risk_level"
        ),
        CheckConstraint(
            "contract IS NULL OR (side_effects IS NOT NULL AND risk_level IS NOT NULL)",
            name="contract_declares_policy",
        ),
        CheckConstraint(
            "contract IS NULL OR ("
            "contract->'implementation'->>'protocol' IN ('http', 'local', 'mcp') "
            "AND protocol = contract->'implementation'->>'protocol')",
            name="contract_protocol",
        ),
        UniqueConstraint("tenant_id", "name", "version", name="uq_skills_tenant_name_version"),
        Index("ix_skills_tenant_created", "tenant_id", "created_at", "id"),
        # The discovery view's only access path: tenant-scoped, disabled
        # versions skipped, ordered by the pagination key (HRS-3).
        Index("ix_skills_tenant_status_name", "tenant_id", "status", "name", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, default="1.0.0")
    description: Mapped[str] = mapped_column(Text, default="")
    protocol: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # ADR-0056 §1: contract v1. NULL for a catalog-only row (every skill
    # registered before M2.1): such a row describes, the core cannot invoke it.
    side_effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none_as_null: "no contract" must be SQL NULL — the CHECKs test IS NULL.
    contract: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="active")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class SkillInvocation(Base):
    """One call of a skill version (ADR-0056 §2) — durable, leased, fenced.

    The server never executes a skill: it validates, stores and hands the row
    to an executor under a lease. ``fencing_token`` grows with every claim, so
    a zombie executor holding an old lease cannot complete a newer attempt.
    """

    __tablename__ = "skill_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "requested_by_kind IN ('principal', 'rule', 'approval', 'verification', 'run')",
            name="requested_by_kind",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND attempt >= 0 AND attempt <= max_attempts",
            name="attempts",
        ),
        CheckConstraint(
            "status <> 'running' OR (lease_expires_at IS NOT NULL "
            "AND executor_principal_id IS NOT NULL)",
            name="running_has_lease",
        ),
        UniqueConstraint(
            "skill_id", "idempotency_key", name="uq_skill_invocations_skill_idempotency"
        ),
        Index("ix_skill_invocations_tenant_created", "tenant_id", "created_at", "id"),
        Index(
            "ix_skill_invocations_queue",
            "tenant_id",
            "status",
            "available_at",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_skill_invocations_task", "task_id"),
        # An approval is a single-use basis for one skill version (ADR-0056).
        Index(
            "uq_skill_invocations_skill_approval",
            "skill_id",
            text("(authorization_basis ->> 'approvalId')"),
            unique=True,
            postgresql_where=text("authorization_basis ->> 'kind' = 'approval'"),
        ),
        # A run of an execution-typed task makes one skill call (ADR-0056 §3).
        Index(
            "uq_skill_invocations_run_execution",
            "run_id",
            unique=True,
            postgresql_where=text("authorization_basis ->> 'kind' = 'execution'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id"))
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    requested_by_kind: Mapped[str] = mapped_column(Text)
    requested_by_ref: Mapped[str] = mapped_column(Text)
    authority_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    # Why an external_write call was allowed (ADR-0056 §4), e.g.
    # {"kind": "approval", "approvalId": ...}; NULL when no basis was needed.
    authorization_basis: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    # Earliest moment a pending row may be claimed (retry backoff).
    available_at: Mapped[datetime]
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    cost: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    executor_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    executor_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    lease_expires_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    # When the current attempt was claimed; bounds how far heartbeats reach.
    attempt_started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class PrincipalSkill(Base):
    __tablename__ = "principal_skills"
    __table_args__ = (
        UniqueConstraint("principal_id", "skill_id", name="uq_principal_skills_pair"),
        Index("ix_principal_skills_skill", "skill_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime]


class TaskRequirement(Base):
    """One mandatory requirement of a task (ALL requirements must be met)."""

    __tablename__ = "task_requirements"
    __table_args__ = (
        CheckConstraint("kind IN ('role', 'capability', 'skill')", name="kind"),
        # Exactly one reference column, matching `kind`.
        CheckConstraint(
            "(kind = 'role' AND role_id IS NOT NULL "
            " AND capability_id IS NULL AND skill_id IS NULL) OR "
            "(kind = 'capability' AND capability_id IS NOT NULL "
            " AND role_id IS NULL AND skill_id IS NULL) OR "
            "(kind = 'skill' AND skill_id IS NOT NULL "
            " AND role_id IS NULL AND capability_id IS NULL)",
            name="exactly_one_ref",
        ),
        Index(
            "uq_task_requirements_role",
            "task_id",
            "role_id",
            unique=True,
            postgresql_where=text("role_id IS NOT NULL"),
        ),
        Index(
            "uq_task_requirements_capability",
            "task_id",
            "capability_id",
            unique=True,
            postgresql_where=text("capability_id IS NOT NULL"),
        ),
        Index(
            "uq_task_requirements_skill",
            "task_id",
            "skill_id",
            unique=True,
            postgresql_where=text("skill_id IS NOT NULL"),
        ),
        Index("ix_task_requirements_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(Text)
    role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id"))
    capability_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("capabilities.id"))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skills.id"))
    # v0.3: True = the requirement pins the exact skill version ("name@version");
    # False = v0.2 semantics, any non-disabled version of the name satisfies it.
    skill_exact: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]


class TaskRelation(Base):
    """Directed edge between tasks; see TaskRelationType for semantics.

    Composite FKs pin both endpoints to the same tenant at the DB level.
    """

    __tablename__ = "task_relations"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('parent', 'blocks', 'depends_on', 'spawned_by', 'related_to')",
            name="relation_type",
        ),
        CheckConstraint("from_task_id != to_task_id", name="not_self"),
        UniqueConstraint(
            "from_task_id", "to_task_id", "relation_type", name="uq_task_relations_edge"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "from_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_task_relations_from_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "to_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_task_relations_to_task",
        ),
        Index("ix_task_relations_from", "from_task_id"),
        Index("ix_task_relations_to", "to_task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    from_task_id: Mapped[uuid.UUID]
    to_task_id: Mapped[uuid.UUID]
    relation_type: Mapped[str] = mapped_column(Text)
    created_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class Run(Base):
    """One concrete execution attempt under a claim (lease).

    The fencing token captured at start pins the run to its claim epoch: a
    zombie run can never write a final task result after a takeover.
    """

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled', 'suspended')",
            name="status",
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "max_duration_seconds IS NULL OR max_duration_seconds > 0",
            name="max_duration_positive",
        ),
        CheckConstraint("max_actions IS NULL OR max_actions > 0", name="max_actions_positive"),
        # DB-level invariant: at most one running run per task.
        Index(
            "uq_runs_one_running_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        UniqueConstraint("task_id", "attempt", name="uq_runs_task_attempt"),
        # GET /runs: newest first by (started_at, id), for the tenant and for one
        # executor (CP-ADR-0073, amendment of 2026-09-29).
        Index("ix_runs_tenant_started", "tenant_id", text("started_at DESC"), text("id DESC")),
        Index(
            "ix_runs_tenant_principal_started",
            "tenant_id",
            "principal_id",
            text("started_at DESC"),
            text("id DESC"),
        ),
        Index("ix_runs_claim", "claim_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_claims.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    fencing_token: Mapped[int] = mapped_column(BigInteger)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="running")
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # v0.3: cooperative cancellation signal (authoritative stop is :cancel).
    cancel_requested_at: Mapped[datetime | None]
    cancel_requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    # v0.3: optional execution budget; enforced where the server authoritatively
    # can (run action recording), respected by the harness elsewhere.
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    max_actions: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    # The executor instructions the run was started under (CP-ADR-0066): the
    # sha256 of the assembled layers and the version of each layer. NULL for
    # runs started before the column existed.
    instructions_hash: Mapped[str | None] = mapped_column(Text)
    instructions_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The agent revision the run went by (CP-ADR-0073 §7); NULL for executors
    # that are not registered agents.
    agent_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_revisions.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Artifact(Base):
    """Append-oriented work product: a reference (`uri`), small JSON (`content`)
    or bytes in the content store (`content_state`, CP-ADR-0072)."""

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_artifacts_task", "task_id"),
        Index("ix_artifacts_run", "run_id"),
        CheckConstraint(
            "content_state IN ('none', 'stored', 'purged')", name="content_state_valid"
        ),
        # Stored (or once stored) content always knows what it was.
        CheckConstraint(
            "(content_state = 'none') = (sha256 IS NULL)"
            " AND (sha256 IS NULL) = (size_bytes IS NULL)"
            " AND (sha256 IS NULL) = (media_type IS NULL)",
            name="content_fields_consistent",
        ),
        # Is an object still referenced? — asked per (tenant, sha256).
        Index(
            "ix_artifacts_tenant_sha256",
            "tenant_id",
            "sha256",
            postgresql_where=text("sha256 IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    created_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    type: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # v0.3 revision lineage: a new artifact may declare which one it replaces.
    # Rows stay append-only; the chain is the revision history (ADR-0020).
    supersedes_artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    # Content in the store (CP-ADR-0072 §1): none | stored | purged. A purge
    # keeps size, media type and checksum as the trace of what was there.
    content_state: Mapped[str] = mapped_column(Text, default="none", server_default=text("'none'"))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    media_type: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    # Version of the registered artifact type checked at creation (§6).
    type_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]


class ArtifactContent(Base):
    """One upload of artifact bytes (CP-ADR-0072 §2, §4).

    The id is the ``contentRef`` handed to the uploader. A row per upload,
    not per object: the reference is bound to the principal who uploaded it
    and to the media type they declared. The object itself is shared by
    content inside the tenant (``storage_key``).
    """

    __tablename__ = "artifact_contents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        Index("ix_artifact_contents_tenant_sha256", "tenant_id", "sha256"),
        Index("ix_artifact_contents_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    uploaded_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    sha256: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    # First artifact that referenced this upload; NULL = still unclaimed.
    referenced_at: Mapped[datetime | None]


class ArtifactType(Base):
    """One immutable version of an artifact type (CP-ADR-0072 §6).

    Versioned like ``task_types`` (ADR-0048): ``(tenant_id, key, version)`` is
    unique and a database trigger rejects every UPDATE except ``status:
    active -> deprecated``. An artifact of a registered type is checked
    against the latest version of its key and records that version.
    """

    __tablename__ = "artifact_types"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deprecated')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("max_bytes >= 1", name="max_bytes_positive"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_artifact_types_key_version"),
        Index("ix_artifact_types_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON Schema for the artifact's metadata; empty means "any object".
    metadata_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # ["type/subtype" | "type/*" | "*/*"], lower case.
    media_types: Mapped[list[str]] = mapped_column(JSONB)
    max_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ConnectionType(Base):
    """One immutable version of a connection type (CP-ADR-0079 §2).

    Versioned like a skill (ADR-0021): ``(tenant_id, key, version)`` is unique,
    the publisher names the version, and a database trigger rejects every
    UPDATE but a forward ``status`` move (active -> deprecated -> disabled,
    active -> disabled) with its ``row_version``; DELETE is rejected.
    ``spec`` is the catalog object's spec as published, ``spec_hash`` the
    sha256 of its canonical JSON: a second publication of the same pair
    compares the hashes. ``display_name`` repeats ``spec.displayName`` for
    lists.
    """

    __tablename__ = "connection_types"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deprecated', 'disabled')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_connection_types_key_version"),
        Index("ix_connection_types_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="active")
    display_name: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Connection(Base):
    """An account of an external system in the tenant (CP-ADR-0079 §3).

    It belongs to the tenant, not to a person; two accounts of one type are two
    rows with different keys. ``key`` never changes and is never reused: a
    connection is not deleted, a revoked one connects again under its key.
    ``(type_key, type_version)`` is a published version of the tenant's type.
    ``secret_ref`` is the path of the material in the secret store, never the
    material; it is set exactly when ``auth`` is. ``oauth_server`` names the
    OAuth server the creds of an ``oauth2`` connection refresh through (one per
    authorization attempt, CP-ADR-0079 §6); it is a name, not a secret.
    ``version`` backs ``If-Match``
    and moves with what a person or a status transition changes, not with
    ``last_checked_at`` alone.
    """

    __tablename__ = "connections"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'active', 'expired', 'revoked')", name="status"),
        CheckConstraint("auth IS NULL OR auth IN ('oauth2', 'token')", name="auth"),
        CheckConstraint("(secret_ref IS NULL) = (auth IS NULL)", name="secret_ref_with_auth"),
        CheckConstraint("oauth_server IS NULL OR auth = 'oauth2'", name="oauth_server_with_oauth2"),
        CheckConstraint("jsonb_typeof(settings) = 'object'", name="settings_is_object"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("tenant_id", "key", name="uq_connections_tenant_key"),
        ForeignKeyConstraint(
            ["tenant_id", "type_key", "type_version"],
            ["connection_types.tenant_id", "connection_types.key", "connection_types.version"],
            name="fk_connections_type_version",
        ),
        Index("ix_connections_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    type_key: Mapped[str] = mapped_column(Text)
    type_version: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text)
    account: Mapped[str | None] = mapped_column(Text)
    auth: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_message: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    secret_ref: Mapped[str | None] = mapped_column(Text)
    oauth_server: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]
    connected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    connected_at: Mapped[datetime | None]
    last_checked_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(Integer, default=1)


class ConnectionOAuthState(Base):
    """One ``state`` of ``POST /connections/{key}:authorize`` (CP-ADR-0079 §6).

    Only the SHA-256 of the state is kept: the state itself went to the browser
    and is not stored anywhere. ``authority`` is the snapshot of the calling
    credential (``authority_snapshot``) the callback acts with. A state is used
    once: ``consumed_at`` is set by the first callback that names it (``outcome``
    then says how the attempt ended) or by a later ``:authorize`` of the same
    connection (``superseded``).
    """

    __tablename__ = "connection_oauth_states"
    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('consumed', 'authorized', 'failed', 'superseded')",
            name="outcome",
        ),
        CheckConstraint("(consumed_at IS NULL) = (outcome IS NULL)", name="consumed_with_outcome"),
        CheckConstraint("octet_length(state_hash) = 32", name="state_hash_sha256"),
        UniqueConstraint("state_hash", name="uq_connection_oauth_states_state_hash"),
        Index("ix_connection_oauth_states_connection", "connection_id", "consumed_at"),
        Index("ix_connection_oauth_states_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    authority: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)


class Agent(Base):
    """A registered agent: its key, desired state and current revision (CP-ADR-0073).

    The spec itself lives in ``agent_revisions``; this row carries what moves
    without a new revision — ``state``/``replicas`` (§3), the principal the
    core derived for the agent and the IAM identity bound to it (§6), and the
    retirement, after which the key is never reused (§9).
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'retired')", name="status"),
        CheckConstraint("state IN ('running', 'stopped')", name="state"),
        CheckConstraint("replicas >= 0 AND replicas <= 100", name="replicas_range"),
        CheckConstraint("current_revision >= 1", name="current_revision_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(principal_id IS NULL) = (iam_principal_id IS NULL)",
            name="identity_with_principal",
        ),
        UniqueConstraint("tenant_id", "key", name="uq_agents_tenant_key"),
        UniqueConstraint("principal_id", name="uq_agents_principal_id"),
        Index("ix_agents_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    state: Mapped[str] = mapped_column(Text)
    replicas: Mapped[int] = mapped_column(Integer)
    current_revision: Mapped[int] = mapped_column(Integer)
    # Workspace of ``spec.work``: the workspace of the agent's events.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    iam_issuer: Mapped[str | None] = mapped_column(Text)
    iam_tenant_id: Mapped[uuid.UUID | None]
    iam_principal_id: Mapped[uuid.UUID | None]
    retired_at: Mapped[datetime | None]
    retired_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class AgentRevision(Base):
    """One immutable published spec of an agent (CP-ADR-0073 §2).

    A trigger rejects every UPDATE and DELETE. ``spec`` is stored as applied,
    without the desired state (``state``, ``placement.replicas``);
    ``spec_hash`` is ``sha256:<hex>`` of its canonical JSON. ``source_kind``
    names who published it: ``package`` (with the package key and version the
    installer sent), ``manual`` (a publication without a package) or
    ``unknown`` (published before the source was recorded).
    """

    __tablename__ = "agent_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("source_kind IN ('package', 'manual', 'unknown')", name="source_kind"),
        CheckConstraint(
            "(source_kind = 'package') = (source_package_key IS NOT NULL) "
            "AND (source_package_key IS NULL) = (source_package_version IS NULL)",
            name="source_package",
        ),
        UniqueConstraint("agent_id", "revision", name="uq_agent_revisions_agent_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    revision: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text)
    source_package_key: Mapped[str | None] = mapped_column(Text)
    source_package_version: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class AgentSecretName(Base):
    """The name of one of an agent's secrets, who set it and when (CP-ADR-0079 §11).

    Only the name: the value went to the secret store in transit
    (``kv/data/tenants/<t>/agents/<key>/<name>``) and is in no table. The
    worker deletes the rows of a retired agent with their documents.
    """

    __tablename__ = "agent_secret_names"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="name_format"),
        Index("ix_agent_secret_names_tenant", "tenant_id"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    updated_at: Mapped[datetime]


class AgentObservedStatus(Base):
    """What actually runs, as the placement service last reported it (CP-ADR-0073 §4)."""

    __tablename__ = "agent_status"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('pending', 'running', 'waiting_for_node', 'crash_looping', "
            "'node_unavailable', 'stopped')",
            name="phase",
        ),
        CheckConstraint(
            "instances_desired >= 0 AND instances_ready >= 0", name="instances_non_negative"
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    phase: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    reason_message: Mapped[str | None] = mapped_column(Text)
    observed_revision: Mapped[int | None] = mapped_column(Integer)
    node: Mapped[str | None] = mapped_column(Text)
    instances_desired: Mapped[int] = mapped_column(Integer)
    instances_ready: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime]
    reported_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    updated_at: Mapped[datetime]


class TaskComment(Base):
    """One reply in a work item's discussion (ADR-0050).

    Coordination, not work product: an Artifact is what the work produced, a
    comment is what was said about it. The author is a Principal taken from the
    authenticated context and never from the request body, so a human and an
    agent are distinguishable in the thread by construction.

    The body is the CURRENT text; every superseded version is kept in
    ``task_comment_revisions``, which is append-only at the database level.
    """

    __tablename__ = "task_comments"
    __table_args__ = (
        CheckConstraint(
            "char_length(body) BETWEEN 1 AND 10000",
            name="body_length",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        # An edit bumps the version and stamps edited_at; the two must agree, so
        # that "was this ever edited?" is answerable from one column.
        CheckConstraint(
            "(version = 1) = (edited_at IS NULL)",
            name="edited_at_matches_version",
        ),
        # Both endpoints in the same tenant, at the DB level (as task_relations).
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_task_comments_task",
        ),
        # The thread reads forward, so the index carries the ascending pair the
        # cursor compares — (created_at, id) — under the task it belongs to.
        Index("ix_task_comments_thread", "tenant_id", "task_id", "created_at", "id"),
        Index("ix_task_comments_author", "tenant_id", "author_principal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID]
    author_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    body: Mapped[str] = mapped_column(Text)
    # Optional provenance: which run said it, which artifact it is about. Both
    # are constrained to the same task by the command, not by the schema —
    # a nullable composite FK cannot express "same task as this comment".
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    edited_at: Mapped[datetime | None]


class TaskCommentRevision(Base):
    """A superseded version of a comment: the audit trail of an edit (ADR-0050).

    Append-only, enforced by a database trigger. Editing a comment writes the
    OLD text here before the new text lands, so the record of what was actually
    said at the time cannot be rewritten by whoever said it.
    """

    __tablename__ = "task_comment_revisions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("comment_id", "version", name="uq_task_comment_revisions_version"),
        Index("ix_task_comment_revisions_comment", "comment_id", "version"),
        Index("ix_task_comment_revisions_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    comment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_comments.id"))
    task_id: Mapped[uuid.UUID]
    # The version this row IS, not the one that replaced it.
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    author_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    # When this version was written, and when it stopped being current.
    created_at: Mapped[datetime]
    superseded_at: Mapped[datetime]
    superseded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))


class Approval(Base):
    """One approval record = one decision (multi-approval = several records)."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')", name="status"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        # Exactly one addressing mode: a specific principal or a required role.
        CheckConstraint(
            "num_nonnulls(required_role_id, assigned_principal_id) = 1",
            name="one_addressing_mode",
        ),
        Index("ix_approvals_tenant_created", "tenant_id", "created_at", "id"),
        Index(
            "ix_approvals_pending",
            "tenant_id",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_approvals_task", "task_id"),
        # v0.3 approval gate: gate=true requires a task to gate.
        CheckConstraint("NOT gate OR task_id IS NOT NULL", name="gate_requires_task"),
        CheckConstraint(
            "outcome_status IS NULL"
            " OR outcome_status IN ('pending', 'deferred', 'executed', 'failed')",
            name="outcome_status",
        ),
        # The worker's scan for outcomes that are due (CP-ADR-0061).
        Index(
            "ix_approvals_outcome_due",
            "outcome_next_attempt_at",
            postgresql_where=text("outcome_status IN ('pending', 'deferred')"),
        ),
        # Fast "is this task gated?" probe inside claim/complete.
        Index(
            "ix_approvals_gate_pending",
            "task_id",
            postgresql_where=text("gate AND status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id"))
    requested_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    status: Mapped[str] = mapped_column(Text, default="pending")
    # v0.3: while a gate approval is pending, its task cannot be claimed or
    # completed (409 approval_required) — first-class approval gate, no
    # workflow engine (ADR-0018).
    gate: Mapped[bool] = mapped_column(default=False)
    required_role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id"))
    assigned_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    decision_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    decision_at: Mapped[datetime | None]
    comment: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    # CP-ADR-0061: NULL when the decision sets nothing in motion (no declared
    # outcome) — exactly an approval as it was before that ADR. Otherwise
    # pending -> executed | failed | deferred (a target task is under a live
    # claim, or an onFailure reaction waits for its skill invocation to end;
    # picked up again later); a failed outcome goes back to pending only via
    # :replay-outcome.
    outcome_status: Mapped[str | None] = mapped_column(Text)
    # The deciding credential as it was at decision time (permissions, carrier,
    # IAM subject): outcome actions run with THIS authority, never the worker's.
    decision_authority: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The executor's own retry state, independent of outbox delivery: attempts
    # that ended in an unexpected error, when the worker looks again, and why.
    outcome_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    outcome_next_attempt_at: Mapped[datetime | None]
    outcome_last_error: Mapped[str | None] = mapped_column(Text)
    # Separation of duties (CP-ADR-0074 section 7): principal ids (as text)
    # whose decision the core refuses, whoever the request comes through.
    excluded_principals: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ApprovalOutcomeAction(Base):
    """One executed (or failed) action of a decided approval's outcome.

    ``(approval_id, action_index)`` is the idempotency key of the executor: a
    redelivered ``approval.approved|rejected`` finds the row and does nothing,
    a replay resumes at the first index without an ``executed`` row.
    """

    __tablename__ = "approval_outcome_actions"
    __table_args__ = (
        CheckConstraint("status IN ('executed', 'failed')", name="status"),
        CheckConstraint("action_index >= 0", name="action_index_nonnegative"),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        UniqueConstraint("approval_id", "action_index", name="uq_approval_outcome_actions_index"),
        Index("ix_approval_outcome_actions_tenant", "tenant_id", "approval_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class TaskCompletionWork(Base):
    """The work a task's type declared for after its completion, as filed.

    One row per task: a completion that files nothing (``when`` unmet) leaves
    no row, one that ran leaves ``executed`` and is never run again, one that
    failed leaves ``failed`` and resumes at its first open action on the next
    completion of the task. ``actions`` — per declared index, what happened.
    """

    __tablename__ = "task_completion_work"
    __table_args__ = (
        CheckConstraint("status IN ('executed', 'failed')", name="status"),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        UniqueConstraint("task_id", name="uq_task_completion_work_task"),
        Index("ix_task_completion_work_tenant", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    task_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_types.id"))
    completed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class TaskVerification(Base):
    """One attempt of the verification stage of a task (CP-ADR-0067).

    Opened when a task with acceptance checks is completed, executed by the
    worker check by check (``cursor``) with the authority of whoever completed
    the task (``authority``), closed ``passed`` (the task is completed),
    ``failed`` (returned to its executor) or ``cancelled`` (the task was).
    ``checks`` is the acceptance the attempt runs, as it was when it opened.
    At most one attempt per task is open at a time; earlier ones stay.
    """

    __tablename__ = "task_verifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'waiting_human', 'waiting_external', 'passed', 'failed', "
            "'cancelled')",
            name="status",
        ),
        CheckConstraint("trigger IN ('run', 'complete', 'approval', 'rule')", name="trigger"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("cursor >= 0", name="cursor_nonnegative"),
        CheckConstraint("jsonb_typeof(checks) = 'array'", name="checks_is_array"),
        CheckConstraint("jsonb_typeof(results) = 'array'", name="results_is_array"),
        CheckConstraint(
            "(status IN ('running', 'waiting_human', 'waiting_external')) = (finished_at IS NULL)",
            name="finished_when_closed",
        ),
        UniqueConstraint("task_id", "attempt", name="uq_task_verifications_task_attempt"),
        # One open attempt per task: a repeated completion finds it (FR-011).
        Index(
            "uq_task_verifications_open",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting_human', 'waiting_external')"),
        ),
        Index(
            "ix_task_verifications_due",
            "next_check_at",
            postgresql_where=text("next_check_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    trigger_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    # The completer's credential snapshot, as an approval keeps its decider's.
    authority: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # The correlation of the completion that opened the attempt: every event
    # of the attempt carries it.
    correlation_id: Mapped[str] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    skill_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("skill_invocations.id"), nullable=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"), nullable=True)
    next_check_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
    # The task's evidence tied to a check when the attempt closed: a later
    # attempt does not count those facts again (CP-ADR-0063 Zh7).
    spent_evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)


# --- M1.3 work derivation rules (CP-ADR-0063) -----------------------------------


class WorkRule(Base):
    """A rule that derives work from observed facts (CP-ADR-0063).

    Tenant data: which facts, which skill and which task type a rule names is
    never known to core. A rule acts with the authority of the credential
    that last enabled it (``authority``), exactly as an approval outcome acts
    with its decider's; ``version`` grows with every change of what it does.
    """

    __tablename__ = "work_rules"
    __table_args__ = (
        CheckConstraint("status IN ('enabled', 'disabled', 'archived')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("jsonb_typeof(trigger) = 'object' AND trigger ? 'kind'", name="trigger"),
        CheckConstraint("jsonb_typeof(action) = 'object' AND action ? 'kind'", name="action"),
        # An enabled rule knows since when and on whose authority it acts.
        CheckConstraint(
            "status <> 'enabled' OR (enabled_at IS NOT NULL AND authority_principal_id "
            "IS NOT NULL)",
            name="enabled_has_authority",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_work_rules_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "goal_id"], ["goals.tenant_id", "goals.id"], name="fk_work_rules_goal"
        ),
        # An archived rule keeps its history but frees its key.
        Index(
            "uq_work_rules_live_key",
            "tenant_id",
            "key",
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        Index("ix_work_rules_tenant_created", "tenant_id", "created_at", "id"),
        Index(
            "ix_work_rules_schedule_due",
            "next_run_at",
            postgresql_where=text("status = 'enabled' AND next_run_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    goal_id: Mapped[uuid.UUID | None]
    key: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Text)
    trigger: Mapped[dict[str, Any]]
    condition: Mapped[Any] = mapped_column(JSONB)
    interpretation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    action: Mapped[dict[str, Any]]
    # Credential snapshot of whoever last enabled the rule (the shape of
    # approvals.decision_authority).
    authority: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    authority_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    # Key of the registry agent the rule evaluates and acts as (amendment
    # 2026-09-27, G1); NULL: it acts with ``authority``.
    identity_agent_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled_at: Mapped[datetime | None]
    # Schedule triggers only: when the next evaluation is due.
    next_run_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    @property
    def identity(self) -> dict[str, Any] | None:
        """The ``identity`` document of the rule, as written."""
        return {"agent": self.identity_agent_key} if self.identity_agent_key else None


class RuleEvaluation(Base):
    """One evaluation of one rule version on one trigger (CP-ADR-0063).

    ``(rule_id, trigger_ref)`` is the idempotency key: a journal event the
    consumer sees again after a crash finds its row and is not evaluated twice.
    """

    __tablename__ = "rule_evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'matched', 'not_matched', 'failed', 'skipped')",
            name="status",
        ),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
        CheckConstraint(
            "jsonb_typeof(created_task_ids) = 'array'", name="created_task_ids_is_array"
        ),
        CheckConstraint(
            "(status = 'waiting') = (next_check_at IS NOT NULL)", name="waiting_has_next_check"
        ),
        UniqueConstraint("rule_id", "trigger_ref", name="uq_rule_evaluations_trigger"),
        Index("ix_rule_evaluations_rule_created", "rule_id", "created_at", "id"),
        Index(
            "ix_rule_evaluations_waiting",
            "next_check_at",
            postgresql_where=text("status = 'waiting'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_rules.id"))
    rule_version: Mapped[int] = mapped_column(Integer)
    # "event:<uuid>" or "schedule:<epoch seconds>".
    trigger_ref: Mapped[str] = mapped_column(Text)
    trigger_event_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    skill_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("skill_invocations.id")
    )
    created_task_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    next_check_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class RuleWorkItem(Base):
    """The work item rules keep for one dedup key (CP-ADR-0063).

    Append-only ledger: ``ensure_work`` looks up the newest row of
    ``(tenant_id, dedup_key)``; while its task is open, that task IS the work
    for the key, once it is closed the next evaluation files a new one. The
    key is tenant-wide, so a second rule (``cancel_work``, ``update_work``)
    reconciles the work a first one filed; ``rule_id`` says who filed it.
    """

    __tablename__ = "rule_work_items"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_rule_work_items_task"),
        Index("ix_rule_work_items_key", "tenant_id", "dedup_key", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_rules.id"))
    dedup_key: Mapped[str] = mapped_column(Text)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    evaluation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rule_evaluations.id"))
    created_at: Mapped[datetime]


# --- v0.3 Harness Protocol & Execution Runtime --------------------------------


class RunCheckpoint(Base):
    """Append-only durable execution metadata for restart/resume.

    NOT memory, NOT chat history: only explicit operational state a harness
    chooses to persist (branch, last commit, next step, ...). ``seq`` is a
    per-run counter assigned under the run row lock.
    """

    __tablename__ = "run_checkpoints"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        UniqueConstraint("run_id", "seq", name="uq_run_checkpoints_run_seq"),
        Index("ix_run_checkpoints_run", "run_id"),
        Index("ix_run_checkpoints_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    created_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


class RunAction(Base):
    """Lightweight execution audit record: one external action of a run.

    Deliberately separate from the domain event journal (ADR-0019): actions
    are telemetry/audit at a different volume and trust level, not domain
    facts. Payload inputs/outputs are NOT stored — only references/metadata.
    """

    __tablename__ = "run_actions"
    __table_args__ = (
        CheckConstraint("status IN ('started', 'completed', 'failed')", name="status"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        UniqueConstraint("run_id", "seq", name="uq_run_actions_run_seq"),
        Index("ix_run_actions_run", "run_id"),
        Index("ix_run_actions_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skills.id"))
    seq: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="started")
    external_reference: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class RunControlMessage(Base):
    """Durable, ordered control input for one active Run.

    The directive is intentional operational input, not a transcript. Domain
    events contain references only and deliberately exclude directive/reason.
    """

    __tablename__ = "run_control_messages"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('queue', 'steer', 'redirect', 'request_cancel', 'force_cancel')",
            name="operation",
        ),
        CheckConstraint(
            "status IN ('accepted', 'applied', 'rejected', 'superseded')",
            name="status",
        ),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status = 'accepted' AND resolved_at IS NULL) OR "
            "(status <> 'accepted' AND resolved_at IS NOT NULL)",
            name="resolution_matches_status",
        ),
        UniqueConstraint("run_id", "seq", name="uq_run_control_messages_run_seq"),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_run_control_messages_run_idempotency"
        ),
        Index("ix_run_control_messages_run", "run_id", "seq"),
        Index("ix_run_control_messages_tenant_run", "tenant_id", "run_id", "seq"),
        Index(
            "ix_run_control_messages_accepted",
            "run_id",
            "seq",
            postgresql_where=text("status = 'accepted'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="accepted")
    causal_position: Mapped[str] = mapped_column(Text)
    directive: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    safe_boundary: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    requested_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    acknowledged_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id")
    )
    request_id: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(Text)
    causation_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    accepted_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]


class RunChildHandle(Base):
    """Durable locator for one child execution launched by a parent Run (HRS-7).

    Deliberately carries no execution status: the child Task/Run remain the
    only source of truth for that, and a second copy would drift. What lives
    here is exactly what has no other home — the launch idempotency key, the
    permission ceiling, the cancellation policy, expiry and revocation.
    """

    __tablename__ = "run_child_handles"
    __table_args__ = (
        CheckConstraint("handle_version >= 1", name="handle_version_positive"),
        CheckConstraint("depth >= 1", name="depth_positive"),
        CheckConstraint(
            "cancellation_policy IN ('cascade_cooperative', 'detach')",
            name="cancellation_policy",
        ),
        CheckConstraint("secret_hash ~ '^sha256:[0-9a-f]{64}$'", name="secret_hash"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_principal_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_principal_id IS NOT NULL)",
            name="revocation_is_attributed",
        ),
        UniqueConstraint(
            "parent_run_id", "correlation_id", name="uq_run_child_handles_parent_correlation"
        ),
        UniqueConstraint("child_task_id", name="uq_run_child_handles_child_task"),
        Index(
            "uq_run_child_handles_child_run",
            "child_run_id",
            unique=True,
            postgresql_where=text("child_run_id IS NOT NULL"),
        ),
        Index("ix_run_child_handles_parent_run", "parent_run_id", "created_at", "id"),
        Index("ix_run_child_handles_tenant_created", "tenant_id", "created_at", "id"),
        Index(
            "ix_run_child_handles_live",
            "parent_run_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    parent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    parent_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    child_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    relation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_relations.id"))
    correlation_id: Mapped[str] = mapped_column(Text)
    secret_hash: Mapped[str] = mapped_column(Text)
    handle_version: Mapped[int] = mapped_column(Integer, default=1)
    granted: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cancellation_policy: Mapped[str] = mapped_column(Text, default="cascade_cooperative")
    depth: Mapped[int] = mapped_column(Integer, default=1)
    created_by_principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    request_id: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    revoke_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]


class RunChildResult(Base):
    """Bounded, immutable terminal result of one child run (HRS-7).

    A separate table rather than a column on the handle: the row is written
    once, inside the child's terminal transition, and a database trigger
    rejects UPDATE and DELETE. ``result_hash`` covers the whole document, so a
    parent can prove it read exactly what the child recorded.
    """

    __tablename__ = "run_child_results"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'cancelled')",
            name="outcome",
        ),
        CheckConstraint("result_hash ~ '^sha256:[0-9a-f]{64}$'", name="result_hash"),
        CheckConstraint("char_length(summary) BETWEEN 1 AND 2000", name="summary_length"),
        UniqueConstraint("handle_id", name="uq_run_child_results_handle"),
        Index("ix_run_child_results_tenant_recorded", "tenant_id", "recorded_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    handle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run_child_handles.id"))
    child_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    outcome: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    artifact_refs: Mapped[list[str]] = mapped_column(default=list)
    result_hash: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime]


# --- v0.5 Project Model -------------------------------------------------------


class WorkspaceType(Base):
    """Tenant-scoped node type: what a workspace *is* and what may nest in it.

    A declarative constraint, not a plugin: it carries a JSON Schema for the
    node's custom fields and a list of allowed child type keys ("*" = any).
    Rules are enforced on create/move/retype under the tenant tree lock
    (ADR-0029).
    """

    __tablename__ = "workspace_types"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("tenant_id", "key", name="uq_workspace_types_tenant_key"),
        UniqueConstraint("tenant_id", "id", name="uq_workspace_types_tenant_id_id"),
        # Exactly one system fallback type per tenant.
        Index(
            "uq_workspace_types_one_system",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_system"),
        ),
        Index("ix_workspace_types_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    field_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    allowed_child_types: Mapped[list[str]] = mapped_column(default=list)
    # The per-tenant fallback type: created by bootstrap/migration, allows any
    # child, and can be neither archived nor deleted.
    is_system: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ProjectTemplate(Base):
    """One immutable version of a project template (ADR-0030).

    ``(tenant_id, key, version)`` is unique and the row never changes after
    INSERT — a database trigger rejects every UPDATE except the single
    ``status: active -> deprecated`` transition.
    """

    __tablename__ = "project_templates"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deprecated')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_project_templates_key_version"),
        UniqueConstraint("tenant_id", "id", name="uq_project_templates_tenant_id_id"),
        Index("ix_project_templates_tenant_key", "tenant_id", "key", "version"),
        Index("ix_project_templates_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON Schema for project custom_fields.
    field_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # {"initialStatus": str, "statuses": [...], "transitions": [...]}
    lifecycle_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Base config document: settings / views / governance / memory / inheritance.
    default_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    default_views: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Typed governance record; the comparison lattice lives in code (ADR-0033).
    governance_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    memory_defaults: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ProjectProfile(Base):
    """Project behaviour attached to exactly one workspace (ADR-0031).

    ``UNIQUE (workspace_id)`` is the whole concurrency story for project
    creation: twenty racing requests produce one row and nineteen conflicts.
    """

    __tablename__ = "project_profiles"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        CheckConstraint(
            "system_status_category IN ('planned', 'active', 'paused', "
            "'terminal_success', 'terminal_cancelled')",
            name="system_status_category",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("workspace_id", name="uq_project_profiles_workspace"),
        UniqueConstraint("tenant_id", "id", name="uq_project_profiles_tenant_id_id"),
        # Tenant-consistent references, enforced by the database.
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_project_profiles_workspace",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["project_templates.tenant_id", "project_templates.id"],
            name="fk_project_profiles_template",
        ),
        Index("ix_project_profiles_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_project_profiles_tenant_status", "tenant_id", "status"),
        Index("ix_project_profiles_template", "template_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID]
    template_id: Mapped[uuid.UUID]
    # User-facing lifecycle key; core only ever branches on the system category.
    status_key: Mapped[str] = mapped_column(Text)
    system_status_category: Mapped[str] = mapped_column(Text)
    owner_principal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    start_date: Mapped[datetime | None]
    target_date: Mapped[datetime | None]
    custom_fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Highest-precedence settings layer of the effective config (ADR-0032).
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active_config_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "project_config_revisions.id",
            use_alter=True,
            name="fk_project_profiles_active_revision",
        )
    )
    status: Mapped[str] = mapped_column(Text, default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    archived_at: Mapped[datetime | None]


class ProjectConfigRevision(Base):
    """Append-only configuration history of one project (ADR-0032).

    Immutable after INSERT except for ``activated_at`` moving NULL -> value;
    a database trigger enforces that and rejects DELETE.
    """

    __tablename__ = "project_config_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        UniqueConstraint("project_id", "revision", name="uq_project_config_revisions_revision"),
        UniqueConstraint("tenant_id", "id", name="uq_project_config_revisions_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["project_profiles.tenant_id", "project_profiles.id"],
            name="fk_project_config_revisions_project",
        ),
        Index("ix_project_config_revisions_project", "project_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    project_id: Mapped[uuid.UUID]
    revision: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # What was checked and against which template version, for later audit.
    validation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    activated_at: Mapped[datetime | None]


class ExternalReference(Base):
    """Mapping from an external system's identifier to an internal entity.

    Product-neutral by construction: no vendor field, no dual-write, never a
    source of truth (ADR-0034).
    """

    __tablename__ = "external_references"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint(
            "tenant_id",
            "external_system",
            "external_type",
            "external_id",
            name="uq_external_references_external_key",
        ),
        Index("ix_external_references_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_external_references_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID]
    external_system: Mapped[str] = mapped_column(Text)
    external_type: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class AttentionFeedback(Base):
    """A principal's verdict on one item of its attention list (CP-ADR-0071).

    One row per ``(principal, item_key)``: a repeated verdict replaces the
    previous one. The item itself is computed, never stored; the row keeps the
    rule, its version, the reason and the score the item had when it was
    judged, so the precision of a rule version can be measured afterwards.
    """

    __tablename__ = "attention_feedback"
    __table_args__ = (
        CheckConstraint("verdict IN ('useful', 'not_needed')", name="verdict"),
        CheckConstraint("rule_version >= 1", name="rule_version_positive"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        UniqueConstraint(
            "tenant_id", "principal_id", "item_key", name="uq_attention_feedback_item"
        ),
        Index("ix_attention_feedback_rule", "tenant_id", "rule_key", "rule_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    item_key: Mapped[str] = mapped_column(Text)
    rule_key: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID]
    score: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class CalendarVersion(Base):
    """One immutable version of a working-day calendar (CP-ADR-0074 §9).

    A trigger rejects every UPDATE and DELETE. ``spec`` is ``$defs.calendarSpec``
    with its lists in canonical order; ``calendar_hash`` is ``sha256:<hex>`` of
    its canonical JSON. The latest version of a key is the one with the
    greatest ``version``.
    """

    __tablename__ = "calendars"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_calendars_tenant_key_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    calendar_hash: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class ProcessDefinition(Base):
    """One immutable version of a process (CP-ADR-0074 §1).

    A trigger rejects every UPDATE and DELETE: instances are pinned to the
    version they started on. ``spec`` is ``$defs.processSpec`` as published
    (normalized, domain/process_definition.py); ``definition_hash`` is
    ``sha256:<hex>`` of its canonical JSON. ``governed_by`` lists the documents
    its elements name, for ``GET /process-definitions?governedBy=``;
    ``warnings`` are the findings that did not refuse the version.
    ``engine_revision`` — the semantics the version runs under (amendment
    2026-09-29): ``1`` before SLA deadlines, ``2`` with them.
    """

    __tablename__ = "process_definitions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint(
            "tenant_id", "key", "version", name="uq_process_definitions_tenant_key_version"
        ),
        Index(
            "ix_process_definitions_governed_by",
            "governed_by",
            postgresql_using="gin",
            postgresql_ops={"governed_by": "jsonb_path_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text)
    definition_hash: Mapped[str] = mapped_column(Text)
    identity_agent: Mapped[str | None] = mapped_column(Text)
    expression_profile: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    governed_by: Mapped[list[str]] = mapped_column(JSONB)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    engine_revision: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


PROCESS_INSTANCE_STATUSES = ("running", "suspended", "completed", "failed", "cancelled")
PROCESS_TIMER_STATES = ("pending", "frozen", "fired", "cancelled")


class ProcessInstance(Base):
    """One case of a process, pinned to the version it started on (CP-ADR-0074 §3).

    ``state`` is the engine's JSON state (domain/process_engine.py) and ``data``
    a copy of its data for reading; both are written only together with the
    journal entry of the step that produced them. ``(tenant, definition_key,
    instance_key)`` is unique: one instance per key. ``refs`` routes the
    journal's facts back to the instance — ``task:<id>``, ``approval:<id>``,
    ``skill:<id>``, ``child:<id>`` — each to the activity that opened it;
    ``activity:<id>`` keeps what the core knows of an open activity (the
    approvers still to ask, the attempt it entered with).
    ``step_attempts`` — ``{element: n}``, the attempt of each step
    (CP-ADR-0074 §13).
    ``sla_due_at``/``sla_warn_at`` — the earliest running deadline and warning
    (CP-ADR-0078 §6), not counting deadlines whose timers are frozen; they
    back the ``slaState`` filter.
    """

    __tablename__ = "process_instances"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'suspended', 'completed', 'failed', 'cancelled')",
            name="status_known",
        ),
        UniqueConstraint(
            "tenant_id",
            "definition_key",
            "instance_key",
            name="uq_process_instances_tenant_definition_key_instance_key",
        ),
        Index("ix_process_instances_tenant_status", "tenant_id", "status", "definition_key"),
        Index("ix_process_instances_refs", "refs", postgresql_using="gin"),
        # The data under a view (CP-ADR-0080 amendment A): data @> {...} and a page of a process.
        Index(
            "ix_process_instances_data",
            "data",
            postgresql_using="gin",
            postgresql_ops={"data": "jsonb_path_ops"},
        ),
        Index(
            "ix_process_instances_tenant_definition_started",
            "tenant_id",
            "definition_key",
            "started_at",
            "id",
        ),
        Index("ix_process_instances_parent", "parent_instance_id"),
        Index(
            "ix_process_instances_sla_due",
            "tenant_id",
            "sla_due_at",
            postgresql_where=text("sla_due_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id"))
    definition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_definitions.id"))
    definition_key: Mapped[str] = mapped_column(Text)
    definition_version: Mapped[int] = mapped_column(Integer)
    instance_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    step_attempts: Mapped[dict[str, int]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    sla_due_at: Mapped[datetime | None]
    sla_warn_at: Mapped[datetime | None]
    parent_instance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("process_instances.id"))
    parent_activity_id: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    started_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class ProcessTimer(Base):
    """A timer of an instance as the engine set it (CP-ADR-0074 §8).

    ``id`` is the engine's timer id. A ``pending`` row with ``due_at`` in the
    past is an input of the timer loop; ``frozen`` keeps ``remaining_seconds``
    while the instance is suspended (``due_at`` null), in ``remaining_unit``
    (``wall | working_seconds | workdays``, CP-ADR-0078 §4); ``fired`` never goes
    back. ``reads`` — the data fields its expression reads, for recomputation.
    """

    __tablename__ = "process_timers"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'frozen', 'fired', 'cancelled')", name="state_known"),
        Index(
            "ix_process_timers_due",
            "due_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_process_timers_instance", "instance_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_instances.id"))
    element: Mapped[str] = mapped_column(Text)
    timer_kind: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None]
    state: Mapped[str] = mapped_column(Text)
    remaining_seconds: Mapped[float | None] = mapped_column(Float)
    remaining_unit: Mapped[str] = mapped_column(Text, default="wall", server_default=text("'wall'"))
    reads: Mapped[list[str]] = mapped_column(JSONB)
    provisional: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    fired_at: Mapped[datetime | None]


class PackageObject(Base):
    """The package that installed a catalog object (CP-ADR-0074 §11 and its amendment).

    One row per ``(tenant, kind, key)`` of the catalog kinds the core holds
    (:data:`control_plane.domain.package_links.LINKED_KINDS`); an object without
    a row was created by hand. ``package_key`` / ``package_version`` — the
    package (``package_version`` is empty for rows applied before it was
    kept); ``plan_hash`` — the hash of the installation: the plan
    ``POST /packages:apply`` applied, or what the installer named in
    ``POST /packages:record``.

    A row ``POST /packages:apply`` wrote is also what the apply wanted:
    ``version`` and ``spec`` (always for ``Process`` and ``Calendar``; for
    ``TaskType``, ``Agent`` and ``WorkRule`` — planned since the amendment of
    2026-09-29 — only when the core applied them, a record of the installer
    leaves them empty). A field of the latest version that differs from
    ``spec`` was changed by a person since, and the plan names its owner
    ``console``; without ``spec`` every field is the package's. Whether the key
    is retired is not the link's business: :class:`CatalogRetirement`.
    """

    __tablename__ = "package_objects"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ArtifactType', 'TaskType', 'ProjectTemplate', 'WorkspaceType', 'Role', "
            "'Capability', 'ConnectionType', 'Skill', 'WorkRule', 'Agent', 'Process', 'Calendar', "
            "'View')",
            name="kind_known",
        ),
        CheckConstraint(
            "kind NOT IN ('Process', 'Calendar', 'View') OR (plan_hash IS NOT NULL"
            " AND version IS NOT NULL"
            " AND spec IS NOT NULL AND spec_hash IS NOT NULL)",
            name="planned_spec",
        ),
        UniqueConstraint("tenant_id", "kind", "key", name="uq_package_objects_tenant_kind_key"),
        Index("ix_package_objects_tenant_package", "tenant_id", "package_key", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    package_key: Mapped[str] = mapped_column(Text)
    package_version: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int | None] = mapped_column(Integer)
    spec_hash: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    plan_hash: Mapped[str | None] = mapped_column(Text)
    applied_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    applied_at: Mapped[datetime]


class View(Base):
    """A screen of a package by description (CP-ADR-0080, TAI-ADR-0066): the key and its state.

    Written only by ``POST /packages:apply`` (under the tenant's apply lock):
    ``current_revision`` is the latest :class:`ViewRevision`; ``status``
    ``retired`` — the package that installed the view no longer brings it.
    Which package that is lives in ``package_objects`` (kind ``View``).
    """

    __tablename__ = "views"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'retired')", name="status_known"),
        CheckConstraint("current_revision >= 1", name="revision_positive"),
        UniqueConstraint("tenant_id", "key", name="uq_views_tenant_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    key: Mapped[str] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    retired_at: Mapped[datetime | None]
    retired_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ViewRevision(Base):
    """An immutable revision of a view: its checked form and the hash of it.

    ``spec`` is the form (``domain/views.py``): the spec with its components
    inlined, ``messages`` — the texts of its keys by locale, ``locales`` and
    ``defaultLocale``. ``source_kind``/``source_key`` and ``audience_roles``
    repeat what a list filters by.
    """

    __tablename__ = "view_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("source_kind IN ('process', 'tasks', 'knowledge')", name="source_known"),
        UniqueConstraint("view_id", "revision", name="uq_view_revisions_view_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("views.id"))
    revision: Mapped[int] = mapped_column(Integer)
    hash: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_kind: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(Text)
    audience_roles: Mapped[list[str] | None] = mapped_column(JSONB)
    package_key: Mapped[str | None] = mapped_column(Text)
    package_version: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class PackageDictionary(Base):
    """The dictionaries of a package, a revision per change (TAI-ADR-0066 p.1a).

    ``messages`` — ``{locale: {key: text}}`` of ``i18n/<locale>.yaml``;
    ``hash`` — of ``locales``, ``defaultLocale`` and ``messages``. An apply of
    the package writes a revision when the hash differs from the latest one.
    """

    __tablename__ = "package_dictionaries"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        UniqueConstraint(
            "tenant_id", "package_key", "revision", name="uq_package_dictionaries_package_revision"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    package_key: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    package_version: Mapped[str | None] = mapped_column(Text)
    locales: Mapped[list[str]] = mapped_column(JSONB)
    default_locale: Mapped[str] = mapped_column(Text)
    messages: Mapped[dict[str, Any]] = mapped_column(JSONB)
    hash: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class PackageSettingsSchema(Base):
    """The settings schema of a package by revision (CP-ADR-0081 §3); rows are only inserted.

    ``POST /packages:apply`` writes a revision when the hash of ``{schema,
    uischema}`` differs from the active one; at most one revision of a package
    is ``active`` (a partial unique index), none once the package stops
    declaring settings. ``schema`` and ``uischema`` are as in the manifest —
    dictionary keys, no strings. Only ``active`` ever changes.
    """

    __tablename__ = "package_settings_schemas"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        UniqueConstraint(
            "tenant_id",
            "package_key",
            "revision",
            name="uq_package_settings_schemas_package_revision",
        ),
        Index(
            "uq_package_settings_schemas_active",
            "tenant_id",
            "package_key",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    package_key: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    package_version: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    uischema: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    schema_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean)
    plan_hash: Mapped[str] = mapped_column(Text)
    applied_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    applied_at: Mapped[datetime]


class PackageSettings(Base):
    """The saved settings of a package, one row per package from its first ``PUT`` (§3).

    ``values`` — what a person saved, nothing of the defaults; ``version`` —
    the number of the latest :class:`PackageSettingsVersion`, moved only by
    ``PUT /packages/{key}/settings``; ``schema_revision`` — the revision the
    values were checked against.
    """

    __tablename__ = "package_settings"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    package_key: Mapped[str] = mapped_column(Text, primary_key=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer)
    schema_revision: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    updated_at: Mapped[datetime]


class PackageSettingsVersion(Base):
    """One saving of the settings of a package (§3); rows are never updated or deleted.

    ``changed_paths`` — JSON Pointers of the members whose saved value differs
    from the version before. The pair (version, schema revision) names the
    effective values an object of the package saw (§6).
    """

    __tablename__ = "package_settings_versions"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    package_key: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schema_revision: Mapped[int] = mapped_column(Integer)
    changed_paths: Mapped[list[str]] = mapped_column(ARRAY(Text))
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    updated_at: Mapped[datetime]


class CatalogRetirement(Base):
    """A process or calendar key taken out of use (CP-ADR-0074, amendment 2026-09-29, Zh1).

    Versions are immutable, so the mark belongs to the key: every version of
    it is retired together. A retired process starts no new instance, its open
    instances go on; a retired calendar is refused to new process versions.
    ``POST /process-definitions/{key}:retire``, ``POST /calendars/{key}:retire``
    and a package renaming the key away write the row; a new version of the
    key deletes it and brings the key back.
    """

    __tablename__ = "catalog_retirements"
    __table_args__ = (CheckConstraint("kind IN ('Process', 'Calendar')", name="kind_known"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    retired_at: Mapped[datetime]
    retired_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    reason: Mapped[str] = mapped_column(Text)


class ProcessRecall(Base):
    """A ``recall`` intent of an instance, executed after its step (CP-ADR-0076 §4).

    ``id`` is the engine's ``recallId`` and ``request`` the intent. The worker
    picks up ``pending`` rows whose ``next_attempt_at`` has come (``SKIP
    LOCKED``), asks memory outside any transaction and gives the answer to the
    instance as its ``recall`` input: ``answered``. A step that stopped
    waiting (its timeout fired, it was cancelled) closes the row: ``closed``.
    """

    __tablename__ = "process_recalls"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'answered', 'closed')", name="state_known"),
        Index(
            "ix_process_recalls_due",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_process_recalls_instance", "instance_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_instances.id"))
    element: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    answered_at: Mapped[datetime | None]


class ProcessInstanceEvent(Base):
    """One step of an instance: its input whole, the decisions and the intents (CP-ADR-0074 §5).

    Append-only (a trigger rejects UPDATE and DELETE). ``source_ref`` names
    where the input came from (``event:<id>``, ``timer:<id>``…) and is unique
    per instance, so the same input delivered twice is taken once. The journal
    alone replays the instance: ``calendars`` names the calendar versions the
    step was computed on; ``settings_version`` and ``settings_schema_revision``
    — the settings of the package a step of a process that reads them saw
    (CP-ADR-0081 §6), ``null`` for every other record.
    """

    __tablename__ = "process_instance_events"
    __table_args__ = (
        UniqueConstraint(
            "instance_id", "source_ref", name="uq_process_instance_events_instance_source"
        ),
        CheckConstraint(
            "(settings_version IS NULL) = (settings_schema_revision IS NULL)"
            " AND (settings_version IS NULL OR settings_version >= 0)",
            name="settings_pair",
        ),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("process_instances.id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text)
    event_id: Mapped[uuid.UUID | None]
    actor_id: Mapped[uuid.UUID | None]
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    intents: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    calendars: Mapped[dict[str, Any]] = mapped_column(JSONB)
    settings_version: Mapped[int | None] = mapped_column(Integer)
    settings_schema_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]
