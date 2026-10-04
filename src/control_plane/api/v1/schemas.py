"""HTTP contract: request/response schemas (camelCase over the wire)."""

import re
import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.json_schema import SkipJsonSchema

from control_plane.domain.work_item import MAX_COMMENT_BODY_LENGTH
from control_plane.infrastructure.db.models import Base


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


# --- Requests -----------------------------------------------------------------


class IamIdentitySpec(ApiModel):
    """A federated identity as IAM names it: issuer, IAM tenant, IAM principal."""

    issuer: str = Field(min_length=1, max_length=2000)
    iam_tenant_id: uuid.UUID
    iam_principal_id: uuid.UUID


class BootstrapRequest(ApiModel):
    tenant_slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    tenant_name: str = Field(min_length=1, max_length=200)
    admin_display_name: str = Field(min_length=1, max_length=200)
    # One tenant, one UUID across IAM, the Control Plane and the platform core
    # (superproject ADR-0030): the installer passes the IAM tenant id here.
    tenant_id: uuid.UUID | None = None
    # Binds the admin principal to a federated identity in the same
    # transaction, so an IAM-only deployment gets its first administrator
    # without a hand-written SQL row (ADR-0053).
    iam_binding: IamIdentitySpec | None = None


BindingVisibility = Literal["tenant", "members"]


class IamBindingUpsertRequest(IamIdentitySpec):
    permissions: list[str] = Field(min_length=1)
    # Absent — unchanged (``tenant`` for a new binding). Taken as is and
    # checked by the command after principals.write: a wrong value is a
    # ``422 validation_error`` with a JSON Pointer, not the generic
    # ``400 invalid_request`` (CP-ADR-0082 §2.2).
    visibility: Annotated[
        Any, WithJsonSchema({"type": "string", "enum": ["tenant", "members"]})
    ] = None


class PrincipalCreateRequest(ApiModel):
    kind: str
    display_name: str = Field(min_length=1, max_length=200)
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PrincipalDisableRequest(ApiModel):
    """Optional body of ``:disable``; the reason goes to ``principal.disabled``."""

    reason: str | None = Field(default=None, min_length=1, max_length=500)


class PrincipalEnableRequest(ApiModel):
    """Optional body of ``:enable``; the reason goes to ``principal.enabled``."""

    reason: str | None = Field(default=None, min_length=1, max_length=500)


class ApiKeyCreateRequest(ApiModel):
    permissions: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class DelegationCreateRequest(ApiModel):
    human_principal_id: uuid.UUID
    agent_principal_id: uuid.UUID
    permissions: list[str] = Field(default_factory=list)
    starts_at: datetime | None = None
    expires_at: datetime | None = None


class HarnessBlock(ApiModel):
    """Harness registration announced at session open (control-harness/<n>)."""

    type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    version: str = Field(default="", max_length=100)
    protocol_version: str = Field(default="1", max_length=20)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    hostname: str | None = Field(default=None, max_length=255)
    environment: dict[str, Any] = Field(default_factory=dict)


class SessionOpenRequest(ApiModel):
    client_name: str = Field(min_length=1, max_length=200)
    client_version: str = Field(default="", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    on_behalf_of: uuid.UUID | None = None
    ttl_seconds: int | None = None
    harness: HarnessBlock | None = None


class SessionHeartbeatRequest(ApiModel):
    ttl_seconds: int | None = None


class TaskRequirementsSpec(ApiModel):
    roles: list[str] = Field(default_factory=list, max_length=50)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    skills: list[str] = Field(default_factory=list, max_length=50)


# --- work graph documents (CP-ADR-0062) -----------------------------------------
# Typed here for the contract and the OpenAPI document; the domain
# (domain/work_graph.py) re-validates the cross-field rules, because an
# approval outcome or a future rule engine writes the same documents without
# passing through HTTP.


class WorkExternalRefSpec(ApiModel):
    system: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=512)
    url: str | None = Field(default=None, min_length=1, max_length=2048)


class WorkEvidenceSpec(ApiModel):
    """A pointer to one fact: an observation, an artifact, an external object
    or the context pack an executor was given (CP-ADR-0064)."""

    kind: Literal["observation", "artifact", "external", "context_pack"]
    observation_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    context_pack_id: uuid.UUID | None = None
    external_ref: WorkExternalRefSpec | None = None
    # Key of the acceptance check this fact speaks to, if any.
    check: str | None = Field(default=None, min_length=1, max_length=63)
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class WorkOriginSpec(ApiModel):
    """Why a work item (or goal) exists; recorded once, never rewritten."""

    kind: Literal["human", "harness", "rule", "parent", "process", "external"]
    ref: str | None = Field(default=None, min_length=1, max_length=512)
    rule_id: str | None = Field(default=None, min_length=1, max_length=200)
    evidence: list[WorkEvidenceSpec] = Field(default_factory=list, max_length=50)


class AcceptanceCheckSpec(ApiModel):
    """One declared check, executed by the verification stage (CP-ADR-0067).

    On a task, ``spec`` follows the grammar of ``kind``: ``deterministic`` —
    ``{skill: name@version, inputs?, expect?}`` or ``{artifact: {type,
    mediaTypes?, content?}}``; ``external_state`` —
    ``{event?}``; ``human`` — ``{approver? | approverRole?}``; ``llm_judge`` —
    as ``human`` plus ``rubric?``. A spec outside it is
    ``422 invalid_acceptance_spec``. On a goal it is not interpreted.

    ``when`` — ``$.task`` expressions (CP-ADR-0061 grammar) that must all
    resolve to something when the attempt reaches the check; otherwise the
    check is ``skipped`` with reason ``condition_unmet``. Not on a goal.
    """

    key: str = Field(min_length=1, max_length=63)
    kind: Literal["deterministic", "external_state", "human", "llm_judge"]
    description: str = Field(min_length=1, max_length=2000)
    spec: dict[str, Any] | None = None
    # CP-ADR-0067 amendment 2026-09-27 (B6): $.task expressions that must all
    # resolve to something for the check to run; unmet, it is ``skipped``.
    when: list[Annotated[str, Field(min_length=1, max_length=500)]] | None = Field(
        default=None, min_length=1, max_length=8
    )


# CP-ADR-0073 amendment A1: an assignment field names a principal by id or an
# agent of the registry by key; the core resolves the key when it writes.
AGENT_REFERENCE_PREFIX = "agent:"
AgentReference = Annotated[str, Field(pattern=r"^agent:[a-z0-9][a-z0-9-]{0,62}$")]


def work_document(value: ApiModel | Sequence[ApiModel] | None) -> Any:
    """A typed work-graph spec as the camelCase JSON document the domain stores."""
    if value is None:
        return None
    if not isinstance(value, ApiModel):
        return [work_document(item) for item in value]
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


class TaskCreateRequest(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    priority: str = "medium"
    # None means "the initial status declared by the task type" (ADR-0048).
    status: str | None = Field(default=None, min_length=1, max_length=64)
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    type_version: int | None = Field(default=None, ge=1)
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | AgentReference | None = None
    workspace_id: uuid.UUID | None = None
    # v0.8: validated against the field_schema of the selected type version.
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    start_date: datetime | None = None
    due_date: datetime | None = None
    parent_task: str | None = Field(default=None, min_length=1, max_length=100)
    requirements: TaskRequirementsSpec | None = None
    # M1.1 work graph (CP-ADR-0062). Without origin, the core derives it:
    # "parent" when parentTask is given, else from the writer's principal kind.
    goal_id: uuid.UUID | None = None
    origin: WorkOriginSpec | None = None
    acceptance: list[AcceptanceCheckSpec] = Field(default_factory=list, max_length=50)
    evidence: list[WorkEvidenceSpec] = Field(default_factory=list, max_length=200)


class TaskUpdateRequest(ApiModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    # A status KEY of the task type's lifecycle. The system status category is
    # never accepted from a client — it is derived from the key.
    status: str | None = Field(default=None, min_length=1, max_length=64)
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | AgentReference | None = None
    workspace_id: uuid.UUID | None = None
    # v0.8: whole-document replace (a merge could never remove a key); the
    # dates are nullable, so an explicit null clears a planned date.
    custom_fields: dict[str, Any] | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    requirements: TaskRequirementsSpec | None = None
    # CP-ADR-0062: goalId null unlinks; acceptance and evidence are
    # whole-document replaces. There is no origin here on purpose: where the
    # work came from is not editable.
    goal_id: uuid.UUID | None = None
    acceptance: list[AcceptanceCheckSpec] | None = Field(default=None, max_length=50)
    evidence: list[WorkEvidenceSpec] | None = Field(default=None, max_length=200)
    claim_id: uuid.UUID | None = None
    fencing_token: int | None = None


class TaskMigrateTypeRequest(ApiModel):
    """ADR-0048, amendment 2026-09-30: move a task to another version of its type.

    ``typeVersion`` omitted means the newest active version of the task's key;
    ``statusMap`` maps a status of the current version to one of the target.
    """

    type_version: int | None = Field(default=None, ge=1)
    status_map: dict[str, str] | None = Field(default=None, max_length=64)


class TaskTypeMigrateTasksRequest(ApiModel):
    """Move the open tasks of a type version, one page per call."""

    to_version: int | None = Field(default=None, ge=1)
    status_map: dict[str, str] | None = Field(default=None, max_length=64)
    limit: int | None = Field(default=None, ge=1, le=500)
    cursor: uuid.UUID | None = None


class TaskCompleteRequest(ApiModel):
    claim_id: uuid.UUID | None = None
    fencing_token: int | None = None


class ClaimTaskRequest(ApiModel):
    session_id: uuid.UUID
    ttl_seconds: int | None = None
    intent: str = Field(default="", max_length=500)


class ClaimHeartbeatRequest(ApiModel):
    ttl_seconds: int | None = None


class ClaimReleaseRequest(ApiModel):
    reason: str = Field(default="released", min_length=1, max_length=200)


class ClaimReclaimRequest(ApiModel):
    session_id: uuid.UUID
    ttl_seconds: int | None = None
    intent: str = Field(default="", max_length=500)


class ContextQueryRequest(ApiModel):
    """Working-context request. The server resolves and authorizes every
    scope inside the caller's tenant before any memory-provider call."""

    query: str = Field(default="", max_length=2000)
    task: str | None = None  # id or publicId
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    include_subprojects: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    include_memory: bool = True
    anchors: list[str] = Field(default_factory=list, max_length=10)
    # Memory compilation strategy (MEM-ADR-016/019): "briefing" builds the standing
    # context of the principal without a query (TAI-ADR-0031 §6).
    strategy: str | None = Field(
        default=None, pattern=r"^(semantic|exact|graph|hybrid|context|briefing)$"
    )
    # Point-in-time recall (TAI-ADR-0042): passed to Memory only when set.
    as_of: AwareDatetime | None = None


_GRAPH_NAME = r"^[a-z][a-z0-9_]{0,62}$"
_WHERE_SCALAR = (str, int, float, bool)
_PREFIX_CODE = re.compile(r"^[^.]+(\.[^.]+)*$")
_DATE_START = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class MemoryWhereCondition(ApiModel):
    """One ``recall.where`` condition (``$defs/memoryWhere``, MEM-ADR-020).

    The value is a literal the caller already computed: the core evaluates no
    CEL here and sends the condition to memory unchanged. The value rules are
    memory's (``context/where.parse_where``): ``eq`` a scalar, ``in`` 1..100
    scalars, ``prefix`` a dotted code, ``lte``/``gte`` a number or an RFC 3339
    date, ``exists`` a bool (true when omitted).
    """

    attr: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    op: Literal["eq", "in", "prefix", "lte", "gte", "exists"]
    value: Any = Field(
        default=None,
        description="eq: string|number|boolean; in: array of 1..100 of them; "
        "prefix: dotted code; lte/gte: number or RFC 3339 date; exists: boolean",
    )

    @model_validator(mode="after")
    def _value_fits_op(self) -> "MemoryWhereCondition":
        given = "value" in self.model_fields_set
        value = self.value
        if self.op == "exists":
            ok = not given or isinstance(value, bool)
        elif not given:
            ok = False
        elif self.op == "eq":
            ok = isinstance(value, _WHERE_SCALAR)
        elif self.op == "in":
            ok = (
                isinstance(value, list)
                and 1 <= len(value) <= 100
                and all(isinstance(v, _WHERE_SCALAR) for v in value)
            )
        elif self.op == "prefix":
            ok = isinstance(value, str) and bool(_PREFIX_CODE.match(value))
        else:
            ok = _is_number(value) or (isinstance(value, str) and bool(_DATE_START.match(value)))
        if not ok:
            raise ValueError(f"value does not fit op {self.op!r}")
        return self

    def to_memory(self) -> dict[str, Any]:
        """The condition as memory's typed traversal takes it."""
        return self.model_dump(mode="json", exclude_unset=True)


class RecallRequest(ApiModel):
    """Pull from the knowledge graph (``cp_recall``, CP-ADR-0064).

    Exactly one of ``anchor`` (an identifier: a key, an alias or text an
    ``idPattern`` matches) and ``query`` (text to extract identifiers from).
    ``relations`` are followed from the anchors, each ``depth`` steps in
    ``direction``, at most ``limit`` new entities per step. The namespaces come
    from ``task``/``workspaceId``, never from the client. The pack in the
    answer is cut to ``budgetTokens`` (``omitted`` counts the rest).
    ``where`` filters the nodes by their attributes (all conditions hold);
    it goes to memory as it is.
    """

    anchor: str | None = Field(default=None, min_length=1, max_length=300)
    kind: str | None = Field(default=None, pattern=_GRAPH_NAME)
    query: str | None = Field(default=None, min_length=1, max_length=2000)
    kinds: list[Annotated[str, Field(pattern=_GRAPH_NAME)]] = Field(
        default_factory=list, max_length=20
    )
    relations: list[Annotated[str, Field(pattern=_GRAPH_NAME)]] = Field(
        default_factory=list, max_length=10
    )
    direction: Literal["in", "out", "both"] = "both"
    depth: int = Field(default=1, ge=1, le=5)
    limit: int = Field(default=20, ge=1, le=200)
    as_of: AwareDatetime | None = None
    task: str | None = None
    workspace_id: uuid.UUID | None = None
    budget_tokens: int = Field(default=3_000, ge=1, le=32_000)
    where: list[MemoryWhereCondition] = Field(default_factory=list, max_length=20)


class ObservationExternalRef(ApiModel):
    """The observed object in its own system (issue, alert, commit...)."""

    system: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=512)
    url: str | None = Field(default=None, min_length=1, max_length=2048)


class ObservationCreateRequest(ApiModel):
    """Explicit remember: only intentional, externalized knowledge — never
    hidden reasoning, raw prompts or terminal history."""

    kind: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=65_536)
    data: dict[str, Any] | None = None
    assertions: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    task: str | None = None  # id or publicId
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    # External observations (CP-ADR-0057): where the fact was seen, how to
    # recognise a repeat of it, and which earlier observation it replaces.
    source: str | None = Field(default=None, min_length=1, max_length=128)
    dedup_key: str | None = Field(default=None, min_length=1, max_length=512)
    observed_at: AwareDatetime | None = None
    supersedes: uuid.UUID | None = None
    external_ref: ObservationExternalRef | None = None


class ObservationRecordedOut(ApiModel):
    id: uuid.UUID
    event_id: uuid.UUID
    kind: str
    recorded_at: datetime
    # True when (source, dedupKey) matched an existing observation (HTTP 200).
    deduplicated: bool = False


# Memory's snapshot limits (``domain.reconcile``: MAX_SOURCE_LEN, MAX_SCOPE_LEN,
# MAX_SNAPSHOT_ID_LEN, ``reconcile_max_items``).
KNOWLEDGE_SOURCE_MAX = 200
KNOWLEDGE_SCOPE_MAX = 200
KNOWLEDGE_SNAPSHOT_ID_MAX = 200
KNOWLEDGE_MAX_ITEMS = 20_000


