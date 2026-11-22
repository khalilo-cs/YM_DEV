import { useState } from "react";
import { api, ApiError } from "../api";
import { Async, Badge, Code, ConfirmButton, ErrorBox, useApi, useI18n } from "../ui";

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
  const q = useApi<SyncStatus>("/synchronization/status");
  return (
    <section>
      <h1>{t("nav_continuity")}</h1>
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

export function Research() {
  const { t } = useI18n();
  return (
    <section>
      <h1>{t("research_title")}</h1>
      <p>{t("research_body")}</p>
      <p>{t("research_hypotheses")}</p>
    </section>
  );
}
