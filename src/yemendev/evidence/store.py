"""Evidence Engine: append-only, content-hashed records of important actions."""

from __future__ import annotations

import json
import platform
import sys
from typing import Any

from pydantic import BaseModel, Field

from yemendev.core.contracts import NotFoundError, canonical_json, iso, new_id, sha256_text
from yemendev.security.redaction import redact
from yemendev.storage.db import Database


class EvidenceBody(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    permission: dict[str, Any] | None = None
    tests: dict[str, Any] | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(BaseModel):
    id: str
    task_id: str | None
    action: str
    actor: str
    target: str
    created_at: str
    outcome: str
    body: EvidenceBody
    content_hash: str
    hash_valid: bool = True


def runtime_environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


class EvidenceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self, *, action: str, actor: str, target: str, outcome: str, body: EvidenceBody,
        task_id: str | None = None,
    ) -> EvidenceRecord:
        if not body.environment:
            body.environment = runtime_environment()
        body_json = canonical_json(redact(body.model_dump()))
        eid = new_id("evd")
        created = iso()
        content_hash = sha256_text(canonical_json([eid, task_id, action, actor, target, created, outcome, body_json]))
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO evidence_records(id, task_id, action, actor, target, created_at, outcome, body_json,"
                " content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, task_id, action, actor, target, created, outcome, body_json, content_hash),
            )
        return self.get(eid)

    def _from_row(self, r: Any) -> EvidenceRecord:
        expected = sha256_text(canonical_json([r["id"], r["task_id"], r["action"], r["actor"], r["target"],
                                               r["created_at"], r["outcome"], r["body_json"]]))
        return EvidenceRecord(
            id=r["id"], task_id=r["task_id"], action=r["action"], actor=r["actor"], target=r["target"],
            created_at=r["created_at"], outcome=r["outcome"], body=EvidenceBody(**json.loads(r["body_json"])),
            content_hash=r["content_hash"], hash_valid=expected == r["content_hash"],
        )

    def get(self, evidence_id: str) -> EvidenceRecord:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM evidence_records WHERE id = ?", (evidence_id,)).fetchone()
        if r is None:
            raise NotFoundError("evidence not found")
        return self._from_row(r)

    def list(self, task_id: str | None = None, limit: int = 100) -> list[EvidenceRecord]:
        limit = max(1, min(limit, 1000))
        with self._db.read() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM evidence_records WHERE task_id = ? ORDER BY created_at LIMIT ?", (task_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM evidence_records ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._from_row(r) for r in rows]
