"""OpenTelemetry tracing.

The platform always creates spans through the OpenTelemetry **API** (a no-op until an SDK is configured),
following the standard library/application split. ``YEMENDEV_OTEL`` selects an exporter when the optional
SDK is installed (``pip install 'yemendev[otel]'``):

* ``off`` (default) - nothing is recorded or sent; incoming W3C ``traceparent`` is still honoured, so
  logs and responses carry the caller's trace id;
* ``console`` - spans are printed (debugging, fully offline);
* ``otlp`` - spans go to an OTLP/HTTP collector configured with the standard ``OTEL_EXPORTER_OTLP_*``
  environment variables.

Spans: one server span per HTTP request (route template, status), one per background run (task, run id,
outcome, CPU/memory) and one per workflow step inside the run process, joined across the process boundary.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import propagate, trace

log = logging.getLogger("yemendev.tracing")
tracer = trace.get_tracer("yemendev")
_configured: str | None = None


def configure_tracing(mode: str | None = None) -> str:
    """Install an SDK tracer provider for ``mode`` (default ``$YEMENDEV_OTEL``). Returns the effective mode."""
    global _configured
    mode = (mode if mode is not None else os.environ.get("YEMENDEV_OTEL", "off")).strip().lower() or "off"
    if mode == "off" or _configured == mode:
        return _configured or "off"
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        log.warning("YEMENDEV_OTEL=%s but the OpenTelemetry SDK is not installed; tracing stays off", mode)
        return "unavailable"
    from yemendev import __version__

    provider = TracerProvider(resource=Resource.create({"service.name": "yemendev", "service.version": __version__}))
    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif mode == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
        except ImportError:
            log.warning("YEMENDEV_OTEL=otlp needs opentelemetry-exporter-otlp-proto-http; tracing stays off")
            return "unavailable"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        log.warning("unknown YEMENDEV_OTEL=%s (use off, console or otlp)", mode)
        return "unavailable"
    trace.set_tracer_provider(provider)
    _configured = mode
    return mode


def inject() -> dict[str, str]:
    """W3C trace context of the current span, for another process or an HTTP response."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract(carrier: dict[str, Any]) -> Any:
    return propagate.extract(carrier)


def flush() -> None:
    provider = trace.get_tracer_provider()
    force = getattr(provider, "force_flush", None)
    if callable(force):
        force()
