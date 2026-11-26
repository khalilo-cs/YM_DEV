"""Gathers recorded evidence and scans for a project and runs the pure analyses."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from yemendev.risk.analysis import (
    MIN_SCANS_FOR_CHURN,
    RunObs,
    dependency_risks,
    module_change_risk,
    predict_test_failures,
    repair_reliability,
    robust_anomalies,
)
from yemendev.risk.model import RiskReport
from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import ProjectSnapshot

if TYPE_CHECKING:
    from yemendev.platform import Platform

COMPLETED = {"all_passed", "tests_failed"}


def assess(p: Platform, project_id: str) -> RiskReport:
    with p.db.read() as conn:
        ev = conn.execute("SELECT action, outcome, body_json FROM evidence_records WHERE target = ? AND action IN"
                          " ('tests.baseline', 'repair.verify') ORDER BY created_at, rowid", (project_id,)).fetchall()
        scans = [ProjectSnapshot(**json.loads(r["snapshot_json"])) for r in conn.execute(
            "SELECT snapshot_json FROM project_scans WHERE project_id = ? ORDER BY created_at, rowid", (project_id,))]
    runs: list[RunObs] = []
    repairs: list[bool] = []
    for r in ev:
        body = json.loads(r["body_json"])
        if r["action"] == "tests.baseline":
            for run in (body.get("tests") or {}).get("runs", []):
                if run.get("status") in COMPLETED:
                    runs.append(RunObs(failing=run.get("failing", []), failed_count=run.get("failed", 0)
                                       + run.get("errors", 0), duration_seconds=run.get("duration_seconds")))
        else:
            repairs.append(r["outcome"] == "passed")

    predictions, insufficient = predict_test_failures(runs)
    anomalies = []
    a, i = robust_anomalies("failing tests per run", project_id, [float(x.failed_count) for x in runs])
    anomalies += a
    insufficient += i
    durations = [x.duration_seconds for x in runs if x.duration_seconds is not None]
    a, i = robust_anomalies("test suite duration (s)", project_id, durations)
    anomalies += a
    insufficient += i

    scores = []
    deps = []
    if scans:
        latest = scans[-1]
        g = DependencyGraph.from_snapshot(latest)
        churn = None
        if len(scans) >= MIN_SCANS_FOR_CHURN:
            recent = scans[-10:]
            churn = {}
            for m in latest.modules:
                hashes = [next((f.sha256 for f in s.files if f.path == m.path), None) for s in recent]
                churn[m.name] = sum(1 for x, y in zip(hashes, hashes[1:], strict=False) if x != y)
        s, i = module_change_risk(g.transitive_dependent_counts(), churn, len(scans))
        scores += s
        insufficient += i
        deps = dependency_risks([(d.name, d.spec, d.ecosystem) for d in latest.declared_dependencies],
                                [(f.kind, f.message, f.certainty.value) for f in latest.findings])
    s, i = repair_reliability(repairs)
    scores += s
    insufficient += i
    return RiskReport(
        project_id=project_id, predictions=predictions, risk_scores=scores, dependency_risks=deps,
        anomalies=anomalies, insufficient_evidence=insufficient,
        limitations=["explainable statistics over this project's recorded history only",
                     "probabilities are smoothed frequencies, not calibrated",
                     "module risk is static structure x change frequency, not observed failure causation"])
