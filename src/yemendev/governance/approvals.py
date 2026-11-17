"""Human approval workflow.

An approval is bound to (action, target, operation_digest) and has an expiry. It is
single-use: ``consume`` atomically transitions APPROVED -> CONSUMED only when every
binding matches, so an approval can never authorize a different or repeated operation.
Separation of duties: the requester can never approve their own request, and only
human actors can decide.
"""

from __future__ import annotations

import builtins
from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel

from yemendev.core.contracts import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    iso,
    new_id,
    parse_iso,
    utcnow,
)
from yemendev.governance.actors import Actor, ActorKind
from yemendev.governance.policy import Action, PolicyEngine
from yemendev.storage.db import Database


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalRequest(BaseModel):
    id: str
    action: Action
    target: str
    operation_digest: str
    summary: str
    requested_by: str
    task_id: str | None
    created_at: str
    expires_at: str
    status: ApprovalStatus
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    consumed_at: str | None = None


def _row(r: object) -> ApprovalRequest:
    return ApprovalRequest(**dict(r))  # type: ignore[call-overload]


class ApprovalService:
    def __init__(self, db: Database, policy: PolicyEngine, ttl_seconds: int) -> None:
        self._db = db
        self._policy = policy
        self._ttl = ttl_seconds

    def request(
        self, *, action: Action, target: str, operation_digest: str, summary: str,
        requested_by: Actor, task_id: str | None = None,
    ) -> ApprovalRequest:
        now = utcnow()
        req = ApprovalRequest(
            id=new_id("apr"), action=action, target=target, operation_digest=operation_digest,
            summary=summary[:2000], requested_by=requested_by.ref, task_id=task_id,
            created_at=iso(now), expires_at=iso(now + timedelta(seconds=self._ttl)),
            status=ApprovalStatus.PENDING,
        )
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO approval_requests(id, action, target, operation_digest, summary, requested_by,"
                " task_id, created_at, expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (req.id, req.action.value, req.target, req.operation_digest, req.summary, req.requested_by,
                 req.task_id, req.created_at, req.expires_at, req.status.value),
            )
        return req

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (approval_id,)).fetchone()
        if r is None:
            raise NotFoundError("approval not found")
        return self._expire_if_needed(_row(r))

    def list(self, status: ApprovalStatus | None = None, limit: int = 100) -> list[ApprovalRequest]:
        q = "SELECT * FROM approval_requests"
        args: tuple[object, ...] = ()
        if status:
            q += " WHERE status = ?"
            args = (status.value,)
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._db.read() as conn:
            rows = conn.execute(q, (*args, max(1, min(limit, 500)))).fetchall()
        return [self._expire_if_needed(_row(r)) for r in rows]

    def for_task(self, task_id: str) -> builtins.list[ApprovalRequest]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM approval_requests WHERE task_id = ? ORDER BY created_at",
                                (task_id,)).fetchall()
        return [self._expire_if_needed(_row(r)) for r in rows]

    def _expire_if_needed(self, req: ApprovalRequest) -> ApprovalRequest:
        if req.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and parse_iso(req.expires_at) <= utcnow():
            with self._db.transaction() as conn:
                conn.execute(
                    "UPDATE approval_requests SET status = ? WHERE id = ? AND status IN (?, ?)",
                    (ApprovalStatus.EXPIRED.value, req.id, ApprovalStatus.PENDING.value,
                     ApprovalStatus.APPROVED.value),
                )
            req = req.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return req

    def decide(self, approval_id: str, approver: Actor, approve: bool, reason: str = "") -> ApprovalRequest:
        if approver.kind is not ActorKind.HUMAN:
            raise PermissionDeniedError("only human actors can decide approvals")
        decision = self._policy.evaluate(approver, Action.DECIDE_APPROVAL)
        if not decision.allowed:
            raise PermissionDeniedError(decision.reason)
        req = self.get(approval_id)
        if req.requested_by == approver.ref:
            raise PermissionDeniedError("requester cannot decide their own approval request")
        if req.status is not ApprovalStatus.PENDING:
            raise ConflictError(f"approval is {req.status.value}, not pending")
        new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE approval_requests SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?"
                " WHERE id = ? AND status = ? AND expires_at > ?",
                (new_status.value, approver.ref, iso(), reason[:2000], approval_id,
                 ApprovalStatus.PENDING.value, iso()),
            )
            if cur.rowcount != 1:
                raise ConflictError("approval changed state concurrently or expired")
        return self.get(approval_id)

    def consume(self, approval_id: str, *, action: Action, target: str, operation_digest: str) -> ApprovalRequest:
        """Atomically consume an approval for exactly this operation, or raise."""
        req = self.get(approval_id)
        if req.status is not ApprovalStatus.APPROVED:
            raise PreconditionFailedError(f"approval is {req.status.value}, not approved")
        if req.action != action or req.target != target or req.operation_digest != operation_digest:
            raise PermissionDeniedError("approval does not match this operation")
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE approval_requests SET status = ?, consumed_at = ? WHERE id = ? AND status = ?"
                " AND action = ? AND target = ? AND operation_digest = ? AND expires_at > ?",
                (ApprovalStatus.CONSUMED.value, iso(), approval_id, ApprovalStatus.APPROVED.value,
                 action.value, target, operation_digest, iso()),
            )
            if cur.rowcount != 1:
                raise PreconditionFailedError("approval could not be consumed (expired or already used)")
        return self.get(approval_id)
