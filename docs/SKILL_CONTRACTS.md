# Skill Contracts

Code: `src/yemendev/learning/`. Tables: `skills`, `skill_validation_runs`.

## Spec fields

name, description, kind (`token_rename` only), params (`old`, `new` dotted identifiers), languages,
frameworks, python_versions, preconditions (hypothesis category + required signals), permissions,
expected_effects, failure_modes, test_cases (≥1 must demonstrate a change), dependencies,
rollback_strategy (`checkpoint_restore`), origin. Content hash = SHA-256 of the canonical spec.

## Lifecycle

`PROPOSED → VALIDATING → VERIFIED → ACTIVE → DEPRECATED`, plus `REJECTED` when validation fails.

* Validation runs the skill's own test cases deterministically and checks the stored content hash.
* Activation requires: status `verified`, latest validation passed **for the current content hash**,
  and a bound, single-use human approval (`skill.activate`) decided by someone other than the requester.
* One active version per skill id; activating a new version deprecates the old one.
* Any spec change creates a new version that starts at `PROPOSED`.
* Field outcomes (verified/failed repairs) are recorded; an active skill whose field failures exceed its
  successes is deprecated automatically. Confidence = (successes+1)/(total+2).

## Safety properties of `token_rename`

No user regex (no ReDoS), no code execution, word-boundary replacement of a dotted identifier, never
applied to test files. Proposals from skills still go through security review, simulation, approval
and verification.

## Learning

After a **verified** repair, `extract_token_rename` derives a candidate skill only when every change is
the same single identifier replacement. Candidates are `PROPOSED` by the learning agent and are never
activated automatically.
