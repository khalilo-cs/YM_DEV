"""Diagnostic Engine.

Turns test-run reports into a diagnostic report:
* which tests fail, and whether failures **reproduce** across independent runs
  (a failure that does not reproduce is classified as flaky, not as a defect);
* the exception type/message and source locations parsed from pytest output;
* rule-based root-cause **hypotheses**. Every hypothesis is ``unverified``: it is an
  explanation consistent with the evidence, not a proven cause. A hypothesis becomes
  supported only when a repair based on it is verified (recorded by the recovery engine).
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from yemendev.sandbox.testrunner import Outcome, RunStatus, TestRunReport

_LOCATION = re.compile(r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+):", re.MULTILINE)
_EXC_LINE = re.compile(r"^E\s+(?P<type>[A-Za-z_][\w.]*(Error|Exception|Warning|Exit))(?::\s*(?P<msg>.*))?$",
                       re.MULTILINE)
_MODULE_ATTR = re.compile(r"module '(?P<mod>[\w.]+)' has no attribute '(?P<attr>\w+)'")
_OBJ_ATTR = re.compile(r"'(?P<obj>\w+)' object has no attribute '(?P<attr>\w+)'")
_NO_MODULE = re.compile(r"No module named '(?P<mod>[\w.]+)'")
_NAME_ERR = re.compile(r"name '(?P<name>\w+)' is not defined")
_CANNOT_IMPORT = re.compile(r"cannot import name '(?P<name>\w+)' from '(?P<mod>[\w.]+)'")


class Reproducibility(StrEnum):
    REPRODUCIBLE = "reproducible"   # failed in every run
    FLAKY = "flaky"                 # failed in some runs only
    NOT_OBSERVED = "not_observed"


class Location(BaseModel):
    path: str
    line: int


class FailureAnalysis(BaseModel):
    test_id: str
    outcome: Outcome
    reproducibility: Reproducibility
    exception_type: str | None = None
    exception_message: str | None = None
    locations: list[Location] = Field(default_factory=list)


class Hypothesis(BaseModel):
    id: str
    category: str
    statement: str
    supporting_tests: list[str]
    signals: dict[str, str] = Field(default_factory=dict)
    status: str = "unverified"
    confidence: float = Field(ge=0, le=1)


class DiagnosticReport(BaseModel):
    run_status: RunStatus
    runs_compared: int
    failures: list[FailureAnalysis]
    hypotheses: list[Hypothesis]
    reproducible_problem: bool
    summary: str
    limitations: list[str]


def _analyse(case_text: str) -> tuple[str | None, str | None, list[Location]]:
    exc_type = exc_msg = None
    matches = list(_EXC_LINE.finditer(case_text))
    if matches:
        m = matches[-1]
        exc_type = m.group("type").split(".")[-1]
        exc_msg = (m.group("msg") or "").strip()[:1000] or None
    locs: list[Location] = []
    for m in _LOCATION.finditer(case_text):
        loc = Location(path=m.group("path").strip(), line=int(m.group("line")))
        if loc not in locs:
            locs.append(loc)
    return exc_type, exc_msg, locs[:20]


def _hypothesize(failures: list[FailureAnalysis]) -> list[Hypothesis]:
    groups: dict[tuple[str, str], Hypothesis] = {}

    def add(category: str, key: str, statement: str, test: str, conf: float, **signals: str) -> None:
        h = groups.get((category, key))
        if h is None:
            h = Hypothesis(id=f"H{len(groups) + 1}", category=category, statement=statement,
                           supporting_tests=[], signals=signals, confidence=conf)
            groups[(category, key)] = h
        if test not in h.supporting_tests:
            h.supporting_tests.append(test)

    for f in failures:
        if f.reproducibility is not Reproducibility.REPRODUCIBLE:
            continue
        msg = f.exception_message or ""
        et = f.exception_type or ""
        if (m := _MODULE_ATTR.search(msg)) and et == "AttributeError":
            add("api_mismatch", f"{m['mod']}.{m['attr']}",
                f"Code references '{m['attr']}' on module '{m['mod']}', which does not define it "
                "(renamed/removed API or typo).", f.test_id, 0.7, module=m["mod"], attribute=m["attr"])
        elif (m := _OBJ_ATTR.search(msg)) and et == "AttributeError":
            add("api_mismatch", f"{m['obj']}.{m['attr']}",
                f"Code accesses attribute '{m['attr']}' that objects of type '{m['obj']}' do not have.",
                f.test_id, 0.6, object_type=m["obj"], attribute=m["attr"])
        elif (m := _CANNOT_IMPORT.search(msg)):
            add("api_mismatch", f"{m['mod']}.{m['name']}",
                f"'{m['name']}' cannot be imported from '{m['mod']}' (renamed/removed or circular import).",
                f.test_id, 0.6, module=m["mod"], attribute=m["name"])
        elif (m := _NO_MODULE.search(msg)):
            add("missing_dependency", m["mod"],
                f"Module '{m['mod']}' is not importable: missing dependency in the environment or wrong module path.",
                f.test_id, 0.7, module=m["mod"])
        elif (m := _NAME_ERR.search(msg)):
            add("undefined_name", m["name"], f"Name '{m['name']}' is used but never defined or imported.",
                f.test_id, 0.7, name=m["name"])
        elif et == "AssertionError":
            add("behavioral_regression", f.test_id,
                "Code returns a value that differs from the test's expectation (logic defect, or the test "
                "expectation is outdated).", f.test_id, 0.4)
        elif et:
            add("runtime_exception", et, f"Unhandled {et} raised during the test.", f.test_id, 0.4,
                exception_type=et)
    return list(groups.values())


def diagnose(runs: list[TestRunReport]) -> DiagnosticReport:
    if not runs:
        raise ValueError("at least one test run is required")
    limitations = [
        "Hypotheses are rule-based and unverified; they are not proven root causes.",
        "Static pytest output only; no runtime tracing was performed.",
    ]
    last = runs[-1]
    if last.status in (RunStatus.RUNNER_ERROR, RunStatus.TIMED_OUT, RunStatus.NO_TESTS):
        return DiagnosticReport(
            run_status=last.status, runs_compared=len(runs), failures=[], hypotheses=[], reproducible_problem=False,
            summary=f"Cannot diagnose: test run status is {last.status.value}. {last.note or ''}".strip(),
            limitations=limitations,
        )
    failing_sets = [set(r.failing_ids()) for r in runs]
    ever = set().union(*failing_sets)
    always = set.intersection(*failing_sets) if failing_sets else set()
    by_id = {c.id: c for c in last.cases}
    for r in runs:
        for c in r.cases:
            by_id.setdefault(c.id, c)
    failures = []
    for tid in sorted(ever):
        c = by_id[tid]
        et, em, locs = _analyse(f"{c.message or ''}\n{c.details or ''}")
        if em is None and c.message:
            em = c.message[:1000]
        failures.append(FailureAnalysis(
            test_id=tid, outcome=c.outcome,
            reproducibility=Reproducibility.REPRODUCIBLE if tid in always else Reproducibility.FLAKY,
            exception_type=et, exception_message=em, locations=locs,
        ))
    hyps = _hypothesize(failures)
    if len(runs) < 2:
        limitations.append("Only one run: reproducibility not established.")
    flaky = [f for f in failures if f.reproducibility is Reproducibility.FLAKY]
    repro = [f for f in failures if f.reproducibility is Reproducibility.REPRODUCIBLE]
    summary = (f"{len(repro)} reproducible failure(s), {len(flaky)} flaky, across {len(runs)} run(s); "
               f"{len(hyps)} unverified hypothesis(es).")
    return DiagnosticReport(
        run_status=last.status, runs_compared=len(runs), failures=failures, hypotheses=hyps,
        reproducible_problem=bool(repro) and len(runs) >= 2, summary=summary, limitations=limitations,
    )
