"""Deterministic fault injection used by tests and resilience benchmarks.

Production code calls ``faults.check("point_name")`` at named crash points. With no
faults armed this is a no-op. Tests arm a point to raise ``InjectedFault`` exactly
where a power loss / crash would interrupt the workflow.
"""

from __future__ import annotations

from collections import Counter


class InjectedFault(RuntimeError):
    """Raised at an armed fault point. Never raised in normal operation."""


class FaultInjector:
    def __init__(self) -> None:
        self._armed: dict[str, int] = {}
        self.hits: Counter[str] = Counter()

    def arm(self, point: str, times: int = 1) -> None:
        self._armed[point] = times

    def disarm_all(self) -> None:
        self._armed.clear()

    def check(self, point: str) -> None:
        self.hits[point] += 1
        remaining = self._armed.get(point, 0)
        if remaining > 0:
            self._armed[point] = remaining - 1
            raise InjectedFault(f"injected fault at {point}")


NO_FAULTS = FaultInjector()
