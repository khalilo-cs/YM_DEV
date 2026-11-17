"""Tamper-evident audit log.

Each event stores ``hash = HMAC(key, prev_hash || canonical(event))``. Verification
walks the chain and detects edits, deletions (gaps / broken links) and reordering.

Limitations (documented in docs/THREAT_MODEL.md): an attacker who can read the
HMAC key file *and* write the database can forge a consistent chain; truncation of
the newest events is only detectable against an externally recorded head hash
(``head()`` is exposed for that purpose).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel

from yemendev.core.contracts import canonical_json, iso, new_id
from yemendev.security.redaction import redact
from yemendev.storage.db import Database

GENESIS = "0" * 64


class AuditEvent(BaseModel):
    seq: int
    id: str
    created_at: str
    actor: str
    action: str
    target: str
    outcome: str
    details: dict[str, Any]
    prev_hash: str
    hash: str


class AuditVerification(BaseModel):
    valid: bool
    events_checked: int
    head_hash: str
    problems: list[str]


class AuditLog:
    def __init__(self, db: Database, key: bytes) -> None:
        self._db = db
        self._key = key

    def _compute(self, prev_hash: str, body: dict[str, Any]) -> str:
        msg = (prev_hash + canonical_json(body)).encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def record(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        details = redact(details or {})
        event_id = new_id("aud")
        created = iso()
        with self._db.transaction() as conn:
            row = conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            body = {
                "id": event_id,
                "created_at": created,
                "actor": actor,
                "action": action,
                "target": target,
                "outcome": outcome,
                "details": details,
            }
            h = self._compute(prev, body)
            conn.execute(
                "INSERT INTO audit_events(id, created_at, actor, action, target, outcome, details_json,"
                " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, created, actor, action, target, outcome, canonical_json(details), prev, h),
            )
        return event_id

    def list(self, limit: int = 100, after_seq: int = 0) -> list[AuditEvent]:
        limit = max(1, min(limit, 1000))
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit)
            ).fetchall()
        return [
            AuditEvent(
                seq=r["seq"],
                id=r["id"],
                created_at=r["created_at"],
                actor=r["actor"],
                action=r["action"],
                target=r["target"],
                outcome=r["outcome"],
                details=json.loads(r["details_json"]),
                prev_hash=r["prev_hash"],
                hash=r["hash"],
            )
            for r in rows
        ]

    def head(self) -> str:
        with self._db.read() as conn:
            row = conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        return str(row["hash"]) if row else GENESIS

    def verify(self, expected_head: str | None = None) -> AuditVerification:
        problems: list[str] = []
        prev = GENESIS
        count = 0
        last_seq = 0
        with self._db.read() as conn:
            for r in conn.execute("SELECT * FROM audit_events ORDER BY seq"):
                count += 1
                if r["prev_hash"] != prev:
                    problems.append(f"seq {r['seq']}: chain link broken (event missing or reordered)")
                body = {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": r["target"],
                    "outcome": r["outcome"],
                    "details": json.loads(r["details_json"]),
                }
                if not hmac.compare_digest(self._compute(r["prev_hash"], body), r["hash"]):
                    problems.append(f"seq {r['seq']}: content hash mismatch (event modified)")
                if r["seq"] != last_seq + 1 and last_seq != 0:
                    problems.append(f"seq {r['seq']}: sequence gap after {last_seq}")
                last_seq = r["seq"]
                prev = r["hash"]
        if expected_head is not None and expected_head != prev:
            problems.append("head hash differs from externally recorded head (possible truncation)")
        return AuditVerification(valid=not problems, events_checked=count, head_hash=prev, problems=problems)
