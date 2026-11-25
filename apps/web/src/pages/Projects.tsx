import { useState } from "react";
import { api } from "../api";
import { Async, Badge, Code, ConfirmButton, ErrorBox, Stat, useApi, useI18n } from "../ui";
import GraphView from "./GraphView";

type Project = { id: string; name: string; root_path: string; created_at: string };

export function ProjectList() {
  const { t, fmtDate } = useI18n();
  const q = useApi<Project[]>("/projects");
  return (
    <section>
      <h1>{t("nav_projects")}</h1>
      <Async q={q} empty={(d) => d.length === 0}>{(d) => (
        <table><thead><tr><th>{t("name")}</th><th>{t("path")}</th><th>{t("created")}</th></tr></thead>
          <tbody>{d.map((p) => (
            <tr key={p.id}><td><a href={`#/projects/${p.id}`}><bdi>{p.name}</bdi></a></td><td><Code>{p.root_path}</Code></td><td>{fmtDate(p.created_at)}</td></tr>
          ))}</tbody></table>
      )}</Async>
    </section>
  );
}

const TABS = ["overview", "graph", "risks", "immune", "debt", "whatif", "members"] as const;
type Tab = (typeof TABS)[number];

export function ProjectDetail({ id }: { id: string }) {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("overview");
  const p = useApi<Project>(`/projects/${id}`);
  return (
    <section>
      <Async q={p}>{(proj) => <h1><bdi>{proj.name}</bdi> <small className="muted"><Code>{proj.id}</Code></small></h1>}</Async>
      <div className="tabs" role="tablist">
        {TABS.map((k) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}>{t(k === "whatif" ? "whatif" : k === "graph" ? "graph" : k)}</button>)}
      </div>
      {tab === "overview" && <Overview id={id} />}
      {tab === "graph" && <GraphView id={id} />}
      {tab === "risks" && <Risks id={id} />}
      {tab === "immune" && <Immune id={id} />}
      {tab === "debt" && <Debt id={id} />}
      {tab === "whatif" && <WhatIf id={id} />}
      {tab === "members" && <Members id={id} />}
    </section>
  );
}

type ScanResult = { files: number; modules: number; languages: Record<string, number>; findings: { kind: string; severity: string; message: string }[] };

function Overview({ id }: { id: string }) {
  const { t, fmtNum } = useI18n();
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [err, setErr] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const doScan = async () => {
    setBusy(true); setErr(null);
    try { setScan(await api<ScanResult>(`/projects/${id}/scan`, { method: "POST" })); } catch (e) { setErr(e as Error); } finally { setBusy(false); }
  };
  return (
    <div>
      <div className="row">
        <button className="primary" onClick={doScan} disabled={busy}>{busy ? t("scanning") : t("scan")}</button>
        <ConfirmButton label={t("start_repair")} question={t("start_repair_q")} onConfirm={async () => {
          const task = await api<{ id: string }>("/tasks", { method: "POST", body: { project_id: id, queue: {} } });
          location.hash = `#/tasks/${task.id}`;
        }} />
      </div>
      {err && <ErrorBox error={err} />}
      {scan && (
        <>
          <div className="stats">
            <Stat label={t("files")} value={fmtNum(scan.files)} />
            <Stat label={t("modules")} value={fmtNum(scan.modules)} />
            <Stat label={t("languages")} value={Object.entries(scan.languages).map(([k, v]) => `${k}: ${v}`).join(" · ")} />
          </div>
          <h3>{t("findings")}</h3>
          {scan.findings.length === 0 ? <p className="empty">{t("empty")}</p> : (
            <table><tbody>{scan.findings.map((f, i) => <tr key={i}><td><Badge v={f.severity} /></td><td><bdi>{f.kind}</bdi></td><td><bdi>{f.message}</bdi></td></tr>)}</tbody></table>
          )}
        </>
      )}
    </div>
  );
}

type Prediction = { subject: string; event: string; probability: number | null; horizon: string; kind: string; limitations: string[]; recommended_response: string };
type RiskReport = {
  predictions: Prediction[]; anomalies: { metric: string; explanation: string }[];
  risk_scores: { subject: string; kind: string; score: number; explanation: string }[];
  dependency_risks: { subject: string; severity: string; reason: string }[];
  insufficient_evidence: { analysis: string; needed: string; available: string }[]; limitations: string[];
};

function Risks({ id }: { id: string }) {
  const { t, fmtNum } = useI18n();
  const q = useApi<RiskReport>(`/projects/${id}/risks`);
  return (
    <Async q={q}>{(r) => (
      <div>
        <h3>{t("predictions")}</h3>
        {r.predictions.length === 0 ? <p className="empty">{t("empty")}</p> : (
          <table><thead><tr><th>{t("subject")}</th><th>{t("category")}</th><th>{t("probability")}</th><th>{t("horizon")}</th><th>{t("recommended")}</th></tr></thead>
            <tbody>{r.predictions.map((p) => <tr key={p.kind + p.subject + p.event}><td><Code>{p.subject}</Code></td><td><Badge v={p.kind} /></td><td>{p.probability === null ? "—" : fmtNum(p.probability)}</td><td><bdi>{p.horizon}</bdi></td><td><bdi>{p.recommended_response}</bdi></td></tr>)}</tbody></table>
        )}
        <h3>{t("risk_scores")}</h3>
        {r.risk_scores.length === 0 ? <p className="empty">{t("empty")}</p> : (
          <table><tbody>{r.risk_scores.map((s) => <tr key={s.kind + s.subject}><td><Code>{s.subject}</Code></td><td>{fmtNum(s.score)}</td><td><bdi>{s.explanation}</bdi></td></tr>)}</tbody></table>
        )}
        <h3>{t("anomalies")}</h3>
        {r.anomalies.length === 0 ? <p className="empty">{t("empty")}</p> : <ul>{r.anomalies.map((a, i) => <li key={i}><bdi>{a.explanation}</bdi></li>)}</ul>}
        <h3>{t("dependency_risks")}</h3>
        {r.dependency_risks.length === 0 ? <p className="empty">{t("empty")}</p> : <ul>{r.dependency_risks.map((d, i) => <li key={i}><Badge v={d.severity} /> <Code>{d.subject}</Code> <bdi>{d.reason}</bdi></li>)}</ul>}
        {r.insufficient_evidence.length > 0 && (<>
          <h3>{t("insufficient")}</h3>
          <ul className="muted">{r.insufficient_evidence.map((x) => <li key={x.analysis}><bdi>{x.analysis}: {x.available} / {x.needed}</bdi></li>)}</ul>
        </>)}
        <h3>{t("limitations")}</h3><ul className="muted">{r.limitations.map((l) => <li key={l}><bdi>{l}</bdi></li>)}</ul>
      </div>
    )}</Async>
  );
}

