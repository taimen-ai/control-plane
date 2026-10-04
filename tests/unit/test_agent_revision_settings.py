"""A revision turned into the daemon's settings (control_plane_agent.revision, D006).

The examples of the catalog schema (``tests/fixtures/agents/``) are what a
package installs; each maps onto the arguments the daemon, its adapters and
its skill executor already take. What the host owns stays the host's.
"""

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane_agent.main import EchoAdapter, adapter_for_revision
from control_plane_agent.revision import (
    DEFAULT_DRAIN_SECONDS,
    AgentRevision,
    RevisionError,
    config_mode,
    mirror,
    settings_of,
    skills_environ,
    workspace_pool_of,
)
from control_plane_agent.skills import (
    ENV_CONCURRENCY,
    ENV_HTTP_ORIGINS,
    ENV_LOCAL_ENV,
    ENV_LOCAL_ISOLATION,
    ENV_LOCAL_PACKAGES,
    ENV_MCP_ORIGINS,
    ENV_PROTOCOLS,
)
from control_plane_claude import ClaudeCodeAdapter
from control_plane_codex import CodexAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"
HOST = {"CONTROL_PLANE_CLAUDE_MCP": "0", "CONTROL_PLANE_CLAUDE_LOGS": "0"}


def _revision(name: str, **overrides: Any) -> AgentRevision:
    spec = copy.deepcopy(yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))["spec"])
    spec.pop("state", None)
    spec.update(overrides)
    return AgentRevision(
        key="coder",
        revision=4,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec=spec,
        status="active",
        state="running",
        workspace_id="22222222-2222-2222-2222-222222222222",
    )


# --- work, drain -----------------------------------------------------------


def test_the_coder_example_takes_its_work() -> None:
    settings = settings_of(_revision("coder.yaml"))

    assert settings.workspace_id == "22222222-2222-2222-2222-222222222222"
    assert (settings.only_assigned, settings.include_subprojects) == (True, False)
    assert settings.task_types == frozenset({"coding-task"})
    assert settings.drain_seconds == 14400


def test_defaults_are_the_schemas_not_the_env_modes() -> None:
    """``onlyAssigned`` defaults to true in the kind's schema: take only what is meant for you."""
    settings = settings_of(_revision("reviewer.yaml"))

    assert settings.only_assigned is True
    assert settings.task_types == frozenset()
    assert settings.drain_seconds == DEFAULT_DRAIN_SECONDS
    assert settings_of(_revision("process-bridge.yaml")).drain_seconds is None


def test_no_placement_is_placed_with_the_defaults() -> None:
    """No ``placement`` field: a placed agent (one replica), drained like any other."""
    revision = _revision("coder.yaml")
    del revision.spec["placement"]  # type: ignore[attr-defined]

    settings = settings_of(revision)
    assert settings.drain_seconds == DEFAULT_DRAIN_SECONDS
    adapter = adapter_for_revision(revision, HOST)
    assert isinstance(adapter, ClaudeCodeAdapter)


@pytest.mark.parametrize(
    "review",
    [
        {"mode": "human", "taskType": "code-review-merge", "reviewer": "someone"},
        {"mode": "none"},
        # Once a reviewer was required; now nothing reads the section.
        {"mode": "agent"},
    ],
)
def test_working_copy_review_is_ignored(review: dict[str, Any]) -> None:
    """Review is an acceptance check of the task type (CP-ADR-0073, amendment A2).

    A revision published with the section still runs, without an auto-review.
    """
    revision = _revision("coder.yaml")
    revision.spec["workingCopy"]["review"] = review
    without = _revision("coder.yaml")
    del without.spec["workingCopy"]["review"]

    assert settings_of(revision) == settings_of(without)
    assert "review_policy" not in settings_of(revision).agent_kwargs()


def test_config_mode() -> None:
    assert config_mode({}) == "auto"
    assert config_mode({"CONTROL_PLANE_AGENT_CONFIG": "env"}) == "env"
    with pytest.raises(RevisionError):
        config_mode({"CONTROL_PLANE_AGENT_CONFIG": "yaml"})


# --- skills ---------------------------------------------------------------------


