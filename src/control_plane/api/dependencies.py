"""FastAPI dependencies: settings, DB sessions, authentication."""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext
from control_plane.config import Settings
from control_plane.infrastructure.auth.iam import IamEnforcement
from control_plane.infrastructure.auth.service import resolve_auth_context
from control_plane.infrastructure.content_store import ContentStore
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.secret_store import SecretStore


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


async def get_db(
    session_factory: SessionFactoryDep,
) -> AsyncIterator[AsyncSession]:
    """A transactional session for read/query endpoints."""
    async with transaction(session_factory) as session:
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_content_store(request: Request) -> ContentStore | None:
    """``None`` when no store is configured (CP-ADR-0072 §3)."""
    return cast(ContentStore | None, getattr(request.app.state, "content_store", None))


ContentStoreDep = Annotated[ContentStore | None, Depends(get_content_store)]


def get_secret_store(request: Request) -> SecretStore | None:
    """``None`` when no store is configured (CP-ADR-0079 §1)."""
    return cast(SecretStore | None, getattr(request.app.state, "secret_store", None))


SecretStoreDep = Annotated[SecretStore | None, Depends(get_secret_store)]


def get_iam_enforcement(request: Request) -> IamEnforcement | None:
    return cast(IamEnforcement | None, getattr(request.app.state, "iam", None))


async def get_auth_context(
    request: Request,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> AuthContext:
    """Authenticate the Bearer credential outside the request transaction."""
    request_id = request.state.request_id
    correlation_id = request.headers.get("x-correlation-id", "") or request_id
    return await resolve_auth_context(
        session_factory=session_factory,
        settings=settings,
        enforcement=get_iam_enforcement(request),
        authorization=request.headers.get("authorization"),
        action=f"{request.method} {request.url.path}",
        path=request.url.path,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=getattr(request.state, "trace_run_id", ""),
    )


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]


async def authenticate_websocket(
    websocket: WebSocket,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> AuthContext:
    """Realtime goes through the same PEP as HTTP.

    A separate authentication path for WebSocket would mean an event
    subscription outliving the credential revocation that closes ordinary
    requests.
    """
    state = websocket.scope.get("state", {})
    request_id = state.get("request_id", "ws")
    return await resolve_auth_context(
        session_factory=session_factory,
        settings=settings,
        enforcement=cast(IamEnforcement | None, getattr(websocket.app.state, "iam", None)),
        authorization=websocket.headers.get("authorization"),
        action="WS /api/v1/events/ws",
        path=websocket.url.path,
        request_id=request_id,
        correlation_id=websocket.headers.get("x-correlation-id", "") or request_id,
        trace_run_id=state.get("trace_run_id", ""),
    )
