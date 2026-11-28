# Changelog

## 0.1.0 — 2026-11-17 (unreleased)

### Added
- Stage 22: JavaScript/TypeScript test execution and repair with `node:test` (TypeScript without a build step) on both sandbox backends: per-test results, file load errors with their real cause and location, JS diagnosis rules, mixed Python+JS projects, project `node_modules` copied into the sandbox (never installed), `RLIMIT_DATA` for Node, `yemendev sandbox build-image --with-node`, `YEMENDEV_NODE`. Fixed: correct repairs of test files that failed to load were rejected (A-20); co-located JS/TS tests were not protected from edits (A-21). ADR-0018.
- Stage 21: web UI completed for every area of directive section 16 - project tabs for vulnerabilities, SBOM (download), memory (category and stale filters), task history and checkpoints; security centre with the advisory database state, platform SBOM and platform dependency check; continuity centre with recovery readiness and backup create/verify; research & benchmark centre showing stored reports and comparisons as stored; background queue view; dashboard resource usage and readiness. `/system/status` gains `resources` (database/checkpoint/backup sizes, free disk, server peak memory, load average). Browser E2E 56/56.
- Stage 20: benchmark completed - 9 new scenarios (skill transfer, 6-change long horizon, signed sync with a hash-recomputing attacker, tamper detection, knowledge staleness, 256 MB sandbox memory limit, legacy project, Arabic project, unauthorized operations) and the directive §15 metrics (`benchmark_version` 0.2, fixtures 3); comparative benchmark `yemendev bench --compare` with arms A-E (B runs only with a configured model and is otherwise reported as not run); reports saved to `<data dir>/benchmarks/` by default and served by `GET /api/v1/research/benchmarks[/{source}/{name}]`; reports record `git_dirty`; benchmark mutation check extended to 8 mutants.
- Stage 19: verified backups (`yemendev backup create|list|verify|restore`, `/api/v1/continuity/backups`),
  recovery readiness (`yemendev readiness`, `/api/v1/continuity/readiness`), checkpoint listing
  (`/api/v1/projects/{id}/checkpoints`); migration 7; policy action `backups.manage`.
