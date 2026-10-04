"""Neighbours of a catalog run through the daemon (universal-runner U006, acceptance).

A repository's ``runner.yaml`` at the base revision names its neighbours; the
daemon places them at the revisions the superproject pins, from mirrors of
their own, so a pool of the same repository in the same replica keeps its
task branches. A change of ``runner.yaml`` on the task branch does not apply;
a changed neighbour stops the run as ``neighbour_modified`` and nothing is
published; a task of the superproject gets its submodules in place. The
checkpoint records the blob of ``AGENTS.md`` at the base and at the commit
handed in.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.catalog import RepositoryPools
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.revision import AgentRevision, workspace_pool_of
from control_plane_agent.workspace import Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import Make
from tests.client.test_agent_catalog import (
    _branches,
    _get,
    _return_to_work,
    _setup,
    _task,
    _workspace_checkpoints,
)

CONVENTIONS = "version: 1\nneighbours: [control-plane]\n"


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


class Forge:
    """control-plane, memory-service and the superproject pinning both."""

    def __init__(self, tmp_path: Path) -> None:
        root = tmp_path / "forge"
        self.core = _repo(root / "control-plane", {"AGENTS.md": "# core\n"})
        self.pin = _git(self.core, "rev-parse", "HEAD")
        # control-plane moves on past the pin.
        self.tip = _commit(self.core, {"README.md": "later\n"}, "later")
        self.memory = _repo(
            root / "memory-service",
            {"AGENTS.md": "# memory\n", ".agents/runner.yaml": CONVENTIONS},
        )
        self.superproject = _repo(
            root / "superproject",
            {"AGENTS.md": "# superproject\n", ".agents/runner.yaml": CONVENTIONS},
        )
        for path, revision in (
            ("control-plane", self.pin),
            ("memory-service", _git(self.memory, "rev-parse", "HEAD")),
        ):
            _git(
                self.superproject,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{revision},{path}",
            )
        _git(self.superproject, "commit", "-qm", "pin the submodules")
        spec = {
            "repositoryField": "repositoryKey",
            "superproject": "superproject",
            "repositories": {
                key: {"url": f"file://{path}"}
                for key, path in (
                    ("control-plane", self.core),
                    ("memory-service", self.memory),
                    ("superproject", self.superproject),
                )
            },
        }
        revision = AgentRevision(
            key="coder",
            revision=1,
            revision_id="11111111-1111-1111-1111-111111111111",
            spec_hash="sha256:0",
            spec={"workingCopy": spec},
            status="active",
            state="running",
        )
        self.root = tmp_path / "w"
        pools = workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(self.root)})
        assert isinstance(pools, RepositoryPools)
        self.pools = pools
        self.mirrors = self.root / ".mirrors"


Act = Callable[[Workspace], None]


class Adapter:
    """Does ``act`` in the copy (a file of the task by default), and may stop."""

    def __init__(self, act: Act | None = None, *, blocked: bool = False) -> None:
        self.act = act
        self.blocked = blocked
        self.seen: list[Workspace] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        self.seen.append(workspace)
        if self.act is not None:
            self.act(workspace)
        else:
            (workspace.path / f"{task['publicId']}.txt").write_text("work\n")
        if self.blocked:
            await client.create_checkpoint(
                str(run["id"]), kind="blocked", data={"reason": "a person has to decide"}
            )
        return [ArtifactSpec(type="report", name="summary", content={"summary": "done"})]


async def _work(sdk: Make, key: str, adapter: Adapter, forge: Forge, cycles: int = 2) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(
            sdk_client, adapter, poll_interval=0.05, max_cycles=cycles, workspaces=forge.pools
        )
        await agent.run_forever()


async def test_a_memory_service_task_beside_a_live_control_plane_pool(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    # A control-plane task stops with work of its own: its pool lives in this
    # replica, and its branch is published before ``blocked`` (U007, WIP).
    core_task = await _task(client, s["admin"], "control-plane")

    def commit_work(workspace: Workspace) -> None:
        _commit(workspace.path, {"core.txt": "work\n"}, "part of it")

    await _work(sdk, s["agent"], Adapter(commit_work, blocked=True), forge)
    branch = f"task/{core_task['publicId']}"
    pool_mirror = forge.mirrors / "control-plane.git"
    local = _git(pool_mirror, "rev-parse", f"refs/heads/{branch}")
    assert _git(forge.core, "rev-parse", f"refs/heads/{branch}") == local
    # Then the forge's branch of that name is moved somewhere else.
    _git(forge.core, "branch", "-f", branch, forge.tip)

    memory_task = await _task(client, s["admin"], "memory-service")
    placed: list[str] = []

    def look_at_the_neighbour(workspace: Workspace) -> None:
        neighbour = workspace.container / "control-plane"
        placed.append(_git(neighbour, "rev-parse", "HEAD"))
        _commit(workspace.path, {"AGENTS.md": "# memory, revised\n"}, "revise the conventions")

    await _work(sdk, s["agent"], Adapter(look_at_the_neighbour), forge)

    record = await _get(client, s["admin"], f"/tasks/{memory_task['id']}")
    assert record["status"] == "done"
    # At the superproject's pin, not at the tip of control-plane.
    assert placed == [forge.pin]
    # The control-plane pool's branch is where its work left it.
    assert _git(pool_mirror, "rev-parse", f"refs/heads/{branch}") == local
    # Only the task's repository is published.
    assert _branches(forge.memory) == [f"task/{memory_task['publicId']}"]
    assert _branches(forge.core) == [branch]
    [opened, committed] = await _workspace_checkpoints(client, s["admin"], memory_task["id"])
    assert opened["neighbours"] == committed["neighbours"] == {"control-plane": forge.pin}
    base = _git(forge.memory, "rev-parse", "main")
    assert opened["conventionsRevision"] == opened["baseRevision"] == base
    assert opened["agentsMdBase"] == _git(forge.memory, "rev-parse", "main:AGENTS.md")
    head = committed["head"]
    assert committed["agentsMdHead"] == _git(forge.memory, "rev-parse", f"{head}:AGENTS.md")
    assert committed["agentsMdHead"] != opened["agentsMdBase"]


async def test_runner_yaml_changed_on_the_task_branch_does_not_apply(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "memory-service")

    def drop_the_neighbours(workspace: Workspace) -> None:
        _commit(
            workspace.path,
            {".agents/runner.yaml": "version: 1\nneighbours: []\nunknown: field\n"},
            "no neighbours",
        )

    await _work(sdk, s["agent"], Adapter(drop_the_neighbours, blocked=True), forge)
    await _return_to_work(client, s["admin"], task["id"], repositoryKey="memory-service")
    placed: list[bool] = []

    def look_and_work(workspace: Workspace) -> None:
        placed.append((workspace.container / "control-plane").is_dir())
        (workspace.path / "work.txt").write_text("work\n")

    await _work(sdk, s["agent"], Adapter(look_and_work), forge)

    assert placed == [True]
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
    checkpoints = await _workspace_checkpoints(client, s["admin"], task["id"])
    assert all(c["neighbours"] == {"control-plane": forge.pin} for c in checkpoints)
    # The change is on the branch, for review; the base still has its own.
    published = _git(forge.memory, "show", f"task/{task['publicId']}:.agents/runner.yaml")
    assert "neighbours: []" in published


async def test_a_changed_neighbour_fails_the_run_and_nothing_is_published(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "memory-service")

    def change_the_neighbour(workspace: Workspace) -> None:
        (workspace.path / "work.txt").write_text("work\n")
        (workspace.container / "control-plane" / "README.md").write_text("patched\n")

    adapter = Adapter(change_the_neighbour)
    await _work(sdk, s["agent"], adapter, forge)

    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert (record["status"], record["systemStatusCategory"]) == ("blocked", "blocked")
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "neighbour_modified")
    assert "control-plane" in run["output"]["reason"]
    assert str(tmp_path) not in run["output"]["reason"]
    comments = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
    assert any("neighbour_modified" in c["body"] for c in comments)
    # Nothing committed, nothing published, anywhere; the change stays.
    assert _branches(forge.memory) == _branches(forge.core) == []
    [workspace] = adapter.seen
    assert (workspace.path / "work.txt").exists()
    assert (workspace.container / "control-plane" / "README.md").read_text() == "patched\n"

    # Returned to work with the neighbour still changed: not worked beside.
    await _return_to_work(client, s["admin"], task["id"], repositoryKey="memory-service")
    await _work(sdk, s["agent"], adapter, forge)
    assert len(adapter.seen) == 1
    runs = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    newest = max(runs, key=lambda r: r["attempt"])
    assert (newest["status"], newest["failureReason"]) == ("failed", "neighbour_modified")
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["systemStatusCategory"] == "blocked"


async def test_a_superproject_task_has_its_submodule_in_place(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "superproject")
    seen: list[tuple[str, str]] = []

    def work_in_the_superproject(workspace: Workspace) -> None:
        submodule = workspace.path / "control-plane"
        seen.append((_git(submodule, "rev-parse", "HEAD"), (submodule / "README.md").read_text()))
        (workspace.path / "notes.md").write_text("work\n")

    await _work(sdk, s["agent"], Adapter(work_in_the_superproject), forge)

    assert seen == [(forge.pin, "control-plane\n")]
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
    branch = f"task/{task['publicId']}"
    assert _branches(forge.superproject) == [branch]
    # The published commit keeps the pin and carries the work.
    assert _git(forge.superproject, "ls-tree", branch, "control-plane").split()[2] == forge.pin
    assert "notes.md" in _git(forge.superproject, "ls-tree", "--name-only", branch).split()
    assert _branches(forge.core) == []
    [opened, committed] = await _workspace_checkpoints(client, s["admin"], task["id"])
    assert opened["repositoryKey"] == "superproject"
    assert committed["neighbours"] == {"control-plane": forge.pin}
    assert committed["agentsMdHead"] == opened["agentsMdBase"]


async def test_a_superproject_task_that_merged_a_moved_pin_publishes_it(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """The daemon's commit keeps the pointer the merge of the base brought in."""
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "superproject")

    def merge_the_base(workspace: Workspace) -> None:
        # The superproject moves its pin while the task runs; the task merges it.
        _git(forge.superproject, "update-index", "--cacheinfo", f"160000,{forge.tip},control-plane")
        _git(forge.superproject, "commit", "-qm", "move the pin")
        _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")
        (workspace.path / "notes.md").write_text("work\n")

    await _work(sdk, s["agent"], Adapter(merge_the_base), forge)

    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
    branch = f"task/{task['publicId']}"
    assert _git(forge.superproject, "ls-tree", branch, "control-plane").split()[2] == forge.tip
    assert "notes.md" in _git(forge.superproject, "ls-tree", "--name-only", branch).split()


