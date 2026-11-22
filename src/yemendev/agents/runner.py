"""AgentRunner: the only way ORION invokes an agent.

For every invocation: (1) each requested action must be in the agent's fixed allow-list, (2) the policy
engine must allow it for an AGENT actor, (3) a row is written to ``agent_runs`` before and after, with
input/output digests, so every agent use is traceable. Denials are audited.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from yemendev.agents.registry import AGENTS, AgentSpec
from yemendev.core.contracts import NotFoundError, PermissionDeniedError, digest, iso, new_id
from yemendev.governance.policy import Action, PolicyEngine
from yemendev.security.audit import AuditLog
from yemendev.storage.db import Database

T = TypeVar("T")


class AgentRun(BaseModel):
    id: str
    task_id: str | None
    project_id: str | None
    agent: str
    operation: str
    actions: list[str]
    status: str  # running | succeeded | stopped | failed | denied
    started_at: str
    finished_at: str | None
    input_digest: str
    output_digest: str | None
    error: str | None


class AgentRunner:
    def __init__(self, db: Database, policy: PolicyEngine, audit: AuditLog) -> None:
        self._db = db
        self._policy = policy
        self._audit = audit

    def spec(self, name: str) -> AgentSpec:
        if name not in AGENTS:
            raise NotFoundError("unknown agent")
        return AGENTS[name]

    def run(self, agent: str, operation: str, actions: tuple[Action, ...], fn: Callable[[], T], *,
            task_id: str | None = None, project_id: str | None = None, inputs: Any = None,
            passthrough: tuple[type[BaseException], ...] = ()) -> T:
        spec = self.spec(agent)
        run_id = new_id("arun")
        in_digest = digest(inputs) if inputs is not None else digest([agent, operation, task_id])
        denied = [a for a in actions
                  if a not in spec.allowed_actions or not self._policy.evaluate(spec.actor, a).allowed]
        self._insert(run_id, task_id, project_id, agent, operation, actions, "denied" if denied else "running",
                     in_digest)
        if denied:
            reason = f"agent '{agent}' is not permitted to perform: {', '.join(a.value for a in denied)}"
            self._finish(run_id, "denied", None, reason)
            self._audit.record(actor=spec.actor.ref, action=denied[0].value, target=task_id or project_id or "-",
                               outcome="denied", details={"reason": reason, "operation": operation})
            raise PermissionDeniedError(reason)
        try:
            out = fn()
        except passthrough as exc:  # control-flow outcomes (e.g. "no action needed")
            self._finish(run_id, "stopped", None, str(exc)[:2000])
            raise
        except Exception as exc:
            self._finish(run_id, "failed", None, f"{exc.__class__.__name__}: {exc}"[:2000])
            raise
        self._finish(run_id, "succeeded", _safe_digest(out), None)
        return out

    def _insert(self, run_id: str, task_id: str | None, project_id: str | None, agent: str, operation: str,
                actions: tuple[Action, ...], status: str, in_digest: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO agent_runs(id, task_id, project_id, agent, operation, actions_json, status, started_at,"
                " input_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, project_id, agent, operation, json.dumps(sorted(a.value for a in actions)), status,
                 iso(), in_digest),
            )

    def _finish(self, run_id: str, status: str, out_digest: str | None, error: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE agent_runs SET status = ?, finished_at = ?, output_digest = ?, error = ? WHERE id = ?",
                         (status, iso(), out_digest, error, run_id))

    def runs(self, agent: str | None = None, task_id: str | None = None, limit: int = 200) -> builtins.list[AgentRun]:
        q, args = "SELECT * FROM agent_runs WHERE 1=1", []
        if agent:
            q += " AND agent = ?"
            args.append(agent)
        if task_id:
            q += " AND task_id = ?"
            args.append(task_id)
        q += " ORDER BY started_at, rowid LIMIT ?"
        with self._db.read() as conn:
            rows = conn.execute(q, (*args, max(1, min(limit, 2000)))).fetchall()
        return [AgentRun(id=r["id"], task_id=r["task_id"], project_id=r["project_id"], agent=r["agent"],
                         operation=r["operation"], actions=json.loads(r["actions_json"]), status=r["status"],
                         started_at=r["started_at"], finished_at=r["finished_at"], input_digest=r["input_digest"],
                         output_digest=r["output_digest"], error=r["error"]) for r in rows]


def _safe_digest(out: Any) -> str:
    try:
        return digest(out.model_dump(mode="json") if isinstance(out, BaseModel) else out)
    except (TypeError, ValueError):
        return digest(repr(out))
