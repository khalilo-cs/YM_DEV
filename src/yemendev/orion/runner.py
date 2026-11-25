"""One queue run in its own process: ``python -m yemendev.orion.runner`` (started by the scheduler).

Why a process per run: a wall-clock timeout or a cancellation can then actually *stop* the work (the
scheduler kills the process group; sandboxed programs die with it via PR_SET_PDEATHSIG, containers are
removed by label). Killing is safe because every workflow step is crash-safe and resumable.

Protocol: the scheduler writes one JSON object to stdin (settings, run and claim ids, actor, result path);
the runner advances the task once and writes the outcome as JSON to the result path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from yemendev.core.config import Settings
from yemendev.governance.actors import Actor
from yemendev.orion.queue import QueueState
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform


def run(job: dict[str, Any]) -> dict[str, Any]:
    platform = Platform(Settings.model_validate(job["settings"]))
    wf = RepairWorkflow(platform)
    actor = Actor.model_validate(job["actor"])
    entry_id, run_id = job["entry_id"], job["run_id"]

    def still_owner() -> bool:
        with platform.db.read() as conn:
            r = conn.execute("SELECT run_id, state FROM task_queue WHERE id = ?", (entry_id,)).fetchone()
        return r is not None and r["run_id"] == run_id and r["state"] == QueueState.RUNNING.value

    res = wf.advance(actor, job["task_id"], worker=run_id, still_owner=still_owner)
    return {"task_status": res.task.status.value, "ran_steps": res.ran_steps, "message": res.message,
            "retryable": res.retryable}


def main() -> int:
    job = json.loads(sys.stdin.read())
    out = Path(job["result_path"])
    try:
        result = run(job)
        code = 0
    except Exception as exc:  # noqa: BLE001 - reported to the scheduler, which records it in the trace
        result = {"error": f"{exc.__class__.__name__}: {str(exc)[:500]}"}
        code = 2
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(out)  # atomic: the scheduler never reads a half-written result
    return code


if __name__ == "__main__":
    raise SystemExit(main())
