"""Execution workspace: an isolated working copy per task (ADR-0016 §5).

Why this exists: an adapter that works in ``os.getcwd()`` serializes the
process to one task at a time and makes the resulting commit useless as
evidence, because changes of different tasks blend together. Here every task
gets its own git worktree on a deterministic branch ``task/<publicId>``, and
the commit — referenced, never copied (harness-protocol §8) — is what proves
the work happened.

Ownership rule (ADR-0016 §2): the workspace belongs to the process holding the
claim. That is enforced locally by an exclusive lock file per workspace, so a
second process cannot write into the same copy even by mistake.

Restart survivability: nothing here is remembered in memory. The branch and the
workspace key are written to a Run Checkpoint by the caller, and ``acquire()``
of the same key reuses the existing copy — including its uncommitted changes —
instead of creating a second one.

Portability of evidence: absolute local paths never leave this module. What
goes to the Control Plane is the workspace key, the branch and the commit sha;
``assert_portable()`` rejects anything else before it is sent.

Completeness of the copy: a repository that builds against a sibling through a
path dependency (``platform-auth-sdk`` at ``../../sdk/platform-auth-sdk``) cannot be
built from a copy of itself alone. So a copy is not one directory but a small
container — the working copy next to the neighbours it needs — and each
neighbour is checked out at the revision the SUPERPROJECT pins, never at the
tip of its own branch. Otherwise a green test run says nothing: it was run
against a combination of revisions that exists in no commit anywhere.
"""

import contextlib
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from control_plane_agent.runner_config import RunnerConfig

logger = logging.getLogger("control_plane_agent.workspace")

CHECKPOINT_KIND = "execution.workspace"
ARTIFACT_TYPE = "commit"
BRANCH_PREFIX = "task/"
DEFAULT_COMMITTER = ("control-plane-agent", "agent@control-plane.local")
#: The executor changed a neighbour, which is read-only (universal-runner FR-007).
NEIGHBOUR_MODIFIED = "neighbour_modified"
#: The task branch would roll back a pointer of a neighbour the base moved on.
NEIGHBOUR_POINTER_REGRESSED = "neighbour_pointer_regressed"
#: The prose conventions whose blob the trace of a catalog run records (FR-011).
AGENTS_MD = "AGENTS.md"
#: Seconds a fetch of the base branch may take before it counts as failed.
BASE_FETCH_TIMEOUT = 120.0
#: Where a neighbour mirror keeps the revisions placed from it (``mirrors.py``).
PINS_PREFIX = "refs/remotes/pins/"

Outcome = Literal["succeeded", "failed", "suspended"]

# A workspace key is a task public id; it becomes a directory name and a branch
# name, so anything that could escape the root or confuse git is refused.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Payload guard. The goal is not general-purpose DLP but a hard stop on the two
# classes the task forbids: local filesystem paths and obvious credentials.
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "credential",
        "key_hash",
        "password",
        "secret",
        "token",
    }
)
_PATH_ROOTS = ("/Users/", "/home/", "/root/", "/private/", "/var/", "/tmp/", "/opt/", "/mnt/")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# The same roots, as a pattern that matches a path ANYWHERE in free text — for
# redacting messages rather than rejecting payloads. Deliberately anchored on
# known roots instead of "anything with slashes", so a URL or a repo-relative
# path is left alone.
_LOCAL_PATH_RE = re.compile(
    # The lookbehind is not decoration: without it the Windows branch matches
    # "s:/" inside "https://…" and redacts every URL in a message.
    r"(?:(?<![A-Za-z])[A-Za-z]:[\\/][^\s'\"]*"
    r"|(?:/(?:Users|home|root|private|var|tmp|opt|mnt|Volumes))(?:/[^\s'\"]*)?)"
)
_WHOLE_PATH_RE = re.compile(r"^/(?:[^/\s]+/)*[^/\s]*$")
_TOKEN_PREFIXES = ("cp_", "sk-", "ghp_", "github_pat_", "xox", "-----BEGIN")


# A directory in a container — the copy's own, a neighbour's — is a relative
# path of one segment (the flat layout) or several (``services/control-plane``,
# TAI-ADR-0064). A segment starts with a letter or a digit, so ``.`` and ``..``
# do not pass; an absolute path, an empty segment and a backslash do not
# either. The rule of package-sdk's ``$defs.workingCopyPath``, with upper case
# kept for the one-repository form, whose directory defaults to the origin's
# name. \Z, not $: $ admits a trailing newline.
_PATH_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_PATH_RE = re.compile(rf"^{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")
PATH_MAX_LENGTH = 200
# "160000 commit <sha>\t<path>" — the gitlink line of ``git ls-tree``.
_GITLINK_MODE = "160000"
# Per-branch git config recording what a task branch was cut from. Kept in the
# repository config rather than in the copy, so it outlives a removed copy and
# goes away with the branch (``git branch -D`` drops the branch's section).
_BASE_BRANCH_KEY = "controlPlaneBase"
_BASE_COMMIT_KEY = "controlPlaneBaseCommit"


#: How a published task branch relates to the mirror's (``published_state``).
PublishedState = Literal["unreachable", "absent", "same", "behind", "diverged"]


@dataclass(frozen=True)
class PointerRegression:
    """A submodule pointer of the task branch set back to an older one of the base.

    ``expected`` — the pointer of the base the branch must keep (where it
    last met the base, its head when merged); ``actual`` — what the branch
    would hand in; ``base`` — the ref of the base to take the pointer from.
    """

    name: str
    path: str
    expected: str
    actual: str
    base: str

    @property
    def what(self) -> str:
        return (
            f"has its pointer rolled back to {self.actual[:12]} on the task branch, "
            f"where the base has {self.expected[:12]}"
        )

    def reason(self) -> str:
        """Words for the executor of the next attempt; no local path."""
        return (
            f"neighbour {self.name}: the task branch would roll the pointer of submodule "
            f"{self.path} back from {self.expected} (the base) to {self.actual}. Take the "
            f"base's pointer: git checkout {self.base} -- {self.path} && "
            f"git submodule update -- {self.path}, commit, and do not stage the submodule "
            "with git add -A or commit -a"
        )


@dataclass(frozen=True)
class NeighbourCheck:
    """What :meth:`Workspace.neighbour_check` found: changes, and rolled-back pointers apart."""

    changes: Mapping[str, str] = field(default_factory=dict)
    regressions: Mapping[str, PointerRegression] = field(default_factory=dict)


class WorkspaceError(RuntimeError):
    """The working copy is not in the state the caller assumed."""


class WorkspaceBusyError(WorkspaceError):
    """Another process already holds this workspace."""


