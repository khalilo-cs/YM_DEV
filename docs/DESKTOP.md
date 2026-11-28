# Desktop application (stage 23)

`apps/desktop` is a native desktop shell built with [Tauri 2](https://tauri.app). It is a window around the
platform's own web UI, not a second UI: it starts the local platform (`yemendev serve` on 127.0.0.1), shows the
same Arabic/English interface, and stops the server it started when you close it.

## What it does

* Finds the platform: `YEMENDEV_BIN`, else `~/.config/yemendev/desktop.json` (`bin`, `data_dir`, `port`, written by
  `scripts/install.sh --with-desktop`), else `yemendev` on `PATH`. Data directory: `YEMENDEV_DATA_DIR`, else the
  file, else `~/.local/share/yemendev`. Port: `YEMENDEV_DESKTOP_PORT`, else the file, else 8765.
* If a YEMENDEV platform already answers on that port it is **reused and left running** on exit; anything else on
  the port is reported, not talked to.
* Otherwise it starts `yemendev serve --host 127.0.0.1` with that data directory (log: `desktop-server.log` in the
  data directory), waits until `/api/v1/health` answers, and opens `http://127.0.0.1:<port>/app/`. The server runs
  in its own process group, is told to exit if the shell dies, and is stopped (SIGTERM, then SIGKILL) on exit.
* A data directory that was never initialised is **explained** (with the `yemendev init` command), never
  initialised silently: the shell does not create admin tokens.
* Sign-in is the same as in a browser: paste a token (kept for the session only).

## Security

* The window may show only the platform origin (`http://127.0.0.1:<port>/`) and the shell's own page; every other
  navigation is refused (`on_navigation`).
* No native commands are exposed: the capability file grants nothing, and the remote platform page has no Tauri
  API (`window.__TAURI__` is undefined - checked end-to-end).
* The shell page has a strict CSP (`script-src 'self'`, `connect-src 'none'`); its one script shows the failure
  reason with `textContent`.
* The platform keeps its own protections (CSP, loopback-only bind, tokens, rate limits).

## Build

Requirements (Linux): Rust (cargo), Node.js/npm, `libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libssl-dev
build-essential file`. Build the web UI first (`npm ci && npm run build` in `apps/web`).

```
cd apps/desktop
npm ci
npm run build:binary   # src-tauri/target/release/yemendev-desktop
npm run build          # also a .deb in src-tauri/target/release/bundle/deb/
```

Or `scripts/install.sh --with-desktop`.

## Verification

* `cargo test` in `apps/desktop/src-tauri`: health parsing, navigation rules, port probing.
* `python scripts/desktop_e2e.py`: the real binary in a real WebKitGTK window (Xvfb), driven by WebDriver
  (`tauri-driver` + `WebKitWebDriver`): starting the platform, Arabic UI, sign-in, blocked navigation, no native API,
  server stopped on exit, reuse of a running platform, the uninitialised-data-directory explanation.

## Limitations

* Built and verified on Linux only (Ubuntu 24.04, WebKitGTK 2.52). Windows and macOS builds are not produced or
  tested; the code avoids Linux-only APIs except process-group handling, which has a plain fallback.
* No auto-update, no code signing, no system tray, no native notifications.
* The platform must be installed separately (the shell does not bundle Python).
