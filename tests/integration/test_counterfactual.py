"""Counterfactual future engine (directive 6.2): faults injected into a sandbox copy, observed via tests."""

import pytest
from end_to_end.conftest import ALICE
from helpers import make_passing_project, write

from yemendev.core.contracts import InvalidInputError, PermissionDeniedError
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.memory.model import MemoryCategory
from yemendev.simulation.counterfactual import (
    DependencyRemoved,
    FileCorrupted,
    ModuleUnavailable,
    Scenario,
)


@pytest.fixture
def proc_platform(settings):
    """Process backend: the scenario uses pydantic, available to the platform's own interpreter."""
    from yemendev.platform import Platform
    settings.sandbox = settings.sandbox.model_copy(update={"backend": "process"})
    return Platform(settings)


def project(platform, tmp_path):
    root = make_passing_project(tmp_path / "p")
    write(root, "pkg/unused.py", "def never_tested():\n    return 42\n")
    write(root, "pkg/config.json", '{"level": 3}\n')
    write(root, "pkg/settings.py", "import json\nfrom pathlib import Path\n\n\ndef level():\n"
          "    return json.loads((Path(__file__).parent / 'config.json').read_text())['level']\n")
    write(root, "tests/test_settings.py",
          "from pkg.settings import level\n\n\ndef test_level():\n    assert level() == 3\n")
    write(root, "tests/test_dep.py", "import pydantic\n\n\ndef test_dep():\n    assert pydantic.BaseModel\n")
    proj = platform.register_project(ALICE, "cf", str(root))
    platform.scan_project(ALICE, proj.id)
    return root, proj


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
            and "__pycache__" not in p.parts}


def test_module_fault_detected_and_static_prediction_scored(proc_platform, tmp_path):
    root, proj = project(proc_platform, tmp_path)
    before = tree(root)
    r = proc_platform.counterfactual(ALICE, proj.id, Scenario(faults=[ModuleUnavailable(module="pkg.m")]))
    assert r.detected and not r.blind_spot and r.newly_failing == ["tests.test_m::test_f"]
    assert r.predicted_impacted_tests == ["tests.test_m::test_f"]
    assert r.static_precision == 1.0 and r.static_recall == 1.0
    assert any("failed to import: tests.test_m" in n for n in r.notes)  # other modules still ran
    assert tree(root) == before  # the real project is never touched
    ev = [e for e in proc_platform.evidence.list(limit=1000) if e.action == "counterfactual.run"][-1]
    assert ev.outcome == "detected" and ev.actor == "agent:testing-agent"
    assert [x.operation for x in proc_platform.agents.runs(agent="testing")][-1] == "counterfactual"


def test_blind_spot_is_reported_and_remembered(proc_platform, tmp_path):
    root, proj = project(proc_platform, tmp_path)
    r = proc_platform.counterfactual(ALICE, proj.id, Scenario(name="unused", faults=[
        ModuleUnavailable(module="pkg.unused")]))
    assert r.blind_spot and r.newly_failing == [] and r.predicted_impacted_tests == []
    mem = proc_platform.memory.query(proj.id, category=MemoryCategory.OBSERVATION, subject_prefix="blind_spot:")
    assert mem and mem[0].value["finding"] == "no test detected this fault" and mem[0].evidence_ids


def test_dependency_and_file_faults(proc_platform, tmp_path):
    root, proj = project(proc_platform, tmp_path)
    r = proc_platform.counterfactual(ALICE, proj.id, Scenario(faults=[DependencyRemoved(import_name="pydantic")]))
    assert r.newly_failing == ["tests.test_dep::test_dep"] and r.predicted_impacted_tests is None
    r = proc_platform.counterfactual(ALICE, proj.id, Scenario(faults=[FileCorrupted(path="pkg/config.json")]))
    assert r.newly_failing == ["tests.test_settings::test_level"]


def test_compound_fault(proc_platform, tmp_path):
    root, proj = project(proc_platform, tmp_path)
    r = proc_platform.counterfactual(ALICE, proj.id, Scenario(name="compound", faults=[
        ModuleUnavailable(module="pkg.m"), FileCorrupted(path="pkg/config.json")]))
    assert set(r.newly_failing) == {"tests.test_m::test_f", "tests.test_settings::test_level"}
    assert any("only computed for scenarios made solely of module faults" in n for n in r.notes)


@pytest.mark.parametrize("fault,msg", [
    (ModuleUnavailable(module="pkg.nope"), "not found"),
    (FileCorrupted(path="tests/test_m.py"), "test files"),
    (FileCorrupted(path="../../etc/passwd"), "escapes"),
    (DependencyRemoved(import_name="pkg"), "internal package"),
])
def test_invalid_scenarios_rejected(proc_platform, tmp_path, fault, msg):
    _, proj = project(proc_platform, tmp_path)
    with pytest.raises(InvalidInputError, match=msg):
        proc_platform.counterfactual(ALICE, proj.id, Scenario(faults=[fault]))


def test_dependency_name_validation():
    with pytest.raises(ValueError):
        DependencyRemoved(import_name="os; rm -rf /")


def test_viewer_cannot_run_scenarios(proc_platform, tmp_path):
    _, proj = project(proc_platform, tmp_path)
    viewer = Actor(id="vic", kind=ActorKind.HUMAN, role=Role.VIEWER)
    proc_platform.grant_member(ALICE, proj.id, viewer.ref, Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        proc_platform.counterfactual(ALICE if False else viewer, proj.id,
                                     Scenario(faults=[ModuleUnavailable(module="pkg.m")]))
