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
  baselineVersion: "futures-structure-oi-official-v1:2026-08-26",
  threshold: null,
  rankLimit: 40,
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
  formulaVersion: "futures-structure-oi-official-v1",
  priceBasis: null,
  source: "fixture",
  updatedAt: "2026-08-26T18:00:00+08:00",
  limitations: ["固定顺序测试说明"],
};

const notionalStructurePayload = {
  ...structurePayload,
  chartId: "product-notional",
  metric: "notionalOpenInterest",
  unit: "CNY",
  baselineVersion: "futures-structure-notional-official-v5:2026-08-26",
  totals: [1_050_000, 1_200_000],
  unclassifiedTotals: [0, 40_000],
  formulaVersion: "futures-structure-notional-official-v5",
  priceBasis: "SETTLEMENT",
  limitations: ["结算价名义持仓规模测试说明"],
};

const volumeStructurePayload = {
  ...structurePayload,
  chartId: "product-volume",
  metric: "tradedVolume",
  formulaVersion: "futures-structure-volume-official-v1",
};

const memberPositionsPayload = {
  available: true,
  tradingDay: "2026-08-26",
  latestPublishedDay: "2026-08-26",
  filters: { exchange: null, contractCode: null, productCode: null, memberKey: null, commodityOnly: true },
  contracts: [
    { exchange: "SHFE", productCode: "RB", contractCode: "RB2610" },
    { exchange: "SHFE", productCode: "HC", contractCode: "HC2610" },
  ],
  members: [{ exchange: "SHFE", memberKey: "会员甲", memberName: "会员甲" }],
  rows: [],
  coverage: {
    publishedDirectionRankCount: 40, memberCount: 20, exchangeCount: 1,
    exchanges: ["SHFE"], missingExchanges: ["DCE"], isComplete: false, dataQualityStatus: "PARTIAL",
    exchangeCoverage: [
      { exchange: "SHFE", status: "PASS", contractCount: 1, recordCount: 40, sources: ["fixture-shfe"], error: null, collectedAt: "2026-08-26T18:00:00+08:00" },
      { exchange: "DCE", status: "FAILED", contractCount: 0, recordCount: 0, sources: ["fixture-dce"], error: "fixture DCE unavailable", collectedAt: "2026-08-26T18:00:00+08:00" },
    ],
  },
  limitations: ["仅统计交易所实际公布的会员方向排名", "请选择交易所、品种或具体月份合约后再读取席位明细。"],
};

const memberOverviewPayload = {
  available: true,
  tradingDay: "2026-08-26",
  latestPublishedDay: "2026-08-26",
  latestObservationDay: "2026-08-28",
  defaultProductKey: "SHFE.RB",
  defaultMemberKey: "会员甲",
  products: [
    { productKey: "SHFE.RB", exchange: "SHFE", productCode: "RB", displayName: "螺纹钢（RB）", grossPublishedPosition: 260, contracts: ["RB2610"] },
    { productKey: "SHFE.HC", exchange: "SHFE", productCode: "HC", displayName: "热卷（HC）", grossPublishedPosition: 80, contracts: ["HC2610"] },
    { productKey: "DCE.JM", exchange: "DCE", productCode: "JM", displayName: "焦煤（JM）", grossPublishedPosition: 70, contracts: ["JM2610"] },
  ],
  members: [
    { memberKey: "会员甲", memberName: "会员甲", grossPublishedPosition: 330, exchanges: ["DCE", "SHFE"] },
    { memberKey: "会员乙", memberName: "会员乙", grossPublishedPosition: 40, exchanges: ["SHFE"] },
  ],
  coverage: {
    exchangeCoverage: [
      { exchange: "SHFE", status: "PASS", contractCount: 2, recordCount: 80, publishers: ["SHFE"], adapters: ["official-shfe"], sourceUrls: ["https://www.shfe.com.cn/data"] },
      { exchange: "DCE", status: "FAILED", contractCount: 0, recordCount: 0, publishers: ["DCE"], adapters: ["official-dce"], sourceUrls: [], error: "fixture DCE unavailable" },
    ],
    missingExchanges: ["DCE"], isComplete: false, dataQualityStatus: "PARTIAL",
  },
  limitations: ["排行仅计入已公布的多头与空头持仓。"],
};

