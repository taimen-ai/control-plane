"""Tests of rules and task types: ``subject: rule | taskType`` of ``POST /packages:test``.

CP-ADR-0074, amendment 2026-09-29 (package-sdk), Z1-Z5. A test of a process
runs in the sandbox of the engine (:mod:`control_plane.domain.process_sandbox`);
a rule and a task type have no model in memory — they are the application
code of the core. So a test of them runs **that code**, in a transaction that
is always rolled back:

- **publication** — the objects of the package are published inside the
  transaction by the commands their routes run: artifact types, roles and
  skills the tenant does not have yet, then ``TaskType``, ``Agent`` and
  ``WorkRule`` by :func:`package_catalog.publish`, each in its savepoint, in
  the order of an apply. What a command refuses is a finding of the object;
  a right the caller lacks is the warning ``permission_required`` and ends
  the publication, as in the trial of a plan. A rule is published enabled
  and acts as the caller (or its agent);
- **one test, one transaction** — each test publishes and runs in a
  transaction of its own, rolled back when the test ends, so tests do not
  see each other and the journal of the database waits for no more than one
  test (a transaction that writes holds back ``pg_snapshot_xmin``, and with
  it the delivery of every event committed after it began). A request runs
  at most :data:`MAX_SUBJECT_TESTS` of them; ``SET LOCAL lock_timeout`` keeps
  a key edited on the stand from holding a test, ``statement_timeout`` and
  :data:`TEST_DEADLINE` bound it (each is an error of the test);
- **the code of the core** — a rule: its input is recorded by the command of
  observations (or as a journal event), and only the rule under test is
  evaluated on it (:func:`rule_evaluations.evaluate_trigger`, then
  :func:`rule_evaluations.resume_evaluation`); a task type: a task of it is
  filed, a gate is decided (:func:`approvals.decide_approval`) and its
  outcome executed (:func:`approval_outcomes.execute_outcome`), the task is
  completed (:func:`tasks.complete_task`, which files the work after
  completion) and verified (:func:`verification.execute_verification`);
- **mocks** — every skill call is queued the ordinary way and answered by
  the test's ``mocks.skills``: a mock executor takes the call and completes
  or fails it by the executor's commands, so ``onSuccess``/``onFailure`` and
  the rule's resumption run as on the stand. A call without a mock stays
  unanswered (``unmocked_skill_call`` when that leaves the rule waiting);
  time does not move (:mod:`control_plane.sandbox`);
- **what the worker would skip** — an input the rules worker would not
  evaluate the rule on (another workspace, a consequence of a rule, before
  the rule was enabled) is not evaluated either: the warning
  ``input_not_delivered`` says why;
- **authorization** — local, except a read in the caller's name of what the
  test did not write, which the PDP decides in ``policy`` mode;
- **what a test sees** — the journal of its own transaction (``events.tx_id``):
  work filed (``task.created``), skill calls queued, and the rows they point
  at;
- **the setting** (Z8) — the workspace of the request or one of the test;
  an install variable of kind principal is always a principal of the test;
  of kind role and workspace, a row of the test unless the value is the id
  of such a row of the tenant (a workspace: one the caller may read
  processes of); an ArtifactType without ``mediaTypes`` takes any (``*/*``,
  as in package-sdk); an artifact of ``given`` may carry content (its upload
  record, no bytes, at most :data:`GIVEN_CONTENT_MAX_BYTES`); a rule test
  may start from a slot of the rule's schedule and from a task filed before
  its input; a task of the stand ``given.event`` names is one the caller
  may read. A setting the core refuses is an error of the
  test (``given_refused``), never an error of the request.

Coverage (Z3): every rule of the package counts the branches of its
``condition`` and ``where`` and its outcomes; every task type with gates,
work after completion or acceptance counts its declared outcomes and their
reactions, preconditions, completion actions and checks.
"""

import copy
import hashlib
import json
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event as orm_event
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane import sandbox as trial_hooks
from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    permits,
)
from control_plane.application.commands import agents as agent_commands
from control_plane.application.commands import artifact_types as artifact_type_commands
from control_plane.application.commands import org as org_commands
from control_plane.application.commands import package_catalog, rule_evaluations
from control_plane.application.commands import work_rules as rule_commands
from control_plane.application.commands.approval_outcomes import OUTCOME_LIVE, execute_outcome
from control_plane.application.commands.approvals import decide_approval, request_approval
from control_plane.application.commands.artifacts import content_ref, create_artifact
from control_plane.application.commands.observations import record_observation
from control_plane.application.commands.package_catalog import CATALOG_KINDS, SpecShape
from control_plane.application.commands.package_settings import unknown_refs
from control_plane.application.commands.process_definitions import process_scope
from control_plane.application.commands.skill_invocations import (
    LIVE_STATUSES,
    complete_skill_invocation,
    fail_skill_invocation,
)
from control_plane.application.commands.task_types import check_type_documents
from control_plane.application.commands.tasks import complete_task, create_task, update_task
from control_plane.application.commands.verification import (
    OPEN_STATUSES,
    Timing,
    execute_verification,
)
from control_plane.application.commands.workspace_types import ensure_system_workspace_type
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.config import Settings
from control_plane.domain import process_sandbox as process_trial
from control_plane.domain.approval_outcomes import (
    APPROVAL_PRECONDITION_FAILED,
    DEFAULT_GATE,
    INVOKE_SKILL,
    ON_FAILURE,
    ON_SUCCESS,
    OUTCOMES,
    ApprovalSchema,
    parse_approval_schema,
)
from control_plane.domain.artifact_type import normalize_media_type
from control_plane.domain.completion_work import schema_of
from control_plane.domain.enums import (
    AgentStatus,
    ApprovalStatus,
    Permission,
    PrincipalKind,
    PrincipalStatus,
    SkillInvocationStatus,
    WorkspaceStatus,
)
from control_plane.domain.errors import AuthorizationError, DomainError, NotFoundError
from control_plane.domain.package_plan import canonical_hash
from control_plane.domain.package_settings import (
    check_declaration,
    references,
    validate,
)
from control_plane.domain.package_source import (
    SUBJECT_RULE,
    SUBJECT_TASK_TYPE,
    PackageObject,
    PackageTestFile,
    ParsedPackage,
)
from control_plane.domain.process_definition import Problem
from control_plane.domain.project import secret_findings
from control_plane.domain.settings_refs import SettingsScope
from control_plane.domain.work_graph import CheckKind, EvidenceKind
from control_plane.domain.work_rules import (
    BASE_ROOTS,
    ROOT_ITEM,
    ROOT_SKILL,
    EvaluationStatus,
    RuleStatus,
    TriggerKind,
    branch_outcomes,
    expression_branches,
    trigger_matches,
)
from control_plane.infrastructure.db.models import (
    Agent,
    ApiKey,
    Approval,
    ApprovalOutcomeAction,
    ArtifactContent,
    ArtifactType,
    Event,
    PackageSettings,
    PackageSettingsSchema,
    PackageSettingsVersion,
    Principal,
    PrincipalRole,
    Role,
    RuleEvaluation,
    Skill,
    SkillInvocation,
    Task,
    TaskComment,
    TaskCompletionWork,
    TaskRelation,
    TaskRequirement,
    TaskType,
    TaskVerification,
    WorkRule,
    Workspace,
)
from control_plane.infrastructure.db.models import PackageObject as PackageRecord

# The kinds published before the catalog kinds, when the tenant lacks them:
# what a type, an agent or a rule of the package names.
SUPPORTING_KINDS = ("ArtifactType", "Role", "Skill")
# The shape of a supporting object: the keyword arguments of its command.
SupportingShape = Callable[[PackageObject], tuple[dict[str, Any] | None, list[Problem]]]

LOCK_TIMEOUT = "lock_timeout"
STATEMENT_TIMEOUT = "statement_timeout"
TEST_TIMEOUT = "test_timeout"
PERMISSION_REQUIRED = "permission_required"
TOO_MANY_TESTS = "too_many_tests"
INPUT_NOT_DELIVERED = "input_not_delivered"
UNMOCKED_SKILL_CALL = "unmocked_skill_call"
GIVEN_REFUSED = "given_refused"
# The kinds of an install variable whose value is a row of the stand
# (``packageVariable.kind`` of the catalog schema): the test gives it one of its own.
PRINCIPAL_VARIABLE = "principal"
ROLE_VARIABLE = "role"
WORKSPACE_VARIABLE = "workspace"
# How long an upload of ``given.artifacts[].content`` lives: longer than a test.
GIVEN_UPLOAD_TTL = timedelta(hours=1)
# The bytes of one ``given.artifacts[].content``: the schema counts characters,
# and a character is up to four bytes of UTF-8.
GIVEN_CONTENT_MAX_BYTES = 1024 * 1024
# Rule and task type tests one request runs: each holds a writing transaction.
MAX_SUBJECT_TESTS = 100
# One statement of a test, and one test as a whole (checked between its steps).
STATEMENT_TIMEOUT_MS = 5000
TEST_DEADLINE = timedelta(seconds=30)
# The rights a principal of a test acts with (a decider, an assignee): those
# of a person who works on tasks. The caller's own rights are what the
# publication and a rule without an identity act with.
PRINCIPAL_PERMISSIONS = frozenset(
    {
        Permission.TASKS_READ.value,
        Permission.TASKS_WRITE.value,
        Permission.APPROVALS_READ.value,
        Permission.APPROVALS_DECIDE.value,
        Permission.APPROVALS_MANAGE.value,
        Permission.SKILLS_INVOKE.value,
        Permission.ARTIFACTS_READ.value,
        Permission.ARTIFACTS_WRITE.value,
        Permission.OBSERVATIONS_WRITE.value,
        Permission.EVENTS_READ.value,
    }
)
# How many passes over outcomes, attempts, evaluations and calls a step may take.
MAX_ROUNDS = 50
_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_TEST_ISSUER = "control-plane:package-test"


# --- results -------------------------------------------------------------------------


@dataclass
class SubjectResult:
    """``PackageTestResultOut`` of a rule or task type test."""

    file: str
    name: str
    subject: str
    object: str
    status: str
    duration_ms: int
    failures: list[process_trial.Failure]

    __test__ = False  # not a pytest class

    def out(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "name": self.name,
            "subject": self.subject,
            "object": self.object,
            "process": None,
            "status": self.status,
            "durationMs": self.duration_ms,
            "failures": [failure.out() for failure in self.failures],
        }


@dataclass
class RuleCoverage:
    rule: str
    tests: int
    branches: process_trial.Counter
    outcomes: process_trial.Counter

    def out(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "tests": self.tests,
            "branches": self.branches.out(),
            "outcomes": self.outcomes.out(),
        }


@dataclass
class TaskTypeCoverage:
    task_type: str
    version: int
    tests: int
    outcomes: process_trial.Counter
    preconditions: process_trial.Counter
    completion: process_trial.Counter
    acceptance: process_trial.Counter

    def out(self) -> dict[str, Any]:
        return {
            "taskType": self.task_type,
            "version": self.version,
            "tests": self.tests,
            "outcomes": self.outcomes.out(),
            "preconditions": self.preconditions.out(),
            "completion": self.completion.out(),
            "acceptance": self.acceptance.out(),
        }


