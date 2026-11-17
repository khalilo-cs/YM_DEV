# ADR-0006: Full-content patches with preconditions

A repair patch = `(path, original_sha256, new_content)`. Application fails if the current file
hash differs from `original_sha256` (protects against concurrent edits / stale plans). A
unified diff is generated for humans, but is not the source of truth. Writes are atomic
(`temp file + fsync + os.replace`), originals are checkpointed first.
