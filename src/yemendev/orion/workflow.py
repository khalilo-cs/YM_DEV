"""ORION repair workflow: the RACE-X loop for a single project, as a persistent state machine.

Steps (each persisted; completed steps are never re-executed on resume):

  scan -> baseline -> diagnose -> propose -> simulate -> authorize -> execute -> verify
       -> learn                      (verify passed:  SUCCEEDED)
       -> rollback -> learn          (verify failed:  ROLLED_BACK)

Early exits: no reproducible problem / no plan / no plan passes simulation -> NO_ACTION;
approval denied or expired -> CANCELLED. Unexpected errors -> FAILED (evidence kept).

Crash safety: a step interrupted mid-way is re-run on resume. Every step is idempotent
or reconciles real state first (``execute`` inspects the files on disk; ``authorize``
reuses the approval bound to this task).

Transient failures (sandbox / container runtime / model endpoint unavailable, a process that
could not be started) return the task to PENDING with the step marked failed, so it can be
resumed (manually or by the queue's retry policy) instead of failing permanently.

Cancellation of a RUNNING task is cooperative: it is honoured before the next step. If the
change was already applied, the next step is ``rollback`` and the task ends CANCELLED with the
project restored. A task that ends without completing (cancelled, timed out, gave up) never
leaves a half-applied change behind: ``terminate`` restores the task's checkpoint.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.agents.registry import AGENTS
from yemendev.agents.security_agent import PlanReview, Verdict, review_plan
from yemendev.core.contracts import (
    ExecutionFailedError,
    NotFoundError,
    UnavailableError,
    YemenDevError,
    digest,
    new_id,
    parse_iso,
)
from yemendev.core.faults import InjectedFault
from yemendev.diagnostics.engine import DiagnosticReport, diagnose
from yemendev.evidence.store import EvidenceBody
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.approvals import ApprovalStatus
from yemendev.governance.policy import Action
from yemendev.learning.experience import candidate_skill
from yemendev.learning.skills import SkillStatus
from yemendev.memory.model import MemoryCategory, MemoryWrite
from yemendev.observability.tracing import tracer
from yemendev.orion.queue import QueueEntry, QueueOptions, TaskQueue
from yemendev.orion.tasks import TERMINAL, StepStatus, Task, TaskStatus, TaskStore
from yemendev.platform import Platform
from yemendev.recovery.engine import PhaseResult
from yemendev.recovery.model import PlanSource, RepairPlan
from yemendev.recovery.model_proposer import propose_with_model
from yemendev.recovery.proposers import propose_from_skills
from yemendev.sandbox.suites import run_tests
from yemendev.sandbox.testrunner import TestRunReport
from yemendev.sandbox.workspace import Workspace
from yemendev.twin.causal import root_cause_candidates

BASELINE_RUNS = 2
TRANSIENT_ERRORS = (UnavailableError, ExecutionFailedError)
# Steps that never modify the registered project: a queue worker may kill them immediately on cancel.
INTERRUPTIBLE_STEPS = frozenset({"scan", "baseline", "diagnose", "propose", "simulate"})
SYSTEM = Actor(id="orion", kind=ActorKind.SYSTEM, role=Role.OPERATOR)
LEARNING_AGENT = AGENTS["learning"].actor

# Which agent performs each step, and the actions that step needs (checked against the agent's allow-list).
STEP_AGENTS: dict[str, tuple[str, tuple[Action, ...]]] = {
    "scan": ("repository", (Action.SCAN_PROJECT, Action.WRITE_MEMORY)),
    "baseline": ("testing", (Action.RUN_SANDBOXED,)),
    "diagnose": ("diagnostic", (Action.WRITE_MEMORY,)),
    "propose": ("planning", (Action.READ,)),
    "simulate": ("testing", (Action.RUN_SANDBOXED,)),
    "execute": ("recovery", (Action.APPLY_PATCH,)),
    "verify": ("verification", (Action.RUN_SANDBOXED,)),
    "rollback": ("recovery", (Action.ROLLBACK_OWN_CHANGE,)),
    "learn": ("learning", (Action.WRITE_MEMORY, Action.PROPOSE_SKILL)),
}


# Task planning: the fixed, dependency-ordered plan of a repair task (who performs each step, after what).
PLAN_STEPS: list[tuple[str, str, str]] = [
    ("scan", "-", "repository"),
    ("baseline", "scan", "testing"),
    ("diagnose", "baseline", "diagnostic"),
    ("propose", "diagnose", "planning"),
    ("simulate", "propose", "testing"),
    ("authorize", "simulate", "human approval (ORION gate)"),
    ("execute", "authorize", "recovery"),
    ("verify", "execute", "verification"),
    ("rollback", "verify failed, or cancellation after execute", "recovery"),
    ("learn", "verify or rollback", "learning"),
]


class Stop(Exception):  # noqa: N818 - control flow signal, not an error
    def __init__(self, status: TaskStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class AdvanceResult(BaseModel):
    task: Task
    ran_steps: list[str]
    message: str
    retryable: bool = False  # a transient failure: the task is PENDING again and can be resumed


class RepairWorkflow:
    KIND = "repair"

    def __init__(self, platform: Platform) -> None:
        self.p = platform
        self.tasks = TaskStore(platform.db)
        self.queue = TaskQueue(platform.db, platform.tokens, platform.settings.orion)

    # ---- public API ------------------------------------------------------------------
    def create(self, actor: Actor, project_id: str, human_plan: RepairPlan | None = None) -> Task:
        self.p.project(project_id)
        self.p.guard.require(actor, Action.CREATE_TASK, project_id, project_id=project_id)
        params: dict[str, Any] = {"python_version": list(sys.version_info[:2])}
        if human_plan is not None:
            # Regression (audit F1): the client must not choose the plan's provenance. Anything submitted
            # with a task is human-authored by the task creator, so a *different* human must approve it.
            human_plan = human_plan.model_copy(update={"source": PlanSource.HUMAN, "source_ref": actor.ref})
            params["human_plan"] = human_plan.model_dump(mode="json")
        t = self.tasks.create(project_id, self.KIND, actor.ref, params)
        self.p.audit.record(actor=actor.ref, action=Action.CREATE_TASK.value, target=t.id, outcome="ok",
                            details={"project_id": project_id, "human_plan": human_plan is not None})
        return t

    def visible_tasks(self, actor: Actor, project_id: str | None = None) -> list[Task]:
        self.p.guard.require(actor, Action.READ, "tasks")
        return [t for t in self.tasks.list(project_id=project_id) if self.p.guard.can_read(actor, t.project_id)]

    def get_task(self, actor: Actor, task_id: str) -> Task:
        t = self.tasks.get(task_id)
        self.p.guard.require(actor, Action.READ, task_id, project_id=t.project_id)
        return t

    def advance(self, actor: Actor, task_id: str, worker: str | None = None,
                still_owner: Callable[[], bool] | None = None) -> AdvanceResult:
        """Run steps until the task finishes or needs a human. Safe to call repeatedly.

        ``still_owner`` (queue runs) is checked before every step: once the run has lost its queue claim
        (e.g. it was presumed dead and recovered), it stops without touching the task again (fencing)."""
        t = self.tasks.get(task_id)
        self.p.guard.require(actor, Action.ADVANCE_TASK, task_id, project_id=t.project_id)
        if t.status in TERMINAL:
            return AdvanceResult(task=t, ran_steps=[], message=f"task already {t.status.value}")
        t = self.tasks.acquire(task_id, worker or new_id("wrk"))
        ran: list[str] = []
        try:
            while (name := self._next_step(task_id)) is not None:
                if still_owner is not None and not still_owner():
                    return AdvanceResult(task=self.tasks.get(task_id), ran_steps=ran,
                                         message="queue claim lost; stopped before the next step without changes")
                self.tasks.start_step(task_id, name)
                try:
                    with tracer.start_as_current_span(f"orion.step {name}") as span:
                        span.set_attribute("yemendev.task_id", task_id)
                        span.set_attribute("yemendev.step", name)
                        out = self._handlers()[name](actor, t)
                except Stop as s:
                    ran.append(name)
                    if s.status is not TaskStatus.WAITING_APPROVAL:
                        self.tasks.finish_step(task_id, name, {"stopped": s.status.value, "reason": s.reason})
                    # WAITING_APPROVAL leaves the step open so the next advance re-checks the approval.
                    t = self.tasks.release(task_id, s.status, s.reason if s.status is TaskStatus.FAILED else None)
                    return AdvanceResult(task=t, ran_steps=ran, message=s.reason)
                self.p.faults.check(f"step.{name}.before_finish")
                self.tasks.finish_step(task_id, name, out)
                ran.append(name)
            final = self._final_status(task_id)
            if final is TaskStatus.CANCELLED:
                return self._finish_cancelled(task_id, ran)
            t = self.tasks.release(task_id, final)
            return AdvanceResult(task=t, ran_steps=ran, message=f"task {final.value}")
        except InjectedFault:
            raise  # simulated crash: leave the lease and step RUNNING, exactly like a dead process
        except TRANSIENT_ERRORS as exc:
            return self._transient(task_id, t, ran, exc.message)
        except YemenDevError as exc:
            self._fail(task_id, t, exc.message)
            return AdvanceResult(task=self.tasks.get(task_id), ran_steps=ran, message=exc.message)
        except Exception as exc:  # noqa: BLE001 - record and surface any unexpected failure as FAILED
            self._fail(task_id, t, f"internal error: {exc.__class__.__name__}")
            return AdvanceResult(task=self.tasks.get(task_id), ran_steps=ran, message="internal error")

    def cancel(self, actor: Actor, task_id: str) -> Task:
        """Cancel now (pending / waiting / queued) or request cancellation of a running task."""
        cur = self.tasks.get(task_id)
        self.p.guard.require(actor, Action.CANCEL_TASK, task_id, project_id=cur.project_id)
        if cur.status is TaskStatus.RUNNING:
            t = self.tasks.request_cancel(task_id, actor.ref)
            self.p.audit.record(actor=actor.ref, action=Action.CANCEL_TASK.value, target=task_id,
                                outcome="requested", details={"step": t.current_step})
            return t
        t = self.tasks.cancel(task_id)
        self.queue.cancel(task_id, actor.ref, "task cancelled")
        self._revoke_approvals(task_id)
        self.p.audit.record(actor=actor.ref, action=Action.CANCEL_TASK.value, target=task_id, outcome="ok")
        return t

    def enqueue(self, actor: Actor, task_id: str, opts: QueueOptions) -> QueueEntry:
        """Queue a task for background execution with the caller's rights (see orion/queue.py)."""
        t = self.tasks.get(task_id)
        self.p.guard.require(actor, Action.ADVANCE_TASK, task_id, project_id=t.project_id)
        for d in opts.depends_on:
            try:
                dep = self.tasks.get(d)
            except NotFoundError:
                continue  # reported by the queue as "does not exist"
            self.p.guard.require(actor, Action.READ, d, project_id=dep.project_id)
        e = self.queue.enqueue(task_id, actor, opts)
        self.p.audit.record(actor=actor.ref, action="task.enqueue", target=task_id, outcome="ok",
                            details={"priority": e.priority, "run_after": e.run_after, "depends_on": e.depends_on,
                                     "max_attempts": e.max_attempts, "timeout_seconds": e.timeout_seconds})
        return e

    def trace(self, actor: Actor, task_id: str) -> dict[str, Any]:
        """Execution trace: plan with per-step status and duration, queue state, timeline, agent runs."""
        t = self.get_task(actor, task_id)
        steps = {s.name: s for s in self.tasks.steps(task_id)}
        plan = []
        for name, after, by in PLAN_STEPS:
            s = steps.get(name)
            dur = None
            if s and s.started_at and s.finished_at:
                dur = round((parse_iso(s.finished_at) - parse_iso(s.started_at)).total_seconds(), 3)
            plan.append({"step": name, "after": after, "performed_by": by,
                         "actions": [x.value for x in STEP_AGENTS[name][1]] if name in STEP_AGENTS else [],
                         "status": s.status.value if s else "not_started", "attempt": s.attempt if s else 0,
                         "started_at": s.started_at if s else None, "finished_at": s.finished_at if s else None,
                         "duration_seconds": dur, "error": s.error if s else None})
        q = self.queue.get(task_id)
        return {"task": t.model_dump(), "queue": q.model_dump() if q else None, "plan": plan,
                "events": [e.model_dump() for e in self.tasks.events(task_id)],
                "agent_runs": [r.model_dump() for r in self.p.agents.runs(task_id=task_id)]}

    def terminate(self, task_id: str, status: TaskStatus, reason: str, detail: dict[str, Any] | None = None) -> Task:
        """End a task that cannot finish normally (killed on timeout / cancel, retries exhausted).

        Never leaves a half-applied change: if the task created a checkpoint that was not rolled back yet,
        the project is restored from it first. Used by queue workers, never by API callers directly."""
        if status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise ValueError("terminate only ends tasks as failed or cancelled")
        t = self.tasks.get(task_id)
        if t.status in TERMINAL:
            return t
        if t.current_step:
            rec = self.tasks.step(task_id, t.current_step)
            if rec is not None and rec.status is StepStatus.RUNNING:
                self.tasks.fail_step(task_id, t.current_step, reason)
        restored = self._restore_if_applied(t)
        self.p.evidence.record(
            action="task.failed" if status is TaskStatus.FAILED else "task.cancelled", actor=SYSTEM.ref,
            target=t.project_id, outcome=status.value, task_id=task_id,
            body=EvidenceBody(details={"reason": reason, "step": t.current_step, "restored_files": restored,
                                       **(detail or {})}))
        if status is TaskStatus.CANCELLED:
            self._revoke_approvals(task_id)
        return self.tasks.release(task_id, status, reason)

    # ---- internals -------------------------------------------------------------------
    def _handlers(self) -> dict[str, Callable[[Actor, Task], Any]]:
        impl: dict[str, Callable[[Actor, Task], Any]] = {
            "scan": self._scan, "baseline": self._baseline, "diagnose": self._diagnose, "propose": self._propose,
            "simulate": self._simulate, "authorize": self._authorize, "execute": self._execute,
            "verify": self._verify, "rollback": self._rollback, "learn": self._learn}

        def via_agent(step: str, fn: Callable[[Actor, Task], Any]) -> Callable[[Actor, Task], Any]:
            if step not in STEP_AGENTS:  # the human approval gate is ORION's own, never an agent's
                return fn
            agent, actions = STEP_AGENTS[step]

            def run(actor: Actor, t: Task) -> Any:
                return self.p.agents.run(agent, step, actions, lambda: fn(actor, t), task_id=t.id,
                                         project_id=t.project_id, inputs={"task": t.id, "step": step},
                                         passthrough=(Stop,))
            return run

        return {k: via_agent(k, v) for k, v in impl.items()}

    def _next_step(self, task_id: str) -> str | None:
        def done(n: str) -> bool:
            rec = self.tasks.step(task_id, n)
            return bool(rec and rec.status is StepStatus.DONE)

        if self.tasks.get(task_id).cancel_requested_at:
            # Cancelled while running: undo an applied change, then stop.
            return "rollback" if done("execute") and not done("rollback") else None
        for n in ("scan", "baseline", "diagnose", "propose", "simulate", "authorize", "execute", "verify"):
            if not done(n):
                return n
        verify = self.tasks.step(task_id, "verify")
        passed = bool(verify and verify.output and verify.output.get("passed"))
        if not passed and not done("rollback"):
            return "rollback"
        return None if done("learn") else "learn"

    def _final_status(self, task_id: str) -> TaskStatus:
        if self.tasks.get(task_id).cancel_requested_at:
            return TaskStatus.CANCELLED
        rb = self.tasks.step(task_id, "rollback")
        return TaskStatus.ROLLED_BACK if rb and rb.status is StepStatus.DONE else TaskStatus.SUCCEEDED

    def _finish_cancelled(self, task_id: str, ran: list[str]) -> AdvanceResult:
        cur = self.tasks.get(task_id)
        rb = self.tasks.step(task_id, "rollback")
        rolled_back = bool(rb and rb.status is StepStatus.DONE)
        self.p.evidence.record(action="task.cancelled", actor=cur.cancel_requested_by or SYSTEM.ref,
                               target=cur.project_id, outcome="cancelled", task_id=task_id,
                               body=EvidenceBody(details={"requested_at": cur.cancel_requested_at,
                                                          "applied_change_rolled_back": rolled_back}))
        self._revoke_approvals(task_id)
        t = self.tasks.release(task_id, TaskStatus.CANCELLED)
        msg = "task cancelled" + ("; the applied change was rolled back" if rolled_back else "")
        return AdvanceResult(task=t, ran_steps=ran, message=msg)

    def _transient(self, task_id: str, t: Task, ran: list[str], msg: str) -> AdvanceResult:
        cur = self.tasks.get(task_id)
        if cur.current_step:
            self.tasks.fail_step(task_id, cur.current_step, msg)
        self.p.evidence.record(action="task.transient_failure", actor=SYSTEM.ref, target=t.project_id,
                               outcome="retryable", task_id=task_id,
                               body=EvidenceBody(details={"error": msg, "step": cur.current_step}))
        released = self.tasks.release(task_id, TaskStatus.PENDING, f"transient: {msg}")
        return AdvanceResult(task=released, ran_steps=ran, retryable=True,
                             message=f"transient failure (the task can be resumed): {msg}")

    def _restore_if_applied(self, t: Task) -> list[str] | None:
        cid = f"ckpt_{t.id}"
        rb = self.tasks.step(t.id, "rollback")
        if not self.p.recovery.checkpoints.exists(cid) or (rb and rb.status is StepStatus.DONE):
            return None
        self.p.guard.require(SYSTEM, Action.ROLLBACK_OWN_CHANGE, t.project_id, project_id=t.project_id)
        restored = self.p.recovery.rollback(cid)
        self.p.evidence.record(action="repair.rollback", actor=SYSTEM.ref, target=t.project_id, outcome="restored",
                               task_id=t.id, body=EvidenceBody(rollback={"checkpoint_id": cid, "restored": restored,
                                                                         "verified_hashes": True,
                                                                         "reason": "task ended before completing"}))
        self.p.audit.record(actor=SYSTEM.ref, action=Action.ROLLBACK_OWN_CHANGE.value, target=t.project_id,
                            outcome="ok", details={"task_id": t.id, "files": restored, "reason": "terminated"})
        return restored

    def _revoke_approvals(self, task_id: str) -> None:
        # Regression (audit F5): approvals of a cancelled task must not stay usable.
        for a in self.p.approvals.for_task(task_id):
            self.p.approvals.revoke(a.id, reason="task cancelled")  # no-op unless pending/approved

    def _fail(self, task_id: str, t: Task, msg: str) -> None:
        cur = self.tasks.get(task_id)
        if cur.current_step:
            self.tasks.fail_step(task_id, cur.current_step, msg)
        self.p.evidence.record(action="task.failed", actor="system:orion", target=t.project_id, outcome="failed",
                               task_id=task_id, body=EvidenceBody(details={"error": msg,
                                                                           "step": cur.current_step}))
        self.tasks.release(task_id, TaskStatus.FAILED, msg)

    def _out(self, task_id: str, name: str) -> Any:
        rec = self.tasks.step(task_id, name)
        if rec is None or rec.status is not StepStatus.DONE:
            raise YemenDevError(f"internal: step {name} has no output")
        return rec.output

    def _root(self, t: Task) -> Path:
        return self.p.project_root(t.project_id)

    def _pyver(self, t: Task) -> tuple[int, int]:
        v = t.params.get("python_version", list(sys.version_info[:2]))
        return int(v[0]), int(v[1])

    def _plan(self, t: Task) -> RepairPlan:
        sim = self._out(t.id, "simulate")
        plans = {p["id"]: p for p in self._out(t.id, "propose")["plans"]}
        return RepairPlan(**plans[sim["selected_plan_id"]])

    def _baseline_report(self, t: Task) -> TestRunReport:
        return TestRunReport(**self._out(t.id, "baseline")["runs"][-1])

    # ---- steps -----------------------------------------------------------------------
    def _scan(self, actor: Actor, t: Task) -> Any:
        sid, snap = self.p.scan_project(actor, t.project_id, task_id=t.id)
        return {"scan_id": sid, "tree_hash": snap.tree_hash}

    def _baseline(self, actor: Actor, t: Task) -> Any:
        self.p.guard.require(actor, Action.RUN_SANDBOXED, t.project_id, project_id=t.project_id)
        runs = []
        sandbox, image_note = self.p.sandbox_for(t.project_id)
        for _ in range(BASELINE_RUNS):
            with Workspace(self._root(t), sandbox, base_dir=self.p.settings.workspaces_dir) as ws:
                runs.append(run_tests(ws))
        r = runs[-1]
        self.p.evidence.record(
            action="tests.baseline", actor=actor.ref, target=t.project_id, outcome=r.status.value, task_id=t.id,
            body=EvidenceBody(tests={"runs": [{"status": x.status.value, "passed": x.passed, "failed": x.failed,
                                               "errors": x.errors, "failing": x.failing_ids(),
                                               "duration_seconds": x.execution.duration_seconds} for x in runs]},
                              environment={"network_isolated": r.execution.network_isolated,
                                           "limits": r.execution.limits, "backend": r.execution.backend,
                                           "image": r.execution.image, "run_as_uid": r.execution.run_as_uid,
                                           "backend_note": r.execution.backend_note},
                              limitations=["test results are only as strong as the project's own test suite"]
                              + ([r.execution.backend_note] if r.execution.backend_note else [])
                              + ([image_note] if image_note else [])))
        return {"runs": [x.model_dump(mode="json") for x in runs]}

    def _diagnose(self, actor: Actor, t: Task) -> Any:
        runs = [TestRunReport(**r) for r in self._out(t.id, "baseline")["runs"]]
        d = diagnose(runs)
        self.p.evidence.record(action="diagnostics.report", actor=AGENTS["diagnostic"].actor.ref, target=t.project_id,
                               outcome="problem" if d.reproducible_problem else "no_problem", task_id=t.id,
                               body=EvidenceBody(details=d.model_dump(mode="json"), limitations=d.limitations))
        for h in d.hypotheses:
            self.p.memory.write(MemoryWrite(project_id=t.project_id, category=MemoryCategory.OBSERVATION,
                                            subject=f"hypothesis:{h.category}", key=",".join(h.supporting_tests)[:200],
                                            value=h.model_dump(), verified=False, confidence=h.confidence,
                                            source=AGENTS["diagnostic"].actor.ref))
        if d.failures:
            _, snap = self.p.latest_scan(t.project_id)
            candidates = root_cause_candidates(snap, d)
            self.p.evidence.record(
                action="diagnostics.root_causes", actor=AGENTS["diagnostic"].actor.ref, target=t.project_id,
                outcome=f"{len(candidates)} candidate(s)", task_id=t.id,
                body=EvidenceBody(details={"candidates": [c.model_dump() for c in candidates]},
                                  limitations=["static imports + traceback locations; candidates, not proven causes"]))
        if not d.reproducible_problem:
            raise Stop(TaskStatus.NO_ACTION, d.summary)
        return d.model_dump(mode="json")

    def _propose(self, actor: Actor, t: Task) -> Any:
        diag = DiagnosticReport(**self._out(t.id, "diagnose"))
        root = self._root(t)
        _, snap = self.p.latest_scan(t.project_id)
        plans: list[RepairPlan] = []
        notes: list[str] = []
        if hp := t.params.get("human_plan"):
            plans.append(RepairPlan(**hp))
        plans += propose_from_skills(self.p.skills, diag, snap, root, self._pyver(t))
        adapter = self.p.models.first_available()
        if adapter is None:
            notes.append("no model available: model-based proposals skipped")
        else:
            try:
                plans.append(propose_with_model(adapter, diag, root))
            except (YemenDevError, ValueError) as exc:
                notes.append(f"model proposal failed: {getattr(exc, 'message', str(exc))[:300]}")
        self.p.evidence.record(action="repair.propose", actor=AGENTS["planning"].actor.ref, target=t.project_id,
                               outcome=f"{len(plans)} plan(s)", task_id=t.id,
                               body=EvidenceBody(details={"plans": [{"id": p.id, "source": p.source.value,
                                                                     "source_ref": p.source_ref,
                                                                     "files": [x.path for x in p.patches]}
                                                                    for p in plans], "notes": notes}))
        if not plans:
            raise Stop(TaskStatus.NO_ACTION, "no repair plan could be proposed; " + "; ".join(notes))
        reviews: dict[str, PlanReview] = {}
        kept: list[RepairPlan] = []
        for plan in plans:
            originals = self._originals(root, plan)
            review = self.p.agents.run("security", "review_plan", (Action.READ,),
                                       functools.partial(review_plan, plan, originals),
                                       task_id=t.id, project_id=t.project_id, inputs={"plan": plan.id})
            reviews[plan.id] = review
            self.p.evidence.record(action="security.plan_review", actor=AGENTS["security"].actor.ref,
                                   target=t.project_id, outcome=review.verdict.value, task_id=t.id,
                                   body=EvidenceBody(inputs={"plan_id": plan.id},
                                                     details=review.model_dump(mode="json"),
                                                     limitations=[review.note]))
            if review.verdict is Verdict.BLOCK:
                # Conflict policy SEC-BLOCK-WINS: planning proposed it, security blocks it -> security prevails.
                self.p.evidence.record(
                    action="agent.conflict", actor="system:orion", target=t.project_id, outcome="security_prevails",
                    task_id=t.id, body=EvidenceBody(details={
                        "plan_id": plan.id, "planning": "proposed", "security": "block",
                        "policy": "SEC-BLOCK-WINS: a security block always overrides a proposal",
                        "findings": [f.model_dump() for f in review.findings]}))
                continue
            kept.append(plan)
        if not kept:
            raise Stop(TaskStatus.NO_ACTION, "every proposed plan was blocked by the security review")
        return {"plans": [p.model_dump(mode="json") for p in kept], "notes": notes,
                "reviews": {k: v.model_dump(mode="json") for k, v in reviews.items()}}

    @staticmethod
    def _originals(root: Path, plan: RepairPlan) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in plan.patches:
            f = root / p.path
            if f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(root.resolve()):
                out[p.path] = f.read_text("utf-8", "replace")
        return out

    def _simulate(self, actor: Actor, t: Task) -> Any:
        self.p.guard.require(actor, Action.RUN_SANDBOXED, t.project_id, project_id=t.project_id)
        baseline = self._baseline_report(t)
        root = self._root(t)
        results = []
        selected = None
        for pd in self._out(t.id, "propose")["plans"]:
            plan = RepairPlan(**pd)
            try:
                res = self.p.recovery.simulate(root, plan, baseline, sandbox=self.p.sandbox_for(t.project_id)[0])
                entry = {"plan_id": plan.id, "passed": res.passed, "problems": res.problems,
                         "comparison": res.comparison.model_dump() if res.comparison else None}
            except YemenDevError as exc:
                entry = {"plan_id": plan.id, "passed": False, "problems": [exc.message], "comparison": None}
            results.append(entry)
            self.p.evidence.record(action="repair.simulate", actor=AGENTS["testing"].actor.ref, target=t.project_id,
                                   outcome="passed" if entry["passed"] else "failed", task_id=t.id,
                                   body=EvidenceBody(inputs={"plan_id": plan.id}, tests=entry,
                                                     limitations=["simulated in an isolated copy of the project"]))
            if entry["passed"]:
                selected = plan.id
                break
        if selected is None:
            self._remember_failure(t, "no proposed plan passed simulation", results)
            raise Stop(TaskStatus.NO_ACTION, "no proposed plan passed simulation")
        return {"selected_plan_id": selected, "results": results}

    def _authorize(self, actor: Actor, t: Task) -> Any:
        plan = self._plan(t)
        dg = self._digest(t, plan)
        eff = self.p.guard.effective(actor, t.project_id)
        if eff is None:
            raise Stop(TaskStatus.FAILED, "actor has no access to this project")
        decision = self.p.policy.evaluate(eff, Action.APPLY_PATCH)
        if not decision.allowed:
            raise Stop(TaskStatus.FAILED, f"actor may not apply patches: {decision.reason}")
        existing = [a for a in self.p.approvals.for_task(t.id)
                    if a.action is Action.APPLY_PATCH and a.operation_digest == dg]
        req = existing[0] if existing else None
        if req is None:
            prep = self.p.recovery.prepare(self._root(t), plan)
            req = self.p.approvals.request(
                action=Action.APPLY_PATCH, target=t.project_id, operation_digest=dg, task_id=t.id,
                requested_by=self._requester(t, plan),
                summary=self._approval_summary(t, plan, prep.diff))
            self.p.audit.record(actor="system:orion", action="approval.request", target=req.id, outcome="pending",
                                details={"task_id": t.id, "digest": dg})
        if req.status is ApprovalStatus.PENDING:
            raise Stop(TaskStatus.WAITING_APPROVAL, f"waiting for approval {req.id}")
        if req.status in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED):
            self.p.audit.record(actor="system:orion", action="approval.outcome", target=req.id,
                                outcome=req.status.value, details={"task_id": t.id})
            raise Stop(TaskStatus.CANCELLED, f"approval {req.status.value}")
        if req.status is ApprovalStatus.APPROVED:
            req = self.p.approvals.consume(req.id, action=Action.APPLY_PATCH, target=t.project_id,
                                           operation_digest=dg)
        # CONSUMED here means: consumed by this task (possibly before a crash) - bound to task + digest.
        self.p.evidence.record(action="repair.authorize", actor=actor.ref, target=t.project_id, outcome="approved",
                               task_id=t.id, body=EvidenceBody(permission={
                                   "approval_id": req.id, "decided_by": req.decided_by, "digest": dg,
                                   "policy": decision.model_dump(mode="json")}))
        return {"approval_id": req.id, "digest": dg}

    def _approval_summary(self, t: Task, plan: RepairPlan, diff: str) -> str:
        review = (self._out(t.id, "propose").get("reviews") or {}).get(plan.id)
        sec = "Security review: not available"
        if review:
            sec = f"Security review: {review['verdict'].upper()}"
            sec += "".join(f"\n  - [{f['severity']}] {f['path']}:{f['line'] or '-'} {f['detail']}"
                           for f in review["findings"])
        return f"{plan.description}\nFiles: {', '.join(p.path for p in plan.patches)}\n{sec}\n\n{diff[:1500]}"

    @staticmethod
    def _digest(t: Task, plan: RepairPlan) -> str:
        """Approval digest binds task, project and the exact file changes."""
        return digest({"task": t.id, "project": t.project_id, "plan": plan.operation_digest()})

    @staticmethod
    def _requester(t: Task, plan: RepairPlan) -> Actor:
        """A human-authored plan is requested by its author, so a *different* human must approve it."""
        if plan.source.value == "human":
            kind, _, ident = t.created_by.partition(":")
            return Actor(id=ident, kind=ActorKind(kind), role=Role.OPERATOR)
        return Actor(id="orion", kind=ActorKind.SYSTEM, role=Role.OPERATOR)

    def _execute(self, actor: Actor, t: Task) -> Any:
        plan = self._plan(t)
        auth = self._out(t.id, "authorize")
        if auth["digest"] != self._digest(t, plan):
            raise Stop(TaskStatus.FAILED, "approved digest does not match plan")
        root = self._root(t)
        cid = f"ckpt_{t.id}"
        state = self.p.recovery.applied_state(root, plan)
        resumed = self.p.recovery.checkpoints.exists(cid)
        if state == "diverged":
            raise Stop(TaskStatus.FAILED, "project files diverged from both original and approved content; "
                                          "refusing to touch them (manual review required)")
        if state == "partial":
            self.p.recovery.rollback(cid)  # back to a known state, then re-apply
            state = "original"
        if state == "original":
            written = self.p.recovery.execute(root, plan, cid)
        else:
            written = [p.path for p in plan.patches]
        self.p.evidence.record(action="repair.execute", actor=actor.ref, target=t.project_id, outcome="applied",
                               task_id=t.id, body=EvidenceBody(
                                   inputs={"plan_id": plan.id, "approval_id": auth["approval_id"]},
                                   before_state={p.path: p.original_sha256 for p in plan.patches},
                                   after_state={p.path: p.new_sha256 for p in plan.patches},
                                   rollback={"checkpoint_id": cid},
                                   details={"resumed": resumed, "written": written}))
        self.p.audit.record(actor=actor.ref, action=Action.APPLY_PATCH.value, target=t.project_id, outcome="ok",
                            details={"task_id": t.id, "approval_id": auth["approval_id"], "files": written})
        return {"checkpoint_id": cid, "written": written}

    def _verify(self, actor: Actor, t: Task) -> Any:
        plan = self._plan(t)
        res: PhaseResult = self.p.recovery.verify(self._root(t), plan, self._baseline_report(t),
                                                  sandbox=self.p.sandbox_for(t.project_id)[0])
        out = {"passed": res.passed, "problems": res.problems,
               "comparison": res.comparison.model_dump() if res.comparison else None}
        self.p.evidence.record(action="repair.verify", actor=AGENTS["verification"].actor.ref, target=t.project_id,
                               outcome="passed" if res.passed else "failed", task_id=t.id,
                               body=EvidenceBody(verification=out, limitations=[
                                   "verification = project's own test suite in a fresh sandbox copy"]))
        return out

    def _rollback(self, actor: Actor, t: Task) -> Any:
        system = SYSTEM
        self.p.guard.require(system, Action.ROLLBACK_OWN_CHANGE, t.project_id, project_id=t.project_id)
        cid = self._out(t.id, "execute")["checkpoint_id"]
        restored = self.p.recovery.rollback(cid)
        self.p.evidence.record(action="repair.rollback", actor=system.ref, target=t.project_id, outcome="restored",
                               task_id=t.id, body=EvidenceBody(rollback={"checkpoint_id": cid, "restored": restored,
                                                                         "verified_hashes": True}))
        self.p.audit.record(actor=system.ref, action=Action.ROLLBACK_OWN_CHANGE.value, target=t.project_id,
                            outcome="ok", details={"task_id": t.id, "files": restored})
        return {"restored": restored}

    def _learn(self, actor: Actor, t: Task) -> Any:
        plan = self._plan(t)
        verified = bool(self._out(t.id, "verify")["passed"])
        ver_ev = [e for e in self.p.evidence.list(task_id=t.id) if e.action == "repair.verify"]
        ev_id = ver_ev[-1].id if ver_ev else "none"
        diag = DiagnosticReport(**self._out(t.id, "diagnose"))
        hyp = next((h for h in diag.hypotheses if h.id == plan.hypothesis_id), diag.hypotheses[0]
                   if diag.hypotheses else None)
        out: dict[str, Any] = {"verified": verified}
        if plan.source.value == "skill":
            # Regression (audit F2): a bad skill reference must not fail a task whose change is already
            # verified; it is recorded instead.
            sid, _, ver = plan.source_ref.partition("@")
            try:
                done = any(r.get("evidence_id") == ev_id for v in self.p.skills.validations(sid, int(ver))
                           for r in v.results)
                if not done:
                    self.p.skills.record_field_outcome(sid, int(ver), verified, ev_id)
                out["skill_field_outcome"] = verified
            except (NotFoundError, ValueError):
                out["skill_field_outcome"] = "skill reference not found"
        if not verified:
            self._remember_failure(t, "repair failed verification and was rolled back", {"plan_id": plan.id})
            return out
        self.p.memory.write(MemoryWrite(
            project_id=t.project_id, category=MemoryCategory.EXPERIENCE, subject=f"repair:{plan.id}",
            key="outcome", value={"plan": plan.description, "source": plan.source_ref,
                                  "files": [x.path for x in plan.patches],
                                  "hypothesis": hyp.model_dump() if hyp else None},
            verified=True, confidence=1.0, source=AGENTS["learning"].actor.ref, evidence_ids=[ev_id]))
        if hyp and plan.source.value != "skill":
            # originals come from the verified checkpoint taken before the change
            m = self.p.recovery.checkpoints.load(f"ckpt_{t.id}")
            base = self.p.settings.checkpoints_dir / m.id / "blobs"
            orig = {e.path: (base / str(e.blob)).read_text("utf-8") for e in m.entries if e.existed}
            patched = {p.path: p.new_content for p in plan.patches if p.path in orig}
            spec = candidate_skill(orig, patched, hyp.category, hyp.signals, origin=f"evidence:{ev_id}")
            if spec:
                existing = [s for s in self.p.skills.list() if s.content_hash == spec.content_hash()]
                if existing:
                    out["candidate_skill"] = existing[0].id
                else:
                    s = self.p.skills.propose(spec, LEARNING_AGENT.ref)
                    out["candidate_skill"] = s.id
                    self.p.audit.record(actor=LEARNING_AGENT.ref, action=Action.PROPOSE_SKILL.value, target=s.id,
                                        outcome=SkillStatus.PROPOSED.value, details={"from_task": t.id})
        return out

    def _remember_failure(self, t: Task, what: str, details: Any) -> None:
        self.p.memory.write(MemoryWrite(
            project_id=t.project_id, category=MemoryCategory.FAILED_EXPERIMENT, subject=f"task:{t.id}", key="failure",
            value={"what": what, "details": details}, verified=False, confidence=1.0, source="orion"))
