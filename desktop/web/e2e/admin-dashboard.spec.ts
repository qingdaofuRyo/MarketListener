import { expect, test, type Page } from "@playwright/test";

const adminUrl = "http://127.0.0.1:18768/";

test("admin address opens directly without a browser password or desktop shortcut", async ({ browser }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const page = await context.newPage();
    await page.route("**/api/admin/tdx-status", (route) => route.fulfill({ json: {
      scanning: false, stale: false, checkedAt: "2026-10-26T02:00:00Z",
      startupImport: { enabled: true }, roots: [],
    } }));
    await page.goto(adminUrl);
    await expect(page.getByRole("navigation", { name: "内部管理导航" })).toBeVisible();
    await expect(page.locator("#admin-login-password, #admin-login-gate")).toHaveCount(0);
    await expect(page.locator("#tdx-status-note")).toContainText("核验时间");
    expect((await page.request.get(adminUrl + "api/admin/state")).status()).toBe(200);
    await context.clearCookies();
    await page.reload();
    await expect(page.locator("#tdx-status-note")).toContainText("核验时间");
    expect((await page.request.get(adminUrl + "api/admin/state")).status()).toBe(200);
  } finally {
    await context.close();
  }
});

async function setupAdmin(page: Page, theme = "dark", tdxGroupCount = 1) {
  const state = {
    ready: true, owned: true, pid: 12345, state: "running", action: null, error: null,
    uptimeSeconds: 42, consoleUptimeSeconds: 60, backendUrl: "http://127.0.0.1:8767/",
    settings: { auto_start: true, auto_update: true, background_priority: true, theme },
    terminalJobs: {}, terminalBusy: false, csrfToken: "synthetic-console-token",
    role: "本机管理员", scope: "仅本机访问", version: "synthetic-test",
    startupEnabled: false, startupSupported: true,
    projectPath: "synthetic-workspace", dataPath: "synthetic-data-root",
    operationLabels: { MARKET_UPDATE: "更新行情", TDX_COVERAGE_CHECK: "检查通达信文件" },
  };
  let tasks = [{ operation_id: "a".repeat(32), kind: "MARKET_UPDATE", status: "QUEUED", created_at: "2026-10-01T00:00:00Z", detail: "synthetic queued task" }];
  const submitted: string[] = [];
  const headers: string[] = [];
  let rejectCancel = false;
  let inventoryFails = false;
  let tdxFails = false;
  let tdxImportStatus = "PARTIAL_FAILURE";
  await page.route("**/api/admin/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (method !== "GET") headers.push(route.request().headers()["x-marketlistener-console-token"]);
    if (path.endsWith("/state")) return route.fulfill({ json: state });
    if (path.includes("/service/")) {
      submitted.push(path);
      state.ready = !path.endsWith("/stop"); state.owned = state.ready; state.state = state.ready ? "running" : "stopped";
      return route.fulfill({ status: 202, json: { status: "ACCEPTED" } });
    }
    if (path.endsWith("/settings")) { Object.assign(state.settings, route.request().postDataJSON()); return route.fulfill({ json: { status: "SAVED" } }); }
    if (path.endsWith("/cancel")) {
      if (rejectCancel) return route.fulfill({ status: 409, json: { detail: "synthetic cancellation rejected" } });
      tasks = tasks.map((item) => ({ ...item, status: "CANCELLED" }));
      return route.fulfill({ json: { status: "CANCELLED" } });
    }
    if (path.endsWith("/tasks")) {
      if (method === "POST") { submitted.push(route.request().postDataJSON().kind); return route.fulfill({ status: 202, json: { created: true } }); }
      return route.fulfill({ json: { items: tasks } });
    }
    if (path.endsWith("/logs")) return route.fulfill({ json: { lines: ["INFO: synthetic task complete", "ERROR: synthetic provider failed", "synthetic <img src=x onerror=alert(1)>"], source: "backend" } });
    if (path.endsWith("/tdx-status")) {
      if (tdxFails) return route.fulfill({ status: 503, json: { detail: "synthetic file scan unavailable" } });
      return route.fulfill({ json: {
        checkedAt: "2026-10-07T02:30:00Z", scanning: false, stale: false,
        startupImport: { enabled: true },
        roots: [{ terminal: "futures", name: "通达信期货通", path: "synthetic-futures-root", available: true, lastFileModifiedAt: "2026-10-07T01:00:00Z", lastDownload: null,
          downloadSchedule: { currentSlot: "2026-10-06T23:00:00+08:00", nextCheckSlot: "2026-10-07T23:00:00+08:00", currentSlotComplete: true, needsReconciliation: false },
          lastImport: { status: tdxImportStatus, completedAt: "2026-10-06T02:00:00Z", rows: 0, importedFiles: 0, pendingFiles: 2, rejectedFiles: 2, quarantinedFiles: 3, detail: "synthetic incomplete import" },
          groups: Array.from({ length: tdxGroupCount }, (_, index) => ({ key: `domestic-5m-${index}`, label: "国内期货", period: "5m", source: "synthetic <img src=x onerror=alert(1)>", fileCount: 10, earliestDate: "2023-01-03", latestDate: "2026-09-30", commonLatestDate: "2026-09-30", commonLatestCount: 8, importedEarliestDate: "2023-01-03", importedLatestDate: "2026-09-24", invalidFileCount: 1 })),
        }],
      } });
    }
    if (path.endsWith("/inventory")) {
      if (inventoryFails) return route.fulfill({ status: 503, json: { detail: "synthetic inventory unavailable" } });
      return route.fulfill({ json: { summary: { rows: 42, instruments: 3 }, storage: { available: true, bytes: 2048 }, inventory: [{ market: "CN", assetType: "FUTURE", period: "1d", instruments: 3, rows: 42, earliestBarAt: "2026-01-01T00:00:00Z", latestBarAt: "2026-09-30T00:00:00Z", sourceDetails: [{ name: "通达信期货通本地缓存" }] }] } });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(adminUrl);
  await expect(page.getByRole("heading", { name: "客户服务已就绪" })).toBeVisible();
  return { state, submitted, headers, rejectCancel: () => { rejectCancel = true; }, inventoryFails: () => { inventoryFails = true; }, tdxFails: () => { tdxFails = true; }, tdxNoChange: () => { tdxImportStatus = "NO_CHANGE"; } };
}

test("internal console owns service controls and remains available after customer stop", async ({ page }) => {
  const fixture = await setupAdmin(page);
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "停止服务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "客户服务已停止" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "内部管理导航" })).toBeVisible();
  expect(fixture.headers).toContain("synthetic-console-token");
  await page.getByRole("button", { name: "启动客户服务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "客户服务已就绪" })).toBeVisible();
  expect(fixture.submitted).toEqual(["/api/admin/service/stop", "/api/admin/service/start"]);
});

test("offline backup preview lists impact before restore can start", async ({ page }) => {
  await setupAdmin(page);
  const backupName = "profile-20261011T010000Z-1234abcd.mlbackup";
  await page.route("**/api/admin/backups/profile**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/restore-preview")) return route.fulfill({ json: {
      status: "PREVIEW", planDigest: "a".repeat(64), replaceCount: 1, createCount: 0,
      removeCount: 1, paths: [{ path: "personal/watchlist.json", action: "REPLACE" },
        { path: "personal/old.json", action: "REMOVE" }],
    } });
    if (path.endsWith("/restore")) return route.fulfill({ status: 202, json: { id: "b".repeat(32), status: "QUEUED" } });
    if (path.endsWith("/" + "b".repeat(32))) return route.fulfill({ json: { status: "PASS" } });
    if (path.endsWith("/verify")) return route.fulfill({ json: { status: "VERIFIED", fileCount: 2 } });
    return route.fulfill({ json: { items: [{ name: backupName, createdAt: "2026-10-11T01:00:00Z", bytes: 4096 }], total: 1 } });
  });
  await page.locator('[data-page="settings"]').first().click();
  await page.getByRole("button", { name: "校验／恢复" }).click();
  const dialog = page.locator("#backup-restore-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "确认恢复" })).toBeDisabled();
  await dialog.getByRole("button", { name: "预览影响" }).click();
  await expect(dialog.locator("#backup-plan")).toContainText("将替换 1、创建 0、移除 1");
  await expect(dialog.getByRole("button", { name: "确认恢复" })).toBeEnabled();
  await dialog.getByRole("button", { name: "确认恢复" }).click();
  await expect(dialog.locator("#backup-plan")).toContainText("个人资料恢复完成");
});

test("internal data tasks show submission and queued cancellation failures", async ({ page }) => {
  const fixture = await setupAdmin(page);
  await page.getByRole("button", { name: "数据管理", exact: false }).click();
  await page.getByRole("button", { name: "检查通达信本地文件", exact: true }).click();
  await expect(page.locator("#notice")).toContainText("任务已排队");
  expect(fixture.submitted).toEqual(["TDX_COVERAGE_CHECK"]);
  await page.getByRole("button", { name: "任务队列", exact: false }).click();
  fixture.rejectCancel();
  await page.getByRole("button", { name: "取消排队" }).click();
  await expect(page.locator("#notice")).toContainText("synthetic cancellation rejected");
});

test("internal logs are searchable text and support an error-only filter", async ({ page }) => {
  await setupAdmin(page);
  await page.getByRole("button", { name: "运行日志", exact: false }).click();
  await expect(page.locator("#log-output")).toContainText("synthetic provider failed");
  expect(await page.locator("#log-output img").count()).toBe(0);
  await page.getByLabel("日志级别").selectOption("error");
  await expect(page.locator("#log-output")).not.toContainText("task complete");
  await page.getByLabel("搜索日志").fill("absent-synthetic-query");
  await expect(page.locator("#log-output")).toContainText("当前筛选下没有日志记录");
});

test("source inventory uses actual evidence and keeps stale results on failure", async ({ page }) => {
  const fixture = await setupAdmin(page);
  await page.getByRole("button", { name: "数据源盘点", exact: false }).click();
  await page.getByRole("button", { name: "刷新盘点", exact: true }).click();
  await expect(page.locator("#inventory-rows")).toHaveText("42");
  await expect(page.locator("#inventory-storage")).toHaveText("2 KiB");
  await expect(page.locator("#inventory-table")).toContainText("通达信期货通本地缓存");
  await expect(page.locator("#inventory-table")).toContainText("2026/01/01");
  await expect(page.locator("#inventory-table")).toContainText("2026/09/30");
  await expect(page.locator("#inventory-table tr td").nth(1)).toHaveText("3");
  fixture.inventoryFails();
  await page.getByRole("button", { name: "刷新盘点", exact: true }).click();
  await expect(page.locator("#inventory-status")).toContainText("读取失败");
  await expect(page.locator("#inventory-rows")).toHaveText("42");
});

test("F10 event inventory separates stored and recent batch counts", async ({ page }) => {
  await setupAdmin(page);
  await page.route("**/api/admin/manual-data-gaps**", route => route.fulfill({json:{generatedAt:'2026-10-31T10:30:00Z',items:[{
    key:'f10.revenue',label:'收入构成',status:'EVENT_DRIVEN',dateSemantics:'EVENT_DRIVEN',
    updateMode:'AUTO_START_AND_MANUAL',auditAt:'2026-10-31T10:28:51Z',missingDates:[],
    lastRecordedBatch:{completedAt:'2026-10-31T10:28:51Z',updated:25,remaining:5464,failed:0,stored:5564},
  },{key:'exchange.sse',label:'上交所官网日统计',status:'UNKNOWN',dateSemantics:'SOURCE_DEPENDENT',
    startDate:'2026-07-27',latestDate:'2026-10-30',missingDates:[],
    observedStatistics:{records:6786,categories:26,metrics:108,lastWrittenAt:'2026-10-31T10:28:51Z'},
  },{key:'exchange.szse-delisted',label:'深交所终止上市名单',status:'EVENT_DRIVEN',dateSemantics:'EVENT_DRIVEN',
    updateMode:'AUTO_DAILY_19_AND_MANUAL',
    startDate:'1993-03-01',latestDate:'2026-10-28',missingDates:[],
    listingSnapshot:{status:'PASS',records:189,totalRecords:189,fetchedAt:'2026-11-01T03:24:33Z',lastJob:{status:'PARTIAL_FAILURE',sourceOutcome:'FAILED',at:'2026-11-01T04:00:00Z'},review:{affectedCount:1,matchedCount:1,observedAt:'2026-11-01T04:00:00Z',lastCheckedAt:'2026-11-01T08:00:00Z',affectedIds:['CN.SZSE.STOCK.000006'],changes:[{instrumentId:'CN.SZSE.STOCK.000006',kind:'MISSING',previousDate:'2025-02-10',currentDate:null}],sourceUrl:'https://www.szse.cn/market/stock/suspend/index.html',truncated:false}},
  },{key:'macro.pmi-manufacturing',label:'制造业 PMI',status:'UNKNOWN',dateSemantics:'SOURCE_DEPENDENT',
    missingDates:[],observedSeries:{records:226,first:'2008-01',latest:'2026-10',frequency:'MONTHLY',lastWrittenAt:'2026-10-31T10:28:51Z'},
  },{key:'collector.macro-series',label:'宏观数据采集',status:'EVENT_DRIVEN',dateSemantics:'EVENT_DRIVEN',missingDates:[],
    collectorActivity:{status:'PARTIAL_FAILURE',rowsProcessed:500,sessionCompletedAt:'2026-10-31T10:28:51Z',freshnessWarnings:[{seriesId:'PMI_CAIXIN_MANUFACTURING',reason:'STALE_OVER_180_DAYS'}]},
  },{key:'quality.pytdx-30m',label:'通达信协议历史 30 分钟线待核验',status:'UNKNOWN',dateSemantics:'SOURCE_DEPENDENT',missingDates:[],
    qualityIssue:{partitions:1,rows:25,lastCutoff:'2026-08-21T00:00:00+08:00'},
  }]}}));
  await page.getByRole('button',{name:'数据管理',exact:false}).click();
  await expect(page.locator('#manual-gap-list')).toContainText('本次更新 25 家');
  await expect(page.locator('#manual-gap-list')).toContainText('待处理 5,464 家');
  await expect(page.locator('#manual-gap-list')).toContainText('本机已有记录 5,564 家');
  await expect(page.locator('#manual-gap-list')).toContainText('已入库 6,786 条指标记录');
  await expect(page.locator('#manual-gap-list')).toContainText('所选范围 189 条 · 快照总计 189 条');
  await expect(page.locator('#manual-gap-list')).toContainText('每日19:00检查 · 晚启动补查 · 可手动更新');
  await expect(page.locator('#manual-gap-list')).toContainText('摘牌日期范围仅反映名单中记录');
  await expect(page.locator('#manual-gap-list')).toContainText('本所更新失败，保留旧快照');
  await expect(page.locator('#manual-gap-list')).toContainText('名单历史记录发生变化：1 只待核');
  await expect(page.locator('#manual-gap-list')).toContainText('最近再次检查 2026/11/01 16:00');
  await page.getByText('查看变动明细（已列 1 / 1 条）').click();
  await expect(page.locator('#manual-gap-list')).toContainText('CN.SZSE.STOCK.000006 · 候选名单未见该代码；原摘牌日：2025-02-10，候选摘牌日：未提供');
  await expect(page.getByRole('link',{name:'查看交易所原名单'})).toHaveAttribute('href','https://www.szse.cn/market/stock/suspend/index.html');
  await expect(page.locator('#manual-gap-list')).toContainText('已观测日期 2026-07-27 至 2026-10-30');
  await expect(page.locator('#manual-gap-list')).toContainText('月度 · 观测范围 2008-01 至 2026-10');
  await expect(page.locator('#manual-gap-list')).toContainText('部分失败 · 处理 500 条（非新增数）');
  await expect(page.locator('#manual-gap-list')).toContainText('财新制造业 PMI：本次响应最新观测超过 180 天');
  await expect(page.locator('#manual-gap-list')).toContainText('相关历史分区 1 个 · 清单记录 25 行');
});

test("摘牌差异支持稳定分页，来源变化时保留当前页", async ({page}) => {
  await setupAdmin(page);
  const version = "a".repeat(64);
  const changes = Array.from({length:43}, (_, index) => ({instrumentId:`CN.SZSE.STOCK.${String(index+1).padStart(6,"0")}`,
    kind:"MISSING",previousDate:"2025-02-10",currentDate:null,previousName:`样本${index+1}`,currentName:null}));
  await page.route("**/api/admin/manual-data-gaps**", route => route.fulfill({json:{generatedAt:"2026-11-01T00:00:00Z",items:[{
    key:"exchange.szse-delisted",label:"深交所终止上市名单",status:"EVENT_DRIVEN",dateSemantics:"EVENT_DRIVEN",missingDates:[],
    listingSnapshot:{status:"PASS",records:189,totalRecords:189,review:{version,affectedCount:43,matchedCount:43,observedAt:"2026-11-01T00:00:00Z",changes:changes.slice(0,40),truncated:true,sourceUrl:"https://www.szse.cn/market/stock/suspend/index.html"}},
  }]}}));
  let changed = false;
  await page.route("**/api/admin/delisted-review/SZSE?**", route => {
    if (changed) return route.fulfill({status:409,json:{detail:"待核名单已变化，请重新查询"}});
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("version")).toBe(version);
    expect(params.get("start")).toBe("2025-01-01");
    const offset = Number(params.get("offset"));
    return route.fulfill({json:{version,offset,matchedCount:43,changes:changes.slice(offset,offset+40)}});
  });
  await page.getByRole("button",{name:"数据管理",exact:false}).click();
  await page.locator("#manual-gap-start").fill("2025-01-01");
  await page.getByRole("button",{name:"查询缺口"}).click();
  await page.getByText("查看变动明细（已列 40 / 43 条）").click();
  await page.getByRole("button",{name:"下一页",exact:true}).click();
  await expect(page.getByText("第 41–43 条，共 43 条")).toBeVisible();
  await expect(page.getByText(/CN\.SZSE\.STOCK\.000043/)).toBeVisible();
  changed = true;
  await page.getByRole("button",{name:"上一页",exact:true}).click();
  await expect(page.getByText("待核名单已变化，请重新查询；保留当前页。")).toBeVisible();
  await expect(page.getByText(/CN\.SZSE\.STOCK\.000043/)).toBeVisible();
  await expect(page.getByRole("button",{name:"上一页",exact:true})).toBeDisabled();
});

test("Silver publication diagnostics distinguish clear, pending and recovery review states", async ({ page }) => {
  await setupAdmin(page);
  let publication: Record<string, unknown> = {
    queryIndex: { ready: true }, models: { all: { snapshotStatus: "CURRENT" } },
    silverWrites: { status: "CLEAR", count: 0 }, silverRecovery: { status: "NOT_NEEDED" },
  };
  await page.route("**/api/admin/market-publication", route => route.fulfill({ json: publication }));
  await page.getByRole("button", { name: "环境诊断", exact: false }).click();
  await expect(page.locator("#index-build-status")).toHaveText("当前版本已就绪");
  await expect(page.locator("#silver-writes-status")).toHaveText("已结清，无待完成写入");
  await expect(page.locator("#silver-recovery-status")).toHaveText("无需恢复");

  publication = {
    queryIndex: { ready: false, build: {
      state: "RUNNING", stage: "AUDIT_SHA", completed: 2, total: 5, unit: "files",
      path: "C:\\private\\never-display",
    } }, models: {},
    silverWrites: { status: "PENDING", count: 2, path: "C:\\private\\never-display" },
    silverRecovery: { status: "REVIEW_REQUIRED", reason: "CATALOG_FILE_MISMATCH", path: "C:\\private\\never-display" },
  };
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.locator("#index-build-status")).toHaveText("核验原件 SHA · 2/5 个文件");
  await expect(page.locator("#silver-writes-status")).toHaveText("待核对 2 笔写入");
  await expect(page.locator("#silver-recovery-status")).toHaveText("需人工核查 · 目录与文件不一致");
  await expect(page.locator("#page-diagnostics")).not.toContainText("never-display");

  publication = {
    queryIndex: { ready: true }, models: {},
    silverWrites: { status: "INVALID", count: null },
    silverRecovery: { status: "RECOVERED", mode: "COMMITTED_OR_SUPERSEDED" },
  };
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.locator("#silver-writes-status")).toHaveText("修订记录无效，需核查");
  await expect(page.locator("#silver-recovery-status")).toHaveText("已自动恢复 · 已提交或被后续写入覆盖");

  publication = {
    queryIndex: { ready: false, build: {
      state: "FAILED", failureCategory: "AUDIT_FAILED", retryAfterSeconds: 30,
    } }, models: {}, silverWrites: { status: "CLEAR", count: 0 },
    silverRecovery: { status: "NOT_NEEDED" },
  };
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.locator("#index-build-status")).toHaveText("原件核验未通过 · 约 30 秒后重试");
});

