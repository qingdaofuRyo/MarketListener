// Capture official DCE futures terms from the already open LDPlayer browser.
// The current tab is restored before disconnecting from its WebView.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const pages = {
  A: 120, B: 121, BB: 126, BZ: 7000020, C: 118, CS: 119,
  EB: 137, EG: 136, FB: 125, I: 132, J: 130, JD: 127, JM: 131,
  L: 133, "L-F": 7000117, LG: 1023, LH: 129, M: 122, P: 124,
  PG: 138, PP: 135, "PP-F": 7000119, RR: 128, V: 134,
  "V-F": 7000118, Y: 2233,
};
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const adb = process.env.MARKETLISTENER_ADB || "adb";
const sockets = execFileSync(adb, ["shell", "cat", "/proc/net/unix"], { encoding: "utf8" });
const socket = sockets.match(/@webview_devtools_remote_\d+/)?.[0]?.slice(1);
if (!socket) throw new Error("LDPlayer browser WebView debug socket is unavailable");
execFileSync(adb, ["forward", "tcp:9222", `localabstract:${socket}`]);

const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
const page = browser.contexts()[0]?.pages()[0];
if (!page || !page.url().startsWith("http://www.dce.com.cn/")) {
  await browser.close();
  throw new Error("Keep the DCE browser page open in LDPlayer before capture");
}
const original = page.url();
const result = { passed: [], failed: [], restored: false };
try {
  for (const [code, channel] of Object.entries(pages)) {
    const url = `http://www.dce.com.cn/dce/channel/list/${channel}.html`;
    try {
      const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 18000 });
      if (response?.status() !== 200 || !page.url().startsWith(url)) {
        throw new Error(`HTTP ${response?.status() || "unknown"}`);
      }
      const bytes = await response.body();
      const text = bytes.toString("utf8");
      if (!["交易品种", "交易单位", "交易代码", "上市交易所"].every(label => text.includes(label))) {
        throw new Error("Contract terms are absent from official response");
      }
      const hash = createHash("sha256").update(bytes).digest("hex");
      const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "DCE", code);
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `${hash}.html`), bytes);
      writeFileSync(resolve(directory, `${hash}.json`), JSON.stringify({
        productKey: `DCE.${code}`, sourceUrl: url, payloadSha256: hash,
        retrievedAt: new Date().toISOString(), adapter: "LDPLAYER_WEBVIEW_CDP",
      }, null, 2));
      result.passed.push(code);
    } catch (error) {
      result.failed.push({ code, error: String(error).slice(0, 180) });
    }
  }
} finally {
  try {
    await page.goto(original, { waitUntil: "commit", timeout: 18000 });
    result.restored = page.url() === original;
  } catch (error) {
    result.restoreError = String(error).slice(0, 180);
  }
  await browser.close();
  execFileSync(adb, ["forward", "--remove", "tcp:9222"]);
}
console.log(JSON.stringify(result));
if (result.failed.length || !result.restored) process.exitCode = 2;
