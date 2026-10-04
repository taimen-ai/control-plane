"""Execution workspace (ADR-0016 §5): one isolated working copy per task.

The properties under test are the ones that make a commit usable as evidence:
copies do not interfere, a restart resumes the same copy instead of forking a
second one, cleanup never destroys work, and nothing naming this host leaves it.
"""

import os
import subprocess
from pathlib import Path

import pytest

from control_plane_agent.workspace import (
    ExecutionWorkspacePool,
    Neighbour,
    UnsafePayloadError,
    WorkspaceBusyError,
    WorkspaceError,
    assert_portable,
    parse_neighbours,
    public_remote_url,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    # Имя ветки явно: init.defaultBranch у CI-раннера не задан (master), тесты пушат main.
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("origin\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


@pytest.fixture
def pool(origin: Path, tmp_path: Path) -> ExecutionWorkspacePool:
    return ExecutionWorkspacePool(origin, tmp_path / "workspaces")


def test_two_tasks_execute_in_independent_copies(pool: ExecutionWorkspacePool) -> None:
    first = pool.acquire("TASK-000001")
    second = pool.acquire("TASK-000002")

    (first.path / "a.txt").write_text("from first\n")
    (second.path / "b.txt").write_text("from second\n")

    assert first.path != second.path
    assert not (second.path / "a.txt").exists()
    assert not (first.path / "b.txt").exists()
    assert first.branch == "task/TASK-000001"
    assert second.branch == "task/TASK-000002"

    first_sha = first.commit("first")
    second_sha = second.commit("second")
    assert first_sha != second_sha
    # Neither commit carries the other task's file: evidence stays separable.
    assert "b.txt" not in git(first.path, "show", "--name-only", "--format=", "HEAD")


def test_restart_reuses_the_copy_and_keeps_uncommitted_work(
    pool: ExecutionWorkspacePool,
) -> None:
    workspace = pool.acquire("TASK-000003")
    (workspace.path / "wip.txt").write_text("half-done\n")
    pool.release(workspace, "failed")  # crash mid-flight

    again = pool.acquire("TASK-000003")

    assert again.reused is True
    assert again.path == workspace.path
    assert (again.path / "wip.txt").read_text() == "half-done\n"


def test_commit_references_the_task_and_is_registered_by_reference(
    pool: ExecutionWorkspacePool,
) -> None:
    workspace = pool.acquire("TASK-000004")
    (workspace.path / "done.txt").write_text("work\n")

    sha = workspace.commit("TASK-000004: do the thing")

    assert sha is not None
    assert workspace.artifact_uri(sha) == f"git:{sha}"
    message = git(workspace.path, "log", "-1", "--format=%B")
    assert "TASK-000004" in message


def test_commit_without_changes_is_not_invented(pool: ExecutionWorkspacePool) -> None:
    workspace = pool.acquire("TASK-000005")
    assert workspace.commit("nothing happened") is None


def test_agent_made_commits_are_evidence_too(pool: ExecutionWorkspacePool, origin: Path) -> None:
    """Регресс первого смоука BidOps: агент закоммитил сам, дерево чистое, но
    ветка ушла вперёд базы — демон должен вернуть её head и опубликовать, а не
    отчитаться «нет изменений» и оставить ветку на runner'е."""
    workspace = pool.acquire("TASK-000105")
    (workspace.path / "by-agent.txt").write_text("committed by the agent itself\n")
    git(workspace.path, "add", "-A")
    git(
        workspace.path,
        "-c",
        "user.name=agent",
        "-c",
        "user.email=agent@example.test",
        "commit",
        "--no-verify",
        "-m",
        "agent: own commit",
    )
    head = git(workspace.path, "rev-parse", "HEAD")

    sha = workspace.commit("TASK-000105: work")

    assert sha == head
    assert sha != workspace.base_commit
    assert git(workspace.path, "status", "--porcelain") == ""


def test_success_drops_the_copy_but_never_the_branch(
    pool: ExecutionWorkspacePool, origin: Path
) -> None:
    workspace = pool.acquire("TASK-000006")
    (workspace.path / "result.txt").write_text("result\n")
    sha = workspace.commit("TASK-000006: work")

    pool.release(workspace, "succeeded")

    assert not workspace.path.exists()
    assert git(origin, "rev-parse", "task/TASK-000006") == sha


def test_success_drops_a_copy_that_holds_only_ignored_files(
    pool: ExecutionWorkspacePool,
) -> None:
    """Регресс runner'а BidOps: после `uv run` в копии остаётся .venv (ignored),
    git без --force отказывался удалять worktree, и на хосте копились ~130 МБ
    на задачу. Игнорируемые файлы — не работа, копия должна уйти."""
    workspace = pool.acquire("TASK-000090")
    (workspace.path / ".gitignore").write_text("junk/\n")
    workspace.commit("TASK-000090: ignore junk")
    (workspace.path / "junk").mkdir()
    (workspace.path / "junk" / "venv-like.bin").write_bytes(b"\0" * 1024)
    assert not workspace.is_dirty

    pool.release(workspace, "succeeded")

    assert not workspace.path.exists()


def test_cleanup_keeps_a_copy_that_still_holds_uncommitted_work(
    pool: ExecutionWorkspacePool,
) -> None:
    workspace = pool.acquire("TASK-000007")
    (workspace.path / "uncommitted.txt").write_text("not saved yet\n")

    pool.release(workspace, "succeeded")

    assert (workspace.path / "uncommitted.txt").exists()


def test_failed_and_suspended_runs_keep_their_copy(pool: ExecutionWorkspacePool) -> None:
    for key, outcome in (("TASK-000008", "failed"), ("TASK-000009", "suspended")):
        workspace = pool.acquire(key)
        (workspace.path / "state.txt").write_text("mid-flight\n")
        workspace.commit(f"{key}: partial")
        pool.release(workspace, outcome)  # type: ignore[arg-type]
        assert workspace.path.exists()


def test_a_second_holder_is_refused_the_same_copy(pool: ExecutionWorkspacePool) -> None:
    pool.acquire("TASK-000010")
    with pytest.raises(WorkspaceBusyError):
        pool.acquire("TASK-000010")


def test_released_copy_can_be_taken_again(pool: ExecutionWorkspacePool) -> None:
    first = pool.acquire("TASK-000011")
    pool.release(first, "failed")
    assert pool.acquire("TASK-000011").reused is True


def test_branch_is_continued_after_its_copy_was_cleaned_up(
    pool: ExecutionWorkspacePool,
) -> None:
    workspace = pool.acquire("TASK-000012")
    (workspace.path / "first.txt").write_text("one\n")
    sha = workspace.commit("TASK-000012: first attempt")
    pool.release(workspace, "succeeded")

    second = pool.acquire("TASK-000012")

    assert second.reused is False  # the directory was gone...
    assert second.base_commit == sha  # ...but the work continues, not forks
    assert (second.path / "first.txt").exists()


def test_a_copy_on_the_wrong_branch_is_refused_not_repaired(
    pool: ExecutionWorkspacePool,
) -> None:
    workspace = pool.acquire("TASK-000013")
    git(workspace.path, "checkout", "-q", "-b", "somebody-elses-branch")
    pool.release(workspace, "failed")

    with pytest.raises(WorkspaceError, match="expected task/TASK-000013"):
        pool.acquire("TASK-000013")


def test_keys_that_could_escape_the_root_are_refused(pool: ExecutionWorkspacePool) -> None:
    for key in ("../escape", "/absolute", "with space", ""):
        with pytest.raises(WorkspaceError):
            pool.acquire(key)


def test_idle_copies_are_pruned_but_working_ones_are_not(origin: Path, tmp_path: Path) -> None:
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", max_workspaces=1)
    for key in ("TASK-000014", "TASK-000015"):
        workspace = pool.acquire(key)
        (workspace.path / "f.txt").write_text(key)
        workspace.commit(f"{key}: work")
        pool.release(workspace, "failed")  # kept: failure keeps its copy

    live = pool.acquire("TASK-000016")
    pool.release(live, "failed")

    remaining = {p.name for p in (tmp_path / "workspaces").iterdir() if p.is_dir()}
    remaining.discard(".locks")
    assert "TASK-000016" in remaining  # the one just worked in is kept
    assert len(remaining) <= 2  # the oldest idle copy was pruned
    assert git(origin, "rev-parse", "task/TASK-000014")  # its branch survived


def test_checkpoint_data_carries_no_local_paths(pool: ExecutionWorkspacePool) -> None:
    workspace = pool.acquire("TASK-000017")

    data = workspace.checkpoint_data

    assert data == {
        "workspaceKey": "TASK-000017",
        "branch": "task/TASK-000017",
        "baseCommit": workspace.base_commit,
        "reused": False,
        "baseBranch": "main",
    }
    assert str(workspace.path) not in str(data)


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/Users/someone/repo/task"},
        {"where": "/home/runner/work"},
        {"uri": "file:///tmp/copy"},
        {"home": "~/workspaces/TASK-1"},
        {"win": "C:\\runner\\work"},
        {"token": "anything-non-empty"},
        {"apiKey": "anything-non-empty"},
        {"note": "cp_live_abcdef"},
        {"nested": {"items": [{"dir": "/opt/runner/copies"}]}},
    ],
)
def test_guard_rejects_paths_and_credentials(payload: dict) -> None:
    with pytest.raises(UnsafePayloadError):
        assert_portable(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"branch": "task/TASK-000018", "commit": "a" * 40},
        {"uri": "git:0123456789abcdef"},
        {"relative": "src/control_plane_agent/workspace.py"},
        {"endpoint": "https://control-plane.example/api/v1/runs"},
        {"reused": True, "attempt": 2},
    ],
)
def test_guard_passes_portable_evidence(payload: dict) -> None:
    assert_portable(payload)


