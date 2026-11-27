"""Deterministic fixture projects for benchmarks (seeded; same seed -> byte-identical project)."""

from __future__ import annotations

import random
from pathlib import Path


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


FIXTURES_VERSION = "3"  # bump when any generator below changes (recorded in benchmark reports)


def renamed_api_project(root: Path, seed: int, names: tuple[str, str] | None = None,
                        package: str | None = None) -> dict[str, str]:
    """A project with a reproducible renamed-API defect. Returns metadata used to build the right fix.

    ``names`` and ``package`` pin the (old, new) API names and the module that defines them, so two
    *different* applications can use the same library and receive the same upstream change (skill
    transfer); everything else still varies with the seed."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixtures, not security
    old, new = f"compute_{rng.randint(100, 999)}", f"calc_{rng.randint(100, 999)}"
    if names:
        old, new = names
    pkg = f"app{rng.randint(10, 99)}"
    if package:
        pkg = package
    a, b = rng.randint(2, 9), rng.randint(2, 9)
    _w(root, f"{pkg}/__init__.py", "")
    _w(root, f"{pkg}/core.py", f"def {new}(x, y):\n    return x + y\n\n\ndef scale(x, k):\n    return x * k\n")
    _w(root, f"{pkg}/service.py",
       f"from {pkg} import core\n\n\ndef total(xs):\n    t = 0\n    for x in xs:\n        t = core.{old}(t, x)\n"
       f"    return t\n\n\ndef scaled(x):\n    return core.scale(x, {a})\n")
    _w(root, f"{pkg}/extra.py", "VERSION = 1\n")
    _w(root, "tests/test_service.py",
       f"from {pkg}.service import scaled, total\n\n\ndef test_total():\n    assert total([1, 2, {b}]) == {3 + b}\n\n\n"
       f"def test_scaled():\n    assert scaled({b}) == {a * b}\n")
    return {"package": pkg, "old": old, "new": new, "service": f"{pkg}/service.py", "extra": f"{pkg}/extra.py"}


def legacy_project(root: Path, seed: int) -> dict[str, str]:
    """Old-style layout: setup.py + requirements.txt, ``class X(object)``, %-formatting, relative imports,
    unittest-style tests - and the same renamed-API defect."""
    rng = random.Random(seed)  # noqa: S311
    old, new = f"add_{rng.randint(100, 999)}", f"plus_{rng.randint(100, 999)}"
    pkg = f"legacy{rng.randint(10, 99)}"
    _w(root, "setup.py", f"from setuptools import setup\n\nsetup(name='{pkg}', version='0.9', packages=['{pkg}'])\n")
    _w(root, "requirements.txt", "# pinned for the 2015 deployment\nsimplejson==3.8.0\n")
    _w(root, f"{pkg}/__init__.py", "from .ledger import *  # noqa\n")
    _w(root, f"{pkg}/core.py", f"def {new}(a, b):\n    return a + b\n")
    _w(root, f"{pkg}/ledger.py",
       f"from . import core\n\n\nclass Ledger(object):\n    def __init__(self):\n        self.items = []\n\n"
       f"    def add(self, x):\n        self.items.append(x)\n\n    def total(self):\n        t = 0\n"
       f"        for x in self.items:\n            t = core.{old}(t, x)\n        return t\n\n"
       f"    def report(self):\n        return 'total=%d' % self.total()\n")
    _w(root, "tests/test_ledger.py",
       f"import unittest\n\nfrom {pkg}.ledger import Ledger\n\n\nclass LedgerTest(unittest.TestCase):\n"
       f"    def test_total(self):\n        l = Ledger()\n        l.add(2)\n        l.add(3)\n"
       f"        self.assertEqual(l.report(), 'total=5')\n")
    return {"package": pkg, "old": old, "new": new, "service": f"{pkg}/ledger.py", "extra": f"{pkg}/core.py"}


def multilingual_project(root: Path, seed: int) -> dict[str, str]:
    """Arabic identifiers, docstrings, comments, test names and documentation (Unicode end to end)."""
    rng = random.Random(seed)  # noqa: S311
    n = rng.randint(10, 99)
    old, new = f"اجمع_{n}", f"احسب_المجموع_{n}"
    pkg = f"hisab{n}"
    _w(root, "README.ar.md", "# الحساب\n\nمكتبة صغيرة لحساب المجاميع. الواجهة البرمجية تغيّرت في الإصدار الثاني.\n")
    _w(root, f"{pkg}/__init__.py", "")
    _w(root, f"{pkg}/core.py", f'def {new}(أ, ب):\n    """يجمع عددين."""\n    return أ + ب\n')
    _w(root, f"{pkg}/service.py",
       f'from {pkg} import core\n\n\ndef المجموع(قيم):\n    """مجموع قائمة من الأعداد."""\n    ن = 0\n'
       f"    for ق in قيم:\n        ن = core.{old}(ن, ق)  # تعطّل بعد تغيير اسم الدالة\n    return ن\n")
    _w(root, "tests/test_service.py",
       f"from {pkg}.service import المجموع\n\n\ndef test_المجموع_الصحيح():\n    assert المجموع([1, 2, 3]) == 6\n")
    return {"package": pkg, "old": old, "new": new, "service": f"{pkg}/service.py", "extra": f"{pkg}/core.py"}


def memory_hungry_project(root: Path, seed: int) -> dict[str, str]:
    """A test that needs far more memory than the sandbox allows."""
    _w(root, "pkg/__init__.py", "")
    _w(root, "pkg/m.py", "def f():\n    return 1\n")
    _w(root, "tests/test_big.py", f"def test_big():\n    block = bytearray({1536 + seed % 7} * 1024 * 1024)\n"
                                  "    assert len(block) > 0\n")
    return {"service": "pkg/m.py"}
