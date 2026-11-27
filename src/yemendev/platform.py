"""Composition root: builds every engine from Settings. The only module that wires engines together."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from yemendev.agents.registry import AGENTS
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
from yemendev.governance.actors import ROLE_RANK, Actor, ActorKind, Role
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


def iso_from_mtime(p: Path) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="microseconds")


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
        try:
            r = self.approvals.decide(approval_id, eff, approve, reason)
        except PermissionDeniedError as exc:  # e.g. self-approval: refused attempts are audited like guard denials
            self.audit.record(actor=actor.ref, action=Action.DECIDE_APPROVAL.value, target=approval_id,
                              outcome="denied", details={"reason": exc.message, "project_id": pid})
            raise
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

    def immune_check(self, actor: Actor, project_id: str) -> Any:
        from yemendev.immune.engine import check

        self.require_read(actor, project_id)
        report = check(self, project_id)
        self.evidence.record(
            action="immune.check", actor="system:immune", target=project_id,
            outcome=f"{report.summary['security']} security / {report.summary['operational']} operational",
            body=EvidenceBody(details={"summary": report.summary, "baseline_scan_id": report.baseline_scan_id,
                                       "security_findings": [f.model_dump() for f in report.findings
                                                             if f.category == "security"]},
                              limitations=report.limitations))
        if report.summary["security"]:
            self.audit.record(actor="system:immune", action="immune.security_findings", target=project_id,
                              outcome="flagged", details={"count": report.summary["security"],
                                                          "high": report.summary["high"]})
        return report

    def continuity_debt(self, actor: Actor, project_id: str) -> Any:
        from yemendev.immune.continuity_debt import assess_debt

        self.require_read(actor, project_id)
        return assess_debt(self, project_id)

    def counterfactual(self, actor: Actor, project_id: str, scenario: Any) -> Any:
        """Run a what-if scenario on a sandbox copy (the real project is never modified)."""
        from yemendev.memory.model import MemoryCategory, MemoryWrite
        from yemendev.simulation.counterfactual import run_counterfactual

        root = self.project_root(project_id)
        self.guard.require(actor, Action.RUN_SANDBOXED, project_id, project_id=project_id)
        _, snap = self.latest_scan(project_id)
        sandbox, image_note = self.sandbox_for(project_id)
        result = self.agents.run(
            "testing", "counterfactual", (Action.RUN_SANDBOXED,),
            lambda: run_counterfactual(root, snap, scenario, sandbox, self.settings.workspaces_dir),
            project_id=project_id, inputs=scenario.model_dump(mode="json"))
        if image_note:
            result.limitations.append(image_note)
        ev = self.evidence.record(
            action="counterfactual.run", actor=AGENTS["testing"].actor.ref, target=project_id,
            outcome="blind_spot" if result.blind_spot else ("detected" if result.detected else "inconclusive"),
            body=EvidenceBody(inputs=scenario.model_dump(mode="json"),
                              details={"newly_failing": result.newly_failing, "baseline": result.baseline_status,
                                       "outcome": result.outcome_status, "precision": result.static_precision,
                                       "recall": result.static_recall, "notes": result.notes},
                              limitations=result.limitations))
        if result.blind_spot:
            self.memory.write(MemoryWrite(
                project_id=project_id, category=MemoryCategory.OBSERVATION, subject=f"blind_spot:{scenario.name}",
                key=",".join(sorted(f.kind for f in scenario.faults)),
                value={"faults": [f.model_dump() for f in scenario.faults],
                       "finding": "no test detected this fault"},
                verified=True, confidence=1.0, source="counterfactual", evidence_ids=[ev.id]))
        return result

    # ---- continuity: passports, synchronization, key trust -----------------------------------------
    @property
    def signing_key(self) -> Any:
        from yemendev.continuity.signing import load_or_create_signing_key

        if not hasattr(self, "_signing_key"):
            self._signing_key = load_or_create_signing_key(self.settings.keys_dir)
        return self._signing_key

    def trusted_public_keys(self) -> set[str]:
        from yemendev.continuity.signing import trusted_keys

        return trusted_keys(self.settings.keys_dir, self.signing_key)

    def export_passport(self, actor: Actor, project_id: str) -> Any:
        from yemendev.continuity.passport import build_passport, sign_passport

        self.require_read(actor, project_id)
        signed = sign_passport(build_passport(self, project_id), self.signing_key)
        self.audit.record(actor=actor.ref, action="passport.export", target=project_id, outcome="ok",
                          details={"body_sha256": signed.signature.body_sha256,
                                   "signer": signed.signature.signer_fingerprint})
        return signed

    def verify_passport(self, actor: Actor, raw: str | bytes) -> Any:
        from yemendev.continuity.passport import verify_passport

        self.guard.require(actor, Action.READ, "passport")
        return verify_passport(raw, self.trusted_public_keys())

    def import_passport(self, actor: Actor, project_id: str, raw: str | bytes) -> Any:
        """Store a verified passport from another installation as UNVERIFIED project identity knowledge."""
        from yemendev.continuity.passport import verify_passport
        from yemendev.memory.model import MemoryCategory, MemoryWrite

        self.project(project_id)
        self.guard.require(actor, Action.SYNC_IMPORT, project_id, project_id=project_id)
        result, sp = verify_passport(raw, self.trusted_public_keys())
        if sp is None or not result.accepted:
            raise PermissionDeniedError("passport rejected: " + "; ".join(result.problems))
        if not result.signer_trusted:
            raise PermissionDeniedError(f"passport signer {result.signer_fingerprint} is not trusted")
        self.memory.write(MemoryWrite(
            project_id=project_id, category=MemoryCategory.PROJECT_IDENTITY,
            subject=f"passport:{result.signer_fingerprint}", key=sp.body.identity.get("name", "unknown"),
            value={"identity": sp.body.identity, "dependencies": sp.body.dependencies,
                   "recovery": sp.body.recovery, "generated_at": sp.body.generated_at},
            verified=False, confidence=0.8, source=f"passport:{result.signer_fingerprint}"))
        self.audit.record(actor=actor.ref, action="passport.import", target=project_id, outcome="ok",
                          details={"signer": result.signer_fingerprint})
        return result

    def sync_export(self, actor: Actor, project_id: str) -> Any:
        from yemendev.continuity.sync import export_bundle

        self.project(project_id)
        self.guard.require(actor, Action.SYNC_EXPORT, project_id, project_id=project_id)
        bundle, job = export_bundle(self, project_id, self.signing_key)
        self.audit.record(actor=actor.ref, action=Action.SYNC_EXPORT.value, target=project_id, outcome=job.status,
                          details={"job_id": job.id, "records": job.records_total})
        return bundle, job

    def sync_import(self, actor: Actor, project_id: str, raw: str | bytes) -> Any:
        from yemendev.continuity.sync import import_bundle

        self.project(project_id)
        self.guard.require(actor, Action.SYNC_IMPORT, project_id, project_id=project_id)
        job = import_bundle(self, project_id, raw, self.trusted_public_keys(), actor.ref)
        self.evidence.record(action="sync.import", actor=actor.ref, target=project_id, outcome=job.status,
                             body=EvidenceBody(details=job.model_dump(),
                                               limitations=["imported knowledge is unverified on this installation"]))
        self.audit.record(actor=actor.ref, action=Action.SYNC_IMPORT.value, target=project_id, outcome=job.status,
                          details={"job_id": job.id, "applied": job.records_applied, "conflicts": job.conflicts,
                                   "peer": job.peer_fingerprint})
        return job

    def sync_conflicts(self, actor: Actor, project_id: str, status: str | None = None) -> Any:
        from yemendev.continuity.sync import list_conflicts

        self.require_read(actor, project_id)
        return list_conflicts(self, project_id, status)

    def resolve_sync_conflict(self, actor: Actor, conflict_id: str, resolution: str) -> Any:
        from yemendev.continuity.sync import get_conflict, resolve_conflict

        c = get_conflict(self, conflict_id)
        if actor.kind is not ActorKind.HUMAN:
            raise PermissionDeniedError("only humans resolve synchronization conflicts")
        self.guard.require(actor, Action.RESOLVE_SYNC_CONFLICT, conflict_id, project_id=c.project_id)
        out = resolve_conflict(self, conflict_id, resolution, actor.ref)
        self.audit.record(actor=actor.ref, action=Action.RESOLVE_SYNC_CONFLICT.value, target=conflict_id,
                          outcome=resolution, details={"project_id": c.project_id})
        return out

    def sync_status(self, actor: Actor) -> dict[str, Any]:
        from yemendev.continuity.sync import list_jobs

        self.guard.require(actor, Action.READ, "synchronization")
        jobs = [j for j in list_jobs(self) if self.guard.can_read(actor, j.project_id)]
        with self.db.read() as conn:
            open_by_project = {r[0]: r[1] for r in conn.execute(
                "SELECT project_id, COUNT(*) FROM sync_conflicts WHERE status = 'open' GROUP BY project_id")}
        return {"transport": "signed file bundles only (no network transport)",
                "jobs": [j.model_dump() for j in jobs],
                "open_conflicts": {k: v for k, v in open_by_project.items() if self.guard.can_read(actor, k)},
                "note": "an export is 'exported_not_delivered': delivery to the peer is not known to this node"}

    def trust_key(self, actor: Actor, public_key_hex: str) -> str:
        from yemendev.continuity.signing import fingerprint

        self.guard.require(actor, Action.TRUST_KEY, "keys")
        key = public_key_hex.strip().lower()
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise InvalidInputError("public key must be 64 hex characters (raw Ed25519)")
        d = self.settings.keys_dir / "trusted"
        d.mkdir(mode=0o700, exist_ok=True)
        fp = fingerprint(key)
        (d / f"{fp}.pub").write_text(key + "\n")
        self.audit.record(actor=actor.ref, action=Action.TRUST_KEY.value, target=fp, outcome="trusted")
        return fp

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

    def project_sbom(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """CycloneDX SBOM of a project from its latest scan; generating one is recorded as evidence."""
        from yemendev.core.contracts import digest
        from yemendev.security.sbom import project_sbom

        self.require_read(actor, project_id)
        sid, snap = self.latest_scan(project_id)
        doc = project_sbom(snap, self.project(project_id).name)
        self.evidence.record(action="sbom.generated", actor=actor.ref, target=project_id, outcome="ok",
                             body=EvidenceBody(details={
                                 "scan_id": sid, "components": len(doc["components"]),
                                 "components_sha256": digest(doc["components"]),
                                 "compositions": {c["description"][:60]: c["aggregate"] for c in doc["compositions"]}},
                                 limitations=["versions are exact only where a lockfile or an exact pin exists"]))
        return doc

    def platform_sbom(self, actor: Actor) -> dict[str, Any]:
        from yemendev.security.sbom import platform_sbom

        self.guard.require(actor, Action.READ, "system")
        lock = Path(__file__).resolve().parents[2] / "apps" / "web" / "package-lock.json"
        return platform_sbom(lock if lock.is_file() else None)

    # ---- continuity: backups, checkpoints, recovery readiness ---------------------------
    @property
    def backups_dir(self) -> Path:
        return self.settings.data_dir / "backups"

    def create_backup(self, actor: Actor) -> dict[str, Any]:
        import hashlib

        from yemendev.continuity.backup import create_backup

        self.guard.require(actor, Action.MANAGE_BACKUPS, "backups")
        archive, m = create_backup(self.settings.data_dir, self.db, self.settings.keys_dir,
                                   self.settings.checkpoints_dir, self.backups_dir, actor.ref)
        h = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backups(id, created_at, created_by, path, size_bytes, archive_sha256,"
                         " schema_version, audit_head) VALUES (?,?,?,?,?,?,?,?)",
                         (m.backup_id, m.created_at, actor.ref, str(archive), archive.stat().st_size, h,
                          m.schema_version, m.audit_head))
        self.audit.record(actor=actor.ref, action=Action.MANAGE_BACKUPS.value, target=m.backup_id, outcome="created",
                          details={"archive_sha256": h, "files": len(m.files), "audit_events": m.audit_events})
        return self.backup(actor, m.backup_id)

    def backup(self, actor: Actor, backup_id: str) -> dict[str, Any]:
        self.guard.require(actor, Action.READ, "backups")
        with self.db.read() as conn:
            r = conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
        if r is None:
            raise NotFoundError("backup not found")
        return self._backup_row(r)

    def list_backups(self, actor: Actor) -> list[dict[str, Any]]:
        self.guard.require(actor, Action.READ, "backups")
        with self.db.read() as conn:
            return [self._backup_row(r) for r in conn.execute("SELECT * FROM backups ORDER BY created_at DESC")]

    @staticmethod
    def _backup_row(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["verified_ok"] = None if d["verified_ok"] is None else bool(d["verified_ok"])
        d["verify_report"] = json.loads(d.pop("verify_report_json")) if d["verify_report_json"] else None
        d["file_present"] = Path(d["path"]).is_file()
        return d

    def verify_backup(self, actor: Actor, backup_id: str) -> dict[str, Any]:
        """Full verification on a temporary copy; the result is stored and audited."""
        import hashlib

        from yemendev.continuity.backup import Check, verify_backup
        from yemendev.continuity.signing import public_hex

        self.guard.require(actor, Action.MANAGE_BACKUPS, backup_id)
        row = self.backup(actor, backup_id)
        if not row["file_present"]:
            raise NotFoundError("backup archive is no longer on disk")
        known = {public_hex(self.signing_key), *self.trusted_public_keys()}
        rep = verify_backup(Path(row["path"]), known)
        # The archive holds the keys, so whoever has the file could re-sign a forged one. What it cannot change
        # is the SHA-256 this database recorded at creation: compare against it.
        now = hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest()
        rep.checks.insert(0, Check(name="archive unchanged since creation", ok=now == row["archive_sha256"],
                                   detail="sha256 matches the value recorded when it was created" if
                                   now == row["archive_sha256"] else "archive was REPLACED or modified on disk"))
        rep.ok = all(c.ok for c in rep.checks)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backups SET verified_at = ?, verified_ok = ?, verify_report_json = ? WHERE id = ?",
                         (iso(), int(rep.ok), rep.model_dump_json(), backup_id))
        self.audit.record(actor=actor.ref, action=Action.MANAGE_BACKUPS.value, target=backup_id,
                          outcome="verified" if rep.ok else "verification_failed",
                          details={"failed": [c.name for c in rep.checks if not c.ok]})
        return self.backup(actor, backup_id)

    def list_checkpoints(self, actor: Actor, project_id: str) -> list[dict[str, Any]]:
        from yemendev.recovery.checkpoint import CheckpointManifest

        self.require_read(actor, project_id)
        root = str(self.project_root(project_id))
        out = []
        base = self.settings.checkpoints_dir
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            try:
                m = CheckpointManifest.model_validate_json((d / "manifest.json").read_bytes())
            except (OSError, ValueError):
                continue
            if m.project_root != root:
                continue
            task_id = m.id.removeprefix("ckpt_")
            with self.db.read() as conn:
                t = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                rb = conn.execute("SELECT status FROM task_steps WHERE task_id = ? AND name = 'rollback'",
                                  (task_id,)).fetchone()
            out.append({"id": m.id, "task_id": task_id, "task_status": t["status"] if t else None,
                        "files": [e.path for e in m.entries], "rolled_back": bool(rb and rb["status"] == "done"),
                        "created_at": iso_from_mtime(d / "manifest.json")})
        return out

    def recovery_readiness(self, actor: Actor) -> dict[str, Any]:
        """Independent checks with explanations - deliberately no combined score."""
        import shutil as _shutil
        from datetime import UTC, datetime

        self.guard.require(actor, Action.READ, "system")
        checks: list[dict[str, str]] = []

        def add(name: str, status: str, detail: str) -> None:
            checks.append({"check": name, "status": status, "detail": detail})

        with self.db.read() as conn:
            last = conn.execute("SELECT * FROM backups ORDER BY created_at DESC LIMIT 1").fetchone()
            last_ok = conn.execute("SELECT * FROM backups WHERE verified_ok = 1 ORDER BY created_at DESC LIMIT 1"
                                   ).fetchone()
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            queue = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM task_queue GROUP BY state")}
            conflicts = conn.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status = 'open'").fetchone()[0]
            stuck = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running' AND lease_expires_at < ?",
                                 (iso(),)).fetchone()[0]
        if last_ok is None:
            add("verified backup", "fail" if last is None else "warn",
                "no backup yet" if last is None else "backups exist but none has passed verification")
        else:
            age = (datetime.now(UTC) - datetime.fromisoformat(last_ok["created_at"])).total_seconds() / 86400
            add("verified backup", "ok" if age <= 7 else "warn",
                f"newest verified backup is {age:.1f} day(s) old ({last_ok['id']})")
        integ = self.verify_integrity()
        add("audit and evidence", "ok" if integ["valid"] else "fail",
            f"{integ['audit_events']} audit events, {integ['evidence_records']} evidence records"
            + ("" if integ["valid"] else f"; {integ['problems'][:3]}"))
        add("database", "ok" if quick == "ok" else "fail", f"PRAGMA quick_check: {quick}")
        free = _shutil.disk_usage(self.settings.data_dir).free
        add("free disk space", "ok" if free > 1 << 30 else "warn" if free > 200 << 20 else "fail",
            f"{free / (1 << 30):.1f} GiB free on the data directory's filesystem")
        pending = sum(queue.get(s, 0) for s in ("queued", "running", "waiting"))
        add("pending work preserved", "warn" if stuck else "ok",
            f"{pending} queued/running/waiting task(s) persisted" + (f"; {stuck} running task(s) with an expired "
                                                                     "lease (resume them)" if stuck else ""))
        add("synchronization conflicts", "warn" if conflicts else "ok", f"{conflicts} open conflict(s)")
        from yemendev.sandbox.docker_backend import isolation_report

        iso_rep = isolation_report(self.settings.sandbox)
        iso_warnings = list(cast(list[str], iso_rep["warnings"]))
        first_warning = f"; {iso_warnings[0]}" if iso_warnings else ""
        add("sandbox", "warn" if iso_warnings else "ok",
            f"effective backend {iso_rep['effective']}{first_warning}")
        from yemendev.continuity.backup import _checkpoints_ok

        cp_ok, cp_detail = _checkpoints_ok(self.settings.checkpoints_dir)
        add("checkpoints restorable", "ok" if cp_ok else "fail", cp_detail)
        rank = {"ok": 0, "warn": 1, "fail": 2}
        overall = max((c["status"] for c in checks), key=lambda s: rank[s])
        return {"overall": overall, "checks": checks, "generated_at": iso()}

    # ---- dependency vulnerabilities (offline OSV database) ------------------------------
    def import_advisories(self, actor: Actor, path: str) -> Any:
        """Admin, CLI only: the source is a local path (an API taking server paths would expose files)."""
        from yemendev.security.vulns import import_advisories

        self.guard.require(actor, Action.IMPORT_ADVISORIES, "advisories")
        s = import_advisories(self.db, Path(path), actor.ref)
        self.audit.record(actor=actor.ref, action=Action.IMPORT_ADVISORIES.value, target=s.import_id, outcome="ok",
                          details=s.model_dump())
        return s

    def benchmark_reports(self, actor: Actor) -> list[dict[str, Any]]:
        from yemendev.benchmarks.results import list_reports

        self.guard.require(actor, Action.READ, "benchmarks")
        return list_reports(self.settings)

    def benchmark_report(self, actor: Actor, source: str, name: str) -> dict[str, Any]:
        from yemendev.benchmarks.results import load_report

        self.guard.require(actor, Action.READ, "benchmarks")
        return load_report(self.settings, source, name)

    def advisory_status(self, actor: Actor) -> dict[str, Any]:
        from yemendev.security.vulns import database_status

        self.guard.require(actor, Action.READ, "advisories")
        return database_status(self.db)

    def vulnerabilities(self, actor: Actor, project_id: str) -> Any:
        from yemendev.security.sbom import project_sbom
        from yemendev.security.vulns import scan_components

        self.require_read(actor, project_id)
        sid, snap = self.latest_scan(project_id)
        report = scan_components(self.db, project_sbom(snap, self.project(project_id).name)["components"],
                                 subject=project_id)
        last = report.database.get("last_import") or {}
        self.evidence.record(action="dependencies.vulnerability_scan", actor=actor.ref, target=project_id,
                             outcome=f"{len(report.findings)} finding(s)",
                             body=EvidenceBody(details={"scan_id": sid, "checked": report.checked,
                                                        "findings": [f"{f.component} {f.advisory_id}"
                                                                     for f in report.findings][:200],
                                                        "not_checkable": len(report.not_checkable),
                                                        "advisory_import": last.get("id")},
                                               limitations=report.limitations))
        return report

    def platform_vulnerabilities(self, actor: Actor) -> Any:
        from yemendev.security.vulns import scan_components

        sbom = self.platform_sbom(actor)
        return scan_components(self.db, sbom["components"], subject="yemendev")

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
            queue = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM task_queue GROUP BY state")}
        from yemendev.sandbox.docker_backend import isolation_report
        from yemendev.sandbox.workspace import network_namespace_available

        return {
            "schema_version": self.db.schema_version(),
            "counts": counts,
            "pending_approvals": pending,
            "queue": {s: queue.get(s, 0) for s in ("queued", "running", "waiting")},
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
