"""Dependency graph analysis: impact, centrality, cycles, single points of failure.

These are structural (static import) relationships, not runtime causation. Results are
labelled accordingly by the API and reports.
"""

from __future__ import annotations

from collections import defaultdict, deque

from pydantic import BaseModel

from yemendev.twin.model import Edge, EdgeKind, ProjectSnapshot


class NodeMetrics(BaseModel):
    module: str
    direct_dependents: int
    transitive_dependents: int
    direct_dependencies: int


class GraphReport(BaseModel):
    nodes: list[str]
    edges: list[tuple[str, str]]
    cycles: list[list[str]]
    metrics: list[NodeMetrics]
    critical_modules: list[str]
    note: str = "Static import graph. Edges are observed imports; they indicate potential, not proven, impact."


class DependencyGraph:
    def __init__(self, nodes: list[str], edges: list[Edge]) -> None:
        self.nodes = sorted(set(nodes))
        self.out: dict[str, set[str]] = defaultdict(set)
        self.inc: dict[str, set[str]] = defaultdict(set)
        for e in edges:
            if e.kind is EdgeKind.INTERNAL_IMPORT and e.target in self.nodes and e.source in self.nodes:
                self.out[e.source].add(e.target)
                self.inc[e.target].add(e.source)

    @classmethod
    def from_snapshot(cls, snap: ProjectSnapshot) -> DependencyGraph:
        return cls([m.name for m in snap.modules], snap.edges)

    def impacted_by(self, module: str) -> set[str]:
        """Modules that (transitively) import ``module`` - affected if it changes or fails."""
        seen: set[str] = set()
        q = deque([module])
        while q:
            cur = q.popleft()
            for dep in self.inc.get(cur, ()):
                if dep not in seen and dep != module:
                    seen.add(dep)
                    q.append(dep)
        return seen

    def cycles(self) -> list[list[str]]:
        """Strongly connected components with >1 node (Tarjan, iterative)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        result: list[list[str]] = []
        counter = 0
        for start in self.nodes:
            if start in index:
                continue
            work: list[tuple[str, list[str]]] = [(start, sorted(self.out.get(start, ())))]
            index[start] = low[start] = counter
            counter += 1
            stack.append(start)
            on_stack.add(start)
            while work:
                node, children = work[-1]
                if children:
                    child = children.pop()
                    if child not in index:
                        index[child] = low[child] = counter
                        counter += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, sorted(self.out.get(child, ()))))
                    elif child in on_stack:
                        low[node] = min(low[node], index[child])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        result.append(sorted(comp))
        return sorted(result)

    def report(self, top: int = 10) -> GraphReport:
        metrics = [
            NodeMetrics(module=n, direct_dependents=len(self.inc.get(n, ())),
                        transitive_dependents=len(self.impacted_by(n)), direct_dependencies=len(self.out.get(n, ())))
            for n in self.nodes
        ]
        ranked = sorted((m for m in metrics if m.transitive_dependents > 0),
                        key=lambda m: (-m.transitive_dependents, m.module))
        return GraphReport(
            nodes=self.nodes,
            edges=sorted((s, t) for s, ts in self.out.items() for t in ts),
            cycles=self.cycles(),
            metrics=metrics,
            critical_modules=[m.module for m in ranked[:top]],
        )
