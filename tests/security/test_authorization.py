"""Stage 9: per-project authorization, token expiry, rate limiting (audit findings A-11, A-14)."""

import pytest
from fastapi.testclient import TestClient
from helpers import make_passing_project, make_renamed_api_project

from yemendev.api.app import create_app
from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import InvalidInputError, PermissionDeniedError, parse_iso, sha256_bytes, utcnow
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.platform import Platform


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def env(tmp_path):
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60)))
    tokens = {name: p.tokens.issue(name, role)[1] for name, role in
              [("alice", Role.ADMIN), ("bob", Role.OPERATOR), ("vic", Role.VIEWER), ("eve", Role.OPERATOR)]}
    c = TestClient(create_app(p), raise_server_exceptions=False)
    return p, c, tokens, tmp_path


def _two_projects_with_pending_approvals(p, c, tok, tmp_path):
    ids = []
    for name in ("one", "two"):
        root = make_renamed_api_project(tmp_path / name)
        pid = c.post("/api/v1/projects", json={"name": name, "path": str(root)}, headers=H(tok["alice"])).json()["id"]
        api = root / "calc/api.py"
        plan = {"id": f"pln_{name}", "description": "fix", "source": "human", "source_ref": "x", "patches": [
            {"path": "calc/api.py", "original_sha256": sha256_bytes(api.read_bytes()),
             "new_content": api.read_text().replace("add_numbers", "add")}]}
        tid = c.post("/api/v1/tasks", json={"project_id": pid, "plan": plan}, headers=H(tok["alice"])).json()["id"]
        assert c.post(f"/api/v1/tasks/{tid}/advance", headers=H(tok["alice"])).json()["task"]["status"] == \
            "waiting_approval"
        ids.append((pid, tid))
    c.post(f"/api/v1/projects/{ids[0][0]}/members", json={"actor_ref": "human:bob", "role": "operator"},
           headers=H(tok["alice"]))
    return ids


def test_cross_project_isolation_on_every_surface(env):
    p, c, tok, tmp = env
    (p1, t1), (p2, t2) = _two_projects_with_pending_approvals(p, c, tok, tmp)
    bob = H(tok["bob"])
    assert [x["id"] for x in c.get("/api/v1/projects", headers=bob).json()] == [p1]
    for path in [f"/api/v1/projects/{p2}", f"/api/v1/projects/{p2}/graph", f"/api/v1/projects/{p2}/memory",
                 f"/api/v1/projects/{p2}/members", f"/api/v1/tasks/{t2}", f"/api/v1/tasks/{t2}/steps/scan"]:
        assert c.get(path, headers=bob).status_code == 403, path
    assert {t["id"] for t in c.get("/api/v1/tasks", headers=bob).json()} == {t1}
    ev = c.get("/api/v1/evidence?limit=1000", headers=bob).json()
    assert ev and {e["target"] for e in ev} == {p1}
    other = c.get(f"/api/v1/evidence?task_id={t2}", headers=H(tok["alice"])).json()[0]["id"]
    assert c.get(f"/api/v1/evidence/{other}", headers=bob).status_code == 403
    appr = c.get("/api/v1/approvals", headers=bob).json()
    assert [a["target"] for a in appr] == [p1]
    a2 = [a for a in c.get("/api/v1/approvals", headers=H(tok["alice"])).json() if a["target"] == p2][0]
    assert c.get(f"/api/v1/approvals/{a2['id']}", headers=bob).status_code == 403
    # bob is a global operator, but not on project two: he cannot approve its patch
    assert c.post(f"/api/v1/approvals/{a2['id']}", json={"approve": True}, headers=bob).status_code == 403
    assert c.post(f"/api/v1/tasks/{t2}/advance", headers=bob).status_code == 403
    assert c.post(f"/api/v1/tasks/{t2}/cancel", headers=bob).status_code == 403
    assert c.post("/api/v1/tasks", json={"project_id": p2}, headers=bob).status_code == 403
    assert c.post(f"/api/v1/projects/{p2}/scan", headers=bob).status_code == 403
    # ...while on project one he can
    a1 = appr[0]["id"]
    assert c.post(f"/api/v1/approvals/{a1}", json={"approve": True}, headers=bob).status_code == 200
    assert c.post(f"/api/v1/tasks/{t1}/advance", headers=bob).json()["task"]["status"] == "succeeded"
    # denials are audited
    audit = c.get("/api/v1/audit?limit=1000", headers=H(tok["alice"])).json()
    assert any(e["outcome"] == "denied" and e["details"].get("project_id") == p2 for e in audit)


def test_token_role_is_a_ceiling(env):
    p, c, tok, tmp = env
    root = make_passing_project(tmp / "p")
    pid = c.post("/api/v1/projects", json={"name": "p", "path": str(root)}, headers=H(tok["alice"])).json()["id"]
    # vic has a *viewer* token; an operator membership must not lift him above viewer
    c.post(f"/api/v1/projects/{pid}/members", json={"actor_ref": "human:vic", "role": "operator"},
           headers=H(tok["alice"]))
    assert c.get(f"/api/v1/projects/{pid}", headers=H(tok["vic"])).status_code == 200
    assert c.post("/api/v1/tasks", json={"project_id": pid}, headers=H(tok["vic"])).status_code == 403
    assert c.post(f"/api/v1/projects/{pid}/scan", headers=H(tok["vic"])).status_code == 403


