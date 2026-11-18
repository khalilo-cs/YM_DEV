"""Deterministic fixture projects for benchmarks (seeded; same seed -> byte-identical project)."""

from __future__ import annotations

import random
from pathlib import Path


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def renamed_api_project(root: Path, seed: int) -> dict[str, str]:
    """A project with a reproducible renamed-API defect. Returns metadata used to build the right fix."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixtures, not security
    old, new = f"compute_{rng.randint(100, 999)}", f"calc_{rng.randint(100, 999)}"
    pkg = f"app{rng.randint(10, 99)}"
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
