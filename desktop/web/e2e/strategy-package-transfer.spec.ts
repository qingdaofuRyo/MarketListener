import { expect, test } from "@playwright/test";

test("strategy package import previews the lock and requires an explicit conflict choice", async ({ page }) => {
  let imported: Record<string, unknown> | undefined;
  await page.route("**/api/strategy/definitions", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/strategy/execution/capabilities", (route) =>
    route.fulfill({ json: { capability: "order_intent_create", platform: "desktop", androidOrderIntentEnabled: false, modes: [] } }),
  );
  await page.route("**/api/strategy/packages/preview", (route) =>
    route.fulfill({
      json: {
        manifest: { packageId: "strategy-transfer-0123456789abcdef", signature: null },
        definition: {
          id: "strategy.imported_demo", version: 1, displayName: "导入策略演示", supportedAssetTypes: ["STOCK"],
        },
        dependencyLock: {
          strategy: { id: "strategy.imported_demo", version: 1, definitionHash: "a".repeat(64) },
          strategyFunctions: [{ id: "technical.sma", version: 1, definitionHash: "b".repeat(64) }],
          indicators: [],
        },
        signatureStatus: "UNSIGNED", signatureVerified: false,
        androidCompatible: false, androidReason: "DECLARATIVE_ANDROID_DSL_REQUIRED",
        conflict: { exists: true, id: "strategy.imported_demo", versions: [1, 2] },
      },
    }),
  );
  await page.route("**/api/strategy/packages/import", async (route) => {
    imported = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ status: 201, json: { definition: { id: "strategy.imported_demo", version: 3 } } });
  });

  await page.goto("/strategy/?section=strategy");
  await page.getByRole("button", { name: "导入策略包" }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "demo.strategy.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("not inspected by the mocked API"),
  });

  const dialog = page.getByRole("dialog", { name: "导入策略包" });
  await expect(dialog).toContainText("导入策略演示");
  await expect(dialog).toContainText("technical.sma@1");
  await expect(dialog).toContainText("DECLARATIVE_ANDROID_DSL_REQUIRED");
  await expect(dialog).toContainText("不可覆盖");
  await dialog.locator("label").filter({ hasText: "作为新版本导入" }).click();
  await dialog.getByRole("button", { name: "确认导入" }).click();
  await expect.poll(() => imported).toMatchObject({ conflict: "new_version" });
});
