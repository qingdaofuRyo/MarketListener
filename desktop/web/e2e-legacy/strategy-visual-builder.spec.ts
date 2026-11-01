import { expect, test } from "@playwright/test";

test("structured strategy builder edits a recursive AST through registered function signatures", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 640, height: 900 });
  let saved: Record<string, unknown> | undefined;
  const functions = [
    {
      resourceKind: "strategy_function",
      id: "condition.crossover",
      version: 1,
      versionedId: "condition.crossover@1",
      name: "向上交叉",
      inputs: [
        { name: "left", type: "series<number>" },
        { name: "right", type: "series<number>" },
      ],
      output: { type: "series<boolean>" },
      supportedAssetTypes: ["STOCK", "FUTURE"],
    },
    {
      resourceKind: "strategy_function",
      id: "condition.crossunder",
      version: 1,
      versionedId: "condition.crossunder@1",
      name: "向下交叉",
      inputs: [
        { name: "left", type: "series<number>" },
        { name: "right", type: "series<number>" },
      ],
      output: { type: "series<boolean>" },
      supportedAssetTypes: ["STOCK", "FUTURE"],
    },
    {
      resourceKind: "strategy_function",
      id: "technical.sma",
      version: 1,
      versionedId: "technical.sma@1",
      name: "简单移动平均",
      inputs: [
        { name: "values", type: "series<number>" },
        { name: "lookback", type: "integer", default: 20, minimum: 1 },
      ],
      output: { type: "series<number>" },
      supportedAssetTypes: ["STOCK", "FUTURE"],
    },
    {
      resourceKind: "strategy_function",
      id: "market.market_cap",
      version: 1,
      versionedId: "market.market_cap@1",
      name: "市值比较",
      inputs: [
        { name: "value_yuan", type: "number" },
        { name: "operator", type: "comparison_operator" },
        { name: "threshold", type: "number" },
        { name: "unit", type: "market_cap_unit" },
      ],
      output: { type: "boolean" },
      supportedAssetTypes: ["STOCK"],
    },
  ];
  await page.route("**/api/strategy/functions", (route) =>
    route.fulfill({ json: { items: functions, total: functions.length } }),
  );
  await page.route("**/api/strategy/definitions", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/strategy/execution/capabilities", (route) =>
    route.fulfill({
      json: {
        capability: "order_intent_create",
        platform: "desktop",
        androidOrderIntentEnabled: false,
        modes: [],
      },
    }),
  );
  await page.route("**/api/strategy/definition/validate", async (route) => {
    saved = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { valid: true } });
  });
  await page.route("**/api/strategy/definition-resources", async (route) => {
    saved = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { id: saved.id, version: saved.version } });
  });

  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", { name: "新建策略" }).click();
  await expect(page.locator(".structured-editor")).toBeVisible();
  await page.getByRole("textbox", { name: "策略名" }).fill("递归编辑器回归");
  await page.getByRole("combobox", { name: "基础周期" }).focus();
  await page.getByRole("combobox", { name: "基础周期" }).press("Enter");
  await page.getByRole("option", { name: "4H", exact: true }).click();
  await page.getByRole("combobox", { name: "交易方向" }).focus();
  await page.getByRole("combobox", { name: "交易方向" }).press("Enter");
  await page.getByRole("option", { name: "做空", exact: true }).click();
  const scaleOut = page.getByTestId("strategy-scale-out");
  await scaleOut.getByRole("spinbutton").fill("40");

  const entry = page.locator('[data-rule-path="entryRules"]');
  await entry.getByRole("button", { name: "添加规则组" }).press("Enter");
  const rootChildren = entry.locator(":scope > .rule-children > .rule-child");
  await expect(rootChildren).toHaveCount(2);
  await entry
    .getByRole("button", { name: "复制节点" })
    .last()
    .click();
  await expect(rootChildren).toHaveCount(3);
  await page.getByRole("button", { name: "撤销上次规则编辑" }).click();
  await expect(rootChildren).toHaveCount(2);

  const nested = page.locator('[data-rule-path="entryRules.1"]');
  await nested.getByLabel("逻辑运算 entryRules.1").focus();
  await nested.getByLabel("逻辑运算 entryRules.1").press("Enter");
  await page.getByRole("option", { name: "OR（满足任一）" }).click();
  await nested.getByRole("button", { name: "添加条件" }).click();
  await expect(nested.locator(".rule-condition")).toHaveCount(2);
  await expect(
    page.getByRole("combobox", { name: "策略函数 entryRules.1.0" }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "嵌套策略函数" }).first(),
  ).toBeVisible();

  const rootConditionFunction = page.getByRole("combobox", {
    name: "策略函数 entryRules.0",
  });
  await rootConditionFunction.focus();
  await rootConditionFunction.press("Enter");
  await page.getByRole("option", { name: "市值比较" }).click();
  await page
    .locator(".scope-row .el-checkbox")
    .filter({ hasText: "国内期货" })
    .click();
  await expect(page.getByRole("checkbox", { name: "国内期货" })).toBeChecked();
  await expect(page.locator(".structured-editor")).toHaveAttribute(
    "data-supported-asset-types",
    "STOCK,FUTURE",
  );
  await page.getByRole("button", { name: "验证规则" }).click();
  await expect(page.locator('[data-rule-path="entryRules.0"]')).toContainText(
    "市值比较 不适用于当前选择的资产类型",
  );
  await page
    .locator(".scope-row .el-checkbox")
    .filter({ hasText: "国内期货" })
    .click();

  const secondRootChild = page
    .locator('[data-rule-path="entryRules.1"]')
    .locator("xpath=..");
  await secondRootChild
    .locator(":scope > .node-actions")
    .getByRole("button", { name: "上移" })
    .focus();
  await secondRootChild
    .locator(":scope > .node-actions")
    .getByRole("button", { name: "上移" })
    .press("Enter");
  await expect(rootChildren.first()).toContainText("规则组");

  await page.getByRole("button", { name: /^保存为 v1$/ }).click();
  await expect.poll(() => saved).toBeTruthy();
  expect(saved).toMatchObject({
    schema_version: 1,
    display_name: "递归编辑器回归",
    entry_rules: { node_type: "group", operator: "AND" },
    exit_rules: { node_type: "group", operator: "AND" },
    base_timeframe: "4h",
    direction: "short",
    scale_out: { enabled: true, ratio_percent: 40 },
    position_sizing: { kind: "equity_percent" },
  });
  expect(JSON.stringify(saved)).toContain('"function_id"');
  expect(JSON.stringify(saved)).not.toContain("nodeId");
});

