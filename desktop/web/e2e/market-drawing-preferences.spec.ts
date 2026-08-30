import { expect, test, type Page, type Route } from "@playwright/test";

interface DrawingPoint { time: string; price: number }
interface Drawing {
  id: string;
  type: string;
  points: DrawingPoint[];
  crossPeriod?: boolean;
  style?: { color?: string; fillColor?: string; width?: number; lineStyle?: string };
}

const instrumentId = "CN.SSE.STOCK.600000";
const bars = Array.from({ length: 60 }, (_, index) => {
  const day = new Date(Date.UTC(2026, 5, index + 1)).toISOString().slice(0, 10);
  const open = 10 + index * 0.08;
  return {
    barOpenTime: `${day}T09:30:00+08:00`,
    tradingDate: day,
    open,
    high: open + 0.45,
    low: open - 0.35,
    close: open + (index % 2 ? -0.12 : 0.2),
    volume: 10_000 + index * 100,
    amount: 1_000_000 + index * 10_000,
  };
});

async function mockMarket(page: Page): Promise<{ drawings: () => Drawing[] }> {
  let drawings: Drawing[] = [];
  const instrument = { instrumentId, symbol: "600000", name: "浦发银行", market: "CN", assetType: "STOCK", latestPrice: 14.72, actualSource: "fixture" };
  const fulfill = (route: Route, json: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(json) });

  await page.route("**/api/strategy/definitions*", route => fulfill(route, { items: [] }));
  await page.route("**/api/market/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/market/cache-status") return fulfill(route, { dataVersion: "e2e" });
    if (path === "/api/market/categories") return fulfill(route, { items: [{ id: "all", label: "全部市场" }] });
    if (path === "/api/market/instruments") return fulfill(route, { items: [instrument], total: 120, dataVersion: "e2e" });
    if (path === "/api/market/instruments/bars/batch") return fulfill(route, { items: { [instrumentId]: bars } });
    if (path === "/api/market/drawings/batch") return fulfill(route, { items: { [instrumentId]: drawings }, total: 1 });
    if (path.endsWith("/chart")) return fulfill(route, { bars, series: {}, drawings, total: bars.length, start: 0, size: bars.length, period: "1d", availablePeriods: ["1d"], hasMore: false, latestBarAt: bars.at(-1)?.barOpenTime, dataVersion: "e2e" });
    if (path.endsWith("/drawings")) {
      if (route.request().method() === "PUT") drawings = (route.request().postDataJSON() as { items: Drawing[] }).items;
      return fulfill(route, { items: drawings });
    }
    return route.continue();
  });
  return { drawings: () => drawings };
}

test("first market request waits for the current data version so ETF names are fresh", async ({ page }) => {
  const requestedVersions: Array<string | null> = [];
  const etf = { instrumentId: "CN.SSE.ETF.510300", symbol: "510300", name: "沪深300ETF", market: "CN", assetType: "ETF", latestPrice: 4.12, actualSource: "通达信金融终端（本地）" };
  const fulfill = (route: Route, json: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(json) });

  await page.route("**/api/strategy/definitions*", route => fulfill(route, { items: [] }));
  await page.route("**/api/market/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/market/cache-status") {
      await new Promise(resolve => setTimeout(resolve, 120));
      return fulfill(route, { dataVersion: "names-v1" });
    }
    if (url.pathname === "/api/market/categories") return fulfill(route, { items: [{ id: "all", label: "全部市场" }] });
    if (url.pathname === "/api/market/instruments") {
      requestedVersions.push(url.searchParams.get("version"));
      return fulfill(route, { items: [etf], total: 1, dataVersion: "names-v1" });
    }
    if (url.pathname === "/api/market/instruments/bars/batch") return fulfill(route, { items: { [etf.instrumentId]: bars } });
    if (url.pathname.endsWith("/drawings")) return fulfill(route, { items: [] });
    return fulfill(route, { bars, series: {}, drawings: [], total: bars.length, start: 0, size: bars.length, period: "1d", availablePeriods: ["1d"], hasMore: false, dataVersion: "names-v1" });
  });

  await page.goto("/market/");
  await expect(page.getByText("沪深300ETF", { exact: true }).first()).toBeVisible();
  expect(requestedVersions).toEqual(["names-v1"]);
});

async function drawRectangle(page: Page, leftRatio: number, rightRatio: number): Promise<Drawing> {
  await page.getByRole("button", { name: "箱体线" }).click();
  await expect(page.getByRole("button", { name: "箱体线" })).toHaveClass(/active/);
  await expect(page.locator(".workbench-chart .el-loading-mask")).toHaveCount(0);
  const canvas = page.locator(".workbench-chart canvas").first();
  const chartRoot = page.locator(".workbench-chart .chart-root");
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(100);
  const box = await canvas.boundingBox();
  if (!box) throw new Error("K 线画布不可用");
  const first = { x: box.x + box.width * leftRatio, y: box.y + box.height * 0.35 };
  const second = { x: box.x + box.width * rightRatio, y: box.y + box.height * 0.48 };
  await page.mouse.move(first.x, first.y);
  await expect(chartRoot).toHaveAttribute("data-rectangle-preview", "true");
  await page.mouse.click(first.x, first.y);
  await expect(chartRoot).toHaveAttribute("data-rectangle-anchor", "true");
  await page.mouse.move(second.x, second.y);
  const requestPromise = page.waitForRequest(request => request.method() === "PUT" && request.url().includes("/drawings"));
  await page.mouse.click(second.x, second.y);
  const request = await requestPromise;
  return (request.postDataJSON() as { items: Drawing[] }).items.at(-1)!;
}

