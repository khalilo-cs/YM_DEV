"""Local key material (kept outside the database, file mode 0600)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def load_or_create_key(path: Path, nbytes: int = 32) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = path.read_bytes()
        if len(data) < 16:
            raise ValueError(f"key file {path.name} is truncated")
        return data
    key = secrets.token_bytes(nbytes)
    # O_EXCL: never clobber a key created concurrently by another process.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key
