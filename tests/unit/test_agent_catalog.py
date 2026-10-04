"""The repository catalog of a universal runner (control_plane_agent.catalog, U005).

The core stores ``workingCopy`` as data (CP-ADR-0073, amendment 2026-09-30):
the daemon reads the catalog form itself and refuses what the schema of the
executor kind refuses, resolves a task's key or alias case-insensitively,
makes a pool per key only when that key's work comes, and removes the copy
of a task that moved to another repository only when it holds no work.
"""

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane_agent import workspace as workspace_module
from control_plane_agent.catalog import (
    REPOSITORY_CHANGED,
    REPOSITORY_UNKNOWN,
    CatalogError,
    RepositoryBlocked,
    RepositoryCatalog,
    RepositoryPools,
    is_catalog,
    previous_repository,
)
from control_plane_agent.revision import AgentRevision, RevisionError, workspace_pool_of
from control_plane_agent.workspace import (
    ExecutionWorkspacePool,
    WorkspaceBusyError,
    WorkspaceError,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "agents" / "universal-coder.yaml"


def _example() -> dict[str, Any]:
    """The catalog of the schema's example, variables resolved as installation does."""
    spec = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))["spec"]["workingCopy"]
    for key, entry in spec["repositories"].items():
        entry["url"] = f"https://forge.example/org/{key}.git"
    return spec


# The superproject's older name, an alias in the example; read from the
# fixture, which keeps the product name out of this file (test_branding).
OLD_NAME = _example()["repositories"]["superproject"]["aliases"][1]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"{path.name}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial")
    return path


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


# --- the catalog ------------------------------------------------------------


def test_the_schema_example_is_a_catalog() -> None:
    spec = _example()
    assert is_catalog(spec)
    assert not is_catalog({"repository": "https://forge.example/org/x.git"})

    catalog = RepositoryCatalog.from_spec(spec)

    assert catalog.field == "repositoryKey"
    assert catalog.superproject == "superproject"
    assert len(catalog.entries) == 10
    memory = catalog.entries["memory-service"]
    assert (memory.base_ref, memory.directory, memory.publish) == ("master", "memory-service", True)
    assert catalog.entries["superproject"].directory == "superproject"
    assert catalog.entries["platform-auth-sdk"].publish is False


def test_a_catalog_of_the_layout_with_segments_is_taken() -> None:
    """``services/``, ``sdk/``, ``apps/`` (TAI-ADR-0064); keys stay what they were."""
    spec = _example()
    layout = {
        "control-plane": "services/control-plane",
        "memory-service": "services/memory-service",
        "platform-auth-sdk": "sdk/platform-auth-sdk",
        "fleet": "services/fleet",
    }
    for key, directory in layout.items():
        spec["repositories"][key]["directory"] = directory

    catalog = RepositoryCatalog.from_spec(spec)

    assert {key: catalog.entries[key].directory for key in layout} == layout
    assert catalog.resolve("platform-auth-sdk") == catalog.entries["platform-auth-sdk"]


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("control-plane", "control-plane"),
        ("Control-Plane", "control-plane"),
        ("  fleet ", "fleet"),
        ("суперпроект", "superproject"),
        ("СУПЕРПРОЕКТ", "superproject"),
        (OLD_NAME, "superproject"),
        (OLD_NAME.upper(), "superproject"),
        ("gamma", None),
        ("", None),
        ("   ", None),
    ],
)
def test_keys_and_aliases_resolve_case_insensitively(name: str, key: str | None) -> None:
    entry = RepositoryCatalog.from_spec(_example()).resolve(name)
    assert (entry.key if entry else None) == key


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (None, "no repository key"),
        ({}, "no repository key"),
        ({"repositoryKey": None}, "no repository key"),
        ({"repositoryKey": ""}, "no repository key"),
        ({"repositoryKey": "  "}, "no repository key"),
        ({"repositoryKey": 7}, "must be a string, got int"),
        ({"repositoryKey": ["fleet"]}, "must be a string, got list"),
        ({"repositoryKey": {"key": "fleet"}}, "must be a string, got dict"),
        ({"repositoryKey": "gamma"}, "'gamma' is not a repository"),
        # A URL is not a key, and the value is not echoed: it may carry a
        # credential.
        ({"repositoryKey": "https://t0ken@forge.example/org/fleet.git"}, "is not a repository"),
        # Another field of the task is not the one the catalog names.
        ({"repository": "fleet"}, "no repository key"),
    ],
)
def test_a_task_without_a_known_key_is_refused(fields: Any, message: str) -> None:
    catalog = RepositoryCatalog.from_spec(_example())
    task = {} if fields is None else {"customFields": fields}
    with pytest.raises(RepositoryBlocked, match=message) as caught:
        catalog.entry_of(task)
    assert caught.value.code == REPOSITORY_UNKNOWN
    assert "t0ken" not in caught.value.reason


