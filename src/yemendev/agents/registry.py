"""The agent registry (directive section 11).

Each agent has one responsibility and a *fixed* set of actions. The set is code, not data: there is no
API that changes it, so an agent cannot grant itself new permissions. The AgentRunner enforces it on
every invocation, in addition to the policy engine and the initiating human's own permissions.

Boundary note: agents are trusted platform code running in-process. The permission check is an
orchestration-level control (it stops a component from being *used* for an action it does not own,
and records every use); it is not a sandbox. Untrusted inputs (project code, model output) are
handled by the sandbox and by validation, not by this registry.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.governance.policy import Action


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    responsibility: str
    allowed_actions: frozenset[Action]

    @property
    def actor(self) -> Actor:
        return Actor(id=f"{self.name}-agent", kind=ActorKind.AGENT, role=Role.OPERATOR)


AGENTS: dict[str, AgentSpec] = {a.name: a for a in [
    AgentSpec(name="repository", responsibility="Scan source, configuration and dependencies (read-only, no code "
              "execution) and record the project's structure in memory.",
              allowed_actions=frozenset({Action.READ, Action.SCAN_PROJECT, Action.WRITE_MEMORY})),
    AgentSpec(name="testing", responsibility="Run the project's tests inside the sandbox (baseline and simulation).",
              allowed_actions=frozenset({Action.READ, Action.RUN_SANDBOXED})),
    AgentSpec(name="diagnostic", responsibility="Turn test evidence into reproducibility analysis and unverified "
              "root-cause hypotheses.", allowed_actions=frozenset({Action.READ, Action.WRITE_MEMORY})),
    AgentSpec(name="planning", responsibility="Propose repair plans from human input, active skills and models.",
              allowed_actions=frozenset({Action.READ})),
    AgentSpec(name="security", responsibility="Review every proposed change for dangerous patterns before it is "
              "simulated; can block a plan.", allowed_actions=frozenset({Action.READ})),
    AgentSpec(name="recovery", responsibility="Apply an approved plan with a checkpoint, and restore it on failure.",
              allowed_actions=frozenset({Action.READ, Action.APPLY_PATCH, Action.ROLLBACK_OWN_CHANGE})),
    AgentSpec(name="verification", responsibility="Independently re-run tests in a fresh sandbox and compare with "
              "the baseline.", allowed_actions=frozenset({Action.READ, Action.RUN_SANDBOXED})),
    AgentSpec(name="learning", responsibility="Record verified experiences and propose candidate skills (never "
              "activates them).", allowed_actions=frozenset({Action.READ, Action.WRITE_MEMORY, Action.PROPOSE_SKILL})),
]}

# Actions no agent may ever hold: decisions reserved for humans or administrators.
HUMAN_ONLY = frozenset({Action.DECIDE_APPROVAL, Action.REGISTER_PROJECT, Action.ISSUE_TOKEN, Action.MANAGE_MEMBERS,
                        Action.ACTIVATE_SKILL, Action.CREATE_TASK, Action.CANCEL_TASK, Action.BUILD_SANDBOX_IMAGE,
                        Action.READ_AUDIT})
