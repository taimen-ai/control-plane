"""Actor context and permission checks.

The actor is always derived from credentials, never from the request body.
Commands re-check permissions themselves (defense in depth): the API layer is
not the only enforcement point.

Two generations of the check live side by side (TAI-ADR-0025, CP-ADR-0055):

* ``require(ctx, *any_of)`` — the flat permission set carried by the credential
  (API key or ``iam_principal_bindings``). It knows neither resource nor scope.
* ``authorize(ctx, *any_of, resource=...)`` — the same question asked of the
  external Policy Decision Point with a resource reference. ``CP_AUTHZ_MODE``
  chooses who decides: ``local`` — ``require`` only; ``shadow`` — ``require``
  decides, the PDP is asked in parallel and divergences are logged; ``policy`` —
  the PDP decides, ``require`` remains only for credentials that have no IAM
  identity (legacy API keys).

The subject the PDP reasons about is the IAM principal id (``sub`` of the
access token), never the local ``principals.id`` (design v0 §16).

Visibility (CP-ADR-0082 §3) narrows both in every mode: a human whose binding
is in ``members`` mode sees only the workspaces of their membership and below.
``visible_objects(..., "workspace")`` returns that set (intersected with the
PDP's answer in ``policy`` mode), and ``authorize`` on a workspace outside it
answers ``404`` as for a workspace that does not exist.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from platform_auth import (
    AuthorizationUnavailable,
    ContextualTuple,
    ObjectPage,
    PermissionDenied,
    PolicyDecision,
    ResourceRef,
    TrustedAuthContext,
)

from control_plane import observability, sandbox
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    DependencyUnavailableError,
    NotFoundError,
)

logger = logging.getLogger("control_plane.authz")

AuthzMode = Literal["local", "shadow", "policy"]
AUTHZ_MODES: tuple[AuthzMode, ...] = ("local", "shadow", "policy")

# Visibility of an IAM binding (CP-ADR-0082 §2): the whole tenant, or the
# workspaces of the principal's membership and their descendants.
VISIBILITY_TENANT = "tenant"
VISIBILITY_MEMBERS = "members"
VISIBILITIES = (VISIBILITY_TENANT, VISIBILITY_MEMBERS)


@dataclass(frozen=True)
class AuthContext:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    principal_kind: str
    api_key_id: uuid.UUID
    permissions: frozenset[str]
    request_id: str = "unknown"
    correlation_id: str = ""
    causation_id: str | None = None
    # Distributed trace id from ``X-Run-Id`` (ADR-0039). Deliberately NOT the
    # execution Run entity: it never participates in an authorization or
    # ownership decision, it only correlates logs across services.
    trace_run_id: str = ""
    # IAM identity behind this context (``sub`` of the access token). ``None``
    # for a legacy API key: such a credential has no subject the PDP knows.
    iam_principal_id: uuid.UUID | None = None
    # Channel the credential entered through (``acr=channel:<name>`` of an IAM
    # token, e.g. ``telegram``); ``None`` for a direct API call (CP-ADR-0070).
    channel: str | None = None
    # The one resource a purpose-bound credential exists for
    # (``approval:<id>``): such a credential decides that approval and
    # nothing else, whatever its permissions say (CP-ADR-0070).
    purpose_ref: str | None = None
    # ``members`` when an active binding of the principal narrows a human to
    # their workspaces (CP-ADR-0082 §3.2); resolved per request at entry.
    visibility: str = VISIBILITY_TENANT
    # The workspaces visible in ``members`` mode; ``None`` in ``tenant`` mode.
    visible_workspaces: frozenset[str] | None = None
    # Roots of the trees those workspaces belong to: their memory namespaces
    # are the workspace namespaces such a caller may read (CP-ADR-0082 §3.4).
    visible_roots: frozenset[str] = frozenset()

    def has(self, permission: Permission) -> bool:
        return Permission.ADMIN.value in self.permissions or permission.value in self.permissions

    @property
    def policy_subject(self) -> str | None:
        return str(self.iam_principal_id) if self.iam_principal_id is not None else None

    def sees_workspace(self, workspace_id: uuid.UUID | str | None) -> bool:
        """Is work of this workspace visible to the caller (CP-ADR-0082 §3.7)?

        In ``members`` mode work outside any workspace is not: lists filter by
        the set and a task without a workspace is not in it.
        """
        if self.visible_workspaces is None:
            return True
        return workspace_id is not None and str(workspace_id) in self.visible_workspaces


class WorkspaceNotVisible(NotFoundError):
    """A workspace outside the caller's visibility: the answer of a missing one.

    A resolver of an object that lives in a workspace catches it and raises
    its own ``not_found`` instead, so the body does not tell that the object
    exists (CP-ADR-0082 §3.7).
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__("Workspace not found", details={"workspaceId": workspace_id})


