import { expect, test } from "@playwright/test";


test("market page shows one unclassified review table without an Other category", async ({ page }) => {
  await page.route("**/api/market/unclassified**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          reviewId: "raw:tdx:62#000300",
          name: null,
          code: "000300",
          sourceCode: "62#000300",
          marketPrefix: "62",
          latestClose: 4492.25,
          lastBarAt: "2026-08-27T00:00:00+08:00",
          pricePeriod: "1d",
          periods: ["1d", "5m"],
          sourceTerminal: "通达信金融终端",
          origin: "RAW_UNRECOGNIZED",
          reason: "文件名未命中已登记分类规则",
        }],
        total: 1,
        page: 1,
        pageSize: 50,
      }),
    });
  });

  await page.goto("/market/");
  await expect(page.getByRole("heading", { name: "待分类标的" })).toBeVisible();
  const table = page.locator(".unclassified-table");
  await expect(table).toContainText("待确认");
  await expect(table).toContainText("62#000300");
  await expect(table).toContainText("4,492.25");
  await expect(table).toContainText("通达信金融终端");
  await expect(table).toContainText("日线、5分钟");

  await page.locator(".all-toolbar .el-select").click();
  await expect(page.getByRole("option", { name: "其它", exact: true })).toHaveCount(0);
});
