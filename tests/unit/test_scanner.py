import os
from pathlib import Path

import pytest

from yemendev.core.contracts import InvalidInputError
from yemendev.twin.graph import DependencyGraph
from yemendev.twin.model import Certainty, EdgeKind
from yemendev.twin.scanner import ProjectScanner


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def proj(tmp_path):
    r = tmp_path / "app"
    write(r, "pyproject.toml", '[project]\nname="app"\ndependencies=["requests>=2", "PyYAML", "unused-lib"]\n'
                              '[project.optional-dependencies]\ndev=["pytest"]\n')
    write(r, "src/app/__init__.py", "")
    write(r, "src/app/core.py", "import os\nimport requests\nfrom . import util\n")
    write(r, "src/app/util.py", "import yaml\nfrom app.models import User\n")
    write(r, "src/app/models.py", "import numpy as np\n")
    write(r, "src/app/sub/__init__.py", "from ..core import x\n")
    write(r, "tests/test_core.py", "from app.core import thing\n")
    write(r, ".env", "SECRET=hunter2\n")
    write(r, "node_modules/x/index.js", "bad")
    return r


def test_scan_structure_and_edges(proj):
    s = ProjectScanner().scan(proj)
    names = {m.name for m in s.modules}
    assert {"app", "app.core", "app.util", "app.models", "app.sub"} <= names
    edges = {(e.source, e.target, e.kind) for e in s.edges}
    assert ("app.core", "app.util", EdgeKind.INTERNAL_IMPORT) in edges
    assert ("app.util", "app.models", EdgeKind.INTERNAL_IMPORT) in edges
    assert ("app.sub", "app.core", EdgeKind.INTERNAL_IMPORT) in edges
    assert ("app.core", "os", EdgeKind.STDLIB_IMPORT) in edges
    assert ("app.core", "requests", EdgeKind.EXTERNAL_IMPORT) in edges
    assert all(e.certainty is Certainty.OBSERVED for e in s.edges)
    assert s.test_framework == "pytest"
    assert not any("node_modules" in f.path for f in s.files)


def test_secret_files_listed_but_not_read(proj):
    s = ProjectScanner().scan(proj)
    assert s.sensitive_files == [".env"]
    assert ".env" not in {f.path for f in s.files}
    assert "hunter2" not in s.model_dump_json()


def test_dependency_findings(proj):
    s = ProjectScanner().scan(proj)
    kinds = {(f.kind, f.message.split("'")[1]) for f in s.findings if "'" in f.message}
    assert ("undeclared_import", "numpy") in kinds
    assert ("declared_not_imported", "unused-lib") in kinds
    # yaml -> pyyaml mapping is used; requests matches directly
    assert not any(f.kind == "undeclared_import" and "'yaml'" in f.message for f in s.findings)
    assert all(f.certainty is Certainty.INFERRED for f in s.findings if f.kind.startswith(("undeclared", "declared")))


def test_symlinks_not_followed(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("TOP_SECRET = 1\n")
    os.symlink(outside, proj / "src" / "app" / "linkdir")
    os.symlink(outside / "secret.py", proj / "src" / "app" / "leak.py")
    s = ProjectScanner().scan(proj)
    assert not any("secret" in f.path or "leak" in f.path for f in s.files)
    assert any("leak.py" in x for x in s.skipped) and any("linkdir" in x for x in s.skipped)


def test_syntax_error_is_finding_not_crash(proj):
    write(proj, "src/app/broken.py", "def f(:\n")
    s = ProjectScanner().scan(proj)
    assert any(f.kind == "syntax_error" and f.path == "src/app/broken.py" for f in s.findings)


def test_tree_hash_changes_with_content(proj):
    a = ProjectScanner().scan(proj).tree_hash
    assert ProjectScanner().scan(proj).tree_hash == a
    write(proj, "src/app/util.py", "import yaml\n")
    assert ProjectScanner().scan(proj).tree_hash != a


def test_invalid_root(tmp_path):
    with pytest.raises(InvalidInputError):
        ProjectScanner().scan(tmp_path / "nope")


def test_file_limit_truncation_is_reported(proj):
    s = ProjectScanner(max_files=2).scan(proj)
    assert len(s.files) == 2 and any(f.kind == "scan_truncated" for f in s.findings)


def test_graph_impact_cycles_and_critical(proj):
    write(proj, "src/app/models.py", "from app import core\n")  # creates cycle core->util->models->core
    g = DependencyGraph.from_snapshot(ProjectScanner().scan(proj))
    assert ["app.core", "app.models", "app.util"] in g.cycles()
    assert {"app.util", "app.models", "app.sub"} <= g.impacted_by("app.core")
    rep = g.report()
    assert rep.critical_modules and "app.core" in rep.critical_modules
    assert "not proven" in rep.note


def test_graph_large_chain_no_recursion_error():
    from yemendev.twin.model import Edge
    n = 5000
    nodes = [f"m{i}" for i in range(n)]
    edges = [Edge(source=f"m{i}", target=f"m{i+1}", kind=EdgeKind.INTERNAL_IMPORT, certainty=Certainty.OBSERVED)
             for i in range(n - 1)]
    g = DependencyGraph(nodes, edges)
    assert g.cycles() == []
    assert len(g.impacted_by(f"m{n-1}")) == n - 1


def test_regression_relative_import_beyond_top_level_is_unknown(tmp_path):
    # Regression: 'from ..core import x' inside top-level package 'app' resolved to a module 'core'.
    write(tmp_path, "app/__init__.py", "")
    write(tmp_path, "app/mod.py", "from ..core import x\n")
    write(tmp_path, "core.py", "")
    s = ProjectScanner().scan(tmp_path)
    rel = [e for e in s.edges if e.source == "app.mod"]
    assert len(rel) == 1 and rel[0].certainty is Certainty.UNKNOWN


def test_scan_recorded_in_memory_goes_stale_on_change(db, project_row, tmp_path):
    from yemendev.memory.engine import MemoryEngine
    from yemendev.memory.model import MemoryStatus
    from yemendev.twin.memory_sync import record_scan

    root = tmp_path / "proj"
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/a.py", "import json\n")
    mem = MemoryEngine(db)
    counts = record_scan(mem, project_row, ProjectScanner().scan(root), "evd_x")
    assert counts["created"] >= 3
    # second identical scan: nothing new, nothing overwritten
    again = record_scan(mem, project_row, ProjectScanner().scan(root), "evd_y")
    assert "created" not in again and again["unchanged"] >= 3
    write(root, "pkg/a.py", "import os\n")
    assert mem.revalidate(project_row, root)["marked_stale"] == 1
    stale = [r for r in mem.query(project_row, include_inactive=True) if r.status is MemoryStatus.STALE]
    assert [r.subject for r in stale] == ["module:pkg.a"]
