# R3 第三轮开发计划（当前唯一活动计划）

最后更新：2026-08-24。本文承接 `Plan_R2.md`，记录当前未提交工作区中自 2026-08-13 以来的全部实现、验证、缺口与后续任务。`Plan_R1.md`、`Plan_R2.md`、`docs/STATUS.md`、`Plan.md` 和 `Plan_full.md` 只保留历史事实，不再新增当前待办。

## 状态与完成定义

- 状态使用 `NEW → ANALYSIS → PLAN_CREATED → CODING → CODE_REVIEW → TESTING → VERIFYING → DONE`；外部条件无法由代码解除时使用 `BLOCKED`。
- `DONE` 必须同时具备实现、定向测试、全量回归、真实数据或页面验证、文档与清理证据。只有代码或测试文件存在时不得标记完成。
- 本轮工作区尚未提交；不得把 `_patch_*.py`、本地数据、缓存、日志、导出文件或凭据纳入提交。
- 数据来源必须物理隔离并保留 `source`、`source_symbol`、获取时间和质量状态。跨来源同标的只在查询层统一，不逐字段拼接成无法追溯的记录。

## 当前基线

- 后端新增或大幅调整：账户分析、公式引擎/运行时、策略绩效、国内外期货批量导入、通达信本地证券导入、市场分类、行情数据版本、持久查询缓存、行情/数据源/统计/策略 API、控制中心与离线 HTML 导出。
- 网页端新增或大幅调整：行情列表与卡片视图、双 K 线看板、全屏详情、画线工具、颜色选择器、迷你 K 线、设置页、数据源页、统计页和策略页。
- 2026-08-24 完整验证：Ruff、共享 Schema、738 项桌面 pytest、Android `lintDebug/testDebugUnitTest/assembleDebug`、Vue 类型检查/生产构建及 19 项 Playwright 全部通过。保留一个测试故意构造重复 ZIP 条目的 Python 警告和 Vite 大 chunk 提示。
- TickDB 与通达信数据审计结论见 `docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md`。

## R3 任务

### R3-T001 — R3 文档收敛与当前事实源

- `type`：治理与文档；`priority`：P0；`state`：DONE；`failure_count`：0。
- `目标`：为当前全部工作区改动建立第三轮唯一活动计划，同步索引、状态、日志、经验和数据审计。
- `实际修改文件`：`Plan_R3.md`、`README.md`、`docs/README.md`、`docs/START_HERE.md`、`docs/STATUS.md`、`docs/CONTEXT.md`、`docs/Log.md`、`docs/Experience.md`、`docs/DATA_SOURCE_CAPABILITY_MATRIX.md`、`docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md`、`docs/R3_CHANGE_INVENTORY_2026-08-24.md`。
- `验证`：Markdown 链接和 Git diff 检查；没有修改业务代码或数据库。

### R3-T002 — 市场分类、证券名称与类型契约

- `type`：领域模型与数据；`priority`：P0；`state`：VERIFYING；`failure_count`：0。
- `已实现`：新增集中式市场分类与配置，覆盖沪深北普通股票、指数、ETF、LOF、公募 REITs、可转债、可交换债、质押式回购、通达信 `880/881` 板块指数、期货月份/连续合约及夜盘资格；解决 `000001` 等跨交易所歧义；补充通达信证券名称和期货品种名称映射。
- `影响文件`：`market_classification.py`、`config/market_classification.json`、`config/tdx_security_names.json`、`config/futures_product_names.json`、`tdx_local.py`、`futures.py`、行情 API 与对应测试。
- `已验证`：市场分类、行情 API、通达信、期货定向测试已包含在本轮 106 项通过结果中。
- `未完成`：扩展 `contracts/bar.schema.json` 的资产类型枚举，使其与运行时的 `LOF/REIT/CONVERTIBLE_BOND/EXCHANGEABLE_BOND/PLEDGED_REPO/REPO` 一致；复核异常文件 `sz501011.day` 的真实类型。

### R3-T003 — 行情查询缓存、数据版本与增量读取

- `type`：性能与存储；`priority`：P0；`state`：VERIFYING；`failure_count`：0。
- `已实现`：新增行情数据版本、基于文件清单的持久查询缓存、游标向前翻页、可见卡片批量尾部 K 线、首次大库后台构建租约、重叠分区去重和 Silver 写入后的增量缓存更新。
- `影响文件`：`market_data_version.py`、`market_query_cache.py`、`storage.py`、`web_api/common.py`、`web_api/market.py`、`control_center.py`、`dashboard.py`、`dataset_catalog.py` 及测试。
- `已验证`：缓存/行情 API 定向测试通过；仍需在实际大库重启、并发导入和长时间运行场景下验证。

### R3-T004 — 网页行情页、详情页与画线交互

