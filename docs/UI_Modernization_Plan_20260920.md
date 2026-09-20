# MarketListener 现代化金融终端 UI / UX 专项开发计划

日期：2026-09-20  
状态：规划稿，尚未实施代码改造  
适用仓库：`qingdaofuRyo/MarketListener`  
当前唯一活动计划：`Plan_R4.md`

> 本文件是桌面 Web UI 专项方案，不替代 `Plan_R4.md` 的任务状态。后续真正进入开发时，应把对应条目登记进当前活动计划。本专项不修改 Android，不改变既定业务口径，不为“现代化”牺牲行情准确性、信息密度或性能。

## 1. 产品定位

MarketListener 的桌面 Web 不应设计成普通企业 Dashboard，而应继续向“研究终端 / 行情工作台”发展。适合参考的不是大卡片 SaaS 首页，而是 TradingView、Bloomberg-lite、专业量化终端、Linear 式高密度工具界面中的共性：

- 稳定的信息层级
- 高信息密度但不过度拥挤
- 表格、K 线、指标、状态之间清晰联动
- 长时间观看时低视觉疲劳
- 数字对齐和状态颜色稳定
- 所有页面复用同一套设计 token 和组件语义

参考 CodePen、CSS-Tricks、web.dev 只用于吸收现代 CSS / interaction patterns，不复制品牌或视觉皮肤。

## 2. 当前代码基线

已确认：

- `desktop/web/src/design/tokens.ts` 已有完整浅色/深色语义 palette，包含背景、surface、文本、accent、上涨/下跌/平盘、图表、tooltip、多空热度等颜色；这是正确方向，应扩展而不是推翻。
- `desktop/web/src/styles.css` 已统一映射 `--ml-*` token 和 Element Plus 主题变量。
- `desktop/web/src/App.vue` 的顶部导航已明确区分“客户端”和“后端”，不建议为现代化重新设计整套导航结构。
- 路由已懒加载各页面，保留此性能策略。
- `MarketView.vue`、`StrategyView.vue`、`DataView.vue` 等页面已经较重，现代化重点应包括组件拆分和职责收敛，而不是继续向单文件堆 UI。
- 当前已有 `components/charts`、`components/futures`、`components/strategy` 等领域组件目录，可以在此基础上建立终端级 primitive，而不引入新的重量 UI 框架。
- 项目已有 ECharts 6、Element Plus、Vue、Pinia，不再引入第二套图表库或全套 UI 框架。

## 3. 总体设计原则

1. **数据第一，装饰第二。** 任何视觉处理都不能遮住价格、涨跌、成交量、持仓、沉淀资金、市值和时间状态。
2. **高密度但有节奏。** 表格可以密，页面结构不能乱；用分隔、对齐和弱层级建立秩序，而不是靠大卡片和大间距。
3. **颜色是语义资源。** 上涨红、下跌绿、平盘灰保持既定口径；accent 蓝只用于选中/操作，不与涨跌语义竞争。
4. **终端优先桌面。** 不为移动端重排桌面核心结构，本专项不修改 Android。
5. **现代化不等于 redesign。** 顶部导航、全部行情/目标行情拆分、左列表 + 右 K 线、14 字段布局等既有决策继续保留。
6. **组件按容器适配。** 优先 Container Queries，减少针对整个 viewport 的脆弱断点。
7. **动效克制。** 金融行情页面不做持续漂浮、强弹性动画、滚动视差或大面积 blur。

## 4. 设计系统扩展

### ML-UIX-01：从 Color Tokens 扩展为完整 Design Tokens

保留现有 `Palette`，新增：

```ts
spacing
radius
borderWidth
elevation
typography
motion
controlHeight
density
zIndex
```

建议语义变量：

```text
--ml-space-1 ... --ml-space-8
--ml-radius-control
--ml-radius-panel
--ml-radius-dialog
--ml-shadow-floating
--ml-shadow-dialog
--ml-font-size-xs/sm/md/lg
--ml-line-height-dense/normal
--ml-control-height-dense/standard
--ml-motion-fast/normal
--ml-focus-ring
--ml-layer-sticky/popover/modal/alert
```

第一轮不大规模改颜色，只把硬编码间距、圆角和阴影逐步收敛。

### ML-UIX-02：三档信息密度

建立组件密度概念，而不是每页自己写 padding：