- Stage 18e: one-command installer `scripts/install.sh` (idempotent, dry-run, token file 0600, sandbox
  reachability warning); `pytest` is now a runtime dependency (the process sandbox runs project tests with
  the platform's interpreter); the process sandbox probes once whether the sandbox user can run the
  interpreter with pytest and explains how to fix it instead of failing as "pytest produced no report";
  verified on Python 3.11.
- Stage 18d: observability - JSON logs with request/trace correlation and redaction, Prometheus metrics
  (`GET /api/v1/metrics`), OpenTelemetry spans for requests, background runs and workflow steps (joined
  across the run process), `X-Request-ID`, W3C `traceparent`; optional `otel` extra; new runtime
  dependency `opentelemetry-api` (already required by FastAPI).
- Stage 18c: offline dependency vulnerability scanning against imported OSV exports (`security/vulns.py`,
  migration 6, `yemendev advisories import|status`, `project vulns`, `vulns`, `GET /projects/{id}/vulnerabilities`,
  `GET /system/vulnerabilities`, `GET /advisories/status`, immune rule `vulnerable-dependency`); new runtime
  dependency `packaging`.
- Stage 18b: secret scanning of file contents (`security/secret_scan.py`, immune rule `secret-in-source`)
  and CycloneDX 1.5 SBOMs for projects and the platform (`GET /projects/{id}/sbom`, `GET /system/sbom`,
  `yemendev project sbom`, `yemendev sbom`).
- Stage 18a: JavaScript/TypeScript understanding with Tree-sitter (`twin/js_scanner.py`): imports of all
  forms, resolution incl. tsconfig paths, npm dependencies from `package.json`, findings, npm-aware
  dependency risk; new runtime dependencies `tree-sitter`, `tree-sitter-javascript`,
  `tree-sitter-typescript`; graph membership made O(1). `apps/web` declares Playwright as an optional
  peer dependency (found by the new analysis).
- Stage 17: ORION queue and background workers - priority/delay scheduling, inter-task dependencies,
  one run per project + global limit, one supervised process per run with wall-clock timeouts,
  retries with exponential backoff, cancellation of running tasks (rollback after execute), fencing,
  claim recovery, CPU/memory accounting, execution trace (`/tasks/{id}/trace`), migration 5; API
  `POST /tasks` with `queue`, `POST /tasks/{id}/enqueue`, `GET /queue`; CLI `task repair --queue`,
  `task enqueue|trace`, `queue`, `worker`, `serve --workers`; task page with queue, plan and timeline;
  ADR-0014.
- Stage 16: web UI under `/app` (React + TypeScript, `apps/web`), Arabic-first with RTL and English;
  every value from the API; confirmation dialogs for sensitive actions; strict CSP, bundled font, no CDN;
  `YEMENDEV_WEB_DIR`; browser end-to-end script `scripts/ui_e2e.py`; ADR-0013.
- Stage 1: SQLite storage + migrations, HMAC audit log, policy engine, bound approvals, evidence, living memory.
- Stage 2: project scanner, dependency graph, scan-to-memory with staleness tracking.
- Stage 3: sandboxed execution, pytest/JUnit runner, diagnostics.
- Stage 4: repair plans, checkpoints, verified rollback, skill engine, experience extraction.
- Stage 5: ORION persistent repair workflow, crash resume, MOSAIC model adapter.
- Stage 6: HTTP API `/api/v1`, CLI.
- Stage 7: integrity cross-check endpoint `/api/v1/integrity/verify`.
- Stage 8: resilience benchmark harness (`yemendev bench`), compound-failure scenarios, mutation check.
- Stage 9: per-project memberships (`/projects/{id}/members`, `yemendev project grant|revoke|members`), token expiry (`--ttl-days`), API rate limiting and failed-auth lockout; DB migration 2.

- Stage 10: Docker container sandbox backend (`YEMENDEV_SANDBOX_BACKEND=docker|auto`), offline-first
  `yemendev sandbox build-image`, `yemendev sandbox status`, `yemendev bench --backend`.

- Stage 11: per-project sandbox images (`yemendev sandbox build-image --project`,
  `POST /api/v1/projects/{id}/sandbox-image`); isolation report with warnings.

- Agents as real components (registry, AgentRunner, `agent_runs` table / migration 3, security agent,
  conflict policy SEC-BLOCK-WINS); API `/agents`, `/agents/{name}/runs`, `/tasks/{id}/agent-runs`.
- Generated `DATABASE_SCHEMA.md` / `API_REFERENCE.md` with a drift test; all 24 documents of directive
  section 18, `CONTRIBUTING.md`, `.env.example`.

- Stage 15: Ed25519-signed digital continuity passports and offline sync bundles (`yemendev continuity ...`,
  `/api/v1/synchronization/status|jobs`, `/projects/{id}/passport`, `/projects/{id}/sync/...`); new
  dependency `cryptography`; DB migration 4 (`sync_jobs`, `sync_conflicts`).
- Stage 14: counterfactual future engine (`POST /projects/{id}/counterfactual`, `yemendev project whatif`):
  sandboxed fault injection with per-test impact, blind spots and static-prediction precision/recall.
- Stage 13: digital immune system (`GET /projects/{id}/immune`, `yemendev project immune`) and the
  EXPERIMENTAL continuity-debt metric (`GET /projects/{id}/continuity-debt`, `yemendev project debt`).
- Stage 12: predictive risk engine (`GET /projects/{id}/risks`, `yemendev project risks`) and causal
  analysis (`GET /projects/{id}/impact`, root-cause candidates recorded by the diagnostic agent).

### Changed (stage 11)
- Default sandbox backend is now `auto` (docker when usable, else process with recorded warnings).

### Security (stage 10)
- CRITICAL (A-16): process-backend runs whose identity can reach a container-runtime socket are refused.

### Changed (breaking)
- Non-admin users need a project membership to see or act on a project. Audit endpoints are admin-only.

### Security (stage 7 audit, see docs/AUDIT_REPORT.md)
- CRITICAL: sandboxed project code could write the platform DB — fixed via privilege separation.
- Self-approval bypass via client-chosen plan provenance — fixed.
- Symlink swap of project root after registration — fixed.
- Chunked request body bypassed size limit — fixed.
- Approvals not revoked on task cancel; DB world-readable; evidence deletion undetectable — fixed.

### Performance
- Graph metrics from O(V·(V+E)) to ~O(V+E) bitset propagation.
