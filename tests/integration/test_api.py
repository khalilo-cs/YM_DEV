import json

import pytest
from fastapi.testclient import TestClient
from helpers import make_renamed_api_project

from yemendev.api.app import create_app
from yemendev.core.config import SandboxSettings, Settings
from yemendev.governance.actors import Role
from yemendev.platform import Platform


@pytest.fixture
def ctx(tmp_path):
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60)))
    _, admin = p.tokens.issue("alice", Role.ADMIN)
    _, op = p.tokens.issue("bob", Role.OPERATOR)
    _, viewer = p.tokens.issue("vic", Role.VIEWER)
    c = TestClient(create_app(p), raise_server_exceptions=False)
    H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    return p, c, H(admin), H(op), H(viewer), tmp_path


def test_health_is_public_everything_else_requires_token(ctx):
    _, c, *_ = ctx
    assert c.get("/api/v1/health").json()["status"] == "ok"
    for path in ["/api/v1/system/status", "/api/v1/projects", "/api/v1/tasks", "/api/v1/approvals",
                 "/api/v1/evidence", "/api/v1/audit", "/api/v1/skills", "/api/v1/models"]:
        r = c.get(path)
        assert r.status_code == 401, path
        assert r.json() == {"error": {"code": "unauthenticated", "message": "missing or invalid bearer token"}}
    assert c.get("/api/v1/projects", headers={"Authorization": "Bearer ydv_wrong"}).status_code == 401
    assert c.get("/api/v1/projects", headers={"Authorization": "Basic abc"}).status_code == 401


def test_revoked_token_rejected(ctx):
    p, c, *_ = ctx
    tid, tok = p.tokens.issue("tmp", Role.VIEWER)
    assert c.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    p.tokens.revoke(tid)
    assert c.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_role_enforcement(ctx, tmp_path):
    p, c, admin, op, viewer, _ = ctx
    root = make_renamed_api_project(tmp_path / "proj")
    body = {"name": "calc", "path": str(root)}
    assert c.post("/api/v1/projects", json=body, headers=op).status_code == 403  # admin only
    assert c.post("/api/v1/projects", json=body, headers=viewer).status_code == 403
    r = c.post("/api/v1/projects", json=body, headers=admin)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert c.post(f"/api/v1/projects/{pid}/scan", headers=viewer).status_code == 403
    assert c.post("/api/v1/tasks", json={"project_id": pid}, headers=viewer).status_code == 403
    assert c.get("/api/v1/projects", headers=viewer).status_code == 200


