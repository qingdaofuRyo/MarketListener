# MarketListener R5 — TradingView 参考 UI/UX 改进计划

日期：2026-09-20  
状态：规划建议，尚未实施代码改造  
适用范围：`desktop/web` 桌面 Web 端  
关联文档：`docs/UI_Modernization_Plan_20260920.md`、`Plan_R4.md`

复核更新：实施条件见 [UI 可行性复核](../../UI_Feasibility_Review_20260920.md)。本次修订消除骨架、自适应、虚拟列表、生命周期和验收依赖的歧义；没有实施代码或改变活动任务状态。

> 本文件是现有 UI Modernization 计划的 TradingView 专项补充，不建立与 `Plan_R4.md` 竞争的第二套任务状态。真正进入开发时，应把对应任务登记进当前活动计划。本计划不修改 Android，不改变行情、策略、数据口径，不更换 ECharts 或 Element Plus，不做 TradingView 像素级克隆。

## 1. 核心设计决策

MarketListener 桌面 Web 正式以 TradingView Supercharts 的**工作区架构与交互组织方式**作为主要参考，而不是以其品牌、Logo、图标、Pine 生态、交易面板或视觉素材作为复制对象。

设计原则：

> **TradingView is an interaction architecture reference, not a pixel reference.**

MarketListener 的目标是形成：

- chart-first 的金融研究工作区；
- 保留左侧列表、现有图表/图上报价、详情右侧绘图栏和底部周期栏的稳定结构；
- 高信息密度、弱装饰、强上下文；
- 长时间看盘时低视觉疲劳；
- 表格、K 线、14 字段、指标、策略状态之间稳定联动；
- 保留用户已熟悉的导航、业务流程和页面位置。

不把 MarketListener 改造成“大卡片 SaaS Dashboard”，也不为了视觉现代化牺牲有效显示面积。

## 2. TradingView 模式与 MarketListener 对应

| TradingView 模式 | MarketListener 对应 | 本项目建议 |
|---|---|---|
| Supercharts 中央主图 | `MarketView` / `FuturesView` ECharts | 图表保持最高视觉权重 |
| Top toolbar | 现有周期、指标、图表操作 | 抽象 `ChartToolbar` 的交互样式，不迁移底部周期栏或新增外部 header |
| Watchlist | 全部行情、目标行情、期货列表 | 抽象 `InstrumentList` |
| Symbol details | 图上 14 字段、标的信息 | 原样复用 `QuoteValues`，必要时提取 `MetricGrid`，不新建右侧报价栏 |
| Right toolbar / contextual panel | 既有右侧绘图工具及状态入口 | `ContextRail` 仅为后续职责研究，不替换绘图栏 |
| Bottom panel | 策略、日志、分析结果 | 中长期形成 `WorkspacePanel`，不作为首轮必做 |
| Screener table | 行情表格 / 统计表格 | 统一 `TerminalTable` |
| Layout workspace | 页面工作状态 | 先做 layout abstraction，不立即复制多图布局 |
| Command search | 快速跳转工具 | 后续增强，非首轮 P0 |

不直接照搬：TradingView 完整绘图工具、Pine、经纪商交易、社区、Ideas、十几种 multi-chart layout、品牌图标与专有视觉资产。

## 3. 推荐工作区骨架

以当前页面实际布局为骨架，组件名表示职责，不表示重新分配空间：

| 页面区域 | 保留的位置与行为 |
|---|---|
| 全局导航 | 客户端 / 后端现有分组与顶部位置 |
| 全部行情 / 目标行情 | 左列表、右侧既有双看板；列表查询工具仍在左侧 |
| 标的详情 | 左列表、中图表、最右绘图工具栏、底部 36px 周期栏 |
| 14 字段 / 指标图例 | 沿用图上 overlay，七组两行；不额外占用主图外部高度 |
| 状态提示 | 原位插槽或轻量 overlay，不增加永久标题栏 |
| 可选工作面板 | 首轮不新增 Bottom Workspace 或替换右栏；留作后续需求研究 |

