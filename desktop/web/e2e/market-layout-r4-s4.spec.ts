import { expect, test, type Page } from '@playwright/test';
import { setupMarketWorkbench as setup, bars as fixtureBars } from './helpers/market-workbench';
import { quoteFieldGroups } from '../src/domain/quoteFieldGroups';
import { chartAxisGutter, CHART_LAYOUT, mergeBarTail, prependHistoryPage, remapBarRange, sameBarTimeline } from '../src/domain/chartLayout';
import { subchartSeries } from '../src/domain/chartSubchart';

test('续4公共分组、测量gutter和副图语义', () => {
  expect(quoteFieldGroups).toEqual([['开','收'],['高','低'],['涨幅','振幅'],['涨跌','结'],['量','额'],['持仓量','沉淀资金'],['总市值','流通市值']]);
  const labels=['-123,456.789','0','9,999,999'];
  expect(chartAxisGutter(labels,s=>s.length*6.3)).toBe(Math.ceil(labels[0].length*6.3)+CHART_LAYOUT.axisPadding+CHART_LAYOUT.axisLabelMargin);
  const series=subchartSeries([{volume:5,openInterest:1e12},{volume:0,openInterest:null}], 'openInterest',['red','green'],'blue');
  expect(series[0]).toMatchObject({type:'bar',yAxisIndex:1,itemStyle:{opacity:0.8}});
  expect(series[1]).toMatchObject({type:'line',yAxisIndex:2,connectNulls:false,data:[1e12,null]});
});

test('K线时间轴一致时允许保留视口，漏失或换周期时不沿用', () => {
  const original = [{barOpenTime:'2026-10-28T00:00:00Z'}, {barOpenTime:'2026-10-29T00:00:00Z'},
    {barOpenTime:'2026-10-30T00:00:00Z'}];
  expect(sameBarTimeline(original.map(item => ({...item})), original)).toBe(true);
  expect(sameBarTimeline([original[0], {barOpenTime:'2026-10-29T01:00:00Z'}, original[2]], original)).toBe(false);
  expect(sameBarTimeline(original.slice(1), original)).toBe(false);
  expect(sameBarTimeline([{}], [{}])).toBe(false);
  expect(sameBarTimeline([], [])).toBe(false);
  const older = [{barOpenTime:'2026-10-27'}, ...original];
  expect(mergeBarTail(older, original.map(item => ({...item})))).toHaveLength(4);
  expect(mergeBarTail(older, [original[0], {barOpenTime:'2026-10-29T01:00:00Z'}, original[2]])).toBeNull();
  expect(mergeBarTail(older, [{barOpenTime:'2026-11-01'}])).toBeNull();
  const older80 = Array.from({length:80}, (_, index) => ({barOpenTime:String(index).padStart(2,'0')}));
  expect(mergeBarTail(older80, older80.slice(22).concat([{barOpenTime:'80'},{barOpenTime:'81'}]))).toHaveLength(82);
  const oldPage = [{barOpenTime:'2026-10-26T00:00:00Z'}, {barOpenTime:'2026-10-27T00:00:00Z'}];
  expect(prependHistoryPage(oldPage, original)).toHaveLength(5);
  expect(prependHistoryPage([original[0]], original)).toBeNull();
  expect(prependHistoryPage([...oldPage].reverse(), original)).toBeNull();
  const days = Array.from({length: 12}, (_, day) => ({tradingDate:`2026-10-${String(day + 1).padStart(2,'0')}`}));
  const shifted = days.slice(1).concat({tradingDate:'2026-10-13'});
  expect(remapBarRange(shifted, days, {start:6,end:11})).toEqual({start:6,end:11});
  expect(remapBarRange(shifted, days, {start:2,end:7})).toEqual({start:1,end:6});
  expect(remapBarRange(shifted, days, {start:0,end:5})).toBeNull();
  const broken = shifted.map(item => ({...item}));
  broken[9].tradingDate = '2026-10-20';
  expect(remapBarRange(broken, days, {start:6,end:11})).toBeNull();
  const earlierAtEnd = days.slice(1).concat({tradingDate:'2026-09-30'});
  expect(remapBarRange(earlierAtEnd, days, {start:6,end:11})).toBeNull();
  expect(remapBarRange(days, days, {start:6,end:11})).toEqual({start:6,end:11});
});

