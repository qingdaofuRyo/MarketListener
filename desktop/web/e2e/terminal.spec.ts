import { expect, test, type Page } from "@playwright/test";

const ROUTES = [
  ["/", "全部行情"],
  ["/market/", "行情"],
  ["/data/", "数据"],
  ["/futures/", "期货数据"],
  ["/strategy/", "策略"],
  ["/stats/", "账户分析"],
  ["/f10/", "F10 企业资料"],
  ["/industry/", "产业链"],
  ["/quant/", "金工"],
] as const;

async function expectCleanTerminal(page: Page): Promise<void> {
  const text = await page.locator("body").innerText();
  // Raw log/dashboard JSON is allowed to contain "null"; company cards have
  // their own stricter assertions in the industry and F10 specs.
  for (const forbidden of ["undefined", "NaN", "Invalid Date"]) {
    expect(text).not.toContain(forbidden);
  }
}

async function chooseDrawingTool(page: Page, group: string, tool: string): Promise<void> {
  await page.getByRole("button", { name: group, exact: true }).click();
  await page.getByRole("menuitem", { name: tool, exact: true }).click();
}

test("all terminal routes are reachable and render clean text", async ({
  page,
}) => {
  for (const [route, title] of ROUTES) {
    await page.goto(route);
    if (route === "/" || route === "/market/") {
      await expect(page.locator('[aria-label="全部行情查询工具"]')).toBeVisible();
    } else if (route === "/futures/") {
      await expect(page.locator(".page-heading h1")).toContainText(title);
    } else if (route === "/industry/" || route === "/quant/") {
      await expect(page.locator("h1").first()).toContainText(title);
    } else {
      await expect(page.locator("h1.page-title")).toContainText(title, { timeout: 15_000 });
    }
    await expectCleanTerminal(page);
  }

  await page.goto("/industry-v2/");
  await expect(page).toHaveURL(/\/industry\/$/);
  await expect(page.locator("h1").first()).toContainText("产业链");
});

test("theme switching persists and follows the system scheme", async ({
  page,
}) => {
  await page.goto("/");
  await page.click('[data-test="theme-toggle"]');
  await page.click('[data-test="theme-option-dark"]');
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.evaluate(() => localStorage.getItem("marketlistener.theme")),
  ).resolves.toBe("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.click('[data-test="theme-toggle"]');
  await page.click('[data-test="theme-option-light"]');
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.evaluate(() =>
    localStorage.setItem("marketlistener.theme", "system"),
  );
  await page.emulateMedia({ colorScheme: "dark" });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.emulateMedia({ colorScheme: "light" });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("customer product does not expose internal management actions", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/market\/all\/(?:\?.*)?$/);
  await expect(page.locator(".operation-buttons button")).toHaveCount(0);
  await expect(page.locator(".management-nav")).toHaveCount(0);

  const created = await page.request.post("/api/operations", {
    data: { kind: "STATUS_REFRESH" },
  });
  expect(created.status()).toBe(403);

  const extraField = await page.request.post("/api/operations", {
    data: { kind: "STATUS_REFRESH", sql: "delete from runs" },
  });
  expect(extraField.status()).toBe(403);

  const arbitrary = await page.request.post("/api/operations", {
    data: { kind: "__import__('os').system('whoami')" },
  });
  expect(arbitrary.status()).toBe(403);
});

