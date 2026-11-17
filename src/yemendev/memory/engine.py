"""Living Memory Engine.

Key guarantees (tested in tests/unit/test_memory.py):
* A verified record is never silently overwritten. A new *verified* value with
  evidence supersedes it (old -> SUPERSEDED, version+1, link kept). A new *unverified*
  conflicting value is stored as CONTRADICTED and linked; the verified one stays.
* Records carry validity conditions (file content hashes). ``revalidate`` marks
  records STALE when a condition no longer holds; stale records are not returned as
  trusted knowledge.
* Every record keeps provenance (source, evidence ids, timestamps).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yemendev.core.contracts import (
    InvalidInputError,
    NotFoundError,
    canonical_json,
    iso,
    new_id,
    parse_iso,
    sha256_bytes,
    utcnow,
)
from yemendev.memory.model import (
    ACTIVE_STATUSES,
    MemoryCategory,
    MemoryRecord,
    MemoryStatus,
    MemoryWrite,
    ValidityCondition,
    WriteOutcome,
    WriteResult,
)
from yemendev.storage.db import Database


def _from_row(r: Any) -> MemoryRecord:
    return MemoryRecord(
        id=r["id"], project_id=r["project_id"], category=MemoryCategory(r["category"]), subject=r["subject"],
        key=r["key"], value=json.loads(r["value_json"]), status=MemoryStatus(r["status"]),
        confidence=r["confidence"], source=r["source"], evidence_ids=json.loads(r["evidence_ids_json"]),
        validity=[ValidityCondition(**v) for v in json.loads(r["validity_json"])], version=r["version"],
        supersedes=r["supersedes"], contradicts=json.loads(r["contradicts_json"]), created_at=r["created_at"],
        validated_at=r["validated_at"], revalidate_after=r["revalidate_after"],
    )


class MemoryEngine:
    def __init__(self, db: Database) -> None:
        self._db = db

    def write(self, w: MemoryWrite) -> WriteResult:
        if w.verified and not w.evidence_ids and not w.validity:
            raise InvalidInputError("a verified memory requires evidence ids or validity conditions")
        value_json = canonical_json(w.value)
        now = iso()
        with self._db.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM memory_records WHERE project_id IS ? AND subject = ? AND key = ? AND status IN (?, ?)"
                " ORDER BY version DESC",
                (w.project_id, w.subject, w.key, *[s.value for s in ACTIVE_STATUSES]),
            ).fetchall()
            verified = [r for r in current if r["status"] == MemoryStatus.VERIFIED.value]
            same = [r for r in current if r["value_json"] == value_json]

            if same and (not w.verified or same[0]["status"] == MemoryStatus.VERIFIED.value):
                r = same[0]
                conn.execute(
                    "UPDATE memory_records SET validated_at = ?, validity_json = ?, evidence_ids_json = ? WHERE id = ?",
                    (now, canonical_json([v.model_dump() for v in w.validity] or json.loads(r["validity_json"])),
                     canonical_json(sorted(set(json.loads(r["evidence_ids_json"])) | set(w.evidence_ids))), r["id"]),
                )
                rec = _from_row(conn.execute("SELECT * FROM memory_records WHERE id = ?", (r["id"],)).fetchone())
                return WriteResult(outcome=WriteOutcome.UNCHANGED, record=rec)

            version = (max((r["version"] for r in current), default=0)) + 1
            status = MemoryStatus.VERIFIED if w.verified else MemoryStatus.UNVERIFIED
            supersedes: str | None = None
            contradicts: list[str] = []
            outcome = WriteOutcome.CREATED

            if verified and not w.verified:
                # Unverified info must not displace verified knowledge.
                status = MemoryStatus.CONTRADICTED
                contradicts = [r["id"] for r in verified]
                outcome = WriteOutcome.CONTRADICTION_RECORDED
            elif current:
                # Either verified replaces anything, or unverified replaces unverified.
                supersedes = current[0]["id"]
                for r in current:
                    conn.execute("UPDATE memory_records SET status = ? WHERE id = ?",
                                 (MemoryStatus.SUPERSEDED.value, r["id"]))
                outcome = WriteOutcome.SUPERSEDED_PREVIOUS

            rid = new_id("mem")
            conn.execute(
                "INSERT INTO memory_records(id, project_id, category, subject, key, value_json, status, confidence,"
                " source, evidence_ids_json, validity_json, version, supersedes, contradicts_json, created_at,"
                " validated_at, revalidate_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, w.project_id, w.category.value, w.subject, w.key, value_json, status.value, w.confidence,
                 w.source, canonical_json(w.evidence_ids), canonical_json([v.model_dump() for v in w.validity]),
                 version, supersedes, canonical_json(contradicts), now, now if w.verified else None,
                 w.revalidate_after),
            )
            rec = _from_row(conn.execute("SELECT * FROM memory_records WHERE id = ?", (rid,)).fetchone())
        return WriteResult(outcome=outcome, record=rec, conflicting_ids=contradicts)

    def get(self, record_id: str) -> MemoryRecord:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM memory_records WHERE id = ?", (record_id,)).fetchone()
        if r is None:
            raise NotFoundError("memory record not found")
        return _from_row(r)

    def query(
        self, project_id: str | None, *, category: MemoryCategory | None = None, subject_prefix: str | None = None,
        include_inactive: bool = False, limit: int = 200,
    ) -> list[MemoryRecord]:
        q = "SELECT * FROM memory_records WHERE project_id IS ?"
        args: list[Any] = [project_id]
        if category:
            q += " AND category = ?"
            args.append(category.value)
        if subject_prefix:
            q += " AND subject LIKE ? ESCAPE '\\'"
            esc = subject_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(esc + "%")
        if not include_inactive:
            q += " AND status IN (?, ?)"
            args.extend(s.value for s in ACTIVE_STATUSES)
        q += " ORDER BY subject, key, version DESC LIMIT ?"
        args.append(max(1, min(limit, 5000)))
        with self._db.read() as conn:
            return [_from_row(r) for r in conn.execute(q, args).fetchall()]

    def history(self, project_id: str | None, subject: str, key: str) -> list[MemoryRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE project_id IS ? AND subject = ? AND key = ? ORDER BY version",
                (project_id, subject, key),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def relevant(self, project_id: str | None, terms: list[str], limit: int = 20) -> list[tuple[float, MemoryRecord]]:
        """Simple, explainable relevance: term overlap x confidence x status weight."""
        terms_l = [t.lower() for t in terms if t]
        scored: list[tuple[float, MemoryRecord]] = []
        for rec in self.query(project_id, limit=5000):
            hay = f"{rec.subject} {rec.key} {json.dumps(rec.value, default=str)}".lower()
            hits = sum(1 for t in terms_l if t in hay)
            if not hits:
                continue
            weight = 1.0 if rec.status is MemoryStatus.VERIFIED else 0.5
            scored.append((hits * rec.confidence * weight, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit]

    def revalidate(self, project_id: str, project_root: Path) -> dict[str, int]:
        """Check validity conditions; mark failing records STALE. Returns counts."""
        root = project_root.resolve()
        stats = {"checked": 0, "still_valid": 0, "marked_stale": 0, "due_for_review": 0}
        now = utcnow()
        for rec in self.query(project_id, limit=5000):
            stats["checked"] += 1
            if rec.revalidate_after and parse_iso(rec.revalidate_after) <= now:
                stats["due_for_review"] += 1
            if not rec.validity:
                continue
            if all(_condition_holds(c, root) for c in rec.validity):
                stats["still_valid"] += 1
                continue
            with self._db.transaction() as conn:
                conn.execute("UPDATE memory_records SET status = ? WHERE id = ? AND status IN (?, ?)",
                             (MemoryStatus.STALE.value, rec.id, *[s.value for s in ACTIVE_STATUSES]))
            stats["marked_stale"] += 1
        return stats

    def deprecate(self, record_id: str) -> MemoryRecord:
        with self._db.transaction() as conn:
            cur = conn.execute("UPDATE memory_records SET status = ? WHERE id = ?",
                               (MemoryStatus.DEPRECATED.value, record_id))
            if cur.rowcount == 0:
                raise NotFoundError("memory record not found")
        return self.get(record_id)


def _condition_holds(c: ValidityCondition, root: Path) -> bool:
    target = (root / c.path).resolve()
    if not target.is_relative_to(root):
        return False
    if c.kind == "file_absent":
        return not target.exists()
    if not target.is_file():
        return False
    return sha256_bytes(target.read_bytes()) == c.sha256
