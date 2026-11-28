import { useState } from "react";
import { api } from "../api";
import { Async, Badge, Code, ConfirmButton, ErrorBox, ShortId, useApi, useI18n } from "../ui";

type Agent = { name: string; responsibility: string; actor: string; allowed_actions: string[] };
type Run = { agent: string; operation: string; status: string; started_at: string; project_id: string | null };
type Model = { adapter: string; available: boolean; reason: string };

export function Agents() {
  const { t, fmtDate } = useI18n();
  const reg = useApi<Agent[]>("/agents");
  const models = useApi<Model[]>("/models");
  return (
    <section>
      <h1>{t("nav_agents")}</h1>
      <h2>{t("models")}</h2>
      <Async q={models}>{(m) => <ul>{m.map((x) => <li key={x.adapter}><Code>{x.adapter}</Code> <Badge v={x.available ? t("available") : t("unavailable")} tone={x.available ? "good" : "warn"} /> <span className="muted"><bdi>{x.reason}</bdi></span></li>)}</ul>}</Async>
      <Async q={reg}>{(agents) => (
        <table><thead><tr><th>{t("agent")}</th><th>{t("responsibility")}</th><th>{t("allowed_actions")}</th></tr></thead>
          <tbody>{agents.map((a) => <tr key={a.name}><td><Code>{a.name}</Code></td><td><bdi>{a.responsibility}</bdi></td><td>{a.allowed_actions.map((x) => <Code key={x}>{x} </Code>)}</td></tr>)}</tbody></table>
      )}</Async>
      <Queue />
      <Schedules />
      <h2>{t("agent_runs")}</h2>
      <Async q={reg}>{(agents) => <RecentRuns names={agents.map((a) => a.name)} fmtDate={fmtDate} />}</Async>
    </section>
  );
}

type QueueEntry = { id: string; task_id: string; project_id: string; state: string; priority: number; run_after: string; enqueued_by: string; depends_on: string[];
  attempts: number; max_attempts: number; timeout_seconds: number; worker: string | null; heartbeat_until: string | null; last_error: string | null };
type QueueView = { entries: QueueEntry[]; limits: Record<string, number> };

function Queue() {
  const { t, fmtDate } = useI18n();
  const q = useApi<QueueView>("/queue?limit=100");
  const [all, setAll] = useState(false);
  return (
    <>
      <h2>{t("queue")} <button className="link" onClick={q.reload}>{t("refresh")}</button></h2>
      <Async q={q}>{(v) => {
        const rows = v.entries.filter((e) => all || ["queued", "running", "waiting"].includes(e.state));
        return (
          <>
            <p className="muted small">{Object.entries(v.limits).map(([k, n]) => <span key={k}><Code>{k}</Code>={n} </span>)}</p>
            <label><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> {t("show_finished")}</label>
            {rows.length === 0 ? <p className="empty">{t("queue_empty")}</p> : (
              <table><thead><tr><th>{t("task")}</th><th>{t("status")}</th><th>{t("priority")}</th><th>{t("attempts")}</th><th>{t("run_after")}</th><th>{t("worker")}</th><th>{t("last_error")}</th></tr></thead>
                <tbody>{rows.map((e) => (
                  <tr key={e.id}><td><a href={`#/tasks/${e.task_id}`}><ShortId v={e.task_id} /></a>{e.depends_on.length > 0 && <div className="muted small">{t("depends_on")}: {e.depends_on.map((d) => <ShortId key={d} v={d} />)}</div>}</td>
                    <td><Badge v={e.state} /></td><td>{e.priority}</td><td>{e.attempts}/{e.max_attempts}</td><td>{fmtDate(e.run_after)}</td>
                    <td>{e.worker ? <Code>{e.worker}</Code> : "—"}{e.heartbeat_until && <div className="muted small">{fmtDate(e.heartbeat_until)}</div>}</td>
                    <td className="small"><bdi>{e.last_error ?? "—"}</bdi></td></tr>
                ))}</tbody></table>
            )}
          </>
        );
      }}</Async>
    </>
  );
}

type Schedule = { id: string; kind: string; project_id: string | null; interval_seconds: number; next_run_at: string; enabled: boolean;
  created_by: string; last_run_at: string | null; last_status: string | null; last_detail: string | null; runs: number; failures: number;
  disabled_reason: string | null; overdue: boolean };
const KINDS = ["scan", "repair", "vulnerabilities", "backup", "readiness"];
const PROJECT_KINDS = ["scan", "repair", "vulnerabilities"];
const INTERVALS: [number, string][] = [[1800, "30m"], [3600, "1h"], [21600, "6h"], [86400, "1d"], [604800, "7d"]];

