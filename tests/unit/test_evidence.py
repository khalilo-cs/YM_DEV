from yemendev.evidence.store import EvidenceBody, EvidenceStore


def test_record_roundtrip_and_tamper_detection(db):
    s = EvidenceStore(db)
    rec = s.record(action="repair.verify", actor="system:y", target="prj", outcome="passed", task_id="t1",
                   body=EvidenceBody(tests={"passed": 3}, limitations=["static only"],
                                     details={"secret": "ydv_" + "a" * 30}))
    got = s.get(rec.id)
    assert got.hash_valid and got.body.tests == {"passed": 3}
    assert "aaaa" not in str(got.body.details)
    assert got.body.environment["python"]
    assert [r.id for r in s.list(task_id="t1")] == [rec.id]
    with db.transaction() as conn:
        conn.execute("UPDATE evidence_records SET outcome='failed' WHERE id=?", (rec.id,))
    assert not s.get(rec.id).hash_valid
