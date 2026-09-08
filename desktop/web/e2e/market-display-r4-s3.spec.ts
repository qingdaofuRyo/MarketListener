import { expect, test, type Page } from "@playwright/test";
import * as echarts from "echarts";
import { createPinia, setActivePinia } from "pinia";
import { changeTone, formatMarketField, formatPercent, marketFieldTone } from "../src/domain/marketQuote";
import { formatAxisDate, formatCrosshairTime } from "../src/domain/chartTime";
import { resolveSecondaryMetric, subchartSeries } from "../src/domain/chartSubchart";
import { useMarketStore } from "../src/stores/market";
import { CROSS_PERIOD_ICON } from "../src/domain/chartIcons";

const periods = ["5m", "15m", "30m", "1h", "2h", "1d", "1w", "1mo", "3mo", "1y"];
const sample = { instrumentId: "CN.SHFE.FUTURE.TESTL8", symbol: "TESTL8", name: "期货测试主连", latestPrice: 12345.67,
  pctChange: 2, volume: 987654, amount: 9e11, amplitude: 6, dailyReturns: { "3": -1, "5": 3, "10": -4, "22": 0, "44": null } };

test("涨跌resolver区分有限正负、零和缺失", () => {
  expect([2, -1, 0, -0, null, undefined, NaN, Infinity].map(changeTone)).toEqual(["up", "down", "flat", "flat", "missing", "missing", "missing", "missing"]);
});

test("最新价跟随当日涨幅，窗口独立着色，普通字段中性", () => {
  expect(["latestPrice", "pctChange", "return3", "return5", "return10", "return22", "return44", "amount"].map(field => marketFieldTone(sample, field)))
    .toEqual(["up", "up", "down", "up", "down", "flat", "missing", "flat"]);
  for (const pctChange of [-2, 0, null]) expect(marketFieldTone({ ...sample, pctChange }, "latestPrice")).toBe(changeTone(pctChange));
  expect(marketFieldTone({ ...sample, latestPrice: null }, "latestPrice")).toBe("missing");
});

test("百分比统一使用百分数单位和固定两位，缺失不伪造0", () => {
  expect([2, -1, 0, -0, 3.126, null, Infinity].map(formatPercent)).toEqual(["+2.00%", "-1.00%", "0.00%", "0.00%", "+3.13%", "—", "—"]);
  expect(formatMarketField(sample, "pctChange")).toBe("+2.00%");
  expect(formatMarketField(sample, "return3")).toBe("-1.00%");
});

test("普通坐标日期简洁，十字保留真实日期星期时间及夜盘自然日", () => {
  const time = "2026-09-08T21:30:00+08:00";
  expect(formatAxisDate(time, "5m")).toBe("09-08");
  expect(formatAxisDate(time, "1y")).toBe("2026");
  expect(formatCrosshairTime(time, "5m")).toBe("2026-09-08 周二 21:30");
  expect(formatCrosshairTime(time, "1d")).toBe("2026-09-08 周二");
  expect(formatCrosshairTime("2026-02-30", "1d")).toBe("—");
  expect(formatAxisDate("invalid", "1d")).toBe("—");
});

test("持仓能力按真实证据判断，全零有效，快照缺失不否定历史能力", () => {
  expect(resolveSecondaryMetric(undefined, [{ openInterest: 0 }])).toBe("openInterest");
  expect(resolveSecondaryMetric({ fieldCapabilities: { openInterest: true } }, [])).toBe("openInterest");
  expect(resolveSecondaryMetric({ fieldCapabilities: { openInterest: false } }, [{ openInterest: 10 }])).toBe("openInterest");
  expect(resolveSecondaryMetric(undefined, [{ openInterest: null, amount: 12 }])).toBe("amount");
  expect(resolveSecondaryMetric(undefined, [{ openInterest: null }], true)).toBe("openInterest");
});

