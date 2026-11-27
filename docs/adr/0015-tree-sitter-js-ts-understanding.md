# ADR-0015: Tree-sitter for JavaScript/TypeScript understanding

**Context.** The directive lists Tree-sitter, Python AST, LSP and static analysis for code understanding.
Until stage 18 only Python was parsed; JS/TS files were counted, not understood.

**Decision.** Parse JS/TS with Tree-sitter (`tree-sitter`, `tree-sitter-javascript`,
`tree-sitter-typescript`; prebuilt wheels, no network or compiler at runtime). Extract imports of every
form and resolve them only against files the scanner already listed (never opening paths taken from source
code). JS/TS modules are named by project-relative path so they can never collide with Python's dotted
names; dependencies carry an `ecosystem` (pypi | npm) and are judged separately. If the grammars are
missing, the scan degrades with an explicit finding instead of failing.

**Consequences.** Error-tolerant parsing yields imports even from files with syntax errors (reported). The
parser is native code processing untrusted input in-process (2 MB cap per file; residual risk recorded in
THREAT_MODEL). Not done: LSP (types, cross-file symbols), tsconfig `extends`, monorepo workspaces, package
`exports` maps, and executing or repairing JS/TS tests.
