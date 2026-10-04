"""Neighbours, the superproject and ``runner.yaml`` of a catalog run (universal-runner U006).

A repository of the catalog names its neighbours in ``.agents/runner.yaml``
at the base revision of the task; the daemon takes their addresses from the
catalog, their revisions from the superproject at its base, and cuts them
from mirrors of their own. A neighbour is read-only: a change in it is found
and stops the run. A task of the superproject gets its copy with the
neighbours in place of its submodules.

Also here, from the review of U005: a malformed address or port of the
catalog, pools made under a lock of their key, a bounded wait for a mirror
another replica clones, and staging of clones that died.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.catalog import CatalogError, RepositoryCatalog, RepositoryPools
from control_plane_agent.checks import checks_at_base, run_check
from control_plane_agent.conventions import (
    RUNNER_CONFIG_INVALID,
    CatalogConventions,
    read_runner_config,
)
from control_plane_agent.mirrors import (
    MirrorBusyError,
    MirrorError,
    NeighbourMirrors,
    RevisionMissing,
    lock_path,
    mirror_lock,
    sweep_stale_clones,
)
from control_plane_agent.revision import AgentRevision, RevisionError, mirror, workspace_pool_of
from control_plane_agent.setup_command import run_setup, setup_at_base
from control_plane_agent.workspace import (
    NEIGHBOUR_MODIFIED,
    NEIGHBOUR_POINTER_REGRESSED,
    ExecutionWorkspacePool,
    NeighbourCheck,
    Workspace,
    WorkspaceBlocked,
    WorkspaceError,
    prune_pins,
)

NEIGHBOURS_OF_ALPHA = "version: 1\nneighbours: [beta]\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _commit(path, {"README.md": f"{path.name}\n", **files}, "initial")
    return path


def _pin(superproject: Path, path: str, revision: str, url: str | None = None) -> None:
    """Record a submodule at an exact revision, without the network."""
    _git(superproject, "update-index", "--add", "--cacheinfo", f"160000,{revision},{path}")
    if url is not None:
        _git(superproject, "config", "-f", ".gitmodules", f"submodule.{path}.path", path)
        _git(superproject, "config", "-f", ".gitmodules", f"submodule.{path}.url", url)
        _git(superproject, "add", ".gitmodules")
    _git(superproject, "commit", "-qm", f"pin {path}")


class World:
    """A forge of three repositories and the pools of one replica."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        alpha: str | None = NEIGHBOURS_OF_ALPHA,
        superproject_config: str | None = None,
        superproject: bool = True,
        beta_path: str = "beta",
        directories: dict[str, str] | None = None,
    ) -> None:
        forge = tmp_path / "forge"
        files = {"AGENTS.md": "# alpha\n"}
        if alpha is not None:
            files[".agents/runner.yaml"] = alpha
        self.alpha = _repo(forge / "alpha", files)
        self.beta = _repo(forge / "beta", {})
        self.pin = _git(self.beta, "rev-parse", "HEAD")
        # The neighbour moves on past what the superproject pins.
        self.beta_tip = _commit(self.beta, {"README.md": "later\n"}, "later")
        sp_files = (
            {} if superproject_config is None else {".agents/runner.yaml": superproject_config}
        )
        self.superproject = _repo(forge / "superproject", sp_files)
        _pin(self.superproject, beta_path, self.pin, url="https://forge.example/org/beta.git")
        spec: dict[str, Any] = {
            "repositoryField": "repositoryKey",
            "repositories": {
                "alpha": {"url": f"file://{self.alpha}"},
                "beta": {"url": f"file://{self.beta}", "publish": False},
                "superproject": {"url": f"file://{self.superproject}"},
            },
        }
        if superproject:
            spec["superproject"] = "superproject"
        for key, directory in (directories or {}).items():
            spec["repositories"][key]["directory"] = directory
        self.root = tmp_path / "w"
        self.mirrors = self.root / ".mirrors"
        pools = workspace_pool_of(
            _revision(spec), {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(self.root)}
        )
        assert isinstance(pools, RepositoryPools)
        self.pools = pools

    def pool(self, key: str) -> ExecutionWorkspacePool:
        return self.pools.pool_for(self.pools.catalog.entries[key])

    def acquire(self, key: str, task: str = "TASK-1") -> Workspace:
        return self.pool(key).acquire(task)


def _revision(working_copy: dict[str, Any]) -> AgentRevision:
    return AgentRevision(
        key="coder",
        revision=1,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec={"workingCopy": working_copy},
        status="active",
        state="running",
    )


# --- neighbours by runner.yaml, at the superproject's pin ---------------------


def test_a_neighbour_is_placed_at_the_revision_the_superproject_pins(tmp_path: Path) -> None:
    world = World(tmp_path)

    workspace = world.acquire("alpha")

    sibling = workspace.container / "beta"
    assert _git(sibling, "rev-parse", "HEAD") == world.pin != world.beta_tip
    assert (sibling / "README.md").read_text() == "beta\n"
    data = workspace.checkpoint_data
    assert data["neighbours"] == {"beta": world.pin}
    base = _git(world.alpha, "rev-parse", "main")
    assert data["conventionsRevision"] == data["baseRevision"] == base
    assert data["agentsMdBase"] == _git(world.alpha, "rev-parse", "main:AGENTS.md")
    assert workspace.conventions is not None and workspace.conventions.config is not None
    assert workspace.conventions.config.neighbours == ("beta",)
    assert workspace.neighbour_changes() == {}


def test_neighbour_mirrors_are_apart_from_the_pools(tmp_path: Path) -> None:
    """A neighbour's fetch never moves a branch of a pool of the same repository."""
    world = World(tmp_path)
    # A live pool of beta in the same replica, with work nobody published.
    beta_pool = world.pool("beta")
    work = beta_pool.acquire("TASK-9")
    (work.path / "work.txt").write_text("mine\n")
    local = work.commit("TASK-9: work")
    assert local is not None
    # The forge has a branch of the same name that says something else.
    _git(world.beta, "branch", "task/TASK-9", world.beta_tip)
    beta_mirror = world.mirrors / "beta.git"
    assert _git(beta_mirror, "rev-parse", "refs/heads/task/TASK-9") == local

    workspace = world.acquire("alpha")

    neighbours = world.mirrors / "neighbours" / "beta.git"
    common = _git(workspace.container / "beta", "rev-parse", "--git-common-dir")
    assert (workspace.container / "beta" / common).resolve() == neighbours.resolve()
    # Only remote-tracking refs in the neighbour mirror, no branch to lose.
    assert _git(neighbours, "for-each-ref", "--format=%(refname)", "refs/heads") == ""
    assert "refs/remotes/origin/main" in _git(neighbours, "for-each-ref", "--format=%(refname)")
    # The pool's task branch is where its work left it.
    assert _git(beta_mirror, "rev-parse", "refs/heads/task/TASK-9") == local
    beta_pool.release(work, "failed")


