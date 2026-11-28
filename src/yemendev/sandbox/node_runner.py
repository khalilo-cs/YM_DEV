"""Run a JavaScript/TypeScript project's ``node:test`` suite inside a Workspace (stage 22).

Node's built-in runner (``node --test``) needs no third-party packages and runs TypeScript directly (type
stripping, Node >= 22.18). A small reporter written by the platform emits one JSON object per test; the JUnit
reporter is not used because it drops the error of a test file that fails to load (e.g. an import of a renamed
export), which is exactly what diagnosis needs.

As with pytest, the result comes from the report, never from the exit code alone: a missing or malformed report
is ``RUNNER_ERROR``. Other runners (vitest, jest, mocha, ...) are recognised and reported as not supported
rather than guessed at. Dependencies are never installed: ``node_modules`` must already be in the project, and the
sandbox has no network.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from yemendev.sandbox.testrunner import Outcome, RunStatus, TestCaseResult, TestRunReport
from yemendev.sandbox.workspace import ExecutionResult, Workspace

REPORTER_NAME = ".ydv-node-reporter.mjs"
REPORT_NAME = ".ydv-node-report.jsonl"
MAX_REPORT_BYTES = 20_000_000
MAX_CASES = 50_000
OTHER_RUNNERS = ("vitest", "jest", "mocha", "ava", "tap", "jasmine", "karma", "playwright", "cypress", "uvu")
JS_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
_ERROR_LINE = re.compile(r"^[A-Z]\w*(?:Error|Exception)\b.*$", re.MULTILINE)
_VALUE_FLAGS = {"--import", "--require", "-r", "--loader", "--experimental-loader", "--test-reporter",
                "--test-reporter-destination", "--test-name-pattern", "--test-skip-pattern", "--test-concurrency",
                "--test-timeout", "--test-shard", "--env-file", "--conditions", "-C", "--input-type",
                "--test-coverage-include", "--test-coverage-exclude", "--watch-path", "--test-isolation"}
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./*{},@+-][A-Za-z0-9_./*{},@+ -]*$")

REPORTER = r"""// Written by YEMENDEV for this run only. One JSON object per test result.
const ancestors = new Map();
const stderr = new Map();
const text = (v) => (v === undefined || v === null ? null : String(v));
// stderr events name the file relative to the cwd, result events absolutely: normalise both
const key = (f) => (f && f.startsWith(process.cwd() + "/") ? f.slice(process.cwd().length + 1) : f);
export default async function* reporter(source) {
  for await (const ev of source) {
    const d = ev.data || {};
    if (ev.type === "test:start") {
      const st = ancestors.get(key(d.file)) || [];
      st.length = d.nesting; st.push(d.name); ancestors.set(key(d.file), st);
    } else if (ev.type === "test:stderr") {
      stderr.set(key(d.file), ((stderr.get(key(d.file)) || "") + d.message).slice(-20000));
    } else if (ev.type === "test:pass" || ev.type === "test:fail") {
      if (d.details && d.details.type === "suite") continue;
      const path = (ancestors.get(key(d.file)) || []).slice(0, d.nesting).concat([d.name]);
      const err = d.details && d.details.error;
      let message = null, details = null, fileLevel = false;
      if (ev.type === "test:fail" && err) {
        const c = err.cause;
        fileLevel = typeof c === "string" && c === "test failed";
        if (c && typeof c === "object") { message = text(c.message ?? c); details = text(c.stack ?? c); }
        else { message = text(c ?? err.message); details = text(err.stack); }
        if (err.failureType === "cancelledByParent") message = "cancelled: " + (message || "");
        if (fileLevel && stderr.get(key(d.file))) details = stderr.get(key(d.file));
      }
      const outcome = ev.type === "test:pass" ? (d.skip || d.todo ? "skipped" : "passed")
        : (d.todo ? "skipped" : "failed");
      yield JSON.stringify({ file: d.file || null, name: path.join(" > "), outcome, message, details, fileLevel,
        duration: (d.details && d.details.duration_ms) || 0 }) + "\n";
    }
  }
}
"""


def _package(root: Path) -> dict[str, object] | None:
    f = root / "package.json"
    if not f.is_file() or f.is_symlink() or f.stat().st_size > 1_000_000:
        return None
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def test_script(root: Path) -> str | None:
    pkg = _package(root)
    scripts = pkg.get("scripts") if pkg else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return script if isinstance(script, str) else None


def _default_test_file(rel: str) -> bool:
    """Node's default ``--test`` discovery patterns (Node 22), for deciding whether a suite exists."""
    p = Path(rel)
    if not p.name.endswith(JS_EXT) or "node_modules" in p.parts:
        return False
    stem = p.name.rsplit(".", 1)[0]
    return (stem in ("test",) or stem.endswith((".test", "-test", "_test")) or stem.startswith("test-")
            or "test" in p.parts[:-1])