- `dense`：行情列表、期货列表、日志表格、持仓/标的高频区域。
- `standard`：策略、数据管理、统计分析、仪表盘。
- `comfortable`：设置、引导、少量说明性页面。

同一 `TerminalTable` / `TerminalPanel` 通过 density prop 控制行高、padding、字体，但不改变数据格式。

## 5. 顶部导航与全局框架

### ML-UIX-03：保留现有 Topbar，增强层级

不重排“客户端 / 后端”分组。只优化：

- active route 使用稳定 accent indicator，而不是大面积背景。
- hover 仅轻微 surface 改变。
- 导航项目保持单行，不用卡片化按钮。
- 主题切换、全局状态等低频操作靠右。
- 长窗口宽度不足时优先紧凑 gap，不随意隐藏核心导航。

顶部栏高度继续维持紧凑桌面终端风格。

## 6. 行情列表设计

### ML-UIX-04：TerminalTable 基础组件

为全部行情、目标行情、期货等统一表格视觉与行为：

- sticky header
- 数字右对齐
- 名称/代码左对齐
- `font-variant-numeric: tabular-nums`
- 价格、百分比、成交量等列固定格式
- hover 使用弱 surface
- selected 使用独立 accent background / left indicator
- focus-visible 独立于 selected
- 上涨/下跌色只用于关键数字，不默认整行染色
- 排序列有明确 icon 和三态视觉
- 列宽拖拽、列顺序等未来扩展不得影响表头 sticky

### ML-UIX-05：行情列视觉优先级

建议分三层：

A 层：名称、代码、最新价、涨跌幅。  
B 层：涨跌、量额、持仓、沉淀。  
C 层：总/流通市值、辅助字段。

不是隐藏 C 层，而是在字体权重和颜色对比上弱一级，减少长时间看盘的视觉噪声。

## 7. 左列表 + 右 K 线终端布局

### ML-UIX-06：保持既定结构，增强可伸缩能力

继续使用左侧标的列表、右侧 K 线与详情。优化重点：

- 左侧列表宽度变化后，行内容按容器自适应，不靠页面 viewport 猜测。
- 右侧 K 线始终优先占据剩余空间。
- 右侧高度继续遵循“屏幕高度 - 导航栏”等既有规则。
- 列表滚动与图表滚动/缩放事件互不干扰。
- 列表选中标的后，右侧保留稳定骨架，避免整页闪烁。

### ML-UIX-07：14 字段统一 MetricGrid

固定业务排布：

1. 开 / 收
2. 高 / 低
3. 涨幅 / 振幅
4. 涨跌 / 结
5. 量 / 额
6. 持仓量 / 沉淀资金
7. 总市值 / 流通市值

抽成统一 `MetricGrid` / `MetricPair`，全部行情右侧详情和标的详情页复用。

要求：

- 标签不使用省略号遮挡。
- 长数字可通过数值格式/最小宽度处理，不挤压标签。
- 正负号、单位、小数位统一。
- 当右侧容器变窄时使用 Container Query 调整列数，而不是把文字缩到不可读。

## 8. ChartFrame 图表系统

### ML-UIX-08：所有 ECharts 图表共享 Frame

建立 `ChartFrame`，统一：

- title
- subtitle / 数据范围
- legend
- toolbar
- loading
- empty
- error
- stale data
- tooltip shell
- 更新时间
- 数据源状态

ECharts option 只负责实际图形，不让每个页面重复实现标题栏和异常状态。

### ML-UIX-09：K 线图视觉规范

- 网格线低对比，不抢价格。
- 十字线、axis label 清晰但克制。
- volume 与主价格区视觉层级分开。
- tooltip 避免覆盖正在查看的蜡烛主体，尽量靠近但不遮挡关键点。
- 涨跌色与全站 token 完全一致。
- 周期切换、复权、指标菜单采用同一 segmented / toolbar primitive。
- 图表 resize 使用节流并避免反复销毁实例。

## 9. 多空热度与市值图专项

### ML-UIX-10：多空热度组件化

保持已定义的总/品种/资金三表与三线语义：

- Gauge/扇形：-100 ~ 100 固定尺度。
- 三种热度色来自 `heatTotal / heatBreadth / heatFund` token。
- 极多/多/偏多/中性/偏空/空/极空颜色统一。
- 权重滑块和折线图使用同一套 legend 颜色。
- 不用过度渐变制造“游戏仪表盘”效果。

