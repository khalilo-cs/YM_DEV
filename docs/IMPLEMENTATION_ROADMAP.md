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
| 8 | Resilience benchmark harness (reproducible runs, fault scenarios, metrics, stored results) | **Done - 15/15, mutation-checked** |
| 9 | Per-project authorization (A-11), token expiry, rate limiting | **Done** |
| 10 | Container sandbox backend (Docker) for non-root hosts | **Done - verified with real containers** |
| 11 | Make `docker` the recommended default where available (`auto` + warnings); per-project sandbox images | **Done - 197 tests; benchmark 15/15 on auto** |
| 11b | Agents as real components (registry, runner, security agent, conflict policy); generated reference docs + drift test; directive section-18 documents | **Done - 215 tests** |
| 12 | Predictive risk engine + causal analysis (directive 5.5, 5.6) | **Done** |
| 13 | Digital immune system + continuity debt (5.9, 6.6) | **Done** |
| 14 | Counterfactual future engine (6.2) | **Done** |
| 15 | Synchronization + digital continuity passport (5.10, 6.3) | **Done** |
| 16 | Web UI, Arabic/English (16, 17) | **Done - 256 tests; browser E2E 34/34 (`scripts/ui_e2e.py`)** |
| 17 | ORION queue, scheduling, retries (5.2) | Next |
| 18 | JS/TS scanning (Tree-sitter), SBOM, structured logging; installer | Planned |
| blocked | Real-model evaluation, comparative baseline B, Android/desktop builds (no model / SDKs here) | Blocked in this environment |
| later | Desktop (Tauri) and mobile companion apps, real-model evaluation | Not started |
