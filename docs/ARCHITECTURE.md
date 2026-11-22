# Architecture

A **modular monolith** (ADR-0001): one Python package, one process, clear internal boundaries.
Engines exchange typed Pydantic contracts; `platform.py` is the only composition root.

```
 Interfaces      CLI ─────────┐   HTTP API (/api/v1) ───┐   web UI /app (static, uses the API)  [desktop / mobile: not built]
                              ▼                          ▼
 Authorization   Platform / RepairWorkflow methods → Guard (policy + project membership) → audit
                              │
 Orchestration   ORION: persistent tasks, leases, steps ──► AgentRunner (fixed per-agent allow-lists)
                              │
 Agents          repository · testing · diagnostic · planning · security · recovery · verification · learning
                              │
 Engines         Twin (scanner, graph) · Diagnostics · Recovery · Skills · Living Memory · MOSAIC (models)
                              │
 Governance      Policy · Memberships · Approvals (bound, single-use) · Evidence · Audit (HMAC chain)
                              │
 Execution       Sandbox workspaces: process backend | docker backend (+ per-project images)
                              │
 Storage         SQLite (WAL, migrations)                    [PostgreSQL, sync: not built]
```

## The RACE-X loop as implemented

`scan → baseline (2 runs) → diagnose → propose (+ security review) → simulate → authorize (human) →
execute (checkpoint) → verify → rollback (if failed) → learn`. Each step is persisted; completed steps are
never re-run; interrupted steps reconcile real state. See [RECOVERY_PROTOCOL.md](RECOVERY_PROTOCOL.md).

## Trust boundaries

1. API/CLI callers - authenticated by token, authorized per action and per project.
2. Untrusted project code - only inside a sandbox workspace (copy of the project).
3. Model output - schema-validated, may only propose plans, which go through security review,
   simulation, approval and verification.
4. Agents - trusted platform code; their *use* is restricted and recorded, they are not sandboxed.

## Key decisions

Eleven ADRs in [adr/](adr/), indexed in [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md).
Generated references: [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), [API_REFERENCE.md](API_REFERENCE.md).

## Deliberate differences from the directive's proposed layout

The directive suggests top-level folders (`apps/`, `agents/`, `intelligence/`, ...). The repository uses
one package `src/yemendev/` with one sub-package per engine, and creates folders only when code exists
(directive section 8: "do not generate empty folders"). SQLAlchemy is not used (ADR-0002).
