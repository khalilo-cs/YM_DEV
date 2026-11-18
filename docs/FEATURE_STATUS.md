# Feature Status

Last reviewed: 2026-11-17 (stage 7 independent audit). Statuses:
**NOT STARTED · IMPLEMENTED** (code exists, no/insufficient tests) **· TESTED** (automated tests pass)
**· VERIFIED** (exercised end-to-end in a real workflow, with evidence) **· EXPERIMENTAL · BLOCKED**.

"VERIFIED (local)" = verified in this Linux container, platform running as root, Python 3.13.

## Core platform

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| SQLite storage, atomic migrations, WAL, FKs | TESTED | `storage/` | `tests/unit/test_storage.py`; DB inspected after workflow (integrity ok, 0 FK violations) | Single node |
| HMAC-chained audit log | TESTED | `security/audit.py` | `tests/security/test_audit.py` | Tail truncation needs external head (A-15) |
| Evidence records anchored in audit chain; integrity cross-check | TESTED | `evidence/store.py`, `Platform.verify_integrity` | `test_evidence_deletion_and_tampering_detected` | - |
| Secret redaction | TESTED | `security/redaction.py` | `tests/unit/test_redaction.py` | Pattern-based |
| API tokens (hashed, revocable) | TESTED | `security/tokens.py` | `tests/integration/test_api.py` | No expiry (A-14) |
| Policy engine, roles, agent restrictions | TESTED | `governance/policy.py` | `tests/security/test_governance.py` | Global per role - **no per-project scoping (A-11)** |
| Bound, single-use, expiring, revocable approvals; no self-approval | VERIFIED (local) | `governance/approvals.py` | governance + E2E + audit regression tests | - |
| Living memory (provenance, contradiction, staleness, history) | TESTED | `memory/` | `tests/unit/test_memory.py` | Keyword relevance only |

## Project intelligence

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| Scanner (files, languages, Python AST imports, declared deps, secret-file detection, symlink safety) | TESTED | `twin/scanner.py` | `tests/unit/test_scanner.py` | Python imports only; other languages counted, not parsed |
| Dependency graph (impact, cycles, critical modules), O(V+E) metrics | TESTED | `twin/graph.py` | incl. equivalence with BFS on 60 random graphs; 10k modules in 0.17 s | Static imports, not runtime causation |
| Scan → memory with file-hash validity | TESTED | `twin/memory_sync.py` | `test_scan_recorded_in_memory_goes_stale_on_change` | - |

## Execution, diagnosis, repair

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| Sandbox: copy-on-run, rlimits, process-group kill, netns, scrubbed env, **privilege drop to uid 65534** | VERIFIED (local) | `sandbox/workspace.py` | `tests/security/test_sandbox.py` (network & privilege tests ran, not skipped); attack scripts | Not a VM/container; no syscall filter; no privilege drop on non-root hosts unless `run_as_uid` set (A-13) |
| pytest runner (JUnit parsing, honest statuses, hostile XML rejected) | TESTED | `sandbox/testrunner.py` | `tests/integration/test_testrunner_diagnostics.py` | Project can forge its own report (A-12) |
| Diagnostics (reproducible vs flaky, locations, unverified hypotheses) | TESTED | `diagnostics/engine.py` | same | Rule-based |
| Repair plans, checkpoints, atomic writes, byte-exact verified rollback | VERIFIED (local) | `recovery/` | `tests/integration/test_recovery.py`, E2E | Full-content patches only |
| Skill engine (lifecycle, validation, regression, field outcomes) | TESTED | `learning/skills.py` | `tests/unit/test_skills.py` | Only `token_rename` kind |
| Experience → candidate skill extraction | VERIFIED (local) | `learning/experience.py` | E2E: learned skill fixed a second project | Single consistent identifier renames only |
| MOSAIC OpenAI-compatible adapter, schema-validated output, model proposer | TESTED (protocol stub only) | `mosaic/`, `recovery/model_proposer.py` | `tests/integration/test_mosaic.py` | **Requires a model** - no real model was available; quality of model repairs unverified |

## Orchestration and interfaces

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| ORION persistent tasks, leases, single active worker | TESTED | `orion/tasks.py` | crash tests, `test_concurrent_advance_only_one_worker_runs` | Time-based lease (15 min) |
| RACE-X repair workflow | VERIFIED (local) | `orion/workflow.py` | `tests/end_to_end/test_mvp_workflow.py` | Synchronous per `advance` call; no background scheduler |
| Crash resume (9 fault points, half-written project) | VERIFIED (local) | `orion/workflow.py` | `test_crash_at_any_point_then_resume` + manual probe | Injected exceptions, not real power loss |
| HTTP API `/api/v1` | VERIFIED (local) | `api/app.py` | `tests/integration/test_api.py`; real `uvicorn` + `curl` | No TLS, no rate limiting |
| CLI | TESTED | `cli/main.py` | `tests/integration/test_cli.py` | - |

## Research & benchmarking

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| Resilience benchmark harness (15 scenarios incl. 2 compound failures, seeded, JSON reports) | TESTED | `benchmarks/` | `tests/regression/test_benchmark.py`; full run 15/15; mutation check 3/3 caught | Not comparative; synthetic fixtures; see BENCHMARK_METHODOLOGY |

## Not started

Predictive risk engine · anomaly detection · counterfactual/compound failure simulation (beyond
fault points) · digital immune system · continuity passport · synchronization queue · continuity
debt metric · comparative benchmark baselines · web/desktop/mobile UI · i18n (Arabic/English) · PostgreSQL ·
OpenTelemetry · Tree-sitter/LSP · container sandbox backend · per-project authorization.

## MVP acceptance (directive §20)

All 18 criteria demonstrated by `tests/end_to_end/test_mvp_workflow.py` (see AUDIT_REPORT for
limits). #8 "propose a repair" is verified for human-authored and skill-based plans; model-based
plans require a model.
