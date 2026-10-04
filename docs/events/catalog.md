# Каталог событий ядра

<!-- Сгенерировано из src/control_plane/domain/event_catalog.py: make event-catalog. Не править руками. -->

Контракт событий — [CP-ADR-0068](../adr/0068-event-filters-catalog-versions.md).
Версия схемы данных только добавляет поля: потребитель версии N читает
N+1 без изменений и игнорирует незнакомые поля. Машиночитаемый каталог
с JSON Schema — [catalog.json](catalog.json).

## Конверт

| Поле | Смысл |
|---|---|
| `id` | Event identifier (uuid); the key for deduplication |
| `type` | Event type, see below |
| `schemaVersion` | Version of the payload schema of this type |
| `sequence` | Journal sequence number; an identifier, not the replay order |
| `cursor` | Opaque replay cursor of the event |
| `tenantId` | Tenant |
| `entityType` | Entity the event is about |
| `entityId` | Its identifier |
| `workspaceId` | Workspace of the entity; null for tenant-level events |
| `actorId` | Principal who acted; null for the core itself |
| `iamActorId` | IAM identity of the actor, when there is one |
| `occurredAt` | When it happened |
| `correlationId` | Correlation of the request chain |
| `causationId` | What caused it, when known |
| `requestId` | Request that wrote it |
| `sessionId` | Work session, when there is one |
| `traceRunId` | Distributed trace id (X-Run-Id) |
| `payload` | Data of the event, by the schema of (type, schemaVersion) |

## Типы

