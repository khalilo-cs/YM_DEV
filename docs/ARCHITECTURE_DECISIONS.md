# Architecture Decisions (index)

| ADR | Title | Status |
|---|---|---|
| [0001](adr/0001-modular-monolith.md) | Modular monolith in one Python package | Accepted |
| [0002](adr/0002-sqlite-stdlib-storage.md) | stdlib `sqlite3` with hand-written migrations | Accepted |
| [0003](adr/0003-approvals-bound-single-use.md) | Approvals bound to an operation digest, single-use | Accepted |
| [0004](adr/0004-hmac-chained-audit.md) | HMAC hash-chained audit log | Accepted |
| [0005](adr/0005-sandbox-process-isolation.md) | Process-level sandbox with network namespace | Accepted |
| [0006](adr/0006-full-content-patches.md) | Repairs as full-content patches with original-hash preconditions | Accepted |
| [0007](adr/0007-project-scoped-authorization.md) | Project-scoped authorization, token role as ceiling | Accepted |
| [0008](adr/0008-rate-limiting-and-token-expiry.md) | In-process rate limiting and mandatory token expiry | Accepted |
| [0009](adr/0009-container-sandbox-backend.md) | Pluggable execution backend with Docker container sandbox | Accepted |
| [0010](adr/0010-per-project-sandbox-images.md) | Per-project sandbox images (wheels only) and `auto` default backend | Accepted |
