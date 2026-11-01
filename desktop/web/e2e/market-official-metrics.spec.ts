import { expect, test, type Page } from "@playwright/test";

const metrics = { notionalValue: 400_000_000, capitalDeposit: 40_000_000, amount: 200_000_000,
  officialMetricsDate: "2026-09-30", sourcePolicyVersion: "futures-tdx-official-supplements-v1" };
function item(index: number, prefix = "合约") {
  return { instrumentId: `CN.SHFE.FUTURE.CU${String(index).padStart(4, "0")}`, symbol: `CU${index}`,
    name: `${prefix}${index}`, assetType: "FUTURE", seriesKind: "CONTRACT", market: "CN",
    latestPrice: index, openInterest: index * 10, ...metrics };
}
const bars = Array.from({ length: 30 }, (_, index) => ({
  barOpenTime: `2026-09-${String(index + 1).padStart(2, "0")}T09:00:00+08:00`,
  open: 100, high: 105, low: 95, close: 102, volume: 10, ...metrics,
}));
async function mockChartRoutes(page: Page) {
  await page.route("**/api/composites/**", route => route.fulfill({ json: { events: [] } }));
  await page.route("**/api/market/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/categories")) return route.fulfill({ json: { items: [{ id: "cn-future-shfe", label: "上海期货交易所" }] } });
    if (path.endsWith("/cache-status")) return route.fulfill({ json: { dataVersion: "official-finance-test" } });
    if (path.endsWith("/drawings")) return route.fulfill({ json: { items: [] } });
    return route.fulfill({ json: { bars, drawings: [], series: {}, total: bars.length, availablePeriods: ["1d", "1h"], hasMore: false } });
  });
}

