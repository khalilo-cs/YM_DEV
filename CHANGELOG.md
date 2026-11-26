# Changelog

## 0.1.0 — 2026-11-17 (unreleased)

### Added
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
