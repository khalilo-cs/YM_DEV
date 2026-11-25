# ADR-0014: ORION queue with one supervised process per run

**Context.** Directive 5.2 asks ORION for task queues, scheduling, dependency-aware execution,
cancellation, retry policies, timeouts, resource allocation, task recovery and execution tracing.
Until stage 16 a task advanced only synchronously inside an API/CLI call: no queue, no way to stop a
hung step, and a transient infrastructure error (container runtime restarting) failed the task for good.

**Decision.**
* **Queue in the same SQLite database** (`task_queue`, one row per task; migration 5). The task row stays
  the state machine; the queue row only says when, how often and with whose rights a worker may advance
  it. Claims are atomic (`BEGIN IMMEDIATE`), carry a heartbeat, and are recovered when a worker dies.
* **Scheduling:** priority, then `run_after` (delays, backoff), then FIFO. **Dependencies** between tasks:
  wait until terminal; anything but `succeeded`/`no_action` cancels the dependent with the reason; cycles
  rejected. **Resources:** one run per project, none while a human advances that project by hand, and a
  global `max_concurrent_runs` across all worker processes.
* **Rights:** a run acts as the human who queued it, with `min(role at enqueue, highest role of that
  human's active tokens)`; no active token -> dropped. Membership is re-checked on every step.
* **One child process per run** (`python -m yemendev.orion.runner`, own process group). This is the only
  way to make a timeout or cancellation actually *stop* work in Python. Killing is safe because every
  workflow step is crash-safe and resumable (tested at 9 crash points since stage 5). Sandboxed programs
  die with the run (`PR_SET_PDEATHSIG`, set after the uid drop); containers carry `org.yemendev.run=<id>`
  and are removed by label; the run's workspaces live under `workspaces/runs/<id>` and are deleted.
* **Fencing:** a worker that cannot renew its claim kills its run; the run itself re-checks ownership
  before every step, so a slow-but-alive worker never races a recovered claim.
* **Retry policy:** transient failures (`UnavailableError`, `ExecutionFailedError`, run crashed, timeout)
  are retried with deterministic exponential backoff (`base * 2^(n-1)`, capped) up to `max_attempts`.
  Without the queue, a transient failure returns the task to `pending` instead of `failed`.
* **Cancellation:** honoured immediately (kill) while the current step does not touch the project
  (scan, baseline, diagnose, propose, simulate); otherwise at the next step boundary, with an applied
  change rolled back. **Termination** (timeout/give-up/cancel-kill) restores the task checkpoint, so a task
  that does not finish never leaves a half-applied change.
* **Tracing:** `task_events` records creation, queue decisions, runs (wall/CPU/peak memory from `wait4`),
  steps and status changes; `/tasks/{id}/trace` adds the plan and agent runs.

**Consequences.** `yemendev serve` runs workers in-process (`--workers N`, default 1); `yemendev worker`
runs them standalone. Process start costs ~0.3 s per run. Not covered: recurring schedules (cron-like),
preemption of running tasks by priority, and descendants of a sandboxed test that detach into their own
session (bounded by the sandbox rlimits / container lifetime, not by the parent-death signal).
