# Contributing to Taimen Control Plane

Thank you for taking the time to contribute. Taimen is an organizational
runtime in which people, AI agents, workflows and services execute the work
of an organization; the platform is developed in the open under the
Apache License 2.0. This repository holds the Control Plane — the
coordination service (tasks, claims with lease and fencing, runs, artifacts,
approvals, the organization model and the event journal), its Python SDK
`control-plane-client`, the `control-plane` CLI, the MCP server for human
harnesses and the reference runner daemon `control-plane-agent`.

## Before you start

- Read the platform overview in the
  [guide](https://github.com/taimen-ai/taimen/tree/main/guide/docs/overview)
  (in Russian). The Control Plane keeps its own series of
  decisions in [`docs/adr/`](docs/adr/README.md) (numbered `0001…`, prefix
  `CP-` in the platform-wide index). ADRs are written in Russian with an
  English title line; English summaries are provided on request in the
  ADR's discussion.
- Check the open issues before starting a large change. For anything that changes
  an API, a data model or a service boundary, open an issue first and propose
  an ADR in `docs/adr/`.

## Contributor License Agreement

We require a signed Contributor License Agreement (CLA) for every
contribution, so that the project can be relicensed or defended without
tracking down every author. The CLA is checked by cla-assistant on each pull
request; you sign once for all Taimen repositories.

- Individuals: [`cla/CLA-individual.md`](https://github.com/taimen-ai/taimen/blob/main/cla/CLA-individual.md)
- Companies contributing on behalf of employees: [`cla/CLA-entity.md`](https://github.com/taimen-ai/taimen/blob/main/cla/CLA-entity.md)

The CLA grants the project a copyright and patent licence to your
contribution; you keep your copyright.

## Development setup

Requirements: Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), Docker with
Docker Compose (PostgreSQL 16 is the only supported database).

The Control Plane depends on the enforcement SDK `platform-auth-sdk` by path
(`../../sdk/platform-auth-sdk`, see `[tool.uv.sources]` in `pyproject.toml`), so
either work from a checkout of the umbrella repository or clone both in its
layout — `services/control-plane` and `sdk/platform-auth-sdk` (TAI-ADR-0064):

```bash
git clone https://github.com/taimen-ai/platform-auth-sdk.git sdk/platform-auth-sdk
git clone https://github.com/taimen-ai/control-plane.git services/control-plane
cd services/control-plane
uv sync                     # runtime deps + the `dev` group (pytest, ruff, mypy)
```

`uv sync` also installs `control-plane-client` from `client/` in editable
mode; there are no optional extras.

Tests need a PostgreSQL. The compose file ships an ephemeral one on port 5434:

```bash
docker compose --profile test up -d db-test   # or: make test-db-up
uv run pytest                                 # all suites under tests/ (unit, integration, contract, concurrency, e2e, client)
make test                                     # starts db-test (without CP_TEST_DATABASE_URL), runs pytest
```

To use another database, set `CP_TEST_DATABASE_URL` (this is what CI does);
`make test` then skips the compose database. Pass pytest arguments with
`make test PYTEST_ARGS="tests/unit -x"`.

- Each pytest process runs in its own database, created next to the one in
  `CP_TEST_DATABASE_URL` and dropped at the end of the session (leftovers of a
  killed run are dropped by a later run after 6 hours), so parallel runs do
  not block each other. The role needs `CREATEDB`.
- Every test has a 120 s timeout (`pytest-timeout`); a legitimately long test
  raises it with `@pytest.mark.timeout(<seconds>)`.
- `make test` holds `flock` on `.pytest.lock`: a second `make test` in the
  same working copy exits at once with a "tests already running" message
  (exit code 75).
  Automated runners (see the repository conventions for agents in
  [`AGENTS.md`](AGENTS.md) and `.agents/runner.yaml`) should run tests through
  `make test`.

Lint and type checks (the same commands run in CI):

```bash
uv run ruff check . && uv run ruff format --check .   # make lint
uv run mypy                                           # make typecheck
make fmt                                              # ruff --fix + format
make check                                            # lint + typecheck + test
```

Running the service locally:

```bash
docker compose up -d db                  # PostgreSQL on localhost:5433
uv run alembic upgrade head              # migrations
CP_BOOTSTRAP_TOKEN=dev-token uv run uvicorn control_plane.main:app --port 8000
uv run python -m control_plane.worker    # in a second terminal
```

Schema changes come with an Alembic migration
(`uv run alembic revision --autogenerate -m "..."` against a running
database); `/health/ready` returns 503 when the database revision is behind
the code. The Docker image is built from the root of the umbrella layout, because the
SDK must be at `../../sdk/platform-auth-sdk`:
`docker build -f services/control-plane/Dockerfile -t control-plane ../..`.

The SDK in `client/` is a separate distribution (`control-plane-client`,
depends on `httpx` only) and is versioned in step with the server; keep the
two `pyproject.toml` versions equal when you bump one.

## Pull requests

- One logical change per pull request; keep the history linear (rebase, no
  merge commits).
- Tests, `ruff check` / `ruff format --check` and `mypy` must pass;
  behaviour changes come with tests.
- Commit messages explain *why*, not *what*; reference the ADR or issue.
- Public API changes (routes, schemas, MCP tools, CLI commands, env
  variables) update `docs/api.md` and the related documents in `docs/`, and,
  when they break compatibility, `docs/migration-vX.Y.md`.
- When opening a pull request, confirm that you have signed the CLA and that
  the change contains no secrets, customer data or internal hostnames.

## Reporting bugs and security issues

Bugs: open an issue in this repository with the version, steps to reproduce
and logs. Security issues: see [SECURITY.md](SECURITY.md) and do not open a
public issue.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