def test_errors_are_enveloped_and_do_not_leak(ctx):
    p, c, admin, *_ = ctx
    r = c.get("/api/v1/projects/prj_missing", headers=admin)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = c.post("/api/v1/projects", content=b"{not json", headers={**admin, "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_input"
    r = c.post("/api/v1/projects", json={"name": "x", "path": "relative/path"}, headers=admin)
    assert r.status_code == 422 and "absolute" in r.json()["error"]["message"]
    r = c.post("/api/v1/projects", json={"name": "x", "path": "/"}, headers=admin)
    assert r.status_code == 422
    r = c.get("/api/v1/projects", headers={**admin, "X-Forwarded-For": "x"})
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_internal_error_is_generic(ctx, monkeypatch):
    p, c, admin, *_ = ctx
    monkeypatch.setattr(p, "projects", lambda: (_ for _ in ()).throw(RuntimeError("secret /etc/shadow detail")))
    r = c.get("/api/v1/projects", headers=admin)
    assert r.status_code == 500 and r.json() == {"error": {"code": "internal", "message": "internal error"}}
    assert "shadow" not in r.text


def test_oversized_body_rejected(ctx):
    _, c, admin, *_ = ctx
    r = c.post("/api/v1/projects", content=b"x" * 10, headers={**admin, "Content-Length": "999999999"})
    assert r.status_code == 413


def test_regression_chunked_oversized_body_rejected(ctx):
    # Regression: Transfer-Encoding: chunked bypassed the Content-Length-only size check.
    _, c, admin, *_ = ctx

    def gen():
        for _ in range(7):
            yield b"a" * 1_000_000

    r = c.post("/api/v1/projects", content=gen(), headers={**admin, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_plan_with_traversal_rejected_by_schema(ctx, tmp_path):
    p, c, admin, *_ = ctx
    root = make_renamed_api_project(tmp_path / "proj")
    pid = c.post("/api/v1/projects", json={"name": "c", "path": str(root)}, headers=admin).json()["id"]
    plan = {"id": "pln_x", "description": "d", "source": "human", "source_ref": "h",
            "patches": [{"path": "../../etc/cron.d/x", "original_sha256": None, "new_content": "boom"}]}
    r = c.post("/api/v1/tasks", json={"project_id": pid, "plan": plan}, headers=admin)
    assert r.status_code == 422


def test_full_workflow_over_http(ctx, tmp_path):
    p, c, admin, op, viewer, _ = ctx
    root = make_renamed_api_project(tmp_path / "proj")
    pid = c.post("/api/v1/projects", json={"name": "c", "path": str(root)}, headers=admin).json()["id"]
    api = (root / "calc/api.py")
    from yemendev.core.contracts import sha256_bytes
    plan = {"id": "pln_h", "description": "rename", "source": "human", "source_ref": "alice", "patches": [
        {"path": "calc/api.py", "original_sha256": sha256_bytes(api.read_bytes()),
         "new_content": api.read_text().replace("add_numbers", "add")}]}
    for who, role in (("human:bob", "operator"), ("human:vic", "viewer")):
        assert c.post(f"/api/v1/projects/{pid}/members", json={"actor_ref": who, "role": role},
                      headers=admin).status_code == 201
    tid = c.post("/api/v1/tasks", json={"project_id": pid, "plan": plan}, headers=admin).json()["id"]
    r = c.post(f"/api/v1/tasks/{tid}/advance", headers=admin).json()
    assert r["task"]["status"] == "waiting_approval"
    [apr] = c.get("/api/v1/approvals?status=pending", headers=viewer).json()
    assert "calc/api.py" in apr["summary"]
    assert c.post(f"/api/v1/approvals/{apr['id']}", json={"approve": True}, headers=viewer).status_code == 403
    assert c.post(f"/api/v1/approvals/{apr['id']}", json={"approve": True}, headers=admin).status_code == 403
    assert c.post(f"/api/v1/approvals/{apr['id']}", json={"approve": True}, headers=op).status_code == 200
    r = c.post(f"/api/v1/tasks/{tid}/advance", headers=op).json()
    assert r["task"]["status"] == "succeeded", r["message"]
    t = c.get(f"/api/v1/tasks/{tid}", headers=viewer).json()
    assert [s["name"] for s in t["steps"]][-1] == "learn"
    ev = c.get(f"/api/v1/evidence?task_id={tid}", headers=viewer).json()
    assert {"repair.execute", "repair.verify"} <= {e["action"] for e in ev}
    assert c.get("/api/v1/audit/verify", headers=viewer).status_code == 403  # audit spans all projects
    assert c.get("/api/v1/audit/verify", headers=admin).json()["valid"]
    g = c.get(f"/api/v1/projects/{pid}/graph", headers=viewer).json()
    assert ["calc.api", "calc.mathutils"] in [list(e) for e in g["edges"]]
    mem = c.get(f"/api/v1/projects/{pid}/memory?category=experience", headers=viewer).json()
    assert len(mem) == 1
    st = c.get("/api/v1/system/status", headers=viewer).json()
    assert st["counts"]["tasks"] == 1 and st["models"][0]["available"] is False
    assert "ydv_" not in json.dumps(c.get("/api/v1/audit?limit=1000", headers=admin).json())


def test_queue_api_enqueue_trace_cancel_and_visibility(ctx, tmp_path):
    from helpers import make_passing_project

    from yemendev.orion.scheduler import Scheduler

    p, c, admin, op, viewer, _ = ctx
    root = make_passing_project(tmp_path / "ok")
    pid = c.post("/api/v1/projects", json={"name": "ok", "path": str(root)}, headers=admin).json()["id"]
    r = c.post("/api/v1/tasks", json={"project_id": pid, "queue": {"priority": 2, "timeout_seconds": 120}},
               headers=admin)
    assert r.status_code == 201 and r.json()["queue"]["state"] == "queued"
    tid = r.json()["id"]
    assert c.post(f"/api/v1/tasks/{tid}/enqueue", json={}, headers=admin).status_code == 409  # already queued
    listed = {t["id"]: t for t in c.get("/api/v1/tasks", headers=admin).json()}
    assert listed[tid]["queue_state"] == "queued"
    q = c.get("/api/v1/queue", headers=admin).json()
    assert [e["task_id"] for e in q["entries"]] == [tid] and q["limits"]["max_concurrent_runs"] >= 1
    assert q["workers_in_this_process"]["running"] is False
    # a viewer with no membership sees neither the entry nor the task, and cannot queue anything
    assert c.get("/api/v1/queue", headers=viewer).json()["entries"] == []
    t2 = c.post("/api/v1/tasks", json={"project_id": pid}, headers=admin).json()["id"]
    assert c.post(f"/api/v1/tasks/{t2}/enqueue", json={}, headers=viewer).status_code == 403
    assert c.post(f"/api/v1/tasks/{t2}/enqueue", json={"priority": 99}, headers=admin).status_code == 422
    # cancel a queued task: entry and task are both cancelled, a worker finds nothing to do
    assert c.post(f"/api/v1/tasks/{tid}/cancel", headers=admin).json()["status"] == "cancelled"
    assert c.get("/api/v1/queue", headers=admin).json()["entries"][0]["state"] == "cancelled"
    # queue t2 and let a worker run it for real
    assert c.post(f"/api/v1/tasks/{t2}/enqueue", json={"timeout_seconds": 120}, headers=admin).status_code == 201
    p.settings.sandbox.backend = "process"
    [out] = Scheduler(p).drain()
    assert out.task_id == t2 and out.task_status == "no_action"
    tr = c.get(f"/api/v1/tasks/{t2}/trace", headers=admin).json()
    assert tr["queue"]["state"] == "done" and len(tr["plan"]) == 10
    assert any(e["kind"] == "queue.run_finished" and e["detail"]["cpu_seconds"] > 0 for e in tr["events"])
    assert c.get(f"/api/v1/tasks/{t2}/trace", headers=viewer).status_code == 403
    st = c.get("/api/v1/system/status", headers=admin).json()
    assert st["queue"] == {"queued": 0, "running": 0, "waiting": 0}


def test_sbom_api_requires_a_scan_and_project_access_and_is_recorded(ctx, tmp_path):
    from helpers import write

    p, c, admin, op, viewer, _ = ctx
    root = tmp_path / "web"
    write(root, "package.json", '{"dependencies": {"react": "18.3.1"}}')
    write(root, "src/a.js", 'import React from "react";\n')
    pid = c.post("/api/v1/projects", json={"name": "web", "path": str(root)}, headers=admin).json()["id"]
    assert c.get(f"/api/v1/projects/{pid}/sbom", headers=admin).status_code == 404  # not scanned yet
    c.post(f"/api/v1/projects/{pid}/scan", headers=admin)
    r = c.get(f"/api/v1/projects/{pid}/sbom", headers=admin)
    assert r.status_code == 200 and r.json()["bomFormat"] == "CycloneDX"
    assert [x["purl"] for x in r.json()["components"]] == ["pkg:npm/react@18.3.1"]
    assert c.get(f"/api/v1/projects/{pid}/sbom", headers=viewer).status_code == 403
    ev = [e for e in p.evidence.list() if e.action == "sbom.generated"]
    assert len(ev) == 1 and ev[0].body.details["components"] == 1
    sys_sbom = c.get("/api/v1/system/sbom", headers=viewer)
    assert sys_sbom.status_code == 200 and any(x["name"] == "fastapi" for x in sys_sbom.json()["components"])
