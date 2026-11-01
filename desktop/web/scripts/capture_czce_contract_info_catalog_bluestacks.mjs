// Collect CZCE futures contract terms through the official catalog links
// already open in BlueStacks Chrome. Existing tabs are left unchanged.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const products = {
  "001": ["WH", "强麦"], "002": ["PM", "普麦"], "003": ["CF", "棉花"],
  "004": ["SR", "白糖"], "005": ["TA", "PTA"], "006": ["OI", "菜籽油"],
  "007": ["RI", "早籼稻"], "008": ["MA", "甲醇"], "009": ["FG", "玻璃"],
  "010": ["RS", "油菜籽"], "011": ["RM", "菜籽粕"], "012": ["ZC", "动力煤"],
  "013": ["JR", "粳稻"], "014": ["LR", "晚籼稻"], "017": ["SF", "硅铁"],
  "019": ["CY", "棉纱"], "020": ["SM", "锰硅"], "021": ["AP", "苹果"],
  "022": ["CJ", "红枣"], "023": ["UR", "尿素"], "024": ["SA", "纯碱"],
  "025": ["PF", "短纤"], "026": ["PK", "花生"], "027": ["PX", "对二甲苯"],
  "028": ["SH", "烧碱"], "029": ["PR", "瓶片"], "030": ["PL", "丙烯"],
};
const selectedCodes = new Set((process.env.CZCE_CODES || Object.values(products).map(v => v[0]).join(","))
  .split(",").map(code => code.trim().toUpperCase()));
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
const catalogUrl = "https://www.czce.com.cn/cn/sspz/H077002index_1.htm";
const catalog = context.pages().find(page => page.url() === catalogUrl);
if (!catalog) {
  await browser.close();
  throw new Error("Keep the official CZCE listed-products catalog open in BlueStacks Chrome");
}
const catalogLinks = await catalog.locator("a[href]").evaluateAll(nodes => [...new Set(nodes
  .map(node => node.href)
  .filter(url => /^https:\/\/www\.czce\.com\.cn\/cn\/sspz\/[^/]+\/H077002\d{3}index_1\.htm$/.test(url)))]);
const sources = catalogLinks.map(url => {
  const id = url.match(/H077002(\d{3})index_1\.htm$/)?.[1];
  return { id, code: id ? products[id]?.[0] : null, name: id ? products[id]?.[1] : null, productUrl: url };
}).filter(item => item.code);
if (new Set(sources.map(item => item.code)).size !== 27) {
  await browser.close();
  throw new Error(`Expected 27 distinct futures product links; found ${sources.length}`);
}
const catalogBytes = Buffer.from(await catalog.content(), "utf8");
const catalogHash = createHash("sha256").update(catalogBytes).digest("hex");
const catalogDir = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", "catalog");
mkdirSync(catalogDir, { recursive: true });
writeFileSync(resolve(catalogDir, `${catalogHash}.html`), catalogBytes);
writeFileSync(resolve(catalogDir, `${catalogHash}.json`), JSON.stringify({
  sourceUrl: catalogUrl, payloadSha256: catalogHash, retrievedAt: new Date().toISOString(),
  adapter: "BLUESTACKS_CHROME_DOM", captureKind: "RENDERED_DOM_SNAPSHOT", productLinks: 27,
}, null, 2));

const result = { passed: [], failed: [] };
try {
  for (const { id, code, name, productUrl } of sources) {
    if (!selectedCodes.has(code)) continue;
    const page = await context.newPage();
    try {
      const searchUrl = `https://www.baidu.com/s?wd=${encodeURIComponent(`郑州商品交易所 ${name} 期货合约 官网`)}`;
      const search = await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 18000 });
      if (search?.status() !== 200) throw new Error(`search HTTP ${search?.status()}`);
      const product = await page.goto(productUrl, { referer: searchUrl, waitUntil: "domcontentloaded", timeout: 18000 });
      if (product?.status() !== 200 || page.url() !== productUrl) {
        throw new Error(`product HTTP ${product?.status()}`);
      }
      const contractLinks = await page.locator("a[href]").evaluateAll(nodes => [...new Set(nodes
        .map(node => node.href)
        .filter(url => /^https:\/\/www\.czce\.com\.cn\/cn\/sspz\/(?:[^/]+\/)+H077002\d{9}index_1\.htm$/.test(url)))]);
      const deep = contractLinks.find(url => url.includes("/bzhy/qhhy/")
        && url.includes(`H077002${id}`));
      if (!deep) throw new Error("product page lacks futures-contract link");
      const contractResponse = await page.goto(deep, { referer: searchUrl, waitUntil: "domcontentloaded", timeout: 18000 });
      if (contractResponse?.status() !== 200 || page.url() !== deep) {
        throw new Error(`contract HTTP ${contractResponse?.status()}`);
      }
      const fields = await page.locator("table").evaluateAll(tables => {
        for (const table of tables) {
          const parsed = {};
          for (const row of table.querySelectorAll("tr")) {
            const cells = [...row.querySelectorAll("th,td")]
              .map(cell => (cell.textContent || "").replace(/\s+/g, "").trim());
            for (let i = 0; i + 1 < cells.length; i += 2) if (cells[i]) parsed[cells[i]] = cells[i + 1];
          }
          if (parsed["交易单位"] && parsed["交易代码"]) return parsed;
        }
        return {};
      });
      if (fields["交易代码"]?.toUpperCase() !== code
          || !["交易品种", "交易单位", "报价单位", "最小变动价位", "最后交易日", "上市交易所"]
            .every(label => fields[label])) {
        throw new Error("futures contract table does not match product code");
      }
      const bytes = await contractResponse.body();
      if (bytes.length < 1000) throw new Error("official HTTP response is too short");
      const hash = createHash("sha256").update(bytes).digest("hex");
      const directory = resolve(root, "data_control", "bronze", "futures_contract_info", "CZCE", code);
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `${hash}.html`), bytes);
      writeFileSync(resolve(directory, `${hash}.json`), JSON.stringify({
        productKey: `CZCE.${code}`, sourceUrl: deep, payloadSha256: hash,
        retrievedAt: new Date().toISOString(), adapter: "BLUESTACKS_CHROME_CDP",
        entryPoint: "OFFICIAL_CATALOG_HREF", productUrl, catalogSha256: catalogHash,
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
