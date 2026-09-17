import {test,expect} from '@playwright/test';
const source = `def attention():
    return {"long": close > 10, "short": False, "cancel": close < 5, "lookback": 1}
def position():
    return {"rewardRisk": 2, "winRate": observed_win_rate, "allocation": 0.5, "capitalUsage": None, "leverage": 0.5}
def timing():
    return {"open": close > 12, "add": False, "reduce": False, "close": close < 8}
`;
test('composite Python strategy replaces four action types and supports versioned editing and recovery',async({page})=>{
  page.on('pageerror',error=>{throw error;});
  let definitions:Array<Record<string,any>>=[], deleted:Record<string,any>|undefined;
  await page.route('**/api/composites/monitor',route=>route.fulfill({json:{items:[],events:[]}}));
  await page.route('**/api/composites/validate',route=>{
    expect(route.request().postDataJSON().source).toContain('def timing');
    return route.fulfill({json:{valid:true}});
  });
  await page.route('**/api/composites/definitions**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/restore')){definitions.push({...deleted!,enabled:false});return route.fulfill({json:deleted});}
    if(route.request().method()==='DELETE'){deleted=definitions.find(item=>path.endsWith(item.id));definitions=definitions.filter(item=>item!==deleted);return route.fulfill({json:{deleted:true,recoverable:true}});}
    if(['POST','PUT'].includes(route.request().method())){
      const body=route.request().postDataJSON();
      expect(body).not.toHaveProperty('action');expect(body.source).toContain('def attention');expect(body.source).toContain('def position');expect(body.source).toContain('def timing');
      const previous=definitions.find(item=>path.endsWith(item.id));
      const item={...body,id:previous?.id||'combo',version:(previous?.version||0)+1};
      definitions=definitions.filter(item=>item!==previous);definitions.push(item);return route.fulfill({json:item});
    }
    return route.fulfill({json:{items:definitions,template:source,legacyCount:2}});
  });
  await page.goto('/strategy/?section=strategy');
  await expect(page.locator('.composite-strategy-manager')).toContainText('暂无组合策略');
  await expect(page.getByRole('navigation',{name:'客户端',exact:true}).getByRole('link',{name:'F10',exact:true})).toBeVisible();
  await expect(page.getByRole('navigation',{name:'后端',exact:true}).getByRole('link',{name:'F10',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'新建策略',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'新建组合策略',exact:true});
  await dialog.getByRole('textbox',{name:'组合策略名称',exact:true}).fill('趋势组合');
  await expect(dialog.getByRole('textbox',{name:'组合策略Python代码'})).toHaveValue(source);
  await expect(dialog.getByRole('combobox',{name:'策略操作',exact:true})).toHaveCount(0);
  await dialog.getByRole('button',{name:'校验Python',exact:true}).click();
  await expect(dialog).toContainText('三类规则和函数依赖校验通过');
  await dialog.getByRole('button',{name:'保存组合策略',exact:true}).click();
  await expect(dialog).not.toBeVisible();
  const row=page.locator('.el-table__row').filter({hasText:'趋势组合'});
  await row.getByRole('button',{name:'编辑',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'编辑组合策略',exact:true});
  await editor.getByRole('textbox',{name:'组合策略名称',exact:true}).fill('趋势组合已修改');
  await editor.getByRole('button',{name:'保存组合策略',exact:true}).click();
  await expect(editor).not.toBeVisible();
  expect(definitions[0].version).toBe(2);
  page.once('dialog',dialog=>dialog.accept());
  await row.getByRole('button',{name:'删除',exact:true}).click();
  await expect(page.locator('.composite-strategy-manager .el-table__row')).toHaveCount(0);
  await page.getByRole('button',{name:'撤销删除',exact:true}).click();
  await expect(page.locator('.composite-strategy-manager .el-table__row')).toHaveCount(1);
  await page.screenshot({path:'test-results/r4-composite-strategy.png'});
});
test('target market shows only composite attention with position metrics and repeatable timing',async({page})=>{
  await page.route('**/api/market/categories',route=>route.fulfill({json:{items:[]}}));
  await page.route('**/api/composites/definitions',route=>route.fulfill({json:{items:[{id:'combo',displayName:'组合A',enabled:true}]}}));
  const item={strategyId:'combo',strategyVersion:1,strategyName:'组合A',instrumentId:'CN.SHFE.FUTURE.AU0',name:'黄金主连',symbol:'AU0',direction:'long',positionOpen:false,latestPrice:100,referenceAt:'2026-09-01T07:00:00Z',asOf:'2026-09-15T07:00:00Z',changePct:12,peerCount:0,peers:[],position:{rewardRisk:2,winRate:null,allocation:0.5,capitalUsage:null,leverage:0.5,observedRounds:0}};
  await page.route('**/api/composites/monitor',route=>route.fulfill({json:{items:[],events:[]}}));
  await page.route('**/api/composites/scan',route=>route.fulfill({json:{items:[item],events:[],scanned:1,total:1,nextOffset:null}}));
  await page.goto('/market/targets/');
  await page.getByRole('button',{name:'扫描组合策略',exact:true}).click();
  await expect(page.locator('.composite-results')).toContainText('等待开仓时机');
  await expect(page.locator('.composite-results')).toContainText('50.00%');
  await expect(page.locator('.composite-results')).toContainText('0个已结束轮次');
  await page.getByRole('button',{name:'黄金主连',exact:true}).click();
  await expect(page).toHaveURL(/market\/instrument\/CN.SHFE.FUTURE.AU0/);
});
