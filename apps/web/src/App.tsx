import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, ApiError, clearToken, getToken, setToken } from "./api";
import { dirOf, type Dict, type Lang } from "./i18n";
import { I18n, makeI18n } from "./ui";
import Dashboard from "./pages/Dashboard";
import { ProjectDetail, ProjectList } from "./pages/Projects";
import { Approvals, TaskDetail, TaskList } from "./pages/Recovery";
import { Agents, Skills } from "./pages/Control";
import { Continuity, Security } from "./pages/Governance";
import { Research } from "./pages/Research";

const LANG_KEY = "ydv.lang";

function initialLang(): Lang {
  try { const v = localStorage.getItem(LANG_KEY); if (v === "ar" || v === "en") return v; } catch { /* storage blocked */ }
  return "ar";
}

function useHash(): string {
  const [h, setH] = useState(() => window.location.hash || "#/");
  useEffect(() => {
    const on = () => setH(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return h;
}

const NAV: [string, keyof Dict][] = [
  ["#/", "nav_dashboard"], ["#/projects", "nav_projects"], ["#/tasks", "nav_tasks"], ["#/approvals", "nav_approvals"],
  ["#/agents", "nav_agents"], ["#/skills", "nav_skills"], ["#/security", "nav_security"],
  ["#/continuity", "nav_continuity"], ["#/research", "nav_research"],
];

function route(hash: string) {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [a, b] = parts;
  switch (a) {
    case undefined: return <Dashboard />;
    case "projects": return b ? <ProjectDetail key={b} id={b} /> : <ProjectList />;
    case "tasks": return b ? <TaskDetail key={b} id={b} /> : <TaskList />;
    case "approvals": return <Approvals />;
    case "agents": return <Agents />;
    case "skills": return <Skills />;
    case "security": return <Security />;
    case "continuity": return <Continuity />;
    case "research": return <Research />;
    default: return <Dashboard />;
  }
}

function Login({ onDone }: { onDone: () => void }) {
  const { t } = useMemo(() => ({ t: makeI18n(initialLang()).t }), []);
  const [v, setV] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    setToken(v.trim());
    try { await api("/system/status"); onDone(); }
    catch (x) { clearToken(); setErr(x instanceof ApiError && x.status === 401 ? t("invalid_token") : (x as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <form className="login card" onSubmit={submit}>
      <h1>{t("app")}</h1>
      <p className="muted">{t("tagline")}</p>
      <h2>{t("login_title")}</h2>
      <p className="muted">{t("login_hint")}</p>
      <label>{t("token")}
        <input type="password" dir="ltr" autoComplete="off" value={v} onChange={(e) => setV(e.target.value)} required />
      </label>
      {err && <p className="error" role="alert"><bdi>{err}</bdi></p>}
      <button className="primary" disabled={busy || !v.trim()}>{busy ? t("loading") : t("sign_in")}</button>
    </form>
  );
}

export default function App() {
  const [lang, setLang] = useState<Lang>(initialLang);
  const [authed, setAuthed] = useState<boolean>(() => !!getToken());
  const hash = useHash();
  const i18n = useMemo(() => makeI18n(lang), [lang]);

  useEffect(() => {
    document.documentElement.lang = lang;
    document.documentElement.dir = dirOf(lang);
    document.title = `${i18n.t("app")} — ${i18n.t("tagline")}`;
    try { localStorage.setItem(LANG_KEY, lang); } catch { /* storage blocked */ }
  }, [lang, i18n]);

  // A 401 anywhere (expired or revoked token) returns the user to the sign-in form.
  useEffect(() => {
    const on = () => setAuthed(false);
    window.addEventListener("ydv:unauthenticated", on);
    return () => window.removeEventListener("ydv:unauthenticated", on);
  }, []);

  const active = (href: string) => (href === "#/" ? hash === "#/" || hash === "#" : hash.startsWith(href));

  return (
    <I18n.Provider value={i18n}>
      <header className="top">
        <a className="brand" href="#/">{i18n.t("app")}</a>
        <span className="muted tagline">{i18n.t("tagline")}</span>
        <span className="spacer" />
        <button className="plain" onClick={() => setLang(lang === "ar" ? "en" : "ar")} lang={lang === "ar" ? "en" : "ar"}>{i18n.t("language")}</button>
        {authed && <button className="plain" onClick={() => { clearToken(); setAuthed(false); }}>{i18n.t("logout")}</button>}
      </header>
      {!authed ? <main><Login key={lang} onDone={() => setAuthed(true)} /></main> : (
        <div className="layout">
          <nav aria-label="main">
            {NAV.map(([href, k]) => <a key={href} href={href} className={active(href) ? "on" : ""} aria-current={active(href) ? "page" : undefined}>{i18n.t(k)}</a>)}
          </nav>
          <main>{route(hash)}</main>
        </div>
      )}
    </I18n.Provider>
  );
}
