"""JavaScript/TypeScript understanding with Tree-sitter (stage 18a)."""

import json
from pathlib import Path

import pytest
from helpers import make_passing_project, write

from yemendev.twin import js_scanner
from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import Certainty, EdgeKind
from yemendev.twin.scanner import ProjectScanner


def make_ts_project(root: Path) -> Path:
    write(root, "package.json", '{"name": "app", "scripts": {"test": "vitest run"},'
                                ' "dependencies": {"react": "^18.3.1", "left-pad": "1.3.0"},'
                                ' "devDependencies": {"vitest": "^2.0.0", "typescript": "5.9.3"}}')
    write(root, "tsconfig.json", '// comments and trailing commas are allowed here\n'
                                 '{"compilerOptions": {"baseUrl": ".", /* alias */ "paths": {"@/*": ["src/*"],},},}')
    write(root, "src/index.ts",
          'import { add } from "./math";\n'
          'import type { T } from "./types";\n'
          'import React from "react";\n'
          'import fs from "node:fs";\n'
          'import path from "path";\n'
          'import "./styles.css";\n'
          'export * from "./util/index.js";\n'
          'import { cfg } from "@/config";\n'
          'const lodash = require("lodash");\n'
          'const dyn = await import(modName);\n'
          'import { x } from "../../outside";\n')
    write(root, "src/math.ts", "export const add = (a: number, b: number) => a + b;\n")
    write(root, "src/types.ts", "export type T = number;\n")
    write(root, "src/util/index.ts", "export const u = 1;\n")
    write(root, "src/config.ts", "export const cfg = {};\n")
    write(root, "src/styles.css", "body { margin: 0 }\n")
    write(root, "src/App.tsx", 'import { add } from "./math";\nexport const App = () => <div>{add(1, 2)}</div>;\n')
    write(root, "src/legacy.cjs", "const m = require('./math');\nmodule.exports = m;\n")
    write(root, "src/broken.js", 'import { y } from "./missing";\nfunction (( {\n')
    write(root, "tests/math.test.ts", 'import { add } from "../src/math";\nimport { describe } from "vitest";\n')
    write(root, "node_modules/react/index.js", 'require("should-not-be-scanned");\n')
    return root


@pytest.fixture
def snap(tmp_path):
    return ProjectScanner().scan(make_ts_project(tmp_path / "app"))


def edges_from(snap, source):
    return {e.target: e for e in snap.edges if e.source == source}


def test_modules_languages_and_tests(snap):
    mods = {m.name: m for m in snap.modules}
    assert mods["src/index.ts"].language == "typescript" and mods["src/App.tsx"].language == "typescript"
    assert mods["src/legacy.cjs"].language == "javascript"
    assert mods["tests/math.test.ts"].is_test and not mods["src/math.ts"].is_test
    assert not any(n.startswith("node_modules/") for n in mods)  # dependencies are never scanned
    assert snap.languages["typescript"] == 7 and snap.test_framework == "vitest"


def test_every_import_form_is_extracted_and_resolved(snap):
    e = edges_from(snap, "src/index.ts")
    for target in ("src/math.ts", "src/types.ts", "src/util/index.ts", "src/config.ts"):
        assert e[target].kind is EdgeKind.INTERNAL_IMPORT and e[target].certainty is Certainty.OBSERVED, target
    assert e["src/types.ts"].type_only and not e["src/math.ts"].type_only
    assert e["src/config.ts"].line == 8  # resolved through tsconfig "paths": {"@/*": ["src/*"]}
    assert e["node:fs"].kind is EdgeKind.STDLIB_IMPORT and e["node:path"].kind is EdgeKind.STDLIB_IMPORT
    assert e["react"].kind is EdgeKind.EXTERNAL_IMPORT and e["lodash"].kind is EdgeKind.EXTERNAL_IMPORT
    assert e["<dynamic>"].certainty is Certainty.UNKNOWN
    assert "src/styles.css" in e  # asset imports are file dependencies
    assert e["../../outside"].certainty is Certainty.UNKNOWN  # escaping the root never resolves
    assert edges_from(snap, "src/legacy.cjs")["src/math.ts"].certainty is Certainty.OBSERVED
    assert edges_from(snap, "tests/math.test.ts")["src/math.ts"].kind is EdgeKind.INTERNAL_IMPORT


