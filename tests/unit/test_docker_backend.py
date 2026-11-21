"""Docker backend unit tests - no daemon needed (fake docker binaries)."""

import os
import stat
from pathlib import Path

import pytest
from helpers import make_passing_project

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import InvalidInputError, UnavailableError
from yemendev.sandbox import docker_backend as db
from yemendev.sandbox.workspace import Workspace


def test_command_has_every_hardening_flag():
    s = SandboxSettings(max_memory_mb=512, docker_pids_limit=99, docker_cpus=1.5)
    cmd = db.docker_run_command(s, Path("/tmp/ws/project"), (65534, 65534), ["python", "-m", "pytest"], "ydv-x", 30)
    joined = " ".join(cmd)
    for flag in ["--rm", "--init", "--network none", "--read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--user 65534:65534", "--pids-limit 99", "--memory 512m",
                 "--memory-swap 512m", "--cpus 1.5", "--ulimit core=0:0",
                 "--mount type=bind,src=/tmp/ws/project,dst=/work", "--workdir /work", "--entrypoint python"]:
        assert flag in joined, flag
    assert cmd[cmd.index("--entrypoint") + 2:] == ["yemendev-sandbox:py3.13", "-m", "pytest"]
    assert "--privileged" not in cmd and "/var/run/docker.sock" not in joined


def test_network_flag_follows_setting():
    cmd = db.docker_run_command(SandboxSettings(network_isolation="disabled"), Path("/w"), (1, 1), ["x"], "n", 5)
    assert cmd[cmd.index("--network") + 1] == "bridge"


@pytest.mark.parametrize("root", [Path("relative"), Path("/tmp/a,b")])
def test_unsafe_mount_paths_rejected(root):
    with pytest.raises(InvalidInputError):
        db.docker_run_command(SandboxSettings(), root, (1, 1), ["x"], "n", 5)


def test_never_root_in_container():
    assert db.container_identity((0, 0)) == (65534, 65534)
    assert db.container_identity((1000, 1000)) == (1000, 1000)


def _fake_docker(tmp_path: Path, script: str) -> str:
    p = tmp_path / "fake-docker"
    p.write_text("#!/bin/sh\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.parametrize("script,reason", [
    (None, "not found"),
    ('[ "$1" = version ] && exit 1\nexit 0\n', "daemon is not reachable"),
    ('[ "$1" = version ] && echo 29.0 && exit 0\n[ "$1" = image ] && exit 1\nexit 0\n', "not present locally"),
])
def test_availability_reasons(tmp_path, script, reason):
    binary = _fake_docker(tmp_path, script) if script else str(tmp_path / "missing-docker")
    a = db.docker_availability(SandboxSettings(docker_binary=binary), use_cache=False)
    assert not a.available and reason in a.reason


def test_docker_required_fails_loudly_and_auto_falls_back_with_note(tmp_path):
    binary = str(tmp_path / "missing-docker")
    src = make_passing_project(tmp_path / "p")
    db.clear_availability_cache()
    with pytest.raises(UnavailableError, match="docker sandbox backend required"):
        Workspace(src, SandboxSettings(backend="docker", docker_binary=binary))
    with Workspace(src, SandboxSettings(backend="auto", docker_binary=binary)) as ws:
        r = ws.run(["true"])
    assert r.backend == "process" and "docker unavailable" in (r.backend_note or "")


def test_invalid_backend_setting():
    with pytest.raises(ValueError):
        SandboxSettings(backend="vm")


def test_no_leftover_tmp_on_required_failure(tmp_path):
    base = tmp_path / "ws"
    base.mkdir()
    with pytest.raises(UnavailableError):
        Workspace(make_passing_project(tmp_path / "p"), SandboxSettings(backend="docker",
                  docker_binary=str(tmp_path / "nope")), base_dir=base)
    assert os.listdir(base) == []
