"""Run a project's pytest suite inside a Workspace and parse the JUnit XML report.

The result is derived from the XML report, not from the exit code alone. If the report
is missing or malformed the run is ``RUNNER_ERROR`` - never reported as success.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 - see _parse_junit for hardening notes
from enum import StrEnum

from pydantic import BaseModel, Field

from yemendev.sandbox.workspace import ExecutionResult, Workspace

REPORT_NAME = ".ydv-junit.xml"
MAX_REPORT_BYTES = 20_000_000


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    ALL_PASSED = "all_passed"
    TESTS_FAILED = "tests_failed"
    NO_TESTS = "no_tests"
    COLLECTION_ERROR = "collection_error"
    TIMED_OUT = "timed_out"
    RUNNER_ERROR = "runner_error"


class TestCaseResult(BaseModel):
    id: str
    outcome: Outcome
    message: str | None = None
    details: str | None = None
    duration_seconds: float = 0.0


class TestRunReport(BaseModel):
    status: RunStatus
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    cases: list[TestCaseResult] = Field(default_factory=list)
    execution: ExecutionResult
    note: str | None = None

    def outcomes(self) -> dict[str, Outcome]:
        return {c.id: c.outcome for c in self.cases}

    def failing_ids(self) -> list[str]:
        return [c.id for c in self.cases if c.outcome in (Outcome.FAILED, Outcome.ERROR)]


def _case_id(tc: ET.Element) -> str:
    cls = tc.get("classname", "")
    name = tc.get("name", "")
    return f"{cls}::{name}" if cls else name


def _parse_junit(data: bytes) -> list[TestCaseResult]:
    # The report is written by pytest inside the sandbox, so it is attacker-influenced.
    # Hardening: size cap; reject DTDs/entities outright (blocks entity-expansion attacks
    # regardless of the expat version); stdlib ElementTree does not fetch external entities.
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("junit report too large")
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise ValueError("junit report contains a DTD/entity declaration")
    root = ET.fromstring(data)  # noqa: S314
    cases = []
    for tc in root.iter("testcase"):
        outcome, msg, details = Outcome.PASSED, None, None
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": Outcome.FAILED, "error": Outcome.ERROR, "skipped": Outcome.SKIPPED}[child.tag]
                msg = (child.get("message") or "")[:4000] or None
                details = (child.text or "")[:20000] or None
                break
        try:
            dur = float(tc.get("time", "0") or 0)
        except ValueError:
            dur = 0.0
        cases.append(TestCaseResult(id=_case_id(tc), outcome=outcome, message=msg, details=details,
                                    duration_seconds=dur))
    return cases


def run_pytest(ws: Workspace, extra_args: list[str] | None = None, timeout: float | None = None) -> TestRunReport:
    report_path = ws.root / REPORT_NAME
    report_path.unlink(missing_ok=True)
    argv = [ws.settings.python_executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={REPORT_NAME}", "-o", "junit_family=xunit2", *(extra_args or [])]
    ex = ws.run(argv, timeout=timeout)
    if ex.timed_out:
        return TestRunReport(status=RunStatus.TIMED_OUT, execution=ex, note="test run exceeded wall-clock timeout")
    if not report_path.is_file() or report_path.is_symlink():
        note = "pytest produced no report"
        if "No module named pytest" in ex.stderr:
            note = "pytest is not installed in the sandbox interpreter"
        return TestRunReport(status=RunStatus.RUNNER_ERROR, execution=ex, note=note)
    try:
        cases = _parse_junit(report_path.read_bytes())
    except (ET.ParseError, ValueError) as exc:
        return TestRunReport(status=RunStatus.RUNNER_ERROR, execution=ex, note=f"invalid junit report: {exc}")
    finally:
        report_path.unlink(missing_ok=True)
    counts = {o: sum(1 for c in cases if c.outcome is o) for o in Outcome}
    # pytest exit codes: 0 ok, 1 failures, 2 interrupted (often collection errors), 5 no tests
    if ex.returncode == 5 or not cases:
        status = RunStatus.NO_TESTS if ex.returncode in (0, 5) else RunStatus.COLLECTION_ERROR
    elif ex.returncode == 2 or (counts[Outcome.ERROR] and any(c.id.startswith("::") or "collect" in (c.message or "")
                                                                    for c in cases if c.outcome is Outcome.ERROR)):
        status = RunStatus.COLLECTION_ERROR
    elif counts[Outcome.FAILED] or counts[Outcome.ERROR]:
        status = RunStatus.TESTS_FAILED
    elif ex.returncode == 0:
        status = RunStatus.ALL_PASSED
    else:
        status = RunStatus.RUNNER_ERROR
    return TestRunReport(
        status=status, total=len(cases), passed=counts[Outcome.PASSED], failed=counts[Outcome.FAILED],
        errors=counts[Outcome.ERROR], skipped=counts[Outcome.SKIPPED], cases=cases, execution=ex,
        note=None if status is not RunStatus.RUNNER_ERROR else f"unexpected pytest exit code {ex.returncode}",
    )
