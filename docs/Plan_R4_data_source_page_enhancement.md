# 数据源页面增强计划（R4-T012 / R4-T013）

本文为 `Plan_R4.md` 的增量设计稿，针对用户在通达信金融终端与期货通盘后下载日线/5 分钟线时反复遗忘的六个问题，给出可直接补入第四轮计划的两个任务条目。任务编号承接现有 R4-T001～R4-T011，新增 **R4-T012**（后端数据能力与 API）和 **R4-T013**（前端数据源页面整合），二者构成单向依赖：T013 依赖 T012 的接口，T012 不依赖 T013 的页面。

## 背景与问题陈述

用户每次盘后下载后，需要快速回答以下问题，当前 `/api/data-sources` 及 `DataSourcesView.vue` 只能部分回答：

| 问题 | 现状 | 缺口 |
| --- | --- | --- |
| 下载了哪些市场/标的的数据？ | inventory 有 market/assetType/period/instruments/rows | 无按市场的标的数/行数/磁盘汇总，无标的级清单 |
| 数据来源是哪里？ | sources + sourceDetails 有 providerId/endpoint/status | 无法一眼看到主源/备用源的端点和认证方式 |
| 更新日期是几号？ | lastUpdatedAt/latestBarAt/earliestBarAt 存在 | 分散在表格行内，无统一"数据截止日"卡片 |
| 中间有没有中断的日期时间？ | 只有 earliest→latest 两端 | **完全缺失**：无交易日历比对、无中断段检测 |
| 每个市场的数据占了多少 GB？ | 只有行数，无字节数 | **完全缺失**：无 Parquet 文件体积统计 |
| 数据源有没有主源和备用源？ | 有路由偏好 UI 和 PUT 保存 | 偏好与实际落库来源未并排展示，无自动推断 |
| 数据需要作哪些清洗或处理？ | tdx-local-normalization 有审计 JSON | 清洗逻辑分散在 ADR/代码/审计中，无页面集中说明 |
| 证券代码前缀/后缀归属哪个分类？ | market_classification.py 有规则 | 规则不在数据源页暴露，用户每次要问 Codex |

## 设计原则

1. **只读优先**：新增的四个能力在数据源页面全部是只读展示或只读检测，不新增写操作；路由偏好保存仍走现有 `PUT /api/data-sources`。
2. **不扫描全量 Silver 行**：中断检测和磁盘统计都基于 K 线清单（`instrument_file` / `instrument_latest`）和文件系统元数据，不打开 Parquet 行存；与现有 `_manifest_connection` 轻量级模式一致。
3. **复用现有交易日历**：中断检测复用 `futures_calendar.load_futures_trading_calendar` 和 `aggregation._trading_day_for_bar` 的日历逻辑，不重建日历源。
4. **复用现有分类规则**：代码前缀/后缀归属展示复用 `market_classification.py` 和 `config/market_classification.json`，不新建第二份规则。
5. **清洗说明集中但不重复**：把分散在 `tdx-cn-v2` 审计、ADR、能力矩阵中的清洗口径集中成页面可读的说明区，内容指向权威文档，不抄写第二份真相。
6. **与 R4-T010/R4-T011 不重叠**：T010（待分类审计表）和 T011（tdx-cn-v2 迁移）的验收和状态不变；本任务只读取它们产出的审计报告和分类规则，不改写其代码或数据。

---

## R4-T012 — 数据源健康盘点：中断检测、磁盘占用与代码归属 API

- `type`：本地数据审计、FastAPI 与交易日历工程；`priority`：P0；`state`：NEW；`failure_count`：0。
- `目标`：为数据源页面提供三个只读后端能力——按交易日历的中断段检测、按市场/资产/周期的 Silver Parquet 磁盘占用统计、以及证券代码前缀/后缀到标准分类的归属查询——使前端无需每次向 Codex 提问即可回答"中断了吗/占多大/这是什么标的"。
- `依赖`：`market_query_cache` 的 K 线清单（`instrument_file` 表）、`futures_calendar.load_futures_trading_calendar`、`market_classification.market_classification_spec`；无新增外部数据源。
- `范围边界`：只新增只读 GET 端点和纯函数计算模块；不修改 Silver 写入路径、不修改 `market_classification.py` 的分类逻辑、不修改 `futures_calendar.py` 的同步逻辑。中断检测只覆盖已入库 Silver 的 K 线时间范围，不检测 Bronze/Raw 层。

