# Changelog

## 0.1.0 — 2026-11-17 (unreleased)

### Added
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
