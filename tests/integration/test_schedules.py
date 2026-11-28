"""Stage 24: ORION recurring schedules - real scans, queued repairs, backups and readiness checks, acting as the
human who created the schedule, with rights re-checked at every run."""

import threading
import time
from datetime import timedelta

import pytest
from end_to_end.conftest import ALICE, BOB
from fastapi.testclient import TestClient
from helpers import make_renamed_api_project
from pydantic import ValidationError

from yemendev.api.app import create_app
from yemendev.core.contracts import InvalidInputError, PermissionDeniedError, iso, utcnow
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.orion.scheduler import Scheduler
from yemendev.orion.schedules import ScheduleCreate, ScheduleKind, Schedules, parse_interval
from yemendev.orion.workflow import RepairWorkflow

HOUR = 3600


@pytest.fixture
def env(platform, tmp_path):
    """alice (admin) and bob (operator, project member) with active tokens; one registered project."""
    alice_tid, alice_tok = platform.tokens.issue("alice", Role.ADMIN)
    _, bob_tok = platform.tokens.issue("bob", Role.OPERATOR)
    pid = platform.register_project(ALICE, "calc", str(make_renamed_api_project(tmp_path / "calc"))).id
    platform.grant_member(ALICE, pid, BOB.ref, Role.OPERATOR)
    return platform, pid, {"alice": alice_tok, "bob": bob_tok, "alice_tid": alice_tid}


def make(p, actor, kind, pid=None, every=HOUR, first=0):
    return p.schedules.create(actor, ScheduleCreate(kind=ScheduleKind(kind), project_id=pid, interval_seconds=every,
                                                    first_run_in_seconds=first))


def test_interval_parsing_and_limits():
    assert parse_interval("30m") == 1800 and parse_interval("6h") == 21600 and parse_interval("1d") == 86400
    for bad in ("1m", "299", "91d", "soon", ""):
        with pytest.raises(InvalidInputError):
            parse_interval(bad)
    with pytest.raises(ValidationError):
        ScheduleCreate(kind=ScheduleKind.SCAN, project_id="x", interval_seconds=60)


def test_who_may_schedule_what(env):
    p, pid, _ = env
    viewer = Actor(id="vic", kind=ActorKind.HUMAN, role=Role.VIEWER)
    outsider = Actor(id="olga", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    agent = Actor(id="planning", kind=ActorKind.AGENT, role=Role.ADMIN)
    with pytest.raises(PermissionDeniedError):
        make(p, viewer, "scan", pid)              # scanning needs operator
    with pytest.raises(PermissionDeniedError):
        make(p, outsider, "scan", pid)            # not a member of the project
    with pytest.raises(PermissionDeniedError):
        make(p, agent, "readiness")               # agents never schedule work
    with pytest.raises(PermissionDeniedError):
        make(p, BOB, "backup")                    # backups are admin-only
    with pytest.raises(InvalidInputError):
        make(p, BOB, "scan")                      # project kinds need a project
    with pytest.raises(InvalidInputError):
        make(p, ALICE, "backup", pid)             # platform kinds take none
    s = make(p, BOB, "scan", pid)
    assert s.created_by == "human:bob" and s.created_role is Role.OPERATOR and s.enabled
    assert [x.id for x in p.schedules.visible(BOB)] == [s.id]
    assert p.schedules.visible(outsider) == []    # project schedules are only visible to project readers


def test_due_scan_runs_as_its_creator_and_is_retimed(env):
    p, pid, _ = env
    s = make(p, BOB, "scan", pid)
    [r] = p.schedules.run_due()
    assert r["status"] == "ok" and "modules scanned" in r["detail"]
    after = p.schedules.get(s.id)
    assert after.runs == 1 and after.last_status == "ok" and after.failures == 0
    assert after.next_run_at > iso(utcnow() + timedelta(seconds=3500))  # re-timed one interval from the run
    assert p.latest_scan(pid)[0] is not None
    audit = [e for e in p.audit.list(limit=500) if e.target == s.id]
    assert {e.action for e in audit} == {"schedule.create", "schedule.run.scan"}
    assert next(e for e in audit if e.action == "schedule.run.scan").actor == "human:bob"
    assert p.schedules.run_due() == []  # not due again yet


def test_each_occurrence_runs_once_even_with_many_workers(env):
    p, pid, _ = env
    s = make(p, BOB, "scan", pid)
    results: list[list[dict]] = []
    workers = [Schedules(p) for _ in range(6)]
    barrier = threading.Barrier(len(workers))

    def go(w):
        barrier.wait()
        results.append(w.run_due())

    threads = [threading.Thread(target=go, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sum(len(r) for r in results) == 1 and p.schedules.get(s.id).runs == 1


def test_no_catch_up_storm_after_downtime(env):
    p, pid, _ = env
    s = make(p, ALICE, "readiness")
    with p.db.transaction() as conn:  # the platform was off for ten days
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=10)), s.id))
    assert len(p.schedules.run_due()) == 1 and p.schedules.run_due() == []
    assert p.schedules.get(s.id).runs == 1


