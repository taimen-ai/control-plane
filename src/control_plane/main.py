"""FastAPI application factory and process entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from control_plane import __version__, observability
from control_plane.api.errors import register_exception_handlers
from control_plane.api.middleware import (
    BodySizeLimitMiddleware,
    MetricsMiddleware,
    RequestIdMiddleware,
)
from control_plane.api.nul_guard import NulCharacterGuardMiddleware
from control_plane.api.v1.router import api_v1_router
from control_plane.application.authorization import configure_authorizer
from control_plane.config import Settings, get_settings
from control_plane.infrastructure.auth.iam import build_iam_enforcement
from control_plane.infrastructure.auth.policy import build_authorizer
from control_plane.infrastructure.content_store import (
    ContentStore,
    ContentStoreUnavailable,
    build_content_store,
)
from control_plane.infrastructure.context_provider import build_context_provider
from control_plane.infrastructure.db.engine import build_engine, build_session_factory
from control_plane.infrastructure.db.migrations import get_head_revision
from control_plane.infrastructure.realtime.hub import RealtimeHub
from control_plane.infrastructure.secret_store import build_secret_store
from control_plane.logging import configure_logging

logger = logging.getLogger(__name__)


async def _prepare_content_store(store: ContentStore | None) -> None:
    """Create the bucket at start-up (CP-ADR-0072 §3). An unreachable store
    does not stop the API: content routes answer 503 and the store tries
    again on first use."""
    if store is None:
        return
    try:
        await store.ensure_bucket()
    except ContentStoreUnavailable as exc:
        logger.warning("content store unavailable at start-up", extra={"error": str(exc)})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    metrics: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings)
        hub = RealtimeHub(settings.database_url)
        provider = build_context_provider(settings)
        session_factory = build_session_factory(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.metrics = metrics
        app.state.realtime_hub = hub
        # Optional external memory: never a readiness dependency.
        app.state.context_provider = provider
        # IAM enforcement (IAM-7): off by default; an incomplete configuration
        # fails startup instead of silently keeping the previous mode.
        enforcement = build_iam_enforcement(settings, session_factory)
        app.state.iam = enforcement
        # Domain authorization source (CP-ADR-0055): local / shadow / policy.
        authorizer, authz_closables = build_authorizer(settings)
        configure_authorizer(authorizer)
        app.state.authorizer = authorizer
        # Artifact bytes (CP-ADR-0072): None without CP_S3_ENDPOINT_URL.
        content_store = build_content_store(settings)
        app.state.content_store = content_store
        await _prepare_content_store(content_store)
        # Secret store of connections (CP-ADR-0079 §1): None without
        # CP_SECRET_STORE_URL; the routes that need it answer 503.
        secret_store = build_secret_store(settings)
        app.state.secret_store = secret_store
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()
            if content_store is not None:
                await content_store.aclose()
            if secret_store is not None:
                await secret_store.aclose()
            if enforcement is not None:
                await enforcement.aclose()
            for closable in authz_closables:
                aclose = getattr(closable, "aclose", None)
                if aclose is not None:
                    await aclose()
            if provider is not None:
                await provider.aclose()
            await engine.dispose()

    app = FastAPI(
        title="control-plane",
        version=__version__,
        description=(
            "Coordination backend for humans, AI agents and automated processes: "
            "tasks, sessions, atomic claims with fencing tokens, immutable domain "
            "events, transactional outbox and realtime updates."
        ),
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # add_middleware() prepends, so the last one added runs outermost.
    # Innermost: buffers a JSON body only after the size limit let it through.
    # The artifact upload streams any media type into the content store, not
    # into PostgreSQL (CP-ADR-0083).
    app.add_middleware(NulCharacterGuardMiddleware, exempt_paths=("/api/v1/artifact-contents",))
    app.add_middleware(MetricsMiddleware, counters=metrics)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        path_limits={
            "/api/v1/knowledge/snapshots": settings.knowledge_snapshot_max_body_bytes,
            # The preview takes the snapshot's body; a document -- its chunks.
            "/api/v1/knowledge/snapshots:preview": settings.knowledge_snapshot_max_body_bytes,
            "/api/v1/knowledge/documents": settings.knowledge_snapshot_max_body_bytes,
            # Uploaded files stream to disk, never into memory (CP-ADR-0072 §2).
            "/api/v1/artifact-contents": settings.artifact_max_bytes,
        },
    )
    app.add_middleware(RequestIdMiddleware)
    if settings.cors_origins:  # CORS stays off unless explicitly configured
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_v1_router)

    @app.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(request: Request) -> JSONResponse:
        try:
            async with request.app.state.engine.connect() as conn:
                db_revision = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "reason": "database_unreachable"},
            )
        head = get_head_revision()
        if head is not None and db_revision != head:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "reason": "migrations_pending",
                    "dbRevision": db_revision,
                    "headRevision": head,
                },
            )
        return JSONResponse({"status": "ready", "revision": db_revision})

    @app.get("/metrics", tags=["health"], response_class=PlainTextResponse)
    async def metrics_endpoint(request: Request) -> PlainTextResponse:
        lines = [
            "# HELP http_requests_total Total HTTP requests.",
            "# TYPE http_requests_total counter",
        ]
        for (method, status), count in sorted(metrics.get("http_requests_total", {}).items()):
            lines.append(f'http_requests_total{{method="{method}",status="{status}"}} {count}')

        for name, value in sorted(observability.counters().items()):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        # Low-cardinality DB gauges (cheap COUNTs; no tenant/task labels).
        try:
            async with request.app.state.engine.connect() as conn:
                gauges = (
                    await conn.execute(
                        text(
                            "SELECT"
                            " (SELECT count(*) FROM sessions WHERE status = 'active'"
                            "   AND expires_at > now()) AS active_harness_sessions,"
                            " (SELECT count(*) FROM task_claims WHERE status = 'active'"
                            "   AND expires_at > now()) AS active_claims,"
                            " (SELECT count(*) FROM runs WHERE status = 'running')"
                            "   AS active_runs"
                        )
                    )
                ).one()
                for name, value in zip(
                    ("active_harness_sessions", "active_claims", "active_runs"),
                    gauges,
                    strict=True,
                ):
                    lines.append(f"# TYPE {name} gauge")
                    lines.append(f"{name} {value}")

                # v0.5: cursors are per tenant. Metrics stay AGGREGATE — a
                # per-tenant label would make cardinality grow with the
                # customer list (ADR-0036).
                adapter = (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS rows,"
                            " count(*) FILTER (WHERE parked_at IS NOT NULL) AS parked,"
                            " coalesce(sum((metadata->>'delivered_total')::bigint), 0)"
                            "   AS delivered,"
                            " coalesce(sum((metadata->>'duplicates_total')::bigint), 0)"
                            "   AS dupes,"
                            " coalesce(sum((metadata->>'failures_total')::bigint), 0)"
                            "   AS failures"
                            " FROM event_consumer_cursors WHERE name = 'context-adapter'"
                        )
                    )
                ).one()
                if adapter.rows:
                    for key, value in (
                        ("delivered_total", adapter.delivered),
                        ("duplicates_total", adapter.dupes),
                        ("failures_total", adapter.failures),
                    ):
                        lines.append(f"# TYPE context_adapter_{key} counter")
                        lines.append(f"context_adapter_{key} {value}")
                    lines.append("# TYPE context_adapter_parked_tenants gauge")
                    lines.append(f"context_adapter_parked_tenants {adapter.parked}")
                    lag = (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM (SELECT 1 FROM events e"
                                " JOIN event_consumer_cursors c"
                                "   ON c.tenant_id = e.tenant_id AND c.name = 'context-adapter'"
                                " WHERE (e.tx_id, e.sequence) > (c.tx_id, c.sequence)"
                                " LIMIT 1000) c"
                            )
                        )
                    ).scalar()
                    lines.append("# TYPE context_adapter_lag gauge")
                    lines.append(f"context_adapter_lag {lag}")
                    lines.append("# TYPE context_adapter_lag_capped gauge")
                    lines.append(f"context_adapter_lag_capped {1 if (lag or 0) >= 1000 else 0}")
        except Exception:  # pragma: no cover - metrics must not 500 on DB blips
            lines.append("# DB gauges unavailable")

        return PlainTextResponse("\n".join(lines) + "\n")

    return app


app = create_app()