test("data page shows A/H markets, official exchange statistics and TDX economy", async ({ page }) => {
  test.setTimeout(60_000);
  const boundedListing = await page.request.get("/api/data/f10?page_size=500");
  expect(boundedListing.status()).toBe(200);
  expect(((await boundedListing.json()) as { items: unknown[] }).items.length).toBeLessThanOrEqual(500);
  expect([403, 405]).toContain((await page.request.post("/api/data/f10")).status());
  expect((await page.request.get("/api/data/f10?page_size=501")).status()).toBe(422);
  expect((await page.request.get("/api/data/equities/cn/overview")).status()).toBe(200);
  expect((await page.request.get("/api/data/equities/hk/overview")).status()).toBe(200);
  const staleRequests: string[] = [];
  let catalogRequests = 0;
  let revision = "test-v1";
  let failEtfRead = false;
  let failKplHistory = false;
  let failDataStatus = false;
  page.on("request", request => {
    if (/\/api\/(?:dashboard\/definitions|personal\/dashboard|metrics\/ranking|metrics\/heatmap|data\/f10)/.test(request.url())) staleRequests.push(request.url());
  });
  await page.route("**/api/data/update-status", route => failDataStatus ? route.fulfill({ status: 503 }) : route.fulfill({ json: {
    autoEnabled: true, completedRevision: revision, jobs: [],
  } }));
  await page.route("**/api/data/tdx-economy/catalog", route => { catalogRequests++; return route.fulfill({ json: {
    items: [
      { id: "TDX_MACRO.3_PMI", name: "制造业采购指数", group: "economy", topic: "中国", latestDay: "2026-09-30", latestValue: 50.1, unit: "指数", source: "通达信金融终端", available: true },
      { id: "TDX_MACRO.2_CPI", name: "居民消费价格指数", group: "economy", topic: "中国", latestDay: "2026-07-31", latestValue: 100.5, unit: "指数", source: "通达信金融终端", available: true },
      { id: "TDX_MACRO.5_M2", name: "广义货币供应量", group: "economy", topic: "中国", latestDay: "2026-07-31", latestValue: 3555077.25, unit: "原始值", source: "通达信金融终端", available: true },
      { id: "TDX_MACRO.8_NONAG", name: "美国非农就业(万)", group: "economy", topic: "美国", latestDay: "2026-09-30", latestValue: 15904.4, unit: "万", source: "通达信金融终端", available: true },
      { id: "TDX_MACRO.8_EFFR", name: "美国联邦基金利率", group: "economy", topic: "美国", latestDay: "2026-09-30", latestValue: 3.88, unit: "%", source: "通达信金融终端", available: true },
      ...["GOLD_SILVER_RATIO", "GOLD_COPPER_RATIO", "GOLD_OIL_RATIO"].map((id, index) => ({
        id, name: ["金银比", "金铜比", "金油比"][index], group: "ratio", topic: "商品比价", latestDay: "2026-10-16",
        latestValue: [73.28, 3044, 41.11][index], unit: "比值", source: "同日外盘期货收盘价", available: true,
      })),
    ],
  } }); });
  await page.route("**/api/data/tdx-economy/series**", route => {
    const id = new URL(route.request().url()).searchParams.get("id") || "";
    return route.fulfill({ json: {
      id, name: id === "GOLD_COPPER_RATIO" ? "金铜比" : "制造业采购指数", group: id.startsWith("GOLD_") ? "ratio" : "economy",
      topic: "测试", unit: "比值", source: "受控测试来源", available: true, limitations: [],
      points: [{ day: "2026-10-15", value: 49.8 }, { day: "2026-10-16", value: 50.1 }],
    } });
  });
  await page.route("**/api/data/tdx-economy/previews**", route => route.fulfill({ json: {
    items: ["TDX_MACRO.3_PMI", "TDX_MACRO.2_CPI", "TDX_MACRO.5_M2", "TDX_MACRO.8_NONAG", "TDX_MACRO.8_EFFR",
      "GOLD_SILVER_RATIO", "GOLD_COPPER_RATIO", "GOLD_OIL_RATIO"].map(id => ({
      id, points: [{ day: "2026-10-15", value: 49.8 }, { day: "2026-10-16", value: 50.1 }],
    })),
  } }));
  await page.route("**/api/data/equities/cn/overview", route => route.fulfill({ json: {
    available: true, market: "CN", limitations: [], points: [
      { tradingDay: "2026-10-15", totalMarketCapYi: 1000, turnoverYi: 600,
        advances: 85, declines: 51, unchanged: 4, limitUpCount: 12, limitDownCount: 5, failedLimitUpCount: 7, sealedLimitUpRatePct: 12 / 19 * 100,
        limitUpHeight: 4, limitDownHeight: 3, yesterdayLimitUpOpenReturnPct: null,
        sources: { turnoverYi: { label: "通达信本机质检日线", coverage: 5568, pairedCoverage: 5566 },
          advances: { label: "通达信本机质检日线", coverage: 5568, pairedCoverage: 5566 } } },
      { tradingDay: "2026-10-16", totalMarketCapYi: 1020, turnoverYi: 630,
        advances: 100, declines: 40, unchanged: 3, limitUpCount: 20, limitDownCount: 4, failedLimitUpCount: 9, sealedLimitUpRatePct: 20 / 29 * 100,
        limitUpHeight: 11, limitDownHeight: 2, yesterdayLimitUpOpenReturnPct: -1.34,
        sources: { turnoverYi: { label: "腾讯行情收盘快照" }, advances: { label: "腾讯行情收盘快照" } } },
    ],
  } }));
  await page.route("**/api/data/equities/hk/overview", route => route.fulfill({ json: {
    available: true, market: "HK", limitations: [], points: [
      { tradingDay: "2026-10-16", totalMarketCapYi: null, turnoverYi: 320,
        advances: 32, declines: 20, unchanged: 2 },
    ],
  } }));
  await page.route("**/api/data/exchanges/overview", route => route.fulfill({ json: {
    available: true, periods: { startDay: "2026-10-15", endDay: "2026-10-16" },
    series: [
      { venue: "SSE", category: "STOCK_A", metric: "TOTAL_MARKET_CAP_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市 A 股", points: [{ day: "2026-10-16", value: 222 }, { day: "2026-10-15", value: 111 }, { day: "2026-10-17", value: null }] },
      { venue: "SSE", category: "STOCK_A", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市 A 股", points: [{ day: "2026-10-15", value: 12 }] },
      { venue: "ALL", category: "STOCK_LISTED", metric: "COMPANY_COUNT", unit: "家", frequency: "DAILY",
        scope: "三所挂牌股票（沪深含B股）", points: [{ day: "2026-10-15", value: 5582 }] },
      { venue: "SSE", category: "STOCK_ALL", metric: "COMPANY_COUNT", unit: "家", frequency: "DAILY",
        scope: "上交所股票", points: [{ day: "2026-10-15", value: 2321 }, { day: "2026-10-16", value: 2322 }] },
      { venue: "SZSE", category: "STOCK_ALL", metric: "COMPANY_COUNT", unit: "家", frequency: "DAILY",
        scope: "深交所股票", points: [{ day: "2026-10-15", value: 2909 }, { day: "2026-10-16", value: 2910 }] },
      { venue: "BSE", category: "STOCK_A", metric: "COMPANY_COUNT", unit: "家", frequency: "DAILY",
        scope: "北交所股票", points: [{ day: "2026-10-15", value: 352 }] },
      { venue: "SSE_SZSE", category: "FUND", metric: "TOTAL_MARKET_CAP_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪深基金", points: [{ day: "2026-10-15", value: 52177.71 }, { day: "2026-10-16", value: 52200.31 }] },
      { venue: "SSE", category: "FUND", metric: "TOTAL_MARKET_CAP_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市基金", points: [{ day: "2026-10-16", value: 35000 }] },
      { venue: "SZSE", category: "FUND", metric: "TOTAL_MARKET_CAP_YI", unit: "亿元", frequency: "DAILY",
        scope: "深市基金", points: [{ day: "2026-10-16", value: 17200.31 }] },
      { venue: "SZSE", category: "FUND", metric: "VOLUME_YI", unit: "亿份", frequency: "DAILY",
        scope: "深市基金", points: [{ day: "2026-10-16", value: 800 }] },
      { venue: "SSE", category: "FUND_REPO", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市基金回购", points: [{ day: "2026-10-16", value: 0 }] },
      { venue: "SSE", category: "FUND_REPO", metric: "VOLUME_YI", unit: "亿份", frequency: "DAILY",
        scope: "沪市基金回购", points: [{ day: "2026-10-16", value: 0 }] },
      { venue: "SSE", category: "FUND_MONEY", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市交易型货币基金", points: [{ day: "2026-10-16", value: 2.5 }] },
      { venue: "SSE", category: "FUND_ETF", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "沪市 ETF", points: [{ day: "2026-10-15", value: 3518.75 }, { day: "2026-10-16", value: 3600.12 }] },
      { venue: "ALL", category: "BOND_CASH", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "三所债券及 ABS 现券", points: [{ day: "2026-10-15", value: 0 }, { day: "2026-10-16", value: 3475.69 }] },
      { venue: "BSE", category: "BOND_PUBLIC_CORP", metric: "TOTAL_AMOUNT_YI", unit: "亿元", frequency: "DAILY",
        scope: "北所公开公司债", points: [{ day: "2026-10-15", value: 0 }, { day: "2026-10-16", value: 0 }] },
      { venue: "BSE", category: "BOND_GOV_ISSUE", metric: "ISSUANCE_YI", unit: "亿元", frequency: "MONTHLY",
        scope: "北所国债发行", points: [{ day: "2026-09", value: 6843 }] },
    ],
    coverage: [
      { day: "2026-10-15", asset: "STOCK_LISTED", requiredVenues: ["SSE", "SZSE", "BSE"],
        presentVenues: ["SSE", "SZSE", "BSE"], missingVenues: [], status: "COMPLETE" },
      { day: "2026-10-16", asset: "STOCK_LISTED", requiredVenues: ["SSE", "SZSE", "BSE"],
        presentVenues: ["SSE", "SZSE"], missingVenues: ["BSE"], status: "PARTIAL" },
      { day: "2026-10-16", asset: "FUND", requiredVenues: ["SSE", "SZSE"],
        presentVenues: ["SZSE"], missingVenues: ["SSE"], failedVenues: ["SSE"], status: "PARTIAL" },
    ],
  } }));
  await page.route("**/api/data/ths-cpi", route => route.fulfill({ json: {
    available: true, source: "同花顺CPI公开表", fetchedAt: "2026-10-20T18:00:00Z",
    points: [{ month: "2026-08", index: 100.8, yoyPct: 0.8, momPct: 0.1 },
      { month: "2026-09", index: 100.9, yoyPct: 0.9, momPct: 0.2 }], limitations: [],
  } }));
  await page.route("**/api/data/ths-indexflash/status", route => route.fulfill({ json: {
    status: "BLOCKED", checkedAt: "2026-10-20T11:00:00Z",
  } }));
  await page.route("**/api/data/ths-app-etf/status", route => route.fulfill({ json: {
    status: "UNVERIFIED", autoEnabled: true, intervalMinutes: 15, attemptAt: "2026-10-30T00:00:00Z",
  } }));
  await page.route("**/api/data/ths-app-etf/observation", route => failEtfRead ? route.fulfill({ status: 503 }) : route.fulfill({ json: {
    available: true, status: "UNVERIFIED", scope: "跟踪指数过滤", count: 4,
    captureStart: "2026-10-30T00:00:00Z", captureEnd: "2026-10-30T00:00:05Z", quoteTime: null,
    rows: [{ code: "20:515880", name: "样本ETF", price: 1.2, changePct: 0, turnover: 0,
      observedAt: "2026-10-30T00:00:00Z" },
    { code: "20:515881", name: "上涨样本", price: 1.5, changePct: 2, turnover: 100,
      observedAt: "2026-10-30T00:00:05Z" },
    { code: "20:515882", name: "缺失样本", price: null, changePct: null, turnover: null,
      observedAt: "2026-10-30T00:00:05Z" },
    { code: "20:515883", name: "下跌样本", price: 0.9, changePct: -1, turnover: 50,
      observedAt: "2026-10-30T00:00:05Z" }],
  } }));
  await page.route("**/api/data/kpl-market/status", route => route.fulfill({ json: {
    status: "UNVERIFIED", checkedAt: "2026-10-20T11:00:00Z", observedDate: "2026-10-20",
    calendarStatus: "VERIFIED", checkAge: "RECENT",
  } }));
  await page.route("**/api/data/kpl-market/history", route => failKplHistory ? route.fulfill({ status: 503 }) : route.fulfill({ json: {
    available: true, fetchedAt: "2026-10-28T06:00:00Z", latestDay: "2026-10-28", latestPhase: "INTRADAY",
    calendarIncomplete: false, points: [
      { day: "2026-10-26", limitUp: 59, limitDown: 26, naturalLimitUp: 51, everLimitDown: 8, brokenCount: 23, brokenRatePct: 29.4872 },
      { day: "2026-10-27", limitUp: null, limitDown: null, naturalLimitUp: null, everLimitDown: null, brokenCount: null, brokenRatePct: null },
      { day: "2026-10-28", limitUp: 72, limitDown: 6, naturalLimitUp: 70, everLimitDown: 13, brokenCount: 69, brokenRatePct: 49.2857 },
    ],
  } }));
  await page.route("**/api/data/disclosure-watch**", route => route.fulfill({ json: {
    available: true, total: 1, keywords: ["停牌", "交易异常", "退市"],
    riskSignalTerms: ["立案告知书"], lastFetchedAt: "2026-10-20T18:00:00Z", limitations: [],
    items: [{ announcementId: "123456789", securityCode: "600001", securityName: "合成公司",
      title: "停牌前一日公告", publicationDay: "2026-10-20", publishedAt: "2026-10-20T03:00:00Z",
      pdfUrl: "https://static.cninfo.com.cn/finalpage/2026-10-20/sample.PDF", keyword: "停牌",
      matchStatus: "KEYWORD_HIT_PENDING_REVIEW" }],
  } }));

  await page.goto("/data/");
  await expect(page.locator("h1.page-title")).toHaveText("数据");
  await expect(page.locator('[data-test="data-refresh"]')).toBeVisible();
  for (const id of ["data-section-cn", "data-exchange-stocks", "data-exchange-funds", "data-exchange-bonds",
    "data-section-hk", "data-section-economy", "data-section-watch"]) {
    await expect(page.locator('[data-test="' + id + '"]')).toBeVisible();
  }
  await expect(page.locator('[data-test="exchange-headline-listed-companies"]')).toContainText("5,582");
  const sseA = page.locator('[data-test="exchange-detail-sse-stock_a"]');
  await expect(sseA.locator('[data-test="exchange-metric-TOTAL_MARKET_CAP_YI"]')).toContainText("222");
  await expect(sseA.locator('[data-test="exchange-metric-TOTAL_MARKET_CAP_YI"]')).toContainText("2026-10-16");
  await expect(sseA.locator('[data-test="exchange-metric-TOTAL_AMOUNT_YI"]')).toContainText("2026-10-15");
  await expect(sseA.locator('[data-test="exchange-metric-FLOAT_MARKET_CAP_YI"]')).toContainText("暂无观测");
  await expect(sseA.locator('[data-test="sparkline-chart"] [role="img"]')).toHaveAttribute("aria-label", /缺报处断线/);
  await expect(page.locator('[data-test="ths-indexflash-status"]')).toContainText("同花顺接口暂时拒绝访问");
  await expect(page.locator('[data-test="ths-indexflash-status"]')).toContainText("口径不同");
  const thsEtf = page.locator('[data-test="ths-app-etf"]');
  await expect(thsEtf).toContainText("行情时间未核验");
  await expect(thsEtf.locator('[data-test="ths-etf-update-status"]')).toContainText("盘中每 15 分钟尝试更新");
  await expect(thsEtf).toContainText("515880");
  await expect(thsEtf).toContainText("0.00%");
  await expect(thsEtf.locator('[data-test="ths-etf-count"]')).toHaveText("显示 4 / 4 只");
  const etfRows = thsEtf.locator("tbody tr");
  await expect(etfRows.first()).toContainText("515880");
  await expect(etfRows.first()).toContainText("2026/10/30 08:00:00");
  await thsEtf.getByRole("button", { name: "涨跌幅" }).click();
  await expect(thsEtf.getByRole("columnheader", { name: "涨跌幅" })).toHaveAttribute("aria-sort", "descending");
  await expect(etfRows.first()).toContainText("515881");
  await expect(etfRows.last()).toContainText("515882");
  await thsEtf.getByRole("button", { name: "涨跌幅" }).click();
  await expect(etfRows.first()).toContainText("515883");
  await expect(etfRows.last()).toContainText("515882");
  await thsEtf.getByRole("button", { name: "恢复采集顺序" }).click();
  await expect(etfRows.first()).toContainText("515880");
  await page.setViewportSize({ width: 390, height: 844 });
  const etfRegion = thsEtf.getByRole("region", { name: "同花顺 ETF 观察表" });
  expect(await etfRegion.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  await expect(thsEtf.getByRole("searchbox")).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 720 });
  await thsEtf.getByRole("searchbox").fill("无匹配代码");
  await expect(thsEtf.locator('[data-test="ths-etf-count"]')).toHaveText("显示 0 / 4 只");
  await expect(thsEtf).toContainText("没有符合条件的 ETF");
  await thsEtf.getByRole("searchbox").clear();
  failDataStatus = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator('[data-test="data-update-status-error"]')).toContainText("保留上次读取结果");
  await expect(page.locator('[data-test="data-update-status"]')).toContainText("上次读取");
  failDataStatus = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator('[data-test="data-update-status-error"]')).toHaveCount(0);
  const previousCatalogRequests = catalogRequests;
  revision = "test-v2";
  failEtfRead = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => catalogRequests).toBeGreaterThan(previousCatalogRequests);
  await expect(thsEtf.locator('[data-test="ths-etf-read-error"]')).toContainText("保留上次读取结果");
  await expect(thsEtf).toContainText("515880");
  failEtfRead = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(thsEtf.locator('[data-test="ths-etf-read-error"]')).toHaveCount(0);
  await expect(page.locator('[data-test="kpl-market-status"]')).toContainText("口径仍待核验");
  await expect(page.locator('[data-test="kpl-market-status"]')).toContainText("不计入上方图表");
  const kplChart = page.locator('[data-test="kpl-market-history"]');
  await expect(kplChart).toContainText("当日盘中样本");
  await expect(kplChart).toContainText("不与上方东方财富序列混合");
  await expect(kplChart.locator('[role="img"]').first()).toHaveAttribute("aria-label", /缺报处断线/);
  failKplHistory = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator('[data-test="kpl-read-error"]')).toBeVisible();
  await expect(kplChart).toContainText("2026-10-28");
  failKplHistory = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator('[data-test="kpl-read-error"]')).toHaveCount(0);
  await expect(page.locator('[data-test="exchange-headline-listed-companies"]')).toContainText("缺 BSE");
  await expect(page.locator('[data-test="exchange-headline-fund-cap"]')).toContainText("52,200.31");
  await expect(page.locator('[data-test="exchange-headline-fund-cap"]')).toContainText("SSE＋SZSE已核验");
  await expect(page.locator('[data-test="exchange-headline-fund-cap"]')).not.toContainText("合计待补");
  await expect(page.locator('[data-test="exchange-headline-fund-volume"]')).toContainText("SSE来源失败；合计待补");
  await expect(page.locator('[data-test="exchange-detail-sse-fund_repo"]')).toContainText("不计入基金现货合计");
  await expect(page.locator('[data-test="exchange-detail-sse-fund_money"]')).toContainText("包含在 ETF 内");
  await expect(page.locator('[data-test="exchange-detail-sse-fund_etf"]')).toContainText("3,600.12");
  await expect(page.locator('[data-test="exchange-headline-bond-cash-turnover"]')).toContainText("3,475.69");
  await expect(page.locator('[data-test="exchange-detail-bse-bond_public_corp"]')).toContainText("0");
  await expect(page.locator('[data-test="exchange-bond-monthly"]')).toContainText("2026-09");
  await expect(page.getByRole("tab")).toHaveCount(0);
  for (const id of ["cn-market-cap", "cn-turnover", "cn-breadth", "cn-limit-pool", "cn-broken-board", "cn-sealed-rate", "cn-limit-height", "cn-limit-up-open-return"]) {
    await expect(page.locator('[data-test="data-panel-' + id + '"]')).toBeVisible();
  }
  for (const id of ["hk-market-cap", "hk-turnover", "hk-breadth"]) {
    await expect(page.locator('[data-test="data-panel-' + id + '"]')).toBeVisible();
  }
  await expect(page.locator('[data-test="data-panel-cn-limit-height"]')).toContainText("A股涨跌停高度");
  await expect(page.locator('[data-test="data-panel-cn-limit-height"]')).toHaveAttribute("data-chart-type", "line");
  await expect(page.locator('[data-test="data-panel-cn-limit-pool"]')).toHaveAttribute("data-chart-type", "bar");
  await expect(page.locator('[data-test="data-panel-cn-broken-board"]')).toHaveAttribute("data-chart-type", "bar");
  await expect(page.locator('[data-test="data-panel-cn-sealed-rate"]')).toBeVisible();
  await expect(page.locator('[data-test="data-panel-cn-turnover"]')).toContainText("通达信本机质检日线、腾讯行情收盘快照");
  await page.locator('[data-test="data-panel-cn-turnover"] .chart-root').hover({ position: { x: 90, y: 110 } });
  await expect(page.getByText("覆盖 5568 只", { exact: false })).toBeVisible();
  await expect(page.locator('[data-test="data-ths-cpi"]')).toContainText("2026-09");
  await expect(page.locator('[data-test="data-disclosure-watch"]')).toContainText("停牌前一日公告");
  await expect(page.locator('[data-test="data-disclosure-watch"]')).toContainText("待核查");
  await expect(page.locator('[data-test="data-search-interest"]')).toContainText("无可核验逐日序列");
  await expect(page.locator('[data-test="data-panel-cn-breadth"] [data-test="composition-donut"]')).toBeVisible();
  await expect(page.locator('[data-test="data-panel-cn-limit-pool"] [data-test="composition-donut"]')).toBeVisible();
  await expect(page.locator('[data-test="data-panel-hk-breadth"] [data-test="composition-donut"]')).toBeVisible();
  await expect(page.locator('[data-test="focused-data-page"]')).not.toContainText("高振幅且低涨幅家数");
  await expect(page.locator('[data-test="focused-data-page"]')).not.toContainText("风险与状态名单");
  for (const oldModule of ["我的仪表盘", "指标探索", "动态排行", "热力图", "数据浏览器"]) {
    await expect(page.getByText(oldModule, { exact: true })).toHaveCount(0);
  }

  await expect(page.locator('[data-test="data-economy-TDX_MACRO.3_PMI"]')).toBeVisible();
  await expect(page.locator('[data-test="economy-item-TDX_MACRO.3_PMI"]')).toBeVisible();
  await expect(page.locator('[data-test="economy-item-TDX_MACRO.2_CPI"] .compact-trend')).toBeVisible();
  for (const id of ["GOLD_SILVER_RATIO", "GOLD_COPPER_RATIO", "GOLD_OIL_RATIO"]) {
    await expect(page.locator('[data-test="data-ratio-' + id + '"]')).toBeVisible();
    await expect(page.locator('[data-test="data-ratio-' + id + '"] [data-test="sparkline-chart"]')).toBeVisible();
  }
  await page.locator('[data-test="economy-item-TDX_MACRO.2_CPI"]').click();
  await expect(page.locator('[data-test="data-selected-economy-chart"]')).toContainText("居民消费价格指数历史走势");

  await page.goto("/data/?section=hk");
  await expect(page.locator('[data-test="data-section-cn"]')).toBeVisible();
  await expect(page.locator('[data-test="data-section-economy"]')).toBeVisible();
  await expect(page.locator('[data-test="data-panel-hk-market-cap"]')).toContainText("待补");
  await expect(page).toHaveURL(/section=hk/);
  expect(staleRequests).toEqual([]);
  await expectCleanTerminal(page);
});