# --- publishing the branch for review -----------------------------------------


@pytest.fixture
def forge(tmp_path: Path) -> Path:
    """A bare repository standing in for the remote a team reviews in."""
    remote = tmp_path / "forge.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return remote


def test_push_publishes_only_the_task_branch(
    pool: ExecutionWorkspacePool, origin: Path, forge: Path
) -> None:
    git(origin, "remote", "add", "forge", str(forge))
    workspace = pool.acquire("TASK-000201")
    (workspace.path / "feature.txt").write_text("work\n")
    sha = workspace.commit("TASK-000201: work")

    assert workspace.push("forge") is True
    assert git(forge, "rev-parse", workspace.branch) == sha
    # The base branch is untouched: a runner offers work, it does not move the
    # line the rest of the team builds on.
    published = git(forge, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert published.split() == [workspace.branch]


def test_push_reports_failure_instead_of_raising(
    pool: ExecutionWorkspacePool, tmp_path: Path
) -> None:
    """An unreachable forge must not turn finished work into a failed run."""
    workspace = pool.acquire("TASK-000202")
    (workspace.path / "feature.txt").write_text("work\n")
    workspace.commit("TASK-000202: work")

    assert workspace.push("nonexistent-remote") is False
    # The commit is still there: evidence does not depend on the forge.
    assert workspace.head()


def test_push_does_not_force_over_a_diverged_branch(
    pool: ExecutionWorkspacePool, origin: Path, forge: Path
) -> None:
    """Someone else's history on the branch is a person's problem, not ours."""
    git(origin, "remote", "add", "forge", str(forge))
    workspace = pool.acquire("TASK-000203")
    (workspace.path / "a.txt").write_text("ours\n")
    workspace.commit("TASK-000203: ours")
    assert workspace.push("forge") is True

    # A different history lands on the same branch in the forge.
    other = tmp_path_clone(forge, workspace.branch)
    (other / "b.txt").write_text("theirs\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "theirs")
    git(other, "push", "-q", "--force", "origin", f"HEAD:{workspace.branch}")

    (workspace.path / "c.txt").write_text("more\n")
    workspace.commit("TASK-000203: more")
    # Rejected rather than overwritten, and reported rather than raised.
    assert workspace.push("forge") is False


def tmp_path_clone(remote: Path, branch: str) -> Path:
    target = remote.parent / "other-clone"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(remote), str(target)], check=True)
    return target


