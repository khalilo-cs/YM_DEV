# Research Plan

None of the concepts below is claimed to be novel until a systematic literature and patent review
has been done (directive section 6). That review is the first research task.

| Concept | Research question | Implementation status |
|---|---|---|
| Living System Memory | Can project memory stay accurate across long change sequences without stale or contradictory knowledge? | Core built (validity conditions, contradiction handling); long-horizon evaluation not started |
| Proof-carrying skills | Do skills that carry test evidence transfer more safely than unvalidated ones? | Built (validation history, field outcomes) |
| Compound failure testing | How well does recovery hold under simultaneous failures? | 2 compound scenarios in the benchmark |
| Counterfactual future engine | Does simulating failures before risky changes reduce real failures? | Fault points only |
| Digital continuity passport | Can a signed, portable system description enable reconstruction elsewhere? | Not started |
| Digital immune system | Can drift/unauthorised-change detection separate operational anomalies from attacks? | Rule-based detector built; separation not yet evaluated on real incidents |
| Continuity debt | Is a continuity-risk metric reliable and useful? | Experimental metric computed; reliability/usefulness not evaluated |

## Evaluation design

Compare (A) conventional automated tooling, (B) a baseline AI coding agent, (C) agent + memory,
(D) agent + skills, (E) full RACE-X, on the same versioned set of projects and injected failures.
Metrics: recovery success, time to recovery, data loss, regressions, false positives/negatives,
evidence completeness, approval frequency. Baselines A-D are **not implemented**, so no comparison
is claimed. Every run must store configuration, dataset version, environment, seed and outputs
(the current benchmark already does - see [BENCHMARK_METHODOLOGY.md](BENCHMARK_METHODOLOGY.md)).

## Data

Synthetic seeded fixtures exist. Real projects need licences/permission for evaluation; a university
partnership is proposed in the project proposal.