首轮不重排顶部导航，不把详情或报价搬入新 ContextRail。重点是让已有结构更统一和可维护。

## 4. 组件拆分策略

优先拆展示责任，不先重写数据流：

```text
MarketView.vue
├─ MarketWorkspace.vue
│  ├─ InstrumentList.vue
│  ├─ ChartWorkspace.vue
│  │  ├─ ChartToolbar.vue
│  │  ├─ ChartFrame.vue
│  │  └─ KLineChart.vue（现有实例 owner）
│  └─ InstrumentDetails.vue
│     └─ MetricGrid.vue
└─ composables / orchestration

StrategyView.vue
├─ StrategyNavigator.vue
├─ StrategyEditor.vue
├─ StrategyParameters.vue
├─ StrategyValidation.vue
└─ StrategyResults.vue
```

原则：业务 API、store、行情语义与数据格式保持不变；先把视觉容器、状态与交互边界抽出。

上图为职责候选，不要求增加 DOM wrapper。EChartsHost 只在后续证据充分时单独评估；策略拆分以现行 CompositeStrategyManager 和 attention/position/timing 为准，保留旧资源兼容，不恢复已移除入口。

## 5. Design Token 扩展

在现有 `design/tokens.ts` 上扩展，不推翻现有涨红跌绿、浅色/深色 palette。

建议新增：

```text
--ml-bg-workspace
--ml-bg-panel
--ml-bg-chart
--ml-bg-elevated
--ml-surface-hover
--ml-surface-selected
--ml-border-subtle
--ml-border-strong
--ml-focus-ring
--ml-row-height-dense
--ml-control-height-sm
--ml-panel-header-height
--ml-radius-control
--ml-radius-popover
--ml-space-1 ... --ml-space-8
--ml-font-data-sm
--ml-font-data-md
--ml-motion-fast
--ml-motion-normal
--ml-layer-base
--ml-layer-sticky
--ml-layer-toolbar
--ml-layer-popover
--ml-layer-modal
--ml-layer-toast
```

所有行情数值统一：

```css
.ml-numeric {
  font-variant-numeric: tabular-nums;
}
```

数字列右对齐，名称/代码左对齐；不要用替换字体的方式解决数字跳动问题。

## 6. 三档信息密度

统一支持：

- `dense`：全部行情、目标行情、期货、日志、高频列表；
- `standard`：策略、数据、统计；
- `comfortable`：设置、帮助、引导。

密度由 primitive 控制行高、padding、字号与控件高度，不允许每个页面私自写一套 spacing。

首轮仅将现有密度命名，不新增切换项；行情虚拟列表保持 33px。未来切密度必须同步行高、表头、占位、窗口索引、键盘定位和滚动锚点，不能只改 CSS。

## 7. 行情列表 / Watchlist 设计

`InstrumentList` / `TerminalTable` 必须统一：

- sticky header；
- 三态排序清晰；
- hover、selected、focus-visible 是三个不同视觉状态；
- 上涨/下跌只强调关键数字，不默认整行铺红/绿；
- 选中行使用弱 accent surface 或左侧 indicator；
- 表格列宽和数值格式稳定；
- 列表切换标的时不销毁整个右侧工作区；
- 高频 quote tick 不应触发整个 workspace re-render；
- 行情列表已经有固定行高虚拟窗口，提取组件须保留；不改成全量 DOM 表格，仅在 trace 证明不足时评估更复杂实现。

## 8. 14 字段详情规范

继续严格保留七组两字段业务排布：

1. 开 / 收
2. 高 / 低
3. 涨幅 / 振幅
4. 涨跌 / 结
5. 量 / 额
6. 持仓量 / 沉淀资金
7. 总市值 / 流通市值

抽象 `MetricPair` / `MetricGrid`：

