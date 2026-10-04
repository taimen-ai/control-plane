"""The agent registry contract (CP-ADR-0073).

What is pinned here: the ``/agents`` routes and bodies in OpenAPI, the core's
validator ``AgentSpec`` against the catalog schema of kind ``Agent`` of the
superproject (``$defs.agentSpec``, declarative-agents D001), and the
``agent.*`` payloads of the event catalog.

The examples in ``tests/fixtures/agents`` are the examples of the superproject
(``tools/tests/test_agent_schema.py``): one per executor kind plus an identity
without placement, and ``universal-coder.yaml`` (``tools/tests/fixtures/agents``,
universal-runner U001) with a catalog of repositories as its working copy.

``workingCopy`` is data of the executor kind (TAI-ADR-0063, amendment of
CP-ADR-0073 p.1): the core checks only that it is an object, looks for secret
material in it and hashes it with the revision; its shape is the schema's
(``$defs.agentWorkingCopies``). The catalog schema is read from package-sdk
when it is checked out next to control-plane, and from the pinned copy
``tests/fixtures/superproject/object.schema.json`` otherwise
(``tests/package_sdk.py``).
"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    AGENT_IMAGE_PATTERN,
    AGENT_INSTRUCTIONS_MAX_CHARS,
    AgentPublishRequest,
    AgentSpec,
    AgentStateUpdateRequest,
    AgentStatusReport,
)
from control_plane.application.commands.agents import spec_hash_of, split_desired_state
from control_plane.domain.enums import AgentPhase, AgentState, Permission
from control_plane.domain.event_catalog import get_event_type
from control_plane_agent.catalog import CatalogError, RepositoryCatalog
from control_plane_agent.skills import _SKILL_ENV_FIXED, _SKILL_ENV_RESERVED, parse_skill_env
from tests.package_sdk import PINNED_SCHEMAS, live_schema_path, schema_path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AGENTS = FIXTURES / "agents"
PINNED_SCHEMA = PINNED_SCHEMAS / "object.schema.json"
# What the kind Agent is made of in the catalog schema.
AGENT_DEFS = (
    "agentSpec",
    "agentExecutors",
    "displayName",
    "slug",
    "typeKey",
    "permission",
    "envOrUuid",
    "nodeLabel",
    "secretName",
    "agentWorkingCopies",
    "repositoryKey",
    "repositoryAlias",
)
# The working copy of the kind claude-code in the pinned copy, as package-sdk
# carries it (universal-runner U001; ``checks`` of U023; paths with segments of
# TAI-ADR-0064, package-sdk TASK-001361): sha256 of the canonical JSON of
# ``_working_copy_contract``. Re-pin together with the copy.
WORKING_COPY_CONTRACT_SHA256 = "53d9d2f2f16300592ed3abd1137a2cb107204f920f233ff7ba5de00cabe603a6"


def _objects() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(AGENTS.glob("*.yaml"))
    ]


def _object(key: str) -> dict[str, Any]:
    return yaml.safe_load((AGENTS / f"{key}.yaml").read_text(encoding="utf-8"))


def _catalog_schema() -> dict[str, Any]:
    return json.loads(schema_path("object.schema.json").read_text(encoding="utf-8"))


CATALOG = jsonschema.Draft202012Validator(_catalog_schema())
API_VERSION = _object("coder")["apiVersion"]


def _catalog_errors(key: str, spec: dict[str, Any]) -> list[str]:
    document = {"apiVersion": API_VERSION, "kind": "Agent", "key": key, "spec": spec}
    return [error.message for error in CATALOG.iter_errors(document)]


def _core_accepts(key: str, spec: dict[str, Any]) -> bool:
    try:
        AgentPublishRequest.model_validate({"key": key, "spec": spec})
    except ValidationError:
        return False
    return True


def _set(spec: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    target = spec
    for part in path[:-1]:
        target = target.setdefault(part, {})
    if value is DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return spec


DELETE = object()
DIGEST = "0123456789abcdef" * 4


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def _ref(schema: dict[str, Any]) -> str:
    return schema["$ref"].rsplit("/", 1)[-1]


# --- OpenAPI -----------------------------------------------------------------

ROUTES: dict[tuple[str, str], tuple[str | None, str | None]] = {
    ("post", "/api/v1/agents"): ("AgentPublishRequest", "AgentOut"),
    ("post", "/api/v1/agents:validate"): ("AgentPublishRequest", "AgentValidationOut"),
    ("get", "/api/v1/agents"): (None, "AgentPageOut"),
    ("get", "/api/v1/agents/me"): (None, "AgentMeOut"),
    # What an agent learns of its connections (CP-ADR-0079 §8, I011).
    ("get", "/api/v1/agents/me/connections"): (None, "AgentConnectionListOut"),
    ("get", "/api/v1/agents/me/connections/{key}"): (None, "AgentConnectionOut"),
    ("get", "/api/v1/agents/{ref}"): (None, "AgentOut"),
    ("patch", "/api/v1/agents/{key}/state"): ("AgentStateUpdateRequest", "AgentOut"),
    ("post", "/api/v1/agents/{key}:retire"): ("AgentRetireRequest", "AgentOut"),
    ("put", "/api/v1/agents/{key}/identity"): ("AgentIdentityLinkRequest", "AgentOut"),
    ("post", "/api/v1/agents/{key}/identity:replace"): ("AgentIdentityReplaceRequest", "AgentOut"),
    ("get", "/api/v1/agents/{key}/revisions"): (None, "AgentRevisionPageOut"),
    ("get", "/api/v1/agents/{key}/status"): (None, "AgentStatusOut"),
    ("put", "/api/v1/agents/{key}/status"): ("AgentStatusReport", "AgentStatusOut"),
    # An agent's secrets by name (CP-ADR-0079 §11, I012).
    ("get", "/api/v1/agents/{key}/secrets"): (None, "AgentSecretListOut"),
    ("put", "/api/v1/agents/{key}/secrets/{name}"): ("AgentSecretSetRequest", "AgentSecretOut"),
    ("delete", "/api/v1/agents/{key}/secrets/{name}"): (None, None),
}


def test_openapi_carries_every_agents_route_with_its_bodies() -> None:
    paths = _openapi()["paths"]
    published = {
        (method, path) for path, item in paths.items() if "/agents" in path for method in item
    }
    assert published == set(ROUTES)
    for (method, path), (request, response) in ROUTES.items():
        operation = paths[path][method]
        body = operation.get("requestBody")
        if request is None:
            assert body is None, (method, path)
        else:
            assert _ref(body["content"]["application/json"]["schema"]) == request
        if response is None:
            assert "204" in operation["responses"], (method, path)
            continue
        success = "201" if (method, path) == ("post", "/api/v1/agents") else "200"
        assert _ref(operation["responses"][success]["content"]["application/json"]["schema"]) == (
            response
        )
        assert "501" not in operation["responses"], "implemented routes no longer answer 501"


def test_openapi_carries_the_revision_of_a_run() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert "agentRevisionId" in schemas["RunOut"]["properties"]
    assert "agentRevisionId" in schemas["RunStartRequest"]["properties"]
    assert "agentRevisionId" not in schemas["RunStartRequest"].get("required", [])


def test_openapi_lists_runs_by_executor() -> None:
    """``GET /runs?principalId=&agentKey=`` (amendment of 2026-09-29, Г1)."""
    operation = _openapi()["paths"]["/api/v1/runs"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert {"taskId", "claimId", "status", "limit", "cursor"} <= set(params)
    assert params["principalId"]["schema"]["anyOf"][0] == {"type": "string", "format": "uuid"}
    assert params["agentKey"]["schema"]["anyOf"][0] == {"type": "string"}
    assert not params["principalId"]["required"] and not params["agentKey"]["required"]
    assert "startedAt" in operation["description"]


def test_openapi_has_no_harness_manifests() -> None:
    """The agent revision replaces the manifest of CP-ADR-0043 (§ Consequences, D007)."""
    openapi = _openapi()
    assert [path for path in openapi["paths"] if "manifest" in path.lower()] == []
    assert [name for name in openapi["components"]["schemas"] if "Manifest" in name] == []


def test_permissions_are_in_the_catalog() -> None:
    assert {p.value for p in Permission} >= {
        "agents.read",
        "agents.manage",
        "agents.status.write",
    }
    catalog = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "authz" / "catalog.yaml").read_text("utf-8")
    )
    for name in ("agents.read", "agents.manage", "agents.status.write"):
        assert catalog["actions"][name] == {"resource": "tenant"}


# --- the core's validator against the catalog schema of the kind -------------


def test_the_pinned_schema_is_the_package_sdk_one() -> None:
    """The whole copy, not only the kind Agent: the kinds TaskType and WorkRule
    carry the core's request bodies too (declarative-cycle C001/C002)."""
    live = json.loads(live_schema_path("object.schema.json").read_text(encoding="utf-8"))
    pinned = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
    assert {name: pinned["$defs"][name] for name in AGENT_DEFS} == {
        name: live["$defs"][name] for name in AGENT_DEFS
    }
    assert pinned == live


