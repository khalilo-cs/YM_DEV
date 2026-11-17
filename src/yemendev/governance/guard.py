"""Single choke point combining policy evaluation with audit logging."""

from __future__ import annotations

from yemendev.core.contracts import PermissionDeniedError
from yemendev.governance.actors import Actor
from yemendev.governance.policy import Action, Decision, PolicyEngine
from yemendev.security.audit import AuditLog


class Guard:
    def __init__(self, policy: PolicyEngine, audit: AuditLog) -> None:
        self.policy = policy
        self.audit = audit

    def require(self, actor: Actor, action: Action, target: str) -> Decision:
        """Raise PermissionDeniedError unless allowed. Sensitive actions still need an approval."""
        d = self.policy.evaluate(actor, action)
        if not d.allowed:
            self.audit.record(actor=actor.ref, action=action.value, target=target, outcome="denied",
                              details={"reason": d.reason})
            raise PermissionDeniedError(d.reason)
        return d
