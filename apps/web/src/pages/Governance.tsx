import { useState } from "react";
import { api, ApiError } from "../api";
import { Async, Badge, Code, ConfirmButton, ErrorBox, fmtBytes, ShortId, useApi, useI18n } from "../ui";
import { VulnReport, VulnTable } from "./Projects";

type Integrity = { valid: boolean; audit_events: number; evidence_records: number; problems: string[] };
type AuditEvent = { seq: number; created_at: string; actor: string; action: string; target: string; outcome: string };

export function Security() {
  const { t, fmtDate } = useI18n();
  const [res, setRes] = useState<Integrity | null>(null);
  const [err, setErr] = useState<Error | null>(null);
  const audit = useApi<AuditEvent[]>("/audit?limit=50");
  const verify = async () => {
    setErr(null);
    try { setRes(await api<Integrity>("/integrity/verify")); } catch (e) { setErr(e as Error); }
  };
  const adminOnly = audit.error instanceof ApiError && audit.error.status === 403;
  return (
    <section>
      <h1>{t("nav_security")}</h1>
      <button className="primary" onClick={verify}>{t("audit_verify")}</button>
      {err && (err instanceof ApiError && err.status === 403 ? <p className="muted">{t("admin_only")}</p> : <ErrorBox error={err} />)}
      {res && (res.valid ? <p className="ok">{t("integrity_ok")} ({res.audit_events} / {res.evidence_records})</p>
        : <div className="error"><strong>{t("integrity_bad")}</strong><ul>{res.problems.map((p) => <li key={p}><bdi>{p}</bdi></li>)}</ul></div>)}
      <SupplyChain />
      <h2>{t("audit_events")}</h2>
      {adminOnly ? <p className="muted">{t("admin_only")}</p> : (
        <Async q={audit} empty={(d) => d.length === 0}>{(d) => (
          <table><tbody>{[...d].reverse().map((e) => <tr key={e.seq}><td>{e.seq}</td><td>{fmtDate(e.created_at)}</td><td><Code>{e.actor}</Code></td><td><Code>{e.action}</Code></td><td><Badge v={e.outcome} /></td></tr>)}</tbody></table>
        )}</Async>
      )}
    </section>
  );
}

type SyncStatus = { transport: string; jobs: { id: string; project_id: string; direction: string; status: string; created_at: string; conflicts: number }[]; open_conflicts: Record<string, number>; note: string };
type Conflict = { id: string; subject: string; key: string; local_value: unknown; remote_value: unknown };

export function Continuity() {
  const { t, fmtDate } = useI18n();
  const [rev, setRev] = useState(0);  // readiness depends on backups: re-check after every backup action
  const q = useApi<SyncStatus>("/synchronization/status");
  return (
    <section>
      <h1>{t("nav_continuity")}</h1>
      <Readiness key={rev} />
      <Backups onChange={() => setRev((n) => n + 1)} />
      <h2>{t("synchronization")}</h2>
      <Async q={q}>{(s) => (
        <>
          <p>{t("transport")}: <bdi>{s.transport}</bdi></p>
          <p className="muted"><bdi>{s.note}</bdi></p>
          <h2>{t("jobs")}</h2>
          {s.jobs.length === 0 ? <p className="empty">{t("empty")}</p> : (
            <table><tbody>{s.jobs.map((j) => <tr key={j.id}><td><Code>{j.direction}</Code></td><td><Badge v={j.status} /></td><td>{j.conflicts}</td><td>{fmtDate(j.created_at)}</td></tr>)}</tbody></table>
          )}
          <h2>{t("conflicts")}</h2>
          {Object.keys(s.open_conflicts).length === 0 ? <p className="empty">{t("empty")}</p> :
            Object.keys(s.open_conflicts).map((pid) => <ProjectConflicts key={pid} projectId={pid} onChange={q.reload} />)}
        </>
      )}</Async>
    </section>
  );
}

function ProjectConflicts({ projectId, onChange }: { projectId: string; onChange: () => void }) {
  const { t } = useI18n();
  const q = useApi<Conflict[]>(`/projects/${projectId}/sync/conflicts?status=open`);
  const resolve = (id: string, resolution: string) => async () => {
    await api(`/sync/conflicts/${id}/resolve`, { method: "POST", body: { resolution } }); q.reload(); onChange();
  };
  return (
    <Async q={q} empty={(d) => d.length === 0}>{(d) => (
      <table><thead><tr><th>{t("subject")}</th><th>{t("local_value")}</th><th>{t("remote_value")}</th><th /></tr></thead>
        <tbody>{d.map((c) => (
          <tr key={c.id}><td><Code>{c.subject}/{c.key}</Code></td><td><Code>{JSON.stringify(c.local_value)}</Code></td><td><Code>{JSON.stringify(c.remote_value)}</Code></td>
            <td className="row"><ConfirmButton label={t("keep_local")} question={t("resolve_q")} kind="plain" onConfirm={resolve(c.id, "keep_local")} />
              <ConfirmButton label={t("take_remote")} question={t("resolve_q")} onConfirm={resolve(c.id, "take_remote")} /></td></tr>
        ))}</tbody></table>
    )}</Async>
  );
}

type AdvisoryStatus = { advisories: Record<string, number>; newest_advisory_modified: string | null; newest_advisory_age_days: number | null; last_import: { imported_at?: string; source?: string } | null; note: string };

