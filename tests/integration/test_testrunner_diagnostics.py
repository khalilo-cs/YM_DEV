from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.config import SandboxSettings
from yemendev.diagnostics.engine import Reproducibility, diagnose
from yemendev.sandbox.testrunner import Outcome, RunStatus, _parse_junit, run_pytest
from yemendev.sandbox.workspace import Workspace

S = SandboxSettings(timeout_seconds=60)


def test_passing_project(tmp_path):
    with Workspace(make_passing_project(tmp_path / "p"), S) as ws:
        r = run_pytest(ws)
    assert r.status is RunStatus.ALL_PASSED and r.passed == 1 and r.total == 1


def test_failing_project_and_diagnosis(tmp_path):
    src = make_renamed_api_project(tmp_path / "p")
    runs = []
    for _ in range(2):
        with Workspace(src, S) as ws:
            runs.append(run_pytest(ws))
    r = runs[0]
    assert r.status is RunStatus.TESTS_FAILED and r.passed == 1 and r.failed == 1
    assert r.outcomes()["tests.test_api::test_total"] is Outcome.FAILED
    d = diagnose(runs)
    assert d.reproducible_problem
    [f] = d.failures
    assert f.reproducibility is Reproducibility.REPRODUCIBLE
    assert f.exception_type == "AttributeError"
    assert any(loc.path.endswith("calc/api.py") for loc in f.locations)
    [h] = d.hypotheses
    assert h.category == "api_mismatch" and h.status == "unverified"
    assert h.signals == {"module": "calc.mathutils", "attribute": "add_numbers"}


def test_flaky_failure_not_reported_as_reproducible(tmp_path):
    src = tmp_path / "p"
    write(src, "tests/test_flaky.py",
          "import os\n\ndef test_x():\n    assert not os.path.exists('/tmp/ydv-flaky-' + os.environ.get('RUN',''))\n")
    write(src, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    with Workspace(src, S) as ws:
        a = run_pytest(ws)
    # Fabricate a second run where the test failed, to model intermittent behaviour.
    b = a.model_copy(deep=True)
    for c in b.cases:
        if c.id.endswith("test_x"):
            c.outcome = Outcome.FAILED
            c.message = "AssertionError"
    d = diagnose([a, b])
    assert [f.reproducibility for f in d.failures] == [Reproducibility.FLAKY]
    assert not d.reproducible_problem and d.hypotheses == []


def test_no_tests(tmp_path):
    src = tmp_path / "p"
    write(src, "x.py", "")
    with Workspace(src, S) as ws:
        r = run_pytest(ws)
    assert r.status is RunStatus.NO_TESTS
    assert not diagnose([r]).reproducible_problem


def test_collection_error(tmp_path):
    src = tmp_path / "p"
    write(src, "tests/test_bad.py", "import does_not_exist_xyz\n\ndef test_a():\n    pass\n")
    with Workspace(src, S) as ws:
        r = run_pytest(ws)
    assert r.status is RunStatus.COLLECTION_ERROR


def test_timeout_is_not_success(tmp_path):
    src = tmp_path / "p"
    write(src, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    with Workspace(src, S) as ws:
        r = run_pytest(ws, timeout=3)
    assert r.status is RunStatus.TIMED_OUT
    assert not diagnose([r]).reproducible_problem


def test_runner_error_when_pytest_missing(tmp_path):
    s = SandboxSettings(backend="process", python_executable="/usr/bin/false")
    with Workspace(make_passing_project(tmp_path / "p"), s) as ws:
        r = run_pytest(ws)
    assert r.status is RunStatus.RUNNER_ERROR


def test_hostile_junit_with_entities_rejected():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b'<testsuite><testcase name="x">&b;</testcase></testsuite>')
    import pytest
    with pytest.raises(ValueError):
        _parse_junit(bomb)


def test_project_that_forges_a_passing_report_is_not_trusted(tmp_path):
    """A malicious conftest writes a fake all-pass report and exits 0 without running tests."""
    src = tmp_path / "p"
    write(src, "tests/test_real.py", "def test_real():\n    assert False\n")
    write(src, "conftest.py",
          "import os, sys\n"
          "def pytest_configure(config):\n"
          "    open('.ydv-junit.xml','w').write('<testsuite><testcase classname=\"t\" name=\"fake\"/></testsuite>')\n"
          "    os._exit(0)\n")
    with Workspace(src, S) as ws:
        r = run_pytest(ws)
    # Known limitation: code running inside the sandbox controls the report.
    # This test documents current behaviour so a future fix is noticed.
    assert r.status is RunStatus.ALL_PASSED and [c.id for c in r.cases] == ["t::fake"]