def check_workspace_visible(ctx: AuthContext, workspace_id: uuid.UUID | str | None) -> None:
    """Raise ``WorkspaceNotVisible`` unless work of ``workspace_id`` is visible."""
    if not ctx.sees_workspace(workspace_id):
        raise WorkspaceNotVisible(str(workspace_id))


async def permits(
    ctx: AuthContext, *any_of: Permission, resource: ResourceRef | None = None
) -> bool:
    """Whether ``authorize`` lets the call through, as a predicate.

    A workspace outside the caller's visibility is "not allowed" too: a
    predicate catching only ``AuthorizationError`` would let its ``404``
    escape and tell that the workspace exists (CP-ADR-0082 §3.7).
    """
    try:
        await authorize(ctx, *any_of, resource=resource)
    except (AuthorizationError, WorkspaceNotVisible):
        return False
    return True


class _InWorkspace(Protocol):
    id: uuid.UUID
    workspace_id: uuid.UUID | None


async def permits_task(ctx: AuthContext, *any_of: Permission, task: _InWorkspace) -> bool:
    """``permits`` on a task, its workspace in the caller's sight.

    ``authorize`` with a task resource asks about permissions only: the
    workspace of the task is a row the authorizer does not read. A predicate
    over a task read from the database asks this instead (CP-ADR-0082 §3.7).
    """
    if not ctx.sees_workspace(task.workspace_id):
        return False
    return await permits(ctx, *any_of, resource=ResourceRef("task", str(task.id)))


@dataclass(frozen=True)
class SystemContext:
    """Actor context for background maintenance (worker); bypasses permissions."""

    request_id: str = "worker"
    correlation_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4()}")
    trace_run_id: str = field(default_factory=lambda: f"worker_{uuid.uuid4().hex}")


def require(ctx: AuthContext, *any_of: Permission) -> None:
    """Raise 403 unless the actor holds at least one of the given permissions."""
    if not any(ctx.has(p) for p in any_of):
        raise AuthorizationError(
            details={"required": [p.value for p in any_of]},
        )


# --- external PDP ------------------------------------------------------------


class PolicyClient(Protocol):
    """The slice of ``platform_auth.AuthorizationClient`` used here."""

    async def check(
        self,
        ctx: TrustedAuthContext,
        action: str,
        resource: ResourceRef,
        *,
        contextual: Sequence[ContextualTuple] = (),
        on_behalf_of: str | None = None,
        consistency: Literal["default", "strong"] = "default",
    ) -> PolicyDecision: ...

    async def list_objects(
        self,
        ctx: TrustedAuthContext,
        action: str,
        resource_type: str,
        *,
        on_behalf_of: str | None = None,
        consistency: Literal["default", "strong"] = "default",
        cursor: str | None = None,
        limit: int = 1000,
    ) -> ObjectPage: ...


def tenant_resource(ctx: AuthContext) -> ResourceRef:
    return ResourceRef("tenant", str(ctx.tenant_id))


def _policy_context(ctx: AuthContext) -> TrustedAuthContext:
    """A service-side context for the PDP call: the Control Plane speaks with
    its own service identity and names the end principal ``on_behalf_of``."""
    now = datetime.now(UTC)
    return TrustedAuthContext(
        tenant_id=ctx.tenant_id,
        principal_id=ctx.iam_principal_id or ctx.principal_id,
        principal_type=ctx.principal_kind,
        credential_id=str(ctx.api_key_id),
        audience="control-plane",
        issuer="",
        token_id="",
        scopes=frozenset(),
        expires_at=now + timedelta(minutes=5),
        issued_at=now,
        correlation_id=ctx.correlation_id or ctx.request_id,
    )


