import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import { dicts, localeOf, type Dict, type Lang } from "./i18n";

type Ctx = { lang: Lang; t: (k: keyof Dict) => string; fmtDate: (iso?: string | null) => string; fmtNum: (n: number) => string };
export const I18n = createContext<Ctx>(null as unknown as Ctx);
export const useI18n = () => useContext(I18n);

export function makeI18n(lang: Lang): Ctx {
  const d = dicts[lang];
  const dt = new Intl.DateTimeFormat(localeOf(lang), { dateStyle: "medium", timeStyle: "short" });
  const nf = new Intl.NumberFormat(localeOf(lang), { maximumFractionDigits: 3 });
  return {
    lang, t: (k) => d[k],
    fmtDate: (iso) => (iso ? dt.format(new Date(iso)) : "—"),
    fmtNum: (n) => nf.format(n),
  };
}

export type Loadable<T> = { data: T | null; error: ApiError | null; loading: boolean; reload: () => void };

export function useApi<T>(path: string | null): Loadable<T> {
  const [state, setState] = useState<{ data: T | null; error: ApiError | null; loading: boolean }>({ data: null, error: null, loading: !!path });
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!path) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    api<T>(path).then(
      (data) => live && setState({ data, error: null, loading: false }),
      (error: ApiError) => live && setState({ data: null, error, loading: false }),
    );
    return () => { live = false; };
  }, [path, n]);
  return { ...state, reload: useCallback(() => setN((x) => x + 1), []) };
}

export function Async<T>({ q, children, empty }: { q: Loadable<T>; children: (d: T) => ReactNode; empty?: (d: T) => boolean }) {
  const { t } = useI18n();
  if (q.loading && !q.data) return <p className="muted" role="status">{t("loading")}</p>;
  if (q.error) return <ErrorBox error={q.error} onRetry={q.reload} />;
  if (q.data === null) return null;
  if (empty && empty(q.data)) return <p className="empty">{t("empty")}</p>;
  return <>{children(q.data)}</>;
}

export function ErrorBox({ error, onRetry }: { error: ApiError | Error; onRetry?: () => void }) {
  const { t } = useI18n();
  const msg = error instanceof ApiError && error.status === 403 && error.message.includes("project") ? t("not_member") : error.message;
  return (
    <div className="error" role="alert">
      <strong>{t("error")}{error instanceof ApiError ? ` ${error.status}` : ""}:</strong> <bdi>{msg}</bdi>
      {onRetry && <button className="link" onClick={onRetry}>{t("retry")}</button>}
    </div>
  );
}

/** A button that asks for confirmation in an accessible dialog before running an action. */
export function ConfirmButton({ label, question, onConfirm, kind = "primary", disabled }: {
  label: string; question: string; onConfirm: () => Promise<unknown>; kind?: "primary" | "danger" | "plain"; disabled?: boolean;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const run = async () => {
    setBusy(true); setErr(null);
    try { await onConfirm(); ref.current?.close(); } catch (e) { setErr(e as Error); } finally { setBusy(false); }
  };
  return (
    <>
      <button className={kind} disabled={disabled || busy} onClick={() => { setErr(null); ref.current?.showModal(); }}>{label}</button>
      <dialog ref={ref} aria-label={label}>
        <p>{question}</p>
        {err && <ErrorBox error={err} />}
        <div className="row">
          <button className={kind} disabled={busy} onClick={run}>{busy ? t("loading") : t("confirm")}</button>
          <button className="plain" disabled={busy} onClick={() => ref.current?.close()}>{t("cancel")}</button>
        </div>
      </dialog>
    </>
  );
}

type Tone = "good" | "warn" | "bad" | "neutral";

/** Status pill. The colour is inferred from English API values; pass `tone` when `v` is translated text. */
export function Badge({ v, tone: forced }: { v: string; tone?: Tone }) {
  const tone = forced ?? (/fail|error|block|denied|high|security|rolled|cancel/.test(v) ? "bad"
    : /pending|waiting|caution|medium|stale|running|unverified|blind/.test(v) ? "warn"
    : /pass|succeed|verified|active|allow|ok|completed|detected|^done$/.test(v) ? "good" : "neutral");
  return <span className={`badge ${tone}`}><bdi>{v}</bdi></span>;
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return <div className="stat"><div className="stat-v">{value}</div><div className="stat-l">{label}</div></div>;
}

export const Code = ({ children }: { children: ReactNode }) => <code dir="ltr">{children}</code>;

/** Opaque ids are long; show a stable prefix that never wraps, with the full id on hover/long-press. */
export const ShortId = ({ v }: { v: string }) => <code dir="ltr" className="id" title={v}>{v.length > 16 ? v.slice(0, 16) + "…" : v}</code>;

/** Byte count in the largest unit that keeps it >= 1 (never shows a non-zero size as 0). */
export function fmtBytes(n: number, fmtNum: (x: number) => string): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${fmtNum(i === 0 ? v : Math.round(v * 10) / 10)} ${units[i]}`;
}
