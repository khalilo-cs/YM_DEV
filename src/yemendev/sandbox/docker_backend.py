"""Container execution backend (ADR-0009).

Untrusted code runs in a throw-away container that sees **only** the workspace copy:

* ``--network none``, ``--read-only`` root filesystem, private ``/tmp`` tmpfs;
* ``--cap-drop ALL``, ``--security-opt no-new-privileges``, Docker's default seccomp profile;
* never uid 0: the sandbox identity (uid 65534 on root hosts) or the platform's own non-root uid;
* ``--pids-limit``, ``--memory`` (= swap), ``--cpus``, file-size and core ulimits; ``--init`` reaps zombies;
* on wall-clock timeout the container itself is killed (killing the CLI is not enough).

The platform never pulls images: the image must already exist locally (offline-first). Access to
the Docker daemon is root-equivalent on the host - the platform needs it, sandboxed code never does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError
from yemendev.security.redaction import redact_text

if TYPE_CHECKING:
    from yemendev.sandbox.workspace import ExecutionResult, Workspace

CONTAINER_WORKDIR = "/work"
NOBODY = 65534
_AVAIL_TTL = 30.0
_avail_cache: dict[tuple[str, str], tuple[float, DockerAvailability]] = {}


class DockerAvailability(BaseModel):
    available: bool
    reason: str
    server_version: str | None = None
    image_id: str | None = None


def docker_availability(settings: SandboxSettings, use_cache: bool = True) -> DockerAvailability:
    key = (settings.docker_binary, settings.docker_image)
    now = time.monotonic()
    if use_cache and key in _avail_cache and now - _avail_cache[key][0] < _AVAIL_TTL:
        return _avail_cache[key][1]
    result = _probe(settings)
    _avail_cache[key] = (now, result)
    return result


def clear_availability_cache() -> None:
    _avail_cache.clear()


def _probe(s: SandboxSettings) -> DockerAvailability:
    exe = shutil.which(s.docker_binary)
    if not exe:
        return DockerAvailability(available=False, reason=f"docker binary '{s.docker_binary}' not found")
    try:
        v = subprocess.run([exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True,
                           timeout=15)
        if v.returncode != 0 or not v.stdout.strip():
            return DockerAvailability(available=False, reason="docker daemon is not reachable")
        img = subprocess.run([exe, "image", "inspect", "--format", "{{.Id}}", s.docker_image], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return DockerAvailability(available=False, reason=f"docker probe failed: {exc.__class__.__name__}")
    if img.returncode != 0 or not img.stdout.strip():
        return DockerAvailability(available=False, server_version=v.stdout.strip(),
                                  reason=f"sandbox image '{s.docker_image}' is not present locally "
                                         "(build it with `yemendev sandbox build-image`; images are never pulled)")
    return DockerAvailability(available=True, reason="ok", server_version=v.stdout.strip(),
                              image_id=img.stdout.strip())


def isolation_report(s: SandboxSettings) -> dict[str, object]:
    """Which backend untrusted code will actually use, and plain-language warnings about weaker isolation."""
    from yemendev.sandbox.workspace import exposed_runtime_sockets

    warnings: list[str] = []
    docker = docker_availability(s) if s.backend != "process" else None
    if s.backend == "process" or (s.backend == "auto" and docker is not None and not docker.available):
        effective = "process"
        if s.backend == "auto" and docker is not None:
            warnings.append(f"docker sandbox not usable ({docker.reason}); falling back to the process backend")
        warnings.append("process backend: untrusted code can read world-readable host files and see host processes")
        ident = s.sandbox_identity()
        if ident is None:
            warnings.append("no privilege separation: untrusted code runs as the platform's own user")
        if exposed_runtime_sockets(ident) and not s.allow_runtime_socket_exposure:
            warnings.append("untrusted code could reach a container runtime socket: runs will be REFUSED")
        elif exposed_runtime_sockets(ident):
            warnings.append("untrusted code can reach a container runtime socket "
                            "(root-equivalent); allowed by setting")
    else:
        effective = "docker"
        if docker is not None and not docker.available:  # backend == "docker" (required) but unusable
            effective = "unavailable"
            warnings.append(f"docker backend required but unusable: {docker.reason}; sandboxed runs will fail")
    if s.network_isolation == "disabled":
        warnings.append("network isolation disabled: untrusted code has network access")
    return {"setting": s.backend, "effective": effective, "warnings": warnings,
            "docker": docker.model_dump() if docker else None}


def container_identity(identity: tuple[int, int] | None) -> tuple[int, int]:
    """Never run as uid 0 inside the container."""
    uid, gid = identity if identity is not None else (os.getuid(), os.getgid())
    if uid == 0:
        return NOBODY, NOBODY
    return uid, gid


def docker_run_command(
    s: SandboxSettings, workspace_root: Path, identity: tuple[int, int], argv: list[str], name: str,
    timeout: float,
) -> list[str]:
    if not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
        raise InvalidInputError("argv must be a non-empty list of strings")
    if not workspace_root.is_absolute() or "," in str(workspace_root):
        raise InvalidInputError("workspace path must be absolute and must not contain ','")
    uid, gid = identity
    fsz = s.max_file_size_mb * 1024 * 1024
    cpu = int(timeout) + 5
    return [
        s.docker_binary, "run", "--rm", "--name", name, "--init",
        "--network", "none" if s.network_isolation != "disabled" else "bridge",
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={s.docker_tmpfs_mb}m,mode=1777",  # noqa: S108 - container-private tmpfs
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{gid}",
        "--pids-limit", str(s.docker_pids_limit),
        "--memory", f"{s.max_memory_mb}m", "--memory-swap", f"{s.max_memory_mb}m",
        "--cpus", str(s.docker_cpus),
        "--ulimit", f"fsize={fsz}:{fsz}", "--ulimit", "core=0:0", "--ulimit", f"cpu={cpu}:{cpu}",
        "--mount", f"type=bind,src={workspace_root},dst={CONTAINER_WORKDIR}",
        "--workdir", CONTAINER_WORKDIR,
        "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", "--env", "LANG=C.UTF-8",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONHASHSEED=0", "--env", "PYTHONNOUSERSITE=1",
        "--env", "NO_COLOR=1",
        "--label", "org.yemendev.sandbox=1",
        *(["--label", f"org.yemendev.run={s.run_label}"] if s.run_label else []),
        "--entrypoint", argv[0],
        s.docker_image, *argv[1:],
    ]


def run_in_container(ws: Workspace, argv: list[str], timeout: float, avail: DockerAvailability) -> ExecutionResult:
    from yemendev.sandbox.workspace import ExecutionResult

    s = ws.settings
    name = f"ydv-{uuid.uuid4().hex[:20]}"
    ident = container_identity(ws.identity)
    cmd = docker_run_command(s, ws.root, ident, argv, name, timeout)
    limits = {"backend": "docker", "memory_mb": s.max_memory_mb, "pids_limit": s.docker_pids_limit,
              "cpus": s.docker_cpus, "file_size_mb": s.max_file_size_mb, "wall_timeout_seconds": timeout,
              "read_only_rootfs": True, "cap_drop": "ALL", "no_new_privileges": True}
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ExecutionFailedError(f"could not start docker: {exc.__class__.__name__}") from exc
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run([s.docker_binary, "kill", name], capture_output=True, timeout=30)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
    finally:
        # --rm normally removes it; make sure nothing is left behind on any path
        subprocess.run([s.docker_binary, "rm", "-f", name], capture_output=True, timeout=30)
    if not timed_out and proc.returncode == 125:
        raise ExecutionFailedError("docker could not start the sandbox container: "
                                   + redact_text(err.decode("utf-8", "replace"))[-500:])
    cap = s.max_output_bytes
    return ExecutionResult(
        argv=list(argv), returncode=None if timed_out else proc.returncode,
        stdout=out[-cap:].decode("utf-8", "replace"), stderr=err[-cap:].decode("utf-8", "replace"),
        duration_seconds=round(time.monotonic() - start, 3), timed_out=timed_out,
        network_isolated=s.network_isolation != "disabled", run_as_uid=ident[0], limits=limits,
        output_truncated=len(out) > cap or len(err) > cap, backend="docker", image=avail.image_id,
    )
