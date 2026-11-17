import socket

import pytest

from yemendev.governance.actors import Actor, ActorKind, Role

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
