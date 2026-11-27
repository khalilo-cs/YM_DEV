# Deployment

> Status: YEMENDEV AI is an **early core (0.1.0)**, verified on a single Linux host. It is not yet
> production-ready: no external security audit, no TLS, no multi-node operation.

## Supported topology

Single node. `yemendev serve` serves the API and, by default, one background worker that runs queued
tasks (`--workers N`, `0` to disable). More workers can run in separate processes on the same host
(`yemendev worker --workers N`, admin token); they share the queue through the database, and
`YEMENDEV_MAX_CONCURRENT_RUNS` caps runs across all of them. Each run is a separate process with a
wall-clock budget (`YEMENDEV_RUN_TIMEOUT`, default 3600 s); stopping a worker hands its runs back to the
queue without using an attempt.
State lives in `YEMENDEV_DATA_DIR` (SQLite + audit key + checkpoints).

## Logs, metrics and traces

* Logs: JSON lines on stderr (`YEMENDEV_LOG_FORMAT=json|text`, `YEMENDEV_LOG_LEVEL`), one access line per
  request with route template, status and duration; every line carries `request_id` and, when tracing is
  active, `trace_id`/`span_id`. Secrets are redacted before writing.
* Metrics: `GET /api/v1/metrics` (Prometheus text format; needs a bearer token - configure the scraper's
  `authorization`). Counters/histograms are per process; state gauges come from the database.
* Traces: `pip install 'yemendev[otel]'`, then `YEMENDEV_OTEL=otlp` (collector from the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT`) or `YEMENDEV_OTEL=console`. Incoming `traceparent` is honoured and returned.

## Recommended setup

1. Dedicated VM or host. Platform user with access to Docker; **set `YEMENDEV_SANDBOX_BACKEND=docker`**
   for untrusted projects so a missing image fails instead of falling back.
2. `yemendev serve` binds to `127.0.0.1` by default. For remote access put a TLS-terminating reverse
   proxy in front; `serve` refuses a public bind without `--allow-remote`.
3. Rate limits are per process; behind a proxy, enforce limits in the proxy too (all clients share the
   proxy's IP from the platform's point of view).

## Backups

```bash
yemendev backup create                 # admin: DB snapshot + keys + checkpoints -> data_dir/backups/*.tar.gz (0600)
yemendev backup verify <backup_id>     # full check on a temporary copy; result stored and audited
yemendev backup restore <archive> --to /new/data/dir   # verifies first; only into a new or empty directory
yemendev readiness                     # recovery readiness checks (backup age, integrity, disk, conflicts, ...)
```

The database is copied with SQLite's online backup API (consistent while the platform runs). A backup
contains `keys/audit.key` (needed to verify the audit chain) and the signing key, so **store it like a
secret**: anyone holding the archive could also produce a consistent forged one. Verification therefore also
compares the archive with the SHA-256 this installation recorded when it created it. For off-host copies, also
keep the audit head hash (`yemendev audit` → `head_hash`) somewhere else to detect truncation later.

## Upgrades

Migrations run automatically at startup and are append-only. Back up before upgrading.

## Not supported yet

PostgreSQL / multi-user scale-out, high availability, workers on several hosts, TLS termination, log/metric
export over OTLP (traces only), Windows hosts.
