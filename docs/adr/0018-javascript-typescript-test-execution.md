# ADR-0018: JavaScript/TypeScript test execution and repair with node:test

**Context.** Stage 18 made the platform *understand* JS/TS projects, but repairs were only verified for Python:
every phase ran pytest. Verifying a JS/TS repair needs its tests to run in the same sandbox, with the same
"no report, no result" rule, offline.

**Decision.**
* Run Node's built-in runner (`node --test`): no third-party packages, and TypeScript runs directly (type
  stripping, Node >= 22.18). Only plain paths/globs from `"test": "node --test …"` are passed on - never options
  or their values. Other runners (vitest, jest, mocha, …) are recognised and reported as **not supported**
  (`runner_error`), not guessed at.
* A small reporter written by the platform emits one JSON line per test. The JUnit reporter was rejected: it
  drops the error of a file that fails to load (an import of a renamed export), which is what diagnosis needs.
  The file's stderr is attached to that file-level failure, and paths are made project-relative.
* A project gets the suites it has: pytest as before; node:test when `package.json` has tests; both, merged, when
  it has both - with the least favourable status (a suite that could not run never hides behind one that passed).
* Process backend: memory is limited with `RLIMIT_DATA` for Node instead of `RLIMIT_AS`. Measured: V8's
  WebAssembly (used for type stripping) cannot start under a 2 GB address-space limit, while `RLIMIT_DATA`
  lets TypeScript run under 512 MB and still stops a 3 GB allocation. Python keeps `RLIMIT_AS`.
* Docker backend: `yemendev sandbox build-image --with-node` copies the host's Node binary into the image
  (offline build, verified by running it during the build).
* Dependencies come only from the project's own `node_modules`, **copied** (never linked, so sandboxed code
  cannot change the real dependencies) up to `max_node_modules_mb`; nothing is installed (no network).
* Diagnosis reads JS errors and stack locations; JS tests (`*.test.*`, `*.spec.*`, `__tests__/`) are test
  paths, so no repair plan may edit them.

**Consequences.** JS/TS repairs go through the same simulation, approval, checkpoint, verification and
rollback as Python ones, on both backends. Fixing this exposed a general defect (A-20): a correct fix for a
test file that failed to *load* was rejected as "test disappeared" - in Python too. Limitations: only node:test;
learned skills remain Python-only (JS repairs come from humans or a model); pnpm/workspace symlinked
`node_modules` layouts are not followed; a suite that writes a well-formed passing report cannot be detected
(as with pytest, A-12).
