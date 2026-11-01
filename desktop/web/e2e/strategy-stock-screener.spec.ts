import { expect, test } from "@playwright/test";

test("条件与 Python 脚本可编辑，并按批次展示筛选结果", async ({ page }) => {
  const requests: Array<{source:string; minMarketCapYi:number; offset:number; instrumentIds?:string[]}> = [];
  let saved: {id:string;name:string;source:string;version:number;versionCount:number;savedAt:string} | null = null;
  await page.route("**/api/strategy/stock-screen/scripts**", async route => {
    if (route.request().method() === "GET" && route.request().url().endsWith("/versions")) {
      return route.fulfill({json:{id:saved?.id,items:saved ? [saved] : []}});
    }
    if (route.request().method() === "GET") return route.fulfill({ json: {items: saved ? [saved] : []} });
    const body = route.request().postDataJSON() as {name:string;source:string};
    saved = {id:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",name:body.name,source:body.source,version:1,versionCount:1,savedAt:"2026-10-30T12:00:00Z"};
    return route.fulfill({status:201,json:saved});
  });
  await page.route("**/api/strategy/stock-screen", async route => {
    const body = route.request().postDataJSON() as {source:string; minMarketCapYi:number; offset:number; instrumentIds?:string[]};
    requests.push(body);
    if (body.instrumentIds) return route.fulfill({json:{asOf:"2026-10-30",silverRevision:"revision-1",universeHash:"universe-1",offset:0,processed:1,universeCount:100,hasMore:false,requestedInstrumentIds:body.instrumentIds,belowMarketCap:0,evaluated:1,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",unavailable:[],matches:[{instrumentId:body.instrumentIds[0],name:"万科Ａ",upDays20:11,return5d:.11,marketCapYi:210,quoteTime:"20261030150000",close:9,priceBasis:"TDX_RAW",adjustmentEvidence:null}]}});
    await route.fulfill({ json: { asOf: "2026-10-30", silverRevision: "revision-1", universeHash: "universe-1", offset: body.offset,
      processed: 50, universeCount: 100, hasMore: body.offset === 0, belowMarketCap: body.offset === 0 ? 48 : 49, evaluated: 1, poolEvidence: [], corporateActionEvidence: {status: "LOCAL_RECORDS_UNVERIFIED", sha256: "fixture", localModifiedAt: "2026-10-30"}, scope: "测试范围",
      matches: body.offset === 0 ? [{ instrumentId: "CN.SZSE.STOCK.000001", name: "平安银行", upDays20: 12,
        return5d: .12, marketCapYi: 220, quoteTime: "20261030150000", close: 10, priceBasis: "TENCENT_QFQ_VERIFIED_AGAINST_TDX_RAW", adjustmentEvidence: {actionDays: ["2026-10-23"]} }] : [],
      unavailable: body.offset === 0 ? [{ instrumentId: "CN.SZSE.STOCK.000002", name: "万科Ａ", reason: "行情缺失", quoteName:"来源名称", quoteTime:"20261030090000", lastLocalDay:"2026-10-29" }] : [],
    } });
  });
  await page.goto("/strategy/?section=strategy");
  await expect(page.getByRole("heading", { name: "A 股条件选股" })).toBeVisible();
  await page.getByRole("button", { name: "仅扫描第一批" }).click();
  await expect(page.getByText("已扫描 50 / 100 只", { exact: false })).toBeVisible();
  await expect(page.getByText("平安银行", { exact: true })).toBeVisible();
  await expect(page.getByText("前复权（已核原价）", { exact: true })).toBeVisible();
  expect(requests[0].source).toContain("up_days(close, 20)");
  expect(requests[0].source).toContain("period_return(close, 5) > 0.1");
  expect(requests[0].minMarketCapYi).toBe(200);
  await page.getByText("查看待核标的（1）").click();
  await expect(page.getByText("CN.SZSE.STOCK.000002：行情缺失", {exact:false})).toBeVisible();
  await expect(page.getByText(/本机名称：万科Ａ；腾讯名称：来源名称；腾讯报价时间：2026-10-30 09:00；本地日线截至：2026-10-29/)).toBeVisible();
  await page.getByRole("button", { name: "扫描下一批" }).click();
  await expect(page.getByText("已扫描 100 / 100 只", { exact: false })).toBeVisible();
  expect(requests[1].offset).toBe(50);
  await page.getByRole("button", { name: "重试待核" }).click();
  await expect(page.getByText("本次重试 1 只，恢复 1 只，仍待核 0 只。")).toBeVisible();
  await expect(page.getByText("万科Ａ", {exact:true})).toBeVisible();
  expect(requests[2].instrumentIds).toEqual(["CN.SZSE.STOCK.000002"]);
  await page.getByText("Python 表达式", { exact: true }).click();
  const editor = page.getByRole("textbox", { name: "选股 Python 表达式" });
  await editor.fill("value = up_days(close, 20)\nsignal = value > 15");
  await expect(page.getByText("条件已修改，以下是旧条件的结果；请重新扫描。")).toBeVisible();
  await page.getByRole("button", { name: "保存脚本" }).click();
  await expect(page.getByText("脚本已保存到本机个人资料（v1）")).toBeVisible();
  await page.reload();
  await page.getByText("Python 表达式", { exact: true }).click();
  await page.getByText("载入已保存脚本", { exact: true }).click();
  await page.getByRole("option", { name: "我的选股脚本 · v1" }).click();
  await expect(editor).toHaveValue("value = up_days(close, 20)\nsignal = value > 15");
  await page.getByRole("button", { name: "历史版本" }).click();
  await expect(page.getByRole("dialog", { name: "选股脚本历史版本" })).toBeVisible();
  await page.getByRole("button", { name: "载入编辑" }).click();
  await expect(page.getByText("已载入 v1，保存时会创建新版本")).toBeVisible();
  await editor.fill("value = up_days(close, 20)\nsignal = value > 18");
  page.once("dialog", dialog => void dialog.dismiss());
  await page.getByRole("tab", { name: "函数库" }).click();
  await expect(editor).toHaveValue("value = up_days(close, 20)\nsignal = value > 18");
});

test("旧浏览器脚本可载入并另存到本机个人资料", async ({ page }) => {
  const oldSource = "value = up_days(close, 20)\nsignal = value > 11";
  await page.addInitScript(source => {
    localStorage.setItem("marketlistener.stockScreener.scripts.v1", JSON.stringify({"旧脚本":source}));
  }, oldSource);
  let savedSource = "";
  await page.route("**/api/strategy/stock-screen/scripts**", async route => {
    if (route.request().method() === "GET") return route.fulfill({json:{items:[]}});
    const body = route.request().postDataJSON() as {name:string;source:string};
    savedSource = body.source;
    return route.fulfill({status:201,json:{id:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",name:body.name,source:body.source,version:1,versionCount:1,savedAt:"2026-10-30T12:00:00Z"}});
  });
  await page.goto("/strategy/?section=strategy");
  await page.getByText("Python 表达式", { exact:true }).click();
  await page.getByText("载入已保存脚本", { exact:true }).click();
  await page.getByRole("option", { name:"旧脚本", exact:true }).click();
  await expect(page.getByRole("textbox", {name:"选股 Python 表达式"})).toHaveValue(oldSource);
  await page.getByRole("button", {name:"保存脚本"}).click();
  await expect(page.getByText("脚本已保存到本机个人资料（v1）")).toBeVisible();
  expect(savedSource).toBe(oldSource);
  expect(await page.evaluate(() => localStorage.getItem("marketlistener.stockScreener.scripts.v1"))).toContain("旧脚本");
});

test("待核重试时来源版本变化不会混入旧结果", async ({ page }) => {
  await page.route("**/api/strategy/stock-screen", async route => {
    const body = route.request().postDataJSON() as {instrumentIds?:string[]};
    const retry = !!body.instrumentIds;
    return route.fulfill({json:{asOf:"2026-10-30",silverRevision:retry ? "revision-new" : "revision-old",universeHash:"universe-1",offset:0,processed:retry ? 1 : 50,universeCount:50,hasMore:false,requestedInstrumentIds:body.instrumentIds ?? null,belowMarketCap:retry ? 0 : 49,evaluated:retry ? 1 : 0,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",matches:[],unavailable:retry ? [] : [{instrumentId:"CN.SZSE.STOCK.000002",name:"万科Ａ",reason:"报价待核"}]}});
  });
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("待核 1 只", {exact:false})).toBeVisible();
  await page.getByRole("button", {name:"重试待核"}).click();
  await expect(page.getByText("数据版本或来源已变化，以下是旧结果；请重新扫描。")).toBeVisible();
  await expect(page.getByRole("button", {name:"重试待核"})).toBeDisabled();
  await expect(page.getByText("待核 1 只", {exact:false})).toBeVisible();
});

test("已核验摘牌股票单独计数、展示公告并可恢复进度", async ({ page }) => {
  await page.route("**/api/strategy/stock-screen", route => route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:0,processed:50,universeCount:100,hasMore:true,belowMarketCap:48,evaluated:1,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",matches:[],unavailable:[],excludedDelisted:[{instrumentId:"CN.SSE.STOCK.601059",name:"信达证券",delistedAt:"2026-10-19",announcedAt:"2026-10-12",sourceUrl:"https://www.sse.com.cn/disclosure/example"}]}}));
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("已核验摘牌 1 只", {exact:false})).toBeVisible();
  await page.reload();
  await page.getByText("查看已核验摘牌（1）").click();
  await expect(page.getByText("信达证券（CN.SSE.STOCK.601059）：2026-10-19 摘牌", {exact:false})).toBeVisible();
  await expect(page.getByRole("link", {name:"查看公告"})).toHaveAttribute("href", "https://www.sse.com.cn/disclosure/example");
});

test("更新交易所名单后旧扫描结果明确失效", async ({ page }) => {
  await page.route("**/api/strategy/stock-screen/delisted-sources**", route => {
    const items = [{exchange:"SSE",status:"PASS",count:148,fetchedAt:"2026-11-01T00:00:00Z"}, {exchange:"SZSE",status:"PASS",count:189,fetchedAt:"2026-11-01T00:00:00Z"}];
    return route.fulfill({json:{items}});
  });
  await page.route("**/api/strategy/stock-screen", route => route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:0,processed:50,universeCount:100,hasMore:true,belowMarketCap:49,evaluated:1,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",matches:[],unavailable:[]}}));
  await page.goto("/strategy/?section=strategy");
  await expect(page.getByText(/上交所 148 条/)).toBeVisible();
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await page.getByRole("button", {name:"更新沪深摘牌名单"}).click();
  await expect(page.getByText("数据版本或来源已变化，以下是旧结果；请重新扫描。")).toBeVisible();
  await expect(page.getByRole("button", {name:"扫描下一批"})).toBeDisabled();
});

test("交易所历史名单变化时展示受影响条目数量", async ({ page }) => {
  await page.route("**/api/strategy/stock-screen/delisted-sources", route => route.fulfill({json:{items:[
    {exchange:"SSE",status:"PASS",count:148,fetchedAt:"2026-11-01T00:00:00Z",review:{affectedCount:2}},
    {exchange:"SZSE",status:"PASS",count:189,fetchedAt:"2026-11-01T00:00:00Z"},
  ]}}));
  await page.goto("/strategy/?section=strategy");
  await expect(page.getByText(/上交所 148 条.*历史变动 2 只待核/)).toBeVisible();
});

test("刷新后恢复当前标签页扫描进度并从正确位置续扫", async ({ page }) => {
  const offsets: number[] = [];
  await page.route("**/api/strategy/stock-screen", async route => {
    const body = route.request().postDataJSON() as {offset:number}; offsets.push(body.offset);
    await route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:body.offset,
      processed:50,universeCount:100,hasMore:body.offset===0,belowMarketCap:49,evaluated:1,poolEvidence:[],
      corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},
      scope:"测试范围",matches:[],unavailable:[]}});
  });
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
  await page.reload();
  await expect(page.getByText("已恢复本标签页上次扫描进度；结果尚未重新核验。继续扫描或重试时会检查数据版本和来源。")).toBeVisible();
  await page.getByRole("button", {name:"扫描下一批"}).click();
  await expect(page.getByText("已扫描 100 / 100 只", {exact:false})).toBeVisible();
  expect(offsets).toEqual([0, 50]);
});

test("恢复扫描时同时恢复用户自定义条件", async ({ page }) => {
  await page.route("**/api/strategy/stock-screen", route => route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:0,processed:50,universeCount:100,hasMore:true,belowMarketCap:49,evaluated:1,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",matches:[],unavailable:[]}}));
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("spinbutton", {name:"上涨天数阈值"}).fill("12");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
  await page.reload();
  await expect(page.getByRole("spinbutton", {name:"上涨天数阈值"})).toHaveValue("12");
  await expect(page.getByRole("button", {name:"扫描下一批"})).toBeEnabled();
});

test("损坏的浏览器进度不会作为扫描结果展示", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("marketlistener.stockScreener.progress.v1", "{bad-json"));
  await page.goto("/strategy/?section=strategy");
  await expect(page.getByRole("heading", {name:"A 股条件选股"})).toBeVisible();
  await expect(page.getByText("已恢复本标签页上次扫描进度", {exact:false})).toHaveCount(0);
});

test("用户保存进度后可关闭标签并在同一浏览器载入续扫", async ({ page, context }) => {
  const offsets: number[] = [];
  await context.route("**/api/strategy/stock-screen", async route => {
    const body = route.request().postDataJSON() as {offset:number}; offsets.push(body.offset);
    await route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:body.offset,
      processed:50,universeCount:100,hasMore:body.offset===0,belowMarketCap:49,evaluated:1,poolEvidence:[],
      corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},
      scope:"测试范围",matches:[],unavailable:[]}});
  });
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("spinbutton", {name:"上涨天数阈值"}).fill("12");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
  await page.getByRole("button", {name:"保存进度到浏览器"}).click();
  await expect(page.getByText("扫描进度已保存到此浏览器；关闭标签页后可手动载入，不随个人资料备份。")).toBeVisible();
  await page.close();
  const fresh = await context.newPage();
  await fresh.goto("/strategy/?section=strategy");
  await expect(fresh.getByText("已扫描 50 / 100 只", {exact:false})).toHaveCount(0);
  await fresh.getByRole("button", {name:"载入已保存进度"}).click();
  await expect(fresh.getByRole("spinbutton", {name:"上涨天数阈值"})).toHaveValue("12");
  await expect(fresh.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
  await fresh.getByRole("button", {name:"扫描下一批"}).click();
  await expect(fresh.getByText("已扫描 100 / 100 只", {exact:false})).toBeVisible();
  expect(offsets).toEqual([0,50]);
});

test("无法读取的持久进度不会覆盖当前结果", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("marketlistener.stockScreener.checkpoint.v1", "{invalid"));
  await page.route("**/api/strategy/stock-screen", route => route.fulfill({json:{asOf:"2026-10-30",silverRevision:"rev",universeHash:"uni",offset:0,processed:50,universeCount:100,hasMore:true,belowMarketCap:49,evaluated:1,poolEvidence:[],corporateActionEvidence:{status:"LOCAL_RECORDS_UNVERIFIED",sha256:"fixture",localModifiedAt:"2026-10-30"},scope:"测试范围",matches:[],unavailable:[]}}));
  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", {name:"仅扫描第一批"}).click();
  await expect(page.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
  await page.getByRole("button", {name:"载入已保存进度"}).click();
  await expect(page.getByText("保存的进度无法读取或格式无效，当前结果未更改。")).toBeVisible();
  await expect(page.getByText("已扫描 50 / 100 只", {exact:false})).toBeVisible();
});

test("一次性启动地址在路由更新及刷新后不会再次使用", async ({ page }) => {
  let bootstrapCalls = 0;
  await page.route("**/api/local-session/bootstrap", route => {
    bootstrapCalls += 1;
    return route.fulfill({json:{audience:"product",csrfToken:"synthetic",expiresIn:300}});
  });
  await page.goto("/strategy/?section=strategy#ml-bootstrap=synthetic-one-use");
  await expect(page.getByRole("heading", {name:"A 股条件选股"})).toBeVisible();
  await page.getByRole("tab", {name:"函数库"}).click();
  await page.getByRole("tab", {name:"我的策略"}).click();
  await expect(page).not.toHaveURL(/ml-bootstrap/);
  await page.reload();
  await expect(page.getByRole("heading", {name:"A 股条件选股"})).toBeVisible();
  expect(bootstrapCalls).toBe(1);
});