### 中断日期检测

#### 口径定义

- **检测粒度**：按 `market + assetType + period` 类别（即 inventory 的 `categoryKey`），在该类别下按标的（`instrument_id`）独立检测，再汇总为类别级中断摘要。
- **交易日历来源**：优先使用 `load_futures_trading_calendar(data_root)`（期货统一日历）；A 股/港股日线的中断检测使用该标的 `earliestBarAt → latestBarAt` 区间内的实际已入库交易日集合与日历的差集。若 `load_futures_trading_calendar` 返回 `None`（日历未同步），则回退为"仅检测连续自然日缺口"模式，并在响应中标记 `calendarMode: "UNAVAILABLE_NATURAL_FALLBACK"`，不伪造日历。
- **中断段定义**：一个中断段（gap segment）是交易日历中该标的应有、但 Silver 中没有对应 `bar_open_time` 的连续交易日区间。单日缺失也是一段（长度=1）。中断段必须记录 `startDate`、`endDate`、`missingTradingDays`、`categoryKey`、`instrumentId`。
- **分钟线特殊规则**：5 分钟线不按交易日历逐日检测，而是按"每个交易日内应有 48 根（A股）或按期货时段应有根数"检测日内缺失；当前 V1 只检测"该交易日是否有任意 5m bar"（即降级为日线粒度的存在性检查），并在响应中标记 `intradayGranularity: "DAY_LEVEL_PRESENCE"`，不声称检测了每 5 分钟。
- **排除规则**：标的的 `earliestBarAt` 之前和 `latestBarAt` 之后不检测（只检测已入库区间内的中断）；已退市/到期合约的最后交易日之后不报中断。

#### 接口

```
GET /api/data-sources/gaps?category_key=&instrument_id=&min_gap_days=1&limit=50&offset=0
```

响应（camelCase，缺失为 `null`，空数组为 `[]`）：

```jsonc
{
  "generatedAt": "...",
  "calendarMode": "FUTURES_UNIFIED | UNAVAILABLE_NATURAL_FALLBACK",
  "intradayGranularity": "DAY_LEVEL_PRESENCE",
  "summary": {
    "categoryKey": "CN:STOCK:1d",
    "instrumentsChecked": 7118,
    "instrumentsWithGaps": 43,
    "totalGapSegments": 51,
    "totalMissingTradingDays": 87,
    "worstGap": { "instrumentId": "...", "startDate": "...", "endDate": "...", "missingTradingDays": 12 }
  },
  "gaps": [
    {
      "categoryKey": "CN:STOCK:1d",
      "instrumentId": "CN.SSE.STOCK.600000",
      "symbol": "600000",
      "name": "浦发银行",
      "earliestBarAt": "...",
      "latestBarAt": "...",
      "segments": [
        { "startDate": "2026-02-05", "endDate": "2026-02-07", "missingTradingDays": 3 }
      ],
      "totalMissingDays": 3
    }
  ],
  "pagination": { "offset": 0, "limit": 50, "total": 51 }
}
```

- `min_gap_days` 默认 1；传 `min_gap_days=3` 只返回中断 ≥3 日的段，用于快速定位大段中断。
- 不带 `category_key` 时跨所有类别汇总，`summary` 为全市场摘要，`gaps` 按中断天数降序排列。
- 响应只读清单元数据 + 交易日历差集，不打开 Parquet 行文件。

#### 实现要点

1. 新增 `desktop/src/market_monitor/data_source_health.py`，包含三个纯函数：`detect_gap_segments(...)`、`compute_disk_usage(...)`、`classify_symbol_by_prefix(...)`。
2. `detect_gap_segments` 从 `_manifest_connection` 读取每个标的的 `earliest_bar_at`/`latest_bar_at`，再用 `instrument_file` 表按 `instrument_id + period` 聚合得到该标的实际出现的交易日集合（`DISTINCT bar_open_time::DATE`）；与日历做集合差得到中断段。
3. 清单表 `instrument_file` 如果不包含逐交易日去重信息，则回退为"读取 `instrument_period` 表的 `earliest/latest` + 日历推算应有天数 vs `row_count` 推算实际天数"的近似法，并标记 `detectionMode: "ROW_COUNT_APPROXIMATE"`；只有清单包含逐日信息时才标记 `detectionMode: "EXACT_TRADING_DAY_SET"`。V1 允许近似模式，但必须在响应中显式标注。
4. 检测结果不落库（不新建 Gold 表），每次请求实时计算；清单连接和日历加载各为 O(1) 打开，集合运算是 O(标的数 × 平均交易日数)，对约 1 万标的可在亚秒级完成。如果实测超过 2 秒，再考虑加 DuckDB 临时表缓存。

