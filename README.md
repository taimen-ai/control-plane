# control-plane

*English. Russian version: [README.ru.md](README.ru.md)*

Control Plane is a product-neutral coordination platform in which humans,
AI agents and
automated processes are **equal participants of the organization**:
tasks, principals, work sessions, **atomic task claims with a lease and a fencing
token**, runs (execution attempts), artifacts, approvals, an organization
model (workspaces / roles / capabilities / skills), an immutable journal of
domain events, a transactional outbox and realtime updates over WebSocket.

**v0.2 Organization Model**: a human and an agent are both a `Principal` and work through
a single transactional coordination protocol. The differences are expressed through
`principal.kind`, roles, capabilities, skills and permissions — not through separate
task systems.

**v0.3 Harness Protocol & Execution Runtime**: real working environments connect
to the Control Plane through a single versioned protocol,
[`control-harness`](docs/harness-protocol.md): harness registration when a
session is opened, a bootstrap context (`GET /harness/context`), discovery
of available work (`GET /work/available`, subtree filters by workspaces),
Run Context, checkpoints, approval gates with suspension, artifact revisions,
skill versions (`name@version`), cooperative cancellation, execution audit
(`run_actions`) and run budgets. The repository ships the official SDK
`control_plane_client`, the `control-plane` CLI, the product-neutral MCP server
`control-plane-mcp` for human harnesses
([Codex](docs/codex.md), [Claude Code](docs/claude-code.md)) and the reference autonomous daemon
`control-plane-agent`. A Principal can leave one
process, come back through another harness and continue the work — continuity
lives on the server, not in the conversation.

**v0.5 Project Model**: portfolios, programs, projects, subprojects and work
streams are described by **one** Workspace tree. A Project is a Project Profile
bound one-to-one to a Workspace; the parent of a project is computed as the
nearest ancestor with a profile, and there is no separate `parentProjectId` in the model
([ADR-0031](docs/adr/0031-project-profile-and-lifecycle.md)). On top of that:
typed Workspace Types, immutable versioned Project
Templates, a configurable lifecycle with five system categories,
append-only configuration revisions with a deterministic effective config and
provenance, a typed governance lattice (a descendant can only tighten
the frame of its ancestor), generic external references and project-scoped discovery/context.
The same version closes the operational debt of v0.4: per-tenant delivery
isolation, operator redrive, journal retention/archive/rebuild, end-to-end
`X-Run-Id` and removal of the product-name compatibility window.

## Architecture summary

- **Modular monolith**, two processes from one codebase: the HTTP API (FastAPI)
  and a background worker.
- **PostgreSQL is the single source of truth.** Current state lives in
  normalized tables; domain events are an append-only journal for audit,
  realtime and downstream consumers (not event sourcing).
- **One command — one transaction.** A successful mutation atomically writes: the new
  state + a record in `events` + a record in `outbox` (+ `pg_notify`, which
  PostgreSQL delivers to subscribers only on commit).
- **Concurrency is enforced by the database**: `SELECT ... FOR UPDATE`, a partial
  unique index "one active claim per task", unique constraints,
  optimistic versioning (`If-Match`/ETag), fencing tokens, idempotency keys,
  `FOR UPDATE SKIP LOCKED` in the worker.
- **Lease + fencing.** Sessions and claims are leases renewed by heartbeats.
  An expired claim is requisitioned atomically by the claim command itself — correctness
  does not depend on the worker running. An old session that wakes up cannot write
  its result: its fencing token no longer equals the task's `claim_epoch`.
- **Organization model (v0.2).** Hierarchical workspaces; roles
  (scope = a workspace subtree), capabilities and a skill registry; tasks describe
  the required executor declaratively (`requirements`), claim checks
  eligibility; a task dependency graph with cycle protection and readiness;
  `Run` — an execution attempt with a pinned fencing token (a zombie run
  cannot write a result after takeover); append-only artifacts; approvals
  "one record — one decision". Claim = `tasks.claim` ∧ eligibility ∧
  readiness ∧ concurrency rules.

Details: [docs/architecture.md](docs/architecture.md),
[docs/api.md](docs/api.md), [docs/harness-protocol.md](docs/harness-protocol.md),
operations — [docs/operations.md](docs/operations.md), upgrading —
[docs/migration-v0.6.md](docs/migration-v0.6.md), decisions —
[docs/adr/](docs/adr/README.md).

## System requirements

- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose (for PostgreSQL and containerized runs)
- PostgreSQL 16 (the only supported database)

## Quick start with Docker Compose

```bash
cp .env.example .env            # set CP_BOOTSTRAP_TOKEN
docker compose up -d --build db api worker
curl -f http://localhost:8000/health/ready
```

