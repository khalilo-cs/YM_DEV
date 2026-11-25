import { Async, Badge, ShortId, Stat, useApi, useI18n } from "../ui";

type Status = {
  schema_version: number; counts: Record<string, number>; pending_approvals: number; offline_capable: boolean;
  queue: { queued: number; running: number; waiting: number };
  models: { adapter: string; available: boolean; reason: string }[];
  sandbox: { isolation_effective: string; isolation_warnings: string[] };
};
type Task = { id: string; project_id: string; status: string; current_step: string | null; updated_at: string };

export default function Dashboard() {
  const { t, fmtNum, fmtDate } = useI18n();
  const st = useApi<Status>("/system/status");
  const tasks = useApi<Task[]>("/tasks");
  return (
    <section>
      <h1>{t("system_status")}</h1>
      <Async q={st}>{(s) => (
        <>
          <div className="stats">
            <Stat label={t("projects")} value={fmtNum(s.counts.projects)} />
            <Stat label={t("tasks")} value={fmtNum(s.counts.tasks)} />
            <Stat label={t("pending_approvals")} value={<a href="#/approvals">{fmtNum(s.pending_approvals)}</a>} />
            <Stat label={t("running_tasks")} value={<a href="#/tasks">{fmtNum(s.queue.running)}</a>} />
            <Stat label={t("queued_tasks")} value={<a href="#/tasks">{fmtNum(s.queue.queued)}</a>} />
            <Stat label={t("evidence")} value={fmtNum(s.counts.evidence_records)} />
            <Stat label={t("skills")} value={fmtNum(s.counts.skills)} />
            <Stat label={t("memory")} value={fmtNum(s.counts.memory_records)} />
          </div>
          <div className="grid2">
            <div className="card">
              <h2>{t("isolation")}</h2>
              <p>{t("effective_backend")}: <Badge v={s.sandbox.isolation_effective} /></p>
              {s.sandbox.isolation_warnings.length === 0 ? <p className="muted">{t("no_warnings")}</p> :
                <ul className="warn-list">{s.sandbox.isolation_warnings.map((w) => <li key={w}><bdi>{w}</bdi></li>)}</ul>}
            </div>
            <div className="card">
              <h2>{t("models")}</h2>
              <ul>{s.models.map((m) => <li key={m.adapter}><bdi>{m.adapter}</bdi>: <Badge v={m.available ? t("available") : t("unavailable")} tone={m.available ? "good" : "warn"} /> <span className="muted"><bdi>{m.reason}</bdi></span></li>)}</ul>
              <p>{t("offline_capable")}: {s.offline_capable ? t("yes") : t("no")} · {t("schema")}: {s.schema_version}</p>
            </div>
          </div>
        </>
      )}</Async>
      <h2>{t("recent_tasks")}</h2>
      <Async q={tasks} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("task")}</th><th>{t("status")}</th><th>{t("step")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.slice(0, 8).map((x) => (
            <tr key={x.id}><td><a href={`#/tasks/${x.id}`}><ShortId v={x.id} /></a></td><td><Badge v={x.status} /></td><td>{x.current_step ?? "—"}</td><td>{fmtDate(x.updated_at)}</td></tr>
          ))}</tbody></table>
      )}</Async>
    </section>
  );
}