### 磁盘 GB 占用统计

#### 口径定义

- **统计对象**：`data_control/silver/**/*.parquet` 的实际文件体积，按 `market / assetType / period` 三级聚合。
- **单位**：API 返回原始字节数（`bytes`），页面换算为 KB/MB/GB；不在此层做人类可读格式化。
- **统计方式**：`Path.rglob("*.parquet")` + `Path.stat().st_size`，不打开文件内容。同时统计每个类别的 `fileCount`。
- **附加维度**：同时统计 `catalog.duckdb`、`bronze/**`、`f10/**`、`personal/**`、`logs/**`、`state/**` 的总体积，给出"数据层占用分布饼图"所需的数据。
- **幂等与缓存**：首次请求遍历目录树，结果写入 `data_control/state/disk_usage_cache.json`（带 `computedAt` 时间戳）；后续请求在 5 分钟内直接读缓存，超时或 `?refresh=true` 时重新遍历。

#### 接口

```
GET /api/data-sources/disk-usage?refresh=false
```

响应：

```jsonc
{
  "generatedAt": "...",
  "computedAt": "...",
  "cacheTtlSeconds": 300,
  "silver": [
    { "categoryKey": "CN:STOCK:1d", "market": "CN", "assetType": "STOCK", "period": "1d",
      "bytes": 1234567890, "fileCount": 42, "gb": 1.15 }
  ],
  "layers": [
    { "layer": "silver", "bytes": 8589934592, "gb": 8.0, "fileCount": 310 },
    { "layer": "catalog", "bytes": 104857600, "gb": 0.1, "fileCount": 1 },
    { "layer": "bronze", "bytes": 2147483648, "gb": 2.0, "fileCount": 1800 },
    { "layer": "f10", "bytes": 536870912, "gb": 0.5, "fileCount": 8345 },
    { "layer": "personal", "bytes": 102400, "gb": 0.0001, "fileCount": 3 },
    { "layer": "logs", "bytes": 204800, "gb": 0.0002, "fileCount": 12 },
    { "layer": "state", "bytes": 51200, "gb": 0.00005, "fileCount": 5 }
  ],
  "total": { "bytes": 11274289152, "gb": 10.5, "fileCount": 10176 }
}
```

- `silver` 数组按 `categoryKey` 粒度，与 inventory 的 `categoryKey` 对齐，前端可做行对齐展示。
- `layers` 数组按数据层（silver/catalog/bronze/f10/personal/logs/state）粒度，用于饼图。
- `?refresh=true` 强制重新遍历，绕过缓存。

#### 实现要点

1. `compute_disk_usage(data_root)` 在 `data_source_health.py` 中实现，接受 `Path` 返回上述结构。
2. 目录遍历使用 `Path.rglob`，对 `data_control` 下约 1 万个 Parquet 文件，实测应在 1～3 秒；缓存 5 分钟避免频繁遍历。
3. 缓存文件 `disk_usage_cache.json` 写入 `data_control/state/`，该目录已被 `.gitignore` 忽略。
4. Silver 子目录的 `market=*/asset_type=*/period=*/year=*` 分区路径解析：从路径段提取 `market`/`asset_type`/`period` 值，聚合到 `categoryKey`；无法解析的文件归入 `UNPARSEABLE` 并单独返回。

### 证券代码前缀/后缀归属查询

#### 口径定义

- **数据源**：`market_classification_spec()` 返回的 `config/market_classification.json`（schemaVersion=1），包含 `categories`、`exchangeAliases`、`aShare.tdxBoardPrefixes`、`aShare.etfPrefixes` 等规则。
- **查询方式**：用户输入一个代码（如 `600000`、`510300`、`RB2610`、`00700`），后端返回该代码在当前规则下命中的分类、交易所、资产类型和命中的规则来源（前缀/后缀/显式字段/允许列表）。
- **未命中行为**：未命中任何规则的代码返回 `matched: false` + `reason: "NO_RULE_MATCH"`，不猜测分类；与 R4-T010 的 `unclassified` 语义一致。
- **与待分类表的关系**：本接口只做"单代码即时归属查询"，不读写 R4-T010 的待分类审计表；两个接口互补但独立。