test("market card paging and drawing preferences persist with stable rectangle dragging", async ({ page }) => {
  const state = await mockMarket(page);
  await page.goto("/market/");
  await page.getByRole("button", { name: "卡片视图" }).click();

  await expect(page.locator(".market-pagination .el-pagination__sizes")).toBeVisible();
  await page.locator(".market-pagination .el-select").click();
  const pageSizes = page.locator(".el-select-dropdown:visible .el-select-dropdown__item");
  await expect(pageSizes).toHaveCount(5);
  for (const [index, size] of [10, 20, 30, 50, 100].entries()) await expect(pageSizes.nth(index)).toContainText(String(size));
  await page.keyboard.press("Escape");
  await expect(page.locator(".mini-kline").first()).toHaveCSS("height", "300px");

  await page.locator(".quote-summary").first().click();
  await expect(page.locator(".workbench-overlay")).toBeVisible();
  await expect(page.locator(".workbench-header")).toContainText("2026-07-30");
  const created = await drawRectangle(page, 0.2, 0.48);
  await expect(page.locator(".drawing-popover")).toBeVisible();

  await page.getByRole("button", { name: "线条颜色" }).click();
  const linePicker = page.locator(".drawing-color-popover:visible");
  await expect(linePicker.locator(".preset-grid button")).toHaveCount(80);
  await linePicker.locator('button[title="#9C27B0"]').click();
  await page.keyboard.press("Escape");
  await expect(page.locator(".drawing-color-popover:visible")).toHaveCount(0);
  await page.getByLabel("选择线宽").click();
  await page.getByRole("button", { name: "3像素线宽" }).click();
  await page.getByLabel("选择线型").click();
  await page.getByRole("button", { name: "虚线" }).click();

  await page.getByRole("button", { name: "箱体填充颜色" }).click();
  const fillPicker = page.locator(".drawing-color-popover:visible");
  await expect(fillPicker.locator(".preset-grid button")).toHaveCount(80);
  await fillPicker.locator('button[title="#4CAF50"]').click();
  const opacity = fillPicker.getByRole("slider", { name: "不透明度" });
  await opacity.fill("0.2");
  await expect(opacity).toHaveCSS("opacity", "1");
  expect(await opacity.evaluate(element => getComputedStyle(element).getPropertyValue("--alpha-thumb-color"))).toContain("rgba(76,175,80,0.200)");
  await page.keyboard.press("Escape");
  await expect(page.locator(".drawing-color-popover:visible")).toHaveCount(0);

  await page.getByRole("button", { name: "吸附" }).click();
  await page.getByRole("button", { name: "连续画线" }).click();
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("market-drawing-preferences-v1") || "{}"));
  expect(stored).toMatchObject({ magnet: true, crossPeriod: true, keepDrawing: true });
  expect(stored.styles.rectangle).toMatchObject({ color: "rgba(156,39,176,1.000)", fillColor: "rgba(76,175,80,0.200)", width: 3, lineStyle: "dashed" });
  await expect.poll(() => state.drawings()[0]?.style?.fillColor).toBe("rgba(76,175,80,0.200)");

  const canvas = page.locator(".workbench-chart canvas").first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("K 线画布不可用");
  const dragRequest = page.waitForRequest(request => request.method() === "PUT" && request.url().includes("/drawings"));
  await page.mouse.move(box.x + box.width * 0.34, box.y + box.height * 0.42);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.44, box.y + box.height * 0.42, { steps: 8 });
  await page.mouse.up();
  const moved = ((await dragRequest).postDataJSON() as { items: Drawing[] }).items.find(item => item.id === created.id)!;
  const indexes = (drawing: Drawing) => drawing.points.map(point => bars.findIndex(bar => bar.barOpenTime === point.time));
  const [createdStart, createdEnd] = indexes(created);
  const [movedStart, movedEnd] = indexes(moved);
  expect(movedStart).toBeGreaterThan(createdStart);
  expect(Math.abs(movedEnd - movedStart)).toBe(Math.abs(createdEnd - createdStart));

  const deleteRequest = page.waitForRequest(request => request.method() === "PUT" && request.url().includes("/drawings"));
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await deleteRequest;
  const recreated = await drawRectangle(page, 0.58, 0.7);
  expect(recreated.style).toMatchObject({ color: "rgba(156,39,176,1.000)", fillColor: "rgba(76,175,80,0.200)", width: 3, lineStyle: "dashed" });
  await page.getByRole("button", { name: "光标" }).click();
  await page.mouse.click(box.x + box.width * 0.9, box.y + box.height * 0.68);
  await expect(page.locator(".drawing-popover")).toHaveCount(0);

  await page.getByRole("button", { name: "关闭图表" }).click();
  await expect(page.locator('.mini-kline .chart-root[data-drawing-count="1"]').first()).toBeVisible();

  await page.reload();
  await page.locator(".quote-summary").first().click();
  await expect(page.locator(".workbench-header")).toContainText("2026-07-30");
  await expect(page.getByRole("button", { name: "吸附" })).toHaveClass(/active/);
  await expect(page.getByRole("button", { name: "跨周期" }).first()).toHaveClass(/active/);
  await expect(page.getByRole("button", { name: "连续画线" })).toHaveClass(/active/);
  const next = await drawRectangle(page, 0.72, 0.88);
  expect(next.crossPeriod).toBe(true);
  expect(next.style).toMatchObject({ color: "rgba(156,39,176,1.000)", fillColor: "rgba(76,175,80,0.200)", width: 3, lineStyle: "dashed" });
  await expect.poll(() => state.drawings().length).toBe(2);
});