test("KPL status polls local metadata and pauses when hidden", async ({ page }) => {
  await page.clock.install();
  let checks = 0;
  await page.route("**/api/data/kpl-market/status", route => {
    checks += 1;
    return route.fulfill({ json: checks === 1 ? {
      status: "EMPTY", checkedAt: "2026-10-20T11:00:00Z", observedDate: null,
      calendarStatus: "VERIFIED", checkAge: "RECENT",
    } : {
      status: "STALE", checkedAt: "2026-10-20T11:00:00Z", observedDate: "2026-10-19",
      calendarStatus: "UNKNOWN", checkAge: "OLD",
    } });
  });
  await page.goto("/data/");
  const card = page.locator('[data-test="kpl-market-status"]');
  await expect(card).toContainText("来源返回空数据");
  expect(checks).toBe(1);
  await page.clock.fastForward(60_000);
  await expect(card).toContainText("不是预期交易日");
  await expect(card).toContainText("2026-10-19");
  await expect(card).toContainText("交易日历尚未核验");
  await expect(card).toContainText("超过 7 天");
  const visibleChecks = checks;
  await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }));
  await page.clock.fastForward(60_000);
  expect(checks).toBe(visibleChecks);
});

test("Hong Kong cards refresh when their local daily aggregate finishes loading", async ({ page }) => {
  let sectionsCalls = 0;
  let hkOverviewCalls = 0;
  let hkReady = false;
  await page.route("**/api/data/sections", async route => {
    const response = await route.fetch();
    const payload = await response.json();
    sectionsCalls += 1;
    const hongKong = payload.sections.find((item: { id: string }) => item.id === "hk-equities");
    if (hkReady) {
      const turnover = hongKong.panels.find((item: { id: string }) => item.id === "hk-turnover");
      turnover.status = "PARTIAL";
      turnover.values = [{ metricKey: "turnoverYi", tradingDate: "2026-10-16", metricName: "成交额", value: 123.45, unit: "亿港元" }];
    }
    await route.fulfill({ json: payload });
  });
  await page.route("**/api/data/equities/hk/overview", route => {
    hkOverviewCalls += 1;
    hkReady = true;
    return route.fulfill({ json: {
      available: true, market: "HK", points: [{ tradingDay: "2026-10-16", totalMarketCapYi: null,
        turnoverYi: 123.45, advances: 12, unchanged: 1, declines: 8 }], limitations: [],
    } });
  });
  await page.goto("/data/?section=hk");
  await expect.poll(() => hkOverviewCalls).toBeGreaterThanOrEqual(1);
  await expect.poll(() => sectionsCalls).toBeGreaterThanOrEqual(2);
  await expect(page.locator('[data-test="data-panel-hk-turnover"]')).toContainText("123.45");
});