#### 接口

```
GET /api/data-sources/classify-symbol?code=600000&exchange=SSE&asset_type=STOCK
```

响应：

```jsonc
{
  "code": "600000",
  "exchange": "SSE",
  "assetType": "STOCK",
  "matched": true,
  "category": "a-sh",
  "categoryLabel": "沪深主板",
  "matchedRules": [
    { "rule": "tdxBoardPrefixes", "prefix": "600", "source": "config/market_classification.json#aShare.tdxBoardPrefixes" }
  ],
  "canonicalInstrumentId": "CN.SSE.STOCK.600000",
  "unclassified": false
}
```

- `exchange` 和 `asset_type` 为可选参数；不传时只用代码前缀规则匹配，返回所有可能的分类候选。
- `matchedRules` 为数组，因为一个代码可能同时命中多条规则（如 ETF 前缀 + 指数类型），按优先级排列。
- 接口纯只读，不写任何文件。

#### 实现要点

1. `classify_symbol_by_prefix(code, exchange, asset_type)` 在 `data_source_health.py` 中实现，内部调用 `market_classification_spec()` 和现有 `classify_instrument` 逻辑（如有）；不复制规则，只引用。
2. 如果 `market_classification.py` 当前没有暴露"单代码即时查询"的公开函数，可在本模块中封装一个薄适配层，调用其内部 `_a_share_category` / `_prefix` 等函数，但不在 `market_classification.py` 中新增公共 API（避免影响 T010）。
3. 前缀/后缀规则的完整列表也可通过 `GET /api/data-sources/classification-rules` 端点暴露，供前端渲染"代码前缀归属表"：

```
GET /api/data-sources/classification-rules
```

返回 `market_classification.json` 的结构化子集：所有 `tdxBoardPrefixes`、`etfPrefixes`、`exchangeAliases` 和类别列表，前端据此渲染只读规则表。

### 验收标准

- **中断检测**：用确定性夹具构造 3 个标的——一个无中断、一个单日中断、一个跨春节 5 日中断——验证 `detect_gap_segments` 返回正确段数、段长和日期；日历不可用时回退模式标记 `UNAVAILABLE_NATURAL_FALLBACK`；`latestBarAt` 之后不报中断。
- **磁盘统计**：用临时目录构造 3 个 Parquet 文件分布于 `silver/market=CN/asset_type=STOCK/period=1d/year=2025/` 和 `silver/market=HK/...`，验证 `compute_disk_usage` 返回正确字节数、文件数和 `categoryKey` 聚合；`?refresh=true` 绕过缓存。
- **代码归属**：验证 `600000` → `a-sh`、`510300` → `a-etf`、`RB2610` → `cn-future`（如规则覆盖）、`00700` → `hk-stock`、`XXXXXX`（未命中）→ `matched: false`；`exchange` 和 `asset_type` 可选参数行为正确。
- **性能**：中断检测在本机约 1 万标的时 P95 < 2 秒；磁盘统计首次遍历 < 3 秒、缓存命中 < 50ms。
- **安全**：三个端点全部只读，不修改任何文件或数据库；`disk_usage_cache.json` 写入被忽略的 `state/` 目录。
- **测试**：新增 `desktop/tests/test_data_source_health.py`，覆盖上述夹具用例；Ruff 通过；不引入新依赖。
- **文档**：更新 `docs/ARCHITECTURE.md` 的 `web_api/sources.py` 段落，列出三个新端点；更新 `docs/DATA_SOURCE_CAPABILITY_MATRIX.md` 的"当前本地存量"表，增加中断检测和磁盘统计能力行。

---

## R4-T013 — 数据源页面前端整合：健康总览与清洗说明

- `type`：Vue 3 网页端信息架构与交互；`priority`：P0；`state`：NEW；`failure_count`：0。
- `目标`：将 R4-T012 的三个后端能力整合到 `DataSourcesView.vue`，新增"数据源健康总览"卡片组（覆盖/中断/磁盘/主备源）和"数据清洗处理说明"区域，并在现有库存表旁新增"代码前缀归属表"只读面板，使用户打开数据源页即可一眼回答全部八个问题。
- `依赖`：R4-T012 的三个 API 端点；现有 `DataSourcesView.vue` 的 `apiGet` / `formatTime` / `formatMarket` / `formatAssetType` 等 domain 函数；Element Plus 表格/标签/进度条组件。
- `范围边界`：只修改 `DataSourcesView.vue` 及其关联 composable；不修改后端代码、不修改路由（`/data-sources` 路由不变）、不修改 `market_classification.py`。清洗说明区的内容从 `tdx-cn-v2` 审计、ADR 和能力矩阵中提取摘要，指向权威文档，不在页面中维护第二份真相。

