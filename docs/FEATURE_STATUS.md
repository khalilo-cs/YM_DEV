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
| Everything else | NOT STARTED | - | - | - |