def test_creator_without_an_active_token_disables_the_schedule(env):
    p, pid, toks = env
    s = make(p, ALICE, "readiness")
    p.tokens.revoke(toks["alice_tid"])
    [r] = p.schedules.run_due()
    assert r["status"] == "dropped"
    after = p.schedules.get(s.id)
    assert not after.enabled and "no active token" in after.disabled_reason and after.failures == 1
    with pytest.raises(PermissionDeniedError):
        p.schedules.set_enabled(BOB, s.id, True)  # not the creator, not an admin


def test_lost_rights_disable_the_schedule_instead_of_running(env):
    p, pid, toks = env
    s = make(p, ALICE, "backup")
    p.tokens.revoke(toks["alice_tid"])
    p.tokens.issue("alice", Role.OPERATOR)  # alice still works here, but is no longer an admin
    [r] = p.schedules.run_due()
    assert r["status"] == "denied" and "not allowed any more" in r["detail"]
    assert not p.schedules.get(s.id).enabled and p.list_backups(ALICE) == []


def test_backup_schedule_creates_and_verifies(env):
    p, pid, _ = env
    make(p, ALICE, "backup")
    [r] = p.schedules.run_due()
    assert r["status"] == "ok" and "created and verified" in r["detail"]
    [b] = p.list_backups(ALICE)
    assert b["verified_ok"] is True


def test_repair_schedule_queues_work_and_never_piles_it_up(env):
    p, pid, _ = env
    s = make(p, BOB, "repair", pid)
    [r] = p.schedules.run_due()
    assert r["status"] == "ok" and "queued" in r["detail"] and "approval" in r["detail"]
    [e] = RepairWorkflow(p).queue.list()
    assert e.project_id == pid and e.enqueued_by == "human:bob"
    with p.db.transaction() as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow()), s.id))
    [r] = p.schedules.run_due()
    assert r["status"] == "skipped" and len(RepairWorkflow(p).queue.list()) == 1


def test_background_workers_run_due_schedules(env):
    p, pid, _ = env
    s = make(p, BOB, "scan", pid)
    sched = Scheduler(p)
    sched.start(workers=2)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and p.schedules.get(s.id).runs == 0:
            time.sleep(0.5)
    finally:
        sched.stop()
    assert p.schedules.get(s.id).runs == 1 and p.schedules.get(s.id).last_status == "ok"


def test_overdue_schedules_show_in_recovery_readiness(env):
    p, pid, _ = env
    s = make(p, ALICE, "readiness")
    checks = {c["check"]: c for c in p.recovery_readiness(ALICE)["checks"]}
    assert checks["scheduled work"]["status"] == "ok"
    with p.db.transaction() as conn:  # no worker has run it for a long time
        conn.execute("UPDATE schedules SET created_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=3)), s.id))
    checks = {c["check"]: c for c in p.recovery_readiness(ALICE)["checks"]}
    assert checks["scheduled work"]["status"] == "warn" and "overdue" in checks["scheduled work"]["detail"]


def test_schedules_api(env):
    p, pid, toks = env
    c = TestClient(create_app(p), raise_server_exceptions=False)
    H = lambda t: {"Authorization": f"Bearer {toks[t]}"}  # noqa: E731
    assert c.get("/api/v1/schedules").status_code == 401
    r = c.post("/api/v1/schedules", json={"kind": "scan", "project_id": pid, "interval_seconds": 3600},
               headers=H("bob"))
    assert r.status_code == 201 and r.json()["created_by"] == "human:bob"
    sid = r.json()["id"]
    assert c.post("/api/v1/schedules", json={"kind": "scan", "project_id": pid, "interval_seconds": 10},
                  headers=H("bob")).status_code == 422
    assert c.post("/api/v1/schedules", json={"kind": "backup", "interval_seconds": 3600},
                  headers=H("bob")).status_code == 403
    rows = c.get("/api/v1/schedules", headers=H("bob")).json()
    assert [x["id"] for x in rows] == [sid] and rows[0]["overdue"] is False
    assert c.post(f"/api/v1/schedules/{sid}/disable", headers=H("bob")).json()["enabled"] is False
    assert c.post(f"/api/v1/schedules/{sid}/enable", headers=H("alice")).json()["enabled"] is True  # admin
    assert c.delete(f"/api/v1/schedules/{sid}", headers=H("bob")).status_code == 204
    assert c.get("/api/v1/schedules", headers=H("alice")).json() == []
