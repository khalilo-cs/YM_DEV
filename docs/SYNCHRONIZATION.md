# Synchronization

**Status: NOT IMPLEMENTED.**

All state is local to one node (SQLite). There is no synchronization queue, no conflict detection
between nodes, and no remote store. Nothing is marked as "synced", and no remote operation is reported
as successful.

## Requirements carried forward (directive sections 5.10, 12)

* Never silently discard work or overwrite important data; distinguish local success from remote sync.
* Validate integrity of synchronized data (the audit chain and evidence hashes are the starting point).
* Do not assume CRDTs preserve business invariants; approvals and audit records must not be merged
  automatically.

## Planned approach (not built)

Append-only outbox of syncable records (memory, evidence references, skills) with per-record hashes;
explicit conflict records requiring human resolution for verified-vs-verified disagreements; audit
events replicated read-only. Tracked in [ROADMAP.md](ROADMAP.md).
