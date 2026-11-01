// Follow indexed CZCE futures-contract links in the user's BlueStacks Chrome.
// A rendered official-page DOM snapshot is saved after navigation status,
// product code, and futures terms are checked. Existing tabs are not navigated.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const products = {
  WH: ["001", "强麦"], PM: ["002", "普通小麦"], CF: ["003", "棉花"],
  SR: ["004", "白糖"], TA: ["005", "PTA"], OI: ["006", "菜籽油"],
  RI: ["007", "早籼稻"], MA: ["008", "甲醇"], FG: ["009", "玻璃"],
  RS: ["010", "油菜籽"], RM: ["011", "菜籽粕"], ZC: ["012", "动力煤"],
  JR: ["013", "粳稻"], LR: ["014", "晚籼稻"], SF: ["017", "硅铁"],
  CY: ["019", "棉纱"], SM: ["020", "锰硅"], AP: ["021", "苹果"],
  CJ: ["022", "红枣"], UR: ["023", "尿素"], SA: ["024", "纯碱"],
  PF: ["025", "短纤"], PK: ["026", "花生"], PX: ["027", "对二甲苯"],
  SH: ["028", "烧碱"], PR: ["029", "瓶片"], PL: ["030", "丙烯"],
};
const selected = (process.env.CZCE_CODES || Object.keys(products).join(","))
  .split(",").map(code => code.trim().toUpperCase()).filter(code => products[code]);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const adb = process.env.MARKETLISTENER_ADB || "adb";
const device = process.env.BLUESTACKS_ADB_DEVICE || "127.0.0.1:5555";
const model = execFileSync(adb, ["-s", device, "shell", "getprop", "ro.product.model"], { encoding: "utf8" }).trim();
if (model !== "NE2211") throw new Error(`ADB device ${device} is not the expected BlueStacks instance`);
const sockets = execFileSync(adb, ["-s", device, "shell", "cat", "/proc/net/unix"], { encoding: "utf8" });
if (!sockets.includes("@chrome_devtools_remote")) throw new Error("BlueStacks Chrome debugging socket is unavailable");
execFileSync(adb, ["-s", device, "forward", "tcp:9224", "localabstract:chrome_devtools_remote"]);

const browser = await chromium.connectOverCDP("http://127.0.0.1:9224");
const context = browser.contexts()[0];
const result = { passed: [], failed: [] };
try {
  for (const code of selected) {
    const [channel, name] = products[code];
    const page = await context.newPage();
    try {
      const pattern = new RegExp(`^https://www\\.czce\\.com\\.cn/cn/sspz/(?:[^/]+/)+H077002${channel}(?:\\d{6})?index_1\\.htm$`);
      let candidate = null;
      let searchUrl = "";
      for (const query of [
        `郑州商品交易所 ${name} 期货合约 官网`,
        `site:czce.com.cn/cn/sspz/ ${name} 期货合约`,
        `郑州商品交易所 ${name} 标准合约 期货合约`,
        `H077002${channel}001001index_1.htm`,
      ]) {
        searchUrl = `https://www.baidu.com/s?wd=${encodeURIComponent(query)}`;
        const search = await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 18000 });
        if (search?.status() !== 200) continue;
        const cards = await page.locator(".c-result[data-log]").evaluateAll(nodes => nodes.map((node, index) => {
          try { return { index, url: JSON.parse(node.getAttribute("data-log") || "{}").mu || "" }; }
          catch { return { index, url: "" }; }
        }));
        candidate = cards.find(item => pattern.test(item.url) && item.url.includes("/qhhy/"));
        if (candidate) break;
      }
      if (!candidate) throw new Error("no indexed official futures-contract page");
      const card = page.locator(".c-result[data-log]").nth(candidate.index);
      await card.click({ noWaitAfter: true, timeout: 8000 });
      await page.waitForURL(candidate.url, { timeout: 18000 });
      await page.waitForLoadState("domcontentloaded", { timeout: 18000 });
      const navigation = await page.evaluate(() => performance.getEntriesByType("navigation")
        .map(entry => ({ responseStatus: entry.responseStatus, transferSize: entry.transferSize }))[0] || null);
      if (navigation?.responseStatus !== 200) throw new Error(`exchange HTTP ${navigation?.responseStatus}`);
      const contract = await page.locator("table").evaluateAll(tables => {
        for (const table of tables) {
          const fields = {};
          for (const row of table.querySelectorAll("tr")) {
            const cells = [...row.querySelectorAll("th,td")].map(cell => (cell.textContent || "").replace(/\s+/g, "").trim());
            for (let i = 0; i + 1 < cells.length; i += 2) if (cells[i]) fields[cells[i]] = cells[i + 1];
          }
          if (fields["交易单位"] && fields["交易代码"]) return fields;
        }
        return {};
      });
      if (contract["交易代码"]?.toUpperCase() !== code
          || !["交易品种", "交易单位", "报价单位", "最小变动价位", "最后交易日", "上市交易所"]
            .every(label => contract[label])) {
        throw new Error("official response lacks matching futures terms");
      }
      const bytes = Buffer.from(await page.content(), "utf8");
      const raw = bytes.toString("utf8");
      if (!raw.includes("交易代码") || !raw.includes("交易单位") || !raw.includes(code)) {
        throw new Error("rendered official page differs from futures table");
      }
      const hash = createHash("sha256").update(bytes).digest("hex");
      const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", code);
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `${hash}.html`), bytes);
      writeFileSync(resolve(directory, `${hash}.json`), JSON.stringify({
        productKey: `CZCE.${code}`, sourceUrl: candidate.url, payloadSha256: hash,
        retrievedAt: new Date().toISOString(), adapter: "BLUESTACKS_CHROME_DOM",
        captureKind: "RENDERED_DOM_SNAPSHOT",
        navigationResponseStatus: navigation.responseStatus,
        navigationTransferSize: navigation.transferSize,
        entryPoint: "BAIDU_INDEXED_OFFICIAL_LINK",
        searchUrl,
      }, null, 2));
      result.passed.push(code);
      process.stdout.write(`${JSON.stringify({ code, status: "PASS", bytes: bytes.length, hash })}\n`);
    } catch (error) {
      result.failed.push({ code, error: String(error).slice(0, 180) });
      process.stdout.write(`${JSON.stringify({ code, status: "FAILED", error: String(error).slice(0, 180) })}\n`);
    } finally {
      await page.close();
    }
    await new Promise(resolveTimeout => setTimeout(resolveTimeout, 750));
  }
} finally {
  await browser.close();
  execFileSync(adb, ["-s", device, "forward", "--remove", "tcp:9224"]);
}
console.log(JSON.stringify(result));
if (result.failed.length) process.exitCode = 2;