test("TDX file dates distinguish downloaded from imported history and retain evidence on failure", async ({ page }) => {
  const fixture = await setupAdmin(page);
  const terminal = page.locator('[data-terminal-status="futures"]');
  await expect(terminal).toContainText("入库部分完成");
  await expect(terminal).toContainText("尚无界面完成时间");
  await expect(terminal).toContainText("本时段已完成 · 下次最早");
  await expect(terminal).toContainText("本次提交 0 根 K 线");
  await expect(terminal).toContainText("当前文件版本待入库 2 个");
  await expect(terminal).toContainText("本次质量拒绝 2 个（隔离总数 3，含拒绝）");
  const row = terminal.locator("tbody tr");
  await expect(row.locator("td").nth(1)).toHaveText("2023-01-03 → 2026-09-30");
  await expect(row.locator("td").nth(3)).toHaveText("2023-01-03 → 2026-09-24");
  await expect(row.locator("td").nth(2)).toContainText("8 / 10 个文件");
  await expect(row).toContainText("1 个无法解析");
  expect(await terminal.locator("img").count()).toBe(0);
  fixture.tdxFails();
  await page.getByRole("button", { name: "刷新文件日期", exact: true }).click();
  await expect(page.locator("#tdx-status-note")).toContainText("保留上次结果");
  await expect(row.locator("td").nth(3)).toHaveText("2023-01-03 → 2026-09-24");
});

