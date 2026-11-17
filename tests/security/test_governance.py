import pytest

from yemendev.core.contracts import ConflictError, PermissionDeniedError, PreconditionFailedError
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.approvals import ApprovalService, ApprovalStatus
from yemendev.governance.policy import Action, PolicyEngine

ALICE = Actor(id="alice", kind=ActorKind.HUMAN, role=Role.OPERATOR)
BOB = Actor(id="bob", kind=ActorKind.HUMAN, role=Role.OPERATOR)
VIEWER = Actor(id="v", kind=ActorKind.HUMAN, role=Role.VIEWER)
AGENT = Actor(id="recovery-agent", kind=ActorKind.AGENT, role=Role.ADMIN)


@pytest.fixture
def svc(db):
    return ApprovalService(db, PolicyEngine(), ttl_seconds=3600)


def _req(svc, who=AGENT, digest="d1"):
    return svc.request(action=Action.APPLY_PATCH, target="prj_1", operation_digest=digest,
                       summary="apply fix", requested_by=who)


def test_policy_role_and_agent_restrictions():
    p = PolicyEngine()
    assert not p.evaluate(VIEWER, Action.APPLY_PATCH).allowed
    assert p.evaluate(ALICE, Action.APPLY_PATCH).requires_approval
    assert not p.evaluate(AGENT, Action.DECIDE_APPROVAL).allowed  # even an ADMIN-role agent
    assert not p.evaluate(AGENT, Action.REGISTER_PROJECT).allowed
    assert p.evaluate(VIEWER, Action.READ).allowed


def test_approval_happy_path_is_single_use(svc):
    r = _req(svc)
    svc.decide(r.id, ALICE, approve=True)
    c = svc.consume(r.id, action=Action.APPLY_PATCH, target="prj_1", operation_digest="d1")
    assert c.status is ApprovalStatus.CONSUMED
    with pytest.raises(PreconditionFailedError):
        svc.consume(r.id, action=Action.APPLY_PATCH, target="prj_1", operation_digest="d1")


@pytest.mark.parametrize("field", ["action", "target", "digest"])
def test_approval_bound_to_operation(svc, field):
    r = _req(svc)
    svc.decide(r.id, ALICE, approve=True)
    kw = {"action": Action.APPLY_PATCH, "target": "prj_1", "operation_digest": "d1"}
    if field == "action":
        kw["action"] = Action.ACTIVATE_SKILL
    elif field == "target":
        kw["target"] = "prj_OTHER"
    else:
        kw["operation_digest"] = "tampered"
    with pytest.raises(PermissionDeniedError):
        svc.consume(r.id, **kw)
    # still usable for the right operation
    svc.consume(r.id, action=Action.APPLY_PATCH, target="prj_1", operation_digest="d1")


def test_no_self_approval_and_agents_cannot_decide(svc):
    r = _req(svc, who=ALICE)
    with pytest.raises(PermissionDeniedError):
        svc.decide(r.id, ALICE, approve=True)
    with pytest.raises(PermissionDeniedError):
        svc.decide(r.id, AGENT, approve=True)
    with pytest.raises(PermissionDeniedError):
        svc.decide(r.id, VIEWER, approve=True)
    svc.decide(r.id, BOB, approve=True)


def test_denied_cannot_be_consumed_or_redecided(svc):
    r = _req(svc)
    svc.decide(r.id, ALICE, approve=False, reason="no")
    with pytest.raises(PreconditionFailedError):
        svc.consume(r.id, action=Action.APPLY_PATCH, target="prj_1", operation_digest="d1")
    with pytest.raises(ConflictError):
        svc.decide(r.id, BOB, approve=True)


def test_expired_approval_cannot_be_used(svc, db):
    r = _req(svc)
    svc.decide(r.id, ALICE, approve=True)
    with db.transaction() as conn:
        conn.execute("UPDATE approval_requests SET expires_at='2000-01-01T00:00:00.000000+00:00' WHERE id=?",
                     (r.id,))
    assert svc.get(r.id).status is ApprovalStatus.EXPIRED
    with pytest.raises(PreconditionFailedError):
        svc.consume(r.id, action=Action.APPLY_PATCH, target="prj_1", operation_digest="d1")
