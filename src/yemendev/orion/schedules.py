"""ORION recurring schedules (stage 24): periodic scans, repair runs, vulnerability checks, backups and recovery
readiness checks.

* A schedule is created by a human and every run acts **as that human**, with
  ``min(role at creation, highest role of their currently active tokens)`` - the same rule as the task queue.
  Each run re-checks the permission for its kind (and project membership). A creator with no active token
  disables the schedule (``disabled_reason``) instead of running it.
* A run is claimed atomically (compare-and-set on ``next_run_at``), so several workers or processes never run
  the same schedule twice. After downtime a schedule runs **once** and is re-timed from now: no catch-up storm.
* ``repair`` only queues a repair task (skipped while the project already has queued or running work); changes
  still need a human approval. ``backup`` creates and then verifies a backup.
* Every run is audited; failures are counted and kept (``last_status``/``last_detail``), not retried in a loop.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from yemendev.core.contracts import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    YemenDevError,
    iso,
    new_id,
    parse_iso,
    utcnow,
)
from yemendev.governance.actors import ROLE_RANK, Actor, ActorKind, Role
from yemendev.governance.policy import Action

if TYPE_CHECKING:
    from yemendev.platform import Platform

MIN_INTERVAL = 300
MAX_INTERVAL = 90 * 86400


class ScheduleKind(StrEnum):
    SCAN = "scan"
    REPAIR = "repair"
    VULNERABILITIES = "vulnerabilities"
    BACKUP = "backup"
    READINESS = "readiness"


PROJECT_KINDS = (ScheduleKind.SCAN, ScheduleKind.REPAIR, ScheduleKind.VULNERABILITIES)
# what the creator must be allowed to do, now and at every run
REQUIRED: dict[ScheduleKind, tuple[Action, ...]] = {
    ScheduleKind.SCAN: (Action.SCAN_PROJECT,),
    ScheduleKind.REPAIR: (Action.CREATE_TASK, Action.ADVANCE_TASK),
    ScheduleKind.VULNERABILITIES: (Action.READ,),
    ScheduleKind.BACKUP: (Action.MANAGE_BACKUPS,),
    ScheduleKind.READINESS: (Action.READ,),
}


class ScheduleCreate(BaseModel):
    kind: ScheduleKind
    project_id: str | None = None
    interval_seconds: int = Field(ge=MIN_INTERVAL, le=MAX_INTERVAL)
    first_run_in_seconds: int = Field(default=0, ge=0, le=MAX_INTERVAL)


class Schedule(BaseModel):
    id: str
    kind: ScheduleKind
    project_id: str | None
    interval_seconds: int
    next_run_at: str
    enabled: bool
    created_by: str
    created_role: Role
    created_at: str
    last_run_at: str | None
    last_status: str | None
    last_detail: str | None
    runs: int
    failures: int
    disabled_reason: str | None


def _row(r: Any) -> Schedule:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return Schedule(**d)


def parse_interval(text: str) -> int:
    """``90m``, ``6h``, ``1d``, ``3600`` -> seconds."""
    t = text.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(t[-1:], None)
    try:
        n = int(t[:-1]) * mult if mult else int(t)
    except ValueError as exc:
        raise InvalidInputError(f"invalid interval {text!r} (use e.g. 30m, 6h, 1d)") from exc
    if not MIN_INTERVAL <= n <= MAX_INTERVAL:
        raise InvalidInputError(
            f"interval must be between {MIN_INTERVAL // 60} minutes and {MAX_INTERVAL // 86400} days")
    return n


class Schedules:
    def __init__(self, platform: Platform) -> None:
        self.p = platform

    # ---- management ------------------------------------------------------------------------
    def _authorize(self, actor: Actor, kind: ScheduleKind, project_id: str | None) -> None:
        if actor.kind is not ActorKind.HUMAN:
            raise PermissionDeniedError("only humans create schedules (agents never schedule work)")
        for action in REQUIRED[kind]:
            self.p.guard.require(actor, action, project_id or "platform", project_id=project_id)

    def create(self, actor: Actor, req: ScheduleCreate) -> Schedule:
        if req.kind in PROJECT_KINDS:
            if not req.project_id:
                raise InvalidInputError(f"a {req.kind.value} schedule needs a project")
            self.p.project(req.project_id)  # 404 if unknown
        elif req.project_id:
            raise InvalidInputError(f"a {req.kind.value} schedule is platform-wide; do not pass a project")
        self._authorize(actor, req.kind, req.project_id)
        sid = new_id("sch")
        now = utcnow()
        with self.p.db.transaction() as conn:
            conn.execute(
                "INSERT INTO schedules(id, kind, project_id, interval_seconds, next_run_at, enabled, created_by,"
                " created_role, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (sid, req.kind.value, req.project_id, req.interval_seconds,
                 iso(now + timedelta(seconds=req.first_run_in_seconds)), actor.ref, actor.role.value, iso(now)))
        self.p.audit.record(actor=actor.ref, action="schedule.create", target=sid, outcome="ok",
                            details={"kind": req.kind.value, "project_id": req.project_id,
                                     "interval_seconds": req.interval_seconds})
        return self.get(sid)

    def get(self, schedule_id: str) -> Schedule:
        with self.p.db.read() as conn:
            r = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if r is None:
            raise NotFoundError("schedule not found")
        return _row(r)

    def visible(self, actor: Actor) -> list[Schedule]:
        self.p.guard.require(actor, Action.READ, "schedules")
        with self.p.db.read() as conn:
            rows = [_row(r) for r in conn.execute("SELECT * FROM schedules ORDER BY created_at")]
        return [s for s in rows if s.project_id is None or self.p.guard.can_read(actor, s.project_id)]

    def _owner_or_admin(self, actor: Actor, s: Schedule) -> None:
        if actor.ref != s.created_by and actor.role is not Role.ADMIN:
            raise PermissionDeniedError("only the schedule's creator or an admin can change it")

    def set_enabled(self, actor: Actor, schedule_id: str, enabled: bool) -> Schedule:
        s = self.get(schedule_id)
        self._owner_or_admin(actor, s)
        if enabled:  # re-enabling acts with the *current* caller's rights for this kind
            self._authorize(actor, s.kind, s.project_id)
        with self.p.db.transaction() as conn:
            conn.execute("UPDATE schedules SET enabled = ?, disabled_reason = ?, next_run_at = ? WHERE id = ?",
                         (int(enabled), None if enabled else f"disabled by {actor.ref}",
                          iso(utcnow()) if enabled else s.next_run_at, schedule_id))
        self.p.audit.record(actor=actor.ref, action="schedule.enable" if enabled else "schedule.disable",
                            target=schedule_id, outcome="ok")
        return self.get(schedule_id)

    def delete(self, actor: Actor, schedule_id: str) -> None:
        s = self.get(schedule_id)
        self._owner_or_admin(actor, s)
        with self.p.db.transaction() as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self.p.audit.record(actor=actor.ref, action="schedule.delete", target=schedule_id, outcome="ok",
                            details={"kind": s.kind.value, "project_id": s.project_id})

    # ---- execution -------------------------------------------------------------------------
    def _claim(self, s: Schedule) -> bool:
        """Compare-and-set: only the caller that moves ``next_run_at`` forward runs this occurrence."""
        nxt = iso(utcnow() + timedelta(seconds=s.interval_seconds))
        with self.p.db.transaction() as conn:
            cur = conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ? AND next_run_at = ? AND enabled = 1",
                               (nxt, s.id, s.next_run_at))
            return cur.rowcount == 1

    def _acting(self, s: Schedule) -> Actor | None:
        kind, _, ident = s.created_by.partition(":")
        if kind != ActorKind.HUMAN.value:
            return None
        current = self.p.tokens.current_role(ident)
        if current is None:
            return None
        return Actor(id=ident, kind=ActorKind.HUMAN, role=min(s.created_role, current, key=lambda r: ROLE_RANK[r]))

    def _finish(self, s: Schedule, status: str, detail: str, failed: bool = False, disable: str | None = None) -> None:
        with self.p.db.transaction() as conn:
            conn.execute(
                "UPDATE schedules SET last_run_at = ?, last_status = ?, last_detail = ?, runs = runs + 1,"
                " failures = failures + ?, enabled = CASE WHEN ? IS NULL THEN enabled ELSE 0 END,"
                " disabled_reason = COALESCE(?, disabled_reason) WHERE id = ?",
                (iso(), status, detail[:1000], int(failed), disable, disable, s.id))
        self.p.audit.record(actor=s.created_by, action=f"schedule.run.{s.kind.value}", target=s.id, outcome=status,
                            details={"project_id": s.project_id, "detail": detail[:300]})

    def run_due(self, limit: int = 10) -> list[dict[str, Any]]:
        """Run every enabled schedule whose time has come (each at most once). Returns what happened."""
        now = iso()
        with self.p.db.read() as conn:
            due = [_row(r) for r in conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at LIMIT ?",
                (now, limit))]
        out = []
        for s in due:
            if not self._claim(s):
                continue  # another worker took it
            out.append({"schedule_id": s.id, "kind": s.kind.value, **self._run(s)})
        return out

    def _run(self, s: Schedule) -> dict[str, str]:
        actor = self._acting(s)
        if actor is None:
            reason = f"{s.created_by} has no active token; schedule disabled (re-enable it as an active user)"
            self._finish(s, "dropped", reason, failed=True, disable=reason)
            return {"status": "dropped", "detail": reason}
        try:
            self._authorize(actor, s.kind, s.project_id)
            status, detail = self._execute(actor, s)
            self._finish(s, status, detail)
        except PermissionDeniedError as exc:
            reason = f"not allowed any more: {exc.message}"
            self._finish(s, "denied", reason, failed=True, disable=reason)
            return {"status": "denied", "detail": reason}
        except YemenDevError as exc:
            status, detail = "failed", f"{exc.__class__.__name__}: {exc.message}"
            self._finish(s, status, detail, failed=True)
        return {"status": status, "detail": detail}

    def _execute(self, actor: Actor, s: Schedule) -> tuple[str, str]:
        p, pid = self.p, s.project_id or ""
        if s.kind is ScheduleKind.SCAN:
            _, snap = p.scan_project(actor, pid)
            return "ok", f"{len(snap.files)} files, {len(snap.modules)} modules scanned"
        if s.kind is ScheduleKind.VULNERABILITIES:
            v = p.vulnerabilities(actor, pid)
            return ("attention" if v.findings else "ok"), (
                f"{len(v.findings)} known vulnerability(ies) in {v.checked} checked component(s); "
                f"{len(v.not_checkable)} not checkable")
        if s.kind is ScheduleKind.REPAIR:
            from yemendev.orion.queue import QueueOptions
            from yemendev.orion.workflow import RepairWorkflow

            wf = RepairWorkflow(p)
            busy = [e for e in wf.queue.list(limit=1000) if e.project_id == pid
                    and e.state.value in ("queued", "running", "waiting")]
            if busy:
                return "skipped", f"the project already has queued or running work ({busy[0].task_id})"
            t = wf.create(actor, pid)
            wf.enqueue(actor, t.id, QueueOptions())
            return "ok", f"repair task {t.id} queued (changes still need a human approval)"
        if s.kind is ScheduleKind.BACKUP:
            b = p.create_backup(actor)
            v = p.verify_backup(actor, b["id"])
            if not v["verified_ok"]:
                return "attention", f"backup {b['id']} created but verification FAILED"
            return "ok", f"backup {b['id']} created and verified"
        if s.kind is ScheduleKind.READINESS:
            r = p.recovery_readiness(actor)
            bad = [c["check"] for c in r["checks"] if c["status"] != "ok"]
            return ("ok" if r["overall"] == "ok" else "attention"), (
                f"overall {r['overall']}" + (f"; not ok: {', '.join(bad)}" if bad else ""))
        raise ConflictError(f"unknown schedule kind {s.kind}")


def is_overdue(s: Schedule, factor: float = 2.0) -> bool:
    """An enabled schedule that has not run for ``factor`` intervals (e.g. no worker is running)."""
    if not s.enabled:
        return False
    ref = s.last_run_at or s.created_at
    return (utcnow() - parse_iso(ref)).total_seconds() > factor * s.interval_seconds + MIN_INTERVAL
