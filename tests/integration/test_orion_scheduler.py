"""ORION background execution (stage 17b): real child processes, real sandbox runs, real kills."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from end_to_end.conftest import ALICE, BOB
from end_to_end.test_mvp_workflow import human_fix, pending_approval
from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.config import OrionSettings, SandboxSettings, Settings
from yemendev.core.faults import FaultInjector, InjectedFault
from yemendev.governance.actors import Role
from yemendev.orion.queue import QueueOptions, QueueState
from yemendev.orion.scheduler import Scheduler
from yemendev.orion.tasks import StepStatus, TaskStatus
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform

FAST = OrionSettings(retry_base_seconds=0.2, retry_max_seconds=1, heartbeat_seconds=3, poll_seconds=0.2)


def make_platform(tmp_path: Path, faults=None, **sandbox) -> Platform:
    sb = SandboxSettings(timeout_seconds=60, **{"backend": "process", **sandbox})
    # default workspace location: it must be traversable by the unprivileged sandbox user (tmp_path is 0700)
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=sb, orion=FAST), faults or FaultInjector())
    p.tokens.issue("alice", Role.ADMIN)
    p.tokens.issue("bob", Role.OPERATOR)
    return p


def make_sleepy_project(root: Path) -> Path:
    make_passing_project(root)
    write(root, "tests/test_slow.py", "import time\n\n\ndef test_slow():\n    time.sleep(120)\n")
    return root


def sandbox_pids(wf: RepairWorkflow, task_id: str) -> list[int]:
    """Processes whose working directory is inside the workspace of this task's current queue run."""
    claims = [e for e in wf.tasks.events(task_id) if e.kind == "queue.claimed"]
    if not claims:
        return []
    run_dir = str(wf.p.settings.workspaces_dir / "runs" / claims[-1].detail["run_id"])
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if os.readlink(d / "cwd").startswith(run_dir):
                    out.append(int(d.name))
            except OSError:
                continue
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]


def run_in_thread(sched: Scheduler, results: list) -> threading.Thread:
    t = threading.Thread(target=lambda: results.append(sched.run_once("test-worker")))
    t.start()
    return t