def test_the_examples_cover_every_executor_kind_and_the_catalog_accepts_them() -> None:
    kinds = set()
    for document in _objects():
        assert document["kind"] == "Agent"
        assert _catalog_errors(document["key"], document["spec"]) == [], document["key"]
        spec = document["spec"]
        kinds.add("none" if spec.get("placement") == "none" else spec["executor"]["kind"])
    assert kinds == {"claude-code", "codex", "skills", "none"}


def _working_copy_contract(schema: dict[str, Any]) -> dict[str, Any]:
    """What decides the shape of ``workingCopy``: the forms and the dispatch by kind."""
    defs = schema["$defs"]
    return {
        "forms": {
            name: defs[name]
            for name in (
                "agentWorkingCopies",
                "repositoryKey",
                "repositoryAlias",
                # The rule of directories and neighbours (TAI-ADR-0064): without
                # it the digest holds only the $ref, not the paths it admits.
                "workingCopyPath",
            )
        },
        "dispatch": defs["agentSpec"]["allOf"],
        "section": defs["agentSpec"]["properties"]["workingCopy"],
    }


def _sha256(document: Any) -> str:
    raw = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_the_claude_code_working_copy_of_the_pinned_schema_is_the_package_one() -> None:
    """Contract of U002: the core's copy of the claude-code form is the package's.

    The core's own CI compares the copy with the pinned digest; with package-sdk
    checked out the copy is compared with its live schema too (below).
    """
    pinned = _working_copy_contract(json.loads(PINNED_SCHEMA.read_text(encoding="utf-8")))
    assert _sha256(pinned) == WORKING_COPY_CONTRACT_SHA256
    claude_code = pinned["forms"]["agentWorkingCopies"]["claude-code"]
    assert (claude_code["then"], claude_code["else"]) == (
        {"$ref": "#/$defs/agentWorkingCopies/catalog"},
        {"$ref": "#/$defs/agentWorkingCopies/single"},
    )


