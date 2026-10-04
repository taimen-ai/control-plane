"""Package test, plan and apply in the core (CP-ADR-0074 §10, §11).

The body is the package as its files: the core parses YAML 1.2 itself so a
finding names the file and line. Test and plan write nothing; apply performs
exactly the plan whose hash it is given, or refuses ``409 plan_stale``. The
shape of an object the plan takes from the request model of its kind's route
(:data:`SHAPES`): a package says what ``POST /task-types``, ``POST /agents``,
``POST /rules`` and ``POST /calendars`` would be sent. ``packages:record``
links the objects the installer applied through their own routes to their
package (:mod:`control_plane.application.commands.package_links`).
``packages:test`` — :mod:`control_plane.application.commands.package_test`
(P013); plan and apply — :mod:`control_plane.application.commands.package_plan`
(P015).
"""

import uuid
from typing import Any, cast

import pydantic
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.calendars import calendar_request, spec_as_sent
from control_plane.api.v1.principals import forget_binding_cache
from control_plane.api.v1.processes import RESPONSES
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    AgentPublishRequest,
    ApiModel,
    ArtifactTypeCreateRequest,
    PackageApplyOut,
    PackageApplyRequest,
    PackagePlanOut,
    PackagePlanRequest,
    PackageRecordOut,
    PackageRecordRequest,
    PackageTestOut,
    PackageTestRequest,
    RoleCreateRequest,
    RuleCreateRequest,
    SkillRegisterRequest,
    TaskTypeCreateRequest,
    work_document,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import package_links, package_plan
from control_plane.application.commands.package_catalog import SpecShape
from control_plane.application.commands.package_test import run_package_tests
from control_plane.application.commands.package_trials import SupportingShape
from control_plane.domain.calendar import Calendar, CalendarError
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.package_source import PackageObject
from control_plane.domain.process_definition import Problem
from control_plane.infrastructure.context_provider import GraphProvider

router = APIRouter(tags=["packages"])