### ML-UIX-11：市值堆叠图

继续遵循现有业务：

- 最新市值固定排序。
- 小于 1.5% 合并“其他”。
- hover 当前项时弱化其他项。
- tooltip 显示绝对值 + 占比，格式统一。
- legend 与堆叠顺序一致，避免用户上下扫描匹配。

## 10. 数据、策略、统计页面的信息架构

### ML-UIX-12：Panel primitive

建立 `TerminalPanel`，统一 header、body、footer、loading、empty。不要每个页面重复 `.panel` 风格。

Panel 允许：

- title + compact actions
- 可选 description
- badge / status
- dense / standard density
- no-card 模式（只用分隔线，不一定有明显边框）

专业终端中避免“一切都是浮起的卡片”。

### ML-UIX-13：策略页面

策略页面应逐步拆解巨型 view：

- Strategy list / navigation
- Editor
- Validation result
- Parameters
- Backtest / analysis
- Monitor state

代码编辑区、结果区、状态区应使用清晰分隔，不用大量彩色卡片。

危险操作（删除策略、停止观察等）使用统一 danger confirm。

### ML-UIX-14：数据页面

数据导入/状态/覆盖范围重点展示：

- 当前状态
- 时间范围
- 数量
- 缺失/异常
- 操作入口

不要把底层技术字段作为视觉第一层；高级细节允许展开。

## 11. 状态设计

### ML-UIX-15：完整状态矩阵

控件：

- default
- hover
- focus-visible
- active
- selected
- disabled
- loading
- error

行情数据：

- loading
- fresh
- stale
- unavailable
- partial
- error

必须把“暂无数据”“数据未就绪”“请求失败”“数据过期”视觉区分，不能全部显示同一个空白占位。

## 12. Container Queries 与响应式策略

### ML-UIX-16

优先在以下区域使用尺寸型 Container Queries：

- 14 字段 MetricGrid
- ChartFrame toolbar
- Stats 指标组
- Strategy editor + result panel
- Data source card / panel
- 右侧详情栏

目的：组件可以在不同页面复用，而不依赖全局 viewport breakpoint。

Chrome 108 可使用尺寸型 Container Queries。2026 年的新式 container style queries 不作为本项目核心功能依赖。

## 13. Overlay / Dialog / Popover

### ML-UIX-17

Element Plus dialog/dropdown 继续作为稳定基础，统一：

- title spacing
- close hit area
- backdrop
- footer alignment
- destructive action
- focus-visible
- loading state

现代原生 `<dialog>`、Popover、Anchor Positioning 可研究，但不为了技术新颖而替换成熟组件。若未来使用，必须保留当前框架 fallback。

## 14. 动效与视觉疲劳控制

### ML-UIX-18

行情终端动画比 swift-seat 更克制：

- hover：80–100ms
- menu / popover：100–160ms
- dialog：140–200ms
- 数据刷新不做整行闪烁
- 价格变化可短暂使用轻量背景/文字强调，但快速回落到正常状态

禁止：

- K 线区域 blur / glass 背景
- 无限呼吸动画
- 强弹簧导航
- 大面积 box-shadow 动画
- 频繁 layout transition

支持 `prefers-reduced-motion`。

## 15. Dark / Light Theme 深化

### ML-UIX-19

继续沿用 `tokens.ts` 两套 palette：

- Dark 不是纯黑，保留分层 surface。
- Light 不是纯白一片，通过 divider / surfaceElevated 建层级。
- 任何新增颜色必须进入 token，不允许组件直接写任意 RGB。
- 上涨/下跌色在两套主题都要满足可读性。
- 图表 tooltip、axis、grid、selected row 在两主题同步验收。

新增设计 token 时，`applyTokens()` 必须可完整映射。

## 16. CSS / Vue 架构改进

### ML-UIX-20：页面拆分优先级

建议按复杂度与收益：

1. `MarketView.vue`
2. `StrategyView.vue`
3. `DataView.vue`
4. `FuturesView.vue`
5. `StatsView.vue`

拆分目标不是追求文件大小数字，而是让组件拥有稳定职责和测试边界。

建议目录：

```text
components/
  terminal/
    TerminalPanel.vue
    TerminalTable.vue
    MetricGrid.vue
    MetricPair.vue
    DataState.vue
    CompactToolbar.vue
  charts/
    ChartFrame.vue
    ...
  market/
    MarketList.vue
    InstrumentSummary.vue
    QuoteHeader.vue
```

