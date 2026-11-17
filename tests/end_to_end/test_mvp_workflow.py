"""MVP acceptance tests (docs/FEATURE_STATUS.md, section 'MVP acceptance')."""

import pytest
from end_to_end.conftest import ALICE, BOB
from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.contracts import PermissionDeniedError, sha256_bytes
from yemendev.core.faults import InjectedFault
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.approvals import ApprovalStatus
from yemendev.learning.skills import SkillStatus
from yemendev.memory.model import MemoryCategory
from yemendev.orion.tasks import TaskStatus
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.sandbox.testrunner import RunStatus, run_pytest
from yemendev.sandbox.workspace import Workspace


def human_fix(root):
    p = root / "calc/api.py"
    return RepairPlan(id="pln_human", description="rename add_numbers -> add", source=PlanSource.HUMAN,
                      source_ref="human:alice", hypothesis_id="H1", patches=[
                          FilePatch(path="calc/api.py", original_sha256=sha256_bytes(p.read_bytes()),
                                    new_content=p.read_text().replace("add_numbers", "add"))])


def pending_approval(platform, task_id):
    [a] = [a for a in platform.approvals.for_task(task_id) if a.status is ApprovalStatus.PENDING]
    return a


def suite_passes(platform, root):
    with Workspace(root, platform.settings.sandbox) as ws:
        return run_pytest(ws).status is RunStatus.ALL_PASSED


def test_mvp_end_to_end_with_learning_and_skill_reuse(platform, wf, tmp_path, no_network):
    # 1-2. accept & inspect a local Python project
    root = make_renamed_api_project(tmp_path / "proj1")
    proj = platform.register_project(ALICE, "calc", str(root))
    t = wf.create(ALICE, proj.id, human_plan=human_fix(root))

    # 3-8. scan, baseline (x2), diagnose, propose, simulate -> stops for approval
    r = wf.advance(ALICE, t.id)
    assert r.task.status is TaskStatus.WAITING_APPROVAL, r.message
    assert r.ran_steps == ["scan", "baseline", "diagnose", "propose", "simulate", "authorize"]
    assert "add_numbers" in (root / "calc/api.py").read_text()  # nothing applied yet
    diag = wf.tasks.step(t.id, "diagnose").output
    assert diag["reproducible_problem"] and diag["hypotheses"][0]["status"] == "unverified"

    # advancing again without a decision changes nothing
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL
    apr = pending_approval(platform, t.id)
    with pytest.raises(PermissionDeniedError):  # human-authored plan: the author cannot approve it
        platform.approvals.decide(apr.id, ALICE, approve=True)
    platform.approvals.decide(apr.id, BOB, approve=True)

    # 9-10, 13. execute in place after approval, verify, preserve evidence
    r = wf.advance(ALICE, t.id)
    assert r.task.status is TaskStatus.SUCCEEDED, r.message
    assert r.ran_steps == ["authorize", "execute", "verify", "learn"]
    assert "add_numbers" not in (root / "calc/api.py").read_text()
    assert suite_passes(platform, root)
    assert platform.approvals.get(apr.id).status is ApprovalStatus.CONSUMED
    actions = [e.action for e in platform.evidence.list(task_id=t.id)]
    for a in ["project.scan", "tests.baseline", "diagnostics.report", "repair.propose", "repair.simulate",
              "repair.authorize", "repair.execute", "repair.verify"]:
        assert a in actions
    assert all(e.hash_valid for e in platform.evidence.list(task_id=t.id))
    assert platform.audit.verify().valid

    # 14. candidate experience + candidate skill (not active!)
    exp = platform.memory.query(proj.id, category=MemoryCategory.EXPERIENCE)
    assert len(exp) == 1 and exp[0].status.value == "verified"
    cand = wf.tasks.step(t.id, "learn").output["candidate_skill"]
    assert platform.skills.get(cand).status is SkillStatus.PROPOSED

    # 15. validate, then activate only through a bound approval decided by another human
    assert platform.skills.validate(cand).passed
    req = platform.request_skill_activation(ALICE, cand, 1)
    with pytest.raises(PermissionDeniedError):
        platform.approvals.decide(req.id, ALICE, approve=True)
    platform.approvals.decide(req.id, BOB, approve=True)
    assert platform.activate_skill(ALICE, req.id).status is SkillStatus.ACTIVE

    # skill reuse on a second project with the same defect - no human plan, no model
    root2 = make_renamed_api_project(tmp_path / "proj2")
    p2 = platform.register_project(ALICE, "calc2", str(root2))
    t2 = wf.create(ALICE, p2.id)
    r2 = wf.advance(ALICE, t2.id)
    assert r2.task.status is TaskStatus.WAITING_APPROVAL, r2.message
    plans = wf.tasks.step(t2.id, "propose").output["plans"]
    assert [p["source"] for p in plans] == ["skill"]
    platform.approvals.decide(pending_approval(platform, t2.id).id, ALICE, approve=True)  # system-requested
    assert wf.advance(BOB, t2.id).task.status is TaskStatus.SUCCEEDED
    assert suite_passes(platform, root2)
    assert platform.skills.field_stats(cand, 1)["successes"] == 1


