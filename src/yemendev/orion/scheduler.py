"""ORION workers: run queued tasks in the background, one process per run (directive 5.2).

For each claim (see ``queue.py``) a worker starts ``python -m yemendev.orion.runner`` in a new process
group and supervises it:

* **Timeout** - the claim's ``timeout_seconds`` is a wall-clock budget for the run. On expiry the process
  group is killed; sandboxed programs die with it (PR_SET_PDEATHSIG in the sandbox launcher) and the run's
  containers are removed by label. The attempt then counts as a transient failure.
* **Retry** - transient failures (sandbox / runtime / model endpoint unavailable, process could not start,
  run process crashed, timeout) are retried with exponential backoff up to ``max_attempts``; then the task
  is terminated as failed. Termination never leaves a half-applied change (checkpoint restore).
* **Cancellation** - a cancel request for a running task is honoured immediately (kill) while the current
  step does not touch the project; otherwise at the next step boundary by the workflow, which rolls an
  applied change back.
* **Fencing** - the worker renews its claim by heartbeat; if renewal fails (it was presumed dead and the
  claim recovered), it kills its run at once, and the run itself re-checks the claim before every step.
* **Accounting** - wall time, CPU time and peak memory of each run (from ``wait4``) go to the trace.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.core.contracts import ConflictError
from yemendev.observability.metrics import ORION_RUN_CPU, ORION_RUN_SECONDS, ORION_RUNS
from yemendev.observability.tracing import inject, tracer
from yemendev.orion.queue import Claim, QueueState
from yemendev.orion.tasks import TaskStatus
from yemendev.orion.workflow import INTERRUPTIBLE_STEPS, RepairWorkflow
from yemendev.platform import Platform
from yemendev.security.redaction import redact_text

log = logging.getLogger("yemendev.orion")
POLL = 0.2


class RunOutcome(BaseModel):
    task_id: str
    run_id: str
    outcome: str  # finished | retry_scheduled | gave_up | timed_out | cancelled | crashed | claim_lost | released
    task_status: str
    message: str
    wall_seconds: float
    cpu_seconds: float | None = None
    max_rss_mb: float | None = None


class Scheduler:
    def __init__(self, platform: Platform, python: str = sys.executable) -> None:
        self.p = platform
        self.wf = RepairWorkflow(platform)
        self.queue = self.wf.queue
        self.settings = platform.settings.orion
        self.python = python
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ident = f"{socket.gethostname()}:{os.getpid()}"

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self, workers: int | None = None) -> None:
        n = self.settings.workers if workers is None else workers
        self._stop.clear()
        for i in range(n):
            t = threading.Thread(target=self._loop, args=(f"{self._ident}:w{i}",), name=f"orion-w{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("ORION started %d worker(s)", n)

    def stop(self, timeout: float = 30.0) -> None:
        """Stop workers. Runs in progress are killed and handed back to the queue without using an attempt."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _loop(self, worker: str) -> None:
        last_recovery = 0.0
        while not self._stop.is_set():
            try:
                if time.monotonic() - last_recovery >= min(5.0, self.settings.heartbeat_seconds / 2):
                    self.queue.recover_stale()
                    last_recovery = time.monotonic()
                if self.run_once(worker) is not None:
                    continue
            except Exception:  # noqa: BLE001 - a worker must survive unexpected errors; they are logged
                log.exception("ORION worker %s error", worker)
            self._stop.wait(self.settings.poll_seconds)

    def drain(self, worker: str = "drain", max_runs: int = 100) -> list[RunOutcome]:
        """Run everything runnable right now (does not wait for delayed entries). For CLI and tests."""
        out: list[RunOutcome] = []
        self.queue.recover_stale()
        while len(out) < max_runs and (r := self.run_once(f"{self._ident}:{worker}")) is not None:
            out.append(r)
        return out

    def run_once(self, worker: str) -> RunOutcome | None:
        claim = self.queue.claim(worker)
        if claim is None:
            return None
        with tracer.start_as_current_span("orion.run") as span:
            span.set_attribute("yemendev.task_id", claim.entry.task_id)
            span.set_attribute("yemendev.run_id", claim.run_id)
            span.set_attribute("yemendev.attempt", claim.entry.attempts)
            out = self._run(claim)
            span.set_attribute("yemendev.outcome", out.outcome)
            span.set_attribute("yemendev.task_status", out.task_status)
        ORION_RUNS.inc(out.outcome)
        ORION_RUN_SECONDS.observe(out.wall_seconds, out.outcome)
        if out.cpu_seconds:
            ORION_RUN_CPU.inc(amount=out.cpu_seconds)
        log.info("run finished", extra={"task_id": out.task_id, "run_id": out.run_id, "outcome": out.outcome,
                                        "task_status": out.task_status, "wall_seconds": out.wall_seconds})
        return out

    # ---- one run ---------------------------------------------------------------------------
    def _run(self, claim: Claim) -> RunOutcome:
        e, run_id = claim.entry, claim.run_id
        st = self.p.settings
        run_dir = st.data_dir / "runs" / run_id
        run_dir.mkdir(parents=True, mode=0o700)
        ws_dir = st.workspaces_dir / "runs" / run_id
        child = st.model_copy(update={"workspaces_root": ws_dir,
                                      "sandbox": st.sandbox.model_copy(update={"run_label": run_id})})
        job = {"settings": child.model_dump(mode="json"), "entry_id": e.id, "run_id": run_id, "task_id": e.task_id,
               "actor": claim.actor.model_dump(mode="json"), "result_path": str(run_dir / "result.json"),
               "trace_context": inject()}
        started = time.monotonic()
        with open(run_dir / "run.log", "wb") as logf:
            proc = subprocess.Popen([self.python, "-m", "yemendev.orion.runner"], stdin=subprocess.PIPE,
                                    stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, cwd=run_dir)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(job).encode())
        proc.stdin.close()

        reason, rusage = self._supervise(proc, e.id, run_id, e.task_id, started + e.timeout_seconds)
        wall = round(time.monotonic() - started, 3)
        acct: dict[str, Any] = {"wall_seconds": wall}
        if rusage is not None:
            acct["cpu_seconds"] = round(rusage.ru_utime + rusage.ru_stime, 3)
            acct["max_rss_mb"] = round(rusage.ru_maxrss / 1024, 1)  # Linux: KiB
        killed = reason is not None
        cleanup = self._cleanup(run_id, ws_dir) if killed else {}
        result = self._read_json(run_dir / "result.json")
        log_tail = redact_text((run_dir / "run.log").read_bytes()[-1500:].decode("utf-8", "replace"))
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(ws_dir, ignore_errors=True)
        task = self.wf.tasks.get(e.task_id)
        step = task.current_step
        detail = {**acct, **cleanup, "step": step}

        def outcome(kind: str, msg: str) -> RunOutcome:
            t = self.wf.tasks.get(e.task_id)
            return RunOutcome(task_id=e.task_id, run_id=run_id, outcome=kind, task_status=t.status.value,
                              message=msg, wall_seconds=wall, cpu_seconds=acct.get("cpu_seconds"),
                              max_rss_mb=acct.get("max_rss_mb"))

        if reason == "claim_lost":
            # Someone else owns the task now: touch nothing (not even the lease).
            return outcome("claim_lost", "queue claim lost; run stopped")
        if killed:
            self.wf.tasks.expire_lease(e.task_id)  # the dead run's lease must not block the next one
        if reason == "shutdown":
            self.queue.release_claim(e.id, run_id, "worker shutting down")
            return outcome("released", "worker shutting down; the task will resume later")
        if reason == "cancelled":
            by = task.cancel_requested_by or "unknown"
            msg = f"cancelled by {by} during step {step} (the step does not modify the project)"
            self.wf.terminate(e.task_id, TaskStatus.CANCELLED, msg, detail)
            self.queue.close(e.id, QueueState.CANCELLED, msg)
            return outcome("cancelled", msg)
        if reason == "timed_out":
            return self._retry(e.id, run_id, e.task_id, f"timed out after {e.timeout_seconds}s in step {step}",
                               {**detail, "outcome": "timed_out"}, outcome)
        if result is None or "error" in result:
            err = (result or {}).get("error") or f"run process exited with code {proc.returncode} without a result"
            return self._retry(e.id, run_id, e.task_id, f"run crashed: {redact_text(str(err))}",
                               {**detail, "outcome": "crashed", "log_tail": log_tail}, outcome)
        if result.get("retryable"):
            return self._retry(e.id, run_id, e.task_id, str(result.get("message")),
                               {**detail, "outcome": "transient_failure"}, outcome)
        try:
            self.queue.finish_run(e.id, run_id, TaskStatus(result["task_status"]),
                                  {**detail, "ran_steps": result.get("ran_steps"), "message": result.get("message")})
        except ConflictError:
            return outcome("claim_lost", str(result.get("message")))
        return outcome("finished", str(result.get("message")))

    def _retry(self, entry_id: str, run_id: str, task_id: str, error: str, detail: dict[str, Any],
               outcome: Callable[[str, str], RunOutcome]) -> RunOutcome:
        try:
            entry, gave_up = self.queue.retry_or_fail(entry_id, run_id, error, detail)
        except ConflictError:
            return outcome("claim_lost", error)
        if gave_up:
            self.wf.terminate(task_id, TaskStatus.FAILED,
                              f"gave up after {entry.attempts} attempt(s): {error}", detail)
            return outcome("gave_up", error)
        kind = "timed_out" if detail.get("outcome") == "timed_out" else "retry_scheduled"
        return outcome(kind, f"{error}; retry {entry.attempts + 1}/{entry.max_attempts} after {entry.run_after}")

    def _supervise(self, proc: subprocess.Popen[bytes], entry_id: str, run_id: str, task_id: str,
                   deadline: float) -> tuple[str | None, Any]:
        """Wait for the run; kill it on timeout, lost claim, cancellation or shutdown. Returns (reason, rusage)."""
        hb_every = max(0.5, self.settings.heartbeat_seconds / 3)
        next_hb = time.monotonic() + hb_every
        next_cancel_check = time.monotonic() + 0.5
        while True:
            pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                proc.returncode = os.waitstatus_to_exitcode(status)
                return None, ru
            now = time.monotonic()
            reason = None
            if now >= next_hb:
                next_hb = now + hb_every
                if not self.queue.heartbeat(entry_id, run_id):
                    reason = "claim_lost"
            if reason is None and now >= deadline:
                reason = "timed_out"
            if reason is None and self._stop.is_set():
                reason = "shutdown"
            if reason is None and now >= next_cancel_check:
                next_cancel_check = now + 0.5
                t = self.wf.tasks.get(task_id)
                if t.cancel_requested_at and t.current_step in INTERRUPTIBLE_STEPS:
                    reason = "cancelled"
            if reason is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status, ru = os.wait4(proc.pid, 0)
                proc.returncode = os.waitstatus_to_exitcode(status)
                return reason, ru
            time.sleep(POLL)

    def _cleanup(self, run_id: str, ws_dir: Path) -> dict[str, Any]:
        """After a kill: remove the run's containers (by label) and its workspaces."""
        removed = 0
        docker = self.p.settings.sandbox.docker_binary
        try:
            ids = subprocess.run([docker, "ps", "-aq", "--filter", f"label=org.yemendev.run={run_id}"],
                                 capture_output=True, text=True, timeout=30).stdout.split()
            if ids:
                subprocess.run([docker, "rm", "-f", *ids], capture_output=True, timeout=60)
                removed = len(ids)
        except (OSError, subprocess.SubprocessError):
            pass  # no container runtime: nothing to remove
        shutil.rmtree(ws_dir, ignore_errors=True)
        return {"containers_removed": removed, "workspace_left_behind": ws_dir.exists()}

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