test("data source map keeps an undercovered Hong Kong day visibly broken", async ({ page }) => {
  await page.route("**/api/data/equities/hk/overview", route => route.fulfill({ json: {
    available: true, market: "HK", limitations: [], points: [
      { tradingDay: "2026-08-14", totalMarketCapYi: null, turnoverYi: 1989.33,
        turnoverCoverage: 2724, advances: 1000, unchanged: 200, declines: 1000 },
      { tradingDay: "2026-08-17", totalMarketCapYi: null, turnoverYi: null,
        turnoverCoverage: 706, referenceCoverage: 2763, coverageStatus: "INSUFFICIENT_LOCAL_FILES",
        advances: null, unchanged: null, declines: null },
      { tradingDay: "2026-08-18", totalMarketCapYi: null, turnoverYi: 1943.27,
        turnoverCoverage: 2763, advances: 1100, unchanged: 200, declines: 1000 },
    ],
  } }));
  await page.goto("/data/");
  await expect(page.locator('[data-test="data-source-overview"]')).toBeVisible();
  await expect(page.locator('[data-test="data-source-sse"]')).toBeVisible();
  await expect(page.locator('[data-test="data-source-hkex"]')).toBeVisible();
  await expect(page.locator('[data-test="data-source-tdx-hk"]')).toContainText("1 日覆盖不足");
  await expect(page.locator('[data-test="hk-turnover-gap-note"]')).toContainText("2026-08-17 706/2763 只");
});

test("source cards show reading state until each source responds", async ({ page }) => {
  await page.route("**/api/data/sections", async route => {
    await new Promise(resolve => setTimeout(resolve, 1800));
    await route.continue();
  });
  await page.goto("/data/");
  await expect(page.locator('[data-test="data-source-eastmoney"]')).toContainText("读取中");
  await expect(page.locator('[data-test="data-source-eastmoney"]')).not.toContainText("待补");
});

