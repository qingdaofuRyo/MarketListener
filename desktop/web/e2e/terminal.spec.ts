import { expect, test, type Page } from "@playwright/test";

const ROUTES = [
  ["/", "仪表盘"],
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

async function chooseDrawingTool(page: Page, group: string, tool: string): Promise<void> {
  await page.getByRole("button", { name: group, exact: true }).click();
  await page.getByRole("menuitem", { name: tool, exact: true }).click();
}

test("all terminal routes are reachable and render clean text", async ({
  page,
}) => {
  for (const [route, title] of ROUTES) {
    await page.goto(route);
    await expect(page.locator("h1.page-title")).toContainText(title, {
      timeout: 15_000,
    });
    await expectCleanTerminal(page);
  }

  await page.goto("/industry-v2/");
  await expect(page).toHaveURL(/\/industry\/$/);
  await expect(page.locator("h1.page-title")).toContainText("产业链图谱");
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

test("home only exposes predefined operations and rejects arbitrary payloads", async ({
  page,
}) => {
  await page.goto("/");
  const operationButtons = page.locator(".operation-buttons button");
  await expect(operationButtons).toHaveCount(10);
  await expect(
    page.locator(".operation-buttons input, .operation-buttons textarea"),
  ).toHaveCount(0);

  const created = await page.request.post("/api/operations", {
    data: { kind: "STATUS_REFRESH" },
  });
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

test("data workbench is read-only, bounded and shows real dashboards", async ({
  page,
}) => {
  // The local DuckDB catalogue can be cold while other browser workers are
  // opening the industry atlas; keep this integration check deterministic.
  test.setTimeout(60_000);
  const listing = await page.request.get("/api/data/f10?page_size=500");
  expect(listing.status()).toBe(200);
  const payload = (await listing.json()) as { items: unknown[] };
  expect(payload.items.length).toBeLessThanOrEqual(500);

  expect([403, 405]).toContain(
    (await page.request.post("/api/data/f10")).status(),
  );
  expect((await page.request.get("/api/data/not_sql")).status()).toBe(404);
  expect((await page.request.get("/api/data/f10?page_size=501")).status()).toBe(
    422,
  );
  const cnOverview = await page.request.get("/api/data/equities/cn/overview");
  expect(cnOverview.status()).toBe(200);
  const hkOverview = await page.request.get("/api/data/equities/hk/overview");
  expect(hkOverview.status()).toBe(200);
  const hkPayload = (await hkOverview.json()) as {
    available: boolean;
    points: unknown[];
    limitations: string[];
  };
  expect(typeof hkPayload.available).toBe("boolean");
  if (hkPayload.available) expect(hkPayload.points.length).toBeGreaterThan(0);
  else expect(hkPayload.limitations.length).toBeGreaterThan(0);
  const statusList = await page.request.get(
    "/api/data/equities/cn/lists?type=st_warning&page=1&pageSize=50",
  );
  expect(statusList.status()).toBe(200);
  expect(
    (await statusList.json()) as { available: boolean; items: unknown[] },
  ).toMatchObject({ available: false, items: [] });

  await page.goto("/data/");
  await expect(page.locator("h1.page-title")).toContainText("数据");
  await expect(page.locator('[data-test="data-refresh"]')).toBeVisible();
  const r4Sections = page.locator('[data-test="r4-data-sections"]');
  await expect(r4Sections).toBeVisible();
  await expect(r4Sections.getByText("A股", { exact: true })).toBeVisible();
  await expect(
    r4Sections.getByText("高振幅且低涨幅家数", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator('[data-test="r4-equity-status-list"]'),
  ).toContainText("ST 风险警示");
  await expect(
    page.locator('[data-test="r4-equity-status-list"]'),
  ).toContainText("不会以当前名称、历史最后状态或空表代替事实。");
  await r4Sections.getByText("其他数据", { exact: true }).click();
  await expect(r4Sections.getByText("中国", { exact: true })).toBeVisible();
  await expect(r4Sections.getByText("美国", { exact: true })).toBeVisible();
  await expect(
    r4Sections.getByText("货币与利率", { exact: true }),
  ).toBeVisible();
  await expect(r4Sections.getByText("物价", { exact: true })).toBeVisible();
  await expect(r4Sections.getByText("贸易", { exact: true })).toBeVisible();
  await expect(
    r4Sections.getByText("消费与生产", { exact: true }),
  ).toBeVisible();
  const m2Seasonal = r4Sections.getByText("季节图", { exact: true });
  await expect(m2Seasonal).toBeVisible();
  await m2Seasonal.click();
  await expect(
    r4Sections.locator(".chart-title").filter({ hasText: "季节图" }),
  ).toBeVisible();
  await expect(page).toHaveURL(/section=other/);
  await page.goto("/data/?section=hk");
  await expect(r4Sections.getByText("港股市值", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/section=hk/);
  await expect(page.locator(".data-browser")).toBeVisible();
  await expectCleanTerminal(page);
});

test("A-share status lists retain their selected date and page through the local API contract", async ({
  page,
}) => {
  await page.route("**/api/data/equities/cn/lists**", async (route) => {
    const url = new URL(route.request().url());
    const requestedPage = Number(url.searchParams.get("page") || "1");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        available: true,
        market: "CN",
        segment: "ALL",
        listType: url.searchParams.get("type") || "st_warning",
        listTitle: "ST 风险警示",
        asOfDay: url.searchParams.get("asOfDay"),
        page: requestedPage,
        pageSize: 50,
        total: 51,
        items: [
          {
            instrumentId: `CN.SSE.STOCK.60000${requestedPage}`,
            symbol: `60000${requestedPage}`,
            name: `测试状态标的 ${requestedPage}`,
            assetType: "STOCK",
            segment: "MAIN",
            status: "ST",
            effectiveFrom: "2026-08-20",
            expectedEnd: null,
            reason: "受控浏览器夹具",
            source: "测试来源",
            capturedAt: "2026-08-20T16:00:00+08:00",
          },
        ],
        limitations: [],
      }),
    });
  });

  await page.goto("/data/?section=cn");
  const list = page.locator('[data-test="r4-equity-status-list"]');
  await expect(list).toContainText("测试状态标的 1");
  await expect(list.locator(".el-pagination")).toBeVisible();
  await list.locator(".el-pagination .btn-next").click();
  await expect(list).toContainText("测试状态标的 2");
  await expect(list.locator(".el-pagination .is-active")).toHaveText("2");
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

test("industry serves only the new atlas and Android package excludes legacy map", async ({
  page,
}) => {
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
  expect(packageResponse.headers()["content-type"]).toContain(
    "application/zip",
  );
});

test("market workbench defaults to a resizable list and opens a full-screen chart", async ({
  page,
}) => {
  await page.goto("/market/");
  await expect(page.getByRole("navigation", { name: "行情导航" })).toBeVisible();
  await expect(page.getByRole("button", { name: "全部行情", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "目标行情", exact: true })).toBeVisible();
  await expect(page.locator(".all-section h2")).toContainText("全部行情");
  await expect(
    page.locator(".all-toolbar").getByRole("button", { name: "查询" }),
  ).toBeVisible();
  await expect(page.locator("main.market-page > section")).toHaveCount(2);
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
  await page.getByRole("tab", { name: "策略函数" }).click();
  await expect(page.locator(".catalog-grid")).toContainText("江恩上升波动率");
  await expect(page.locator("body")).not.toContainText("时序动量");
  await page.getByRole("tab", { name: "策略", exact: true }).click();
  await expect(
    page.getByTestId("strategy-execution-capabilities"),
  ).toContainText("仅桌面 Strategy 可创建");
  await expect(
    page.getByTestId("strategy-execution-capabilities"),
  ).toContainText("live · DISABLED");
  await page.getByRole("button", { name: "新建策略" }).click();
  await expect(page.getByRole("radio", { name: "结构化规则" })).toBeChecked();
  await expect(page.locator(".structured-editor")).toContainText(
    "开仓与平仓规则",
  );
  await page
    .locator(".el-radio-button")
    .filter({ hasText: "兼容安全 Python" })
    .click();
  await expect(page.getByText("兼容安全 Python 条件")).toBeVisible();
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

test("data source page reports local categories and provider configuration", async ({
  page,
}) => {
  await page.route("**/api/data-sources/inventory", async (route) =>
    route.fulfill({
      json: {
        summary: {
          categories: 1,
          rows: 2,
          instruments: 1,
          tables: 1,
          datasets: 1,
        },
        preferences: {},
        metadata: {
          mode: "LIGHTWEIGHT_MANIFEST",
          rowCounts: "清单精确计数",
          fieldCoverage: "最新记录抽样",
          scansSilverRows: false,
        },
        tables: [
          {
            tableId: "instrument_file",
            name: "K线文件清单",
            kind: "DUCKDB_TABLE",
            storage: "catalog.duckdb",
            dataSources: ["tdx_futures_local"],
            rows: 1,
            rowCountMode: "EXACT",
            partitions: 1,
            updatedAt: "2026-08-02T16:00:00+08:00",
            columnCount: 2,
            fields: [
              {
                name: "instrument_id",
                type: "VARCHAR",
                nullable: false,
                storage: "DuckDB",
              },
            ],
          },
        ],
        datasets: [
          {
            datasetId: "CN:FUTURE:1d",
            name: "国内期货日线",
            market: "CN",
            assetType: "FUTURE",
            frequency: "1d",
            source: "tdx_futures_local",
            rows: 2,
            rowCountMode: "MANIFEST_EXACT",
            partitions: 1,
            registeredAt: "2026-08-02T16:00:00+08:00",
            primaryKey: ["instrument_id", "bar_open_time"],
            fields: [
              {
                name: "close",
                type: "DOUBLE",
                nullable: true,
                storage: "Parquet",
              },
            ],
            description: "测试数据集",
          },
        ],
        inventory: [
          {
            categoryKey: "CN:FUTURE:1d",
            market: "CN",
            assetType: "FUTURE",
            period: "1d",
            instruments: 1,
            rows: 2,
            earliestBarAt: "2026-08-01T00:00:00+08:00",
            latestBarAt: "2026-08-02T00:00:00+08:00",
            lastUpdatedAt: "2026-08-02T16:00:00+08:00",
            sources: ["tdx_futures_local"],
            quality: { PASS: 2 },
            partitions: 1,
            rowCountMode: "MANIFEST_EXACT",
            fieldCoverageSamples: 1,
            sourceDetails: [
              {
                providerId: "tdx_futures_local",
                name: "通达信期货通本地缓存",
                endpoint: "TDX_FUTURES_ROOT",
                status: "IMPLEMENTED_LOCAL",
                periods: ["5m", "1d"],
                fields: [
                  "open",
                  "high",
                  "low",
                  "close",
                  "volume",
                  "open_interest",
                  "settlement",
                ],
                fieldNotes: "日线含持仓量和结算价。",
              },
            ],
            fieldCompleteness: {
              open: 1,
              high: 1,
              low: 1,
              close: 1,
              volume: 1,
              amount: 0,
              open_interest: 1,
              settlement: 1,
              pct_change: 0,
              amplitude: 1,
            },
          },
        ],
      },
    }),
  );
  await page.route("**/api/data-sources/providers", async (route) =>
    route.fulfill({
      json: {
        items: [
          {
            providerId: "tdx_futures_local",
            name: "通达信期货通本地缓存",
            type: "local_file_adapter",
            access: "vipdoc",
            endpoint: "TDX_FUTURES_ROOT",
            authentication: "none",
            implemented: true,
            configured: true,
            priority: 5,
            enabled: true,
            markets: ["CN"],
            assetTypes: ["FUTURE"],
            periods: ["5m", "1d"],
            fields: [
              "open",
              "high",
              "low",
              "close",
              "volume",
              "open_interest",
              "settlement",
            ],
            fieldSchema: [
              {
                name: "open_interest",
                type: "DOUBLE",
                nullable: true,
                storage: "Parquet",
              },
            ],
            fieldNotes: "日线含持仓量和结算价。",
            status: "IMPLEMENTED_LOCAL",
          },
        ],
      },
    }),
  );
  await page.goto("/data-sources/");
  await expect(page.getByText("通达信期货通本地缓存")).toBeVisible();
  await expect(page.getByRole("heading", { name: "数据覆盖与更新" })).toBeVisible();
  await expect(page.locator(".el-table__row").first()).toContainText("期货");
  await expect(page.locator('[data-test="provider-registry"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存主备配置" })).toHaveCount(0);
  await expectCleanTerminal(page);
});
