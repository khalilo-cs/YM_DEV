"""Record a scan into Living Memory with validity conditions bound to file hashes.

Facts read directly from files are VERIFIED with a ``file_sha256`` validity condition
(so they go STALE automatically when the file changes). Heuristic findings are stored
as UNVERIFIED observations.
"""

from __future__ import annotations

from yemendev.memory.engine import MemoryEngine
from yemendev.memory.model import MemoryCategory, MemoryWrite, ValidityCondition
from yemendev.twin.model import Certainty, ProjectSnapshot

SOURCE = "scanner"


def record_scan(memory: MemoryEngine, project_id: str, snap: ProjectSnapshot, evidence_id: str) -> dict[str, int]:
    hashes = {f.path: f.sha256 for f in snap.files}
    counts: dict[str, int] = {}

    def put(w: MemoryWrite) -> None:
        r = memory.write(w)
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1

    put(MemoryWrite(project_id=project_id, category=MemoryCategory.PROJECT_STRUCTURE, subject="project",
                    key="languages", value=snap.languages, verified=True, confidence=1.0, source=SOURCE,
                    evidence_ids=[evidence_id]))
    for m in snap.modules:
        put(MemoryWrite(project_id=project_id, category=MemoryCategory.PROJECT_STRUCTURE, subject=f"module:{m.name}",
                        key="path", value=m.path, verified=True, confidence=1.0, source=SOURCE,
                        evidence_ids=[evidence_id],
                        validity=[ValidityCondition(kind="file_sha256", path=m.path, sha256=hashes[m.path])]))
    for d in snap.declared_dependencies:
        dep_subject = f"dependency:{d.name}" if d.ecosystem == "pypi" else f"dependency:{d.ecosystem}:{d.name}"
        cond = [ValidityCondition(kind="file_sha256", path=d.source_file, sha256=hashes[d.source_file])] \
            if d.source_file in hashes else []
        put(MemoryWrite(project_id=project_id, category=MemoryCategory.DEPENDENCY, subject=dep_subject,
                        key=f"declared:{d.group}", value=d.spec, verified=bool(cond), confidence=1.0 if cond else 0.6,
                        source=SOURCE, evidence_ids=[evidence_id], validity=cond))
    for f in snap.findings:
        put(MemoryWrite(project_id=project_id, category=MemoryCategory.OBSERVATION, subject=f"finding:{f.kind}",
                        key=f.path or f.message[:200], value=f.model_dump(), verified=False,
                        confidence=0.9 if f.certainty is Certainty.OBSERVED else 0.5, source=SOURCE,
                        evidence_ids=[evidence_id]))
    return counts
