# Feature Status

Statuses: NOT STARTED · IN PROGRESS · IMPLEMENTED · TESTED · VERIFIED · BLOCKED · EXPERIMENTAL.
TESTED = automated tests pass. VERIFIED = exercised end-to-end in a real workflow with evidence.

| Feature | Status | Location | Tests | Known limitations |
|---|---|---|---|---|
| SQLite storage + migrations | TESTED | `storage/` | `tests/unit/test_storage.py` | Single-node only |
| Tamper-evident audit log | TESTED | `security/audit.py` | `tests/security/test_audit.py` | Tail truncation needs external head hash |
| Secret redaction | TESTED | `security/redaction.py` | `tests/unit/test_redaction.py` | Pattern-based; cannot catch arbitrary secrets |
| Policy engine + roles | TESTED | `governance/policy.py` | `tests/security/test_governance.py` | Static policy table |
| Bound single-use approvals | TESTED | `governance/approvals.py` | `tests/security/test_governance.py` | - |
| Evidence records | TESTED | `evidence/store.py` | `tests/unit/test_evidence.py` | Hash detects edits, not deletion |
| Living memory (provenance, contradiction, staleness) | TESTED | `memory/` | `tests/unit/test_memory.py` | Relevance is keyword overlap, not semantic |
| API tokens | IMPLEMENTED | `security/tokens.py` | none yet | - |
| Project scanner (files, languages, AST imports, declared deps, secret-file detection) | TESTED | `twin/scanner.py` | `tests/unit/test_scanner.py` | Python-only import analysis; other languages counted, not parsed |
| Dependency graph (impact, Tarjan cycles, critical modules) | TESTED | `twin/graph.py` | `tests/unit/test_scanner.py` | Static imports only (not runtime); report is O(N·E) |
| Scan -> memory with file-hash validity | TESTED | `twin/memory_sync.py` | `tests/unit/test_scanner.py` | - |
| Sandbox workspace (copy-on-run, rlimits, pgroup kill, netns, scrubbed env) | TESTED | `sandbox/workspace.py` | `tests/security/test_sandbox.py` (network test ran, not skipped) | Process-level only; can read host files readable by the OS user (RISK-001) |
| pytest runner with JUnit parsing | TESTED | `sandbox/testrunner.py` | `tests/integration/test_testrunner_diagnostics.py` | A malicious project can forge its own report (documented by a test); verification proves only that the project's own suite reports passing |
| Diagnostics (reproducibility, locations, unverified hypotheses) | TESTED | `diagnostics/engine.py` | `tests/integration/test_testrunner_diagnostics.py` | Rule-based; pytest output parsing only |
| Repair plans (validated paths, original-hash preconditions, operation digest) | TESTED | `recovery/model.py` | `tests/integration/test_recovery.py` | Full-content patches only |
| Checkpoints + atomic writes + verified byte-exact rollback | TESTED | `recovery/checkpoint.py` | `tests/integration/test_recovery.py` | Local filesystem only |
| Recovery lifecycle mechanics (prepare/simulate/execute/verify/rollback, crash-state reconcile) | TESTED | `recovery/engine.py` | `tests/integration/test_recovery.py` | Authorization + evidence wiring is stage 5 |
| Skill engine (lifecycle, validation, regression, field outcomes, one active version) | TESTED | `learning/skills.py` | `tests/unit/test_skills.py` | Only `token_rename` skill kind exists |
| Experience -> candidate skill extraction | TESTED | `learning/experience.py` | `tests/unit/test_skills.py` | Only single consistent identifier renames are extracted |
| Skill-based repair proposer | TESTED | `recovery/proposers.py` | `tests/end_to_end/test_mvp_workflow.py` (skill reuse on 2nd project) | Never patches test files |
| ORION persistent tasks, leases, step persistence | TESTED | `orion/tasks.py` | `tests/end_to_end/` | Lease expiry is time-based (15 min default) |
| RACE-X repair workflow (scan→baseline→diagnose→propose→simulate→authorize→execute→verify→rollback/learn) | VERIFIED (local) | `orion/workflow.py` | `tests/end_to_end/test_mvp_workflow.py` | Runs synchronously per `advance` call |
| Crash resume at 9 fault points incl. half-written project | VERIFIED (local) | `orion/workflow.py`, `recovery/engine.py` | `test_crash_at_any_point_then_resume` | Fault injection simulates crashes in-process, not real power loss |
| Skill activation via bound approval | TESTED | `platform.py` | `test_mvp_end_to_end_with_learning_and_skill_reuse` | - |
| MOSAIC model adapter (OpenAI-compatible) + schema-validated output | IMPLEMENTED | `mosaic/adapters.py`, `recovery/model_proposer.py` | **no tests yet** | **Requires a model**; none available here |
| Everything else | NOT STARTED | - | - | - |

## MVP acceptance (directive section 20)

Evidence: `tests/end_to_end/test_mvp_workflow.py`, run 2026-11-17, 16/16 passing.

| # | Criterion | Status | Test |
|---|---|---|---|
| 1-2 | Accept & inspect a local Python project | VERIFIED | `test_mvp_end_to_end...` |
| 3-4 | Dependencies + dependency representation | TESTED | `tests/unit/test_scanner.py` |
| 5 | Run tests safely (sandbox) | VERIFIED | all E2E tests |
| 6 | Identify a reproducible problem (2 runs) | VERIFIED | `test_mvp_end_to_end...` |
| 7 | Evidence-backed diagnostic report | VERIFIED | `diagnostics.report` evidence |
| 8 | Propose at least one repair | VERIFIED | human plan; skill plan on 2nd project. Model plans: requires a model |
| 9 | Execute an approved repair | VERIFIED (in place, with checkpoint) | `test_mvp_end_to_end...` |
| 10 | Run verification tests | VERIFIED | `repair.verify` evidence |
| 11-12 | Detect failed repair + roll back | VERIFIED | `test_failed_verification_rolls_back_exactly` |
| 13 | Preserve execution evidence | VERIFIED | hash-valid evidence per step |
| 14 | Save a candidate experience | VERIFIED | memory EXPERIENCE + PROPOSED skill |
| 15 | Validate a skill before activating | VERIFIED | activation needs validation + bound approval |
| 16 | Persist state locally | VERIFIED | SQLite; fresh `Platform` instances resume |
| 17 | Resume an interrupted task | VERIFIED | 9 fault points |
| 18 | Work without internet | VERIFIED for the platform process (sockets blocked) + sandbox netns | `no_network` fixture |