- `type`：前端产品；`priority`：P0；`state`：VERIFYING；`failure_count`：0。
- `已实现`：列表/卡片视图、可调整列表列宽与左右看板宽度、行标记、双 K 线看板、全屏详情、按可见区间加载历史 K 线、卡片图延迟加载、ETF 名称随数据版本刷新，以及水平线、垂直线、箱体线和文本框的保存与显示。
- `本轮用户验收项`：
  - 卡片分页默认 10 条，提供 10/20/30/50/100；迷你 K 线高度 300px。
  - 箱体说明文字使用“开盘:/收盘:/最高:/最低:”格式并增大 2px；左右圆点位于边框垂直中心。
  - 主图价格范围为可见最高/最低价预留上下空间，缩短主图与副图间距，以容纳箱体说明。
  - 24 种高差异预置颜色；透明度滑块以当前半透明颜色显示圆点。
  - 悬浮工具栏只随新建/选中图形出现，点击非图形区域后消失；拖动柄六点缩小。
  - 箱体整体拖动按 K 线索引平移，固定内部 K 线数量；只有拖动端点才能改变开始/截止时间。
  - 卡片 K 线读取并显示详情页保存的画线，且为只读展示。
  - 各图形分别记忆线色、线宽、线型、填充色/透明度和文字样式；吸附、跨周期、连续画线使用 `market-drawing-preferences-v1` 永久保存。删除图形不删除该类型的默认样式。
- `影响文件`：`MarketView.vue`、`KLineChart.vue`、`MiniKLine.vue`、`DrawingColorPicker.vue`、`api.ts`、行情 API，以及 `terminal.spec.ts`、`market-drawing-preferences.spec.ts`。
- `已验证`：Vue 类型检查及生产构建、后端画线/行情 API 和完整 19 项 Playwright 通过；浏览器测试覆盖卡片分页/300px 高度、24 色/透明度、样式永久保存、箱体平移、删除后默认样式、卡片画线与 ETF 首屏名称。真实宽屏/高 DPI 的主观拖动手感仍建议人工复核。

### R3-T005 — 账户分析、交易统计与策略公式

- `type`：业务功能；`priority`：P1；`state`：VERIFYING；`failure_count`：0。
- `已实现`：账户 CRUD、FIFO 成本与持仓、CSV 导入导出、回收站；多空策略台账和独立资金绩效；公式目录、白名单解析、时间序列/截面指标、无未来数据约束、公式策略创建/校验/运行、市场类型和本地 F10 市值过滤。
- `影响文件`：`account_analysis.py`、`formula_engine.py`、`formula_runtime.py`、`strategy_performance.py`、`web_api/stats.py`、`web_api/strategy.py`、`StatsView.vue`、`StrategyView.vue` 及测试。
- `已验证`：公式、统计和策略 API 定向测试通过；仍需真实账本数据的人工流程验证和全量安全审查。

### R3-T006 — 期货合约、资金沉淀与批量本地导入

- `type`：数据工程；`priority`：P1；`state`：VERIFYING；`failure_count`：0。
- `已实现`：合约乘数/保证金规则按生效日读取；沉淀资金公式保留中间字段；交割月解析；持仓量加权序列；通达信期货通 `.day/.lc5` 增量导入；`L7/L8/L9` 分别表示次连/主连/原生加权；具体月份合约、商品指数、AKShare 主连补缺和受控国际连续合约支持。
- `影响文件`：`futures.py`、`futures_bulk.py`、期货配置、CLI、PyTDX、数据源/行情 API、README 和测试。
- `已验证`：期货和批量导入定向测试通过；真实覆盖仍以本机通达信期货通已下载文件为边界，不承诺全市场实时更新。

### R3-T007 — 数据源、设置、控制中心与离线快照

- `type`：运维与可观察性；`priority`：P1；`state`：VERIFYING；`failure_count`：0。
- `已实现`：数据源清单/偏好/本地类别展示增强；本地数据库/数据集/字段类型浏览和分钟字段口径说明；设置页路由；控制中心在 DuckDB 写锁期间的诚实降级；目录和统计接口增强；单文件只读离线 HTML 构建脚本；配置 JSON 随 Python 包发布；`exports/` 纳入 Git 忽略；网页发行资源更新。
- `影响文件`：`DataSourcesView.vue`、`SettingsView.vue`、`App.vue`、`router.ts`、`sources.py`、`control_center.py`、`scripts/build_offline_html.py`、`scripts/build_static_site.py` 等。
- `整理结论`：`build_offline_html.py` 直接读取本地数据生成单文件，`build_static_site.py` 从运行中的后端截取受限数据并复用生产 Vue 资源，两者职责不同并保留；未引用的 `MarketView-v2.vue`、`MarketInstrumentPanel.vue` 与 `_patch_*.py` 已移除，`.gitignore` 已阻止临时补丁再次进入候选提交。