test("F10 API and detail page share one local company model", async ({
  page,
}) => {
  const listing = await page.request.get(
    "/api/f10/companies?page_size=10&market=CN",
  );
  expect(listing.status()).toBe(200);
  const payload = (await listing.json()) as {
    items: Array<{ instrumentKey: string; name: string; code: string }>;
  };
  if (!payload.items.length) {
    await page.goto("/f10/");
    await expect(page.locator("h1.page-title")).toContainText("F10 企业资料库");
    return;
  }
  const first = payload.items[0];
  expect(first.instrumentKey).toBeTruthy();
  expect(first.name).toBeTruthy();
  expect(first.code).toBeTruthy();

  const detailResponse = await page.request.get(
    `/api/f10/companies/${encodeURIComponent(first.instrumentKey)}`,
  );
  expect(detailResponse.status()).toBe(200);
  const detail = (await detailResponse.json()) as {
    instrumentKey: string;
    totalMarketCap?: {
      value: number;
      currency: string;
      asOf: string;
      source: string;
    };
  };
  expect(detail.instrumentKey).toBe(first.instrumentKey);
  expect(detail.totalMarketCap?.value).toBeGreaterThan(0);
  expect(detail.totalMarketCap?.currency).toBeTruthy();
  expect(detail.totalMarketCap?.asOf).toBeTruthy();
  expect(detail.totalMarketCap?.source).toBeTruthy();

  await page.goto("/f10/");
  await expect(
    page.locator(".company-list-panel .el-table__row").first(),
  ).toBeVisible({
    timeout: 15_000,
  });
  await page.locator(".company-list-panel .el-table__row").first().click();
  await expect(page).toHaveURL(/\/f10\/company\//);
  await expect(page.locator(".company-detail")).toBeVisible({
    timeout: 15_000,
  });
  await expectCleanTerminal(page);
});

test("internal logs and their old customer bookmark require the internal console", async ({ page }) => {
  const response = await page.request.get("/api/logs?page_size=100");
  expect(response.status()).toBe(403);
  expect([403, 405]).toContain((await page.request.post("/api/logs")).status());

  await page.goto("/logs/");
  await expect(page).toHaveURL(/\/market\/all\/(?:\?category=[^#]+)?$/);
  await expect(page.locator('[aria-label="全部行情查询工具"]')).toBeVisible();
  await expectCleanTerminal(page);
});

test("industry serves safe knowledge APIs and reports absent Android packages honestly", async ({
  page,
}) => {
  const atlasResponse = await page.request.get("/api/industry/atlas");
  expect(atlasResponse.status()).toBe(410);
  const catalog = await page.request.get("/api/industry/catalog");
  expect(catalog.status()).toBe(200);
  expect(await catalog.text()).not.toContain("synthetic-industry.pdf");

  expect((await page.request.get("/industry-map.html")).status()).toBe(404);

  const infoResponse = await page.request.get("/api/android-package-info");
  const packageResponse = await page.request.get("/api/android-package");
  expect(infoResponse.status()).toBe(404);
  expect(packageResponse.status()).toBe(404);
});

test("market workbench defaults to a resizable list and opens a full-screen chart", async ({
  page,
}) => {
  await page.goto("/market/");
  await expect(page).toHaveURL(/\/market\/all\//);
  await expect(page.getByRole("navigation", { name: "客户端" }).getByRole("link", { name: "全部行情" })).toBeVisible();
  await expect(page.locator(".market-workspace-header h1")).toContainText("全部行情");
  await expect(
    page.locator(".all-toolbar").getByRole("button", { name: "查询" }),
  ).toBeVisible();
  await expect(page.locator("main.market-page > section")).toHaveCount(1);
  await expect(page.locator(".list-workbench")).toBeVisible();
  await expect(
    page.getByRole("separator", { name: "调整行情列表与K线图宽度" }),
  ).toBeVisible();
  await expect(page.locator(".flag-column-title")).toHaveText("标记");
  await expect(page.locator(".sequence-column-title")).toHaveText("序号");
  await expect(page.locator(".list-table-header")).toContainText("近44日涨幅");
  const firstFlag = page.locator(".row-flag").first();
  if (await firstFlag.count()) {
    await firstFlag.click();
    await expect(
      page.getByRole("menu", { name: "选择行标记颜色" }),
    ).toBeVisible();
    await page.getByRole("menuitem", { name: "红色" }).click();
    await expect(page.locator(".instrument-row.flagged").first()).toBeVisible();
  }
  // The desktop terminal can be launched before a local collector has written
  // any instruments.  Layout assertions still apply; chart interaction is
  // conditional on the local data that makes it meaningful.
  const firstRow = page.locator(".row-main").first();
  if ((await firstRow.count()) === 0) {
    await expect(page.locator(".all-toolbar")).toBeVisible();
    return;
  }
  await expect(firstRow).toBeVisible({ timeout: 15_000 });
  await firstRow.dblclick();
  await expect(page.locator(".workbench-overlay")).toBeVisible({
    timeout: 15_000,
  });
  if (await page.locator(".workbench-chart .quote-panel").count()) {
    await expect(
      page.locator(".workbench-chart .quote-panel .quote-pair"),
    ).toHaveCount(7);
  }
  await expect(
    page.locator(".drawing-toolbar").getByRole("button", { name: "线条工具" }),
  ).toBeVisible();
  await expect(
    page.locator(".drawing-toolbar").getByRole("button", { name: "笔刷与激光笔" }),
  ).toBeVisible();
  await expect(
    page.locator(".drawing-toolbar").getByRole("button", { name: "图形与盈亏比" }),
  ).toBeVisible();
  await expect(
    page.locator(".drawing-toolbar").getByRole("button", { name: "文本框" }),
  ).toBeVisible();
  await expect(
    page.locator(".drawing-toolbar").getByRole("button", { name: "隐藏画线" }),
  ).toBeVisible();
  await expect(page.locator(".period-bar")).toContainText("季线");
  await page.getByRole("button", { name: "关闭图表" }).click();
  await expect(page.locator(".workbench-overlay")).toHaveCount(0);
  await expectCleanTerminal(page);
});

test("rectangle drawing previews hover, creates on two clicks, and drags endpoints together", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await page.goto("/market/");
  const firstRow = page.locator(".row-main").first();
  if ((await firstRow.count()) === 0) return;

  await firstRow.dblclick();
  const overlay = page.locator(".workbench-overlay");
  await expect(overlay).toBeVisible({ timeout: 20_000 });

  const clearResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await clearResponse;

  await chooseDrawingTool(page, "图形与盈亏比", "箱体线");
  const canvas = page.locator(".workbench-chart canvas").first();
  const chartRoot = page.locator(".workbench-chart .chart-root");
  const box = await canvas.boundingBox();
  expect(box).toBeTruthy();
  if (!box) return;

  const firstX = box.x + box.width * 0.28;
  const firstY = box.y + box.height * 0.34;
  const secondX = box.x + box.width * 0.68;
  const secondY = box.y + box.height * 0.46;

  await page.mouse.move(firstX, firstY);
  await expect(chartRoot).toHaveAttribute("data-rectangle-preview", "true");
  await page.mouse.click(firstX, firstY);
  await expect(chartRoot).toHaveAttribute("data-rectangle-anchor", "true");

  await page.mouse.move(secondX, secondY);
  await expect(chartRoot).toHaveAttribute("data-rectangle-preview", "true");
  const createRequest = page.waitForRequest(
    (request) =>
      request.method() === "PUT" &&
      request.url().includes("/drawings") &&
      request
        .postDataJSON()
        .items.some((item: { type?: string }) => item.type === "rectangle"),
  );
  await page.mouse.click(secondX, secondY);
  const createdDrawing = (await createRequest).postDataJSON().items[0] as {
    points: Array<{ time: string; price: number }>;
  };
  expect(createdDrawing.points).toHaveLength(2);
  await expect(page.locator(".drawing-popover")).toBeVisible();
  await expect(chartRoot).toHaveAttribute("data-rectangle-anchor", "false");

  const dragRequest = page.waitForRequest(
    (request) =>
      request.method() === "PUT" &&
      request.url().includes("/drawings") &&
      request
        .postDataJSON()
        .items.some((item: { type?: string }) => item.type === "rectangle"),
  );
  const middleX = (firstX + secondX) / 2;
  const middleY = (firstY + secondY) / 2;
  await page.mouse.move(middleX, middleY);
  await page.mouse.down();
  await page.mouse.move(middleX + 120, middleY, { steps: 8 });
  await page.mouse.up();
  const movedDrawing = (await dragRequest).postDataJSON().items[0] as {
    points: Array<{ time: string; price: number }>;
  };
  const createdTimes = createdDrawing.points.map((point) =>
    Date.parse(point.time),
  );
  const movedTimes = movedDrawing.points.map((point) => Date.parse(point.time));
  const shift = movedTimes[0] - createdTimes[0];
  expect(shift).not.toBe(0);
  expect(movedTimes[1] - createdTimes[1]).toBe(shift);

  await page.getByRole("button", { name: "线条颜色" }).click();
  await expect(
    page.locator(".drawing-color-popover:visible .preset-grid button"),
  ).toHaveCount(80);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "箱体填充颜色" }).click();
  await expect(
    page.locator(".drawing-color-popover:visible .preset-grid button"),
  ).toHaveCount(80);

  const cleanupResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await cleanupResponse;
});

test("brush drawing saves a simplified path and exposes the shared toolbar", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await page.goto("/market/");
  const firstRow = page.locator(".row-main").first();
  if ((await firstRow.count()) === 0) return;
  await firstRow.dblclick();
  await expect(page.locator(".workbench-overlay")).toBeVisible({
    timeout: 20_000,
  });
  const clearResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await clearResponse;
  await chooseDrawingTool(page, "笔刷与激光笔", "笔刷");
  const canvas = page.locator(".workbench-chart canvas").first();
  const box = await canvas.boundingBox();
  expect(box).toBeTruthy();
  if (!box) return;
  const request = page.waitForRequest(
    (candidate) =>
      candidate.method() === "PUT" &&
      candidate.url().includes("/drawings") &&
      candidate
        .postDataJSON()
        .items.some((item: { type?: string }) => item.type === "brush"),
  );
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.36);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.38, box.y + box.height * 0.42, {
    steps: 12,
  });
  await page.mouse.move(box.x + box.width * 0.54, box.y + box.height * 0.3, {
    steps: 12,
  });
  await page.mouse.up();
  const drawing = (await request)
    .postDataJSON()
    .items.find((item: { type?: string }) => item.type === "brush") as {
    points: unknown[];
  };
  expect(drawing.points.length).toBeGreaterThanOrEqual(2);
  expect(drawing.points.length).toBeLessThanOrEqual(2048);
  await expect(page.locator(".drawing-popover")).toBeVisible();
  await expect(page.getByRole("button", { name: "线条颜色" })).toBeVisible();
  await expect(page.getByRole("group", { name: "线宽" })).toBeVisible();
  await expect(page.getByRole("group", { name: "线型" })).toBeVisible();
  await page.getByRole("button", { name: "线条颜色" }).click();
  await expect(
    page.locator(".drawing-color-popover:visible .preset-grid button"),
  ).toHaveCount(80);
  const styleResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().includes("/drawings"),
  );
  const redPreset = page
    .locator(".drawing-color-popover:visible .preset-grid button")
    .nth(10);
  await redPreset.focus();
  await expect(redPreset).toBeFocused();
  await redPreset.press("Enter");
  await styleResponse;
  await expect(redPreset).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(() =>
      page.evaluate(() => {
        const value = localStorage.getItem("market-drawing-preferences-v1");
        return value ? JSON.parse(value).styles?.brush?.color : null;
      }),
    )
    .toBe("#F23645");
  const alphaSlider = page.getByRole("slider", { name: "不透明度" });
  const setAlpha = async (value: string, label: string): Promise<void> => {
    const alphaResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response.url().includes("/drawings"),
    );
    await alphaSlider.evaluate((element, nextValue) => {
      const input = element as HTMLInputElement;
      input.value = nextValue;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, value);
    await alphaResponse;
    await expect(
      page.locator(".drawing-color-popover:visible .alpha-value"),
    ).toHaveText(label);
  };
  await setAlpha("0", "0%");
  await setAlpha("0.2", "20%");
  await setAlpha("1", "100%");
  await page.getByRole("button", { name: "锁定" }).click();
  await expect(page.getByRole("button", { name: "解除锁定" })).toBeVisible();
  const brushPopover = page.locator(".drawing-popover");
  await brushPopover.getByRole("button", { name: "跨周期" }).click();
  await expect(
    brushPopover.getByRole("button", { name: "跨周期" }),
  ).toHaveClass(/active/);

  await chooseDrawingTool(page, "笔刷与激光笔", "笔刷");
  const cancelledRequest = page.waitForRequest(
    (candidate) =>
      candidate.method() === "PUT" &&
      candidate.url().includes("/drawings") &&
      candidate
        .postDataJSON()
        .items.some((item: { type?: string }) => item.type === "brush"),
  );
  await page.mouse.move(box.x + box.width * 0.32, box.y + box.height * 0.33);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.58, box.y + box.height * 0.4, {
    steps: 8,
  });
  await page
    .locator(".workbench-chart .chart-root")
    .dispatchEvent("pointercancel", { pointerId: 1, bubbles: true });
  await page.mouse.up();
  const cancelledDrawing = (await cancelledRequest)
    .postDataJSON()
    .items.filter((item: { type?: string }) => item.type === "brush")
    .at(-1) as { points: unknown[] };
  expect(cancelledDrawing.points.length).toBeGreaterThanOrEqual(2);
});

