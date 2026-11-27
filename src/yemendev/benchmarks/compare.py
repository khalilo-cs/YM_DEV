"""Comparative benchmark (directive section 15, arms A-E). ``yemendev bench --compare``.

Every arm is real code run on the same seeded fixtures with the same inputs (the same human-authored plan
where a scenario provides one), through the same sandbox and test runner. What the arms are - stated in the
report, because it bounds what the comparison can show:

* **A - conventional automation**: apply the given patch, run the tests. No simulation, approval, verification
  gate, checkpoint, rollback, evidence or learning. A stand-in for "apply then test" tooling written for this
  benchmark, not a third-party product.
* **B - baseline AI coding agent**: the configured model proposes a patch from the failing tests (YEMENDEV's
  diagnosis is its context) and the patch is applied directly, then the tests run. **Only run when a model is
  configured**; otherwise reported as not run - never estimated. When a model is configured, the YEMENDEV arms
  receive the same model.
* **C - YEMENDEV, memory without skills** (ablation): the full workflow, but learned skills are never activated.
* **D - YEMENDEV, skills without gates** (ablation): plans and active skills are applied directly - no
  simulation, approval, verification or rollback.
* **E - YEMENDEV, complete RACE-X workflow.**

So C and D isolate what skills and the safety gates contribute; A shows the same inputs without either. None of
this measures other vendors' agents, and the fixtures are small synthetic Python projects.

Correctness is judged by invariants, not by an expected status alone: a repair must leave the tests passing; a
refused plan must leave the project byte-identical; a human edit must never be overwritten; a healthy project
must not be touched.
"""

from __future__ import annotations

import os
import platform as pyplatform
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev import __version__
from yemendev.benchmarks.extended import learn_and_activate, rename_plan, tests_pass
from yemendev.benchmarks.fixtures import FIXTURES_VERSION, renamed_api_project
from yemendev.benchmarks.resilience import (
    OPERATOR,
    Ctx,
    _git_commit,
    _git_dirty,
    _plan,
    _run_to_end,
    _tmp,
    project_id_for,
)
from yemendev.core.config import ModelSettings, SandboxSettings, Settings
from yemendev.core.contracts import InvalidInputError, iso
from yemendev.core.faults import FaultInjector
from yemendev.diagnostics.engine import DiagnosticReport
from yemendev.mosaic.adapters import OpenAICompatibleAdapter
from yemendev.orion.tasks import TaskStatus
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import RepairPlan
from yemendev.recovery.model_proposer import propose_with_model

ARMS = {
    "A": "conventional automation: apply the given patch, then run the tests (no simulation, approval, verification "
         "gate, checkpoint, rollback, evidence or learning)",
    "B": "baseline AI coding agent: the configured model proposes a patch that is applied directly, then tests run",
    "C": "YEMENDEV ablation: complete workflow and memory, learned skills never activated",
    "D": "YEMENDEV ablation: plans and active skills applied directly, without simulation/approval/verification/"
         "rollback",
    "E": "YEMENDEV with the complete RACE-X workflow",
}
SCENARIOS = ["repair_basic", "reject_regressing_plan", "healthy_no_action", "compound_crash_plus_human_edit",
             "skill_transfer"]
HUMAN_TEXT = "VERSION = 1  # edited by a human during the outage\n"


class ArmResult(BaseModel):
    arm: str
    scenario: str
    outcome: str
    correct: bool
    tests_pass_after: bool
    regression: bool
    data_loss: bool
    approvals: int
    evidence_records: int
    seconds: float
    note: str = ""


class CompareReport(BaseModel):
    benchmark: str = "yemendev-compare"
    benchmark_version: str = "0.1"
    platform_version: str = __version__
    started_at: str
    finished_at: str
    seed: int
    git_commit: str | None
    environment: dict[str, Any]
    arms: dict[str, str]
    not_run: dict[str, str]
    caveats: list[str]
    results: list[ArmResult]
    summary: dict[str, dict[str, Any]]


