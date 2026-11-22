"""Immune check for one project: compares the live tree with the baseline scan (read-only, no code runs)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from yemendev.immune.model import ImmuneFinding, ImmuneReport
from yemendev.immune.rules import classify_changed_file, classify_new_file, denial_bursts, typosquat_candidates

if TYPE_CHECKING:
    from yemendev.platform import Platform


def check(p: Platform, project_id: str, audit_window: int = 1000) -> ImmuneReport:
    root = p.project_root(project_id)
    findings: list[ImmuneFinding] = []
    try:
        baseline_id, baseline = p.latest_scan(project_id)
    except Exception:  # noqa: BLE001 - no baseline yet is a normal state, reported as such
        baseline_id, baseline = None, None
    current = p.scanner.scan(root)

    with p.db.read() as conn:
        applied: set[str] = set()
        for r in conn.execute("SELECT body_json FROM evidence_records WHERE target = ? AND action = 'repair.execute'",
                              (project_id,)):
            applied.update(v for v in (json.loads(r["body_json"]).get("after_state") or {}).values() if v)

    if baseline is not None:
        before = {f.path: f.sha256 for f in baseline.files}
        now = {f.path: f.sha256 for f in current.files}
        for path in sorted(now):
            if path not in before:
                findings.append(classify_new_file(path))
            elif now[path] != before[path]:
                findings.append(classify_changed_file(path, authorized=now[path] in applied))
        for path in sorted(set(before) - set(now)):
            findings.append(ImmuneFinding(rule="file-removed", category="operational", severity="low",
                                          certainty="observed", subject=path, detail="file removed since baseline",
                                          recommended_action="rescan"))
        old_deps = {d.name for d in baseline.declared_dependencies}
        for d in current.declared_dependencies:
            if d.name not in old_deps:
                findings.append(ImmuneFinding(rule="dependency-added", category="operational", severity="info",
                                              certainty="observed", subject=d.name,
                                              detail=f"new declared dependency '{d.spec}' in {d.source_file}",
                                              recommended_action="review; rebuild the project sandbox image"))
        new_secrets = sorted(set(current.sensitive_files) - set(baseline.sensitive_files))
        for path in new_secrets:
            findings.append(ImmuneFinding(rule="secret-file-added", category="security", severity="medium",
                                          certainty="inferred", subject=path,
                                          detail="new file that looks like a secret (contents not read)",
                                          recommended_action="make sure it is not committed or shared"))
    findings += typosquat_candidates(sorted({d.name for d in current.declared_dependencies}))

    integ = p.verify_integrity()
    if not integ["valid"]:
        findings.append(ImmuneFinding(rule="platform-integrity-violation", category="security", severity="high",
                                      certainty="observed", subject="audit/evidence",
                                      detail="; ".join(integ["problems"][:5]),
                                      recommended_action="treat platform records as untrusted; investigate access "
                                                         "to the data directory"))
    denials = [(e.actor, e.details.get("project_id")) for e in p.audit.list(limit=audit_window)
               if e.outcome == "denied" and (e.details.get("project_id") in (project_id, None))]
    findings += denial_bursts(denials)

    summary = {"security": sum(f.category == "security" for f in findings),
               "operational": sum(f.category == "operational" for f in findings),
               "high": sum(f.severity == "high" for f in findings)}
    return ImmuneReport(
        project_id=project_id, baseline_scan_id=baseline_id, findings=findings, summary=summary,
        limitations=(["no baseline scan yet: drift cannot be assessed"] if baseline is None else [])
        + ["typosquatting check uses a small offline list and edit distance (inferred, not proof)",
           "operational anomalies are not treated as attacks; security findings are indicators, not verdicts",
           f"permission-denial correlation covers the last {audit_window} audit events"])
