"""ORION queue (stage 17a): scheduling order, dependencies, resource allocation, rights, recovery, trace."""

import pytest
from helpers import make_passing_project

from yemendev.core.config import OrionSettings
from yemendev.core.contracts import ConflictError, InvalidInputError, iso
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.policy import Action
from yemendev.orion.queue import QueueOptions, QueueState, TaskQueue
from yemendev.orion.tasks import TaskStatus, TaskStore

ALICE = Actor(id="alice", kind=ActorKind.HUMAN, role=Role.ADMIN)
BOB = Actor(id="bob", kind=ActorKind.HUMAN, role=Role.ADMIN)


@pytest.fixture
def env(platform, tmp_path):
    platform.tokens.issue("alice", Role.ADMIN)
    platform.tokens.issue("bob", Role.ADMIN)
    tasks = TaskStore(platform.db)

    def task(name: str, project=None) -> str:
        if project is None:
            project = platform.register_project(ALICE, name, str(make_passing_project(tmp_path / name))).id
        return tasks.create(project, "repair", ALICE.ref, {}).id

    def queue(**kw) -> TaskQueue:
        return TaskQueue(platform.db, platform.tokens, OrionSettings(**kw))

    return platform, tasks, task, queue


def test_claim_order_priority_then_time_then_fifo_and_delays_are_respected(env):
    _, _, task, queue = env
    q = queue()
    low1, high, low2, later = task("a"), task("b"), task("c"), task("d")
    q.enqueue(low1, ALICE, QueueOptions())
    q.enqueue(high, ALICE, QueueOptions(priority=5))
    q.enqueue(low2, ALICE, QueueOptions())
    q.enqueue(later, ALICE, QueueOptions(priority=10, delay_seconds=3600))
    order = [q.claim("w").entry.task_id for _ in range(3)]
    assert order == [high, low1, low2]
    assert q.claim("w") is None  # the delayed one is not due yet, whatever its priority


def test_one_run_per_project_and_global_concurrency_limit(env):
    platform, tasks, task, queue = env
    q = queue(max_concurrent_runs=2)
    a1 = task("p1")
    a2 = task("ignored", project=tasks.get(a1).project_id)
    b1, c1 = task("p2"), task("p3")
    for t in (a1, a2, b1, c1):
        q.enqueue(t, ALICE, QueueOptions())
    first = q.claim("w1")
    assert first.entry.task_id == a1
    second = q.claim("w2")
    assert second.entry.task_id == b1  # a2 shares a1's project -> skipped, not blocked behind it
    assert q.claim("w3") is None  # global limit 2 reached
    q.finish_run(first.entry.id, first.run_id, TaskStatus.NO_ACTION, {})
    assert q.claim("w3").entry.task_id == a2  # project free again, and a slot is free


def test_project_busy_while_a_human_advances_a_task_by_hand(env):
    _, tasks, task, queue = env
    q = queue()
    manual = task("p")
    queued = task("x", project=tasks.get(manual).project_id)
    tasks.acquire(manual, "human-advance")
    q.enqueue(queued, ALICE, QueueOptions())
    assert q.claim("w") is None
    tasks.release(manual, TaskStatus.NO_ACTION)
    assert q.claim("w").entry.task_id == queued


def test_dependencies_wait_then_run_or_cancel_with_reason(env):
    _, tasks, task, queue = env
    q = queue()
    a, b, c = task("a"), task("b"), task("c")
    q.enqueue(a, ALICE, QueueOptions())
    q.enqueue(b, ALICE, QueueOptions(depends_on=[a], priority=9))
    run = q.claim("w")
    assert run.entry.task_id == a  # b has higher priority but must wait for a
    assert q.claim("w") is None
    tasks.release(a, TaskStatus.NO_ACTION)
    q.finish_run(run.entry.id, run.run_id, TaskStatus.NO_ACTION, {})
    assert q.claim("w").entry.task_id == b  # no_action counts as a healthy outcome

    tasks.cancel(c)
    d = task("d")
    q.enqueue(d, ALICE, QueueOptions(depends_on=[c]))
    assert q.claim("w") is None
    assert q.get(d).state is QueueState.CANCELLED
    t = tasks.get(d)
    assert t.status is TaskStatus.CANCELLED and t.error == f"dependency {c} ended cancelled"


def test_invalid_dependencies_are_rejected(env):
    _, _, task, queue = env
    q = queue()
    a, b = task("a"), task("b")
    with pytest.raises(InvalidInputError, match="itself"):
        q.enqueue(a, ALICE, QueueOptions(depends_on=[a]))
    with pytest.raises(InvalidInputError, match="does not exist"):
        q.enqueue(a, ALICE, QueueOptions(depends_on=["tsk_missing"]))
    q.enqueue(a, ALICE, QueueOptions(depends_on=[b]))
    with pytest.raises(InvalidInputError, match="cycle"):
        q.enqueue(b, ALICE, QueueOptions(depends_on=[a]))


