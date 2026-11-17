import os
import sys
import time

import pytest
from helpers import make_passing_project, write

from yemendev.core.config import SandboxSettings
from yemendev.sandbox.workspace import Workspace, network_namespace_available


@pytest.fixture
def settings():
    return SandboxSettings(timeout_seconds=20, max_memory_mb=512)


def test_source_is_copied_not_modified(tmp_path, settings):
    src = make_passing_project(tmp_path / "p")
    with Workspace(src, settings) as ws:
        r = ws.run([sys.executable, "-c", "open('pkg/m.py','w').write('X')"])
        assert r.returncode == 0
        assert ws.path("pkg/m.py").read_text() == "X"
    assert (src / "pkg/m.py").read_text().startswith("def f")


def test_workspace_removed_after_use(tmp_path, settings):
    src = make_passing_project(tmp_path / "p")
    ws = Workspace(src, settings)
    root = ws.root
    ws.cleanup()
    assert not root.exists()


def test_symlinks_and_secrets_not_copied(tmp_path, settings):
    src = make_passing_project(tmp_path / "p")
    (tmp_path / "outside.txt").write_text("host secret")
    os.symlink(tmp_path / "outside.txt", src / "link.txt")
    write(src, ".env", "TOKEN=abc")
    with Workspace(src, settings) as ws:
        assert not (ws.root / "link.txt").exists()
        assert not (ws.root / ".env").exists()


def test_path_escape_rejected(tmp_path, settings):
    from yemendev.core.contracts import InvalidInputError
    with Workspace(make_passing_project(tmp_path / "p"), settings) as ws, pytest.raises(InvalidInputError):
        ws.path("../../etc/passwd")


def test_environment_is_scrubbed(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("YDV_TEST_SECRET", "should-not-leak")
    with Workspace(make_passing_project(tmp_path / "p"), settings) as ws:
        r = ws.run([sys.executable, "-c", "import os;print(sorted(os.environ));print(os.environ['HOME'])"])
    assert "YDV_TEST_SECRET" not in r.stdout
    assert str(ws.home) in r.stdout


def test_timeout_kills_whole_process_group(tmp_path, settings):
    marker = tmp_path / "child_alive"
    src = make_passing_project(tmp_path / "p")
    write(src, "child.py", f"import time\ntime.sleep(4)\nopen({str(marker)!r}, 'w').write('x')\n")
    write(src, "parent.py", "import subprocess, sys, time\nsubprocess.Popen([sys.executable, 'child.py'])\n"
                            "time.sleep(60)\n")
    with Workspace(src, settings) as ws:
        t = time.monotonic()
        r = ws.run([sys.executable, "parent.py"], timeout=1.5)
        assert r.timed_out and r.returncode is None and time.monotonic() - t < 10
    time.sleep(5)
    assert not marker.exists(), "grandchild survived the timeout"


def test_memory_limit_enforced(tmp_path, settings):
    with Workspace(make_passing_project(tmp_path / "p"), settings) as ws:
        r = ws.run([sys.executable, "-c", "x = bytearray(2 * 1024**3)"])
    assert r.returncode != 0 and "MemoryError" in r.stderr


def test_output_is_capped(tmp_path):
    s = SandboxSettings(timeout_seconds=20, max_output_bytes=2048)
    with Workspace(make_passing_project(tmp_path / "p"), s) as ws:
        r = ws.run([sys.executable, "-c", "print('a'*100000)"])
    assert r.output_truncated and len(r.stdout) <= 2048


@pytest.mark.skipif(not network_namespace_available(), reason="network namespaces unavailable on this host")
def test_network_is_isolated(tmp_path, settings):
    code = ("import socket\ns=socket.socket();s.settimeout(3)\n"
            "try:\n s.connect(('1.1.1.1',53));print('CONNECTED')\nexcept OSError as e:\n print('BLOCKED',e)\n")
    with Workspace(make_passing_project(tmp_path / "p"), settings) as ws:
        r = ws.run([sys.executable, "-c", code])
    assert r.network_isolated and "BLOCKED" in r.stdout


def test_required_isolation_refuses_when_unavailable(tmp_path, monkeypatch):
    from yemendev.core.contracts import UnavailableError
    from yemendev.sandbox import workspace as wsmod
    monkeypatch.setattr(wsmod, "network_namespace_available", lambda: False)
    s = SandboxSettings(network_isolation="required")
    with Workspace(make_passing_project(tmp_path / "p"), s) as ws, pytest.raises(UnavailableError):
        ws.run([sys.executable, "-c", "pass"])
