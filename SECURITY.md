# Security Policy

Please report vulnerabilities privately to the repository owner rather than opening a public issue.

Security design: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Latest audit: [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md).

Deployment guidance for this early version:
* Run `yemendev serve` on loopback only (default). There is no TLS.
* For untrusted projects, run the platform as root in a dedicated container/VM so sandboxed code
  can be dropped to an unprivileged uid, and set `privilege_separation=required`.
* Treat every token as a credential; tokens are shown once and stored hashed.
