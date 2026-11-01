import { expect, test } from "@playwright/test";

test("strategy loads into the K-line and renders a traceable report", async ({ page }) => {
  test.setTimeout(60_000);
  let backtestRequests = 0;
  let chartRequests = 0;
  const strategy = {
    strategyId: "strategy.ma_crossover",
    id: "strategy.ma_crossover",
    version: 1,
    versionedId: "strategy.ma_crossover@1",
    strategyVersion: "1",
    displayName: "MA 均线交叉",
    description: "共享 SMA 的演示策略",
    category: "structured",
    origin: "builtin",
    status: "active",
    enabled: true,
    runMode: "backtest",
    backtestStatus: "not_run",
    scriptKind: "structured_v1",
    baseTimeframe: "1d",
    supportedAssetTypes: ["STOCK"],
    inputs: [],
    parameters: {
      fast: { type: "integer", default: 2 },
      slow: { type: "integer", default: 4 },
    },
    createdAt: "2026-09-03T00:00:00+08:00",
    updatedAt: "2026-09-03T00:00:00+08:00",
  };
  const bars = Array.from({ length: 40 }, (_item, index) => {
    const day = `2026-06-${String(index + 1).padStart(2, "0")}`;
    return {
      barOpenTime: `${day}T09:30:00+08:00`, tradingDate: day,
      open: 100 + index, high: 102 + index, low: 99 + index,
      close: 101 + index, volume: 1000 + index,
    };
  });
  await page.addInitScript(() => localStorage.setItem("market-all-view", "card"));
  await page.route("**/api/strategy/definitions**", (route) =>
    route.fulfill({ json: { items: [strategy], total: 1 } }),
  );
  await page.route("**/api/strategy/execution/capabilities", (route) =>
    route.fulfill({
      json: {
        capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false,
        resourceKinds: { strategy_function: false, indicator: false, strategy: true },
        modes: [
          { runMode: "backtest", enabled: true, executionStatus: "available", adapterId: "backtest-simulation-v1", reason: "隔离模拟" },
          { runMode: "paper", enabled: true, executionStatus: "available", adapterId: "paper-simulation-v1", reason: "隔离模拟" },
          { runMode: "live", enabled: false, executionStatus: "disabled", adapterId: null, reason: "未配置" },
        ],
      },
    }),
  );
  await page.route("**/api/strategy/indicators**", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/strategy/matches", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/market/instruments?**", (route) =>
    route.fulfill({
      json: {
        items: [{ instrumentId: "CN.SSE.STOCK.600000", symbol: "600000", name: "浦发银行", market: "CN", assetType: "STOCK", period: "1d", latestPrice: 139 }],
        total: 1, page: 1, pageSize: 10, dataVersion: "e2e-report-v1",
      },
    }),
  );
  await page.route("**/api/market/instruments/CN.SSE.STOCK.600000/chart**", async (route) => {
    chartRequests += 1;
    await route.fulfill({
      json: {
        instrumentId: "CN.SSE.STOCK.600000", period: "1d", availablePeriods: ["1d"],
        start: 0, size: bars.length, total: bars.length, hasMore: false,
        bars, series: {}, drawings: [], dataVersion: "e2e-report-v1",
      },
    });
  });
  await page.route("**/api/market/instruments/CN.SSE.STOCK.600000/drawings**", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/api/strategy/backtests", async (route) => {
    backtestRequests += 1;
    const request = route.request().postDataJSON() as { parameters: Record<string, number> };
    await route.fulfill({
      json: {
        runId: "bt_0123456789abcdef01234567", status: "ready", barsCount: 40,
        definitionHash: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        dependencyLock: {
          strategy: { id: "strategy.ma_crossover", version: 1, definitionHash: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" },
          strategyFunctions: [
            { id: "technical.sma", version: 1, definitionHash: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789" },
          ],
          indicators: [],
          marketData: { dataVersion: "e2e-report-v1", dataFingerprint: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210" },
        },
        report: {
          reportId: "report_bt_0123456789abcdef01234567", runId: "bt_0123456789abcdef01234567",
          currency: "CNY", period: { barsCount: 40 },
          metrics: {
            totalReturnPercent: request.parameters.fast === 3 ? 8 : 10,
            annualizedReturnPercent: 12, tradeCount: 1, winRatePercent: 100,
            averageWinLossRatio: null, profitFactor: null, maxDrawdownPercent: 3,
            averageHoldingBars: 8, maxConsecutiveWins: 1, maxConsecutiveLosses: 0,
            totalCommission: 12.5, totalSlippageCost: 3.2, finalEquity: 110000,
          },
          metricUnavailableReasons: { profitFactor: "NO_LOSING_TRADES" },
          markers: [
            { kind: "entry", time: bars[10].barOpenTime, price: 111, barIndex: 10, reason: "strategy_entry" },
            { kind: "exit", time: bars[18].barOpenTime, price: 119, barIndex: 18, reason: "strategy_exit", tradeId: "trade-1" },
          ],
          trades: [{
            tradeId: "trade-1", instrumentId: "CN.SSE.STOCK.600000", direction: "long",
            entryTime: bars[10].barOpenTime, exitTime: bars[18].barOpenTime,
            entryPrice: 111, exitPrice: 119, quantity: 100, commission: 12.5,
            slippageCost: 3.2, pnl: 787.5, pnlPercent: 7.09, holdingBars: 8,
            exitReason: "strategy_exit", currency: "CNY",
          }],
        },
      },
    });
  });

  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", { name: "回测" }).click();
  await expect(page.locator(".workbench-overlay")).toBeVisible({ timeout: 20_000 });
  const report = page.getByTestId("strategy-report");
  await expect(report).toContainText("MA 均线交叉");
  await expect(page.getByTestId("strategy-dependency-lock")).toContainText("strategy.ma_crossover@1");
  await expect(report).toContainText("110,000 CNY");
  await expect(report).toContainText("787.5");
  await expect(page.locator(".workbench-chart .chart-root")).toHaveAttribute("data-strategy-marker-count", "2");
  const chartCount = chartRequests;
  const requestCount = backtestRequests;
  await report.locator("input[role=spinbutton]").first().fill("3");
  await report.locator("input[role=spinbutton]").first().press("Enter");
  await expect.poll(() => backtestRequests).toBeGreaterThan(requestCount);
  expect(chartRequests).toBe(chartCount);
  await expect(report).toContainText("8%");
});
