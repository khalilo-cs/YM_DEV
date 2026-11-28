"""Stage 23: the desktop shell's security configuration cannot drift silently.

The behaviour itself is verified end-to-end in a real window by scripts/desktop_e2e.py (needs Xvfb, WebKitGTK,
tauri-driver) and by `cargo test` in apps/desktop/src-tauri; these checks need neither.
"""

import json
import re
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2] / "apps" / "desktop"


def test_no_native_capability_is_granted():
    caps = list((DESKTOP / "src-tauri" / "capabilities").glob("*.json"))
    assert caps
    for f in caps:
        c = json.loads(f.read_text())
        assert c["permissions"] == [], f  # neither the shell page nor the platform page gets native commands
        assert "remote" not in c, f       # no remote origin is ever granted IPC


def test_shell_page_has_a_strict_csp_and_no_global_api():
    conf = json.loads((DESKTOP / "src-tauri" / "tauri.conf.json").read_text())
    sec = conf["app"]["security"]
    assert "script-src 'self'" in sec["csp"] and "connect-src 'none'" in sec["csp"]
    assert "unsafe-inline" not in sec["csp"] and "unsafe-eval" not in sec["csp"]
    assert conf["app"]["withGlobalTauri"] is False and not sec.get("dangerousDisableAssetCspModification")
    assert conf["app"]["windows"] == []  # the only window is created in code, with the navigation guard


def test_failure_reason_is_shown_as_text_never_html():
    js = (DESKTOP / "src" / "shell.js").read_text()
    assert "textContent" in js and not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js)
    html = (DESKTOP / "src" / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son[a-z]+=", html)


def test_navigation_guard_is_wired_and_tokens_are_never_created():
    main = (DESKTOP / "src-tauri" / "src" / "main.rs").read_text()
    backend = (DESKTOP / "src-tauri" / "src" / "backend.rs").read_text()
    assert ".on_navigation(" in main and "navigation_allowed" in main
    calls = re.findall(r"\.args\(\[([^\]]*)\]", main + backend)
    # the only command the shell runs is a loopback `serve`: it explains `yemendev init`, never runs it
    assert calls == ['"serve", "--host", "127.0.0.1", "--port", &cfg.port.to_string()'], calls