test('行情看板价格区收紧留白并保留真实高低价', async ({page}) => {
  const fixture = Array.from({length: 80}, (_, i) => ({
    barOpenTime: new Date(Date.UTC(2026, 6, i + 1)).toISOString(),
    open: 14500, close: 15000, low: i === 20 ? 12639 : 13000,
    high: i === 79 ? 17388 : 17000, volume: 1000 + i,
  }));
  await setup(page, fixture);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board = page.locator('.boards .board').first();
  const root = board.locator('.chart-root');
  await expect(root.locator('canvas').first()).toBeVisible();
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name));
    if (!resource) return null;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const options = init(el).getOption();
    return {min: options.yAxis[0].min, max: options.yAxis[0].max,
      priceHeight: options.grid[0].height, volumeHeight: options.grid[1].height,
      candles: options.series[0].data.length};
  })).toEqual({min: 12000, max: 18000, priceHeight: expect.any(Number),
    volumeHeight: expect.any(Number), candles: 80});
  const geometry = await root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const options = init(el).getOption();
    return {price: options.grid[0].height, volume: options.grid[1].height};
  });
  expect(geometry.price / (geometry.price + geometry.volume)).toBeGreaterThan(.70);
  const box = (await root.boundingBox())!;
  await page.mouse.move(box.x + box.width * .3, box.y + box.height * .4);
  await page.mouse.wheel(0, -200);
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return opt.dataZoom[0].endValue - opt.dataZoom[0].startValue + 1;
  })).toBeLessThan(60);
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    const visible = opt.series[0].data.slice(opt.dataZoom[0].startValue, opt.dataZoom[0].endValue + 1);
    const lows = visible.map((bar: number[]) => bar[2]), highs = visible.map((bar: number[]) => bar[3]);
    return opt.yAxis[0].min <= Math.min(...lows) && opt.yAxis[0].max >= Math.max(...highs);
  })).toBe(true);
});

async function option(page: Page) {
  return page.locator('.workbench-chart .chart-root').evaluate(async el => {
    // The production split chunk exports ECharts init; init on its existing DOM
    // returns that instance, so these assertions inspect actual rendered options.
    const resource=performance.getEntriesByType('resource').find(entry=>/\/echarts-[^/]+\.js$/.test(entry.name));
    if (!resource) throw new Error('ECharts production chunk not loaded');
    const module=await import(resource.name);
    const init=Object.values(module)[0] as (element:Element)=>{getOption:()=>unknown};
    return JSON.parse(JSON.stringify(init(el).getOption()));
  });
}

test('两张行情看板可独立缩放和复位', async ({page}) => {
  await setup(page);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const boards = page.locator('.boards .board');
  const top = boards.nth(0), bottom = boards.nth(1);
  const readRange = async (index: number) => boards.nth(index).locator('.chart-root').evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return {start: opt.dataZoom[0].startValue, end: opt.dataZoom[0].endValue};
  });
  await expect(top.locator('.chart-root canvas').first()).toBeVisible();
  await expect(bottom.locator('.chart-root canvas').first()).toBeVisible();
  const initialTop = await readRange(0), initialBottom = await readRange(1);
  await top.getByRole('button',{name:'放大上方K线'}).click();
  await expect.poll(async () => (await readRange(0)).start).toBeGreaterThan(initialTop.start);
  const enlarged = await readRange(0);
  expect(enlarged.end).toBe(initialTop.end);
  expect(await readRange(1)).toEqual(initialBottom);
  await top.getByRole('button',{name:'缩小上方K线'}).click();
  await expect.poll(async () => (await readRange(0)).start).toBeLessThan(enlarged.start);
  await top.getByRole('button',{name:'复位上方K线'}).click();
  await expect.poll(() => readRange(0)).toEqual(initialTop);
  await bottom.getByRole('button',{name:'放大下方K线'}).click();
  await expect.poll(async () => (await readRange(1)).start).toBeGreaterThan(initialBottom.start);
  expect(await readRange(0)).toEqual(initialTop);
});