function contractHeatResponse(payload: Record<string, unknown>): Record<string, unknown> {
  if (!payload.available) return { ...payload, formulaVersion: "2026-09-contract-turnover-v2" };
  const config = payload.config as typeof heatPayload.config;
  const adapt = (point: Record<string, unknown>) => {
    const coverage = point.coverage as { variety: number; fund: number };
    const up = Number(point.upVarietyCount);
    const down = Number(point.downVarietyCount);
    const flat = Number(point.flatVarietyCount);
    return {
      tradeDate: point.tradeDate,
      totalContractCount: up + down + flat,
      validContractCount: up + down + flat,
      missingDirectionCount: 0,
      upContractCount: up,
      downContractCount: down,
      flatContractCount: flat,
      turnoverValidContractCount: point.fundScore10 === null ? 0 : up + down + flat,
      turnoverMissingContractCount: point.fundScore10 === null ? up + down + flat : 0,
      upTurnoverCny: Number(point.upFund ?? 0) * 100_000_000,
      downTurnoverCny: Number(point.downFund ?? 0) * 100_000_000,
      flatTurnoverCny: Number(point.flatFund ?? 0) * 100_000_000,
      totalDepositCny: 12_000_000_000,
      countScore10: point.breadthScore10,
      turnoverScore10: point.fundScore10,
      divergence: point.divergence,
      isWarmup: point.isWarmup,
      dataQualityStatus: point.dataQualityStatus,
      coverage: { direction: coverage.variety, turnover: coverage.fund, deposit: 1, missingExchanges: [] },
    };
  };
  return {
    ...payload,
    config: {
      defaultUserWeight: { contractWeight: config.defaultUserWeight.breadthWeight, turnoverWeight: config.defaultUserWeight.fundWeight },
      userWeight: config.userWeight, score: config.score,
      stateBands: config.stateBands, divergenceThreshold: config.divergenceThreshold,
      lookbackTradingDays: config.lookbackTradingDays, halfLife: config.halfLife,
      minTurnoverCoverage: 0.8, turnoverUnit: "CNY", depositUnit: "CNY",
    },
    points: (payload.points as Array<Record<string, unknown>>).map(adapt),
    latest: payload.latest ? adapt(payload.latest as Record<string, unknown>) : null,
    formulaVersion: "2026-09-contract-turnover-v2",
  };
}

