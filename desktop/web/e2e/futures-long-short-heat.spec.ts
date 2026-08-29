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

async function openFutures(
  page: Page,
  payload: Record<string, unknown> = heatPayload,
  requests?: { count: number },
): Promise<void> {
  await page.route("**/api/futures/heat", route => {
    if (requests) requests.count += 1;
    return route.fulfill({ json: payload });
  });
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
