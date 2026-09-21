import { expect, type Page } from '@playwright/test';

const id = 'CN.SSE.STOCK.600000';
const bars = Array.from({ length: 80 }, (_, i) => ({
  barOpenTime: new Date(Date.UTC(2026, 5, i + 1)).toISOString(),
  open: 12 + Math.sin(i / 8),
  high: 13 + Math.sin(i / 8),
  low: 11 + Math.sin(i / 8),
  close: 12.4 + Math.sin(i / 8),
  volume: 10000 + i * 50,
}));
const catalog = [
  { id: 'indicator.ma', name: '移动平均线', englishName: 'Moving Average', placement: 'overlay' },
  { id: 'indicator.rsi', name: '相对强弱指数', englishName: 'RSI', placement: 'pane' },
].map((item) => ({
  ...item,
  resourceKind: 'indicator',
  version: 1,
  status: 'active',
  categoryLabel: '趋势',
  supportedAssetTypes: ['STOCK'],
  parameters: [{ name: 'lookback', default: 14, type: 'integer', minimum: 2, maximum: 500 }],
}));

export async function setupMarketWorkbench(page: Page, testBars = bars) {
  page.on('pageerror', (error) => {
    throw error;
  });
  await page.setViewportSize({ width: 1440, height: 960 });
  const instrument = {
    instrumentId: id,
    symbol: '600000',
    name: '浦发银行',
    assetType: 'STOCK',
    market: 'CN',
    latestPrice: 12,
  };
  let drawings: Array<{ type: string; points: Array<{ time: string; price: number }> }> = [];
  let saves = 0;
  const requests: Array<{ size: number }> = [];

  await page.route('**/api/strategy/indicators**', (route) => route.fulfill({ json: { items: catalog } }));
  await page.route('**/api/composites/**', (route) => route.fulfill({ json: { items: [], events: [] } }));
  await page.route('**/api/strategy/definitions**', (route) => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/market/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let result: unknown = {};

    if (path.endsWith('cache-status')) result = { dataVersion: 'r4' };
    else if (path.endsWith('categories')) {
      result = {
        items: [
          { id: 'cn-future-main', label: '国内期货主连合约' },
          { id: 'cn-future-weighted', label: '国内期货加权合约' },
        ],
      };
    } else if (path.endsWith('/instruments')) result = { items: [instrument], total: 1 };
    else if (path.endsWith('drawings/batch')) result = { items: { [id]: drawings } };
    else if (path.endsWith('bars/batch')) result = { items: { [id]: testBars } };
    else if (path.endsWith('/drawings')) {
      if (route.request().method() === 'PUT') {
        drawings = route.request().postDataJSON().items;
        saves += 1;
      }
      result = { items: drawings };
    } else if (path.endsWith('indicator-series')) {
      const body = route.request().postDataJSON();
      requests.push(body);
      result = {
        instances: body.instances.map((item: { definitionId: string }) => ({
          ...item,
          status: 'ready',
          plots: [{ id: 'value', type: 'line' }],
          series: { value: bars.slice(0, body.size).map((bar) => bar.close) },
        })),
      };
    } else if (path.endsWith('/bars')) {
      result = {
        bars: url.searchParams.get('limit') === '1' ? [testBars.at(-1)] : testBars,
        availablePeriods: ['1d', '1h'],
        start: 0,
        size: testBars.length,
        total: testBars.length,
        hasMore: false,
      };
    } else {
      result = {
        bars: testBars,
        drawings,
        series: {},
        total: testBars.length,
        start: 0,
        size: testBars.length,
        period: url.searchParams.get('period') || '1d',
        availablePeriods: ['1d', '1h'],
        hasMore: false,
      };
    }
    await route.fulfill({ json: result });
  });

  await page.goto('/market/');
  await page.locator('.row-main').first().dblclick();
  await expect(page.locator('.workbench-overlay')).toBeVisible();
  await expect(page.locator('.workbench-chart .el-loading-mask')).toHaveCount(0);
  return { drawings: () => drawings, saves: () => saves, requests };
}