def test_runner_yaml_on_the_task_branch_does_not_apply(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    first = pool.acquire("TASK-1")
    # The task changes its conventions: no neighbours, and an invalid file.
    _commit(first.path, {".agents/runner.yaml": "version: 1\nneighbours: []\nbogus: 1\n"})
    pool.release(first, "failed")

    again = pool.acquire("TASK-1")

    assert again.reused
    assert again.checkpoint_data["neighbours"] == {"beta": world.pin}
    assert again.conventions is not None and again.conventions.config is not None
    assert again.conventions.config.neighbours == ("beta",)
    pool.release(again, "failed")


def test_a_merged_change_of_runner_yaml_applies_to_the_next_task(tmp_path: Path) -> None:
    world = World(tmp_path)
    _commit(world.alpha, {".agents/runner.yaml": "version: 1\n"}, "no neighbours")

    workspace = world.acquire("alpha")

    assert workspace.checkpoint_data.get("neighbours") is None
    assert not (workspace.container / "beta").exists()


def test_a_repository_without_runner_yaml_has_no_neighbours(tmp_path: Path) -> None:
    world = World(tmp_path, alpha=None)

    workspace = world.acquire("alpha")

    assert workspace.conventions is not None and workspace.conventions.config is None
    assert "neighbours" not in workspace.checkpoint_data
    assert workspace.checkpoint_data["agentsMdBase"] is not None
    # No AGENTS.md in the base either: recorded as absent.
    beta = world.acquire("beta", "TASK-2")
    assert beta.checkpoint_data["agentsMdBase"] is None


def test_a_moved_pin_moves_a_clean_neighbour(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    pool.release(pool.acquire("TASK-1"), "failed")
    _pin(world.superproject, "beta", world.beta_tip)

    again = pool.acquire("TASK-1")

    assert _git(again.container / "beta", "rev-parse", "HEAD") == world.beta_tip
    assert again.checkpoint_data["neighbours"] == {"beta": world.beta_tip}


def test_a_pin_no_branch_reaches_is_fetched_by_its_id(tmp_path: Path) -> None:
    world = World(tmp_path)
    # The superproject pins a commit the neighbour's branches no longer have.
    _git(world.beta, "checkout", "-q", "-b", "gone")
    orphan = _commit(world.beta, {"gone.txt": "x\n"}, "on a branch deleted later")
    _git(world.beta, "checkout", "-q", "main")
    _git(world.beta, "branch", "-D", "gone")
    _git(world.beta, "config", "uploadpack.allowAnySHA1InWant", "true")
    _pin(world.superproject, "beta", orphan)

    workspace = world.acquire("alpha")

    assert _git(workspace.container / "beta", "rev-parse", "HEAD") == orphan
    # Kept under a ref: not counted as a commit of the neighbour's own.
    assert workspace.neighbour_changes() == {}
    world.pool("alpha").release(workspace, "failed")
    world.acquire("alpha")


def test_the_submodule_is_found_by_its_url_when_its_path_is_another(tmp_path: Path) -> None:
    world = World(tmp_path, beta_path="libs/beta-sdk")

    workspace = world.acquire("alpha")

    # Beside the copy, under the catalog's directory — what ``../beta`` names.
    assert _git(workspace.container / "beta", "rev-parse", "HEAD") == world.pin


# --- conventions that cannot be used -------------------------------------------


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ("version: 2\n", r"\$\.version"),
        ("version: 1\nneighbours: [beta]\nextra: 1\n", r"\$\.extra: unknown field"),
        ("version: 1\nneighbours: [gamma]\n", "'gamma' of .agents/runner.yaml is not a repository"),
        ("version: 1\nneighbours: [alpha]\n", "names the repository itself"),
        ("version: 1\nneighbours: [superproject]\n", "not a submodule of the superproject"),
        (": [\n", "not valid YAML"),
    ],
)
def test_conventions_the_daemon_cannot_use_go_to_a_person(
    tmp_path: Path, config: str, message: str
) -> None:
    world = World(tmp_path, alpha=config)
    pool = world.pool("alpha")

    with pytest.raises(WorkspaceBlocked, match=message) as caught:
        pool.acquire("TASK-1")

    assert caught.value.code == RUNNER_CONFIG_INVALID
    assert str(tmp_path) not in caught.value.reason
    # The copy is released: the next attempt is not "busy".
    with pytest.raises(WorkspaceBlocked):
        pool.acquire("TASK-1")


def test_neighbours_without_a_superproject_in_the_catalog_are_an_error(tmp_path: Path) -> None:
    world = World(tmp_path, superproject=False)
    with pytest.raises(WorkspaceError, match="no superproject"):
        world.acquire("alpha")


def test_a_huge_runner_yaml_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, alpha="version: 1\n" + "#" * 300_000 + "\n")
    with pytest.raises(WorkspaceBlocked, match="larger than"):
        world.acquire("alpha")


def test_runner_yaml_is_read_from_the_revision_asked(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", {".agents/runner.yaml": NEIGHBOURS_OF_ALPHA})
    first = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {".agents/runner.yaml": "version: 1\n"})
    config = read_runner_config(repo, first)
    assert config is not None and config.neighbours == ("beta",)
    later = read_runner_config(repo, "HEAD")
    assert later is not None and later.neighbours == ()
    # A directory where the file should be is no file.
    _git(repo, "rm", "-q", ".agents/runner.yaml")
    _commit(repo, {".agents/runner.yaml/x": "y\n"})
    assert read_runner_config(repo, "HEAD") is None


# --- a neighbour is read-only --------------------------------------------------


@pytest.mark.parametrize(
    ("change", "what"),
    [
        (lambda d: (d / "README.md").write_text("changed\n"), "has changed or new files"),
        (lambda d: (d / "new.txt").write_text("new\n"), "has changed or new files"),
        (lambda d: _commit(d, {"README.md": "committed\n"}), "moved from"),
        (lambda d: _git(d, "checkout", "-q", "--detach", "origin/main"), "moved from"),
    ],
)
def test_a_changed_neighbour_is_found(tmp_path: Path, change: Any, what: str) -> None:
    world = World(tmp_path)
    workspace = world.acquire("alpha")
    change(workspace.container / "beta")

    assert what in workspace.neighbour_changes()["beta"]


def test_ignored_files_in_a_neighbour_are_no_change(tmp_path: Path) -> None:
    world = World(tmp_path)
    _commit(world.beta, {".gitignore": "*.pyc\n"})
    _pin(world.superproject, "beta", _git(world.beta, "rev-parse", "HEAD"))
    workspace = world.acquire("alpha")
    (workspace.container / "beta" / "cache.pyc").write_text("x")
    assert workspace.neighbour_changes() == {}


def test_a_removed_neighbour_is_found(tmp_path: Path) -> None:
    world = World(tmp_path)
    workspace = world.acquire("alpha")
    _git(
        world.mirrors / "neighbours" / "beta.git",
        "worktree",
        "remove",
        "--force",
        str(workspace.container / "beta"),
    )
    assert workspace.neighbour_changes() == {"beta": "was removed or replaced"}


@pytest.mark.parametrize("left", ["file", "commit"])
def test_a_neighbour_left_changed_is_not_worked_beside(tmp_path: Path, left: str) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    workspace = pool.acquire("TASK-1")
    sibling = workspace.container / "beta"
    if left == "file":
        (sibling / "README.md").write_text("changed\n")
    else:
        _commit(sibling, {"README.md": "committed\n"})
    head = _git(sibling, "rev-parse", "HEAD")
    pool.release(workspace, "failed")

    with pytest.raises(WorkspaceBlocked) as caught:
        pool.acquire("TASK-1")

    assert caught.value.code == NEIGHBOUR_MODIFIED
    assert "beta" in caught.value.reason and "read-only" in caught.value.reason
    # Nothing of it was moved or dropped.
    assert _git(sibling, "rev-parse", "HEAD") == head
    if left == "file":
        assert (sibling / "README.md").read_text() == "changed\n"


def test_files_where_a_neighbour_goes_are_not_overwritten(tmp_path: Path) -> None:
    world = World(tmp_path)
    (world.root / "TASK-1" / "beta").mkdir(parents=True)
    (world.root / "TASK-1" / "beta" / "notes.txt").write_text("mine\n")
    with pytest.raises(WorkspaceBlocked, match="no copy of neighbour beta"):
        world.acquire("alpha")
    assert (world.root / "TASK-1" / "beta" / "notes.txt").read_text() == "mine\n"


def test_a_neighbour_of_the_one_repository_form_is_replaced_when_clean(tmp_path: Path) -> None:
    """A copy made before the catalog keeps its container; its neighbour is cut again."""
    world = World(tmp_path)
    # The one-repository form cut neighbours from the host's shared mirror.
    stray = world.root / "TASK-1" / "beta"
    stray.parent.mkdir(parents=True)
    _git(world.beta, "worktree", "add", "-q", "--detach", str(stray), world.beta_tip)

    workspace = world.acquire("alpha")

    assert _git(stray, "rev-parse", "HEAD") == world.pin
    common = _git(stray, "rev-parse", "--git-common-dir")
    assert (stray / common).resolve() == (world.mirrors / "neighbours" / "beta.git").resolve()
    assert workspace.neighbour_changes() == {}


