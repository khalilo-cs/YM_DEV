"""Policy engine: classifies actions and decides whether an actor may perform them.

The policy table is code, not data the platform can modify at runtime: there is no
API that changes it, so an agent cannot relax its own constraints.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from yemendev.governance.actors import Actor, ActorKind, Role


class Action(StrEnum):
    READ = "read"
    REGISTER_PROJECT = "project.register"
    SCAN_PROJECT = "project.scan"
    RUN_SANDBOXED = "sandbox.run"          # runs untrusted project code in an isolated copy
    CREATE_TASK = "task.create"
    ADVANCE_TASK = "task.advance"
    CANCEL_TASK = "task.cancel"
    APPLY_PATCH = "project.apply_patch"    # modifies the real project tree
    ROLLBACK_OWN_CHANGE = "project.rollback_own_change"
    PROPOSE_SKILL = "skill.propose"
    VALIDATE_SKILL = "skill.validate"
    ACTIVATE_SKILL = "skill.activate"
    DEPRECATE_SKILL = "skill.deprecate"
    DECIDE_APPROVAL = "approval.decide"
    WRITE_MEMORY = "memory.write"
    ISSUE_TOKEN = "token.issue"  # noqa: S105 - action name, not a secret
    MANAGE_MEMBERS = "project.manage_members"
    READ_AUDIT = "audit.read"  # the audit log spans all projects
    BUILD_SANDBOX_IMAGE = "sandbox.build_image"  # downloads third-party code: admin only
    SYNC_EXPORT = "sync.export"
    SYNC_IMPORT = "sync.import"
    RESOLVE_SYNC_CONFLICT = "sync.resolve_conflict"
    TRUST_KEY = "keys.trust"
    IMPORT_ADVISORIES = "advisories.import"  # replaces the vulnerability database everyone relies on
    MANAGE_BACKUPS = "backups.manage"  # a backup contains the audit and signing keys


class Sensitivity(StrEnum):
    LOW = "low"
    SENSITIVE = "sensitive"   # requires a bound, single-use human approval


class Rule(BaseModel):
    min_role: Role
    sensitivity: Sensitivity
    agents_allowed: bool


POLICY: dict[Action, Rule] = {
    Action.READ: Rule(min_role=Role.VIEWER, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.REGISTER_PROJECT: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.SCAN_PROJECT: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.RUN_SANDBOXED: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.CREATE_TASK: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.ADVANCE_TASK: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.CANCEL_TASK: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.APPLY_PATCH: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.SENSITIVE, agents_allowed=True),
    # Restoring the exact pre-change state of a change the platform itself made is
    # pre-authorized by the approval of that change.
    Action.ROLLBACK_OWN_CHANGE: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.PROPOSE_SKILL: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.VALIDATE_SKILL: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.ACTIVATE_SKILL: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.SENSITIVE, agents_allowed=True),
    Action.DEPRECATE_SKILL: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.DECIDE_APPROVAL: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.WRITE_MEMORY: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=True),
    Action.ISSUE_TOKEN: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.MANAGE_MEMBERS: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.READ_AUDIT: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.BUILD_SANDBOX_IMAGE: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.SYNC_EXPORT: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.SYNC_IMPORT: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.RESOLVE_SYNC_CONFLICT: Rule(min_role=Role.OPERATOR, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.TRUST_KEY: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.IMPORT_ADVISORIES: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
    Action.MANAGE_BACKUPS: Rule(min_role=Role.ADMIN, sensitivity=Sensitivity.LOW, agents_allowed=False),
}


class Decision(BaseModel):
    action: Action
    actor: str
    allowed: bool
    requires_approval: bool
    reason: str


class PolicyEngine:
    def evaluate(self, actor: Actor, action: Action) -> Decision:
        rule = POLICY[action]
        if actor.kind is ActorKind.AGENT and not rule.agents_allowed:
            return Decision(action=action, actor=actor.ref, allowed=False, requires_approval=False,
                            reason="agents may not perform this action")
        if not actor.has_role(rule.min_role):
            return Decision(action=action, actor=actor.ref, allowed=False, requires_approval=False,
                            reason=f"requires role {rule.min_role.value}")
        needs = rule.sensitivity is Sensitivity.SENSITIVE
        return Decision(action=action, actor=actor.ref, allowed=True, requires_approval=needs,
                        reason="sensitive action: bound human approval required" if needs else "permitted")
