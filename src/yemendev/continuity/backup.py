"""Backups with real verification, restore into a fresh directory, and checkpoint inspection.

Directive (Continuity Center): checkpoints, backup verification, recovery readiness; offline-first:
"never overwrite important data silently".

A backup is one ``.tar.gz`` (mode 0600 - it contains the audit and signing keys) with:

* ``db/yemendev.sqlite3`` - a consistent snapshot taken with SQLite's online backup API (safe while the
  platform runs, WAL included);
* ``keys/`` - ``audit.key`` (without it the audit chain cannot be verified), ``ed25519.pem``, trusted keys;
* ``checkpoints/`` - every repair checkpoint (original file contents);
* ``manifest.json`` - SHA-256 of every file, schema version, audit head and counts, signed with the
  installation's Ed25519 key (``manifest.sig``).

``verify`` does not trust the manifest: it checks the signature, every file hash, ``PRAGMA integrity_check``,
the audit HMAC chain (recomputed with the backed-up key, head compared to the manifest), that every evidence
record is anchored and unmodified, and that every checkpoint blob matches its recorded hash - all on a
temporary copy. ``restore`` verifies first and only writes into an empty or new directory.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from yemendev import __version__
from yemendev.continuity.signing import fingerprint, load_or_create_signing_key, public_hex, sign, verify
from yemendev.core.contracts import IntegrityError, InvalidInputError, canonical_json, iso, new_id, sha256_bytes
from yemendev.recovery.checkpoint import CheckpointManifest
from yemendev.security.audit import AuditLog
from yemendev.storage.db import Database

FORMAT = "yemendev-backup/1"
_ALLOWED_TOP = {"manifest.json", "manifest.sig", "db", "keys", "checkpoints"}
MAX_MEMBER_BYTES = 8 * 1024**3


class BackupManifest(BaseModel):
    format: str
    backup_id: str
    created_at: str
    created_by: str
    platform_version: str
    schema_version: int
    audit_events: int
    audit_head: str
    evidence_records: int
    files: dict[str, str]  # archive path -> sha256
    signer_public_key: str


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


class VerifyReport(BaseModel):
    ok: bool
    backup_id: str | None
    signer_fingerprint: str | None
    signer_known: bool
    checks: list[Check]
    manifest: dict[str, Any] | None


# ---- create -----------------------------------------------------------------------------------------
def _snapshot_db(src: Path, dst: Path) -> None:
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)  # consistent online copy, including what is still in the WAL
    finally:
        d.close()
        s.close()


def create_backup(data_dir: Path, db: Database, keys_dir: Path, checkpoints_dir: Path, out_dir: Path,
                  actor_ref: str) -> tuple[Path, BackupManifest]:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_id = new_id("bkp")
    with tempfile.TemporaryDirectory(prefix="ydv-backup-", dir=out_dir) as tmp:
        stage = Path(tmp)
        (stage / "db").mkdir()
        _snapshot_db(db.path, stage / "db" / "yemendev.sqlite3")
        shutil.copytree(keys_dir, stage / "keys", symlinks=False)
        if checkpoints_dir.is_dir():
            shutil.copytree(checkpoints_dir, stage / "checkpoints", symlinks=False)
        snap = Database(stage / "db" / "yemendev.sqlite3")
        with snap.read() as conn:
            schema = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            n_audit, head = conn.execute("SELECT COUNT(*), (SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1)"
                                         " FROM audit_events").fetchone()
            n_ev = conn.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
        files = {p.relative_to(stage).as_posix(): sha256_bytes(p.read_bytes())
                 for p in sorted(stage.rglob("*")) if p.is_file() and not p.is_symlink()}
        key = load_or_create_signing_key(keys_dir)
        manifest = BackupManifest(format=FORMAT, backup_id=backup_id, created_at=iso(), created_by=actor_ref,
                                  platform_version=__version__, schema_version=int(schema), audit_events=n_audit,
                                  audit_head=head or "", evidence_records=n_ev, files=files,
                                  signer_public_key=public_hex(key))
        body = canonical_json(manifest.model_dump()).encode()
        (stage / "manifest.json").write_bytes(body)
        (stage / "manifest.sig").write_text(sign(key, body))
        stamp = manifest.created_at.replace("-", "").replace(":", "")[:15]
        archive = out_dir / f"ydv-backup-{stamp}-{backup_id[4:12]}.tar.gz"
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for p in sorted(stage.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    tar.add(p, arcname=p.relative_to(stage).as_posix(), recursive=False)
    return archive, manifest


# ---- verify -----------------------------------------------------------------------------------------
def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    out = []
    for m in tar.getmembers():
        p = PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] not in _ALLOWED_TOP:
            raise IntegrityError(f"unexpected path in backup: {m.name!r}")
        if not (m.isfile() or m.isdir()):
            raise IntegrityError(f"backup contains a link or special file: {m.name!r}")
        if m.size > MAX_MEMBER_BYTES:
            raise IntegrityError(f"backup member too large: {m.name!r}")
        out.append(m)
    return out


def _extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for m in _safe_members(tar):
            target = dest / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                raise IntegrityError(f"cannot read {m.name!r}")
            with src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(target, 0o600)


def verify_backup(archive: Path, known_public_keys: set[str]) -> VerifyReport:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name=name, ok=ok, detail=detail))
        return ok

    def report(manifest: BackupManifest | None, signer_known: bool = False) -> VerifyReport:
        return VerifyReport(ok=bool(checks) and all(c.ok for c in checks),
                            backup_id=manifest.backup_id if manifest else None,
                            signer_fingerprint=fingerprint(manifest.signer_public_key) if manifest else None,
                            signer_known=signer_known, checks=checks,
                            manifest={k: v for k, v in manifest.model_dump().items() if k != "files"}
                            if manifest else None)

    if not archive.is_file():
        raise InvalidInputError("backup file not found")
    with tempfile.TemporaryDirectory(prefix="ydv-verify-") as tmp:
        root = Path(tmp)
        try:
            _extract(archive, root)
        except (IntegrityError, tarfile.TarError, OSError, EOFError) as exc:
            add("archive", False, f"cannot be read safely: {exc}")
            return report(None)
        add("archive", True, "readable; only expected regular files")
        try:
            body = (root / "manifest.json").read_bytes()
            manifest = BackupManifest.model_validate_json(body)
            sig = (root / "manifest.sig").read_text().strip()
        except (OSError, ValueError) as exc:
            add("manifest", False, f"missing or invalid: {exc.__class__.__name__}")
            return report(None)
        if not add("format", manifest.format == FORMAT, manifest.format):
            return report(manifest)
        sig_ok = verify(manifest.signer_public_key, body, sig)
        known = manifest.signer_public_key in known_public_keys
        who = " (this installation or a trusted key)" if known else " (unknown signer)"
        add("signature", sig_ok,
            ("valid" if sig_ok else "INVALID") + f"; signer {fingerprint(manifest.signer_public_key)}{who}")
        present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"manifest.json",
                                                                                              "manifest.sig"}
        bad = sorted(n for n, h in manifest.files.items() if n not in present or
                     sha256_bytes((root / n).read_bytes()) != h)
        extra = sorted(present - set(manifest.files))
        add("file hashes", not bad and not extra, f"{len(manifest.files)} files match" if not bad and not extra
            else f"mismatched/missing: {bad[:5]}; unexpected: {extra[:5]}")
        dbp = root / "db" / "yemendev.sqlite3"
        if not add("database present", dbp.is_file(), str(dbp.name)):
            return report(manifest, known)
        db = Database(dbp)
        with db.read() as conn:
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
            schema = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            n_ev = conn.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
        add("database integrity", integ == "ok", f"PRAGMA integrity_check: {integ}")
        add("schema version", schema == manifest.schema_version, f"{schema} (manifest {manifest.schema_version})")
        try:
            audit = AuditLog(db, (root / "keys" / "audit.key").read_bytes())
            av = audit.verify()
            head_ok = av.head_hash == (manifest.audit_head or av.head_hash)
            problems = f"; {av.problems[:2]}" if av.problems else ""
            add("audit chain", av.valid and head_ok and av.events_checked == manifest.audit_events,
                f"{av.events_checked} events, chain {'valid' if av.valid else 'BROKEN'}, head "
                f"{'matches' if head_ok else 'DIFFERS FROM'} manifest{problems}")
        except OSError:
            add("audit chain", False, "audit key missing from backup")
        add("evidence", n_ev == manifest.evidence_records and _evidence_ok(db),
            f"{n_ev} records (manifest {manifest.evidence_records}); content hashes and audit anchors checked")
        cp_ok, cp_detail = _checkpoints_ok(root / "checkpoints")
        add("checkpoints", cp_ok, cp_detail)
        return report(manifest, known)


def _evidence_ok(db: Database) -> bool:
    from yemendev.evidence.store import EvidenceStore

    with db.read() as conn:
        anchored = {r[0]: json.loads(r[1]).get("content_hash") for r in conn.execute(
            "SELECT target, details_json FROM audit_events WHERE action = 'evidence.recorded'")}
    store = EvidenceStore(db)
    hashes = store.all_hashes()
    return all(anchored.get(i) == h for i, h in hashes.items()) and set(anchored) <= set(hashes) and \
        all(store.get(i).hash_valid for i in hashes)


def _checkpoints_ok(base: Path) -> tuple[bool, str]:
    if not base.is_dir():
        return True, "no checkpoints"
    n, problems = 0, []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            m = CheckpointManifest.model_validate_json((d / "manifest.json").read_bytes())
        except (OSError, ValueError):
            problems.append(f"{d.name}: manifest unreadable (incomplete checkpoint)")
            continue
        n += 1
        for e in m.entries:
            if e.existed and sha256_bytes((d / "blobs" / str(e.blob)).read_bytes()) != e.sha256:
                problems.append(f"{d.name}: blob for {e.path} corrupted")
    return not problems, f"{n} checkpoint(s) verified" if not problems else "; ".join(problems[:5])


# ---- restore ----------------------------------------------------------------------------------------
def restore_backup(archive: Path, target: Path, known_public_keys: set[str],
                   allow_unknown_signer: bool) -> VerifyReport:
    """Verify, then materialise the backup as a new data directory. Never writes into a non-empty directory."""
    if target.exists() and any(target.iterdir()):
        raise InvalidInputError(f"{target} is not empty: restore only into a new or empty directory")
    rep = verify_backup(archive, known_public_keys)
    if not rep.ok:
        raise IntegrityError("backup failed verification; nothing restored: "
                             + "; ".join(f"{c.name}: {c.detail}" for c in rep.checks if not c.ok))
    if not rep.signer_known and not allow_unknown_signer:
        raise IntegrityError(f"backup signed by unknown key {rep.signer_fingerprint}; confirm it is yours with "
                             "--allow-unknown-signer (compare the fingerprint with the source installation)")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="ydv-restore-", dir=target) as tmp:
        stage = Path(tmp)
        _extract(archive, stage)
        shutil.move(str(stage / "db" / "yemendev.sqlite3"), target / "yemendev.sqlite3")
        shutil.move(str(stage / "keys"), target / "keys")
        if (stage / "checkpoints").is_dir():
            shutil.move(str(stage / "checkpoints"), target / "checkpoints")
    for p in [target, *target.rglob("*")]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return rep


def read_manifest(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        for m in _safe_members(tar):
            if m.name == "manifest.json":
                f = tar.extractfile(m)
                if f is not None:
                    return dict(json.load(io.TextIOWrapper(f, "utf-8")))
    raise IntegrityError("manifest.json not found")