class Authorizer:
    """Domain authorization with a switchable decision source."""

    def __init__(self, client: PolicyClient | None, mode: AuthzMode = "local") -> None:
        if mode not in AUTHZ_MODES:
            raise ValueError(f"unknown CP_AUTHZ_MODE {mode!r}")
        if mode != "local" and client is None:
            raise ValueError(f"CP_AUTHZ_MODE={mode} requires a policy client")
        self._client = client
        self.mode: AuthzMode = mode

    async def authorize(
        self,
        ctx: AuthContext,
        *any_of: Permission,
        resource: ResourceRef | None = None,
        contextual: Sequence[ContextualTuple] = (),
        consistency: Literal["default", "strong"] = "default",
    ) -> None:
        if not any_of:
            raise ValueError("authorize() needs at least one action")
        await self._authorize(
            ctx, any_of, resource=resource, contextual=contextual, consistency=consistency
        )
        # Permissions first: a missing right stays a 403, visibility does not
        # mask it (CP-ADR-0082 §3.6).
        if resource is not None and resource.type == "workspace":
            check_workspace_visible(ctx, resource.id)

    async def _authorize(
        self,
        ctx: AuthContext,
        any_of: tuple[Permission, ...],
        *,
        resource: ResourceRef | None,
        contextual: Sequence[ContextualTuple],
        consistency: Literal["default", "strong"],
    ) -> None:
        target = resource or tenant_resource(ctx)

        if ctx.purpose_ref is not None:
            # A purpose-bound credential is held to its narrowed permissions in
            # every mode: the PDP knows the person, not this token's ceiling.
            require(ctx, *any_of)

        if self.mode == "local":
            require(ctx, *any_of)
            return

        if self.mode == "shadow":
            local_error: AuthorizationError | None = None
            try:
                require(ctx, *any_of)
            except AuthorizationError as exc:
                local_error = exc
            await self._shadow(ctx, any_of, target, contextual, local_error is None)
            if local_error is not None:
                raise local_error
            return

        # policy: the PDP decides for every credential that has an IAM subject.
        if ctx.policy_subject is None:
            require(ctx, *any_of)
            return
        decision = await self._decide(ctx, any_of, target, contextual, consistency)
        if not decision.allowed:
            raise AuthorizationError(
                details={
                    "required": [p.value for p in any_of],
                    "resource": target.key,
                    "reasonCode": decision.reason_code,
                    "decisionId": decision.decision_id,
                },
            )

    async def visible_objects(
        self, ctx: AuthContext, action: Permission | str, resource_type: str
    ) -> set[str] | None:
        """Objects of ``resource_type`` on which ``action`` is allowed.

        ``None`` means "no restriction beyond the flat permission": local and
        shadow modes, and legacy credentials in policy mode — unless the
        caller is in ``members`` mode, when workspaces are narrowed to the
        visible set in every mode (CP-ADR-0082 §3.4).
        """
        decided = await self._policy_objects(ctx, action, resource_type)
        if resource_type != "workspace" or ctx.visible_workspaces is None:
            return decided
        if decided is None:
            return set(ctx.visible_workspaces)
        return decided & ctx.visible_workspaces

    async def _policy_objects(
        self, ctx: AuthContext, action: Permission | str, resource_type: str
    ) -> set[str] | None:
        if self.mode != "policy" or self._client is None or ctx.policy_subject is None:
            return None
        name = action.value if isinstance(action, Permission) else action
        sandbox.refuse_outgoing("policy")
        try:
            page = await self._client.list_objects(
                _policy_context(ctx),
                name,
                resource_type,
                on_behalf_of=ctx.policy_subject,
            )
        except AuthorizationUnavailable as exc:
            raise DependencyUnavailableError(
                "policy_unavailable", details={"action": name}
            ) from exc
        return set(page.objects)

    async def _decide(
        self,
        ctx: AuthContext,
        any_of: tuple[Permission, ...],
        target: ResourceRef,
        contextual: Sequence[ContextualTuple],
        consistency: Literal["default", "strong"],
    ) -> PolicyDecision:
        assert self._client is not None
        sandbox.refuse_outgoing("policy")
        last: PolicyDecision | None = None
        try:
            for permission in any_of:
                last = await self._client.check(
                    _policy_context(ctx),
                    permission.value,
                    target,
                    contextual=contextual,
                    on_behalf_of=ctx.policy_subject,
                    consistency=consistency,
                )
                if last.allowed:
                    return last
        except AuthorizationUnavailable as exc:
            observability.inc("authz_policy_unavailable_total")
            raise DependencyUnavailableError(
                "policy_unavailable",
                details={"required": [p.value for p in any_of], "resource": target.key},
            ) from exc
        except PermissionDenied as exc:
            # The client raises only on its own demand; here we read the decision.
            raise AuthorizationError(details={"required": [p.value for p in any_of]}) from exc
        assert last is not None
        return last

    async def _shadow(
        self,
        ctx: AuthContext,
        any_of: tuple[Permission, ...],
        target: ResourceRef,
        contextual: Sequence[ContextualTuple],
        local_allowed: bool,
    ) -> None:
        """Ask the PDP without letting it decide; count and log every divergence."""
        if ctx.policy_subject is None:
            observability.inc("authz_shadow_skipped_total")
            return
        try:
            decision = await self._decide(ctx, any_of, target, contextual, "default")
        except DependencyUnavailableError:
            observability.inc("authz_shadow_unavailable_total")
            return
        except AuthorizationError:
            observability.inc("authz_shadow_unavailable_total")
            return
        observability.inc("authz_shadow_checks_total")
        if decision.allowed != local_allowed:
            observability.inc("authz_shadow_divergence_total")
            logger.warning(
                "authz shadow divergence",
                extra={
                    "divergence": {
                        "tenantId": str(ctx.tenant_id),
                        "principalId": str(ctx.principal_id),
                        "iamPrincipalId": ctx.policy_subject,
                        "actions": [p.value for p in any_of],
                        "resource": target.key,
                        "localAllowed": local_allowed,
                        "policyAllowed": decision.allowed,
                        "reasonCode": decision.reason_code,
                        "decisionId": decision.decision_id,
                        "correlationId": ctx.correlation_id,
                    }
                },
            )


