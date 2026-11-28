"""Validate the benchmark itself: sabotage one mechanism at a time and check the matching scenario fails.

A benchmark that cannot tell a broken system from a working one measures nothing. Each mutant disables one
safety mechanism in-process; the scenario that is meant to exercise it must then report a failure.
Run: PYTHONDONTWRITEBYTECODE=1 python benchmarks/mutation_check.py   (exit 1 if any mutant is missed)
"""

import sys

from yemendev.benchmarks.resilience import run_benchmark
from yemendev.continuity import sync as S
from yemendev.governance.guard import Guard
from yemendev.memory.engine import MemoryEngine
from yemendev.platform import Platform
from yemendev.recovery import engine as E
from yemendev.sandbox import workspace as W

missed = 0


def check(label, target, patch, scenario):
    global missed
    orig = {k: getattr(target, k) for k in patch}
    for k, v in patch.items():
        setattr(target, k, v)
    try:
        [r] = run_benchmark(seed=7, only=[scenario]).results
    finally:
        for k, v in orig.items():
            setattr(target, k, v)
    caught = not r.correct
    missed += not caught
    print(f"{'CAUGHT ' if caught else 'MISSED!'} mutant '{label}' -> {scenario}: status={r.actual_status} "
          f"notes={r.integrity_notes}")


R = E.RecoveryEngine
check("rollback is a no-op", R, {"rollback": lambda self, cid: []}, "compound_drift_then_rollback")
check("crash reconcile ignores disk state", R, {"applied_state": lambda self, root, plan: "original"},
      "compound_crash_plus_human_edit")
check("simulation always passes", R,
      {"_judge": lambda self, b, r: E.PhaseResult(passed=True, report=r, comparison=E.compare(b, r), problems=[])},
      "reject_regressing_plan")
# stage 20: one mutant per new safety property
check("policy guard allows everything", Guard, {"require": lambda self, *a, **k: None}, "unauthorized_operations")
check("integrity check always reports valid", Platform, {"verify_integrity": lambda self: {"valid": True}},
      "tamper_detection")
check("revalidation never marks anything stale", MemoryEngine,
      {"revalidate": lambda self, pid, root: {"checked": 0, "marked_stale": 0}}, "knowledge_staleness")
check("sync accepts any signature", S, {"verify": lambda *a, **k: True}, "distributed_sync")
check("sandbox memory limit not applied", W,
      {"_LAUNCHER": W._LAUNCHER.replace(
          "resource.setrlimit(resource.RLIMIT_DATA if kind=='data' else resource.RLIMIT_AS,(mem,mem))\n", "")},
      "sandbox_memory_limit")
print(f"{'all mutants caught' if not missed else f'{missed} mutant(s) MISSED'}")
sys.exit(1 if missed else 0)
