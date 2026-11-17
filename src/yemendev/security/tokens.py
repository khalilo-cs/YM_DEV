"""API bearer tokens. Only SHA-256 hashes are stored; the plaintext is shown once."""

from __future__ import annotations

import hashlib
import secrets

from yemendev.core.contracts import NotFoundError, iso, new_id
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.storage.db import Database

TOKEN_PREFIX = "ydv_"  # noqa: S105 - public prefix, not a secret


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def issue(self, actor_id: str, role: Role) -> tuple[str, str]:
        """Return (token_id, plaintext_token)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = new_id("tok")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_tokens(id, token_hash, actor_id, role, created_at) VALUES (?,?,?,?,?)",
                (token_id, _hash(token), actor_id, role.value, iso()),
            )
        return token_id, token

    def authenticate(self, token: str) -> Actor | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > 256:
            return None
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT actor_id, role FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (_hash(token),),
            ).fetchone()
        if row is None:
            return None
        return Actor(id=row["actor_id"], kind=ActorKind.HUMAN, role=Role(row["role"]))

    def revoke(self, token_id: str) -> None:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (iso(), token_id)
            )
            if cur.rowcount == 0:
                raise NotFoundError("token not found or already revoked")
