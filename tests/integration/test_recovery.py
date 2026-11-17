import os

import pytest
from helpers import make_renamed_api_project, write

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import IntegrityError, InvalidInputError, PreconditionFailedError, sha256_bytes
from yemendev.core.faults import FaultInjector, InjectedFault
from yemendev.recovery.checkpoint import CheckpointStore
from yemendev.recovery.engine import RecoveryEngine
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.sandbox.testrunner import run_pytest
from yemendev.sandbox.workspace import Workspace

S = SandboxSettings(timeout_seconds=60)


@pytest.fixture
def env(tmp_path):
    root = make_renamed_api_project(tmp_path / "proj")
    faults = FaultInjector()
    eng = RecoveryEngine(S, CheckpointStore(tmp_path / "ckpt"), tmp_path, faults)
    with Workspace(root, S) as ws:
        baseline = run_pytest(ws)
    return root, eng, baseline, faults


def plan_for(root, new_text, path="calc/api.py"):
    return RepairPlan(id="pln_t", description="d", source=PlanSource.HUMAN, source_ref="t",
                      patches=[FilePatch(path=path, original_sha256=sha256_bytes((root / path).read_bytes()),
                                         new_content=new_text)])


def good(root):
    return (root / "calc/api.py").read_text().replace("add_numbers", "add")


def bad(root):  # "fixes" total but breaks product
    return good(root).replace("mathutils.mul(p, x)", "mathutils.add(p, x)")


@pytest.mark.parametrize("path", ["../x.py", "/etc/passwd", "a/../../x", ".git/config", "a\\b.py", "./a.py"])
def test_patch_paths_validated(path):
    with pytest.raises(ValueError):
        FilePatch(path=path, original_sha256=None, new_content="")


def test_good_repair_full_lifecycle(env):
    root, eng, baseline, _ = env
    plan = plan_for(root, good(root))
    prep = eng.prepare(root, plan)
    assert "-        t = mathutils.add_numbers(t, x)" in prep.diff
    sim = eng.simulate(root, plan, baseline)
    assert sim.passed and sim.comparison.fixed == ["tests.test_api::test_total"]
    assert "add_numbers" in (root / "calc/api.py").read_text()  # simulation never touches the project
    eng.execute(root, plan, "ckpt_good")
    v = eng.verify(root, plan, baseline)
    assert v.passed and not v.comparison.regressed


def test_bad_repair_detected_and_rolled_back_byte_exact(env):
    root, eng, baseline, _ = env
    before = (root / "calc/api.py").read_bytes()
    plan = plan_for(root, bad(root))
    sim = eng.simulate(root, plan, baseline)
    assert not sim.passed and sim.comparison.regressed == ["tests.test_api::test_product"]
    # Even if executed anyway, verification fails and rollback restores the exact bytes.
    eng.execute(root, plan, "ckpt_bad")
    assert not eng.verify(root, plan, baseline).passed
    eng.rollback("ckpt_bad")
    assert (root / "calc/api.py").read_bytes() == before


def test_stale_plan_rejected(env):
    root, eng, _, _ = env
    plan = plan_for(root, good(root))
    write(root, "calc/api.py", "# edited by a human meanwhile\n")
    with pytest.raises(PreconditionFailedError):
        eng.execute(root, plan, "ckpt_stale")


def test_plans_touching_tests_rejected(env):
    root, eng, _, _ = env
    for path in ["tests/test_api.py", "conftest.py"]:
        if not (root / path).exists():
            write(root, path, "")
        with pytest.raises(PreconditionFailedError):
            eng.prepare(root, plan_for(root, "def test_total(): pass\n", path=path))


def test_refuses_to_write_through_symlink(env, tmp_path):
    root, eng, _, _ = env
    victim = tmp_path / "victim.py"
    victim.write_text("precious\n")
    os.symlink(victim, root / "calc" / "evil.py")
    plan = RepairPlan(id="pln_s", description="d", source=PlanSource.HUMAN, source_ref="t",
                      patches=[FilePatch(path="calc/evil.py", original_sha256=sha256_bytes(b"precious\n"),
                                         new_content="pwned\n")])
    with pytest.raises(InvalidInputError):
        eng.execute(root, plan, "ckpt_sym")
    assert victim.read_text() == "precious\n"


def test_crash_after_partial_write_is_detectable_and_recoverable(env):
    root, eng, _, faults = env
    write(root, "calc/extra.py", "A = 1\n")
    plan = RepairPlan(id="pln_p", description="d", source=PlanSource.HUMAN, source_ref="t", patches=[
        FilePatch(path="calc/api.py", original_sha256=sha256_bytes((root / "calc/api.py").read_bytes()),
                  new_content=good(root)),
        FilePatch(path="calc/extra.py", original_sha256=sha256_bytes(b"A = 1\n"), new_content="A = 2\n"),
    ])
    before = {p: (root / p).read_bytes() for p in ("calc/api.py", "calc/extra.py")}
    faults.arm("recovery.after_file_write")
    with pytest.raises(InjectedFault):
        eng.execute(root, plan, "ckpt_partial")
    assert eng.applied_state(root, plan) == "partial"
    eng.rollback("ckpt_partial")
    assert {p: (root / p).read_bytes() for p in before} == before
    assert eng.applied_state(root, plan) == "original"


def test_corrupted_checkpoint_refuses_restore(env, tmp_path):
    root, eng, _, _ = env
    plan = plan_for(root, good(root))
    eng.execute(root, plan, "ckpt_corrupt")
    (tmp_path / "ckpt" / "ckpt_corrupt" / "blobs" / "0000").write_bytes(b"garbage")
    with pytest.raises(IntegrityError):
        eng.rollback("ckpt_corrupt")
