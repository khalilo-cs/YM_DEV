"""Composition root: builds every engine from Settings. The only module that wires engines together."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.agents.runner import AgentRunner
from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    canonical_json,
    iso,
    new_id,
)
from yemendev.core.faults import NO_FAULTS, FaultInjector
from yemendev.evidence.store import EvidenceBody, EvidenceRecord, EvidenceStore
from yemendev.governance.actors import ROLE_RANK, Actor, Role
from yemendev.governance.approvals import ApprovalRequest, ApprovalService, ApprovalStatus
from yemendev.governance.guard import Guard
from yemendev.governance.membership import Member, MembershipStore
from yemendev.governance.policy import Action, PolicyEngine
from yemendev.learning.skills import Skill, SkillEngine
from yemendev.memory.engine import MemoryEngine
from yemendev.mosaic.adapters import ModelRegistry, OpenAICompatibleAdapter
from yemendev.recovery.checkpoint import CheckpointStore
from yemendev.recovery.engine import RecoveryEngine
from yemendev.security.audit import AuditLog
from yemendev.security.keys import load_or_create_key
from yemendev.security.tokens import TokenStore
from yemendev.storage.db import Database
from yemendev.twin.memory_sync import record_scan
from yemendev.twin.model import ProjectSnapshot
from yemendev.twin.scanner import ProjectScanner


class Project(BaseModel):
    id: str
    name: str
    root_path: str
    created_at: str
    created_by: str


class Platform:
    def __init__(self, settings: Settings, faults: FaultInjector = NO_FAULTS) -> None:
        settings.ensure_dirs()
        self.settings = settings
        self.faults = faults
        self.db = Database(settings.db_path)
        self.db.migrate()
        for f in settings.data_dir.glob("yemendev.sqlite3*"):
            os.chmod(f, 0o600)  # audit F3: platform DB must not be world-readable
        self.audit = AuditLog(self.db, load_or_create_key(settings.keys_dir / "audit.key"))
        self.policy = PolicyEngine()
        self.members = MembershipStore(self.db)
        self.guard = Guard(self.policy, self.audit, self.members)
        self.approvals = ApprovalService(self.db, self.policy, settings.approval_ttl_seconds)
        self.evidence = EvidenceStore(self.db, on_record=lambda r: self.audit.record(
            actor=r.actor, action="evidence.recorded", target=r.id, outcome=r.outcome,
            details={"content_hash": r.content_hash, "task_id": r.task_id}))
        self.memory = MemoryEngine(self.db)
        self.agents = AgentRunner(self.db, self.policy, self.audit)
        self.skills = SkillEngine(self.db)
        self.tokens = TokenStore(self.db)
        self.scanner = ProjectScanner()
        self.models = ModelRegistry([OpenAICompatibleAdapter(settings.model)])
        self.recovery = RecoveryEngine(settings.sandbox, CheckpointStore(settings.checkpoints_dir),
                                       settings.workspaces_dir, faults)

    # ---- projects --------------------------------------------------------------------
    def register_project(self, actor: Actor, name: str, path: str) -> Project:
        self.guard.require(actor, Action.REGISTER_PROJECT, path)
        if not name.strip() or len(name) > 200:
            raise InvalidInputError("invalid project name")
        root = Path(path).expanduser()
        if not root.is_absolute():
            raise InvalidInputError("project path must be absolute")
        if root.is_symlink():
            raise InvalidInputError("project root must not be a symlink")
        root = root.resolve()
        if not root.is_dir():
            raise InvalidInputError("project path is not a directory")
        self._check_root(root)
        allowed = [r.resolve() for r in self.settings.allowed_project_roots]
        if allowed and not any(root.is_relative_to(a) for a in allowed):
            raise PermissionDeniedError("project path is outside the allowed project roots")
        p = Project(id=new_id("prj"), name=name.strip(), root_path=str(root), created_at=iso(), created_by=actor.ref)
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO projects(id, name, root_path, created_at, created_by) VALUES (?,?,?,?,?)",
                             (p.id, p.name, p.root_path, p.created_at, p.created_by))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ConflictError("a project with this path is already registered") from exc
            raise
        self.members.grant(p.id, actor.ref, Role.ADMIN, granted_by=actor.ref)  # registrant owns the project
        self.audit.record(actor=actor.ref, action=Action.REGISTER_PROJECT.value, target=p.id, outcome="ok",
                          details={"name": p.name})
        return p

    # ---- membership (per-project authorization) --------------------------------------
    def grant_member(self, actor: Actor, project_id: str, actor_ref: str, role: Role) -> Member:
        self.project(project_id)
        self.guard.require(actor, Action.MANAGE_MEMBERS, project_id, project_id=project_id)
        eff = self.guard.effective(actor, project_id)
        if eff is None or ROLE_RANK[role] > ROLE_RANK[eff.role]:
            raise PermissionDeniedError("cannot grant a role higher than your own on this project")
        m = self.members.grant(project_id, actor_ref, role, granted_by=actor.ref)
        self.audit.record(actor=actor.ref, action=Action.MANAGE_MEMBERS.value, target=project_id, outcome="granted",
                          details={"member": actor_ref, "role": role.value})
        return m

    def revoke_member(self, actor: Actor, project_id: str, actor_ref: str) -> None:
        self.guard.require(actor, Action.MANAGE_MEMBERS, project_id, project_id=project_id)
        self.members.revoke(project_id, actor_ref)
        self.audit.record(actor=actor.ref, action=Action.MANAGE_MEMBERS.value, target=project_id, outcome="revoked",
                          details={"member": actor_ref})

    def list_members(self, actor: Actor, project_id: str) -> list[Member]:
        self.require_read(actor, project_id)
        return self.members.list(project_id)

    def require_read(self, actor: Actor, project_id: str) -> Project:
        p = self.project(project_id)
        self.guard.require(actor, Action.READ, project_id, project_id=project_id)
        return p

    def visible_projects(self, actor: Actor) -> list[Project]:
        self.guard.require(actor, Action.READ, "projects")
        return [p for p in self.projects() if self.guard.can_read(actor, p.id)]

    # ---- approvals (the authorization boundary for decisions) ------------------------
    def _approval_project(self, req: ApprovalRequest) -> str | None:
        return req.target if req.action is Action.APPLY_PATCH else None

    def get_approval(self, actor: Actor, approval_id: str) -> ApprovalRequest:
        req = self.approvals.get(approval_id)
        pid = self._approval_project(req)
        self.guard.require(actor, Action.READ, approval_id, project_id=pid)
        return req

    def visible_approvals(self, actor: Actor, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        self.guard.require(actor, Action.READ, "approvals")
        out = []
        for a in self.approvals.list(status=status):
            pid = self._approval_project(a)
            if pid is None or self.guard.can_read(actor, pid):
                out.append(a)
        return out

    def decide_approval(self, actor: Actor, approval_id: str, approve: bool, reason: str = "") -> ApprovalRequest:
        """Project-scoped decision: deciding a patch approval needs operator rights on *that* project."""
        req = self.approvals.get(approval_id)
        pid = self._approval_project(req)
        self.guard.require(actor, Action.DECIDE_APPROVAL, approval_id, project_id=pid)
        eff = self.guard.effective(actor, pid) if pid else actor
        if eff is None:  # unreachable after require(); explicit so it survives python -O
            raise PermissionDeniedError("no access to this project")
        r = self.approvals.decide(approval_id, eff, approve, reason)
        self.audit.record(actor=actor.ref, action=Action.DECIDE_APPROVAL.value, target=approval_id,
                          outcome=r.status.value, details={"reason": reason, "project_id": pid})
        return r

    # ---- evidence (filtered to readable projects) ------------------------------------
    def visible_evidence(self, actor: Actor, task_id: str | None = None, limit: int = 100) -> list[EvidenceRecord]:
        self.guard.require(actor, Action.READ, "evidence")
        recs = self.evidence.list(task_id=task_id, limit=limit)
        readable: dict[str, bool] = {}
        out = []
        for r in recs:
            if r.target not in readable:
                readable[r.target] = self._is_project(r.target) and self.guard.can_read(actor, r.target)
            if readable[r.target]:
                out.append(r)
        return out

    def get_evidence(self, actor: Actor, evidence_id: str) -> EvidenceRecord:
        r = self.evidence.get(evidence_id)
        if not (self._is_project(r.target) and self.guard.can_read(actor, r.target)):
            raise PermissionDeniedError("no access to this evidence")
        return r

    def _is_project(self, project_id: str) -> bool:
        with self.db.read() as conn:
            return conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    def project(self, project_id: str) -> Project:
        with self.db.read() as conn:
            r = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if r is None:
            raise NotFoundError("project not found")
        return Project(**dict(r))

    def _check_root(self, root: Path) -> None:
        data_dir = self.settings.data_dir.resolve()
        ws = self.settings.workspaces_dir.resolve()
        if root == Path("/") or root == Path.home().resolve():
            raise InvalidInputError("refusing to use the filesystem root or home directory as a project")
        for d in (data_dir, ws):
            if root.is_relative_to(d) or d.is_relative_to(root):
                raise InvalidInputError("project path must not contain or be inside platform directories")

    def project_root(self, project_id: str) -> Path:
        """Re-validate the stored root on every use (audit F4: root swapped for a symlink after registration)."""
        stored = Path(self.project(project_id).root_path)
        if stored.is_symlink() or not stored.is_dir() or stored.resolve() != stored:
            raise PreconditionFailedError("project root changed since registration (missing, moved or replaced "
                                          "by a symlink); re-register the project after review")
        self._check_root(stored)
        return stored

    def projects(self) -> list[Project]:
        with self.db.read() as conn:
            return [Project(**dict(r)) for r in conn.execute("SELECT * FROM projects ORDER BY created_at")]

    def scan_project(self, actor: Actor, project_id: str, task_id: str | None = None) -> tuple[str, ProjectSnapshot]:
        p = self.project(project_id)
        self.guard.require(actor, Action.SCAN_PROJECT, project_id, project_id=project_id)
        root = self.project_root(project_id)
        snap = self.scanner.scan(root)
        sid = new_id("scn")
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO project_scans(id, project_id, created_at, tree_hash, snapshot_json)"
                         " VALUES (?,?,?,?,?)", (sid, project_id, iso(), snap.tree_hash, snap.model_dump_json()))
        ev = self.evidence.record(
            action="project.scan", actor=actor.ref, target=project_id, outcome="ok", task_id=task_id,
            body=EvidenceBody(
                inputs={"root": p.root_path},
                after_state={"scan_id": sid, "tree_hash": snap.tree_hash, "files": len(snap.files),
                             "modules": len(snap.modules), "edges": len(snap.edges), "findings": len(snap.findings)},
                limitations=["static analysis only; no code executed", "Python imports only"],
            ))
        record_scan(self.memory, project_id, snap, ev.id)
        self.memory.revalidate(project_id, root)
        self.audit.record(actor=actor.ref, action=Action.SCAN_PROJECT.value, target=project_id, outcome="ok",
                          details={"scan_id": sid, "evidence_id": ev.id})
        return sid, snap

    # ---- per-project sandbox images ---------------------------------------------------------
    def sandbox_for(self, project_id: str) -> tuple[SandboxSettings, str | None]:
        """Sandbox settings for this project (its dependency image when built) and a note if degraded."""
        from yemendev.sandbox.docker_backend import docker_availability
        from yemendev.sandbox.project_image import image_exists, project_image_tag, runtime_requirements

        base = self.settings.sandbox
        if base.backend == "process":
            return base, None
        try:
            _, snap = self.latest_scan(project_id)
            reqs = runtime_requirements(snap)
        except (NotFoundError, InvalidInputError):
            return base, None
        if not reqs:
            return base, None
        avail = docker_availability(base)
        if not avail.available or not avail.image_id:
            return base, None
        tag = project_image_tag(avail.image_id, reqs)
        if image_exists(base, tag):
            return base.model_copy(update={"docker_image": tag}), None
        return base, (f"project declares {len(reqs)} dependenc{'y' if len(reqs) == 1 else 'ies'} but no project "
                      "sandbox image is built; tests that import them will fail in the container "
                      "(build it: `yemendev sandbox build-image --project <id>`)")

    def build_project_image(self, actor: Actor, project_id: str, wheels_dir: Path | None = None) -> Any:
        from yemendev.sandbox.project_image import build_project_image, runtime_requirements

        self.project(project_id)
        self.guard.require(actor, Action.BUILD_SANDBOX_IMAGE, project_id, project_id=project_id)
        _, snap = self.latest_scan(project_id)
        reqs = runtime_requirements(snap)
        img = build_project_image(self.settings.sandbox, reqs, wheels_dir=wheels_dir)
        outcome = "reused" if img.reused else "built"
        self.evidence.record(
            action="sandbox.project_image", actor=actor.ref, target=project_id, outcome=outcome,
            body=EvidenceBody(inputs={"requirements": reqs}, after_state=img.model_dump(),
                              limitations=["wheels only; dependency code is installed into the image, never run on the "
                                           "host", "unpinned requirements resolve to the newest wheels at build time"]))
        self.audit.record(actor=actor.ref, action=Action.BUILD_SANDBOX_IMAGE.value, target=project_id,
                          outcome=outcome, details={"tag": img.tag, "image_id": img.image_id})
        return img

    # ---- risk & causal analysis ----------------------------------------------------------------
    def assess_risk(self, actor: Actor, project_id: str) -> Any:
        from yemendev.risk.engine import assess

        self.require_read(actor, project_id)
        report = assess(self, project_id)
        self.evidence.record(
            action="risk.assessment", actor="system:risk-engine", target=project_id,
            outcome=f"{len(report.predictions)} prediction(s), {len(report.anomalies)} anomaly(ies)",
            body=EvidenceBody(details={"predictions": len(report.predictions), "anomalies": len(report.anomalies),
                                       "risk_scores": len(report.risk_scores),
                                       "insufficient": [x.analysis for x in report.insufficient_evidence]},
                              limitations=report.limitations))
        return report

    def impact(self, actor: Actor, project_id: str, module: str) -> dict[str, Any]:
        from yemendev.twin.causal import failure_propagation, single_points_of_failure
        from yemendev.twin.graph import DependencyGraph

        self.require_read(actor, project_id)
        _, snap = self.latest_scan(project_id)
        g = DependencyGraph.from_snapshot(snap)
        if module not in g.nodes:
            raise NotFoundError("module not found in the latest scan")
        tests = {m.name for m in snap.modules if m.is_test}
        return {"module": module, "impacted": [x.model_dump() for x in failure_propagation(g, module, tests)],
                "single_points_of_failure": single_points_of_failure(g, snap),
                "note": "static import reachability: where a failure CAN propagate, not where it did"}

    def latest_scan(self, project_id: str) -> tuple[str, ProjectSnapshot]:
        with self.db.read() as conn:
            r = conn.execute("SELECT id, snapshot_json FROM project_scans WHERE project_id = ? ORDER BY created_at"
                             " DESC LIMIT 1", (project_id,)).fetchone()
        if r is None:
            raise NotFoundError("project has not been scanned")
        return r["id"], ProjectSnapshot(**json.loads(r["snapshot_json"]))

    # ---- skills (activation requires a bound human approval) --------------------------
    def request_skill_activation(self, actor: Actor, skill_id: str, version: int) -> ApprovalRequest:
        self.guard.require(actor, Action.ACTIVATE_SKILL, skill_id)
        s = self.skills.check_activatable(skill_id, version)
        req = self.approvals.request(
            action=Action.ACTIVATE_SKILL, target=f"{skill_id}@{version}",
            operation_digest=self.skills.activation_digest(s),
            summary=f"Activate skill '{s.spec.name}' v{version}: {s.spec.description}", requested_by=actor)
        self.audit.record(actor=actor.ref, action="approval.request", target=req.id, outcome="pending",
                          details={"skill": skill_id, "version": version})
        return req

    def activate_skill(self, actor: Actor, approval_id: str) -> Skill:
        self.guard.require(actor, Action.ACTIVATE_SKILL, approval_id)
        req = self.approvals.get(approval_id)
        skill_id, _, ver = req.target.rpartition("@")
        if not skill_id or not ver.isdigit():
            raise InvalidInputError("approval is not a skill activation")
        s = self.skills.check_activatable(skill_id, int(ver))
        self.approvals.consume(approval_id, action=Action.ACTIVATE_SKILL, target=req.target,
                               operation_digest=self.skills.activation_digest(s))
        out = self.skills.activate(skill_id, int(ver))
        self.audit.record(actor=actor.ref, action=Action.ACTIVATE_SKILL.value, target=req.target, outcome="ok",
                          details={"approval_id": approval_id, "decided_by": req.decided_by})
        return out

    def verify_integrity(self) -> dict[str, Any]:
        """Cross-check: audit chain valid; every anchored evidence record exists, is unmodified, and is anchored."""
        audit = self.audit.verify()
        anchored: dict[str, str] = {}
        after = 0
        while batch := self.audit.list(limit=1000, after_seq=after):
            for e in batch:
                if e.action == "evidence.recorded":
                    anchored[e.target] = str(e.details.get("content_hash"))
            after = batch[-1].seq
        stored = self.evidence.all_hashes()
        problems = list(audit.problems)
        problems += [f"evidence {i} missing (deleted)" for i in anchored if i not in stored]
        problems += [f"evidence {i} hash differs from audit anchor" for i, h in anchored.items()
                     if i in stored and stored[i] != h]
        problems += [f"evidence {i} not anchored in audit log" for i in stored if i not in anchored]
        for i in stored:
            if not self.evidence.get(i).hash_valid:
                problems.append(f"evidence {i} content does not match its hash (modified)")
        return {"valid": not problems, "audit_events": audit.events_checked, "evidence_records": len(stored),
                "audit_head": audit.head_hash, "problems": problems}

    def status(self) -> dict[str, Any]:
        with self.db.read() as conn:
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 - fixed names
                      for t in ("projects", "tasks", "approval_requests", "evidence_records", "skills",
                                "memory_records", "audit_events")}
            pending = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'").fetchone()[0]
        from yemendev.sandbox.docker_backend import isolation_report
        from yemendev.sandbox.workspace import network_namespace_available

        return {
            "schema_version": self.db.schema_version(),
            "counts": counts,
            "pending_approvals": pending,
            "models": self.models.status(),
            "sandbox": {"network_isolation_mode": self.settings.sandbox.network_isolation,
                        "network_namespace_available": network_namespace_available(),
                        "privilege_separation": self.settings.sandbox.sandbox_identity() is not None,
                        "privilege_separation_mode": self.settings.sandbox.privilege_separation,
                        **{f"isolation_{k}": v for k, v in isolation_report(self.settings.sandbox).items()}},
            "offline_capable": True,
            "generated_at": iso(),
        }

    def config_digest(self) -> str:
        return canonical_json({"sandbox": self.settings.sandbox.model_dump(), "model_configured":
                               bool(self.settings.model.base_url)})
