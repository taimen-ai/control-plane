"""The daemon configured by its agent's revision (CP-ADR-0073 §8, declarative-agents D006).

A principal bound to an agent reads its description with ``GET /agents/me``
and works by the current revision: which work it takes, which executor with
which parameters and instructions, which working copy with which neighbours,
which skills it runs itself, how long a run may take to drain. Environment
variables keep what belongs to the host and not to the agent — paths,
binaries, local logs, the credential — and, for a principal that is no agent,
the whole configuration as before (the env mode of local debugging).

The revision is fixed for the life of the process. Between runs the daemon
reads ``/agents/me`` again; a newer revision ends the process with
:data:`EXIT_REVISION_CHANGED` once the current run is over, and whoever
placed it starts it again on the new one. A process never mixes two
revisions: ``agentRevisionId`` of every run it starts is the one it was
built from.

Nothing here talks to the executor: this module turns a spec into the
arguments the daemon and its parts already take.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from control_plane_agent.catalog import (
    CatalogEntry,
    CatalogError,
    RepositoryCatalog,
    RepositoryPools,
    is_catalog,
)
from control_plane_agent.conventions import CatalogConventions
from control_plane_agent.mirrors import (
    MIRROR_LOCK_TIMEOUT,
    MirrorError,
    NeighbourMirrors,
    clone_bare,
    lock_path,
    mirror_lock,
    repository_name,
    shown,
    sweep_stale_clones,
)
from control_plane_agent.skills import (
    ENV_AUDIENCES,
    ENV_CONCURRENCY,
    ENV_HTTP_ORIGINS,
    ENV_LOCAL_ENV,
    ENV_LOCAL_PACKAGES,
    ENV_MCP_ORIGINS,
    ENV_PROTOCOLS,
    SkillExecutor,
    executor_from_environment,
    parse_skill_env,
)
from control_plane_agent.workspace import (
    ExecutionWorkspacePool,
    Neighbour,
    WorkspaceError,
)
from control_plane_client import ControlPlaneClient, NotFoundError

logger = logging.getLogger("control_plane_agent.revision")

#: The agent's revision changed: restart this process on the new one
#: (``EX_TEMPFAIL``). The run in flight was finished first.
EXIT_REVISION_CHANGED = 75
#: The configuration cannot be used — a spec the daemon cannot honour, or an
#: environment missing what the host must provide.
EXIT_MISCONFIGURED = 2

#: ``CONTROL_PLANE_AGENT_CONFIG``: ``auto`` (default) — the revision when the
#: principal is an agent, the environment otherwise; ``revision`` — an agent
#: is required; ``env`` — the environment, for a principal that is no agent.
CONFIG_MODES = ("auto", "revision", "env")
ENV_CONFIG_MODE = "CONTROL_PLANE_AGENT_CONFIG"
#: Where bare mirrors of the revision's repositories live on this host.
ENV_MIRRORS = "CONTROL_PLANE_AGENT_MIRRORS"
#: Mirrors of neighbours, apart from the pools' (``<mirrors>/neighbours/<key>.git``).
NEIGHBOUR_MIRRORS = "neighbours"
#: ``placement.drainSeconds`` when the spec does not say (the run limit).
DEFAULT_DRAIN_SECONDS = 14_400

#: Skill settings a revision owns. The host keeps the rest of
#: ``CONTROL_PLANE_SKILLS_*`` (isolation, private hosts, stdio MCP servers).
_SKILL_ENV = {
    "protocols": ENV_PROTOCOLS,
    "local": ENV_LOCAL_PACKAGES,
    "httpOrigins": ENV_HTTP_ORIGINS,
    "mcpOrigins": ENV_MCP_ORIGINS,
    "audiences": ENV_AUDIENCES,
}


class RevisionError(ValueError):
    """The revision describes something this daemon cannot run."""


@dataclass(frozen=True)
class AgentRevision:
    """One revision of the caller's agent, as ``GET /agents/me`` returned it."""

    key: str
    revision: int
    revision_id: str
    spec_hash: str
    spec: Mapping[str, Any]
    status: str
    state: str
    # Resolved by the core from ``work.workspace`` (an id or a slug).
    workspace_id: str | None = None

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> AgentRevision:
        revision = body["revision"]
        return cls(
            key=str(body["key"]),
            revision=int(revision["revision"]),
            revision_id=str(revision["id"]),
            spec_hash=str(revision["specHash"]),
            spec=dict(revision.get("spec") or {}),
            status=str(body.get("status") or "active"),
            state=str(body.get("state") or "running"),
            workspace_id=str(body["workspaceId"]) if body.get("workspaceId") else None,
        )

    @property
    def label(self) -> str:
        return f"{self.key}@{self.revision}"

    @property
    def retired(self) -> bool:
        return self.status == "retired"

    @property
    def stopped(self) -> bool:
        return self.state == "stopped"

    def section(self, name: str) -> dict[str, Any]:
        value = self.spec.get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def executor_kind(self) -> str | None:
        kind = self.section("executor").get("kind")
        return str(kind) if kind else None

    @property
    def executor_params(self) -> dict[str, Any]:
        return dict(self.section("executor").get("params") or {})

    @property
    def instructions(self) -> str:
        return str(self.section("executor").get("instructions") or "")