class KnowledgeSnapshotPreviewRequest(ApiModel):
    """A connector's snapshot of one source (CP-ADR-0060), forwarded as-is.

    Namespace and visibility scopes are computed by the core from
    ``workspaceId``; a client that sends them gets 400 like any unknown field.
    Entities and relations are opaque here: Memory validates them against the
    pack. Bounds mirror Memory's ``parse_snapshot`` so that an oversized
    document is refused here, not after a round trip."""

    workspace_id: uuid.UUID
    # Optional as in Memory (``pack: str = ""``): without it Memory checks kinds
    # against the namespace's catalog only.
    pack: str | None = Field(default=None, min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=KNOWLEDGE_SOURCE_MAX)
    # The snapshot's scope within its source: a string, not a memory namespace.
    scope: str | None = Field(default=None, max_length=KNOWLEDGE_SCOPE_MAX)
    snapshot_id: str = Field(min_length=1, max_length=KNOWLEDGE_SNAPSHOT_ID_MAX)
    observed_at: AwareDatetime
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=KNOWLEDGE_MAX_ITEMS)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=KNOWLEDGE_MAX_ITEMS)

    @model_validator(mode="after")
    def _bounded_items(self) -> "KnowledgeSnapshotPreviewRequest":
        if len(self.entities) + len(self.relations) > KNOWLEDGE_MAX_ITEMS:
            raise ValueError(
                f"entities and relations together must not exceed {KNOWLEDGE_MAX_ITEMS} items"
            )
        return self

    def snapshot_document(self) -> dict[str, Any]:
        """The snapshot as Memory reads it: camelCase fields, no ``workspaceId``,
        ``null`` fields omitted."""
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"workspace_id", "expected_state"},
            exclude_none=True,
        )


# The state fingerprint Memory answers a reconciliation with (``stateToken``,
# amendment MEM-ADR-020): opaque to the core, passed back as ``expectedState``.
# The bound is Memory's (``ReconcileIn.expectedState``): a longer token would be
# its 422, i.e. a 502 here instead of the caller's 400.
KNOWLEDGE_STATE_TOKEN_MAX = 128


class KnowledgeSnapshotRequest(KnowledgeSnapshotPreviewRequest):
    """A snapshot to apply. ``expectedState`` (CP-ADR-0060 amendment
    2026-09-28) applies it only while the knowledge of its source is still in
    the state a preview showed: otherwise ``409 snapshot_stale``."""

    expected_state: str | None = Field(
        default=None, min_length=1, max_length=KNOWLEDGE_STATE_TOKEN_MAX
    )


class KnowledgeSnapshotPreviewOut(BaseModel):
    """Memory's plan of a reconciliation, returned as-is: what would open,
    change and close, and the state it was computed on. Only ``stateToken`` is
    the core's to name; the rest is Memory's (MEM-ADR-020)."""

    model_config = ConfigDict(extra="allow")

    state_token: str = Field(alias="stateToken")


# Memory's document ingest bound (``retrieval.documents.MAX_CHUNKS_PER_REQUEST``).
KNOWLEDGE_DOCUMENT_MAX_CHUNKS = 500


class KnowledgeDocumentChunk(ApiModel):
    text: str = Field(min_length=1)
    heading: str = Field(default="", max_length=500)
    order: int | None = Field(default=None, ge=0)


class KnowledgeDocumentLink(ApiModel):
    """An entity of the knowledge base the document is about: ``{kind, key}``
    as a snapshot names it, and the relation from the document to it."""

    kind: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=512)
    rel: str = Field(min_length=1, max_length=128)


class KnowledgeDocumentRequest(ApiModel):
    """A document of the knowledge base (CP-ADR-0060 amendment 2026-09-28):
    text already cut into chunks by the caller -- the core parses no files --
    stored in the namespace of the workspace tree root, like a snapshot."""

    workspace_id: uuid.UUID
    natural_key: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)
    type: str = Field(default="document", min_length=1, max_length=128)
    chunks: list[KnowledgeDocumentChunk] = Field(
        min_length=1, max_length=KNOWLEDGE_DOCUMENT_MAX_CHUNKS
    )
    links: list[KnowledgeDocumentLink] = Field(default_factory=list, max_length=200)
    meta: dict[str, Any] | None = None

    def memory_document(self) -> dict[str, Any]:
        """The document as Memory's ``DocumentIngestRequest`` reads it, without
        namespace and scopes (the core adds them).

        Memory's ``links`` are natural keys that become untyped ``LINKS_TO``
        edges to existing nodes, so they carry the keys; the typed links
        ``{kind, key, rel}`` travel whole in ``properties.links``. One call
        holds the whole document: ``replace`` swaps the chunks of an earlier
        write with the same key."""
        document: dict[str, Any] = {
            "natural_key": self.natural_key,
            "title": self.title,
            "type": self.type,
            "chunks": [chunk.model_dump(exclude_none=True) for chunk in self.chunks],
            "replace": True,
        }
        if self.links:
            document["links"] = list(dict.fromkeys(link.key for link in self.links))
            document["properties"] = {"links": [link.model_dump() for link in self.links]}
        if self.meta is not None:
            document["meta"] = self.meta
        return document


# Memory's entity list bounds (``context/entities``: MAX_KINDS, MAX_LIMIT, KIND_RE).
KNOWLEDGE_ENTITY_KIND = r"^[A-Za-z][A-Za-z0-9_]{0,62}$"
KNOWLEDGE_ENTITIES_MAX_LIMIT = 500
# Relations of an entity (amendment 2026-10-03): a relation name as Memory's
# packs declare it (``RelationSpecIn``), at most a step limit of its typed
# traversal per entity, and a smaller page: each entity is one traversal.
KNOWLEDGE_RELATION_NAME = r"^[a-z][a-z0-9_]{0,62}$"
KNOWLEDGE_RELATIONS_MAX_NAMES = 20
KNOWLEDGE_RELATIONS_MAX_LIMIT = 200
KNOWLEDGE_RELATIONS_MAX_PAGE = 100


class KnowledgeEntitiesInclude(ApiModel):
    """What to add to every entity of the page (CP-ADR-0060, amendment 2026-10-03).

    ``relations`` -- the relation names, or ``"*"`` for every relation the
    namespace's packs declare; ``direction`` -- the entity as the subject
    (``out``), the object (``in``) or either; ``limit`` -- relations per entity."""

    relations: (
        Literal["*"]
        | Annotated[
            list[Annotated[str, Field(pattern=KNOWLEDGE_RELATION_NAME)]],
            Field(min_length=1, max_length=KNOWLEDGE_RELATIONS_MAX_NAMES),
        ]
    )
    direction: Literal["out", "in", "both"] = "both"
    limit: int = Field(default=20, ge=1, le=KNOWLEDGE_RELATIONS_MAX_LIMIT)


class KnowledgeEntitiesQueryRequest(ApiModel):
    """The entities of ``kinds`` in a workspace's knowledge (CP-ADR-0060, K031).

    Every entity of the kinds valid at ``asOf`` (now when omitted) whose
    attributes satisfy all ``where`` conditions, ``limit`` per page; ``cursor``
    is the ``nextCursor`` of the previous page, the other fields unchanged.
    The namespace (the workspace tree root) and the visibility come from
    ``workspaceId`` and the caller, never from the client."""

    workspace_id: uuid.UUID
    kinds: list[Annotated[str, Field(pattern=KNOWLEDGE_ENTITY_KIND)]] = Field(
        min_length=1, max_length=20
    )
    where: list[MemoryWhereCondition] = Field(default_factory=list, max_length=20)
    as_of: AwareDatetime | None = None
    limit: int = Field(default=100, ge=1, le=KNOWLEDGE_ENTITIES_MAX_LIMIT)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    include: KnowledgeEntitiesInclude | None = None

    @model_validator(mode="after")
    def _include_page(self) -> "KnowledgeEntitiesQueryRequest":
        if self.include is not None and self.limit > KNOWLEDGE_RELATIONS_MAX_PAGE:
            raise ValueError(
                f"limit is at most {KNOWLEDGE_RELATIONS_MAX_PAGE} when include is given"
            )
        return self


def _transitional(description: str) -> Any:
    return Field(
        default=None,
        deprecated=True,
        description=f"{description} Transitional (CP-ADR-0060, amendment 2026-10-03): "
        "removed only by a later amendment.",
    )


class KnowledgeEntitySourceOut(BaseModel):
    """A source the entity's version was merged from, the senior first."""

    model_config = ConfigDict(populate_by_name=True)

    source: str
    citation: str = Field(
        default="", alias="sourcePath", description="Path or link to the source's version."
    )
    snapshot: str | None = Field(default=None, alias="snapshotId")
    source_path: str | None = _transitional("sourcePath.")
    snapshot_id: str | None = _transitional("snapshotId.")
    scope: str | None = _transitional("The visibility scope of the source.")


class KnowledgeEntityRelationOut(BaseModel):
    """A relation of the entity and its other end (``include.relations``)."""

    relation: str
    direction: Literal["out", "in"] = Field(
        description="out: the entity is the subject; in: the object."
    )
    kind: str
    key: str
    title: str = ""


class KnowledgeEntityOut(BaseModel):
    """An entity of the list: the version valid at ``asOf``, merged from its
    sources (CP-ADR-0060, amendment 2026-10-03). ``validFrom``, ``validTo``
    and ``sources`` are the contract; the snake_case fields are Memory's
    ``EntityItem`` as it was passed before, kept for the transition."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    key: str
    title: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from_at: str | None = Field(
        default=None, alias="validFrom", description="Start of the version valid at asOf."
    )
    valid_to_at: str | None = Field(
        default=None, alias="validTo", description="End of that version; null -- open."
    )
    sources: list[KnowledgeEntitySourceOut] = Field(
        default_factory=list,
        description="Every source the entity is merged from, the senior first. Each also "
        "carries the transitional scope, snapshot_id and source_path.",
    )
    relations: list[KnowledgeEntityRelationOut] | None = Field(
        default=None, description="Only when the request has include.relations."
    )
    source: str | None = _transitional("The senior source: sources[0].source.")
    source_path: str | None = _transitional("The senior citation: sources[0].sourcePath.")
    snapshot_id: str | None = _transitional("The senior snapshot: sources[0].snapshotId.")
    valid_from: str | None = _transitional("validFrom.")
    valid_to: str | None = _transitional("validTo.")
    namespace: str | None = _transitional("Memory's namespace of the workspace tree root.")
    scope: str | None = _transitional("The visibility scope of the senior source.")


class KnowledgeEntitiesPageOut(BaseModel):
    """One page in ``(kind, key, namespace)`` order. The list ends only at
    ``nextCursor: null``: a page may hold fewer than ``limit`` items before it."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[KnowledgeEntityOut]
    next_cursor: str | None = Field(alias="nextCursor")
    as_of: str | None = Field(default=None, alias="asOf")


class KnowledgePackRegisterRequest(BaseModel):
    """A domain pack manifest, forwarded to Memory as-is (CP-ADR-0060).

    Only ``scope`` is the core's to read: absent -- a shared pack, registered
    by platform administrators; ``tenant`` -- a pack of the caller's tenant,
    registered under ``knowledge.packs.manage`` (amendment 2026-09-28).
    ``name`` and ``version`` are checked by the core with ``422 pack_invalid``,
    the rest of the manifest by Memory. The owner of a tenant pack is the
    core's to name: a ``namespace`` in the manifest is refused."""

    model_config = ConfigDict(extra="allow")

    name: Any = Field(default=None, description="Pack name (Memory's grammar).")
    version: Any = Field(default=None, description="Pack version: a string or a number.")
    scope: Literal["common", "tenant"] | None = Field(
        default=None,
        description="Absent or common: a shared pack (Memory's default scope). "
        "tenant: a pack of the caller's tenant.",
    )

    @model_validator(mode="after")
    def _no_owner(self) -> "KnowledgePackRegisterRequest":
        if self.model_extra and "namespace" in self.model_extra:
            raise ValueError("namespace is set by the core, not by the caller")
        return self


class WorkspaceKnowledgePacksRequest(ApiModel):
    packs: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=64)
    strict: bool = False


class WorkspaceKnowledgePacksOut(ApiModel):
    """The packs of a workspace tree's namespace (CP-ADR-0060, amendment
    2026-09-30): ``packs`` and ``strict`` as ``PUT .../knowledge-packs`` takes
    them, ``effective`` -- the packs Memory applies (its default pack while
    nobody configured the namespace, ``configured: false``)."""

    workspace_id: uuid.UUID
    root_workspace_id: uuid.UUID
    configured: bool
    packs: list[str]
    strict: bool
    effective: list[str]
    updated_at: str | None = None


class KnowledgePackOut(BaseModel):
    """A registered pack version as Memory gives it (``kinds`` with their
    ``idPatterns``, ``relations``, ``description``), with ``scope`` (``common``
    or ``tenant``) and ``ref``, the pinned reference Memory enables it by
    (``name@version`` or ``tenant:name@version``)."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    scope: Literal["common", "tenant"] = "common"
    ref: str | None = None
    kinds: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)


# --- Responses ----------------------------------------------------------------


class TenantOut(ApiModel):
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime


def _is_none(value: object) -> bool:
    return value is None


def _absent_not_null(schema: dict[str, Any]) -> None:
    """Drop ``default: null``: an optional field here is absent, ``null`` is refused."""
    for prop in schema.get("properties", {}).values():
        if prop.get("default", ...) is None:
            del prop["default"]


class PrincipalProfile(ApiModel):
    """Display details of a principal in the organization (CP-ADR-0082 §1.2).

    Every field is optional; an absent one is left out of the answer, not
    sent as ``null``. ``PATCH /principals/{id}`` checks a profile it receives
    against ``domain/principal_profile.PROFILE_SCHEMA``, the same shape.
    """

    model_config = ConfigDict(json_schema_extra=_absent_not_null)

    job_title: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=200, exclude_if=_is_none
    )
    email: str | SkipJsonSchema[None] = Field(
        default=None, max_length=254, json_schema_extra={"format": "email"}, exclude_if=_is_none
    )
    phone: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=50, exclude_if=_is_none
    )
    note: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=2000, exclude_if=_is_none
    )


class PrincipalUpdateRequest(ApiModel):
    """Body of ``PATCH /principals/{id}``; ``profile`` replaces the whole profile.

    Documentation of the route only: the route checks the body itself, in the
    order of CP-ADR-0082 §1.4 (``domain/principal_profile``).
    """

    model_config = ConfigDict(json_schema_extra=_absent_not_null)

    display_name: str | SkipJsonSchema[None] = Field(default=None, min_length=1, max_length=200)
    profile: PrincipalProfile | SkipJsonSchema[None] = None


class FieldError(ApiModel):
    """One field error; never the value of the field (CP-ADR-0082 §1.4, CP-ADR-0081 §4.3)."""

    model_config = ConfigDict(json_schema_extra=_absent_not_null)

    path: str = Field(
        description=(
            "JSON Pointer into the request body; query.<name> or path.<name> for a "
            "request parameter with nul_character (CP-ADR-0083)"
        ),
        examples=["/profile/email"],
    )
    code: str | SkipJsonSchema[None] = Field(
        default=None,
        description="JSON Schema keyword, or nul_character (CP-ADR-0083)",
        examples=["format"],
    )
    message: str | SkipJsonSchema[None] = Field(
        default=None, description="Explanation without the value"
    )
    field: str | SkipJsonSchema[None] = Field(
        default=None, description="secret_material_rejected only, in addition to path"
    )
    match: str | SkipJsonSchema[None] = Field(
        default=None,
        description="secret_material_rejected only: kind of material, never the material",
    )


class FieldErrorDetails(ApiModel):
    errors: list[FieldError] = Field(min_length=1)


class FieldErrorBody(ApiModel):
    code: Literal["validation_error", "secret_material_rejected", "visibility_requires_human"]
    message: str
    details: FieldErrorDetails
    request_id: str


class FieldErrorResponse(ApiModel):
    error: FieldErrorBody


class PrincipalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    display_name: str
    status: str
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    profile: PrincipalProfile
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class PrincipalEnabledOut(PrincipalOut):
    """``principals/{id}:enable``: the principal plus what came back with its status."""

    live_api_keys: int = Field(
        ge=0,
        description="API keys of the principal that are not revoked and not expired:"
        " :disable does not revoke keys, so these authenticate again (CP-ADR-0077)",
    )


class RoleHolderOut(ApiModel):
    """A principal holding a role in a workspace (``GET /roles/{id}/principals``)."""

    id: uuid.UUID
    kind: str
    display_name: str
    status: str


class ApiKeyOut(ApiModel):
    id: uuid.UUID
    principal_id: uuid.UUID
    key_prefix: str
    permissions: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    # The full key is returned exactly once, at creation time. An idempotent
    # replay of the same request returns key=null (secrets are never stored).
    key: str | None


class IamBindingOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    issuer: str
    iam_tenant_id: uuid.UUID
    iam_principal_id: uuid.UUID
    permissions: list[str]
    status: str
    visibility: BindingVisibility
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DelegationOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    human_principal_id: uuid.UUID
    agent_principal_id: uuid.UUID
    permissions: list[str]
    starts_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class SessionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    on_behalf_of_id: uuid.UUID | None
    delegation_id: uuid.UUID | None
    status: str
    client_name: str
    client_version: str
    control_level: str
    harness_type: str | None
    harness_version: str | None
    protocol_version: str | None
    harness_capabilities: list[str] | None
    hostname: str | None
    environment: dict[str, Any] | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    started_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ended_at: datetime | None


class TaskVerificationSummaryOut(ApiModel):
    id: uuid.UUID
    status: str
    attempt: int
    updated_at: datetime


class TaskOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    public_id: str
    workspace_id: uuid.UUID | None
    # v0.5: derived from the workspace tree at read time, never stored.
    project_id: uuid.UUID | None = None
    # v0.8: the type this task was created against, denormalized for readers.
    type_id: uuid.UUID
    type_key: str = ""
    type_version: int = 0
    title: str
    description: str
    # The tenant's own lifecycle key; core branches on the category next to it.
    status: str
    system_status_category: str
    priority: str
    owner_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    # v0.8: tenant-defined fields and planned dates (ADR-0049).
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    start_date: datetime | None = None
    due_date: datetime | None = None
    # M1.1 work graph (CP-ADR-0062).
    goal_id: uuid.UUID | None = None
    origin: dict[str, Any] = Field(default_factory=dict)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    version: int
    claim_epoch: int
    active_claim_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    # M1.6 (CP-ADR-0067): the newest verification attempt, in brief; null for
    # a task never handed in with acceptance checks.
    verification: TaskVerificationSummaryOut | None = None


class TaskVerificationOut(ApiModel):
    """One attempt of the verification stage (CP-ADR-0067).

    ``checks`` — the checks the attempt runs, as they were when it opened,
    each with its ``source``: ``output`` (a required output of the type),
    ``type`` (declared by the task type version), ``task`` (the task's own
    acceptance) or ``rule`` (the implicit check of a rule closing the work);
    ``results`` — per check run so far: ``{key, kind, source, status,
    evidence, reason, message?, details?}``, ``status`` ``passed | skipped |
    failed | cancelled`` (``skipped`` — its ``when`` did not hold,
    ``details.when`` names the expression); ``cursor`` — the index of the
    check it is at. Attempts opened before 2026-09-27 carry no ``source``.
    The completer's credential snapshot stays internal.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    attempt: int
    status: str
    trigger: str
    trigger_ref: str | None
    authority_principal_id: uuid.UUID
    checks: list[dict[str, Any]]
    results: list[dict[str, Any]]
    cursor: int
    skill_invocation_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    next_check_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    updated_at: datetime


class ClaimOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    session_id: uuid.UUID
    holder_id: uuid.UUID
    status: str
    fencing_token: int
    intent: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None
    release_reason: str | None


class EventOut(ApiModel):
    """One journal event. ``sequence`` is an identifier/audit field; the
    replay cursor is the opaque ``cursor`` attached by the endpoint."""

    sequence: int
    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str = Field(serialization_alias="type")
    entity_type: str
    entity_id: uuid.UUID
    actor_id: uuid.UUID | None
    session_id: uuid.UUID | None
    correlation_id: str
    causation_id: str | None
    request_id: str
    # v0.5 trace correlator (X-Run-Id); null on pre-v0.5 events (ADR-0039).
    trace_run_id: str | None
    # IAM identity of the actor (CP-ADR-0055); null for legacy keys and old events.
    iam_actor_id: uuid.UUID | None = None
    # Workspace of the event's entity; null at tenant level and on events older
    # than CP-ADR-0068.
    workspace_id: uuid.UUID | None = None
    # Version of the payload schema in the event catalog (CP-ADR-0068).
    schema_version: int = 1
    payload: dict[str, Any]
    occurred_at: datetime


class BootstrapOut(ApiModel):
    tenant: TenantOut
    admin_principal: PrincipalOut
    api_key: ApiKeyCreatedOut
    iam_binding: IamBindingOut | None = None


class ClaimWithTaskOut(ApiModel):
    claim: ClaimOut
    task: TaskOut


class PageOut(ApiModel):
    items: list[dict[str, Any]]
    next_cursor: str | None


class EventPageOut(ApiModel):
    """Journal page: ``nextCursor`` is always present (echoes the input when
    nothing new is stable) so followers can poll without decoding cursors.
    ``prevCursor`` is the ``before`` of the preceding page; ``null`` on a
    backward read (``before``/``tail``) means the start of the journal."""

    items: list[dict[str, Any]]
    next_cursor: str
    prev_cursor: str | None
    has_more: bool


class EventTypeVersionOut(ApiModel):
    changes: str | None = Field(
        default=None, description="What the version added to the previous one; absent for v1"
    )
    payload_schema: dict[str, Any] = Field(
        alias="schema", description="JSON Schema 2020-12 of the payload of this version"
    )


class EventTypeOut(ApiModel):
    """A type of the core's event journal as ``docs/events/catalog.json`` declares it,
    plus what a console groups and labels it by (CP-ADR-0068, amendment of 2026-10-04)."""

    type: str
    group: str = Field(description="The prefix of the type before its first dot: a types= filter")
    entity_type: str
    description: str = Field(description="The caption of the type in the language of `locale`")
    label_key: str = Field(
        description="event.<type>: the key of the console's dictionary for its own caption"
    )
    current_version: int = Field(description="The version a new event of the type is written with")
    supported_versions: list[int] = Field(
        description="Every version the journal may hold, ascending; a consumer reads them all"
    )
    versions: dict[str, EventTypeVersionOut] = Field(description="By the version as a string")


class EventTypeListOut(ApiModel):
    locale: str = Field(description="The language of the captions: the one asked or en")
    items: list[EventTypeOut] = Field(description="Every type of the catalog, by name; no pages")


class ErrorDetail(ApiModel):
    code: str
    message: str
    details: dict[str, Any]
    request_id: str


class ErrorEnvelope(ApiModel):
    error: ErrorDetail


def dump[M: ApiModel](model_cls: type[M], obj: Base, **extra: Any) -> dict[str, Any]:
    """Serialize an ORM object through its response schema to a JSON-safe dict."""
    model = model_cls.model_validate(obj, from_attributes=True)
    data = model.model_dump(mode="json", by_alias=True)
    data.update(extra)
    return data


def page_body(items: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "nextCursor": next_cursor}


# --- v0.2 Organization Model --------------------------------------------------


_SLUG_FIELD = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")


class WorkspaceCreateRequest(ApiModel):
    slug: str = _SLUG_FIELD
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parent_id: uuid.UUID | None = None
    # v0.5: node type; omitted means the tenant's system default type.
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class WorkspaceUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    slug: str | None = Field(
        default=None, min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    custom_fields: dict[str, Any] | None = None
    # CP-ADR-0008 amendment 2026-10-03 (A1): keys of the task types allowed
    # here. Explicit null inherits from the ancestors, [] allows none.
    task_types: (
        list[Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]*$")]]
        | None
    ) = Field(
        default=None,
        max_length=100,
        description="Keys of the task types allowed in this workspace; null inherits "
        "from the nearest ancestor that sets them (every type when none does), [] allows none",
    )

    @field_validator("task_types")
    @classmethod
    def _task_types_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("items must be unique")
        return value


class WorkspaceMoveRequest(ApiModel):
    new_parent_id: uuid.UUID | None = None


class WorkspaceMemberRequest(ApiModel):
    principal_id: uuid.UUID


class RoleCreateRequest(ApiModel):
    slug: str = _SLUG_FIELD
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    workspace_id: uuid.UUID | None = None


class RoleUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class RoleAssignRequest(ApiModel):
    role_id: uuid.UUID
    workspace_id: uuid.UUID | None = None


class ScopeRevokeRequest(ApiModel):
    workspace_id: uuid.UUID | None = None


class CapabilityCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class CapabilityAssignRequest(ApiModel):
    capability_id: uuid.UUID
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillRegisterRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="1.0.0", min_length=1, max_length=50)
    description: str = ""
    # Optional with a contract: then it is contract.implementation.protocol.
    protocol: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    # ADR-0056 §1: contract v1; without it the version is a catalog entry only.
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None


class SkillUpdateRequest(ApiModel):
    description: str | None = None
    config: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    status: str | None = None
    # The one mutable part of a published contract: implementation.endpoint
    # (ADR-0056, amendment 2026-09-29).
    endpoint: str | None = Field(default=None, min_length=1, max_length=2000)


class SkillInvokeRequest(ApiModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    run_id: uuid.UUID | None = None
    # Basis for an external_write skill (ADR-0056 §4): an approved gate
    # approval on the same task.
    approval_id: uuid.UUID | None = None


class SkillInvocationClaimRequest(ApiModel):
    protocols: list[str] = Field(min_length=1, max_length=10)
    local_entrypoints: list[str] = Field(default_factory=list, max_length=500)
    # What the executor may reach and which tokens it issues (ADR-0056
    # amendment M2.2, D): http/mcp calls outside them are never handed out.
    http_origins: list[str] = Field(default_factory=list, max_length=100)
    mcp_endpoints: list[str] = Field(default_factory=list, max_length=100)
    audiences: list[str] = Field(default_factory=list, max_length=100)
    session_id: uuid.UUID | None = None
    lease_seconds: int | None = Field(default=None, gt=0)
    # Take this invocation and no other (the executor of an execution-typed
    # task claims the call its own run created).
    invocation_id: uuid.UUID | None = None


class SkillInvocationCancelRequest(ApiModel):
    reason: str = Field(default="cancelled", min_length=1, max_length=500)


class SkillInvocationHeartbeatRequest(ApiModel):
    fencing_token: int
    lease_seconds: int | None = Field(default=None, gt=0)
    # Required when the lease was claimed under a session (ADR-0056 amendment).
    session_id: uuid.UUID | None = None


class SkillInvocationCompleteRequest(ApiModel):
    fencing_token: int
    output: dict[str, Any]
    cost: dict[str, Any] | None = None
    session_id: uuid.UUID | None = None


class SkillInvocationError(ApiModel):
    code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    message: str = Field(default="", max_length=4000)
    retryable: bool = False
    details: dict[str, Any] | None = None


class SkillInvocationFailRequest(ApiModel):
    fencing_token: int
    error: SkillInvocationError
    session_id: uuid.UUID | None = None


class SkillAssignRequest(ApiModel):
    skill_id: uuid.UUID
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationCreateRequest(ApiModel):
    to_task: str = Field(min_length=1, max_length=100)
    type: str


class RunStartRequest(ApiModel):
    claim_id: uuid.UUID
    fencing_token: int
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_duration_seconds: int | None = Field(default=None, gt=0)
    max_actions: int | None = Field(default=None, gt=0)
    # CP-ADR-0073 §7: the agent revision the executor runs by. Required from a
    # principal linked to an agent, refused from anyone else [plan D005].
    agent_revision_id: uuid.UUID | None = None


class RunSucceedRequest(ApiModel):
    output: dict[str, Any] | None = None
    complete_task: bool = True


class RunFailRequest(ApiModel):
    failure_reason: str = Field(default="failed", min_length=1, max_length=2000)
    output: dict[str, Any] | None = None


class RunCancelRequest(ApiModel):
    reason: str = Field(default="cancelled", min_length=1, max_length=500)


class RunSuspendRequest(ApiModel):
    reason: str = Field(default="waiting_approval", min_length=1, max_length=200)
    waiting_for_approval_id: uuid.UUID | None = None


class HandoffCheckpointData(ApiModel):
    summary: str = Field(min_length=1, max_length=10_000)
    next_steps: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class HandoffCheckpointRequest(ApiModel):
    kind: str = Field(default="handoff", pattern=r"^handoff$")
    data: HandoffCheckpointData


class RunHandoffRequest(ApiModel):
    reason: str = Field(default="human_harness_handoff", pattern=r"^human_harness_handoff$")
    checkpoint: HandoffCheckpointRequest


class RunRequestCancelRequest(ApiModel):
    reason: str = Field(default="", max_length=500)


class RunControlMessageCreateRequest(ApiModel):
    operation: str
    causal_position: str = Field(min_length=1, max_length=500)
    directive: str | None = Field(default=None, max_length=10_000)
    reason: str = Field(default="", max_length=2000)
    expected_run_version: int = Field(ge=1)


class RunControlMessageAcknowledgeRequest(ApiModel):
    status: str
    claim_id: uuid.UUID
    fencing_token: int
    expected_run_version: int = Field(ge=1)
    expected_message_version: int = Field(ge=1)
    safe_boundary: str | None = Field(default=None, max_length=500)
    reason: str = Field(default="", max_length=2000)


class ChildGrantRequest(ApiModel):
    """Requested ceiling. An omitted field inherits; ``[]`` grants nothing."""

    permissions: list[str] | None = Field(default=None, max_length=100)
    capabilities: list[str] | None = Field(default=None, max_length=100)
    skills: list[str] | None = Field(default=None, max_length=100)


class ChildRunLaunchRequest(ApiModel):
    correlation_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10_000)
    priority: str = "medium"
    workspace_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    grant: ChildGrantRequest | None = None
    cancellation_policy: str | None = None
    expires_in_seconds: int | None = Field(default=None, ge=1)


class ChildRunRevokeRequest(ApiModel):
    reason: str = Field(default="", max_length=500)
    cancel_child: bool = False


class CheckpointCreateRequest(ApiModel):
    kind: str = Field(min_length=1, max_length=100)
    data: dict[str, Any] = Field(default_factory=dict)


