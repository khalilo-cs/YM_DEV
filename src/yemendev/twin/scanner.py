"""Repository Intelligence: safe, read-only, offline project scanner.

Security properties:
* never follows symlinks (they are recorded and skipped);
* never reads contents of files that look like secrets (.env, keys) - presence only;
* every path is resolved and confirmed to be inside the project root;
* bounded: max files and max bytes parsed per file;
* never executes project code (Python is parsed with ``ast``, JavaScript/TypeScript with Tree-sitter
  - see ``js_scanner.py`` -, TOML with ``tomllib``, JSON with ``json``).
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sys
import tomllib
from pathlib import Path

from yemendev.core.contracts import InvalidInputError, sha256_text
from yemendev.security.secret_scan import scan_text
from yemendev.twin.js_scanner import JsAnalysis, is_js_path
from yemendev.twin.model import (
    Certainty,
    DeclaredDependency,
    Edge,
    EdgeKind,
    FileInfo,
    Finding,
    ModuleInfo,
    ProjectSnapshot,
)

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", ".eggs", ".idea", ".vscode", ".yemendev",
}
LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript", ".jsx": "javascript",
    ".rs": "rust", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".sh": "shell", ".md": "markdown", ".rst": "restructuredtext",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".sql": "sql", ".html": "html",
    ".css": "css", ".cfg": "ini", ".ini": "ini", ".txt": "text",
}
SENSITIVE_NAMES = re.compile(
    r"(^\.env(\..*)?$|\.pem$|\.key$|^id_(rsa|dsa|ecdsa|ed25519)$|\.p12$|\.pfx$|^credentials(\.json)?$|\.keystore$)",
    re.IGNORECASE,
)
# Common import-name -> distribution-name differences. A match is still only "inferred".
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "jwt": "pyjwt", "dotenv": "python-dotenv", "google": "protobuf",
    "Crypto": "pycryptodome", "attr": "attrs", "pkg_resources": "setuptools", "OpenSSL": "pyopenssl",
}
MAX_FILES = 50_000
# Lockfiles are large, machine-written and full of hashes: not worth secret-scanning.
_NO_SECRET_SCAN = re.compile(r"(\.lock|-lock\.json|-lock\.yaml|\.min\.js|\.map)$")
MAX_PARSE_BYTES = 2_000_000


def normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_name(spec: str) -> str | None:
    m = _REQ_NAME.match(spec)
    return normalize_dist(m.group(1)) if m else None


class ProjectScanner:
    def __init__(self, max_files: int = MAX_FILES) -> None:
        self.max_files = max_files

    def scan(self, root: Path) -> ProjectSnapshot:
        root = root.resolve()
        if not root.is_dir():
            raise InvalidInputError("project root is not a directory")
        files: list[FileInfo] = []
        skipped: list[str] = []
        sensitive: list[str] = []
        findings: list[Finding] = []
        sources: dict[str, str] = {}
        js_sources: dict[str, bytes] = {}
        secrets_suppressed = 0

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            keep = []
            for name in sorted(dirnames):
                p = d / name
                if name in SKIP_DIRS or name.endswith(".egg-info"):
                    continue
                if p.is_symlink():
                    skipped.append(f"{p.relative_to(root)} (symlinked directory)")
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in sorted(filenames):
                p = d / name
                rel = p.relative_to(root).as_posix()
                if p.is_symlink():
                    skipped.append(f"{rel} (symlink)")
                    continue
                if not p.resolve().is_relative_to(root):
                    skipped.append(f"{rel} (outside root)")
                    continue
                if SENSITIVE_NAMES.search(name):
                    sensitive.append(rel)
                    continue
                if len(files) >= self.max_files:
                    findings.append(Finding(kind="scan_truncated", severity="medium",
                                            message=f"file limit {self.max_files} reached; scan incomplete"))
                    break
                try:
                    data = p.read_bytes()
                except OSError as exc:
                    skipped.append(f"{rel} (unreadable: {exc.__class__.__name__})")
                    continue
                lang = LANGUAGES.get(p.suffix.lower(), "other")
                fi = FileInfo(path=rel, language=lang, size=len(data), sha256=hashlib.sha256(data).hexdigest())
                if lang == "python" and len(data) <= MAX_PARSE_BYTES:
                    try:
                        sources[rel] = data.decode("utf-8")
                    except UnicodeDecodeError:
                        fi.parse_error = "not valid UTF-8"
                elif is_js_path(rel) and len(data) <= MAX_PARSE_BYTES:
                    js_sources[rel] = data
                if not _NO_SECRET_SCAN.search(name):
                    hits, suppressed = scan_text(data)
                    secrets_suppressed += suppressed
                    findings.extend(Finding(kind="secret_in_source", severity=h.severity, path=rel,
                                            certainty=Certainty.INFERRED,
                                            message=f"line {h.line}: possible {h.description} ({h.rule}); "
                                                    "value not shown") for h in hits)
                files.append(fi)

        file_index = {f.path: f for f in files}
        source_roots = [r for r in ("src", "") if r == "" or (root / r).is_dir()]
        modules = self._modules(sources.keys(), source_roots)
        module_names = {m.name for m in modules}
        edges: list[Edge] = []
        for m in modules:
            fi = file_index[m.path]
            try:
                tree = ast.parse(sources[m.path], filename=m.path)
                fi.parsed = True
            except (SyntaxError, ValueError) as exc:
                fi.parse_error = f"{exc.__class__.__name__}: {getattr(exc, 'msg', str(exc))}"
                findings.append(Finding(kind="syntax_error", severity="high", path=m.path,
                                        message=f"Python file does not parse: {fi.parse_error}"))
                continue
            edges.extend(self._imports(tree, m, module_names))

        declared = self._declared_deps(root, findings)
        findings.extend(self._dependency_findings(edges, declared))  # Python imports vs PyPI declarations
        js = JsAnalysis(root, js_sources, files).run()
        if module_names & {m.name for m in js.modules}:  # cannot happen with path-style names; be explicit
            raise InvalidInputError("module name collision between Python and JS/TS modules")
        modules += js.modules
        edges += js.edges
        findings += js.findings
        declared += js.declared
        langs: dict[str, int] = {}
        for f in files:
            langs[f.language] = langs.get(f.language, 0) + 1
        tree_hash = sha256_text("\n".join(f"{f.path}\0{f.sha256}" for f in sorted(files, key=lambda f: f.path)))
        test_framework = self._test_framework(root, files, declared) or js.test_framework
        if secrets_suppressed:
            findings.append(Finding(kind="secret_scan_suppressed", severity="info", certainty=Certainty.OBSERVED,
                                    message=f"{secrets_suppressed} line(s) marked 'yemendev:allow-secret' were not "
                                            "reported"))
        if sensitive:
            findings.append(Finding(kind="sensitive_files_present", severity="medium",
                                    message=f"{len(sensitive)} file(s) look like secrets; contents not read",
                                    certainty=Certainty.INFERRED))
        return ProjectSnapshot(
            root=str(root), tree_hash=tree_hash, files=files, languages=langs, modules=modules, edges=edges,
            declared_dependencies=declared, findings=findings, skipped=skipped, sensitive_files=sensitive,
            test_framework=test_framework,
        )

    @staticmethod
    def _modules(paths: object, source_roots: list[str]) -> list[ModuleInfo]:
        out: list[ModuleInfo] = []
        for rel in sorted(paths):  # type: ignore[call-overload]
            if not rel.endswith(".py"):
                continue
            base = rel
            for sr in source_roots:
                if sr and rel.startswith(sr + "/"):
                    base = rel[len(sr) + 1:]
                    break
            parts = base[:-3].split("/")
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            if not parts or not all(p.isidentifier() for p in parts):
                continue
            name = ".".join(parts)
            leaf = Path(rel).name
            is_test = leaf.startswith("test_") or leaf.endswith("_test.py") or "tests" in parts or leaf == "conftest.py"
            out.append(ModuleInfo(name=name, path=rel, is_package=is_pkg, is_test=is_test))
        return out

    @staticmethod
    def _imports(tree: ast.AST, m: ModuleInfo, internal: set[str]) -> list[Edge]:
        edges: list[Edge] = []
        pkg_parts = m.name.split(".") if m.is_package else m.name.split(".")[:-1]

        def resolve(name: str, line: int) -> None:
            top = name.split(".")[0]
            cand = name
            while cand and cand not in internal:
                cand = cand.rpartition(".")[0]
            if cand:
                if cand != m.name:
                    edges.append(Edge(source=m.name, target=cand, kind=EdgeKind.INTERNAL_IMPORT,
                                      certainty=Certainty.OBSERVED, line=line))
            elif top in sys.stdlib_module_names:
                edges.append(Edge(source=m.name, target=top, kind=EdgeKind.STDLIB_IMPORT,
                                  certainty=Certainty.OBSERVED, line=line))
            else:
                edges.append(Edge(source=m.name, target=top, kind=EdgeKind.EXTERNAL_IMPORT,
                                  certainty=Certainty.OBSERVED, line=line))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    resolve(a.name, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(pkg_parts):
                        # Relative import beyond the top-level package: cannot be resolved.
                        edges.append(Edge(source=m.name, target=f"{'.' * node.level}{node.module or ''}",
                                          kind=EdgeKind.INTERNAL_IMPORT, certainty=Certainty.UNKNOWN,
                                          line=node.lineno))
                        continue
                    base_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    base = ".".join([*base_parts, *( [node.module] if node.module else [])])
                    for a in node.names:
                        full = f"{base}.{a.name}" if base else a.name
                        if full in internal:
                            resolve(full, node.lineno)
                        elif base:
                            resolve(base, node.lineno)
                else:
                    mod = node.module or ""
                    for a in node.names:
                        full = f"{mod}.{a.name}"
                        resolve(full if full in internal else mod, node.lineno)
        # de-duplicate (keep first line)
        seen: set[tuple[str, str, str]] = set()
        uniq = []
        for e in edges:
            k = (e.source, e.target, e.kind.value)
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        return uniq

    @staticmethod
    def _declared_deps(root: Path, findings: list[Finding]) -> list[DeclaredDependency]:
        deps: list[DeclaredDependency] = []
        pp = root / "pyproject.toml"
        if pp.is_file() and not pp.is_symlink():
            try:
                data = tomllib.loads(pp.read_text("utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                findings.append(Finding(kind="invalid_pyproject", severity="high", path="pyproject.toml",
                                        message=f"pyproject.toml does not parse: {exc}"))
                data = {}
            proj = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
            for spec in proj.get("dependencies", []) or []:
                if isinstance(spec, str) and (n := _req_name(spec)):
                    deps.append(DeclaredDependency(name=n, spec=spec, source_file="pyproject.toml"))
            for group, specs in (proj.get("optional-dependencies") or {}).items():
                for spec in specs or []:
                    if isinstance(spec, str) and (n := _req_name(spec)):
                        deps.append(DeclaredDependency(name=n, spec=spec, source_file="pyproject.toml", group=group))
        for req in sorted(root.glob("requirements*.txt")):
            if req.is_symlink():
                continue
            try:
                lines = req.read_text("utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            group = "dev" if "dev" in req.name or "test" in req.name else "main"
            for line in lines:
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                if n := _req_name(line):
                    deps.append(DeclaredDependency(name=n, spec=line, source_file=req.name, group=group))
        return deps

    @staticmethod
    def _dependency_findings(edges: list[Edge], declared: list[DeclaredDependency]) -> list[Finding]:
        out: list[Finding] = []
        declared_names = {d.name for d in declared if d.ecosystem == "pypi"}
        used_external = sorted({e.target for e in edges if e.kind is EdgeKind.EXTERNAL_IMPORT})
        matched: set[str] = set()
        for imp in used_external:
            direct = normalize_dist(imp)
            if direct in declared_names:
                matched.add(direct)
                continue
            guess = IMPORT_TO_DIST.get(imp)
            if guess and normalize_dist(guess) in declared_names:
                matched.add(normalize_dist(guess))
                continue
            if declared_names:
                out.append(Finding(kind="undeclared_import", severity="medium", certainty=Certainty.INFERRED,
                                   message=f"import '{imp}' has no matching declared dependency"
                                           " (import and distribution names may differ)"))
        for d in sorted(declared_names - matched):
            out.append(Finding(kind="declared_not_imported", severity="info", certainty=Certainty.INFERRED,
                               message=f"declared dependency '{d}' is not imported by scanned Python code"
                                       " (may be a plugin, tool, or runtime-only dependency)"))
        return out

    @staticmethod
    def _test_framework(root: Path, files: list[FileInfo], declared: list[DeclaredDependency]) -> str | None:
        names = {d.name for d in declared if d.ecosystem == "pypi"}
        has_tests = any(Path(f.path).name.startswith("test_") and f.path.endswith(".py") for f in files)
        if "pytest" in names or (root / "pytest.ini").is_file() or (root / "conftest.py").is_file() or has_tests:
            return "pytest"
        return None
