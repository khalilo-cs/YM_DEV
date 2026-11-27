"""Ordered, append-only schema migrations.

Rules: never edit a released migration; add a new one. Each migration runs in its own
transaction and is recorded in ``schema_migrations``.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial_core_schema",
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        );

        -- One row per scan (a "project version" as observed at a point in time).
        CREATE TABLE project_scans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            tree_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL
        );
        CREATE INDEX ix_scans_project_time ON project_scans(project_id, created_at);

        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            subject TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            source TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            validity_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            supersedes TEXT REFERENCES memory_records(id),
            contradicts_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            validated_at TEXT,
            revalidate_after TEXT
        );
        CREATE INDEX ix_memory_lookup ON memory_records(project_id, subject, key, status);
        CREATE INDEX ix_memory_category ON memory_records(project_id, category);

        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            params_json TEXT NOT NULL,
            current_step TEXT,
            error TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT
        );
        CREATE INDEX ix_tasks_status ON tasks(status);

        CREATE TABLE task_steps (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            output_json TEXT,
            error TEXT,
            PRIMARY KEY (task_id, name)
        );

        CREATE TABLE approval_requests (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            operation_digest TEXT NOT NULL,
            summary TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_by TEXT,
            decided_at TEXT,
            decision_reason TEXT,
            consumed_at TEXT
        );
        CREATE INDEX ix_approvals_status ON approval_requests(status);

        -- Append-only; no UPDATE/DELETE path exists in application code.
        CREATE TABLE evidence_records (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            target TEXT NOT NULL,
            created_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            body_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE INDEX ix_evidence_task ON evidence_records(task_id);

        -- Hash-chained, HMAC-keyed audit log.
        CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            outcome TEXT NOT NULL,
            details_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL
        );

        CREATE TABLE skills (
            id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (id, version)
        );
        CREATE INDEX ix_skills_status ON skills(status);

        CREATE TABLE skill_validation_runs (
            id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            skill_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            passed INTEGER NOT NULL,
            results_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            FOREIGN KEY (skill_id, skill_version) REFERENCES skills(id, version)
        );

        CREATE TABLE api_tokens (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            actor_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        """,
    ),
    (
        2,
        "project_members_and_token_expiry",
        """
        CREATE TABLE project_members (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            actor_ref TEXT NOT NULL,
            role TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (project_id, actor_ref)
        );
        CREATE INDEX ix_members_actor ON project_members(actor_ref);

        ALTER TABLE api_tokens ADD COLUMN expires_at TEXT;
        -- Existing tokens get a 90-day lifetime from the moment of migration (same ISO format as iso()).
        UPDATE api_tokens SET expires_at = strftime('%Y-%m-%dT%H:%M:%f', 'now', '+90 days') || '000+00:00'
            WHERE expires_at IS NULL
        """,
    ),
    (
        3,
        "agent_runs",
        """
        CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            project_id TEXT,
            agent TEXT NOT NULL,
            operation TEXT NOT NULL,
            actions_json TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            input_digest TEXT NOT NULL,
            output_digest TEXT,
            error TEXT
        );
        CREATE INDEX ix_agent_runs_task ON agent_runs(task_id);
        CREATE INDEX ix_agent_runs_agent ON agent_runs(agent, started_at)
        """,
    ),
    (
        4,
        "synchronization",
        """
        CREATE TABLE sync_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            direction TEXT NOT NULL,
            status TEXT NOT NULL,
            peer_fingerprint TEXT,
            bundle_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            records_total INTEGER NOT NULL,
            records_applied INTEGER NOT NULL,
            conflicts INTEGER NOT NULL,
            error TEXT
        );
        CREATE INDEX ix_sync_jobs_project ON sync_jobs(project_id, created_at);
        CREATE TABLE sync_conflicts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            key TEXT NOT NULL,
            local_id TEXT NOT NULL,
            local_value_json TEXT NOT NULL,
            remote_value_json TEXT NOT NULL,
            status TEXT NOT NULL,
            resolution TEXT,
            resolved_by TEXT,
            resolved_at TEXT
        );
        CREATE INDEX ix_sync_conflicts_project ON sync_conflicts(project_id, status)
        """,
    ),
    (
        5,
        "orion_queue_and_trace",
        """
        ALTER TABLE tasks ADD COLUMN cancel_requested_at TEXT;
        ALTER TABLE tasks ADD COLUMN cancel_requested_by TEXT;

        -- One row per task (re-used when a task is queued again). The task row stays the state machine;
        -- this row only says when, how often and with whose rights a background worker may advance it.
        CREATE TABLE task_queue (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            priority INTEGER NOT NULL,
            run_after TEXT NOT NULL,
            enqueued_at TEXT NOT NULL,
            enqueued_by TEXT NOT NULL,
            enqueued_role TEXT NOT NULL,
            depends_on_json TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            worker TEXT,
            run_id TEXT,
            heartbeat_until TEXT,
            last_error TEXT,
            finished_at TEXT
        );
        CREATE INDEX ix_task_queue_ready ON task_queue(state, priority, run_after);
        CREATE INDEX ix_task_queue_project ON task_queue(project_id, state);

        -- Execution trace: an append-only timeline per task (queue, runs, steps, status changes).
        CREATE TABLE task_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail_json TEXT NOT NULL
        );
        CREATE INDEX ix_task_events_task ON task_events(task_id, seq)
        """,
    ),
    (
        6,
        "vulnerability_advisories",
        """
        -- OSV advisories imported offline (one row per advisory and affected package).
        CREATE TABLE advisories (
            id TEXT NOT NULL,
            ecosystem TEXT NOT NULL,
            package TEXT NOT NULL,
            modified TEXT NOT NULL,
            summary TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            severity TEXT,
            affected_json TEXT NOT NULL,
            references_json TEXT NOT NULL,
            import_id TEXT NOT NULL,
            PRIMARY KEY (id, ecosystem, package)
        );
        CREATE INDEX ix_advisories_package ON advisories(ecosystem, package);
        CREATE TABLE advisory_imports (
            id TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            imported_by TEXT NOT NULL,
            source TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            newest_modified TEXT,
            records_read INTEGER NOT NULL,
            rows_written INTEGER NOT NULL,
            withdrawn_removed INTEGER NOT NULL,
            skipped INTEGER NOT NULL
        )
        """,
    ),
    (
        7,
        "backups",
        """
        CREATE TABLE backups (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            archive_sha256 TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            audit_head TEXT NOT NULL,
            verified_at TEXT,
            verified_ok INTEGER,
            verify_report_json TEXT
        )
        """,
    ),
]
