# Project Audit (Phase 0)

Date: 2026-11-17 · Branch: `ccr-5b7e38f8-g2boke`

## Starting state (evidence)

| Item | Finding |
|---|---|
| Repository contents | One file: `README.md` (contents: `# YM_DEV`). One commit (`b983f55 Initial commit`). |
| Git status | Clean working tree. |
| Existing code / tests / docs / config | None. |
| Secrets / env files | None present. |
| Classification | **A — empty repository.** Nothing to preserve beyond the README. |

## Environment (observed in the session container)

| Resource | Observed |
|---|---|
| OS | Linux 6.18 (container), 4 vCPU, 15 GiB RAM, no swap |
| Python | 3.13.18 (project targets ≥ 3.11) |
| Package managers | pip 26, uv, poetry; Node 22 / npm 10; cargo/rustc; Go |
| Network | Outbound HTTPS via proxy; PyPI reachable |
| Network namespaces | `unshare --net` works (used for sandbox network isolation) |
| Docker | Binary present (not used yet) |
| Local AI models | **None found.** No llama.cpp / Ollama binaries, no model files. |
| GPUs | None observed |

## Consequences

* Everything is built from scratch as a modular monolith (ADR-0001).
* Any feature that needs a language model is marked **Requires a model**; deterministic
  paths are built first so the MVP workflow works without one.
