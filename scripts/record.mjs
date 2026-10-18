// node scripts/record.mjs   (needs: npm i --no-save playwright && npx playwright install chromium)
// Drives a headed-quality Chromium through scripts/capture.js against PORTDB_URL (default localhost:5173).
import { readFileSync } from "node:fs";
import { chromium } from "playwright";

// capture.js is our own committed file, written as a bare function so Playwright MCP can run it too
const capture = eval(readFileSync(new URL("./capture.js", import.meta.url), "utf8"));
const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage();
console.log(await capture(page));
await browser.close();
