# Independent Audit Report — Stage 7

Date: 2026-11-17 · Scope: all code on branch `ccr-5b7e38f8-g2boke` at the end of stage 6 (`68024fc`).
Method: no status claim was trusted. Findings come from attack scripts run against the real
code, inspection of the real SQLite database after a full workflow, a real `uvicorn` server
probed with `curl`, performance measurement, `pip-audit`, a secret scan and lint/type checks.

## Summary

| Severity | Found | Fixed | Open |
|---|---|---|---|
| Critical | 2 (A-01, A-16) | 2 | 0 |
| High | 2 | 2 | 0 |
| Medium | 6 | 6 (A-11 in stage 9) | 0 |
| Low / design limits | 5 | 2 (A-14 partially: TLS still absent) | 3 (documented) |

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
| A-11 | Medium | Authorization was global per role; no per-project scoping. | Review. | **Fixed in stage 9** (ADR-0007): memberships, token-role ceiling, scoped lists/decisions, admin-only audit. | `tests/security/test_authorization.py` |
| A-12 | Low (open) | A malicious project controls its own test results (can forge a passing JUnit report). | `test_project_that_forges_a_passing_report_is_not_trusted` documents it. | Inherent: verification = "the project's own suite says it passes". Stated in every verify evidence record. | documents behaviour |
| A-13 | Low | Without root or `run_as_uid`, A-01 protection was unavailable. | Design. | **Stage 10:** Docker backend gives separation on any host (ADR-0009); `required` mode accepts it. | `test_container_backend_satisfies_required_privilege_separation` |
| A-16 | **Critical** | Without a uid drop (non-root host, or `auto` falling back to process), sandboxed code reached `/var/run/docker.sock` and got `HTTP 200` from the Docker API (root-equivalent). Network namespaces do not block Unix sockets. | Comparative attack script, stage 10. | Process backend refuses when the run identity can reach docker/containerd/podman/CRI-O sockets (incl. rootless, `DOCKER_HOST`) unless explicitly allowed; exposure then recorded in evidence. Docker backend never mounts sockets. | `tests/security/test_runtime_socket_exposure.py` (real socket); mutation: 3 fail without fix |
| A-17 | Low (test defect) | Benchmark drift scenario depended on a host file the sandbox could write; under Docker the drift never happened → false 14/15 with a misleading note. | Running the benchmark on the docker backend. | Scenario redesigned (crash + external change during outage), backend-independent. | `test_regression_drift_scenario_is_backend_independent` |
| A-18 | Low (test defect) | Benchmark sync scenario never exercised the bundle signature check: its tampered bundle was also rejected by the per-record hash check, so disabling signature verification went unnoticed. | Benchmark mutation check, stage 20 (mutant missed). | Scenario recomputes the record hashes like a capable attacker; only the signature can reject it now. | mutant `sync accepts any signature` caught; `test_tampered_bundle_rejected[True]` |
| A-19 | Low | A refused self-approval attempt left no audit record: the guard allowed it (the requester holds the role) and the approval store refused it afterwards, so it was invisible in the audit log, unlike every other refused operation. | `unauthorized_operations` benchmark scenario, stage 20 (5 blocked, 4 audited). | `decide_approval` records `denied` with the reason before re-raising any refusal from the approval store. | `test_refused_self_approval_attempt_is_audited`; mutation (fix removed) fails it |
| A-20 | Medium | A correct fix for a test file that failed to *load* (Python collection error, JS import of a renamed export) was rejected: the file's single failing entry is replaced by its tests after the fix, and the comparison reported "1 test(s) disappeared" and "no previously failing test now passes". | Running a real JS repair, stage 22. | `compare` treats a failed file entry as fixed only if every test that replaces it passes, as still failing otherwise. | `tests/regression/test_import_error_repairs.py` (Python and synthetic), `test_js_repair_end_to_end_with_approval_and_verification`; mutation fails both |
| A-21 | Medium | Co-located JS/TS tests (`*.test.ts`, `*.spec.js`, `__tests__/`) were not recognised as tests, so a model-proposed plan could have edited a test to make it pass. | Review while adding JS execution, stage 22. | `is_test_path` recognises JS/TS conventions (and Node's default patterns). | `test_test_files_are_recognised_so_repairs_never_edit_them`; mutation fails it |
| A-22 | High (pre-release) | The desktop shell's platform server was killed seconds after start-up: Linux sends the parent-death signal (`PR_SET_PDEATHSIG`) when the *thread* that spawned the process exits, and the server was spawned from a short-lived thread. The window showed "connection refused". | Desktop E2E in a real window, stage 23 (before the first release). | The spawning thread now lives as long as the server and waits on it; an unexpected stop is reported in the window. | `desktop_e2e.py` scenario 4 (server alive after 5 s; crash reported) |
| A-23 | Medium | JS/TS repair plans (possible since stage 22) were reviewed with Python patterns only: `child_process`, `new Function`, `fetch` or a changed `package.json` raised nothing. | Review while adding prompt-injection defences, stage 24. | JS/TS rule set; JS/TS build and dependency files are sensitive. | `test_javascript_security_rules`, `test_javascript_build_and_dependency_files_need_attention`; mutations fail them |
| A-24 | Medium | The plan review cut Python lines at the first `#`, so `x = "#"; os.system(...)` hid the call. | Same review, stage 24. | Comments are removed with the Python tokenizer (whole line scanned if it cannot be tokenized); JS lines are scanned whole. | `test_a_hash_inside_a_string_cannot_hide_python_code`; mutation fails it |
| A-14 | Low | No API rate limiting, no token expiry, no TLS. | Review. | **Stage 9:** rate limiting + failed-auth lockout, mandatory token expiry (ADR-0008). TLS still not provided (loopback-only default; `serve` refuses public bind without `--allow-remote`). | `test_rate_limit_per_credential_and_auth_failure_lockout`, `test_expired_and_revoked_tokens_rejected` |
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

## Stage 10 addendum (2026-11-21): same attacks against both backends

| Attack (identical payload) | process backend (root host, uid 65534) | docker backend |
|---|---|---|
| Read a world-readable host file | **open** | blocked |
| See host processes | **open** | blocked |
| Connect to /var/run/docker.sock | blocked (only because of the uid drop - see A-16) | blocked (not mounted) |
| Outbound network | blocked | blocked |
| Write outside the workspace | blocked | blocked |
| Hold kernel capabilities | blocked | blocked |
| Write /proc/sys | blocked | blocked |

Also verified with real containers: timeout kills the container (none left behind), fork bomb stopped by
`--pids-limit`, 1 GiB allocation refused under a 256 MiB limit, platform key invisible even by exact path,
full repair workflow succeeds on docker with a malicious test unable to touch the audit log.
