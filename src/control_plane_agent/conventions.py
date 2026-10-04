"""What a catalog run is prepared by: conventions at the base revision (universal-runner U006).

A repository of the catalog says itself what the daemon prepares for its
tasks, in ``.agents/runner.yaml`` (``runner_config.py``). The file is read
with ``git show <base>:.agents/runner.yaml`` from the mirror, at the commit the
task branch was cut from — never from the branch: a change of the file on a
task branch takes effect once it is reviewed and merged (FR-009,
TAI-ADR-0063 §4).

Neighbours are keys of the catalog. The address of each is the catalog's
(FR-002); its revision is the one the superproject pins at its base revision
(FR-006): the gitlink of its submodule in the superproject's tree, the
submodule found at the entry's directory, or else by the repository name of
its ``.gitmodules`` URL. For a task of the superproject itself the pins are
read from the head of the task's own branch (its base, until the task merges a
newer one), and the neighbours are placed inside the copy, at their submodule
paths — a copy of the superproject with its submodules.
Neighbours are cut from mirrors of their own (``mirrors.NeighbourMirrors``),
never from a pool's.

The blob of ``AGENTS.md`` at the base goes into the checkpoint with the
revision it was read at (FR-011); the daemon adds the blob at the commit it
hands in.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from control_plane_agent.catalog import CatalogEntry, RepositoryCatalog
from control_plane_agent.mirrors import (
    MirrorError,
    NeighbourMirrors,
    RevisionMissing,
    repository_name,
)
from control_plane_agent.runner_config import (
    RUNNER_CONFIG_PATH,
    RunnerConfig,
    RunnerConfigError,
    parse_runner_config,
)
from control_plane_agent.workspace import (
    AGENTS_MD,
    NEIGHBOUR_MODIFIED,
    Conventions,
    PinnedNeighbour,
    WorkspaceBlocked,
    WorkspaceError,
    blob_of,
)

logger = logging.getLogger("control_plane_agent.conventions")

#: ``runner.yaml`` at the base cannot be used for this task; a person fixes it.
RUNNER_CONFIG_INVALID = "runner_config_invalid"
# A conventions file is a few hundred bytes; more is not one.
MAX_RUNNER_CONFIG_BYTES = 256 * 1024
_GITLINK_MODE = "160000"


def read_runner_config(repository: Path, revision: str) -> RunnerConfig | None:
    """``.agents/runner.yaml`` at ``revision``; None when the repository has none there.

    An invalid file is :class:`WorkspaceBlocked` (:data:`RUNNER_CONFIG_INVALID`)
    with the errors by field path: the base is what a person fixes, and a
    run cannot guess what the file meant.
    """
    blob = blob_of(repository, revision, RUNNER_CONFIG_PATH)
    if blob is None:
        return None
    size = _git(repository, "cat-file", "-s", blob)
    if int(size or "0") > MAX_RUNNER_CONFIG_BYTES:
        raise WorkspaceBlocked(
            RUNNER_CONFIG_INVALID,
            f"{RUNNER_CONFIG_PATH} at {revision[:12]} is larger than "
            f"{MAX_RUNNER_CONFIG_BYTES} bytes",
        )
    text = subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=repository, capture_output=True, check=True
    ).stdout
    try:
        return parse_runner_config(text)
    except RunnerConfigError as exc:
        raise WorkspaceBlocked(
            RUNNER_CONFIG_INVALID,
            f"{RUNNER_CONFIG_PATH} at {revision[:12]} is invalid: "
            + "; ".join(map(str, exc.errors)),
        ) from None


class CatalogConventions:
    """The :data:`workspace.ConventionsReader` of one repository of a catalog."""

    def __init__(
        self, catalog: RepositoryCatalog, entry: CatalogEntry, mirrors: NeighbourMirrors
    ) -> None:
        self.catalog = catalog
        self.entry = entry
        self.mirrors = mirrors

    @property
    def is_superproject(self) -> bool:
        return self.catalog.superproject == self.entry.key

    def __call__(self, origin: Path, base: str, head: str = "", base_ref: str = "") -> Conventions:
        config = read_runner_config(origin, base)
        agents_md = blob_of(origin, base, AGENTS_MD)
        if config is None:
            logger.info("%s has no %s at %s", self.entry.key, RUNNER_CONFIG_PATH, base[:12])
        if config is None or not config.neighbours:
            # A superproject without neighbours still holds its submodules to
            # the base's pointers (``Workspace.neighbour_check``).
            return Conventions(
                revision=base, config=config, agents_md=agents_md, inside=self.is_superproject
            )
        entries = [self._neighbour(name) for name in config.neighbours]
        inside = self.is_superproject
        pins, at = (origin, base) if inside else self._superproject_base()
        neighbours = []
        for entry in entries:
            submodule = _submodule_path(pins, at, entry)
            if submodule is None:
                raise WorkspaceBlocked(
                    RUNNER_CONFIG_INVALID,
                    f"neighbour {entry.key} of {RUNNER_CONFIG_PATH} is not a submodule of the "
                    f"superproject at {at[:12]}",
                )
            revision = pinned = _gitlink(pins, at, submodule)
            if inside and head and _is_gitlink(origin, head, submodule):
                # A task of the superproject builds against the pointers of
                # its own branch: a merge of the base may have moved them, and
                # a copy of the base's pins would read as a rollback of it.
                revision = _gitlink(origin, head, submodule)
            try:
                mirror = self.mirrors.ensure(entry.key, entry.url)
                self.mirrors.reach(entry.key, revision)
            except RevisionMissing as exc:
                if revision == pinned:
                    raise WorkspaceError(str(exc)) from exc
                # The task branch points the submodule at a commit its forge
                # does not have (never pushed, or pushed and rewritten away):
                # no attempt can place it, a person fixes the pointer.
                merged = _merged_pointer(origin, head, base_ref, submodule, revision)
                if merged:
                    # The pointer came with a merge of a newer base: the fault
                    # is the base's, and setting it back on the branch would
                    # read as a rollback of the base at review.
                    raise WorkspaceBlocked(
                        NEIGHBOUR_MODIFIED,
                        f"the task branch took submodule {submodule} (neighbour {entry.key}) "
                        f"at {revision[:12]} from its base at {merged[:12]}, and the forge of "
                        f"{entry.key} does not have that commit; fix the base, not the task: "
                        f"push that commit to {entry.key} or move the pointer in the base, "
                        "merge the base again, then return the task",
                    ) from exc
                raise WorkspaceBlocked(
                    NEIGHBOUR_MODIFIED,
                    f"the task branch points submodule {submodule} (neighbour {entry.key}) "
                    f"at {revision[:12]}, which the forge of {entry.key} does not have; "
                    f"set the pointer back to {pinned[:12] or 'the base'} or push that "
                    f"commit to {entry.key}, then return the task",
                ) from exc
            except MirrorError as exc:
                raise WorkspaceError(str(exc)) from exc
            neighbours.append(
                PinnedNeighbour(
                    name=entry.key,
                    path=submodule if inside else entry.directory,
                    origin=mirror,
                    revision=revision,
                )
            )
        return Conventions(
            revision=base,
            config=config,
            agents_md=agents_md,
            neighbours=tuple(neighbours),
            inside=inside,
        )

    def _neighbour(self, name: str) -> CatalogEntry:
        entry = self.catalog.resolve(name)
        if entry is None:
            raise WorkspaceBlocked(
                RUNNER_CONFIG_INVALID,
                f"neighbour {name!r} of {RUNNER_CONFIG_PATH} is not a repository of this "
                "agent's catalog",
            )
        if entry.key == self.entry.key:
            raise WorkspaceBlocked(
                RUNNER_CONFIG_INVALID,
                f"{RUNNER_CONFIG_PATH} of {self.entry.key} names the repository itself "
                "as its neighbour",
            )
        return entry

    def _superproject_base(self) -> tuple[Path, str]:
        """(mirror, commit) of the superproject's base, fetched now if the forge answers."""
        key = self.catalog.superproject
        if key is None:
            raise WorkspaceError(
                f"{RUNNER_CONFIG_PATH} of {self.entry.key} names neighbours, and the catalog "
                "has no superproject to pin their revisions"
            )
        superproject = self.catalog.entries[key]
        base_ref = superproject.base_ref
        tracking = (
            "refs/remotes/origin/HEAD" if base_ref == "HEAD" else f"refs/remotes/origin/{base_ref}"
        )
        extra = ("+HEAD:refs/remotes/origin/HEAD",) if base_ref == "HEAD" else ()
        try:
            mirror = self.mirrors.ensure(key, superproject.url)
        except MirrorError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.mirrors.fetch(key, *extra)
        commit = _git(mirror, "rev-parse", "--verify", "--quiet", f"{tracking}^{{commit}}")
        if not commit:
            raise WorkspaceError(
                f"the superproject's base {base_ref} is not in its mirror; "
                "its forge could not be fetched"
            )
        return mirror, commit


