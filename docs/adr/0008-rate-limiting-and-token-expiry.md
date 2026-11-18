# ADR-0008: In-process rate limiting and mandatory token expiry

* Every token has `expires_at` (default 90 days, 1 minute-366 days). Migration 2 gives legacy
  tokens 90 days from migration time rather than leaving them valid forever.
* Token-bucket limiter keyed by SHA-256 of the Authorization header (or client IP when absent);
  a second, stricter per-IP bucket counts 401 responses and locks the client out (even with a
  valid token) once exhausted. Both are LRU-bounded (10k keys) and run as the outermost
  middleware, so throttled requests are rejected before their body is read.
* Limitation: limits are per process. Behind several workers or a proxy, put limits in the proxy;
  behind a reverse proxy the client IP is the proxy's unless the server is configured to trust
  forwarded headers (not done here).
