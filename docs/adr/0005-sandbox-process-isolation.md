# ADR-0005: Process-level sandbox

Untrusted project code runs in a **copy** of the project in a temporary workspace, via
`subprocess` with: no shell, new session (whole process group killed on timeout), scrubbed
environment, `RLIMIT_AS/CPU/FSIZE/CORE`, and a network namespace via `unshare --net` when
available. Whether network isolation was actually applied is recorded in every execution
result. This is **not** a security boundary equivalent to a VM; code can still read files the
OS user can read. Container/VM isolation is a planned hardening item (RISK-001).
