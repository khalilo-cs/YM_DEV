"""Isolated workspaces for running untrusted project code.

See ADR-0005. Isolation provided:
* the project is **copied** into a fresh temporary directory; the original is never
  touched by sandboxed code (symlinks and secret-looking files are not copied);
* ``subprocess`` with an argv list (never a shell), new session, scrubbed environment
  (HOME/TMPDIR point inside the workspace), rlimits on address space, CPU time, file
  size and core dumps;
* on timeout the entire process group is killed;
* network namespace via ``unshare --net`` (loopback only) when available. Whether it
  was applied is reported in ``ExecutionResult.network_isolated``.

NOT provided: filesystem isolation from files readable by the OS user, syscall
filtering, or a container/VM boundary. Documented in docs/THREAT_MODEL.md (RISK-001).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError, UnavailableError, sha256_bytes
from yemendev.twin.scanner import SENSITIVE_NAMES, SKIP_DIRS


class ExecutionResult(BaseModel):
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    network_isolated: bool
    limits: dict[str, Any]
    output_truncated: bool


# Trusted launcher: applies rlimits, then replaces itself with the target program.
# Used instead of ``preexec_fn``, which is unsafe in multi-threaded parents (API server).
_LAUNCHER = (
    "import os,resource,sys\n"
    "mem,cpu,fsz=(int(x) for x in sys.argv[1:4])\n"
    "resource.setrlimit(resource.RLIMIT_AS,(mem,mem))\n"
    "resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(fsz,fsz))\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "argv=sys.argv[5:]\n"
    "os.execvp(argv[0],argv)\n"
)


@lru_cache(maxsize=1)
def network_namespace_available() -> bool:
    exe = shutil.which("unshare")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--net", "--map-root-user", "true"], capture_output=True, timeout=10)
        if r.returncode == 0:
            return True
        r = subprocess.run([exe, "--net", "true"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def _unshare_prefix() -> list[str]:
    exe = shutil.which("unshare") or "unshare"
    try:
        if subprocess.run([exe, "--net", "--map-root-user", "true"], capture_output=True, timeout=10).returncode == 0:
            return [exe, "--net", "--map-root-user"]
    except (OSError, subprocess.SubprocessError):
        pass
    return [exe, "--net"]


def _ignore(src: str, names: list[str]) -> set[str]:
    out = set()
    for n in names:
        p = Path(src) / n
        if n in SKIP_DIRS or n.endswith(".egg-info") or p.is_symlink() or SENSITIVE_NAMES.search(n):
            out.add(n)
    return out


class Workspace:
    """A disposable copy of a project. Use as a context manager."""

    def __init__(self, source: Path, settings: SandboxSettings, base_dir: Path | None = None) -> None:
        self.source = source.resolve()
        if not self.source.is_dir():
            raise InvalidInputError("workspace source is not a directory")
        self.settings = settings
        self._tmp = Path(tempfile.mkdtemp(prefix="ydv-ws-", dir=base_dir))
        os.chmod(self._tmp, 0o700)
        self.root = self._tmp / "project"
        self.home = self._tmp / "home"
        self.home.mkdir()
        shutil.copytree(self.source, self.root, symlinks=False, ignore=_ignore)

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def path(self, rel: str) -> Path:
        """Resolve a project-relative path inside the workspace, refusing escapes."""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise InvalidInputError("path escapes workspace")
        return p

    def file_hashes(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts:
                out[p.relative_to(self.root).as_posix()] = sha256_bytes(p.read_bytes())
        return out

    def run(self, argv: list[str], timeout: float | None = None) -> ExecutionResult:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise InvalidInputError("argv must be a non-empty list of strings")
        s = self.settings
        timeout = timeout or s.timeout_seconds
        isolate = s.network_isolation != "disabled" and network_namespace_available()
        if s.network_isolation == "required" and not isolate:
            raise UnavailableError("network isolation is required but not available on this host")
        launcher = [
            sys.executable, "-I", "-c", _LAUNCHER, str(s.max_memory_mb * 1024 * 1024),
            str(int(timeout) + 5), str(s.max_file_size_mb * 1024 * 1024), "--",
        ]
        full = [*(_unshare_prefix() if isolate else []), *launcher, *argv]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "TMPDIR": str(self.home),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "NO_COLOR": "1",
        }
        limits = {
            "address_space_mb": s.max_memory_mb,
            "cpu_seconds": int(timeout) + 5,
            "file_size_mb": s.max_file_size_mb,
            "wall_timeout_seconds": timeout,
        }

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                full, cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            raise ExecutionFailedError(f"could not start process: {exc.__class__.__name__}") from exc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        duration = time.monotonic() - start
        cap = s.max_output_bytes
        truncated = len(out) > cap or len(err) > cap
        return ExecutionResult(
            argv=list(argv), returncode=None if timed_out else proc.returncode,
            stdout=out[-cap:].decode("utf-8", "replace"), stderr=err[-cap:].decode("utf-8", "replace"),
            duration_seconds=round(duration, 3), timed_out=timed_out, network_isolated=isolate,
            limits=limits, output_truncated=truncated,
        )
