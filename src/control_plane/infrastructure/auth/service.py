"""Authentication: resolve a Bearer credential into an AuthContext.

During the compatibility window the service accepts two kinds of credential: the
legacy ``cp_<prefix>_<secret>`` key and an IAM access token. Which one it is is
decided by the shape of the presented value rather than by trying each in turn:
trying them in order would report through the response code which one matched.
"""

from platform_auth import EnforcementError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext
from control_plane.application.common import utcnow
from control_plane.application.visibility import with_visibility
from control_plane.config import Settings
from control_plane.domain.enums import PrincipalStatus
from control_plane.domain.errors import AuthenticationError, AuthorizationError
from control_plane.infrastructure.auth.api_keys import (
    extract_prefix,
    is_break_glass_prefix,
    verify_api_key,
)
from control_plane.infrastructure.auth.iam import (
    IamEnforcement,
    authenticate_with_iam,
    looks_like_iam_token,
    to_domain_error,
)
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import ApiKey, Principal


def parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


async def resolve_auth_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    enforcement: IamEnforcement | None,
    authorization: str | None,
    action: str,
    path: str,
    request_id: str,
    correlation_id: str = "",
    trace_run_id: str = "",
) -> AuthContext:
    """The single authentication entry point for HTTP, WebSocket and workers.

    One entry point is not about brevity: diverging authentication paths are
    exactly how realtime or a background job quietly ends up without
    enforcement.
    """
    token = parse_bearer(authorization)
    if token is None:
        raise AuthenticationError()

    if enforcement is not None and looks_like_iam_token(token):
        try:
            ctx = await authenticate_with_iam(
                enforcement,
                token,
                action=action,
                path=path,
                request_id=request_id,
                correlation_id=correlation_id,
                trace_run_id=trace_run_id,
            )
        except EnforcementError as exc:
            raise to_domain_error(exc) from exc
        # Visibility is read per request, not from the binding cache: the
        # cache is keyed by identity, the mode by principal (CP-ADR-0082 §3.3).
        async with transaction(session_factory) as session:
            return await with_visibility(session, ctx)

    break_glass = settings.break_glass_enabled and is_break_glass_prefix(extract_prefix(token))
    if not settings.legacy_api_keys_enabled and not break_glass:
        # The compatibility window is closed: the legacy key is no longer a
        # credential here. A break-glass key (ADR-0065) still is — it exists for
        # exactly the moment when IAM cannot be reached.
        raise AuthenticationError()

    async with transaction(session_factory) as session:
        ctx = await authenticate(
            session,
            settings,
            authorization=authorization,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_run_id=trace_run_id,
        )
        return await with_visibility(session, ctx)


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    *,
    authorization: str | None,
    request_id: str,
    correlation_id: str = "",
    trace_run_id: str = "",
) -> AuthContext:
    token = parse_bearer(authorization)
    if token is None:
        raise AuthenticationError()

    prefix = extract_prefix(token)
    if prefix is None:
        raise AuthenticationError()

    now = utcnow()
    api_key = await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    if (
        api_key is None
        or not verify_api_key(token, api_key.key_hash)
        or api_key.revoked_at is not None
        or (api_key.expires_at is not None and api_key.expires_at <= now)
    ):
        raise AuthenticationError()
    if is_break_glass_prefix(prefix) and (
        not settings.break_glass_enabled
        or api_key.expires_at is None
        or (api_key.expires_at - api_key.created_at).total_seconds()
        > settings.break_glass_max_ttl_seconds
    ):
        # A break-glass key is short-lived by construction; one without a bound,
        # or with a longer one than this installation allows, is not honoured.
        raise AuthenticationError()

    principal = await session.get(Principal, api_key.principal_id)
    if principal is None:
        raise AuthenticationError()
    if principal.status != PrincipalStatus.ACTIVE:
        raise AuthorizationError(
            "Principal is not active",
            code="principal_not_active",
            details={"principalId": str(principal.id), "status": principal.status},
        )

    # Throttled last_used_at refresh: at most one write per refresh interval.
    threshold_age = api_key.last_used_at is None or (
        (now - api_key.last_used_at).total_seconds() >= settings.api_key_last_used_refresh_seconds
    )
    if threshold_age:
        await session.execute(
            update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
        )

    return AuthContext(
        tenant_id=api_key.tenant_id,
        principal_id=principal.id,
        principal_kind=principal.kind,
        api_key_id=api_key.id,
        permissions=frozenset(api_key.permissions),
        request_id=request_id,
        correlation_id=correlation_id or request_id,
        trace_run_id=trace_run_id,
    )
