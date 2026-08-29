import { expect, test, type Page } from "@playwright/test";

const ROUTES = [
  ["/", "首页"],
  ["/market/", "行情"],
  ["/data/", "数据"],
  ["/futures/", "国内期货数据"],
  ["/data-sources/", "数据源"],
  ["/strategy/", "策略"],
  ["/stats/", "账户分析"],
  ["/f10/", "F10 企业资料库"],
  ["/industry/", "产业链图谱"],
  ["/logs/", "日志"],
] as const;

async function expectCleanTerminal(page: Page): Promise<void> {
  const text = await page.locator("body").innerText();
  // Raw log/dashboard JSON is allowed to contain "null"; company cards have
  // their own stricter assertions in the industry and F10 specs.
  for (const forbidden of ["undefined", "NaN", "Invalid Date"]) {
    expect(text).not.toContain(forbidden);
  }
}

test("all terminal routes are reachable and render clean text", async ({ page }) => {
  for (const [route, title] of ROUTES) {
    await page.goto(route);
    await expect(page.locator("h1.page-title")).toContainText(title, { timeout: 15_000 });
    await expectCleanTerminal(page);
  }

  await page.goto("/industry-v2/");
  await expect(page).toHaveURL(/\/industry\/$/);
  await expect(page.locator("h1.page-title")).toContainText("产业链图谱");

});

