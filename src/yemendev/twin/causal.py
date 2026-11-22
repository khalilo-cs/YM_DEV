"""Causal analysis foundation (directive 5.5).

Static import structure + observed failure locations. Every output is a *candidate* with its certainty;
static reachability shows where a failure CAN propagate, not that it did (correlation != causation).
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel

from yemendev.diagnostics.engine import DiagnosticReport
from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import ProjectSnapshot


class PropagationPath(BaseModel):
    module: str
    path: list[str]  # failing module -> ... -> impacted module (import chain, reversed)
    is_test: bool = False


class RootCauseCandidate(BaseModel):
    module: str
    certainty: str  # observed (in a traceback of a reproducible failure) | inferred (dependency of one)
    supporting_tests: list[str]
    reason: str
    status: str = "candidate, not proven"


def failure_propagation(g: DependencyGraph, module: str, tests: set[str] | None = None) -> list[PropagationPath]:
    """Modules that would be affected if ``module`` broke, with the shortest import chain to each."""
    parent: dict[str, str] = {}
    q = deque([module])
    seen = {module}
    while q:
        cur = q.popleft()
        for dep in sorted(g.inc.get(cur, ())):
            if dep not in seen:
                seen.add(dep)
                parent[dep] = cur
                q.append(dep)
    out = []
    for m in sorted(seen - {module}):
        path = [m]
        while path[-1] != module:
            path.append(parent[path[-1]])
        out.append(PropagationPath(module=m, path=list(reversed(path)), is_test=m in (tests or set())))
    return out


def root_cause_candidates(snap: ProjectSnapshot, diag: DiagnosticReport) -> list[RootCauseCandidate]:
    by_path = {m.path: m.name for m in snap.modules if not m.is_test}
    g = DependencyGraph.from_snapshot(snap)
    observed: dict[str, list[str]] = {}
    for f in diag.failures:
        if f.reproducibility.value != "reproducible":
            continue
        for loc in f.locations:
            for path, mod in by_path.items():
                if loc.path == path or loc.path.endswith("/" + path):
                    observed.setdefault(mod, [])
                    if f.test_id not in observed[mod]:
                        observed[mod].append(f.test_id)
    out = [RootCauseCandidate(module=m, certainty="observed", supporting_tests=t,
                              reason="appears in the traceback of a reproducible failure")
           for m, t in sorted(observed.items())]
    inferred: dict[str, list[str]] = {}
    for m, tests in observed.items():
        for dep in sorted(g.out.get(m, ())):
            if dep not in observed:
                inferred.setdefault(dep, [])
                inferred[dep].extend(x for x in tests if x not in inferred[dep])
    out += [RootCauseCandidate(module=m, certainty="inferred", supporting_tests=t,
                               reason="imported by a module in the failing traceback")
            for m, t in sorted(inferred.items())]
    return out


def single_points_of_failure(g: DependencyGraph, snap: ProjectSnapshot, threshold: float = 0.5) -> list[str]:
    """Non-test modules that at least ``threshold`` of all other non-test modules transitively depend on."""
    non_test = {m.name for m in snap.modules if not m.is_test}
    if len(non_test) < 3:
        return []
    counts = g.transitive_dependent_counts()
    return sorted(m for m in non_test
                  if len(g.impacted_by(m) & non_test) >= threshold * (len(non_test) - 1) and counts.get(m, 0))
