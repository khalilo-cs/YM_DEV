import { useEffect } from "react";
import { api } from "../api";
import { Async, Badge, Code, ConfirmButton, ShortId, useApi, useI18n } from "../ui";

type Task = { id: string; project_id: string; status: string; current_step: string | null; created_at: string; updated_at: string;
  error: string | null; cancel_requested_at?: string | null; cancel_requested_by?: string | null; queue_state?: string | null };
type Run = { agent: string; operation: string; status: string; started_at: string; error: string | null };
type Evidence = { id: string; action: string; actor: string; outcome: string; created_at: string; hash_valid: boolean };
type PlanStep = { step: string; after: string; performed_by: string; status: string; attempt: number; started_at: string | null;
  duration_seconds: number | null; error: string | null };
type QueueEntry = { state: string; priority: number; attempts: number; max_attempts: number; run_after: string; timeout_seconds: number;
  last_error: string | null; depends_on: string[]; enqueued_by: string };
type TaskEvent = { seq: number; at: string; kind: string; actor: string; detail: Record<string, unknown> };
type Trace = { task: Task; queue: QueueEntry | null; plan: PlanStep[]; events: TaskEvent[]; agent_runs: Run[] };

const TERMINAL = ["succeeded", "no_action", "rolled_back", "failed", "cancelled"];
const isActive = (tr: Trace) => ["pending", "running"].includes(tr.task.status) || ["queued", "running"].includes(tr.queue?.state ?? "");

export function TaskList() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Task[]>("/tasks");
  return (
    <section>
      <h1>{t("nav_tasks")}</h1>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("task")}</th><th>{t("status")}</th><th>{t("queue")}</th><th>{t("step")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((x) => <tr key={x.id}><td><a href={`#/tasks/${x.id}`}><ShortId v={x.id} /></a></td><td><Badge v={x.status} /></td>
            <td>{x.queue_state ? <Badge v={x.queue_state} /> : "—"}</td><td>{x.current_step ?? "—"}</td><td>{fmtDate(x.created_at)}</td></tr>)}</tbody></table>
      )}</Async>
    </section>
  );
}

/** Short, readable summary of an event's details (the full record stays available through the API). */
function summary(d: Record<string, unknown>): string {
  const keys = ["step", "status", "attempt", "priority", "reason", "error", "delay_seconds", "wall_seconds", "cpu_seconds", "max_rss_mb", "containers_removed", "role", "acting_as"];
  return keys.filter((k) => d[k] !== undefined && d[k] !== null).map((k) => `${k}=${typeof d[k] === "object" ? JSON.stringify(d[k]) : String(d[k])}`).join(" · ");
}