type ImmuneReport = { findings: { rule: string; category: string; severity: string; subject: string; detail: string; recommended_action: string }[]; summary: Record<string, number>; limitations: string[] };

function Immune({ id }: { id: string }) {
  const { t } = useI18n();
  const q = useApi<ImmuneReport>(`/projects/${id}/immune`);
  return (
    <Async q={q}>{(r) => (
      <div>
        <div className="stats"><Stat label={t("security_findings")} value={r.summary.security} /><Stat label={t("operational_findings")} value={r.summary.operational} /></div>
        {r.findings.length === 0 ? <p className="empty">{t("empty")}</p> : (
          <table><thead><tr><th>{t("category")}</th><th>{t("severity")}</th><th>{t("subject")}</th><th>{t("details")}</th><th>{t("recommended")}</th></tr></thead>
            <tbody>{r.findings.map((f, i) => <tr key={i}><td><Badge v={f.category} /></td><td><Badge v={f.severity} /></td><td><Code>{f.subject}</Code></td><td><bdi>{f.detail}</bdi></td><td><bdi>{f.recommended_action}</bdi></td></tr>)}</tbody></table>
        )}
        <ul className="muted">{r.limitations.map((l) => <li key={l}><bdi>{l}</bdi></li>)}</ul>
      </div>
    )}</Async>
  );
}

type DebtReport = { status: string; score: number | null; components: { name: string; value: number | null; evidence: string }[] };

function Debt({ id }: { id: string }) {
  const { t, fmtNum } = useI18n();
  const q = useApi<DebtReport>(`/projects/${id}/continuity-debt`);
  return (
    <Async q={q}>{(d) => (
      <div>
        <p className="notice">{t("experimental")}</p>
        <Stat label={t("score")} value={d.score === null ? "—" : fmtNum(d.score)} />
        <table><tbody>{d.components.map((c) => <tr key={c.name}><td><Code>{c.name}</Code></td><td>{c.value === null ? <em>{t("not_measurable")}</em> : fmtNum(c.value)}</td><td><bdi>{c.evidence}</bdi></td></tr>)}</tbody></table>
      </div>
    )}</Async>
  );
}

type CF = { detected: boolean; blind_spot: boolean; newly_failing: string[]; static_precision: number | null; static_recall: number | null; notes: string[] };

function WhatIf({ id }: { id: string }) {
  const { t, fmtNum } = useI18n();
  const graph = useApi<{ nodes: string[] }>(`/projects/${id}/graph`);
  const [mod, setMod] = useState("");
  const [res, setRes] = useState<CF | null>(null);
  return (
    <Async q={graph}>{(g) => (
      <div>
        <label>{t("module_fault")}{" "}
          <select value={mod} onChange={(e) => setMod(e.target.value)}>
            <option value="">—</option>{g.nodes.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>{" "}
        <ConfirmButton label={t("run_scenario")} question={t("run_scenario_q")} disabled={!mod} onConfirm={async () => {
          setRes(await api<CF>(`/projects/${id}/counterfactual`, { method: "POST", body: { name: `ui:${mod}`, faults: [{ kind: "module_unavailable", module: mod }] } }));
        }} />
        {res && (
          <div className="card">
            <p>{res.blind_spot ? <Badge v={t("blind_spot")} tone="bad" /> : <Badge v={t("detected")} tone="good" />}</p>
            <p>{t("newly_failing")}: {res.newly_failing.length === 0 ? "—" : res.newly_failing.map((x) => <Code key={x}>{x} </Code>)}</p>
            <p>{t("precision")}: {res.static_precision === null ? "—" : fmtNum(res.static_precision)} · {t("recall")}: {res.static_recall === null ? "—" : fmtNum(res.static_recall)}</p>
            <ul className="muted">{res.notes.map((n) => <li key={n}><bdi>{n}</bdi></li>)}</ul>
          </div>
        )}
      </div>
    )}</Async>
  );
}

function Members({ id }: { id: string }) {
  const { t, fmtDate } = useI18n();
  const q = useApi<{ actor_ref: string; role: string; granted_at: string }[]>(`/projects/${id}/members`);
  return (
    <Async q={q} empty={(d) => d.length === 0}>{(d) => (
      <table><thead><tr><th>{t("name")}</th><th>{t("role")}</th><th>{t("created")}</th></tr></thead>
        <tbody>{d.map((m) => <tr key={m.actor_ref}><td><Code>{m.actor_ref}</Code></td><td><Badge v={m.role} /></td><td>{fmtDate(m.granted_at)}</td></tr>)}</tbody></table>
    )}</Async>
  );
}