def wait_for(cond, timeout: float = 60.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def claimed_run_id(wf: RepairWorkflow, task_id: str) -> str:
    return [e for e in wf.tasks.events(task_id) if e.kind == "queue.claimed"][-1].detail["run_id"]


def test_queued_repair_runs_in_background_waits_for_approval_then_succeeds(tmp_path):
    p = make_platform(tmp_path)
    sched = Scheduler(p)
    wf = sched.wf
    root = make_renamed_api_project(tmp_path / "proj")
    proj = p.register_project(ALICE, "calc", str(root))
    t = wf.create(ALICE, proj.id, human_plan=human_fix(root))
    wf.enqueue(ALICE, t.id, QueueOptions())

    [first] = sched.drain()
    assert first.outcome == "finished" and first.task_status == "waiting_approval", first
    assert wf.queue.get(t.id).state is QueueState.WAITING
    assert sched.drain() == []  # nothing to do until a human decides

    p.approvals.decide(pending_approval(p, t.id).id, BOB, approve=True)
    [second] = sched.drain()
    assert second.task_status == "succeeded", second
    assert "add_numbers" not in (root / "calc/api.py").read_text()
    assert wf.queue.get(t.id).state is QueueState.DONE

    finished = [e for e in wf.tasks.events(t.id) if e.kind == "queue.run_finished"]
    assert len(finished) == 2
    assert all(e.detail["cpu_seconds"] > 0 and e.detail["max_rss_mb"] > 0 for e in finished)
    claims = [e for e in wf.tasks.events(t.id) if e.kind == "queue.claimed"]
    assert [c.detail["acting_as"] for c in claims] == ["human:alice", "human:alice"]
    assert not any((p.settings.data_dir / "runs").iterdir())  # run directories are cleaned up
    assert p.verify_integrity()["valid"]


def test_timeout_kills_the_run_and_the_sandboxed_program(tmp_path):
    p = make_platform(tmp_path)
    sched = Scheduler(p)
    wf = sched.wf
    root = make_sleepy_project(tmp_path / "proj")
    t = wf.create(ALICE, p.register_project(ALICE, "slow", str(root)).id)
    wf.enqueue(ALICE, t.id, QueueOptions(timeout_seconds=6, max_attempts=1))
    results: list = []
    started = time.monotonic()
    th = run_in_thread(sched, results)
    pids = wait_for(lambda: sandbox_pids(wf, t.id), what="sandboxed pytest")
    th.join(60)
    elapsed = time.monotonic() - started
    [out] = results
    assert out.outcome == "gave_up" and out.task_status == "failed", out
    assert elapsed < 20  # the 120 s test was cut off by the 6 s budget
    assert not any(alive(pid) for pid in pids), "sandboxed program survived the killed run"
    task = wf.tasks.get(t.id)
    assert "timed out after 6s in step baseline" in task.error
    assert wf.tasks.step(t.id, "baseline").status is StepStatus.FAILED
    assert not (p.settings.workspaces_dir / "runs" / claimed_run_id(wf, t.id)).exists()
    kinds = [e.kind for e in wf.tasks.events(t.id)]
    assert "queue.gave_up" in kinds and kinds[-1] == "task.status"


def test_cancel_kills_a_step_that_does_not_touch_the_project(tmp_path):
    p = make_platform(tmp_path)
    sched = Scheduler(p)
    wf = sched.wf
    root = make_sleepy_project(tmp_path / "proj")
    before = {f: f.read_bytes() for f in root.rglob("*.py")}
    proj = p.register_project(ALICE, "slow", str(root))
    p.grant_member(ALICE, proj.id, BOB.ref, Role.OPERATOR)  # bob may cancel tasks of this project
    t = wf.create(ALICE, proj.id)
    wf.enqueue(ALICE, t.id, QueueOptions(timeout_seconds=300))
    results: list = []
    th = run_in_thread(sched, results)
    pids = wait_for(lambda: sandbox_pids(wf, t.id), what="sandboxed pytest")
    asked = time.monotonic()
    assert wf.cancel(BOB, t.id).cancel_requested_by == "human:bob"
    th.join(30)
    [out] = results
    assert out.outcome == "cancelled" and out.task_status == "cancelled", out
    assert time.monotonic() - asked < 10  # did not wait for the 120 s step
    assert not any(alive(pid) for pid in pids)
    assert {f: f.read_bytes() for f in root.rglob("*.py")} == before
    assert wf.queue.get(t.id).state is QueueState.CANCELLED
    assert "cancelled by human:bob during step baseline" in wf.tasks.get(t.id).error


def test_cancel_after_the_change_was_applied_rolls_it_back(platform, wf, tmp_path, monkeypatch):
    root = make_renamed_api_project(tmp_path / "proj")
    original = (root / "calc/api.py").read_text()
    t = wf.create(ALICE, platform.register_project(ALICE, "calc", str(root)).id, human_plan=human_fix(root))
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL
    platform.approvals.decide(pending_approval(platform, t.id).id, BOB, approve=True)
    real_execute = wf._execute

    def execute_then_cancel_arrives(actor, task):  # a cancel request lands while `execute` runs
        out = real_execute(actor, task)
        wf.tasks.request_cancel(task.id, BOB.ref)
        return out

    monkeypatch.setattr(wf, "_execute", execute_then_cancel_arrives)
    r = wf.advance(ALICE, t.id)
    assert r.ran_steps == ["authorize", "execute", "rollback"]
    assert r.task.status is TaskStatus.CANCELLED and "rolled back" in r.message
    assert (root / "calc/api.py").read_text() == original
    ev = [e for e in platform.evidence.list(task_id=t.id) if e.action == "task.cancelled"]
    assert ev and ev[-1].body.details["applied_change_rolled_back"] is True


def test_terminate_never_leaves_a_half_applied_change(tmp_path):
    faults = FaultInjector()
    p = make_platform(tmp_path, faults)
    wf = RepairWorkflow(p)
    root = make_renamed_api_project(tmp_path / "proj")
    original = (root / "calc/api.py").read_bytes()
    plan = human_fix(root)
    t = wf.create(ALICE, p.register_project(ALICE, "calc", str(root)).id, human_plan=plan)
    wf.advance(ALICE, t.id)
    p.approvals.decide(pending_approval(p, t.id).id, BOB, approve=True)
    faults.arm("recovery.after_file_write")  # dies mid-apply, like a run killed on timeout
    with pytest.raises(InjectedFault):
        wf.advance(ALICE, t.id)
    assert (root / "calc/api.py").read_bytes() != original  # the change is (partly) on disk
    done = wf.terminate(t.id, TaskStatus.FAILED, "timed out after 5s in step execute")
    assert done.status is TaskStatus.FAILED
    assert (root / "calc/api.py").read_bytes() == original
    rb = [e for e in p.evidence.list(task_id=t.id) if e.action == "repair.rollback"]
    assert rb and rb[-1].body.rollback["reason"] == "task ended before completing"


def test_transient_failures_are_retried_with_backoff_then_the_task_fails(tmp_path):
    p = make_platform(tmp_path, backend="docker", docker_binary=str(tmp_path / "no-such-docker"))
    sched = Scheduler(p)
    wf = sched.wf
    t = wf.create(ALICE, p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "proj"))).id)
    wf.enqueue(ALICE, t.id, QueueOptions(max_attempts=2))
    [first] = sched.drain()
    assert first.outcome == "retry_scheduled" and first.task_status == "pending", first
    assert sched.drain() == []  # backing off
    time.sleep(0.4)
    [second] = sched.drain()
    assert second.outcome == "gave_up" and second.task_status == "failed"
    task = wf.tasks.get(t.id)
    assert task.error.startswith("gave up after 2 attempt(s): transient failure") and "docker" in task.error
    retry = [e for e in wf.tasks.events(t.id) if e.kind == "queue.retry_scheduled"]
    assert len(retry) == 1 and retry[0].detail["delay_seconds"] == 0.2
    assert wf.tasks.step(t.id, "baseline").attempt == 2  # resumed at the failed step, not from scratch
    assert wf.tasks.step(t.id, "scan").attempt == 1


