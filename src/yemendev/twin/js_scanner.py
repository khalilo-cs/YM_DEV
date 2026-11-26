"""JavaScript / TypeScript understanding with Tree-sitter (directive: "Code understanding: Tree-sitter").

Read-only and offline, like the Python scanner: files are parsed, never executed or installed.
Tree-sitter is error-tolerant, so a file with syntax errors still yields the imports it can recognise;
such files are reported (``parse_error``) and their edges keep ``certainty=observed`` only for what was
actually read.

Extracted per file (module name = project-relative path, e.g. ``src/utils/math.ts``):

* ES ``import ... from "x"`` (incl. ``import type`` - marked ``type_only``), side-effect ``import "x"``,
  ``export ... from "x"``, TypeScript ``import x = require("x")``, CommonJS ``require("x")`` and
  dynamic ``import("x")``. A non-literal ``require(expr)`` / ``import(expr)`` is an ``unknown`` edge.
* Resolution: relative specifiers against the file (extensions, ``index`` files, ``.js`` -> ``.ts``
  source mapping); ``tsconfig.json`` ``baseUrl``/``paths`` (single ``*`` patterns); Node built-ins
  (``fs``, ``node:fs``...) are the standard library; anything else is an npm package (``@scope/name``).
* Declared dependencies from every ``package.json`` (dependencies / dev / peer / optional).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from yemendev.twin.model import Certainty, DeclaredDependency, Edge, EdgeKind, FileInfo, Finding, ModuleInfo

JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
NODE_BUILTINS = frozenset("""
assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain
events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl
stream string_decoder sys test timers tls trace_events tty url util v8 vm wasi worker_threads zlib sqlite sea
""".split())
_TEST_FILE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.IGNORECASE)
DEP_GROUPS = {"dependencies": "main", "devDependencies": "dev", "peerDependencies": "peer",
              "optionalDependencies": "optional"}
TEST_RUNNERS = ("vitest", "jest", "mocha", "ava", "jasmine")


def _languages() -> dict[str, Any] | None:
    """Tree-sitter languages, or None if the grammars are not installed (the scan degrades, it does not fail)."""
    try:
        import tree_sitter_javascript as tsjs
        import tree_sitter_typescript as tsts
        from tree_sitter import Language
    except ImportError:
        return None
    js = Language(tsjs.language())
    return {".js": js, ".jsx": js, ".mjs": js, ".cjs": js, ".ts": Language(tsts.language_typescript()),
            ".mts": Language(tsts.language_typescript()), ".cts": Language(tsts.language_typescript()),
            ".tsx": Language(tsts.language_tsx())}


def is_js_path(rel: str) -> bool:
    return rel.endswith(JS_EXTS) and not rel.endswith(".min.js")


def _is_test(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    in_test_dir = len(parts) > 1 and parts[0] in ("test", "tests", "e2e")
    return bool(_TEST_FILE.search(rel)) or "__tests__" in parts or in_test_dir


def _literal(node: Any) -> str | None:
    """Text of a string literal / template literal without substitutions, else None."""
    if node is None:
        return None
    if node.type == "string":
        frag = [c for c in node.named_children if c.type in ("string_fragment", "escape_sequence")]
        return "".join(c.text.decode("utf-8", "replace") for c in frag) if frag else ""
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.named_children):
        text: str = node.text.decode("utf-8", "replace")
        return text[1:-1]
    return None


def _specifiers(root: Any) -> Iterator[tuple[str | None, int, bool]]:
    """Yield (specifier or None if dynamic, line, type_only) for every import-like construct."""
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ("import_statement", "export_statement"):
            src = n.child_by_field_name("source")
            if src is None:
                req = next((c for c in n.children if c.type == "import_require_clause"), None)
                src = req.child_by_field_name("source") if req is not None else None
            if src is not None:
                type_only = t == "import_statement" and any(c.type == "type" for c in n.children)
                yield _literal(src), n.start_point[0] + 1, type_only
        elif t == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and (fn.type == "import" or (fn.type == "identifier" and fn.text == b"require")):
                args = n.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                yield _literal(first), n.start_point[0] + 1, False
        stack.extend(reversed(n.children))


def _strip_jsonc(text: str) -> str:
    """tsconfig.json allows comments and trailing commas; remove them outside string literals."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class JsAnalysis:
    def __init__(self, root: Path, sources: dict[str, bytes], files: list[FileInfo]) -> None:
        self.root = root
        self.sources = sources
        self.files = {f.path: f for f in files}
        self.modules: list[ModuleInfo] = []
        self.edges: list[Edge] = []
        self.findings: list[Finding] = []
        self.declared: list[DeclaredDependency] = []
        self.test_framework: str | None = None
        self._base_url, self._paths = self._tsconfig()

    def run(self) -> JsAnalysis:
        self._package_json()
        if not self.sources:
            return self
        langs = _languages()
        if langs is None:
            self.findings.append(Finding(
                kind="js_ts_analysis_unavailable", severity="medium", certainty=Certainty.OBSERVED,
                message=f"{len(self.sources)} JavaScript/TypeScript file(s) listed but not analysed: "
                        "the tree-sitter grammars are not installed"))
            return self
        from tree_sitter import Parser

        known = set(self.sources)
        for rel in sorted(self.sources):
            self.modules.append(ModuleInfo(name=rel, path=rel, is_package=False, is_test=_is_test(rel),
                                           language="typescript" if ".ts" in Path(rel).suffix else "javascript"))
            ext = Path(rel).suffix
            tree = Parser(langs[ext]).parse(self.sources[rel])
            fi = self.files[rel]
            fi.parsed = True
            if tree.root_node.has_error:
                fi.parse_error = "syntax errors (tree-sitter recovered the rest of the file)"
                self.findings.append(Finding(kind="parse_error", severity="medium", path=rel,
                                             certainty=Certainty.INFERRED,
                                             message="file has syntax errors; imports read from the recoverable "
                                                     "parts only"))
            seen: set[tuple[str, str]] = set()
            for spec, line, type_only in _specifiers(tree.root_node):
                e = self._edge(rel, spec, line, type_only, known)
                if (e.target, e.kind.value) not in seen and e.target != rel:
                    seen.add((e.target, e.kind.value))
                    self.edges.append(e)
        self._dependency_findings()
        return self

    # ---- resolution ------------------------------------------------------------------------
    def _edge(self, rel: str, spec: str | None, line: int, type_only: bool, known: set[str]) -> Edge:
        if spec is None:
            self.findings.append(Finding(kind="dynamic_import", severity="info", path=rel, certainty=Certainty.UNKNOWN,
                                         message=f"line {line}: require()/import() with a computed argument cannot be "
                                                 "resolved statically"))
            return Edge(source=rel, target="<dynamic>", kind=EdgeKind.EXTERNAL_IMPORT, certainty=Certainty.UNKNOWN,
                        line=line)
        if spec.startswith((".", "/")):
            base = PurePosixPath(rel).parent / spec if not spec.startswith("/") else PurePosixPath(spec.lstrip("/"))
            target = self._resolve_path(base, known)
            if target is None and not self._is_asset(spec):
                self.findings.append(Finding(
                    kind="unresolved_import", severity="medium", path=rel,
                    message=f"line {line}: '{spec}' does not resolve to a file in the project"))
                return Edge(source=rel, target=spec, kind=EdgeKind.INTERNAL_IMPORT, certainty=Certainty.UNKNOWN,
                            line=line, type_only=type_only)
            if target is None:  # stylesheet, image, JSON...: a file dependency, not a module
                return Edge(source=rel, target=str(self._norm(base)), kind=EdgeKind.INTERNAL_IMPORT,
                            certainty=Certainty.OBSERVED, line=line, type_only=type_only)
            return Edge(source=rel, target=target, kind=EdgeKind.INTERNAL_IMPORT, certainty=Certainty.OBSERVED,
                        line=line, type_only=type_only)
        aliased = self._resolve_alias(spec, known)
        if aliased is not None:
            return Edge(source=rel, target=aliased, kind=EdgeKind.INTERNAL_IMPORT, certainty=Certainty.OBSERVED,
                        line=line, type_only=type_only)
        bare = spec.removeprefix("node:")
        top = bare.split("/", 1)[0]
        if spec.startswith("node:") or top in NODE_BUILTINS:
            return Edge(source=rel, target=f"node:{top}", kind=EdgeKind.STDLIB_IMPORT, certainty=Certainty.OBSERVED,
                        line=line, type_only=type_only)
        pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else top
        if not _NPM_NAME.match(pkg):
            self.findings.append(Finding(kind="unresolved_import", severity="medium", path=rel,
                                         certainty=Certainty.INFERRED,
                                         message=f"line {line}: '{spec}' is neither a project file nor a valid npm "
                                                 "package name (path alias not configured in tsconfig.json?)"))
            return Edge(source=rel, target=spec, kind=EdgeKind.EXTERNAL_IMPORT, certainty=Certainty.UNKNOWN,
                        line=line, type_only=type_only)
        return Edge(source=rel, target=pkg, kind=EdgeKind.EXTERNAL_IMPORT, certainty=Certainty.OBSERVED, line=line,
                    type_only=type_only)

    @staticmethod
    def _norm(p: PurePosixPath) -> PurePosixPath:
        parts: list[str] = []
        for part in p.parts:
            if part == "..":
                if parts:
                    parts.pop()
                else:
                    return PurePosixPath("..")  # escapes the project root
            elif part not in (".", ""):
                parts.append(part)
        return PurePosixPath(*parts) if parts else PurePosixPath(".")

    def _resolve_path(self, base: PurePosixPath, known: set[str]) -> str | None:
        b = self._norm(base)
        if b.parts and b.parts[0] == "..":
            return None
        s = str(b)
        cands = [s]
        stem, ext = (s[: -len(b.suffix)], b.suffix) if b.suffix else (s, "")
        if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TS sources imported with their compiled extension
            alt = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}[ext]
            cands += [stem + a for a in alt]
        cands += [s + e for e in JS_EXTS] + [f"{s}/index{e}" for e in JS_EXTS]
        return next((c for c in cands if c in known), None)

    def _resolve_alias(self, spec: str, known: set[str]) -> str | None:
        for pattern, targets in self._paths.items():
            if pattern.endswith("*") and spec.startswith(pattern[:-1]):
                rest = spec[len(pattern) - 1:]
            elif pattern == spec:
                rest = ""
            else:
                continue
            for t in targets:
                hit = self._resolve_path(self._base_url / t.replace("*", rest), known)
                if hit:
                    return hit
        if self._base_url_set:
            return self._resolve_path(self._base_url / spec, known)
        return None

    @staticmethod
    def _is_asset(spec: str) -> bool:
        return bool(re.search(r"\.(css|scss|sass|less|svg|png|jpe?g|gif|webp|json|woff2?|html)(\?.*)?$", spec))

    # ---- configuration -----------------------------------------------------------------------
    def _read_json(self, rel: str) -> Any:
        p = self.root / rel
        if rel not in self.files or p.is_symlink() or self.files[rel].size > 2_000_000:
            return None
        try:
            return json.loads(_strip_jsonc(p.read_text("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.findings.append(Finding(kind="invalid_json", severity="medium", path=rel,
                                         message=f"{rel} does not parse: {exc.__class__.__name__}"))
            return None

    def _tsconfig(self) -> tuple[PurePosixPath, dict[str, list[str]]]:
        self._base_url_set = False
        data = self._read_json("tsconfig.json") if "tsconfig.json" in self.files else None
        opts = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        if not isinstance(opts, dict):
            return PurePosixPath("."), {}
        base = opts.get("baseUrl")
        self._base_url_set = isinstance(base, str)
        raw = opts.get("paths")
        paths: dict[str, Any] = raw if isinstance(raw, dict) else {}
        clean = {k: [t for t in v if isinstance(t, str)] for k, v in paths.items() if isinstance(v, list)}
        return self._norm(PurePosixPath(base if isinstance(base, str) else ".")), clean

    def _package_json(self) -> None:
        for rel in sorted(r for r in self.files if PurePosixPath(r).name == "package.json"):
            data = self._read_json(rel)
            if not isinstance(data, dict):
                continue
            for key, group in DEP_GROUPS.items():
                deps = data.get(key)
                if isinstance(deps, dict):
                    for name, rng in sorted(deps.items()):
                        if isinstance(name, str) and _NPM_NAME.match(name):
                            self.declared.append(DeclaredDependency(name=name, spec=f"{name}@{rng}", source_file=rel,
                                                                    group=group, ecosystem="npm"))
            if self.test_framework is None:
                names = {d.name for d in self.declared if d.source_file == rel}
                script = str((data.get("scripts") or {}).get("test", "")) if isinstance(data.get("scripts"), dict) \
                    else ""
                self.test_framework = next((r for r in TEST_RUNNERS if r in names or r in script), None) or \
                    ("node-test" if "node --test" in script else None)

    def _dependency_findings(self) -> None:
        if not any(PurePosixPath(r).name == "package.json" for r in self.files):
            return
        declared = {d.name for d in self.declared}
        used = sorted({e.target for e in self.edges if e.kind is EdgeKind.EXTERNAL_IMPORT
                       and e.certainty is Certainty.OBSERVED})
        for pkg in used:
            if pkg not in declared:
                self.findings.append(Finding(
                    kind="undeclared_import", severity="medium", certainty=Certainty.OBSERVED,
                    message=f"npm package '{pkg}' is imported but not declared in package.json"))
        for d in sorted({d.name for d in self.declared if d.group == "main"} - set(used)):
            self.findings.append(Finding(kind="declared_not_imported", severity="info", certainty=Certainty.INFERRED,
                                         message=f"npm dependency '{d}' is not imported by scanned JS/TS code "
                                                 "(may be used by tooling or at runtime only)"))