def has_js_tests(root: Path) -> bool:
    if _package(root) is None:
        return False
    script = test_script(root)
    if script and "node --test" in script:
        return True
    for f in root.rglob("*"):
        rel = f.relative_to(root).as_posix()
        if "node_modules" in f.parts or any(part.startswith(".") for part in f.relative_to(root).parts):
            continue
        if f.is_file() and _default_test_file(rel):
            return True
    return False


def _runner_args(root: Path) -> tuple[list[str], str | None]:
    """File/glob arguments from ``"test": "node --test <args>"``, or a reason the suite cannot be run."""
    script = test_script(root)
    if not script:
        return [], None
    try:
        words = shlex.split(script)
    except ValueError:
        return [], "package.json test script cannot be parsed"
    for other in OTHER_RUNNERS:
        if any(w == other or w.endswith(f"/{other}") for w in words):
            return [], (f"test runner '{other}' is not supported yet (supported: node:test); its results are not "
                        "reported as passing or failing")
    if "node" in words and "--test" in words:
        tail = words[words.index("--test") + 1:]
        if any(w in ("&&", "||", ";", "|") for w in tail):
            tail = tail[:min(tail.index(w) for w in ("&&", "||", ";", "|") if w in tail)]
        # Only plain relative paths/globs are passed on; options from the script are not (they could
        # change where the report goes or load code into the runner), nor the values of those options.
        out: list[str] = []
        skip = False
        for w in tail:
            if skip:
                skip = False
            elif w.startswith("-"):
                skip = "=" not in w and w in _VALUE_FLAGS
            elif _SAFE_ARG.match(w) and ".." not in w and not w.startswith("/"):
                out.append(w)
        return out, None
    return [], None


def _relative(path: str | None, ws: Workspace) -> str:
    if not path:
        return "?"
    p = path.removeprefix("file://")
    for base in (str(ws.root.resolve()), str(ws.root), "/work"):
        if p.startswith(base + "/"):
            return p[len(base) + 1:]
    return p


def _clean(text: str | None, ws: Workspace, limit: int) -> str | None:
    """Make locations project-relative (``file:///…/project/src/x.js:3`` -> ``src/x.js:3``) for diagnosis."""
    if not text:
        return None
    for base in (str(ws.root.resolve()), str(ws.root), "/work"):
        text = text.replace(f"file://{base}/", "").replace(f"{base}/", "")
    return text[:limit]


def _parse(data: bytes, ws: Workspace) -> list[TestCaseResult]:
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("node test report too large")
    cases: list[TestCaseResult] = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict) or obj.get("outcome") not in ("passed", "failed", "skipped"):
            raise ValueError("unexpected record in node test report")
        rel = _relative(obj.get("file") if isinstance(obj.get("file"), str) else None, ws)
        name = str(obj.get("name") or "")[:500]
        file_level = obj.get("fileLevel") is True
        outcome = {"passed": Outcome.PASSED, "skipped": Outcome.SKIPPED}.get(obj["outcome"], Outcome.FAILED)
        details = _clean(obj.get("details") if isinstance(obj.get("details"), str) else None, ws, 20000)
        message = _clean(obj.get("message") if isinstance(obj.get("message"), str) else None, ws, 4000)
        if file_level:
            outcome = Outcome.ERROR  # the file did not load or crashed outside a test: like a collection error
            first = _ERROR_LINE.search(details or "")
            message = first.group(0)[:4000] if first else message
        try:
            duration = float(obj.get("duration") or 0) / 1000
        except (TypeError, ValueError):
            duration = 0.0
        cases.append(TestCaseResult(
            id=rel if file_level else f"{rel}::{name}", outcome=outcome,
            message=message, details=details, duration_seconds=duration))
        if len(cases) > MAX_CASES:
            raise ValueError("too many test results")
    return cases


