from yemendev.benchmarks.fixtures import FIXTURES_VERSION, renamed_api_project
from yemendev.benchmarks.resilience import run_benchmark, scenarios


def test_fixtures_are_deterministic(tmp_path):
    a = renamed_api_project(tmp_path / "a", 42)
    b = renamed_api_project(tmp_path / "b", 42)
    assert a == b
    for f in ["tests/test_service.py", a["service"]]:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    assert renamed_api_project(tmp_path / "c", 43) != a


def test_benchmark_compound_scenarios_hold_invariants():
    rep = run_benchmark(seed=3, only=["compound_crash_plus_human_edit", "reject_regressing_plan"])
    by = {r.scenario: r for r in rep.results}
    human = by["compound_crash_plus_human_edit"]
    assert human.actual_status == "failed" and human.data_integrity, human
    assert "diverged" in human.detail
    assert by["reject_regressing_plan"].correct and by["reject_regressing_plan"].approvals_requested == 0
    assert any("bench --compare" in c for c in rep.caveats)  # comparisons live in their own report
    assert any("exact module and symbol names" in c for c in rep.caveats)  # the skill-matching limit is stated
    assert rep.git_commit is None or len(rep.git_commit) == 40


def test_scenario_catalogue():
    names = [n for n, _, _ in scenarios(full=True)]
    assert len(names) == len(set(names)) == 6 + 9 + 9  # core, crash points, extended (stage 20)
    cats = {c for _, c, _ in scenarios(full=True)}
    for required in ("skill_transfer", "long_horizon", "distributed_synchronization", "data_integrity",
                     "knowledge_staleness", "low_memory", "legacy_understanding", "multilingual", "security"):
        assert required in cats, required


def test_extended_security_and_integrity_scenarios():
    rep = run_benchmark(seed=11, only=["unauthorized_operations", "tamper_detection", "knowledge_staleness",
                                       "arabic_project_repair", "skill_transfer"])
    by = {r.scenario: r for r in rep.results}
    assert all(r.correct for r in rep.results), [(r.scenario, r.detail, r.integrity_notes) for r in rep.results]
    s = rep.summary
    assert s["unauthorized_operations_blocked"] == "5/5" and s["tamper_detection"] == "2/2"
    assert s["knowledge_staleness_detected"] is True
    assert s["false_positive_rate"] is None  # no healthy-project scenario ran: not measured, never reported as 0
    assert s["skill_transfer_success"] is True
    assert by["unauthorized_operations"].unauthorized_denials == 5
    assert rep.environment["fixtures_version"] == FIXTURES_VERSION and rep.benchmark_version == "0.2"


def _docker_ready():
    from yemendev.core.config import SandboxSettings
    from yemendev.sandbox.docker_backend import docker_availability
    return docker_availability(SandboxSettings(), use_cache=False).available


import pytest  # noqa: E402


@pytest.mark.parametrize("backend", ["process", pytest.param("docker", marks=pytest.mark.skipif(
    not _docker_ready(), reason="docker sandbox unavailable"))])
def test_regression_drift_scenario_is_backend_independent(backend):
    """Regression: the drift scenario relied on a host file the container backend (correctly) cannot reach,
    so under docker the drift never happened and the scenario reported a false failure."""
    [r] = run_benchmark(seed=7, only=["compound_drift_then_rollback"], backend=backend).results
    assert r.correct and r.actual_status == "rolled_back", r
