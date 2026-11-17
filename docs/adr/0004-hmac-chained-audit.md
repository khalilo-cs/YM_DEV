# ADR-0004: HMAC hash-chained audit log

Each audit event's hash is `HMAC-SHA256(key, prev_hash || canonical(event))`. The key lives
in `data_dir/keys/audit.key` (0600), not in the database. Verification detects modification,
deletion and reordering; truncation of the tail is detected only against an externally
recorded head hash. An attacker holding both the key file and DB write access can forge the
chain - see THREAT_MODEL.
