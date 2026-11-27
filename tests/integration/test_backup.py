"""Backups, verification, restore, checkpoints and recovery readiness (stage 19)."""

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from end_to_end.conftest import ALICE, BOB
from end_to_end.test_mvp_workflow import human_fix, pending_approval
from helpers import make_renamed_api_project

from yemendev.continuity.backup import restore_backup, verify_backup
from yemendev.continuity.signing import public_hex
from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import IntegrityError, InvalidInputError, PermissionDeniedError
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform


@pytest.fixture
def repaired(platform, tmp_path):
    """A platform with real history: a repair applied, verified and checkpointed."""
    wf = RepairWorkflow(platform)
    root = make_renamed_api_project(tmp_path / "proj")
    pid = platform.register_project(ALICE, "calc", str(root)).id
    t = wf.create(ALICE, pid, human_plan=human_fix(root))
    wf.advance(ALICE, t.id)
    platform.approvals.decide(pending_approval(platform, t.id).id, BOB, approve=True)
    assert wf.advance(ALICE, t.id).task.status.value == "succeeded"
    return platform, pid, t.id


def known(p):
    return {public_hex(p.signing_key)}


def rewrite(archive: Path, out: Path, change) -> Path:
    """Copy a backup archive, letting ``change(name, data) -> data | None`` alter or drop members."""
    with tarfile.open(archive, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            new = change(m.name, data)
            if new is None:
                continue
            info = tarfile.TarInfo(m.name)
            info.size = len(new)
            dst.addfile(info, io.BytesIO(new))
    return out


def test_backup_verify_restore_round_trip(repaired, tmp_path):
    p, pid, tid = repaired
    b = p.create_backup(ALICE)
    assert Path(b["path"]).stat().st_mode & 0o777 == 0o600  # it contains keys
    v = p.verify_backup(ALICE, b["id"])
    assert v["verified_ok"] and all(c["ok"] for c in v["verify_report"]["checks"])
    names = [c["name"] for c in v["verify_report"]["checks"]]
    for n in ("archive unchanged since creation", "signature", "file hashes", "database integrity", "audit chain",
              "evidence", "checkpoints"):
        assert n in names
    target = tmp_path / "restored"
    restore_backup(Path(b["path"]), target, known(p), allow_unknown_signer=False)
    q = Platform(Settings(data_dir=target, sandbox=SandboxSettings(timeout_seconds=60)))
    assert q.verify_integrity()["valid"]
    assert q.audit.verify().head_hash == v["verify_report"]["manifest"]["audit_head"]
    assert [t.id for t in RepairWorkflow(q).tasks.list()] == [tid]
    assert q.settings.checkpoints_dir.joinpath(f"ckpt_{tid}", "manifest.json").is_file()
    assert all((target / x).stat().st_mode & 0o077 == 0 for x in ("yemendev.sqlite3", "keys"))


def test_tampering_is_detected(repaired, tmp_path):
    p, *_ = repaired
    b = Path(p.create_backup(ALICE)["path"])

    def db_flip(name, data):
        return data[:-10] + bytes(10) if name == "db/yemendev.sqlite3" else data

    def manifest_edit(name, data):
        if name != "manifest.json":
            return data
        m = json.loads(data)
        m["audit_head"] = "0" * 64
        return json.dumps(m).encode()

    def checkpoint_blob(name, data):
        return b"evil" if "/blobs/" in name else data

    for label, change, failing in [("db", db_flip, "file hashes"), ("manifest", manifest_edit, "signature"),
                                   ("checkpoint", checkpoint_blob, "checkpoints")]:
        rep = verify_backup(rewrite(b, tmp_path / f"{label}.tar.gz", change), known(p))
        assert not rep.ok, label
        assert failing in [c.name for c in rep.checks if not c.ok], (label, rep.checks)


def test_replaced_archive_is_detected_even_if_internally_consistent(repaired, tmp_path):
    p, *_ = repaired
    first = p.create_backup(ALICE)
    second = p.create_backup(ALICE)  # a valid, signed backup - but not the one recorded as `first`
    shutil.copyfile(second["path"], first["path"])
    v = p.verify_backup(ALICE, first["id"])
    assert not v["verified_ok"]
    assert [c["name"] for c in v["verify_report"]["checks"] if not c["ok"]] == ["archive unchanged since creation"]


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink"])
def test_hostile_archives_are_rejected_without_writing_anything(tmp_path, kind):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo({"traversal": "../escaped.txt", "absolute": "/tmp/escaped.txt",
                                "symlink": "keys/link"}[kind])
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        else:
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
    rep = verify_backup(evil, set())
    assert not rep.ok and rep.checks[0].name == "archive" and not rep.checks[0].ok
    assert not (tmp_path / "escaped.txt").exists()
    with pytest.raises(IntegrityError):
        restore_backup(evil, tmp_path / "t", set(), allow_unknown_signer=True)
    assert not (tmp_path / "t").exists() or not any((tmp_path / "t").iterdir())


def test_restore_never_overwrites_and_checks_the_signer(repaired, tmp_path):
    p, *_ = repaired
    b = Path(p.create_backup(ALICE)["path"])
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("user data")
    with pytest.raises(InvalidInputError, match="not empty"):
        restore_backup(b, occupied, known(p), allow_unknown_signer=False)
    assert (occupied / "keep.txt").read_text() == "user data"
    with pytest.raises(IntegrityError, match="unknown key"):
        restore_backup(b, tmp_path / "new", set(), allow_unknown_signer=False)
    restore_backup(b, tmp_path / "new", set(), allow_unknown_signer=True)  # explicit, fingerprint checked by human
    assert (tmp_path / "new" / "yemendev.sqlite3").is_file()


def test_checkpoints_and_readiness(repaired):
    p, pid, tid = repaired
    [cp] = p.list_checkpoints(ALICE, pid)
    assert cp["task_id"] == tid and cp["files"] == ["calc/api.py"] and cp["rolled_back"] is False
    r = {c["check"]: c["status"] for c in p.recovery_readiness(ALICE)["checks"]}
    assert r["verified backup"] == "fail" and r["audit and evidence"] == "ok" and r["checkpoints restorable"] == "ok"
    b = p.create_backup(ALICE)
    assert {c["check"]: c["status"] for c in p.recovery_readiness(ALICE)["checks"]}["verified backup"] == "warn"
    p.verify_backup(ALICE, b["id"])
    rep = p.recovery_readiness(ALICE)
    assert {c["check"]: c["status"] for c in rep["checks"]}["verified backup"] == "ok"
    blob = next(p.settings.checkpoints_dir.joinpath(f"ckpt_{tid}", "blobs").iterdir())
    blob.write_bytes(b"corrupted")
    rep = p.recovery_readiness(ALICE)
    assert {c["check"]: c["status"] for c in rep["checks"]}["checkpoints restorable"] == "fail"
    assert rep["overall"] == "fail"


def test_backups_are_admin_only(platform):
    op = Actor(id="olga", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    with pytest.raises(PermissionDeniedError):
        platform.create_backup(op)
    assert platform.list_backups(op) == []