async def test_a_superproject_task_that_followed_a_pin_the_base_moved_is_done(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """TASK-001387: a pin moved in the base during the run is not the executor's change."""
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "superproject")

    def follow_the_base(workspace: Workspace) -> None:
        # The submodule-lag rule moves the pin while the task runs; the task
        # merges its base and updates the submodule checkout to match.
        _git(forge.superproject, "update-index", "--cacheinfo", f"160000,{forge.tip},control-plane")
        _git(forge.superproject, "commit", "-qm", "move the pin")
        _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")
        submodule = workspace.path / "control-plane"
        _git(submodule, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        _git(submodule, "checkout", "-q", "--detach", forge.tip)
        (workspace.path / "notes.md").write_text("work\n")

    await _work(sdk, s["agent"], Adapter(follow_the_base), forge)

    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert run["failureReason"] != "neighbour_modified"
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
    branch = f"task/{task['publicId']}"
    assert _git(forge.superproject, "ls-tree", branch, "control-plane").split()[2] == forge.tip
    assert "notes.md" in _git(forge.superproject, "ls-tree", "--name-only", branch).split()


async def test_a_superproject_task_that_rolled_a_pin_back_is_told_how_to_fix_it(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """TASK-001396: a merge of the base, then ``commit -a`` over the old checkout."""
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "superproject")

    def merge_then_commit_all(workspace: Workspace) -> None:
        _git(forge.superproject, "update-index", "--cacheinfo", f"160000,{forge.tip},control-plane")
        _git(forge.superproject, "commit", "-qm", "move the pin")
        _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")
        (workspace.path / "notes.md").write_text("work\n")
        _git(workspace.path, "add", "-A")
        _git(workspace.path, "commit", "-qm", "work")

    await _work(sdk, s["agent"], Adapter(merge_then_commit_all), forge)

    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "neighbour_pointer_regressed")
    reason = run["output"]["reason"]
    assert forge.tip in reason and forge.pin in reason
    assert "git checkout origin/main -- control-plane" in reason
    assert str(tmp_path) not in reason
    # The next attempt reads it in the task's comments; nothing was published.
    comments = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
    assert any("git checkout origin/main -- control-plane" in c["body"] for c in comments)
    assert _branches(forge.superproject) == []


