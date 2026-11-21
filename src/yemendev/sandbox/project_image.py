"""Per-project sandbox images: the base sandbox image + the project's declared dependencies.

Supply-chain rules (ADR-0010):
* wheels only (``--only-binary=:all:``): ``pip download`` of a source distribution can execute the
  package's build code *on the host*; wheels are installed by copying files, so nothing from the
  project's dependencies runs outside the sandbox;
* direct URL / VCS / local-path requirements and pip options are rejected;
* wheels are selected for the *container's* Python version, architecture and glibc, which are read
  from the base image itself;
* the image build runs with ``--network none``; every wheel's name and SHA-256 is returned so it can
  be recorded as evidence;
* the image tag is derived from the base image id + the exact requirement list, so changing the
  dependencies (or the base image) yields a different image, and an existing image is reused.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError, UnavailableError
from yemendev.sandbox.docker_backend import docker_availability
from yemendev.sandbox.image import _find_pip
from yemendev.twin.model import ProjectSnapshot

TEST_GROUPS = {"main", "dev", "test", "tests", "testing"}
TAG_REPO = "yemendev-proj"
_SAFE_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"         # name[extras]
    r"\s*([<>=!~]=?[^;@\s]+(\s*,\s*[<>=!~]=?[^;@\s]+)*)?"        # version specifiers
    r"(\s*;\s*[^@]*)?$"                                         # environment marker
)


class WheelInfo(BaseModel):
    name: str
    sha256: str


class ProjectImage(BaseModel):
    tag: str
    image_id: str
    requirements: list[str]
    wheels: list[WheelInfo]
    reused: bool
    container_python: str


def runtime_requirements(snap: ProjectSnapshot) -> list[str]:
    """Requirement specs needed to run the project's tests (main + dev/test groups), validated."""
    reqs = sorted({d.spec.strip() for d in snap.declared_dependencies if d.group in TEST_GROUPS})
    for r in reqs:
        if r.startswith("-") or "://" in r or "@" in r or not _SAFE_SPEC.match(r):
            raise InvalidInputError(f"unsupported requirement for a sandbox image (URL, VCS, path or option): {r!r}")
    return reqs


def project_image_tag(base_image_id: str, requirements: list[str]) -> str:
    h = hashlib.sha256((base_image_id + "\n" + "\n".join(requirements)).encode()).hexdigest()
    return f"{TAG_REPO}:{h[:20]}"


def image_exists(s: SandboxSettings, tag: str) -> str | None:
    exe = shutil.which(s.docker_binary)
    if not exe:
        return None
    r = subprocess.run([exe, "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True,
                       timeout=15)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _container_target(s: SandboxSettings) -> tuple[str, str, str]:
    """(python 'X.Y', machine, glibc 'A.B') of the base image, read from inside it."""
    code = ("import sys,platform;print(f'{sys.version_info[0]}.{sys.version_info[1]}',platform.machine(),"
            "platform.libc_ver()[1] or '2.17')")
    r = subprocess.run([s.docker_binary, "run", "--rm", "--network", "none", "--entrypoint", "python",
                        s.docker_image, "-c", code], capture_output=True, text=True, timeout=120)
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 3:
        raise ExecutionFailedError("could not inspect the base sandbox image's python")
    return parts[0], parts[1], parts[2]


def build_project_image(s: SandboxSettings, requirements: list[str],
                        wheels_dir: Path | None = None) -> ProjectImage:
    if not requirements:
        raise InvalidInputError("project declares no dependencies; the base sandbox image is sufficient")
    base = docker_availability(s, use_cache=False)
    if not base.available or not base.image_id:
        raise UnavailableError(f"base sandbox image unavailable: {base.reason}")
    tag = project_image_tag(base.image_id, requirements)
    pyver, machine, glibc = _container_target(s)
    if existing := image_exists(s, tag):
        return ProjectImage(tag=tag, image_id=existing, requirements=requirements, wheels=[], reused=True,
                            container_python=pyver)
    with tempfile.TemporaryDirectory(prefix="ydv-projimg-") as tmp:
        ctx = Path(tmp)
        wheels = ctx / "wheels"
        (ctx / "requirements.txt").write_text("\n".join(requirements) + "\n")
        if wheels_dir is not None:
            if not wheels_dir.is_dir():
                raise InvalidInputError("wheels directory does not exist")
            shutil.copytree(wheels_dir, wheels)
        else:
            plat = f"manylinux_{glibc.replace('.', '_')}_{machine}"
            cmd = [*_find_pip(), "download", "--only-binary=:all:", "--dest", str(wheels),
                   "--python-version", pyver, "--implementation", "cp", "--platform", plat,
                   "--platform", f"linux_{machine}", "-r", str(ctx / "requirements.txt")]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                raise ExecutionFailedError("dependency download failed (wheels only; packages without a wheel for "
                                           f"python {pyver}/{machine} are not supported): {r.stderr[-900:]}")
        infos = [WheelInfo(name=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in sorted(wheels.glob("*.whl"))]
        if not infos:
            raise InvalidInputError("no wheels available for the project's dependencies")
        (ctx / "Dockerfile").write_text(
            f"FROM {s.docker_image}\nUSER 0:0\nCOPY wheels /tmp/wheels\nCOPY requirements.txt /tmp/requirements.txt\n"
            "RUN python -m pip install --no-cache-dir --no-index --find-links /tmp/wheels -r /tmp/requirements.txt"
            " && rm -rf /tmp/wheels /tmp/requirements.txt\n"
            f'LABEL org.yemendev.sandbox="1" org.yemendev.project-deps="{tag.split(":")[1]}"\n'
            "USER 65534:65534\nWORKDIR /work\n")
        b = subprocess.run([s.docker_binary, "build", "--network", "none", "-t", tag, str(ctx)], capture_output=True,
                           text=True, timeout=1800)
        if b.returncode != 0:
            raise ExecutionFailedError(f"project image build failed: {b.stderr[-1200:]}")
    image_id = image_exists(s, tag)
    if not image_id:
        raise ExecutionFailedError("project image was built but cannot be found")
    return ProjectImage(tag=tag, image_id=image_id, requirements=requirements, wheels=infos, reused=False,
                        container_python=pyver)