def test_enqueue_guards(env):
    _, tasks, task, queue = env
    q = queue()
    a = task("a")
    q.enqueue(a, ALICE, QueueOptions())
    with pytest.raises(ConflictError, match="already queued"):
        q.enqueue(a, ALICE, QueueOptions())
    b = task("b")
    tasks.cancel(b)
    with pytest.raises(ConflictError, match="already cancelled"):
        q.enqueue(b, ALICE, QueueOptions())
    agent = Actor(id="planning-agent", kind=ActorKind.AGENT, role=Role.OPERATOR)
    with pytest.raises(InvalidInputError, match="only humans"):
        q.enqueue(task("c"), agent, QueueOptions())


def test_runs_act_with_the_enqueuers_current_rights_and_stop_when_revoked(env):
    platform, _, task, queue = env
    q = queue()
    tid, _ = platform.tokens.issue("carol", Role.OPERATOR)
    carol_as_admin = Actor(id="carol", kind=ActorKind.HUMAN, role=Role.ADMIN)
    a, b = task("a"), task("b")
    q.enqueue(a, carol_as_admin, QueueOptions())
    claim = q.claim("w")
    assert claim.actor.role is Role.OPERATOR  # never more than the human's currently active tokens allow
    q.enqueue(b, carol_as_admin, QueueOptions())
    platform.tokens.revoke(tid)
    assert q.claim("w") is None
    e = q.get(b)
    assert e.state is QueueState.DROPPED and "no active token" in e.last_error


def test_waiting_task_reruns_only_after_the_human_decision(env):
    platform, tasks, task, queue = env
    q = queue()
    a = task("a")
    q.enqueue(a, ALICE, QueueOptions())
    run = q.claim("w")
    apr = platform.approvals.request(action=Action.APPLY_PATCH, target="x", operation_digest="d", summary="s",
                                     requested_by=ALICE, task_id=a)
    tasks.acquire(a, run.run_id)
    tasks.release(a, TaskStatus.WAITING_APPROVAL)
    assert q.finish_run(run.entry.id, run.run_id, TaskStatus.WAITING_APPROVAL, {}).state is QueueState.WAITING
    assert q.claim("w") is None
    platform.approvals.decide(apr.id, BOB, approve=True)
    assert q.claim("w").entry.task_id == a


def test_stale_claims_are_recovered_and_count_as_attempts(env):
    platform, tasks, task, queue = env
    q = queue()
    a, b = task("a"), task("b")
    q.enqueue(a, ALICE, QueueOptions(max_attempts=2))
    q.enqueue(b, ALICE, QueueOptions(max_attempts=1))
    ca, _ = q.claim("dead-worker"), q.claim("dead-worker")
    tasks.acquire(a, ca.run_id)
    with platform.db.transaction() as conn:
        conn.execute("UPDATE task_queue SET heartbeat_until = ?", ("2000-01-01T00:00:00.000000+00:00",))
    recovered = {e.task_id: e for e in q.recover_stale()}
    assert q.heartbeat(ca.entry.id, ca.run_id) is False  # once recovered, the old claim is dead for good
    assert recovered[a].state is QueueState.QUEUED and recovered[a].attempts == 1
    assert recovered[b].state is QueueState.FAILED  # out of attempts
    again = q.claim("w2")
    assert again.entry.task_id == a and again.entry.attempts == 2
    tasks.acquire(a, again.run_id)  # the task's lease was expired, so the new run can resume it
    kinds = [e.kind for e in tasks.events(a)]
    assert kinds.count("queue.claimed") == 2 and "queue.recovered" in kinds


def test_heartbeat_extends_only_the_current_claim(env):
    _, _, task, queue = env
    q = queue()
    a = task("a")
    q.enqueue(a, ALICE, QueueOptions())
    c = q.claim("w")
    assert q.heartbeat(c.entry.id, c.run_id) is True
    assert q.heartbeat(c.entry.id, "run_other") is False


def test_retry_backoff_is_exponential_capped_and_gives_up(env):
    _, _, task, queue = env
    q = queue(retry_base_seconds=2, retry_max_seconds=5)
    assert [q.backoff(n) for n in (1, 2, 3, 4)] == [2, 4, 5, 5]
    a = task("a")
    q.enqueue(a, ALICE, QueueOptions(max_attempts=2))
    c = q.claim("w")
    e, gave_up = q.retry_or_fail(c.entry.id, c.run_id, "sandbox unavailable", {})
    assert not gave_up and e.state is QueueState.QUEUED and e.run_after > iso()
    with pytest.raises(ConflictError):  # the old claim cannot report twice
        q.retry_or_fail(c.entry.id, c.run_id, "again", {})


def test_cancel_removes_queued_entries_and_trace_records_everything(env):
    _, tasks, task, queue = env
    q = queue()
    a = task("a")
    q.enqueue(a, ALICE, QueueOptions(priority=3))
    assert q.cancel(a, BOB.ref, "not needed").state is QueueState.CANCELLED
    assert q.claim("w") is None
    ev = tasks.events(a)
    assert [e.kind for e in ev] == ["task.created", "queue.enqueued", "queue.cancelled"]
    assert ev[1].detail["priority"] == 3 and ev[2].actor == BOB.ref and ev[2].detail["reason"] == "not needed"
