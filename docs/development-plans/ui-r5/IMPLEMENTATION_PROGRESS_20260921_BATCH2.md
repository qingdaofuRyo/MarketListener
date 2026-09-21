# MarketListener TradingView UI 实施进度 — 第二批（2026-09-21）

状态：**第二批基础组件与回归基线已写入 `master`，等待可执行环境验证；不得视为 DONE。**

关联：`IMPLEMENTATION_PROGRESS_20260921.md`、`TRADINGVIEW_UI_IMPROVEMENT_PLAN.md`、`../../UI_Feasibility_Review_20260920.md`。

## 1. 本批新增实施

| 编号 | 优先级 | 状态 | 已实施内容 | 关键约束 |
|---|---|---|---|---|
| TV-IMP-08 | P1 | IMPLEMENTED / VERIFYING | 新增 `TerminalTable.vue`，提供 dense / standard / comfortable 外壳、sticky header、header/body/footer slots | 当前未替换 `MarketView.vue` 现有列表，避免在无本机回归结果时改变虚拟滚动 DOM |
| TV-IMP-09 | P0 | IMPLEMENTED / VERIFYING | 新增 `e2e/ui-r5-baseline.spec.ts`，锁定行情虚拟行数量、33px 行高、14 字段七组×两行、结构 token | 只建立回归门禁，不改变业务逻辑 |
| TV-IMP-10 | P1 | IMPLEMENTED / VERIFYING | 新增 `InstrumentList.vue` 虚拟列表结构壳，显式接收 top/bottom spacer、33px rowHeight、30px headerHeight 与 scroll 事件 | 当前未接入 `MarketView.vue`；不复制排序、选择、键盘定位、数据请求或虚拟窗口计算 |

## 2. 本批提交

- `54254281d0204a716a42270c783091b2db2429f8` — `feat(web): add terminal table primitive`
- `5682a55971b7958001e3abb2d2e1dca19322f6bc` — `test(web): add R5 terminal UI baseline`
- `e860c0d52aaececb2e36b9acf1906af532d9daf2` — `feat(web): add virtual instrument list shell`

## 3. 为什么暂不直接替换 MarketView 行情列表

当前 `MarketView.vue` 已经有固定行高虚拟窗口、上下占位、滚动定位和键盘选择逻辑。第二批先把视觉/结构 primitive 独立落地，再用 E2E 锁定现有行为，避免一次提交同时改变：

- scroll owner；
- 33px 行高与 30px 表头；
- `virtualStart` / `virtualEnd` 计算；
- 上下 spacer；
- 选中 ID；
- flag、排序、列拖拽；
- 键盘定位和滚动锚点。

只有基线验证通过后，才允许把现有 DOM 逐段迁入 `InstrumentList` / `TerminalTable`，且每次迁移只改变展示层，不重写数据流。

## 4. 第二批定向验证

在可执行工作树中执行：

```bash
cd desktop/web
npm run lint
npm run typecheck
npm run build
npx playwright test e2e/ui-r5-baseline.spec.ts --workers=1
npx playwright test e2e/market-list-r4.spec.ts --workers=1
```

若 `ui-r5-baseline.spec.ts` 失败，优先判定为回归信号，不直接放宽断言。允许调整断言的前提是确认原有业务/UI 约束已经正式变更。

## 5. 下一步

验证通过后按以下顺序继续：

1. 把 `MarketView.vue` 左侧列表的**纯展示 DOM**迁入 `InstrumentList`，保留现有虚拟窗口计算在 `MarketView.vue`。
2. 再把 header / row 的视觉容器迁入 `TerminalTable`，不先修改列定义和拖拽逻辑。
3. 迁移后复跑 `ui-r5-baseline.spec.ts`、`market-list-r4.spec.ts`、`terminal.spec.ts`。
4. 之后才评估 `ChartFrame` 在非核心图表区域的小范围接入。

## 6. 回退边界

- `TerminalTable.vue`、`InstrumentList.vue` 当前均未接入核心页面，可独立删除或调整，不影响现有行情逻辑。
- 新增 E2E 只提供门禁；若测试本身存在 fixture 问题，应修复 fixture，不通过删除业务约束断言来“变绿”。
- 不允许为了接入 primitive 改成全量 DOM、修改 `LIST_ROW_HEIGHT = 33`、改变七组×两行报价结构或移动图表工作区。
