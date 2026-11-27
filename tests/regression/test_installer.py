"""The one-command installer (scripts/install.sh): offline checks. A full install from a fresh clone on
Python 3.11 was verified manually in stage 18e (it needs PyPI and npm)."""

import os
import subprocess
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


def test_installer_parses_and_documents_its_options():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0
    r = run("--help")
    assert r.returncode == 0
    for opt in ("--data-dir", "--actor", "--token-file", "--python", "--no-ui", "--with-sandbox-image", "--dry-run"):
        assert opt in r.stdout


def test_dry_run_changes_nothing(tmp_path):
    data = tmp_path / "data"
    r = run("--dry-run", "--data-dir", str(data), "--actor", "alice", "--no-ui")
    assert r.returncode == 0, r.stderr
    assert "would run:" in r.stdout and "init --actor alice" in r.stdout
    assert not data.exists()  # nothing was created


def test_bad_input_is_rejected_before_doing_anything(tmp_path):
    r = run("--dry-run", "--actor", "bob; rm -rf /")
    assert r.returncode == 1 and "--actor may only contain" in r.stderr
    r = run("--dry-run", "--python", str(tmp_path / "no-such-python"))
    assert r.returncode == 1 and "Python 3.11 or newer is required" in r.stderr
    r = run("--frobnicate")
    assert r.returncode == 1 and "unknown option" in r.stderr