_authorizer = Authorizer(None, "local")


def configure_authorizer(authorizer: Authorizer) -> None:
    global _authorizer
    _authorizer = authorizer


def get_authorizer() -> Authorizer:
    return _authorizer


_LOCAL = Authorizer(None, "local")


def _current(ctx: AuthContext, actions: Sequence[str], resource: ResourceRef | None) -> Authorizer:
    """The configured authorizer; inside a package test, the local one (CP-ADR-0074 Z2).

    Except a read by the caller of the test of a resource the test did not
    write, which the PDP still decides in ``policy`` mode (Z7): a test never
    reads, in the caller's name, what the caller could not.
    """
    trial = sandbox.active()
    if trial is None:
        return _authorizer
    if _authorizer.mode == "policy" and trial.asks_policy(
        ctx.policy_subject, actions, resource.id if resource is not None else None
    ):
        return _authorizer
    return _LOCAL


async def authorize(
    ctx: AuthContext,
    *any_of: Permission,
    resource: ResourceRef | None = None,
    contextual: Sequence[ContextualTuple] = (),
    consistency: Literal["default", "strong"] = "default",
) -> None:
    """Raise 403 unless the actor may perform one of ``any_of`` on ``resource``.

    Without ``resource`` the question is asked at tenant level; commands name
    the concrete resource wherever it is known so that scoped bindings work.
    """
    authorizer = _current(ctx, [p.value for p in any_of], resource)
    with sandbox.permit("policy") if authorizer is not _LOCAL else nullcontext():
        await authorizer.authorize(
            ctx, *any_of, resource=resource, contextual=contextual, consistency=consistency
        )


async def visible_objects(
    ctx: AuthContext, action: Permission | str, resource_type: str
) -> set[str] | None:
    name = action.value if isinstance(action, Permission) else action
    authorizer = _current(ctx, [name], None)
    with sandbox.permit("policy") if authorizer is not _LOCAL else nullcontext():
        return await authorizer.visible_objects(ctx, action, resource_type)


__all__ = [
    "AUTHZ_MODES",
    "VISIBILITIES",
    "VISIBILITY_MEMBERS",
    "VISIBILITY_TENANT",
    "AuthContext",
    "Authorizer",
    "AuthzMode",
    "ResourceRef",
    "SystemContext",
    "WorkspaceNotVisible",
    "authorize",
    "check_workspace_visible",
    "configure_authorizer",
    "get_authorizer",
    "permits",
    "permits_task",
    "require",
    "tenant_resource",
    "visible_objects",
]
