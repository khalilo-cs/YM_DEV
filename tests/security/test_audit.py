from yemendev.security.audit import GENESIS, AuditLog


def make(db):
    return AuditLog(db, b"k" * 32)


def test_chain_verifies(db):
    log = make(db)
    for i in range(5):
        log.record(actor="human:a", action="x", target=f"t{i}", outcome="ok", details={"i": i})
    v = log.verify()
    assert v.valid and v.events_checked == 5 and v.head_hash != GENESIS
    assert log.verify(expected_head=log.head()).valid


def test_detects_modification(db):
    log = make(db)
    log.record(actor="human:a", action="x", target="t", outcome="ok")
    log.record(actor="human:a", action="y", target="t", outcome="ok")
    with db.transaction() as conn:
        conn.execute("UPDATE audit_events SET outcome='denied' WHERE seq=1")
    v = log.verify()
    assert not v.valid and any("modified" in p for p in v.problems)


def test_detects_deletion(db):
    log = make(db)
    for _ in range(3):
        log.record(actor="human:a", action="x", target="t", outcome="ok")
    with db.transaction() as conn:
        conn.execute("DELETE FROM audit_events WHERE seq=2")
    assert not log.verify().valid


def test_detects_truncation_against_recorded_head(db):
    log = make(db)
    log.record(actor="human:a", action="x", target="t", outcome="ok")
    log.record(actor="human:a", action="y", target="t", outcome="ok")
    head = log.head()
    with db.transaction() as conn:
        conn.execute("DELETE FROM audit_events WHERE seq=2")
    assert log.verify().valid  # undetectable without an external head...
    assert not log.verify(expected_head=head).valid  # ...detected with it


def test_wrong_key_fails_verification(db):
    make(db).record(actor="human:a", action="x", target="t", outcome="ok")
    assert not AuditLog(db, b"z" * 32).verify().valid


def test_details_are_redacted(db):
    log = make(db)
    log.record(actor="human:a", action="x", target="t", outcome="ok", details={"token": "ydv_" + "a" * 40})
    assert "aaaa" not in str(log.list()[0].details)
