# Security Policy

Please report vulnerabilities privately to the repository owner rather than opening a public issue.

Security design: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Latest audit: [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md).

Deployment guidance for this early version:
* Run `yemendev serve` on loopback only (default). There is no TLS.
* The default backend is `auto`: containers are used when Docker and the sandbox image are available.
  Run `yemendev sandbox build-image` once, and `yemendev sandbox status` to see the effective backend and
  any warnings. For untrusted projects set `YEMENDEV_SANDBOX_BACKEND=docker` so a missing image fails
  instead of falling back.
* Projects with dependencies: `yemendev sandbox build-image --project <id>` (admin). Pin dependencies for
  reproducible images; only wheels are accepted.
* The process backend refuses to run when sandboxed code could reach a Docker/containerd/podman socket.
  Do not set `allow_runtime_socket_exposure` for untrusted code.
* Docker daemon access is root-equivalent: protect the platform's own account accordingly.
* Treat every token as a credential; tokens are shown once and stored hashed.
