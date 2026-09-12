import { expect, test } from "@playwright/test";
import { formatBytes, occupiedSources, share, type InventoryPayload } from "../src/domain/sourceDashboard";
const inventory: InventoryPayload = {
  summary: {rows: 42, instruments: 3},
  inventory: [{categoryKey:"CN:FUTURE:1d",market:"CN",assetType:"FUTURE",period:"1d",rows:42,instruments:3,
    sources:["tdx_futures_local"],sourceDetails:[{providerId:"tdx_futures_local",name:"通达信期货通本地缓存"}],
    earliestBarAt:"2026-01-01T00:00:00+08:00",latestBarAt:"2026-09-11T00:00:00+08:00"}],
  datasets: [{datasetId:"registered-only",name:"未采集",source:"joinquant",rows:42}],
  storage: {available:true,bytes:2048,files:1,missingFiles:0,groups:[{market:"CN",assetType:"FUTURE",period:"1d",bytes:2048,files:1}]}
};
test("inventory helpers use actual source evidence, bytes and safe shares", () => {
  expect(occupiedSources(inventory).map(item=>item.id)).toEqual(["tdx_futures_local"]);
  expect(occupiedSources({...inventory,inventory:[{...inventory.inventory[0],rows:0}]})).toEqual([]);
  expect(formatBytes(2048)).toBe("2 KiB");
  for (const value of [null, undefined, NaN, Infinity, -1]) expect(formatBytes(value)).toBe("—");
  expect(formatBytes(0)).toBe("0 B");
  expect(formatBytes(0.5)).toBe("0.5 B");
  expect(share(1,0)).toBe(0);
  expect(share(1,4)).toBe(25);
});
test("dashboard moves queue to logs and cancellation errors remain visible", async ({page}) => {
  await page.route("**/api/health", route=>route.fulfill({json:{stats:{run_count:2}}}));
  await page.route("**/api/logs?**", route=>route.fulfill({json:{items:[],total:0}}));
  await page.route("**/api/operations", route=>route.fulfill({json:{items:[{operation_id:"fixture",kind:"MARKET_UPDATE",status:"QUEUED",created_at:"2026-09-12T00:00:00Z"}]}}));
  await page.route("**/api/operations/fixture/cancel", route=>route.fulfill({status:409,json:{detail:"already running"}}));
  await page.goto("/");
  await expect(page.getByRole("heading",{name:"仪表盘",exact:true})).toBeVisible();
  await expect(page.getByRole("heading",{name:"任务队列"})).toHaveCount(0);
  await page.getByRole("link",{name:"日志与任务队列"}).click();
  await expect(page.getByRole("heading",{name:"任务队列"})).toBeVisible();
  await page.getByRole("button",{name:"取消",exact:true}).click();
  await expect(page.getByText("取消失败，请刷新确认任务状态")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading",{name:"任务队列"})).toBeVisible();
});
for (const theme of ["light","dark"]) {
  test(`Chinese read-only inventory dashboard ${theme}`, async ({page}) => {
    await page.addInitScript(theme=>localStorage.setItem("marketlistener.theme",theme),theme);
    let writes = 0;
    page.on("request",request=>{if(request.url().includes("/api/data-sources") && request.method() !== "GET") writes++;});
    await page.route("**/api/data-sources/inventory",route=>route.fulfill({json:inventory}));
    await page.setViewportSize({width:1366,height:768});
    await page.goto("/data-sources/");
    await expect(page.getByText("通达信期货通本地缓存")).toBeVisible();
    await expect(page.getByText("2 KiB").first()).toBeVisible();
    await expect(page.locator("meter")).toHaveCount(2);
    await expect(page.locator("select,input")).toHaveCount(0);
    await expect(page.getByText("聚宽")).toHaveCount(0);
    await expect(page.getByText("joinquant")).toHaveCount(0);
    await page.getByRole("button",{name:"刷新盘点"}).click();
    expect(writes).toBe(0);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path: test.info().outputPath("inventory.png"), fullPage: true});
  });
}
test("not-ready and failed inventory must not appear as empty stock", async ({page}) => {
  await page.route("**/api/data-sources/inventory",route=>route.fulfill({json:{...inventory,inventory:[],summary:{rows:0,instruments:0},storage:{available:false,bytes:null,groups:[]}}}));
  await page.goto("/data-sources/");
  await expect(page.getByText("本地行情清单尚未就绪", {exact:false})).toBeVisible();
  await expect(page.locator(".source-metrics")).toHaveCount(0);
  await page.route("**/api/data-sources/inventory",route=>route.fulfill({status:500,json:{detail:"fixture failure"}}));
  await page.getByRole("button",{name:"刷新盘点"}).click();
  await expect(page.getByText("本地数据盘点加载失败", {exact:false})).toBeVisible();
});
test("queued cancellation refreshes results and event log errors are visible", async ({page}) => {
  let cancelled = false;
  await page.route("**/api/logs?**", route=>route.fulfill({status:500,json:{detail:"fixture log failure"}}));
  await page.route("**/api/operations", route=>route.fulfill({json:{items:[{operation_id:"fixture",kind:"MARKET_UPDATE",status:cancelled ? "CANCELLED" : "QUEUED",created_at:"2026-09-12T00:00:00Z"}]}}));
  await page.route("**/api/operations/fixture/cancel", route=>{cancelled=true; return route.fulfill({json:{status:"CANCELLED"}});});
  await page.goto("/logs/");
  await page.getByRole("button",{name:"取消",exact:true}).click();
  await expect(page.getByRole("button",{name:"取消",exact:true})).toHaveCount(0);
  await expect(page.getByText("事件日志加载失败，请重试")).toBeVisible();
  expect(cancelled).toBe(true);
});