# --- error messages that leave this host --------------------------------------


def test_git_failure_message_carries_no_host_path(
    pool: ExecutionWorkspacePool, tmp_path: Path
) -> None:
    """The text of a git failure reaches durable state through failure_reason.

    The paths are real and this machine's; ADR-0016 forbids them in anything the
    Control Plane keeps, so they must not survive into the exception.
    """
    workspace = pool.acquire("TASK-000301")
    with pytest.raises(WorkspaceError) as raised:
        # A branch that does not exist: git fails and quotes its own argv back,
        # which is where the working-copy path comes from.
        git_via_pool(workspace.path, "checkout", "no-such-branch")

    message = str(raised.value)
    assert "<path>" in message or str(tmp_path) not in message
    assert str(workspace.path) not in message
    assert str(pool.root) not in message


def git_via_pool(cwd: Path, *args: str) -> str:
    from control_plane_agent.workspace import _git

    return _git(cwd, *args)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Paths of this host go, whatever surrounds them.
        ("git worktree add /opt/runner/worktrees/T-1 failed", "git worktree add <path> failed"),
        ("cannot read /Users/someone/.config/iam/credentials.json", "cannot read <path>"),
        (r"fatal: C:\Users\dev\repo is not a repository", "fatal: <path> is not a repository"),
        # What is NOT a local path stays readable: a reviewer needs these.
        ("fatal: could not read Username for 'https://github.com'", None),
        ("error in src/control_plane_agent/workspace.py", None),
        ("branch task/TASK-000042 already exists", None),
    ],
)
def test_redaction_removes_host_paths_and_keeps_the_rest(raw: str, expected: str | None) -> None:
    from control_plane_agent.workspace import redact_local_paths

    assert redact_local_paths(raw) == (raw if expected is None else expected)


def test_copy_is_cut_from_a_freshly_fetched_base(origin: Path, tmp_path: Path) -> None:
    """A runner must not branch from a base it last updated by hand.

    Otherwise the agent fixes code that no longer exists, and its branch reads
    as an undo of whatever landed meanwhile — which is exactly what happened
    before this fetch was added.
    """
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
    git(origin, "remote", "add", "forge", str(upstream))
    git(origin, "push", "-q", "forge", "main:main")

    # Someone lands a commit in the forge that this runner has never seen.
    contributor = tmp_path / "contributor"
    subprocess.run(["git", "clone", "-q", str(upstream), str(contributor)], check=True)
    (contributor / "landed.txt").write_text("landed upstream\n")
    git(contributor, "add", "-A")
    git(contributor, "commit", "-qm", "landed upstream")
    git(contributor, "push", "-q", "origin", "main")

    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="forge")
    workspace = pool.acquire("TASK-000401")

    assert (workspace.path / "landed.txt").exists(), "copy was cut from a stale base"


def test_unreachable_forge_does_not_stop_the_work(origin: Path, tmp_path: Path) -> None:
    """Refreshing the base is best-effort: work proceeds from what is on disk."""
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="nonexistent")
    workspace = pool.acquire("TASK-000402")
    assert workspace.head()


# --- neighbours the copy must build against ------------------------------------
#
# A repository with a path dependency cannot be built from a copy of itself
# alone. These cover the two ways that goes wrong: the neighbour missing, and
# the neighbour present at a revision nobody committed.

NEIGHBOUR = "platform-auth-sdk"