class PackageSdk:
    """A submodule of the superproject ``neighbours`` does not name, as package-sdk."""

    def __init__(self, forge: Forge) -> None:
        self.forge = forge
        self.repo = _repo(forge.superproject.parent / "package-sdk", {})
        self.pin = _git(self.repo, "rev-parse", "HEAD")
        self.tip = _commit(self.repo, {"README.md": "later\n"}, "later")
        self._point(self.pin)

    def _point(self, revision: str) -> None:
        sp = self.forge.superproject
        _git(sp, "update-index", "--add", "--cacheinfo", f"160000,{revision},package-sdk")
        _git(sp, "commit", "-qm", "pin package-sdk")

    def merge_moved_base(self, workspace: Workspace) -> None:
        """The executor checks the SDK out, the base moves it, the task merges the base."""
        _git(workspace.path, "clone", "-q", "--no-checkout", str(self.repo), "package-sdk")
        _git(workspace.path / "package-sdk", "checkout", "-q", "--detach", self.pin)
        self._point(self.tip)
        _git(workspace.path, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        _git(workspace.path, "merge", "-q", "--no-edit", "origin/main")
        assert _git(workspace.path, "status", "--porcelain") == "M package-sdk"
        (workspace.path / "notes.md").write_text("work\n")


async def test_the_daemon_s_commit_keeps_the_base_s_pointer_of_an_undeclared_submodule(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """9b30f86, 54d149a: the daemon's own commit took the old checkout of package-sdk."""
    s = await _setup(client)
    forge = Forge(tmp_path)
    package = PackageSdk(forge)
    task = await _task(client, s["admin"], "superproject")

    await _work(sdk, s["agent"], Adapter(package.merge_moved_base), forge)

    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
    branch = f"task/{task['publicId']}"
    assert _git(forge.superproject, "ls-tree", branch, "package-sdk").split()[2] == package.tip
    assert "notes.md" in _git(forge.superproject, "ls-tree", "--name-only", branch).split()


async def test_commit_all_over_an_undeclared_submodule_is_told_how_to_fix_it(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    package = PackageSdk(forge)
    task = await _task(client, s["admin"], "superproject")

    def merge_then_commit_all(workspace: Workspace) -> None:
        package.merge_moved_base(workspace)
        _git(workspace.path, "add", "-A")
        _git(workspace.path, "commit", "-qm", "work")

    await _work(sdk, s["agent"], Adapter(merge_then_commit_all), forge)

    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "neighbour_pointer_regressed")
    reason = run["output"]["reason"]
    assert package.tip in reason and package.pin in reason
    assert "git checkout origin/main -- package-sdk" in reason
    assert str(tmp_path) not in reason
    assert _branches(forge.superproject) == []


async def test_a_superproject_task_that_moved_a_pin_itself_is_blocked(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = Forge(tmp_path)
    task = await _task(client, s["admin"], "superproject")

    def move_the_pin(workspace: Workspace) -> None:
        _git(workspace.path, "update-index", "--cacheinfo", f"160000,{forge.tip},control-plane")

    await _work(sdk, s["agent"], Adapter(move_the_pin), forge)

    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "neighbour_modified")
    assert "pointer" in run["output"]["reason"]
    assert _branches(forge.superproject) == []