def test_the_working_copy_of_the_pinned_schema_is_the_live_one() -> None:
    """Without package-sdk: a failure inside the umbrella, a skip with its reason
    outside it — never a silent pass (TAI-ADR-0064, rule 4)."""
    live = json.loads(live_schema_path("object.schema.json").read_text(encoding="utf-8"))
    pinned = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
    assert _working_copy_contract(pinned) == _working_copy_contract(live)


# (path, admitted): the rule of $defs.workingCopyPath, as the runner reads it.
WORKING_COPY_PATHS: list[tuple[str, bool]] = [
    ("control-plane", True),
    ("services/control-plane", True),
    ("sdk/platform-auth-sdk", True),
    ("apps/demos/support-demo", True),
    ("a" * 100, True),
    ("/".join(["a" * 99] * 2), True),
    ("", False),
    ("..", False),
    (".", False),
    ("../platform-auth-sdk", False),
    ("services/../control-plane", False),
    ("/services/control-plane", False),
    ("services//control-plane", False),
    ("services/control-plane/", False),
    ("services\\control-plane", False),
    ("services/control-plane\n", False),
    ("Services/control-plane", False),
    ("a" * 101, False),
    ("/".join(["a" * 100] * 3), False),
]


@pytest.mark.parametrize(("path", "admitted"), WORKING_COPY_PATHS)
def test_the_runner_admits_the_paths_the_schema_admits(path: str, admitted: bool) -> None:
    """``$defs.workingCopyPath`` of the pinned copy and ``catalog.py`` agree on each path."""
    rule = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))["$defs"]["workingCopyPath"]
    by_schema = not list(jsonschema.Draft202012Validator(rule).iter_errors(path))
    entry = {"url": "https://forge.example/org/x.git", "directory": path}
    try:
        RepositoryCatalog.from_spec(
            {"repositoryField": "repositoryKey", "repositories": {"x": entry}}
        )
    except CatalogError:
        by_runner = False
    else:
        by_runner = True
    assert by_schema == by_runner == admitted


def _skill_env_names() -> Any:
    """``propertyNames`` of ``params.env`` of a ``skills`` executor in the pinned copy."""
    executors = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))["$defs"]["agentExecutors"]
    return executors["skills"]["properties"]["env"]["propertyNames"]


def _skill_env_admitted(name: str) -> tuple[bool, bool]:
    """Whether the pinned schema and the daemon (``parse_skill_env``) admit ``name``."""
    by_schema = not list(jsonschema.Draft202012Validator(_skill_env_names()).iter_errors(name))
    try:
        parse_skill_env({name: "x"}, where="params.env")
    except ValueError:
        by_daemon = False
    else:
        by_daemon = True
    return by_schema, by_daemon


# Built from the daemon's own lists: a name refused in skills.py and not in the
# schema fails here (CP-ADR-0073 Zh1).
HOST_ENV_NAMES = sorted(
    set(_SKILL_ENV_RESERVED)
    | {f"{prefix}X" for prefix in _SKILL_ENV_RESERVED}
    | {f"{prefix}SOME_SETTING" for prefix in _SKILL_ENV_RESERVED}
    | set(_SKILL_ENV_FIXED)
)


