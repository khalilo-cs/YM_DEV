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

* privilege separation: when the platform runs as root (or ``run_as_uid`` is set),
  untrusted code runs as an unprivileged uid (default 65534) and cannot read the
  platform's 0700 data directory. Recorded in ``ExecutionResult.run_as_uid``.

NOT provided: syscall filtering or a container/VM boundary; world-readable host files
remain readable. Without privilege separation (non-root, no run_as_uid) untrusted code
runs as the platform user and CAN access platform state - see RISK-001. Documented in docs/THREAT_MODEL.md (RISK-001).
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
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
from yemendev.sandbox.docker_backend import DockerAvailability, docker_availability, run_in_container
from yemendev.twin.scanner import SENSITIVE_NAMES, SKIP_DIRS


class ExecutionResult(BaseModel):
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    network_isolated: bool
    run_as_uid: int | None
    limits: dict[str, Any]
    output_truncated: bool
    backend: str = "process"
    image: str | None = None
    backend_note: str | None = None


# Trusted launcher: applies rlimits, then replaces itself with the target program.
# Used instead of ``preexec_fn``, which is unsafe in multi-threaded parents (API server).
_LAUNCHER = (
    "import os,resource,sys\n"
    "mem,cpu,fsz=(int(x) for x in sys.argv[1:4])\n"
    "resource.setrlimit(resource.RLIMIT_AS,(mem,mem))\n"
    "resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(fsz,fsz))\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "uid,gid=int(sys.argv[4]),int(sys.argv[5])\n"
    "if uid>=0:\n"
    "    os.setgroups([]);os.setgid(gid);os.setuid(uid)\n"
    "    assert os.getuid()==uid and os.geteuid()==uid, 'privilege drop failed'\n"
    # Die with the parent (set after the uid change, which clears it): a killed queue run must not leave
    # sandboxed code running. If the parent is already gone, do not start at all.
    "if sys.platform.startswith('linux'):\n"
    "    import ctypes\n"
    "    ctypes.CDLL(None).prctl(1,9,0,0,0)\n"
    "    if os.getppid()!=int(sys.argv[6]): os._exit(70)\n"
    "argv=sys.argv[8:]\n"
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


@lru_cache(maxsize=2)
def _unshare_prefix(dropping_privileges: bool = False) -> list[str]:
    exe = shutil.which("unshare") or "unshare"
    if dropping_privileges:
        # inside a --map-root-user namespace the target uid is unmapped; plain --net (as root) is required
        return [exe, "--net"]
    try:
        if subprocess.run([exe, "--net", "--map-root-user", "true"], capture_output=True, timeout=10).returncode == 0:
            return [exe, "--net", "--map-root-user"]
    except (OSError, subprocess.SubprocessError):
        pass
    return [exe, "--net"]


def runtime_socket_candidates() -> list[Path]:
    uid = os.getuid()
    cands = [Path("/var/run/docker.sock"), Path("/run/docker.sock"), Path(f"/run/user/{uid}/docker.sock"),
             Path("/run/containerd/containerd.sock"), Path("/run/podman/podman.sock"),
             Path(f"/run/user/{uid}/podman/podman.sock"), Path("/var/run/crio/crio.sock")]
    dh = os.environ.get("DOCKER_HOST", "")
    if dh.startswith("unix://"):
        cands.append(Path(dh[len("unix://"):]))
    return cands


def exposed_runtime_sockets(identity: tuple[int, int] | None,
                            candidates: list[Path] | None = None) -> list[str]:
    """Runtime sockets the sandbox identity could connect to.

    ``identity=None`` means code runs as the platform's own user (with its supplementary groups).
    For a dropped identity, groups are cleared by the launcher, so only owner/primary-group/world bits count.
    """
    out = []
    for p in candidates if candidates is not None else runtime_socket_candidates():
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode):
            continue
        if identity is None:
            reachable = os.access(p, os.R_OK | os.W_OK)
        else:
            uid, gid = identity
            mode = st.st_mode
            reachable = (st.st_uid == uid and mode & 0o600 == 0o600) or \
                (st.st_gid == gid and mode & 0o060 == 0o060) or (mode & 0o006 == 0o006)
        if reachable:
            out.append(str(p))
    return out


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
        self.identity = settings.sandbox_identity()
        self.backend, self.backend_note, self._docker = self._select_backend()
        # A container provides privilege separation even when the platform cannot switch users.
        if self.identity is None and settings.privilege_separation == "required" and self.backend != "docker":
            self.cleanup()
            raise UnavailableError("privilege separation is required but the platform cannot switch users "
                                   "(not running as root and no run_as_uid configured)")
        self._chown_tree()
        if self.backend == "process":
            self._check_reachable()
            self._check_runtime_sockets()

    def _select_backend(self) -> tuple[str, str | None, DockerAvailability | None]:
        mode = self.settings.backend
        if mode == "process":
            return "process", None, None
        avail = docker_availability(self.settings)
        if avail.available:
            return "docker", None, avail
        if mode == "docker":
            self.cleanup()
            raise UnavailableError(f"docker sandbox backend required but unavailable: {avail.reason}")
        return "process", f"auto: docker unavailable ({avail.reason}); used process backend", None

    def _check_runtime_sockets(self) -> None:
        """Refuse to run untrusted code that could reach a container-runtime socket (root-equivalent).

        Regression (stage 10 audit): with no uid drop, sandboxed code connected to /var/run/docker.sock
        and got ``HTTP 200`` from the Docker API - network namespaces do not block Unix sockets.
        """
        exposed = exposed_runtime_sockets(self.identity)
        if not exposed:
            return
        if self.settings.allow_runtime_socket_exposure:
            note = f"WARNING: sandboxed code can reach container runtime socket(s) {exposed} (allowed by setting)"
            self.backend_note = f"{self.backend_note}; {note}" if self.backend_note else note
            return
        self.cleanup()
        hint = f" ({self.backend_note})" if self.backend_note else ""
        raise UnavailableError(
            f"refusing to run untrusted code: its identity can reach container runtime socket(s) {exposed}, "
            "which is equivalent to root on this host. Use the docker backend (build its image with "
            "`yemendev sandbox build-image`), run the platform as root so code runs as an unprivileged uid, "
            f"or set allow_runtime_socket_exposure explicitly{hint}.")

    @property
    def python_executable(self) -> str:
        """Interpreter to use for this backend (the container's own python for docker)."""
        return "python" if self.backend == "docker" else self.settings.python_executable

    def _check_reachable(self) -> None:
        """Fail clearly if the sandbox uid cannot traverse to its workspace (instead of an opaque test failure)."""
        if self.identity is None:
            return
        uid = self.identity[0]
        for anc in self._tmp.parents:
            st = anc.stat()
            if st.st_uid != uid and not st.st_mode & 0o001:
                self.cleanup()
                raise UnavailableError(f"sandbox uid {uid} cannot traverse workspace parent directory {anc}; "
                                       "configure a workspaces directory that is traversable (mode o+x)")

    def _chown_tree(self) -> None:
        if self.identity is None:
            return
        uid, gid = self.identity
        for dirpath, dirnames, filenames in os.walk(self._tmp):
            os.chown(dirpath, uid, gid)
            for n in (*dirnames, *filenames):
                os.chown(os.path.join(dirpath, n), uid, gid, follow_symlinks=False)

    def write(self, rel: str, data: bytes) -> None:
        """Write a file into the workspace (owned by the sandbox identity)."""
        from yemendev.recovery.checkpoint import atomic_write

        target = self.path(rel)
        atomic_write(target, data)
        if self.identity is not None:
            os.chown(target, *self.identity)

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
        timeout = timeout or self.settings.timeout_seconds
        if self.backend == "docker":
            if self._docker is None:
                raise UnavailableError("docker backend selected without availability information")
            return run_in_container(self, argv, timeout, self._docker)
        r = self._run_process(argv, timeout)
        if self.backend_note:
            r.backend_note = self.backend_note
        return r

    def _run_process(self, argv: list[str], timeout: float) -> ExecutionResult:
        s = self.settings
        isolate = s.network_isolation != "disabled" and network_namespace_available()
        if s.network_isolation == "required" and not isolate:
            raise UnavailableError("network isolation is required but not available on this host")
        uid, gid = self.identity if self.identity is not None else (-1, -1)
        launcher = [
            sys.executable, "-I", "-c", _LAUNCHER, str(s.max_memory_mb * 1024 * 1024),
            str(int(timeout) + 5), str(s.max_file_size_mb * 1024 * 1024), str(uid), str(gid), str(os.getpid()), "--",
        ]
        full = [*(_unshare_prefix(self.identity is not None) if isolate else []), *launcher, *argv]
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
            run_as_uid=self.identity[0] if self.identity else None, limits=limits, output_truncated=truncated,
        )
