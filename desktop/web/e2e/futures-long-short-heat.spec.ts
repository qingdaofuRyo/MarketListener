import { expect, test, type Page } from "@playwright/test";

const heatPayload = {
  available: true,
  config: {
    defaultUserWeight: { breadthWeight: 0.4, fundWeight: 0.6 },
    userWeight: { min: 0, max: 1, step: 0.05 },
    score: { min: -100, max: 100 },
    stateBands: [
      { min: -100, max: -80, label: "极度偏空" },
      { min: -80, max: -60, label: "明显偏空" },
      { min: -60, max: -20, label: "偏空" },
      { min: -20, max: 20, label: "多空均衡" },
      { min: 20, max: 60, label: "偏多" },
      { min: 60, max: 80, label: "明显偏多" },
      { min: 80, max: 100, label: "极度偏多" },
    ],
    divergenceThreshold: 10,
    fundUnit: "亿元",
    lookbackTradingDays: 10,
    halfLife: 3,
  },
  points: [
    {
      tradeDate: "2026-08-24", upVarietyCount: 42, downVarietyCount: 43, flatVarietyCount: 2,
      upFund: 1200, downFund: 1300, flatFund: 30, breadthScore10: -10, fundScore10: -20,
      divergence: 10, isWarmup: false, dataQualityStatus: "PASS", coverage: { variety: 0.98, fund: 0.96 },
    },
    {
      tradeDate: "2026-08-25", upVarietyCount: 51, downVarietyCount: 34, flatVarietyCount: 2,
      upFund: 1600, downFund: 1000, flatFund: 20, breadthScore10: 10, fundScore10: 40,
      divergence: -30, isWarmup: false, dataQualityStatus: "PASS", coverage: { variety: 0.98, fund: 0.96 },
    },
    {
      tradeDate: "2026-08-26", upVarietyCount: 61, downVarietyCount: 28, flatVarietyCount: 1,
      upFund: 2100, downFund: 900, flatFund: 15, breadthScore10: 20, fundScore10: 70,
      divergence: -50, isWarmup: false, dataQualityStatus: "PASS", coverage: { variety: 0.99, fund: 0.97 },
    },
  ],
  latest: {
    tradeDate: "2026-08-26", upVarietyCount: 61, downVarietyCount: 28, flatVarietyCount: 1,
    upFund: 2100, downFund: 900, flatFund: 15, breadthScore10: 20, fundScore10: 70,
    divergence: -50, isWarmup: false, dataQualityStatus: "PASS", coverage: { variety: 0.99, fund: 0.97 },
  },
  generatedAt: "2026-08-26T18:00:00+08:00",
  source: "fixture",
  formulaVersion: "long-short-heat-v1",
  limitations: [],
};

const structurePayload = {
  available: true,
  chartId: "product-open-interest",
  metric: "openInterest",
  direction: "gross",
  unit: "contracts",
  baselineDay: "2026-08-26",
  baselineVersion: "futures-structure-oi-v1:2026-08-26",
  threshold: 0.015,
  stackOrder: ["DCE.JM", "OTHER"],
  primaryMembers: [{ memberKey: "DCE.JM", memberName: "焦煤" }],
  otherMembers: [{ memberKey: "CZCE.AP", memberName: "苹果" }],
  unclassifiedMembers: [{ memberKey: "DCE.I", memberName: "铁矿石" }],
  dates: ["2026-08-25", "2026-08-26"],
  series: [
    { memberKey: "DCE.JM", memberName: "焦煤", values: [100, 110] },
    { memberKey: "OTHER", memberName: "其他", memberCount: 1, values: [5, 6] },
  ],
  totals: [105, 120],
  unclassifiedTotals: [0, 4],
  coverage: [
    { tradeDate: "2026-08-25", inputRowCount: 3, missingRowCount: 0, dataQualityStatus: "PASS" },
    { tradeDate: "2026-08-26", inputRowCount: 3, missingRowCount: 0, dataQualityStatus: "PASS" },
  ],
  formulaVersion: "futures-structure-oi-v1",
  priceBasis: null,
  source: "fixture",
  updatedAt: "2026-08-26T18:00:00+08:00",
  limitations: ["固定顺序测试说明"],
};