test("brush coalesces 10k pointer moves into one bounded save", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await page.goto("/market/");
  const firstRow = page.locator(".row-main").first();
  if ((await firstRow.count()) === 0) return;
  await firstRow.dblclick();
  await expect(page.locator(".workbench-overlay")).toBeVisible({
    timeout: 20_000,
  });
  const clearResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await clearResponse;
  await chooseDrawingTool(page, "笔刷与激光笔", "笔刷");
  const canvas = page.locator(".workbench-chart canvas").first();
  const box = await canvas.boundingBox();
  expect(box).toBeTruthy();
  if (!box) return;

  let brushSaveCount = 0;
  const countBrushSaves = (
    request: import("@playwright/test").Request,
  ): void => {
    if (request.method() === "PUT" && request.url().includes("/drawings"))
      brushSaveCount += 1;
  };
  page.on("request", countBrushSaves);
  const savedRequest = page.waitForRequest(
    (request) =>
      request.method() === "PUT" &&
      request.url().includes("/drawings") &&
      request
        .postDataJSON()
        .items.some((item: { type?: string }) => item.type === "brush"),
  );
  const elapsedMs = await canvas.evaluate((node, bounds) => {
    const canvasElement = node as HTMLCanvasElement;
    const start = performance.now();
    const dispatch = (
      type: "mousedown" | "mousemove" | "mouseup",
      x: number,
      y: number,
    ): void => {
      canvasElement.dispatchEvent(
        new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          button: 0,
          buttons: type === "mouseup" ? 0 : 1,
          clientX: bounds.left + x,
          clientY: bounds.top + y,
        }),
      );
    };
    dispatch("mousedown", bounds.width * 0.24, bounds.height * 0.34);
    for (let index = 0; index < 10_000; index += 1) {
      const progress = index / 9_999;
      dispatch(
        "mousemove",
        bounds.width * (0.24 + progress * 0.52),
        bounds.height * (0.36 + Math.sin(progress * Math.PI * 16) * 0.12),
      );
    }
    dispatch("mouseup", bounds.width * 0.76, bounds.height * 0.36);
    return performance.now() - start;
  }, box);
  const drawing = (await savedRequest)
    .postDataJSON()
    .items.find((item: { type?: string }) => item.type === "brush") as {
    points: unknown[];
  };
  page.off("request", countBrushSaves);
  expect(drawing.points.length).toBeGreaterThanOrEqual(2);
  expect(drawing.points.length).toBeLessThanOrEqual(2048);
  expect(brushSaveCount).toBe(1);
  expect(elapsedMs).toBeLessThan(5_000);
});

test("strategy page separates chart indicators from Python strategy functions", async ({
  page,
}) => {
  await page.goto("/strategy/");
  await expect(page.locator("h1.page-title")).toHaveText("策略");
  await expect(page.locator(".resource-tabs")).toContainText("指标");
  await expect(page.locator(".catalog-grid")).toContainText("平均真实波幅");
  await page.getByRole("tab", { name: "函数库" }).click();
  await expect(page.locator(".catalog-grid")).toContainText("江恩上升波动率");
  await expect(page.locator("body")).not.toContainText("时序动量");
  await page.getByRole("tab", { name: "我的策略" }).click();
  await expect(page.locator(".composite-strategy-manager")).toBeVisible();
  await page.locator(".page-actions").getByRole("button", { name: "新建策略" }).click();
  await expect(page.getByRole("dialog", { name: "新建组合策略" })).toContainText("Python");
  await page.getByRole("button", { name: "Close this dialog" }).click();
});