`api` applies the migrations itself (`alembic upgrade head`) on startup. OpenAPI:
<http://localhost:8000/docs>.

The build context is the root of the umbrella layout, two levels **above** the repository
(`services/control-plane`, TAI-ADR-0064): the enforcement SDK lives in the
`platform-auth-sdk` repository at `sdk/platform-auth-sdk` and is wired in by path, because
the platform has no shared internal package index yet. Compose takes this into account
by itself; manually the image is built like this:

```bash
docker build -f services/control-plane/Dockerfile -t control-plane ../..
```

## Running locally (no container for the application)

```bash
uv sync
docker compose up -d db                  # PostgreSQL on localhost:5433
uv run alembic upgrade head              # migrations
CP_BOOTSTRAP_TOKEN=dev-token uv run uvicorn control_plane.main:app --port 8000
# in a second terminal — the worker:
uv run python -m control_plane.worker
```

## Bootstrap (one-time initialization)

While the system has no tenant yet, a guarded endpoint is available:

```bash
curl -s -X POST http://localhost:8000/api/v1/bootstrap \
  -H "Authorization: Bearer $CP_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenantSlug":"acme","tenantName":"Acme Corp","adminDisplayName":"Alice"}'
```

The response contains the tenant, the administrative principal and **the full admin API key —
it is shown exactly once**. A repeated call → `409 already_bootstrapped`.

## Main scenario (example)

```bash
ADMIN_KEY="cp_..."   # from the bootstrap response
B="http://localhost:8000/api/v1"
H="Authorization: Bearer $ADMIN_KEY"

# 1. An agent and its key
AGENT=$(curl -s -X POST $B/principals -H "$H" -H "Content-Type: application/json" \
  -d '{"kind":"agent","displayName":"Build Agent"}')
AGENT_ID=$(echo $AGENT | jq -r .id)
AGENT_KEY=$(curl -s -X POST $B/principals/$AGENT_ID/api-keys -H "$H" \
  -H "Content-Type: application/json" \
  -d '{"permissions":["sessions.open","tasks.read","tasks.write","tasks.claim","events.read"]}' \
  | jq -r .key)
AH="Authorization: Bearer $AGENT_KEY"

# 2. The agent's session
SESSION_ID=$(curl -s -X POST $B/sessions -H "$AH" -H "Content-Type: application/json" \
  -d '{"clientName":"builder","clientVersion":"1.0.0"}' | jq -r .id)

# 3. A task
TASK=$(curl -s -X POST $B/tasks -H "$H" -H "Content-Type: application/json" \
  -d '{"title":"Ship the release","priority":"high"}')
TASK_ID=$(echo $TASK | jq -r .id)

# 4. Atomic claim (lease + fencing token)
CLAIM=$(curl -s -X POST "$B/tasks/${TASK_ID}:claim" -H "$AH" \
  -H "Content-Type: application/json" -d "{\"sessionId\":\"$SESSION_ID\"}")
CLAIM_ID=$(echo $CLAIM | jq -r .id); TOKEN=$(echo $CLAIM | jq -r .fencingToken)

# 5. Lease heartbeats
curl -s -X POST "$B/sessions/${SESSION_ID}:heartbeat" -H "$AH" > /dev/null
curl -s -X POST "$B/claims/${CLAIM_ID}:heartbeat"     -H "$AH" > /dev/null

# 6. Completion with optimistic concurrency + fencing
VERSION=$(curl -s "$B/tasks/$TASK_ID" -H "$AH" | jq -r .version)
curl -s -X POST "$B/tasks/${TASK_ID}:complete" -H "$AH" \
  -H "If-Match: \"task-$VERSION\"" -H "Content-Type: application/json" \
  -d "{\"claimId\":\"$CLAIM_ID\",\"fencingToken\":$TOKEN}"
```

Realtime: `GET /api/v1/events` (paginated by sequence) and the WebSocket
`/api/v1/events/ws?after=<sequence>` — the client stores the last processed
sequence and reads what it missed after a reconnect.

## Operator front end

The Control Plane web console (11 screens: overview, focus, tasks, agents, approvals,
event journal, organization, task types, principals, memory) does not live here but
as a module of the platform's management panel — `platform-core/apps/web/src/products/control-plane`.
Sign-in to the panel is via platform-core's Keycloak; the panel talks to this API through the
platform-api gateway `/api/v1/services/control-plane/…`, which federates the user's
identity into IAM (`federation:exchange`) and obtains a token for the
`control-plane` audience. Every human needs a row in `iam_principal_bindings`.

## Tests