def test_skill_settings_of_the_revision_replace_the_hosts() -> None:
    host = {
        ENV_PROTOCOLS: "local,http,mcp",
        ENV_MCP_ORIGINS: "https://elsewhere.example",
        ENV_CONCURRENCY: "8",
        ENV_LOCAL_ISOLATION: "thread",
    }
    revision = _revision("coder.yaml")
    values = skills_environ(revision, host)

    assert values is not None
    assert values[ENV_PROTOCOLS] == "local,http"
    assert values[ENV_LOCAL_PACKAGES] == ",".join(revision.spec["skills"]["local"])
    assert values[ENV_HTTP_ORIGINS] == "https://platform.example.com"
    # Not in the spec: not run, whatever the host says.
    assert ENV_MCP_ORIGINS not in values
    assert ENV_CONCURRENCY not in values
    # The host's own.
    assert values[ENV_LOCAL_ISOLATION] == "thread"

    assert skills_environ(_revision("skills-executor.yaml"), {})[ENV_CONCURRENCY] == "2"  # type: ignore[index]
    assert skills_environ(_revision("reviewer.yaml"), host) is None


def _skills_with(params: dict[str, Any]) -> AgentRevision:
    revision = _revision("skills-executor.yaml")
    revision.spec["executor"] = {"kind": "skills", "params": params}
    return revision


def test_the_settings_of_a_skills_executor_reach_the_skill_host() -> None:
    """``executor.params.env`` (CP-ADR-0073, amendment 2026-10-01)."""
    revision = _skills_with({"env": {"PORTAL_URL": "https://portal.example.test"}})
    assert adapter_for_revision(revision, HOST) is None
    values = skills_environ(revision, {ENV_LOCAL_ENV: '{"OTHER": "host"}'})
    assert values is not None
    # The revision owns the variable: the host's value does not leak in.
    assert json.loads(values[ENV_LOCAL_ENV]) == {"PORTAL_URL": "https://portal.example.test"}

    # No settings, empty settings: the host's value is dropped all the same.
    for params in ({}, {"env": {}}):
        values = skills_environ(_skills_with(params), {ENV_LOCAL_ENV: '{"OTHER": "host"}'})
        assert values is not None and ENV_LOCAL_ENV not in values
    # Another kind has no such parameter.
    assert ENV_LOCAL_ENV not in (skills_environ(_revision("coder.yaml"), {}) or {})


def test_bad_settings_of_a_skills_executor_are_a_revision_error() -> None:
    with pytest.raises(RevisionError, match="a secret"):
        skills_environ(_skills_with({"env": {"PORTAL_API_KEY": "x"}}), {})
    with pytest.raises(RevisionError, match="must be a string"):
        skills_environ(_skills_with({"env": {"PORTAL_URL": None}}), {})
    with pytest.raises(RevisionError, match="at most"):
        skills_environ(_skills_with({"env": {f"V{i}": "x" for i in range(51)}}), {})


# --- executor ------------------------------------------------------------------------


def test_claude_code_params_and_instructions_come_from_the_revision() -> None:
    adapter = adapter_for_revision(_revision("coder.yaml"), HOST)

    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter.cli.model == "claude-opus-5-5"
    assert adapter.cli.permission_mode == "bypassPermissions"
    assert adapter.cli.timeout_seconds == 10800
    assert list(adapter.cli.disallowed_tools) == ["WebFetch"]
    assert adapter.instructions == "Соглашения репозитория: …"
    assert adapter.prompt_file is None


def test_revision_instructions_replace_the_conventions_file(tmp_path: Path) -> None:
    adapter = adapter_for_revision(_revision("coder.yaml"), HOST)
    assert isinstance(adapter, ClaudeCodeAdapter)
    stale = tmp_path / "conventions.md"
    stale.write_text("from the file")
    adapter.prompt_file = stale

    prompt = adapter._build_prompt({"id": "t", "publicId": "TASK-1", "title": "x"}, {})

    assert "### Agent conventions" in prompt
    assert "Соглашения репозитория" in prompt
    assert "from the file" not in prompt