@pytest.mark.parametrize("name", HOST_ENV_NAMES)
def test_the_schema_refuses_every_host_env_name_the_daemon_refuses(name: str) -> None:
    assert _skill_env_admitted(name) == (False, False)


# (name, admitted): a refusal is a prefix or a whole name, never a substring.
SKILL_ENV_NAMES: list[tuple[str, bool]] = [
    ("PORTAL_URL", True),
    ("PORTAL_BASE_URL", True),
    ("PROXY_URL", True),
    ("MY_NODE_URL", True),
    ("MY_GIT_DIR", True),
    ("HTTPS_PROXY_URL", True),
    ("SSL_CERT_FILES", True),
    ("PATHS", True),
    ("NODE", True),
    ("GIT", True),
    ("A", True),
    ("A" * 100, True),
    ("A" * 101, False),
    ("", False),
    ("https_proxy", False),
    ("Git_Dir", False),
    ("_PORTAL_URL", False),
    ("1PORTAL", False),
    ("PORTAL-URL", False),
    ("PORTAL_TOKEN", False),
    ("PORTAL_API_KEY", False),
    ("PORTAL_CREDENTIALS", False),
]


@pytest.mark.parametrize(("name", "admitted"), SKILL_ENV_NAMES)
def test_the_schema_and_the_daemon_agree_on_skill_env_names(name: str, admitted: bool) -> None:
    assert _skill_env_admitted(name) == (admitted, admitted)


@pytest.mark.parametrize(
    "key", ["coder", "reviewer", "skills-executor", "process-bridge", "universal-coder"]
)
def test_every_catalog_example_passes_the_core_and_comes_back_as_it_was(key: str) -> None:
    """SC-007 at the core's level: nothing is added or renamed on the way in."""
    document = _object(key)
    request = AgentPublishRequest.model_validate({"key": key, "spec": document["spec"]})
    assert (
        request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
        == (document["spec"])
    )


# (example, path in spec, value): documents the catalog schema accepts.
BOTH_ACCEPT: list[tuple[str, tuple[str, ...], Any]] = [
    ("skills-executor", ("placement",), DELETE),  # absent: placed with the defaults
    ("coder", ("placement", "requires"), ["gpu", "gpu.model=a100", "zone=eu-1"]),
    ("coder", ("placement", "drainSeconds"), 0),
    ("coder", ("placement", "resources"), {"cpus": 2, "memoryMb": 4096}),
    ("coder", ("placement", "resources"), {"cpus": 64}),
    ("coder", ("work", "includeSubprojects"), True),
    ("coder", ("work", "project"), "00000000-0000-4000-8000-000000000001"),
    ("coder", ("skills", "mcpOrigins"), ["https://mcp.example.com"]),
    ("coder", ("skills", "audiences"), ["platform-core"]),
    ("skills-executor", ("skills", "concurrency"), 32),
    ("skills-executor", ("skills", "invoke"), ["oss.publish@1", "ledger.post@2.0.0"]),
    ("coder", ("workingCopy", "baseRef"), "feature/declarative-agents"),
    ("coder", ("workingCopy", "superproject"), "https://git.example/org/superproject.git"),
    ("coder", ("workingCopy", "publish"), False),
    ("coder", ("workingCopy", "review", "taskTypes"), ["coding-task"]),
    ("coder", ("workingCopy", "review", "base"), "main"),
    ("universal-coder", ("workingCopy", "publish"), False),
    ("universal-coder", ("workingCopy", "superproject"), DELETE),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "baseRef"), DELETE),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "publish"), False),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "aliases"), ["флот"]),
    (
        "universal-coder",
        ("workingCopy", "repositories", "fleet", "url"),
        "https://git.example/org/fleet.git",
    ),
    ("process-bridge", ("executor",), {"kind": "skills"}),
    ("process-bridge", ("description",), "Bridges process-runtime to the queue"),
    ("process-bridge", ("identity", "iam"), DELETE),
    ("coder", ("identity", "iam"), {"audiences": ["iam"], "scopeCeiling": ["iam:agents"]}),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["policy:check-on-behalf"]),
    # Dotted names of the IAM registry (iam-service ADR-0004), as the connector's.
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:identities.link"]),
    (
        "process-bridge",
        ("identity", "iam", "scopeCeiling"),
        ["iam:people", "iam:identities.link", "reports.v2:read_all", "crm:deals.write"],
    ),
    # executor.image (CP-ADR-0073 Z1): a tag, a digest or both; a registry with a port.
    ("coder", ("executor", "image"), "observer:1.2.0"),
    ("coder", ("executor", "image"), "observer:latest"),
    ("coder", ("executor", "image"), f"ghcr.io/org/observer@sha256:{DIGEST}"),
    ("coder", ("executor", "image"), f"ghcr.io/org/observer:1.2@sha256:{DIGEST}"),
    ("coder", ("executor", "image"), "registry.local:5000/team/sub/observer:v1"),
    ("coder", ("executor", "image"), "localhost:5000/observer:1"),
    ("skills-executor", ("executor", "image"), "ghcr.io/org/skills-host__x:2026.09.29-1"),
    ("coder", ("executor", "image"), "r.io/" + "o" * 248 + ":1"),  # 255 characters
]


