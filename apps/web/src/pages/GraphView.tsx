import { Async, Code, useApi, useI18n } from "../ui";

type Report = { nodes: string[]; edges: [string, string][]; cycles: string[][]; critical_modules: string[]; note: string };

/** Layered drawing of the real import graph: a module is placed one layer after everything it imports. */
export default function GraphView({ id }: { id: string }) {
  const { t } = useI18n();
  const q = useApi<Report>(`/projects/${id}/graph`);
  return (
    <Async q={q}>{(g) => {
      if (g.nodes.length === 0) return <p className="empty">{t("no_scan")}</p>;
      const deps = new Map<string, string[]>(g.nodes.map((n) => [n, []]));
      g.edges.forEach(([s, d]) => deps.get(s)?.push(d));
      const layer = new Map<string, number>();
      const depth = (n: string, seen: Set<string>): number => {
        if (layer.has(n)) return layer.get(n)!;
        if (seen.has(n)) return 0; // cycle: stop
        seen.add(n);
        const v = Math.max(0, ...(deps.get(n) ?? []).map((d) => depth(d, seen) + 1));
        layer.set(n, v);
        return v;
      };
      g.nodes.forEach((n) => depth(n, new Set()));
      const cols = new Map<number, string[]>();
      g.nodes.forEach((n) => { const l = layer.get(n)!; cols.set(l, [...(cols.get(l) ?? []), n]); });
      const W = 210, H = 34, maxRows = Math.max(...[...cols.values()].map((c) => c.length));
      const pos = new Map<string, { x: number; y: number }>();
      [...cols.entries()].forEach(([l, ns]) => ns.forEach((n, i) => pos.set(n, { x: 10 + l * W, y: 10 + i * H })));
      const width = 20 + cols.size * W, height = 20 + maxRows * H;
      const critical = new Set(g.critical_modules);
      const inCycle = new Set(g.cycles.flat());
      return (
        <div>
          <p className="muted"><bdi>{g.note}</bdi></p>
          <div className="graph" dir="ltr">
            <svg width={width} height={height} role="img" aria-label={t("graph")}>
              {g.edges.map(([s, d]) => { const a = pos.get(s)!, b = pos.get(d)!; return <line key={s + d} x1={a.x} y1={a.y + 12} x2={b.x + 170} y2={b.y + 12} className="edge" />; })}
              {g.nodes.map((n) => { const p = pos.get(n)!; return (
                <g key={n} transform={`translate(${p.x},${p.y})`}>
                  <rect width={170} height={24} rx={5} className={inCycle.has(n) ? "node cyc" : critical.has(n) ? "node crit" : "node"} />
                  <text x={8} y={16}>{n.length > 26 ? n.slice(0, 25) + "…" : n}</text><title>{n}</title>
                </g>); })}
            </svg>
          </div>
          <p>{t("critical_modules")}: {g.critical_modules.length === 0 ? "—" : g.critical_modules.map((m) => <Code key={m}>{m} </Code>)}</p>
          <p>{t("cycles")}: {g.cycles.length === 0 ? "—" : g.cycles.map((c, i) => <Code key={i}>{c.join(" → ")} </Code>)}</p>
        </div>
      );
    }}</Async>
  );
}
