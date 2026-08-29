# MarketListener 本地投资研究终端重构任务队列

> 建立日期：2026-08-09；2026-08-24 归档。此队列的实现结果已并入当前代码和 R3 文档，后续不得从本文领取任务；当前唯一活动计划为根目录 `Plan_R4.md`。

## 约束与完成规则

- 正式一级导航固定为：`/`、`/data/`、`/f10/`、`/industry/`、`/logs/`。`/industry-v2/` 仅可重定向至 `/industry/`。
- 保留研报、`chain_index.json`、`industry_graph`、Evidence、原始 F10 与有效 F10 数据；不新建第三套产业链数据库。
- Web 和 CLI 复用既有 Python service 函数；Web mutation 只接受预定义 operation enum，禁止 shell 字符串、任意 Python 或 SQL。
- 每个写入操作默认串行；同类 operation 同时只允许一个运行；非 loopback 的 mutation POST 必须拒绝。
- 公司资料以 Canonical Instrument 为键，统一 `CompanySummary` / `CompanyDetail`；金额使用 `MoneySnapshot(value, currency, asOf, source)`，收入使用 `RevenueSegment`。
- 任务状态只按 `NEW → ANALYSIS → PLAN_CREATED → CODING → CODE_REVIEW → TESTING → DEPLOY → DONE` 前进。未满足验收标准不得标记 `DONE`。

## 阶段任务卡

### T-001 — 当前架构与迁移影响审计

- task_id：T-001
- title：当前架构与迁移影响审计

- priority：P0
- 现状：已完成标准库 HTTP 控制中心、F10、产业链、打包、Android 与测试的证据审计。
- 目标：建立可验证的改造基线，明确旧 Viewer 依赖，避免依据历史文档或猜测删除数据。
- 影响范围：`control_center.py`、`cli.py`、`f10.py`、`industry_atlas.py`、`industry_graph/`、`report_pipeline.py`、`package_builder.py`、`storage.py`、Android graph/WebView、测试与脚本。
- 验收标准：记录当前架构、旧 `industry-map.html` 依赖、F10 数据路径、Android package 路径和 Vue/FastAPI 迁移影响；执行 `desktop\.venv\Scripts\python -m pytest desktop\tests` 作为基线。
- state：DONE
- failure_count：0
- 证据：2026-08-09，pytest `525 passed, 1 warning`；审计结论位于本文件“审计结论”。

### T-002 — 统一公司领域模型与本地 F10 API

- task_id：T-002
- title：统一 CompanySummary / CompanyDetail / MoneySnapshot / RevenueSegment 与本地 F10 API

- priority：P0
- 现状：F10 原始 JSONL、Atlas 导出 JSONL、DuckDB `f10_company` 三种表示并存；现有金额仅为裸数值，未绑定币种、日期与来源。
- 目标：定义并实现唯一的 `CompanySummary`、`CompanyDetail`、`MoneySnapshot`、`RevenueSegment`，并提供受控的本地 F10 列表/详情 API。
- 影响范围：`f10.py`、`storage.py`、`industry_graph/`、contracts、后端 API、单元测试。
- 验收标准：A/H 代码统一映射至 Canonical Instrument；`GET /api/f10/companies` 支持白名单字段筛选、排序、分页；`GET /api/f10/companies/{instrument_key}` 按需返回详情；缺失值无伪造/无 `null` 字符串；金额日期和来源不分离。
- state：DONE
- failure_count：0
- 当前证据：新增 `industry_graph/f10` 的共享 DTO/只读 repository 与 `/api/f10/companies`、`/api/f10/companies/{instrument_key}`；针对 A/H 键冲突、金额来源、收入构成、分页/白名单排序和 HTTP 端点的测试已通过。Atlas serializer 已只保存 `companyRefs` 并使用同一 `CompanySummary` 索引，满足共享数据模型验收。

### T-003 — 产业链 CompanyPopover、Drawer 与 Hover E2E

- task_id：T-003
- title：产业链 CompanyPopover、Drawer 与 Hover E2E