test("首屏不等元数据或剩余分页，三路分页保持同版本全局顺序", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockChartRoutes(page);
  let releaseMetadata!: () => void;
  const metadataGate = new Promise<void>(resolve => { releaseMetadata = resolve; });
  await page.route("**/api/strategy/**", async route => { await metadataGate; await route.fulfill({ json: { items: [] } }); });
  await page.route("**/api/market/categories**", async route => { await metadataGate; await route.fulfill({ json: { items: [{ id: "cn-future-shfe", label: "上海期货交易所" }] } }); });
  await page.route("**/api/market/cache-status**", async route => { await metadataGate; await route.fulfill({ json: { dataVersion: "official-finance-test" } }); });
  const items = Array.from({ length: 1501 }, (_, index) => item(index + 1));
  let releasePages!: () => void;
  const pagesGate = new Promise<void>(resolve => { releasePages = resolve; });
  let active = 0, maxActive = 0;
  const versions: Array<string | null> = [];
  await page.route("**/api/market/instruments?**", async route => {
    const url = new URL(route.request().url());
    const number = Number(url.searchParams.get("page"));
    const field = url.searchParams.get("sortField");
    const direction = url.searchParams.get("sortDirection");
    const ordered = field === "latestPrice" ? [...items].sort((a, b) => (a.latestPrice - b.latestPrice) * (direction === "desc" ? -1 : 1)) : items;
    if (number > 1) {
      versions.push(url.searchParams.get("version")); active++; maxActive = Math.max(maxActive, active);
      await pagesGate; active--;
    }
    await route.fulfill({ json: { items: ordered.slice((number - 1) * 500, number * 500), total: items.length, dataVersion: "official-finance-test" } });
  });
  await page.goto("/market/all/?category=cn-future-shfe");
  await expect(page.getByText("合约1", { exact: true }).first()).toBeVisible();
  await expect.poll(() => maxActive).toBe(3);
  expect(versions).toEqual(["official-finance-test", "official-finance-test", "official-finance-test"]);
  await expect(page.locator(".list-workbench .el-loading-mask")).toHaveCount(0);
  await expect(page.locator('[data-test="market-loading-skeleton"][data-kind="list"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "按总市值排序", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "按流通市值排序", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "按市值排序", exact: true })).toBeVisible();
  expect((await page.locator(".list-workbench .column-sort").evaluateAll(nodes =>
    nodes.map(node => node.getAttribute("aria-label")))).slice(3, 6))
    .toEqual(["按市值排序", "按成交额排序", "按沉淀资金排序"]);
  await expect(page.locator(".board").first().locator('[data-field="市值"] strong')).toHaveText("4亿");
  for (const field of ["市值", "沉淀资金", "额"]) {
    await expect(page.locator(".board").nth(1).locator(`[data-field="${field}"] strong`)).toHaveText("—");
  }
  releasePages(); releaseMetadata();
  await expect(page.locator('[data-test="market-loading-skeleton"][data-kind="list"]')).toHaveCount(0);
  await page.getByRole("button", { name: "按最新价排序", exact: true }).click();
  await expect(page.locator(".instrument-row .row-main").first()).toContainText("合约1501");
});

test("分页409自动重取，旧版本行不会混入新版本", async ({ page }) => {
  await mockChartRoutes(page);
  await page.route("**/api/strategy/**", route => route.fulfill({ json: { items: [] } }));
  let firstPageRequests = 0;
  await page.route("**/api/market/instruments?**", route => {
    const url = new URL(route.request().url());
    const number = Number(url.searchParams.get("page"));
    if (number === 1) firstPageRequests++;
    if (number > 1 && url.searchParams.get("version") === "old") return route.fulfill({ status: 409, json: { detail: "行情数据版本已更新，请重新加载列表" } });
    const version = firstPageRequests === 1 ? "old" : "new";
    const items = Array.from({ length: 501 }, (_, index) => item(index + 1, version === "old" ? "旧" : "新"));
    return route.fulfill({ json: { items: items.slice((number - 1) * 500, number * 500), total: items.length, dataVersion: version } });
  });
  await page.goto("/market/all/?category=cn-future-shfe");
  await expect(page.locator(".instrument-row .row-main").first()).toContainText("新1");
  await expect(page.locator('[data-test="market-loading-skeleton"][data-kind="list"]')).toHaveCount(0);
  expect(firstPageRequests).toBe(2);
  await expect(page.locator(".instrument-row .row-main").filter({ hasText: "旧" })).toHaveCount(0);
});

for (const kind of ["MAIN", "WEIGHTED"]) {
  test(`${kind}连续合约保留通达信价并展示同日官网补充指标`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await mockChartRoutes(page);
    await page.route("**/api/strategy/**", route => route.fulfill({ json: { items: [] } }));
    const mapped = kind === "MAIN" ? { underlyingContractCode: "JM2701", mappingStatus: "MATCHED", mappingAsOfDate: "2026-09-24" } : {};
    const continuous = { ...item(1), instrumentId: `CN.DCE.FUTURE.JM.${kind}`, symbol: kind === "MAIN" ? "JML8" : "JML9",
      name: kind === "MAIN" ? "焦煤主连" : "焦煤加权", seriesKind: kind, latestPrice: 1509,
      quoteAsOf: "2026-09-24", dailyAsOf: "2026-09-24", officialMetricsDate: "2026-09-24",
      capitalDepositLong: 20_000_000, capitalDepositShort: 20_000_000, ...mapped };
    await page.route("**/api/market/instruments?**", route => route.fulfill({ json: { items: [continuous], total: 1, dataVersion: "continuous-test" } }));
    await page.route("**/api/market/instruments/*/bars?**", route => route.fulfill({ json: {
      bars: bars.map(bar => ({ ...bar, close: 1509, ...mapped, officialMetricsDate: "2026-09-24" })),
      total: bars.length, availablePeriods: ["1d", "1h"], hasMore: false,
    } }));
    await page.goto("/market/all/?category=cn-future-shfe");
    const row = page.locator(".instrument-row").first();
    await expect(row).toContainText(continuous.name);
    await expect(row.locator(".quote-as-of")).toHaveCount(0);
    await expect(row.locator(".column-name")).toHaveAttribute("title", /2026-09-24/);
    await expect(row.locator(".column-capitalDeposit")).toHaveAttribute("title", /多头 2,000万元 \+ 空头 2,000万元/);
    await expect(row.locator(".column-capitalDeposit")).toHaveAttribute("title", /通达信结算价/);
    const board = page.locator(".board").first();
    await expect(board.locator('[data-field="市值"] strong')).toHaveText("4亿");
    await expect(board.locator('[data-field="沉淀资金"] strong')).toHaveText("4,000万");
    await expect(board.locator('[data-field="额"] strong')).toHaveText("2亿");
    if (kind === "MAIN") {
      await expect(row.locator(".month-mapping")).toContainText("JM2701");
      await expect(board.locator('[data-field="映射合约"] strong')).toHaveText("JM2701");
    } else {
      await expect(board.locator('[data-field="映射合约"]')).toHaveCount(0);
    }
  });
}

test("月份量仓差异暂停派生值，官网成交额仍可查看", async ({ page }) => {
  await mockChartRoutes(page);
  await page.route("**/api/strategy/**", route => route.fulfill({ json: { items: [] } }));
  const pending = { ...item(2611, "红枣"), quoteAsOf: "2026-09-30", dailyAsOf: "2026-09-30",
    comparisonStatus: "MISMATCH", notionalValue: null, capitalDeposit: null,
    capitalDepositReason: "通达信与官网结算价或持仓量不同，派生指标待核验" };
  await page.route("**/api/market/instruments?**", route => route.fulfill({ json: {
    items: [pending], total: 1, dataVersion: "tdx-source-difference-test",
  } }));
  await page.goto("/market/all/?category=cn-future-shfe");
  const row = page.locator(".instrument-row").first();
  await expect(row.locator(".column-notionalValue")).toContainText("—");
  await expect(row.locator(".column-amount")).toContainText("2亿");
  await expect(row.locator(".column-capitalDeposit")).toContainText("—");
  await expect(row.locator(".column-capitalDeposit")).toHaveAttribute("title", /待核验/);
  await expect(row.locator(".column-name")).toHaveAttribute("title", /通达信量价仓/);
});
