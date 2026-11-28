"""Build the sandbox container image offline-first.

Wheels are obtained on the host (``pip download``, which honours the host's proxy/CA settings) or
taken from a pre-staged directory, then installed in the image with ``--no-index``: the image build
itself never needs network access, and an air-gapped machine only needs the wheel files.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from yemendev.core.config import SandboxSettings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError, UnavailableError
from yemendev.sandbox.docker_backend import clear_availability_cache, docker_availability

DEFAULT_BASE_IMAGE = "python:3.13-slim"
DEFAULT_PYTEST = "9.2.0"

DOCKERFILE = """\
FROM {base}
COPY wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-index --find-links /tmp/wheels "pytest=={pytest}" \\
    && rm -rf /tmp/wheels \\
    && python -c "import pytest; assert pytest.__version__ == '{pytest}'"
{node}LABEL org.yemendev.sandbox="1" org.yemendev.pytest="{pytest}" org.yemendev.base="{base}"
USER 65534:65534
WORKDIR /work
"""


NODE_STEP = """\
COPY node /usr/local/bin/node
RUN chmod 0755 /usr/local/bin/node && node --version
LABEL org.yemendev.node="{version}"
"""


class ImageBuildResult(BaseModel):
    image: str
    image_id: str
    base_image: str
    pytest_version: str
    wheels: list[str]
    node_version: str | None = None


def _node_binary(path: Path) -> str:
    """Validate a Node.js binary to copy into the image; returns its version. Offline: nothing is downloaded."""
    if path.is_symlink():
        path = path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InvalidInputError(f"not an executable file: {path}")
    r = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
    version = r.stdout.strip()
    if r.returncode != 0 or not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise InvalidInputError(f"{path} does not look like a Node.js binary")
    return version


def _find_pip() -> list[str]:
    for py in (sys.executable, shutil.which("python3"), shutil.which("python")):
        if py and subprocess.run([py, "-m", "pip", "--version"], capture_output=True, timeout=30).returncode == 0:
            return [py, "-m", "pip"]
    raise UnavailableError("no pip found to download wheels; pass --wheels DIR with pre-downloaded wheels")


def build_sandbox_image(s: SandboxSettings, base_image: str = DEFAULT_BASE_IMAGE,
                        pytest_version: str = DEFAULT_PYTEST, wheels_dir: Path | None = None,
                        node_binary: Path | None = None) -> ImageBuildResult:
    """``node_binary``: copy this Node.js executable into the image so JavaScript/TypeScript suites can run in
    containers (it must be compatible with the base image's C library; it is started once during the build)."""
    if not all(c.isalnum() or c in "._-" for c in pytest_version):
        raise InvalidInputError("invalid pytest version")
    if not all(c.isalnum() or c in "._-:/@" for c in base_image):
        raise InvalidInputError("invalid base image reference")
    exe = shutil.which(s.docker_binary)
    if not exe:
        raise UnavailableError(f"docker binary '{s.docker_binary}' not found")
    with tempfile.TemporaryDirectory(prefix="ydv-image-") as tmp:
        ctx = Path(tmp)
        wheels = ctx / "wheels"
        if wheels_dir is not None:
            if not wheels_dir.is_dir():
                raise InvalidInputError("wheels directory does not exist")
            shutil.copytree(wheels_dir, wheels)
        else:
            r = subprocess.run([*_find_pip(), "download", "--only-binary=:all:", "--dest", str(wheels),
                                f"pytest=={pytest_version}"], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise ExecutionFailedError(f"wheel download failed: {r.stderr[-800:]}")
        names = sorted(p.name for p in wheels.glob("*.whl"))
        if not names:
            raise InvalidInputError("no wheels available for the image")
        node_version = None
        if node_binary is not None:
            node_version = _node_binary(node_binary)
            shutil.copyfile(node_binary.resolve(), ctx / "node")
        (ctx / "Dockerfile").write_text(DOCKERFILE.format(
            base=base_image, pytest=pytest_version,
            node=NODE_STEP.format(version=node_version) if node_version else ""))
        b = subprocess.run([exe, "build", "--network", "none", "-t", s.docker_image, str(ctx)],
                           capture_output=True, text=True, timeout=1800)
        if b.returncode != 0:
            raise ExecutionFailedError(f"docker build failed: {b.stderr[-1500:]}")
    clear_availability_cache()
    avail = docker_availability(s, use_cache=False)
    if not avail.available or not avail.image_id:
        raise ExecutionFailedError(f"image built but not usable: {avail.reason}")
    return ImageBuildResult(image=s.docker_image, image_id=avail.image_id, base_image=base_image,
                            pytest_version=pytest_version, wheels=names, node_version=node_version)