test('缩小到已载入边界时补载更早K线并扩展视窗', async ({page}) => {
  const recent = fixtureBars.slice(-60);
  await setup(page, recent, true);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board = page.locator('.boards .board').first();
  const root = board.locator('.chart-root');
  await expect(root.locator('canvas').first()).toBeVisible();
  let fetches = 0;
  await page.route('**/api/market/instruments/*/bars/history?**', async route => {
    fetches++;
    await route.fulfill({json:{bars:fixtureBars.slice(0,20),start:0,total:80,hasMore:false,
      before:fixtureBars[0].barOpenTime}});
  });
  await board.getByRole('button',{name:'缩小上方K线'}).click();
  await expect.poll(() => fetches).toBe(1);
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return {total: opt.series[0].data.length,
      visible: opt.dataZoom[0].endValue - opt.dataZoom[0].startValue + 1};
  })).toEqual({total:80,visible:73});
  await page.route('**/api/market/instruments/*/bars?**', async route =>
    route.fulfill({json:{bars:recent.map(item => ({...item})),start:20,total:80,
      hasMore:false,availablePeriods:['1d','1h']}}));
  await page.evaluate(() => { const current = Date.now.bind(Date); Date.now = () => current() + 360_000; });
  const selector = board.locator('.board-header select');
  const refreshed = page.waitForResponse(response => response.url().includes('/bars?') && response.ok());
  await selector.dispatchEvent('change');
  await refreshed;
  await selector.dispatchEvent('change');
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return {total: opt.series[0].data.length,
      visible: opt.dataZoom[0].endValue - opt.dataZoom[0].startValue + 1};
  })).toEqual({total:80,visible:73});
  await board.getByRole('button',{name:'复位上方K线'}).click();
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return opt.dataZoom[0].endValue - opt.dataZoom[0].startValue + 1;
  })).toBe(60);
});

test('补载更早K线失败后缩小按钮可重试', async ({page}) => {
  await setup(page, fixtureBars.slice(-60), true);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board = page.locator('.boards .board').first();
  await expect(board.locator('.chart-root canvas').first()).toBeVisible();
  let calls = 0;
  await page.route('**/api/market/instruments/*/bars/history?**', async route => {
    calls++;
    if (calls === 1) return route.fulfill({status:503,json:{detail:'temporary fixture failure'}});
    return route.fulfill({json:{bars:fixtureBars.slice(0,20),start:0,total:80,hasMore:false,
      before:fixtureBars[0].barOpenTime}});
  });
  await board.getByRole('button',{name:'缩小上方K线'}).click();
  await expect.poll(() => calls).toBe(1);
  await expect(board).toHaveAttribute('aria-busy','false');
  await board.getByRole('button',{name:'缩小上方K线'}).click();
  await expect.poll(() => calls).toBe(2);
  await expect.poll(async () => board.locator('.chart-root').evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return opt.dataZoom[0].endValue - opt.dataZoom[0].startValue + 1;
  })).toBe(73);
});

test('重复历史分页不会进入图表且重试会重新请求', async ({page}) => {
  await setup(page, fixtureBars.slice(-60), true);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board = page.locator('.boards .board').first();
  await expect(board.locator('.chart-root canvas').first()).toBeVisible();
  let calls = 0;
  await page.route('**/api/market/instruments/*/bars/history?**', async route => {
    calls++;
    const bars = calls === 1 ? [fixtureBars[20]] : fixtureBars.slice(0,20);
    return route.fulfill({json:{bars,start:0,total:80,hasMore:false,before:bars[0].barOpenTime}});
  });
  await board.getByRole('button',{name:'缩小上方K线'}).click();
  await expect(board.locator('.board-status')).toHaveAttribute('title','更早行情分页日期异常或重复，请重试');
  await expect.poll(() => calls).toBe(1);
  await board.getByRole('button',{name:'缩小上方K线'}).click();
  await expect.poll(() => calls).toBe(2);
  await expect(board.locator('.board-status')).toHaveCount(0);
  await expect.poll(async () => board.locator('.chart-root').evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    return init(el).getOption().series[0].data.length;
  })).toBe(80);
});