### 页面信息架构调整

当前页面布局（从上到下）：

1. 页面标题 + 刷新/保存按钮
2. 通达信证券标准化状态面板
3. overview-strip 摘要（表/数据集/K线记录/标的数）
4. 本地数据库浏览器（物理表）
5. 数据集注册目录
6. K 线字段口径
7. 分钟 K 线字段规则
8. 本地数据类别与路由（inventory 表 + 主备源选择）
9. 已实现 Provider 注册表

新增/调整后布局：

1. 页面标题 + 刷新/保存按钮（不变）
2. **【新增】数据源健康总览卡片组**（4 张卡片，一行排列）
3. **【新增】数据中断检测面板**（可折叠表格）
4. **【新增】磁盘占用分布面板**（饼图 + 按类别柱状图）
5. 通达信证券标准化状态面板（不变）
6. overview-strip 摘要（不变）
7. **【新增】证券代码前缀归属表**（只读规则表 + 单代码查询输入框）
8. 本地数据库浏览器（不变）
9. 数据集注册目录（不变）
10. K 线字段口径（不变）
11. 分钟 K 线字段规则（不变）
12. 本地数据类别与路由（inventory 表新增"磁盘 GB"和"中断摘要"两列）
13. 已实现 Provider 注册表（不变，但新增"主源/备用源"标签叠加在已有路由偏好列旁）
14. **【新增】数据清洗处理说明**（集中说明区）

### 健康总览卡片组（4 张卡片）

每张卡片为一个 `el-card`，顶部为标题，中间为大数字，底部为说明文字。一行排列，窄屏折为 2×2 或单列。

| 卡片 | 标题 | 主数字 | 副信息 | 数据来源 |
| --- | --- | --- | --- | --- |
| 1 | 数据覆盖 | 已入库标的数 | `X 个标的 · Y 万行 · Z 个类别` + 最新数据日期 | `payload.summary` + `inventory` 的 `latestBarAt` 最大值 |
| 2 | 数据中断 | 有中断的标的数 | `X 个标的存在中断 · 共 Y 个中断段 · 最长中断 Z 日` + "查看明细"链接 | `/api/data-sources/gaps` 的 `summary` |
| 3 | 磁盘占用 | Silver 总 GB | `Silver X GB · Bronze Y GB · Catalog Z GB` + "查看分布"链接 | `/api/data-sources/disk-usage` 的 `layers` + `total` |
| 4 | 数据源状态 | 可用 Provider 数 | `X 个已配置 · Y 个未配置` + 主源/备用源覆盖提示 | `/api/data-sources/providers` 的 `configured` 计数 + inventory 的 `sources` 去重 |

- 卡片 2 中断数和卡片 4 主备源覆盖为绿色（无中断/主备齐全）、黄色（有中断或缺备用源）、红色（大面积中断或无主源），用 `el-tag` 的 `type` 属性。
- 卡片组数据在 `onMounted` 时与现有 `load()` 并行请求，使用 `Promise.allSettled`，单个失败不阻塞其他卡片。

### 数据中断检测面板

- 默认折叠（`el-collapse`），标题显示中断摘要（"3 个标的存在中断，共 5 段，最长 7 日"）。
- 展开后为 `el-table`，列：标的代码、名称、市场/类型、周期、最早数据、最新数据、中断段数、最长中断、中断明细（可展开行显示每段的 `startDate→endDate` 和 `missingTradingDays`）。
- 顶部筛选：`category_key` 下拉（从 inventory 的类别生成）、`min_gap_days` 输入（默认 1）、"只看中断 ≥3 日"快捷按钮。
- 表格分页，默认每页 50 条；`el-pagination`。
- 若 `calendarMode === "UNAVAILABLE_NATURAL_FALLBACK"`，显示 `el-alert` 提示"交易日历未同步，当前为自然日近似检测，请运行 `futures-calendar-sync` 后刷新"。
- 若 `detectionMode === "ROW_COUNT_APPROXIMATE"`，在表格标题旁显示 `el-tooltip` 解释近似模式。

