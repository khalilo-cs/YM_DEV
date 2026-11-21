# YEMENDEV Resilience Benchmark — Methodology (v0.1)

Code: `src/yemendev/benchmarks/` · Run: `yemendev bench [--full] [--seed N] [--backend process|docker|auto] --out benchmarks/results/<file>.json`

## What it measures
Each scenario runs the **real** workflow (real sandbox, real pytest, real files on disk) against a
seeded synthetic Python project, injects a failure, and checks the outcome **on disk**, not only the
reported status.

| Scenario | Category (directive §15) | Expected | Invariant checked on disk |
|---|---|---|---|
| repair_basic | safe recovery | succeeded | fix present |
| reject_regressing_plan | safe recovery | no_action | project untouched, no approval requested |
| healthy_no_action | false positive | no_action | project untouched |
| offline_repair | intermittent connectivity | succeeded | platform sockets blocked for the whole run |
| compound_drift_then_rollback | compound failure | rolled_back | crash after apply + a module outside the plan changes during the outage: plan files byte-identical to original, external change preserved |
| compound_crash_plus_human_edit | compound failure | failed ("diverged") | human's edit not overwritten |
| crash_resume@<point> ×9 | power interruption | succeeded | both patched files correct after restart |

Per scenario: correctness, data-integrity notes, evidence completeness, audit-chain + evidence
integrity (`verify_integrity`), approvals requested, seconds to terminal state.
Reproducibility: seed, git commit, Python/OS/CPU, platform version are stored in every report.

## Validating the benchmark itself
`benchmarks/mutation_check.py` sabotages the system (no-op rollback, broken crash reconciliation,
simulation that always passes) and checks the benchmark flags each mutant. Result 2026-11-17: 3/3 caught.

## Results (2026-11-17, seed 7, `--full`)
15/15 scenarios correct, 0 data-integrity violations, evidence complete 100%, audit valid 100%,
mean 1.51 s to terminal. File: `benchmarks/results/run-20261117T235832Z.json`.

## Limitations — read before citing
* **Not a comparative evaluation.** Baselines A–D from the directive (conventional tooling, baseline
  agent, memory-only, skills-only) are **not implemented**; no comparison is claimed.
* Scenarios were written by the same author as the system; a perfect score indicates the tested
  invariants hold, not general robustness. Mutation checking reduces but does not remove this bias.
* Fixtures are small synthetic projects with one defect class (renamed API).
* Approvals are granted automatically by the harness acting as a second human.
* Crashes are in-process exceptions; real power loss (torn writes, fsync behaviour) is not exercised.
* Not yet covered: low-memory operation, long-horizon maintenance, skill transfer across projects at
  scale, legacy code, multilingual docs, knowledge staleness over many changes, sync.

## Results (2026-11-21, seed 7, `--full`, after stage 10)

| Backend | Correct | Data-integrity violations | Mean s to terminal | File |
|---|---|---|---|---|
| process | 15/15 | 0 | 1.60 | `benchmarks/results/run-stage10-process.json` |
| docker | 15/15 | 0 | 2.48 | `benchmarks/results/run-stage10-docker.json` |

The first docker run scored 14/15 because of a **scenario defect** (A-17), not a platform defect; that
run is kept as `benchmarks/results/run-stage10-docker-SCENARIO-BUG.json`. Mutation check re-run after
the redesign: 3/3 caught.
