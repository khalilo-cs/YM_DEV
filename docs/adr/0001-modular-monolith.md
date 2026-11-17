# ADR-0001: Modular monolith

**Context.** Empty repo; the directive warns against premature microservices.
**Decision.** One package `yemendev` under `src/`, with internal boundaries per engine
(`storage`, `security`, `governance`, `evidence`, `memory`, `twin`, `sandbox`,
`diagnostics`, `learning`, `recovery`, `mosaic`, `orion`, `api`, `cli`). Engines
communicate through typed Pydantic models; only `platform.py` wires them together.
**Consequences.** Simple deploy and test; a module can be extracted later if profiling or
operations justify it. The proposed top-level layout (`apps/`, `agents/`, ...) is not
created until there is code for it.