### ML-UIX-21：styles.css 治理

逐步拆为：

```text
styles/
  tokens.css
  base.css
  terminal.css
  table.css
  overlays.css
  motion.css
```

但 `tokens.ts` 仍是主题颜色唯一事实来源。CSS 文件只消费 token，不复制第二份 palette。

## 17. 性能门禁

### ML-UIX-22

重点测：

- 行情列表 500 / 1500 / 5000 标的。
- 左列表快速滚动。
- 快速切换 20 个标的。
- K 线 resize、周期切换、tooltip。
- 三张以上 ECharts 同页。
- 策略页复杂表单和结果面板同时存在。
- Dark/Light 即时切换。

现代化改造不得明显增加：

- 首屏 JS bundle
- 高频 resize 处理
- DOM 数量
- 图表重复初始化
- 长列表重排

只有 trace 证明必要时才引入更复杂虚拟化；不能先为了“架构漂亮”引入额外依赖。

## 18. 可访问性

### ML-UIX-23

- 所有图标按钮有可访问名称。
- 键盘 focus-visible 清楚但不过亮。
- 表格排序状态用 `aria-sort` 或等效语义。
- 红/绿不能是唯一涨跌信息：正负号、文字和数值方向也必须存在。
- 图表核心数据需有文本摘要/标题，不把重要状态只放 Canvas。
- Modal 打开时焦点正确管理。

## 19. 视觉回归与 Playwright

### ML-UIX-24：Desktop UI Regression Gate

固定截图：

1. 全部行情 + 右侧 K 线
2. 目标行情
3. 标的详情
4. 期货
5. 数据
6. 数据源
7. 策略
8. 账户分析
9. 产业链
10. F10
11. 设置
12. 日志

每页至少浅色 / 深色各一组；核心行情页增加 1366×768、1920×1080 两个桌面视口。

检查重点：

- topbar 断行
- 表头错位
- 数字溢出
- 14 字段被遮挡
- chart toolbar 挤压
- dialog 层级
- 主题 token 漏映射
- 红绿语义错误

## 20. 分阶段实施

| 阶段 | 内容 | 风险 |
|---|---|---|
| A | 设计 token 扩展、density、layer、截图基线 | 低 |
| B | TerminalPanel / TerminalTable / MetricGrid / DataState | 中低 |
| C | 全部行情 / 目标行情 / 标的详情统一 | 中 |
| D | ChartFrame + 多空热度 / 市值图统一 | 中 |
| E | Strategy/Data/Futures 大页面拆分 | 中高 |
| F | Container Query 自适应 | 中 |
| G | 新 Web API progressive enhancement 实验 | 低，必须 fallback |

每个阶段应独立提交和验证，不能一次重写全部页面。

## 21. 建议验收标准

- 既有导航、业务路由和行情语义不变。
- 全部行情 / 目标行情左右结构不被 redesign。
- 14 字段在常用桌面宽度完整可见。
- 表格数字使用统一格式和 tabular nums。
- 浅色 / 深色没有硬编码漏色。
- 所有图表共享一致 tooltip / empty / error / loading 风格。
- 红绿只表示金融涨跌语义，不被普通按钮滥用。
- 大页面拆分后现有 API 与业务结果完全一致。
- Playwright 关键页面无明显布局回归。
- Chrome 108 核心页面保持可用；新 API 有 fallback。
- UI 改造无明显滚动、图表、切标性能退化。

## 22. 外部技术参考

仅作为研究来源，不作为运行依赖：

- CSS-Tricks — CSS Container Queries: https://css-tricks.com/css-container-queries/
- web.dev — Container queries: https://web.dev/learn/css/container-queries
- web.dev — Popover and dialog: https://web.dev/learn/css/popover-and-dialog
- web.dev — Interop 2026: https://web.dev/blog/interop-2026
- CodePen：用于研究高密度 table、terminal toolbar、segmented control、chart panel、popover 等交互示例，不直接复制未经审查代码。

## 23. 结论

MarketListener 的下一轮 UI 现代化重点不是“更像网站”，而是“更像稳定、耐看的专业研究终端”。优先级应是：**数据可读性 > 信息结构 > 组件一致性 > 响应适配 > 微交互 > 装饰效果**。任何视觉调整必须服从行情语义、桌面信息密度、现有功能和性能。