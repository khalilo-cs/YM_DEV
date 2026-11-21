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
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.core.contracts import NotFoundError, YemenDevError, digest, new_id
from yemendev.core.faults import InjectedFault
from yemendev.diagnostics.engine import DiagnosticReport, diagnose
from yemendev.evidence.store import EvidenceBody
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.approvals import ApprovalStatus
from yemendev.governance.policy import Action
from yemendev.learning.experience import candidate_skill
from yemendev.learning.skills import SkillStatus
from yemendev.memory.model import MemoryCategory, MemoryWrite
from yemendev.orion.tasks import TERMINAL, StepStatus, Task, TaskStatus, TaskStore
from yemendev.platform import Platform
from yemendev.recovery.engine import PhaseResult
from yemendev.recovery.model import PlanSource, RepairPlan
from yemendev.recovery.model_proposer import propose_with_model
from yemendev.recovery.proposers import propose_from_skills
from yemendev.sandbox.testrunner import TestRunReport, run_pytest
from yemendev.sandbox.workspace import Workspace

BASELINE_RUNS = 2
LEARNING_AGENT = Actor(id="learning-agent", kind=ActorKind.AGENT, role=Role.OPERATOR)


class Stop(Exception):  # noqa: N818 - control flow signal, not an error
    def __init__(self, status: TaskStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class AdvanceResult(BaseModel):
    task: Task
    ran_steps: list[str]
    message: str


class RepairWorkflow:
    KIND = "repair"

    def __init__(self, platform: Platform) -> None:
        self.p = platform
        self.tasks = TaskStore(platform.db)

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

    def advance(self, actor: Actor, task_id: str, worker: str | None = None) -> AdvanceResult:
        """Run steps until the task finishes or needs a human. Safe to call repeatedly."""
        t = self.tasks.get(task_id)
        self.p.guard.require(actor, Action.ADVANCE_TASK, task_id, project_id=t.project_id)
        if t.status in TERMINAL:
            return AdvanceResult(task=t, ran_steps=[], message=f"task already {t.status.value}")
        t = self.tasks.acquire(task_id, worker or new_id("wrk"))
        ran: list[str] = []
        try:
            while (name := self._next_step(task_id)) is not None:
                self.tasks.start_step(task_id, name)
                try:
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
            t = self.tasks.release(task_id, final)
            return AdvanceResult(task=t, ran_steps=ran, message=f"task {final.value}")
        except InjectedFault:
            raise  # simulated crash: leave the lease and step RUNNING, exactly like a dead process
        except YemenDevError as exc:
            self._fail(task_id, t, exc.message)
            return AdvanceResult(task=self.tasks.get(task_id), ran_steps=ran, message=exc.message)
        except Exception as exc:  # noqa: BLE001 - record and surface any unexpected failure as FAILED
            self._fail(task_id, t, f"internal error: {exc.__class__.__name__}")
            return AdvanceResult(task=self.tasks.get(task_id), ran_steps=ran, message="internal error")

    def cancel(self, actor: Actor, task_id: str) -> Task:
        self.p.guard.require(actor, Action.CANCEL_TASK, task_id, project_id=self.tasks.get(task_id).project_id)
        t = self.tasks.cancel(task_id)
        # Regression (audit F5): approvals of a cancelled task must not stay usable.
        for a in self.p.approvals.for_task(task_id):
            self.p.approvals.revoke(a.id, reason="task cancelled")
        self.p.audit.record(actor=actor.ref, action=Action.CANCEL_TASK.value, target=task_id, outcome="ok")
        return t

    # ---- internals -------------------------------------------------------------------
    def _handlers(self) -> dict[str, Callable[[Actor, Task], Any]]:
        return {"scan": self._scan, "baseline": self._baseline, "diagnose": self._diagnose,
                "propose": self._propose, "simulate": self._simulate, "authorize": self._authorize,
                "execute": self._execute, "verify": self._verify, "rollback": self._rollback, "learn": self._learn}

    def _next_step(self, task_id: str) -> str | None:
        def done(n: str) -> bool:
            rec = self.tasks.step(task_id, n)
            return bool(rec and rec.status is StepStatus.DONE)

        for n in ("scan", "baseline", "diagnose", "propose", "simulate", "authorize", "execute", "verify"):
            if not done(n):
                return n
        verify = self.tasks.step(task_id, "verify")
        passed = bool(verify and verify.output and verify.output.get("passed"))
        if not passed and not done("rollback"):
            return "rollback"
        return None if done("learn") else "learn"

    def _final_status(self, task_id: str) -> TaskStatus:
        rb = self.tasks.step(task_id, "rollback")
        return TaskStatus.ROLLED_BACK if rb and rb.status is StepStatus.DONE else TaskStatus.SUCCEEDED

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
        for _ in range(BASELINE_RUNS):
            with Workspace(self._root(t), self.p.settings.sandbox, base_dir=self.p.settings.workspaces_dir) as ws:
                runs.append(run_pytest(ws))
        r = runs[-1]
        self.p.evidence.record(
            action="tests.baseline", actor=actor.ref, target=t.project_id, outcome=r.status.value, task_id=t.id,
            body=EvidenceBody(tests={"runs": [{"status": x.status.value, "passed": x.passed, "failed": x.failed,
                                               "errors": x.errors, "failing": x.failing_ids()} for x in runs]},
                              environment={"network_isolated": r.execution.network_isolated,
                                           "limits": r.execution.limits, "backend": r.execution.backend,
                                           "image": r.execution.image, "run_as_uid": r.execution.run_as_uid,
                                           "backend_note": r.execution.backend_note},
                              limitations=["test results are only as strong as the project's own test suite"]
                              + ([r.execution.backend_note] if r.execution.backend_note else [])))
        return {"runs": [x.model_dump(mode="json") for x in runs]}

    def _diagnose(self, actor: Actor, t: Task) -> Any:
        runs = [TestRunReport(**r) for r in self._out(t.id, "baseline")["runs"]]
        d = diagnose(runs)
        self.p.evidence.record(action="diagnostics.report", actor="agent:diagnostic", target=t.project_id,
                               outcome="problem" if d.reproducible_problem else "no_problem", task_id=t.id,
                               body=EvidenceBody(details=d.model_dump(mode="json"), limitations=d.limitations))
        for h in d.hypotheses:
            self.p.memory.write(MemoryWrite(project_id=t.project_id, category=MemoryCategory.OBSERVATION,
                                            subject=f"hypothesis:{h.category}", key=",".join(h.supporting_tests)[:200],
                                            value=h.model_dump(), verified=False, confidence=h.confidence,
                                            source="agent:diagnostic"))
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
        self.p.evidence.record(action="repair.propose", actor="agent:planning", target=t.project_id,
                               outcome=f"{len(plans)} plan(s)", task_id=t.id,
                               body=EvidenceBody(details={"plans": [{"id": p.id, "source": p.source.value,
                                                                     "source_ref": p.source_ref,
                                                                     "files": [x.path for x in p.patches]}
                                                                    for p in plans], "notes": notes}))
        if not plans:
            raise Stop(TaskStatus.NO_ACTION, "no repair plan could be proposed; " + "; ".join(notes))
        return {"plans": [p.model_dump(mode="json") for p in plans], "notes": notes}

    def _simulate(self, actor: Actor, t: Task) -> Any:
        self.p.guard.require(actor, Action.RUN_SANDBOXED, t.project_id, project_id=t.project_id)
        baseline = self._baseline_report(t)
        root = self._root(t)
        results = []
        selected = None
        for pd in self._out(t.id, "propose")["plans"]:
            plan = RepairPlan(**pd)
            try:
                res = self.p.recovery.simulate(root, plan, baseline)
                entry = {"plan_id": plan.id, "passed": res.passed, "problems": res.problems,
                         "comparison": res.comparison.model_dump() if res.comparison else None}
            except YemenDevError as exc:
                entry = {"plan_id": plan.id, "passed": False, "problems": [exc.message], "comparison": None}
            results.append(entry)
            self.p.evidence.record(action="repair.simulate", actor="agent:testing", target=t.project_id,
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
                summary=f"{plan.description}\nFiles: {', '.join(p.path for p in plan.patches)}\n\n{prep.diff[:1500]}")
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
        res: PhaseResult = self.p.recovery.verify(self._root(t), plan, self._baseline_report(t))
        out = {"passed": res.passed, "problems": res.problems,
               "comparison": res.comparison.model_dump() if res.comparison else None}
        self.p.evidence.record(action="repair.verify", actor="agent:verification", target=t.project_id,
                               outcome="passed" if res.passed else "failed", task_id=t.id,
                               body=EvidenceBody(verification=out, limitations=[
                                   "verification = project's own test suite in a fresh sandbox copy"]))
        return out

    def _rollback(self, actor: Actor, t: Task) -> Any:
        system = Actor(id="orion", kind=ActorKind.SYSTEM, role=Role.OPERATOR)
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
            verified=True, confidence=1.0, source="agent:learning", evidence_ids=[ev_id]))
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
