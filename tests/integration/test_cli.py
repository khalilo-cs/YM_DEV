import json

import pytest
from helpers import make_renamed_api_project

from yemendev.cli.main import main


def run(capsys, *args):
    rc = main(list(args))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    dd = str(tmp_path / "data")
    rc, out, _ = run(capsys, "--data-dir", dd, "init", "--actor", "alice")
    assert rc == 0
    admin = out.strip().splitlines()[-1]
    monkeypatch.setenv("YEMENDEV_TOKEN", admin)
    rc, out, _ = run(capsys, "--data-dir", dd, "token", "issue", "bob", "--role", "operator")
    bob = out.strip().splitlines()[-1]
    return dd, admin, bob


def test_init_refuses_second_time(cli, capsys):
    dd, *_ = cli
    assert run(capsys, "--data-dir", dd, "init")[0] == 1


def test_requires_token(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("YEMENDEV_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main(["--data-dir", str(tmp_path / "d"), "project", "list"])


def test_cli_workflow(cli, tmp_path, capsys, monkeypatch):
    dd, admin, bob = cli
    root = make_renamed_api_project(tmp_path / "proj")
    rc, out, _ = run(capsys, "--data-dir", dd, "project", "add", "calc", str(root))
    pid = json.loads(out)["id"]
    rc, out, _ = run(capsys, "--data-dir", dd, "project", "scan", pid)
    assert rc == 0 and json.loads(out)["modules"] >= 3
    # no skills and no model: honest NO_ACTION, nothing fabricated
    rc, out, _ = run(capsys, "--data-dir", dd, "task", "repair", pid)
    res = json.loads(out)
    assert res["status"] == "no_action" and "no model available" in res["message"]
    assert "add_numbers" in (root / "calc/api.py").read_text()
    rc, out, _ = run(capsys, "--data-dir", dd, "audit")
    assert rc == 0 and json.loads(out)["valid"]
    # operator cannot issue tokens
    monkeypatch.setenv("YEMENDEV_TOKEN", bob)
    rc, _, err = run(capsys, "--data-dir", dd, "token", "issue", "eve", "--role", "admin")
    assert rc == 1 and "permission_denied" in err


def test_serve_refuses_public_bind(cli, capsys):
    dd, *_ = cli
    with pytest.raises(SystemExit):
        main(["--data-dir", dd, "serve", "--host", "0.0.0.0"])  # noqa: S104


def test_cli_project_access_requires_grant(cli, tmp_path, capsys, monkeypatch):
    dd, admin, bob = cli
    root = make_renamed_api_project(tmp_path / "proj2")
    rc, out, _ = run(capsys, "--data-dir", dd, "project", "add", "calc", str(root))
    pid = json.loads(out)["id"]
    monkeypatch.setenv("YEMENDEV_TOKEN", bob)
    assert json.loads(run(capsys, "--data-dir", dd, "project", "list")[1]) == []
    rc, _, err = run(capsys, "--data-dir", dd, "project", "scan", pid)
    assert rc == 1 and "no access to this project" in err
    assert run(capsys, "--data-dir", dd, "audit")[0] == 1  # audit is admin-only
    monkeypatch.setenv("YEMENDEV_TOKEN", admin)
    assert run(capsys, "--data-dir", dd, "project", "grant", pid, "human:bob", "--role", "operator")[0] == 0
    monkeypatch.setenv("YEMENDEV_TOKEN", bob)
    assert [p["id"] for p in json.loads(run(capsys, "--data-dir", dd, "project", "list")[1])] == [pid]
    assert run(capsys, "--data-dir", dd, "project", "scan", pid)[0] == 0


def test_cli_queue_worker_and_trace(cli, tmp_path, capsys, monkeypatch):
    dd, admin, bob = cli
    from helpers import make_passing_project

    monkeypatch.setenv("YEMENDEV_SANDBOX_BACKEND", "process")
    root = make_passing_project(tmp_path / "ok")
    pid = json.loads(run(capsys, "--data-dir", dd, "project", "add", "ok", str(root))[1])["id"]
    rc, out, _ = run(capsys, "--data-dir", dd, "task", "repair", pid, "--queue", "--priority", "3", "--timeout", "120")
    queued = json.loads(out)
    tid = queued["task_id"]
    assert rc == 0 and queued["queued"]["state"] == "queued" and queued["queued"]["priority"] == 3
    entries = json.loads(run(capsys, "--data-dir", dd, "queue")[1])
    assert [e["task_id"] for e in entries] == [tid]
    # starting workers is an admin operation
    monkeypatch.setenv("YEMENDEV_TOKEN", bob)
    with pytest.raises(SystemExit, match="admin"):
        main(["--data-dir", dd, "worker", "--once"])
    monkeypatch.setenv("YEMENDEV_TOKEN", admin)
    rc, out, _ = run(capsys, "--data-dir", dd, "worker", "--once")
    [outcome] = json.loads(out)
    assert rc == 0 and outcome["task_id"] == tid and outcome["task_status"] == "no_action", outcome
    trace = json.loads(run(capsys, "--data-dir", dd, "task", "trace", tid)[1])
    assert trace["queue"]["state"] == "done" and trace["task"]["status"] == "no_action"
    assert [s["step"] for s in trace["plan"]][:3] == ["scan", "baseline", "diagnose"]
    assert trace["plan"][0]["status"] == "done" and trace["plan"][0]["duration_seconds"] is not None
    kinds = [e["kind"] for e in trace["events"]]
    for k in ("task.created", "queue.enqueued", "queue.claimed", "run.started", "step.started", "queue.run_finished"):
        assert k in kinds
    # dependencies are validated through the CLI as well
    rc, _, err = run(capsys, "--data-dir", dd, "task", "repair", pid, "--queue", "--after", "tsk_missing")
    assert rc == 1 and "does not exist" in err
