import { expect, test } from '@playwright/test';

test('R5 数据源页用统一状态展示不可用清单', async ({ page }) => {
  await page.route('**/api/data-sources/inventory**', (route) => route.fulfill({
    json: {
      inventory: [],
      summary: { instruments: 0 },
      storage: { available: false, bytes: 0, files: 0, missingFiles: 0, groups: [] },
    },
  }));

  await page.goto('/data-sources/');

  const state = page.locator('.data-state.is-unavailable');
  await expect(state).toBeVisible();
  await expect(state).toContainText('本地行情清单尚未就绪');
  await expect(state).toContainText('暂不能确认库存数量及来源');
  await expect(state.getByRole('button', { name: '重新检查', exact: true })).toBeVisible();
  await expect(page.locator('.source-metrics')).toHaveCount(0);
});

test('R5 数据源页加载失败提供统一错误状态和重试入口', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/data-sources/inventory**', (route) => {
    requests += 1;
    return route.fulfill({ status: 500, json: { detail: 'synthetic failure' } });
  });

  await page.goto('/data-sources/');

  const state = page.locator('.data-state.is-error');
  await expect(state).toBeVisible();
  await expect(state).toContainText('数据盘点加载失败');
  await expect(state).toContainText('不能据此判断数据为空');

  await state.getByRole('button', { name: '重试', exact: true }).click();
  await expect.poll(() => requests).toBeGreaterThan(1);
});