function SupplyChain() {
  const { t, fmtNum, fmtDate } = useI18n();
  const adv = useApi<AdvisoryStatus>("/advisories/status");
  const sbom = useApi<{ components: unknown[] }>("/system/sbom");
  const [rep, setRep] = useState<VulnReport | null>(null);
  const [err, setErr] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true); setErr(null);
    try { setRep(await api<VulnReport>("/system/vulnerabilities")); } catch (e) { setErr(e as Error); } finally { setBusy(false); }
  };
  return (
    <>
      <h2>{t("supply_chain")}</h2>
      <div className="grid2">
        <div className="card">
          <h3>{t("advisory_db")}</h3>
          <Async q={adv}>{(a) => (
            <>
              <p>{Object.keys(a.advisories).length === 0 ? <Badge v={t("not_imported")} tone="warn" /> :
                Object.entries(a.advisories).map(([eco, n]) => <span key={eco}><Code>{eco}</Code>: {fmtNum(n)} </span>)}</p>
              <p className="muted">{t("newest_advisory")}: {a.newest_advisory_modified ? fmtDate(a.newest_advisory_modified) : "—"}
                {a.newest_advisory_age_days !== null && <> ({fmtNum(a.newest_advisory_age_days)} {t("days")})</>}</p>
              <p className="muted"><bdi>{a.note}</bdi></p>
              <p className="muted small"><Code>yemendev advisories import &lt;osv.zip&gt;</Code></p>
            </>
          )}</Async>
        </div>
        <div className="card">
          <h3>{t("platform_sbom")}</h3>
          <Async q={sbom}>{(b) => <p>{t("components")}: {fmtNum(b.components.length)} · <span className="muted">CycloneDX 1.5</span></p>}</Async>
          <button className="primary" onClick={check} disabled={busy}>{busy ? t("checking") : t("check_platform_deps")}</button>
        </div>
      </div>
      {err && <ErrorBox error={err} />}
      {rep && <VulnTable r={rep} />}
    </>
  );
}

type Readiness = { overall: string; checks: { check: string; status: string; detail: string }[]; generated_at: string };
const tone = (s: string) => (s === "ok" ? "good" : s === "warn" ? "warn" : "bad") as "good" | "warn" | "bad";

function Readiness() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Readiness>("/continuity/readiness");
  return (
    <>
      <h2>{t("readiness")} <button className="link" onClick={q.reload}>{t("refresh")}</button></h2>
      {q.error instanceof ApiError && q.error.status === 403 ? <p className="muted">{t("admin_only")}</p> : (
        <Async q={q}>{(r) => (
          <>
            <p>{t("overall")}: <Badge v={r.overall} tone={tone(r.overall)} /> <span className="muted">{fmtDate(r.generated_at)} · {t("no_combined_score")}</span></p>
            <table><tbody>{r.checks.map((c) => <tr key={c.check}><td><bdi>{c.check}</bdi></td><td><Badge v={c.status} tone={tone(c.status)} /></td><td className="small"><bdi>{c.detail}</bdi></td></tr>)}</tbody></table>
          </>
        )}</Async>
      )}
    </>
  );
}

type Backup = { id: string; created_at: string; created_by: string; size_bytes: number; archive_sha256: string; verified_at: string | null; verified_ok: boolean | null; file_present: boolean;
  verify_report: { checks: { name: string; ok: boolean; detail: string }[] } | null };

function Backups({ onChange }: { onChange: () => void }) {
  const { t, fmtDate, fmtNum } = useI18n();
  const q = useApi<Backup[]>("/continuity/backups");
  const [err, setErr] = useState<Error | null>(null);
  const run = (f: () => Promise<unknown>) => async () => { setErr(null); try { await f(); } catch (e) { setErr(e as Error); } q.reload(); onChange(); };
  const denied = q.error instanceof ApiError && q.error.status === 403;
  return (
    <>
      <h2>{t("backups")}</h2>
      {denied ? <p className="muted">{t("admin_only")}</p> : (
        <>
          <ConfirmButton label={t("create_backup")} question={t("create_backup_q")} onConfirm={run(() => api("/continuity/backups", { method: "POST" }))} />
          {err && (err instanceof ApiError && err.status === 403 ? <p className="muted">{t("admin_only")}</p> : <ErrorBox error={err} />)}
          <Async q={q} empty={(d) => d.length === 0}>{(d) => (
            <table><thead><tr><th>{t("backup")}</th><th>{t("created")}</th><th>{t("size")}</th><th>{t("verified")}</th><th /></tr></thead>
              <tbody>{d.map((b) => (
                <tr key={b.id}><td><ShortId v={b.id} /><div className="muted small"><Code>{b.archive_sha256.slice(0, 16)}…</Code></div></td><td>{fmtDate(b.created_at)}<div className="muted small"><Code>{b.created_by}</Code></div></td>
                  <td>{fmtBytes(b.size_bytes, fmtNum)}{!b.file_present && <> <Badge v={t("file_missing")} tone="bad" /></>}</td>
                  <td>{b.verified_ok === null ? <Badge v={t("not_verified")} tone="warn" /> : <Badge v={b.verified_ok ? t("verified_ok") : t("verify_failed")} tone={b.verified_ok ? "good" : "bad"} />}
                    {b.verified_at && <div className="muted small">{fmtDate(b.verified_at)}</div>}
                    {b.verify_report && b.verify_report.checks.some((c) => !c.ok) && <ul className="warn-list small">{b.verify_report.checks.filter((c) => !c.ok).map((c) => <li key={c.name}><bdi>{c.name}: {c.detail}</bdi></li>)}</ul>}</td>
                  <td><ConfirmButton label={t("verify")} question={t("verify_backup_q")} kind="plain" disabled={!b.file_present} onConfirm={run(() => api(`/continuity/backups/${b.id}/verify`, { method: "POST" }))} /></td></tr>
              ))}</tbody></table>
          )}</Async>
          <p className="muted small">{t("restore_hint")} <Code>yemendev backup restore &lt;id&gt; --to &lt;empty dir&gt;</Code></p>
        </>
      )}
    </>
  );
}
