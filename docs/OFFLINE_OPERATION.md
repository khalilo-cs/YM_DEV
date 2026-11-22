# Offline Operation

## Works without internet (verified)

Project scanning, sandboxed tests (the container sandbox runs with `--network none`), diagnostics,
repair planning from human plans and skills, simulation, approvals, execution, verification, rollback,
learning, audit and evidence, the HTTP API and CLI. The end-to-end MVP test and the `offline_repair`
benchmark scenario run with all outbound sockets of the platform process blocked.

## Needs a network once

Installing Python packages; `docker pull` of the base image; downloading wheels for sandbox images
(or provide them with `--wheels <dir>` for a fully offline build).

## Degrades without network

A *remote* model endpoint becomes unavailable: model proposals are skipped and the reason is recorded
in the propose evidence (`no model available` / `model endpoint unreachable`). A local model server on
the same machine keeps working.

## Power loss

SQLite runs in WAL mode with `synchronous=FULL`; file writes are atomic (temp + fsync + rename);
checkpoints are written before any project change; interrupted tasks resume from the interrupted step.
Crashes are tested by fault injection, **not** by real power cuts.

## Not implemented

Deferred synchronization of work done offline to another node - see [SYNCHRONIZATION.md](SYNCHRONIZATION.md).