test("共享状态保存周期和标的能力，无效周期不改变选择", () => {
  setActivePinia(createPinia());
  const store = useMarketStore();
  store.select(sample.instrumentId);
  expect(store.selectDetailPeriod("1h", periods)).toBe(true);
  expect(store.selectDetailPeriod("1s", periods)).toBe(false);
  expect(store.detailPeriod).toBe("1h");
  expect(store.selectedId).toBe(sample.instrumentId);
  store.observeMeasures(sample.instrumentId, undefined, [{ openInterest: 0 }]);
  store.observeMeasures(sample.instrumentId, undefined, [{ openInterest: null }]);
  expect(store.interestInstruments).toEqual([sample.instrumentId]);
});

for (const metric of ["amount", "openInterest"] as const) {
  test(`${metric}真实ECharts副图独立坐标、图层、缺口与resize`, () => {
    const measures = [{ volume: 10, [metric]: 1e12 }, { volume: 0, [metric]: 0 }, { volume: null, [metric]: null }];
    const series = subchartSeries(measures, metric, ["red", "green", "gray"], "blue");
    const chart = echarts.init(null, undefined, { renderer: "svg", ssr: true, width: 800, height: 300 });
    try {
      chart.setOption({ animation: false, grid: [{show:false}, {left:40,right:40,top:30,bottom:30}],
        xAxis: [{type:"category",data:["a","b","c"]},{type:"category",gridIndex:1,data:["a","b","c"]}],
        yAxis: [{}, {gridIndex:1,min:0}, {gridIndex:1,min:0,position:"right"}], series });
      const option = chart.getOption().series as Array<{type:string;z:number;data:unknown[];yAxisIndex:number;connectNulls:boolean}>;
      expect(option.map(item => item.type)).toEqual(["bar", "line"]);
      expect(option[1].z).toBeGreaterThan(option[0].z);
      expect(option[1].data).toEqual([1e12, 0, null]);
      expect(option[1].connectNulls).toBe(false);
      for (const width of [800, 1366, 400]) {
        chart.resize({width,height:300});
        const volumeY = chart.convertToPixel({yAxisIndex:1},10) as number;
        const lineY = chart.convertToPixel({yAxisIndex:2},1e12) as number;
        expect(Math.abs(volumeY-lineY)).toBeLessThan(1);
        expect(chart.renderToSVGString()).toContain("<path");
      }
      chart.setOption({series:subchartSeries([{volume:0,[metric]:0},{volume:null,[metric]:null}],metric,["red"],"blue")});
      expect((chart.getOption().series as Array<{data:unknown[]}>)[1].data).toEqual([0,null]);
      expect(Number.isFinite(chart.convertToPixel({yAxisIndex:2},0) as number)).toBe(true);
    } finally { chart.dispose(); }
  });
}

async function setup(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const requests: string[] = [];
  const items = [sample, { ...sample, instrumentId: "CN.SSE.STOCK.TEST", symbol:"TEST", name:"证券测试", pctChange:-1, latestPrice:10, dailyReturns:{"3":2,"5":-1,"10":0,"22":-2,"44":1} }];
  await page.route("**/api/signals/**", route => route.fulfill({json:{items:[],events:[]}}));
  await page.route("**/api/strategy/**", route => route.fulfill({json:{items:[],instances:[]}}));
  await page.route("**/api/market/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("cache-status")) return route.fulfill({json:{dataVersion:"r4-s3"}});
    if (url.pathname.endsWith("categories")) return route.fulfill({json:{items:[{id:"cn-future-main",label:"国内期货主连合约"},{id:"cn-future-weighted",label:"国内期货加权合约"}]}});
    if (url.pathname.endsWith("/instruments")) return route.fulfill({json:{items,total:items.length}});
    if (url.pathname.endsWith("/drawings")) return route.fulfill({json:{items:[]}});
    const period = url.searchParams.get("period") || "1d";
    requests.push(period);
    const future = url.pathname.includes("FUTURE");
    const bars = Array.from({length:80}, (_, index) => ({
      barOpenTime: new Date(Date.UTC(2026, 5, 1+index, 10, 30)).toISOString(),
      open:100+index,close:101+index,high:102+index,low:99+index,
      volume:1e5+index,amount:1e12+index*1e8,openInterest:future && period === "1d" ? 1e6+index*100 : null,
      pctChange:index%2 ? 2 : -1,amplitude:4,change:index%2?2:-1,
    }));
    return route.fulfill({json:{bars,total:bars.length,size:bars.length,start:0,hasMore:false,period,availablePeriods:periods,drawings:[]}});
  });
  return {errors,requests};
}