export function TaskDetail({ id }: { id: string }) {
  const { t, fmtDate, fmtNum } = useI18n();
  const tr = useApi<Trace>(`/tasks/${id}/trace`);
  const ev = useApi<Evidence[]>(`/evidence?task_id=${id}&limit=500`);
  const reloadAll = () => { tr.reload(); ev.reload(); };
  const active = tr.data ? isActive(tr.data) : false;
  useEffect(() => {  // follow background runs without a manual refresh
    if (!active) return;
    const h = window.setInterval(() => { tr.reload(); ev.reload(); }, 2000);
    return () => window.clearInterval(h);
  }, [active, tr.reload, ev.reload]);
  return (
    <section>
      <Async q={tr}>{({ task: x, queue, plan, events, agent_runs }) => (
        <>
          <h1>{t("task")} <Code>{x.id}</Code> <Badge v={x.status} /> {active && <span className="muted live">{t("live")}</span>}</h1>
          {x.error && <p className="error"><bdi>{x.error}</bdi></p>}
          {x.cancel_requested_at && !TERMINAL.includes(x.status) &&
            <p className="notice">{t("cancel_requested")} <Code>{x.cancel_requested_by ?? ""}</Code></p>}
          <div className="row">
            <ConfirmButton label={t("advance")} question={t("advance_q")} disabled={TERMINAL.includes(x.status) || x.status === "running"}
              onConfirm={async () => { await api(`/tasks/${id}/advance`, { method: "POST" }); reloadAll(); }} />
            <ConfirmButton label={t("run_background")} question={t("run_background_q")} kind="plain"
              disabled={TERMINAL.includes(x.status) || ["queued", "running", "waiting"].includes(queue?.state ?? "")}
              onConfirm={async () => { await api(`/tasks/${id}/enqueue`, { method: "POST", body: {} }); reloadAll(); }} />
            <ConfirmButton label={t("cancel_task")} question={t("cancel_task_q")} kind="danger"
              disabled={!["pending", "waiting_approval", "running"].includes(x.status) || !!x.cancel_requested_at}
              onConfirm={async () => { await api(`/tasks/${id}/cancel`, { method: "POST" }); reloadAll(); }} />
            <a className="button plain" href={`#/projects/${x.project_id}`}>{t("nav_projects")}</a>
          </div>
          {["no_action", "rolled_back", "failed", "waiting_approval"].includes(x.status) && plan.some((s) => s.status !== "not_started") &&
            <StopReason taskId={x.id} step={x.current_step ?? plan.filter((s) => s.status !== "not_started").slice(-1)[0].step} status={x.status} />}
          {queue && (
            <div className="card">
              <h2>{t("queue")} <Badge v={queue.state} /></h2>
              <p>{t("attempts")}: {fmtNum(queue.attempts)} / {fmtNum(queue.max_attempts)} · {t("priority")}: {fmtNum(queue.priority)} ·
                {" "}{t("timeout")}: {fmtNum(queue.timeout_seconds)} {t("seconds")} · {t("requested_by")}: <Code>{queue.enqueued_by}</Code>
                {queue.state === "queued" && <> · {t("run_after")}: {fmtDate(queue.run_after)}</>}</p>
              {queue.depends_on.length > 0 && <p>{t("depends_on")}: {queue.depends_on.map((d) => <a key={d} href={`#/tasks/${d}`}><ShortId v={d} /> </a>)}</p>}
              {queue.last_error && <p className="muted"><bdi dir="ltr">{queue.last_error}</bdi></p>}
            </div>
          )}
          <h2>{t("plan")}</h2>
          <table><thead><tr><th>{t("step")}</th><th>{t("performed_by")}</th><th>{t("status")}</th><th>{t("attempt")}</th><th>{t("duration")}</th></tr></thead>
            <tbody>{plan.map((s) => (
              <tr key={s.step}><td><Code>{s.step}</Code></td><td><bdi>{s.performed_by}</bdi></td>
                <td><Badge v={s.status} /></td><td>{s.attempt || "—"}</td>
                <td>{s.duration_seconds === null ? "—" : `${fmtNum(s.duration_seconds)} ${t("seconds")}`}</td></tr>
            ))}</tbody></table>
          <h2>{t("timeline")}</h2>
          <table className="timeline-table"><thead><tr><th>{t("created")}</th><th>{t("event")}</th><th>{t("agent")}</th><th>{t("details")}</th></tr></thead>
            <tbody>{events.slice(-150).map((e) => (
              <tr key={e.seq}><td className="nowrap">{fmtDate(e.at)}</td><td><Code>{e.kind}</Code></td><td><Code>{e.actor}</Code></td>
                <td><bdi dir="ltr" className="small">{summary(e.detail)}</bdi></td></tr>
            ))}</tbody></table>
          <h2>{t("agent_runs")}</h2>
          {agent_runs.length === 0 ? <p className="empty">{t("empty")}</p> : (
            <table><thead><tr><th>{t("agent")}</th><th>{t("operation")}</th><th>{t("status")}</th><th>{t("created")}</th></tr></thead>
              <tbody>{agent_runs.map((r, i) => <tr key={i}><td>{r.agent}</td><td>{r.operation}</td><td><Badge v={r.status} /></td><td>{fmtDate(r.started_at)}</td></tr>)}</tbody></table>
          )}
        </>
      )}</Async>
      <h2>{t("evidence")}</h2>
      <Async q={ev} empty={(d) => d.length === 0}>{(d) => (
        <table className="evidence-table"><thead><tr><th>{t("action")}</th><th>{t("agent")}</th><th>{t("outcome")}</th><th>{t("created")}</th></tr></thead>
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