const memberStructurePayload = {
  ...structurePayload,
  chartId: "member-open-interest",
  direction: "gross",
  baselineVersion: "futures-member-position-v1:2026-08-26",
  primaryMembers: [{ memberKey: "SHFE.会员甲", memberName: "SHFE · 会员甲" }],
  otherMembers: [{ memberKey: "DCE.会员乙", memberName: "DCE · 会员乙" }],
  series: [
    { memberKey: "SHFE.会员甲", memberName: "SHFE · 会员甲", values: [100, 110] },
    { memberKey: "OTHER", memberName: "其他", memberCount: 1, values: [5, 6] },
  ],
  formulaVersion: "futures-member-position-v1",
  limitations: ["仅统计交易所实际公布的会员方向排名"],
};

const memberPositionsPayload = {
  available: true,
  tradingDay: "2026-08-26",
  filters: { exchange: null, contractCode: null, productCode: null, commodityOnly: true },
  contracts: [{ exchange: "SHFE", productCode: "RB", contractCode: "RB2610" }],
  rows: [],
  coverage: {
    publishedDirectionRankCount: 40, memberCount: 20, exchangeCount: 1,
    exchanges: ["SHFE"], missingExchanges: ["DCE"], isComplete: false,
  },
  limitations: ["仅统计交易所实际公布的会员方向排名", "请选择交易所、品种或具体月份合约后再读取席位明细。"],
};

const contractOptionsPayload = {
  available: true,
  tradingDay: null,
  filters: { exchange: null, product: null, seriesKind: null },
  items: [
    { instrumentId: "CN.SHFE.FUTURE.RB2610.CONTRACT", exchange: "SHFE", productCode: "RB", contractCode: "RB2610", name: "螺纹钢2610", seriesKind: "CONTRACT", lastTradingDay: "2026-08-27", actualSource: "fixture", qualityStatus: "PASS" },
    { instrumentId: "CN.SHFE.FUTURE.RB2701.CONTRACT", exchange: "SHFE", productCode: "RB", contractCode: "RB2701", name: "螺纹钢2701", seriesKind: "CONTRACT", lastTradingDay: "2026-08-27", actualSource: "fixture", qualityStatus: "PASS" },
  ],
  limitations: ["默认仅列出商品期货。"],
};

const contractSeriesPayload = {
  available: true,
  instrument: contractOptionsPayload.items[0],
  seriesKind: "CONTRACT",
  priceBasis: null,
  units: { price: "source-price-unit", openInterest: "contracts", notional: "CNY", basis: null },
  availability: {
    price: { available: true, render: "candlestick", reason: null },
    openInterest: { available: true, render: "line", reason: null },
    notional: { available: false, render: "line", reason: "等待锁定 priceBasis" },
    basis: { available: false, render: "line", reason: "等待现货来源" },
  },
  points: [
    { tradingDay: "2026-08-26", open: 3200, high: 3230, low: 3180, close: 3210, settlement: 3208, openInterest: 1234, notionalRmb: null, basisRmb: null, basisPercent: null, unavailable: { notional: "等待锁定 priceBasis", basis: "等待现货来源" } },
    { tradingDay: "2026-08-27", open: 3210, high: 3250, low: 3200, close: 3230, settlement: 3228, openInterest: 1300, notionalRmb: null, basisRmb: null, basisPercent: null, unavailable: { notional: "等待锁定 priceBasis", basis: "等待现货来源" } },
  ],
  source: "fixture",
  updatedAt: "2026-08-27T15:00:00+08:00",
  limitations: ["价格序列保留本地来源的 OHLC；持仓量为交易所公布的单边持仓量。", "等待锁定 priceBasis", "等待现货来源"],
};

