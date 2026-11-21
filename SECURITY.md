# Security Policy

Please report vulnerabilities privately to the repository owner rather than opening a public issue.

Security design: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Latest audit: [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md).

Deployment guidance for this early version:
* Run `yemendev serve` on loopback only (default). There is no TLS.
* **For untrusted projects use the container backend:** `yemendev sandbox build-image`, then
  `YEMENDEV_SANDBOX_BACKEND=docker`. It works on non-root hosts and blocks host file and process access.
* The process backend refuses to run when sandboxed code could reach a Docker/containerd/podman socket.
  Do not set `allow_runtime_socket_exposure` for untrusted code.
* Docker daemon access is root-equivalent: protect the platform's own account accordingly.
* Treat every token as a credential; tokens are shown once and stored hashed.