### 磁盘占用分布面板

- 左侧为 ECharts 饼图（数据层分布：silver/bronze/catalog/f10/personal/logs/state），右侧为按 `categoryKey` 的柱状图（横轴为类别，纵轴为 GB）。
- 饼图和柱状图复用现有 ECharts 6 实例生命周期基线（与 FuturesView 一致的 `onMounted`/`onUnmounted` 管理）。
- 柱状图支持点击某根柱子，下钻显示该类别下按 `year` 分区的占用明细（需后端 `disk-usage` 接口在 `silver` 数组中可选返回 `byYear` 子结构；V1 可不实现下钻，显示"按年份明细待后续实现"）。
- 顶部显示 `computedAt` 和"刷新"按钮（调用 `?refresh=true`）。

### 证券代码前缀归属表

- 上半部为单代码查询输入框（`el-input` + `el-button`"查询"），输入代码后调用 `/api/data-sources/classify-symbol`，在下方显示命中结果卡片（分类、交易所、资产类型、命中的规则、标准标的 ID）。
- 下半部为只读规则表（`el-table`），数据来自 `/api/data-sources/classification-rules`，列：代码前缀/后缀、命中分类、命中交易所、规则来源（`config/market_classification.json` 的 JSON 路径）、说明。
- 规则表支持搜索筛选（按前缀或分类名）。
- 未命中时显示 `el-empty`"该代码未命中任何已登记规则，将进入待分类审计表"。

### inventory 表新增列

在现有"本地数据类别与路由"表格中新增两列：

- **磁盘 GB**：从 `disk-usage` 的 `silver` 数组按 `categoryKey` 对齐，显示 `X.XX GB`；无数据时显示 `—`。
- **中断摘要**：从 `gaps` 接口的 `summary` 按 `categoryKey` 对齐，显示 `N 段 / 最长 M 日`；无中断时显示绿色 `el-tag`"完整"；有中断时显示黄色 `el-tag` 可点击跳转到中断面板。

### 数据清洗处理说明区

位于页面最底部，为只读说明面板，内容从权威文档提取摘要并指向原文。分三个子区：

1. **通达信证券清洗（tdx-cn-v2）**：概述 A 股/B 股/ETF/转债/回购的资产级价格精度规则、逐行成交量倍率验证、隔离区机制；指向 `docs/ADR.md` 的 ADR 和 `docs/TICKDB_TDX_DATA_AUDIT_2026-08-24.md`（历史审计）以及最新 `tdx-local/latest-audit.json`（页面已通过 `tdx-local-normalization` 端点展示统计数字）。
2. **期货数据清洗**：概述 `bulk-futures` 的 L7/L8/L9 导入、AKShare 主连备用逻辑、`futures-calendar-sync` 和 `futures-rule-sync` 的交易日/规则快照、多空热度 Gold 构建顺序；指向 `Plan_R4.md` 的 R4-T008 和 `docs/DATA_SOURCE_CAPABILITY_MATRIX.md`。
3. **通用字段口径**：概述 OHLC 标准字段、volume/amount/open_interest 缺失为 NULL 不补零、涨跌停不用统一阈值、派生周期只在查询时生成；指向现有"K 线字段口径"和"分钟 K 线字段规则"面板及 `docs/DATA_SOURCE_CAPABILITY_MATRIX.md` 的统一口径段。

- 说明区使用 `el-descriptions` 或 `el-collapse` 组件，默认折叠，用户需要时展开。
- 内容文字为静态常量（TypeScript `const`），不调用后端；但每条说明附带指向权威文档的相对路径链接（页面内 `router-link` 或外链）。
- 当 R4-T011 的审计状态或 R4-T008 的公式版本变化时，只需更新对应的 TypeScript 常量和指向的权威文档，不在页面中硬编码具体数字。

### 验收标准

- **Vue 构建**：`npm run build` 通过，无 TypeScript 类型错误，无未使用变量警告。
- **Playwright 覆盖**：
  - 健康总览 4 张卡片在数据存在/为空两种状态下正确渲染。
  - 中断检测面板折叠/展开、筛选 `min_gap_days`、分页、中断段展开行、日历不可用时的 `el-alert` 提示。
  - 磁盘占用饼图和柱状图渲染、`computedAt` 显示、刷新按钮触发 `?refresh=true`。
  - 代码前缀归属表：输入 `600000` 查询命中、输入未命中代码显示 `el-empty`、规则表搜索筛选。
  - inventory 表新增"磁盘 GB"和"中断摘要"列正确对齐、无中断时绿色标签、有中断时黄色标签可跳转。
  - 清洗说明区三个子区折叠/展开、链接指向正确文档路径。
  - 浅色/深色主题、窄屏 2×2/单列布局。
  - API 失败时显示错误 `el-alert`，不显示空白或模拟值。
