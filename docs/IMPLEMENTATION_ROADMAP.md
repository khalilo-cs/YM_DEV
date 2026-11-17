# Implementation Roadmap

Working in independently verifiable stages. "Done" = acceptance tests pass in CI/local.

| Stage | Scope | Status |
|---|---|---|
| 0 | Repository & environment audit, ADRs, risk register | Done |
| 1 | Foundation: contracts, SQLite + migrations, audit log, governance/approvals, evidence, memory | **Done - 31 tests passing** |
| 2 | Project intelligence: scanner, dependency extraction, import graph, memory population | **Done - 43 tests total** |
| 3 | Sandbox execution, pytest runner (JUnit parsing), diagnostics | **Done - 62 tests total** |
| 4 | Verified repair lifecycle + skill engine | **Done - 86 tests total** |
| 5 | ORION persistent orchestration, crash resume, fault injection | **Done - 102 tests total; MVP criteria 1-18 demonstrated** |
| 6 | HTTP API + CLI | **Done - 115 tests total** |
| 7 | Independent audit of stages 1-6, fixes, regression tests | **Done - see AUDIT_REPORT.md** |
| 8 | Resilience benchmark harness (reproducible runs, fault scenarios, metrics, stored results) | Next |
| 9 | Per-project authorization (A-11), token expiry, rate limiting | Planned |
| 10 | Container sandbox backend (Docker) for non-root hosts | Planned |
| later | Risk engine, sync queue, UI (web/desktop/mobile), i18n, real-model evaluation | Not started |
