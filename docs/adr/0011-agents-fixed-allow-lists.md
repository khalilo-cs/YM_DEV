# ADR-0011: Agents with fixed allow-lists, recorded runs and an explicit conflict policy

**Context.** Agents existed only as labels in evidence records; directive section 11 requires explicit
responsibilities, limited permissions, typed messages, recorded execution and explicit resolution of
conflicting recommendations.

**Decision.** A code-defined registry of 8 agents, each with one responsibility and a frozen set of
actions; `AgentRunner` is the only way ORION invokes an agent and checks (allow-list ∩ policy) before,
and records an `agent_runs` row around, every invocation. The human initiator's rights are checked
separately, so the effective permission is the intersection. A new security agent statically reviews
each plan; conflict policy **SEC-BLOCK-WINS** excludes blocked plans and records `agent.conflict`
evidence; `caution` findings are put in front of the human approver.

**Consequences.** Every step is attributable to an agent and auditable. Agents remain trusted
in-process code (the control limits and records their use; it is not isolation). The security review
is a tripwire, not proof of safety.
