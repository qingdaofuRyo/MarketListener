// Save an already open CZCE futures product page from BlueStacks Chrome.
// This is a rendered DOM snapshot, not the original HTTP response body.
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
if (model !== "NE2211") throw new Error(`Unexpected BlueStacks ADB model: ${model}`);
execFileSync(adb, ["-s", device, "forward", "tcp:9224", "localabstract:chrome_devtools_remote"]);
const browser = await chromium.connectOverCDP("http://127.0.0.1:9224");
const captured = [];
try {
  for (const context of browser.contexts()) {
    for (const page of context.pages()) {
      const url = page.url();
      const match = url.match(/^https:\/\/www\.czce\.com\.cn\/cn\/sspz\/[^/]+\/H077002(\d{3})index_1\.htm$/);
      const code = match && channelCodes[match[1]];
      if (!code) continue;
      const navigation = await page.evaluate(() => performance.getEntriesByType("navigation")
        .map(entry => ({ responseStatus: entry.responseStatus, transferSize: entry.transferSize }))[0] || null);
      if (navigation?.responseStatus !== 200) continue;
      const html = await page.content();
      const tables = await page.locator("table").evaluateAll(items => items
        .map(item => item.innerText)
        .filter(text => text.includes("交易单位") && text.includes("交易代码")));
      if (tables.length < 1 || !tables[0].includes(code)
          || !["交易品种", "报价单位", "最小变动价位", "最后交易日", "上市交易所"].every(label => tables[0].includes(label))) {
        continue;
      }
      const bytes = Buffer.from(html, "utf8");
      const digest = createHash("sha256").update(bytes).digest("hex");
      const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", code);
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `${digest}.html`), bytes);
      writeFileSync(resolve(directory, `${digest}.json`), JSON.stringify({
        productKey: `CZCE.${code}`, sourceUrl: url, payloadSha256: digest,
        retrievedAt: new Date().toISOString(), adapter: "BLUESTACKS_CHROME_DOM",
        captureKind: "RENDERED_DOM_SNAPSHOT",
        navigationResponseStatus: navigation.responseStatus,
        navigationTransferSize: navigation.transferSize,
      }, null, 2));
      captured.push({ code, url, bytes: bytes.length, sha256: digest });
    }
  }
} finally {
  await browser.close();
  execFileSync(adb, ["-s", device, "forward", "--remove", "tcp:9224"]);
}
console.log(JSON.stringify({ captured }));
