"""Real-container security tests. Skipped (with the reason) when Docker or the sandbox image is unavailable."""

import subprocess
import sys
import time

import pytest
from helpers import make_passing_project, make_renamed_api_project, write

from yemendev.core.config import SandboxSettings, Settings
from yemendev.sandbox.docker_backend import docker_availability
from yemendev.sandbox.testrunner import RunStatus, run_pytest
from yemendev.sandbox.workspace import Workspace

_avail = docker_availability(SandboxSettings(), use_cache=False)
pytestmark = pytest.mark.skipif(not _avail.available, reason=f"docker sandbox unavailable: {_avail.reason}")


@pytest.fixture
def ds():
    return SandboxSettings(backend="docker", timeout_seconds=60, max_memory_mb=256, docker_pids_limit=64)


def run_py(ws, code, timeout=None):
    return ws.run(["python", "-c", code], timeout=timeout)


def leftover_containers():
    out = subprocess.run(["docker", "ps", "-aq", "--filter", "label=org.yemendev.sandbox=1"],
                         capture_output=True, text=True).stdout
    return [x for x in out.split() if x]


def test_isolation_properties(tmp_path, ds):
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("TOP SECRET")
    src = make_passing_project(tmp_path / "p")
    code = f"""
import os, socket
print("uid", os.getuid())
for label, fn in [("net", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3)),
                  ("rootfs", lambda: open("/usr/evil", "w")),
                  ("host_secret", lambda: open({str(host_secret)!r}).read()),
                  ("source_dir", lambda: os.listdir({str(src)!r}))]:
    try:
        fn(); print(label, "OPEN")
    except OSError as e:
        print(label, "blocked", type(e).__name__)
print("cwd", os.getcwd(), sorted(os.listdir(".")))
"""
    with Workspace(src, ds) as ws:
        r = run_py(ws, code)
    assert r.backend == "docker" and r.image and r.image.startswith("sha256:")
    out = r.stdout
    assert "uid 0" not in out and f"uid {r.run_as_uid}" in out
    for label in ["net", "rootfs", "host_secret", "source_dir"]:
        assert f"{label} blocked" in out, (label, out, r.stderr)
    assert "cwd /work" in out


def test_platform_state_invisible_even_with_exact_path(tmp_path, ds):
    st = Settings(data_dir=tmp_path / "data")
    st.ensure_dirs()
    (st.keys_dir / "audit.key").write_bytes(b"k" * 32)
    with Workspace(make_passing_project(tmp_path / "p"), ds) as ws:
        r = run_py(ws, f"import os;print(os.path.exists({str(st.keys_dir / 'audit.key')!r}))")
    assert r.stdout.strip() == "False"


def test_writes_land_in_copy_not_source(tmp_path, ds):
    src = make_passing_project(tmp_path / "p")
    with Workspace(src, ds) as ws:
        r = run_py(ws, "open('pkg/m.py','w').write('CHANGED')")
        assert r.returncode == 0, r.stderr
        assert ws.path("pkg/m.py").read_text() == "CHANGED"
    assert (src / "pkg/m.py").read_text().startswith("def f")


def test_timeout_kills_container(tmp_path, ds):
    with Workspace(make_passing_project(tmp_path / "p"), ds) as ws:
        t = time.monotonic()
        r = run_py(ws, "import time; time.sleep(120)", timeout=3)
    assert r.timed_out and r.returncode is None and time.monotonic() - t < 30
    assert leftover_containers() == []


def test_fork_bomb_is_contained(tmp_path, ds):
    code = ("import os, time\nn = 0\ntry:\n    while True:\n        if os.fork() == 0:\n            time.sleep(30)\n"
            "            os._exit(0)\n        n += 1\nexcept OSError:\n    print('fork refused after', n)\n")
    with Workspace(make_passing_project(tmp_path / "p"), ds) as ws:
        r = run_py(ws, code, timeout=20)
    assert "fork refused after" in r.stdout, (r.stdout, r.stderr)
    n = int(r.stdout.split("fork refused after")[1].split()[0])
    assert n < ds.docker_pids_limit
    assert leftover_containers() == []


def test_memory_limit(tmp_path, ds):
    with Workspace(make_passing_project(tmp_path / "p"), ds) as ws:
        r = run_py(ws, "x = bytearray(1024 * 1024 * 1024); print('ALLOCATED')")
    assert "ALLOCATED" not in r.stdout and r.returncode != 0


def test_pytest_results_match_process_backend(tmp_path, ds):
    src = make_renamed_api_project(tmp_path / "p")
    reports = {}
    for backend in ("process", "docker"):
        with Workspace(src, ds.model_copy(update={"backend": backend})) as ws:
            reports[backend] = run_pytest(ws)
    assert reports["docker"].status is RunStatus.TESTS_FAILED
    assert reports["docker"].outcomes() == reports["process"].outcomes()
    assert reports["docker"].execution.backend == "docker"


def test_container_backend_satisfies_required_privilege_separation(tmp_path, ds, monkeypatch):
    """On a non-root host there is no uid to drop to; the container still provides separation."""
    monkeypatch.setattr(SandboxSettings, "sandbox_identity", lambda self: None)
    s = ds.model_copy(update={"privilege_separation": "required"})
    with Workspace(make_passing_project(tmp_path / "p"), s) as ws:
        r = run_py(ws, "import os; print(os.getuid())")
    assert r.returncode == 0 and r.stdout.strip() != "0"


def test_end_to_end_repair_on_docker_backend(tmp_path):
    from end_to_end.conftest import ALICE, BOB

    from yemendev.core.contracts import sha256_bytes
    from yemendev.governance.actors import Role
    from yemendev.orion.tasks import TaskStatus
    from yemendev.orion.workflow import RepairWorkflow
    from yemendev.platform import Platform
    from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan

    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(backend="docker", timeout_seconds=60)))
    wf = RepairWorkflow(p)
    root = make_renamed_api_project(tmp_path / "proj")
    write(root, "tests/test_evil.py", "import sqlite3\n\ndef test_evil():\n"
          f"    sqlite3.connect({str(p.settings.db_path)!r}).execute('DELETE FROM audit_events')\n")
    api = root / "calc/api.py"
    plan = RepairPlan(id="pln_d", description="fix", source=PlanSource.HUMAN, source_ref="x", patches=[FilePatch(
        path="calc/api.py", original_sha256=sha256_bytes(api.read_bytes()),
        new_content=api.read_text().replace("add_numbers", "add"))])
    proj = p.register_project(ALICE, "c", str(root))
    p.grant_member(ALICE, proj.id, BOB.ref, Role.OPERATOR)
    t = wf.create(ALICE, proj.id, human_plan=plan)
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.WAITING_APPROVAL
    p.decide_approval(BOB, p.approvals.for_task(t.id)[0].id, approve=True)
    assert wf.advance(ALICE, t.id).task.status is TaskStatus.SUCCEEDED
    assert p.verify_integrity()["valid"]  # the malicious test could not touch the audit log
    base = next(e for e in p.evidence.list(task_id=t.id) if e.action == "tests.baseline")
    assert base.body.environment["limits"]["backend"] == "docker"
    assert "tests.test_evil::test_evil" in base.body.tests["runs"][0]["failing"]
    assert sys.version_info >= (3, 11)
