"""Counterfactual scenarios: "what if <fault>?" answered by *running* the project's tests on a sandbox copy
with the fault injected, never by guessing. The real project is never modified.

Each run reports: tests that newly fail, whether any test detects the fault at all (a test **blind spot**
otherwise), and how well the static import graph predicted the impact (precision / recall) - a direct
measurement of the platform's own static analysis.
"""

from __future__ import annotations

import keyword
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import InvalidInputError
from yemendev.learning.skills import is_test_path
from yemendev.sandbox.testrunner import Outcome, RunStatus, TestRunReport, run_pytest
from yemendev.sandbox.workspace import Workspace
from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import ProjectSnapshot


class ModuleUnavailable(BaseModel):
    kind: Literal["module_unavailable"] = "module_unavailable"
    module: str = Field(min_length=1, max_length=300)


class DependencyRemoved(BaseModel):
    kind: Literal["dependency_removed"] = "dependency_removed"
    import_name: str = Field(min_length=1, max_length=100)

    @field_validator("import_name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not v.isidentifier() or keyword.iskeyword(v):
            raise ValueError("import_name must be a top-level Python identifier")
        return v


class FileCorrupted(BaseModel):
    kind: Literal["file_corrupted"] = "file_corrupted"
    path: str = Field(min_length=1, max_length=1000)
    mode: Literal["truncate", "garbage"] = "truncate"


Fault = Annotated[ModuleUnavailable | DependencyRemoved | FileCorrupted, Field(discriminator="kind")]


class Scenario(BaseModel):
    name: str = Field(default="what-if", max_length=200)
    faults: list[Fault] = Field(min_length=1, max_length=10)


class CounterfactualResult(BaseModel):
    scenario: Scenario
    baseline_status: str
    outcome_status: str
    newly_failing: list[str]
    still_passing: int
    detected: bool
    blind_spot: bool
    predicted_impacted_tests: list[str] | None
    static_precision: float | None
    static_recall: float | None
    notes: list[str]
    limitations: list[str]


def _test_module(test_id: str, test_modules: set[str]) -> str | None:
    head = test_id.split("::", 1)[0]
    while head:
        if head in test_modules:
            return head
        head = head.rpartition(".")[0]
    return None


def _apply(ws: Workspace, fault: ModuleUnavailable | DependencyRemoved | FileCorrupted, snap: ProjectSnapshot) -> str:
    if isinstance(fault, ModuleUnavailable):
        mod = next((m for m in snap.modules if m.name == fault.module and not m.is_test), None)
        if mod is None:
            raise InvalidInputError(f"module {fault.module!r} not found among non-test modules")
        ws.write(mod.path, f'raise ImportError("counterfactual: module {fault.module} unavailable")\n'.encode())
        return f"{mod.path} replaced by a module that raises ImportError"
    if isinstance(fault, DependencyRemoved):
        if any(m.name.split(".")[0] == fault.import_name for m in snap.modules):
            raise InvalidInputError("import_name is an internal package; use module_unavailable instead")
        # A package in the project root shadows the installed one (cwd is first on sys.path under `python -m`).
        ws.write(f"{fault.import_name}/__init__.py",
                 f'raise ImportError("counterfactual: dependency {fault.import_name} removed")\n'.encode())
        return f"import of {fault.import_name!r} made to fail"
    if is_test_path(fault.path):
        raise InvalidInputError("corrupting test files is not a meaningful scenario")
    target = ws.path(fault.path)
    if not target.is_file():
        raise InvalidInputError(f"file {fault.path!r} not found")
    ws.write(fault.path, b"" if fault.mode == "truncate" else b"\x00\xff{corrupted:\n")
    return f"{fault.path} {fault.mode}d"


def run_counterfactual(root: object, snap: ProjectSnapshot, scenario: Scenario, sandbox: SandboxSettings,
                       workspaces_dir: object = None) -> CounterfactualResult:
    from pathlib import Path

    base_dir = Path(str(workspaces_dir)) if workspaces_dir else None
    # Keep running the other test modules when one fails to import, so impact is measured per test
    # (regression: one broken import made pytest stop and every test was counted as affected).
    args = ["--continue-on-collection-errors"]
    with Workspace(Path(str(root)), sandbox, base_dir=base_dir) as ws:
        baseline = run_pytest(ws, extra_args=args)
    notes: list[str] = []
    with Workspace(Path(str(root)), sandbox, base_dir=base_dir) as ws:
        for f in scenario.faults:
            notes.append(_apply(ws, f, snap))
        after = run_pytest(ws, extra_args=args)
    return _compare(scenario, snap, baseline, after, notes)


def _compare(scenario: Scenario, snap: ProjectSnapshot, baseline: TestRunReport, after: TestRunReport,
             notes: list[str]) -> CounterfactualResult:
    limitations = ["impact is observed through the project's own tests only",
                   "one fault configuration per run; no timing or load effects"]
    ok_runs = (RunStatus.ALL_PASSED, RunStatus.TESTS_FAILED)
    if baseline.status not in ok_runs:
        return CounterfactualResult(
            scenario=scenario, baseline_status=baseline.status.value, outcome_status=after.status.value,
            newly_failing=[], still_passing=0, detected=False, blind_spot=False, predicted_impacted_tests=None,
            static_precision=None, static_recall=None, notes=[*notes, "baseline run did not complete; no conclusion"],
            limitations=limitations)
    b = baseline.outcomes()
    passing_before = {t for t, o in b.items() if o is Outcome.PASSED}
    if after.status in ok_runs or (after.status is RunStatus.COLLECTION_ERROR and after.cases):
        a = after.outcomes()
        newly = sorted(t for t in passing_before if a.get(t) is not Outcome.PASSED)  # failed, errored or not run
        still = sum(1 for t in passing_before if a.get(t) is Outcome.PASSED)
        broken = sorted(c.id for c in after.cases if c.outcome is Outcome.ERROR and "::" not in c.id)
        if broken:
            notes.append(f"test module(s) failed to import: {', '.join(broken)}")
    else:  # the suite could not run at all: every previously passing test is affected
        newly, still = sorted(passing_before), 0
        notes.append(f"test run ended with {after.status.value}: the fault breaks the suite as a whole")
    detected = bool(newly)

    predicted: list[str] | None = None
    precision = recall = None
    modules = [f.module for f in scenario.faults if isinstance(f, ModuleUnavailable)]
    if modules and len(modules) == len(scenario.faults):
        g = DependencyGraph.from_snapshot(snap)
        test_modules = {m.name for m in snap.modules if m.is_test}
        reach: set[str] = set()
        for m in modules:
            reach |= g.impacted_by(m) | {m}
        predicted = sorted(t for t in passing_before if _test_module(t, test_modules) in reach)
        tp = len(set(predicted) & set(newly))
        precision = round(tp / len(predicted), 3) if predicted else None
        recall = round(tp / len(newly), 3) if newly else None
    elif modules:
        notes.append("static prediction is only computed for scenarios made solely of module faults")
    return CounterfactualResult(
        scenario=scenario, baseline_status=baseline.status.value, outcome_status=after.status.value,
        newly_failing=newly, still_passing=still, detected=detected, blind_spot=not detected,
        predicted_impacted_tests=predicted, static_precision=precision, static_recall=recall, notes=notes,
        limitations=limitations)
