# Independent Audit Report — Stage 7

Date: 2026-11-17 · Scope: all code on branch `ccr-5b7e38f8-g2boke` at the end of stage 6 (`68024fc`).
Method: no status claim was trusted. Findings come from attack scripts run against the real
code, inspection of the real SQLite database after a full workflow, a real `uvicorn` server
probed with `curl`, performance measurement, `pip-audit`, a secret scan and lint/type checks.

## Summary

| Severity | Found | Fixed | Open |
|---|---|---|---|
| Critical | 1 | 1 | 0 |
| High | 2 | 2 | 0 |
| Medium | 6 | 5 | 1 (per-project authorization) |
| Low / design limits | 5 | 1 | 4 (documented) |

Test suite after fixes: **136 passed, 0 failed** (`pytest`), `ruff` clean, `mypy --strict` clean,
`pip-audit`: no known vulnerabilities.

## Findings

| ID | Sev | Finding | Evidence (how it was found) | Fix | Regression test |
|---|---|---|---|---|---|
| A-01 | **Critical** | Sandboxed project tests could read/write the platform DB and HMAC key: they ran as the platform's OS user, and workspaces were *inside* the data dir. | Attack script: a malicious `tests/test_evil.py` inserted an **admin token row** into `api_tokens`. | Untrusted code runs as uid 65534 when the platform is root (or a configured `run_as_uid`); workspaces moved to a separate temp root; data dir 0700, DB 0600; `privilege_separation=required` mode refuses to run without it. Re-attacked with the exact absolute DB path: `PermissionError`. | `test_critical_sandboxed_tests_cannot_touch_platform_db`, `test_privilege_separation_applied_and_platform_state_unreachable` |
| A-02 | High | Separation-of-duties bypass: the client chose a plan's `source`; labelling a human plan `skill` let its author approve it. | Attack script: alice approved her own patch. | Plans submitted with a task are always `HUMAN`, authored by the task creator. | `test_f1_client_cannot_relabel_plan_to_self_approve` |
| A-03 | High | Project root replaced by a symlink after registration was followed by scanner, sandbox copy and **patch writes**. | Attack script: scan of a swapped root listed `yemendev.sqlite3`. | Root re-validated on every use (`Platform.project_root`). | `test_f4_root_swapped_for_symlink_is_refused` |
| A-04 | Medium | Request-size limit bypass with `Transfer-Encoding: chunked` (memory exhaustion). | `curl` 6 MB chunked body → 422 (fully parsed). | ASGI middleware counts real body bytes → 413 (verified with `curl` against real server). | `test_regression_chunked_oversized_body_rejected` |
| A-05 | Medium | Approvals stayed `approved` after the task was cancelled. | Attack script. | Cancel revokes; digests also bind task + project. | `test_f5_cancel_revokes_task_approvals` |
| A-06 | Medium | Platform DB world-readable (0644). | `stat` in attack script. | 0700 dir / 0600 files. | `test_f3_platform_state_permissions` |
| A-07 | Medium | Evidence deletion undetectable (hash only detects edits). | Code review. | Every evidence record anchored in the HMAC audit chain; `GET /api/v1/integrity/verify`. | `test_evidence_deletion_and_tampering_detected` |
| A-08 | Medium | Forged skill reference made a task `FAILED` *after* its change was applied and verified (status lies). | Attack script. | Learn step records the bad ref instead of failing (and A-02 removes the vector). | covered by A-02 test |
| A-09 | Medium | Graph metrics quadratic (one BFS per node): 0.68 s @2k modules, ~17 s projected @10k. | `perf.py` measurement. | SCC condensation + bitsets: 0.02 s @2k, 0.17 s @10k. Checked equal to BFS on 60 random graphs. | `test_regression_fast_transitive_counts_match_bfs` |
| A-10 | Low | Unreachable workspace parent gave an opaque "pytest produced no report". | Found while fixing A-01. | Explicit precondition with a clear error. | `test_unreachable_workspace_parent_fails_clearly` |
| A-11 | Medium (open) | Authorization is global per role; no per-project scoping. A viewer sees all projects' evidence. | Review. | **Not fixed** - needs a workspace/membership model. | - |
| A-12 | Low (open) | A malicious project controls its own test results (can forge a passing JUnit report). | `test_project_that_forges_a_passing_report_is_not_trusted` documents it. | Inherent: verification = "the project's own suite says it passes". Stated in every verify evidence record. | documents behaviour |
| A-13 | Low (open) | Without root or `run_as_uid`, A-01 protection is unavailable (best-effort mode records it; `required` refuses). | Design. | `GET /system/status` reports `privilege_separation`. Container backend planned. | `test_privilege_separation_required_refuses_when_unavailable` |
| A-14 | Low (open) | No API rate limiting, no token expiry, no TLS (loopback-only by default; `serve` refuses public bind without `--allow-remote`). | Review. | Documented. | - |
| A-15 | Low (open) | Audit chain tail truncation detectable only against an externally recorded head hash. | `test_detects_truncation_against_recorded_head`. | `--expected-head` option; export head regularly. | existing |

## Defects found earlier by tests during stages 1-6 (also fixed, with regression tests)

* Redaction destroyed test counts (`passed` matched `pass(word)?`) — stage 1.
* Relative import beyond top-level package resolved to a wrong module — stage 2.
* `preexec_fn` used in a multi-threaded server process (deadlock risk) — stage 3.
* Every unauthenticated API request would have returned 500 instead of 401 (found by mypy) — stage 6.
* `from __future__ import annotations` broke FastAPI dependency resolution (auth skipped → 422) — stage 6.

## What was verified, and how

| Claim | Verification |
|---|---|
| Sandbox blocks network | test opens a TCP connection from inside → blocked; test **ran** (not skipped) |
| Sandboxed code cannot reach platform state (as root host) | attack with exact DB path → `PermissionError`; regression tests |
| Timeout kills grandchildren | grandchild that writes a marker after 4 s never writes it |
| Crash resume | 9 fault points; manual probe confirmed a genuinely half-written project on disk was reconciled |
| Real API server | `yemendev serve` + `curl`: health, 401, 200, 413 on chunked body, no tracebacks |
| DB integrity after workflow | `PRAGMA integrity_check` ok, 0 FK violations, no secrets / tokens in any column |

## Not verified (honest limits)

* Any real language model (none available). MOSAIC is tested only against a protocol stub.
* Behaviour on non-root hosts with privilege separation (cannot be exercised in this container).
* Real power loss (crashes are injected exceptions; SQLite durability relies on WAL + `synchronous=FULL`).
* Multi-user or remote deployment.