- priority：P0
- 现状：旧 Atlas tooltip 曾内嵌在缩放画布中；现已替换为画布外固定定位 overlay，并由统一 CompanySummary 索引驱动。
- 目标：任何含 `instrument_key` 的产业链公司名称和代码都复用 T-002 公司摘要；PC hover、点击 Drawer 与触屏 tap 使用同一模型。
- 影响范围：Vue industry UI、产业链 API serializer、F10 API、E2E 测试。
- 验收标准：150–250ms hover；弹窗可停留、边缘 flip、不受 canvas transform/overflow 裁切；50/100/150% 缩放正常；A/H 可用；无 `undefined/null/NaN/0亿元/Invalid Date`；hover 不访问第三方站点；字段与 F10 列表/详情一致。
- state：DONE
- failure_count：0
- 当前证据：Atlas 节点仅保存 `companyRefs` + `companyEvidence`，公司资料集中在 `companySummaries`；固定定位的 body-level overlay 在画布缩放时关闭、重新 Hover 后以正常字体重开，Drawer 和触屏 click 共用同一模型。`desktop/web/e2e/industry-hover.spec.ts` 已在真实 FastAPI `/industry/` 路由通过 5 项 Playwright：A 股名称/代码 Hover 与停留、边缘 flip、PC Drawer、顶部 F10 跳转、触屏 tap、港股及 50/100/150% 缩放；同时检查无 `undefined`/`Invalid Date`。模板和 Python 回归 15 项均通过。

### T-004 — Vue 3 五页面 Shell 与统一导航

- task_id：T-004
- title：Vue 3 五页面 Shell 与统一导航

- priority：P1
- 现状：桌面端为 `_INDEX_HTML` 单页；`/industry/` 仍返回旧 SVG，`/industry-v2/` 返回 Atlas。
- 目标：引入本地打包的 Vue 3 + TypeScript + Vite + Vue Router + Pinia + Element Plus + ECharts，建立五个固定路由与导航壳。
- 影响范围：前端工程、FastAPI 静态资源与路由、控制中心替换策略、路由测试。
- 验收标准：五路由均可直达；无 CDN；`/industry-v2/` 只有重定向；数据页无 mutation 控件；现有 CLI service 不重写。
- state：DONE
- failure_count：0

- 当前证据：`desktop/web` 已定版引入 Vue 3、TypeScript、Vite、Vue Router、Pinia、Element Plus 和本地 ECharts；构建产物由 `web_app.py` 的 FastAPI 托管，CLI `serve` 已切换至该应用。五个固定路由（含 F10 详情）与 `/industry-v2/` 307 redirect 已有 API 回归测试；真实 FastAPI 服务器上五路由均返回 Vue Shell、`/industry-v2/` 返回 307。`npm run build`、ruff 与 531 项 desktop pytest 通过。

### T-005 — 首页 OperationManager 与写入安全

- task_id：T-005
- title：首页 OperationManager 与写入安全
- priority：P1
- 现状：HTTP handler 只读；`ops.py` 的 nightly job 状态不等同于交互 operation 队列。
- 目标：在首页集中发起预定义 operation，持久化 QUEUED/RUNNING/PASS/PARTIAL_FAILURE/FAILED/CANCELLED 状态，并直接调用既有 service。
- 影响范围：FastAPI、operation domain/service、首页、日志、API 与安全测试。
- 验收标准：写任务串行、同类去重、可查询和取消；拒绝非 loopback POST；拒绝任意 shell/Python/SQL；复用 `run_fetch_session`、F10、研报、atlas、package service。
- state：DONE
- failure_count：0

- 当前证据：新增持久化 FIFO `OperationManager`，运行中同类操作去重、仅取消排队任务、进程中断会记录显式失败；FastAPI 仅接受 `OperationKind` 枚举，并直接绑定既有 Python service，未使用 shell/CLI 字符串。所有 `/api/*` mutation 都在 middleware 拒绝非 `127.0.0.1/::1` 客户端；测试已覆盖 remote 403、loopback 202、枚举外 Python 字符串与额外 SQL 字段 422。首页已只显示白名单操作与任务队列。

