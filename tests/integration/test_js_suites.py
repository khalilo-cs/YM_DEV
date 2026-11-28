"""Stage 22: JavaScript/TypeScript suites (node:test) run, diagnosed and repaired - on both sandbox backends.

Real Node.js, real sandbox (uid drop + rlimits, or a container), real files. Nothing is mocked.
"""

import json
import shutil

import pytest
from end_to_end.conftest import ALICE, BOB
from helpers import make_js_renamed_export_project, make_renamed_api_project, write

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import sha256_bytes
from yemendev.diagnostics.engine import diagnose
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.sandbox.docker_backend import docker_availability
from yemendev.sandbox.node_runner import run_node_tests
from yemendev.sandbox.suites import detect_suites, run_tests
from yemendev.sandbox.testrunner import Outcome, RunStatus
from yemendev.sandbox.workspace import Workspace

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def _docker_node():
    s = SandboxSettings(backend="docker")
    a = docker_availability(s, use_cache=False)
    return a.available


BACKENDS = ["process", pytest.param("docker", marks=pytest.mark.skipif(
    not _docker_node(), reason="docker sandbox unavailable"))]


def sandbox(backend, **kw):
    return SandboxSettings(backend=backend, timeout_seconds=90, **kw)


def run(root, backend, **kw):
    with Workspace(root, sandbox(backend, **kw)) as ws:
        return run_tests(ws)


def fixed_service(root):
    svc = root / "src/service.js"
    return svc.read_text().replace("import { add, scale }", "import { addNumbers as add, scale }")


@pytest.mark.parametrize("backend", BACKENDS)
def test_load_error_is_reported_with_its_real_cause_and_location(tmp_path, backend):
    root = make_js_renamed_export_project(tmp_path / "calc")
    assert detect_suites(root) == ["node"]
    r1, r2 = run(root, backend), run(root, backend)
    assert r1.status is RunStatus.COLLECTION_ERROR
    by = {c.id: c for c in r1.cases}
    assert by["test/service.test.js"].outcome is Outcome.ERROR
    assert "does not provide an export named 'add'" in by["test/service.test.js"].message
    assert by["test/util.test.ts::typescript runs"].outcome is Outcome.PASSED  # TypeScript, no build step
    assert r1.execution.run_as_uid not in (None, 0) and r1.execution.backend == backend
    d = diagnose([r1, r2])
    [f] = d.failures
    assert f.exception_type == "SyntaxError" and [(x.path, x.line) for x in f.locations] == [("src/service.js", 1)]
    [h] = d.hypotheses
    assert h.category == "api_mismatch" and h.signals == {"module": "./core.js", "attribute": "add",
                                                          "language": "javascript"}
    assert d.reproducible_problem


@pytest.mark.parametrize("backend", BACKENDS)
def test_results_per_test_including_nested_skipped_and_assertion_failures(tmp_path, backend):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root).replace("return scale(x, 2);", "return scale(x, 3);"))
    write(root, "test/skip.test.mjs", 'import { test } from "node:test";\n'
                                      'test("later", { skip: "not yet" }, () => {});\n')
    r = run(root, backend)
    assert r.status is RunStatus.TESTS_FAILED and (r.passed, r.failed, r.skipped) == (2, 1, 1)
    by = {c.id: c for c in r.cases}
    bad = by["test/service.test.js::service > doubled"]
    assert bad.outcome is Outcome.FAILED and "12" in bad.message and "8" in bad.message
    assert "test/service.test.js:" in bad.details  # stack made project-relative
    assert by["test/skip.test.mjs::later"].outcome is Outcome.SKIPPED
    [f] = diagnose([r, r]).failures
    assert f.exception_type == "AssertionError"
    assert ("test/service.test.js", 7) in [(x.path, x.line) for x in f.locations]


