// Render the two-page brief to PDF with Chromium (Playwright). Pages carry their own footer.
// Usage: node print_brief.mjs in.html out.pdf
import { createRequire } from "module";
import { pathToFileURL } from "url";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const [, , input, output] = process.argv;
const browser = await chromium.launch();
const page = await browser.newPage();
await page.goto(pathToFileURL(input).href, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
const missing = await page.evaluate(() => [...document.fonts].filter(f => f.status !== "loaded").length);
if (missing) console.error(`warning: ${missing} font face(s) not loaded`);
// Fail loudly if any page's content is taller than the A4 page box (it would be clipped).
const overflow = await page.evaluate(() => [...document.querySelectorAll(".page")].map((p, i) => {
  const last = [...p.children].filter(c => !c.classList.contains("foot")).pop();
  const foot = p.querySelector(".foot");
  return last && foot && last.getBoundingClientRect().bottom > foot.getBoundingClientRect().top - 2 ? i + 1 : 0;
}).filter(Boolean));
if (overflow.length) { console.error(`content overlaps the footer on page(s) ${overflow}`); process.exitCode = 3; }
await page.pdf({ path: output, format: "A4", printBackground: true, preferCSSPageSize: true });
await browser.close();
