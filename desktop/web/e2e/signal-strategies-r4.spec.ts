import {test,expect} from '@playwright/test';

test('four signal operations replace default backtest strategies and support editing and recoverable deletion',async({page})=>{
  page.on('pageerror',error=>{throw error});
  await page.setViewportSize({width:1440,height:960});
  let definitions:Array<Record<string,any>>=[], deleted:Record<string,any>|undefined;
  await page.route('**/api/signals/definitions**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/restore')){definitions.push(deleted!);await route.fulfill({json:deleted});return;}
    if(route.request().method()==='DELETE'){deleted=definitions.find(item=>path.endsWith(item.id));definitions=definitions.filter(item=>item!==deleted);await route.fulfill({json:{deleted:true,recoverable:true}});return;}
    if(['POST','PUT'].includes(route.request().method())){
      const body=route.request().postDataJSON();
      expect(body).not.toHaveProperty('positionSizing');expect(body).not.toHaveProperty('backtest');expect(body).not.toHaveProperty('supportedAssetTypes');
      const previous=definitions.find(item=>path.endsWith(item.id));
      const item={...body,id:previous?.id||'signal_'+definitions.length,version:(previous?.version||0)+1};
      definitions=definitions.filter(item=>item!==previous);definitions.push(item);await route.fulfill({json:item});return;
    }
    await route.fulfill({json:{items:definitions}});
  });
  await page.goto('/strategy/?section=strategy');
  await expect(page.locator('.signal-strategy-manager')).toContainText('暂无策略');
  for(const name of ['回测','归档','历史版本','复制','加载到图表'])await expect(page.getByRole('button',{name,exact:true})).toHaveCount(0);
  for(const [action,label] of [['open','开仓'],['add','加仓'],['reduce','减仓'],['close','平仓']]){
    await page.getByRole('button',{name:'新建策略',exact:true}).click();
    const dialog=page.getByRole('dialog',{name:'新建策略',exact:true});
    await dialog.getByRole('textbox',{name:'策略名称',exact:true}).fill(label+'观察');
    await dialog.locator('.el-select').filter({has:page.getByRole('combobox',{name:'策略操作',exact:true})}).click();
    await page.getByRole('option',{name:label,exact:true}).click();
    await dialog.locator('.el-select').filter({has:page.getByRole('combobox',{name:'策略周期',exact:true})}).click();
    for(const period of ['5分','15分','30分','60分','120分','日线','周线','月线','季线','年线'])await expect(page.getByRole('option',{name:period,exact:true})).toBeVisible();
    await page.getByRole('option',{name:'120分',exact:true}).click();
    for(const name of ['市场类型','仓位','滑点','止盈','初始资金'])await expect(dialog).not.toContainText(name);
    await dialog.getByRole('button',{name:'保存策略',exact:true}).click();
    await expect(dialog).not.toBeVisible();
    expect(definitions.find(item=>item.action===action)?.period).toBe('2h');
  }
  const first=page.locator('.el-table__row').filter({hasText:'开仓观察'});
  await first.getByRole('button',{name:'编辑',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'编辑策略',exact:true});
  await editor.getByRole('textbox',{name:'策略名称',exact:true}).fill('开仓观察已修改');
  await editor.getByRole('button',{name:'保存策略',exact:true}).click();
  await expect(editor).not.toBeVisible();
  page.once('dialog',dialog=>dialog.accept());
  await first.getByRole('button',{name:'删除',exact:true}).click();
  await expect(page.locator('.el-table__row')).toHaveCount(3);
  await page.getByRole('button',{name:'撤销删除',exact:true}).click();
  await expect(page.locator('.el-table__row')).toHaveCount(4);
  await page.screenshot({path:'test-results/r4-signal-strategies.png',animations:'disabled'});
});
