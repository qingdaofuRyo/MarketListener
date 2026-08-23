# R3 当前工作区变更清单

盘点日期：2026-08-24。本清单依据 `git status --short`、`git diff --stat`、新增模块和测试审计生成，用于回答“当前有哪些改动”。功能状态与下一步以根目录 `Plan_R3.md` 为准。

## 仓库与构建配置

| 文件 | 状态 | 目的 |
| --- | --- | --- |
| `.gitignore` | 修改 | 忽略导出物、数据库、Parquet、通达信行情文件、临时凭据和补丁文件。 |
| `README.md` | 修改 | R3 入口、期货增量导入和离线 HTML 使用说明。 |
| `desktop/pyproject.toml` | 修改 | 把 `market_monitor/config/*.json` 作为包数据发布。 |
| `AGENTS.md` | 新增/更新 | R3 入口、测试规则、数据安全和 GitHub 发布检查。 |

## 后端与数据模块

| 文件/目录 | 状态 | 主要改动 |
| --- | --- | --- |
| `desktop/src/market_monitor/account_analysis.py` | 新增 | 账户、交易、FIFO 成本、持仓、CSV 和回收站。 |
| `aggregation.py` | 修改 | 季线/年线等派生周期与聚合边界。 |
| `cli.py` | 修改 | `kline-cache`、`bulk-futures`、`import-tdx-local` 命令。 |
| `control_center.py` | 修改 | 优先读取查询清单；DuckDB 写锁期间返回可解释状态。 |
| `dashboard.py` | 修改 | catalog 写锁期间健康报告降级。 |
| `dataset_catalog.py` | 修改 | 期货次连等数据集登记。 |
| `formula_engine.py` | 新增 | 安全公式解析、时间序列/截面指标与边界。 |
| `formula_runtime.py` | 新增 | 公式策略编译、运行和条件计算。 |
| `futures.py` | 修改 | 合约规格、保证金/沉淀资金、交割月、加权序列与标准化。 |
| `futures_bulk.py` | 新增 | 通达信期货通、AKShare 和国际期货批量增量导入。 |
| `market_classification.py` | 新增 | 统一市场/证券类型/代码区间分类。 |
| `market_data_version.py` | 新增 | 本地行情数据版本。 |
| `market_query_cache.py` | 新增 | 持久清单、游标窗口、批量尾部数据和后台构建租约。 |
| `providers/pytdx.py` | 修改 | 跳过单条非法日历记录，保留其余有效 K 线。 |
| `storage.py` | 修改 | Silver 写入与查询缓存/数据版本衔接。 |
| `strategy_performance.py` | 新增 | 多空策略台账和独立资金绩效。 |
| `tdx_local.py` | 新增 | 通达信沪深北/港股本地日线、五分钟线和增量检查点。 |
| `web_app.py` | 修改 | 注册设置页 Web 路由。 |

## 配置数据

| 文件 | 状态 | 目的 |
| --- | --- | --- |
| `config/futures_contract_specs.json` | 新增 | 合约乘数和保证金等生效日规格。 |
| `config/futures_product_names.json` | 新增 | 期货品种中文名称。 |
| `config/market_classification.json` | 新增 | 市场分类、代码区间和夜盘规则。 |
| `config/tdx_commodity_index_names.json` | 新增 | 通达信商品指数名称映射。 |
| `config/tdx_security_names.json` | 新增 | 通达信证券名称映射。 |

## Web API

| 文件 | 状态 | 主要改动 |
| --- | --- | --- |
| `web_api/common.py` | 修改 | 数据版本、缓存窗口和统一读取辅助。 |
| `web_api/market.py` | 修改 | 分类、分页、游标 K 线、卡片批量、画线和数据版本。 |
| `web_api/sources.py` | 修改 | 本地数据库/数据集/字段元数据、Provider 与路由配置。 |
| `web_api/stats.py` | 修改 | 账户分析、交易与策略绩效 API。 |
| `web_api/strategy.py` | 修改 | 策略 CRUD、公式校验/运行、匹配和市场/F10 过滤。 |

## 网页端

| 文件 | 状态 | 主要改动 |
| --- | --- | --- |
| `desktop/web/src/App.vue` | 修改 | 设置入口、账户分析命名和行情页布局。 |
| `components/charts/DrawingColorPicker.vue` | 新增 | 24 色预置、自定义颜色和透明度滑块。 |
| `components/charts/KLineChart.vue` | 修改 | K 线布局、画线渲染/创建/选择/拖动及只读模式。 |
| `components/charts/MiniKLine.vue` | 新增 | 卡片 300px 延迟加载 K 线和只读画线。 |
| `domain/api.ts` | 修改 | API 类型、显示映射和数据版本请求。 |
| `router.ts` | 修改 | 设置页路由。 |
| `styles.css` | 修改 | 页面与组件样式。 |
| `views/DataSourcesView.vue` | 修改 | 本地表、数据集、字段口径和 Provider 浏览。 |
| `views/MarketView.vue` | 修改 | 行情分类、列表/卡片/双看板/详情和画线偏好。 |
| `views/SettingsView.vue` | 新增 | 网页设置页。 |
| `views/StatsView.vue` | 修改 | 账户、交易与绩效界面。 |
| `views/StrategyView.vue` | 修改 | 策略与公式界面。 |

