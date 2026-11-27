import { useState } from "react";
import { Async, Badge, Code, Stat, useApi, useI18n } from "../ui";

type Row = {
  source: string; name: string; readable: boolean; kind?: string; started_at?: string; git_commit?: string | null;
  sandbox_backend?: string | null; results?: number; summary?: Record<string, unknown>; not_run?: Record<string, string> | null;
};
type ArmResult = { arm: string; scenario: string; outcome: string; correct: boolean; regression: boolean; data_loss: boolean; approvals: number; evidence_records: number; seconds: number; note: string };
type ScenarioResult = { scenario: string; category: string; expected_status: string; actual_status: string; correct: boolean; seconds_to_terminal: number; integrity_notes: string[] };
type Report = {
  benchmark?: string; benchmark_version?: string; seed?: number; git_commit?: string | null; started_at?: string;
  environment?: Record<string, unknown>; caveats?: string[]; arms?: Record<string, string>; not_run?: Record<string, string>;
  summary?: Record<string, unknown>; results: (ArmResult | ScenarioResult)[];
};

const show = (v: unknown) => (v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

export function Research() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Row[]>("/research/benchmarks");
  const [sel, setSel] = useState<Row | null>(null);
  return (
    <section>
      <h1>{t("research_title")}</h1>
      <p className="muted">{t("research_body")}</p>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("report")}</th><th>{t("kind")}</th><th>{t("source")}</th><th>{t("backend")}</th><th>{t("result")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((r) => (
            <tr key={r.source + r.name} className={sel?.name === r.name && sel.source === r.source ? "selected" : undefined}>
              <td>{r.readable ? <button className="link" onClick={() => setSel(r)}><Code>{r.name}</Code></button> : <><Code>{r.name}</Code> <Badge v={t("unreadable")} tone="warn" /></>}</td>
              <td>{r.kind ? <Badge v={r.kind} /> : "—"}</td><td>{r.source}</td><td>{show(r.sandbox_backend)}</td>
              <td>{r.kind === "resilience" ? `${show(r.summary?.correct)}/${show(r.summary?.scenarios)}` : r.kind === "compare" ? Object.keys(r.summary ?? {}).join(" · ") : "—"}
                {r.not_run && Object.keys(r.not_run).length > 0 && <> <Badge v={`${t("not_run")}: ${Object.keys(r.not_run).join(",")}`} tone="warn" /></>}</td>
              <td>{r.started_at ? fmtDate(r.started_at) : "—"}</td>
            </tr>
          ))}</tbody></table>
      )}</Async>
      {sel && <ReportView key={sel.source + sel.name} row={sel} />}
      <p className="muted">{t("research_hypotheses")}</p>
    </section>
  );
}

function ReportView({ row }: { row: Row }) {
  const { t } = useI18n();
  const q = useApi<{ report: Report }>(`/research/benchmarks/${encodeURIComponent(row.source)}/${encodeURIComponent(row.name)}`);
  return (
    <div className="card" data-testid="report">
      <h2><Code>{row.name}</Code></h2>
      <Async q={q}>{({ report: r }) => (
        <>
          <p className="muted">{t("seed")}: {show(r.seed)} · {t("commit")}: <Code>{(r.git_commit ?? "—").slice(0, 10)}</Code>
            {r.environment?.git_dirty ? <> <Badge v={t("dirty_tree")} tone="warn" /></> : null} · {t("version")}: {show(r.benchmark_version)}</p>
          {r.benchmark === "yemendev-compare" ? <Compare r={r} /> : <Resilience r={r} />}
          <h3>{t("caveats")}</h3>
          <ul className="muted">{(r.caveats ?? []).map((c) => <li key={c}><bdi>{c}</bdi></li>)}</ul>
        </>
      )}</Async>
    </div>
  );
}

function Compare({ r }: { r: Report }) {
  const { t } = useI18n();
  const rows = r.results as ArmResult[];
  const arms = Object.keys(r.summary ?? {});
  const scenarios = [...new Set(rows.map((x) => x.scenario))];
  const cell = (arm: string, sc: string) => rows.find((x) => x.arm === arm && x.scenario === sc);
  return (
    <>
      <h3>{t("arms")}</h3>
      <ul>{Object.entries(r.arms ?? {}).map(([k, v]) => <li key={k}><strong>{k}</strong>: <bdi>{v}</bdi>{r.not_run?.[k] && <> — <Badge v={t("not_run")} tone="warn" /> <span className="muted"><bdi>{r.not_run[k]}</bdi></span></>}</li>)}</ul>
      <table><thead><tr><th>{t("arm")}</th><th>{t("correct")}</th><th>{t("regressions")}</th><th>{t("data_loss")}</th><th>{t("approvals_n")}</th><th>{t("evidence")}</th></tr></thead>
        <tbody>{arms.map((a) => { const s = (r.summary ?? {})[a] as Record<string, unknown>; return (
          <tr key={a}><td><strong>{a}</strong></td><td>{show(s.correct)}</td><td>{show(s.regressions)}</td><td>{show(s.data_loss)}</td><td>{show(s.human_approvals)}</td><td>{show(s.evidence_records)}</td></tr>); })}</tbody></table>
      <h3>{t("per_scenario")}</h3>
      <table><thead><tr><th>{t("scenario")}</th>{arms.map((a) => <th key={a}>{a}</th>)}</tr></thead>
        <tbody>{scenarios.map((sc) => (
          <tr key={sc}><td><Code>{sc}</Code></td>{arms.map((a) => { const c = cell(a, sc); return (
            <td key={a}>{c ? <><Badge v={c.correct ? t("ok") : t("wrong")} tone={c.correct ? "good" : "bad"} /> <span className="muted">{c.outcome}</span>
              {c.regression && <> <Badge v={t("regression")} tone="bad" /></>}{c.data_loss && <> <Badge v={t("data_loss")} tone="bad" /></>}</> : "—"}</td>); })}</tr>
        ))}</tbody></table>
    </>
  );
}

function Resilience({ r }: { r: Report }) {
  const { t } = useI18n();
  const rows = r.results as ScenarioResult[];
  const s = r.summary ?? {};
  const keys = ["recovery_success_rate", "mean_time_to_recovery_seconds", "data_loss_incidents", "regression_rate",
    "skill_transfer_success", "knowledge_retention", "false_positive_rate", "false_negative_rate", "tamper_detection",
    "unauthorized_operations_blocked", "cpu_seconds_total"];
  return (
    <>
      <div className="stats"><Stat label={t("correct")} value={`${show(s.correct)}/${show(s.scenarios)}`} />
        <Stat label={t("integrity_violations")} value={show(s.data_integrity_violations)} /></div>
      <table><tbody>{keys.filter((k) => k in s).map((k) => <tr key={k}><td><Code>{k}</Code></td><td>{show(s[k])}</td></tr>)}</tbody></table>
      <h3>{t("per_scenario")}</h3>
      <table><thead><tr><th>{t("scenario")}</th><th>{t("category")}</th><th>{t("expected")}</th><th>{t("actual")}</th><th>{t("result")}</th></tr></thead>
        <tbody>{rows.map((x) => (
          <tr key={x.scenario}><td><Code>{x.scenario}</Code></td><td>{x.category}</td><td>{x.expected_status}</td><td>{x.actual_status}</td>
            <td><Badge v={x.correct ? t("ok") : t("wrong")} tone={x.correct ? "good" : "bad"} />{x.integrity_notes.length > 0 && <span className="muted"> <bdi>{x.integrity_notes.join("; ")}</bdi></span>}</td></tr>
        ))}</tbody></table>
    </>
  );
}
