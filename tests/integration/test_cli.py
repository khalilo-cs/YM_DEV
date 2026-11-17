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
