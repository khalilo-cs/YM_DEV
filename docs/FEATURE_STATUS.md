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
| Skill-based repair proposer | IMPLEMENTED | `recovery/proposers.py` | exercised in stage 5 E2E | Never patches test files |
| Everything else | NOT STARTED | - | - | - |