@pytest.mark.parametrize("backend", BACKENDS)
def test_js_repair_end_to_end_with_approval_and_verification(tmp_path, backend):
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=sandbox(backend)))
    root = make_js_renamed_export_project(tmp_path / "calc")
    pid = p.register_project(ALICE, "calc", str(root)).id
    svc = root / "src/service.js"
    plan = RepairPlan(id="pln_js", description="use the renamed export", source=PlanSource.HUMAN, source_ref="h",
                      patches=[FilePatch(path="src/service.js", original_sha256=sha256_bytes(svc.read_bytes()),
                                         new_content=fixed_service(root))])
    wf = RepairWorkflow(p)
    t = wf.create(ALICE, pid, human_plan=plan)
    assert wf.advance(ALICE, t.id).task.status.value == "waiting_approval"
    [apr] = [a for a in p.approvals.for_task(t.id) if a.status.value == "pending"]
    p.approvals.decide(apr.id, BOB, approve=True)
    assert wf.advance(ALICE, t.id).task.status.value == "succeeded"
    assert svc.read_text() == fixed_service(root)
    actions = [e.action for e in p.evidence.list(task_id=t.id, limit=100)]
    for a in ("tests.baseline", "repair.simulate", "repair.execute", "repair.verify"):
        assert a in actions
    sim = next(e for e in p.evidence.list(task_id=t.id, limit=100) if e.action == "repair.simulate")
    cmp = sim.body.tests["comparison"]
    assert cmp["fixed"] == ["test/service.test.js"] and not cmp["removed"]  # the container entry counts as fixed
    assert p.verify_integrity()["valid"]


def test_a_js_plan_that_regresses_is_rejected_and_the_project_is_untouched(tmp_path):
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=sandbox("process")))
    root = make_js_renamed_export_project(tmp_path / "calc")
    pid = p.register_project(ALICE, "calc", str(root)).id
    svc = root / "src/service.js"
    before = svc.read_bytes()
    bad = fixed_service(root).replace("return scale(x, 2);", "return scale(x + 1, 2);")
    plan = RepairPlan(id="pln_bad", description="fixes the import, breaks doubled()", source=PlanSource.HUMAN,
                      source_ref="h", patches=[FilePatch(path="src/service.js", original_sha256=sha256_bytes(before),
                                                         new_content=bad)])
    wf = RepairWorkflow(p)
    t = wf.create(ALICE, pid, human_plan=plan)
    r = wf.advance(ALICE, t.id)
    assert r.task.status.value == "no_action" and svc.read_bytes() == before
    assert not p.approvals.for_task(t.id)


def test_a_suite_that_kills_the_runner_is_never_reported_as_passing(tmp_path):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root))
    write(root, "test/zz.test.mjs", 'import { test } from "node:test";\n'
                                    'test("kill", () => { process.kill(process.ppid, "SIGKILL"); });\n')
    r = run(root, "process")
    assert r.status is RunStatus.RUNNER_ERROR, (r.status, r.note)


def test_a_tampered_report_is_a_runner_error_not_a_pass(tmp_path):
    """The report is written inside the sandbox (attacker-influenced, like pytest's JUnit file - A-12): a test
    that overwrites it with something malformed makes the run a runner error, never a pass. (A suite that
    writes a well-formed passing report cannot be told apart; that limitation is documented.)"""
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root))
    write(root, "test/zz.test.mjs", 'import { test } from "node:test";\nimport fs from "node:fs";\n'
                                    'test("forge", () => { fs.writeFileSync(".ydv-node-report.jsonl", '
                                    '"{\\"outcome\\": \\"yes\\"}\\n"); });\n')
    r = run(root, "process")
    assert r.status is RunStatus.RUNNER_ERROR and "invalid node test report" in r.note


