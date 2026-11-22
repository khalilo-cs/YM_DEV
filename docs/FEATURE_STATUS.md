# Feature Status

Last reviewed: 2026-11-21 (stage 11). Statuses:
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
| API tokens (hashed, revocable, mandatory expiry) | TESTED | `security/tokens.py` | `tests/security/test_authorization.py` incl. legacy-token migration | - |
| Policy engine, roles, agent restrictions | TESTED | `governance/policy.py` | `tests/security/test_governance.py` | Static policy table |
| Per-project authorization (memberships, token-role ceiling, scoped lists) | VERIFIED (local) | `governance/membership.py`, `governance/guard.py`, `platform.py` | `tests/security/test_authorization.py` (every API surface), CLI test; mutation check (scoping disabled → 3 tests fail) | Only global admins manage members |
| API rate limiting + failed-auth lockout | VERIFIED (local) | `security/ratelimit.py`, `api/app.py` | unit + API tests; real `uvicorn` + `curl` | Per process; client IP = proxy IP behind a reverse proxy |
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
| Sandbox `process` backend: copy-on-run, rlimits, process-group kill, netns, scrubbed env, uid drop, runtime-socket refusal | VERIFIED (local) | `sandbox/workspace.py` | `tests/security/test_sandbox.py`, `test_runtime_socket_exposure.py`; attack scripts | Host world-readable files/processes visible; uid drop needs root or `run_as_uid` |
| Sandbox `docker` backend (ADR-0009) | VERIFIED (local) | `sandbox/docker_backend.py` | `tests/unit/test_docker_backend.py` (no daemon), `tests/security/test_docker_sandbox.py` (real containers, ran); benchmark 15/15 on docker; comparative attacks | Verified with Docker 29 / cgroup v1 / runc in this container only; shares host kernel; projects with third-party deps need a custom image |
| Offline-first sandbox image build | VERIFIED (local) | `sandbox/image.py`, `yemendev sandbox build-image` | built here with `--network none` | Base image must already be present locally |
| Per-project sandbox images (wheels only, dependency-hash tags, evidence of wheel hashes) | VERIFIED (local) | `sandbox/project_image.py`, `Platform.build_project_image` | `tests/integration/test_project_image.py` (real build with `six`: fails without image, passes with it; reuse; stale-image avoidance; admin-only) | Packages without a wheel unsupported; unpinned deps resolve at build time |
| Agents: registry with fixed allow-lists, AgentRunner, `agent_runs` (ADR-0011) | VERIFIED (local) | `agents/registry.py`, `agents/runner.py` | `tests/security/test_agents.py`; mutation: removing the allow-list fails a test | Agents are trusted in-process code (use is restricted and recorded, not sandboxed) |
| Security agent + conflict policy SEC-BLOCK-WINS | TESTED | `agents/security_agent.py`, `orion/workflow.py` | `tests/security/test_agents.py`; mutation: ignoring a block fails a test | Static pattern review of added lines; evadable by obfuscation |
| Predictive risk engine: test-failure predictions, robust anomaly detection, module change risk, dependency risk, repair reliability | TESTED | `risk/` | `tests/unit/test_risk.py`, `tests/integration/test_risk_engine.py` | Explainable statistics over recorded history; probabilities are smoothed frequencies (uncalibrated); returns *insufficient evidence* instead of predicting below minimum data (4 runs, 6 points, 3 scans, 3 repairs) |
| Causal analysis: failure propagation paths, root-cause candidates (observed/inferred), single points of failure | TESTED | `twin/causal.py`, `diagnostics.root_causes` evidence | same | Static imports + traceback locations; candidates are never reported as proven causes; no fault trees |
| Digital immune system: drift vs baseline scan, platform-authorized vs unrecorded changes, auto-executing files (conftest/.pth/sitecustomize), build/CI changes, binaries, secret files, typosquatting, new deps, platform integrity, repeated-denial correlation | TESTED | `immune/` | `tests/unit/test_immune_rules.py`, `tests/integration/test_immune_and_debt.py`; mutation: 2/2 caught | Every finding is `operational` or `security` (anomalies are not called attacks); typosquat list is small and offline; read-only, no runtime monitoring |
| Continuity debt (EXPERIMENTAL) | EXPERIMENTAL | `immune/continuity_debt.py` | `tests/integration/test_immune_and_debt.py` | **Not a validated metric**; unmeasurable components (backups) are excluded, never guessed |
| Counterfactual future engine: module unavailable, dependency removed, file corrupted, compound faults; per-test impact, blind-spot detection (remembered), static-prediction precision/recall | VERIFIED (local) | `simulation/counterfactual.py`, `Platform.counterfactual` | `tests/integration/test_counterfactual.py`; manual run on the docker backend | Impact is observed only through the project's own tests; no timing/load faults; static scoring only for pure module scenarios |
| Generated reference docs + drift test | TESTED | `scripts/gen_reference_docs.py` | `tests/regression/test_docs_in_sync.py` (fails on manual edits) | Covers schema and API only |
| `auto` default backend + isolation warnings | TESTED | `docker_backend.isolation_report`, `yemendev sandbox status`, `/system/status` | `tests/unit/test_isolation_report.py`; benchmark 15/15 on `auto` | - |
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
| HTTP API `/api/v1` | VERIFIED (local) | `api/app.py` | `tests/integration/test_api.py`; real `uvicorn` + `curl` | No TLS (loopback by default) |
| CLI | TESTED | `cli/main.py` | `tests/integration/test_cli.py` | - |

## Research & benchmarking

| Feature | Status | Location | Tests / evidence | Known limitations |
|---|---|---|---|---|
| Resilience benchmark harness (15 scenarios incl. 2 compound failures, seeded, JSON reports, `--backend`) | TESTED | `benchmarks/` | `tests/regression/test_benchmark.py`; full run 15/15 on **both** backends; mutation check 3/3 caught | Not comparative; synthetic fixtures; see BENCHMARK_METHODOLOGY |

## Not started

Continuity passport · synchronization queue ·
comparative benchmark baselines · web/desktop/mobile UI · i18n (Arabic/English) · PostgreSQL ·
OpenTelemetry · Tree-sitter/LSP · VM-level (gVisor/Firecracker) sandboxing.

## MVP acceptance (directive §20)

All 18 criteria demonstrated by `tests/end_to_end/test_mvp_workflow.py` (see AUDIT_REPORT for
limits). #8 "propose a repair" is verified for human-authored and skill-based plans; model-based
plans require a model.