```bash
docker compose --profile test up -d db-test   # ephemeral PostgreSQL :5434
uv run pytest                                 # unit + integration + concurrency + e2e
# or: make test                               # the same, one run per working copy
```

- Each pytest process creates its own database next to the one in
  `CP_TEST_DATABASE_URL` (`<name>_<epoch>_<hex>`) and drops it at the end;
  databases left by a killed run are dropped by the next run after 6 hours.
  Parallel runs on one server therefore do not block each other.
- Every test is capped at 120 s (`pytest-timeout`, `timeout` in
  `pyproject.toml`); a long test raises it with `@pytest.mark.timeout(<s>)`.
  A hung lock wait fails with a stack dump instead of hanging the run.
- `make test` takes `flock` on `.pytest.lock`: a second `make test` in the same
  working copy exits immediately with a "tests already running" message
  (exit code 75). With `CP_TEST_DATABASE_URL` set it does not start the
  compose database; extra
  arguments go through `make test PYTEST_ARGS="tests/unit -x"`. Automated
  runners should run tests through `make test`.

Lint and types: `uv run ruff check . && uv run ruff format --check . && uv run mypy`.

## Migrations

```bash
uv run alembic upgrade head          # apply
uv run alembic revision --autogenerate -m "..."   # new migration (requires a running database)
uv run alembic downgrade -1         # roll back
```

`/health/ready` returns 503 if the database revision is behind the head in the code.

## v0.2 example: organization and the single protocol

```bash
# Organization: workspace, role, capability, skill (admin)
WS=$(curl -s -X POST $B/workspaces -H "$H" -H "Content-Type: application/json" \
  -d '{"slug":"platform","name":"Platform"}' | jq -r .id)
curl -s -X POST $B/roles -H "$H" -H "Content-Type: application/json" \
  -d '{"slug":"software-engineer","name":"Software Engineer"}' > /dev/null
# Agent profile: /principals/{id}/roles|capabilities|skills

# A task with requirements instead of a specific executor
curl -s -X POST $B/tasks -H "$H" -H "Content-Type: application/json" -d '{
  "title": "Fix regression #483", "workspaceId": "'$WS'",
  "requirements": {"roles": ["software-engineer"]}
}'

# Agent: claim -> start-run -> artifact -> succeed (the task completes atomically)
curl -s -X POST "$B/tasks/${TASK_ID}:start-run" -H "$AH" \
  -H "Content-Type: application/json" \
  -d "{\"claimId\":\"$CLAIM_ID\",\"fencingToken\":$TOKEN}"
curl -s -X POST "$B/runs/${RUN_ID}:succeed" -H "$AH" \
  -H "Content-Type: application/json" -d '{"output":{"pr":484}}'
```

## Known MVP limitations

- Delivery of outbox records is a structured log (an extension point for
  webhooks/a broker); after retries are exhausted the record is dead-lettered
  (`available_at` far in the future) with `last_error` for diagnostics;
  there is no automatic re-drive.
- Rate limiting (429) is not implemented.
- Session/claim heartbeats are not journaled in `events` (they are lease renewals,
  not domain changes) — see ADR-0003.
- Delegation permissions are stored but do not narrow the rights of an on-behalf-of
  session; rights are always determined by the API key of the acting principal.
- PATCH of a task with a live claim held by someone else is forbidden even for an admin (release
  the claim with `:release`); a claim with a dead session does not block the task.
- An idempotent replay of API key creation returns `key: null` — the one-time
  secret is never persisted to disk; if you lost the response, issue a new key.
- Bootstrap does not support `Idempotency-Key` (a one-time operation, protected by
  an advisory lock + `409 already_bootstrapped`).
- One WS-gateway process = one LISTEN connection; horizontal scaling of
  API instances is possible, but each holds its own LISTEN.
- Delivery of events to readers is delayed until the longest open
  writing transaction commits (see architecture.md).
- **The v0.3 replay defect is fixed in v0.4:** delivery and the cursor go by the pair
  `(tx_id, sequence)` under a stable horizon — a committed event
  cannot be skipped forever because of the commit order of concurrent
  transactions. The former strict xfail
  `test_event_prefix_is_complete_under_xid_inversion` is a regular passing
  regression test. The public cursor is an opaque `ec1_...`; the legacy
  `after=<sequence>` is accepted within the compatibility window (see
  docs/architecture.md).
- v0.2: workspace membership is organizational metadata and does not take part in
  eligibility; task requirements are a strict AND, without an expression language; only
  `done` satisfies a dependency (`cancelled` keeps blocking until the
  edge is removed).