class RunActionCreateRequest(ApiModel):
    action: str = Field(min_length=1, max_length=200)
    status: str = "completed"
    skill: str | None = Field(default=None, max_length=250)
    external_reference: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunActionFinishRequest(ApiModel):
    status: str
    external_reference: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreateRequest(ApiModel):
    type: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    task: str | None = Field(default=None, max_length=100)
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    uri: str | None = Field(default=None, max_length=2000)
    content: dict[str, Any] | None = None
    # An upload of PUT /artifact-contents (CP-ADR-0072 §2); excludes uri and content.
    content_ref: str | None = Field(default=None, min_length=1, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    supersedes_artifact_id: uuid.UUID | None = None


class ArtifactPurgeContentRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class TaskCommentCreateRequest(ApiModel):
    """The author is NOT a field here: it comes from the credential (ADR-0050)."""

    body: str = Field(min_length=1, max_length=MAX_COMMENT_BODY_LENGTH)
    run_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None


class TaskCommentUpdateRequest(ApiModel):
    body: str = Field(min_length=1, max_length=MAX_COMMENT_BODY_LENGTH)


class AttentionFeedbackRequest(ApiModel):
    """A verdict on an item of the caller's attention list (CP-ADR-0071)."""

    verdict: Literal["useful", "not_needed"]
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalRequestRequest(ApiModel):
    task: str | None = Field(default=None, max_length=100)
    artifact_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    required_role_id: uuid.UUID | None = None
    assigned_principal_id: uuid.UUID | None = None
    comment: str = Field(default="", max_length=4000)
    gate: bool = False
    # Separation of duties (CP-ADR-0074 §7): principals the core refuses a
    # decision from, whoever asks.
    excluded_principals: list[uuid.UUID] | None = Field(
        default=None,
        max_length=100,
        description="Principals that may not decide this approval"
        " (403 separation_of_duties_violation on their decision)",
    )


class ApprovalDecisionRequest(ApiModel):
    comment: str | None = Field(default=None, max_length=4000)


class WorkspaceOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_id: uuid.UUID | None
    type_id: uuid.UUID
    slug: str
    name: str
    description: str
    custom_fields: dict[str, Any]
    task_types: list[str] | None = Field(
        default=None,
        description="Own setting: keys of the task types allowed here; null inherits "
        "(CP-ADR-0008, amendment 2026-10-03 A1)",
    )
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class WorkspaceDetailOut(WorkspaceOut):
    """One workspace with what its position in the tree makes of its settings."""

    effective_task_types: list[str] | None = Field(
        default=None,
        description="Task types allowed here after inheritance: the own taskTypes or "
        "those of the nearest ancestor that sets them; null allows every type",
    )


class WorkspaceMemberOut(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    created_at: datetime


class ParticipantRoleOut(ApiModel):
    role_id: uuid.UUID
    slug: str
    name: str
    role_workspace_id: uuid.UUID | None = Field(
        description="Workspace the role belongs to; null for a tenant-wide role"
    )
    assignment_workspace_id: uuid.UUID | None = Field(
        description="Scope of the assignment: this workspace, its ancestor, or null for tenant"
    )


class WorkspaceParticipantOut(ApiModel):
    """A participant of a workspace (``GET /workspaces/{id}/participants``,
    CP-ADR-0010 amendment): an explicit member, a holder of its roles, or both."""

    principal_id: uuid.UUID
    kind: str
    display_name: str
    status: str
    member: bool = Field(
        description="Explicit membership (GET /workspaces/{id}/members); false for a "
        "principal who only holds a role of the workspace"
    )
    roles: list[ParticipantRoleOut] = Field(
        description="Role assignments that count in this workspace; empty for a member "
        "without roles here"
    )


class PackageLinkOut(ApiModel):
    """The package that installed a catalog object (CP-ADR-0074 §11, amendment TASK-000904)."""

    key: str = Field(description="Key of the package (package.yaml -> metadata key)")
    version: str | None = Field(
        description="Version of the package whose installation last named the object;"
        " null for objects applied before versions were kept"
    )
    install_hash: str | None = Field(
        description="The installation: planHash of POST /packages:apply, or installHash"
        " the installer named in POST /packages:record"
    )
    installed_at: datetime


class PackageSettingsValuesOut(ApiModel):
    """The effective settings of the package of an agent or a skill (CP-ADR-0081 §8)."""

    package: str = Field(description="The key of the package")
    version: int = Field(description="The version of the values; 0 — nothing saved yet")
    schema_revision: int = Field(description="The active schema revision they were read by")
    values: dict[str, Any] = Field(description="The saved values over the defaults")


PACKAGE_FILTER_DESCRIPTION = (
    "Only the objects the package with this key installed (package.key of the items)"
)
# Every list and card of a catalog kind the core holds carries it (TASK-000904).
_PACKAGE_LINK_FIELD = Field(
    default=None,
    description="The package that installed the object (the key, all its versions);"
    " null for an object created by hand. Filter a list with ?package=<key>",
)


class RoleOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    slug: str
    name: str
    description: str
    version: int
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class CapabilityOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str
    created_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class SkillOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    version: str
    description: str
    protocol: str
    config: dict[str, Any]
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None
    status: str
    row_version: int
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class SkillExecutionOut(ApiModel):
    """What an executor needs to run a claimed call — the contract, nothing else.

    The catalog ``config`` (addresses, headers) stays behind ``org.read``
    (ADR-0056 amendment, item 8); ``skills.execute`` alone does not reveal it.
    """

    id: uuid.UUID
    name: str
    version: str
    protocol: str
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None


class SkillInvocationOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    skill_id: uuid.UUID
    inputs: dict[str, Any]
    requested_by_kind: str = Field(exclude=True)
    requested_by_ref: str = Field(exclude=True)
    authority_principal_id: uuid.UUID
    authorization_basis: dict[str, Any] | None
    idempotency_key: str | None
    status: str
    attempt: int
    max_attempts: int
    available_at: datetime
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    cost: dict[str, Any] | None
    fencing_token: int
    executor_principal_id: uuid.UUID | None
    executor_session_id: uuid.UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskRelationOut(ApiModel):
    id: uuid.UUID
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID
    relation_type: str = Field(serialization_alias="type")
    created_by_principal_id: uuid.UUID
    created_at: datetime


class RunOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    claim_id: uuid.UUID
    principal_id: uuid.UUID
    session_id: uuid.UUID
    fencing_token: int
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    failure_reason: str | None
    cancel_requested_at: datetime | None
    cancel_requested_by: uuid.UUID | None
    max_duration_seconds: int | None
    max_actions: int | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    # CP-ADR-0066: the executor instructions the run was started under.
    instructions_hash: str | None = None
    instructions_refs: dict[str, Any] | None = None
    # CP-ADR-0073 §7: the agent revision the run went by; null for executors
    # that are not registered agents.
    agent_revision_id: uuid.UUID | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class CheckpointOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    created_by_principal_id: uuid.UUID
    seq: int
    kind: str
    data: dict[str, Any]
    created_at: datetime


class RunActionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    principal_id: uuid.UUID
    session_id: uuid.UUID | None
    skill_id: uuid.UUID | None
    seq: int
    action: str
    status: str
    external_reference: str | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime


class RunControlMessageOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    seq: int
    operation: str
    status: str
    causal_position: str
    directive: str | None
    reason: str
    safe_boundary: str | None
    idempotency_key: str
    requested_by_principal_id: uuid.UUID
    acknowledged_by_principal_id: uuid.UUID | None
    request_id: str
    correlation_id: str
    causation_id: str | None
    version: int
    accepted_at: datetime
    resolved_at: datetime | None


class RunControlMessageResultOut(ApiModel):
    control_message: RunControlMessageOut
    run_version: int


class ArtifactOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_by_principal_id: uuid.UUID
    type: str
    name: str
    uri: str | None
    content: dict[str, Any] | None
    supersedes_artifact_id: uuid.UUID | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    # Content in the store (CP-ADR-0072 §1): none | stored | purged; the other
    # fields are null without stored (or once stored) content.
    content_state: str
    size_bytes: int | None
    media_type: str | None
    sha256: str | None
    # Version of the registered artifact type it was checked against
    # (CP-ADR-0072 §6); null for an unregistered type.
    type_version: int | None
    created_at: datetime


class ArtifactContentOut(ApiModel):
    """An upload waiting for an artifact to reference it (CP-ADR-0072 §2)."""

    content_ref: str
    size_bytes: int
    media_type: str
    sha256: str
    expires_at: datetime


class TaskCommentAuthorOut(ApiModel):
    """Who wrote a comment, in words (ADR-0050, amendment of 2026-09-30).

    Part of the thread and read with it (``tasks.read``): a reader of the
    discussion tells an owner from an agent without ``principals.read``.
    """

    kind: str
    display_name: str


class TaskCommentOut(ApiModel):
    """One reply in a work item's thread (ADR-0050)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    # Derived from the authenticated context at write time, never from a body.
    author_principal_id: uuid.UUID
    # The author's current kind and display name, looked up at read time.
    author: TaskCommentAuthorOut
    body: str
    run_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    edited_at: datetime | None


class TaskCommentRevisionOut(ApiModel):
    """A superseded version of a comment — the audit trail of an edit."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    comment_id: uuid.UUID
    task_id: uuid.UUID
    version: int
    body: str
    author_principal_id: uuid.UUID
    created_at: datetime
    superseded_at: datetime
    superseded_by: uuid.UUID


class ApprovalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    task_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    requested_by_principal_id: uuid.UUID
    status: str
    gate: bool
    required_role_id: uuid.UUID | None
    assigned_principal_id: uuid.UUID | None
    decision_by_principal_id: uuid.UUID | None
    decision_at: datetime | None
    comment: str
    version: int
    # null | pending | deferred | executed | failed (CP-ADR-0061)
    outcome_status: str | None
    excluded_principals: list[uuid.UUID] = Field(
        default_factory=list,
        description="Principals that may not decide this approval (CP-ADR-0074 §7)",
    )
    created_at: datetime
    updated_at: datetime


class ApprovalOutcomeActionOut(ApiModel):
    index: int
    action: str
    status: str
    attempts: int
    result: dict[str, Any]
    error: dict[str, Any] | None
    # A reaction: the index of the invokeSkill it reacts to, and to which
    # ending (onSuccess | onFailure).
    reacts_to: int | None = None
    when: str | None = None


class ApprovalOutcomeOut(ApiModel):
    """A decision's declared outcome and what happened to each action."""

    approval_id: uuid.UUID
    outcome: str | None
    outcome_status: str | None
    # Attempts that died of an unexpected error, the last such error and when
    # the worker looks again (pending/deferred only).
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    actions: list[ApprovalOutcomeActionOut]


# --- v0.5 Project Model -------------------------------------------------------

_TYPE_KEY_FIELD = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]*$")


def _unique_items(items: list[str]) -> list[str]:
    if len(set(items)) != len(items):
        raise ValueError("items must be unique")
    return items


class WorkspaceTypeCreateRequest(ApiModel):
    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_child_types: list[str] | None = Field(default=None, max_length=100)


class WorkspaceTypeUpdateRequest(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    field_schema: dict[str, Any] | None = None
    allowed_child_types: list[str] | None = Field(default=None, max_length=100)


class WorkspaceTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    display_name: str
    description: str
    field_schema: dict[str, Any]
    allowed_child_types: list[str]
    is_system: bool
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class ProjectTemplateCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`."""

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    lifecycle_schema: dict[str, Any] | None = None
    default_config: dict[str, Any] = Field(default_factory=dict)
    default_views: list[Any] = Field(default_factory=list, max_length=50)
    governance_schema: dict[str, Any] = Field(default_factory=dict)
    memory_defaults: dict[str, Any] = Field(default_factory=dict)


class ProjectTemplateOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    field_schema: dict[str, Any]
    lifecycle_schema: dict[str, Any]
    default_config: dict[str, Any]
    default_views: list[Any]
    governance_schema: dict[str, Any]
    memory_defaults: dict[str, Any]
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class TaskTypeCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`."""

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    lifecycle_schema: dict[str, Any] | None = None
    # ADR-0056 §3: {skill, version, inputs} — tasks of this type are executed
    # by one skill invocation.
    execution: dict[str, Any] | None = None
    # CP-ADR-0061: outcomes of a decided gate approval on a task of this version.
    approval_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0064: where a task of this version takes its knowledge context from.
    context_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0066: how to execute a task of this version, Markdown up to 16 KiB
    # (size and credentials are checked by the command, with a stable code).
    instructions: str = ""
    # CP-ADR-0061 amendment 2026-09-25: work core files once a task of this
    # version is completed ({"onComplete": {"when", "actions"}}).
    completion_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0072 §7: artifacts a task of this version takes in and hands on
    # ({"inputs": [...], "outputs": [...]}); checked against the registry.
    artifact_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0067 amendment 2026-09-27 (B5): checks every task of this version
    # passes, after the required outputs and before the task's own acceptance.
    acceptance: list[AcceptanceCheckSpec] = Field(default_factory=list, max_length=50)
    # CP-ADR-0048 amendment 2026-10-03 (A1): slugs of the roles a person needs
    # to take work of this version; empty does not restrict people.
    executor_roles: Annotated[
        list[Annotated[str, _SLUG_FIELD]],
        Field(max_length=20),
        AfterValidator(_unique_items),
    ] = Field(default_factory=list)


class TaskTypeMigratedTaskOut(ApiModel):
    task_id: uuid.UUID
    public_id: str
    from_status: str
    status: str
    version: int


class TaskTypeSkippedTaskOut(ApiModel):
    """A task left where it was, with the refusal a single migration would give."""

    task_id: uuid.UUID
    code: str
    message: str
    details: dict[str, Any]


class TaskTypeMigrateTasksOut(ApiModel):
    """One page of ``POST /task-types/{id}:migrate-tasks`` (ADR-0048)."""

    type_key: str
    from_type_version: int
    type_version: int
    type_id: uuid.UUID
    migrated: list[TaskTypeMigratedTaskOut]
    skipped: list[TaskTypeSkippedTaskOut]
    next_cursor: str | None


class TaskTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    field_schema: dict[str, Any]
    lifecycle_schema: dict[str, Any]
    execution: dict[str, Any] | None = None
    approval_schema: dict[str, Any]
    context_schema: dict[str, Any]
    instructions: str = ""
    completion_schema: dict[str, Any] = Field(default_factory=dict)
    artifact_schema: dict[str, Any] = Field(default_factory=dict)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    executor_roles: list[str] = Field(default_factory=list)
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class TaskTypeExecutorOut(ApiModel):
    """Who may take a task type in a workspace (CP-ADR-0048 amendment 2026-10-03, A2)."""

    principal_id: uuid.UUID
    kind: str = Field(description="human, agent or service")
    display_name: str
    roles: list[str] = Field(
        description="role: the executor roles of the type the person holds here; any: the "
        "person's roles from participants; agents and services: empty"
    )
    reason: Literal["role", "any", "agent_task_types", "agent_any"] = Field(
        description="role: holds an executor role of the type; any: the type has no executor "
        "roles; agent_task_types: spec.work.taskTypes names the type; agent_any: the agent "
        "takes every type"
    )


class TaskTypeExecutorsOut(ApiModel):
    items: list[TaskTypeExecutorOut]


class ArtifactTypeCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`.

    ``mediaTypes`` and ``maxBytes`` are checked by the command, so an empty
    list or a ceiling above ``CP_ARTIFACT_MAX_BYTES`` is ``invalid_artifact_type``
    like every other defect of the definition (CP-ADR-0072 §6).
    """

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    media_types: list[Any]
    # Omitted: the global ceiling at the time of creation.
    max_bytes: int | None = None


class ArtifactTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    metadata_schema: dict[str, Any]
    media_types: list[str]
    max_bytes: int
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


# --- connection types (CP-ADR-0079 §2) -----------------------------------------------------

_CONNECTION_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class ConnectionTypeOAuth2(ApiModel):
    authorize_url: str = Field(
        max_length=2000, pattern=r"^https://", json_schema_extra={"format": "uri"}
    )
    token_url_template: str = Field(
        max_length=2000,
        pattern=r"^https://",
        description="The only placeholder is {account}; the host is an external DNS name"
        " (two or more labels, the last one not numeric, no port, no userinfo)",
    )
    account_param: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="The callback parameter that names the account;"
        " required when tokenUrlTemplate names {account}",
    )
    auth_style: Literal["in_params", "in_header"] = Field(
        description="How the client id and secret go to the exchange address"
        " (provider_options.auth_style of the plugin)"
    )
    scopes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(max_length=50)


class ConnectionTypeAccountField(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    pattern: str = Field(
        min_length=1,
        max_length=500,
        description="A regular expression the whole account matches",
    )


class ConnectionTypeSpec(ApiModel):
    """``spec`` of the catalog kind ``ConnectionType``; ``null`` in a response — not set."""

    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    auth: list[Literal["oauth2", "token"]] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    oauth2: ConnectionTypeOAuth2 | None = Field(
        default=None, description="Required when auth names oauth2"
    )
    account_field: ConnectionTypeAccountField | None = Field(
        default=None,
        description="Required when auth names token or tokenUrlTemplate names {account}",
    )
    settings_schema: dict[str, Any] = Field(
        description="JSON Schema (draft 2020-12, at most 64 KiB, root type: object) of the"
        " connection's non-secret settings"
    )
    default_key: str = Field(pattern=_CONNECTION_KEY_PATTERN)


class ConnectionTypePublishRequest(ApiModel):
    key: str = Field(pattern=_CONNECTION_KEY_PATTERN)
    version: int = Field(ge=1, le=999_999_999)
    # Checked by the core, not by the request's shape: a violation of the table
    # of CP-ADR-0079 §2 is 422 invalid_connection_type with details.field, an
    # unknown field 400 invalid_request.
    spec: Annotated[
        dict[str, Any],
        WithJsonSchema({"allOf": [{"$ref": "#/components/schemas/ConnectionTypeSpec"}]}),
    ]


class ConnectionTypeUpdateRequest(ApiModel):
    status: Literal["active", "deprecated", "disabled"]


class ConnectionTypeOut(ApiModel):
    id: uuid.UUID
    key: str
    version: int
    status: Literal["active", "deprecated", "disabled"]
    spec: ConnectionTypeSpec
    spec_hash: str = Field(description="sha256 of the canonical JSON of spec")
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD
    created_by: uuid.UUID
    created_at: datetime
    row_version: int = Field(description='ETag "connection-type-<rowVersion>"')


# --- connections (CP-ADR-0079 §3) ------------------------------------------------------------

ConnectionStatusValue = Literal["pending", "active", "expired", "revoked"]
_REASON_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class ConnectionCreateRequest(ApiModel):
    key: str | None = Field(
        default=None,
        pattern=_CONNECTION_KEY_PATTERN,
        description="Default: defaultKey of the latest active version of the type",
    )
    type: str = Field(min_length=1, max_length=63)
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Default: displayName of the type",
    )
    settings: dict[str, Any] | None = Field(
        default=None, description="By settingsSchema of the type version; default {}"
    )


class ConnectionUpdateRequest(ApiModel):
    """At least one field; a field sent is a value, never ``null``."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = Field(
        default=None,
        description="Replaced whole; checked by the schema of the type version after the edit",
    )
    type_version: int | None = Field(
        default=None,
        ge=1,
        le=999_999_999,
        description="A published version of the same type that is not disabled",
    )

    @model_validator(mode="after")
    def _one_field_and_no_nulls(self) -> "ConnectionUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one of displayName, settings, typeVersion is required")
        nulls = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if nulls:
            raise ValueError(f"fields must not be null: {', '.join(to_camel(n) for n in nulls)}")
        return self

    def changes(self) -> dict[str, Any]:
        """The fields sent, by their API names."""
        return self.model_dump(by_alias=True, exclude_unset=True)


class ConnectionStatusReport(ApiModel):
    """``PUT /connections/{key}/status``: the connector's report as of ``checkedAt``."""

    status: Literal["active", "expired"]
    reason: str | None = Field(
        default=None,
        pattern=_REASON_CODE_PATTERN,
        description="A code; required when an active connection expired",
    )
    message: str | None = Field(
        default=None,
        max_length=500,
        description="Stored without anything shaped like credentials",
    )
    checked_at: AwareDatetime


class ConnectionOut(ApiModel):
    id: uuid.UUID
    key: str
    type: str
    type_version: int
    display_name: str
    account: str | None
    auth: Literal["oauth2", "token"] | None
    status: ConnectionStatusValue
    status_reason: str | None
    status_message: str | None
    settings: dict[str, Any]
    secret_ref: str | None = Field(description="The path of the material in the secret store")
    expires_at: datetime | None
    connected_by: uuid.UUID | None
    connected_at: datetime | None
    last_checked_at: datetime | None
    agents: list[str] | None = Field(
        default=None,
        description="Only in GET /connections/{key}: keys of the agents whose current"
        " revision names the connection",
    )
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    version: int = Field(description='ETag "connection-<version>"')


# --- access to connections (CP-ADR-0079 §5, §6, §7) --------------------------------------------


class OAuthAppSetRequest(ApiModel):
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
        description="Goes to the secret store in transit; never stored or answered by the core",
    )