def test_failed_verification_rolls_back_exactly(platform, wf, tmp_path):
    """Environment drift between simulation and verification: a test that starts failing after N runs."""
    import os
    import tempfile
    root = make_renamed_api_project(tmp_path / "proj")
    # An external, shared resource the sandbox may legitimately write to (world-writable on purpose:
    # sandboxed code runs as an unprivileged uid and cannot write into root's private tmp dirs).
    shared = tempfile.mkdtemp(prefix="ydv-shared-")
    os.chmod(shared, 0o777)  # noqa: S103
    counter = os.path.join(shared, "runs.txt")
    write(root, "tests/test_env.py",
          f"from pathlib import Path\n\n\ndef test_env():\n    p = Path({str(counter)!r})\n"
          "    n = int(p.read_text()) + 1 if p.exists() else 1\n    p.write_text(str(n))\n    assert n <= 3\n")
    before = (root / "calc/api.py").read_bytes()
    proj = platform.register_project(ALICE, "drift", str(root))
    t = wf.create(ALICE, proj.id, human_plan=human_fix(root))
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL  # runs 1,2 (baseline) 3 (simulate)
    platform.approvals.decide(pending_approval(platform, t.id).id, BOB, approve=True)
    r = wf.advance(ALICE, t.id)  # run 4 (verify) fails -> rollback
    assert r.task.status is TaskStatus.ROLLED_BACK, r.message
    assert r.ran_steps == ["authorize", "execute", "verify", "rollback", "learn"]
    assert (root / "calc/api.py").read_bytes() == before
    v = wf.tasks.step(t.id, "verify").output
    assert not v["passed"] and v["comparison"]["regressed"] == ["tests.test_env::test_env"]
    assert "repair.rollback" in [e.action for e in platform.evidence.list(task_id=t.id)]
    assert platform.memory.query(proj.id, category=MemoryCategory.FAILED_EXPERIMENT)


def test_denied_approval_changes_nothing(platform, wf, tmp_path):
    root = make_renamed_api_project(tmp_path / "proj")
    before = (root / "calc/api.py").read_bytes()
    proj = platform.register_project(ALICE, "calc", str(root))
    t = wf.create(ALICE, proj.id, human_plan=human_fix(root))
    wf.advance(ALICE, t.id)
    platform.approvals.decide(pending_approval(platform, t.id).id, BOB, approve=False, reason="not now")
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.CANCELLED
    assert (root / "calc/api.py").read_bytes() == before


def test_healthy_project_needs_no_action(platform, wf, tmp_path):
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    r = wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    assert r.task.status is TaskStatus.NO_ACTION and r.ran_steps[-1] == "diagnose"


def test_no_plan_without_skills_or_model(platform, wf, tmp_path):
    proj = platform.register_project(ALICE, "calc", str(make_renamed_api_project(tmp_path / "p")))
    r = wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    assert r.task.status is TaskStatus.NO_ACTION
    assert "no model available" in r.message


def test_stale_human_plan_fails_simulation_safely(platform, wf, tmp_path):
    root = make_renamed_api_project(tmp_path / "p")
    plan = human_fix(root)
    write(root, "calc/api.py", (root / "calc/api.py").read_text() + "\n# concurrent edit\n")
    proj = platform.register_project(ALICE, "calc", str(root))
    r = wf.advance(ALICE, wf.create(ALICE, proj.id, human_plan=plan).id)
    assert r.task.status is TaskStatus.NO_ACTION
    sim = wf.tasks.step(r.task.id, "simulate")
    assert sim.output["stopped"] == "no_action"


