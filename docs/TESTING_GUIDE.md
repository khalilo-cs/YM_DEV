# Testing Guide

Run everything: `.venv/bin/python -m pytest` (about 2 minutes with Docker available).

| Level | Location | What it proves |
|---|---|---|
| Unit | `tests/unit/` | storage, redaction, memory, scanner/graph, skills, rate limiter, docker command building (no daemon) |
| Integration | `tests/integration/` | test runner + diagnostics, recovery engine, API, CLI, MOSAIC (protocol stub), project images |
| Security | `tests/security/` | audit tampering, governance, sandbox escape attempts, authorization isolation, agents, runtime-socket refusal, audit-finding regressions |
| End-to-end | `tests/end_to_end/` | the 18 MVP criteria, crash at 9 points + resume, concurrency, offline |
| Regression | `tests/regression/` | benchmark fixtures/scenarios, docs-in-sync |
| ORION queue/workers | `tests/unit/test_orion_queue.py`, `tests/integration/test_orion_scheduler.py` | scheduling, dependencies, rights, recovery; real timeouts/kills/cancellation/retries with child processes, sandbox processes and containers |
| Web UI | `tests/integration/test_web_ui.py`, `scripts/ui_e2e.py` | static serving + CSP + traversal; no remote resources; Arabic dictionary completeness (tsc); real-browser flow against a real server |

## Environment-dependent tests

Tests that need Docker (and the sandbox image) or network namespaces are **skipped with the reason
printed** (`pytest -rs`) when unavailable. A skipped test is not a passing test: check the skip list
before reporting results. Process-backend tests pin `backend="process"` explicitly.

## Rules

* A fixed bug gets a regression test that fails without the fix.
* For security tests, verify the test can fail: temporarily remove the protection and confirm a failure
  (mutation check). Several suites were validated this way (see `docs/AUDIT_REPORT.md`).
* Never weaken or delete a failing test to get green.

## Benchmarks

`yemendev bench [--full] [--backend auto|docker|process]` - see `docs/BENCHMARK_METHODOLOGY.md`.
`benchmarks/mutation_check.py` verifies the benchmark detects deliberately broken behaviour.

## Not covered yet

Long-horizon tests (many sequential changes), automated resource/performance tests, real-model
evaluation, multi-host synchronization, accessibility audit with assistive technology, UI tests on
browsers other than Chromium.

## Browser end-to-end

`python scripts/ui_e2e.py --shots <dir>` (needs the built UI, Node.js and the `playwright` package with
Chromium) creates a throw-away installation and drives the UI: sign-in, Arabic/English switch, scan,
graph, a no-plan task (honest `no_action` with its recorded reason), a human-authored plan, refused
self-approval, approval by a second human, successful verified repair, integrity check, phone width.