class OAuthAppOut(ApiModel):
    type: str
    configured: bool
    client_id: str | None
    updated_at: datetime | None


class ConnectionAuthorizeRequest(ApiModel):
    """``{}``: the connection and the caller say everything."""


class ConnectionAuthorizeOut(ApiModel):
    authorize_url: str | None = Field(
        description="Where to send the person for consent; null only in the replay of an"
        " Idempotency-Key — the state is kept hashed and is not issued twice"
    )
    expires_at: datetime


class ConnectionTokenRequest(ApiModel):
    account: str = Field(
        min_length=1,
        max_length=253,
        description="The account in the external system; matches accountField.pattern",
    )
    token: str = Field(
        min_length=1,
        max_length=8192,
        json_schema_extra={"writeOnly": True},
        description="Goes to the secret store in transit; never stored or answered by the core",
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="When the key expires; in the future"
    )


class ConnectionRevokeRequest(ApiModel):
    """``POST /connections/{key}:revoke``: an optional reason, kept as the status message."""

    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why the connection is revoked; stored without anything shaped like"
        " credentials",
    )


class AgentConnectionOut(ApiModel):
    """What an agent learns of a connection its current revision names (CP-ADR-0079 §8)."""

    key: str
    type: str
    type_version: int
    account: str | None
    auth: Literal["oauth2", "token"] | None
    status: ConnectionStatusValue
    settings: dict[str, Any]
    secret_ref: str | None = Field(
        description="The path to read in the secret store with the agent's own token; not a value"
    )
    expires_at: datetime | None


class AgentConnectionListOut(ApiModel):
    items: list[AgentConnectionOut]


class AgentSecretSetRequest(ApiModel):
    """``PUT /agents/{key}/secrets/{name}``: the value, in transit to the secret store."""

    value: str = Field(
        min_length=1,
        max_length=65_536,
        json_schema_extra={"writeOnly": True},
        description="Goes to the secret store in transit; never stored or answered by the core",
    )


class AgentSecretOut(ApiModel):
    """One secret of an agent: the name and who set it last — never the value (§11)."""

    name: str
    updated_at: datetime
    updated_by: uuid.UUID


class AgentSecretListOut(ApiModel):
    items: list[AgentSecretOut]


class ProjectCreateRequest(ApiModel):
    """Attach to an existing workspace, or create workspace + profile at once."""

    workspace_id: uuid.UUID | None = None
    workspace_slug: str | None = Field(
        default=None, min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    workspace_name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_workspace_id: uuid.UUID | None = None
    workspace_type_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_id: uuid.UUID | None = None
    template_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_version: int | None = Field(default=None, ge=1)
    status_key: str | None = Field(default=None, min_length=1, max_length=64)
    owner_principal_id: uuid.UUID | None = None
    start_date: datetime | None = None
    target_date: datetime | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(ApiModel):
    owner_principal_id: uuid.UUID | None = None
    clear_owner: bool = False
    start_date: datetime | None = None
    target_date: datetime | None = None
    custom_fields: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    template_id: uuid.UUID | None = None
    template_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_version: int | None = Field(default=None, ge=1)


class ProjectTransitionRequest(ApiModel):
    status_key: str = Field(min_length=1, max_length=64)
    comment: str = Field(default="", max_length=1000)


class ProjectOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    template_id: uuid.UUID
    status_key: str
    system_status_category: str
    owner_principal_id: uuid.UUID | None
    start_date: datetime | None
    target_date: datetime | None
    custom_fields: dict[str, Any]
    settings: dict[str, Any]
    active_config_revision_id: uuid.UUID | None
    status: str
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    # Derived at read time from the workspace tree and the template.
    parent_project_id: uuid.UUID | None = None
    template_key: str | None = None
    template_version: int | None = None
    active_config_revision: int | None = None


class ConfigRevisionCreateRequest(ApiModel):
    config: dict[str, Any] = Field(default_factory=dict)
    comment: str = Field(default="", max_length=1000)


class ConfigRevisionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    revision: int
    config: dict[str, Any]
    validation: dict[str, Any]
    comment: str
    created_by: uuid.UUID
    created_at: datetime
    activated_at: datetime | None


class ExternalReferenceCreateRequest(ApiModel):
    external_system: str = Field(min_length=1, max_length=512)
    external_type: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalReferenceRegisterRequest(ExternalReferenceCreateRequest):
    """Generic registration: the entity is named in the body, not in the path.

    ``entityId`` is a reference rather than a strict UUID — a task may be named
    by its public id, which is what an importer carrying legacy identifiers has
    in hand.
    """

    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=128)


class ExternalReferenceOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    external_system: str
    external_type: str
    external_id: str
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AdapterRedriveRequest(ApiModel):
    reason: str = Field(default="operator_redrive", min_length=1, max_length=500)


class AdapterRebuildRequest(ApiModel):
    cursor: str | None = None
    reason: str = Field(default="operator_rebuild", min_length=1, max_length=500)


class JournalArchiveRequest(ApiModel):
    """Move (or delete) journal history up to a safe horizon."""

    before_seconds: int | None = Field(default=None, ge=0)
    max_events: int | None = Field(default=None, ge=1, le=100_000)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Malformed request"},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials"},
    403: {"model": ErrorEnvelope, "description": "Insufficient permissions"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    409: {"model": ErrorEnvelope, "description": "Concurrency conflict"},
    422: {"model": ErrorEnvelope, "description": "Domain validation failed"},
}

# Routes that reach the secret store (CP-ADR-0079 §1): not configured, not
# reachable or sealed is 503 secret_store_unavailable, and nothing changed.
SECRET_STORE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    503: {"model": ErrorEnvelope, "description": "Secret store unavailable; nothing changed"},
}


# --- M1.1 work graph: goals (CP-ADR-0062) -------------------------------------


class GoalCreateRequest(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    desired_state: str = Field(default="", max_length=10_000)
    criteria: list[AcceptanceCheckSpec] = Field(default_factory=list, max_length=50)
    owner_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    parent_goal_id: uuid.UUID | None = None
    # Omitted: derived from the writer's principal kind (human / harness).
    created_from: WorkOriginSpec | None = None


class GoalUpdateRequest(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    desired_state: str | None = Field(default=None, max_length=10_000)
    criteria: list[AcceptanceCheckSpec] | None = Field(default=None, max_length=50)
    # null clears the owner / detaches from the parent goal.
    owner_id: uuid.UUID | None = None
    status: Literal["active", "achieved", "abandoned"] | None = None
    parent_goal_id: uuid.UUID | None = None


class GoalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    title: str
    desired_state: str
    criteria: list[dict[str, Any]]
    owner_id: uuid.UUID | None
    status: str
    created_from: dict[str, Any]
    parent_goal_id: uuid.UUID | None
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


# --- M1.3 work derivation rules (CP-ADR-0063) -----------------------------------
# The four documents are typed loosely here on purpose: their grammar (a
# closed expression language, templates, cross-document rules) is validated by
# domain/work_rules.py, which also serves writers that do not pass through HTTP.


class RuleIdentitySpec(ApiModel):
    """Whose authority a rule acts with (CP-ADR-0063 amendment 2026-09-27, G1):
    an agent of the registry, by key."""

    agent: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")


class RuleCreateRequest(ApiModel):
    key: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    workspace_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    trigger: dict[str, Any]
    # Omitted: always true.
    condition: dict[str, Any] | bool | None = None
    interpretation: dict[str, Any] | None = None
    action: dict[str, Any]
    status: Literal["enabled", "disabled"] = "enabled"
    # Omitted: the rule acts with the authority of whoever enabled it.
    identity: RuleIdentitySpec | None = None


class RuleUpdateRequest(ApiModel):
    description: str | None = Field(default=None, max_length=2000)
    trigger: dict[str, Any] | None = None
    condition: dict[str, Any] | bool | None = None
    # null removes the interpretation; goalId null unlinks the goal.
    interpretation: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    goal_id: uuid.UUID | None = None
    # null removes the identity: the rule acts with its enabler's authority.
    identity: RuleIdentitySpec | None = None


class RuleOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    key: str
    description: str
    version: int
    status: str
    trigger: dict[str, Any]
    condition: Any
    interpretation: dict[str, Any] | None
    action: dict[str, Any]
    # {agent: <key>} when the rule acts as an agent of the registry, else null.
    identity: dict[str, Any] | None = None
    # Whose authority the rule acts with, and since when it sees facts.
    authority_principal_id: uuid.UUID | None
    enabled_at: datetime | None
    next_run_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class RuleEvaluationOut(ApiModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    rule_version: int
    trigger_ref: str
    trigger_event_id: uuid.UUID | None
    status: str
    result: dict[str, Any]
    evidence: list[Any]
    skill_invocation_id: uuid.UUID | None
    created_task_ids: list[Any]
    error: dict[str, Any] | None
    next_check_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- declarative agents: registry (CP-ADR-0073) ------------------------------
#
# The contract lands before the implementation (constitution art. V): the
# routes answer 501 until declarative-agents D005. ``AgentSpec`` describes the
# same object as ``$defs.agentSpec`` of the package-sdk catalog schema
# (``schema/v1/object.schema.json``); a contract test keeps the two
# together. The core validates the shape of every section but two: the executor
# parameters (``$defs.agentExecutors``) and the working copy
# (``$defs.agentWorkingCopies``) are data of the executor kind and pass through
# uninterpreted (TAI-ADR-0063); the core only looks for secret material in them.

AGENT_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_AGENT_KEY_FIELD = Field(min_length=1, max_length=63, pattern=AGENT_KEY_PATTERN)
_AGENT_REF = Annotated[str, Field(min_length=1, max_length=500)]
_AGENT_NODE_LABEL = Annotated[
    str, Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9.-]*(=[a-zA-Z0-9._-]+)?$")
]
_AGENT_SECRET_NAME = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
_AGENT_CONNECTION_KEY = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
AGENT_MAX_CONNECTIONS = 20
# Id of the tenant's object, or ${VARIABLE} of the installation.
_AGENT_TOPOLOGY = Annotated[str, Field(min_length=1, max_length=200)]
_AGENT_ITEMS = 200
AGENT_INSTRUCTIONS_MAX_CHARS = 65_536
AGENT_MAX_REPLICAS = 20

AgentStateValue = Literal["running", "stopped"]
AgentPhaseValue = Literal[
    "pending", "running", "waiting_for_node", "crash_looping", "node_unavailable", "stopped"
]


# An IAM scope: ``<audience>:<action>``, as ``control-plane:read`` or
# ``iam:identities.link``. Names are separated by ``:``, segments of a name by
# ``.``; a segment is lowercase letters, digits, ``_`` and ``-``, and starts
# with a letter or a digit (IAM audience keys may start with a digit).
_AGENT_IAM_SCOPE = Annotated[
    str,
    Field(
        max_length=200,
        pattern=(
            r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*"
            r"(:[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*)+$"
        ),
    ),
]


class AgentIamSpec(ApiModel):
    """The IAM part of the identity: data for whoever issues the account, not the core."""

    audiences: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=100)]],
        Field(min_length=1, max_length=20),
        AfterValidator(_unique_items),
    ]
    scope_ceiling: Annotated[
        list[_AGENT_IAM_SCOPE],
        Field(min_length=1, max_length=50),
        AfterValidator(_unique_items),
    ]


class AgentIdentitySpec(ApiModel):
    """Who the agent is: the core derives its principal and binding from this."""

    kind: Literal["agent", "service"]
    roles: list[Annotated[str, _SLUG_FIELD]] = Field(default_factory=list, max_length=_AGENT_ITEMS)
    permissions: list[Annotated[str, Field(pattern=r"^[a-z_]+(\.[a-z_]+)+$")]] = Field(
        min_length=1, max_length=_AGENT_ITEMS
    )
    capabilities: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=_AGENT_ITEMS
    )
    iam: AgentIamSpec | None = Field(
        default=None,
        description="Audiences and scope ceiling of the IAM account; stored, not interpreted",
    )


class AgentWorkSpec(ApiModel):
    """Which work the agent takes: the same selection a runner does today."""

    workspace: _AGENT_TOPOLOGY | None = None
    project: _AGENT_TOPOLOGY | None = None
    include_subprojects: bool = False
    only_assigned: bool = True
    task_types: list[Annotated[str, _TYPE_KEY_FIELD]] = Field(
        default_factory=list,
        max_length=_AGENT_ITEMS,
        description="Task type keys; empty means any type",
    )


# An OCI image reference with a tag or a digest (the ``distribution/reference``
# grammar with the implicit ``latest`` taken away): ``[registry[:port]/]path``
# then ``:tag``, ``@sha256:<hex>`` or both. ``@`` only precedes ``sha256:``, so
# a reference cannot carry credentials (CP-ADR-0073, Z1).
_OCI_HOST_PART = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
_OCI_REGISTRY = rf"{_OCI_HOST_PART}(?:\.{_OCI_HOST_PART})*"
_OCI_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_OCI_DIGEST = r"@sha256:[a-f0-9]{64}"
AGENT_IMAGE_PATTERN = (
    rf"^(?:{_OCI_REGISTRY}(?::[0-9]{{1,5}})?/)?"
    rf"{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*"
    rf"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}(?:{_OCI_DIGEST})?|{_OCI_DIGEST})$"
)


class AgentExecutorSpec(ApiModel):
    """How the agent executes. ``kind`` is a string, never a vendor enum (art. II)."""

    kind: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9-]*$")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters of the executor kind; checked by its schema, not by the core",
    )
    instructions: str = Field(default="", max_length=AGENT_INSTRUCTIONS_MAX_CHARS)
    image: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=AGENT_IMAGE_PATTERN,
        description=(
            "OCI image reference the placing node runs this agent from (tag or digest "
            "required). The node runs it only if its allow-list for the executor kind "
            "admits it; otherwise the placement reason is image_not_allowed. Absent: the "
            "node's default image of the kind. The core checks the form only."
        ),
    )