async function openFutures(
  page: Page,
  payload: Record<string, unknown> = heatPayload,
  requests?: { count: number },
  options: { productStructure?: Record<string, unknown>; productNotionalStructure?: Record<string, unknown>; rankingLatestFailed?: boolean; legacyBackend?: boolean } = {},
): Promise<void> {
  const productStructure = options.productStructure ?? structurePayload;
  const productNotionalStructure = options.productNotionalStructure ?? notionalStructurePayload;
  await page.route("**/api/futures/contract-heat", route => {
    if (requests) requests.count += 1;
    return route.fulfill({ json: contractHeatResponse(payload) });
  });
  await page.route("**/api/futures/structures/product-open-interest**", route => {
    const url = new URL(route.request().url());
    const level = url.searchParams.get("level");
    const source = url.searchParams.get("source");
    const sourcePayload = source === "tdx"
      ? { ...productStructure, formulaVersion: "futures-structure-oi-top40-v3" }
      : productStructure;
    return route.fulfill({ json: level === "other" ? {
      ...sourcePayload,
      series: [{ memberKey: "CZCE.AP", memberName: "苹果", values: [5, 6] }],
    } : options.legacyBackend ? {
      ...productStructure, formulaVersion: "futures-structure-oi-v1", threshold: 0.015,
    } : sourcePayload });
  });
  await page.route("**/api/futures/structures/product-notional**", route => {
    const url = new URL(route.request().url());
    const level = url.searchParams.get("level");
    const source = url.searchParams.get("source");
    const sourcePayload = source === "tdx"
      ? { ...productNotionalStructure, formulaVersion: "futures-structure-notional-settlement-top40-v3" }
      : productNotionalStructure;
    return route.fulfill({ json: level === "other" ? {
      ...sourcePayload,
      series: [{ memberKey: "CZCE.AP", memberName: "苹果", values: [50_000, 60_000] }],
    } : sourcePayload });
  });
  await page.route("**/api/futures/structures/product-volume**", route => route.fulfill({ json: volumeStructurePayload }));
  await page.route("**/api/futures/member-positions**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/overview")) {
      const selectedDay = url.searchParams.get("trading_day");
      if (options.legacyBackend) return route.fulfill({ status: 404, json: { detail: "Not Found" } });
      if (options.rankingLatestFailed && !selectedDay) return route.fulfill({ json: {
        ...memberOverviewPayload, tradingDay: "2026-08-26", latestObservationDay: "2026-09-25",
        latestObservationCoverage: { ...memberOverviewPayload.coverage },
      } });
      return route.fulfill({ json: memberOverviewPayload });
    }
    if (url.pathname.endsWith("/history")) {
      const memberKey = url.searchParams.get("member_key");
      const productKey = url.searchParams.get("product_key");
      const contractCode = url.searchParams.get("contract_code");
      const rows = [
        { tradingDay: "2026-08-25", exchange: "SHFE", productKey: "SHFE.RB", productCode: "RB", productName: "螺纹钢", productDisplayName: "螺纹钢（RB）", contractCode: "RB2610", memberKey: "会员甲", memberName: "会员甲", longPosition: 100, longPositionChange: -10, shortPosition: 70, shortPositionChange: -5, netLongPosition: 30, netLongPositionChange: -5, netShortPosition: 0, netShortPositionChange: 0 },
        { tradingDay: "2026-08-26", exchange: "SHFE", productKey: "SHFE.RB", productCode: "RB", productName: "螺纹钢", productDisplayName: "螺纹钢（RB）", contractCode: "RB2610", memberKey: "会员甲", memberName: "会员甲", longPosition: 120, longPositionChange: 20, shortPosition: null, shortPositionChange: null, netLongPosition: null, netLongPositionChange: null, netShortPosition: null, netShortPositionChange: null },
        { tradingDay: "2026-08-26", exchange: "SHFE", productKey: "SHFE.HC", productCode: "HC", productName: "热卷", productDisplayName: "热卷（HC）", contractCode: "HC2610", memberKey: "会员甲", memberName: "会员甲", longPosition: 50, longPositionChange: 2, shortPosition: 30, shortPositionChange: -1, netLongPosition: 20, netLongPositionChange: 3, netShortPosition: 0, netShortPositionChange: 0 },
        { tradingDay: "2026-08-26", exchange: "DCE", productKey: "DCE.JM", productCode: "JM", productName: "焦煤", productDisplayName: "焦煤（JM）", contractCode: "JM2610", memberKey: "会员甲", memberName: "会员甲", longPosition: 25, longPositionChange: 1, shortPosition: 20, shortPositionChange: 0, netLongPosition: 5, netLongPositionChange: 1, netShortPosition: 0, netShortPositionChange: 0 },
      ];
      return route.fulfill({ json: {
        available: true, exchange: memberKey ? null : "SHFE", productKey: productKey || null,
        dates: ["2026-08-25", "2026-08-26"],
        rows: rows.filter(item => (!memberKey || item.memberKey === memberKey)
          && (!productKey || item.productKey === productKey)
          && (!contractCode || item.contractCode === contractCode)),
        sourceStatusByDay: { "2026-08-25": "PASS", "2026-08-26": "PASS" },
        sourceStatusByExchange: {},
        limitations: ["仅统计已公布排名"],
      } });
    }
    const selectedDay = url.searchParams.get("trading_day");
    if (options.legacyBackend) return route.fulfill({ json: {
      ...memberPositionsPayload, latestPublishedDay: undefined,
    } });
    if (options.rankingLatestFailed && !selectedDay) return route.fulfill({ json: {
      ...memberPositionsPayload,
      available: false,
      tradingDay: "2026-09-25",
      contracts: [],
      members: [],
      coverage: { ...memberPositionsPayload.coverage, publishedDirectionRankCount: 0, memberCount: 0, dataQualityStatus: "PARTIAL" },
      limitations: ["最新交易所采集失败"],
    } });
    const contractCode = url.searchParams.get("contract_code");
    const productKey = url.searchParams.get("product_key");
    const memberKey = url.searchParams.get("member_key");
    const ranks = [
      {
        exchange: "SHFE", contractCode: "RB2610", productCode: "RB", memberKey: "会员甲", memberName: "会员甲",
        longPosition: 120, longPositionChange: 4, longRank: 1,
        shortPosition: null, shortPositionChange: null, shortRank: null,
        netPosition: null, netLongPosition: null, netShortPosition: null, sources: ["fixture"],
      },
      {
        exchange: "SHFE", contractCode: "HC2610", productCode: "HC", memberKey: "会员甲", memberName: "会员甲",
        longPosition: 50, longPositionChange: 2, longRank: 2,
        shortPosition: 30, shortPositionChange: -1, shortRank: 3,
        netPosition: 20, netLongPosition: 20, netShortPosition: 0, sources: ["fixture"],
      },
    ];
    return route.fulfill({ json: productKey || memberKey ? {
      ...memberPositionsPayload,
      filters: { ...memberPositionsPayload.filters, contractCode, productCode: productKey?.split(".")[1] ?? null, memberKey },
      rows: ranks.filter(item => (!productKey || `${item.exchange}.${item.productCode}` === productKey)
        && (!contractCode || item.contractCode === contractCode)),
    } : memberPositionsPayload });
  });
  await page.goto("/futures/");
  await expect(page.getByRole("heading", { name: "国内期货数据" })).toBeVisible();
  await expect(page.locator('[data-test="futures-long-short-heat"]')).toBeVisible();
}