class WorkspaceBlocked(WorkspaceError):
    """The copy cannot be worked in until a person acts.

    ``code`` is the failure reason of the run (:data:`NEIGHBOUR_MODIFIED`,
    ``runner_config_invalid``), ``reason`` the words for the person; neither
    carries a local path.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = redact_local_paths(reason)


class UnsafePayloadError(ValueError):
    """A payload carries a local path or a credential and must not be sent."""


def assert_portable(data: Any, *, where: str = "payload") -> None:
    """Reject local paths and obvious credentials before they leave the host.

    Checkpoints, artifacts and events are read by other principals in other
    environments, where an absolute path of this machine is at best noise and
    at worst a disclosure (harness-protocol §8, ADR-0016 verification list).
    """
    _walk(data, where)


def _walk(value: Any, where: str, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _walk(child, f"{where}.{child_key}", str(child_key))
        return
    if isinstance(value, str | bytes):
        _check_string(
            value if isinstance(value, str) else value.decode("utf-8", "replace"), where, key
        )
        return
    if isinstance(value, Sequence):
        for index, child in enumerate(value):
            _walk(child, f"{where}[{index}]")


def _check_string(value: str, where: str, key: str | None) -> None:
    if key is not None and key.lower().replace("-", "_") in _SENSITIVE_KEYS and value:
        raise UnsafePayloadError(f"{where} looks like a credential and must not be sent")
    if value.startswith(_TOKEN_PREFIXES):
        raise UnsafePayloadError(f"{where} looks like a credential and must not be sent")
    if value.startswith("file://") or value.startswith("~/") or _WINDOWS_PATH_RE.match(value):
        raise UnsafePayloadError(f"{where} carries a local path and must not be sent")
    if any(root in value for root in _PATH_ROOTS) or _WHOLE_PATH_RE.match(value):
        raise UnsafePayloadError(f"{where} carries a local path and must not be sent")


def redact_local_paths(text: str) -> str:
    """Replace absolute host paths with a placeholder, keeping the rest intact.

    An error message has two readers with opposite needs: the runner's log wants
    every detail, and durable Control Plane state must carry no path of this
    machine (ADR-0016, harness-protocol §8). Redaction serves the second without
    reducing the message to "something failed" — the git subcommand, the exit
    condition and the remote's own words survive.
    """
    return _LOCAL_PATH_RE.sub("<path>", text)


def public_remote_url(url: str) -> str | None:
    """A remote URL as others may read it, or ``None`` if it cannot be shared.

    Credentials embedded in an http(s) URL (``https://user:token@host/…``) are
    dropped; a local path or ``file://`` remote means nothing off this host and
    is not shared at all (``assert_portable``).
    """
    url = url.strip()
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() in ("http", "https", "ssh", "git"):
        authority, slash, path = rest.partition("/")
        _, at, host = authority.rpartition("@")
        if at and scheme.lower() in ("http", "https"):
            authority = host
        url = f"{scheme}://{authority}{slash}{path}"
    if not url:
        return None
    try:
        assert_portable(url, where="repository")
    except UnsafePayloadError:
        return None
    return url


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    # Fixed argv, never a shell: workspace keys are validated before they reach git.
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env is not None else None,
        input=stdin,
    )
    if check and result.returncode != 0:
        detail = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        # Full detail to the log, redacted detail to the exception: the caller
        # may put this text into a run's failure_reason, which is durable and
        # read elsewhere. Fixing it here rather than at that call site means
        # every future caller inherits the guarantee.
        logger.warning("%s", detail)
        raise WorkspaceError(redact_local_paths(detail))
    return result.stdout.strip()


#: How long a push or a fetch from the forge may take before it counts as failed.
REMOTE_TIMEOUT_SECONDS = 300.0


def _git_remote(
    cwd: Path, *args: str, timeout: float = REMOTE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    """Run a git command that talks to a forge; None when it did not finish in time.

    Bounded by ``timeout`` (:data:`REMOTE_TIMEOUT_SECONDS` unless the caller
    has a tighter one) with its transport killed too (:func:`_bounded`), and
    never asks for credentials on a terminal nobody watches: a forge that
    wants a password fails the command instead of hanging the daemon.
    """
    result = _bounded(["git", *args], cwd, timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result is None:
        logger.warning("git %s did not finish in %gs", args[0], timeout)
    return result


@dataclass(frozen=True)
class Neighbour:
    """A repository the working copy must be able to build against.

    ``path`` is the submodule path in the superproject AND the neighbour's path
    in the container of the copy — the same string on purpose: the container
    repeats the superproject's layout, which is what a path dependency like
    ``../../sdk/platform-auth-sdk`` resolves against.
    ``origin`` is where copies of it are cut from, normally a bare mirror on
    the runner.
    """

    path: str
    origin: Path

    def __post_init__(self) -> None:
        if not is_relative_path(self.path):
            raise WorkspaceError(f"unsafe neighbour path: {self.path!r}")


def is_relative_path(value: object) -> bool:
    """Whether ``value`` is a directory a container may hold (:data:`_PATH_RE`)."""
    return isinstance(value, str) and len(value) <= PATH_MAX_LENGTH and bool(_PATH_RE.match(value))


def _overlaps(path: str, other: str) -> bool:
    """Whether one of two relative paths is the other or lies inside it."""
    a, b = path.casefold(), other.casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


@dataclass(frozen=True)
class PinnedNeighbour:
    """A neighbour of a catalog run, at the revision the superproject pins.

    ``name`` is its catalog key, as the checkpoint names it; ``path`` is
    where it goes — relative to the task's container, beside the copy, or,
    for a task of the superproject itself, relative to the copy, at the
    submodule's path. ``origin`` is its neighbour mirror (``mirrors.py``).
    """

    name: str
    path: str
    origin: Path
    revision: str

    def __post_init__(self) -> None:
        if not is_relative_path(self.path):
            raise WorkspaceError(f"unsafe neighbour path: {self.path!r}")


@dataclass(frozen=True)
class Conventions:
    """What a catalog run is prepared by, read from the base revision (TAI-ADR-0063 §4).

    ``revision`` is the commit ``runner.yaml`` and ``AGENTS.md`` were read
    from; ``config`` is None when the repository has no ``runner.yaml``
    there. ``inside`` — the neighbours are submodules of the copy itself.
    """

    revision: str
    config: RunnerConfig | None = None
    agents_md: str | None = None
    neighbours: tuple[PinnedNeighbour, ...] = ()
    inside: bool = False


#: Reads the conventions of a repository:
#: ``(mirror, base revision, head of the task branch, ref of the base branch)
#: -> Conventions``.
ConventionsReader = Callable[[Path, str, str, str], Conventions]


def _keep_pin(neighbour: PinnedNeighbour) -> None:
    """Keep a placed revision under a ref of its mirror.

    A copy left at it is then no "commit of its own" when the forge rewrites
    the branch that had it (a force-push), and gc does not take it.

    Not checked: replicas share the mirror, and two of them keeping the same
    pin at once race for the ref's lock. The loser has nothing to redo — the
    ref names the same commit either way — and a later run keeps it again.
    """
    _git(
        neighbour.origin,
        "update-ref",
        f"{PINS_PREFIX}{neighbour.revision}",
        neighbour.revision,
        check=False,
    )


def prune_pins(mirror: Path) -> list[str]:
    """Drop pins of ``mirror`` no copy cut from it stands at; the revisions dropped.

    A pin exists for a copy (:func:`_keep_pin`); once no worktree of the
    mirror is at its revision, it only keeps a commit the forge may have
    dropped from gc, and pins would pile up with every move of a pointer.

    Refs are listed before worktrees: a copy is placed before its pin is
    kept, so a pin listed here has its copy listed too. A pin fetched by
    ``reach`` for a copy not placed yet may go; its commit stays in the
    object store until gc's expiry, and placing the copy keeps the pin again.
    """
    refs = _git(mirror, "for-each-ref", "--format=%(refname)", PINS_PREFIX, check=False)
    _git(mirror, "worktree", "prune", check=False)
    listed = _git(mirror, "worktree", "list", "--porcelain", check=False)
    in_use = {line.split()[1] for line in listed.splitlines() if line.startswith("HEAD ")}
    stale = [
        ref.removeprefix(PINS_PREFIX)
        for ref in refs.splitlines()
        if ref and ref.removeprefix(PINS_PREFIX) not in in_use
    ]
    if stale:
        # With the old value: a ref named by its own sha is never moved, and
        # a delete that finds something else there leaves it alone.
        commands = "".join(f"delete {PINS_PREFIX}{sha} {sha}\n" for sha in stale)
        result = subprocess.run(
            ["git", "update-ref", "--stdin"],
            cwd=mirror,
            input=commands,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Another replica pruned or kept one of them meanwhile: the next
            # prune gets the rest.
            logger.info("pins of %s not pruned: %s", mirror.name, result.stderr.strip()[:200])
            return []
        logger.info("pruned %d pin(s) of %s", len(stale), mirror.name)
    return stale


def _run_bounded(
    argv: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv``; a run past ``timeout`` is killed with its children and fails."""
    result = _bounded(argv, cwd, timeout)
    if result is None:
        return subprocess.CompletedProcess(
            list(argv), -signal.SIGKILL, "", f"timed out after {timeout:g} s"
        )
    return result


def _bounded(
    argv: Sequence[str], cwd: Path, timeout: float, *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run ``argv``; None when it ran past ``timeout`` and was killed with its children.

    A fetch spawns a transport (``ssh``, ``git-remote-https``, a local
    ``upload-pack``) that a kill of git alone leaves behind holding the
    pipes, so the process gets a group of its own and the whole group goes.
    """
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=None if env is None else dict(env),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return None
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _tree_gitlink(repository: Path, revision: str, path: str) -> str:
    """The commit a gitlink at ``revision:path`` points to; empty if none is there."""
    fields = _git(repository, "ls-tree", revision, "--", path, check=False).split()
    return fields[2] if len(fields) >= 3 and fields[0] == "160000" else ""


def _index_gitlink(repository: Path, path: str) -> str:
    """The commit a gitlink staged at ``path`` points to; empty if none is staged."""
    fields = _git(repository, "ls-files", "--stage", "--", path, check=False).split()
    return fields[1] if len(fields) >= 3 and fields[0] == "160000" else ""


def blob_of(repository: Path, revision: str, path: str) -> str | None:
    """Id of the blob at ``revision:path``; None when there is no file there."""
    spec = f"{revision}:{path}"
    kind = _git(repository, "cat-file", "-t", spec, check=False)
    if kind != "blob":
        return None
    return _git(repository, "rev-parse", "--verify", "--quiet", spec, check=False) or None


def _check_branch_name(name: str) -> None:
    """Refuse a base branch git would not accept or would read as an option.

    The name comes from a task field anyone with write access to the task can
    set, and it ends up in git argv and in a refspec.
    """
    valid = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{name}"], capture_output=True, check=False
    )
    if not name or name.startswith("-") or valid.returncode != 0:
        raise WorkspaceError(f"unsafe base branch name: {name!r}")


def base_branch_of(task: Mapping[str, Any]) -> str | None:
    """The task's own base branch (``customFields.baseBranch``), if it has one.

    An ordinary field of the task type, not a core concept: a task without it
    keeps the pool's default base. A value that is not a string is refused
    rather than ignored — ignoring it would cut the copy from the default base.
    """
    value = (task.get("customFields") or {}).get("baseBranch")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        kind = type(value).__name__
        raise WorkspaceError(f"customFields.baseBranch must be a string, got {kind}")
    return value.strip()


def parse_neighbours(spec: str) -> list[Neighbour]:
    """Read ``name=/path/to/mirror.git`` pairs, comma or whitespace separated.

    A malformed entry raises instead of being skipped: a neighbour silently
    dropped here comes back as a build failure inside the agent's copy, where
    the cause is no longer visible.
    """
    neighbours: list[Neighbour] = []
    for entry in (item for chunk in spec.split(",") for item in chunk.split()):
        name, separator, origin = entry.partition("=")
        if not separator or not origin:
            raise WorkspaceError(f"neighbour must be given as name=origin, got {entry!r}")
        neighbours.append(Neighbour(name, Path(origin).expanduser()))
    return neighbours


@dataclass
class Workspace:
    """One task's working copy. Created and released by the pool."""

    key: str
    branch: str
    path: Path
    base_commit: str
    reused: bool
    # Revision each neighbour was placed at, by directory name. Part of the
    # evidence: it says which combination of revisions the work was done and
    # verified against.
    neighbours: Mapping[str, str] = field(default_factory=dict)
    # Branch the copy was cut from and the work is meant to be merged into: the
    # task's own ``baseBranch`` (a feature branch), or the pool's default.
    base_branch: str = ""
    # Key of the repository in the agent's catalog (universal-runner U005);
    # empty for the one-repository form of ``workingCopy``.
    repository_key: str = ""
    # The commit the task branch was cut from — for a published branch taken
    # over, the point it shares with the base. Empty when no record says.
    base_revision: str = ""
    # What the run was prepared by (a catalog run, universal-runner U006).
    conventions: Conventions | None = None
    # The commit the run's runner.yaml is read at: the task's base as the pool
    # found it (``_base_of``), never the branch's own head. Empty when the
    # pool could not tell.
    conventions_base: str = ""
    # (name, directory, revision) of each neighbour placed for this run.
    placed: tuple[tuple[str, Path, str], ...] = ()
    # The copy's directory in its container, as the pool names it: one name
    # or several segments (``services/control-plane``). Empty — one name.
    repo_dir: str = ""
    # Refs of the base branch in the copy: a submodule pointer a merge of
    # them brought in is the base's, not a change of the task's own.
    base_refs: tuple[str, ...] = ()
    # Brings the forge's copy of the base into ``base_refs``, best-effort:
    # the task may have merged a base that moved after the copy was cut.
    refresh_base: Callable[[], object] | None = field(default=None, repr=False, compare=False)
    _lock_fd: int | None = None

    @property
    def container(self) -> Path:
        """Directory holding this copy and its neighbours, as deep as ``repo_dir`` goes."""
        depth = self.repo_dir.count("/") + 1 if self.repo_dir else 1
        return self.path.parents[depth - 1]

    @property
    def checkpoint_data(self) -> dict[str, Any]:
        """Durable, portable state of this workspace — no local paths."""
        data: dict[str, Any] = {
            "workspaceKey": self.key,
            "branch": self.branch,
            "baseCommit": self.base_commit,
            "reused": self.reused,
        }
        if self.base_branch:
            data["baseBranch"] = self.base_branch
        if self.repository_key:
            # The trace of a catalog run (FR-011); the one-repository form
            # keeps its checkpoint as it was.
            data["repositoryKey"] = self.repository_key
            if self.base_revision:
                data["baseRevision"] = self.base_revision
        if self.conventions is not None:
            data["conventionsRevision"] = self.conventions.revision
            data["agentsMdBase"] = self.conventions.agents_md
        if self.neighbours:
            data["neighbours"] = dict(self.neighbours)
        assert_portable(data, where=CHECKPOINT_KIND)
        return data

    @property
    def is_dirty(self) -> bool:
        return bool(_git(self.path, "status", "--porcelain"))

    def head(self) -> str:
        return _git(self.path, "rev-parse", "HEAD")

    def head_state(self) -> tuple[str, str]:
        """Where HEAD is: its branch (``HEAD`` when detached) and its commit."""
        ref = _git(self.path, "rev-parse", "--symbolic-full-name", "HEAD", check=False)
        return ref or "HEAD", self.head()

    def head_message(self) -> str:
        """The message of the commit HEAD points at; empty when it cannot be read."""
        return _git(self.path, "log", "-1", "--format=%B", "HEAD", check=False)

    def agents_md_at(self, revision: str) -> str | None:
        """Blob of ``AGENTS.md`` at ``revision`` of the copy; None without the file."""
        return blob_of(self.path, revision, AGENTS_MD)

    def neighbour_changes(self) -> dict[str, str]:
        """What happened to each neighbour placed for the run, if anything.

        Every finding of :meth:`neighbour_check` in words, a rolled-back
        pointer included.
        """
        check = self.neighbour_check()
        return {
            **check.changes,
            **{name: regression.what for name, regression in check.regressions.items()},
        }

    def neighbour_check(self) -> NeighbourCheck:
        """What happened to each neighbour placed for the run, if anything.

        A neighbour is read-only (FR-007): changed or new files that are not
        ignored, a HEAD moved off the pinned revision, a directory removed
        or replaced — each means the run did not work against the revisions
        its checkpoint names, and only the task's repository is published.

        For a task of the superproject the pointer is the neighbour's too: a
        pointer staged in the copy's index, or committed on the task branch
        and brought in by no merge of the base, is a change of the neighbour.
        A pointer the base has — where the branch last met it, or at its head
        now — is not: the base may move it while the run goes on, and the
        checkout of a nested neighbour may follow it (``git submodule
        update`` after a merge of the base).

        A pointer set back to an older one of the base — a merge of the base
        followed by ``commit -a`` with the checkout still at the revision of
        acquisition — is told apart (``regressions``): the executor fixes it
        with one checkout of the base's pointer.

        Every submodule of the superproject is held to the base's pointers,
        not only the neighbours ``runner.yaml`` names (package-sdk was not
        one): its pointer is no work of the task either. One not placed is
        named by its path; its checkout is the executor's own and is not
        looked at.
        """
        changes: dict[str, str] = {}
        regressions: dict[str, PointerRegression] = {}
        nested = self._nested()
        superproject = self.conventions is not None and self.conventions.inside
        if superproject and self.refresh_base is not None:
            self.refresh_base()
        for name, directory, revision in self.placed:
            if not (directory / ".git").exists():
                changes[name] = "was removed or replaced"
                continue
            path = nested.get(directory)
            allowed = self._base_pointers(path, revision) if path is not None else {revision}
            head = _git(directory, "rev-parse", "HEAD", check=False)
            if head != revision and not (head and head in allowed):
                changes[name] = f"moved from {revision[:12]} to {head[:12] or 'nothing'}"
            elif _git(directory, "status", "--porcelain", check=False):
                changes[name] = "has changed or new files"
            elif path is not None:
                pointer = self._pointer_change(path, allowed)
                if not pointer:
                    continue
                regression = self._regression(name, directory, path, allowed)
                if regression is not None:
                    regressions[name] = regression
                else:
                    changes[name] = pointer
        placed = set(nested.values())
        for path in self._gitlinks() if superproject else []:
            if path in placed:
                continue
            allowed = self._base_pointers(path, "") - {""}
            pointer = self._pointer_change(path, allowed)
            if not pointer:
                continue
            regression = self._regression(path, self.path / path, path, allowed)
            if regression is not None:
                regressions[path] = regression
            else:
                changes[path] = pointer
        return NeighbourCheck(changes, regressions)

    def _gitlinks(self) -> list[str]:
        """Paths of every submodule of the copy: gitlinks on the branch and in the index."""
        paths: set[str] = set()
        for listing in (
            _git(self.path, "ls-tree", "-r", "-z", "HEAD", check=False),
            _git(self.path, "ls-files", "--stage", "-z", check=False),
        ):
            for entry in listing.split("\0"):
                meta, _, path = entry.partition("\t")
                if path and meta.split()[:1] == [_GITLINK_MODE]:
                    paths.add(path)
        return sorted(paths)

    def _nested(self) -> dict[Path, str]:
        """Neighbours placed inside the copy, by directory: their submodule paths."""
        if self.conventions is None or not self.conventions.inside:
            return {}
        return {
            directory: directory.relative_to(self.path).as_posix()
            for _, directory, _ in self.placed
        }

    def _base_pointers(self, path: str, revision: str) -> set[str]:
        """Pointers of the submodule at ``path`` that are the base's, not the task's.

        The base's pointer where the branch last met its base: the point it
        was cut from, moved on by each merge of a ref of the base branch.
        Only the latest meeting counts — a pointer rolled back to what the
        base had before a merge is the task's change, not the base's. And the
        pointer at the head of the base now: one the base moved to during the
        run is the base's, merged or not.
        """
        latest = self._last_meetings()
        allowed = {_tree_gitlink(self.path, point, path) for point in latest} or {revision}
        # The first ref that is there: the forge's copy of the base before the
        # copy's own branch, which may be as old as the mirror.
        for ref in self.base_refs:
            tip = _git(
                self.path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
            )
            if tip:
                pointer = _tree_gitlink(self.path, tip, path)
                if pointer:
                    allowed.add(pointer)
                break
        return allowed

    def _last_meetings(self) -> list[str]:
        """Where the branch last met its base: the cut point, moved on by each merge of it."""
        points = {self.base_revision} if self.base_revision else set()
        for ref in self.base_refs:
            fork = _git(self.path, "merge-base", "HEAD", ref, check=False)
            if fork:
                points.add(fork)
        return sorted(
            point
            for point in points
            if not any(other != point and self._is_ancestor(point, other) for other in points)
        )

    def _regression(
        self, name: str, directory: Path, path: str, allowed: set[str]
    ) -> PointerRegression | None:
        """The pointer of ``path`` the branch would hand in, if it is an older one of the base.

        Older: the pointer at the point the branch was cut from — the
        checkout of acquisition taken by ``commit -a`` after a merge of the
        base — or an ancestor of the pointer the base expects, by the
        history of the neighbour when ``directory`` has it (a submodule not
        placed may have no checkout). A pointer moved anywhere else is the
        task's own move, :data:`NEIGHBOUR_MODIFIED` as before.
        """
        committed = _tree_gitlink(self.path, "HEAD", path)
        staged = _index_gitlink(self.path, path)
        # What the commit would take: the branch's pointer, or a staged one over it.
        actual = committed if committed not in allowed else staged
        if not actual or actual in allowed:
            return None
        expected = next(
            (
                pointer
                for point in self._last_meetings()
                if (pointer := _tree_gitlink(self.path, point, path))
            ),
            "",
        )
        if not expected:
            return None
        cut = _tree_gitlink(self.path, self.base_revision, path) if self.base_revision else ""
        older = actual == cut or (
            (directory / ".git").exists()
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", actual, expected],
                cwd=directory,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not older:
            return None
        return PointerRegression(name, path, expected, actual, self._base_name())

    def _base_name(self) -> str:
        """The base as an executor names it in the copy: ``origin/main``, or ``main``."""
        for ref in self.base_refs:
            if ref.startswith("refs/remotes/"):
                return ref.removeprefix("refs/remotes/")
        return self.base_branch or "main"

    def _pointer_change(self, path: str, allowed: set[str]) -> str:
        """What the task did to the pointer of the submodule at ``path``; empty if nothing.

        ``allowed`` — the pointers of the base (:meth:`_base_pointers`).
        """
        staged = _index_gitlink(self.path, path)
        committed = _tree_gitlink(self.path, "HEAD", path)
        if staged != committed:
            return f"has its pointer staged at {staged[:12] or 'nothing'} in the copy"
        if committed not in allowed:
            return (
                f"has its pointer committed at {committed[:12] or 'nothing'} on the task "
                "branch, which no merge of the base brought in"
            )
        return ""

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.path,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def snapshot(self) -> str:
        """Tree of what the copy holds now, as ``commit`` would take it.

        Written through an index of its own: the copy's index, and so what
        the executor staged, is not touched.
        """
        with tempfile.TemporaryDirectory(prefix="cp-snapshot-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            _git(self.path, "read-tree", "HEAD", env=env)
            _git(self.path, "add", "-A", "--", ".", *self._excluded(), env=env)
            return _git(self.path, "write-tree", env=env)

    def restore(self, tree: str) -> None:
        """Put the files of the copy back to ``tree`` (:meth:`snapshot`).

        What was added since goes, what was changed or removed comes back;
        ignored files and nested neighbours stay as they are. The checks run
        in the copy, and what they leave behind is not the executor's work.

        The ``.gitignore`` files come back first, and the rest is compared
        under them: a check that ignored its own output there would otherwise
        leave it behind, to be committed once the file is back.
        """
        changes = self._changes_since(tree)
        ignores = [change for change in changes if Path(change[1]).name == ".gitignore"]
        if ignores:
            self._put_back(tree, ignores)
            changes = self._changes_since(tree)
        self._put_back(tree, changes)

    def _changes_since(self, tree: str) -> list[tuple[str, str]]:
        """``(status, path)`` of what differs between ``tree`` and the copy now."""
        diff = _git(
            self.path,
            "diff",
            "--no-renames",
            "--name-status",
            "-z",
            tree,
            self.snapshot(),
            "--",
            ".",
            *self._excluded(),
        )
        fields = diff.split("\0")
        return list(zip(fields[::2], fields[1::2], strict=False))

    def _put_back(self, tree: str, changes: list[tuple[str, str]]) -> None:
        added: list[str] = []
        back: list[str] = []
        for status, path in changes:
            (added if status == "A" else back).append(path)
        root = self.path.resolve()
        for path in added:
            target = self.path / path
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            # Directories the checks made for their files go with them.
            parent = target.parent
            while parent.resolve() != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        if back:
            _git(
                self.path,
                "restore",
                f"--source={tree}",
                "--worktree",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                env={"GIT_LITERAL_PATHSPECS": "1"},
                stdin="\0".join(back),
            )

    def _excluded(self) -> list[str]:
        """Pathspecs of the nested neighbours: never part of the task's work."""
        return [f":(exclude,literal){path}" for path in self._nested().values()]

    def commit(
        self,
        summary: str = "",
        *,
        committer: tuple[str, str] = DEFAULT_COMMITTER,
        allow_empty: bool = False,
    ) -> str | None:
        """Commit everything in the copy as evidence. None if nothing changed.

        The message always carries the task public id: a commit that cannot be
        traced back to its task is not evidence.

        An agent that commits inside the copy itself leaves a clean tree but a
        branch that moved past ``base_commit``. That IS the evidence — the
        daemon must publish it, not report "no changes" and leave the branch
        stranded on the runner (seen on the first BidOps smoke run).

        With ``allow_empty`` a clean copy still gets a commit of its own:
        the result of a run that continued saved WIP and added nothing is
        not that WIP (FR-022).
        """
        self._stage_all()
        if not allow_empty and not _git(self.path, "diff", "--cached", "--name-only", check=False):
            head = self.head()
            return head if head != self.base_commit else None
        self._commit_staged(summary, committer, allow_empty=allow_empty)
        return self.head()

    def commit_wip(
        self, summary: str, *, committer: tuple[str, str] = DEFAULT_COMMITTER
    ) -> str | None:
        """Commit what the copy holds, uncommitted, as WIP; None if it holds nothing.

        Unlike :meth:`commit`, it never answers with a commit already there:
        a WIP record names only a commit made here. A moved submodule pointer
        is left out (:meth:`_stage_all`).
        """
        self._stage_all()
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.path, check=False
        ).returncode
        if staged == 0:
            return None
        self._commit_staged(summary, committer)
        return self.head()

    def _stage_all(self) -> None:
        # No submodule pointer is the daemon's to commit, a neighbour's or
        # not (9b30f86, 54d149a: package-sdk, outside ``neighbours``): the
        # pointer on the branch is the base's (checked by ``neighbour_check``),
        # while the checkout under it may be of an older pin when the task
        # merged its base during the run. One staged before goes back too.
        excluded = self._excluded()
        _git(self.path, "add", "-A", *(["--", ".", *excluded] if excluded else []))
        # "-z --raw": ":<old mode> <new mode> <old> <new> <status>\0<path>\0" per entry.
        tokens = _git(self.path, "diff", "--cached", "--raw", "--no-renames", "-z").split("\0")
        gitlinks = [
            path
            for meta, path in zip(tokens[::2], tokens[1::2], strict=False)
            if _GITLINK_MODE in meta.lstrip(":").split()[:2]
        ]
        if gitlinks:
            _git(self.path, "reset", "-q", "--", *gitlinks)

    def _commit_staged(
        self, summary: str, committer: tuple[str, str], *, allow_empty: bool = False
    ) -> None:
        name, email = committer
        message = summary.strip() or f"{self.key}: automated execution"
        if self.key not in message:
            message = f"{message}\n\nTask: {self.key}"
        _git(
            self.path,
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "--no-verify",
            *(["--allow-empty"] if allow_empty else []),
            "-m",
            message,
        )

    def push(self, remote: str = "origin") -> bool:
        """Publish the task branch so the work can be reviewed in the forge.

        Three deliberate limits. Only ``self.branch`` is ever pushed, and it is
        named explicitly on both sides — a runner offers work for review, it
        does not move the branch everyone else builds on. The push is never
        forced: if the remote branch has diverged, that is a person's business
        to resolve, and overwriting it would destroy exactly the review history
        this exists to create. And a failure is reported, not raised — the
        commit is already the evidence, so a forge that is unreachable must not
        turn finished work into a failed run.
        """
        result = _git_remote(
            self.path, "push", remote, f"refs/heads/{self.branch}:refs/heads/{self.branch}"
        )
        if result is None:
            return False
        if result.returncode != 0:
            # stderr may name the remote URL and local paths, so it stays in the
            # runner's log and never travels to the Control Plane.
            logger.warning("push of %s failed: %s", self.branch, result.stderr.strip()[:300])
            return False
        return True

    def artifact_uri(self, sha: str) -> str:
        """Reference, not content: the Control Plane is not a file store."""
        return f"git:{sha}"


class ExecutionWorkspacePool:
    """Hands out isolated working copies of one repository, one per task.

    ``origin`` is the repository the copies are made from — a bare clone on a
    runner, a normal checkout in tests. Copies live under ``root`` and are git
    worktrees, so they share the object store and cost little disk each.

    Each task gets a container directory ``root/<key>/`` holding the working
    copy at ``root/<key>/<repo_dir>/`` and, beside it, every configured
    neighbour. The nesting is what makes a path dependency ``../<neighbour>``
    resolve, and it keeps neighbours per task: two tasks running at once cannot
    move the same sibling under each other's feet. ``repo_dir`` and the
    neighbours' paths may have several segments (TAI-ADR-0064): the container
    is then laid out like the superproject, and ``../../sdk/<neighbour>`` from
    ``services/<repository>`` resolves as ``../<neighbour>`` does in the flat
    layout.

    ``superproject`` is the repository whose submodules pin the neighbours.
    Revisions are read from its tree, so a copy is built against the
    combination of revisions somebody actually committed — not against the tip
    of each neighbour's branch, which is a combination no commit describes.
    """

    def __init__(
        self,
        origin: Path | str,
        root: Path | str,
        *,
        base_ref: str = "HEAD",
        branch_prefix: str = BRANCH_PREFIX,
        keep_on_success: bool = False,
        max_workspaces: int = 8,
        push_remote: str = "",
        repo_dir: str = "",
        neighbours: Sequence[Neighbour] = (),
        superproject: Path | str | None = None,
        superproject_ref: str = "HEAD",
        superproject_remote: str = "",
        repository_key: str = "",
        publish_url: str = "",
        conventions: ConventionsReader | None = None,
        neighbour_mirrors: Path | str | None = None,
        base_fetch_timeout: float = BASE_FETCH_TIMEOUT,
    ) -> None:
        self.origin = Path(origin).expanduser().resolve()
        self.root = Path(root).expanduser().resolve()
        self.base_ref = base_ref
        self.branch_prefix = branch_prefix
        self.keep_on_success = keep_on_success
        self.max_workspaces = max_workspaces
        # Empty means "keep the work local". Publishing is opt-in because it
        # needs a credential on the runner and because not every deployment
        # wants a branch per attempt in its forge.
        self.push_remote = push_remote
        # A forge that hangs must not hang the start or the end of a run: a
        # fetch of the base past this is a failed one, and the copy goes on
        # with the ref it has (``_track_base``).
        self.base_fetch_timeout = base_fetch_timeout
        # The working copy's own directory name inside the container. It
        # matters when a neighbour's path dependency is written relative to it,
        # so it defaults to the repository name rather than something generic.
        self.repo_dir = repo_dir or self.origin.name.removesuffix(".git")
        if not is_relative_path(self.repo_dir):
            raise WorkspaceError(f"unsafe repository directory: {self.repo_dir!r}")
        self.neighbours = tuple(neighbours)
        for neighbour in self.neighbours:
            # One copy inside the other would have one checkout write into
            # the files of the other.
            if _overlaps(neighbour.path, self.repo_dir):
                raise WorkspaceError(
                    f"neighbour {neighbour.path!r} and the copy's directory "
                    f"{self.repo_dir!r} lie one inside the other"
                )
        self.superproject = Path(superproject).expanduser().resolve() if superproject else None
        self.superproject_ref = superproject_ref
        self.superproject_remote = superproject_remote
        # The catalog key this pool cuts copies of, written to the checkpoint
        # (``catalog.py``); empty for a pool of the one-repository form.
        self.repository_key = repository_key
        # The address branches are meant to go to, as the configuration names
        # it (the catalog entry's url, ``workingCopy.repository``): the push
        # target is checked against it before every push (``publish.py``).
        # Empty — nothing to check against but the remote itself.
        self.publish_url = publish_url
        # A catalog pool reads its neighbours from ``runner.yaml`` at the base
        # revision of each task (``conventions.py``) instead of ``neighbours``;
        # they are cut from mirrors under ``neighbour_mirrors``, which is also
        # how their copies are told from anything else in a container.
        self.conventions = conventions
        self.neighbour_mirrors = (
            Path(neighbour_mirrors).expanduser().resolve() if neighbour_mirrors else None
        )
        if self.neighbours and self.superproject is None:
            # Placing neighbours at "whatever their main is" is the failure this
            # exists to prevent, so it is refused at construction rather than
            # improvised per task.
            raise WorkspaceError("neighbours require a superproject that pins their revisions")
        self.root.mkdir(parents=True, exist_ok=True)

    # -- lifecycle -------------------------------------------------------------

    def branch_for(self, key: str) -> str:
        return f"{self.branch_prefix}{key}"

    def container_for(self, key: str) -> Path:
        return self.root / key

    def path_for(self, key: str) -> Path:
        return self.container_for(key) / self.repo_dir

    def push_remote_url(self) -> str | None:
        """Where published branches go, as a URL a reviewer or a skill can use.

        ``None`` without a push remote, or when the remote cannot be shared
        (a local path; see :func:`public_remote_url`).
        """
        if not self.push_remote:
            return None
        url = _git(self.origin, "remote", "get-url", self.push_remote, check=False)
        return public_remote_url(url) if url else None

    def push_urls(self) -> list[str]:
        """Every address ``git push`` to the push remote goes to, as git resolves them.

        A remote may have several push URLs, and git pushes to each of them:
        checking only the first would let the others through.
        """
        if not self.push_remote:
            return []
        out = _git(
            self.origin, "remote", "get-url", "--push", "--all", self.push_remote, check=False
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def is_task_branch(self, branch: str) -> bool:
        """Whether ``branch`` is a task branch this pool cuts: prefix and a valid key."""
        prefix = self.branch_prefix
        return (
            bool(prefix)
            and branch.startswith(prefix)
            and bool(_KEY_RE.match(branch[len(prefix) :]))
        )

    def branch_head(self, branch: str) -> str | None:
        """The commit ``branch`` points at in the mirror; None if there is no such branch."""
        return (
            _git(
                self.origin,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}^{{commit}}",
                check=False,
            )
            or None
        )

    def push_branch(
        self, branch: str, commit: str | None = None, *, urls: Sequence[str] | None = None
    ) -> str | None:
        """Push ``branch`` of the mirror; None when it went, else why not. Never forced.

        The same limits as :meth:`Workspace.push`; pushing from the mirror
        rather than a copy lets a branch whose copy is gone be published.
        Only a task branch is pushed (:meth:`is_task_branch`), whoever asks.
        ``commit`` — push exactly that commit to the branch (the one the
        target check was asked about), not whatever the branch holds by
        then. ``urls`` — push to exactly these addresses, one by one, rather
        than to the push remote by name: the mirror's config is shared with
        the executor's copies, and a push URL read at the check may be
        another by the push. Callers go through ``publish.publish_branch``,
        which checks the target first. The reason is git's own words and may
        name addresses: callers redact it before recording it.
        """
        if not self.is_task_branch(branch):
            logger.warning("refusing to push %r: not a task branch", branch)
            return "not a task branch"
        _check_branch_name(branch)
        ref = f"refs/heads/{branch}"
        targets = [self.push_remote] if urls is None else list(urls)
        if not targets or not all(targets):
            return "no address to push to"
        for target in targets:
            result = _git_remote(self.origin, "push", target, f"{commit or ref}:{ref}")
            if result is None:
                return f"git push did not finish in {REMOTE_TIMEOUT_SECONDS:g}s"
            if result.returncode != 0:
                error = result.stderr.strip()
                # stderr may name the remote URL and local paths: the log only.
                logger.warning("push of %s failed: %s", branch, error[:300])
                lines = [line.strip() for line in error.splitlines() if line.strip()]
                telling = [x for x in lines if x.startswith(("fatal:", "!", "remote: error"))]
                return (telling or lines or [f"git push exited {result.returncode}"])[0]
        return None

    def published_state(self, branch: str) -> PublishedState:
        """How the published ``branch`` relates to this mirror's, fetched now.

        ``unreachable`` — the forge did not answer; ``absent`` — not
        published; ``same`` — published as it is here; ``behind`` — a push
        would fast-forward it; ``diverged`` — it has commits this mirror does
        not (rewritten, or continued elsewhere): a push can never succeed.

        A remote may push to several addresses (:meth:`push_urls`), and a
        push may have gone to some of them only: each is asked, and the
        branch is ``same`` only when it is the same at every one. The fetch
        address is fetched as before; any other is asked with ``ls-remote``.
        """
        if not self.push_remote:
            return "absent"
        fetch_url = _git(self.origin, "remote", "get-url", self.push_remote, check=False)
        mine = _git(
            self.origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        )
        states: list[PublishedState] = []
        for url in self.push_urls() or [fetch_url]:
            if url == fetch_url:
                fetched, theirs = self._fetch_published(branch)
            else:
                fetched, theirs = self._list_published(url, branch)
            if fetched == "unreachable":
                return "unreachable"
            if fetched != "fetched" or theirs is None:
                states.append("absent")
            elif mine == theirs:
                states.append("same")
            elif mine and self._is_ancestor(theirs, mine):
                states.append("behind")
            else:
                return "diverged"
        if all(state == "same" for state in states):
            return "same"
        return "behind" if "behind" in states else "absent"

    def _fetch_published(self, branch: str) -> tuple[str, str | None]:
        """Fetch the published ``branch`` into its remote-tracking ref.

        ``("fetched", sha)``, ``("absent", None)`` when the forge has no such
        branch, ``("unreachable", None)`` when it could not be asked.
        """
        if not self.push_remote:
            return "absent", None
        local = f"refs/heads/{branch}"
        tracking = f"refs/remotes/{self.push_remote}/{branch}"
        # At acquisition too, where a hung forge must not hold the copy
        # longer than the base fetch may (``base_fetch_timeout``).
        result = _git_remote(
            self.origin,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            self.push_remote,
            f"+{local}:{tracking}",
            timeout=min(self.base_fetch_timeout, REMOTE_TIMEOUT_SECONDS),
        )
        if result is None:
            return "unreachable", None
        if result.returncode != 0:
            if "couldn't find remote ref" in result.stderr:
                return "absent", None
            return "unreachable", None
        return "fetched", _git(self.origin, "rev-parse", tracking)

    def _list_published(self, url: str, branch: str) -> tuple[str, str | None]:
        """Ask ``url`` where ``branch`` is, without fetching; as :meth:`_fetch_published`.

        A commit the mirror does not have is still named: it is not an
        ancestor of the local branch, and that is what the caller asks.
        """
        ref = f"refs/heads/{branch}"
        if url.startswith("-"):
            return "unreachable", None  # an option, not an address
        result = _git_remote(
            self.origin,
            "ls-remote",
            "--quiet",
            url,
            ref,
            timeout=min(self.base_fetch_timeout, REMOTE_TIMEOUT_SECONDS),
        )
        if result is None or result.returncode != 0:
            return "unreachable", None
        for line in result.stdout.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == ref:
                return "fetched", sha.strip()
        return "absent", None

    def has_copy(self, key: str) -> bool:
        """Whether this pool holds a working copy of task ``key`` on this host."""
        return bool(_KEY_RE.match(key)) and (self.path_for(key) / ".git").exists()

    def reopen(self, key: str) -> Workspace | None:
        """Take the copy of ``key`` as it is, without refreshing anything; None if absent.

        For a copy whose run is being closed (restart recovery): its work is
        saved and published, not continued, so no base is fetched and no
        neighbour is placed. ``base_commit`` is where the branch was cut, so
        :meth:`Workspace.commit` sees every commit of the task's own.
        :class:`WorkspaceBusyError` if another process holds it. Give it back
        with :meth:`release` (outcome ``failed``: the copy stays).
        """
        if not self.has_copy(key):
            return None
        branch = self.branch_for(key)
        path = self.path_for(key)
        lock_fd = self._lock(key)
        try:
            self._verify(path, branch)
            base_branch, base_commit = self._recorded_base(branch)
            head = _git(path, "rev-parse", "HEAD")
        except Exception:
            os.close(lock_fd)
            raise
        return Workspace(
            key=key,
            branch=branch,
            path=path,
            base_commit=base_commit or head,
            reused=True,
            base_branch=base_branch,
            repository_key=self.repository_key,
            base_revision=base_commit,
            repo_dir=self.repo_dir,
            _lock_fd=lock_fd,
        )

    @property
    def base_branch(self) -> str:
        """Name of the branch copies are cut from, resolved once per call.

        ``base_ref`` may be a literal ref name, or the default ``HEAD`` — which
        is not a branch and cannot be a fetch destination. In the latter case
        the repository's own HEAD says which branch it means.
        """
        if self.base_ref != "HEAD":
            return self.base_ref
        head = _git(self.origin, "symbolic-ref", "--quiet", "HEAD", check=False)
        return head.removeprefix("refs/heads/") or "main"

    def acquire(self, key: str, base_branch: str | None = None) -> Workspace:
        """Take exclusive ownership of this task's copy, creating it if needed.

        Reuse is the normal path after a restart: the same key gives back the
        same copy with its uncommitted changes intact.

        ``base_branch`` is the task's own base (``customFields.baseBranch``,
        a feature branch of TAI-ADR-0047). A copy is then cut from that branch
        as the forge has it, and a branch the forge does not have fails the
        acquisition — falling back to the default base would hand the agent
        the wrong code without anyone noticing. ``None`` keeps the pool's
        default base.
        """
        if not _KEY_RE.match(key):
            raise WorkspaceError(f"unsafe workspace key: {key!r}")
        if base_branch is not None:
            _check_branch_name(base_branch)
        branch = self.branch_for(key)
        container = self.container_for(key)
        path = self.path_for(key)
        lock_fd = self._lock(key)
        try:
            # Drop records of copies deleted behind git's back, otherwise
            # ``worktree add`` refuses the path as already registered.
            _git(self.origin, "worktree", "prune")
            self._adopt_flat_copy(container, path)
            base_ref = self._refresh_base(base_branch)
            wanted = base_branch or self.base_branch
            self._settle_base(path, branch, wanted)
            reused = path.exists()
            if reused:
                self._verify(path, branch)
            else:
                self._create(path, branch, base_ref, wanted)
            self._catch_up(path, branch)
            conventions: Conventions | None = None
            placed: tuple[tuple[str, Path, str], ...] = ()
            base = _git(path, "rev-parse", "HEAD")
            if self.conventions is not None:
                conventions_base = self._base_of(branch, base_ref)
                conventions = self.conventions(self.origin, conventions_base, base, base_ref)
                placed = self._place_pinned(
                    path if conventions.inside else container, conventions.neighbours
                )
                neighbours = {n.name: n.revision for n in conventions.neighbours}
            else:
                neighbours = self._place_neighbours(container)
                # Read for setup and checks (``setup_command.py``,
                # ``checks.py``); a base that cannot be told blocks the checks
                # there and skips setup, not every run here.
                try:
                    conventions_base = self._base_of(branch, base_ref)
                except WorkspaceError:
                    conventions_base = ""
            _, base_revision = self._recorded_base(branch)
        except Exception:
            os.close(lock_fd)
            raise
        return Workspace(
            key=key,
            branch=branch,
            path=path,
            base_commit=base,
            reused=reused,
            neighbours=neighbours,
            base_branch=wanted,
            repository_key=self.repository_key,
            base_revision=base_revision,
            conventions=conventions,
            conventions_base=conventions_base,
            placed=placed,
            repo_dir=self.repo_dir,
            base_refs=self._base_refs(wanted),
            refresh_base=functools.partial(self._track_base, wanted),
            _lock_fd=lock_fd,
        )

    def release(self, workspace: Workspace, outcome: Outcome) -> None:
        """Give the copy back. Cleanup never destroys unmerged work.

        A failed or suspended run keeps its copy verbatim — that is the state a
        later attempt resumes from. A successful one may drop the working copy,
        but the branch always stays: the commit is the evidence. The disk
        budget never prunes a copy whose branch waits to be published again
        (:meth:`pinned_tasks`).
        """
        try:
            if outcome == "succeeded" and not self.keep_on_success:
                self._remove(workspace)
            self._enforce_disk_budget(keep={workspace.key})
        finally:
            if workspace._lock_fd is not None:
                os.close(workspace._lock_fd)
                workspace._lock_fd = None

    def discard(self, key: str) -> str | None:
        """Remove a task's copy and branch unless they hold work; the work, if any.

        For a task that moved to another repository of the catalog
        (``catalog.py``): a copy nobody wrote to and a branch without commits
        of its own go, anything else stays verbatim and is described in the
        return value — this module never destroys work. The branch in the
        forge is not touched.
        """
        if not _KEY_RE.match(key):
            raise WorkspaceError(f"unsafe workspace key: {key!r}")
        branch = self.branch_for(key)
        path = self.path_for(key)
        lock_fd = self._lock(key)
        try:
            _git(self.origin, "worktree", "prune")
            if path.exists() and _git(path, "status", "--porcelain", check=False):
                return "uncommitted changes"
            has_branch = self._has_ref(f"refs/heads/{branch}")
            if has_branch:
                _, base_commit = self._recorded_base(branch)
                own = self._own_commits(branch, base_commit)
                if own:
                    return f"{own} commit(s) of its own on {branch}"
            stray = self._stray_commits(path)
            if stray:
                return f"{stray} commit(s) on a detached HEAD that no branch has"
            if self._stashed(branch):
                # A stash survives the copy in the shared refs/stash, so it is
                # not lost; still, a person should know it is there.
                logger.warning("%s: a stash made on %s stays behind", key, branch)
            self._drop_nested(path)
            if path.exists():
                # Clean (checked above): what is left is ignored files.
                _git(self.origin, "worktree", "remove", "--force", str(path))
            self._drop_neighbours(self.container_for(key))
            if has_branch:
                _git(self.origin, "branch", "-D", branch)
            return None
        finally:
            os.close(lock_fd)

    # -- internals -------------------------------------------------------------

    def _lock(self, key: str) -> int:
        import fcntl

        locks = self.root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        fd = os.open(locks / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WorkspaceBusyError(f"workspace {key} is held by another process") from exc
        return fd

    def _refresh_base(self, base_branch: str | None = None) -> str:
        """Fetch the base branch and return the ref a copy should be cut from.

        Without this the runner branches from whatever it had when it was last
        updated by hand, and the agent fixes code that no longer exists — the
        work then arrives as a branch whose diff looks like it reverts whatever
        landed meanwhile. Observed for real: a task branched one commit behind,
        and its diff read as an undo of the change in between.

        The fetch lands in the remote-tracking ref rather than in the local
        branch, and that ref is what we branch from. Updating the local branch
        directly is what git refuses when a working copy has it checked out —
        true for a plain repository, and true on a runner the moment someone
        opens a copy of the base for themselves. The tracking ref is also what
        tells a merge of the base from a change of the task's own
        (``Workspace.base_refs``): a task that pulled its base with
        ``git pull`` or merged ``FETCH_HEAD`` leaves no ref of it behind.

        Best-effort on purpose. A runner may have no upstream at all, and an
        unreachable forge must not stop work that can proceed from what is
        already on disk — so the fallback is the configured ``base_ref``.

        A task's own ``base_branch`` is stricter, see :meth:`_refresh_task_base`.
        """
        if base_branch is not None:
            return self._refresh_task_base(base_branch)
        if not self.push_remote:
            return self.base_ref
        return self._track_base(self.base_branch) or self.base_ref

    def _track_base(self, base_branch: str) -> str | None:
        """Fetch the base branch into its remote-tracking ref; the ref, or None.

        Best-effort: a failure is logged and leaves the ref where it was. Only
        the tracking ref moves — never a branch, least of all a task's.
        """
        if not self.push_remote:
            return None
        result = self._fetch_base(base_branch)
        if result.returncode != 0:
            logger.warning(
                "could not refresh %s from %s: %s",
                base_branch,
                self.push_remote,
                redact_local_paths(result.stderr.strip())[:200],
            )
            return None
        return self._tracking_ref(base_branch)

    def _tracking_ref(self, base_branch: str) -> str:
        return f"refs/remotes/{self.push_remote}/{base_branch}"

    def _fetch_base(self, base_branch: str) -> subprocess.CompletedProcess[str]:
        refspec = f"+refs/heads/{base_branch}:{self._tracking_ref(base_branch)}"
        return _run_bounded(
            ["git", "fetch", "--quiet", self.push_remote, refspec],
            self.origin,
            self.base_fetch_timeout,
        )

    def _refresh_task_base(self, base_branch: str) -> str:
        """Fetch a task's own base branch and return the ref to cut from.

        Unlike the default base, a missing branch is an error: the fallback
        would be the default branch, and work cut from it arrives at review
        as a diff against the wrong line. Only an unreachable forge (not a
        branch the forge says it lacks) falls back to a copy the mirror
        already holds, the same bargain as the default base.

        The fetch lands in a ref of its own rather than in FETCH_HEAD: tasks
        of different features acquire copies concurrently, and a shared
        FETCH_HEAD would let one task be cut from another's branch.
        """
        local = f"refs/heads/{base_branch}"
        if not self.push_remote:
            if not self._has_ref(local):
                raise WorkspaceError(f"base branch {base_branch} does not exist in the repository")
            return local
        tracking = self._tracking_ref(base_branch)
        result = self._fetch_base(base_branch)
        if result.returncode == 0:
            return tracking
        stderr = result.stderr.strip()
        if "couldn't find remote ref" in stderr:
            raise WorkspaceError(f"base branch {base_branch} does not exist in {self.push_remote}")
        logger.warning(
            "could not refresh %s from %s: %s",
            base_branch,
            self.push_remote,
            redact_local_paths(stderr)[:200],
        )
        for ref in (tracking, local):
            if self._has_ref(ref):
                return ref
        raise WorkspaceError(
            f"base branch {base_branch} could not be fetched from {self.push_remote} "
            "and is not in the mirror"
        )

    def _has_ref(self, ref: str) -> bool:
        return bool(_git(self.origin, "rev-parse", "--verify", "--quiet", ref, check=False))

    def _recorded_base(self, branch: str) -> tuple[str, str]:
        """(base branch, base commit) the task branch was cut from, as recorded.

        Branches made before the record existed were cut from the default base,
        so that is what an absent record means; their base commit is unknown.
        """
        recorded = _git(
            self.origin, "config", "--get", f"branch.{branch}.{_BASE_BRANCH_KEY}", check=False
        )
        commit = _git(
            self.origin, "config", "--get", f"branch.{branch}.{_BASE_COMMIT_KEY}", check=False
        )
        return recorded or self.base_branch, commit

    def _settle_base(self, path: Path, branch: str, wanted: str) -> None:
        """Make sure an existing task branch was cut from the base asked for now.

        A task whose ``baseBranch`` changed since its copy was made (set after
        a first attempt, or moved to another feature) must not silently resume
        on the old line: its work would be reviewed and merged against the
        wrong branch. A branch that holds no work yet is recreated from the new
        base; one that holds work — uncommitted changes or commits of its own —
        is refused with the reason, because moving that work is a person's
        decision and this module never destroys it.
        """
        if not self._has_ref(f"refs/heads/{branch}"):
            return
        recorded, base_commit = self._recorded_base(branch)
        if recorded == wanted:
            return
        if path.exists() and _git(path, "status", "--porcelain"):
            raise WorkspaceError(
                f"{branch} was cut from {recorded}, the task now asks for {wanted}, "
                "and the copy holds uncommitted changes; move them by hand"
            )
        own = self._own_commits(branch, base_commit)
        if own:
            raise WorkspaceError(
                f"{branch} was cut from {recorded}, the task now asks for {wanted}, "
                f"and the branch holds {own} commit(s) of its own; move them by hand"
            )
        stray = self._stray_commits(path)
        if stray:
            raise WorkspaceError(
                f"{branch} was cut from {recorded}, the task now asks for {wanted}, "
                f"and the copy's detached HEAD holds {stray} commit(s) no branch has; "
                "move them by hand"
            )
        logger.info("recreating %s: cut from %s, task now asks for %s", branch, recorded, wanted)
        self._drop_nested(path)
        if path.exists():
            # The copy is clean (checked above): whatever is left is ignored
            # files, caches and a .venv, which --force may drop.
            _git(self.origin, "worktree", "remove", "--force", str(path))
        _git(self.origin, "branch", "-D", branch)

    def _own_commits(self, branch: str, base_commit: str) -> int:
        """Commits on the task branch past the point it was cut from."""
        if base_commit:
            spec = [f"{base_commit}..refs/heads/{branch}"]
        else:
            # No record of the cut point: count what no other ref reaches.
            # Refs only: ``--all`` would count the copy's own HEAD, which
            # reaches every commit of the branch.
            spec = [f"refs/heads/{branch}", "--not", f"--exclude={branch}", "--branches"]
            spec += ["--tags", "--remotes"]
        return int(_git(self.origin, "rev-list", "--count", *spec) or "0")

    def _stray_commits(self, path: Path) -> int:
        """Commits the copy's HEAD reaches that no branch, tag or remote does.

        Work committed on a detached HEAD is on no branch: removing the copy
        removes its HEAD and reflog with it, and the commits are gone. The
        task branch counts as a ref here; its own commits are counted apart.
        """
        if not path.exists():
            return 0
        count = _git(
            path,
            "rev-list",
            "--count",
            "HEAD",
            "--not",
            "--branches",
            "--tags",
            "--remotes",
            check=False,
        )
        return int(count or "0")

    def _stashed(self, branch: str) -> bool:
        """Whether a stash entry was made on ``branch`` (``On``/``WIP on <branch>:``)."""
        # ``stash list`` wants a work tree; the mirror is bare, refs/stash is shared.
        entries = _git(self.origin, "log", "-g", "--format=%gs", "refs/stash", check=False)
        return any(
            line.startswith((f"On {branch}:", f"WIP on {branch}:")) for line in entries.splitlines()
        )

    def _adopt_flat_copy(self, container: Path, path: Path) -> None:
        """Move a copy made before containers existed into the new layout.

        Runners carry unfinished work: copies of failed and suspended runs are
        exactly the state a later attempt resumes from. Recreating them under
        the new layout would either fork a second copy of the branch or make
        git refuse the branch as already checked out, so the existing copy is
        moved rather than abandoned — with its uncommitted changes.
        """
        if path.exists() or not (container / ".git").exists():
            return
        staging = self.root / f".adopt-{container.name}"
        # Two moves because git cannot move a worktree into a subdirectory of
        # itself, and the container is exactly that.
        _git(self.origin, "worktree", "move", str(container), str(staging))
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(self.origin, "worktree", "move", str(staging), str(path))
        logger.info("adopted %s into the container layout", container.name)

    # -- neighbours ------------------------------------------------------------

    def _place_neighbours(self, container: Path) -> dict[str, str]:
        """Lay out the repositories this copy must build against.

        Returns the revision each one was placed at, which travels into the
        checkpoint: a green test run is only meaningful together with the
        revisions it ran against.
        """
        if not self.neighbours:
            return {}
        ref = self._refresh_superproject()
        placed: dict[str, str] = {}
        for neighbour in self.neighbours:
            revision = self._pinned_revision(neighbour, ref)
            self._place_neighbour(container / neighbour.path, neighbour, revision)
            placed[neighbour.path] = revision
        return placed

    def _pinned_revision(self, neighbour: Neighbour, ref: str) -> str:
        """Revision the superproject pins for this neighbour, from its tree."""
        assert self.superproject is not None  # guarded in __init__
        entry = _git(self.superproject, "ls-tree", ref, neighbour.path)
        fields = entry.split()
        if len(fields) < 3 or fields[0] != _GITLINK_MODE:
            raise WorkspaceError(
                f"{neighbour.path} is not a submodule of the superproject at {ref}"
            )
        return fields[2]

    def _ensure_neighbour_revision(self, neighbour: Neighbour, revision: str) -> None:
        """Fetch the neighbour mirror when it does not yet hold the pinned commit.

        The superproject moves its pin whenever the neighbour's own main
        moves; a bare mirror on the runner only knows what it was last told.
        Observed for real: the pin advanced, the mirror did not, and every
        task died in `git worktree add` with "invalid reference" until someone
        fetched by hand. Best-effort: an unreachable forge leaves the clear
        error that follows, not a silent stale checkout.
        """
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=neighbour.origin,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return
        result = subprocess.run(
            # Into remote-tracking refs, never over branches: the mirror may be
            # a pool's too, and its task branches are not the forge's to move.
            ["git", "fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            cwd=neighbour.origin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "could not fetch neighbour %s for %s: %s",
                neighbour.path,
                revision[:12],
                redact_local_paths(result.stderr.strip())[:200],
            )
        else:
            logger.info("fetched neighbour %s to reach %s", neighbour.path, revision[:12])

    def _place_neighbour(self, dest: Path, neighbour: Neighbour, revision: str) -> None:
        self._ensure_neighbour_revision(neighbour, revision)
        if not dest.exists():
            _git(neighbour.origin, "worktree", "prune")
            _git(neighbour.origin, "worktree", "add", "--detach", str(dest), revision)
            return
        if _git(dest, "rev-parse", "HEAD", check=False) == revision:
            return
        if _git(dest, "status", "--porcelain", check=False):
            # Somebody — the agent, or a person debugging — has work in there.
            # Moving the checkout under it would destroy that work silently,
            # and this module never does that.
            logger.info("keeping local changes in %s; left off the pinned revision", neighbour.path)
            return
        _git(dest, "checkout", "--quiet", "--detach", revision)

    def _base_refs(self, base_branch: str) -> tuple[str, ...]:
        """Refs of the base branch in the copy: the forge's copy of it, then its own.

        Not filtered by what exists now: the task may fetch its base during
        the run, and ``Workspace.refresh_base`` fetches it again before the
        pointers are checked.
        """
        refs = [f"refs/heads/{base_branch}"]
        if self.push_remote:
            refs.insert(0, f"refs/remotes/{self.push_remote}/{base_branch}")
        return tuple(refs)

    def _base_of(self, branch: str, base_ref: str) -> str:
        """The commit a catalog run reads its conventions from: the task's base.

        The point the task branch was cut from, as recorded; for a branch
        without the record, the point it shares with the base; for a branch
        that does not exist yet, the base itself. Never the branch's own
        head: a change of ``runner.yaml`` on the task branch takes effect only
        once it is reviewed and merged (FR-009).
        """
        _, recorded = self._recorded_base(branch)
        if recorded:
            return recorded
        fork = _git(self.origin, "merge-base", f"refs/heads/{branch}", base_ref, check=False)
        return fork or _git(self.origin, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

    def _place_pinned(
        self, root: Path, neighbours: Sequence[PinnedNeighbour]
    ) -> tuple[tuple[str, Path, str], ...]:
        """Put each neighbour of a catalog run at its pinned revision under ``root``.

        A neighbour left with changes, or with commits no ref of its mirror
        has, by an earlier run is not moved and not worked beside:
        :class:`WorkspaceBlocked` with :data:`NEIGHBOUR_MODIFIED`, and a
        person decides what becomes of that work.
        """
        placed = []
        for neighbour in neighbours:
            dest = root / neighbour.path
            self._place_one(dest, neighbour)
            placed.append((neighbour.name, dest, neighbour.revision))
        return tuple(placed)

    def _place_one(self, dest: Path, neighbour: PinnedNeighbour) -> None:
        if (dest / ".git").exists():
            owner = self._neighbour_owner(dest)
            dirty = _git(dest, "status", "--porcelain", check=False)
            stray = _git(
                dest,
                "rev-list",
                "--count",
                "HEAD",
                "--not",
                "--branches",
                "--tags",
                "--remotes",
                check=False,
            )
            if dirty or (stray and stray != "0"):
                what = "changed or new files" if dirty else f"{stray} commit(s) of its own"
                raise WorkspaceBlocked(
                    NEIGHBOUR_MODIFIED,
                    f"neighbour {neighbour.name} ({neighbour.path}) holds {what}, and "
                    "neighbours are read-only; move the change to a task of that repository "
                    "or drop it, then return the task",
                )
            if owner == neighbour.origin.resolve():
                if _git(dest, "rev-parse", "HEAD", check=False) != neighbour.revision:
                    _git(dest, "checkout", "--quiet", "--detach", neighbour.revision)
                _keep_pin(neighbour)
                prune_pins(neighbour.origin)
                return
            if not (dest / ".git").is_file():
                raise WorkspaceBlocked(
                    NEIGHBOUR_MODIFIED,
                    f"{neighbour.path} is a repository of its own, not a copy of neighbour "
                    f"{neighbour.name}; move it away, then return the task",
                )
            # Clean, and cut from another repository (a copy of the
            # one-repository form, or another mirror): made again below.
            common = _git(dest, "rev-parse", "--git-common-dir")
            _git((dest / common).resolve(), "worktree", "remove", "--force", str(dest))
        elif dest.exists() and any(dest.iterdir()):
            raise WorkspaceBlocked(
                NEIGHBOUR_MODIFIED,
                f"{neighbour.path} holds files that are no copy of neighbour {neighbour.name}; "
                "move them away, then return the task",
            )
        _git(neighbour.origin, "worktree", "prune")
        # An empty directory is what a checkout leaves for a submodule.
        _git(neighbour.origin, "worktree", "add", "--detach", str(dest), neighbour.revision)
        _keep_pin(neighbour)
        prune_pins(neighbour.origin)

    def _refresh_superproject(self) -> str:
        """Update the pin source, best-effort, and return the ref to read.

        Same bargain as ``_refresh_base``: an unreachable forge must not stop
        work that can proceed from what is already on disk. Without a remote
        the mirror is whatever the deployment last put there — deliberate, so
        that a pool never guesses which remote a superproject belongs to.
        """
        assert self.superproject is not None  # guarded in __init__
        if not self.superproject_remote:
            return self.superproject_ref
        ref = self.superproject_ref if self.superproject_ref != "HEAD" else "HEAD"
        result = subprocess.run(
            ["git", "fetch", "--quiet", self.superproject_remote, ref],
            cwd=self.superproject,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "could not refresh the superproject from %s: %s",
                self.superproject_remote,
                redact_local_paths(result.stderr.strip())[:200],
            )
            return self.superproject_ref
        return "FETCH_HEAD"

    def _create(self, path: Path, branch: str, base_ref: str, base_branch: str) -> None:
        exists = self._has_ref(f"refs/heads/{branch}") or self._adopt_published(
            branch, base_ref, base_branch
        )
        if exists:
            # The branch outlived its working copy (cleanup after success, or a
            # pruned copy): continue it instead of forking a second line. Note
            # it is NOT rebased onto the refreshed base — a second attempt must
            # resume the work, not silently move it.
            _git(self.origin, "worktree", "add", str(path), branch)
        else:
            _git(self.origin, "worktree", "add", str(path), "-b", branch, base_ref)
            # What the branch was cut from, so a later attempt can tell whether
            # the task still asks for the same base (``_settle_base``).
            config = f"branch.{branch}"
            _git(self.origin, "config", f"{config}.{_BASE_BRANCH_KEY}", base_branch)
            head = _git(path, "rev-parse", "HEAD")
            _git(self.origin, "config", f"{config}.{_BASE_COMMIT_KEY}", head)

    def _adopt_published(self, branch: str, base_ref: str, base_branch: str) -> bool:
        """Take the task branch from the forge when this mirror does not have it.

        A task returned by its verification (a rejected review, a merge that
        failed) is taken again, possibly by another runner or after the mirror
        was rebuilt. Its published branch is where the work and the review
        history are: cutting a fresh one from the base would fork a second
        line that the never-forced push then cannot publish. Best-effort like
        the base refresh: a branch the forge lacks, or an unreachable forge,
        leaves the copy to be cut from the base as before.
        """
        if not self.push_remote:
            return False
        local = f"refs/heads/{branch}"
        # FETCH_HEAD may be the base this copy is about to be cut from.
        result = _git_remote(
            self.origin,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            self.push_remote,
            f"{local}:{local}",
        )
        if result is None:
            return False
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "couldn't find remote ref" not in stderr:
                logger.warning(
                    "could not look for %s in %s: %s",
                    branch,
                    self.push_remote,
                    redact_local_paths(stderr)[:200],
                )
            return False
        # Recorded as if cut here, from the point it shares with the base, so
        # a later change of base still sees the commits of its own.
        config = f"branch.{branch}"
        _git(self.origin, "config", f"{config}.{_BASE_BRANCH_KEY}", base_branch)
        fork = _git(self.origin, "merge-base", local, base_ref, check=False)
        if fork:
            _git(self.origin, "config", f"{config}.{_BASE_COMMIT_KEY}", fork)
        logger.info("continuing %s as published in %s", branch, self.push_remote)
        return True

    def _catch_up(self, path: Path, branch: str) -> None:
        """Move the task branch to its published head when that only adds to it.

        Replicas share the forge, not their mirrors: a task this replica
        worked before may have gone on elsewhere — saved as WIP before a
        ``blocked`` status, or returned by its verification — and its branch
        here is behind. Resuming from the old head would lose that work and
        fork a line the never-forced push cannot publish. So the published
        branch is fetched and, when the local one is its ancestor, the local
        one is fast-forwarded (a copy with uncommitted changes is not moved).
        A branch that diverged is left as it is, with a warning: that is a
        person's business. Best-effort: an unreachable forge changes nothing.
        """
        if not self.push_remote:
            return
        state, theirs = self._fetch_published(branch)
        if state != "fetched" or theirs is None:
            return  # not published, or the forge is unreachable
        local = f"refs/heads/{branch}"
        mine = _git(self.origin, "rev-parse", local)
        if mine == theirs or self._is_ancestor(theirs, mine):
            return  # the same, or this replica is ahead (its push is pending)
        if not self._is_ancestor(mine, theirs):
            logger.warning(
                "%s here and in %s have diverged; continuing the local one",
                branch,
                self.push_remote,
            )
            return
        if path.exists():
            if _git(path, "status", "--porcelain"):
                logger.warning(
                    "%s is behind %s, but the copy holds uncommitted changes; not moved",
                    branch,
                    self.push_remote,
                )
                return
            _git(path, "merge", "--ff-only", "--quiet", theirs)
        else:
            _git(self.origin, "update-ref", local, theirs, mine)
        logger.info("%s caught up with %s (%s)", branch, self.push_remote, theirs[:12])

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.origin,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def _verify(self, path: Path, branch: str) -> None:
        if not (path / ".git").exists():
            raise WorkspaceError(f"{path.name} exists but is not a git worktree")
        current = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            # Silently switching would mix two tasks in one copy — the exact
            # failure this module exists to prevent.
            raise WorkspaceError(f"{path.name} is on branch {current}, expected {branch}")

    def _remove(self, workspace: Workspace) -> None:
        if workspace.is_dirty:
            logger.info("keeping %s: uncommitted changes", workspace.key)
            return
        # is_dirty above already proved the copy holds no work: `status
        # --porcelain` is empty, so every remaining file is ignored (a .venv,
        # caches). Without --force git still refuses to drop a copy that has
        # ignored files, and on a runner that leaves ~130 MB per finished task
        # behind until the disk budget prunes it. Forcing here destroys nothing
        # that could be evidence.
        self._drop_nested(workspace.path)
        _git(self.origin, "worktree", "remove", "--force", str(workspace.path), check=False)
        if workspace.path.exists():
            logger.info("keeping %s: git declined to remove the worktree", workspace.key)
            return
        self._drop_neighbours(workspace.container)

    def _drop_neighbours(self, container: Path) -> None:
        """Take the neighbours down with the copy they were placed for.

        They are cheap to recreate and hold no work of their own — the agent's
        changes live on the task branch of the working copy — but they are
        worktrees, so leaving them behind leaves both disk and stale records in
        their own repositories.
        """
        for neighbour in self.neighbours:
            dest = container / neighbour.path
            if dest.exists():
                _git(neighbour.origin, "worktree", "remove", str(dest), check=False)
        if container.is_dir():
            self._clear(container, container / self.repo_dir)
        with contextlib.suppress(OSError):
            container.rmdir()  # only when nothing else is left in it

    def _clear(self, directory: Path, own: Path) -> None:
        """Drop the neighbours placed under ``directory`` and the directories they leave empty.

        Placed by runner.yaml of some base revision: what is there is found on
        disk, so a copy made before a restart goes as well. A directory that
        is no copy (``services/``, ``sdk/`` of a layout with segments) is
        looked into; a copy is never entered, and ``own`` — the task's copy —
        is left alone.
        """
        for child in directory.iterdir():
            if child == own or child.is_symlink() or not child.is_dir():
                continue
            if (child / ".git").exists():
                self._drop_placed(child)
            else:
                self._clear(child, own)
            with contextlib.suppress(OSError):
                child.rmdir()  # only when nothing else is left in it

    def _drop_nested(self, copy: Path) -> None:
        """Remove neighbours placed inside ``copy`` (submodules of a superproject task).

        Before the copy itself: removing the copy would take their files and
        leave their mirrors with records of worktrees that no longer exist.
        """
        if self.neighbour_mirrors is None or not (copy / ".git").exists():
            return
        for line in _git(copy, "ls-files", "--stage", check=False).splitlines():
            mode, _, rest = line.partition(" ")
            if mode == _GITLINK_MODE:
                self._drop_placed(copy / rest.split("\t", 1)[-1])

    def _drop_placed(self, dest: Path) -> None:
        """Remove ``dest`` if it is a clean copy cut from a neighbour mirror."""
        owner = self._neighbour_owner(dest)
        if owner is not None:
            # Without --force: a neighbour with changes stays for a person.
            _git(owner, "worktree", "remove", str(dest), check=False)
            prune_pins(owner)

    def _neighbour_owner(self, dest: Path) -> Path | None:
        """The neighbour mirror ``dest`` is a worktree of; None if it is none of them."""
        if self.neighbour_mirrors is None or not (dest / ".git").is_file():
            return None
        common = _git(dest, "rev-parse", "--git-common-dir", check=False)
        if not common:
            return None
        owner = (dest / common).resolve()
        return owner if owner.parent == self.neighbour_mirrors else None

    def _enforce_disk_budget(self, *, keep: Collection[str]) -> None:
        """Bound how much disk idle copies may hold. Branches are never touched.

        The copies named in ``keep`` are neither pruned nor counted, and
        neither are those of :meth:`pinned_tasks`: they are read here, so no
        caller of :meth:`release` can forget them. A list that cannot be
        read prunes nothing this time.
        """
        pinned = self.pinned_tasks()
        if pinned is None:
            return
        candidates = sorted(self._idle_workspaces(keep={*keep, *pinned}), key=lambda item: item[1])
        excess = len(candidates) - self.max_workspaces
        for container, _ in candidates[: max(0, excess)]:
            self._drop_nested(container / self.repo_dir)
            _git(self.origin, "worktree", "remove", str(container / self.repo_dir), check=False)
            if (container / self.repo_dir).exists():
                continue
            self._drop_neighbours(container)
            logger.info("pruned idle workspace %s", container.name)

    def pinned_tasks(self) -> set[str] | None:
        """Tasks of this pool whose branches wait on the unpublished list; None if unreadable.

        The list lies in the root of the replica's copies (``publish.py``).
        An entry is pushed again only while its task has a copy here, and a
        copy pruned under it would drop the entry without a trace in the core.
        """
        # publish.py builds on this module: imported when first needed.
        from control_plane_agent.publish import UnpublishedLedger

        try:
            entries = UnpublishedLedger(self.root).read()
        except OSError as exc:
            logger.warning("the unpublished list cannot be locked (%s); no copy pruned", exc)
            return None
        if entries is None:
            # A list that is there but not parsed: which copies wait is unknown.
            logger.warning("the unpublished list cannot be parsed; no copy pruned")
            return None
        return {
            e.task
            for e in entries
            if e.repository_key == self.repository_key and self.branch_for(e.task) == e.branch
        }

    def _owns(self, copy: Path) -> bool:
        """Whether ``copy`` is a worktree of this pool's origin.

        Pools of a catalog share one root (``catalog.py``), and a directory
        named like this pool's copy may be a neighbour placed for another
        repository's task.
        """
        common = _git(copy, "rev-parse", "--git-common-dir", check=False)
        if not common:
            return False
        resolved = (copy / common).resolve()
        return resolved in (self.origin, self.origin / ".git")

    def _idle_workspaces(self, *, keep: Collection[str]) -> Iterable[tuple[Path, float]]:
        import fcntl

        for path in self.root.iterdir():
            if not path.is_dir() or path.name == ".locks" or path.name in keep:
                continue
            copy = path / self.repo_dir
            if not copy.is_dir() or not self._owns(copy):
                continue  # not a container of ours: leave it alone
            if _git(copy, "status", "--porcelain", check=False):
                continue  # holds uncommitted work
            lock_path = self.root / ".locks" / f"{path.name}.lock"
            if lock_path.exists():
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    continue  # someone is working in it right now
                finally:
                    os.close(fd)
            yield path, path.stat().st_mtime


def remove_pool(root: Path | str) -> None:  # pragma: no cover - operational helper
    """Delete a pool root outright. For tests and manual cleanup only."""
    shutil.rmtree(Path(root), ignore_errors=True)
