import { expect, test } from '@playwright/test';
import { setup } from './chart-workbench-r4.spec';
import { quoteFieldGroups } from '../src/domain/quoteFieldGroups';

test.beforeEach(async ({ page }) => {
  await setup(page);
});

test('R5 行情终端基线保持虚拟列表与 14 字段布局', async ({ page }) => {
  await page.goto('/market/');

  const rows = page.locator('.instrument-row');
  await expect(rows.first()).toBeVisible();
  const renderedRowCount = await rows.count();
  expect(renderedRowCount).toBeGreaterThan(0);
  expect(renderedRowCount).toBeLessThan(100);

  const firstRowBox = await rows.first().boundingBox();
  expect(firstRowBox).not.toBeNull();
  expect(firstRowBox!.height).toBe(33);

  const quoteValues = page.locator('.quote-values').first();
  await expect(quoteValues).toBeVisible();
  const groups = quoteValues.locator('.quote-field-group');
  await expect(groups).toHaveCount(quoteFieldGroups.length);

  for (let index = 0; index < quoteFieldGroups.length; index += 1) {
    await expect(groups.nth(index).locator('p')).toHaveCount(2);
  }

  const visibleFields = await quoteValues.locator('[data-field]').evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute('data-field')),
  );
  expect(visibleFields).toEqual(quoteFieldGroups.flat());
});

test('R5 结构 token 已加载且密度基线未漂移', async ({ page }) => {
  await page.goto('/market/');

  const tokens = await page.evaluate(() => {
    const styles = getComputedStyle(document.documentElement);
    return {
      radiusPanel: styles.getPropertyValue('--ml-radius-panel').trim(),
      motionFast: styles.getPropertyValue('--ml-motion-fast').trim(),
      layerModal: styles.getPropertyValue('--ml-layer-modal').trim(),
    };
  });

  expect(tokens).toEqual({
    radiusPanel: '8px',
    motionFast: '120ms',
    layerModal: '3000',
  });
});
