// Render the proposal HTML to PDF with Chromium (Playwright). Usage: node print_pdf.mjs in.html out.pdf
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
await page.pdf({
  path: output, format: "A4", printBackground: true, preferCSSPageSize: true,
  displayHeaderFooter: true, headerTemplate: "<span></span>",
  footerTemplate: '<div style="font-size:8px;width:100%;text-align:center;color:#8a949c;font-family:sans-serif;">' +
    'YEMENDEV AI — Project Proposal v1.0 · <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
});
await browser.close();