async function setFundWeight(page: Page, percent: number): Promise<void> {
  const slider = page.getByRole("slider", { name: "成交额热度权重" });
  await slider.focus();
  await slider.press("Home");
  for (let value = 0; value < percent; value += 5) await slider.press("ArrowRight");
}

async function formatTrendTooltip(page: Page, chartSelector: string, tradeDay: string): Promise<string> {
  return page.locator(chartSelector).evaluate(async (element, selectedDay) => {
    const chartElement = element.querySelector(".trend-chart");
    const resource = performance.getEntriesByType("resource").find(entry => /\/echarts-[^/]+\.js$/.test(entry.name));
    if (!chartElement || !resource) throw new Error("The rendered ECharts trend chart is unavailable");
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (target: Element) => {
      getOption: () => {
        tooltip: Array<{ formatter?: (params: unknown) => string }>;
        series: Array<{ name: string }>;
      };
    };
    const option = init(chartElement).getOption();
    const formatter = option.tooltip[0]?.formatter;
    if (!formatter) throw new Error("The chart tooltip formatter is unavailable");
    return formatter(option.series.map(series => ({ axisValue: selectedDay, seriesName: series.name })));
  }, tradeDay);
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const initialized = "marketlistener.e2e.longShortHeat.initialized";
    if (sessionStorage.getItem(initialized) !== "1") {
      localStorage.removeItem("marketlistener.futures.contractTurnoverHeat.turnoverWeight");
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
  await expect.poll(() => page.evaluate(() => localStorage.getItem("marketlistener.futures.contractTurnoverHeat.turnoverWeight"))).toBe("0.3");
  expect(requests.count).toBe(1);

  await page.reload();
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "35");
  await page.goto("/");
  await page.goto("/futures/");
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "35");
  await page.locator('[data-test="heat-weight-reset"]').click();
  await expect(page.locator('[data-test-kind="total"]')).toHaveAttribute("data-current-value", "50");
  await expect(page.locator('[data-test="heat-history-chart"]')).toHaveAttribute("data-last-value", "50");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("marketlistener.futures.contractTurnoverHeat.turnoverWeight"))).toBe("0.6");
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
  await expect(page.getByText(/官方成交额 35\.0%/)).toBeVisible();
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
  await page.route("**/api/futures/contract-heat", route => route.fulfill({ json: {
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
  await expect(panel).toContainText("基准日前 40 名");
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await expect(panel.locator('[data-test="futures-structure-line-chart"]')).toBeVisible();
  await expect(panel).toContainText("未分类新品种：1");
  await panel.getByText("全部", { exact: true }).click();
  await panel.getByRole("button", { name: /查看其他/ }).click();
  await expect(panel.getByRole("button", { name: "返回主图" })).toBeVisible();
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await expect(panel.locator('[data-test="futures-structure-line-chart"]')).toBeVisible();
});

test("product notional structure states its settlement-price basis and drills into other", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="product-notional-structure"]');
  await expect(panel).toContainText("品种市值分布");
  await expect(panel).toContainText("结算价 × 交易乘数 × 交易所单边持仓量");
  await expect(panel).toContainText("价格基准：结算价");
  await expect(panel).toContainText("基准日：2026-08-26");
  await expect(panel).toContainText("基准日前 40 名");
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await panel.getByRole("button", { name: /查看其他/ }).click();
  await expect(panel.getByRole("button", { name: "返回主图" })).toBeVisible();
});

