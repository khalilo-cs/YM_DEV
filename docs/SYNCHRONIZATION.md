# Synchronization

**Status: IMPLEMENTED for file-based, signed, offline transfer between installations (stage 15).**
There is no network transport; nothing is reported as delivered to a peer.

Code: `src/yemendev/continuity/sync.py`. Tables: `sync_jobs`, `sync_conflicts`
([DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)). Decision: [ADR-0012](adr/0012-signed-offline-sync-and-passports.md).

## What is synchronized

| Synced | Never synced (local trust records) |
|---|---|
| Active memory records (verified/unverified) of one project | Approvals, audit events, API tokens, evidence |
| Verified/active skills | Memberships, tasks, checkpoints |

## Flow

```bash
# installation A
yemendev continuity sync-export <project_id> --out bundle.json     # job status: exported_not_delivered
yemendev continuity keys-show                                       # give A's public key to B out of band
# installation B (admin trusts A once)
yemendev continuity keys-trust <A's public key hex>
yemendev continuity sync-import <local_project_id> bundle.json
yemendev continuity sync-conflicts <local_project_id>
yemendev continuity sync-resolve <conflict_id> keep_local|take_remote
```

## Rules (each tested; see `tests/integration/test_continuity.py`)

* Bundles are Ed25519-signed; modified or unsigned bundles are rejected; bundles from **untrusted**
  signers are rejected (an admin trusts a key explicitly).
* Imported memory is always **unverified** locally; imported skills start **PROPOSED**.
* A remote value that differs from an active local value is **never written**: it becomes an open
  conflict. Only a human operator on that project resolves it (`take_remote` writes it as verified,
  backed by a resolution evidence record; `keep_local` changes nothing).
* Re-importing the same bundle returns the existing job (idempotent). A crash mid-import leaves the job
  `pending`; re-importing is safe because already-applied records are recognised.
* Per-record hashes are checked inside the signed bundle.

## Limitations

Manual key exchange; no automatic transport or scheduling; no deletion propagation; conflicts are
detected per (subject, key) only; business invariants across records are not checked.
