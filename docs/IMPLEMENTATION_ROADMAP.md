# Implementation Roadmap

Working in independently verifiable stages. "Done" = acceptance tests pass in CI/local.

| Stage | Scope | Status |
|---|---|---|
| 0 | Repository & environment audit, ADRs, risk register | Done |
| 1 | Foundation: contracts, SQLite + migrations, audit log, governance/approvals, evidence, memory | **Done - 31 tests passing** |
| 2 | Project intelligence: scanner, dependency extraction, import graph, memory population | Next |
| 3 | Sandbox execution, pytest runner (JUnit parsing), diagnostics | Planned |
| 4 | Verified repair lifecycle + skill engine | Planned |
| 5 | ORION persistent orchestration, crash resume, fault injection | Planned |
| 6 | HTTP API + CLI | Planned |
| 7 | Independent audit of stages 1-6, fixes, regression tests | Planned |
| later | Model adapters w/ real model, risk engine, sync queue, UI (web/desktop/mobile), benchmarks, i18n | Not started |
