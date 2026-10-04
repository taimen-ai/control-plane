"""Catalog of the event types the core writes to its journal (CP-ADR-0068).

Every event carries an envelope (``id``, ``type``, ``sequence``, ``entityType``,
``entityId``, ``workspaceId``, ``occurredAt``, ``actorId``, ``schemaVersion``,
``payload``...) and a ``payload`` whose shape is declared here: event type ->
version -> JSON Schema of the payload. The writer stamps the current version of
the type onto the event (``schema_version``), so a consumer reading an old event
knows which schema it follows.

Evolution rule: a version only ADDS payload fields. A consumer written against
version N reads version N+1 unchanged; a field that changes its meaning or goes
away is a new event type, not a new version. Older versions stay in the catalog
for as long as the journal may hold events written under them.

The catalog is neutral (constitution, art. II): it names the core's own
entities — tasks, runs, approvals, workspaces — never a domain of a package.

``python -m control_plane.domain.event_catalog <dir>`` renders the catalog into
``<dir>/catalog.md`` and ``<dir>/catalog.json`` (``make event-catalog`` writes
``docs/events/``); a test keeps the committed files in step with this module.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonSchema = dict[str, Any]

# --- schema vocabulary -------------------------------------------------------

ANY: JsonSchema = {}
STR: JsonSchema = {"type": "string"}
INT: JsonSchema = {"type": "integer"}
BOOL: JsonSchema = {"type": "boolean"}
OBJ: JsonSchema = {"type": "object"}
ARR: JsonSchema = {"type": "array"}
UUID: JsonSchema = {"type": "string", "format": "uuid"}
TIME: JsonSchema = {"type": "string", "format": "date-time"}


def nullable(schema: JsonSchema) -> JsonSchema:
    """``schema`` or ``null``."""
    kind = schema.get("type")
    if kind is None:
        return schema
    return {**schema, "type": [kind, "null"]}


STR_N = nullable(STR)
UUID_N = nullable(UUID)
INT_N = nullable(INT)


def described(schema: JsonSchema, description: str) -> JsonSchema:
    return {**schema, "description": description}


def data(
    required: Mapping[str, JsonSchema] | None = None,
    optional: Mapping[str, JsonSchema] | None = None,
) -> JsonSchema:
    """An object schema: ``required`` keys are always present (possibly null),
    ``optional`` ones only on some paths. Unknown keys are allowed — a newer
    version may add them, and a consumer must ignore what it does not know."""
    properties = {**(required or {}), **(optional or {})}
    schema: JsonSchema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


# --- catalog entries ---------------------------------------------------------


@dataclass(frozen=True)
class EventVersion:
    version: int
    schema: JsonSchema
    # What this version added relative to the previous one (empty for v1).
    changes: str = ""


@dataclass(frozen=True)
class EventType:
    type: str
    entity_type: str
    description: str
    versions: tuple[EventVersion, ...] = field(default_factory=tuple)

    @property
    def current(self) -> EventVersion:
        return self.versions[-1]

    def version(self, number: int) -> EventVersion | None:
        return next((v for v in self.versions if v.version == number), None)


class UnknownEventType(ValueError):
    """An event type missing from the catalog: a programming error in the core."""


_TYPES: dict[str, EventType] = {}


def _register(
    event_type: str,
    entity_type: str,
    description: str,
    *versions: JsonSchema | tuple[JsonSchema, str],
) -> None:
    if event_type in _TYPES:
        raise ValueError(f"event type {event_type!r} registered twice")
    built: list[EventVersion] = []
    for number, item in enumerate(versions, start=1):
        schema, changes = item if isinstance(item, tuple) else (item, "")
        built.append(EventVersion(version=number, schema=schema, changes=changes))
    _TYPES[event_type] = EventType(event_type, entity_type, description, tuple(built))


def event_types() -> list[EventType]:
    """Every registered type, ordered by name."""
    return [_TYPES[name] for name in sorted(_TYPES)]


def get_event_type(event_type: str) -> EventType:
    try:
        return _TYPES[event_type]
    except KeyError:
        raise UnknownEventType(
            f"event type {event_type!r} is not in the event catalog"
            " (control_plane/domain/event_catalog.py)"
        ) from None


def current_version(event_type: str) -> int:
    """The version a new event of this type is written with."""
    return get_event_type(event_type).current.version


def schema_for(event_type: str, version: int) -> JsonSchema | None:
    entry = _TYPES.get(event_type)
    found = entry.version(version) if entry else None
    return found.schema if found else None


# Limit of free text copied into an event payload (a decision comment...): the
# event says what happened, the full text stays with its entity.
PAYLOAD_TEXT_LIMIT = 1000

# --- approvals ---------------------------------------------------------------

_APPROVAL_REQUESTED_V1 = {
    "taskId": UUID_N,
    "artifactId": UUID_N,
    "requiredRoleId": described(UUID_N, "Set when any holder of the role may decide"),
    "assignedPrincipalId": described(UUID_N, "Set when one principal decides"),
    "gate": described(BOOL, "A gate holds the task's claim and completion until decided"),
}
_APPROVAL_REQUESTED_V2 = {
    **_APPROVAL_REQUESTED_V1,
    "workspaceId": described(UUID_N, "The approval's workspace, else its task's workspace"),
    "taskPublicId": described(STR_N, "Public id of the task, e.g. TASK-000123"),
    "taskTitle": STR_N,
    "requestedBy": described(UUID, "Principal who requested the decision"),
    "comment": described(
        {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
        "Request comment; credential-shaped material redacted, cut to the limit",
    ),
}
_register(
    "approval.requested",
    "approval",
    "A decision was requested from a principal or from the holders of a role.",
    data(_APPROVAL_REQUESTED_V1),
    (data(_APPROVAL_REQUESTED_V2), "workspaceId, taskPublicId, taskTitle, requestedBy, comment"),
    (
        data(
            {
                **_APPROVAL_REQUESTED_V2,
                "excludedPrincipals": described(
                    {"type": "array", "items": UUID},
                    "Principals whose decision the core refuses"
                    " (separation of duties, CP-ADR-0074); empty when nobody is excluded",
                ),
            }
        ),
        "excludedPrincipals",
    ),
)

_APPROVAL_DECIDED_V1 = {
    "taskId": UUID_N,
    "artifactId": UUID_N,
    "outcomeStatus": described(
        STR_N, "pending when the task type declares outcomes for this decision (CP-ADR-0061)"
    ),
}
_APPROVAL_DECIDED_V2 = {
    **_APPROVAL_DECIDED_V1,
    "decisionBy": described(UUID, "Principal who decided"),
    "comment": described(
        {"type": ["string", "null"], "maxLength": PAYLOAD_TEXT_LIMIT},
        "Decision comment; credential-shaped material redacted, cut to the limit",
    ),
    "channel": described(
        STR_N,
        "Channel the decision came through when it was not a direct API call"
        " (e.g. telegram); null for a direct call",
    ),
}
for _decision in ("approved", "rejected"):
    _register(
        f"approval.{_decision}",
        "approval",
        f"The approval was {_decision} by an eligible principal.",
        data(_APPROVAL_DECIDED_V1),
        (data(_APPROVAL_DECIDED_V2), "decisionBy, comment, channel"),
    )

_register(
    "approval.cancelled",
    "approval",
    "The pending approval was cancelled; nobody decides it any more.",
    data({"taskId": UUID_N}),
    (
        data({"taskId": UUID_N, "cancelledBy": described(UUID, "Principal who cancelled")}),
        "cancelledBy",
    ),
)
_register(
    "approval.outcome_executed",
    "approval",
    "The outcome actions the task type declares for the decision were executed.",
    data({"taskId": UUID, "outcome": STR, "actions": ARR}),
)
_register(
    "approval.outcome_deferred",
    "approval",
    "An outcome action waits for something (e.g. a skill invocation) before continuing.",
    data({"taskId": UUID, "outcome": STR, "waitingAction": OBJ, "reason": STR, "details": OBJ}),
)
_register(
    "approval.outcome_failed",
    "approval",
    "An outcome action failed; the remaining actions stay for replay.",
    data(
        {
            "taskId": UUID,
            "outcome": STR,
            "failedAction": OBJ,
            "actions": ARR,
            "failureWorkTaskId": UUID_N,
        }
    ),
)

# --- credentials and identities ---------------------------------------------

_register(
    "api_key.created",
    "api_key",
    "An API key was issued to a principal.",
    data({"principalId": UUID, "keyPrefix": STR, "permissions": ARR}),
)
_register(
    "api_key.revoked",
    "api_key",
    "An API key was revoked.",
    data({"keyPrefix": STR}, {"breakGlass": BOOL, "issuedBy": ANY}),
)
_register(
    "api_key.break_glass_issued",
    "api_key",
    "A short-lived break-glass key was issued from the host shell (CP-ADR-0065).",
    data(
        {
            "principalId": UUID,
            "keyPrefix": STR,
            "permissions": ARR,
            "expiresAt": TIME,
            "ttlSeconds": INT,
            "reason": STR,
            "issuedBy": ANY,
        }
    ),
)
_IAM_BINDING_V1 = {
    "principalId": UUID,
    "issuer": STR,
    "iamTenantId": ANY,
    "iamPrincipalId": ANY,
    "permissions": ARR,
}
_IAM_BINDING_VISIBILITY = described(
    {"type": "string", "enum": ["tenant", "members"]},
    "Visibility of the binding: the whole tenant or the workspaces of membership (CP-ADR-0082)",
)
_IAM_BINDING_MOVED = {
    "previousPrincipalId": described(UUID, "Only when the identity moved from another principal"),
}
_register(
    "iam_binding.created",
    "iam_binding",
    "An IAM identity was bound to a local principal (CP-ADR-0053).",
    data(_IAM_BINDING_V1),
    (data({**_IAM_BINDING_V1, "visibility": _IAM_BINDING_VISIBILITY}), "visibility"),
)
_register(
    "iam_binding.updated",
    "iam_binding",
    "The permissions or the visibility of an IAM binding changed.",
    data(_IAM_BINDING_V1, _IAM_BINDING_MOVED),
    (
        data({**_IAM_BINDING_V1, "visibility": _IAM_BINDING_VISIBILITY}, _IAM_BINDING_MOVED),
        "visibility",
    ),
)
_register(
    "iam_binding.revoked",
    "iam_binding",
    "An IAM binding was revoked; the identity no longer enters.",
    data({"principalId": UUID, "issuer": STR, "iamPrincipalId": ANY}),
)
_register(
    "delegation.created",
    "delegation",
    "A human delegated permissions to an agent.",
    data({"humanPrincipalId": UUID, "agentPrincipalId": UUID, "permissions": ARR}),
)
_register(
    "delegation.revoked",
    "delegation",
    "A delegation was revoked.",
    data(),
    (
        data(
            optional={
                "reason": described(
                    STR, "principal_disabled when a side of it was disabled (CP-ADR-0077)"
                )
            }
        ),
        "reason",
    ),
)
_register(
    "principal.created",
    "principal",
    "A principal (human, agent or service) was created.",
    data({"kind": STR, "displayName": STR}),
)
_register(
    "principal.updated",
    "principal",
    "The display name or the profile of a principal changed (CP-ADR-0082). Names of"
    " the changed fields only, never their values: read them via GET /principals/{id}.",
    data(
        {
            "principalId": UUID,
            "version": described(INT, "Version of the principal after the change"),
            "changes": described(
                {"type": "array", "items": STR, "minItems": 1},
                "Changed fields: displayName, profile.<field>",
            ),
        }
    ),
)
_register(
    "principal.disabled",
    "principal",
    "A human or agent principal was disabled: bindings and delegations revoked,"
    " sessions closed, claims freed, runs failed.",
    data(
        {
            "kind": STR,
            "previousStatus": described(STR, "active or paused"),
            "reason": described(
                {"type": ["string", "null"], "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "revokedBindings": described(INT, "IAM bindings revoked"),
            "revokedDelegations": described(INT, "Delegations to or from it revoked"),
            "closedSessions": described(INT, "Open sessions of it or on its behalf closed"),
            "releasedClaims": described(INT, "Active claims released to the queue"),
            "failedRuns": described(INT, "Running runs on the released claims failed"),
            "withdrawnInvocations": described(
                INT, "Skill calls on its authority cancelled, leases it held returned"
            ),
        }
    ),
)
_register(
    "principal.enabled",
    "principal",
    "A disabled (or paused) human or agent principal was enabled. Only the status"
    " comes back: bindings, delegations, sessions and claims closed by :disable stay"
    " closed, IAM entry needs a new binding.",
    data(
        {
            "kind": STR,
            "previousStatus": described(STR, "disabled or paused"),
            "reason": described(
                {"type": ["string", "null"], "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "liveApiKeys": described(
                INT, "Unrevoked, unexpired API keys of it, which authenticate again"
            ),
        }
    ),
)
_register(
    "tenant.bootstrapped",
    "tenant",
    "The tenant was created with its first administrator.",
    data(
        {
            "slug": STR,
            "adminPrincipalId": UUID,
            "apiKeyId": UUID,
            "apiKeyPrefix": STR,
            "iamBindingId": UUID_N,
            "iamPrincipalId": ANY,
        }
    ),
)

# --- organization ------------------------------------------------------------

_register(
    "role.created",
    "role",
    "A role was created, tenant-wide or in a workspace.",
    data({"slug": STR, "name": STR, "workspaceId": UUID_N}),
)
_register(
    "role.updated",
    "role",
    "A role was renamed or redescribed.",
    data({"changes": ANY, "version": INT}),
)
_register(
    "role.assigned",
    "principal",
    "A role was assigned to the principal, tenant-wide or in a workspace subtree.",
    data({"roleId": UUID, "workspaceId": UUID_N}),
)
_register(
    "role.revoked",
    "principal",
    "A role assignment was revoked.",
    data({"roleId": UUID, "workspaceId": UUID_N}),
)
_register("capability.created", "capability", "A capability was created.", data({"name": STR}))
_register(
    "capability.assigned",
    "principal",
    "A capability was assigned to the principal.",
    data({"capabilityId": UUID}),
)
_register(
    "capability.revoked",
    "principal",
    "A capability was revoked from the principal.",
    data({"capabilityId": UUID}),
)
_register(
    "skill.registered",
    "skill",
    "A skill version was registered.",
    data(
        {
            "name": STR,
            "version": ANY,
            "protocol": ANY,
            "invocable": BOOL,
            "sideEffects": ANY,
            "riskLevel": ANY,
        }
    ),
)
_register(
    "skill.updated",
    "skill",
    "The description, status or implementation endpoint of a skill version changed.",
    data(
        {"changedFields": ARR, "rowVersion": INT},
        # ADR-0056, amendment 2026-09-29: only when the endpoint moved.
        {"endpoint": data({"from": STR_N, "to": STR})},
    ),
)
_register("skill.assigned", "principal", "A skill was assigned.", data({"skillId": UUID}))
_register("skill.revoked", "principal", "A skill was revoked.", data({"skillId": UUID}))

_INVOCATION_BASE = {"skillId": UUID}
_INVOCATION_SUCCEEDED_V1 = {
    **_INVOCATION_BASE,
    "skill": STR,
    "version": ANY,
    "attempt": INT,
    "taskId": UUID_N,
    "runId": UUID_N,
    "artifactId": UUID_N,
    "cost": ANY,
}
_register(
    "skill.invocation_requested",
    "skill_invocation",
    "The core was asked to invoke a skill (CP-ADR-0056).",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "sideEffects": ANY,
            "riskLevel": ANY,
            "requestedBy": OBJ,
            "taskId": UUID_N,
            "runId": UUID_N,
            "authorizationBasis": ANY,
        }
    ),
)
_register(
    "skill.invocation_claimed",
    "skill_invocation",
    "An executor took the invocation under a lease.",
    data({**_INVOCATION_BASE, "attempt": INT, "fencingToken": INT, "leaseExpiresAt": TIME}),
)
_register(
    "skill.invocation_succeeded",
    "skill_invocation",
    "The invocation finished; its result is an artifact.",
    data(_INVOCATION_SUCCEEDED_V1),
    (
        data(
            {
                **_INVOCATION_SUCCEEDED_V1,
                "outputs": described(
                    ARR,
                    "Typed outputs of the executed task: {key, type, status"
                    " (created | absent | missing | rejected), artifactId?, reason?};"
                    " empty unless this is the task's execution call",
                ),
            }
        ),
        "outputs (CP-ADR-0072 amendment 2026-10-01)",
    ),
)
_register(
    "skill.invocation_retry_scheduled",
    "skill_invocation",
    "The attempt failed with a retryable error; another one is scheduled.",
    data(
        {
            **_INVOCATION_BASE,
            "attempt": INT,
            "maxAttempts": INT,
            "availableAt": TIME,
            "error": OBJ,
        }
    ),
)
_register(
    "skill.invocation_failed",
    "skill_invocation",
    "The invocation failed for good.",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "attempt": INT,
            "maxAttempts": INT,
            "taskId": UUID_N,
            "runId": UUID_N,
            "error": OBJ,
        }
    ),
)
_register(
    "skill.invocation_cancelled",
    "skill_invocation",
    "The invocation was cancelled.",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "attempt": INT,
            "taskId": UUID_N,
            "runId": UUID_N,
            "code": ANY,
            "reason": STR,
            "wasRunning": BOOL,
            "cancelledBy": UUID_N,
            "initiator": ANY,
        }
    ),
)

# --- workspaces and projects -------------------------------------------------

_register(
    "workspace.created",
    "workspace",
    "A workspace was created.",
    data({"slug": STR, "name": STR, "parentId": UUID_N, "typeKey": ANY}),
)
_register(
    "workspace.updated",
    "workspace",
    "Workspace attributes changed.",
    data({"changes": ANY, "version": INT}),
    (
        data(
            {"changes": ANY, "version": INT},
            {
                "taskTypes": described(
                    nullable({"type": "array", "items": STR}),
                    "New own setting of the allowed task types, when it changed; "
                    "null inherits from the ancestors",
                )
            },
        ),
        "taskTypes (CP-ADR-0008, amendment 2026-10-03 A2)",
    ),
)
_register("workspace.archived", "workspace", "A workspace was archived.", data({"slug": STR}))
_register(
    "workspace.moved",
    "workspace",
    "A workspace moved under another parent.",
    data({"fromParentId": UUID_N, "toParentId": UUID_N}),
)
_register(
    "workspace.member_added",
    "workspace",
    "A principal became a member of the workspace.",
    data({"principalId": UUID}),
)
_register(
    "workspace.member_removed",
    "workspace",
    "A principal stopped being a member of the workspace.",
    data({"principalId": UUID}),
)
_register(
    "workspace_type.created",
    "workspace_type",
    "A workspace type was created.",
    data({"key": STR, "displayName": STR, "allowedChildTypes": ANY}),
)
_register(
    "workspace_type.updated",
    "workspace_type",
    "A workspace type changed.",
    data({"changes": ANY, "version": INT}),
)
_register(
    "workspace_type.archived",
    "workspace_type",
    "A workspace type was archived.",
    data({"key": STR}),
)
_register(
    "knowledge.snapshot_reconciled",
    "workspace",
    "A knowledge snapshot was reconciled into the memory service (CP-ADR-0060).",
    data(
        {
            "snapshotId": ANY,
            "pack": ANY,
            "source": ANY,
            "observedAt": ANY,
            "workspaceId": UUID,
            "rootWorkspaceId": UUID,
            "namespace": STR,
            "entityCount": INT,
            "relationCount": INT,
            "duplicate": BOOL,
            "counters": OBJ,
        }
    ),
)
_register(
    "knowledge.pack_registered",
    "knowledge_pack",
    "A domain knowledge pack version was registered.",
    data({"name": STR, "version": ANY, "status": STR_N}),
    (
        data(
            {
                "name": STR,
                "version": ANY,
                "status": STR_N,
                "scope": described(
                    {"type": "string", "enum": ["common", "tenant"]},
                    "common: a shared pack; tenant: a pack of the event's tenant",
                ),
            }
        ),
        "scope (CP-ADR-0060, amendment 2026-09-28)",
    ),
)
_register(
    "knowledge.packs_configured",
    "workspace",
    "The knowledge packs of a workspace tree were configured.",
    data({"workspaceId": UUID, "namespace": STR, "packs": ARR, "strict": BOOL}),
)
_register(
    "knowledge.document_stored",
    "workspace",
    "A knowledge base document was stored in the memory service"
    " (CP-ADR-0060, amendment 2026-09-28); its text stays out of the journal.",
    data(
        {
            "naturalKey": STR,
            "title": STR,
            "type": STR,
            "workspaceId": UUID,
            "rootWorkspaceId": UUID,
            "namespace": STR,
            "chunkCount": INT,
            "linkCount": INT,
        }
    ),
)
_KNOWLEDGE_CHANGE: JsonSchema = {
    "type": "object",
    "required": ["kind", "key", "change"],
    "properties": {
        "kind": described(STR, "Kind of the memory node, e.g. regulation"),
        "key": described(STR, "Natural key of the node"),
        "change": {"type": "string", "enum": ["opened", "changed", "closed"]},
    },
}
_register(
    "knowledge.changed",
    "workspace",
    "A knowledge snapshot opened, changed or closed documents in memory"
    " (CP-ADR-0076 §6); an empty reconciliation writes no event.",
    data(
        {
            "snapshotId": ANY,
            "pack": ANY,
            "source": ANY,
            "observedAt": ANY,
            "workspaceId": UUID,
            "rootWorkspaceId": UUID,
            "namespace": STR,
            "changes": described(
                {"type": "array", "items": _KNOWLEDGE_CHANGE},
                "Natural keys the memory service reported for the snapshot",
            ),
            "truncated": described(
                BOOL, "The memory service cut the list; the counters stay complete"
            ),
            "counters": OBJ,
        }
    ),
)
_register(
    "project.created",
    "project",
    "A project was created on a workspace (ADR-0031).",
    data(
        {
            "workspaceId": UUID,
            "parentProjectId": UUID_N,
            "templateKey": STR,
            "templateVersion": INT,
            "statusKey": STR,
            "systemStatusCategory": STR,
        }
    ),
)
_register(
    "project.updated",
    "project",
    "Project attributes changed.",
    data({"changes": ARR, "version": INT}),
)
_register(
    "project.status_changed",
    "project",
    "The project moved to another status.",
    data(
        {
            "fromStatusKey": STR,
            "fromSystemStatusCategory": STR,
            "statusKey": STR,
            "systemStatusCategory": STR,
            "comment": ANY,
            "version": INT,
        }
    ),
)
_register(
    "project.archived",
    "project",
    "The project was archived.",
    data({"workspaceId": UUID, "version": INT}),
)
_register(
    "project.config_revision_created",
    "project",
    "A new configuration revision of the project was drafted.",
    data({"revision": INT, "revisionId": UUID, "comment": ANY}),
)
_register(
    "project.config_revision_activated",
    "project",
    "A configuration revision became the active one.",
    data({"revision": INT, "revisionId": UUID, "version": INT}),
)
_register(
    "project_template.created",
    "project_template",
    "A project template version was created.",
    data({"key": STR, "version": INT, "displayName": STR, "initialStatus": ANY}),
)
_register(
    "project_template.deprecated",
    "project_template",
    "A project template version was deprecated.",
    data({"key": STR, "version": INT}),
)

_EXTERNAL_REFERENCE = data(
    {
        "externalReferenceId": UUID,
        "externalSystem": STR,
        "externalType": STR,
        "externalId": STR,
    }
)
for _entity in ("project", "task"):
    _register(
        f"{_entity}.external_reference_added",
        _entity,
        f"A reference to an external system was attached to the {_entity} (ADR-0047).",
        _EXTERNAL_REFERENCE,
    )
    _register(
        f"{_entity}.external_reference_updated",
        _entity,
        f"An external reference of the {_entity} changed.",
        _EXTERNAL_REFERENCE,
    )

# --- tasks -------------------------------------------------------------------

_register(
    "task.created",
    "task",
    "A task was created.",
    data(
        {
            "publicId": STR,
            "title": STR,
            "status": STR,
            "systemStatusCategory": STR,
            "typeKey": STR,
            "typeVersion": INT,
            "priority": ANY,
            "workspaceId": UUID_N,
            "startDate": STR_N,
            "dueDate": STR_N,
            "customFields": BOOL,
            "goalId": UUID_N,
            "origin": ANY,
            "acceptanceChecks": INT,
        }
    ),
)
_register(
    "task.updated",
    "task",
    "Task attributes or its status changed.",
    data(
        {"publicId": STR, "changes": ANY, "version": INT},
        {"fromStatus": STR, "status": STR, "systemStatusCategory": STR},
    ),
)
_register(
    "task.type_migrated",
    "task",
    "The task was moved to another version of its type (ADR-0048, amendment 2026-09-30).",
    data(
        {
            "publicId": STR,
            "typeKey": STR,
            "fromTypeVersion": INT,
            "typeVersion": INT,
            "fromStatus": STR,
            "status": STR,
            "systemStatusCategory": STR,
            "trigger": described(STR, "task (one task) or bulk (:migrate-tasks of a version)"),
            "version": INT,
        }
    ),
)
_register(
    "task.claimed",
    "task",
    "An executor claimed the task under a lease.",
    data(
        {
            "publicId": STR,
            "claimId": UUID,
            "sessionId": UUID_N,
            "holderId": UUID,
            "fencingToken": INT,
            "expiresAt": TIME,
            "status": STR,
            "systemStatusCategory": STR,
            "version": INT,
        }
    ),
)
_register(
    "task.completed",
    "task",
    "The task reached its completion status.",
    data(
        {"publicId": STR, "status": STR, "systemStatusCategory": STR, "version": INT},
        {"verificationId": UUID, "attempt": INT},
    ),
)
_register(
    "task.relation_added",
    "task",
    "A relation to another task was added.",
    data({"relationId": UUID, "toTaskId": UUID, "type": STR}),
)
_register(
    "task.relation_removed",
    "task",
    "A relation between tasks was removed.",
    data({"relationId": UUID, "fromTaskId": UUID, "toTaskId": UUID, "type": STR}),
)
_COMMENT = data(
    {
        "commentId": UUID,
        "authorPrincipalId": UUID,
        "version": INT,
        "bodyLength": INT,
        "runId": UUID_N,
        "artifactId": UUID_N,
    }
)
_register("task.comment_added", "task", "A comment was added to the task.", _COMMENT)
_register("task.comment_edited", "task", "A task comment was edited.", _COMMENT)
_register(
    "task.context_pack_recorded",
    "task",
    "The context pack assembled for the task on claim was recorded (CP-ADR-0064).",
    data(
        {
            "publicId": STR,
            "contextPackId": UUID,
            "claimId": UUID_N,
            "asOf": STR,
            "asOfMode": STR,
            "entities": INT,
            "facts": INT,
            "snapshots": INT,
        }
    ),
)
_register(
    "task.completion_work_executed",
    "task",
    "The completion work the task type declares was executed (ADR-0061).",
    data({"publicId": STR, "taskTypeId": UUID, "actions": ARR}),
)
_register(
    "task.completion_work_failed",
    "task",
    "An action of the completion work failed; the completion stands.",
    data({"publicId": STR, "taskTypeId": UUID, "failedAction": OBJ, "actions": ARR}),
)
_VERIFICATION = {
    "publicId": STR,
    "taskId": UUID,
    "verificationId": UUID,
    "attempt": INT,
    "trigger": ANY,
    "checks": INT,
}
_register(
    "task.verification_started",
    "task",
    "A verification attempt of the task's acceptance checks opened (CP-ADR-0067).",
    data(_VERIFICATION, {"triggerRef": ANY}),
)
_register(
    "task.verified",
    "task",
    "Every acceptance check passed; the task is complete.",
    data({**_VERIFICATION, "results": ARR, "artifactId": UUID}),
)
_register(
    "task.verification_failed",
    "task",
    "An acceptance check failed; the task went back to its executor or got blocked.",
    data(
        {
            **_VERIFICATION,
            "results": ARR,
            "failedCheck": ANY,
            "reason": ANY,
            "consecutiveFailures": INT,
            "blocked": BOOL,
            "fromStatus": STR,
            "status": STR,
            "systemStatusCategory": STR,
        }
    ),
)
_TASK_TYPE_CREATED_V1: dict[str, JsonSchema] = {
    "key": STR,
    "version": INT,
    "displayName": STR,
    "initialStatus": ANY,
    "completionStatus": ANY,
    "execution": ANY,
    "declaresApprovalOutcomes": BOOL,
    "declaresContextProfile": BOOL,
    "declaresInstructions": BOOL,
    "declaresCompletionWork": BOOL,
}
_TASK_TYPE_CREATED_V2 = {
    **_TASK_TYPE_CREATED_V1,
    "declaresArtifactSchema": BOOL,
    "inputs": described(INT, "Number of declared artifact inputs"),
    "outputs": described(INT, "Number of declared artifact outputs"),
}
_register(
    "task_type.created",
    "task_type",
    "A task type version was created (ADR-0048).",
    data(_TASK_TYPE_CREATED_V1),
    (data(_TASK_TYPE_CREATED_V2), "declaresArtifactSchema, inputs, outputs (CP-ADR-0072)"),
    (
        data(
            {
                **_TASK_TYPE_CREATED_V2,
                "executorRoles": described(
                    {"type": "array", "items": STR},
                    "Slugs of the roles a person needs to take work of the version",
                ),
            }
        ),
        "executorRoles (CP-ADR-0048, amendment 2026-10-03 A1)",
    ),
)
_register(
    "task_type.deprecated",
    "task_type",
    "A task type version was deprecated.",
    data({"key": STR, "version": INT}),
)

# --- claims, sessions, runs --------------------------------------------------

_register(
    "claim.released",
    "claim",
    "The claim on a task ended: completed, released, cancelled or superseded.",
    data({"taskId": UUID, "reason": ANY}, {"taskStatus": STR, "taskSystemStatusCategory": STR}),
)
_register(
    "claim.expired",
    "claim",
    "The lease of a claim ran out.",
    data({"taskId": UUID}, {"reason": ANY, "taskStatus": STR, "taskSystemStatusCategory": STR}),
)
_register(
    "session.opened",
    "session",
    "A harness opened a work session.",
    data(
        {
            "clientName": ANY,
            "harnessType": ANY,
            "controlLevel": ANY,
            "protocolVersion": ANY,
            "onBehalfOf": UUID_N,
            "expiresAt": TIME,
        }
    ),
)
_register(
    "session.expired",
    "session",
    "A work session expired; its claims were released.",
    data({"expiresAt": ANY}, {"releasedClaims": ARR}),
)
_register(
    "session.closed",
    "session",
    "A work session was closed.",
    data({"releasedClaims": ARR}),
    (
        data(
            {"releasedClaims": ARR},
            {
                "reason": described(
                    STR,
                    "principal_disabled when its principal, or the human it acted for,"
                    " was disabled (CP-ADR-0077)",
                )
            },
        ),
        "reason",
    ),
)

_RUN = {"taskId": UUID}
_RUN_STARTED_V1 = {
    **_RUN,
    "claimId": UUID,
    "attempt": INT,
    "fencingToken": INT,
    "instructionsHash": ANY,
    "instructionsRefs": ANY,
}
_register(
    "run.started",
    "run",
    "An execution attempt started under a claim.",
    data(_RUN_STARTED_V1),
    (
        data(
            {
                **_RUN_STARTED_V1,
                "agentRevisionId": described(
                    UUID_N,
                    "Agent revision the run goes by (CP-ADR-0073 §7); "
                    "null for executors that are not registered agents",
                ),
            }
        ),
        "agentRevisionId",
    ),
)
_register(
    "run.succeeded",
    "run",
    "The run finished successfully.",
    data({**_RUN, "attempt": INT, "taskCompleted": BOOL}),
)
_register(
    "run.failed",
    "run",
    "The run failed.",
    data({**_RUN, "reason": ANY, "attempt": INT}),
)
_register(
    "run.suspended",
    "run",
    "The run was suspended, e.g. to wait for a decision.",
    data({**_RUN, "reason": ANY, "attempt": INT}, {"waitingForApprovalId": UUID_N}),
)
_register(
    "run.checkpointed",
    "run",
    "The run left a checkpoint.",
    data({**_RUN, "checkpointId": UUID, "seq": INT, "kind": STR}),
)
_register(
    "run.handoff_prepared",
    "run",
    "The run prepared a handoff to another executor.",
    data({**_RUN, "claimId": UUID, "checkpointId": UUID, "fencingToken": INT, "reason": ANY}),
)
_register(
    "run.cancel_requested",
    "run",
    "Cancellation of the run was requested.",
    data({**_RUN, "attempt": INT}, {"reason": ANY, "controlMessageId": UUID}),
)
_register(
    "run.cancelled",
    "run",
    "The run was cancelled.",
    data({**_RUN, "reason": ANY, "attempt": INT}, {"controlMessageId": UUID}),
)
_register(
    "run.manifest_compiled",
    "run",
    "The effective harness manifest of the run was compiled (ADR-0043). No longer "
    "written since ADR-0073; kept for events already in the journal.",
    data(
        {
            **_RUN,
            "manifestId": UUID,
            "version": INT,
            "baseHash": ANY,
            "reason": ANY,
            "modelAttempt": ANY,
            "supersedesVersion": ANY,
        }
    ),
)
_register(
    "run.manifest_ephemeral_recorded",
    "run",
    "An ephemeral manifest change was recorded (ADR-0043). No longer written since "
    "ADR-0073; kept for events already in the journal.",
    data({**_RUN, "manifestId": UUID, "version": INT, "seq": INT, "kind": ANY}),
)
_CONTROL_MESSAGE = {
    **_RUN,
    "controlMessageId": UUID,
    "seq": INT,
    "operation": ANY,
    "status": STR,
    "causalPosition": ANY,
}
_register(
    "run.control_message.accepted",
    "run",
    "A control message for the active turn was accepted (ADR-0044).",
    data(_CONTROL_MESSAGE),
)
for _status in ("applied", "rejected", "superseded"):
    _register(
        f"run.control_message.{_status}",
        "run",
        f"A control message was {_status}.",
        data({**_CONTROL_MESSAGE, "safeBoundary": ANY}),
    )
_register(
    "run.child.launched",
    "run",
    "The run launched a child task under a handle (ADR-0046).",
    data(
        {
            **_RUN,
            "childHandleId": UUID,
            "childTaskId": UUID,
            "childTaskPublicId": STR,
            "correlationId": ANY,
            "cancellationPolicy": ANY,
            "depth": INT,
            "grantSizes": OBJ,
        }
    ),
)
_CHILD = {"childHandleId": UUID, "correlationId": ANY}
_register(
    "run.child.started",
    "run",
    "A run of the child task started.",
    data({**_CHILD, "childTaskId": UUID, "childRunId": UUID, "attempt": INT}),
)
_register(
    "run.child.resolved",
    "run",
    "The child handle was resolved with the child's outcome.",
    data(
        {
            **_CHILD,
            "childRunId": UUID_N,
            "outcome": ANY,
            "resultHash": ANY,
            "artifactRefs": ARR,
        }
    ),
)
_register(
    "run.child.revoked",
    "run",
    "The child handle was revoked.",
    data({**_CHILD, "childTaskId": UUID, "reason": ANY}),
)
_register(
    "run.child.cancel_requested",
    "run",
    "Cancellation of the child run was requested.",
    data({**_CHILD, "childRunId": UUID_N, "controlMessageId": UUID, "reason": ANY}),
)

# --- artifacts, observations, goals -----------------------------------------

_ARTIFACT_CREATED_V1 = {
    "type": STR,
    "name": STR,
    "taskId": UUID_N,
    "runId": UUID_N,
    "uri": ANY,
    "supersedesArtifactId": UUID_N,
}
_ARTIFACT_CREATED_OPTIONAL = {
    "skillInvocationId": UUID,
    "ruleEvaluationId": UUID,
    "verificationId": UUID,
}
_CONTENT_STATE: JsonSchema = {"type": "string", "enum": ["none", "stored", "purged"]}
_SHA256: JsonSchema = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_register(
    "artifact.created",
    "artifact",
    "An artifact was recorded.",
    data(_ARTIFACT_CREATED_V1, _ARTIFACT_CREATED_OPTIONAL),
    (
        data(
            {
                **_ARTIFACT_CREATED_V1,
                "sizeBytes": described(INT_N, "Size of the stored content; null without one"),
                "mediaType": STR_N,
                "sha256": nullable(_SHA256),
                "contentState": _CONTENT_STATE,
                "typeVersion": described(
                    INT_N, "Version of the registered artifact type it was checked against"
                ),
            },
            _ARTIFACT_CREATED_OPTIONAL,
        ),
        "sizeBytes, mediaType, sha256, contentState, typeVersion (CP-ADR-0072)",
    ),
)
_register(
    "artifact.content_read",
    "artifact",
    "The bytes of an artifact were handed out (CP-ADR-0072 §5).",
    data(
        {
            "artifactId": UUID,
            "taskId": UUID_N,
            "forTaskId": described(UUID_N, "Receiving task when read as its input"),
            "runId": described(UUID_N, "The reader's running run on that task, if any"),
            "sha256": _SHA256,
            "sizeBytes": INT,
        }
    ),
)
_register(
    "artifact.content_purged",
    "artifact",
    "The bytes of an artifact were removed by an administrator; the record stays.",
    data(
        {
            "artifactId": UUID,
            "taskId": UUID_N,
            "sha256": _SHA256,
            "sizeBytes": INT,
            "reason": described(
                {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "objectDeleted": described(
                BOOL, "False when other artifacts or uploads still need the object"
            ),
        }
    ),
)
_register(
    "artifact_type.created",
    "artifact_type",
    "An artifact type version was created (CP-ADR-0072).",
    data(
        {
            "key": STR,
            "version": INT,
            "mediaTypes": ARR,
            "maxBytes": INT,
            "declaresMetadataSchema": BOOL,
        }
    ),
)
# --- connections (CP-ADR-0079) --------------------------------------------------
_register(
    "connection_type.published",
    "connection_type",
    "A version of a connection type was published (CP-ADR-0079 §2); a repeat"
    " of the same spec records nothing.",
    data({"key": STR, "version": INT, "auth": ARR}),
)
_CONNECTION_STATUS: JsonSchema = {
    "type": "string",
    "enum": ["pending", "active", "expired", "revoked"],
}
_register(
    "connection.created",
    "connection",
    "A connection was created (CP-ADR-0079 §3); it waits for authorization.",
    data({"key": STR, "type": STR, "typeVersion": INT, "status": _CONNECTION_STATUS}),
)
_register(
    "connection.updated",
    "connection",
    "The display name, settings or type version of a connection changed; only the"
    " names of the changed fields, never their values.",
    data(
        {
            "key": STR,
            "version": INT,
            "changes": described(
                ARR, "Names of the changed fields: displayName, settings, typeVersion"
            ),
        }
    ),
)
_register(
    "connection.status_changed",
    "connection",
    "The status of a connection moved without a new authorization — the connector"
    " reported that access is lost. Codes only, no text of the provider.",
    data(
        {
            "key": STR,
            "type": STR,
            "from": _CONNECTION_STATUS,
            "to": _CONNECTION_STATUS,
            "reason": described(STR_N, "A code, e.g. refresh_rejected"),
            "connectedBy": described(UUID_N, "Who connected it last; null if nobody has"),
        }
    ),
)
_CONNECTION_AUTH: JsonSchema = {"type": "string", "enum": ["oauth2", "token"]}
_register(
    "connection.authorized",
    "connection",
    "A connection became active: the OAuth code was exchanged in the secret store or"
    " a key of the connection was entered. No value, no account, no provider text.",
    data(
        {
            "key": STR,
            "type": STR,
            "auth": _CONNECTION_AUTH,
            "previousStatus": _CONNECTION_STATUS,
            "connectedBy": described(UUID, "The principal that connected it"),
        }
    ),
)
_register(
    "connection.authorization_failed",
    "connection",
    "An OAuth callback of a live state did not connect: consent denied, a provider"
    " error, an invalid account, the initiator no longer authorized or a failed"
    " exchange. The status of the connection did not change.",
    data(
        {
            "key": STR,
            "type": STR,
            "reason": described(STR, "A code, e.g. consent_denied, oauth_exchange_failed"),
            "initiatedBy": described(UUID, "The principal that started :authorize"),
        }
    ),
)
_register(
    "connection.revoked",
    "connection",
    "A connection was revoked (CP-ADR-0079 §10): its material is deleted from the secret"
    " store and no agent's policy names it any more. A repeated revocation records nothing.",
    data(
        {
            "key": STR,
            "type": STR,
            "previousStatus": described(
                _CONNECTION_STATUS, "pending, active or expired: the status before the revocation"
            ),
        }
    ),
)
_register(
    "connection_type.oauth_app_set",
    "connection_type",
    "The OAuth application of a connection type was written to the secret store;"
    " neither the client id nor the secret is in the event.",
    data({"type": STR, "created": described(BOOL, "The first write, not a replacement")}),
)
# --- agent registry (CP-ADR-0073) ---------------------------------------------

_AGENT_HASH: JsonSchema = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_AGENT_STATE: JsonSchema = {"type": "string", "enum": ["running", "stopped"]}
_AGENT_PHASE: JsonSchema = {
    "type": "string",
    "enum": [
        "pending",
        "running",
        "waiting_for_node",
        "crash_looping",
        "node_unavailable",
        "stopped",
    ],
}
_register(
    "agent.revision_published",
    "agent",
    "A new immutable revision of an agent spec was published (CP-ADR-0073 §2).",
    data(
        {
            "key": STR,
            "revision": INT,
            "specHash": _AGENT_HASH,
            "previousRevision": described(INT_N, "Null for the first revision of the key"),
            "executorKind": described(STR_N, "Null for an identity without placement"),
            "placed": described(BOOL, "False for placement none"),
            "permissionsChanged": described(
                BOOL, "Identity (roles, permissions, capabilities) differs from the previous one"
            ),
        }
    ),
)
_register(
    "agent.state_changed",
    "agent",
    "The desired state or replica count of an agent changed; no new revision.",
    data(
        {
            "key": STR,
            "state": _AGENT_STATE,
            "replicas": INT,
            "previousState": described(
                {"type": ["string", "null"], "enum": [*_AGENT_STATE["enum"], None]},
                "Null when the agent is first published",
            ),
            "previousReplicas": INT_N,
        }
    ),
)
_register(
    "agent.status_changed",
    "agent",
    "The observed state of an agent changed: phase, reason, node or revision.",
    data(
        {
            "key": STR,
            "phase": _AGENT_PHASE,
            "previousPhase": described(
                {"type": ["string", "null"], "enum": [*_AGENT_PHASE["enum"], None]},
                "Null on the first report",
            ),
            "reasonCode": described(STR_N, "Why it is not running, e.g. no_matching_node"),
            "node": STR_N,
            "observedRevision": INT_N,
            "observedAt": TIME,
        }
    ),
)
_register(
    "agent.retired",
    "agent",
    "An agent was retired: stopped, binding revoked, history kept.",
    data(
        {
            "key": STR,
            "revision": described(INT, "The last revision of the agent"),
            "principalId": UUID_N,
            "reason": described(
                {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "releasedClaims": described(INT, "Active claims of the agent released to the queue"),
        }
    ),
)
_register(
    "agent.identity_replaced",
    "agent",
    "A service agent moved to a new IAM identity; its principal stayed (CP-ADR-0073).",
    data(
        {
            "key": STR,
            "revision": described(INT, "The revision whose rights the new identity got"),
            "principalId": UUID,
            "issuer": STR,
            "iamTenantId": UUID,
            "iamPrincipalId": UUID,
            "previousIssuer": STR_N,
            "previousIamTenantId": UUID_N,
            "previousIamPrincipalId": UUID_N,
            "reason": described(
                {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
        }
    ),
)
_register(
    "agent.secret_set",
    "agent",
    "A secret of an agent was set by name (CP-ADR-0079 §11): the value went to the secret"
    " store in transit; the event carries the name only.",
    data(
        {
            "agentKey": STR,
            "name": STR,
            "created": described(BOOL, "The first value under the name, not a replacement"),
        }
    ),
)
_register(
    "agent.secret_deleted",
    "agent",
    "A secret of an agent was deleted with every version from the secret store (CP-ADR-0079 §11).",
    data({"agentKey": STR, "name": STR}),
)
_register(
    "observation.recorded",
    "observation",
    "An observation was recorded (ADR-0057).",
    data(
        {"kind": STR, "content": ANY, "observedAt": TIME},
        {
            "source": STR,
            "dedupKey": STR,
            "externalRef": OBJ,
            "assertions": ARR,
            "data": OBJ,
            "taskId": UUID,
            "runId": UUID,
            "workspaceId": UUID,
            "supersedes": UUID,
        },
    ),
)
_register(
    "goal.created",
    "goal",
    "A goal was created (CP-ADR-0062).",
    data(
        {
            "goalId": UUID,
            "title": STR,
            "status": STR,
            "workspaceId": UUID_N,
            "ownerId": UUID_N,
            "parentGoalId": UUID_N,
            "criteriaCount": INT,
            "createdFrom": ANY,
        }
    ),
)
_register(
    "goal.updated",
    "goal",
    "Goal attributes or its status changed.",
    data({"goalId": UUID, "changes": ANY, "version": INT}, {"fromStatus": STR, "status": STR}),
)

# --- work derivation rules ---------------------------------------------------

_RULE_SUMMARY = {
    "ruleId": UUID,
    "key": STR,
    "version": INT,
    "status": STR,
    "workspaceId": UUID_N,
    "goalId": UUID_N,
    "trigger": OBJ,
    "skill": ANY,
    "action": OBJ,
}
_register("rule.created", "rule", "A work rule was created (CP-ADR-0063).", data(_RULE_SUMMARY))
_register(
    "rule.updated",
    "rule",
    "A work rule changed.",
    data({**_RULE_SUMMARY, "changes": ARR}),
)
_register("rule.enabled", "rule", "A work rule was enabled.", data(_RULE_SUMMARY))
_register("rule.disabled", "rule", "A work rule was disabled.", data(_RULE_SUMMARY))
_register("rule.archived", "rule", "A work rule was archived.", data(_RULE_SUMMARY))
_register(
    "rule.evaluated",
    "rule",
    "A work rule was evaluated against a trigger.",
    data(
        {
            "ruleId": UUID,
            "ruleKey": STR,
            "ruleVersion": INT,
            "evaluationId": UUID,
            "triggerRef": ANY,
            "trigger": OBJ,
            "result": STR,
            "conditionMatched": ANY,
            "evidence": ARR,
            "skillInvocationId": UUID_N,
            "work": ANY,
            "error": nullable(OBJ),
        }
    ),
)
_WORK = {
    "ruleId": UUID,
    "ruleKey": STR,
    "ruleVersion": INT,
    "evaluationId": UUID,
    "taskId": UUID,
    "publicId": STR,
    "evidence": ARR,
    "action": STR,
    "dedupKey": STR,
}
_register(
    "work.derived",
    "task",
    "A rule derived new work.",
    data({**_WORK, "created": BOOL}, {"approvalId": UUID}),
)
_register(
    "work.reconciled",
    "task",
    "A rule updated, cancelled or completed work: the work it derived earlier, "
    "or the task an observation is bound to.",
    data(
        {**_WORK, "changes": ARR},
        {
            "verificationId": UUID,
            "check": ANY,
            # "task": the work is the task an observation is bound to, and
            # dedupKey its pseudo-key task:<id> (CP-ADR-0063, amendment Zh4).
            "target": STR,
        },
    ),
)

# --- processes (CP-ADR-0074, CP-ADR-0076) -----------------------------------
#
# The engine's own decision journal is process_instance_events (CP-ADR-0074
# §5); these events tell the rest of the platform what happened to a case.
# The case projection (``memory``) carries only the fields the process
# declares for memory, evaluated by the core (CP-ADR-0076 §2).

_PROCESS_HASH: JsonSchema = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_PROCESS_INSTANCE = {
    "instanceId": UUID,
    "definitionKey": STR,
    "version": described(INT, "Version of the process the instance is pinned to"),
    "instanceKey": described(STR, "Value of start.key; unique per definition key"),
}


def _instance_data(
    required: Mapping[str, JsonSchema], optional: Mapping[str, JsonSchema] | None = None
) -> JsonSchema:
    # The application adds the workspace when it records the event; the
    # engine's own intents (its replayed journal) do not carry it.
    workspace = described(UUID_N, "Workspace of the instance: the scope of its case in memory")
    return data(required, {"workspaceId": workspace, **(optional or {})})


_CASE_PROJECTION = described(
    nullable(OBJ),
    "Case projection {case, facts, entities, documents} evaluated from the"
    " process's memory section; null when the process declares none",
)
_PROCESS_ERROR: JsonSchema = {
    "type": "object",
    "required": ["type"],
    "properties": {"type": STR, "status": INT_N, "detail": STR_N},
}
_REASON = described(
    {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
    "Reason given; credential-shaped material redacted, cut to the limit",
)
_register(
    "process.definition_published",
    "process_definition",
    "A new immutable version of a process was published (CP-ADR-0074 §3).",
    data(
        {
            "key": STR,
            "version": INT,
            "definitionHash": _PROCESS_HASH,
            "previousVersion": described(INT_N, "Null for the first version of the key"),
            "workspaceId": UUID_N,
            "identityAgent": described(STR_N, "Agent key the process acts as"),
            "displayName": STR,
            "governedBy": described(
                ARR, "Regulations of the process as a whole: {document, section}"
            ),
            "elements": described(
                ARR,
                "Stages, steps, milestones and decision tables: {id, kind, parent,"
                " displayName, governedBy} — what the memory projection of the"
                " version is built from (CP-ADR-0076 §3)",
            ),
        }
    ),
)
_register(
    "process.definition_retired",
    "process_definition",
    "Every version of a process was retired: no new instances, open ones run to the end"
    " (CP-ADR-0074, amendment Zh2).",
    data(
        {
            "key": STR,
            "latestVersion": INT,
            "workspaceId": UUID_N,
            "reason": STR,
            "openInstances": described(INT, "Open instances of every workspace of the key"),
            "byVersion": described(ARR, "{version, openInstances} of the versions with open ones"),
        }
    ),
)
_register(
    "process.definition_restored",
    "process_definition",
    "A retired process is back in use: a package apply installed it as it is"
    " (CP-ADR-0074, amendment Zh3).",
    data(
        {
            "key": STR,
            "latestVersion": INT,
            "packageKey": STR_N,
            "packageVersion": STR_N,
        }
    ),
)
_register(
    "process.started",
    "process_instance",
    "A process instance started from its start trigger.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "triggerEventId": described(UUID_N, "Journal event that started it"),
            "triggerType": STR,
            "memory": _CASE_PROJECTION,
        }
    ),
)
_register(
    "process.correlated",
    "process_instance",
    "An event matched start.key or a correlate rule of a running instance.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "triggerEventId": UUID_N,
            "triggerType": STR,
            "changedFields": described(ARR, "Data paths the event changed"),
        }
    ),
)
_register(
    "process.data_changed",
    "process_instance",
    "Instance data changed; timers depending on the fields were recomputed.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "changedFields": described(ARR, "Data paths that changed"),
            "element": described(STR_N, "Element whose output or set changed them"),
            "memory": _CASE_PROJECTION,
        }
    ),
)
for _moment in ("entered", "exited"):
    _register(
        f"process.stage_{_moment}",
        "process_instance",
        f"A stage of the case was {_moment}.",
        _instance_data({**_PROCESS_INSTANCE, "stage": STR}),
    )
_register(
    "process.milestone_reached",
    "process_instance",
    "A milestone of the case was reached.",
    _instance_data({**_PROCESS_INSTANCE, "milestone": STR, "stage": STR_N}),
)
_register(
    "process.milestone_lost",
    "process_instance",
    "A reached milestone stopped holding: its guard is false again (a standing goal"
    " is no longer met); it is reached again when the guard holds again.",
    _instance_data({**_PROCESS_INSTANCE, "milestone": STR, "stage": STR_N}),
)
_register(
    "process.timer_fired",
    "process_instance",
    "A timer of the instance fired; the engine takes it as its next event.",
    _instance_data({**_PROCESS_INSTANCE, "timerId": UUID, "element": STR, "dueAt": TIME}),
)
_register(
    "process.timer_rescheduled",
    "process_instance",
    "A pending timer moved: data or a calendar it reads changed, or on resume.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "timerId": UUID,
            "element": STR,
            "previousDueAt": TIME,
            "dueAt": TIME,
            "provisional": described(BOOL, "Computed on a provisional calendar year"),
            "cause": described(
                STR,
                "data_changed, calendar_changed, resumed or migrated (deadlines"
                " recomputed by the new version, CP-ADR-0074 amendment 2026-09-29 §11)",
            ),
            "changedFields": ARR,
        }
    ),
)
# An addressee in the form of notification rules (CP-ADR-0078 §3): one of
# principalId and roleId set.
_ADDRESSEE: JsonSchema = {
    "type": "object",
    "required": ["principalId", "roleId", "workspaceId"],
    "properties": {"principalId": UUID_N, "roleId": UUID_N, "workspaceId": UUID_N},
    "anyOf": [
        {"properties": {"principalId": UUID, "roleId": {"type": "null"}}},
        {"properties": {"principalId": {"type": "null"}, "roleId": UUID}},
    ],
}
_ESCALATED = {
    **_PROCESS_INSTANCE,
    "element": STR,
    "level": described(INT, "1-based escalation level of the step"),
    "action": described(STR, "remind, reassign, notify or raise"),
    "taskId": UUID_N,
    "to": described(ARR, "Resolved principals the action addresses"),
}
_register(
    "process.escalated",
    "process_instance",
    "An escalation level of a step fired.",
    _instance_data(_ESCALATED),
    (
        # The core resolves the targets when it records the event; the
        # engine's own intents (its replayed journal) do not carry them.
        _instance_data(
            _ESCALATED,
            {
                "addressees": described(
                    {"type": "array", "items": nullable(_ADDRESSEE)},
                    "One per item of to, in its order: the addressee {principalId, roleId,"
                    " workspaceId} of the target (a role is the one of the instance's"
                    " workspace); null when the target does not resolve",
                ),
                "unresolved": described(
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["index", "target", "reason"],
                            "properties": {
                                "index": described(INT, "0-based position in to"),
                                "target": described(STR, "The target as to names it"),
                                "reason": described(
                                    STR, "unknown_role, unknown_agent or unknown_principal"
                                ),
                            },
                        },
                    },
                    "Why the null addressees did not resolve; empty when all did",
                ),
            },
        ),
        "addressees, unresolved: addressees of the to targets (CP-ADR-0078, amendment 2026-09-30)",
    ),
)
_register(
    "process.suspended",
    "process_instance",
    "The instance was suspended; its timers froze.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "cause": described(STR, "event (a suspend block) or operator"),
            "reason": _REASON,
        }
    ),
)
_register(
    "process.resumed",
    "process_instance",
    "The instance was resumed; frozen timers got their remaining time back.",
    _instance_data({**_PROCESS_INSTANCE, "cause": described(STR, "event or operator")}),
)
_register(
    "process.compensated",
    "process_instance",
    "Compensations of completed steps ran in reverse order.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "scope": described(STR, "all, or the id of the scope compensated"),
            "steps": described(ARR, "Ids of the steps compensated, in the order run"),
        }
    ),
)
_register(
    "process.recall_completed",
    "process_instance",
    "Memory answered a recall step; the answer is in the instance journal (CP-ADR-0076 §4).",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "step": STR,
            "recallId": described(UUID, "Id of the recall intent"),
            "asOf": TIME,
            "nodeCount": INT,
            "edgeCount": INT,
            "truncated": BOOL,
            "resultHash": _PROCESS_HASH,
        }
    ),
)
_register(
    "process.recall_timed_out",
    "process_instance",
    "A recall step got no answer in time; the step's onTimeout runs.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "step": STR,
            "recallId": UUID,
            "reason": described(STR, "timeout, memory_unavailable or memory_disabled"),
        }
    ),
)
_register(
    "process.migrated",
    "process_instance",
    "The instance moved to another version by the process's migration map.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "fromVersion": INT,
            "map": described(OBJ, "Old element id -> new element id"),
            "policy": STR,
        }
    ),
)
_register(
    "process.completed",
    "process_instance",
    "The instance completed with an outcome.",
    _instance_data({**_PROCESS_INSTANCE, "outcome": STR, "memory": _CASE_PROJECTION}),
)
_register(
    "process.cancelled",
    "process_instance",
    "An operator cancelled the instance.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            "reason": _REASON,
            "compensated": described(BOOL, "Compensations ran before the cancel"),
        }
    ),
)
_register(
    "process.failed",
    "process_instance",
    "An error reached the top of the instance without a handler.",
    _instance_data({**_PROCESS_INSTANCE, "error": _PROCESS_ERROR, "element": STR_N}),
)

# --- process steps and deadlines (CP-ADR-0074 amendment 2026-09-29 §13, CP-ADR-0078)
#
# Step events are a projection of the instance journal written by the
# application; the SLA facts are emitted by the engine when a deadline timer
# fires. Neither carries instance data: they are read with events.read on the
# workspace of the process, not processes.read.

_STEP = {
    "element": described(STR, "Id of the waiting step"),
    "stage": described(STR_N, "Stage the step belongs to; null outside stages"),
    "stepKind": described(STR, "human, approve, call, recall, listen or wait"),
    "attempt": described(INT, "1-based number of the entry into this element in the instance"),
    "activityId": described(UUID, "Activity of this attempt"),
    "enteredAt": TIME,
}
_STEP_DUE = described(nullable(TIME), "Declared deadline of the step; null without due")
_SLA_OWNER = described(
    nullable(_ADDRESSEE),
    "Addressee {principalId, roleId, workspaceId}, one of principalId and roleId set:"
    " the first resolvable candidate of spec.owner; null when none resolves",
)
_SLA_ASSIGNEE = described(
    nullable(_ADDRESSEE),
    "Addressee {principalId, roleId, workspaceId} the step is assigned to;"
    " null for the process scope or an unassigned step",
)
_SLA = {
    "scope": described(STR, "step or process"),
    "element": described(STR_N, "Id of the step; null for the process scope"),
    "attempt": described(INT_N, "Attempt of the step; null for the process scope"),
    "activityId": described(UUID_N, "Activity of the attempt; null for the process scope"),
}
_SLA_DUE_AT = described(TIME, "Declared deadline: the due time of the deadline timer")
_SLA_PROVISIONAL = described(BOOL, "Computed on a provisional calendar year")
_register(
    "process.step_entered",
    "process_instance",
    "A waiting step (activity) of the instance opened.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            **_STEP,
            "waitsFor": described(
                STR, "task, approval, skill, agent, child, event, time or memory"
            ),
            "taskId": described(UUID_N, "Task the step waits for"),
            "approvalIds": described(ARR, "Approvals the step waits for"),
            "skillInvocationId": described(UUID_N, "Skill invocation the step waits for"),
            "childInstanceId": described(UUID_N, "Child instance the step waits for"),
            "due": _STEP_DUE,
            "warnAt": described(nullable(TIME), "Warning threshold; null without warnBefore"),
            "provisional": described(BOOL, "The deadline is computed on a provisional year"),
        }
    ),
)
_register(
    "process.step_exited",
    "process_instance",
    "A waiting step (activity) of the instance closed.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            **_STEP,
            "exitedAt": TIME,
            "outcome": described(
                STR,
                "completed, cancelled, withdrawn (a participant cancelled the step's task, or"
                " the last approval of the step, outside the process; the event's actorId is"
                " that participant), interrupted, failed, timed_out or migrated",
            ),
            "durationSeconds": described(INT, "exitedAt - enteredAt, wall-clock seconds"),
            "due": _STEP_DUE,
            "breached": described(BOOL, "The step closed after its deadline"),
            "overdueSeconds": described(INT_N, "exitedAt - due when breached, else null"),
        }
    ),
)
_register(
    "process.sla_warning",
    "process_instance",
    "The warning threshold of a deadline passed while the step (process) is open.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            **_SLA,
            "dueAt": _SLA_DUE_AT,
            "warnAt": TIME,
            "provisional": _SLA_PROVISIONAL,
            "owner": _SLA_OWNER,
            "assignee": _SLA_ASSIGNEE,
        }
    ),
)
_register(
    "process.sla_breached",
    "process_instance",
    "A deadline passed while the step (process) is open; one per attempt.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            **_SLA,
            "dueAt": _SLA_DUE_AT,
            "detectedAt": described(TIME, "When the core processed the breach"),
            "overdueSeconds": described(INT, "detectedAt - dueAt, seconds, not negative"),
            "detectedBy": described(STR, "timer or migration"),
            "provisional": _SLA_PROVISIONAL,
            "owner": _SLA_OWNER,
            "assignee": _SLA_ASSIGNEE,
        }
    ),
)
_register(
    "process.sla_failed",
    "process_instance",
    "A deadline could not be computed; the instance goes on, its SLA state is unknown.",
    _instance_data(
        {
            **_PROCESS_INSTANCE,
            **_SLA,
            "error": described(_PROCESS_ERROR, "calendar_missing or an expression error"),
            "owner": _SLA_OWNER,
        }
    ),
)
_register(
    "calendar.published",
    "calendar",
    "A new version of a working-day calendar was published (CP-ADR-0074 §9).",
    data(
        {
            "key": STR,
            "version": INT,
            "calendarHash": _PROCESS_HASH,
            "previousVersion": INT_N,
            "years": ARR,
            "provisionalYears": ARR,
        }
    ),
)
_register(
    "calendar.retired",
    "calendar",
    "Every version of a working-day calendar was retired: no process needs it any more"
    " (CP-ADR-0074, amendment Zh3).",
    data({"key": STR, "latestVersion": INT, "reason": STR}),
)
_register(
    "calendar.restored",
    "calendar",
    "A retired working-day calendar is back in use: a package apply installed it as it is"
    " (CP-ADR-0074, amendment Zh3).",
    data({"key": STR, "latestVersion": INT, "packageKey": STR_N, "packageVersion": STR_N}),
)

# --- views of packages (CP-ADR-0080) ------------------------------------------

_register(
    "view.published",
    "view",
    "A view of a package is in use at a revision: published by a package apply, or brought"
    " back as it was; a console drops what it cached of the key (CP-ADR-0080).",
    data(
        {
            "key": STR,
            "revision": INT,
            "hash": described(STR, "sha256 of the revision, as GET /views/{key} returns it"),
            "previousRevision": described(INT_N, "The revision before; null for a new view"),
            "packageKey": STR_N,
            "packageVersion": STR_N,
        }
    ),
)
_register(
    "view.retired",
    "view",
    "A view of a package is out of use: the package that installed it no longer brings it"
    " (CP-ADR-0080).",
    data(
        {
            "key": STR,
            "revision": described(INT, "The last revision of the view"),
            "reason": STR,
            "packageKey": STR_N,
            "packageVersion": STR_N,
        }
    ),
)

# --- settings of packages (CP-ADR-0081) ---------------------------------------

_register(
    "package.settings_changed",
    "package",
    "The settings of a package have a new version: a PUT /packages/{key}/settings saved"
    " other values. No value is in the event — neither the old, the new nor the defaults;"
    " who may read them reads GET /packages/{key}/settings/versions (CP-ADR-0081 §5).",
    data(
        {
            "package": described(STR, "The key of the package"),
            "version": described(INT, "The new version of the values"),
            "previousVersion": described(INT, "The version before; 0 for the first saving"),
            "schemaRevision": described(INT, "The schema revision the values were checked by"),
            "changedPaths": described(
                {"type": "array", "items": STR},
                "JSON Pointers of the members whose saved value changed",
            ),
            "actorId": UUID,
        }
    ),
)

# --- attention ---------------------------------------------------------------

_register(
    "attention.feedback_recorded",
    "attention_feedback",
    "A principal judged an item of its attention list (CP-ADR-0071).",
    data(
        {
            "principalId": described(UUID, "Whose attention list the item was on"),
            "itemKey": described(STR, "Stable key of the item: <ruleKey>:<entityId>"),
            "rule": described(STR, "The rule that raised the item, as ruleKey@version"),
            "ruleKey": STR,
            "ruleVersion": INT,
            "kind": STR,
            "reasonCode": STR,
            "entityType": described(STR, "approval or task"),
            "entityId": UUID,
            "score": INT,
            "verdict": described(STR, "useful or not_needed"),
            "created": described(BOOL, "false when the verdict replaced an earlier one"),
            "hasComment": described(BOOL, "The comment itself stays with the feedback row"),
        }
    ),
)

# --- operations --------------------------------------------------------------

_register(
    "event_journal.archived",
    "event_journal",
    "Journal events were moved to the archive (ADR-0038).",
    data({"archived": INT, "throughCursor": STR, "minAgeSeconds": INT}),
)
_register(
    "event_journal.pruned",
    "event_journal",
    "Archived journal events were deleted.",
    data({"pruned": INT, "throughCursor": STR, "minAgeSeconds": INT}),
)
_register(
    "event_journal.exported",
    "event_journal",
    "The journal was exported for a period (CP-ADR-0068, export amendment): the filters of the"
    " export and the number of events, never the events themselves. Written before the"
    " body is streamed.",
    data(
        {
            "format": described(STR, "jsonl or csv"),
            "types": described(ARR, "Event type prefixes of the filter; empty - every type"),
            "entityType": STR_N,
            "entityId": UUID_N,
            "actorId": described(UUID_N, "Author filter of the export, not its author"),
            "occurredFrom": TIME,
            "occurredTo": TIME,
            "workspaceId": UUID_N,
            "includeDescendants": nullable(BOOL),
            "events": described(INT, "Events the export holds"),
            "throughCursor": described(STR, "Journal position the export reads up to"),
        }
    ),
)
_register(
    "context_adapter.redriven",
    "event_consumer",
    "The memory context adapter was redriven past a parked event.",
    data(
        {
            "consumer": STR,
            "wasParked": BOOL,
            "parkedReason": ANY,
            "parkedEventId": UUID_N,
            "cursor": STR,
            "reason": STR,
        }
    ),
)
_register(
    "context_adapter.rebuilt",
    "event_consumer",
    "The memory context adapter was rewound to rebuild its projection.",
    data({"consumer": STR, "fromCursor": ANY, "toCursor": STR, "reason": STR}),
)


# --- publication -------------------------------------------------------------

ENVELOPE_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "Event identifier (uuid); the key for deduplication"),
    ("type", "Event type, see below"),
    ("schemaVersion", "Version of the payload schema of this type"),
    ("sequence", "Journal sequence number; an identifier, not the replay order"),
    ("cursor", "Opaque replay cursor of the event"),
    ("tenantId", "Tenant"),
    ("entityType", "Entity the event is about"),
    ("entityId", "Its identifier"),
    ("workspaceId", "Workspace of the entity; null for tenant-level events"),
    ("actorId", "Principal who acted; null for the core itself"),
    ("iamActorId", "IAM identity of the actor, when there is one"),
    ("occurredAt", "When it happened"),
    ("correlationId", "Correlation of the request chain"),
    ("causationId", "What caused it, when known"),
    ("requestId", "Request that wrote it"),
    ("sessionId", "Work session, when there is one"),
    ("traceRunId", "Distributed trace id (X-Run-Id)"),
    ("payload", "Data of the event, by the schema of (type, schemaVersion)"),
)


def catalog_document() -> dict[str, Any]:
    """The catalog as one JSON document (``docs/events/catalog.json``)."""
    return {
        "envelope": {name: description for name, description in ENVELOPE_FIELDS},
        "types": {
            entry.type: {
                "entityType": entry.entity_type,
                "description": entry.description,
                "currentVersion": entry.current.version,
                "versions": {
                    str(v.version): {
                        **({"changes": v.changes} if v.changes else {}),
                        "schema": v.schema,
                    }
                    for v in entry.versions
                },
            }
            for entry in event_types()
        },
    }


def _schema_rows(schema: JsonSchema) -> list[str]:
    required = set(schema.get("required", ()))
    rows = []
    for name, prop in schema.get("properties", {}).items():
        kind = prop.get("type", "any")
        kind = " \\| ".join(kind) if isinstance(kind, list) else kind
        if "format" in prop:
            kind = f"{kind} ({prop['format']})"
        presence = "да" if name in required else "нет"
        rows.append(f"| `{name}` | {kind} | {presence} | {prop.get('description', '')} |")
    return rows


def render_markdown() -> str:
    """The human-readable catalog (``docs/events/catalog.md``)."""
    lines = [
        "# Каталог событий ядра",
        "",
        "<!-- Сгенерировано из src/control_plane/domain/event_catalog.py:"
        " make event-catalog. Не править руками. -->",
        "",
        "Контракт событий — [CP-ADR-0068](../adr/0068-event-filters-catalog-versions.md).",
        "Версия схемы данных только добавляет поля: потребитель версии N читает",
        "N+1 без изменений и игнорирует незнакомые поля. Машиночитаемый каталог",
        "с JSON Schema — [catalog.json](catalog.json).",
        "",
        "## Конверт",
        "",
        "| Поле | Смысл |",
        "|---|---|",
        *(f"| `{name}` | {description} |" for name, description in ENVELOPE_FIELDS),
        "",
        "## Типы",
        "",
        "| Тип | Сущность | Версия | Описание |",
        "|---|---|---|---|",
        *(
            f"| [`{e.type}`](#{e.type.replace('.', '')}) | `{e.entity_type}` |"
            f" {e.current.version} | {e.description} |"
            for e in event_types()
        ),
    ]
    for entry in event_types():
        lines += ["", f"### {entry.type}", "", entry.description, ""]
        lines.append(f"Сущность: `{entry.entity_type}`.")
        for version in reversed(entry.versions):
            lines += ["", f"Версия {version.version}"]
            if version.changes:
                lines[-1] += f" (добавлено: {version.changes})"
            lines[-1] += ":"
            rows = _schema_rows(version.schema)
            if rows:
                lines += ["", "| Поле | Тип | Всегда | Описание |", "|---|---|---|---|", *rows]
            else:
                lines += ["", "Данных нет."]
    return "\n".join(lines) + "\n"


def write_catalog(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "catalog.md").write_text(render_markdown(), encoding="utf-8")
    (directory / "catalog.json").write_text(
        json.dumps(catalog_document(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":  # pragma: no cover - thin CLI
    write_catalog(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/events"))