@pytest.fixture
def neighbour_origin(tmp_path: Path) -> Path:
    """The sibling repository, with a later commit than the one pinned."""
    repo = tmp_path / "neighbour"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "sdk.py").write_text("pinned\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "pinned revision")
    (repo / "sdk.py").write_text("moved on\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later revision")
    return repo


@pytest.fixture
def superproject(tmp_path: Path, neighbour_origin: Path) -> Path:
    """A superproject pinning the neighbour one commit behind its tip."""
    repo = tmp_path / "superproject"
    repo.mkdir()
    git(repo, "init", "-q")
    pin(repo, NEIGHBOUR, git(neighbour_origin, "rev-parse", "HEAD~1"))
    return repo


def pin(superproject: Path, path: str, revision: str) -> str:
    """Record a submodule at an exact revision, without needing the network."""
    git(superproject, "update-index", "--add", "--cacheinfo", f"160000,{revision},{path}")
    git(superproject, "commit", "-q", "--allow-empty", "-m", f"pin {path} at {revision[:12]}")
    return revision


@pytest.fixture
def pool_with_neighbour(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> ExecutionWorkspacePool:
    return ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        repo_dir="control-plane",
        neighbours=[Neighbour(NEIGHBOUR, neighbour_origin)],
        superproject=superproject,
    )


def test_neighbour_is_placed_beside_the_copy_at_the_pinned_revision(
    pool_with_neighbour: ExecutionWorkspacePool, neighbour_origin: Path
) -> None:
    """The revision comes from the superproject, not from the neighbour's tip.

    Building against the tip verifies a combination of revisions that exists in
    no commit anywhere, so a green run would say nothing about the branch under
    review.
    """
    workspace = pool_with_neighbour.acquire("TASK-000501")

    sibling = workspace.path.parent / NEIGHBOUR
    assert sibling.is_dir(), "the copy cannot be built: its neighbour is missing"
    assert (sibling / "sdk.py").read_text() == "pinned\n"
    assert git(sibling, "rev-parse", "HEAD") == git(neighbour_origin, "rev-parse", "HEAD~1")
    assert git(sibling, "rev-parse", "HEAD") != git(neighbour_origin, "rev-parse", "HEAD")


def test_path_dependencies_of_this_repository_resolve_in_a_fresh_copy(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> None:
    """Every ``../x`` this project declares must exist in a copy, unaided.

    Read from the real pyproject rather than hardcoded: a path dependency added
    later must fail here, not on the runner as an unbuildable copy. The copy and
    its neighbours are laid out as in the superproject (TAI-ADR-0064), where these
    paths are written: control-plane at ``services/control-plane``.
    """
    import posixpath
    import tomllib

    manifest = Path(__file__).resolve().parents[2] / "pyproject.toml"
    sources = tomllib.loads(manifest.read_text()).get("tool", {}).get("uv", {}).get("sources", {})
    relative = [
        source["path"]
        for source in sources.values()
        if isinstance(source, dict) and source.get("path", "").startswith("../")
    ]
    assert relative, "expected this project to declare at least one path dependency"

    repo_dir = "services/control-plane"
    # Each neighbour at its path in the superproject, pinned there as a submodule.
    paths = [posixpath.normpath(posixpath.join(repo_dir, path)) for path in relative]
    for neighbour in paths:
        pin(superproject, neighbour, git(neighbour_origin, "rev-parse", "HEAD~1"))
    pool = ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        repo_dir=repo_dir,
        # The mirror of each declared neighbour, as a deployment provides it.
        neighbours=[Neighbour(neighbour, neighbour_origin) for neighbour in paths],
        superproject=superproject,
    )
    workspace = pool.acquire("TASK-000502")

    for path in relative:
        assert (workspace.path / path).is_dir(), f"{path} does not resolve in the copy"


def test_neighbour_revisions_travel_into_the_checkpoint(
    pool_with_neighbour: ExecutionWorkspacePool, neighbour_origin: Path
) -> None:
    """Evidence is the commit AND what it was built against."""
    workspace = pool_with_neighbour.acquire("TASK-000503")

    data = workspace.checkpoint_data
    assert data["neighbours"] == {NEIGHBOUR: git(neighbour_origin, "rev-parse", "HEAD~1")}
    assert str(workspace.path) not in str(data)


def test_a_reused_copy_follows_a_moved_pin(
    pool_with_neighbour: ExecutionWorkspacePool, neighbour_origin: Path, superproject: Path
) -> None:
    workspace = pool_with_neighbour.acquire("TASK-000504")
    pool_with_neighbour.release(workspace, "failed")
    pin(superproject, NEIGHBOUR, git(neighbour_origin, "rev-parse", "HEAD"))

    again = pool_with_neighbour.acquire("TASK-000504")

    assert (again.path.parent / NEIGHBOUR / "sdk.py").read_text() == "moved on\n"


def test_local_changes_in_a_neighbour_are_never_overwritten(
    pool_with_neighbour: ExecutionWorkspacePool, neighbour_origin: Path, superproject: Path
) -> None:
    """Debugging a task often means editing the sibling; that work is not ours to drop."""
    workspace = pool_with_neighbour.acquire("TASK-000505")
    (workspace.path.parent / NEIGHBOUR / "sdk.py").write_text("being debugged\n")
    pool_with_neighbour.release(workspace, "failed")
    pin(superproject, NEIGHBOUR, git(neighbour_origin, "rev-parse", "HEAD"))

    again = pool_with_neighbour.acquire("TASK-000505")

    assert (again.path.parent / NEIGHBOUR / "sdk.py").read_text() == "being debugged\n"


def test_success_takes_the_whole_container_including_neighbours(
    pool_with_neighbour: ExecutionWorkspacePool, origin: Path
) -> None:
    workspace = pool_with_neighbour.acquire("TASK-000506")
    (workspace.path / "result.txt").write_text("result\n")
    sha = workspace.commit("TASK-000506: work")

    pool_with_neighbour.release(workspace, "succeeded")

    assert not workspace.container.exists()
    assert git(origin, "rev-parse", "task/TASK-000506") == sha  # the branch survives


def test_a_copy_made_before_containers_is_adopted_with_its_work(
    origin: Path, tmp_path: Path
) -> None:
    """Runners carry unfinished work: an existing copy is moved, not abandoned.

    Recreating it under the new layout would make git refuse the branch as
    already checked out, and the half-done work would be stranded.
    """
    root = tmp_path / "workspaces"
    # The layout as it was: the copy itself sat at root/<key>, with no container.
    old = root / "TASK-000507"
    git(origin, "worktree", "add", "-q", str(old), "-b", "task/TASK-000507")
    (old / "wip.txt").write_text("half-done\n")

    pool = ExecutionWorkspacePool(origin, root, repo_dir="control-plane")
    adopted = pool.acquire("TASK-000507")

    assert adopted.path == root / "TASK-000507" / "control-plane"
    assert adopted.reused is True
    assert (adopted.path / "wip.txt").read_text() == "half-done\n"
    assert git(adopted.path, "rev-parse", "--abbrev-ref", "HEAD") == "task/TASK-000507"


# --- the layout with segments (TAI-ADR-0064) -----------------------------------
#
# The superproject moves to services/, sdk/, apps/: a service then reaches an
# SDK two levels up (``../../sdk/platform-auth-sdk``). The container is laid out
# like the superproject, so the same path resolves in a copy of the runner.

# (directory of the copy, path of the neighbour, path dependency from the copy)
LAYOUTS = [
    pytest.param("control-plane", NEIGHBOUR, f"../{NEIGHBOUR}", id="flat"),
    pytest.param(
        "services/control-plane",
        f"sdk/{NEIGHBOUR}",
        f"../../sdk/{NEIGHBOUR}",
        id="segments",
    ),
]


@pytest.mark.parametrize(("repo_dir", "path", "dependency"), LAYOUTS)
def test_a_path_dependency_resolves_on_both_layouts(
    origin: Path,
    neighbour_origin: Path,
    superproject: Path,
    tmp_path: Path,
    repo_dir: str,
    path: str,
    dependency: str,
) -> None:
    pinned = pin(superproject, path, git(neighbour_origin, "rev-parse", "HEAD~1"))
    root = tmp_path / "workspaces"
    pool = ExecutionWorkspacePool(
        origin,
        root,
        repo_dir=repo_dir,
        neighbours=[Neighbour(path, neighbour_origin)],
        superproject=superproject,
    )

    workspace = pool.acquire("TASK-000520")

    assert workspace.path == root / "TASK-000520" / repo_dir
    assert workspace.container == root / "TASK-000520"
    sibling = workspace.path / dependency
    assert (sibling / "sdk.py").read_text() == "pinned\n"
    assert git(sibling, "rev-parse", "HEAD") == pinned
    assert workspace.checkpoint_data["neighbours"] == {path: pinned}

    (workspace.path / "result.txt").write_text("result\n")
    sha = workspace.commit("TASK-000520: work")
    pool.release(workspace, "succeeded")

    # The container goes whole, the directories of the layout with it.
    assert not (root / "TASK-000520").exists()
    assert git(origin, "rev-parse", "task/TASK-000520") == sha
    assert str(root / "TASK-000520") not in git(neighbour_origin, "worktree", "list")


@pytest.mark.parametrize(("repo_dir", "path", "dependency"), LAYOUTS)
def test_a_failed_copy_is_taken_again_where_it_was_on_both_layouts(
    origin: Path,
    neighbour_origin: Path,
    superproject: Path,
    tmp_path: Path,
    repo_dir: str,
    path: str,
    dependency: str,
) -> None:
    pin(superproject, path, git(neighbour_origin, "rev-parse", "HEAD~1"))
    pool = ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        repo_dir=repo_dir,
        neighbours=[Neighbour(path, neighbour_origin)],
        superproject=superproject,
    )
    workspace = pool.acquire("TASK-000521")
    (workspace.path / "wip.txt").write_text("half-done\n")
    pool.release(workspace, "failed")
    pin(superproject, path, git(neighbour_origin, "rev-parse", "HEAD"))

    again = pool.acquire("TASK-000521")

    assert again.reused is True
    assert again.path == workspace.path
    assert (again.path / "wip.txt").read_text() == "half-done\n"
    assert (again.path / dependency / "sdk.py").read_text() == "moved on\n"


