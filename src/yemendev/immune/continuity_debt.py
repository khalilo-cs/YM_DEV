"""Continuity Debt - EXPERIMENTAL metric (directive 6.6).

Not a validated scientific metric: its definition, reliability and usefulness have not been evaluated.
Each component is 0 (no debt) .. 1 (high debt) and carries the evidence it was computed from.
Components that cannot be measured yet are listed as such and EXCLUDED from the score - never guessed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from yemendev.platform import Platform


class DebtComponent(BaseModel):
    name: str
    value: float | None  # None = not measurable yet
    evidence: str


class ContinuityDebt(BaseModel):
    project_id: str
    status: str = "EXPERIMENTAL - not a validated metric"
    score: float | None
    measured_components: int
    components: list[DebtComponent]


def assess_debt(p: Platform, project_id: str) -> ContinuityDebt:
    root = p.project_root(project_id)
    _, snap = p.latest_scan(project_id)
    comps: list[DebtComponent] = []

    has_readme = any(f.path.lower().startswith("readme") for f in snap.files)
    doc_files = [f for f in snap.files if f.language in ("markdown", "restructuredtext")]
    comps.append(DebtComponent(name="missing_documentation",
                               value=0.0 if has_readme and len(doc_files) >= 3 else (0.5 if has_readme else 1.0),
                               evidence=f"README present: {has_readme}; {len(doc_files)} documentation file(s)"))

    with p.db.read() as conn:
        verified = conn.execute("SELECT COUNT(*) FROM evidence_records WHERE target = ? AND action = 'repair.verify'",
                                (project_id,)).fetchone()[0]
        baselines = [json.loads(r["body_json"]) for r in conn.execute(
            "SELECT body_json FROM evidence_records WHERE target = ? AND action = 'tests.baseline'", (project_id,))]
    comps.append(DebtComponent(name="untested_recovery", value=0.0 if verified else 1.0,
                               evidence=f"{verified} verified recovery operation(s) recorded"))

    comps.append(DebtComponent(name="unverified_backups", value=None,
                               evidence="not measurable: the platform has no backup verification feature yet"))

    externals = sorted({e.target for e in snap.edges if e.kind.value == "external_import"})
    undeclared = [f for f in snap.findings if f.kind == "undeclared_import"]
    unpinned = [d for d in snap.declared_dependencies
                if not any(op in d.spec.split(";")[0] for op in ("==", "<", "~="))]
    n_dep = max(len(externals) + len(snap.declared_dependencies), 1)
    comps.append(DebtComponent(name="unmanaged_dependencies",
                               value=round(min(1.0, (len(undeclared) + len(unpinned)) / n_dep), 3),
                               evidence=f"{len(undeclared)} undeclared import(s), {len(unpinned)} unpinned of "
                                        f"{len(snap.declared_dependencies)} declared"))

    members = p.members.list(project_id)
    operators = [m for m in members if m.role.value in ("operator", "admin")]
    has_codeowners = (root / "CODEOWNERS").is_file() or (root / ".github" / "CODEOWNERS").is_file()
    comps.append(DebtComponent(name="missing_ownership",
                               value=0.0 if has_codeowners or len(operators) >= 2 else 1.0,
                               evidence=f"CODEOWNERS: {has_codeowners}; {len(operators)} member(s) able to operate"))

    runbook = any(k in f.path.lower() for f in doc_files for k in ("runbook", "operation", "recovery",
                                                                    "troubleshoot", "deploy"))
    comps.append(DebtComponent(name="incomplete_operational_knowledge", value=0.0 if runbook else 1.0,
                               evidence="runbook/operations/recovery documentation found" if runbook
                               else "no runbook, operations, recovery or troubleshooting document found"))

    completed = sum(1 for b in baselines for r in (b.get("tests") or {}).get("runs", [])
                    if r.get("status") in ("all_passed", "tests_failed"))
    weak = 1.0 if snap.test_framework is None else (0.0 if completed else 0.5)
    comps.append(DebtComponent(name="weak_restart_procedures", value=weak,
                               evidence=f"test framework: {snap.test_framework}; "
                                        f"{completed} completed sandboxed run(s)"))

    measured = [c.value for c in comps if c.value is not None]
    return ContinuityDebt(project_id=project_id, score=round(sum(measured) / len(measured), 3) if measured else None,
                          measured_components=len(measured), components=comps)
