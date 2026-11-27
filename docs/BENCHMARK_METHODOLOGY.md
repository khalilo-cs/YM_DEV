# YEMENDEV Benchmarks — Methodology (resilience v0.2, comparison v0.1)

Code: `src/yemendev/benchmarks/` · Runs:

```
yemendev bench [--full] [--seed N] [--backend process|docker|auto] [--only NAME ...] [--out FILE]
yemendev bench --compare [--arms ABCDE] [--seed N] [--backend ...] [--out FILE]
```

Every report is plain JSON. Without `--out` it is written to `<data dir>/benchmarks/`. The research centre
lists both `<data dir>/benchmarks/` ("local") and the reports committed under `benchmarks/results/`
("published") at `GET /api/v1/research/benchmarks` and `GET /api/v1/research/benchmarks/{source}/{name}`.
Reports are served exactly as stored: nothing is recomputed or merged.

## 1. Resilience benchmark (measures YEMENDEV only)

Each scenario runs the **real** workflow (real sandbox, real pytest, real files on disk) against a seeded
synthetic project, injects a failure, and checks the outcome **on disk**, not only the reported status.

| Scenario | Category (directive §15) | Expected | Invariant checked |
|---|---|---|---|
| repair_basic | safe recovery | succeeded | fix present on disk |
| reject_regressing_plan | safe recovery | no_action | project untouched, no approval requested |
| healthy_no_action | false positive | no_action | project untouched |
| offline_repair | intermittent connectivity | succeeded | platform sockets blocked for the whole run |
| compound_drift_then_rollback | compound failure | rolled_back | plan files byte-identical to original; the external change made during the outage is preserved |
| compound_crash_plus_human_edit | compound failure | failed ("diverged") | the human's edit is not overwritten |
| crash_resume@&lt;point&gt; ×9 (`--full`; 3 by default) | power interruption | succeeded | both patched files correct after restart |
| skill_transfer | skill transfer | succeeded | a skill learned on project 1 is validated, activated and repairs project 2 with **no** human plan |
| long_horizon_6_changes | long-horizon maintenance | succeeded | 6 successive breaking changes repaired; no earlier fix lost (`knowledge_retention`), no regression |
| distributed_sync | distributed synchronization | completed | signed bundle imported; a bundle whose content **and** per-record hashes were altered is rejected by its signature; a disagreement becomes a conflict, verified local knowledge is not overwritten; imported facts stay unverified |
| tamper_detection | data integrity | detected | an untouched store raises no alarm; a modified evidence record and a deleted audit event are both detected |
| knowledge_staleness | knowledge staleness | detected | facts about the changed file become stale; facts about unchanged files do not |
| sandbox_memory_limit | low memory | no_action | a test needing ~1.5 GB under a 256 MB limit is stopped; project untouched; platform healthy |
| legacy_project_repair | legacy understanding | succeeded | `setup.py` + `requirements.txt` + relative imports + `unittest` understood and repaired |
| arabic_project_repair | multilingual | succeeded | Arabic identifiers, docstrings, test names and plan description survive scan, repair, evidence and memory |
| unauthorized_operations | security | blocked | 5 operations outside an actor's rights are refused (agent deciding, self-approval, non-member, role too low, non-admin backup) and nothing changes |

Per scenario: correctness, data-integrity notes, evidence completeness, audit-chain + evidence integrity
(`verify_integrity`), approvals requested, seconds to terminal state, CPU seconds, scenario metrics.
The summary reports the directive §15 metrics, each computed **only** from the scenarios that measure it:
recovery success rate, mean time to recovery, data-loss incidents, regression rate, skill-transfer success,
knowledge retention, staleness detection, false-positive / false-negative rate, tamper detection,
unauthorized operations blocked, human approval frequency, CPU seconds.
Reproducibility: seed, git commit **and whether the source tree differed from it** (`git_dirty`), Python /
OS / CPU count, sandbox backend, fixtures version and platform version are stored in every report.

## 2. Comparative benchmark (`--compare`)

Five shared scenarios (repair_basic, reject_regressing_plan, healthy_no_action, compound_crash_plus_human_edit,
skill_transfer), identical seeded fixtures and identical inputs for every arm, the same sandbox and test runner.