@pytest.mark.parametrize(("repo_dir", "path", "dependency"), LAYOUTS)
def test_the_disk_budget_prunes_a_copy_on_both_layouts(
    origin: Path,
    neighbour_origin: Path,
    superproject: Path,
    tmp_path: Path,
    repo_dir: str,
    path: str,
    dependency: str,
) -> None:
    pin(superproject, path, git(neighbour_origin, "rev-parse", "HEAD~1"))
    root = tmp_path / "workspaces"
    pool = ExecutionWorkspacePool(
        origin,
        root,
        repo_dir=repo_dir,
        neighbours=[Neighbour(path, neighbour_origin)],
        superproject=superproject,
        max_workspaces=0,
    )
    first = pool.acquire("TASK-000522")
    pool.release(first, "failed")
    assert first.path.is_dir()  # its own release keeps it

    second = pool.acquire("TASK-000523")
    pool.release(second, "failed")

    assert not (root / "TASK-000522").exists()
    assert (second.path / dependency / "sdk.py").is_file()


@pytest.mark.parametrize(("repo_dir", "path", "dependency"), LAYOUTS)
def test_a_reopened_copy_knows_its_container_on_both_layouts(
    origin: Path,
    neighbour_origin: Path,
    superproject: Path,
    tmp_path: Path,
    repo_dir: str,
    path: str,
    dependency: str,
) -> None:
    """Restart recovery takes the copy as it is: its container is ``<key>``, not inside it."""
    pin(superproject, path, git(neighbour_origin, "rev-parse", "HEAD~1"))
    root = tmp_path / "workspaces"
    pool = ExecutionWorkspacePool(
        origin,
        root,
        repo_dir=repo_dir,
        neighbours=[Neighbour(path, neighbour_origin)],
        superproject=superproject,
    )
    workspace = pool.acquire("TASK-000525")
    (workspace.path / "result.txt").write_text("result\n")
    sha = workspace.commit("TASK-000525: work")
    pool.release(workspace, "failed")

    reopened = pool.reopen("TASK-000525")

    assert reopened is not None
    assert reopened.path == root / "TASK-000525" / repo_dir
    assert reopened.repo_dir == repo_dir
    assert reopened.container == root / "TASK-000525"
    assert (reopened.path / dependency / "sdk.py").is_file()

    pool.release(reopened, "succeeded")

    # Removal clears the container itself, the neighbour and the layout's directories with it.
    assert not (root / "TASK-000525").exists()
    assert root.is_dir()
    assert git(origin, "rev-parse", "task/TASK-000525") == sha
    assert str(root / "TASK-000525") not in git(neighbour_origin, "worktree", "list")


