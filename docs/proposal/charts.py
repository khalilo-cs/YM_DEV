"""Inline SVG charts for the proposal (RTL-aware)."""

from __future__ import annotations

from model import MILESTONES, SCENARIOS, WORKSTREAMS, lean_totals, totals

COLORS = {"core": "#0f6e6e", "sec": "#b4442e", "ai": "#5b4bb3", "res": "#8a6d1d", "ui": "#2e6db4"}


def gantt() -> str:
    label_w, colw, rowh, top = 250, 34, 30, 34
    months = 12
    width = label_w + months * colw + 10
    height = top + len(WORKSTREAMS) * rowh + 40
    right = width - label_w  # month grid ends where the labels start (labels on the right: RTL)
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg" '
           'font-family="IBM Plex Sans Arabic">']
    for m in range(months):  # month 1 at the right edge of the grid
        x = right - (m + 1) * colw
        fill = "#f4f6f8" if m % 2 == 0 else "#ffffff"
        out.append(f'<rect x="{x}" y="{top - 4}" width="{colw}" height="{len(WORKSTREAMS) * rowh + 4}" '
                   f'fill="{fill}"/>')
        out.append(f'<text x="{x + colw / 2}" y="{top - 12}" font-size="11" text-anchor="middle" '
                   f'fill="#555">ش{m + 1}</text>')
    for i, (ar, _en, s, e, kind) in enumerate(WORKSTREAMS):
        y = top + i * rowh
        out.append(f'<text x="{width - 6}" y="{y + 18}" font-size="11.5" text-anchor="start" fill="#222">{ar}</text>')
        x1 = right - e * colw
        w = (e - s + 1) * colw
        out.append(f'<rect x="{x1 + 2}" y="{y + 5}" width="{w - 4}" height="{rowh - 11}" rx="5" '
                   f'fill="{COLORS[kind]}" opacity="0.88"/>')
    base = top + len(WORKSTREAMS) * rowh
    for m, label in MILESTONES:
        x = right - m * colw + 3
        out.append(f'<line x1="{x}" y1="{top - 4}" x2="{x}" y2="{base + 6}" stroke="#d4a017" stroke-width="2" '
                   'stroke-dasharray="4 3"/>')
        lx = min(max(x, 70), right - 70)
        out.append(f'<text x="{lx}" y="{base + 24}" font-size="11" text-anchor="middle" fill="#7a5c00" '
                   f'font-weight="600">{label}</text>')
    out.append("</svg>")
    return "".join(out)


def cost_chart() -> str:
    rows = [(SCENARIOS[k]["ar"], totals(k)["low"], totals(k)["high"]) for k in SCENARIOS]
    lt = lean_totals()
    rows.insert(0, ("المسار الأدنى (6 أشهر)", lt["low"], lt["high"]))
    maxv = max(h for _, _, h in rows) * 1.35  # leave room for the value label of the largest bar
    label_w, barw, rowh, top = 230, 440, 40, 14
    width, height = label_w + barw + 20, top + len(rows) * rowh + 30
    right = barw + 10
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg" '
           'font-family="IBM Plex Sans Arabic">']
    for t in range(0, int(maxv) + 1, 200000):
        x = right - t / maxv * barw
        out.append(f'<line x1="{x}" y1="{top - 4}" x2="{x}" y2="{top + len(rows) * rowh}" stroke="#e3e6ea"/>')
        out.append(f'<text x="{x}" y="{height - 8}" font-size="10" text-anchor="middle" fill="#666">'
                   f'{t // 1000}k $</text>')
    for i, (label, lo, hi) in enumerate(rows):
        y = top + i * rowh
        out.append(f'<text x="{width - 6}" y="{y + 22}" font-size="12" text-anchor="start" fill="#222">{label}</text>')
        x_lo, x_hi = right - lo / maxv * barw, right - hi / maxv * barw
        out.append(f'<rect x="{x_lo}" y="{y + 8}" width="{right - x_lo}" height="20" fill="#0f6e6e" opacity="0.85"/>')
        out.append(f'<rect x="{x_hi}" y="{y + 8}" width="{x_lo - x_hi}" height="20" fill="#0f6e6e" opacity="0.35"/>')
        out.append(f'<text x="{x_hi - 6}" y="{y + 23}" font-size="11" text-anchor="end" fill="#333" '
                   f'direction="ltr">{lo / 1000:,.0f}k – {hi / 1000:,.0f}k $</text>')
    out.append("</svg>")
    return "".join(out)


