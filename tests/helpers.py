"""Fixture projects used across tests (real, runnable Python projects)."""

from __future__ import annotations

from pathlib import Path


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def make_renamed_api_project(root: Path) -> Path:
    """A project whose code calls ``mathutils.add_numbers`` but the API was renamed to ``add``.

    Reproducible, deterministic failure; the correct repair is a rename at the call site.
    """
    write(root, "pyproject.toml", '[project]\nname="calc"\nversion="0.1"\n'
                                  '[project.optional-dependencies]\ndev=["pytest"]\n')
    write(root, "calc/__init__.py", "")
    write(root, "calc/mathutils.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    write(root, "calc/api.py",
          "from calc import mathutils\n\n\ndef total(xs):\n    t = 0\n    for x in xs:\n"
          "        t = mathutils.add_numbers(t, x)\n    return t\n\n\ndef product(xs):\n    p = 1\n"
          "    for x in xs:\n        p = mathutils.mul(p, x)\n    return p\n")
    write(root, "tests/test_api.py",
          "from calc.api import product, total\n\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n\n\n"
          "def test_product():\n    assert product([2, 4]) == 8\n")
    return root


def make_passing_project(root: Path) -> Path:
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/m.py", "def f():\n    return 1\n")
    write(root, "tests/test_m.py", "from pkg.m import f\n\n\ndef test_f():\n    assert f() == 1\n")
    return root


def make_js_renamed_export_project(root: Path) -> Path:
    """An ES-module JavaScript project whose service imports an export that was renamed (``add`` ->
    ``addNumbers``), with a JavaScript and a TypeScript test file (node:test, no dependencies)."""
    write(root, "package.json", '{"name": "calc", "version": "1.0.0", "type": "module", '
                                '"scripts": {"test": "node --test"}}\n')
    write(root, "src/core.js", "export function addNumbers(a, b) {\n  return a + b;\n}\n\n"
                               "export function scale(x, k) {\n  return x * k;\n}\n")
    write(root, "src/service.js", 'import { add, scale } from "./core.js";\n\n'
                                  "export function total(xs) {\n  return xs.reduce((s, x) => add(s, x), 0);\n}\n\n"
                                  "export function doubled(x) {\n  return scale(x, 2);\n}\n")
    write(root, "test/service.test.js", 'import { test, describe } from "node:test";\n'
                                        'import assert from "node:assert/strict";\n'
                                        'import { total, doubled } from "../src/service.js";\n\n'
                                        'describe("service", () => {\n'
                                        '  test("total sums", () => assert.equal(total([1, 2, 3]), 6));\n'
                                        '  test("doubled", () => assert.equal(doubled(4), 8));\n'
                                        "});\n")
    write(root, "test/util.test.ts", 'import { test } from "node:test";\n'
                                     'import assert from "node:assert/strict";\n\n'
                                     "const half = (x: number): number => x / 2;\n"
                                     'test("typescript runs", () => assert.equal(half(8), 4));\n')
    return root