| Arm | What it is |
|---|---|
| A | conventional automation: apply the given patch, then run the tests. No simulation, approval, verification gate, checkpoint, rollback, evidence or learning. **Written for this benchmark**, not a third-party product. |
| B | baseline AI coding agent: the configured model proposes a patch from the failing tests (YEMENDEV's diagnosis is its context) and it is applied directly. **Run only when a model is configured; otherwise reported under `not_run` - never estimated.** |
| C | YEMENDEV ablation: complete workflow and memory, learned skills never activated |
| D | YEMENDEV ablation: plans and active skills applied directly - no simulation, approval, verification or rollback |
| E | YEMENDEV, complete RACE-X workflow |

When a model is configured, C, D and E receive the same model as B.
Correctness is judged by invariants: a repair must leave the tests passing; a refused plan must leave the
project byte-identical; a human edit must never be overwritten; a healthy project must not be touched.

## 3. Validating the benchmark itself

`benchmarks/mutation_check.py` sabotages one mechanism at a time and checks that the scenario meant to exercise
it fails. A benchmark that cannot tell a broken system from a working one measures nothing.

| Mutant | Must be caught by |
|---|---|
| rollback is a no-op | compound_drift_then_rollback |
| crash reconciliation ignores disk state | compound_crash_plus_human_edit |
| simulation always passes | reject_regressing_plan |
| policy guard allows everything | unauthorized_operations |
| integrity check always reports valid | tamper_detection |
| revalidation never marks anything stale | knowledge_staleness |
| sync accepts any signature | distributed_sync |
| sandbox memory limit not applied | sandbox_memory_limit |

Stage 20 found one gap this way: the sync mutant was first **missed**, because the scenario's tampered bundle
was also rejected by the per-record hash check, so the signature check was never exercised. The scenario now
recomputes the record hashes as a capable attacker would; the mutant is caught (A-18 in the audit report).
Result 2026-11-27 (stage 20): **8/8 mutants caught** (`PYTHONDONTWRITEBYTECODE=1 python benchmarks/mutation_check.py`).
The `unauthorized_operations` scenario also surfaced a real defect: a refused self-approval attempt was not
audited (A-19, fixed).

## 4. Results

See [section 6](#6-results-history) for earlier runs. Stage 20 (seed 7, `--full`):

| Backend | Correct | Data-integrity violations | Recovery success | Mean time to recovery (s) | Tamper detection | Unauthorized blocked | CPU s | Commit | File |
|---|---|---|---|---|---|---|---|---|---|
| process | 24/24 | 0 | 1.0 | 1.77 | 2/2 | 5/5 | 41.0 | `3f8b2e9` | `benchmarks/results/run-stage20-process.json` |
| docker | 24/24 | 0 | 1.0 | 2.74 | 2/2 | 5/5 | 13.8 | `3f8b2e9` | `benchmarks/results/run-stage20-docker.json` |

Other §15 metrics (process run): data_loss_incidents = 0, regression_rate = 0.0, skill_transfer_success = True, knowledge_retention = 1.0, knowledge_staleness_detected = True, false_positive_rate = 0.0, false_negative_rate = 0.0, human_approval_frequency = 1.0.

Comparison (process backend, seed 7, `benchmarks/results/compare-stage20-process.json`, commit `3f8b2e9`):

| Arm | Correct | Regressions | Data loss | Human approvals | Evidence records |
|---|---|---|---|---|---|
| A | 2/5 | 1 | 1 | 0 | 0 |
| C | 4/5 | 0 | 0 | 2 | 43 |
| D | 3/5 | 1 | 1 | 0 | 31 |
| E | 5/5 | 0 | 0 | 3 | 48 |
| B | **not run** - no model is configured or reachable (YEMENDEV_MODEL_BASE_URL); not estimated | | | | |

Per scenario:

| Scenario | A | C | D | E |
|---|---|---|---|---|
| repair_basic | ok (applied) | ok (succeeded) | ok (applied) | ok (succeeded) |
| reject_regressing_plan | **wrong** (applied; regression) | ok (no_action) | **wrong** (applied; regression) | ok (no_action) |
| healthy_no_action | ok (nothing_applied) | ok (no_action) | ok (nothing_applied) | ok (no_action) |
| compound_crash_plus_human_edit | **wrong** (applied; data loss) | ok (failed) | **wrong** (applied; data loss) | ok (failed) |
| skill_transfer | **wrong** (not_repaired) | **wrong** (no_action) | ok (repaired) | ok (succeeded) |

Reading: A applies a regressing patch and overwrites a human edit; D (no gates) does the same; C (no skills) cannot repair the second project in skill_transfer; E is correct on all five. These are five small synthetic cases built to exercise these mechanisms - see the limitations.

## 5. Limitations — read before citing

* **What the comparison is not.** A, C and D are constructed for this benchmark (a naive pipeline and two
  ablations). No third-party tool or vendor agent was measured. B has **not** been run: no model is available
  in the environment where these results were produced, so there is no B number at all.
* Scenarios were written by the same author as the system; a perfect score shows that the tested invariants
  hold, not general robustness. Mutation checking reduces, but does not remove, this bias.
* Fixtures are small synthetic projects; most use one defect class (renamed API).
* Learned skills match on the exact module and symbol names in their preconditions: skill_transfer shows
  reuse on a second project with the same module, **not** generalisation to differently named code.
* Approvals are granted automatically by the harness acting as a second human.
* Crashes are in-process exceptions; real power loss (torn writes, fsync behaviour) is not exercised.
* CPU seconds cover this process and its children; under the docker backend, CPU used inside containers is
  accounted to the docker daemon and is **not** included.
* distributed_sync uses two installations on one machine exchanging a file; no network partition is simulated.

## 6. Results history

### Benchmark validation (v0.1)
`benchmarks/mutation_check.py` sabotages the system (no-op rollback, broken crash reconciliation,
simulation that always passes) and checks the benchmark flags each mutant. Result 2026-11-17: 3/3 caught.

### Results (2026-11-17, seed 7, `--full`)
15/15 scenarios correct, 0 data-integrity violations, evidence complete 100%, audit valid 100%,
mean 1.51 s to terminal. File: `benchmarks/results/run-20261117T235832Z.json`.

### Results (2026-11-21, seed 7, `--full`, after stage 10)

| Backend | Correct | Data-integrity violations | Mean s to terminal | File |
|---|---|---|---|---|
| process | 15/15 | 0 | 1.60 | `benchmarks/results/run-stage10-process.json` |
| docker | 15/15 | 0 | 2.48 | `benchmarks/results/run-stage10-docker.json` |

The first docker run scored 14/15 because of a **scenario defect** (A-17), not a platform defect; that
run is kept as `benchmarks/results/run-stage10-docker-SCENARIO-BUG.json`. Mutation check re-run after
the redesign: 3/3 caught.

### Results (2026-11-21, seed 7, `--full`, stage 11, default backend `auto`)

15/15 correct, 0 data-integrity violations, mean 1.96 s to terminal (docker selected automatically).
File: `benchmarks/results/run-stage11-auto.json`.
