"""Regression (stage 10 audit, CRITICAL): sandboxed code reached the Docker API through /var/run/docker.sock.

Network namespaces do not block Unix sockets; without a uid drop the code runs as the platform user, who
typically has docker access on non-root hosts. Access to the runtime socket is root-equivalent.
"""

import os
import socket
import sys

import pytest
from end_to_end.conftest import ALICE
from helpers import make_passing_project

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import UnavailableError
from yemendev.sandbox import workspace as wsmod
from yemendev.sandbox.workspace import Workspace, exposed_runtime_sockets


@pytest.fixture
def fake_socket(tmp_path, monkeypatch):
    path = tmp_path / "fake-docker.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    monkeypatch.setattr(wsmod, "runtime_socket_candidates", lambda: [path])
    yield path
    srv.close()


def test_detection_rules(fake_socket):
    os.chmod(fake_socket, 0o660)
    assert exposed_runtime_sockets(None, [fake_socket]) == [str(fake_socket)]  # platform user (owner)
    if os.geteuid() == 0:
        os.chown(fake_socket, 0, 0)
        assert exposed_runtime_sockets((65534, 65534), [fake_socket]) == []     # unrelated dropped uid
        os.chmod(fake_socket, 0o666)
        assert exposed_runtime_sockets((65534, 65534), [fake_socket]) == [str(fake_socket)]  # world-writable
        os.chmod(fake_socket, 0o660)
        os.chown(fake_socket, 0, 65534)
        assert exposed_runtime_sockets((65534, 65534), [fake_socket]) == [str(fake_socket)]  # group match
    regular = fake_socket.parent / "not-a-socket"
    regular.write_text("")
    assert exposed_runtime_sockets(None, [regular, fake_socket.parent / "missing"]) == []


def test_refuses_when_untrusted_code_could_reach_runtime(tmp_path, fake_socket, monkeypatch):
    monkeypatch.setattr(SandboxSettings, "sandbox_identity", lambda self: None)
    base = tmp_path / "ws"
    base.mkdir()
    with pytest.raises(UnavailableError, match="root on this host"):
        Workspace(make_passing_project(tmp_path / "p"), SandboxSettings(backend="process"), base_dir=base)
    assert os.listdir(base) == []  # nothing left behind


def test_explicit_override_runs_but_is_recorded_in_evidence(tmp_path, fake_socket, monkeypatch):
    monkeypatch.setattr(SandboxSettings, "sandbox_identity", lambda self: None)
    from yemendev.orion.workflow import RepairWorkflow
    from yemendev.platform import Platform
    s = Settings(data_dir=tmp_path / "data",
                 sandbox=SandboxSettings(backend="process", allow_runtime_socket_exposure=True))
    p = Platform(s)
    wf = RepairWorkflow(p)
    proj = p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    wf.advance(ALICE, wf.create(ALICE, proj.id).id)
    ev = next(e for e in p.evidence.list(limit=1000) if e.action == "tests.baseline")
    assert "container runtime socket" in (ev.body.environment["backend_note"] or "")
    assert any("container runtime socket" in x for x in ev.body.limitations)


def test_docker_backend_is_unaffected(tmp_path, fake_socket):
    """Containers never see host sockets, so the check does not apply to (or block) the docker backend."""
    from yemendev.sandbox.docker_backend import docker_availability
    if not docker_availability(SandboxSettings(), use_cache=False).available:
        pytest.skip("docker sandbox unavailable")
    with Workspace(make_passing_project(tmp_path / "p"), SandboxSettings(backend="docker")) as ws:
        r = ws.run(["python", "-c", "import os; print(os.path.exists('/var/run/docker.sock'))"])
    assert r.stdout.strip() == "False"


def test_real_host_socket_attack_is_refused_without_uid_drop(tmp_path, monkeypatch):
    if not exposed_runtime_sockets(None):
        pytest.skip("no container runtime socket on this host")
    monkeypatch.setattr(SandboxSettings, "sandbox_identity", lambda self: None)
    with pytest.raises(UnavailableError):
        with Workspace(make_passing_project(tmp_path / "p"), SandboxSettings(backend="process")) as ws:
            ws.run([sys.executable, "-c", "pass"])
