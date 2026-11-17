import socket

import pytest

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.faults import FaultInjector
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform

ALICE = Actor(id="alice", kind=ActorKind.HUMAN, role=Role.ADMIN)
BOB = Actor(id="bob", kind=ActorKind.HUMAN, role=Role.OPERATOR)


@pytest.fixture
def no_network(monkeypatch):
    """Offline mode for the platform process: any non-Unix socket connect fails."""
    real = socket.socket.connect

    def guarded(self, addr):
        if self.family == socket.AF_UNIX:
            return real(self, addr)
        raise OSError("network disabled for offline test")

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60))


@pytest.fixture
def faults():
    return FaultInjector()


@pytest.fixture
def platform(settings, faults):
    return Platform(settings, faults)


@pytest.fixture
def wf(platform):
    return RepairWorkflow(platform)