def test_a_copy_made_before_containers_is_adopted_into_a_directory_with_segments(
    origin: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    old = root / "TASK-000524"
    git(origin, "worktree", "add", "-q", str(old), "-b", "task/TASK-000524")
    (old / "wip.txt").write_text("half-done\n")

    pool = ExecutionWorkspacePool(origin, root, repo_dir="services/control-plane")
    adopted = pool.acquire("TASK-000524")

    assert adopted.path == root / "TASK-000524" / "services" / "control-plane"
    assert adopted.container == root / "TASK-000524"
    assert (adopted.path / "wip.txt").read_text() == "half-done\n"


@pytest.mark.parametrize(
    "directory",
    [
        "",
        ".",
        "..",
        "../control-plane",
        "services/../control-plane",
        "/services/control-plane",
        "services//control-plane",
        "services/control-plane/",
        "services\\control-plane",
        "services/control-plane\n",
        "services/.hidden",
        "a" * 101,
        "/".join(["a" * 100] * 3),
    ],
)
def test_an_unsafe_directory_of_the_copy_or_a_neighbour_is_refused(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path, directory: str
) -> None:
    if directory:  # empty: the copy's directory defaults to the origin's name
        with pytest.raises(WorkspaceError, match="unsafe repository directory"):
            ExecutionWorkspacePool(origin, tmp_path / "workspaces", repo_dir=directory)
    with pytest.raises(WorkspaceError, match="unsafe neighbour path"):
        Neighbour(directory, neighbour_origin)
    assert not (tmp_path / "workspaces").exists() or not any((tmp_path / "workspaces").iterdir())


@pytest.mark.parametrize(
    ("repo_dir", "path"),
    [
        ("services", "services/platform-auth-sdk"),
        ("services/control-plane", "services"),
        ("services/control-plane", "services/control-plane"),
        ("Services/control-plane", "services/control-plane/sdk"),
    ],
)
def test_a_neighbour_inside_the_copy_or_around_it_is_refused(
    origin: Path,
    neighbour_origin: Path,
    superproject: Path,
    tmp_path: Path,
    repo_dir: str,
    path: str,
) -> None:
    """One checkout would write into the files of the other."""
    with pytest.raises(WorkspaceError, match="one inside the other"):
        ExecutionWorkspacePool(
            origin,
            tmp_path / "workspaces",
            repo_dir=repo_dir,
            neighbours=[Neighbour(path, neighbour_origin)],
            superproject=superproject,
        )


def test_a_neighbour_beside_the_copy_in_the_same_directory_is_placed(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> None:
    """``services/control-plane`` and ``services/memory-service``: one ``../`` apart."""
    revision = git(neighbour_origin, "rev-parse", "HEAD~1")
    pinned = pin(superproject, "services/memory-service", revision)
    root = tmp_path / "workspaces"
    pool = ExecutionWorkspacePool(
        origin,
        root,
        repo_dir="services/control-plane",
        neighbours=[Neighbour("services/memory-service", neighbour_origin)],
        superproject=superproject,
    )

    workspace = pool.acquire("TASK-000525")

    assert git(workspace.path / "../memory-service", "rev-parse", "HEAD") == pinned
    pool.release(workspace, "succeeded")
    assert not (root / "TASK-000525").exists()


def test_neighbours_without_a_superproject_are_refused(
    origin: Path, neighbour_origin: Path, tmp_path: Path
) -> None:
    """Nothing would pin the revisions, and "latest" is the bug being fixed."""
    with pytest.raises(WorkspaceError, match="superproject"):
        ExecutionWorkspacePool(
            origin,
            tmp_path / "workspaces",
            neighbours=[Neighbour(NEIGHBOUR, neighbour_origin)],
        )


def test_a_neighbour_the_superproject_does_not_pin_is_refused(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> None:
    pool = ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        neighbours=[Neighbour("not-a-submodule", neighbour_origin)],
        superproject=superproject,
    )
    with pytest.raises(WorkspaceError, match="not a submodule"):
        pool.acquire("TASK-000508")


@pytest.mark.parametrize("spec", ["", "   "])
def test_no_neighbours_configured_is_a_valid_deployment(spec: str) -> None:
    assert parse_neighbours(spec) == []


def test_neighbours_are_read_as_name_and_origin_pairs() -> None:
    parsed = parse_neighbours("platform-auth-sdk=/mirrors/sdk.git, memory-service=/mirrors/mem.git")

    assert [n.path for n in parsed] == ["platform-auth-sdk", "memory-service"]
    assert parsed[0].origin == Path("/mirrors/sdk.git")


@pytest.mark.parametrize("spec", ["platform-auth-sdk", "=/mirrors/sdk.git", "../escape=/m.git"])
def test_a_malformed_neighbour_is_refused_not_skipped(spec: str) -> None:
    """Skipping it would resurface as an unbuildable copy, far from the cause."""
    with pytest.raises(WorkspaceError):
        parse_neighbours(spec)


def test_a_stale_neighbour_mirror_is_fetched_to_reach_the_pin(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> None:
    """Регресс runner'а BidOps (2026-09-12): суперпроект подвинул pin соседа, а
    bare-зеркало на runner'е об этом не знало — каждая задача падала в
    `git worktree add` с "invalid reference", пока зеркало не обновили руками.
    Теперь недостающая ревизия подтягивается из origin зеркала сама."""
    mirror = tmp_path / "neighbour-mirror.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(neighbour_origin), str(mirror)], check=True)
    # Сосед двигается дальше, суперпроект закрепляет новый коммит; зеркало отстало.
    (neighbour_origin / "sdk.py").write_text("newest\n")
    git(neighbour_origin, "add", "-A")
    git(neighbour_origin, "commit", "-qm", "newest revision")
    newest = git(neighbour_origin, "rev-parse", "HEAD")
    pin(superproject, NEIGHBOUR, newest)
    assert (
        subprocess.run(["git", "cat-file", "-e", newest], cwd=mirror, check=False).returncode != 0
    )

    pool = ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        repo_dir="control-plane",
        neighbours=[Neighbour(NEIGHBOUR, mirror)],
        superproject=superproject,
    )
    workspace = pool.acquire("TASK-000601")

    sibling = workspace.path.parent / NEIGHBOUR
    assert (sibling / "sdk.py").read_text() == "newest\n"
    assert git(sibling, "rev-parse", "HEAD") == newest


def test_reaching_a_pin_never_moves_a_branch_of_the_neighbour_mirror(
    origin: Path, neighbour_origin: Path, superproject: Path, tmp_path: Path
) -> None:
    """The mirror may be a pool's too (universal-runner FR-006): its task
    branches are its own, and a fetch for a pin lands in remote-tracking refs."""
    mirror = tmp_path / "neighbour-mirror.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(neighbour_origin), str(mirror)], check=True)
    local = git(mirror, "rev-parse", "HEAD~1")
    git(mirror, "branch", "task/TASK-000700", local)
    # The forge has the same branch elsewhere, and a pin the mirror lacks.
    git(neighbour_origin, "branch", "task/TASK-000700", "HEAD")
    (neighbour_origin / "sdk.py").write_text("newest\n")
    git(neighbour_origin, "add", "-A")
    git(neighbour_origin, "commit", "-qm", "newest revision")
    pin(superproject, NEIGHBOUR, git(neighbour_origin, "rev-parse", "HEAD"))

    pool = ExecutionWorkspacePool(
        origin,
        tmp_path / "workspaces",
        repo_dir="control-plane",
        neighbours=[Neighbour(NEIGHBOUR, mirror)],
        superproject=superproject,
    )
    pool.acquire("TASK-000701")

    assert git(mirror, "rev-parse", "refs/heads/task/TASK-000700") == local


# --- where the branch lives, for whoever merges it -----------------------------


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://forge.example/org/repo.git", "https://forge.example/org/repo.git"),
        (
            "https://x-access-token:secret@forge.example/org/repo.git",
            "https://forge.example/org/repo.git",
        ),
        ("https://user@forge.example/org/repo.git", "https://forge.example/org/repo.git"),
        ("ssh://git@forge.example:22/org/repo.git", "ssh://git@forge.example:22/org/repo.git"),
        ("git@forge.example:org/repo.git", "git@forge.example:org/repo.git"),
        ("/srv/mirrors/repo.git", None),
        ("file:///srv/mirrors/repo.git", None),
        ("", None),
    ],
)
def test_remote_url_is_shared_without_credentials_or_local_paths(
    url: str, public: str | None
) -> None:
    assert public_remote_url(url) == public


