# Risk Register

| ID | Risk | Likelihood | Impact | Mitigation | Status |
|---|---|---|---|---|---|
| RISK-001 | Sandbox escape / host file reads by untrusted project tests (process-level isolation only) | Medium | High | Copy-on-run workspace, rlimits, netns, scrubbed env; plan container/VM backend | Open |
| RISK-002 | Model-generated patches are wrong or malicious | High | High | Model output treated as untrusted; simulate in sandbox; human approval; verify; rollback | Mitigated by design |
| RISK-003 | Audit key + DB both compromised -> forged history | Low | High | Key outside DB, 0600; export head hash externally | Open |
| RISK-004 | Crash mid-repair leaves project half-modified | Medium | High | Checkpoint before write, atomic writes, resumable reconcile step | Mitigated (tested in stage 5) |
| RISK-005 | Stale memory treated as truth | Medium | Medium | Validity conditions + revalidation; stale excluded from queries | Mitigated |
| RISK-006 | Over-/under-redaction of secrets | Medium | Medium | Token-based key matching + value patterns; regression tests | Partially mitigated |
| RISK-007 | Target project deps missing from sandbox interpreter -> false "failures" | High | Medium | Configurable interpreter; diagnostics distinguish collection errors | Open |
| RISK-008 | No local model available -> no generative repair | Certain (here) | Medium | Deterministic skill-based repair path; model adapter optional | Accepted |
