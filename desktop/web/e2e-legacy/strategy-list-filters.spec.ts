import { expect, test, type Page } from "@playwright/test";

async function choose(page: Page, placeholder: string, option: string): Promise<void> {
  await page.locator(".strategy-toolbar").getByText(placeholder, { exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}

test("strategy list filters source, category, asset, run mode, and favorites", async ({ page }) => {
  const common = {
    version: 1, strategyVersion: "1", status: "active", scriptKind: "structured_v1",
    updatedAt: "2026-09-05T00:00:00+00:00", inputs: [], parameters: {}, baseTimeframe: "1d", backtestStatus: "not_run",
  };
  await page.route("**/api/strategy/definitions", (route) => route.fulfill({
    json: { items: [
      { ...common, strategyId: "strategy.builtin_fixture", versionedId: "strategy.builtin_fixture@1", displayName: "内置趋势", category: "trend", origin: "builtin", supportedAssetTypes: ["STOCK"], runMode: "backtest" },
      { ...common, strategyId: "strategy.custom_fixture", versionedId: "strategy.custom_fixture@1", displayName: "自定义突破", category: "breakout", origin: "custom", supportedAssetTypes: ["ETF"], runMode: "paper" },
      { ...common, strategyId: "strategy.community_fixture", versionedId: "strategy.community_fixture@1", displayName: "社区趋势", category: "trend", origin: "community", status: "disabled", supportedAssetTypes: ["FUTURE"], runMode: "backtest" },
    ], total: 3 },
  }));
  await page.route("**/api/strategy/execution/capabilities", (route) => route.fulfill({
    json: { capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false, modes: [] },
  }));

  await page.goto("/strategy/?section=strategy");
  const customRow = page.getByRole("row", { name: /自定义突破/ });
  await expect(page.getByRole("row", { name: /内置趋势/ })).toBeVisible();
  await expect(customRow).toBeVisible();

  await choose(page, "全部来源", "自定义");
  await expect(customRow).toBeVisible();
  await expect(page.getByRole("row", { name: /内置趋势/ })).toBeHidden();
  await choose(page, "全部分类", "breakout");
  await choose(page, "全部资产", "ETF");
  await choose(page, "全部模式", "模拟");
  await expect(customRow).toBeVisible();

  await customRow.getByRole("button", { name: "收藏" }).click();
  await expect(customRow.getByRole("button", { name: "取消收藏" })).toBeVisible();
  await page.locator(".strategy-toolbar").getByRole("button", { name: "收藏", exact: true }).click();
  await expect(customRow).toBeVisible();
});
