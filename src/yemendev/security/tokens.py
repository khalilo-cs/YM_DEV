"""API bearer tokens. Only SHA-256 hashes are stored; the plaintext is shown once."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from yemendev.core.contracts import InvalidInputError, NotFoundError, iso, new_id, utcnow
from yemendev.governance.actors import ROLE_RANK, Actor, ActorKind, Role
from yemendev.storage.db import Database

TOKEN_PREFIX = "ydv_"  # noqa: S105 - public prefix, not a secret
DEFAULT_TTL_SECONDS = 90 * 86400
MAX_TTL_SECONDS = 366 * 86400


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def issue(self, actor_id: str, role: Role, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, str]:
        """Return (token_id, plaintext_token). Every token expires (audit finding A-14)."""
        if not 60 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise InvalidInputError("token lifetime must be between 1 minute and 366 days")
        if not actor_id or len(actor_id) > 128 or not all(c.isalnum() or c in "_.@-" for c in actor_id):
            raise InvalidInputError("invalid actor id")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = new_id("tok")
        now = utcnow()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_tokens(id, token_hash, actor_id, role, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (token_id, _hash(token), actor_id, role.value, iso(now), iso(now + timedelta(seconds=ttl_seconds))),
            )
        return token_id, token

    def authenticate(self, token: str) -> Actor | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > 256:
            return None
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT actor_id, role FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL"
                " AND expires_at IS NOT NULL AND expires_at > ?",
                (_hash(token), iso()),
            ).fetchone()
        if row is None:
            return None
        return Actor(id=row["actor_id"], kind=ActorKind.HUMAN, role=Role(row["role"]))

    def current_role(self, actor_id: str) -> Role | None:
        """Highest role among the actor's active (unrevoked, unexpired) tokens; None if it has none.

        Background work queued by a human runs with at most this role, so revoking a person's tokens
        (or letting them expire) also stops work queued in their name.
        """
        with self._db.read() as conn:
            roles = [Role(r["role"]) for r in conn.execute(
                "SELECT role FROM api_tokens WHERE actor_id = ? AND revoked_at IS NULL AND expires_at IS NOT NULL"
                " AND expires_at > ?", (actor_id, iso())).fetchall()]
        return max(roles, key=lambda r: ROLE_RANK[r]) if roles else None

    def revoke(self, token_id: str) -> None:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (iso(), token_id)
            )
            if cur.rowcount == 0:
                raise NotFoundError("token not found or already revoked")
