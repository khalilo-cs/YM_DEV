# ADR-0002: stdlib sqlite3 + hand-written migrations

**Context.** Offline-first, single-user local operation first. SQLAlchemy/Alembic were
suggested.
**Decision.** Use `sqlite3` directly behind small repository classes. Ordered, append-only
migrations in `storage/migrations.py`, each applied atomically. WAL + `synchronous=FULL`
for power-loss durability; `BEGIN IMMEDIATE` for write transactions; FKs enforced.
**Why not SQLAlchemy now.** Zero extra dependency on the critical path; the SQL is small and
explicit. **Revisit** when PostgreSQL / multi-user is needed: the repository classes are the
seam where SQLAlchemy Core would be introduced.