def calendar_spec(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A package object of kind Calendar as ``POST /calendars`` takes its spec, or its findings."""
    try:
        payload = calendar_request({"kind": obj.kind, "key": obj.key, "spec": obj.spec})
    except ValidationError as exc:
        errors = (exc.details or {}).get("errors") or [{"path": "/spec", "message": exc.message}]
        return None, [
            Problem("invalid_calendar", "error", str(e["path"]), str(e["message"])) for e in errors
        ]
    spec = spec_as_sent(payload)
    try:
        Calendar.from_spec(spec)
    except CalendarError as exc:
        return None, [Problem(exc.code, "error", exc.path or "/spec", exc.message)]
    return spec, []


def _shaped(
    model: type[ApiModel], body: dict[str, Any], code: str, prefix: str
) -> tuple[ApiModel | None, list[Problem]]:
    """``body`` validated by the request model of a route, or the findings of its shape."""
    try:
        return model.model_validate(body), []
    except pydantic.ValidationError as exc:
        problems = []
        for error in exc.errors():
            loc = [str(part) for part in error["loc"]]
            path = "/key" if loc[:1] == ["key"] else prefix + "".join("/" + p for p in loc)
            problems.append(Problem(code, "error", path, error["msg"]))
        return None, problems


def task_type_spec(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A TaskType as ``POST /task-types`` takes it: the fields the file sets."""
    payload, problems = _shaped(
        TaskTypeCreateRequest, {"key": obj.key, **obj.spec}, "invalid_task_type", "/spec"
    )
    if payload is None:
        return None, problems
    sent = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
    sent.pop("key", None)
    if "acceptance" in sent:
        sent["acceptance"] = work_document(cast(TaskTypeCreateRequest, payload).acceptance)
    return sent, []


def agent_spec(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """An Agent as ``POST /agents`` takes it: the spec as sent, without defaults."""
    payload, problems = _shaped(
        AgentPublishRequest, {"key": obj.key, "spec": obj.spec}, "invalid_agent", ""
    )
    if payload is None:
        return None, problems
    spec = cast(AgentPublishRequest, payload).spec
    return spec.model_dump(mode="json", by_alias=True, exclude_unset=True), []


def rule_spec(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A WorkRule as ``POST /rules`` takes it (without ``goalId``: a package names no goal)."""
    if "goalId" in obj.spec:
        return None, [Problem("invalid_rule", "error", "/spec/goalId", "a package names no goal")]
    payload, problems = _shaped(
        RuleCreateRequest, {"key": obj.key, **obj.spec}, "invalid_rule", "/spec"
    )
    if payload is None:
        return None, problems
    sent = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
    sent.pop("key", None)
    return sent, []


def _arguments(
    model: type[ApiModel], identity: str, obj: PackageObject, code: str
) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A supporting object as the keyword arguments of its command, or its findings."""
    payload, problems = _shaped(model, {identity: obj.key, **obj.spec}, code, "/spec")
    if payload is None:
        return None, problems
    return payload.model_dump(), []


def artifact_type_arguments(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """``mediaTypes`` is optional in the catalog schema: omitted, any (as package-sdk has it)."""
    spec = obj.spec if "mediaTypes" in obj.spec else {**obj.spec, "mediaTypes": ["*/*"]}
    return _arguments(
        ArtifactTypeCreateRequest,
        "key",
        PackageObject(obj.kind, obj.key, spec, obj.file, obj.lines),
        "invalid_artifact_type",
    )


def role_arguments(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    return _arguments(RoleCreateRequest, "slug", obj, "invalid_role")


def skill_arguments(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    return _arguments(SkillRegisterRequest, "name", obj, "invalid_skill")


def check_calendar(obj: PackageObject) -> tuple[Calendar | None, list[Problem]]:
    """A package object of kind Calendar: its shape (``CalendarSpec``), then the calendar itself."""
    spec, problems = calendar_spec(obj)
    return (Calendar.from_spec(spec) if spec is not None else None), problems


SHAPES: dict[str, SpecShape] = {
    "Calendar": calendar_spec,
    "TaskType": task_type_spec,
    "Agent": agent_spec,
    "WorkRule": rule_spec,
}
# What a test of a rule or a task type publishes first, when the tenant lacks it
# (CP-ADR-0074 Z2): the arguments of the command of each kind.
SUPPORTING: dict[str, SupportingShape] = {
    "ArtifactType": artifact_type_arguments,
    "Role": role_arguments,
    "Skill": skill_arguments,
}


# visibility: tenant — packages are objects of the tenant
@router.post(
    "/packages:test",
    response_model=PackageTestOut,
    responses=RESPONSES,
    summary="Check a package and run the tests of its processes, rules and task types;"
    " nothing is written",
)
async def package_tests(
    payload: PackageTestRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    check_only: bool = Query(
        default=False, alias="checkOnly", description="Check the package, run no test"
    ),
) -> JSONResponse:
    await authorize(ctx, Permission.PACKAGES_TEST)
    report = await run_package_tests(
        session_factory,
        ctx,
        settings,
        cast(GraphProvider | None, getattr(request.app.state, "context_provider", None)),
        files=[(item.path, item.content) for item in payload.package.files],
        tests=payload.tests,
        workspace_id=payload.workspace_id,
        check_only=check_only,
        check_calendar=check_calendar,
        shapes=SHAPES,
        supporting=SUPPORTING,
    )
    body = PackageTestOut.model_validate(report.out()).model_dump(mode="json", by_alias=True)
    return JSONResponse(body)


# visibility: tenant — packages are objects of the tenant
@router.post(
    "/packages:plan",
    response_model=PackagePlanOut,
    responses=ERROR_RESPONSES,
    summary="Plan applying a package: structural and behavioural diff, open instances, hash",
)
async def plan_package(
    payload: PackagePlanRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.PACKAGES_PLAN)
    plan = await package_plan.plan_package(
        session_factory,
        ctx,
        settings,
        cast(GraphProvider | None, getattr(request.app.state, "context_provider", None)),
        files=[(item.path, item.content) for item in payload.package.files],
        workspace_id=payload.workspace_id,
        replay_limit=payload.replay_limit,
        overwrite=payload.overwrite_console,
        shapes=SHAPES,
        supporting=SUPPORTING,
    )
    body = PackagePlanOut.model_validate(plan.out()).model_dump(mode="json", by_alias=True)
    return JSONResponse(body)


# visibility: tenant — packages are objects of the tenant
@router.post(
    "/packages:apply",
    response_model=PackageApplyOut,
    responses=ERROR_RESPONSES,
    summary="Apply exactly the plan with this hash; the catalog changed since — 409 plan_stale",
)
async def apply_package(
    payload: PackageApplyRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    # packages.plan here; each change is checked again against the right of
    # its kind (task_types.manage, agents.manage, calendars.write,
    # processes.write, rules.write) when it is applied.
    await authorize(ctx, Permission.PACKAGES_PLAN)
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        applied = await package_plan.apply_package(
            db,
            ctx,
            files=[(item.path, item.content) for item in payload.package.files],
            expected_hash=payload.plan_hash,
            workspace_id=payload.workspace_id,
            overwrite=payload.overwrite_console,
            shapes=SHAPES,
            touched=touched,
        )
        return 200, PackageApplyOut.model_validate(applied).model_dump(mode="json", by_alias=True)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(),
        executor=executor,
    )
    for issuer, iam_principal_id in touched:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response


# visibility: tenant — packages are objects of the tenant
@router.post(
    "/packages:record",
    response_model=PackageRecordOut,
    responses=ERROR_RESPONSES,
    summary="Link the objects an installer applied through their routes to their package",
)
async def record_package(
    payload: PackageRecordRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        recorded = await package_links.record_package(
            db,
            ctx,
            package_key=payload.package.key,
            package_version=payload.package.version,
            install_hash=payload.install_hash,
            objects=[(item.kind, item.key) for item in payload.objects],
        )
        return 200, PackageRecordOut.model_validate(recorded).model_dump(mode="json", by_alias=True)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(),
        executor=executor,
    )
