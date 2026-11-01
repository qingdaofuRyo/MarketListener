import { expect, test } from "@playwright/test";

test("industry directory, graph and explanation use a safe published snapshot", async ({ page }) => {
  await page.goto("/industry/");
  await expect(page.getByRole("heading", { name: "产业链", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "半导体", exact: true })).toBeVisible();
  await page.getByText("国民经济行业分类 · 完整目录").click();
  await page.getByRole("searchbox", { name: "搜索国标行业分类" }).fill("0111");
  await expect(page.locator(".classification-list button", { hasText: "0111" })).toBeVisible();
  await page.getByText("上市公司制造业辅助分类").click();
  await expect(page.locator(".csrc-group")).toHaveCount(10);
  await expect(page.getByRole("img", { name: "半导体产业关系图" })).toBeVisible();
  await expect(page.getByText("硅片在芯片制造中充当上游材料。")).toBeVisible();
  const nodes = page.getByLabel("产业节点列表");
  await nodes.getByRole("button", { name: /硅片/ }).click();
  const explanation = page.getByRole("region", { name: "产业解释" });
  await expect(explanation).toContainText("硅片");
  await expect(explanation).toContainText("芯片");
  await expect(explanation).toContainText("投入");
  await expect(nodes.getByRole("button", { name: /硅片/ })).toHaveAttribute("aria-pressed", "true");
  await explanation.getByRole("button", { name: "返回产业说明" }).click();
  await expect(explanation.getByRole("heading", { name: "产业说明" })).toBeVisible();
  await page.getByRole("searchbox", { name: "搜索产业链" }).fill("半导体");
  await expect(page.locator(".chain-list button")).toHaveCount(1);
  const content = await page.locator("body").innerText();
  expect(content).not.toContain("synthetic-industry.pdf");
  expect(content).not.toContain("Silicon wafers are processed");
  expect(content).not.toContain("第 1 页");
  const catalog = await (await page.request.get("/api/industry/catalog")).json();
  const id = catalog.chains.find((item: { name: string }) => item.name === "半导体").id;
  const detail = await (await page.request.get(`/api/industry/chains/${id}`)).text();
  expect(detail).not.toContain("evidenceText");
  expect(detail).not.toContain("synthetic-industry.pdf");
});

test("unreviewed industry entries stay visibly pending", async ({ page }) => {
  await page.goto("/industry/");
  await page.getByRole("searchbox", { name: "搜索产业链" }).fill("风电");
  await page.locator(".chain-list button", { hasText: "风电" }).first().click();
  await expect(page.getByText("详细关系仍待核验")).toBeVisible();
  await expect(page.getByText("暂无经过核验的产品关系")).toBeVisible();
  await page.getByRole("searchbox", { name: "搜索产业链" }).fill("半导体");
  await page.locator(".chain-list button", { hasText: "半导体" }).click();
  await expect(page.getByRole("img", { name: "半导体产业关系图" }).locator("canvas").first()).toBeVisible();
});

test("industry selection ignores late responses from an earlier selection", async ({ page }) => {
  await page.goto("/industry/");
  await expect(page.getByRole("heading", { name: "半导体", exact: true })).toBeVisible();
  const catalog = await (await page.request.get("/api/industry/catalog")).json();
  const wind = catalog.chains.find((item: { name: string }) => item.name === "风电");
  expect(wind).toBeTruthy();
  let release!: () => void;
  let started!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const entered = new Promise<void>(resolve => { started = resolve; });
  await page.route(`**/api/industry/chains/${wind.id}`, async route => {
    const response = await route.fetch();
    started();
    await gate;
    await route.fulfill({ response });
  });
  await page.locator(".chain-list button", { hasText: "风电" }).first().click();
  await entered;
  await page.locator(".chain-list button", { hasText: "半导体" }).first().click();
  const completed = page.waitForResponse(response => response.url().endsWith(`/api/industry/chains/${wind.id}`));
  release();
  await completed;
  await expect(page.getByRole("heading", { name: "半导体", exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: "半导体产业关系图" })).toBeVisible();
});

test("classification failure does not hide the industry catalog", async ({ page }) => {
  await page.route("**/api/industry/classification", route => route.fulfill({ status: 503 }));
  await page.goto("/industry/");
  await expect(page.getByRole("heading", { name: "半导体", exact: true })).toBeVisible();
  await page.getByText("国民经济行业分类 · 完整目录").click();
  await expect(page.getByText("部分行业分类暂时不可用，可稍后重试。")).toBeVisible();
});