### T-006 — 只读 Grafana 风格数据监查页

- task_id：T-006
- title：只读 Grafana 风格数据监查页

- priority：P1
- 现状：`/api/health` 仅提供控制中心概要，未提供统一受控数据浏览器。
- 目标：以类别展示 Market、Silver、Gold、F10、Industry、Runs、Partitions、Quarantine、Package、Storage、Quality、Freshness。
- 影响范围：数据查询 service、FastAPI、Vue data page、测试。
- 验收标准：服务端允许列表查询，支持筛选/排序/分页；preview 最大 500 行，图表点数和查询超时受限；无任意 SQL 接口，页面无 mutation。
- state：DONE
- failure_count：0

- 当前证据：`/api/data/{view}` 仅允许 Market、Silver、Gold、F10、Industry、Runs、Partitions、Quarantine、Package、Storage、Quality、Freshness 12 个固定 view；服务端限定 500 行、分页、受控排序和文本筛选。Gold 仅用固定 DuckDB `SELECT`，没有任何客户端 SQL 入口；数据页只包含读取和 ECharts 可视化控件。API 回归验证 F10 预览、未知 view 404 与 POST 拒绝。

### T-007 — 完整 F10 企业资料库

- task_id：T-007
- title：完整 F10 企业资料库

- priority：P1
- 现状：F10 仅作为 Atlas 的内嵌紧凑记录；没有独立列表、详情或产业链定位页面。
- 目标：交付统一的企业搜索、筛选、列表、详情和产业链定位。
- 影响范围：F10 API、Vue F10 views/components、共享公司模型、测试。
- 验收标准：A/H 及可扩展市场可检索；展示要求的资料字段与更新时间；详情展示全部 RevenueSegment；从产业链点击可进入对应企业并定位链路。
- state：DONE
- failure_count：0

- 当前证据：Vue F10 已支持 A/H 公司名称/代码/instrument_key 搜索、市场筛选、列表、详情和结构化收入构成；详情使用同一 `CompanyDetail`，并从现有 Atlas `companyRefs` 反解 Chain/Stage/Node 定位。API 回归验证该定位，不新增公司或产业链数据库。

### T-008 — 移除旧 SVG Viewer 与旧 Android 打包路径

- task_id：T-008
- title：移除旧 SVG Viewer 与旧 Android 打包路径
- priority：P0
- 现状：旧 `industry-map.html` 仍由报告流水线生成、HTTP `/industry/` 提供、首页链接、Android 包 extra file、导入白名单与 `GraphScreen` 入口依赖。
- 目标：删除旧 Viewer 代码与产物路径，不删除其输入研报/知识数据；新版 Atlas 成为唯一正式入口和唯一 Android HTML。
- 影响范围：`report_pipeline.py`、`control_center.py`、`package_builder.py`、Android importer/MainActivity/GraphScreen、文档、测试。
- 验收标准：仓库不再生成、路由、打包或显示 `industry-map.html`；`/industry/` 是新版；`/industry-v2/` 重定向；Android zip 仅含 `industry/industry-atlas.html`；底层研报、`chain_index.json`、Evidence、`industry_graph`、F10 完整保留。
- state：DONE
- failure_count：1

- 当前证据：`build_chain_index()` 已不再生成 SVG，标准库 control center 的 `/industry/` 改为 Atlas、`/industry-v2/` 为 307；Android GraphTab 移除 SVG 分支和旧文件复制；Android package builder 只打入 `industry/industry-atlas.html`，zip 回归已通过。旧模板源码与 `reports/industry/industry-map.html`、`data_control/industry/industry-map.html` 均已删除；全仓源码搜索不存在旧 Viewer 引用，且未删除任何底层数据。

### T-009 — 新产业链全景 Viewer 深化

- task_id：T-009
- title：新产业链全景 Viewer 深化

