"""Per-project sandbox images (stage 11)."""

import subprocess

import pytest
from end_to_end.conftest import ALICE, BOB
from helpers import write

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError, PermissionDeniedError
from yemendev.governance.actors import Role
from yemendev.sandbox.docker_backend import docker_availability
from yemendev.sandbox.project_image import project_image_tag, runtime_requirements
from yemendev.sandbox.testrunner import RunStatus, run_pytest
from yemendev.sandbox.workspace import Workspace
from yemendev.twin.model import DeclaredDependency, ProjectSnapshot

_avail = docker_availability(SandboxSettings(), use_cache=False)
needs_docker = pytest.mark.skipif(not _avail.available, reason=f"docker sandbox unavailable: {_avail.reason}")


def snap(*deps):
    return ProjectSnapshot(root="/x", tree_hash="h", files=[], languages={}, modules=[], edges=[], findings=[],
                           skipped=[], sensitive_files=[], test_framework=None,
                           declared_dependencies=[DeclaredDependency(name=n, spec=s, source_file="pyproject.toml",
                                                                     group=g) for n, s, g in deps])


def test_requirement_selection_uses_main_and_test_groups_only():
    s = snap(("six", "six>=1.16", "main"), ("pytest", "pytest", "dev"), ("sphinx", "sphinx", "docs"),
             ("six", "six>=1.16", "main"))
    assert runtime_requirements(s) == ["pytest", "six>=1.16"]


@pytest.mark.parametrize("spec", ["git+https://github.com/x/y", "pkg @ file:///tmp/evil", "-e .",
                                  "--index-url http://evil", "pkg @ https://evil/pkg.whl"])
def test_dangerous_requirements_rejected(spec):
    with pytest.raises(InvalidInputError):
        runtime_requirements(snap(("x", spec, "main")))


def test_tag_is_deterministic_and_dependency_sensitive():
    a = project_image_tag("sha256:base", ["six>=1.16"])
    assert a == project_image_tag("sha256:base", ["six>=1.16"]) and a.startswith("yemendev-proj:")
    assert a != project_image_tag("sha256:base", ["six>=1.17"])
    assert a != project_image_tag("sha256:other", ["six>=1.16"])


def _six_project(root):
    write(root, "pyproject.toml", '[project]\nname="usesix"\nversion="0.1"\ndependencies=["six>=1.16"]\n')
    write(root, "usesix/__init__.py", "")
    write(root, "usesix/core.py",
          "import six\n\n\ndef kind(x):\n    return 'text' if isinstance(x, six.string_types) else 'other'\n")
    write(root, "tests/test_core.py",
          "from usesix.core import kind\n\n\ndef test_kind():\n    assert kind('a') == 'text'\n")
    return root


@needs_docker
def test_project_image_end_to_end(tmp_path):
    p = __import__("yemendev.platform", fromlist=["Platform"]).Platform(
        Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(backend="docker", timeout_seconds=120)))
    root = _six_project(tmp_path / "proj")
    proj = p.register_project(ALICE, "usesix", str(root))
    p.scan_project(ALICE, proj.id)
    tag = project_image_tag(_avail.image_id, ["six>=1.16"])
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)  # start from a known state

    # 1. without a project image: base image, explicit degradation note, the dependency is missing
    sb, note = p.sandbox_for(proj.id)
    assert sb.docker_image == "yemendev-sandbox:py3.13" and "no project sandbox image" in note
    with Workspace(root, sb) as ws:
        before = run_pytest(ws)
    assert before.status in (RunStatus.COLLECTION_ERROR, RunStatus.TESTS_FAILED)
    assert "six" in (before.execution.stdout + before.execution.stderr)

    # 2. only admins may build (it downloads third-party code)
    p.grant_member(ALICE, proj.id, BOB.ref, Role.OPERATOR)
    with pytest.raises(PermissionDeniedError):
        p.build_project_image(BOB, proj.id)

    img = p.build_project_image(ALICE, proj.id)
    assert not img.reused and img.tag == tag and img.container_python.startswith("3.")
    assert any(w.name.startswith("six-") and len(w.sha256) == 64 for w in img.wheels)
    ev = [e for e in p.evidence.list(limit=1000) if e.action == "sandbox.project_image"][-1]
    assert ev.outcome == "built" and ev.body.after_state["wheels"]

    # 3. the project now runs on its own image and its tests pass
    sb, note = p.sandbox_for(proj.id)
    assert sb.docker_image == tag and note is None
    with Workspace(root, sb) as ws:
        after = run_pytest(ws)
    assert after.status is RunStatus.ALL_PASSED, after.execution.stdout[-500:]

    # 4. unchanged dependencies -> reused, not rebuilt
    assert p.build_project_image(ALICE, proj.id).reused

    # 5. changing the dependencies yields a different image (stale image not used)
    write(root, "pyproject.toml", '[project]\nname="usesix"\nversion="0.1"\ndependencies=["six>=1.15"]\n')
    p.scan_project(ALICE, proj.id)
    sb, note = p.sandbox_for(proj.id)
    assert sb.docker_image != tag and note
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@needs_docker
def test_unresolvable_dependency_fails_clearly(tmp_path):
    from yemendev.sandbox.project_image import build_project_image
    with pytest.raises(ExecutionFailedError, match="dependency download failed"):
        build_project_image(SandboxSettings(backend="docker"), ["yemendev-no-such-package-zz9==0.0.1"])