## 自动化测试

### Python 新增测试

- `test_formula_engine.py`
- `test_futures_bulk.py`
- `test_market_classification.py`
- `test_market_query_cache.py`
- `test_tdx_local.py`
- `test_tickdb_download.py`

### Python 修改测试

- `test_aggregation.py`
- `test_control_center.py`
- `test_dashboard.py`
- `test_dataset_catalog.py`
- `test_futures.py`
- `test_pytdx_provider.py`
- `test_web_market_api.py`
- `test_web_stats_api.py`
- `test_web_strategy_api.py`

### 浏览器测试

- `desktop/web/e2e/market-drawing-preferences.spec.ts`：新增 ETF 首屏名称、卡片分页/高度、颜色/透明度、画线偏好、箱体拖动、删除后样式记忆和卡片画线验证。
- `desktop/web/e2e/terminal.spec.ts`：扩展行情详情、箱体画线、策略和账户分析路径。
- `desktop/web/e2e/industry-hover.spec.ts`：调整产业链页面回归。

## 脚本

| 文件 | 状态 | 目的 |
| --- | --- | --- |
| `scripts/build_offline_html.py` | 新增 | 生成无后端、无网络的单文件只读快照。 |
| `scripts/build_static_site.py` | 新增 | 从运行中的本机后端截取受限数据，复用生产 Vue 资源生成只读静态网站文件夹。 |
| `scripts/tickdb_download.py` | 新增 | TickDB 目录、参考数据和 K 线的可恢复原始下载。 |

## 文档

- `Plan_R3.md`：第三轮唯一活动计划。
- `docs/ARCHITECTURE.md`：桌面/Web/Android、存储、来源隔离、数据流和安全边界。
- `docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md`：TickDB/通达信字段、覆盖、质量和接入门槛。
- `docs/R3_CHANGE_INVENTORY_2026-08-24.md`：本文件，逐文件变更清单。
- `README.md`、`docs/README.md`、`docs/START_HERE.md`、`docs/STATUS.md`：入口和历史状态切换。
- `docs/CONTEXT.md`：市场分类和开发轮次统一术语。
- `docs/DATA_SOURCE_CAPABILITY_MATRIX.md`：期货、通达信和 TickDB 能力状态。
- `docs/Experience.md`：行情单位、来源隔离、画线、缓存和文档治理经验。
- `docs/Log.md`：R3 实现与验证时间线。

## 清理结果与非源码边界

- `_patch_kline*.py`、`_patch_mv*.py`、未引用的 `MarketView-v2.vue` 和 `MarketInstrumentPanel.vue` 已在 2026-08-24 清理，不进入正式提交。
- `.gitignore` 新增 `_patch_*.py`、通用数据库/Parquet/通达信文件和本地凭据规则，防止同类文件再次进入候选提交。
- `AGENTS.md` 已按用户最新指令纳入仓库，补充 R3 入口、数据安全和 GitHub 发布检查；它是协作规则，不是运行时功能。
- `data_control/`、`reports/`、`artifacts/`、`exports/`、日志、缓存和下载数据均为本地运行产物，不纳入源码提交。

## 当前验证

- 2026-08-24：本轮相关 106 项 Python 定向测试通过。
- 2026-08-24：`npm run build` 通过，存在 Vite 大 chunk 警告。
- 2026-08-24：`scripts/verify.ps1` 完整通过，包括 Ruff、共享 Schema、738 项桌面 pytest、Android lint/JVM/APK。
- 2026-08-24：完整 19 项 Playwright 通过；三个新增脚本的 `--help` 均可启动。
- `git diff --check` 通过；全仓库 Markdown 本地链接检查通过。
- Vite 保留两个大 chunk 提示；签名篡改测试保留一个故意重复 `manifest.json` 的既有警告，不影响通过结论。

## 发布记录

- 目标仓库：`https://github.com/qingdaofuRyo/MarketListener`，分支：`master`。
- R3 主体提交：`ce91a83328e28fd8398704b5cf76f624ee5abd62`。
- 首次推送后，`git ls-remote origin refs/heads/master` 返回值与本地 HEAD 一致。
- 发布只包含源码、测试、公共配置和文档；本地数据库、行情原始文件、报告、日志、导出和凭据未进入提交。
