"""YEMENDEV Resilience Benchmark (initial harness).

Runs scripted scenarios through the *real* platform (real sandbox, real tests, real files) and
measures what happened. Nothing is simulated except the failures being injected.

Honesty rules baked into the output:
* only this system is measured - no comparative baselines are implemented, and the report says so;
* approvals are granted by the harness acting as a second human; recorded in the report;
* injected crashes are in-process exceptions, not real power loss.
"""

from __future__ import annotations

import os
import platform as pyplatform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev import __version__
from yemendev.benchmarks.fixtures import renamed_api_project
from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import iso, sha256_bytes
from yemendev.core.faults import FaultInjector, InjectedFault
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.approvals import ApprovalStatus
from yemendev.orion.tasks import TaskStatus
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan

OPERATOR = Actor(id="bench-operator", kind=ActorKind.HUMAN, role=Role.ADMIN)
APPROVER = Actor(id="bench-approver", kind=ActorKind.HUMAN, role=Role.OPERATOR)
REQUIRED_EVIDENCE = {"project.scan", "tests.baseline", "diagnostics.report", "repair.propose", "repair.simulate"}
HEALTHY_EVIDENCE = {"project.scan", "tests.baseline", "diagnostics.report"}


class ScenarioResult(BaseModel):
    scenario: str
    category: str
    seed: int
    expected_status: str
    actual_status: str
    correct: bool
    data_integrity: bool
    integrity_notes: list[str]
    evidence_complete: bool
    audit_valid: bool
    approvals_requested: int
    seconds_to_terminal: float
    detail: str


class BenchmarkReport(BaseModel):
    benchmark: str = "yemendev-resilience"
    benchmark_version: str = "0.1"
    platform_version: str = __version__
    started_at: str
    finished_at: str
    seed: int
    git_commit: str | None
    environment: dict[str, Any]
    caveats: list[str]
    results: list[ScenarioResult]
    summary: dict[str, Any]


@dataclass
class Ctx:
    root: Path
    meta: dict[str, str]
    settings: Settings
    faults: FaultInjector

    def platform(self, with_faults: bool = True) -> Platform:
        return Platform(self.settings, self.faults if with_faults else FaultInjector())

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()


def _plan(ctx: Ctx, correct: bool = True, touch_extra: bool = False) -> RepairPlan:
    svc = ctx.root / ctx.meta["service"]
    text = svc.read_text()
    new = text.replace(f"core.{ctx.meta['old']}", f"core.{ctx.meta['new']}")
    if not correct:  # fixes total() but breaks scaled()
        new = new.replace("core.scale(x,", "core.scale(x + 1,")
    patches = [FilePatch(path=ctx.meta["service"], original_sha256=sha256_bytes(svc.read_bytes()), new_content=new)]
    if touch_extra:
        ex = ctx.root / ctx.meta["extra"]
        patches.append(FilePatch(path=ctx.meta["extra"], original_sha256=sha256_bytes(ex.read_bytes()),
                                 new_content="VERSION = 2\n"))
    return RepairPlan(id="pln_bench", description="benchmark plan", source=PlanSource.HUMAN, source_ref="bench",
                      patches=patches)


def _approve(p: Platform, task_id: str) -> int:
    pend = [a for a in p.approvals.for_task(task_id) if a.status is ApprovalStatus.PENDING]
    for a in pend:
        p.decide_approval(APPROVER, a.id, approve=True, reason="benchmark harness")
    return len(pend)


def _run_to_end(ctx: Ctx, plan: RepairPlan | None, crash_point: str | None = None,
                between: Callable[[Ctx], None] | None = None) -> tuple[TaskStatus, str, Platform, str]:
    p = ctx.platform()
    wf = RepairWorkflow(p)
    proj = p.register_project(OPERATOR, "bench", str(ctx.root))
    p.grant_member(OPERATOR, proj.id, APPROVER.ref, Role.OPERATOR)
    t = wf.create(OPERATOR, proj.id, human_plan=plan)
    if crash_point:
        ctx.faults.arm(crash_point)
    msg = ""
    for _ in range(6):  # advance / approve / crash-resume loop with a hard bound
        try:
            r = wf.advance(OPERATOR, t.id)
            msg = r.message
            if r.task.status is TaskStatus.WAITING_APPROVAL:
                _approve(p, t.id)
                continue
            return r.task.status, msg, p, t.id
        except InjectedFault:
            if between:
                between(ctx)
            wf.tasks.expire_lease(t.id)  # the crashed worker's lease would expire with time
            p = ctx.platform(with_faults=False)  # "restart": fresh process state
            wf = RepairWorkflow(p)
    return wf.tasks.get(t.id).status, "did not terminate within bound", p, t.id


