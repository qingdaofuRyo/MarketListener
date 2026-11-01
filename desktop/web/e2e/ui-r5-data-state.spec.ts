import { expect, test, type Page } from '@playwright/test';

async function internalPage(page: Page) {
  await page.route('**/api/admin/state', (route) => route.fulfill({ json: {
    ready: true, owned: true, state: 'running', action: null, error: null,
    settings: { auto_start: false, auto_update: false, background_priority: true, theme: 'dark' },
    terminalJobs: {}, terminalBusy: false, csrfToken: 'synthetic-console-token',
    startupSupported: false, startupEnabled: false, scope: '仅本机访问',
    projectPath: 'synthetic-project', dataPath: 'synthetic-data',
    backendUrl: 'http://127.0.0.1:8767/', operationLabels: {},
  } }));
  await page.route('**/api/admin/tasks', (route) => route.fulfill({ json: { items: [] } }));
  await page.goto('http://127.0.0.1:18768/#sources');
}

test('internal inventory marks unavailable evidence without zero stock claims', async ({ page }) => {
  await page.route('**/api/admin/inventory', (route) => route.fulfill({ json: {
    inventory: [], summary: { instruments: 0 }, storage: { available: false, bytes: 0 },
  } }));
  await internalPage(page);
  await page.getByRole('button', { name: '刷新盘点', exact: true }).click();
  await expect(page.locator('#inventory-status')).toContainText('本地行情清单尚未就绪');
  await expect(page.locator('#inventory-rows')).toHaveText('—');
  await expect(page.locator('#inventory-storage')).toHaveText('—');
});

test('internal inventory failure remains visible and allows retry', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/admin/inventory', (route) => {
    requests += 1;
    return route.fulfill({ status: 503, json: { detail: 'synthetic inventory failure' } });
  });
  await internalPage(page);
  await page.getByRole('button', { name: '刷新盘点', exact: true }).click();
  await expect(page.locator('#inventory-status')).toContainText('不能据此判断数据为空');
  await page.getByRole('button', { name: '刷新盘点', exact: true }).click();
  await expect.poll(() => requests).toBeGreaterThan(1);
});