def test_transient_error_returns_a_manually_advanced_task_to_pending(tmp_path):
    p = make_platform(tmp_path, backend="docker", docker_binary=str(tmp_path / "no-such-docker"))
    wf = RepairWorkflow(p)
    t = wf.create(ALICE, p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "proj"))).id)
    r = wf.advance(ALICE, t.id)
    assert r.retryable and r.task.status is TaskStatus.PENDING and r.task.error.startswith("transient:")
    assert wf.tasks.step(t.id, "baseline").status is StepStatus.FAILED
    assert any(e.action == "task.transient_failure" for e in p.evidence.list(task_id=t.id))


def test_a_run_that_lost_its_claim_stops_before_the_next_step(platform, wf, tmp_path):
    t = wf.create(ALICE, platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "p"))).id)
    calls = iter([True, False])
    r = wf.advance(ALICE, t.id, still_owner=lambda: next(calls))
    assert r.ran_steps == ["scan"] and "claim lost" in r.message
    assert wf.tasks.step(t.id, "baseline") is None


def test_shutdown_hands_the_run_back_without_using_an_attempt(tmp_path):
    p = make_platform(tmp_path)
    sched = Scheduler(p)
    wf = sched.wf
    root = make_sleepy_project(tmp_path / "proj")
    t = wf.create(ALICE, p.register_project(ALICE, "slow", str(root)).id)
    wf.enqueue(ALICE, t.id, QueueOptions(max_attempts=1))
    sched.start(workers=1)
    pids = wait_for(lambda: sandbox_pids(wf, t.id), what="sandboxed pytest")
    sched.stop(timeout=30)
    assert not sched.running
    assert not any(alive(pid) for pid in pids)
    e = wf.queue.get(t.id)
    assert e.state is QueueState.QUEUED and e.attempts == 0
    assert wf.queue.claim("next") is not None  # the dead run's lease does not block the task


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", SandboxSettings().docker_image], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon or sandbox image not available")
def test_timeout_with_the_docker_backend_removes_the_runs_containers(tmp_path):
    p = make_platform(tmp_path, backend="docker")
    sched = Scheduler(p)
    wf = sched.wf
    t = wf.create(ALICE, p.register_project(ALICE, "slow", str(make_sleepy_project(tmp_path / "proj"))).id)
    wf.enqueue(ALICE, t.id, QueueOptions(timeout_seconds=10, max_attempts=1))
    results: list = []
    th = run_in_thread(sched, results)
    run_id = wait_for(lambda: [e for e in wf.tasks.events(t.id) if e.kind == "queue.claimed"], what="claim")[0]
    run_id = run_id.detail["run_id"]

    def containers() -> list[str]:
        return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=org.yemendev.run={run_id}"],
                              capture_output=True, text=True).stdout.split()

    wait_for(containers, timeout=60, what="sandbox container of this run")
    th.join(90)
    [out] = results
    assert out.outcome == "gave_up", out
    assert containers() == []
    gave_up = [e for e in wf.tasks.events(t.id) if e.kind == "queue.gave_up"][-1]
    assert gave_up.detail["containers_removed"] >= 1


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon or sandbox image not available")
def test_a_transient_runtime_outage_recovers_on_retry(tmp_path):
    state = tmp_path / "docker-state"
    state.write_text("down")
    wrapper = tmp_path / "docker"
    wrapper.write_text(f'#!/bin/sh\n[ "$(cat {state})" = up ] || exit 1\nexec {shutil.which("docker")} "$@"\n')
    wrapper.chmod(0o755)
    p = make_platform(tmp_path, backend="docker", docker_binary=str(wrapper))
    sched = Scheduler(p)
    wf = sched.wf
    t = wf.create(ALICE, p.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "proj"))).id)
    wf.enqueue(ALICE, t.id, QueueOptions(max_attempts=3))
    [first] = sched.drain()
    assert first.outcome == "retry_scheduled"
    state.write_text("up")  # the container runtime comes back
    time.sleep(0.4)
    [second] = sched.drain()
    assert second.outcome == "finished" and second.task_status == "no_action", second  # healthy project
    assert wf.queue.get(t.id).state is QueueState.DONE
