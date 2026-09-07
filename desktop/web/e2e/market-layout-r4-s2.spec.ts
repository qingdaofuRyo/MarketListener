import { expect, test } from "@playwright/test";
import { finiteExtent, nestedBarGeometry } from "../src/domain/chartLayout";

const instrument = { instrumentId: "CN.SSE.STOCK.600000", symbol: "600000", name: "浦发银行", assetType: "STOCK", market: "CN", latestPrice: 12 };
const bars = Array.from({ length: 80 }, (_, index) => ({
  barOpenTime: new Date(Date.UTC(2026, 5, index + 1)).toISOString(),
  open: 11 + index / 100, high: 13 + index / 100, low: 10 + index / 100, close: 12 + index / 100,
  volume: index % 7 ? 10_000 + index * 20 : null, amount: 100_000_000 + index * 1000,
}));

async function mockMarket(page: import("@playwright/test").Page): Promise<void> {
  await page.route("**/api/market/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("cache-status")) return route.fulfill({ json: { dataVersion: "s2" } });
    if (url.pathname.endsWith("categories")) return route.fulfill({ json: { items: [{ id: "cn-future-main", label: "国内期货主连合约" }] } });
    if (url.pathname.endsWith("/instruments")) return route.fulfill({ json: { items: [instrument], total: 1, dataVersion: "s2" } });
    if (url.pathname.endsWith("/drawings")) return route.fulfill({ json: { items: [] } });
    return route.fulfill({ json: { bars, total: bars.length, start: 0, size: bars.length, period: "1d", availablePeriods: ["1d", "1h"], hasMore: false } });
  });
  await page.route("**/api/signals/**", (route) => route.fulfill({ json: { items: [], events: [] } }));
  await page.route("**/api/strategy/**", (route) => route.fulfill({ json: { items: [], instances: [] } }));
}

test("R4续2 nested bars preserve center, visible back sides, and separate finite scales", () => {
  const geometry = nestedBarGeometry(80, 20);
  expect(geometry.back.x + geometry.back.width / 2).toBe(80);
  expect(geometry.front.x + geometry.front.width / 2).toBe(80);
  expect(geometry.back.width).toBeGreaterThan(geometry.front.width);
  expect(geometry.front.x).toBeGreaterThan(geometry.back.x);
  expect(finiteExtent([null, 0, 10, 1_000_000])).toEqual({ min: 0, max: 1_000_000 });
  expect(finiteExtent([null, undefined])).toBeNull();
});

test("R4续2 all-market uses independent route, opaque list header and canvas overlay", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await mockMarket(page);
  await page.goto("/market/all/");
  await expect(page).toHaveURL(/\/market\/all\/\?category=cn-future-main/);
  await expect(page.getByRole("link", { name: "全部行情", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "目标行情", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "行情", exact: true })).toHaveCount(0);
  const toolbar = page.locator(".instrument-list .all-toolbar");
  const list = page.locator(".instrument-list");
  const header = page.locator(".list-header-row");
  const toolbarBox = (await toolbar.boundingBox())!;
  const listBox = (await list.boundingBox())!;
  expect(toolbarBox.x).toBeGreaterThanOrEqual(listBox.x);
  expect(toolbarBox.y).toBeGreaterThanOrEqual(listBox.y);
  await expect(header).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await page.evaluate(() => localStorage.setItem("marketlistener.theme", "light"));
  await page.reload();
  await expect(page.locator(".list-header-row")).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  const topPeriod = page.getByRole("combobox", { name: "上看板 K 线周期" });
  const periodBox = (await topPeriod.boundingBox())!;
  expect(periodBox.width).toBeGreaterThan(45);
  expect(periodBox.width).toBeLessThan(90);
  const chart = page.locator(".board .chart-root").first();
  await expect(chart).toHaveAttribute("data-price-axis-count", "2");
  await expect(chart).toHaveAttribute("data-layout-height");
  const overflow = await page.evaluate(() => document.documentElement.scrollHeight <= document.documentElement.clientHeight);
  expect(overflow).toBeTruthy();
  for (const viewport of [{ width: 1920, height: 1080 }, { width: 2560, height: 1440 }]) {
    await page.setViewportSize(viewport);
    await expect(chart).toHaveAttribute("data-layout-height");
    expect(await page.evaluate(() => document.documentElement.scrollHeight <= document.documentElement.clientHeight)).toBeTruthy();
  }
});