async def test_tools_narrow_on_top_of_the_withheld_commands(tmp_path: Path) -> None:
    revision = _revision("coder.yaml")
    revision.spec["executor"]["params"]["tools"] = {
        "allow": ["Bash(uv run pytest:*)", "mcp__control-plane__cp_complete_run"],
        "deny": ["WebFetch"],
    }
    adapter = adapter_for_revision(
        revision,
        {
            **HOST,
            "CONTROL_PLANE_CLAUDE_MCP": "1",
            "CONTROL_PLANE_CLAUDE_RUNTIME_DIR": str(tmp_path),
        },
    )
    assert isinstance(adapter, ClaudeCodeAdapter)

    await adapter._narrow_tools()
    await adapter._narrow_tools()  # once per process
    argv = adapter.cli.command(session_id="s", resume=False)

    denied = argv[argv.index("--disallowedTools") + 1 :]
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--disallowedTools")]
    # The authoritative commands stay withheld whatever the revision allows.
    assert "mcp__control-plane__cp_complete_run" in denied
    assert "mcp__control-plane__cp_complete_run" not in allowed
    assert "WebFetch" in denied
    assert denied.count("WebFetch") == 1
    assert allowed == ["Bash(uv run pytest:*)"]


def test_codex_params_come_from_the_revision() -> None:
    adapter = adapter_for_revision(_revision("reviewer.yaml"), {"CONTROL_PLANE_CODEX_LOGS": "0"})

    assert isinstance(adapter, CodexAdapter)
    assert adapter.cli.sandbox == "read-only"
    assert adapter.credential_class == "subscription"
    assert adapter.cli.timeout_seconds == 3600
    assert adapter.instructions == ""


@pytest.mark.parametrize(
    ("executor", "message"),
    [
        ({"kind": "claude-code", "params": {"modle": "x"}}, "unknown executor params"),
        ({"kind": "claude-code", "params": {"permissionMode": "yolo"}}, "permissionMode"),
        ({"kind": "claude-code", "params": {"tools": {"only": []}}}, "allow and deny"),
        ({"kind": "codex", "params": {"sandbox": "none"}}, "sandbox"),
        ({"kind": "codex", "params": {"timeoutSeconds": "60"}}, "timeoutSeconds"),
        ({"kind": "skills", "params": {"x": 1}}, "takes only params.env"),
        ({"kind": "skills", "params": {"env": {"PORTAL_TOKEN": "x"}}}, "a secret"),
        (
            {"kind": "skills", "params": {"env": {"CONTROL_PLANE_URL": "x"}}},
            "belongs to the host",
        ),
        (
            {"kind": "skills", "params": {"env": {"HTTPS_PROXY": "http://proxy.test"}}},
            "belongs to the host",
        ),
        ({"kind": "skills", "params": {"env": {"GIT_DIR": "x"}}}, "belongs to the host"),
        ({"kind": "skills", "params": {"env": {"portal": "x"}}}, "not a variable name"),
        ({"kind": "skills", "params": {"env": {"PORTAL_URL": 1}}}, "must be a string"),
        ({"kind": "skills", "params": {"env": []}}, "must be an object"),
        ({"kind": "opencode"}, "not run by this daemon"),
    ],
)
def test_params_an_adapter_refuses(executor: dict[str, Any], message: str) -> None:
    with pytest.raises(RevisionError, match=message):
        adapter_for_revision(_revision("reviewer.yaml", executor=executor), HOST)


def test_skills_and_echo_kinds() -> None:
    assert adapter_for_revision(_revision("skills-executor.yaml"), HOST) is None
    echo = adapter_for_revision(_revision("reviewer.yaml", executor={"kind": "echo"}), HOST)
    assert isinstance(echo, EchoAdapter)
    with pytest.raises(RevisionError, match="no executor"):
        adapter_for_revision(_revision("process-bridge.yaml"), HOST)


# --- working copy -----------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "init")
    return path


def test_a_repository_url_is_mirrored_once_and_checked(tmp_path: Path) -> None:
    source = _repository(tmp_path / "forge" / "service")
    url = f"file://{source}"
    mirrors = tmp_path / "mirrors"

    path = mirror(url, mirrors)
    assert path == mirrors / "service.git"
    assert _git(path, "rev-parse", "--is-bare-repository") == "true"
    assert mirror(url, mirrors) == path  # the host's mirror is reused

    other = _repository(tmp_path / "elsewhere" / "service")
    with pytest.raises(RevisionError, match="not"):
        mirror(f"file://{other}", mirrors)
    # A directory on this host is used as it is.
    assert mirror(str(source), mirrors) == source


