// Capture CZCE futures terms from the user's already open BlueStacks Chrome.
// Product pages are navigated within that official site, then the original tab is restored.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const channelCodes = {
  "001": "WH", "002": "PM", "003": "CF", "004": "SR", "005": "TA",
  "006": "OI", "007": "RI", "008": "MA", "009": "FG", "010": "RS",
  "011": "RM", "012": "ZC", "013": "JR", "014": "LR", "017": "SF",
  "019": "CY", "020": "SM", "021": "AP", "022": "CJ", "023": "UR",
  "024": "SA", "025": "PF", "026": "PK", "027": "PX", "028": "SH",
  "029": "PR", "030": "PL",
};
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const adb = process.env.MARKETLISTENER_ADB || "adb";
const device = process.env.BLUESTACKS_ADB_DEVICE || "emulator-5554";
const model = execFileSync(adb, ["-s", device, "shell", "getprop", "ro.product.model"], { encoding: "utf8" }).trim();
if (model !== "NE2211") throw new Error(`ADB device ${device} is not the expected BlueStacks instance`);
const sockets = execFileSync(adb, ["-s", device, "shell", "cat", "/proc/net/unix"], { encoding: "utf8" });
if (!sockets.includes("@chrome_devtools_remote")) throw new Error("BlueStacks Chrome debugging socket is unavailable");
execFileSync(adb, ["-s", device, "forward", "tcp:9224", "localabstract:chrome_devtools_remote"]);

const browser = await chromium.connectOverCDP("http://127.0.0.1:9224");
const page = browser.contexts()[0]?.pages().find(item => item.url().startsWith("https://www.czce.com.cn/"));
if (!page) {
  await browser.close();
  throw new Error("Open the CZCE listed-products page in BlueStacks Chrome first");
}
const original = page.url();
const result = { passed: [], failed: [], restored: false };
try {
  const index = "https://www.czce.com.cn/cn/sspz/H077002index_1.htm";
  let indexResponse = null;
  if (page.url() !== index) {
    indexResponse = await page.goto(index, { waitUntil: "domcontentloaded", timeout: 18000 });
    if (indexResponse?.status() !== 200) throw new Error(`CZCE catalog HTTP ${indexResponse?.status()}`);
  }
  const links = await page.locator("a[href]").evaluateAll(nodes => [...new Set(nodes
    .map(node => node.href)
    .filter(url => /^https:\/\/www\.czce\.com\.cn\/cn\/sspz\/[^/]+\/H077002\d{3}index_1\.htm$/.test(url)))]);
  const targets = links.map(url => {
    const id = url.match(/H077002(\d{3})index_1\.htm$/)?.[1];
    return { code: channelCodes[id], url };
  }).filter(item => item.code);
  if (new Set(targets.map(item => item.code)).size !== 27) throw new Error("CZCE official catalog does not list 27 futures products");
  let indexHash = null;
  if (indexResponse) {
    const indexBytes = await indexResponse.body();
    indexHash = createHash("sha256").update(indexBytes).digest("hex");
    const indexDir = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", "catalog");
    mkdirSync(indexDir, { recursive: true });
    writeFileSync(resolve(indexDir, `${indexHash}.html`), indexBytes);
  }
  for (const { code, url } of targets) {
    try {
      const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 18000 });
      if (response?.status() !== 200 || page.url() !== url) throw new Error(`HTTP ${response?.status()}`);
      const bytes = await response.body();
      const text = bytes.toString("utf8");
      if (!["交易品种", "交易单位", "交易代码", "期货合约"].every(label => text.includes(label))) {
        throw new Error("Futures terms are absent from official response");
      }
      const hash = createHash("sha256").update(bytes).digest("hex");
      const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", code);
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `${hash}.html`), bytes);
      writeFileSync(resolve(directory, `${hash}.json`), JSON.stringify({
        productKey: `CZCE.${code}`, sourceUrl: url, payloadSha256: hash,
        retrievedAt: new Date().toISOString(), adapter: "BLUESTACKS_CHROME_CDP",
        ...(indexHash ? { catalogSha256: indexHash } : {}),
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
  execFileSync(adb, ["-s", device, "forward", "--remove", "tcp:9224"]);
}
console.log(JSON.stringify(result));
if (result.failed.length || !result.restored) process.exitCode = 2;