def run_node_tests(ws: Workspace, timeout: float | None = None) -> TestRunReport:
    def not_run(note: str) -> TestRunReport:
        return TestRunReport(status=RunStatus.RUNNER_ERROR, note=note, execution=ExecutionResult(
            argv=["node", "--test"], returncode=None, stdout="", stderr="", duration_seconds=0.0, timed_out=False,
            network_isolated=False, run_as_uid=None, limits={}, output_truncated=False, backend=ws.backend))

    args, problem = _runner_args(ws.root)
    if problem is None:
        problem = ws.node_problem()
    if problem:
        return not_run(problem)
    node = ws.node_executable
    if node is None:  # unreachable: node_problem() reports it; explicit for type checking and python -O
        return not_run("Node.js is not available")
    report = ws.root / REPORT_NAME
    reporter = ws.root / REPORTER_NAME
    report.unlink(missing_ok=True)
    ws.write(REPORTER_NAME, REPORTER.encode())
    argv = [node, "--test", "--test-concurrency=1", f"--test-reporter=./{REPORTER_NAME}",
            f"--test-reporter-destination={REPORT_NAME}", *args]
    try:
        ex = ws.run(argv, timeout=timeout, memory_limit="data")
    finally:
        reporter.unlink(missing_ok=True)
    if ex.timed_out:
        report.unlink(missing_ok=True)
        return TestRunReport(status=RunStatus.TIMED_OUT, execution=ex, note="test run exceeded wall-clock timeout")
    if not report.is_file() or report.is_symlink():
        note = "node produced no test report"
        if ws.backend == "docker" and ("executable file not found" in ex.stderr or ex.returncode == 127):
            note = ("node is not available in the sandbox image; rebuild it with "
                    "`yemendev sandbox build-image --with-node`")
        elif ex.returncode is not None and ex.returncode < 0:
            note = f"node was killed by signal {-ex.returncode}"
        return TestRunReport(status=RunStatus.RUNNER_ERROR, execution=ex, note=note)
    try:
        cases = _parse(report.read_bytes(), ws)
    except ValueError as exc:  # includes json.JSONDecodeError
        return TestRunReport(status=RunStatus.RUNNER_ERROR, execution=ex, note=f"invalid node test report: {exc}")
    finally:
        report.unlink(missing_ok=True)
    counts = {o: sum(1 for c in cases if c.outcome is o) for o in Outcome}
    if not cases:
        status = RunStatus.NO_TESTS if ex.returncode == 0 else RunStatus.RUNNER_ERROR
    elif counts[Outcome.ERROR]:
        status = RunStatus.COLLECTION_ERROR
    elif counts[Outcome.FAILED]:
        status = RunStatus.TESTS_FAILED
    elif ex.returncode == 0:
        status = RunStatus.ALL_PASSED
    else:  # every reported test passed, yet node failed: something outside the tests went wrong
        status = RunStatus.RUNNER_ERROR
    notes = [f"node exited with {ex.returncode} although no test failed"] if status is RunStatus.RUNNER_ERROR else []
    if ws.dependency_note and "not copied" in ws.dependency_note:
        notes.append(ws.dependency_note)
    return TestRunReport(
        status=status, total=len(cases), passed=counts[Outcome.PASSED], failed=counts[Outcome.FAILED],
        errors=counts[Outcome.ERROR], skipped=counts[Outcome.SKIPPED], cases=cases, execution=ex,
        note="; ".join(notes) or None)