def racex_loop() -> str:
    import math
    steps = [("رصد", "Observe"), ("فهم وذاكرة", "Understand"), ("تشخيص", "Diagnose"), ("اقتراح خطط", "Plan"),
             ("محاكاة", "Simulate"), ("موافقة بشرية", "Authorize"), ("تنفيذ", "Execute"), ("تحقق مستقل", "Verify"),
             ("تراجع عند الفشل", "Rollback"), ("تعلّم بأدلة", "Learn")]
    cx, cy, r = 330, 215, 165
    out = ['<svg viewBox="0 0 660 430" width="100%" xmlns="http://www.w3.org/2000/svg" '
           'font-family="IBM Plex Sans Arabic">',
           '<defs><marker id="ar" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">'
           '<path d="M0,0 L8,4 L0,8 z" fill="#0f6e6e"/></marker></defs>',
           f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#cfe3e3" stroke-width="16"/>',
           f'<text x="{cx}" y="{cy - 8}" font-size="24" font-weight="700" text-anchor="middle" fill="#0f6e6e">RACE-X</text>',
           f'<text x="{cx}" y="{cy + 18}" font-size="12" text-anchor="middle" fill="#555">حلقة الهندسة والتعافي المستمرة</text>']
    n = len(steps)
    pts = []
    for i in range(n):
        a = -math.pi / 2 - 2 * math.pi * i / n  # counter-clockwise = right-to-left reading
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        k = 0.82
        out.append(f'<path d="M{x1 + (mx - x1) * 0.35:.1f},{y1 + (my - y1) * 0.35:.1f} '
                   f'Q{cx + (mx - cx) * 1.08:.1f},{cy + (my - cy) * 1.08:.1f} '
                   f'{x2 + (mx - x2) * 0.35:.1f},{y2 + (my - y2) * 0.35:.1f}" fill="none" stroke="#0f6e6e" '
                   f'stroke-width="1.6" marker-end="url(#ar)" opacity="{k}"/>')
    for i, (ar, en) in enumerate(steps):
        x, y = pts[i]
        color = "#b4442e" if en == "Rollback" else ("#d4a017" if en == "Authorize" else "#0f6e6e")
        out.append(f'<rect x="{x - 58}" y="{y - 20}" width="116" height="40" rx="9" fill="#fff" stroke="{color}" '
                   'stroke-width="2"/>')
        out.append(f'<text x="{x}" y="{y - 2}" font-size="12.5" font-weight="600" text-anchor="middle" '
                   f'fill="#222">{ar}</text>')
        out.append(f'<text x="{x}" y="{y + 13}" font-size="9.5" text-anchor="middle" fill="#777">{en}</text>')
    out.append("</svg>")
    return "".join(out)


def architecture() -> str:
    def box(x, y, w, h, title, sub, built=True, color="#0f6e6e"):
        dash = "" if built else ' stroke-dasharray="6 4"'
        fill = "#eef6f6" if built else "#fafafa"
        tcolor = "#163" if built else "#888"
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{color if built else "#aaa"}"'
                f' stroke-width="1.6"{dash}/>'
                f'<text x="{x + w / 2}" y="{y + h / 2 - 2}" font-size="12" font-weight="600" text-anchor="middle" '
                f'fill="{tcolor}">{title}</text>'
                f'<text x="{x + w / 2}" y="{y + h / 2 + 13}" font-size="9.5" text-anchor="middle" fill="#777">{sub}</text>')

    out = ['<svg viewBox="0 0 700 430" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="IBM Plex Sans Arabic">']
    layers = [
        ("الواجهات", 10, [("سطر الأوامر", "CLI", True), ("واجهة HTTP", "REST API /api/v1", True),
                         ("لوحة الويب", "React (مخطط)", False), ("سطح المكتب", "Tauri (مخطط)", False),
                         ("الجوال المرافق", "Android (مخطط)", False)]),
        ("التنسيق", 85, [("ORION", "مهام دائمة، استئناف، عقود", True), ("MOSAIC", "طبقة نماذج مستقلة عن المزود", True)]),
        ("المحركات", 160, [("التوأم الرقمي", "فحص + رسم التبعيات", True), ("التشخيص", "فرضيات غير مؤكدة", True),
                          ("التعافي", "محاكاة/تنفيذ/تراجع", True), ("المهارات", "تعلّم مُتحقق منه", True),
                          ("الذاكرة الحية", "مصدر + صلاحية", True), ("المخاطر", "(مخطط)", False)]),
        ("الحوكمة والأمن", 235, [("السياسات والأدوار", "+ عضوية المشاريع", True), ("الموافقات", "مربوطة، لمرة واحدة", True),
                                ("سجل التدقيق", "سلسلة HMAC", True), ("الأدلة", "مرتبطة بالسلسلة", True)]),
        ("التنفيذ المعزول", 310, [("واجهة العمليات", "rlimits + netns + uid", True), ("حاويات Docker", "بلا شبكة، قراءة فقط", True)]),
        ("التخزين", 385, [("SQLite محلي", "WAL، ترحيلات ذرية", True), ("PostgreSQL", "(مخطط لتعدد المستخدمين)", False),
                         ("المزامنة", "(مخطط)", False)]),
    ]
    for name, y, items in layers:
        out.append(f'<text x="694" y="{y + 27}" font-size="12" font-weight="700" text-anchor="start" fill="#444">{name}</text>')
        avail = 590
        n = len(items)
        gap = 8
        w = (avail - gap * (n - 1)) / n
        for i, (t, s, b) in enumerate(items):
            x = 590 - (i + 1) * w - i * gap
            out.append(box(x, y, w, 46, t, s, b))
    out.append("</svg>")
    return "".join(out)