@pytest.mark.parametrize(
    "working_copy",
    [
        # A catalog left with installation variables unresolved (U005 reads
        # catalogs: tests/unit/test_agent_catalog.py).
        yaml.safe_load((FIXTURES / "universal-coder.yaml").read_text(encoding="utf-8"))["spec"][
            "workingCopy"
        ],
        {"directory": "service"},
        {"repository": ""},
        {"repository": ["https://git.example/org/service.git"]},
    ],
)
def test_a_working_copy_this_runner_cannot_build_is_refused(
    tmp_path: Path, working_copy: dict[str, Any]
) -> None:
    """The core no longer checks the shape (U002): the daemon refuses, not crashes."""
    revision = _revision("reviewer.yaml", workingCopy=working_copy)
    with pytest.raises(RevisionError, match="workingCopy"):
        workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path)})


def test_the_working_copy_of_a_revision(tmp_path: Path) -> None:
    source = _repository(tmp_path / "forge" / "service")
    revision = _revision(
        "reviewer.yaml",
        workingCopy={
            "repository": f"file://{source}",
            "directory": "service",
            "baseRef": "main",
        },
    )
    root = tmp_path / "worktrees"

    pool = workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(root)})

    assert pool is not None
    assert pool.origin == (root / ".mirrors" / "service.git").resolve()
    assert (pool.repo_dir, pool.base_ref) == ("service", "main")
    # publish defaults to true: branches go back where the mirror came from.
    assert pool.push_remote == "origin"

    local = _revision("reviewer.yaml", workingCopy={"repository": str(source), "publish": False})
    pool = workspace_pool_of(local, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(root)})
    assert pool is not None
    assert (pool.origin, pool.push_remote) == (source.resolve(), "")
    assert workspace_pool_of(_revision("reviewer.yaml"), {}) is None


def test_neighbours_without_a_superproject_are_refused(tmp_path: Path) -> None:
    source = _repository(tmp_path / "forge" / "service")
    sdk = _repository(tmp_path / "forge" / "sdk")
    revision = _revision(
        "reviewer.yaml",
        workingCopy={"repository": str(source), "neighbours": {"sdk": str(sdk)}},
    )
    with pytest.raises(RevisionError, match="superproject"):
        workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        # bool("false") is True: a string must not publish.
        ({"publish": "false"}, "publish must be a boolean"),
        ({"publish": 0}, "publish must be a boolean"),
        ({"neighbours": ["sdk"]}, "neighbours must map"),
        ({"neighbours": {"sdk": 1}}, "neighbours must map"),
        ({"neighbours": {"../sdk": "SDK"}, "superproject": "SDK"}, "unsafe neighbour path"),
        ({"superproject": ["x"]}, "superproject must be"),
        ({"baseRef": 3}, "baseRef must be"),
        ({"directory": {"a": 1}}, "directory must be"),
    ],
)
def test_a_one_repository_section_the_schema_refuses_is_a_revision_error(
    tmp_path: Path, fields: dict[str, Any], message: str
) -> None:
    """Every error of the section is exit 2 at start (RevisionError), never a traceback."""
    source = _repository(tmp_path / "forge" / "service")
    sdk = str(_repository(tmp_path / "forge" / "sdk"))
    resolved = {
        name: (
            {k: sdk if v == "SDK" else v for k, v in value.items()}
            if isinstance(value, dict)
            else sdk
            if value == "SDK"
            else value
        )
        for name, value in fields.items()
    }
    revision = _revision("reviewer.yaml", workingCopy={"repository": str(source), **resolved})
    with pytest.raises(RevisionError, match=message):
        workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")})


def test_a_budget_that_is_not_a_number_is_a_revision_error(tmp_path: Path) -> None:
    source = _repository(tmp_path / "forge" / "service")
    revision = _revision("reviewer.yaml", workingCopy={"repository": str(source)})
    with pytest.raises(RevisionError, match="MAX_WORKSPACES"):
        workspace_pool_of(
            revision,
            {
                "CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w"),
                "CONTROL_PLANE_AGENT_MAX_WORKSPACES": "many",
            },
        )
