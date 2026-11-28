# YEMENDEV AI

**Global Autonomous Digital Resilience & Evolution Platform** — *Understand. Predict. Protect. Recover. Evolve.*

> Status: **v0.1.0, pre-release**. Everything listed below runs locally and offline and is covered by automated
> tests; what is *not* done (e.g. a real-model evaluation, a mobile app, an external audit) is listed honestly in
> [docs/FEATURE_STATUS.md](docs/FEATURE_STATUS.md).

## What works today

For a local Python or JavaScript/TypeScript project, YEMENDEV AI can:

1. scan it (files, languages, import graph, declared dependencies, secrets, SBOM, known vulnerabilities from an
   imported OSV database) without executing any code;
2. run its test suite (pytest, `node:test`) twice in an isolated sandbox (a Docker container, or a restricted
   process: no network, unprivileged);
3. diagnose reproducible failures and record *unverified* hypotheses;
4. propose repairs from a human-authored plan, from validated skills, or (if configured) from a model - with
   prompt-injection defences and a static security review of every plan;
5. simulate each repair in a fresh sandbox and reject any that fixes nothing or breaks a passing test;
6. ask a **different human** for an approval bound to the exact file changes;
7. apply the change with a checkpoint, re-run tests, and **roll back byte-exactly** if verification fails;
8. record hash-chained evidence for every step, learn a candidate skill from verified repairs, resume after a
   crash at any step, run work in background workers, and make verified backups.

Clients: CLI, HTTP API, an Arabic/English web UI (`/app`) and a Linux desktop app (`apps/desktop`, Tauri).
A resilience benchmark (24 scenarios) and an A-E comparison are in [docs/BENCHMARK_METHODOLOGY.md](docs/BENCHMARK_METHODOLOGY.md).

## Quick start

```bash
scripts/install.sh --actor alice --token-file ~/.alice.token   # one command: venv, deps, UI, data dir, admin token
export YEMENDEV_DATA_DIR=$PWD/.yemendev
export YEMENDEV_TOKEN=$(cat ~/.alice.token)         # alice's token
.venv/bin/yemendev token issue bob --role operator  # a second human, to approve alice's plans (90-day token)
.venv/bin/yemendev project add myproj /abs/path/to/project
.venv/bin/yemendev project grant <project_id> human:bob --role operator   # access is per project
.venv/bin/yemendev task repair <project_id> [--plan plan.json]
.venv/bin/yemendev approvals list
YEMENDEV_TOKEN=<bob's token> .venv/bin/yemendev approvals approve <approval_id>
.venv/bin/yemendev task advance <task_id>
.venv/bin/yemendev serve                            # HTTP API on 127.0.0.1:8765, docs at /api/docs

# Containers are used automatically once the sandbox image exists (needs a local Docker daemon)
docker pull python:3.13-slim && .venv/bin/yemendev sandbox build-image
.venv/bin/yemendev sandbox status                          # effective backend + warnings
.venv/bin/yemendev sandbox build-image --project <id>      # image with the project's dependencies (admin)
```

Optional model (any OpenAI-compatible server, e.g. llama.cpp / Ollama / vLLM):
`YEMENDEV_MODEL_BASE_URL`, `YEMENDEV_MODEL_NAME`, `YEMENDEV_MODEL_API_KEY_ENV` (name of the env var holding the key).

## Development

```bash
.venv/bin/python -m pytest          # full suite (~4 min; Docker tests run when a daemon is available)
.venv/bin/ruff check src tests
.venv/bin/mypy                      # strict
```

## Documentation

[Overview](docs/PROJECT_OVERVIEW.md) · [Architecture](docs/ARCHITECTURE.md) · [Installation](docs/INSTALLATION.md) ·
[Feature status](docs/FEATURE_STATUS.md) · [Audit report](docs/AUDIT_REPORT.md) · [API](docs/API_REFERENCE.md) ·
[Threat model](docs/THREAT_MODEL.md) · [Roadmap](docs/IMPLEMENTATION_ROADMAP.md) ·
[Risk register](docs/RISK_REGISTER.md) · [ADRs](docs/ARCHITECTURE_DECISIONS.md) · [Security](SECURITY.md) ·
[Desktop app](docs/DESKTOP.md) · [Benchmarks](docs/BENCHMARK_METHODOLOGY.md)

License: **pending the owner's decision** (`pyproject.toml` declares Apache-2.0 provisionally; no `LICENSE` file yet).
