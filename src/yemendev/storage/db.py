"""SQLite access layer.

Design (see docs/adr/0002-sqlite-stdlib-storage.md):
* one short-lived connection per unit of work (safe across FastAPI worker threads);
* ``BEGIN IMMEDIATE`` write transactions so concurrent writers serialize instead of
  interleaving read-modify-write sequences;
* WAL journal + ``synchronous=FULL`` for durability across power loss;
* foreign keys enforced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from yemendev.core.contracts import iso
from yemendev.storage.migrations import MIGRATIONS


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def migrate(self) -> list[int]:
        """Apply pending migrations. Returns versions applied in this call."""
        applied: list[int] = []
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        for version, name, sql in MIGRATIONS:
            with self.transaction() as conn:
                row = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if row:
                    continue
                # executescript would COMMIT implicitly; run statements individually
                # so the migration and its bookkeeping row are atomic.
                for statement in _split_sql(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, iso()),
                )
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"] or 0)

    def integrity_check(self) -> str:
        with self.read() as conn:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _split_sql(script: str) -> list[str]:
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
