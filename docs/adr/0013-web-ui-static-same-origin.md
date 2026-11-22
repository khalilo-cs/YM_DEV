# ADR-0013: Web UI as a static, same-origin client of the public API

**Context.** Directive sections 16-17 ask for a web interface (dashboard, projects, digital twin, risks,
recovery, approvals, AI control, skills, security, continuity, research) with Arabic as a first-class
language, no fake metrics, and loading/empty/error states. The platform must keep working offline and
must not add a second authorization path.

**Decision.**
* `apps/web`: React 18 + TypeScript (strict) + Vite, exact dependency versions, no UI framework and no
  charting library. The build output is plain static files served by the same FastAPI process under
  `/app`; `/` redirects there. Nothing is loaded from a CDN: the Arabic font (IBM Plex Sans Arabic, SIL
  OFL 1.1) is bundled.
* The UI is only an API client. It signs in with the same bearer tokens, holds the token in
  `sessionStorage` (cleared with the tab; never a cookie, so no CSRF surface), and every number it shows
  comes from an API response. Every permission decision stays on the server; the UI only displays the
  server's refusal (e.g. self-approval returns 403 and is shown in the confirmation dialog).
* Strict CSP on `/app`: `default-src 'none'`, same-origin script/style/font/connect only, no inline
  script, `frame-ancestors 'none'`. API responses carry `default-src 'none'; frame-ancestors 'none'`.
  Static files are served without following symlinks.
* i18n: one dictionary per language; `const ar: Dict` makes the TypeScript build fail if any English
  string lacks an Arabic translation. Layout uses CSS logical properties so one stylesheet serves RTL
  and LTR; dates and numbers use `Intl` for the active locale.
* Sensitive actions (start repair, advance, cancel, approve, deny, resolve conflict, request skill
  activation) open a confirmation dialog stating the consequence first.

**Consequences.** A Node.js toolchain is needed to *build* the UI, not to run the platform. Hash routing
keeps the server free of SPA fallbacks. `sessionStorage` is readable by script in the page, so the CSP
(no third-party or inline script) is the main protection against token theft via XSS; React escapes all
rendered text. Desktop (Tauri) and mobile companion apps are not built (see FEATURE_STATUS).