- v0.3: `/work/available` post-filters eligibility per task (a page
  may be shorter than limit; on very large backlogs discovery is O(pages));
  the event WS does not filter by type (the client filters itself); skill version
  ranges (`^2`) are not supported — only `name` and `name@version`; server-side
  enforcement of the run budget is limited to recording actions/checkpoints (wall-clock
  execution time is the harness's responsibility); `control-plane login` stores the key in the macOS
  Keychain or a 0600 file — other OS secure storages are not integrated;
  `pendingApprovals` in the context is limited to 50 records (the full list is
  `GET /approvals`); discovery/claimability answer the question
  "is the task organizationally available" and do not check that the key has
  `tasks.claim` — claim remains the authoritative gate.
- v0.5: `GET /workspaces/tree` without `depth` on a tree of 10,000 nodes returns
  a document of ~2.6 MB (the server side is ~100 ms, the rest is transfer and parsing on
  the client) — use `depth`; tightening a type's `allowedChildTypes` or
  `fieldSchema` does not retroactively make an existing tree invalid
  (the rule is checked only on mutations, ADR-0029); provenance
  of the effective config is given per top-level section key, not per
  leaf; an inherited nested key can be removed only by overriding the
  container object as a whole (there is no deletion sentinel, ADR-0032); an external
  reference cannot be deleted through the API (only created/metadata updated,
  ADR-0034); `governance` is set only through config revisions, not through
  the profile's `settings`; the Context Adapter remains a single process — per-tenant
  isolation isolates FAILURES, it does not provide parallelism (ADR-0036); the journal archive
  lives in the same database, external object storage is the next stage
  (ADR-0038); retention is an operator command, there is no scheduler; the OpenCode
  adapter is verified in CI by a contract test bench, a live `opencode serve` is not
  part of the image (ADR-0041).

## v0.3: Claude Code and harness clients

```bash
control-plane init --server http://localhost:8000   # .control-plane/config.json
control-plane login --server http://localhost:8000  # key -> Keychain / 0600 file
claude mcp add control-plane -- control-plane-mcp   # MCP server for Claude Code
control-plane work list                             # discovery from the terminal
control-plane-agent                                 # reference daemon (CONTROL_PLANE_*)
```

Aliases and legacy locations of the former codename **were removed in v0.5**
(ADR-0040); when an obsolete environment variable or a
codename directory of the project binding is detected, the tools print an explicit
migration error — see [docs/migration-v0.5.md](docs/migration-v0.5.md).

Protocol: [docs/harness-protocol.md](docs/harness-protocol.md). Integrations:
[Codex](docs/codex.md), [Claude Code](docs/claude-code.md). A working copy per
task and a commit as evidence:
[docs/execution-workspace.md](docs/execution-workspace.md). A task with
`customFields.baseBranch` (a feature branch, TAI-ADR-0047) gets a copy cut from
`origin/<baseBranch>`, and the `targetBranch` of its `commit` artifact names the same
branch; a branch missing in the forge fails the run with a reason instead of falling
back to `main`.

## v0.4: Reliable Event Cursor, Context Memory, White-Label

The main idea of the version: **the Control Plane holds the truth about the work; the Context
Memory Engine holds what was learned along the way; a reliable event stream
connects the present with long-term memory.**

- **Reliable replay** (ADR-0023/0024): delivery by `(tx_id, sequence)` under a
  stable horizon — a committed event cannot be lost
  because of the commit order; the public cursor is an opaque `ec1_...`
  (harness protocol v2), the legacy integer is accepted within the compatibility window.
- **External memory** (ADR-0025/0026): an optional Context Memory Engine behind a
  hard HTTP boundary (`CP_CONTEXT_PROVIDER=none|http`); the Context Adapter
  (`python -m control_plane.worker.context_adapter`) replays the journal into
  Observations with at-least-once delivery and a durable cursor; disabled
  memory affects neither coordination nor readiness.
- **Explicit remember** (ADR-0027): `POST /observations` — a replayable
  `observation.recorded` event with server-side provenance; in Claude Code —
  `cp_remember`.
- **Working context** (ADR-0028): `POST /context` = the authoritative current
  state + a memory ContextPack with an explicit `memoryStatus`, freshness cursors
  and a trace id; in Claude Code — `cp_get_context`. A new session without the previous
  conversation continues the work: the E2E `scripts/e2e_v04.py` proves
  continuity, surviving a memory outage and deduplication on adapter crash.
- **White-label** (ADR-0022): the core and the official clients are neutral;
  the codename remains only in deprecated aliases and legacy fallbacks.

## v0.5: Project Model, Reliability Backlog

The main idea of the version: **the project hierarchy and the organizational hierarchy are the same
tree**, and everything "project-related" is computed from it rather than duplicated in a field.

- **Workspace Types** (ADR-0029): a tenant-scoped registry of node types with
  `fieldSchema` (JSON Schema for custom fields) and `allowedChildTypes`; the rule
  is checked on create/move/retype under the same per-tenant advisory lock.
  The system type `generic` is created automatically, so a v0.4 tree
  migrates without manual repair.
- **Project Templates** (ADR-0030): a template version is immutable from the moment it is
  written (a database trigger); a change is a new version; a project always references
  an exact version.
- **Project Profile and lifecycle** (ADR-0031): `UNIQUE (workspace_id)` is the whole
  story of concurrent creation; user-defined statuses map to five
  system categories (`planned`, `active`, `paused`, `terminal_success`,
  `terminal_cancelled`), and the core makes decisions by category only.
- **Versioned configuration** (ADR-0032): append-only revisions; creating does not
  activate; activation is a separate transactional command with `If-Match`;
  the effective config is composed from the layers template → ancestors → revision →
  profile and is returned together with provenance for every key.
- **Governance** (ADR-0033): a fixed typed vocabulary with a partial
  order "stricter"; a descendant can only tighten, an attempt to loosen is
  rejected before commit; `:move` re-checks the whole subtree and is rejected
  as a whole.
- **External references** (ADR-0034): product-neutral mapping of external
  identifiers, immutable identity, no dual-write.
- **Project scope** (ADR-0035): `projectId` in `/tasks` and `/work/available` with
  two semantics (exact scope and `includeSubprojects`), filtering in
  PostgreSQL before pagination; an archived project stops handing out new work without
  touching live claims and runs.
- **Per-tenant delivery** (ADR-0036): a poison event of one Tenant no longer
  stops the others; cursors, the parked state and backoff are per
  Tenant row, round-robin by service age.
- **Operator redrive** (ADR-0037): `GET /operations/context-adapter`,
  `:redrive`, `:rebuild` — no operation can advance the cursor forward,
  i.e. skip an event; each writes an audit event.
- **Journal retention** (ADR-0038): `event_archive` + floor; `:archive`
  keeps replay transparent, `:prune` turns an old cursor into
  `cursor_below_journal_floor` instead of silently skipping.
- **End-to-end `X-Run-Id`** (ADR-0039): validated, echoed back,
  written into the event and the outbox, passed on to Memory and to logs as `run_id`.
- **OpenCode adapter** (ADR-0041): `control-plane-opencode` — a harness on top of
  the verified HTTP contract of `opencode serve`, continuity through Run
  Checkpoints.
- **Claude Code adapter** (ADR-0016 §1): `control_plane_claude` — an adapter of the
  reference daemon (`CONTROL_PLANE_AGENT_ADAPTER=claude-code`); it runs
  `claude -p` in the task's working copy, continuity through the Run Checkpoint
  `claude-code.session`, MCP is forwarded inside with a curated set of authoritative
  commands. The prompt goes via stdin, the transcript stays on the runner, only the
  summary goes out. Details: [docs/claude-code-adapter.md](docs/claude-code-adapter.md).
- **Executor instructions** (CP-ADR-0066): a task type version carries
  `instructions` (Markdown ≤ 16 KiB, immutable, checked for size and pasted
  secrets), a project carries `settings.agentInstructions`. The core assembles
  them after its platform contract into `instructions: {layers, hash}` of the run
  context and the working context, and pins the hash and layer versions on the
  run and on `run.started`. All three adapters build their prompt with one
  renderer (`control_plane_agent/instructions.py`): harness note, layers, the
  agent conventions — the "Agent conventions" layer: `executor.instructions` of the
  agent revision or the file (`CONTROL_PLANE_CLAUDE_PROMPT_FILE`,
  `CONTROL_PLANE_CODEX_PROMPT_FILE`, `CONTROL_PLANE_OPENCODE_PROMPT_FILE`), the task,
  the memory pack as data. The raw `effectiveConfig` JSON is no longer pasted
  into the prompt. Details: [docs/api.md](docs/api.md#executor-instructions).

E2E: `scripts/e2e_v05.py` (a real Docker bench + a real Memory Service),
benchmarks: `scripts/bench_v05.py`, migration:
[docs/migration-v0.5.md](docs/migration-v0.5.md), measured guarantees and
limitations — [baseline v0.5](docs/reference/control-plane-v0.5-baseline.md).

## v0.6: Human Operator Harness Pilot

- Session returns a server-derived `controlLevel`; a human gets
  `human_operated`, agent/service — `connected`. This is observability, not a right.
- The shared MCP adapter configures harness type/version/client name through the
  environment and takes the client version from the installed package metadata.
- SDK/MCP support create/list/optimistic update of Tasks, relations and
  atomic creation of a child + parent relation.
- `POST /runs/{runId}:handoff` under `Idempotency-Key` writes, in one transaction,
  a handoff checkpoint, suspends the Run, releases the Claim, returns the Task to
  `todo` and publishes events/outbox. The next harness creates a new Claim and
  Run from the server context.
- The operator workflow and the confirmation boundaries are fixed in
  [ADR-0042](docs/adr/0042-human-operator-harness-handoff.md); upgrading — in
  [migration-v0.6](docs/migration-v0.6.md).

## v0.7–v0.9: version map

A brief map of the versions after v0.6; the full register of decisions by version is
[docs/adr/](docs/adr/README.md).

| Version | What is included | Decisions | Upgrade |
|---|---|---|---|
| **v0.7 — Agent Runtime Contracts** | Effective Harness Manifest as immutable evidence of a Run; Durable Active Turn Control as a Run subresource (`run_control_messages`, capability `active_turn_control.v1`); Scoped Tool Discovery (`GET /tools`, `GET /tools/{ref}`, `cp_search_tools`/`cp_describe_tool`) and re-authorization at execution time; Durable Child Run Handle (launch/list/resolve/revoke, capability `child_run_handle.v1`) | [ADR-0043](docs/adr/0043-effective-harness-manifest.md), [ADR-0044](docs/adr/0044-durable-active-turn-control.md), [ADR-0045](docs/adr/0045-scoped-tool-discovery.md), [ADR-0046](docs/adr/0046-durable-child-run-handle.md) | [docs/migration-v0.7.md](docs/migration-v0.7.md): revisions `c91f3c7ad8e2`, `9c41ee0d7b52`, merge `d4e6f8a1b2c3`, `e7c2a95d41b8`, head `a7f2c4d19b60` |
| **v0.8 — work item model** | the `task_types` registry, status as a "key + category" pair and a configurable lifecycle; custom fields, planned dates and extended querying (`/tasks` with filters and `sort=dueDate\|startDate`); work item comments with an append-only revision history; a registry of entity bindings for external references | [ADR-0047](docs/adr/0047-generic-external-references.md), [ADR-0048](docs/adr/0048-work-item-type-and-lifecycle.md), [ADR-0049](docs/adr/0049-work-item-fields-dates-filters.md), [ADR-0050](docs/adr/0050-work-item-comments.md) | [docs/migration-v0.8.md](docs/migration-v0.8.md): `c8a51d70b394` → `a1c7e94b2f60` → `b8d3f1a45c72` |
| **v0.9 — execution observability** | execution trace: the run transcript as a bounded `transcript` artifact and a run action `tool.<name>` for every tool call; automatic code review — the runner daemon creates the task for the reviewer, the verdict from the summary lands in the task's fields | [ADR-0051](docs/adr/0051-execution-trace-transcript-artifact.md), [ADR-0052](docs/adr/0052-auto-review-by-runner-daemon.md) | no schema change, no migration |

Between v0.7 and v0.8 sits the revision `f5b91c3e7a24` (`iam_principal_bindings`,
IAM enforcement — the section below). Later: v0.10 Identity —
[ADR-0053](docs/adr/0053-iam-identity-source-and-binding-api.md), revision
`c2d8e4f6a1b3` (binding status `active|disabled|revoked`, the section "Managing
bindings" below); v0.11 memory and authorization —
[ADR-0054](docs/adr/0054-governed-graph-memory.md) and
[ADR-0055](docs/adr/0055-policy-authorize-and-shadow-mode.md), revision
`a9c4e2d7f1b3` (`iam_actor_id` in the event journal).

Automatic review (v0.9) moves from the daemon to the task type: a type version may
declare work after completion — `completionSchema` (revision `a4c7e2f9b1d3`,
[ADR-0061, amendment 2026-09-25](docs/adr/0061-approval-outcomes-declared-by-task-type.md)).
Core files it when the task is completed by anyone — a runner, a human, an
approval outcome — with the completer's authority: for example, a `code-review`
task with `customFields` (branch, commit) and a gate approval for the reviewer,
if the task has a published `commit` artifact. If the type declares it, the runner
daemon files no review; types without the section keep the ADR-0052 behaviour.

## IAM enforcement (IAM-7)

The Control Plane remains a resource server: it verifies a token issued by
`iam-service`, but does not issue credentials itself and does not store licenses. The decision
order is the same for HTTP, WebSocket and background calls:

```
IAM identity → local revocation → entitlement → domain policy → transactional gates
```

The shared Policy Enforcement Point lives in the separate product-neutral package
`platform-auth-sdk`; only what is specific to the product is described here.

### Where permissions come from

There are no domain permissions in the IAM token, and there must not be any: the right to "create a Task"
belongs to the product, not to the identity provider. An external identity is mapped to a
local Principal through the `iam_principal_bindings` table, and the permissions are taken
from there. The pair `(issuer, iam_principal_id)` is unique — one upstream identity does not
get two local Principals — and `iam_tenant_id` is stored alongside so that
a token of a foreign tenant does not work by a coincidence of subject.

The scope of the presented token acts as a ceiling: `control-plane:read`,
`control-plane:write`, `control-plane:admin`. A binding may allow writes, but
a token issued for reading only will not let you write. The intersection narrows, never
widens; the admin scope adds nothing to the binding — it merely does not narrow it.

The fourth scope, `control-plane:decide`, is a token for one decision taken in a
channel such as Telegram (CP-ADR-0070). Its `purpose_ref=approval:<id>` names the
approval. Such a token can only `POST /approvals/{id}:approve|:reject` that approval,
and only with an `Idempotency-Key`. Any other request gets `403 outside_purpose`, and a
token without `purpose_ref` gets `403 purpose_ref_required`. Of the binding's
permissions it keeps only `approvals.decide`. `acr=channel:<name>` goes into the
`channel` field of the `approval.approved|rejected` event.

The `status`/`revoked_at` columns of a binding are the local revocation policy: it
closes access immediately, without waiting for an already issued access token to expire.
Statuses: `active`, `disabled` (an operator switch) and `revoked`
(revoked through the API); any non-`active` status closes access.

### Managing bindings (ADR-0053)

IAM is the source of identity; the `cp_` API key remains only a credential bootstrap
and an emergency entry when IAM is unavailable. Bindings are created and revoked through
the API, not SQL.

**The first binding — at bootstrap.** The optional `iamBinding` field creates
the row for the admin principal in the same transaction as the tenant and the key — on an
IAM-only installation the administrator signs in without a single line of SQL. The permissions are all
permissions by name (the scope of the first token acts as a ceiling, and a bare `admin`
under `read+write` would narrow to an empty set):

```json
POST /api/v1/bootstrap            Authorization: Bearer <CP_BOOTSTRAP_TOKEN>
{
  "tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin",
  "iamBinding": {
    "issuer": "https://iam.example/iam",
    "iamTenantId": "00000000-…", "iamPrincipalId": "11111111-…"
  }
}
→ 201 { "tenant": …, "adminPrincipal": …, "apiKey": …,
        "iamBinding": { "id": "…", "issuer": "…", "iamPrincipalId": "…",
                        "permissions": [ "admin", "approvals.decide", … ],
                        "status": "active", … } }
```

**From then on — the principals API** (the permissions are the same as for API keys):

| Route | Permission | Semantics |
|---|---|---|
| `GET /api/v1/principals/{id}/iam-bindings` | `principals.read` | all identities of the principal, including revoked ones |
| `POST /api/v1/principals/{id}/iam-bindings` | `principals.write` | upsert by `(issuer, iamPrincipalId)`: `201` created, `200` updated (re-pointed, permissions replaced, status `active` again) |
| `POST /api/v1/iam-bindings/{id}:revoke` | `principals.write` | `status=revoked`; idempotent |

```json
POST /api/v1/principals/{id}/iam-bindings
{
  "issuer": "https://iam.example/iam",
  "iamTenantId": "00000000-…", "iamPrincipalId": "22222222-…",
  "permissions": ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]
}
```

The upsert rules mirror key issuance: an unknown permission, or `admin` from a
non-admin — `422 invalid_permissions`; permissions beyond one's own — `403
permission_escalation` (`details.missing`); a non-active principal — `422
principal_not_active`. On top of that, a principal of kind `agent`/`service` does not
get `admin` and `approvals.decide` — `422 permissions_not_allowed_for_kind`.
An identity bound in another tenant — `409 iam_identity_bound_elsewhere`.
An issuer other than `CP_IAM_ISSUER` (when set) — `422 iam_issuer_untrusted`.
Moving an identity from another principal takes an `admin` (`403
permission_escalation`, `details.previousOwnerKind`). The principal of an active
registry agent takes no identity through this route — `409 agent_identity_conflict`
with `details.route` `/agents/{key}/identity`; only an `admin` may re-bind the
identity the registry already records (CP-ADR-0073, amendment 2026-09-30, I4).

Journal events: `iam_binding.created` / `updated` / `revoked` with
`principalId`, `issuer`, `iamPrincipalId` and the permissions. After commit the router
invalidates the enforcement cache for this identity (`BindingDirectory.invalidate`),
so a new or revoked binding takes effect from the next request — including
on top of a negative answer cached before the row appeared.

SDK: `list_iam_bindings`, `upsert_iam_binding`, `revoke_iam_binding`.
Conformance — `tests/integration/test_iam_bindings.py`.

### Compatibility window

While `CP_LEGACY_API_KEYS_ENABLED=true`, the previous key `cp_<prefix>_<secret>`
remains a working credential. The kind of the presented value is determined by its shape,
not by trying each method in turn: trying them in turn would reveal, through the response code, which one matched.
Turning it off switches the service to IAM-only mode.

### Break-glass key (ADR-0065)

While IAM is down, the host operator mints a short-lived admin key for an active
human from a shell on the host; there is no API for it:

```bash
docker compose exec control-plane-api python -m control_plane.break_glass \
  issue --principal <uuid> --ttl 3600 --reason "IAM is down"
docker compose exec control-plane-api python -m control_plane.break_glass revoke
```

The key (`cp_bg…`) is accepted even with the compatibility window closed, lives at most
`CP_BREAK_GLASS_MAX_TTL_SECONDS` (4 h), and its issue lands in the journal as
`api_key.break_glass_issued` with the reason. `revoke` closes every live break-glass
key once IAM is back. `CP_BREAK_GLASS_ENABLED=false` turns the path off.

### Failures

| Situation | Response |
|---|---|
| Any token defect, an unknown or revoked binding | `401 invalid_credentials` |
| A token without a Control Plane scope | `403 insufficient_scope` |
| An identity without a license | `403 not_entitled` |
| A license without the domain permission | `403 permission_denied` |
| JWKS or entitlement unavailable for longer than the window | `503` |

All token defects collapse into one response, and an unknown binding answers
the same as a revoked one: distinct codes would turn the endpoint into a directory of
other people's credentials. The exact reason goes to the decision journal (`control_plane.authz`)
with a correlation id and without secrets. For WebSocket the same outcomes are returned as close codes
`4401`/`4403`/`4503`.

Unavailability of the external decision is a `503`, not an allow: the permission was not verified, not
confirmed.

### Configuration

```bash
CP_IAM_ENABLED=true
CP_IAM_ISSUER=https://iam.example
CP_IAM_JWKS_URL=https://iam.example/.well-known/jwks.json
CP_IAM_AUDIENCE=control-plane
CP_LEGACY_API_KEYS_ENABLED=true          # compatibility window
CP_BREAK_GLASS_ENABLED=true              # ADR-0065: emergency key from a host shell

CP_ENTITLEMENT_ENABLED=true              # requires the service's own identity
CP_ENTITLEMENT_BASE_URL=https://entitlement.example
CP_IAM_BASE_URL=https://iam.example
CP_IAM_CLIENT_ID=control-plane
CP_IAM_CLIENT_SECRET=...                 # environment only
```

`CP_IAM_ENABLED` turned on without an issuer or JWKS fails startup: a service that has
"almost" moved to IAM is worse than either state. Disabled entitlement is visible in the
decision journal as the source `disabled`, not as a missing record.

The conformance matrix — `tests/integration/test_iam_enforcement.py`.

### Credential of the local harness

Codex, Claude Code and the CLI obtain their credential through `resolve_credential`: if
`CONTROL_PLANE_IAM_URL` is declared, the IAM identity is used, otherwise the previous
`cp_` key. A half-configured IAM (URL without tenant) closes access with an error
rather than silently falling back to the old key.

The Platform Access Token is presented **only** to IAM and only in the request body;
the Control Plane receives a short-lived access token of its own audience. The token lives
for minutes while a harness session lives for hours, so the credential is not resolved once at
startup: it is exchanged again before expiry and once more if the server answered
`401`. The retry goes with the same `Idempotency-Key` — a re-sent request must
remain the same business command — and a second `401` counts as a refusal, not as
a reason to keep trying.

```bash
iam auth login                              # the PAT lands in the credential store
CONTROL_PLANE_IAM_URL=https://iam.example
CONTROL_PLANE_IAM_TENANT=<tenant-uuid>
CONTROL_PLANE_IAM_AUDIENCE=control-plane    # default
CONTROL_PLANE_IAM_SCOPES="control-plane:read control-plane:write"
```

The PAT itself never enters the environment: the client reads it from the same store as
`iam auth` (a declared CI mode, Keychain, a `0600` file). A file with broader
permissions is not read at all — that is an incident, not a configuration inaccuracy.
