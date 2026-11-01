import { expect, test } from "@playwright/test";

test("strategy template wizard previews dependencies and creates an isolated custom definition", async ({ page }) => {
  let createPayload: Record<string, unknown> | undefined;
  const definition = {
    schemaVersion: 1, id: "strategy.template_demo", version: 1,
    displayName: "模板化均线", description: "模板副本", origin: "custom", status: "active",
    supportedAssetTypes: ["STOCK"], baseTimeframe: "1d", universe: { marketTypes: ["a_share"] }, parameters: {},
    entryRules: { nodeType: "condition", left: { functionId: "condition.crossover", version: 1, arguments: [{ kind: "series", field: "close" }, { kind: "series", field: "close" }] } },
    exitRules: { nodeType: "condition", left: { functionId: "condition.crossunder", version: 1, arguments: [{ kind: "series", field: "close" }, { kind: "series", field: "close" }] } },
    positionSizing: { kind: "equity_percent", value: 20 }, stopLoss: { enabled: false, kind: "percent", value: 0 }, takeProfit: { enabled: false, kind: "percent", value: 0 },
    pyramiding: { enabled: false, maxEntries: 1 }, reentry: { enabled: false, cooldownBars: 0 }, risk: { maxPositionPercent: 20, maxDrawdownPercent: 10 },
    execution: { runMode: "backtest", signalTiming: "bar_close", fillPrice: "next_open" }, backtest: { initialCash: 100000, commissionRate: 0.0003, slippageRate: 0.0001 },
    createdAt: "2026-09-05T00:00:00+00:00", updatedAt: "2026-09-05T00:00:00+00:00",
  };
  await page.route("**/api/strategy/definitions", (route) => route.fulfill({ json: { items: [], total: 0 } }));
  await page.route("**/api/strategy/functions", (route) => route.fulfill({ json: { items: [], total: 0 } }));
  await page.route("**/api/strategy/execution/capabilities", (route) => route.fulfill({ json: { capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false, modes: [] } }));
  await page.route("**/api/strategy/templates", (route) => route.fulfill({
    json: { items: [{
      templateId: "template.ma_crossover", version: 1, displayName: "MA Crossover", description: "趋势起点",
      sourceStrategyId: "strategy.ma_crossover", sourceStrategyVersion: 1, defaultOverrides: { parameters: { fast: 20, slow: 60 } },
      disclaimer: "仅供策略研究与参数演示，不代表收益承诺。", supportedAssetTypes: ["STOCK"],
      dependencies: [{ id: "technical.sma", version: 1 }],
      preview: { parameters: { fast: { default: 20 }, slow: { default: 60 } }, risk: { maxDrawdownPercent: 25 }, positionSizing: { kind: "equity_percent", value: 20 }, stopLoss: { enabled: true, kind: "percent", value: 5 } },
    }], total: 1 },
  }));
  await page.route("**/api/strategy/templates/template.ma_crossover/create", async (route) => {
    createPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ status: 201, json: { definition } });
  });
  await page.route("**/api/strategy/definition-resources/strategy.template_demo?**", (route) => route.fulfill({ json: definition }));
  await page.route("**/api/strategy/definition-resources/strategy.template_demo/versions", (route) => route.fulfill({ json: { items: [definition], total: 1 } }));

  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", { name: "从模板创建" }).click();
  const dialog = page.getByRole("dialog", { name: "从策略模板创建" });
  await expect(dialog).toContainText("technical.sma@1");
  await expect(dialog).toContainText("最大回撤 25%");
  await expect(dialog).toContainText("收益承诺");
  await dialog.getByRole("textbox", { name: "新策略名称（可选）" }).fill("模板化均线");
  await dialog.getByRole("button", { name: "以此模板创建" }).click();
  await expect.poll(() => createPayload).toEqual({ displayName: "模板化均线" });
  await expect(page.locator(".structured-editor")).toBeVisible();
});
