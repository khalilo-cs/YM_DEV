# Deployment

> Status: YEMENDEV AI is an **early core (0.1.0)**, verified on a single Linux host. It is not yet
> production-ready: no external security audit, no TLS, no multi-node operation.

## Supported topology

Single node. One process serves the API (`yemendev serve`) and runs tasks synchronously per request.
State lives in `YEMENDEV_DATA_DIR` (SQLite + audit key + checkpoints).

## Recommended setup

1. Dedicated VM or host. Platform user with access to Docker; **set `YEMENDEV_SANDBOX_BACKEND=docker`**
   for untrusted projects so a missing image fails instead of falling back.
2. `yemendev serve` binds to `127.0.0.1` by default. For remote access put a TLS-terminating reverse
   proxy in front; `serve` refuses a public bind without `--allow-remote`.
3. Rate limits are per process; behind a proxy, enforce limits in the proxy too (all clients share the
   proxy's IP from the platform's point of view).

## Backups

Back up the **whole** data directory, including `keys/audit.key`: without the key the audit chain
cannot be verified. Use SQLite's online backup (e.g. `sqlite3 yemendev.sqlite3 ".backup backup.sqlite3"`)
rather than copying a live WAL database. Record the audit head hash
(`yemendev audit` → `head_hash`) outside the host to detect truncation later
(`yemendev audit --expected-head <hash>`).

A built-in backup/restore command and backup verification are **not implemented yet**.

## Upgrades

Migrations run automatically at startup and are append-only. Back up before upgrading.

## Not supported yet

PostgreSQL / multi-user scale-out, high availability, background workers, TLS termination, central
logging/metrics (OpenTelemetry), Windows hosts.
