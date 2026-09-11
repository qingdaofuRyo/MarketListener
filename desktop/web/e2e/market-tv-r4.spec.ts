import { expect, test, type Page } from '@playwright/test';
import { setup } from './chart-workbench-r4.spec';
import { CHART_BARS, chartAxisGutter, chartAxisScale } from '../src/domain/chartLayout';
import { advanceReplay, replayRange, REPLAY_SPEEDS } from '../src/domain/chartReplay';
import { subchartSeries } from '../src/domain/chartSubchart';
import { chartTypeGroups } from '../src/domain/chartPresentation';

async function option(page: Page) {
  return page.locator('.workbench-chart .chart-root').evaluate(async el => {
    const resource = performance.getEntriesByType('resource').find(item=>/\/echarts-[^/]+\.js$/.test(item.name));
    if (!resource) throw new Error('Missing production chart module');
    const module = await import(resource.name);
    const init = Object.values(module)[0] as (el:Element)=>{getOption:()=>unknown};
    return JSON.parse(JSON.stringify(init(el).getOption()));
  });
}
test('TV公共柱宽、独立副轴、菜单目录和回放窗口',()=>{
  expect(CHART_BARS).toEqual({candle:{barWidth:'80%',barMaxWidth:24},volume:{barWidth:'90%',barMaxWidth:28}});
  expect(chartAxisGutter(['-100.01'],v=>v.length*6)).toBe(47);
  const series=subchartSeries([{volume:0,amount:1e15},{volume:undefined,amount:null}],'amount',['red','green'],'blue');
  expect(series[0]).toMatchObject({barWidth:'90%',barMaxWidth:28,yAxisIndex:1});
  expect(series[1]).toMatchObject({type:'line',yAxisIndex:2,data:[1e15,null]});
  expect(chartTypeGroups.flatMap(group=>group.items).length).toBe(5);
  for(const count of [1,5,80,500]) { const range=replayRange(count,80);expect(range.end-range.start+1).toBe(80);expect(range.end).toBeGreaterThanOrEqual(count+4); }
  expect(advanceReplay(79,80,10)).toBe(80); expect(REPLAY_SPEEDS).toContain(10);
  for(const [min,max] of [[-125.5,230.9],[0,0],[0,1e15],[0.00012,0.00018]]) {
    const scale=chartAxisScale(min,max);expect(scale.min).toBeLessThanOrEqual(min);expect(scale.max).toBeGreaterThanOrEqual(max);expect(scale.ticks.length).toBeLessThan(15);expect(scale.interval).toBeGreaterThan(0);
  }
});

test('TV菜单键盘、分组浮层及双轴柱宽真实配置',async({page})=>{
  await setup(page);
  const current=await option(page);
  expect(current.series[0]).toMatchObject(CHART_BARS.candle);
  expect(current.series.find((s:{id:string})=>s.id==='market-volume')).toMatchObject(CHART_BARS.volume);
  expect(current.yAxis[0].min).toBe(current.yAxis.at(-1).min);
  expect(current.yAxis[0].max).toBe(current.yAxis.at(-1).max);
  const trigger=page.getByRole('button',{name:'K线类型',exact:true});
  await trigger.click(); const menu=page.getByRole('menu',{name:'K线类型',exact:true});
  await expect(menu.getByRole('group')).toHaveCount(3);
  await page.keyboard.press('End'); await page.keyboard.press('Enter');
  await expect(page.locator('.workbench-chart .chart-root')).toHaveAttribute('data-chart-type','heikin');
  await expect(trigger).toBeFocused();
  await page.getByRole('button',{name:'笔刷与激光笔',exact:true}).click();
  const drawing=page.getByRole('menu',{name:'笔刷与激光笔',exact:true}); await expect(drawing).toBeVisible();
  const box=(await drawing.boundingBox())!; expect(box.x).toBeGreaterThanOrEqual(0);expect(box.x+box.width).toBeLessThanOrEqual(1440);
  await page.keyboard.press('Escape'); await expect(drawing).toHaveCount(0);
});

