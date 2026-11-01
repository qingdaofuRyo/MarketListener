import { expect, test } from '@playwright/test';

test('BLS payroll card shows reference month and revised series', async ({ page }) => {
  await page.route('**/api/data/macro/series?**', route => route.fulfill({json:{
    available:true, observations:[
      {observationPeriod:'2026-08',value:-10,fetchedAt:'2026-10-31T12:00:00Z'},
      {observationPeriod:'2026-09',value:29,fetchedAt:'2026-10-31T12:00:00Z'},
    ],
  }}));
  await page.goto('/data/');
  const card = page.locator('[data-test="bls-payroll-series"]');
  await expect(card).toContainText('美国非农就业月度变化（BLS 最新修订）');
  await expect(card).toContainText('统计月 2026-09');
  await expect(card).toContainText('29');
  await expect(card).toContainText('不代表首次公布值或公布日期');
});
