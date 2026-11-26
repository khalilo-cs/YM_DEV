"""Observability (stage 18d): structured logs, Prometheus metrics, OpenTelemetry tracing."""

import json
import logging
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from helpers import make_passing_project

from yemendev.api.app import create_app
from yemendev.core.config import SandboxSettings, Settings
from yemendev.governance.actors import Role
from yemendev.observability.logs import JsonFormatter, request_id_var
from yemendev.observability.metrics import Registry
from yemendev.platform import Platform

SECRET = "ydv_" + "Q" * 40


@pytest.fixture(scope="module")
def spans():
    """A real OpenTelemetry SDK provider with an in-memory exporter (set once per process)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def client(tmp_path):
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60, backend="process")))
    _, tok = p.tokens.issue("alice", Role.ADMIN)
    return p, TestClient(create_app(p), raise_server_exceptions=False), {"Authorization": f"Bearer {tok}"}


def test_json_logs_are_correlated_and_redacted():
    rec = logging.LogRecord("yemendev.t", logging.WARNING, __file__, 1, "token was %s", (SECRET,), None)
    rec.detail = {"api_key": "abc12345", "note": f"Bearer {SECRET}"}
    tok = request_id_var.set("req-12345678")
    try:
        out = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(tok)
    assert out["level"] == "warning" and out["logger"] == "yemendev.t" and out["request_id"] == "req-12345678"
    assert SECRET not in json.dumps(out) and "abc12345" not in json.dumps(out)
    assert out["msg"] == "token was [REDACTED]"


def test_prometheus_text_format():
    r = Registry()
    c = r.counter("x_total", "things", ("kind",))
    c.inc("a")
    c.inc('we"ird\\', amount=2)
    h = r.histogram("lat_seconds", "latency", ("route",), buckets=(0.1, 1))
    for v in (0.05, 0.5, 3):
        h.observe(v, "/p/{id}")
    r.gauge("up", "is up", (), lambda: [((), 1.0)])
    text = r.render()
    assert 'x_total{kind="a"} 1\n' in text and 'x_total{kind="we\\"ird\\\\"} 2\n' in text
    assert 'lat_seconds_bucket{route="/p/{id}",le="0.1"} 1' in text
    assert 'lat_seconds_bucket{route="/p/{id}",le="1"} 2' in text  # cumulative
    assert 'lat_seconds_bucket{route="/p/{id}",le="+Inf"} 3' in text
    assert 'lat_seconds_sum{route="/p/{id}"} 3.55' in text and 'lat_seconds_count{route="/p/{id}"} 3' in text
    assert "# TYPE up gauge\nup 1\n" in text
    with pytest.raises(ValueError):
        c.inc("a", amount=-1)


def test_request_ids_trace_context_and_access_log(client, spans, caplog):
    _, c, h = client
    r = c.get("/api/v1/health", headers={"X-Request-ID": "client-req-0001"})
    assert r.headers["x-request-id"] == "client-req-0001"
    bad = c.get("/api/v1/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert len(bad.headers["x-request-id"]) == 32 and bad.headers["x-request-id"] != "bad id\nwith newline"
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    with caplog.at_level(logging.INFO, logger="yemendev.access"):
        caplog.clear()  # other tests may already have configured logging at INFO for this process
        r = c.get("/api/v1/projects/prj_does_not_exist", headers={
            **h, "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})
    assert r.status_code == 404
    assert r.headers["traceparent"].split("-")[1] == trace_id  # the caller's trace continues
    [rec] = [x for x in caplog.records if x.name == "yemendev.access"]
    assert (rec.route, rec.status) == ("/api/v1/projects/{project_id}", 404) and rec.duration_ms >= 0
    server = [s for s in spans.get_finished_spans() if s.name == "GET /api/v1/projects/{project_id}"][-1]
    assert f"{server.context.trace_id:032x}" == trace_id
    assert server.attributes["http.route"] == "/api/v1/projects/{project_id}"
    assert server.attributes["http.response.status_code"] == 404


def test_metrics_endpoint_needs_a_token_and_uses_route_templates(client):
    _, c, h = client
    c.get("/api/v1/projects/prj_secret_looking_id", headers=h)
    assert c.get("/api/v1/metrics").status_code == 401
    r = c.get("/api/v1/metrics", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'route="/api/v1/projects/{project_id}",status="404"' in r.text
    assert "prj_secret_looking_id" not in r.text  # raw ids never become label values
    assert "# TYPE yemendev_queue_entries gauge" in r.text and "# TYPE yemendev_tasks gauge" in r.text


def test_background_runs_emit_spans_and_metrics(client, spans, tmp_path):
    from end_to_end.conftest import ALICE

    from yemendev.observability.metrics import ORION_RUNS
    from yemendev.orion.queue import QueueOptions
    from yemendev.orion.scheduler import Scheduler

    p, c, h = client
    sched = Scheduler(p)
    t = sched.wf.create(ALICE, p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok"))).id)
    sched.wf.enqueue(ALICE, t.id, QueueOptions())
    before = ORION_RUNS.value("finished")
    [out] = sched.drain()
    assert ORION_RUNS.value("finished") == before + 1
    run = [s for s in spans.get_finished_spans() if s.name == "orion.run"][-1]
    assert run.attributes["yemendev.task_id"] == t.id and run.attributes["yemendev.outcome"] == "finished"
    assert 'yemendev_orion_runs_total{outcome="finished"}' in c.get("/api/v1/metrics", headers=h).text


def test_the_run_process_continues_the_parents_trace(tmp_path):
    """A real child process (YEMENDEV_OTEL=console) exports step spans in the trace the parent started."""
    from end_to_end.conftest import ALICE

    from yemendev.orion.queue import QueueOptions
    from yemendev.orion.workflow import RepairWorkflow

    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60, backend="process")))
    p.tokens.issue("alice", Role.ADMIN)
    wf = RepairWorkflow(p)
    t = wf.create(ALICE, p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok"))).id)
    wf.enqueue(ALICE, t.id, QueueOptions())
    claim = wf.queue.claim("test")
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    job = {"settings": p.settings.model_dump(mode="json"), "entry_id": claim.entry.id, "run_id": claim.run_id,
           "task_id": t.id, "actor": claim.actor.model_dump(mode="json"), "result_path": str(tmp_path / "r.json"),
           "trace_context": {"traceparent": f"00-{trace_id}-b7ad6b7169203331-01"}}
    r = subprocess.run([sys.executable, "-m", "yemendev.orion.runner"], input=json.dumps(job), text=True,
                       capture_output=True, timeout=300, env={**os.environ, "YEMENDEV_OTEL": "console"})
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "r.json").read_text())["task_status"] == "no_action"
    exported = json.loads("[" + r.stdout.replace("}\n{", "},\n{") + "]")
    steps = [s for s in exported if s["name"].startswith("orion.step ")]
    assert {s["name"] for s in steps} >= {"orion.step scan", "orion.step baseline"}
    assert {s["context"]["trace_id"] for s in steps} == {f"0x{trace_id}"}