@dataclass
class SubjectReport:
    problems: list[Problem] = field(default_factory=list)
    tests: list[SubjectResult] = field(default_factory=list)
    rule_coverage: list[RuleCoverage] = field(default_factory=list)
    task_type_coverage: list[TaskTypeCoverage] = field(default_factory=list)


class _Abort(Exception):
    """The test cannot go on: a failure of the step, or an error of the test."""

    def __init__(
        self, message: str, expected: Any = None, actual: Any = None, *, error: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual
        self.error = error


# --- the package's objects -----------------------------------------------------------


def manifest_variables(package: ParsedPackage) -> dict[str, str]:
    """The defaults of the installation variables the manifest declares."""
    declared = (package.manifest or {}).get("variables") or {}
    if not isinstance(declared, dict):
        return {}
    return {
        str(name): str(item["default"])
        for name, item in declared.items()
        if isinstance(item, dict) and item.get("default") is not None
    }


def variable_kinds(package: ParsedPackage) -> dict[str, str]:
    """The kind of each installation variable the manifest declares."""
    declared = (package.manifest or {}).get("variables") or {}
    if not isinstance(declared, dict):
        return {}
    return {
        str(name): item["kind"]
        for name, item in declared.items()
        if isinstance(item, dict) and isinstance(item.get("kind"), str)
    }


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def substitute(value: Any, variables: Mapping[str, str]) -> Any:
    """``${NAME}`` in every string replaced by its value; unknown names stay."""
    if isinstance(value, str):
        return _VARIABLE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    return value


def _unresolved(value: Any) -> bool:
    if isinstance(value, str):
        return _VARIABLE.search(value) is not None
    if isinstance(value, list):
        return any(_unresolved(item) for item in value)
    if isinstance(value, dict):
        return any(_unresolved(item) for item in value.values())
    return False


def _prepared(
    obj: PackageObject, variables: Mapping[str, str], workspace_id: uuid.UUID | None
) -> PackageObject:
    """The object with the variables of the test and the workspace of the request."""
    from control_plane.application.commands.package_test import with_workspace

    spec = substitute(obj.spec, variables)
    if obj.kind == "WorkRule":
        spec = with_workspace(spec, workspace_id)
    return PackageObject(obj.kind, obj.key, spec, obj.file, obj.lines)


def finding(obj: PackageObject, exc: DomainError, severity: str = "error") -> Problem:
    return obj.place(package_catalog.finding(exc, severity))


def settings_scope(package: ParsedPackage) -> SettingsScope:
    """What ``settings`` of the package's objects is: the schema its files declare (§6)."""
    manifest = package.manifest_object
    declared = check_declaration(package).declared
    return SettingsScope(
        manifest.key if manifest is not None else None,
        declared.schema if declared is not None else None,
    )


def static_problems(
    package: ParsedPackage,
    shapes: Mapping[str, SpecShape],
    workspace_id: uuid.UUID | None,
) -> list[Problem]:
    """The form and vocabulary of the rules and task types, and the shape of the agents.

    ``invalid_rule`` / ``invalid_task_type`` with file and line (Z4). An
    object with an installation variable that has no default is checked when
    a test that gives its value publishes it: its form depends on the value.
    """
    problems: list[Problem] = []
    variables = manifest_variables(package)
    scope = settings_scope(package)
    for kind in ("TaskType", "WorkRule"):
        for obj in sorted(package.of_kind(kind), key=lambda o: o.key):
            prepared = _prepared(obj, variables, workspace_id)
            if _unresolved(prepared.spec):
                continue
            sent, found = shapes[kind](prepared)
            problems.extend(obj.place(p) for p in found)
            if sent is None:
                continue
            try:
                form = package_catalog.wanted_form(kind, sent, None, scope)
                if kind == "TaskType":
                    check_type_documents(
                        field_schema=form["fieldSchema"],
                        lifecycle_schema=form["lifecycleSchema"],
                        approval_schema=form["approvalSchema"],
                        context_schema=form["contextSchema"],
                        instructions=form["instructions"],
                        completion_schema=form["completionSchema"],
                        artifact_schema=form["artifactSchema"],
                    )
            except DomainError as exc:
                problems.append(finding(obj, exc))
    # An agent is checked by its shape, as ``POST /agents`` takes it: a fractional
    # ``cpus`` is a finding here, not a refusal of the apply (CP-ADR-0073,
    # amendment 2026-10-03).
    for obj in sorted(package.of_kind("Agent"), key=lambda o: o.key):
        prepared = _prepared(obj, variables, workspace_id)
        if _unresolved(prepared.spec):
            continue
        _, found = shapes["Agent"](prepared)
        problems.extend(obj.place(p) for p in found)
    return problems


# --- the principals of a test ----------------------------------------------------------


class _People:
    """The caller and the temporary principals of one test; gone with its rollback."""

    def __init__(self, db: AsyncSession, caller: AuthContext, trace: str) -> None:
        self.db = db
        self.caller = caller
        self.trace = trace
        self.by_name: dict[str, AuthContext] = {}
        self.names: dict[uuid.UUID, str] = {}
        self._executor: AuthContext | None = None

    def context(self, base: AuthContext) -> AuthContext:
        """``base`` with the correlation of the test."""
        return AuthContext(
            tenant_id=base.tenant_id,
            principal_id=base.principal_id,
            principal_kind=base.principal_kind,
            api_key_id=base.api_key_id,
            permissions=base.permissions,
            request_id=self.trace,
            correlation_id=self.trace,
            trace_run_id=self.trace,
            iam_principal_id=base.iam_principal_id,
        )

    @property
    def acting_caller(self) -> AuthContext:
        return self.context(self.caller)

    async def create(
        self, name: str, permissions: frozenset[str], kind: str = PrincipalKind.HUMAN
    ) -> AuthContext:
        now = utcnow()
        principal = Principal(
            id=new_uuid(),
            tenant_id=self.caller.tenant_id,
            kind=kind,
            display_name=name,
            status=PrincipalStatus.ACTIVE,
            metadata_json={"packageTest": name},
            created_at=now,
            updated_at=now,
        )
        self.db.add(principal)
        await self.db.flush()
        key = ApiKey(
            id=new_uuid(),
            tenant_id=self.caller.tenant_id,
            principal_id=principal.id,
            key_prefix=f"test_{secrets.token_hex(8)}",
            key_hash=secrets.token_hex(32),
            permissions=sorted(permissions),
            expires_at=None,
            last_used_at=None,
            revoked_at=None,
            created_at=now,
        )
        self.db.add(key)
        await self.db.flush()
        ctx = AuthContext(
            tenant_id=self.caller.tenant_id,
            principal_id=principal.id,
            principal_kind=kind,
            api_key_id=key.id,
            permissions=permissions,
            request_id=self.trace,
            correlation_id=self.trace,
            trace_run_id=self.trace,
        )
        self.names[principal.id] = name
        return ctx

    async def named(self, name: str) -> AuthContext:
        """The principal of the test called ``name``, created on first mention."""
        if name not in self.by_name:
            self.by_name[name] = await self.create(name, PRINCIPAL_PERMISSIONS)
        return self.by_name[name]

    async def as_principal(self, principal_id: uuid.UUID) -> AuthContext:
        """A context of an existing principal (a stand's person asked for a decision)."""
        for ctx in self.by_name.values():
            if ctx.principal_id == principal_id:
                return ctx
        if principal_id == self.caller.principal_id:
            return self.acting_caller
        now = utcnow()
        key = ApiKey(
            id=new_uuid(),
            tenant_id=self.caller.tenant_id,
            principal_id=principal_id,
            key_prefix=f"test_{secrets.token_hex(8)}",
            key_hash=secrets.token_hex(32),
            permissions=sorted(PRINCIPAL_PERMISSIONS),
            expires_at=None,
            last_used_at=None,
            revoked_at=None,
            created_at=now,
        )
        self.db.add(key)
        await self.db.flush()
        kind = await self.db.scalar(select(Principal.kind).where(Principal.id == principal_id))
        return AuthContext(
            tenant_id=self.caller.tenant_id,
            principal_id=principal_id,
            principal_kind=str(kind or PrincipalKind.HUMAN),
            api_key_id=key.id,
            permissions=PRINCIPAL_PERMISSIONS,
            request_id=self.trace,
            correlation_id=self.trace,
            trace_run_id=self.trace,
        )

    async def executor(self) -> AuthContext:
        """The mock executor of skill calls."""
        if self._executor is None:
            self._executor = await self.create(
                "mock-executor",
                frozenset({Permission.SKILLS_EXECUTE.value}),
                kind=PrincipalKind.SERVICE,
            )
        return self._executor

    async def role(self, slug: str, workspace_id: uuid.UUID | None) -> Role:
        """The role ``slug`` of the tenant, or a role of the test with that slug."""
        found = await self.db.scalar(
            select(Role)
            .where(Role.tenant_id == self.caller.tenant_id, Role.slug == slug)
            .order_by(Role.workspace_id.is_not(None), Role.created_at)
            .limit(1)
        )
        if found is not None:
            return found
        now = utcnow()
        role = Role(
            id=new_uuid(),
            tenant_id=self.caller.tenant_id,
            workspace_id=None,
            slug=slug,
            name=slug,
            description="a role of a package test",
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.db.add(role)
        await self.db.flush()
        return role

    async def grant(self, ctx: AuthContext, role_id: uuid.UUID) -> None:
        held = await self.db.scalar(
            select(PrincipalRole.id).where(
                PrincipalRole.principal_id == ctx.principal_id,
                PrincipalRole.role_id == role_id,
                PrincipalRole.workspace_id.is_(None),
            )
        )
        if held is not None:
            return
        self.db.add(
            PrincipalRole(
                id=new_uuid(),
                tenant_id=self.caller.tenant_id,
                principal_id=ctx.principal_id,
                role_id=role_id,
                workspace_id=None,
                created_at=utcnow(),
            )
        )
        await self.db.flush()

    async def given(self, principals: Mapping[str, Sequence[str]]) -> None:
        """``given.principals``: role -> names of the test; each name holds the role."""
        for slug, names in principals.items():
            role = await self.role(str(slug), None)
            for name in names:
                await self.grant(await self.named(str(name)), role.id)

    async def label(self, principal_id: uuid.UUID | None) -> str | None:
        """How a test names a principal: its test name, ``agent:<key>`` or the id."""
        if principal_id is None:
            return None
        if principal_id in self.names:
            return self.names[principal_id]
        agent = await self.db.scalar(
            select(Agent.key).where(
                Agent.tenant_id == self.caller.tenant_id, Agent.principal_id == principal_id
            )
        )
        return f"agent:{agent}" if agent else str(principal_id)


# --- the run ---------------------------------------------------------------------------


def limit_problems(tests: Sequence[PackageTestFile]) -> list[Problem]:
    """More rule and task type tests than a request runs: an error of the request."""
    if len(tests) <= MAX_SUBJECT_TESTS:
        return []
    return [
        Problem(
            TOO_MANY_TESTS,
            "error",
            "/tests",
            f"the request runs {len(tests)} rule and task type tests; at most"
            f" {MAX_SUBJECT_TESTS} run in one request",
            hint="name the test files to run in `tests`",
        )
    ]


async def run_subject_tests(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    settings: Settings,
    *,
    package: ParsedPackage,
    tests: Sequence[PackageTestFile],
    workspace_id: uuid.UUID | None,
    shapes: Mapping[str, SpecShape],
    supporting: Mapping[str, SupportingShape],
) -> SubjectReport:
    """Run the rule and task type tests of a package, each in a transaction rolled back."""
    report = SubjectReport()
    runs: list[_Run] = []
    for test in tests[:MAX_SUBJECT_TESTS]:
        async with session_factory() as db:
            tx = await db.begin()
            try:
                await db.execute(text("SET LOCAL lock_timeout = '2s'"))
                await db.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
                run = _Run(db, ctx, settings, package, test, workspace_id, shapes, supporting)
                await run.execute()
            finally:
                await tx.rollback()
        runs.append(run)
        for problem in run.problems:
            if problem not in report.problems:
                report.problems.append(problem)
    async with session_factory() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        versions = await _planned_versions(db, ctx, package, shapes, workspace_id)
    report.tests = [run.result for run in runs]
    report.rule_coverage = rule_coverage(package, runs)
    report.task_type_coverage = task_type_coverage(package, runs, versions)
    return report


async def _planned_versions(
    db: AsyncSession,
    ctx: AuthContext,
    package: ParsedPackage,
    shapes: Mapping[str, SpecShape],
    workspace_id: uuid.UUID | None,
) -> dict[str, int]:
    """The version each task type of the package would have after an apply."""
    versions: dict[str, int] = {}
    variables = manifest_variables(package)
    for obj in package.of_kind("TaskType"):
        latest = await package_catalog.latest_of(db, ctx.tenant_id, "TaskType", obj.key)
        sent, _ = shapes["TaskType"](_prepared(obj, variables, workspace_id))
        action = "create"
        if latest is not None and sent is not None:
            try:
                form = package_catalog.wanted_form("TaskType", sent, latest)
            except DomainError:
                form = None
            action = "unchanged" if form == latest.spec else "update"
        versions[obj.key] = await package_catalog.planned_version(
            db, ctx.tenant_id, "TaskType", obj.key, latest, {}, action
        )
    return versions


@dataclass
class _Seen:
    """What a test observed of its object, for the coverage."""

    rule_branches: set[str] = field(default_factory=set)
    rule_outcomes: set[str] = field(default_factory=set)
    outcomes: set[str] = field(default_factory=set)
    preconditions: set[str] = field(default_factory=set)
    completion: set[str] = field(default_factory=set)
    acceptance: set[str] = field(default_factory=set)


class _Run:
    """One rule or task type test in its savepoint."""

    def __init__(
        self,
        db: AsyncSession,
        ctx: AuthContext,
        settings: Settings,
        package: ParsedPackage,
        test: PackageTestFile,
        workspace_id: uuid.UUID | None,
        shapes: Mapping[str, SpecShape],
        supporting: Mapping[str, SupportingShape],
    ) -> None:
        self.db = db
        self.ctx = ctx
        self.settings = settings
        self.package = package
        self.test = test
        self.data = test.data
        self.workspace_id = workspace_id
        self.shapes = shapes
        self.supporting = supporting
        given = self.data.get("given") or {}
        self.given: dict[str, Any] = given
        self.settings_scope = settings_scope(package)
        self.variables = {**manifest_variables(package), **(given.get("variables") or {})}
        self.mocks: dict[str, Any] = self.data.get("mocks") or {}
        self.used: dict[str, int] = {}
        self.problems: list[Problem] = []
        self.failures: list[process_trial.Failure] = []
        self.seen = _Seen()
        self.trace = f"package-test:{uuid.uuid4()}"
        clock = given.get("clock")
        # The time the test starts at: ``given.clock``, before any read moves it.
        self.start = _time(str(clock)) if clock else process_trial.DEFAULT_CLOCK
        self.trial = trial_hooks.Trial(
            clock=self.start,
            policy_subjects=frozenset({ctx.policy_subject} if ctx.policy_subject else ()),
        )
        self.deadline = time.monotonic() + TEST_DEADLINE.total_seconds()
        self.result = SubjectResult(
            file=test.file,
            name=test.name,
            subject=test.subject,
            object=test.object,
            status="passed",
            duration_ms=0,
            failures=self.failures,
        )
        # Filled as the test goes.
        self.people: _People
        self.tx_id = 0
        self.mark = 0
        self.task: Task | None = None
        # ``given.task`` of a rule test: work on the stand before the input.
        self.given_task: Task | None = None
        self.rule: WorkRule | None = None
        self.evaluation: RuleEvaluation | None = None
        # Why the rules worker would not evaluate the rule on the input.
        self.skipped: str | None = None
        self.answered: set[uuid.UUID] = set()
        self.published: dict[tuple[str, str], Any] = {}
        # Role variables whose slug a Role of the package declares: variable -> slug.
        self.package_roles: dict[str, str] = {}

    # --- the frame -----------------------------------------------------------------

    async def execute(self) -> None:
        """The test in the transaction of ``db``; the caller rolls it back."""
        started = time.monotonic()
        step = 0
        orm_event.listen(self.db.sync_session, "after_flush", self._written_rows)
        try:
            with trial_hooks.trial(self.trial):
                self.people = _People(self.db, self.ctx, self.trace)
                self.tx_id = int(
                    await self.db.scalar(text("SELECT pg_current_xact_id()::text::bigint"))
                )
                await self._workspace()
                await self._stand_variables()
                await self._publish()
                await self._stand_settings()
                if self.test.subject == SUBJECT_RULE:
                    await self._rule()
                else:
                    await self._task_type()
                for step, spec in enumerate(self.data.get("steps") or ()):
                    self._in_time()
                    await self._step(step, spec)
                self._minimum(len(self.data.get("steps") or ()) - 1)
                await self._unmocked_calls()
        except _Abort as abort:
            self.failures.append(
                process_trial.Failure(step, abort.message, abort.expected, abort.actual)
            )
            if abort.error:
                self.result.status = "error"
        except trial_hooks.SandboxOutgoingCall as exc:
            self.failures.append(process_trial.Failure(step, f"{exc.code}: {exc}"))
            self.result.status = "error"
        except DomainError as exc:
            # A refusal no step turned into a verdict: the test is red and says why.
            self.failures.append(
                process_trial.Failure(
                    step,
                    f"the core refused: {exc.code}: {exc.message}",
                    actual={"code": exc.code, "details": exc.details},
                )
            )
            self.result.status = "error"
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code == "55P03":  # lock_not_available
                message = f"{LOCK_TIMEOUT}: an object of the test is being changed on the stand"
            elif code == "57014":  # query_canceled
                message = (
                    f"{STATEMENT_TIMEOUT}: a statement of the test ran longer than"
                    f" {STATEMENT_TIMEOUT_MS} ms"
                )
            else:
                raise
            self.failures.append(process_trial.Failure(step, message))
            self.result.status = "error"
        finally:
            orm_event.remove(self.db.sync_session, "after_flush", self._written_rows)
        if self.trial.outgoing and self.result.status != "error":
            self.failures.append(
                process_trial.Failure(
                    step,
                    f"{trial_hooks.SANDBOX_OUTGOING_CALL}: the code tried to reach "
                    + ", ".join(sorted(set(self.trial.outgoing))),
                )
            )
            self.result.status = "error"
        if self.failures and self.result.status == "passed":
            self.result.status = "failed"
        self.result.duration_ms = int((time.monotonic() - started) * 1000)

    async def _reload(self, row: Any) -> None:
        """``row`` as the database has it, after what the code changed in memory.

        The session does not autoflush: a refresh alone would drop what a
        command set on the row and did not flush (an evaluation left waiting).
        """
        await self.db.flush()
        await self.db.refresh(row)

    def _written_rows(self, session: Any, flush_context: Any) -> None:
        """The rows this flush inserted: the PDP does not know them (:mod:`sandbox`)."""
        for row in session.new:
            row_id = getattr(row, "id", None)
            if row_id is not None:
                self.trial.written.add(str(row_id))

    def _in_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Abort(
                f"{TEST_TIMEOUT}: the test ran longer than {int(TEST_DEADLINE.total_seconds())} s",
                error=True,
            )

    async def _step(self, index: int, spec: Mapping[str, Any]) -> None:
        if "expect" in spec:
            self.failures.extend(await self._expect(index, spec["expect"]))
            return
        try:
            if "approve" in spec:
                self.failures.extend(await self._approve(index, spec["approve"]))
            elif "verify" in spec:
                await self._verify(spec["verify"])
            elif "complete" in spec:
                await self._complete(spec["complete"])
            await self._settle()
        except _Abort:
            raise
        except DomainError as exc:
            raise _Abort(
                f"the core refused the step: {exc.code}: {exc.message}",
                actual={"code": exc.code, "details": exc.details},
            ) from exc

    def _minimum(self, last: int) -> None:
        minimum = (self.data.get("coverage") or {}).get("minimum")
        if minimum is None:
            return
        if self.test.subject == SUBJECT_RULE:
            spec = _object(self.package, "WorkRule", self.test.object)
            total = rule_branches(spec) if spec else []
            reached = self.seen.rule_branches
            what = "branches of the rule"
        else:
            spec = _object(self.package, "TaskType", self.test.object)
            total = type_outcomes(spec) if spec else []
            reached = self.seen.outcomes
            what = "outcomes of the task type"
        counter = process_trial._counter(total, reached)
        if counter.percent < float(minimum):
            self.failures.append(
                process_trial.Failure(
                    max(last, 0),
                    f"the test covers {counter.percent:.1f}% of the {what}",
                    minimum,
                    {"percent": round(counter.percent, 1), "missing": counter.missing},
                )
            )

    # --- the setting -----------------------------------------------------------------

    @asynccontextmanager
    async def _given(self, where: str) -> AsyncIterator[None]:
        """What the core refuses of the setting at ``where`` is an error of the test."""
        try:
            yield
        except DomainError as exc:
            raise _Abort(
                f"{GIVEN_REFUSED}: {where}: the core refused it: {exc.code}: {exc.message}",
                actual={"code": exc.code, "details": exc.details},
                error=True,
            ) from exc

    async def _workspace(self) -> None:
        """The workspace of the request, or a workspace of the test in its transaction.

        Inserted as a row, not by the command of workspaces: that one holds the
        lock of the tenant's workspace tree, which a test would keep until its
        rollback. The slug is the test's own, so no sibling waits for it.
        """
        if self.workspace_id is not None:
            return
        workspace_type = await ensure_system_workspace_type(self.db, self.ctx.tenant_id)
        now = utcnow()
        workspace = Workspace(
            id=new_uuid(),
            tenant_id=self.ctx.tenant_id,
            parent_id=None,
            type_id=workspace_type.id,
            slug=f"package-test-{secrets.token_hex(8)}",
            name="package test",
            description="the workspace of a package test",
            custom_fields={},
            status=WorkspaceStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.db.add(workspace)
        await self.db.flush()
        self.workspace_id = workspace.id

    async def _stand_variables(self) -> None:
        """Variables of kind principal, role and workspace name rows of the test.

        ``principal`` — always the principal of the test called by the value
        (by the variable's name when the value is empty or an id): a scenario
        does not depend on the principals of the stand. ``role`` — the id of a
        role of the tenant stays; otherwise the role with the value (or the
        name) as its slug. A slug of a Role of the package waits for its
        publication (``_package_roles``): the variable does not take it.
        ``workspace`` — the id of a workspace of the tenant the caller may
        read processes of (as ``workspaceId`` of the request) stays; otherwise
        the workspace of the test, so a workspace the caller may not read is
        told apart from none.
        """
        declared = {obj.key for obj in self.package.of_kind("Role")}
        for name, kind in sorted(variable_kinds(self.package).items()):
            value = self.variables.get(name) or None
            given_id = _uuid(value)
            if kind == PRINCIPAL_VARIABLE:
                label = value if value and given_id is None else name
                self.variables[name] = str((await self.people.named(label)).principal_id)
            elif kind == ROLE_VARIABLE:
                if await self._of_tenant(Role, given_id):
                    continue
                slug = value if value and given_id is None else name.lower().replace("_", "-")
                if slug in declared:
                    self.variables.pop(name, None)
                    self.package_roles[name] = slug
                    continue
                self.variables[name] = str((await self.people.role(slug, None)).id)
            elif kind == WORKSPACE_VARIABLE:
                if await self._of_tenant(Workspace, given_id) and await self._reads(given_id):
                    continue
                self.variables[name] = str(self.workspace_id)

    async def _package_roles(self) -> None:
        """Role variables naming a Role of the package: that role, once published."""
        for name, slug in sorted(self.package_roles.items()):
            self.variables[name] = str((await self.people.role(slug, None)).id)

    async def _reads(self, workspace_id: uuid.UUID | None) -> bool:
        """May the caller read processes of the workspace, as of ``workspaceId`` of the request?

        Asked in the trial: the PDP decides it in ``policy`` mode (Z7).
        """
        return await permits(
            self.ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id)
        )

    async def _of_tenant(self, model: Any, row_id: uuid.UUID | None) -> bool:
        if row_id is None:
            return False
        found = await self.db.scalar(
            select(model.id).where(model.id == row_id, model.tenant_id == self.ctx.tenant_id)
        )
        return found is not None

    async def _file_task(
        self, spec: Mapping[str, Any], type_key: str, workspace_id: uuid.UUID | None, where: str
    ) -> Task:
        """A task of the setting, filed by the caller as the API files it."""
        assignee = spec.get("assignee")
        assignee_ref: uuid.UUID | str | None = None
        if isinstance(assignee, str) and assignee:
            if assignee.startswith("agent:"):
                assignee_ref = assignee
            else:
                assignee_ref = (await self.people.named(assignee)).principal_id
        filer = self.people.acting_caller
        async with self._given(where):
            task = await create_task(
                self.db,
                filer,
                title=str(spec.get("title") or f"{type_key}: {self.test.name}")[:500],
                type_key=type_key,
                assignee_id=assignee_ref,
                workspace_id=workspace_id,
                custom_fields=spec.get("customFields"),
            )
        if spec.get("status") and spec["status"] != task.status:
            async with self._given(f"{where}.status"):
                task = await update_task(
                    self.db,
                    filer,
                    task_ref=str(task.id),
                    expected_version=task.version,
                    status=str(spec["status"]),
                )
        return task

    async def _upload(self, filer: AuthContext, content: str, media_type: str) -> str:
        """``given.artifacts[].content`` as an upload of the filer, for ``contentRef``.

        Only the record: the content store is out of reach of a test (Z2), and
        what reads a task's artifacts in the core — the checks of its outputs —
        reads their records, never their bytes.
        """
        data = content.encode("utf-8")
        limit = min(GIVEN_CONTENT_MAX_BYTES, self.settings.artifact_max_bytes)
        if len(data) > limit:
            raise _Abort(
                f"{GIVEN_REFUSED}: given.artifacts: the content is larger than {limit} bytes",
                error=True,
            )
        sha256 = hashlib.sha256(data).hexdigest()
        now = utcnow()
        upload = ArtifactContent(
            id=new_uuid(),
            tenant_id=filer.tenant_id,
            uploaded_by_principal_id=filer.principal_id,
            sha256=sha256,
            size_bytes=len(data),
            media_type=normalize_media_type(media_type),
            storage_key=f"package-test/{sha256}",
            created_at=now,
            expires_at=now + GIVEN_UPLOAD_TTL,
            referenced_at=None,
        )
        self.db.add(upload)
        await self.db.flush()
        return content_ref(upload.id)

    # --- publication -----------------------------------------------------------------

    async def _publish(self) -> None:
        """The objects of the package, by the commands of their kinds, in apply order."""
        ctx = self.people.acting_caller
        for kind in (*SUPPORTING_KINDS, *CATALOG_KINDS):
            if kind == CATALOG_KINDS[0]:
                # The supporting objects are in: a role variable may name a Role of them.
                await self._package_roles()
            for obj in sorted(self.package.of_kind(kind), key=lambda o: o.key):
                prepared = _prepared(obj, self.variables, self.workspace_id)
                if _unresolved(prepared.spec):
                    # Published as it is, it would be refused for the variable, not
                    # for itself: only a test that needs it has to give the value.
                    if self._needs(obj):
                        names = sorted(set(_VARIABLE.findall(json.dumps(prepared.spec))))
                        raise _Abort(
                            f"unresolved_install_variable: {obj.kind}/{obj.key} names"
                            f" {', '.join(names)}; give their values in given.variables",
                            error=True,
                        )
                    continue
                try:
                    async with self.db.begin_nested():
                        if kind in SUPPORTING_KINDS:
                            await self._publish_supporting(ctx, prepared)
                        else:
                            await self._publish_catalog(ctx, prepared)
                except AuthorizationError as exc:
                    self._problem(
                        obj.place(
                            Problem(
                                PERMISSION_REQUIRED,
                                "warning",
                                "",
                                f"{kind}/{obj.key} was not published for the tests:"
                                f" {exc.message}; a test publishes the package with the"
                                " rights of every kind it holds",
                                hint=", ".join((exc.details or {}).get("missing") or ())
                                or ", ".join((exc.details or {}).get("required") or ())
                                or None,
                            )
                        )
                    )
                    raise _Abort(
                        f"{PERMISSION_REQUIRED}: {kind}/{obj.key} cannot be published by the"
                        " caller",
                        error=True,
                    ) from exc
                except _Shape as shape:
                    for problem in shape.problems:
                        self._problem(obj.place(problem))
                    self._blocked(obj)
                except DomainError as exc:
                    self._problem(finding(obj, exc))
                    self._blocked(obj)

    def _problem(self, problem: Problem) -> None:
        if problem not in self.problems:
            self.problems.append(problem)

    def _needs(self, obj: PackageObject) -> bool:
        """Is ``obj`` the object of the test, or named by it?"""
        if obj.kind == _KIND[self.test.subject] and obj.key == self.test.object:
            return True
        mine = _object(self.package, _KIND[self.test.subject], self.test.object)
        return mine is not None and _names(mine, obj)

    def _blocked(self, obj: PackageObject) -> None:
        """An object that failed to publish: the test errs if its object needs it."""
        if self._needs(obj):
            raise _Abort(
                f"{obj.kind}/{obj.key} was not published: see the problems of {obj.file}",
                error=True,
            )

    async def _publish_supporting(self, ctx: AuthContext, obj: PackageObject) -> None:
        problems = await publish_supporting(self.db, ctx, self.settings, self.supporting, obj)
        if problems:
            raise _Shape(problems)

    async def _publish_catalog(self, ctx: AuthContext, obj: PackageObject) -> None:
        sent, problems = self.shapes[obj.kind](obj)
        if sent is None:
            raise _Shape(problems)
        kind, key = obj.kind, obj.key
        latest = await package_catalog.latest_of(self.db, ctx.tenant_id, kind, key)
        form = package_catalog.wanted_form(kind, sent, latest, self.settings_scope)
        if kind == "WorkRule":
            # Evaluated whatever the package says of its status (Z2).
            form["status"] = RuleStatus.ENABLED
        found = package_catalog.static_problems(kind, key, latest, form)
        if found:
            raise _Shape(found)
        action = "create" if latest is None else ("unchanged" if form == latest.spec else "update")
        if kind == "WorkRule" and latest is not None and action == "unchanged":
            action = "update"
        await package_catalog.publish(
            self.db,
            ctx,
            kind=kind,
            key=key,
            spec=form,
            latest=latest,
            action=action,
            deprecates=[],
            package=None,
            settings=self.settings_scope,
        )
        if kind == "WorkRule":
            await self._take_rule(ctx, key)
        elif kind == "Agent":
            await self._link_agent(key)

    async def _stand_settings(self) -> None:
        """The settings of the package on the stand, as an apply and a saving would leave them.

        CP-ADR-0081 §6: the rules of the package are linked to it, the schema
        its files declare is the active revision and ``given.settings`` —
        checked as ``PUT`` checks them — the saved values of a new version
        (none given: the defaults). All of it is rolled back with the test.
        """
        scope = self.settings_scope
        values = self.given.get("settings")
        if scope.package is None or scope.schema is None:
            if values is not None:
                raise _Abort(
                    "given.settings: settings_not_declared — the package declares no settings",
                    error=True,
                )
            return
        rules = self.package.of_kind("WorkRule")
        if values is None and not any("settings" in json.dumps(o.spec) for o in rules):
            return  # nothing reads them: the stand is left as it is
        values = dict(values or {})
        await self._check_settings(values, scope.schema)
        ctx, now, tenant = self.ctx, utcnow(), self.ctx.tenant_id
        for obj in rules:
            await self.db.execute(
                insert(PackageRecord)
                .values(
                    id=new_uuid(),
                    tenant_id=tenant,
                    kind="WorkRule",
                    key=obj.key,
                    package_key=scope.package,
                    applied_by=ctx.principal_id,
                    applied_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_package_objects_tenant_kind_key",
                    set_={"package_key": scope.package},
                )
            )
        await self.db.execute(
            update(PackageSettingsSchema)
            .where(
                PackageSettingsSchema.tenant_id == tenant,
                PackageSettingsSchema.package_key == scope.package,
                PackageSettingsSchema.active.is_(True),
            )
            .values(active=False)
        )
        latest = await self.db.scalar(
            select(func.max(PackageSettingsSchema.revision)).where(
                PackageSettingsSchema.tenant_id == tenant,
                PackageSettingsSchema.package_key == scope.package,
            )
        )
        revision = int(latest or 0) + 1
        self.db.add(
            PackageSettingsSchema(
                id=new_uuid(),
                tenant_id=tenant,
                package_key=scope.package,
                revision=revision,
                package_version=None,
                schema=dict(scope.schema),
                uischema=None,
                schema_hash=canonical_hash(scope.schema),
                active=True,
                plan_hash=self.trace,
                applied_by=ctx.principal_id,
                applied_at=now,
            )
        )
        row = await self.db.scalar(
            select(PackageSettings).where(
                PackageSettings.tenant_id == tenant,
                PackageSettings.package_key == scope.package,
            )
        )
        if row is None and not values:
            await self.db.flush()
            return  # version 0: the defaults
        version = (row.version if row is not None else 0) + 1
        if row is None:
            self.db.add(
                PackageSettings(
                    tenant_id=tenant,
                    package_key=scope.package,
                    values=values,
                    version=version,
                    schema_revision=revision,
                    updated_by=ctx.principal_id,
                    updated_at=now,
                )
            )
        else:
            row.values, row.version, row.schema_revision = values, version, revision
            row.updated_by, row.updated_at = ctx.principal_id, now
        self.db.add(
            PackageSettingsVersion(
                tenant_id=tenant,
                package_key=scope.package,
                version=version,
                values=values,
                schema_revision=revision,
                changed_paths=[],
                updated_by=ctx.principal_id,
                updated_at=now,
            )
        )
        await self.db.flush()

    async def _check_settings(self, values: dict[str, Any], schema: Mapping[str, Any]) -> None:
        """``given.settings`` as ``PUT`` checks them: the same codes, no value in the answer."""
        found = secret_findings(values)
        if found:
            raise _Abort(
                "given.settings: secret_material_rejected — settings hold no secrets",
                actual=found,
                error=True,
            )
        errors = validate(values, schema)
        if errors:
            raise _Abort(
                "given.settings: settings_invalid — the values do not match the settings schema",
                actual=errors,
                error=True,
            )
        missing = await unknown_refs(self.db, self.ctx.tenant_id, references(values, schema))
        if missing:
            raise _Abort(
                "given.settings: unknown_ref — a value references an object the organization"
                " does not have in use",
                actual=missing,
                error=True,
            )

    async def _take_rule(self, ctx: AuthContext, key: str) -> None:
        """A rule without an identity acts as the caller of the test, as for who enabled it.

        The rule is enabled from the start of the test's time: a rule the stand
        already has keeps the moment it was enabled there, which is later than
        the default clock of a test, and the worker skips what occurred before
        it (TASK-001197).
        """
        rule = await _live_rule(self.db, ctx.tenant_id, key)
        if rule.identity_agent_key is None and rule.authority_principal_id != ctx.principal_id:
            await rule_commands.set_rule_status(
                self.db, ctx, rule_id=rule.id, status=RuleStatus.DISABLED
            )
            await rule_commands.set_rule_status(
                self.db, ctx, rule_id=rule.id, status=RuleStatus.ENABLED
            )
        if rule.enabled_at is not None and rule.enabled_at > self.start:
            rule.enabled_at = self.start
            await self.db.flush()

    async def _link_agent(self, key: str) -> None:
        """An agent without a principal gets a temporary IAM identity with its rights."""
        agent = await self.db.scalar(
            select(Agent).where(Agent.tenant_id == self.ctx.tenant_id, Agent.key == key)
        )
        if agent is None or agent.principal_id is not None or agent.status != AgentStatus.ACTIVE:
            return
        linker = AuthContext(
            tenant_id=self.ctx.tenant_id,
            principal_id=self.ctx.principal_id,
            principal_kind=self.ctx.principal_kind,
            api_key_id=self.ctx.api_key_id,
            permissions=frozenset({Permission.AGENTS_STATUS_WRITE.value}),
            request_id=self.trace,
            correlation_id=self.trace,
            trace_run_id=self.trace,
        )
        await agent_commands.link_agent_identity(
            self.db,
            linker,
            key=key,
            issuer=_TEST_ISSUER,
            iam_tenant_id=self.ctx.tenant_id,
            iam_principal_id=new_uuid(),
            # The trial's own identity in its rolled-back transaction: nobody
            # presents a token of it, so the stand's issuer is not the one.
            trusted_issuer=_TEST_ISSUER,
        )

    # --- rule -------------------------------------------------------------------------

    async def _rule(self) -> None:
        rule = await _live_rule(self.db, self.ctx.tenant_id, self.test.object)
        self.rule = rule
        spec = self.given.get("task")
        if spec is not None:
            # Filed before the input, as work the rule finds on the stand.
            self.given_task = await self._file_task(
                spec, str(spec["type"]), rule.workspace_id or self.workspace_id, "given.task"
            )
        # What the setting wrote is not what the rule did.
        self.mark = await self._sequence()
        if "schedule" in self.given:
            await self._scheduled(rule)
            return
        event = await self._input(rule)
        payload = dict(event.payload or {})
        if not trigger_matches(rule.trigger, event.event_type, payload):
            raise _Abort(
                f"the input does not fire the rule: its trigger is {rule.trigger}",
                expected=rule.trigger,
                actual={"eventType": event.event_type, "kind": payload.get("kind")},
            )
        skipped = await self._not_delivered(rule, event)
        if skipped is not None:
            # The worker would not evaluate the rule on it: neither does the test.
            where = "/given/observation" if "observation" in self.given else "/given/event"
            self._problem(
                Problem(
                    INPUT_NOT_DELIVERED,
                    "warning",
                    where,
                    f"the rules worker does not evaluate {rule.key} on this input: {skipped}",
                    file=self.test.file,
                )
            )
            self.skipped = skipped
            return
        facts = rule_evaluations._event_facts(rule, event)
        token = rule_evaluations.observe_facts.set(self._observe)
        try:
            row = await rule_evaluations.evaluate_trigger(
                self.db,
                rule,
                facts,
                trigger_ref=facts.trigger["ref"],
                trigger_event_id=event.id,
                trace_run_id=self.trace,
            )
            assert row is not None  # a new event is never evaluated twice
            self.evaluation = row
            await self._settle()
        finally:
            rule_evaluations.observe_facts.reset(token)

    async def _scheduled(self, rule: WorkRule) -> None:
        """``given.schedule``: the slot of the rule falls due, and the worker's code runs it."""
        if rule.trigger.get("kind") != TriggerKind.SCHEDULE:
            raise _Abort(
                f"the input does not fire the rule: its trigger is {rule.trigger}",
                expected=rule.trigger,
                actual={"schedule": self.given["schedule"]},
            )
        at = self.given["schedule"].get("at")
        due = _time(str(at)) if at else self.start
        # Time does not move in a test (Z2): the clock is set at the slot, never back.
        self.trial.clock = max(self.trial.clock, due)
        rule.next_run_at = due
        await self.db.flush()
        token = rule_evaluations.observe_facts.set(self._observe)
        try:
            row = await rule_evaluations.run_schedule(
                self.db, rule_id=rule.id, trace_run_id=self.trace
            )
            if row is None:
                raise _Abort(f"the schedule of {rule.key} did not run its slot", error=True)
            self.evaluation = row
            await self._settle()
        finally:
            rule_evaluations.observe_facts.reset(token)

    async def _not_delivered(self, rule: WorkRule, event: Event) -> str | None:
        """Why the rules worker would skip ``event`` for ``rule``, as its loop does."""
        calls = await rule_evaluations._rule_calls(self.db, self.ctx.tenant_id, [event])
        if rule_evaluations._caused_by_rules(event, calls):
            return "it is a consequence of what a rule did"
        assert rule.enabled_at is not None
        if event.occurred_at < rule.enabled_at:
            return "it occurred before the rule was enabled"
        if rule_evaluations._outside_workspace(rule, event):
            return (
                f"it names workspace {(event.payload or {}).get('workspaceId')},"
                f" the rule is of workspace {rule.workspace_id}"
            )
        return None

    def _observe(self, where: str, facts: rule_evaluations.Facts, item: Any) -> None:
        assert self.rule is not None
        if where == "condition":
            self.seen.rule_branches |= branch_outcomes(
                self.rule.condition, facts.resolve, roots=BASE_ROOTS, at="/condition"
            )
        else:
            self.seen.rule_branches |= branch_outcomes(
                self.rule.action.get("where"),
                lambda path: facts.resolve(path, item),
                roots=frozenset({*BASE_ROOTS, ROOT_SKILL, ROOT_ITEM}),
                at="/action/where",
            )

    async def _input(self, rule: WorkRule) -> Event:
        """``given.observation`` by the command of observations, or ``given.event`` as recorded."""
        observation = self.given.get("observation")
        if observation is not None:
            observer = await self.people.create(
                "observer", frozenset({Permission.OBSERVATIONS_WRITE.value})
            )
            content = observation.get("content") or f"{observation['kind']} (package test)"
            async with self._given("given.observation"):
                recorded = await record_observation(
                    self.db,
                    observer,
                    kind=str(observation["kind"]),
                    content=str(content),
                    data=observation.get("data"),
                    source=observation.get("source"),
                    external_ref=observation.get("externalRef"),
                    workspace_id=rule.workspace_id,
                )
            event_id = recorded.event_id
        else:
            spec = self.given["event"]
            payload = dict(spec.get("payload") or {})
            if rule.workspace_id is not None:
                payload.setdefault("workspaceId", str(rule.workspace_id))
            if self.given_task is not None:
                # An event that names no task is about the task of the setting.
                payload.setdefault("taskId", str(self.given_task.id))
            entity_type, entity_id = _entity_of(str(spec["type"]), payload)
            await self._readable_tasks(str(spec["type"]), payload)
            try:
                recorded_event = await record_event(
                    self.db,
                    tenant_id=self.ctx.tenant_id,
                    event_type=str(spec["type"]),
                    entity_type=entity_type,
                    entity_id=entity_id,
                    actor_id=self.ctx.principal_id,
                    request_id=self.trace,
                    correlation_id=self.trace,
                    trace_run_id=self.trace,
                    payload=payload,
                )
            except (KeyError, ValueError) as exc:
                raise _Abort(
                    f"given.event: {spec['type']!r} is not an event type of the core catalog",
                    actual=str(spec["type"]),
                    error=True,
                ) from exc
            event_id = recorded_event.id
        event = await self.db.scalar(select(Event).where(Event.id == event_id))
        assert event is not None
        return event

    async def _readable_tasks(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """A task of the stand ``given.event`` names is one the caller may read.

        The rule reads the task of its trigger as the principal it acts as: an
        agent of the test reads with the local check (Z2), so without this the
        test would read, in the caller's name, a task the caller could not.
        Asked of an id, not of a row: the answer does not tell whether it exists.
        A caller in ``members`` mode is asked of the row too, as
        ``permits_task`` asks (CP-ADR-0082 V6): a task outside their sight
        and a missing one are the same "Task not found".
        """
        names = ["taskId"]
        if event_type.split(".", 1)[0] == "task":
            names.append("id")
        for name in names:
            raw = payload.get(name)
            task_id = _uuid(raw) if isinstance(raw, str) else None
            if task_id is None or str(task_id) in self.trial.written:
                continue
            async with self._given(f"given.event.payload.{name}"):
                await authorize(
                    self.ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(task_id))
                )
                if self.ctx.visible_workspaces is not None:
                    found = (
                        await self.db.execute(
                            select(Task.workspace_id).where(
                                Task.id == task_id, Task.tenant_id == self.ctx.tenant_id
                            )
                        )
                    ).first()
                    if found is None or not self.ctx.sees_workspace(found.workspace_id):
                        raise NotFoundError("Task not found", details={"taskId": str(task_id)})

    async def _rule_outcome(self) -> None:
        """The result of the evaluation and the outcomes it reached, for the coverage."""
        row = self.evaluation
        if row is None:
            return
        await self._reload(row)
        if row.status in (EvaluationStatus.MATCHED, EvaluationStatus.NOT_MATCHED):
            self.seen.rule_outcomes.add(str(row.status))
        if row.skill_invocation_id is not None:
            invocation = await self.db.get(SkillInvocation, row.skill_invocation_id)
            if invocation is not None and invocation.status == SkillInvocationStatus.SUCCEEDED:
                self.seen.rule_outcomes.add("interpretation:answered")
            elif invocation is not None and invocation.status not in LIVE_STATUSES:
                self.seen.rule_outcomes.add("interpretation:failed")

    # --- task type ---------------------------------------------------------------------

    async def _task_type(self) -> None:
        await self.people.given(self.given.get("principals") or {})
        task = await self._file_task(
            self.given.get("task") or {}, self.test.object, self.workspace_id, "given.task"
        )
        filer = self.people.acting_caller
        for index, artifact in enumerate(self.given.get("artifacts") or ()):
            self._in_time()
            ref = None
            if artifact.get("content") is not None:
                content, media_type = str(artifact["content"]), str(artifact["mediaType"])
                ref = await self._upload(filer, content, media_type)
            async with self._given(f"given.artifacts[{index}]"):
                await create_artifact(
                    self.db,
                    filer,
                    type_=str(artifact["type"]),
                    name=str(artifact.get("key") or f"{artifact['type']}-{index + 1}"),
                    task_ref=str(task.id),
                    content_ref_value=ref,
                    metadata=artifact.get("metadata"),
                )
        self.task = task
        # What filing the task wrote is the test's setting, not what the type did.
        self.mark = await self._sequence()
        await self._settle()

    async def _current_task(self) -> Task:
        assert self.task is not None
        await self._reload(self.task)
        return self.task

    async def _type(self) -> TaskType:
        task = await self._current_task()
        task_type = await self.db.get(TaskType, task.type_id)
        assert task_type is not None
        return task_type

    async def _approve(self, index: int, spec: Mapping[str, Any]) -> list[process_trial.Failure]:
        """Decide the pending gate; ``expectRefused`` — the core must refuse the decider.

        Whoever decides needs the right to (CP-ADR-0061, amendment 2026-10-01):
        a gate addressed to a role is decided by a holder of it, so a test
        states the refusal of somebody else as ``expectRefused: not_eligible``.
        A refusal expected is not decided, and the test goes on.
        """
        gate = str(spec.get("gate") or DEFAULT_GATE)
        if gate != DEFAULT_GATE:
            raise _Abort(
                f"approve.gate {gate!r}: the core has only the {DEFAULT_GATE!r} gate",
                expected=DEFAULT_GATE,
                actual=gate,
            )
        approve = spec["decision"] == "approved"
        task = await self._current_task()
        approval = await self.db.scalar(
            select(Approval)
            .where(
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at, Approval.id)
            .limit(1)
        )
        by = spec.get("by")
        wanted = spec.get("expectRefused")
        if approval is None and wanted:
            # The trial would open a gate for the decider: nobody to refuse.
            return [
                process_trial.Failure(
                    index, "no gate is pending: a refusal cannot be checked", wanted, None
                )
            ]
        if approval is None:
            decider = await self.people.named(str(by or "approver"))
            approval = await request_approval(
                self.db,
                decider,
                task_ref=str(task.id),
                workspace_id=task.workspace_id,
                assigned_principal_id=decider.principal_id,
                comment="a gate of a package test",
                gate=True,
            )
        else:
            decider = await self._decider(approval, by)
        if approve and not wanted:
            await self._preconditions(approval, decider)
        try:
            async with self.db.begin_nested():
                decided = await decide_approval(
                    self.db,
                    decider,
                    approval_id=approval.id,
                    approve=approve,
                    comment=spec.get("comment"),
                )
        except DomainError as exc:
            if wanted == exc.code:
                return []
            if wanted:
                return [
                    process_trial.Failure(
                        index,
                        f"the core refused the decision: {exc.code}",
                        wanted,
                        exc.code,
                    )
                ]
            raise _Abort(
                f"the core refused the decision: {exc.code}",
                expected=spec["decision"],
                actual={"code": exc.code, "message": exc.message},
            ) from exc
        failures: list[process_trial.Failure] = []
        if wanted:
            who = f" of {by}" if by else ""
            failures.append(
                process_trial.Failure(
                    index, f"the decision{who} was taken, a refusal was expected", wanted, None
                )
            )
        self.seen.outcomes.add(f"{gate}/{spec['decision']}")
        if decided.outcome_status in OUTCOME_LIVE:
            await execute_outcome(
                self.db,
                tenant_id=self.ctx.tenant_id,
                approval_id=decided.id,
                trace_run_id=self.trace,
            )
        return failures

    async def _decider(self, approval: Approval, by: Any) -> AuthContext:
        """Who decides a gate the type or the verification opened.

        ``by`` decides with the roles ``given.principals`` gave it: whether it
        may is the core's answer. Without it, the assigned principal decides,
        or a principal of the test given the required role.
        """
        if by:
            return await self.people.named(str(by))
        if approval.assigned_principal_id is not None:
            return await self.people.as_principal(approval.assigned_principal_id)
        decider = await self.people.named("approver")
        if approval.required_role_id is not None:
            await self.people.grant(decider, approval.required_role_id)
        return decider

    async def _preconditions(self, approval: Approval, decider: AuthContext) -> None:
        """Which preconditions of ``approved`` hold now: the coverage of each."""
        task_type = await self._type()
        schema = parse_approval_schema(dict(task_type.approval_schema or {}))
        declared = schema.preconditions_for(DEFAULT_GATE, "approved")
        if not declared:
            return
        from control_plane.application.commands.approval_preconditions import (
            require_preconditions,
        )

        refused: set[int] = set()
        try:
            async with self.db.begin_nested():
                await require_preconditions(self.db, decider, approval)
        except DomainError as exc:
            if exc.code != APPROVAL_PRECONDITION_FAILED:
                return
            refused = {int(item["index"]) for item in (exc.details or {}).get("failed") or ()}
        for index in range(len(declared)):
            verdict = "refused" if index in refused else "held"
            self.seen.preconditions.add(f"{DEFAULT_GATE}/preconditions/approved/{index}:{verdict}")

    async def _open_attempt(self) -> TaskVerification | None:
        task = await self._current_task()
        attempt: TaskVerification | None = await self.db.scalar(
            select(TaskVerification)
            .where(
                TaskVerification.task_id == task.id,
                TaskVerification.status.in_(OPEN_STATUSES),
            )
            .execution_options(populate_existing=True)
        )
        return attempt

    async def _verify(self, spec: Mapping[str, Any]) -> None:
        """The result of the current check, recorded the way its verifier records it."""
        attempt = await self._open_attempt()
        wanted, passed = str(spec["check"]), spec["result"] == "passed"
        if attempt is None:
            raise _Abort(f"verify {wanted!r}: the task has no open verification attempt")
        check = attempt.checks[attempt.cursor] if attempt.cursor < len(attempt.checks) else None
        if check is None or check["key"] != wanted:
            raise _Abort(
                f"verify {wanted!r}: the attempt is at another check",
                expected=wanted,
                actual=check["key"] if check else None,
            )
        kind, check_spec = check["kind"], check.get("spec") or {}
        task = await self._current_task()
        if kind in (CheckKind.HUMAN, CheckKind.LLM_JUDGE):
            if attempt.approval_id is None:
                raise _Abort(f"verify {wanted!r}: the check has asked nobody for a decision")
            approval = await self.db.get(Approval, attempt.approval_id)
            assert approval is not None
            decider = await self._decider(approval, None)
            await decide_approval(
                self.db,
                decider,
                approval_id=approval.id,
                approve=passed,
                comment=None if passed else f"verify {wanted}: failed",
            )
        elif kind == CheckKind.DETERMINISTIC and check_spec.get("skill"):
            if attempt.skill_invocation_id is None:
                raise _Abort(f"verify {wanted!r}: the check has not called its skill")
            invocation = await self.db.get(SkillInvocation, attempt.skill_invocation_id)
            assert invocation is not None
            if invocation.status in LIVE_STATUSES:
                output = spec.get("output")
                if output is None:
                    output = dict(check_spec.get("expect") or {}) if passed else {}
                mock: dict[str, Any] = (
                    {"output": output}
                    if passed
                    else {"error": {"type": "verify_failed", "detail": f"verify {wanted}: failed"}}
                )
                await self._answer_with(invocation, mock)
        elif kind == CheckKind.DETERMINISTIC and check_spec.get("artifact"):
            raise _Abort(
                f"verify {wanted!r}: a check of an artifact is decided by the task's artifacts",
                expected="given.artifacts",
            )
        elif passed:
            await update_task(
                self.db,
                self.people.acting_caller,
                task_ref=str(task.id),
                expected_version=task.version,
                evidence=[
                    *(task.evidence or []),
                    {
                        "kind": EvidenceKind.EXTERNAL.value,
                        "externalRef": {"system": "package-test", "id": f"{wanted}:{new_uuid()}"},
                        "check": wanted,
                    },
                ],
            )
        else:
            # No fact comes: the check ends without one, as when its wait runs out.
            await execute_verification(
                self.db,
                verification_id=attempt.id,
                trace_run_id=self.trace,
                timing=Timing(external_timeout=timedelta(0)),
            )

    async def _complete(self, spec: Mapping[str, Any]) -> None:
        task = await self._current_task()
        completer = self.people.acting_caller
        if task.assignee_id is not None and task.assignee_id in self.people.names:
            completer = await self.people.as_principal(task.assignee_id)
        output = spec.get("output")
        if output:
            # A task's result lives in its fields: checked by the type's fieldSchema.
            task = await update_task(
                self.db,
                completer,
                task_ref=str(task.id),
                expected_version=task.version,
                custom_fields={**(task.custom_fields or {}), **output},
            )
        await complete_task(
            self.db, completer, task_ref=str(task.id), expected_version=task.version
        )

    # --- until nothing moves -------------------------------------------------------------

    async def _settle(self) -> None:
        """Run what waits — outcomes, attempts, the evaluation, mocked calls — until it rests."""
        for _ in range(MAX_ROUNDS):
            self._in_time()
            before = await self._fingerprint()
            await self._answer_calls()
            if self.evaluation is not None:
                await self._reload(self.evaluation)
                if self.evaluation.status == EvaluationStatus.WAITING:
                    token = rule_evaluations.observe_facts.set(self._observe)
                    try:
                        await rule_evaluations.resume_evaluation(
                            self.db, evaluation_id=self.evaluation.id, trace_run_id=self.trace
                        )
                    finally:
                        rule_evaluations.observe_facts.reset(token)
            if self.task is not None:
                for approval_id in await self._live_outcomes():
                    await execute_outcome(
                        self.db,
                        tenant_id=self.ctx.tenant_id,
                        approval_id=approval_id,
                        trace_run_id=self.trace,
                    )
                attempt = await self._open_attempt()
                if attempt is not None:
                    await execute_verification(
                        self.db, verification_id=attempt.id, trace_run_id=self.trace
                    )
            if await self._fingerprint() == before:
                break
        await self._record_coverage()

    async def _fingerprint(self) -> tuple[Any, ...]:
        count = await self.db.scalar(
            select(func.count()).select_from(Event).where(Event.tx_id == self.tx_id)
        )
        attempt = await self._open_attempt() if self.task is not None else None
        return (
            count,
            attempt.status if attempt else None,
            attempt.cursor if attempt else None,
        )

    async def _live_outcomes(self) -> list[uuid.UUID]:
        assert self.task is not None
        rows = await self.db.scalars(
            select(Approval.id).where(
                Approval.tenant_id == self.ctx.tenant_id,
                Approval.id.in_(self._written("approval")),
                Approval.outcome_status.in_(OUTCOME_LIVE),
            )
        )
        return list(rows.all())

    def _written(self, entity_type: str) -> Any:
        """The ids of the entities of ``entity_type`` this transaction wrote an event about."""
        return select(Event.entity_id).where(
            Event.tx_id == self.tx_id, Event.entity_type == entity_type
        )

    async def _answer_calls(self) -> None:
        rows = await self.db.scalars(
            select(SkillInvocation)
            .where(
                SkillInvocation.tenant_id == self.ctx.tenant_id,
                SkillInvocation.id.in_(self._written("skill_invocation")),
                SkillInvocation.status == SkillInvocationStatus.PENDING,
            )
            .order_by(SkillInvocation.created_at, SkillInvocation.id)
            .execution_options(populate_existing=True)
        )
        for invocation in rows.all():
            if invocation.id in self.answered:
                continue
            skill = await self.db.get(Skill, invocation.skill_id)
            assert skill is not None
            ref = f"{skill.name}@{skill.version}"
            answers = (self.mocks.get("skills") or {}).get(ref)
            if not answers:
                continue
            mock = self._pick(ref, answers, dict(invocation.inputs or {}))
            if mock is None or mock.get("timeout") is True:
                self.answered.add(invocation.id)
                continue
            await self._answer_with(invocation, mock, skill=skill)

    def _pick(
        self, ref: str, answers: Sequence[Mapping[str, Any]], inputs: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        """The next answer in order among those whose ``when`` holds; the last one repeats."""
        try:
            fitting = [mock for mock in answers if process_trial._when(mock.get("when"), inputs)]
        except process_trial._Abort as exc:
            raise _Abort(exc.message, exc.expected, exc.actual) from exc
        if not fitting:
            return None
        used = self.used.get(ref, 0)
        self.used[ref] = used + 1
        return fitting[min(used, len(fitting) - 1)]

    async def _answer_with(
        self,
        invocation: SkillInvocation,
        mock: Mapping[str, Any],
        *,
        skill: Skill | None = None,
    ) -> None:
        """The mock executor takes the call, as a claim does, and reports the answer."""
        skill = skill or await self.db.get(Skill, invocation.skill_id)
        assert skill is not None
        ref = f"{skill.name}@{skill.version}"
        self.answered.add(invocation.id)
        if mock.get("error") is None:
            output = mock.get("output")
            outputs = (skill.contract or {}).get("outputs") or skill.output_schema
            problems = process_trial._schema_errors(outputs, output)
            if problems or not isinstance(output, dict):
                raise _Abort(
                    f"the mock of skill {ref} does not match the skill's output schema: "
                    + ("; ".join(problems) or "the output is not an object"),
                    expected=outputs,
                    actual=output,
                )
        executor = await self.people.executor()
        now = utcnow()
        invocation.status = SkillInvocationStatus.RUNNING
        invocation.attempt += 1
        invocation.fencing_token += 1
        invocation.executor_principal_id = executor.principal_id
        invocation.executor_session_id = None
        invocation.attempt_started_at = now
        invocation.lease_expires_at = now + timedelta(days=1)
        invocation.heartbeat_at = now
        invocation.started_at = invocation.started_at or now
        invocation.updated_at = now
        await self.db.flush()
        if mock.get("error") is not None:
            error = mock["error"]
            await fail_skill_invocation(
                self.db,
                executor,
                invocation_id=invocation.id,
                fencing_token=invocation.fencing_token,
                code=str(error.get("type")),
                message=str(error.get("detail") or error.get("type")),
                retryable=False,
                details={"status": error["status"]} if error.get("status") else None,
            )
        else:
            await complete_skill_invocation(
                self.db,
                executor,
                invocation_id=invocation.id,
                fencing_token=invocation.fencing_token,
                output=dict(mock["output"]),
            )

    async def _unmocked_calls(self) -> None:
        """A rule left waiting by a call the test has no mock for: ``unmocked_skill_call``.

        A mock that answers ``timeout``, or none of whose ``when`` holds, is
        the test's intent, not a gap.
        """
        row = self.evaluation
        if row is None:
            return
        await self._reload(row)
        if row.status != EvaluationStatus.WAITING or row.skill_invocation_id is None:
            return
        invocation = await self.db.get(SkillInvocation, row.skill_invocation_id)
        if invocation is None or invocation.status not in LIVE_STATUSES:
            return
        skill = await self.db.get(Skill, invocation.skill_id)
        assert skill is not None
        ref = f"{skill.name}@{skill.version}"
        if (self.mocks.get("skills") or {}).get(ref):
            return
        self._problem(
            Problem(
                UNMOCKED_SKILL_CALL,
                "warning",
                "/mocks/skills",
                f"no mock answers the call of skill {ref}: the rule stays"
                f" {EvaluationStatus.WAITING.value}",
                hint=f"add mocks.skills.{ref}",
                file=self.test.file,
            )
        )

    # --- what the test sees ------------------------------------------------------------------

    async def _sequence(self) -> int:
        value = await self.db.scalar(
            select(func.max(Event.sequence)).where(Event.tx_id == self.tx_id)
        )
        return int(value or 0)

    async def _events(self, event_type: str, since: int) -> list[Event]:
        rows = await self.db.scalars(
            select(Event)
            .where(
                Event.tx_id == self.tx_id,
                Event.event_type == event_type,
                Event.sequence > since,
            )
            .order_by(Event.sequence)
        )
        return list(rows.all())

    async def _work(self, since: int) -> list[dict[str, Any]]:
        """The work filed since ``since``, as a test names it."""
        out = []
        for event in await self._events("task.created", since):
            task = await self.db.get(Task, event.entity_id, populate_existing=True)
            if task is None:
                continue
            task_type = await self.db.get(TaskType, task.type_id)
            assignee = await self.people.label(task.assignee_id)
            if assignee is None:
                slug = await self.db.scalar(
                    select(Role.slug)
                    .join(TaskRequirement, TaskRequirement.role_id == Role.id)
                    .where(TaskRequirement.task_id == task.id)
                    .limit(1)
                )
                assignee = f"role:{slug}" if slug else None
            relations: dict[str, Any] = {}
            for relation_type, target in (
                await self.db.execute(
                    select(TaskRelation.relation_type, Task.public_id)
                    .join(Task, Task.id == TaskRelation.to_task_id)
                    .where(TaskRelation.from_task_id == task.id)
                    .order_by(TaskRelation.created_at)
                )
            ).all():
                relations.setdefault(_camel(relation_type), []).append(target)
            out.append(
                {
                    "type": task_type.key if task_type else None,
                    "title": task.title,
                    "assignee": assignee,
                    "customFields": dict(task.custom_fields or {}),
                    "relation": {
                        k: (v[0] if len(v) == 1 else v) for k, v in sorted(relations.items())
                    },
                    "publicId": task.public_id,
                }
            )
        return out

    async def _calls(self, since: int) -> list[dict[str, Any]]:
        out = []
        for event in await self._events("skill.invocation_requested", since):
            invocation = await self.db.get(SkillInvocation, event.entity_id)
            payload = event.payload or {}
            out.append(
                {
                    "skill": f"{payload.get('skill')}@{payload.get('version')}",
                    "inputs": dict(invocation.inputs or {}) if invocation else {},
                }
            )
        return out

    async def _expect(self, index: int, spec: Mapping[str, Any]) -> list[process_trial.Failure]:
        failures: list[process_trial.Failure] = []
        # A rule has one evaluation: every expect looks at all of it (Z1).
        since = self.mark
        if "result" in spec:
            row = self.evaluation
            if row is not None:
                await self._reload(row)
            actual = str(row.status) if row is not None else None
            if actual != spec["result"]:
                message = (
                    f"the rule is not evaluated: {self.skipped}"
                    if self.skipped
                    else f"the rule's evaluation ended {actual}"
                )
                failures.append(
                    process_trial.Failure(
                        index,
                        message,
                        spec["result"],
                        {"result": actual, "error": row.error if row is not None else None},
                    )
                )
        if "ensureWork" in spec:
            work = await self._work(since)
            if not _matches(spec["ensureWork"], work):
                failures.append(
                    process_trial.Failure(index, "the work filed differs", spec["ensureWork"], work)
                )
        if "invokeSkill" in spec:
            calls = await self._calls(since)
            if not _matches(spec["invokeSkill"], calls):
                failures.append(
                    process_trial.Failure(
                        index, "the skill calls differ", spec["invokeSkill"], calls
                    )
                )
        if spec.get("noSideEffects") and self.trial.outgoing:
            failures.append(
                process_trial.Failure(
                    index,
                    "the code tried to reach outside the transaction",
                    [],
                    sorted(set(self.trial.outgoing)),
                )
            )
        if "status" in spec:
            task = await self._current_task()
            actual_status = {"key": task.status, "category": task.system_status_category}
            wanted = dict(spec["status"])
            if any(actual_status.get(k) != v for k, v in wanted.items()):
                failures.append(
                    process_trial.Failure(index, "the task's status differs", wanted, actual_status)
                )
        if "comments" in spec:
            task = await self._current_task()
            bodies = list(
                (
                    await self.db.scalars(
                        select(TaskComment.body)
                        .where(TaskComment.task_id == task.id)
                        .order_by(TaskComment.created_at, TaskComment.id)
                    )
                ).all()
            )
            for wanted_text in spec["comments"]:
                if not any(str(wanted_text) in body for body in bodies):
                    failures.append(
                        process_trial.Failure(
                            index, f"no comment says {wanted_text!r}", wanted_text, bodies
                        )
                    )
        if self.test.subject == SUBJECT_TASK_TYPE:
            self.mark = await self._sequence()
        return failures

    # --- coverage --------------------------------------------------------------------------

    async def _record_coverage(self) -> None:
        if self.test.subject == SUBJECT_RULE:
            await self._rule_outcome()
            return
        if self.task is None:
            return
        task_type = await self._type()
        schema = parse_approval_schema(dict(task_type.approval_schema or {}))
        decided = (
            await self.db.scalars(
                select(Approval).where(
                    Approval.task_id == self.task.id,
                    Approval.gate.is_(True),
                    Approval.status.in_((ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)),
                )
            )
        ).all()
        for approval in decided:
            outcome = "approved" if approval.status == ApprovalStatus.APPROVED else "rejected"
            self.seen.outcomes.add(f"{DEFAULT_GATE}/{outcome}")
            actions = schema.actions_for(DEFAULT_GATE, outcome)
            rows = (
                await self.db.scalars(
                    select(ApprovalOutcomeAction).where(
                        ApprovalOutcomeAction.approval_id == approval.id,
                        ApprovalOutcomeAction.status == "executed",
                    )
                )
            ).all()
            for row in rows:
                if row.action_index >= len(actions) or (row.result or {}).get("skipped"):
                    continue
                action = actions[row.action_index]
                if action.reacts_to is not None:
                    self.seen.outcomes.add(
                        f"{DEFAULT_GATE}/{outcome}/{action.reacts_to}/{action.when}"
                    )
        work = await self.db.scalar(
            select(TaskCompletionWork).where(TaskCompletionWork.task_id == self.task.id)
        )
        if work is not None:
            for item in work.actions or []:
                if item.get("status") == "executed":
                    self.seen.completion.add(f"completion/{item['index']}")
        attempts = (
            await self.db.scalars(
                select(TaskVerification).where(TaskVerification.task_id == self.task.id)
            )
        ).all()
        for attempt in attempts:
            for result in attempt.results or []:
                if result.get("status") in ("passed", "failed"):
                    self.seen.acceptance.add(f"acceptance/{result['key']}:{result['status']}")


class _Shape(Exception):
    """The shape of an object refused by the request model of its route."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("shape")
        self.problems = problems


async def publish_supporting(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    supporting: Mapping[str, SupportingShape],
    obj: PackageObject,
) -> list[Problem]:
    """An artifact type, a role or a skill of the package, by its command, if the tenant lacks it.

    What a type, an agent or a rule of the package names is then there for
    the command that publishes them: the test of a rule or a task type, the
    trial of a plan (TASK-001197). A key the tenant has is left as it is.
    Returns the findings of the object's shape (nothing is published then);
    a refusal of the command is raised.
    """
    kwargs, problems = supporting[obj.kind](obj)
    if kwargs is None:
        return problems
    tenant = ctx.tenant_id
    if obj.kind == "ArtifactType":
        exists = await db.scalar(
            select(ArtifactType.id).where(
                ArtifactType.tenant_id == tenant, ArtifactType.key == obj.key
            )
        )
        if exists is None:
            await artifact_type_commands.create_artifact_type_version(
                db, ctx, global_max_bytes=settings.artifact_max_bytes, **kwargs
            )
    elif obj.kind == "Role":
        exists = await db.scalar(
            select(Role.id).where(Role.tenant_id == tenant, Role.slug == obj.key)
        )
        if exists is None:
            await org_commands.create_role(db, ctx, **kwargs)
    else:
        exists = await db.scalar(
            select(Skill.id).where(
                Skill.tenant_id == tenant,
                Skill.name == kwargs["name"],
                Skill.version == kwargs["version"],
            )
        )
        if exists is None:
            await org_commands.register_skill(db, ctx, **kwargs)
    return []


# --- coverage of the package ---------------------------------------------------------------


_KIND = {SUBJECT_RULE: "WorkRule", SUBJECT_TASK_TYPE: "TaskType"}


async def _live_rule(db: AsyncSession, tenant_id: uuid.UUID, key: str) -> WorkRule:
    rule = await db.scalar(
        select(WorkRule)
        .where(
            WorkRule.tenant_id == tenant_id,
            WorkRule.key == key,
            WorkRule.status != RuleStatus.ARCHIVED,
        )
        .execution_options(populate_existing=True)
    )
    if rule is None:
        raise _Abort(f"the rule {key!r} was not published", error=True)
    return rule


def _object(package: ParsedPackage, kind: str, key: str) -> PackageObject | None:
    return next((obj for obj in package.of_kind(kind) if obj.key == key), None)


def _names(obj: PackageObject, other: PackageObject) -> bool:
    """Does ``obj`` name ``other`` (by its key, or a skill by ``name@``)?"""
    body = json.dumps(obj.spec, ensure_ascii=False)
    key = re.escape(other.key)
    return re.search(rf"(?<![\w.-]){key}(?![\w.-])", body) is not None


def rule_branches(obj: PackageObject) -> list[str]:
    spec = obj.spec
    action: dict[str, Any] = spec["action"] if isinstance(spec.get("action"), dict) else {}
    return [
        *expression_branches(spec.get("condition"), "/condition"),
        *expression_branches(action.get("where"), "/action/where"),
    ]


def rule_outcomes(obj: PackageObject) -> list[str]:
    outcomes = [EvaluationStatus.MATCHED.value, EvaluationStatus.NOT_MATCHED.value]
    if obj.spec.get("interpretation"):
        outcomes += ["interpretation:answered", "interpretation:failed"]
    return outcomes


def _schema(obj: PackageObject) -> ApprovalSchema | None:
    try:
        return parse_approval_schema(copy.deepcopy(obj.spec.get("approvalSchema") or {}))
    except DomainError:
        return None


def type_outcomes(obj: PackageObject) -> list[str]:
    schema = _schema(obj)
    if schema is None:
        return []
    ids = []
    for gate, outcomes in schema.gates.items():
        for outcome in OUTCOMES:
            if outcome not in outcomes:
                continue
            ids.append(f"{gate}/{outcome}")
            for index, action in enumerate(outcomes[outcome]):
                if action.name != INVOKE_SKILL:
                    continue
                for when, branch in (
                    (ON_SUCCESS, action.on_success),
                    (ON_FAILURE, action.on_failure),
                ):
                    if branch:
                        ids.append(f"{gate}/{outcome}/{index}/{when}")
    return ids


def type_preconditions(obj: PackageObject) -> list[str]:
    schema = _schema(obj)
    if schema is None:
        return []
    return [
        f"{gate}/preconditions/{outcome}/{index}:{verdict}"
        for gate, by_outcome in schema.preconditions.items()
        for outcome, items in by_outcome.items()
        for index in range(len(items))
        for verdict in ("held", "refused")
    ]


def type_completion(obj: PackageObject) -> list[str]:
    try:
        schema = schema_of(copy.deepcopy(obj.spec.get("completionSchema") or {}))
    except DomainError:
        return []
    return [f"completion/{index}" for index in range(len(schema.actions))]


def type_acceptance(obj: PackageObject) -> list[str]:
    checks = obj.spec.get("acceptance") or []
    return [
        f"acceptance/{check['key']}:{verdict}"
        for check in checks
        if isinstance(check, dict) and check.get("key")
        for verdict in ("passed", "failed")
    ]


def rule_coverage(package: ParsedPackage, runs: Sequence[_Run]) -> list[RuleCoverage]:
    out = []
    for obj in sorted(package.of_kind("WorkRule"), key=lambda o: o.key):
        mine = [r for r in runs if r.test.subject == SUBJECT_RULE and r.test.object == obj.key]
        out.append(
            RuleCoverage(
                rule=obj.key,
                tests=len(mine),
                branches=process_trial._counter(
                    rule_branches(obj), set().union(*(r.seen.rule_branches for r in mine))
                ),
                outcomes=process_trial._counter(
                    rule_outcomes(obj), set().union(*(r.seen.rule_outcomes for r in mine))
                ),
            )
        )
    return out


def task_type_coverage(
    package: ParsedPackage, runs: Sequence[_Run], versions: Mapping[str, int]
) -> list[TaskTypeCoverage]:
    out = []
    for obj in sorted(package.of_kind("TaskType"), key=lambda o: o.key):
        outcomes, preconditions = type_outcomes(obj), type_preconditions(obj)
        completion, acceptance = type_completion(obj), type_acceptance(obj)
        if not (outcomes or preconditions or completion or acceptance):
            continue
        mine = [r for r in runs if r.test.subject == SUBJECT_TASK_TYPE and r.test.object == obj.key]
        seen = _Seen()
        for run in mine:
            seen.outcomes |= run.seen.outcomes
            seen.preconditions |= run.seen.preconditions
            seen.completion |= run.seen.completion
            seen.acceptance |= run.seen.acceptance
        out.append(
            TaskTypeCoverage(
                task_type=obj.key,
                version=versions.get(obj.key, 1),
                tests=len(mine),
                outcomes=process_trial._counter(outcomes, seen.outcomes),
                preconditions=process_trial._counter(preconditions, seen.preconditions),
                completion=process_trial._counter(completion, seen.completion),
                acceptance=process_trial._counter(acceptance, seen.acceptance),
            )
        )
    return out


# --- helpers --------------------------------------------------------------------------------


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _entity_of(event_type: str, payload: Mapping[str, Any]) -> tuple[str, uuid.UUID]:
    """The entity a journal event of a test is about: named by its type, id from the payload."""
    entity = event_type.split(".", 1)[0]
    camel = _camel(entity)
    for name in (f"{camel}Id", "id"):
        raw = payload.get(name)
        if isinstance(raw, str):
            try:
                return entity, uuid.UUID(raw)
            except ValueError:
                continue
    return entity, new_uuid()


def _matches(expected: Sequence[Any], actual: Sequence[Mapping[str, Any]]) -> bool:
    """As many items, each expected item a part of a distinct actual one."""
    if len(expected) != len(actual):
        return False
    left = list(actual)
    for item in expected:
        found = next((a for a in left if process_trial._contains(item, a)), None)
        if found is None:
            return False
        left.remove(found)
    return True


__all__ = [
    "PRINCIPAL_PERMISSIONS",
    "SUPPORTING_KINDS",
    "RuleCoverage",
    "SubjectReport",
    "SubjectResult",
    "TaskTypeCoverage",
    "run_subject_tests",
    "static_problems",
    "substitute",
]
