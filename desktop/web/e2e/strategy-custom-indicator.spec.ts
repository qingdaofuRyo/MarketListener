import { expect, test } from "@playwright/test";

test("strategy custom indicator is available from the market-detail indicator action", async ({ page }) => {
  const customIndicator = {
    resourceKind: "indicator",
    id: "indicator.user.two_day_ma",
    version: 1,
    versionedId: "indicator.user.two_day_ma@1",
    name: "自定义两日均线",
    englishName: "Custom Two Day Moving Average",
    category: "trend",
    categoryLabel: "趋势",
    description: "策略页创建的安全指标模板副本。",
    placement: "overlay",
    supportedAssetTypes: ["STOCK", "ETF"],
    origin: "custom",
    status: "active",
    createdAt: "2026-09-05T08:00:00+08:00",
    updatedAt: "2026-09-05T08:00:00+08:00",
    calculationId: "indicator.ma",
    dependencies: [{ id: "technical.sma", version: 1 }],
    parameters: [{ name: "lookback", type: "integer", default: 2, minimum: 1, maximum: 500 }],
    plots: [{ id: "ma", type: "line" }],
  };
  await page.route("**/api/strategy/indicators**", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: { items: [customIndicator], total: 1 } });
      return;
    }
    await route.fulfill({ json: { ...customIndicator, id: "indicator.user.copied", name: "自定义两日均线 副本" } });
  });

  await page.goto("/strategy/?section=indicator");
  await page.getByText("自定义两日均线", { exact: true }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toContainText("indicator.user.two_day_ma@1");
  await drawer.getByRole("button", { name: "添加到当前图表" }).click();

  await page.waitForURL(/\/market\/\?indicator=indicator\.user.two_day_ma&version=1/);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("marketlistener.pendingIndicator"))).toContain(
    "indicator.user.two_day_ma",
  );
});

test("strategy page creates a custom indicator then publishes an immutable next version", async ({ page }) => {
  const customIndicator = {
    resourceKind: "indicator",
    id: "indicator.user.two_day_ma",
    version: 1,
    versionedId: "indicator.user.two_day_ma@1",
    name: "自定义两日均线",
    englishName: "Custom Two Day Moving Average",
    category: "trend",
    categoryLabel: "趋势",
    description: "策略页创建的安全指标模板副本。",
    placement: "overlay",
    supportedAssetTypes: ["STOCK", "ETF"],
    origin: "custom",
    status: "active",
    createdAt: "2026-09-05T08:00:00+08:00",
    updatedAt: "2026-09-05T08:00:00+08:00",
    calculationId: "indicator.ma",
    dependencies: [{ id: "technical.sma", version: 1 }],
    parameters: [{ name: "lookback", type: "integer", default: 2, minimum: 1, maximum: 500 }],
    plots: [{ id: "ma", type: "line" }],
  };
  let copyBody: unknown;
  let updateBody: Record<string, unknown> | undefined;
  await page.route("**/api/strategy/indicators**", async (route) => {
    if (route.request().method() === "GET") {
      const pathname = new URL(route.request().url()).pathname;
      await route.fulfill({
        json: pathname.endsWith(`/indicators/${customIndicator.id}`)
          ? customIndicator
          : { items: [customIndicator], total: 1 },
      });
      return;
    }
    copyBody = route.request().postDataJSON();
    await route.fulfill({ status: 201, json: { ...customIndicator, id: "indicator.user.created" } });
  });
  await page.route("**/api/strategy/indicator-resources/indicator.user.two_day_ma", async (route) => {
    updateBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { ...customIndicator, version: 2, versionedId: "indicator.user.two_day_ma@2" } });
  });

  await page.goto("/strategy/?section=indicator");
  await page.getByRole("button", { name: "新建自定义指标" }).click();
  const createDialog = page.getByRole("dialog", { name: "新建自定义指标" });
  await createDialog.getByLabel("显示名称").fill("我的均线");
  await createDialog.getByRole("button", { name: "创建" }).click();
  await expect.poll(() => copyBody).toEqual({ displayName: "我的均线" });

  await page.getByText("自定义两日均线", { exact: true }).click();
  const detail = page.getByRole("dialog", { name: "自定义两日均线" });
  await detail.getByRole("button", { name: "编辑为新版本" }).click();
  const editDialog = page.getByRole("dialog", { name: "编辑自定义指标（新版本）" });
  await editDialog.getByLabel("显示名称").fill("两日均线 v2");
  await editDialog.getByRole("button", { name: "发布新版本" }).click();
  await expect.poll(() => updateBody).toMatchObject({
    id: "indicator.user.two_day_ma",
    version: 2,
    display_name: "两日均线 v2",
    definition: { calculation_id: "indicator.ma" },
  });
});