- priority：P1
- 现状：Atlas 是单文件原生 HTML，已有卡片/搜索/tooltip，但信息模型与 Vue overlay 不统一。
- 目标：以 `Chain → Stage → Node → Product/Material/Service → Company` 实现券商研报式全景布局。
- 影响范围：industry serializer、Vue industry components、样式、E2E。
- 验收标准：不使用默认力导向/蜘蛛网；仅环节箭头、无大量公司连线；同一公司可多链多节点；证据可追溯；缩放和平移可靠。
- state：DONE
- failure_count：0

- 当前证据：Atlas 使用 Chain→Stage→Card(Node)→Product/Material/Service→Company 的券商研报式分区布局，环节箭头而非公司蜘蛛网；节点保存 `companyRefs`，集中 `companySummaries` 索引与 Evidence。真实 Playwright 覆盖名称/代码 Hover、Drawer、边缘 flip、A/H、触屏 tap 和 50/100/150% 缩放。

### T-010 — 统一结构化日志页

- task_id：T-010
- title：统一结构化日志页

- priority：P1
- 现状：日志分散在运行输出和少量目录，未形成统一的事件流和页面。
- 目标：写入 `data_control/logs/events-YYYY-MM-DD.jsonl`，并只读展示事件。
- 影响范围：logging service、operations/providers/pipelines、logs API/UI、测试。
- 验收标准：行情、F10、研报、产业链、Android package、Operation、Provider、质量与异常均有结构化事件；日志不是第二业务库；可按安全白名单过滤分页。
- state：DONE
- failure_count：0

- 当前证据：新增 `EventLog` 追加式写入 `data_control/logs/events-YYYY-MM-DD.jsonl`，并提供受限 `/api/logs`（类别/状态、分页、最大 500 行）与 Vue 日志页。Operation 的 QUEUED/RUNNING/PASS/FAILED/CANCELLED 生命周期已写入该流，并覆盖 Market、F10、Report、Industry、Android、Provider、Quality、Exception 领域分类；日志测试验证 JSONL 与只读 API，不创建第二套业务库。

### T-011 — Android 离线兼容与全量回归

- task_id：T-011
- title：Android 离线兼容与全量回归

- priority：P0
- 现状：Android GraphTab 仍同时暴露 Atlas、SVG map 和 GraphSnapshot 搜索；WebView 已禁网络但允许旧文件路径。
- 目标：Android 仅加载新版离线 Atlas，触屏使用 Quick Card/Bottom Sheet，并完成最终回归。
- 影响范围：Android package importer、MainActivity、GraphScreen、测试和验证脚本。
- 验收标准：离线 WebView 无 CDN/网络；旧 SVG 不存在；tap 公司展示与 PC 同数据模型；`desktop\.venv\Scripts\python -m pytest desktop\tests`、`powershell -ExecutionPolicy Bypass -File scripts\verify.ps1` 全部通过，并新增五路由、Operation、安全、Data API、F10 API、日志、旧 map 移除、Android Atlas-only 与 Hover E2E 覆盖。
- state：DONE
- failure_count：0

- 当前证据：Android GraphTab 只加载离线 Atlas，WebView 保持 `blockNetworkLoads=true`；package zip 测试确认仅包含 Atlas。`powershell -ExecutionPolicy Bypass -File scripts\verify.ps1` 已完整通过，覆盖锁定依赖、ruff、538 项 desktop pytest、Android lintDebug、testDebugUnitTest 和 assembleDebug。

## T-001 审计结论

### 当前架构

1. `desktop/src/market_monitor/control_center.py` 是 Python 标准库 `BaseHTTPRequestHandler + ThreadingHTTPServer`。首页由 `_INDEX_HTML` 字符串生成；`do_POST` 统一返回 405。当前 GET 仅有 health/dataset/sync plan/package/industry 端点。
2. `desktop/src/market_monitor/cli.py` 是唯一 CLI 入口，已直接调用 `run_fetch_session()`、`run_f10_fetch()`、`run_revenue_fetch()`、`process_report_batch()`、`verify_report_batch()`、`build_chain_index()`、`build_atlas()`、`build_android_package()`。新 Web 层应直接复用这些 service，不应 shell 调 CLI。
3. `MarketStore` (`storage.py`) 使用 `data_control/catalog.duckdb`，管理 runs、partitions、datasets 和 gold_metrics；Silver 是分区 parquet，Bronze 是 JSON。F10 另建了 DuckDB `f10_company` 表，但 schema 当前用 `(code)` 作主键，无法安全表示 A/H 同代码。
4. `industry_graph/` 已有受 contracts 约束的 Entity/Evidence/Relationship 模型；Entity 的 `attributes` 是可扩展字段，适合作为统一公司属性的宿主，而不是新建第三套图数据库。

