# Technical Specification (implemented behaviour)

This specifies what the code does today. Planned behaviour is in [ROADMAP.md](ROADMAP.md).

## Repair task state machine

| Status | Meaning |
|---|---|
| `pending` | created, not started |
| `running` | a worker holds the lease (15 min; expired leases can be re-acquired) |
| `waiting_approval` | stopped at the human gate; re-checked on the next `advance` |
| `succeeded` | change applied and verified |
| `rolled_back` | change applied, verification failed, originals restored and hash-verified |
| `no_action` | nothing reproducible to fix, no plan, every plan blocked, or none passed simulation |
| `cancelled` | cancelled, or the approval was denied/expired (approvals of a cancelled task are revoked) |
| `failed` | workflow error (evidence recorded), e.g. project root changed, files diverged |

## Acceptance rules

* **Reproducible problem:** a test fails in *both* baseline runs (otherwise flaky, no action).
* **Simulation / verification pass** only if: test run completed normally, ≥1 previously failing test
  now passes, no previously passing test fails or is skipped, no test disappeared.
* **Plans** may not modify test files or test configuration (`conftest.py`, `pytest.ini`, `tox.ini`,
  `setup.cfg`, `noxfile.py`), and every patched file must still have the hash the plan was built from.
* **Security review:** HIGH finding → plan blocked; MEDIUM → shown to the approver.

## Limits (defaults)

| Limit | Default | Setting |
|---|---|---|
| Sandbox wall clock | 120 s | `YEMENDEV_SANDBOX_TIMEOUT` |
| Sandbox memory | 2048 MB | `SandboxSettings.max_memory_mb` |
| Container PIDs / CPUs / tmpfs | 256 / 2.0 / 256 MB | `docker_pids_limit`, `docker_cpus`, `docker_tmpfs_mb` |
| Captured output | 200 KB per stream | `max_output_bytes` |
| API request body | 5 MB (incl. chunked) | `api/app.py` |
| API rate | 600/min, burst 120 per credential; 30 failed logins/min per client | env vars |
| Approval lifetime | 1 hour | `approval_ttl_seconds` |
| Token lifetime | 90 days (1 min - 366 days) | `YEMENDEV_TOKEN_TTL_DAYS`, `--ttl-days` |
| Scanner | 50,000 files; Python files ≤ 2 MB parsed | `twin/scanner.py` |

## Supported inputs

Python projects with pytest (other languages are counted, not analysed). Repairs are full-content
file patches (ADR-0006). The only executable skill kind is `token_rename`.

## Interfaces

HTTP API: [API_REFERENCE.md](API_REFERENCE.md) (generated). CLI: `yemendev --help`.
Storage: [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) (generated).