@pytest.mark.parametrize(("key", "path", "value"), BOTH_ACCEPT)
def test_what_the_catalog_accepts_the_core_accepts_unchanged(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    spec = _set(copy.deepcopy(_object(key)["spec"]), path, value)
    assert _catalog_errors(key, spec) == []
    parsed = AgentSpec.model_validate(spec)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == spec


# (example, path in spec, value): documents the catalog schema rejects.
BOTH_REJECT: list[tuple[str, tuple[str, ...], Any]] = [
    ("process-bridge", ("placement",), {"replicas": 1}),  # placed without executor
    ("process-bridge", ("placement",), DELETE),  # placed by default, without executor
    ("coder", ("workspace",), {"repository": "https://git.example/org/control-plane.git"}),
    ("coder", ("skills", "concurrency"), 0),
    ("coder", ("skills", "concurrency"), 33),
    ("coder", ("skills", "local"), ["not an entry point"]),
    ("coder", ("skills", "unknownField"), True),
    ("coder", ("skills", "invoke"), ["oss.publish"]),  # a version is pinned
    ("coder", ("skills", "invoke"), ["oss.publish@1", "oss.publish@1"]),
    ("coder", ("skills", "invoke"), ["oss publish@1"]),
    ("coder", ("skills", "invoke"), ["@1"]),
    ("coder", ("placement", "requires"), ["GPU"]),
    ("coder", ("placement", "requires"), ["gpu=a 100"]),
    ("coder", ("placement", "secrets"), ["sk-ant-Very_Secret"]),
    ("coder", ("placement", "secrets"), ["claude.oauth"]),
    ("coder", ("placement", "replicas"), 21),
    ("coder", ("placement", "replicas"), -1),
    ("coder", ("placement", "drainSeconds"), 14_401),
    ("coder", ("placement", "resources"), {"gpus": 1}),
    ("coder", ("placement", "resources"), {"memoryMb": 32}),
    # Every number of the spec is hashed with the revision: an integer, not a float,
    # a string or a boolean (amendment 2026-10-03).
    ("coder", ("placement", "resources"), {"cpus": 0}),
    ("coder", ("placement", "resources"), {"cpus": 65}),
    ("coder", ("placement", "resources"), {"cpus": "2"}),
    ("coder", ("placement", "resources"), {"cpus": True}),
    ("coder", ("placement", "resources"), {"cpus": 0.5}),
    ("coder", ("placement", "resources"), {"cpus": 1.5}),
    ("coder", ("placement", "resources"), {"cpus": 63.99}),
    ("coder", ("placement", "resources"), {"memoryMb": 4096.5}),
    ("coder", ("placement", "resources"), {"memoryMb": "4096"}),
    ("coder", ("placement", "replicas"), 1.5),
    ("coder", ("placement", "replicas"), True),
    ("coder", ("placement", "drainSeconds"), 0.5),
    ("coder", ("placement", "drainSeconds"), "60"),
    ("skills-executor", ("skills", "concurrency"), 1.5),
    ("coder", ("work", "unknownField"), True),
    ("coder", ("identity", "kind"), DELETE),
    ("coder", ("identity", "kind"), "human"),
    ("coder", ("identity", "unknownField"), True),
    ("coder", ("identity", "permissions"), ["Tasks Read"]),
    ("process-bridge", ("identity", "iam", "unknownField"), True),
    ("process-bridge", ("identity", "iam", "audiences"), DELETE),
    ("process-bridge", ("identity", "iam", "audiences"), []),
    ("process-bridge", ("identity", "iam", "audiences"), ["a", "a"]),
    ("process-bridge", ("identity", "iam", "audiences"), [f"a{i}" for i in range(21)]),
    ("process-bridge", ("identity", "iam", "audiences"), [""]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), DELETE),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), []),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["Control-Plane:read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["memory:read", "memory:read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:identities."]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:.link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:identities..link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam::link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), [":read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam.identities.link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:identities link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:-link"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["iam:identities/link"]),
    (
        "process-bridge",
        ("identity", "iam", "scopeCeiling"),
        [f"control-plane:s{i}" for i in range(51)],
    ),
    ("coder", ("state",), "paused"),
    ("coder", ("unknownTopLevel",), 1),
    ("coder", ("description",), "x" * 2001),
    # executor.image (CP-ADR-0073 Z1): no implicit latest, no credentials, no whitespace, <= 255.
    ("coder", ("executor", "image"), "ghcr.io/org/observer"),
    ("coder", ("executor", "image"), "observer"),
    ("coder", ("executor", "image"), "user:secret@ghcr.io/org/observer:1"),
    ("coder", ("executor", "image"), "https://ghcr.io/org/observer:1"),
    ("coder", ("executor", "image"), "ghcr.io/org/observer :1"),
    ("coder", ("executor", "image"), "ghcr.io/Org/observer:1"),
    ("coder", ("executor", "image"), "ghcr.io/org/observer@sha256:abc"),
    ("coder", ("executor", "image"), "ghcr.io/org/observer@md5:" + "a" * 32),
    ("coder", ("executor", "image"), "ghcr.io/org/observer:" + "t" * 129),
    ("coder", ("executor", "image"), "r.io/" + "o" * 249 + ":1"),  # 256 characters
    ("coder", ("executor", "image"), ""),
]


