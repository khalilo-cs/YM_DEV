"""Per-project membership (audit finding A-11).

A token's role is a *ceiling*. On a project, a non-admin actor's effective role is
``min(token role, membership role)``; without a membership the actor has no access to
that project at all. Global admins (and the platform's own SYSTEM actor) are unrestricted.
"""

from __future__ import annotations

import builtins
import re

from pydantic import BaseModel

from yemendev.core.contracts import InvalidInputError, NotFoundError, iso
from yemendev.governance.actors import ROLE_RANK, Actor, ActorKind, Role
from yemendev.storage.db import Database

_REF = re.compile(r"^(human|agent):[A-Za-z0-9_.@-]{1,128}$")
_BY_RANK = {v: k for k, v in ROLE_RANK.items()}


class Member(BaseModel):
    project_id: str
    actor_ref: str
    role: Role
    granted_by: str
    granted_at: str


class MembershipStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def validate_ref(actor_ref: str) -> str:
        if not _REF.match(actor_ref):
            raise InvalidInputError("actor reference must look like 'human:<id>'")
        return actor_ref

    def grant(self, project_id: str, actor_ref: str, role: Role, granted_by: str) -> Member:
        self.validate_ref(actor_ref)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO project_members(project_id, actor_ref, role, granted_by, granted_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(project_id, actor_ref) DO UPDATE SET role = excluded.role,"
                " granted_by = excluded.granted_by, granted_at = excluded.granted_at",
                (project_id, actor_ref, role.value, granted_by, iso()),
            )
        return self.get(project_id, actor_ref)

    def revoke(self, project_id: str, actor_ref: str) -> None:
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM project_members WHERE project_id = ? AND actor_ref = ?",
                               (project_id, actor_ref))
            if cur.rowcount == 0:
                raise NotFoundError("membership not found")

    def get(self, project_id: str, actor_ref: str) -> Member:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM project_members WHERE project_id = ? AND actor_ref = ?",
                             (project_id, actor_ref)).fetchone()
        if r is None:
            raise NotFoundError("membership not found")
        return Member(**dict(r))

    def role(self, project_id: str, actor_ref: str) -> Role | None:
        with self._db.read() as conn:
            r = conn.execute("SELECT role FROM project_members WHERE project_id = ? AND actor_ref = ?",
                             (project_id, actor_ref)).fetchone()
        return Role(r["role"]) if r else None

    def list(self, project_id: str) -> builtins.list[Member]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM project_members WHERE project_id = ? ORDER BY actor_ref",
                                (project_id,)).fetchall()
        return [Member(**dict(r)) for r in rows]

    def projects_for(self, actor_ref: str) -> dict[str, Role]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT project_id, role FROM project_members WHERE actor_ref = ?",
                                (actor_ref,)).fetchall()
        return {r["project_id"]: Role(r["role"]) for r in rows}


def is_unrestricted(actor: Actor) -> bool:
    return actor.kind is ActorKind.SYSTEM or (actor.kind is ActorKind.HUMAN and actor.role is Role.ADMIN)


def effective_role(actor: Actor, membership: Role | None) -> Role | None:
    if is_unrestricted(actor):
        return actor.role
    if membership is None:
        return None
    return _BY_RANK[min(ROLE_RANK[actor.role], ROLE_RANK[membership])]