test("product open interest and notional keep official and TDX formula versions independently selectable", async ({ page }) => {
  await openFutures(page);
  const oiPanel = page.locator('[data-test="product-open-interest-structure"]');
  const notionalPanel = page.locator('[data-test="product-notional-structure"]');
  await expect(oiPanel).toContainText("futures-structure-oi-official-v1");
  await oiPanel.getByText("通达信", { exact: true }).click();
  await expect(oiPanel).toContainText("futures-structure-oi-top40-v3");
  await notionalPanel.getByText("通达信", { exact: true }).click();
  await expect(notionalPanel).toContainText("futures-structure-notional-settlement-top40-v3");
  await expect(page.getByText("本机服务仍在运行旧版后端", { exact: false })).toHaveCount(0);
  await oiPanel.getByText("交易所官网", { exact: true }).click();
  await notionalPanel.getByText("交易所官网", { exact: true }).click();
  await expect(oiPanel).toContainText("futures-structure-oi-official-v1");
  await expect(notionalPanel).toContainText("futures-structure-notional-official-v5");
});

test("partial notional coverage leaves a visible chart gap", async ({ page }) => {
  const partial = {
    ...notionalStructurePayload,
    coverage: [
      notionalStructurePayload.coverage[0],
      {
        ...notionalStructurePayload.coverage[1], inputRowCount: 777, missingRowCount: 749,
        dataQualityStatus: "PARTIAL", missingSettlementCount: 5,
        missingOpenInterestCount: 2, missingMultiplierCount: 742,
        missingProducts: ["焦煤（JM）", "铁矿石（I）"],
        missingContracts: ["DCE.JM2610", "DCE.JM2701"],
      },
    ],
  };
  await openFutures(page, heatPayload, undefined, { productNotionalStructure: partial });
  const panel = page.locator('[data-test="product-notional-structure"]');
  await expect(panel).toContainText("官方有效输入 28/777 条");
  await expect(panel.locator('[data-test="notional-gap-details"]')).toContainText("官方乘数缺 742 条");
  await expect(panel.locator('[data-test="notional-gap-details"]')).toContainText("焦煤（JM）");
  await expect(panel.locator('[data-test="notional-gap-details"]')).toContainText("DCE.JM2610");
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
});

test("product traded volume renders both the ranked area and absolute-value trend views", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="product-volume-structure"]');
  await expect(panel).toContainText("品种成交分布");
  await expect(panel.locator('[data-test="futures-structure-chart"]')).toBeVisible();
  await expect(panel.locator('[data-test="futures-structure-line-chart"]')).toBeVisible();
  await expect(panel).toContainText("futures-structure-volume-official-v1");
});

