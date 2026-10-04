"""Domain enumerations. Stored as plain strings; the DB enforces CHECK constraints."""

from enum import StrEnum


class PrincipalKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    CLOSED = "closed"


class ControlLevel(StrEnum):
    """Observed harness operation mode; never an authorization input."""

    MANAGED = "managed"
    CONNECTED = "connected"
    HUMAN_OPERATED = "human_operated"


class TaskStatus(StrEnum):
    """Status keys of the SYSTEM task type only (ADR-0048).

    Since v0.8 the status vocabulary belongs to a tenant's task type, so this
    enumeration is no longer the set of legal values — it names the keys the
    system type ships with, for migrations, tests and defaults. Core decisions
    read ``WorkItemStatusCategory``, never these keys.
    """

    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    STALE = "stale"


class Permission(StrEnum):
    PRINCIPALS_READ = "principals.read"
    PRINCIPALS_WRITE = "principals.write"
    DELEGATIONS_MANAGE = "delegations.manage"
    SESSIONS_OPEN = "sessions.open"
    SESSIONS_MANAGE = "sessions.manage"
    TASKS_READ = "tasks.read"
    TASKS_WRITE = "tasks.write"
    TASKS_CLAIM = "tasks.claim"
    CLAIMS_MANAGE = "claims.manage"
    EVENTS_READ = "events.read"
    # Bulk export of the journal for a period (CP-ADR-0068, export amendment). Apart
    # from events.read: seeing the journal does not include taking it away.
    EVENTS_EXPORT = "events.export"
    # v0.2 organization model
    WORKSPACES_READ = "workspaces.read"
    WORKSPACES_MANAGE = "workspaces.manage"
    ORG_READ = "org.read"
    ORG_MANAGE = "org.manage"
    ARTIFACTS_READ = "artifacts.read"
    ARTIFACTS_WRITE = "artifacts.write"
    APPROVALS_READ = "approvals.read"
    APPROVALS_MANAGE = "approvals.manage"
    APPROVALS_DECIDE = "approvals.decide"
    # v0.4 context memory
    OBSERVATIONS_WRITE = "observations.write"
    # v0.5 project model. Read and manage are separate so a harness key can see
    # project context without being able to reshape it.
    PROJECTS_READ = "projects.read"
    PROJECTS_MANAGE = "projects.manage"
    PROJECT_TEMPLATES_READ = "project_templates.read"
    PROJECT_TEMPLATES_MANAGE = "project_templates.manage"
    # v0.5 operator reliability actions (adapter redrive, journal retention).
    OPERATIONS_READ = "operations.read"
    OPERATIONS_MANAGE = "operations.manage"
    # v0.8 work item model. Separate from tasks.*: the right to shape a
    # tenant's state vocabulary does not follow from the right to file work.
    TASK_TYPES_READ = "task_types.read"
    TASK_TYPES_MANAGE = "task_types.manage"
    # M2.1 skill runtime (ADR-0056). Asking the core to invoke a skill and
    # executing invocations are different roles: the executor is a transport
    # and adds no rights of its own, the caller does not get to run anything.
    SKILLS_INVOKE = "skills.invoke"
    SKILLS_EXECUTE = "skills.execute"
    # M1.1 work graph (CP-ADR-0062). Separate from tasks.*: a Goal states what
    # the tenant wants to be true, and filing work does not grant the right to
    # redefine that.
    GOALS_READ = "goals.read"
    GOALS_WRITE = "goals.write"
    # M1.3 work derivation rules (CP-ADR-0063). Separate from goals.* and
    # tasks.*: a rule files work on its own, long after it was written, so
    # the right to automate that is not implied by the right to file work.
    RULES_READ = "rules.read"
    RULES_WRITE = "rules.write"
    # artifact-handoff (CP-ADR-0072 §6). Separate from artifacts.*: the right to
    # hand in an artifact does not include the right to reshape what a type
    # of artifact must look like.
    ARTIFACT_TYPES_READ = "artifact_types.read"
    ARTIFACT_TYPES_MANAGE = "artifact_types.manage"
    # declarative-agents (CP-ADR-0073 §5). Describing an agent and reporting
    # what actually runs are different roles: the placement service writes the
    # observed state and nothing else, an administrator never writes it.
    AGENTS_READ = "agents.read"
    AGENTS_MANAGE = "agents.manage"
    AGENTS_STATUS_WRITE = "agents.status.write"
    # process-packages (CP-ADR-0074 §11). Publishing a process and operating its
    # instances are different roles: suspending or cancelling a running case is
    # an operator's decision, not the author's. Testing and planning a package
    # write nothing, so they are rights of their own, apart from applying it;
    # a calendar is tenant data every process reads.
    PROCESSES_READ = "processes.read"
    PROCESSES_WRITE = "processes.write"
    PROCESSES_OPERATE = "processes.operate"
    PACKAGES_TEST = "packages.test"
    PACKAGES_PLAN = "packages.plan"
    # package-settings (CP-ADR-0081 §5): reading and changing the settings of a
    # package are rights apart from planning it — installing the code of a
    # package and changing its thresholds are different roles (FR-009).
    PACKAGES_SETTINGS_READ = "packages.settings.read"
    PACKAGES_SETTINGS_MANAGE = "packages.settings.manage"
    CALENDARS_WRITE = "calendars.write"
    # company-knowledge (CP-ADR-0060 amendment 2026-09-28). A tenant registers
    # the ontology packs of its own kinds; the shared registry stays with the
    # platform administrators, so this right never reaches a shared pack.
    KNOWLEDGE_PACKS_MANAGE = "knowledge.packs.manage"
    # integrations-connections (CP-ADR-0079 §12). Reading the tenant's
    # connections and publishing what a connection needs are different roles:
    # a person who sees a connection does not reshape its type.
    CONNECTIONS_READ = "connections.read"
    CONNECTIONS_MANAGE = "connections.manage"
    # The connector's report that a connection stopped working (§3): neither
    # right includes the other, a person does not report for the connector.
    CONNECTIONS_STATUS_WRITE = "connections.status.write"
    # An agent's secrets by name (§11): the values go to the secret store, so
    # setting them is a right of its own, apart from describing the agent.
    AGENTS_SECRETS_MANAGE = "agents.secrets.manage"
    ADMIN = "admin"


