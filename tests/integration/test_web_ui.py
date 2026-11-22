"""Web UI serving (stage 16): static files under /app, strict CSP, no data without a token, offline assets."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yemendev.api.app import API_CSP, DEFAULT_WEB_DIR, UI_CSP, create_app
from yemendev.core.config import SandboxSettings, Settings
from yemendev.platform import Platform

WEB_SRC = Path(__file__).resolve().parents[2] / "apps" / "web"


def make_client(tmp_path: Path, web_dir: Path) -> TestClient:
    p = Platform(Settings(data_dir=tmp_path / "data", sandbox=SandboxSettings(timeout_seconds=60), web_dir=web_dir))
    return TestClient(create_app(p), raise_server_exceptions=False)


@pytest.fixture
def web(tmp_path):
    d = tmp_path / "site" / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text('<!doctype html><script type="module" src="/app/assets/app.js"></script>')
    (d / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "site" / "secret.txt").write_text("TOP-SECRET")
    return d


def test_ui_is_served_with_a_strict_csp(tmp_path, web):
    c = make_client(tmp_path, web)
    r = c.get("/app/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert csp == UI_CSP
    for weak in ("'unsafe-inline'", "'unsafe-eval'", "http:", "https:", "*"):
        assert weak not in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"  # index.html must pick up new builds
    a = c.get("/app/assets/app.js")
    assert a.status_code == 200 and "immutable" in a.headers["cache-control"]
    root = c.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/app/"


def test_ui_cannot_serve_files_outside_its_directory(tmp_path, web):
    (web / "link.txt").symlink_to(tmp_path / "site" / "secret.txt")
    c = make_client(tmp_path, web)
    for path in ["/app/%2e%2e/secret.txt", "/app/..%2fsecret.txt", "/app/link.txt",
                 "/app/assets/%2e%2e/%2e%2e/secret.txt"]:
        r = c.get(path)
        assert r.status_code == 404, path
        assert "TOP-SECRET" not in r.text


def test_ui_is_public_but_carries_no_data_and_api_still_requires_a_token(tmp_path, web):
    c = make_client(tmp_path, web)
    assert c.get("/app/").status_code == 200
    r = c.get("/api/v1/system/status")
    assert r.status_code == 401
    assert r.headers["content-security-policy"] == API_CSP


def test_missing_build_gives_a_clear_404(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    c = make_client(tmp_path, empty)
    for path in ["/app", "/app/", "/app/assets/x.js"]:
        r = c.get(path)
        assert r.status_code == 404
        assert "npm ci && npm run build" in r.json()["error"]["message"]
    assert c.get("/", follow_redirects=False).status_code == 404


def test_web_sources_load_nothing_from_the_network():
    """Offline-first: no CDN, no remote fonts, no analytics. Fonts are bundled (SIL OFL)."""
    files = [WEB_SRC / "index.html", *WEB_SRC.joinpath("src").rglob("*.ts"), *WEB_SRC.joinpath("src").rglob("*.tsx"),
             *WEB_SRC.joinpath("src").rglob("*.css")]
    assert len(files) > 5
    for f in files:
        text = f.read_text()
        assert not re.search(r"https?://|//cdn|@import\s+url", text), f"{f} references a remote resource"
    assert (WEB_SRC / "src" / "fonts" / "OFL-LICENSE.txt").is_file()


def test_built_ui_has_no_inline_script():
    """The CSP forbids inline script; a build that needs it would be blank in the browser."""
    index = DEFAULT_WEB_DIR / "index.html"
    if not index.is_file():
        pytest.skip("web UI not built (run `npm ci && npm run build` in apps/web)")
    html = index.read_text()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S)
    assert scripts
    for attrs, body in scripts:
        assert 'src="/app/' in attrs and not body.strip()
    assert not re.search(r"\son[a-z]+=", html) and "style=" not in html


def test_ui_type_checks_including_arabic_dictionary_completeness():
    """`const ar: Dict` makes tsc fail if any English UI string lacks an Arabic translation."""
    if shutil.which("node") is None or not (WEB_SRC / "node_modules" / ".bin" / "tsc").exists():
        pytest.skip("node or apps/web/node_modules not installed (run `npm ci` in apps/web)")
    r = subprocess.run([str(WEB_SRC / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", str(WEB_SRC)],
                       capture_output=True, text=True, timeout=300, env={**os.environ, "NO_COLOR": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
