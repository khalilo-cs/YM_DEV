from __future__ import annotations

from pathlib import Path

import pytest

from yemendev.core.contracts import new_id
from yemendev.storage.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "data" / "test.sqlite3")
    d.migrate()
    return d


@pytest.fixture
def project_row(db: Database, tmp_path: Path) -> str:
    """Insert a minimal project row (for FK-constrained tables) and return its id."""
    pid = new_id("prj")
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    with db.transaction() as conn:
        conn.execute("INSERT INTO projects(id, name, root_path, created_at, created_by) VALUES (?,?,?,?,?)",
                     (pid, "p", str(root), "2026-01-01T00:00:00.000000+00:00", "human:t"))
    return pid