@pytest.mark.parametrize(("key", "path", "value"), BOTH_REJECT)
def test_what_the_catalog_rejects_the_core_rejects(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    spec = _set(copy.deepcopy(_object(key)["spec"]), path, value)
    assert _catalog_errors(key, spec) != []
    assert not _core_accepts(key, spec)


# Fractional CPUs (amendment 2026-10-03): the core refuses them by the shape of the
# field, with its path, not as ``non_canonical_value`` of the revision hash; the
# catalog schema (package-sdk TASK-001344) rejects them too (BOTH_REJECT).
FRACTIONAL_CPUS = [0.5, 1.5, 63.99, float("inf"), float("nan")]


@pytest.mark.parametrize("cpus", FRACTIONAL_CPUS)
def test_fractional_cpus_are_refused_with_the_path_of_the_field(cpus: float) -> None:
    spec = _set(copy.deepcopy(_object("coder")["spec"]), ("placement", "resources"), {"cpus": cpus})
    with pytest.raises(ValidationError) as caught:
        AgentPublishRequest.model_validate({"key": "coder", "spec": spec})
    [error] = caught.value.errors()
    assert error["loc"] == ("spec", "placement", "resources", "cpus")
    assert "whole number" in error["msg"]


def test_a_whole_float_is_the_integer_it_names_and_hashes_as_it() -> None:
    """YAML ``cpus: 2.0`` is an integer to JSON Schema; the revision stores and hashes ``2``."""
    as_int = _set(copy.deepcopy(_object("coder")["spec"]), ("placement", "resources"), {"cpus": 2})
    as_float = _set(copy.deepcopy(as_int), ("placement", "resources"), {"cpus": 2.0})
    sent = AgentSpec.model_validate(as_float).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    assert type(sent["placement"]["resources"]["cpus"]) is int
    assert _hash_as_published(as_float) == _hash_as_published(as_int)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("placement", "replicas"), 2.0),
        (("placement", "drainSeconds"), 60.0),
        (("placement", "resources", "memoryMb"), 4096.0),
    ],
)
def test_every_whole_number_of_the_spec_reaches_the_hash_as_an_integer(
    path: tuple[str, ...], value: float
) -> None:
    spec = _set(copy.deepcopy(_object("coder")["spec"]), path, value)
    sent = AgentSpec.model_validate(spec).model_dump(mode="json", by_alias=True, exclude_unset=True)
    target = sent
    for part in path:
        target = target[part]
    assert type(target) is int
    _hash_as_published(spec)  # the canonical form takes it


# (example, path in spec, value): shapes of ``workingCopy`` the schema of the kind
# rejects and the core stores as they are (FR-030): the shape is not the core's.
SCHEMA_REJECTS_CORE_STORES: list[tuple[str, tuple[str, ...], Any]] = [
    ("coder", ("workingCopy", "repository"), DELETE),
    ("coder", ("workingCopy", "directory"), "Control Plane"),
    ("coder", ("workingCopy", "review", "mode"), "later"),
    ("coder", ("workingCopy", "review", "unknownField"), True),
    ("coder", ("workingCopy",), {}),
    # The catalog is a form of claude-code only.
    ("universal-coder", ("executor",), {"kind": "codex"}),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "url"), DELETE),
    ("universal-coder", ("workingCopy", "repositories", "Fleet Service"), {"url": "${X_URL}"}),
    ("universal-coder", ("workingCopy", "repositoryField"), DELETE),
    ("universal-coder", ("workingCopy", "repositories"), {}),
    ("universal-coder", ("workingCopy", "unknownField"), True),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "publish"), "no"),
    ("universal-coder", ("workingCopy", "repositories", "fleet", "aliases"), [1, None]),
]


