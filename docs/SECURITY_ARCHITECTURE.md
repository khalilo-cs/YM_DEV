# Security Architecture

Threats and residual risks: [THREAT_MODEL.md](THREAT_MODEL.md). Findings and fixes:
[AUDIT_REPORT.md](AUDIT_REPORT.md). Secure development lifecycle reference: NIST SSDF.

## Layers

| Layer | Mechanism | Code |
|---|---|---|
| Identity | Bearer tokens, SHA-256 at rest, mandatory expiry, revocation | `security/tokens.py` |
| Authorization | Static role policy + per-project memberships (token role = ceiling) | `governance/policy.py`, `membership.py`, `guard.py` |
| Human control | Approvals bound to action + target + digest(task, project, files); single-use; expiring; revoked on cancel; requester ≠ decider; agents never decide | `governance/approvals.py` |
| Agent control | Fixed allow-lists, intersection with the human's rights, every run recorded | `agents/` |
| Change review | Security agent blocks dangerous added code; caution findings shown to approver | `agents/security_agent.py` |
| Untrusted code | Copy-on-run workspace; docker backend (no network, read-only rootfs, cap-drop ALL, no-new-privileges, non-root, pids/memory/cpu limits) or process backend (rlimits, netns, uid drop); refusal if the run identity can reach a container runtime socket | `sandbox/` |
| Supply chain | Project images from wheels only, no URL/VCS/path specs, `--network none` builds, wheel hashes recorded; `pip-audit` clean | `sandbox/project_image.py` |
| File integrity | Normalized relative patch paths, original-hash preconditions, per-component symlink refusal, root re-validation, atomic writes, verified byte-exact rollback | `recovery/` |
| Tamper evidence | HMAC hash-chained audit (key outside DB, 0600); evidence content-hashed and anchored in the chain; `/integrity/verify` | `security/audit.py`, `evidence/` |
| Secrets | Secret-looking files never read or copied; redaction on audit/evidence; env scrubbed in sandbox | `security/redaction.py`, `twin/scanner.py` |
| API | Error envelope without internals, body limit (incl. chunked), rate limits + failed-auth lockout, loopback default | `api/app.py` |
| Web UI | Static same-origin client; strict CSP (no inline/third-party script, `connect-src 'self'`, no framing); token in `sessionStorage` (no cookies, no CSRF); no symlink following; confirmation before sensitive actions; all authorization server-side | `api/app.py`, `apps/web/` |
| Platform state | data dir 0700, DB 0600, workspaces outside the data dir | `core/config.py`, `platform.py` |

## Verification

Security behaviour is covered by `tests/security/` (escape attempts, tampering, isolation, agents,
socket exposure) and was validated by attack scripts and mutation checks during the stage 7 and 10
audits. Two critical issues were found and fixed (A-01, A-16).

## Not provided (yet)

External penetration test, TLS, SBOM generation, syscall-level isolation beyond Docker defaults
(gVisor/Firecracker), prompt-injection defences beyond "model output is untrusted and only proposes".
