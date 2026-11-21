# ADR-0009: Pluggable execution backend with a Docker container sandbox

**Context.** The process backend's protection of platform state depends on dropping to an
unprivileged uid, which only works when the platform runs as root (audit A-13). Even then, untrusted
code can read world-readable host files and see host processes.

**Decision.**
* `sandbox.backend = process | docker | auto`. `docker` is *required* mode: if the daemon or image is
  unusable the run fails with the reason; it never silently falls back. `auto` uses docker when
  usable, otherwise process, and records the choice and reason in the run's result and evidence.
* Default stays `process` (backwards compatible). **Recommended for untrusted projects: `docker`.**
* Container flags: `--network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt
  no-new-privileges --user <non-zero uid> --pids-limit --memory(=swap) --cpus --ulimit fsize/core/cpu
  --init`, only the workspace copy bind-mounted at `/work`; the container is killed on timeout and
  removed on every path.
* Offline-first image: `yemendev sandbox build-image` downloads wheels on the host (or uses a
  pre-staged directory) and builds with `docker build --network none`. Images are never pulled
  implicitly. The image digest is recorded in every run's evidence.
* The process backend refuses to run untrusted code whose identity can reach a container-runtime
  socket (audit A-16) unless `allow_runtime_socket_exposure` is set explicitly.

**Consequences.** ~0.25 s overhead per sandboxed run (~1 s per repair task in the benchmark). The
platform needs Docker daemon access, which is root-equivalent on the host; sandboxed code never has it.
Projects with third-party dependencies need an image that contains them (configurable `docker_image`).
