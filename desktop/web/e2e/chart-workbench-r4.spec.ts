import { test, expect, type Page } from "@playwright/test";
import { heikinAshi } from "../src/domain/chartPresentation";

const id = "CN.SSE.STOCK.600000";
const bars = Array.from({ length: 80 }, (_, i) => ({
  barOpenTime: new Date(Date.UTC(2026, 5, i + 1)).toISOString(),
  open: 12 + Math.sin(i / 8), high: 13 + Math.sin(i / 8), low: 11 + Math.sin(i / 8),
  close: 12.4 + Math.sin(i / 8), volume: 10000 + i * 50,
}));
const catalog = [
  { id: "indicator.ma", name: "移动平均线", englishName: "Moving Average", placement: "overlay" },
  { id: "indicator.rsi", name: "相对强弱指数", englishName: "RSI", placement: "pane" },
].map((item) => ({ ...item, resourceKind: "indicator", version: 1, status: "active", categoryLabel: "趋势", supportedAssetTypes: ["STOCK"], parameters: [{ name: "lookback", default: 14, type: "integer", minimum: 2, maximum: 500 }] }));

export async function setup(page: Page, testBars = bars) {
  page.on("pageerror", (error) => { throw error; });
  await page.setViewportSize({ width: 1440, height: 960 });
  const instrument = { instrumentId: id, symbol: "600000", name: "浦发银行", assetType: "STOCK", market: "CN", latestPrice: 12 };
  let drawings: Array<{ type: string; points: Array<{ time: string; price: number }> }> = [];
  let saves = 0;
  const requests: Array<{ size: number }> = [];
  await page.route("**/api/strategy/indicators**", (route) => route.fulfill({ json: { items: catalog } }));
  await page.route("**/api/signals/**", (route) => route.fulfill({json:{items:[],events:[]}}));
  await page.route("**/api/strategy/definitions**", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/market/**", async (route) => {
    const url = new URL(route.request().url()), path = url.pathname;
    let result: unknown = {};
    if (path.endsWith("cache-status")) result = { dataVersion: "r4" };
    else if (path.endsWith("categories")) result = { items: [{ id: "cn-future-main", label: "国内期货主连合约" }, { id: "cn-future-weighted", label: "国内期货加权合约" }] };
    else if (path.endsWith("/instruments")) result = { items: [instrument], total: 1 };
    else if (path.endsWith("drawings/batch")) result = { items: { [id]: drawings } };
    else if (path.endsWith("bars/batch")) result = { items: { [id]: testBars } };
    else if (path.endsWith("/drawings")) {
      if (route.request().method() === "PUT") { drawings = route.request().postDataJSON().items; saves++; }
      result = { items: drawings };
    } else if (path.endsWith("indicator-series")) {
      const body = route.request().postDataJSON(); requests.push(body);
      result = { instances: body.instances.map((item: { definitionId: string }) => ({ ...item, status: "ready", plots: [{ id: "value", type: "line" }], series: { value: bars.slice(0, body.size).map((bar) => bar.close) } })) };
    } else if (path.endsWith("/bars")) result = { bars: url.searchParams.get("limit") === "1" ? [testBars.at(-1)] : testBars, availablePeriods: ["1d", "1h"], start: 0, size: testBars.length, total: testBars.length, hasMore: false };
    else result = { bars:testBars, drawings, series: {}, total: testBars.length, start: 0, size: testBars.length, period: url.searchParams.get("period") || "1d", availablePeriods: ["1d", "1h"], hasMore: false };
    await route.fulfill({ json: result });
  });
  await page.goto("/market/");
  await page.locator(".row-main").first().dblclick();
  await expect(page.locator(".workbench-overlay")).toBeVisible();
  await expect(page.locator(".workbench-chart .el-loading-mask")).toHaveCount(0);
  return { drawings: () => drawings, saves: () => saves, requests };
}

test("chart presentation formulas use historical bars", () => {
  const result = heikinAshi([{ open: 10, high: 14, low: 8, close: 12 }, { open: 12, high: 15, low: 9, close: 14 }]);
  expect(result[0]).toMatchObject({ open: 11, close: 11, high: 14, low: 8 });
  expect(result[1]).toMatchObject({ open: 11, close: 12.5 });
  expect(heikinAshi(bars.slice(0, 10))).toEqual(heikinAshi(bars).slice(0, 10));

});

test("brush preserves a free curve with magnet enabled; laser expires without saving", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "吸附", exact: true }).click();
  await page.getByRole("button", { name: "笔刷与激光笔", exact: true }).click();
  await page.getByRole("menuitem", { name: "笔刷", exact: true }).click();
  const box = (await page.locator(".workbench-chart .chart-root").boundingBox())!;
  const startX = box.x + box.width * .3, startY = box.y + box.height * .3;
  await page.mouse.move(startX, startY); await page.mouse.down();
  for (let i = 1; i <= 35; i++) await page.mouse.move(startX + i * 5, startY + Math.sin(i / 7) * 48);
  await page.mouse.up();
  await expect.poll(state.saves).toBe(1);
  const points = state.drawings()[0].points;
  expect(points.length).toBeGreaterThan(4);
  const barTimes = new Set(bars.map((bar) => Date.parse(bar.barOpenTime)));
  expect(points.filter((point) => !barTimes.has(Date.parse(point.time))).length).toBeGreaterThan(3);
  expect(new Set(points.map((point) => point.price)).size).toBeGreaterThan(4);
  await page.getByRole("button", { name: "笔刷与激光笔", exact: true }).click();
  await page.getByRole("menuitem", { name: "激光笔", exact: true }).click();
  const laser = page.getByLabel("激光笔画布");
  await page.mouse.move(startX, startY); await page.mouse.down();
  await page.mouse.move(startX + 180, startY + 80, { steps: 15 }); await page.mouse.up();
  await expect.poll(async () => Number(await laser.getAttribute("data-point-count"))).toBeGreaterThan(2);
  await expect(laser).toHaveAttribute("data-point-count", "0", { timeout: 4000 });
  expect(state.saves()).toBe(1);
  await page.keyboard.press("Escape");
  await expect(page.locator(".workbench-overlay")).toHaveCount(0);
});

test("indicator library, icon types, direct controls, quotes and replay work together", async ({ page }) => {
  const state = await setup(page);
  const overlay = page.locator(".workbench-overlay");
  await overlay.getByRole("button", { name: /^指标/ }).click();
  const dialog = page.getByRole("dialog", { name: "指标", exact: true });
  expect((await dialog.boundingBox())!.width).toBeGreaterThan(750);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(overlay).toBeVisible();
  await overlay.getByRole("button", { name: /^指标/ }).click();
  await dialog.getByRole("button", { name: "收藏移动平均线", exact: true }).click();
  await page.screenshot({ path: "test-results/r4-indicator-library.png", animations: "disabled" });
  await dialog.getByRole("button", { name: /移动平均线 Moving Average/ }).click();
  const legend = page.getByLabel("已添加指标");
  await expect(legend).toContainText("移动平均线");
  const crossPeriodPath = await overlay.locator('.drawing-toolbar button[aria-label="跨周期"] path').getAttribute("d");
  await expect(legend.getByRole("button", {name:"跨周期移动平均线"}).locator("path")).toHaveAttribute("d", crossPeriodPath!);
  await expect.poll(() => state.requests.length).toBeGreaterThan(0);
  await legend.getByRole("button", { name: "跨周期移动平均线" }).click();
  await expect(legend.getByLabel("颜色移动平均线")).toHaveCount(0);
  await legend.getByRole("button", { name: "设置移动平均线" }).click();
  await expect(dialog.getByText("颜色", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await overlay.getByRole("button", { name: /^指标/ }).click();
  await expect(dialog.getByRole("button", { name: "取消收藏移动平均线" })).toBeVisible();
  await dialog.getByRole("button", { name: /相对强弱指数 RSI/ }).click();
  const chart = page.locator(".workbench-chart .chart-root");
  await expect(chart).toHaveAttribute("data-indicator-pane-count", "1");
  const periods = page.getByRole("navigation", { name: "K线周期" });
  await expect(page.getByRole("combobox", { name: "详情 K 线周期" })).toHaveCount(0);
  await periods.getByRole("button", { name: "60分", exact: true }).click();
  await expect(chart).toHaveAttribute("data-indicator-count", "1");
  await periods.getByRole("button", { name: "日线", exact: true }).click();
  await expect(chart).toHaveAttribute("data-indicator-count", "2");
  for (const [label, value] of [["空心K线图", "hollow"], ["平均K线图", "heikin"], ["折线图", "line"], ["面积图", "area"], ["实心K线图", "candles"]]) {
    await overlay.getByRole("button", { name: "K线类型", exact: true }).click();
    await page.getByRole("menuitem", { name: label, exact: true }).click();
    await expect(chart).toHaveAttribute("data-chart-type", value);
  }
  const periodBox = (await periods.boundingBox())!;
  const overlayChartBox = (await chart.boundingBox())!;
  expect(periodBox.y).toBeGreaterThanOrEqual(overlayChartBox.y + overlayChartBox.height - 1);
  await overlay.getByRole("button", { name: "回放", exact: true }).click();
  const chartBox=(await chart.boundingBox())!;
  await page.mouse.click(chartBox.x+chartBox.width*.4,chartBox.y+chartBox.height*.3);
  await expect(overlay.getByRole("button",{name:"播放",exact:true})).toBeEnabled();
  const initialSize=state.requests.at(-1)!.size;
  await overlay.getByRole("button", { name: "下一根" }).click();
  await expect.poll(() => state.requests.at(-1)?.size).toBe(initialSize+1);
  await overlay.getByRole("button", { name: "播放", exact: true }).click();
  await expect.poll(() => state.requests.at(-1)?.size || 0).toBeGreaterThan(initialSize+1);
  await overlay.getByRole("button", { name: "暂停", exact: true }).click();
  await overlay.getByRole("button", { name: "退出回放" }).click();
  await expect.poll(() => state.requests.at(-1)?.size).toBe(80);
  await expect(overlay.getByRole("button",{name:"警报",exact:true})).toHaveCount(0);
  await expect(overlay.getByRole("button",{name:"指标",exact:true})).toBeVisible();
  const quote=overlay.locator(".workbench-chart .quote-panel");
  for(const field of ["振幅","涨跌","额","持仓量","沉淀资金","总市值","流通市值"]) await expect(quote).toContainText(field);
  await expect(quote).not.toContainText("00:00");
  await expect(quote.locator('[data-field="收"] strong')).not.toHaveCSS("color","rgb(0, 0, 0)");
  await page.screenshot({ path: "test-results/r4-chart-workbench.png", fullPage: false });
  await page.keyboard.press("Escape");
  await expect(overlay).toHaveCount(0);
});

test("trend line uses two logical anchors and survives reopening", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "线条工具", exact: true }).click();
  await page.getByRole("menuitem", { name: "趋势线", exact: true }).click();
  const chart = page.locator(".workbench-chart .chart-root");
  const box = (await chart.boundingBox())!;
  await page.mouse.click(box.x + box.width * .3, box.y + box.height * .3);
  expect(state.saves()).toBe(0);
  await page.mouse.click(box.x + box.width * .6, box.y + box.height * .4);
  await expect.poll(state.saves).toBe(1);
  expect(state.drawings()[0].type).toBe("trend");
  expect(state.drawings()[0].points).toHaveLength(2);
  await page.reload();
  await expect(page.locator(".workbench-overlay")).toBeVisible();
  await expect(chart).toHaveAttribute("data-drawing-count", "1");
});
test("grouped risk reward drawings use three prices and list board periods are dropdowns",async({page})=>{
  const state=await setup(page);
  await page.getByRole("button",{name:"图形与盈亏比",exact:true}).click();
  await page.getByRole("menuitem",{name:"多头盈亏比",exact:true}).click();
  const chart=page.locator(".workbench-chart .chart-root"),box=(await chart.boundingBox())!;
  await page.mouse.click(box.x+box.width*.35,box.y+box.height*.3);
  await page.mouse.click(box.x+box.width*.45,box.y+box.height*.4);
  await page.mouse.click(box.x+box.width*.6,box.y+box.height*.18);
  await expect.poll(state.saves).toBe(1);
  expect(state.drawings()[0].type).toBe("long_position");
  expect(state.drawings()[0].points).toHaveLength(3);
  await page.keyboard.press("Escape");
  const topPeriod = page.getByRole("combobox",{name:"上看板 K 线周期",exact:true});
  await topPeriod.selectOption("1h");
  await expect(topPeriod).toHaveValue("1h");
});

test("market filters use only opening signals and ongoing cycles show later operations",async({page})=>{
  await setup(page);
  await page.keyboard.press('Escape');
  let scans=0;
  await page.route('**/api/signals/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('definitions')) {await route.fulfill({json:{items:[{id:'entry',displayName:'突破开仓',action:'open',enabled:true},{id:'add',displayName:'回调加仓',action:'add',enabled:true},{id:'close',displayName:'转弱平仓',action:'close',enabled:true}]}});return;}
    if(path.endsWith('/scan'))scans++;
    const event={instrumentId:id,strategyName:scans>1?'回调加仓':'突破开仓',action:scans>1?'add':'open',direction:'long',at:bars[79].barOpenTime,barAt:bars[79].barOpenTime,period:'1d',price:12};
    await route.fulfill({json:{items:scans?[{instrumentId:id,name:'浦发银行',symbol:'600000',direction:'long',openingStrategyId:'entry',openedAt:event.at,latestSignal:event}]:[],events:scans?[event]:[],scanned:1,total:1,nextOffset:null}});
  });
  await page.reload();
  await page.goto('/market/targets/');
  const filters=page.getByRole('navigation',{name:'目标行情策略筛选'});
  await expect(filters).toContainText('突破开仓');
  await expect(filters).not.toContainText('回调加仓');
  await page.getByRole('button',{name:'检查开仓信号',exact:true}).click();
  await expect(page.locator('.target-results')).toContainText('浦发银行');
  await page.getByRole('button',{name:'更新已开仓监控',exact:true}).click();
  await expect(page.locator('.target-monitor-panel')).toContainText('回调加仓');
  await page.locator('.target-results').getByRole('button',{name:/浦发银行/}).click();
  await expect(page.locator('.workbench-chart .chart-root')).toHaveAttribute('data-strategy-marker-count','1');
});
