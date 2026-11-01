import { expect, test } from "@playwright/test";

test("community strategy is identifiable but has no chart, edit, or execution entry point", async ({ page }) => {
  await page.route("**/api/strategy/definitions", (route) => route.fulfill({
    json: { items: [{
      strategyId: "strategy.community_fixture", version: 1, strategyVersion: "1", versionedId: "strategy.community_fixture@1",
      displayName: "社区只读样例", origin: "community", status: "disabled", scriptKind: "structured_v1",
      updatedAt: "2026-09-05T00:00:00+00:00", inputs: [], parameters: {}, supportedAssetTypes: ["STOCK"], baseTimeframe: "1d",
    }], total: 1 },
  }));
  await page.route("**/api/strategy/execution/capabilities", (route) => route.fulfill({
    json: { capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false, modes: [] },
  }));

  await page.goto("/strategy/?section=strategy");
  const row = page.getByRole("row", { name: /社区只读样例/ });
  await expect(row).toContainText("社区 · 待审核");
  await expect(row.getByRole("button", { name: "加载到图表" })).toBeDisabled();
  await expect(row.getByRole("button", { name: "编辑" })).toBeDisabled();
  await expect(row.getByRole("button", { name: "复制" })).toBeDisabled();
});
