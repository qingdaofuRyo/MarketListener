import { expect, test, type Page } from '@playwright/test';
import { setup } from './chart-workbench-r4.spec';
import { quoteFieldGroups } from '../src/domain/quoteFieldGroups';
import { chartAxisGutter, CHART_LAYOUT } from '../src/domain/chartLayout';
import { subchartSeries } from '../src/domain/chartSubchart';

test('续4公共分组、测量gutter和副图语义', () => {
  expect(quoteFieldGroups).toEqual([['开','收'],['高','低'],['涨幅','振幅'],['涨跌','结'],['量','额'],['持仓量','沉淀资金'],['总市值','流通市值']]);
  const labels=['-123,456.789','0','9,999,999'];
  expect(chartAxisGutter(labels,s=>s.length*6.3)).toBe(Math.ceil(labels[0].length*6.3)+CHART_LAYOUT.axisPadding+CHART_LAYOUT.axisLabelMargin);
  const series=subchartSeries([{volume:5,openInterest:1e12},{volume:0,openInterest:null}], 'openInterest',['red','green'],'blue');
  expect(series[0]).toMatchObject({type:'bar',yAxisIndex:1,itemStyle:{opacity:0.8}});
  expect(series[1]).toMatchObject({type:'line',yAxisIndex:2,connectNulls:false,data:[1e12,null]});
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
  const overlay=page.locator('.boards .chart-overlay').first();
  await expect(overlay).toBeVisible();
  const selector=overlay.getByRole('combobox');
  const a=(await overlay.boundingBox())!, b=(await selector.boundingBox())!;
  expect(b.y+b.height/2).toBeCloseTo(a.y+a.height/2,0);
  await expect(overlay.locator('.quote-field-group')).toHaveCount(7);
  expect(await overlay.locator('.quote-panel').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
  await expect(overlay.locator('.instrument-info small')).toHaveText('600000');
  await selector.selectOption('1h');await expect(selector).toHaveValue('1h');
  await page.screenshot({path:'test-results/r4-s4-all.png'});
});

test('续4文本框active与激光共用连续绘制收尾', async ({page}) => {
  const state=await setup(page);
  const text=page.getByRole('button',{name:'文本框',exact:true});
  await text.click();await expect(text).toHaveClass(/active/);await expect(text).toHaveAttribute('aria-pressed','true');
  const box=(await page.locator('.workbench-chart .chart-root').boundingBox())!;
  async function laser() {await page.getByRole('button',{name:'笔刷与激光笔',exact:true}).click();await page.getByRole('menuitem',{name:'激光笔',exact:true}).click();}
  async function stroke() {await page.mouse.move(box.x+box.width*.3,box.y+box.height*.3);await page.mouse.down();await page.mouse.move(box.x+box.width*.6,box.y+box.height*.4,{steps:10});await page.mouse.up();}
  await laser();await stroke();
  await expect(page.getByRole('button',{name:'光标',exact:true})).toHaveClass(/active/);
  const canvas=page.getByLabel('激光笔画布');
  await expect(canvas).toHaveCSS('pointer-events','none');
  await expect.poll(async()=>Number(await canvas.getAttribute('data-point-count'))).toBeGreaterThan(2);
  await expect(canvas).toHaveAttribute('data-point-count','0',{timeout:4000});
  await page.getByRole('button',{name:'连续画线',exact:true}).click();
  await laser();await stroke();
  await expect(page.getByRole('button',{name:'笔刷与激光笔',exact:true})).toHaveClass(/active/);
  await expect(canvas).not.toHaveCSS('pointer-events','none');
  await page.mouse.move(box.x+box.width*.3,box.y+box.height*.3);await page.mouse.down();
  await text.focus();await page.keyboard.press('Enter');
  await page.mouse.move(box.x+box.width*.6,box.y+box.height*.4,{steps:5});await page.mouse.up();
  await expect(text).toHaveClass(/active/);
  expect(state.saves()).toBe(0);
});
