# AGENTS.md

Instructions for coding agents working on EVARuntime. This file applies to the
whole repository unless a more specific `AGENTS.md` is added in a subdirectory.

## Project Mission

EVARuntime is an OpenAI-compatible inference gateway for private GPU
infrastructure. It is built to keep prompts, model execution, access control,
usage logs, and operational controls inside the host organization.

The codebase favors pragmatic operations over a heavy platform stack:
FastAPI, SQLite WAL, systemd, nginx, and `llama.cpp` managed as gateway-owned
subprocesses.

## Documentation and planning source of truth

- `ROADMAP.md` is the canonical public roadmap, current status, known
  limitations, target architecture, and prioritized work. Read it first for a
  new feature, review, or audit.
- `docs/vision.md` is the concise product vision. `VISION.md` is only a
  historical entry point that links to it; do not maintain a second copy.
- `docs/architecture.md`, `docs/api.md`, `docs/deployment.md`, and
  `docs/observability.md` describe behavior already delivered.
- `codex-analyse.md` is a historical implementation log. Use it only when
  reconstructing an old change; its counters and statuses are not authoritative.
- Use GitHub Issues for scoped, actionable work with acceptance criteria. Keep
  broad initiatives as epics and use checklists or linked issues for their
  current slice. Use a discussion/RFC for an unresolved architectural choice,
  then record the final decision in an ADR before implementing it.

Do not duplicate the same live status in multiple documents. When behavior
changes, update the relevant technical document and the roadmap milestone in
the same change. Keep security and correctness defects explicit even when they
are not yet scheduled; do not hide them merely to reduce the visible issue
count.

## Repository Map

- `gateway/`: main OpenAI-compatible gateway.
- `gateway/cluster/`: multi-node orchestration, node scheduling, node client,
  and shared protocol models.
- `gateway/readiness.py`: structural readiness checks. Consumed by `/ready` and
  by `doctor`. Reuse it, do not reimplement its checks.
- `gateway/doctor.py`: host preflight (secrets, runtime, GPU, models, ports,
  nginx, TLS, systemd). Human and JSON output, stable exit codes.
- `gateway/deploy/`: systemd, nginx, installer, update script, and example node
  configuration.
- `gateway/deploy/smoke_test.sh`: first-token recipe. Gates `update.sh` and is
  runnable standalone during an incident.