test("large TDX inventories scroll within the card and retain position on refresh", async ({ page }) => {
  const fixture = await setupAdmin(page, "dark", 61);
  const region = page.getByRole("region", { name: "通达信期货通本地原件与入库日期" });
  await expect(region.locator("tbody tr")).toHaveCount(61);
  expect(await region.evaluate((element) => element.clientHeight)).toBeLessThanOrEqual(340);
  await region.evaluate((element) => { element.scrollTop = 800; });
  expect(await region.locator("th").first().evaluate((element) => getComputedStyle(element).position)).toBe("sticky");
  fixture.tdxNoChange();
  await page.getByRole("button", { name: "刷新文件日期", exact: true }).click();
  await expect(page.locator('[data-terminal-status="futures"] .badge')).toHaveText("入库无需增量");
  expect(await region.evaluate((element) => element.scrollTop)).toBe(800);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await region.evaluate((element) => element.clientHeight)).toBeLessThanOrEqual(320);
  await region.focus();
  await expect(region).toBeFocused();
});

for (const theme of ["dark", "light"]) {
  test(`admin theme and responsive layout ${theme}`, async ({ page }) => {
    await setupAdmin(page);
    await page.getByRole("button", { name: "设置", exact: false }).click();
    await page.getByRole("button", { name: theme === "dark" ? "深色" : "浅色", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.setViewportSize({ width: 1000, height: 740 });
    await page.getByRole("button", { name: "运行总览", exact: false }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`admin-${theme}.png`), fullPage: true });
  });
}