test("theme switching persists and follows the system scheme", async ({ page }) => {
  await page.goto("/");
  await page.click('[data-test="theme-toggle"]');
  await page.click('[data-test="theme-option-dark"]');
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.evaluate(() => localStorage.getItem("marketlistener.theme"))).resolves.toBe("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.click('[data-test="theme-toggle"]');
  await page.click('[data-test="theme-option-light"]');
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.evaluate(() => localStorage.setItem("marketlistener.theme", "system"));
  await page.emulateMedia({ colorScheme: "dark" });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.emulateMedia({ colorScheme: "light" });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("home only exposes predefined operations and rejects arbitrary payloads", async ({ page }) => {
  await page.goto("/");
  const operationButtons = page.locator(".operation-buttons button");
  await expect(operationButtons).toHaveCount(10);
  await expect(page.locator(".operation-buttons input, .operation-buttons textarea")).toHaveCount(0);

  const created = await page.request.post("/api/operations", { data: { kind: "STATUS_REFRESH" } });
  expect(created.status()).toBe(202);

  const extraField = await page.request.post("/api/operations", {
    data: { kind: "STATUS_REFRESH", sql: "delete from runs" },
  });
  expect(extraField.status()).toBe(422);

  const arbitrary = await page.request.post("/api/operations", {
    data: { kind: "__import__('os').system('whoami')" },
  });
  expect(arbitrary.status()).toBe(422);

  await page.reload();
  const queue = page.locator(".el-table").last();
  await expect(queue).toContainText("操作");
  await expect(queue).not.toContainText("STATUS_REFRESH");
});

test("data workbench is read-only, bounded and shows real dashboards", async ({ page }) => {
  // The local DuckDB catalogue can be cold while other browser workers are
  // opening the industry atlas; keep this integration check deterministic.
  test.setTimeout(60_000);
  const listing = await page.request.get("/api/data/f10?page_size=500");
  expect(listing.status()).toBe(200);
  const payload = (await listing.json()) as { items: unknown[] };
  expect(payload.items.length).toBeLessThanOrEqual(500);

  expect([403, 405]).toContain((await page.request.post("/api/data/f10")).status());
  expect((await page.request.get("/api/data/not_sql")).status()).toBe(404);
  expect((await page.request.get("/api/data/f10?page_size=501")).status()).toBe(422);

  await page.goto("/data/");
  await expect(page.locator('h1.page-title')).toContainText("数据");
  await expect(page.locator('[data-test="data-refresh"]')).toBeVisible();
  await expect(page.locator(".data-browser")).toBeVisible();
  await expectCleanTerminal(page);
});

test("F10 API and detail page share one local company model", async ({ page }) => {
  const listing = await page.request.get("/api/f10/companies?page_size=10&market=CN");
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
    totalMarketCap?: { value: number; currency: string; asOf: string; source: string };
  };
  expect(detail.instrumentKey).toBe(first.instrumentKey);
  expect(detail.totalMarketCap?.value).toBeGreaterThan(0);
  expect(detail.totalMarketCap?.currency).toBeTruthy();
  expect(detail.totalMarketCap?.asOf).toBeTruthy();
  expect(detail.totalMarketCap?.source).toBeTruthy();

  await page.goto("/f10/");
  await expect(page.locator(".company-list-panel .el-table__row").first()).toBeVisible({
    timeout: 15_000,
  });
  await page.locator(".company-list-panel .el-table__row").first().click();
  await expect(page).toHaveURL(/\/f10\/company\//);
  await expect(page.locator(".company-detail")).toBeVisible({ timeout: 15_000 });
  await expectCleanTerminal(page);
});

test("logs API and page are bounded JSONL event views", async ({ page }) => {
  const response = await page.request.get("/api/logs?page_size=100");
  expect(response.status()).toBe(200);
  const payload = (await response.json()) as { total: number };
  expect(payload.total).toBeGreaterThanOrEqual(0);
  expect([403, 405]).toContain((await page.request.post("/api/logs")).status());

  await page.goto("/logs/");
  await expect(page.locator("h1.page-title")).toContainText("日志");
  await expect(page.locator(".data-controls")).toBeVisible();
  await expect(page.locator(".el-table").last()).toBeVisible();
  await expectCleanTerminal(page);
});

test("industry serves only the new atlas and Android package excludes legacy map", async ({ page }) => {
  const atlasResponse = await page.request.get("/api/industry/atlas");
  expect(atlasResponse.status()).toBe(200);
  const atlasHtml = await atlasResponse.text();
  expect(atlasHtml).toContain("atlas-data");
  expect(atlasHtml).not.toContain("industry-map");

  expect((await page.request.get("/industry-map.html")).status()).toBe(404);

  const infoResponse = await page.request.get("/api/android-package-info");
  expect(infoResponse.status()).toBe(200);
  const info = (await infoResponse.json()) as { package_id: string };
  expect(info.package_id).toBeTruthy();

  const packageResponse = await page.request.get("/api/android-package");
  expect(packageResponse.status()).toBe(200);
  expect(packageResponse.headers()["content-type"]).toContain("application/zip");
});

test("market workbench defaults to a resizable list and opens a full-screen chart", async ({ page }) => {
  await page.goto("/market/");
  await expect(page.locator("h1.page-title")).toContainText("目标行情");
  await expect(page.locator(".all-section h2")).toContainText("全部行情");
  await expect(page.getByRole("navigation", { name: "目标行情市场筛选" })).toContainText("全部市场");
  await expect(page.getByRole("navigation", { name: "目标行情策略筛选" })).toContainText("全部策略");
  await expect(page.locator(".all-toolbar").getByRole("button", { name: "查询" })).toBeVisible();
  await expect(page.locator("main.market-page > section")).toHaveCount(2);
  await expect(page.locator(".list-workbench")).toBeVisible();
  await expect(page.getByRole("separator", { name: "调整行情列表与K线图宽度" })).toBeVisible();
  await expect(page.locator(".flag-column-title")).toHaveText("标记");
  await expect(page.locator(".sequence-column-title")).toHaveText("序号");
  await expect(page.locator(".list-table-header")).toContainText("数据源");
  const firstFlag = page.locator(".row-flag").first();
  if (await firstFlag.count()) {
    await firstFlag.click();
    await expect(page.getByRole("menu", { name: "选择行标记颜色" })).toBeVisible();
    await page.getByRole("menuitem", { name: "红色" }).click();
    await expect(page.locator(".instrument-row.flagged").first()).toBeVisible();
  }
  await page.getByRole("button", { name: "卡片视图" }).click();
  const firstQuote = page.locator(".quote-card").first();
  // The desktop terminal can be launched before a local collector has written
  // any instruments.  Layout assertions still apply; chart interaction is
  // conditional on the local data that makes it meaningful.
  if (await firstQuote.count() === 0) {
    await expect(page.locator(".all-toolbar")).toBeVisible();
    return;
  }
  await expect(page.locator(".market-pagination .el-pagination__sizes")).toBeVisible();
  await expect(page.locator(".mini-kline").first()).toHaveCSS("height", "300px");
  await expect(firstQuote).toBeVisible({ timeout: 15_000 });
  await expect(firstQuote).toContainText("数据源");
  await expect(firstQuote.locator(".mini-kline")).toBeVisible();
  if (await firstQuote.locator(".quote-panel").count()) {
    await expect(firstQuote.locator(".quote-panel .quote-pair")).toHaveCount(7);
  }
  await firstQuote.click();
  await expect(page.locator(".workbench-overlay")).toBeVisible({ timeout: 15_000 });
  if (await page.locator(".workbench-chart .quote-panel").count()) {
    await expect(page.locator(".workbench-chart .quote-panel .quote-pair")).toHaveCount(7);
  }
  await expect(page.locator(".drawing-toolbar").getByRole("button", { name: "水平线" })).toBeVisible();
  await expect(page.locator(".drawing-toolbar").getByRole("button", { name: "垂直线" })).toBeVisible();
  await expect(page.locator(".drawing-toolbar").getByRole("button", { name: "箱体线" })).toBeVisible();
  await expect(page.locator(".drawing-toolbar").getByRole("button", { name: "文本框" })).toBeVisible();
  await expect(page.locator(".drawing-toolbar").getByRole("button", { name: "隐藏画线" })).toBeVisible();
  await expect(page.locator(".period-bar")).toContainText("季线");
  await page.getByRole("button", { name: "关闭图表" }).click();
  await expect(page.locator(".workbench-overlay")).toHaveCount(0);
  await expectCleanTerminal(page);
});

test("rectangle drawing previews hover, creates on two clicks, and drags endpoints together", async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto("/market/");
  await page.getByRole("button", { name: "卡片视图" }).click();
  const firstQuote = page.locator(".quote-summary").first();
  if (await firstQuote.count() === 0) return;

  await firstQuote.click();
  const overlay = page.locator(".workbench-overlay");
  await expect(overlay).toBeVisible({ timeout: 20_000 });

  const clearResponse = page.waitForResponse((response) =>
    response.request().method() === "PUT" && response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await clearResponse;

  await page.getByRole("button", { name: "箱体线" }).click();
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
  const createRequest = page.waitForRequest((request) =>
    request.method() === "PUT"
      && request.url().includes("/drawings")
      && request.postDataJSON().items.some((item: { type?: string }) => item.type === "rectangle"),
  );
  await page.mouse.click(secondX, secondY);
  const createdDrawing = (await createRequest).postDataJSON().items[0] as {
    points: Array<{ time: string; price: number }>;
  };
  expect(createdDrawing.points).toHaveLength(2);
  await expect(page.locator(".drawing-popover")).toBeVisible();
  await expect(chartRoot).toHaveAttribute("data-rectangle-anchor", "false");

  const dragRequest = page.waitForRequest((request) =>
    request.method() === "PUT"
      && request.url().includes("/drawings")
      && request.postDataJSON().items.some((item: { type?: string }) => item.type === "rectangle"),
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
  const createdTimes = createdDrawing.points.map((point) => Date.parse(point.time));
  const movedTimes = movedDrawing.points.map((point) => Date.parse(point.time));
  const shift = movedTimes[0] - createdTimes[0];
  expect(shift).not.toBe(0);
  expect(movedTimes[1] - createdTimes[1]).toBe(shift);

  await page.getByRole("button", { name: "线条颜色" }).click();
  await expect(page.locator(".drawing-color-popover:visible .preset-grid button")).toHaveCount(24);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "箱体填充颜色" }).click();
  await expect(page.locator(".drawing-color-popover:visible .preset-grid button")).toHaveCount(24);

  const cleanupResponse = page.waitForResponse((response) =>
    response.request().method() === "PUT" && response.url().includes("/drawings"),
  );
  await page.getByRole("button", { name: "删除全部画线" }).click();
  await cleanupResponse;
});

test("strategy page separates chart indicators from Python strategy functions", async ({ page }) => {
  await page.goto("/strategy/");
  await expect(page.locator("h1.page-title")).toHaveText("策略");
  await expect(page.locator("main.strategy-page > section")).toHaveCount(3);
  await expect(page.locator(".catalog-grid").first()).toContainText("ATR 通道");
  await expect(page.locator(".catalog-grid").nth(1)).toContainText("江恩上升波动率");
  await expect(page.locator("body")).not.toContainText("时序动量");
  await page.getByRole("button", { name: "新建策略" }).click();
  await expect(page.getByRole("radio", { name: "可视化函数组合" })).toBeChecked();
  await expect(page.locator(".condition-builder")).toContainText("根条件组");
  await page.locator(".el-radio-button").filter({ hasText: "安全 Python" }).click();
  await expect(page.getByText("安全 Python 条件")).toBeVisible();
  await page.getByRole("button", { name: "Close this dialog" }).click();
});

test("account analysis creates an account and exposes account-entry tools", async ({ page }) => {
  await page.goto("/stats/");
  await expect(page.locator("h1.page-title")).toHaveText("账户分析");
  await page.getByRole("button", { name: "新建账户" }).click();
  await page.getByRole("textbox", { name: "账户名称" }).fill(`E2E 分析账户 ${Date.now()}`);
  await page.getByRole("textbox", { name: "开始日期" }).fill("2026-08-01");
  await page.getByRole("spinbutton", { name: "期初资金" }).fill("100000");
  await page.getByRole("button", { name: "创建" }).click();
  await expect(page.locator(".metric-grid")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "每日账户快照" })).toBeVisible();
  await expect(page.locator(".csv-file")).toBeVisible();
});