- 标签不使用省略号遮挡；
- 长数字使用统一格式与最小宽度；
- 单位、正负号、小数位规则统一；
- 首轮保持七组×两行及现有 overlay；窄容器先检查工具/标签/数值预算，不自动改变组序和行数；
- 全部行情右侧详情与标的详情页复用同一个组件。

## 9. ChartFrame / ChartToolbar

`ChartFrame` 只管理图表外壳：

- title / subtitle；
- timeframe / legend；
- loading / empty / error / stale；
- updatedAt / source status；
- toolbar slot；
- tooltip shell。

首轮 ECharts instance 生命周期继续由现有 `KLineChart` 管理，`ChartFrame` 仅接收状态/插槽，不负责 API/store，也不新增第二组 resize 监听。是否抽取 `EChartsHost` 留作独立、有回归证据的重构；保留缩放、绘图、回放、指针穿透及 DPR 必要重建语义。

`ChartToolbar` 参考 TradingView 上方工具区的组织方式，但只保留 MarketListener 实际存在的功能：

- 周期；
- 指标；
- 图表类型；
- 复权/显示选项（如项目已有）；
- 必要的全屏/重置等动作。

不增加无业务基础的绘图工具、交易按钮或 Pine 类入口。

## 10. 图表视觉规范

- chart background 与 panel background 接近但可区分；
- grid line 低对比；
- axis / crosshair 清晰但克制；
- volume 与主图分层；
- tooltip 不长期遮住主体蜡烛；
- 涨跌色与全站 token 一致；
- quote 更新不做 bounce / scale / glow；
- 图表 resize 使用 `ResizeObserver` + 节流/去抖；
- 普通 reactive update 不销毁并重建 ECharts instance。

## 11. Interaction State Matrix

所有 primitive 明确支持：

| 状态 | 视觉要求 | 交互/语义要求 |
|---|---|---|
| default | neutral | 正常 role/name |
| hover | 弱 surface | 不改变 selected 语义 |
| focus-visible | 独立 focus ring | Tab 可达 |
| selected | accent indicator/surface | `aria-selected` 适用时 |
| active | toolbar accent | `aria-pressed` 适用时 |
| disabled | 降低强调但仍可读 | 禁止触发动作 |
| loading | 稳定 skeleton | `aria-busy` |
| empty | 明确无数据 | 不与 error 混淆 |
| error | 错误 + 重试 | 清晰状态 |
| stale | 弱化 + 状态说明 | 与 fresh 区分 |
| positive | market-up | 同时保留 `+` / 数值 |
| negative | market-down | 同时保留 `-` / 数值 |
| flat | neutral | 不误用涨跌色 |

禁止把 hover、selected、focus 全部做成同一蓝色背景。

## 12. Layer / Z-index 规范

统一层级，禁止组件私自使用 `9999`：

```css
:root {
  --ml-layer-base: 0;
  --ml-layer-chart-overlay: 10;
  --ml-layer-sticky: 20;
  --ml-layer-toolbar: 30;
  --ml-layer-rail: 40;
  --ml-layer-dropdown: 100;
  --ml-layer-popover: 120;
  --ml-layer-modal-backdrop: 200;
  --ml-layer-modal: 210;
  --ml-layer-toast: 300;
}
```

ECharts HTML tooltip 必须明确归属 chart overlay / popover 层。

## 13. Motion System

建议：

```text
80–120ms：hover / focus
160–180ms：dropdown / popover
180–220ms：panel open / close
0ms：行情数字本身的 layout movement
```

只优先动画 `opacity` / `transform`。禁止：大面积 blur、backdrop-filter、持续 pulse、每次价格更新 bounce/glow、长时间 spring。

支持 `prefers-reduced-motion`。

## 14. Container Queries 与 Chrome 108

Chrome 108 可使用**尺寸型** Container Queries，因此以下组件优先采用 container-aware layout：

- `MetricGrid`；
- `InstrumentList`；
- `ChartToolbar`；
- `TerminalPanel`；
- `StrategyParameters`；
- `ChartFrame` legend / actions。

规则：

