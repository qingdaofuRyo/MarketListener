import { expect, test } from '@playwright/test';

test('F10 local refresh shows each market cap observation date and reloads the selected company', async ({page}) => {
  const key = 'CN.SSE.STOCK.600000';
  let detailCalls = 0;
  let revision = 'v1';
  const company = {instrumentKey:key, name:'合成企业', code:'600000', market:'CN',
    totalMarketCap:{value:100000000,currency:'CNY',asOf:'2026-10-29T07:00:00Z',source:'合成行情源'},
    floatMarketCap:{value:50000000,currency:'CNY',asOf:'2026-10-28T07:00:00Z',source:'合成行情源'},
    updatedAt:'2026-10-27T07:00:00Z'};
  await page.route('**/api/f10/companies?**', route => route.fulfill({json:{items:[company],total:1,page:1,pageSize:50}}));
  await page.route('**/api/f10/update-status', route => route.fulfill({json:{autoEnabled:true,completedRevision:revision,
    revenuePublicationPending:true,
    cnUniverseCache:{status:'STALE_CACHE',count:5577,savedAt:'2026-10-20T00:00:00Z'},
    hkUniverseFile:{status:'LOCAL_FILE_PRESENT',savedAt:'2026-10-01T00:00:00Z'},
    jobs:[{kind:'F10_UPDATE_CN',status:'PARTIAL_FAILURE',batchMatchesLatest:true,completedAt:'2026-10-31T01:50:19Z',lastBatch:{status:'PARTIAL',updated:25,remaining:17,failed:0,completedAt:'2026-10-31T01:50:19Z'}},
      {kind:'REVENUE_UPDATE',status:'PARTIAL_FAILURE',batchMatchesLatest:false,completedAt:'2026-10-31T03:00:00Z',lastBatch:{status:'PARTIAL',updated:25,remaining:17,failed:0,completedAt:'2026-10-31T01:50:19Z'}}]}}));
  await page.route('**/api/f10/companies/*', route => {
    detailCalls++;
    return route.fulfill({json:{...company, companyIntro:detailCalls > 1 ? '更新后的本机资料' : '原本机资料',
      topRevenueSegment:{name:'新产品',period:'2026-06-30',classification:'product',ratio:86.5,revenueSharePct:86.5},
      revenueSegments:[
        {name:'新产品',period:'2026-06-30',classification:'product',ratio:86.5,revenueSharePct:86.5,fetchedAt:'2026-10-20T00:00:00Z'},
        {name:'零收入',period:'2026-06-30',classification:'product',amount:{value:0,currency:'CNY',asOf:'2026-10-31T00:00:00Z',source:'合成'}},
        {name:'负收入',period:'2026-06-30',classification:'product',amount:{value:-5,currency:'CNY',asOf:'2026-10-31T00:00:00Z',source:'合成'}},
        {name:'时间未知金额',period:'2026-06-30',classification:'product',amountWithoutTimestamp:{value:99,currency:'CNY'}},
        {name:'币种未知金额',period:'2026-06-30',classification:'product',amountWithoutTimestamp:{value:32,currency:'UNKNOWN'}},
        {name:'老产品',period:'2025-12-31',classification:'product',ratio:0.8},
        {name:'时期未知',classification:'region',ratio:0.3},
      ]}});
  });
  await page.goto(`/f10/company/${key}`);
  const detail = page.locator('.company-detail');
  await expect(detail).toContainText('原本机资料');
  await expect(detail).toContainText('合成行情源');
  await expect(detail).toContainText('2026-10-29');
  await expect(detail).toContainText('2026-10-28');
  await expect(detail).toContainText('收入最高业务');
  await expect(detail).toContainText('不代表利润最高');
  await expect(detail).toContainText('86.5%');
  await expect(detail).not.toContainText('8650.0%');
  const revenues = detail.locator('.revenue-section');
  await expect(revenues).toContainText('新产品');
  await expect(revenues).toContainText('0 CNY');
  await expect(revenues).toContainText('-5 CNY');
  await expect(revenues).toContainText('99 CNY');
  await expect(revenues).toContainText('32 币种未知');
  await expect(revenues).toContainText('2026-10-20');
  await expect(revenues).toContainText('未知');
  await expect(revenues).not.toContainText('老产品');
  await expect(page.getByRole('combobox',{name:'收入构成报告期'})).toBeVisible();
  await revenues.locator('.period-select').click();
  await page.getByRole('option',{name:'全部报告期'}).click();
  await expect(revenues).toContainText('老产品');
  await expect(revenues).toContainText('时期未知');
  await expect(page.locator('[data-test="f10-update-status"]')).toContainText('后台分批检查');
  await expect(page.locator('[data-test="f10-update-status"]')).toContainText('A股概况：本批完成，待继续');
  await expect(page.locator('[data-test="f10-update-status"]')).toContainText('A股收入构成：任务未完成，请查看任务记录');
  await expect(page.locator('[data-test="f10-batch-F10_UPDATE_CN"]')).toContainText('更新 25 家，剩余 17 家，失败 0 项');
  await expect(page.locator('[data-test="f10-cn-universe"]')).toContainText('5577 条');
  await expect(page.locator('[data-test="f10-cn-universe"]')).toContainText('超过 7 天未更新缓存');
  await expect(page.locator('[data-test="f10-hk-universe"]')).toContainText('可能不含新上市公司');
  await expect(page.locator('[data-test="f10-hk-universe"]')).toContainText('2026-10-01');
  await expect(page.locator('[data-test="f10-revenue-publication"]')).toContainText('后台将尝试恢复本机发布');
  await page.getByRole('button',{name:'重新读取本机资料'}).click();
  await expect(detail).toContainText('更新后的本机资料');
  expect(detailCalls).toBe(2);
  revision = 'v2';
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect.poll(() => detailCalls).toBe(3);
});