async def my_agent(client: ControlPlaneClient) -> AgentRevision | None:
    """The caller's agent, or None when its principal is not bound to one."""
    try:
        body = await client.get_my_agent()
    except NotFoundError:
        return None
    return AgentRevision.from_body(body)


def config_mode(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    mode = (values.get(ENV_CONFIG_MODE) or "auto").strip().lower()
    if mode not in CONFIG_MODES:
        raise RevisionError(
            f"{ENV_CONFIG_MODE}={mode!r}: expected one of {', '.join(CONFIG_MODES)}"
        )
    return mode


# -- what the daemon takes and how it finishes -----------------------------------


@dataclass(frozen=True)
class RevisionSettings:
    """The daemon's own arguments from a revision (``Agent(**settings.agent_kwargs())``)."""

    workspace_id: str | None = None
    project_id: str | None = None
    include_subprojects: bool = False
    only_assigned: bool = True
    task_types: frozenset[str] = field(default_factory=frozenset)
    drain_seconds: float | None = DEFAULT_DRAIN_SECONDS
    checks: bool = False

    def agent_kwargs(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "include_subprojects": self.include_subprojects,
            "only_assigned": self.only_assigned,
            "task_types": self.task_types,
            "drain_seconds": self.drain_seconds,
            "checks": self.checks,
        }


def settings_of(revision: AgentRevision) -> RevisionSettings:
    """``work`` and ``placement.drainSeconds`` of a revision.

    Defaults are the schema's, not the env mode's: ``onlyAssigned`` is true
    unless the spec says otherwise — an agent takes only work meant for it.
    ``workingCopy.review`` is read by nobody: review is an acceptance check
    of the task type now (CP-ADR-0073, amendment A2), and a revision that
    still carries the section is run without it. ``workingCopy.checks``
    switches the checks before hand-in on (universal-runner U014); off unless
    the spec says ``true``.
    """
    work = revision.section("work")
    if revision.section("workingCopy").get("review") is not None:
        logger.warning(
            "%s: workingCopy.review is ignored; the task type declares review", revision.label
        )
    project = work.get("project")
    only_assigned = bool(work.get("onlyAssigned", True))
    if not only_assigned and is_catalog(revision.section("workingCopy")):
        # A universal runner takes no work from the common pool (TAI-ADR-0063,
        # owner's decision 7): which agent runs a task is a person's choice.
        logger.warning(
            "%s: an agent with a repository catalog takes only work assigned to it; "
            "work.onlyAssigned=false is ignored",
            revision.label,
        )
        only_assigned = True
    checks = revision.section("workingCopy").get("checks", False)
    if not isinstance(checks, bool):
        # ``bool("false")`` is True: a string here would switch them on.
        raise RevisionError("workingCopy.checks must be a boolean")
    return RevisionSettings(
        workspace_id=revision.workspace_id,
        project_id=str(project) if project else None,
        include_subprojects=bool(work.get("includeSubprojects", False)),
        only_assigned=only_assigned,
        task_types=frozenset(str(t) for t in work.get("taskTypes") or []),
        drain_seconds=drain_seconds_of(revision),
        checks=checks,
    )


def drain_seconds_of(revision: AgentRevision) -> float | None:
    """How long a stop may wait for the run in flight; None — as long as it takes.

    An agent with ``placement: none`` is placed by nobody, so nobody drains
    it either: it keeps the old behaviour of finishing the run.
    """
    placement = revision.spec.get("placement")
    if placement == "none":
        return None
    if not isinstance(placement, Mapping):
        return float(DEFAULT_DRAIN_SECONDS)
    return float(placement.get("drainSeconds", DEFAULT_DRAIN_SECONDS))


# -- skills ----------------------------------------------------------------------


def skills_params(revision: AgentRevision) -> dict[str, str]:
    """``params.env`` of a ``skills`` executor (CP-ADR-0073, amendment 2026-10-01).

    The only parameter of the kind: non-secret settings of the skills, as a
    portal's address. Anything else, or a name the host keeps, is a
    :class:`RevisionError` — the core stores the params without reading them.
    """
    params = revision.executor_params
    unknown = sorted(set(params) - {"env"})
    if unknown:
        raise RevisionError(f"executor skills takes only params.env, got {unknown}")
    try:
        return parse_skill_env(params.get("env"), where="executor.params.env")
    except ValueError as exc:
        raise RevisionError(str(exc)) from exc


def skills_environ(revision: AgentRevision, environ: Mapping[str, str]) -> dict[str, str] | None:
    """``CONTROL_PLANE_SKILLS_*`` as the revision sets them; None without ``skills``.

    What the spec owns replaces the host's value even when the spec leaves it
    out: a skill protocol configured on the host must not run for an agent
    whose description does not name it.
    """
    skills = revision.section("skills")
    if not skills:
        return None
    owned = {*_SKILL_ENV.values(), ENV_CONCURRENCY, ENV_LOCAL_ENV}
    values = {k: v for k, v in environ.items() if k not in owned}
    for name, variable in _SKILL_ENV.items():
        items = skills.get(name) or []
        if items:
            values[variable] = ",".join(str(item) for item in items)
    if "concurrency" in skills:
        values[ENV_CONCURRENCY] = str(int(skills["concurrency"]))
    if revision.executor_kind == "skills":
        settings = skills_params(revision)
        if settings:
            values[ENV_LOCAL_ENV] = json.dumps(settings, sort_keys=True)
    return values


def skills_of(
    revision: AgentRevision, client: ControlPlaneClient, environ: Mapping[str, str] | None = None
) -> SkillExecutor | None:
    values = skills_environ(revision, os.environ if environ is None else environ)
    if values is None:
        return None
    try:
        return executor_from_environment(client, values)
    except ValueError as exc:
        raise RevisionError(f"skills: {exc}") from exc


# -- working copy ----------------------------------------------------------------


def workspace_pool_of(
    revision: AgentRevision, environ: Mapping[str, str] | None = None
) -> ExecutionWorkspacePool | RepositoryPools | None:
    """The working copies of ``workingCopy``; None when the revision has none.

    Repositories are named by URL in a spec, and copies are cut from bare
    mirrors on this host (``CONTROL_PLANE_AGENT_MIRRORS``, by default
    ``<worktree root>/.mirrors``): a mirror the host already keeps is used,
    a missing one is cloned. A value that is a directory on this host is used
    as it is — local debugging and tests. Where the copies live stays the
    host's (``CONTROL_PLANE_AGENT_WORKTREE_ROOT``).

    A catalog of repositories (``repositories``, ``repositoryField``;
    TAI-ADR-0063 §3) gives :class:`RepositoryPools`: a pool per key, made
    when the first task of that repository comes. The one-repository form
    gives one pool, as before.
    """
    values = os.environ if environ is None else environ
    copy = revision.section("workingCopy")
    if not copy:
        return None
    root = Path(
        values.get("CONTROL_PLANE_AGENT_WORKTREE_ROOT")
        or Path.home() / ".control-plane-agent" / "worktrees"
    ).expanduser()
    mirrors = Path(values.get(ENV_MIRRORS) or root / ".mirrors").expanduser()
    # Clones a process that died left half-made: at start, as nothing of this
    # process is cloning yet.
    for directory in (mirrors, mirrors / NEIGHBOUR_MIRRORS):
        sweep_stale_clones(directory)
    if is_catalog(copy):
        return _catalog_pools(copy, root, mirrors, values)
    # The core stores workingCopy as data (TAI-ADR-0063): its shape is checked
    # by the schema of the executor kind, and again here.
    repository = copy.get("repository")
    if not isinstance(repository, str) or not repository:
        raise RevisionError(
            "workingCopy: this runner builds a working copy of one repository; "
            "workingCopy.repository is missing"
        )
    try:
        return _one_pool(copy, repository, root, mirrors, values)
    except WorkspaceError as exc:
        raise RevisionError(f"workingCopy: {exc}") from exc


def _one_pool(
    copy: Mapping[str, Any],
    repository: str,
    root: Path,
    mirrors: Path,
    values: Mapping[str, str],
) -> ExecutionWorkspacePool:
    """The pool of the one-repository form; its fields checked as the schema does.

    Every error of the section is a :class:`RevisionError` (exit 2 at start),
    not a traceback: the core no longer checks this shape.
    """
    publish = copy.get("publish", True)
    if not isinstance(publish, bool):
        # ``bool("false")`` is True: a string here would publish.
        raise RevisionError("workingCopy.publish must be a boolean")
    named = copy.get("neighbours") or {}
    if not isinstance(named, Mapping) or not all(
        isinstance(name, str) and isinstance(url, str) and url for name, url in named.items()
    ):
        raise RevisionError("workingCopy.neighbours must map names to repository URLs")
    superproject_url = copy.get("superproject")
    if superproject_url is not None and not isinstance(superproject_url, str):
        raise RevisionError("workingCopy.superproject must be a repository URL")
    base_ref = copy.get("baseRef") or "HEAD"
    if not isinstance(base_ref, str):
        raise RevisionError("workingCopy.baseRef must be a branch name")
    directory = copy.get("directory") or ""
    if not isinstance(directory, str):
        raise RevisionError("workingCopy.directory must be a string")
    origin = mirror(repository, mirrors)
    push_remote = ""
    if publish:
        if _has_remote(origin, "origin"):
            push_remote = "origin"
        else:
            logger.warning("publish requested, but %s has no remote to publish to", origin.name)
    neighbours = [Neighbour(name, mirror(url, mirrors)) for name, url in named.items()]
    superproject = mirror(superproject_url, mirrors) if superproject_url else None
    return ExecutionWorkspacePool(
        origin,
        root,
        base_ref=base_ref,
        keep_on_success=values.get("CONTROL_PLANE_AGENT_KEEP_WORKSPACES") == "1",
        max_workspaces=_budget(values),
        push_remote=push_remote,
        repo_dir=directory,
        neighbours=neighbours,
        superproject=superproject,
        superproject_remote=(
            "origin" if superproject is not None and _has_remote(superproject, "origin") else ""
        ),
        publish_url=_publish_url(repository),
    )


def _budget(values: Mapping[str, str]) -> int:
    raw = values.get("CONTROL_PLANE_AGENT_MAX_WORKSPACES", "8")
    try:
        return int(raw)
    except ValueError:
        raise RevisionError("CONTROL_PLANE_AGENT_MAX_WORKSPACES must be a number") from None


def _catalog_pools(
    copy: Mapping[str, Any], root: Path, mirrors: Path, values: Mapping[str, str]
) -> RepositoryPools:
    try:
        catalog = RepositoryCatalog.from_spec(copy)
    except CatalogError as exc:
        raise RevisionError(f"workingCopy: {exc}") from exc
    keep = values.get("CONTROL_PLANE_AGENT_KEEP_WORKSPACES") == "1"
    budget = _budget(values)
    neighbours = NeighbourMirrors(mirrors / NEIGHBOUR_MIRRORS)

    def make_pool(entry: CatalogEntry, create: bool) -> ExecutionWorkspacePool | None:
        # Called when a task of the repository is taken: a repository that
        # cannot be mirrored fails that task's run like any other workspace
        # error, instead of refusing the whole revision at start.
        if not create and not _has_mirror(entry.url, mirrors):
            return None
        try:
            origin = mirror(entry.url, mirrors)
            push_remote = ""
            if entry.publish:
                if _has_remote(origin, "origin"):
                    push_remote = "origin"
                else:
                    logger.warning("publish requested, but %s has no remote", origin.name)
            return ExecutionWorkspacePool(
                origin,
                root,
                base_ref=entry.base_ref,
                keep_on_success=keep,
                max_workspaces=budget,
                push_remote=push_remote,
                repo_dir=entry.directory,
                repository_key=entry.key,
                publish_url=_publish_url(entry.url),
                conventions=CatalogConventions(catalog, entry, neighbours),
                neighbour_mirrors=neighbours.root,
            )
        except (RevisionError, WorkspaceError) as exc:
            raise WorkspaceError(f"repository {entry.key}: {exc}") from exc

    root.mkdir(parents=True, exist_ok=True)
    return RepositoryPools(catalog, root.resolve(), make_pool)


def _publish_url(repository: str) -> str:
    """The address pushes are checked against (``publish.py``): the configured one.

    A mirror's ``origin`` is that address (:func:`mirror`), so a push to it
    passes the check. A directory of this host is used as it is, and its
    ``origin`` is whatever it was cloned from: there is no configured address
    to hold it to, and only the hook checks the push.
    """
    if "://" not in repository and Path(repository).expanduser().is_dir():
        return ""
    return repository


def _has_mirror(repository: str, mirrors: Path) -> bool:
    """Whether copies of ``repository`` can already be on this host."""
    local = Path(repository).expanduser()
    if "://" not in repository and local.is_dir():
        return True
    try:
        name = repository_name(repository)
    except MirrorError:
        return False
    return bool(name) and (mirrors / f"{name}.git").is_dir()


def mirror(repository: str, mirrors: Path, *, lock_timeout: float = MIRROR_LOCK_TIMEOUT) -> Path:
    """The local repository copies of ``repository`` are cut from.

    A local directory is itself. A URL maps to ``<mirrors>/<name>.git`` by the
    last segment of its path; an existing mirror must have that URL as its
    ``origin`` — two repositories of one name are a conflict to resolve on
    the host, not a guess — and a missing one is cloned bare.

    Every failure is a :class:`RevisionError`: a malformed address, a clone
    that failed, or one another replica has run for more than
    ``lock_timeout`` seconds.
    """
    local = Path(repository).expanduser()
    if "://" not in repository and local.is_dir():
        return local
    try:
        name = repository_name(repository)
    except MirrorError as exc:
        raise RevisionError(str(exc)) from None
    if not name:
        raise RevisionError(f"repository {shown(repository)} names no repository")
    path = mirrors / f"{name}.git"
    if not path.is_dir():
        mirrors.mkdir(parents=True, exist_ok=True)
        # Replicas on one host share the mirrors: the first task of a
        # repository in two of them at once clones it once, the other waits
        # — for a while, not for ever.
        try:
            with mirror_lock(lock_path(path), what=f"mirror {path.name}", timeout=lock_timeout):
                if not path.is_dir():
                    clone_bare(repository, path)
        except MirrorError as exc:
            raise RevisionError(str(exc)) from None
    current = _git_output(path, "remote", "get-url", "origin")
    if _same_repository(current, repository):
        return path
    raise RevisionError(
        f"mirror {name}.git on this host is of {shown(current or '(no origin)')}, "
        f"not {shown(repository)}"
    )


def _same_repository(left: str | None, right: str) -> bool:
    def norm(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git")

    return left is not None and norm(left) == norm(right)


def _has_remote(repository: Path, name: str) -> bool:
    return _git_output(repository, "remote", "get-url", name) is not None


def _git_output(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


__all__ = [
    "CONFIG_MODES",
    "EXIT_MISCONFIGURED",
    "EXIT_REVISION_CHANGED",
    "AgentRevision",
    "RevisionError",
    "RevisionSettings",
    "config_mode",
    "drain_seconds_of",
    "mirror",
    "my_agent",
    "settings_of",
    "skills_environ",
    "skills_of",
    "skills_params",
    "workspace_pool_of",
]