- size container query 可以作为 Chrome 108 主路径；
- style query 依赖 Chromium 111+，禁止作为核心依赖；
- 更现代 Popover / Anchor Positioning / View Transition 只能渐进增强；
- 关键功能必须保留 Element Plus / 常规 DOM 定位 fallback。

还需核对生产构建目标和既有 `color-mix()` 后备颜色；当前 Web Vite 配置未显式指定 Chrome 108。现代 Chrome 的 Playwright 通过不代表 Chrome 108 通过。

## 15. 无障碍与键盘

至少要求：

- icon-only button 有 accessible name；
- Tab order 与视觉顺序一致；
- `focus-visible` 不被清除；
- 上涨/下跌不只依赖红绿；
- table sort 使用 `aria-sort`；
- segmented control 可用键盘；
- dialog 有标题、Esc、焦点管理；
- loading/error/stale 有状态语义；
- 重要 OHLC / 14 字段不可只存在于 Canvas 图形内。

## 16. R5 任务建议

| ID | P | 任务 | 主要修改对象 | 依赖 | 完成标准 |
|---|---|---|---|---|---|
| R5-UI-001 | P0 | UI inventory + baseline screenshots | docs/tests | 无 | 固定 light/dark、主要 viewport、核心页面截图 |
| R5-UI-002 | P0 | 完整 token layer | `design/tokens.ts`, styles | 001 | 颜色语义不变，新增 spacing/radius/layer/motion/density |
| R5-UI-003 | P0 | state/layer/motion/focus primitives | design/styles | 002 | 状态矩阵可复用 |
| R5-UI-004 | P0 | `TerminalPanel` / `SegmentedControl` | components | 002-003 | 两个以上页面复用 |
| R5-UI-005 | P0 | `TerminalTable` | market/futures tables | 004 | sticky、数字对齐、三态排序、focus/selected 分离 |
| R5-UI-006 | P0 | TradingView-style `InstrumentList` | MarketView | 005 | 选择稳定、quote tick 不整页重渲染 |
| R5-UI-007 | P0 | `ChartFrame` + `ChartToolbar` | charts | 002-004、OPT-002/003/012 状态契约 | 状态对应正确数据；保留原图表 owner、plot rect 和工具位置 |
| R5-UI-008 | P0 | 原位 `QuoteValues` / `MetricGrid` 复用 | market detail | 004 | 14 字段七组×两行和 overlay 位置完全保留 |
| R5-UI-009 | P1 | Container-aware workspace | list/details/chart | 006-008 | Chrome108 size CQ；窄容器不遮挡信息 |
| R5-UI-010 | P1 | Strategy/Data/Stats primitive migration | heavy views | 004-009 | 不改业务流程，逐页迁移 |
| R5-UI-011 | P0 | Playwright visual + interaction suite | tests | 001，随每项迁移执行 | 首次改动前有基线；每项有截图 + 键盘 + 状态断言 |
| R5-UI-012 | P0 | performance/a11y/Chrome108 release gate | CI/docs | 001/011 与本批实际交付项 | 性能、兼容、可访问性通过；不等待未实施 P1/P2 |
| R5-UI-013 | P2 | ContextRail abstraction | market/futures | 006-009 | 不增加无业务意义入口 |
| R5-UI-014 | P2 | Optional WorkspacePanel | strategy/log/data | 010 | 仅真实需要时引入，不强制复制 TradingView bottom panel |
| R5-UI-015 | P2 | Command search prototype | global | 010+ | 只覆盖已有命令与页面，不引入复杂命令系统 |

## 17. Visual Regression Matrix

Playwright 至少固定：

```text
market-default-light-1440x900
market-default-dark-1440x900
market-1280x720
market-1920x1080
market-list-hover
market-list-selected
market-list-keyboard-focus
market-positive-values
market-negative-values
market-flat-values
market-chart-loading
market-chart-empty
market-chart-error
market-chart-stale
market-details-wide
market-details-narrow
market-toolbar-open
market-popover-open
futures-default-light
futures-default-dark
futures-row-selected
focus-instrument-list
focus-chart-toolbar
reduced-motion
```

