/** Read-only first usable timing against the running local product service. */
import { execFileSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { chromium } from "@playwright/test";

const repository = path.resolve(import.meta.dirname, "..", "..", "..");
const python = path.join(repository, "desktop", ".venv", "Scripts", "python.exe");
const port = Number(process.env.MARKETLISTENER_PORT || "8765");
const trials = Number(process.argv[3] || process.env.MARKETLISTENER_TRIALS || "3");
const pageKind = process.argv[2] || process.env.MARKETLISTENER_PAGE || "market";
if (!Number.isInteger(port) || port < 1 || port > 65535 ||
    !Number.isInteger(trials) || trials < 1 || trials > 20 ||
    !["market", "futures"].includes(pageKind)) {
  throw new Error("invalid local port, trial count, or page");
}
const base = `http://127.0.0.1:${port}`;
const targetPath = pageKind === "futures" ? "/futures/" : "/market/all/";
const apiMarker = pageKind === "futures" ? "/api/futures/weekly-heat" : "/api/market/instruments";
const readySelector = pageKind === "futures" ? '[data-test="futures-weekly-heat"]' : ".row-main";
const bootstrap = `
import json, sys, requests
from market_monitor.local_session import CONTROL_HEADER, load_control_token
port = int(sys.argv[1]); base = f"http://127.0.0.1:{port}"
session = requests.Session(); session.trust_env = False
issued = session.post(base + "/api/local-session/issue", data=b"{}", timeout=5,
    headers={CONTROL_HEADER: load_control_token("product", port, create=False)})
issued.raise_for_status()
exchanged = session.post(base + "/api/local-session/bootstrap", timeout=5,
    headers={"Origin": base}, json={"bootstrap": issued.json()["bootstrap"]})
exchanged.raise_for_status()
name = f"ML_Product_Session_{port}"
print(json.dumps({"name": name, "value": session.cookies[name]}))
`;

function percentile(sorted, fraction) {
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const samples = [];
try {
  for (let trial = 0; trial < trials; trial += 1) {
    const cookie = JSON.parse(execFileSync(python, ["-c", bootstrap, String(port)], {
      cwd: repository, encoding: "utf8", windowsHide: true,
    }));
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.addCookies([{ ...cookie, url: base, httpOnly: true, sameSite: "Strict" }]);
    const page = await context.newPage();
    const errors = [];
    const apiEvents = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const started = performance.now();
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith("/api/")) apiEvents.push({
        path: url.pathname, status: response.status(), atMs: Number((performance.now() - started).toFixed(1)),
      });
    });
    try {
      const listResponse = page.waitForResponse((response) =>
        response.url().includes(apiMarker) && response.status() === 200,
      { timeout: 15000 });
      const response = await page.goto(`${base}${targetPath}`, { waitUntil: "domcontentloaded", timeout: 15000 });
      const navigationMs = Number((performance.now() - started).toFixed(1));
      await page.locator(readySelector).first().waitFor({ state: "visible", timeout: 15000 });
      const firstUsableMs = performance.now() - started;
      const list = await (await listResponse).json();
      let firstChartMs = null;
      if (pageKind === "market") {
        try {
          await page.locator(".boards .chart-root canvas").first().waitFor({ state: "visible", timeout: 10000 });
          firstChartMs = Number((performance.now() - started).toFixed(1));
        } catch {
          // Keep the first-row sample, and report chart availability separately.
        }
      }
      const resourceBytes = await page.evaluate(() =>
        performance.getEntriesByType("resource").reduce((sum, entry) => sum + (entry.transferSize || 0), 0));
      const slowResources = await page.evaluate(() => performance.getEntriesByType("resource")
        .filter((entry) => entry.duration >= 100)
        .sort((left, right) => right.duration - left.duration)
        .slice(0, 8)
        .map((entry) => ({ path: new URL(entry.name).pathname, durationMs: Number(entry.duration.toFixed(1)) })));
      samples.push({ trial: trial + 1, httpStatus: response?.status() ?? null,
        navigationMs, firstUsableMs: Number(firstUsableMs.toFixed(1)), firstChartMs, resourceBytes,
        apiEvents, slowResources,
        readyElements: await page.locator(readySelector).count(),
        snapshotStatus: list.snapshotStatus || null,
        snapshotVersion: list.snapshotVersion || null,
        queryVersion: list.queryVersion || null, errors });
    } catch (error) {
      samples.push({ trial: trial + 1, failed: String(error), errors });
    } finally {
      await context.close();
    }
  }
} finally {
  await browser.close();
}
const values = samples.filter((item) => Number.isFinite(item.firstUsableMs))
  .map((item) => item.firstUsableMs).sort((a, b) => a - b);
const report = { environment: { platform: os.platform(), arch: os.arch(),
  logicalCpus: os.cpus().length, totalMemoryBytes: os.totalmem(), viewport: "1440x900" },
  mode: "BROWSER_COLD_CONTEXT_WITH_READY_SERVICE", url: `${base}${targetPath}`,
  samples, p50Ms: values.length ? percentile(values, 0.5) : null,
  p95Ms: values.length ? percentile(values, 0.95) : null };
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (values.length !== trials || samples.some((item) => item.httpStatus !== 200 || item.errors.length)) {
  process.exitCode = 1;
}