# ---- helpers ---------------------------------------------------------------------------------------
def _apply(ctx: Ctx, plan: RepairPlan, only_first: bool = False) -> None:
    for patch in plan.patches[:1] if only_first else plan.patches:
        (ctx.root / patch.path).write_text(patch.new_content)


def _evidence(p: Platform | None) -> int:
    return len(p.evidence.list(limit=10_000)) if p else 0


def _understand(p: Platform, ctx: Ctx, plan: RepairPlan | None, upto: str) -> tuple[RepairWorkflow, str] | None:
    """Run the workflow's own understanding steps (scan, baseline, diagnose[, propose]) and stop there.
    ``None`` when the workflow stops early (e.g. no reproducible problem): there is nothing to apply."""
    wf = RepairWorkflow(p)
    pid = project_id_for(p, ctx.root) or p.register_project(OPERATOR, "bench", str(ctx.root)).id
    t = wf.create(OPERATOR, pid, human_plan=plan)
    handlers = wf._handlers()
    for step in ("scan", "baseline", "diagnose", "propose"):
        wf.tasks.start_step(t.id, step)
        try:
            out = handlers[step](OPERATOR, t)
        except Exception:  # noqa: BLE001 - a workflow stop means: nothing to apply
            return None
        wf.tasks.finish_step(t.id, step, out)
        if step == upto:
            break
    return wf, t.id


def _output(wf: RepairWorkflow, task_id: str, step: str) -> dict[str, Any]:
    rec = wf.tasks.step(task_id, step)
    return (rec.output if rec else None) or {}


def _plans_without_gates(p: Platform, ctx: Ctx, plan: RepairPlan | None) -> list[RepairPlan]:
    r = _understand(p, ctx, plan, "propose")
    return [RepairPlan(**x) for x in _output(*r, "propose").get("plans", [])] if r else []


def _model_plans(p: Platform, ctx: Ctx) -> tuple[list[RepairPlan], str]:
    adapter = p.models.first_available()
    if adapter is None:
        return [], "model unavailable"
    r = _understand(p, ctx, None, "diagnose")
    if r is None:
        return [], "no reproducible problem"
    try:
        return [propose_with_model(adapter, DiagnosticReport(**_output(*r, "diagnose")), ctx.root)], "model patch"
    except Exception as exc:  # noqa: BLE001 - an unusable model answer is a measured outcome
        return [], f"model proposal failed: {exc}"[:120]


def _snapshot(root: Any) -> dict[str, bytes]:
    return {f.relative_to(root).as_posix(): f.read_bytes() for f in root.rglob("*.py")}


