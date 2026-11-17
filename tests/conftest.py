from __future__ import annotations

from pathlib import Path

import pytest

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import new_id
from yemendev.core.faults import FaultInjector
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
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


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60))


@pytest.fixture
def faults():
    return FaultInjector()


@pytest.fixture
def platform(settings, faults):
    return Platform(settings, faults)


@pytest.fixture
def wf(platform):
    return RepairWorkflow(platform)
