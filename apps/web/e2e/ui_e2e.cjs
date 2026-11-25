// Real browser against a real `yemendev serve`: every value the UI shows is checked against the API.
// Run through scripts/ui_e2e.py, which prepares a fresh data dir, two human tokens, a broken sample
// project and a human-authored plan, then starts the server.
const { chromium } = require("playwright");
const fs = require("fs");
const [, , base, aliceTok, bobTok, pid, shots, planFile] = process.argv;
const log = (...a) => console.log(...a);
const api = async (tok, path, opts = {}) => {
  const r = await fetch(base + "/api/v1" + path, { ...opts, headers: { Authorization: "Bearer " + tok, "Content-Type": "application/json" } });
  return r.json();
};
const fails = [];
const check = (cond, msg) => { if (!cond) fails.push(msg); log((cond ? "PASS " : "FAIL ") + msg); };

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (m.type() === "error" || /Content Security Policy/i.test(m.text())) problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  const external = [];
  page.on("request", (r) => { if (!r.url().startsWith(base)) external.push(r.url()); });

  // 1. login form in Arabic, RTL by default
  await page.goto(base + "/");
  await page.waitForSelector("form.login");
  check(await page.evaluate(() => document.documentElement.dir) === "rtl", "default language is Arabic (dir=rtl)");
  await page.fill("input[type=password]", "ydv_not_a_real_token");
  await page.click("form.login button.primary");
  await page.waitForSelector("form.login .error");
  check((await page.textContent("form.login .error")).includes("غير صالح"), "invalid token shows Arabic error");
  await page.screenshot({ path: shots + "/01-login-ar.png" });

  await page.fill("input[type=password]", aliceTok);
  await page.click("form.login button.primary");
  await page.waitForSelector(".stats .stat");
  const st = await api(aliceTok, "/system/status");
  const statText = await page.textContent(".stats");
  const nf = new Intl.NumberFormat("ar");
  check(statText.includes(nf.format(st.counts.projects)) && statText.includes(nf.format(st.counts.evidence_records)), `dashboard numbers match API (projects=${st.counts.projects}, evidence=${st.counts.evidence_records})`);
  check((await page.textContent("main")).includes(st.sandbox.isolation_effective), `dashboard shows effective sandbox backend '${st.sandbox.isolation_effective}'`);
  await page.screenshot({ path: shots + "/02-dashboard-ar.png", fullPage: true });

  // 2. language switch
  await page.click("header button:has-text('English')");
  check(await page.evaluate(() => [document.documentElement.dir, document.documentElement.lang].join()) === "ltr,en", "switching to English sets dir=ltr lang=en");
  await page.screenshot({ path: shots + "/03-dashboard-en.png", fullPage: true });
  await page.click("header button:has-text('العربية')");
  check(await page.evaluate(() => document.documentElement.dir) === "rtl", "switching back restores RTL");

  // 3. project: scan, graph
  await page.goto(base + "/app/#/projects/" + pid);
  await page.waitForSelector(".tabs");
  await page.click("button.primary:has-text('فحص')");
  await page.waitForSelector(".stats .stat");
  const graph = await api(aliceTok, `/projects/${pid}/graph`);
  const modulesShown = await page.locator(".stat", { hasText: "الوحدات" }).locator(".stat-v").textContent();
  check(modulesShown === nf.format(graph.nodes.length), `scan shows ${modulesShown} modules = API graph ${graph.nodes.length}`);
  await page.screenshot({ path: shots + "/04-project-scan-ar.png", fullPage: true });
  await page.click("button[role=tab]:has-text('رسم التبعيات')");
  await page.waitForSelector(".graph svg .node");
  const nodes = await page.locator(".graph svg .node").count();
  check(nodes === graph.nodes.length, `graph renders ${nodes} nodes = API ${graph.nodes.length}`);
  await page.screenshot({ path: shots + "/05-graph-ar.png", fullPage: true });

  // 3b. every other project tab renders real data without an error box
  const tabs = { "المخاطر": `/projects/${pid}/risks`, "فحص المناعة": `/projects/${pid}/immune`, "دَين الاستمرارية": `/projects/${pid}/continuity-debt`, "الأعضاء": `/projects/${pid}/members` };
  for (const [label, path] of Object.entries(tabs)) {
    await page.click(`button[role=tab]:has-text('${label}')`);
    await page.waitForFunction(() => !document.querySelector("main [role=status]"));
    await page.waitForTimeout(150);
    const res = await fetch(base + "/api/v1" + path, { headers: { Authorization: "Bearer " + aliceTok } });
    check(res.ok && await page.locator("main .error").count() === 0, `tab '${label}' renders (API ${res.status})`);
  }
  const debt = await api(aliceTok, `/projects/${pid}/continuity-debt`);
  await page.click("button[role=tab]:has-text('دَين الاستمرارية')");
  await page.waitForSelector("main .notice");
  check((await page.textContent("main .notice")).includes("تجريبي") && (await page.locator("main table tbody tr").count()) === debt.components.length, `continuity debt marked experimental, ${debt.components.length} components`);
  await page.screenshot({ path: shots + "/05b-debt-ar.png", fullPage: true });
  await page.click("button[role=tab]:has-text('ماذا لو')");
  await page.waitForSelector("main select");
  await page.selectOption("main select", graph.nodes[0]);
  await page.click("main button:has-text('تشغيل السيناريو')");
  await page.click("dialog[open] button.primary");
  await page.waitForSelector("main .card .badge", { timeout: 300000 });
  const cf = await page.locator("main .card .badge").getAttribute("class");
  check(await page.locator("main .error").count() === 0 && /good|bad/.test(cf), `what-if scenario on '${graph.nodes[0]}' ran in the sandbox and rendered a coloured verdict (${cf})`);
  await page.screenshot({ path: shots + "/05c-whatif-ar.png", fullPage: true });
  for (const [route, needle] of [["#/agents", "planning"], ["#/skills", null], ["#/continuity", "signed file bundles"], ["#/research", "RESEARCH_PLAN"]]) {
    await page.goto(base + "/app/" + route);
    await page.waitForSelector("main h1");
    await page.waitForFunction(() => !document.querySelector("main [role=status]"));
    await page.waitForTimeout(200);
    const txt = await page.textContent("main");
    check(await page.locator("main .error").count() === 0 && (!needle || txt.includes(needle)), `page ${route} renders${needle ? ` (contains '${needle}')` : ""}`);
  }
  await page.goto(base + "/app/#/agents");
  await page.waitForSelector("main table tbody tr");
  const agents = await api(aliceTok, "/agents");
  check(await page.locator("main table").first().locator("tbody tr").count() === agents.length, `AI control lists ${agents.length} agents = API`);
  await page.screenshot({ path: shots + "/05d-agents-ar.png", fullPage: true });
  await page.goto(base + "/app/#/projects/" + pid);
  await page.waitForSelector(".tabs");

  // 4a. start repair without a plan: honest no_action with the recorded reason
  await page.click("button[role=tab]:has-text('نظرة عامة')");
  await page.click("button:has-text('بدء مهمة إصلاح')");
  check(await page.locator("dialog[open]").count() === 1, "sensitive action asks for confirmation first");
  await page.click("dialog[open] button.primary");
  await page.waitForURL(/#\/tasks\/tsk_/, { timeout: 180000 });
  const nid = page.url().split("/").pop();
  await page.waitForSelector("main .notice");
  const nTask = await api(aliceTok, "/tasks/" + nid);
  const nStep = await api(aliceTok, `/tasks/${nid}/steps/${nTask.current_step || "propose"}`);
  check(nTask.status === "no_action" && (await page.textContent("main .notice")).includes(nStep.output.reason), `no-plan task shows recorded stop reason: "${nStep.output.reason}"`);
  // it ran in the background: the page followed it by itself and shows the queue and the trace
  const nTrace = await api(aliceTok, `/tasks/${nid}/trace`);
  await page.waitForFunction((n) => document.querySelectorAll("main table.timeline-table tbody tr").length >= n, Math.min(nTrace.events.length, 150), { timeout: 10000 }).catch(() => {});
  const queueCard = await page.locator("main .card", { hasText: "طابور الخلفية" }).textContent();
  check(nTrace.queue.state === "done" && queueCard.includes("done") && nTrace.events.some((e) => e.kind === "queue.run_finished"),
    `no-plan task ran in the background worker (queue ${nTrace.queue.state})`);
  check(await page.locator("main table.timeline-table tbody tr").count() === Math.min(nTrace.events.length, 150),
    `timeline shows the ${nTrace.events.length} trace events`);
  await page.screenshot({ path: shots + "/06-task-no-action-ar.png", fullPage: true });

  // 4b. a human-authored plan (created by alice through the API), advanced from the UI
  const plan = JSON.parse(fs.readFileSync(planFile, "utf8"));
  const created = await api(aliceTok, "/tasks", { method: "POST", body: JSON.stringify({ project_id: pid, plan }) });
  const tid = created.id;
  await page.goto(base + "/app/#/tasks/" + tid);
  await page.waitForSelector("main h1 .badge");
  await page.click("main button.primary:has-text('متابعة')");
  await page.click("dialog[open] button.primary");
  await page.waitForFunction(() => /waiting_approval|no_action|failed/.test(document.querySelector("main h1")?.textContent || ""), null, { timeout: 180000 });
  const task = await api(aliceTok, "/tasks/" + tid);
  check(task.status === "waiting_approval", `task ${tid} is waiting_approval (API: ${task.status})`);
  check((await page.textContent("main h1")).includes(task.status), "task page shows API status");
  const evCount = (await api(aliceTok, `/evidence?task_id=${tid}&limit=500`)).length;
  await page.waitForFunction((n) => document.querySelectorAll("main table.evidence-table tbody tr").length === n, evCount, { timeout: 10000 }).catch(() => {});
  const evRows = await page.locator("main table.evidence-table tbody tr").count();
  check(evRows === evCount, `evidence rows ${evRows} = API ${evCount}`);
  await page.screenshot({ path: shots + "/07-task-waiting-ar.png", fullPage: true });

  // 5. alice authored the plan: the UI must surface the server's refusal of self-approval
  await page.goto(base + "/app/#/approvals");
  await page.waitForSelector("main .card pre.diff");
  await page.click("main .card button.primary:has-text('موافقة')");
  await page.click("dialog[open] button.primary");
  await page.waitForSelector("dialog[open] .error");
  const selfErr = await page.textContent("dialog[open] .error");
  check(/403/.test(selfErr), "self-approval refused and shown in the dialog: " + selfErr.trim());
  await page.screenshot({ path: shots + "/08-self-approval-refused-ar.png" });
  await page.click("dialog[open] button.plain");
  check((await api(aliceTok, "/approvals?status=pending")).length === 1, "approval still pending after refused self-approval");

  await page.click("header button:has-text('تسجيل الخروج')");
  await page.waitForSelector("form.login");
  await page.fill("input[type=password]", bobTok);
  await page.click("form.login button.primary");
  await page.waitForSelector("main .card pre.diff");  // returns to the approvals page
  check((await page.textContent("main .card pre.diff")).includes("add_numbers"), "approval shows the exact change");
  await page.screenshot({ path: shots + "/09-approval-bob-ar.png", fullPage: true });
  await page.click("main .card button.primary:has-text('موافقة')");
  await page.click("dialog[open] button.primary");
  await page.waitForSelector("main p.empty");
  check((await api(bobTok, "/approvals?status=pending")).length === 0, "bob's approval decided through the UI");

  // 6. advance -> applied & verified
  await page.goto(base + "/app/#/tasks/" + tid);
  await page.waitForSelector("main h1 .badge");
  await page.click("main button.primary:has-text('متابعة')");
  await page.click("dialog[open] button.primary");
  await page.waitForFunction(() => /succeeded|rolled_back|failed|no_action/.test(document.querySelector("main h1")?.textContent || ""), null, { timeout: 180000 });
  const done = await api(bobTok, "/tasks/" + tid);
  check(done.status === "succeeded", `task finished: ${done.status}`);
  await page.screenshot({ path: shots + "/10-task-succeeded-ar.png", fullPage: true });

  // 7. security page: integrity check through UI
  await page.goto(base + "/app/#/security");
  await page.click("main button.primary");
  await page.waitForSelector("main p.ok, main div.error");
  check(await page.locator("main p.ok").count() === 1, "integrity verification reports intact chain");
  await page.screenshot({ path: shots + "/11-security-ar.png", fullPage: true });

  // 8. mobile width: no horizontal page scroll
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base + "/app/#/");
  await page.waitForSelector(".stats .stat");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 0, `no horizontal scroll at 390px (overflow=${overflow})`);
  // regression: ids used to wrap one character per line in narrow table cells
  const idHeights = await page.locator("code.id").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  check(idHeights.length > 0 && idHeights.every((h) => h < 30), `task ids stay on one line on a phone (heights ${idHeights.map(Math.round)})`);
  await page.screenshot({ path: shots + "/12-mobile-ar.png", fullPage: true });

  check(external.length === 0, "no requests leave the origin " + (external.length ? external.join(",") : ""));
  // Exactly the two HTTP errors this script provokes on purpose (bad token -> 401, self-approval -> 403);
  // anything else (another failed request, a JS error, a CSP violation) fails the run.
  const expected = [/status of 401/, /status of 403/];
  const unexpected = problems.filter((m) => { const i = expected.findIndex((re) => re.test(m)); if (i < 0) return true; expected.splice(i, 1); return false; });
  check(unexpected.length === 0 && expected.length === 0, "console: only the two provoked HTTP errors, no JS errors or CSP violations " + unexpected.join(" | "));
  await browser.close();
  log(fails.length ? `\n${fails.length} FAILED` : "\nALL PASSED");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
