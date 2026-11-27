"""YEMENDEV Resilience Benchmark.

Runs scripted scenarios through the *real* platform (real sandbox, real tests, real files) and
measures what happened. Nothing is simulated except the failures being injected. Scenario categories
follow directive section 15 (see ``extended.py`` for the stage-20 additions); comparative arms are in
``compare.py``.

Honesty rules baked into the output:
* this report measures this system only; comparisons are a separate report with their own caveats;
* approvals are granted by the harness acting as a second human; recorded in the report;
* injected crashes are in-process exceptions, not real power loss.
"""

from __future__ import annotations

import os
import platform as pyplatform
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev import __version__
from yemendev.benchmarks.fixtures import FIXTURES_VERSION, renamed_api_project
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
    cpu_seconds: float | None = None
    unauthorized_denials: int | None = None
    metrics: dict[str, Any] = {}


class BenchmarkReport(BaseModel):
    benchmark: str = "yemendev-resilience"
    benchmark_version: str = "0.2"
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
    seed: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    def platform(self, with_faults: bool = True) -> Platform:
        return Platform(self.settings, self.faults if with_faults else FaultInjector())

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def with_root(self, root: Path, meta: dict[str, str]) -> Ctx:
        """Another project on the same installation (shares settings, faults and metrics)."""
        return replace(self, root=root, meta=meta)

    @staticmethod
    def operator() -> Actor:
        return OPERATOR


def project_id_for(p: Platform, root: Path) -> str | None:
    want = str(root.resolve())
    return next((x.id for x in p.visible_projects(OPERATOR) if x.root_path == want), None)


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
                between: Callable[[Ctx], None] | None = None, platform: Platform | None = None,
                task_id: str | None = None, approve: bool = True) -> tuple[TaskStatus, str, Platform, str]:
    p = platform or ctx.platform()
    wf = RepairWorkflow(p)
    pid = project_id_for(p, ctx.root)
    if pid is None:
        pid = p.register_project(OPERATOR, "bench", str(ctx.root)).id
    if APPROVER.ref not in {m.actor_ref for m in p.list_members(OPERATOR, pid)}:
        p.grant_member(OPERATOR, pid, APPROVER.ref, Role.OPERATOR)
    t = wf.tasks.get(task_id) if task_id else wf.create(OPERATOR, pid, human_plan=plan)
    if crash_point:
        ctx.faults.arm(crash_point)
    msg = ""
    for _ in range(6):  # advance / approve / crash-resume loop with a hard bound
        try:
            r = wf.advance(OPERATOR, t.id)
            msg = r.message
            if r.task.status is TaskStatus.WAITING_APPROVAL and approve:
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
    """Crash right after the change is applied; during the outage a module the tests depend on (outside
    the plan) changes, so verification sees a previously passing test fail.

    Required: roll back the plan's files byte-exactly, and do NOT revert the external change.
    Backend-independent (regression: the earlier version relied on a host file the sandbox could reach,
    which the container backend correctly blocks, so the drift never happened there).
    """
    core = ctx.root / f"{ctx.meta['package']}/core.py"
    drifted = core.read_text().replace("return x * k", "return x * k + 1  # changed during the outage")

    def drift(c: Ctx) -> None:
        core.write_text(drifted)

    plan_files = (ctx.meta["service"], ctx.meta["extra"])
    before = {r: ctx.read(r) for r in plan_files}
    st, msg, p, tid = _run_to_end(ctx, _plan(ctx, touch_extra=True), crash_point="step.execute.before_finish",
                                  between=drift)
    notes = []
    if st is TaskStatus.ROLLED_BACK and {r: ctx.read(r) for r in plan_files} != before:
        notes.append("rollback was not byte-exact")
    if core.read_text() != drifted:
        notes.append("external change made during the outage was overwritten")
    return "rolled_back", st, msg, p, tid, notes


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
    from yemendev.benchmarks.extended import EXTENDED

    out += EXTENDED
    return out


