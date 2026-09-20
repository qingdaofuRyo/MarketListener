# UI 与 TradingView 专项计划可行性复核

日期：2026-09-20。状态：静态审查和计划修订；没有实施 UI、业务、数据或 Android 改造。

基线：[45a1c41](https://github.com/qingdaofuRyo/MarketListener/commit/45a1c413a52961b50ccdf7cbb92184350c32e496)。审查 [现代化 UI 计划](UI_Modernization_Plan_20260920.md)、新 [TradingView 专项](development-plans/ui-r5/TRADINGVIEW_UI_IMPROVEMENT_PLAN.md)，对照唯一活动计划 [Plan_R4.md](../Plan_R4.md)、[优化计划](Optimization_Plan_20260920.md)、ADR 和当前源码。最新提交只新增专项文档，未改运行代码。

本文 RV 编号是审查意见，`R5-UI-*` 是专项方案编号；执行状态仍统一在 Plan_R4，不另开 R5 活动队列，也不改历史完成结论。

## 1. 总体判断

参考 TradingView 的工具组织、信息密度和状态一致性是可行的。当前项目已有成熟图表与布局，不需要重做工作区。原稿的“稳定位置”原则与部分骨架、MetricGrid、自适应和 EChartsHost 拆分建议之间存在歧义，实施前必须明确以下边界。

低风险起点是同值 token、独立焦点状态、原位错误反馈和测试基线。TerminalTable、ChartFrame、MetricGrid 及生命周期迁移按中高风险处理，不以“只改前端”推断无功能影响。

## 2. 关键风险及可行替代方案

| 意见 | 已确认依据与可能影响 | 修订方式与通过条件 |
|---|---|---|
| ML-RV-01 / P0：工作区骨架 | 当前全部/目标行情是左列表与右侧双看板，详情是左列表、中图表、右绘图栏、底部周期栏；`QuoteValues` 位于图上 overlay。新稿把右侧画成 Details/Metrics/Status，并把周期工具泛化为上方工具区 | 新骨架仅表达职责，不迁移 DOM 区域。保留现有独立路由、右绘图栏、底部 36px 周期栏和 overlay；不因 ChartToolbar 增加外部 header。ContextRail/WorkspacePanel 保持可选研究项，首轮不占图表面积 |
| ML-RV-02 / P0：虚拟列表已经存在 | `MarketView.vue` 已有 `LIST_ROW_HEIGHT=33`、缓冲窗口、上下占位和键盘滚动计算。新稿“以后再评估虚拟化”容易被理解成应先换成全量表格；density 只改 CSS 会使索引与像素高度不一致 | 复用现有窗口渲染，不退化为 5000 行 DOM。首轮 dense 保持 33px。若未来允许切密度，DOM 行高、表头、占位、可见范围、滚动定位共用同一数值，并保留滚动锚点与选中 ID；不在本轮加全局密度切换 |
| ML-RV-03 / P0：14 字段的适配边界 | `quoteFieldGroups.ts` 固定七组，`QuoteValues.vue` 是七组×两行；现有 s4 E2E 明确断言两行。UI 两稿提出窄容器改变列数，会突破该约束 | MetricGrid 首轮只是原样提取现有 QuoteValues/格式逻辑，不改组序、行数或位置。宽度变化先测量标签/数值与工具槽预算；不能用省略、横向滚动或随意改单位遮掩溢出。若可读字号与固定槽位无法同时满足，单列设计取舍，不自动重排 |
| ML-RV-04 / P0：K 线状态和生命周期 | `KLineChart.vue` 已有 ResizeObserver、rAF 合并、清理逻辑、DPR 改变时重建、绘图命中及 overlay 测量；watch 仍有深度重绘范围。再加一层 EChartsHost 可能产生双重 owner、重复监听，或因 key/v-if 重建导致缩放/绘图丢失 | 首轮 ChartFrame 只接收状态和插槽，保留 KLineChart 为实例 owner。EChartsHost 留作有性能证据的独立重构；不删除既有监听而未核对窗口/DPR语义。普通状态/报价/图例更新不重建实例，不挤小主图；DPR 等必要重建单独记录、恢复视图状态 |
| ML-RV-05 / P0：Chrome 108 不是写一句门禁即可支持 | Web 使用 Vite 8.2.1，`vite.config.ts` 没有显式 build.target；现有 KLineChart、MarketView、DrawingColorPicker 有 `color-mix()`。官方资料表明该 CSS 能力从 Chrome 111 支持，部分声明未见同规则中的基础颜色 | 审计锁定依赖和生产 JS/CSS，显式核对 Chrome 108 构建目标并提供基础颜色/`@supports` 增强。仅改变 JS target 不会自动补齐 Web API 或动态 CSS。Playwright `channel: chrome` 也不等于运行 Chrome 108；必须单列真实旧版本产物验证 |
| ML-RV-06 / P0：状态必须对应正确数据 | DataState/ChartFrame 不能仅按 bars 是否为空决定成功或失败；api.ts 的在途缓存、取消和刷新问题已登记 OPT-002/003/012 | 每个看板分别绑定 instrument/period/dataVersion/请求代次。只有同一数据上下文的旧值能标 stale 并保留；切到 B 时不能把 A 的图表挂在 B 名称下。区分初载、同标的刷新、空、缺覆盖、失败和取消；错误重试不清除选择或画线 |
| ML-RV-07 / P1：报价字号已有可读性风险 | `QuoteValues.numericSize()`、`labelSize()` 使用宽度计算但没有字号下限；这能避免溢出，却不能证明长负数在窄窗口中可读。目前属于代码可见风险，尚未测得最低实际字号 | 基线同时记录真实字体像素、槽宽、负号、单位与全部字段；先在现有布局内试验宽度预算和测量开销，不全局换字体。冻结当前格式，任何最小字号或数值缩写调整须单独比较，不能只用“不溢出”判成功 |
| ML-RV-08 / P1：策略拆分须使用最新业务 | 当前 StrategyView 实际挂载 CompositeStrategyManager；最新活动任务是 attention/position/timing 组合策略，COMBO-02 仍为 VERIFYING。历史架构段落和旧信号/回测描述不能作为当前页面模板 | 拆分围绕当前组合编辑、校验、版本和扫描流程；保留精确函数版本、无未来数据和迟到响应防护，不恢复已移除入口，不把观察结果变成交易。旧定义、监控记录与回测资源原样兼容保存 |
| ML-RV-09 / P1：浮层、表格和主题不能只统一数字 | Element Plus 弹层、ECharts tooltip、绘图输入和 overlay 分属不同挂载点；改层级 token、wrapper 或祖先 transform 可能让弹层被裁切或截获图表指针 | 盘点 DOM 挂载、实际层叠和 pointer-events；只让可交互工具接收事件。保留项目菜单与 Element Plus 生命周期；深浅主题同时验证所有 popup/tooltip，取消粗暴全站 z-index 替换 |
| ML-RV-10 / P0：验收依赖顺序 | 新稿把 R5-UI-011 放在 005–010 后，012 依赖“全部”，但 010 是 P1，013–015 又为 P2。这样会让首批 P0 等待扩展功能，也会太晚发现回归 | 001 和 011 从首个实现前启动，每项迁移随附对应断言；012 只验收当前批次范围。先完成 OPT-014 的可重复运行入口，再扩面；无浏览器/设备证据时保留待验，不把截图数或构建成功当作兼容证明 |

源码证据：[`MarketView.vue`](../desktop/web/src/views/MarketView.vue)、[`QuoteValues.vue`](../desktop/web/src/components/charts/QuoteValues.vue)、[`quoteFieldGroups.ts`](../desktop/web/src/domain/quoteFieldGroups.ts)、[`KLineChart.vue`](../desktop/web/src/components/charts/KLineChart.vue)、[`StrategyView.vue`](../desktop/web/src/views/StrategyView.vue)、[`api.ts`](../desktop/web/src/domain/api.ts)、[`vite.config.ts`](../desktop/web/vite.config.ts)、[`playwright.config.ts`](../desktop/web/playwright.config.ts)。上表区分已确认代码事实与待浏览器复现的影响，没有声称发生过尚未复现的线上故障。

## 3. 进一步值得做的改进

1. **运行状态可追溯。** 原位显示行情截至时间、缓存更新时间和来源/覆盖状态；三者分开，不把新请求完成时间当作新行情。刷新失败保留同上下文可读数据并提供重试。不要整体淡化数字导致对比度下降。
2. **保护用户工作状态。** 组件拆分前列清选择、类别、搜索、三态排序、列宽、缩放、周期、绘图草稿、指标和回放的所有者；同上下文视觉更新不得重置这些值。切换标的和路由则遵守已有清理规则，不能跨标的保留旧草稿。偏好持久化与权威行情/画线/策略分开。
3. **减少重复测量。** QuoteValues 已逐个实例观察宽度；提取组件不能再叠加一组观察者。只在容器或字体度量变化时更新宽度预算，报价 tick/hover 不重复 getBoundingClientRect 与整图 render；是否需要优化以 trace 为准。
4. **统一契约测试入口。** 现有 E2E 有通过产物文件名和模块导出顺序获取 ECharts 的做法；拆包或按需导入可能误伤测试。后续改依赖时把断言接到稳定的测试适配层，并保留真实交互/几何断言，避免将“图表模块名称恰好相同”当成功能契约。
5. **测试边界清晰。** `npm test` 当前仅运行 Vite build，不能代替类型检查和交互测试；Playwright 使用 Windows Python 路径和 data_control。OPT-014 应提供可移植入口与独立合成数据目录；真实行情覆盖仍依实际探针记录。当前仓库没有 GitHub Actions workflow，新增 CI 属于后续实现任务。
6. **一致但克制的可访问性。** 键盘方向键不抢搜索/策略编辑/IME 输入；虚拟表格只让可见可操作项参与焦点，排序语义与三态一致。状态变化使用适当语义，不为每个行情 tick 建立 live region。

## 4. 分批落地与原任务合并

| 批次 | 合并任务 | 范围和出口 |
|---|---|---|
| A：基线与入口 | OPT-001/014；R5-UI-001/011 | 固定构建、合成数据、截图、字体、浏览器、DPR；先跑受影响旧用例，区分已有缺陷与新回归；记录 Chrome 108 支持缺口 |
| B：数据与兼容基础 | OPT-002/003/012；R5-UI-002/003/012 的兼容部分 | 先解决旧响应、取消、错误态契约与 CSS 后备，再展示真实状态；兼容修复与缓存修复分提交 |
| C：低风险复用试点 | OPT-013；R5-UI-002/003/004 | 一个已有 Panel 或菜单同值替换，完整状态/键盘/主题验证；不增加工作区横栏和侧栏 |
| D：列表与字段 | OPT-007；R5-UI-005/006/008 | 保留虚拟窗口和 33px；复用七组两行原组件；验证完整列表、三态、焦点、长数值和详情上下文 |
| E：图表与重页面 | OPT-005/007/009/011；R5-UI-007/009/010 | 先 wrapper 插槽，后有证据的生命周期拆分；分别交付图表、策略、数据页面，避免同 PR 重写 store/API/模板 |
| F：可选功能 | R5-UI-013/014/015 | 仅有实际需求时另行细化 ContextRail、底部面板、命令搜索；不阻塞 A–E，不在首轮增加多图布局或实盘入口 |

同一个组件只有一个迁移任务和一组验收记录；UIX、R5-UI 和 OPT 三套编号仅作映射，不重复开发。建议先完成小范围可用试点，再决定其余抽象是否值得推广。

## 5. 验收与回退条件

- **结构**：1366×768、1920×1080、2560×1440 为原布局回归集，另测新稿 1280×720；深浅主题、实际系统缩放/DPR。七组两行、底部周期栏、右绘图栏和图表 plot rect 不漂移；新增图例不压缩主图。
- **列表**：503 条跨页与 5000 条滚动；倒序→顺序→默认；稳定排序；输入焦点；首尾定位；真实行高与占位一致；返详情和返回列表状态正确。
- **图表**：切标的、周期、双看板、绘图、回放、主题、resize/DPR；保留既有主副图量纲、负数/零/null、可见范围和逻辑锚点。按当前代码和最新 s4/COMBO 测试验收，不恢复早期已被替代的副图形态。
- **竞态与状态**：延迟 A/快速 B、同 key 多订阅者取消、刷新失败、覆盖不足、删除指标后旧响应、扫描停止后旧结果；名称、图表、报价和策略上下文始终一致。
- **性能**：同设备、同浏览器、同数据版本及冷热缓存，至少 3 轮，记录 P50/P95、长任务、实例/监听器/请求数量和内存。沿用优化计划的 P95 恶化 >10% 复测规则；新稿 median 指标是补充，不能掩盖 P95 退化。反复复现的显著退化必须修正或回退，不以更好看抵消。
- **兼容与交付**：生产 JS/CSS 在真实 Chrome 108 验证；现代 Chrome 用例不能替代。每项只运行必要的受影响 Web/接口测试，不默认扩大到 Android 或全库回归。

每个迁移提交能独立撤回。回退 UI 时不重置数据目录、浏览器业务记录、策略版本或画线；新存储字段若未来需要引入，另附双向兼容和迁移方案。以上是后续实施标准，本次没有执行这些运行时验收。

## 6. 官方技术核对

- [TradingView Supercharts](https://www.tradingview.com/support/solutions/43000746464-getting-started-with-supercharts/) 用于参考工具组织；本项目现有布局和业务能力仍是实施边界。
- [Chrome Container/Style Queries](https://developer.chrome.com/docs/css-ui/style-queries)：尺寸查询可用于 Chrome 108，样式查询从 Chromium 111 起，不能将二者混用。
- [Chrome color-mix](https://developer.chrome.com/docs/css-ui/css-color-mix)：Chrome 111 起支持；当前 Chrome 108 计划需核查已有声明和后备。
- [Vite build.target/cssTarget](https://vite.dev/config/build-options)：默认目标随主版本固定到相应 Baseline，不保证等于项目要求；需结合锁文件和产物确认，不能只测试 dev server。
- [Popover API](https://developer.chrome.com/blog/introducing-popover-api)：Chromium 114 起支持；Element Plus/常规定位继续作为现有兼容实现。

本次仅完成计划/源码/官方资料的交叉核对；新增验收标准均为待执行，不代表已通过性能、可访问性或兼容性测试。
