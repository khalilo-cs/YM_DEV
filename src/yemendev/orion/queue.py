"""ORION task queue: scheduling, dependencies, resource allocation and claim recovery (directive 5.2).

The queue never changes *what* a task does - the persistent workflow (``workflow.py``) does that. It only
decides *when* a background worker may advance a task, and *with whose rights*:

* Scheduling: highest ``priority`` first, then earliest ``run_after`` (delays and retry backoff), then FIFO.
* Dependencies: a task waits until every task it depends on is terminal. A dependency that ended other
  than ``succeeded`` / ``no_action`` cancels the dependent task (recorded with the reason).
* Resource allocation: at most one run per project at a time (runs mutate the same project files), no run
  while someone advances the project's task by hand, and at most ``max_concurrent_runs`` runs in total
  across all worker processes sharing the database.
* Approvals: a task waiting for a human decision is re-run automatically once the decision is made.
* Rights: a run acts as the human who queued it, with ``min(role at enqueue, highest role of that human's
  currently active tokens)``; with no active token the entry is dropped. Project membership is still
  checked by the workflow on every step.
* Recovery: claims carry a heartbeat; a claim whose worker died is returned to the queue (counted as an
  attempt) and the task resumes from its last completed step.

Every decision is appended to the task's execution trace (``task_events``).
"""

from __future__ import annotations

import builtins
import json
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from yemendev.core.config import OrionSettings
from yemendev.core.contracts import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    canonical_json,
    iso,
    new_id,
    parse_iso,
    utcnow,
)
from yemendev.governance.actors import ROLE_RANK, Actor, ActorKind, Role
from yemendev.orion.tasks import TERMINAL, TaskStatus, add_event
from yemendev.security.tokens import TokenStore
from yemendev.storage.db import Database

HEALTHY_DEPENDENCY = {TaskStatus.SUCCEEDED, TaskStatus.NO_ACTION}


class QueueState(StrEnum):
    QUEUED = "queued"        # waiting for its time, its dependencies or a free slot
    RUNNING = "running"      # claimed by a worker (heartbeat)
    WAITING = "waiting"      # task waits for a human approval; re-run once decided
    DONE = "done"            # task reached a terminal status
    CANCELLED = "cancelled"  # by a human, or because a dependency did not succeed
    FAILED = "failed"        # gave up (attempts exhausted / timeout); task marked failed
    DROPPED = "dropped"      # the human who queued it no longer has an active credential


ACTIVE = (QueueState.QUEUED, QueueState.RUNNING, QueueState.WAITING)


class QueueOptions(BaseModel):
    priority: int = Field(default=0, ge=-10, le=10)
    delay_seconds: int = Field(default=0, ge=0, le=30 * 86400)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=5, le=86400)
    depends_on: list[str] = Field(default_factory=list, max_length=20)


class QueueEntry(BaseModel):
    id: str
    task_id: str
    project_id: str
    state: QueueState
    priority: int
    run_after: str
    enqueued_at: str
    enqueued_by: str
    enqueued_role: Role
    depends_on: list[str]
    attempts: int
    max_attempts: int
    timeout_seconds: int
    worker: str | None
    run_id: str | None
    heartbeat_until: str | None
    last_error: str | None
    finished_at: str | None


class Claim(BaseModel):
    entry: QueueEntry
    actor: Actor
    run_id: str


def _entry(r: Any) -> QueueEntry:
    return QueueEntry(id=r["id"], task_id=r["task_id"], project_id=r["project_id"], state=QueueState(r["state"]),
                      priority=r["priority"], run_after=r["run_after"], enqueued_at=r["enqueued_at"],
                      enqueued_by=r["enqueued_by"], enqueued_role=Role(r["enqueued_role"]),
                      depends_on=json.loads(r["depends_on_json"]), attempts=r["attempts"],
                      max_attempts=r["max_attempts"], timeout_seconds=r["timeout_seconds"], worker=r["worker"],
                      run_id=r["run_id"], heartbeat_until=r["heartbeat_until"], last_error=r["last_error"],
                      finished_at=r["finished_at"])


def _actor_from_ref(ref: str) -> tuple[ActorKind, str]:
    kind, _, ident = ref.partition(":")
    return ActorKind(kind), ident