def _git_commit() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                           cwd=Path(__file__).resolve().parent)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dirty() -> bool | None:
    """True when tracked source files differ from ``git_commit`` (the run is then not reproducible from it)."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal", "--", "src"],
                           capture_output=True, text=True, timeout=10, cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(r.stdout.strip()) if r.returncode == 0 else None


def run_benchmark(seed: int = 7, full: bool = False, only: list[str] | None = None,
                  backend: str = "process") -> BenchmarkReport:
    started = iso()
    results: list[ScenarioResult] = []
    for i, (name, category, fn) in enumerate(scenarios(full)):
        if only and name not in only:
            continue
        sc_seed = seed * 1000 + i
        with _tmp() as base:
            meta = renamed_api_project(base / "proj", sc_seed)
            settings = Settings(data_dir=base / "data", workspaces_root=base / "ws",
                                sandbox=SandboxSettings(timeout_seconds=60, backend=backend))
            ctx = Ctx(root=base / "proj", meta=meta, settings=settings, faults=FaultInjector(), seed=sc_seed)
            t0 = time.monotonic()
            cpu0 = _cpu()
            try:
                expected, st, msg, p, tid, notes = fn(ctx)
                elapsed = time.monotonic() - t0
                ev = {e.action for e in p.evidence.list(task_id=tid, limit=1000)} if tid else \
                    {e.action for e in p.evidence.list(limit=1000)}
                integ = p.verify_integrity()
                # a healthy project legitimately stops after diagnosis
                req_ev = set(ctx.metrics.pop("required_evidence", None) or (
                    HEALTHY_EVIDENCE if name == "healthy_no_action" else REQUIRED_EVIDENCE))
                audit_ok = bool(integ["valid"]) or bool(ctx.metrics.pop("skip_audit_valid", False))
                denials = sum(1 for e in p.audit.list(limit=5000) if e.outcome == "denied")
                results.append(ScenarioResult(
                    scenario=name, category=category, seed=sc_seed, expected_status=expected,
                    actual_status=st.value, correct=st.value == expected and not notes, data_integrity=not notes,
                    integrity_notes=notes, evidence_complete=req_ev <= ev, audit_valid=audit_ok,
                    approvals_requested=len(p.approvals.for_task(tid)) if tid else 0,
                    seconds_to_terminal=round(elapsed, 2), detail=msg[:300], cpu_seconds=round(_cpu() - cpu0, 2),
                    unauthorized_denials=denials, metrics=ctx.metrics))
            except Exception as exc:  # noqa: BLE001 - a crashing scenario is a measured failure, not a harness abort
                results.append(ScenarioResult(
                    scenario=name, category=category, seed=sc_seed, expected_status="?", actual_status="harness_error",
                    correct=False, data_integrity=False, integrity_notes=[f"{exc.__class__.__name__}: {exc}"[:300]],
                    evidence_complete=False, audit_valid=False, approvals_requested=0,
                    seconds_to_terminal=round(time.monotonic() - t0, 2), detail="scenario raised"))
    n = len(results)
    rec = [r for r in results if r.category in ("power_interruption", "compound_failure")]
    summary = _summary(results) | {
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
                     "sandbox_backend": backend, "euid": os.geteuid(), "fixtures_version": FIXTURES_VERSION,
                     "git_dirty": _git_dirty(),
                     "full": full, "only": only, "peak_rss_mb_self": round(_maxrss(resource.RUSAGE_SELF), 1),
                     "peak_rss_mb_children": round(_maxrss(resource.RUSAGE_CHILDREN), 1)},
        caveats=[
            "This report measures YEMENDEV only; comparisons with other approaches are a separate report "
            "(`yemendev bench --compare`) with its own caveats.",
            "Approvals were granted automatically by the harness acting as a second human.",
            "Crashes are injected in-process exceptions, not real power loss.",
            "Fixture projects are small synthetic Python projects; results do not generalize to real codebases.",
            "Learned skills match on the exact module and symbol names in their preconditions: skill_transfer "
            "shows reuse on a second project with the same module, not generalisation to differently named code.",
            "CPU seconds are this process and its child processes; under the docker backend, CPU used inside the "
            "containers is accounted to the docker daemon and is NOT included.",
        ],
        results=results, summary=summary,
    )


def _cpu() -> float:
    s, c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


def _maxrss(who: int) -> float:
    return resource.getrusage(who).ru_maxrss / 1024  # KiB on Linux


def _rate(num: float, den: float) -> float | None:
    return round(num / den, 3) if den else None


def _summary(results: list[ScenarioResult]) -> dict[str, Any]:
    """Directive section 15 metrics, each computed only from scenarios that measure it."""
    by = {r.category: [x for x in results if x.category == r.category] for r in results}
    m = [r.metrics for r in results]
    recovery = [r for r in results if r.category in ("safe_recovery", "power_interruption", "compound_failure")]
    crash = by.get("power_interruption", [])
    problems = [r for r in results if r.expected_status in ("succeeded", "rolled_back", "failed", "detected",
                                                            "blocked")]
    healthy = [r for r in results if r.expected_status == "no_action"]
    lh = next((x for x in m if "iterations" in x), None)
    integ = next((x for x in m if "tampering_attempts" in x), None)
    stale = next((x for x in m if "marked_stale" in x), None)
    unauth = next((x for x in m if "unauthorized_attempts" in x), None)
    tasks = [r for r in results if r.approvals_requested or r.expected_status in ("succeeded", "rolled_back")]
    return {
        "recovery_success_rate": _rate(sum(r.correct for r in recovery), len(recovery)),
        "mean_time_to_recovery_seconds": round(sum(r.seconds_to_terminal for r in crash) / len(crash), 2)
        if crash else None,
        "data_loss_incidents": sum(any("overwritten" in n or "lost" in n for n in r.integrity_notes) for r in results),
        "regression_rate": _rate(lh["regressions"], lh["iterations"]) if lh else None,
        "skill_transfer_success": next((x["skill_transfer_success"] for x in m if "skill_transfer_success" in x), None),
        "knowledge_retention": lh["knowledge_retention"] if lh else None,
        "knowledge_staleness_detected": (stale["marked_stale"] > 0 and stale["false_stale"] == 0) if stale else None,
        "false_positive_rate": _rate(sum(not r.correct for r in healthy), len(healthy)),
        "false_negative_rate": _rate(sum(r.actual_status in ("no_action", "missed") for r in problems), len(problems)),
        "tamper_detection": f"{integ['tampering_detected']}/{integ['tampering_attempts']}" if integ else None,
        "unauthorized_operations_blocked": f"{unauth['blocked']}/{unauth['unauthorized_attempts']}" if unauth else None,
        "human_approval_frequency": _rate(sum(r.approvals_requested for r in tasks), len(tasks)),
        "cpu_seconds_total": round(sum(r.cpu_seconds or 0 for r in results), 1),
    }


@contextmanager
def _tmp() -> Any:
    d = Path(tempfile.mkdtemp(prefix="ydv-bench-run-"))
    os.chmod(d, 0o711)  # noqa: S103 - sandbox uid must traverse to its workspace
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
