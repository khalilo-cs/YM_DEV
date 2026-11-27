"""Regression tests for defects found in the stage-7 independent audit (docs/AUDIT_REPORT.md)."""

import os
import shutil
import stat

import pytest
from end_to_end.conftest import ALICE, BOB
from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.contracts import PermissionDeniedError, PreconditionFailedError, sha256_bytes
from yemendev.governance.approvals import ApprovalStatus
from yemendev.orion.tasks import TaskStatus
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan


def fix_plan(root, source=PlanSource.HUMAN, ref="h"):
    api = root / "calc/api.py"
    return RepairPlan(id="pln_r", description="x", source=source, source_ref=ref, patches=[FilePatch(
        path="calc/api.py", original_sha256=sha256_bytes(api.read_bytes()),
        new_content=api.read_text().replace("add_numbers", "add"))])


def test_f1_client_cannot_relabel_plan_to_self_approve(platform, wf, tmp_path):
    root = make_renamed_api_project(tmp_path / "p")
    proj = platform.register_project(ALICE, "p", str(root))
    t = wf.create(ALICE, proj.id, human_plan=fix_plan(root, PlanSource.SKILL, "skl_fake@1"))
    assert t.params["human_plan"]["source"] == "human" and t.params["human_plan"]["source_ref"] == "human:alice"
    wf.advance(ALICE, t.id)
    [apr] = platform.approvals.for_task(t.id)
    assert apr.requested_by == "human:alice"
    with pytest.raises(PermissionDeniedError):
        platform.approvals.decide(apr.id, ALICE, approve=True)


def test_f3_platform_state_permissions(platform, settings):
    for path in [settings.data_dir, settings.keys_dir, settings.checkpoints_dir]:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, path
    for f in settings.data_dir.glob("yemendev.sqlite3*"):
        assert not stat.S_IMODE(os.stat(f).st_mode) & 0o077, f
    assert not settings.workspaces_dir.resolve().is_relative_to(settings.data_dir.resolve())


def test_f4_root_swapped_for_symlink_is_refused(platform, wf, tmp_path, settings):
    victim = make_passing_project(tmp_path / "p")
    proj = platform.register_project(ALICE, "p", str(victim))
    shutil.rmtree(victim)
    os.symlink(settings.data_dir, victim)
    with pytest.raises(PreconditionFailedError):
        platform.scan_project(ALICE, proj.id)
    r = wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    assert r.task.status is TaskStatus.FAILED and "root changed" in r.message


def test_f4_cannot_register_platform_directories(platform, settings):
    from yemendev.core.contracts import InvalidInputError
    for d in [settings.data_dir, settings.workspaces_dir]:
        with pytest.raises(InvalidInputError):
            platform.register_project(ALICE, "x", str(d))


def test_f5_cancel_revokes_task_approvals(platform, wf, tmp_path):
    root = make_renamed_api_project(tmp_path / "p")
    proj = platform.register_project(ALICE, "p", str(root))
    t = wf.create(ALICE, proj.id, human_plan=fix_plan(root))
    wf.advance(ALICE, t.id)
    [apr] = platform.approvals.for_task(t.id)
    platform.approvals.decide(apr.id, BOB, approve=True)
    wf.cancel(ALICE, t.id)
    assert platform.approvals.get(apr.id).status is ApprovalStatus.REVOKED
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.CANCELLED
    assert "add_numbers" in (root / "calc/api.py").read_text()


def test_critical_sandboxed_tests_cannot_touch_platform_db(platform, wf, tmp_path, settings):
    if settings.sandbox.sandbox_identity() is None:
        pytest.skip("no privilege separation on this host")
    root = make_renamed_api_project(tmp_path / "p")
    write(root, "tests/test_evil.py",
          "import sqlite3\n\ndef test_evil():\n"
          f"    con = sqlite3.connect({str(settings.db_path)!r})\n"
          "    con.execute(\"INSERT INTO api_tokens(id, token_hash, actor_id, role, created_at) VALUES "
          "('tok_evil','x','mallory','admin','x')\")\n    con.commit()\n")
    proj = platform.register_project(ALICE, "p", str(root))
    wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    with platform.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens WHERE id='tok_evil'").fetchone()[0] == 0
    ev = [e for e in platform.evidence.list(limit=1000) if e.action == "tests.baseline"][-1]
    assert "tests.test_evil::test_evil" in ev.body.tests["runs"][0]["failing"]


def test_evidence_deletion_and_tampering_detected(platform, wf, tmp_path):
    """Audit finding: evidence content hashes detected edits but not deletion."""
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    assert platform.verify_integrity()["valid"]
    ids = list(platform.evidence.all_hashes())
    with platform.db.transaction() as conn:
        conn.execute("DELETE FROM evidence_records WHERE id = ?", (ids[0],))
        conn.execute("UPDATE evidence_records SET outcome = 'forged' WHERE id = ?", (ids[1],))
    probs = platform.verify_integrity()["problems"]
    assert any("missing (deleted)" in p for p in probs)
    assert any("modified" in p for p in probs)


def test_refused_self_approval_attempt_is_audited(platform, wf, tmp_path):
    """Regression (stage 20, found by the unauthorized_operations benchmark): a self-approval attempt passed the
    guard (the requester has the role) and was refused by the approval store, but left no audit record, unlike
    every other refused operation."""
    root = make_renamed_api_project(tmp_path / "p")
    proj = platform.register_project(ALICE, "p", str(root))
    t = wf.create(ALICE, proj.id, human_plan=fix_plan(root))
    wf.advance(ALICE, t.id)
    [apr] = platform.approvals.for_task(t.id)
    with pytest.raises(PermissionDeniedError, match="own approval"):
        platform.decide_approval(ALICE, apr.id, approve=True, reason="mine")
    [e] = [e for e in platform.audit.list(limit=500) if e.target == apr.id and e.action == "approval.decide"]
    assert e.outcome == "denied" and e.actor == ALICE.ref and "own approval" in e.details["reason"]
    assert platform.approvals.get(apr.id).status is ApprovalStatus.PENDING
    assert platform.audit.verify().valid
