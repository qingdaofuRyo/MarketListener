# MarketListener TradingView UI 实施进度（2026-09-21）

状态：**第一批基础设施已写入 `master`，等待本机/CI 验证；不得视为 DONE。**  
关联计划：`TRADINGVIEW_UI_IMPROVEMENT_PLAN.md`、`../../UI_Feasibility_Review_20260920.md`、根目录 `Plan_R4.md`。

本记录只描述已经实际写入仓库的代码与剩余验证，不替代 `Plan_R4.md` 的活动任务状态。

## 1. 本批实施范围

本批严格按可行性复核中的低风险顺序推进，没有重排 MarketView 工作区，也没有改变行情业务口径。

| 编号 | 优先级 | 状态 | 已实施内容 | 关键约束 |
|---|---|---|---|---|
| TV-IMP-01 | P0 | IMPLEMENTED / VERIFYING | Vite 生产构建显式设置 `build.target = chrome108` 与 `cssTarget = chrome108` | 仅降低构建目标；不声称自动补齐 Web API/CSS 能力 |
| TV-IMP-02 | P0 | IMPLEMENTED / VERIFYING | 为 `color-mix()` 增加 Chrome 108 基础后备，DrawingColorPicker 先给出 `var(--ml-divider)` fallback | 新浏览器继续使用渐进增强；旧浏览器保持可读而非像素一致 |
| TV-IMP-03 | P1 | IMPLEMENTED / VERIFYING | 扩展 Design Tokens：spacing、radius、control height、line-height、motion、focus、layer；新增 `design/system.css` | 首轮数值尽量贴近现状，不借 token 化偷偷改布局 |
| TV-IMP-04 | P1 | IMPLEMENTED / VERIFYING | 统一 `focus-visible`、`prefers-reduced-motion`、`.ml-numeric` | 不用 hover 代替键盘焦点；金融数值使用 tabular numerals |
| TV-IMP-05 | P0 | IMPLEMENTED / VERIFYING | 从 `QuoteValues.vue` 原样抽取 `MetricGrid.vue` | 七组×两行、字段顺序、overlay 位置、ResizeObserver 字号预算保持不变 |
| TV-IMP-06 | P1 | IMPLEMENTED / VERIFYING | 新增 `DataState.vue`，区分 loading / empty / error / stale / unavailable / partial | 尚未大范围接入页面，避免改变既有 DOM 与业务判断 |
| TV-IMP-07 | P1 | IMPLEMENTED / VERIFYING | 新增非拥有型 `ChartFrame.vue`，提供 header/body/footer/status/slots | 不创建 ECharts、不监听 resize、不请求 API、不持有 store；首轮不接管 `KLineChart` 生命周期 |

## 2. 已写入提交

按实施顺序：

- `8b180b26b8fa8e633d1e6d4ec26cfd28a045491f` — `build(web): target Chrome 108 production output`
- `1e250b0dd07ec68c1c5ec17d244fbd203be64f09` — `feat(web): extend terminal design tokens`
- `d5b505d5226aca4fc43571cc58446c1eee5cbe8a` — `feat(web): add terminal structural design system`
- `0768ae4b3927aabd73e7db19c22d4d7d726bd794` — `feat(web): load terminal structural design system`
- `fcf4211ac6164f29b69ba7e85f4d8532921521d2` — `fix(web): add Chrome 108 color picker fallback`
- `4058572ab80d9374e7eb4fcb4aa42d8499e8ec4a` — `feat(web): extract reusable metric grid`
- `9223a880e67bc7a07a78bdb440c8cea2eb96f073` — `refactor(web): render quotes through MetricGrid`
- `4b8637daea1685409797517636cdb58926d5c1c9` — `fix(web): add Chrome 108 structural CSS fallbacks`
- `48316611fad6225519bbe5cc78feaab9c589d33e` — `feat(web): add terminal data state primitive`
- `35283fe926e092ef90b035777c2f1dd7f07149eb` — `feat(web): add non-owning chart frame primitive`

## 3. 本批明确没有做的事情

以下内容故意没有在第一批实施，避免把 UI 现代化变成高风险重构：

