# ADR-0003: Bound, single-use approvals

An approval row stores `(action, target, operation_digest, expires_at)`. The digest is the
SHA-256 of the canonical JSON of the exact operation (e.g. every patched path, its original
hash and new content hash). `consume()` performs a single conditional `UPDATE ... WHERE
status='approved' AND action=? AND target=? AND operation_digest=? AND expires_at>now`, so
an approval cannot be reused, used after expiry, or used for a different/modified
operation. Only human actors may decide; requesters cannot decide their own requests.