def test_findings_are_specific_and_honest(snap):
    by_kind = {}
    for f in snap.findings:
        by_kind.setdefault(f.kind, []).append(f)
    assert [f.message for f in by_kind["undeclared_import"]] == [
        "npm package 'lodash' is imported but not declared in package.json"]
    assert [f.message for f in by_kind["declared_not_imported"]] == [
        "npm dependency 'left-pad' is not imported by scanned JS/TS code (may be used by tooling or at runtime only)"]
    unresolved = {(f.path, f.message.split("'")[1]) for f in by_kind["unresolved_import"]}
    assert unresolved == {("src/broken.js", "./missing"), ("src/index.ts", "../../outside")}
    assert [f.path for f in by_kind["parse_error"]] == ["src/broken.js"]
    assert by_kind["dynamic_import"][0].certainty is Certainty.UNKNOWN
    broken = next(f for f in snap.files if f.path == "src/broken.js")
    assert broken.parsed and broken.parse_error  # error-tolerant: imports still read


def test_declared_npm_dependencies(snap):
    deps = {d.name: d for d in snap.declared_dependencies}
    assert deps["react"].ecosystem == "npm" and deps["react"].group == "main" and deps["react"].spec == "react@^18.3.1"
    assert deps["vitest"].group == "dev"


def test_graph_impact_crosses_files(snap):
    g = DependencyGraph.from_snapshot(snap)
    assert g.impacted_by("src/math.ts") == {"src/index.ts", "src/App.tsx", "src/legacy.cjs", "tests/math.test.ts"}


def test_python_and_npm_dependencies_do_not_mix(tmp_path):
    root = make_passing_project(tmp_path / "mixed")
    write(root, "pyproject.toml", '[project]\nname = "m"\ndependencies = ["requests"]\n')
    write(root, "pkg/net.py", "import requests\n")
    write(root, "package.json", '{"dependencies": {"requests": "1.0.0"}}')
    write(root, "web/app.js", 'import x from "react";\n')
    snap = ProjectScanner().scan(root)
    msgs = [f.message for f in snap.findings if f.kind in ("undeclared_import", "declared_not_imported")]
    assert "npm package 'react' is imported but not declared in package.json" in msgs
    # the PyPI 'requests' is used by Python; the npm 'requests' is not imported by JS - judged separately
    assert not any("declared dependency 'requests'" in m for m in msgs)
    assert any(m.startswith("npm dependency 'requests'") for m in msgs)
    assert snap.test_framework == "pytest"


def test_an_npm_declaration_never_satisfies_or_pollutes_python_findings(tmp_path):
    root = make_passing_project(tmp_path / "mixed")
    write(root, "pyproject.toml", '[project]\nname = "m"\ndependencies = ["requests"]\n')
    write(root, "pkg/conf.py", "import requests\nimport yaml\n")  # yaml (PyYAML) is NOT declared for Python
    write(root, "package.json", '{"dependencies": {"yaml": "2.4.0", "react": "18.3.1"}}')
    write(root, "web/app.js", 'import x from "react";\nimport y from "yaml";\n')
    msgs = [f.message for f in ProjectScanner().scan(root).findings]
    assert any(m.startswith("import 'yaml' has no matching declared dependency") for m in msgs)
    assert not any("declared dependency 'react'" in m for m in msgs)  # an npm package is not a Python dep


def test_without_grammars_the_scan_degrades_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(js_scanner, "_languages", lambda: None)
    snap = ProjectScanner().scan(make_ts_project(tmp_path / "app"))
    assert not any(m.language != "python" for m in snap.modules)
    assert any(f.kind == "js_ts_analysis_unavailable" for f in snap.findings)
    assert snap.languages["typescript"] == 7  # files are still listed


def test_jsonc_stripping_keeps_strings_intact():
    text = '{"a": "http://x//not-a-comment", /* c */ "b": [1, 2,], // tail\n}'
    assert json.loads(js_scanner._strip_jsonc(text)) == {"a": "http://x//not-a-comment", "b": [1, 2]}


def test_resolution_never_reads_the_filesystem_outside_known_files(tmp_path):
    secret = tmp_path / "secret.ts"
    secret.write_text("export const s = 1;\n")
    root = tmp_path / "proj"
    write(root, "a.ts", 'import { s } from "../secret";\n')
    snap = ProjectScanner().scan(root)
    assert [e.certainty for e in snap.edges] == [Certainty.UNKNOWN]
    assert all("secret" not in m.name for m in snap.modules)