1. 没有移动顶部导航、左行情列表、中间 K 线、右侧绘图栏或底部 36px 周期栏。
2. 没有新增永久占空间的 ContextRail / Bottom Workspace。
3. 没有修改 `LIST_ROW_HEIGHT = 33`，也没有把既有虚拟列表替换成全量 DOM 表格。
4. 没有改变 `quoteFieldGroups` 的七组字段、顺序、两行结构或报价语义。
5. 没有让 `ChartFrame` 接管 ECharts instance、`ResizeObserver`、DPR 重建、绘图命中、回放或 API/store。
6. 没有引入第二套 UI 框架、图表库或 TradingView 专有资产。
7. 没有修改 Android。

## 4. 待验证门禁

当前连接环境没有提供可执行的仓库工作树，也没有返回本批提交的 GitHub CI status；因此以下命令**尚未在本批环境运行**，不得把 IMPLEMENTED 写成 DONE。

在可执行工作树中至少运行：

```bash
cd desktop/web
npm ci
npm run lint
npm run typecheck
npm run build
```

并定向运行与本批最相关的 Playwright：

```bash
npx playwright test e2e/market-layout-r4-s4.spec.ts --workers=1
npx playwright test e2e/chart-workbench-r4.spec.ts --workers=1
```

浏览器验收至少包含：

- Chrome 108：启动、全部行情、目标行情、标的详情、颜色选择器；
- 当前主流 Chromium：深色/浅色两套主题；
- 14 字段：七组×两行仍全部可见，长负数、缺失 `--`、期货/股票字段不移位；
- 键盘：导航、按钮、颜色选择器均存在明确 `focus-visible`；
- `prefers-reduced-motion: reduce`：不依赖动画表达状态；
- 控制台：无新 Vue warning、ResizeObserver loop error、ECharts dispose/recreate 异常。

## 5. 回退边界

每项均拆为小提交，可按以下顺序独立回退：

- 若 Chrome 108 构建输出出现第三方依赖不可转译问题：仅回退 `vite.config.ts` 目标提交，再单独处理依赖兼容，不回退 UI primitive。
- 若全局 focus 样式影响 Element Plus 特殊控件：保留 token，缩小 `system.css` focus selector 作用域。
- 若 `MetricGrid` 导致报价布局回归：回退 `QuoteValues -> MetricGrid` 接入提交，保留未使用的 `MetricGrid.vue` 继续修复，不改业务字段定义。
- 若 `DataState` / `ChartFrame` API 后续不满足页面需求：它们当前尚未接入核心页面，可直接调整或删除，不影响行情逻辑。

## 6. 下一批实施顺序

在上述验证通过前，不开始大规模 `MarketView.vue` DOM 拆分。验证通过后按以下顺序继续：

1. **P0：回归基线** — 为 QuoteValues/MetricGrid 固定七组×两行 DOM 与截图验收，补 Chrome 108 真实浏览器记录。
2. **P1：TerminalTable / InstrumentList** — 先提取视觉与交互壳，保留现有 33px 虚拟窗口、滚动锚点、选中 ID 和键盘定位。
3. **P1：ChartFrame 小范围接入** — 先选一个不改变 ECharts owner 的图表区域，仅接 loading/empty/error/stale 状态。
4. **P1：TerminalPanel / CompactToolbar** — 从数据/策略等非高频页面试点，不先重构 MarketView 核心路径。
5. **P2：Container Queries / Overlay 统一** — 只在组件宽度测量已有证据时应用；不得自动重排 14 字段。
6. **P2：可选 TradingView 式增强** — command search、可选 workspace panel 等继续保持研究项，除非业务需求明确。

## 7. 完成定义

只有同时满足下列条件，TV-IMP-01～07 才可从 VERIFYING 转为 DONE：

- lint / typecheck / production build 通过；
- 关联 Playwright 通过；
- Chrome 108 真实浏览器核心路径通过；
- 现有深/浅主题视觉没有非计划变化；
- 14 字段、虚拟列表、KLineChart 生命周期与原业务口径无回归；
- 实际验证证据回写 `Plan_R4.md` 或其正式交付记录。
