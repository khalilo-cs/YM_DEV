"""End-to-end check of the desktop shell (stage 23): the real app binary, a real window (WebKitGTK under Xvfb),
driven through WebDriver (tauri-driver + WebKitWebDriver), against a real `yemendev serve`.

Scenarios
1. the shell starts the platform, shows the Arabic UI, a human signs in; navigation away from the platform is
   blocked; closing the app stops the server it started;
2. a platform already running on the port is reused and left running when the app closes;
3. an uninitialised data directory is explained (with the command to fix it) instead of minting a token;
4. if the platform stops unexpectedly, the window says so (regression: the server was killed as soon as the
   thread that started it ended - Linux sends the parent-death signal when the spawning *thread* exits).

Requires: `npm run build` in apps/web, the shell built (`npm run build:binary` in apps/desktop), `tauri-driver`
(cargo install tauri-driver --locked), `WebKitWebDriver` (package webkit2gtk-driver) and Xvfb.

    python scripts/desktop_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from yemendev.core.config import Settings  # noqa: E402
from yemendev.governance.actors import Role  # noqa: E402
from yemendev.platform import Platform  # noqa: E402

APP = REPO / "apps/desktop/src-tauri/target/release/yemendev-desktop"
YD = Path(sys.executable).parent / "yemendev"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wd(method: str, path: str, body: dict[str, Any] | None = None, port: int = 4444) -> Any:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body or {}).encode() if method != "GET" else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - local driver
        return json.loads(r.read() or b"{}").get("value")


class Session:
    def __init__(self) -> None:
        caps = {"capabilities": {"alwaysMatch": {"tauri:options": {"application": str(APP)}}}}
        self.id = wd("POST", "/session", caps)["sessionId"]

    def url(self) -> str:
        return str(wd("GET", f"/session/{self.id}/url"))

    def js(self, script: str) -> Any:
        return wd("POST", f"/session/{self.id}/execute/sync", {"script": script, "args": []})

    def js_async(self, script: str) -> Any:
        return wd("POST", f"/session/{self.id}/execute/async", {"script": script, "args": []})

    def find(self, css: str) -> str | None:
        try:
            el = wd("POST", f"/session/{self.id}/element", {"using": "css selector", "value": css})
        except urllib.error.HTTPError:
            return None
        return next(iter(el.values()))

    def wait(self, cond: Any, timeout: float = 120) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if cond():
                    return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        return False

    def shot(self, path: Path) -> None:
        path.write_bytes(base64.b64decode(wd("GET", f"/session/{self.id}/screenshot")))

    def close(self) -> None:
        try:
            wd("DELETE", f"/session/{self.id}")
        except (urllib.error.URLError, OSError):
            pass


def initialised(data: Path) -> str:
    p = Platform(Settings.from_env(data_dir=data))
    return p.tokens.issue("desk", Role.ADMIN)[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path, default=None)
    args = ap.parse_args()
    missing = [n for n, ok in [("app binary", APP.is_file()), ("tauri-driver", shutil.which("tauri-driver")),
                               ("WebKitWebDriver", shutil.which("WebKitWebDriver")), ("Xvfb", shutil.which("Xvfb")),
                               ("web UI build", (REPO / "apps/web/dist/index.html").is_file())] if not ok]
    if missing:
        print("missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="ydv-desktop-e2e-") as tmp:
        t = Path(tmp)
        shots = args.shots or t / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        xvfb = subprocess.Popen(["Xvfb", ":97", "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs: list[subprocess.Popen[bytes]] = [xvfb]
        try:
            time.sleep(1)

            def driver(env_extra: dict[str, str]) -> subprocess.Popen[bytes]:
                env = {**os.environ, "DISPLAY": ":97", **env_extra}
                d = subprocess.Popen(["tauri-driver", "--port", "4444"], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                for _ in range(40):
                    if listening(4444):
                        break
                    time.sleep(0.25)
                return d

            # ---- 1. shell starts the platform; sign in; navigation guard; server stopped on exit -----------
            data = t / "data"
            token = initialised(data)
            port = free_port()
            d = driver({"YEMENDEV_BIN": str(YD), "YEMENDEV_DATA_DIR": str(data), "YEMENDEV_DESKTOP_PORT": str(port)})
            procs.append(d)
            s = Session()
            ui = f"http://127.0.0.1:{port}/app/"
            check(s.wait(lambda: s.url().startswith(ui)), f"the window shows the platform UI from {ui}")
            check(s.wait(lambda: s.find("form.login input[type=password]") is not None, 60), "login form shown")
            check(s.js("return document.documentElement.dir") == "rtl", "Arabic, right-to-left by default")
            el = s.find("form.login input[type=password]")
            wd("POST", f"/session/{s.id}/element/{el}/value", {"text": token})
            wd("POST", f"/session/{s.id}/element/{s.find('form.login button.primary')}/click")
            check(s.wait(lambda: s.find(".stats .stat") is not None, 60), "signed in: dashboard rendered in the shell")
            status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5).read())  # noqa: S310
            check(status["status"] == "ok", f"platform {status['version']} answers on 127.0.0.1:{port}")
            s.shot(shots / "desktop-dashboard-ar.png")
            s.js("window.location.href = 'https://example.com/'; return 1")
            time.sleep(2)
            check(s.url().startswith(ui), f"navigation to another site is blocked (still {s.url()})")
            # Tauri injects its IPC bridge into every page; what matters is that no call is allowed from it.
            answers = s.js_async(
                "const done = arguments[arguments.length - 1];"
                "const cmds = ['plugin:app|version', 'plugin:window|close', 'plugin:webview|create_webview_window',"
                " 'plugin:event|emit', 'plugin:path|resolve_directory', 'plugin:window|set_title'];"
                "Promise.all(cmds.map(c => window.__TAURI_INTERNALS__.invoke(c, {})"
                ".then(() => c + ': ALLOWED', e => c + ': ' + String(e)))).then(done);")
            check(s.js("return typeof window.__TAURI__") == "undefined"
                  and all("not allowed by ACL" in a for a in answers),
                  f"the platform page cannot call any native command ({len(answers)} tried, all refused by the ACL)")
            s.close()
            d.terminate()
            d.wait(timeout=10)
            check(s.wait(lambda: not listening(port), 20), "closing the app stopped the server it had started")

            # ---- 2. an already running platform is reused and left running ---------------------------------
            port2 = free_port()
            own = subprocess.Popen([str(YD), "serve", "--port", str(port2)],
                                   env={**os.environ, "YEMENDEV_DATA_DIR": str(data)},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(own)
            for _ in range(120):
                if listening(port2):
                    break
                time.sleep(0.25)
            d = driver({"YEMENDEV_BIN": str(YD), "YEMENDEV_DATA_DIR": str(data), "YEMENDEV_DESKTOP_PORT": str(port2)})
            procs.append(d)
            s = Session()
            check(s.wait(lambda: s.url().startswith(f"http://127.0.0.1:{port2}/app/")), "a running platform is reused")
            s.close()
            d.terminate()
            d.wait(timeout=10)
            time.sleep(3)
            check(listening(port2) and own.poll() is None, "a platform the shell did not start is left running")

            # ---- 3. uninitialised data directory: explained, no token minted -------------------------------
            empty = t / "empty"
            d = driver({"YEMENDEV_BIN": str(YD), "YEMENDEV_DATA_DIR": str(empty),
                        "YEMENDEV_DESKTOP_PORT": str(free_port())})
            procs.append(d)
            s = Session()
            check(s.wait(lambda: s.find("#failed:not([hidden]) #reason") is not None, 60), "failure page shown")
            reason = s.js("return document.getElementById('reason').textContent")
            check("not initialised" in reason and "init" in reason, f"reason names the fix: {reason.splitlines()[0]}")
            check(not (empty / "yemendev.sqlite3").exists(), "no database or token was created behind the user's back")
            s.shot(shots / "desktop-not-initialised-ar.png")
            s.close()
            d.terminate()
            d.wait(timeout=10)

            # ---- 4. the platform stops unexpectedly: the window says so -----------------------------------
            port4 = free_port()
            d = driver({"YEMENDEV_BIN": str(YD), "YEMENDEV_DATA_DIR": str(data), "YEMENDEV_DESKTOP_PORT": str(port4)})
            procs.append(d)
            s = Session()
            check(s.wait(lambda: s.url().startswith(f"http://127.0.0.1:{port4}/app/")), "UI shown")
            time.sleep(5)
            check(listening(port4), "the server is still running 5 s after start-up (it outlives the start-up thread)")
            subprocess.run(["pkill", "-KILL", "-f", f"serve --host 127.0.0.1 --port {port4}"], check=False)
            check(s.wait(lambda: s.find("#failed:not([hidden]) #reason") is not None, 30),
                  "crash reported in the window")
            reason = s.js("return document.getElementById('reason').textContent")
            check("stopped unexpectedly" in reason, f"reason: {reason.splitlines()[0]}")
            s.shot(shots / "desktop-platform-stopped-ar.png")
            s.close()
        finally:
            for p in reversed(procs):
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        print(f"screenshots: {shots}" if args.shots else "")
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
