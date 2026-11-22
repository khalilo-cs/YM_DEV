# Recovery Protocol

Directive lifecycle `PREPARE → SIMULATE → AUTHORIZE → EXECUTE → VERIFY → COMMIT`, failure path
`FAILURE → ROLLBACK → DIAGNOSE → PRESERVE EVIDENCE`. Code: `recovery/engine.py`, `orion/workflow.py`.

| Phase | What happens | Evidence |
|---|---|---|
| Baseline | Tests run twice in fresh sandboxes; reproducible failures only | `tests.baseline` |
| Diagnose | Failures, locations, unverified hypotheses | `diagnostics.report` |
| Propose | Human plan, active skills, optional model; each reviewed by the security agent | `repair.propose`, `security.plan_review`, `agent.conflict` |
| **Prepare** | Paths validated; no test files; every file still has the planned original hash | (part of simulate/execute) |
| **Simulate** | Patch applied to a sandbox copy; tests compared with baseline | `repair.simulate` |
| **Authorize** | Bound approval requested from a human (≠ plan author); consumed atomically | `repair.authorize` |
| **Execute** | Preconditions re-checked (TOCTOU); checkpoint of originals; atomic writes | `repair.execute` |
| **Verify** | Patched files match approved content; tests re-run in a fresh sandbox | `repair.verify` |
| **Commit** | Task `succeeded`; experience + candidate skill recorded | `learn` step |
| **Rollback** | Originals restored from checkpoint and hash-verified; task `rolled_back` | `repair.rollback` |

## Crash safety

Each step is persisted. On resume, `execute` inspects the files on disk: all original → apply;
all new → done; partial → restore checkpoint then re-apply; **diverged** (someone else changed a file)
→ stop as `failed` and touch nothing. Verified at 9 injected crash points and in the compound
"crash + human edit" benchmark scenario.

## What "recovered" does and does not mean

Recovery means the project's own tests pass where they failed, with no regressions. It does **not**
prove data integrity of external systems, nor correctness beyond what the tests check; a malicious
project can forge its own test results.
