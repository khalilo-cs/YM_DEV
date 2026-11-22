# Development Guide

## Layout

```
src/yemendev/
  core/          contracts (ids, errors, hashing), config, fault injection
  storage/       SQLite access + append-only migrations
  security/      audit chain, redaction, keys, tokens, rate limiting
  governance/    actors/roles, policy, memberships, approvals, guard
  evidence/      evidence records (anchored in the audit chain)
  memory/        Living Memory
  twin/          scanner, dependency graph, scan -> memory
  sandbox/       workspaces, process + docker backends, images, pytest runner
  diagnostics/   reproducibility + unverified hypotheses
  recovery/      plans, checkpoints, prepare/simulate/execute/verify/rollback, proposers
  learning/      skills + experience extraction
  agents/        agent registry, runner, security agent
  mosaic/        model adapters (OpenAI-compatible) and structured output validation
  orion/         persistent tasks + the RACE-X repair workflow
  api/  cli/     HTTP API and command line (same service layer)
  benchmarks/    resilience benchmark
  platform.py    composition root - the only module that wires engines together
apps/web/        web UI (React + TypeScript + Vite): src/pages/*, i18n.ts (en + ar), ui.tsx (shared states)
```

## Conventions

* Pydantic models for every contract crossing a module boundary; `mypy --strict` must pass.
* Errors: raise a subclass of `YemenDevError`; its `message` must be safe to show to API clients.
* Every write path to platform state goes through a `Database.transaction()` (`BEGIN IMMEDIATE`).
* Authorization: API/CLI call `Platform`/`RepairWorkflow` methods, which call `Guard.require(...)` with a
  `project_id` for project resources. Never call low-level services (e.g. `ApprovalService.decide`)
  from an interface.
* Workflow steps run through `AgentRunner` with the step's agent; a new step must be added to
  `STEP_AGENTS` with the minimal actions it needs.
* Schema changes: append a migration in `storage/migrations.py` (never edit a released one), then run
  `python scripts/gen_reference_docs.py`.
* UI: every string goes in `apps/web/src/i18n.ts` (English and Arabic); use logical CSS properties
  (`margin-inline-start`, not `margin-left`); show only API data; wrap sensitive actions in
  `ConfirmButton`; never load anything from another origin.
* Crash safety: a workflow step must be idempotent or reconcile real state first (see `_execute`).
  Add a fault point (`faults.check("...")`) for new crash-sensitive code and a resume test.

## Useful commands

```bash
.venv/bin/yemendev --data-dir /tmp/ydv serve          # API on 127.0.0.1:8765, docs at /api/docs
.venv/bin/python scripts/gen_reference_docs.py        # regenerate DATABASE_SCHEMA.md / API_REFERENCE.md
.venv/bin/python benchmarks/mutation_check.py         # check the benchmark still detects sabotage
python3 docs/proposal/build.py                        # regenerate the Arabic proposal PDF
(cd apps/web && npm ci && npm run build)              # build the web UI served at /app
.venv/bin/python scripts/ui_e2e.py --shots /tmp/shots # real-browser end-to-end check of the UI
```