### 旧 `industry-map.html` 的实际依赖

1. `report_pipeline.build_chain_index()` 生成 `reports/industry/industry-map.html`，并同步至 `data_control/industry/industry-map.html`；测试明确断言 SVG 与该文件存在。
2. `control_center.py` 将 `/industry`、`/industry/`、`/industry/industry-map.html` 指向 `_send_industry_map()`；首页仍链接这一路径。`/industry-v2/` 目前独立服务 Atlas。
3. `package_builder.build_android_package()` 将旧 map 和 Atlas 同时放入 Android zip 的 `extra_files`。
4. Android 的 `MainActivity.refreshIndustryHtml()` 同时复制两个文件；`GraphScreen.GraphTab` 同时显示“产业链全景图”和“SVG 图谱”选项；package importer 的白名单仍接收旧 map。
5. 因而 T-008 必须原子地替换上述五类依赖并更新测试；不能单独删除数据目录中的文件。

### F10 数据路径与现状

1. 原始、断点续抓 F10：`data_control/f10/{cn,hk}/details_*.jsonl`、`quotes_*.jsonl`、`revenue_*.jsonl`、`summary.json`、state/universe caches。
2. Atlas 导出：`data_control/industry/f10/cn_f10.jsonl`、`hk_f10.jsonl`、`meta.json`；当前 meta 记录 CN 5,539、HK 2,806 条。
3. `f10_company` DuckDB 镜像在每次 `run_f10_fetch()` 里按 market 删除并重建；其现有裸字段没有 `currency/as_of/source` 的 MoneySnapshot 语义，`_atlas_record()` 也未输出 `instrument_key`。
4. `industry_atlas.py` 把紧凑公司记录及 `company_index` 内嵌进 20 MB 单文件 HTML；当前 tooltip 是原生 DOM，不是可复用的正式 CompanyPopover/Drawer。

### Android package / WebView 路径

1. Android zip 由 `package_builder.build_android_package()` 写入 `industry/*` 额外文件，签名后以 `/api/android-package` 提供只读下载。
2. Android `GraphScreen.kt` 通过 `file://` WebView 打开已导入的 HTML，`blockNetworkLoads=true`，因此新 frontend/Atlas assets 必须本地打包，不能使用 CDN。
3. Android `GraphSnapshot` / `GraphRepository` 与 HTML Atlas 是平行读取路径；它们可以保留作底层 Evidence/离线搜索能力，但旧 SVG UI 和旧 map 文件不得保留。

### Vue 3 / FastAPI 迁移影响

1. 当前 `pyproject.toml` 没有 FastAPI/Uvicorn，仓库没有 npm/Vite frontend；迁移需要新增受锁定版本管理的 Python 和 Node 前端工程，但不应改写采集、F10、研报、打包业务 service。
2. 首先把 service 结果适配为明确的 domain/API DTO，并通过 FastAPI 调用；这避免 Web 层依赖 CLI stdout 或子进程。
3. Vue 打包产物由 FastAPI 静态托管，API 以 `/api/*` 提供。Router fallback 必须显式保留 Android package GET，而 mutation middleware 必须在 handler 前检查 loopback client 地址。
4. 现有单文件 Atlas 的“完整 F10 嵌入”应改为 chain payload 的 `companyRefs + CompanySummary index`，详情只访问本地 `/api/f10/companies/{instrument_key}`；这既消除 hover 临时外网请求，也避免重复嵌入完整 F10。