- **一致性**：健康总览卡片 2 的中断数与中断检测面板的摘要数字一致；卡片 3 的 Silver GB 与磁盘面板饼图 silver 层一致；inventory 表"磁盘 GB"列合计与卡片 3 一致。
- **性能**：页面首次加载（含 4 个新 API 并行请求）在本地数据量下 P95 < 3 秒；磁盘缓存命中时 < 1 秒。
- **无第三方请求**：页面所有请求只指向本机 FastAPI，无第三方网络调用。
- **文档**：更新 `docs/ARCHITECTURE.md` 的 Vue 网页端段落，记录数据源页面新增的健康总览、中断检测、磁盘占用、代码归属和清洗说明区。

---

## R4-T012 推荐执行顺序

1. 新建 `data_source_health.py`，先实现 `classify_symbol_by_prefix`（最简单、无 IO），用 `market_classification.json` 的现有规则跑通单代码查询。
2. 实现 `compute_disk_usage`，构造临时目录夹具验证字节聚合和缓存逻辑。
3. 实现 `detect_gap_segments`，先以 `ROW_COUNT_APPROXIMATE` 模式跑通，再根据清单表实际结构决定是否能升级为 `EXACT_TRADING_DAY_SET`。
4. 在 `sources.py` 注册三个新 GET 路由，复用现有 `_data_root` / `_manifest_connection` / `clean` / `now_iso` 辅助函数。
5. 编写 `test_data_source_health.py`，覆盖全部夹具用例和边界条件。
6. 执行 Ruff + 相关 pytest + Vue build + 相关 Playwright；同步更新 `ARCHITECTURE.md` 和 `DATA_SOURCE_CAPABILITY_MATRIX.md`。

## R4-T013 推荐执行顺序

1. 在 `DataSourcesView.vue` 的 `onMounted` 中新增三个 API 请求（gaps/disk-usage/classification-rules），用 `Promise.allSettled` 与现有请求并行；定义对应的 TypeScript interface。
2. 实现健康总览卡片组（4 张 `el-card`），先硬接线到 API 返回值，再做绿/黄/红状态色。
3. 实现中断检测面板（折叠 + 表格 + 筛选 + 分页 + 展开行）。
4. 实现磁盘占用面板（ECharts 饼图 + 柱状图，复用现有 ECharts 生命周期基线）。
5. 实现代码前缀归属表（查询输入框 + 只读规则表 + 搜索）。
6. 在 inventory 表新增"磁盘 GB"和"中断摘要"两列，对齐 `categoryKey`。
7. 实现清洗说明区（`el-collapse` + 静态常量 + 文档链接）。
8. 编写 Playwright 用例，覆盖全部新增交互和状态。
9. 执行 `npm run build` + 全量 Playwright；浅色/深色主题和窄屏人工验收；同步文档。

---

## 与现有任务的关系

- **R4-T010（待分类审计表）**：T012 的代码归属查询与 T010 的待分类表互补——归属查询是"输入代码即时查规则"，待分类表是"列出所有未命中规则的标的"。两者共享 `market_classification.py` 规则，但接口和代码独立。
- **R4-T011（tdx-cn-v2 迁移）**：T013 的清洗说明区引用 T011 的审计报告和 ADR，但不改写 T011 的代码或数据。`tdx-local-normalization` 端点继续由 T011 维护，T013 只在页面上增加指向它的说明文字。
- **R4-T008（多空热度）**：T013 的清洗说明区"期货数据清洗"子区引用 T008 的 Gold 构建顺序，但不重复其公式细节。
- **Plan_R4.md 接入方式**：将本文的 R4-T012 和 R4-T013 两节直接追加到 `Plan_R4.md` 的"R4 任务"段落末尾（R4-T011 之后），并在 Plan_R4.md 顶部的当前开发状态中追加一行"2026-08-29 R4 进展：数据源页面增强任务 R4-T012/R4-T013 已立项，新增中断检测、磁盘占用、代码归属和清洗说明四个能力"。
