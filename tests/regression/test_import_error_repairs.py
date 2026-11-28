"""Regression (stage 22): a correct fix for a test module that failed to *load* was rejected.

When a test file cannot be imported, the runner reports one failing entry for the file (pytest:
``tests.test_api``; node:test: ``test/x.test.js``). After the fix, the file's tests appear as
``<entry>::<test>``. The comparison counted that as "1 test(s) disappeared" and "no previously failing test
now passes", so correct repairs of import-time breakage were refused - in Python as well as JavaScript.
"""

from helpers import write

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import sha256_bytes
from yemendev.recovery.checkpoint import CheckpointStore
from yemendev.recovery.engine import RecoveryEngine, compare
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.sandbox import testrunner as tr
from yemendev.sandbox.suites import run_tests
from yemendev.sandbox.testrunner import Outcome, RunStatus
from yemendev.sandbox.workspace import ExecutionResult, Workspace

EX = ExecutionResult(argv=["x"], returncode=1, stdout="", stderr="", duration_seconds=0, timed_out=False,
                     network_isolated=True, run_as_uid=None, limits={}, output_truncated=False)


def rep(**cases):
    cs = [tr.TestCaseResult(id=k.replace("__", "::").replace("_dot_", "."), outcome=v) for k, v in cases.items()]
    return tr.TestRunReport(status=RunStatus.TESTS_FAILED, cases=cs, execution=EX)


def test_container_entry_is_fixed_only_if_all_its_tests_pass():
    base = rep(tests_dot_test_api=Outcome.ERROR, tests_dot_other__test_x=Outcome.PASSED)
    ok = rep(tests_dot_test_api__test_a=Outcome.PASSED, tests_dot_test_api__test_b=Outcome.PASSED,
             tests_dot_other__test_x=Outcome.PASSED)
    c = compare(base, ok)
    assert c.fixed == ["tests.test_api"] and not c.removed and c.acceptable
    half = rep(tests_dot_test_api__test_a=Outcome.PASSED, tests_dot_test_api__test_b=Outcome.FAILED,
               tests_dot_other__test_x=Outcome.PASSED)
    c = compare(base, half)
    assert not c.fixed and c.still_failing == ["tests.test_api"] and not c.acceptable
    gone = rep(tests_dot_other__test_x=Outcome.PASSED)  # the file's tests vanished entirely
    c = compare(base, gone)
    assert c.removed == ["tests.test_api"] and not c.acceptable


def test_python_import_error_fix_passes_simulation(tmp_path):
    root = tmp_path / "calc"
    write(root, "calc/__init__.py", "")
    write(root, "calc/api.py", "def add(a, b):\n    return a + b\n")
    write(root, "tests/test_api.py", "from calc.api import add_numbers\n\n\ndef test_add():\n"
                                     "    assert add_numbers(1, 2) == 3\n\n\ndef test_add_zero():\n"
                                     "    assert add_numbers(0, 2) == 2\n")
    api = root / "calc/api.py"
    s = SandboxSettings(backend="process", timeout_seconds=60)
    with Workspace(root, s) as ws:
        baseline = run_tests(ws)
    assert baseline.status is RunStatus.COLLECTION_ERROR
    plan = RepairPlan(id="pln_alias", description="restore the old name as an alias", source=PlanSource.HUMAN,
                      source_ref="h", patches=[FilePatch(
                          path="calc/api.py", original_sha256=sha256_bytes(api.read_bytes()),
                          new_content=api.read_text() + "\n\nadd_numbers = add\n")])
    eng = RecoveryEngine(sandbox=s, checkpoints=CheckpointStore(tmp_path / "ckpt"), workspaces_dir=None)
    res = eng.simulate(root, plan, baseline)
    assert res.passed, res.problems
    assert res.comparison.fixed == [baseline.failing_ids()[0]] and not res.comparison.removed
