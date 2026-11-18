# Threat Model (current implementation)

## Assets
Platform DB (approvals, tokens, evidence, memory), HMAC audit key, registered project trees, API tokens.

## Trust boundaries
1. **API / CLI callers** — authenticated by bearer token; authorized by a static role policy.
2. **Untrusted project code** (tests, conftest, imported modules) — runs only inside a sandbox workspace.
3. **Model output** — untrusted text; parsed and schema-validated; can only propose plans.
4. **Platform process** — trusted.

## Threats and controls

| Threat | Control | Residual risk |
|---|---|---|
| Project code tampers with platform state | Copy-on-run workspace outside data dir; unprivileged uid (when root); 0700 data dir; rlimits; netns | Without privilege separation (non-root host) code runs as platform user (A-13). No syscall filter. |
| Project code exfiltrates data over network | `unshare --net` (loopback only), recorded per run | Best-effort unless `network_isolation=required` |
| Project forges test results | None possible from outside; documented in evidence | Verification is only as trustworthy as the project (A-12) |
| Model proposes malicious patch | Model sees/edits only non-test files it was shown; simulation; bound human approval; verify; rollback | A human may approve a subtle malicious patch; diff is shown in the approval summary |
| Approval replay / reuse / scope creep | Approval bound to action+target+digest(task, project, files), expiring, single-use, revoked on cancel | - |
| Self-approval | Requester ≠ decider; only humans decide; plan provenance set server-side | Single-user installs need a second identity to approve human-authored plans |
| Path traversal / symlink writes | Normalized relative patch paths; per-component symlink refusal; root re-validation on use | - |
| Audit tampering | HMAC chain, key outside DB (0600), evidence anchored in chain | Key + DB both compromised → forgeable; tail truncation needs external head |
| Secret leakage | `.env`/keys never read by scanner nor copied into sandbox; redaction on audit/evidence; tokens hashed | Pattern-based redaction can miss novel formats |
| DoS via large/many requests | Body limit (Content-Length and chunked), list limits, per-credential rate limit, failed-auth lockout | Per process only; behind a proxy all clients share the proxy IP |
| Cross-project access by a legitimate user | Memberships; token role is a ceiling; scoped lists, reads and approval decisions; audit admin-only | Only global admins manage members |
| Stolen long-lived token | Mandatory expiry (default 90 d), revocation | No refresh/rotation flow yet |