async function openFutures(
  page: Page,
  payload: Record<string, unknown> = heatPayload,
  requests?: { count: number },
  options: { productStructure?: Record<string, unknown> } = {},
): Promise<void> {
  const productStructure = options.productStructure ?? structurePayload;
  await page.route("**/api/futures/heat", route => {
    if (requests) requests.count += 1;
    return route.fulfill({ json: payload });
  });
  await page.route("**/api/futures/structures/product-open-interest**", route => {
    const level = new URL(route.request().url()).searchParams.get("level");
    return route.fulfill({ json: level === "other" ? {
      ...productStructure,
      series: [{ memberKey: "CZCE.AP", memberName: "苹果", values: [5, 6] }],
    } : productStructure });
  });
  await page.route("**/api/futures/structures/member-open-interest**", route => {
    const level = new URL(route.request().url()).searchParams.get("level");
    const direction = new URL(route.request().url()).searchParams.get("direction") || "gross";
    return route.fulfill({ json: {
      ...memberStructurePayload,
      direction,
      series: level === "other"
        ? [{ memberKey: "DCE.会员乙", memberName: "DCE · 会员乙", values: [5, 6] }]
        : memberStructurePayload.series,
    } });
  });
  await page.route("**/api/futures/member-positions**", route => {
    const url = new URL(route.request().url());
    const contractCode = url.searchParams.get("contract_code");
    return route.fulfill({ json: contractCode ? {
      ...memberPositionsPayload,
      filters: { ...memberPositionsPayload.filters, contractCode },
      rows: [{
        exchange: "SHFE", contractCode, productCode: "RB", memberKey: "会员甲", memberName: "会员甲",
        longPosition: 120, longPositionChange: 4, longRank: 1,
        shortPosition: null, shortPositionChange: null, shortRank: null,
        netPosition: null, netLongPosition: null, netShortPosition: null,
        sources: ["fixture"],
      }],
    } : memberPositionsPayload });
  });
  await page.route("**/api/futures/contracts", route => route.fulfill({ json: contractOptionsPayload }));
  await page.route("**/api/futures/contract-series**", route => route.fulfill({ json: contractSeriesPayload }));
  await page.goto("/futures/");
  await expect(page.getByRole("heading", { name: "国内期货数据" })).toBeVisible();
  await expect(page.locator('[data-test="futures-long-short-heat"]')).toBeVisible();
}

async function setFundWeight(page: Page, percent: number): Promise<void> {
  const slider = page.getByRole("slider", { name: "资金热度权重" });
  await slider.focus();
  await slider.press("Home");
  for (let value = 0; value < percent; value += 5) await slider.press("ArrowRight");
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const initialized = "marketlistener.e2e.longShortHeat.initialized";
    if (sessionStorage.getItem(initialized) !== "1") {
      localStorage.removeItem("marketlistener.futures.longShortHeat.fundWeight");
      sessionStorage.setItem(initialized, "1");
    }
  });
});

test("three gauges and history use the same scores and persisted user weight", async ({ page }) => {
  const requests = { count: 0 };
  await openFutures(page, heatPayload, requests);

  const gauges = page.locator('[data-test="heat-gauge"]');
  await expect(gauges).toHaveCount(3);
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "50");
  await expect(page.locator('[data-test-kind="breadth"]')).toHaveAttribute("data-current-value", "20");
  await expect(page.locator('[data-test-kind="fund"]')).toHaveAttribute("data-current-value", "70");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-value", "50");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-breadth-value", "20");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-fund-value", "70");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-y-min", "-100");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-y-max", "100");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-neutral-line", "0");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-legend-count", "3");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-total-checksum", "190.000000");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-breadth-checksum", "70.000000");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-fund-checksum", "270.000000");
  await expect(page.locator('[data-test="heat-range-selector"] .el-radio-button')).toHaveCount(7);

  await setFundWeight(page, 30);
  await expect(page.locator('[data-test="fund-weight"]')).toHaveText("30%");
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "35");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-value", "35");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-total-checksum", "130.000000");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-breadth-checksum", "70.000000");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-fund-checksum", "270.000000");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("marketlistener.futures.longShortHeat.fundWeight"))).toBe("0.3");
  expect(requests.count).toBe(1);

  await page.reload();
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "35");
  await page.goto("/");
  await page.goto("/futures/");
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "35");
  await page.locator('[data-test="heat-weight-reset"]').click();
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "50");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-value", "50");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("marketlistener.futures.longShortHeat.fundWeight"))).toBe("0.6");
});