def _whole_number(value: Any) -> Any:
    """An integer as the catalog schema has it: ``2`` and ``2.0``, not ``0.5``, ``"2"`` or ``true``.

    The revision hash takes no floats (``domain/canonical.py``): a fraction is
    refused here, with the path of its field, instead of as ``non_canonical_value``
    of the whole spec (CP-ADR-0073, amendment 2026-10-03).
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a whole number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("must be a whole number: a fraction such as 0.5 is not accepted")
        return int(value)
    return value


# A number of the spec: every one is hashed with the revision, so none is a float.
_AgentWholeNumber = Annotated[int, BeforeValidator(_whole_number)]


# A pinned skill version the agent invokes: ``name@version``.
_AGENT_SKILL_REF = Annotated[str, Field(max_length=251, pattern=r"^[^@\s/]{1,200}@[^@\s/]{1,50}$")]


class AgentSkillsSpec(ApiModel):
    """Which skills the agent executes itself (CP-ADR-0056) and where they may go.

    ``invoke`` is the other direction: skill versions the agent calls through
    ``POST /skills/{ref}:invoke`` rather than hosts (amendment 2026-09-28).
    """

    protocols: list[Literal["local", "http", "mcp"]] = Field(default_factory=list)
    local: list[Annotated[str, Field(pattern=r"^[A-Za-z_][\w.]*(:[A-Za-z_]\w*)?$")]] = Field(
        default_factory=list,
        max_length=_AGENT_ITEMS,
        description="Allowed entry points or packages",
    )
    http_origins: list[_AGENT_REF] = Field(default_factory=list, max_length=_AGENT_ITEMS)
    mcp_origins: list[_AGENT_REF] = Field(default_factory=list, max_length=_AGENT_ITEMS)
    audiences: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=_AGENT_ITEMS,
        description="IAM audiences the skills get a token for",
    )
    concurrency: _AgentWholeNumber | None = Field(default=None, ge=1, le=32)
    invoke: Annotated[
        list[_AGENT_SKILL_REF],
        Field(max_length=_AGENT_ITEMS),
        AfterValidator(_unique_items),
    ] = Field(
        default_factory=list,
        description=(
            "Skill versions the agent calls through the core (name@version); "
            "the registry assigns them to its principal"
        ),
    )


class AgentResourcesSpec(ApiModel):
    cpus: _AgentWholeNumber | None = Field(
        default=None, ge=1, le=64, description="Whole CPUs; a fraction is refused"
    )
    memory_mb: _AgentWholeNumber | None = Field(default=None, ge=64, le=262_144)


class AgentPlacementSpec(ApiModel):
    """Where the agent may run. The core stores it; the placement service reads it."""

    requires: list[_AGENT_NODE_LABEL] = Field(
        default_factory=list,
        max_length=_AGENT_ITEMS,
        description="Labels a node must carry: name or name=value",
    )
    secrets: list[_AGENT_SECRET_NAME] = Field(
        default_factory=list,
        max_length=_AGENT_ITEMS,
        description="Names of node secrets; a value never reaches the core (FR-008)",
    )
    resources: AgentResourcesSpec | None = None
    replicas: _AgentWholeNumber = Field(
        default=1, ge=0, le=AGENT_MAX_REPLICAS, description="Desired state, not revision"
    )
    drain_seconds: _AgentWholeNumber = Field(default=14_400, ge=0, le=14_400)


class AgentSpec(ApiModel):
    """``spec`` of a catalog object of kind ``Agent`` (TAI-ADR-0052)."""

    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    identity: AgentIdentitySpec
    work: AgentWorkSpec | None = Field(
        default=None, description="Absent for identities that take no work (placement none)"
    )
    executor: AgentExecutorSpec | None = None
    working_copy: dict[str, Any] | None = Field(
        default=None,
        description="The working copy the executor daemon builds; stored as data, its shape is "
        "checked by the schema of the executor kind, not by the core (TAI-ADR-0063)",
    )
    skills: AgentSkillsSpec | None = None
    connections: Annotated[
        list[_AGENT_CONNECTION_KEY],
        Field(max_length=AGENT_MAX_CONNECTIONS),
        AfterValidator(_unique_items),
    ] = Field(
        default_factory=list,
        description=(
            "Keys of the tenant's connections whose material the agent may read"
            " (CP-ADR-0079 §8); a non-empty list needs connections.manage of whoever applies it"
        ),
    )
    placement: AgentPlacementSpec | Literal["none"] | None = Field(
        default=None, description="Absent means placed with the defaults; none means no process"
    )
    state: AgentStateValue = Field(default="running", description="Desired state, not revision")

    @field_validator("placement", mode="wrap")
    @classmethod
    def _placement_errors_name_the_field(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> Any:
        """An object is a placement, not ``none``: its errors carry the path of the field alone.

        Left to the union, ``placement.resources.cpus`` would come back as
        ``placement.AgentPlacementSpec.resources.cpus`` next to ``Input should be 'none'``.
        """
        if isinstance(value, dict):
            return handler(AgentPlacementSpec.model_validate(value))
        return handler(value)

    @model_validator(mode="after")
    def _placed_agent_executes(self) -> "AgentSpec":
        if self.placement != "none" and self.executor is None:
            raise ValueError("a placed agent needs an executor")
        return self


class AgentPackageRef(ApiModel):
    """The package whose apply published a revision, as its installer names it."""

    key: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)


class AgentPublishRequest(ApiModel):
    """``POST /agents`` and ``POST /agents:validate``: a catalog object without its envelope."""

    key: str = _AGENT_KEY_FIELD
    spec: AgentSpec
    package: AgentPackageRef | None = Field(
        default=None,
        description="Set by a package installer: a new revision records the package as its "
        "source; without it the revision is a manual edit. Not part of the spec hash",
    )


class AgentStateUpdateRequest(ApiModel):
    state: AgentStateValue | None = None
    replicas: int | None = Field(default=None, ge=0, le=AGENT_MAX_REPLICAS)

    @model_validator(mode="after")
    def _something_to_change(self) -> "AgentStateUpdateRequest":
        if self.state is None and self.replicas is None:
            raise ValueError("state or replicas is required")
        return self


class AgentRetireRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)


class AgentIdentityLinkRequest(IamIdentitySpec):
    """The IAM identity the placement service created for the agent (§6)."""


class AgentIdentityReplaceRequest(IamIdentitySpec):
    """A new IAM identity for a service agent (CP-ADR-0073, amendment 2026-09-30)."""

    reason: str = Field(min_length=1, max_length=500)


class AgentStatusReason(ApiModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(default="", max_length=500)


class AgentInstances(ApiModel):
    desired: int = Field(ge=0)
    ready: int = Field(ge=0)


class AgentStatusReport(ApiModel):
    """``PUT /agents/{key}/status``: what actually runs, as of ``observedAt``."""

    phase: AgentPhaseValue
    reason: AgentStatusReason | None = None
    observed_revision: int | None = Field(default=None, ge=1)
    node: str | None = Field(default=None, min_length=1, max_length=200)
    instances: AgentInstances
    observed_at: AwareDatetime


class AgentRevisionOut(ApiModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_key: str
    revision: int
    spec: dict[str, Any] = Field(
        description="The published spec as applied, without state and placement.replicas"
    )
    spec_hash: str = Field(description="sha256:<hex> of the canonical JSON of spec")
    created_by: uuid.UUID
    created_at: datetime


class AgentOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    display_name: str
    status: Literal["active", "retired"]
    state: AgentStateValue
    replicas: int
    current_revision: int
    revision: AgentRevisionOut = Field(
        description="The current revision, or the one addressed by key@revision"
    )
    principal_id: uuid.UUID | None = Field(
        description="Principal of the agent once its identity is linked (§6)"
    )
    workspace_id: uuid.UUID | None
    retired_at: datetime | None
    retired_by: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class AgentMeOut(AgentOut):
    """``GET /agents/me``: the caller's agent and the settings of its package."""

    package_settings: PackageSettingsValuesOut | None = Field(
        description="The effective settings of the package that installed the agent;"
        " null for an agent created by hand or a package without settings (CP-ADR-0081 §8)"
    )


class AgentValidationOut(ApiModel):
    """``POST /agents:validate`` on success; a failure is the error ``POST /agents`` returns."""

    key: str
    spec_hash: str
    current_revision: int | None
    would_create_revision: bool
    would_change_state: bool


class AgentStatusOut(ApiModel):
    agent_key: str
    phase: AgentPhaseValue | Literal["unknown"] = Field(
        description="unknown until the first report; stays so for placement none"
    )
    reason: AgentStatusReason | None
    observed_revision: int | None
    node: str | None
    instances: AgentInstances | None
    observed_at: datetime | None
    reported_by: uuid.UUID | None
    updated_at: datetime | None


class AgentListItemOut(AgentOut):
    observed_status: AgentStatusOut | None = Field(
        default=None,
        description="Present only with include=status: the body of GET /agents/{key}/status",
    )


class AgentPageOut(ApiModel):
    items: list[AgentListItemOut]
    next_cursor: str | None


class AgentRevisionSourceOut(ApiModel):
    kind: Literal["package", "manual", "unknown"] = Field(
        description="package: published by a package apply; manual: without a package; "
        "unknown: published before the source was recorded"
    )
    package: AgentPackageRef | None


class AgentRevisionSummaryOut(ApiModel):
    """An item of ``GET /agents/{key}/revisions``: the revision without its spec."""

    id: uuid.UUID
    agent_key: str
    revision: int
    spec_hash: str = Field(description="sha256:<hex> of the canonical JSON of spec")
    created_by: uuid.UUID = Field(description="The principal that published the revision")
    created_at: datetime
    source: AgentRevisionSourceOut
    active: bool = Field(
        description="The current revision of an agent that is not retired; "
        "a retired agent has no active revision"
    )
    changed_fields: list[str] | None = Field(
        description="Spec fields that differ from the previous revision (a nested object "
        "one level down, as section.field), sorted; null for revision 1"
    )


class AgentRevisionPageOut(ApiModel):
    items: list[AgentRevisionSummaryOut]
    next_cursor: str | None


# --- process-packages: processes, calendars, packages (CP-ADR-0074) ----------
#
# The contract lands before the implementation (constitution art. V): the
# routes answer 501 until the steps of the feature that implement them. The
# spec of a process is ``$defs.processSpec`` of the package-sdk catalog schema
# (``schema/v1/object.schema.json``); the core checks it by that
# schema and then by the language (CP-ADR-0074 §2, CP-ADR-0075), so the body
# here is the document as the catalog holds it. The calendar is small and
# modelled field by field; a contract test keeps both sides together.

PROCESS_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
PROCESS_ELEMENT_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
_PROCESS_KEY_FIELD = Field(min_length=1, max_length=63, pattern=PROCESS_KEY_PATTERN)
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
PACKAGE_MAX_FILES = 1000
PACKAGE_FILE_MAX_CHARS = 1_000_000
REPLAY_MAX_INSTANCES = 200

ProcessInstanceStatus = Literal["running", "suspended", "completed", "failed", "cancelled"]


class ProcessProblemOut(ApiModel):
    """One finding of a check: the same shape from every route and MCP tool."""

    code: str = Field(description="Machine-readable class, e.g. unknown_data_field")
    severity: Literal["error", "warning"]
    path: str = Field(description="JSON pointer into the object, e.g. /spec/stages/0/steps/1")
    file: str | None = Field(description="Package file, when the object came from one")
    line: int | None
    message: str
    hint: str | None


class ProcessDefinitionPublishRequest(ApiModel):
    """``POST /process-definitions``: a catalog object of kind Process without its envelope."""

    key: str = _PROCESS_KEY_FIELD
    spec: dict[str, Any] = Field(
        description="spec of a catalog object of kind Process ($defs.processSpec);"
        " spec.version is the version being published"
    )


CatalogStatus = Literal["active", "retired"]
STATUS_FILTER_DESCRIPTION = (
    "active — only keys in use, retired — only retired ones; both without it (CP-ADR-0074 Zh1)"
)


class RetirementOut(ApiModel):
    """When, by whom and why a process or calendar key was retired (CP-ADR-0074 Zh1)."""

    at: datetime
    by: uuid.UUID
    reason: str


_STATUS_FIELD: Any = Field(
    default="active",
    description="retired — every version of the key is out of use (CP-ADR-0074 Zh1)",
)
_RETIRED_FIELD = Field(default=None, description="The retirement of the key; null when in use")


class CatalogRetireRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)


class ProcessRetireVersionOut(ApiModel):
    version: int = Field(ge=1)
    open_instances: int = Field(ge=1)


class ProcessRetireOut(ApiModel):
    """``POST /process-definitions/{key}:retire``: no new instances, open ones run to the end."""

    key: str
    status: Literal["retired"] = "retired"
    retired: RetirementOut
    open_instances: int = Field(
        ge=0, description="Open (running, suspended) instances of every workspace"
    )
    by_version: list[ProcessRetireVersionOut] = Field(
        description="Only the versions with open instances"
    )


class CalendarRetireOut(ApiModel):
    key: str
    status: Literal["retired"] = "retired"
    retired: RetirementOut


class ProcessDefinitionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    key: str
    version: int
    latest_version: int
    display_name: str
    definition_hash: str = Field(description="sha256:<hex> of the canonical JSON of spec")
    identity_agent: str | None = Field(description="Agent key the process acts as")
    owner: list[dict[str, Any]] | None = Field(
        default=None,
        description="spec.owner: the assignment chain tasks about the process itself go to"
        " (regulation drift, failed instances); null when the process has none",
    )
    expression_profile: str = Field(description="CEL profile of its expressions, e.g. cp/1")
    engine_revision: int = Field(
        description="Revision of the engine semantics the version runs under (CP-ADR-0074)"
    )
    spec: dict[str, Any]
    warnings: list[ProcessProblemOut] = Field(
        default_factory=list, description="Findings that did not refuse the version"
    )
    status: CatalogStatus = _STATUS_FIELD
    retired: RetirementOut | None = _RETIRED_FIELD
    created_by: uuid.UUID
    created_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


class ProcessVersionOut(ApiModel):
    """An item of ``GET /process-definitions/{key}/versions``: a version without its spec."""

    id: uuid.UUID
    key: str
    version: int
    latest_version: int
    workspace_id: uuid.UUID | None
    display_name: str
    definition_hash: str
    identity_agent: str | None
    expression_profile: str
    engine_revision: int
    warnings: list[ProcessProblemOut] = Field(default_factory=list)
    status: CatalogStatus = _STATUS_FIELD
    retired: RetirementOut | None = _RETIRED_FIELD
    created_by: uuid.UUID
    created_at: datetime


class ProcessReplayRequest(ApiModel):
    """``POST /process-definitions/{key}:replay``: journals of real instances into a new version."""

    spec: dict[str, Any] = Field(description="The candidate version, as for publishing")
    instance_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=REPLAY_MAX_INSTANCES,
        description="Instances to replay; by default the latest ones of the current version",
    )
    limit: int = Field(default=50, ge=1, le=REPLAY_MAX_INSTANCES)


class ProcessDivergenceOut(ApiModel):
    journal_seq: int = Field(description="Entry of the instance journal where the paths part")
    element: str | None
    kind: str = Field(
        description="decision or intent (the first that differs); input — the candidate"
        " refused a recorded input; data, timer or state — every step matched, the final"
        " state did not"
    )
    recorded: Any = Field(description="What the instance's version decided")
    replayed: Any = Field(description="What the candidate decides on the same input")


class ProcessReplayInstanceOut(ApiModel):
    instance_id: uuid.UUID
    instance_key: str
    version: int
    events: int = Field(description="Journal inputs fed to the candidate")
    divergences: list[ProcessDivergenceOut]


class ProcessReplayOut(ApiModel):
    key: str
    candidate_hash: str
    replayed: int
    diverged: int
    problems: list[ProcessProblemOut]
    instances: list[ProcessReplayInstanceOut]


class ProcessStageStateOut(ApiModel):
    id: str
    state: Literal["available", "active", "completed", "terminated"]


SlaState = Literal["ok", "warning", "breached", "paused", "unknown", "none"]
_SLA_STATE_DESCRIPTION = (
    "Computed on read from dueAt, warnAt and now (CP-ADR-0078 §6): ok, warning,"
    " breached; paused while its clock stands (its timer is frozen while the"
    " instance is suspended); unknown when the deadline"
    " could not be computed; none without a deadline"
)