- `gateway/deploy-macos/`: launchd/Homebrew mirror of `deploy/`. `deploy/` is
  the reference tree: parity is enforced by
  `gateway/tests/test_deploy_trees_parity.py` (issue #29).
- `gateway/static/dashboard.html`: admin dashboard served by the main gateway.
- `gateway/tests/`: main gateway unit tests.
- `node_agent/`: lightweight FastAPI agent that loads/unloads `llama-server` on a
  remote GPU node.
- `docs/`: main gateway architecture, API, admin, deployment, vision, decisions,
  and research notes.
- `ROADMAP.md`: canonical project roadmap and consolidated audit findings.

## Core Architecture

The main gateway exposes OpenAI-compatible routes and owns the model lifecycle.
Requests are authenticated, rate-limited, routed by `model`, and proxied to the
right `llama-server` backend. In local mode, backends are subprocesses launched
by `gateway/server_manager.py`. In cluster mode, `gateway/model_manager.py`
selects `ClusterManager`, which delegates model placement and lifecycle calls to
remote `node_agent` services.

The model registry is the source of truth for available models. Treat
`models.yaml` and `MODELS_CONFIG_PATH` as operational configuration, not casual
test data. Model IDs, file paths, VRAM estimates, capabilities, and per-model
`llama.cpp` parameters are validated by `gateway/model_registry.py`.

## Development Commands

Use Python 3.11 or newer.

Main gateway:

```bash
cd gateway
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock -r ../requirements-dev.lock
python -m pytest tests -v
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Node agent smoke setup:

```bash
cd node_agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
uvicorn main:app --host 127.0.0.1 --port 9443 --reload
```

Do not run `gateway/tests` and `node_agent/tests` in one pytest process unless
you have verified import isolation. Both components intentionally use top-level
module names such as `config`, `database`, `auth`, and `schemas`.

### CI parity

The `.venv/` directories already exist and have `ruff` installed. Do not
recreate them; call them directly, e.g. `gateway/.venv/bin/python -m pytest tests -q`.

CI runs `ruff check .` per component **in addition to** pytest. Run it before you
call a change done, otherwise the failure only surfaces after the PR is opened.

CI runs on **Python 3.11** from the checked-in hashed lockfiles, while the local
venvs may be on 3.14 and retain older packages. To reproduce the CI environment:

```bash
uv venv --python 3.11 /tmp/ci
uv pip install --python /tmp/ci/bin/python --require-hashes \
  -r gateway/requirements.lock -r requirements-dev.lock
cd gateway && /tmp/ci/bin/python -m pytest tests -q
```

## Coding Style

- Match the existing Python style: typed functions where useful, direct module
  functions/classes, and small FastAPI handlers that delegate to focused modules.
- Keep async code async end to end. Do not introduce blocking subprocess, network,
  or file operations into request paths without pushing them out of the hot path.
- Keep comments concise and useful. The existing code uses French comments and
  operational docstrings; follow that style when editing nearby code.
- Prefer standard-library and existing dependency solutions. Avoid adding new
  dependencies unless they remove real operational risk or substantial complexity.
- Preserve OpenAI-compatible response shapes and error bodies on `/v1/*` routes.
- Keep deployment files boring and explicit. systemd/nginx scripts are production
  artifacts, not generated scratch files.
- A list-typed setting loaded from the environment must be annotated
  `str | list[str]` and normalized by a `mode="before"` validator
  (`split_list_setting`). Annotated as a bare `list[str]`, pydantic-settings
  decodes it as JSON **in the source, before any validator runs**: the documented
  comma-separated syntax — and even an empty value — fail startup with
  `SettingsError`, and the validator never executes. This shipped broken for
  `ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS` and `ALLOWED_MODELS`.

## Security Rules

- Never commit real `.env` files, secrets, API keys, TLS keys, certificates,
  databases, logs, or generated runtime state.
- Keep only `.env.example` or `env.example` templates in Git.
- User API keys are stored as SHA-256 hashes. Do not add code paths that log,
  persist, echo, or return raw keys after creation.
- Admin routes must remain protected by `ADMIN_SECRET` and should not leak model
  file paths, local infrastructure details, or usage data to regular users.
- Do not weaken path validation for GGUF or multimodal projector files. Keep
  `ALLOWED_MODEL_DIRS` constraints meaningful.
- Never log a `username`, an email, or the free-text `notes` field: log the
  technical `id` instead. Request paths are redacted by `main._redact_path()`
  for the same reason. GDPR anonymization is void if the logs keep a copy.
- `DELETE /admin/users/{username}` **anonymizes** (decision DEC-001): the `users`
  row is kept, personal data is erased, the account is disabled and every key is
  revoked. Usage history is retained under a pseudonym so billing and audit stay
  consistent. The verb and path are unchanged on purpose, for operator scripts.
- Treat SSRF, prompt-content leakage, key leakage, and GPU denial of service as
  primary threat models.

## Model Lifecycle Invariants

- A model handling an active request must not be evicted. Respect the
  `pin()`/`unpin()` pattern in `ServerManager` and the `finally` blocks around it.
- Streaming requests must keep the model pinned until the async generator exits,
  including client disconnect and upstream error cases.
- Capacity handling is based on both VRAM budget and port availability. When
  changing scheduling or eviction logic, test queue-full, timeout, active-request,
  and oversized-model cases.
- VRAM estimates come from registry entries. If `ctx_size`, `parallel`, KV cache,
  quantization, or multimodal settings change, update `vram_gb` and docs together.
- Cluster mode should keep the same public behavior as local mode. Prefer changes
  behind the shared `model_manager` interface.

## Schema Migrations

The SQLite schema is versioned by `PRAGMA user_version` in `gateway/database.py`.
A `CREATE TABLE IF NOT EXISTS` never reaches an existing database, so any schema
change goes through a migration.

- Append a `Migration` to the **end** of the `MIGRATIONS` tuple. Never rewrite a
  migration that has shipped.
- `statements` is a tuple of individual statements, not a script:
  `executescript()` would commit the open transaction and break atomicity. Use
  the `apply` Python hook for anything SQL alone cannot express.
- Changing a constraint requires `_rebuild_table()` (create/copy/drop/rename).
  `PRAGMA foreign_keys` cannot be changed inside a transaction — the engine
  toggles it around the whole migration series.
- A `*.pre-migration.*.bak` backup is produced before any migration that
  actually applies. These files are gitignored and subject to the bounded
  retention policy in `gateway/database.py` (see OPS-002).
- Details: `docs/architecture.md`, section « Migrations versionnées ».

## Testing Expectations

Run the test suite for the component you touch:

- `gateway`: `cd gateway && python -m pytest tests -v`
- `node_agent`: `cd node_agent && python -m pytest tests -v`

Add or update tests when changing:

- authentication, hashing, quotas, or rate limits;
- request normalization and allowed fields;
- streaming behavior or SSE chunk rewriting;
- model registry validation;
- VRAM capacity queue, LRU eviction, or pin/unpin behavior;
- cluster node scheduling, node protocol validation, or node client errors;
- admin model mutation APIs that persist to YAML.

For deployment-only changes, validate scripts/configs by inspection and, where
reasonable, with syntax checks. Do not run installers against the host unless the
user explicitly asks for it. Deployment logic can still be tested for real: drive
the script against a fake local HTTP server, as `test_smoke_test_script.py` does.

Deploy scripts exist twice (`gateway/deploy/` for Linux/systemd,
`gateway/deploy-macos/` for launchd). `gateway/deploy/` is the reference: when a
fix touches a common file, replicate it on the other side or declare the
divergence with a justification in `test_deploy_trees_parity.py` — CI fails on
undeclared drift.

Two rules that caught real defects:

- Never enumerate `app.routes` directly. Since FastAPI 0.141, `include_router()`
  stores an `_IncludedRouter` there and the included routes are only reachable
  through `original_router`. Walk it recursively, or you silently see none of
  `/admin/*`.
- A test that asserts an **absence** (no `/admin/*` route, no secret in an
  output) must carry a positive control proving it can see anything at all.
  Without one it goes inert without ever failing.

## Documentation Expectations

Keep docs synchronized with behavior. Important docs:

- `ROADMAP.md`: public status, milestones, known limitations, and prioritized
  epics. Keep the public backlog curated; do not turn every future idea into an
  issue immediately.
- `docs/vision.md`: concise product thesis and differentiation.
- `README.md`: public overview and repository-level quick start.
- `docs/architecture.md`: main gateway design and invariants.
- `docs/api.md`: user-facing OpenAI-compatible API behavior.
- `docs/admin.md`: admin CLI/API operations.
- `docs/deployment.md`: production deployment and maintenance.

If you change an endpoint, an environment variable, a default limit, model
registry semantics, or operational behavior, update the relevant documentation in
the same change.

## Git and Workspace Hygiene

- Work with the user's existing changes. Do not revert files you did not modify.
- Avoid broad refactors while fixing narrow behavior.
- Do not delete generated or runtime files with destructive commands unless the
  user asked for that cleanup.
- Keep generated caches out of commits: `__pycache__/`, `.pytest_cache/`,
  `.ruff_cache/`, `.mypy_cache/`, logs, databases, and local virtualenvs.
- Before finishing, check `git status --short` and report the files changed.

## Useful Reading Order

When you need context, start here:

0. `ROADMAP.md` — canonical status, current risks, milestones, and target
   architecture.
1. `README.md`
2. `docs/architecture.md`
3. `docs/api.md` or `docs/deployment.md`, depending on the task
4. `gateway/main.py`
5. `gateway/proxy.py`
6. `gateway/model_manager.py`
7. `gateway/model_registry.py`
8. `gateway/cluster/*` for cluster behavior
9. `node_agent/main.py`
10. `codex-analyse.md` only when historical implementation context is needed;
    it is not the current tracker.
