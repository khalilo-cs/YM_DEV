from yemendev.benchmarks.resilience import run_benchmark
from yemendev.recovery import engine as E

def check(label, patch, scenario):
    orig = {k: getattr(E.RecoveryEngine, k) for k in patch}
    for k, v in patch.items(): setattr(E.RecoveryEngine, k, v)
    try:
        [r] = run_benchmark(seed=7, only=[scenario]).results
    finally:
        for k, v in orig.items(): setattr(E.RecoveryEngine, k, v)
    caught = not r.correct
    print(f"{'CAUGHT ' if caught else 'MISSED!'} mutant '{label}' -> {scenario}: status={r.actual_status} notes={r.integrity_notes}")

check("rollback is a no-op", {"rollback": lambda self, cid: []}, "compound_drift_then_rollback")
check("crash reconcile ignores disk state", {"applied_state": lambda self, root, plan: "original"},
      "compound_crash_plus_human_edit")
check("simulation always passes", {"_judge": lambda self, b, r: E.PhaseResult(passed=True, report=r, comparison=E.compare(b, r), problems=[])},
      "reject_regressing_plan")