Scenario = tuple[str, str, Callable[[Ctx], tuple[str, TaskStatus, str, Platform, str, list[str]]]]


def _sc_repair(ctx: Ctx) -> Any:
    st, msg, p, tid = _run_to_end(ctx, _plan(ctx))
    ok = ctx.meta["old"] not in (ctx.root / ctx.meta["service"]).read_text()
    return "succeeded", st, msg, p, tid, [] if ok else ["fix not present on disk"]


def _sc_bad_plan(ctx: Ctx) -> Any:
    before = ctx.read(ctx.meta["service"])
    st, msg, p, tid = _run_to_end(ctx, _plan(ctx, correct=False))
    return "no_action", st, msg, p, tid, [] if ctx.read(ctx.meta["service"]) == before else ["project modified"]


def _sc_healthy(ctx: Ctx) -> Any:
    svc = ctx.root / ctx.meta["service"]
    svc.write_text(svc.read_text().replace(ctx.meta["old"], ctx.meta["new"]))
    before = ctx.read(ctx.meta["service"])
    st, msg, p, tid = _run_to_end(ctx, None)
    return "no_action", st, msg, p, tid, [] if ctx.read(ctx.meta["service"]) == before else ["project modified"]


def _sc_crash(point: str) -> Callable[[Ctx], Any]:
    def run(ctx: Ctx) -> Any:
        st, msg, p, tid = _run_to_end(ctx, _plan(ctx, touch_extra=True), crash_point=point)
        notes = []
        if ctx.meta["old"] in (ctx.root / ctx.meta["service"]).read_text():
            notes.append("service fix missing after resume")
        if (ctx.root / ctx.meta["extra"]).read_text() != "VERSION = 2\n":
            notes.append("second file not applied after resume")
        return "succeeded", st, msg, p, tid, notes
    return run


def _sc_compound_crash_plus_human_edit(ctx: Ctx) -> Any:
    """Crash after the first of two file writes AND a human edits the second file before restart.

    Required behaviour: never overwrite the human's edit; stop and ask for review (FAILED + reason).
    """
    human_text = "VERSION = 1  # edited by a human during the outage\n"

    def human_edit(c: Ctx) -> None:
        (c.root / c.meta["extra"]).write_text(human_text)

    st, msg, p, tid = _run_to_end(ctx, _plan(ctx, touch_extra=True), crash_point="recovery.after_file_write",
                                  between=human_edit)
    notes = [] if (ctx.root / ctx.meta["extra"]).read_text() == human_text else ["human edit was overwritten"]
    if st is TaskStatus.FAILED and "diverged" not in msg:
        notes.append(f"failed for an unexpected reason: {msg}")
    return "failed", st, msg, p, tid, notes


def _sc_compound_drift_rollback(ctx: Ctx) -> Any:
    """Simulation passes, then the environment changes before verification -> must roll back byte-exactly."""
    shared = Path(tempfile.mkdtemp(prefix="ydv-bench-"))
    os.chmod(shared, 0o777)  # noqa: S103 - external shared resource the sandbox may write
    counter = shared / "n"
    (ctx.root / "tests/test_env.py").write_text(
        f"from pathlib import Path\n\n\ndef test_env():\n    p = Path({str(counter)!r})\n"
        "    n = int(p.read_text()) + 1 if p.exists() else 1\n    p.write_text(str(n))\n    assert n <= 3\n")
    before = {r: ctx.read(r) for r in (ctx.meta["service"], ctx.meta["extra"])}
    st, msg, p, tid = _run_to_end(ctx, _plan(ctx, touch_extra=True))
    after = {r: ctx.read(r) for r in before}
    shutil.rmtree(shared, ignore_errors=True)
    return "rolled_back", st, msg, p, tid, [] if after == before else ["rollback was not byte-exact"]


def _sc_offline(ctx: Ctx) -> Any:
    real = socket.socket.connect

    def guarded(self: socket.socket, addr: Any) -> Any:
        if self.family == socket.AF_UNIX:
            return real(self, addr)
        raise OSError("offline benchmark: network disabled")

    socket.socket.connect = guarded  # type: ignore[method-assign,assignment]
    try:
        st, msg, p, tid = _run_to_end(ctx, _plan(ctx))
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
    return "succeeded", st, msg, p, tid, []


QUICK_CRASH_POINTS = ["step.baseline.before_finish", "recovery.after_file_write", "step.verify.before_finish"]
ALL_CRASH_POINTS = ["step.scan.before_finish", "step.baseline.before_finish", "step.simulate.before_finish",
                    "step.authorize.before_finish", "recovery.after_checkpoint", "recovery.after_file_write",
                    "step.execute.before_finish", "step.verify.before_finish", "step.learn.before_finish"]


