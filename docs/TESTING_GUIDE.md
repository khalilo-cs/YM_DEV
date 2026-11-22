# Testing Guide

Run everything: `.venv/bin/python -m pytest` (about 2 minutes with Docker available).

| Level | Location | What it proves |
|---|---|---|
| Unit | `tests/unit/` | storage, redaction, memory, scanner/graph, skills, rate limiter, docker command building (no daemon) |
| Integration | `tests/integration/` | test runner + diagnostics, recovery engine, API, CLI, MOSAIC (protocol stub), project images |
| Security | `tests/security/` | audit tampering, governance, sandbox escape attempts, authorization isolation, agents, runtime-socket refusal, audit-finding regressions |
| End-to-end | `tests/end_to_end/` | the 18 MVP criteria, crash at 9 points + resume, concurrency, offline |
| Regression | `tests/regression/` | benchmark fixtures/scenarios, docs-in-sync |

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
evaluation, multi-host synchronization, UI tests (no UI exists).