def test_node_memory_is_limited_but_typescript_still_runs_under_a_small_limit(tmp_path):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root))
    assert run(root, "process", max_memory_mb=512).status is RunStatus.ALL_PASSED  # V8/Wasm start fine
    write(root, "test/hog.test.mjs", 'import { test } from "node:test";\n'
                                     'test("hog", () => { const a = []; for (let i = 0; i < 30; i++) '
                                     "a.push(Buffer.alloc(100 * 1024 * 1024, 1)); });\n")
    r = run(root, "process", max_memory_mb=512)
    assert r.status is not RunStatus.ALL_PASSED and "test/hog.test.mjs::hog" not in [
        c.id for c in r.cases if c.outcome is Outcome.PASSED]


def test_dependencies_come_from_the_projects_own_node_modules_never_installed(tmp_path):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root))
    write(root, "node_modules/tiny-sum/package.json", '{"name": "tiny-sum", "type": "module", "main": "index.js"}\n')
    write(root, "node_modules/tiny-sum/index.js", "export default (xs) => xs.reduce((a, b) => a + b, 0);\n")
    write(root, "test/dep.test.mjs", 'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
                                     'import sum from "tiny-sum";\ntest("dep", () => assert.equal(sum([1, 2]), 3));\n')
    assert run(root, "process").status is RunStatus.ALL_PASSED
    r = run(root, "process", max_node_modules_mb=0)
    assert r.status is RunStatus.COLLECTION_ERROR and "not copied" in r.note
    d = diagnose([r, r])
    assert any(h.category == "missing_dependency" and h.signals["module"] == "tiny-sum" for h in d.hypotheses)


@pytest.mark.parametrize("script,needle", [("vitest run", "vitest"), ("jest --ci", "jest"),
                                           ("mocha test/", "mocha")])
def test_other_runners_are_reported_as_unsupported_not_guessed(tmp_path, script, needle):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "package.json", json.dumps({"name": "calc", "type": "module", "scripts": {"test": script}}))
    r = run(root, "process")
    assert r.status is RunStatus.RUNNER_ERROR and needle in r.note and "not supported" in r.note


def test_missing_node_is_a_reason_not_a_result(tmp_path):
    root = make_js_renamed_export_project(tmp_path / "calc")
    with Workspace(root, SandboxSettings(backend="process", node_executable=None)) as ws:
        r = run_node_tests(ws)
    assert r.status is RunStatus.RUNNER_ERROR and "Node.js is not available" in r.note


def test_script_options_are_not_passed_on_only_plain_paths(tmp_path):
    root = make_js_renamed_export_project(tmp_path / "calc")
    write(root, "src/service.js", fixed_service(root))
    write(root, "package.json", json.dumps({"name": "calc", "type": "module", "scripts": {
        "test": "node --test --test-reporter-destination=/tmp/x --import ./evil.mjs test/util.test.ts && echo hi"}}))
    write(root, "evil.mjs", 'throw new Error("evil preload ran");\n')
    r = run(root, "process")
    assert r.status is RunStatus.ALL_PASSED and [c.id for c in r.cases] == ["test/util.test.ts::typescript runs"]


def test_mixed_project_runs_both_suites_and_a_failing_suite_fails_the_whole(tmp_path):
    root = make_renamed_api_project(tmp_path / "mixed")  # Python suite fails (renamed API)
    js = make_js_renamed_export_project(tmp_path / "js")
    for f in js.rglob("*"):
        if f.is_file():
            write(root, f.relative_to(js).as_posix(), f.read_text())
    write(root, "src/service.js", fixed_service(js))  # JS suite passes
    assert detect_suites(root) == ["pytest", "node"]
    r = run(root, "process")
    assert r.status is RunStatus.TESTS_FAILED and "pytest: tests_failed" in r.note and "node: all_passed" in r.note
    assert any(c.id.startswith("test/") for c in r.cases) and any("::test_" in c.id for c in r.cases)


def test_python_projects_are_unaffected(tmp_path):
    root = make_renamed_api_project(tmp_path / "py")
    write(root, "package.json", '{"name": "docs-only"}\n')  # a package.json without JS tests changes nothing
    assert detect_suites(root) == ["pytest"]
