"""Persistent task state for ORION."""

from __future__ import annotations

import builtins
import json
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from yemendev.core.contracts import ConflictError, NotFoundError, canonical_json, iso, new_id, utcnow
from yemendev.storage.db import Database


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"            # repair applied and verified
    NO_ACTION = "no_action"            # nothing to repair / no acceptable plan
    ROLLED_BACK = "rolled_back"        # change applied, verification failed, original restored
    FAILED = "failed"                  # workflow error; see error + evidence
    CANCELLED = "cancelled"


TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.NO_ACTION, TaskStatus.ROLLED_BACK, TaskStatus.FAILED,
            TaskStatus.CANCELLED}


class StepStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Task(BaseModel):
    id: str
    project_id: str
    kind: str
    status: TaskStatus
    created_by: str
    created_at: str
    updated_at: str
    params: dict[str, Any]
    current_step: str | None
    error: str | None


class StepRecord(BaseModel):
    name: str
    status: StepStatus
    attempt: int
    started_at: str | None
    finished_at: str | None
    output: Any
    error: str | None


def _task(r: Any) -> Task:
    return Task(id=r["id"], project_id=r["project_id"], kind=r["kind"], status=TaskStatus(r["status"]),
                created_by=r["created_by"], created_at=r["created_at"], updated_at=r["updated_at"],
                params=json.loads(r["params_json"]), current_step=r["current_step"], error=r["error"])


class TaskStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, project_id: str, kind: str, created_by: str, params: dict[str, Any]) -> Task:
        tid, now = new_id("tsk"), iso()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(id, project_id, kind, status, created_by, created_at, updated_at, params_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tid, project_id, kind, TaskStatus.PENDING.value, created_by, now, now, canonical_json(params)),
            )
        return self.get(tid)

    def get(self, task_id: str) -> Task:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None:
            raise NotFoundError("task not found")
        return _task(r)

    def list(self, project_id: str | None = None, status: TaskStatus | None = None, limit: int = 100) -> list[Task]:
        q, args = "SELECT * FROM tasks WHERE 1=1", []
        if project_id:
            q += " AND project_id = ?"
            args.append(project_id)
        if status:
            q += " AND status = ?"
            args.append(status.value)
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._db.read() as conn:
            return [_task(r) for r in conn.execute(q, (*args, max(1, min(limit, 500)))).fetchall()]

    def acquire(self, task_id: str, owner: str, lease_seconds: int = 900) -> Task:
        """Take an exclusive lease. A RUNNING task whose lease expired (crashed worker) can be re-acquired."""
        now = utcnow()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ? WHERE id = ? AND ("
                " status IN (?, ?) OR (status = ? AND (lease_expires_at IS NULL OR lease_expires_at < ? OR"
                " lease_owner = ?)))",
                (TaskStatus.RUNNING.value, owner, iso(now + timedelta(seconds=lease_seconds)), iso(now), task_id,
                 TaskStatus.PENDING.value, TaskStatus.WAITING_APPROVAL.value, TaskStatus.RUNNING.value, iso(now),
                 owner),
            )
            if cur.rowcount != 1:
                t = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if t is None:
                    raise NotFoundError("task not found")
                raise ConflictError(f"task is {t['status']} and cannot be run now (terminal or leased)")
        return self.get(task_id)

    def release(self, task_id: str, status: TaskStatus, error: str | None = None) -> Task:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?"
                " WHERE id = ?",
                (status.value, error, iso(), task_id),
            )
        return self.get(task_id)

    def expire_lease(self, task_id: str) -> None:
        """Simulate/handle a crashed worker (used by recovery tooling and tests)."""
        with self._db.transaction() as conn:
            conn.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ?",
                         ("2000-01-01T00:00:00.000000+00:00", task_id))

    def cancel(self, task_id: str) -> Task:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
                (TaskStatus.CANCELLED.value, iso(), task_id, TaskStatus.PENDING.value,
                 TaskStatus.WAITING_APPROVAL.value),
            )
            if cur.rowcount != 1:
                raise ConflictError("only pending or waiting tasks can be cancelled")
        return self.get(task_id)

    # ---- steps -----------------------------------------------------------------------
    def step(self, task_id: str, name: str) -> StepRecord | None:
        with self._db.read() as conn:
            r = conn.execute("SELECT * FROM task_steps WHERE task_id = ? AND name = ?", (task_id, name)).fetchone()
        if r is None:
            return None
        return StepRecord(name=r["name"], status=StepStatus(r["status"]), attempt=r["attempt"],
                          started_at=r["started_at"], finished_at=r["finished_at"],
                          output=json.loads(r["output_json"]) if r["output_json"] else None, error=r["error"])

    def steps(self, task_id: str) -> builtins.list[StepRecord]:
        with self._db.read() as conn:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM task_steps WHERE task_id = ? ORDER BY started_at", (task_id,)).fetchall()]
        return [s for n in names if (s := self.step(task_id, n))]

    def start_step(self, task_id: str, name: str) -> int:
        with self._db.transaction() as conn:
            r = conn.execute("SELECT attempt FROM task_steps WHERE task_id = ? AND name = ?",
                             (task_id, name)).fetchone()
            attempt = (r["attempt"] + 1) if r else 1
            conn.execute(
                "INSERT INTO task_steps(task_id, name, status, attempt, started_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(task_id, name) DO UPDATE SET status = excluded.status, attempt = excluded.attempt,"
                " started_at = excluded.started_at, finished_at = NULL, error = NULL",
                (task_id, name, StepStatus.RUNNING.value, attempt, iso()),
            )
            conn.execute("UPDATE tasks SET current_step = ?, updated_at = ? WHERE id = ?", (name, iso(), task_id))
        return attempt

    def finish_step(self, task_id: str, name: str, output: Any) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE task_steps SET status = ?, finished_at = ?, output_json = ? WHERE task_id = ? AND"
                         " name = ?", (StepStatus.DONE.value, iso(), canonical_json(output), task_id, name))

    def fail_step(self, task_id: str, name: str, error: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE task_steps SET status = ?, finished_at = ?, error = ? WHERE task_id = ? AND name = ?",
                         (StepStatus.FAILED.value, iso(), error[:4000], task_id, name))
