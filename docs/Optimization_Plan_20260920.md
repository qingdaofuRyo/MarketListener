# MarketListener 性能、功能代码与现代桌面界面优化开发计划

日期：2026-09-20｜状态：仓库计划稿，功能改动待实施｜范围：桌面 Web 与直接相关的本地查询服务。

目标是在保持现有行情工作台、数据含义和操作习惯的基础上，减少重复请求、图表重建、无界缓存和模块耦合，提升长时间使用的稳定性，并完善现代数据应用的交互细节。

## 1. 审查基线与现有计划

本计划依据 `master` 提交 [da0b12b](https://github.com/qingdaofuRyo/MarketListener/commit/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849)。已核对根目录 `AGENTS.md`、`Plan_R4.md`、架构/ADR/领域文档、相关日志，以及前端和查询链路关键源码。

发布前已合并远端 `6611557` 新增的 [现代化金融终端 UI / UX 专项计划](UI_Modernization_Plan_20260920.md)，该提交未改变上述代码基线。本计划负责性能、正确性与验收约束；专项计划提供进一步的界面设计细节。同一组件复用实现和验收，不在性能 PR 中捆绑布局或密度改版。

继续以 [Plan_R4.md](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/Plan_R4.md) 为唯一活动计划。本文件 ML 编号是建议任务包，已在 R4 追加性能与界面维护任务索引，并关联原任务；不把历史 R1/R2/R3 队列重新激活，也不另建与 R4 竞争的计划入口。

此次完成的是静态代码审查与计划设计，未启动真实数据服务、运行全市场扫描或测量当前性能。仓库中历史数据规模、覆盖率和测试结果不能当作本次实测结论。

| 已核实的现状 | 对新计划的影响 |
|---|---|
| Vue 3、TypeScript、Pinia、Element Plus、ECharts；`router.ts` 已使用路由动态导入 | 保留技术栈和已有懒加载，进一步分析实际 chunk，不重复实施路由懒加载 |
| `MarketView.vue` 约 3975 行，`KLineChart.vue` 约 2771 行，`StrategyView.vue` 约 2499 行 | 按状态生命周期和领域职责渐进拆分；文件长本身不证明运行慢 |
| 行情列表已使用 33px 固定行高的可视窗口，自动完成 API 分页与去重；搜索已有 250ms 延后 | 保留全量可访问和既有虚拟列表，重点检查大量数据下的排序、选择与缓存成本 |
| K 线十字指针、图形更新和 ResizeObserver 已按帧合并，卸载已有清理 | 不重复添加同类节流；检查剩余深度监听和数据更新路径 |
| `KLineChart.vue` 将 bars、指标、画线、选中画线和主题等放在同一 `deep: true` 监听中，回调调用 `render()` 与 `installHandlers()` | 存在把局部变化扩大为整图更新的路径，应通过调用计数和 trace 验证 |
| 多个图表文件全量导入 `echarts`；`main.ts` 全局注册 Element Plus 并加载完整样式 | 有按需注册的候选空间，先看实际构建产物，不能以缺失图表功能换体积 |
| `domain/api.ts` 已有内存缓存、IndexedDB、本地回退及请求去重 | 缺少明确容量上限；失效不使在途请求失效；共享请求使用首个调用者 signal；过期后台刷新未通过此 helper 主动通知已返回结果的调用者 |
| 后端已有按数据版本的索引/缓存、批量 `read_tails`、`periods_many`、后台重建和分区增量更新 | 继续优化现有查询系统，避免新增另一个数据库或每次请求全盘扫描 |
| `_MARKET_LIST_METRICS_CACHE` 键为数据版本与标的 ID，并在超过阈值后整表清空 | 核对多 data-root 场景隔离，评估集中清空造成的冷缓存抖动；不是断言已发生串数据 |
| 期货热度权重变化已按帧只更新总分序列 | 保留这一已实现的优化，验收权重拖动不请求重算 Silver |
| 最新 R4-COMBO-02 为 VERIFYING；记录了 `StrategyView` 的 13 项历史 unused lint 问题 | 独立清理并恢复完整验收，不能靠关闭规则或删历史功能标记完成；此次未重新运行 lint |

## 2. 保持不变的布局、数据与功能契约

### 桌面界面

- 保留全部行情、目标行情、标的详情的路由和导航组织；F10 维持当前网页客户端入口，首页与日志中的任务队列位置沿用最新版本。
- 详情维持左侧列表、中间图表、右侧绘图工具、底部周期栏的结构；主图占用高度、现有分栏尺寸和用户保存的宽度不被 token 化重置。
- 报价保留七组双行、共 14 个字段，字段顺序、分色、空值标识与固定位置一致。窄看板也不能为提速删字段、使用省略号或退回旧版横向滚动布局。
- 保留 36px 单行底部周期栏、当前五种图表类型、绘图与激光工具、指标图例、回放状态和镜像价格轴。
- 保留当前列表三态排序及恢复默认顺序、搜索、列宽调整、键盘导航和详情继承上下文。仍自动获取完整逻辑列表，不能把只加载前几百条冒充完整市场。
- 复用已有浅色/深色/系统主题与语义颜色，保留涨跌、方向、质量、未知和缺失的区别。

### 数据和执行边界

- DuckDB catalog、Silver Parquet、Bronze 原始记录和 Gold 派生结果维持既有职责；IndexedDB 是可丢弃缓存，不是权威数据。
- 缓存优化不能改变标准标的键、来源隔离、数据版本、交易日/自然日语义、价格与成交量单位、复权规则和 `tdx-cn-v2` 质量门槛。
- 缺失、零值、过期、失败、覆盖不全是不同状态；不能用零补历史缺口，不能把缓存旧值标成最新值，不能把无法盘点库存当作零库存。
- 页面查询继续读取本地结果。期货权重拖动不扫描 Silver；长历史热度缺口保留 null 与覆盖信息，不能通过插值制造真实观测。
- 策略仍锁定精确版本、函数依赖和数据窗口；组合策略关注/仓位/择时门控、优先级、幂等游标、停止/恢复和回放未来隔离保持原语义。
- 本轮不改变 Android、签名包、个人数据隔离及真实交易能力边界。普通可逆实现按现有 ADR 推进；改变这些架构边界时另走仓库规定的 ADR 流程。

## 3. 开发任务与优先顺序

### 第一阶段：建立基准和处理一致性风险

| 编号 / 优先级 | 实施内容与主要文件 | 验收条件 |
|---|---|---|
| ML-01 / P0 | 固定生产构建、合成行情/策略夹具、主要页面截图；记录路由加载、网络请求、图表 `setOption` 次数、API 时延及内存；沿用现有 E2E 场景 | 冷/热缓存分开；503 条跨页场景与 1万条压力夹具分开；1000/5000 根 K 线、双看板、绘图、回放均有基准 |
| ML-02 / P0 | `domain/api.ts`：为查询键增加失效代次；在途请求返回时检查所属代次；按读接口定义新鲜度策略；过期数据后台刷新提供可观察更新或明确的强制刷新路径 | 失效前旧请求不能重新填充缓存；刷新成功可更新当前页面；失败保留带过期说明的旧视图；策略保存后的定义读取不能默默拿旧版本 |
| ML-03 / P0 | 同一 helper 的请求去重与取消：单个订阅者取消仅取消自己的等待；共享网络请求仅在无人需要时取消；保留页面 requestId/标的/周期保护 | A、B 共用请求，取消 A 不损坏 B；强制刷新后旧请求不覆盖；切路由、标的、周期和停止扫描都不产生旧响应回写 |

ML-02/03 先用可控 Promise 和延迟响应夹具证明风险，再修改 helper。不能只在组件中忽略旧值，而允许它回填共享缓存。后台刷新是否更新界面要有明确契约，不能只是写回 Map 就声称“实时刷新”。

缓存身份建议包含：本地服务/数据集命名空间、API 表示版本、路径与规范化参数，以及适用的数据版本。当前没有足够版本信息的接口先采用显式失效和严格刷新；不得凭空假定所有接口返回同一种 revision。涉及策略定义、监控状态的读写要单独确定新鲜度策略。

### 第二阶段：优化渲染、缓存和服务查询

| 编号 / 优先级 / 依赖 | 方案与边界 | 验收与回退 |
|---|---|---|
| ML-04 / P1 / ML-02、03 | 给内存与 IndexedDB 缓存增加可配置的内部容量预算、TTL 清理、按最近使用淘汰；缓存只保存需要的数据窗口，回退 localStorage 限制同步大对象写入 | 30 次路由往返、200 次标的切换后缓存有界；清缓存不删自选、画线、策略、个人设置；存储不可用或额度耗尽仍可正常查询 |
| ML-05 / P1 / ML-01 | `KLineChart.vue`：把行情/指标数据、主题、几何、画线和选中态的更新拆开；依赖显式版本或稳定引用；合并同一帧更新；事件注册保持幂等 | 单纯十字移动不重建完整 series；选择一条画线只更新相关图层；图表缩放、镜像轴、指标 pane、回放和精确坐标一致；出现差异可逐通道退回原更新 |
| ML-06 / P1 / ML-01 | 增设统一 ECharts 注册入口，盘点 candlestick/line/bar/custom/heatmap 等实际类型、坐标、graphic、dataZoom、tooltip 与 renderer；对 Element Plus 评估实际按需导入 | 所有路由、懒加载弹窗、期货图表与离线构建正常；构建报告证明实际下载/解析体积下降。只改变 chunk 划分却总解析量不降时不宣称优化完成 |
| ML-07 / P1 / ML-01 | `MarketView.vue` 按列表状态、行情窗口、画线持久化、指标请求与回放拆 composable；`KLineChart` 拆 option 构造和事件生命周期 | 保持 props、事件、路由与 DOM/CSS 契约；每次抽取一个职责；虚拟列表行高、分页完整性、排序恢复与键盘滚动不变 |
| ML-08 / P1 / ML-01 | `web_api/market.py`、`common.py`、`market_query_cache.py`：对实际慢查询做 profiling；复用批量尾部读取与周期索引；确认缓存隔离；有证据再改查询投影、分区筛选、JSON 解码和有界缓存 | 同一固定数据集结果、顺序、缺失原因、来源与版本完全一致；记录磁盘读取、CPU、P50/P95；不引入逐行混源，不绕过标准化，不以全量重建替代增量路径 |
| ML-09 / P1 / ML-01、05 | `FuturesView` 及期货图表复核挂载、切范围、主题、权重和资源回收；复用已实现的总分序列局部更新 | 连续拖权重无需新的 Silver 扫描和后端重算；结构图固定堆叠与“其他”集合、热度覆盖和 null 断点保持；无重复 chart 实例 |
| ML-10 / P2 / ML-01 | `IndustryView` 与 `/api/industry/atlas` 先测量大图谱 SVG、搜索和 F10 悬浮；若热点成立，再优化检索索引、事件委托、重复 DOM 更新和挂载释放 | 完整节点、证据、连线和公司卡片可访问；iframe 内部也采集性能；裁剪节点或 Canvas 重写不作为默认方案 |

ML-08 应先确认后端实际版本和现有锁范围，不直接共享一个可变 DuckDB 连接到所有请求，也不把并发线程无限增大。后台重建要保证在同一完整版本切换，查询期间仍有一致的旧版本或明确“未就绪”状态。

### 第三阶段：功能代码质量与现代界面细节

| 编号 / 优先级 / 依赖 | 实施内容 | 验收条件 |
|---|---|---|
| ML-11 / P1 / ML-01 | 独立清理 `StrategyView` 历史 lint 问题，核实未使用符号与旧指标/回测弹窗关系；逐步拆出策略编辑、校验、版本与扫描状态 | 相关文件完整 lint/typecheck 通过；旧资源可读；受限 Python 三函数、精确版本、删除恢复、取消关注、零/缺失仓位和扫描竞态保持；按原证据补齐 R4-COMBO-02 |
| ML-12 / P1 / ML-02、03 | 查询反馈统一：首次加载、已有数据刷新、过期数据、覆盖不足、无结果、接口失败分别呈现；错误可在当前面板重试并保留筛选、列宽和画线 | 旧数据显示数据时间/状态的现有说明位置；失败不伪装为空列表；请求取消不弹错误；页面和图表不因全局 loading 重建 |
| ML-13 / P2 / ML-01、07 | 复用 `design/tokens.ts` 和既有 CSS 变量，为表格、菜单、标签、图表工具栏和弹窗完善焦点、键盘和状态；整理重复样式 | 两种主题均可读；控件位置、报价槽、主图高度及列表密度保持；焦点返回和指针穿透有效 |
| ML-14 / P1 / 各相关任务 | 完善可重复的验证入口与 CI：明确 build、逻辑测试、E2E、性能各自职责；修正浏览器配置对 Windows 专属路径的硬编码 | `npm test` 当前只是构建，不能用它替代交互测试；CI 用独立合成数据目录；Windows 的原运行方式继续工作；每任务按风险选择测试范围 |

ML-11 与 ML-05 分开交付，避免同一 PR 同时改策略语义和 Canvas 事件链路。暂不以升级全部依赖、迁移框架或替换数据库作为维护计划的起点。

## 4. 现代网站设计方案：在原位置改善体验

设计定位为紧凑、清晰、适合长时间使用的数据工作台。优先提高状态辨识、操作反馈和跨页面一致性，保留已经完成的 TradingView 风格布局与项目主题。

| 界面对象 | 具体设计 | 布局保护与验收 |
|---|---|---|
| 行情列表 | 复用当前 hover/选中背景；键盘焦点用独立 outline；排序箭头与当前方向清楚可见 | 保留 33px 行高、列顺序、列宽和三态排序；不能因新边框让虚拟滚动偏移 |
| 14 字段报价 | 维持七组双行；统一标签/数值的字体度量与垂直对齐；沿用现有长值字号预算 | 不减少字段，不隐藏长负数，不挤压主图；字段刷新时列位置稳定 |
| 图表工具和菜单 | 图标与中文名称对应；当前模式、禁用原因、焦点、Esc 和方向键完整；悬浮层边界可见 | 不挡十字指针、不截断 tooltip、不移动绘图工具栏；对现有 `ChartMenu` 补缺，不重复实现菜单 |
| 加载与刷新 | 首次加载使用当前面板等尺寸占位；后台刷新保留可读图表并显示轻量状态；快速请求不闪烁占位 | 不修改面板高度和滚动位置；占位动画可关闭，不用大量闪烁图表假装真实数据 |
| 错误与空态 | 区分无本地数据、筛选无结果、请求失败、缓存过期和覆盖不足；在已有说明区域提供重试/返回筛选 | 失败不删除当前选择或画线；未知值不显示为 0；信息更新不改变报价格式契约 |
| 主题与排版 | 扩展现有 token 的语义用途，先原值映射；数字对齐可评估 tabular-nums，但先验证既有字体度量 | 不新增第二套主题系统；浅深主题图表轴、斜线、图例和 disabled 均可读；字号变化需独立视觉复核 |
| 非关键动效 | 菜单、提示建议 120～180ms 的 opacity/transform；尊重系统减少动态效果 | 不对行情值持续补间，不开全局 60fps 循环，不给大面积 Canvas/SVG 外层添加模糊 |

创建本地开发用组件试验页，覆盖长中文名称、极大/负数、真实零、缺失、失败、键盘与深浅主题，复用生产组件和合成数据。可借鉴 CodePen 的实时预览方式；生产页面不嵌入第三方 Pen，也不上传真实行情、策略或个人数据作为演示。

本轮不强行把桌面终端改成移动端卡片布局。现有窗口缩放问题在原布局内修复；新增手机 Web 版属于单独范围。

## 5. 性能目标与验证矩阵

### 度量方法

先锁定当前构建、CPU/内存、系统、浏览器、视口、DPR、数据版本和缓存状态。建议视口覆盖 1366×768、1920×1080、2560×1440，并按用户实际系统缩放补测。至少 3 轮，每个热态动作 30 次；原始样本、P50/P95、长任务、网络与查询计数分别保存。

| 项目 | 拟定目标，不是当前成绩 |
|---|---|
| 列表选中、菜单、周期操作的首个反馈 | P95 ≤100ms；数据加载完成时间另计 |
| 暖缓存标的切换至正确图表可交互 | 固定夹具下争取 P95 ≤300ms；有后端 I/O 的冷态单列 |
| 十字指针与拖拽 | 60Hz 下主线程每帧工作争取 ≤16.7ms；单纯 hover 不触发完整 series 重建 |
| 查询热点 | 以同版本、同结果集为前提争取 P95 降低 ≥30%；区分缓存命中、未命中与后台重建 |
| 构建与启动 | 按需导入试点争取相关加载路径的压缩 JS 下降 ≥20%；同时记录解压后解析/执行成本，不能只看 gzip |
| 长时间稳定性 | 30 次路由往返和 200 次切标的后，无持续增加的监听器、图表实例和无限缓存；固定静置/采集方法确认堆与缓存趋于稳定 |
| 回归门槛 | 无关路径 P95 较基线恶化 >10% 必须复测定位；核心容器尺寸建议偏差 ≤1 CSS px，语义断言单独检查 |

本地终端没有公共网站流量数据时，用浏览器 trace 和真实输入流程诊断；不要把 requestAnimationFrame 回调或 API 时延叫作整页 INP。数值门槛需在 ML-01 后结合真实硬件确认；原本已经足够快的路径优先减少复杂度。

### 功能与数据验收

| 测试组 | 关键场景 |
|---|---|
| 列表与导航 | 自动跨页完整加载、重复/空页报不完整、倒序/顺序/默认、相同值稳定排序、键盘焦点、搜索输入焦点、返回详情上下文、列宽持久化 |
| 缓存与竞态 | 同 key 多订阅者、取消一个、强制刷新、失效时旧请求返回、数据版本切换、多 data-root、IDB 拒绝/满额、断网旧值与过期标识 |
| 图表精度 | 五种图表类型、镜像价格轴、不同数量级/负数、双副图量纲、持仓真实零与能力继承、缩放后画线锚点、DPR/尺寸变化 |
| 布局 | 两个看板与详情全部 14 字段可读；长值无省略；底部周期栏无纵向溢出；报价刷新不移动槽位、不缩主图 |
| 回放与绘图 | 选点/暂停/播放/单步/倍速/结束/退出；指标只取揭示前缀；切标的停止；隐藏页面暂停；激光自然收尾和 pointer capture 清理 |
| 策略 | 版本锁、关注门控、同根限制、优先级、游标补消费、停止后迟到响应、删除恢复、零与缺失、无可靠结束时间的原降级规则 |
| 后端数据 | 冷热结果一致；来源、单位、交易日、数据版本与质量不变；重建/增量更新不返回半份数据；真实 Silver 查询与合成夹具证据分开 |
| 期货与图谱 | 权重只更新显示；null 历史不断言为零；固定堆叠不变；产业链搜索、F10 悬浮和证据链接完整 |

优先复用现有 `market-list-r4`、`market-layout-r4-s4`、`market-display-r4-s3`、`market-tv-r4`、`chart-workbench-r4`、`composite-state`、`futures-long-short-heat` 等浏览器测试，以及后端 query-cache、market API、composite/signal 测试。针对新增缓存竞态补真正可失败的逻辑测试；不为纯常量整理堆砌镜像测试。

## 6. 交付、工作流与回退

建议顺序：ML-01 → ML-02/03 → ML-04/05/06 → ML-07/08/09/11 → ML-12/13/14；ML-10 仅在图谱基准证明有热点时排入。一个开发者粗估 5～8 周，含受影响功能回归；完成基线后按收益重新排期。

每个 PR 只承担一个主要优化，提供关联 R4 任务、前后 trace/查询样本、功能回归、截图、未验收环境与回退方式。内部函数重构、缓存、依赖按需导入和视觉细节分别提交，以便定位与回滚。

已有 Windows 本地验证入口：

```powershell
desktop\.venv\Scripts\python -m ruff check desktop\src desktop\tests
desktop\.venv\Scripts\python -m pytest desktop\tests
```

在 `desktop/web` 目录：

```bash
npm run lint
npm run typecheck
npm run build
npm run test:e2e
```

实际每任务先运行受影响文件/场景；大范围查询或契约改动再扩大回归。`scripts/verify.ps1` 是包含 Android 的完整基线，仅在受影响范围或发布门槛要求时执行，不能把本轮 Web 验证说成全部跨端验收。当前 Playwright 配置使用 Windows Python 路径和 Chrome channel，CI 接入前需显式提供可配置解释器、浏览器和独立数据目录。

此次读取的仓库树未见 GitHub Actions 工作流，可增加独立 Web/API 校验工作流：使用锁定依赖、合成数据、不连接真实 Provider；输出构建和测试报告。真实数据性能在固定本机复核。不要只运行 `npm test`，因为当前该命令实际是 `vite build`。

回退规则：渲染通道和按需注册可独立 revert；缓存可丢弃重建；界面 token 保留原值映射。个人文件、策略版本、画线与运行记录不得成为清缓存的删除对象。本轮默认无权威数据迁移；若查询优化需要改变持久结构，先提交兼容、迁移和回滚设计，再单独评审。

按仓库 ADR，由不同于实现者的审查和验收角色核对证据后更新任务状态；本文未执行这些步骤，也未将 VERIFYING 改为 DONE/ACCEPTED。

## 7. 参考与源码依据

| 资料 | 使用方式 |
|---|---|
| [CodePen Features](https://codepen.io/features) | 采用组件实时预览、最小复现和独立状态演示的方法；视觉细节在本地试验后再进入生产组件 |
| [CSS-Tricks：Reduced Motion](https://css-tricks.com/introduction-reduced-motion-media-query/) | 作为后续动效阅读入口；此次仅确认检索记录，正文访问受限。动效技术要求采用下列 MDN 依据 |
| [Vue 性能指南](https://vuejs.org/guide/best-practices/performance.html) | 测量优先、保持 props 稳定、按实际依赖拆分加载；既有虚拟列表继续维护 |
| [ECharts 按需导入](https://echarts.apache.org/handbook/en/basics/import/) | 通过统一注册实际使用的图表、组件和 renderer 缩减依赖，并验收完整能力 |
| [DuckDB 查询分析](https://duckdb.org/docs/current/sql/statements/profiling) | 用 EXPLAIN/EXPLAIN ANALYZE 等证据识别扫描、过滤和执行成本；命令需核对项目安装版本 |
| [web.dev：优化 INP](https://web.dev/articles/optimize-inp) | 区分输入、处理与呈现；优化用户真实操作链，避免只测函数执行 |
| [MDN：focus-visible](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Selectors/:focus-visible) / [减少动态效果](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion) | 键盘焦点和系统动效偏好 |
| [WAI-ARIA：模态弹窗](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/) | 弹窗焦点约束、键盘关闭与返回原触发点 |

代码证据固定在审查提交：

- [行情主页面](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/web/src/views/MarketView.vue)、[K 线组件](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/web/src/components/charts/KLineChart.vue)、[查询 helper](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/web/src/domain/api.ts)。
- [服务端行情接口](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/src/market_monitor/web_api/market.py)、[K 线查询缓存](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/src/market_monitor/market_query_cache.py)、[热度局部更新](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/web/src/components/futures/LongShortHeatHistoryChart.vue)。
- [主题 token](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/desktop/web/src/design/tokens.ts)、[项目约束](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/docs/ADR.md)、[最新实施日志](https://github.com/qingdaofuRyo/MarketListener/blob/da0b12babc4a0caabd85a5983eb2fa8a4ecb1849/docs/Log.md)。

## 8. 补充问题登记（2026-09-20 复核）

实现状态统一维护在根目录 [Plan_R4.md](../Plan_R4.md) 的 R4-OPT-001～014；本文件 ML 编号仅为详细方案映射。代码路径确认、待复现风险和待测量热点分别标记，避免把优化假设当作已发生故障。

| 问题 | 证据与触发条件 | 影响 / 优先级 | 对应任务 |
|---|---|---|---|
| ML-F01 查询失效后旧响应仍可回填 | 代码路径确认：invalidateQuery 删除缓存，但 fetchQuery 完成时直接写 Map/IDB；失效与响应交错时待夹具复现 | 新数据可能被旧结果覆盖 / P0 | ML-02 |
| ML-F02 共用请求与取消所有权不一致 | 代码路径确认：相同 key 共用 Promise，底层 fetch 使用首次调用的 signal；A 发起、B 订阅后 A 取消 | 一个页面取消可能影响其他消费者 / P0 | ML-03 |
| ML-F03 后台刷新对当前视图不可观察 | helper 在返回过期值后只写缓存，无订阅通知；是否由页面其他事件重读需逐调用点核对 | 用户停留当前页面时可能持续看到过期结果 / P1 | ML-02、12 |
| ML-F04 缓存生命周期无明确上限 | 内存 Map 与持久缓存未见统一容量淘汰；多 key/长时间访问待测量 | 内存或磁盘增长、存储额度耗尽 / P1 | ML-04 |
| ML-F05 IndexedDB 成功与事务完成混用 | writePersistent 在 request.onsuccess 就 resolve，未以 transaction.complete/abort 收尾；连接也需核对关闭/升级生命周期 | 缓存写失败的状态可能不准确；不涉及权威数据丢失 / P2 | ML-04 |
| ML-F06 单看板取数错误退化为空数组 | MarketView.loadBoard 对当前非取消错误只清空 bars，未在该分支保存错误原因 | 网络/API 失败可能被误读为没有行情 / P1 | ML-12 |
| ML-F07 深度监听扩大图表更新 | KLineChart 多种状态共用 deep watch 并调用 render/installHandlers；已有 hover rAF 保留 | 高频局部状态可能引起过量计算，实际占比待测 / P1 | ML-05 |
| ML-F08 后端缓存隔离与清空策略 | _daily_list_metrics 键不显式含 data_root，条目多时整表 clear；需验证 revision 是否足以隔离及真实访问分布 | 多实例/重建场景可能出现隔离或冷缓存抖动风险，尚未宣称串数据 / P1 | ML-08 |
| ML-F09 历史 lint 与验证命令语义 | R4 记录 StrategyView 13 项 unused；package.json 的 npm test 实际执行构建；Playwright 固定 Windows 解释器 | 容易把构建当测试通过，影响后续可靠交付 / P1 | ML-11、14 |
| ML-F10 大组件增加变更耦合 | MarketView、KLineChart、StrategyView 集中多个生命周期和领域职责 | 修一个交互可能影响列表、绘图、回放；属于维护风险，非单凭行数判性能差 / P1 | ML-07、11 |

### 对关键任务追加的实施约束

- ML-02：强制刷新与普通请求须有明确代次优先级；失效、网络完成、持久缓存读取和删除之间都要有竞态用例。无需等待缓存写盘完成才能显示有效查询，但写入代次必须受控。
- ML-04：以事务 complete/abort/error 判定持久写入结果，连接关闭或版本变化后可重新打开；失败时回退可用但有界。区分查询缓存与用户画线/策略/偏好，不以清整个 origin 存储作为淘汰方案。
- ML-12：为每个看板分别保留 loading/error/dataVersion 状态；在原有面板内展示可重试错误。旧标的响应不能改变新标的错误状态，取消不转成失败，无本地数据才进入空态。
- ML-11/14：保留既有 VERIFYING 原因和历史证据。完成定义须说明实际执行的是 lint、typecheck、build、逻辑测试还是 E2E，不通过重命名脚本或调整成功提示制造验收通过。
- 文档维护：README 的日期覆盖率和样本数量是历史快照；新性能报告记录本次数据版本、实际数量和夹具性质。文件说明与代码冲突时先核对现行 Plan_R4/ADR，不把旧段落自动当新开发任务。

建议先投入的三个修复包为“缓存失效/取消”“图表更新粒度”“看板错误与数据新鲜度”。按需导入和大组件拆分安排在基线之后；真实数据迁移、Android 与新交易能力保持原有独立任务边界。