test("warmup and low fund coverage stay explicit instead of fabricating total heat", async ({ page }) => {
  const point = {
    ...heatPayload.latest,
    fundScore10: null,
    divergence: null,
    isWarmup: true,
    dataQualityStatus: "PARTIAL",
    coverage: { variety: 0.92, fund: 0.35 },
  };
  await openFutures(page, { ...heatPayload, points: [point], latest: point });
  await expect(page.getByText("预热期", { exact: true })).toBeVisible();
  await expect(page.locator('[data-test-kind="breadth"]')).toHaveAttribute("data-current-value", "20");
  await expect(page.locator('[data-test-kind="fund"]')).toHaveAttribute("data-current-value", "");
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "");
  await expect(page.getByText(/资金 35\.0%/)).toBeVisible();
});

test("heat charts remain visible across dark and light semantic palettes", async ({ page }) => {
  await openFutures(page);
  await page.locator('[data-test="theme-toggle"]').click();
  await page.locator('[data-test="theme-option-dark"]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator('[data-test="heat-gauge"] canvas')).toHaveCount(3);
  await expect(page.locator('[data-test="heat-history-chart"] canvas')).toBeVisible();
  await page.locator('[data-test="theme-toggle"]').click();
  await page.locator('[data-test="theme-option-light"]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator('[data-test="heat-history-chart"] canvas')).toBeVisible();
});

test("five-thousand-point history stays interactive without rescanning the API", async ({ page }) => {
  test.slow();
  const points = Array.from({ length: 5_000 }, (_, index) => {
    const date = new Date(Date.UTC(2007, 0, 2 + index)).toISOString().slice(0, 10);
    const breadth = (index % 161) - 80;
    const fund = ((index * 3) % 181) - 90;
    return {
      tradeDate: date,
      upVarietyCount: 40,
      downVarietyCount: 30,
      flatVarietyCount: 2,
      upFund: 2_000,
      downFund: 1_500,
      flatFund: 20,
      breadthScore10: breadth,
      fundScore10: fund,
      divergence: breadth - fund,
      isWarmup: false,
      dataQualityStatus: "PASS",
      coverage: { variety: 1, fund: 1 },
    };
  });
  const payload = { ...heatPayload, points, latest: points.at(-1) };
  const requests = { count: 0 };
  const startedAt = Date.now();
  await openFutures(page, payload, requests);
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-series-count", "3");
  await page.locator('[data-test="heat-range-selector"] .el-radio-button').filter({ hasText: "全部" }).click();
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-point-count", "5000");
  await setFundWeight(page, 45);
  await expect(page.locator('[data-test="fund-weight"]')).toHaveText("45%");
  expect(requests.count).toBe(1);
  expect(Date.now() - startedAt).toBeLessThan(10_000);
});

test("heat dashboard follows desktop, two-plus-one and mobile layouts", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openFutures(page);
  const gauges = page.locator('[data-test="heat-gauge"]');
  const desktopBoxes = await gauges.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
  expect(desktopBoxes[0].y).toBe(desktopBoxes[1].y);
  expect(desktopBoxes[1].y).toBe(desktopBoxes[2].y);

  await page.setViewportSize({ width: 900, height: 900 });
  const tabletBoxes = await gauges.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
  expect(tabletBoxes[0].y).toBe(tabletBoxes[1].y);
  expect(tabletBoxes[2].y).toBeGreaterThan(tabletBoxes[1].y);

  await page.setViewportSize({ width: 390, height: 844 });
  const mobileBoxes = await gauges.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
  expect(mobileBoxes[1].y).toBeGreaterThan(mobileBoxes[0].y);
  expect(mobileBoxes[2].y).toBeGreaterThan(mobileBoxes[1].y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("empty API state never substitutes demonstration data", async ({ page }) => {
  await page.route("**/api/futures/heat", route => route.fulfill({ json: {
    available: false,
    config: { defaultUserWeight: { breadthWeight: 0.4, fundWeight: 0.6 } },
    points: [],
    limitations: ["Gold 派生数据尚未生成"],
  } }));
  await page.goto("/futures/");
  await expect(page.locator('[data-test="futures-heat-empty"]')).toContainText("暂无可用数据");
  await expect(page.locator('[data-test="heat-gauge"]')).toHaveCount(0);
});

test("product open-interest structure preserves its fixed baseline and drills into other", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="product-open-interest-structure"]');
  await expect(panel).toContainText("基准日：2026-08-26");
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await expect(panel).toContainText("未分类新品种：1");
  await panel.getByText("全部", { exact: true }).click();
  await panel.getByRole("button", { name: /查看其他/ }).click();
  await expect(panel.getByRole("button", { name: "返回主图" })).toBeVisible();
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
});

test("product structure renders one hundred real members without API truncation", async ({ page }) => {
  test.slow();
  const dates = Array.from({ length: 500 }, (_, index) => {
    const date = new Date(Date.UTC(2024, 0, 2 + index));
    return date.toISOString().slice(0, 10);
  });
  const series = Array.from({ length: 100 }, (_, member) => ({
    memberKey: `DCE.P${String(member).padStart(3, "0")}`,
    memberName: `合成品种 ${member + 1}`,
    values: dates.map((_, day) => 10_000 + member * 100 + (day % 37)),
  }));
  const wideStructure = {
    ...structurePayload,
    dates,
    stackOrder: series.map(item => item.memberKey),
    primaryMembers: series.map(item => ({ memberKey: item.memberKey, memberName: item.memberName })),
    otherMembers: [],
    unclassifiedMembers: [],
    series,
    totals: dates.map((_, day) => series.reduce((sum, item) => sum + item.values[day], 0)),
    unclassifiedTotals: dates.map(() => 0),
    coverage: dates.map(tradeDate => ({ tradeDate, inputRowCount: 100, missingRowCount: 0, dataQualityStatus: "PASS" })),
  };
  const startedAt = Date.now();
  await openFutures(page, heatPayload, undefined, { productStructure: wideStructure });
  const panel = page.locator('[data-test="product-open-interest-structure"]');
  await expect(panel.locator('[data-test="futures-structure-chart"] canvas')).toBeVisible();
  expect(Date.now() - startedAt).toBeLessThan(15_000);
  const chartCanvas = panel.locator('[data-test="futures-structure-chart"] canvas').first();
  await chartCanvas.hover();
  await page.mouse.wheel(0, 240);
  await expect(chartCanvas).toBeVisible();
});

test("member open-interest structure keeps published-ranking coverage and direction controls", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="member-open-interest-structure"]');
  await expect(panel).toContainText("席位持仓分布");
  await expect(panel).toContainText("基准日：2026-08-26");
  await expect(panel).toContainText("仅统计交易所实际公布的会员方向排名");
  await panel.getByText("多头净持仓", { exact: true }).click();
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await panel.getByRole("button", { name: /查看其他/ }).click();
  await expect(panel.getByRole("button", { name: "返回主图" })).toBeVisible();
});

