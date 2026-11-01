// Observe only the user's already open CZCE product pages in LDPlayer WebView.
// No navigation or clicks are issued. Captures rendered DOM, not HTTP bytes.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const codes = {
  "001": "WH", "002": "PM", "003": "CF", "004": "SR", "005": "TA",
  "006": "OI", "007": "RI", "008": "MA", "009": "FG", "010": "RS",
  "011": "RM", "012": "ZC", "013": "JR", "014": "LR", "017": "SF",
  "019": "CY", "020": "SM", "021": "AP", "022": "CJ", "023": "UR",
  "024": "SA", "025": "PF", "026": "PK", "027": "PX", "028": "SH",
  "029": "PR", "030": "PL",
};
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const ldconsole = process.env.MARKETLISTENER_LDCONSOLE || "C:\\leidian\\LDPlayer14\\ldconsole.exe";
const duration = Math.max(10, Math.min(Number(process.env.CZCE_WATCH_SECONDS || 900), 3600)) * 1000;
const unixSockets = execFileSync(ldconsole, ["adb", "--index", "0", "--command", "shell cat /proc/net/unix"], { encoding: "utf8" });
const socket = unixSockets.match(/@webview_devtools_remote_\d+/)?.[0]?.slice(1);
if (!socket) throw new Error("LDPlayer browser WebView debug socket is unavailable");
execFileSync(ldconsole, ["adb", "--index", "0", "--command", `forward tcp:9223 localabstract:${socket}`]);
const browser = await chromium.connectOverCDP("http://127.0.0.1:9223");
const saved = new Set();
const started = Date.now();
try {
  while (Date.now() - started < duration && saved.size < 27) {
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        const url = page.url();
        const match = url.match(/^https:\/\/www\.czce\.com\.cn\/cn\/sspz\/[^/]+\/H077002(\d{3})index_1\.htm$/);
        const code = match && codes[match[1]];
        if (!code || saved.has(code)) continue;
        try {
          const nav = await page.evaluate(() => performance.getEntriesByType("navigation")
            .map(entry => ({ status: entry.responseStatus, transferSize: entry.transferSize }))[0] || null);
          if (nav?.status !== 200) continue;
          const table = await page.locator("table").evaluateAll(items => items
            .map(item => item.innerText)
            .find(text => text.includes("交易品种") && text.includes("交易代码")) || "");
          if (!["交易单位", "报价单位", "最小变动价位", "最后交易日", "上市交易所", code]
            .every(label => table.includes(label))) continue;
          const bytes = Buffer.from(await page.content(), "utf8");
          const hash = createHash("sha256").update(bytes).digest("hex");
          const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", code);
          mkdirSync(directory, { recursive: true });
          writeFileSync(resolve(directory, `${hash}.html`), bytes);
          writeFileSync(resolve(directory, `${hash}.json`), JSON.stringify({
            productKey: `CZCE.${code}`, sourceUrl: url, payloadSha256: hash,
            retrievedAt: new Date().toISOString(), adapter: "LDPLAYER_WEBVIEW_DOM",
            captureKind: "RENDERED_DOM_SNAPSHOT",
            navigationResponseStatus: nav.status, navigationTransferSize: nav.transferSize,
          }, null, 2));
          saved.add(code);
          process.stdout.write(`${JSON.stringify({ code, hash, captured: saved.size })}\n`);
        } catch {
          // A navigation may still be in progress. Retry after it settles.
        }
      }
    }
    await new Promise(resolveTimeout => setTimeout(resolveTimeout, 750));
  }
} finally {
  process.stdout.write(`${JSON.stringify({ finished: true, codes: [...saved] })}\n`);
  await browser.close();
  execFileSync(ldconsole, ["adb", "--index", "0", "--command", "forward --remove tcp:9223"]);
}