class ProcessSlaOut(ApiModel):
    """The deadline of a step or of the whole process (CP-ADR-0078 §6)."""

    due_at: datetime | None = Field(default=None, description="Null while frozen")
    warn_at: datetime | None = Field(
        default=None, description="Null without warnBefore and while frozen"
    )
    provisional: bool = Field(default=False, description="Computed on a provisional calendar year")
    remaining_seconds: int | None = Field(
        default=None,
        description="Until dueAt while running; kept while frozen, in remainingUnit;"
        " null once breached",
    )
    remaining_unit: Literal["wall", "working_seconds", "workdays"] | None = Field(
        default=None,
        description="Unit of remainingSeconds: wall — seconds of wall-clock time (always"
        " while running), working_seconds — seconds of working time, workdays — whole"
        " working days times 86400 plus the second of the day the deadline falls on,"
        " local to the calendar (CP-ADR-0078 §4); null with remainingSeconds",
    )


class ProcessOpenElementOut(ApiModel):
    id: str
    kind: str = Field(description="Step kind: human, approve, call, recall, listen, wait...")
    since: datetime
    task_id: uuid.UUID | None
    approval_ids: list[uuid.UUID]
    attempt: int | None = Field(
        default=None, description="Entry into this element within the instance, from 1"
    )
    due: ProcessSlaOut | None = Field(default=None, description="Null without a deadline")
    sla_state: SlaState | None = Field(default=None, description=_SLA_STATE_DESCRIPTION)
    overdue_seconds: int | None = Field(
        default=None, description="How far past dueAt the open step is; null while in time"
    )


class ProcessTimerOut(ApiModel):
    id: uuid.UUID
    element: str
    due_at: datetime | None = Field(description="Null while frozen by a suspension")
    state: Literal["pending", "frozen", "fired", "cancelled"]
    provisional: bool = Field(description="Computed on a provisional calendar year")
    remaining_seconds: int | None = Field(description="Kept while frozen (CP-ADR-0074 §6)")


class ProcessInstanceOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    definition_key: str
    definition_version: int
    instance_key: str
    status: ProcessInstanceStatus
    outcome: str | None
    data: dict[str, Any]
    stages: list[ProcessStageStateOut]
    open_elements: list[ProcessOpenElementOut]
    timers: list[ProcessTimerOut]
    sla: ProcessSlaOut | None = Field(
        default=None, description="The deadline of the process (spec.due), counted from start"
    )
    sla_state: SlaState | None = Field(
        default=None, description="The worst state of the process and its open steps"
    )
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ProcessJournalEntryOut(ApiModel):
    """One decision of the engine with its reason and author (FR-018)."""

    seq: int
    at: datetime = Field(description="Engine time of the input: the event's time, never now")
    kind: str = Field(
        description="input, transition, stage, milestone, timer, intent, vote, recall,"
        " compensation, migration, error"
    )
    element: str | None
    reason: str
    actor_id: uuid.UUID | None = Field(description="Principal behind the input, if any")
    event_id: uuid.UUID | None = Field(description="Journal event the input came from")
    data: dict[str, Any]


class ProcessInstanceStartRequest(ApiModel):
    """``POST /process-instances``: an instance started without a trigger event (TAI-ADR-0055).

    For a standing goal — a reconciling process without ``complete`` whose
    milestones are reached and lost again. One instance per key: a key that
    has one is ``409 process_instance_exists`` with its id.
    """

    process: str = _PROCESS_KEY_FIELD
    key: str = Field(min_length=1, max_length=500, description="The instance key (start.key)")
    data: dict[str, Any] | None = Field(
        default=None, description="Initial data, checked against the data schema of the process"
    )
    workspace_id: uuid.UUID | None = Field(
        default=None,
        description="Workspace of an instance of a tenant-wide process; a process of a"
        " workspace keeps its instances there",
    )


class ProcessSuspendRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)


class ProcessResumeRequest(ApiModel):
    reason: str | None = Field(default=None, max_length=500)


class ProcessCancelRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)
    compensate: bool = Field(
        default=True, description="Run the compensations of completed steps first"
    )


def _unique_dates(items: list[date]) -> list[date]:
    if len(set(items)) != len(items):
        raise ValueError("dates must be unique")
    return items


_CalendarDates = Annotated[list[date], AfterValidator(_unique_dates)]


class CalendarYear(ApiModel):
    year: int = Field(ge=2000, le=2100)
    provisional: bool | None = None
    source: str | None = Field(default=None, max_length=500)
    holidays: _CalendarDates | None = None
    workdays: _CalendarDates | None = Field(
        default=None, description="Working days moved onto a weekend"
    )
    short_days: _CalendarDates | None = None


def _unique_weekdays(items: list[int]) -> list[int]:
    if len(set(items)) != len(items):
        raise ValueError("weekend days must be unique")
    return items


