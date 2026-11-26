"""Real-world check: the scanner understands this repository's own React/TypeScript UI (apps/web)."""

from pathlib import Path

from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import Certainty, EdgeKind
from yemendev.twin.scanner import ProjectScanner

WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


def test_own_web_ui_is_fully_resolved():
    snap = ProjectScanner().scan(WEB)
    mods = {m.name for m in snap.modules}
    assert {"src/main.tsx", "src/App.tsx", "src/api.ts", "src/i18n.ts", "src/ui.tsx", "src/pages/Projects.tsx"} <= mods
    assert not any(n.startswith(("node_modules/", "dist/")) for n in mods)
    # every relative import resolves, every package used is declared, nothing failed to parse
    assert [f for f in snap.findings if f.kind in ("unresolved_import", "undeclared_import", "parse_error")] == []
    external = {e.target for e in snap.edges if e.kind is EdgeKind.EXTERNAL_IMPORT}
    assert external <= {d.name for d in snap.declared_dependencies if d.ecosystem == "npm"}
    assert {"react", "react-dom"} <= external
    assert all(e.certainty is Certainty.OBSERVED for e in snap.edges)
    # the shared UI helpers are a critical module: most pages depend on them
    g = DependencyGraph.from_snapshot(snap)
    assert len(g.impacted_by("src/ui.tsx")) >= 7