def scenarios(full: bool = False) -> list[Scenario]:
    out: list[Scenario] = [
        ("repair_basic", "safe_recovery", _sc_repair),
        ("reject_regressing_plan", "safe_recovery", _sc_bad_plan),
        ("healthy_no_action", "false_positive", _sc_healthy),
        ("offline_repair", "intermittent_connectivity", _sc_offline),
        ("compound_drift_then_rollback", "compound_failure", _sc_compound_drift_rollback),
        ("compound_crash_plus_human_edit", "compound_failure", _sc_compound_crash_plus_human_edit),
    ]
    for pt in (ALL_CRASH_POINTS if full else QUICK_CRASH_POINTS):
        out.append((f"crash_resume@{pt}", "power_interruption", _sc_crash(pt)))
    return out


def _git_commit() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                           cwd=Path(__file__).resolve().parent)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_benchmark(seed: int = 7, full: bool = False, only: list[str] | None = None) -> BenchmarkReport:
    started = iso()
    results: list[ScenarioResult] = []
    for i, (name, category, fn) in enumerate(scenarios(full)):
        if only and name not in only:
            continue
        sc_seed = seed * 1000 + i
        with _tmp() as base:
            meta = renamed_api_project(base / "proj", sc_seed)
            settings = Settings(data_dir=base / "data", workspaces_root=base / "ws",
                                sandbox=SandboxSettings(timeout_seconds=60))
            ctx = Ctx(root=base / "proj", meta=meta, settings=settings, faults=FaultInjector())
            t0 = time.monotonic()
            try:
                expected, st, msg, p, tid, notes = fn(ctx)
                elapsed = time.monotonic() - t0
                ev = {e.action for e in p.evidence.list(task_id=tid, limit=1000)}
                integ = p.verify_integrity()
                # a healthy project legitimately stops after diagnosis
                req_ev = HEALTHY_EVIDENCE if name == "healthy_no_action" else REQUIRED_EVIDENCE
                results.append(ScenarioResult(
                    scenario=name, category=category, seed=sc_seed, expected_status=expected,
                    actual_status=st.value, correct=st.value == expected and not notes, data_integrity=not notes,
                    integrity_notes=notes, evidence_complete=req_ev <= ev, audit_valid=bool(integ["valid"]),
                    approvals_requested=len(p.approvals.for_task(tid)), seconds_to_terminal=round(elapsed, 2),
                    detail=msg[:300]))
            except Exception as exc:  # noqa: BLE001 - a crashing scenario is a measured failure, not a harness abort
                results.append(ScenarioResult(
                    scenario=name, category=category, seed=sc_seed, expected_status="?", actual_status="harness_error",
                    correct=False, data_integrity=False, integrity_notes=[f"{exc.__class__.__name__}: {exc}"[:300]],
                    evidence_complete=False, audit_valid=False, approvals_requested=0,
                    seconds_to_terminal=round(time.monotonic() - t0, 2), detail="scenario raised"))
    n = len(results)
    rec = [r for r in results if r.category in ("power_interruption", "compound_failure")]
    summary = {
        "scenarios": n,
        "correct": sum(r.correct for r in results),
        "task_success_rate": round(sum(r.correct for r in results) / n, 3) if n else None,
        "data_integrity_violations": sum(not r.data_integrity for r in results),
        "evidence_complete_rate": round(sum(r.evidence_complete for r in results) / n, 3) if n else None,
        "audit_valid_rate": round(sum(r.audit_valid for r in results) / n, 3) if n else None,
        "recovery_scenarios_correct": f"{sum(r.correct for r in rec)}/{len(rec)}",
        "mean_seconds_to_terminal": round(sum(r.seconds_to_terminal for r in results) / n, 2) if n else None,
    }
    return BenchmarkReport(
        started_at=started, finished_at=iso(), seed=seed, git_commit=_git_commit(),
        environment={"python": sys.version.split()[0], "platform": pyplatform.platform(), "cpus": os.cpu_count(),
                     "euid": os.geteuid()},
        caveats=[
            "Only YEMENDEV itself is measured; no comparative baselines (conventional tooling, other agents) "
            "are implemented, so no comparison is claimed.",
            "Approvals were granted automatically by the harness acting as a second human.",
            "Crashes are injected in-process exceptions, not real power loss.",
            "Fixture projects are small synthetic Python projects; results do not generalize to real codebases.",
        ],
        results=results, summary=summary,
    )


@contextmanager
def _tmp() -> Any:
    d = Path(tempfile.mkdtemp(prefix="ydv-bench-run-"))
    os.chmod(d, 0o711)  # noqa: S103 - sandbox uid must traverse to its workspace
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
