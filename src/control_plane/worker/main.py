"""Background worker: outbox delivery, lease expiry sweeps, idempotency GC.

It is also the executor of approval outcomes declared by task types
(CP-ADR-0061): a decided gate approval with a live outcome is picked up from
the approvals themselves, not from the outbox, so outcome retries and outbox
delivery never hold each other up. Outcome actions are authorized by the same
authorizer (``CP_AUTHZ_MODE``) the API uses, configured here at start-up.

It is the rule engine too (CP-ADR-0063): it reads each tenant's journal with
its own cursor, evaluates enabled work rules on observations, core events and
schedules, and resumes evaluations whose interpretation skill call has ended.

And it runs the verification stage (CP-ADR-0067): open attempts of tasks
handed in with acceptance checks are executed check by check, each look in
its own transaction, waiting between looks on ``next_check_at``.

It runs the process instances too (CP-ADR-0074 §5, §8): its own journal
cursor ``processes`` feeds the events of each tenant to the instances they
start, correlate or answer (``process_events``), and due rows of
``process_timers`` become the instances' timer inputs (``process_timers``).
The ``recall`` steps of instances are asked of memory here, outside any
transaction, and answered into their journals (``process_recalls``,
CP-ADR-0076 §4).

It keeps agents' access in the secret store (CP-ADR-0079 §9,
``connections-policy-sync``): its own journal cursor per tenant brings the
policies and roles of a tenant's agents to the records after the events that
change them, and a full pass every ``connections_sync_seconds`` does it for
every tenant, expires keys past ``expiresAt`` and deletes policies of
principals no agent has. An unavailable store holds the tenant back; it never
stops the worker.

With a content store configured it also sweeps artifact uploads no artifact
referenced before they expired, and the objects nothing needs any more
(CP-ADR-0072 §10).

The worker is an availability optimization, not a correctness requirement:
claims and sessions are also reaped lazily by the commands themselves.

Outbox processing uses ``FOR UPDATE SKIP LOCKED`` so several workers can run
side by side; a crash mid-batch rolls the transaction back and the records
are picked up again (at-least-once delivery).
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from control_plane.application.authorization import Authorizer, configure_authorizer
from control_plane.application.commands import connection_policies, process_instances
from control_plane.application.commands._claim_release import (
    release_active_claims_for_session,
    release_claim_on_locked_task,
)
from control_plane.application.commands.approval_outcomes import (
    due_outcomes,
    execute_outcome,
    record_attempt_failure,
)
from control_plane.application.commands.artifacts import sweep_expired_uploads
from control_plane.application.commands.catalog_retirements import is_key_busy
from control_plane.application.commands.rule_evaluations import (
    due_evaluations,
    due_rule_tenants,
    due_schedules,
    postpone_evaluation,
    process_tenant_events,
    record_schedule_failure,
    record_tenant_failure,
    resume_evaluation,
    run_schedule,
)
from control_plane.application.commands.skill_invocations import (
    expire_leases as expire_skill_leases,
)
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.verification import (
    Timing,
    due_verifications,
    execute_verification,
    postpone_verification,
)
from control_plane.application.common import utcnow
from control_plane.application.events import record_event
from control_plane.config import Settings
from control_plane.domain.enums import ClaimStatus, SessionStatus
from control_plane.infrastructure.auth.policy import build_authorizer
from control_plane.infrastructure.content_store import ContentStore, build_content_store
from control_plane.infrastructure.context_provider import GraphProvider, build_context_provider
from control_plane.infrastructure.db.engine import (
    build_engine,
    build_session_factory,
    transaction,
)
from control_plane.infrastructure.db.models import (
    ConnectionOAuthState,
    IdempotencyKey,
    OutboxRecord,
    Session,
    Task,
    TaskClaim,
)
from control_plane.infrastructure.secret_store import (
    SecretStore,
    SecretStoreError,
    build_secret_store,
)

logger = logging.getLogger(__name__)

_SWEEP_BATCH = 100
# A state of :authorize is kept this long after it was issued (CP-ADR-0079 §6):
# past its TTL it cannot be used, and a hash without the state is worth nothing.
_OAUTH_STATE_RETENTION = timedelta(days=1)


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        authorizer: Authorizer | None = None,
        content_store: ContentStore | None = None,
        graph_provider: GraphProvider | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.settings = settings
        # Agents' access in the store (CP-ADR-0079 §9); None without CP_SECRET_STORE_URL.
        self.secret_store = (
            secret_store if secret_store is not None else build_secret_store(settings)
        )
        # Monotonic time of the next full pass of connections-policy-sync: the first cycle.
        self._next_policy_pass = 0.0
        self.engine = engine or build_engine(settings)
        self.content_store = (
            content_store if content_store is not None else build_content_store(settings)
        )
        # Memory for the recall steps of processes; None when it is disabled.
        self.graph_provider: GraphProvider | None = (
            graph_provider if graph_provider is not None else build_context_provider(settings)  # type: ignore[assignment]
        )
        # Outcome actions pass through the ordinary commands, which ask the
        # process-wide authorizer: without this the worker would answer from
        # the flat permission snapshot while the API asks the PDP, and a
        # replay through the API could decide differently (CP-ADR-0061 §5).
        if authorizer is None:
            authorizer, self._authz_closables = build_authorizer(settings)
        else:
            self._authz_closables = []
        self.authorizer = authorizer
        configure_authorizer(authorizer)
        self.session_factory = build_session_factory(self.engine)
        self.name = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        # One trace id per worker process: maintenance events are not caused by
        # any client request, so there is no inbound X-Run-Id to propagate.
        self.trace_run_id = f"worker_{uuid.uuid4().hex}"
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)
        logger.info("worker started", extra={"worker": self.name})
        consecutive_failures = 0
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                    consecutive_failures = 0
                    delay = self.settings.worker_poll_interval_seconds
                except Exception:
                    # A transient DB outage must not kill the worker process.
                    consecutive_failures += 1
                    delay = min(
                        self.settings.worker_poll_interval_seconds * (2**consecutive_failures),
                        30.0,
                    )
                    logger.exception(
                        "worker cycle failed",
                        extra={"worker": self.name, "failures": consecutive_failures},
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay)
        finally:
            await self.aclose()
            logger.info("worker stopped", extra={"worker": self.name})

    async def aclose(self) -> None:
        for closable in self._authz_closables:
            aclose = getattr(closable, "aclose", None)
            if aclose is not None:
                await aclose()
        if self.content_store is not None:
            await self.content_store.aclose()
        if self.secret_store is not None:
            await self.secret_store.aclose()
        aclose_graph = getattr(self.graph_provider, "aclose", None)
        if aclose_graph is not None:
            await aclose_graph()
        await self.engine.dispose()

    async def run_once(self) -> dict[str, int]:
        """One maintenance cycle; each sub-task runs in its own transaction."""
        stats = {
            "outbox_delivered": await self.process_outbox(),
            "outcomes_processed": await self.process_outcomes(),
            "rule_events_read": await self.process_rule_events(),
            "rule_schedules_run": await self.process_rule_schedules(),
            "rule_evaluations_resumed": await self.resume_rule_evaluations(),
            "verifications_processed": await self.process_verifications(),
            "process_events_read": await self.process_events(),
            "process_timers_fired": await self.process_timers(),
            "process_recalls_answered": await self.process_recalls(),
            "sessions_expired": await self.expire_sessions(),
            "claims_expired": await self.expire_claims(),
            "skill_leases_expired": await self.expire_skill_invocation_leases(),
            "idempotency_cleaned": await self.cleanup_idempotency(),
            "oauth_states_cleaned": await self.cleanup_oauth_states(),
            "connection_keys_expired": await self.expire_connection_keys(),
            "connection_policy_events_read": await self.process_connection_policy_events(),
            "connection_policy_changes": await self.sync_connection_policies(),
            "artifact_uploads_swept": await self.sweep_artifact_uploads(),
        }
        if any(stats.values()):
            logger.info("worker cycle", extra={"worker": self.name, **stats})
        return stats

    # -- outbox ---------------------------------------------------------------

    async def deliver(self, record: OutboxRecord) -> None:
        """Deliver one outbox record.

        MVP delivery target is the structured log stream (a downstream
        consumer tails it); this method is the extension point for webhooks
        or brokers later.
        """
        logger.info(
            "outbox delivered",
            extra={
                "topic": record.topic,
                "event_id": str(record.event_id),
                "tenant_id": str(record.tenant_id),
                "outbox_payload": record.payload,
            },
        )

    async def process_outbox(self) -> int:
        now = utcnow()
        delivered = 0
        async with transaction(self.session_factory) as session:
            records = (
                await session.scalars(
                    select(OutboxRecord)
                    .where(
                        OutboxRecord.delivered_at.is_(None),
                        OutboxRecord.available_at <= now,
                        OutboxRecord.attempt_count < self.settings.outbox_max_attempts,
                    )
                    .order_by(OutboxRecord.available_at)
                    .limit(self.settings.outbox_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for record in records:
                record.locked_at = utcnow()
                record.locked_by = self.name
                try:
                    await self.deliver(record)
                except Exception as exc:
                    record.attempt_count += 1
                    record.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    backoff = min(
                        self.settings.outbox_backoff_base_seconds
                        * (2 ** (record.attempt_count - 1)),
                        self.settings.outbox_backoff_max_seconds,
                    )
                    record.available_at = utcnow() + timedelta(seconds=backoff)
                    if record.attempt_count >= self.settings.outbox_max_attempts:
                        # Dead-letter: push far into the future so the partial
                        # index scan stops revisiting it; last_error and
                        # attempt_count stay behind for diagnostics/re-drive.
                        record.available_at = utcnow() + timedelta(days=3650)
                        logger.error(
                            "outbox record exhausted retries",
                            extra={
                                "outbox_id": str(record.id),
                                "topic": record.topic,
                                "last_error": record.last_error,
                            },
                        )
                else:
                    record.delivered_at = utcnow()
                    record.last_error = None
                    delivered += 1
        return delivered

    # -- approval outcomes (CP-ADR-0061) -------------------------------------

    async def process_outcomes(self) -> int:
        """Run every live outcome that is due, each in its own transaction.

        Its own transaction, so one outcome's unexpected error (or a broken
        connection) rolls back only that outcome's attempt; the attempt is then
        counted in a fresh transaction with the outbox backoff settings, and
        after ``outbox_max_attempts`` the outcome fails with a work item on it.
        """
        async with transaction(self.session_factory) as session:
            due = await due_outcomes(session, limit=self.settings.outbox_batch_size)
        processed = 0
        for tenant_id, approval_id in due:
            try:
                async with transaction(self.session_factory) as session:
                    await execute_outcome(
                        session,
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        trace_run_id=self.trace_run_id,
                        skip_locked=True,
                        defer_seconds=self.settings.approval_outcome_defer_seconds,
                        skill_wait_seconds=self.settings.approval_outcome_skill_wait_seconds,
                    )
            except Exception as exc:
                logger.exception(
                    "approval outcome attempt failed",
                    extra={"approval_id": str(approval_id), "worker": self.name},
                )
                async with transaction(self.session_factory) as session:
                    await record_attempt_failure(
                        session,
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        error=f"{type(exc).__name__}: {exc}",
                        max_attempts=self.settings.outbox_max_attempts,
                        backoff_base_seconds=self.settings.outbox_backoff_base_seconds,
                        backoff_max_seconds=self.settings.outbox_backoff_max_seconds,
                        trace_run_id=self.trace_run_id,
                    )
            else:
                processed += 1
        return processed

    # -- work rules (CP-ADR-0063) ------------------------------------------------

    async def process_rule_events(self) -> int:
        """One journal batch per due tenant, each in its own transaction.

        An unexpected error rolls the tenant's batch back (its cursor does
        not move) and holds that tenant back with backoff; others go on.
        After ``rules_max_attempts`` failed batches the evaluation that still
        breaks is recorded failed and the batch passes it.
        """
        async with transaction(self.session_factory) as session:
            tenants = await due_rule_tenants(session, limit=self.settings.outbox_batch_size)
        read = 0
        for tenant_id in tenants:
            try:
                async with transaction(self.session_factory) as session:
                    read += await process_tenant_events(
                        session,
                        tenant_id=tenant_id,
                        batch_size=self.settings.rules_batch_size,
                        trace_run_id=self.trace_run_id,
                        skill_check_seconds=self.settings.rules_skill_check_seconds,
                        max_attempts=self.settings.rules_max_attempts,
                    )
            except Exception as exc:
                logger.exception(
                    "rule evaluation batch failed",
                    extra={"tenant": str(tenant_id), "worker": self.name},
                )
                async with transaction(self.session_factory) as session:
                    await record_tenant_failure(
                        session,
                        tenant_id=tenant_id,
                        error=f"{type(exc).__name__}: {exc}",
                        backoff_base_seconds=self.settings.outbox_backoff_base_seconds,
                        backoff_max_seconds=self.settings.outbox_backoff_max_seconds,
                    )
        return read

    async def process_rule_schedules(self) -> int:
        async with transaction(self.session_factory) as session:
            due = await due_schedules(session, limit=self.settings.outbox_batch_size)
        ran = 0
        for rule_id in due:
            try:
                async with transaction(self.session_factory) as session:
                    done = await run_schedule(
                        session,
                        rule_id=rule_id,
                        trace_run_id=self.trace_run_id,
                        skill_check_seconds=self.settings.rules_skill_check_seconds,
                    )
                ran += done is not None
            except Exception as exc:
                logger.exception(
                    "scheduled rule evaluation failed",
                    extra={"rule_id": str(rule_id), "worker": self.name},
                )
                # The slot is closed as failed and the rule waits for its next
                # one, instead of being retried on every tick.
                async with transaction(self.session_factory) as session:
                    await record_schedule_failure(
                        session,
                        rule_id=rule_id,
                        error=f"{type(exc).__name__}: {exc}",
                        trace_run_id=self.trace_run_id,
                    )
        return ran

    async def resume_rule_evaluations(self) -> int:
        async with transaction(self.session_factory) as session:
            due = await due_evaluations(session, limit=self.settings.outbox_batch_size)
        resumed = 0
        for evaluation_id in due:
            try:
                async with transaction(self.session_factory) as session:
                    done = await resume_evaluation(
                        session,
                        evaluation_id=evaluation_id,
                        trace_run_id=self.trace_run_id,
                        skill_wait_seconds=self.settings.rules_skill_wait_seconds,
                        skill_check_seconds=self.settings.rules_skill_check_seconds,
                        claim_wait_seconds=self.settings.rules_claim_wait_seconds,
                    )
                resumed += done is not None and done.status != "waiting"
            except Exception:
                logger.exception(
                    "rule evaluation resume failed",
                    extra={"evaluation_id": str(evaluation_id), "worker": self.name},
                )
                async with transaction(self.session_factory) as session:
                    await postpone_evaluation(
                        session,
                        evaluation_id=evaluation_id,
                        seconds=self.settings.outbox_backoff_max_seconds,
                    )
        return resumed

    # -- process instances (CP-ADR-0074) ------------------------------------------

    async def process_events(self) -> int:
        """One journal batch per due tenant into its process instances.

        An unexpected error rolls the tenant's batch back (its cursor does not
        move) and holds the tenant back with backoff; the others go on. A key
        an apply holds (``catalog_key_busy``) rolls the batch back too, but the
        tenant is only deferred: that is expected, not a failure.
        """
        async with transaction(self.session_factory) as session:
            tenants = await process_instances.due_process_tenants(
                session, limit=self.settings.outbox_batch_size
            )
        read = 0
        for tenant_id in tenants:
            try:
                async with transaction(self.session_factory) as session:
                    read += await process_instances.process_tenant_events(
                        session,
                        tenant_id=tenant_id,
                        batch_size=self.settings.rules_batch_size,
                        trace_run_id=self.trace_run_id,
                    )
            except Exception as exc:
                if is_key_busy(exc):
                    # An apply holds the key of a child a step starts: the
                    # batch is rolled back and read again soon, not failed.
                    logger.info(
                        "process event batch deferred: catalog key busy",
                        extra={"tenant": str(tenant_id), "worker": self.name},
                    )
                    async with transaction(self.session_factory) as session:
                        await process_instances.defer_tenant(
                            session,
                            tenant_id=tenant_id,
                            seconds=self.settings.outbox_backoff_base_seconds,
                        )
                    continue
                logger.exception(
                    "process event batch failed",
                    extra={"tenant": str(tenant_id), "worker": self.name},
                )
                async with transaction(self.session_factory) as session:
                    await process_instances.record_tenant_failure(
                        session,
                        tenant_id=tenant_id,
                        error=f"{type(exc).__name__}: {exc}",
                        backoff_base_seconds=self.settings.outbox_backoff_base_seconds,
                        backoff_max_seconds=self.settings.outbox_backoff_max_seconds,
                    )
        return read

    async def process_timers(self) -> int:
        """Every due timer is its instance's input, each in its own transaction.

        A timer whose instance is busy (locked by another step) waits for the
        next cycle; one that keeps breaking is logged and tried again.
        """
        async with transaction(self.session_factory) as session:
            due = await process_instances.due_timers(session, limit=self.settings.outbox_batch_size)
        fired = 0
        for timer_id in due:
            try:
                async with transaction(self.session_factory) as session:
                    fired += await process_instances.fire_timer(
                        session, timer_id, trace_run_id=self.trace_run_id
                    )
            except Exception as exc:
                if is_key_busy(exc):
                    logger.info(
                        "process timer deferred: catalog key busy",
                        extra={"timer_id": str(timer_id), "worker": self.name},
                    )
                    continue
                logger.exception(
                    "process timer failed",
                    extra={"timer_id": str(timer_id), "worker": self.name},
                )
        return fired

    async def process_recalls(self) -> int:
        """Every due recall is asked of memory, then answered into its instance.

        Each attempt resolves in one transaction, asks memory outside any, and
        answers in another; one that breaks is logged and tried again later.
        """
        async with transaction(self.session_factory) as session:
            due = await process_instances.due_recalls(
                session, limit=self.settings.outbox_batch_size
            )
        answered = 0
        for recall_id in due:
            try:
                answered += await process_instances.run_recall(
                    self.session_factory,
                    self.graph_provider,
                    self.settings,
                    recall_id,
                    trace_run_id=self.trace_run_id,
                )
            except Exception as exc:
                if is_key_busy(exc):
                    # The recall keeps its lease and is answered after it.
                    logger.info(
                        "process recall deferred: catalog key busy",
                        extra={"recall_id": str(recall_id), "worker": self.name},
                    )
                    continue
                logger.exception(
                    "process recall failed",
                    extra={"recall_id": str(recall_id), "worker": self.name},
                )
        return answered

    # -- verification stage (CP-ADR-0067) --------------------------------------

    async def process_verifications(self) -> int:
        """Advance every open attempt that is due, each in its own transaction.

        An unexpected error rolls back only that attempt's pass and postpones
        it by the outbox backoff ceiling, the way a broken rule evaluation is;
        the checks it runs are idempotent (a skill call per attempt and check).
        """
        async with transaction(self.session_factory) as session:
            due = await due_verifications(session, limit=self.settings.outbox_batch_size)
        timing = Timing(
            check=timedelta(seconds=self.settings.verification_check_seconds),
            skill_timeout=timedelta(seconds=self.settings.verification_skill_timeout_seconds),
            external_timeout=timedelta(seconds=self.settings.verification_external_timeout_seconds),
        )
        processed = 0
        for verification_id in due:
            try:
                async with transaction(self.session_factory) as session:
                    done = await execute_verification(
                        session,
                        verification_id=verification_id,
                        trace_run_id=self.trace_run_id,
                        timing=timing,
                    )
                processed += done is not None
            except Exception:
                logger.exception(
                    "verification pass failed",
                    extra={"verification_id": str(verification_id), "worker": self.name},
                )
                async with transaction(self.session_factory) as session:
                    await postpone_verification(
                        session,
                        verification_id=verification_id,
                        seconds=self.settings.outbox_backoff_max_seconds,
                    )
        return processed

    # -- lease sweeps ---------------------------------------------------------

    async def expire_sessions(self) -> int:
        now = utcnow()
        expired = 0
        async with transaction(self.session_factory) as session:
            rows = (
                await session.scalars(
                    select(Session)
                    .where(
                        Session.status == SessionStatus.ACTIVE,
                        Session.expires_at <= now,
                    )
                    .limit(_SWEEP_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for work_session in rows:
                work_session.status = SessionStatus.STALE
                work_session.ended_at = utcnow()
                # A dead session cannot hold work: free its claims so tasks
                # do not stay blocked for the rest of the claim TTL.
                released = await release_active_claims_for_session(
                    session,
                    tenant_id=work_session.tenant_id,
                    session_id=work_session.id,
                    actor_id=None,
                    request_id="worker",
                    correlation_id=f"worker-{self.name}",
                    trace_run_id=self.trace_run_id,
                    reason="session_expired",
                )
                await record_event(
                    session,
                    tenant_id=work_session.tenant_id,
                    event_type="session.expired",
                    entity_type="session",
                    entity_id=work_session.id,
                    actor_id=None,
                    session_id=work_session.id,
                    request_id="worker",
                    correlation_id=f"worker-{self.name}",
                    trace_run_id=self.trace_run_id,
                    payload={
                        "expiresAt": work_session.expires_at.isoformat(),
                        "releasedClaims": [str(c) for c in released],
                    },
                )
                expired += 1
        return expired

    async def expire_claims(self) -> int:
        now = utcnow()
        expired = 0
        async with transaction(self.session_factory) as session:
            candidates = (
                await session.execute(
                    select(TaskClaim.id, TaskClaim.task_id)
                    .where(
                        TaskClaim.status == ClaimStatus.ACTIVE,
                        TaskClaim.expires_at <= now,
                    )
                    .order_by(TaskClaim.task_id)
                    .limit(_SWEEP_BATCH)
                )
            ).all()
            for claim_id, task_id in candidates:
                # Lock ordering: task first, then claim; re-check afterwards.
                task = await session.scalar(
                    select(Task).where(Task.id == task_id).with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                claim = await session.scalar(
                    select(TaskClaim)
                    .where(TaskClaim.id == claim_id)
                    .with_for_update(skip_locked=True)
                )
                if (
                    claim is None
                    or claim.status != ClaimStatus.ACTIVE
                    or claim.expires_at > utcnow()
                ):
                    continue
                release_claim_on_locked_task(
                    task,
                    claim,
                    reason="expired",
                    new_status=ClaimStatus.STALE,
                    lifecycle=await lifecycle_of(session, task),
                )
                task.version += 1
                await record_event(
                    session,
                    tenant_id=claim.tenant_id,
                    event_type="claim.expired",
                    entity_type="claim",
                    entity_id=claim.id,
                    actor_id=None,
                    session_id=claim.session_id,
                    request_id="worker",
                    correlation_id=f"worker-{self.name}",
                    trace_run_id=self.trace_run_id,
                    payload={
                        "taskId": str(task.id),
                        "taskStatus": task.status,
                        "taskSystemStatusCategory": task.system_status_category,
                    },
                )
                expired += 1
        return expired

    async def expire_skill_invocation_leases(self) -> int:
        """Return dead-lease skill invocations to the queue (ADR-0056 §2).

        The executor's claim does the same lazily for its own tenant; this
        sweep only makes the state visible sooner.
        """
        async with transaction(self.session_factory) as session:
            return await expire_skill_leases(
                session,
                tenant_id=None,
                actor_id=None,
                request_id="worker",
                correlation_id=f"worker-{self.name}",
                trace_run_id=self.trace_run_id,
            )

    async def cleanup_idempotency(self) -> int:
        async with transaction(self.session_factory) as session:
            result = await session.execute(
                delete(IdempotencyKey)
                .where(IdempotencyKey.expires_at <= utcnow())
                .returning(IdempotencyKey.key)
            )
            return len(result.all())

    async def cleanup_oauth_states(self) -> int:
        async with transaction(self.session_factory) as session:
            result = await session.execute(
                delete(ConnectionOAuthState)
                .where(ConnectionOAuthState.created_at <= utcnow() - _OAUTH_STATE_RETENTION)
                .returning(ConnectionOAuthState.id)
            )
            return len(result.all())

    # -- agents' access in the secret store (CP-ADR-0079 §9) -------------------------

    async def expire_connection_keys(self) -> int:
        """Keys past ``expiresAt`` become ``expired``; the event brings the policies along."""
        async with transaction(self.session_factory) as session:
            return await connection_policies.expire_keys(
                session, now=utcnow(), trace_run_id=self.trace_run_id
            )

    async def process_connection_policy_events(self) -> int:
        """One journal batch per due tenant; a failing store holds that tenant back."""
        store = self.secret_store
        if store is None:
            return 0
        async with transaction(self.session_factory) as session:
            tenants = await connection_policies.due_policy_tenants(
                session, limit=self.settings.outbox_batch_size
            )
        read = 0
        for tenant_id in tenants:
            try:
                async with transaction(self.session_factory) as session:
                    read += await connection_policies.process_tenant_events(
                        session,
                        store,
                        tenant_id=tenant_id,
                        batch_size=self.settings.rules_batch_size,
                    )
            except Exception as exc:
                reason = exc.reason if isinstance(exc, SecretStoreError) else type(exc).__name__
                logger.warning(
                    "connection policy sync failed",
                    extra={"tenant": str(tenant_id), "worker": self.name, "error_code": reason},
                    exc_info=not isinstance(exc, SecretStoreError),
                )
                async with transaction(self.session_factory) as session:
                    await connection_policies.record_tenant_failure(
                        session,
                        tenant_id=tenant_id,
                        reason=reason,
                        backoff_base_seconds=self.settings.outbox_backoff_base_seconds,
                        backoff_max_seconds=self.settings.outbox_backoff_max_seconds,
                    )
        return read

    async def sync_connection_policies(self, *, force: bool = False) -> int:
        """The full pass, when it is due (or ``force``): every tenant, then the orphans.

        Each tenant syncs in its own transaction, so one tenant the store fails
        for does not hold the others; the orphans go last, and only when every
        tenant went through. Answers the number of changes written.
        """
        store = self.secret_store
        if store is None:
            return 0
        if not force and time.monotonic() < self._next_policy_pass:
            return 0
        self._next_policy_pass = time.monotonic() + self.settings.connections_sync_seconds
        async with transaction(self.session_factory) as session:
            tenants = await connection_policies.policy_tenants(session)
        stats = connection_policies.SyncStats()
        failed = False
        for tenant_id in tenants:
            try:
                async with transaction(self.session_factory) as session:
                    await connection_policies.ensure_policy_cursor(session, tenant_id)
                    stats.add(
                        await connection_policies.sync_tenant(session, store, tenant_id, sweep=True)
                    )
            except SecretStoreError as exc:
                failed = True
                logger.warning(
                    "connection policy sync failed",
                    extra={"tenant": str(tenant_id), "worker": self.name, "error_code": exc.reason},
                )
        if not failed:
            try:
                async with transaction(self.session_factory) as session:
                    policies, roles = await connection_policies.orphan_principals(session, store)
                stats.add(await connection_policies.delete_orphans(store, policies, roles))
            except SecretStoreError as exc:
                logger.warning(
                    "connection policy orphans not swept",
                    extra={"worker": self.name, "error_code": exc.reason},
                )
        return stats.changes

    async def sweep_artifact_uploads(self) -> int:
        """Uploads without an artifact past their TTL, and orphaned objects."""
        if self.content_store is None:
            return 0
        return await sweep_expired_uploads(
            self.session_factory, self.content_store, batch=_SWEEP_BATCH
        )