test('同一时间序列行情更新后保留看板缩放并更新价格轴', async ({page}) => {
  await setup(page);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board = page.locator('.boards .board').first();
  const root = board.locator('.chart-root');
  await expect(root.locator('canvas').first()).toBeVisible();
  const options = async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    const opt = init(el).getOption();
    return {start: opt.dataZoom[0].startValue, end: opt.dataZoom[0].endValue, min: opt.yAxis[0].min};
  });
  const original = await options();
  await board.getByRole('button',{name:'放大上方K线'}).click();
  await expect.poll(async () => (await options()).start).toBeGreaterThan(original.start);
  const zoomed = await options();
  let latestBars = fixtureBars;
  await page.route('**/api/market/instruments/*/bars?**', async route => {
    const bars = latestBars.map(bar => ({...bar, open: bar.open + 100, close: bar.close + 100,
      high: bar.high + 100, low: bar.low + 100}));
    await route.fulfill({json: {bars, availablePeriods: ['1d','1h'], start: 0,
      total: bars.length, size: bars.length, hasMore: false}});
  });
  await page.evaluate(() => { const current = Date.now.bind(Date); Date.now = () => current() + 360_000; });
  const selector = board.locator('.board-header select');
  const response = page.waitForResponse(value => value.url().includes('/bars?') && value.ok());
  await selector.dispatchEvent('change');
  await response;
  await selector.dispatchEvent('change');
  await expect.poll(async () => (await options()).min).toBeGreaterThan(100);
  const updated = await options();
  expect({start: updated.start, end: updated.end}).toEqual({start: zoomed.start, end: zoomed.end});
  latestBars = fixtureBars.slice(1).concat({...fixtureBars.at(-1)!, barOpenTime: '2026-10-31T00:00:00.000Z'});
  await page.evaluate(() => { const current = Date.now.bind(Date); Date.now = () => current() + 360_000; });
  const rollover = page.waitForResponse(value => value.url().includes('/bars?') && value.ok());
  await selector.dispatchEvent('change');
  await rollover;
  await selector.dispatchEvent('change');
  await expect.poll(async () => root.evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(entry => /\/echarts-[^/]+\.js$/.test(entry.name))!;
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (element: Element) => {getOption: () => any};
    return init(el).getOption().xAxis[0].data.at(-1);
  })).toBe('2026-10-31T00:00:00.000Z');
  const rolled = await options();
  expect({start: rolled.start, end: rolled.end}).toEqual({start: zoomed.start, end: zoomed.end});
  await selector.selectOption('1h');
  await expect.poll(async () => (await options()).start).toBe(original.start);
});

test('主图指标参与可见价格轴范围，隐藏后恢复 K 线范围', async ({page}) => {
  await setup(page);
  const before = await option(page);
  expect(before.yAxis[0].max).toBeLessThan(25);
  await page.route('**/api/market/instruments/*/indicator-series', async route => {
    const body = route.request().postDataJSON();
    await route.fulfill({json: {instances: body.instances.map((item: {definitionId: string}) => ({
      ...item, status: 'ready', plots: [{id: 'value', type: 'line'}],
      series: {value: Array.from({length: body.size}, () => 25)},
    }))}});
  });
  await page.locator('.workbench-overlay').getByRole('button',{name:/^指标/}).click();
  await page.getByRole('button',{name:/移动平均线 Moving Average/}).click();
  await expect.poll(async () => (await option(page)).yAxis[0].max).toBeGreaterThanOrEqual(25);
  await page.getByRole('button',{name:'隐藏移动平均线',exact:true}).click();
  await expect.poll(async () => (await option(page)).yAxis[0].max).toBe(before.yAxis[0].max);
});

test('续4详情Overlay、主副图、周期栏和菜单真实布局', async ({page}) => {
  await setup(page);
  const fields=page.locator('.workbench-chart .quote-values');
  await expect(fields.locator('.quote-field-group')).toHaveCount(7);
  expect(await fields.locator('.quote-field-group').evaluateAll(groups=>groups.map(group=>Array.from(group.querySelectorAll('p')).map(p=>p.getAttribute('data-field'))))).toEqual(quoteFieldGroups);
  const slots=await fields.locator('p').evaluateAll(items=>items.map(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y,clip:getComputedStyle(el.querySelector('strong')!).textOverflow,full:el.scrollWidth<=el.clientWidth})));
  expect(slots.every(s=>s.clip!=='ellipsis'&&s.full)).toBe(true);
  expect(new Set(slots.map(s=>s.y)).size).toBe(2);
  const root=page.locator('.workbench-chart .chart-root');
  const before=await option(page);
  expect(before.grid[0].top+before.grid[0].height).toBe(before.grid[1].top);
  expect(before.title[0].top).toBeGreaterThanOrEqual(before.grid[1].top);
  expect(before.series.find((s:{id:string})=>s.id==='market-volume').itemStyle.opacity).toBe(0.8);
  expect(before.series.find((s:{id:string})=>s.id==='market-secondary').type).toBe('line');
  await page.locator('.workbench-overlay').getByRole('button',{name:/^指标/}).click();
  await page.getByRole('button',{name:/移动平均线 Moving Average/}).click();
  await expect(page.locator('.chart-indicator-legend-row')).toHaveCount(1);
  const after=await option(page);
  expect(after.grid[0]).toEqual(before.grid[0]);
  const legend=await page.locator('.workbench-chart .chart-legend-overlay').boundingBox();
  const box=(await root.boundingBox())!;
  expect(legend!.y).toBeGreaterThanOrEqual(box.y+after.grid[0].top-1);
  const nav=page.getByRole('navigation',{name:'K线周期'});
  expect(await nav.evaluate(el=>el.scrollHeight<=el.clientHeight)).toBe(true);
  expect(await page.evaluate(()=>document.documentElement.scrollHeight<=document.documentElement.clientHeight)).toBe(true);
  await nav.getByRole('button',{name:'60分',exact:true}).click();
  await expect(nav.getByRole('button',{name:'60分',exact:true})).toHaveAttribute('aria-pressed','true');
  const row=page.locator('.detail-instrument-row').first();
  const price=(await row.locator('strong').boundingBox())!, pct=(await row.locator('em').boundingBox())!;
  expect(pct.y).toBeGreaterThan(price.y); expect(pct.x+pct.width).toBeCloseTo(price.x+price.width,0);
  await page.getByRole('button',{name:'K线类型',exact:true}).click();
  const menu=page.getByRole('menu',{name:'K线类型'});
  expect(await menu.locator('button').evaluateAll(els=>els.every(el=>getComputedStyle(el).borderWidth==='0px'))).toBe(true);
  await page.screenshot({path:'test-results/r4-s4-detail.png'});
});