def test_success_takes_the_neighbours_with_the_copy(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    workspace = pool.acquire("TASK-1")

    pool.release(workspace, "succeeded")

    assert not workspace.container.exists()
    listed = _git(world.mirrors / "neighbours" / "beta.git", "worktree", "list", "--porcelain")
    assert "TASK-1" not in listed


# --- the layout with segments (TAI-ADR-0064) -----------------------------------

# The service reaches the SDK two levels up, as in the superproject after the move.
SEGMENTS = {"alpha": "services/alpha", "beta": "sdk/beta"}
RUNNER_YAML_OF_ALPHA = (
    "version: 1\n"
    "neighbours: [beta]\n"
    "setup: cat {dependency}/README.md > installed\n"
    "checks:\n"
    "  - {{name: tests, run: cat {dependency}/README.md installed}}\n"
)
# (directories of the catalog, where beta sits in the superproject, path dependency)
CATALOG_LAYOUTS = [
    pytest.param({}, "beta", "../beta", id="flat"),
    pytest.param(SEGMENTS, "sdk/beta", "../../sdk/beta", id="segments"),
]


@pytest.mark.parametrize(("directories", "beta_path", "dependency"), CATALOG_LAYOUTS)
async def test_a_catalog_run_works_on_both_layouts(
    tmp_path: Path, directories: dict[str, str], beta_path: str, dependency: str
) -> None:
    """Neighbour, setup and checks of runner.yaml, then the container goes whole."""
    world = World(
        tmp_path,
        alpha=RUNNER_YAML_OF_ALPHA.format(dependency=dependency),
        beta_path=beta_path,
        directories=directories,
    )
    pool = world.pool("alpha")

    workspace = pool.acquire("TASK-1")

    assert workspace.path == world.root / "TASK-1" / directories.get("alpha", "alpha")
    assert workspace.container == world.root / "TASK-1"
    sibling = workspace.path / dependency
    assert _git(sibling, "rev-parse", "HEAD") == world.pin
    assert sibling.resolve() == (workspace.container / directories.get("beta", "beta")).resolve()
    assert workspace.checkpoint_data["neighbours"] == {"beta": world.pin}
    assert workspace.neighbour_changes() == {}

    plan = setup_at_base(workspace)
    assert plan is not None
    assert (await run_setup(plan, workspace)).passed
    (check,) = checks_at_base(workspace).checks
    result = await run_check(check, workspace)
    assert result.passed
    assert result.output.splitlines() == ["beta", "beta"]

    (sibling / "README.md").write_text("changed\n")
    assert workspace.neighbour_changes() == {"beta": "has changed or new files"}
    (sibling / "README.md").write_text("beta\n")

    (workspace.path / "installed").unlink()
    (workspace.path / "result.txt").write_text("result\n")
    assert workspace.commit("TASK-1: work") is not None
    pool.release(workspace, "succeeded")

    assert not workspace.container.exists()
    listed = _git(world.mirrors / "neighbours" / "beta.git", "worktree", "list", "--porcelain")
    assert "TASK-1" not in listed


@pytest.mark.parametrize(("directories", "beta_path", "dependency"), CATALOG_LAYOUTS)
def test_a_failed_catalog_copy_is_taken_again_on_both_layouts(
    tmp_path: Path, directories: dict[str, str], beta_path: str, dependency: str
) -> None:
    world = World(tmp_path, beta_path=beta_path, directories=directories)
    pool = world.pool("alpha")
    workspace = pool.acquire("TASK-1")
    (workspace.path / "wip.txt").write_text("half-done\n")
    pool.release(workspace, "failed")
    _pin(world.superproject, beta_path, world.beta_tip)

    again = pool.acquire("TASK-1")

    assert again.reused is True and again.path == workspace.path
    assert (again.path / "wip.txt").read_text() == "half-done\n"
    assert _git(again.path / dependency, "rev-parse", "HEAD") == world.beta_tip


def test_a_task_that_moved_repository_leaves_no_directory_of_the_layout_behind(
    tmp_path: Path,
) -> None:
    """``discard`` of a clean copy at ``services/alpha`` takes ``services/`` and ``sdk/`` too."""
    world = World(tmp_path, beta_path="sdk/beta", directories=SEGMENTS)
    pool = world.pool("alpha")
    workspace = pool.acquire("TASK-1")
    pool.release(workspace, "failed")

    assert pool.discard("TASK-1") is None

    assert not (world.root / "TASK-1").exists()


def test_files_where_a_neighbour_goes_with_segments_are_not_overwritten(tmp_path: Path) -> None:
    world = World(tmp_path, beta_path="sdk/beta", directories=SEGMENTS)
    (world.root / "TASK-1" / "sdk" / "beta").mkdir(parents=True)
    (world.root / "TASK-1" / "sdk" / "beta" / "notes.txt").write_text("mine\n")
    with pytest.raises(WorkspaceBlocked, match="no copy of neighbour beta"):
        world.acquire("alpha")
    assert (world.root / "TASK-1" / "sdk" / "beta" / "notes.txt").read_text() == "mine\n"


# --- a task of the superproject -------------------------------------------------


def test_a_superproject_task_has_its_submodules_in_place(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config="version: 1\nneighbours: [beta]\n")
    pool = world.pool("superproject")

    workspace = pool.acquire("TASK-5")

    submodule = workspace.path / "beta"
    assert _git(submodule, "rev-parse", "HEAD") == world.pin
    assert (submodule / "README.md").read_text() == "beta\n"
    assert not (workspace.container / "beta").exists()
    # The copy of the superproject sees its submodule clean at its pin.
    assert _git(workspace.path, "status", "--porcelain") == ""
    assert workspace.checkpoint_data["neighbours"] == {"beta": world.pin}

    # A change in the submodule is a changed neighbour, and the copy shows it.
    (submodule / "README.md").write_text("changed\n")
    assert workspace.neighbour_changes() == {"beta": "has changed or new files"}
    assert _git(workspace.path, "status", "--porcelain") == "M beta"
    (submodule / "README.md").write_text("beta\n")

    # Work of the superproject itself is committed without touching the pin.
    (workspace.path / "notes.md").write_text("work\n")
    head = workspace.commit("TASK-5: notes")
    assert head is not None
    assert _git(workspace.path, "ls-tree", head, "beta").split()[2] == world.pin

    pool.release(workspace, "succeeded")
    assert not workspace.container.exists()
    listed = _git(world.mirrors / "neighbours" / "beta.git", "worktree", "list", "--porcelain")
    assert "TASK-5" not in listed


def test_a_superproject_task_reads_pins_from_its_own_base(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config="version: 1\nneighbours: [beta]\n")
    pool = world.pool("superproject")
    first = pool.acquire("TASK-5")
    pool.release(first, "failed")
    # The superproject moves the pin after the branch was cut: the task's
    # copy is of its base, and so are its submodules.
    _pin(world.superproject, "beta", world.beta_tip)

    again = pool.acquire("TASK-5")

    assert _git(again.path / "beta", "rev-parse", "HEAD") == world.pin


def test_a_superproject_copy_with_a_changed_submodule_is_kept(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config="version: 1\nneighbours: [beta]\n")
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    (workspace.path / "beta" / "new.txt").write_text("x\n")

    pool.release(workspace, "succeeded")

    assert (workspace.path / "beta" / "new.txt").read_text() == "x\n"


# --- from the review of U005 ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://[abc/x/r.git", "not a valid URL"),
        ("https://forge.example:99999/org/r.git", "not a valid URL"),
        ("https://forge.example:abc/org/r.git", "not a valid URL"),
        ("https://forge.example:0/org/r.git", "port from 1 to 65535"),
    ],
)
def test_a_malformed_address_of_the_catalog_is_a_catalog_error(url: str, message: str) -> None:
    spec = {"repositoryField": "repositoryKey", "repositories": {"r": {"url": url}}}
    with pytest.raises(CatalogError, match=message):
        RepositoryCatalog.from_spec(spec)
    with pytest.raises(RevisionError, match="workingCopy"):
        workspace_pool_of(_revision(spec), {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": "/nonexistent"})


@pytest.mark.parametrize("port", ["1", "443", "65535"])
def test_a_port_in_range_is_taken(port: str) -> None:
    url = f"https://forge.example:{port}/org/r.git"
    spec = {"repositoryField": "repositoryKey", "repositories": {"r": {"url": url}}}
    assert RepositoryCatalog.from_spec(spec).entries["r"].url == url


def test_a_malformed_address_of_the_one_repository_form_is_a_revision_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(RevisionError, match="not a valid address"):
        mirror("https://[abc/x/r.git", tmp_path / "m")
    with pytest.raises(RevisionError, match="not a valid address"):
        workspace_pool_of(
            _revision({"repository": "https://[abc/x/r.git"}),
            {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")},
        )


def test_a_first_clone_keeps_only_tasks_of_its_repository_waiting(tmp_path: Path) -> None:
    catalog = RepositoryCatalog.from_spec(
        {
            "repositoryField": "repositoryKey",
            "repositories": {
                "slow": {"url": "https://forge.example/org/slow.git"},
                "fast": {"url": "https://forge.example/org/fast.git"},
            },
        }
    )
    release = threading.Event()
    made: list[str] = []

    def make_pool(entry: Any, create: bool) -> Any:
        if entry.key == "slow":
            assert release.wait(10)
        made.append(entry.key)
        return object()

    pools = RepositoryPools(catalog, tmp_path, make_pool)
    slow = [
        threading.Thread(target=pools.pool_for, args=(catalog.entries["slow"],)) for _ in range(2)
    ]
    for thread in slow:
        thread.start()
    time.sleep(0.1)

    started = time.monotonic()
    pools.pool_for(catalog.entries["fast"])  # not behind the clone of "slow"
    assert time.monotonic() - started < 1
    release.set()
    for thread in slow:
        thread.join()

    # Made once per key, however many asked at once.
    assert sorted(made) == ["fast", "slow"]


def test_a_mirror_another_replica_holds_too_long_is_an_error(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "alpha", {})
    mirrors = tmp_path / "m"
    mirrors.mkdir()
    holder = os.open(lock_path(mirrors / "alpha.git"), os.O_CREAT | os.O_RDWR, 0o600)
    import fcntl

    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(RevisionError, match="held by another process for more than"):
            mirror(f"file://{source}", mirrors, lock_timeout=0.3)
        assert time.monotonic() - started < 5
    finally:
        os.close(holder)
    assert mirror(f"file://{source}", mirrors) == mirrors / "alpha.git"


def test_a_neighbour_fetch_behind_a_held_lock_gives_up(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n", lock_timeout=0.2)
    path = mirrors.ensure("beta", f"file://{source}")
    with mirror_lock(lock_path(path), what="held"):
        assert mirrors.fetch("beta") is False
    assert mirrors.fetch("beta") is True
    with pytest.raises(WorkspaceError, match="unsafe neighbour key"):
        mirrors.path_of("../x")


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def test_staging_of_clones_that_died_is_swept_at_start(tmp_path: Path) -> None:
    mirrors = tmp_path / "w" / ".mirrors"
    dead = mirrors / f".alpha.git.{_dead_pid()}.tmp"
    alive = mirrors / f".beta.git.{os.getppid()}.tmp"
    locked = mirrors / f".gamma.git.{_dead_pid()}.tmp"
    neighbour = mirrors / "neighbours" / f".delta.git.{_dead_pid()}.tmp"
    other = mirrors / "alpha.git"
    for path in (dead, alive, locked, neighbour, other):
        path.mkdir(parents=True)

    with mirror_lock(mirrors / ".gamma.git.lock", what="gamma"):
        workspace_pool_of(
            _revision({"repositoryField": "repositoryKey", "repositories": {"a": {"url": "/x"}}}),
            {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")},
        )

    assert not dead.exists() and not neighbour.exists()
    # A live process, or a clone running under the lock, keeps its staging.
    assert alive.exists() and locked.exists() and other.exists()
    assert sweep_stale_clones(tmp_path / "missing") == []


def test_the_lock_wait_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / ".x.lock"
    with (
        mirror_lock(path, what="x"),
        pytest.raises(MirrorBusyError),
        mirror_lock(path, what="x", timeout=0),
    ):
        pass


def test_replicas_taking_a_neighbour_at_once_make_its_mirror_once(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    revision = _git(source, "rev-parse", "HEAD")
    mirrors = NeighbourMirrors(tmp_path / "n")
    results: list[object] = []

    def take() -> None:
        try:
            path = mirrors.ensure("beta", f"file://{source}")
            mirrors.reach("beta", revision)
            results.append(path)
        except Exception as exc:  # pragma: no cover - the failure this guards against
            results.append(exc)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [tmp_path / "n" / "beta.git"] * 4
    assert [p.name for p in (tmp_path / "n").iterdir() if p.is_dir()] == ["beta.git"]


def test_a_neighbour_under_a_linked_root_is_reused_not_cut_again(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    world = World(tmp_path / "link")
    pool = world.pool("alpha")
    first = pool.acquire("TASK-1")
    marker = first.container / "beta" / ".git"
    before = marker.stat().st_ino
    pool.release(first, "failed")

    pool.acquire("TASK-1")

    assert marker.stat().st_ino == before


# --- pointers of a superproject task (review of U006) -----------------------------

SUPERPROJECT_NEIGHBOURS = "version: 1\nneighbours: [beta]\n"


def _merge_moved_base(world: World, workspace: Workspace) -> None:
    """The superproject moves its pin; the task merges its base, as it must."""
    _pin(world.superproject, "beta", world.beta_tip)
    _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")


def _pointer(repo: Path, revision: str, path: str = "beta") -> str:
    return _git(repo, "ls-tree", revision, "--", path).split()[2]


def test_a_merged_pointer_is_not_rolled_back_by_the_commit(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    (workspace.path / "notes.md").write_text("work\n")

    assert workspace.neighbour_changes() == {}
    head = workspace.commit("TASK-5: notes")

    assert head is not None
    assert _pointer(workspace.path, head) == world.beta_tip


def test_a_merge_alone_is_published_without_a_commit_of_the_daemon(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    merged = workspace.head()

    # The submodule shows as moved in the copy; the daemon does not stage it.
    assert workspace.commit("TASK-5: merge") == merged
    assert _pointer(workspace.path, merged) == world.beta_tip


def test_a_copy_taken_again_places_the_pointer_of_its_branch(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    first = pool.acquire("TASK-5")
    _merge_moved_base(world, first)
    pool.release(first, "failed")

    again = pool.acquire("TASK-5")

    assert _git(again.path / "beta", "rev-parse", "HEAD") == world.beta_tip
    assert again.checkpoint_data["neighbours"] == {"beta": world.beta_tip}
    assert _git(again.path, "status", "--porcelain") == ""
    assert again.neighbour_changes() == {}
    (again.path / "notes.md").write_text("work\n")
    head = again.commit("TASK-5: notes")
    assert head is not None and _pointer(again.path, head) == world.beta_tip


def test_a_staged_pointer_is_a_changed_neighbour(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.beta_tip},beta")

    assert "pointer" in workspace.neighbour_changes()["beta"]


def test_a_pointer_committed_by_the_task_is_a_changed_neighbour(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.beta_tip},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")

    assert "pointer" in workspace.neighbour_changes()["beta"]
    # Taken again, the copy is placed at what its branch says, and still told.
    pool.release(workspace, "failed")
    again = pool.acquire("TASK-5")
    assert "pointer" in again.neighbour_changes()["beta"]


def test_a_removed_submodule_is_a_changed_neighbour(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _git(workspace.path, "rm", "-q", "--cached", "beta")

    assert "pointer" in workspace.neighbour_changes()["beta"]


def test_a_force_push_of_a_neighbour_is_no_change_of_it(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    pool.release(pool.acquire("TASK-1"), "failed")
    # The neighbour's forge rewrites its history: the pin is on no branch now.
    _git(world.beta, "checkout", "-q", "--orphan", "rewritten")
    _commit(world.beta, {"README.md": "rewritten\n"}, "rewritten")
    _git(world.beta, "branch", "-q", "-M", "main")
    NeighbourMirrors(world.mirrors / "neighbours").fetch("beta")

    again = pool.acquire("TASK-1")

    assert _git(again.container / "beta", "rev-parse", "HEAD") == world.pin
    assert again.neighbour_changes() == {}


def test_a_new_address_of_a_neighbour_rewrites_its_mirror(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    moved = tmp_path / "forge" / "beta-moved"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(moved))
    later = _commit(source, {"x.txt": "x\n"})
    _git(source, "push", "-q", str(moved), "main")
    mirrors = NeighbourMirrors(tmp_path / "n")
    path = mirrors.ensure("beta", f"file://{source}")
    assert mirrors.fetch("beta")

    assert mirrors.ensure("beta", f"file://{moved}") == path

    assert _git(path, "config", "--get", "remote.origin.url") == f"file://{moved}"
    # What the old address had is not taken for what the new one has.
    assert _git(path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin") == ""
    assert mirrors.fetch("beta")
    assert _git(path, "rev-parse", "refs/remotes/origin/main") == later
    assert "refs/remotes/origin/main" in _git(path, "for-each-ref", "--format=%(refname)")


def test_a_pin_behind_a_held_lock_is_busy_not_missing(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n", lock_timeout=0.2)
    path = mirrors.ensure("beta", f"file://{source}")
    with mirror_lock(lock_path(path), what="held"), pytest.raises(MirrorBusyError):
        mirrors.reach("beta", _git(source, "rev-parse", "HEAD"))


def _pull_moved_base(world: World, workspace: Workspace, how: str) -> None:
    """The task brings in its moved base the way a person would, into no tracking ref."""
    _pin(world.superproject, "beta", world.beta_tip)
    if how == "pull":
        _git(workspace.path, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
    else:
        _git(workspace.path, "fetch", "-q", "origin", "main")
        _git(workspace.path, "merge", "-q", "--no-edit", "FETCH_HEAD")


@pytest.mark.parametrize("how", ["pull", "fetch-merge"])
def test_a_base_pulled_without_a_tracking_ref_is_no_change_of_the_neighbour(
    tmp_path: Path, how: str
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _pull_moved_base(world, workspace, how)
    assert _pointer(workspace.path, "HEAD") == world.beta_tip

    assert workspace.neighbour_changes() == {}
    # Taken again, the copy is still the task's merge of its base.
    pool.release(workspace, "failed")
    again = pool.acquire("TASK-5")
    assert again.neighbour_changes() == {}
    assert _git(again.path / "beta", "rev-parse", "HEAD") == world.beta_tip


def test_the_default_base_is_tracked_in_the_mirror(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    pool.release(pool.acquire("TASK-5"), "failed")
    _pin(world.superproject, "beta", world.beta_tip)
    tip = _git(world.superproject, "rev-parse", "HEAD")

    again = pool.acquire("TASK-6")

    assert _git(pool.origin, "rev-parse", "refs/remotes/origin/main") == tip
    assert again.head() == tip
    # A tracking ref only: the branch of the earlier task stays where it was.
    assert _git(pool.origin, "rev-parse", "refs/heads/task/TASK-5") != tip


def test_a_pull_of_a_base_that_moved_again_is_no_change_of_the_neighbour(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _pull_moved_base(world, workspace, "pull")
    # The base moves on before the run ends: the merged pointer is still its.
    _pin(world.superproject, "beta", world.pin)

    assert workspace.neighbour_changes() == {}


def test_a_committed_pointer_is_told_after_the_base_was_pulled(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _pull_moved_base(world, workspace, "pull")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.pin},beta")
    _git(workspace.path, "commit", "-qm", "roll the pin back")

    assert "pointer" in workspace.neighbour_changes()["beta"]


def test_an_unreachable_forge_leaves_the_pointer_check_to_what_is_on_disk(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _git(world.pool("superproject").origin, "remote", "set-url", "origin", "file:///nowhere")

    assert workspace.neighbour_changes() == {}


def test_replicas_keeping_one_pin_at_once_do_not_fail(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    pool.release(pool.acquire("TASK-1"), "failed")
    origin = world.mirrors / "neighbours" / "beta.git"
    ref = f"refs/remotes/pins/{world.pin}"
    # Another replica holds the ref's lock while this one keeps the same pin.
    lock = origin / (ref + ".lock")
    lock.write_text(world.pin + "\n")
    try:
        again = pool.acquire("TASK-1")
    finally:
        lock.unlink()

    assert _git(again.container / "beta", "rev-parse", "HEAD") == world.pin


# --- from the review of U006 ------------------------------------------------------

MISSING = "0123456789abcdef0123456789abcdef01234567"


def test_a_pointer_to_a_commit_the_forge_lacks_is_blocked_not_a_crash(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    # The task commits a pointer to a commit that was never pushed.
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{MISSING},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")
    pool.release(workspace, "failed")

    with pytest.raises(WorkspaceBlocked) as blocked:
        pool.acquire("TASK-5")

    assert blocked.value.code == NEIGHBOUR_MODIFIED
    assert MISSING[:12] in blocked.value.reason
    assert "submodule beta" in blocked.value.reason
    assert world.pin[:12] in blocked.value.reason
    assert str(tmp_path) not in blocked.value.reason
    # Nothing was held: the pointer set back, the task goes on.
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.pin},beta")
    _git(workspace.path, "commit", "-qm", "set the pin back")
    again = pool.acquire("TASK-5")
    assert _git(again.path / "beta", "rev-parse", "HEAD") == world.pin


def test_a_missing_pointer_behind_an_unreachable_forge_is_not_blocked(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{MISSING},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")
    pool.release(workspace, "failed")
    # The forge of the neighbour does not answer: a later attempt may place it.
    world.beta.rename(tmp_path / "beta-away")

    with pytest.raises(WorkspaceError) as failed:
        pool.acquire("TASK-5")

    assert not isinstance(failed.value, WorkspaceBlocked)
    assert "could not be fetched" in str(failed.value)


def test_a_revision_the_forge_lacks_is_missing(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n")
    mirrors.ensure("beta", f"file://{source}")

    with pytest.raises(RevisionMissing):
        mirrors.reach("beta", MISSING)
    # Asked again, the same answer: nothing was left half-made.
    with pytest.raises(RevisionMissing):
        mirrors.reach("beta", MISSING)
    mirrors.reach("beta", _git(source, "rev-parse", "HEAD"))


def test_the_base_pin_the_forge_lacks_stays_an_error_of_the_run(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    # The base itself names a commit nobody pushed: not the task's pointer.
    _pin(world.superproject, "beta", MISSING)

    with pytest.raises(WorkspaceError) as failed:
        world.pool("superproject").acquire("TASK-5")

    assert not isinstance(failed.value, WorkspaceBlocked)
    assert "not in its forge" in str(failed.value)


def _hang_the_forge(pool: ExecutionWorkspacePool) -> None:
    """The pool's forge accepts the connection and never answers."""
    _git(
        pool.origin, "config", f"remote.{pool.push_remote}.uploadpack", "sleep 60; git-upload-pack"
    )


def test_a_hung_forge_does_not_hang_the_end_of_a_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_agent.workspace"), "disabled", False)
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    assert pool.push_remote
    workspace = pool.acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _hang_the_forge(pool)
    pool.base_fetch_timeout = 0.5

    started = time.monotonic()
    changes = workspace.neighbour_changes()

    assert time.monotonic() - started < 10
    # Checked against the tracking ref the copy has.
    assert changes == {}
    assert "timed out after 0.5 s" in caplog.text


def test_a_hung_forge_is_still_told_from_a_committed_pointer(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.beta_tip},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")
    _hang_the_forge(pool)
    pool.base_fetch_timeout = 0.5

    assert "pointer" in workspace.neighbour_changes()["beta"]


def test_a_hung_forge_at_acquisition_falls_back_to_the_mirror(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    pool.release(pool.acquire("TASK-5"), "failed")
    _hang_the_forge(pool)
    pool.base_fetch_timeout = 0.5

    started = time.monotonic()
    workspace = pool.acquire("TASK-5")

    assert time.monotonic() - started < 10
    assert workspace.reused
    assert workspace.neighbour_changes() == {}


def _pins(mirror: Path) -> set[str]:
    listed = _git(mirror, "for-each-ref", "--format=%(refname)", "refs/remotes/pins/")
    return {ref.removeprefix("refs/remotes/pins/") for ref in listed.splitlines()}


def test_pins_no_copy_stands_at_are_pruned(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    mirror = world.mirrors / "neighbours" / "beta.git"
    pool.release(pool.acquire("TASK-1"), "failed")
    assert _pins(mirror) == {world.pin}
    _pin(world.superproject, "beta", world.beta_tip)

    second = pool.acquire("TASK-2")

    # The copy of TASK-1 still stands at the old pin: both are kept.
    assert _pins(mirror) == {world.pin, world.beta_tip}
    pool.release(second, "succeeded")
    assert _pins(mirror) == {world.pin}
    assert pool.discard("TASK-1") is None
    assert _pins(mirror) == set()


def test_a_moved_neighbour_drops_the_pin_it_left(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    mirror = world.mirrors / "neighbours" / "beta.git"
    pool.release(pool.acquire("TASK-1"), "failed")
    _pin(world.superproject, "beta", world.beta_tip)

    again = pool.acquire("TASK-1")

    assert _git(again.container / "beta", "rev-parse", "HEAD") == world.beta_tip
    assert _pins(mirror) == {world.beta_tip}


def test_pruning_pins_is_idempotent_and_leaves_other_refs(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    mirror = world.mirrors / "neighbours" / "beta.git"
    empty = tmp_path / "empty.git"
    _git(tmp_path, "init", "-q", "--bare", str(empty))
    assert prune_pins(empty) == []  # a mirror without pins or copies
    workspace = pool.acquire("TASK-1")
    # A pin fetched for a copy that is gone, and one nobody stands at.
    _git(mirror, "update-ref", f"refs/remotes/pins/{world.beta_tip}", world.beta_tip)

    assert prune_pins(mirror) == [world.beta_tip]
    assert prune_pins(mirror) == []

    assert _pins(mirror) == {world.pin}
    assert _git(mirror, "rev-parse", "refs/remotes/origin/main") == world.beta_tip
    assert workspace.neighbour_changes() == {}


def test_a_pinned_neighbour_is_no_change_after_a_force_push_and_a_prune(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    held = pool.acquire("TASK-1")
    pool.release(held, "failed")
    pool.release(pool.acquire("TASK-2"), "succeeded")  # prunes, TASK-1 still stands at the pin
    _git(world.beta, "checkout", "-q", "--orphan", "rewritten")
    _commit(world.beta, {"README.md": "rewritten\n"}, "rewritten")
    _git(world.beta, "branch", "-q", "-M", "main")
    NeighbourMirrors(world.mirrors / "neighbours").fetch("beta")

    again = pool.acquire("TASK-1")

    assert again.neighbour_changes() == {}


# --- from the review of TASK-001158 ------------------------------------------------


def _flaky_forge(mirror: Path, tmp_path: Path, second: str) -> None:
    """The forge serves the first fetch of ``mirror``; every later one runs ``second``."""
    flag = tmp_path / "served-once"
    script = tmp_path / "flaky-upload-pack"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ -e "{flag}" ]; then {second}; fi\n'
        f': > "{flag}"\n'
        'exec git-upload-pack "$@"\n'
    )
    script.chmod(0o755)
    _git(mirror, "config", "remote.origin.uploadpack", str(script))


DROPPED = "echo 'fatal: the remote end hung up unexpectedly' >&2; exit 128"


def test_a_network_failure_of_the_fetch_by_id_is_not_a_missing_revision(
    tmp_path: Path,
) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n")
    path = mirrors.ensure("beta", f"file://{source}")
    # The fetch of the branches is answered, the fetch by id is cut off.
    _flaky_forge(path, tmp_path, DROPPED)

    with pytest.raises(MirrorError) as failed:
        mirrors.reach("beta", MISSING)

    assert not isinstance(failed.value, RevisionMissing)
    assert "could not be fetched" in str(failed.value)


def test_a_fetch_by_id_that_times_out_is_not_a_missing_revision(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n", fetch_timeout=0.5)
    path = mirrors.ensure("beta", f"file://{source}")
    _flaky_forge(path, tmp_path, "sleep 60")

    started = time.monotonic()
    with pytest.raises(MirrorError) as failed:
        mirrors.reach("beta", MISSING)

    assert time.monotonic() - started < 10
    assert not isinstance(failed.value, RevisionMissing)


def test_a_pointer_behind_a_dropped_fetch_by_id_is_an_error_not_blocked(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{MISSING},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")
    pool.release(workspace, "failed")
    _flaky_forge(world.mirrors / "neighbours" / "beta.git", tmp_path, DROPPED)

    with pytest.raises(WorkspaceError) as failed:
        pool.acquire("TASK-5")

    # The forge said nothing about the commit: a later attempt may place it.
    assert not isinstance(failed.value, WorkspaceBlocked)
    assert "could not be fetched" in str(failed.value)


def test_a_refusal_of_the_fetch_by_id_is_still_a_missing_revision(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n")
    path = mirrors.ensure("beta", f"file://{source}")
    # A v0 client: the forge refuses an unadvertised object in its own words.
    _git(path, "config", "protocol.version", "0")

    with pytest.raises(RevisionMissing):
        mirrors.reach("beta", MISSING)


def test_a_hung_forge_of_a_neighbour_fails_its_fetch_in_time(tmp_path: Path) -> None:
    source = _repo(tmp_path / "forge" / "beta", {})
    mirrors = NeighbourMirrors(tmp_path / "n", fetch_timeout=0.5)
    path = mirrors.ensure("beta", f"file://{source}")
    _git(path, "config", "remote.origin.uploadpack", "sleep 60; git-upload-pack")

    started = time.monotonic()
    assert mirrors.fetch("beta") is False
    assert time.monotonic() - started < 10
    # The lock is not left behind with the killed fetch.
    with mirror_lock(lock_path(path), what="free", timeout=0):
        pass


def test_a_hung_forge_of_the_superproject_does_not_hang_the_copy(tmp_path: Path) -> None:
    world = World(tmp_path)
    pool = world.pool("alpha")
    pool.release(pool.acquire("TASK-1"), "failed")
    neighbours = world.mirrors / "neighbours"
    _git(neighbours / "superproject.git", "config", "remote.origin.uploadpack", "sleep 60; x")
    # What the forge would say now is not what the mirror has.
    _pin(world.superproject, "beta", world.beta_tip)
    catalog = world.pools.catalog
    read = CatalogConventions(
        catalog, catalog.entries["alpha"], NeighbourMirrors(neighbours, fetch_timeout=0.5)
    )

    started = time.monotonic()
    conventions = read(pool.origin, _git(pool.origin, "rev-parse", "refs/heads/main"))

    assert time.monotonic() - started < 10
    # Pinned from what the mirror of the superproject already held.
    assert [(n.name, n.revision) for n in conventions.neighbours] == [("beta", world.pin)]


def _work(workspace: Workspace, name: str) -> None:
    """The task's own commit; the submodule left out, as the daemon's commit leaves it."""
    (workspace.path / name).write_text("work\n")
    _git(workspace.path, "add", name)
    _git(workspace.path, "commit", "-qm", name)


@pytest.mark.parametrize("how", ["fast-forward", "merge-commit"])
def test_a_missing_pointer_merged_with_the_base_blames_the_base(tmp_path: Path, how: str) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    if how == "merge-commit":
        _work(workspace, "before.md")
    # The base moves its pointer to a commit nobody pushed; the task merges it
    # and goes on working.
    _pin(world.superproject, "beta", MISSING)
    # No submodule fetch: the placed neighbour cannot have the missing commit.
    _git(
        workspace.path,
        "fetch",
        "-q",
        "--no-recurse-submodules",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")
    merged = _git(world.superproject, "rev-parse", "HEAD")
    _work(workspace, "after.md")
    pool.release(workspace, "failed")

    with pytest.raises(WorkspaceBlocked) as blocked:
        pool.acquire("TASK-5")

    assert blocked.value.code == NEIGHBOUR_MODIFIED
    reason = blocked.value.reason
    assert MISSING[:12] in reason and "submodule beta" in reason
    assert f"from its base at {merged[:12]}" in reason
    assert "fix the base" in reason
    assert "set the pointer back" not in reason
    assert str(tmp_path) not in reason


def test_a_missing_pointer_of_the_task_after_a_merge_is_still_the_task_s(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _merge_moved_base(world, workspace)
    # After the merge the task itself moves the pointer to an unpushed commit.
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{MISSING},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")
    pool.release(workspace, "failed")

    with pytest.raises(WorkspaceBlocked) as blocked:
        pool.acquire("TASK-5")

    assert "set the pointer back" in blocked.value.reason
    assert "fix the base" not in blocked.value.reason


# --- the base moves a pointer during the run (TASK-001387) -------------------------


def _update_submodule(workspace: Workspace, revision: str) -> None:
    """What ``git submodule update`` does to a placed neighbour: a detached checkout."""
    _git(workspace.path / "beta", "checkout", "-q", "--detach", revision)


def test_a_neighbour_checked_out_at_the_merged_pointer_is_no_change(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    # An operator or the submodule-lag rule moves the pin while the run goes on;
    # the task merges its base and updates its submodules, as the conventions say.
    _merge_moved_base(world, workspace)
    _update_submodule(workspace, world.beta_tip)
    (workspace.path / "notes.md").write_text("work\n")

    assert workspace.neighbour_changes() == {}
    head = workspace.commit("TASK-5: notes")
    assert head is not None and _pointer(workspace.path, head) == world.beta_tip


def test_a_base_moved_during_the_run_and_not_merged_is_no_change(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _pin(world.superproject, "beta", world.beta_tip)
    (workspace.path / "notes.md").write_text("work\n")

    assert workspace.neighbour_changes() == {}
    # The checkout follows the base's pointer of the moment of hand-in.
    _update_submodule(workspace, world.beta_tip)
    assert workspace.neighbour_changes() == {}
    head = workspace.commit("TASK-5: notes")
    assert head is not None and _pointer(workspace.path, head) == world.pin


def test_a_pointer_equal_to_the_base_at_hand_in_is_no_change(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    # The branch and the base point at one commit at hand-in: merging the base
    # changes nothing there, whoever wrote the pointer first.
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.beta_tip},beta")
    _git(workspace.path, "commit", "-qm", "same pin as the base")
    _update_submodule(workspace, world.beta_tip)
    _pin(world.superproject, "beta", world.beta_tip)

    assert workspace.neighbour_changes() == {}


def test_a_pointer_the_task_moved_while_the_base_moved_is_a_changed_neighbour(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    own = _commit(world.beta, {"README.md": "own\n"}, "own")
    _merge_moved_base(world, workspace)
    # The executor moves the pointer itself, past what the base pins.
    _git(workspace.path / "beta", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _update_submodule(workspace, own)
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{own},beta")
    _git(workspace.path, "commit", "-qm", "move the pin")

    assert own[:12] in workspace.neighbour_changes()["beta"]


def test_a_checkout_off_every_pointer_of_the_base_is_a_moved_neighbour(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    own = _commit(world.beta, {"README.md": "own\n"}, "own")
    _merge_moved_base(world, workspace)
    _git(workspace.path / "beta", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _update_submodule(workspace, own)

    changes = workspace.neighbour_changes()

    assert changes["beta"] == f"moved from {world.pin[:12]} to {own[:12]}"


def test_a_stale_local_base_does_not_excuse_a_rolled_back_pointer(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    pool = world.pool("superproject")
    workspace = pool.acquire("TASK-5")
    _merge_moved_base(world, workspace)
    # The local base of the mirror still has the old pin; the forge's has the new.
    assert _pointer(pool.origin, "refs/heads/main") == world.pin
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.pin},beta")
    _git(workspace.path, "commit", "-qm", "roll the pin back")

    assert "pointer" in workspace.neighbour_changes()["beta"]


# --- a pointer the base moved on, rolled back by the hand-in (TASK-001396) ---------


def _commit_all(workspace: Workspace, how: str) -> None:
    """The executor's own commit of everything, the submodule's old checkout included."""
    (workspace.path / "notes.md").write_text("work\n")
    if how == "commit -a":
        _git(workspace.path, "add", "notes.md")
        _git(workspace.path, "commit", "-qam", "work")
    else:
        _git(workspace.path, "add", "-A")
        _git(workspace.path, "commit", "-qm", "work")


@pytest.mark.parametrize("how", ["commit -a", "add -A"])
def test_a_merge_of_the_base_then_commit_all_is_a_rolled_back_pointer(
    tmp_path: Path, how: str
) -> None:
    """TASK-001346: the base moved the pin, the checkout stayed, ``commit -a`` took it."""
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    assert _git(workspace.path, "status", "--porcelain") == "M beta"
    _commit_all(workspace, how)
    assert _pointer(workspace.path, "HEAD") == world.pin

    check = workspace.neighbour_check()

    assert check.changes == {}
    regression = check.regressions["beta"]
    assert (regression.expected, regression.actual) == (world.beta_tip, world.pin)
    reason = regression.reason()
    assert world.beta_tip in reason and world.pin in reason
    assert "git checkout origin/main -- beta" in reason
    assert str(tmp_path) not in reason
    # Told the same in words, and the same when asked again.
    assert "pointer" in workspace.neighbour_changes()["beta"]
    assert workspace.neighbour_check() == check


def test_a_rolled_back_pointer_only_staged_is_told_too(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _git(workspace.path, "add", "-A")

    check = workspace.neighbour_check()

    assert check.changes == {}
    assert check.regressions["beta"].actual == world.pin


def test_the_hint_of_a_rolled_back_pointer_fixes_it(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _commit_all(workspace, "commit -a")
    assert workspace.neighbour_check().regressions

    # What the reason says: the base's pointer back, the checkout after it.
    _git(workspace.path, "checkout", "origin/main", "--", "beta")
    _git(workspace.path / "beta", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _update_submodule(workspace, world.beta_tip)
    _git(workspace.path, "commit", "-qm", "the base's pointer")

    assert workspace.neighbour_check().regressions == {}
    assert workspace.neighbour_changes() == {}
    assert _pointer(workspace.path, workspace.head()) == world.beta_tip


def test_a_merge_with_the_checkout_updated_then_commit_all_hands_in(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _update_submodule(workspace, world.beta_tip)
    _commit_all(workspace, "commit -a")

    assert workspace.neighbour_check().regressions == {}
    assert workspace.neighbour_changes() == {}
    assert _pointer(workspace.path, "HEAD") == world.beta_tip


def test_a_base_not_merged_is_held_to_the_pointer_the_branch_was_cut_at(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    # The base pins the later commit when the copy is cut.
    _pin(world.superproject, "beta", world.beta_tip)
    workspace = world.pool("superproject").acquire("TASK-5")
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{world.pin},beta")
    _git(workspace.path, "commit", "-qm", "an older pin")

    check = workspace.neighbour_check()

    assert check.changes == {}
    regression = check.regressions["beta"]
    assert (regression.expected, regression.actual) == (world.beta_tip, world.pin)


def test_a_pointer_the_task_moved_forward_is_not_a_rolled_back_one(tmp_path: Path) -> None:
    """A move of the pointer by the task stays what it was: a change of the neighbour."""
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _git(workspace.path / "beta", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _update_submodule(workspace, world.beta_tip)
    _git(workspace.path, "add", "beta")
    _git(workspace.path, "commit", "-qm", "move the pin forward")

    check = workspace.neighbour_check()

    assert check.regressions == {}
    assert check.changes["beta"] == f"moved from {world.pin[:12]} to {world.beta_tip[:12]}"


def test_neighbours_outside_the_copy_have_no_pointer_to_roll_back(tmp_path: Path) -> None:
    world = World(tmp_path)
    workspace = world.acquire("alpha")
    _pin(world.superproject, "beta", world.beta_tip)
    (workspace.path / "notes.md").write_text("work\n")

    assert workspace.neighbour_check().regressions == {}
    assert workspace.neighbour_changes() == {}


def test_the_daemon_s_commit_takes_no_staged_pointer(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    workspace = world.pool("superproject").acquire("TASK-5")
    _merge_moved_base(world, workspace)
    _git(workspace.path, "add", "-A")
    assert _git(workspace.path, "diff", "--cached", "--name-only") == "beta"
    (workspace.path / "notes.md").write_text("work\n")

    head = workspace.commit("TASK-5: notes")

    assert head is not None and _pointer(workspace.path, head) == world.beta_tip
    assert "notes.md" in _git(workspace.path, "ls-tree", "--name-only", head).split()


def test_the_rolled_back_pointer_has_a_reason_of_its_own() -> None:
    assert NEIGHBOUR_POINTER_REGRESSED == "neighbour_pointer_regressed"
    assert NEIGHBOUR_POINTER_REGRESSED != NEIGHBOUR_MODIFIED


# --- a submodule runner.yaml does not name (package-sdk), TASK-001396 review -------


class Sdk:
    """A submodule of the superproject outside ``neighbours``, as package-sdk is."""

    def __init__(self, world: World, tmp_path: Path) -> None:
        self.forge = _repo(tmp_path / "forge" / "sdk", {})
        self.pin = _git(self.forge, "rev-parse", "HEAD")
        self.tip = _commit(self.forge, {"README.md": "later\n"}, "later")
        self.world = world
        _pin(world.superproject, "sdk", self.pin, url="https://forge.example/org/sdk.git")

    def check_out(self, workspace: Workspace, revision: str) -> None:
        """The executor's own checkout of the submodule, nothing the pool placed."""
        target = workspace.path / "sdk"
        if not (target / ".git").exists():
            _git(workspace.path, "clone", "-q", "--no-checkout", str(self.forge), "sdk")
        _git(target, "checkout", "-q", "--detach", revision)

    def merge_moved_base(self, workspace: Workspace) -> None:
        """The base moves the pin of the SDK; the task merges its base."""
        _pin(self.world.superproject, "sdk", self.tip)
        _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")


def _sdk_task(tmp_path: Path) -> tuple[World, Sdk, Workspace]:
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    sdk = Sdk(world, tmp_path)
    workspace = world.pool("superproject").acquire("TASK-5")
    sdk.check_out(workspace, sdk.pin)
    return world, sdk, workspace


def test_the_daemon_s_commit_after_a_merge_keeps_the_base_s_pointer_of_an_undeclared_submodule(
    tmp_path: Path,
) -> None:
    """9b30f86, 54d149a: the daemon's ``add -A`` took the old checkout of package-sdk."""
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    assert _git(workspace.path, "status", "--porcelain") == "M sdk"
    (workspace.path / "notes.md").write_text("work\n")

    assert workspace.neighbour_check() == NeighbourCheck()
    head = workspace.commit("TASK-5: notes")

    assert head is not None and _pointer(workspace.path, head, "sdk") == sdk.tip
    assert "notes.md" in _git(workspace.path, "ls-tree", "--name-only", head).split()


def test_the_daemon_s_wip_keeps_the_base_s_pointer_of_an_undeclared_submodule(
    tmp_path: Path,
) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    (workspace.path / "notes.md").write_text("work\n")

    head = workspace.commit_wip("TASK-5: wip")

    assert head is not None and _pointer(workspace.path, head, "sdk") == sdk.tip


@pytest.mark.parametrize("how", ["commit -a", "add -A"])
def test_commit_all_over_an_undeclared_submodule_is_a_rolled_back_pointer(
    tmp_path: Path, how: str
) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    _commit_all(workspace, how)
    assert _pointer(workspace.path, "HEAD", "sdk") == sdk.pin

    check = workspace.neighbour_check()

    assert check.changes == {}
    regression = check.regressions["sdk"]
    assert (regression.path, regression.expected, regression.actual) == ("sdk", sdk.tip, sdk.pin)
    assert "git checkout origin/main -- sdk" in regression.reason()
    assert str(tmp_path) not in regression.reason()
    # The daemon's commit over it does not hide it: the branch already has it.
    assert workspace.neighbour_check() == check


def test_a_rolled_back_pointer_of_an_undeclared_submodule_without_a_checkout_is_told(
    tmp_path: Path,
) -> None:
    """Without the SDK's history, the pointer of the cut point is the rollback."""
    world = World(tmp_path, superproject_config=SUPERPROJECT_NEIGHBOURS)
    sdk = Sdk(world, tmp_path)
    workspace = world.pool("superproject").acquire("TASK-5")
    sdk.merge_moved_base(workspace)
    _git(workspace.path, "update-index", "--cacheinfo", f"160000,{sdk.pin},sdk")
    _git(workspace.path, "commit", "-qm", "the old pin")

    check = workspace.neighbour_check()

    assert check.changes == {}
    assert (check.regressions["sdk"].expected, check.regressions["sdk"].actual) == (
        sdk.tip,
        sdk.pin,
    )


def test_a_submodule_update_after_the_merge_hands_in(tmp_path: Path) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    sdk.check_out(workspace, sdk.tip)
    assert _git(workspace.path, "status", "--porcelain") == ""
    _commit_all(workspace, "commit -a")

    assert workspace.neighbour_check() == NeighbourCheck()
    assert _pointer(workspace.path, "HEAD", "sdk") == sdk.tip


def test_the_hint_fixes_a_rolled_back_pointer_of_an_undeclared_submodule(tmp_path: Path) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    _commit_all(workspace, "commit -a")
    assert workspace.neighbour_check().regressions

    _git(workspace.path, "checkout", "origin/main", "--", "sdk")
    sdk.check_out(workspace, sdk.tip)
    _git(workspace.path, "commit", "-qm", "the base's pointer")

    assert workspace.neighbour_check() == NeighbourCheck()


def test_an_undeclared_submodule_the_base_moved_and_the_task_did_not_merge_is_no_change(
    tmp_path: Path,
) -> None:
    world, sdk, workspace = _sdk_task(tmp_path)
    _pin(world.superproject, "sdk", sdk.tip)
    _commit_all(workspace, "commit -a")

    assert workspace.neighbour_check() == NeighbourCheck()


def test_an_undeclared_submodule_moved_by_the_task_is_a_changed_neighbour(tmp_path: Path) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.check_out(workspace, sdk.tip)
    _commit_all(workspace, "commit -a")

    check = workspace.neighbour_check()

    assert check.regressions == {}
    assert "pointer" in check.changes["sdk"]


def test_an_undeclared_submodule_pointer_only_staged_is_told(tmp_path: Path) -> None:
    _, sdk, workspace = _sdk_task(tmp_path)
    sdk.merge_moved_base(workspace)
    _git(workspace.path, "add", "-A")

    check = workspace.neighbour_check()

    assert check.regressions["sdk"].actual == sdk.pin
    # The daemon's commit drops it all the same.
    (workspace.path / "notes.md").write_text("work\n")
    head = workspace.commit("TASK-5: notes")
    assert head is not None and _pointer(workspace.path, head, "sdk") == sdk.tip


def test_a_superproject_without_neighbours_has_its_submodules_checked(tmp_path: Path) -> None:
    world = World(tmp_path, superproject_config=None)
    sdk = Sdk(world, tmp_path)
    workspace = world.pool("superproject").acquire("TASK-5")
    sdk.check_out(workspace, sdk.pin)
    sdk.merge_moved_base(workspace)
    _commit_all(workspace, "commit -a")

    assert workspace.neighbour_check().regressions["sdk"].actual == sdk.pin


def test_a_repository_that_is_not_the_superproject_keeps_its_submodules_to_itself(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    workspace = world.acquire("alpha")
    _git(workspace.path, "update-index", "--add", "--cacheinfo", f"160000,{world.pin},vendored")
    _git(workspace.path, "commit", "-qm", "vendor")

    assert workspace.neighbour_check() == NeighbourCheck()
