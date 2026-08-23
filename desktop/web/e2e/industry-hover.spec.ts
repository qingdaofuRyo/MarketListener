import { expect, test } from "@playwright/test";

test("industry company name/code hover remains open on its data card", async ({ page }) => {
  await page.goto("/industry/");
  const atlas = page.frameLocator("iframe.atlas-frame");
  const cnCompany = atlas.locator('[data-instrument-key^="CN."]').first();
  const code = cnCompany.locator(".code");
  await expect(cnCompany).toBeVisible({ timeout: 15_000 });
  await code.hover();

  const tooltip = atlas.locator("#tooltip");
  await expect(tooltip).toBeVisible({ timeout: 1_000 });
  await expect(tooltip).toContainText("总市值");
  await expect(tooltip).not.toContainText("undefined");
  await expect(tooltip).not.toContainText("null");
  await expect(tooltip).not.toContainText("Invalid Date");
  await expect(tooltip.evaluate((node) => {
    const chip = document.querySelector('[data-instrument-key^="CN."]')?.getBoundingClientRect();
    const popover = node.getBoundingClientRect();
    return Boolean(chip && popover.top < chip.top && popover.top >= 0 && popover.right <= window.innerWidth);
  })).resolves.toBeTruthy();
  await tooltip.hover();
  await page.waitForTimeout(250);
  await expect(tooltip).toBeVisible();

});

test("industry company click opens the complete local drawer", async ({ page }) => {
  await page.goto("/industry/");
  const atlas = page.frameLocator("iframe.atlas-frame");
  const cnCompany = atlas.locator('[data-instrument-key^="CN."]').first();
  // 图谱数据来自本地库存；在并发端到端测试时首个读取可能需要数秒。
  await expect(cnCompany).toBeVisible({ timeout: 15_000 });
  await cnCompany.click();
  const drawer = atlas.locator("#drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("所属行业");
});

test.describe("touch industry card", () => {
  test.use({ hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });

  test("tap opens the same local company drawer", async ({ page }) => {
    await page.goto("/industry/");
    const atlas = page.frameLocator("iframe.atlas-frame");
    const company = atlas.locator('[data-instrument-key^="CN."]').first();
    await company.tap();
    await expect(atlas.locator("#drawer")).toContainText("所属行业");
  });
});

test("industry F10 link navigates the top-level terminal", async ({ page }) => {
  await page.goto("/industry/");
  const atlas = page.frameLocator("iframe.atlas-frame");
  const cnCompany = atlas.locator('[data-instrument-key^="CN."]').first();
  await cnCompany.hover();
  const tooltip = atlas.locator("#tooltip");
  await expect(tooltip).toBeVisible({ timeout: 1_000 });
  const f10Link = tooltip.locator(".f10-link");
  await expect(f10Link).toHaveAttribute("target", "_top");
  await f10Link.click();
  await expect(page).toHaveURL(/\/f10\/company\/CN\./);
});

test("industry popover works for HK companies and is unscaled at 50/100/150 percent", async ({ page }) => {
  await page.goto("/industry/");
  const atlas = page.frameLocator("iframe.atlas-frame");
  const hkCompany = atlas.locator('[data-instrument-key^="HK."]').first();
  // A locally curated industry graph may legitimately contain CN companies
  // only.  Exercise the HK-specific popover when that optional local data is
  // available, without turning an empty HK catalogue into a UI failure.
  if (await hkCompany.count() === 0) return;
  await expect(hkCompany).toBeVisible();
  await hkCompany.hover();
  const tooltip = atlas.locator("#tooltip");
  await expect(tooltip).toBeVisible({ timeout: 1_000 });
  await expect(tooltip).toContainText("HK");

  const initialFont = await tooltip.evaluate((node) => getComputedStyle(node).fontSize);
  for (let click = 0; click < 4; click += 1) await atlas.locator("#zoom-out").click();
  await hkCompany.hover();
  await expect(tooltip).toBeVisible({ timeout: 1_000 });
  await expect(tooltip.evaluate((node) => getComputedStyle(node).fontSize)).resolves.toBe(initialFont);
  for (let click = 0; click < 7; click += 1) await atlas.locator("#zoom-in").click();
  await hkCompany.hover();
  await expect(tooltip).toBeVisible({ timeout: 1_000 });
  await expect(tooltip.evaluate((node) => getComputedStyle(node).fontSize)).resolves.toBe(initialFont);
});
