from yemendev.benchmarks.fixtures import renamed_api_project
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
    assert any("no comparative baselines" in c for c in rep.caveats)
    assert rep.git_commit is None or len(rep.git_commit) == 40


def test_scenario_catalogue():
    names = [n for n, _, _ in scenarios(full=True)]
    assert len(names) == len(set(names)) == 6 + 9