test("three product trends are doubled in height and expose all computed products without pretending full coverage", async ({ page }) => {
  const lineSeries = [
    { memberKey: "DCE.JM", memberName: "焦煤（JM）", values: [null, null], lineValues: [100, 110] },
    { memberKey: "CZCE.AP", memberName: "苹果（AP）", values: [null, null], lineValues: [5, 6] },
    { memberKey: "SHFE.NEW", memberName: "合成新品种（NEW）", values: [null, null], lineValues: [null, 4] },
  ];
  const partial = {
    ...structurePayload, lineSeries, totals: [null, null],
    series: structurePayload.series.map(item => ({ ...item, values: [null, null], lineValues: item.values })),
    coverage: structurePayload.coverage.map(item => ({
      ...item, dataQualityStatus: "PARTIAL", missingExchangeCount: 2, missingExchanges: ["DCE", "GFEX"],
    })),
  };
  await openFutures(page, heatPayload, undefined, { productStructure: partial, productNotionalStructure: {
    ...partial, chartId: "product-notional", formulaVersion: "futures-structure-notional-official-v5", priceBasis: "SETTLEMENT",
  } });
  await page.route("**/api/futures/structures/product-volume**", route => route.fulfill({ json: {
    ...partial, chartId: "product-volume", formulaVersion: "futures-structure-volume-official-v1",
  } }));
  await page.locator('[data-test="futures-refresh"]').click();
  for (const id of ["product-open-interest", "product-notional", "product-volume"]) {
    const panel = page.locator(`[data-test="${id}-structure"]`);
    const chart = panel.locator('[data-test="futures-structure-line-chart"]');
    await expect(chart).toHaveCSS("height", "780px");
    await expect(chart).toHaveAttribute("data-series-count", "3");
    await expect(panel.locator('[data-test="structure-area-empty"]')).toBeVisible();
    await expect(panel.locator('[data-test="structure-coverage-summary"]')).toContainText("大商所、广期所");
    await expect(panel.locator('[data-test="structure-coverage-summary"]')).toContainText("0/2 个完整市场日");
    await panel.getByText("当前面积图成员", { exact: true }).click();
    await expect(chart).toHaveAttribute("data-series-count", "2");
    await panel.getByText("全部已计算品种", { exact: true }).click();
    await expect(chart).toHaveAttribute("data-series-count", "3");
  }
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

test("member inventory shows a company's published commodity pies and area trend", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="member-product-positions"]');
  await expect(panel).toContainText("席位持仓分布");
  await expect(panel.locator('[data-test="member-product-charts"]')).toBeVisible();
  await expect(panel).toContainText("会员甲 · 330张");
  await expect(panel.getByRole("combobox", { name: /交易所/ })).toHaveCount(0);
  await expect(panel.locator('[data-test="member-product-charts"] canvas')).toHaveCount(2);
  await expect(panel).toContainText("已公布商品持仓占比");
  await expect(panel).toContainText("各商品已公布持仓与增减历史");
  const tooltip = await formatTrendTooltip(page, '[data-test="member-product-charts"] article:last-child', "2026-08-26");
  expect(tooltip).toContain("螺纹钢（RB）");
  expect(tooltip).toContain("多 120 / 空 未公布");
  expect(tooltip).toContain("多 +20 / 空 未公布");
  expect(tooltip).toContain("SHFE");
  expect(tooltip).toContain("当日已公布增减合计");
  await panel.getByText("查看当日已公布商品与合约明细").click();
  await expect(panel.locator('[data-test="member-products-table"]')).toContainText("RB");
  await expect(panel.locator('[data-test="member-products-table"]')).toContainText("HC");
  await expect(panel.locator('[data-test="member-products-table"]')).toContainText("JM");
  await expect(panel).toContainText("未上榜和采集失败日不按零持仓处理");
  await expect(page.getByRole("heading", { name: "商品合约", exact: true })).toHaveCount(0);
});

test("commodity member rankings load by product and optional contract", async ({ page }) => {
  await openFutures(page);
  const panel = page.locator('[data-test="futures-member-positions"]');
  await expect(panel).toContainText("期货席位分布");
  await expect(panel.locator('[data-test="member-position-source-coverage"]')).toContainText("DCE：FAILED");
  await expect(panel.locator('[data-test="member-position-source-coverage"]')).toContainText("fixture DCE unavailable");
  await expect(panel.locator('[data-test="commodity-member-pies"] article')).toHaveCount(4);
  await expect(panel).toContainText("螺纹钢（RB）");
  await expect(panel.getByRole("combobox", { name: /交易所/ })).toHaveCount(0);
  await expect(panel.locator('[data-test="commodity-member-trends"] article')).toHaveCount(4);
  await expect(panel).toContainText("会员甲");
  await panel.getByText("查看所选日的公布名次与合约明细").click();
  await expect(panel).toContainText("120（1）");
  await expect(panel).toContainText("—（—）");
  await panel.locator(".member-position-filters .el-select__wrapper").nth(1).click();
  await page.getByRole("option", { name: "RB2610", exact: true }).click();
  await expect(panel).toContainText("RB2610");
  await expect(page.getByRole("heading", { name: "商品合约席位分布", exact: true })).toHaveCount(0);
});

test("latest failed observation remains visible while the latest published ranking stays selected", async ({ page }) => {
  await openFutures(page, heatPayload, undefined, { rankingLatestFailed: true });
  const memberPanel = page.locator('[data-test="member-product-positions"]');
  await expect(memberPanel).toContainText("交易日：2026-08-26");
  await expect(memberPanel.locator('[data-test="member-latest-observation"]')).toContainText("DCE");
  await expect(memberPanel.locator('[data-test="member-latest-observation"]')).toContainText("2026-09-25");
  await expect(memberPanel.getByRole("combobox", { name: "席位公司筛选" })).toBeEnabled();
});

test("old running backend is identified before the new charts are trusted", async ({ page }) => {
  await openFutures(page, heatPayload, undefined, { legacyBackend: true });
  await expect(page.getByText(/本机服务仍在运行旧版后端/)).toBeVisible();
});
