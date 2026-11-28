"""Stage 22 units: JS/TS test-file recognition, JS diagnosis rules, Node in the sandbox image."""

import shutil

import pytest

from yemendev.core.contracts import InvalidInputError
from yemendev.diagnostics.engine import _analyse, diagnose
from yemendev.learning.skills import is_test_path
from yemendev.sandbox import testrunner as tr
from yemendev.sandbox.image import DOCKERFILE, NODE_STEP, _node_binary
from yemendev.sandbox.node_runner import _default_test_file
from yemendev.sandbox.workspace import ExecutionResult

EX = ExecutionResult(argv=["node"], returncode=1, stdout="", stderr="", duration_seconds=0, timed_out=False,
                     network_isolated=True, run_as_uid=None, limits={}, output_truncated=False)


@pytest.mark.parametrize("path", ["src/service.test.js", "src/service.spec.ts", "lib/x.test.mjs", "a/b.spec.tsx",
                                  "src/__tests__/x.js", "test.js", "test-utils.cjs", "src/x_test.ts", "x-test.js",
                                  "tests/test_api.py", "pkg/test_m.py", "conftest.py"])
def test_test_files_are_recognised_so_repairs_never_edit_them(path):
    """Regression: co-located JS/TS tests (``*.test.ts``, ``*.spec.js``, ``__tests__/``) were treated as
    source, so a model-proposed plan could have edited a test to make it pass."""
    assert is_test_path(path)


@pytest.mark.parametrize("path", ["src/service.js", "src/testing.ts", "src/contest.js", "src/latest.js",
                                  "calc/api.py", "src/spectrum.ts", "src/protest.mjs"])
def test_source_files_are_not_mistaken_for_tests(path):
    assert not is_test_path(path)


@pytest.mark.parametrize("path,expected", [("test/a.mjs", True), ("src/a.test.cjs", True), ("a-test.ts", True),
                                           ("test.js", True), ("src/a.js", False), ("node_modules/x/test/a.js", False),
                                           ("test/readme.md", False)])
def test_default_node_discovery(path, expected):
    assert _default_test_file(path) is expected


@pytest.mark.parametrize("text,etype,msg,locs", [
    ("TypeError: core.add is not a function\n    at total (src/service.js:4:28)\n"
     "    at TestContext.<anonymous> (test/service.test.js:6:30)\n    at node:internal/test_runner/test:935:25",
     "TypeError", "core.add is not a function", [("src/service.js", 4), ("test/service.test.js", 6)]),
    ("ReferenceError: total is not defined\n    at file.ts (src/a.ts:2:1)", "ReferenceError",
     "total is not defined", [("src/a.ts", 2)]),
    ("AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n12 !== 8\n\n"
     "    at TestContext.<anonymous> (test/x.test.mjs:7:3)", "AssertionError",
     "Expected values to be strictly equal:", [("test/x.test.mjs", 7)]),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'tiny-sum' imported from test/dep.test.mjs\n"
     "    at node_modules/foo/index.js:1:1\n    at /usr/lib/node/x.js:3:3", "Error",
     "Cannot find package 'tiny-sum' imported from test/dep.test.mjs", []),
])
def test_js_errors_and_project_locations_are_parsed(text, etype, msg, locs):
    et, em, ls = _analyse(text)
    assert (et, em) == (etype, msg)
    assert [(x.path, x.line) for x in ls] == locs  # runtime internals, dependencies and absolute paths excluded


def _run(cases):
    return tr.TestRunReport(status=tr.RunStatus.TESTS_FAILED, execution=EX,
                            cases=[tr.TestCaseResult(id=i, outcome=tr.Outcome.FAILED, message=m, details=d)
                                   for i, m, d in cases])


def test_js_hypotheses():
    r = _run([("t::a", "core.add is not a function", "TypeError: core.add is not a function\n at x (src/s.js:1:1)"),
              ("t::b", "total is not defined", "ReferenceError: total is not defined"),
              ("t::c", "Cannot find module 'left-pad'", "Error: Cannot find module 'left-pad'")])
    hyps = {h.category: h for h in diagnose([r, r]).hypotheses}
    assert hyps["api_mismatch"].signals == {"object": "core", "attribute": "add", "language": "javascript"}
    assert hyps["undefined_name"].signals["name"] == "total"
    assert hyps["missing_dependency"].signals["module"] == "left-pad"


def test_python_diagnosis_is_unchanged_by_js_rules():
    r = _run([("tests.test_api::test_total", "AttributeError: module 'calc.mathutils' has no attribute 'add_numbers'",
               "calc/api.py:7: in total\n    t = mathutils.add_numbers(t, x)\n"
               "E   AttributeError: module 'calc.mathutils' has no attribute 'add_numbers'")])
    [f] = diagnose([r, r]).failures
    assert f.exception_type == "AttributeError" and [(x.path, x.line) for x in f.locations] == [("calc/api.py", 7)]
    [h] = diagnose([r, r]).hypotheses
    assert h.signals == {"module": "calc.mathutils", "attribute": "add_numbers"}


def test_image_node_step_and_validation(tmp_path):
    df = DOCKERFILE.format(base="python:3.13-slim", pytest="9.2.0", node=NODE_STEP.format(version="v22.1.0"))
    assert "COPY node /usr/local/bin/node" in df and 'org.yemendev.node="v22.1.0"' in df
    assert df.index("COPY node") < df.index("USER 65534:65534")  # installed as root, run as nobody
    assert "COPY node" not in DOCKERFILE.format(base="b", pytest="1", node="")
    fake = tmp_path / "node"
    fake.write_text("#!/bin/sh\necho hello\n")
    fake.chmod(0o755)
    with pytest.raises(InvalidInputError, match="does not look like"):
        _node_binary(fake)
    fake.chmod(0o644)
    with pytest.raises(InvalidInputError, match="not an executable"):
        _node_binary(fake)
    if shutil.which("node"):
        assert _node_binary(type(tmp_path)(shutil.which("node"))).startswith("v")