function Schedules() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Schedule[]>("/schedules");
  const projects = useApi<{ id: string; name: string }[]>("/projects");
  const [kind, setKind] = useState("readiness");
  const [project, setProject] = useState("");
  const [every, setEvery] = useState(86400);
  const [err, setErr] = useState<Error | null>(null);
  const needsProject = PROJECT_KINDS.includes(kind);
  const act = (f: () => Promise<unknown>) => async () => { setErr(null); try { await f(); } catch (e) { setErr(e as Error); } q.reload(); };
  const label = (secs: number) => INTERVALS.find(([s]) => s === secs)?.[1] ?? `${Math.round(secs / 60)}m`;
  return (
    <>
      <h2>{t("schedules")}</h2>
      <p className="muted small">{t("schedules_note")}</p>
      <div className="row">
        <label>{t("kind")} <select value={kind} onChange={(e) => setKind(e.target.value)}>{KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select></label>
        {needsProject && <Async q={projects}>{(ps) => (
          <label>{t("project")} <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">—</option>{ps.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
        )}</Async>}
        <label>{t("every")} <select value={every} onChange={(e) => setEvery(Number(e.target.value))}>{INTERVALS.map(([s, l]) => <option key={s} value={s}>{l}</option>)}</select></label>
        <ConfirmButton label={t("add_schedule")} question={t("add_schedule_q")} disabled={needsProject && !project}
          onConfirm={act(() => api("/schedules", { method: "POST", body: { kind, project_id: needsProject ? project : null, interval_seconds: every } }))} />
      </div>
      {err && <ErrorBox error={err} />}
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("kind")}</th><th>{t("every")}</th><th>{t("next_run")}</th><th>{t("last_result")}</th><th>{t("created_by")}</th><th /></tr></thead>
          <tbody>{d.map((x) => (
            <tr key={x.id}>
              <td><Badge v={x.kind} />{x.project_id && <div className="small"><ShortId v={x.project_id} /></div>}</td>
              <td>{label(x.interval_seconds)}</td>
              <td>{x.enabled ? fmtDate(x.next_run_at) : <Badge v={t("disabled")} tone="warn" />}{x.overdue && <> <Badge v={t("overdue")} tone="bad" /></>}
                {x.disabled_reason && <div className="muted small"><bdi>{x.disabled_reason}</bdi></div>}</td>
              <td>{x.last_status ? <><Badge v={x.last_status} tone={x.last_status === "ok" ? "good" : x.last_status === "skipped" ? undefined : "warn"} />
                <div className="muted small"><bdi>{x.last_detail}</bdi></div><div className="muted small">{fmtDate(x.last_run_at)} · {x.runs}/{x.failures}</div></> : "—"}</td>
              <td><Code>{x.created_by}</Code></td>
              <td className="row">
                <button className="plain" onClick={act(() => api(`/schedules/${x.id}/${x.enabled ? "disable" : "enable"}`, { method: "POST" }))}>{x.enabled ? t("disable") : t("enable")}</button>
                <ConfirmButton label={t("delete")} question={t("delete_schedule_q")} kind="danger" onConfirm={act(() => api(`/schedules/${x.id}`, { method: "DELETE" }))} />
              </td>
            </tr>
          ))}</tbody></table>
      )}</Async>
    </>
  );
}

function RecentRuns({ names, fmtDate }: { names: string[]; fmtDate: (s?: string | null) => string }) {
  const { t } = useI18n();
  return <>{names.map((n) => <AgentRuns key={n} name={n} fmtDate={fmtDate} label={t("agent")} />)}</>;
}

function AgentRuns({ name, fmtDate, label }: { name: string; fmtDate: (s?: string | null) => string; label: string }) {
  const q = useApi<Run[]>(`/agents/${name}/runs?limit=5`);
  return (
    <Async q={q} empty={(d) => d.length === 0}>{(d) => (
      <details><summary>{label}: <Code>{name}</Code> ({d.length})</summary>
        <ul>{d.map((r, i) => <li key={i}><Code>{r.operation}</Code> <Badge v={r.status} /> {fmtDate(r.started_at)}</li>)}</ul>
      </details>
    )}</Async>
  );
}

type Skill = { id: string; version: number; status: string; spec: { name: string; description: string } };

export function Skills() {
  const { t } = useI18n();
  const q = useApi<Skill[]>("/skills");
  return (
    <section>
      <h1>{t("nav_skills")}</h1>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("name")}</th><th>{t("version")}</th><th>{t("status")}</th><th /></tr></thead>
          <tbody>{d.map((s) => (
            <tr key={s.id + s.version}><td><bdi>{s.spec.name}</bdi><div className="muted"><bdi>{s.spec.description}</bdi></div></td><td>{s.version}</td><td><Badge v={s.status} /></td>
              <td className="row">
                <ConfirmButton label={t("validate")} question={t("validate")} kind="plain" disabled={!["proposed", "rejected", "verified", "active"].includes(s.status)}
                  onConfirm={async () => { await api(`/skills/${s.id}/validate`, { method: "POST" }); q.reload(); }} />
                <ConfirmButton label={t("request_activation")} question={t("request_activation_q")} kind="plain" disabled={s.status !== "verified"}
                  onConfirm={async () => { await api(`/skills/${s.id}/activation-requests`, { method: "POST" }); q.reload(); }} />
              </td></tr>
          ))}</tbody></table>
      )}</Async>
    </section>
  );
}
