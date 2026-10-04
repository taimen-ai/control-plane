"""Application configuration loaded from environment variables (prefix ``CP_``)."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_https_url(value: str) -> bool:
    """An absolute ``https`` address with a host."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and value == value.strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = (
        "postgresql+psycopg://control_plane:control_plane@localhost:5433/control_plane"
    )
    # Token that guards POST /api/v1/bootstrap. Unset => bootstrap endpoint is disabled.
    bootstrap_token: str | None = None

    log_level: str = "INFO"

    # Lease defaults / bounds (seconds).
    session_ttl_seconds: int = 300
    session_ttl_min_seconds: int = 10
    session_ttl_max_seconds: int = 3600
    claim_ttl_seconds: int = 300
    claim_ttl_min_seconds: int = 10
    claim_ttl_max_seconds: int = 3600

    idempotency_ttl_seconds: int = 86_400
    # How long a concurrent duplicate request waits for the first one to finish.
    idempotency_wait_timeout_seconds: float = 10.0
    # Lifetime of a *pending* idempotency record (no stored response yet). Keeps
    # a key from being wedged for the full TTL if the executor process crashes.
    idempotency_pending_ttl_seconds: int = 60

    max_body_bytes: int = 1_048_576
    # Knowledge snapshots are whole-source documents (CP-ADR-0060): their path
    # alone gets a larger body ceiling than the rest of the API.
    knowledge_snapshot_max_body_bytes: int = 8 * 1_048_576
    # Platform administrators allowed to register knowledge packs in Memory's
    # registry, which all tenants share (CP-ADR-0060): Control Plane or IAM
    # principal ids. Empty => POST /api/v1/knowledge/packs answers 403.
    knowledge_pack_admins: list[str] = []
    cors_origins: list[str] = []

    # Realtime: WS falls back to polling at this interval if NOTIFY is lost.
    ws_poll_interval_seconds: float = 5.0

    # Export of the journal (CP-ADR-0068, export amendment): the longest period and
    # the most events one GET /events:export hands out; above either the
    # export is refused before the body starts.
    events_export_max_period_days: int = 92
    events_export_max_events: int = 100_000

    worker_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 8
    outbox_lock_timeout_seconds: int = 60
    outbox_backoff_base_seconds: float = 2.0
    outbox_backoff_max_seconds: float = 300.0
    # CP-ADR-0061: how long an approval outcome that met a live claim on its
    # target waits before it looks again.
    approval_outcome_defer_seconds: float = 15.0
    # CP-ADR-0061 §10: how long a skill call queued by an outcome may stay
    # unclaimed before the outcome cancels it and reacts as to a failure.
    approval_outcome_skill_wait_seconds: float = 86400.0
    # CP-ADR-0063 work rules: journal events one tenant's batch evaluates, how
    # often an evaluation waiting on its interpretation skill looks again, and
    # how long such a call may stay unclaimed before the rule gives up on it.
    # After rules_max_attempts failed batches of a tenant, an evaluation that
    # still breaks is recorded failed (rule_internal_error) and passed.
    rules_batch_size: int = 200
    rules_max_attempts: int = 3
    rules_skill_check_seconds: float = 15.0
    rules_skill_wait_seconds: float = 86400.0
    # Amendment 2026-09-25 (A4): how long a cancel_work / complete_work
    # decision on work under a live claim waits for the claim to end before
    # it fails with claim_not_released.
    rules_claim_wait_seconds: float = 86400.0
    # CP-ADR-0067 verification stage: how often an open attempt looks at the
    # check it waits on, how long a check's skill call may go without a result
    # and how long evidence of an external state may take to arrive before the
    # check fails with no_result.
    verification_check_seconds: float = 15.0
    verification_skill_timeout_seconds: float = 900.0
    verification_external_timeout_seconds: float = 86400.0

    api_key_last_used_refresh_seconds: int = 60

    # --- Context Memory provider (optional; "none" keeps the Control Plane
    # fully standalone: no readiness impact, no runtime dependency).
    context_provider: str = "none"  # none | http
    context_base_url: str = "http://localhost:8077"
    context_api_key: str | None = None
    # How the provider authenticates to the Memory Service (superproject ADR-0030,
    # memory-service ADR-018): ``api_key`` — the static bearer above; ``iam`` — the
    # Control Plane's own service account (CP_IAM_CLIENT_ID/SECRET) exchanged at IAM
    # for an access token of the memory audience; ``auto`` — ``iam`` when the service
    # account is configured, ``api_key`` otherwise.
    context_auth: str = "auto"  # auto | api_key | iam
    context_iam_audience: str = "memory-service"
    # ``memory:service`` is the core's identity on Memory's service routes (pack
    # registry, namespace kinds, reconcile; MEM-ADR-020): one token per audience.
    context_iam_scopes: list[str] = [
        "memory:read",
        "memory:write",
        "memory:tenants",
        "memory:service",
    ]
    # tenant UUID -> Memory namespace: f"{prefix}{tenant_id}"
    context_namespace_prefix: str = "tenant:"
    # Interactive /context calls sit on the harness latency path: keep tight.
    context_timeout_seconds: float = 3.0
    context_ingest_timeout_seconds: float = 15.0
    # Snapshot reconcile / pack administration (CP-ADR-0060): a synchronous call on
    # the connector's path, bounded but larger than a batch ingest.
    context_reconcile_timeout_seconds: float = 60.0
    context_batch_size: int = 100
    # v0.5 per-tenant delivery isolation: conservative fairness/rate limits so
    # one busy tenant cannot monopolise a cycle (ADR-0036).
    context_tenant_batch_size: int = 100
    context_max_tenants_per_cycle: int = 20
    context_poll_interval_seconds: float = 1.0
    context_retry_backoff_base_seconds: float = 1.0
    context_retry_backoff_max_seconds: float = 60.0
    # v0.5 journal retention (ADR-0038): the minimum age of an event before an
    # operator may archive or prune it, on top of the consumer-cursor floor.
    journal_retention_min_age_seconds: int = 30 * 24 * 3600
    # Token budget: server-enforced ceiling and default for ContextPack builds.
    context_max_tokens_limit: int = 16_000
    context_default_max_tokens: int = 8_000

    # --- IAM enforcement (IAM-7). Off by default: turning it on is a deliberate
    # configuration decision, not a side effect of a deployment.
    iam_enabled: bool = False
    iam_issuer: str = ""
    iam_jwks_url: str = ""
    iam_audience: str = "control-plane"
    iam_leeway_seconds: float = 5.0
    iam_jwks_refresh_after_seconds: float = 300.0
    iam_jwks_stale_after_seconds: float = 3600.0
    iam_jwks_min_refresh_interval_seconds: float = 10.0
    iam_request_timeout_seconds: float = 3.0
    # Local revocation policy: how long the binding projection is reused, and
    # past what age entry is closed when it cannot be re-read.
    iam_binding_cache_ttl_seconds: float = 30.0
    iam_binding_stale_after_seconds: float = 120.0
    # Compatibility window: until cutover the legacy `cp_...` key keeps working.
    # Turning this off moves the Control Plane to IAM-only.
    legacy_api_keys_enabled: bool = True
    # Break-glass (ADR-0065): a short-lived admin key minted from a shell on the
    # host (`python -m control_plane.break_glass`) is accepted even when the
    # window above is closed — it is the way in while IAM is down.
    break_glass_enabled: bool = True
    break_glass_max_ttl_seconds: int = 4 * 3600

    # --- Entitlement (ADR-0013). Enforcement order: IAM identity ->
    # entitlement -> domain policy. Entitlement being off is visible in the
    # audit as decision source `disabled`, not as a missing record.
    entitlement_enabled: bool = False
    entitlement_base_url: str = "http://localhost:8020"
    entitlement_product: str = "control-plane"
    entitlement_audience: str = "entitlement-service"
    entitlement_default_feature: str = "api"
    entitlement_cache_ttl_seconds: float = 30.0
    entitlement_degraded_max_age_seconds: float = 300.0
    entitlement_timeout_seconds: float = 3.0

    # The Control Plane's own service identity in IAM: it asks entitlement about
    # a user's licence with it. The secret arrives through the environment.
    iam_base_url: str = "http://localhost:8010"
    iam_client_id: str = ""
    iam_client_secret: str | None = None
    iam_client_scopes: list[str] = ["entitlement:check-on-behalf"]

    # Policy Decision Point (TAI-ADR-0025, CP-ADR-0055). local — только require();
    # shadow — require решает, policy-service спрашивается параллельно, расхождения
    # считаются и пишутся в журнал; policy — решает PDP (legacy-ключи остаются на require).
    authz_mode: str = "local"  # local | shadow | policy
    policy_base_url: str = "http://localhost:8030"
    policy_audience: str = "policy-service"
    policy_scopes: list[str] = ["policy:check", "policy:check-on-behalf"]
    policy_timeout_seconds: float = 3.0
    policy_cache_ttl_seconds: float = 5.0

    # --- Artifact content store (CP-ADR-0072 §3). Any S3-compatible service;
    # without an endpoint the store is off and content routes answer 503
    # content_store_unavailable, while artifact records keep working.
    s3_endpoint_url: str | None = None
    s3_bucket: str = "artifacts"
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_connect_timeout_seconds: float = 5.0
    s3_read_timeout_seconds: float = 60.0
    # Ceiling of one uploaded file; PUT /artifact-contents gets it as its own
    # body limit instead of max_body_bytes. An artifact type may only narrow
    # it (maxBytes).
    artifact_max_bytes: int = 104_857_600
    # How long an upload may wait for an artifact to reference it (§2, §10).
    artifact_upload_ttl_seconds: int = 86_400

    # --- Secret store of connections (CP-ADR-0079 §1, §15). OpenBao; the core
    # logs in with its IAM service token (audience below) at ``auth/jwt/login``
    # under its own role. Empty URL: routes that need the store answer 503
    # secret_store_unavailable and change nothing.
    secret_store_url: str = ""
    secret_store_audience: str = "openbao"
    secret_store_role: str = "control-plane"
    secret_store_timeout_seconds: float = 10.0
    # The public https address of GET /api/v1/connections:callback, registered
    # at the provider letter for letter; empty: :authorize is 409.
    oauth_redirect_uri: str = ""
    # The console page the callback returns the browser to; empty: :authorize
    # is 409, the callback of a live state answers 200 text/plain.
    connections_return_url: str = ""
    oauth_state_ttl_seconds: int = 600
    # The full pass of the worker connections-policy-sync (§9): agents'
    # policies and roles in the store, expired keys, orphaned policies.
    connections_sync_seconds: float = 300.0

    @field_validator("oauth_redirect_uri", "connections_return_url")
    @classmethod
    def _https_or_empty(cls, value: str) -> str:
        # The redirect carries the provider's code, the return page its
        # outcome: either goes over https or OAuth stays off (CP-ADR-0079 §6).
        if value and not is_https_url(value):
            raise ValueError("an https:// address, or empty")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