test('TV回放预览、前缀、空槽、跟随与恢复',async({page})=>{
  await setup(page);
  const root=page.locator('.workbench-chart .chart-root');
  const before=await option(page);
  await page.getByRole('button',{name:'回放',exact:true}).click();
  const box=(await root.boundingBox())!;
  await page.mouse.move(box.x+box.width*.35,box.y+box.height*.3);
  await expect(page.locator('.replay-preview')).toBeVisible();
  await expect(page.locator('.replay-preview')).toContainText(/2026-.*周/);
  await page.mouse.click(box.x+box.width*.35,box.y+box.height*.3);
  await expect(page.getByRole('button',{name:'播放',exact:true})).toBeEnabled();
  const picked=await option(page);
  const length=picked.series[0].data.length;
  expect(length).toBeLessThan(80);
  expect(picked.xAxis[0].data.filter((v:string)=>v==='').length).toBeGreaterThanOrEqual(5);
  expect(picked.dataZoom[0].endValue-picked.dataZoom[0].startValue).toBe(before.dataZoom[0].endValue-before.dataZoom[0].startValue);
  await page.getByRole('button',{name:'下一根',exact:true}).click();
  await expect.poll(async()=> (await option(page)).series[0].data.length).toBe(length+1);
  await page.getByRole('combobox',{name:'回放速度'}).selectOption('10');
  await page.getByRole('button',{name:'播放',exact:true}).click();
  await expect.poll(async()=> (await option(page)).series[0].data.length).toBeGreaterThan(length+2);
  await page.getByRole('button',{name:'暂停',exact:true}).click();
  await page.mouse.move(box.x+box.width*.5,box.y+box.height*.3);await page.mouse.wheel(0,-160);
  await expect(page.getByRole('button',{name:'恢复跟随'})).toHaveAttribute('aria-pressed','false');
  await page.getByRole('button',{name:'恢复跟随'}).click();
  await expect(page.getByRole('button',{name:'恢复跟随'})).toHaveAttribute('aria-pressed','true');
  await page.screenshot({path:'test-results/r4-tv-replay.png'});
  await page.getByRole('button',{name:'播放',exact:true}).click();
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
  await expect(page.getByRole('button',{name:'播放',exact:true})).toBeVisible();
  await page.evaluate(()=>{delete (document as unknown as {hidden?:boolean}).hidden;document.dispatchEvent(new Event('visibilitychange'));});
  await expect(page.getByRole('button',{name:'播放',exact:true})).toBeVisible();
  for(let i=0;i<8;i++) { const forward=page.getByRole('button',{name:'快进10根'});if(await forward.isEnabled()) await forward.click(); }
  await expect(page.getByRole('button',{name:'播放',exact:true})).toBeDisabled();
  await expect.poll(async()=> (await option(page)).series[0].data.length).toBe(80);
  await page.getByRole('button',{name:'退出回放'}).click();
  await expect.poll(async()=> (await option(page)).series[0].data.length).toBe(80);
  const after=await option(page);expect(after.dataZoom[0].startValue).toBe(before.dataZoom[0].startValue);expect(after.dataZoom[0].endValue).toBe(before.dataZoom[0].endValue);
  await page.getByRole('button',{name:'回放',exact:true}).click();
  await page.getByRole('navigation',{name:'K线周期'}).getByRole('button',{name:'60分',exact:true}).click();
  await expect(page.getByLabel('K线回放', {exact:true})).toHaveCount(0);
  await expect(page.getByRole('navigation',{name:'K线周期'}).getByRole('button',{name:'60分',exact:true})).toHaveAttribute('aria-pressed','true');
});

test('TV桌面布局缩放、主题与DPR',async({page,browser})=>{
  await setup(page);
  for(const colorScheme of ['light','dark'] as const) for(const [width,height] of [[1366,768],[1920,1080],[2560,1440]]) for(const zoom of [1,1.25,1.5]) {
    await page.emulateMedia({colorScheme});
    await page.setViewportSize({width,height});
    await page.evaluate(z=>{document.documentElement.style.zoom=String(z);},zoom);
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight<=document.documentElement.clientHeight)).toBe(true);
    const row=page.locator('.detail-instrument-row').first();
    expect(await row.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
    const root=page.locator('.workbench-chart .chart-root');
    await expect.poll(async()=> (await root.boundingBox())!.height).toBeGreaterThan(100);
    await page.getByRole('button',{name:'K线类型',exact:true}).click();
    expect(await page.getByRole('menu',{name:'K线类型',exact:true}).evaluate(el=>{const b=el.getBoundingClientRect();return b.x>=0&&b.right<=innerWidth&&b.y>=0&&b.bottom<=innerHeight;})).toBe(true);
    await page.keyboard.press('Escape');
  }
  const context=await browser.newContext({viewport:{width:1366,height:768},deviceScaleFactor:2});
  const hi=await context.newPage();await setup(hi);
  expect(await hi.evaluate(()=>devicePixelRatio)).toBe(2);
  await hi.screenshot({path:'test-results/r4-tv-dpr2.png'});await context.close();
});