@pytest.mark.parametrize(("key", "path", "value"), SCHEMA_REJECTS_CORE_STORES)
def test_the_core_stores_a_working_copy_whatever_its_shape(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    spec = _set(copy.deepcopy(_object(key)["spec"]), path, value)
    assert _catalog_errors(key, spec) != []
    parsed = AgentSpec.model_validate(spec)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == spec


@pytest.mark.parametrize("value", ["https://git.example/org/control-plane.git", [], 1, True])
def test_a_working_copy_is_an_object(value: Any) -> None:
    spec = _object("coder")["spec"]
    spec["workingCopy"] = value
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)


def test_an_empty_working_copy_is_stored_as_sent() -> None:
    """``null`` and absent are what they were: sent null stays null, absent stays absent."""
    spec = _object("coder")["spec"]
    spec["workingCopy"] = None
    dumped = AgentSpec.model_validate(spec).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    assert dumped["workingCopy"] is None
    del spec["workingCopy"]
    dumped = AgentSpec.model_validate(spec).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    assert "workingCopy" not in dumped


def _hash_as_published(spec: dict[str, Any]) -> str:
    sent = AgentSpec.model_validate(spec).model_dump(mode="json", by_alias=True, exclude_unset=True)
    body, _, _ = split_desired_state(sent)
    return spec_hash_of(body)[1]


def test_identity_iam_is_part_of_the_revision_and_absent_iam_hashes_as_before() -> None:
    """D014a: ``identity.iam`` is data of the spec; without it the hash is what it was."""
    with_iam = _object("process-bridge")["spec"]
    without_iam = copy.deepcopy(with_iam)
    del without_iam["identity"]["iam"]
    assert (
        "iam"
        not in AgentSpec.model_validate(without_iam).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )["identity"]
    )
    # The hash of a spec without iam is the hash of the same dict before D014a.
    assert _hash_as_published(without_iam) == spec_hash_of(split_desired_state(without_iam)[0])[1]
    assert _hash_as_published(with_iam) != _hash_as_published(without_iam)


def test_executor_image_is_part_of_the_revision_and_absent_image_hashes_as_before() -> None:
    """CP-ADR-0073 Z1-Z2: ``executor.image`` is data of the spec; no image, the hash as before."""
    without_image = _object("coder")["spec"]
    assert "image" not in without_image["executor"]
    parsed = AgentSpec.model_validate(without_image)
    assert parsed.executor is not None and parsed.executor.image is None
    assert (
        "image" not in parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)["executor"]
    )
    # The hash of a spec without image is the hash of the same dict before S016.
    assert (
        _hash_as_published(without_image) == spec_hash_of(split_desired_state(without_image)[0])[1]
    )
    with_image = copy.deepcopy(without_image)
    with_image["executor"]["image"] = "ghcr.io/org/coder:1"
    other_image = copy.deepcopy(without_image)
    other_image["executor"]["image"] = "ghcr.io/org/coder:2"
    hashes = {_hash_as_published(s) for s in (without_image, with_image, other_image)}
    assert len(hashes) == 3


def test_executor_image_in_the_core_is_the_pattern_of_the_catalog() -> None:
    """CP-ADR-0073 Z1/Z3: one grammar in the core, its OpenAPI and the catalog schema."""
    catalog = _catalog_schema()["$defs"]["agentSpec"]["properties"]["executor"]["properties"]
    published = _openapi()["components"]["schemas"]["AgentExecutorSpec"]["properties"]
    image = next(v for v in published["image"]["anyOf"] if v.get("type") == "string")
    assert image["pattern"] == AGENT_IMAGE_PATTERN == catalog["image"]["pattern"]
    assert image["maxLength"] == catalog["image"]["maxLength"] == 255
    assert "image_not_allowed" in published["image"]["description"]


# Hashes of revisions published before the working copy became data (FR-029):
# computed on the core that still checked the shape (``AgentWorkingCopySpec``).
_EARLIER_HASHES: dict[str, str] = {
    "as-is": "sha256:789f4c8f5cf628ecc49fb236146376716698be8a1886e1c321c7463c6f98c095",
    "minimal": "sha256:216128f2fe394de2975465031f1ea7a9d7c2373495d7ac943ee136e8b6c236b8",
    "full": "sha256:818f6c10bfcda9cb2e7a81653b3a7278b4341b69714acdb314931e3318de9b7d",
    "absent": "sha256:495c68fc130bce70ce97ebc827fcd0ee40608c8268aae609e527b38cc3f9ae85",
}


