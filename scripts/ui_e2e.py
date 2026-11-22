"""Browser end-to-end check of the web UI against a real `yemendev serve` (stage 16).

Prepares a fresh, throw-away data directory, two human admin tokens (alice, bob), a sample project
whose tests fail (a renamed function), and a human-authored repair plan; starts the server; then runs
apps/web/e2e/ui_e2e.cjs in Chromium. Requires: `npm ci && npm run build` in apps/web, Node.js, and
the `playwright` npm package with Chromium installed (resolvable by `require("playwright")`).

    python scripts/ui_e2e.py [--shots DIR]
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from helpers import make_renamed_api_project  # type: ignore[import-not-found]  # noqa: E402

from yemendev.core.config import Settings  # noqa: E402
from yemendev.governance.actors import Role  # noqa: E402
from yemendev.platform import Platform  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path, default=None, help="directory for screenshots")
    args = ap.parse_args()
    if not (REPO / "apps/web/dist/index.html").is_file():
        print("web UI not built: run `npm ci && npm run build` in apps/web", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="ydv-ui-e2e-") as tmp:
        t = Path(tmp)
        data = t / "data"
        p = Platform(Settings.from_env(data_dir=data))
        _, alice = p.tokens.issue("alice", Role.ADMIN)
        _, bob = p.tokens.issue("bob", Role.ADMIN)
        root = make_renamed_api_project(t / "calc")
        pid = p.register_project(p.tokens.authenticate(alice), "calc", str(root)).id  # type: ignore[arg-type]
        api = root / "calc/api.py"
        plan = {"id": "pln_ui_e2e", "description": "rename add_numbers -> add", "source": "human",
                "source_ref": "human:alice", "hypothesis_id": "H1",
                "patches": [{"path": "calc/api.py", "original_sha256": hashlib.sha256(api.read_bytes()).hexdigest(),
                             "new_content": api.read_text().replace("add_numbers", "add")}]}
        (t / "plan.json").write_text(json.dumps(plan))
        shots = args.shots or (t / "shots")
        shots.mkdir(parents=True, exist_ok=True)
        port = free_port()
        env = {**os.environ, "YEMENDEV_DATA_DIR": str(data)}
        server = subprocess.Popen([sys.executable, "-m", "yemendev.cli.main", "serve", "--port", str(port)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(base + "/api/v1/health", timeout=1)  # noqa: S310 - local server
                    break
                except OSError:
                    time.sleep(0.5)
            r = subprocess.run(["node", str(REPO / "apps/web/e2e/ui_e2e.cjs"), base, alice, bob, pid, str(shots),
                                str(t / "plan.json")], timeout=900)
            print(f"screenshots: {shots}" if args.shots else "")
            return r.returncode
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
