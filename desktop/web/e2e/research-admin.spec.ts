import { expect, test } from "@playwright/test";

test("internal research console keeps source review separate from customer pages", async ({ page }) => {
  await page.goto("http://127.0.0.1:18768/#research");
  await expect(page.getByRole("heading", { name: "研报知识" })).toBeVisible();
  await expect(page.locator("#research-status")).toContainText("篇去重文档");
  await expect(page.getByRole("button", { name: "扫描与去重" })).toBeVisible();
  await expect(page.getByRole("button", { name: "继续逐页提取" })).toBeVisible();
  await expect(page.getByRole("button", { name: "发布已核验内容" })).toBeVisible();
  await page.getByRole("button", { name: "读取最近 100 篇" }).click();
  await expect(page.locator("#research-documents")).toContainText("导入清单为空");
  await page.getByRole("button", { name: "读取识图结果" }).click();
  await expect(page.locator("#research-visual-list")).toContainText("没有该状态的识图观察");
});
