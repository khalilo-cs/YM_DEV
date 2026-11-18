"""Single choke point combining (project-scoped) policy evaluation with audit logging."""

from __future__ import annotations

from yemendev.core.contracts import PermissionDeniedError
from yemendev.governance.actors import Actor
from yemendev.governance.membership import MembershipStore, effective_role, is_unrestricted
from yemendev.governance.policy import Action, Decision, PolicyEngine
from yemendev.security.audit import AuditLog


class Guard:
    def __init__(self, policy: PolicyEngine, audit: AuditLog, members: MembershipStore) -> None:
        self.policy = policy
        self.audit = audit
        self.members = members

    def effective(self, actor: Actor, project_id: str) -> Actor | None:
        """The actor with its role on ``project_id``, or None if it has no access to that project."""
        if is_unrestricted(actor):
            return actor
        role = effective_role(actor, self.members.role(project_id, actor.ref))
        return None if role is None else actor.model_copy(update={"role": role})

    def can_read(self, actor: Actor, project_id: str) -> bool:
        eff = self.effective(actor, project_id)
        return eff is not None and self.policy.evaluate(eff, Action.READ).allowed

    def require(self, actor: Actor, action: Action, target: str, project_id: str | None = None) -> Decision:
        """Raise PermissionDeniedError unless allowed. Sensitive actions still need an approval.

        With ``project_id`` the check uses the actor's effective role on that project.
        """
        eff = actor
        if project_id is not None:
            scoped = self.effective(actor, project_id)
            if scoped is None:
                self.audit.record(actor=actor.ref, action=action.value, target=target, outcome="denied",
                                  details={"reason": "no access to project", "project_id": project_id})
                raise PermissionDeniedError("no access to this project")
            eff = scoped
        d = self.policy.evaluate(eff, action)
        if not d.allowed:
            self.audit.record(actor=actor.ref, action=action.value, target=target, outcome="denied",
                              details={"reason": d.reason, "project_id": project_id})
            raise PermissionDeniedError(d.reason)
        return d
