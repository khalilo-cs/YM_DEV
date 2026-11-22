"""Pure, explainable risk analyses. Each returns results OR an InsufficientEvidence entry - never a guess."""

from __future__ import annotations

import re
import statistics

from pydantic import BaseModel

from yemendev.risk.model import Anomaly, DependencyRisk, InsufficientEvidence, Prediction, RiskScore

MIN_RUNS_FOR_PREDICTION = 4
MIN_POINTS_FOR_ANOMALY = 5
MIN_SCANS_FOR_CHURN = 3
MIN_REPAIRS_FOR_TREND = 3
MODIFIED_Z_THRESHOLD = 3.5  # Iglewicz & Hoaglin


class RunObs(BaseModel):
    """One completed test run, as recorded in baseline evidence."""
    failing: list[str]
    failed_count: int
    duration_seconds: float | None = None


def predict_test_failures(runs: list[RunObs]) -> tuple[list[Prediction], list[InsufficientEvidence]]:
    if len(runs) < MIN_RUNS_FOR_PREDICTION:
        return [], [InsufficientEvidence(analysis="test failure prediction",
                                         needed=f">= {MIN_RUNS_FOR_PREDICTION} completed test runs",
                                         available=f"{len(runs)} run(s)")]
    n = len(runs)
    tests = sorted({t for r in runs for t in r.failing})
    preds = []
    for t in tests:
        seq = [t in r.failing for r in runs]
        k = sum(seq)
        flips = sum(1 for a, b in zip(seq, seq[1:], strict=False) if a != b)
        p = (k + 1) / (n + 2)  # Laplace-smoothed frequency
        flaky = 0 < k < n
        preds.append(Prediction(
            kind="flaky_test" if flaky else "persistent_failure", subject=t,
            event=f"test {t} fails on the next run", probability=round(p, 3), horizon="next test run",
            supporting_observations=[f"failed in {k} of {n} recorded runs", f"{flips} outcome change(s)",
                                     f"last run: {'failed' if seq[-1] else 'passed'}"],
            limitations=["assumes the code and environment do not change before the next run",
                         "Laplace-smoothed frequency over recorded runs; not a calibrated probability"],
            recommended_response=("investigate nondeterminism (timing, ordering, shared state) before trusting "
                                  "this test for verification") if flaky else "open a repair task"))
    return preds, []


def robust_anomalies(
    metric: str, subject: str, series: list[float]
) -> tuple[list[Anomaly], list[InsufficientEvidence]]:
    """Flag the latest value if its modified z-score vs the history exceeds 3.5 (median/MAD based)."""
    if len(series) < MIN_POINTS_FOR_ANOMALY + 1:
        return [], [InsufficientEvidence(analysis=f"anomaly detection: {metric}",
                                         needed=f">= {MIN_POINTS_FOR_ANOMALY + 1} observations",
                                         available=f"{len(series)}")]
    history, latest = series[:-1], series[-1]
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history])
    if mad == 0:
        if latest == med:
            return [], []
        return [Anomaly(metric=metric, subject=subject, observed=latest, baseline_median=med, baseline_mad=0.0,
                        robust_z=None, explanation=f"{metric} was constant at {med:g} in {len(history)} prior "
                        f"observations and is now {latest:g}")], []
    z = 0.6745 * (latest - med) / mad
    if abs(z) <= MODIFIED_Z_THRESHOLD:
        return [], []
    return [Anomaly(metric=metric, subject=subject, observed=latest, baseline_median=med, baseline_mad=mad,
                    robust_z=round(z, 2), explanation=f"latest {metric} {latest:g} deviates from median {med:g} "
                    f"(modified z = {z:.1f}, threshold {MODIFIED_Z_THRESHOLD})")], []


def module_change_risk(transitive_dependents: dict[str, int], churn: dict[str, int] | None,
                       scans: int, top: int = 10) -> tuple[list[RiskScore], list[InsufficientEvidence]]:
    """Blast radius (static) x churn (how often the file changed across scans). Uncalibrated ranking."""
    notes = []
    if not transitive_dependents:
        return [], [InsufficientEvidence(analysis="module change risk", needed="a scanned project with modules",
                                         available="no modules")]
    # normalise so the score stays in [0, 1] even for inconsistent inputs (regression: score was 2.0)
    n = max(len(transitive_dependents) - 1, max(transitive_dependents.values(), default=0), 1)
    max_churn = max(churn.values(), default=0) if churn else 0
    if churn is None:
        notes.append(InsufficientEvidence(analysis="change frequency (churn)",
                                          needed=f">= {MIN_SCANS_FOR_CHURN} scans",
                                          available=f"{scans} scan(s); score uses blast radius only"))
    scores = []
    for mod, deps in transitive_dependents.items():
        impact = deps / n
        ch = (churn.get(mod, 0) / max_churn) if churn and max_churn else 0.0
        score = impact if churn is None else 0.6 * impact + 0.4 * ch
        if score <= 0:
            continue
        scores.append(RiskScore(kind="module_change_risk", subject=mod, score=round(score, 3),
                                components={"blast_radius": round(impact, 3), "churn": round(ch, 3)},
                                explanation=f"{deps} module(s) transitively import it"
                                + (f"; changed in {churn.get(mod, 0)} of the recent scans" if churn else "")))
    scores.sort(key=lambda s: (-s.score, s.subject))
    return scores[:top], notes


_PINNED = re.compile(r"(==|~=|<=|<(?!=))")


def dependency_risks(declared: list[tuple[str, str]], findings: list[tuple[str, str, str]]) -> list[DependencyRisk]:
    """declared: (name, spec); findings: (kind, message, certainty) from the scanner."""
    out = [DependencyRisk(subject=name, severity="low", certainty="observed",
                          reason=f"unpinned requirement '{spec}': a new upstream release can change behaviour")
           for name, spec in declared if not _PINNED.search(spec.split(";", 1)[0])]  # ignore env markers
    for kind, message, certainty in findings:
        if kind == "undeclared_import":
            out.append(DependencyRisk(subject=message.split("'")[1] if "'" in message else message,
                                      severity="medium", certainty=certainty, reason=message))
    return out


def repair_reliability(outcomes: list[bool]) -> tuple[list[RiskScore], list[InsufficientEvidence]]:
    """outcomes: verification results of applied repairs, oldest first."""
    if len(outcomes) < MIN_REPAIRS_FOR_TREND:
        return [], [InsufficientEvidence(analysis="repair reliability",
                                         needed=f">= {MIN_REPAIRS_FOR_TREND} applied repairs",
                                         available=f"{len(outcomes)}")]
    fail = outcomes.count(False)
    rate = (fail + 1) / (len(outcomes) + 2)
    recent = outcomes[-3:]
    return [RiskScore(kind="repair_failure_rate", subject="project", score=round(rate, 3),
                      components={"failed": float(fail), "total": float(len(outcomes)),
                                  "recent_failed": float(recent.count(False))},
                      explanation=f"{fail} of {len(outcomes)} applied repairs failed verification and were rolled "
                                  "back (Laplace-smoothed)")], []
