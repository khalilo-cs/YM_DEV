import { api } from "../api";
import { Async, Badge, Code, ConfirmButton, ShortId, useApi, useI18n } from "../ui";

type Task = { id: string; project_id: string; status: string; current_step: string | null; created_at: string; updated_at: string; error: string | null;
  steps?: { name: string; status: string; attempt: number; started_at: string | null }[] };
type Run = { agent: string; operation: string; status: string; started_at: string; error: string | null };
type Evidence = { id: string; action: string; actor: string; outcome: string; created_at: string; hash_valid: boolean };

export function TaskList() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Task[]>("/tasks");
  return (
    <section>
      <h1>{t("nav_tasks")}</h1>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("task")}</th><th>{t("status")}</th><th>{t("step")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((x) => <tr key={x.id}><td><a href={`#/tasks/${x.id}`}><ShortId v={x.id} /></a></td><td><Badge v={x.status} /></td><td>{x.current_step ?? "—"}</td><td>{fmtDate(x.created_at)}</td></tr>)}</tbody></table>
      )}</Async>
    </section>
  );
}

export function TaskDetail({ id }: { id: string }) {
  const { t, fmtDate } = useI18n();
  const task = useApi<Task>(`/tasks/${id}`);
  const runs = useApi<Run[]>(`/tasks/${id}/agent-runs`);
  const ev = useApi<Evidence[]>(`/evidence?task_id=${id}&limit=500`);
  const reloadAll = () => { task.reload(); runs.reload(); ev.reload(); };
  const terminal = (s: string) => ["succeeded", "no_action", "rolled_back", "failed", "cancelled"].includes(s);
  return (
    <section>
      <Async q={task}>{(x) => (
        <>
          <h1>{t("task")} <Code>{x.id}</Code> <Badge v={x.status} /></h1>
          {x.error && <p className="error"><bdi>{x.error}</bdi></p>}
          <div className="row">
            <ConfirmButton label={t("advance")} question={t("advance_q")} disabled={terminal(x.status)}
              onConfirm={async () => { await api(`/tasks/${id}/advance`, { method: "POST" }); reloadAll(); }} />
            <ConfirmButton label={t("cancel_task")} question={t("cancel_task_q")} kind="danger" disabled={!["pending", "waiting_approval"].includes(x.status)}
              onConfirm={async () => { await api(`/tasks/${id}/cancel`, { method: "POST" }); reloadAll(); }} />
            <a className="button plain" href={`#/projects/${x.project_id}`}>{t("nav_projects")}</a>
          </div>
          {["no_action", "rolled_back", "failed", "waiting_approval"].includes(x.status) && x.steps && x.steps.length > 0 &&
            <StopReason taskId={x.id} step={x.current_step ?? x.steps[x.steps.length - 1].name} status={x.status} />}
          <h2>{t("steps")}</h2>
          <table><thead><tr><th>{t("step")}</th><th>{t("status")}</th><th>{t("attempt")}</th><th>{t("created")}</th></tr></thead>
            <tbody>{(x.steps ?? []).map((s) => <tr key={s.name}><td><Code>{s.name}</Code></td><td><Badge v={s.status} /></td><td>{s.attempt}</td><td>{fmtDate(s.started_at)}</td></tr>)}</tbody></table>
        </>
      )}</Async>
      <h2>{t("agent_runs")}</h2>
      <Async q={runs} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("agent")}</th><th>{t("operation")}</th><th>{t("status")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((r, i) => <tr key={i}><td>{r.agent}</td><td>{r.operation}</td><td><Badge v={r.status} /></td><td>{fmtDate(r.started_at)}</td></tr>)}</tbody></table>
      )}</Async>
      <h2>{t("evidence")}</h2>
      <Async q={ev} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("action")}</th><th>{t("agent")}</th><th>{t("outcome")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((e) => <tr key={e.id}><td><Code>{e.action}</Code></td><td><Code>{e.actor}</Code></td><td><Badge v={e.hash_valid ? e.outcome : "hash invalid"} /></td><td>{fmtDate(e.created_at)}</td></tr>)}</tbody></table>
      )}</Async>
    </section>
  );
}

/** Why the workflow stopped, taken verbatim from the step's recorded output (not paraphrased). */
function StopReason({ taskId, step, status }: { taskId: string; step: string; status: string }) {
  const { t } = useI18n();
  const q = useApi<{ output: { reason?: string; message?: string } | null }>(`/tasks/${taskId}/steps/${step}`);
  const reason = q.data?.output?.reason ?? q.data?.output?.message;
  if (status === "waiting_approval") return <p className="notice">{t("waiting_hint")} <a href="#/approvals">{t("nav_approvals")}</a></p>;
  if (!reason) return null;
  return (
    <div className="notice">
      <strong>{t("stop_reason")}:</strong> <bdi dir="ltr">{reason}</bdi>
      {status === "no_action" && <p>{t("no_plan_hint")} <Code>yemendev task repair &lt;project_id&gt; --plan plan.json</Code></p>}
    </div>
  );
}

type Approval = { id: string; action: string; target: string; summary: string; requested_by: string; status: string; expires_at: string; task_id: string | null };

export function Approvals() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Approval[]>("/approvals?status=pending");
  return (
    <section>
      <h1>{t("nav_approvals")}</h1>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <div>{d.map((a) => (
          <div key={a.id} className="card">
            <p><Badge v={a.action} /> {t("target")}: <Code>{a.target}</Code> · {t("requested_by")}: <Code>{a.requested_by}</Code> · {t("expires")}: {fmtDate(a.expires_at)}
              {a.task_id && <> · <a href={`#/tasks/${a.task_id}`}>{t("task")}</a></>}</p>
            <pre className="diff" dir="ltr">{a.summary}</pre>
            <div className="row">
              <ConfirmButton label={t("approve")} question={t("approve_q")} onConfirm={async () => { await api(`/approvals/${a.id}`, { method: "POST", body: { approve: true } }); q.reload(); }} />
              <ConfirmButton label={t("deny")} question={t("deny_q")} kind="danger" onConfirm={async () => { await api(`/approvals/${a.id}`, { method: "POST", body: { approve: false, reason: "denied in web UI" } }); q.reload(); }} />
            </div>
          </div>
        ))}</div>
      )}</Async>
    </section>
  );
}
