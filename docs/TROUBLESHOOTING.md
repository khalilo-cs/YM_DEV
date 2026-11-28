# Troubleshooting

Problems below were all encountered while building and testing the platform.

| Symptom | Cause | Fix |
|---|---|---|
| `sandbox status` shows `effective: process` with "docker sandbox not usable" | Docker daemon not running, or the sandbox image is not built | Start Docker; run `yemendev sandbox build-image`. Images are never pulled automatically. |
| `docker daemon is not reachable` after a reboot / container restart | `dockerd` is not running | Start the Docker service. Docker tests are skipped (not failed) while it is down. |
| `refusing to run untrusted code: its identity can reach container runtime socket(s)` | Process backend without a uid drop, and the platform user can use Docker (root-equivalent) | Use the docker backend (build its image), or run the platform as root so code runs as uid 65534. Only set `allow_runtime_socket_exposure` for trusted code. |
| `sandbox uid 65534 cannot traverse workspace parent directory` | Workspaces directory sits under a private (0700) directory | Use the default workspaces location or a directory with mode `o+x` on every parent. |
| Tests fail in the sandbox with `ModuleNotFoundError` for a third-party package | Container has only pytest | `yemendev sandbox build-image --project <id>` (admin). The baseline evidence records this limitation. |
| `dependency download failed (wheels only...)` | A dependency has no wheel for the container's Python/arch | Pin a version that ships a wheel; source builds are refused on purpose. |
| `pytest is not installed in the sandbox interpreter` | Process backend interpreter lacks pytest | Install pytest in that interpreter or set `YEMENDEV_PYTHON`. |
| `node is not available in the sandbox image` | Docker image built without Node.js | `yemendev sandbox build-image --with-node` (copies the host's `node`; offline). |
| `Node.js is not available (install it or set YEMENDEV_NODE)` | Process backend cannot find `node` | Install Node.js 22.18+ or set `YEMENDEV_NODE=/path/to/node`. |
| `test runner 'vitest' is not supported yet` | The project's `test` script uses vitest/jest/mocha | Only `node:test` is supported in this version; the result is reported as not runnable, never as passing. |
| JS tests fail with `Cannot find package` in the sandbox | The project's `node_modules` is missing, symlinked (pnpm) or larger than `max_node_modules_mb` | Run `npm ci` in the project first; nothing is installed in the sandbox (no network). |
| HTTP 401 `missing or invalid bearer token` | Token missing, revoked or **expired** (default 90 days) | Issue a new token: `yemendev token issue <name>`. |
| HTTP 403 `no access to this project` | Not a member of the project | An admin runs `yemendev project grant <id> human:<name> --role operator`. |
| HTTP 429 `rate_limited` | Request rate or failed-login budget exceeded | Wait for `Retry-After` seconds. |
| `project root changed since registration` | Root moved, deleted or replaced by a symlink | Review the directory, then register it again. |
| `requester cannot decide their own approval request` | Human-authored plans need a *different* human | Issue a token to a second person and grant them operator on the project. |
| Task stays `running` after a crash | The crashed worker's lease (15 min) has not expired | Wait for the lease to expire, then `yemendev task advance <id>`; the task resumes from the interrupted step. |
| `test_reference_docs_match_the_code` fails | Schema or API changed without regenerating docs | `python scripts/gen_reference_docs.py` |
