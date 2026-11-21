# ADR-0010: Per-project sandbox images and 'auto' as the default backend

**Context.** The container sandbox only had pytest, so any project with third-party dependencies
failed inside it. Installing dependencies is a supply-chain risk: `pip download` of a source
distribution may run the package's build code on the host.

**Decision.**
* `yemendev sandbox build-image --project <id>` / `POST /api/v1/projects/{id}/sandbox-image`
  (admin only, audited, evidence recorded) builds `yemendev-proj:<hash>` = base sandbox image + the
  project's declared dependencies (main + dev/test groups, from the latest scan).
* **Wheels only** (`--only-binary=:all:`), selected for the container's own Python version,
  architecture and glibc (read from the base image). URL, VCS, local-path requirements and pip
  options are rejected. The build runs with `--network none`. Each wheel's SHA-256 is recorded.
* Tag = hash(base image id + exact requirement list): unchanged dependencies reuse the image;
  changed dependencies or a rebuilt base image never use a stale image.
* The workflow uses the project image when it exists; otherwise it runs on the base image and
  records the limitation in the baseline evidence (so missing-module failures are explainable).
* The default backend becomes `auto`. `isolation_report()` states the effective backend and
  plain-language warnings (`yemendev sandbox status`, `GET /api/v1/system/status`).

**Consequences.** Packages without a compatible wheel are unsupported in project images. Unpinned
requirements resolve to the newest wheels at build time (recorded, but not reproducible without pins).
