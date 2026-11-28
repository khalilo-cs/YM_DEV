# ADR-0019: Desktop app as a Tauri shell around the platform's own web UI

**Context.** The directive asks for desktop and web clients. The web UI already covers every required area and is
tested end-to-end in a browser; the platform runs locally and offline.

**Decision.** A Tauri 2 shell (`apps/desktop`) opens one window on `http://127.0.0.1:<port>/app/`, served by the
platform itself, instead of bundling a copy of the UI that would call the API cross-origin. The shell manages the
platform's lifecycle (reuse if running, otherwise start and later stop it), refuses every other navigation, exposes
no native commands, and never initialises a data directory or creates tokens.

**Alternatives.** Bundling the UI into the app: two copies to keep in sync, CORS and a weaker CSP. Electron:
much larger, bundles Chromium. A native UI: a second UI to build and keep correct in two languages.

**Consequences.** One UI, one security model, every web UI improvement reaches the desktop. The shell is small
(Rust, a few hundred lines). Verified on Linux only; Windows/macOS packaging, signing and updates are future work.
The platform must be installed separately.
