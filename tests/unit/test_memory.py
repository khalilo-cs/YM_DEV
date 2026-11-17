import pytest

from yemendev.core.contracts import InvalidInputError, sha256_bytes
from yemendev.memory.engine import MemoryEngine
from yemendev.memory.model import (
    MemoryCategory,
    MemoryStatus,
    MemoryWrite,
    ValidityCondition,
    WriteOutcome,
)


def w(pid, value, verified=False, **kw):
    base = dict(project_id=pid, category=MemoryCategory.DEPENDENCY, subject="dep:requests", key="version",
                value=value, verified=verified, source="test",
                evidence_ids=["evd_1"] if verified else [])
    base.update(kw)
    return MemoryWrite(**base)


def test_verified_requires_evidence(db, project_row):
    with pytest.raises(InvalidInputError):
        MemoryEngine(db).write(w(project_row, "1", verified=True, evidence_ids=[]))


def test_unverified_cannot_overwrite_verified(db, project_row):
    m = MemoryEngine(db)
    v = m.write(w(project_row, "2.31", verified=True)).record
    r = m.write(w(project_row, "9.99"))
    assert r.outcome is WriteOutcome.CONTRADICTION_RECORDED
    assert r.record.status is MemoryStatus.CONTRADICTED and r.conflicting_ids == [v.id]
    assert m.get(v.id).status is MemoryStatus.VERIFIED
    active = m.query(project_row)
    assert [a.value for a in active] == ["2.31"]


def test_verified_supersedes_with_history(db, project_row):
    m = MemoryEngine(db)
    a = m.write(w(project_row, "1", verified=True)).record
    b = m.write(w(project_row, "2", verified=True)).record
    assert b.supersedes == a.id and b.version == 2
    assert m.get(a.id).status is MemoryStatus.SUPERSEDED
    assert [h.value for h in m.history(project_row, "dep:requests", "version")] == ["1", "2"]


def test_same_value_is_unchanged(db, project_row):
    m = MemoryEngine(db)
    a = m.write(w(project_row, "1", verified=True)).record
    r = m.write(w(project_row, "1", verified=True, evidence_ids=["evd_2"]))
    assert r.outcome is WriteOutcome.UNCHANGED and r.record.id == a.id
    assert r.record.evidence_ids == ["evd_1", "evd_2"]


def test_revalidate_marks_stale_when_file_changes(db, project_row, tmp_path):
    root = tmp_path / "proj"
    f = root / "mod.py"
    f.write_text("x = 1\n")
    m = MemoryEngine(db)
    rec = m.write(w(project_row, "fact", verified=True, evidence_ids=[],
                    validity=[ValidityCondition(kind="file_sha256", path="mod.py",
                                                sha256=sha256_bytes(f.read_bytes()))])).record
    assert m.revalidate(project_row, root)["marked_stale"] == 0
    f.write_text("x = 2\n")
    assert m.revalidate(project_row, root)["marked_stale"] == 1
    assert m.get(rec.id).status is MemoryStatus.STALE
    assert m.query(project_row) == []


def test_validity_path_cannot_escape_root(db, project_row, tmp_path):
    m = MemoryEngine(db)
    m.write(w(project_row, "x", verified=True, evidence_ids=[],
              validity=[ValidityCondition(kind="file_absent", path="../../etc/passwd")]))
    assert m.revalidate(project_row, tmp_path / "proj")["marked_stale"] == 1


def test_like_wildcards_escaped(db, project_row):
    m = MemoryEngine(db)
    m.write(w(project_row, "1", subject="a_b"))
    m.write(w(project_row, "1", subject="axb"))
    assert [r.subject for r in m.query(project_row, subject_prefix="a_")] == ["a_b"]
