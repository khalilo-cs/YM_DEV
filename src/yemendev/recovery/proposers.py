"""Repair proposers. Proposals are untrusted until simulated, approved and verified."""

from __future__ import annotations

from pathlib import Path

from yemendev.core.contracts import new_id, sha256_bytes
from yemendev.diagnostics.engine import DiagnosticReport
from yemendev.learning.skills import SkillEngine, SkillStatus, apply_token_rename, is_test_path
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.twin.model import ProjectSnapshot


def propose_from_skills(
    skills: SkillEngine, diag: DiagnosticReport, snap: ProjectSnapshot, root: Path,
    python_version: tuple[int, int],
) -> list[RepairPlan]:
    plans: list[RepairPlan] = []
    active = skills.list(SkillStatus.ACTIVE)
    for h in diag.hypotheses:
        for s in active:
            if not skills.matches(s, h.category, h.signals):
                continue
            ok, _ = skills.compatible(s, snap.languages, python_version)
            if not ok:
                continue
            patches = []
            for f in snap.files:
                if f.language != "python" or is_test_path(f.path):
                    continue
                p = root / f.path
                if p.is_symlink() or not p.is_file():
                    continue
                data = p.read_bytes()
                if sha256_bytes(data) != f.sha256:
                    continue  # changed since scan; skip rather than patch stale content
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new, n = apply_token_rename(s.spec.params, text)
                if n:
                    patches.append(FilePatch(path=f.path, original_sha256=f.sha256, new_content=new))
            if patches:
                plans.append(RepairPlan(
                    id=new_id("pln"), description=f"Apply skill '{s.spec.name}' (v{s.version})",
                    source=PlanSource.SKILL, source_ref=f"{s.id}@{s.version}", hypothesis_id=h.id,
                    patches=patches, rationale=f"Skill preconditions match hypothesis {h.id}: {h.statement}",
                ))
    return plans
