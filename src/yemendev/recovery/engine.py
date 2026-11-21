"""Autonomous Recovery Engine - the mechanics of each lifecycle phase.

    PREPARE -> SIMULATE -> (AUTHORIZE: orchestrator) -> EXECUTE -> VERIFY -> COMMIT
    on failure: ROLLBACK -> (DIAGNOSE) -> preserve evidence

Authorization and evidence recording are done by the orchestrator, which owns policy.
This module never decides *whether* to modify a project, only *how* to do it safely.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import PreconditionFailedError, sha256_bytes
from yemendev.core.faults import NO_FAULTS, FaultInjector
from yemendev.learning.skills import is_test_path
from yemendev.recovery.checkpoint import CheckpointStore, atomic_write, safe_target
from yemendev.recovery.model import RepairPlan, TestComparison
from yemendev.sandbox.testrunner import Outcome, RunStatus, TestRunReport, run_pytest
from yemendev.sandbox.workspace import Workspace

TEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "conftest.py", "noxfile.py"}


class PreparedPlan(BaseModel):
    plan: RepairPlan
    originals: dict[str, str]
    diff: str


class PhaseResult(BaseModel):
    passed: bool
    report: TestRunReport | None
    comparison: TestComparison | None
    problems: list[str]


def compare(baseline: TestRunReport, after: TestRunReport) -> TestComparison:
    b, a = baseline.outcomes(), after.outcomes()
    failing = (Outcome.FAILED, Outcome.ERROR)
    return TestComparison(
        fixed=sorted(t for t, o in b.items() if o in failing and a.get(t) is Outcome.PASSED),
        regressed=sorted(t for t, o in b.items() if o is Outcome.PASSED and t in a and a[t] is not Outcome.PASSED),
        still_failing=sorted(t for t, o in b.items() if o in failing and a.get(t) in failing),
        newly_added=sorted(set(a) - set(b)),
        removed=sorted(set(b) - set(a)),
        baseline_failing=sum(1 for o in b.values() if o in failing),
        after_failing=sum(1 for o in a.values() if o in failing),
    )


class RecoveryEngine:
    def __init__(self, sandbox: SandboxSettings, checkpoints: CheckpointStore, workspaces_dir: Path,
                 faults: FaultInjector = NO_FAULTS) -> None:
        self.sandbox = sandbox
        self.checkpoints = checkpoints
        self.workspaces_dir = workspaces_dir
        self.faults = faults

    def prepare(self, root: Path, plan: RepairPlan) -> PreparedPlan:
        originals: dict[str, str] = {}
        for p in plan.patches:
            name = Path(p.path).name
            if is_test_path(p.path) or name in TEST_CONFIG_FILES:
                raise PreconditionFailedError(
                    f"plan modifies test code or test configuration ({p.path}); it cannot be verified by those tests")
            t = safe_target(root, p.path)
            if p.original_sha256 is None:
                if t.exists():
                    raise PreconditionFailedError(f"{p.path} already exists but plan expects to create it")
                originals[p.path] = ""
                continue
            if not t.is_file():
                raise PreconditionFailedError(f"{p.path} does not exist")
            data = t.read_bytes()
            if sha256_bytes(data) != p.original_sha256:
                raise PreconditionFailedError(f"{p.path} changed since the plan was created")
            originals[p.path] = data.decode("utf-8", "replace")
        return PreparedPlan(plan=plan, originals=originals, diff=plan.unified_diff(originals))

    def simulate(self, root: Path, plan: RepairPlan, baseline: TestRunReport,
                 sandbox: SandboxSettings | None = None) -> PhaseResult:
        self.prepare(root, plan)
        with Workspace(root, sandbox or self.sandbox, base_dir=self.workspaces_dir) as ws:
            for p in plan.patches:
                ws.write(p.path, p.new_content.encode("utf-8"))
            report = run_pytest(ws)
        return self._judge(baseline, report)

    def _judge(self, baseline: TestRunReport, report: TestRunReport) -> PhaseResult:
        problems = []
        if report.status in (RunStatus.RUNNER_ERROR, RunStatus.TIMED_OUT, RunStatus.COLLECTION_ERROR,
                             RunStatus.NO_TESTS):
            problems.append(f"test run status {report.status.value}")
            return PhaseResult(passed=False, report=report, comparison=None, problems=problems)
        cmp = compare(baseline, report)
        if not cmp.fixed:
            problems.append("no previously failing test now passes")
        if cmp.regressed:
            problems.append(f"{len(cmp.regressed)} previously passing test(s) no longer pass")
        if cmp.removed:
            problems.append(f"{len(cmp.removed)} test(s) disappeared")
        return PhaseResult(passed=cmp.acceptable, report=report, comparison=cmp, problems=problems)

    def execute(self, root: Path, plan: RepairPlan, checkpoint_id: str) -> list[str]:
        self.prepare(root, plan)  # re-check preconditions right before writing (TOCTOU)
        self.checkpoints.create(checkpoint_id, root, [p.path for p in plan.patches])
        self.faults.check("recovery.after_checkpoint")
        written = []
        for p in plan.patches:
            atomic_write(safe_target(root, p.path), p.new_content.encode("utf-8"))
            written.append(p.path)
            self.faults.check("recovery.after_file_write")
        return written

    def applied_state(self, root: Path, plan: RepairPlan) -> str:
        """'applied' | 'original' | 'partial' | 'diverged' - used to reconcile after a crash."""
        states = []
        for p in plan.patches:
            t = safe_target(root, p.path)
            cur = sha256_bytes(t.read_bytes()) if t.is_file() else None
            if cur == p.new_sha256:
                states.append("new")
            elif cur == p.original_sha256:
                states.append("orig")
            else:
                states.append("other")
        if all(s == "new" for s in states):
            return "applied"
        if all(s == "orig" for s in states):
            return "original"
        if "other" in states:
            return "diverged"
        return "partial"

    def verify(self, root: Path, plan: RepairPlan, baseline: TestRunReport,
               sandbox: SandboxSettings | None = None) -> PhaseResult:
        if self.applied_state(root, plan) != "applied":
            return PhaseResult(passed=False, report=None, comparison=None,
                               problems=["patched files do not match the approved content"])
        with Workspace(root, sandbox or self.sandbox, base_dir=self.workspaces_dir) as ws:
            report = run_pytest(ws)
        return self._judge(baseline, report)

    def rollback(self, checkpoint_id: str) -> list[str]:
        return self.checkpoints.restore(checkpoint_id)
