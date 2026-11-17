"""Checkpoints of original file contents taken before modifying a real project."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel

from yemendev.core.contracts import IntegrityError, InvalidInputError, sha256_bytes


class CheckpointEntry(BaseModel):
    path: str
    existed: bool
    sha256: str | None
    blob: str | None


class CheckpointManifest(BaseModel):
    id: str
    project_root: str
    entries: list[CheckpointEntry]


def fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.ydv-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if target.exists():
        shutil.copymode(target, tmp)
    os.replace(tmp, target)
    fsync_dir(target.parent)


def safe_target(root: Path, rel: str) -> Path:
    root = root.resolve()
    t = (root / rel).resolve()
    if not t.is_relative_to(root):
        raise InvalidInputError("path escapes project root")
    # refuse to write through symlinks anywhere along the path
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise InvalidInputError("refusing to modify a path that contains a symlink")
    return t


class CheckpointStore:
    def __init__(self, base: Path) -> None:
        self.base = base

    def _dir(self, cid: str) -> Path:
        if not cid.replace("_", "").isalnum():
            raise InvalidInputError("invalid checkpoint id")
        return self.base / cid

    def create(self, cid: str, project_root: Path, paths: list[str]) -> CheckpointManifest:
        d = self._dir(cid)
        if (d / "manifest.json").exists():
            return self.load(cid)  # idempotent (resume after crash)
        blobs = d / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        entries = []
        for i, rel in enumerate(paths):
            t = safe_target(project_root, rel)
            if t.is_file():
                data = t.read_bytes()
                blob = f"{i:04d}"
                atomic_write(blobs / blob, data)
                entries.append(CheckpointEntry(path=rel, existed=True, sha256=sha256_bytes(data), blob=blob))
            else:
                entries.append(CheckpointEntry(path=rel, existed=False, sha256=None, blob=None))
        m = CheckpointManifest(id=cid, project_root=str(project_root.resolve()), entries=entries)
        # manifest written last: its presence means the checkpoint is complete
        atomic_write(d / "manifest.json", m.model_dump_json().encode())
        return m

    def load(self, cid: str) -> CheckpointManifest:
        p = self._dir(cid) / "manifest.json"
        if not p.is_file():
            raise IntegrityError("checkpoint manifest missing")
        return CheckpointManifest(**json.loads(p.read_text()))

    def exists(self, cid: str) -> bool:
        return (self._dir(cid) / "manifest.json").is_file()

    def restore(self, cid: str) -> list[str]:
        """Restore every entry and verify hashes. Returns restored paths."""
        m = self.load(cid)
        root = Path(m.project_root)
        restored = []
        for e in m.entries:
            t = safe_target(root, e.path)
            if e.existed:
                data = (self._dir(cid) / "blobs" / str(e.blob)).read_bytes()
                if sha256_bytes(data) != e.sha256:
                    raise IntegrityError(f"checkpoint blob for {e.path} is corrupted")
                atomic_write(t, data)
            elif t.exists():
                t.unlink()
            restored.append(e.path)
        for e in m.entries:
            t = safe_target(root, e.path)
            ok = (t.is_file() and sha256_bytes(t.read_bytes()) == e.sha256) if e.existed else not t.exists()
            if not ok:
                raise IntegrityError(f"rollback verification failed for {e.path}")
        return restored
