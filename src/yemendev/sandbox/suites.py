"""Which test suites a project has, and running all of them as one result (stage 22).

* Python (pytest) runs whenever the project is not a JavaScript/TypeScript-only project, exactly as before.
* JavaScript/TypeScript (``node:test``) runs when ``package.json`` exists and there are tests for it.
* A project with both gets both, merged into one report. The merged status is the *least* favourable one: a
  suite that could not run makes the whole result non-passing (never "passed because the other suite passed").
"""

from __future__ import annotations

from pathlib import Path

from yemendev.sandbox.node_runner import has_js_tests, run_node_tests
from yemendev.sandbox.testrunner import Outcome, RunStatus, TestRunReport, run_pytest
from yemendev.sandbox.workspace import Workspace

# least favourable first
_SEVERITY = [RunStatus.RUNNER_ERROR, RunStatus.TIMED_OUT, RunStatus.COLLECTION_ERROR, RunStatus.TESTS_FAILED,
             RunStatus.ALL_PASSED, RunStatus.NO_TESTS]
_PY_CONFIG = ("pytest.ini", "conftest.py", "tox.ini", "setup.cfg")


def _has_python_tests(root: Path) -> bool:
    if any((root / n).is_file() for n in _PY_CONFIG):
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "[tool.pytest" in pyproject.read_text("utf-8", "replace"):
        return True
    for f in root.rglob("*.py"):
        parts = f.relative_to(root).parts
        if "node_modules" in parts or any(p.startswith(".") for p in parts):
            continue
        if f.name.startswith("test_") or f.name.endswith("_test.py"):
            return True
    return False


def detect_suites(root: Path) -> list[str]:
    if not has_js_tests(root):
        return ["pytest"]  # unchanged behaviour for every non-JS project
    return (["pytest"] if _has_python_tests(root) else []) + ["node"]


def merge(reports: dict[str, TestRunReport]) -> TestRunReport:
    if len(reports) == 1:
        return next(iter(reports.values()))
    items = list(reports.items())
    present = [r.status for _, r in items]
    nonempty = [s for s in present if s is not RunStatus.NO_TESTS] or present
    status = min(nonempty, key=_SEVERITY.index)
    cases = [c for _, r in items for c in r.cases]
    counts = {o: sum(1 for c in cases if c.outcome is o) for o in Outcome}
    notes = [f"{name}: {r.status.value}" + (f" ({r.note})" if r.note else "") for name, r in items]
    first = min(items, key=lambda kv: _SEVERITY.index(kv[1].status))[1]
    return TestRunReport(
        status=status, total=len(cases), passed=counts[Outcome.PASSED], failed=counts[Outcome.FAILED],
        errors=counts[Outcome.ERROR], skipped=counts[Outcome.SKIPPED], cases=cases, execution=first.execution,
        note="suites - " + "; ".join(notes))


def run_tests(ws: Workspace, timeout: float | None = None) -> TestRunReport:
    """Run every suite the project has in the workspace and return one report."""
    runners = {"pytest": lambda: run_pytest(ws, timeout=timeout), "node": lambda: run_node_tests(ws, timeout)}
    return merge({name: runners[name]() for name in detect_suites(ws.root)})