test("data source page reports local categories and provider configuration", async ({ page }) => {
  await page.route("**/api/data-sources/inventory", async route => route.fulfill({ json: {
    summary: { categories: 1, rows: 2, instruments: 1, tables: 1, datasets: 1 }, preferences: {},
    metadata: { mode: "LIGHTWEIGHT_MANIFEST", rowCounts: "清单精确计数", fieldCoverage: "最新记录抽样", scansSilverRows: false },
    tables: [{ tableId: "instrument_file", name: "K线文件清单", kind: "DUCKDB_TABLE", storage: "catalog.duckdb", dataSources: ["tdx_futures_local"], rows: 1, rowCountMode: "EXACT", partitions: 1, updatedAt: "2026-08-02T16:00:00+08:00", columnCount: 2, fields: [{ name: "instrument_id", type: "VARCHAR", nullable: false, storage: "DuckDB" }] }],
    datasets: [{ datasetId: "CN:FUTURE:1d", name: "国内期货日线", market: "CN", assetType: "FUTURE", frequency: "1d", source: "tdx_futures_local", rows: 2, rowCountMode: "MANIFEST_EXACT", partitions: 1, registeredAt: "2026-08-02T16:00:00+08:00", primaryKey: ["instrument_id", "bar_open_time"], fields: [{ name: "close", type: "DOUBLE", nullable: true, storage: "Parquet" }], description: "测试数据集" }],
    inventory: [{
      categoryKey: "CN:FUTURE:1d", market: "CN", assetType: "FUTURE", period: "1d", instruments: 1, rows: 2,
      earliestBarAt: "2026-08-01T00:00:00+08:00", latestBarAt: "2026-08-02T00:00:00+08:00", lastUpdatedAt: "2026-08-02T16:00:00+08:00",
      sources: ["tdx_futures_local"], quality: { PASS: 2 }, partitions: 1, rowCountMode: "MANIFEST_EXACT", fieldCoverageSamples: 1,
      sourceDetails: [{ providerId: "tdx_futures_local", name: "通达信期货通本地缓存", endpoint: "TDX_FUTURES_ROOT", status: "IMPLEMENTED_LOCAL", periods: ["5m", "1d"], fields: ["open", "high", "low", "close", "volume", "open_interest", "settlement"], fieldNotes: "日线含持仓量和结算价。" }],
      fieldCompleteness: { open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 0, open_interest: 1, settlement: 1, pct_change: 0, amplitude: 1 },
    }],
  } }));
  await page.route("**/api/data-sources/providers", async route => route.fulfill({ json: {
    items: [{ providerId: "tdx_futures_local", name: "通达信期货通本地缓存", type: "local_file_adapter", access: "vipdoc", endpoint: "TDX_FUTURES_ROOT", authentication: "none", implemented: true, configured: true, priority: 5, enabled: true, markets: ["CN"], assetTypes: ["FUTURE"], periods: ["5m", "1d"], fields: ["open", "high", "low", "close", "volume", "open_interest", "settlement"], fieldSchema: [{ name: "open_interest", type: "DOUBLE", nullable: true, storage: "Parquet" }], fieldNotes: "日线含持仓量和结算价。", status: "IMPLEMENTED_LOCAL" }],
  } }));
  await page.goto("/data-sources/");
  await expect(page.locator('[data-test="local-database-tables"] .el-table__row').first()).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('[data-test="registered-datasets"] .el-table__row').first()).toBeVisible();
  await expect(page.locator('[data-test="data-source-inventory"] .el-table__row').first()).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('[data-test="provider-registry"] .el-table__row').first()).toBeVisible();
  await expect(page.locator('[data-test="kline-field-legend"]')).toContainText("持仓量");
  await expect(page.locator('[data-test="minute-kline-field-rules"]')).toContainText("沉淀资金");
  await expect(page.locator('[data-test="data-source-inventory"]')).toContainText("结算价 100%");
  await expect(page.locator('[data-test="data-source-inventory"]')).toContainText("成交额：样本无值");
  await expectCleanTerminal(page);
});