test("strategy indicators load into the current K-line and manage independent panes", async ({
  page,
}) => {
  test.setTimeout(60_000);
  let chartRequests = 0;
  let indicatorRequests = 0;
  const profileRanges: Array<{ start: number; end: number }> = [];
  const catalog = [
    {
      resourceKind: "indicator",
      id: "indicator.ma",
      version: 1,
      versionedId: "indicator.ma@1",
      name: "移动平均线",
      englishName: "Moving Average",
      displayName: "移动平均线",
      category: "trend",
      categoryLabel: "趋势",
      description: "收盘价的简单移动平均线。",
      definition: "收盘价的简单移动平均线。",
      placement: "overlay",
      supportedAssetTypes: ["STOCK"],
      dependencies: [{ id: "technical.sma", version: 1 }],
      parameters: [
        {
          name: "lookback",
          type: "integer",
          default: 20,
          minimum: 1,
          maximum: 500,
        },
      ],
      plots: [{ id: "ma", type: "line" }],
      origin: "builtin",
      status: "active",
    },
    {
      resourceKind: "indicator",
      id: "indicator.rsi",
      version: 1,
      versionedId: "indicator.rsi@1",
      name: "相对强弱指数",
      englishName: "RSI",
      displayName: "相对强弱指数",
      category: "momentum",
      categoryLabel: "动量",
      description: "价格动量。",
      definition: "价格动量。",
      placement: "pane",
      supportedAssetTypes: ["STOCK"],
      dependencies: [{ id: "technical.rsi", version: 1 }],
      parameters: [
        {
          name: "lookback",
          type: "integer",
          default: 14,
          minimum: 2,
          maximum: 200,
        },
      ],
      plots: [{ id: "rsi", type: "line" }],
      origin: "builtin",
      status: "active",
    },
    {
      resourceKind: "indicator",
      id: "indicator.chaikin_volatility",
      version: 1,
      versionedId: "indicator.chaikin_volatility@1",
      name: "蔡金波动率",
      englishName: "Chaikin Volatility",
      displayName: "蔡金波动率",
      category: "volatility",
      categoryLabel: "波动率",
      description: "EMA(H-L) 的变化率。",
      definition: "EMA(H-L) 的变化率。",
      mathFormula: "100 × (EMA(H-L)[t] / EMA(H-L)[t-m] − 1)",
      requiredFields: ["high", "low"],
      warmupBars: "emaLookback + changeLookback",
      placement: "pane",
      supportedAssetTypes: ["STOCK"],
      dependencies: [{ id: "volatility.chaikin_volatility", version: 1 }],
      parameters: [
        { name: "emaLookback", type: "integer", default: 10, minimum: 2, maximum: 200 },
        { name: "changeLookback", type: "integer", default: 10, minimum: 1, maximum: 200 },
      ],
      plots: [{ id: "value", type: "line" }],
      origin: "builtin",
      status: "active",
    },
    {
      resourceKind: "indicator",
      id: "indicator.volume_profile",
      version: 1,
      versionedId: "indicator.volume_profile@1",
      name: "成交量分布",
      englishName: "Volume Profile",
      displayName: "成交量分布",
      category: "advanced_chart",
      categoryLabel: "高级图表",
      description: "可见区间的 HLC3 成交量分桶。",
      definition: "可见区间的 HLC3 成交量分桶。",
      placement: "overlay",
      supportedAssetTypes: ["STOCK"],
      dependencies: [{ id: "market.volume_profile", version: 1 }],
      parameters: [
        { name: "bins", type: "integer", default: 4, minimum: 4, maximum: 200 },
        { name: "valueAreaPercent", type: "number", default: 70, minimum: 1, maximum: 100 },
      ],
      plots: [{ id: "profile", type: "profile" }],
      origin: "builtin",
      status: "active",
    },
    {
      resourceKind: "indicator",
      id: "indicator.vix",
      version: 1,
      versionedId: "indicator.vix@1",
      name: "VIX 恐慌指数",
      englishName: "CBOE Volatility Index",
      displayName: "VIX 恐慌指数",
      category: "external_market",
      categoryLabel: "外部市场",
      description: "本地标准 VIX 序列按交易日对齐。",
      definition: "本地标准 VIX 序列按交易日对齐。",
      placement: "pane",
      supportedAssetTypes: ["STOCK"],
      dependencies: [],
      parameters: [],
      plots: [{ id: "vix", type: "line" }],
      origin: "builtin",
      status: "active",
    },
    {
      resourceKind: "indicator",
      id: "indicator.futures_only_fixture",
      version: 1,
      versionedId: "indicator.futures_only_fixture@1",
      name: "期货专用样例",
      englishName: "Futures-only fixture",
      displayName: "期货专用样例",
      category: "trend",
      categoryLabel: "趋势",
      description: "仅用于资产过滤回归。",
      definition: "仅用于资产过滤回归。",
      placement: "pane",
      supportedAssetTypes: ["FUTURE"],
      dependencies: [],
      parameters: [],
      plots: [{ id: "value", type: "line" }],
      origin: "builtin",
      status: "active",
    },
  ];
  const bars = Array.from({ length: 40 }, (_item, index) => {
    const day = new Date(Date.UTC(2026, 5, index + 1))
      .toISOString()
      .slice(0, 10);
    return {
      barOpenTime: `${day}T09:30:00+08:00`,
      tradingDate: day,
      open: 100 + index,
      high: 102 + index,
      low: 99 + index,
      close: 101 + index,
      volume: 1000 + index,
      amount: 100000 + index,
      turnoverRate: 1 + index / 100,
    };
  });
  await page.addInitScript(() =>
    localStorage.setItem("market-all-view", "card"),
  );
  await page.route("**/api/strategy/indicators**", async (route) =>
    route.fulfill({ json: { items: catalog, total: catalog.length } }),
  );
  await page.route("**/api/strategy/definitions**", async (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/strategy/matches", async (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/market/instruments?**", async (route) =>
    route.fulfill({
      json: {
        items: [
          {
            instrumentId: "CN.SSE.STOCK.600000",
            symbol: "600000",
            name: "浦发银行",
            market: "CN",
            assetType: "STOCK",
            period: "1d",
            latestPrice: 12.3,
          },
        ],
        total: 1,
        page: 1,
        pageSize: 10,
        dataVersion: "e2e-indicator-v1",
      },
    }),
  );
  await page.route(
    "**/api/market/instruments/CN.SSE.STOCK.600000/chart**",
    async (route) => {
      chartRequests += 1;
      await route.fulfill({
        json: {
          instrumentId: "CN.SSE.STOCK.600000",
          period: "1d",
          availablePeriods: ["1d"],
          start: 0,
          size: bars.length,
          total: bars.length,
          before: bars[0].barOpenTime,
          hasMore: false,
          earliestBarAt: bars[0].barOpenTime,
          latestBarAt: bars.at(-1)?.barOpenTime,
          bars,
          series: {},
          drawings: [],
          dataVersion: "e2e-indicator-v1",
        },
      });
    },
  );
  await page.route(
    "**/api/market/instruments/CN.SSE.STOCK.600000/drawings**",
    async (route) => route.fulfill({ json: { items: [] } }),
  );
  await page.route(
    "**/api/market/instruments/CN.SSE.STOCK.600000/indicator-series",
    async (route) => {
      indicatorRequests += 1;
      const request = route.request().postDataJSON() as {
        size: number;
        instances: Array<Record<string, unknown>>;
      };
      await route.fulfill({
        json: {
          instances: request.instances.map((instance) => {
            const definition = catalog.find(
              (item) => item.id === instance.definitionId,
            )!;
            if (definition.id === "indicator.volume_profile") {
              const start = Number(instance.rangeStart ?? 0);
              const end = Number(instance.rangeEnd ?? request.size - 1);
              profileRanges.push({ start, end });
              return {
                ...instance,
                displayName: definition.name,
                englishName: definition.englishName,
                plots: definition.plots,
                status: "ready",
                series: {},
                profile: {
                  algorithmVersion: "volume_profile_typical_price_v1",
                  range: { startIndex: start, endIndex: end, inputBarCount: end - start + 1, rangeSemantics: "inclusive_visible_indices_in_requested_window" },
                  requestedBinCount: 4,
                  effectiveBinCount: 4,
                  priceRepresentative: "HLC3",
                  allocation: "full_bar_volume_to_hlc3_bucket",
                  valueAreaPercent: 70,
                  totalVolume: 1000,
                  pocBucketIndex: 2,
                  valueAreaVolume: 800,
                  buckets: [
                    { bucketLow: 99, bucketHigh: 109, volume: 100, share: 10, isPoc: false, isValueArea: false },
                    { bucketLow: 109, bucketHigh: 119, volume: 200, share: 20, isPoc: false, isValueArea: true },
                    { bucketLow: 119, bucketHigh: 129, volume: 400, share: 40, isPoc: true, isValueArea: true },
                    { bucketLow: 129, bucketHigh: 139, volume: 300, share: 30, isPoc: false, isValueArea: true },
                  ],
                },
              };
            }
            if (definition.id === "indicator.vix") {
              return {
                ...instance,
                displayName: definition.name,
                englishName: definition.englishName,
                plots: definition.plots,
                status: "ready",
                series: {
                  vix: Array.from({ length: request.size }, (_value, index) =>
                    index % 5 === 0 ? null : 18 + index / 10,
                  ),
                },
                external: {
                  externalInstrumentId: "US.CBOE.INDEX.VIX",
                  source: "fixture: verified VIX standard series",
                  asOfDate: "2026-07-10",
                  sourcePointCount: request.size - 8,
                  status: "ready",
                  reason: null,
                  coverage: {
                    inputBarCount: request.size,
                    matchedBarCount: request.size - 8,
                    sourcePointCount: request.size - 8,
                    firstInputDate: "2026-06-01",
                    lastInputDate: "2026-07-10",
                  },
                  points: [],
                },
              };
            }
            const plot = definition.id === "indicator.ma" ? "ma" : definition.id === "indicator.rsi" ? "rsi" : "value";
            return {
              ...instance,
              displayName: definition.name,
              englishName: definition.englishName,
              plots: definition.plots,
              status: "ready",
              series: {
                [plot]: Array.from({ length: request.size }, (_value, index) =>
                  index < 2 ? null : 100 + index,
                ),
              },
            };
          }),
        },
      });
    },
  );

  await page.goto("/strategy/");
  await page
    .locator(".catalog-grid article")
    .filter({ hasText: "移动平均线" })
    .click();
  await page.getByRole("button", { name: "添加到当前图表" }).click();
  await expect(page).toHaveURL(/\/market\//);
  await expect(page.locator(".workbench-overlay")).toBeVisible({
    timeout: 20_000,
  });
  const chartRoot = page.locator(".workbench-chart .chart-root");
  await expect(chartRoot).toHaveAttribute("data-indicator-count", "1");
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "0");

  await page.getByRole("button", { name: /^指标/ }).click();
  const manager = page.getByRole("region", { name: "图表指标管理" });
  const indicatorDialog = page.getByRole("dialog", { name: "指标", exact: true });
  const legend = page.getByLabel("已添加指标");
  const addFromLibrary = async (name: string) => {
    await page.getByRole("button", { name: /^指标/ }).click();
    await manager.locator(".indicator-catalog-name").filter({ hasText: name }).click();
  };
  await expect(manager.locator(".indicator-catalog-name").filter({ hasText: "期货专用样例" })).toHaveCount(0);
  await manager.locator(".indicator-catalog-name").filter({ hasText: "相对强弱指数" }).click();
  await expect(chartRoot).toHaveAttribute("data-indicator-count", "2");
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "1");

  const rsi = page
    .locator(".indicator-instance")
    .filter({ hasText: "相对强弱指数" });
  await legend.getByRole("button", { name: "设置相对强弱指数" }).click();
  const requestsBeforeParameterChange = indicatorRequests;
  const chartsBeforeParameterChange = chartRequests;
  await rsi.locator("input[role=spinbutton]").first().fill("10");
  await rsi.locator("input[role=spinbutton]").first().press("Enter");
  await expect
    .poll(() => indicatorRequests)
    .toBeGreaterThan(requestsBeforeParameterChange);
  expect(chartRequests).toBe(chartsBeforeParameterChange);

  await rsi.getByRole("button", { name: "隐藏相对强弱指数" }).click();
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "0");
  await rsi.getByRole("button", { name: "显示相对强弱指数" }).click();
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "1");
  await indicatorDialog.getByRole("button", { name: "Close this dialog" }).click();
  await addFromLibrary("蔡金波动率");
  await expect(chartRoot).toHaveAttribute("data-indicator-count", "3");
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "2");
  const chaikin = page
    .locator(".indicator-instance")
    .filter({ hasText: "蔡金波动率" });
  await legend.getByRole("button", { name: "设置蔡金波动率" }).click();
  const requestsBeforeAdvancedParameterChange = indicatorRequests;
  await chaikin.locator("input[role=spinbutton]").first().fill("5");
  await chaikin.locator("input[role=spinbutton]").first().press("Enter");
  await expect
    .poll(() => indicatorRequests)
    .toBeGreaterThan(requestsBeforeAdvancedParameterChange);
  expect(chartRequests).toBe(chartsBeforeParameterChange);
  await chaikin.getByRole("button", { name: "删除蔡金波动率" }).click();
  await expect(chartRoot).toHaveAttribute("data-indicator-count", "2");
  await expect(chartRoot).toHaveAttribute("data-indicator-pane-count", "1");
  await indicatorDialog.getByRole("button", { name: "Close this dialog" }).click();
  await addFromLibrary("成交量分布");
  await expect(chartRoot).toHaveAttribute("data-volume-profile-bucket-count", "4");
  const profileRequestsBeforeZoom = profileRanges.length;
  await page.locator(".workbench-chart canvas").first().hover();
  await page.mouse.wheel(0, -240);
  await expect.poll(() => profileRanges.length).toBeGreaterThan(profileRequestsBeforeZoom);
  expect(profileRanges.at(-1)!.end - profileRanges.at(-1)!.start).toBeLessThan(bars.length - 1);
  await legend
    .getByRole("button", { name: "删除成交量分布" })
    .click();
  await expect(chartRoot).toHaveAttribute("data-volume-profile-bucket-count", "0");
  await addFromLibrary("VIX 恐慌指数");
  await legend.getByRole("button", { name: "设置VIX 恐慌指数" }).click();
  const vix = page
    .locator(".indicator-instance")
    .filter({ hasText: "VIX 恐慌指数" });
  await expect(vix).toContainText("来源：fixture: verified VIX standard series");
  await expect(vix).toContainText("数据截至：2026-07-10");
  await expect(vix).toContainText("对齐 32/40 个交易日");
  await vix.getByRole("button", { name: "删除VIX 恐慌指数" }).click();
  await indicatorDialog.getByRole("button", { name: "Close this dialog" }).click();
  await legend.getByRole("button", { name: "删除相对强弱指数" }).click();
  await expect(chartRoot).toHaveAttribute("data-indicator-count", "1");
  await expect
    .poll(
      async () =>
        JSON.parse(
          await page.evaluate(
            () =>
              localStorage.getItem(
                "marketlistener.chartIndicatorInstances.v1",
              ) || "[]",
          ),
        ).length,
    )
    .toBe(1);
});