test('续4长数值不越槽、两页分组及周期垂直对齐', async ({page}) => {
  const bars=Array.from({length:80},(_,i)=>({barOpenTime:new Date(Date.UTC(2026,5,i+1)).toISOString(),open:-123456789.12,close:-123456788.98,high:-123456780.12,low:-123456799.12,volume:8e15,amount:9e18,openInterest:7e15,pctChange:12345678.12}));
  await setup(page,bars);
  const values=page.locator('.workbench-chart .quote-values');
  await expect(values.locator('[data-field="开"] strong')).toHaveText('-123,456,789.12');
  expect(await values.locator('strong').evaluateAll(els=>els.every(el=>el.scrollWidth<=el.clientWidth))).toBe(true);
  const rendered=await option(page);
  expect(rendered.grid[0].left).toBeGreaterThan(60);
  await page.getByRole('button',{name:'关闭图表',exact:true}).click();
  const board=page.locator('.boards .board').first();
  const overlay=board.locator('.chart-overlay');
  await expect(overlay).toBeVisible();
  const selector=board.locator('.board-header select');
  const a=(await board.locator('.board-header').boundingBox())!, b=(await selector.boundingBox())!;
  expect(Math.abs(b.y+b.height/2-(a.y+a.height/2))).toBeLessThanOrEqual(1);
  await expect(overlay.locator('.quote-field-group')).toHaveCount(7);
  expect(await overlay.locator('.quote-panel').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
  await expect(board.locator('.board-instrument strong')).toHaveText('600000');
  await selector.selectOption('1h');await expect(selector).toHaveValue('1h');
  await page.screenshot({path:'test-results/r4-s4-all.png'});
});

test('续4文本框active与激光共用连续绘制收尾', async ({page}) => {
  const state=await setup(page);
  const text=page.getByRole('button',{name:'文本框',exact:true});
  await text.click();await expect(text).toHaveClass(/active/);await expect(text).toHaveAttribute('aria-pressed','true');
  const canvas=page.getByLabel('激光笔画布');
  async function laser() {
    await page.getByRole('button',{name:'笔刷与激光笔',exact:true}).click();
    await page.getByRole('menuitem',{name:'激光笔',exact:true}).click();
    await expect(canvas).toHaveCSS('pointer-events','auto');
  }
  async function stroke() {
    const box=(await canvas.boundingBox())!;
    await canvas.hover({position:{x:box.width*.3,y:box.height*.3}});
    await page.mouse.down();
    await page.mouse.move(box.x+box.width*.6,box.y+box.height*.4,{steps:10});
    await page.mouse.up();
  }
  await laser();await stroke();
  await expect(page.getByRole('button',{name:'光标',exact:true})).toHaveClass(/active/);
  await expect(canvas).toHaveCSS('pointer-events','none');
  await expect.poll(async()=>Number(await canvas.getAttribute('data-point-count'))).toBeGreaterThan(2);
  await expect(canvas).toHaveAttribute('data-point-count','0',{timeout:4000});
  await page.getByRole('button',{name:'连续画线',exact:true}).click();
  await laser();await stroke();
  await expect(page.getByRole('button',{name:'笔刷与激光笔',exact:true})).toHaveClass(/active/);
  await expect(canvas).not.toHaveCSS('pointer-events','none');
  const box=(await canvas.boundingBox())!;
  await page.mouse.move(box.x+box.width*.3,box.y+box.height*.3);await page.mouse.down();
  await text.focus();await page.keyboard.press('Enter');
  await page.mouse.move(box.x+box.width*.6,box.y+box.height*.4,{steps:5});await page.mouse.up();
  await expect(text).toHaveClass(/active/);
  expect(state.saves()).toBe(0);
});