| Тип | Сущность | Версия | Описание |
|---|---|---|---|
| [`agent.identity_replaced`](#agentidentity_replaced) | `agent` | 1 | A service agent moved to a new IAM identity; its principal stayed (CP-ADR-0073). |
| [`agent.retired`](#agentretired) | `agent` | 1 | An agent was retired: stopped, binding revoked, history kept. |
| [`agent.revision_published`](#agentrevision_published) | `agent` | 1 | A new immutable revision of an agent spec was published (CP-ADR-0073 §2). |
| [`agent.secret_deleted`](#agentsecret_deleted) | `agent` | 1 | A secret of an agent was deleted with every version from the secret store (CP-ADR-0079 §11). |
| [`agent.secret_set`](#agentsecret_set) | `agent` | 1 | A secret of an agent was set by name (CP-ADR-0079 §11): the value went to the secret store in transit; the event carries the name only. |
| [`agent.state_changed`](#agentstate_changed) | `agent` | 1 | The desired state or replica count of an agent changed; no new revision. |
| [`agent.status_changed`](#agentstatus_changed) | `agent` | 1 | The observed state of an agent changed: phase, reason, node or revision. |
| [`api_key.break_glass_issued`](#api_keybreak_glass_issued) | `api_key` | 1 | A short-lived break-glass key was issued from the host shell (CP-ADR-0065). |
| [`api_key.created`](#api_keycreated) | `api_key` | 1 | An API key was issued to a principal. |
| [`api_key.revoked`](#api_keyrevoked) | `api_key` | 1 | An API key was revoked. |
| [`approval.approved`](#approvalapproved) | `approval` | 2 | The approval was approved by an eligible principal. |
| [`approval.cancelled`](#approvalcancelled) | `approval` | 2 | The pending approval was cancelled; nobody decides it any more. |
| [`approval.outcome_deferred`](#approvaloutcome_deferred) | `approval` | 1 | An outcome action waits for something (e.g. a skill invocation) before continuing. |
| [`approval.outcome_executed`](#approvaloutcome_executed) | `approval` | 1 | The outcome actions the task type declares for the decision were executed. |
| [`approval.outcome_failed`](#approvaloutcome_failed) | `approval` | 1 | An outcome action failed; the remaining actions stay for replay. |
| [`approval.rejected`](#approvalrejected) | `approval` | 2 | The approval was rejected by an eligible principal. |
| [`approval.requested`](#approvalrequested) | `approval` | 3 | A decision was requested from a principal or from the holders of a role. |
| [`artifact.content_purged`](#artifactcontent_purged) | `artifact` | 1 | The bytes of an artifact were removed by an administrator; the record stays. |
| [`artifact.content_read`](#artifactcontent_read) | `artifact` | 1 | The bytes of an artifact were handed out (CP-ADR-0072 §5). |
| [`artifact.created`](#artifactcreated) | `artifact` | 2 | An artifact was recorded. |
| [`artifact_type.created`](#artifact_typecreated) | `artifact_type` | 1 | An artifact type version was created (CP-ADR-0072). |
| [`attention.feedback_recorded`](#attentionfeedback_recorded) | `attention_feedback` | 1 | A principal judged an item of its attention list (CP-ADR-0071). |
| [`calendar.published`](#calendarpublished) | `calendar` | 1 | A new version of a working-day calendar was published (CP-ADR-0074 §9). |
| [`calendar.restored`](#calendarrestored) | `calendar` | 1 | A retired working-day calendar is back in use: a package apply installed it as it is (CP-ADR-0074, amendment Zh3). |
| [`calendar.retired`](#calendarretired) | `calendar` | 1 | Every version of a working-day calendar was retired: no process needs it any more (CP-ADR-0074, amendment Zh3). |
| [`capability.assigned`](#capabilityassigned) | `principal` | 1 | A capability was assigned to the principal. |
| [`capability.created`](#capabilitycreated) | `capability` | 1 | A capability was created. |
| [`capability.revoked`](#capabilityrevoked) | `principal` | 1 | A capability was revoked from the principal. |
| [`claim.expired`](#claimexpired) | `claim` | 1 | The lease of a claim ran out. |
| [`claim.released`](#claimreleased) | `claim` | 1 | The claim on a task ended: completed, released, cancelled or superseded. |
| [`connection.authorization_failed`](#connectionauthorization_failed) | `connection` | 1 | An OAuth callback of a live state did not connect: consent denied, a provider error, an invalid account, the initiator no longer authorized or a failed exchange. The status of the connection did not change. |
| [`connection.authorized`](#connectionauthorized) | `connection` | 1 | A connection became active: the OAuth code was exchanged in the secret store or a key of the connection was entered. No value, no account, no provider text. |
| [`connection.created`](#connectioncreated) | `connection` | 1 | A connection was created (CP-ADR-0079 §3); it waits for authorization. |
| [`connection.revoked`](#connectionrevoked) | `connection` | 1 | A connection was revoked (CP-ADR-0079 §10): its material is deleted from the secret store and no agent's policy names it any more. A repeated revocation records nothing. |
| [`connection.status_changed`](#connectionstatus_changed) | `connection` | 1 | The status of a connection moved without a new authorization — the connector reported that access is lost. Codes only, no text of the provider. |
| [`connection.updated`](#connectionupdated) | `connection` | 1 | The display name, settings or type version of a connection changed; only the names of the changed fields, never their values. |
| [`connection_type.oauth_app_set`](#connection_typeoauth_app_set) | `connection_type` | 1 | The OAuth application of a connection type was written to the secret store; neither the client id nor the secret is in the event. |
| [`connection_type.published`](#connection_typepublished) | `connection_type` | 1 | A version of a connection type was published (CP-ADR-0079 §2); a repeat of the same spec records nothing. |
| [`context_adapter.rebuilt`](#context_adapterrebuilt) | `event_consumer` | 1 | The memory context adapter was rewound to rebuild its projection. |
| [`context_adapter.redriven`](#context_adapterredriven) | `event_consumer` | 1 | The memory context adapter was redriven past a parked event. |
| [`delegation.created`](#delegationcreated) | `delegation` | 1 | A human delegated permissions to an agent. |
| [`delegation.revoked`](#delegationrevoked) | `delegation` | 2 | A delegation was revoked. |
| [`event_journal.archived`](#event_journalarchived) | `event_journal` | 1 | Journal events were moved to the archive (ADR-0038). |
| [`event_journal.exported`](#event_journalexported) | `event_journal` | 1 | The journal was exported for a period (CP-ADR-0068, export amendment): the filters of the export and the number of events, never the events themselves. Written before the body is streamed. |
| [`event_journal.pruned`](#event_journalpruned) | `event_journal` | 1 | Archived journal events were deleted. |
| [`goal.created`](#goalcreated) | `goal` | 1 | A goal was created (CP-ADR-0062). |
| [`goal.updated`](#goalupdated) | `goal` | 1 | Goal attributes or its status changed. |
| [`iam_binding.created`](#iam_bindingcreated) | `iam_binding` | 2 | An IAM identity was bound to a local principal (CP-ADR-0053). |
| [`iam_binding.revoked`](#iam_bindingrevoked) | `iam_binding` | 1 | An IAM binding was revoked; the identity no longer enters. |
| [`iam_binding.updated`](#iam_bindingupdated) | `iam_binding` | 2 | The permissions or the visibility of an IAM binding changed. |
| [`knowledge.changed`](#knowledgechanged) | `workspace` | 1 | A knowledge snapshot opened, changed or closed documents in memory (CP-ADR-0076 §6); an empty reconciliation writes no event. |
| [`knowledge.document_stored`](#knowledgedocument_stored) | `workspace` | 1 | A knowledge base document was stored in the memory service (CP-ADR-0060, amendment 2026-09-28); its text stays out of the journal. |
| [`knowledge.pack_registered`](#knowledgepack_registered) | `knowledge_pack` | 2 | A domain knowledge pack version was registered. |
| [`knowledge.packs_configured`](#knowledgepacks_configured) | `workspace` | 1 | The knowledge packs of a workspace tree were configured. |
| [`knowledge.snapshot_reconciled`](#knowledgesnapshot_reconciled) | `workspace` | 1 | A knowledge snapshot was reconciled into the memory service (CP-ADR-0060). |
| [`observation.recorded`](#observationrecorded) | `observation` | 1 | An observation was recorded (ADR-0057). |
| [`package.settings_changed`](#packagesettings_changed) | `package` | 1 | The settings of a package have a new version: a PUT /packages/{key}/settings saved other values. No value is in the event — neither the old, the new nor the defaults; who may read them reads GET /packages/{key}/settings/versions (CP-ADR-0081 §5). |
| [`principal.created`](#principalcreated) | `principal` | 1 | A principal (human, agent or service) was created. |
| [`principal.disabled`](#principaldisabled) | `principal` | 1 | A human or agent principal was disabled: bindings and delegations revoked, sessions closed, claims freed, runs failed. |
| [`principal.enabled`](#principalenabled) | `principal` | 1 | A disabled (or paused) human or agent principal was enabled. Only the status comes back: bindings, delegations, sessions and claims closed by :disable stay closed, IAM entry needs a new binding. |
| [`principal.updated`](#principalupdated) | `principal` | 1 | The display name or the profile of a principal changed (CP-ADR-0082). Names of the changed fields only, never their values: read them via GET /principals/{id}. |
| [`process.cancelled`](#processcancelled) | `process_instance` | 1 | An operator cancelled the instance. |
| [`process.compensated`](#processcompensated) | `process_instance` | 1 | Compensations of completed steps ran in reverse order. |
| [`process.completed`](#processcompleted) | `process_instance` | 1 | The instance completed with an outcome. |
| [`process.correlated`](#processcorrelated) | `process_instance` | 1 | An event matched start.key or a correlate rule of a running instance. |
| [`process.data_changed`](#processdata_changed) | `process_instance` | 1 | Instance data changed; timers depending on the fields were recomputed. |
| [`process.definition_published`](#processdefinition_published) | `process_definition` | 1 | A new immutable version of a process was published (CP-ADR-0074 §3). |
| [`process.definition_restored`](#processdefinition_restored) | `process_definition` | 1 | A retired process is back in use: a package apply installed it as it is (CP-ADR-0074, amendment Zh3). |
| [`process.definition_retired`](#processdefinition_retired) | `process_definition` | 1 | Every version of a process was retired: no new instances, open ones run to the end (CP-ADR-0074, amendment Zh2). |
| [`process.escalated`](#processescalated) | `process_instance` | 2 | An escalation level of a step fired. |
| [`process.failed`](#processfailed) | `process_instance` | 1 | An error reached the top of the instance without a handler. |
| [`process.migrated`](#processmigrated) | `process_instance` | 1 | The instance moved to another version by the process's migration map. |
| [`process.milestone_lost`](#processmilestone_lost) | `process_instance` | 1 | A reached milestone stopped holding: its guard is false again (a standing goal is no longer met); it is reached again when the guard holds again. |
| [`process.milestone_reached`](#processmilestone_reached) | `process_instance` | 1 | A milestone of the case was reached. |
| [`process.recall_completed`](#processrecall_completed) | `process_instance` | 1 | Memory answered a recall step; the answer is in the instance journal (CP-ADR-0076 §4). |
| [`process.recall_timed_out`](#processrecall_timed_out) | `process_instance` | 1 | A recall step got no answer in time; the step's onTimeout runs. |
| [`process.resumed`](#processresumed) | `process_instance` | 1 | The instance was resumed; frozen timers got their remaining time back. |
| [`process.sla_breached`](#processsla_breached) | `process_instance` | 1 | A deadline passed while the step (process) is open; one per attempt. |
| [`process.sla_failed`](#processsla_failed) | `process_instance` | 1 | A deadline could not be computed; the instance goes on, its SLA state is unknown. |
| [`process.sla_warning`](#processsla_warning) | `process_instance` | 1 | The warning threshold of a deadline passed while the step (process) is open. |
| [`process.stage_entered`](#processstage_entered) | `process_instance` | 1 | A stage of the case was entered. |
| [`process.stage_exited`](#processstage_exited) | `process_instance` | 1 | A stage of the case was exited. |
| [`process.started`](#processstarted) | `process_instance` | 1 | A process instance started from its start trigger. |
| [`process.step_entered`](#processstep_entered) | `process_instance` | 1 | A waiting step (activity) of the instance opened. |
| [`process.step_exited`](#processstep_exited) | `process_instance` | 1 | A waiting step (activity) of the instance closed. |
| [`process.suspended`](#processsuspended) | `process_instance` | 1 | The instance was suspended; its timers froze. |
| [`process.timer_fired`](#processtimer_fired) | `process_instance` | 1 | A timer of the instance fired; the engine takes it as its next event. |
| [`process.timer_rescheduled`](#processtimer_rescheduled) | `process_instance` | 1 | A pending timer moved: data or a calendar it reads changed, or on resume. |
| [`project.archived`](#projectarchived) | `project` | 1 | The project was archived. |
| [`project.config_revision_activated`](#projectconfig_revision_activated) | `project` | 1 | A configuration revision became the active one. |
| [`project.config_revision_created`](#projectconfig_revision_created) | `project` | 1 | A new configuration revision of the project was drafted. |
| [`project.created`](#projectcreated) | `project` | 1 | A project was created on a workspace (ADR-0031). |
| [`project.external_reference_added`](#projectexternal_reference_added) | `project` | 1 | A reference to an external system was attached to the project (ADR-0047). |
| [`project.external_reference_updated`](#projectexternal_reference_updated) | `project` | 1 | An external reference of the project changed. |
| [`project.status_changed`](#projectstatus_changed) | `project` | 1 | The project moved to another status. |
| [`project.updated`](#projectupdated) | `project` | 1 | Project attributes changed. |
| [`project_template.created`](#project_templatecreated) | `project_template` | 1 | A project template version was created. |
| [`project_template.deprecated`](#project_templatedeprecated) | `project_template` | 1 | A project template version was deprecated. |
| [`role.assigned`](#roleassigned) | `principal` | 1 | A role was assigned to the principal, tenant-wide or in a workspace subtree. |
| [`role.created`](#rolecreated) | `role` | 1 | A role was created, tenant-wide or in a workspace. |
| [`role.revoked`](#rolerevoked) | `principal` | 1 | A role assignment was revoked. |
| [`role.updated`](#roleupdated) | `role` | 1 | A role was renamed or redescribed. |
| [`rule.archived`](#rulearchived) | `rule` | 1 | A work rule was archived. |
| [`rule.created`](#rulecreated) | `rule` | 1 | A work rule was created (CP-ADR-0063). |
| [`rule.disabled`](#ruledisabled) | `rule` | 1 | A work rule was disabled. |
| [`rule.enabled`](#ruleenabled) | `rule` | 1 | A work rule was enabled. |
| [`rule.evaluated`](#ruleevaluated) | `rule` | 1 | A work rule was evaluated against a trigger. |
| [`rule.updated`](#ruleupdated) | `rule` | 1 | A work rule changed. |
| [`run.cancel_requested`](#runcancel_requested) | `run` | 1 | Cancellation of the run was requested. |
| [`run.cancelled`](#runcancelled) | `run` | 1 | The run was cancelled. |
| [`run.checkpointed`](#runcheckpointed) | `run` | 1 | The run left a checkpoint. |
| [`run.child.cancel_requested`](#runchildcancel_requested) | `run` | 1 | Cancellation of the child run was requested. |
| [`run.child.launched`](#runchildlaunched) | `run` | 1 | The run launched a child task under a handle (ADR-0046). |
| [`run.child.resolved`](#runchildresolved) | `run` | 1 | The child handle was resolved with the child's outcome. |
| [`run.child.revoked`](#runchildrevoked) | `run` | 1 | The child handle was revoked. |
| [`run.child.started`](#runchildstarted) | `run` | 1 | A run of the child task started. |
| [`run.control_message.accepted`](#runcontrol_messageaccepted) | `run` | 1 | A control message for the active turn was accepted (ADR-0044). |
| [`run.control_message.applied`](#runcontrol_messageapplied) | `run` | 1 | A control message was applied. |
| [`run.control_message.rejected`](#runcontrol_messagerejected) | `run` | 1 | A control message was rejected. |
| [`run.control_message.superseded`](#runcontrol_messagesuperseded) | `run` | 1 | A control message was superseded. |
| [`run.failed`](#runfailed) | `run` | 1 | The run failed. |
| [`run.handoff_prepared`](#runhandoff_prepared) | `run` | 1 | The run prepared a handoff to another executor. |
| [`run.manifest_compiled`](#runmanifest_compiled) | `run` | 1 | The effective harness manifest of the run was compiled (ADR-0043). No longer written since ADR-0073; kept for events already in the journal. |
| [`run.manifest_ephemeral_recorded`](#runmanifest_ephemeral_recorded) | `run` | 1 | An ephemeral manifest change was recorded (ADR-0043). No longer written since ADR-0073; kept for events already in the journal. |
| [`run.started`](#runstarted) | `run` | 2 | An execution attempt started under a claim. |
| [`run.succeeded`](#runsucceeded) | `run` | 1 | The run finished successfully. |
| [`run.suspended`](#runsuspended) | `run` | 1 | The run was suspended, e.g. to wait for a decision. |
| [`session.closed`](#sessionclosed) | `session` | 2 | A work session was closed. |
| [`session.expired`](#sessionexpired) | `session` | 1 | A work session expired; its claims were released. |
| [`session.opened`](#sessionopened) | `session` | 1 | A harness opened a work session. |
| [`skill.assigned`](#skillassigned) | `principal` | 1 | A skill was assigned. |
| [`skill.invocation_cancelled`](#skillinvocation_cancelled) | `skill_invocation` | 1 | The invocation was cancelled. |
| [`skill.invocation_claimed`](#skillinvocation_claimed) | `skill_invocation` | 1 | An executor took the invocation under a lease. |
| [`skill.invocation_failed`](#skillinvocation_failed) | `skill_invocation` | 1 | The invocation failed for good. |
| [`skill.invocation_requested`](#skillinvocation_requested) | `skill_invocation` | 1 | The core was asked to invoke a skill (CP-ADR-0056). |
| [`skill.invocation_retry_scheduled`](#skillinvocation_retry_scheduled) | `skill_invocation` | 1 | The attempt failed with a retryable error; another one is scheduled. |
| [`skill.invocation_succeeded`](#skillinvocation_succeeded) | `skill_invocation` | 2 | The invocation finished; its result is an artifact. |
| [`skill.registered`](#skillregistered) | `skill` | 1 | A skill version was registered. |
| [`skill.revoked`](#skillrevoked) | `principal` | 1 | A skill was revoked. |
| [`skill.updated`](#skillupdated) | `skill` | 1 | The description, status or implementation endpoint of a skill version changed. |
| [`task.claimed`](#taskclaimed) | `task` | 1 | An executor claimed the task under a lease. |
| [`task.comment_added`](#taskcomment_added) | `task` | 1 | A comment was added to the task. |
| [`task.comment_edited`](#taskcomment_edited) | `task` | 1 | A task comment was edited. |
| [`task.completed`](#taskcompleted) | `task` | 1 | The task reached its completion status. |
| [`task.completion_work_executed`](#taskcompletion_work_executed) | `task` | 1 | The completion work the task type declares was executed (ADR-0061). |
| [`task.completion_work_failed`](#taskcompletion_work_failed) | `task` | 1 | An action of the completion work failed; the completion stands. |
| [`task.context_pack_recorded`](#taskcontext_pack_recorded) | `task` | 1 | The context pack assembled for the task on claim was recorded (CP-ADR-0064). |
| [`task.created`](#taskcreated) | `task` | 1 | A task was created. |
| [`task.external_reference_added`](#taskexternal_reference_added) | `task` | 1 | A reference to an external system was attached to the task (ADR-0047). |
| [`task.external_reference_updated`](#taskexternal_reference_updated) | `task` | 1 | An external reference of the task changed. |
| [`task.relation_added`](#taskrelation_added) | `task` | 1 | A relation to another task was added. |
| [`task.relation_removed`](#taskrelation_removed) | `task` | 1 | A relation between tasks was removed. |
| [`task.type_migrated`](#tasktype_migrated) | `task` | 1 | The task was moved to another version of its type (ADR-0048, amendment 2026-09-30). |
| [`task.updated`](#taskupdated) | `task` | 1 | Task attributes or its status changed. |
| [`task.verification_failed`](#taskverification_failed) | `task` | 1 | An acceptance check failed; the task went back to its executor or got blocked. |
| [`task.verification_started`](#taskverification_started) | `task` | 1 | A verification attempt of the task's acceptance checks opened (CP-ADR-0067). |
| [`task.verified`](#taskverified) | `task` | 1 | Every acceptance check passed; the task is complete. |
| [`task_type.created`](#task_typecreated) | `task_type` | 3 | A task type version was created (ADR-0048). |
| [`task_type.deprecated`](#task_typedeprecated) | `task_type` | 1 | A task type version was deprecated. |
| [`tenant.bootstrapped`](#tenantbootstrapped) | `tenant` | 1 | The tenant was created with its first administrator. |
| [`view.published`](#viewpublished) | `view` | 1 | A view of a package is in use at a revision: published by a package apply, or brought back as it was; a console drops what it cached of the key (CP-ADR-0080). |
| [`view.retired`](#viewretired) | `view` | 1 | A view of a package is out of use: the package that installed it no longer brings it (CP-ADR-0080). |
| [`work.derived`](#workderived) | `task` | 1 | A rule derived new work. |
| [`work.reconciled`](#workreconciled) | `task` | 1 | A rule updated, cancelled or completed work: the work it derived earlier, or the task an observation is bound to. |
| [`workspace.archived`](#workspacearchived) | `workspace` | 1 | A workspace was archived. |
| [`workspace.created`](#workspacecreated) | `workspace` | 1 | A workspace was created. |
| [`workspace.member_added`](#workspacemember_added) | `workspace` | 1 | A principal became a member of the workspace. |
| [`workspace.member_removed`](#workspacemember_removed) | `workspace` | 1 | A principal stopped being a member of the workspace. |
| [`workspace.moved`](#workspacemoved) | `workspace` | 1 | A workspace moved under another parent. |
| [`workspace.updated`](#workspaceupdated) | `workspace` | 2 | Workspace attributes changed. |
| [`workspace_type.archived`](#workspace_typearchived) | `workspace_type` | 1 | A workspace type was archived. |
| [`workspace_type.created`](#workspace_typecreated) | `workspace_type` | 1 | A workspace type was created. |
| [`workspace_type.updated`](#workspace_typeupdated) | `workspace_type` | 1 | A workspace type changed. |

### agent.identity_replaced

A service agent moved to a new IAM identity; its principal stayed (CP-ADR-0073).

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `revision` | integer | да | The revision whose rights the new identity got |
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamTenantId` | string (uuid) | да |  |
| `iamPrincipalId` | string (uuid) | да |  |
| `previousIssuer` | string \| null | да |  |
| `previousIamTenantId` | string \| null (uuid) | да |  |
| `previousIamPrincipalId` | string \| null (uuid) | да |  |
| `reason` | string | да | Reason given; credential-shaped material redacted, cut to the limit |

### agent.retired

An agent was retired: stopped, binding revoked, history kept.

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `revision` | integer | да | The last revision of the agent |
| `principalId` | string \| null (uuid) | да |  |
| `reason` | string | да | Reason given; credential-shaped material redacted, cut to the limit |
| `releasedClaims` | integer | да | Active claims of the agent released to the queue |

### agent.revision_published

A new immutable revision of an agent spec was published (CP-ADR-0073 §2).

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `revision` | integer | да |  |
| `specHash` | string | да |  |
| `previousRevision` | integer \| null | да | Null for the first revision of the key |
| `executorKind` | string \| null | да | Null for an identity without placement |
| `placed` | boolean | да | False for placement none |
| `permissionsChanged` | boolean | да | Identity (roles, permissions, capabilities) differs from the previous one |

### agent.secret_deleted

A secret of an agent was deleted with every version from the secret store (CP-ADR-0079 §11).

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `agentKey` | string | да |  |
| `name` | string | да |  |

### agent.secret_set

A secret of an agent was set by name (CP-ADR-0079 §11): the value went to the secret store in transit; the event carries the name only.

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `agentKey` | string | да |  |
| `name` | string | да |  |
| `created` | boolean | да | The first value under the name, not a replacement |

### agent.state_changed

The desired state or replica count of an agent changed; no new revision.

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `state` | string | да |  |
| `replicas` | integer | да |  |
| `previousState` | string \| null | да | Null when the agent is first published |
| `previousReplicas` | integer \| null | да |  |

### agent.status_changed

The observed state of an agent changed: phase, reason, node or revision.

Сущность: `agent`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `phase` | string | да |  |
| `previousPhase` | string \| null | да | Null on the first report |
| `reasonCode` | string \| null | да | Why it is not running, e.g. no_matching_node |
| `node` | string \| null | да |  |
| `observedRevision` | integer \| null | да |  |
| `observedAt` | string (date-time) | да |  |

### api_key.break_glass_issued

A short-lived break-glass key was issued from the host shell (CP-ADR-0065).

Сущность: `api_key`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `keyPrefix` | string | да |  |
| `permissions` | array | да |  |
| `expiresAt` | string (date-time) | да |  |
| `ttlSeconds` | integer | да |  |
| `reason` | string | да |  |
| `issuedBy` | any | да |  |

### api_key.created

An API key was issued to a principal.

Сущность: `api_key`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `keyPrefix` | string | да |  |
| `permissions` | array | да |  |

### api_key.revoked

An API key was revoked.

Сущность: `api_key`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `keyPrefix` | string | да |  |
| `breakGlass` | boolean | нет |  |
| `issuedBy` | any | нет |  |

### approval.approved

The approval was approved by an eligible principal.

Сущность: `approval`.

Версия 2 (добавлено: decisionBy, comment, channel):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `outcomeStatus` | string \| null | да | pending when the task type declares outcomes for this decision (CP-ADR-0061) |
| `decisionBy` | string (uuid) | да | Principal who decided |
| `comment` | string \| null | да | Decision comment; credential-shaped material redacted, cut to the limit |
| `channel` | string \| null | да | Channel the decision came through when it was not a direct API call (e.g. telegram); null for a direct call |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `outcomeStatus` | string \| null | да | pending when the task type declares outcomes for this decision (CP-ADR-0061) |

### approval.cancelled

The pending approval was cancelled; nobody decides it any more.

Сущность: `approval`.

Версия 2 (добавлено: cancelledBy):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `cancelledBy` | string (uuid) | да | Principal who cancelled |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |

### approval.outcome_deferred

An outcome action waits for something (e.g. a skill invocation) before continuing.

Сущность: `approval`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `outcome` | string | да |  |
| `waitingAction` | object | да |  |
| `reason` | string | да |  |
| `details` | object | да |  |

### approval.outcome_executed

The outcome actions the task type declares for the decision were executed.

Сущность: `approval`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `outcome` | string | да |  |
| `actions` | array | да |  |

### approval.outcome_failed

An outcome action failed; the remaining actions stay for replay.

Сущность: `approval`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `outcome` | string | да |  |
| `failedAction` | object | да |  |
| `actions` | array | да |  |
| `failureWorkTaskId` | string \| null (uuid) | да |  |

### approval.rejected

The approval was rejected by an eligible principal.

Сущность: `approval`.

Версия 2 (добавлено: decisionBy, comment, channel):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `outcomeStatus` | string \| null | да | pending when the task type declares outcomes for this decision (CP-ADR-0061) |
| `decisionBy` | string (uuid) | да | Principal who decided |
| `comment` | string \| null | да | Decision comment; credential-shaped material redacted, cut to the limit |
| `channel` | string \| null | да | Channel the decision came through when it was not a direct API call (e.g. telegram); null for a direct call |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `outcomeStatus` | string \| null | да | pending when the task type declares outcomes for this decision (CP-ADR-0061) |

### approval.requested

A decision was requested from a principal or from the holders of a role.

Сущность: `approval`.

Версия 3 (добавлено: excludedPrincipals):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `requiredRoleId` | string \| null (uuid) | да | Set when any holder of the role may decide |
| `assignedPrincipalId` | string \| null (uuid) | да | Set when one principal decides |
| `gate` | boolean | да | A gate holds the task's claim and completion until decided |
| `workspaceId` | string \| null (uuid) | да | The approval's workspace, else its task's workspace |
| `taskPublicId` | string \| null | да | Public id of the task, e.g. TASK-000123 |
| `taskTitle` | string \| null | да |  |
| `requestedBy` | string (uuid) | да | Principal who requested the decision |
| `comment` | string | да | Request comment; credential-shaped material redacted, cut to the limit |
| `excludedPrincipals` | array | да | Principals whose decision the core refuses (separation of duties, CP-ADR-0074); empty when nobody is excluded |

Версия 2 (добавлено: workspaceId, taskPublicId, taskTitle, requestedBy, comment):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `requiredRoleId` | string \| null (uuid) | да | Set when any holder of the role may decide |
| `assignedPrincipalId` | string \| null (uuid) | да | Set when one principal decides |
| `gate` | boolean | да | A gate holds the task's claim and completion until decided |
| `workspaceId` | string \| null (uuid) | да | The approval's workspace, else its task's workspace |
| `taskPublicId` | string \| null | да | Public id of the task, e.g. TASK-000123 |
| `taskTitle` | string \| null | да |  |
| `requestedBy` | string (uuid) | да | Principal who requested the decision |
| `comment` | string | да | Request comment; credential-shaped material redacted, cut to the limit |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `requiredRoleId` | string \| null (uuid) | да | Set when any holder of the role may decide |
| `assignedPrincipalId` | string \| null (uuid) | да | Set when one principal decides |
| `gate` | boolean | да | A gate holds the task's claim and completion until decided |

### artifact.content_purged

The bytes of an artifact were removed by an administrator; the record stays.

Сущность: `artifact`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `artifactId` | string (uuid) | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `sha256` | string | да |  |
| `sizeBytes` | integer | да |  |
| `reason` | string | да | Reason given; credential-shaped material redacted, cut to the limit |
| `objectDeleted` | boolean | да | False when other artifacts or uploads still need the object |

### artifact.content_read

The bytes of an artifact were handed out (CP-ADR-0072 §5).

Сущность: `artifact`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `artifactId` | string (uuid) | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `forTaskId` | string \| null (uuid) | да | Receiving task when read as its input |
| `runId` | string \| null (uuid) | да | The reader's running run on that task, if any |
| `sha256` | string | да |  |
| `sizeBytes` | integer | да |  |

### artifact.created

An artifact was recorded.

Сущность: `artifact`.

Версия 2 (добавлено: sizeBytes, mediaType, sha256, contentState, typeVersion (CP-ADR-0072)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `type` | string | да |  |
| `name` | string | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `uri` | any | да |  |
| `supersedesArtifactId` | string \| null (uuid) | да |  |
| `sizeBytes` | integer \| null | да | Size of the stored content; null without one |
| `mediaType` | string \| null | да |  |
| `sha256` | string \| null | да |  |
| `contentState` | string | да |  |
| `typeVersion` | integer \| null | да | Version of the registered artifact type it was checked against |
| `skillInvocationId` | string (uuid) | нет |  |
| `ruleEvaluationId` | string (uuid) | нет |  |
| `verificationId` | string (uuid) | нет |  |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `type` | string | да |  |
| `name` | string | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `uri` | any | да |  |
| `supersedesArtifactId` | string \| null (uuid) | да |  |
| `skillInvocationId` | string (uuid) | нет |  |
| `ruleEvaluationId` | string (uuid) | нет |  |
| `verificationId` | string (uuid) | нет |  |

### artifact_type.created

An artifact type version was created (CP-ADR-0072).

Сущность: `artifact_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `mediaTypes` | array | да |  |
| `maxBytes` | integer | да |  |
| `declaresMetadataSchema` | boolean | да |  |

### attention.feedback_recorded

A principal judged an item of its attention list (CP-ADR-0071).

Сущность: `attention_feedback`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да | Whose attention list the item was on |
| `itemKey` | string | да | Stable key of the item: <ruleKey>:<entityId> |
| `rule` | string | да | The rule that raised the item, as ruleKey@version |
| `ruleKey` | string | да |  |
| `ruleVersion` | integer | да |  |
| `kind` | string | да |  |
| `reasonCode` | string | да |  |
| `entityType` | string | да | approval or task |
| `entityId` | string (uuid) | да |  |
| `score` | integer | да |  |
| `verdict` | string | да | useful or not_needed |
| `created` | boolean | да | false when the verdict replaced an earlier one |
| `hasComment` | boolean | да | The comment itself stays with the feedback row |

### calendar.published

A new version of a working-day calendar was published (CP-ADR-0074 §9).

Сущность: `calendar`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `calendarHash` | string | да |  |
| `previousVersion` | integer \| null | да |  |
| `years` | array | да |  |
| `provisionalYears` | array | да |  |

### calendar.restored

A retired working-day calendar is back in use: a package apply installed it as it is (CP-ADR-0074, amendment Zh3).

Сущность: `calendar`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `latestVersion` | integer | да |  |
| `packageKey` | string \| null | да |  |
| `packageVersion` | string \| null | да |  |

### calendar.retired

Every version of a working-day calendar was retired: no process needs it any more (CP-ADR-0074, amendment Zh3).

Сущность: `calendar`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `latestVersion` | integer | да |  |
| `reason` | string | да |  |

### capability.assigned

A capability was assigned to the principal.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `capabilityId` | string (uuid) | да |  |

### capability.created

A capability was created.

Сущность: `capability`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `name` | string | да |  |

### capability.revoked

A capability was revoked from the principal.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `capabilityId` | string (uuid) | да |  |

### claim.expired

The lease of a claim ran out.

Сущность: `claim`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `reason` | any | нет |  |
| `taskStatus` | string | нет |  |
| `taskSystemStatusCategory` | string | нет |  |

### claim.released

The claim on a task ended: completed, released, cancelled or superseded.

Сущность: `claim`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `reason` | any | да |  |
| `taskStatus` | string | нет |  |
| `taskSystemStatusCategory` | string | нет |  |

### connection.authorization_failed

An OAuth callback of a live state did not connect: consent denied, a provider error, an invalid account, the initiator no longer authorized or a failed exchange. The status of the connection did not change.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `type` | string | да |  |
| `reason` | string | да | A code, e.g. consent_denied, oauth_exchange_failed |
| `initiatedBy` | string (uuid) | да | The principal that started :authorize |

### connection.authorized

A connection became active: the OAuth code was exchanged in the secret store or a key of the connection was entered. No value, no account, no provider text.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `type` | string | да |  |
| `auth` | string | да |  |
| `previousStatus` | string | да |  |
| `connectedBy` | string (uuid) | да | The principal that connected it |

### connection.created

A connection was created (CP-ADR-0079 §3); it waits for authorization.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `type` | string | да |  |
| `typeVersion` | integer | да |  |
| `status` | string | да |  |

### connection.revoked

A connection was revoked (CP-ADR-0079 §10): its material is deleted from the secret store and no agent's policy names it any more. A repeated revocation records nothing.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `type` | string | да |  |
| `previousStatus` | string | да | pending, active or expired: the status before the revocation |

### connection.status_changed

The status of a connection moved without a new authorization — the connector reported that access is lost. Codes only, no text of the provider.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `type` | string | да |  |
| `from` | string | да |  |
| `to` | string | да |  |
| `reason` | string \| null | да | A code, e.g. refresh_rejected |
| `connectedBy` | string \| null (uuid) | да | Who connected it last; null if nobody has |

### connection.updated

The display name, settings or type version of a connection changed; only the names of the changed fields, never their values.

Сущность: `connection`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `changes` | array | да | Names of the changed fields: displayName, settings, typeVersion |

### connection_type.oauth_app_set

The OAuth application of a connection type was written to the secret store; neither the client id nor the secret is in the event.

Сущность: `connection_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `type` | string | да |  |
| `created` | boolean | да | The first write, not a replacement |

### connection_type.published

A version of a connection type was published (CP-ADR-0079 §2); a repeat of the same spec records nothing.

Сущность: `connection_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `auth` | array | да |  |

### context_adapter.rebuilt

The memory context adapter was rewound to rebuild its projection.

Сущность: `event_consumer`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `consumer` | string | да |  |
| `fromCursor` | any | да |  |
| `toCursor` | string | да |  |
| `reason` | string | да |  |

### context_adapter.redriven

The memory context adapter was redriven past a parked event.

Сущность: `event_consumer`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `consumer` | string | да |  |
| `wasParked` | boolean | да |  |
| `parkedReason` | any | да |  |
| `parkedEventId` | string \| null (uuid) | да |  |
| `cursor` | string | да |  |
| `reason` | string | да |  |

### delegation.created

A human delegated permissions to an agent.

Сущность: `delegation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `humanPrincipalId` | string (uuid) | да |  |
| `agentPrincipalId` | string (uuid) | да |  |
| `permissions` | array | да |  |

### delegation.revoked

A delegation was revoked.

Сущность: `delegation`.

Версия 2 (добавлено: reason):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `reason` | string | нет | principal_disabled when a side of it was disabled (CP-ADR-0077) |

Версия 1:

Данных нет.

### event_journal.archived

Journal events were moved to the archive (ADR-0038).

Сущность: `event_journal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `archived` | integer | да |  |
| `throughCursor` | string | да |  |
| `minAgeSeconds` | integer | да |  |

### event_journal.exported

The journal was exported for a period (CP-ADR-0068, export amendment): the filters of the export and the number of events, never the events themselves. Written before the body is streamed.

Сущность: `event_journal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `format` | string | да | jsonl or csv |
| `types` | array | да | Event type prefixes of the filter; empty - every type |
| `entityType` | string \| null | да |  |
| `entityId` | string \| null (uuid) | да |  |
| `actorId` | string \| null (uuid) | да | Author filter of the export, not its author |
| `occurredFrom` | string (date-time) | да |  |
| `occurredTo` | string (date-time) | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `includeDescendants` | boolean \| null | да |  |
| `events` | integer | да | Events the export holds |
| `throughCursor` | string | да | Journal position the export reads up to |

### event_journal.pruned

Archived journal events were deleted.

Сущность: `event_journal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `pruned` | integer | да |  |
| `throughCursor` | string | да |  |
| `minAgeSeconds` | integer | да |  |

### goal.created

A goal was created (CP-ADR-0062).

Сущность: `goal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `goalId` | string (uuid) | да |  |
| `title` | string | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `ownerId` | string \| null (uuid) | да |  |
| `parentGoalId` | string \| null (uuid) | да |  |
| `criteriaCount` | integer | да |  |
| `createdFrom` | any | да |  |

### goal.updated

Goal attributes or its status changed.

Сущность: `goal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `goalId` | string (uuid) | да |  |
| `changes` | any | да |  |
| `version` | integer | да |  |
| `fromStatus` | string | нет |  |
| `status` | string | нет |  |

### iam_binding.created

An IAM identity was bound to a local principal (CP-ADR-0053).

Сущность: `iam_binding`.

Версия 2 (добавлено: visibility):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamTenantId` | any | да |  |
| `iamPrincipalId` | any | да |  |
| `permissions` | array | да |  |
| `visibility` | string | да | Visibility of the binding: the whole tenant or the workspaces of membership (CP-ADR-0082) |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamTenantId` | any | да |  |
| `iamPrincipalId` | any | да |  |
| `permissions` | array | да |  |

### iam_binding.revoked

An IAM binding was revoked; the identity no longer enters.

Сущность: `iam_binding`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamPrincipalId` | any | да |  |

### iam_binding.updated

The permissions or the visibility of an IAM binding changed.

Сущность: `iam_binding`.

Версия 2 (добавлено: visibility):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamTenantId` | any | да |  |
| `iamPrincipalId` | any | да |  |
| `permissions` | array | да |  |
| `visibility` | string | да | Visibility of the binding: the whole tenant or the workspaces of membership (CP-ADR-0082) |
| `previousPrincipalId` | string (uuid) | нет | Only when the identity moved from another principal |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `issuer` | string | да |  |
| `iamTenantId` | any | да |  |
| `iamPrincipalId` | any | да |  |
| `permissions` | array | да |  |
| `previousPrincipalId` | string (uuid) | нет | Only when the identity moved from another principal |

### knowledge.changed

A knowledge snapshot opened, changed or closed documents in memory (CP-ADR-0076 §6); an empty reconciliation writes no event.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `snapshotId` | any | да |  |
| `pack` | any | да |  |
| `source` | any | да |  |
| `observedAt` | any | да |  |
| `workspaceId` | string (uuid) | да |  |
| `rootWorkspaceId` | string (uuid) | да |  |
| `namespace` | string | да |  |
| `changes` | array | да | Natural keys the memory service reported for the snapshot |
| `truncated` | boolean | да | The memory service cut the list; the counters stay complete |
| `counters` | object | да |  |

### knowledge.document_stored

A knowledge base document was stored in the memory service (CP-ADR-0060, amendment 2026-09-28); its text stays out of the journal.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `naturalKey` | string | да |  |
| `title` | string | да |  |
| `type` | string | да |  |
| `workspaceId` | string (uuid) | да |  |
| `rootWorkspaceId` | string (uuid) | да |  |
| `namespace` | string | да |  |
| `chunkCount` | integer | да |  |
| `linkCount` | integer | да |  |

### knowledge.pack_registered

A domain knowledge pack version was registered.

Сущность: `knowledge_pack`.

Версия 2 (добавлено: scope (CP-ADR-0060, amendment 2026-09-28)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `name` | string | да |  |
| `version` | any | да |  |
| `status` | string \| null | да |  |
| `scope` | string | да | common: a shared pack; tenant: a pack of the event's tenant |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `name` | string | да |  |
| `version` | any | да |  |
| `status` | string \| null | да |  |

### knowledge.packs_configured

The knowledge packs of a workspace tree were configured.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `workspaceId` | string (uuid) | да |  |
| `namespace` | string | да |  |
| `packs` | array | да |  |
| `strict` | boolean | да |  |

### knowledge.snapshot_reconciled

A knowledge snapshot was reconciled into the memory service (CP-ADR-0060).

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `snapshotId` | any | да |  |
| `pack` | any | да |  |
| `source` | any | да |  |
| `observedAt` | any | да |  |
| `workspaceId` | string (uuid) | да |  |
| `rootWorkspaceId` | string (uuid) | да |  |
| `namespace` | string | да |  |
| `entityCount` | integer | да |  |
| `relationCount` | integer | да |  |
| `duplicate` | boolean | да |  |
| `counters` | object | да |  |

### observation.recorded

An observation was recorded (ADR-0057).

Сущность: `observation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `kind` | string | да |  |
| `content` | any | да |  |
| `observedAt` | string (date-time) | да |  |
| `source` | string | нет |  |
| `dedupKey` | string | нет |  |
| `externalRef` | object | нет |  |
| `assertions` | array | нет |  |
| `data` | object | нет |  |
| `taskId` | string (uuid) | нет |  |
| `runId` | string (uuid) | нет |  |
| `workspaceId` | string (uuid) | нет |  |
| `supersedes` | string (uuid) | нет |  |

### package.settings_changed

The settings of a package have a new version: a PUT /packages/{key}/settings saved other values. No value is in the event — neither the old, the new nor the defaults; who may read them reads GET /packages/{key}/settings/versions (CP-ADR-0081 §5).

Сущность: `package`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `package` | string | да | The key of the package |
| `version` | integer | да | The new version of the values |
| `previousVersion` | integer | да | The version before; 0 for the first saving |
| `schemaRevision` | integer | да | The schema revision the values were checked by |
| `changedPaths` | array | да | JSON Pointers of the members whose saved value changed |
| `actorId` | string (uuid) | да |  |

### principal.created

A principal (human, agent or service) was created.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `kind` | string | да |  |
| `displayName` | string | да |  |

### principal.disabled

A human or agent principal was disabled: bindings and delegations revoked, sessions closed, claims freed, runs failed.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `kind` | string | да |  |
| `previousStatus` | string | да | active or paused |
| `reason` | string \| null | да | Reason given; credential-shaped material redacted, cut to the limit |
| `revokedBindings` | integer | да | IAM bindings revoked |
| `revokedDelegations` | integer | да | Delegations to or from it revoked |
| `closedSessions` | integer | да | Open sessions of it or on its behalf closed |
| `releasedClaims` | integer | да | Active claims released to the queue |
| `failedRuns` | integer | да | Running runs on the released claims failed |
| `withdrawnInvocations` | integer | да | Skill calls on its authority cancelled, leases it held returned |

### principal.enabled

A disabled (or paused) human or agent principal was enabled. Only the status comes back: bindings, delegations, sessions and claims closed by :disable stay closed, IAM entry needs a new binding.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `kind` | string | да |  |
| `previousStatus` | string | да | disabled or paused |
| `reason` | string \| null | да | Reason given; credential-shaped material redacted, cut to the limit |
| `liveApiKeys` | integer | да | Unrevoked, unexpired API keys of it, which authenticate again |

### principal.updated

The display name or the profile of a principal changed (CP-ADR-0082). Names of the changed fields only, never their values: read them via GET /principals/{id}.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |
| `version` | integer | да | Version of the principal after the change |
| `changes` | array | да | Changed fields: displayName, profile.<field> |

### process.cancelled

An operator cancelled the instance.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `reason` | string | да | Reason given; credential-shaped material redacted, cut to the limit |
| `compensated` | boolean | да | Compensations ran before the cancel |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.compensated

Compensations of completed steps ran in reverse order.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `scope` | string | да | all, or the id of the scope compensated |
| `steps` | array | да | Ids of the steps compensated, in the order run |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.completed

The instance completed with an outcome.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `outcome` | string | да |  |
| `memory` | object \| null | да | Case projection {case, facts, entities, documents} evaluated from the process's memory section; null when the process declares none |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.correlated

An event matched start.key or a correlate rule of a running instance.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `triggerEventId` | string \| null (uuid) | да |  |
| `triggerType` | string | да |  |
| `changedFields` | array | да | Data paths the event changed |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.data_changed

Instance data changed; timers depending on the fields were recomputed.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `changedFields` | array | да | Data paths that changed |
| `element` | string \| null | да | Element whose output or set changed them |
| `memory` | object \| null | да | Case projection {case, facts, entities, documents} evaluated from the process's memory section; null when the process declares none |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.definition_published

A new immutable version of a process was published (CP-ADR-0074 §3).

Сущность: `process_definition`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `definitionHash` | string | да |  |
| `previousVersion` | integer \| null | да | Null for the first version of the key |
| `workspaceId` | string \| null (uuid) | да |  |
| `identityAgent` | string \| null | да | Agent key the process acts as |
| `displayName` | string | да |  |
| `governedBy` | array | да | Regulations of the process as a whole: {document, section} |
| `elements` | array | да | Stages, steps, milestones and decision tables: {id, kind, parent, displayName, governedBy} — what the memory projection of the version is built from (CP-ADR-0076 §3) |

### process.definition_restored

A retired process is back in use: a package apply installed it as it is (CP-ADR-0074, amendment Zh3).

Сущность: `process_definition`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `latestVersion` | integer | да |  |
| `packageKey` | string \| null | да |  |
| `packageVersion` | string \| null | да |  |

### process.definition_retired

Every version of a process was retired: no new instances, open ones run to the end (CP-ADR-0074, amendment Zh2).

Сущность: `process_definition`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `latestVersion` | integer | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `reason` | string | да |  |
| `openInstances` | integer | да | Open instances of every workspace of the key |
| `byVersion` | array | да | {version, openInstances} of the versions with open ones |

### process.escalated

An escalation level of a step fired.

Сущность: `process_instance`.

Версия 2 (добавлено: addressees, unresolved: addressees of the to targets (CP-ADR-0078, amendment 2026-09-30)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `element` | string | да |  |
| `level` | integer | да | 1-based escalation level of the step |
| `action` | string | да | remind, reassign, notify or raise |
| `taskId` | string \| null (uuid) | да |  |
| `to` | array | да | Resolved principals the action addresses |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |
| `addressees` | array | нет | One per item of to, in its order: the addressee {principalId, roleId, workspaceId} of the target (a role is the one of the instance's workspace); null when the target does not resolve |
| `unresolved` | array | нет | Why the null addressees did not resolve; empty when all did |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `element` | string | да |  |
| `level` | integer | да | 1-based escalation level of the step |
| `action` | string | да | remind, reassign, notify or raise |
| `taskId` | string \| null (uuid) | да |  |
| `to` | array | да | Resolved principals the action addresses |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.failed

An error reached the top of the instance without a handler.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `error` | object | да |  |
| `element` | string \| null | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.migrated

The instance moved to another version by the process's migration map.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `fromVersion` | integer | да |  |
| `map` | object | да | Old element id -> new element id |
| `policy` | string | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.milestone_lost

A reached milestone stopped holding: its guard is false again (a standing goal is no longer met); it is reached again when the guard holds again.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `milestone` | string | да |  |
| `stage` | string \| null | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.milestone_reached

A milestone of the case was reached.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `milestone` | string | да |  |
| `stage` | string \| null | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.recall_completed

Memory answered a recall step; the answer is in the instance journal (CP-ADR-0076 §4).

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `step` | string | да |  |
| `recallId` | string (uuid) | да | Id of the recall intent |
| `asOf` | string (date-time) | да |  |
| `nodeCount` | integer | да |  |
| `edgeCount` | integer | да |  |
| `truncated` | boolean | да |  |
| `resultHash` | string | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.recall_timed_out

A recall step got no answer in time; the step's onTimeout runs.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `step` | string | да |  |
| `recallId` | string (uuid) | да |  |
| `reason` | string | да | timeout, memory_unavailable or memory_disabled |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.resumed

The instance was resumed; frozen timers got their remaining time back.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `cause` | string | да | event or operator |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.sla_breached

A deadline passed while the step (process) is open; one per attempt.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `scope` | string | да | step or process |
| `element` | string \| null | да | Id of the step; null for the process scope |
| `attempt` | integer \| null | да | Attempt of the step; null for the process scope |
| `activityId` | string \| null (uuid) | да | Activity of the attempt; null for the process scope |
| `dueAt` | string (date-time) | да | Declared deadline: the due time of the deadline timer |
| `detectedAt` | string (date-time) | да | When the core processed the breach |
| `overdueSeconds` | integer | да | detectedAt - dueAt, seconds, not negative |
| `detectedBy` | string | да | timer or migration |
| `provisional` | boolean | да | Computed on a provisional calendar year |
| `owner` | object \| null | да | Addressee {principalId, roleId, workspaceId}, one of principalId and roleId set: the first resolvable candidate of spec.owner; null when none resolves |
| `assignee` | object \| null | да | Addressee {principalId, roleId, workspaceId} the step is assigned to; null for the process scope or an unassigned step |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.sla_failed

A deadline could not be computed; the instance goes on, its SLA state is unknown.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `scope` | string | да | step or process |
| `element` | string \| null | да | Id of the step; null for the process scope |
| `attempt` | integer \| null | да | Attempt of the step; null for the process scope |
| `activityId` | string \| null (uuid) | да | Activity of the attempt; null for the process scope |
| `error` | object | да | calendar_missing or an expression error |
| `owner` | object \| null | да | Addressee {principalId, roleId, workspaceId}, one of principalId and roleId set: the first resolvable candidate of spec.owner; null when none resolves |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.sla_warning

The warning threshold of a deadline passed while the step (process) is open.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `scope` | string | да | step or process |
| `element` | string \| null | да | Id of the step; null for the process scope |
| `attempt` | integer \| null | да | Attempt of the step; null for the process scope |
| `activityId` | string \| null (uuid) | да | Activity of the attempt; null for the process scope |
| `dueAt` | string (date-time) | да | Declared deadline: the due time of the deadline timer |
| `warnAt` | string (date-time) | да |  |
| `provisional` | boolean | да | Computed on a provisional calendar year |
| `owner` | object \| null | да | Addressee {principalId, roleId, workspaceId}, one of principalId and roleId set: the first resolvable candidate of spec.owner; null when none resolves |
| `assignee` | object \| null | да | Addressee {principalId, roleId, workspaceId} the step is assigned to; null for the process scope or an unassigned step |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.stage_entered

A stage of the case was entered.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `stage` | string | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.stage_exited

A stage of the case was exited.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `stage` | string | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.started

A process instance started from its start trigger.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `triggerEventId` | string \| null (uuid) | да | Journal event that started it |
| `triggerType` | string | да |  |
| `memory` | object \| null | да | Case projection {case, facts, entities, documents} evaluated from the process's memory section; null when the process declares none |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.step_entered

A waiting step (activity) of the instance opened.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `element` | string | да | Id of the waiting step |
| `stage` | string \| null | да | Stage the step belongs to; null outside stages |
| `stepKind` | string | да | human, approve, call, recall, listen or wait |
| `attempt` | integer | да | 1-based number of the entry into this element in the instance |
| `activityId` | string (uuid) | да | Activity of this attempt |
| `enteredAt` | string (date-time) | да |  |
| `waitsFor` | string | да | task, approval, skill, agent, child, event, time or memory |
| `taskId` | string \| null (uuid) | да | Task the step waits for |
| `approvalIds` | array | да | Approvals the step waits for |
| `skillInvocationId` | string \| null (uuid) | да | Skill invocation the step waits for |
| `childInstanceId` | string \| null (uuid) | да | Child instance the step waits for |
| `due` | string \| null (date-time) | да | Declared deadline of the step; null without due |
| `warnAt` | string \| null (date-time) | да | Warning threshold; null without warnBefore |
| `provisional` | boolean | да | The deadline is computed on a provisional year |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.step_exited

A waiting step (activity) of the instance closed.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `element` | string | да | Id of the waiting step |
| `stage` | string \| null | да | Stage the step belongs to; null outside stages |
| `stepKind` | string | да | human, approve, call, recall, listen or wait |
| `attempt` | integer | да | 1-based number of the entry into this element in the instance |
| `activityId` | string (uuid) | да | Activity of this attempt |
| `enteredAt` | string (date-time) | да |  |
| `exitedAt` | string (date-time) | да |  |
| `outcome` | string | да | completed, cancelled, withdrawn (a participant cancelled the step's task, or the last approval of the step, outside the process; the event's actorId is that participant), interrupted, failed, timed_out or migrated |
| `durationSeconds` | integer | да | exitedAt - enteredAt, wall-clock seconds |
| `due` | string \| null (date-time) | да | Declared deadline of the step; null without due |
| `breached` | boolean | да | The step closed after its deadline |
| `overdueSeconds` | integer \| null | да | exitedAt - due when breached, else null |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.suspended

The instance was suspended; its timers froze.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `cause` | string | да | event (a suspend block) or operator |
| `reason` | string | да | Reason given; credential-shaped material redacted, cut to the limit |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.timer_fired

A timer of the instance fired; the engine takes it as its next event.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `timerId` | string (uuid) | да |  |
| `element` | string | да |  |
| `dueAt` | string (date-time) | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### process.timer_rescheduled

A pending timer moved: data or a calendar it reads changed, or on resume.

Сущность: `process_instance`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `instanceId` | string (uuid) | да |  |
| `definitionKey` | string | да |  |
| `version` | integer | да | Version of the process the instance is pinned to |
| `instanceKey` | string | да | Value of start.key; unique per definition key |
| `timerId` | string (uuid) | да |  |
| `element` | string | да |  |
| `previousDueAt` | string (date-time) | да |  |
| `dueAt` | string (date-time) | да |  |
| `provisional` | boolean | да | Computed on a provisional calendar year |
| `cause` | string | да | data_changed, calendar_changed, resumed or migrated (deadlines recomputed by the new version, CP-ADR-0074 amendment 2026-09-29 §11) |
| `changedFields` | array | да |  |
| `workspaceId` | string \| null (uuid) | нет | Workspace of the instance: the scope of its case in memory |

### project.archived

The project was archived.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `workspaceId` | string (uuid) | да |  |
| `version` | integer | да |  |

### project.config_revision_activated

A configuration revision became the active one.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `revision` | integer | да |  |
| `revisionId` | string (uuid) | да |  |
| `version` | integer | да |  |

### project.config_revision_created

A new configuration revision of the project was drafted.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `revision` | integer | да |  |
| `revisionId` | string (uuid) | да |  |
| `comment` | any | да |  |

### project.created

A project was created on a workspace (ADR-0031).

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `workspaceId` | string (uuid) | да |  |
| `parentProjectId` | string \| null (uuid) | да |  |
| `templateKey` | string | да |  |
| `templateVersion` | integer | да |  |
| `statusKey` | string | да |  |
| `systemStatusCategory` | string | да |  |

### project.external_reference_added

A reference to an external system was attached to the project (ADR-0047).

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `externalReferenceId` | string (uuid) | да |  |
| `externalSystem` | string | да |  |
| `externalType` | string | да |  |
| `externalId` | string | да |  |

### project.external_reference_updated

An external reference of the project changed.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `externalReferenceId` | string (uuid) | да |  |
| `externalSystem` | string | да |  |
| `externalType` | string | да |  |
| `externalId` | string | да |  |

### project.status_changed

The project moved to another status.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `fromStatusKey` | string | да |  |
| `fromSystemStatusCategory` | string | да |  |
| `statusKey` | string | да |  |
| `systemStatusCategory` | string | да |  |
| `comment` | any | да |  |
| `version` | integer | да |  |

### project.updated

Project attributes changed.

Сущность: `project`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changes` | array | да |  |
| `version` | integer | да |  |

### project_template.created

A project template version was created.

Сущность: `project_template`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `displayName` | string | да |  |
| `initialStatus` | any | да |  |

### project_template.deprecated

A project template version was deprecated.

Сущность: `project_template`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |

### role.assigned

A role was assigned to the principal, tenant-wide or in a workspace subtree.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `roleId` | string (uuid) | да |  |
| `workspaceId` | string \| null (uuid) | да |  |

### role.created

A role was created, tenant-wide or in a workspace.

Сущность: `role`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `slug` | string | да |  |
| `name` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |

### role.revoked

A role assignment was revoked.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `roleId` | string (uuid) | да |  |
| `workspaceId` | string \| null (uuid) | да |  |

### role.updated

A role was renamed or redescribed.

Сущность: `role`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changes` | any | да |  |
| `version` | integer | да |  |

### rule.archived

A work rule was archived.

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `key` | string | да |  |
| `version` | integer | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `trigger` | object | да |  |
| `skill` | any | да |  |
| `action` | object | да |  |

### rule.created

A work rule was created (CP-ADR-0063).

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `key` | string | да |  |
| `version` | integer | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `trigger` | object | да |  |
| `skill` | any | да |  |
| `action` | object | да |  |

### rule.disabled

A work rule was disabled.

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `key` | string | да |  |
| `version` | integer | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `trigger` | object | да |  |
| `skill` | any | да |  |
| `action` | object | да |  |

### rule.enabled

A work rule was enabled.

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `key` | string | да |  |
| `version` | integer | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `trigger` | object | да |  |
| `skill` | any | да |  |
| `action` | object | да |  |

### rule.evaluated

A work rule was evaluated against a trigger.

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `ruleKey` | string | да |  |
| `ruleVersion` | integer | да |  |
| `evaluationId` | string (uuid) | да |  |
| `triggerRef` | any | да |  |
| `trigger` | object | да |  |
| `result` | string | да |  |
| `conditionMatched` | any | да |  |
| `evidence` | array | да |  |
| `skillInvocationId` | string \| null (uuid) | да |  |
| `work` | any | да |  |
| `error` | object \| null | да |  |

### rule.updated

A work rule changed.

Сущность: `rule`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `key` | string | да |  |
| `version` | integer | да |  |
| `status` | string | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `trigger` | object | да |  |
| `skill` | any | да |  |
| `action` | object | да |  |
| `changes` | array | да |  |

### run.cancel_requested

Cancellation of the run was requested.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `reason` | any | нет |  |
| `controlMessageId` | string (uuid) | нет |  |

### run.cancelled

The run was cancelled.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `reason` | any | да |  |
| `attempt` | integer | да |  |
| `controlMessageId` | string (uuid) | нет |  |

### run.checkpointed

The run left a checkpoint.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `checkpointId` | string (uuid) | да |  |
| `seq` | integer | да |  |
| `kind` | string | да |  |

### run.child.cancel_requested

Cancellation of the child run was requested.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `childHandleId` | string (uuid) | да |  |
| `correlationId` | any | да |  |
| `childRunId` | string \| null (uuid) | да |  |
| `controlMessageId` | string (uuid) | да |  |
| `reason` | any | да |  |

### run.child.launched

The run launched a child task under a handle (ADR-0046).

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `childHandleId` | string (uuid) | да |  |
| `childTaskId` | string (uuid) | да |  |
| `childTaskPublicId` | string | да |  |
| `correlationId` | any | да |  |
| `cancellationPolicy` | any | да |  |
| `depth` | integer | да |  |
| `grantSizes` | object | да |  |

### run.child.resolved

The child handle was resolved with the child's outcome.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `childHandleId` | string (uuid) | да |  |
| `correlationId` | any | да |  |
| `childRunId` | string \| null (uuid) | да |  |
| `outcome` | any | да |  |
| `resultHash` | any | да |  |
| `artifactRefs` | array | да |  |

### run.child.revoked

The child handle was revoked.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `childHandleId` | string (uuid) | да |  |
| `correlationId` | any | да |  |
| `childTaskId` | string (uuid) | да |  |
| `reason` | any | да |  |

### run.child.started

A run of the child task started.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `childHandleId` | string (uuid) | да |  |
| `correlationId` | any | да |  |
| `childTaskId` | string (uuid) | да |  |
| `childRunId` | string (uuid) | да |  |
| `attempt` | integer | да |  |

### run.control_message.accepted

A control message for the active turn was accepted (ADR-0044).

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `controlMessageId` | string (uuid) | да |  |
| `seq` | integer | да |  |
| `operation` | any | да |  |
| `status` | string | да |  |
| `causalPosition` | any | да |  |

### run.control_message.applied

A control message was applied.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `controlMessageId` | string (uuid) | да |  |
| `seq` | integer | да |  |
| `operation` | any | да |  |
| `status` | string | да |  |
| `causalPosition` | any | да |  |
| `safeBoundary` | any | да |  |

### run.control_message.rejected

A control message was rejected.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `controlMessageId` | string (uuid) | да |  |
| `seq` | integer | да |  |
| `operation` | any | да |  |
| `status` | string | да |  |
| `causalPosition` | any | да |  |
| `safeBoundary` | any | да |  |

### run.control_message.superseded

A control message was superseded.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `controlMessageId` | string (uuid) | да |  |
| `seq` | integer | да |  |
| `operation` | any | да |  |
| `status` | string | да |  |
| `causalPosition` | any | да |  |
| `safeBoundary` | any | да |  |

### run.failed

The run failed.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `reason` | any | да |  |
| `attempt` | integer | да |  |

### run.handoff_prepared

The run prepared a handoff to another executor.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `claimId` | string (uuid) | да |  |
| `checkpointId` | string (uuid) | да |  |
| `fencingToken` | integer | да |  |
| `reason` | any | да |  |

### run.manifest_compiled

The effective harness manifest of the run was compiled (ADR-0043). No longer written since ADR-0073; kept for events already in the journal.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `manifestId` | string (uuid) | да |  |
| `version` | integer | да |  |
| `baseHash` | any | да |  |
| `reason` | any | да |  |
| `modelAttempt` | any | да |  |
| `supersedesVersion` | any | да |  |

### run.manifest_ephemeral_recorded

An ephemeral manifest change was recorded (ADR-0043). No longer written since ADR-0073; kept for events already in the journal.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `manifestId` | string (uuid) | да |  |
| `version` | integer | да |  |
| `seq` | integer | да |  |
| `kind` | any | да |  |

### run.started

An execution attempt started under a claim.

Сущность: `run`.

Версия 2 (добавлено: agentRevisionId):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `claimId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `fencingToken` | integer | да |  |
| `instructionsHash` | any | да |  |
| `instructionsRefs` | any | да |  |
| `agentRevisionId` | string \| null (uuid) | да | Agent revision the run goes by (CP-ADR-0073 §7); null for executors that are not registered agents |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `claimId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `fencingToken` | integer | да |  |
| `instructionsHash` | any | да |  |
| `instructionsRefs` | any | да |  |

### run.succeeded

The run finished successfully.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `taskCompleted` | boolean | да |  |

### run.suspended

The run was suspended, e.g. to wait for a decision.

Сущность: `run`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `taskId` | string (uuid) | да |  |
| `reason` | any | да |  |
| `attempt` | integer | да |  |
| `waitingForApprovalId` | string \| null (uuid) | нет |  |

### session.closed

A work session was closed.

Сущность: `session`.

Версия 2 (добавлено: reason):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `releasedClaims` | array | да |  |
| `reason` | string | нет | principal_disabled when its principal, or the human it acted for, was disabled (CP-ADR-0077) |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `releasedClaims` | array | да |  |

### session.expired

A work session expired; its claims were released.

Сущность: `session`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `expiresAt` | any | да |  |
| `releasedClaims` | array | нет |  |

### session.opened

A harness opened a work session.

Сущность: `session`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `clientName` | any | да |  |
| `harnessType` | any | да |  |
| `controlLevel` | any | да |  |
| `protocolVersion` | any | да |  |
| `onBehalfOf` | string \| null (uuid) | да |  |
| `expiresAt` | string (date-time) | да |  |

### skill.assigned

A skill was assigned.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |

### skill.invocation_cancelled

The invocation was cancelled.

Сущность: `skill_invocation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `skill` | string | да |  |
| `version` | any | да |  |
| `attempt` | integer | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `code` | any | да |  |
| `reason` | string | да |  |
| `wasRunning` | boolean | да |  |
| `cancelledBy` | string \| null (uuid) | да |  |
| `initiator` | any | да |  |

### skill.invocation_claimed

An executor took the invocation under a lease.

Сущность: `skill_invocation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `fencingToken` | integer | да |  |
| `leaseExpiresAt` | string (date-time) | да |  |

### skill.invocation_failed

The invocation failed for good.

Сущность: `skill_invocation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `skill` | string | да |  |
| `version` | any | да |  |
| `attempt` | integer | да |  |
| `maxAttempts` | integer | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `error` | object | да |  |

### skill.invocation_requested

The core was asked to invoke a skill (CP-ADR-0056).

Сущность: `skill_invocation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `skill` | string | да |  |
| `version` | any | да |  |
| `sideEffects` | any | да |  |
| `riskLevel` | any | да |  |
| `requestedBy` | object | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `authorizationBasis` | any | да |  |

### skill.invocation_retry_scheduled

The attempt failed with a retryable error; another one is scheduled.

Сущность: `skill_invocation`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `maxAttempts` | integer | да |  |
| `availableAt` | string (date-time) | да |  |
| `error` | object | да |  |

### skill.invocation_succeeded

The invocation finished; its result is an artifact.

Сущность: `skill_invocation`.

Версия 2 (добавлено: outputs (CP-ADR-0072 amendment 2026-10-01)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `skill` | string | да |  |
| `version` | any | да |  |
| `attempt` | integer | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `cost` | any | да |  |
| `outputs` | array | да | Typed outputs of the executed task: {key, type, status (created | absent | missing | rejected), artifactId?, reason?}; empty unless this is the task's execution call |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |
| `skill` | string | да |  |
| `version` | any | да |  |
| `attempt` | integer | да |  |
| `taskId` | string \| null (uuid) | да |  |
| `runId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |
| `cost` | any | да |  |

### skill.registered

A skill version was registered.

Сущность: `skill`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `name` | string | да |  |
| `version` | any | да |  |
| `protocol` | any | да |  |
| `invocable` | boolean | да |  |
| `sideEffects` | any | да |  |
| `riskLevel` | any | да |  |

### skill.revoked

A skill was revoked.

Сущность: `principal`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `skillId` | string (uuid) | да |  |

### skill.updated

The description, status or implementation endpoint of a skill version changed.

Сущность: `skill`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changedFields` | array | да |  |
| `rowVersion` | integer | да |  |
| `endpoint` | object | нет |  |

### task.claimed

An executor claimed the task under a lease.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `claimId` | string (uuid) | да |  |
| `sessionId` | string \| null (uuid) | да |  |
| `holderId` | string (uuid) | да |  |
| `fencingToken` | integer | да |  |
| `expiresAt` | string (date-time) | да |  |
| `status` | string | да |  |
| `systemStatusCategory` | string | да |  |
| `version` | integer | да |  |

### task.comment_added

A comment was added to the task.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `commentId` | string (uuid) | да |  |
| `authorPrincipalId` | string (uuid) | да |  |
| `version` | integer | да |  |
| `bodyLength` | integer | да |  |
| `runId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |

### task.comment_edited

A task comment was edited.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `commentId` | string (uuid) | да |  |
| `authorPrincipalId` | string (uuid) | да |  |
| `version` | integer | да |  |
| `bodyLength` | integer | да |  |
| `runId` | string \| null (uuid) | да |  |
| `artifactId` | string \| null (uuid) | да |  |

### task.completed

The task reached its completion status.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `status` | string | да |  |
| `systemStatusCategory` | string | да |  |
| `version` | integer | да |  |
| `verificationId` | string (uuid) | нет |  |
| `attempt` | integer | нет |  |

### task.completion_work_executed

The completion work the task type declares was executed (ADR-0061).

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `taskTypeId` | string (uuid) | да |  |
| `actions` | array | да |  |

### task.completion_work_failed

An action of the completion work failed; the completion stands.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `taskTypeId` | string (uuid) | да |  |
| `failedAction` | object | да |  |
| `actions` | array | да |  |

### task.context_pack_recorded

The context pack assembled for the task on claim was recorded (CP-ADR-0064).

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `contextPackId` | string (uuid) | да |  |
| `claimId` | string \| null (uuid) | да |  |
| `asOf` | string | да |  |
| `asOfMode` | string | да |  |
| `entities` | integer | да |  |
| `facts` | integer | да |  |
| `snapshots` | integer | да |  |

### task.created

A task was created.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `title` | string | да |  |
| `status` | string | да |  |
| `systemStatusCategory` | string | да |  |
| `typeKey` | string | да |  |
| `typeVersion` | integer | да |  |
| `priority` | any | да |  |
| `workspaceId` | string \| null (uuid) | да |  |
| `startDate` | string \| null | да |  |
| `dueDate` | string \| null | да |  |
| `customFields` | boolean | да |  |
| `goalId` | string \| null (uuid) | да |  |
| `origin` | any | да |  |
| `acceptanceChecks` | integer | да |  |

### task.external_reference_added

A reference to an external system was attached to the task (ADR-0047).

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `externalReferenceId` | string (uuid) | да |  |
| `externalSystem` | string | да |  |
| `externalType` | string | да |  |
| `externalId` | string | да |  |

### task.external_reference_updated

An external reference of the task changed.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `externalReferenceId` | string (uuid) | да |  |
| `externalSystem` | string | да |  |
| `externalType` | string | да |  |
| `externalId` | string | да |  |

### task.relation_added

A relation to another task was added.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `relationId` | string (uuid) | да |  |
| `toTaskId` | string (uuid) | да |  |
| `type` | string | да |  |

### task.relation_removed

A relation between tasks was removed.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `relationId` | string (uuid) | да |  |
| `fromTaskId` | string (uuid) | да |  |
| `toTaskId` | string (uuid) | да |  |
| `type` | string | да |  |

### task.type_migrated

The task was moved to another version of its type (ADR-0048, amendment 2026-09-30).

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `typeKey` | string | да |  |
| `fromTypeVersion` | integer | да |  |
| `typeVersion` | integer | да |  |
| `fromStatus` | string | да |  |
| `status` | string | да |  |
| `systemStatusCategory` | string | да |  |
| `trigger` | string | да | task (one task) or bulk (:migrate-tasks of a version) |
| `version` | integer | да |  |

### task.updated

Task attributes or its status changed.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `changes` | any | да |  |
| `version` | integer | да |  |
| `fromStatus` | string | нет |  |
| `status` | string | нет |  |
| `systemStatusCategory` | string | нет |  |

### task.verification_failed

An acceptance check failed; the task went back to its executor or got blocked.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `taskId` | string (uuid) | да |  |
| `verificationId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `trigger` | any | да |  |
| `checks` | integer | да |  |
| `results` | array | да |  |
| `failedCheck` | any | да |  |
| `reason` | any | да |  |
| `consecutiveFailures` | integer | да |  |
| `blocked` | boolean | да |  |
| `fromStatus` | string | да |  |
| `status` | string | да |  |
| `systemStatusCategory` | string | да |  |

### task.verification_started

A verification attempt of the task's acceptance checks opened (CP-ADR-0067).

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `taskId` | string (uuid) | да |  |
| `verificationId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `trigger` | any | да |  |
| `checks` | integer | да |  |
| `triggerRef` | any | нет |  |

### task.verified

Every acceptance check passed; the task is complete.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `publicId` | string | да |  |
| `taskId` | string (uuid) | да |  |
| `verificationId` | string (uuid) | да |  |
| `attempt` | integer | да |  |
| `trigger` | any | да |  |
| `checks` | integer | да |  |
| `results` | array | да |  |
| `artifactId` | string (uuid) | да |  |

### task_type.created

A task type version was created (ADR-0048).

Сущность: `task_type`.

Версия 3 (добавлено: executorRoles (CP-ADR-0048, amendment 2026-10-03 A1)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `displayName` | string | да |  |
| `initialStatus` | any | да |  |
| `completionStatus` | any | да |  |
| `execution` | any | да |  |
| `declaresApprovalOutcomes` | boolean | да |  |
| `declaresContextProfile` | boolean | да |  |
| `declaresInstructions` | boolean | да |  |
| `declaresCompletionWork` | boolean | да |  |
| `declaresArtifactSchema` | boolean | да |  |
| `inputs` | integer | да | Number of declared artifact inputs |
| `outputs` | integer | да | Number of declared artifact outputs |
| `executorRoles` | array | да | Slugs of the roles a person needs to take work of the version |

Версия 2 (добавлено: declaresArtifactSchema, inputs, outputs (CP-ADR-0072)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `displayName` | string | да |  |
| `initialStatus` | any | да |  |
| `completionStatus` | any | да |  |
| `execution` | any | да |  |
| `declaresApprovalOutcomes` | boolean | да |  |
| `declaresContextProfile` | boolean | да |  |
| `declaresInstructions` | boolean | да |  |
| `declaresCompletionWork` | boolean | да |  |
| `declaresArtifactSchema` | boolean | да |  |
| `inputs` | integer | да | Number of declared artifact inputs |
| `outputs` | integer | да | Number of declared artifact outputs |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |
| `displayName` | string | да |  |
| `initialStatus` | any | да |  |
| `completionStatus` | any | да |  |
| `execution` | any | да |  |
| `declaresApprovalOutcomes` | boolean | да |  |
| `declaresContextProfile` | boolean | да |  |
| `declaresInstructions` | boolean | да |  |
| `declaresCompletionWork` | boolean | да |  |

### task_type.deprecated

A task type version was deprecated.

Сущность: `task_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `version` | integer | да |  |

### tenant.bootstrapped

The tenant was created with its first administrator.

Сущность: `tenant`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `slug` | string | да |  |
| `adminPrincipalId` | string (uuid) | да |  |
| `apiKeyId` | string (uuid) | да |  |
| `apiKeyPrefix` | string | да |  |
| `iamBindingId` | string \| null (uuid) | да |  |
| `iamPrincipalId` | any | да |  |

### view.published

A view of a package is in use at a revision: published by a package apply, or brought back as it was; a console drops what it cached of the key (CP-ADR-0080).

Сущность: `view`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `revision` | integer | да |  |
| `hash` | string | да | sha256 of the revision, as GET /views/{key} returns it |
| `previousRevision` | integer \| null | да | The revision before; null for a new view |
| `packageKey` | string \| null | да |  |
| `packageVersion` | string \| null | да |  |

### view.retired

A view of a package is out of use: the package that installed it no longer brings it (CP-ADR-0080).

Сущность: `view`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `revision` | integer | да | The last revision of the view |
| `reason` | string | да |  |
| `packageKey` | string \| null | да |  |
| `packageVersion` | string \| null | да |  |

### work.derived

A rule derived new work.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `ruleKey` | string | да |  |
| `ruleVersion` | integer | да |  |
| `evaluationId` | string (uuid) | да |  |
| `taskId` | string (uuid) | да |  |
| `publicId` | string | да |  |
| `evidence` | array | да |  |
| `action` | string | да |  |
| `dedupKey` | string | да |  |
| `created` | boolean | да |  |
| `approvalId` | string (uuid) | нет |  |

### work.reconciled

A rule updated, cancelled or completed work: the work it derived earlier, or the task an observation is bound to.

Сущность: `task`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `ruleId` | string (uuid) | да |  |
| `ruleKey` | string | да |  |
| `ruleVersion` | integer | да |  |
| `evaluationId` | string (uuid) | да |  |
| `taskId` | string (uuid) | да |  |
| `publicId` | string | да |  |
| `evidence` | array | да |  |
| `action` | string | да |  |
| `dedupKey` | string | да |  |
| `changes` | array | да |  |
| `verificationId` | string (uuid) | нет |  |
| `check` | any | нет |  |
| `target` | string | нет |  |

### workspace.archived

A workspace was archived.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `slug` | string | да |  |

### workspace.created

A workspace was created.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `slug` | string | да |  |
| `name` | string | да |  |
| `parentId` | string \| null (uuid) | да |  |
| `typeKey` | any | да |  |

### workspace.member_added

A principal became a member of the workspace.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |

### workspace.member_removed

A principal stopped being a member of the workspace.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `principalId` | string (uuid) | да |  |

### workspace.moved

A workspace moved under another parent.

Сущность: `workspace`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `fromParentId` | string \| null (uuid) | да |  |
| `toParentId` | string \| null (uuid) | да |  |

### workspace.updated

Workspace attributes changed.

Сущность: `workspace`.

Версия 2 (добавлено: taskTypes (CP-ADR-0008, amendment 2026-10-03 A2)):

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changes` | any | да |  |
| `version` | integer | да |  |
| `taskTypes` | array \| null | нет | New own setting of the allowed task types, when it changed; null inherits from the ancestors |

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changes` | any | да |  |
| `version` | integer | да |  |

### workspace_type.archived

A workspace type was archived.

Сущность: `workspace_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |

### workspace_type.created

A workspace type was created.

Сущность: `workspace_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `key` | string | да |  |
| `displayName` | string | да |  |
| `allowedChildTypes` | any | да |  |

### workspace_type.updated

A workspace type changed.

Сущность: `workspace_type`.

Версия 1:

| Поле | Тип | Всегда | Описание |
|---|---|---|---|
| `changes` | any | да |  |
| `version` | integer | да |  |
