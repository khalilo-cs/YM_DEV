import sqlite3

import pytest

from yemendev.storage.db import Database
from yemendev.storage.migrations import MIGRATIONS


def test_migrate_is_idempotent(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    assert db.migrate() == [v for v, _, _ in MIGRATIONS]
    assert db.migrate() == []
    assert db.schema_version() == MIGRATIONS[-1][0]
    assert db.integrity_check() == "ok"


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO projects(id,name,root_path,created_at,created_by) VALUES ('a','a','/a','t','u')")
            raise RuntimeError("boom")
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO project_scans(id,project_id,created_at,tree_hash,snapshot_json)"
                         " VALUES ('s','missing','t','h','{}')")