截图测试之外必须包含真实交互断言：列表选中 → 图表更新 → 详情同步，同时 chart workspace 骨架保持稳定。

## 18. 性能门禁

每个 UI PR 检查：

```text
[ ] ECharts instance 不因普通 reactive update 重建
[ ] resize 使用 ResizeObserver + 节流/去抖
[ ] ticker 更新不触发整个 workspace re-render
[ ] table row hover 不使用 JS state
[ ] 高频表格区域不使用 backdrop-filter
[ ] 行情数值不使用持续 CSS animation
[ ] 不新增无必要的全局 window resize listener
[ ] bundle 无异常增长
[ ] Chrome 108 不依赖 style queries / 新 Popover API
```

性能回归以**相对基线**为主要门禁：同 fixture、viewport、browser version 下，median interaction/render benchmark 不建议恶化超过 10%；超过 10% 必须说明，超过 20% 默认阻止合并，除非有明确功能收益和批准记录。

同时遵循 Optimization_Plan 的 P95 >10% 复测规则；median 不能替代尾部延迟。固定机器、字体、DPR、数据版本和缓存状态，记录原始样本、长任务、实例/监听器数量；未测量不声称通过。

## 19. 禁止事项

1. 不把 TradingView 视觉素材、图标、品牌色、Logo 复制进项目。
2. 不把现有页面整体改成 TradingView 导航结构。
3. 不移除“客户端 / 后端”现有导航分组。
4. 不改变“全部行情 / 目标行情”的既定拆分。
5. 不改变 14 字段业务顺序和数据含义。
6. 不更换 ECharts、Element Plus 或新增第二套 UI framework。
7. 不因为现代化增加大面积卡片、阴影、毛玻璃或渐变。
8. 不让实时行情更新触发高成本动效。
9. 不依赖 Chrome 108 不支持的新 Web API 完成核心交互。
10. 不在同一 PR 同时重写 store、API、图表和整个页面模板。

## 20. 风险与回退

每个 primitive / 页面迁移必须可独立回退：

```text
功能测试失败 → 回退 UI 迁移
Chrome108 失败 → 回退相关 CSS/API
视觉回归异常 → 恢复上一稳定样式
性能恶化 → 关闭/回退新 primitive 或动效
可访问性下降 → 阻止合并
```

业务层和数据层与 UI 抽取分离，确保回退视觉实现时不影响行情与策略数据。

## 21. 建议执行顺序

```text
Baseline + Visual / Interaction Tests
→ Tokens
→ State / Layer / Motion
→ Primitive Components
→ TerminalTable
→ InstrumentList
→ ChartFrame / ChartToolbar
→ MetricGrid
→ Container-aware Workspace
→ Strategy / Data / Stats migration
→ 每项迁移同步 Visual / Interaction Regression
→ Performance / A11y / Chrome108 Gate
```

首批只应做 P0，不做 multi-chart、command palette、完整 bottom panel 等扩展功能。

各小批独立应用 011/012 门禁，不要求先完成 P1 页面迁移或 P2 扩展才能交付首批。与 OPT/ML-UIX 重叠任务合并实施，映射和更细的验收场景见复核文档。

## 22. 官方参考

- TradingView Supercharts：`https://www.tradingview.com/support/solutions/43000746464-getting-started-with-supercharts/`
- TradingView Layouts：`https://www.tradingview.com/support/solutions/43000746975-tradingview-layouts-a-quick-guide/`
- TradingView Watchlists：`https://www.tradingview.com/support/solutions/43000745825-mastering-the-tradingview-watchlists/`
- Chrome Container / Style Queries：`https://developer.chrome.com/docs/css-ui/style-queries`

这些资料用于确认工作区、watchlist、layout 与现代 CSS 能力的边界，不代表需要复制 TradingView 全部产品功能。