def test_pool_names_its_push_remote_by_url(origin: Path, tmp_path: Path, forge: Path) -> None:
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="forge")
    assert pool.push_remote_url() is None  # no such remote yet
    git(origin, "remote", "add", "forge", str(forge))
    assert pool.push_remote_url() is None  # a local path means nothing elsewhere
    git(origin, "remote", "set-url", "forge", "https://bot:token@forge.example/org/repo.git")
    assert pool.push_remote_url() == "https://forge.example/org/repo.git"
    assert ExecutionWorkspacePool(origin, tmp_path / "w2").push_remote_url() is None


# --- a task's own base branch (customFields.baseBranch, TAI-ADR-0047) ----------
#
# Tasks of a feature are cut from the feature branch and merged back into it.
# The failure to guard against is silent: a copy cut from main instead reads as
# a perfectly normal copy until the review diff shows the wrong line.

FEATURE = "feature/checkout"


@pytest.fixture
def feature_forge(origin: Path, tmp_path: Path) -> Path:
    """The forge, holding main and a feature branch this runner never fetched."""
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
    git(origin, "remote", "add", "forge", str(upstream))
    git(origin, "push", "-q", "forge", "main:main")
    contributor = tmp_path / "contributor"
    subprocess.run(["git", "clone", "-q", str(upstream), str(contributor)], check=True)
    git(contributor, "checkout", "-q", "-b", FEATURE)
    (contributor / "feature.txt").write_text("feature work\n")
    git(contributor, "add", "-A")
    git(contributor, "commit", "-qm", "feature work")
    git(contributor, "push", "-q", "origin", FEATURE)
    return upstream


def _pool(origin: Path, tmp_path: Path) -> ExecutionWorkspacePool:
    return ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="forge")