def test_a_task_names_its_repository_by_key_or_alias() -> None:
    catalog = RepositoryCatalog.from_spec(_example())
    assert catalog.entry_of({"customFields": {"repositoryKey": "fleet"}}).key == "fleet"
    assert catalog.entry_of({"customFields": {"repositoryKey": "Суперпроект"}}).key == (
        "superproject"
    )


def _broken(change: Any) -> dict[str, Any]:
    spec = copy.deepcopy(_example())
    change(spec)
    return spec


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_broken(lambda s: s.update(neighbours={})), "unknown field"),
        (_broken(lambda s: s.pop("repositoryField")), "repositoryField"),
        (_broken(lambda s: s.update(repositoryField="customFields.key")), "repositoryField"),
        (_broken(lambda s: s.update(repositoryField=None)), "repositoryField"),
        (_broken(lambda s: s.update(repositories={})), "at least one"),
        (_broken(lambda s: s.update(repositories=["fleet"])), "at least one"),
        (_broken(lambda s: s.update(publish="yes")), "publish"),
        (_broken(lambda s: s["repositories"].update({"Fleet": {"url": "x"}})), "lowercase"),
        (_broken(lambda s: s["repositories"].update({"fleet": "https://x"})), "object"),
        (_broken(lambda s: s["repositories"]["fleet"].pop("url")), "url is missing"),
        (_broken(lambda s: s["repositories"]["fleet"].update(url="  ")), "url is missing"),
        (
            _broken(lambda s: s["repositories"]["fleet"].update(url="${SELFDEV_FLEET_URL}")),
            "unresolved installation variable",
        ),
        (_broken(lambda s: s["repositories"]["fleet"].update(branch="x")), "unknown field"),
        (_broken(lambda s: s["repositories"]["fleet"].update(baseRef="")), "baseRef"),
        (_broken(lambda s: s["repositories"]["fleet"].update(publish=1)), "publish"),
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases="old")), "aliases"),
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases=[""])), "aliases"),
        # An alias repeating a key or another alias, in any case.
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases=["FLEET"])), "twice"),
        (
            _broken(lambda s: s["repositories"]["fleet"].update(aliases=["Control-Plane"])),
            "twice",
        ),
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases=["Суперпроект"])), "twice"),
        (
            _broken(
                lambda s: (
                    s["repositories"]["fleet"].update(directory="shared"),
                    s["repositories"]["skill-sdk"].update(directory="shared"),
                )
            ),
            "share the directory",
        ),
        (
            _broken(lambda s: s["repositories"]["fleet"].update(directory="superproject")),
            "key of another entry",
        ),
        (_broken(lambda s: s["repositories"]["fleet"].update(directory=3)), "directory"),
        (_broken(lambda s: s["repositories"]["fleet"].update(directory="")), "directory"),
        (_broken(lambda s: s["repositories"]["fleet"].update(directory="../up")), "directory"),
        (_broken(lambda s: s["repositories"]["fleet"].update(directory="Fleet")), "directory"),
        # Paths with segments (TAI-ADR-0064): the rule of $defs.workingCopyPath.
        *(
            (
                _broken(lambda s, d=d: s["repositories"]["fleet"].update(directory=d)),
                "relative path",
            )
            for d in (
                "services/../fleet",
                "services//fleet",
                "services/fleet/",
                "/services/fleet",
                "services\\fleet",
                "services/fleet\n",
                "services/.fleet",
            )
        ),
        # One clone inside another: by a directory, or by a key without one.
        (
            _broken(lambda s: s["repositories"]["fleet"].update(directory="superproject/fleet")),
            "lies inside",
        ),
        (
            _broken(
                lambda s: (
                    s["repositories"]["fleet"].update(directory="services"),
                    s["repositories"]["skill-sdk"].update(directory="services/skill-sdk"),
                )
            ),
            "lies inside",
        ),
        (
            _broken(lambda s: s["repositories"]["skill-sdk"].update(directory="fleet/skill-sdk")),
            "lies inside",
        ),
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases=["a b"])), "aliases"),
        (_broken(lambda s: s["repositories"]["fleet"].update(aliases=[" old"])), "aliases"),
        # The address: https without credentials, query or fragment; one entry each.
        (
            _broken(lambda s: s["repositories"]["fleet"].update(url="ssh://forge.example/f.git")),
            "https URL",
        ),
        (_broken(lambda s: s["repositories"]["fleet"].update(url="--upload-pack=x")), "https"),
        (_broken(lambda s: s["repositories"]["fleet"].update(url="https:///f.git")), "https"),
        (
            _broken(
                lambda s: s["repositories"]["fleet"].update(url="https://u:t@forge.example/f.git")
            ),
            "credentials",
        ),
        (
            _broken(lambda s: s["repositories"]["fleet"].update(url="https://u@forge.example/f")),
            "credentials",
        ),
        (
            _broken(lambda s: s["repositories"]["fleet"].update(url="https://forge.example/f?t=1")),
            "query",
        ),
        (
            _broken(
                lambda s: s["repositories"]["fleet"].update(
                    url="HTTPS://forge.example/org/Control-Plane/"
                )
            ),
            "same url",
        ),
        (_broken(lambda s: s.update(superproject=OLD_NAME)), "superproject"),
    ],
)
def test_a_catalog_the_schema_refuses_is_refused(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        RepositoryCatalog.from_spec(spec)
    # The revision is refused as a whole (exit 2), not a task at a time.
    with pytest.raises(RevisionError, match="workingCopy"):
        workspace_pool_of(_revision(spec), {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": "/nonexistent"})


def test_the_previous_key_is_the_newest_workspace_checkpoints() -> None:
    checkpoints = [
        {"kind": "blocked", "data": {"repositoryKey": "nope"}},
        {"kind": "execution.workspace", "data": {"workspaceKey": "TASK-1"}},
        {"kind": "execution.workspace", "data": {"repositoryKey": "beta"}},
        {"kind": "execution.workspace", "data": {"repositoryKey": "alpha"}},
    ]
    assert previous_repository(checkpoints, "execution.workspace") == "beta"
    assert previous_repository(checkpoints[:2], "execution.workspace") is None
    assert previous_repository([], "execution.workspace") is None
    assert previous_repository([{"kind": "execution.workspace", "data": None}], "x") is None


# --- pools ------------------------------------------------------------------


def _catalog_pools(tmp_path: Path, **extra: Any) -> tuple[RepositoryPools, dict[str, Path]]:
    forge = tmp_path / "forge"
    origins = {name: _origin(forge / name) for name in ("alpha", "beta")}
    spec = {
        "repositoryField": "repositoryKey",
        "repositories": {
            "alpha": {"url": f"file://{origins['alpha']}", "aliases": ["old-alpha"]},
            "beta": {"url": f"file://{origins['beta']}", "baseRef": "main", "publish": False},
        },
        **extra,
    }
    pools = workspace_pool_of(
        _revision(spec), {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")}
    )
    assert isinstance(pools, RepositoryPools)
    return pools, origins


def test_a_pool_per_key_is_made_when_its_work_comes(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    mirrors = tmp_path / "w" / ".mirrors"
    # Nothing is cloned for a catalog nobody has worked in yet.
    assert not mirrors.exists() or not any(mirrors.iterdir())

    alpha = pools.pool_for(pools.catalog.entries["alpha"])

    assert sorted(p.name for p in mirrors.glob("*.git")) == ["alpha.git"]
    assert pools.pool_for(pools.catalog.entries["alpha"]) is alpha
    assert (alpha.repo_dir, alpha.repository_key, alpha.push_remote) == ("alpha", "alpha", "origin")
    assert alpha.root == pools.root
    beta = pools.pool_for(pools.catalog.entries["beta"])
    # publish: false — a copy nobody publishes from; baseRef from the entry.
    assert (beta.push_remote, beta.base_ref) == ("", "main")
    assert sorted(p.name for p in mirrors.glob("*.git")) == ["alpha.git", "beta.git"]


def test_a_repository_that_cannot_be_mirrored_fails_its_task_only(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    broken = RepositoryCatalog.from_spec(
        {
            "repositoryField": "repositoryKey",
            "repositories": {"gone": {"url": f"file://{tmp_path / 'forge' / 'gone'}"}},
        }
    ).entries["gone"]
    with pytest.raises(WorkspaceError, match="repository gone"):
        pools._make_pool(broken, True)
    # Nothing on the host: no pool without being asked to make one.
    assert pools._make_pool(broken, False) is None


def test_the_checkpoint_carries_the_key_and_the_base_revision(tmp_path: Path) -> None:
    pools, origins = _catalog_pools(tmp_path)
    pool = pools.pool_for(pools.catalog.entries["beta"])

    workspace = pool.acquire("TASK-1")
    data = workspace.checkpoint_data

    assert data["repositoryKey"] == "beta"
    assert data["baseRevision"] == _git(origins["beta"], "rev-parse", "main")
    assert workspace.path == pools.root / "TASK-1" / "beta"
    pool.release(workspace, "failed")


def test_the_one_repository_form_is_one_pool_as_before(tmp_path: Path) -> None:
    source = _origin(tmp_path / "forge" / "service")
    pool = workspace_pool_of(
        _revision({"repository": str(source), "directory": "service", "publish": False}),
        {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")},
    )
    assert isinstance(pool, ExecutionWorkspacePool)
    workspace = pool.acquire("TASK-1")
    # No key, no new fields: the checkpoint is what it was.
    assert set(workspace.checkpoint_data) == {
        "workspaceKey",
        "branch",
        "baseCommit",
        "reused",
        "baseBranch",
    }
    pool.release(workspace, "failed")


# --- a task that moved ------------------------------------------------------


def test_a_move_without_work_removes_the_old_copy_and_branch(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    alpha.release(alpha.acquire("TASK-1"), "failed")
    mirror = tmp_path / "w" / ".mirrors" / "alpha.git"
    assert (pools.root / "TASK-1" / "alpha").is_dir()

    pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])

    assert not (pools.root / "TASK-1" / "alpha").exists()
    assert _git(mirror, "branch", "--list", "task/TASK-1") == ""
    # Again: nothing left to remove, nothing refused.
    pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])


@pytest.mark.parametrize("work", ["commit", "uncommitted"])
def test_a_move_with_work_is_refused_and_leaves_it(tmp_path: Path, work: str) -> None:
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    workspace = alpha.acquire("TASK-1")
    (workspace.path / "work.txt").write_text("work\n")
    if work == "commit":
        _git(workspace.path, "add", "-A")
        _git(workspace.path, "commit", "-qm", "TASK-1: part")
    head = workspace.head()
    alpha.release(workspace, "failed")

    with pytest.raises(RepositoryBlocked, match="alpha to beta") as caught:
        pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])

    assert caught.value.code == REPOSITORY_CHANGED
    assert ("commit" if work == "commit" else "uncommitted") in caught.value.reason
    assert (workspace.path / "work.txt").exists()
    assert workspace.head() == head


def test_a_move_keeps_commits_made_on_a_detached_head(tmp_path: Path) -> None:
    """Work on a detached HEAD is on no branch: removing the copy would lose it."""
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    workspace = alpha.acquire("TASK-1")
    _git(workspace.path, "checkout", "-q", "--detach")
    (workspace.path / "work.txt").write_text("work\n")
    _git(workspace.path, "add", "-A")
    _git(workspace.path, "commit", "-qm", "TASK-1: detached")
    head = workspace.head()
    alpha.release(workspace, "failed")

    with pytest.raises(RepositoryBlocked, match="detached HEAD") as caught:
        pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])

    assert caught.value.code == REPOSITORY_CHANGED
    assert workspace.head() == head


