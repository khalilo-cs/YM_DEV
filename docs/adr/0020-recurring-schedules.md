# ADR-0020: Recurring schedules act as their creator, inside ORION

**Context.** The directive asks ORION for scheduling. Periodic scans, vulnerability checks, backups and readiness
checks keep a project's picture current; unattended work must not outlive or exceed the rights of the person who
set it up.

**Decision.** Schedules live in the platform database and are executed by the existing ORION workers (or by
`yemendev schedule run-due` from cron). Each run acts as the creating human with the queue's rule -
`min(role at creation, highest role of their active tokens)` - and re-checks the permission for its kind and the
project membership; failing that, the schedule is disabled with the reason. A run is claimed with a compare-and-set
on `next_run_at`; after downtime it runs once and is re-timed from now. A `repair` schedule only queues a task, so
human approval of changes is unchanged. Intervals only (5 minutes to 90 days), no cron syntax.

**Consequences.** No new daemon, no new privilege path; revoking a person's tokens stops their schedules. Without a
running worker nothing runs - shown as "overdue" in recovery readiness instead of failing silently.
