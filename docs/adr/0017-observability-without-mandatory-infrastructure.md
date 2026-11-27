# ADR-0017: Observability without mandatory infrastructure

**Context.** The directive lists OpenTelemetry, structured logging, metrics and tracing. Installations may
be offline and small, with no collector or metrics backend.

**Decision.** Structured JSON logs (redacted, with request/trace ids) and a dependency-free Prometheus text
endpoint are always on. Tracing uses the OpenTelemetry **API** (already a FastAPI dependency; a no-op until
configured): spans for requests, background runs and every workflow step, with W3C context passed into the
run process. Exporting is opt-in through the `otel` extra (`YEMENDEV_OTEL=console|otlp`).

**Consequences.** Zero extra infrastructure by default; incoming `traceparent` is honoured even without an
SDK. Metrics are per process (a standalone worker does not expose its own); logs and metrics are not
exported over OTLP; metric labels use route templates only, so cardinality stays bounded.