# ---- one scenario for one arm ----------------------------------------------------------------------
def _run(arm: str, scenario: str, ctx: Ctx) -> tuple[str, bool, bool, bool, int, Platform | None, str]:
    """Returns (outcome, correct, regression, data_loss, approvals, platform, note)."""
    p = ctx.platform(with_faults=False)
    extra = ctx.root / ctx.meta["extra"]
    if scenario == "healthy_no_action":
        svc = ctx.root / ctx.meta["service"]
        svc.write_text(svc.read_text().replace(ctx.meta["old"], ctx.meta["new"]))
    before = _snapshot(ctx.root)

    if scenario == "skill_transfer":
        first_plan = rename_plan(ctx.root, ctx.meta["service"], ctx.meta["old"], ctx.meta["new"])
        if arm in ("C", "D", "E"):
            st1, _, p, t1 = _run_to_end(ctx, first_plan, platform=p)
            if arm in ("D", "E") and st1 is TaskStatus.SUCCEEDED:
                learn_and_activate(p, ctx, t1)
        else:  # A and B keep no memory or skills: the first repair teaches them nothing
            _apply(ctx, first_plan)
        second = ctx.root.parent / "second"
        meta2 = renamed_api_project(second, ctx.seed + 1, names=(ctx.meta["old"], ctx.meta["new"]),
                                    package=ctx.meta["package"])
        c2 = ctx.with_root(second, meta2)
        if arm == "A":
            ok = tests_pass(p, second)  # nothing to apply without a plan
            return "not_repaired", ok, False, False, 0, None, "no plan for the second project"
        if arm in ("B", "D"):
            plans, note = _model_plans(p, c2) if arm == "B" else (_plans_without_gates(p, c2, None), "")
            if plans:
                _apply(c2, plans[0])
            ok = tests_pass(p, second)
            return ("repaired" if ok else "not_repaired"), ok, bool(plans) and not ok, False, 0, p, \
                note or f"{len(plans)} plan(s) proposed"
        st, msg, p, t2 = _run_to_end(c2, None, platform=p)
        ok = tests_pass(p, second)
        return st.value, st is TaskStatus.SUCCEEDED and ok, False, False, len(p.approvals.for_task(t2)), p, msg[:120]

    plan: RepairPlan | None = None
    if scenario == "repair_basic":
        plan = _plan(ctx)
    elif scenario == "reject_regressing_plan":
        plan = _plan(ctx, correct=False)
    elif scenario == "compound_crash_plus_human_edit":
        plan = _plan(ctx, touch_extra=True)

    if arm in ("A", "B", "D"):
        note = ""
        plans = [plan] if plan else []
        if arm == "B":
            plans, note = _model_plans(p, ctx)
        elif arm == "D" and scenario != "compound_crash_plus_human_edit":
            plans = _plans_without_gates(p, ctx, plan)
        if scenario == "compound_crash_plus_human_edit" and plans:
            _apply(ctx, plans[0], only_first=True)   # the run dies after the first file ...
            extra.write_text(HUMAN_TEXT)             # ... a human edits the second file during the outage ...
            _apply(ctx, plans[0])                    # ... and the tool simply runs again from the start
        elif plans:
            _apply(ctx, plans[0])
        ok = tests_pass(p, ctx.root)
        loss = scenario == "compound_crash_plus_human_edit" and extra.read_text() != HUMAN_TEXT
        regression = bool(plans) and not ok
        outcome = "applied" if plans else "nothing_applied"
        if scenario == "healthy_no_action":
            correct = _snapshot(ctx.root) == before and ok
        elif scenario == "compound_crash_plus_human_edit":
            correct = not loss
        else:
            correct = ok
        return outcome, correct, regression, loss, 0, (p if arm != "A" else None), note

    # C and E: the platform's own workflow
    crash = "recovery.after_file_write" if scenario == "compound_crash_plus_human_edit" else None

    def human_edit(c: Ctx) -> None:
        (c.root / c.meta["extra"]).write_text(HUMAN_TEXT)

    st, msg, p, tid = _run_to_end(ctx, plan, crash_point=crash, between=human_edit if crash else None)
    ok = tests_pass(p, ctx.root)
    unchanged = _snapshot(ctx.root) == before
    loss = crash is not None and extra.read_text() != HUMAN_TEXT
    if scenario == "repair_basic":
        correct = st is TaskStatus.SUCCEEDED and ok
    elif scenario == "reject_regressing_plan":  # refuse untouched, or (with a model) a verified alternative
        correct = (st is TaskStatus.NO_ACTION and unchanged) or (st is TaskStatus.SUCCEEDED and ok)
    elif scenario == "healthy_no_action":
        correct = st is TaskStatus.NO_ACTION and unchanged
    else:  # compound: stop for review, human edit intact
        correct = st is TaskStatus.FAILED and not loss
    return st.value, correct, st is TaskStatus.SUCCEEDED and not ok, loss, len(p.approvals.for_task(tid)), p, \
        msg[:120]


