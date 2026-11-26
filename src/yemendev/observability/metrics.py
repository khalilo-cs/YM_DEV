"""In-process metrics with Prometheus text exposition (format 0.0.4), no third-party dependency.

Label values are bounded by design: HTTP routes are templates (``/api/v1/projects/{project_id}``), never
raw paths or ids. Gauges that describe platform state are computed at scrape time from the database.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable

Labels = tuple[str, ...]


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v: float) -> str:
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v)) if not float(v).is_integer() else str(int(v)) if abs(v) < 1e15 else repr(float(v))


def _labels(names: Labels, values: Labels, extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class Counter:
    def __init__(self, name: str, help_: str, labels: Labels = ()) -> None:
        self.name, self.help, self.label_names = name, help_, labels
        self._v: dict[Labels, float] = {}
        self._lock = threading.Lock()

    def inc(self, *labels: str, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("counters only go up")
        with self._lock:
            self._v[labels] = self._v.get(labels, 0.0) + amount

    def value(self, *labels: str) -> float:
        return self._v.get(labels, 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            for lv, v in sorted(self._v.items()):
                yield f"{self.name}{_labels(self.label_names, lv)} {_fmt(v)}"


DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


class Histogram:
    def __init__(self, name: str, help_: str, labels: Labels = (),
                 buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self.name, self.help, self.label_names, self.buckets = name, help_, labels, tuple(sorted(buckets))
        self._v: dict[Labels, tuple[list[int], float, int]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, *labels: str) -> None:
        with self._lock:
            counts, total, n = self._v.get(labels, ([0] * len(self.buckets), 0.0, 0))
            counts = [c + (1 if value <= b else 0) for c, b in zip(counts, self.buckets, strict=True)]
            self._v[labels] = (counts, total + value, n + 1)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            for lv, (counts, total, n) in sorted(self._v.items()):
                for b, c in zip(self.buckets, counts, strict=True):  # counts are already cumulative
                    le = 'le="' + _fmt(b) + '"'
                    yield f"{self.name}_bucket{_labels(self.label_names, lv, le)} {c}"
                inf = 'le="+Inf"'
                yield f"{self.name}_bucket{_labels(self.label_names, lv, inf)} {n}"
                yield f"{self.name}_sum{_labels(self.label_names, lv)} {_fmt(total)}"
                yield f"{self.name}_count{_labels(self.label_names, lv)} {n}"


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Counter | Histogram] = []
        self._gauges: list[tuple[str, str, Labels, Callable[[], list[tuple[Labels, float]]]]] = []

    def counter(self, name: str, help_: str, labels: Labels = ()) -> Counter:
        c = Counter(name, help_, labels)
        self._metrics.append(c)
        return c

    def histogram(self, name: str, help_: str, labels: Labels = (),
                  buckets: tuple[float, ...] | None = None) -> Histogram:
        h = Histogram(name, help_, labels, buckets) if buckets else Histogram(name, help_, labels)
        self._metrics.append(h)
        return h

    def gauge(self, name: str, help_: str, labels: Labels, fn: Callable[[], list[tuple[Labels, float]]]) -> None:
        self._gauges = [g for g in self._gauges if g[0] != name] + [(name, help_, labels, fn)]

    def render(self) -> str:
        lines: list[str] = []
        for m in self._metrics:
            lines.extend(m.render())
        for name, help_, labels, fn in self._gauges:
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
            lines += [f"{name}{_labels(labels, lv)} {_fmt(v)}" for lv, v in sorted(fn())]
        return "\n".join(lines) + "\n"


REGISTRY = Registry()
HTTP_REQUESTS = REGISTRY.counter("yemendev_http_requests_total", "HTTP requests by route template and status",
                                 ("method", "route", "status"))
HTTP_DURATION = REGISTRY.histogram("yemendev_http_request_duration_seconds", "HTTP request latency",
                                   ("method", "route"))
ORION_RUNS = REGISTRY.counter("yemendev_orion_runs_total", "Background runs by outcome", ("outcome",))
ORION_RUN_SECONDS = REGISTRY.histogram("yemendev_orion_run_duration_seconds", "Wall time of background runs",
                                       ("outcome",), buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600))
ORION_RUN_CPU = REGISTRY.counter("yemendev_orion_run_cpu_seconds_total", "CPU seconds used by background runs")
