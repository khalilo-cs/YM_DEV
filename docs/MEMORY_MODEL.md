# Memory Model (Living Memory)

Code: `src/yemendev/memory/`. Table: `memory_records` (see [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)).

## Record

`id, project_id, category, subject, key, value, status, confidence, source, evidence_ids, validity,
version, supersedes, contradicts, created_at, validated_at, revalidate_after`.

Categories (13): project_identity, project_structure, technical_decision, requirement, constraint,
dependency, verified_fact, observation, experience, failed_experiment, candidate_skill, validated_skill,
deprecated_knowledge.

Statuses: `unverified`, `verified`, `stale`, `contradicted`, `superseded`, `deprecated`. Only
`unverified` and `verified` are returned as active knowledge.

## Write rules (tested in `tests/unit/test_memory.py`)

| Situation | Result |
|---|---|
| Verified write without evidence or validity conditions | rejected |
| Same value again | unchanged; re-validated; evidence ids merged |
| New **verified** value for a key with an active record | old → `superseded`, new version + link |
| New **unverified** value conflicting with a **verified** one | stored as `contradicted`, linked; verified one stays |
| New unverified value replacing an unverified one | old → `superseded` |

## Staleness

Records carry validity conditions (`file_sha256`, `file_absent`). `revalidate()` (run after every scan)
marks records `stale` when a condition no longer holds; paths escaping the project root never validate.
`revalidate_after` timestamps are reported as due for review.

## What writes memory

Scans (structure and dependencies as verified facts bound to file hashes; findings as observations),
diagnostics (hypotheses, unverified), learning (verified experiences with evidence), failures
(failed experiments).

## Limitations

Relevance scoring is keyword overlap × confidence × status weight - no embeddings or semantic search.
"Scope" and "related entities" from the directive are represented only by project_id/subject/key.