def _merged_pointer(
    repository: Path, head: str, base_ref: str, submodule: str, revision: str
) -> str:
    """Where the branch last met its base, if the base had ``submodule`` at ``revision`` there.

    Empty when it did not: the pointer is then the task's own. The meeting
    point is the merge base of ``head`` and ``base_ref`` — the base the task
    merged last, by a merge commit or a fast-forward.
    """
    if not head or not base_ref:
        return ""
    met = _git(repository, "merge-base", head, base_ref)
    return met if met and _gitlink(repository, met, submodule) == revision else ""


def _submodule_path(repository: Path, revision: str, entry: CatalogEntry) -> str | None:
    """Where the superproject keeps ``entry`` as a submodule at ``revision``; None if nowhere."""
    for candidate in dict.fromkeys((entry.directory, entry.key)):
        if _is_gitlink(repository, revision, candidate):
            return candidate
    try:
        wanted = repository_name(entry.url).casefold()
    except MirrorError:
        return None
    listed = _git(
        repository,
        "config",
        "--blob",
        f"{revision}:.gitmodules",
        "--get-regexp",
        r"^submodule\..*\.url$",
    )
    for line in listed.splitlines():
        name_key, _, url = line.partition(" ")
        try:
            matches = repository_name(url).casefold() == wanted
        except MirrorError:
            continue
        if not matches:
            continue
        name = name_key.removeprefix("submodule.").removesuffix(".url")
        path = _git(
            repository, "config", "--blob", f"{revision}:.gitmodules", f"submodule.{name}.path"
        )
        if path and _is_gitlink(repository, revision, path):
            return path
    return None


def _is_gitlink(repository: Path, revision: str, path: str) -> bool:
    return _entry(repository, revision, path)[0] == _GITLINK_MODE


def _gitlink(repository: Path, revision: str, path: str) -> str:
    return _entry(repository, revision, path)[1]


def _entry(repository: Path, revision: str, path: str) -> tuple[str, str]:
    """(mode, object) of ``path`` in the tree of ``revision``; empty strings if absent."""
    fields = _git(repository, "ls-tree", revision, "--", path).split()
    return (fields[0], fields[2]) if len(fields) >= 3 else ("", "")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


__all__ = [
    "MAX_RUNNER_CONFIG_BYTES",
    "RUNNER_CONFIG_INVALID",
    "CatalogConventions",
    "read_runner_config",
]