class CalendarInterval(ApiModel):
    """One interval of a working day, local time of the calendar (``$defs.workingIntervals``)."""

    from_: str = Field(alias="from", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    to: str = Field(pattern=r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")


def _weekday_keys(value: Any) -> Any:
    # YAML reads the key 6 of `weekdays: {6: []}` as an integer.
    if isinstance(value, dict):
        return {str(key) if isinstance(key, int) else key: item for key, item in value.items()}
    return value


# $defs.duration of the catalog schema; look-ahead needs Python's re, not the model's regex.
_ISO_DURATION = re.compile(r"P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?")


def _iso_duration(value: str) -> str:
    if not _ISO_DURATION.fullmatch(value):
        raise ValueError("must be an ISO 8601 duration such as PT1H")
    return value


_Weekday = Literal["1", "2", "3", "4", "5", "6", "7"]
_CalendarIntervals = Annotated[list[CalendarInterval], Field(max_length=10)]


class CalendarWorkingHours(ApiModel):
    """Working hours of a calendar (CP-ADR-0078 §2): only the shape.

    The order of intervals and what they mean are the calendar's business.
    """

    intervals: Annotated[list[CalendarInterval], Field(min_length=1, max_length=10)]
    weekdays: (
        Annotated[dict[_Weekday, _CalendarIntervals], BeforeValidator(_weekday_keys)] | None
    ) = Field(default=None, description="Intervals by ISO weekday instead of intervals; [] is none")
    short_day_reduction: Annotated[str, AfterValidator(_iso_duration)] | None = Field(
        default=None,
        description="How much shorter a short day is, taken off the end of its last interval",
    )


class CalendarSpec(ApiModel):
    """``$defs.calendarSpec`` of the catalog schema."""

    display_name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(max_length=64)
    weekend: (
        Annotated[list[Annotated[int, Field(ge=1, le=7)]], AfterValidator(_unique_weekdays)] | None
    ) = Field(default=None, description="ISO weekdays, 1 is Monday; [6, 7] by default")
    working_hours: CalendarWorkingHours | None = None
    years: list[CalendarYear] = Field(min_length=1)


class CalendarPublishRequest(ApiModel):
    key: str = _PROCESS_KEY_FIELD
    spec: CalendarSpec


class CalendarOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    latest_version: int
    calendar_hash: str
    spec: dict[str, Any]
    provisional_years: list[int]
    status: CatalogStatus = _STATUS_FIELD
    retired: RetirementOut | None = _RETIRED_FIELD
    created_by: uuid.UUID
    created_at: datetime
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD


ViewFormat = Literal[
    "text",
    "number",
    "money",
    "percent",
    "date",
    "datetime",
    "due",
    "duration",
    "principal",
    "status",
    "link",
]
_VIEW_KEY_DESCRIPTION = "Stable key: the values of the field come by it in the data of the view"


class ViewNavOut(ApiModel):
    group: Literal["work", "knowledge", "packages"] = Field(
        description="Group of the console menu (a closed list); a view that names none: packages"
    )
    icon: str | None = None
    order: int | None = None


class ViewSourceOut(ApiModel):
    kind: Literal["process", "tasks", "knowledge"]
    process: str | None = Field(default=None, description="Key of the process of a process source")
    instance: bool = Field(description="The view shows one instance (a card), opened by its id")


class ViewColumnOut(ApiModel):
    key: str = Field(description=_VIEW_KEY_DESCRIPTION)
    title: str
    format: ViewFormat | None = None


class ViewCardFieldOut(ApiModel):
    key: str = Field(description=_VIEW_KEY_DESCRIPTION)
    format: ViewFormat | None = None


class ViewFieldOut(ApiModel):
    key: str = Field(description=_VIEW_KEY_DESCRIPTION)
    label: str
    format: ViewFormat | None = None


class ViewFilterOptionOut(ApiModel):
    value: str | int | float | bool
    title: str


class ViewFilterOut(ApiModel):
    field: str = Field(description="The key a filter of the data of the view names")
    title: str
    type: Literal["text", "enum", "number", "date"] = Field(
        description="Read by the core from the data schema of the source"
    )
    options: list[ViewFilterOptionOut] | None = Field(
        default=None, description="The values of an enum"
    )


class ViewSortOut(ApiModel):
    field: str
    title: str


class ViewOpenOut(ApiModel):
    view: str = Field(description="The view a record opens; its id is the id of the row")


class ViewMetricsBlockOut(ApiModel):
    block: Literal["metrics"]
    title: str | None = None
    items: list[ViewColumnOut]


class ViewTableBlockOut(ApiModel):
    block: Literal["table", "list"]
    title: str | None = None
    columns: list[ViewColumnOut]
    filters: list[ViewFilterOut] | None = None
    sort: list[ViewSortOut] | None = None
    open: ViewOpenOut | None = None


class ViewCardOut(ApiModel):
    fields: list[ViewCardFieldOut]


class ViewBoardBlockOut(ApiModel):
    block: Literal["board"] = Field(description="A column per stage of the process")
    title: str | None = None
    card: ViewCardOut
    filters: list[ViewFilterOut] | None = None
    open: ViewOpenOut | None = None


class ViewChartBlockOut(ApiModel):
    block: Literal["chart"]
    type: Literal["bar", "line", "donut"]
    title: str | None = None
    format: ViewFormat | None = None


class ViewFieldsBlockOut(ApiModel):
    block: Literal["fields"]
    title: str | None = None
    section: str | None = None
    items: list[ViewFieldOut]


class ViewHeaderBlockOut(ApiModel):
    block: Literal["header"] = Field(description="Title, status and lead come in its data")
    actions: Any | None = Field(default=None, description="As the package writes it")


class ViewRecordBlockOut(ApiModel):
    block: Literal["steps", "timeline", "artifacts"]
    title: str | None = None


class ViewRelatedBlockOut(ApiModel):
    block: Literal["related"]
    title: str | None = None
    relations: list[str] | None = Field(default=None, description="Names of the relations shown")


class ViewInvokeBlockOut(ApiModel):
    model_config = ConfigDict(extra="allow")

    block: Literal["invoke"] = Field(description="As the package writes it, its strings in text")


class ViewComponentBlockOut(ApiModel):
    block: Literal["component"]
    component: str
    layout: list[dict[str, Any]] = Field(description="The blocks of the component, as a view's")


ViewBlockOut = Annotated[
    ViewMetricsBlockOut
    | ViewTableBlockOut
    | ViewBoardBlockOut
    | ViewChartBlockOut
    | ViewFieldsBlockOut
    | ViewHeaderBlockOut
    | ViewRecordBlockOut
    | ViewRelatedBlockOut
    | ViewInvokeBlockOut
    | ViewComponentBlockOut,
    Field(discriminator="block"),
]


class ViewSummaryOut(ApiModel):
    """A screen of a package as the console menu lists it, in one language (CP-ADR-0080 §9).

    The raw source of the package (its CEL ``filter``, ``param.<name>``) is not here.
    """

    key: str
    title: str
    description: str | None = None
    revision: int = Field(description="Revision of the view; a new one per change")
    hash: str = Field(description="sha256 of the revision: what a console caches by")
    blocks: int = Field(description="Version of the set of blocks the layout is written in")
    locale: str = Field(description="The language of the strings: the one asked or the default")
    package: PackageLinkOut | None = _PACKAGE_LINK_FIELD
    nav: ViewNavOut | None = Field(default=None, description="null: opened only by a link")
    source: ViewSourceOut


class ViewOut(ViewSummaryOut):
    """A screen of a package as a console draws it: no path and no expression in its blocks."""

    layout: list[ViewBlockOut]


class ViewSummaryPageOut(ApiModel):
    items: list[ViewSummaryOut]
    next_cursor: str | None


# --- POST /views/{view_key}:query (CP-ADR-0080 amendment A) ---

_VIEW_VALUES_DESCRIPTION = (
    "Values by the keys of GET /views/{key}, in their formats: money {amount, currency},"
    " date/datetime/due ISO 8601, principal an id, status {title, category}, link"
    " {href, title}; no value: null"
)
ViewStatusCategory = Literal["running", "suspended", "completed", "failed", "cancelled"]
_SCALAR = str | int | float | bool


class ViewQueryFilter(ApiModel):
    field: str = Field(
        min_length=1, max_length=200, description="A filter the block declares (its field)"
    )
    op: Literal["eq", "in", "gte", "lte", "prefix"] = Field(
        description="enum: eq, in; text: eq, in, prefix; number: eq, in, gte, lte;"
        " date: eq, gte, lte (a day YYYY-MM-DD)"
    )
    value: _SCALAR | list[_SCALAR] = Field(description="A list for in, else one value")


class ViewQuerySort(ApiModel):
    field: str = Field(min_length=1, max_length=200, description="A sort the block declares")
    dir: Literal["asc", "desc"] = "asc"


class ViewQueryRequest(ApiModel):
    """The data one block of a view draws (TAI-ADR-0066 p.4): values, never expressions."""

    params: dict[str, Any] | None = Field(
        default=None, description="Params of the view by their types: {id} of a card"
    )
    block: int = Field(ge=0, le=49, description="Index of the block in the layout of the view")
    filter: list[ViewQueryFilter] | None = Field(
        default=None, max_length=20, description="Only the filters the block declares"
    )
    sort: list[ViewQuerySort] | None = Field(
        default=None, max_length=5, description="Only the sorts a table or a list declares"
    )
    limit: int | None = Field(default=None, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=4000)
    workspace_id: uuid.UUID | None = Field(
        default=None,
        description="A view of knowledge: the workspace whose tree's knowledge base it reads;"
        " none — the caller's only tree (several — 422 workspace_required). Not read by other"
        " sources",
    )


class ViewStatusValueOut(ApiModel):
    title: str
    category: ViewStatusCategory = Field(
        description="Of the status of the instance as ProcessInstanceOut.status: running,"
        " suspended, completed, failed, cancelled"
    )


class ViewRowOut(ApiModel):
    id: str = Field(description="What open.view opens: open.id of the view, else the instance id")
    title: str = Field(description="The key of the instance")
    values: dict[str, Any] = Field(description=_VIEW_VALUES_DESCRIPTION)


class ViewRowsOut(ApiModel):
    """``table``, ``list``: a page."""

    items: list[ViewRowOut]
    next_cursor: str | None


class ViewBoardCardOut(ApiModel):
    id: str
    title: str
    subtitle: str | None = None
    values: dict[str, Any] = Field(description=_VIEW_VALUES_DESCRIPTION)
    badge: ViewStatusValueOut | None = None


class ViewBoardColumnOut(ApiModel):
    key: str = Field(description="The id of the stage")
    title: str
    items: list[ViewBoardCardOut]


class ViewBoardOut(ApiModel):
    """``board``: a column per stage of the process, in its order."""

    columns: list[ViewBoardColumnOut]


class ViewMetricsOut(ApiModel):
    """``metrics``: a value per item, in the order of the items.

    An aggregate is over every instance of the source the caller sees, after
    ``source.filter``: never over a page (CP-ADR-0080 A5).
    """

    values: list[Any]


class ViewChartPointOut(ApiModel):
    label: str
    value: Any


class ViewChartOut(ApiModel):
    """``chart``: a point per group of ``groupBy``.

    An aggregate is over every instance of the source the caller sees, after
    ``source.filter``: never over a page (CP-ADR-0080 A5).
    """

    points: list[ViewChartPointOut]


class ViewHeaderOut(ApiModel):
    title: str
    status: ViewStatusValueOut
    lead: str | None = None


class ViewFieldsOut(ApiModel):
    values: dict[str, Any] = Field(description=_VIEW_VALUES_DESCRIPTION)


class ViewStepTaskOut(ApiModel):
    ref: str = Field(description="The public id of the task: the console opens its form by it")
    title: str


class ViewStepOut(ApiModel):
    key: str = Field(description="The id of the step")
    title: str
    task: ViewStepTaskOut | None = Field(
        default=None, description="The task of the step, when the caller may read it"
    )
    assignee: str | None = None
    since: str | None = None


class ViewStepsOut(ApiModel):
    items: list[ViewStepOut]


class ViewTimelineItemOut(ApiModel):
    at: str
    title: str
    actor: str | None = None


class ViewTimelineOut(ApiModel):
    items: list[ViewTimelineItemOut]


class ViewArtifactItemOut(ApiModel):
    id: str
    name: str
    type: str
    created_at: str


class ViewArtifactsOut(ApiModel):
    items: list[ViewArtifactItemOut]


class ViewRelationOut(ApiModel):
    relation: str
    title: str | None = None
    kind: str
    key: str
    entity_title: str
    direction: Literal["in", "out"]


class ViewRelatedOut(ApiModel):
    items: list[ViewRelationOut]


ViewQueryOut = (
    ViewRowsOut
    | ViewBoardOut
    | ViewMetricsOut
    | ViewChartOut
    | ViewHeaderOut
    | ViewFieldsOut
    | ViewStepsOut
    | ViewTimelineOut
    | ViewArtifactsOut
    | ViewRelatedOut
)


def _package_path(path: str) -> str:
    parts = path.split("/")
    if "\\" in path or "\x00" in path or ".." in parts or "" in parts:
        raise ValueError("a relative path inside the package, without .. and empty parts")
    return path


class PackageFile(ApiModel):
    path: Annotated[str, AfterValidator(_package_path)] = Field(
        min_length=1,
        max_length=500,
        description="Path inside the package, e.g. processes/onboarding.yaml",
    )
    content: str = Field(max_length=PACKAGE_FILE_MAX_CHARS, description="YAML 1.2 text")


class PackageSource(ApiModel):
    """The files of one package as the author has them (CP-ADR-0074 §10)."""

    files: list[PackageFile] = Field(min_length=1, max_length=PACKAGE_MAX_FILES)

    @model_validator(mode="after")
    def _unique_paths(self) -> "PackageSource":
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("file paths must be unique")
        return self


class PackageTestRequest(ApiModel):
    package: PackageSource
    tests: list[str] | None = Field(
        default=None, max_length=500, description="Test files to run; all by default"
    )
    workspace_id: uuid.UUID | None = Field(
        default=None, description="Workspace whose roles, calendars and instances the run reads"
    )


class PackageTestFailureOut(ApiModel):
    step: int = Field(description="Index of the test step, 0-based")
    message: str
    expected: Any = None
    actual: Any = None


class PackageTestResultOut(ApiModel):
    file: str
    name: str
    subject: Literal["process", "rule", "taskType"]
    object: str = Field(description="Key of the process, rule or task type under test")
    process: str | None = Field(description="Key of the process; null for rule and taskType tests")
    status: Literal["passed", "failed", "error"]
    duration_ms: int
    failures: list[PackageTestFailureOut]


class CoverageCounterOut(ApiModel):
    covered: int
    total: int
    missing: list[str] = Field(description="Ids of what no test reached")


class ProcessCoverageOut(ApiModel):
    process: str
    version: int
    elements: CoverageCounterOut
    transitions: CoverageCounterOut
    decision_rows: CoverageCounterOut
    handlers: CoverageCounterOut


class RuleCoverageOut(ApiModel):
    """A rule of the package (CP-ADR-0074 Z3): branches of its expressions, its outcomes."""

    rule: str
    tests: int = Field(ge=0)
    branches: CoverageCounterOut = Field(
        description="Each operand of and/or, not and comparison of condition and where, "
        "true and false: '/condition/and/1:true'"
    )
    outcomes: CoverageCounterOut = Field(
        description="matched, not_matched; with an interpretation also "
        "interpretation:answered and interpretation:failed"
    )


class TaskTypeCoverageOut(ApiModel):
    """A task type of the package with gates, work after completion or acceptance (Z3)."""

    task_type: str
    version: int
    tests: int = Field(ge=0)
    outcomes: CoverageCounterOut = Field(
        description="<gate>/<outcome> and <gate>/<outcome>/<i>/onSuccess|onFailure"
    )
    preconditions: CoverageCounterOut = Field(
        description="<gate>/preconditions/approved/<i>:held|refused"
    )
    completion: CoverageCounterOut = Field(description="completion/<i>")
    acceptance: CoverageCounterOut = Field(description="acceptance/<key>:passed|failed")


class PackageTestOut(ApiModel):
    status: Literal["passed", "failed", "invalid"]
    check_only: bool
    problems: list[ProcessProblemOut]
    tests: list[PackageTestResultOut]
    coverage: list[ProcessCoverageOut] = Field(description="Coverage of the processes")
    rule_coverage: list[RuleCoverageOut]
    task_type_coverage: list[TaskTypeCoverageOut]
    duration_ms: int


_OVERWRITE_CONSOLE = Field(
    default=False,
    description="Overwrite the fields a person changed since the last apply (owner console);"
    " by default they are kept. Part of the plan: apply with the flag the plan was built with",
)


class PackagePlanRequest(ApiModel):
    package: PackageSource
    workspace_id: uuid.UUID | None = Field(
        default=None,
        description="Workspace an install variable ${...} of spec.workspaceId stands for",
    )
    replay_limit: int = Field(
        default=50, ge=0, le=REPLAY_MAX_INSTANCES, description="Instances replayed per process"
    )
    overwrite_console: bool = _OVERWRITE_CONSOLE


class PlanFieldOut(ApiModel):
    path: str
    before: Any
    after: Any
    owner: Literal["package", "console"] = Field(
        description="console: a person changed the field since the last apply"
    )
    applies: bool = Field(description="False when a console-owned field is kept")


class PlanChangeOut(ApiModel):
    kind: Literal["TaskType", "Agent", "Calendar", "Process", "WorkRule", "View"] = Field(
        description="The kinds the core plans, in the order the apply publishes them"
    )
    key: str
    action: Literal["create", "update", "rename", "restore", "retire", "unchanged"]
    renamed_from: str | None
    fields: list[PlanFieldOut]
    deprecates: list[int] = Field(
        default_factory=list,
        description="TaskType: the active versions the apply deprecates (all but the kept one)",
    )


class PlanOutsideOut(ApiModel):
    """An object of the package the core does not plan, and who applies it."""

    kind: str
    key: str
    applied_by: Literal["installer", "notification-service"]


class PlanReplayOut(ApiModel):
    replayed: int
    diverged: int
    instance_ids: list[uuid.UUID] = Field(description="Diverged instances, at most 20")


class PlanInstancesOut(ApiModel):
    version: int
    open: int
    fate: Literal["pin", "migrate", "unaffected"]
    migration_required: bool = Field(
        description="An element they stand on is gone and no migration covers it"
    )


class PlanDeadlineOut(ApiModel):
    """An open instance whose deadline the migration sets, moves or lifts (FR-023).

    The section also lists a deadline the new version finds already past
    (``breached``): the apply records its breach. A deadline breached on the
    old version and past by the new one at the same moment is not listed.
    """

    instance_id: uuid.UUID
    element: str | None = Field(description="Null for the deadline of the process")
    previous_due_at: datetime | None = Field(description="Null when the deadline is new")
    due_at: datetime | None = Field(description="Null when the new version lifts it")
    breached: bool = Field(
        description="The deadline by the new version has already passed at the time of the plan"
    )


class PlanProcessOut(ApiModel):
    key: str
    from_version: int | None
    to_version: int
    behaviour: PlanReplayOut | None
    instances: list[PlanInstancesOut]
    deadlines: list[PlanDeadlineOut] = Field(
        default_factory=list,
        description="Deadlines of migrated instances recomputed by the new version, at most 200",
    )
    deadlines_total: int = Field(
        default=0,
        description="Deadlines the migration moves, all of them: more than listed when the"
        " section is cut to its limit",
    )


class RegulationCoverageOut(ApiModel):
    document: str
    found: bool = Field(description="The document is in memory")
    covered: dict[str, list[str]] = Field(description="Section -> element ids governed by it")
    uncovered: list[str] = Field(description="Sections no element is governed by")


class PlanSettingsRevisionOut(ApiModel):
    before: int | None = Field(description="The active schema revision; null — none")
    after: int | None = Field(
        description="The revision the apply writes; null — the schema does not change, or"
        " the package stops declaring settings"
    )


class PlanSettingsAddedOut(ApiModel):
    path: str = Field(description="JSON Pointer of the new field")
    default: Any = Field(
        default=None, description="Its default; absent for a required field without one"
    )

    @model_serializer(mode="wrap")
    def _without_absent_default(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # A field without a default has none: absent, not null (a null default is a value).
        out: dict[str, Any] = handler(self)
        if "default" not in self.model_fields_set:
            out.pop("default", None)
        return out


class PlanSettingsRemovedOut(ApiModel):
    path: str
    saved: bool = Field(description="The field had a saved value: it stays in the history")


class PlanSettingsIncompatibleOut(ApiModel):
    path: str
    code: str = Field(description="The JSON Schema keyword the saved value fails; no value")


class PlanSettingsOut(ApiModel):
    """``settings`` of the plan (CP-ADR-0081 §7): the new revision against the active one."""

    schema_revision: PlanSettingsRevisionOut
    added: list[PlanSettingsAddedOut]
    removed: list[PlanSettingsRemovedOut]
    incompatible: list[PlanSettingsIncompatibleOut] = Field(
        description="Saved values the new schema refuses: each is also an error"
        " settings_incompatible of the plan"
    )
    uischema_changed: bool


class PackagePlanOut(ApiModel):
    plan_hash: str = Field(description="sha256 over package, catalog etag and the plan body")
    catalog_etag: str
    package: dict[str, Any] = Field(description="{key, version} of the package")
    changes: list[PlanChangeOut]
    outside: list[PlanOutsideOut] = Field(
        default_factory=list,
        description="Objects of kinds the core does not plan (NotificationRule, Skill, Role…)",
    )
    processes: list[PlanProcessOut]
    regulation_coverage: list[RegulationCoverageOut]
    settings: PlanSettingsOut | None = Field(
        default=None,
        description="Settings of the package against the active revision; null — the package"
        " neither declared nor declares settings",
    )
    problems: list[ProcessProblemOut]
    created_at: datetime


class PackageApplyRequest(ApiModel):
    package: PackageSource
    plan_hash: str = Field(pattern=_SHA256_PATTERN)
    workspace_id: uuid.UUID | None = None
    overwrite_console: bool = _OVERWRITE_CONSOLE


class PackageAppliedOut(ApiModel):
    kind: str
    key: str
    action: Literal["create", "update", "rename", "restore", "retire", "unchanged"]
    version: int | None


class PackageApplyOut(ApiModel):
    plan_hash: str
    catalog_etag: str = Field(description="Etag of the catalog after the apply")
    applied: list[PackageAppliedOut]


PACKAGE_RECORD_MAX_OBJECTS = 1000
RecordedKind = Literal[
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
]


class PackageRef(ApiModel):
    """A package as its installer names it: ``package.yaml`` key and version."""

    key: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)


class PackageRecordedObject(ApiModel):
    kind: RecordedKind
    key: str = Field(
        min_length=1,
        max_length=200,
        description="key; slug of a Role, name of a Capability or Skill",
    )


class PackageRecordRequest(ApiModel):
    """``POST /packages:record``: the objects one package's installation applied.

    Every object of the package the installer applied, the unchanged ones too:
    the link moves to this version of the package. Processes and calendars are
    linked by ``POST /packages:apply``.
    """

    package: PackageRef
    install_hash: str | None = Field(
        default=None,
        max_length=200,
        description="The installation as the installer identifies it (e.g. sha256 of the"
        " package files); returned as package.installHash of the objects",
    )
    objects: list[PackageRecordedObject] = Field(
        min_length=1, max_length=PACKAGE_RECORD_MAX_OBJECTS
    )


class PackageRecordOut(ApiModel):
    package: PackageRef
    install_hash: str | None
    recorded: list[PackageRecordedObject]


# --- may I do X on Y (CP-ADR-0055, amendment of 2026-09-29) --------------------

AUTHZ_CHECK_MAX_ITEMS = 100

AuthzResourceType = Literal["approval", "process_instance", "run", "rule", "agent", "principal"]
AuthzAction = Literal[
    "approve",
    "reject",
    "suspend",
    "resume",
    "cancel",
    "request-cancel",
    "enable",
    "disable",
    "update-state",
]


class AuthzCheckItem(ApiModel):
    action: AuthzAction = Field(
        description="The endpoint's verb: approval approve|reject; process_instance"
        " suspend|resume|cancel; run request-cancel|cancel; rule enable|disable;"
        " agent update-state (PATCH /agents/{key}/state, state and replicas);"
        " principal enable|disable"
    )
    resource_type: AuthzResourceType
    resource_id: str = Field(
        min_length=1, max_length=200, description="The id; for an agent — its key"
    )


class AuthzCheckRequest(ApiModel):
    checks: list[AuthzCheckItem] = Field(min_length=1, max_length=AUTHZ_CHECK_MAX_ITEMS)


class AuthzDenialOut(ApiModel):
    code: str = Field(
        description="The code of the endpoint's own refusal: permission_denied, not_eligible,"
        " separation_of_duties_violation, run_holder_mismatch, outside_purpose, not_found;"
        " for a principal also permission_escalation, principal_kind_not_enableable,"
        " principal_kind_not_disableable, cannot_disable_self, use_agent_publish,"
        " use_agent_retire"
    )
    message: str
    details: dict[str, Any]


class AuthzCheckResultOut(ApiModel):
    action: AuthzAction
    resource_type: AuthzResourceType
    resource_id: str
    allowed: bool
    reason: AuthzDenialOut | None = Field(description="Why not; null when allowed")


class AuthzCheckOut(ApiModel):
    results: list[AuthzCheckResultOut] = Field(description="One per check, in request order")


# --- settings of packages (CP-ADR-0081 §4) -------------------------------------


class PackageSettingsSummaryOut(ApiModel):
    package: str = Field(description="The key of the package")
    title: str = Field(description="The string <package>.title in the language asked")
    package_version: str | None = Field(description="The package version of the active revision")
    version: int = Field(description="The version of the values; 0 — nothing saved yet")
    updated_by: uuid.UUID | None
    updated_at: datetime | None


class PackageSettingsListOut(ApiModel):
    items: list[PackageSettingsSummaryOut] = Field(
        description="The packages whose active revision declares settings, by key"
    )


class PackageSettingsOut(ApiModel):
    """The settings of a package as the console draws its form (CP-ADR-0081 §4)."""

    package: str
    title: str
    package_version: str | None
    settings_schema: dict[str, Any] = Field(
        alias="schema",
        description="The schema of the active revision, title and description of each"
        " property put in from the dictionaries",
    )
    uischema: dict[str, Any] | None = Field(
        description="The layout with its labels put in; null — the console lays the fields out"
    )
    values: dict[str, Any] = Field(description="The saved values the active schema declares")
    effective: dict[str, Any] = Field(description="The saved values over the defaults")
    version: int = Field(description="The version of the values; 0 — nothing saved yet")
    schema_hash: str
    updated_by: uuid.UUID | None
    updated_at: datetime | None
    can_manage: bool = Field(description="The caller holds packages.settings.manage")


class PackageSettingsPutRequest(ApiModel):
    """The saved values whole: a field the body lacks goes back to its default."""

    # Other members are refused by the route, their names never quoted back
    # when they look like a credential.
    model_config = ConfigDict(extra="allow", json_schema_extra={"additionalProperties": False})

    values: dict[str, Any]


class PackageSettingsVersionOut(ApiModel):
    version: int
    values: dict[str, Any] = Field(description="The saved values of the version, as written")
    changed_paths: list[str] = Field(
        description="JSON Pointers of the members whose value changed against the version before"
    )
    updated_by: uuid.UUID
    updated_at: datetime


class PackageSettingsVersionPageOut(ApiModel):
    items: list[PackageSettingsVersionOut]
    next_cursor: str | None