def test_only_admins_manage_members_and_revocation_is_immediate(env):
    p, c, tok, tmp = env
    root = make_passing_project(tmp / "p")
    pid = c.post("/api/v1/projects", json={"name": "p", "path": str(root)}, headers=H(tok["alice"])).json()["id"]
    body = {"actor_ref": "human:bob", "role": "operator"}
    assert c.post(f"/api/v1/projects/{pid}/members", json=body, headers=H(tok["bob"])).status_code == 403
    assert c.post(f"/api/v1/projects/{pid}/members", json=body, headers=H(tok["alice"])).status_code == 201
    # bob (operator member) still cannot grant eve access
    eve = {"actor_ref": "human:eve", "role": "viewer"}
    assert c.post(f"/api/v1/projects/{pid}/members", json=eve, headers=H(tok["bob"])).status_code == 403
    assert c.get(f"/api/v1/projects/{pid}", headers=H(tok["bob"])).status_code == 200
    assert c.delete(f"/api/v1/projects/{pid}/members/human:bob", headers=H(tok["alice"])).status_code == 204
    assert c.get(f"/api/v1/projects/{pid}", headers=H(tok["bob"])).status_code == 403
    assert c.post(f"/api/v1/projects/{pid}/members", json={"actor_ref": "bob", "role": "viewer"},
                  headers=H(tok["alice"])).status_code == 422
    members = c.get(f"/api/v1/projects/{pid}/members", headers=H(tok["alice"])).json()
    assert [(m["actor_ref"], m["role"]) for m in members] == [("human:alice", "admin")]  # registrant is owner


def test_audit_is_admin_only(env):
    _, c, tok, _ = env
    for path in ["/api/v1/audit", "/api/v1/audit/verify", "/api/v1/integrity/verify"]:
        assert c.get(path, headers=H(tok["bob"])).status_code == 403
        assert c.get(path, headers=H(tok["alice"])).status_code == 200


def test_platform_layer_scoping_without_api(env):
    p, _, _, tmp = env
    alice = Actor(id="alice", kind=ActorKind.HUMAN, role=Role.ADMIN)
    bob = Actor(id="bob", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    proj = p.register_project(alice, "p", str(make_passing_project(tmp / "p")))
    with pytest.raises(PermissionDeniedError):
        p.scan_project(bob, proj.id)
    with pytest.raises(PermissionDeniedError):
        p.grant_member(bob, proj.id, "human:bob", Role.ADMIN)


def test_expired_and_revoked_tokens_rejected(env):
    p, c, _, _ = env
    tid, tok = p.tokens.issue("tmp", Role.VIEWER, ttl_seconds=3600)
    assert c.get("/api/v1/projects", headers=H(tok)).status_code == 200
    with p.db.transaction() as conn:
        conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00.000000+00:00' WHERE id = ?", (tid,))
    assert c.get("/api/v1/projects", headers=H(tok)).status_code == 401


@pytest.mark.parametrize("ttl", [0, 59, 367 * 86400])
def test_token_lifetime_bounds(env, ttl):
    with pytest.raises(InvalidInputError):
        env[0].tokens.issue("x", Role.VIEWER, ttl_seconds=ttl)


def test_migration_gives_legacy_tokens_an_expiry(tmp_path):
    from yemendev.storage.db import Database
    from yemendev.storage.migrations import MIGRATIONS
    db = Database(tmp_path / "legacy.sqlite3")
    import yemendev.storage.migrations as m
    saved = list(m.MIGRATIONS)
    try:
        m.MIGRATIONS[:] = saved[:1]
        db.migrate()
        with db.transaction() as conn:
            conn.execute("INSERT INTO api_tokens(id, token_hash, actor_id, role, created_at) VALUES"
                         " ('tok_old','h','old','admin','2026-01-01T00:00:00.000000+00:00')")
    finally:
        m.MIGRATIONS[:] = saved
    assert db.migrate() == [v for v, _, _ in MIGRATIONS[1:]] and db.schema_version() == MIGRATIONS[-1][0]
    with db.read() as conn:
        exp = conn.execute("SELECT expires_at FROM api_tokens WHERE id='tok_old'").fetchone()[0]
    days = (parse_iso(exp) - utcnow()).total_seconds() / 86400
    assert 89.9 < days <= 90.0, exp


def test_rate_limit_per_credential_and_auth_failure_lockout(tmp_path):
    s = Settings(data_dir=tmp_path / "data", rate_limit_per_minute=60, rate_limit_burst=3,
                 auth_failures_per_minute=2)
    p = Platform(s)
    _, a = p.tokens.issue("alice", Role.ADMIN)
    _, b = p.tokens.issue("bob", Role.VIEWER)
    clock = Clock()
    c = TestClient(create_app(p, clock=clock), raise_server_exceptions=False)
    codes = [c.get("/api/v1/projects", headers=H(a)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = c.get("/api/v1/projects", headers=H(a))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited"
    assert c.get("/api/v1/projects", headers=H(b)).status_code == 200  # independent bucket
    clock.t += 1.0
    assert c.get("/api/v1/projects", headers=H(a)).status_code == 200  # refilled
    # failed authentication: after the budget is spent the client is locked out, even with a valid token
    assert [c.get("/api/v1/projects", headers=H("ydv_bogus" + str(i) * 30)).status_code for i in range(2)] == \
        [401, 401]
    assert c.get("/api/v1/projects", headers=H(b)).status_code == 429
    clock.t += 60
    assert c.get("/api/v1/projects", headers=H(b)).status_code == 200