def _earlier_spec(variant: str) -> dict[str, Any]:
    spec = _object("coder")["spec"]
    if variant == "minimal":
        spec["workingCopy"] = {"repository": "https://git.example/org/control-plane.git"}
    elif variant == "full":
        spec["workingCopy"].update(
            {
                "baseRef": "main",
                "publish": False,
                "superproject": "https://git.example/org/superproject.git",
            }
        )
    elif variant == "absent":
        del spec["workingCopy"]
    return spec


@pytest.mark.parametrize("variant", sorted(_EARLIER_HASHES))
def test_hashes_of_earlier_revisions_do_not_change(variant: str) -> None:
    assert _hash_as_published(_earlier_spec(variant)) == _EARLIER_HASHES[variant]


def test_the_working_copy_is_part_of_the_revision() -> None:
    spec = _object("universal-coder")["spec"]
    changed = copy.deepcopy(spec)
    changed["workingCopy"]["repositories"]["fleet"]["baseRef"] = "develop"
    reordered = copy.deepcopy(spec)
    reordered["workingCopy"]["repositories"] = dict(
        reversed(list(spec["workingCopy"]["repositories"].items()))
    )
    assert _hash_as_published(changed) != _hash_as_published(spec)
    assert _hash_as_published(reordered) == _hash_as_published(spec)


def test_executor_parameters_are_not_interpreted() -> None:
    spec = _object("coder")["spec"]
    spec["executor"]["params"] = {"anything": {"nested": [1, "two"]}}
    assert AgentSpec.model_validate(spec).executor.params == {"anything": {"nested": [1, "two"]}}  # type: ignore[union-attr]


def test_the_core_is_stricter_where_the_adr_says_so() -> None:
    """CP-ADR-0073 §1: an agent without permissions could not even read its work."""
    spec = _object("coder")["spec"]
    spec["identity"]["permissions"] = []
    assert _catalog_errors("coder", spec) == []
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)


def test_instructions_are_not_bound_by_the_short_string_limit() -> None:
    spec = _object("coder")["spec"]
    spec["executor"]["instructions"] = "x" * AGENT_INSTRUCTIONS_MAX_CHARS
    AgentSpec.model_validate(spec)
    spec["executor"]["instructions"] += "x"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)


def test_key_is_a_dns_label() -> None:
    spec = _object("coder")["spec"]
    for bad in ("Coder", "coder_1", "-coder", "c" * 64, "coder@2"):
        with pytest.raises(ValidationError):
            AgentPublishRequest.model_validate({"key": bad, "spec": spec})


def test_state_update_needs_something_to_change() -> None:
    with pytest.raises(ValidationError):
        AgentStateUpdateRequest.model_validate({})
    assert AgentStateUpdateRequest.model_validate({"replicas": 0}).replicas == 0


def test_status_report_speaks_the_phases_of_the_domain() -> None:
    schema = AgentStatusReport.model_json_schema(by_alias=True)
    assert set(schema["properties"]["phase"]["enum"]) == {p.value for p in AgentPhase}
    with pytest.raises(ValidationError):  # observedAt without a zone is ambiguous
        AgentStatusReport.model_validate(
            {
                "phase": "running",
                "instances": {"desired": 1, "ready": 1},
                "observedAt": "2026-09-27T10:00:00",
            }
        )


def test_desired_states_match_the_domain() -> None:
    schema = AgentSpec.model_json_schema(by_alias=True)
    assert set(schema["properties"]["state"]["enum"]) == {s.value for s in AgentState}


# --- events ------------------------------------------------------------------

SAMPLES: dict[str, dict[str, Any]] = {
    "agent.revision_published": {
        "key": "selfdev-coder",
        "revision": 2,
        "specHash": "sha256:" + "a" * 64,
        "previousRevision": 1,
        "executorKind": "claude-code",
        "placed": True,
        "permissionsChanged": False,
    },
    "agent.state_changed": {
        "key": "selfdev-coder",
        "state": "stopped",
        "replicas": 1,
        "previousState": "running",
        "previousReplicas": 1,
    },
    "agent.status_changed": {
        "key": "selfdev-coder",
        "phase": "waiting_for_node",
        "previousPhase": None,
        "reasonCode": "no_matching_node",
        "node": None,
        "observedRevision": None,
        "observedAt": "2026-09-27T10:00:00+00:00",
    },
    "agent.retired": {
        "key": "selfdev-coder",
        "revision": 3,
        "principalId": None,
        "reason": "replaced by selfdev-coder-2",
        "releasedClaims": 0,
    },
}


@pytest.mark.parametrize("event_type", sorted(SAMPLES))
def test_agent_events_are_in_the_catalog(event_type: str) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == "agent"
    schema = entry.current.schema
    jsonschema.validate(SAMPLES[event_type], schema)
    assert set(schema["required"]) == set(SAMPLES[event_type])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**SAMPLES[event_type], "key": None}, schema)