test("contract member rankings load only after a concrete monthly contract is selected", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="futures-member-positions"]');
  await expect(panel).toContainText("请选择月份合约");
  await panel.getByText("月份合约", { exact: true }).click();
  await page.getByText("SHFE · RB2610", { exact: true }).last().click();
  await expect(panel).toContainText("会员甲");
  await expect(panel).toContainText("120（1）");
  await expect(panel).toContainText("—（—）");
});

test("contract chart keeps K-line and open interest while blocked metrics stay explicit", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="futures-contract-series"]');
  await expect(panel).toContainText("请选择交易所、品种和月份合约");
  await panel.getByRole("combobox", { name: "商品合约交易所筛选" }).click({ force: true });
  await page.locator(".el-select-dropdown:visible").getByText("SHFE", { exact: true }).click();
  await panel.getByRole("combobox", { name: "商品合约品种筛选" }).click({ force: true });
  await page.locator(".el-select-dropdown:visible").getByText("RB", { exact: true }).click();
  await panel.getByRole("combobox", { name: "商品合约月份筛选" }).click({ force: true });
  await page.locator(".el-select-dropdown:visible").getByText(/RB2610 · 2026-08-27/, { exact: true }).click();
  await expect(panel.locator('[data-test="futures-contract-chart"]')).toBeVisible();
  await expect(panel).toContainText("名义规模价格基准：尚未锁定");
  await expect(panel).toContainText("等待锁定 priceBasis");
  await expect(panel).toContainText("等待现货来源");
});
