"""Structured (JSON) logging with request and trace correlation.

One JSON object per line on stderr: ``ts, level, logger, msg`` plus ``request_id`` / ``trace_id`` /
``span_id`` when known and any ``extra=`` fields. Messages, extras and exception text pass through the
secret redactor, so a token that reaches a log call is masked rather than written.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from yemendev.security.redaction import redact, redact_text

request_id_var: ContextVar[str | None] = ContextVar("yemendev_request_id", default=None)
_STANDARD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}


def trace_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the current OpenTelemetry span, if it has a valid context."""
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(), "logger": record.name, "msg": redact_text(record.getMessage()),
        }
        rid = request_id_var.get()
        if rid:
            out["request_id"] = rid
        tid, sid = trace_ids()
        if tid:
            out["trace_id"], out["span_id"] = tid, sid
        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}
        if extras:
            out.update(redact(json.loads(json.dumps(extras, default=str))))
        if record.exc_info:
            out["exc"] = redact_text(self.formatException(record.exc_info))[-4000:]
        return json.dumps(out, ensure_ascii=False)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    """Idempotent root configuration for long-running processes (serve, worker)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.set_name("yemendev")
    root = logging.getLogger()
    for h in list(root.handlers):
        if h.get_name() == "yemendev":
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level.upper())