def test_a_move_warns_of_a_stash_left_on_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    workspace = alpha.acquire("TASK-1")
    (workspace.path / "README.md").write_text("stashed\n")
    _git(workspace.path, "stash", "push", "-q", "-m", "half-done")
    alpha.release(workspace, "failed")
    mirror = tmp_path / "w" / ".mirrors" / "alpha.git"
    # Recorded at the call, not through logging: levels and handlers other
    # tests leave behind do not decide this test.
    warnings: list[str] = []
    monkeypatch.setattr(
        workspace_module.logger, "warning", lambda msg, *args: warnings.append(msg % args)
    )

    pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])

    assert warnings == ["TASK-1: a stash made on task/TASK-1 stays behind"]
    # The copy goes, the stash stays where git keeps it.
    assert not workspace.path.exists()
    assert "half-done" in _git(mirror, "log", "-g", "--format=%gs", "refs/stash")


def test_no_move_when_the_key_names_the_same_repository(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    workspace = alpha.acquire("TASK-1")
    (workspace.path / "work.txt").write_text("work\n")
    alpha.release(workspace, "failed")

    for previous in (None, "", "alpha", "OLD-ALPHA"):
        pools.settle_change("TASK-1", previous, pools.catalog.entries["alpha"])
    assert (workspace.path / "work.txt").exists()


def test_a_move_from_a_key_no_longer_known_or_never_mirrored_goes_on(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    mirrors = tmp_path / "w" / ".mirrors"

    pools.settle_change("TASK-1", "retired-repository", pools.catalog.entries["beta"])
    # alpha was never worked in on this host: it is not cloned to look.
    pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])

    assert not mirrors.exists() or not any(mirrors.iterdir())


