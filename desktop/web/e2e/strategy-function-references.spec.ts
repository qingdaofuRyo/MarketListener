import { expect, test } from "@playwright/test";

test("strategy function details disclose exact indicator and strategy references", async ({ page }) => {
  await page.route("**/api/strategy/indicators**", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/strategy/functions**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            resourceKind: "strategy_function",
            id: "technical.sma",
            version: 1,
            versionedId: "technical.sma@1",
            name: "简单移动平均",
            category: "trend",
            categoryLabel: "趋势",
            description: "指定窗口的算术移动平均。",
            inputs: [
              { name: "values", type: "series<number>" },
              { name: "lookback", type: "integer", default: 20 },
            ],
            output: { type: "series<number>" },
            supportedAssetTypes: ["STOCK", "INDEX", "FUTURE"],
            origin: "builtin",
            referencedBy: {
              indicators: ["indicator.ma"],
              strategies: ["strategy.ma_crossover"],
            },
          },
        ],
        total: 1,
      },
    }),
  );

  await page.goto("/strategy/?section=function");
  await page.getByText("简单移动平均", { exact: true }).click();

  const drawer = page.getByRole("dialog");
  await expect(drawer).toContainText("values · series<number>");
  await expect(drawer).toContainText("返回类型：series<number>");
  await expect(drawer).toContainText("被引用");
  await expect(drawer).toContainText("indicator.ma");
  await expect(drawer).toContainText("strategy.ma_crossover");
});
