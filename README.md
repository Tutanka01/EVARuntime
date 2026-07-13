# EVARuntime

> OpenAI-compatible LLM inference gateway for private GPU infrastructure.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![llama.cpp](https://img.shields.io/badge/llama.cpp-CUDA-orange.svg)](https://github.com/ggml-org/llama.cpp)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20compatible-green.svg)](https://platform.openai.com/docs/api-reference)

EVARuntime turns one or more NVIDIA GPU servers into a controlled, auditable and energy-aware LLM inference platform. It exposes a familiar OpenAI-compatible API while keeping model execution, access control and usage logs inside your own infrastructure.

Developed by **Mohamad El Akhal** within the **Université de Pau et des Pays de l'Adour (UPPA)**.

---

## What It Solves

Many research teams, universities and internal platforms need LLM access without sending data to a third-party API. EVARuntime focuses on that operational problem:

| Need | EVARuntime approach |
| --- | --- |
| Keep prompts and responses on-premise | Models run on your GPU nodes; clients call your gateway |
| Reuse existing tooling | OpenAI-compatible `/v1/chat/completions`, streaming included |
| Share a costly GPU safely | API keys, rate limits, quotas and request logs |
| Avoid idle GPU waste | Models are loaded on demand and unloaded after inactivity |
| Operate several models | VRAM budget, model registry, LRU eviction and optional cluster mode |

The project is intentionally pragmatic: FastAPI, SQLite WAL, systemd, nginx and `llama.cpp` instead of a heavy control plane.

## Core Features

- OpenAI-compatible chat completions API, including Server-Sent Events streaming.
- Per-user API keys stored as SHA-256 hashes.
- Sliding-window rate limiting and monthly token quotas.
- SQLite WAL storage with per-connection robustness pragmas and a manual retention purge.
- On-demand `llama-server` lifecycle management with a shared keep-alive HTTP client on the hot path.
- Full GPU memory release when models are idle, plus a bounded drain of active requests on shutdown.
- Multi-model VRAM budgeting with LRU eviction and best-effort VRAM reconciliation via `nvidia-smi`.
- Optional multi-node mode with lightweight GPU agents, state reconciliation on startup and fast failover.
- Supply-chain hardening: optional GGUF `sha256` integrity checks and `llama-server` build pinning.
- Observability: Prometheus text exposition (`/admin/metrics/prometheus`) and a `/ready` readiness probe.
- Admin CLI and REST API for users, keys, models and reports.
- Deployment assets for systemd, nginx, journald and scheduled SQLite backups.

## Architecture

```text
Client
  |
  v
nginx / TLS / optional network filtering
  |
  v
FastAPI Gateway
  |-- Auth, rate limits, quotas
  |-- Model manager
  |-- SQLite WAL
  |
  +-- llama-server process, model A
  +-- llama-server process, model B
  +-- remote node agents, optional cluster mode
```

Each model backend is managed as a gateway-owned subprocess instead of a permanently running service. That design lets EVARuntime terminate idle backends and return GPU memory to the host, which is useful when the same machine is shared between inference, experimentation and training workloads.

## Repository Layout

```text
gateway/                 Main OpenAI-compatible gateway
gateway/cluster/         Multi-node scheduling and remote node client
gateway/deploy/          systemd, nginx and install scripts
gateway/static/          Admin dashboard
gateway/tests/           Gateway test suite
gateway-student/         Restricted student-facing edge gateway
node_agent/              Lightweight remote GPU node agent
docs/                    Architecture, API, admin and deployment guides
```

## Quick Start

Prerequisites:

- Local mode: a Linux server with NVIDIA GPU, CUDA `llama.cpp` and local GGUFs.
- Cluster mode: a Linux orchestrator (GPU optional) plus separately installed
  GPU node-agents with CUDA `llama.cpp` and identical model paths.
- Python 3.11+.

EVARuntime has two explicit deployment paths. The single-node product remains
the safe default; the multi-node control plane is an optional second product:

| Path | Gateway host | GPU execution | Command |
| --- | --- | --- | --- |
| Local (default) | Gateway + `llama-server` | On the gateway host | `install.sh --mode local` |
| Cluster (opt-in) | Orchestrator only | On separately installed node-agents | `install.sh --mode cluster` |

Install the local single-node gateway:

```bash
git clone https://github.com/Tutanka01/EVARuntime.git
cd EVARuntime
sudo bash gateway/deploy/install.sh --mode local
```

Omitting `--mode` on a fresh install is equivalent to `--mode local`. For a
multi-node orchestrator, inspect the plan first, then install explicitly:

```bash
bash gateway/deploy/install.sh --mode cluster --dry-run
sudo bash gateway/deploy/install.sh --mode cluster
```

The installer never replaces an existing `env`, `models.yaml`, `nodes.yaml` or
secret. A local↔cluster migration requires both an explicit target mode and
`--allow-mode-change`. Updates preserve the installed mode when `--mode` is
omitted:

```bash
sudo bash gateway/deploy/update.sh                 # auto-detect and preserve
sudo bash gateway/deploy/update.sh --mode cluster  # explicit cluster update
```

Cluster agents are installed and updated on each GPU node; the orchestrator
does not update them remotely. See the [deployment guide](docs/deployment.md)
for TLS, ports, shared model paths, migration and rollback.

## API Example

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key="llmgw-your_api_key",
)

response = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Explain Bayes' theorem."}],
)

print(response.choices[0].message.content)
```

Streaming works with standard OpenAI clients:

```python
stream = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Write a short introduction to LLMs."}],
    stream=True,
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

## Administration

```bash
cd /opt/llm-gateway

sudo -u llmservice ./venv/bin/python cli.py add-user alice \
  --email alice@univ-pau.fr --rpm 30

sudo -u llmservice ./venv/bin/python cli.py create-key alice --name research
sudo -u llmservice ./venv/bin/python cli.py list-users
sudo -u llmservice ./venv/bin/python cli.py usage-report --month 2026-06 --summary
sudo -u llmservice ./venv/bin/python cli.py status
```

Admin HTTP routes are protected by `Authorization: Bearer <ADMIN_SECRET>` and should be restricted at the reverse proxy layer.

## Configuration

Start with [gateway/.env.example](gateway/.env.example). The important settings are:

| Setting | Purpose |
| --- | --- |
| `MODELS_CONFIG_PATH` | YAML model registry |
| `LLAMA_SERVER_BIN` | Path to the CUDA-enabled `llama-server` binary |
| `INTERNAL_API_KEY` | Internal gateway-to-backend key |
| `ADMIN_SECRET` | Secret for admin endpoints |
| `IDLE_TIMEOUT_SECONDS` | Idle delay before unloading a model |
| `TOTAL_VRAM_GB` | Total GPU VRAM used to compute the model manager budget |
| `CLUSTER_MODE` | `local` or `cluster` |

Never publish real `.env` files, generated secrets, TLS private keys, databases or logs.

## Documentation

- [Architecture](docs/architecture.md)
- [API guide](docs/api.md)
- [Admin guide](docs/admin.md)
- [Deployment guide](docs/deployment.md)
- [Observability guide](docs/observability.md)
- [llama.cpp build notes](docs/build-llama-cpp-dgx-spark.md)

## Public Release Checklist

Before making a fork or repository public:

- Keep only `.env.example` files, never production `.env` files.
- Replace organization-specific domains, addresses and certificates with placeholders.
- Remove generated caches such as `__pycache__`, `.pytest_cache` and `.pyc`.
- Rotate any secret that may have appeared in a local file or terminal output.
- Review deployment documentation for infrastructure details that should remain private.

## Author

EVARuntime was designed and implemented by **Mohamad El Akhal** within the **Université de Pau et des Pays de l'Adour (UPPA)**.