test("unpublished industry data has a clear empty state", async ({ page }) => {
  await page.route("**/api/industry/catalog", route => route.fulfill({ json: {
    status: "NOT_PUBLISHED", chains: [], sectors: [],
  } }));
  await page.goto("/industry/");
  await expect(page.locator('[data-test="industry-unpublished"]')).toContainText("尚未发布");
  await expect(page.locator(".chain-list")).toContainText("尚未发布");
  await expect(page.getByRole("region", { name: "产业关系图" })).toHaveCount(0);
  const explorer = page.getByRole("region", { name: "行业分类浏览" });
  await expect(explorer.locator(".explorer-grid button")).toHaveCount(20);
  await explorer.locator(".explorer-grid button").first().click();
  await expect(explorer.getByRole("navigation", { name: "分类路径" })).toContainText("农、林、牧、渔业");
  await expect(explorer.locator(".explorer-grid button").first()).toBeVisible();
  await explorer.getByRole("searchbox", { name: "搜索行业代码或名称" }).fill("0111");
  await expect(explorer.locator(".explorer-grid button")).toHaveCount(1);
  await explorer.locator(".explorer-grid button").first().click();
  await expect(explorer.getByRole("navigation", { name: "分类路径" })).toContainText("0111");
  await expect(explorer).toContainText("具体产业关系仍待核验");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(explorer.getByRole("searchbox", { name: "搜索行业代码或名称" })).toBeVisible();
  const bounds = await explorer.boundingBox();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
});

test("quant method explanation is separate from data readiness and local results", async ({ page }) => {
  await page.goto("/quant/");
  await expect(page.getByRole("heading", { name: "金工", exact: true })).toBeVisible();
  await expect(page.getByText("尚无本地结果。先选择方法并通过数据检查。")).toBeVisible();
  await page.getByRole("searchbox", { name: "搜索金工方法" }).fill("滚动历史波动率");
  await expect(page.locator(".method-list button")).toHaveCount(1);
  await page.locator(".method-list button").click();
  await page.getByPlaceholder("例如 CN.SSE.STOCK.600000").fill("SYNTHETIC.NONE");
  await page.locator(".run-form input[type=date]").first().fill("2026-09-01");
  await page.locator(".run-form input[type=date]").last().fill("2026-09-30");
  await page.getByPlaceholder("自行填写").fill("20");
  await page.getByRole("button", { name: "检查可用性" }).click();
  await expect(page.getByText("所选区间没有本机日线数据")).toBeVisible();
  await expect(page.getByText("尚无本地结果。先选择方法并通过数据检查。")).toBeVisible();
  await expect(page.getByRole("button", { name: "运行本地价格统计" })).toHaveCount(0);
});

test("quant public research is readable and changing parameters clears readiness", async ({ page }) => {
  await page.goto('/quant/');
  await page.getByRole('searchbox',{name:'搜索金工方法'}).fill('横截面动量');
  await page.locator('.method-list button').click();
  await expect(page.getByRole('region',{name:'方法说明'})).toContainText('公开研究原理概览');
  await expect(page.getByRole('region',{name:'方法说明'})).toContainText('尚未实现');
  await expect(page.getByRole('region',{name:'方法说明'})).toContainText('当前不可运行');
  await page.getByRole('searchbox',{name:'搜索金工方法'}).fill('滚动历史波动率');
  await page.locator('.method-list button').click();
  await page.getByPlaceholder('例如 CN.SSE.STOCK.600000').fill('CN.SSE.STOCK.600000');
  await page.locator('.run-form input[type=date]').first().fill('2026-01-05');
  await page.locator('.run-form input[type=date]').last().fill('2026-03-20');
  await page.getByPlaceholder('自行填写').fill('20');
  await page.route('**/api/quant/methods/*/readiness**', route => route.fulfill({json:{ready:true,reasons:[],barCount:30,firstDay:'2026-01-05',lastDay:'2026-03-20'}}));
  await page.getByRole('button',{name:'检查可用性'}).click();
  await expect(page.getByRole('button',{name:'运行本地价格统计'})).toBeVisible();
  await page.getByPlaceholder('自行填写').fill('21');
  await expect(page.getByRole('button',{name:'运行本地价格统计'})).toHaveCount(0);
  await expect(page.locator('.gate')).toHaveCount(0);
});

test("quant readiness ignores an old result after parameters change", async ({page}) => {
  await page.goto('/quant/');
  await page.getByRole('searchbox',{name:'搜索金工方法'}).fill('滚动历史波动率');
  await page.locator('.method-list button').click();
  await page.getByPlaceholder('例如 CN.SSE.STOCK.600000').fill('CN.SSE.STOCK.600000');
  await page.locator('.run-form input[type=date]').first().fill('2026-01-05');
  await page.locator('.run-form input[type=date]').last().fill('2026-03-20');
  await page.getByPlaceholder('自行填写').fill('20');
  let release!: () => void, started!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const entered = new Promise<void>(resolve => { started = resolve; });
  await page.route('**/api/quant/methods/*/readiness**', async route => {
    started(); await pending;
    await route.fulfill({json:{ready:true,reasons:[],barCount:30,firstDay:'2026-01-05',lastDay:'2026-03-20'}});
  });
  await page.getByRole('button',{name:'检查可用性'}).click();
  await entered;
  await page.getByPlaceholder('自行填写').fill('21');
  const response = page.waitForResponse(value => value.url().includes('/readiness'));
  release(); await response;
  await expect(page.locator('.gate')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'运行本地价格统计'})).toHaveCount(0);
});
