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
        node_set = set(self.nodes)  # O(1) membership: JS projects can have thousands of modules
        self.out: dict[str, set[str]] = defaultdict(set)
        self.inc: dict[str, set[str]] = defaultdict(set)
        for e in edges:
            if e.kind is EdgeKind.INTERNAL_IMPORT and e.target in node_set and e.source in node_set:
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
        """Strongly connected components with >1 node."""
        return sorted(sorted(c) for c in self.components() if len(c) > 1)

    def components(self) -> list[list[str]]:
        """All strongly connected components (Tarjan, iterative - no recursion limit)."""
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
                    result.append(comp)
        return result

    def transitive_dependent_counts(self) -> dict[str, int]:
        """|impacted_by(n)| for every node in ~O(V + E) big-int bitset operations (not V BFS runs).

        Regression (audit): computing this with one BFS per node was quadratic.
        """
        comps = self.components()
        comp_of = {n: i for i, c in enumerate(comps) for n in c}
        bit = {n: 1 << i for i, n in enumerate(self.nodes)}
        members = [sum(bit[n] for n in c) for c in comps]
        importers: list[set[int]] = [set() for _ in comps]  # condensed edges: importer comp -> imported comp
        indeg = [0] * len(comps)
        for src, dsts in self.out.items():
            for dst in dsts:
                a, b = comp_of[src], comp_of[dst]
                if a != b and a not in importers[b]:
                    importers[b].add(a)
                    indeg[b] += 1
        # Kahn order over importer->imported edges: a component is processed after all its importers.
        succ: list[list[int]] = [[] for _ in comps]
        for b, imps in enumerate(importers):
            for a in imps:
                succ[a].append(b)
        dependents = [0] * len(comps)
        queue = deque(i for i, d in enumerate(indeg) if d == 0)
        while queue:
            c = queue.popleft()
            for b in succ[c]:
                dependents[b] |= dependents[c] | members[c]
                indeg[b] -= 1
                if indeg[b] == 0:
                    queue.append(b)
        out = {}
        for n in self.nodes:
            c = comp_of[n]
            out[n] = ((dependents[c] | members[c]) & ~bit[n]).bit_count()
        return out

    def report(self, top: int = 10) -> GraphReport:
        trans = self.transitive_dependent_counts()
        metrics = [
            NodeMetrics(module=n, direct_dependents=len(self.inc.get(n, ())),
                        transitive_dependents=trans[n], direct_dependencies=len(self.out.get(n, ())))
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