def test_copy_is_cut_from_the_tasks_base_branch(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    workspace = _pool(origin, tmp_path).acquire("TASK-000500", FEATURE)

    assert (workspace.path / "feature.txt").exists(), "copy was not cut from the feature"
    assert workspace.branch == "task/TASK-000500"
    assert workspace.base_branch == FEATURE
    assert workspace.checkpoint_data["baseBranch"] == FEATURE


def test_tasks_of_different_bases_do_not_share_a_fetch(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    """Each base is fetched into its own ref, so one task never gets another's base."""
    pool = _pool(origin, tmp_path)
    on_feature = pool.acquire("TASK-000501", FEATURE)
    on_main = pool.acquire("TASK-000502")

    assert (on_feature.path / "feature.txt").exists()
    assert not (on_main.path / "feature.txt").exists()
    assert on_main.base_branch == "main"


def test_a_base_branch_missing_in_the_forge_fails_instead_of_using_main(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    pool = _pool(origin, tmp_path)
    with pytest.raises(WorkspaceError, match="base branch feature/nope does not exist in forge"):
        pool.acquire("TASK-000503", "feature/nope")

    assert not pool.path_for("TASK-000503").exists()
    assert not git(origin, "branch", "--list", "task/TASK-000503")
    pool.acquire("TASK-000503")  # the failed attempt left no lock behind


def test_without_a_forge_the_base_branch_must_be_in_the_repository(
    origin: Path, tmp_path: Path
) -> None:
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces")
    with pytest.raises(WorkspaceError, match="does not exist in the repository"):
        pool.acquire("TASK-000504", FEATURE)

    git(origin, "branch", FEATURE)
    assert pool.acquire("TASK-000504", FEATURE).base_branch == FEATURE


@pytest.mark.parametrize("name", ["-upload-pack=x", "feature/..", "a b", "x~1", ""])
def test_unsafe_base_branch_names_are_refused(pool: ExecutionWorkspacePool, name: str) -> None:
    with pytest.raises(WorkspaceError, match="unsafe base branch name"):
        pool.acquire("TASK-000505", name)


def test_a_task_without_a_base_branch_keeps_the_default(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    workspace = _pool(origin, tmp_path).acquire("TASK-000506")

    assert workspace.base_branch == "main"
    assert not (workspace.path / "feature.txt").exists()


def test_base_branch_of_reads_the_task_field() -> None:
    from control_plane_agent.workspace import base_branch_of

    assert base_branch_of({"customFields": {"baseBranch": " feature/x "}}) == "feature/x"
    assert base_branch_of({"customFields": {"baseBranch": ""}}) is None
    assert base_branch_of({"customFields": None}) is None
    assert base_branch_of({}) is None
    with pytest.raises(WorkspaceError, match="must be a string"):
        base_branch_of({"customFields": {"baseBranch": 7}})


def test_a_clean_copy_of_another_base_is_recreated(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    """baseBranch set after a first attempt: the copy moves to the feature."""
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000507")
    pool.release(first, "failed")

    again = pool.acquire("TASK-000507", FEATURE)

    assert not again.reused
    assert (again.path / "feature.txt").exists()
    assert again.base_branch == FEATURE


def test_a_branch_without_a_copy_is_recreated_on_another_base(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000508", FEATURE)
    pool.release(first, "succeeded")  # copy dropped, branch kept
    assert not first.path.exists()

    again = pool.acquire("TASK-000508")

    assert not (again.path / "feature.txt").exists()
    assert again.base_branch == "main"


def test_uncommitted_work_on_another_base_is_refused(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000509")
    (first.path / "wip.txt").write_text("half-done\n")
    pool.release(first, "failed")

    with pytest.raises(
        WorkspaceError, match=r"cut from main.*asks for feature/checkout.*uncommitted"
    ):
        pool.acquire("TASK-000509", FEATURE)

    assert (first.path / "wip.txt").read_text() == "half-done\n"
    # Asking for the base it was cut from resumes it as before.
    assert pool.acquire("TASK-000509").reused


def test_committed_work_on_another_base_is_refused(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000510")
    (first.path / "done.txt").write_text("done\n")
    sha = first.commit("done")
    pool.release(first, "succeeded")

    with pytest.raises(WorkspaceError, match="1 commit"):
        pool.acquire("TASK-000510", FEATURE)

    assert git(origin, "rev-parse", "task/TASK-000510") == sha


def test_detached_work_on_another_base_is_refused(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    """A commit on a detached HEAD is on no branch; recreating the copy would lose it."""
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000512")
    git(first.path, "checkout", "-q", "--detach")
    (first.path / "done.txt").write_text("done\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-qm", "TASK-000512: detached")
    sha = first.head()
    pool.release(first, "failed")

    with pytest.raises(WorkspaceError, match="detached HEAD holds 1 commit"):
        pool.acquire("TASK-000512", FEATURE)

    assert first.head() == sha


def test_a_legacy_branch_counts_as_cut_from_the_default(
    origin: Path, tmp_path: Path, feature_forge: Path
) -> None:
    """Branches made before the record existed: default base, commits by reachability."""
    pool = _pool(origin, tmp_path)
    first = pool.acquire("TASK-000511")
    git(origin, "config", "--remove-section", "branch.task/TASK-000511")
    pool.release(first, "failed")

    resumed = pool.acquire("TASK-000511")
    assert resumed.reused  # default base: resumed as before
    pool.release(resumed, "failed")

    moved = pool.acquire("TASK-000511", FEATURE)  # no commits of its own: recreated
    assert (moved.path / "feature.txt").exists()
    pool.release(moved, "succeeded")

    git(origin, "config", "--remove-section", "branch.task/TASK-000511")
    again = pool.acquire("TASK-000511")  # now "default" by absence of a record...
    (again.path / "own.txt").write_text("own\n")
    again.commit("own")
    pool.release(again, "failed")
    with pytest.raises(WorkspaceError, match="commit"):
        pool.acquire("TASK-000511", FEATURE)  # ...and holding a commit no other ref has


# --- the base checks are read at, and what the checks leave (U014) ------------


def test_a_fresh_copy_reads_its_conventions_at_the_base(
    pool: ExecutionWorkspacePool, origin: Path
) -> None:
    workspace = pool.acquire("TASK-000401")
    assert workspace.conventions_base == git(origin, "rev-parse", "main")


def test_a_branch_without_the_record_reads_at_its_fork_not_its_head(
    pool: ExecutionWorkspacePool, origin: Path
) -> None:
    base = git(origin, "rev-parse", "main")
    workspace = pool.acquire("TASK-000402")
    (workspace.path / ".agents").mkdir()
    (workspace.path / ".agents" / "runner.yaml").write_text("version: 1\n")
    workspace.commit("the task edits its conventions")
    pool.release(workspace, "failed")
    # A branch cut before the record existed.
    git(pool.origin, "config", "--unset", "branch.task/TASK-000402.controlPlaneBaseCommit")
    (origin / "README.md").write_text("the base moved on\n")
    git(origin, "commit", "-qam", "moved")

    again = pool.acquire("TASK-000402")
    assert again.base_revision == ""
    assert again.base_commit == again.head() != base
    assert again.conventions_base == base


def _tracked(path: Path) -> dict[str, str]:
    return {
        p.relative_to(path).as_posix(): p.read_text()
        for p in sorted(path.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(path).parts
    }


def test_restore_takes_away_what_came_after_the_snapshot(pool: ExecutionWorkspacePool) -> None:
    workspace = pool.acquire("TASK-000403")
    root = workspace.path
    (root / ".gitignore").write_text(".cache/\n")
    (root / "work.txt").write_text("the executor's\n")
    (root / "staged.txt").write_text("staged by the executor\n")
    git(root, "add", "staged.txt")
    (root / "gone.txt").write_text("to be removed by the executor\n")
    workspace.commit("before")
    (root / "gone.txt").unlink()
    (root / "README.md").write_text("changed by the executor\n")
    before = _tracked(root)
    index = git(root, "ls-files", "--stage")
    snapshot = workspace.snapshot()
    assert git(root, "ls-files", "--stage") == index  # the copy's index is not touched

    # What checks may do: add files (in new directories, with odd names),
    # change and remove the executor's, bring a removed one back, chmod.
    (root / "coverage.xml").write_text("<coverage/>\n")
    (root / "reports" / "deep").mkdir(parents=True)
    (root / "reports" / "deep" / "a *b?.txt").write_text("x\n")
    (root / "work.txt").write_text("formatted by the check\n")
    (root / "README.md").unlink()
    (root / "gone.txt").write_text("back again\n")
    (root / "staged.txt").chmod(0o755)
    (root / ".cache").mkdir()
    (root / ".cache" / "kept").write_text("ignored\n")

    workspace.restore(snapshot)

    assert _tracked(root) == {**before, ".cache/kept": "ignored\n"}
    assert not (root / "reports").exists()
    assert not os.access(root / "staged.txt", os.X_OK)
    assert workspace.snapshot() == snapshot
    sha = workspace.commit("after")
    assert sha is not None
    changed = git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(changed) == ["README.md", "gone.txt"]


def test_restore_without_changes_does_nothing(pool: ExecutionWorkspacePool) -> None:
    workspace = pool.acquire("TASK-000404")
    snapshot = workspace.snapshot()
    workspace.restore(snapshot)
    assert workspace.snapshot() == snapshot
    assert workspace.commit("nothing") is None
