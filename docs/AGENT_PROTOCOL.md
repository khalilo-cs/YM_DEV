# Agent Protocol

Implements directive section 11. Code: `src/yemendev/agents/`.

## Registry

| Agent | Responsibility | Allowed actions |
|---|---|---|
| repository | scan source/config/deps (no execution), record structure | read, project.scan, memory.write |
| testing | run tests in the sandbox (baseline, simulation) | read, sandbox.run |
| diagnostic | reproducibility + unverified hypotheses | read, memory.write |
| planning | propose plans (human input, skills, models) | read |
| security | review every plan before simulation; may block | read |
| recovery | apply an approved plan with a checkpoint; restore on failure | read, project.apply_patch, project.rollback_own_change |
| verification | re-run tests independently in a fresh sandbox | read, sandbox.run |
| learning | record experiences, propose (never activate) skills | read, memory.write, skill.propose |

No agent may hold: approval decisions, project registration, token issuance, membership management,
skill activation, task creation/cancellation, image builds, audit reading (`HUMAN_ONLY`).

## Invocation contract

ORION calls `AgentRunner.run(agent, operation, actions, fn, task_id, project_id, inputs)`:

1. every requested action must be in the agent's **fixed** allow-list (code, not data - an agent cannot
   grant itself permissions) **and** allowed by the policy engine for an `agent` actor;
2. the initiating human's own permissions on the project are checked separately; the effective
   permission is the intersection;
3. a row is written to `agent_runs` before execution and finalised after it:
   `succeeded` (with output digest) · `stopped` (control-flow outcome, e.g. no action) · `failed`
   (exception class + message) · `denied` (never executed; audited);
4. results become step outputs persisted by ORION; they are not trusted by later steps without checks
   (e.g. verification re-runs tests itself; execution re-checks file hashes).

## Conflict resolution

Policy **SEC-BLOCK-WINS**: when the planning agent proposes a plan and the security agent's verdict is
`block`, the plan is excluded, and an `agent.conflict` evidence record stores both positions, the
findings and the policy applied. A `caution` verdict lets the plan proceed but its findings are included
in the human approval request.

## Limitations

Agents are trusted in-process code: the allow-list restricts how ORION *uses* them and records every
use; it is not a sandbox. The security agent is static pattern matching over added lines and can be
evaded by obfuscation - a clean review is never treated as proof of safety.