test("列表控件宽度、黑体零padding、独立颜色和列拖拽/排序", async ({page}) => {
  await page.setViewportSize({width:1366,height:768});
  const state=await setup(page);
  await page.goto("/market/all/");
  const row=page.locator(".row-main").first();
  await expect(row.locator(".column-pctChange")).toHaveText("+2.00%");
  const widths=()=>page.locator(".all-toolbar").evaluate(el=>Array.from(el.children).map(child=>child.getBoundingClientRect().width));
  const initial=await widths();
  await page.locator(".all-toolbar .el-select").click();
  await page.getByRole("option",{name:"国内期货加权合约",exact:true}).click();
  expect(await widths()).toEqual(initial);
  await page.getByRole("textbox",{name:"搜索全部行情"}).fill("测试");
  await page.getByRole("button",{name:"查询",exact:true}).click();
  expect(await widths()).toEqual(initial);
  const headers=page.locator(".column-header");
  const headerStyles=await headers.evaluateAll(els=>els.map(el=>({left:getComputedStyle(el).paddingLeft,right:getComputedStyle(el).paddingRight,font:getComputedStyle(el).fontFamily})));
  expect(headerStyles.every(style=>style.left==="0px"&&style.right==="0px"&&style.font.includes("SimHei"))).toBe(true);
  const up=await row.locator(".column-pctChange").evaluate(el=>getComputedStyle(el).color);
  const down=await row.locator(".column-return3").evaluate(el=>getComputedStyle(el).color);
  expect(up).not.toBe(down);
  await expect(row.locator(".column-latestPrice")).toHaveCSS("color",up);
  await expect(row.locator(".column-return5")).toHaveCSS("color",up);
  await expect(row.locator(".column-return10")).toHaveCSS("color",down);
  const body=await page.locator(".market-page").evaluate(el=>getComputedStyle(el).getPropertyValue("--ml-text-primary").trim());
  const expectedBody=await page.evaluate(color=>{const el=document.createElement("span");el.style.color=color;document.body.append(el);const value=getComputedStyle(el).color;el.remove();return value;},body);
  await expect(row.locator(".column-amount")).toHaveCSS("color",expectedBody);
  await expect(page.getByRole("button",{name:"按名称排序"})).toHaveCSS("color",expectedBody);
  await expect(row.locator(".column-return22")).toHaveCSS("color",expectedBody);
  const sorter=page.getByRole("button",{name:"按近3日涨幅排序"});
  for (const arrow of ["↑","↓",""]) {await sorter.click();expect((await sorter.textContent())?.trim()).toBe(`近3日涨幅 ${arrow}`.trim());}
  await headers.nth(0).dragTo(headers.nth(1));
  await expect(headers.first()).toContainText("代码");
  const headerBoxes=await headers.evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,w:el.getBoundingClientRect().width})));
  const cellBoxes=await row.locator("span").evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,w:el.getBoundingClientRect().width})));
  expect(cellBoxes).toEqual(headerBoxes);
  await page.screenshot({path:"test-results/r4-s3-list.png"});
  expect(state.errors).toEqual([]);
});

