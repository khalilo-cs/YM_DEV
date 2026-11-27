# Research Plan

None of the concepts below is claimed to be novel until a systematic literature and patent review
has been done (directive section 6). That review is the first research task.

| Concept | Research question | Implementation status |
|---|---|---|
| Living System Memory | Can project memory stay accurate across long change sequences without stale or contradictory knowledge? | Core built (validity conditions, contradiction handling); small-scale evaluation only: benchmark scenarios `long_horizon_6_changes` (6 successive changes) and `knowledge_staleness`; long-horizon evaluation on real projects not started |
| Proof-carrying skills | Do skills that carry test evidence transfer more safely than unvalidated ones? | Built (validation history, field outcomes) |
| Compound failure testing | How well does recovery hold under simultaneous failures? | 2 compound benchmark scenarios + compound counterfactual scenarios |
| Counterfactual future engine | Does simulating failures before risky changes reduce real failures? | Engine built (sandboxed fault injection, blind spots, static-analysis precision/recall); effect on real failures not evaluated |
| Digital continuity passport | Can a signed, portable system description enable reconstruction elsewhere? | Signed passport built; reconstruction from it not evaluated |
| Digital immune system | Can drift/unauthorised-change detection separate operational anomalies from attacks? | Rule-based detector built; separation not yet evaluated on real incidents |
| Continuity debt | Is a continuity-risk metric reliable and useful? | Experimental metric computed; reliability/usefulness not evaluated |

## Evaluation design

Compare (A) conventional automated tooling, (B) a baseline AI coding agent, (C) agent + memory,
(D) agent + skills, (E) full RACE-X, on the same versioned set of projects and injected failures.
Metrics: recovery success, time to recovery, data loss, regressions, false positives/negatives,
evidence completeness, approval frequency. Every run must store configuration, dataset version,
environment, seed and outputs (see [BENCHMARK_METHODOLOGY.md](BENCHMARK_METHODOLOGY.md)).

**Status (stage 20):** `yemendev bench --compare` implements the arms as follows, on 5 small synthetic
scenarios - A: a naive apply-then-test pipeline written for the benchmark; B: the configured model's
patch applied directly (**not run yet: no model available**, and never estimated); C: YEMENDEV without
learned skills (stands in for "agent + memory"); D: YEMENDEV's plans and skills applied without the
safety gates (stands in for "agent + skills"); E: the full workflow. This is an ablation study of this
system plus a naive baseline, not a comparison with third-party tools or agents; that still needs a
model, a versioned set of real projects, and ideally an independent evaluator.

## Data

Synthetic seeded fixtures exist. Real projects need licences/permission for evaluation; a university
partnership is proposed in the project proposal.