test("structured strategy builder reopens and resaves a complex published AST without template loss", async ({
  page,
}) => {
  test.setTimeout(60_000);
  let resaved: Record<string, unknown> | undefined;
  const functions = [
    {
      id: "condition.crossover", version: 1, name: "向上交叉",
      inputs: [{ name: "left", type: "series<number>" }, { name: "right", type: "series<number>" }],
      output: { type: "series<boolean>" }, supportedAssetTypes: ["STOCK"],
    },
    {
      id: "technical.sma", version: 1, name: "简单移动平均",
      inputs: [{ name: "values", type: "series<number>" }, { name: "lookback", type: "integer", default: 20 }],
      output: { type: "series<number>" }, supportedAssetTypes: ["STOCK"],
    },
  ];
  const sma = (lookback: number) => ({
    kind: "function",
    call: {
      functionId: "technical.sma", version: 1,
      arguments: [
        { kind: "series", field: "close" },
        { kind: "literal", value: lookback },
      ],
    },
  });
  const complexDefinition = {
    schemaVersion: 1, id: "strategy.user_complex", version: 1,
    displayName: "复杂规则保真", description: "递归树回归", origin: "custom", status: "active",
    supportedAssetTypes: ["STOCK"], baseTimeframe: "1d", direction: "short",
    universe: { marketTypes: ["a_share"], excludeSt: false }, parameters: {},
    entryRules: {
      nodeType: "group", operator: "OR",
      children: [
        {
          nodeType: "group", operator: "AND",
          children: [{
            nodeType: "condition", left: { functionId: "technical.sma", version: 1, arguments: [{ kind: "series", field: "close" }, { kind: "literal", value: 10 }] },
            comparator: "gt", right: { kind: "literal", value: 100 },
          }],
        },
        {
          nodeType: "group", operator: "NOT",
          children: [{ nodeType: "condition", left: { functionId: "condition.crossover", version: 1, arguments: [sma(20), sma(60)] } }],
        },
      ],
    },
    exitRules: {
      nodeType: "group", operator: "AND",
      children: [{ nodeType: "condition", left: { functionId: "condition.crossover", version: 1, arguments: [sma(10), sma(20)] } }],
    },
    positionSizing: { kind: "risk_percent", value: 2 },
    stopLoss: { enabled: true, kind: "atr_multiple", value: 2 },
    takeProfit: { enabled: true, kind: "fixed_price", value: 120 },
    scaleOut: { enabled: true, ratioPercent: 40 },
    pyramiding: { enabled: true, maxEntries: 3 }, reentry: { enabled: false, cooldownBars: 5 },
    risk: { maxPositionPercent: 20, maxDrawdownPercent: 12 },
    execution: { runMode: "backtest", signalTiming: "bar_close", fillPrice: "next_open" },
    backtest: { initialCash: 100000, commissionRate: 0.0003, slippageRate: 0.0001 },
    createdAt: "2026-09-05T00:00:00+08:00", updatedAt: "2026-09-05T00:00:00+08:00",
  };
  await page.route("**/api/strategy/functions", (route) =>
    route.fulfill({ json: { items: functions, total: functions.length } }),
  );
  await page.route("**/api/strategy/definitions", (route) =>
    route.fulfill({
      json: {
        items: [{
          strategyId: complexDefinition.id, id: complexDefinition.id, version: 1,
          displayName: complexDefinition.displayName, scriptKind: "structured_v1", status: "active",
          updatedAt: complexDefinition.updatedAt, inputs: [], parameters: {},
        }], total: 1,
      },
    }),
  );
  await page.route("**/api/strategy/execution/capabilities", (route) =>
    route.fulfill({ json: { capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false, modes: [] } }),
  );
  await page.route("**/api/strategy/definition-resources/strategy.user_complex?**", (route) =>
    route.fulfill({ json: complexDefinition }),
  );
  await page.route("**/api/strategy/definition-resources/strategy.user_complex/versions", (route) =>
    route.fulfill({ json: { items: [complexDefinition], total: 1 } }),
  );
  await page.route("**/api/strategy/definition/validate", (route) =>
    route.fulfill({ json: { valid: true } }),
  );
  await page.route("**/api/strategy/definition-resources", async (route) => {
    resaved = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ status: 201, json: { definition: resaved, validation: { valid: true } } });
  });

  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", { name: "编辑" }).click();
  await expect(page.locator(".structured-editor")).toBeVisible();
  await expect(page.locator('[data-rule-path="entryRules.0"]')).toContainText("简单移动平均");
  await expect(page.locator('[data-rule-path="entryRules.1"]')).toContainText("NOT（取反）");
  await expect(page.locator(".structured-editor")).toContainText("风险比例");
  await expect(page.locator(".structured-editor")).toContainText("ATR 倍数");
  await page.getByRole("button", { name: "保存为 v2" }).click();

  await expect.poll(() => resaved).toBeTruthy();
  expect(resaved).toMatchObject({
    version: 2,
    position_sizing: { kind: "risk_percent", value: 2 },
    stop_loss: { kind: "atr_multiple", value: 2 },
    take_profit: { kind: "fixed_price", value: 120 },
    scale_out: { enabled: true, ratio_percent: 40 },
    direction: "short",
    entry_rules: {
      node_type: "group", operator: "OR",
      children: [
        { node_type: "group", operator: "AND" },
        { node_type: "group", operator: "NOT" },
      ],
    },
  });
  expect(JSON.stringify(resaved)).toContain('"function_id":"technical.sma"');
});