def test_permissions(platform, wf, tmp_path):
    agent = Actor(id="planner", kind=ActorKind.AGENT, role=Role.ADMIN)
    viewer = Actor(id="v", kind=ActorKind.HUMAN, role=Role.VIEWER)
    proj = platform.register_project(ALICE, "calc", str(make_renamed_api_project(tmp_path / "p")))
    with pytest.raises(PermissionDeniedError):
        wf.create(agent, proj.id)
    with pytest.raises(PermissionDeniedError):
        platform.register_project(agent, "x", str(tmp_path))
    t = wf.create(ALICE, proj.id)
    with pytest.raises(PermissionDeniedError):
        wf.advance(viewer, t.id)
    assert any(e.outcome == "denied" for e in platform.audit.list(limit=1000))


@pytest.mark.parametrize("point", [
    "step.scan.before_finish", "step.baseline.before_finish", "step.simulate.before_finish",
    "step.authorize.before_finish", "recovery.after_checkpoint", "recovery.after_file_write",
    "step.execute.before_finish", "step.verify.before_finish", "step.learn.before_finish",
])
def test_crash_at_any_point_then_resume(settings, faults, tmp_path, point):
    """17. Resume an interrupted task: crash (InjectedFault ~ power loss), new process, resume."""
    root = make_renamed_api_project(tmp_path / "proj")
    write(root, "calc/extra.py", "A = 1\n")
    p = root / "calc/api.py"
    plan = RepairPlan(id="pln_two", description="fix + touch extra", source=PlanSource.HUMAN, source_ref="h",
                      patches=[FilePatch(path="calc/api.py", original_sha256=sha256_bytes(p.read_bytes()),
                                         new_content=p.read_text().replace("add_numbers", "add")),
                               FilePatch(path="calc/extra.py", original_sha256=sha256_bytes(b"A = 1\n"),
                                         new_content="A = 2\n")])
    plat = Platform(settings, faults)
    wf = RepairWorkflow(plat)
    proj = plat.register_project(ALICE, "calc", str(root))
    t = wf.create(ALICE, proj.id, human_plan=plan)
    pre_approval = point.split(".")[1] in ("scan", "baseline", "simulate")

    faults.arm(point)
    if pre_approval:
        with pytest.raises(InjectedFault):
            wf.advance(ALICE, t.id)
        wf.tasks.expire_lease(t.id)
        plat, wf = Platform(settings), None
        wf = RepairWorkflow(plat)
        assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL
        plat.approvals.decide(pending_approval(plat, t.id).id, BOB, approve=True)
        r = wf.advance(ALICE, t.id)
    else:
        assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL
        plat.approvals.decide(pending_approval(plat, t.id).id, BOB, approve=True)
        with pytest.raises(InjectedFault):
            wf.advance(ALICE, t.id)
        # A second worker must not run while the crashed worker's lease is still valid.
        from yemendev.core.contracts import ConflictError
        with pytest.raises(ConflictError):
            RepairWorkflow(Platform(settings)).advance(ALICE, t.id)
        wf.tasks.expire_lease(t.id)
        plat = Platform(settings)  # fresh process, no faults
        r = RepairWorkflow(plat).advance(ALICE, t.id)

    assert r.task.status is TaskStatus.SUCCEEDED, r.message
    assert (root / "calc/extra.py").read_text() == "A = 2\n"
    assert "add_numbers" not in p.read_text()
    assert suite_passes(plat, root)
    approvals = plat.approvals.for_task(t.id)
    assert len(approvals) == 1 and approvals[0].status is ApprovalStatus.CONSUMED  # never re-requested or reused
    assert plat.audit.verify().valid


def test_concurrent_advance_only_one_worker_runs(platform, wf, tmp_path):
    import threading

    from yemendev.core.contracts import ConflictError
    proj = platform.register_project(ALICE, "c", str(make_renamed_api_project(tmp_path / "p")))
    t = wf.create(ALICE, proj.id, human_plan=human_fix(tmp_path / "p"))
    results, errors = [], []

    def go():
        try:
            results.append(RepairWorkflow(platform).advance(ALICE, t.id))
        except ConflictError as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ran = [r for r in results if r.ran_steps]
    assert len(ran) == 1, "exactly one worker may execute steps"
    assert len(errors) + len(results) == 4
    steps = wf.tasks.steps(t.id)
    assert all(s.attempt == 1 for s in steps), [(s.name, s.attempt) for s in steps]