test("account analysis creates an account and exposes account-entry tools", async ({
  page,
}) => {
  await page.goto("/stats/");
  await expect(page.locator("h1.page-title")).toHaveText("账户分析");
  await page.getByRole("button", { name: "新建账户" }).click();
  await page
    .getByRole("textbox", { name: "账户名称" })
    .fill(`E2E 分析账户 ${Date.now()}`);
  await page.getByRole("textbox", { name: "开始日期" }).fill("2026-08-01");
  await page.getByRole("spinbutton", { name: "期初资金" }).fill("100000");
  await page.getByRole("button", { name: "创建" }).click();
  await expect(page.locator(".metric-grid")).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole("heading", { name: "每日账户快照" }),
  ).toBeVisible();
  await expect(page.locator(".csv-file")).toBeVisible();
});

test("customer product redirects the retired source-management bookmark", async ({ page }) => {
  await page.goto("/data-sources/");
  await expect(page).toHaveURL(/\/market\/all\/(?:\?.*)?$/);
  expect((await page.request.get("/api/data-sources/providers")).status()).toBe(403);
  expect((await page.request.put("/api/data-sources", { data: { preferences: {} } })).status()).toBe(403);
  await expect(page.getByRole("button", { name: "保存主备配置" })).toHaveCount(0);
});
