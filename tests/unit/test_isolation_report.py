"""Stage 11: 'auto' is the default backend; isolation status is reported with plain warnings."""

import pytest

from yemendev.core.config import SandboxSettings, Settings
from yemendev.sandbox.docker_backend import docker_availability, isolation_report

_docker_ok = docker_availability(SandboxSettings(), use_cache=False).available


def test_default_backend_is_auto(monkeypatch):
    assert SandboxSettings().backend == "auto"
    monkeypatch.delenv("YEMENDEV_SANDBOX_BACKEND", raising=False)
    assert Settings.from_env().sandbox.backend == "auto"


@pytest.mark.skipif(not _docker_ok, reason="docker sandbox unavailable")
def test_auto_uses_docker_without_warnings_when_available():
    r = isolation_report(SandboxSettings())
    assert r["effective"] == "docker" and r["warnings"] == []


def test_auto_falls_back_with_explicit_warnings(tmp_path):
    r = isolation_report(SandboxSettings(docker_binary=str(tmp_path / "no-docker")))
    assert r["effective"] == "process"
    assert any("falling back to the process backend" in w for w in r["warnings"])
    assert any("world-readable host files" in w for w in r["warnings"])


def test_required_docker_unusable_is_reported_as_unavailable(tmp_path):
    r = isolation_report(SandboxSettings(backend="docker", docker_binary=str(tmp_path / "no-docker")))
    assert r["effective"] == "unavailable" and "will fail" in r["warnings"][0]


def test_process_backend_and_network_warnings():
    r = isolation_report(SandboxSettings(backend="process", network_isolation="disabled"))
    assert r["effective"] == "process" and r["docker"] is None
    assert any("network access" in w for w in r["warnings"])


def test_platform_status_exposes_isolation(tmp_path):
    from yemendev.platform import Platform
    st = Platform(Settings(data_dir=tmp_path / "d")).status()["sandbox"]
    assert {"isolation_setting", "isolation_effective", "isolation_warnings"} <= set(st)