test("Overlay固定槽、详情底部周期、左栏价格、主题/缩放/DPR及指针回归", async ({page,browser}) => {
  const state=await setup(page);
  await page.setViewportSize({width:1366,height:768});
  await page.goto("/market/all/");
  await expect(page.locator(".row-main").first()).toBeVisible();
  const canvas=page.locator(".boards .chart-root").first();
  const fields=page.locator(".boards .quote-values").first().locator("p");
  const initial=await fields.evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y,w:el.getBoundingClientRect().width})));
  expect(initial.every(box=>box.w===104)).toBe(true);
  expect(new Set(initial.map(box=>box.y)).size).toBe(2);
  const box=(await canvas.boundingBox())!;
  expect(box.width).toBeGreaterThan(600);
  await page.mouse.move(box.x+box.width*.4,box.y+box.height*.4);
  await page.mouse.move(box.x+box.width*.8,box.y+box.height*.5);
  expect(await fields.evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y,w:el.getBoundingClientRect().width})))).toEqual(initial);
  await page.mouse.move(10,10);
  await expect(fields.locator('strong').nth(1)).toHaveText("180");
  const quotePanel=page.locator(".boards .quote-panel").first();
  const quoteBox=(await quotePanel.boundingBox())!;
  await page.mouse.move(quoteBox.x+20,quoteBox.y+10);
  await page.keyboard.down("Shift");await page.mouse.wheel(0,160);await page.keyboard.up("Shift");
  await expect.poll(()=>quotePanel.evaluate(el=>el.scrollLeft)).toBeGreaterThan(0);
  await canvas.dblclick({position:{x:box.width*.5,y:box.height*.45}});
  const nav=page.getByRole("navigation",{name:"K线周期"});
  await expect(nav).toBeVisible();
  await expect(nav.getByRole("button")).toHaveCount(10);
  await expect(page.getByRole("combobox",{name:"详情 K 线周期"})).toHaveCount(0);
  const side=page.locator(".detail-instrument-row").first();
  expect((await side.locator("strong").boundingBox())!.y).toBeGreaterThan((await side.locator("b").boundingBox())!.y);
  await nav.getByRole("button",{name:"60分",exact:true}).click();
  await expect(nav.getByRole("button",{name:"60分",exact:true})).toHaveAttribute("aria-pressed","true");
  await expect.poll(()=>state.requests.includes("1h")).toBe(true);
  const detail=page.locator(".workbench-chart .chart-root");
  for (const theme of ["light","dark"]) {
    await page.evaluate(value=>localStorage.setItem("marketlistener.theme",value),theme);
    await page.reload();
    await expect(nav).toBeVisible();
    for (const viewport of [{width:1366,height:768},{width:1920,height:1080},{width:2560,height:1440}]) {
      await page.setViewportSize(viewport);
      await expect.poll(async()=>Math.round((await nav.boundingBox())!.y+(await nav.boundingBox())!.height)).toBe(viewport.height);
      expect(await page.evaluate(()=>document.documentElement.scrollHeight<=document.documentElement.clientHeight)).toBe(true);
      const plot=(await detail.boundingBox())!;
      expect(plot.y+plot.height).toBeLessThanOrEqual((await nav.boundingBox())!.y+1);
    }
  }
  await page.screenshot({path:"test-results/r4-s3-detail.png"});
  const drawPath=page.locator('.drawing-toolbar button[aria-label="跨周期"] svg path');
  await expect(drawPath).toHaveAttribute("d",CROSS_PERIOD_ICON);
  // CSS zoom exercises layout scaling separately from physical Canvas pixel density.
  for (const zoom of [1.25,1.5]) {
    await page.evaluate(value=>{document.documentElement.style.zoom=String(value);},zoom);
    expect(await page.evaluate(()=>document.documentElement.scrollHeight<=document.documentElement.clientHeight)).toBe(true);
  }
  expect(state.errors).toEqual([]);
  const context=await browser.newContext({viewport:{width:1366,height:768},deviceScaleFactor:2});
  try {
    const dense=await context.newPage();await setup(dense);await dense.goto("http://127.0.0.1:8767/market/all/");
    await expect(dense.locator(".board canvas").first()).toBeVisible();
    expect(await dense.locator(".board canvas").first().evaluate(el=>(el as HTMLCanvasElement).width/el.getBoundingClientRect().width)).toBeCloseTo(2,1);
  } finally { await context.close(); }
});
