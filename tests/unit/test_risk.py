import pytest
from helpers import make_renamed_api_project

from yemendev.risk.analysis import (
    RunObs,
    dependency_risks,
    module_change_risk,
    predict_test_failures,
    repair_reliability,
    robust_anomalies,
)


def runs(*failing_sets):
    return [RunObs(failing=list(f), failed_count=len(f)) for f in failing_sets]


def test_no_prediction_without_enough_runs():
    preds, ins = predict_test_failures(runs({"t1"}, {"t1"}, set()))
    assert preds == [] and ins[0].available == "3 run(s)"


def test_flaky_and_persistent_failures():
    preds, ins = predict_test_failures(runs({"a", "b"}, {"a"}, {"a", "b"}, {"a"}))
    by = {p.subject: p for p in preds}
    assert ins == []
    assert by["a"].kind == "persistent_failure" and by["a"].probability == pytest.approx(5 / 6, abs=1e-3)
    assert by["b"].kind == "flaky_test" and by["b"].probability == pytest.approx(3 / 6, abs=1e-3)
    assert "3 outcome change(s)" in by["b"].supporting_observations[1]
    for p in preds:  # every prediction states its limits and the response
        assert p.limitations and p.recommended_response and p.horizon == "next test run"
        assert p.probability_kind == "empirical_frequency_uncalibrated"


def test_robust_anomalies():
    assert robust_anomalies("m", "s", [1, 2, 3])[1]  # insufficient
    assert robust_anomalies("m", "s", [10, 11, 10, 12, 11, 11.5])[0] == []  # normal variation
    [a], _ = robust_anomalies("m", "s", [10, 11, 10, 12, 11, 60])
    assert a.robust_z > 3.5 and a.classification == "operational"
    [c], _ = robust_anomalies("m", "s", [0, 0, 0, 0, 0, 3])
    assert c.robust_z is None and "constant" in c.explanation
    assert robust_anomalies("m", "s", [0, 0, 0, 0, 0, 0])[0] == []


def test_module_change_risk_ranking_and_churn_note():
    scores, notes = module_change_risk({"core": 4, "util": 1, "leaf": 0}, None, scans=1)
    assert [s.subject for s in scores] == ["core", "util"] and notes and not scores[0].calibrated
    assert all(0 <= s.score <= 1 for s in scores)  # regression: inconsistent inputs produced 2.0
    scores, notes = module_change_risk({"core": 4, "util": 1, "leaf": 0}, {"core": 0, "util": 3, "leaf": 1}, 5)
    assert notes == [] and scores[0].components["churn"] in (0.0, 1.0)


def test_dependency_risks():
    out = dependency_risks([("a", "a>=1"), ("b", "b==1.2"), ("c", "c>=1,<2"), ("d", 'd; python_version<"3.12"')],
                           [("undeclared_import", "import 'numpy' has no matching declared dependency", "inferred")])
    subjects = {(r.subject, r.severity) for r in out}
    assert ("a", "low") in subjects and ("d", "low") in subjects and ("numpy", "medium") in subjects
    assert not any(r.subject in ("b", "c") for r in out)


def test_npm_dependency_pinning_uses_npm_semantics():
    out = dependency_risks([("react", "react@^18.3.1", "npm"), ("left-pad", "left-pad@1.3.0", "npm"),
                            ("x", "x@latest", "npm"), ("y", "y@=2.0.0-rc.1", "npm"), ("@s/z", "@s/z@~1.2.0", "npm")],
                           [])
    assert sorted(r.subject for r in out) == ["npm:@s/z", "npm:react", "npm:x"]


def test_repair_reliability():
    assert repair_reliability([True])[1]
    [s], _ = repair_reliability([True, False, False, True])
    assert s.score == pytest.approx(3 / 6, abs=1e-3)


def test_causal_root_causes_and_propagation(tmp_path):
    from yemendev.core.config import SandboxSettings
    from yemendev.diagnostics.engine import diagnose
    from yemendev.sandbox.testrunner import run_pytest
    from yemendev.sandbox.workspace import Workspace
    from yemendev.twin.causal import failure_propagation, root_cause_candidates
    from yemendev.twin.graph import DependencyGraph
    from yemendev.twin.scanner import ProjectScanner
    root = make_renamed_api_project(tmp_path / "p")
    snap = ProjectScanner().scan(root)
    reports = []
    for _ in range(2):
        with Workspace(root, SandboxSettings(backend="process")) as ws:
            reports.append(run_pytest(ws))
    cands = root_cause_candidates(snap, diagnose(reports))
    by = {c.module: c for c in cands}
    assert by["calc.api"].certainty == "observed" and by["calc.mathutils"].certainty == "inferred"
    assert all(c.status == "candidate, not proven" for c in cands)
    paths = {p.module: p.path for p in failure_propagation(DependencyGraph.from_snapshot(snap), "calc.mathutils")}
    assert paths["calc.api"] == ["calc.mathutils", "calc.api"]