def test_a_move_waits_for_nobody_holding_the_copy(tmp_path: Path) -> None:
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    held = alpha.acquire("TASK-1")
    try:
        with pytest.raises(WorkspaceBusyError):
            pools.settle_change("TASK-1", "alpha", pools.catalog.entries["beta"])
    finally:
        alpha.release(held, "failed")
    assert held.path.exists()


def test_pools_sharing_a_root_tell_their_copies_apart(tmp_path: Path) -> None:
    """A directory named like another pool's copy is not that pool's to prune."""
    pools, _ = _catalog_pools(tmp_path)
    alpha = pools.pool_for(pools.catalog.entries["alpha"])
    beta = pools.pool_for(pools.catalog.entries["beta"])
    alpha.max_workspaces = 0
    kept = beta.acquire("TASK-1")
    beta.release(kept, "failed")
    # A repository named like alpha's copy in beta's container (a neighbour).
    stranger = pools.root / "TASK-1" / "alpha"
    _origin(stranger)

    alpha.release(alpha.acquire("TASK-2"), "succeeded")

    assert alpha._owns(pools.root / "TASK-1" / "beta") is False
    assert beta._owns(kept.path) is True
    assert alpha._owns(stranger) is False
    assert stranger.is_dir() and kept.path.is_dir()


def test_replicas_taking_a_repository_at_once_clone_it_once(tmp_path: Path) -> None:
    """Pools of two replicas on one host share the mirrors: one clones, the other waits."""
    import threading

    from control_plane_agent.revision import mirror

    source = _origin(tmp_path / "forge" / "alpha")
    mirrors = tmp_path / "w" / ".mirrors"
    results: list[Path | Exception] = []

    def take() -> None:
        try:
            results.append(mirror(f"file://{source}", mirrors))
        except Exception as exc:  # pragma: no cover - the failure this guards against
            results.append(exc)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [mirrors / "alpha.git"] * 4
