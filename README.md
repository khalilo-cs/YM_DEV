# YEMENDEV AI

**Global Autonomous Digital Resilience & Evolution Platform** — *Understand. Predict. Protect. Recover. Evolve.*

> Status: **early core (v0.1.0)**. The MVP repair workflow works locally and offline and is covered
> by automated tests. Most of the long-term vision is **not started** — see
> [docs/FEATURE_STATUS.md](docs/FEATURE_STATUS.md) for exactly what is implemented and verified.

## What works today

For a local Python project, YEMENDEV AI can:

1. scan it (files, languages, import graph, declared dependencies) without executing any code;
2. run its test suite twice in an isolated sandbox (a Docker container, or a restricted process: no network, unprivileged);
3. diagnose reproducible failures and record *unverified* hypotheses;
4. propose repairs from a human-authored plan, from validated skills, or (if configured) from a model;
5. simulate each repair in a fresh sandbox and reject any that fixes nothing or breaks a passing test;
6. ask a **different human** for an approval bound to the exact file changes;
7. apply the change with a checkpoint, re-run tests, and **roll back byte-exactly** if verification fails;
8. record hash-chained evidence for every step, learn a candidate skill from verified repairs, and
   resume after a crash at any step.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
export YEMENDEV_DATA_DIR=$PWD/.yemendev
.venv/bin/yemendev init --actor alice              # prints an admin token once
export YEMENDEV_TOKEN=ydv_...                       # alice's token
.venv/bin/yemendev token issue bob --role operator  # a second human, to approve alice's plans (90-day token)
.venv/bin/yemendev project add myproj /abs/path/to/project
.venv/bin/yemendev project grant <project_id> human:bob --role operator   # access is per project
.venv/bin/yemendev task repair <project_id> [--plan plan.json]
.venv/bin/yemendev approvals list
YEMENDEV_TOKEN=<bob's token> .venv/bin/yemendev approvals approve <approval_id>
.venv/bin/yemendev task advance <task_id>
.venv/bin/yemendev serve                            # HTTP API on 127.0.0.1:8765, docs at /api/docs

# Recommended for untrusted projects: run their tests in containers (needs a local Docker daemon)
docker pull python:3.13-slim && .venv/bin/yemendev sandbox build-image
export YEMENDEV_SANDBOX_BACKEND=docker && .venv/bin/yemendev sandbox status
```

Optional model (any OpenAI-compatible server, e.g. llama.cpp / Ollama / vLLM):
`YEMENDEV_MODEL_BASE_URL`, `YEMENDEV_MODEL_NAME`, `YEMENDEV_MODEL_API_KEY_ENV` (name of the env var holding the key).

## Development

```bash
.venv/bin/python -m pytest          # full suite (~1 min)
.venv/bin/ruff check src tests
.venv/bin/mypy                      # strict
```

## Documentation

[Feature status](docs/FEATURE_STATUS.md) · [Audit report](docs/AUDIT_REPORT.md) ·
[Threat model](docs/THREAT_MODEL.md) · [Roadmap](docs/IMPLEMENTATION_ROADMAP.md) ·
[Risk register](docs/RISK_REGISTER.md) · [ADRs](docs/ARCHITECTURE_DECISIONS.md) · [Security](SECURITY.md)

License: Apache-2.0 (declared in `pyproject.toml`).