ALL_PERMISSIONS = frozenset(p.value for p in Permission)


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectStatus(StrEnum):
    """Record state of a Project Profile — orthogonal to its lifecycle status."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectTemplateStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class TaskTypeStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class ArtifactTypeStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class ConnectionTypeStatus(StrEnum):
    """A version of a connection type (CP-ADR-0079 §2), moved like a skill version."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class ConnectionStatus(StrEnum):
    """Where a connection stands (CP-ADR-0079 §3); a revoked one connects again."""

    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ConnectionAuth(StrEnum):
    """How a connection is authorized now (CP-ADR-0079 §3)."""

    OAUTH2 = "oauth2"
    TOKEN = "token"


class AgentStatus(StrEnum):
    """Record state of an agent (CP-ADR-0073 §2): a retired key stays retired."""

    ACTIVE = "active"
    RETIRED = "retired"


class AgentState(StrEnum):
    """Desired state of an agent, kept apart from its revisions (CP-ADR-0073 §3)."""

    RUNNING = "running"
    STOPPED = "stopped"


class AgentPhase(StrEnum):
    """Observed state of an agent as the placement service reports it (§4)."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_NODE = "waiting_for_node"
    CRASH_LOOPING = "crash_looping"
    NODE_UNAVAILABLE = "node_unavailable"
    STOPPED = "stopped"


class SkillStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class SkillProtocol(StrEnum):
    MCP = "mcp"
    HTTP = "http"
    LOCAL = "local"
    OPENCODE = "opencode"
    CUSTOM = "custom"


# Protocols the core can invoke (ADR-0056 §1). ``opencode``/``custom`` remain
# descriptive values of pre-M2.1 catalog rows only.
INVOCABLE_SKILL_PROTOCOLS = frozenset({SkillProtocol.HTTP, SkillProtocol.LOCAL, SkillProtocol.MCP})


class SkillSideEffects(StrEnum):
    NONE = "none"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"


class SkillRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SkillIdempotency(StrEnum):
    REQUIRED = "required"
    NATURAL = "natural"
    NONE = "none"


class SkillInvocationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SkillInvocationRequester(StrEnum):
    PRINCIPAL = "principal"
    RULE = "rule"
    APPROVAL = "approval"
    VERIFICATION = "verification"
    RUN = "run"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # v0.3: execution paused (approval / external input); terminal for THIS
    # run — continuation is a new claim + a new run reading the checkpoints.
    SUSPENDED = "suspended"


class RunActionStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class RunControlOperation(StrEnum):
    QUEUE = "queue"
    STEER = "steer"
    REDIRECT = "redirect"
    REQUEST_CANCEL = "request_cancel"
    FORCE_CANCEL = "force_cancel"


class RunControlStatus(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# --- v0.3 Harness Protocol ----------------------------------------------------

# Semantic contract version negotiated at session open ("control-harness/<n>").
# v2 (v0.4): eventCursor/context cursors are opaque strings and /events pages
# carry nextCursor+hasMore. v1 sessions are still accepted — that is PROTOCOL
# compatibility, unrelated to the product-name compatibility window closed in
# v0.5 (ADR-0040), which is why the legacy protocol NAME is gone.
HARNESS_PROTOCOL_NAME = "control-harness"
SUPPORTED_HARNESS_PROTOCOL_VERSIONS = frozenset({"1", "2"})

# Protocol capabilities a harness may declare (what the CLIENT can do; not to
# be confused with organizational PrincipalCapability).
KNOWN_HARNESS_CAPABILITIES = frozenset(
    {
        "events.realtime",  # consumes the WebSocket event stream
        "tasks.interactive",  # a human picks tasks (no auto-claim)
        "artifacts.publish",  # can register artifacts
        "approvals.interactive",  # can surface approvals to a human
        "resume",  # persists cursors/state and can resume after restart
        "checkpoints",  # writes run checkpoints
        "active_turn_control.v1",  # consumes durable Run control messages
        "child_run_handle.v1",  # launches and reconnects to child runs
        # skills.protocol.<p>: harness can execute skills of protocol <p>
        *(f"skills.protocol.{p}" for p in ("mcp", "http", "local", "opencode", "custom")),
    }
)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TaskRelationType(StrEnum):
    """Directional semantics (``from --type--> to``):

    - ``parent``:     *from* is a subtask of *to* (*to* is the parent).
    - ``blocks``:     *from* must complete before *to* may be claimed.
    - ``depends_on``: *from* may not be claimed until *to* is completed.
    - ``spawned_by``: *from* was created as a consequence of *to*.
    - ``related_to``: free-form association, no execution semantics.
    """

    PARENT = "parent"
    BLOCKS = "blocks"
    DEPENDS_ON = "depends_on"
    SPAWNED_BY = "spawned_by"
    RELATED_TO = "related_to"


# Relation types that gate task readiness (form the prerequisite graph).
BLOCKING_RELATION_TYPES = frozenset({TaskRelationType.BLOCKS, TaskRelationType.DEPENDS_ON})


class RequirementKind(StrEnum):
    ROLE = "role"
    CAPABILITY = "capability"
    SKILL = "skill"
