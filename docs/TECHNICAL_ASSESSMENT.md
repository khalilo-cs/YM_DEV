# Technical Assessment

## What can be built and verified in this environment

* Python core (FastAPI, Pydantic, stdlib `sqlite3`) - fully testable offline.
* Sandboxed execution of project test suites via restricted subprocesses with
  rlimits and Linux network namespaces (`unshare --net`) - verifiable here.
* Static project intelligence (Python `ast`, `tomllib`) - verifiable here.

## What cannot be verified here

* Local LLM inference (no model, no GPU). The model adapter is implemented against
  the OpenAI-compatible HTTP protocol and tested against a local protocol stub only.
* Android / desktop builds (no SDKs/emulators). UI work is deferred (see roadmap).
* Multi-host synchronization (single container).

## Key constraints

* The platform runs untrusted project code (tests). Isolation is process-level
  (rlimits, new session, scrubbed env, network namespace when available), **not**
  a VM/container boundary. This is a known, documented limitation (RISK-001).
