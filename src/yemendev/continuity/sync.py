"""Offline synchronization between installations via signed bundles (directive 5.10, 12).

Transport is a file (works offline, e.g. USB); there is no network transport. Policies:
* synced: active memory records (verified/unverified) of one project, and skills;
* never synced: approvals, audit events, tokens, evidence (they are local trust records);
* imported knowledge is always UNVERIFIED locally; imported skills start PROPOSED;
* any differing local value becomes a conflict for a human - nothing is overwritten;
* bundles must be signed by a trusted key; re-importing the same bundle is a no-op;
* an export is recorded as "exported (not delivered)": the platform cannot know it arrived.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from yemendev.continuity.signing import fingerprint, public_hex, sign, verify
from yemendev.core.contracts import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    canonical_json,
    digest,
    iso,
    new_id,
    sha256_text,
)
from yemendev.learning.skills import SkillSpec
from yemendev.memory.model import ACTIVE_STATUSES, MemoryCategory, MemoryStatus, MemoryWrite

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from yemendev.platform import Platform

BUNDLE_SCHEMA = "1.0"


class SyncRecord(BaseModel):
    category: MemoryCategory
    subject: str
    key: str
    value: Any
    remote_status: MemoryStatus
    confidence: float = Field(ge=0, le=1)
    source: str
    record_hash: str


class BundleBody(BaseModel):
    schema_version: str = Field(pattern=r"^1\.\d+$")
    created_at: str
    source_project_name: str
    memory: list[SyncRecord]
    skills: list[SkillSpec]


class SignedBundle(BaseModel):
    body: BundleBody
    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class SyncJob(BaseModel):
    id: str
    project_id: str
    direction: str
    status: str
    peer_fingerprint: str | None
    bundle_sha256: str
    created_at: str
    finished_at: str | None
    records_total: int
    records_applied: int
    conflicts: int
    error: str | None


class SyncConflict(BaseModel):
    id: str
    job_id: str
    project_id: str
    subject: str
    key: str
    local_id: str
    local_value: Any
    remote_value: Any
    status: str
    resolution: str | None
    resolved_by: str | None


def _record_hash(category: str, subject: str, key: str, value: Any) -> str:
    return digest([category, subject, key, value])


def export_bundle(p: Platform, project_id: str, key: Ed25519PrivateKey) -> tuple[SignedBundle, SyncJob]:
    proj = p.project(project_id)
    recs = [SyncRecord(category=r.category, subject=r.subject, key=r.key, value=r.value, remote_status=r.status,
                       confidence=r.confidence, source=r.source,
                       record_hash=_record_hash(r.category.value, r.subject, r.key, r.value))
            for r in p.memory.query(project_id, limit=5000) if r.status in ACTIVE_STATUSES]
    skills = [s.spec for s in p.skills.list() if s.status.value in ("verified", "active")]
    body = BundleBody(schema_version=BUNDLE_SCHEMA, created_at=iso(), source_project_name=proj.name, memory=recs,
                      skills=skills)
    msg = canonical_json(body.model_dump(mode="json")).encode()
    bundle = SignedBundle(body=body, public_key=public_hex(key), signature=sign(key, msg))
    job = _create_job(p, project_id, "export", "exported_not_delivered", fingerprint(bundle.public_key),
                      sha256_text(msg.decode()), len(recs) + len(skills))
    return bundle, job


def import_bundle(p: Platform, project_id: str, raw: str | bytes, trusted: set[str], actor_ref: str) -> SyncJob:
    try:
        bundle = SignedBundle.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise ConflictError(f"bundle rejected: schema validation failed ({str(exc)[:200]})") from exc
    msg = canonical_json(bundle.body.model_dump(mode="json")).encode()
    if not verify(bundle.public_key, msg, bundle.signature):
        raise PermissionDeniedError("bundle rejected: invalid signature (modified or corrupted)")
    if bundle.public_key not in trusted:
        raise PermissionDeniedError(f"bundle rejected: signer {fingerprint(bundle.public_key)} is not trusted "
                                    "(an admin can trust the key with `yemendev keys trust`)")
    bhash = sha256_text(msg.decode())
    with p.db.read() as conn:
        done = conn.execute("SELECT id FROM sync_jobs WHERE project_id = ? AND direction = 'import' AND "
                            "bundle_sha256 = ? AND status = 'completed'", (project_id, bhash)).fetchone()
    if done:
        return get_job(p, done["id"])
    total = len(bundle.body.memory) + len(bundle.body.skills)
    job = _create_job(p, project_id, "import", "pending", fingerprint(bundle.public_key), bhash, total)
    applied = conflicts = 0
    src = f"sync:{fingerprint(bundle.public_key)}"
    try:
        for r in bundle.body.memory:
            if _record_hash(r.category.value, r.subject, r.key, r.value) != r.record_hash:
                raise ConflictError(f"record hash mismatch for {r.subject}/{r.key}")
            local = [x for x in p.memory.history(project_id, r.subject, r.key) if x.status in ACTIVE_STATUSES]
            if any(x.value == r.value for x in local):
                continue  # already known (idempotent)
            if local:
                _add_conflict(p, job.id, project_id, r, local[-1].id, local[-1].value)
                conflicts += 1
                continue
            p.memory.write(MemoryWrite(project_id=project_id, category=r.category, subject=r.subject, key=r.key,
                                       value=r.value, verified=False, confidence=r.confidence, source=src))
            applied += 1
        existing = {s.content_hash for s in p.skills.list()}
        for spec in bundle.body.skills:
            if spec.content_hash() in existing:
                continue
            p.skills.propose(spec, actor_ref)  # PROPOSED: must be validated and activated locally
            applied += 1
    except Exception as exc:
        _finish_job(p, job.id, "failed", applied, conflicts, f"{exc.__class__.__name__}: {exc}"[:500])
        raise
    _finish_job(p, job.id, "completed", applied, conflicts, None)
    return get_job(p, job.id)


def resolve_conflict(p: Platform, conflict_id: str, resolution: str, actor_ref: str) -> SyncConflict:
    c = get_conflict(p, conflict_id)
    if c.status != "open":
        raise ConflictError("conflict already resolved")
    if resolution not in ("keep_local", "take_remote"):
        raise ConflictError("resolution must be keep_local or take_remote")
    if resolution == "take_remote":
        local = p.memory.get(c.local_id)
        ev = p.evidence.record(action="sync.conflict_resolution", actor=actor_ref, target=c.project_id,
                               outcome="take_remote", body=_ev_body(c))
        p.memory.write(MemoryWrite(project_id=c.project_id, category=local.category, subject=c.subject, key=c.key,
                                   value=c.remote_value, verified=True, confidence=1.0,
                                   source=f"human:{actor_ref}", evidence_ids=[ev.id]))
    with p.db.transaction() as conn:
        conn.execute("UPDATE sync_conflicts SET status = 'resolved', resolution = ?, resolved_by = ?, resolved_at = ? "
                     "WHERE id = ? AND status = 'open'", (resolution, actor_ref, iso(), conflict_id))
    return get_conflict(p, conflict_id)


def _ev_body(c: SyncConflict) -> Any:
    from yemendev.evidence.store import EvidenceBody

    return EvidenceBody(inputs={"conflict_id": c.id}, before_state={"local": c.local_value},
                        after_state={"remote": c.remote_value})


def _create_job(p: Platform, project_id: str, direction: str, status: str, peer: str | None, bhash: str,
                total: int) -> SyncJob:
    jid = new_id("sync")
    with p.db.transaction() as conn:
        conn.execute("INSERT INTO sync_jobs(id, project_id, direction, status, peer_fingerprint, bundle_sha256, "
                     "created_at, finished_at, records_total, records_applied, conflicts) "
                     "VALUES (?,?,?,?,?,?,?,?,?,0,0)",
                     (jid, project_id, direction, status, peer, bhash, iso(),
                      iso() if direction == "export" else None, total))
    return get_job(p, jid)


def _finish_job(p: Platform, job_id: str, status: str, applied: int, conflicts: int, error: str | None) -> None:
    with p.db.transaction() as conn:
        conn.execute("UPDATE sync_jobs SET status = ?, finished_at = ?, records_applied = ?, conflicts = ?, error = ? "
                     "WHERE id = ?", (status, iso(), applied, conflicts, error, job_id))


def _add_conflict(p: Platform, job_id: str, project_id: str, r: SyncRecord, local_id: str, local_value: Any) -> None:
    with p.db.transaction() as conn:
        conn.execute("INSERT INTO sync_conflicts(id, job_id, project_id, subject, key, local_id, local_value_json, "
                     "remote_value_json, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
                     (new_id("scf"), job_id, project_id, r.subject, r.key, local_id, canonical_json(local_value),
                      canonical_json(r.value)))


def get_job(p: Platform, job_id: str) -> SyncJob:
    with p.db.read() as conn:
        r = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        raise NotFoundError("sync job not found")
    return SyncJob(**dict(r))


def list_jobs(p: Platform, project_id: str | None = None) -> list[SyncJob]:
    q = "SELECT * FROM sync_jobs"
    args: tuple[str, ...] = ()
    if project_id:
        q, args = q + " WHERE project_id = ?", (project_id,)
    with p.db.read() as conn:
        return [SyncJob(**dict(r)) for r in conn.execute(q + " ORDER BY created_at, rowid", args).fetchall()]


def get_conflict(p: Platform, conflict_id: str) -> SyncConflict:
    with p.db.read() as conn:
        r = conn.execute("SELECT * FROM sync_conflicts WHERE id = ?", (conflict_id,)).fetchone()
    if r is None:
        raise NotFoundError("sync conflict not found")
    return _conflict(r)


def list_conflicts(p: Platform, project_id: str, status: str | None = None) -> list[SyncConflict]:
    q, args = "SELECT * FROM sync_conflicts WHERE project_id = ?", [project_id]
    if status:
        q += " AND status = ?"
        args.append(status)
    with p.db.read() as conn:
        return [_conflict(r) for r in conn.execute(q + " ORDER BY rowid", args).fetchall()]


def _conflict(r: Any) -> SyncConflict:
    return SyncConflict(id=r["id"], job_id=r["job_id"], project_id=r["project_id"], subject=r["subject"],
                        key=r["key"], local_id=r["local_id"], local_value=json.loads(r["local_value_json"]),
                        remote_value=json.loads(r["remote_value_json"]), status=r["status"],
                        resolution=r["resolution"], resolved_by=r["resolved_by"])