### R3-T008 — 通达信本地证券数据修复与正式接入

- `type`：数据工程；`priority`：P0；`state`：CODING；`failure_count`：1。
- `已实现`：本地目录发现、日线/五分钟解析、证券名称读取、来源隔离、增量检查点、日期范围导入和市场分类。
- `阻止正式导入的问题`：当前日线解析统一把中国市场价格整数除以 100。抽样与 TickDB 交叉验证显示 ETF/LOF/REIT 当前价格放大 10 倍，可转债和质押式回购抽样放大 100 倍；通达信日线成交量还会在“份”和“手”之间变化。
- `后续实施`：按资产类型修正价格倍率；将成交量统一为股/份并以成交额校验；补齐每类二进制样本；迁移或重导已受影响分区；处理 28 个已变化文件和 1 个未见文件。
- `验收`：跨来源同日 OHLC/成交额对照、零/负值检查、重复写入检查、API 查询和页面抽样全部通过后才可标记 DONE。

### R3-T009 — TickDB 原始 K 线标准化导入

- `type`：数据工程；`priority`：P1；`state`：PLAN_CREATED；`failure_count`：0。
- `现状`：`data_control/tickdb` 已保存 9,702 个 gzip 文件和检查点；约 50.7 万条唯一 K 线，尚无任何 TickDB Silver 数据。
- `实施计划`：补齐 ETF 前缀；重试两个失败的 K 线任务；新增只读审计/dry-run 和 Raw-to-Silver 导入器；按来源任务与时间去重；隔离零价记录；把 `quote_volume` 映射为成交额；按市场/品种规范化成交量；建立 `.TICKDB` 物理来源 ID；证券名称使用项目/通达信名称表。
- `边界`：当前下载主要是最近约 100 根 K 线，不得描述为完整历史库；TickDB 中文名称缓存乱码，不得写入标准名称。

### R3-T010 — 全量回归、代码审查与工作区收尾

- `type`：质量与交付；`priority`：P0；`state`：VERIFYING；`failure_count`：0。
- `前置条件`：R3-T002 至 R3-T009 达到可验证状态，尤其先解决通达信缩放问题。
- `验证清单`：Ruff；完整桌面 pytest；Vue build；完整 Playwright；适用的 Android lint/JVM/APK；真实本地服务页面巡检；`git diff --check`；凭据/大文件/运行数据审查。
- `清理清单`：临时补丁和未引用候选组件已移除；`web_dist` 保持 Git 忽略；`AGENTS.md` 已更新安全发布约束；不得提交 `data_control`、报告、日志、数据库或导出。
- `2026-08-24 验证结果`：`scripts/verify.ps1` 完整通过（Python 3.11、JDK 21、依赖锁、Ruff、Schema、738 项 pytest、Android lint/JVM/APK）；Vue build 和 19 项 Playwright 通过；三个新增脚本 `--help` 可启动；全仓库 Markdown 本地链接检查在修复历史交付索引后通过。

### R3-T011 — 安全提交与 GitHub 发布

- `type`：版本控制与发布；`priority`：P0；`state`：VERIFYING；`failure_count`：0。
- `目标`：在不上传 API Key、凭据、本地数据库、行情、报告、日志和导出的前提下，把 R3 源码、配置、测试与文档推送到用户指定的 `https://github.com/qingdaofuRyo/MarketListener`。
- `安全证据`：候选文件密钥扫描只命中测试中的显式合成凭据；无候选源码文件超过 5 MiB；`data_control/reports/artifacts/exports/.env/local.properties` 均被忽略；`.gitignore` 增加 DuckDB/SQLite/DB/Parquet/TDX/临时凭据/补丁规则。
- `远端证据`：发布前只读 `git ls-remote` 显示指定仓库 `master` 为本地基线 `1bc76c2`；当前旧 `origin` 指向另一仓库，推送前必须改为用户指定地址并再次核对。
- `完成条件`：检查精确暂存区、提交、推送、读取远端 `master` SHA 并与本地 HEAD 一致；最终提交信息和验证记录写入 `docs/Log.md`。

## 推荐执行顺序

1. R3-T008：修复通达信价格和成交量单位，阻止错误数据继续进入 Silver。
2. R3-T002：同步正式资产类型契约并解决剩余代码分类例外。
3. R3-T009：实现 TickDB dry-run、质量报告和来源隔离导入。
4. R3-T004：完成画线和行情页真实浏览器验收。
5. R3-T003/R3-T005/R3-T006/R3-T007：完成实际数据与长时间运行复核。
6. R3-T010：全量回归、审查和清理。
7. R3-T011：安全提交、推送和远端核验。