class TaskQueue:
    def __init__(self, db: Database, tokens: TokenStore, settings: OrionSettings) -> None:
        self._db = db
        self._tokens = tokens
        self.settings = settings

    # ---- enqueue / inspect ---------------------------------------------------------------
    def enqueue(self, task_id: str, actor: Actor, opts: QueueOptions) -> QueueEntry:
        """Queue a task for background execution. Authorization is the caller's job (Guard)."""
        if actor.kind is not ActorKind.HUMAN:
            raise InvalidInputError("only humans queue tasks")
        deps = list(dict.fromkeys(opts.depends_on))
        if task_id in deps:
            raise InvalidInputError("a task cannot depend on itself")
        now = utcnow()
        with self._db.transaction() as conn:
            t = conn.execute("SELECT project_id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if t is None:
                raise NotFoundError("task not found")
            if TaskStatus(t["status"]) in TERMINAL:
                raise ConflictError(f"task is already {t['status']}")
            for d in deps:
                if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (d,)).fetchone() is None:
                    raise InvalidInputError(f"dependency {d} does not exist")
            if self._reaches(conn, deps, task_id):
                raise InvalidInputError("dependency cycle")
            cur = conn.execute("SELECT state FROM task_queue WHERE task_id = ?", (task_id,)).fetchone()
            if cur is not None and QueueState(cur["state"]) in ACTIVE:
                raise ConflictError(f"task is already queued ({cur['state']})")
            values = (task_id, t["project_id"], QueueState.QUEUED.value, opts.priority,
                      iso(now + timedelta(seconds=opts.delay_seconds)), iso(now), actor.ref, actor.role.value,
                      canonical_json(deps), 0, opts.max_attempts or self.settings.max_attempts,
                      opts.timeout_seconds or self.settings.run_timeout_seconds)
            conn.execute(
                "INSERT INTO task_queue(id, task_id, project_id, state, priority, run_after, enqueued_at, enqueued_by,"
                " enqueued_role, depends_on_json, attempts, max_attempts, timeout_seconds) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state = excluded.state,"
                " priority = excluded.priority, run_after = excluded.run_after, enqueued_at = excluded.enqueued_at,"
                " enqueued_by = excluded.enqueued_by, enqueued_role = excluded.enqueued_role,"
                " depends_on_json = excluded.depends_on_json, attempts = 0, max_attempts = excluded.max_attempts,"
                " timeout_seconds = excluded.timeout_seconds, worker = NULL, run_id = NULL, heartbeat_until = NULL,"
                " last_error = NULL, finished_at = NULL",
                (new_id("que"), *values))
            add_event(conn, task_id, "queue.enqueued", actor.ref, {
                "priority": opts.priority, "run_after": values[4], "depends_on": deps,
                "max_attempts": values[10], "timeout_seconds": values[11]})
        return self.get(task_id)  # type: ignore[return-value]

    @staticmethod
    def _reaches(conn: Any, start: builtins.list[str], target: str) -> bool:
        """True if following queue dependencies from ``start`` reaches ``target`` (would form a cycle)."""
        seen: set[str] = set()
        stack = list(start)
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            r = conn.execute("SELECT depends_on_json FROM task_queue WHERE task_id = ?", (cur,)).fetchone()
            if r is not None:
                stack.extend(json.loads(r["depends_on_json"]))
        return False

    def get(self, task_id: str) -> QueueEntry | None:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM task_queue WHERE task_id = ?", (task_id,)).fetchone()
        return _entry(r) if r else None

    def list(self, states: builtins.list[QueueState] | None = None, limit: int = 200) -> builtins.list[QueueEntry]:
        q, args = "SELECT * FROM task_queue", []
        if states:
            q += f" WHERE state IN ({','.join('?' * len(states))})"
            args = [s.value for s in states]
        q += " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'waiting' THEN 2 ELSE 3 END,"
        q += " priority DESC, run_after, enqueued_at LIMIT ?"
        with self._db.read() as conn:
            return [_entry(r) for r in conn.execute(q, (*args, max(1, min(limit, 1000)))).fetchall()]

    # ---- claiming ------------------------------------------------------------------------
    def claim(self, worker: str) -> Claim | None:
        """Atomically pick the next runnable entry, or None. Resolves dependencies as a side effect."""
        now = utcnow()
        with self._db.transaction() as conn:
            running = conn.execute("SELECT COUNT(*) FROM task_queue WHERE state = 'running'").fetchone()[0]
            if running >= self.settings.max_concurrent_runs:
                return None
            rows = conn.execute(
                "SELECT * FROM task_queue WHERE state IN ('queued', 'waiting') AND run_after <= ?"
                " ORDER BY priority DESC, run_after, enqueued_at", (iso(now),)).fetchall()
            for r in rows:
                e = _entry(r)
                if not self._runnable(conn, e, now):
                    continue
                kind, ident = _actor_from_ref(e.enqueued_by)
                current = self._tokens.current_role(ident) if kind is ActorKind.HUMAN else None
                if current is None:
                    self._close(conn, e, QueueState.DROPPED, "the human who queued this task has no active token")
                    continue
                role = min(e.enqueued_role, current, key=lambda x: ROLE_RANK[x])
                run_id = new_id("run")
                conn.execute(
                    "UPDATE task_queue SET state = 'running', attempts = attempts + 1, worker = ?, run_id = ?,"
                    " heartbeat_until = ? WHERE id = ?",
                    (worker, run_id, iso(now + timedelta(seconds=self.settings.heartbeat_seconds)), e.id))
                add_event(conn, e.task_id, "queue.claimed", worker, {
                    "run_id": run_id, "attempt": e.attempts + 1, "max_attempts": e.max_attempts,
                    "acting_as": e.enqueued_by, "role": role.value})
                fresh = _entry(conn.execute("SELECT * FROM task_queue WHERE id = ?", (e.id,)).fetchone())
                return Claim(entry=fresh, actor=Actor(id=ident, kind=kind, role=role), run_id=run_id)
        return None

    def _runnable(self, conn: Any, e: QueueEntry, now: Any) -> bool:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (e.task_id,)).fetchone()
        status = TaskStatus(t["status"])
        if status in TERMINAL:  # finished by other means (manual advance, cancel)
            self._close(conn, e, QueueState.DONE, f"task already {status.value}")
            return False
        for d in e.depends_on:
            ds = TaskStatus(conn.execute("SELECT status FROM tasks WHERE id = ?", (d,)).fetchone()["status"])
            if ds not in TERMINAL:
                return False
            if ds not in HEALTHY_DEPENDENCY:
                reason = f"dependency {d} ended {ds.value}"
                self._close(conn, e, QueueState.CANCELLED, reason)
                if status in (TaskStatus.PENDING, TaskStatus.WAITING_APPROVAL):
                    conn.execute("UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                                 (TaskStatus.CANCELLED.value, reason, iso(), e.task_id))
                    add_event(conn, e.task_id, "task.status", detail={"status": "cancelled", "error": reason})
                return False
        if status is TaskStatus.WAITING_APPROVAL and conn.execute(
                "SELECT 1 FROM approval_requests WHERE task_id = ? AND status = 'pending' AND expires_at > ?",
                (e.task_id, iso(now))).fetchone():
            return False  # still waiting for a human decision
        busy = conn.execute(
            "SELECT 1 FROM task_queue WHERE project_id = ? AND state = 'running' UNION ALL"
            " SELECT 1 FROM tasks WHERE project_id = ? AND status = 'running' AND lease_expires_at > ?",
            (e.project_id, e.project_id, iso(now))).fetchone()
        return busy is None

    # ---- worker callbacks ----------------------------------------------------------------
    def heartbeat(self, entry_id: str, run_id: str) -> bool:
        """Extend the claim. False means the claim is no longer ours (recovered or closed)."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE task_queue SET heartbeat_until = ? WHERE id = ? AND run_id = ? AND state = 'running'",
                (iso(utcnow() + timedelta(seconds=self.settings.heartbeat_seconds)), entry_id, run_id))
            return cur.rowcount == 1

    def finish_run(self, entry_id: str, run_id: str, task_status: TaskStatus, detail: dict[str, Any]) -> QueueEntry:
        """Record the end of a run in which the workflow itself decided the task's status."""
        state = (QueueState.WAITING if task_status is TaskStatus.WAITING_APPROVAL
                 else QueueState.QUEUED if task_status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                 else QueueState.DONE)
        with self._db.transaction() as conn:
            e = self._own(conn, entry_id, run_id)
            conn.execute("UPDATE task_queue SET state = ?, worker = NULL, heartbeat_until = NULL, finished_at = ?"
                         " WHERE id = ?", (state.value, iso() if state is QueueState.DONE else None, entry_id))
            add_event(conn, e.task_id, "queue.run_finished", e.worker or "system:orion",
                      {**detail, "run_id": run_id, "task_status": task_status.value, "queue_state": state.value})
        return self.get(e.task_id)  # type: ignore[return-value]

    def retry_or_fail(self, entry_id: str, run_id: str, error: str, detail: dict[str, Any]) -> tuple[QueueEntry, bool]:
        """After a transient failure / timeout / worker crash: back off and requeue, or give up.

        Returns (entry, gave_up). On give-up the caller marks the task failed."""
        with self._db.transaction() as conn:
            e = self._own(conn, entry_id, run_id)
            if e.attempts >= e.max_attempts:
                conn.execute("UPDATE task_queue SET state = 'failed', worker = NULL, heartbeat_until = NULL,"
                             " last_error = ?, finished_at = ? WHERE id = ?", (error[:2000], iso(), entry_id))
                add_event(conn, e.task_id, "queue.gave_up", detail={
                    **detail, "run_id": run_id, "attempts": e.attempts, "error": error[:500]})
                gave_up = True
            else:
                delay = self.backoff(e.attempts)
                after = iso(utcnow() + timedelta(seconds=delay))
                conn.execute("UPDATE task_queue SET state = 'queued', worker = NULL, heartbeat_until = NULL,"
                             " last_error = ?, run_after = ? WHERE id = ?", (error[:2000], after, entry_id))
                add_event(conn, e.task_id, "queue.retry_scheduled", detail={
                    **detail, "run_id": run_id, "attempt": e.attempts, "max_attempts": e.max_attempts,
                    "delay_seconds": delay, "run_after": after, "error": error[:500]})
                gave_up = False
        return self.get(e.task_id), gave_up  # type: ignore[return-value]

    def backoff(self, attempts: int) -> float:
        """Exponential backoff without jitter (deterministic, auditable): base * 2^(attempts-1), capped."""
        s = self.settings
        return float(min(s.retry_max_seconds, s.retry_base_seconds * 2 ** max(0, attempts - 1)))

    def release_claim(self, entry_id: str, run_id: str, reason: str) -> QueueEntry:
        """Give a claim back without counting the attempt (the worker is shutting down, not the task failing)."""
        with self._db.transaction() as conn:
            e = self._own(conn, entry_id, run_id)
            conn.execute("UPDATE task_queue SET state = 'queued', worker = NULL, heartbeat_until = NULL,"
                         " attempts = MAX(0, attempts - 1), run_after = ? WHERE id = ?", (iso(), entry_id))
            add_event(conn, e.task_id, "queue.released", e.worker or "system:orion",
                      {"run_id": run_id, "reason": reason})
        return self.get(e.task_id)  # type: ignore[return-value]

    def cancel(self, task_id: str, actor_ref: str, reason: str = "cancelled") -> QueueEntry | None:
        """Remove a queued/waiting entry. A running entry is stopped by the worker via the task's cancel flag."""
        with self._db.transaction() as conn:
            r = conn.execute("SELECT * FROM task_queue WHERE task_id = ?", (task_id,)).fetchone()
            if r is None:
                return None
            e = _entry(r)
            if e.state in (QueueState.QUEUED, QueueState.WAITING):
                self._close(conn, e, QueueState.CANCELLED, reason, actor_ref)
        return self.get(task_id)

    def close(self, entry_id: str, state: QueueState, reason: str) -> None:
        with self._db.transaction() as conn:
            r = conn.execute("SELECT * FROM task_queue WHERE id = ?", (entry_id,)).fetchone()
            if r is not None:
                self._close(conn, _entry(r), state, reason)

    def recover_stale(self) -> builtins.list[QueueEntry]:
        """Return claims whose worker stopped heart-beating to the queue (or give up if out of attempts).

        The task's lease is expired as well so the next run can resume it. Returns the affected entries."""
        now = iso()
        out: builtins.list[QueueEntry] = []
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT * FROM task_queue WHERE state = 'running' AND heartbeat_until < ?",
                                (now,)).fetchall()
            for r in rows:
                e = _entry(r)
                conn.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ? AND status = 'running'",
                             ("2000-01-01T00:00:00.000000+00:00", e.task_id))
                if e.attempts >= e.max_attempts:
                    conn.execute("UPDATE task_queue SET state = 'failed', worker = NULL, last_error = ?,"
                                 " finished_at = ? WHERE id = ?", ("worker stopped responding", now, e.id))
                    add_event(conn, e.task_id, "queue.gave_up", detail={
                        "run_id": e.run_id, "attempts": e.attempts, "error": "worker stopped responding"})
                else:
                    conn.execute("UPDATE task_queue SET state = 'queued', worker = NULL, run_after = ?,"
                                 " last_error = ? WHERE id = ?", (now, "worker stopped responding", e.id))
                    add_event(conn, e.task_id, "queue.recovered", detail={
                        "run_id": e.run_id, "previous_worker": e.worker, "attempt": e.attempts})
                out.append(_entry(conn.execute("SELECT * FROM task_queue WHERE id = ?", (e.id,)).fetchone()))
        return out

    # ---- internals -----------------------------------------------------------------------
    @staticmethod
    def _own(conn: Any, entry_id: str, run_id: str) -> QueueEntry:
        r = conn.execute("SELECT * FROM task_queue WHERE id = ?", (entry_id,)).fetchone()
        if r is None or r["run_id"] != run_id or r["state"] != QueueState.RUNNING.value:
            raise ConflictError("queue claim is no longer held by this run")
        return _entry(r)

    @staticmethod
    def _close(conn: Any, e: QueueEntry, state: QueueState, reason: str, actor: str = "system:orion") -> None:
        conn.execute("UPDATE task_queue SET state = ?, worker = NULL, heartbeat_until = NULL, last_error = ?,"
                     " finished_at = ? WHERE id = ?", (state.value, reason[:2000], iso(), e.id))
        add_event(conn, e.task_id, f"queue.{state.value}", actor, {"reason": reason})


def seconds_until(ts: str) -> float:
    return (parse_iso(ts) - utcnow()).total_seconds()
