import { expect, test } from "@playwright/test";
import {
  nextSortState,
  sortMarketInstruments,
  weekdayLabel,
  type SortableMarketInstrument,
} from "../src/domain/marketList";

const bars = Array.from({ length: 50 }, (_, index) => ({
  barOpenTime: new Date(Date.UTC(2026, 8, index + 1)).toISOString(),
  open: 10 + index / 10,
  high: 11 + index / 10,
  low: 9 + index / 10,
  close: 10.5 + index / 10,
  volume: 1000 + index,
  amount: 10000 + index * 10,
}));

function instrument(index: number) {
  return {
    instrumentId: `CN.CFFEX.FUTURE.TEST${index}`,
    symbol: `TEST${index}`,
    name: `测试主连${String(index).padStart(3, "0")}`,
    assetType: "FUTURE",
    market: "CN",
    latestPrice: index,
    pctChange: index % 2 ? index / 100 : -index / 100,
    amplitude: index / 50,
    lastVolume: index * 100,
    lastAmount: index * 1000,
    lastOpenInterest: index * 10,
    capitalDeposit: index * 10000,
    totalMarketCap: null,
    floatMarketCap: null,
    dailyReturns: { "3": index / 10, "5": index / 11, "10": index / 12, "22": index / 13, "44": index / 14 },
  };
}

test("R4 list obtains the full category, virtualizes rows, and carries state into detail", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const items = Array.from({ length: 503 }, (_, index) => instrument(index + 1));
  await page.route("**/api/signals/**", (route) => route.fulfill({ json: { items: [], events: [] } }));
  await page.route("**/api/strategy/**", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/market/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("cache-status")) return route.fulfill({ json: { dataVersion: "market-r4" } });
    if (path.endsWith("categories")) {
      return route.fulfill({ json: { items: [
        { id: "cn-future-main", label: "国内期货主连合约" },
        { id: "cn-future-weighted", label: "国内期货加权合约" },
      ] } });
    }
    if (path.endsWith("/instruments")) {
      const pageNumber = Number(url.searchParams.get("page") || "1");
      const pageSize = Number(url.searchParams.get("pageSize") || "500");
      return route.fulfill({ json: {
        items: items.slice((pageNumber - 1) * pageSize, pageNumber * pageSize),
        total: items.length,
        page: pageNumber,
        pageSize,
        dataVersion: "market-r4",
      } });
    }
    if (path.endsWith("bars/batch")) return route.fulfill({ json: { items: Object.fromEntries(items.slice(0, 2).map((item) => [item.instrumentId, bars])) } });
    if (path.endsWith("drawings/batch")) return route.fulfill({ json: { items: {} } });
    if (path.endsWith("/bars")) return route.fulfill({ json: { bars, availablePeriods: ["1d", "1h"], total: bars.length, start: 0, size: bars.length, hasMore: false } });
    return route.fulfill({ json: { items: [], bars, availablePeriods: ["1d", "1h"], total: bars.length, start: 0, size: bars.length, hasMore: false } });
  });

  await page.goto("/market/");
  await expect(page).toHaveURL(/\/market\/all\/\?category=cn-future-main/);
  await expect(page.getByRole("button", { name: "卡片视图" })).toHaveCount(0);
  await expect(page.locator(".instrument-row").first()).toBeVisible();
  expect(await page.locator(".instrument-row").count()).toBeLessThan(100);
  expect(await page.locator(".instrument-row").count()).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "按近22日涨幅排序" })).toBeVisible();

  const list = page.locator(".instrument-list");
  await list.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect(page.getByText("测试主连503", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "按最新价排序" }).click();
  await expect(page.getByRole("button", { name: "按最新价排序" })).toContainText("↑");
  await page.getByRole("button", { name: "按最新价排序" }).click();
  await expect(page.getByRole("button", { name: "按最新价排序" })).toContainText("↓");
  await page.getByRole("button", { name: "按最新价排序" }).click();
  await expect(page.getByRole("button", { name: "按最新价排序" })).not.toContainText(/[↑↓]/);

  await page.locator(".row-main").first().click();
  await page.keyboard.press("ArrowDown");
  await expect(page.locator(".instrument-row.active")).toHaveCount(1);
  const beforeInputKey = await page.locator(".instrument-row.active .row-main").textContent();
  const search = page.getByPlaceholder("查询代码或名称");
  await search.fill("测试");
  await search.press("ArrowDown");
  await expect(page.locator(".instrument-row.active .row-main")).toHaveText(beforeInputKey || "");
  await search.fill("");

  await list.evaluate((element) => element.dispatchEvent(new WheelEvent("wheel", { deltaY: 180, shiftKey: true, bubbles: true })));
  expect(await list.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await page.locator(".row-main").first().dblclick();
  await expect(page).toHaveURL(/\/market\/instrument\//);
  await expect(page.locator(".detail-instrument-list .detail-instrument-row")).toHaveCount(503);
  await expect(page.locator(".workbench-overlay")).toBeVisible();
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).overflowY)).toBe("hidden");
  const toolbar = (await page.locator(".drawing-toolbar").boundingBox())!;
  expect(toolbar.x).toBeGreaterThan(1000);
});

test("R4 list domain rules retain trading-date wording and stable three-state sorting", () => {
  const records: SortableMarketInstrument[] = [
    { instrumentId: "b", latestPrice: 10 },
    { instrumentId: "a", latestPrice: 10 },
    { instrumentId: "none", latestPrice: null },
  ];
  const ascending = nextSortState({ field: null, direction: null }, "latestPrice");
  expect(ascending).toEqual({ field: "latestPrice", direction: "asc" });
  expect(sortMarketInstruments(records, ascending).map((item) => item.instrumentId)).toEqual(["b", "a", "none"]);
  const descending = nextSortState(ascending, "latestPrice");
  expect(sortMarketInstruments(records, descending).map((item) => item.instrumentId)).toEqual(["b", "a", "none"]);
  expect(nextSortState(descending, "latestPrice")).toEqual({ field: null, direction: null });
  expect(weekdayLabel("2026-09-04T09:30:00+08:00")).toBe("周五");
});