def run_compare(seed: int = 7, backend: str = "process", arms: str = "ABCDE",
                only: list[str] | None = None, model: ModelSettings | None = None) -> CompareReport:
    """``model`` defaults to the environment's model settings (``YEMENDEV_MODEL_*``); every arm that can use a
    model gets the same one."""
    model = model if model is not None else Settings.from_env(Path(".")).model
    arms = "".join(dict.fromkeys(arms))
    bad = sorted(set(arms) - set(ARMS)) + sorted(set(only or []) - set(SCENARIOS))
    if bad or not arms:
        raise InvalidInputError(f"unknown arm(s)/scenario(s): {bad}; arms are {''.join(ARMS)}, "
                                f"scenarios are {SCENARIOS}")
    chosen = [s for s in SCENARIOS if not only or s in only]
    started = iso()
    results: list[ArmResult] = []
    not_run: dict[str, str] = {}
    model_ok = OpenAICompatibleAdapter(model).availability().available
    for arm in arms:
        if arm == "B" and not model_ok:
            not_run["B"] = "no model is configured or reachable (YEMENDEV_MODEL_BASE_URL); not estimated"
            continue
        for scenario in chosen:
            sc_seed = seed * 1000 + SCENARIOS.index(scenario)  # stable when filtering
            with _tmp() as base:
                meta = renamed_api_project(base / "proj", sc_seed)
                settings = Settings(data_dir=base / "data", workspaces_root=base / "ws", model=model,
                                    sandbox=SandboxSettings(timeout_seconds=60, backend=backend))
                ctx = Ctx(root=base / "proj", meta=meta, settings=settings, faults=FaultInjector(), seed=sc_seed)
                t0 = time.monotonic()
                try:
                    outcome, correct, regression, loss, approvals, p, note = _run(arm, scenario, ctx)
                    results.append(ArmResult(
                        arm=arm, scenario=scenario, outcome=outcome, correct=correct,
                        tests_pass_after=tests_pass(ctx.platform(with_faults=False), ctx.root)
                        if scenario != "skill_transfer" else correct,
                        regression=regression, data_loss=loss, approvals=approvals, evidence_records=_evidence(p),
                        seconds=round(time.monotonic() - t0, 2), note=note))
                except Exception as exc:  # noqa: BLE001 - an arm crashing is a measured result, not a harness abort
                    results.append(ArmResult(arm=arm, scenario=scenario, outcome="harness_error", correct=False,
                                             tests_pass_after=False, regression=False, data_loss=False, approvals=0,
                                             evidence_records=0, seconds=round(time.monotonic() - t0, 2),
                                             note=f"{exc.__class__.__name__}: {exc}"[:200]))
    summary: dict[str, dict[str, Any]] = {}
    for arm in sorted({r.arm for r in results}):
        rs = [r for r in results if r.arm == arm]
        summary[arm] = {"correct": f"{sum(r.correct for r in rs)}/{len(rs)}",
                        "regressions": sum(r.regression for r in rs), "data_loss": sum(r.data_loss for r in rs),
                        "human_approvals": sum(r.approvals for r in rs),
                        "evidence_records": sum(r.evidence_records for r in rs)}
    return CompareReport(
        started_at=started, finished_at=iso(), seed=seed, git_commit=_git_commit(),
        environment={"model": f"{model.model}@{model.base_url}" if model_ok else None,
                     "python": sys.version.split()[0], "platform": pyplatform.platform(), "cpus": os.cpu_count(),
                     "sandbox_backend": backend, "fixtures_version": FIXTURES_VERSION, "scenarios": chosen,
                     "git_dirty": _git_dirty()},
        arms={k: v for k, v in ARMS.items() if k in arms}, not_run=not_run,
        caveats=[
            "A, C and D are constructed for this benchmark (a naive apply-then-test pipeline and two ablations of "
            "YEMENDEV); they are not third-party tools or agents.",
            "B (an AI coding agent) is only run when a model is configured; no numbers are estimated otherwise. "
            "With a model, B's quality is that model's, on these fixtures only.",
            "All arms receive identical inputs, including the same human-authored plan where a scenario has one.",
            "Five small synthetic Python scenarios: the results show what the mechanisms prevent on these cases; "
            "they are not a general ranking.",
        ],
        results=results, summary=summary)
